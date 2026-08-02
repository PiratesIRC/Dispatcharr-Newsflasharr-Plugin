"""Reads Dispatcharr's OWN configuration: system timezone + provider hostnames.

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

HOST_TTL_S = 300.0
TZ_TTL_S = 300.0

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
