"""
afc_rankings/public_tiers.py: THE tier a team or player holds, as the site shows it.

One question, one answer (owner rules R24 / R27): "what tier is this team?" is the ranking tier,
TeamQuarterlyScore.tier_assigned, in the latest Season whose tiers are PUBLISHED
(Season.tiers_published, the gate afc_rankings/views.py applies before a tier is public). A team
with no row in that season is unranked, and the answer is None.

Why this module exists (inbox #162 / #165, 8 Oct 2026). There used to be a second "tier":
afc_team.Team.team_tier, a hand-set column that defaults to "3". Every one of the 926 teams on
production held "3", so team cards, team pages, invites, the Player Market, the help bot,
broadcast audiences, poll eligibility and the admin dashboard all said "Tier 3" for everybody,
while the Rankings page said two teams were Tier 3 and 75 were Tier 4. The owner: "yes" (show the
Rankings tier instead), then "broadcasts and polls should use the tiering everything else uses."
Every one of those readers now asks this module, so the two cannot disagree again.

THE CODE IS NOT THE LABEL. tier_assigned 0 is shown as "Tier 1" (owner 2026-07-04 renamed Elite ..
Entry to Tier 1 .. Tier 4; frontend components/rankings/TierBadge.tsx renders code + 1). APIs hand
out the CODE so the frontend can localise the label; anything that writes a sentence on the server
(the help bot's tool results, poll eligibility lines) uses tier_label() below. Codes above 3 are
legitimate: tiers are extensible in the scoring config (owner 2026-10-01), so nothing here caps them.

Callers:
  - afc_team/views.py          get_all_teams, get_team_details, get_user_current_team,
                               get_team_details_based_on_invite (the `ranking_tier` field)
  - afc_team/views_transfers.py the transfer feed's tier filter and its options
  - afc_player_market/views.py application rows (`team.tier`)
  - afc_auth/audience.py        the broadcast / poll tier filters (`tiers` and `season_tiers`)
  - afc_auth/views_broadcast_audience.py  the composer's tier options and counts
  - afc_auth/views_dashboard.py the admin dashboard's teams-by-tier split
  - afc_polls/eligibility.py    "your tier" on the eligibility panel
"""
from .models import PlayerQuarterlyScore, Season, TeamQuarterlyScore


def published_tier_season():
    """The latest season whose tiers are PUBLISHED, or None when no season has published tiers.

    Latest by start date, so a team keeps the tier it was last given until the next set of tiers
    is published: a quarter whose scores are still a draft (computed but not published) never
    changes what anybody sees. That is deliberately not the Rankings page's own season choice for
    its LADDER (afc_rankings/views.py _resolve_quarterly_season, which may show a season whose
    rankings are published but whose tiers are not, with the tier column blank): a ladder describes
    one quarter, a team's tier is a standing status.
    """
    return Season.objects.filter(tiers_published=True).order_by("-start_date", "-season_id").first()


def tier_label(code):
    """The English label a person reads for a tier code: 0 -> "Tier 1". None -> "Unranked".

    For server-written sentences only (the help bot, poll eligibility). Screens localise the code
    themselves through messages/<locale>/rankings.json, which says the same thing in fr / pt.
    """
    if code is None:
        return "Unranked"
    return f"Tier {int(code) + 1}"


def published_team_tiers(team_ids, season=None):
    """{team_id: tier code} for the given teams, in ONE query. Teams with no published tier are
    simply absent, so `.get(team_id)` answers None for unranked.

    `season` lets a caller that already resolved published_tier_season() (a list endpoint, say)
    skip the second lookup; leave it out otherwise.
    """
    season = season or published_tier_season()
    team_ids = [t for t in team_ids if t is not None]
    if not season or not team_ids:
        return {}
    return dict(
        TeamQuarterlyScore.objects
        .filter(season=season, team_id__in=team_ids, tier_assigned__isnull=False)
        .values_list("team_id", "tier_assigned")
    )


def published_team_tier(team):
    """The published tier code of one team (a Team or a team id), or None when unranked."""
    team_id = getattr(team, "team_id", team)
    return published_team_tiers([team_id]).get(team_id)


def team_ids_in_tiers(codes, season=None):
    """A .values("team_id") QUERYSET of the teams holding any of `codes` in the published season,
    for use as a subquery (team_id__in=...). Empty when no season has published tiers."""
    season = season or published_tier_season()
    if not season:
        return TeamQuarterlyScore.objects.none().values("team_id")
    return (TeamQuarterlyScore.objects
            .filter(season=season, tier_assigned__in=[int(c) for c in codes], team__isnull=False)
            .values("team_id"))


def player_ids_in_tiers(codes, season=None):
    """The player-scope twin of team_ids_in_tiers: a .values("player_id") queryset of the players
    holding any of `codes` as their OWN published tier (PlayerQuarterlyScore.tier_assigned)."""
    season = season or published_tier_season()
    if not season:
        return PlayerQuarterlyScore.objects.none().values("player_id")
    return (PlayerQuarterlyScore.objects
            .filter(season=season, tier_assigned__in=[int(c) for c in codes], player__isnull=False)
            .values("player_id"))


