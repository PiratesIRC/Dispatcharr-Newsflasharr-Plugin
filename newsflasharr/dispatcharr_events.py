"""Newsflasharr <- Dispatcharr's own system events.

Dispatcharr emits 17 system events (`apps/connect/models.py` SUPPORTED_EVENTS)
and dispatches them to any plugin action declaring an `events` list. This
module is the pure half of that subscription: the event table, the operator's
selection, the lint, and the composition of a notification from a raw payload.

No Django, no `notify` import, no I/O -- everything here is a function of its
arguments, so it can be unit tested with plain dicts.

The operator-facing description of these events is in README.md, under
"Dispatcharr system events".

Two things drove the design, both measured against the live box rather than
assumed, and both the opposite of the obvious answer:

  1. The payload is NOT safe to format generically. `dispatch_event_system`
     puts FIVE credential-bearing keys in it (see _URL_BEARING), and provider
     credentials live in the path of a stream URL in this installation. So there is an
     allowlist.

  2. The allowlist is NOT sufficient on its own. Several allowlisted fields are
     attacker-controlled -- `user` on login_failed is raw request input, and a
     live probe produced a stored payload containing Discord markdown plus a
     newline. NO layer of the three-layer redaction model touches that: all
     three are credential-shaped, and fit_body only truncates. `sanitize()` is
     the actual control; the allowlist is only the first filter.
"""
from __future__ import annotations

import unicodedata

# volume: "rare" | "occasional" | "frequent" | "daily" | "per_zap"
EVENTS = {
    "login_failed": {"severity": "critical", "volume": "rare",
                     "overlaps_stream_monitor": False,
                     "label": "login attempt failed"},
    "m3u_blocked": {"severity": "critical", "volume": "rare",
                    "overlaps_stream_monitor": False,
                    "label": "M3U request blocked"},
    "epg_blocked": {"severity": "critical", "volume": "rare",
                    "overlaps_stream_monitor": False,
                    "label": "EPG request blocked"},
    "channel_error": {"severity": "warning", "volume": "occasional",
                      "overlaps_stream_monitor": True, "label": "channel error"},
    "channel_failover": {"severity": "warning", "volume": "occasional",
                         "overlaps_stream_monitor": True, "label": "channel failover"},
    "channel_reconnect": {"severity": "info", "volume": "occasional",
                          "overlaps_stream_monitor": True,
                          "label": "channel reconnected"},
    "stream_switch": {"severity": "info", "volume": "frequent",
                      "overlaps_stream_monitor": True, "label": "stream switched"},
    "m3u_refresh": {"severity": "info", "volume": "daily",
                    "overlaps_stream_monitor": False, "label": "M3U refreshed"},
    "epg_refresh": {"severity": "info", "volume": "daily",
                    "overlaps_stream_monitor": False, "label": "EPG refreshed"},
    "recording_start": {"severity": "info", "volume": "occasional",
                        "overlaps_stream_monitor": False, "label": "recording started"},
    "recording_end": {"severity": "info", "volume": "occasional",
                      "overlaps_stream_monitor": False, "label": "recording ended"},
    "channel_start": {"severity": "info", "volume": "per_zap",
                      "overlaps_stream_monitor": False, "label": "channel started"},
    "channel_stop": {"severity": "info", "volume": "per_zap",
                     "overlaps_stream_monitor": False, "label": "channel stopped"},
    "client_connect": {"severity": "info", "volume": "per_zap",
                       "overlaps_stream_monitor": False, "label": "client connected"},
    "client_disconnect": {"severity": "info", "volume": "per_zap",
                          "overlaps_stream_monitor": False,
                          "label": "client disconnected"},
    "vod_start": {"severity": "info", "volume": "per_zap",
                  "overlaps_stream_monitor": False, "label": "VOD started"},
    "vod_stop": {"severity": "info", "volume": "per_zap",
                 "overlaps_stream_monitor": False, "label": "VOD stopped"},
}

# Rare, actionable, and not already reported by sentinelarr. login_failed is
# deliberately NOT here: it is remotely triggerable by an unauthenticated party
# (see lint()), so recommending it by default would recommend an attacker-driven
# mail volume.
RECOMMENDED = ("m3u_blocked", "epg_blocked")

