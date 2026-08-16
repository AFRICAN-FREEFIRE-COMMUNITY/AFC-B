"""Validation for an admin-authored scoring config, and contradiction detection.

TWO DIFFERENT THINGS, ON PURPOSE
    ERRORS are refusals. A config that would corrupt scoring is rejected outright, because
    a bad config silently mis-scores every team on the site and nobody notices until the
    ladder is wrong. Examples: a compression table with no open top band (a team above the
    last threshold would score nothing at all), a negative multiplier (better results would
    lower a score), a tier cutoff no team could ever reach.

    CONTRADICTIONS are reports. They describe a config that WORKS but does not do what the
    author probably meant: a second rule that reads "above 100" when an earlier rule already
    reads "above 100" is legal, it just can never fire. These are surfaced with the offending
    entries named, and the save is allowed - the admin is told, not blocked.

PURE MODULE
    Django-free like the rest of ``scoring/`` (see the package docstring). It takes the raw
    JSON blob and plain rule dicts, and returns plain dicts. The Django layer
    (``admin_scoring_config.py`` / ``admin_tournament_tiers.py``) converts ORM rows into the
    rule dicts and turns the result into an HTTP response.

HOW IT CONNECTS
    * ``admin_scoring_config.scoring_config_save``     - rejects on ``errors``, reports
                                                          ``contradictions`` in the response.
    * ``admin_scoring_config.scoring_config_validate`` - the same check with nothing written.
    * ``admin_tournament_tiers``                       - runs ``rule_contradictions`` over the
                                                          EventTierRule list on every write and
                                                          on the list endpoint.
"""

from __future__ import annotations

from .constants import TIER_MODE_DEFAULT, TIER_MODE_THRESHOLD, TIER_MODE_TOP_N, TIER_MODES
from .tables import (
    ALLOWED_TOP_LEVEL_KEYS,
    FIELD_META,
    max_achievable_score,
    tables_from_config,
)

# Bracket groups share one shape and one set of rules, so they are checked in a loop.
_BRACKET_GROUPS = (
    "kill_compression",
    "placement_compression",
    "prize_money_points",
    "social_media_points",
)


def _err(code, path, message):
    return {"code": code, "path": path, "message": message}


def _contra(kind, path, message, entries=None):
    return {"kind": kind, "path": path, "message": message, "entries": entries or []}


