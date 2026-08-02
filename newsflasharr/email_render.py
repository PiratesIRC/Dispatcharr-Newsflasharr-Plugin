"""Pure rendering for the SMTP channel: subject, addresses, message body.

No I/O, no smtplib, no network -- every function here is total over hostile
input and its tests need no fakes.

**Do not rename this module to `email.py`.** Dispatcharr inserts the plugin
directory on `sys.path`, so a module named `email.py` would shadow the stdlib
`email` package and break `EmailMessage` for the whole process.

Three behaviours here are counter-intuitive enough to state up front, each
measured on CPython 3.12 rather than assumed:

  - `EmailMessage.__setitem__` RAISES on a header value containing a line
    break -- it does not encode it. It uses `str.splitlines()` semantics, so
    U+2028 (category Zl) and U+2029 (Zp) raise too, even though neither is
    category Cc. Stripping is therefore an AVAILABILITY requirement, not
    defence in depth: one provider-supplied channel name with a newline would
    otherwise make every email for that event fail permanently.
  - NUL is the exception: it is neither rejected nor encoded, it travels to
    the wire verbatim. It IS category Cc, so the strip below removes it.
  - A caller-supplied RFC 2047 encoded-word contains no line break, passes
    every strip, and is DECODED on output -- so `=?utf-8?B?aGkNCg==?=`
    renders in the recipient's client as a subject containing a literal CRLF.
    That is a spoofed multi-line subject, not protocol-level injection, and
    `=?` is neutralized below to prevent it.
"""
from __future__ import annotations

import email.utils
import html
import unicodedata
from email.message import EmailMessage

SUBJECT_FALLBACK = "Newsflasharr notification"

# Categories that either raise inside EmailMessage (Cc/Zl/Zp) or reach the
# wire raw (NUL, itself Cc). Cf is included for addresses (see _ADDR_BAD_CATS);
# for subjects the zero-width chars are merely invisible, not dangerous, but
# stripping them costs nothing and they have caused real faults here
# before.
_SUBJECT_BAD_CATS = frozenset({"Cc", "Cf", "Zl", "Zp"})

_ADDR_BAD_CATS = frozenset({"Cc", "Cf", "Zl", "Zp"})
_ADDR_BAD_CHARS = frozenset('<>,;:"\\[]')

_SEVERITY_PREFIX = {"critical": "[CRITICAL] ", "warning": "[WARNING] "}


def strip_header_text(text):
    """Text safe to place in a header value.

    Removes every codepoint that would raise inside `EmailMessage` or survive
    to the wire raw, then neutralizes `=?` so a caller cannot smuggle an
    RFC 2047 encoded-word (see the module docstring).
    """
    if not text:
        return ""
    cleaned = "".join(c for c in str(text)
                      if unicodedata.category(c) not in _SUBJECT_BAD_CATS)
    return cleaned.replace("=?", "=_?").strip()


def subject_for(event):
    """`[SEVERITY] title`, stripped and never empty."""
    title = strip_header_text(event.get("title"))
    if not title:
        title = SUBJECT_FALLBACK
    return _SEVERITY_PREFIX.get(event.get("severity"), "") + title


def validate_address(entry):
    """A normalized `local@domain`, or None if `entry` is not usable.

    `parseaddr` first, so the `Display Name <a@b.com>` form an operator will
    type is accepted. But `parseaddr` is a NORMALIZER, NOT a sanitizer, and it
    signals failure by returning `('', '')` with no exception -- measured:

        parseaddr('a@x.com, b@y.com') -> ('', '')        # a whole list, lost
        parseaddr('a"@b.com')         -> ('', 'a')       # silent truncation
        parseaddr('a@b.com\\x0bRCPT')  -> passes the vertical tab straight through

    So the empty-result reject and the codepoint gate below are both load
    bearing. There is deliberately NO dot-in-domain requirement: it would
    reject `root@localhost` and `ops@mailserver`, which are exactly the
    unauthenticated-LAN-relay recipients this channel is meant to support.
    """
    if not isinstance(entry, str) or not entry.strip():
        return None
    addr = email.utils.parseaddr(entry)[1]
    if not addr:
        return None
    if addr.count("@") != 1:
        return None
    local, _, domain = addr.partition("@")
    if not local or not domain:
        return None
    for ch in addr:
        if ch.isspace() or ch in _ADDR_BAD_CHARS:
            return None
        if unicodedata.category(ch) in _ADDR_BAD_CATS:
            return None
    return addr


def parse_recipients(raw):
    """Comma-separated `smtp_to` -> (valid_addresses, invalid_indexes).

    Invalid entries are reported by **1-based index, never by value** -- the
    rule that lets `validate_settings` and a failure `detail` stay free of
    addresses while remaining diagnosable.
    """
    if isinstance(raw, (list, tuple)):
        entries = [str(x) for x in raw]
    else:
        entries = [p for p in str(raw or "").split(",")]
    valid, invalid = [], []
    for i, entry in enumerate(entries, start=1):
        if not entry.strip():
            continue
        addr = validate_address(entry)
        if addr is None:
            invalid.append(i)
        else:
            valid.append(addr)
    return valid, invalid


