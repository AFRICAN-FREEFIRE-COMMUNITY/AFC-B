"""
Admin write API — tournament tier classification rules (Phase 2).

This module owns the admin CRUD + reorder surface for the ordered, first-match-wins
rule list that classifies a tournament into a tier (Tier 1/2/3), plus the singleton
fall-through default, plus a *dry-run* classifier the admin UI uses to preview which
rule a hypothetical event would hit.

Data model (see ``models.py``):
  * ``EventTierRule``   — one rule: priority (lower = evaluated first), match ("all"/"any"),
                          conditions JSON [{field, op, value}], tier (1-3), enabled.
  * ``EventTierConfig`` — singleton row holding ``default_tier`` (the fall-through when an
                          event matches no enabled rule).

Idiom (matches the rest of afc_rankings — read views.py / serializers.py / admin_views.py):
  * function-based ``@api_view`` views, NOT class-based; NO DRF Serializer classes.
  * manual-dict serialization via the LOCAL ``serialize_tier_rule`` helper below.
  * the auth + audit foundation is REUSED from ``admin_views.py`` — never reimplemented:
        user, err = _auth(request)              # 401/403 short-circuit
        reason, err = _require_reason(request)   # mandatory >= 10-char audit reason
        with transaction.atomic(): ...write...
        _audit(user, "event_tier", "<action>", reason, object_ref=..., before=..., after=...)
  * list endpoints page through ``serializers.paginate`` and return the same
    {"results": [...], "pagination": meta, ...extra} envelope views.py uses.
  * validation errors mirror afc_auth.views: ``Response({"message": "..."}, status=...)``.

object_type is fixed to "event_tier" for every audit row (one of RankingAuditLog.OBJECT_TYPES),
so the §16 audit log filters every tournament-tier change into a single bucket.

WHY no recalc enqueue here: editing a tier *rule* changes how FUTURE events are classified;
it does not mutate any already-computed TeamMonthlyScore / TeamQuarterlyScore. Re-tiering of
existing events is a separate re-evaluation pass (run-evaluation surface), so — unlike the
data-entry surfaces — these writes deliberately do NOT call ``tasks.enqueue_*``.

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

from .admin_views import _auth, _require_reason, _audit
from .models import EventTierRule, EventTierConfig
from .serializers import paginate


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
        "priority": r.priority,
        "match": r.match,
        "conditions": r.conditions,   # already a JSON list [{field, op, value}]
        "tier": r.tier,
        "enabled": r.enabled,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


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
    passes the priority-ordered queryset). Disabled rules are skipped. For each enabled rule,
    its conditions are combined with all()/any() per the rule's ``match`` ("all"=AND, "any"=OR).
    A rule with NO conditions never matches (all([]) is True, but an empty rule classifying
    everything would be a footgun — so we require at least one condition to match). The first
    matching rule's tier wins; if none match, fall through to ``default_tier``.
    """
    for rule in rules:
        if not rule.enabled:
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
    """List tier rules ordered by priority + the singleton default_tier. Read-only.

    Requires an admin token (config is internal) but no reason / no audit row.
    """
    user, err = _auth(request)
    if err:
        return err

    qs = EventTierRule.objects.all().order_by("priority", "created_at")
    items, meta = paginate(request, qs)
    config = _get_config()
    return Response({
        "results": [serialize_tier_rule(r) for r in items],
        "pagination": meta,
        "default_tier": config.default_tier,
    })


# ───────────────────────── CREATE ─────────────────────────
@api_view(["POST"])
def tier_rule_create(request):
    """Create a rule. Body: { match, conditions, tier, enabled, reason }.

    priority is assigned as (current max priority) + 1 so a new rule lands LAST in the
    evaluation order (lowest precedence) until an explicit reorder moves it.
    """
    user, err = _auth(request)
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
    enabled = bool(request.data.get("enabled", True))

    with transaction.atomic():
        # New rule sorts to the bottom of the priority order (max + 1, or 0 when empty).
        max_priority = EventTierRule.objects.order_by("-priority").values_list("priority", flat=True).first()
        next_priority = (max_priority + 1) if max_priority is not None else 0
        rule = EventTierRule.objects.create(
            priority=next_priority,
            match=match,
            conditions=conditions,
            tier=tier,
            enabled=enabled,
        )
        after = serialize_tier_rule(rule)
        # before={} — the rule did not exist prior to this write.
        _audit(user, "event_tier", "create", reason, object_ref=rule.id, before={}, after=after)

    return Response(serialize_tier_rule(rule), status=status.HTTP_201_CREATED)


