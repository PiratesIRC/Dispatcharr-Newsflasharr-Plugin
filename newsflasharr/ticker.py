"""On-screen ticker: the one file Newsflasharr writes for ffmpeg to draw.

An operator-created Dispatcharr OutputProfile runs a `drawtext` filter with
`textfile=/data/newsflasharr/ticker.txt:reload=N`, so writing this file puts a
banner over live video within about a second, and emptying it removes the
banner -- both WITHOUT restarting ffmpeg or interrupting the stream.

Newsflasharr writes this file and nothing else. It does NOT create, modify, or
even read a Dispatcharr row for this feature: the plugin is read-only on
Dispatcharr and that invariant is what makes it safe to run. The OutputProfile
is an operator recipe (see README).

Everything here was measured against the live box; the surprising parts:

  - A MISSING file is not a degradation. `drawtext` fails to initialise the
    whole filtergraph and ffmpeg exits -- but Dispatcharr's ProfileManager has
    already marked the transcode ACTIVE, so the client attaches to a buffer
    that never fills. The operator sees a HUNG CHANNEL, not an error, and every
    reconnect respawns it. `Store.ensure_dirs()` therefore creates the file.
  - An EMPTY file, and a file containing only a newline, both draw nothing.
  - A WHITESPACE-ONLY file draws a bare black box (with `box=1`). So clearing
    must write "" or "\\n" -- never " ".
  - `os.replace` is safe: drawtext re-opens by path on each reload interval, so
    it never holds a stale inode.
  - `expansion=none` in the operator's filter is what stops `%{...}` being
    evaluated. This module also strips control characters, but that second
    layer lives in a profile the plugin cannot enforce -- so sanitising here is
    the layer we actually control, not a belt-and-braces nicety.

Stdlib only, standalone-loadable (conftest.load_mod).
"""
from __future__ import annotations

import math
import os
import time
import unicodedata

TICKER_FILENAME = "ticker.txt"
MAX_CHARS = 120                 # a TV banner, not a document; drawtext cannot wrap

# The prefix gets its OWN, much smaller budget rather than sharing MAX_CHARS.
# _compose builds "<prefix> <sym> <Source>: <title>" and sanitize() truncates
# the result at MAX_CHARS, so an uncapped prefix sits at the FRONT of the line
# and silently pushes the severity and then the whole title off the end -- a
# banner containing no alert, with nothing raised anywhere. Capping here keeps
# the majority of the line for the thing the operator actually needs to read.
MAX_PREFIX_CHARS = 24

# Severity symbols (operator's 2026-07-21 format): ASCII on purpose -- the
# drawtext font (DejaVuSans) has no emoji glyphs and every byte of the
# 120-char budget matters at TV distance. An UNKNOWN severity falls back to
# the spelled-out word rather than guessing a symbol; upstream ingest coerces
# unknowns to "warning", so the fallback is a test-path safety net, not a
# production state.
_SEVERITY_SYMBOLS = {"critical": "[!]", "warning": "[~]", "info": "[i]"}

# Used when the TTL cannot be read from settings -- see expire(). Deliberately
# generous relative to the 20s default: this is a backstop against a stuck
# banner, not the normal path.
FALLBACK_TTL_S = 120.0

_RANK = {"info": 0, "warning": 1, "critical": 2}

# Categories that break a single-line banner or are invisible: Cc (CR, LF,
# NUL), Cf (zero-width), Zl/Zp (U+2028/U+2029).
_STRIP_CATS = frozenset({"Cc", "Cf", "Zl", "Zp"})


def ticker_path(base_dir):
    return os.path.join(base_dir, TICKER_FILENAME)


def sanitize(value):
    """One line of plain text, safe to hand to a C library reading a file.

    Structure-breaking codepoints become a space rather than vanishing, so
    "a\\nb" reads as "a b" and not the misleading "ab".
    """
    text = "" if value is None else str(value)
    cleaned = "".join(
        " " if unicodedata.category(ch) in _STRIP_CATS else ch
        for ch in text
    )
    return " ".join(cleaned.split())[:MAX_CHARS]


def rank(severity):
    return _RANK.get(str(severity or "").lower(), 0)


DEFAULT_MIN_SEVERITY = "critical"


