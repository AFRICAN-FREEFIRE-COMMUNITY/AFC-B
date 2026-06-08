"""
Public read API for rankings & tiering (Phase 1).
Admin write endpoints (seasons create, transfer-window, run-evaluation, ghost CRUD)
land in Phase 2. URL prefix: rankings/.
"""
import datetime

from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import serializers as S
from .models import (
    Season, TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
    AnnualLeaderboardEntry,
)


def _resolve_month(request):
    """?month=YYYY-MM, else latest populated month, else current month (day=1)."""
    raw = request.GET.get("month")
    if raw:
        try:
            y, m = raw.split("-")
            return datetime.date(int(y), int(m), 1)
        except (ValueError, AttributeError):
            pass
    latest = TeamMonthlyScore.objects.order_by("-month").values_list("month", flat=True).first()
    return latest or datetime.date.today().replace(day=1)


def _resolve_season(request):
    sid = request.GET.get("season_id")
    if sid:
        s = Season.objects.filter(pk=sid).first()
        if s:
            return s
    return Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()


def _envelope(request, qs, serialize_fn, extra=None):
    items, meta = S.paginate(request, qs)
    body = {"results": [serialize_fn(x) for x in items], "pagination": meta}
    if extra:
        body.update(extra)
    return Response(body)


# ───────────────────────── TEAM ─────────────────────────
# Read-only: serializes the score tables that aggregation/recalc already wrote
# (TeamMonthlyScore / TeamQuarterlyScore). This layer never computes — if a field is
# missing here, add it in aggregation first.
@api_view(["GET"])
def teams_monthly(request):
    month = _resolve_month(request)
    qs = (TeamMonthlyScore.objects.filter(month=month, team__isnull=False)
          .select_related("team").order_by("rank"))
    return _envelope(request, qs, S.team_monthly, {"month": month.isoformat()})


# Publish gates live on Season (rankings_published / tiers_published), toggled by
# admin_publish.publish_state. Admins bypass these via the draft-preview endpoints
# admin_publish.admin_teams_quarterly / admin_players_quarterly — keep in sync if the
# gate logic below changes.
def _gated_quarterly(request, season, qs, serialize_fn):
    """Public quarterly response with the two independent publish gates applied:
    nothing until ``rankings_published``; tier fields nulled until ``tiers_published``.
    Admins use the (ungated) admin preview endpoint instead — see admin_publish.py."""
    if not season.rankings_published:
        # rankings not published yet → public sees an empty, clearly-flagged result.
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False},
                         "season": S.season(season)})
    items, meta = S.paginate(request, qs)
    results = [serialize_fn(x) for x in items]
    if not season.tiers_published:
        for r in results:           # tiers are a separate gate — hide them until published
            r["tier"] = None
            r["tier_label"] = None
    return Response({"results": results, "pagination": meta, "season": S.season(season)})


@api_view(["GET"])
def teams_quarterly(request):
    season = _resolve_season(request)
    if not season:
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False}, "season": None})
    qs = (TeamQuarterlyScore.objects.filter(season=season, team__isnull=False)
          .select_related("team").order_by("rank"))
    return _gated_quarterly(request, season, qs, S.team_quarterly)


@api_view(["GET"])
def teams_annual(request):
    year = int(request.GET.get("year", datetime.date.today().year))
    qs = AnnualLeaderboardEntry.objects.filter(year=year, entity_type="team").select_related("team").order_by("rank")
    return _envelope(request, qs, S.annual, {"year": year})


# ───────────────────────── PLAYER ─────────────────────────
@api_view(["GET"])
def players_monthly(request):
    month = _resolve_month(request)
    qs = (PlayerMonthlyScore.objects.filter(month=month)
          .select_related("player").order_by("rank"))
    return _envelope(request, qs, S.player_monthly, {"month": month.isoformat()})


@api_view(["GET"])
def players_quarterly(request):
    season = _resolve_season(request)
    if not season:
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False}, "season": None})
    qs = (PlayerQuarterlyScore.objects.filter(season=season)
          .select_related("player").order_by("rank"))
    return _gated_quarterly(request, season, qs, S.player_quarterly)


@api_view(["GET"])
def players_annual(request):
    year = int(request.GET.get("year", datetime.date.today().year))
    qs = AnnualLeaderboardEntry.objects.filter(year=year, entity_type="player").select_related("player").order_by("rank")
    return _envelope(request, qs, S.annual, {"year": year})


# ───────────────────────── DETAIL ─────────────────────────
@api_view(["GET"])
def team_score_detail(request, team_id):
    month = _resolve_month(request)
    season = _resolve_season(request)
    tm = TeamMonthlyScore.objects.filter(team_id=team_id, month=month).select_related("team").first()
    tq = (TeamQuarterlyScore.objects.filter(team_id=team_id, season=season).select_related("team").first()
          if season else None)
    return Response({
        "team_id": team_id,
        "monthly": S.team_monthly(tm) if tm else None,
        "quarterly": S.team_quarterly(tq) if tq else None,
    })


@api_view(["GET"])
def player_score_detail(request, player_id):
    month = _resolve_month(request)
    season = _resolve_season(request)
    pm = PlayerMonthlyScore.objects.filter(player_id=player_id, month=month).select_related("player").first()
    pq = (PlayerQuarterlyScore.objects.filter(player_id=player_id, season=season).select_related("player").first()
          if season else None)
    return Response({
        "player_id": player_id,
        "monthly": S.player_monthly(pm) if pm else None,
        "quarterly": S.player_quarterly(pq) if pq else None,
    })


# ───────────────────────── SEASONS ─────────────────────────
@api_view(["GET"])
def seasons_list(request):
    qs = Season.objects.all().order_by("-year", "-quarter")
    return _envelope(request, qs, S.season)


@api_view(["GET"])
def season_current(request):
    s = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
    return Response(S.season(s) if s else None)
