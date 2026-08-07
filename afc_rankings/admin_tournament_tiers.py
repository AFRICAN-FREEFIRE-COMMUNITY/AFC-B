"""
Admin write API - tournament tier classification rules (Phase 2).

This module owns the admin CRUD + reorder surface for the ordered, first-match-wins
rule list that classifies a tournament into a tier (Tier 1/2/3), plus the singleton
fall-through default, plus a *dry-run* classifier the admin UI uses to preview which
rule a hypothetical event would hit.

Data model (see ``models.py``):
  * ``EventTierRule``   - one rule: priority (lower = evaluated first), match ("all"/"any"),
                          conditions JSON [{field, op, value}], tier (1-3), enabled.
  * ``EventTierConfig`` - singleton row holding ``default_tier`` (the fall-through when an
                          event matches no enabled rule).

Idiom (matches the rest of afc_rankings - read views.py / serializers.py / admin_views.py):
  * function-based ``@api_view`` views, NOT class-based; NO DRF Serializer classes.
  * manual-dict serialization via the LOCAL ``serialize_tier_rule`` helper below.
  * the auth + audit foundation is REUSED from ``admin_views.py`` - never reimplemented:
        user, err = _auth(request)              # 401/403 short-circuit
        reason, err = _require_reason(request)   # mandatory >= 10-char audit reason
        with transaction.atomic(): ...write...
        _audit(user, "event_tier", "<action>", reason, object_ref=..., before=..., after=...)
  * list endpoints page through ``serializers.paginate`` and return the same
    {"results": [...], "pagination": meta, ...extra} envelope views.py uses.
  * validation errors mirror afc_auth.views: ``Response({"message": "..."}, status=...)``.

object_type is fixed to "event_tier" for every audit row (one of RankingAuditLog.OBJECT_TYPES),
so the §16 audit log filters every tournament-tier change into a single bucket.

RETIRE, NEVER DELETE (owner rule, 2026-08-03). A rule that past events were classified under
must stay readable, or those events have no explanation for the tier they sit in. DELETE on a
rule therefore RETIRES it: the row stays, ``retired_at`` / ``retired_by`` record who removed it
and when, the classifier skips it from then on, and ``?include_retired=1`` brings it back into
the list. ``event-tier-rules/<id>/restore/`` un-retires. Nothing on this surface destroys a row.
Rules also carry a free-text ``name`` so they can be RENAMED without changing what they do, and
so contradiction reports can name them instead of quoting a database id.

CONTRADICTIONS. The list and every write return a ``contradictions`` array built by
``scoring/validation.rule_contradictions``: rules that can never fire because an earlier rule
already matches everything they would (the owner's "two rules both above 100,000" case), and
prize ranges no rule covers. They are reported, never blocking - the config still works, it just
does not do what the author probably meant.

CURRENCY. A condition on the ``prize`` field may now name the currency its THRESHOLD is written in
(owner 2026-08-07: "should be able to select currency there"), but the COMPARISON still happens in
one canonical currency, NAIRA. An event's pool is converted to naira first
(afc_tournament_and_scrims.views._prize_pool_ngn) and so is the rule's threshold
(scoring/currency.threshold_ngn); comparing a raw $400 against a 100,000 naira threshold is the bug
that mis-tiered an event on 2026-08-03, and comparing a "$1,000" threshold as if it were 1,000 naira
would be the same bug pointed the other way. Every response carries ``field_meta`` and
``base_currency`` stating that, so the UI cannot render a threshold as a bare number.

A prize condition with NO ``currency`` key means NAIRA, which is what every rule written before this
change already meant, so none of them changes meaning and nothing had to be backfilled. The write
path normalizes an omitted currency to an explicit "NGN" so a stored rule is self-describing; that
is a spelling change, never a behaviour change. See ``scoring/currency.py`` for the whole contract,
including why an unconvertible threshold fails closed instead of falling back to the raw number.

FX DRIFT. A threshold written in a currency OTHER than naira is re-converted at the exchange rate in
force each time an event is classified, so the set of events it matches moves as the rate moves. A
naira threshold is fixed and never touches the FX layer. This is not hidden from the admin: the list
response carries ``fx_note`` plus each prize condition's naira equivalent at today's rate, and the
Tournament Tiers page prints both next to any non-naira threshold.

WHY no recalc enqueue here: editing a tier *rule* changes how FUTURE events are classified;
it does not mutate any already-computed TeamMonthlyScore / TeamQuarterlyScore. Re-tiering of
existing events is a separate re-evaluation pass (run-evaluation surface), so - unlike the
data-entry surfaces - these writes deliberately do NOT call ``tasks.enqueue_*``.

Auth: writes are gated on head_admin OR metrics_admin (the default ``_auth`` set,
RANKING_ADMIN_ROLES). The read-only list + the dry-run classifier still require a valid
admin token (they expose internal config), but skip the reason gate and the audit write.

URL routes returned to the coordinator (mounted under the existing ``rankings/`` prefix):
  GET    event-tier-rules/                     -> tier_rules_list      (read-only)
  POST   event-tier-rules/                     -> tier_rule_create
  PATCH  event-tier-rules/<int:rule_id>/       -> tier_rule_update
  DELETE event-tier-rules/<int:rule_id>/       -> tier_rule_delete
  POST   event-tier-rules/reorder/             -> tier_rules_reorder
  PATCH  event-tier-config/                    -> tier_config_update
  POST   event-tier-rules/classify/            -> tier_rules_classify   (read-only dry-run)
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.db import transaction

from django.utils import timezone

from .admin_views import _auth, _require_reason, _audit
from .models import EventTierRule, EventTierConfig
# Prize thresholds may be authored in any supported currency but are always COMPARED in naira.
# The pure conversion rules (including "no currency key means naira") live in scoring/currency.py.
from .scoring.currency import (
    BASE_CURRENCY,
    convert_to_ngn,
    normalize_condition_currency,
    threshold_ngn,
)
from .scoring.tables import FIELD_META
from .scoring.validation import rule_contradictions
from .serializers import paginate

# Tier rules decide how much every result in an event is worth, so writing them is head-admin
# only - the same gate as the rest of the editable scoring config. Reads keep the wider default
# (head_admin + metrics_admin) so a metrics admin can still see the rules in force.
TIER_WRITE_ROLES = ("head_admin",)

# ── the FX-drift disclosure (owner 2026-08-07) ──
# Served on every read so the admin page can print it verbatim and there is ONE authority on what
# picking a non-naira currency actually commits them to. The honest fact it has to carry: the
# threshold is converted at classification time, not at authoring time, so the same rule can match a
# different set of events next month without anybody editing it. A naira threshold has no such
# behaviour, which is why the sentence says so explicitly rather than leaving it to be inferred.
_FX_NOTE = (
    "A threshold in a currency other than naira is converted to naira at the exchange rate in force "
    "each time an event is classified, so the events it matches can change as the rate moves. A "
    "threshold written in naira is fixed and never uses an exchange rate."
)


# ───────────────────────── constants / validation tables ─────────────────────────
# Allowed condition operators, split by the field family they apply to. The numeric
# fields (prize/teams/players) compare with gte/lte against an int threshold; the
# `format` field uses the two boolean ops (is_lan / is_virtual) and ignores `value`.
_NUMERIC_FIELDS = ("prize", "teams", "players")
_NUMERIC_OPS = ("gte", "lte")
_FORMAT_OPS = ("is_lan", "is_virtual")
_VALID_MATCH = ("all", "any")
_VALID_TIERS = (1, 2, 3)            # EventTierRule.TIER_CHOICES keys
_VALID_FORMATS = ("lan", "virtual")  # accepted `format` values in a classify sample
# Only `prize` is money, so only `prize` may carry a currency. A currency on a team/player COUNT is
# a mistake worth refusing rather than silently dropping: the admin would believe it did something.
_CURRENCY_FIELDS = ("prize",)


# ───────────────────────── FX ─────────────────────────
def _fx_rate_map():
    """The {currency: rate} map (rate = units per 1 USD) every threshold conversion here reads.

    ONE query, and the caller is expected to hold onto the result. The trap this exists to avoid:
    ``afc_tournament_and_scrims.views._prize_pool_ngn`` builds this map itself on every call, so a
    loop over events (the ``reclassify_event_tiers`` command, a contradiction report over a long
    rule list) used to re-read the whole FxRate table once per row. Both now take a prebuilt map.

    Reads the same ``afc_auth.FxRate`` table ``afc_auth.fx`` populates and ``prize_sync`` converts
    prize money with, so tiering and prize points always agree on what an amount is worth.
    """
    from afc_auth.models import FxRate
    return {f.currency: f.rate for f in FxRate.objects.all()}


# ───────────────────────── local serializer ─────────────────────────
def _serialize_conditions(conditions, rate_map):
    """Conditions as the admin UI reads them: every prize condition carries an EXPLICIT currency,
    plus ``value_ngn``, the naira figure the classifier will actually compare against.

    Why spell the currency out here rather than leave the stored blob alone: rules written before
    this feature have no ``currency`` key and mean naira (scoring/currency.condition_currency). If
    the API passed that gap through, every consumer would have to re-implement the default, and the
    first one to forget would render a naira rule as though it had no currency at all.

    ``value_ngn`` is null when the threshold cannot be converted (no FX row for that currency). The
    page shows that as a warning rather than a number, because a missing conversion means the
    condition currently matches nothing (see scoring/currency.convert_to_ngn).
    """
    out = []
    for c in (conditions or []):
        if not isinstance(c, dict):
            continue
        if c.get("field") != "prize":
            out.append(c)
            continue
        item = normalize_condition_currency(c)
        ngn = threshold_ngn(c, rate_map)
        # int, not Decimal: this is JSON, and the sample it is compared against is an int naira
        # figure too (_prize_pool_ngn truncates to whole naira).
        item["value_ngn"] = int(ngn) if ngn is not None else None
        out.append(item)
    return out


def serialize_tier_rule(r, rate_map=None):
    """Manual-dict serialization of one EventTierRule (mirrors serializers.py style).

    ``rate_map`` (see ``_fx_rate_map``) is optional so a single-rule caller stays a one-liner; pass
    it when serializing a list, or every row re-reads the FxRate table.
    """
    if rate_map is None:
        rate_map = _fx_rate_map()
    return {
        "id": r.id,
        "name": r.name,
        "priority": r.priority,
        "match": r.match,
        # [{field, op, value, currency?, value_ngn?}] - see _serialize_conditions.
        "conditions": _serialize_conditions(r.conditions, rate_map),
        # The currency every prize condition is COMPARED in, whatever it was authored in. Stated
        # inline as well as in field_meta so a caller rendering one rule in isolation still has it.
        "condition_currency": {"prize": BASE_CURRENCY},
        "tier": r.tier,
        "enabled": r.enabled,
        # Retired rules are kept, never deleted - see the module docstring.
        "retired": r.retired_at is not None,
        "retired_at": r.retired_at.isoformat() if r.retired_at else None,
        "retired_by": r.retired_by.username if r.retired_by_id else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _rule_dicts(queryset=None):
    """Every rule as the plain dicts ``scoring/validation.rule_contradictions`` expects.

    That function is in the pure package and never touches the ORM, so the translation
    happens here. Retired rules are included with their flag set; the checker ignores them.

    Conditions are passed through RAW (not through ``_serialize_conditions``): the checker does its
    own currency normalization from the same scoring/currency helpers, and handing it a value that
    had already been converted would convert it twice.
    """
    rules = queryset if queryset is not None else EventTierRule.objects.all()
    return [
        {
            "id": r.id, "name": r.name, "priority": r.priority, "match": r.match,
            "conditions": r.conditions or [], "tier": r.tier,
            "enabled": r.enabled, "retired": r.retired_at is not None,
        }
        for r in rules
    ]


def _contradictions(rate_map=None):
    """Report rules that can never fire, and prize ranges nothing covers.

    Recomputed on every read and after every write so the admin sees the consequence of the
    edit they just made. Advisory only: it never blocks a write.

    ``rate_map`` is threaded through because thresholds can now be written in different currencies:
    without it, "prize >= 1000 USD" and "prize >= 100000 NGN" would be compared as the bare numbers
    1000 and 100000 and the checker would report the shadowing backwards.
    """
    from .aggregation import resolve_tables
    return rule_contradictions(
        _rule_dicts(), default_tier=_get_config().default_tier, tables=resolve_tables(),
        rate_map=rate_map if rate_map is not None else _fx_rate_map(),
    )


def _validate_name(value):
    """Return (name, None) or (None, error_message). A blank name is allowed."""
    if value is None:
        return "", None
    if not isinstance(value, str):
        return None, "`name` must be text."
    name = value.strip()
    if len(name) > 120:
        return None, "`name` must be 120 characters or fewer."
    return name, None


# ───────────────────────── shared helpers ─────────────────────────
def _get_config():
    """Fetch (or lazily create) the EventTierConfig singleton with the spec default (Tier 3)."""
    config, _ = EventTierConfig.objects.get_or_create(pk=1, defaults={"default_tier": 3})
    return config


def _validate_match(match):
    """Return (normalized_match, None) or (None, error_message)."""
    if match not in _VALID_MATCH:
        return None, f"`match` must be one of {list(_VALID_MATCH)}."
    return match, None


def _validate_tier(tier):
    """Return (int_tier, None) or (None, error_message)."""
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        return None, "`tier` must be an integer (1, 2, or 3)."
    if tier not in _VALID_TIERS:
        return None, f"`tier` must be one of {list(_VALID_TIERS)}."
    return tier, None


def _validate_currency(field, raw, index):
    """Validate the optional `currency` on one condition. Return (code, None) or (None, error).

    Only ``prize`` is money, so only ``prize`` may carry a currency (``_CURRENCY_FIELDS``). A
    currency sent on a team/player count is REFUSED rather than dropped, because dropping it would
    leave the admin believing they had set something.

    Omitted currency resolves to naira, which is what every rule authored before this feature
    already meant - the whole back-compatibility contract, see scoring/currency.py. The accepted
    codes are the platform's single currency menu (afc_auth.currencies), so this picker can never
    drift from the one on the prize-pool form. ``is_known_currency`` rather than
    ``is_supported_currency``: a rule saved on a since-redenominated code (SLL, ZWL) must still be
    re-saveable, and those codes still have FX rows so they still convert.
    """
    if raw is None:
        return None if field not in _CURRENCY_FIELDS else BASE_CURRENCY, None
    if field not in _CURRENCY_FIELDS:
        return None, (
            f"Condition #{index}: only a `prize` threshold has a currency. "
            f"`{field}` is a count, not an amount."
        )
    from afc_auth.currencies import is_known_currency
    code = str(raw).strip().upper()
    if not is_known_currency(code):
        return None, (
            f"Condition #{index}: `{raw}` is not a currency AFC supports. Use a three-letter "
            f"ISO code from the currency list, for example NGN or USD."
        )
    return code, None


def _validate_conditions(conditions):
    """Validate the conditions JSON list. Return (clean_list, None) or (None, error_message).

    Each condition is {field, op, value}, plus an optional {currency} on a ``prize`` threshold.
    Numeric fields need an int value, format ops ignore value. We normalize numeric values to int
    and always write an explicit currency onto a prize condition, so the stored JSON is clean,
    self-describing, and the classifier can compare without re-parsing or re-deriving a default.

    Writing "NGN" onto a condition that omitted it does NOT change what the rule does: an omitted
    currency already meant naira everywhere it was read.
    """
    if not isinstance(conditions, list):
        return None, "`conditions` must be a list of {field, op, value} objects."
    clean = []
    for i, c in enumerate(conditions):
        if not isinstance(c, dict):
            return None, f"Condition #{i} must be an object with field/op/value."
        field = c.get("field")
        op = c.get("op")
        currency, msg = _validate_currency(field, c.get("currency"), i)
        if msg:
            return None, msg
        if field in _NUMERIC_FIELDS:
            # prize/teams/players → must use gte/lte with an int threshold.
            if op not in _NUMERIC_OPS:
                return None, f"Condition #{i}: field `{field}` requires op in {list(_NUMERIC_OPS)}."
            try:
                value = int(c.get("value"))
            except (TypeError, ValueError):
                return None, f"Condition #{i}: field `{field}` requires an integer `value`."
            row = {"field": field, "op": op, "value": value}
            if currency is not None:
                # prize only; teams/players stay exactly the three-key shape they have always had,
                # so nothing about a count condition changes on disk.
                row["currency"] = currency
            clean.append(row)
        elif field == "format":
            # format → is_lan / is_virtual; value is irrelevant (kept null for a clean blob).
            if op not in _FORMAT_OPS:
                return None, f"Condition #{i}: field `format` requires op in {list(_FORMAT_OPS)}."
            clean.append({"field": "format", "op": op, "value": None})
        else:
            return None, (
                f"Condition #{i}: `field` must be one of "
                f"{list(_NUMERIC_FIELDS) + ['format']}."
            )
    return clean, None


# ───────────────────────── pure classification logic (first-match-wins) ─────────────────────────
def _eval_condition(c, sample, rate_map=None):
    """Evaluate ONE condition object against a sample dict. Returns a bool.

    sample = {"prize": int, "teams": int, "players": int, "format": "lan"|"virtual"}, with
    ``prize`` already in NAIRA (the caller converts - see _prize_pool_ngn).
    Numeric ops compare sample[field] against the condition's value; format ops test
    sample["format"]. An unknown op or a missing sample key fails closed (returns False)
    so a malformed rule can never accidentally match everything.

    ``prize`` is the one field whose threshold may be written in another currency, so it is
    converted to naira here before comparing. A naira threshold (which is every threshold written
    before 2026-08-07, and every one written without picking a currency) short-circuits inside
    ``threshold_ngn`` and never reads ``rate_map`` at all, so those rules compare exactly the number
    they always compared. A threshold that CANNOT be converted (no FX row for its currency) fails
    closed for the reason spelled out in scoring/currency.convert_to_ngn: reading a dollar bar as a
    naira bar would silently promote nearly every event on the platform.
    """
    field = c.get("field")
    op = c.get("op")
    if field in _NUMERIC_FIELDS:
        actual = sample.get(field)
        if actual is None:
            return False
        threshold = threshold_ngn(c, rate_map) if field == "prize" else c.get("value")
        if threshold is None:
            return False
        if op == "gte":
            return actual >= threshold
        if op == "lte":
            return actual <= threshold
        return False
    if field == "format":
        fmt = sample.get("format")
        if op == "is_lan":
            return fmt == "lan"
        if op == "is_virtual":
            return fmt == "virtual"
        return False
    return False


def classify(rules, default_tier, sample, rate_map=None):
    """First-match-wins classification: returns {"tier": int, "matched_rule_id": int|None}.

    ``rules`` is an iterable of EventTierRule, expected pre-ordered by priority (the caller
    passes the priority-ordered queryset). Disabled AND RETIRED rules are skipped - a retired
    rule is kept only so the events it once classified stay explainable, it must never
    classify anything new. For each remaining rule, its conditions are combined with
    all()/any() per the rule's ``match`` ("all"=AND, "any"=OR). A rule with NO conditions
    never matches (all([]) is True, but an empty rule classifying everything would be a
    footgun - so we require at least one condition to match). The first matching rule's tier
    wins; if none match, fall through to ``default_tier``.

    NOTE on the `prize` field: the sample's value is in NAIRA - the caller converts it
    (afc_tournament_and_scrims.views._prize_pool_ngn). A rule's THRESHOLD may be authored in any
    supported currency and is converted to naira here, using ``rate_map`` ({currency: units per 1
    USD}, built by ``_fx_rate_map``). Pass the map in when classifying more than one event, or every
    call re-reads the whole FxRate table. A naira threshold needs no rate and is compared exactly as
    before whether or not a map is supplied, which is what keeps every pre-existing rule identical.
    """
    for rule in rules:
        if not rule.enabled or getattr(rule, "retired_at", None) is not None:
            continue
        conditions = rule.conditions or []
        if not conditions:
            # An empty-condition rule is treated as non-matching (see docstring).
            continue
        results = (_eval_condition(c, sample, rate_map) for c in conditions)
        matched = all(results) if rule.match == "all" else any(results)
        if matched:
            return {"tier": rule.tier, "matched_rule_id": rule.id}
    return {"tier": default_tier, "matched_rule_id": None}


# ───────────────────────── LIST (read-only) ─────────────────────────
@api_view(["GET"])
def tier_rules_list(request):
    """The ordered, first-match-wins rules that classify an event into a tier.

    Purpose:  drive the admin Rankings > Tournament Tiers page.
    Auth:     Bearer SessionToken, head_admin or metrics_admin (reads use the wider set).
    Request:  optional query ``?include_retired=1`` to show retired rules as well
              (they are hidden by default), plus the usual ``page`` / ``page_size``.
    Response 200::

        {"results": [
            {"id":3,"name":"Major LAN","priority":0,"match":"all",
             "conditions":[{"field":"prize","op":"gte","value":1000,
                            "currency":"USD","value_ngn":1364220}],
             "condition_currency":{"prize":"NGN"},
             "tier":1,"enabled":true,
             "retired":false,"retired_at":null,"retired_by":null,
             "created_at":"...","updated_at":"..."}
         ],
         "pagination": {...},
         "default_tier": 3,
         "base_currency": "NGN",
         "fx_note": "...one sentence on FX drift, see below...",
         "field_meta": {"event_tier_rule_prize": {"currency":"NGN", ...}},
         "contradictions": [{"kind":"unreachable_rule","path":...,"message":...,"entries":[...]}],
         "include_retired": false}

    ``base_currency`` is the currency the comparison happens in, whatever a threshold was authored
    in. ``fx_note`` states, in one sentence the page can print verbatim, that a non-naira threshold
    is re-converted at classification time and therefore moves with the exchange rate. Both are
    served rather than hardcoded in the client so there is one authority on the currency contract.

    Consumed by: the admin Tournament Tiers page (rule table, the retired filter, the currency
    picker on a prize condition, and the warning banner fed by ``contradictions``).
    """
    user, err = _auth(request)
    if err:
        return err

    include_retired = str(request.query_params.get("include_retired", "")).lower() in (
        "1", "true", "yes")
    qs = EventTierRule.objects.all().order_by("priority", "created_at")
    if not include_retired:
        qs = qs.filter(retired_at__isnull=True)
    items, meta = paginate(request, qs)
    config = _get_config()
    # ONE FxRate read for the whole response: every serialized row and the contradiction pass share
    # it, instead of each re-reading the table (see _fx_rate_map).
    rate_map = _fx_rate_map()
    return Response({
        "results": [serialize_tier_rule(r, rate_map) for r in items],
        "pagination": meta,
        "default_tier": config.default_tier,
        "base_currency": BASE_CURRENCY,
        "fx_note": _FX_NOTE,
        "field_meta": {"event_tier_rule_prize": FIELD_META["event_tier_rule_prize"]},
        "contradictions": _contradictions(rate_map),
        "include_retired": include_retired,
    })


# ───────────────────────── CREATE ─────────────────────────
@api_view(["POST"])
def tier_rule_create(request):
    """Add a tier classification rule.

    Purpose:  let a head admin add a rule without a deploy.
    Auth:     Bearer SessionToken, head_admin only.
    Request::

        {"name": "Major LAN",                       # optional, free text, renameable later
         "match": "all" | "any",                    # default "all"
         "conditions": [{"field":"prize"|"teams"|"players"|"format",
                         "op":"gte"|"lte"|"is_lan"|"is_virtual",
                         "value": 1000000,
                         "currency": "NGN"}],       # prize only; omitted means NGN
         "tier": 1 | 2 | 3,                         # default 2
         "enabled": true,                           # default true
         "reason": "at least 10 characters"}

    A prize threshold may be written in any currency on the platform's currency menu
    (afc_auth.currencies) and is converted to naira for the comparison. Omitting ``currency``
    means naira, so a client that never sends the key behaves exactly as it did before.

    Response 201: the rule (see the list endpoint's shape) plus
        ``{"contradictions": [...]}`` so the admin immediately sees if the new rule can
        never fire, or hides one below it.
    Response 400 ``{"message": "..."}`` on a bad field or a missing reason.

    ``priority`` is assigned as (current max) + 1 so a new rule lands LAST in the evaluation
    order (lowest precedence) until an explicit reorder moves it.

    Consumed by: the admin Tournament Tiers page's "Add rule" dialog.
    """
    user, err = _auth(request, roles=TIER_WRITE_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    # Validate the inbound fields before opening the transaction.
    match, msg = _validate_match(request.data.get("match", "all"))
    if msg:
        return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    conditions, msg = _validate_conditions(request.data.get("conditions", []))
    if msg:
        return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    tier, msg = _validate_tier(request.data.get("tier", 2))
    if msg:
        return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    name, msg = _validate_name(request.data.get("name"))
    if msg:
        return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    enabled = bool(request.data.get("enabled", True))

    with transaction.atomic():
        # New rule sorts to the bottom of the priority order (max + 1, or 0 when empty).
        max_priority = EventTierRule.objects.order_by("-priority").values_list("priority", flat=True).first()
        next_priority = (max_priority + 1) if max_priority is not None else 0
        rule = EventTierRule.objects.create(
            priority=next_priority,
            name=name,
            match=match,
            conditions=conditions,
            tier=tier,
            enabled=enabled,
        )
        after = serialize_tier_rule(rule)
        # before={} - the rule did not exist prior to this write.
        _audit(user, "event_tier", "create", reason, object_ref=rule.id, before={}, after=after)

    rate_map = _fx_rate_map()
    body = serialize_tier_rule(rule, rate_map)
    body["contradictions"] = _contradictions(rate_map)
    return Response(body, status=status.HTTP_201_CREATED)


# ───────────────────────── UPDATE ─────────────────────────
@api_view(["PATCH"])
def tier_rule_update(request, rule_id):
    """Edit or rename a rule. Every body key is optional (PATCH semantics).

    Purpose:  change what a rule does, or just what it is called.
    Auth:     Bearer SessionToken, head_admin only.
    Request::

        {"name": "Major LAN events",      # rename, does not change behaviour
         "match": "all" | "any",
         "conditions": [{"field","op","value","currency"}],  # prize currency, omitted means NGN
         "tier": 1 | 2 | 3,
         "enabled": true | false,          # the reversible on/off switch, NOT retirement
         "reason": "at least 10 characters"}

    Response 200: the rule plus ``{"contradictions": [...]}``.
    Response 400 on a bad field; 404 when the rule does not exist.

    ``priority`` is NOT editable here - use the reorder endpoint, which keeps the whole
    order consistent in one atomic pass. Retirement is not editable here either: use DELETE
    (which retires) and ``restore/``, so the who and when are always recorded.

    Consumed by: the admin Tournament Tiers page's inline edit + rename.
    """
    user, err = _auth(request, roles=TIER_WRITE_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    rule = EventTierRule.objects.filter(pk=rule_id).first()
    if not rule:
        return Response({"message": "Tier rule not found."}, status=status.HTTP_404_NOT_FOUND)

    # Validate only the fields the caller actually sent (PATCH semantics).
    if "match" in request.data:
        match, msg = _validate_match(request.data.get("match"))
        if msg:
            return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    if "conditions" in request.data:
        conditions, msg = _validate_conditions(request.data.get("conditions"))
        if msg:
            return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    if "tier" in request.data:
        tier, msg = _validate_tier(request.data.get("tier"))
        if msg:
            return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    if "name" in request.data:
        name, msg = _validate_name(request.data.get("name"))
        if msg:
            return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        before = serialize_tier_rule(rule)
        if "match" in request.data:
            rule.match = match
        if "conditions" in request.data:
            rule.conditions = conditions
        if "tier" in request.data:
            rule.tier = tier
        if "name" in request.data:
            rule.name = name
        if "enabled" in request.data:
            rule.enabled = bool(request.data.get("enabled"))
        rule.save()
        after = serialize_tier_rule(rule)
        _audit(user, "event_tier", "update", reason, object_ref=rule.id, before=before, after=after)

    rate_map = _fx_rate_map()
    body = serialize_tier_rule(rule, rate_map)
    body["contradictions"] = _contradictions(rate_map)
    return Response(body)


# ───────────────────────── RETIRE (the old DELETE) ─────────────────────────
@api_view(["DELETE"])
def tier_rule_delete(request, rule_id):
    """RETIRE a rule. Nothing is destroyed.

    Purpose:  take a rule out of classification for good while keeping it readable.
    Auth:     Bearer SessionToken, head_admin only.
    Request:  ``{"reason": "at least 10 characters"}``.
    Response 200::

        {"message": "Tier rule retired.",
         "rule": { ...the rule, now with retired: true, retired_at, retired_by... },
         "contradictions": [...]}

    Response 400 when the rule is already retired, or the reason is missing; 404 when the
    rule does not exist.

    WHY THIS IS NOT A DELETE (owner rule, 2026-08-03): events carry the tier this rule gave
    them. Destroy the rule and those events have no explanation for the tier they sit in,
    and the audit trail points at a row that is gone. Retiring keeps the row, records who
    retired it and when, and takes it out of ``classify`` from that moment on. It is
    reversible through ``event-tier-rules/<id>/restore/``.

    Consumed by: the admin Tournament Tiers page's "Retire" action (the button previously
    labelled Delete).
    """
    user, err = _auth(request, roles=TIER_WRITE_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    rule = EventTierRule.objects.filter(pk=rule_id).first()
    if not rule:
        return Response({"message": "Tier rule not found."}, status=status.HTTP_404_NOT_FOUND)
    if rule.retired_at is not None:
        return Response({"message": "This rule is already retired."},
                        status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        before = serialize_tier_rule(rule)
        rule.retired_at = timezone.now()
        rule.retired_by = user
        rule.save(update_fields=["retired_at", "retired_by", "updated_at"])
        after = serialize_tier_rule(rule)
        _audit(user, "event_tier", "retire", reason, object_ref=rule.id,
               before=before, after=after)

    rate_map = _fx_rate_map()
    return Response({
        "message": "Tier rule retired.",
        "rule": serialize_tier_rule(rule, rate_map),
        "contradictions": _contradictions(rate_map),
    })


# ───────────────────────── RESTORE (un-retire) ─────────────────────────
@api_view(["POST"])
def tier_rule_restore(request, rule_id):
    """Bring a retired rule back into classification.

    Purpose:  undo a retirement, for example one done by mistake.
    Auth:     Bearer SessionToken, head_admin only.
    Request:  ``{"reason": "at least 10 characters"}``.
    Response 200: ``{"message": "Tier rule restored.", "rule": {...},
                     "contradictions": [...]}``.
    Response 400 when the rule is not retired; 404 when it does not exist.

    The rule returns at its original priority. Whether that still makes sense is exactly
    what the returned ``contradictions`` are for: a rule restored below a broader one added
    since would come back unreachable, and the response says so.

    Consumed by: the admin Tournament Tiers page, from the retired-rules view.
    """
    user, err = _auth(request, roles=TIER_WRITE_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    rule = EventTierRule.objects.filter(pk=rule_id).first()
    if not rule:
        return Response({"message": "Tier rule not found."}, status=status.HTTP_404_NOT_FOUND)
    if rule.retired_at is None:
        return Response({"message": "This rule is not retired."},
                        status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        before = serialize_tier_rule(rule)
        rule.retired_at = None
        rule.retired_by = None
        rule.save(update_fields=["retired_at", "retired_by", "updated_at"])
        after = serialize_tier_rule(rule)
        _audit(user, "event_tier", "restore", reason, object_ref=rule.id,
               before=before, after=after)

    rate_map = _fx_rate_map()
    return Response({
        "message": "Tier rule restored.",
        "rule": serialize_tier_rule(rule, rate_map),
        "contradictions": _contradictions(rate_map),
    })


# ───────────────────────── REORDER ─────────────────────────
@api_view(["POST"])
def tier_rules_reorder(request):
    """Reorder the rules. Order decides which rule wins when several match.

    Purpose:  drag-and-drop reordering on the Tournament Tiers page.
    Auth:     Bearer SessionToken, head_admin only.
    Request:  ``{"order": [rule_id, ...], "reason": "at least 10 characters"}``.
              The list must contain every LIVE (non-retired) rule id exactly once. Retired
              rules keep their stored priority and must NOT be listed - they classify
              nothing, so their position is meaningless until they are restored.
    Response 200: ``{"results": [ ...live rules in the new order... ],
                     "contradictions": [...]}``.
    Response 400 on a duplicate id, an unknown id, or a partial list.

    ``priority`` is set to the rule's INDEX in ``order`` (0-based), so order[0] becomes the
    highest-precedence rule. A partial reorder would leave the order ambiguous, which is why
    the whole live set is required.

    Consumed by: the admin Tournament Tiers page's drag handles.
    """
    user, err = _auth(request, roles=TIER_WRITE_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    order = request.data.get("order")
    if not isinstance(order, list) or not order:
        return Response(
            {"message": "`order` must be a non-empty list of rule ids."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Reject duplicates up front - a repeated id would silently overwrite a priority.
    if len(order) != len(set(order)):
        return Response(
            {"message": "`order` contains duplicate rule ids."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    live_ids = set(EventTierRule.objects.filter(retired_at__isnull=True)
                   .values_list("id", flat=True))
    if set(order) != live_ids:
        return Response(
            {"message": "`order` must list every active rule id exactly once. Retired rules "
                        "are not ordered."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = {"order": list(EventTierRule.objects.filter(retired_at__isnull=True)
                                 .order_by("priority", "created_at")
                                 .values_list("id", flat=True))}
        # priority = position in the supplied order (index 0 = top of the list = evaluated first).
        for index, rid in enumerate(order):
            EventTierRule.objects.filter(pk=rid).update(priority=index)
        after = {"order": list(order)}
        _audit(user, "event_tier", "reorder", reason, before=before, after=after)

    qs = EventTierRule.objects.filter(retired_at__isnull=True).order_by("priority", "created_at")
    rate_map = _fx_rate_map()
    return Response({
        "results": [serialize_tier_rule(r, rate_map) for r in qs],
        "contradictions": _contradictions(rate_map),
    })


# ───────────────────────── CONFIG (default tier) ─────────────────────────
@api_view(["PATCH"])
def tier_config_update(request):
    """Set the fall-through tier an event gets when no rule matches it.

    Purpose:  edit the one setting that is not a rule.
    Auth:     Bearer SessionToken, head_admin only.
    Request:  ``{"default_tier": 1 | 2 | 3, "reason": "at least 10 characters"}``.
    Response 200: ``{"default_tier": 3, "contradictions": [...]}``.
    Response 400 when the tier is out of range or the reason is missing.

    Consumed by: the admin Tournament Tiers page's default-tier selector.
    """
    user, err = _auth(request, roles=TIER_WRITE_ROLES)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    default_tier, msg = _validate_tier(request.data.get("default_tier"))
    if msg:
        # Reuse the tier validator (same 1-3 range); reword for this field.
        return Response(
            {"message": msg.replace("`tier`", "`default_tier`")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        config = _get_config()
        before = {"default_tier": config.default_tier}
        config.default_tier = default_tier
        config.save(update_fields=["default_tier", "updated_at"])
        after = {"default_tier": config.default_tier}
        _audit(user, "event_tier", "default", reason, object_ref=config.pk, before=before, after=after)

    return Response({
        "default_tier": config.default_tier,
        "contradictions": _contradictions(),
    })


# ───────────────────────── CLASSIFY (read-only dry-run) ─────────────────────────
# ── dry-run twin of the production classifier ──
# This previews the SAME first-match-wins classifier the real scoring path runs when an event is
# scored. The rule semantics - priority order, first-match-wins, all/any, empty-rule means
# non-matching - MUST stay identical to the production classifier, or the admin preview lies
# about the tier an event would land in.
@api_view(["POST"])
def tier_rules_classify(request):
    """Preview which rule a hypothetical event would hit, using the SAME classifier.

    Purpose:  let an admin check a change before saving it.
    Auth:     Bearer SessionToken, head_admin or metrics_admin (read-only, no audit row).
    Request:  ``{"prize": 400, "prize_currency": "USD",
                 "teams": 32, "players": 0, "format": "lan"|"virtual"}``.
              ``prize_currency`` is optional and defaults to NAIRA, so a caller that never sends it
              behaves exactly as before. It exists because an admin thinking in dollars should be
              able to type the pool the way an organizer would enter it, rather than pre-converting
              in their head. It is converted here through the SAME path the real classification
              runs (afc_tournament_and_scrims.views._prize_pool_ngn converts the event's pool from
              Event.prize_currency using the same FxRate map and the same formula).
    Response 200: ``{"tier": 1, "matched_rule_id": 3 | null,
                     "prize_currency": "NGN", "prize_ngn": 545688, "prize_converted": true}``.
                  ``matched_rule_id`` null means nothing matched and the default tier applies.
                  ``prize_ngn`` is the naira figure actually compared, so the panel can show the
                  admin the number the rules saw rather than only the number they typed.
    Response 400 when a numeric field is not a number, the format is unrecognised, the currency is
                 not on the platform's menu, or the pool cannot be converted (no FX data).

    Consumed by: the admin Tournament Tiers page's "Test a tournament" panel.
    """
    user, err = _auth(request)
    if err:
        return err

    data = request.data
    # Build the sample the classifier compares against. Numeric fields coerce to int;
    # bad input → 400 rather than a silently-wrong preview.
    sample = {}
    for field in _NUMERIC_FIELDS:
        raw = data.get(field, 0)
        try:
            sample[field] = int(raw)
        except (TypeError, ValueError):
            return Response(
                {"message": f"`{field}` must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    fmt = data.get("format")
    if fmt is not None and fmt not in _VALID_FORMATS:
        return Response(
            {"message": f"`format` must be one of {list(_VALID_FORMATS)}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    sample["format"] = fmt

    # ── the sample pool, converted into the comparison currency ──
    # Reuses _validate_currency so the preview accepts exactly the codes a saved rule accepts.
    prize_currency, msg = _validate_currency("prize", data.get("prize_currency"), 0)
    if msg:
        return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)
    rate_map = _fx_rate_map()
    prize_ngn = convert_to_ngn(sample["prize"], prize_currency, rate_map)
    if prize_ngn is None:
        # Only reachable for a non-naira sample with no FX row. Refusing beats previewing a tier
        # from a pool we could not convert: the admin would take the answer at face value.
        return Response(
            {"message": f"No exchange rate is stored for {prize_currency}, so a pool in that "
                        f"currency cannot be converted to naira right now."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    sample["prize"] = int(prize_ngn)

    rules = list(EventTierRule.objects.all().order_by("priority", "created_at"))
    config = _get_config()
    result = classify(rules, config.default_tier, sample, rate_map)
    # Stated so the caller cannot present the preview without saying what currency it read.
    result["prize_currency"] = BASE_CURRENCY
    result["prize_ngn"] = sample["prize"]
    result["prize_converted"] = prize_currency != BASE_CURRENCY
    return Response(result)
