"""Newsflasharr per-tick delivery pipeline.

Wires store (Task 3), spool (Task 4), routing (Task 5), gates (Task 6) and
channels (Task 7) together into one pass over `pending/`: parse -> route ->
admit -> send (per-channel retry) -> ledger -> delete, then flush any digests
Gates has released. Stdlib only. Imports nothing from the other notifier
modules directly -- the caller (plugin.py / tests) injects them via `mods`
because a standalone-loaded module (conftest's `load_mod`) can't do relative
imports (see Task 8 brief wiring note). Spec §4.2, §4.5, §4.7.
"""
from __future__ import annotations

import json
import os
import time

BACKOFF_S = (30.0, 300.0, 1800.0)

# Ticker outcomes that are genuine FAILURES rather than the operator's own
# gates declining a banner. Terminal either way -- a banner is a statement
# about NOW, so it never enters the retry ladder -- but they must not share
# a counter with `below_threshold`/`source_filtered`.
_TICKER_FAILURES = ("write_failed", "clear_failed", "error")

# The ONLY reasons counted as declines. `Ticker.note` also returns
# `cleared_by_resolve` (a SUCCESSFUL banner clear), `empty` and `disabled`
# into the same ticker_skipped branch; counting those would report a success
# as a decline. Membership-gating also bounds state.json to <=2 keys no
# matter what note() returns.
_TICKER_DECLINES = ("below_threshold", "source_filtered")

DEFAULT_MAX_AGE_H = 24


