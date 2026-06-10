"""
Recalculation + persistence layer.

Entered three ways: (1) the tasks.py Celery wrappers (real-time, fired from
signals.py), (2) the bulk recalc helpers below (seeding / admin), (3) the admin
recalc endpoints. Reads the engine output from aggregation.compute_*, writes it
into the score models, then re-ranks the affected period (§17.3) with the spec
tiebreakers (§5.4 / §6.4). The Celery tasks (tasks.py) are thin wrappers around
these; they can also be called synchronously (seeding / tests / the admin
recalc trigger).

Participation floors:
  §5.2 team monthly: ≥1 tournament to appear (else row removed).
  §6 player monthly: ≥1 tournament to appear.
  §7.4 / §9.2 quarterly floors are applied at tier evaluation (Phase 2), not here —
  quarterly scores are computed read-only; tier_assigned stays null until eval.
"""
import datetime

from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from afc_team.models import Team
from afc_auth.models import User
from . import aggregation
from .scoring import engine
from .models import (
    Season, TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
)

# §11 bottom tier (Entry) — the fallback when an attached player's team has no assigned tier.
TIER_ENTRY = 3


def current_month() -> datetime.date:
    return timezone.now().date().replace(day=1)


def current_season():
    return Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()


# ───────────────────────── TEAM ─────────────────────────
# Each recalc_* reads engine output from aggregation.compute_*, writes the score
# model, then re-ranks. Reached via the tasks.py Celery wrappers (from signals),
# the bulk helpers below (seeding/admin), or the admin recalc endpoints.
def recalc_team_monthly(team_id, month: datetime.date = None):
    team = Team.objects.filter(pk=team_id).first()
    if not team:
        return
    month = (month or current_month()).replace(day=1)
    agg = aggregation.compute_team_monthly(team, month)
    if agg.tournaments_played == 0:
        # §5.2 participation floor — no tournament activity → don't appear
        TeamMonthlyScore.objects.filter(team=team, month=month).delete()
        rerank_team_month(month)
        return
    r = agg.result
    TeamMonthlyScore.objects.update_or_create(
        team=team, month=month,
        defaults=dict(
            tournament_pts=r.tournament_pts, scrim_pts=r.scrim_pts, total_score=r.total,
            tournament_wins=agg.tournament_wins, total_kills=agg.total_kills,
            tournaments_played=agg.tournaments_played,
        ),
    )
    rerank_team_month(month)


def recalc_team_quarterly(team_id, season_id):
    team = Team.objects.filter(pk=team_id).first()
    season = Season.objects.filter(pk=season_id).first()
    if not (team and season):
        return
    existing = TeamQuarterlyScore.objects.filter(team=team, season=season).first()
    # §2.15 sticky-ban guard: a zeroed (banned) team must NOT be silently un-banned by an
    # unrelated recalc. Freeze the row exactly as the ban left it (total_score 0, banned
    # tier, is_zeroed True); only keep its rank current. Un-banning is an explicit admin
    # action (unzero-team) that clears is_zeroed first, then triggers a fresh recalc.
    if existing and existing.is_zeroed:
        rerank_team_quarter(season)
        return
    agg = aggregation.compute_team_quarterly(team, season)
    if agg.tournaments_played == 0:
        TeamQuarterlyScore.objects.filter(team=team, season=season).delete()
        rerank_team_quarter(season)
        return
    r = agg.result
    meets = agg.tournaments_played >= 2  # §7.4
    # Respect an admin tier override (§5): a locked tier is not stomped by the projected one.
    # Otherwise use the live (projected) tier from score — Entry if below the activity floor.
    # The official locked tier is (re)set when an admin runs the quarterly evaluation (Phase 2).
    if existing and existing.tier_overridden:
        tier = existing.tier_assigned
    else:
        tier = engine.assign_tier(r.total, meets)
    note = "" if meets else f"Insufficient activity ({agg.tournaments_played}/2 tournaments)"
    TeamQuarterlyScore.objects.update_or_create(
        team=team, season=season,
        defaults=dict(
            tournament_pts=r.tournament_pts, scrim_pts=r.scrim_pts,
            prize_money_pts=r.prize_money_pts, social_media_pts=r.social_media_pts,
            total_score=r.total, tournament_wins=agg.tournament_wins, total_kills=agg.total_kills,
            participated_in_tournaments=agg.tournaments_played, meets_participation_floor=meets,
            tier_assigned=tier, insufficient_activity_note=note,
        ),
    )
    rerank_team_quarter(season)


