"""
afc_rankings.standalone — feed published standalone leaderboards into the rankings engine.

PURPOSE
    A standalone leaderboard (afc_leaderboard.StandaloneLeaderboard) is an event-less competition
    table. When an AFC admin marks one counts_toward_rankings=True and publishes it, each of its
    per-participant results must feed the SAME aggregation -> engine -> score-row -> rerank machinery
    that events use, so a later recompute stays correct and idempotent. This module owns ALL of the
    standalone-LB -> rankings logic in one place:

      1. Input builders: collapse one published+counting LB into ONE engine input per participant
         (a TournamentInput for a team, a PlayerTournamentInput for a solo user), mirroring how one
         event collapses to one input per entity in aggregation._collect_team / _collect_player.
      2. Ghost-team compute + recalc: real teams/users ride the existing aggregation + recalc path
         (see HOW IT CONNECTS); ghost teams have no event activity to ride, so we compute + persist
         their TeamMonthlyScore/TeamQuarterlyScore(ghost_team=...) rows here, left at rank 0 (never
         interleaved into the real-team rank — rerank_team_* filters team__isnull=False).
      3. recompute_for_leaderboard(lb): the signal entry point that enqueues a recompute for every
         participant entity (real team, ghost team, real user) for the LB's month + season.

HOW IT CONNECTS
    - Read by afc_rankings.aggregation._collect_team / _collect_player (Task 3.4): each appends this
      module's standalone_team_inputs / standalone_player_inputs to the event-built tournaments list,
      so a real team/user that played a counting standalone LB scores it exactly like an event.
    - Read by afc_rankings.standalone.recompute_for_leaderboard (the signal handler in signals.py),
      which dispatches via tasks.enqueue_team / enqueue_ghost_team / enqueue_player.
    - Writes TeamMonthlyScore / TeamQuarterlyScore (ghost_team=...) through recalc-style upserts that
      mirror recalc.recalc_team_monthly / recalc_team_quarterly MINUS the real-team rerank + the
      sticky-ban/override branches (a ghost has neither).
    - Reads the canonical placement table via scoring.engine.placement_points (NOT the leaderboard's
      own placement_points JSON, which drives only the LB's own standings) so the rankings
      contribution uses the same §4.1 table events use (matches aggregation._collect_team line ~190).

LOAD-ORDER NOTE
    afc_leaderboard.models imports afc_rankings.models (GhostTeam/GhostPlayer FKs), so importing
    afc_leaderboard at THIS module's top level would risk a circular import during app loading.
    Every function that needs the leaderboard models imports them lazily INSIDE the function body.
"""
import datetime

from .scoring import engine
from .scoring.engine import TournamentInput, PlayerTournamentInput, ScrimInput, PlayerScrimInput
from .aggregation import month_bounds, TeamAgg, PlayerAgg
from .models import (
    Season, GhostTeam, GhostPlayer,
    TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
)
from . import recalc


# ───────────────────────── window membership ─────────────────────────
def _lb_in_window(lb, start: datetime.date, end: datetime.date) -> bool:
    """True if leaderboard `lb`'s effective date falls in the half-open [start, end) window.

    Mirrors the day-range semantics aggregation._day_range_q uses for events (gte start, lt end).
    Used by every input builder below to pick only the LBs whose results bucket into the period
    being scored.
    """
    d = lb.effective_date
    return d is not None and start <= d < end


def _published_counting_team_lbs(start, end):
    """Published, counts_toward_rankings, TEAM-format leaderboards whose effective date is in
    [start, end). Helper for the team + ghost-team input builders so the filter stays in one place.
    """
    from afc_leaderboard.models import StandaloneLeaderboard
    lbs = StandaloneLeaderboard.objects.filter(
        status="published", counts_toward_rankings=True, format="team",
    )
    return [lb for lb in lbs if _lb_in_window(lb, start, end)]


def _published_counting_solo_lbs(start, end):
    """Published, counts_toward_rankings, SOLO-format leaderboards in [start, end). Helper for the
    player input builder."""
    from afc_leaderboard.models import StandaloneLeaderboard
    lbs = StandaloneLeaderboard.objects.filter(
        status="published", counts_toward_rankings=True, format="solo",
    )
    return [lb for lb in lbs if _lb_in_window(lb, start, end)]


