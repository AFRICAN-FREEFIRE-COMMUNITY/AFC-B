"""
Admin publish controls + draft preview (Phase 2c).

The public quarterly endpoints (views.py) hide a season's rankings until
``rankings_published`` and its tiers until ``tiers_published``. Admins manage those flags
here, AND read the UNGATED draft (the full computed data, including not-yet-published
tiers) so they can preview before publishing. Rankings and tiers publish independently.

Same idiom as the other admin modules: function-based @api_view, manual-dict serializers,
the ``admin_views`` auth/reason/audit spine.
"""
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import views as V
from . import serializers as S
from .admin_views import _auth, _require_reason, _audit
from .models import Season, TeamQuarterlyScore, PlayerQuarterlyScore


@api_view(["PATCH"])
def publish_state(request, season_id):
    """Set the rankings / tiers publish flags for a season.

    Body may include ``rankings_published`` and/or ``tiers_published`` (bool) — only the keys
    present are changed, so rankings and tiers publish/unpublish independently. ``reason``
    (>=10 chars) is mandatory and goes to the audit log.
    """
    user, err = _auth(request)
    if err:
        return err
    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)
    reason, err = _require_reason(request)
    if err:
        return err

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
    qs = (TeamQuarterlyScore.objects.filter(season=season, team__isnull=False)
          .select_related("team").order_by("rank"))
    items, meta = S.paginate(request, qs)
    return Response({"results": [S.team_quarterly(x) for x in items], "pagination": meta,
                     "season": S.season(season)})


@api_view(["GET"])
def admin_players_quarterly(request):
    """Ungated draft of player quarterly scores for admin preview."""
    user, err = _auth(request)
    if err:
        return err
    season = V._resolve_season(request)
    if not season:
        return Response({"results": [], "pagination": {"total_count": 0, "has_more": False}, "season": None})
    qs = (PlayerQuarterlyScore.objects.filter(season=season).select_related("player").order_by("rank"))
    items, meta = S.paginate(request, qs)
    return Response({"results": [S.player_quarterly(x) for x in items], "pagination": meta,
                     "season": S.season(season)})
