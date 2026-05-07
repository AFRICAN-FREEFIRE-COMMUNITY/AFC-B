"""Auto-grader functions for the 8 preset MarketTemplates.

Each grader takes the underlying tournament data and returns
`(suggested_option_id, confidence)` or `(None, None)` when the grader can
not produce a clean call (data missing, tied, multiple flagged).

The grader_key on `MarketTemplate` selects which function runs:
    GRADERS["match_winner"](market) -> (suggested_option_id, "high")

These functions are intentionally permissive — they read whatever fields
exist on the underlying afc_tournament_and_scrims models. Missing fields
are tolerated via getattr/None checks; the admin always sees the
suggestion as optional + must confirm before money moves.

Spec Section 7 — Auto-suggestion confidence rules table.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional, Tuple

from django.db.models import Count, Sum

from afc_tournament_and_scrims.models import (
    Match,
    SoloPlayerMatchStats,
    TournamentPlayerMatchStats,
    TournamentTeamMatchStats,
)

from ..models import Market, MarketOption


GraderResult = Tuple[Optional[int], Optional[str]]  # (option_id, confidence)


def _option_for_team(market: Market, team_id: int) -> Optional[int]:
    """Find the MarketOption whose ref_team_id matches the given team."""
    if team_id is None:
        return None
    opt = MarketOption.objects.filter(
        market=market, ref_team_id=team_id
    ).first()
    return opt.pk if opt else None


def _option_for_player(market: Market, player_id: int) -> Optional[int]:
    if player_id is None:
        return None
    opt = MarketOption.objects.filter(
        market=market, ref_player_id=player_id
    ).first()
    return opt.pk if opt else None


def _option_for_numeric(market: Market, value) -> Optional[int]:
    if value is None:
        return None
    opt = MarketOption.objects.filter(
        market=market, ref_numeric=value
    ).first()
    return opt.pk if opt else None


# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------


def grade_match_winner(market: Market) -> GraderResult:
    """Read Match.winning_team_id (or fall back to placement=1 in stats)."""
    if market.match is None:
        return (None, None)
    # Try a direct field first (won't exist on current Match — Decision: rely
    # on TournamentTeamMatchStats with placement=1 instead).
    winning_team_id = getattr(market.match, "winning_team_id", None)
    if winning_team_id is None:
        # Fall back to placement=1.
        first = (
            TournamentTeamMatchStats.objects.filter(
                match=market.match, placement=1
            ).select_related("tournament_team")
            .first()
        )
        if first is not None:
            winning_team_id = first.tournament_team.team_id
    if winning_team_id is None:
        return (None, None)
    opt = _option_for_team(market, winning_team_id)
    return (opt, "high" if opt else None)


def grade_first_blood(market: Market) -> GraderResult:
    """Look for the team with the `first_blood` flag set in match stats.
    Current Match model doesn't have first_blood; tolerate gracefully."""
    if market.match is None:
        return (None, None)
    qs = TournamentTeamMatchStats.objects.filter(match=market.match)
    flagged = []
    for tts in qs.select_related("tournament_team"):
        if getattr(tts, "first_blood", False):
            flagged.append(tts.tournament_team.team_id)
    if len(flagged) != 1:
        return (None, None)
    opt = _option_for_team(market, flagged[0])
    return (opt, "high" if opt else None)


def grade_mvp(market: Market) -> GraderResult:
    """Match.mvp is a User FK on the existing Match model."""
    if market.match is None:
        return (None, None)
    mvp_id = getattr(market.match, "mvp_id", None)
    if mvp_id is None:
        return (None, None)
    opt = _option_for_player(market, mvp_id)
    return (opt, "high" if opt else "low")


def grade_most_kills(market: Market) -> GraderResult:
    """Sum kills per team across player stats. Unique max wins."""
    if market.match is None:
        return (None, None)
    by_team = defaultdict(int)
    qs = TournamentPlayerMatchStats.objects.filter(
        team_stats__match=market.match
    ).select_related("team_stats__tournament_team")
    for pms in qs:
        team_id = pms.team_stats.tournament_team.team_id
        by_team[team_id] += pms.kills or 0
    if not by_team:
        return (None, None)
    max_kills = max(by_team.values())
    leaders = [tid for tid, k in by_team.items() if k == max_kills]
    if len(leaders) != 1:
        return (None, None)
    opt = _option_for_team(market, leaders[0])
    return (opt, "high" if opt else None)


