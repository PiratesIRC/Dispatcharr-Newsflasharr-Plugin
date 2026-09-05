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


def write(action, body, base_dir=DEFAULT_DIR, opener=None, fallback_dir=None,
          header=None):
    """Write `<action>.txt` under base_dir, or under fallback_dir. NEVER raises.

    `header` is an optional callable taking `(path, fell_back)` and returning
    text to place above `body`. It exists because the header names the file's
    own location, and which of the two directories is used is not known until
    this function has tried one. Composing the header in the caller instead
    asserted the PREFERRED path on every readout, including the ones that
    landed in the fallback, so on any installation without a writable /config
    every file told its reader to look where it is not.

    `fell_back` is passed rather than left for the header to work out. The
    first version handed over only the path and the header inferred the rest
    by testing it against DEFAULT_DIR, which is wrong for any base_dir that is
    not literally DEFAULT_DIR: a successful write then described itself as a
    failed one. Deriving a fact this function already holds is the same
    mistake the callable exists to remove.

    A header that RAISES is not retried against the second directory. A header
    fault is not a property of the directory, so a retry pays for the same
    failure twice and reports one fault as two, which is the reasoning already
    applied to a write failure below.

    -> (path, None) on success, (None, reason) on failure. `path` names the
    directory actually used, so a caller surfacing it tells the operator where
    the file went without needing a separate signal.

    WHY THERE IS A FALLBACK. `/config/newsflasharr` is only writable on an
    installation that bind-mounts a host directory over `/config`. In the stock
    Dispatcharr image `/config` is `root:root` mode 755, so `os.makedirs` raises
    PermissionError for the non-root user the plugin runs as, and EVERY action
    lost its complete output: no `file` key, and the toast clips at ~280
    characters from the middle, which left Validate unreadable. Reported by a
    user 2026-08-07 and reproduced against the stock image. The fallback is the
    plugin's own runtime directory, which is writable by definition because the
    spool lives there.

    The preferred directory still wins whenever it works, so an installation
    that does mount `/config` keeps a file its operator can open from the host.

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
        # for base_dir's existence, let alone escape it. The fallback is not
        # tried either: the id is the problem, and a second directory cannot
        # make it safe.
        return None, "BadAction"
    path, reason = _write_to(action, body, base_dir, opener, header, False)
    if (path is not None or not fallback_dir or fallback_dir == base_dir
            or str(reason).startswith("HeaderError/")):
        # Retrying the SAME directory would pay for the failure twice and
        # report one fault as two. A raising header is the same case: the
        # fault travels with the header, not with the directory.
        return path, reason
    return _write_to(action, body, fallback_dir, opener, header, True)


def _write_to(action, body, base_dir, opener, header=None, fell_back=False):
    """One attempt at one directory. -> (path, None) or (None, reason).

    The caller has already validated `action`; this is not a public entry
    point and does not re-check it.

    `header` is called with `(path, fell_back)` for this attempt, so a header
    that names the file's location names the directory actually being written
    and does not have to work out which one that was.

    The directory is created BEFORE the header is composed. Composing first
    ran the header twice on the fallback route, which is the route every stock
    Dispatcharr image takes on every action press, and the first result was
    always discarded.
    """
    opener = opener or _default_opener
    tmp = None
    try:
        path = os.path.join(base_dir, f"{action}.txt")
        os.makedirs(base_dir, exist_ok=True)
        if header is None:
            text = body
        else:
            # Labelled separately from every other failure here. A header that
            # raises and a directory that refuses the write both leave the
            # operator with no file, and they need different fixes: one is a
            # code fault (the timezone lookup is the realistic one), the other
            # is ownership or a full disk. Reporting both as a bare exception
            # name loses that.
            try:
                text = header(path, fell_back) + body
            except Exception as exc:
                return None, "HeaderError/" + _reason(exc)
        data = text.encode("utf-8")
        fd, tmp = opener(base_dir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # The mode is deliberately left at mkstemp's 0600.
        #
        # It used to be widened to 0644 for cosmetic consistency with a sibling
        # plugin's files in the same parent directory, and the comment doing so
        # recorded that the narrower mode had been measured NOT to break
        # reading the file from Windows, because the mount's modes do not
        # override the filesystem's own access control. So the widening bought
        # nothing and cost something: these files print the DERIVED PROVIDER
        # HOSTNAME LIST, and both show_redaction.txt and validate_settings.txt
        # say so in their own headers. 0644 handed that to every other user on
        # the host.
        #
        # Found by CodeQL on the Dispatcharr Plugin Hub submission, rule
        # py/overly-permissive-file, and confirmed by measuring the mode inside
        # the container: it really was -rw-r--r--.
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
