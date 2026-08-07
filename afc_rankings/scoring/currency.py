"""
afc_rankings/scoring/currency.py
────────────────────────────────
PURE currency handling for tournament-tier rule thresholds (owner 2026-08-07: "should be able to
select currency there").

WHY THIS MODULE EXISTS
    An ``EventTierRule`` prize condition used to be a bare number that everything agreed to read as
    NAIRA (see the CURRENCY note in ``admin_tournament_tiers.py`` and ``FIELD_META
    ["event_tier_rule_prize"]``). An admin who thinks in dollars had to do the conversion in their
    head and re-do it whenever the rate moved. Now a prize condition may carry its own ``currency``,
    and the comparison still happens in ONE canonical currency so rules written in different
    currencies remain comparable to each other and to the event's pool.

THE BACK-COMPATIBILITY CONTRACT (the important part)
    A prize condition with NO ``currency`` key means NAIRA. Every rule that existed before this
    change has no key, so every one of them keeps the exact meaning it had, without a data
    migration and without anything having to be backfilled. ``condition_currency`` is the single
    place that rule is written down, and every reader goes through it. A NAIRA threshold is
    ALSO short-circuited before the FX layer is consulted at all (see ``threshold_ngn``), so no
    pre-existing rule can be affected by missing, stale or moving exchange rates.

WHY NGN IS THE CANONICAL CURRENCY (and not USD, the platform's storage currency)
    Because that is what the comparison already used, and changing it would re-tier the platform.
    ``afc_tournament_and_scrims.views._prize_pool_ngn`` converts an event's pool to naira before
    classification, the seeded rules are authored in naira, and spec section 4 says naira. One
    canonical currency on both sides of the comparison, unchanged.

THIS MODULE NEVER TOUCHES THE DATABASE
    Same contract as the rest of ``afc_rankings/scoring``. The caller supplies ``rate_map``, the
    plain ``{currency_code: rate}`` dict built from ``afc_auth.FxRate`` where rate = units of that
    currency per 1 USD. That is the SAME map and the SAME formula
    ``afc_tournament_and_scrims.prize_sync._amount_ngn`` uses for prize money, so tiering and prize
    points can never disagree about what an amount is worth.

HOW IT CONNECTS
    * ``admin_tournament_tiers._eval_condition`` / ``classify`` - converts each prize threshold to
      naira before comparing it against the sample, which the caller already put in naira.
    * ``admin_tournament_tiers._validate_conditions`` - uses ``normalize_condition_currency`` so a
      saved condition always carries an explicit currency and the admin UI never has to know the
      absent-means-naira rule.
    * ``scoring.validation.rule_contradictions`` - normalizes every prize threshold to naira before
      looking for shadowed rules, or a dollar rule and a naira rule would be compared as bare
      numbers and the checker would report nonsense.
    * ``afc_tournament_and_scrims.views.auto_classify_event`` - builds the rate map once per
      classification pass and hands it down.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# The one currency every tier comparison happens in. Both sides of the comparison are converted to
# it: the event's prize pool by _prize_pool_ngn, the rule's threshold by threshold_ngn below.
BASE_CURRENCY = "NGN"

# The currency a prize condition is read as when it does not name one. This is the whole
# back-compatibility contract, in one constant.
IMPLICIT_CURRENCY = BASE_CURRENCY


def condition_currency(condition):
    """The currency ONE condition dict is expressed in, upper-cased.

    Returns ``IMPLICIT_CURRENCY`` ("NGN") when the condition names no currency, which is how every
    rule authored before this feature is read. Non-prize fields (teams / players / format) are
    counts and formats, not money, so they always report the base currency and nothing ever
    converts them.
    """
    if not isinstance(condition, dict):
        return IMPLICIT_CURRENCY
    if condition.get("field") != "prize":
        return IMPLICIT_CURRENCY
    raw = condition.get("currency")
    if raw is None:
        return IMPLICIT_CURRENCY
    code = str(raw).strip().upper()
    return code or IMPLICIT_CURRENCY


def normalize_condition_currency(condition):
    """Return ``condition`` with an explicit ``currency`` on a prize condition.

    Called on the WRITE path so a stored condition is self-describing: the admin UI, the audit
    snapshot and any future reader all see the currency spelled out rather than having to know the
    absent-means-naira rule. Applying this to a legacy naira condition writes "NGN", which is what
    it already meant, so re-saving an old rule cannot change its behaviour.
    """
    if not isinstance(condition, dict) or condition.get("field") != "prize":
        return condition
    out = dict(condition)
    out["currency"] = condition_currency(condition)
    return out


def _as_decimal(value):
    """Coerce a number-ish value to Decimal, or None. Booleans are refused (JSON ``true`` where an
    amount belongs is a mistake, and Python would otherwise read it as 1)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def convert_to_ngn(value, currency, rate_map):
    """Convert ``value`` from ``currency`` into naira. Returns a Decimal, or None when it cannot.

    ``rate_map`` = {code: units of that code per 1 USD}, the shape ``afc_auth.FxRate`` rows make
    and the shape ``prize_sync._amount_ngn`` already expects. USD is the pivot, so
    ``ngn = value / rate[currency] * rate[NGN]`` with rate[USD] fixed at 1.

    NAIRA SHORT-CIRCUITS BEFORE ``rate_map`` IS READ. That is deliberate and it is what makes this
    change safe for the rules that already exist: a naira threshold produces the identical number
    whether FX data is present, stale, or missing entirely.

    RETURNS NONE RATHER THAN THE RAW NUMBER when the rate is missing. This differs on purpose from
    ``prize_sync._amount_ngn``, which passes an unconvertible AMOUNT through unchanged. For an
    amount that is the cautious choice (a payout is shown slightly wrong instead of vanishing). For
    a THRESHOLD it would be the dangerous one: a "$1,000" bar read as "1,000 naira" is roughly
    1,400x too low, so the rule would suddenly match nearly every event on the platform and silently
    promote them all. Callers treat None as "this condition does not match" and report it: the
    failure then under-tiers (the documented no-rule-matched fall-through) instead of over-tiering,
    and it is visible on the admin page rather than silent.
    """
    amount = _as_decimal(value)
    if amount is None:
        return None
    code = str(currency or IMPLICIT_CURRENCY).strip().upper() or IMPLICIT_CURRENCY
    if code == BASE_CURRENCY:
        return amount
    rates = rate_map or {}
    ngn_rate = _as_decimal(rates.get(BASE_CURRENCY))
    src_rate = Decimal("1") if code == "USD" else _as_decimal(rates.get(code))
    if not ngn_rate or not src_rate:
        return None
    return (amount / src_rate) * ngn_rate


def threshold_ngn(condition, rate_map):
    """The naira value of ONE prize condition's threshold, or None when it cannot be converted.

    The single entry point both the classifier and the contradiction checker use, so there is
    exactly one answer to "what is this rule actually comparing against".
    """
    return convert_to_ngn(condition.get("value"), condition_currency(condition), rate_map)
