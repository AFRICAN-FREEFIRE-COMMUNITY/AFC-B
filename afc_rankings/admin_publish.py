"""
Admin publish controls + draft preview (Phase 2c).

The public quarterly endpoints (views.py) hide a season's rankings until
``rankings_published`` and its tiers until ``tiers_published``. Admins manage those flags
here, AND read the UNGATED draft (the full computed data, including not-yet-published
tiers) so they can preview before publishing. Rankings and tiers publish independently.

Same idiom as the other admin modules: function-based @api_view, manual-dict serializers,
the ``admin_views`` auth/reason/audit spine.
"""
import datetime

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import views as V
from . import serializers as S
from .admin_views import _auth, _require_reason, _audit
from .models import (
    Season,
    TeamQuarterlyScore, PlayerQuarterlyScore,
    TeamMonthlyScore, PlayerMonthlyScore,
)


# ───────────────────────── PATCH seasons/<id>/publish/  (publish flags) ─────────────────────────
# These two flags ARE the gate the public views.py enforces: ``_gated_quarterly`` hides a
# season's rankings until ``rankings_published`` and nulls out each row's tier until
# ``tiers_published``. The admin preview endpoints below (admin_teams_quarterly /
# admin_players_quarterly) deliberately BYPASS that gate so an admin can see the full
# computed draft - incl. unpublished tiers - before flipping these flags.
@api_view(["PATCH"])
def publish_state(request, season_id):
    """Set the rankings / tiers publish flags for a season.

    Body may include ``rankings_published`` and/or ``tiers_published`` (bool) - only the keys
    present are changed, so rankings and tiers publish/unpublish independently. ``reason``
    (>=10 chars) is mandatory and goes to the audit log.
    """
    # Gate order matches the sibling admin files (admin_seasons.py): (1) auth, (2) reason,
    # (3) Season lookup/404 - so a real write never half-runs the reason check after the lookup.
    user, err = _auth(request)
    if err:
        return err
    reason, err = _require_reason(request)
    if err:
        return err
    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)

    before = {"rankings_published": season.rankings_published, "tiers_published": season.tiers_published}
    changed = []
    now = timezone.now()
    if "rankings_published" in request.data:
        season.rankings_published = bool(request.data["rankings_published"])
        season.rankings_published_at = now if season.rankings_published else None
        season.rankings_published_by = user if season.rankings_published else None
        changed += ["rankings_published", "rankings_published_at", "rankings_published_by"]
    if "tiers_published" in request.data:
        season.tiers_published = bool(request.data["tiers_published"])
        season.tiers_published_at = now if season.tiers_published else None
        season.tiers_published_by = user if season.tiers_published else None
        changed += ["tiers_published", "tiers_published_at", "tiers_published_by"]
    if not changed:
        return Response({"message": "Provide rankings_published and/or tiers_published."},
                        status=status.HTTP_400_BAD_REQUEST)

    season.save(update_fields=changed)
    after = {"rankings_published": season.rankings_published, "tiers_published": season.tiers_published}
    _audit(user, "season", "publish", reason, object_ref=f"season:{season.season_id}",
           before=before, after=after, season=season)
    return Response(S.season(season))


# ───────────────────────── GET admin teams quarterly  (ungated draft preview) ─────────────────────────
@api_view(["GET"])
def admin_teams_quarterly(request):
    """Ungated draft of team quarterly scores (full data incl. unpublished tiers + the
    admin override fields) so admins can preview rankings + tiers before publishing."""
    user, err = _auth(request)
    if err:
        return err
    season = V._resolve_season(request)
    if not season:
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False}, "season": None})
    # Ghost teams are ranked + tiered alongside real teams now, so the admin draft must show them
    # too (drop team__isnull=False; select_related both sides for the serializer's _team_name).
    qs = (TeamQuarterlyScore.objects.filter(season=season)
          .select_related("team", "ghost_team").order_by("rank"))
    items, meta = S.paginate(request, qs)
    return Response({"results": [S.team_quarterly(x) for x in items], "pagination": meta,
                     "season": S.season(season)})


# ───────────────────────── GET admin players quarterly  (ungated draft preview) ─────────────────────────
@api_view(["GET"])
def admin_players_quarterly(request):
    """Ungated draft of player quarterly scores for admin preview."""
    user, err = _auth(request)
    if err:
        return err
    season = V._resolve_season(request)
    if not season:
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False}, "season": None})
    # Ghost players are ranked + tiered alongside real players now (select_related both sides for
    # the serializer's _player_name).
    qs = (PlayerQuarterlyScore.objects.filter(season=season)
          .select_related("player", "ghost_player").order_by("rank"))
    items, meta = S.paginate(request, qs)
    return Response({"results": [S.player_quarterly(x) for x in items], "pagination": meta,
                     "season": S.season(season)})


