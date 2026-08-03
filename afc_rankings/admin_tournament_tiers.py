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

CURRENCY. A condition on the ``prize`` field is compared in NAIRA. An event's pool is stored in
the event's own currency and converted first (afc_tournament_and_scrims.views._prize_pool_ngn);
comparing a raw $400 against a 100,000 naira threshold is the bug that mis-tiered an event on
2026-08-03. Every response carries ``field_meta`` stating that, so the UI cannot render the
threshold as a bare number.

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
from .scoring.tables import FIELD_META
from .scoring.validation import rule_contradictions
from .serializers import paginate

# Tier rules decide how much every result in an event is worth, so writing them is head-admin
# only - the same gate as the rest of the editable scoring config. Reads keep the wider default
# (head_admin + metrics_admin) so a metrics admin can still see the rules in force.
TIER_WRITE_ROLES = ("head_admin",)


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


# ───────────────────────── local serializer ─────────────────────────
def serialize_tier_rule(r):
    """Manual-dict serialization of one EventTierRule (mirrors serializers.py style)."""
    return {
        "id": r.id,
        "name": r.name,
        "priority": r.priority,
        "match": r.match,
        "conditions": r.conditions,   # already a JSON list [{field, op, value}]
        # A prize threshold is in naira, always. Stated inline as well as in field_meta so a
        # caller rendering one rule in isolation still has it.
        "condition_currency": {"prize": "NGN"},
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


def _contradictions():
    """Report rules that can never fire, and prize ranges nothing covers.

    Recomputed on every read and after every write so the admin sees the consequence of the
    edit they just made. Advisory only: it never blocks a write.
    """
    from .aggregation import resolve_tables
    return rule_contradictions(
        _rule_dicts(), default_tier=_get_config().default_tier, tables=resolve_tables(),
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


def _validate_conditions(conditions):
    """Validate the conditions JSON list. Return (clean_list, None) or (None, error_message).

    Each condition is {field, op, value}; numeric fields need an int value, format ops
    ignore value. We normalize numeric values to int so the stored JSON is clean and the
    classifier can compare without re-parsing.
    """
    if not isinstance(conditions, list):
        return None, "`conditions` must be a list of {field, op, value} objects."
    clean = []
    for i, c in enumerate(conditions):
        if not isinstance(c, dict):
            return None, f"Condition #{i} must be an object with field/op/value."
        field = c.get("field")
        op = c.get("op")
        if field in _NUMERIC_FIELDS:
            # prize/teams/players → must use gte/lte with an int threshold.
            if op not in _NUMERIC_OPS:
                return None, f"Condition #{i}: field `{field}` requires op in {list(_NUMERIC_OPS)}."
            try:
                value = int(c.get("value"))
            except (TypeError, ValueError):
                return None, f"Condition #{i}: field `{field}` requires an integer `value`."
            clean.append({"field": field, "op": op, "value": value})
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
def _eval_condition(c, sample):
    """Evaluate ONE condition object against a sample dict. Returns a bool.

    sample = {"prize": int, "teams": int, "players": int, "format": "lan"|"virtual"}.
    Numeric ops compare sample[field] against the condition's value; format ops test
    sample["format"]. An unknown op or a missing sample key fails closed (returns False)
    so a malformed rule can never accidentally match everything.
    """
    field = c.get("field")
    op = c.get("op")
    if field in _NUMERIC_FIELDS:
        actual = sample.get(field)
        if actual is None:
            return False
        if op == "gte":
            return actual >= c.get("value")
        if op == "lte":
            return actual <= c.get("value")
        return False
    if field == "format":
        fmt = sample.get("format")
        if op == "is_lan":
            return fmt == "lan"
        if op == "is_virtual":
            return fmt == "virtual"
        return False
    return False


def classify(rules, default_tier, sample):
    """First-match-wins classification: returns {"tier": int, "matched_rule_id": int|None}.

    ``rules`` is an iterable of EventTierRule, expected pre-ordered by priority (the caller
    passes the priority-ordered queryset). Disabled AND RETIRED rules are skipped - a retired
    rule is kept only so the events it once classified stay explainable, it must never
    classify anything new. For each remaining rule, its conditions are combined with
    all()/any() per the rule's ``match`` ("all"=AND, "any"=OR). A rule with NO conditions
    never matches (all([]) is True, but an empty rule classifying everything would be a
    footgun - so we require at least one condition to match). The first matching rule's tier
    wins; if none match, fall through to ``default_tier``.

    NOTE on the `prize` field: the sample's value is in NAIRA and so is the rule's threshold.
    The caller converts (afc_tournament_and_scrims.views._prize_pool_ngn); this function
    compares bare numbers and cannot tell one currency from another.
    """
    for rule in rules:
        if not rule.enabled or getattr(rule, "retired_at", None) is not None:
            continue
        conditions = rule.conditions or []
        if not conditions:
            # An empty-condition rule is treated as non-matching (see docstring).
            continue
        results = (_eval_condition(c, sample) for c in conditions)
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
             "conditions":[{"field":"prize","op":"gte","value":1000000}],
             "condition_currency":{"prize":"NGN"},
             "tier":1,"enabled":true,
             "retired":false,"retired_at":null,"retired_by":null,
             "created_at":"...","updated_at":"..."}
         ],
         "pagination": {...},
         "default_tier": 3,
         "field_meta": {"event_tier_rule_prize": {"currency":"NGN", ...}},
         "contradictions": [{"kind":"unreachable_rule","path":...,"message":...,"entries":[...]}],
         "include_retired": false}

    Consumed by: the admin Tournament Tiers page (rule table, the retired filter, and the
    warning banner fed by ``contradictions``).
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
    return Response({
        "results": [serialize_tier_rule(r) for r in items],
        "pagination": meta,
        "default_tier": config.default_tier,
        "field_meta": {"event_tier_rule_prize": FIELD_META["event_tier_rule_prize"]},
        "contradictions": _contradictions(),
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
                         "value": 1000000}],        # prize values are in NAIRA
         "tier": 1 | 2 | 3,                         # default 2
         "enabled": true,                           # default true
         "reason": "at least 10 characters"}

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

    body = serialize_tier_rule(rule)
    body["contradictions"] = _contradictions()
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
         "conditions": [{"field","op","value"}],   # prize values are in NAIRA
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

    body = serialize_tier_rule(rule)
    body["contradictions"] = _contradictions()
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

    return Response({
        "message": "Tier rule retired.",
        "rule": serialize_tier_rule(rule),
        "contradictions": _contradictions(),
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

    return Response({
        "message": "Tier rule restored.",
        "rule": serialize_tier_rule(rule),
        "contradictions": _contradictions(),
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
    return Response({
        "results": [serialize_tier_rule(r) for r in qs],
        "contradictions": _contradictions(),
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
    Request:  ``{"prize": 500000, "teams": 32, "players": 0, "format": "lan"|"virtual"}``.
              ``prize`` is in NAIRA, matching the rule thresholds. A caller holding a pool in
              another currency must convert first, exactly as the real classification path
              does (afc_tournament_and_scrims.views._prize_pool_ngn).
    Response 200: ``{"tier": 1, "matched_rule_id": 3 | null, "prize_currency": "NGN"}``.
                  ``matched_rule_id`` null means nothing matched and the default tier applies.
    Response 400 when a numeric field is not a number or the format is unrecognised.

    Consumed by: the admin Tournament Tiers page's "Test a rule" panel.
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

    rules = list(EventTierRule.objects.all().order_by("priority", "created_at"))
    config = _get_config()
    result = classify(rules, config.default_tier, sample)
    # Stated so the caller cannot present the preview without saying what currency it read.
    result["prize_currency"] = "NGN"
    return Response(result)
