"""Newsflasharr delivery channels — Discord, generic webhook, ntfy, Apprise.

Stdlib-only urllib senders. Each `send()` call MUST NEVER raise (deliver.py
calls this per-channel, per-event, on a hot path) and the returned detail
string MUST NEVER contain the target URL — a Discord webhook URL or an ntfy
token IS a bearer credential, and detail strings can end up in logs / UI
toast text. Use the channel name and/or `mask_target()` instead.

Hard-won lessons baked in here:
  - Discord's Cloudflare edge 403s urllib's default User-Agent, so every
    request (not just Discord's) carries an explicit one.
  - Discord messages must set `allowed_mentions: {"parse": []}` so a title
    containing "@everyone" can't ping the whole server.
  - ntfy must use its JSON publish format (POST to the server root with a
    JSON body), not the header-based publish form — event titles carry
    non-latin-1 glyphs (▎, ᴿᴬᵂ) that would crash an HTTP header value.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# "ticker" is a routable channel name but NOT a transport: it has no builder and
# no entry in _TRANSPORTS. deliver.py intercepts it before dispatch and calls the
# injected Ticker instead, because the on-screen banner is a local file write with
# terminal (never retried) semantics, not a network send. It is listed here so
# routing.lint accepts it in an operator's channel list and show_status can report
# it -- a name routing rejects is a name the operator cannot use.
KNOWN = ("discord", "webhook", "connect", "ntfy", "apprise", "smtp", "ticker")
USER_AGENT_PREFIX = "Dispatcharr-Newsflasharr/"
DISCORD_LIMIT = 2000

# ntfy.sh answers HTTP 500 at exactly 4096 BYTES on the `message` field --
# measured 2026-07-19: 4090 -> 200, 4096 -> 500. Not a 4xx, and not the
# attachment conversion its docs imply. A 500 is indistinguishable from a
# transient fault, so deliver.py's ladder retries it 30s/300s/1800s and then
# gives up: an over-limit ntfy notification is LOST, not degraded. 4000 leaves
# margin for the title and JSON overhead sharing the request.
NTFY_LIMIT_BYTES = 4000


def _measure(text, unit):
    return len(text.encode("utf-8")) if unit == "bytes" else len(text)


def _cut(text, budget, unit):
    """Longest prefix of `text` measuring <= `budget`, never splitting a
    multi-byte character, preferring a whitespace boundary in the last 10%."""
    if budget <= 0:
        return ""
    if unit == "bytes":
        raw = text.encode("utf-8")[:budget]
        cut = raw.decode("utf-8", "ignore")     # drops a split trailing char
    else:
        cut = text[:budget]
    window = max(1, len(cut) // 10)
    space = cut.rfind(" ", len(cut) - window)
    if space > 0:
        cut = cut[:space]
    return cut


def truncation_marker(omitted):
    """ASCII, with a MANDATORY leading newline.

    `redact_hosts`' `_WIRE_URL_RE = r"https?://\\S+"` swallows any marker that
    abuts a URL -- measured: a marker with no preceding whitespace was consumed
    entirely. Only reachable once redaction fires, so happy-path tests never
    catch it. ASCII because plugin.py contains zero em dashes and the workspace
    rule is plain ASCII; decorative characters also cost bytes on the
    byte-limited channel.
    """
    return f"\n[truncated, {omitted} characters omitted]"


def fit_body(text, limit, unit="chars", marker_for=truncation_marker):
    """`text` shortened to fit `limit`, with the marker INSIDE the budget.

    Appending a marker past the limit is what pushes a message over the API
    bound, which is the bug this exists to fix -- so the marker is always
    accounted for.

    MUST run on already-wire-redacted text. Truncating first would defeat
    `notify_client._BASIC_AUTH_RE`, whose trailing `@` is mandatory: cutting
    between the password and the `@` removes the anchor, the regex stops
    matching, and the credential ships. The wire layer cannot rescue it either,
    because after truncation the credential BECOMES the hostname
    (`urlsplit("http://USER:Pa55w0rd").hostname` -> `"user"`). Calling this from
    inside the builders satisfies that structurally: deliver.py applies
    `wire_redact` before `channels.send`, so a builder only ever sees redacted
    text. Redaction also EXPANDS text (~26% measured), so a budget computed
    before it would be measuring the wrong string.
    """
    text = text or ""
    if limit is None or _measure(text, unit) <= limit:
        return text

    # Size the budget against the LONGEST marker this call could produce, so a
    # later digit reduction can only ever make the result smaller.
    reserve = _measure(marker_for(len(text)), unit)
    budget = limit - reserve
    if budget <= 0:
        return _cut(marker_for(len(text)), limit, unit)

    kept = _cut(text, budget, unit)
    marker = marker_for(len(text) - len(kept))
    if not kept.strip():
        return _cut(marker, limit, unit)     # never an empty Discord content
    return kept + marker

_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")

_EMAIL_RENDER = None


def local_stamp(event, settings, now_fn=time.time):
    """The event's time in DISPATCHARR'S OWN timezone, e.g.
    `2026-07-27 19:31 CDT`, or None if it cannot be determined.

    Lives here rather than in `email_render` because that module is pure by
    contract -- no clock, no `zoneinfo`, no settings. Returning None on any
    failure is deliberate: the footer simply omits the date. A wrong local
    time on a report footer is worse than no time at all, and this must
    never be able to fail an email that would otherwise send.

    The zone is `_resolved_quiet_tz` (Dispatcharr's `CoreSettings`
    time_zone, already zoneinfo-validated upstream) -- NEVER the container's
    `TZ` or Django's `settings.TIME_ZONE`, which are a documented trap: on
    this box `TZ` currently happens to agree, so code reading it looks
    correct today and would silently drift the moment either changed.
    """
    try:
        from datetime import UTC, datetime
        from zoneinfo import ZoneInfo
        ts = event.get("ts")
        # `ts` is epoch MILLISECONDS in an event; seconds everywhere else.
        secs = (ts / 1000.0) if isinstance(ts, (int, float)) and ts > 0 \
            else now_fn()
        name = (settings.get("_resolved_quiet_tz") or "").strip()
        if not name:
            return None
        moment = datetime.fromtimestamp(secs, tz=UTC)
        return moment.astimezone(ZoneInfo(name)).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return None


def _email_render():
    """The `email_render` sibling, resolved relative-first then by file path.

    channels.py must stay standalone-loadable (conftest.load_mod), so the
    relative import cannot be at module level and its failure is a normal
    state. The path fallback works in both worlds, which the bare-name
    fallback would not: conftest registers modules under a prefixed key, so
    `import email_render` finds nothing.
    """
    global _EMAIL_RENDER
    if _EMAIL_RENDER is not None:
        return _EMAIL_RENDER
    try:
        from . import email_render as mod
    except Exception:
        import importlib.util
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "email_render.py")
        spec = importlib.util.spec_from_file_location("_notifyarr_email_render",
                                                       path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    _EMAIL_RENDER = mod
    return mod


def safe_url(url):
    """Return url unchanged iff its scheme is http/https, else None.

    Drops javascript:/data:/file:/etc — those have no business being
    embedded in an outbound webhook payload or click target.
    """
    if not url:
        return None
    try:
        scheme = urllib.parse.urlsplit(str(url)).scheme
    except Exception:
        return None
    return url if scheme in ("http", "https") else None


def mask_target(url):
    """scheme://host/first-path-segment/… — enough to identify the service
    in logs without exposing the token/path that follows."""
    try:
        parsed = urllib.parse.urlsplit(str(url))
        segments = [s for s in parsed.path.split("/") if s]
        first = segments[0] if segments else ""
        tail = f"{first}/…" if first else "…"
        return f"{parsed.scheme}://{parsed.netloc}/{tail}"
    except Exception:
        return "…"


_WIRE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# A hostname-shaped run of text OUTSIDE any URL: letters/digits/dots/hyphens,
# bounded so a match can never start or end mid-token. Two chars minimum,
# which every real (multi-label) needle exceeds.
_WIRE_HOST_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.-])[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9](?![A-Za-z0-9.-])")


def _bare_host_sub(text, needles):
    """Scrub BARE (schemeless) occurrences of the configured hostnames.

    The URL pass above cannot see these, and in this installation they are the COMMON
    form: M3U account NAMES are hostnames ("provider.tv-alt1"), and
    dispatcharr_events pipes account/source names straight into titles and
    bodies -- so a URL-anchored redactor left the provider's identity in
    cleartext on the wire.

    A needle matches inside a token at a label boundary: preceded by the
    token start or a ".", followed by the token end, a "." or a "-" (the
    account-name suffix shape). The WHOLE token is replaced -- the suffix
    alone ("-alt1") still identifies which account. Deliberately biased
    toward over-redaction, same as A1's extraction: a scrubbed token in
    prose is inert, a missed hostname goes on the wire.
    """
    def _tok(match):
        token = match.group(0)
        low = token.lower()
        for h in needles:
            i = low.find(h)
            while i != -1:
                end = i + len(h)
                if (i == 0 or low[i - 1] == ".") and (
                        end == len(low) or low[end] in ".-"):
                    return "<redacted-host>"
                i = low.find(h, i + 1)
        return token

    return _WIRE_HOST_TOKEN_RE.sub(_tok, text)


def redact_hosts(text, hosts):
    """Replace any http(s) URL in `text` whose bare host matches one of
    `hosts` (exact, or a subdomain of it) with the literal
    `<redacted-url>`, and any BARE (schemeless) occurrence of one of
    `hosts` outside a URL with `<redacted-host>` (spec §8.1: the operator
    knows his provider hostnames even though no regex knows every
    credential transport shape). `hosts` is a list of plain hostname
    strings (e.g. from the `redact_hostnames` setting, comma-split by the
    caller); empty/falsy `text` or an empty/all-empty `hosts` is a no-op —
    returns `text` unchanged.

    URL matching is domain-boundary, not substring: `edge-23.provider.tv`
    and `provider.tv` both match host `provider.tv`, but
    `evil-provider.tv.attacker.example` does NOT — a bare `h in netloc`
    substring check would over-match that. `urlsplit(...).hostname`
    already strips `userinfo@` and `:port` (and lower-cases) to get the
    bare host. The bare pass runs ONLY on the text between URL matches, so
    a URL the host rule declines to redact keeps that contract even when a
    needle-shaped string appears in its path."""
    if not text or not hosts:
        return text
    needles = [h.lower() for h in hosts if h]
    if not needles:
        return text

    def _sub(match):
        url = match.group(0)
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except ValueError:
            # Can't even parse it as a URL -- fail CLOSED (this codebase's
            # convention) rather than leak a malformed-but-real credential
            # URL. A cheap substring check against the raw match is
            # acceptable ONLY in this can't-parse fallback; the normal
            # path above always uses the real domain-boundary match.
            low = url.lower()
            return "<redacted-url>" if any(h in low for h in needles) else url
        matched = any(host == h or host.endswith("." + h) for h in needles)
        return "<redacted-url>" if matched else url

    out = []
    pos = 0
    for match in _WIRE_URL_RE.finditer(text):
        out.append(_bare_host_sub(text[pos:match.start()], needles))
        out.append(_sub(match))
        pos = match.end()
    out.append(_bare_host_sub(text[pos:], needles))
    return "".join(out)


def _safe_reason(exc, text=None):
    """Exception text, with any embedded URL masked.

    NOTE: this is `://`-gated, not exhaustive — a schemeless URL (no
    `://`) would sail through unmasked. Callers that might see a raw
    schemeless target in exception text must not route it through here;
    see the target-scheme validation in each `_*_request` builder and
    the build-failure branch in `send()`, which reports only
    `type(exc).__name__` for exactly this reason.

    `text`, when given, is used INSTEAD of `str(exc)` -- the caller in
    `send()` runs `_mask_request_target`/`_mask_request_header_values` on the
    raw, untruncated exception text FIRST (see those functions' docstrings
    for why order matters), then hands the result in here as a fallback pass
    for any leftover `://` text those exact-match maskers did not cover.
    """
    text = str(exc) if text is None else text
    if "://" in text:
        text = _URL_RE.sub(lambda m: mask_target(m.group(0)), text)
    return text


class _InvalidTarget:
    """Sentinel returned by a builder when the configured target URL has
    no http/https scheme. Distinct from `None` (= "not configured") so
    `send()` can report a channel-name-only message without ever touching
    the URL/exception text (a schemeless target has no "://" for
    `_safe_reason` to mask, so `urllib.request.Request` raising on it
    must never reach `str(exc)`)."""


_INVALID_TARGET = _InvalidTarget()


class _Unresolved:
    """Sentinel returned by a builder when the target could not be resolved,
    carrying WHICH condition applies.

    Distinct from `None` ("not configured") and `_INVALID_TARGET`
    ("configured but unpostable"). The reason must always be a fixed literal,
    never a string built from a configuration value, because it travels into
    the delivery failure detail, which reaches state.json and the readout
    files with no masking applied."""

    def __init__(self, reason):
        self.reason = reason


# Below this length a header value reads as an ordinary operational flag (a
# priority level, an environment code) rather than a credential -- measured
# 2026-08-24: an Integration row carrying `X-Priority: 1` / `X-Env: e` made
# an UNRELATED connection failure (Errno text, containing "r", "n", "d" as
# single characters shared with those values) render as a string of
# <redacted> tokens with the actual cause destroyed. The failure detail is
# the only surface naming why a send failed, so masking must not be allowed
# to consume it on values this short.
#
# This BOUNDS the shredding of an unrelated failure message; it does not
# eliminate the risk of a leak. A secret shorter than the floor, or a header
# value of eight characters or more that happens to be an ordinary English
# word, is deliberately left unmasked and will still consume that word from
# the detail text if it is present -- both are accepted trade-offs of buying
# back a diagnosable failure message, not gaps a future reader should treat
# as closed.
_MIN_MASKED_HEADER_VALUE_LEN = 8

# The channel's OWN headers (`_headers()` always sets both), never a value
# from a row the plugin does not own -- these are never secret and must
# never be candidates for masking regardless of length.
_OWN_HEADER_NAMES = frozenset({"content-type", "user-agent"})


def _mask_request_header_values(text, request):
    """Replace any header VALUE carried by `request` that appears verbatim in
    `text`, with `<redacted>` -- except the channel's own `Content-Type` and
    `User-Agent` (see `_OWN_HEADER_NAMES`), and any value shorter than
    `_MIN_MASKED_HEADER_VALUE_LEN`.

    `_safe_reason` is gated on `"://"`, which never fires for a message such
    as `ValueError("Invalid header value b'Bearer S3CR3T'")` -- that text
    carries no URL at all. The connect channel is the only one whose headers
    can come from a row the plugin does not own (an Integration's own extra
    headers, resolved by `collector.inject_derived`), so it is the only
    channel that can put a genuine credential into an otherwise-inert header
    value; this runs for every channel's send() failure uniformly rather than
    special-casing one name.
    """
    try:
        for name, value in request.headers.items():
            if name.lower() in _OWN_HEADER_NAMES:
                continue
            value = str(value)
            if len(value) < _MIN_MASKED_HEADER_VALUE_LEN:
                continue
            if value in text:
                text = text.replace(value, "<redacted>")
    except Exception:
        pass
    return text


def _mask_request_target(text, request):
    """Replace the request's own full URL, verbatim, with `scheme://host` (no
    userinfo, no path) if it appears in `text`, and replace the address's own
    PASSWORD wherever it appears whether the full URL matched or not.

    The password pass is defence in depth, and it exists because the full-URL
    match is an exact one. Measured 2026-08-24 on
    `https://user:PA55W0RD@host.invalid/hooks/HOOKTOK9`: `urllib.request`
    leaves the userinfo on the request host, so `http.client` splits that host
    on its first colon, reads the password as a port and raises `InvalidURL`
    with a message quoting `PA55W0RD@host.invalid` and NOTHING ELSE of the
    address. The full URL is absent from that text, so the exact match never
    fires, and `_safe_reason` is gated on `://`, which that text also lacks.
    `dispatcharr._has_userinfo` now refuses such an address outright, which is
    the primary fix; this layer means a password that reaches here by some
    other route still does not reach the failure detail.

    Reading `.password` is safe where reading `.port` is not: it splits the
    userinfo and never touches the port, so the non-numeric-port `ValueError`
    that this function already fails closed on cannot come from it.

    Called on the RAW, UNTRUNCATED exception text -- i.e. BEFORE
    `_safe_reason` has had a chance to run its own `"://"`-gated
    `mask_target` pass -- because `mask_target` truncates the path down to
    its first segment, and an exact-string match against `request.full_url`
    then only succeeds by coincidence (a one-path-segment URL, where the
    truncated form happens to still start with the original literal).
    Measured 2026-08-24: a two-segment path (`/hooks/HOOKTOK9`) broke that
    coincidence and the password in the URL's userinfo, which `mask_target`
    never strips at all, leaked in full.

    Unlike `mask_target`, this drops the ENTIRE path and any userinfo rather
    than keeping the first path segment: `mask_target`'s contract assumes an
    identifying prefix and a secret suffix (Discord's
    `/api/webhooks/<id>/<token>` shape), which does not hold for a Connect
    Integration's resolved URL -- it may have no structure beyond a single
    opaque path segment that IS the credential. Scheme and host are kept so
    the failure detail still names which endpoint failed, matching this
    codebase's rule that a channel's OWN target host is diagnostic
    information, never a secret.

    Fails CLOSED on a parse error, the same convention `redact_hosts` follows
    elsewhere in this file. Reading `.port` raises `ValueError` for a
    non-numeric port -- an operator typo Dispatcharr never validates
    (`dispatcharr.py` documents that it does not validate the Integration
    URL, and `connect_status` checks only the scheme, via `safe_url`). A
    failure while deriving the replacement must still remove the address:
    it falls back to the blanket `<redacted-url>` placeholder rather than
    returning `text` unmasked, which would leave the raw url -- userinfo,
    password included -- sitting in the returned text. Measured 2026-08-24:
    before this fallback existed, a bad port made
    `urllib.parse.urlsplit(url).port` raise, this function returned `text`
    unchanged, and `_safe_reason`'s own fallback mask kept the netloc
    (including the password) verbatim.
    """
    try:
        url = request.full_url
    except Exception:
        return text
    if not url:
        return text

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        parsed = None

    # The full-URL pass runs FIRST. Replacing the password first would break
    # the exact match this pass depends on, leaving the rest of the address --
    # its path, which on a Connect Integration may itself BE the credential --
    # sitting in the text unmasked. Measured 2026-08-24 against
    # `test_connect_detail_never_carries_the_url_or_a_header_value`.
    if url in text:
        replacement = "<redacted-url>"
        if parsed is not None:
            try:
                host = parsed.hostname or ""
                port = f":{parsed.port}" if parsed.port else ""
                if host:
                    replacement = f"{parsed.scheme}://{host}{port}"
            except Exception:
                replacement = "<redacted-url>"
        text = text.replace(url, replacement)

    if parsed is not None:
        try:
            password = parsed.password
        except Exception:
            password = None
        if password and password in text:
            text = text.replace(password, "<redacted>")
    return text


def _headers(version, extra=None):
    headers = {"Content-Type": "application/json",
               "User-Agent": f"{USER_AGENT_PREFIX}{version}"}
    if extra:
        headers.update(extra)
    return headers


def _strip_control_chars(text):
    """Drop Unicode category Cc (control) chars; tabs/newlines become a
    single space instead of vanishing. Non-latin-1 printable glyphs (▎,
    ᴿᴬᵂ, category So/Lm/etc) are untouched — this is NOT an ASCII filter."""
    out = []
    for c in text or "":
        if c in ("\t", "\n", "\r"):
            out.append(" ")
        elif unicodedata.category(c) == "Cc":
            continue
        else:
            out.append(c)
    return "".join(out)


def connect_status(settings):
    """`{"state", "reason", "available", "stale", "unpostable"}` for the
    connect channel. Pure: reads only the settings dict, never a database.

    `state` is one of:
      "off"     the operator named no Integration; the channel is disabled
      "unknown" a name is set but NO resolution has been recorded, so this
                function cannot tell. It is a distinct value, never folded
                into "ok", because a predicate that cannot tell and answers
                "healthy" is the shape this workspace has shipped four times.
      "ok"      a postable target is available (possibly a stale one, which
                still delivers)
      "broken"  a resolution was recorded and failed, or the resolved URL is
                not postable

    Membership is tested with `in`, never `dict.get`, which cannot tell an
    absent key from a present-but-None one.

    BOTH resolved values are type-guarded, mirroring the guard on the
    consuming side in `plugin.py`. `deliver.tick` calls this function BEFORE
    the branch that fails closed when no channel is enabled, and does not wrap
    the call, so a non-dictionary under either key would raise there and stop
    delivery on every channel rather than on this one. Nothing writes such a
    value today, because `collector.inject_derived` overwrites both keys
    unconditionally before its own early return, but that ordering must not be
    what keeps the other six channels delivering.
    """
    name = str(settings.get("connect_integration") or "").strip()
    out = {"state": "off", "reason": None, "available": [], "stale": False,
           "unpostable": False}
    if not name:
        return out
    if "_resolved_connect_info" not in settings:
        out["state"] = "unknown"
        out["reason"] = ("the Integration has not been resolved yet; "
                         "Dispatcharr could not be read")
        return out
    info = settings.get("_resolved_connect_info")
    info = info if isinstance(info, dict) else {}
    out["available"] = list(info.get("available") or [])
    out["stale"] = bool(info.get("stale"))
    target = settings.get("_resolved_connect_target")
    if not isinstance(target, dict) or not target:
        out["state"] = "broken"
        out["reason"] = info.get("reason") or (
            "the Integration could not be read from Dispatcharr")
        return out
    if safe_url(str(target.get("url") or "")) is None:
        out["state"] = "broken"
        out["unpostable"] = True
        out["reason"] = ("the Integration's URL does not start with http:// "
                         "or https://, so every send will fail")
        return out
    out["state"] = "ok"
    return out


def enabled_channels(settings):
    """Channels whose URL/server setting is non-empty, in KNOWN order.

    `smtp` is the one channel that needs TWO settings: a server AND at least
    one VALID recipient. A server with no deliverable recipient is not a
    working channel, and reporting it enabled would make show_status claim a
    healthy fanout that silently sends nowhere.
    """
    checks = {"discord": "discord_webhook_url", "webhook": "webhook_url",
              "apprise": "apprise_url"}
    out = [name for name in KNOWN
           if name in checks and (settings.get(checks[name]) or "").strip()]
    # ntfy is the SECOND two-setting channel and did not apply the rule above:
    # counted on `ntfy_server` alone, an empty topic produced an HTTP 400 per
    # event -- which never says "no topic" -- while show_status reported a
    # healthy fanout. Same dishonesty the smtp rule exists to prevent.
    if ntfy_configured(settings):
        out.append("ntfy")
    if smtp_configured(settings):
        out.append("smtp")
    # `ticker` needs no URL -- it writes a local file -- so its "configured"
    # test is simply whether the operator switched it on. Without this a
    # ticker-only setup would trip deliver.py's fail-closed no-channels guard
    # and nothing would ever be claimed.
    if settings.get("ticker_enabled"):
        out.append("ticker")
    # Counted on the NAME alone, deliberately unlike the smtp and ntfy rules
    # above. Excluding an unresolvable connect would filter it out of the
    # routed list, so an event routed only here would be recorded as reaching
    # nothing rather than entering the retry ladder. The honesty those rules
    # protect is provided by unusable_channels and by the status action, both
    # of which report the condition in red.
    if str(settings.get("connect_integration") or "").strip():
        out.append("connect")
    return [name for name in KNOWN if name in out]


_URL_SETTINGS = (("discord", "discord_webhook_url"),
                 ("webhook", "webhook_url"),
                 ("apprise", "apprise_url"),
                 ("ntfy", "ntfy_server"))


def unusable_channels(settings):
    """[(channel, reason)] for channels that are CONFIGURED but cannot address
    anything -- in KNOWN order. [] when clean.

    `enabled_channels` counts an HTTP channel on non-blankness alone, but every
    builder rejects a target failing `safe_url` at SEND time
    (`_INVALID_TARGET`). So `ntfy.sh` with no scheme -- which the ntfy field's
    own help text writes as a bare example -- validates green, is counted as an
    enabled channel, is shown as a live route, and delivers NOTHING, forever:
    the retry ladder just walks to `gave_up` on every event.

    Uses `safe_url`, the SAME predicate the builders use, so config-time and
    send-time cannot drift apart.

    Deliberately silent about a BLANK setting (that is a disabled channel, not
    a broken one -- reporting it would be a false alarm) and about `smtp` and
    `ticker`, which address nothing by URL.
    """
    out = []
    for name, key in _URL_SETTINGS:
        raw = (settings.get(key) or "").strip()
        if not raw:
            continue
        if safe_url(raw) is None:
            out.append((name, f"{key} is not an http(s) URL; delivery will "
                              f"fail on every event until it starts with "
                              f"http:// or https://"))
    status = connect_status(settings)
    if status["state"] in ("broken", "unknown"):
        reason = status["reason"]
        if status["available"]:
            reason = (f"{reason}. Webhook Integrations that do exist: "
                      f"{', '.join(status['available'])}")
        out.append(("connect", reason))
    return [(n, r) for n, r in out if n in KNOWN]


def ntfy_configured(settings):
    """True only when a send could actually address a topic.

    Server AND topic, for the reason spelled out in `smtp_configured`: a server
    with no destination is not a working channel.
    """
    return bool((settings.get("ntfy_server") or "").strip()
                and (settings.get("ntfy_topic") or "").strip())


def smtp_configured(settings):
    """True only when the transport would actually attempt a send.

    A config the transport refuses (plaintext AUTH, no From address) must
    NOT count as enabled: show_status would otherwise report a healthy
    fanout for a channel that silently sends nowhere, which is the exact
    dishonesty the two-setting rule exists to prevent.
    """
    host, port = parse_server(settings.get("smtp_server"))
    if host is None:
        return False
    render = _email_render()
    valid, _ = render.parse_recipients(settings.get("smtp_to", ""))
    if not valid:
        return False
    username = (settings.get("smtp_username") or "").strip()
    security, _ = resolve_security(settings, port)
    if security == "none" and username:
        return False
    from_addr = (render.validate_address(settings.get("smtp_from") or "")
                 or render.validate_address(username))
    return bool(from_addr)


def _discord_request(event, settings, version):
    target = (settings.get("discord_webhook_url") or "").strip()
    if not target:
        return None
    if safe_url(target) is None:
        return _INVALID_TARGET
    title = event.get("title") or ""
    body = event.get("body") or ""
    link = safe_url(event.get("url"))

    # Budget computed PER EVENT, never a constant. The composed overhead is
    # variable -- notify_client caps titles at 1024 -- so a fixed reserve is
    # wrong by up to 5x: measured, a 139-char title plus a typical URL was
    # already enough to push the composed content to exactly 2000 and drop the
    # URL silently; at 200 the marker itself was chopped mid-string; at 400 it
    # vanished and the hard cap fired exactly as before the fix.
    overhead = len(f"**{title}**\n") + (len(f"\n<{link}>") if link else 0)
    body = fit_body(body, DISCORD_LIMIT - overhead)

    content = f"**{title}**\n{body}"
    if link:
        content += f"\n<{link}>"                # angle brackets suppress embed unfurl
    content = content.replace("\r", "")
    content = content[:DISCORD_LIMIT]           # final safety net (title alone > limit)
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(target, data=data, method="POST",
                                   headers=_headers(version))


def _webhook_request(event, settings, version):
    target = (settings.get("webhook_url") or "").strip()
    if not target:
        return None
    if safe_url(target) is None:
        return _INVALID_TARGET
    payload = {"plugin": "newsflasharr",
               "source": event.get("source"), "event": event.get("event"),
               "severity": event.get("severity"), "kind": event.get("kind"),
               "title": event.get("title"), "body": event.get("body"),
               "url": event.get("url"), "ts": event.get("ts")}
    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(target, data=data, method="POST",
                                   headers=_headers(version))


def _connect_request(event, settings, version):
    """POST to the Integration resolved by `collector.inject_derived`. The
    payload is byte-identical to the generic webhook channel's, so a receiver
    already handling Newsflasharr webhooks needs no change.

    Trusts that `target.get("headers")` never carries `Content-Type`,
    `User-Agent`, `Host`, `Content-Length` or `Transfer-Encoding` (in any
    case): `dispatcharr._clean_connect_headers` drops all five before a
    resolved target is ever written into settings (see
    `_CONNECT_OWNED_HEADERS` there, and
    `test_read_connect_target_drops_headers_it_must_own` /
    `test_connect_builder_trusts_the_resolver_to_have_stripped_owned_headers`
    in tests/test_dispatcharr.py, which prove the chain end to end). This
    builder and `_headers()` below do NOT re-check, so a caller feeding a
    hand-built target straight to `_connect_request` -- bypassing the
    resolver -- CAN override those headers; no production settings dict can
    do so.
    """
    status = connect_status(settings)
    if status["state"] == "off":
        return None
    if status["state"] == "unknown":
        return _Unresolved(status["reason"])
    if status["state"] == "broken":
        if status["unpostable"]:
            return _INVALID_TARGET
        return _Unresolved(status["reason"])
    target = settings.get("_resolved_connect_target") or {}
    url = safe_url(str(target.get("url") or ""))
    if url is None:                      # unreachable via connect_status; kept
        return _INVALID_TARGET           # so this function is safe alone
    payload = {"plugin": "newsflasharr",
               "source": event.get("source"), "event": event.get("event"),
               "severity": event.get("severity"), "kind": event.get("kind"),
               "title": event.get("title"), "body": event.get("body"),
               "url": event.get("url"), "ts": event.get("ts")}
    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(url, data=data, method="POST",
                                  headers=_headers(version,
                                                   dict(target.get("headers")
                                                        or {})))


def _ntfy_request(event, settings, version):
    server = (settings.get("ntfy_server") or "").strip().rstrip("/")
    if not server:
        return None
    if safe_url(server) is None:
        return _INVALID_TARGET
    priority = {"critical": 5, "warning": 4}.get(event.get("severity"), 3)
    title = _strip_control_chars(event.get("title") or "")
    # BYTES, not characters: this box's channel names carry U+258E and the
    # superscript RAW glyphs at 3 bytes each -- which is why this builder uses
    # JSON publish rather than headers in the first place. 4000 characters of
    # those is ~12,000 bytes, three times the server's limit.
    title = fit_body(title, NTFY_LIMIT_BYTES // 4, unit="bytes")
    message = fit_body(event.get("body") or "", NTFY_LIMIT_BYTES, unit="bytes")
    payload = {"topic": settings.get("ntfy_topic") or "",
               "title": title,
               "message": message,
               "priority": priority}
    link = safe_url(event.get("url"))
    if link:
        payload["click"] = link
    data = json.dumps(payload).encode("utf-8")
    token = (settings.get("ntfy_token") or "").strip()
    extra = {"Authorization": f"Bearer {token}"} if token else None
    return urllib.request.Request(server, data=data, method="POST",
                                   headers=_headers(version, extra))


def _apprise_request(event, settings, version):
    target = (settings.get("apprise_url") or "").strip()
    if not target:
        return None
    if safe_url(target) is None:
        return _INVALID_TARGET
    body = event.get("body") or ""
    link = safe_url(event.get("url"))
    if link:
        body = f"{body}\n{link}"
    title = event.get("title") or ""
    if not body.strip():
        # The Apprise gateway REJECTS a payload with an empty body: it answers
        # HTTP 400 "Payload lacks minimum requirements". The retry ladder is
        # powerless against it, because every attempt sends byte-identical
        # data, so the event burns all three attempts and is given up on.
        #
        # This is not hypothetical. Repeat-collapse digests ("N more in the
        # last 10 min") are constructed with an empty body by design, since
        # their title already carries everything they know. On the live
        # installation two of them were rejected ten times between them on
        # 2026-08-01 and lost. Requiring a body is a fact about Apprise, so it
        # is handled here rather than by giving every digest a body that would
        # merely restate its title on every other channel.
        body = title or "(no details)"
    payload = {"title": title, "body": body,
               "type": {"critical": "failure",
                        "warning": "warning"}.get(event.get("severity"), "info")}
    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(target, data=data, method="POST",
                                   headers=_headers(version))


_BUILDERS = {"discord": _discord_request, "webhook": _webhook_request,
             "connect": _connect_request,
             "ntfy": _ntfy_request, "apprise": _apprise_request}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect. Returning None makes urllib raise HTTPError
    carrying the 3xx code, which send() renders as `connect: HTTP <code>`.

    Python's redirect handler rebuilds the request stripping only
    content-length and content-type, so an Authorization header taken from the
    Integration row would be forwarded to whatever host the redirect names,
    and the send would report success. This is the only channel whose
    credential comes from a row the plugin does not own."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_CONNECT_OPENER = None


def _connect_opener():
    """Built lazily. build_opener at import time would run on Dispatcharr's
    per-request hot path through the plugin's constructor."""
    global _CONNECT_OPENER
    if _CONNECT_OPENER is None:
        _CONNECT_OPENER = urllib.request.build_opener(_NoRedirect)
    return _CONNECT_OPENER