# ═════════════════ admin MONTHLY ladders (ungated preview) ═════════════════
# WHY these exist (owner 2026-08-03: "there is no page or place for rankings on the admin ranking
# and tiering page"). The two public monthly endpoints (views.teams_monthly / views.players_monthly)
# do two things that are right for the public and wrong for an admin:
#   1. they return NOTHING until the month's season is published, and
#   2. they fall back to the last PUBLISHED month while the live one is pending.
# An admin is asked to publish a ladder, so they have to be able to look at the LIVE numbers first,
# published or not. These two views are the monthly twin of admin_teams_quarterly /
# admin_players_quarterly above: identical envelope, no gate.
def _admin_month(request, model):
    """The month an ADMIN ladder should show: ``?month=YYYY-MM``, else the newest month that has
    rows in ``model``, else the current calendar month.

    Deliberately NOT ``views._resolve_month``: that one prefers the newest PUBLISHED month so the
    public keeps seeing last quarter while the live one is pending. Admin preview wants the newest
    REAL month instead. ``model`` is the caller's own score table (TeamMonthlyScore /
    PlayerMonthlyScore) so the teams ladder never defaults to a month only the players table has
    populated, and vice versa (the same per-table rule _resolve_month follows).
    """
    raw = request.GET.get("month")
    if raw:
        try:
            y, m = raw.split("-")
            return datetime.date(int(y), int(m), 1)
        except (ValueError, AttributeError):
            pass
    newest = model.objects.order_by("-month").values_list("month", flat=True).first()
    return newest or datetime.date.today().replace(day=1)


def _admin_monthly_response(request, qs, serialize_fn, month):
    """Shared envelope for the two admin monthly ladders below.

    Keys match the PUBLIC monthly envelope (views._gated_monthly) so the frontend reuses one
    Envelope type, with one difference in MEANING that callers must respect: ``published`` here
    only REPORTS whether the period is live to the public, it never withholds rows. The admin UI
    uses it to badge the ladder as a preview (see app/(a)/a/rankings/ladders).
    ``season`` is the season the MONTH belongs to (views._season_of_month), not whichever season is
    active today, so the publish badge describes the period actually on screen.
    """
    season = V._season_of_month(request, month)
    items, meta = S.paginate(request, qs)
    return Response({
        "results": [serialize_fn(x) for x in items],
        "pagination": meta,
        "month": month.isoformat(),
        # S.season carries rankings_published + tiers_published, which is what the UI badges.
        "season": S.season(season) if season else None,
        "published": bool(season and season.rankings_published),
    })


# ───────────────────────── GET admin teams monthly  (ungated draft preview) ─────────────────────────
@api_view(["GET"])
def admin_teams_monthly(request):
    """Ungated draft of the TEAM monthly ladder, for admin preview before publishing.

    Purpose  the monthly half of the admin Ladders view; the public twin (views.teams_monthly)
             hides these rows until the month's season is published.
    Request  GET /rankings/admin/teams/monthly/?month=YYYY-MM&limit=&offset=
             ``month`` optional, defaults to the newest populated TEAM month (see _admin_month).
    Response {results: [serializers.team_monthly], pagination, month, season, published}
             ``published`` = is this period live to the public (a flag, not a gate).
    Auth     Authorization: Bearer <SessionToken>, head_admin | metrics_admin (admin_views._auth).
    Consumed by frontend app/(a)/a/rankings/ladders/page.tsx via
             lib/rankingsAdmin.ts rankingsAdminApi.adminTeamsMonthly().
    """
    user, err = _auth(request)
    if err:
        return err
    month = _admin_month(request, TeamMonthlyScore)
    # Ghost teams are ranked alongside real teams (rerank_team_month interleaves them), so the
    # admin preview must show them too; select_related both sides for the serializer's _team_name.
    qs = (TeamMonthlyScore.objects.filter(month=month)
          .select_related("team", "ghost_team").order_by("rank"))
    return _admin_monthly_response(request, qs, S.team_monthly, month)


# ───────────────────────── GET admin players monthly  (ungated draft preview) ─────────────────────────
@api_view(["GET"])
def admin_players_monthly(request):
    """Ungated draft of the PLAYER monthly ladder, for admin preview before publishing.

    Purpose  player rankings had NO admin surface at all before the Ladders view (owner
             2026-08-03), so this is the only place an admin can read them pre-publish.
    Request  GET /rankings/admin/players/monthly/?month=YYYY-MM&limit=&offset=
             ``month`` optional, defaults to the newest populated PLAYER month (see _admin_month).
    Response {results: [serializers.player_monthly], pagination, month, season, published}
    Auth     Authorization: Bearer <SessionToken>, head_admin | metrics_admin (admin_views._auth).
    Consumed by frontend app/(a)/a/rankings/ladders/page.tsx via
             lib/rankingsAdmin.ts rankingsAdminApi.adminPlayersMonthly().
    """
    user, err = _auth(request)
    if err:
        return err
    month = _admin_month(request, PlayerMonthlyScore)
    # Ghost players are interleaved by rerank_player_month; select_related both sides so the
    # serializer's _player_name reads player OR ghost_player without an extra query.
    qs = (PlayerMonthlyScore.objects.filter(month=month)
          .select_related("player", "ghost_player").order_by("rank"))
    return _admin_monthly_response(request, qs, S.player_monthly, month)
