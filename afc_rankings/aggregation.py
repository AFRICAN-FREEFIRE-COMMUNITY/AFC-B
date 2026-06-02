"""
Aggregation adapter — ORM → scoring-engine inputs.

The keystone glue: queries real tournament/scrim match stats from
afc_tournament_and_scrims, recomputes raw placement points via the canonical
§4.1 table (NOT the legacy stored column), applies §12 scrim count caps, derives
win/finals from the admin-set markers, builds the engine's frozen dataclasses,
and returns the engine Result plus tiebreaker counts. The recalc layer persists.

Bucketing uses Match.played_on (falls back to match_date entry date).
Registered teams/players only — ghost-team scoring lands in Phase 3.

Driven by recalc.recalc_* (the compute_* entry points below). Reads the rankings
columns on afc_tournament_and_scrims — Match.played_on, Match.mvp,
Stages.is_finals_stage, TournamentTeam.is_tournament_winner /
TournamentTeam.finals_appearances, EventPrizePayout.amount — so changing any of
those models changes the scoring inputs here.
"""
import calendar
import datetime
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from django.db.models import Q, Sum
from django.utils import timezone

from afc_tournament_and_scrims.models import (
    TournamentTeamMatchStats, TournamentPlayerMatchStats, TournamentTeam, EventPrizePayout,
)
from afc_team.models import Team
from .scoring import engine
from .scoring.engine import TournamentInput, ScrimInput, PlayerTournamentInput, PlayerScrimInput
from .models import TeamSocialSnapshot


# ───────────────────────── date helpers ─────────────────────────
def month_bounds(month: datetime.date):
    """[first-of-month, first-of-next-month) for a date already on day 1 (or any day)."""
    start = month.replace(day=1)
    last = calendar.monthrange(start.year, start.month)[1]
    end = start + datetime.timedelta(days=last)
    return start, end


def _day_range_q(prefix: str, start: datetime.date, end: datetime.date) -> Q:
    """Filter on play day, preferring Match.played_on, else the match_date entry datetime."""
    start_dt = datetime.datetime.combine(start, datetime.time.min)
    end_dt = datetime.datetime.combine(end, datetime.time.min)
    if timezone.is_naive(start_dt):  # USE_TZ=True → compare against aware bounds
        start_dt = timezone.make_aware(start_dt)
        end_dt = timezone.make_aware(end_dt)
    return (
        Q(**{f"{prefix}__played_on__gte": start, f"{prefix}__played_on__lt": end}) |
        Q(**{f"{prefix}__played_on__isnull": True,
             f"{prefix}__match_date__gte": start_dt, f"{prefix}__match_date__lt": end_dt})
    )


def _event_of_match(match) -> Optional[object]:
    """Resolve a Match to its Event via group→stage→event, else leaderboard→event."""
    grp = getattr(match, "group", None)
    if grp and getattr(grp, "stage", None):
        return grp.stage.event
    lb = getattr(match, "leaderboard", None)
    if lb:
        return lb.event
    return None


def _match_day(match) -> Optional[datetime.date]:
    if match.played_on:
        return match.played_on
    if match.match_date:
        return match.match_date.date()
    return None


# ───────────────────────── counting controls (Result Markers, §16) ─────────────────────────
def _counting_controls(event_ids):
    """event_id → EventCountingControl for the given events. A missing row ⇒ everything counts.

    Fetched once per aggregation pass to avoid an N+1 over the (few) events an entity played.
    """
    from .models import EventCountingControl
    return {c.event_id: c for c in EventCountingControl.objects.filter(event_id__in=event_ids)}


def _excluded_event_ids(event_ids, *, team=None, player=None):
    """Set of event_ids where this team/player is opted out of counting (ResultExclusion)."""
    from .models import ResultExclusion
    qs = ResultExclusion.objects.filter(event_id__in=event_ids)
    qs = qs.filter(team=team) if team is not None else qs.filter(player=player)
    return set(qs.values_list("event_id", flat=True))


# ───────────────────────── result containers ─────────────────────────
@dataclass
class TeamAgg:
    result: object                 # engine TeamScoreResult / TeamQuarterlyResult
    tournament_wins: int
    total_kills: int
    tournaments_played: int


@dataclass
class PlayerAgg:
    result: object
    total_kills: int
    mvp_count: int
    finals_appearances: int
    tournaments_played: int


