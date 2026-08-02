"""Newsflasharr collector: flock leadership, fail-closed settings, per-tick loop.

Runs one daemon thread per uWSGI worker (spawned by `ensure_collector`, called
from `Plugin.__init__` on every request -- must stay cheap and
idempotent). Only the worker holding the `fcntl.flock` on `leader.lock`
ever claims/delivers; all others sit idle, retrying the flock each tick.

All mutable runtime state (thread handle, stop Event, arm signature) parks in
a synthetic `sys.modules` entry (`RUNTIME_KEY`) instead of module globals --
Dispatcharr's plugin-reload ping-pong wipes module globals on nearly
every request, which would otherwise orphan the collector thread and
double-spawn a new one per reload.

Stdlib only. Django (`apps.plugins.models.PluginConfig`) is imported lazily,
INSIDE `read_settings()`, and only there -- this module must stay importable
standalone (tests load it via `conftest.load_mod`, with no Django present).

`Collector` never imports its sibling notifier modules (store/spool/routing/
gates/channels/deliver) directly -- like `Deliverer` (Task 8), it receives
them via an injected `mods` dict, so this file stays standalone-loadable.
`ensure_collector` (the real entry point, wired by `plugin.py` in Task 10)
resolves those siblings itself via `_load_sibling_mods`, which needs a real
package context (relative import) and is not available when this module is
loaded standalone by tests -- tests build `Collector` directly with their own
injected `mods`, never going through `ensure_collector` for that part.

Spec §4.1, §4.3, §4.8.
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import sys
import threading
import time
import types

RUNTIME_KEY = "_newsflasharr_runtime"

DEFAULTS = {
    "spool_poll_s": 5,
    "dedup_window_min": 10,
    # Blank means "use dedup_window_min"; 0 means criticals never collapse.
    "dedup_window_critical_min": "",
    "max_sends_per_hour": 20,
    "hourly_cap_bypass": "critical",
    "quiet_hours": "",
    "quiet_hours_bypass": "critical",
    "max_event_age_h": 24,
    # Blank means "do not monitor" and is NOT the same as 0 -- same shape as
    # dedup_window_critical_min above. Unseeded keys return None from a
    # pre-existing stored row, so this MUST be in DEFAULTS.
    "report_expect_days": "",
    "discord_webhook_url": "",
    "webhook_url": "",
    "ntfy_server": "",
    "ntfy_topic": "",
    "ntfy_token": "",
    "apprise_url": "",
    # SMTP. These MUST be here: both settings entry points seed from DEFAULTS,
    # so without them settings.get("smtp_security") is None on any path whose
    # stored row predates the fields.
    "smtp_server": "",
    "smtp_security": "auto",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_to": "",
    "default_channels": "",
    "routing_rules": "[]",
    "critical_extra_channels": "none",
    "redact_hostnames": "",
    "dispatcharr_events": "",
    "ticker_enabled": False,
    "ticker_min_severity": "critical",
    "ticker_ttl_s": 20,
    "ticker_sources": "sentinelarr",
    "ticker_prefix": "",
    # Appearance only -- these never reach the collector's own behaviour; they
    # are rendered into the filter string the operator pastes into their Output
    # Profile (the plugin edits no Dispatcharr rows). Seeded here so a
    # pre-existing stored settings row does not return None for them.
    "ticker_font_size": "medium",
    "ticker_colors": "white_on_black",
    "ticker_bg_opacity": 0.6,
    # A literal, not ticker.DEFAULT_POSITION: this module must stay
    # standalone-loadable and does not import ticker at module level. A contract
    # test cross-checks the two so they cannot drift.
    "ticker_position": 8,
}

_PRUNE_INTERVAL_S = 3600.0


def _parse_hostnames(raw):
    """`redact_hostnames` setting -> a clean list of non-empty hostname
    strings. `read_settings()`/`plugin._coerce_settings` both pass this
    setting through as the raw comma-separated string (same treatment as
    every other plain-string field) -- parsed here, once, at the one call
    site (`_tick`) that needs a list. Tolerates an already-parsed list too,
    so a future settings-layer change to pre-parse it stays compatible."""
    if isinstance(raw, (list, tuple)):
        return [h.strip() for h in raw if isinstance(h, str) and h.strip()]
    if not raw:
        return []
    return [h.strip() for h in str(raw).split(",") if h.strip()]


def parse_routing_rules(raw):
    """`routing_rules` (JSON string in the stored row) -> (rules, error).

    ONE algorithm for both settings entry points -- `read_settings` here and
    `plugin._coerce_settings` -- previously hand-duplicated, which is the
    exact drift shape that caused the 'Ntfy' outage in the sibling field.
    The value ALWAYS degrades to a usable list (a bad value must never stop
    the collector delivering); `error` is None, "not valid JSON" or "not a
    list of rules" so the plugin's reporters can raise what happened."""
    error = None
    rules = raw if raw is not None else "[]"
    if isinstance(rules, str):
        try:
            rules = json.loads(rules) if rules.strip() else []
        except (ValueError, TypeError):
            error = "not valid JSON"
            rules = []
    if not isinstance(rules, list):
        error = error or "not a list of rules"
        rules = []
    return rules, error


