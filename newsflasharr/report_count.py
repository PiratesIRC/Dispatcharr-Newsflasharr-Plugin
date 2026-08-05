"""Reads the per-source published-report counter that sibling plugins write.

Stdlib only and standalone-loadable, like every module here except plugin.py.

THE CONTRACT, set by the plugin that writes the file, not by us:

    /data/<source>/report_count.json   ->   {"reports_built": 42}

One key, one non-negative integer, counting reports whose HTML file was
confirmed written to disk. It is written atomically, so a reader never sees a
partial file. Today only one plugin writes it; a source with no such file is
simply absent from the readout rather than reported as zero.

WHAT THE NUMBER IS NOT. It is not a delivery count: a report that was built
while notifications were off still increments it. It is not backed by a
database, so clearing the data directory resets it, and the writer increments
without a lock across several workers, so two reports finishing in the same
instant can lose one. The number is therefore a floor, not an audit, and this
module exists to display it, never to decide anything.

WHY NOT PARSE THE NOTIFICATION INSTEAD. The report email body carries a line
naming the report number, for a human reading the mail. Prose in a body is not
an interface and would break the first time the wording changed. The number is
not a structured field on the event because notify_client takes a closed
keyword set and is vendored byte-identically into five projects behind a hash
pin, so adding a field there is a five-project change.

WHY NOT COUNT THE LEDGER INSTEAD. notifications.jsonl records one line per
delivery attempt and carries `source`, but it does NOT carry `event` on a
delivery row (measured 2026-08-05: 2 of 166 lines had one), so a report cannot
be told apart from any other notification for the same source. The ledger also
rotates by size into a single previous file whose predecessor is discarded, so
a count over it covers a window of unknown length.
"""
import json
import os

# The whole documented file is around twenty bytes. The cap exists because
# this runs on an operator click inside a uWSGI worker, where a blocking read
# freezes every greenlet in that process, not only the request being served.
MAX_BYTES = 4096

# One file probe per entry, matched to report_expect's own cap on the same
# table, so one click cannot turn into unbounded disk input/output.
MAX_KEYS = 64

FILENAME = "report_count.json"


def read(source, data_root):
    """Reports `source` has published, or None when there is no usable count.

    None means "say nothing about this source". A genuine stored 0 is returned
    as 0, because "the counter exists and has never incremented" and "this
    plugin does not publish a counter" are different facts and only one of
    them is worth printing.

    Every unexpected condition degrades to None rather than raising: this
    feeds a status readout, which must never be the reason a page fails to
    render, and must never invent activity that did not happen.
    """
    if not _is_one_path_segment(source):
        return None
    path = os.path.join(data_root, source, FILENAME)
    try:
        # Size first, then content, so an unexpectedly large file is refused
        # rather than read. os.stat follows the link and reports the target,
        # which is what we want to bound.
        if not os.path.isfile(path) or os.stat(path).st_size > MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("reports_built")
    # bool is a subclass of int, so True would otherwise be accepted and
    # printed as "1 built". A float is refused rather than truncated, because
    # int(2.9) is 2 and a silent under-report looks deliberate.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def summarize(state, now, data_root):
    """Each source that has delivered a report, with its age and its count.

    -> [{"source", "event", "age_s", "count"}], sorted, count possibly None.

    `report_last` is the table the report-absence detector already maintains,
    keyed "source/event". Reading it here means a source appears in the
    readout as soon as it sends a report, with no per-plugin special casing
    and no list to keep in step.

    Returns [] on any shape that is not the expected one. state.json can be
    recovered from a corrupt copy, and a readout that raises on a damaged
    state file would hide every other line it was going to print.
    """
    table = state.get("report_last")
    if not isinstance(table, dict):
        return []
    out = []
    for key in sorted(table)[:MAX_KEYS]:
        if not isinstance(key, str):
            continue
        source, slash, event = key.partition("/")
        if not slash or not source or not event:
            continue
        # The same guard `read` applies, applied again here so an untrusted
        # source name is not merely denied its file but kept out of the
        # readout altogether. Without this a key "../outside/usage_report"
        # printed a row naming ".." -- no file was read outside the root,
        # because `read` refused it, but the row was still rendered.
        if not _is_one_path_segment(source):
            continue
        ts = table.get(key)
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        age_s = float(now) - float(ts)
        # A timestamp in the future means the clock moved backwards. Printing
        # a negative age would read as a defect in the plugin rather than in
        # the clock, so the entry is dropped instead.
        if age_s < 0:
            continue
        out.append({"source": source, "event": event, "age_s": age_s,
                    "count": read(source, data_root)})
    return out


def _is_one_path_segment(name):
    """Source names arrive from events spooled by other plugins, so they are
    not trusted to be safe path components. Without this check the source
    "../../etc" would make the reader open a file outside the data root."""
    if not isinstance(name, str):
        return False
    name = name.strip()
    if not name or name in (".", ".."):
        return False
    if name != os.path.basename(name):
        return False
    return "/" not in name and "\\" not in name