# ───────────────────────── UPDATE ─────────────────────────
@api_view(["PATCH"])
def tier_rule_update(request, rule_id):
    """Partially update a rule's match / conditions / tier / enabled. Body keys are optional.

    priority is NOT editable here — use the reorder endpoint, which keeps the whole order
    consistent in one atomic pass.
    """
    user, err = _auth(request)
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

    with transaction.atomic():
        before = serialize_tier_rule(rule)
        if "match" in request.data:
            rule.match = match
        if "conditions" in request.data:
            rule.conditions = conditions
        if "tier" in request.data:
            rule.tier = tier
        if "enabled" in request.data:
            rule.enabled = bool(request.data.get("enabled"))
        rule.save()
        after = serialize_tier_rule(rule)
        _audit(user, "event_tier", "update", reason, object_ref=rule.id, before=before, after=after)

    return Response(serialize_tier_rule(rule))


# ───────────────────────── DELETE ─────────────────────────
@api_view(["DELETE"])
def tier_rule_delete(request, rule_id):
    """Delete a rule. The audit row's before-snapshot preserves the deleted rule (hand-reversible)."""
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    rule = EventTierRule.objects.filter(pk=rule_id).first()
    if not rule:
        return Response({"message": "Tier rule not found."}, status=status.HTTP_404_NOT_FOUND)

    with transaction.atomic():
        before = serialize_tier_rule(rule)
        # Capture the ref before .delete() clears the pk on the in-memory instance.
        object_ref = rule.id
        rule.delete()
        # after={} — the rule no longer exists after this write.
        _audit(user, "event_tier", "delete", reason, object_ref=object_ref, before=before, after={})

    return Response({"message": "Tier rule deleted."})


# ───────────────────────── REORDER ─────────────────────────
@api_view(["POST"])
def tier_rules_reorder(request):
    """Reassign priorities from an ordered list of rule ids. Body: { order: [rule_id, ...], reason }.

    priority is set to the rule's INDEX in ``order`` (0-based), so order[0] becomes the
    highest-precedence rule. The id set must exactly match the current rule set (no missing,
    no extra, no duplicates) — a partial reorder would leave the order ambiguous.
    """
    user, err = _auth(request)
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
    # Reject duplicates up front — a repeated id would silently overwrite a priority.
    if len(order) != len(set(order)):
        return Response(
            {"message": "`order` contains duplicate rule ids."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    existing_ids = set(EventTierRule.objects.values_list("id", flat=True))
    if set(order) != existing_ids:
        return Response(
            {"message": "`order` must list every existing rule id exactly once."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        before = {"order": list(EventTierRule.objects.order_by("priority", "created_at")
                                 .values_list("id", flat=True))}
        # priority = position in the supplied order (index 0 = top of the list = evaluated first).
        for index, rid in enumerate(order):
            EventTierRule.objects.filter(pk=rid).update(priority=index)
        after = {"order": list(order)}
        _audit(user, "event_tier", "reorder", reason, before=before, after=after)

    qs = EventTierRule.objects.all().order_by("priority", "created_at")
    return Response({"results": [serialize_tier_rule(r) for r in qs]})


# ───────────────────────── CONFIG (default tier) ─────────────────────────
@api_view(["PATCH"])
def tier_config_update(request):
    """Update the singleton fall-through default tier. Body: { default_tier, reason }."""
    user, err = _auth(request)
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

    return Response({"default_tier": config.default_tier})


# ───────────────────────── CLASSIFY (read-only dry-run) ─────────────────────────
# ── dry-run twin of the production classifier ──
# This previews the SAME first-match-wins classifier the real scoring path runs when an event is
# scored. The rule semantics — priority order, first-match-wins, all/any, empty-rule means
# non-matching — MUST stay identical to the production classifier, or the admin preview lies
# about the tier an event would land in.
@api_view(["POST"])
def tier_rules_classify(request):
    """Dry-run the SAME first-match-wins logic against a hypothetical event.

    Body: { prize, teams, players, format("lan"|"virtual") }. Returns
    {"tier": int, "matched_rule_id": int|None}. Read-only: gated by _auth, but no reason
    and no audit row — it mutates nothing, it only previews which rule would fire.
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
    return Response(result)
