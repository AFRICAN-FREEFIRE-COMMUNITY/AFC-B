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
  §7.4 / §9.2 quarterly floors are applied at tier evaluation (Phase 2), not here - 
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
from .scoring.tables import ScoringTables  # noqa: F401  (type reference in docstrings/signatures)
from .models import (
    Season, TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
)

# §11 bottom tier (Entry) - the fallback when an attached player's team has no assigned tier.
TIER_ENTRY = 3


def current_month() -> datetime.date:
    return timezone.now().date().replace(day=1)


def current_season():
    from .models import auto_rollover_seasons
    auto_rollover_seasons()  # calendar-driven activation (owner 2026-07-02)
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
    # The monthly floor is admin-editable too, resolved from the config bound to the season
    # this month sits in (default 1 tournament, spec §5.2).
    monthly_floor = aggregation.resolve_tables(month=month).team_monthly_floor
    if agg.tournaments_played < monthly_floor and not agg.result.scrim_pts:
        # §5.2 participation floor - no activity at all → don't appear.
        #
        # AMENDED (owner 2026-08-03): scrim activity now satisfies this floor too. The floor
        # used to require a TOURNAMENT, which combined with the old scrim cap to make
        # scrim-only teams doubly invisible: they scored zero because the cap was a
        # percentage of zero, and then this deleted their row anyway. Scrims are meant to
        # count toward rankings, so a team with real scrim points now stays on the ladder.
        # A team with neither is still removed, which is the case this rule is actually for.
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
    # §7.4 activity floor and the tier cutoffs are admin-editable, and the config that applies
    # is the one bound to THIS season - so a closed season keeps the floor it was scored under
    # even after an admin changes it for the current one (aggregation.resolve_tables).
    tables = aggregation.resolve_tables(season=season)
    floor = tables.team_quarterly_floor
    meets = agg.tournaments_played >= floor
    # Respect an admin tier override (§5): a locked tier is not stomped by the projected one.
    # Otherwise use the live (projected) tier from score - Entry if below the activity floor.
    # The official locked tier is (re)set when an admin runs the quarterly evaluation (Phase 2).
    # Under TOP-N the value written here is a placeholder: a tier decided by position cannot
    # be worked out from one team's score, so rerank_team_quarter (called at the end of this
    # function, once this row exists and is in the ladder) overwrites it for the whole table.
    # The placeholder is never observable - both writes happen inside this one call.
    if existing and existing.tier_overridden:
        tier = existing.tier_assigned
    else:
        tier = engine.assign_tier(r.total, meets, tables)
    note = "" if meets else f"Insufficient activity ({agg.tournaments_played}/{floor} tournaments)"
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


def team_quarter_ladder(season):
    """The season's team ladder as a queryset, in the ONE canonical order (§5.4 tiebreaks).

    Ordered by the EFFECTIVE score (total minus any manual point deduction, §16) so a
    partial penalty actually moves a team down the table - not just the displayed score.
    A ban-zeroed team already has total_score 0, so it naturally sinks to the bottom.
    Ghost teams are interleaved with real teams here (no team__isnull=False filter); the name
    tiebreak coalesces real + ghost names so a ghost row sorts without a null crash.

    Extracted so that the printed rank (``rerank_team_quarter``), the top-N tiers computed
    from it, and ``run_evaluation``'s locked tiers are all reading the same order. If they
    ever diverged, a team could be shown as rank 9 while another team was given the last
    Tier 1 place, and there would be no way to explain it.
    """
    return (
        TeamQuarterlyScore.objects.filter(season=season)
        .annotate(effective=F("total_score") - F("points_deducted"),
                  _name=Coalesce("team__team_name", "ghost_team__team_name"))
        .order_by("-effective", "-tournament_wins", "-total_kills", "_name")
    )


def top_n_team_tiers(rows, tables):
    """{score_row_pk: tier} for a team ladder under top-N tiering. ``rows`` in ladder order.

    Rows whose tier must not be recomputed are left out of the map entirely rather than
    given a tier: a ban-zeroed team (§2.15) and an admin-overridden tier (§5) keep the
    decision that was made about them, exactly as they do in threshold mode. They are still
    excluded from the COUNT, because they are not competing for a place - a banned team
    holding a Tier 1 slot open would be the same bug the participation floor avoids.

    See ``engine.assign_tiers_top_n`` for the floor / tie / leftover rules.
    """
    entries = [
        engine.LadderEntry(key=r.pk,
                           score=r.total_score - r.points_deducted,
                           meets_floor=r.meets_participation_floor)
        for r in rows if not team_tier_is_locked(r)
    ]
    return engine.assign_tiers_top_n(entries, tables)