def parse_default_channels(raw):
    """`default_channels` (CSV string) -> list of stripped, non-empty names.
    Tolerates an already-parsed list; anything else degrades to []. Shared
    for the same one-algorithm reason as `parse_routing_rules`."""
    if isinstance(raw, str):
        return [c.strip() for c in raw.split(",") if c.strip()]
    return list(raw) if isinstance(raw, (list, tuple)) else []


def heartbeat_threshold(settings):
    """Staleness threshold for `last_tick`: two poll intervals + 30s grace.

    Routed through the SAME `_coerce_poll_s` the collector's run loop uses,
    so the DISPLAYED threshold can never disagree with the real cadence: a
    junk/negative `spool_poll_s` (reachable via a direct DB write -- the
    UI's `min: 1` is browser-side only) previously made the two plugin.py
    report sites compute a threshold from the raw bad value while the
    collector itself polled at the clamped default."""
    return 2 * _coerce_poll_s(settings.get("spool_poll_s")) + 30.0


def effective_hostnames(settings):
    """The UNION of the hostnames derived from Dispatcharr's EPG sources and
    M3U accounts (`_resolved_redact_hosts`, injected by `read_settings` /
    `plugin._coerce_settings`) with whatever the operator typed into
    `redact_hostnames`.

    Additive on purpose. Derivation covers only what Dispatcharr knows about;
    it cannot know a webhook target, a LAN IP like the dustarr report host,
    a reverse proxy, or a hostname some caller plugin puts in a body -- and a
    leak discovered at 2am must be fixable by typing into a field, not by a
    code change and a redeploy."""
    manual = _parse_hostnames(settings.get("redact_hostnames", ""))
    derived = settings.get("_resolved_redact_hosts") or []
    if not isinstance(derived, (list, tuple)):
        derived = []
    return sorted({h for h in list(manual) + list(derived) if h})


def build_wire_redact(channels_mod, settings, redact):
    """Build the wire-side redact callable (spec §8.1): base credential
    `redact` composed with `channels_mod.redact_hosts()` over the effective
    hostname list. Shared by `Collector._tick` (the unattended production
    path) and `plugin.py`'s `flush_held` admin action so a manual flush gets
    the identical hostname backstop as every other send -- previously
    `flush_held` built its `Deliverer` with no `wire_redact` at all, so a
    manual Flush sent held-event bodies without the hostname scrub."""
    hostnames = effective_hostnames(settings)

    def wire_redact(text):
        return channels_mod.redact_hosts(redact(text), hostnames)

    return wire_redact


def _ticker_mod():
    """The sibling `ticker` module, or None when standalone-loaded."""
    try:
        from . import ticker
        return ticker
    except Exception:
        return None


_SETTINGS_ERROR_ATTR = "notifyarr_settings_error"
_LOCK_ERROR_ATTR = "_newsflasharr_lock_error"


