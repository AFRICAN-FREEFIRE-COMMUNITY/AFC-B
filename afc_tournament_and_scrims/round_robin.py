"""
Pure helpers for the BR Round-Robin stage format (sub-project B).

A Round-Robin stage keeps *base groups* (A/B/C…) as the stable team identity, and
forms each game-day *lobby* by merging two base groups. The round-robin schedule is
therefore every unordered pairing of base groups, one pairing per game-day — the
direct analogue of a sports round-robin, but over groups-of-teams instead of teams.

This module hosts the schedule *generator* only (Task 2). It is deliberately pure:
group ids in → plain lobby-spec dicts out, no ORM, no side effects. Keeping it
DB-free lets create_event / edit_event call it to plan the schedule, then materialise
the specs into StageGroups rows themselves (Task 4), and lets it be unit-tested
without a database. Aggregation/standings helpers land in later tasks.

Spec: WEBSITE/tasks/round-robin-design.md.
"""
from itertools import combinations

# Default lobby map when the caller doesn't specify maps. BR lobbies always carry at
# least one map; Bermuda is the standard Free Fire BR map used across the codebase.
DEFAULT_MAPS = ["bermuda"]


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
    specs = []
    # `combinations(..., 2)` yields each unordered pair once; enumerate from 1 so
    # game_day is human-facing 1-based (Day 1, Day 2, …) to match the UI / lobby labels.
    for day, (g1, g2) in enumerate(combinations(group_ids, 2), start=1):
        specs.append({
            "game_day": day,
            "source_group_ids": [g1, g2],
            "match_count": games_per_day,
            # `list(...)` copies per spec: each lobby owns its own maps list so a later
            # edit to one lobby can't alias the caller's input or a sibling spec.
            "match_maps": list(maps or DEFAULT_MAPS),
        })
    return specs