# ───────────────────────── input builders ─────────────────────────
def _team_input_from_results(results, tier, won):
    """Collapse one team participant's ParticipantMatchResult rows in ONE LB into one TournamentInput.

    raw_placement_pts uses the CANONICAL engine.placement_points table (NOT the LB's own
    placement_points config) so the rankings contribution matches how events score (aggregation
    line ~190). raw_kills sums the raw kills. finals_appearances is 0 (a standalone LB has no finals
    stage). `won` is decided by the caller from the LB's standings.
    """
    return TournamentInput(
        tier=tier,
        raw_placement_pts=sum(engine.placement_points(r.placement) for r in results),
        raw_kills=sum(r.kills for r in results),
        won=won,
        finals_appearances=0,
    )


def _standings_leader_participant_id(lb):
    """The participant id ranked 1 in this LB's standings, or None when there are no standings.

    Reads afc_leaderboard.standings.standalone_standings (the same on-read standings the FE shows).
    Used to set `won` on the team input — the standalone analogue of TournamentTeam.is_tournament_winner.
    """
    from afc_leaderboard.standings import standalone_standings
    rows = standalone_standings(lb)
    if not rows:
        return None
    return rows[0]["participant"]["id"]


def standalone_team_inputs(team, start, end):
    """One TournamentInput per published+counting TEAM-format LB in [start, end) this `team` played.

    Read by aggregation._collect_team (Task 3.4): the returned inputs are appended to the team's
    event tournaments list, so they score through the identical engine path. `won` is True when this
    team's participant row is rank 1 in the LB's standings.
    """
    from afc_leaderboard.models import LeaderboardParticipant, ParticipantMatchResult
    inputs = []
    for lb in _published_counting_team_lbs(start, end):
        participant = LeaderboardParticipant.objects.filter(leaderboard=lb, team=team).first()
        if not participant:
            continue
        results = list(ParticipantMatchResult.objects.filter(participant=participant))
        won = _standings_leader_participant_id(lb) == participant.id
        inputs.append(_team_input_from_results(results, lb.ranking_tier, won))
    return inputs


def standalone_ghost_team_inputs(ghost_team, start, end):
    """Same as standalone_team_inputs but for a GHOST team participant (ghost_team=...).

    Read by compute_ghost_team_monthly / compute_ghost_team_quarterly below (ghost teams have no
    event activity, so they do not flow through aggregation._collect_team — they are computed here
    and persisted at rank 0).
    """
    from afc_leaderboard.models import LeaderboardParticipant, ParticipantMatchResult
    inputs = []
    for lb in _published_counting_team_lbs(start, end):
        participant = LeaderboardParticipant.objects.filter(leaderboard=lb, ghost_team=ghost_team).first()
        if not participant:
            continue
        results = list(ParticipantMatchResult.objects.filter(participant=participant))
        won = _standings_leader_participant_id(lb) == participant.id
        inputs.append(_team_input_from_results(results, lb.ranking_tier, won))
    return inputs


def standalone_player_inputs(user, start, end):
    """One PlayerTournamentInput per published+counting SOLO-format LB in [start, end) this `user`
    played.

    Read by aggregation._collect_player (Task 3.4): appended to the player's event tournaments list.
    Mirrors aggregation._collect_player — a player scores on kills + participation, never on raw
    placement (personal_placement_pts stays 0; line ~306 in aggregation). mvp/finals/team_won are
    0/0/False (a standalone solo LB has no MVP, finals stage, or team-win marker).
    """
    from afc_leaderboard.models import LeaderboardParticipant, ParticipantMatchResult
    inputs = []
    for lb in _published_counting_solo_lbs(start, end):
        participant = LeaderboardParticipant.objects.filter(leaderboard=lb, user=user).first()
        if not participant:
            continue
        results = list(ParticipantMatchResult.objects.filter(participant=participant))
        inputs.append(_player_input_from_results(results, lb.ranking_tier))
    return inputs


