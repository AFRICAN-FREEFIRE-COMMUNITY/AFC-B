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


# ── Config-driven TIE-BREAKERS (owner 2026-07-02) ───────────────────────────────
# The hardcoded chain (points -> booyahs -> kills) becomes ARRANGEABLE, exactly like the MVP
# criteria: the admin/organizer orders team criteria and they apply to the whole event, one stage,
# or one group (group > stage > event default; nothing configured = the legacy chain, so every
# existing leaderboard is unchanged). effective_total ALWAYS ranks first - tie-breakers only order
# equal-point teams. "mvp_count" counts how many per-map MVPs a team's players won (views_mvp
# semantics), computed lazily only when the criterion is actually configured.
TIE_BREAKER_KEYS = (
    "booyahs", "kills", "placement_points", "kill_points",
    "bonus", "fewest_penalties", "matches_played", "mvp_count",
)
_ROW_FIELD = {
    "booyahs": "total_booyah", "kills": "total_kills", "placement_sum": "placement_sum",
    "placement_points": "placement_sum", "kill_points": "kill_sum", "bonus": "bonus_sum",
    "fewest_penalties": "penalty_sum", "matches_played": "games_played",
}


def _resolve_tie_breakers(event, stage=None, group=None):
    """group > stage > event default. Returns [] when nothing configured (legacy chain)."""
    cfg = (getattr(event, "tie_breakers", None) or {}) if event else {}
    if group is not None:
        v = (cfg.get("groups") or {}).get(str(getattr(group, "group_id", group)))
        if v:
            return [c for c in v if c in TIE_BREAKER_KEYS]
    if stage is not None:
        v = (cfg.get("stages") or {}).get(str(getattr(stage, "stage_id", stage)))
        if v:
            return [c for c in v if c in TIE_BREAKER_KEYS]
    return [c for c in (cfg.get("default") or []) if c in TIE_BREAKER_KEYS]


def _team_mvp_counts(event):
    """{tournament_team_id: per-map MVP wins by that team's players} - the owner's "mvp is a
    tie-breaker criterion". Mirrors views_mvp: per MATCH, the best player line by the event's MVP
    criteria; the winner's TEAM gets the count."""
    from .views_mvp import CRITERIA_META, DEFAULT_CRITERIA, _crit_key
    from .models import TournamentPlayerMatchStats
    cfg = (getattr(event, "mvp_config", None) or {})
    rankable = [c for c in (cfg.get("criteria") or DEFAULT_CRITERIA)
                if c in CRITERIA_META and CRITERIA_META[c][1]] or DEFAULT_CRITERIA
    best_by_match = {}
    for s in TournamentPlayerMatchStats.objects.filter(
        team_stats__match__group__stage__event=event
    ).select_related("team_stats"):
        line = {"kills": s.kills or 0, "damage": s.damage or 0, "assists": s.assists or 0}
        key = _crit_key(line, rankable)
        cur = best_by_match.get(s.team_stats.match_id)
        if cur is None or key > cur[0]:
            best_by_match[s.team_stats.match_id] = (key, s.team_stats.tournament_team_id)
    counts = {}
    for _k, tt in best_by_match.values():
        counts[tt] = counts.get(tt, 0) + 1
    return counts


def apply_tie_breakers(rows, event, stage=None, group=None):
    """Re-sort aggregated standings rows by effective_total then the CONFIGURED criteria chain.
    No config -> rows unchanged (the aggregator's legacy DB sort already applied)."""
    criteria = _resolve_tie_breakers(event, stage, group)
    if not criteria or not rows:
        return rows
    mvp_counts = _team_mvp_counts(event) if "mvp_count" in criteria else {}

    def key(r):
        parts = [-(r.get("effective_total") or 0)]
        for c in criteria:
            if c == "mvp_count":
                parts.append(-(mvp_counts.get(r.get("tournament_team_id"), 0)))
            elif c == "fewest_penalties":
                parts.append(r.get("penalty_sum") or 0)   # fewer penalties = better (ascending)
            else:
                # games_played (aggregator rows) vs matches_played (the event-leaderboard endpoint's
                # rows) name the same stat - accept either.
                f = _ROW_FIELD.get(c, c)
                parts.append(-((r.get(f) if r.get(f) is not None else r.get("matches_played")) or 0))
        parts.append(r.get("team_name") or "")
        return tuple(parts)

    return sorted(rows, key=key)


def _aggregate_team_standings(stats_qs, event=None, stage=None, group=None):
    """Fold a TournamentTeamMatchStats queryset into a per-team points table.

    Shared core of `cumulative_standings` (whole stage) and `day_standings` (one game
    day) — both differ ONLY in which match-stats rows they feed in, so the grouping,
    points formula and sort live here once. The aggregation mirrors the per-group OVERALL
    block in `get_all_leaderboard_details_for_event` exactly, so a team's number is the
    same whether read per-lobby, per-day or cumulatively:

      effective_total = Σtotal_points = Σ(placement + kill + assist + damage + bonus − penalty)
                        (the stored per-match total summed; the authoritative score)
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
            # effective_total = the authoritative TEAM score = the stored per-match total_points
            # summed (placement + kill + ASSIST + DAMAGE + bonus - penalty). It previously re-derived
            # the total from placement+kill+bonus-penalty only, which DROPPED assist/damage points and
            # so disagreed with the public leaderboard + advance_group (both rank on the stored
            # total_points). Summing the stored column unifies every team-ranking surface (this
            # aggregator feeds advance_round_robin, advancement_routing, and event_links) and includes
            # assist/damage (owner 2026-06-29 point-rush metric split). SOLO is unaffected: its stored
            # total_points is already placement+kill only. The placement/kill/bonus/penalty sums stay
            # as independent display columns; the sort key below still orders on effective_total.
            effective_total=Coalesce(Sum("total_points"), 0),
        )
        # Keep the server sort authoritative (FE renders verbatim) and identical to the
        # per-lobby leaderboard's lead tiebreakers: points, then booyahs, then kills, then
        # name for a stable final order.
        .order_by("-effective_total", "-total_booyah", "-total_kills", "team_name")
    )
    # Config-driven tie-breakers (owner 2026-07-02): when the event has an arrangement for this
    # scope, re-sort equal-point teams by it; otherwise the DB order above stands (legacy chain).
    return apply_tie_breakers(list(rows), event, stage, group)


def cumulative_standings(stage):
    """Whole-stage standings: every team summed across ALL of the stage's lobbies.

    This is the round-robin table the format is built around — a raw cumulative points
    table over the entire stage (Champion-Point / Point-Rush overlays stay per-lobby and
    are NOT applied here, per spec). `match__group__stage=stage` walks
    TournamentTeamMatchStats → Match → StageGroups(lobby) → Stages, so it picks up every
    lobby of the stage regardless of game day.
    """
    qs = TournamentTeamMatchStats.objects.filter(match__group__stage=stage)
    return _aggregate_team_standings(qs, event=stage.event, stage=stage)


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
    return _aggregate_team_standings(qs, event=group.stage.event, stage=group.stage, group=group)


def day_standings(stage, game_day):
    """Single-game-day standings: teams summed across only that day's lobbies.

    Same aggregate as `cumulative_standings`, but additionally filtered to one
    `StageGroups.game_day`, so a team that also played other days shows ONLY this day's
    points here (the per-day slice the Day↔Cumulative toggle renders).
    """
    qs = TournamentTeamMatchStats.objects.filter(
        match__group__stage=stage, match__group__game_day=game_day)
    return _aggregate_team_standings(qs, event=stage.event, stage=stage)
