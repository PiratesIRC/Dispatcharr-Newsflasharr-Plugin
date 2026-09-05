"""Reads Dispatcharr's OWN configuration: system timezone, provider hostnames,
and Connect Integrations.

Two settings used to be typed by hand into Newsflasharr's UI and are now derived
from Dispatcharr itself:

  - the quiet-hours timezone, which is just Dispatcharr's system timezone
    (Settings > System). Nobody wants to keep two timezones in sync, and a
    typo'd-but-valid zone (`America/Chihuahua` for `America/Chicago`) is
    silently wrong by an hour forever.
  - the provider hostnames scrubbed from outbound text, which are derivable
    from the EPG sources and M3U accounts Dispatcharr already knows about.

The derived host list is ADDITIVE, never a replacement: `redact_hostnames`
survives as a user field and `collector.build_wire_redact` unions the two.
Derivation covers only what Dispatcharr knows -- it cannot know a webhook
target, a LAN IP, a reverse proxy, or a hostname some caller plugin puts in a
notification body, and there must always be a way to add one WITHOUT a code
change and a redeploy.

NOTHING here ever raises. A failed read degrades to the last good cached value
(or an empty list) and reports that fact through the returned info dict, which
`validate_settings` / `show_status` surface. It deliberately does NOT fail
closed: hostname redaction is the THIRD of three redaction layers (client-side
at spool-write, Store at rest, then the wire) and the other two are unaffected
by a DB hiccup here. Gating all delivery on this would trade a partial,
hypothetical leak for a certain, total outage -- and worse, a stalled tick
lets the spool age past `max_event_age_h`, so the backlog would be deleted
wholesale as `expired` the moment the query recovered.

Stdlib only at import time. Django (`core.models`, `apps.epg.models`,
`apps.m3u.models`) is imported lazily INSIDE functions so this module stays
standalone-loadable by `conftest.load_mod` with no Django present -- the same
contract as `collector.read_settings()`.
"""
from __future__ import annotations

import re
import sys
import threading
import time
import types
import urllib.parse
import zoneinfo

RUNTIME_KEY = "_newsflasharr_runtime"
HOST_CACHE_ATTR = "notifyarr_host_cache"
TZ_CACHE_ATTR = "notifyarr_tz_cache"
CONNECT_CACHE_ATTR = "notifyarr_connect_cache"

HOST_TTL_S = 300.0
TZ_TTL_S = 300.0
CONNECT_TTL_S = 300.0

# How long a NO-MATCH answer is held. Deliberately far shorter than
# CONNECT_TTL_S: a name matching nothing is usually a typo, but it is also the
# state that exists for a few seconds while the operator is creating the row,
# and reporting a row you just made as missing for five minutes reads as a
# broken plugin.
#
# It used to be zero, meaning not cached at all, which made a typo re-query
# every Integration row on EVERY collector tick, forever, from inside the
# gevent worker holding the collector lease. Measured at the default 5s poll:
# a matching name cost 1 query per 12 ticks, a typo cost 12. Thirty seconds
# brings that to 2 per minute while keeping the create-then-check loop usable,
# and Validate is unaffected either way because it forces a refresh.
CONNECT_MISS_TTL_S = 30.0

FALLBACK_TZ = "UTC"

# -- hostname extraction ------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# A TLD is a run of letters preceded by a dot and NOT followed by another
# name char. The negative lookahead is what lets `provider.tv-alt1` yield
# `provider.tv` (the `-` ends the TLD) while `edge-23.example.net` still yields the
# full host via the `.com` anchor. Matching then walking LEFT (rather than one
# greedy pattern) is what keeps a trailing sentence period -- `provider.tv.` --
# from silently yielding nothing, which is a fail-OPEN miss.
_TLD_ANCHOR = re.compile(r"\.([a-z]{2,})(?![a-z0-9])", re.IGNORECASE)
_LABELS_LEFT = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+$", re.IGNORECASE)

# Conservative allowlist, NOT the full IANA list. `.live`, `.stream`, `.news`,
# `.movie`, `.sport`, `.show` and friends are all delegated gTLDs *and* are
# exactly the vocabulary of EPG/M3U source names, so the full list would
# maximize false positives in this specific domain.
_TLD_ALLOW = frozenset("""
com net org edu gov int tv co io me info biz online site live stream
xyz club pro app dev shop store space website tech link one now
uk us ca au de fr es it nl se dk fi pl ru br mx ar cl pe ve
in jp kr cn hk tw sg my th vn ph id nz za ng ke gh ma eg
ie pt gr tr ua ro bg hr si sk cz hu at ch be lu no is li
il ae sa qa kw pk bd lk np ir
guru pw nz
""".split())