def _player_input_from_results(results, tier):
    """Collapse one solo participant's ParticipantMatchResult rows in ONE LB into one
    PlayerTournamentInput. Shared by standalone_player_inputs (real user) and
    standalone_ghost_player_inputs (ghost player) so both build the IDENTICAL personal input: a
    player scores on kills + participation only, never raw placement (personal_placement_pts stays 0,
    symmetric with aggregation._collect_player line ~315). mvp/finals/team_won are 0/0/False (a solo
    LB has no MVP, finals stage, or team-win marker)."""
    return PlayerTournamentInput(
        tier=tier,
        personal_kills=sum(r.kills for r in results),
        personal_placement_pts=0,
        mvp_count=0,
        finals_appearances=0,
        team_won=False,
        participated=True,
    )


def standalone_ghost_player_inputs(ghost_player, start, end):
    """One PlayerTournamentInput per published+counting SOLO-format LB in [start, end) this
    `ghost_player` participated in.

    Mirrors standalone_player_inputs but for a GHOST player (ghost_player=...). A ghost player has no
    event activity (nothing in the event aggregation path reads GhostPlayer), so it does not flow
    through aggregation._collect_player — it is computed here and persisted by
    recalc_ghost_player_monthly / _quarterly below, then ranked alongside real players.
    """
    from afc_leaderboard.models import LeaderboardParticipant, ParticipantMatchResult
    inputs = []
    for lb in _published_counting_solo_lbs(start, end):
        participant = LeaderboardParticipant.objects.filter(leaderboard=lb, ghost_player=ghost_player).first()
        if not participant:
            continue
        results = list(ParticipantMatchResult.objects.filter(participant=participant))
        inputs.append(_player_input_from_results(results, lb.ranking_tier))
    return inputs


# ───────────────────────── ghost-team compute ─────────────────────────
# A ghost team has no event activity, so it never flows through aggregation._collect_team. We
# compute its score from the standalone inputs alone (no scrims — a ghost has none) and persist a
# rank-0 row via the recalc helpers below. These mirror aggregation.compute_team_monthly /
# compute_team_quarterly MINUS the event collection + scrim caps (ghosts have neither).
def compute_ghost_team_monthly(ghost_team, month: datetime.date) -> TeamAgg:
    """TeamAgg for a ghost team's monthly score, built only from its standalone-LB inputs.

    Read by recalc_ghost_team_monthly below. Returns the same TeamAgg shape recalc_team_monthly
    consumes so the persistence code mirrors recalc.recalc_team_monthly field-for-field.
    """
    start, end = month_bounds(month)
    tours = standalone_ghost_team_inputs(ghost_team, start, end)
    result = engine.monthly_team_score(tours, ScrimInput(0, 0, 0))
    return TeamAgg(
        result=result,
        tournament_wins=sum(1 for t in tours if t.won),
        total_kills=sum(t.raw_kills for t in tours),
        tournaments_played=len(tours),
    )


def compute_ghost_team_quarterly(ghost_team, season) -> TeamAgg:
    """TeamAgg for a ghost team's quarterly score from its standalone-LB inputs over the season
    window. No prize money / social media (a ghost team has neither — passed as 0). Read by
    recalc_ghost_team_quarterly below."""
    start, end = season.start_date, season.end_date + datetime.timedelta(days=1)
    tours = standalone_ghost_team_inputs(ghost_team, start, end)
    result = engine.quarterly_team_score(
        tours, ScrimInput(0, 0, 0), prize_money_naira=0.0, combined_followers=0,
    )
    return TeamAgg(
        result=result,
        tournament_wins=sum(1 for t in tours if t.won),
        total_kills=sum(t.raw_kills for t in tours),
        tournaments_played=len(tours),
    )