def rerank_team_month(month: datetime.date):
    # Ghost teams now rank INTERLEAVED with real teams (the team__isnull=False filter is gone), so a
    # ghost team that outscores a real team takes the higher rank. The final name tiebreak coalesces
    # team__team_name (real) with ghost_team__team_name (ghost) so a ghost row sorts without a null
    # crash. Called by recalc_team_monthly + standalone.recalc_ghost_team_monthly.
    qs = list(
        TeamMonthlyScore.objects.filter(month=month)
        .annotate(_name=Coalesce("team__team_name", "ghost_team__team_name"))
        .order_by("-total_score", "-tournament_wins", "-total_kills", "-tournaments_played", "_name")
    )
    for i, s in enumerate(qs, 1):
        s.rank = i
    if qs:
        TeamMonthlyScore.objects.bulk_update(qs, ["rank"])


def rerank_team_quarter(season):
    # Rank by the EFFECTIVE score (total minus any manual point deduction, §16) so a
    # partial penalty actually moves a team down the table — not just the displayed score.
    # A ban-zeroed team already has total_score 0, so it naturally sinks to the bottom.
    # Ghost teams are interleaved with real teams here too (no team__isnull=False filter). The name
    # tiebreak coalesces real + ghost names. Called by recalc_team_quarterly +
    # standalone.recalc_ghost_team_quarterly.
    qs = list(
        TeamQuarterlyScore.objects.filter(season=season)
        .annotate(effective=F("total_score") - F("points_deducted"),
                  _name=Coalesce("team__team_name", "ghost_team__team_name"))
        .order_by("-effective", "-tournament_wins", "-total_kills", "_name")
    )
    for i, s in enumerate(qs, 1):
        s.rank = i
    if qs:
        TeamQuarterlyScore.objects.bulk_update(qs, ["rank"])


# ───────────────────────── PLAYER ─────────────────────────
def recalc_player_monthly(player_id, month: datetime.date = None):
    player = User.objects.filter(pk=player_id).first()
    if not player:
        return
    month = (month or current_month()).replace(day=1)
    agg = aggregation.compute_player_monthly(player, month)
    if agg.tournaments_played == 0:
        PlayerMonthlyScore.objects.filter(player=player, month=month).delete()
        rerank_player_month(month)
        return
    r = agg.result
    PlayerMonthlyScore.objects.update_or_create(
        player=player, month=month,
        defaults=dict(
            kill_pts=r.kill_pts, placement_pts=r.placement_pts, mvp_pts=r.mvp_pts,
            finals_pts=r.finals_pts, team_win_pts=r.team_win_pts, participation_pts=r.participation_pts,
            scrim_kill_pts=r.scrim_kill_pts, scrim_win_pts=r.scrim_win_pts, total_score=r.total,
            total_kills=agg.total_kills, mvp_count=agg.mvp_count, finals_appearances=agg.finals_appearances,
        ),
    )
    rerank_player_month(month)


def recalc_player_quarterly(player_id, season_id):
    player = User.objects.filter(pk=player_id).first()
    season = Season.objects.filter(pk=season_id).first()
    if not (player and season):
        return
    # §2.15 sticky-ban guard: a zeroed (banned) player must NOT be silently un-banned by
    # an unrelated recalc. Freeze the row exactly as the ban left it; only keep its rank
    # current. (PlayerQuarterlyScore has no tier override — players inherit, §2.11 — so
    # the ban flag is the only sticky state here.) Un-banning is an explicit admin action.
    existing = PlayerQuarterlyScore.objects.filter(player=player, season=season).first()
    if existing and existing.is_zeroed:
        rerank_player_quarter(season)
        return
    agg = aggregation.compute_player_quarterly(player, season)
    if agg.tournaments_played == 0:
        PlayerQuarterlyScore.objects.filter(player=player, season=season).delete()
        rerank_player_quarter(season)
        return
    r = agg.result
    meets = agg.tournaments_played >= 1  # §9.2
    # Phase 1: individual projected tier from personal score. Phase 2 eval applies the
    # team-tier inheritance (§8.1) and locks tier_source = "team" vs "individual".
    tier = engine.assign_tier(r.total, meets)
    PlayerQuarterlyScore.objects.update_or_create(
        player=player, season=season,
        defaults=dict(
            total_score=r.total, prize_money_pts=r.prize_money_pts,
            participated_in_tournaments=agg.tournaments_played, meets_participation_floor=meets,
            tier_assigned=tier, tier_source="individual",
        ),
    )
    rerank_player_quarter(season)