# TLDs that are ALSO common file extensions. `.sh` (Saint Helena), `.ts`
# (Tuvalu-adjacent in practice a TypeScript/transport-stream file), `.zip`,
# `.mov`, `.md`, `.cc`, `.py`, `.pl`, `.so`, `.gz` are all real delegations,
# so an allowlist ALONE lets `run.sh` and `segment.ts` through as "hostnames".
# Belt and braces: most of these are already absent from the allowlist above,
# but this layer keeps a future allowlist edit from silently reopening the
# hole.
_EXT_DENY = frozenset("""
sh ts zip mov md cc py pl so gz rs go js css html htm php log txt csv
ini cfg bak tmp jpg jpeg png gif bmp svg mp4 mp3 avi mkv webm srt vtt
exe dll sys bin dat db iso tar rar xz gzip json xml yml yaml sql m3u
""".split())

# Two-label public suffixes. Without this, `bbc.co.uk` extracts `co.uk`, and
# because `redact_hosts` matches `host.endswith("." + needle)` that single
# needle would replace EVERY .co.uk URL in every notification with
# `<redacted-url>` -- a silent, total loss of notification usefulness.
_PUBLIC_SUFFIX = frozenset("""
co.uk org.uk ac.uk gov.uk me.uk net.uk ltd.uk plc.uk
com.au net.au org.au edu.au gov.au id.au
co.nz net.nz org.nz govt.nz
co.za org.za net.za web.za
com.br net.br org.br gov.br
com.mx org.mx net.mx
co.jp or.jp ne.jp ac.jp go.jp
co.in net.in org.in gen.in firm.in
co.kr or.kr ne.kr
com.tr net.tr org.tr gov.tr
com.ar net.ar org.ar
com.cn net.cn org.cn gov.cn
com.sg net.sg org.sg
co.il org.il net.il ac.il
com.hk org.hk net.hk
com.tw net.tw org.tw
com.my net.my org.my
com.ph net.ph org.ph
com.vn net.vn org.vn
com.pk com.bd com.np com.lk
""".split())


def _scan_tokens(text):
    """Hostname-shaped tokens inside a free-text NAME (not a URL).

    This is how `provider.tv-alt1` (an M3U account name) yields `provider.tv` --
    the provider brand host appears in no `server_url` in this installation, only in
    account names, so a URL-only derivation would miss it entirely.

    Biased deliberately toward OVER-extraction: a bogus needle like
    `edge.example` (from `edge-26.example.net`) is inert, since `redact_hosts` only
    rewrites URLs whose parsed host actually matches. A MISSED host, by
    contrast, puts a provider hostname on the wire. Over-extract, never
    under-extract.
    """
    found = set()
    for match in _TLD_ANCHOR.finditer(text):
        tld = match.group(1).lower()
        if tld not in _TLD_ALLOW or tld in _EXT_DENY:
            continue
        left = _LABELS_LEFT.search(text[:match.start() + 1])
        if left is None:
            continue
        host = (left.group(0) + tld).lower()
        labels = host.split(".")
        if len(labels) < 2:
            continue
        # `123.45` / `3.5 Mbps` / a bare version number is not a host.
        if not any(ch.isalpha() for ch in labels[0]):
            continue
        if host in _PUBLIC_SUFFIX:
            continue
        found.add(host)
    return found


def extract_hosts(values):
    """Sorted, deduped, lower-cased hostnames found in `values`.

    Each value may be a URL (`http://edge-23.example.net`), a free-text name
    (`provider.tv-alt1`), or junk (`custom`, `Dummy - Local OTA`, `24/7
    Streams`). Real URLs are parsed with `urlsplit` and their host taken
    verbatim -- including bare IPs, which are legitimate hosts to redact --
    while everything left over goes through the heuristic token scan.
    """
    found = set()
    for value in values or ():
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        remainder = text
        for match in _URL_RE.finditer(text):
            try:
                host = urllib.parse.urlsplit(match.group(0)).hostname
            except ValueError:
                host = None
            if host:
                found.add(host.lower())
            remainder = remainder.replace(match.group(0), " ")
        found.update(_scan_tokens(remainder))
    return sorted(found)


