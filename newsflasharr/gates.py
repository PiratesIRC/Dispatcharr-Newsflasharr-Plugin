"""Newsflasharr delivery gates: dedup collapse, hourly storm cap, quiet hours,
and the digest engine that surfaces held/collapsed events later.

Stdlib only, no imports of other notifier modules. Operates on a mutable
`state` dict (persisted by Store) so all gate state must round-trip through
JSON: rebuilding a Gates from the same dict must reproduce the same
decisions. deliver.py drives admit()/note_delivered()/due_flushes(); Gates
itself never performs I/O. Spec §4.5.
"""
from __future__ import annotations

import datetime
import zoneinfo

_DEFAULT_QUIET_TZ = "America/Chicago"
_HOUR_S = 3600
_MAX_DIGEST_LINES = 20
# Below every real channel limit (Discord 2000 chars, ntfy 4000 bytes) with
# room for the title and composed overhead. See _held_digest.
_MAX_DIGEST_CHARS = 1500
# held[] was an unbounded append. See _hold for why that matters more
# than it looks: dedup does not collapse held events.
_MAX_HELD = 500
# `opens` pairs an all-clear to its alert. It only ever shrank when a resolve
# arrived, so an incident that never resolves left an entry forever -- and
# since _hold now writes one, the HIGH-VOLUME path feeds it (dedup does not
# collapse held events). Bounding `held` and leaving this unbounded would just
# move the state.json bloat somewhere less visible.
_MAX_OPENS = 500
# `dedup` self-cleans at window expiry, but its instantaneous size is "distinct
# (source, dedup_key) pairs inside the current window" -- unbounded by anything.
# A wide simultaneous-channel event (sentinelarr keys per incident per channel)
# could outgrow what held/opens are ALLOWED to reach, in the same fully-
# parsed-and-rewritten-per-tick state.json. Same bound, same eviction shape.
_MAX_DEDUP = 500


# Severity ordering for dedup escalation. Duplicated from ticker.py rather
# than imported: every module here must stay standalone-loadable (conftest
# load_mod), so gates.py and ticker.py must not import each other.
_RANK = {"info": 0, "warning": 1, "critical": 2}

# Bypass THRESHOLDS are a separate vocabulary from severities, so they get their
# own table. `none` maps ABOVE the highest real severity so that nothing -- not
# even a critical -- clears it.
#
# Reusing _RANK here is the trap: it has no "none" entry and _rank falls through
# to 0, so `none` would rank BELOW everything and every severity would bypass --
# the exact inverse of "hold everything overnight". Two tables, deliberately.
_BYPASS_RANK = {"critical": 2, "warning": 1, "none": 3}
_DEFAULT_BYPASS = "critical"


def _rank(severity):
    return _RANK.get(str(severity or "").lower(), 0)


def _bypasses(severity, threshold):
    """Does `severity` clear the configured bypass `threshold`?

    Blank/unknown thresholds degrade to `critical`, i.e. today's behaviour --
    an operator typo must not silently widen or close a gate.
    """
    key = str(threshold or "").strip().lower()
    return _rank(severity) >= _BYPASS_RANK.get(key, _BYPASS_RANK[_DEFAULT_BYPASS])


def _key(source, dedup_key):
    return f"{source}|{dedup_key}"


def _num(settings, key, default):
    """A numeric setting, tolerant of the present-but-None case.

    `settings.get(key, default)` does NOT apply its default when the key is
    PRESENT with a null value -- which is exactly what Dispatcharr stores for a
    field the operator cleared. `count >= None` then raises TypeError on EVERY
    admit, i.e. all delivery stops, from one blank box. The same trap is called
    out by name in collector.py and ticker.py; this is the last unguarded copy.
    """
    raw = settings.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


