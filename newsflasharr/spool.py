"""Newsflasharr spool layer: scan, claim, parse/validate, outcome sidecars.

Stdlib only, no imports of other notifier modules. Pure functions over
paths so deliver.py / collector.py can depend on exact signatures without
pulling in the rest of the plugin. Spec §3.5 (version-skew tolerance).
"""
from __future__ import annotations

import json
import os

_REQUIRED = {"v": int, "source": str, "title": str, "ts": int}
_KNOWN_SEVERITIES = {"critical", "warning", "info"}


def scan(spool_dir):
    """Full paths of *.json in spool_dir, dot-prefixed/sidecar files
    excluded, sorted by basename (ts_ms-prefixed filenames -> chronological).
    """
    try:
        names = os.listdir(spool_dir)
    except OSError:
        return []
    names = [n for n in names if n.endswith(".json") and not n.startswith(".")]
    names.sort()
    return [os.path.join(spool_dir, n) for n in names]


def claim(paths, pending_dir):
    """Atomically move each path into pending_dir; skip races silently."""
    claimed = []
    for p in paths:
        dest = os.path.join(pending_dir, os.path.basename(p))
        try:
            os.replace(p, dest)
        except OSError:
            continue
        claimed.append(dest)
    return claimed


def parse_event(path, now_ms, max_age_h):
    """Load + validate an event file. Returns (event, None) on success or
    (None, reason) on failure. Never rejects solely for v > 1 (spec §3.5).
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        return None, f"schema: unparseable ({exc})"

    if not isinstance(raw, dict):
        return None, "schema: not an object"

    for field, typ in _REQUIRED.items():
        if field not in raw:
            return None, f"schema: missing {field}"
        value = raw[field]
        # bool is an int subclass; keep it out of int-typed required fields.
        if typ is int and isinstance(value, bool):
            return None, f"schema: {field} must be int"
        if not isinstance(value, typ):
            return None, f"schema: {field} must be {typ.__name__}"

    event = dict(raw)
    tags = []

    event.setdefault("severity", "info")
    event.setdefault("kind", "event")
    event.setdefault("body", "")

    if event["severity"] not in _KNOWN_SEVERITIES:
        event["severity"] = "warning"
        tags.append("severity_coerced")

    if event["v"] > 1:
        tags.append("client_newer")

    if now_ms - event["ts"] > max_age_h * 3600 * 1000:
        return None, "expired"

    event["_tags"] = tags
    event["_path"] = path
    return event, None


def outcomes_path(event_path):
    return event_path + ".outcomes"


def read_outcomes(event_path):
    try:
        with open(outcomes_path(event_path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_outcomes(event_path, outcomes):
    op = outcomes_path(event_path)
    tmp = os.path.join(os.path.dirname(op) or ".",
                        f".tmp-{os.path.basename(op)}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(outcomes, f, separators=(",", ":"), default=str)
        os.replace(tmp, op)
        return True
    except OSError:
        # The sidecar is the ONLY record of per-channel progress. If it cannot
        # be written, the next tick sees no outcomes and re-admits the event as
        # a first admission -- re-sending to channels that already succeeded,
        # and `attempts` never passes 1 so it can never reach `gave_up`. The
        # caller surfaces this as a counter; there is nothing to recover here.
        return False


def clear_outcomes(event_path):
    try:
        os.remove(outcomes_path(event_path))
    except OSError:
        pass


def pending_events(pending_dir):
    return scan(pending_dir)


def reap_orphan_sidecars(pending_dir):
    try:
        names = os.listdir(pending_dir)
    except OSError:
        return 0
    sidecars = [
        n for n in names if n.endswith(".outcomes") and not n.startswith(".")
    ]
    events = set(names)
    removed = 0
    for n in sidecars:
        base = n[: -len(".outcomes")]
        if base not in events:
            try:
                os.remove(os.path.join(pending_dir, n))
                removed += 1
            except OSError:
                pass
    return removed