# -- synthetic sys.modules runtime, which survives a plugin reload -------------

# Deliberately NOT importing collector._get_runtime: that would give this
# module a package-relative import and break standalone loading. Same shape,
# same key -- the cache rides on the same synthetic module the collector uses,
# under its own attribute so `ensure_collector`'s wholesale replacement of
# `mod.state` can't discard it.
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
            mod.lock = threading.Lock()
            sys.modules[RUNTIME_KEY] = mod
        return mod


def _cache_get(attr):
    """Read a cache slot defensively.

    An in-place upgrade over an already-running build leaves the synthetic
    runtime module in sys.modules WITHOUT these attributes (it is created once
    ever, by whichever version got there first), so plain attribute access
    would raise AttributeError on the first tick after every deploy.
    """
    return getattr(_get_runtime(), attr, None)


def _cache_set(attr, value):
    setattr(_get_runtime(), attr, value)


# -- timezone -----------------------------------------------------------------

def _query_timezone():
    """Dispatcharr's configured system timezone, or None.

    Ladder ordered newest-API-first and getattr-guarded throughout:
    `plugin.json` claims `min_dispatcharr_version: v0.20.0`, which predates
    `CoreSettings.get_system_time_zone`, so its absence is an expected state
    rather than an error. Note Django's own `settings.TIME_ZONE` is the WRONG
    answer on a normal install (it is UTC while the operator's Dispatcharr
    timezone is local) -- it is a last resort, not a preference.
    """
    try:
        from core.models import CoreSettings
    except Exception:
        CoreSettings = None

    if CoreSettings is not None:
        getter = getattr(CoreSettings, "get_system_time_zone", None)
        if getter is not None:
            try:
                value = getter()
                if value:
                    return str(value).strip()
            except Exception:
                pass

        group_getter = getattr(CoreSettings, "get_system_settings", None)
        if group_getter is not None:
            try:
                value = (group_getter() or {}).get("time_zone")
                if value:
                    return str(value).strip()
            except Exception:
                pass

        try:
            row = CoreSettings.objects.get(key="system_settings")
            value = (row.value or {}).get("time_zone")
            if value:
                return str(value).strip()
        except Exception:
            pass

    try:
        from django.conf import settings as django_settings
        value = getattr(django_settings, "TIME_ZONE", None)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return None


def read_timezone(now_fn=time.time, force_refresh=False):
    """`{"tz": str, "ok": bool, "raw": str|None, "source": str}`.

    `tz` is ALWAYS a zone `zoneinfo` accepts. Dispatcharr's timezone is still
    user-typed -- `CoreSettings.set_system_time_zone` only strips whitespace
    and accepts any string -- so the resolved value is validated HERE and a
    bad one is replaced with FALLBACK_TZ.

    That validation is why a bad zone does NOT stop quiet hours: because `tz`
    is always valid, `gates.in_quiet` never reaches its except branch, and the
    window is simply evaluated in the FALLBACK zone rather than the operator's
    own. On an America/Chicago box that is a 5-6 hour shift -- a configured
    23:00-07:00 lands at roughly 18:00-02:00 local, suppressing alerts through
    prime time and delivering them overnight. `ok=False` with the offending
    `raw` is what lets validate_settings report that.
    """
    now = now_fn()
    cached = _cache_get(TZ_CACHE_ATTR)
    if not force_refresh and cached and now - cached.get("at", 0) < TZ_TTL_S:
        return {k: v for k, v in cached.items() if k != "at"}

    raw = _query_timezone()
    if raw:
        try:
            zoneinfo.ZoneInfo(raw)
            info = {"tz": raw, "ok": True, "raw": raw, "source": "dispatcharr"}
        except Exception:
            info = {"tz": FALLBACK_TZ, "ok": False, "raw": raw,
                    "source": "fallback"}
    else:
        info = {"tz": FALLBACK_TZ, "ok": True, "raw": None, "source": "fallback"}

    _cache_set(TZ_CACHE_ATTR, dict(info, at=now))
    return info


# -- provider hostnames -------------------------------------------------------

