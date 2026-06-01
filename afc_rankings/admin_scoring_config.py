"""
Admin write API for rankings & tiering (Phase 2) — Scoring Config surface.

Versioned, admin-editable snapshot of the scoring rule set. The engine hardcodes
its scales in ``scoring/constants.py``; this surface lets a ranking admin draft a
NEW immutable version of those scales (stored as one JSON blob on
``ScoringConfig``) and activate it. History is append-only: each save bumps the
version and deactivates every prior row, so the active config is always the
newest. When no config row exists, the engine (and this surface's GET) fall back
to the ``constants.py`` defaults via ``_defaults_snapshot()``.

This module follows the same idiom as ``views.py`` / ``admin_views.py`` so the
original dev reads it without surprises:
  * function-based DRF views (``@api_view``), not class-based.
  * the auth + audit FOUNDATION from ``admin_views.py`` is REUSED, never
    reimplemented: ``_auth`` (role gate), ``_require_reason`` (mandatory audit
    reason), ``_audit`` (one RankingAuditLog row per write).
  * manual-dict serialization (see ``serializers.py``), no DRF Serializer classes.

Endpoints (mounted by the coordinator under the existing ``rankings/`` prefix):
  GET  scoring-config/           → active config (or defaults snapshot) + version list
  GET  scoring-config/defaults/  → just the constants.py defaults snapshot
  POST scoring-config/           → draft + activate a new version (head_admin / metrics_admin)

NOTE on recalc: editing the scoring rules changes how EVERY score is computed, so
activating a new config implies a GLOBAL recalc of all teams/players — not the
per-entity ``enqueue_team`` / ``enqueue_player`` used by data-entry surfaces. That
global sweep is the coordinator's concern; this module deliberately does NOT
enqueue anything.
# TODO(recalc): global recalc on activate
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.db import transaction
from django.db.models import Max

from .admin_views import _auth, _require_reason, _audit
from .models import ScoringConfig
from .scoring import constants as C


# ───────────────────────── defaults snapshot ─────────────────────────
def _defaults_snapshot():
    """Serialise the immutable ``constants.py`` scoring tables into the SAME JSON
    shape the frontend edits / a saved ``ScoringConfig.config`` blob carries.

    This is the canonical "factory reset" of the scoring rules: it is returned
    when no config row is active, and it seeds the editor on the
    ``scoring-config/defaults/`` route. Bracket tables (``(upper_bound, value)``
    tuples, where ``upper_bound`` is ``None`` for the open-ended top band) are
    emitted as ``[{"max": <int|null>, "points": <int>}]`` lists so JSON has no
    tuple/None ambiguity and the editor can render them as rows.

    Keys mirror the constants.py section names so a reviewer can diff the two
    one-to-one.
    """

    def _brackets(table):
        # (upper_bound_inclusive | None, value) → [{"max": int|None, "points": value}]
        return [{"max": upper, "points": value} for (upper, value) in table]

    return {
        # §4 — tier scoring multipliers (placement, kills, finals only)
        "tier_multiplier": dict(C.TIER_MULTIPLIER),
        # §4.1 — placement points per match finish (keys coerced to str for JSON object keys)
        "placement_points": {str(finish): pts for finish, pts in C.PLACEMENT_POINTS.items()},
        # §4.2 / §4.3 — cumulative compression scales
        "kill_compression": _brackets(C.KILL_COMPRESSION),
        "placement_compression": _brackets(C.PLACEMENT_COMPRESSION),
        # §4.4 — flat win bonus per tier (not compressed, not multiplied)
        "win_bonus": dict(C.WIN_BONUS),
        # §4.5 — finals appearance bonus base (finals_bonus = base * tier_multiplier)
        "finals_base": C.FINALS_BASE,
        # §7.2 / §7.3 — quarterly tiering brackets
        "prize_money_points": _brackets(C.PRIZE_MONEY_POINTS),
        "social_media_points": _brackets(C.SOCIAL_MEDIA_POINTS),
        # §11 — tier cutoffs. (min_score_inclusive, tier_int) + the default (Entry).
        "tier_thresholds": {
            "brackets": [{"min": min_score, "tier": tier} for (min_score, tier) in C.TIER_THRESHOLDS],
            "default_tier": C.TIER_DEFAULT,
            "labels": {str(k): v for k, v in C.TIER_LABELS.items()},
        },
        # §6 / §12 — scrim rules
        "scrim": {
            "weight": C.SCRIM_WEIGHT,             # placement & kill weight (0.5x tournament)
            "win_flat": C.SCRIM_WIN_FLAT,         # flat points per scrim win
            "cap_ratio": C.SCRIM_CAP_RATIO,       # max scrim contribution as a share of tournament total
            "daily_cap": C.SCRIM_DAILY_CAP,       # max scrims/day counted (enforced upstream)
            "monthly_cap": C.SCRIM_MONTHLY_CAP,   # max scrims/month counted (enforced upstream)
        },
        # §7 — player ranking flat weights
        "player_weights": {
            "mvp_pts": C.PLAYER_MVP_PTS,
            "finals_pts": C.PLAYER_FINALS_PTS,
            "team_win_pts": C.PLAYER_TEAM_WIN_PTS,
            "participation_pts": C.PLAYER_PARTICIPATION_PTS,
            "scrim_win_pts": C.PLAYER_SCRIM_WIN_PTS,
            "scrim_kill_weight": C.PLAYER_SCRIM_KILL_WEIGHT,
        },
    }


# ───────────────────────── local serializer ─────────────────────────
def serialize_scoring_config(cfg):
    """Manual-dict serialization of one ``ScoringConfig`` row (matches serializers.py)."""
    return {
        "id": cfg.id,
        "version": cfg.version,
        "is_active": cfg.is_active,
        "config": cfg.config,
        "note": cfg.note,
        # created_by is nullable (SET_NULL) — guard the username lookup.
        "created_by": cfg.created_by.username if cfg.created_by_id else None,
        "created_at": cfg.created_at.isoformat(),
    }


def _version_row(cfg):
    """Lightweight row for the version-history list (no full config blob)."""
    return {
        "id": cfg.id,
        "version": cfg.version,
        "is_active": cfg.is_active,
        "note": cfg.note,
        "created_by": cfg.created_by.username if cfg.created_by_id else None,
        "created_at": cfg.created_at.isoformat(),
    }


# ───────────────────────── endpoints ─────────────────────────
@api_view(["GET"])
def scoring_config(request):
    """Return the ACTIVE scoring config + the version history list.

    Read-only → ``_auth`` gate only (no reason, no audit). When no config row is
    active, ``config`` is a ``_defaults_snapshot()`` and ``is_default`` is True so
    the frontend knows it is showing factory defaults rather than a saved version.
    """
    user, err = _auth(request)
    if err:
        return err

    active = ScoringConfig.objects.filter(is_active=True).order_by("-version").first()
    # Version history (newest first) — list is small, no pagination needed.
    versions = [_version_row(c) for c in ScoringConfig.objects.all().order_by("-version")]

    if active:
        body = serialize_scoring_config(active)
        body["is_default"] = False
    else:
        # No saved config yet → surface the constants.py defaults as the "current" config.
        body = {
            "id": None,
            "version": None,
            "is_active": False,
            "config": _defaults_snapshot(),
            "note": "",
            "created_by": None,
            "created_at": None,
            "is_default": True,
        }

    body["versions"] = versions
    return Response(body)


@api_view(["GET"])
def scoring_config_defaults(request):
    """Return just the constants.py defaults snapshot (the "factory reset" payload).

    Read-only → ``_auth`` gate only. Used by the editor's "Reset to defaults" action.
    """
    user, err = _auth(request)
    if err:
        return err
    return Response({"config": _defaults_snapshot()})


@api_view(["POST"])
def scoring_config_save(request):
    """Draft + activate a NEW scoring-config version.

    Body: ``{ "config": {...full snapshot...}, "reason": "<>=10 chars>" }``.
    The new version = ``max(version) + 1`` (or 1 for the first row). Every prior
    row is deactivated and the new row is created ``is_active=True`` with
    ``created_by=user`` and ``note=reason``. Auth: head_admin OR metrics_admin
    (the default ``_auth`` set).

    The version bump + deactivate-others + create all run inside one
    ``transaction.atomic()`` so there is never more than one active row, then a
    single ``_audit`` row records the save (object_type="scoring_config",
    action="save").
    """
    # (1) auth gate — default roles = head_admin / metrics_admin.
    user, err = _auth(request)
    if err:
        return err

    # (2) mandatory audit reason (also reused as the row's `note`).
    reason, err = _require_reason(request)
    if err:
        return err

    # Validate the config payload: must be a non-empty JSON object.
    config = request.data.get("config")
    if not isinstance(config, dict) or not config:
        return Response(
            {"message": "A 'config' object is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # (3) the write, atomic so "deactivate all + create active" can't half-apply.
    with transaction.atomic():
        # Snapshot the prior active version for the audit trail (before-state).
        prev_active = (
            ScoringConfig.objects.select_for_update()
            .filter(is_active=True)
            .order_by("-version")
            .first()
        )
        before = (
            {"version": prev_active.version, "note": prev_active.note}
            if prev_active
            else {"version": None, "note": "(defaults)"}
        )

        # Next version number — Max over all rows (None on first save → start at 1).
        next_version = (ScoringConfig.objects.aggregate(m=Max("version"))["m"] or 0) + 1

        # Deactivate every existing row, then create the new active one.
        ScoringConfig.objects.filter(is_active=True).update(is_active=False)
        new_cfg = ScoringConfig.objects.create(
            version=next_version,
            is_active=True,
            config=config,
            note=reason,
            created_by=user,
        )

        after = {"version": new_cfg.version, "note": new_cfg.note}

        # (4) audit — one row per write. Not season-scoped (config is global), so season=None.
        _audit(
            user, "scoring_config", "save", reason,
            object_ref=new_cfg.id, before=before, after=after,
        )

    # TODO(recalc): global recalc on activate — every score must be recomputed
    # against the new scales. Left to the coordinator (not a per-entity enqueue).

    return Response(serialize_scoring_config(new_cfg), status=status.HTTP_201_CREATED)