# -- smtp ---------------------------------------------------------------------

_SECURITY_BY_PORT = {465: "ssl", 587: "starttls", 25: "starttls", 2525: "starttls"}
# Post-resolution vocabulary: "auto" is always resolved away by
# resolve_security, so anything outside this set is a typo, not a mode.
_KNOWN_SECURITY = ("starttls", "ssl", "none")
DEFAULT_SMTP_PORT = 587
SMTP_TIMEOUT_S = 5.0


def parse_server(raw):
    """`host` or `host:port` (IPv6 literals bracketed) -> (host, port).

    Returns (None, None) when unusable.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    if text.startswith("["):                       # [2001:db8::1]:587
        close = text.find("]")
        if close < 0:
            return None, None
        host = text[1:close]
        rest = text[close + 1:]
        port_part = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") == 1:
        host, _, port_part = text.partition(":")
    else:                                          # bare host, or bare IPv6
        host, port_part = text, ""
    if not host:
        return None, None
    if not port_part:
        return host, DEFAULT_SMTP_PORT
    try:
        port = int(port_part)
    except (TypeError, ValueError):
        return None, None
    if not 1 <= port <= 65535:
        return None, None
    return host, port


def resolve_security(settings, port):
    """`auto` -> a concrete mode, inferred from the port.

    Inference is safe ONLY because validate_settings prints the resolution --
    the wrong security mode presents as a HANG rather than an error, which is
    the worst diagnostic signature available, so it must never be silent.
    """
    mode = (settings.get("smtp_security") or "auto").strip().lower()
    if mode == "auto":
        return _SECURITY_BY_PORT.get(port, "starttls"), True
    return mode, False


def _smtp_transport(event, settings, version, factory=None, timeout=None,
                    tick_state=None, attachment=None):
    """Send `event` as email. Never raises; returns (ok, detail).

    `detail` never contains the host, the credentials, the recipient
    addresses, or `str(exc)` -- see the exception table below.
    """
    import smtplib

    timeout = SMTP_TIMEOUT_S if timeout is None else timeout
    render = _email_render()

    host, port = parse_server(settings.get("smtp_server"))
    to_addrs, _invalid = render.parse_recipients(settings.get("smtp_to", ""))
    if not host or not to_addrs:
        return False, "smtp: not configured"

    security, _inferred = resolve_security(settings, port)
    username = (settings.get("smtp_username") or "").strip()
    password = settings.get("smtp_password") or ""
    from_addr = render.validate_address(settings.get("smtp_from") or "") \
        or render.validate_address(username) or ""
    if not from_addr:
        return False, "smtp: not configured (no valid From address)"

    # Plaintext AUTH is recoverable from any packet capture, and it would be
    # re-sent on every tick. validate_settings reports this as an error too;
    # this is the runtime backstop so it can never happen by configuration
    # drift alone.
    if security == "none" and username:
        return False, "smtp: refusing plaintext AUTH (set smtp_security)"

    # An UNRECOGNISED mode is behaviourally identical to "none" and used to
    # slip past the check above: `factory` falls back to plain smtplib.SMTP,
    # the starttls block is skipped, and login() then puts base64 credentials
    # on an unencrypted socket -- returning ok=True. The comment above once
    # called that check "the runtime backstop so it can never happen by
    # configuration drift alone"; it only ever covered the literal "none".
    # Refuse instead of guessing: a wrong guess is a silent credential leak,
    # and refusing costs one channel while validate names the fix.
    if security not in _KNOWN_SECURITY:
        return False, f"smtp: unknown security mode {security!r} (set smtp_security)"

    if tick_state is not None and tick_state.get("smtp_connect_failed"):
        return False, "smtp: skipped (connect failed earlier this tick)"

    try:
        message = render.build_message(event, from_addr, to_addrs,
                                       attachment=attachment,
                                       stamp=local_stamp(event, settings))
    except Exception as exc:
        # deliver.py has no try around its send call, so a render error
        # escaping here would kill the whole collector tick -- every channel,
        # not just email.
        return False, f"smtp: render failed ({type(exc).__name__})"

    import ssl
    context = ssl.create_default_context()

    if factory is None:
        factory = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP

    conn = None
    try:
        try:
            if security == "ssl" and factory is smtplib.SMTP_SSL:
                conn = factory(host, port, timeout=timeout, context=context)
            else:
                conn = factory(host, port, timeout=timeout)
        except Exception as exc:
            if tick_state is not None:
                tick_state["smtp_connect_failed"] = True
            return False, _smtp_detail(exc, stage="connect")

        if security == "starttls":
            conn.ehlo()
            # Deliberately NOT wrapped in a defensive try/except: starttls()
            # raises SMTPNotSupportedError when the server does not advertise
            # the extension, which is a downgrade attempt or a
            # misconfiguration. Swallowing it here would let login() run on a
            # plaintext socket -- unconditional credential disclosure, one
            # reflexive `except` away.
            conn.starttls(context=context)
            conn.ehlo()

        if username:
            conn.login(username, password)

        refused = conn.send_message(message) or {}
        if refused:
            # send_message RETURNS refused recipients and only RAISES when
            # ALL of them fail -- ignoring this silently drops part of the
            # fanout while reporting success. Report by INDEX, never address.
            bad = sorted(to_addrs.index(a) + 1 for a in refused if a in to_addrs)
            return False, (f"smtp: {len(refused)}/{len(to_addrs)} recipients "
                           f"rejected (smtp_to entr{'y' if len(bad) == 1 else 'ies'} "
                           f"{', '.join(str(b) for b in bad) or '?'})")
        return True, f"smtp: sent to {len(to_addrs)} recipient(s)"
    except Exception as exc:
        return False, _smtp_detail(exc)
    finally:
        if conn is not None:
            # quit() calls docmd("quit") BEFORE close(), so a raising quit
            # skips its own close and leaks a file descriptor every tick
            # against a flaky server.
            try:
                conn.quit()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


def _smtp_detail(exc, stage=None):
    """`smtp: <ExceptionType> (code NNN)` -- never `str(exc)`.

    `str()` on these leaks material the ledger must never carry:
      SMTPRecipientsRefused   the full dict of every recipient address
      SMTPSenderRefused       the sender address, as a third arg
      SMTPAuthenticationError the server's echo, often the username
      SMTPConnectError        the server hostname, from the banner
      ValueError from putcmd  the full recipient address

    The type name is used rather than the base class because
    SMTPConnectError, SMTPAuthenticationError, SMTPSenderRefused,
    SMTPHeloError and SMTPDataError are ALL SMTPResponseException subclasses:
    keying on the base would render a wrong password and an unreachable
    server identically, from the one channel whose failure mode is "silently
    sends nowhere".
    """
    import smtplib
    name = type(exc).__name__
    prefix = f"smtp: {name}"
    if stage:
        prefix = f"smtp: {stage} failed ({name})"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "smtp: STARTTLS unavailable (server did not advertise it)"
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return f"{prefix} (code {code})"
    return prefix


_TRANSPORTS = {"smtp": _smtp_transport}

DEFAULT_HTTP_TIMEOUT_S = 10


def send(name, event, settings, version, opener=None, timeout=None,
         smtp_factory=None, tick_state=None, attachment=None):
    """Deliver `event` to channel `name`. Never raises. Returns (ok, detail);
    detail never contains the target URL.

    `timeout=None` means "each transport's own default" (HTTP channels
    `DEFAULT_HTTP_TIMEOUT_S`, smtp `SMTP_TIMEOUT_S`). The old default of a
    literal 10 doubled as the "unset" sentinel, so a caller explicitly
    passing `timeout=10` to smtp silently got 5s instead.

    Non-HTTP channels are dispatched from `_TRANSPORTS` FIRST, leaving the
    urllib builder path below byte-identical for the four HTTP channels.
    `smtp_factory` is a test seam (see conftest.FakeSMTP); production passes
    None and the transport selects smtplib.SMTP / SMTP_SSL itself.

    `attachment` is a 4-tuple `(filename, maintype, subtype, bytes)` or None,
    forwarded ONLY to `_TRANSPORTS["smtp"]` -- the four HTTP builders below
    never see it, so it can never leak onto a Discord/webhook/ntfy/Apprise
    payload.
    """
    transport = _TRANSPORTS.get(name)
    if transport is not None:
        try:
            return transport(event, settings, version, factory=smtp_factory,
                             timeout=timeout,
                             tick_state=tick_state, attachment=attachment)
        except Exception as exc:            # never raises to the caller
            return False, f"{name}: {type(exc).__name__}"

    builder = _BUILDERS.get(name)
    if builder is None:
        return False, f"{name}: unknown channel"

    try:
        request = builder(event, settings, version)
    except Exception as exc:                       # never raises to the caller
        # Report the exception TYPE only, never str(exc) — a schemeless
        # target URL (no "://") can reach urllib.request.Request and raise
        # ValueError with the raw credential-bearing URL as its message,
        # which _safe_reason's "://"-gated masking would NOT catch.
        return False, f"{name}: build failed ({type(exc).__name__})"
    if request is None:
        return False, f"{name}: not configured"
    if request is _INVALID_TARGET:
        return False, f"{name}: invalid target URL"
    if isinstance(request, _Unresolved):
        return False, f"{name}: {request.reason}"
    if not isinstance(request, urllib.request.Request):
        return False, f"{name}: build failed (unrecognised request)"

    send_fn = opener or (_connect_opener().open if name == "connect"
                         else urllib.request.urlopen)
    if timeout is None:
        timeout = DEFAULT_HTTP_TIMEOUT_S
    try:
        with send_fn(request, timeout=timeout) as response:
            status = getattr(response, "status", 0)
        return True, f"{name}: HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"{name}: HTTP {exc.code}"
    except Exception as exc:                        # never raises to the caller
        # Order matters (see _mask_request_target's docstring): mask on the
        # RAW text first, so the exact-match premise holds regardless of path
        # shape, then let _safe_reason's regex pass catch anything left over.
        pre_masked = _mask_request_header_values(
            _mask_request_target(str(exc), request), request)
        reason = _safe_reason(exc, text=pre_masked)
        return False, f"{name}: {type(exc).__name__} {reason}".strip()
