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


def _season_index():
    """Every season, newest window first. One query, so a caller can attribute MANY months to
    their seasons in memory instead of one query per month (see _resolve_month's scan)."""
    return list(Season.objects.order_by("-start_date"))


def _owning_season(month, seasons):
    """The one season ``month`` belongs to, from a pre-fetched ``_season_index()`` list.

    Season windows TOUCH at the boundary (Q2 ends 1 July, the day Q3 starts), so a boundary month
    matches two rows. ``seasons`` is ordered ``-start_date``, so the LATER season is hit first and
    claims the shared day, which is where a month on the cusp actually belongs (July 2026 is Q3,
    not the tail of Q2). Returns None for a month outside every configured season.
    """
    for s in seasons:
        if s.start_date <= month <= s.end_date:
            return s
    return None


def _resolve_month(request, model=TeamMonthlyScore):
    """?month=YYYY-MM, else the newest month whose season is PUBLISHED, else the newest populated
    month, else this month.

    ``model`` is the score table the CALLING endpoint reads, so the players ladder falls back to
    the latest populated PLAYER month and the teams ladder to the latest populated TEAM month.
    It used to always read TeamMonthlyScore, which meant a month with team results but no player
    results (or vice versa) sent the other endpoint to an empty month and the ladder rendered
    blank even though its own table had data (owner 2026-08-03: "public ranking page shows no
    player rankings").

    PUBLISHED-MONTH FALLBACK (owner 2026-08-03: "it should show the past one pending when a new one
    is published"). Defaulting to the newest POPULATED month meant that the day a new quarter
    started collecting results, every default read landed on that unpublished quarter and the
    ladders went blank until an admin published it. The public surfaces (/home and the /rankings
    monthly tab) now keep showing the last PUBLISHED period instead, and flag it as not current via
    ``is_current_period`` on the envelope so nobody mistakes it for live standings.

    If nothing has ever been published we fall back to the newest populated month anyway: the gate
    in _gated_monthly then returns published=False for it, which is the genuine "no rankings yet"
    empty state and lets the response still name a sensible month.
    """
    raw = request.GET.get("month")
    if raw:
        try:
            y, m = raw.split("-")
            return datetime.date(int(y), int(m), 1)
        except (ValueError, AttributeError):
            pass
    # Newest first. Bounded scan: one row per month that has ever been scored, a handful in practice.
    months = list(model.objects.order_by("-month").values_list("month", flat=True).distinct())
    if not months:
        return datetime.date.today().replace(day=1)
    seasons = _season_index()
    for m in months:
        owner = _owning_season(m, seasons)
        if owner and owner.rankings_published:
            return m
    return months[0]


def _season_of_month(request, month):
    """The season whose date window contains ``month``, else the active season.

    WHY (owner 2026-08-03): the monthly ladders are gated on ``Season.rankings_published``, but the
    gate used to read whichever season is ACTIVE today rather than the season the requested month
    belongs to. The public season picker (frontend app/(user)/rankings) only sends ``month`` on the
    monthly endpoints, so picking a PUBLISHED past season still got judged against the CURRENT
    (unpublished) season and returned nothing — the whole ladder read as empty even though the
    scores were published. Gating on the month's own season makes the picker work.

    An explicit ``?season_id`` still wins (``_resolve_season`` honours it). Boundary handling lives
    in _owning_season; this wrapper only adds the request-level overrides.
    """
    if request.GET.get("season_id"):
        return _resolve_season(request)
    return _owning_season(month, _season_index()) or _resolve_season(request)


def _active_season():
    """The season that is live TODAY, independent of what any endpoint chose to display.

    Split out of _resolve_season so the public reads can fall back to an older PUBLISHED period
    while still telling the client which season is the current one (the envelope's
    ``current_season``), which is what lets the UI say "SEASON 3 2026 is still pending".
    """
    from .models import auto_rollover_seasons
    auto_rollover_seasons()  # calendar-driven activation (owner 2026-07-02)
    return Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()


def _resolve_season(request):
    sid = request.GET.get("season_id")
    if sid:
        s = Season.objects.filter(pk=sid).first()
        if s:
            return s
    return _active_season()


