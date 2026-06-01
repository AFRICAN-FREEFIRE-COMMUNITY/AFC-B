"""
Admin read-only VERIFICATION surface for rankings & tiering (Phase 2).

This is the audit / "show your work" side of the ranking admin API. Where
``admin_audit`` differs from ``admin_views`` is intent: nothing here MUTATES.
These endpoints exist so a ranking admin (or the original dev debugging a
disputed score) can answer two questions:

  1. "Who changed what, and why?"  → the §16 audit-log browser
        GET admin/audit-log/
  2. "Where did this team/player's current quarter score actually come from?"
        GET admin/teams/<team_id>/raw/
        GET admin/players/<player_id>/raw/

Because everything is read-only, the mutating-endpoint protocol in
``admin_views`` (``_require_reason`` + ``_audit``) does NOT apply — there is no
ranking state being changed, so there is nothing to reason-gate or to log. We
keep ONLY step (1): ``_auth(request)`` to confirm the caller is a ranking admin.

Style notes (kept identical to ``views.py`` / ``serializers.py`` so the original
dev reads it without surprises):
  * function-based DRF views (``@api_view(["GET"])``), never class-based.
  * manual-dict serialization via LOCAL ``serialize_*`` functions, never DRF
    Serializer classes.
  * list endpoints paginate via ``serializers.paginate`` and return the same
    envelope shape ``views.py`` uses: ``{"results": [...], "pagination": meta}``.
  * errors are message-dicts: ``Response({"message": "..."}, status=...)``.

The "raw" endpoints deliberately call the SAME aggregation helpers
(``aggregation.compute_team_quarterly`` / ``compute_player_quarterly``) that the
recalc layer uses to persist scores. So the component breakdown returned here is
exactly what produced the stored ``TeamQuarterlyScore`` / ``PlayerQuarterlyScore``
row — that is the whole point of a verification surface: it must recompute, not
re-read, so an admin can confirm the stored number is right.
"""
import datetime

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_team.models import Team
from afc_auth.models import User

from . import aggregation as A
from . import serializers as S
from . import views as V                       # reuse views._resolve_season (?season_id= or active)
from .admin_views import _auth                  # the shared auth gate — do NOT reimplement
from .models import RankingAuditLog


# ─────────────────────────── local serializers ───────────────────────────
def serialize_audit(row):
    """One RankingAuditLog row → the audit-browser dict.

    ``changed_by`` is flattened to the admin's username (the FK id is noise for a
    human-facing audit view). ``before_snapshot`` / ``after_snapshot`` are passed
    through untouched — they are already JSON blobs written by ``_audit``.
    """
    return {
        "audit_id": row.audit_id,
        "object_type": row.object_type,
        "object_ref": row.object_ref,
        "action": row.action,
        "reason": row.reason,
        "changed_by": row.changed_by.username if row.changed_by_id else None,
        "changed_at": row.changed_at.isoformat(),
        "season_id": row.season_id,
        "before_snapshot": row.before_snapshot,
        "after_snapshot": row.after_snapshot,
    }


def _serialize_team_raw(agg):
    """A TeamAgg (from aggregation.compute_team_quarterly) → component breakdown.

    Field names mirror ``serializers.team_quarterly`` so the verification view and
    the public quarterly view speak the same vocabulary. ``agg.result`` is the
    engine's frozen ``TeamQuarterlyResult``; the tiebreaker counts live on the
    ``TeamAgg`` wrapper itself.
    """
    r = agg.result
    return {
        "tournament_pts": round(r.tournament_pts, 2),
        "scrim_pts": round(r.scrim_pts, 2),
        "prize_money_pts": round(r.prize_money_pts, 2),
        "social_media_pts": round(r.social_media_pts, 2),
        "total": round(r.total, 2),
        "wins": agg.tournament_wins,
        "kills": agg.total_kills,
        "tournaments_played": agg.tournaments_played,
    }


def _serialize_player_raw(agg):
    """A PlayerAgg (from aggregation.compute_player_quarterly) → component breakdown.

    ``agg.result`` is the engine's ``PlayerQuarterlyResult``. We surface every
    component the engine breaks out (so a disputed player score is fully
    decomposable), plus the ``PlayerAgg`` tiebreaker counts (kills / mvps /
    finals / tournaments_played).
    """
    r = agg.result
    return {
        "kill_pts": round(r.kill_pts, 2),
        "placement_pts": round(r.placement_pts, 2),
        "mvp_pts": round(r.mvp_pts, 2),
        "finals_pts": round(r.finals_pts, 2),
        "team_win_pts": round(r.team_win_pts, 2),
        "participation_pts": round(r.participation_pts, 2),
        "scrim_kill_pts": round(r.scrim_kill_pts, 2),
        "scrim_win_pts": round(r.scrim_win_pts, 2),
        "prize_money_pts": round(r.prize_money_pts, 2),
        "total": round(r.total, 2),
        "kills": agg.total_kills,
        "mvps": agg.mvp_count,
        "finals_appearances": agg.finals_appearances,
        "tournaments_played": agg.tournaments_played,
    }


