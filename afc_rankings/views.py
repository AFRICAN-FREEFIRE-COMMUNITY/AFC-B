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
# Monthly standings are a LIVE snapshot, but (owner 2026-06-16) they must NOT be public until an
# admin publishes the season's rankings — same gate the quarterly endpoints already enforce, so the
# public never sees unpublished/auto-computed numbers. There is no tier at the monthly level, so the
# single rankings_published gate is all that applies (tiers_published only matters for quarterly).
# Admins still see the ungated draft via the admin preview endpoints (admin_publish.py).
def _gated_monthly(request, qs, serialize_fn, month):
    season = _resolve_season(request)
    if not (season and season.rankings_published):
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False},
                         "month": month.isoformat(),
                         "season": S.season(season) if season else None,
                         "published": False})
    items, meta = S.paginate(request, qs)
    return Response({"results": [serialize_fn(x) for x in items], "pagination": meta,
                     "month": month.isoformat(), "season": S.season(season), "published": True})


@api_view(["GET"])
def teams_monthly(request):
    month = _resolve_month(request)
    # Ghost teams are first-class here now: drop the team__isnull=False filter so ghost rows (ranked
    # alongside real teams by rerank_team_month) are returned too. select_related both sides so the
    # serializer's _team_name reads team OR ghost_team without an extra query. The serializer emits
    # is_ghost + a "[Ghost] <name>" label so the FE can badge the row.
    qs = (TeamMonthlyScore.objects.filter(month=month)
          .select_related("team", "ghost_team").order_by("rank"))
    return _gated_monthly(request, qs, S.team_monthly, month)


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
    # Ghost teams are ranked + tiered alongside real teams now (see teams_monthly note). Drop the
    # team__isnull=False filter; select_related both sides for the serializer's _team_name.
    qs = (TeamQuarterlyScore.objects.filter(season=season)
          .select_related("team", "ghost_team").order_by("rank"))
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
    # Ghost players are ranked alongside real players now (rerank_player_month interleaves them).
    # select_related both sides so the serializer's _player_name reads player OR ghost_player without
    # an extra query; it emits is_ghost + a "[Ghost] <ign>" label for the FE badge.
    qs = (PlayerMonthlyScore.objects.filter(month=month)
          .select_related("player", "ghost_player").order_by("rank"))
    return _gated_monthly(request, qs, S.player_monthly, month)


@api_view(["GET"])
def players_quarterly(request):
    season = _resolve_season(request)
    if not season:
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False}, "season": None})
    # Ghost players are ranked + tiered alongside real players now (see players_monthly note).
    qs = (PlayerQuarterlyScore.objects.filter(season=season)
          .select_related("player", "ghost_player").order_by("rank"))
    return _gated_quarterly(request, season, qs, S.player_quarterly)


@api_view(["GET"])
def players_annual(request):
    year = int(request.GET.get("year", datetime.date.today().year))
    qs = AnnualLeaderboardEntry.objects.filter(year=year, entity_type="player").select_related("player").order_by("rank")
    return _envelope(request, qs, S.annual, {"year": year})


# ───────────────────────── DETAIL ─────────────────────────
# Detail drill-downs obey the SAME publish gates as the ladders (owner 2026-06-16): nothing until
# rankings_published, and the quarterly tier stays hidden until tiers_published. Without this, a
# public client could read a team/player's unpublished score straight from the detail route.
@api_view(["GET"])
def team_score_detail(request, team_id):
    month = _resolve_month(request)
    season = _resolve_season(request)
    if not (season and season.rankings_published):
        return Response({"team_id": team_id, "monthly": None, "quarterly": None, "published": False})
    tm = TeamMonthlyScore.objects.filter(team_id=team_id, month=month).select_related("team").first()
    tq = (TeamQuarterlyScore.objects.filter(team_id=team_id, season=season).select_related("team").first())
    q = S.team_quarterly(tq) if tq else None
    if q and not season.tiers_published:
        q["tier"] = None
        q["tier_label"] = None
    return Response({
        "team_id": team_id,
        "monthly": S.team_monthly(tm) if tm else None,
        "quarterly": q,
        "published": True,
    })


@api_view(["GET"])
def player_score_detail(request, player_id):
    month = _resolve_month(request)
    season = _resolve_season(request)
    if not (season and season.rankings_published):
        return Response({"player_id": player_id, "monthly": None, "quarterly": None, "published": False})
    pm = PlayerMonthlyScore.objects.filter(player_id=player_id, month=month).select_related("player").first()
    pq = (PlayerQuarterlyScore.objects.filter(player_id=player_id, season=season).select_related("player").first())
    q = S.player_quarterly(pq) if pq else None
    if q and not season.tiers_published:
        q["tier"] = None
        q["tier_label"] = None
    return Response({
        "player_id": player_id,
        "monthly": S.player_monthly(pm) if pm else None,
        "quarterly": q,
        "published": True,
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