# ───────────────────────── scrim cap helper (§12) ─────────────────────────
def _apply_scrim_caps(scrim_match_rows):
    """
    scrim_match_rows: list of (day, placement, kills). Enforce 4/day + 60/month,
    then return raw (scrim_placement_pts, scrim_kills, scrim_wins) for the kept rows.
    """
    per_day = defaultdict(int)
    total = 0
    placement_pts = 0.0
    kills = 0.0
    wins = 0
    for day, placement, k in sorted(scrim_match_rows, key=lambda r: (r[0] or datetime.date.max)):
        if total >= 60:
            break
        if day is not None and per_day[day] >= 4:
            continue
        per_day[day] += 1
        total += 1
        placement_pts += engine.placement_points(placement)
        kills += k
        if placement == 1:
            wins += 1
    return placement_pts, kills, wins


# ───────────────────────── TEAM ─────────────────────────
# Reads the rankings columns on afc_tournament_and_scrims: Match.played_on,
# Stages.is_finals_stage, TournamentTeam.is_tournament_winner /
# TournamentTeam.finals_appearances, EventPrizePayout.amount. Changing those
# models changes the scoring inputs the engine sees.
def _collect_team(team: Team, start: datetime.date, end: datetime.date):
    """Returns (tournaments: list[TournamentInput], scrim_rows, win_count, kill_total)."""
    stats = (
        TournamentTeamMatchStats.objects
        .filter(tournament_team__team=team)
        .filter(_day_range_q("match", start, end))
        .select_related("match", "match__group__stage__event", "match__leaderboard__event",
                        "tournament_team", "tournament_team__event")
    )
    tour_events = defaultdict(list)     # event -> [stats]
    scrim_rows = []                     # (day, placement, kills)
    for s in stats:
        ev = _event_of_match(s.match)
        if ev is None:
            continue
        if ev.competition_type == "scrims":
            scrim_rows.append((_match_day(s.match), s.placement, s.kills))
        else:
            tour_events[ev.event_id].append((ev, s))

    # admin counting controls + per-team exclusions (§16, Result Markers surface)
    event_ids = list(tour_events.keys())
    controls = _counting_controls(event_ids)
    excluded = _excluded_event_ids(event_ids, team=team)

    tournaments = []
    win_count = 0
    kill_total = 0
    for event_id, rows in tour_events.items():
        if event_id in excluded:
            continue  # this team's results in this event are opted out of counting
        ev = rows[0][0]
        raw_placement = sum(engine.placement_points(s.placement) for _, s in rows)
        raw_kills = sum(s.kills for _, s in rows)
        tt = TournamentTeam.objects.filter(event_id=event_id, team=team).first()
        won = bool(tt and tt.is_tournament_winner)
        finals = (tt.finals_appearances if tt else 0)
        # a component the admin disabled is zeroed before the engine sees it (engine stays pure)
        ctrl = controls.get(event_id)
        if ctrl:
            if not ctrl.count_winner:
                won = False
            if not ctrl.count_placement:
                raw_placement = 0
            if not ctrl.count_kills:
                raw_kills = 0
        tournaments.append(TournamentInput(
            tier=ev.tournament_tier, raw_placement_pts=raw_placement,
            raw_kills=raw_kills, won=won, finals_appearances=finals,
        ))
        win_count += 1 if won else 0
        kill_total += raw_kills
    return tournaments, scrim_rows, win_count, kill_total


def compute_team_monthly(team: Team, month: datetime.date) -> TeamAgg:
    start, end = month_bounds(month)
    tournaments, scrim_rows, wins, kills = _collect_team(team, start, end)
    sp, sk, sw = _apply_scrim_caps(scrim_rows)
    result = engine.monthly_team_score(tournaments, ScrimInput(sp, sk, sw))
    return TeamAgg(result=result, tournament_wins=wins, total_kills=kills,
                   tournaments_played=len(tournaments))


def compute_team_quarterly(team: Team, season) -> TeamAgg:
    start, end = season.start_date, season.end_date + datetime.timedelta(days=1)
    tournaments, scrim_rows, wins, kills = _collect_team(team, start, end)
    sp, sk, sw = _apply_scrim_caps(scrim_rows)

    # prize money (§7.2) — sum payouts to this team's tournament-teams in the season window
    prize = (EventPrizePayout.objects
             .filter(tournament_team__team=team,
                     created_at__date__gte=start, created_at__date__lt=end)
             .aggregate(total=Sum("amount"))["total"] or 0)

    # social (§7.3) — quarter snapshot. Only a VERIFIED snapshot contributes points
    # (self-connect → admin verify); an unverified or absent snapshot scores 0.
    snap = TeamSocialSnapshot.objects.filter(team=team, season=season).first()
    followers = snap.combined_followers if (snap and snap.is_verified) else 0

    result = engine.quarterly_team_score(
        tournaments, ScrimInput(sp, sk, sw),
        prize_money_naira=float(prize), combined_followers=followers,
    )
    return TeamAgg(result=result, tournament_wins=wins, total_kills=kills,
                   tournaments_played=len(tournaments))


