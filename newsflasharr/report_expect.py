"""Per-caller report-absence detection: parsing, recording, arming, checking.

Stdlib only and standalone-loadable, like every module here except plugin.py.

WHY THIS EXISTS. The original detector stamped ONE timestamp,
state["last_attachment_delivered_ts"], on any successful smtp send carrying an
attachment (deliver.py). It could therefore answer "did an attachment email go
out recently" and nothing more. With several report callers, the loudest one
refreshes that timestamp and covers for every quiet one: a weekly report can die
completely while a daily one keeps the check green.

The key is `source/event`, not `source`. One caller here already emits three
different report events, and merging a weekly report with an on-demand one would
reintroduce exactly the blindness this module removes.
"""
MAX_KEYS = 64


def parse(raw, default_days):
    """`"a/b=7, c/d"` -> (entries, errors).

    An entry that cannot be read produces an ERROR rather than being dropped.
    A silently ignored entry is indistinguishable from a monitored one, which
    is the failure shape ticker_min_severity shipped with: an unrecognised
    value there mapped to the lowest severity, so a typo did not disable the
    banner, it drew everything over live television while the report printed
    the operator's value back as though it were in force.
    """
    entries, errors, seen = [], [], set()
    if not raw or not str(raw).strip():
        return entries, errors
    for chunk in str(raw).replace("\n", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        spec, _, days_text = item.partition("=")
        source, slash, event = spec.strip().partition("/")
        source, event = source.strip(), event.strip()
        if not slash or not source or not event:
            errors.append(f'cannot read entry "{item}": expected source/event')
            continue
        key = f"{source}/{event}"
        if key in seen:
            errors.append(f'duplicate entry "{key}": the first one is used')
            continue
        if days_text.strip():
            try:
                days = float(days_text.strip())
            except (TypeError, ValueError):
                errors.append(f'cannot read the cadence "{days_text.strip()}" '
                              f'for {key}')
                continue
            if days <= 0:
                errors.append(f"the cadence for {key} must be greater than zero")
                continue
        elif default_days:
            try:
                days = float(default_days)
            except (TypeError, ValueError):
                errors.append(f'cannot read the default cadence "{default_days}" '
                              f'for {key}')
                continue
            if days <= 0:
                errors.append(f"the default cadence must be greater than zero, "
                              f"not {default_days} (for {key})")
                continue
        else:
            errors.append(f"{key} has no cadence: give it one as {key}=N, or "
                          f"set the default in report_expect_days")
            continue
        seen.add(key)
        entries.append({"key": key, "source": source, "event": event,
                        "days": days})
    return entries, errors


def record_delivery(state, source, event, now):
    """Stamp `source/event` as delivered at `now`. Never raises.

    Bounded at MAX_KEYS, oldest evicted. A caller that emits a unique event
    name per send must not be able to grow state.json without limit: this
    dictionary is written by the collector leader several times per tick and
    is read by the status action.

    KNOWN LIMITATION, accepted deliberately. Eviction is by timestamp, so the
    oldest entry goes first, and a legitimately monitored caller with a very
    long cadence -- a weekly report -- is by construction among the oldest.
    Another caller emitting many distinct event names can therefore evict it,
    after which `arm` sees no delivery for that key and stamps it as newly
    configured, so it reads as never-delivered until its next send. The cap
    protects state.json from unbounded growth and that is the price it pays.
    """
    table = state.get("report_last")
    if not isinstance(table, dict):
        table = {}
    key = f"{source or ''}/{event or ''}"
    table[key] = float(now)
    if len(table) > MAX_KEYS:
        for stale in sorted(table, key=lambda k: table[k])[:len(table) - MAX_KEYS]:
            table.pop(stale, None)
    state["report_last"] = table
    # A real delivery is the ONLY thing that clears a history-lost mark, and
    # it clears it without the operator touching anything. The mark says "this
    # key was re-armed from a state file that had just been rebuilt, so its
    # arm timestamp proves nothing"; one genuine delivery replaces that with a
    # timestamp that does prove something, and the mark has no further job.
    lost = state.get("report_history_lost")
    if isinstance(lost, list) and key in lost:
        remaining = [k for k in lost if k != key]
        if remaining:
            state["report_history_lost"] = remaining
        else:
            state.pop("report_history_lost", None)


def arm(state, entries, now, history_lost=False, forget_stale=True):
    """Record when each expectation began. -> True when state changed.

    An expectation that has never delivered cannot be judged for absence
    against anything except the moment it was configured, so that moment has
    to be stored somewhere durable. `state["report_armed"]["<key>"]` is that
    store, keyed the same way as `report_last` so a later absence check can
    compare the two directly.

    THE CALLER MUST BE THE COLLECTOR LEADER, never the status action. The
    status action runs in whichever uWSGI worker served the click, while
    state.json is rewritten by the leader several times per tick from a
    different process, so arming from the status action would race those
    writes.

    An entry the operator has since removed from report_expect is forgotten
    here rather than left in `report_armed` forever: otherwise re-adding the
    same key later would read its old arm timestamp and appear instantly
    overdue, even though it has not had a chance to deliver yet.

    `forget_stale=False` suspends exactly that forgetting, and the caller
    passes it when the parse produced ERRORS. An operator who deleted an entry
    and an operator who typed one the parser cannot read reach this function
    the same way -- as a key that is armed but not wanted -- yet they mean
    opposite things. Forgetting on a parse error means that the moment the
    typo is corrected, a long-overdue expectation re-arms at `now` and reads
    green for a full cadence, which is the very blindness this module exists
    to remove. Keys are still ADDED while errors exist, since a valid entry
    beside a broken one is still being monitored.

    `history_lost=True` says this state dictionary was rebuilt from a corrupt
    file moments ago, so `report_last` is empty because the record was LOST,
    not because nothing was ever delivered. Every key armed under that flag is
    listed in `state["report_history_lost"]`, which `evaluate` turns into a
    problem rather than into the reassuring "none delivered yet" line a bare
    arm timestamp would earn it. The mark is per-key and durable: a caller
    that has been dead for a month cannot buy itself a fresh cadence by being
    re-armed, and a caller that is delivering normally never carries the mark
    at all, because `record_delivery` removes it on the next real send and a
    key added after the loss is never marked in the first place.

    A PRESENT-BUT-non-dictionary `report_armed` or `report_last` is corrupt,
    and the repair itself counts as a change even when no key needed adding
    or removing that tick: the isinstance guard alone only protects the READ,
    and a tick that needs no additions or removals would then set
    `changed = False` and never write the fix back, leaving the corrupt value
    in state.json indefinitely. This is distinct from the key being merely
    ABSENT, which is the normal, unremarkable shape of a fresh state.json and
    must not itself be treated as a repair (`dict.get` cannot tell the two
    apart, so the check is `key in state`, not the type check alone).
    `report_last` carries no recoverable keys once it is not a dictionary, so
    replacing it here loses nothing real; it is repaired in this function
    rather than left for the next `record_delivery` call, which may not run
    for a long time if the source it tracks is exactly the one that has
    stopped delivering.
    """
    changed = False
    armed = state.get("report_armed")
    if not isinstance(armed, dict):
        if "report_armed" in state:
            changed = True
        armed = {}
    delivered = state.get("report_last")
    if not isinstance(delivered, dict):
        delivered = {}
        if "report_last" in state:
            state["report_last"] = delivered
            changed = True
    lost = state.get("report_history_lost")
    if not isinstance(lost, list):
        if "report_history_lost" in state:
            changed = True
        lost = []
    wanted = {entry["key"] for entry in entries}
    for key in wanted:
        if key in delivered or key in armed:
            continue
        armed[key] = float(now)
        if history_lost and key not in lost:
            lost.append(key)
        changed = True
    if forget_stale:
        for stale in [key for key in armed if key not in wanted]:
            armed.pop(stale, None)
            changed = True
        for stale in [key for key in lost if key not in wanted]:
            lost.remove(stale)
            changed = True
    if changed:
        state["report_armed"] = armed
        # Absent rather than empty when there is nothing to say, so a healthy
        # state.json does not carry a permanent empty list that reads, to
        # anyone inspecting the file, as though the condition were a standing
        # feature of this install.
        if lost:
            state["report_history_lost"] = lost
        else:
            state.pop("report_history_lost", None)
    return changed


def _age_days(then, now):
    """Age of `then` in days, or None when it cannot be read at all."""
    try:
        return (float(now) - float(then)) / 86400.0
    except (TypeError, ValueError):
        return None


def evaluate(entries, state, now):
    """One result per entry: `{"key": str, "line": str, "problem": str or None}`.

    This is a PURE read. It does not write to `state`, does not save
    anything, and does not arm or record anything -- `arm` and
    `record_delivery` own that.

    EVERY uncertain case resolves to a problem, never to silence. The
    detector this replaces stamped one shared timestamp for every report
    caller, so the loudest caller kept the check green while a quiet one
    could die completely and nothing noticed. This function exists to close
    that hole, which means it must never trade an unreadable signal for a
    reassuring one: no delivery and no arm timestamp is a problem, a
    delivery timestamp that cannot be read is a problem, a delivery or arm
    timestamp in the FUTURE relative to `now` is a problem (a negative age
    is not merely unfortunate, it means a corrupted or out-of-order state
    write, or clock skew, and is reported with its own wording so an
    operator can tell it apart from a plain unreadable value), and any
    other case this function does not explicitly recognise as healthy is a
    problem too.

    `report_armed`/`report_last` are read the same way `arm` reads them: a
    PRESENT-but-non-dictionary container is treated as empty, the same
    convention as an ABSENT one. This function never writes, so it cannot
    repair a corrupt container the way `arm` does -- it can only refuse to
    let the corruption read as health, which the problem it reports for the
    affected entries already does.

    `report_history_lost` (see `arm`) outranks the arm timestamp for the same
    key, because after a rebuild that timestamp records when the loss was
    noticed and nothing about the caller. It does NOT outrank a recorded
    delivery: once a real send lands the key is judged on that, and
    `record_delivery` drops the mark at the same moment.
    """
    delivered = state.get("report_last")
    delivered = delivered if isinstance(delivered, dict) else {}
    armed = state.get("report_armed")
    armed = armed if isinstance(armed, dict) else {}
    lost = state.get("report_history_lost")
    lost = lost if isinstance(lost, list) else []

    results = []
    for entry in entries:
        key, days = entry["key"], entry["days"]

        if key in delivered:
            last_age = _age_days(delivered[key], now)
            if last_age is None:
                results.append({
                    "key": key,
                    "line": f"{key}: UNKNOWN -- the recorded delivery time "
                            f"is unreadable, treat as overdue.",
                    "problem": f"{key}: the recorded delivery time is "
                               f"unreadable, treat as overdue"})
            elif last_age < 0:
                results.append({
                    "key": key,
                    "line": f"{key}: UNKNOWN -- the recorded delivery time "
                            f"is in the future, treat as overdue.",
                    "problem": f"{key}: the recorded delivery time is in "
                               f"the future, treat as overdue"})
            elif last_age > days:
                results.append({
                    "key": key,
                    "line": f"{key}: OVERDUE -- none delivered in "
                            f"{last_age:.0f} day(s) (expected every "
                            f"{days:g} day(s)).",
                    "problem": f"{key}: no report in {last_age:.0f} day(s), "
                               f"expected every {days:g} day(s)"})
            else:
                results.append({
                    "key": key,
                    "line": f"{key}: last delivered {last_age:.1f} day(s) "
                            f"ago (expected every {days:g} day(s)).",
                    "problem": None})
            continue

        if key in lost:
            # Checked BEFORE the arm timestamp, and deliberately so: the arm
            # timestamp for such a key is the moment the rebuilt state file
            # was noticed, so reading it as "configured N days ago, none
            # delivered yet" would hand a month-dead caller a clean green line
            # for a whole cadence. The detector cannot vouch for this key
            # until a real delivery lands, and saying so is the only honest
            # answer available.
            results.append({
                "key": key,
                "line": f"{key}: UNKNOWN -- the delivery history was lost "
                        f"(state.json was rebuilt) and nothing has been "
                        f"delivered since, so absence cannot be judged "
                        f"(expected every {days:g} day(s)).",
                "problem": f"{key}: the delivery history was lost and nothing "
                           f"has been delivered since, so absence cannot be "
                           f"judged (expected every {days:g} day(s))"})
            continue

        if key in armed:
            armed_age = _age_days(armed[key], now)
            if armed_age is None:
                results.append({
                    "key": key,
                    "line": f"{key}: UNKNOWN -- the recorded start time is "
                            f"unreadable, treat as overdue.",
                    "problem": f"{key}: the recorded start time is "
                               f"unreadable, treat as overdue"})
            elif armed_age < 0:
                results.append({
                    "key": key,
                    "line": f"{key}: UNKNOWN -- the recorded start time is "
                            f"in the future, treat as overdue.",
                    "problem": f"{key}: the recorded start time is in the "
                               f"future, treat as overdue"})
            elif armed_age > days:
                results.append({
                    "key": key,
                    "line": f"{key}: OVERDUE -- nothing delivered since it "
                            f"was configured {armed_age:.0f} day(s) ago "
                            f"(expected every {days:g} day(s)).",
                    "problem": f"{key}: nothing delivered since it was "
                               f"configured {armed_age:.0f} day(s) ago "
                               f"(expected every {days:g} day(s))"})
            else:
                results.append({
                    "key": key,
                    "line": f"{key}: expected every {days:g} day(s) -- none "
                            f"delivered yet (configured {armed_age:.1f} "
                            f"day(s) ago).",
                    "problem": None})
            continue

        results.append({
            "key": key,
            "line": f"{key}: UNKNOWN -- no delivery and no start time "
                    f"recorded for it at all.",
            "problem": f"{key}: expected, but nothing has been recorded "
                       f"for it at all"})
    return results