def rerank_player_month(month: datetime.date):
    # Ghost players are interleaved with real players (the table already has no entity filter, ghost
    # rows simply appear once written). The name tiebreak coalesces player__username (real) with
    # ghost_player__ign (ghost) so a ghost row sorts without a null crash. Called by
    # recalc_player_monthly + standalone.recalc_ghost_player_monthly.
    qs = list(
        PlayerMonthlyScore.objects.filter(month=month)
        .annotate(_name=Coalesce("player__username", "ghost_player__ign"))
        .order_by("-total_score", "-total_kills", "-mvp_count", "-finals_appearances", "_name")
    )
    for i, s in enumerate(qs, 1):
        s.rank = i
    if qs:
        PlayerMonthlyScore.objects.bulk_update(qs, ["rank"])


def rerank_player_quarter(season):
    # Ghost players interleaved with real players; name tiebreak coalesces real + ghost names. Called
    # by recalc_player_quarterly + standalone.recalc_ghost_player_quarterly.
    qs = list(
        PlayerQuarterlyScore.objects.filter(season=season)
        .annotate(_name=Coalesce("player__username", "ghost_player__ign"))
        .order_by("-total_score", "_name")
    )
    for i, s in enumerate(qs, 1):
        s.rank = i
    if qs:
        PlayerQuarterlyScore.objects.bulk_update(qs, ["rank"])


# ───────────────────────── bulk (seeding / admin recalc) ─────────────────────────
def _active_team_ids(start, end):
    from afc_tournament_and_scrims.models import TournamentTeamMatchStats
    return list(
        TournamentTeamMatchStats.objects
        .filter(aggregation._day_range_q("match", start, end))
        .values_list("tournament_team__team_id", flat=True).distinct()
    )


def _active_player_ids(start, end):
    from afc_tournament_and_scrims.models import TournamentPlayerMatchStats
    return list(
        TournamentPlayerMatchStats.objects
        .filter(aggregation._day_range_q("team_stats__match", start, end))
        .values_list("player_id", flat=True).distinct()
    )


def recalc_month(month: datetime.date = None):
    """Recompute every active team + player for a month. Used by seeding/tests/admin."""
    month = (month or current_month()).replace(day=1)
    start, end = aggregation.month_bounds(month)
    for tid in _active_team_ids(start, end):
        if tid:
            recalc_team_monthly(tid, month)
    for pid in _active_player_ids(start, end):
        if pid:
            recalc_player_monthly(pid, month)


def recalc_season(season=None):
    """Recompute every active team + player for a season's quarter."""
    season = season or current_season()
    if not season:
        return
    start, end = season.start_date, season.end_date + datetime.timedelta(days=1)
    for tid in _active_team_ids(start, end):
        if tid:
            recalc_team_quarterly(tid, season.season_id)
    for pid in _active_player_ids(start, end):
        if pid:
            recalc_player_quarterly(pid, season.season_id)


# ───────────────────────── Phase 2: quarterly evaluation (§16 tier lock) ─────────────────────────
def _player_team_at_eval(player, season):
    """The team a player is rostered on for this season at evaluation time (§8.1).

    Uses the active roster row (``left_at`` IS NULL). Returns a ``Team`` or None (unattached).
    Stored on the player's quarterly row as ``team_at_evaluation`` so a later transfer doesn't
    rewrite the locked tier.
    """
    from .models import TeamSeasonRoster
    row = (TeamSeasonRoster.objects
           .filter(season=season, player=player, is_active=True, left_at__isnull=True)
           .select_related("team").first())
    return row.team if row else None


