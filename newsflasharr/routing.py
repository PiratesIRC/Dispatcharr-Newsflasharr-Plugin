"""Newsflasharr routing-rule engine: which channels a caller event fans out to.

Pure functions, stdlib only, no imports of other notifier modules. deliver.py
and the Validate-settings action depend on these exact signatures. Spec §4.4:
matched rules' channels union with the configured defaults (adding a rule
never silently un-routes the defaults) unless a matching rule opts out via
"exclusive".
"""
from __future__ import annotations

MATCH_KEYS = ("source", "event", "severity")
SEVERITIES = ("critical", "warning", "info")

# A rule's own top-level keys. `route()` reads only `match`, `channels` and
# `exclusive`; anything else is inert, and an inert `match` is the dangerous
# case (see lint()).
RULE_KEYS = frozenset({"match", "channels", "exclusive"})


def _nearest_rule_key(key):
    """Best-guess correction for an unknown rule key, for the lint message.
    Deliberately trivial -- a prefix/containment match covers the realistic
    typos (`matches`, `exclusiv`, `channel`) without pulling in a dependency."""
    k = str(key).strip().lower()
    for candidate in sorted(RULE_KEYS):
        if k.startswith(candidate[:4]) or candidate.startswith(k[:4]):
            return candidate
    return "match"


def _clean(value):
    return value.strip().lower() if isinstance(value, str) else value