# Every key whose value is, or may contain, a URL. In a typical installation a stream URL
# carries provider credentials as `<user>/<pass>` path segments, so ANY of these
# reaching a notification body is a credential leak. Listed exhaustively because
# naming only the obvious two is how a later maintainer adds `url` "just for
# debugging channel_error" and reintroduces the leak.
_URL_BEARING = ("stream_url", "channel_url", "url", "new_url", "error_message")

# Safe to show AND worth showing. Chosen per event from 500 real SystemEvent
# rows: an allowlist picked for fewness alone left channel_error, m3u_refresh
# and epg_refresh with no content at all.
#
# `client_ip` is deliberately absent. On login_failed it is ALWAYS 127.0.0.1
# (the call site reads request.META["REMOTE_ADDR"], and behind nginx -> uWSGI
# over a unix socket that is the socket peer), so it would actively mislead an
# operator into thinking a brute-force attempt was local. On client_connect it
# IS attacker-controlled, since get_client_ip() reads X-Forwarded-For and nginx
# appends rather than replaces. Useless where it was justified, unsafe where it
# was not.
# INVARIANT: no _URL_BEARING key may ever be added to this allowlist.
# sanitize() truncates each value to _VALUE_LIMIT BEFORE notify_client's
# credential redaction runs, and a cut landing between a password and its
# "@" defeats _BASIC_AUTH_RE (the "@" anchor is mandatory) -- the partial
# credential then survives the wire layer too, because after truncation the
# credential BECOMES the parsed hostname. The ordering is safe today ONLY
# because nothing here can carry a URL; adding one would need the truncation
# moved after redaction first. Same measured failure as channels.fit_body's
# redact-then-truncate rule.
_ALLOWED = (
    "channel_name", "stream_name", "provider_name", "profile_used",
    "reason", "user", "error_type", "attempts", "account_name",
    "source_name", "duration", "elapsed_time",
    "streams_created", "streams_updated", "streams_deleted",
    "channels", "programs",
    # 2026-07-21 formatting spec: `profile` is the OUTPUT-profile name the
    # blocked M3U/EPG endpoints stamp ("all" default) -- verified at the
    # emission sites in /app/apps/output/views.py, never a provider value.
    "profile",
    # `user_agent` REVERSES the 2026-07-19 spec's "stays excluded" decision,
    # deliberately: the prose "Client:" clause gives it the diagnostic value
    # the old spec said it lacked (your own misconfigured device vs a
    # scanner). It is attacker-controlled free text that MAY contain a URL --
    # but only the ATTACKER'S OWN text; nothing server-side (and no provider
    # credential) is ever interpolated into it, it is not _URL_BEARING, and
    # _prose_slot/sanitize + the wire redaction layers apply to it like any
    # other slot.
    "user_agent",
)

_STRIP_CATS = frozenset({"Cc", "Cf", "Zl", "Zp"})
_MARKDOWN = "`*_~|[]"
_VALUE_LIMIT = 200


def sanitize(value):
    """A payload value made safe to place in a notification body.

    Load-bearing, not decorative. `user` on login_failed is raw
    `request.data.get("username")`; a live probe stored
    `[click](https://evil.example/x)\\nINJECTED`, which renders in Discord as a
    clickable link and breaks the body's line structure. Nothing else in the
    pipeline stops it: the three redaction layers all match credential shapes,
    and fit_body only truncates.

    Strips the categories that break structure (Cc/Zl/Zp -- CR, LF, NUL,
    U+2028, U+2029) plus Cf (zero-width characters, which have caused real
    faults here), neutralises markdown actives, and truncates: a payload field
    is a label, not a document.
    """
    text = "" if value is None else str(value)
    # Structure-breaking codepoints become a space rather than vanishing, so
    # "a\nb" reads as "a b" instead of the misleading "ab".
    cleaned = "".join(
        " " if unicodedata.category(ch) in _STRIP_CATS else ch
        for ch in text
    )
    for ch in _MARKDOWN:
        cleaned = cleaned.replace(ch, "")
    return " ".join(cleaned.split())[:_VALUE_LIMIT]


