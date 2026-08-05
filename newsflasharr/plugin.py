"""Newsflasharr -- Dispatcharr plugin entrypoint.

The one module in this package that uses real relative imports: it is only
ever loaded as part of the `notifier` package (Dispatcharr's plugin loader,
or tests/conftest.py's load_plugin()), which is what makes `from . import
...` resolve. Every other module in this plugin is stdlib-only and loads
standalone (conftest.load_mod) so it can be unit tested without a package
context or Django.

Wires: `collector.ensure_collector` (boot-arms the leader-lease daemon
thread from `__init__` -- it must stay idempotent, lock-free,
and a failure here must never break plugin construction, i.e. break
Dispatcharr itself), and the four settings-UI actions (validate/test/
status/flush), each operating on the ON-DISK state via Store/spool -- the
worker serving a UI click is usually not the leader, so nothing here trusts
in-memory collector state. The caller contract this implements is published
as docs/API.md.

Wire-side redaction (spec section 8.1: "the notifier applies redact() again
at the wire, defense-in-depth, dustarr pattern"): `notify_client.redact`
already runs at spool-write time (client-side, load-bearing) and Store's
`redact` callable covers the ledger/failed-sidecar path everywhere a Store
is constructed in this module (`show_status`, `flush_held`) AND, as of the
redaction-completeness pass, the production leader's own Store too --
`ensure_collector(PLUGIN_VERSION, base_dir=DEFAULT_BASE_DIR, redact=redact)`
below threads the real redactor down into `Collector.__init__`, which was
previously defaulting to an identity redact on every tick's ledger/
failed-sidecar write. `test_notification`'s per-channel path additionally
re-applies `redact()` to the event it builds by hand, since that path does
not go through `notify_client.notify()`. Hostname redaction (spec section
8.1's "any URL whose host matches is replaced wholesale") is ENFORCED at the
wire: `Collector._tick` and `flush_held` both build their `wire_redact` via
the shared `collector.build_wire_redact(channels_mod, settings, redact)`
(`channels.redact_hosts(redact(text), hostnames)`) and pass it to the
`Deliverer` they construct, which applies it to a copy of each event's
title/body/url immediately before `channels.send` -- never to the stored
event/sidecar. This does not touch the individual channel builders in
channels.py (`_discord_request` et al.), so Task 7/8's tests are
unaffected. `flush_held`'s admin-triggered `Deliverer` passes the same
`wire_redact` as the unattended production path (final-review fix M1: it
previously passed none, so a manual Flush skipped the hostname backstop).

The hostname list itself is the UNION of hosts auto-derived from Dispatcharr's
EPG sources and M3U accounts (`dispatcharr.read_redact_hosts`) with the
`redact_hostnames` setting -- see spec amendment A1. The quiet-hours timezone
is likewise derived (`dispatcharr.read_timezone`) rather than typed. Both
arrive as `_resolved_*` keys via `collector.inject_derived`; the prefix keeps
a stored value from a pre-derivation version from shadowing them, since
Dispatcharr never prunes settings whose field was removed.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time

from . import (
    channels,
    collector,
    deliver,
    dispatcharr,
    dispatcharr_events,
    email_render,
    gates,
    readout,
    report_count,
    report_expect,
    routing,
    spool,
    store,
    ticker,
)
from .notify_client import notify, redact

PLUGIN_VERSION = "1.26.2171427"

# A delivery failure older than this is history, not an outage: reported as
# a total, never as a problem. A bare cumulative counter would otherwise go
# permanently red on one ancient failure.
_RECENT_FAILURE_S = 24 * 3600.0

DEFAULT_BASE_DIR = "/data/newsflasharr"

FIELDS = [
    # Grouped with `type: "info"` section headers -- the same primitive
    # Event-Channel-Managarr uses. Dispatcharr renders settings as one flat
    # scroll with no native sectioning, and 20 ungrouped fields interleaving
    # channel config with delivery policy is a usability cliff.
    #
    # Section headers and button labels carry emoji deliberately -- they are
    # rendered by a BROWSER and are the only visual structure this page gets.
    # This does NOT extend to text Newsflasharr puts on the wire or on screen:
    # channels.py's fit marker and gates.py's digest separators stay ASCII (both
    # test-enforced -- glyphs cost bytes on the byte-limited ntfy channel), and
    # `ticker_prefix` stays ASCII because ffmpeg's drawtext has no emoji glyphs.
    #
    # NOTE the `options` shape below. Dispatcharr's PluginFieldSerializer
    # requires a list of {"value", "label"} dicts, and loader._normalize_fields
    # SILENTLY DROPS any field that fails validation (logger.warning only). A
    # bare list of strings therefore removes the field from the UI entirely --
    # which is exactly what happened to smtp_security on first release: it was
    # declared but never rendered, pinned to its default with no error anywhere.
    {"id": "_section_start", "type": "info", "label": "🚀 Start here",
     "description": "New here? RELOAD this page before you change anything; an "
                    "old tab silently reverts your settings when it saves. Then "
                    "fill in ONE channel below; ntfy needs both server "
                    "and topic; email needs server, From and Recipients. Type that "
                    "channel's name (discord, webhook, ntfy, apprise, smtp) into "
                    "Default channels. Then click Validate, then Test; only Test "
                    "proves a channel works."},
    {"id": "_section_channels", "type": "info", "label": "📢 Channels",
     "description": "Where notifications are sent. Leave a channel blank to "
                    "disable it. If none is enabled, nothing can be delivered; alerts wait "
                    "in the queue (they are NOT lost) until you configure one."},
    {"id": "discord_webhook_url", "label": "Discord webhook URL", "type": "string",
     "default": "", "input_type": "password",
     "description": "Discord channel webhook URL. Leave blank to disable Discord."},
    {"id": "webhook_url", "label": "Generic webhook URL", "type": "string",
     "default": "", "input_type": "password",
     "description": "Any endpoint that accepts a JSON POST body. Leave blank to disable."},
    {"id": "ntfy_server", "label": "ntfy server URL", "type": "string", "default": "",
     "placeholder": "https://ntfy.sh",
     "description": "e.g. https://ntfy.sh or your own ntfy instance. Leave blank to disable."},
    {"id": "ntfy_topic", "label": "ntfy topic", "type": "string", "default": "",
     "description": "The name you subscribe to in the ntfy app. On a public "
                    "server this IS the password; invent a long unguessable "
                    "one, never 'alerts' or your hostname, or strangers read "
                    "every notificatiin this installation sends."},
    {"id": "ntfy_token", "label": "ntfy access token", "type": "string", "default": "",
     "input_type": "password",
     "description": "Only needed for a protected/private ntfy topic."},
    {"id": "apprise_url", "label": "Apprise URL", "type": "string", "default": "",
     "input_type": "password",
     "description": "URL of an Apprise API endpoint. Leave blank to disable."},

    {"id": "_section_email", "type": "info", "label": "📧 Email (SMTP)",
     "description": "Email straight from Newsflasharr. Only counts as enabled when "
                    "server, From and Recipients are all filled in. A server "
                    "accepting the message means ACCEPTED, not delivered; check "
                    "your spam folder on the first send."},
    {"id": "smtp_server", "label": "Server", "type": "string", "default": "",
     "placeholder": "smtp.gmail.com:587",
     "description": "host or host:port (default port 587). Blank disables email. "
                    "Bracket IPv6 literals, e.g. [2001:db8::1]:587."},
    {"id": "smtp_security", "label": "Security", "type": "select",
     "default": "auto",
     "options": [
         {"value": "auto", "label": "Auto (infer from port)"},
         {"value": "starttls", "label": "STARTTLS (587, 25)"},
         {"value": "ssl", "label": "Implicit TLS / SSL (465)"},
         {"value": "none", "label": "None (unauthenticated relay only)"},
     ],
     "description": "Auto infers from the port (465 = SSL, otherwise STARTTLS) "
                    "and Validate prints what it resolved to. "
                    "Certificates are ALWAYS verified; there is no opt-out. "
                    "'None' with a username is refused; it would put your "
                    "password on the wire in base64 on every send."},
    {"id": "smtp_username", "label": "Username", "type": "string", "default": "",
     "description": "Leave blank for an unauthenticated relay. Gmail, Outlook "
                    "and Yahoo reject normal account passwords over SMTP; you "
                    "need an APP PASSWORD."},
    {"id": "smtp_password", "label": "Password", "type": "string", "default": "",
     "input_type": "password",
     "description": "An app password for most providers, not your account password."},
    {"id": "smtp_from", "label": "From address", "type": "string", "default": "",
     "description": "Defaults to the username. A From address at a domain you do "
                    "not control is likely to be junked or rejected."},
    {"id": "smtp_to", "label": "Recipients", "type": "string", "default": "",
     "placeholder": "you@example.com, ops@example.com",
     "description": "Comma-separated."},

    {"id": "_section_routing", "type": "info", "label": "🔀 Routing",
     "description": "Which events reach which channels. A matched rule ADDS its "
                    "channels to the default ones (so adding a rule never "
                    "silently un-routes them) UNLESS it sets "
                    "'exclusive': true, which sends ONLY to that rule's "
                    "channels. Matched rules UNION with "
                    "the default channels, so adding a rule never silently "
                    "un-routes them."},
    {"id": "default_channels", "label": "Default channels", "type": "string",
     "default": "", "placeholder": "ntfy, smtp",
     "description": "Where alerts go unless a rule below says otherwise. BLANK "
                    "MEANS NOTHING IS DELIVERED; type at least one name: "
                    "discord, webhook, ntfy, "
                    "apprise, smtp, ticker. Adding 'ticker' will not put every "
                    "alert on your TV; it still obeys the Ticker settings below."},
    {"id": "routing_rules", "label": "Routing rules", "type": "text", "default": "[]",
     "description": "JSON list, e.g. [{\"match\": {\"severity\": \"critical\"}, "
                    "\"channels\": [\"smtp\"]}]. Match on source, event or severity. "
                    "Empty list = everything uses the default channels. Rules ADD "
                    "to those defaults unless the rule sets \"exclusive\": true."},
    {"id": "critical_extra_channels", "label": "Critical alerts also go to",
     "type": "select", "default": "none",
     "options": [{"value": "none", "label": "Nothing (criticals use your normal channels)"}]
                + [{"value": k, "label": f"Also {phrase}"}
                   for k, (phrase, _ch) in routing._ESCALATION.items()],
     "description": "Send every CRITICAL alert to this extra channel on top of "
                    "your normal routing; a durable escalation without editing "
                    "the rules JSON. It applies to EVERY critical, including one "
                    "you routed elsewhere with an exclusive rule (a critical is a "
                    "critical). Warnings and info are unaffected."},

    {"id": "_section_ticker", "type": "info", "label": "📺 On-screen ticker",
     "description": "Draws alert text over live video. Needs a Dispatcharr Output "
                    "Profile with the drawtext filter (see README) and only reaches "
                    "devices whose playlist URL carries ?output_profile=<id>. It "
                    "RE-ENCODES video for those devices, and a banner cannot be "
                    "dismissed from a TV remote. REMOVING THIS PLUGIN LATER? "
                    "Delete that Output Profile FIRST; without the ticker "
                    "file, ffmpeg fails to start and those devices hang on a "
                    "stream that never loads, with no error anywhere."},
    {"id": "ticker_enabled", "label": "Enable on-screen ticker", "type": "boolean",
     "default": False,
     "description": "Off by default. Enabling this alone does nothing until the "
                    "Output Profile exists and a device subscribes to it."},
    {"id": "ticker_min_severity", "label": "Minimum severity", "type": "select",
     "default": "critical",
     "options": [
         {"value": "critical", "label": "Critical only (recommended)"},
         {"value": "warning", "label": "Warning and above"},
         {"value": "info", "label": "Everything"},
     ],
     "description": "Anything below critical puts routine noise over your "
                    "picture. All-clears clear the banner regardless of this "
                    "setting."},
    {"id": "ticker_ttl_s", "label": "Banner duration (s)", "type": "number",
     "default": 20, "min": 1,
     "description": "How long the banner stays up; long enough to read twice. An "
                    "all-clear removes it sooner. If Newsflasharr is killed uncleanly "
                    "the banner persists until the channel changes."},
    {"id": "ticker_prefix", "label": "Banner prefix", "type": "string",
     "default": "", "placeholder": "DISPATCHARR",
     "description": "Optional text at the front of every banner: 'HOME CRITICAL: "
                    "CNN is black'. Plain text only; emoji draw as an empty box. "
                    "Applies to the next banner; no re-paste needed. Trimmed to 24 "
                    "characters."},
    # APPEARANCE: these do NOT repaint a running filter. Newsflasharr creates and
    # edits no Dispatcharr rows -- that read-only invariant is why it is safe to
    # run -- so the ffmpeg filter belongs to the operator. These settings RENDER
    # the string; "Ticker filter" prints it to paste into the Output Profile.
    {"id": "ticker_sources", "label": "Only these sources may draw", "type": "string",
     "default": "sentinelarr", "placeholder": "sentinelarr",
     "description": "ONLY these plugins may draw. A name that is not listed is "
                    "silently skipped, and Test ticker IGNORES this list, so a "
                    "passing test does NOT prove your real alerts will draw. "
                    "Blank means ALL, which would put scheduled reports over "
                    "live TV."},

    {"id": "_section_ticker_look", "type": "info",
     "label": "🎨 Banner appearance",
     "description": "These four settings only change what the FILTER STRING says. Nothing on screen changes until you click 'Ticker filter', paste the result into your Output Profile, and restart the stream. (The settings above take effect on the next banner.)"},
    {"id": "ticker_font_size", "label": "Banner text size", "type": "select",
     "default": "medium",
     "options": [
         {"value": "small", "label": "Small (h/40)"},
         {"value": "medium", "label": "Medium (h/30)"},
         {"value": "large", "label": "Large (h/20)"},
     ],
     "description": "Scales with picture height, so it looks the same on 720p and "
                    "4K. Bigger text is wider and scrolls longer; raise the banner "
                    "duration to match."},
    {"id": "ticker_position", "label": "Banner vertical position",
     "type": "number", "default": ticker.DEFAULT_POSITION,
     "min": ticker.POSITION_MIN, "max": ticker.POSITION_MAX, "step": 1,
     "description": "-10 = top of the picture, 0 = dead centre, 10 = bottom. "
                    "Default 8 sits just above the subtitle band, where the "
                    "banner is today; 9 and 10 sit on it. Blank means 8, not "
                    "0; type 0 for the centre."},
    {"id": "ticker_colors", "label": "Colours", "type": "select",
     "default": "white_on_black",
     "options": [{"value": "white_on_black", "label": "White on black"},
                 {"value": "yellow_on_black", "label": "Yellow on black"},
                 {"value": "cyan_on_black", "label": "Cyan on black"},
                 {"value": "green_on_black", "label": "Green on black"},
                 {"value": "black_on_white", "label": "Black on white"}],
     "description": "Text-on-background for the banner. Every pair is chosen to "
                    "stay readable over moving video."},
    {"id": "ticker_bg_opacity", "label": "Background opacity", "type": "number",
     "default": 0.6, "min": 0, "max": 1, "step": 0.1,
     "description": "0 = invisible box (text alone, harder to read over bright "
                    "video), 1 = solid. 0.6 is a good default."},
    {"id": "_section_noise", "type": "info", "label": "🔕 Noise control",
     "description": "Stops a storm from burying you. Each limit has its own "
                    "setting for which severities are allowed to ignore it."},
    {"id": "dedup_window_min", "label": "Repeat window (minutes)", "type": "number",
     "default": 10, "min": 0,
     "description": "If the same alert repeats within this many minutes you get one "
                    "message now, plus a summary of the repeats when the window ends. "
                    "0 turns repeat-collapsing OFF entirely: every repeat is "
                    "its own message."},
    {"id": "dedup_window_critical_min", "label": "Critical repeat window (minutes)",
     "type": "number", "default": "", "min": 0,
     "placeholder": "leave blank to use the window above",
     "description": "Same idea, for critical alerts only. Blank uses the window "
                    "above. 0 means a critical repeat always comes through."},
    {"id": "max_sends_per_hour", "label": "Max sends per hour", "type": "number",
     "default": 20, "min": 1,
     "description": "Alerts over this limit wait and arrive as one summary. Gmail "
                    "relays cap around 500/day; 20/hour is 480/day worst case."},
    {"id": "hourly_cap_bypass", "label": "Hourly cap bypass", "type": "select",
     "default": "critical",
     "options": [{"value": "critical", "label": "Critical only (recommended)"},
                 {"value": "warning", "label": "Critical and warning"},
                 {"value": "none", "label": "Nothing; criticals wait too"}],
     "description": "Which alerts ignore the hourly cap. They do not count "
                    "towards it either, so a storm of them cannot use up the "
                    "budget the rest share."},
    {"id": "quiet_hours", "label": "Quiet hours", "type": "string", "default": "",
     "placeholder": "22:00-07:00",
     "description": "HH:MM-HH:MM in Dispatcharr's own timezone (Settings > System). "
                    "Blank = off. Alerts that wait arrive as one summary when the "
                    "window ends. See quiet hours bypass below for what comes "
                    "through anyway."},
    {"id": "quiet_hours_bypass", "label": "Quiet hours bypass", "type": "select",
     "default": "critical",
     "options": [{"value": "critical", "label": "Critical only (recommended)"},
                 {"value": "warning", "label": "Critical and warning"},
                 {"value": "none", "label": "Nothing; criticals wait too"}],
     "description": "Which alerts come through during quiet hours instead of "
                    "waiting for the window to end."},

    {"id": "_section_privacy", "type": "info", "label": "🔒 Privacy",
     "description": "Hosts from your EPG sources and M3U accounts are redacted "
                    "automatically. Redaction list shows the effective list."},
    {"id": "redact_hostnames", "label": "Additional hostnames to redact",
     "type": "string", "default": "",
     "placeholder": "192.168.1.50, my-proxy.example",
     "description": "Extra hostnames to scrub from outgoing text, on top of the "
                    "auto-derived list; your webhook host, a LAN IP, a reverse "
                    "proxy. A URL at these hosts and the bare hostname in prose "
                    "are both replaced."},

    {"id": "_section_dispatcharr", "type": "info",
     "label": "📡 Dispatcharr events",
     "description": "Notify on Dispatcharr's own system events. Blank = none. Some "
                    "fire on every channel change and will exhaust your hourly cap, "
                    "holding real alerts behind them; see the README before "
                    "adding any."},
    {"id": "events_security", "label": "Notify on blocked M3U/EPG requests",
     "type": "boolean", "default": False,
     "description": "A client request to your playlist/guide endpoints was "
                    "denied by network-access rules. Rare and actionable."},
    {"id": "events_login", "label": "Notify on failed logins",
     "type": "boolean", "default": False,
     "description": "WARNING: failed logins can be triggered by an "
                    "unauthenticated remote party, and critical alerts bypass "
                    "the hourly cap; an attacker controls the volume. Route "
                    "to push, never email."},
    {"id": "events_stream_health", "label": "Notify on channel errors/failovers",
     "type": "boolean", "default": False,
     "description": "Raw channel errors and failovers. LEAVE OFF if you run "
                    "Sentinelarr: it reports the same faults as debounced "
                    "incidents, so both on means two notifications per fault. "
                    "Validate warns if both are on."},
    {"id": "events_recordings", "label": "Notify on recordings",
     "type": "boolean", "default": False,
     "description": "Recording started/ended, with the channel name."},
    {"id": "dispatcharr_events", "label": "Extra events (advanced)", "type": "text",
     "default": "", "placeholder": "m3u_blocked, epg_blocked",
     "description": "Fine-grain escape hatch, added on top of the toggles "
                    "above. Validate checks this: unknown names, "
                    "chatty events, and overlap with Sentinelarr."},

    {"id": "_section_advanced", "type": "info", "label": "⚙️ Advanced",
     "description": "Sane defaults. Leave these alone unless you have a reason."},
    {"id": "spool_poll_s", "label": "Queue check interval (s)", "type": "number",
     "default": 5, "min": 1,
     "description": "How often Newsflasharr checks for new alerts. Lower is faster "
                    "but busier. The default is fine."},
    {"id": "max_event_age_h", "label": "Max event age (hours)", "type": "number",
     "default": 24, "min": 1,
     "description": "An undelivered event older than this is dropped as expired "
                    "-- CRITICALS INCLUDED, with nothing retried afterwards."},
    {"id": "report_expect_days", "label": "Expect a report every N days",
     "type": "number", "default": "", "min": 1,
     "placeholder": "blank = do not monitor",
     "description": "If a plugin emails a scheduled report, set the expected "
                    "cadence in days; blank = off. Status turns red once "
                    "a report that WAS arriving stops; it cannot flag one "
                    "that never arrived, so confirm delivery works once "
                    "before relying on it. The attached file is sent "
                    "verbatim, NOT redacted; never attach credentials."},
    {"id": "report_expect", "label": "Expect a report from each of these",
     "type": "text", "default": "",
     "placeholder": "sentinelarr/weekly_report=7, dustarr/usage_report=1",
     "description": "One entry per scheduled report, as source/event=days. "
                    "Omit =days to use the cadence above. Blank = watch only "
                    "the total, which cannot tell which caller went quiet."},
]

ACTIONS = [
    # Colour carries meaning: green = inspect-only, cyan = sends a test,
    # blue = prints something to read, orange = moves real traffic. Outline for
    # anything read-only, filled for anything that leaves the box.
    {"id": "validate_settings", "label": "Validate settings",
     "description": "Check routing rules and list enabled channels.",
     "button_label": "✅ Validate", "button_variant": "outline",
     "button_color": "green"},
    {"id": "test_notification", "label": "Send test notification",
     "description": "Leave the channel blank and this sends a REAL notification "
                    "through every enabled channel right now; it can page "
                    "your phone and, with ticker in Default channels, paint a "
                    "banner over live TV. Provide a channel name to test only "
                    "that one channel instead.",
     "button_label": "🔔 Test", "button_variant": "filled",
     "button_color": "cyan",
     "confirm": {"title": "Send a real test notification?",
                "message": "Leaving the channel blank runs the full delivery "
                           "pipeline for real: it can page your phone and "
                           "paint a banner over live TV right now."}},
    {"id": "test_ticker", "label": "Test on-screen ticker",
     "description": "Draw a test banner over live video now. Ignores the "
                    "severity and source filters below, so it proves the file "
                    "write and the ffmpeg overlay, not your routing. Only "
                    "devices on an output profile with the drawtext filter can "
                    "see it. VOD never shows a banner.",
     # FILLED since the 2026-07-25 QA review: it was `outline`, which in this
     # plugin's own vocabulary means READ-ONLY, while this paints an
     # undismissable banner on every device using the ticker output profile.
     # It deliberately keeps NO confirm -- drawing a banner is its entire,
     # clearly-labelled function, unlike test_notification whose blank-channel
     # path pages real channels in a way the label does not advertise. See
     # test_actions_that_move_real_traffic_require_confirmation.
     "button_label": "📺 Test ticker", "button_variant": "filled",
     "button_color": "cyan"},
    {"id": "show_ticker_filter", "label": "Ticker filter (copy into Output Profile)",
     "description": "Print the ffmpeg parameters built from the appearance "
                    "settings above. Newsflasharr never edits your Output Profile "
                    "-- paste this into it yourself, then restart the stream.",
     "button_label": "🎬 Ticker filter", "button_variant": "outline",
     "button_color": "blue"},
    {"id": "show_redaction", "label": "Show redaction list",
     "description": "List every hostname currently redacted from outgoing "
                    "notifications, grouped by EPG source and M3U account, "
                    "and refresh that list from Dispatcharr right now. A "
                    "count cannot answer 'is provider.tv in the list?'; "
                    "this prints the hosts.",
     "button_label": "📋 Redaction list", "button_variant": "outline",
     "button_color": "blue"},
    {"id": "show_routing", "label": "Show routing",
     "description": "Print where each kind of notification actually goes, "
                    "resolved per source, so you can see at a glance which "
                    "channel a critical from a given plugin reaches, and "
                    "which rules can never fire.",
     "button_label": "🧭 Show routing", "button_variant": "outline",
     "button_color": "blue"},
    {"id": "show_status", "label": "Show status",
     "description": "Is it alive and is anything stuck? Heartbeat, queue depth, "
                    "how many alerts are waiting, and the most recent "
                    "deliveries; all read from disk.",
     "button_label": "📊 Status", "button_variant": "outline",
     "button_color": "blue"},
    {"id": "on_dispatcharr_event",
     "label": "Dispatcharr event hook",
     "description": "Runs automatically when Dispatcharr emits a system event. "
                    "Clicking it does nothing; use the 'Notify on ...' "
                    "toggles instead.",
     "events": list(dispatcharr_events.EVENTS),
     # Deliberately left visually inert -- clicking it does nothing, so it must
     # not look like the buttons that do.
     "button_label": "(automatic)"},
    {"id": "flush_held", "label": "Flush held notifications",
     "description": "Force-deliver any currently held notifications now.",
     "button_label": "📤 Flush", "button_variant": "filled",
     "button_color": "orange",
     "confirm": {"title": "Flush held notifications?",
                "message": "Force-delivers every currently held notification "
                           "right now, including anything held for quiet "
                           "hours. At 3am that is a phone full of alerts."}},
]


def _coerce_settings(raw):
    """`raw` is Dispatcharr's `context["settings"]` -- plain field values as
    stored in the DB row, unparsed strings for the two list-shaped fields.
    The list-shaped parsing itself is `collector.parse_routing_rules` /
    `parse_default_channels` -- ONE algorithm shared with
    collector.read_settings() (the `_parse_hostnames` pattern), so the
    delivery path and the reporting path cannot drift."""
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(collector.DEFAULTS)
    settings.update(raw)

    # The DEGRADE to [] is deliberate -- a bad value must never stop the
    # collector delivering. What was missing is that it was never REPORTED: an
    # operator pasted the wrong text into the field, the rules were silently
    # discarded, `lint([])` came back clean and validate showed a GREEN toast
    # while their entire routing config was gone. Record the failure under a
    # `_resolved_*` key so the reporters can raise it. (The prefix is
    # load-bearing: Dispatcharr never prunes a stored setting whose field was
    # removed, so an un-namespaced key could be shadowed by a stale stored one.)
    rules, routing_error = collector.parse_routing_rules(
        settings.get("routing_rules", "[]"))
    settings["routing_rules"] = rules
    settings["_resolved_routing_error"] = routing_error

    settings["default_channels"] = collector.parse_default_channels(
        settings.get("default_channels", ""))
    routing.normalize(settings)
    # #20 Phase 2: presets compose into an EFFECTIVE list used for delivery and
    # the readout; the operator's routing_rules is never rewritten. `routing` is
    # unconditionally imported here, so (unlike the collector's guarded path)
    # there is no None branch to default against.
    settings["_effective_routing_rules"] = routing.compose(settings) + settings["routing_rules"]

    # AFTER the update() above, never before: Dispatcharr keeps stored values
    # for fields that no longer exist, so a pre-derivation `quiet_tz` is still
    # in `raw` on any upgraded install.
    collector.inject_derived(settings)
    return settings


def _base_dir(settings):
    """`_base_dir_override` is a TEST-ONLY hook (no plugin.json field exposes
    it to the settings UI): real deploys always use DEFAULT_BASE_DIR."""
    override = settings.get("_base_dir_override")
    return override if override else DEFAULT_BASE_DIR


def _mask_record(record, hosts=None):
    """Best-effort mask of any URL-ish string value in a ledger record before
    it leaves the process via show_status. Ledger records are already
    redacted at write time by Store's `redact` callable, but this is
    defense-in-depth against a stray target URL surviving in e.g. a
    channel-send failure detail.

    `hosts` (the effective redact-hostname list) additionally scrubs BARE
    provider hostnames: Store's at-rest redaction is credentials-only by
    design (the operator's own disk may hold hostnames), but show_status
    output is a display surface that docs/API.md warns gets pasted into
    GitHub issues -- and M3U account NAMES are hostnames, so a ledger title
    can carry one with no "://" anywhere for the mask below to key on."""
    masked = {}
    for key, value in record.items():
        if not isinstance(value, str):
            masked[key] = value
            continue
        if hosts:
            value = channels.redact_hosts(value, hosts)
        if "://" in value:
            value = channels.mask_target(value)
        masked[key] = value
    return masked


def _distinct_selectors(rows):
    """Selectors in row order, de-duplicated -- the empty selector first,
    then each source/event selector once (rows for one selector are
    contiguous, so first-seen order preserves summarize()'s ordering)."""
    seen = []
    for row in rows:
        items = tuple(sorted(row["selector"].items()))
        if items not in [tuple(sorted(s.items())) for s in seen]:
            seen.append(row["selector"])
    return seen


def _unconfigured_channels(effective_rules, default_channels, enabled):
    """Channels referenced by defaults or any rule but not enabled. ONE copy,
    shared by _routing_report and _action_show_routing so the two surfaces
    cannot disagree (spec honesty-C2). Reads the EFFECTIVE rules so a preset's
    channel is included."""
    referenced = set(default_channels or [])
    for rule in effective_rules:
        if isinstance(rule, dict):
            referenced |= set(rule.get("channels") or [])
    return sorted(referenced - set(enabled))


def _presets_in_effect(settings):
    """Human lines for active presets, derived from routing.describe_presets --
    the SAME gate compose uses, so an active preset is exactly one that
    composed a rule (spec honesty-3)."""
    return routing.describe_presets(settings)


class Plugin:
    name = "Newsflasharr"
    version = PLUGIN_VERSION
    # Dispatcharr's loader syncs PluginConfig.description from THIS attribute,
    # not from plugin.json (apps/plugins/loader.py). The two drifted apart and
    # the plugin card showed the older wording for weeks, because nothing
    # compared them. scripts/check_version_sync.py now does.
    description = ("Central notification service: other plugins drop events, "
                   "Newsflasharr routes them to Discord, a webhook, ntfy, "
                   "Apprise, email, or an on-screen banner over live TV, with "
                   "deduplication, storm throttling, quiet hours and "
                   "per-channel retry.")
    fields = FIELDS
    actions = ACTIONS

    def __init__(self):
        """Runs on Dispatcharr's per-request hot path -- the loader
        instantiates Plugin in every uWSGI worker. ensure_collector() is
        idempotent and cheap once a thread is already armed at this version
        ; wrapped so a collector failure can never break
        plugin construction."""
        try:
            collector.ensure_collector(PLUGIN_VERSION, base_dir=DEFAULT_BASE_DIR,
                                        redact=redact)
        except Exception:
            pass

    def stop(self):
        """Called by the loader on disable/uninstall/reload. Signals the
        collector thread armed under this plugin's synthetic runtime module
        to stop. Never raises -- a cleanup failure must not break the loader."""
        try:
            mod = sys.modules.get(collector.RUNTIME_KEY)
            if mod is not None and mod.state is not None:
                mod.state["stop"].set()
        except Exception:
            pass

    @staticmethod
    def _result(ok, message, error=None, **extra):
        """The ONE constructor for an action return.

        `status` and `error` are derived together so they cannot drift: every
        failure carries `error`, which is the only surface that persists (the
        toast is transient AND hardcoded green -- a plugin cannot colour it,
        and raising to force red would violate the never-raises contract and
        put unredacted str(exc) on screen).

        `message` is toast-only and capped by the caller; `error` is never
        capped, because truncating the durable surface defeats its purpose.

        **`details` is FOLDED INTO `message`, because `details` renders
        nowhere.** Measured against the compiled frontend
        (`/app/static/assets/PluginCard-*.js`, Dispatcharr 0.28.2): the
        component handed an action result renders exactly a "Running action..."
        line, `.file` as "Output: <path>", and `.error` in red -- and `message`
        becomes a toast. `details`, `problems` and `checks` appear in NO bundle,
        and nothing anywhere maps over a details array. So UX plan 2's move of
        content off the capped toast and into `details` moved it into the void:
        show_routing built its whole per-source table there, show_redaction its
        host list, validate its 13 inventory lines, show_status its report.
        `error` is not an alternative for a SUCCESS -- it flips `status` and
        renders red -- so `message` is the only surface left.

        Two limits are inherent to that surface and cannot be fixed from a
        plugin: the toast is TRANSIENT, and nothing in the bundle sets
        `whiteSpace`, so the newlines below collapse to spaces in the rendered
        HTML. Folded content is therefore visible but unformatted. That is
        strictly better than invisible, which is what it replaces; a properly
        formatted persistent readout needs an upstream frontend change (the
        `dist` ships compiled) or an nginx-served file.

        `details` stays in the payload: the tests and any API consumer read it.
        """
        out = {"status": "ok" if ok else "error", "message": message}
        if not ok:
            out["error"] = error if error is not None else message
        out.update(extra)
        details = extra.get("details")
        if details:
            # `message` first -- it is the headline (a problems summary from
            # _fit_message, or a one-line verdict); details elaborate it.
            #
            # BOUNDED (QA review 2026-07-25). The unbounded fold made failures
            # visible but pushed validate to 1751 chars in a TRANSIENT toast
            # whose newlines COLLAPSE to one paragraph -- 56% worse than the
            # 1124 chars the earlier redesign existed to fix, i.e. unreadable
            # rather than invisible. The cap keeps the fold's benefit (the first
            # and most decision-relevant lines are seen) without the wall, and
            # the drop is NAMED -- a silent truncation would be this codebase's
            # own signature defect. `details` itself stays COMPLETE in the
            # payload for the API and the tests.
            #
            # Preserve the RAW headline BEFORE the fold below overwrites
            # `message`. Only needed when a fold actually happens: with no
            # details, `message` IS the headline. Keeping it conditional also
            # keeps _result's no-details return shape unchanged.
            out["_headline"] = message
            # marker_target is None here: _result cannot know the action, so it
            # cannot name the file. Task 3 RE-folds with the real path once the
            # write has succeeded.
            out["message"] = Plugin._fold(message, details,
                                           Plugin.DETAIL_CAP, None)
        return out

    def run(self, action, params=None, context=None):
        params = params if isinstance(params, dict) else {}
        raw_settings = (context or {}).get("settings") or {}

        # Dispatcharr fires this for ALL 17 event types regardless of what
        # the operator subscribed to (`events` is a static manifest list), and
        # it arrives on the streaming dispatch path. The reject must therefore
        # happen BEFORE _coerce_settings, which runs collector.inject_derived
        # -> read_timezone() + read_redact_hosts(): TTL-cached at 300s, but on
        # every expiry the next event pays a full EPGSource + M3UAccount scan.
        # Below this branch an unsubscribed event costs one dict lookup and a
        # string split -- no derivation, no DB, no file I/O. (gevent does not
        # make regular-file I/O non-blocking, so reaching notify()'s spool
        # write here would block the whole hub.)
        if action == "on_dispatcharr_event":
            return self._on_dispatcharr_event(raw_settings, params)

        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            result = self._result(False, f"unknown action: {action}")
            # An unmapped `action` is NOT manifest-constrained here -- it can
            # be attacker-shaped ("../evil", rejected by readout.safe_action
            # as "BadAction") or merely a typo that happens to be filename-
            # safe ("foo"). Routing either through _publish_readout is wrong
            # two different ways: "BadAction" is readout.write's REJECTION
            # reason, not a filesystem fact, and _note_readout_failure would
            # still record it as a write failure -- flipping show_status's
            # `status` to `error` for the full 24h _RECENT_FAILURE_S window
            # over an id that was never real to begin with. And a safe-but-
            # unknown id would actually SUCCEED, leaving an orphan
            # "<action>.txt" nothing ever prunes in a directory the operator
            # browses in Explorer. Neither is a readout concern, so no write
            # is attempted at all -- only a pre-existing stale file for this
            # exact id is stubbed (guarded by the same safe_action check
            # _stub_stale_readout already applies), so a RENAMED action's old
            # file cannot be mistaken for current.
            try:
                self._stub_stale_readout(action, {}, "UnknownAction")
            except Exception:
                pass
            return result

        settings = {}
        # _coerce_settings is INSIDE the try: it now touches the DB (deriving
        # the timezone and provider hostnames), and it used to be called
        # outside, where an exception escaped run() entirely and violated the
        # never-raises contract -- a DB hiccup would have 500'd every button
        # in the UI, including show_status, the one diagnostic the operator
        # has.
        try:
            settings = _coerce_settings(raw_settings)
            result = handler(settings, params)
        except Exception as exc:
            # type name only -- str(exc) on a DB error routinely carries a
            # host, a user, or a whole connection string, and this reaches the
            # operator's screen. Same rule as _note_settings_error.
            reason = f"{action} raised {type(exc).__name__}"
            # validate_settings' contract is that `checks`/`details`/`problems`
            # are ALWAYS present on its return -- a consumer indexing
            # r["checks"] must not get a KeyError just because _coerce_settings
            # (which now touches the DB) hiccuped. Every check reports
            # `not_checked`, which is exactly what happened.
            extra = {}
            if action == "validate_settings":
                extra = {"checks": {k: "not_checked" for k in self._VALIDATE_CHECKS},
                         "details": [], "problems": [reason]}
            result = self._result(False, f"FAILED: {type(exc).__name__}",
                                  error=reason, **extra)
        # OUTSIDE the try on purpose, and after BOTH exits: the failure readout
        # is the one worth keeping. _publish_readout owns its own try/except.
        return self._publish_readout(action, result, settings)

    def _on_dispatcharr_event(self, raw_settings, params):
        """Dispatcharr's system-event hook. Never raises.

        Reached from Dispatcharr's event dispatcher rather than a UI click.
        Both `connect.trigger_event` and `_dispatch_system_event_integrations`
        already catch, and on gevent uWSGI the whole dispatch is a spawned
        greenlet, so a raise here cannot break stream teardown -- but a
        notification plugin is the last thing that should be relying on
        somebody else's `except`, so this owns its own.
        """
        try:
            raw_settings = raw_settings if isinstance(raw_settings, dict) else {}
            event = (params or {}).get("event")
            if not event:
                # A manual button click: no event, no payload. Must NOT be
                # read as "all events".
                return {"status": "ok",
                        "message": "This action runs automatically when "
                                   "Dispatcharr emits a system event. Choose "
                                   "which ones with the 'Notify on ...' toggles."}

            # THE one resolver (union of group toggles + the CSV) -- the same
            # function validate's report uses, so the two surfaces cannot
            # drift. Still hot-path-cheap: dict reads + a set union, no
            # coercion, no ORM (spec s2).
            enabled, _unknown = dispatcharr_events.selected_events(raw_settings)
            if event not in enabled:
                return {"status": "ok", "skipped": True}

            title, body, severity, dedup_key = dispatcharr_events.compose(
                event, (params or {}).get("payload"))
            delivered = notify(source="dispatcharr", title=title, event=event,
                               body=body, severity=severity,
                               dedup_key=dedup_key,
                               base_dir=_base_dir(raw_settings))
            return {"status": "ok", "spooled": bool(delivered)}
        except Exception as exc:
            # A broken handler is otherwise completely silent: Plugin.run
            # swallows the error dict and the dispatcher discards the return
            # value, so nobody ever reads it. Count it where show_status can
            # find it.
            self._count_event_error()
            # via _result: `status` renders NOWHERE, only `error` does.
            return self._result(False, "Event handler failed",
                                error=redact(str(exc)))

    @staticmethod
    def _count_event_error():
        try:
            st = store.Store(DEFAULT_BASE_DIR, redact=redact)
            now = time.time()
            state = st.load_state(now)
            g = state.setdefault("gates", {})
            g["event_handler_errors"] = g.get("event_handler_errors", 0) + 1
            st.save_state(state, now)
        except Exception:
            pass

    # -- actions -------------------------------------------------------------

    # Fixed iteration order for every check-map/details/problems assembly
    # below -- also the order the "no early return" contract runs them in.
    _VALIDATE_CHECKS = ("routing", "timezone", "noise", "smtp", "events", "ticker",
                        "redaction", "liveness")

    MESSAGE_CAP = 120
    # MEASURED, not assumed. Dispatcharr mounts Mantine's Notifications with
    # notificationMaxHeight at its default 200px: 200 - 20 padding - 22 title
    # = ~158px / 20.3px per line = 7 lines, at ~40 chars per line for the
    # configured 350px container = ~280 characters. 600 was tuned to a budget
    # more than twice the real one. See spec section 1.2 for the full geometry.
    #
    # This is a TOTAL budget including the headline, not a details budget.
    DETAIL_CAP = 250

    # Used ONLY when no file was written: with nothing to fall back to, the
    # tighter budget would discard content that now has nowhere else to live,
    # so this reverts to the pre-file behaviour and accepts its clipping.
    DETAIL_CAP_FALLBACK = 600

    # The readout directory. NOT /data/logos: nginx serves that UNAUTHENTICATED
    # to the whole LAN, and validate_settings.txt + show_redaction.txt both
    # carry the derived provider hostname list (spec section 3.7).
    READOUT_DIR = readout.DEFAULT_DIR

    def _readout_dir(self, settings):
        return settings.get("_readout_dir_override") or self.READOUT_DIR

    def _readout_header(self, action, settings):
        """Version, local time, the WINDOWS path, and the sensitivity line.

        The timezone comes from the ALREADY-RESOLVED settings key, not from a
        fresh dispatcharr.read_timezone() call: that would duplicate the
        resolution _coerce_settings has already done and add a DB scan to every
        action click. Dispatcharr's own zone is the only correct one here --
        settings.TIME_ZONE and the container TZ are both UTC and are not what
        the operator reads.

        The header names the CONTAINER path only. An earlier version also
        printed a Windows drive mapping, which was correct on exactly one
        machine: it was that deployment's own mapping of Dispatcharr's
        `/config`, it was wrong for every other installation, and it was wrong
        even there whenever `_readout_dir_override` was in play (a test or
        development base directory), because the file would sit somewhere else
        entirely while the header still named the drive.
        """
        tz_name = settings.get("_resolved_quiet_tz") or "UTC"
        try:
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo(tz_name))
        except Exception:
            tz_name = "UTC (could not resolve Dispatcharr's time zone)"
            now = datetime.datetime.now(datetime.UTC)
        return "\n".join([
            f"Newsflasharr {PLUGIN_VERSION}: {action}",
            f"Written {now.strftime('%Y-%m-%d %H:%M:%S')} {tz_name}",
            f"Inside the container: /config/newsflasharr/{action}.txt "
            "(reachable from the host wherever you mounted Dispatcharr's "
            "/config volume).",
            "This file may name your provider hostnames. Do not paste it into "
            "a public issue without reading it first.",
            "-" * 60,
            "",
        ])

    def _publish_readout(self, action, result, settings):
        """Write the complete readout and point the card at it. NEVER RAISES.

        See _publish_readout_inner for the composition. This wrapper exists
        because a convenience file write must not be able to fail an action:
        inside run()'s try an exception renders a healthy Validate red, and
        outside it the exception escapes run() and 500s the UI.

        The except branch is ALSO the last line of defence against a stale
        file: header composition (`_readout_header`) or the body join (a
        caller's `str(d)`) can raise before `readout.write` is ever reached,
        which is a DIFFERENT failure shape than a write failure but must
        degrade exactly the same way -- no `Output:` line, a note the
        operator can see, Task 5's record, and a stubbed stale file, not
        silence. Each recovery step gets its OWN try/except: one failing
        (e.g. `result` not being a dict) must not swallow the others.
        """
        try:
            return self._publish_readout_inner(action, result, settings)
        except Exception as exc:
            reason = f"PublishError/{type(exc).__name__}"
            if isinstance(result, dict):
                result.pop("_headline", None)
                result.pop("_readout", None)
                try:
                    note = f"[readout file NOT written: {reason}]"
                    result["message"] = note + " " + str(result.get("message") or "")
                except Exception:
                    pass
            try:
                self._note_readout_failure(settings, action, reason)
            except Exception:
                pass
            try:
                self._stub_stale_readout(action, settings, reason)
            except Exception:
                pass
            return result

    def _publish_readout_inner(self, action, result, settings):
        if not isinstance(result, dict):
            return result
        headline = result.pop("_headline", None)
        body_override = result.pop("_readout", None)
        if headline is None:
            headline = result.get("message") or ""
        problems = result.get("problems") or []
        details = result.get("details") or []

        if body_override is not None:
            body = body_override
        else:
            lines = [str(headline)]
            if problems:
                lines.append("")
                lines.append(f"Problems ({len(problems)}):")
                lines.extend(f"  - {p}" for p in problems)
            if details:
                lines.append("")
                lines.extend(str(d) for d in details)
            body = "\n".join(lines)

        base = self._readout_dir(settings)
        path, reason = readout.write(
            action, self._readout_header(action, settings) + body,
            base_dir=base)

        if path:
            result["file"] = path
            if details:
                # RE-fold now that the marker can name a file that exists.
                result["message"] = self._fold(headline, details,
                                               self.DETAIL_CAP, path)
            return result

        # Degraded: no file key, so no stale "Output:" line. The note is
        # PREPENDED -- appended it lands in the clipped region of a ~280-char
        # toast, leaving absence-of-an-Output-line as the only signal.
        self._note_readout_failure(settings, action, reason)
        note = f"[readout file NOT written: {reason}]"
        if body_override is not None:
            # Do NOT swallow the payload. For show_ticker_filter the parameters
            # string would otherwise exist in NO surface at all while the action
            # reported success -- the exact failure family this design ends.
            result["message"] = note + " " + body_override
        else:
            result["message"] = note + " " + self._fold(
                headline, details, self.DETAIL_CAP_FALLBACK, None)
        self._stub_stale_readout(action, settings, reason)
        return result

    def _stub_stale_readout(self, action, settings, reason):
        """Replace a previous run's file so it cannot be read as current.

        Builds its own path from `action` rather than going through
        readout.write, so it needs the SAME guard readout.write applies to
        itself -- action reaches here from Plugin.run()'s unknown-action
        branch, which is not manifest-constrained (see readout.safe_action's
        docstring). No-op on an unsafe action: there is nothing "this
        action's stale file" can mean for a value that was never a real
        action id, and reaching os.path.join with it is the exact defect
        this guard exists to prevent.
        """
        try:
            if readout.safe_action(action) is None:
                return
            path = os.path.join(self._readout_dir(settings),
                                f"{action}.txt")
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(f"Newsflasharr {PLUGIN_VERSION}: {action}\nreadout NOT written this "
                             f"run: {reason}\n")
        except Exception:
            pass

    def _note_readout_failure(self, settings, action, reason):
        """Record a failed readout write where show_status can find it.

        Deliberately NOT state.json: the collector owns that file and
        rewrites it 2-4x per tick from the leader worker, while this runs in
        whichever web worker served the click -- an action write would race
        it and be clobbered. This lands in the RUNTIME dir, a different
        filesystem from the one that just failed, which is why it can still
        be written.
        """
        try:
            base = _base_dir(settings)
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, "readout_health.json")
            try:
                cur = json.loads(open(path, encoding="utf-8").read())
            except Exception:
                cur = {}
            # Concurrent web workers can race this read-modify-write and lose
            # an increment (two workers both read errors=N, both write N+1).
            # This is therefore a BEST-EFFORT health signal, not an exact
            # audit count. The write itself stays atomic (tmp+replace), so a
            # lost update costs a count, never a corrupt file -- do not "fix"
            # this with a lock.
            cur["errors"] = int(cur.get("errors") or 0) + 1
            cur["last"] = {"action": action, "reason": reason,
                           "ts": time.time()}
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(cur))
            os.replace(tmp, path)
        except Exception:
            # Swallowed only because the immediate failure is ALREADY
            # reported in the prepended toast note; this is the durable
            # second copy.
            pass

    @staticmethod
    def _fold(headline, lines, cap, marker_target):
        """Join a headline and detail lines into one toast-sized blob.

        `cap` is a TOTAL budget including the headline, not a details budget.

        `marker_target` names where the dropped lines actually went. None means
        NOWHERE, and that must read differently: a marker promising "see the
        file" when no file was written sends the operator to a stale artefact
        from a previous run and invites them to read it as current.
        """
        parts = [str(headline)] if headline else []
        kept, dropped = [], 0
        used = len(parts[0]) if parts else 0
        for line in lines:
            line = str(line)
            if used + len(line) + 1 > cap:
                dropped += 1
                continue
            kept.append(line)
            used += len(line) + 1
        parts.extend(kept)
        if dropped:
            where = f"; see {marker_target}" if marker_target else ""
            parts.append(f"(+{dropped} more line(s){where})")
        return "\n".join(parts)

    @classmethod
    def _fit_message(cls, parts, cap=None):
        """Join short summaries into one toast-sized line.

        The COUNT is computed before any truncation and always leads, and the
        marker names how many were dropped -- never a bare slice, which is
        this codebase's silent-drop signature. The marker is ASCII and its
        length is inside the budget.

        Two invariants the callers must not have to remember:

        * The result NEVER contains a newline or a run of whitespace. The
          toast is one line; a problem string with an embedded newline would
          break it silently, so whitespace is collapsed HERE rather than
          relying on every current problem string happening to be one line.
        * The result ALWAYS names at least one real problem. When not even
          the first part fits whole, it is hard-truncated rather than
          dropped: `"2 problems:  (+2 more)"` -- count and marker with no
          content, which is what the previous version emitted -- tells the
          operator strictly nothing and is this plugin's silence failure
          mode wearing a number.
        """
        cap = cls.MESSAGE_CAP if cap is None else cap
        # Collapse first: length budgeting must measure the string that is
        # actually emitted, and a multi-line part must not survive as one.
        parts = [c for c in (" ".join(str(p).split()) for p in parts if p) if c]
        if not parts:
            return "OK"
        total = len(parts)
        head = f"{total} problems: " if total > 1 else ""
        out = []
        for part in parts:
            dropped = total - (len(out) + 1)
            marker = f" (+{dropped} more)" if dropped else ""
            if len(head + "; ".join(out + [part]) + marker) > cap:
                break
            out.append(part)
        if not out:
            marker = f" (+{total - 1} more)" if total > 1 else ""
            room = cap - len(head) - len(marker)
            if room >= 8:
                return head + parts[0][:room - 3].rstrip() + "..." + marker
            # Degenerate cap: the problem text still beats the bookkeeping.
            return parts[0][:cap]
        text = head + "; ".join(out)
        if len(out) < total:
            text += f" (+{total - len(out)} more)"
        return text[:cap]

    def _action_validate_settings(self, settings, params):
        """Run every check unconditionally -- no early return.

        The old code aborted on the first routing-lint problem, so one
        malformed rule silently skipped SMTP/events/ticker/redaction/
        liveness, and a short error message looked identical to all of them
        having passed. Now every check always runs and always contributes a
        verdict to `checks`.

        ANY check reporting `problem` fails the action (status=="error"),
        because `error` is the only surface that persists -- the toast is
        transient AND hardcoded green. An earlier version gated status on
        routing alone, which meant "SMTP: no valid recipient -- email is OFF",
        an unparseable SMTP host, a non-existent subscribed event, a dead
        collector and a missing `ticker.txt` (whose own text says the channel
        will HANG) all rendered as a green toast and nothing else. A check
        whose text begins "SMTP: ERROR --" cannot return status "ok".

        `problem` therefore means one thing only: THE OPERATOR CAN FIX THIS.
        A check that could not run says `not_checked` (and also fails, since
        "I could not tell" must not read as "fine"), and a check that ran but
        on incomplete data says `degraded` -- notably hostname derivation, which
        Amendment A1 deliberately degrades rather than failing closed. A DB
        hiccup there is not a configuration mistake and must not paint the
        operator's settings page red; it still lands loudly in
        `problems`/`message`, which is what A1 requires of it.

        Return shape: `checks` maps every check name to its verdict;
        `problems` holds the problem/degradation text in check order;
        `details` holds inventory ONLY. The two lists are disjoint -- an
        earlier version put each check's `problems` into BOTH, so a UI
        rendering both showed every problem twice. The intended render is
        `problems` first, then `details`, which preserves the
        degradation-warnings-lead-inventory ordering that duplication was
        introduced to get.
        """
        reports = {
            "routing": self._routing_report(settings),
            "timezone": self._timezone_report(settings),
            "noise": self._noise_report(settings),
            "smtp": self._smtp_report(settings),
            "events": self._events_report(settings),
            "ticker": self._ticker_report(settings),
            "redaction": self._redaction_report(settings),
            "liveness": self._liveness_report(settings),
        }

        checks = {key: reports[key]["verdict"] for key in self._VALIDATE_CHECKS}
        details = []
        problems = []
        for key in self._VALIDATE_CHECKS:
            rep = reports[key]
            if rep["verdict"] == "skipped":
                continue
            details.extend(rep["details"])
            problems.extend(rep["problems"])

        tz_info = settings.get("_resolved_tz_info") or {}
        tz_name = settings.get("_resolved_quiet_tz") or "UTC"

        # `not_checked` fails too: a check that RAISED could not confirm the
        # config is good, and "I could not tell" rendered as a green toast is
        # the same silence. `degraded` does not -- it ran, it just ran on
        # incomplete data the operator cannot do anything about.
        ok = not any(v in ("problem", "not_checked") for v in checks.values())
        message = self._fit_message(problems)
        return self._result(
            ok, message, error="; ".join(problems) if problems else None,
            checks=checks, details=details, problems=problems,
            timezone=tz_name, timezone_ok=bool(tz_info.get("ok", True)))

    _BYPASS_LABELS = {"critical": "critical only",
                      "warning": "critical and warning",
                      "none": "nothing (everything waits)"}

    @classmethod
    def _noise_report(cls, settings):
        """Inventory the noise thresholds.

        `critical` and `warning` are both `ok` -- letting warnings through is a
        legitimate operator choice, not a misconfiguration.

        **`none` is a `problem`, deliberately**, even though the operator chose
        it on purpose. It means criticals wait too, which is a genuine reduction
        in alerting guarantees, and `error` is the ONLY surface that persists:
        `message` is toast-only and capped, and `problems`/`details` render
        nowhere. A deliberate non-error warning would therefore render nowhere
        at all -- the silence this codebase keeps producing. Setting it back is
        a one-click fix, which is exactly what `problem` is defined to mean.
        """
        check = "Noise"
        try:
            details, problems = [], []
            for key, label in (("quiet_hours_bypass", "Quiet hours bypass"),
                               ("hourly_cap_bypass", "Hourly cap bypass")):
                raw = str(settings.get(key) or "").strip().lower() or "critical"
                details.append(
                    f"{label}: {cls._BYPASS_LABELS.get(raw, raw)}")
                if raw == "none":
                    problems.append(
                        f"{check}: {label} is set to NONE. CRITICAL alerts "
                        f"are held too, not delivered.")

            # The two values that actually DECIDE whether an alert is
            # suppressed or held appeared on NO surface at all -- the
            # operator could not see the thresholds his alerts were
            # being judged against. Read through the same _num guard
            # gates uses, so the report cannot claim a value gates
            # would not use.
            cap = gates._num(settings, "max_sends_per_hour", 20)
            window = gates._num(settings, "dedup_window_min", 10)
            details.append(
                f"Throttles: at most {cap} sends/hour; repeats of the "
                f"same alert collapse for {window} minute(s).")

            # Blank and 0 mean DIFFERENT things and must never render alike:
            # blank follows the global window, 0 means criticals never collapse.
            raw = settings.get("dedup_window_critical_min")
            if raw in (None, ""):
                window = "same as Repeat window"
            else:
                try:
                    minutes = max(0, int(raw))
                except (TypeError, ValueError):
                    window = "same as Repeat window"
                else:
                    window = ("every repeat delivered" if minutes == 0
                              else f"{minutes} min")
            details.append(f"Critical repeat window: {window}")
            return {"verdict": "problem" if problems else "ok",
                    "details": details, "problems": problems}
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    @classmethod
    def _routing_report(cls, settings):
        check = "Routing"
        try:
            problems = routing.lint(
                settings["routing_rules"], channels.KNOWN,
                default_channels=settings.get("default_channels"))
            enabled = channels.enabled_channels(settings)
            enabled_desc = ", ".join(enabled) if enabled else "none"
            rule_count = len(settings["routing_rules"])
            details = [f"{len(enabled)} channel(s) enabled ({enabled_desc}), "
                       f"{rule_count} routing rule(s)."]
            # THE SEAM. This judges context["settings"] -- what the page
            # POSTed, which the frontend sends on EVERY action click and
            # whose failure it swallows -- while the collector delivers
            # using the PERSISTED row. So a clean report can describe a
            # config that was never saved, and the liveness lines below
            # describe a process running the old one. Nothing marked it.
            details.append(
                "Checked the settings ON SCREEN, which may not be what "
                "is saved; reload this page if you are unsure, then "
                "Validate again.")

            # "Enabled" only ever meant "the box is non-blank". A channel whose
            # URL has no scheme is counted enabled, shown as a live route, and
            # rejected at SEND time on every event -- delivering nothing,
            # forever, with no surface anywhere. Uses channels.safe_url, the
            # same predicate the builders use, so this cannot drift from
            # delivery.
            for name, reason in channels.unusable_channels(settings):
                problems.append(f"Channel {name}: {reason}")

            # A value that failed to parse was DISCARDED, so `lint` sees []
            # and reports nothing. Surface it first -- it is strictly worse
            # than a lint problem: every rule is gone, not just one.
            # Does the configuration actually CONNECT? Everything above this
            # is an inventory: it counts channels and rules and never asks
            # whether a routed name reaches a configured channel. `lint` checks
            # membership in channels.KNOWN, which is config-INDEPENDENT -- so a
            # perfectly-spelled but unconfigured name passed, which is the same
            # hole one layer over from the 'Ntfy' outage.
            effective = settings.get("_effective_routing_rules",
                                     settings.get("routing_rules") or [])
            enabled_set = set(enabled)
            if not enabled_set:
                problems.append(
                    f"{check}: no channel is configured; every event is held "
                    f"and nothing can be delivered.")
            else:
                unconfigured = _unconfigured_channels(
                    effective, settings.get("default_channels"), enabled)
                if unconfigured:
                    problems.append(
                        f"{check}: routed to {', '.join(unconfigured)} which "
                        f"is not configured; those events reach nothing.")

                # The shipped DEFAULT for default_channels is blank, so a fresh
                # install routes every event to [] and ledgers `no_route`
                # forever while this check reported ok.
                dead = [sev for sev in ("critical", "warning", "info")
                        if not [c for c in routing.route(
                            {"severity": sev, "kind": "event"},
                            effective,
                            settings.get("default_channels") or [])
                            if c in enabled_set]]
                if dead:
                    problems.append(
                        f"{check}: {', '.join(dead)} events reach no channel "
                        f"at all; set Default channels or add a rule.")

            # A preset value that is set, non-'none', and not a known escalation
            # composes to NOTHING and is caught by neither lint (raw only) nor
            # the unconfigured check (no channel ever appears) -- the Ntfy shape
            # (spec §6). Report it explicitly.
            raw_esc = str(settings.get("critical_extra_channels") or "none").strip().lower()
            if raw_esc != "none" and raw_esc not in routing._ESCALATION:
                problems.append(
                    f"{check}: 'Critical alerts also go to' is set to "
                    f"{settings.get('critical_extra_channels')!r}, which is not a "
                    f"valid choice; criticals are NOT being escalated. Valid: "
                    f"{', '.join(routing._ESCALATION)}.")

            parse_error = settings.get("_resolved_routing_error")
            if parse_error:
                problems.insert(0, (
                    f"{check}: Routing rules could not be read ({parse_error}) "
                    f"-- your typed rules were discarded; events route to the "
                    f"default channels (plus any active presets) only."))

            # Positive picture: where a no-rule event of each severity lands.
            # Built from the SAME summarize() the show_routing action uses, so
            # the two surfaces cannot drift. The compact line shows only the
            # generic path; per-source resolution needs the full action, so
            # point at it rather than imply this line is the whole story.
            routing_summary = routing.summarize(
                effective, settings.get("default_channels") or [],
                enabled=enabled, known_channels=None)
            generic = [r for r in routing_summary["rows"] if not r["selector"]]
            resolved = "; ".join(
                f"{'all' if r['severities'] == 'any' else r['severities']} -> "
                f"{', '.join(r['channels']) or 'nothing'}" for r in generic)
            suffix = (" (source-scoped rules exist: run Show routing)"
                      if any(r["selector"] for r in routing_summary["rows"]) else "")
            details.append(f"Routing (no-rule events): {resolved}."
                           f" See Show routing for the full picture.{suffix}")
            for line in _presets_in_effect(settings):
                details.append(line)

            verdict = "problem" if problems else "ok"
            summary = (f"{len(problems)} problem(s)" if problems
                       else f"{len(enabled)} channel(s), {rule_count} rule(s)")
            return cls._report(check, verdict, summary, details, problems)
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    @staticmethod
    def _selector_label(selector):
        if not selector:
            return "any source, no matching rule"
        return ", ".join(f"{k}={v}" for k, v in sorted(selector.items()))

    @classmethod
    def _render_routing(cls, summary, broken=(), escalation_channels=()):
        """ASCII detail lines for the show_routing readout. `broken` is the RAW
        lint result (not summary['broken'], which is suppressed). Critical rows
        that carry an escalation channel are marked so 'why did my exclusive
        rule get smtp?' is answered inline (spec §7)."""
        esc = set(escalation_channels)
        lines = []
        defaults = summary["defaults"]
        lines.append("Default channels: "
                     + (", ".join(defaults) if defaults else "(none)"))
        lines.append("")
        lines.append("Where events go (resolved, per source):")
        for sel in _distinct_selectors(summary["rows"]):
            lines.append("  " + cls._selector_label(sel))
            for row in summary["rows"]:
                if row["selector"] != sel:
                    continue
                chans = ", ".join(row["channels"]) if row["channels"] else "(nothing)"
                sev = ("any severity" if row["severities"] == "any"
                       else row["severities"])
                marker = ("   (reaches nothing; no configured channel)"
                          if row["reaches_nothing"] else "")
                # provenance: an escalation channel on a critical/any row.
                # Shown even when the row also reaches nothing (spec §7: this
                # is exactly the "why did my exclusive rule get smtp" case,
                # and it stays the most useful when the added channel turns
                # out to be dead too).
                added = [c for c in row["channels"]
                         if c in esc and row["severities"] in ("critical", "any")]
                prov = (f"   ({', '.join(added)} via critical escalation)"
                        if added else "")
                lines.append(f"    {sev:<12} -> {chans}{marker}{prov}")
        if broken:
            lines.append("")
            lines.append("Rules that can never fire:")
            for msg in broken:
                lines.append(f"  {msg}")
        return lines

    @classmethod
    def _timezone_report(cls, settings):
        """The typo didn't disappear when the `quiet_tz` field did, it moved
        to Dispatcharr's own settings UI: set_system_time_zone accepts any
        string.

        What a bad zone actually does -- an earlier version of this docstring
        got it wrong: `dispatcharr.read_timezone` zoneinfo-VALIDATES and
        returns `tz=FALLBACK_TZ` with `ok=False`, so `_resolved_quiet_tz` is
        ALWAYS a zone `zoneinfo` accepts and `gates.in_quiet` never reaches
        its except branch. Quiet hours therefore do NOT stop working -- the
        window is evaluated in the WRONG ZONE. On a box configured
        America/Chicago that is a 5-6 hour shift, so a configured
        `23:00-07:00` runs at roughly 18:00-02:00 local: alerts suppressed
        through prime time and delivered overnight. That is arguably worse
        than stopping, which is why it is a `problem` and not a warning.

        It is a `problem` ONLY when `quiet_hours` is set. With quiet hours off
        the zone affects nothing at all, and failing Validate over a setting
        with zero effect trains the operator to ignore red -- so it degrades
        to a `detail`: still visible, not blocking.
        """
        check = "Timezone / quiet hours"
        try:
            tz_info = settings.get("_resolved_tz_info") or {}
            tz_name = settings.get("_resolved_quiet_tz") or "UTC"
            quiet_hours = settings.get("quiet_hours") or ""

            details = []
            problems = []

            # PARSE it. This used to echo the operator's string back as
            # "Quiet hours: 10pm-7am, evaluated in America/Chicago" -- positive
            # confirmation that a window which silently never fires is armed.
            # `_parse_quiet_hours` returns None for anything it cannot read,
            # and gates.in_quiet then returns False forever.
            if quiet_hours and gates.Gates._parse_quiet_hours(quiet_hours) is None:
                problems.append(
                    f"{check}: Quiet hours {quiet_hours!r} could not be read "
                    f"(expected HH:MM-HH:MM, 24-hour); quiet hours are OFF.")
            if not tz_info.get("ok", True):
                bad = (f"Dispatcharr's timezone {tz_info.get('raw')!r} is not a valid "
                       f"IANA name; falling back to {tz_name}.")
                if quiet_hours:
                    problems.append(
                        f"WARNING: {bad} Quiet hours {quiet_hours} are still enforced, "
                        f"but the window is evaluated in {tz_name} instead of your own "
                        f"zone, so it applies at the wrong time of day.")
                else:
                    # Quiet hours are off: the zone decides nothing. Say it,
                    # do not fail Validate over it.
                    details.append(f"{bad} Quiet hours are off, so nothing is "
                                   f"affected, but fix it before enabling them.")
            elif quiet_hours and not problems:
                details.append(f"Quiet hours: {quiet_hours}, evaluated in {tz_name} "
                               f"(from Dispatcharr Settings > System).")
            else:
                details.append(f"Quiet hours: off. (Dispatcharr timezone is {tz_name}.)")

            verdict = "problem" if problems else "ok"
            if problems:
                summary = "invalid timezone"
            elif not tz_info.get("ok", True):
                summary = f"{tz_name} (Dispatcharr's is invalid; quiet hours off)"
            else:
                summary = tz_name
            return cls._report(check, verdict, summary, details, problems)
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    @classmethod
    def _liveness_report(cls, settings):
        """Is the collector actually ticking? `__init__` swallows an
        `ensure_collector` failure (it must never break
        plugin construction), so nothing else tells the operator the
        collector never armed -- and the first-run path ends on Validate.

        Keyed on `state.json`'s own `last_tick` field (seconds epoch, written
        every tick by Collector._tick), not the file's OS mtime -- mtime
        would also move on show_status's ledger/ticker writes and is
        awkward to control from a test.
        """
        check = "Collector liveness"
        try:
            base_dir = _base_dir(settings)
            st = store.Store(base_dir, redact=redact)
            now = time.time()
            state = st.load_state(now)

            threshold = collector.heartbeat_threshold(settings)

            details = []
            problems = []
            last_tick = state.get("last_tick")
            if not last_tick:
                problems.append(
                    "Collector liveness: no tick recorded yet; the collector "
                    "may not have run. It arms itself automatically on the next "
                    "request; try Validate again in a few seconds.")
            else:
                age = now - last_tick
                if age > threshold:
                    problems.append(
                        f"Collector liveness: last tick was {age:.0f}s ago "
                        f"(threshold {threshold:.0f}s); the collector may be "
                        f"stuck. See Show status for detail.")
                else:
                    details.append(f"Collector liveness: OK, last tick {age:.0f}s ago.")

            verdict = "problem" if problems else "ok"
            summary = "stale" if problems else "ticking"
            return cls._report(check, verdict, summary, details, problems)
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    @staticmethod
    def _report(check, verdict, summary, details=None, problems=None):
        """The ONE constructor for a report-helper return -- mirrors _result's
        role for actions. Every caller of a report helper (Task 3) can rely on
        this exact key set regardless of which helper produced it, and
        `skipped` is always a VALUE here, never an absent key: a check that
        did not run must be distinguishable from one that quietly passed.

        Verdict vocabulary, and the distinction that decides whether Validate
        goes red: `ok` · `skipped` (not configured, deliberately silent) ·
        `problem` (THE OPERATOR CAN FIX THIS -- the only verdict that fails
        the action) · `degraded` (ran, but on incomplete data through no
        fault of the config) · `not_checked` (raised)."""
        return {"check": check, "verdict": verdict, "summary": summary,
                "details": list(details or []), "problems": list(problems or [])}

    @classmethod
    def _report_not_checked(cls, check, exc):
        # type name only in `problems` -- str(exc) on a DB/parse error
        # routinely carries a host, a user, or a whole connection string
        # (same rule as _note_settings_error and Plugin.run's own except).
        # `summary` still gets human framing (review finding on Task 2's
        # rebuild): a bare exception type with no explanation of what failed
        # is exactly the kind of silent-looking output this codebase keeps
        # tripping over, so the toast/summary line says a check could not be
        # checked, never just "RuntimeError" on its own.
        return cls._report(check, "not_checked",
                           f"could not be checked ({type(exc).__name__})",
                           problems=[type(exc).__name__])

    @classmethod
    def _ticker_report(cls, settings):
        """Silent (skipped) unless the ticker is enabled."""
        check = "On-screen ticker"
        try:
            if not settings.get("ticker_enabled"):
                return cls._report(check, "skipped", None)

            t = ticker.from_settings(DEFAULT_BASE_DIR, settings)
            # `or`, not a two-arg get: a present-but-None value (the trap that
            # already bit this very field) would render "severity None".
            # Resolved, not echoed: an unrecognised value used to mean
            # rank 0 -- draw EVERYTHING -- while this printed it back as
            # if in force.
            _sev_rank, _sev_eff, _sev_reason = ticker.resolve_min_severity(
                settings.get("ticker_min_severity"))
            details = [f"On-screen ticker: ON, {t.ttl_s:.0f}s banner, minimum "
                       f"severity {_sev_eff}."]
            problems = []

            # The SAME resolver render_filter uses, so this line can never claim
            # a position the filter did not draw.
            _f, eff_n, reason = ticker.resolve_position(
                settings.get("ticker_position"))
            details.append(
                f"On-screen ticker: vertical position {eff_n:g} of "
                f"{ticker.POSITION_MIN} (top)..{ticker.POSITION_MAX} (bottom). Appearance is RENDERED, not applied: re-paste the "
                f"filter and restart the stream before believing this.")
            if reason is not None:
                problems.append(
                    f"On-screen ticker: banner position "
                    f"{settings.get('ticker_position')!r} {reason}; drawing "
                    f"at {eff_n:g} instead. Set it between "
                    f"{ticker.POSITION_MIN} and {ticker.POSITION_MAX}.")

            # The other three appearance settings had the identical hole: each
            # substituted its default in silence. Worst was opacity, where a
            # non-finite value rendered a FULLY TRANSPARENT box -- the banner
            # loses its background over bright video and nothing says why.
            for label, key, res in (
                ("text size", "ticker_font_size",
                 ticker.resolve_font_size(settings.get("ticker_font_size"))),
                ("colours", "ticker_colors",
                 ticker.resolve_colours(settings.get("ticker_colors"))),
                ("background opacity", "ticker_bg_opacity",
                 ticker.resolve_opacity(settings.get("ticker_bg_opacity"))),
            ):
                res_reason = res[-1]
                if res_reason is not None:
                    problems.append(
                        f"On-screen ticker: {label} {settings.get(key)!r} "
                        f"{res_reason}; using the default instead.")

            if t.sources is None:
                problems.append(
                    "On-screen ticker: WARNING; no source filter, so ANY "
                    "plugin can draw over live TV. Set 'Only these sources "
                    "may draw' to sentinelarr.")
            else:
                details.append(
                    "On-screen ticker: sources " + ", ".join(sorted(t.sources)))
                # A typo here is UNCATCHABLE by the one check the operator has:
                # _draw_test_banner deliberately clears the filter, so the Test
                # ticker button passes while production draws nothing. Say so,
                # since nothing else can.
                details.append(
                    "  A misspelt source silently stops every banner, and the "
                    "Test ticker button BYPASSES this filter, so a passing "
                    "test does not prove these names are right. Check the "
                    "ledger for ticker_skipped / source_filtered.")
                # The two halves of the ticker have to agree: a rule must send
                # the event to the ticker channel AND the source must be in
                # this filter. Configuring one without the other is silent, and
                # the ledger counter named just above cannot catch it because
                # it only increments once routing has already chosen the
                # ticker. Resolved through route(), so this cannot disagree
                # with delivery.
                effective = settings.get(
                    "_effective_routing_rules",
                    settings.get("routing_rules") or [])
                unreachable, filtered_out = routing.ticker_reachability(
                    t.sources, effective,
                    settings.get("default_channels") or [], _sev_eff)
                for name in unreachable:
                    problems.append(
                        f"On-screen ticker: {name} is allowed to draw, but no "
                        f"routing rule sends it to the ticker channel at "
                        f"severity {_sev_eff}, so it can never draw a banner. "
                        f"Add a rule routing {name} to ticker.")
                for name in filtered_out:
                    problems.append(
                        f"On-screen ticker: a routing rule sends {name} to the "
                        f"ticker, but {name} is not in the source filter, so "
                        f"every one of those banners is discarded. Add {name} "
                        f"to the filter, or remove the rule.")
            if not os.path.exists(t.path):
                problems.append(
                    f"On-screen ticker: ERROR; {t.path} is missing. A "
                    f"drawtext profile pointing at it will fail to start "
                    f"ffmpeg and the channel will HANG with no error. It is "
                    f"recreated on the next collector tick.")
            # Unconditional by necessity: the plugin cannot detect an invalid
            # or inactive output_profile id -- it fails SILENTLY to normal
            # passthrough (no banner, no error), so this can never be raised
            # as a problem, only stated every time the ticker is on.
            details.append(
                "On-screen ticker: this only reaches devices whose playlist "
                "URL carries ?output_profile=<id>. An invalid or inactive "
                "id fails SILENTLY to normal passthrough; no banner, no "
                "error. Verify the id in Dispatcharr's Output Profiles.")

            verdict = "problem" if problems else "ok"
            summary = f"{len(problems)} issue(s)" if problems else "on, healthy"
            return cls._report(check, verdict, summary, details, problems)
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    @classmethod
    def _events_report(cls, settings):
        """Subscribed Dispatcharr events, or skipped entirely when unused.

        Silent (skipped) when blank so operators who do not use the feature
        never see a word about it.
        """
        check = "Dispatcharr events"
        try:
            # THE one resolver: union(group toggles, CSV) -- same function the
            # event hook uses. A group-only config must not report `skipped`
            # (spec s2; rev-1 defect), so the skip keys on the EFFECTIVE set.
            enabled, unknown = dispatcharr_events.selected_events(settings)
            if not enabled and not unknown:
                return cls._report(check, "skipped", None)

            # Blank-vs-zero: 0 is a real cap value, so the blank test is an
            # explicit `in (None, "")`, never a two-arg get (present-but-None
            # skips the default) and never truthiness.
            cap = settings.get("max_sends_per_hour")
            if cap in (None, ""):
                cap = 20
            # lint_settings warns over the EFFECTIVE set: a group-enabled
            # login_failed fires the same safety warning a CSV-enabled one
            # does (keying warnings on the CSV alone was rev 1's downgrade).
            problems = [f"Dispatcharr events: {p}" for p in
                        dispatcharr_events.lint_settings(settings, cap)]
            details = [f"Dispatcharr events: {len(enabled)} subscribed"
                       + (f" ({', '.join(sorted(enabled))})." if enabled else ".")]
            # Stream-monitor-presence hint (spec s2): a HINT in details, never
            # an error -- another plugin's presence must not fail validation.
            # Lazy ORM, guarded: absent Django (tests) skips silently.
            # BOTH keys are queried on purpose. dmonitarr was renamed to
            # sentinelarr on 2026-07-27; a box that has not migrated still has
            # the identical duplicate-notification problem, and querying only
            # one key silently disables the hint on half the population.
            overlap = enabled & {"channel_error", "channel_failover"}
            if overlap:
                try:
                    from apps.plugins.models import PluginConfig
                    if PluginConfig.objects.filter(
                            key__in=("sentinelarr", "dmonitarr"),
                            enabled=True).exists():
                        details.append(
                            "Dispatcharr events: Sentinelarr is installed and "
                            "covers channel errors/failovers with debounced "
                            "incidents; these raw events duplicate it.")
                except Exception:
                    pass

            verdict = "problem" if problems else "ok"
            summary = f"{len(problems)} issue(s)" if problems else f"{len(enabled)} subscribed"
            return cls._report(check, verdict, summary, details, problems)
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    @staticmethod
    def _report_expect_horizon_s(settings):
        """Seconds in the report_expect_days horizon, or None when report
        monitoring is off.

        Blank means off, and is NOT the same as 0 (`in (None, "")`, never
        settings.get(k, DEFAULT) -- see dedup_window_critical_min in
        gates.py for the same shape). A stored 0 or negative value -- only
        reachable via a direct DB write bypassing the UI's min:1 -- means
        the same thing: OFF, not "immediately and permanently overdue".
        Without this clamp a 0 made every past last_attachment_delivered_ts
        perpetually overdue, i.e. permanent red instead of no check at all.
        The ONE place this is computed; every reader of report_expect_days
        that decides whether monitoring is on goes through here so the
        clamp cannot be forgotten at a second call site.
        """
        raw = settings.get("report_expect_days")
        if raw in (None, ""):
            return None
        try:
            days = float(raw)
        except (TypeError, ValueError):
            return None
        if days <= 0:
            return None
        return days * 86400

    @classmethod
    def _smtp_report(cls, settings):
        """SMTP config problems, reported without ever printing an address.

        `skipped` when SMTP is entirely unconfigured, so an operator who does
        not use email never sees a word about it -- UNLESS #22's report
        monitoring is on, in which case an unconfigured smtp channel means
        reports can never be attached and that must not be silent.
        """
        check = "SMTP"
        try:
            server = (settings.get("smtp_server") or "").strip()
            to_raw = settings.get("smtp_to") or ""
            username = (settings.get("smtp_username") or "").strip()
            password = settings.get("smtp_password") or ""
            # Blank means off and is NOT the same as 0 -- `in (None, "")`,
            # never settings.get(k, DEFAULT) (see dedup_window_critical_min
            # in gates.py for the same shape). Gated on the setting alone,
            # channel-agnostic: there is no kind=report to identify a "report
            # rule" by, so any smtp misconfiguration while this is on is
            # a problem regardless of what else the operator uses smtp for.
            #
            # Monitoring is also armed by report_expect alone, with its own
            # per-entry cadence and report_expect_days left blank -- an
            # entry that parses is enough, the horizon clamp in
            # _report_expect_horizon_s stays the ONE place report_expect_days
            # itself is interpreted, it is not duplicated here.
            #
            # ERRORS count as monitoring being on, exactly like entries. A
            # report_expect the parser cannot read is not "monitoring off": it
            # is monitoring the operator meant to have, and show_status already
            # goes red about the same setting. Counting only entries made these
            # two surfaces disagree about one setting -- validate calling smtp
            # skipped while status called it a problem -- which is a defect in
            # its own right here.
            expect_entries, expect_errors = report_expect.parse(
                settings.get("report_expect"), settings.get("report_expect_days"))
            report_monitoring_on = (cls._report_expect_horizon_s(settings) is not None
                                    or bool(expect_entries) or bool(expect_errors))
            if not server and not to_raw.strip() and not username:
                if report_monitoring_on:
                    return cls._report(
                        check, "problem", "1 problem(s)", [],
                        ["SMTP: report monitoring is on (report_expect / "
                         "report_expect_days) but smtp is not configured; "
                         "reports cannot be attached."])
                return cls._report(check, "skipped", None)

            details = []
            problems = []
            host, port = channels.parse_server(server)
            valid, invalid = email_render.parse_recipients(to_raw)

            if not server:
                problems.append("SMTP: no server set; email is OFF.")
            elif host is None:
                problems.append(f"SMTP: ERROR; {server!r} is not a valid host or "
                                 f"host:port (port must be 1-65535).")
            if invalid:
                one = len(invalid) == 1
                problems.append(
                    "SMTP: ERROR; smtp_to "
                    + ("entry " if one else "entries ")
                    + ", ".join(str(i) for i in invalid)
                    + (" is not a valid address" if one
                       else " are not valid addresses")
                    + " (reported by position; addresses are never printed here).")
            if server and not valid:
                problems.append("SMTP: no valid recipient; email is OFF.")

            mode = (settings.get("smtp_security") or "auto").strip().lower()
            if mode not in ("auto", "starttls", "ssl", "none"):
                problems.append(f"SMTP: ERROR; unknown security {mode!r} "
                                 f"(auto | starttls | ssl | none).")
            elif host is not None:
                resolved, inferred = channels.resolve_security(settings, port)
                suffix = f" (inferred from port {port})" if inferred else ""
                # LOAD-BEARING: smtp_security's own field description
                # promises "Validate prints what it resolved to" --
                # this must survive even when nothing here is wrong, so it is
                # unconditional `details`, never gated behind a problem.
                details.append(f"SMTP: security {mode} -> {resolved}{suffix}.")
                if resolved == "none" and username:
                    problems.append(
                        "SMTP: ERROR; security 'none' with a username means the "
                        "password is base64 on the wire on EVERY send. Refusing to "
                        "send. Use starttls/ssl, or clear the username for an "
                        "unauthenticated relay.")
                if resolved == "none" and port in (465, 587):
                    problems.append(f"SMTP: WARNING; security 'none' on port {port} "
                                     f"is almost certainly wrong.")

            if username and not password:
                problems.append("SMTP: WARNING; username set but password blank; "
                                 "auth will be attempted with an empty password and "
                                 "will fail. Leave both blank for an open relay.")
            if password and not username:
                problems.append("SMTP: WARNING; password set but username blank; "
                                 "no authentication will be attempted.")
            if server and not (settings.get("smtp_from") or "").strip() and not username:
                problems.append("SMTP: ERROR; no From address (smtp_from is blank "
                                 "and there is no username to fall back to).")

            # #22: the row is not entirely blank (so the early-return above
            # never fired) but does not add up to a channel `channels.send`
            # would actually try -- e.g. a server with no valid recipient.
            # Report monitoring being on then means reports can never be
            # attached, and that must not read as merely "email is degraded".
            if report_monitoring_on and not channels.smtp_configured(settings):
                problems.append(
                    "SMTP: report monitoring is on (report_expect / "
                    "report_expect_days) but smtp is not configured; "
                    "reports cannot be attached.")

            # Do not print the reassuring summary when something above is an
            # ERROR: it would read as "email is working" next to a line
            # saying it refuses to send. A WARNING alone does not suppress it.
            if valid and host is not None and not any("ERROR" in p for p in problems):
                # LOAD-BEARING: this is the only channel where a `delivered`
                # ledger row can be a lie -- a 250 means the server ACCEPTED
                # the message for relay, not that it arrived.
                details.append(f"SMTP: {len(valid)} recipient(s) configured. "
                                f"'Sent' means the server ACCEPTED the message, not "
                                f"that it arrived; check the inbox AND the spam "
                                f"folder before relying on this channel.")

            verdict = "problem" if problems else "ok"
            summary = (f"{len(problems)} problem(s)" if problems
                       else f"{len(valid)} recipient(s) OK")
            return cls._report(check, verdict, summary, details, problems)
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    # A needle is compared against `urlsplit(url).hostname`, so anything
    # carrying a scheme, a port, a path or a leading dot can NEVER match. Those
    # entries were counted and printed under "additional:" -- a redaction no-op
    # rendered as coverage, the same family as `redact_hostnames` shipping as
    # an unredacted no-op.
    @staticmethod
    def _inert_needles(manual):
        bad = []
        for h in manual or []:
            s = str(h).strip()
            if not s:
                continue
            if ("://" in s or "/" in s or ":" in s or s.startswith(".")
                    or s.endswith(".") or " " in s):
                bad.append(s)
        return bad

    @classmethod
    def _redaction_report(cls, settings):
        """The effective hostname list, grouped by where each host came from.

        Always runs (never `skipped` -- it is not opt-in). A COUNT cannot
        answer the only question anyone ever asks, which is "is provider.tv in
        the list or not?", so this prints the hosts. It is the full list on
        purpose and carries a do-not-paste warning: unlike show_status (which
        docs/API.md notes gets pasted into GitHub issues), Validate is an
        explicit show-me-my-config click."""
        check = "Hostname redaction"
        try:
            # force_refresh: this is the plugin's ONLY refresh trigger -- an
            # operator who just added an M3U account and clicks Validate must
            # see ground truth, not a cached list that makes derivation look
            # broken.
            info = dispatcharr.read_redact_hosts(force_refresh=True)

            manual = collector._parse_hostnames(settings.get("redact_hostnames", ""))
            derived = set(info.get("hosts") or [])
            extra = [h for h in manual if h not in derived]
            total = len(derived | set(manual))

            details = []
            problems = []
            inert = cls._inert_needles(manual)
            if inert:
                problems.append(
                    f"Redaction: {', '.join(inert)} can never match; a needle is "
                    f"compared against a URL's HOSTNAME, so drop any scheme, port, "
                    f"path or leading dot and list the bare hostname.")
            if not info.get("ok") and not info.get("stale"):
                problems.append(
                    "WARNING: could not read your EPG sources and M3U accounts, so NO "
                    "hostnames were auto-derived. Delivery continues and credential "
                    "redaction still runs, but provider hostnames may appear in "
                    f"notifications. ({info.get('error')})")
            elif info.get("stale"):
                problems.append(
                    f"WARNING: could not re-read your EPG sources and M3U accounts; "
                    f"using the list cached {info.get('age_s', 0):.0f}s ago. Sources "
                    f"added since then are NOT redacted. ({info.get('error')})")

            # LOAD-BEARING: the grouped hostname list itself -- see docstring.
            details.append(f"Hostname redaction: {total} host(s) - "
                            f"{len(derived)} auto-derived, {len(extra)} added manually.")
            if info.get("epg"):
                details.append("  from EPG sources:  " + ", ".join(info["epg"]))
            if info.get("m3u"):
                details.append("  from M3U accounts: " + ", ".join(info["m3u"]))
            if extra:
                details.append("  additional:        " + ", ".join(extra))
            # CORRECTED 2026-07-25 (QA review). This said "A bare hostname in
            # prose is NOT replaced" -- false since the bare-token pass landed
            # on 2026-07-21 (channels.redact_hosts; measured:
            # "blocked: provider.tv-alt1" -> "blocked: <redacted-host>"). Stale
            # in the SAFE direction, but it is a false claim about a safety
            # property in the one action whose job is stating that property.
            details.append(
                "A URL at these hosts becomes <redacted-url>, and the bare "
                "hostname on its own becomes <redacted-host>. Anything NOT "
                "listed above can still appear verbatim in a notification.")
            details.append("Do not paste this list into a public issue.")

            # A DERIVATION failure is `degraded`, never `problem`: A1 makes it
            # degrade instead of failing closed, and an unreadable
            # EPGSource/M3UAccount table is not something the operator can fix
            # on this settings page -- so it must not paint Validate red. It
            # still lands in `problems` (and therefore the toast), which is
            # what A1's "the failure MUST stay visible" requires.
            #
            # An INERT NEEDLE is the opposite: the operator typed it, only they
            # can fix it, and it silently buys nothing while being counted as
            # coverage. That is a `problem` and must outrank the degradation --
            # otherwise a typo'd needle is invisible on any box whose
            # derivation happens to be degraded.
            if inert:
                verdict = "problem"
            elif problems:
                verdict = "degraded"
            else:
                verdict = "ok"
            summary = (f"{total} host(s)"
                       + (", inert entries" if inert
                          else ", degraded" if problems else ""))
            return cls._report(check, verdict, summary, details, problems)
        except Exception as exc:
            return cls._report_not_checked(check, exc)

    def _action_test_notification(self, settings, params):
        channel = params.get("channel")
        # `_opener` is a test-only injection hook (see FakeOpener in
        # tests/conftest.py); real deploys always fall through to
        # channels.send's default urllib.request.urlopen.
        opener = params.get("_opener")
        smtp_factory = params.get("_smtp_factory")

        if channel == "ticker":
            return self._draw_test_banner(settings)

        if channel:
            event = {
                "source": "newsflasharr", "event": "test", "severity": "info",
                "kind": "event",
                # Wire-side redaction (spec section 8.1, defense-in-depth):
                # this path builds the event by hand instead of going
                # through notify_client.notify() (which already redacts at
                # spool-write time), so redact() runs here too.
                "title": redact("Newsflasharr test notification"),
                "body": redact("This is a test notification from Newsflasharr."),
                "ts": int(time.time() * 1000),
            }
            ok, detail = channels.send(channel, event, settings, PLUGIN_VERSION,
                                        opener=opener, smtp_factory=smtp_factory)
            # A failed channel test is the one failure the operator MUST see:
            # this button exists to prove a channel works. Without `error` it
            # renders as the same transient GREEN toast as success.
            return self._result(ok, detail, error=None if ok else detail)

        delivered = notify(
            source="newsflasharr", title="Newsflasharr test notification",
            body="This is a test notification from Newsflasharr.",
            severity="info", kind="event", base_dir=_base_dir(settings))
        if delivered:
            return {"status": "ok",
                    "message": "Test event spooled; the collector delivers it "
                               "on its next tick."}
        # A failed spool write is the E3 unwritable-directory trap -- the state
        # in which every caller silently loses 100% of its events.
        return self._result(
            False, "Failed to spool test event.",
            error="Failed to spool the test event. The spool directory is "
                  "probably unwritable; check that /data/newsflasharr is owned "
                  "by dispatch:dispatch. While this fails, EVERY plugin's "
                  "notifications are being lost silently.")

    def _action_test_ticker(self, settings, params):
        """Its own button because the generic test could never draw a banner.

        `test_notification` sends severity=info from source "newsflasharr", so any
        realistic ticker config (critical, one named source) declines it -- the
        operator reported "nothing happened" three times before this existed.
        Documentation did not fix it; a button does.
        """
        return self._draw_test_banner(settings)

    def _draw_test_banner(self, settings):
        """Draw a banner regardless of the operator's ticker filters.

        Naming a channel means "does this channel WORK?", not "would this event
        route here" -- the direct-send path already bypasses routing, dedup and
        quiet hours for every other channel. Filtering the ticker test would
        make a correctly-configured ticker look broken, which is precisely the
        confusion this action exists to end. The thresholds are relaxed on this
        LOCAL instance only; stored settings are never touched.
        """
        tk = ticker.from_settings(_base_dir(settings), settings)
        if not tk.enabled:
            msg = ("ticker: disabled (enable it in the Ticker section, then "
                   "test again)")
            return self._result(False, msg, error=msg)
        tk.min_rank = ticker.rank("info")
        tk.sources = None
        result = tk.note({
            "source": "newsflasharr", "event": "test", "severity": "critical",
            "kind": "event",
            "title": redact("Newsflasharr ticker test - clears on its own"),
        })
        if result == "written":
            return {"status": "ok",
                    "message": f"ticker: banner drawn; it clears in "
                               f"{int(tk.ttl_s)}-{int(tk.ttl_s) + 6}s. It is "
                               f"removed on the first collector tick AFTER the "
                               f"{int(tk.ttl_s)}s life, not by a precise timer, "
                               f"so expect a few seconds over. Only devices on "
                               f"an output profile with the drawtext filter can "
                               f"see it. VOD never shows a banner."}
        return self._result(False, f"ticker: {result}",
                            error=f"The banner was NOT drawn: {result}.")

    def _action_show_ticker_filter(self, settings, params):
        """Render the operator's ffmpeg parameters. Newsflasharr writes NOTHING to
        Dispatcharr -- this is a string to paste, which is what keeps the
        read-only invariant intact while still making appearance configurable.

        Colours are validated here and reported by NAME, because an unusable
        colour does not degrade: drawtext fails to initialise, ffmpeg exits, and
        Dispatcharr has already marked the transcode active, so the operator
        gets a hung channel with no error anywhere.
        """
        # Colours are a preset select now (#21) -- an un-renderable colour is
        # unrepresentable, so there is nothing to validate here.
        #
        # ORDER IS LOAD-BEARING: explanation/warnings first, the parameters
        # block LAST with nothing after it. The file exists so the operator
        # can select the parameters and paste them -- ending the file with
        # exactly the parameters means a careless select-to-end cannot pick
        # up trailing prose. A partial OR over-inclusive paste gives ffmpeg
        # no output muxer: it starts fine, serves nothing, and has taken
        # this box's live TV down once. The parameters string is 989 chars
        # and the toast shows ~280 clipped from the middle, so the paste
        # target goes to the FILE, where it can be selected whole.
        lines = []
        if not settings.get("ticker_enabled"):
            lines.append("NOTE: the ticker is disabled, so nothing will draw "
                         "even with this filter in place.")
            lines.append("")

        # Reported HERE as well as in validate, because validate's ticker check
        # is `skipped` while the ticker is disabled -- which is exactly when the
        # operator is setting appearance up and pasting this string. Same
        # resolver as the render, so the two can never disagree. Built ONCE
        # and reused in both the headline and the file body, not written twice.
        _f, eff_n, reason = ticker.resolve_position(
            settings.get("ticker_position"))
        position_line = (
            f"Vertical position: {eff_n:g}, on a scale of "
            f"{ticker.POSITION_MIN} (top) to {ticker.POSITION_MAX} (bottom).")
        lines.append(position_line)
        if reason is not None:
            lines.append(
                f"WARNING: the stored position "
                f"{settings.get('ticker_position')!r} {reason}; drawing at "
                f"{eff_n:g} instead.")
        for label, key, res in (
            ("text size", "ticker_font_size",
             ticker.resolve_font_size(settings.get("ticker_font_size"))),
            ("colours", "ticker_colors",
             ticker.resolve_colours(settings.get("ticker_colors"))),
            ("background opacity", "ticker_bg_opacity",
             ticker.resolve_opacity(settings.get("ticker_bg_opacity"))),
        ):
            if res[-1] is not None:
                lines.append(
                    f"WARNING: {label} {settings.get(key)!r} {res[-1]}; "
                    f"using the default instead.")
        lines.append("")
        lines.append("The doubled backslashes are REQUIRED. Dispatcharr runs "
                     "shlex over this and ffmpeg needs the surviving one. A "
                     "wrong level does not error on save; it hangs the channel "
                     "at stream start.")
        lines.append("Swap h264_nvenc for 'libx264 -preset veryfast' if you "
                     "have no GPU. Restart the stream to pick up a change.")
        lines.append("")
        lines.append("Paste as the Output Profile PARAMETERS (command: ffmpeg):")
        lines.append("")
        # LAST, with nothing appended after it -- see the ordering note above.
        lines.append(ticker.render_parameters(settings))

        # No colour-validation failure is possible now (#21 presets), and the
        # rendered filter is always emittable, so this action is informational.
        # `lines` now feeds ONLY `_readout` (the file); `message` is this
        # separately-built headline, never `lines` composed with problems/details.
        warnings = [ln for ln in lines if ln.startswith("WARNING")]
        headline = " ".join(warnings + [
            "Ticker filter written to the file. Paste it as the Output "
            "Profile PARAMETERS (command: ffmpeg).",
            position_line])
        return {"status": "ok", "message": headline,
                # the WHOLE readout, not just the parameters: the warnings must
                # have a durable home too, or the superset guarantee is hollow.
                "_readout": "\n".join(lines)}

    def _action_show_redaction(self, settings, params):
        """The effective redacted-hostname list, on its own button.

        Validate's toast is capped at 120 chars, so it can only carry a
        COUNT of redacted hosts -- and a count cannot answer the only
        question anyone ever asks, which is whether a specific host (say
        provider.tv) is in the list. Delegates to `_redaction_report`, the
        same helper `validate_settings` uses for this check, so the two
        surfaces can never disagree -- and `force_refresh=True` (this
        action's whole reason to exist: it is the plugin's ONLY refresh
        trigger, letting an operator who just added an M3U account see
        ground truth instead of a stale cache) comes along for free.
        """
        rep = self._redaction_report(settings)
        ok = rep["verdict"] not in ("problem", "not_checked")
        message = self._fit_message(rep["problems"]) if rep["problems"] else rep["summary"]
        return self._result(
            ok, message, error="; ".join(rep["problems"]) if rep["problems"] else None,
            details=rep["details"], problems=rep["problems"])

    def _action_show_routing(self, settings, params):
        """Resolve the EFFECTIVE routing rules (operator JSON + presets) and
        print where events land. Persistent surface (details). lint stays on
        the RAW rules so rule indices track the textarea (spec §5)."""
        enabled = channels.enabled_channels(settings)
        effective = settings.get("_effective_routing_rules",
                                 settings.get("routing_rules", []))
        # rows on the effective list; known_channels=None suppresses summarize's
        # internal lint so it cannot renumber the operator's rules.
        summary = routing.summarize(
            effective, settings.get("default_channels") or [],
            enabled=enabled, known_channels=None)
        # broken/lint problems from the RAW rules, correct indices.
        broken = routing.lint(
            settings.get("routing_rules") or [], channels.KNOWN,
            default_channels=settings.get("default_channels"))

        problems = []
        for row in summary["rows"]:
            if row["reaches_nothing"]:
                chans = ", ".join(row["channels"]) or "(nothing)"
                problems.append(
                    f"{self._selector_label(row['selector'])} "
                    f"({row['severities']}) routes to {chans}, none configured "
                    f"-- reaches nothing.")
        unconfigured = _unconfigured_channels(
            effective, settings.get("default_channels"), enabled)
        if unconfigured:
            problems.append(
                f"Routes to {', '.join(unconfigured)}, not configured "
                f"-- those events reach nothing.")
        problems.extend(broken)

        esc = routing._active_escalation(settings)
        escalation_channels = esc[1] if esc else ()
        details = self._render_routing(summary, broken, escalation_channels)
        in_effect = _presets_in_effect(settings)
        if in_effect:
            details.append("")
            details.append("Presets in effect:")
            details.extend(f"  {line}" for line in in_effect)
        if unconfigured:
            details.append("")
            details.append(
                f"Unconfigured channels referenced: {', '.join(unconfigured)} "
                f"(routed but not set up).")
        ok = not problems
        generic = [r for r in summary["rows"] if not r["selector"]]
        message = "; ".join(
            f"{'all' if r['severities'] == 'any' else r['severities']} -> "
            f"{', '.join(r['channels']) or 'nothing'}"
            for r in generic)[:self.MESSAGE_CAP]
        return self._result(
            ok, message, error="; ".join(problems) if problems else None,
            details=details, problems=problems)

    def _action_show_status(self, settings, params):
        """Reads ONLY from disk (state.json, spool listing, ledger tail) --
        never in-memory collector counters, because the worker serving this
        click is usually not the leader holding the flock (spec section 6)."""
        base_dir = _base_dir(settings)
        st = store.Store(base_dir, redact=redact)
        now = time.time()
        state = st.load_state(now)
        lines = []
        ticker_file_missing = False

        # Same computation _liveness_report uses (the two surfaces must
        # agree), clamped exactly as the collector's own run loop clamps.
        max_heartbeat_age = collector.heartbeat_threshold(settings)

        # Keyed on `last_tick`, NOT the file's mtime. Deliverer.tick saves
        # state before _tick's tail runs, so an exception there left mtime
        # FRESH while last_tick froze: show_status reported "heartbeat: ok"
        # while _liveness_report reported stale, and neither named the cause.
        # _liveness_report's docstring already argues last_tick is the right
        # key; this makes the two surfaces agree. mtime remains the fallback
        # for a state file written before last_tick existed.
        try:
            last_tick = state.get("last_tick")
            heartbeat_age = (now - float(last_tick) if last_tick
                             else now - os.path.getmtime(st.state_path))
            heartbeat_fresh = heartbeat_age < max_heartbeat_age
            lines.append(
                f"heartbeat: {'ok' if heartbeat_fresh else 'STALE'} "
                f"({heartbeat_age:.0f}s old, threshold {max_heartbeat_age:.0f}s)")
        except OSError:
            heartbeat_age = None
            heartbeat_fresh = False
            lines.append("heartbeat: never seen (no state.json on disk yet)")

        # A recorded lock FAULT is the difference between "nothing has started
        # yet" and "no worker can ever become leader". Without it the E3
        # root-ownership trap renders as "heartbeat: never seen (no state.json
        # on disk yet)" -- the same words as a healthy first boot, which is how
        # it cost a deploy once already. Every worker attempts acquisition, so
        # the worker serving this click has the fault recorded too.
        # A corrupt state.json silently discarded EVERY held event (criticals
        # included), all dedup windows, all `opens`, the cap counter -- and
        # held_overflow itself, so the counter that would have recorded the
        # loss was zeroed by the same event. `_corrupt_recovered` was written
        # by the store and read by nothing.
        if state.get("_corrupt_recovered"):
            lines.append(
                "state: RECOVERED FROM A CORRUPT FILE; held events, repeat "
                "windows and open incidents were lost; the sidelined copy is "
                "state.json.corrupt-<ts>. The hourly budget starts consumed.")
            state_lost = True
        else:
            state_lost = False

        # `_spool_has_room`'s docstring claimed this marker made the loss "at
        # least visible". Nothing opened it. A marker means a CRITICAL was
        # refused past the reserve -- the one loss this system must never make
        # quietly.
        spool_full_at = None
        try:
            marker = os.path.join(base_dir, "spool_full")
            if os.path.exists(marker):
                spool_full_at = os.path.getmtime(marker)
                lines.append(
                    f"spool_full marker present ({time.time() - spool_full_at:.0f}s "
                    f"ago); at least one CRITICAL was REFUSED and dropped "
                    f"because the queue was full. Delete {marker} once handled.")
        except OSError:
            pass

        # THE E3 TRAP, checked actively. notify_client's blanket except returns
        # False forever when the spool cannot be written, leaving no artefact
        # anywhere -- so every caller silently loses 100% of its events while
        # this page reports a fresh heartbeat and queue depth 0.
        #
        # Tested here rather than recorded by notify(): if the base dir is
        # unwritable then a marker written there fails too, so the only
        # reliable moment is when the operator looks. Same reasoning as the
        # ticker.txt check. os.access is a true test because show_status runs
        # as the same user as the collector and the callers.
        spool_dir = os.path.join(base_dir, "spool")
        spool_writable = True
        if not os.path.isdir(spool_dir):
            # Only a fault once the collector has ticked at least once -- the
            # collector creates this tree, so before the first tick its absence
            # is a healthy fresh install, and the heartbeat line already says
            # "never seen". Flagging it here would invert the first-boot
            # confusion this check exists to remove.
            if state.get("last_tick"):
                spool_writable = False
                lines.append(
                    f"queue directory MISSING ({spool_dir}); callers cannot "
                    f"queue anything and notify() is failing silently.")
        elif not os.access(spool_dir, os.W_OK | os.X_OK):
            spool_writable = False
            lines.append(
                f"queue directory NOT WRITABLE ({spool_dir}); every notify() "
                f"call is failing silently and returning False. Check "
                f"ownership; a root `docker exec` creates this tree root:root "
                f"(the E3 trap).")

        sidecar_write_errors = state.get("sidecar_write_errors") or 0
        if sidecar_write_errors:
            lines.append(
                f"sidecar write errors: {sidecar_write_errors}; delivery "
                f"progress could not be persisted; affected events re-send and "
                f"never terminate.")

        ticker_errors = state.get("ticker_errors") or 0
        if ticker_errors:
            lines.append(
                f"banner errors: {ticker_errors}; the on-screen banner could "
                f"not be updated or cleared, so a stale alert may still be "
                f"drawn over live video.")

        tick_error = state.get("tick_error") or {}
        if tick_error:
            lines.append(
                f"tick errors: {tick_error.get('type')} "
                f"x{tick_error.get('count')}; the collector is raising on "
                f"every pass and retrying silently.")

        lock_error = collector.last_lock_error()
        if lock_error:
            lines.append(
                f"leader lock: FAULT ({lock_error.get('type')}, "
                f"x{lock_error.get('count')}); no worker can become leader, "
                f"so nothing is being delivered. Check ownership of "
                f"{base_dir} (must be writable by the dispatch user).")

        fail_closed = bool(state.get("fail_closed"))
        fc_reason = state.get("fail_closed_reason")
        fc_count = state.get("fail_closed_count")
        if fail_closed:
            detail = f" ({fc_reason})" if fc_reason else ""
            # Defined once; problems (below) gets the short form, details
            # gets the fuller explanation -- same shape as every other
            # condition in this method (heartbeat, held_overflow, enabled,
            # critical_held).
            fail_closed_summary = f"FAIL_CLOSED{detail}: delivery is completely stopped."
            lines.append(f"{fail_closed_summary} Settings could not be read "
                          f"on the last tick; nothing is being claimed or "
                          f"delivered.")
            if state.get("fail_closed_since"):
                lines.append(f"  failing since {now - state['fail_closed_since']:.0f}s ago")
        elif fc_count:
            # Survives recovery on purpose: an intermittent settings read is
            # exactly the shape of the one unexplained production occurrence,
            # and without a counter a self-healing blip leaves no trace at all.
            lines.append(f"NOTE: settings have failed to read {fc_count} time(s) "
                          f"since this state file was created (currently OK).")

        spool_paths = spool.scan(st.spool_dir)
        spool_depth = len(spool_paths)
        oldest_unclaimed_s = collector.Collector._oldest_age(spool_paths, now)
        lines.append(f"queue depth: {spool_depth} "
                      f"(oldest unclaimed {oldest_unclaimed_s:.0f}s)")
        if heartbeat_fresh and oldest_unclaimed_s > 300:
            lines.append(
                f"WARNING: {spool_depth} event(s) unclaimed for over 5 minutes "
                f"despite a fresh heartbeat; the collector may be stuck.")

        gates_state = state.get("gates") or {}
        held_count = len(gates_state.get("held") or [])
        dedup_count = len(gates_state.get("dedup") or {})
        lines.append(
            f"held: {held_count}, active repeat windows: {dedup_count}")

        # The banner file is load-bearing: if an OutputProfile points at it and
        # it is missing, drawtext fails to initialise, ffmpeg exits, and the
        # client attaches to a buffer that never fills -- a HUNG CHANNEL with
        # no error, on every reconnect. Reported only when the ticker is on,
        # so it costs nothing for operators not using it.
        if settings.get("ticker_enabled"):
            ticker_path = os.path.join(base_dir, "ticker.txt")
            if os.path.exists(ticker_path):
                lines.append("ticker.txt: present")
            else:
                lines.append(
                    "ticker.txt: MISSING; any channel using the ticker "
                    "OutputProfile will HANG until this file exists")
                ticker_file_missing = True

        # Standing reminder while a bypass is disabled (spec section 7). An
        # operator who set this months ago sees `held: 14` during a 3am outage
        # and cannot tell it from a night of routine info noise -- the whole
        # reason the held counters got a severity breakdown. Silent at the
        # defaults, so it costs nothing when it does not apply.
        for key, label in (("quiet_hours_bypass", "quiet hours bypass"),
                           ("hourly_cap_bypass", "hourly cap bypass")):
            if str(settings.get(key) or "").strip().lower() == "none":
                lines.append(
                    f"{label}: NONE; critical alerts are held too, "
                    f"not delivered")

        # Both of these are silent losses without a surface. held[] is
        # capped now (it was an unbounded append), and a broken event
        # handler returns into a void nobody reads.
        held_overflow = gates_state.get("held_overflow", 0)
        if held_overflow:
            lines.append(
                f"WARNING: {held_overflow} held event(s) DROPPED (oldest "
                f"first); the held queue hit its cap. Something is "
                f"generating far more notifications than can be sent.")
        dedup_overflow = gates_state.get("dedup_overflow", 0)
        if dedup_overflow:
            lines.append(
                f"WARNING: {dedup_overflow} duplicate-suppression window(s) "
                f"evicted at the cap; their pending 'N more' summaries "
                f"were dropped.")
        ticker_drawn = gates_state.get("ticker_drawn") or 0
        ticker_declined = gates_state.get("ticker_declined") or {}
        declined_total = sum(ticker_declined.values())
        if ticker_drawn or declined_total:
            # Informational, never a `problem`: a declined banner is the
            # operator's own thresholds working, and drew 0 is the normal
            # steady state for an opt-in critical-only ticker. The value is
            # that BOTH numbers are visible -- `drew 0, declined 200` is how an
            # operator spots a typo'd ticker_sources himself.
            line = f"ticker: drew {ticker_drawn}, declined {declined_total}"
            if declined_total:
                parts = ", ".join(
                    f"{reason}: {count}" for reason, count in
                    sorted(ticker_declined.items(), key=lambda kv: (-kv[1], kv[0])))
                line += f" ({parts})"
            lines.append(line)
        event_errors = gates_state.get("event_handler_errors", 0)
        if event_errors:
            lines.append(
                f"WARNING: {event_errors} Dispatcharr-event handler "
                f"error(s) since last reset.")

        # M2: surface the enabled-channel count and the last tick's
        # held_no_channels so a misconfigured (zero-channel) notifier
        # doesn't look healthy just because the heartbeat is fresh and
        # nothing is erroring -- deliver.tick() holds everything silently
        # (by design, spec §4.3) when no channel is enabled.
        enabled = channels.enabled_channels(settings)
        enabled_desc = ", ".join(enabled) if enabled else "none"
        lines.append(f"enabled channels: {len(enabled)} ({enabled_desc})")

        # Informational, NEVER a problem. Report ABSENCE is already detected by
        # report_expect and reported by Validate against the operator's own
        # cadence; duplicating that judgement here would give two surfaces that
        # can disagree. This line answers a different question: which callers
        # send reports at all, and how many each says it has published.
        #
        # The age comes from `report_last`, which this plugin maintains itself.
        # The count comes from the caller's own /data/<source>/report_count.json
        # and is omitted when that file is absent, which is the normal case:
        # one sibling plugin writes it today. Printing 0 for the others would
        # read as "this plugin has published nothing" when the truth is "this
        # plugin does not publish a counter".
        published = report_count.summarize(
            state, now, os.path.dirname(os.path.normpath(base_dir)))
        if published:
            parts = []
            for entry in published:
                part = (f"{entry['source']}/{entry['event']} "
                        f"{entry['age_s'] / 3600.0:.1f}h ago")
                if entry["count"] is not None:
                    part += f" ({entry['count']} built)"
                parts.append(part)
            # "report sources:", NOT "reports:". The report-absence check below
            # already owns the "reports:" prefix on five different lines, and
            # two labels reading the same while meaning different things is
            # worse in a readout than a longer label.
            lines.append("report sources: " + "; ".join(parts))
            if any(entry["count"] is not None for entry in published):
                # Said once, here, because the number looks authoritative and
                # is not. The writer increments without a lock across several
                # workers and the file is not backed by a database.
                lines.append(
                    "  a 'built' count is reported by that plugin itself, "
                    "counts reports written rather than emails delivered, "
                    "can undercount, and restarts if its data directory is "
                    "cleared.")

        # Counts + provenance only, never the host list itself: docs/API.md
        # warns that status dumps get pasted into GitHub issues, and dumping
        # the derived provider hostnames there would recreate the exact leak
        # this redaction exists to prevent. Validate prints the full list.
        tz_info = settings.get("_resolved_tz_info") or {}
        tz_name = settings.get("_resolved_quiet_tz") or "UTC"
        lines.append(f"timezone: {tz_name} (from Dispatcharr)"
                     + ("" if tz_info.get("ok", True) else ". INVALID, see Validate"))

        host_info = settings.get("_resolved_host_info") or {}
        effective = collector.effective_hostnames(settings)
        manual_only = len(effective) - len(host_info.get("hosts") or [])
        if not host_info.get("ok") and not host_info.get("stale"):
            lines.append(
                "WARNING: EPG/M3U hostnames could not be derived; provider "
                "hostnames may appear in notifications. Run Validate for details.")
        elif host_info.get("stale"):
            lines.append(
                f"WARNING: hostname list is stale (cached "
                f"{host_info.get('age_s', 0):.0f}s ago); newly added sources are "
                f"NOT redacted.")
        lines.append(f"redaction: {len(effective)} hostname(s) "
                     f"({len(host_info.get('hosts') or [])} derived, "
                     f"{max(0, manual_only)} additional)")

        last_stats = state.get("last_stats") or {}
        held_no_channels = last_stats.get("held_no_channels", 0)
        if held_no_channels:
            lines.append(
                f"WARNING: {held_no_channels} event(s) held on the last tick "
                f"because no channel is enabled.")

        skipped = last_stats.get("skipped", 0)
        if skipped:
            # Not a failure: every routed channel declined it (today, the
            # ticker's severity/source gates). Said plainly so it is not read as
            # an outage -- see the `not_delivered` ledger rows for which channel
            # declined and why.
            lines.append(
                f"skipped: {skipped} event(s) reached no channel on the last "
                f"tick because every routed channel declined them. NOT a "
                f"failure; see the not_delivered ledger rows for which "
                f"channel declined and why.")

        tail = st.ledger_tail(20)
        # AGGREGATED. As 20 comma-separated tokens with no timestamp,
        # source or severity this said only "some skips happened", and
        # it was ~40% of a toast whose newlines collapse to one line.
        counts = {}
        for rec in tail:
            key = rec.get("outcome", "?")
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(f"{n} {name}" for name, n in
                            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"ledger (last {len(tail)}): " + (summary or "empty"))

        # Severity breakdown of what is CURRENTLY held -- surfacing data that
        # was already stored (_hold stores dict(event), severity included)
        # rather than inventing anything. `held: 14` during a 3am outage is
        # indistinguishable from a night of routine info noise without this.
        held_by_severity = {}
        for held in (gates_state.get("held") or []):
            sev = ((held or {}).get("event") or {}).get("severity", "unknown")
            held_by_severity[sev] = held_by_severity.get(sev, 0) + 1
        critical_held = held_by_severity.get("critical", 0)

        # #22: the only way to notice a scheduled report (e.g. dustarr's
        # emailed report) has silently stopped arriving -- notify() has no
        # way to detect an ABSENCE, so this is client-side bookkeeping. Task
        # 6 stamps state["last_attachment_delivered_ts"] on a real
        # smtp-with-file send; this compares its age against the operator's
        # own cadence. `_report_expect_horizon_s` is the ONE place blank/0/
        # negative all resolve to "not monitoring" -- see its docstring.
        report_horizon_s = self._report_expect_horizon_s(settings)
        report_overdue = None
        report_problems = []
        expect_entries, expect_errors = report_expect.parse(
            settings.get("report_expect"), settings.get("report_expect_days"))
        if expect_entries or expect_errors:
            # Per-caller supersedes the global check. Running both would print
            # a reassuring total beside a red per-caller line about the same
            # silence, which is the confusion this replaced.
            for err in expect_errors:
                lines.append(f"reports: {err}")
                report_problems.append(f"report_expect: {err}")
            for got in report_expect.evaluate(expect_entries, state, now):
                lines.append(f"reports: {got['line']}")
                if got["problem"]:
                    report_problems.append(got["problem"])
        elif report_horizon_s is not None:
            # ALWAYS say something when monitoring is armed. It used to print a
            # line only when OVERDUE, so a detector that had never seen a report
            # was permanently inert AND silent -- indistinguishable from one that
            # was watching happily, and the operator had no way to tell the
            # feature was on at all.
            last = state.get("last_attachment_delivered_ts")
            every = settings.get("report_expect_days")
            if not last:
                lines.append(
                    f"reports: monitoring on (expected every {every} day(s)) "
                    f"-- none delivered yet, so nothing can be overdue.")
            else:
                age_d = (now - float(last)) / 86400.0
                if now - float(last) > report_horizon_s:
                    report_overdue = age_d
                    lines.append(
                        f"reports: OVERDUE; none delivered in {age_d:.0f} "
                        f"days (expected every {every} day(s)).")
                else:
                    lines.append(
                        f"reports: last delivered {age_d:.1f} day(s) ago "
                        f"(expected every {every} day(s)).")

        # This is the outage surface. Keyed on the CONDITION REPORTED, not on
        # the code path -- show_status previously returned status "ok"
        # unconditionally, so a total delivery outage (fail_closed) rendered
        # as a green toast. `error` is the only surface that persists (the
        # toast is transient and hardcoded green), so every one of these
        # must land there, uncapped.
        problems = []

        # DELIVERY FAILURE had no problem condition at all: `stats` is per-tick
        # and overwritten every tick, so a channel failing 100% of its sends
        # for a week produced a green toast identical to a healthy one. Recent
        # is what makes it actionable -- a bare cumulative counter would go
        # permanently red on one ancient failure, so an old total is reported
        # (below) but is NOT a problem.
        delivery = state.get("delivery")
        delivery = delivery if isinstance(delivery, dict) else {}
        try:
            gave_up = int(delivery.get("gave_up") or 0)
            no_route = int(delivery.get("no_route") or 0)
            not_delivered = int(delivery.get("not_delivered") or 0)
            last_fail_ts = float(delivery.get("last_failure_ts") or 0) or None
        except (TypeError, ValueError):
            gave_up = no_route = not_delivered = 0
            last_fail_ts = None
        fail_total = gave_up + no_route + not_delivered
        fail_recent = (last_fail_ts is not None
                       and (now - last_fail_ts) <= _RECENT_FAILURE_S)
        if fail_total:
            age = ("" if last_fail_ts is None
                   else f", last {(now - last_fail_ts) / 3600.0:.1f}h ago")
            lines.append(
                f"delivery failures: {gave_up} gave up, {no_route} reached "
                f"nothing, {not_delivered} partly failed{age}")
        if fail_recent:
            detail = str(delivery.get("last_failure") or "")
            problems.append(
                f"delivery is FAILING: {gave_up} event(s) gave up after every "
                f"retry, {no_route} reached nothing, {not_delivered} partly "
                f"failed. Most recent: {detail}")

        if not spool_writable:
            problems.append(
                "the spool directory is missing or not writable; callers "
                "are losing 100% of their events silently.")
        if tick_error:
            problems.append(
                f"the collector tick is raising ({tick_error.get('type')}, "
                f"x{tick_error.get('count')}); delivery is not running.")
        if sidecar_write_errors:
            problems.append(
                f"{sidecar_write_errors} sidecar write(s) failed; affected "
                f"events re-send to already-delivered channels each tick and "
                f"never terminate. Check that the spool disk is writable.")
        if state_lost:
            problems.append(
                "state was recovered from a corrupt file; held events, "
                "repeat windows and open incidents were lost.")
        if spool_full_at is not None:
            problems.append(
                "a CRITICAL was refused and dropped because the queue was "
                "full (spool_full marker present).")
        if lock_error:
            problems.append(
                f"leader lock FAULT ({lock_error.get('type')}); no worker "
                f"can become leader; nothing is being delivered.")
        if ticker_file_missing:
            problems.append(
                "ticker.txt is MISSING; channels using the ticker "
                "OutputProfile will hang.")
        if fail_closed:
            problems.append(fail_closed_summary)
        if not heartbeat_fresh:
            problems.append(
                f"heartbeat is stale; the collector may be down or stuck, "
                f"or unable to WRITE its state. Check that {base_dir} is "
                f"writable by the dispatch user and not out of disk.")
        if held_overflow:
            problems.append(
                f"{held_overflow} held event(s) were DROPPED; the held "
                f"queue hit its cap.")
        if not enabled:
            problems.append(
                "no channel is enabled; nothing can be delivered.")
        # Same outage SHAPE as the two above: delivery is silently narrowed to
        # the default channels, so a `critical -> smtp` rule can be gone and
        # email receiving nothing while every other line here reads healthy.
        if settings.get("_resolved_routing_error"):
            problems.append(
                f"Routing rules could not be read "
                f"({settings['_resolved_routing_error']}). ALL rules were "
                f"discarded and events route to the default channels only.")
        if critical_held:
            problems.append(
                f"{critical_held} CRITICAL event(s) are currently held, "
                f"not delivered.")
        if report_overdue is not None:
            problems.append(
                f"no report attachment delivered in {report_overdue:.0f} "
                f"days (expected every {settings.get('report_expect_days')}).")
        for problem in report_problems:
            problems.append(problem)

        # Task 5: a root:root readout dir (the E3 trap) degrades every
        # action's Output: file every time, and until now the only trace was
        # a note prepended to a 4-second toast. Recency-gated exactly like
        # the delivery-failure check above (`_RECENT_FAILURE_S`) -- a bare
        # cumulative counter would otherwise go permanently red on one
        # ancient, since-fixed failure, the same lesson that check exists to
        # apply. Read from the RUNTIME dir, not the readout dir --
        # _note_readout_failure deliberately writes there, since it is the
        # readout dir that just failed. Defensively parsed: this file can be
        # absent, empty, corrupt JSON, a JSON value that is not a dict, hold
        # a non-numeric `errors`, or (E3 trap again) be a directory -- none
        # of that may break show_status, the operator's one diagnostic.
        try:
            health = json.loads(open(os.path.join(
                base_dir, "readout_health.json"),
                encoding="utf-8").read())
        except Exception:
            health = {}
        if not isinstance(health, dict):
            health = {}
        try:
            readout_errors = int(health.get("errors") or 0)
        except (TypeError, ValueError):
            readout_errors = 0
        readout_last = health.get("last")
        readout_last = readout_last if isinstance(readout_last, dict) else {}
        readout_last_reason = readout_last.get("reason")
        readout_last_action = readout_last.get("action")
        try:
            readout_last_ts = float(readout_last.get("ts") or 0) or None
        except (TypeError, ValueError):
            readout_last_ts = None
        readout_recent = (readout_last_ts is not None
                          and (now - readout_last_ts) <= _RECENT_FAILURE_S)
        if readout_errors:
            age = ("" if readout_last_ts is None
                   else f", last {(now - readout_last_ts) / 3600.0:.1f}h ago")
            currency = ("" if readout_recent
                       else " (historical, not currently failing)")
            lines.append(
                f"readout files: {readout_errors} write failure(s) total{age}; "
                f"most recent {readout_last_reason} on {readout_last_action}"
                f"{currency}")
        if readout_recent:
            problems.append(
                f"readout files are not being written "
                f"({readout_last_reason}); the persistent Output: readout "
                f"is unavailable")

        ok = not problems
        message = self._fit_message(problems)

        return self._result(
            ok, message, error="; ".join(problems) if problems else None,
            details=lines,
            fail_closed=fail_closed,
            fail_closed_reason=fc_reason,
            fail_closed_count=fc_count,
            heartbeat_fresh=heartbeat_fresh,
            heartbeat_age_s=heartbeat_age,
            spool_depth=spool_depth,
            oldest_unclaimed_s=oldest_unclaimed_s,
            held=held_count,
            held_by_severity=held_by_severity,
            dedup_active=dedup_count,
            held_overflow=held_overflow,
            dedup_overflow=dedup_overflow,
            ticker_drawn=ticker_drawn,
            ticker_declined=dict(ticker_declined),
            event_handler_errors=event_errors,
            enabled_channels=len(enabled),
            held_no_channels=held_no_channels,
            timezone=tz_name,
            redact_host_count=len(effective),
            redact_hosts_stale=bool(host_info.get("stale")),
            ledger_tail=[_mask_record(rec, hosts=effective) for rec in tail],
        )

    @staticmethod
    def _flush_message(held_before, held_after):
        """How many were ACTUALLY released.

        This used to read "Flushed {held_before}; {held_after} remain" -- with 5
        before and 3 after it claimed 8 events had existed, because it reported
        the PRE count as the flushed count. It also said "Flushed", implying
        delivery; the tick releases the hold and ATTEMPTS delivery.
        """
        if held_after >= held_before:
            return (f"Flush made no progress: {held_before} held before, "
                    f"{held_after} held after.")
        return (f"Released {held_before - held_after} of {held_before} held "
                f"event(s) for delivery; {held_after} still held.")

    def _action_flush_held(self, settings, params):
        base_dir = _base_dir(settings)
        opener = params.get("_opener")
        smtp_factory = params.get("_smtp_factory")
        st = store.Store(base_dir, redact=redact)
        st.ensure_dirs()
        now = time.time()
        state = st.load_state(now)
        g = gates.Gates(state, settings, lambda: now)
        gg = g._g()

        held_before = len(gg["held"])
        if held_before == 0:
            return {"status": "ok", "message": "No held events to flush.",
                    "flushed_before": 0, "flushed_after": 0}

        # due_flushes() only releases a held record whose CURRENT hold
        # reason has cleared (quiet hours ended / hourly cap window rolled
        # over) -- a manual flush means "send them now regardless of why
        # they were held". Gates._held_released() treats any reason other
        # than "quiet" or "cap" as unconditionally released.
        for held in gg["held"]:
            held["reason"] = "manual_flush"
        st.save_state(state, now)

        mods = {"store": store, "spool": spool, "routing": routing,
                "gates": gates, "channels": channels, "deliver": deliver,
                "report_expect": report_expect}
        wire_redact = collector.build_wire_redact(channels, settings, redact)
        # Deliberately NO ticker here: held events are stale by definition
        # (quiet hours, or an hourly cap that has since rolled over), so a
        # manual flush at 8am would paint a banner about a 2am fault.
        deliverer = deliver.Deliverer(st, settings, PLUGIN_VERSION, lambda: now,
                                       mods, opener=opener, wire_redact=wire_redact,
                                       smtp_factory=smtp_factory)
        stats = deliverer.tick(int(now * 1000))

        after_state = st.load_state(now)
        held_after = len((after_state.get("gates") or {}).get("held") or [])

        # held_before > 0 was already checked above (the early return), so
        # reaching here means a flush was actually attempted. If nothing
        # moved -- held_after didn't drop below held_before -- reporting
        # "ok" would be the same green-toast-over-an-outage shape as
        # show_status previously had.
        stalled = held_after >= held_before
        message = self._flush_message(held_before, held_after)
        return self._result(
            not stalled, message,
            error=message if stalled else None,
            flushed_before=held_before, flushed_after=held_after,
            delivered=stats.get("delivered", 0))