def _query_source_values():
    """(epg_values, m3u_values) -- raw strings to mine for hostnames.

    INACTIVE rows are included on purpose: a parked M3U account's host is
    every bit as much a provider secret as a live one's, and this box keeps a
    disabled backup account precisely so it can be re-enabled later.

    Deliberately NOT reading `Stream.url`: there are five figures of those
    rows and this runs on the collector tick path. The additive
    `redact_hostnames` field covers any CDN edge that appears only there.
    """
    from apps.epg.models import EPGSource
    from apps.m3u.models import M3UAccount

    epg = []
    for row in EPGSource.objects.all():
        epg.append(getattr(row, "url", None))
        epg.append(getattr(row, "name", None))

    m3u = []
    for row in M3UAccount.objects.all():
        m3u.append(getattr(row, "server_url", None))
        m3u.append(getattr(row, "name", None))

    return epg, m3u


def read_redact_hosts(now_fn=time.time, force_refresh=False):
    """`{"hosts": [...], "epg": [...], "m3u": [...], "ok": bool,
        "stale": bool, "age_s": float|None, "error": str|None}`.

    Never raises. On a query failure the last good list is served and flagged
    `stale`; with no cache at all it degrades to an empty list and `ok=False`.
    Callers MUST surface `ok`/`stale` -- an unreported empty list is exactly
    the silent unredacted-no-op this codebase has already shipped once.

    `force_refresh=True` bypasses the TTL. `validate_settings` uses it so an
    operator who just added an M3U account and immediately clicks Validate
    sees ground truth instead of a cached list that makes derivation look
    broken.
    """
    now = now_fn()
    cached = _cache_get(HOST_CACHE_ATTR)
    if (not force_refresh and cached and cached.get("ok")
            and now - cached.get("at", 0) < HOST_TTL_S):
        return _host_info(cached, now, stale=False)

    try:
        epg_values, m3u_values = _query_source_values()
    except Exception as exc:
        if cached:
            return _host_info(cached, now, stale=True,
                              error=f"{type(exc).__name__}: {exc}")
        return {"hosts": [], "epg": [], "m3u": [], "ok": False, "stale": False,
                "age_s": None, "error": f"{type(exc).__name__}: {exc}"}

    epg_hosts = extract_hosts(epg_values)
    m3u_hosts = extract_hosts(m3u_values)
    fresh = {"at": now, "ok": True, "epg": epg_hosts, "m3u": m3u_hosts,
             "hosts": sorted(set(epg_hosts) | set(m3u_hosts))}
    _cache_set(HOST_CACHE_ATTR, fresh)
    return _host_info(fresh, now, stale=False)


def _host_info(entry, now, stale, error=None):
    return {"hosts": list(entry.get("hosts") or []),
            "epg": list(entry.get("epg") or []),
            "m3u": list(entry.get("m3u") or []),
            "ok": bool(entry.get("ok")),
            "stale": stale,
            "age_s": max(0.0, now - entry.get("at", now)),
            "error": error}


# -- Connect Integration ------------------------------------------------------

CONNECT_NO_MATCH = ("no webhook Integration on Dispatcharr's Connect page has "
                    "that name")
CONNECT_AMBIGUOUS = ("more than one Integration has that name; rename one on "
                     "Dispatcharr's Connect page")
CONNECT_DISABLED = "the Integration is switched off on Dispatcharr's Connect page"
CONNECT_NOT_WEBHOOK = ("the Integration is not a webhook Integration; this "
                       "channel does not run script Integrations")
CONNECT_NO_URL = "the Integration has no URL in its configuration"
CONNECT_URL_USERINFO = ("the Integration's URL carries a username or password "
                        "in the address; put the credential in a header on "
                        "the Integration instead")
CONNECT_BAD_HEADERS = ("the Integration's headers are not usable; they must be "
                       "plain text names and values with no line breaks")
CONNECT_LOOKUP_FAILED = "could not read the Integration list from Dispatcharr"

# Headers this channel owns. An operator value here does not customise the
# request, it breaks it or hides it:
#   - Content-Length and Transfer-Encoding frame the body, so a wrong value
#     mis-frames it against the receiver.
#   - Host sends the request, and any credential travelling with it, to a
#     different virtual host than the URL names.
#   - Content-Type would mislabel a body this channel always sends as JSON.
#   - User-Agent carries the plugin version the delivery ledger is correlated
#     against, and Discord's edge rejects an unexpected one (see the note at
#     the top of channels.py).
# Dropped rather than refused, because a header block pasted from a vendor's
# documentation often carries them harmlessly.
_CONNECT_OWNED_HEADERS = frozenset({"content-length", "transfer-encoding",
                                    "host", "content-type", "user-agent"})

