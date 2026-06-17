"""
Pure helpers for the BR Round-Robin stage format (sub-project B).

A Round-Robin stage keeps *base groups* (A/B/C…) as the stable team identity, and
forms each game-day *lobby* by merging two base groups. The round-robin schedule is
therefore every unordered pairing of base groups, one pairing per game-day — the
direct analogue of a sports round-robin, but over groups-of-teams instead of teams.

This module hosts:
  • the schedule *generator* (`round_robin_schedule`, Task 2) — deliberately pure: group
    ids in → plain lobby-spec dicts out, no ORM, so create_event / edit_event can plan a
    schedule and materialise it into StageGroups rows themselves (Task 4), and so it stays
    unit-testable without a database; and
  • the read-time *standings* aggregators (`cumulative_standings` / `day_standings`,
    Task 3) — these DO touch the ORM (they read `TournamentTeamMatchStats`) but write
    nothing: they fold the same per-match team stats the leaderboard view uses into a
    whole-stage (cumulative) or single-game-day (per-day) points table.

Spec: WEBSITE/tasks/round-robin-design.md.
"""
from itertools import combinations

from django.db.models import Case, Count, F, IntegerField, Sum, Value, When
from django.db.models.functions import Coalesce

from .models import TournamentTeamMatchStats

# Default lobby map when the caller doesn't specify maps. BR lobbies always carry at
# least one map; Bermuda is the standard Free Fire BR map used across the codebase.
# Capitalised to match the FE AVAILABLE_MAPS labels ("Bermuda"…) so an auto-generated
# schedule's maps line up with the meeting editor's map stepper (owner 2026-06-17: a
# lowercase "bermuda" default rendered as 0 maps under the capitalized "Bermuda" stepper).
DEFAULT_MAPS = ["Bermuda"]


def round_robin_schedule(group_ids, games_per_day=1, maps=None):
    """Plan a round-robin schedule over base groups.

    Each unordered pairing of base groups becomes exactly one game-day lobby, ordered
    by `itertools.combinations` (so for [A, B, C] → A+B, A+C, B+C). Pure: ids in,
    lobby-spec dicts out — the caller materialises these into StageGroups rows.

    Args:
        group_ids:     ordered base-group ids (e.g. RoundRobinGroup pks, A→B→C).
        games_per_day: matches to run in each lobby → spec["match_count"].
        maps:          map list applied to every lobby; defaults to ["bermuda"].

    Returns:
        list of spec dicts, one per pairing, each:
          {game_day, source_group_ids: [g1, g2], match_count, match_maps}
        Game-days are numbered 1..C(n, 2). Fewer than two groups → [] (nothing to merge).
    """
    # One match per map (owner 2026-06-17): a meeting plays `games_per_day` matches, each on its
    # OWN map, so `match_count` and `len(match_maps)` stay in lock-step. The FE meeting editor
    # derives the match count from the maps list, so when these drifted (count=3 but a single
    # ["bermuda"] map) a "3 matches per meeting" setting rendered — and re-saved — as 1, which is
    # the "matches per meeting always changes" bug. We expand the supplied maps to exactly
    # `games_per_day` entries, cycling them when fewer maps than matches are given
    # (e.g. 3 matches over ["bermuda","kalahari"] → bermuda, kalahari, bermuda).
    base_maps = list(maps) if maps else list(DEFAULT_MAPS)
    count = max(int(games_per_day or 0), 0)
    day_maps = [base_maps[i % len(base_maps)] for i in range(count)] if base_maps else []

    specs = []
    # `combinations(..., 2)` yields each unordered pair once; enumerate from 1 so
    # game_day is human-facing 1-based (Day 1, Day 2, …) to match the UI / lobby labels.
    for day, (g1, g2) in enumerate(combinations(group_ids, 2), start=1):
        specs.append({
            "game_day": day,
            "source_group_ids": [g1, g2],
            # count == len(match_maps): kept identical so the round-trip can't desync them.
            "match_count": len(day_maps),
            # `list(...)` copies per spec: each lobby owns its own maps list so a later
            # edit to one lobby can't alias the caller's input or a sibling spec.
            "match_maps": list(day_maps),
        })
    return specs