def resolve_min_severity(value):
    """-> (rank, effective_name, reason_or_None). Same contract as
    resolve_position / resolve_opacity / resolve_colours / resolve_font_size.

    This was the ONE ticker setting never resolve-and-reported, and it is the
    one with the worst failure direction: `rank()` maps anything unrecognised
    to **0 = info**, so a typo does not disable the banner -- it makes the
    ticker draw EVERYTHING over live TV, the most permissive state available,
    while the report printed the operator's own value back as if in force.

    Fails SAFE (to `critical`) rather than permissive, and reports. Case and
    surrounding whitespace are normalised rather than flagged: unambiguous
    input, and a false alarm is its own defect.
    """
    default_rank = _RANK[DEFAULT_MIN_SEVERITY]
    if isinstance(value, bool):
        return (default_rank, DEFAULT_MIN_SEVERITY,
                "is a true/false value, not a severity")
    if value is None or (isinstance(value, str) and not value.strip()):
        return default_rank, DEFAULT_MIN_SEVERITY, None
    name = str(value).strip().lower()
    if name in _RANK:
        return _RANK[name], name, None
    return (default_rank, DEFAULT_MIN_SEVERITY,
            "is not one of critical, warning, info")


class Ticker:
    """Writes/clears the ticker file. Never raises.

    Constructed per tick from the live settings, like `wire_redact` -- there is
    no long-lived state beyond the file itself, deliberately: the file's mtime
    IS the state, which is what lets `expire()` work on a tick that could not
    read settings at all.
    """

    def __init__(self, base_dir, enabled=False, min_severity="critical",
                 ttl_s=20.0, sources=None, now_fn=time.time, prefix=""):
        self.path = ticker_path(base_dir)
        self.enabled = bool(enabled)
        self.min_rank = rank(min_severity)
        # Sanitized and capped once, here, so _compose can concatenate freely.
        self.prefix = sanitize(prefix)[:MAX_PREFIX_CHARS].strip()
        try:
            self.ttl_s = max(1.0, float(ttl_s))
        except (TypeError, ValueError):
            self.ttl_s = 20.0
        # None/empty = every source may draw. Defaulting to a specific source
        # is the field's job, not this class's.
        self.sources = {s.strip().lower() for s in (sources or []) if s.strip()} or None
        self.now_fn = now_fn

    # -- writing -----------------------------------------------------------

    def note(self, event):
        """Raise (or clear) the banner for a delivered event. Returns what it did.

        A RESOLVE clears, and does so REGARDLESS of severity. This is not
        symmetry for its own sake: sentinelarr hardcodes resolves to
        `severity="info"`, so keying the clear on severity would filter it out
        under the default `min_severity="critical"` and a channel that broke
        and recovered inside 30s would sit under a stale banner for the whole
        TTL, with the all-clear already delivered to the phone.
        """
        if not self.enabled:
            return "disabled"
        event = event if isinstance(event, dict) else {}

        if str(event.get("kind") or "") == "resolve":
            # The bool is CHECKED here, as it already was on the write path
            # below -- the asymmetry was the defect. A failed clear leaves a
            # stale CRITICAL banner over live TV indefinitely, and deliver.py
            # ledgered `cleared_by_resolve` regardless: the ledger positively
            # asserted it had been cleared.
            if not self.clear():
                return "clear_failed"
            return "cleared_by_resolve"

        if rank(event.get("severity")) < self.min_rank:
            return "below_threshold"
        if self.sources is not None:
            if str(event.get("source") or "").lower() not in self.sources:
                return "source_filtered"

        text = self._compose(event)
        if not text:
            return "empty"
        return "written" if self._write(text) else "write_failed"

    def _compose(self, event):
        severity = str(event.get("severity") or "").lower()
        title = sanitize(event.get("title"))
        if not title:
            return ""
        lead = f"{self.prefix} " if self.prefix else ""
        symbol = _SEVERITY_SYMBOLS.get(severity)
        if symbol:
            lead += f"{symbol} "
        elif severity:
            lead += f"{severity.upper()}: "
        # The SOURCE names who is talking -- "[!] Sentinelarr: ..." -- so a
        # banner is attributable at TV distance. Display-capitalized only;
        # the routing/dedup identity stays the raw lowercase source field.
        source = sanitize(str(event.get("source") or "")).strip()
        if source:
            lead += f"{source[:1].upper()}{source[1:]}: "
        return sanitize(lead + title)

    # -- clearing ----------------------------------------------------------

    def clear(self):
        """Empty the banner. Writes "" -- never " ", which would draw a box."""
        return self._write("")

    def expire(self, now=None):
        """Clear the banner once it is older than the TTL.

        Keyed on the file's MTIME rather than any in-memory or state.json
        timestamp, so it still works on a tick that could not read settings
        (the fail-closed path returns before the settings are available, and
        `read_settings()` is on record returning None once in production).
        Callers on that path pass no ttl and get FALLBACK_TTL_S.
        """
        now = self.now_fn() if now is None else now
        try:
            if os.path.getsize(self.path) == 0:
                return False                       # already clear
            age = now - os.path.getmtime(self.path)
        except OSError:
            return False
        if age < self.ttl_s:
            return False
        # True means "the banner is gone". If the write failed it is still on
        # screen, so saying True would make the TTL backstop -- the last line
        # of defence against a stale banner -- report success for a no-op.
        return bool(self.clear())

    # -- io ----------------------------------------------------------------

    def _write(self, text):
        """Atomic: drawtext re-opens by path each reload, so a rename is safe
        and a half-written file is never observable."""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False