class Gates:
    def __init__(self, state, settings, now_fn):
        self.state = state
        self.settings = settings
        self.now_fn = now_fn

    # -- state access --------------------------------------------------

    def _g(self):
        g = self.state.setdefault("gates", {})
        g.setdefault("dedup", {})
        g.setdefault("cap", {"hour_start": self.now_fn(), "count": 0})
        g.setdefault("held", [])
        g.setdefault("opens", {})
        g.setdefault("held_overflow", 0)
        g.setdefault("opens_overflow", 0)
        g.setdefault("dedup_overflow", 0)

        # Spec: a corrupt state.json "sidelines the file and starts
        # conservative (assume cap consumed) -- never 'treat as empty and
        # re-page the storm'". `store.load_state` has always SET this flag and
        # nothing ever consumed it, so recovery started PERMISSIVE: the counter
        # reset to 0 and a storm got a fresh full budget on the one tick least
        # able to afford it.
        #
        # Held events are not lost by holding (they digest later) and criticals
        # bypass the cap, so the conservative direction costs delay, not
        # alerts. The flag is consumed here so it cannot re-fire every tick,
        # and the hour rolls normally afterwards.
        if self.state.pop("_corrupt_recovered", None):
            g["cap"]["count"] = _num(self.settings, "max_sends_per_hour", 20)
            g["cap"]["hour_start"] = self.now_fn()
            g["cap"]["assumed_consumed"] = True
        return g

    @staticmethod
    def _hour_elapsed(hour_start, now):
        """Has the cap's hour ended?

        A bare `now - hour_start >= _HOUR_S` goes NEGATIVE after a backwards
        clock jump (an NTP correction, a Docker Desktop suspend/resume), and
        `hour_start` persists in state.json across restarts -- so the cap stayed
        saturated until real time caught up: every non-bypassing event held,
        and cap-held events never released. A window that is negative or longer
        than an hour is nonsense either way, so roll and re-anchor. Self-healing
        rather than another counter to read.
        """
        elapsed = now - hour_start
        return not (0 <= elapsed < _HOUR_S)

    def _roll_cap(self, cap, now):
        if self._hour_elapsed(cap.get("hour_start", now), now):
            cap["hour_start"] = now
            cap["count"] = 0

    # -- admission -------------------------------------------------------

    def admit(self, event, channels):
        g = self._g()
        now = self.now_fn()
        source = event.get("source")
        dedup_key = event.get("dedup_key")
        key = _key(source, dedup_key) if dedup_key else None
        kind = event.get("kind", "event")

        if kind == "resolve":
            return self._admit_resolve(g, now, key, event)

        # Dedup collapse applies to every kind except resolve (handled above
        # via _admit_resolve) — including critical events; critical only
        # bypasses quiet hours and the hourly cap below, never dedup.
        #
        # EXCEPT on ESCALATION. The window records the severity that opened it,
        # and an arriving event that OUTRANKS it breaks the window instead of
        # collapsing into it. Without this, a warning's window swallowed a
        # critical on the same dedup_key: the critical was ledgered as
        # `collapse` with no severity recorded and nothing paged, so it was
        # indistinguishable from a collapsed info. Live in this installation —
        # sentinelarr re-evaluates black_screen severity per emit from the
        # confirming sample's `clients` count while keying on
        # (type, channel_id) alone, so the same incident legitimately arrives
        # as warning then critical.
        #
        # Deliberately asymmetric: only an INCREASE breaks the window. A lower
        # or equal severity inside a window is a genuine duplicate and still
        # collapses.
        #
        # An attachment event never collapses, matching bypasses_quiet/
        # bypasses_cap above: a held report loses its file, and so would a
        # collapsed one (note_delivered/the ledger only ever see the
        # surviving event; the file on the collapsed one is gone). Report
        # callers today pass no dedup_key (spec §7), so this is latent, but
        # a future caller that does must not have its report silently eaten.
        if key is not None and not event.get("attachment"):
            window = g["dedup"].get(key)
            if window is not None and now < window["window_end"]:
                if _rank(event.get("severity")) <= _rank(window.get("severity")):
                    window["count"] += 1
                    return "collapse", window["count"]

        # Quiet-hold is checked before cap-hold: when both apply, the event is
        # held for "quiet", not "cap". The two are gated SEPARATELY by their own
        # thresholds -- they are independent decisions, not one value used twice.
        if not self.bypasses_quiet(event) and self.in_quiet(now):
            self._hold(g, event, channels, "quiet", now)
            return "hold", None

        # A bypassing severity does NOT increment the counter, so a critical
        # storm cannot consume the budget that non-critical alerts share.
        if not self.bypasses_cap(event):
            self._roll_cap(g["cap"], now)
            if g["cap"]["count"] >= _num(self.settings, "max_sends_per_hour", 20):
                self._hold(g, event, channels, "cap", now)
                return "hold", None
            g["cap"]["count"] += 1

        return "deliver", list(channels)

    def bypasses_quiet(self, event):
        """Does this event ignore quiet hours? THE single source of truth.

        deliver.py used to answer this itself, passing a literal
        `severity == "critical"` as note_delivered's `bypassed_quiet`. That is
        a hardcoded restatement of a decision that is not its to make: the
        moment the threshold becomes configurable (spec §4, `quiet_hours_bypass`)
        a warning that legitimately bypasses quiet would record bypassed=False,
        so its RESOLVE would be held instead of following its open -- the alert
        pages, the all-clear does not, and the ticker's resolve-clear never
        fires, leaving a banner up with the fault already over.

        Keeping the answer in one method means that threshold lands in exactly
        one place and no caller can drift from it.
        """
        # an attachment is never held -- a held report becomes a digest line and loses its file.
        if event.get("attachment"):
            return True
        return _bypasses(event.get("severity"),
                         self.settings.get("quiet_hours_bypass"))

    def _window_min_for(self, severity):
        """Dedup window length in minutes for `severity`.

        `dedup_window_critical_min` is blank-by-default and blank means "use the
        global". **0 is a REAL value** meaning criticals never collapse --
        `dedup_window_min` itself has min=0 with that same meaning -- so the
        blank test is an explicit `in (None, "")`, never a truthiness check and
        never `settings.get(k, DEFAULT)` (which does not apply the default when
        the key is present-but-None: the trap that already bit
        ticker_min_severity).
        """
        default = _num(self.settings, "dedup_window_min", 10)
        if severity != "critical":
            return default
        raw = self.settings.get("dedup_window_critical_min")
        if raw in (None, ""):
            return default
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return default

    def bypasses_cap(self, event):
        """Does this event ignore the hourly cap? Independent of quiet hours.

        Rev 1 of the spec called these "the same threshold substitution". They
        are not: a severity can legitimately bypass one gate and not the other,
        which is why admit()'s single `if not critical:` had to split into two
        separately-gated blocks rather than have one value swapped.
        """
        # an attachment is never held -- a held report becomes a digest line and loses its file.
        if event.get("attachment"):
            return True
        return _bypasses(event.get("severity"),
                         self.settings.get("hourly_cap_bypass"))

    def _admit_resolve(self, g, now, key, event):
        open_ = g["opens"].get(key) if key is not None else None
        if open_ is None:
            return "drop_resolve", None

        channels = list(open_.get("channels", []))
        bypassed = bool(open_.get("bypassed"))
        # Consume the open now that it has matched — a later resolve for the
        # same key has nothing left to pair against and must drop_resolve,
        # regardless of whether THIS resolve ends up delivered or held.
        g["opens"].pop(key, None)
        if not self.bypasses_quiet(event) and not bypassed and self.in_quiet(now):
            self._hold(g, event, channels, "quiet", now)
            return "hold", None

        return "deliver", channels

    def _hold(self, g, event, channels, reason, now):
        """Append to the held list, BOUNDED.

        This was an unbounded append, and the exposure is larger than it
        looks: dedup does NOT collapse held events (`note_delivered` runs only
        on the deliver path), so once the hourly cap is reached -- or at any
        moment during quiet hours -- every event is held individually. One
        person channel-surfing produces roughly 4,800 held records an hour,
        and `state.json` is fully parsed and rewritten two to four times every
        tick, so that becomes ~1.7 MB of JSON rewritten at ~1.4 MB/s,
        indefinitely, for notifications nobody will read: the digest only ever
        shows `_MAX_DIGEST_LINES` of them.

        OLDEST is dropped, not newest -- the recent events are the ones still
        worth telling someone about. Drops are counted rather than silent, and
        `show_status` surfaces the counter.
        """
        held = g["held"]
        record = {
            "event": dict(event),
            "channels": list(channels),
            "reason": reason,
            "held_at": now,
        }
        if reason == "cap":
            # Anchor the release check to the window the event was held IN.
            # The LIVE cap window is mutable: admit() re-anchors it to `now`
            # at every hour boundary, and deliver.py admits new events BEFORE
            # due_flushes runs -- so a release check against the live window
            # is starved indefinitely by exactly the sustained traffic that
            # creates cap-holds. See _held_released.
            record["cap_hour_start"] = g["cap"].get("hour_start", now)
        held.append(record)
        self._open_for_held(g, event, channels, now)

        overflow = len(held) - _MAX_HELD
        if overflow <= 0:
            return

        # Evict the oldest NON-critical records first. Plain oldest-first
        # eviction let noise push out an alert: dedup does not collapse held
        # events, so one person channel-surfing produces ~4,800 held records an
        # hour, and a critical held at 23:00 was gone by 23:07 -- counted only
        # in held_overflow, which cannot say WHAT was dropped.
        victims = []
        for i, rec in enumerate(held):
            if len(victims) >= overflow:
                break
            if rec["event"].get("severity") != "critical":
                victims.append(i)
        if len(victims) < overflow:
            # Nothing but criticals left to drop. The cap is a hard bound on
            # state.json size, so the oldest critical goes: bounded beats
            # complete, and show_status surfaces the count.
            taken = set(victims)
            spare = [i for i in range(len(held)) if i not in taken]
            victims.extend(spare[:overflow - len(victims)])
        for i in sorted(victims, reverse=True):
            del held[i]
        g["held_overflow"] = g.get("held_overflow", 0) + overflow

    def _open_for_held(self, g, event, channels, now):
        """Record the open for a HELD event.

        `_hold` never wrote g["opens"] -- only note_delivered did, on the
        DELIVER path. So a held event got no open, and when the incident
        recovered `_admit_resolve` found nothing to pair against and returned
        `drop_resolve` with NO ledger row: the operator is told the channel
        died and never told it recovered. Every night of quiet hours does this
        to warnings today, and it is what makes a `none` bypass unsafe to ship.

        A resolve CLOSES an incident and must never create an open -- the same
        phantom-open family note_delivered's own kind guard exists to close.
        """
        if str(event.get("kind") or "") == "resolve":
            return
        dedup_key = event.get("dedup_key")
        if not dedup_key:
            return
        self._record_open(
            g, _key(event.get("source"), dedup_key), channels,
            # It was held, so it did not bypass quiet -- unless it was held for
            # the CAP, which is a separate threshold. Ask the one decider.
            self.bypasses_quiet(event), event.get("severity"), now)

    # -- post-delivery bookkeeping ---------------------------------------

    def note_delivered(self, event, channels, bypassed_quiet):
        # A RESOLVE consumes an open; it must never create one. deliver.py
        # calls this on the deliver path with no `kind` check, so without this
        # guard an all-clear did two wrong things: it pushed `window_end`
        # forward (a flapping channel extended its own collapse window with
        # every recovery, making the next failure more likely to be swallowed),
        # and it recreated the very `opens` entry `_admit_resolve` had just
        # popped -- so a SECOND resolve paired against the phantom and
        # delivered a duplicate all-clear instead of dropping.
        if str(event.get("kind") or "") == "resolve":
            return

        dedup_key = event.get("dedup_key")
        if not dedup_key:
            return
        g = self._g()
        now = self.now_fn()
        key = _key(event.get("source"), dedup_key)
        window_min = self._window_min_for(event.get("severity"))
        # An escalation replaces a window that may already have collapsed
        # events into it. `due_flushes` only emits the "N more" digest for an
        # entry it finds AT expiry, so a wholesale replace would discard that
        # count silently -- reintroducing, in the fix itself, the same
        # silent-loss family the escalation change exists to close. Carry the
        # count and the original first_ts forward so the summary still lands.
        prior = g["dedup"].get(key) or {}
        g["dedup"][key] = {
            "first_ts": prior.get("first_ts", now),
            "count": prior.get("count", 0),
            "window_end": now + window_min * 60,
            "severity": event.get("severity"),
            # Stored so due_flushes reports THIS window's length rather than
            # re-reading the global -- otherwise a critical window summarised
            # its repeats with the wrong number of minutes.
            "window_min": window_min,
        }
        overflow = len(g["dedup"]) - _MAX_DEDUP
        if overflow > 0:
            # Soonest-expiring first (it was about to self-clean anyway),
            # criticals last -- the held/opens eviction shape. COUNTED: an
            # evicted window silently drops its pending "N more" digest count,
            # and show_status surfaces the counter.
            by_end = sorted(g["dedup"].items(),
                            key=lambda kv: kv[1].get("window_end", 0))
            victims = [k for k, v in by_end if v.get("severity") != "critical"]
            if len(victims) < overflow:
                taken = set(victims)
                victims += [k for k, _ in by_end if k not in taken]
            for k in victims[:overflow]:
                g["dedup"].pop(k, None)
            g["dedup_overflow"] = g.get("dedup_overflow", 0) + overflow
        self._record_open(g, key, channels, bypassed_quiet,
                          event.get("severity"), now)

    def _record_open(self, g, key, channels, bypassed_quiet, severity, now):
        """Write an open and keep the map BOUNDED.

        Eviction spares criticals and then takes the STALEST by `ts`, not by
        insertion order: re-assigning an existing dict key does not move it to
        the end, so insertion order records first-seen, not recency, and would
        evict the entry a resolve is most likely to still want.
        """
        g["opens"][key] = {
            "channels": list(channels),
            "bypassed": bool(bypassed_quiet),
            "severity": severity,
            "ts": now,
        }

        overflow = len(g["opens"]) - _MAX_OPENS
        if overflow <= 0:
            return
        by_age = sorted(g["opens"].items(), key=lambda kv: kv[1].get("ts", 0))
        victims = [k for k, v in by_age if v.get("severity") != "critical"]
        if len(victims) < overflow:
            taken = set(victims)
            victims += [k for k, _ in by_age if k not in taken]
        # COUNTED, like _hold's held_overflow. An evicted open is not a
        # bookkeeping detail: it is the entry a resolve pairs against, so each
        # silent pop turns a future all-clear into `drop_resolve` and leaves
        # the ticker's resolve-clear unfired. `_open_for_held` now feeds this
        # from the high-volume held path, so eviction is reachable in ordinary
        # operation rather than theoretical.
        for k in victims[:overflow]:
            g["opens"].pop(k, None)
        g["opens_overflow"] = g.get("opens_overflow", 0) + overflow

    # -- digests -----------------------------------------------------------

    def due_flushes(self):
        g = self._g()
        now = self.now_fn()
        flushes = []

        for k in list(g["dedup"].keys()):
            entry = g["dedup"][k]
            if entry["window_end"] > now:
                continue
            del g["dedup"][k]
            if entry["count"] > 0:
                source, _, dedup_key = k.partition("|")
                # Legacy entries predate `window_min` and fall back to the
                # global, exactly as the `severity` fallback below does.
                entry_min = entry.get("window_min")
                if entry_min is None:
                    entry_min = _num(self.settings, "dedup_window_min", 10)
                title = (f"{source} {dedup_key}: {entry['count']} more in "
                         f"the last {entry_min} min")
                # Same severity-laundering fix as the held digest: repeats of
                # a CRITICAL were summarised as an info digest and routed as
                # one. The window has recorded its own severity since the
                # escalation fix; legacy entries have none and read as info.
                flushes.append(self._digest(
                    title, "", now, severity=entry.get("severity") or "info"))

        # Computed ONCE for the whole pass: in_quiet re-parses quiet_hours and
        # re-resolves zoneinfo per call, and this loop runs per held record --
        # up to _MAX_HELD identical evaluations per tick with the same `now`.
        quiet_now = self.in_quiet(now)
        released, remaining = [], []
        for held in g["held"]:
            if self._held_released(held, now, quiet_now):
                released.append(held)
            else:
                remaining.append(held)
        g["held"] = remaining

        if released:
            flushes.append(self._held_digest(released, now))

        return flushes

    def _held_released(self, held, now, quiet_now):
        reason = held.get("reason")
        if reason == "quiet":
            return not quiet_now
        if reason == "cap":
            # Keyed to the record's OWN anchor (`cap_hour_start`, captured by
            # _hold), never the live cap window: `_roll_cap` resets that to
            # `now` at every hour boundary, and under sustained traffic an
            # admit precedes every due_flushes -- checking the live window
            # therefore never saw a full hour elapse and cap-held events were
            # starved until eviction. Quiet-holds were always immune because
            # they key on absolute wall-clock; this gives cap-holds the same
            # property. A legacy record (pre-anchor state.json) falls back to
            # held_at, and a record with neither releases immediately -- the
            # same fail-toward-delivering direction as _hour_elapsed's
            # backwards-clock guard.
            anchor = held.get("cap_hour_start", held.get("held_at", 0))
            return self._hour_elapsed(anchor, now)
        return True

    def _held_digest(self, released, now):
        """Bounded by CHARACTERS as well as lines.

        This is the only body Newsflasharr generates itself, and the only 20x
        amplifier in the system -- every other body comes from a caller and
        measures ~52 chars median. A line cap alone bounds nothing, because the
        unbounded dimension is per-line title length (callers may send up to
        1024). Bounding here makes the digest structurally incapable of
        overflowing any channel, rather than relying on each channel to cope.

        Separators are ASCII: this previously emitted two em dashes per line,
        i.e. 120 wasted bytes in a 20-line digest on the byte-limited channel,
        against the workspace's plain-ASCII rule.
        """
        lines, used, shown = [], 0, 0
        for held in released[:_MAX_DIGEST_LINES]:
            ev = held.get("event", {})
            line = (f"{ev.get('severity', '?')} - {ev.get('source', '?')} "
                    f"- {ev.get('title', '')}")
            if used + len(line) + 1 > _MAX_DIGEST_CHARS and lines:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        remaining = len(released) - shown
        if remaining > 0:
            lines.append(f"...and {remaining} more")
        title = f"{len(released)} held notification(s)"

        # The digest is only as safe to downgrade as its WORST member. Taking
        # the max means a batch containing one critical routes as a critical,
        # which is the whole point -- see _digest.
        severity = "info"
        channels = []
        for held in released:
            ev = held.get("event") or {}
            if _rank(ev.get("severity")) > _rank(severity):
                severity = str(ev.get("severity") or "info").lower()
            channels.extend(held.get("channels") or [])

        return self._digest(title, "\n".join(lines), now,
                            severity=severity, channels=channels)

    def _digest(self, title, body, now, severity="info", channels=None):
        """A digest inherits the SEVERITY of what it summarises.

        This used to be hardcoded `info`, which laundered severity: a critical
        held overnight was released as an info digest, so an operator whose
        only routing rule is `critical -> smtp` never got it by email at all --
        and the ledger recorded `delivered`, because the digest genuinely was
        delivered, just nowhere useful.

        `_channels` carries the channels the held events were originally bound
        for. `_hold` has always stored them and nothing ever read them back;
        they are the operator's intent at the time the events were held, and
        they must survive a routing change made while the events sat waiting.
        """
        digest = {
            "v": 1,
            "source": "newsflasharr",
            "event": "digest",
            "severity": severity if severity in _RANK else "info",
            "kind": "event",
            "title": title,
            "body": body,
            "ts": int(now * 1000),
            "_tags": ["digest"],
        }
        if channels:
            digest["_channels"] = sorted({c for c in channels if c})
        return digest

    # -- quiet hours ---------------------------------------------------

    def in_quiet(self, now=None):
        quiet_hours = self.settings.get("quiet_hours") or ""
        if not quiet_hours:
            return False
        parsed = self._parse_quiet_hours(quiet_hours)
        if parsed is None:
            return False
        start_min, end_min = parsed

        if now is None:
            now = self.now_fn()
        # `_resolved_quiet_tz` is Dispatcharr's own system timezone, derived
        # and zoneinfo-validated by dispatcharr.read_timezone(). It wins over
        # any `quiet_tz` left in the stored settings row by a pre-derivation
        # version -- Dispatcharr never prunes removed fields.
        tz_name = (self.settings.get("_resolved_quiet_tz")
                   or self.settings.get("quiet_tz")
                   or _DEFAULT_QUIET_TZ)
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
            dt = datetime.datetime.fromtimestamp(now, tz)
        except Exception:
            # A typo'd/unknown quiet_tz must fail toward delivering, never
            # wedge a tick: ZoneInfoNotFoundError propagating out of here
            # would abort the whole Deliverer.tick pass (heartbeat goes
            # stale with an opaque cause). validate_settings flags this up
            # front; this is the runtime backstop.
            return False
        cur_min = dt.hour * 60 + dt.minute

        if start_min <= end_min:
            return start_min <= cur_min < end_min
        return cur_min >= start_min or cur_min < end_min

    @staticmethod
    def _parse_quiet_hours(quiet_hours):
        try:
            start_str, end_str = quiet_hours.split("-", 1)
            sh, sm = (int(p) for p in start_str.strip().split(":", 1))
            eh, em = (int(p) for p in end_str.strip().split(":", 1))
            # Range-validated: "25:00-26:00" used to parse cleanly into minute
            # values no clock can reach, so in_quiet returned False forever and
            # quiet hours were silently OFF while validate echoed the string
            # back as confirmation. Returning None makes it indistinguishable
            # from any other unreadable value -- which is what validate reports.
            if not (0 <= sh < 24 and 0 <= sm < 60
                    and 0 <= eh < 24 and 0 <= em < 60):
                return None
            return sh * 60 + sm, eh * 60 + em
        except (ValueError, AttributeError):
            return None