# ───────────────────────── ghost-team recalc + persist ─────────────────────────
# These write TeamMonthlyScore/TeamQuarterlyScore(ghost_team=...) rows and then RE-RANK the period.
# Ghost teams are now first-class in the ladder (owner directive 2026-06-10): rerank_team_month /
# rerank_team_quarter no longer filter team__isnull=False, so they interleave ghost + real rows by
# score and assign a real 1-based rank to the ghost. (The serializer badges the row via is_ghost.)
# Participation floor: 0 standalone tournaments => delete the row (mirrors recalc.recalc_team_monthly
# §5.2) then rerank so the now-removed ghost no longer holds a rank slot.
def recalc_ghost_team_monthly(ghost_team_id, month: datetime.date = None):
    """Recompute + persist a ghost team's monthly score, then rerank the month so the ghost gets a
    real rank interleaved with the real teams.

    Reached via tasks.recalculate_ghost_team_monthly (the Celery wrapper enqueue_ghost_team
    dispatches), itself fired from recompute_for_leaderboard when a counting LB changes.
    """
    ghost = GhostTeam.objects.filter(pk=ghost_team_id).first()
    if not ghost:
        return
    month = (month or recalc.current_month()).replace(day=1)
    agg = compute_ghost_team_monthly(ghost, month)
    if agg.tournaments_played == 0:
        # §5.2 participation floor — no standalone activity this month => no row. Rerank so the ghost
        # vacates its rank slot and the remaining rows close the gap.
        TeamMonthlyScore.objects.filter(ghost_team=ghost, month=month).delete()
        recalc.rerank_team_month(month)
        return
    r = agg.result
    TeamMonthlyScore.objects.update_or_create(
        ghost_team=ghost, month=month,
        defaults=dict(
            tournament_pts=r.tournament_pts, scrim_pts=r.scrim_pts, total_score=r.total,
            tournament_wins=agg.tournament_wins, total_kills=agg.total_kills,
            tournaments_played=agg.tournaments_played,
        ),
    )
    # Rerank the whole month (real + ghost together) so the ghost lands at its score-ordered rank.
    recalc.rerank_team_month(month)


def recalc_ghost_team_quarterly(ghost_team_id, season_id):
    """Recompute + persist a ghost team's quarterly score, then rerank the season. Mirrors
    recalc.recalc_team_quarterly MINUS the sticky-ban / tier-override branches (a ghost has neither).

    tier is the projected tier from the score with the §7.4 activity floor (played >= 2); the ghost
    now gets a real interleaved rank via rerank_team_quarter.
    """
    ghost = GhostTeam.objects.filter(pk=ghost_team_id).first()
    season = Season.objects.filter(pk=season_id).first()
    if not (ghost and season):
        return
    agg = compute_ghost_team_quarterly(ghost, season)
    if agg.tournaments_played == 0:
        TeamQuarterlyScore.objects.filter(ghost_team=ghost, season=season).delete()
        recalc.rerank_team_quarter(season)
        return
    r = agg.result
    meets = agg.tournaments_played >= 2  # §7.4 activity floor
    tier = engine.assign_tier(r.total, meets)
    note = "" if meets else f"Insufficient activity ({agg.tournaments_played}/2 tournaments)"
    TeamQuarterlyScore.objects.update_or_create(
        ghost_team=ghost, season=season,
        defaults=dict(
            tournament_pts=r.tournament_pts, scrim_pts=r.scrim_pts,
            prize_money_pts=r.prize_money_pts, social_media_pts=r.social_media_pts,
            total_score=r.total, tournament_wins=agg.tournament_wins, total_kills=agg.total_kills,
            participated_in_tournaments=agg.tournaments_played, meets_participation_floor=meets,
            tier_assigned=tier, insufficient_activity_note=note,
        ),
    )
    recalc.rerank_team_quarter(season)


