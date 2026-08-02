"""Action readout files -- the persistent surface for action output.

WHY THIS MODULE EXISTS. Dispatcharr's plugin card renders exactly three things
from an action result: `message` (a TRANSIENT green toast), `error` (red,
persistent) and `file` ("Output: <path>", persistent). `details`, `problems`
and `checks` appear in NO frontend bundle. And the toast is hard-clipped:
Mantine's Notifications is mounted with notificationMaxHeight left at its
default 200px, which is ~7 lines / ~280 characters at the configured width, and
it clips from the MIDDLE with no ellipsis. So `file` is the only surface a
complete, green readout can occupy.

Stdlib only, and standalone-loadable (no relative imports) like every module
here except plugin.py. Header composition deliberately lives in plugin.py: it
needs Dispatcharr's resolved timezone, and reaching for that here would break
the standalone contract.
"""
import errno as _errno
import os
import re
import tempfile

# /config is a bind mount, so these files are readable from the host without
# `docker exec`. Deliberately NOT /data/logos, which Dispatcharr's own nginx
# serves UNAUTHENTICATED to the whole LAN and which would therefore publish
# the derived provider hostname list.
DEFAULT_DIR = "/config/newsflasharr"

_ACTION_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def safe_action(action):
    """-> `action` if it is a bare, filename-safe token; else None.

    Every caller of `write()` (and any code -- plugin.py's `_stub_stale_
    readout` included -- that builds a "<action>.txt" path by hand) MUST
    gate on this first. `action` is not manifest-constrained by the time it
    reaches here: Plugin.run()'s unknown-action branch passes through
    whatever action id the caller asked for, so this is effectively
    untrusted input. os.path.join(base_dir, f"{action}.txt") does NOT
    protect against it -- a "../" component is concatenated as-is and only
    resolved by the OS at open time (walking above base_dir), and on a
    string that os.path recognises as itself absolute, os.path.join
    DISCARDS base_dir entirely. Reject rather than sanitize: turning
    "../../evil" into some other string the caller did not ask for is its
    own kind of surprise.
    """
    return action if isinstance(action, str) and _ACTION_RE.match(action) else None


def _reason(exc):
    """`TypeName` plus the errno NAME -- never `str(exc)`.

    `str(exc)` on an OSError carries the mount and path, and show_status output
    gets pasted into GitHub issues (the `_note_settings_error` rule).

    The errno is not decoration. The three realistic failures here all arrive
    as a bare PermissionError/OSError otherwise, and they need three different
    fixes: an E3 root-owned directory (chown it), a Windows editor holding the
    file open (close it -- and that IS the expected workflow, since this file
    exists to be opened), and a full disk. errno names carry no path, host or
    mount detail, so the redaction rule stays intact.
    """
    name = type(exc).__name__
    code = getattr(exc, "errno", None)
    if code is not None and code in _errno.errorcode:
        return f"{name}/{_errno.errorcode[code]}"
    return name


def _default_opener(base_dir):
    """-> (fd, temp_path). mkstemp implies O_CREAT|O_EXCL and mode 0600.

    A process-unique temp name, never a fixed `{path}.tmp`: dustarr shipped
    the fixed form and two concurrent writers interleaved into a torn file that
    still reported success.
    """
    fd, tmp = tempfile.mkstemp(dir=base_dir, prefix=".readout-", suffix=".tmp")
    return fd, tmp


def write(action, body, base_dir=DEFAULT_DIR, opener=None):
    """Write `<action>.txt` under base_dir. NEVER raises.

    -> (path, None) on success, (None, reason) on failure.

    Success means the artefact was VERIFIED, not merely that no exception was
    raised: the bytes are fsync'd before the replace and the resulting size is
    checked. Without that, a zero-byte or truncated file renders an "Output:"
    line indistinguishable from a good one.

    NOTE on atomicity: temp+replace protects against a TORN file, which was
    measured to hold on the production mount. It does NOT provide rename
    atomicity there -- os.replace on 9p/drvfs was measured to leave the target
    transiently absent for ~1.3% of concurrent reads (spec section 3.2). Do not
    upgrade this comment into an atomicity claim.
    """
    if safe_action(action) is None:
        # Reject BEFORE any filesystem touch -- not even os.makedirs(base_dir)
        # runs on this path, so an attacker-shaped action cannot even probe
        # for base_dir's existence, let alone escape it.
        return None, "BadAction"
    opener = opener or _default_opener
    tmp = None
    try:
        path = os.path.join(base_dir, f"{action}.txt")
        data = body.encode("utf-8")
        os.makedirs(base_dir, exist_ok=True)
        fd, tmp = opener(base_dir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            # 0644 for consistency with dustarr's files in the same parent.
            # mkstemp alone yields 0600, which was measured NOT to break the
            # Windows read (drvfs modes do not override NTFS ACLs) -- this is
            # tidiness, not a fix.
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
        tmp = None
        size = os.stat(path).st_size
        if size != len(data):
            return None, f"ShortWrite/{size}of{len(data)}"
        return path, None
    except Exception as exc:
        return None, _reason(exc)
    finally:
        if tmp is not None:
            # An orphaned .tmp litters a directory the operator browses.
            try:
                os.unlink(tmp)
            except OSError:
                pass