def ensure_file(base_dir):
    """Create an empty ticker file if absent. Never raises.

    Load-bearing, not tidiness: a missing textfile makes drawtext fail to
    initialise the filtergraph, ffmpeg exits, and Dispatcharr has already
    marked the transcode ACTIVE -- so the operator gets a hung channel with no
    error, respawned on every reconnect.
    """
    path = ticker_path(base_dir)
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
        return True
    except Exception:
        return False


# -- rendering the operator's ffmpeg filter -----------------------------------
# Newsflasharr is READ-ONLY on Dispatcharr: it creates and edits no rows, which is a
# large part of why it is safe to run. So appearance settings CANNOT repaint a
# running filter. They render a string the operator pastes into their own
# OutputProfile. Everything below is pure -- no I/O, no Django.

# Resolution-RELATIVE, so a tier means the same proportion of the picture on 720p,
# 1080p and 4K. A fixed pixel size renders proportionally larger on smaller
# streams (measured: fontsize=36 draws 27px of glyph at BOTH 720p and 1080p).
FONT_SIZES = {"small": "h/40", "medium": "h/30", "large": "h/20"}
DEFAULT_FONT_SIZE = "medium"

# `boxborderw` takes a 1-to-4 INTEGER LIST, not an expression -- `h/90` does not
# evaluate and renders identically to 0 (measured on ffmpeg 8.1.2: a 26px box vs
# 50px for a literal 12), which is how this banner shipped with no padding at all
# for months. So this one value CANNOT be resolution-relative, unlike `fontsize`
# above and `y` below. Do not "tidy" it back into an expression to match them --
# tests/test_ticker.py pins it as a literal integer for exactly that reason.
BOX_BORDER_W = 12

# The operator-facing vertical scale: -10 = top of the picture, 0 = exact centre,
# +10 = bottom. DEFAULT_POSITION is the tick nearest where the banner sat before
# this setting existed (measured: box top row 927 vs the old 925 at 1080p).
POSITION_MIN, POSITION_MAX = -10, 10
DEFAULT_POSITION = 8
DEFAULT_TEXT_COLOR = "white"
DEFAULT_BG_COLOR = "black"
DEFAULT_BG_OPACITY = 0.6

DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SCROLL_PX_PER_S = 150

# ffmpeg's named colours, plus #RRGGBB / #RRGGBBAA / 0xRRGGBB. Deliberately a
# CURATED list rather than "anything without a bad character": an unknown name
# makes drawtext fail to initialise, and a filter that fails to initialise HANGS
# THE CHANNEL (ffmpeg exits, but Dispatcharr has already marked the transcode
# active, so the client waits on a buffer that never fills -- no error message).
# Rejecting at validation is the only way the operator finds out cheaply.
_COLOR_NAMES = frozenset("""
black white red green blue yellow cyan magenta gray grey silver maroon olive
lime aqua teal navy fuchsia purple orange pink brown gold beige ivory khaki
lavender salmon tan turquoise violet wheat crimson indigo orchid plum coral
darkred darkgreen darkblue darkgray darkgrey lightgray lightgrey lightblue
lightgreen lightyellow yellowgreen skyblue royalblue steelblue seagreen
springgreen slategray slategrey midnightblue forestgreen firebrick
""".split())