def _aggregate_team_standings(stats_qs):
    """Fold a TournamentTeamMatchStats queryset into a per-team points table.

    Shared core of `cumulative_standings` (whole stage) and `day_standings` (one game
    day) — both differ ONLY in which match-stats rows they feed in, so the grouping,
    points formula and sort live here once. The aggregation mirrors the per-group OVERALL
    block in `get_all_leaderboard_details_for_event` exactly, so a team's number is the
    same whether read per-lobby, per-day or cumulatively:

      effective_total = Σplacement + Σkill + Σbonus − Σpenalty   (the authoritative score)
      tiebreakers     = −effective_total, −total_booyah, −total_kills   (same chain, DB-side)

    Grouping by `tournament_team` is what makes a team that plays MORE THAN ONE lobby
    collapse to a single summed row — the whole point of cumulative standings (a team gets
    one lobby per game day, so without this each day would be a separate row).

    Returns a list of plain dicts (so callers can JSON it straight out). Each row carries
    `tournament_team_id` for internal use (advancement seeding in Task 5) plus the public
    `team_name` + stat fields; no other raw PKs leak.
    """
    rows = (
        stats_qs
        # Group by team; surface the human name via F() so the dict is UI-ready.
        .values("tournament_team_id", team_name=F("tournament_team__team__team_name"))
        .annotate(
            # games_played = matches this team has stats in within the fed-in slice.
            games_played=Count("match_id"),
            total_kills=Coalesce(Sum("kills"), 0),
            # Booyahs = matches finished 1st; Case/When mirrors the leaderboard view.
            total_booyah=Coalesce(Sum(
                Case(
                    When(placement=1, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ), 0),
            # Point columns surfaced SEPARATELY so a leaderboard-design graphic can place a
            # placement-points (PP) and a kill-points (KP) column independently of the total
            # (owner 2026-06-14). effective_total stays the authoritative score below.
            placement_sum=Coalesce(Sum("placement_points"), 0),
            kill_sum=Coalesce(Sum("kill_points"), 0),
            bonus_sum=Coalesce(Sum("bonus_points"), 0),
            penalty_sum=Coalesce(Sum("penalty_points"), 0),
            effective_total=(
                Coalesce(Sum("placement_points"), 0)
                + Coalesce(Sum("kill_points"), 0)
                + Coalesce(Sum("bonus_points"), 0)
                - Coalesce(Sum("penalty_points"), 0)
            ),
        )
        # Keep the server sort authoritative (FE renders verbatim) and identical to the
        # per-lobby leaderboard's lead tiebreakers: points, then booyahs, then kills, then
        # name for a stable final order.
        .order_by("-effective_total", "-total_booyah", "-total_kills", "team_name")
    )
    return list(rows)


def cumulative_standings(stage):
    """Whole-stage standings: every team summed across ALL of the stage's lobbies.

    This is the round-robin table the format is built around — a raw cumulative points
    table over the entire stage (Champion-Point / Point-Rush overlays stay per-lobby and
    are NOT applied here, per spec). `match__group__stage=stage` walks
    TournamentTeamMatchStats → Match → StageGroups(lobby) → Stages, so it picks up every
    lobby of the stage regardless of game day.
    """
    qs = TournamentTeamMatchStats.objects.filter(match__group__stage=stage)
    return _aggregate_team_standings(qs)


def group_standings(group):
    """Whole-GROUP standings: every team summed across only THAT group's (lobby's) matches.

    Same aggregate as `cumulative_standings`, but scoped to one StageGroups via
    `match__group=group` — identical to the per-group "Overall Leaderboard" filter in
    `get_all_leaderboard_details_for_event` (views.py). The graphic export uses this when a
    group is selected so the exported image matches EXACTLY what the user sees on the page
    (owner 2026-06-16: export showed the design background but no rows because the stage-wide
    query missed the data the per-group page view was showing).
    """
    qs = TournamentTeamMatchStats.objects.filter(match__group=group)
    return _aggregate_team_standings(qs)


def day_standings(stage, game_day):
    """Single-game-day standings: teams summed across only that day's lobbies.

    Same aggregate as `cumulative_standings`, but additionally filtered to one
    `StageGroups.game_day`, so a team that also played other days shows ONLY this day's
    points here (the per-day slice the Day↔Cumulative toggle renders).
    """
    qs = TournamentTeamMatchStats.objects.filter(
        match__group__stage=stage, match__group__game_day=game_day)
    return _aggregate_team_standings(qs)