def normalize(settings):
    """Lowercase/strip channel names and match severities, IN PLACE.

    A phone keyboard autocapitalized `ntfy` to `Ntfy` in `default_channels` on
    2026-07-20. Matching is case-sensitive, so deliver's enabled-filter dropped
    it and warnings/info routed to NOTHING while validate still reported ok.

    Normalize what is unambiguous, REPORT what is not: `Ntfy` can only mean
    ntfy, but `ntfyy` is a typo `lint` must still surface rather than have this
    silently swallow. `source` and `event` are deliberately untouched -- they
    are caller-defined identifiers, not a fixed vocabulary.

    Both settings paths call this (plugin._coerce_settings and
    collector.read_settings). The collector one is load-bearing: it is the path
    the actual delivery loop reads, so normalizing only in the plugin would fix
    the report and leave the outage.

    Never raises -- malformed shapes are lint's job to describe.
    """
    if not isinstance(settings, dict):
        return settings

    chans = settings.get("default_channels")
    if isinstance(chans, list):
        settings["default_channels"] = [_clean(c) for c in chans]

    rules = settings.get("routing_rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            chans = rule.get("channels")
            if isinstance(chans, list):
                rule["channels"] = [_clean(c) for c in chans]
            match = rule.get("match")
            if isinstance(match, dict) and "severity" in match:
                match["severity"] = _clean(match["severity"])
    return settings


def _rule_matches(event, match):
    return all(event.get(key) == value for key, value in match.items())


def route(event, rules, default_channels):
    """Channels this event fans out to: order-stable, deduped.

    Defaults come first, then matched-rule channels in rule order, unless
    at least one matching rule is "exclusive" -- then defaults are dropped
    and only matched rules' channels are used. No matching rule -> defaults.
    """
    matched = [rule for rule in rules if _rule_matches(event, rule.get("match", {}))]
    if not matched:
        return list(dict.fromkeys(default_channels))

    exclusive = any(rule.get("exclusive") for rule in matched)
    channels = [] if exclusive else list(default_channels)
    for rule in matched:
        channels.extend(rule.get("channels", []))
    return list(dict.fromkeys(channels))


def lint(rules, known_channels, default_channels=None):
    """Human-readable problems in `rules`, for the Validate-settings action.

    `default_channels` is optional and keyword-safe: older call sites pass two
    positional args. It is checked here because NOTHING checked it before -- an
    unknown name there routes to nothing at all and reported clean.

    Returns [] when clean.
    """
    problems = []
    known_set = set(known_channels)
    for ch in (default_channels or []):
        if ch not in known_set:
            problems.append(f"default channels: unknown channel {ch!r}")
    if not isinstance(rules, list):
        return ["rules must be a list"]

    known = set(known_channels)
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            problems.append(f"rule {i}: not an object")
            continue

        # The rule's OWN keys, checked BEFORE `match`. Nothing looked at these,
        # and the failure is the worst shape available: `route()` reads
        # `rule.get("match", {})`, and `_rule_matches(event, {})` is `all([])`
        # -> True, so a one-letter typo (`matches:`) turns a targeted rule into
        # a MATCH-EVERYTHING rule -- proven: it routed an `info` event to smtp.
        # A misspelt `exclusive` silently makes an exclusive rule additive.
        for key in rule:
            if key not in RULE_KEYS:
                problems.append(
                    f"rule {i}: unknown key {key!r} -- did you mean "
                    f"{_nearest_rule_key(key)!r}? A rule with no {'match'!r} "
                    f"key matches EVERY event.")

        match = rule.get("match", {})
        if not isinstance(match, dict):
            problems.append(f"rule {i}: match must be an object")
        else:
            for key in match:
                if key not in MATCH_KEYS:
                    problems.append(f"rule {i}: unknown match key {key!r}")
            # A severity outside the fixed vocabulary never matches any event,
            # so the rule is silently inert -- the same failure as an unknown
            # channel name, which has always been reported.
            sev = match.get("severity")
            if sev is not None and sev not in SEVERITIES:
                problems.append(f"rule {i}: unknown severity {sev!r}")
            # `_rule_matches` compares with `==` on the raw value, so a stray
            # space means the rule can NEVER fire and is silently inert -- and
            # an inert `critical -> smtp` rule means criticals quietly fall
            # back to the defaults. `normalize` deliberately leaves source and
            # event alone (caller-defined identifiers, not a fixed vocabulary),
            # so this is reported rather than corrected. Case is NOT flagged:
            # an uppercase source may be legitimate, whitespace never is.
            for key in ("source", "event"):
                value = match.get(key)
                if value is None:
                    continue
                if not isinstance(value, str):
                    problems.append(f"rule {i}: {key} must be a string")
                    continue
                if value != value.strip():
                    problems.append(
                        f"rule {i}: {key} {value!r} has leading/trailing "
                        f"whitespace and can never match")
                elif not value:
                    problems.append(
                        f"rule {i}: {key} is empty and can never match")

        channels = rule.get("channels") or []
        if not channels:
            problems.append(f"rule {i}: channels is empty")
        elif not isinstance(channels, list):
            problems.append(f"rule {i}: channels must be a list")
        else:
            for ch in channels:
                if ch not in known:
                    problems.append(f"rule {i}: unknown channel {ch!r}")

    return problems


def summarize(rules, default_channels, enabled=None, known_channels=None):
    """Resolve routing_rules through the REAL route(), for a human readout.

    Answers "where does a <source> <severity> actually go?" without
    re-implementing route()'s exclusive/union/dedupe logic (which would be a
    divergence source, the matching_core lesson). Every row's channel set comes
    straight from route(), so the readout cannot disagree with delivery.

    Resolves PER CONFIGURED SOURCE, not a source-less severity line: notify()
    makes `source` mandatory, so a generic "critical -> ntfy" line is false the
    moment a `source=dustarr` rule sends dustarr criticals elsewhere.

    Never raises: non-dict rule entries survive _coerce_settings and route()
    would AttributeError on them, so they are filtered before delegating. lint
    (which describes the malformed shapes) is run on the ORIGINAL rules so its
    `rule N` indices match what the operator edits; it is skipped entirely when
    `known_channels is None` (a pure resolve with no configured vocabulary to
    check against).

    Returns {"defaults": [...], "rows": [...], "broken": [...]}.
    """
    safe_rules = [r for r in rules
                  if isinstance(r, dict) and isinstance(r.get("match", {}), dict)] \
        if isinstance(rules, list) else []
    defaults = list(default_channels) if isinstance(default_channels, list) else []
    enabled_set = set(enabled) if enabled is not None else None

    broken = []
    if known_channels is not None:
        broken = lint(rules, known_channels, default_channels=defaults)

    # Selectors = the distinct non-severity match constraints across rules,
    # plus the empty selector (an event matching no source/event rule).
    seen = set()
    selectors = [()]
    for rule in safe_rules:
        match = rule.get("match")
        if not isinstance(match, dict):
            continue
        sel = tuple(sorted((k, v) for k, v in match.items() if k != "severity"))
        if not sel:
            continue
        # `sel` may hold a list/dict value (e.g. {"source": ["a", "b"]}) --
        # legal JSON from the routing_rules textarea. Dedup on a hashable
        # repr() projection while keeping the real `sel` (with its live
        # values) for the row/display.
        dedup_key = tuple((k, repr(v)) for k, v in sel)
        if dedup_key not in seen:
            seen.add(dedup_key)
            selectors.append(sel)

    # Sort all selectors after the empty one for stable readout.
    # Use a type-tolerant key to avoid TypeError when selector values have
    # mixed types (e.g., {"source": 5} and {"source": "aaa"} -- both legal from
    # the operator's JSON textarea). Convert to string form for sorting.
    selectors[1:] = sorted(selectors[1:],
                           key=lambda sel: [(str(k), str(v)) for k, v in sel])

    rows = []
    for sel in selectors:
        base = dict(sel)
        resolved = {}
        for sev in SEVERITIES:
            event = {**base, "severity": sev, "kind": "event"}
            resolved[sev] = route(event, safe_rules, defaults)

        def _row(severities, channels, sel=sel):
            reaches_nothing = (
                enabled_set is not None
                and not [c for c in channels if c in enabled_set])
            return {"selector": dict(sel), "severities": severities,
                    "channels": channels, "reaches_nothing": reaches_nothing}

        first = resolved[SEVERITIES[0]]
        if all(resolved[s] == first for s in SEVERITIES):
            rows.append(_row("any", first))
        else:
            for sev in SEVERITIES:
                rows.append(_row(sev, resolved[sev]))

    return {"defaults": defaults, "rows": rows, "broken": broken}


# Critical-escalation preset (#20 Phase 2). ONE table drives the select
# options, the composed channels, and the readout label -- so they cannot
# fall out of step (a value the select offers that compose does not know is
# the Ntfy silent-no-op shape). value -> (readout phrase, channels).
_ESCALATION = {
    "smtp":         ("email",                    ["smtp"]),
    "smtp_ticker":  ("email + on-screen ticker", ["smtp", "ticker"]),
    "discord":      ("Discord",                  ["discord"]),
    "smtp_discord": ("email + Discord",          ["smtp", "discord"]),
}


def _active_escalation(settings):
    """The (phrase, channels) for the chosen escalation, or None. The SINGLE
    gate shared by compose() and describe_presets() so they cannot disagree
    about whether a preset is active (spec honesty-3). Normalizes case and
    whitespace -- the Ntfy outage class."""
    raw = str(settings.get("critical_extra_channels") or "none").strip().lower()
    return _ESCALATION.get(raw)


def compose(settings):
    """Synthesized routing rules from the preset selects. 0-or-1 rule today
    (critical escalation). Pure, never raises. The result is PREPENDED to the
    operator's routing_rules for delivery/readout but never written back."""
    entry = _active_escalation(settings)
    if not entry:
        return []
    _phrase, chans = entry
    return [{"match": {"severity": "critical"}, "channels": list(chans)}]


def describe_presets(settings):
    """Human 'Presets in effect' lines, derived from the SAME gate compose
    uses -- so a preset shown as active is exactly one that composed a rule."""
    entry = _active_escalation(settings)
    if not entry:
        return []
    return [f"Critical alerts also go to: {entry[0]}"]


TICKER_CHANNEL = "ticker"


def ticker_reachability(sources, rules, default_channels, severity):
    """Which ticker sources can never draw, and which rules draw for nobody.

    -> (unreachable, filtered_out), both sorted lists of source names.

    The on-screen banner needs BOTH halves to agree: a routing rule has to
    send the event to the ticker channel, AND the source has to appear in the
    ticker source filter. Configuring one without the other is silent, and the
    counter the operator is told to check (`ticker_skipped` with reason
    `source_filtered`) cannot help, because it only increments once routing
    has already selected the ticker.

    Reachability comes from `route()` rather than from scanning rules for the
    channel name, so a ticker reached through `default_channels`, or through
    the critical-escalation preset that is prepended to the effective rules,
    counts as reachable. A second implementation of a routing rule is a
    divergence source, which is the lesson the shared matcher core exists to
    enforce.

    `sources` of None or empty means no filter, which means every source may
    draw, so there is nothing to report in either direction.

    This answers "can this source ever draw", not "does every event from
    this source draw" -- those are different questions, and the second one
    is not answerable without knowing every event the caller will ever emit.
    A source counts as reachable the moment ANY probe (any spelling, any
    named event) reaches the ticker channel; it says nothing about sources
    or events the probe never tried.

    Two mismatches this function is deliberately built to survive:

    Case. `ticker.py` builds the source filter lowercased and lowercases the
    event source before comparing, so the DRAW-TIME filter is
    case-insensitive. `route()` -> `_rule_matches()` compares with `==` on
    the raw value, so ROUTING is case-sensitive. The rule is the authority on
    spelling, because that is what delivery actually matches against: a
    filter entry is probed using its own spelling AND the spelling of every
    rule whose source matches it case-insensitively, so a filter entry
    "Sentinelarr" and a rule naming "Sentinelarr" agree instead of being
    reported as both unreachable and filtered out.

    Event-scoped rules. A rule may narrow on `event` as well as `source`
    (`{"source": "sentinelarr", "event": "black_screen"}`), and the probe
    event here otherwise carries no `event` key, so such a rule would never
    match a bare probe. Each source is therefore probed once with no event
    key and once per distinct event name any matching rule mentions -- both
    a rule naming this exact source AND a source-less rule
    (`{"match": {"event": "black_screen"}, ...}`), which applies to EVERY
    source and would otherwise contribute no event name at all.
    """
    if not sources:
        return [], []
    safe_rules = [r for r in rules
                  if isinstance(r, dict) and isinstance(r.get("match"), dict)] \
        if isinstance(rules, list) else []
    defaults = list(default_channels) if isinstance(default_channels, list) else []
    # Defensive, not just documentary: `ticker.py` already lowercases the
    # filter it builds, but normalizing again here means this function is
    # correct on its own terms rather than trusting a caller invariant.
    sources_lower = {s.strip().lower() for s in sources
                     if isinstance(s, str) and s.strip()}

    def _spellings_and_events(source_lower):
        """Every spelling/event combination worth probing for a filter entry
        or a rule-named source that lowercases to `source_lower`."""
        spellings = {source_lower}
        events = {None}
        for rule in safe_rules:
            match = rule.get("match", {})
            named = match.get("source")
            if isinstance(named, str) and named.strip().lower() == source_lower:
                spellings.add(named)
                ev = match.get("event")
                if isinstance(ev, str):
                    events.add(ev)
            elif "source" not in match:
                # A rule with no source key applies to EVERY source, so its
                # event name is relevant here too -- otherwise a source-less
                # {"event": "black_screen"} rule contributes nothing and a
                # source that only draws for that event is wrongly reported
                # as unable to draw at all.
                ev = match.get("event")
                if isinstance(ev, str):
                    events.add(ev)
        return spellings, events

    def _reaches_ticker(source_lower):
        spellings, events = _spellings_and_events(source_lower)
        for spelling in spellings:
            for ev in events:
                event = {"source": spelling, "severity": severity, "kind": "event"}
                if ev is not None:
                    event["event"] = ev
                if TICKER_CHANNEL in route(event, safe_rules, defaults):
                    return True
        return False

    unreachable = [source for source in sources_lower if not _reaches_ticker(source)]

    # Grouped by lowercased name FIRST, reachability checked once per group --
    # rules naming "Dustarr" and "DUSTARR" are one logical source to an
    # operator and must produce one problem paragraph, not two near-identical
    # ones.
    filtered_spellings = {}
    for rule in safe_rules:
        named = rule.get("match", {}).get("source")
        if not isinstance(named, str):
            continue
        named_lower = named.strip().lower()
        if named_lower in sources_lower:
            continue
        filtered_spellings.setdefault(named_lower, set()).add(named)

    filtered_out = [
        # The alphabetically first spelling: stable and arbitrary-but-
        # consistent, and always one an operator actually typed.
        min(spellings) for named_lower, spellings in filtered_spellings.items()
        if _reaches_ticker(named_lower)]

    return sorted(set(unreachable)), sorted(set(filtered_out))