_COLOR_PAIRS = {
    "white_on_black": ("white", "black"),
    "yellow_on_black": ("yellow", "black"),
    "cyan_on_black": ("cyan", "black"),
    "green_on_black": ("green", "black"),
    "black_on_white": ("black", "white"),
}
_DEFAULT_PAIR = "white_on_black"


def colours_for(value):
    """(text, bg) for a `ticker_colors` preset; white-on-black for anything
    unknown/absent (an upgraded install with no stored value renders exactly as
    before). Presets make an un-renderable colour unrepresentable -- the hang
    the old free-text fields risked cannot happen."""
    return _COLOR_PAIRS.get(str(value or "").strip().lower(),
                            _COLOR_PAIRS[_DEFAULT_PAIR])


def validate_color(value):
    """None if usable, else a human-readable reason.

    Rejects anything carrying `:` `,` `\\` `'` `"` or whitespace BEFORE checking
    the name: those are the characters that would break out of the option and
    inject further drawtext options or whole filters into the operator's
    filterchain.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return "empty"
    for ch in ":,\\'\" \t":
        if ch in text:
            return f"contains {ch!r}, which would break the filter"
    low = text.lower()
    if low in _COLOR_NAMES:
        return None
    if low.startswith("#") and len(low) in (7, 9):
        body = low[1:]
    elif low.startswith("0x") and len(low) == 8:
        body = low[2:]
    else:
        return "not a known colour name or #RRGGBB / 0xRRGGBB value"
    if all(c in "0123456789abcdef" for c in body):
        return None
    return "not a valid hex colour"


def _safe_color(value, default):
    return value if validate_color(value) is None else default


def _opacity(value):
    """Kept as the bare-value helper; resolve_opacity is the reporting one."""
    return resolve_opacity(value)[0]


def _is_blank(value):
    """The untouched-field case: absent, or present-but-None (which is what
    Dispatcharr's settings UI actually stores), or whitespace. Silent by design
    -- reporting it would turn Validate red on every install."""
    return value is None or (isinstance(value, str) and not value.strip())


def resolve_opacity(value):
    """-> (alpha, reason_or_None). Same contract and gate order as
    resolve_position, for the same reasons.

    The hole this closes: `nan` previously resolved to **0.0** -- a fully
    transparent box, so the banner loses its background over bright video --
    and nothing said so. `min/max` cannot catch it because every NaN comparison
    is False, so the clamp silently returned whichever bound was nested
    innermost.
    """
    if isinstance(value, bool):
        return DEFAULT_BG_OPACITY, "is a true/false value, not a number"
    if _is_blank(value):
        return DEFAULT_BG_OPACITY, None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_BG_OPACITY, "is not a number"
    if not math.isfinite(f):
        return DEFAULT_BG_OPACITY, "is not a finite number"
    if f < 0.0 or f > 1.0:
        return min(1.0, max(0.0, f)), "is outside 0..1"
    return f, None


def resolve_font_size(value):
    """-> (expression, effective_name, reason_or_None).

    Case and surrounding whitespace are normalized rather than reported --
    unambiguous input, and flagging it would be a false alarm. An unrecognised
    tier IS reported: it silently renders medium, so the operator who typed it
    sees no change and has nothing to read.
    """
    if _is_blank(value):
        return FONT_SIZES[DEFAULT_FONT_SIZE], DEFAULT_FONT_SIZE, None
    name = str(value).strip().lower()
    if name in FONT_SIZES:
        return FONT_SIZES[name], name, None
    return (FONT_SIZES[DEFAULT_FONT_SIZE], DEFAULT_FONT_SIZE,
            "is not one of small, medium, large")


def resolve_colours(value):
    """-> (text, bg, effective_preset, reason_or_None).

    A stale value from the pre-preset era (the free-text `ticker_text_color` /
    `ticker_bg_color` fields, which Dispatcharr never prunes) lands here and
    would otherwise render white-on-black with no explanation.
    """
    if _is_blank(value):
        return (*_COLOR_PAIRS[_DEFAULT_PAIR], _DEFAULT_PAIR, None)
    name = str(value).strip().lower()
    if name in _COLOR_PAIRS:
        return (*_COLOR_PAIRS[name], name, None)
    return (*_COLOR_PAIRS[_DEFAULT_PAIR], _DEFAULT_PAIR,
            "is not one of " + ", ".join(sorted(_COLOR_PAIRS)))


def resolve_position(value):
    """-> (factor, effective_n, reason_or_None); factor is 0.0 top .. 1.0 bottom.

    THE ONE CLASSIFIER for this setting. `render_filter`, `_ticker_report` and
    `show_ticker_filter` all call it, so a report can never claim a position the
    render did not draw -- the drift that bit `dispatcharr_events` (two copies of
    the event resolver) and that `routing.summarize` avoids by calling the real
    `route()`. `reason` is None when the value needs no operator attention.

    Order matters and every gate earns its place:

      * bool FIRST -- it is an int subclass, so `False` would resolve to 0 ==
        dead centre, indistinguishable from a deliberate centre banner.
      * blank SECOND and SILENTLY -- a present-but-None value is the normal
        state of an untouched field, and reporting it would turn Validate red on
        every install. Blank is NOT zero here; zero means the centre.
      * non-finite BEFORE the clamp -- NaN makes every comparison False, so a
        min/max clamp returns whichever bound is nested innermost, and an
        `if n < MIN` form passes NaN straight through into `y=...*nan`, which
        drawtext cannot evaluate: a hung channel with no error anywhere.
        Reachable -- `json.loads("NaN")` yields it.
      * out of range is CLAMPED (never hand drawtext something unevaluatable)
        but still REPORTED: `-160` is `y=h-160`, the value this project's own
        docs publish for this dimension, and it clamps to the TOP -- the exact
        inverse of its intent.
    """
    span = POSITION_MAX - POSITION_MIN
    default_f = (DEFAULT_POSITION - POSITION_MIN) / span
    if isinstance(value, bool):
        return default_f, DEFAULT_POSITION, "is a true/false value, not a position"
    if value is None or (isinstance(value, str) and not value.strip()):
        return default_f, DEFAULT_POSITION, None
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return default_f, DEFAULT_POSITION, "is not a number"
    if not math.isfinite(n):
        return default_f, DEFAULT_POSITION, "is not a finite number"
    if n < POSITION_MIN or n > POSITION_MAX:
        clamped = min(POSITION_MAX, max(POSITION_MIN, n))
        return ((clamped - POSITION_MIN) / span, clamped,
                f"is outside {POSITION_MIN}..{POSITION_MAX}")
    return (n - POSITION_MIN) / span, n, None


def render_filter(settings, font=DEFAULT_FONT, ticker_path_="/data/newsflasharr/ticker.txt"):
    """The drawtext filter, in the form the operator PASTES (double backslashes).

    Dispatcharr builds the command as [command] + shlex_split(parameters), and
    shlex consumes one backslash level, leaving ffmpeg the `\\,` its filter parser
    needs to keep a comma INSIDE an expression rather than treating it as a
    filterchain separator. So two real backslashes must be stored per comma. A
    wrong level does not fail on save -- it fails at stream start with
    "Error parsing filterchain", i.e. a hung channel.

    The `y` expression is deliberately COMMA-FREE, so it needs none of that
    escaping. Add a comma to it (a `min()`, say) and the two-stored-backslashes
    rule applies to it as well.
    """
    settings = settings if isinstance(settings, dict) else {}
    size, _size_name, _size_reason = resolve_font_size(settings.get("ticker_font_size"))
    _text, _bg, _pair_name, _pair_reason = resolve_colours(settings.get("ticker_colors"))
    # _safe_color stays as belt-and-braces: the preset map only emits allowlist
    # colours, so this is a no-op today, but the defense (and its tests) remain.
    text_color = _safe_color(_text, DEFAULT_TEXT_COLOR)
    bg_color = _safe_color(_bg, DEFAULT_BG_COLOR)
    # settings.get(k), NOT the two-arg form: it does not apply its default to a
    # present-but-None value, which is what an untouched field actually stores.
    alpha, _alpha_reason = resolve_opacity(settings.get("ticker_bg_opacity"))
    position_f, _eff_n, _reason = resolve_position(settings.get("ticker_position"))

    c = "\\\\,"          # two REAL backslashes + comma
    # Centre text that fits; scroll only what overflows. drawtext cannot wrap, so
    # without this a long alert is silently cut off. The scrolling is done by the
    # x EXPRESSION -- a different evaluator from `expansion`, which must stay
    # `none` because the TEXT carries provider-controlled channel names.
    x = (f"if(gt(text_w{c}w){c}w-mod(t*{SCROLL_PX_PER_S}{c}w+text_w){c}"
         f"(w-text_w)/2)")
    return (
        f"drawtext=fontfile={font}:textfile={ticker_path_}:reload=30:"
        f"expansion=none:fontsize={size}:fontcolor={text_color}:box=1:"
        f"boxcolor={bg_color}@{alpha:g}:boxborderw={BOX_BORDER_W}:x={x}:"
        # `y` is the TOP OF THE TEXT, and the box spans
        # [y-BOX_BORDER_W, y+text_h+BOX_BORDER_W), so the usable span for the
        # whole box is inset by the border at BOTH ends. At f=0.5 this is
        # algebraically (h-text_h)/2 -- the exact centre; at f=0 and f=1 the box
        # is flush with the top and bottom edges (all measured). Keying on
        # text_h rather than a fixed offset is what keeps the bottom tick fully
        # on-screen for descender-bearing text, which measures 8px taller.
        f"y={BOX_BORDER_W}+(h-text_h-{2 * BOX_BORDER_W})*{position_f:g}"
    )


# Quantiser per quality tier. LOWER IS BETTER PICTURE. Chosen after a live
# complaint on 2026-08-09: the profile was re-encoding at a flat 5 Mbit/s,
# which is roughly what the sources already arrive at, so every channel through
# it was a second generation encode at the first generation's bitrate. ffmpeg
# reported its own quantiser swinging to 29, which is visibly blocky, and the
# operator could see it: the same channel played cleanly as a passthrough
# seconds later.
ENCODE_QUALITY = {"high": 19, "balanced": 21, "light": 25}
DEFAULT_ENCODE_QUALITY = "balanced"

# A CEILING AND A BUFFER, not a target. With neither, nvenc has nothing
# bounding a burst and quality swings instead of degrading gracefully. These
# are deliberately generous: the stream crosses a LAN, not the internet.
ENCODE_MAXRATE = "16M"
ENCODE_BUFSIZE = "32M"

# A KEYFRAME INTERVAL, IN FRAMES. This is a correctness setting, not a quality
# one. Measured 2026-09-12, after the operator reported every channel black on
# the device carrying the banner while Sentinelarr reported nothing wrong,
# correctly, because its sensors read the INPUT side of the pipeline.
#
# With no explicit interval this encoder emitted exactly ONE SPS, ONE PPS and
# ONE IDR keyframe in a 12 s live capture, against 9 of each on the untouched
# passthrough over the same session and minute. A client that joins after that
# single keyframe has left the output buffer never receives the parameter sets
# its decoder needs to start, so it shows a black picture indefinitely while
# audio and bytes keep arriving. The passthrough profiles are unaffected
# because they copy the video and the source's own keyframes ride through.
#
# 60 frames is 1.0 s at 60 fps and 2.4 s at 25 fps, so the worst case is a
# short tune-in wait rather than a black screen.
ENCODE_GOP_FRAMES = 60


def render_encoder(settings):
    """The video encoder arguments, quality targeted rather than bitrate capped.

    `-b:v 0` is REQUIRED alongside `-cq`: with a bitrate target still set,
    nvenc treats the quantiser as a hint and goes back to hitting the number,
    which is the behaviour being fixed here.

    Resolved with `or` rather than `.get(key, default)` on purpose. Dispatcharr
    stores a null for a field the operator never touched, and the two argument
    form only applies its default when the key is ABSENT, so a present null
    would fall through as None. That exact shape has already produced a live
    defect in this plugin.
    """
    settings = settings if isinstance(settings, dict) else {}
    tier = settings.get("ticker_encode_quality") or DEFAULT_ENCODE_QUALITY
    cq = ENCODE_QUALITY.get(tier if isinstance(tier, str) else "",
                            ENCODE_QUALITY[DEFAULT_ENCODE_QUALITY])
    return (f"-c:v h264_nvenc -preset p5 -rc vbr -cq {cq} -b:v 0 "
            f"-maxrate {ENCODE_MAXRATE} -bufsize {ENCODE_BUFSIZE} "
            # `-forced-idr 1` is REQUIRED alongside `-g`: nvenc defaults it to
            # false, and a plain I-frame does not reset the decoder of a client
            # that joined mid-stream. Only an IDR does, and every client here
            # joins mid-stream.
            f"-g {ENCODE_GOP_FRAMES} -forced-idr 1")


def render_parameters(settings, font=DEFAULT_FONT,
                      ticker_path_="/data/newsflasharr/ticker.txt", encoder=None):
    """The COMPLETE OutputProfile parameters string, ready to paste.

    Complete, not just the drawtext fragment: a partial paste produces an ffmpeg
    with no output muxer, which starts successfully and then serves nothing --
    that failure took the operator's live TV down once already.
    """
    settings = settings if isinstance(settings, dict) else {}
    enc = encoder or render_encoder(settings)
    return " ".join([
        # +nobuffer is gone: it left no packets in hand, and on 2026-08-09 this
        # stage produced no frames at all on a channel that played perfectly as
        # an untouched passthrough seconds later. +genpts stays, because it
        # FILLS IN missing timestamps and never rewrites present ones.
        #
        # +discardcorrupt STAYS, and the argument for removing it was WRONG.
        # It was removed on the grounds that the passthrough keeps these
        # packets and plays fine; that conflated two paths. The passthrough
        # COPIES without decoding, so corruption rides through harmlessly.
        # This stage DECODES. Measured within minutes of going live without
        # it: a channel start failed with "Error opening input: Invalid data
        # found when processing input" and the channel went to ERROR, playing
        # only on the retry. Corrupt data as the pipe opens can stop ffmpeg
        # identifying the stream at all.
        "-fflags +discardcorrupt+genpts",
        # The probe limits are OMITTED, NOT RAISED. Measured in this workspace
        # on Dispatcharr's input profile, same trade-off: with no limits ffmpeg
        # stops probing the moment the streams are identified (2.0 s on a real
        # channel), while a large explicit limit makes it keep reading up to
        # that much (19.6 s at 10M) and buys nothing. `-analyzeduration 0` is
        # the opposite failure: no time to establish the parameters at all.
        "-i pipe:0",
        "-map 0:v:0 -map 0:a:0",
        f"-filter:v {render_filter(settings, font, ticker_path_)}",
        enc,
        "-c:a aac -b:a 192k -ac 2",
        "-max_muxing_queue_size 4096 -flush_packets 1",
        "-mpegts_flags +pat_pmt_at_frames+resend_headers+initial_discontinuity",
        "-f mpegts pipe:1",
    ])


def from_settings(base_dir, settings, now_fn=time.time):
    """Build a Ticker from the plugin settings dict.

    Uses `or DEFAULT` rather than `settings.get(key, DEFAULT)` throughout. The
    two-arg form only applies its default when the key is ABSENT -- a key
    present with a null value returns None, and Dispatcharr's settings UI
    stores exactly that for a field the operator never touched. Observed live:
    `ticker_min_severity` saved as None, which made rank(None) == 0, i.e.
    "notify on everything", the opposite of the field's stated default. Same
    shape as settings-derived state failing silently on an entry path that
    never primed it.
    """
    settings = settings if isinstance(settings, dict) else {}
    raw_sources = settings.get("ticker_sources") or ""
    if isinstance(raw_sources, (list, tuple)):
        sources = [str(s) for s in raw_sources]
    else:
        sources = str(raw_sources or "").split(",")
    return Ticker(
        base_dir,
        enabled=bool(settings.get("ticker_enabled")),
        # Through the resolver so an unrecognised value fails SAFE to
        # critical instead of rank 0 = "draw everything over live TV".
        min_severity=resolve_min_severity(
            settings.get("ticker_min_severity"))[1],
        ttl_s=settings.get("ticker_ttl_s") or 20,
        sources=sources,
        now_fn=now_fn,
        prefix=settings.get("ticker_prefix") or "",
    )