def team_tier_is_locked(row):
    """True when this team's tier is a standing decision that no recalculation may revisit.

    A ban zeroes the row (§2.15) and an admin override pins it (§5); in both cases the tier
    on the row is the answer and stays the answer. ONE predicate on purpose: it decides both
    which rows top_n_team_tiers leaves out of the ladder and which rows run_evaluation skips.
    If those two ever disagreed, a row could be skipped by one and expected by the other.
    """
    return bool(row.is_zeroed or row.tier_overridden)


def rerank_team_quarter(season):
    """Renumber the season's team ladder, and in top-N mode re-tier it in the same pass.

    WHY THE TIER IS DECIDED HERE and not in recalc_team_quarterly: under top-N a team's tier
    is a property of the whole ladder, not of its own score, so one team's new result can
    move a different team out of Tier 1. There is no per-team answer to compute. This runs
    on every write to the ladder (recalc_team_quarterly + standalone.recalc_ghost_team_
    quarterly both call it), which is what keeps the displayed tiers honest between
    evaluations. Threshold mode is untouched: the per-team ``engine.assign_tier`` call in
    recalc_team_quarterly stands, and this function only writes ranks, exactly as before.

    The tiers written here are PROVISIONAL, in the same sense the ladder itself is: they
    follow the live standings and move as results land. The locked, end-of-season answer is
    still whatever ``run_evaluation`` stamps (``Season.tier_eval_run``), which is the
    existing contract for every mode.
    """
    qs = list(team_quarter_ladder(season))
    for i, s in enumerate(qs, 1):
        s.rank = i

    fields = ["rank"]
    if qs:
        tables = aggregation.resolve_tables(season=season)
        if tables.tier_mode == engine.TIER_MODE_TOP_N:
            tiers = top_n_team_tiers(qs, tables)
            for s in qs:
                if s.pk in tiers:
                    s.tier_assigned = tiers[s.pk]
            fields.append("tier_assigned")
        TeamQuarterlyScore.objects.bulk_update(qs, fields)


# ───────────────────────── PLAYER ─────────────────────────
def recalc_player_monthly(player_id, month: datetime.date = None):
    player = User.objects.filter(pk=player_id).first()
    if not player:
        return
    month = (month or current_month()).replace(day=1)
    agg = aggregation.compute_player_monthly(player, month)
    # Admin-editable monthly floor (default 1 tournament, spec §6), from this month's config.
    if agg.tournaments_played < aggregation.resolve_tables(month=month).player_monthly_floor:
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
    # current. (PlayerQuarterlyScore has no tier override - players inherit, §2.11 - so
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
    # §9.2 floor + cutoffs from the config bound to this season (see recalc_team_quarterly).
    tables = aggregation.resolve_tables(season=season)
    meets = agg.tournaments_played >= tables.player_quarterly_floor
    # Phase 1: individual projected tier from personal score. Phase 2 eval applies the
    # team-tier inheritance (§8.1) and locks tier_source = "team" vs "individual".
    # Under TOP-N this is a placeholder, overwritten for the whole ladder by
    # rerank_player_quarter at the end of this call - see recalc_team_quarterly.
    tier = engine.assign_tier(r.total, meets, tables)
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


def player_quarter_ladder(season):
    """The season's player ladder as a queryset, in the one canonical order (§6.4 tiebreak).

    Ghost players interleaved with real players; the name tiebreak coalesces real + ghost
    names so a ghost row sorts without a null crash. Extracted for the same reason as
    ``team_quarter_ladder``: rank, top-N tier and evaluation must read one order.
    """
    return (
        PlayerQuarterlyScore.objects.filter(season=season)
        .annotate(_name=Coalesce("player__username", "ghost_player__ign"))
        .order_by("-total_score", "_name")
    )


