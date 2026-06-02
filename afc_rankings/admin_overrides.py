"""
Admin manual-override write API for rankings & tiering (Phase 2).

These endpoints are the human-judgement escape hatches on top of the automatic
score engine. Five surfaces, all keyed on a quarterly score row for a given season:

  * tier override        — pin a team's tier by hand (§5 / §11), or clear the pin.
  * ban-zeroing (team)   — zero a team's quarterly score on a ban (§2.15), and undo.
  * ban-zeroing (player) — zero a player's quarterly score on a ban (§2.15, §2.11).
  * point deduction      — a partial penalty: subtract points without a full ban (§16).
  * clear deduction      — remove a partial penalty.

WHY these live apart from recalc.py: the score engine is purely *derived* — it
recomputes from raw stats and would happily wipe any manual decision. Each of these
writes sets a *sticky* field that recalc.py already knows to respect:
  - ``tier_overridden`` → recalc keeps ``tier_assigned`` instead of the projected tier.
  - ``is_zeroed``       → recalc freezes the row (the §2.15 sticky-ban guard).
  - ``points_deducted`` → recalc's ``update_or_create`` never touches it; the effective
                          ranking score the coordinator exposes is max(0, total - deducted).
So the pattern everywhere below is: flip the sticky field inside a transaction, audit it,
then enqueue a recalc on commit. The recalc re-ranks the period and honours the sticky
state — it does NOT undo the manual decision.

Idiom (matches admin_views.py / views.py / serializers.py — do not class-ify):
  * function-based ``@api_view`` views, manual-dict serialization, message-dict errors.
  * every mutation: ``_auth`` → ``_require_reason`` → ``with transaction.atomic()`` →
    write → ``_audit`` → enqueue recalc via ``transaction.on_commit`` (never inline).

The season is taken from the URL path (``seasons/<int:season_id>/...``) and looked up
directly; 404 if it doesn't exist. The (team|player, season) quarterly score row must
already exist (it is produced by the read-side recalc) — 404 if it doesn't.

URL routes (coordinator mounts these under the existing ``rankings/`` prefix):
  PATCH seasons/<season_id>/team-tier/<team_id>/        team_tier_override
  POST  seasons/<season_id>/zero-team/<team_id>/        zero_team
  POST  seasons/<season_id>/unzero-team/<team_id>/      unzero_team
  POST  seasons/<season_id>/zero-player/<player_id>/    zero_player
  POST  seasons/<season_id>/deduct-points/<team_id>/    deduct_points
  POST  seasons/<season_id>/clear-deduction/<team_id>/  clear_deduction
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.db import transaction
from django.utils import timezone

from . import recalc
from . import tasks
from .admin_views import _auth, _require_reason, _audit
from .models import Season, TeamQuarterlyScore, PlayerQuarterlyScore
from .serializers import TIER_LABELS

# Valid tier values an admin may pin a team to (§11: 0 Elite … 3 Entry). Kept local
# so the override endpoint can reject garbage input without trusting the DB layer.
VALID_TIERS = {0, 1, 2, 3}
# A ban zero forces the team to the bottom tier (Entry) per §2.15.
BAN_TIER = 3


# ───────────────────────── local serializer ─────────────────────────
def serialize_team_quarterly_admin(s):
    """Admin view of a TeamQuarterlyScore — surfaces the manual-override state the
    public ``serializers.team_quarterly`` omits (override flag, ban flag, deduction)
    plus the *effective* score the ranking actually uses after a deduction.

    effective_score mirrors the rule baked into the model docstring + recalc:
    ``max(0, total_score - points_deducted)``.
    """
    effective = max(0.0, s.total_score - s.points_deducted)
    return {
        "team_id": s.team_id,
        "team_name": s.team.team_name if s.team_id else "Unknown",
        "season_id": s.season_id,
        "total_score": round(s.total_score, 2),
        "effective_score": round(effective, 2),
        "tier": s.tier_assigned,
        "tier_label": TIER_LABELS.get(s.tier_assigned),
        "tier_overridden": s.tier_overridden,
        "tier_override_reason": s.tier_override_reason,
        "is_zeroed": s.is_zeroed,
        "zeroed_reason": s.zeroed_reason,
        "points_deducted": round(s.points_deducted, 2),
        "points_deducted_reason": s.points_deducted_reason,
        "rank": s.rank,
    }


def serialize_player_quarterly_admin(s):
    """Admin view of a PlayerQuarterlyScore — players only carry the ban flag (§2.11:
    no per-player tier override or deduction), so this surface is intentionally smaller."""
    return {
        "player_id": s.player_id,
        "username": s.player.username,
        "season_id": s.season_id,
        "total_score": round(s.total_score, 2),
        "tier": s.tier_assigned,
        "tier_label": TIER_LABELS.get(s.tier_assigned),
        "is_zeroed": s.is_zeroed,
        "zeroed_reason": s.zeroed_reason,
        "rank": s.rank,
    }


# ───────────────────────── shared lookups ─────────────────────────
def _get_season(season_id):
    """Resolve the season from the URL path PK, or return (None, 404 Response)."""
    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return None, Response(
            {"message": "Season not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return season, None


def _get_team_score(team_id, season):
    """Resolve the TeamQuarterlyScore row for (team, season), or return (None, 404).

    The row must already exist — it is created by the derived recalc, never here.
    select_related("team") so the serializer can read team_name without an extra query.
    """
    score = (TeamQuarterlyScore.objects
             .select_related("team")
             .filter(team_id=team_id, season=season).first())
    if not score:
        return None, Response(
            {"message": "No quarterly score exists for this team in this season."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return score, None


def _get_player_score(player_id, season):
    """Resolve the PlayerQuarterlyScore row for (player, season), or return (None, 404)."""
    score = (PlayerQuarterlyScore.objects
             .select_related("player")
             .filter(player_id=player_id, season=season).first())
    if not score:
        return None, Response(
            {"message": "No quarterly score exists for this player in this season."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return score, None


# ───────────────────────── TIER OVERRIDE (team) ─────────────────────────
# ── sticky-field contract with recalc.py ──
# The flags written by the endpoints below (tier_overridden / is_zeroed / points_deducted)
# are EXACTLY the fields recalc.py respects and never overwrites. This surface only flips
# the flag; recalc re-ranks around it. effective_score = max(0, total_score - points_deducted),
# mirrored in serializers.team_quarterly (and serialize_team_quarterly_admin above).
@api_view(["PATCH"])
def team_tier_override(request, season_id, team_id):
    """Pin (or clear) a team's tier by hand for a season (§5 / §11).

    Body: ``{"tier": <0-3>, "reason": "..."}``.

    Clearing rule (per spec): if the requested tier equals the tier the team currently
    holds (its computed/assigned tier *before* this override is applied), we treat the
    request as "no manual deviation needed" and CLEAR the override flag — letting recalc
    resume projecting the tier automatically. Otherwise we lock the requested tier and
    set ``tier_overridden=True`` so recalc's guard (recalc.py §83) keeps it.

    Either way we audit ``tier_override`` and enqueue a team recalc on commit; recalc
    re-ranks and respects whatever sticky state we just wrote.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season(season_id)
    if err:
        return err

    # Validate the requested tier before touching anything.
    raw_tier = request.data.get("tier")
    try:
        tier = int(raw_tier)
    except (TypeError, ValueError):
        return Response(
            {"message": "A 'tier' (integer 0-3) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if tier not in VALID_TIERS:
        return Response(
            {"message": "Invalid tier — must be one of 0 (Elite), 1 (Competitive), 2 (Rising), 3 (Entry)."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    score, err = _get_team_score(team_id, season)
    if err:
        return err

    with transaction.atomic():
        before = {
            "tier_assigned": score.tier_assigned,
            "tier_overridden": score.tier_overridden,
            "tier_override_reason": score.tier_override_reason,
        }
        # Compare against the tier the team holds *now* (its computed tier before override).
        # Requesting that same tier means "no manual deviation" → clear the override.
        clearing = (tier == score.tier_assigned)
        if clearing:
            score.tier_overridden = False
            score.tier_override_reason = ""
            # leave tier_assigned as-is; recalc will project it fresh from now on.
        else:
            score.tier_assigned = tier
            score.tier_overridden = True
            score.tier_override_reason = reason
            score.tier_assigned_at = timezone.now()
        score.save(update_fields=[
            "tier_assigned", "tier_overridden", "tier_override_reason", "tier_assigned_at",
        ])
        after = {
            "tier_assigned": score.tier_assigned,
            "tier_overridden": score.tier_overridden,
            "tier_override_reason": score.tier_override_reason,
        }
        _audit(
            user, "tier_override", "clear" if clearing else "override", reason,
            object_ref=f"team:{team_id}:season:{season.season_id}",
            before=before, after=after, season=season,
        )
        # Enqueue AFTER commit — recalc re-ranks the quarter and honours the override guard.
        transaction.on_commit(
            lambda: tasks.enqueue_team(team_id, recalc.current_month(), season.season_id)
        )

    return Response(serialize_team_quarterly_admin(score))


# ───────────────────────── BAN-ZERO (team) ─────────────────────────
@api_view(["POST"])
def zero_team(request, season_id, team_id):
    """Zero a team's quarterly score on a ban (§2.15).

    Forces ``total_score=0``, ``tier_assigned=Entry``, ``is_zeroed=True`` and records the
    reason. recalc's sticky-ban guard (recalc.py §70) then FREEZES the row on every
    subsequent recalc until an admin explicitly un-zeros it. Audited as ``ban_zeroing``.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season(season_id)
    if err:
        return err
    score, err = _get_team_score(team_id, season)
    if err:
        return err

    with transaction.atomic():
        before = {
            "total_score": score.total_score,
            "tier_assigned": score.tier_assigned,
            "is_zeroed": score.is_zeroed,
            "zeroed_reason": score.zeroed_reason,
        }
        score.is_zeroed = True
        score.zeroed_reason = reason
        score.total_score = 0
        score.tier_assigned = BAN_TIER
        score.save(update_fields=["is_zeroed", "zeroed_reason", "total_score", "tier_assigned"])
        after = {
            "total_score": score.total_score,
            "tier_assigned": score.tier_assigned,
            "is_zeroed": score.is_zeroed,
            "zeroed_reason": score.zeroed_reason,
        }
        _audit(
            user, "ban_zeroing", "zero", reason,
            object_ref=f"team:{team_id}:season:{season.season_id}",
            before=before, after=after, season=season,
        )
        # Recalc only re-ranks here (the sticky guard keeps the row zeroed).
        transaction.on_commit(
            lambda: tasks.enqueue_team(team_id, recalc.current_month(), season.season_id)
        )

    return Response(serialize_team_quarterly_admin(score))


@api_view(["POST"])
def unzero_team(request, season_id, team_id):
    """Lift a team's ban-zero (§2.15) — clears ``is_zeroed`` + the reason so the next
    recalc recomputes the score and tier fresh. Audited as ``ban_zeroing`` / ``unzero``.

    Note we do NOT restore the old score here; clearing the flag and triggering a recalc
    is the correct, idempotent way to get an accurate fresh score from current stats.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season(season_id)
    if err:
        return err
    score, err = _get_team_score(team_id, season)
    if err:
        return err

    with transaction.atomic():
        before = {"is_zeroed": score.is_zeroed, "zeroed_reason": score.zeroed_reason}
        score.is_zeroed = False
        score.zeroed_reason = ""
        score.save(update_fields=["is_zeroed", "zeroed_reason"])
        after = {"is_zeroed": score.is_zeroed, "zeroed_reason": score.zeroed_reason}
        _audit(
            user, "ban_zeroing", "unzero", reason,
            object_ref=f"team:{team_id}:season:{season.season_id}",
            before=before, after=after, season=season,
        )
        # Now that the guard is cleared, the recalc recomputes score + tier from scratch.
        transaction.on_commit(
            lambda: tasks.enqueue_team(team_id, recalc.current_month(), season.season_id)
        )

    return Response(serialize_team_quarterly_admin(score))


# ───────────────────────── BAN-ZERO (player) ─────────────────────────
@api_view(["POST"])
def zero_player(request, season_id, player_id):
    """Zero a player's quarterly score on a ban (§2.15). Players have no tier override or
    deduction (§2.11), so the ban flag is the only sticky state. recalc's player sticky-ban
    guard (recalc.py §157) freezes the row until an admin un-zeros it elsewhere.
    Audited as ``ban_zeroing`` / ``zero``.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season(season_id)
    if err:
        return err
    score, err = _get_player_score(player_id, season)
    if err:
        return err

    with transaction.atomic():
        before = {
            "total_score": score.total_score,
            "is_zeroed": score.is_zeroed,
            "zeroed_reason": score.zeroed_reason,
        }
        score.is_zeroed = True
        score.zeroed_reason = reason
        score.total_score = 0
        score.save(update_fields=["is_zeroed", "zeroed_reason", "total_score"])
        after = {
            "total_score": score.total_score,
            "is_zeroed": score.is_zeroed,
            "zeroed_reason": score.zeroed_reason,
        }
        _audit(
            user, "ban_zeroing", "zero", reason,
            object_ref=f"player:{player_id}:season:{season.season_id}",
            before=before, after=after, season=season,
        )
        transaction.on_commit(
            lambda: tasks.enqueue_player(player_id, recalc.current_month(), season.season_id)
        )

    return Response(serialize_player_quarterly_admin(score))


# ───────────────────────── POINT DEDUCTION (team) ─────────────────────────
@api_view(["POST"])
def deduct_points(request, season_id, team_id):
    """Apply a manual partial penalty to a team (§16).

    Body: ``{"points": <int >= 1>, "reason": "..."}``.

    ACCUMULATES the deduction (``points_deducted += points``) so stacking penalties add up,
    and stamps the latest reason. ``points_deducted`` is sticky across recalc (recalc's
    ``update_or_create`` never writes it), so the penalty persists until cleared. The
    coordinator's rerank/serializer subtracts it: effective = max(0, total - deducted).
    We still enqueue a recalc so the effective-score re-rank picks up the change.
    Audited as ``point_deduction`` / ``deduct``.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season(season_id)
    if err:
        return err

    # Validate the points amount (integer >= 1).
    raw_points = request.data.get("points")
    try:
        points = int(raw_points)
    except (TypeError, ValueError):
        return Response(
            {"message": "A 'points' value (integer >= 1) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if points < 1:
        return Response(
            {"message": "Deduction must be at least 1 point."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    score, err = _get_team_score(team_id, season)
    if err:
        return err

    with transaction.atomic():
        before = {
            "points_deducted": score.points_deducted,
            "points_deducted_reason": score.points_deducted_reason,
        }
        score.points_deducted = score.points_deducted + points  # accumulate (stack penalties)
        score.points_deducted_reason = reason
        score.save(update_fields=["points_deducted", "points_deducted_reason"])
        after = {
            "points_deducted": score.points_deducted,
            "points_deducted_reason": score.points_deducted_reason,
        }
        _audit(
            user, "point_deduction", "deduct", reason,
            object_ref=f"team:{team_id}:season:{season.season_id}",
            before=before, after=after, season=season,
        )
        # Recalc re-ranks on the effective score (total - deducted); the deduction is sticky.
        transaction.on_commit(
            lambda: tasks.enqueue_team(team_id, recalc.current_month(), season.season_id)
        )

    return Response(serialize_team_quarterly_admin(score))


@api_view(["POST"])
def clear_deduction(request, season_id, team_id):
    """Remove a team's manual point penalty (§16) — resets ``points_deducted`` to 0 and
    clears the reason, restoring the full derived score. Audited as ``point_deduction`` /
    ``clear``, then a recalc re-ranks without the penalty.
    """
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season(season_id)
    if err:
        return err
    score, err = _get_team_score(team_id, season)
    if err:
        return err

    with transaction.atomic():
        before = {
            "points_deducted": score.points_deducted,
            "points_deducted_reason": score.points_deducted_reason,
        }
        score.points_deducted = 0
        score.points_deducted_reason = ""
        score.save(update_fields=["points_deducted", "points_deducted_reason"])
        after = {
            "points_deducted": score.points_deducted,
            "points_deducted_reason": score.points_deducted_reason,
        }
        _audit(
            user, "point_deduction", "clear", reason,
            object_ref=f"team:{team_id}:season:{season.season_id}",
            before=before, after=after, season=season,
        )
        transaction.on_commit(
            lambda: tasks.enqueue_team(team_id, recalc.current_month(), season.season_id)
        )

    return Response(serialize_team_quarterly_admin(score))
