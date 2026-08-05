# Newsflasharr caller API

This is the contract for any plugin that wants to send a notification through
Newsflasharr. It is versioned and compatibility-governed (§3.5 below) because the
audience is the user's own plugins first and third-party plugins later. A
caller vendors this client once and never has to touch it again.

This document is the caller contract. It is the published source of truth for
anything a calling plugin depends on; the project's internal design documents
are not published.

## 1. Vendor the client

Copy `newsflasharr/notify_client.py` from this repository into your own plugin's
inner folder. It is
stdlib-only, no imports from Newsflasharr, no import coupling, no dependency on
Newsflasharr being installed, enabled, or even present. If Newsflasharr is absent,
`notify()` still returns `True`/`False` (spooling to a directory nobody reads)
or `False` (spool guard / IO error), it never raises and never blocks your
plugin.

Do not hand-edit your vendored copy. If the shared core changes, re-copy the
whole file (like `matching_core.py`'s `sync_core.py` workflow) rather than
patching in place. That keeps every caller byte-identical and lets a hash
check (if one is later added) actually mean something.

```python
from .notify_client import notify

notify(source="my-plugin", event="black_screen",
       title="Black screen: US: 24/7 AL PACINO",
       body="All 4 sources black, breadth-confirmed in 40s",
       severity="critical",              # critical | warning | info
       kind="event",                     # event | resolve
       dedup_key="black_screen:72877",   # optional; storm-collapse key
       url="http://...")                 # optional link
```

## 2. The function

Actual signature, from `newsflasharr/notify_client.py`:

```python
def notify(source, title, *, event=None, body="", severity="info",
           kind="event", dedup_key=None, url=None, attachment=None,
           base_dir=DEFAULT_BASE, _now_ms=None) -> bool:
```

(`_now_ms` is a test-only injection hook; real callers never pass it.
`base_dir` defaults to `/data/newsflasharr` and normally never needs overriding.)

- **Never raises. Returns `True` or `False`.** `True` means "durably spooled",
  **not** "delivered", and not even "will be delivered". Delivery, routing,
  dedup, quiet hours, and retry all happen later, inside Newsflasharr's own
  collector.
- **Redacts before writing** (see §6 Security below): `redact()` runs on
  `title`/`body`/`url` **before** the 64 KB body truncation, so a truncation
  boundary landing mid-credential can never defeat the regex.
- **Atomic write:** a dot-prefixed temp file (`.tmp-*`) is written into the
  spool directory, then `os.replace`'d into place. Newsflasharr (and any other
  consumer) ignores dot-prefixed files, so a half-written event is never
  visible.
- **Payload cap:** 64 KB. Oversize bodies are truncated rather than dropped:
  the event still gets spooled and delivered.
- **`attachment` (optional, str-coerced like `source`):** an **absolute path**
  to a file to email as a real attachment alongside the notification. See §3a
  below for the full contract. It is **smtp-only**, is sent **verbatim and
  unredacted**, and has real requirements on the caller (immutable path,
  allowed extension, size). Passing it does not change what gets spooled here
  beyond adding the one field: `notify()` never opens or reads the file
  itself, so a bad path or an oversize file cannot make `notify()` fail.
  Those checks happen later, at Newsflasharr's smtp send site.
- **Filename** is `<epoch_ms>-<counter>-<source>-<rand8>.json`. It exists only
  for ordering and uniqueness: the JSON body is the entire contract. Newsflasharr
  accepts any `*.json` file in the spool regardless of name.
- **Spool guard:** if the spool already holds >= 1000 `*.json` files, `notify()`
  drops the event and returns `False`. The file count is cached per process
  for ~5 s so a storm of calls does not pay a `listdir()` on every single call.
  Because Newsflasharr's collector claims files out of the spool promptly, a full
  spool means exactly one thing: **Newsflasharr is absent or wedged**, never
  "retry backlog" or "quiet-hours hold" (those states live inside Newsflasharr's
  own `pending/` directory, which the client never touches or measures).