def _note_settings_error(exc):
    """Record WHY read_settings() failed, so a recurrence is diagnosable.

    The bare `except Exception: return None` below is correct policy -- an
    unreadable settings row must fail closed -- but it used to discard the
    cause, which made the one production occurrence unknowable by
    construction: no log line, no state, nothing to look at afterwards.

    Records the exception TYPE NAME ONLY, never str(exc). `state.json` is
    surfaced by show_status, and docs/API.md warns that status dumps get pasted
    into GitHub issues; a database error string routinely carries a host, a
    username, or a whole connection string. Same rule as the smtp channel's
    `detail`.

    Parked on the synthetic runtime module rather than a module global, so it
    survives Dispatcharr's plugin-reload ping-pong -- otherwise a
    reload between the failure and the next tick would erase the evidence.
    """
    mod = _get_runtime()
    prev = getattr(mod, _SETTINGS_ERROR_ATTR, None)
    count = (prev or {}).get("count", 0) + 1
    setattr(mod, _SETTINGS_ERROR_ATTR, {
        "type": type(exc).__name__,
        "at": time.time(),
        "count": count,
    })


def _coerce_poll_s(raw):
    """A usable poll interval, whatever the settings row holds.

    `settings.get(k, DEFAULT)` does NOT apply the default when the key is
    PRESENT with None -- the documented trap that already bit
    `ticker_min_severity` -- and `stop_event.wait(None)` blocks until a plugin
    reload, so the collector simply stops with no exception, no fail-closed
    flag and no trace. A non-numeric string was worse: the TypeError raised
    OUTSIDE the inner try, so the thread exited via `finally` and died silently.

    0 would busy-spin the tick loop and negative is meaningless, so both fall
    back too. plugin.py already float()-guards this value in two places -- the
    reporting side assumed it could be junk while the side that USES it did not.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULTS["spool_poll_s"])
    if value <= 0:
        return float(DEFAULTS["spool_poll_s"])
    return value


def _fcntl_mod():
    """`fcntl` or None (Windows dev, most local test runs).

    A function rather than a bare import inside `try_acquire` so the whole
    acquisition path -- including the fault/contention split -- is reachable in
    tests on a platform without fcntl. Gating those tests on the real module
    left `try_acquire` effectively untested off Linux, which mutation testing
    caught: three mutations of it survived a full green suite.
    """
    try:
        import fcntl
        return fcntl
    except ImportError:
        return None


def _note_lock_error(exc):
    """Record WHY leadership could not be acquired -- but ONLY for real faults.

    `try_acquire` returned False for two completely different conditions:
    "another worker holds the lock", which is the NORMAL answer for N-1 workers
    on every tick, and "I could not even open the file". Collapsing them made
    the E3 root-ownership trap invisible: `/data/newsflasharr` owned `root:root`
    makes `os.open` raise PermissionError in EVERY worker, so nobody is leader,
    nothing ticks, and no `state.json` is ever written -- and `show_status`
    reports "heartbeat: never seen (no state.json on disk yet)", which is
    exactly what a healthy first boot looks like. That has already cost a
    deploy in this installation.

    Contention must therefore stay SILENT: recording it would fire every tick
    on every non-leader worker and bury the signal this exists to raise.

    TYPE NAME ONLY, never str(exc) -- an OSError string carries a filesystem
    path, and `show_status` output gets pasted into issues. Same rule as
    `_note_settings_error` and the smtp `detail`.

    Parked on the synthetic runtime module so a plugin reload between the
    failure and the operator looking cannot erase it.
    """
    mod = _get_runtime()
    prev = getattr(mod, _LOCK_ERROR_ATTR, None)
    count = (prev or {}).get("count", 0) + 1
    setattr(mod, _LOCK_ERROR_ATTR, {
        "type": type(exc).__name__,
        "at": time.time(),
        "count": count,
    })


def last_lock_error():
    return getattr(_get_runtime(), _LOCK_ERROR_ATTR, None)


def clear_lock_error():
    setattr(_get_runtime(), _LOCK_ERROR_ATTR, None)


def last_settings_error():
    return getattr(_get_runtime(), _SETTINGS_ERROR_ATTR, None)


def clear_last_settings_error():
    setattr(_get_runtime(), _SETTINGS_ERROR_ATTR, None)


def _dispatcharr_mod():
    """The sibling `dispatcharr` module, or None when standalone-loaded.

    `collector.py` must stay importable with no package context
    (conftest.load_mod), so this relative import is lazy and its failure is a
    normal state, not an error: without it `read_settings` simply performs no
    derivation and the `_resolved_*` keys stay absent, which every consumer
    already treats as "fall back"."""
    try:
        from . import dispatcharr
        return dispatcharr
    except Exception:
        return None


def _routing_mod():
    """Lazy sibling import, same contract as `_dispatcharr_mod` above: this
    module must stay importable with no package context (conftest.load_mod),
    and a failure here is a normal state, not an error -- without it settings
    are simply not case-normalized, which is the behaviour that shipped before
    the normalization existed."""
    try:
        from . import routing
        return routing
    except Exception:
        return None


def read_settings():
    """`PluginConfig.settings` merged over `DEFAULTS`; `None` on ANY
    exception reading/resolving the DB row itself -- fail closed (Django not
    ready, row missing, ...).

    `routing_rules` (JSON string) and `default_channels` (CSV string) are the
    two settings stored as plain strings in the DB row (plugin.json text
    fields) but consumed as Python lists by `routing.route` / `Deliverer`
    (see deliver.py, which passes `settings["routing_rules"]` /
    `settings["default_channels"]` straight through with no parsing of its
    own). Parsing them HERE, in the one place that touches the DB, means
    every other module -- and every test -- only ever sees already-parsed
    lists.

    A malformed `routing_rules` JSON string is deliberately NOT treated as a
    fail-closed condition: it is one optional field's syntax error, not
    evidence the DB row itself is unreadable. It is parsed in its own narrow
    `try/except` and degrades to `[]` (empty rules), which is the same safe
    baseline as "no rules configured" -- delivery continues, routed to
    `default_channels`. Conflating that with the DB-read failure below would
    zero out ALL delivery, including criticals, over a typo.
    """
    try:
        from apps.plugins.models import PluginConfig

        stored = PluginConfig.objects.get(key="newsflasharr").settings
        settings = dict(DEFAULTS)
        settings.update(stored or {})

        settings["routing_rules"] = parse_routing_rules(
            settings.get("routing_rules", "[]"))[0]
        settings["default_channels"] = parse_default_channels(
            settings.get("default_channels", ""))

        # Case/whitespace normalization. THIS call is the one that fixes
        # delivery -- the plugin's copy only fixes what validate reports.
        # DEFAULT the effective list to raw UNCONDITIONALLY so a None _routing
        # branch keeps the operator's hand-written rules (spec correctness-I1).
        # This DELIBERATELY shares the raw list reference: every consumer treats
        # _effective_routing_rules read-only, and the non-None branch below
        # rebinds it to a fresh (compose + raw) list. Do NOT start mutating
        # _effective in place, or you would mutate the operator's rules too.
        settings["_effective_routing_rules"] = settings["routing_rules"]
        _routing = _routing_mod()
        if _routing is not None:
            _routing.normalize(settings)
            settings["_effective_routing_rules"] = (
                _routing.compose(settings) + settings["routing_rules"])

        inject_derived(settings)
        clear_last_settings_error()
        return settings
    except Exception as exc:
        _note_settings_error(exc)
        return None


def inject_derived(settings):
    """Add `_resolved_quiet_tz` / `_resolved_redact_hosts` (+ their info dicts
    for status reporting) to an already-merged settings dict.

    MUST run AFTER `settings.update(stored)`. Dispatcharr never prunes stored
    values whose field has been removed (`PluginManager.update_settings` does
    a wholesale replace; `_merge_settings_with_defaults` only ever ADDS), so
    an upgraded install still carries the old hand-typed `quiet_tz`. The
    `_resolved_` prefix is what makes shadowing structurally impossible
    rather than merely ordering-dependent.

    Never raises: derivation failing must not turn into an unreadable
    settings row (which fails the tick closed) or an exception escaping
    `Plugin.run()`."""
    mod = _dispatcharr_mod()
    if mod is None:
        return settings
    try:
        tz_info = mod.read_timezone()
        settings["_resolved_quiet_tz"] = tz_info["tz"]
        settings["_resolved_tz_info"] = tz_info
    except Exception:
        pass
    try:
        host_info = mod.read_redact_hosts()
        settings["_resolved_redact_hosts"] = host_info["hosts"]
        settings["_resolved_host_info"] = host_info
    except Exception:
        pass
    return settings


class LeaderLock:
    """Kernel-enforced single-leadership via `fcntl.flock` on
    `<base_dir>/leader.lock`. On platforms with no `fcntl` (Windows dev,
    most test runs) falls back to a single-process "always leader" stub so
    local/Windows development and the test suite both work; CI runs Linux
    and exercises the real flock path.
    """

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, "leader.lock")
        self._fd = None
        self._owned = False

    @property
    def owned(self):
        return self._owned

    def try_acquire(self):
        if self._owned:
            return True

        fcntl = _fcntl_mod()
        if fcntl is None:
            self._owned = True
            clear_lock_error()
            return True

        # A failure HERE is always a fault -- there is no benign reason the
        # lock file cannot be created. This is where the E3 trap lands.
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            _note_lock_error(exc)
            return False

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            # EWOULDBLOCK/EAGAIN is CONTENTION: another worker holds the lock,
            # which is the expected outcome for every non-leader worker on
            # every tick. Silent by design. Any other errno is a fault.
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                _note_lock_error(exc)
            return False

        self._fd = fd
        self._owned = True
        # Leadership proves the path is writable, so a stale fault from an
        # earlier boot must not linger and mislead.
        clear_lock_error()
        return True

    def release(self):
        if self._fd is not None:
            try:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except ImportError:
                pass
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._owned = False


class Collector:
    """One leader's claim + deliver loop. `mods` is a dict of the sibling
    notifier modules this collector drives: `store`, `spool`, `routing`,
    `gates`, `channels`, `deliver` (the last supplies the `Deliverer` class;
    `routing`/`gates`/`channels` are forwarded to `Deliverer` untouched, the
    same `mods` shape `deliver.py`'s own tests use)."""

    def __init__(self, version, base_dir, stop_event, mods, opener=None,
                 settings_fn=None, redact=None, smtp_factory=None):
        self.version = version
        self.base_dir = base_dir
        self.stop_event = stop_event
        self.mods = mods
        self.opener = opener
        self.settings_fn = settings_fn or read_settings
        # Fix 1: the production leader's Store previously defaulted to an
        # identity redact (Store._redact = lambda t: t), so every ledger and
        # failed/*.reason write on every tick was unredacted plaintext --
        # only plugin.py's admin-action Store instances got the real
        # redactor. `redact` (notify_client.redact, threaded in from
        # ensure_collector -> Plugin.__init__) now covers this path too;
        # `redact=None` (every existing direct-construction caller/test)
        # preserves the prior identity behavior byte-for-byte.
        self.redact = redact or (lambda t: t)
        self.store = mods["store"].Store(base_dir, redact=self.redact)
        self.lock = LeaderLock(base_dir)
        # Test seam for the smtp channel; None in production, where the
        # transport selects smtplib itself. Appended last so every
        # existing positional/keyword call site is unaffected.
        self.smtp_factory = smtp_factory
        self.runtime = {"last_error": None, "last_tick": 0}
        self._last_prune = 0.0

    # -- thread body ---------------------------------------------------

    def run(self):
        poll = DEFAULTS["spool_poll_s"]
        try:
            while not self.stop_event.is_set():
                if not self.lock.owned:
                    self.lock.try_acquire()
                if self.lock.owned:
                    try:
                        self._tick()
                        self.runtime["last_error"] = None
                    except Exception as exc:
                        # TYPE NAME ONLY on both surfaces: this used to store
                        # f"{type}: {exc}", which puts an OSError's filesystem
                        # path into a dict show_status can print.
                        self.runtime["last_error"] = type(exc).__name__
                        self.store.note_tick_error(exc, time.time())
                # Live cadence: _tick() stashes the effective spool_poll_s
                # from the settings it just read (fail-closed keeps the
                # prior value, so a bad/unreadable settings row never speeds
                # up or stalls the poll loop).
                poll = _coerce_poll_s(self.runtime.get("spool_poll_s", poll))
                self.stop_event.wait(poll)
        finally:
            self._clear_ticker()
            # ALWAYS release the flock on thread exit (stop Event or an
            # uncaught exception) -- a leaked fd deadlocks the NEXT
            # collector's try_acquire() forever (flock binds to the open
            # file description, not the process), defeating in-process
            # supersession on a version bump (bug: see module docstring).
            self.lock.release()

    # -- one pass --------------------------------------------------------

    def _tick(self, now_ms=None):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        now = now_ms / 1000.0
        self.store.ensure_dirs()

        settings = self.settings_fn()
        if settings is None:
            # Fail closed (spec §4.3): claim nothing, deliver nothing, but
            # keep beating so liveness stays visible in status/self-health.
            #
            # The banner MUST still expire here. This branch returns before
            # settings are available, so a ticker keyed on anything but the
            # file's own mtime would freeze a critical alert on the operator's
            # TV for as long as the fault lasted -- and read_settings() is on
            # record returning None once in production with no root cause.
            # FALLBACK_TTL_S is used because the configured TTL is unreadable.
            self._expire_ticker(now, ttl_s=None)
            state = self.store.load_state(now)
            state["fail_closed"] = True
            err = last_settings_error()
            if err:
                state["fail_closed_reason"] = err.get("type")
                state["fail_closed_count"] = err.get("count")
                state.setdefault("fail_closed_since", now)
            self.store.save_state(state, now)
            self.runtime["last_tick"] = now
            return {"fail_closed": True}

        # Live poll cadence (fail-closed tolerant): only stashed when
        # settings actually resolved -- run()'s loop keeps whatever value
        # (or the static default) it already has otherwise.
        self.runtime["spool_poll_s"] = _coerce_poll_s(
            settings.get("spool_poll_s"))

        spool = self.mods["spool"]
        spool.claim(spool.scan(self.store.spool_dir), self.store.pending_dir)

        # Fix 2 (spec §8.1): enforce redact_hostnames at the wire, on top of
        # the base credential redact -- built fresh each tick since
        # redact_hostnames is a live setting. `self.redact` is identity when
        # no real redactor was threaded in (direct-construction tests).
        wire_redact = build_wire_redact(self.mods["channels"], settings,
                                         self.redact)

        ticker = None
        ticker_errors = 0
        tmod = _ticker_mod()
        if tmod is not None:
            ticker = tmod.from_settings(self.base_dir, settings, lambda: now)
            try:
                ticker.expire(now)
            except Exception:
                # Never propagate -- the banner must not break delivery. But
                # COUNT it: an expiry that keeps failing leaves a stale
                # CRITICAL banner drawn over live video indefinitely, which is
                # the exact failure the five-stale-paths design exists to
                # prevent, and nothing anywhere said why. deliver.py already
                # ledgers this class; the collector silently swallowed it.
                #
                # Held in a LOCAL and applied to the state loaded further down:
                # `state` up here belongs to the fail-closed branch, which
                # returns, so assigning to it on this path is a NameError.
                ticker_errors += 1

        deliverer = self.mods["deliver"].Deliverer(
            self.store, settings, self.version, lambda: now, self.mods,
            opener=self.opener, wire_redact=wire_redact,
            smtp_factory=self.smtp_factory, ticker=ticker)
        stats = deliverer.tick(now_ms)
        # Captured HERE, immediately after the Deliverer loaded state.json, and
        # read by the arming block further down. Two things would otherwise
        # erase it: `gates.Gates` pops `_corrupt_recovered` out of the state
        # dict while it consumes the conservative-cap recovery (inside the tick
        # above), and the fallback `load_state` below would reset the store
        # attribute for a deliver module that does not expose `.state`.
        corrupt_recovered = bool(getattr(self.store, "last_load_corrupt", False))

        if now - self._last_prune >= _PRUNE_INTERVAL_S:
            self.store.prune_failed(now)
            spool.reap_orphan_sidecars(self.store.pending_dir)
            self._last_prune = now

        # "Unclaimed" (spec §4.8 self-health) means still sitting in
        # spool/, never reached pending/ -- retrying/held events legitimately
        # sit in pending/ for hours and must not trip a staleness warning.
        # Under a healthy, ticking leader this is ~always empty right after
        # the claim above; a growing age here is exactly the signal that the
        # collector has stopped ticking while notify_client keeps writing.
        remaining = spool.scan(self.store.spool_dir)
        # Reuse the state dict the Deliverer just loaded, mutated and saved
        # rather than re-parsing state.json from disk -- the second read was
        # pure waste every tick, and on a failed Deliverer save the in-memory
        # dict is MORE current than the file. The getattr guard keeps this
        # compatible with an injected deliver module whose Deliverer does not
        # expose `.state`.
        state = getattr(deliverer, "state", None)
        if state is None:
            state = self.store.load_state(now)
        if ticker_errors:
            state["ticker_errors"] = (state.get("ticker_errors") or 0) + ticker_errors
        state["fail_closed"] = False
        # Clear the diagnosis on recovery, but keep the COUNT: a settings
        # read that fails intermittently is exactly the shape of the one
        # unexplained production occurrence, and a counter that survives
        # recovery is the only way to notice it happening again.
        state.pop("fail_closed_reason", None)
        state.pop("fail_closed_since", None)
        state["last_tick"] = now
        state["last_stats"] = stats
        # Arming lives HERE, in the leader, not in the status action: the
        # status action runs in whichever uWSGI worker served the click, and
        # state.json is rewritten by this tick from a different process, so
        # arming from the status action would race those writes. Parse errors
        # are consumed on the very next line to control the forget_stale flag --
        # the status action is what surfaces the errors to the operator, not the
        # collector.
        #
        # `history_lost` carries the corrupt-file recovery INTO the arm, which
        # is durable, instead of skipping the arm for that one tick, which was
        # not. Skipping made the status red for about five seconds: the next
        # tick found an intact, empty state.json, armed every configured key at
        # `now`, and a caller dead for a month then read as "expected, none
        # delivered yet" with no problem for a full cadence. `arm` now marks
        # those keys instead, and the mark survives until a real delivery
        # clears it.
        #
        # `forget_stale` is switched OFF when the parse produced errors. Arming
        # is otherwise UNCONDITIONAL, with no `if entries` guard, because an
        # empty parse is exactly the case where every armed key has just become
        # stale (the operator blanked report_expect) and `arm` is the code that
        # forgets a stale key -- but a parse ERROR yields the same empty entry
        # list for the opposite reason, and pruning there means a corrected
        # typo re-arms an overdue expectation at `now` and buys it a green
        # cadence. Errors are still not reported from here: the status action
        # is what surfaces them to the operator.
        entries, errors = self.mods["report_expect"].parse(
            settings.get("report_expect"), settings.get("report_expect_days"))
        # KNOWN GAP: if settings read returned None on this same tick that
        # detected corrupt state.json, the fail-closed branch above returns
        # before reaching this point, so corrupt_recovered is never passed to
        # arm() and no history-lost mark is recorded even though recovery just
        # happened. This path is rare and pre-existing; it trades a transient
        # blindness to ONE key's origin on ONE tick against fail-closed safety.
        self.mods["report_expect"].arm(
            state, entries, now, history_lost=corrupt_recovered,
            forget_stale=not errors)
        state["spool_depth"] = len(remaining)
        state["oldest_unclaimed_s"] = self._oldest_age(remaining, now)
        # A tick that reached here succeeded, so a recorded error is stale.
        # Cleared on the state dict this save is about to persist rather than
        # via a second read/write pair.
        self.store.clear_tick_error(state)
        self.store.save_state(state, now)
        self.runtime["last_tick"] = now
        return stats

    def _expire_ticker(self, now, ttl_s):
        """Expire the banner without needing settings. Never raises."""
        tmod = _ticker_mod()
        if tmod is None:
            return
        try:
            ttl = tmod.FALLBACK_TTL_S if ttl_s is None else ttl_s
            tmod.Ticker(self.base_dir, ttl_s=ttl).expire(now)
        except Exception:
            pass

    def _clear_ticker(self):
        """Wipe the banner on thread exit.

        ffmpeg's lifetime is bound to the CLIENT's session and Newsflasharr's
        to the plugin -- they are independent -- so without this a stopped
        or superseded collector leaves a stale critical alert drawn over
        live video until the viewer changes channel. Runs on supersession
        too (version bump), which is intentional and harmless.
        """
        tmod = _ticker_mod()
        if tmod is None:
            return
        try:
            tmod.Ticker(self.base_dir).clear()
        except Exception:
            pass

    @staticmethod
    def _oldest_age(paths, now):
        """Oldest age (seconds) among `paths`, keyed off the `<ts_ms>-...`
        filename prefix `notify_client.notify()` writes (falls back to
        mtime for anything that doesn't parse). 0.0 when `paths` is empty."""
        ages = []
        for p in paths:
            name = os.path.basename(p)
            try:
                ts_ms = int(name.split("-", 1)[0])
                ages.append(now - ts_ms / 1000.0)
            except (ValueError, IndexError):
                try:
                    ages.append(now - os.path.getmtime(p))
                except OSError:
                    pass
        return max(ages) if ages else 0.0


# -- synthetic sys.modules runtime, which survives a plugin reload ------------

# Module-level, import-time -- CPython serializes module import, so this lock
# object itself is created race-free. Guards the FIRST creation of the
# synthetic runtime module below: an unguarded check-then-act here let two
# concurrent preemptive threads both see sys.modules.get(RUNTIME_KEY) is None
# on the very first call in a worker, each build their own module with their
# own Lock(), and only the last write survives in sys.modules -- so the two
# callers would serialize on DIFFERENT lock objects and ensure_collector's
# `mod.lock` guard would provide no actual mutual exclusion between them
# (the same shape as the reload problem, one layer up).
_RUNTIME_LOCK = threading.Lock()


def _get_runtime():
    mod = sys.modules.get(RUNTIME_KEY)
    if mod is not None:
        return mod
    with _RUNTIME_LOCK:
        mod = sys.modules.get(RUNTIME_KEY)   # re-check: we may have lost the race
        if mod is None:
            mod = types.ModuleType(RUNTIME_KEY)   # no __file__: dodges the
            mod.state = None                      # loader's reload-purge patterns
            # Guards ensure_collector's whole check-signature-and-spawn
            # sequence -- parked here, not a
            # module global, so it survives Dispatcharr's plugin-reload
            # ping-pong the same way mod.state does.
            mod.lock = threading.Lock()
            sys.modules[RUNTIME_KEY] = mod
        return mod


def _load_sibling_mods():
    """Import store/spool/routing/gates/channels/deliver/attachment as real
    package siblings of this module. Only works when collector.py was itself
    imported as part of a real package (Dispatcharr's plugin loader, or
    conftest.load_plugin()) -- raises ImportError when standalone-loaded
    (conftest.load_mod), which is fine: nothing in the test suite calls
    `ensure_collector` for real, tests construct `Collector` directly with
    their own injected `mods`.
    """
    pkg = __package__
    if not pkg:
        raise ImportError("collector.py has no package context")
    names = ("store", "spool", "routing", "gates", "channels", "deliver",
              "attachment", "report_expect")
    return {name: importlib.import_module(f".{name}", pkg) for name in names}


def ensure_collector(version, base_dir="/data/newsflasharr", redact=None):
    """Idempotent, cheap to call on every request:
    a no-op when a live thread is already armed at this exact version;
    otherwise signals the old thread's parked stop Event (if any) and spawns
    a fresh one. Never called from `_dispatch` -- called unconditionally from
    `Plugin.__init__` in every worker so the leader lives wherever the flock
    lands, not wherever the UI request happened to be routed (spec §4.1).

    The whole check-signature-and-spawn sequence runs under `mod.lock` so
    concurrent gevent greenlets (many in-flight requests all seeing a stale
    signature right after a version bump) can't both pass the "not armed at
    this version" check and each spawn a thread -- only the last write to
    `mod.state` would be tracked, orphaning the other, un-supersedable.
    On the already-armed happy path this costs one cheap lock
    acquire/release, no thread work.
    """
    mod = _get_runtime()
    sig = (version,)
    with mod.lock:
        prev = mod.state
        if prev is not None and prev["sig"] == sig and prev["thread"].is_alive():
            return

        if prev is not None:
            prev["stop"].set()

        stop = threading.Event()
        mods = _load_sibling_mods()
        inst = Collector(version, base_dir, stop, mods, redact=redact)
        thread = threading.Thread(target=inst.run, daemon=True,
                                   name="notifyarr-collector")
        mod.state = {"thread": thread, "stop": stop, "sig": sig,
                     "runtime": inst.runtime}
        thread.start()