def run_evaluation(season, user=None, *, dry_run=False, force=False):
    """Quarterly EVALUATION — lock every team/player tier for the season (§16).

    Order matters: teams are tiered first (from their final score with the §7.4 activity
    floor), then players INHERIT their team's tier (§8.1) when attached at eval time, else
    take their individual tier (§9.2 floor). Already-zeroed (banned) and ``tier_overridden``
    rows are LEFT UNTOUCHED — evaluation never silently un-bans or un-overrides. A successful
    run stamps ``Season.tier_eval_run/_at/_by`` + ``scores_frozen_at`` and each row's
    ``tier_assigned_at``.

    dry_run=True computes the would-be changes and returns them WITHOUT writing anything.
    force=True re-runs an already-evaluated season; without it a second run is rejected.

    Returns a summary dict for the admin endpoint to serialise.
    """
    from django.db import transaction
    from .models import Season, TeamQuarterlyScore, PlayerQuarterlyScore

    # re-run guard (skipped for a dry run, which writes nothing)
    if season.tier_eval_run and not force and not dry_run:
        return {"ok": False, "error": "Season already evaluated — re-run with force=true to overwrite."}

    now = timezone.now()
    team_changes, player_changes = [], []

    def _evaluate():
        # 1) Teams — tier from effective score (total minus deduction) + §7.4 floor.
        #    Zeroed / overridden rows keep their existing tier (preserved, not recomputed).
        #    GHOST teams are tiered here too (the team__isnull=False filter is gone): a ghost has no
        #    sticky-ban/override state, so it always hits the assign_tier branch. It is NEVER added to
        #    team_tier_by_id (that map drives real-player inheritance, and a ghost team has no players
        #    inheriting from it), so ghost tiering cannot alter any real player's inherited tier.
        team_rows = list(
            TeamQuarterlyScore.objects.filter(season=season).select_related("team", "ghost_team")
        )
        team_tier_by_id = {}     # team_id -> would-be tier (used for player inheritance, incl. dry run)
        team_writes = []
        for t in team_rows:
            if t.is_zeroed or t.tier_overridden:
                if t.team_id is not None:  # only real teams feed player inheritance
                    team_tier_by_id[t.team_id] = t.tier_assigned  # preserve the locked decision
                continue
            new_tier = engine.assign_tier(t.total_score - t.points_deducted, t.meets_participation_floor)
            if t.team_id is not None:  # ghosts have no inheriting players -> stay out of the map
                team_tier_by_id[t.team_id] = new_tier
            # change-record name: real team name, else the ghost team name (guarded so a ghost row
            # does not dereference a null team).
            name = t.team.team_name if t.team_id else t.ghost_team.team_name
            team_changes.append({"team_id": t.team_id, "name": name,
                                 "old_tier": t.tier_assigned, "new_tier": new_tier})
            t.tier_assigned = new_tier
            t.tier_assigned_at = now
            team_writes.append(t)
        if not dry_run and team_writes:
            TeamQuarterlyScore.objects.bulk_update(team_writes, ["tier_assigned", "tier_assigned_at"])

        # 2) Players — inherit team tier (§8.1) when attached, else individual tier (§9.2).
        #    Zeroed players are preserved. team_at_evaluation locks the inheritance source.
        #    GHOST players are tiered here too (the table has no entity filter). A ghost player has no
        #    roster, so _player_team_at_eval is NOT called for it (guarded by p.player_id) -> it is
        #    always unattached -> takes its individual tier (source "individual"), team_at_evaluation
        #    None. This cannot touch any real player's inheritance (each row is independent).
        player_rows = list(
            PlayerQuarterlyScore.objects.filter(season=season).select_related("player", "ghost_player")
        )
        player_writes = []
        for p in player_rows:
            if p.is_zeroed:
                continue
            # a ghost player has no player FK -> never look up a roster; treat as unattached.
            team = _player_team_at_eval(p.player, season) if p.player_id else None
            is_attached = team is not None
            team_tier = team_tier_by_id.get(team.team_id, TIER_ENTRY) if is_attached else None
            new_tier, source = engine.player_tier(is_attached, team_tier, p.total_score, p.meets_participation_floor)
            # change-record name: real username, else the ghost in-game name (guarded for nulls).
            name = p.player.username if p.player_id else p.ghost_player.ign
            player_changes.append({"player_id": p.player_id, "name": name,
                                   "old_tier": p.tier_assigned, "new_tier": new_tier, "source": source})
            p.tier_assigned = new_tier
            p.tier_source = source
            p.team_at_evaluation = team if is_attached else None
            p.tier_assigned_at = now
            player_writes.append(p)
        if not dry_run and player_writes:
            PlayerQuarterlyScore.objects.bulk_update(
                player_writes, ["tier_assigned", "tier_source", "team_at_evaluation", "tier_assigned_at"])

        # 3) Stamp + freeze the season.
        if not dry_run:
            season.tier_eval_run = True
            season.tier_eval_run_at = now
            season.tier_eval_run_by = user
            season.scores_frozen_at = now
            season.save(update_fields=["tier_eval_run", "tier_eval_run_at",
                                       "tier_eval_run_by", "scores_frozen_at"])

    if dry_run:
        _evaluate()
    else:
        # lock the season row so two admins can't evaluate it concurrently
        with transaction.atomic():
            Season.objects.select_for_update().get(pk=season.season_id)
            _evaluate()

    dist = {0: 0, 1: 0, 2: 0, 3: 0}
    for c in team_changes:
        if c["new_tier"] is not None:
            dist[c["new_tier"]] = dist.get(c["new_tier"], 0) + 1
    return {
        "ok": True, "dry_run": dry_run, "force": force, "season_id": season.season_id,
        "teams_evaluated": len(team_changes), "players_evaluated": len(player_changes),
        "tier_distribution": dist, "team_changes": team_changes, "player_changes": player_changes,
    }