def grade_most_damage(market: Market) -> GraderResult:
    """Sum damage per team. Unique max wins."""
    if market.match is None:
        return (None, None)
    by_team = defaultdict(int)
    qs = TournamentPlayerMatchStats.objects.filter(
        team_stats__match=market.match
    ).select_related("team_stats__tournament_team")
    for pms in qs:
        team_id = pms.team_stats.tournament_team.team_id
        by_team[team_id] += pms.damage or 0
    if not by_team:
        return (None, None)
    max_damage = max(by_team.values())
    leaders = [tid for tid, d in by_team.items() if d == max_damage]
    if len(leaders) != 1:
        return (None, None)
    opt = _option_for_team(market, leaders[0])
    return (opt, "high" if opt else None)


def grade_top_3(market: Market) -> GraderResult:
    """Top-3 placement market — typically a "will Team X be top 3" boolean.
    For simplicity in v1, we resolve to the option matching the team that
    placed 1st-3rd (if placement is 1, 2, or 3 unambiguously)."""
    if market.match is None:
        return (None, None)
    qs = TournamentTeamMatchStats.objects.filter(
        match=market.match, placement__lte=3
    ).select_related("tournament_team").order_by("placement")
    placements = list(qs)
    # Need exactly 3 distinct teams in slots 1, 2, 3.
    if len(placements) != 3:
        return (None, None)
    placement_set = sorted(p.placement for p in placements)
    if placement_set != [1, 2, 3]:
        return (None, None)
    # Without a clear "which option does this market ask about" without
    # parsing market title, return None — admin manually picks. v2 can
    # improve this with parse hints on MarketOption.ref_team_id.
    return (None, None)


def grade_booyah_count(market: Market) -> GraderResult:
    """Count matches with booyah=true. Compare to ref_numeric on options."""
    if market.match is None:
        return (None, None)
    booyah_count = TournamentTeamMatchStats.objects.filter(
        match=market.match, placement=1
    ).count()
    opt = _option_for_numeric(market, booyah_count)
    return (opt, "high" if opt else None)


def grade_survival_time(market: Market) -> GraderResult:
    """Team with the longest survival_seconds. Unique max wins."""
    if market.match is None:
        return (None, None)
    longest = None
    qs = TournamentTeamMatchStats.objects.filter(match=market.match)
    by_team = defaultdict(int)
    for tts in qs.select_related("tournament_team"):
        # Field may not exist on current model — tolerate.
        secs = getattr(tts, "survival_seconds", None)
        if secs is not None:
            by_team[tts.tournament_team.team_id] = secs
    if not by_team:
        return (None, None)
    max_secs = max(by_team.values())
    leaders = [tid for tid, s in by_team.items() if s == max_secs]
    if len(leaders) != 1:
        return (None, None)
    opt = _option_for_team(market, leaders[0])
    return (opt, "high" if opt else None)


def grade_custom(market: Market) -> GraderResult:
    """Custom markets are always manual — no auto-suggestion."""
    return (None, None)


# Map of grader_key -> function. Used by the settlement queue endpoint
# (v2 will live-call this; v1 ships the registry).
GRADERS = {
    "match_winner": grade_match_winner,
    "first_blood": grade_first_blood,
    "mvp": grade_mvp,
    "most_kills": grade_most_kills,
    "most_damage": grade_most_damage,
    "top_3": grade_top_3,
    "booyah_count": grade_booyah_count,
    "survival_time": grade_survival_time,
    "custom": grade_custom,
}


def grade_market(market: Market) -> GraderResult:
    """Look up the grader by the market's template grader_key and run it.

    Returns (None, None) if the template has no grader_key (custom) or the
    underlying data isn't sufficient to make a confident call.
    """
    grader_key = (
        market.template.grader_key if market.template_id else None
    )
    if not grader_key:
        return (None, None)
    fn = GRADERS.get(grader_key)
    if fn is None:
        return (None, None)
    try:
        return fn(market)
    except Exception:
        # Auto-grading is best-effort; never block the admin queue on a
        # broken grader. Return None so admin picks manually.
        return (None, None)