- **`notifier_alive(base_dir=DEFAULT_BASE, max_age_s=120.0) -> bool`:** checks
  whether `/data/newsflasharr/state.json` has a fresh heartbeat. Use it to tell
  "Newsflasharr is dead/not installed" (worth logging once, worth surfacing in
  your own plugin's self-health) apart from ordinary backpressure. Callers
  should keep their own `notify_ok`/`notify_dropped` counters so a silently
  dead Newsflasharr is visible from the caller's side too. The principle is
  that a sensor should report when it cannot see, and it applies to both ends
  of this pipe.

## 3. The spool JSON schema (v1)

```json
{"v": 1, "client_v": "1.1.0", "source": "my-plugin",
 "event": "black_screen", "kind": "event", "severity": "critical",
 "title": "...", "body": "...", "dedup_key": "black_screen:72877",
 "url": "http://...", "attachment": "/data/my-plugin/report-20260720-135050.html",
 "ts": 1752770000123}
```

| Field | Required | Notes |
|---|---|---|
| `v` | yes | Schema version. Currently `1`. |
| `source` | yes | Your plugin's short name. Sanitized to `[a-z0-9_-]{1,32}` for the filename; the JSON field is authoritative for routing/dedup. **Identifier, not content:** `source`, `event` and `dedup_key` are NEVER redacted (title/body/url are) and the generic-webhook channel puts `source`/`event` on the wire verbatim, so never place credentials, hostnames or any sensitive text in them. |
| `title` | yes | Redacted, truncated to 1024 bytes UTF-8-safe. |
| `ts` | yes | Epoch **milliseconds**. |
| `client_v` | no | The vendored client's own version string, for diagnostics only. Currently `1.1.0`. |
| `event` | no | Free-form event name (e.g. `black_screen`). Used in routing match and in `dedup_key` conventions, not otherwise interpreted. |
| `kind` | no | `event` (default) or `resolve`, see §4. |
| `severity` | no | `critical` \| `warning` \| `info` (default `info`). Closed 3-level enum. |
| `body` | no | Redacted, truncated to 64 KB UTF-8-safe. |
| `dedup_key` | no | Storm-collapse identity is `(source, dedup_key)`. No `dedup_key` means no collapse: a keyless storm is only rate-capped, and is never silently defaulted to `source:event`. |
| `url` | no | Optional link. Scheme allowlist is `http`/`https` on the delivery side, anything else is dropped before it can reach a clickable surface. |
| `attachment` | no | Absolute path to a file to email as an attachment. **Not redacted, unlike every other field here.** See §3a. |

`category` was deliberately cut from v1 (a free-form vocabulary invites
silent-never-match routing rules); re-adding it later is free under the
version-skew policy below.

## 3a. `attachment`, the full contract

Added in `client_v` `1.1.0`. This is the one field on the wire that is
**never passed through `redact()`**: it is a filesystem path, not
attacker-reachable text, and there is nothing in a path string for the
credential regexes to usefully match.

- **smtp-only.** Discord/webhook/ntfy/Apprise have no concept of an
  attachment; if a report event routes to one of them it is delivered as an
  ordinary notification (whatever is in `url`/`body`), with no file. Only
  when it reaches the smtp channel does the file get read and attached.
- **Absolute path, in Newsflasharr's own filesystem namespace** (same
  container, same user as every plugin), not a URL, not relative to your
  plugin's own directory.
- **Allowed extensions: `.html`, `.htm`, `.csv` only.** Anything else is
  rejected before the file is opened (`attachment_rejected` in the ledger);
  the email still sends, just without the file.
- **Capped at 1 MB.** An oversize file is rejected the same way
  (`attachment_too_large`) rather than silently truncated.
- **Read fd-safe and bounded:** opened `O_NOFOLLOW | O_NONBLOCK`, checked
  `S_ISREG` on the open file descriptor (not the path, closes the
  check-then-open race), read in a bounded loop. This guards against
  **accidents** (a symlink, a FIFO, a huge file), **not** malice. Every
  plugin in a Dispatcharr install runs as the same user in one process; there is no
  privilege boundary a hardlink could not defeat. Do not rely on it as one.
- **Sent verbatim, byte-exact, never redacted.** No content scan runs over
  it either (an earlier design considered one, and it was cut, see the design
  spec §5 for why a credential scan over an arbitrary HTML/CSV file is
  security theater, not a control). **Never point `attachment` at anything
  that could contain a credential, a raw stream URL, or provider hostnames.**
  If the file might contain any of that, do not attach it, link to it instead
  (`url=`) and let the operator open it somewhere that redaction applies.
- **The path must be immutable.** The file is re-read from disk on every
  smtp send attempt, including retries on the normal 30s/300s/1800s ladder.
  there is no caching, no single-read-then-forget. Point `attachment` at a
  timestamped snapshot the producer never rewrites (e.g.
  `report-20260720-135050.html`), never a live file a scheduler overwrites in
  place (e.g. a bare `report.html`), a retry an hour later would attach
  whatever happens to be at that path *then*, not what triggered the
  original notification.
- **Bypasses quiet hours and the hourly cap.** Any event carrying
  `attachment` is delivered immediately rather than held. A held event
  becomes a text-only digest line and the file would be lost. This applies
  to any caller that sets the field; it is not separately configurable.
- **Retried normally on failure.** An smtp send with an attachment that
  fails (a dead mail server, an auth error) retries on the same ladder as
  any other smtp send: there is no separate "attachment delivery" outcome.