# A header NAME is validated positively, against the RFC 7230 token character
# set. A negative check misses a name containing a colon, a space, or an empty
# name, all of which are storable in Dispatcharr's JSON config column and all
# of which make http.client raise at SEND time, past this channel's own error
# handling, with a message that quotes the name into the delivery ledger.
#
# MATCHED WITH fullmatch, NEVER match. Python's "$" also matches immediately
# before a TRAILING NEWLINE, so `match()` accepted "X-Evil\n" while
# http.client's own `_is_legal_header_name`, which uses fullmatch, refused it.
# Measured through a real send: the delivery detail read
# `connect: ValueError Invalid header name b'X-Evil\n'`, so the one name shape
# this positive check was written to catch was the one it let through.
_CONNECT_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

# Refused in a header VALUE. The carriage return is the dangerous one:
# http.client raises ValueError whose message quotes the value verbatim. A bare
# newline followed by a space is an obsolete line fold that http.client
# ACCEPTS, and receivers parse folds inconsistently, so it is refused here.
# Written as escapes: revision 1 embedded U+2028 and U+2029 as literal
# characters, invisible to a reviewer and destroyable by any formatter.
_CONNECT_VALUE_RE = re.compile("[\x00-\x1f\x7f\u2028\u2029]")


def _clean_connect_headers(raw):
    """`(headers, ok)`. `({}, True)` when the row carries none.

    Dispatcharr validates this value NOT AT ALL: `Integration.config` is a
    plain JSON column and its serializer checks only that `url` is a non-empty
    string, so a list, a string, or a mapping of numbers is storable.
    """
    if raw is None:
        return {}, True
    if not isinstance(raw, dict):
        return None, False
    cleaned = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None, False
        if not _CONNECT_NAME_RE.fullmatch(key):
            return None, False
        if key.lower() in _CONNECT_OWNED_HEADERS:
            continue
        if _CONNECT_VALUE_RE.search(value):
            return None, False
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            return None, False
        cleaned[key] = value
    return cleaned, True


_CONNECT_NAME_DISPLAY_MAX = 60
_CONNECT_NAMES_LISTED_MAX = 10


def _display_names(names):
    """Integration names, safe to put in a report. They come from a database
    row any Dispatcharr user with Connect access can write, so control
    characters are stripped and each name is capped; the list is deduped,
    sorted and bounded so one row cannot flood a readout."""
    out = []
    for name in sorted(set(names)):
        clean = _CONNECT_VALUE_RE.sub(" ", name).strip()
        if clean:
            out.append(clean[:_CONNECT_NAME_DISPLAY_MAX])
    return out[:_CONNECT_NAMES_LISTED_MAX]


def _query_connect_rows():
    """Every Integration row as plain dicts. Raises on any database fault; the
    caller turns that into a reason plus a degraded read."""
    from apps.connect.models import Integration

    rows = []
    for row in Integration.objects.all():
        rows.append({
            "name": str(getattr(row, "name", "") or ""),
            "type": str(getattr(row, "type", "") or ""),
            "enabled": bool(getattr(row, "enabled", False)),
            "config": getattr(row, "config", None) or {},
        })
    return rows