def parse_selection(raw):
    """`dispatcharr_events` -> (enabled set, unknown names).

    Unknown names are RETURNED, never silently dropped: a typo that quietly
    disables an alert the operator believes is armed is the one failure this
    feature must not have.
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw or "").split(",")
    enabled, unknown = set(), []
    for part in parts:
        name = part.strip().lower()
        if not name:
            continue
        if name in EVENTS:
            enabled.add(name)
        elif name not in unknown:
            unknown.append(name)
    return enabled, unknown


# Group toggles (2026-07-21 formatting spec s2). Churn/per_zap events are
# deliberately absent from every group -- they stay reachable only through
# the CSV escape hatch, so one careless click cannot bury the ledger.
GROUP_EVENTS = {
    "events_security": ("m3u_blocked", "epg_blocked"),
    "events_login": ("login_failed",),
    "events_stream_health": ("channel_error", "channel_failover"),
    "events_recordings": ("recording_start", "recording_end"),
}


def _truthy(value):
    """Group-toggle truthiness for the RAW settings dict.

    The subscription check runs on the streaming hot path ABOVE
    _coerce_settings, so this must handle what a GUI save (real bool) or a
    direct DB write (string) can leave -- the same vocabulary dustarr's
    boolean-coercion fix uses. `bool("false")` is True; this is not.
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no")
    return bool(value)


def selected_events(settings):
    """The EFFECTIVE event set: union(enabled groups' events, CSV).

    THE one resolver (spec s2): the on_dispatcharr_event handler and
    validate's report both call this, so the hot path and the reporting
    surface cannot drift. Returns (enabled_set, unknown_csv_names).
    """
    settings = settings if isinstance(settings, dict) else {}
    enabled, unknown = parse_selection(settings.get("dispatcharr_events"))
    for field, events in GROUP_EVENTS.items():
        if _truthy(settings.get(field)):
            enabled.update(events)
    return enabled, unknown


def lint(raw, max_sends_per_hour=20):
    """Human-readable problems and warnings for the CSV alone (legacy
    surface; the group-aware entry point is lint_settings below)."""
    enabled, unknown = parse_selection(raw)
    return _warnings_for(enabled, unknown, max_sends_per_hour)


def lint_settings(settings, max_sends_per_hour=20):
    """Warnings over the EFFECTIVE event set (spec s2): a group-enabled
    login_failed must fire the same safety warning a CSV-enabled one does --
    keying warnings on the CSV alone was rev 1's downgrade defect."""
    enabled, unknown = selected_events(settings)
    return _warnings_for(enabled, unknown, max_sends_per_hour)


def _warnings_for(enabled, unknown, max_sends_per_hour):
    out = []
    for name in unknown:
        out.append(f"ERROR -- unknown event {name!r}. It will be IGNORED.")
    for name in sorted(enabled):
        meta = EVENTS[name]
        if meta["volume"] == "per_zap":
            out.append(
                f"WARNING -- {name!r} fires on every channel change. With "
                f"max_sends_per_hour={max_sends_per_hour} it will exhaust your "
                f"hourly budget and HOLD real alerts behind it.")
        elif meta["volume"] == "frequent":
            out.append(f"WARNING -- {name!r} fires frequently and may crowd out "
                       f"other notifications.")
        if meta["overlaps_stream_monitor"]:
            out.append(f"WARNING -- {name!r} is also reported by Sentinelarr; "
                       f"you will get two notifications for one fault.")
    if "login_failed" in enabled:
        out.append(
            "WARNING -- 'login_failed' can be triggered by an unauthenticated "
            "remote party (Dispatcharr's login throttle keys on a "
            "caller-supplied X-Forwarded-For). It is critical, so it bypasses "
            "the hourly cap and CANNOT starve other alerts -- but at roughly "
            "6/hour it can exhaust a mail provider's daily quota. Prefer "
            "routing it to a push channel rather than email.")
    return out


def _prose_slot(value):
    """A payload value made safe for a PROSE slot (spec s5a).

    sanitize() was proven for the key:value context, where each value sits on
    its own line. Prose adds structure sanitize never defended: templates
    quote slots ('{user}') and use ':' as a clause delimiter, so a value
    containing quotes/colons could forge a misleading clause ("admin' failed:
    password correct"). Stripping quotes and collapsing colons/semicolons
    makes breakout UNREPRESENTABLE rather than filtered.
    """
    text = sanitize(value)
    text = text.replace("'", "").replace('"', "")
    text = text.replace(":", " ").replace(";", " ")
    return " ".join(text.split())


def _slot(payload, key, default="unknown"):
    value = _prose_slot(payload.get(key))
    return value or default