def _as_number(value):
    """Return the value as a float, or None when it is not a number.

    Booleans are refused deliberately: JSON ``true`` arriving where a multiplier belongs is
    a mistake, and Python would otherwise happily read it as 1.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_number(blob, path, value, errors, *, minimum=0.0, allow_equal_min=True,
                  maximum=None, label=None):
    """Shared numeric guard. Appends an error and returns None when the value is unusable."""
    label = label or path
    number = _as_number(value)
    if number is None:
        errors.append(_err("not_a_number", path, f"{label} must be a number."))
        return None
    if minimum is not None:
        if number < minimum or (not allow_equal_min and number == minimum):
            bound = "greater than" if not allow_equal_min else "at least"
            errors.append(_err(
                "out_of_range", path,
                f"{label} must be {bound} {minimum:g}. Got {number:g}.",
            ))
            return None
    if maximum is not None and number > maximum:
        errors.append(_err(
            "out_of_range", path,
            f"{label} must not be above {maximum:g}. Got {number:g}.",
        ))
        return None
    return number


# ---------------------------------------------------------------------------
# Bracket tables
# ---------------------------------------------------------------------------
def _validate_brackets(group, rows, errors, contradictions):
    """A bracket table is an ordered list of {"max": n|null, "points": n} rows.

    The engine walks the list top down and returns the points of the first row whose upper
    bound is open (null) or whose bound the value does not exceed. Two things follow, and
    both are checked here:

      * the LAST row must be open (``max: null``) or a value above every bound falls off the
        end of the table and the lookup raises. That is a rejection: it would break scoring.
      * bounds must climb. A bound that is not above the one before it can never be selected,
        because the earlier row already caught everything up to it. That is legal but
        pointless, so it is reported as an unreachable band rather than refused.
    """
    if not isinstance(rows, list) or not rows:
        errors.append(_err(
            "empty_table", group,
            f"{FIELD_META[group]['label']} needs at least one band.",
        ))
        return

    for index, row in enumerate(rows):
        path = f"{group}[{index}]"
        if not isinstance(row, dict):
            errors.append(_err("bad_row", path, "Each band must be an object with a max and points."))
            return
        if "max" not in row or "points" not in row:
            errors.append(_err("bad_row", path, "Each band needs both a max and a points value."))
            return
        _check_number(rows, f"{path}.points", row.get("points"), errors,
                      minimum=0.0, label="Band points")
        upper = row.get("max")
        if upper is None:
            # Only the final row may be open ended: an open band swallows everything after
            # it, so anything below it in the list is dead.
            if index != len(rows) - 1:
                errors.append(_err(
                    "open_band_not_last", path,
                    "Only the last band may be open ended. An open band matches every value, "
                    "so the bands below it could never be reached.",
                ))
            continue
        if _as_number(upper) is None:
            errors.append(_err("not_a_number", f"{path}.max",
                               "A band's max must be a number, or empty for the top band."))
            return
        if float(upper) < 0:
            errors.append(_err("out_of_range", f"{path}.max", "A band's max cannot be negative."))

    if rows[-1].get("max") is not None:
        errors.append(_err(
            "no_open_top_band", group,
            f"{FIELD_META[group]['label']} has no open top band. The last band must be left "
            f"open (no upper limit), otherwise anything above "
            f"{rows[-1].get('max')} scores nothing at all.",
        ))

    # Ordering: reported, not refused (see the docstring).
    previous = None
    for index, row in enumerate(rows[:-1] if rows[-1].get("max") is None else rows):
        upper = _as_number(row.get("max"))
        if upper is None:
            continue
        if previous is not None and upper <= previous:
            contradictions.append(_contra(
                "unreachable_band", f"{group}[{index}]",
                f"Band up to {upper:g} can never be selected: the band before it already "
                f"covers everything up to {previous:g}.",
                entries=[{"index": index, "max": upper, "previous_max": previous}],
            ))
        previous = upper if previous is None else max(previous, upper)


# ---------------------------------------------------------------------------
# The whole config
# ---------------------------------------------------------------------------
def validate_config(blob):
    """Check an admin-authored scoring config.

    Returns ``{"errors": [...], "contradictions": [...]}``. ``errors`` non-empty means the
    save must be refused; ``contradictions`` is advisory and never blocks.

    Error shape:         {"code", "path", "message"}
    Contradiction shape: {"kind", "path", "message", "entries": [...]}
    """
    errors, contradictions = [], []

    if not isinstance(blob, dict) or not blob:
        return {
            "errors": [_err("not_an_object", "", "The config must be a non-empty object.")],
            "contradictions": [],
        }

    # An unrecognised key is refused rather than ignored: a typo that silently does nothing
    # is the worst outcome here, because the admin believes the change landed.
    unknown = sorted(set(blob) - ALLOWED_TOP_LEVEL_KEYS)
    for key in unknown:
        errors.append(_err(
            "unknown_key", key,
            f"'{key}' is not a scoring setting. Check the spelling: an unrecognised setting "
            f"would be saved and then ignored.",
        ))

    # ── tiers ──
    tiers = blob.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, list) or not tiers:
            errors.append(_err("empty_table", "tiers", "At least one tournament tier is required."))
            tiers = []
        seen_keys = set()
        live_count = 0
        for index, row in enumerate(tiers):
            path = f"tiers[{index}]"
            if not isinstance(row, dict):
                errors.append(_err("bad_row", path, "Each tier must be an object."))
                continue
            key = row.get("key")
            if not key or not isinstance(key, str):
                errors.append(_err("missing_key", path, "Each tier needs a key, for example tier_1."))
                continue
            if key in seen_keys:
                errors.append(_err(
                    "duplicate_tier", path,
                    f"Tier '{key}' is listed twice. Every tier key must be unique, because "
                    f"events are stored against it.",
                ))
            seen_keys.add(key)
            # A zero or negative multiplier is refused: zero silently voids every result at
            # that tier, negative makes a better result score worse.
            _check_number(row, f"{path}.multiplier", row.get("multiplier"), errors,
                          minimum=0.0, allow_equal_min=False, label=f"Tier '{key}' multiplier")
            _check_number(row, f"{path}.win_bonus", row.get("win_bonus"), errors,
                          minimum=0.0, label=f"Tier '{key}' win bonus")
            if not row.get("retired"):
                live_count += 1
        if tiers and live_count == 0:
            errors.append(_err(
                "all_tiers_retired", "tiers",
                "Every tier is retired. At least one tier must stay active or no new event "
                "can be classified.",
            ))

    # ── placement points ──
    placement = blob.get("placement_points")
    if placement is not None:
        if not isinstance(placement, dict) or not placement:
            errors.append(_err("empty_table", "placement_points",
                               "Placement points needs at least one finishing position."))
        else:
            for key, value in placement.items():
                path = f"placement_points.{key}"
                try:
                    finish = int(key)
                except (TypeError, ValueError):
                    errors.append(_err("bad_key", path,
                                       "Finishing positions must be whole numbers."))
                    continue
                if finish < 1:
                    errors.append(_err("out_of_range", path,
                                       "A finishing position must be 1 or higher."))
                _check_number(placement, path, value, errors, minimum=0.0,
                              label=f"Placement points for position {finish}")

    # ── bracket tables ──
    for group in _BRACKET_GROUPS:
        if group in blob:
            _validate_brackets(group, blob.get(group), errors, contradictions)

    if "finals_base" in blob:
        _check_number(blob, "finals_base", blob.get("finals_base"), errors,
                      minimum=0.0, label="Finals appearance base")

    # ── scrim rules ──
    scrim = blob.get("scrim")
    if scrim is not None:
        if not isinstance(scrim, dict):
            errors.append(_err("bad_row", "scrim", "Scrim rules must be an object."))
        else:
            _check_number(scrim, "scrim.weight", scrim.get("weight"), errors,
                          minimum=0.0, label="Scrim weight")
            _check_number(scrim, "scrim.win_flat", scrim.get("win_flat"), errors,
                          minimum=0.0, label="Scrim win points")
            # A ratio above 1 would let scrims be worth more than the tournaments they are
            # meant to supplement; below 0 is meaningless.
            _check_number(scrim, "scrim.cap_ratio", scrim.get("cap_ratio"), errors,
                          minimum=0.0, maximum=1.0,
                          label="Scrim cap ratio (a share of tournament points, 0 to 1)")
            _check_number(scrim, "scrim.flat_cap", scrim.get("flat_cap"), errors,
                          minimum=0.0, label="Flat scrim allowance")
            for name, label in (("daily_cap", "Daily scrim cap"),
                                ("monthly_cap", "Monthly scrim cap")):
                if name in scrim:
                    number = _check_number(scrim, f"scrim.{name}", scrim.get(name), errors,
                                           minimum=0.0, label=label)
                    if number is not None and number != int(number):
                        errors.append(_err("not_an_integer", f"scrim.{name}",
                                           f"{label} must be a whole number of scrims."))
    if "scrim_flat_cap" in blob:  # pre-v2 spelling, still honoured
        _check_number(blob, "scrim_flat_cap", blob.get("scrim_flat_cap"), errors,
                      minimum=0.0, label="Flat scrim allowance")

    # ── player weights ──
    weights = blob.get("player_weights")
    if weights is not None:
        if not isinstance(weights, dict):
            errors.append(_err("bad_row", "player_weights", "Player weights must be an object."))
        else:
            for name, value in weights.items():
                _check_number(weights, f"player_weights.{name}", value, errors,
                              minimum=0.0, label=f"Player {name.replace('_', ' ')}")

    # ── participation floors ──
    floors = blob.get("participation_floors")
    if floors is not None:
        if not isinstance(floors, dict):
            errors.append(_err("bad_row", "participation_floors",
                               "Participation floors must be an object."))
        else:
            for name, value in floors.items():
                path = f"participation_floors.{name}"
                number = _check_number(floors, path, value, errors, minimum=0.0,
                                       label=f"{name.replace('_', ' ').title()} floor")
                if number is not None and number != int(number):
                    errors.append(_err("not_an_integer", path,
                                       "A participation floor must be a whole number of tournaments."))

    # ── ranking tiers (cutoffs in threshold mode, sizes in top-N mode) ──
    # ``tier_mode`` decides which column of the SAME tier rows is in force, so it decides
    # which column is checked. The dormant column is still checked for SHAPE (it must remain
    # a number, because switching the mode back must not land on a broken table) but not for
    # meaning: a cutoff no team can reach is irrelevant while sizes are what count.
    tier_mode = TIER_MODE_DEFAULT
    thresholds_blob = blob.get("tier_thresholds")
    if thresholds_blob is not None:
        if not isinstance(thresholds_blob, dict):
            errors.append(_err("bad_row", "tier_thresholds",
                               "Tier cutoffs must be an object with brackets and a default tier."))
        else:
            raw_mode = thresholds_blob.get("mode", TIER_MODE_DEFAULT)
            if raw_mode not in TIER_MODES:
                errors.append(_err(
                    "unknown_tier_mode", "tier_thresholds.mode",
                    f"'{raw_mode}' is not a way of deciding tiers. Choose "
                    f"'{TIER_MODE_THRESHOLD}' (a team reaches a tier by scoring enough) or "
                    f"'{TIER_MODE_TOP_N}' (each tier holds a fixed number of teams).",
                ))
            else:
                tier_mode = raw_mode
            rows = thresholds_blob.get("brackets")
            if not isinstance(rows, list) or not rows:
                errors.append(_err("empty_table", "tier_thresholds.brackets",
                                   "At least one tier cutoff is required."))
                rows = []
            labels = thresholds_blob.get("labels") if isinstance(
                thresholds_blob.get("labels"), dict) else {}
            known_tiers = {int(k) for k in labels if str(k).lstrip("-").isdigit()}
            default_tier = thresholds_blob.get("default_tier")
            if default_tier is None or _as_number(default_tier) is None:
                errors.append(_err("not_a_number", "tier_thresholds.default_tier",
                                   "A default tier is required for anyone below every cutoff."))
            elif known_tiers and int(default_tier) not in known_tiers:
                errors.append(_err(
                    "unknown_tier", "tier_thresholds.default_tier",
                    f"The default tier {int(default_tier)} has no label. Add it to the tier "
                    f"labels or pick one of {sorted(known_tiers)}.",
                ))

            previous_min = None
            seen_tier_ints = set()
            for index, row in enumerate(rows):
                path = f"tier_thresholds.brackets[{index}]"
                if not isinstance(row, dict):
                    errors.append(_err("bad_row", path, "Each cutoff must be an object."))
                    continue
                # ``min`` is always required, in both modes: it is the row's threshold column
                # and it has to survive intact so that switching back to score mode lands on a
                # working table rather than an empty one.
                min_score = _check_number(row, f"{path}.min", row.get("min"), errors,
                                          minimum=0.0, label="Tier cutoff score")
                tier_int = row.get("tier")
                if _as_number(tier_int) is None:
                    errors.append(_err("not_a_number", f"{path}.tier",
                                       "Each cutoff must name the tier it awards."))
                elif known_tiers and int(tier_int) not in known_tiers:
                    errors.append(_err(
                        "unknown_tier", f"{path}.tier",
                        f"Tier {int(tier_int)} has no label. Add it to the tier labels or pick "
                        f"one of {sorted(known_tiers)}.",
                    ))

                # ── the top-N size column ──
                _validate_tier_count(row, path, tier_mode, errors, contradictions)
                if tier_mode == TIER_MODE_TOP_N and _as_number(tier_int) is not None:
                    # In top-N mode a tier's size is looked up BY TIER, so listing the same
                    # tier twice makes the second size unreadable. Legal, but not what anyone
                    # means, so it is reported the same way a shadowed rule is.
                    if int(tier_int) in seen_tier_ints:
                        contradictions.append(_contra(
                            "duplicate_tier_size", path,
                            f"Tier {int(tier_int)} is listed more than once. Only the first row's "
                            f"size is used, so this row's size is ignored.",
                            entries=[{"index": index, "tier": int(tier_int)}],
                        ))
                    seen_tier_ints.add(int(tier_int))

                # Cutoffs are read top down and the first one the score clears wins, so they
                # must descend. One that does not can never fire - reported, not refused,
                # because the scale still works for every other cutoff. Silent in top-N mode:
                # the cutoffs are dormant there, and warning about a number that is not in
                # force is noise that hides the warnings that are.
                if min_score is not None and tier_mode == TIER_MODE_THRESHOLD:
                    if previous_min is not None and min_score >= previous_min:
                        contradictions.append(_contra(
                            "unreachable_tier_cutoff", path,
                            f"A cutoff at {min_score:g} can never be reached: the cutoff above it "
                            f"({previous_min:g}) already catches every score that high. Cutoffs are "
                            f"checked from the top down, so they must go from highest to lowest.",
                            entries=[{"index": index, "min": min_score,
                                      "previous_min": previous_min}],
                        ))
                    previous_min = min_score if previous_min is None else min(previous_min, min_score)

    # ── reachability of the scale as a whole ──
    # Only meaningful once the individual numbers are sound; running it on a broken blob
    # would produce a confusing second error about a value already reported.
    #
    # THRESHOLD MODE ONLY. The question it answers is "can any team clear the lowest cutoff",
    # and in top-N mode no team ever has to: the tiers are filled by position, so a ladder of
    # low scores still produces a full Tier 1. Running this check there would refuse a config
    # that works perfectly.
    if not errors and tier_mode == TIER_MODE_THRESHOLD:
        tables = tables_from_config(blob)
        if tables.tier_thresholds:
            lowest_cutoff = min(m for m, _ in tables.tier_thresholds)
            ceiling = max_achievable_score(tables)
            if lowest_cutoff > ceiling:
                errors.append(_err(
                    "unreachable_scale", "tier_thresholds",
                    f"The lowest tier cutoff is {lowest_cutoff:g}, but the highest score these "
                    f"settings can produce from a perfect tournament is about {ceiling:g}. No "
                    f"team could ever leave the default tier.",
                ))

    return {"errors": errors, "contradictions": contradictions}


def _validate_tier_count(row, path, tier_mode, errors, contradictions):
    """Check one tier row's ``count`` - the number of teams that tier holds in top-N mode.

    In threshold mode the column is dormant, so only its SHAPE is checked (a stray string
    there would be lost on the next save). In top-N mode it is the number that decides every
    team's tier, so:

      * missing or blank is REFUSED. Left unset the tier would silently hold nobody, and the
        admin who just switched modes would see Tier 1 empty with no explanation.
      * a fraction or a negative is REFUSED - a tier holds a whole number of teams.
      * an explicit 0 is ALLOWED but REPORTED. It works, and deliberately emptying a tier for
        a season is a real thing to want; it is just worth saying out loud.
    """
    count_path = f"{path}.count"
    raw = row.get("count")

    if tier_mode != TIER_MODE_TOP_N:
        if raw is not None and _as_number(raw) is None:
            errors.append(_err("not_a_number", count_path,
                               "A tier's size must be a number of teams, or left empty."))
        return

    if raw is None:
        errors.append(_err(
            "missing_tier_count", count_path,
            "This tier has no size. In top-N mode every tier holds a fixed number of teams, "
            "so each one needs a count - for example 10 for 'the top 10 teams are Tier 1'.",
        ))
        return

    count = _check_number(row, count_path, raw, errors, minimum=0.0, label="Tier size")
    if count is None:
        return
    if count != int(count):
        errors.append(_err("not_an_integer", count_path,
                           "A tier holds a whole number of teams."))
        return
    if count == 0:
        contradictions.append(_contra(
            "empty_tier", count_path,
            "This tier is set to hold 0 teams, so no team will ever be placed in it.",
            entries=[{"tier": row.get("tier"), "count": 0}],
        ))


# ---------------------------------------------------------------------------
# Tournament tier rules - the owner's "two rules both above 100" case
# ---------------------------------------------------------------------------
# A rule dict is the plain-data form of an EventTierRule row:
#   {"id", "name", "priority", "match": "all"|"any",
#    "conditions": [{field, op, value, currency?}], "tier": int, "enabled": bool, "retired": bool}
# The Django layer builds these from the ORM; this module never touches the database.
#
# CURRENCY. A `prize` condition may name the currency its threshold is written in; no key means
# naira (scoring/currency.condition_currency). Every check below runs on thresholds ALREADY
# converted to naira by _normalized_rules, because otherwise "prize >= 1000 USD" and
# "prize >= 100000 NGN" would be compared as the bare numbers 1000 and 100000 and this module would
# report the shadowing backwards - claiming the naira rule hides the dollar rule when the opposite
# is true. Converting once at the top means every existing check works unchanged.

_NUMERIC_OPS = ("gte", "lte")


def _condition_implies(term, target):
    """True when satisfying every condition in ``term`` guarantees ``target`` is satisfied.

    ``term`` is a conjunction (a list of conditions that all hold). For a threshold, a
    stricter requirement implies a looser one: prize >= 500 guarantees prize >= 100.
    """
    field = target.get("field")
    op = target.get("op")
    target_value = _as_number(target.get("value"))
    for cond in term:
        if cond.get("field") != field:
            continue
        if op not in _NUMERIC_OPS:
            # format-style boolean ops: only an identical test implies the target.
            if cond.get("op") == op:
                return True
            continue
        if cond.get("op") != op:
            continue
        value = _as_number(cond.get("value"))
        if value is None or target_value is None:
            continue
        if op == "gte" and value >= target_value:
            return True
        if op == "lte" and value <= target_value:
            return True
    return False


def _terms(rule):
    """The rule's satisfying set as a list of conjunctions (a disjunctive normal form).

    match "all" is one conjunction of every condition; match "any" is one conjunction per
    condition. Reducing both to the same structure lets one implication check cover both.
    """
    conditions = [c for c in (rule.get("conditions") or []) if isinstance(c, dict)]
    if not conditions:
        return []
    return [conditions] if rule.get("match", "all") == "all" else [[c] for c in conditions]


def _rule_matches_term(rule, term):
    """True when every sample satisfying ``term`` also satisfies ``rule``."""
    conditions = [c for c in (rule.get("conditions") or []) if isinstance(c, dict)]
    if not conditions:
        return False
    checks = [_condition_implies(term, c) for c in conditions]
    return all(checks) if rule.get("match", "all") == "all" else any(checks)


def _shadows(earlier, later):
    """True when ``earlier`` fires for every event ``later`` would have matched.

    Because the classifier is first-match-wins in priority order, that makes ``later``
    unreachable. This is the owner's example: two rules both reading "prize above 100,000"
    means the second can never fire, whatever tier it awards.
    """
    later_terms = _terms(later)
    if not later_terms or not _terms(earlier):
        return False
    return all(_rule_matches_term(earlier, term) for term in later_terms)


def _describe_condition(cond):
    """One condition as a phrase a human reads, e.g. "prize >= 1,000 USD".

    Falls back to the raw dict rather than guessing when the shape is unfamiliar: a contradiction
    report that invents a phrase is worse than one that shows what is stored.
    """
    if not isinstance(cond, dict):
        return str(cond)
    field, op, value = cond.get("field"), cond.get("op"), cond.get("value")
    if field is None or op is None:
        return str(cond)
    symbol = {"gte": ">=", "lte": "<=", "eq": "="}.get(op, op)
    currency = cond.get("currency")
    number = _as_number(value)
    shown = f"{number:,.0f}" if number is not None else value
    return f"{field} {symbol} {shown}{' ' + currency if currency else ''}"


def _live(rules):
    """Rules that actually take part in classification, in evaluation order."""
    return [
        r for r in sorted(rules, key=lambda r: (r.get("priority", 0), r.get("id") or 0))
        if r.get("enabled", True) and not r.get("retired")
    ]


def _describe(rule):
    """Short human label for a rule in a contradiction message."""
    name = rule.get("name")
    if name:
        return f"'{name}'"
    return f"rule #{rule.get('id')}"


def _normalized_rules(rules, rate_map):
    """Every rule with its prize thresholds restated in NAIRA, plus the ones that would not convert.

    Returns ``(normalized_rules, unconvertible)`` where ``unconvertible`` is a list of
    ``(rule, condition, currency)`` triples. Only PRIZE conditions are touched; a teams/players
    count and a format test are copied through as they are.

    Each normalized rule keeps ``source_conditions``, the untouched list the admin actually
    authored. Contradiction ``entries`` quote that rather than the converted view, so a tool reading
    the report sees the rule as it is stored instead of a naira number nobody wrote.

    A condition whose currency has no exchange rate is DROPPED from the normalized rule rather than
    left in with its raw number. Two reasons. It is reported separately, so nothing is lost. And
    leaving it in would let the analysis treat a dollar figure as a naira figure, which is the exact
    confusion the conversion exists to prevent. Dropping it makes the checker claim LESS (a rule
    with a dropped condition stops implying things it cannot back up), which is the safe direction
    for a report that a human acts on.
    """
    from .currency import condition_currency, threshold_ngn

    out, unconvertible = [], []
    for rule in rules:
        conditions, kept = rule.get("conditions") or [], []
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            if cond.get("field") != "prize":
                kept.append(cond)
                continue
            ngn = threshold_ngn(cond, rate_map)
            if ngn is None:
                unconvertible.append((rule, cond, condition_currency(cond)))
                continue
            # Currency stripped along with the conversion: downstream now reads one currency, so a
            # leftover key could only mislead the next person to read this code.
            kept.append({"field": "prize", "op": cond.get("op"), "value": float(ngn),
                         "source_currency": condition_currency(cond)})
        clone = dict(rule)
        clone["conditions"] = kept
        clone["source_conditions"] = conditions
        out.append(clone)
    return out, unconvertible


def rule_contradictions(rules, default_tier=None, tables=None, rate_map=None):
    """Report tier rules that can never fire, and ranges no rule covers.

    ``rules``     - plain dicts (see the module note), any order; priority decides evaluation.
    ``default_tier`` - the fall-through tier, used only to explain where a gap lands.
    ``tables``    - optional ScoringTables, so a rule awarding a RETIRED tier can be flagged.
    ``rate_map``  - optional {currency: units per 1 USD}, needed only to compare thresholds written
                    in different currencies. Rules written in naira (which is every rule that omits
                    a currency, and therefore every rule authored before 2026-08-07) need no rates
                    and are analysed identically with or without it.

    Returns a list of contradiction dicts. Nothing here blocks a save; it is reporting.
    """
    found = []
    # Convert first, then run every existing check against one currency.
    normalized, unconvertible = _normalized_rules(rules, rate_map)
    live = _live(normalized)

    # 0. Thresholds that could not be converted. Reported FIRST because a rule in this state is not
    #    merely odd, it currently matches nothing at all: the classifier fails a condition it cannot
    #    convert closed (scoring/currency.convert_to_ngn explains why that beats the alternative).
    for rule, cond, currency in unconvertible:
        found.append(_contra(
            "unconvertible_threshold", f"event_tier_rules[{rule.get('id')}]",
            f"{_describe(rule)} has a prize threshold in {currency}, but no exchange rate is "
            f"stored for it, so that condition cannot be compared and the rule will not match "
            f"anything. Rewrite the threshold in naira, or pick a currency with rate data.",
            entries=[{"id": rule.get("id"), "currency": currency, "value": cond.get("value")}],
        ))

    # 1. Unreachable rules. Every pair is checked, not just neighbours, because a shadowing
    #    rule can sit several places above the rule it hides.
    for index, later in enumerate(live):
        for earlier in live[:index]:
            if _shadows(earlier, later):
                found.append(_contra(
                    "unreachable_rule", f"event_tier_rules[{later.get('id')}]",
                    f"{_describe(later)} can never fire: {_describe(earlier)} is checked "
                    f"first and already matches every event this rule would have matched.",
                    # `source_conditions` = what the admin authored, currency and all. The
                    # naira-converted view is an implementation detail of the check above and must
                    # not leak into a report a human reads.
                    entries=[
                        {"role": "shadowed_by", "id": earlier.get("id"),
                         "name": earlier.get("name"), "priority": earlier.get("priority"),
                         "tier": earlier.get("tier"),
                         "conditions": earlier.get("source_conditions")},
                        {"role": "unreachable", "id": later.get("id"),
                         "name": later.get("name"), "priority": later.get("priority"),
                         "tier": later.get("tier"),
                         "conditions": later.get("source_conditions")},
                    ],
                ))
                break  # one explanation per hidden rule is enough

    # 2. Gaps in the prize ladder. Only rules whose whole condition is a single prize
    #    threshold are considered, which is how the seeded rules are written; anything more
    #    complex is left alone rather than guessed at. A hole only counts when there is
    #    covered ground on BOTH sides, since everything below the lowest threshold is meant
    #    to fall through to the default tier.
    intervals = []
    for rule in live:
        conditions = [c for c in (rule.get("conditions") or []) if isinstance(c, dict)]
        if len(conditions) != 1 or conditions[0].get("field") != "prize":
            continue
        cond = conditions[0]
        value = _as_number(cond.get("value"))
        if value is None:
            continue
        if cond.get("op") == "gte":
            intervals.append((value, float("inf"), rule))
        elif cond.get("op") == "lte":
            intervals.append((0.0, value, rule))
    if len(intervals) >= 2:
        intervals.sort(key=lambda i: i[0])
        merged = []
        for low, high, _rule in intervals:
            if merged and low <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], high)
            else:
                merged.append([low, high])
        for first, second in zip(merged, merged[1:]):
            if first[1] < second[0]:
                where = (f"Tier {default_tier}" if default_tier is not None
                         else "the default tier")
                found.append(_contra(
                    "uncovered_range", "event_tier_rules",
                    f"No rule covers a prize pool between {first[1]:,.0f} and "
                    f"{second[0]:,.0f} naira. Events in that range fall through to {where}.",
                    entries=[{"from": first[1], "to": second[0], "currency": "NGN"}],
                ))

    # 2b. A CONDITION inside a rule that can never decide anything.
    #
    # The checks above look for dead RULES. This one looks inside a rule, because a condition can
    # be dead while the rule around it works fine, and nothing on screen would say so. The case
    # that prompted it (owner, 2026-08-16): a Match ANY rule reading "prize >= 1,000,000 naira OR
    # prize >= 1,000 USD", where $1,000 converts to about 1,358,704 naira. Every event clearing the
    # dollar line has already cleared the naira line, so the dollar line can never be the branch
    # that fires. The admin had written it expecting dollar events to be caught by it.
    #
    # The two match modes fail in opposite directions, and both are worth saying out loud:
    #   MATCH ANY - a branch is dead when ANOTHER branch is LOOSER, because the looser one already
    #               caught everything the stricter one would have.
    #   MATCH ALL - a condition is dead when ANOTHER condition is STRICTER, because the stricter
    #               one already excluded everything the looser one would have.
    # Either way the rule still classifies correctly; the condition is simply doing no work, which
    # is worth knowing before somebody edits the OTHER condition and silently changes the rule.
    for rule in live:
        conditions = [c for c in (rule.get("conditions") or []) if isinstance(c, dict)]
        if len(conditions) < 2:
            continue
        any_mode = rule.get("match", "all") != "all"
        for index, cond in enumerate(conditions):
            others = conditions[:index] + conditions[index + 1:]
            # WHICH ONE IS DEAD depends on the match mode, and getting it backwards names the
            # condition that is doing the work:
            #   ANY - this branch is dead when IT implies another, because everything it would
            #         catch was already caught by the looser branch (the $1,000 line above the
            #         1,000,000 naira line: the dollar line is the dead one).
            #   ALL - this condition is dead when ANOTHER implies IT, because the stricter
            #         condition has already excluded everything this one would have.
            # _condition_implies takes a conjunction, so each other condition is offered alone.
            covered_by = next(
                (other for other in others
                 if (_condition_implies([cond], other) if any_mode
                     else _condition_implies([other], cond))),
                None,
            )
            if covered_by is None:
                continue
            # Report what the ADMIN WROTE, currency and all. `conditions` here may be the
            # naira-normalized view, and a message quoting "1,358,704" at somebody who typed
            # "$1,000" is a message about a number they cannot find on their screen.
            source = (rule.get("source_conditions") or conditions)
            shown = source[index] if index < len(source) else cond
            covered_index = conditions.index(covered_by)
            covered_shown = (source[covered_index] if covered_index < len(source) else covered_by)
            found.append(_contra(
                "redundant_condition", f"event_tier_rules[{rule.get('id')}]",
                f"{_describe(rule)} has a condition that can never decide anything: its "
                f"{_describe_condition(shown)} is already covered by "
                f"{_describe_condition(covered_shown)} in the same rule. The rule still works; "
                f"that line just has no effect.",
                entries=[{"id": rule.get("id"), "condition": shown,
                          "covered_by": covered_shown}],
            ))

    # 3. A live rule that still awards a retired tier.
    if tables is not None:
        active = set(tables.active_tier_keys)
        for rule in live:
            key = f"tier_{rule.get('tier')}"
            if key not in active:
                found.append(_contra(
                    "retired_tier_in_use", f"event_tier_rules[{rule.get('id')}]",
                    f"{_describe(rule)} still awards {key}, which has been retired. Point "
                    f"it at an active tier or retire the rule.",
                    entries=[{"id": rule.get("id"), "tier": rule.get("tier")}],
                ))

    return found