# ───────────────────────── PLAYER ─────────────────────────
def _collect_player(player, start: datetime.date, end: datetime.date):
    """Build per-tournament PlayerTournamentInput list + scrim rows for a player."""
    pstats = (
        TournamentPlayerMatchStats.objects
        .filter(player=player, played=True)
        .filter(_day_range_q("team_stats__match", start, end))
        .select_related("team_stats", "team_stats__match",
                        "team_stats__match__group__stage", "team_stats__match__group__stage__event",
                        "team_stats__match__leaderboard__event", "team_stats__tournament_team")
    )
    tour = defaultdict(lambda: {"kills": 0, "ev": None, "finals": 0, "team_won": False, "mvp": 0})
    scrim_rows = []   # (day, kills, is_win)
    for ps in pstats:
        match = ps.team_stats.match
        ev = _event_of_match(match)
        if ev is None:
            continue
        if ev.competition_type == "scrims":
            scrim_rows.append((_match_day(match), ps.kills, ps.team_stats.placement == 1))
            continue
        bucket = tour[ev.event_id]
        bucket["ev"] = ev
        bucket["kills"] += ps.kills
        # finals appearance: played a match in a finals stage
        grp = getattr(match, "group", None)
        stage = grp.stage if grp else None
        if stage and getattr(stage, "is_finals_stage", False):
            bucket["finals"] += 1
        # team win for this tournament
        tt = ps.team_stats.tournament_team
        if tt and tt.is_tournament_winner:
            bucket["team_won"] = True
        # MVP for this match
        if match.mvp_id == getattr(player, "user_id", None):
            bucket["mvp"] += 1

    # admin counting controls + per-player exclusions (§16)
    event_ids = list(tour.keys())
    controls = _counting_controls(event_ids)
    excluded = _excluded_event_ids(event_ids, player=player)

    tournaments = []
    mvp_total = finals_total = kill_total = 0
    for event_id, b in tour.items():
        if event_id in excluded:
            continue  # this player's results in this event are opted out of counting
        ev = b["ev"]
        kills = b["kills"]
        team_won = b["team_won"]
        ctrl = controls.get(event_id)
        if ctrl:
            if not ctrl.count_kills:
                kills = 0
            if not ctrl.count_winner:
                team_won = False
            # count_placement: players score on kills/mvp/finals/team-win/participation,
            # not raw placement (personal_placement_pts is already 0) — nothing to zero here.
        tournaments.append(PlayerTournamentInput(
            tier=ev.tournament_tier, personal_kills=kills, personal_placement_pts=0,
            mvp_count=b["mvp"], finals_appearances=b["finals"],
            team_won=team_won, participated=True,
        ))
        mvp_total += b["mvp"]
        finals_total += b["finals"]
        kill_total += kills
    return tournaments, scrim_rows, mvp_total, finals_total, kill_total


def compute_player_monthly(player, month: datetime.date) -> PlayerAgg:
    start, end = month_bounds(month)
    tournaments, scrim_rows, mvp, finals, kills = _collect_player(player, start, end)
    s_kills = sum(k for _, k, _ in scrim_rows)
    s_wins = sum(1 for _, _, win in scrim_rows if win)
    result = engine.monthly_player_score(tournaments, PlayerScrimInput(scrim_kills=s_kills, scrim_wins=s_wins))
    return PlayerAgg(result=result, total_kills=kills, mvp_count=mvp,
                     finals_appearances=finals, tournaments_played=len(tournaments))


def compute_player_quarterly(player, season) -> PlayerAgg:
    start, end = season.start_date, season.end_date + datetime.timedelta(days=1)
    tournaments, scrim_rows, mvp, finals, kills = _collect_player(player, start, end)
    s_kills = sum(k for _, k, _ in scrim_rows)
    s_wins = sum(1 for _, _, win in scrim_rows if win)
    # inherited prize money — payouts to any team the player was rostered on (Phase 1: via tournament_team membership)
    prize = (EventPrizePayout.objects
             .filter(tournament_team__members__user=player,
                     created_at__date__gte=start, created_at__date__lt=end)
             .aggregate(total=Sum("amount"))["total"] or 0)
    result = engine.quarterly_player_score(
        tournaments, PlayerScrimInput(scrim_kills=s_kills, scrim_wins=s_wins),
        inherited_prize_money_naira=float(prize),
    )
    return PlayerAgg(result=result, total_kills=kills, mvp_count=mvp,
                     finals_appearances=finals, tournaments_played=len(tournaments))