- Recommended pairing: always send `url=` alongside `attachment=` pointing at
  the same content served over HTTP. The link is the fallback if a gateway
  strips the attachment or the recipient is off the LAN.

## 4. `kind`: open/resolve pairing

`kind: "resolve"` marks an all-clear for a prior event with the same
`(source, dedup_key)`.

- A resolve is **exempt from dedup collapse**: it must never be swallowed by
  its own open's collapse window.
- A resolve **inherits the open's routing** and bypasses quiet hours **iff**
  the open itself was delivered with a bypass (a 2 a.m. critical page gets its
  2:40 all-clear at 2:40, not folded into the 08:00 digest).
- A resolve whose open was never actually delivered (debounced away, deduped,
  expired) is dropped silently, so there are no orphan all-clears.

If you emit `resolve` events, use the same `dedup_key` convention for both the
open and the close, e.g. `dedup_key = f"{incident_type}:{channel_id}"`.

**Digests are the one exception to "your event, your routing."** A
dedup-storm summary or a released-held-events digest is synthesized
internally as `source: "notifier"` and routed fresh through
`routing_rules`/`default_channels`, so it does **not** inherit the channels of
the event(s) it summarizes (unlike a `resolve`, which does inherit its
open's routing, per above). If your routing rules do not have a catch-all for
`source: notifier`, digests land on `default_channels`, which may not be
where the original events went.

## 5. Delivery contract: read this before you page anyone

**Delivery is at-least-once per channel.** Duplicates are possible across
crashes and leader handovers. `notifications.jsonl` (Newsflasharr's ledger) is
the record of what actually went out. A rare double-page is by design, not a
bug, do not build caller-side logic that assumes exactly-once delivery.

**Ordering:** FIFO by filename within a single `source`, under normal
operation. There is no cross-source ordering promise.

## 6. Version-skew policy, and why this contract will not change under you

Vendored clients never get updated in place once a caller ships, so Newsflasharr
will forever see event files written by clients both older and newer than
itself. So, in writing, from v1 forward:

1. Validation checks **required fields and types only** (`v`, `source`,
   `title`, `ts`). Unknown fields are ignored for processing but preserved
   (re-redacted) into the ledger, so adding a field to your events is always
   safe, even before Newsflasharr knows what to do with it.
2. `v` only bumps when required-field *semantics* break, not for additive
   changes.
3. A file with `v` greater than the running Newsflasharr understands is
   **best-effort parsed and delivered anyway**, tagged `client_newer` in the
   ledger, never routed to `failed/` for version alone.
4. An unknown `severity` value coerces to `warning`, is still delivered, and
   is tagged `severity_coerced` in the ledger. The 3-level severity enum is
   closed; `kind` already removes the only real pressure that would otherwise
   push it open.
5. `client_v` (the vendored client's own version) travels in the payload
   purely for diagnostics: it plays no role in routing or delivery decisions.

The practical upshot: you can vendor this client today, never touch it again,
and a future Newsflasharr release adding fields/severities/event kinds will not
break you, and your old client will not confuse a future Newsflasharr either.

## 7. Security: strings here reach the outside world

Everything you pass to `title`, `body`, and `url` may be POSTed to an
external third-party service (Discord, a generic webhook endpoint, ntfy,
Apprise's 110-service fan-out). Treat this exactly like writing to a public
log:

- **Never place raw stream URLs, provider credentials, or unfiltered
  exception text in `title`/`body`.** `notify_client.redact()` strips the
  common shapes: `/live|movie|series/<user>/<pass>/...` path segments and
  `?username=`/`?password=`-style query credentials, and `user:pass@host`
  basic-auth, **before** the 64 KB truncation, at spool-write time.
  Newsflasharr's own collector applies `redact()` again at the wire as
  defense-in-depth.
- **Redaction is best-effort shape-matching, not a guarantee.** No regex can
  know every credential transport a provider might invent. If a string did not
  come from a URL builder you control, do not assume it is safe just because it
  passed through `redact()`.
- **Hostname redaction** is the backstop for provider hostnames the built-in
  patterns cannot recognise by shape alone: any URL anywhere in an outbound
  string whose host matches is replaced wholesale with `<redacted-url>`. This
  runs on Newsflasharr's side (wire-side) rather than in the client, because it needs the
  operator's per-box hostname list, which the client has no way to know.
  The list is derived automatically from the operator's EPG sources and M3U
  accounts, **unioned** with whatever they typed into the `redact_hostnames`
  setting.
- **Do not rely on auto-derivation to cover your strings.** It knows only the
  hosts Dispatcharr itself stores. It does not know your webhook targets, your
  LAN hosts, your reverse proxy, or any hostname *your* plugin puts in a body.
  those are covered only if the operator adds them by hand. It also matches
  full `http(s)://` URLs only: a bare hostname in prose is not redacted. Treat
  it as a safety net under your own discipline, not a substitute for it.
- Ledger entries (`notifications.jsonl`) store metadata plus the **redacted**
  title only, never a raw body. Target URLs are never written to the ledger,
  status output, or test-action results either (they are masked to
  scheme+host+first-path-segment). Status dumps get pasted into GitHub
  issues, so a webhook token leaking into a failure detail would
  eventually go public.

If in doubt, summarize rather than paste: `"Provider connection refused"`
beats `f"Failed: {raw_upstream_url}"`.

**`attachment` is the one exception to all of the above.** It is a path, not
POSTed text, and the file it points at is sent to the smtp recipient
byte-exact with none of `redact()`/`redact_hosts()` applied. See §3a. Treat
it like handing someone a file directly, not like writing to a log.

## 8. Optional: publish a count of the reports you have built

This is a separate, optional file. It is not part of `notify()`, it is not part
of the spool schema, and nothing breaks if you never write it.

If your plugin builds reports, you can publish how many it has built, and
Newsflasharr's Show status action will display that number beside your plugin.
Write one file:

```
/data/<source>/report_count.json        {"reports_built": 42}
```

`<source>` is the exact string you pass as `source` to `notify()`. One key, one
integer. That is the whole interface.

**That directory is named after your `source` string, which is often not the
directory your plugin already uses for its own runtime files.** One caller runs
out of `/data/stream_health/` and writes this counter to `/data/sentinelarr/`,
because `sentinelarr` is what it passes as `source`. Check the string rather than
reusing a path you already have.

**Count reports whose file you confirmed was written to disk.** Increment once
per successful build, whether an operator pressed a button or a scheduled job
ran. A build that failed to write must not increment it. A report writer that
degrades instead of raising makes a failed publish look identical to a good one,
and a counter that increments anyway turns that failure into apparent success.

Newsflasharr shows no count, and reports no error, if any of the following is
not true. There is no feedback surface for a malformed file, so test against
these rather than waiting to see the number appear.

| Requirement | Reason |
|---|---|
| The value is a JSON integer | A float is refused rather than truncated, because `int(2.9)` is 2 and a silent under-report looks deliberate |
| The value is not a JSON boolean | `bool` is a subclass of `int` in Python, so `true` would otherwise display as one report |
| The value is zero or greater | A negative value is treated as a corrupt file |
| The top level is a JSON object | Anything else is refused |
| The whole file is under 4096 bytes | The read is bounded before the content is parsed, because it happens on an operator click inside a web worker |
| It is a regular file the plugin user can read | Newsflasharr runs as the same user your plugin does |

A stored `0` is displayed as zero reports built. An absent file is not an error
and displays nothing at all, which is the correct display for a plugin that
publishes no counter.

Write it atomically, with a temporary file in the same directory followed by
`os.replace`, so a reader never sees a partial file. Do not let a failure to
write it fail the report itself.

**No locking is expected.** An unlocked read, modify and write across several
workers can lose an increment when two reports finish in the same instant.
Newsflasharr treats the number as a floor rather than an audit, and says so on
screen next to it.

**Two things this number is deliberately not.** It is not a delivery count: a
report built while notifications are switched off still increments it. It is not
backed by a database, so clearing your plugin's data directory resets it.

**Do not put the number only in the notification body.** Prose in a body is for
a person reading the mail. It is not an interface, and it breaks the first time
the wording changes. `notify()` takes a closed keyword set and writes a fixed
spool schema, so there is nowhere on the event itself to put arbitrary data.
That is why this is a file.

One behaviour that reads as a fault and is not: your plugin appears in the Show
status report list only once Newsflasharr has recorded a delivered report from
it. A plugin that writes this file but has never delivered a report through
Newsflasharr shows nothing, whatever the file says.

## 9. A note on action results (not part of this API)

This is unrelated to `notify()` and the spool schema above; it concerns
Newsflasharr's own plugin *actions* (Validate settings, Show status, and so
on), which nothing in this document calls. Recorded here only because it
affects the same "what reaches the outside world" boundary as §7.

Every action attempts to write its complete output to a file, on success OR
failure of the action itself, and carries a `file` key with that path
**when the write succeeds** (see the README's "Every action writes its
complete output to a file" for the operator-facing version). The write
itself can independently fail (a full disk, a permissions problem) --
`status` stays whatever the action's own outcome was, but `file` is
deliberately ABSENT, and the toast carries a prepended
`[readout file NOT written: <reason>]` note instead. **A consumer must
check for the key rather than assuming it, even on an otherwise-green
result**; indexing `result["file"]` unconditionally raises `KeyError`
exactly when the filesystem is the thing that is broken. `_headline` and
`_readout` are internal composition keys used to build that file; they
are popped from the result dict before it is returned, so they are never
present in a payload an operator, a log, or a test sees.