def published_player_tier(user, season=None):
    """One player's OWN published tier code, or None. (A player on a team also inherits the team's
    tier inside the scoring engine; this is the row the Rankings players ladder shows.)"""
    season = season or published_tier_season()
    if not season or user is None:
        return None
    return (PlayerQuarterlyScore.objects
            .filter(season=season, player=user, tier_assigned__isnull=False)
            .values_list("tier_assigned", flat=True).first())


def published_team_tier_codes(season=None):
    """The tier codes at least one real team holds in the published season, ascending (best first).
    What a tier picker offers: only tiers that select somebody."""
    season = season or published_tier_season()
    if not season:
        return []
    return sorted(set(
        TeamQuarterlyScore.objects
        .filter(season=season, team__isnull=False, tier_assigned__isnull=False)
        .values_list("tier_assigned", flat=True)
    ))


# ── Setting a team's tier by hand (inbox #173, owner 2026-10-08: "admins should still be able to
# manually change the tier") ───────────────────────────────────────────────────────────────────────
# The hand-set Team.team_tier is gone as a separate fact, so a manual tier is written where every
# reader looks: the team's row in the PUBLISHED tier season, pinned (tier_overridden=True), the same
# sticky flag the Rankings > Overrides page sets. recalc honours it, and since 8 Oct 2026 it also
# keeps a pinned row of a team with no activity instead of deleting it (recalc_team_quarterly), so a
# pin on a team that was unranked survives the next recalculation. "Automatic" removes the pin.
# Called by afc_team/views.py admin_team_tier (the admin team page).


class TierPinRefused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def configured_tier_codes(season=None):
    """The tier codes the scoring config defines for `season` (0 = "Tier 1"), ascending. Tiers are
    extensible (owner 2026-10-01), so this is read from the config, never a fixed 0-3."""
    from .aggregation import resolve_tables

    tables = resolve_tables(season=season)
    # The threshold rows name every tier above the floor; the floor ("everyone else") is the
    # config's default tier, which has no row of its own.
    codes = {int(code) for _count, code in tables.tier_counts} | {int(tables.tier_default)}
    return sorted(codes)


def team_tier_state(team):
    """What the admin team page shows: {ranking_tier, pinned, reason, season, options}."""
    season = published_tier_season()
    row = (TeamQuarterlyScore.objects.filter(team=team, season=season).first()
           if season else None)
    return {
        "ranking_tier": row.tier_assigned if row else None,
        "pinned": bool(row and row.tier_overridden),
        "reason": (row.tier_override_reason if row and row.tier_overridden else ""),
        "season": season.name if season else None,
        "options": configured_tier_codes(season) if season else [],
    }


def set_team_tier(team, code, actor, reason=""):
    """Pin `team` to tier `code` in the published tier season, or remove the pin when `code` is
    None. Returns team_tier_state(team). Raises TierPinRefused with a code on a refusal.

    Removing a pin from a row the pin itself created (a team with no activity that season) deletes
    that row, so the team reads Unranked again rather than keeping a tier nobody set. A row with
    real scores keeps them and is re-projected by the next recalculation.
    """
    from django.db import transaction
    from django.utils import timezone

    from .admin_views import _audit

    season = published_tier_season()
    if not season:
        raise TierPinRefused("no_published_tier_season",
                             "No season has published tiers yet, so there is no tier to set.")
    reason = (reason or "").strip()[:255] or "Set by hand on the admin team page."
    with transaction.atomic():
        row = TeamQuarterlyScore.objects.select_for_update().filter(team=team, season=season).first()
        before = ({"tier_assigned": row.tier_assigned, "tier_overridden": row.tier_overridden}
                  if row else {"tier_assigned": None, "tier_overridden": False})
        if code is None:
            if not row or not row.tier_overridden:
                return team_tier_state(team)
            no_activity = (not row.total_score and not row.participated_in_tournaments
                           and not row.scrim_pts and not row.tournament_pts)
            if no_activity:
                row.delete()
                after = {"tier_assigned": None, "tier_overridden": False}
            else:
                row.tier_overridden = False
                row.tier_override_reason = ""
                row.save(update_fields=["tier_overridden", "tier_override_reason"])
                after = {"tier_assigned": row.tier_assigned, "tier_overridden": False}
                from . import recalc, tasks
                transaction.on_commit(lambda: tasks.enqueue_team(team.team_id, recalc.current_month(),
                                                                 season.season_id))
            action = "clear"
        else:
            code = int(code)
            if code not in configured_tier_codes(season):
                raise TierPinRefused("tier_unknown", "That tier does not exist in the rankings settings.")
            if row is None:
                row = TeamQuarterlyScore(team=team, season=season)
            row.tier_assigned = code
            row.tier_overridden = True
            row.tier_override_reason = reason
            row.tier_assigned_at = timezone.now()
            row.save()
            after = {"tier_assigned": code, "tier_overridden": True}
            action = "override"
        _audit(actor, "tier_override", action, reason,
               object_ref=f"team:{team.team_id}:season:{season.season_id}",
               before=before, after=after, season=season)
    return team_tier_state(team)