def _coerce_max_age_h(raw):
    """A usable max event age, whatever the settings row holds.

    `0` expired essentially EVERY event on arrival -- `now_ms - ts > 0` is true
    for anything spooled before the tick read it -- ledgering `expired`,
    deleting the file and bumping `failed`, criticals included. `None` and a
    non-numeric string raised a TypeError from spool.parse_event, OUTSIDE its
    try. Nothing validated the field anywhere: the UI's `min: 1` is
    browser-side only and was never re-checked server-side.

    There is no useful meaning for "expire immediately", so anything <= 0 or
    unreadable falls back to the default rather than silently discarding the
    operator's alerts.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_H
    return value if value > 0 else DEFAULT_MAX_AGE_H


def note_delivery_failure(state, kind, detail, now):
    """Record a TERMINAL delivery failure so a surface can report it.

    `stats` is per-tick and is overwritten as `last_stats` every tick, so a
    channel that failed every send for a week left no trace an operator could
    see: show_status read last_stats for two unrelated keys and its problems
    list had NO failure condition at all. These counters are cumulative, and
    `last_failure_ts` is what lets a reporter distinguish "failing NOW" from
    "failed once, months ago" -- a bare cumulative counter would go permanently
    red on one ancient failure.

    Never raises: a health counter must not break the delivery it measures.
    """
    try:
        block = state.get("delivery")
        if not isinstance(block, dict):
            block = {}
        block[kind] = int(block.get(kind) or 0) + 1
        block["last_failure_ts"] = now
        block["last_failure"] = str(detail)[:200]
        state["delivery"] = block
    except Exception:
        pass


class Deliverer:
    def __init__(self, store, settings, version, now_fn, mods, opener=None,
                 wire_redact=None, smtp_factory=None, ticker=None):
        self.store = store
        self.settings = settings
        self.version = version
        self.now_fn = now_fn
        self.mods = mods
        self.opener = opener
        # Spec §8.1 wire-side redaction, defense-in-depth: applied to a COPY
        # of title/body/url just before channels.send, never to the stored
        # event/sidecar. `wire_redact=None` (default) is a byte-identical
        # no-op -- every caller that doesn't pass it (all locked Task 7/8
        # tests, plugin.py's flush_held) is unaffected.
        self.wire_redact = wire_redact
        # Test seam for the smtp channel (conftest.FakeSMTP). None in
        # production, where the transport selects smtplib itself. Appended
        # last on purpose: every call site passes 5 positionals then
        # keywords, so appending breaks nothing and inserting breaks all.
        self.smtp_factory = smtp_factory
        # Per-TICK scratch shared with channels.send: a dead mail server
        # otherwise costs a full connect stall per EVENT, and this tick is
        # the single leader thread for every channel, not just email.
        self._tick_state = {}
        # On-screen ticker, injected like wire_redact so deliver.py keeps
        # no sibling import and None stays a byte-identical no-op.
        self.ticker = ticker

    def _wire_copy(self, event):
        """A copy of `event` with title/body/url passed through
        `wire_redact`, or `event` itself, untouched, when no `wire_redact`
        is configured."""
        if self.wire_redact is None:
            return event
        redacted = dict(event)
        for key in ("title", "body", "url"):
            value = redacted.get(key)
            if isinstance(value, str):
                redacted[key] = self.wire_redact(value)
        return redacted

    # -- tick --------------------------------------------------------------

    def tick(self, now_ms, budget_s=30.0, max_sends=10):
        now = now_ms / 1000.0
        self._tick_state = {}          # fresh breaker each tick
        # "skipped" = every routed channel declined it (today: the ticker's own
        # severity/source gates). Deliberately NOT folded into "failed": nothing
        # failed, so counting it there would make show_status report a
        # correctly-configured system as broken.
        stats = {"delivered": 0, "held": 0, "collapsed": 0, "failed": 0,
                 "retries_due": 0, "skipped": 0}
        state = self.store.load_state(now)
        # Exposed for Collector._tick, which appends its housekeeping keys to
        # this same dict rather than re-reading state.json from disk -- the
        # tick otherwise paid an unconditional second full JSON parse forever
        # (and, at the documented 500-held storm size, a real one).
        self.state = state
        g = self.mods["gates"].Gates(state, self.settings, lambda: now)
        enabled = self.mods["channels"].enabled_channels(self.settings)

        # Recorded every tick so the status action reports what the DELIVERING
        # process resolved, which is not necessarily what the settings on the
        # operator's screen would resolve to. Only the two flat values: the
        # available-names list belongs on the validate readout, not in a file
        # that gets pasted into issues.
        connect = self.mods["channels"].connect_status(self.settings)
        state["connect"] = {"state": connect["state"],
                            "reason": connect["reason"]}

        if not enabled:
            # Fail-closed (spec §4.3): heartbeat still beats, but nothing is
            # claimed, sent, or deleted while no channel is configured.
            self.store.save_state(state, now)
            stats["held_no_channels"] = len(
                self.mods["spool"].pending_events(self.store.pending_dir))
            return stats

        # Wall-clock budget is measured against real elapsed time, not the
        # (possibly fake / injected) event-time `now_ms` -- a FakeClock
        # frozen far from the real clock must never make the very first
        # deadline check trip.
        deadline = time.monotonic() + budget_s
        sends = 0
        held_paths = []

        for path in self.mods["spool"].pending_events(self.store.pending_dir):
            if time.monotonic() >= deadline or sends >= max_sends:
                break
            sends += self._process_one(path, g, state, now, enabled, stats,
                                        held_paths)

        # due_flushes() EAGERLY pops closed dedup windows / released held
        # records out of state["gates"] the moment it's called -- capture
        # the whole list up front so a budget/send-limit break partway
        # through delivery doesn't just drop the rest on the floor. Any
        # digest we don't reach this tick gets durably requeued below
        # (same at-least-once contract as the total-failure path in
        # _deliver_digest); a digest that WAS delivered above is never
        # revisited, since the loop only requeues due[idx:] -- everything
        # from the current (not-yet-delivered) index onward.
        due = list(g.due_flushes())
        for idx, digest in enumerate(due):
            if time.monotonic() >= deadline or sends >= max_sends:
                for not_yet_delivered in due[idx:]:
                    self._requeue_digest(not_yet_delivered, now)
                break
            sends += self._deliver_digest(digest, now, stats)

        state_written = self.store.save_state(state, now)

        # Held events live in state, not as pending files (locked design
        # note): only delete the file once the held record has been
        # durably persisted, so a crash mid-hold can't lose the event.
        #
        # That comment was already here and the code did not honour it: the
        # save's bool was discarded and the files were deleted regardless, so
        # a full disk or the E3 permission trap destroyed held CRITICALS while
        # the ledger claimed they were held. Keeping the files costs a
        # duplicate at worst (delivery is at-least-once by contract); deleting
        # them loses the event outright.
        if state_written:
            for path in held_paths:
                self._delete_file(path)
        elif held_paths:
            stats["state_write_failed"] = len(held_paths)
            self.store.ledger({"outcome": "state_write_failed",
                                "held": len(held_paths)})

        return stats

    # -- per-event pipeline --------------------------------------------------

    def _process_one(self, path, g, state, now, enabled, stats, held_paths):
        now_ms = int(now * 1000)
        max_age_h = _coerce_max_age_h(self.settings.get("max_event_age_h"))
        event, reason = self.mods["spool"].parse_event(path, now_ms, max_age_h)

        if event is None:
            if reason == "expired":
                self.store.ledger({"outcome": "expired",
                                    "path": os.path.basename(path)})
                self._delete_file(path)
            else:
                if self.store.move_failed(path, reason, now):
                    self.store.ledger({"outcome": "malformed", "reason": reason,
                                        "path": os.path.basename(path)})
                else:
                    # It is STILL in pending/ and will be re-parsed every tick.
                    # Saying `malformed` here would assert a quarantine that
                    # did not happen.
                    self.store.ledger({"outcome": "quarantine_failed",
                                        "reason": reason,
                                        "path": os.path.basename(path)})
            stats["failed"] += 1
            return 0

        source = event.get("source")
        title = event.get("title")
        dedup_key = event.get("dedup_key")

        outcomes = self.mods["spool"].read_outcomes(path)
        if not outcomes:
            routed_all = self.mods["routing"].route(
                event,
                self.settings.get("_effective_routing_rules",
                                  self.settings.get("routing_rules", [])),
                self.settings.get("default_channels", []))
            # Only admit channels that are actually configured -- routing to
            # a channel with no URL/server set would just backoff-and-give-up
            # for nothing.
            routed = [ch for ch in routed_all if ch in enabled]
            verdict, info = g.admit(event, routed)

            if verdict == "collapse":
                self.store.ledger({"outcome": "collapse", "source": source,
                                    "dedup_key": dedup_key, "title": title,
                                    "count": info})
                self._delete_file(path)
                stats["collapsed"] += 1
                return 0

            if verdict == "drop_resolve":
                self.store.ledger({"outcome": "drop_resolve", "source": source,
                                    "dedup_key": dedup_key, "title": title})
                self._delete_file(path)
                return 0

            if verdict == "hold":
                self.store.ledger({"outcome": "hold", "source": source,
                                    "title": title})
                # Gates.admit() already appended the record to
                # state["gates"]["held"]; the file is deleted by the caller
                # AFTER state has been saved this tick.
                held_paths.append(path)
                stats["held"] += 1
                return 0

            # verdict == "deliver"
            channels = list(info)
        else:
            # Reconstruct the routed channel set from the sidecar, but
            # exclude reserved bookkeeping keys (e.g. "_opened", used below
            # to guard the critical note_delivered-once rule) -- those are
            # not channel names and must never be treated as one on a retry
            # tick.
            known = self.mods["channels"].KNOWN
            channels = [ch for ch in outcomes if ch in known]

        return self._send_channels(path, event, channels, outcomes, g, state,
                                    now, stats)

    def _send_channels(self, path, event, channels, outcomes, g, state, now,
                        stats):
        critical = event.get("severity") == "critical"
        # Ask gates whether this event bypasses quiet hours -- never re-derive
        # it from severity here. This used to pass the literal `critical`
        # boolean, which silently breaks resolve pairing the moment the
        # threshold is configurable (spec §6.3): the alert pages and the
        # all-clear does not.
        bypassed_quiet = g.bypasses_quiet(event)
        source = event.get("source")
        title = event.get("title")

        first_admission = not outcomes
        if first_admission and not critical:
            # §4.5 ordering: warning/info persist dedup/cap bookkeeping
            # BEFORE the first POST (crash => suppressed duplicate, never a
            # duplicate page).
            g.note_delivered(event, channels, bypassed_quiet)
            self.store.save_state(state, now)

        wire_event = self._wire_copy(event)
        sends = 0
        # Why a channel failed, for THIS tick only. Deliberately not persisted
        # to the sidecar: it exists so the per-event summary at the end of this
        # method can name a CAUSE, and a cause carried over from an earlier
        # tick would be reported as though it were the latest one.
        channel_details = {}
        # Snapshot for the end-of-loop write: an event sitting out a backoff
        # re-assigns value-identical dicts, and rewriting a byte-identical
        # sidecar (temp file + os.replace) every tick for every stuck event
        # is pure I/O waste during exactly the outage that creates stuck
        # events.
        outcomes_before = {k: dict(v) if isinstance(v, dict) else v
                           for k, v in outcomes.items()}
        for ch in channels:
            entry = outcomes.get(ch)

            # "skipped"/"failed" are the ticker's TERMINAL outcomes and are
            # already treated as terminal by the completion check below; this
            # set was inconsistent with it, so while a sibling channel sat in
            # the retry ladder every tick re-entered the ticker branch and
            # re-wrote its ledger row. A network channel never carries either
            # as a bare string (it carries "delivered", "gave_up", or a retry
            # dict), so this is ticker-only.
            if entry in ("delivered", "gave_up", "skipped", "failed"):
                continue

            if isinstance(entry, dict):
                next_due = min(entry.get("next_due", now), now + BACKOFF_S[-1])
                if next_due > now:
                    outcomes[ch] = {"attempts": entry.get("attempts", 0),
                                     "next_due": next_due}
                    continue
                stats["retries_due"] += 1
                attempts = entry.get("attempts", 0)
            else:
                attempts = 0

            if ch == "ticker":
                # Terminal in EVERY outcome -- never enters the retry ladder.
                # A banner is a statement about NOW; redrawing one 30 minutes
                # later because a write failed would be actively wrong, and a
                # banner the operator's own thresholds rejected is not a
                # delivery failure at all.
                outcomes[ch] = self._note_ticker(wire_event, source, title)
                sends += 1
                if outcomes[ch] == "delivered" and critical \
                        and "_opened" not in outcomes:
                    # A drawn banner IS a delivery: without this, a critical
                    # whose only successful channel was the ticker recorded no
                    # open (this branch used to skip straight past the
                    # note_delivered block below), so its resolve hit
                    # drop_resolve, the banner's resolve-clear never fired,
                    # and repeats on the same key never collapsed.
                    self._open_critical(path, event, channels, outcomes, g,
                                        state, now, bypassed_quiet)
                continue

            # Computed ONCE per channel iteration, smtp only, from the RAW
            # event (never wire_event -- the attachment path is never
            # redacted, it is a caller-supplied file, not wire text). The
            # bytes returned here live ONLY in this local variable and the
            # `attachment=` kwarg below -- they must never be assigned onto
            # `event` or `wire_event`, which get serialized into
            # state.json/.outcomes/failed/the ledger.
            attachment_arg = None
            if ch == "smtp" and event.get("attachment"):
                attachment_arg = self._prepare_attachment(event, source, title)

            ok, _detail = self.mods["channels"].send(
                ch, wire_event, self.settings, self.version, opener=self.opener,
                smtp_factory=self.smtp_factory, tick_state=self._tick_state,
                attachment=attachment_arg)
            sends += 1

            if ok:
                outcomes[ch] = "delivered"
                if ch == "smtp" and attachment_arg is not None:
                    state["last_attachment_delivered_ts"] = now
                    self.mods["report_expect"].record_delivery(
                        state, source, event.get("event"), now)
                    self.store.save_state(state, now)
                if critical and "_opened" not in outcomes:
                    # §4.5 ordering: critical persists AFTER the first
                    # successful POST (crash => at most a duplicate page,
                    # never a swallowed one).
                    self._open_critical(path, event, channels, outcomes, g,
                                        state, now, bypassed_quiet)
            else:
                attempts += 1
                if attempts > len(BACKOFF_S):
                    outcomes[ch] = "gave_up"
                    self.store.ledger({"outcome": "gave_up", "source": source,
                                        "channel": ch, "title": title})
                    # Kept so the per-event summary below can report the CAUSE
                    # rather than the outcome word. `outcomes` stores only
                    # "gave_up", and the summary used to overwrite this same
                    # `last_failure` slot with those words, discarding the one
                    # string that said why. `_detail` never contains the target
                    # URL (see channels.send).
                    channel_details[ch] = f"{ch}: {_detail}"
                    note_delivery_failure(state, "gave_up",
                                          f"{ch}: {_detail}", now)
                else:
                    next_due = now + BACKOFF_S[attempts - 1]
                    outcomes[ch] = {"attempts": attempts, "next_due": next_due}
                    self.store.ledger({"outcome": "retry_scheduled",
                                        "source": source, "channel": ch,
                                        "attempt": attempts,
                                        "next_due": next_due})

        if outcomes != outcomes_before \
                and not self.mods["spool"].write_outcomes(path, outcomes):
            # A lost sidecar means this event will re-send to already-delivered
            # channels next tick and never terminate. Count it (best-effort;
            # state.json is a different file than the sidecar, so a pending/-
            # scoped failure still records) so show_status is not silent.
            state["sidecar_write_errors"] = (
                state.get("sidecar_write_errors") or 0) + 1

        # "failed" is the ticker's I/O-failure outcome and is TERMINAL like the
        # rest: a banner is a statement about NOW, so redrawing one 30 minutes
        # later would be actively wrong. Omitting it here would leave the event
        # pending forever, retried every tick -- turning a reporting fix into a
        # delivery loop.
        if all(outcomes.get(ch) in ("delivered", "gave_up", "skipped", "failed")
               for ch in channels):
            delivered = [ch for ch in channels if outcomes.get(ch) == "delivered"]
            if delivered:
                self.store.ledger({"outcome": "delivered", "source": source,
                                    "channels": delivered, "title": title,
                                    "tags": event.get("_tags") or []})
                stats["delivered"] += 1
            elif channels:
                # Every routed channel is terminal and none delivered. This
                # branch used to bump a counter, delete the file and write
                # NOTHING -- silence with no trace, found in production when an
                # event routed only to the ticker was declined by the ticker's
                # own filters. Record WHICH channels ended how, so the operator
                # can tell "your thresholds declined it" from "everything
                # failed" without reading the source.
                reasons = {ch: outcomes.get(ch) for ch in channels}
                self.store.ledger({"outcome": "not_delivered", "source": source,
                                    "title": title, "channels": list(channels),
                                    "reasons": reasons})
                if all(v == "skipped" for v in reasons.values()):
                    # Nothing FAILED -- the operator's own gates declined it.
                    # Counting this as a failure would make show_status report
                    # a correctly-configured system as broken.
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1
                    # Prefer the recorded cause over the outcome word. A
                    # channel that gave up THIS tick has a real reason in
                    # `channel_details`; one that failed on an earlier tick, or
                    # was skipped, has only its outcome, which is still better
                    # than nothing.
                    note_delivery_failure(
                        state, "not_delivered",
                        ", ".join(channel_details.get(k) or f"{k}: {v}"
                                  for k, v in reasons.items()), now)
            else:
                # Routed to zero enabled channels (e.g. a rule targets an
                # unconfigured channel with no default fallback) -- distinct
                # from the all-channels-gave-up case (already ledgered
                # per-channel above): without this the file is just deleted
                # with a bare "failed" counter bump, silent and unauditable.
                self.store.ledger({"outcome": "no_route", "source": source,
                                    "event": event.get("event"),
                                    "title": title})
                stats["failed"] += 1
                note_delivery_failure(state, "no_route",
                                      f"no enabled channel for {source}", now)
            self.mods["spool"].clear_outcomes(path)
            self._delete_file(path)

        return sends

    def _open_critical(self, path, event, channels, outcomes, g, state, now,
                       bypassed_quiet):
        """Open a critical's dedup window + `opens` entry -- sidecar FIRST.

        The window must never outlive the `_opened` sidecar mark: if the
        window is persisted while the sidecar write fails, this same event
        re-enters as a first admission next tick and `admit` collapses it
        into the window its own partial delivery opened -- the still-owed
        channels are swallowed with a `collapse` row claiming dedup worked,
        the exact "never a swallowed one" §4.5 exists to forbid. When the
        sidecar cannot be written, no window is opened: the cost is a
        duplicate page per tick until the disk fault clears (loud, counted
        by the end-of-loop write's `sidecar_write_errors`), which is the
        documented at-least-once preference over silence.

        The written snapshot seeds a fresh retry entry for every channel the
        loop has not reached yet, so a crash between this write and the
        end-of-loop write cannot silently drop the unvisited channels from
        the reconstructed channel set on the retry tick.
        """
        outcomes["_opened"] = True
        snapshot = dict(outcomes)
        for c in channels:
            snapshot.setdefault(c, {"attempts": 0, "next_due": 0.0})
        if self.mods["spool"].write_outcomes(path, snapshot):
            g.note_delivered(event, channels, bypassed_quiet)
            self.store.save_state(state, now)
        else:
            outcomes.pop("_opened", None)

    def _note_ticker(self, wire_event, source, title):
        """Draw (or decline to draw) the on-screen banner. Returns the terminal
        sidecar outcome for the "ticker" pseudo-channel: never a retry dict.

        Two gates apply and that is deliberate (operator's choice): routing
        decides the ticker is a target at all, then the Ticker's own
        `ticker_min_severity` / `ticker_sources` decide whether this particular
        event earns the screen. So adding "ticker" to `default_channels` cannot
        start painting banners over every info event.

        A gated-out result is NOT a failure -- reporting it as one would make a
        correctly-configured system look broken -- but it IS ledgered with its
        reason, because "I routed it to the ticker and saw nothing" must be
        answerable without reading the source.
        """
        if self.ticker is None:
            return "skipped"
        try:
            result = self.ticker.note(wire_event)
        except Exception as exc:
            # Never let the banner break delivery of the real channels -- but
            # an exception is a FAILURE, not a declined banner.
            return self._ticker_failed(source, title, type(exc).__name__)

        if result == "written":
            self._count_ticker("ticker_drawn")
            return "delivered"

        # An I/O failure must not be laundered into the same bucket as "your
        # own thresholds declined this". Counting a genuine failure as
        # `skipped` kept stats["failed"] at 0 while the ticker was permanently
        # broken, so show_status reported a correct system -- the exact
        # distinction the codebase already draws between gave_up and skipped.
        if result in _TICKER_FAILURES:
            return self._ticker_failed(source, title, result)

        if result in _TICKER_DECLINES:
            self._count_ticker("ticker_declined", result)
        self.store.ledger({"outcome": "ticker_skipped", "source": source,
                            "title": title, "reason": result})
        return "skipped"

    def _ticker_failed(self, source, title, reason):
        self.store.ledger({"outcome": "ticker_failed", "source": source,
                            "title": title, "reason": reason})
        return "failed"

    def _count_ticker(self, key, reason=None):
        """Bump a ticker counter in state["gates"], persisted by the tick's
        terminal save_state.

        MUST NOT raise: this runs OUTSIDE the try that wraps ticker.note(), so
        an exception here would break delivery of the event -- a reporting
        counter turning into a delivery break. Hence getattr (a future direct
        call outside a tick has no self.state) and setdefault throughout (a
        legacy state.json has "gates" but not these keys).
        """
        state = getattr(self, "state", None)
        if state is None:
            return
        g = state.setdefault("gates", {})
        if reason is None:
            g[key] = (g.get(key) or 0) + 1
        else:
            d = g.setdefault(key, {})
            d[reason] = (d.get(reason) or 0) + 1

    # -- digests -------------------------------------------------------------

    def _deliver_digest(self, digest, now, stats):
        routed = self.mods["routing"].route(
            digest,
            self.settings.get("_effective_routing_rules",
                              self.settings.get("routing_rules", [])),
            self.settings.get("default_channels", []))
        # Union with the channels the held events were ORIGINALLY bound for.
        # Routing alone is not enough: the digest now carries the right
        # severity (so a critical batch matches a critical rule), but settings
        # can change while events sit held, and `_channels` is the operator's
        # intent at the moment they were held. Dropping it would silently
        # narrow delivery -- the defect this whole change exists to fix.
        carried = digest.get("_channels") or []
        routed = list(dict.fromkeys(list(routed) + list(carried)))
        # A digest summarises faults that are already OVER. Painting one over
        # live video would put a banner on screen about something finished --
        # the same reasoning that keeps flush_held from passing a ticker.
        routed = [ch for ch in routed if ch != "ticker"]
        wire_digest = self._wire_copy(digest)
        sends = 0
        delivered = []
        for ch in routed:
            ok, _detail = self.mods["channels"].send(
                ch, wire_digest, self.settings, self.version, opener=self.opener,
                smtp_factory=self.smtp_factory, tick_state=self._tick_state)
            sends += 1
            if ok:
                delivered.append(ch)

        if delivered:
            self.store.ledger({"outcome": "delivered", "source": digest.get("source"),
                                "channels": delivered, "title": digest.get("title"),
                                "digest": True, "tags": digest.get("_tags") or []})
            stats["delivered"] += 1
        else:
            # Total failure (or zero routed channels): due_flushes() has
            # already popped these held/dedup records out of
            # state["gates"] on the caller's behalf, so losing the digest
            # here would silently drop every warning/info event it
            # summarized -- an at-least-once violation. Requeue it as a
            # durable pending event instead; the NEXT tick's pending scan
            # (this tick's list was already finalized before due_flushes()
            # ran) will parse/route/admit/send it through the normal
            # per-channel retry ladder.
            if routed:
                self.store.ledger({"outcome": "gave_up",
                                    "source": digest.get("source"),
                                    "title": digest.get("title"), "digest": True})
            self._requeue_digest(digest, now)
            stats["failed"] += 1

        return sends

    def _requeue_digest(self, digest, now):
        """Durably re-spool a digest pseudo-event that reached zero channels.

        Same filename convention as notify_client's spool writer
        (`<ts_ms>-<counter>-<source>-<rand8>.json`) and the same
        dot-temp + os.replace atomicity, written straight into
        `pending/` (not `spool/`) since it is already claimed -- the
        next tick's `spool.pending_events()` scan picks it up and
        `spool.parse_event` accepts it as-is (v/source/title/ts present).
        """
        ts_ms = int(now * 1000)
        rand8 = os.urandom(4).hex()
        name = f"{ts_ms}-0000-notifier-{rand8}.json"
        dest = os.path.join(self.store.pending_dir, name)
        tmp = os.path.join(self.store.pending_dir, f".tmp-{name}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(digest, f, separators=(",", ":"), default=str)
            os.replace(tmp, dest)
        except OSError:
            return
        self.store.ledger({"outcome": "digest_requeued",
                            "source": digest.get("source"),
                            "title": digest.get("title")})

    # -- helpers ---------------------------------------------------------

    def _prepare_attachment(self, event, source, title):
        """Read the caller's file for an smtp send. Returns the 4-tuple for
        channels.send, or None (send the email WITHOUT the file, ledgered).

        Bytes live only in the returned tuple -- never on the event dict,
        which would serialize a megabyte into state.json/.outcomes/failed/
        the ledger.
        """
        path = event.get("attachment")
        mime = self.mods["attachment"].classify_extension(path)
        base = os.path.basename(str(path))
        if mime is None:
            self.store.ledger({"outcome": "attachment_rejected",
                                "source": source, "title": title,
                                "file": base, "reason": "extension"})
            return None
        data, reason = self.mods["attachment"].read_attachment(path)
        if reason is not None:
            outcome = {"missing": "attachment_missing",
                       "not_regular": "attachment_missing",
                       "too_large": "attachment_too_large"}[reason]
            self.store.ledger({"outcome": outcome, "source": source,
                                "title": title, "file": base, "reason": reason})
            return None
        return (base, mime[0], mime[1], data)

    @staticmethod
    def _delete_file(path):
        try:
            os.remove(path)
        except OSError:
            pass