def top_n_player_tiers(rows, tables):
    """{score_row_pk: tier} for the INDIVIDUAL player ladder under top-N tiering.

    Computed over every player row, which is exactly the population ``engine.assign_tier``
    is applied to today - so this is the same question ("what tier does this player's own
    season earn"), answered by position instead of by score. A player attached to a team at
    evaluation still INHERITS the team's tier (§8.1) and never uses this; see
    ``engine.player_tier``. Zeroed (banned) rows keep their decision and are left out.

    WHY PLAYERS FOLLOW THE MODE TOO: Tier 1 is one scale shared by the team ladder and the
    player ladder. If teams were tiered by position while players were tiered by score, the
    same badge would mean two different things on the same site, and in a season where no
    team cleared the old Tier 1 cutoff there would be Tier 1 players and no Tier 1 teams.
    """
    entries = [
        engine.LadderEntry(key=r.pk, score=r.total_score,
                           meets_floor=r.meets_participation_floor)
        for r in rows if not r.is_zeroed
    ]
    return engine.assign_tiers_top_n(entries, tables)


def rerank_player_quarter(season):
    """Renumber the season's player ladder, and in top-N mode re-tier it in the same pass.

    The mirror of ``rerank_team_quarter`` - see its docstring for why the tier is decided
    here under top-N and why these tiers are provisional until evaluation. Called by
    recalc_player_quarterly + standalone.recalc_ghost_player_quarterly.
    """
    qs = list(player_quarter_ladder(season))
    for i, s in enumerate(qs, 1):
        s.rank = i

    fields = ["rank"]
    if qs:
        tables = aggregation.resolve_tables(season=season)
        if tables.tier_mode == engine.TIER_MODE_TOP_N:
            tiers = top_n_player_tiers(qs, tables)
            for s in qs:
                if s.pk in tiers:
                    s.tier_assigned = tiers[s.pk]
            fields.append("tier_assigned")
        PlayerQuarterlyScore.objects.bulk_update(qs, fields)


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