# ───────────────────────── ghost-player compute ─────────────────────────
# A ghost player has no event activity, so it never flows through aggregation._collect_player. We
# compute its score from the standalone solo-LB inputs alone (no scrims — a ghost has none) and
# persist a PlayerMonthlyScore/PlayerQuarterlyScore(ghost_player=...) row via the recalc helpers
# below. These mirror aggregation.compute_player_monthly / compute_player_quarterly MINUS the event
# collection + scrims + inherited prize money (a ghost player has none of those).
def compute_ghost_player_monthly(ghost_player, month: datetime.date) -> PlayerAgg:
    """PlayerAgg for a ghost player's monthly score, built only from its standalone solo-LB inputs.

    Read by recalc_ghost_player_monthly below. Returns the same PlayerAgg shape recalc_player_monthly
    consumes so the persistence code mirrors recalc.recalc_player_monthly field-for-field. Scrims are
    PlayerScrimInput(0, 0) (a ghost player has no scrim activity).
    """
    start, end = month_bounds(month)
    tours = standalone_ghost_player_inputs(ghost_player, start, end)
    result = engine.monthly_player_score(tours, PlayerScrimInput(scrim_kills=0, scrim_wins=0))
    return PlayerAgg(
        result=result,
        total_kills=sum(t.personal_kills for t in tours),
        mvp_count=0,                 # a solo standalone LB has no MVP marker
        finals_appearances=0,        # ... nor a finals stage
        tournaments_played=len(tours),
    )


def compute_ghost_player_quarterly(ghost_player, season) -> PlayerAgg:
    """PlayerAgg for a ghost player's quarterly score from its standalone solo-LB inputs over the
    season window. No inherited prize money (a ghost player rosters on no team, passed 0.0). Read by
    recalc_ghost_player_quarterly below."""
    start, end = season.start_date, season.end_date + datetime.timedelta(days=1)
    tours = standalone_ghost_player_inputs(ghost_player, start, end)
    result = engine.quarterly_player_score(
        tours, PlayerScrimInput(scrim_kills=0, scrim_wins=0), inherited_prize_money_naira=0.0,
    )
    return PlayerAgg(
        result=result,
        total_kills=sum(t.personal_kills for t in tours),
        mvp_count=0,
        finals_appearances=0,
        tournaments_played=len(tours),
    )


# ───────────────────────── ghost-player recalc + persist ─────────────────────────
# Mirror recalc.recalc_player_monthly / recalc_player_quarterly for a ghost player, then rerank the
# period so the ghost interleaves with the real players (owner directive 2026-06-10). A ghost player
# has no sticky-ban state (the recalc_player_* §2.15 guard does not apply). Participation floor: 0
# standalone tournaments => delete the row then rerank (mirrors recalc.recalc_player_monthly §6).
def recalc_ghost_player_monthly(ghost_player_id, month: datetime.date = None):
    """Recompute + persist a ghost player's monthly score, then rerank the month so the ghost gets a
    real rank interleaved with the real players.

    Reached via tasks.recalculate_ghost_player_monthly (the Celery wrapper enqueue_ghost_player
    dispatches), fired from recompute_for_leaderboard for each ghost-player participant of a counting
    solo LB.
    """
    ghost = GhostPlayer.objects.filter(pk=ghost_player_id).first()
    if not ghost:
        return
    month = (month or recalc.current_month()).replace(day=1)
    agg = compute_ghost_player_monthly(ghost, month)
    if agg.tournaments_played == 0:
        # §6 participation floor — no standalone activity this month => no row, then rerank so the
        # ghost vacates its rank slot.
        PlayerMonthlyScore.objects.filter(ghost_player=ghost, month=month).delete()
        recalc.rerank_player_month(month)
        return
    r = agg.result
    PlayerMonthlyScore.objects.update_or_create(
        ghost_player=ghost, month=month,
        defaults=dict(
            kill_pts=r.kill_pts, placement_pts=r.placement_pts, mvp_pts=r.mvp_pts,
            finals_pts=r.finals_pts, team_win_pts=r.team_win_pts, participation_pts=r.participation_pts,
            scrim_kill_pts=r.scrim_kill_pts, scrim_win_pts=r.scrim_win_pts, total_score=r.total,
            total_kills=agg.total_kills, mvp_count=agg.mvp_count, finals_appearances=agg.finals_appearances,
        ),
    )
    recalc.rerank_player_month(month)