# ─────────────────────────── helpers ───────────────────────────
def _parse_date(raw):
    """Parse a ?date_from / ?date_to value (YYYY-MM-DD) → date, or None if absent/bad.

    Returns ``None`` for a missing OR malformed value so a typo in the query
    string degrades to "no filter" rather than a 500 — the same forgiving
    posture ``views._resolve_month`` takes with ?month=.
    """
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ─────────────────────────── audit log browser ───────────────────────────
@api_view(["GET"])
def audit_log(request):
    """Filtered, paginated view of the §16 RankingAuditLog (read-only).

    Auth-gated (ranking admin) but NOT reason/audit-gated — reading the audit log
    is itself not a ranking write.

    Query params (all optional, AND-combined):
      object_type   exact match against RankingAuditLog.OBJECT_TYPES
      changed_by    the admin's user id (User.user_id)
      season_id     rows tagged to a given season
      object_ref    exact object reference string the write recorded
      date_from     YYYY-MM-DD — rows on/after this day (inclusive)
      date_to       YYYY-MM-DD — rows up to/including this day (inclusive)

    Ordered newest-first (-changed_at). Bad/absent date filters are ignored.
    """
    user, err = _auth(request)
    if err:
        return err

    qs = RankingAuditLog.objects.select_related("changed_by").all()

    object_type = request.GET.get("object_type")
    if object_type:
        qs = qs.filter(object_type=object_type)

    changed_by = request.GET.get("changed_by")
    if changed_by:
        qs = qs.filter(changed_by_id=changed_by)

    season_id = request.GET.get("season_id")
    if season_id:
        qs = qs.filter(season_id=season_id)

    object_ref = request.GET.get("object_ref")
    if object_ref:
        qs = qs.filter(object_ref=object_ref)

    date_from = _parse_date(request.GET.get("date_from"))
    if date_from:
        # __date__gte compares the DATE part of the changed_at datetime, so a
        # caller passing a plain YYYY-MM-DD gets the whole day, not midnight only.
        qs = qs.filter(changed_at__date__gte=date_from)

    date_to = _parse_date(request.GET.get("date_to"))
    if date_to:
        qs = qs.filter(changed_at__date__lte=date_to)   # inclusive upper bound

    qs = qs.order_by("-changed_at")

    items, meta = S.paginate(request, qs)
    return Response({
        "results": [serialize_audit(row) for row in items],
        "pagination": meta,
    })


# ─────────────────────────── raw score derivation ───────────────────────────
@api_view(["GET"])
def team_raw(request, team_id):
    """Raw aggregation behind a team's CURRENT-quarter score (read-only).

    Recomputes — does not re-read — the component breakdown that produced the
    stored ``TeamQuarterlyScore`` row, so an admin can verify a disputed total.
    Season is resolved exactly like the public views: ?season_id= wins, else the
    active season. 404 if the team or the season can't be resolved.
    """
    user, err = _auth(request)
    if err:
        return err

    team = Team.objects.filter(pk=team_id).first()
    if not team:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    season = V._resolve_season(request)
    if not season:
        return Response({"message": "No active season."}, status=status.HTTP_404_NOT_FOUND)

    agg = A.compute_team_quarterly(team, season)
    return Response({
        "team_id": team.pk,
        "team_name": team.team_name,
        "season": S.season(season),
        "raw": _serialize_team_raw(agg),
    })


@api_view(["GET"])
def player_raw(request, player_id):
    """Raw aggregation behind a player's CURRENT-quarter score (read-only).

    Mirror of ``team_raw`` for the individual player track — recomputes the
    component breakdown that produced the stored ``PlayerQuarterlyScore`` row.
    404 if the player or the active season can't be resolved.
    """
    user, err = _auth(request)
    if err:
        return err

    player = User.objects.filter(pk=player_id).first()
    if not player:
        return Response({"message": "Player not found."}, status=status.HTTP_404_NOT_FOUND)

    season = V._resolve_season(request)
    if not season:
        return Response({"message": "No active season."}, status=status.HTTP_404_NOT_FOUND)

    agg = A.compute_player_quarterly(player, season)
    return Response({
        "player_id": player.pk,
        "username": player.username,
        "season": S.season(season),
        "raw": _serialize_player_raw(agg),
    })