REPO_URL = "https://github.com/PiratesIRC/Dispatcharr-Newsflasharr-Plugin"


def source_label(event):
    """`sentinelarr` -> `Sentinelarr`, leaving deliberate casing alone.

    `str.capitalize()` is wrong here: it lowercases the tail, so a source
    like `EPG-Janitor` would render `Epg-janitor`.
    """
    src = str(event.get("source") or "").strip() or "unknown"
    return src[:1].upper() + src[1:]


def footer_text(event, stamp=None):
    """The credit line, as PLAIN TEXT. Pure: `stamp` is supplied by the
    caller, because this module never reads a clock or a timezone (the
    local zone is Dispatcharr's own, resolved in channels.py).

    The noun is chosen rather than fixed: this footer is on EVERY email,
    so calling a black-screen alert a "report" would be a small untruth in
    a line the operator is told to trust.
    """
    noun = "report" if "report" in str(event.get("event") or "") else "notification"
    credit = f"{source_label(event)} {noun} courtesy of Newsflasharr"
    return f"-- {stamp} {credit}" if stamp else f"-- {credit}"


def render_text(event, note=None, stamp=None):
    lines = [event.get("title") or "", ""]
    body = event.get("body") or ""
    if body:
        lines.extend([body, ""])
    url = event.get("url")
    if url:
        lines.extend([str(url), ""])
    if note:
        lines.extend([note, ""])
    lines.append(footer_text(event, stamp))
    lines.append(REPO_URL)
    return "\n".join(lines)


def render_html(event, note=None, stamp=None):
    """Newsflasharr's own HTML rendering.

    `html.escape` is load-bearing: event text routinely carries
    provider-controlled channel names (decorative glyphs, and potentially `<`
    or `&`), and unescaped that is HTML injection into the operator's mail
    client. `\\n` -> `<br>` is applied AFTER escaping because `html.escape`
    does not convert newlines -- without it a newline-joined digest body (the
    email you get after every quiet-hours window) renders as one run-on line.
    """
    def esc(text):
        return html.escape(str(text or "")).replace("\n", "<br>\n")

    parts = [f"<h2>{esc(event.get('title'))}</h2>"]
    body = event.get("body") or ""
    if body:
        parts.append(f"<p>{esc(body)}</p>")
    url = event.get("url")
    if url:
        safe = html.escape(str(url), quote=True)
        parts.append(f'<p><a href="{safe}">{safe}</a></p>')
    if note:
        parts.append(f"<p><em>{esc(note)}</em></p>")
    # esc() the whole credit line: `source` is caller-supplied and reaches
    # HTML here. REPO_URL is a module constant, never caller input.
    credit = esc(footer_text(event, stamp).lstrip("- ").strip())
    parts.append(f"<hr><p style=\"color:#888;font-size:12px\">"
                 f"{credit}<br>"
                 f"<a href=\"{REPO_URL}\">{REPO_URL}</a></p>")
    return "<html><body>" + "\n".join(parts) + "</body></html>"


def build_message(event, from_addr, to_addrs, note=None, attachment=None,
                  stamp=None):
    """`multipart/alternative` EmailMessage, plain part FIRST.

    Part order is a consequence of CALL order (`set_content` then
    `add_alternative`), not of `add_alternative` semantics -- a swap would
    silently ship mail that renders as plain text in every client, so the
    tests assert the order.

    `cte` is specified EXPLICITLY. The default is content-dependent and picks
    `8bit` for a short non-ASCII body -- i.e. every event carrying a glyph in
    a channel name -- and `smtplib.send_message` only negotiates
    `BODY=8BITMIME` from the ADDRESSES, never the body, so those raw high
    octets would reach a server that never agreed to accept them. Specifying
    `quoted-printable` also keeps every line under the RFC 5321 998-octet
    limit, which `7bit`/`8bit` would violate on a long line.

    `attachment`, when given, is a 4-tuple `(filename, maintype, subtype,
    data: bytes)`. Adding it promotes the message to `multipart/mixed` with
    the alternative part first and the attachment second. The payload is
    passed as `bytes` and never decoded -- `add_attachment` takes the raw
    (base64) path for `bytes`, which is byte-exact even for non-UTF-8 data;
    decoding first would require a charset and mangle it. The filename is run
    through `strip_header_text` because a raw newline in it raises inside
    `add_attachment`, same as it would in a header.
    """
    msg = EmailMessage()
    msg["Subject"] = subject_for(event)
    msg["From"] = from_addr
    msg["To"] = list(to_addrs)
    msg.set_content(render_text(event, note=note, stamp=stamp), subtype="plain",
                    cte="quoted-printable")
    msg.add_alternative(render_html(event, note=note, stamp=stamp), subtype="html",
                        cte="quoted-printable")
    if attachment is not None:
        filename, maintype, subtype, data = attachment
        safe_name = strip_header_text(filename) or "attachment"
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=safe_name)
    return msg