def run_evaluation(season, user=None, *, dry_run=False, force=False, recompute=True):
    """Quarterly EVALUATION - lock every team/player tier for the season (§16).

    Order matters: teams are tiered first (from their final score with the §7.4 activity
    floor), then players INHERIT their team's tier (§8.1) when attached at eval time, else
    take their individual tier (§9.2 floor). Already-zeroed (banned) and ``tier_overridden``
    rows are LEFT UNTOUCHED - evaluation never silently un-bans or un-overrides. A successful
    run stamps ``Season.tier_eval_run/_at/_by`` + ``scores_frozen_at`` and each row's
    ``tier_assigned_at``.

    recompute=True (default, REAL runs only): before tiering, rebuild the season's quarterly
    SCORES from match results via ``recalc_season(season)``. WHY (owner bug 2026-06-29: "I ran
    evaluation and got nothing even though results were inputted"): tiering only re-tiers
    ``*QuarterlyScore`` rows that ALREADY exist, and those rows are normally kept fresh by the
    signal-driven (Celery) recalc pipeline. If that pipeline never ran for this season (no
    worker, or results entered before the season was active), the rows are missing/empty and
    evaluation produces NOTHING. Recomputing here makes "Run evaluation" self-sufficient: it
    derives the scores straight from the match stats in the season window first, THEN tiers
    them. This is a deliberate, explicit admin batch action (like ``manage.py recalc_rankings``)
    - NOT the live-edit hot path the "recalc is never inline" rule guards, so running it
    synchronously here is correct. It runs OUTSIDE the tier-lock transaction so the heavy
    recompute never holds the season row lock. dry_run skips it (a preview must write nothing);
    pass recompute=False to tier the existing rows as-is (tests / callers that pre-seed scores).

    dry_run=True computes the would-be changes and returns them WITHOUT writing anything.
    force=True re-runs an already-evaluated season; without it a second run is rejected.

    Returns a summary dict for the admin endpoint to serialise.
    """
    from django.db import transaction
    from .models import Season, TeamQuarterlyScore, PlayerQuarterlyScore

    # re-run guard (skipped for a dry run, which writes nothing)
    if season.tier_eval_run and not force and not dry_run:
        return {"ok": False, "error": "Season already evaluated - re-run with force=true to overwrite."}

    # Refresh the season's quarterly SCORES from match results before tiering (real runs only).
    # See the docstring: this is what makes evaluation return tiers even if the async recalc
    # pipeline never built the score rows. Done before the atomic tier-lock so the recompute
    # (which writes many rows + reranks) doesn't hold the season select_for_update lock.
    recomputed = bool(recompute and not dry_run)
    if recomputed:
        recalc_season(season)

    now = timezone.now()
    team_changes, player_changes = [], []
    # The cutoffs that decide every tier below come from the scoring config bound to THIS
    # season, not from whatever is active today - re-evaluating a closed season must reproduce
    # the tiers it was given, not re-tier it under newer rules.
    tables = aggregation.resolve_tables(season=season)

    def _evaluate():
        # 1) Teams - tier from effective score (total minus deduction) + §7.4 floor.
        #    Zeroed / overridden rows keep their existing tier (preserved, not recomputed).
        #    GHOST teams are tiered here too (the team__isnull=False filter is gone): a ghost has no
        #    sticky-ban/override state, so it always hits the assign_tier branch. It is NEVER added to
        #    team_tier_by_id (that map drives real-player inheritance, and a ghost team has no players
        #    inheriting from it), so ghost tiering cannot alter any real player's inherited tier.
        # Read in LADDER order, not arbitrary order: under top-N the tier is decided by
        # position, so evaluation has to see the same sequence the printed ranks came from.
        # Threshold mode does not care about the order, so one query serves both.
        team_rows = list(team_quarter_ladder(season).select_related("team", "ghost_team"))
        # Under top-N the whole ladder is tiered in one pass up front (a team's tier depends
        # on every other team), then each row reads its answer out of the map. Empty in
        # threshold mode, where each row is still decided from its own score below.
        top_n_teams = (top_n_team_tiers(team_rows, tables)
                       if tables.tier_mode == engine.TIER_MODE_TOP_N else {})
        team_tier_by_id = {}     # team_id -> would-be tier (used for player inheritance, incl. dry run)
        team_writes = []
        for t in team_rows:
            # Same predicate top_n_team_tiers uses to leave a row out of the ladder, so every
            # row that reaches the lookup below is guaranteed to be in the map.
            if team_tier_is_locked(t):
                if t.team_id is not None:  # only real teams feed player inheritance
                    team_tier_by_id[t.team_id] = t.tier_assigned  # preserve the locked decision
                continue
            new_tier = (
                top_n_teams[t.pk] if tables.tier_mode == engine.TIER_MODE_TOP_N
                else engine.assign_tier(
                    t.total_score - t.points_deducted, t.meets_participation_floor, tables)
            )
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

        # 2) Players - inherit team tier (§8.1) when attached, else individual tier (§9.2).
        #    Zeroed players are preserved. team_at_evaluation locks the inheritance source.
        #    GHOST players are tiered here too (the table has no entity filter). A ghost player has no
        #    roster, so _player_team_at_eval is NOT called for it (guarded by p.player_id) -> it is
        #    always unattached -> takes its individual tier (source "individual"), team_at_evaluation
        #    None. This cannot touch any real player's inheritance (each row is independent).
        player_rows = list(
            player_quarter_ladder(season).select_related("player", "ghost_player")
        )
        # The individual-tier answer for every player, by position, when the season runs on
        # top-N. Only reached for an UNATTACHED player - an attached one inherits its team's
        # tier either way (§8.1), which engine.player_tier still decides.
        top_n_players = (top_n_player_tiers(player_rows, tables)
                         if tables.tier_mode == engine.TIER_MODE_TOP_N else {})
        player_writes = []
        for p in player_rows:
            if p.is_zeroed:
                continue
            # a ghost player has no player FK -> never look up a roster; treat as unattached.
            team = _player_team_at_eval(p.player, season) if p.player_id else None
            is_attached = team is not None
            team_tier = team_tier_by_id.get(team.team_id, TIER_ENTRY) if is_attached else None
            new_tier, source = engine.player_tier(
                is_attached, team_tier, p.total_score, p.meets_participation_floor, tables,
                individual_tier=top_n_players.get(p.pk))
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
    # When NOTHING was evaluated, tell the admin WHY (the old silent "0" is what made this look
    # broken). After a recompute, empty means there are genuinely no countable results in the
    # season's date window; without a recompute it means the score rows were never built.
    note = ""
    if not team_changes and not player_changes:
        note = (
            "No tournament results were found in this season's date range, so there is nothing "
            "to tier. Check that results have been entered and that the season's start/end dates "
            "cover them."
            if recomputed else
            "No quarterly scores exist for this season yet. Run a real evaluation (not a dry run) "
            "so the scores are rebuilt from match results first."
        )
    return {
        "ok": True, "dry_run": dry_run, "force": force, "season_id": season.season_id,
        "recomputed": recomputed, "note": note,
        "teams_evaluated": len(team_changes), "players_evaluated": len(player_changes),
        "tier_distribution": dist, "team_changes": team_changes, "player_changes": player_changes,
    }