def _sentence(event, payload):
    """The PROSE body for a templated event (spec s5), or None.

    One template per event, built ONLY from allowlisted, _prose_slot'd keys --
    a missing key degrades to 'unknown' or drops its clause, never KeyError,
    and the payload is never iterated. Untemplated events return None and
    compose() falls back to the key:value loop.
    """
    if event in ("m3u_blocked", "epg_blocked"):
        what = "M3U playlist" if event == "m3u_blocked" else "EPG guide"
        body = (f"Dispatcharr denied an {what} request for profile "
                f"'{_slot(payload, 'profile')}': {_slot(payload, 'reason')}.")
        ua = _prose_slot(payload.get("user_agent"))
        return body + (f" Client: {ua}." if ua else "")
    if event == "login_failed":
        body = (f"A login attempt for user '{_slot(payload, 'user')}' "
                f"failed: {_slot(payload, 'reason')}.")
        ua = _prose_slot(payload.get("user_agent"))
        return body + (f" Client: {ua}." if ua else "")
    if event == "channel_error":
        name = _prose_slot(payload.get("channel_name"))
        where = f" on {name}" if name else ""
        return (f"Channel error{where}: {_slot(payload, 'error_type')} "
                f"after {_slot(payload, 'attempts')} attempt(s).")
    if event == "channel_failover":
        body = (f"Channel {_slot(payload, 'channel_name')} failed over: "
                f"{_slot(payload, 'reason')}.")
        duration = _prose_slot(payload.get("duration"))
        return body + (f" (buffering {duration}s)" if duration else "")
    if event == "channel_reconnect":
        name = _prose_slot(payload.get("channel_name"))
        return f"Channel {name} reconnected." if name else None
    if event == "recording_start":
        name = _prose_slot(payload.get("channel_name"))
        return f"Recording started on {name}." if name else None
    if event == "recording_end":
        name = _prose_slot(payload.get("channel_name"))
        return f"Recording ended on {name}." if name else None
    return None


def _title_for(event, payload):
    meta = EVENTS.get(event) or {}
    label = meta.get("label", event)
    # Subjects come from keys the event ACTUALLY carries (spec s1/s4): the
    # blocked endpoints stamp an output-profile name, login_failed a user.
    if event in ("m3u_blocked", "epg_blocked"):
        profile = _prose_slot(payload.get("profile"))
        return f"{label}: profile '{profile}'" if profile else label
    if event == "login_failed":
        user = _prose_slot(payload.get("user"))
        return f"{label}: user '{user}'" if user else label
    # _prose_slot, not bare sanitize: after "[sym] Source: " the colon is
    # structural in the delivered title too, and a subject containing
    # quotes/colons could forge a clause there (the s5a breakout, one
    # surface over).
    subject = (_prose_slot(payload.get("channel_name"))
               or _prose_slot(payload.get("account_name"))
               or _prose_slot(payload.get("source_name"))
               or _prose_slot(payload.get("user")))
    return f"{label}: {subject}" if subject else label


def compose(event, payload):
    """(title, body, severity, dedup_key) from a raw Dispatcharr payload.

    Reads an explicit allowlist and NEVER iterates the payload -- a generic
    `", ".join(f"{k}={v}")` would put provider credentials on the wire via any
    of `_URL_BEARING`. Every value goes through `sanitize()`.

    Total over hostile input: a non-dict payload, a missing key, and non-string
    values (`attempts` is an int, `interrupted` a bool in real rows) all
    compose without raising.
    """
    payload = payload if isinstance(payload, dict) else {}
    meta = EVENTS.get(event) or {}
    severity = meta.get("severity", "info")

    # Templated events get PROSE (spec s5) -- _sentence IS the body for them.
    # The key:value loop below remains the fallback for untemplated events.
    body = _sentence(event, payload)
    if body is None:
        lines = []
        for key in _ALLOWED:
            if key not in payload:
                continue
            value = sanitize(payload.get(key))
            if value:
                lines.append(f"{key.replace('_', ' ')}: {value}")
        body = "\n".join(lines) if lines else f"Dispatcharr reported {event}."
    channel_id = payload.get("channel_id")
    dedup_key = f"{event}:{sanitize(channel_id) or 'system'}"
    return _title_for(event, payload), body, severity, dedup_key