def _resolve_quarterly_season(request):
    """Season for the PUBLIC quarterly ladders, with the same last-published fallback as months.

    ``?season_id`` wins; otherwise the ACTIVE season if its rankings are published; otherwise the
    most recently STARTED season that is published (owner 2026-08-03: keep showing the past
    quarter while the new one is pending); otherwise the active season, so the genuine
    "nothing published yet" empty state can still name it.

    Deliberately SEPARATE from _resolve_season: the admin surfaces (admin_publish preview,
    admin_evaluation, admin_audit) import _resolve_season and must keep resolving to the ACTIVE
    season, since their whole job is previewing the unpublished draft.
    """
    sid = request.GET.get("season_id")
    if sid:
        s = Season.objects.filter(pk=sid).first()
        if s:
            return s
    active = _active_season()
    if active and active.rankings_published:
        return active
    published = (Season.objects.filter(rankings_published=True)
                 .order_by("-start_date").first())
    return published or active


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
def _period_meta(shown_season):
    """The two envelope keys that tell a client WHICH period it is looking at.

    ``is_current_period``  False when we are serving an older PUBLISHED period because the live
                           season's rankings are still pending (see _resolve_month). A client MUST
                           label the numbers when this is False, otherwise a viewer reads last
                           quarter's standings as today's.
    ``current_season``     The season that is live today, so the UI can name what is still pending
                           ("showing SEASON 2 2026, SEASON 3 2026 is not published yet").

    Consumed by frontend app/(user)/_components/HomeRankingsTiers.tsx (the /home card) and
    available to app/(user)/rankings/page.tsx on the same envelope.
    """
    active = _active_season()
    is_current = bool(shown_season and active and shown_season.season_id == active.season_id)
    return {"is_current_period": is_current,
            "current_season": S.season(active) if active else None}


def _gated_monthly(request, qs, serialize_fn, month):
    # Gate on the season the REQUESTED MONTH belongs to, not on whatever season happens to be
    # active today — see _season_of_month for the why.
    season = _season_of_month(request, month)
    if not (season and season.rankings_published):
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False},
                         "month": month.isoformat(),
                         "season": S.season(season) if season else None,
                         "published": False, **_period_meta(season)})
    items, meta = S.paginate(request, qs)
    return Response({"results": [serialize_fn(x) for x in items], "pagination": meta,
                     "month": month.isoformat(), "season": S.season(season), "published": True,
                     **_period_meta(season)})


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
                         "season": S.season(season), **_period_meta(season)})
    items, meta = S.paginate(request, qs)
    results = [serialize_fn(x) for x in items]
    if not season.tiers_published:
        for r in results:           # tiers are a separate gate — hide them until published
            r["tier"] = None
            r["tier_label"] = None
    return Response({"results": results, "pagination": meta, "season": S.season(season),
                     **_period_meta(season)})


@api_view(["GET"])
def teams_quarterly(request):
    # Falls back to the last PUBLISHED season while the live one is pending (owner 2026-08-03).
    season = _resolve_quarterly_season(request)
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
    # Default month comes from the PLAYER table, not the team one (see _resolve_month).
    month = _resolve_month(request, PlayerMonthlyScore)
    # Ghost players are ranked alongside real players now (rerank_player_month interleaves them).
    # select_related both sides so the serializer's _player_name reads player OR ghost_player without
    # an extra query; it emits is_ghost + a "[Ghost] <ign>" label for the FE badge.
    qs = (PlayerMonthlyScore.objects.filter(month=month)
          .select_related("player", "ghost_player").order_by("rank"))
    return _gated_monthly(request, qs, S.player_monthly, month)


@api_view(["GET"])
def players_quarterly(request):
    # Falls back to the last PUBLISHED season while the live one is pending (owner 2026-08-03).
    season = _resolve_quarterly_season(request)
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
    # Default month comes from the PLAYER table (see _resolve_month). The quarterly half of this
    # response is season-scoped, so the gate stays on the resolved season, unlike the ladders.
    month = _resolve_month(request, PlayerMonthlyScore)
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
    # Calendar-driven activation sweep (owner 2026-07-02): the admin Seasons page always shows the
    # TRUE current season without a manual edit - Q rollover applies the moment its start date hits.
    from .models import auto_rollover_seasons
    auto_rollover_seasons()
    qs = Season.objects.all().order_by("-year", "-quarter")
    return _envelope(request, qs, S.season)


@api_view(["GET"])
def season_current(request):
    from .models import auto_rollover_seasons
    auto_rollover_seasons()  # calendar-driven activation (owner 2026-07-02)
    s = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
    return Response(S.season(s) if s else None)