def recalc_ghost_player_quarterly(ghost_player_id, season_id):
    """Recompute + persist a ghost player's quarterly score, then rerank the season. Mirrors
    recalc.recalc_player_quarterly MINUS the sticky-ban guard (a ghost has none).

    tier_assigned = engine.assign_tier(total, meets) with tier_source "individual": a ghost player is
    never attached to a team, so it always takes the individual tier (mirrors the unattached branch of
    engine.player_tier). The §9.2 floor is >= 1 tournament.
    """
    ghost = GhostPlayer.objects.filter(pk=ghost_player_id).first()
    season = Season.objects.filter(pk=season_id).first()
    if not (ghost and season):
        return
    agg = compute_ghost_player_quarterly(ghost, season)
    if agg.tournaments_played == 0:
        PlayerQuarterlyScore.objects.filter(ghost_player=ghost, season=season).delete()
        recalc.rerank_player_quarter(season)
        return
    r = agg.result
    meets = agg.tournaments_played >= 1  # §9.2
    tier = engine.assign_tier(r.total, meets)
    PlayerQuarterlyScore.objects.update_or_create(
        ghost_player=ghost, season=season,
        defaults=dict(
            total_score=r.total, prize_money_pts=r.prize_money_pts,
            participated_in_tournaments=agg.tournaments_played, meets_participation_floor=meets,
            tier_assigned=tier, tier_source="individual",
        ),
    )
    recalc.rerank_player_quarter(season)


# ───────────────────────── recompute entry point (signal handler target) ─────────────────────────
def _season_for(day):
    """The active Season covering `day`, else the latest active season. Replicates signals._season_for
    (kept here so this module has no dependency on signals.py, which imports tasks). Used by
    recompute_for_leaderboard to pick the season a standalone LB's results belong to."""
    if day:
        from .models import auto_rollover_seasons
        auto_rollover_seasons()  # calendar-driven activation (owner 2026-07-02)
        s = Season.objects.filter(is_active=True, start_date__lte=day, end_date__gte=day).first()
        if s:
            return s
    return Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()


def recompute_for_leaderboard(lb):
    """Enqueue a rankings recompute for EVERY participant of standalone leaderboard `lb`, for the
    LB's month + active season.

    This is the single entry point the signal receivers in signals.py call (on a result change or an
    LB save). It ALWAYS enqueues, even when the LB is not currently published+counting: toggling the
    flag off or un-publishing must still trigger a recompute so the (now non-counting) contribution is
    dropped from the score the next time aggregation runs (aggregation only reads published+counting
    LBs, so the recompute naturally removes it).

    Dispatch per participant kind:
      - real team    -> tasks.enqueue_team          (rides the event aggregation + recalc path)
      - ghost team   -> tasks.enqueue_ghost_team    (standalone-only ghost recalc above)
      - real user    -> tasks.enqueue_player        (rides the event player aggregation + recalc path)
      - ghost player -> tasks.enqueue_ghost_player  (standalone-only ghost recalc above; owner
        directive 2026-06-10 makes ghost players first-class in the player ladder, so they are no
        longer skipped)

    Lazy imports: tasks (avoid an import cycle through recalc) and the leaderboard participant model
    (load-order). Ids are de-duplicated so a participant cannot be enqueued twice.
    """
    from . import tasks
    from afc_leaderboard.models import LeaderboardParticipant

    day = lb.effective_date
    if not day:
        return
    month = day.replace(day=1)
    season = _season_for(day)
    season_id = season.season_id if season else None

    seen = set()  # ("team"|"ghost_team"|"user"|"ghost_player", id) so each entity is enqueued once
    participants = LeaderboardParticipant.objects.filter(leaderboard=lb)
    for p in participants:
        if p.team_id:
            key = ("team", p.team_id)
            if key not in seen:
                seen.add(key)
                tasks.enqueue_team(p.team_id, month, season_id)
        elif p.ghost_team_id:
            key = ("ghost_team", p.ghost_team_id)
            if key not in seen:
                seen.add(key)
                tasks.enqueue_ghost_team(p.ghost_team_id, month, season_id)
        elif p.user_id:
            key = ("user", p.user_id)
            if key not in seen:
                seen.add(key)
                tasks.enqueue_player(p.user_id, month, season_id)
        elif p.ghost_player_id:
            key = ("ghost_player", p.ghost_player_id)
            if key not in seen:
                seen.add(key)
                tasks.enqueue_ghost_player(p.ghost_player_id, month, season_id)