def _has_userinfo(url):
    """True when the address embeds a username or a password.

    Such an address is refused rather than posted to, for two reasons that
    both bite before any byte reaches the network. `urllib.request` does not
    strip the userinfo from the request host, so `http.client` splits the
    host on its first colon, reads the password as a port and raises
    `InvalidURL` quoting the password back; that text becomes the delivery
    failure detail, which lands in `state.json` and in the status readout.
    And the channel could never have worked anyway, so refusing here also
    stops `channels.connect_status` reporting the Integration ready while
    every send fails.

    Reading `.username` / `.password` is safe where reading `.port` is not:
    those two properties split the userinfo and never touch the port, so the
    non-numeric-port `ValueError` cannot come from here. A `urlsplit` that
    raises outright is left to `channels.safe_url`, which fails closed on it
    and reports the target unpostable.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        return bool(parsed.username) or bool(parsed.password)
    except Exception:
        return False


def _resolve_connect_rows(name, rows):
    """Pure. `(target, reason, available)` for `name` against `rows`."""
    available = _display_names(r["name"] for r in rows
                              if r["type"] == "webhook" and r["name"])
    wanted = name.strip().lower()
    matches = [r for r in rows if r["name"].strip().lower() == wanted]
    if not matches:
        return None, CONNECT_NO_MATCH, available
    if len(matches) > 1:
        return None, CONNECT_AMBIGUOUS, available
    row = matches[0]
    # Type before enabled: a script row that is also switched off must report
    # what it IS, or the operator switches it on and it still does not work.
    if row["type"] != "webhook":
        return None, CONNECT_NOT_WEBHOOK, available
    if not row["enabled"]:
        return None, CONNECT_DISABLED, available
    config = row["config"] if isinstance(row["config"], dict) else {}
    url = config.get("url")
    if not isinstance(url, str) or not url.strip():
        return None, CONNECT_NO_URL, available
    if _has_userinfo(url.strip()):
        return None, CONNECT_URL_USERINFO, available
    headers, ok = _clean_connect_headers(config.get("headers"))
    if not ok:
        return None, CONNECT_BAD_HEADERS, available
    return {"url": url.strip(), "headers": headers}, None, available


def read_connect_target(name, now_fn=time.time, force_refresh=False):
    """`{"target", "found", "reason", "available", "stale", "error"}`.

    `target` is `{"url": str, "headers": dict}` or None. `reason` is one of the
    CONNECT_* literals above, or None when the name is blank, because blank
    means the channel is switched off and that is not a misconfiguration.

    EVERY OUTCOME OF A QUERY THAT SUCCEEDED IS CACHED against the name it was
    resolved for, for CONNECT_TTL_S, EXCEPT CONNECT_NO_MATCH which is held for
    the much shorter CONNECT_MISS_TTL_S. The no-match reason alone is the one
    an operator clears from Dispatcharr's side seconds later: the loop is to
    create the Integration and then check, and a five-minute cached no-match
    would report the row missing well after it was created. Every other reason
    is a stable state (a disabled row, a script row, a missing URL, an address
    carrying a credential, unusable headers).

    NO-MATCH WAS ONCE NOT CACHED AT ALL, and that was worse than a short
    cache: a typo'd name re-queried every Integration row on every
    five-second collector tick, forever, from inside a web worker, which is
    the same cost this docstring cites as the reason to cache the rest.
    Measured over twelve ticks: a matching name cost one query, a typo cost
    twelve. `_connect_report` forces a refresh, so the button an operator
    presses after creating a row is unaffected by either lifetime. `read_redact_hosts` caches on the same
    rule: it stores every successful query, including one that yields nothing,
    and leaves only an exception uncached. A LOOKUP THAT RAISED is likewise
    not cached here.

    A lookup failure degrades to the last good target and reports `stale`. It
    is deliberately NOT fail-closed: a database hiccup must not stop delivery
    on every other channel.

    The URL is NOT validated here. `channels.safe_url` is the one predicate
    that decides whether a target is postable, and duplicating it in this
    module would let configuration time and send time drift apart.
    """
    blank = {"target": None, "found": False, "reason": None, "available": [],
             "stale": False, "error": None}
    if not name or not str(name).strip():
        return blank

    name = str(name)
    now = now_fn()
    cached = _cache_get(CONNECT_CACHE_ATTR)
    if not force_refresh and cached and cached.get("name") == name:
        ttl = (CONNECT_MISS_TTL_S if cached.get("reason") == CONNECT_NO_MATCH
               else CONNECT_TTL_S)
        if now - cached.get("at", 0) < ttl:
            return {k: v for k, v in cached.items()
                    if k not in ("at", "name")}

    try:
        rows = _query_connect_rows()
    except Exception as exc:
        info = {"target": None, "found": False,
                "reason": CONNECT_LOOKUP_FAILED, "available": [],
                "stale": False, "error": type(exc).__name__}
        if cached and cached.get("name") == name and cached.get("target"):
            info["target"] = cached["target"]
            info["stale"] = True
        return info

    target, reason, available = _resolve_connect_rows(name, rows)
    info = {"target": target, "found": target is not None, "reason": reason,
            "available": available, "stale": False, "error": None}
    # Every outcome of a query that SUCCEEDED is cached, including no-match,
    # which is held for the shorter CONNECT_MISS_TTL_S. Leaving it uncached
    # made a typo'd name re-query on every collector tick indefinitely.
    _cache_set(CONNECT_CACHE_ATTR, dict(info, at=now, name=name))
    return info
