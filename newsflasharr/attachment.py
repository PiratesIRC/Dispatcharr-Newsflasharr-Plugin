"""Newsflasharr attachment reader: fd-safe, bounded, standalone-loadable.

Guards ACCIDENTS (a buggy caller), not malice -- every plugin runs as the same
user in one process and a hardlink defeats realpath (spec 6). The controls that
do work: open through the fd (not path-then-reopen), reject non-regular files,
bound the read.
"""
from __future__ import annotations

import os
import stat

MAX_ATTACHMENT_BYTES = 1_048_576
ALLOWED_EXTENSIONS = (".html", ".htm", ".csv")
EXT_MIME = {".html": ("text", "html"), ".htm": ("text", "html"),
            ".csv": ("text", "csv")}


def _default_open(path):
    # O_NOFOLLOW rejects a symlink at open; O_NONBLOCK stops a FIFO/character
    # device open from BLOCKING the leader thread before fstat can reject it.
    # Both are POSIX-only, which is why `opener` is injectable -- the tests run
    # the S_ISREG/cap/short-read logic under a fake on the Windows dev box.
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    return os.open(path, flags)


def classify_extension(path):
    ext = os.path.splitext(str(path))[1].lower()
    return EXT_MIME.get(ext)


def read_attachment(path, cap=MAX_ATTACHMENT_BYTES, opener=None):
    opener = opener or _default_open
    try:
        fd = opener(path)
    except OSError:
        return None, "missing"
    try:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None, "not_regular"
            buf = bytearray()
            while len(buf) <= cap:
                chunk = os.read(fd, cap + 1 - len(buf))
                if not chunk:
                    break
                buf += chunk
        except OSError:
            # A read/stat failure mid-file (disk error, the file truncated or
            # replaced out from under us) used to propagate uncaught: this
            # function has no exception boundary above it all the way to
            # tick(), so one bad read would kill delivery of every OTHER
            # pending event in the tick, not just this one. Map it onto the
            # existing "missing" outcome -- it is effectively "could not read
            # this file" -- rather than adding a ledger outcome nothing maps.
            return None, "missing"
        if len(buf) > cap:
            return None, "too_large"
        return bytes(buf), None
    finally:
        os.close(fd)
