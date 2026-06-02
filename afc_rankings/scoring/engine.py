"""AFC Ranking & Tiering — pure scoring functions.

Every function maps to a spec section (cited in its docstring). The module is
pure: no Django, no ORM, no I/O, no global mutable state. Same input -> same
output, always. This Django-free purity is a hard requirement — do not import
Django here.

Built and called exclusively by afc_rankings/aggregation.py (_collect_team /
_collect_player), which feeds these functions the frozen input dataclasses;
afc_rankings/recalc.py then persists the returned Result objects into the score
models.

Compression granularity (LOCKED CONVENTION, resolves spec FLAG A):
    Compression (kills AND placement) is applied PER-TOURNAMENT, then the
    per-tournament scores are summed. This holds for BOTH monthly and
    quarterly. Quarterly re-runs the raw per-tournament data across the full
    3-month window through the identical per-tournament path; it NEVER sums
    already-computed monthly totals. The same per-tournament rule is applied to
    the player path so team and player scoring stay symmetric.

    The spec §4.2 "120 cumulative kills -> 12" example is therefore asserted
    against the ``compress_kills`` PRIMITIVE directly (which is unambiguous),
    not against a multi-tournament aggregate.

Participation floors (spec §5.2, §7.4, §9.2) are NOT enforced here — the score
functions are pure arithmetic. The caller passes an explicit ``meets_floor``
flag to ``assign_tier`` / ``player_tier`` where the floor matters.

The §12 daily (4/day) and monthly (60/month) scrim COUNT caps are enforced
UPSTREAM by the aggregation subsystem when it builds ``ScrimInput`` — only the
30%-of-tournament-total POINTS cap lives here (it depends on the tournament
total, which only the engine knows). The count-cap constants are exported from
``constants.py`` so the aggregation layer reads them from one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    FINALS_BASE,
    KILL_COMPRESSION,
    PLACEMENT_COMPRESSION,
    PLACEMENT_POINTS,
    PLAYER_FINALS_PTS,
    PLAYER_MVP_PTS,
    PLAYER_PARTICIPATION_PTS,
    PLAYER_SCRIM_KILL_WEIGHT,
    PLAYER_SCRIM_WIN_PTS,
    PLAYER_TEAM_WIN_PTS,
    PRIZE_MONEY_POINTS,
    SCRIM_CAP_RATIO,
    SCRIM_WEIGHT,
    SCRIM_WIN_FLAT,
    SOCIAL_MEDIA_POINTS,
    TIER_DEFAULT,
    TIER_MULTIPLIER,
    TIER_THRESHOLDS,
    WIN_BONUS,
)

TierStr = str  # "tier_1" | "tier_2" | "tier_3" (matches Event.tournament_tier)


# ===========================================================================
# Input dataclasses — the I/O contract the aggregation subsystem builds.
# ===========================================================================
@dataclass(frozen=True)
class TournamentInput:
    """One tournament's already-aggregated raw inputs for ONE team.

    The caller (aggregation layer) decides ``won`` / ``finals_appearances`` from
    stage/match structure; the engine never parses what counts as a win.

    Spec §5.1 Step 1.
    """

    tier: TierStr             # "tier_1" | "tier_2" | "tier_3"
    raw_placement_pts: int    # Σ per-match placement points (§4.1) for this tournament
    raw_kills: int            # Σ kills across all matches in this tournament
    won: bool = False         # team won this tournament
    finals_appearances: int = 0  # count of finals reached in this tournament (§4.5)


@dataclass(frozen=True)
class ScrimInput:
    """Already day/month-capped scrim aggregate for a team (§12 count caps
    applied upstream). Spec §5.1 Step 3.
    """

    scrim_placement_pts: float = 0.0  # Σ raw placement pts across counted scrims
    scrim_kills: float = 0.0          # Σ kills across counted scrims
    scrim_wins: int = 0               # count of counted scrim wins


@dataclass(frozen=True)
class PlayerTournamentInput:
    """One tournament's already-aggregated personal inputs for ONE player.

    Spec §7.1 (monthly metrics) + §9.1 (quarterly personal placement points).
    """

    tier: TierStr
    personal_kills: int = 0          # personal kills in this tournament (compressed per-tournament)
    personal_placement_pts: int = 0  # Σ personal per-match placement points (§4.1 / §9.1)
    mvp_count: int = 0               # §7: 5 pts each
    finals_appearances: int = 0      # §7: 3 pts each (player in lineup)
    team_won: bool = False           # §7: 5 pts per team win
    participated: bool = False       # §7: 1 pt if player played >= 1 match


@dataclass(frozen=True)
class PlayerScrimInput:
    """Already-capped personal scrim aggregate. Spec §7.1 (scrim rows)."""

    scrim_kills: float = 0.0  # personal scrim kills (compressed, then x0.5)
    scrim_wins: int = 0       # §7: 1 pt per scrim win while in lineup


# ===========================================================================
# Result dataclasses — mirror the DB score-model field breakdowns (§19).
# ===========================================================================
@dataclass(frozen=True)
class TeamScoreResult:
    """Monthly team score breakdown — mirrors TeamMonthlyScore (§19.5)."""

    tournament_pts: float
    scrim_pts: float
    total: float


@dataclass(frozen=True)
class TeamQuarterlyResult:
    """Quarterly team score breakdown — mirrors TeamQuarterlyScore (§19.6)."""

    tournament_pts: float
    scrim_pts: float
    prize_money_pts: float
    social_media_pts: float
    total: float


@dataclass(frozen=True)
class PlayerScoreResult:
    """Monthly player score breakdown — mirrors PlayerMonthlyScore (§19.7)."""

    kill_pts: float
    placement_pts: float
    mvp_pts: float
    finals_pts: float
    team_win_pts: float
    participation_pts: float
    scrim_kill_pts: float
    scrim_win_pts: float
    total: float


@dataclass(frozen=True)
class PlayerQuarterlyResult:
    """Quarterly player score breakdown — mirrors PlayerQuarterlyScore (§19.8).

    ``prize_money_pts`` is inherited from the team(s) the player rostered for
    (spec §6.2 / §9.1) and is only applied at the quarterly level.
    """

    kill_pts: float
    placement_pts: float
    mvp_pts: float
    finals_pts: float
    team_win_pts: float
    participation_pts: float
    scrim_kill_pts: float
    scrim_win_pts: float
    prize_money_pts: float
    total: float


# ===========================================================================
# Bracket-lookup primitive
# ===========================================================================
def _bracket_lookup(
    table: tuple[tuple[int | None, int], ...], value: float
) -> int:
    """Return the points of the first bracket whose upper bound is open (None)
    or whose ``value <= upper_bound``.

    The table must be ascending by upper bound with the final entry's upper
    bound == None (open top). Inclusive upper bounds; the next band covers any
    1-unit gap in the spec's published bands.
    """
    for upper, points in table:
        if upper is None or value <= upper:
            return points
    # Unreachable when the table ends with an open (None) top bracket.
    raise ValueError("bracket table has no open top bracket")


# ===========================================================================
# Compression / lookup scales
# ===========================================================================
def compress_kills(raw_kills: float) -> int:
    """Compress a raw kill total to its bracket points. Spec §4.2.

    The bracket determines the value (not additive): 120 raw kills -> 12.

    Zero-stat floor (product decision): raw == 0 returns 0 — a no-kill
    appearance scores nothing. Any raw > 0 uses the bracket table unchanged
    (1 -> 3, 50 -> 3, 51 -> 7, ...).
    """
    if raw_kills == 0:
        return 0
    return _bracket_lookup(KILL_COMPRESSION, raw_kills)


def compress_placement(raw_placement_pts: float) -> int:
    """Compress a raw placement-points total to its bracket points. Spec §4.3.

    Zero-stat floor (product decision): raw == 0 returns 0. Any raw > 0 uses the
    bracket table unchanged (1 -> 5, 50 -> 5, 51 -> 10, ...).
    """
    if raw_placement_pts == 0:
        return 0
    return _bracket_lookup(PLACEMENT_COMPRESSION, raw_placement_pts)


def prize_money_points(total_naira: float) -> int:
    """Points for total prize money won (₦) across the quarter. Spec §7.2."""
    return _bracket_lookup(PRIZE_MONEY_POINTS, total_naira)


def social_media_points(combined_followers: float) -> int:
    """Points for combined IG+TikTok followers (capped at 10). Spec §7.3."""
    return _bracket_lookup(SOCIAL_MEDIA_POINTS, combined_followers)


# ===========================================================================
# Building blocks
# ===========================================================================
def placement_points(finish: int) -> int:
    """Raw placement points for a single match finish. Spec §4.1.

    Finishes 11th+ award 0. This is the canonical mapping — callers must NOT
    trust any legacy ``placement_points`` column on existing models.
    """
    return PLACEMENT_POINTS.get(finish, 0)


def tier_multiplier(tier: TierStr) -> float:
    """Tournament tier multiplier. Spec §4. Raises ValueError on unknown tier."""
    try:
        return TIER_MULTIPLIER[tier]
    except KeyError:
        raise ValueError(f"unknown tournament tier: {tier!r}") from None


def win_bonus(tier: TierStr) -> int:
    """Flat win bonus for the tournament winner (not multiplied). Spec §4.4.

    Raises ValueError on unknown tier.
    """
    try:
        return WIN_BONUS[tier]
    except KeyError:
        raise ValueError(f"unknown tournament tier: {tier!r}") from None


def finals_bonus(tier: TierStr, appearances: int = 1) -> float:
    """Finals appearance bonus = 5 * tier_multiplier * appearances. Spec §4.5."""
    return FINALS_BASE * tier_multiplier(tier) * appearances


# ===========================================================================
# Per-tournament team score — spec §5.1 Step 1
# ===========================================================================
def tournament_score(t: TournamentInput) -> float:
    """Score for ONE tournament for a team. Spec §5.1 Step 1.

        tournament_score = (compress_placement(raw_placement)
                            + compress_kills(raw_kills)) * tier_multiplier
                         + win_bonus           (if won)
                         + 5 * tier_multiplier * finals_appearances

    The tier multiplier applies to placement, kills, and finals — NOT to the
    flat win bonus (spec §4).
    """
    mult = tier_multiplier(t.tier)
    base = (compress_placement(t.raw_placement_pts) + compress_kills(t.raw_kills)) * mult
    bonus = win_bonus(t.tier) if t.won else 0
    finals = finals_bonus(t.tier, t.finals_appearances)
    return base + bonus + finals


# ===========================================================================
# Scrims — spec §5.1 Step 3 + §12
# ===========================================================================
def raw_scrim_points(s: ScrimInput) -> float:
    """Raw (uncapped) scrim points for a team. Spec §5.1 Step 3 / §12.

        raw = scrim_placement * 0.5 + scrim_kills * 0.5 + scrim_wins * 3
    """
    return (
        s.scrim_placement_pts * SCRIM_WEIGHT
        + s.scrim_kills * SCRIM_WEIGHT
        + s.scrim_wins * SCRIM_WIN_FLAT
    )


def capped_scrim_points(raw_scrim: float, total_tournament_pts: float) -> float:
    """Cap scrim contribution at 30% of the tournament total. Spec §5.1 Step 3.

    With zero tournament points the cap is 0, so scrims cannot bank credit for a
    team with no tournament activity (reinforces the participation floor §5.2).
    """
    return min(raw_scrim, total_tournament_pts * SCRIM_CAP_RATIO)


# ===========================================================================
# Team aggregates — spec §6 (monthly) + §8 (quarterly)
# ===========================================================================
def monthly_team_score(
    tournaments: list[TournamentInput],
    scrims: ScrimInput | None = None,
) -> TeamScoreResult:
    """Monthly team score. Spec §6.

    Sums per-tournament scores, then adds the 30%-capped scrim contribution.
    """
    total_tournament_pts = sum(tournament_score(t) for t in tournaments)
    raw = raw_scrim_points(scrims) if scrims is not None else 0.0
    counted = capped_scrim_points(raw, total_tournament_pts)
    return TeamScoreResult(
        tournament_pts=total_tournament_pts,
        scrim_pts=counted,
        total=total_tournament_pts + counted,
    )


def quarterly_team_prize_money_points(prize_money_naira: float) -> int:
    """Prize-money points for the quarter. Spec §7.2 (thin alias of the scale)."""
    return prize_money_points(prize_money_naira)


def quarterly_team_social_media_points(combined_followers: float) -> int:
    """Social-media points for the quarter (max 10). Spec §7.3 (thin alias)."""
    return social_media_points(combined_followers)


def quarterly_team_score(
    tournaments: list[TournamentInput],
    scrims: ScrimInput | None = None,
    prize_money_naira: float = 0.0,
    combined_followers: float = 0,
) -> TeamQuarterlyResult:
    """Quarterly team score. Spec §8.

    Uses the SAME per-tournament formula as monthly (§8.1) over the full
    3-month raw dataset (the ``tournaments`` list spans all 3 months), then adds
    prize money (§7.2) and social media (§7.3). Tier assignment / participation
    floor are NOT applied here — see ``assign_tier``.
    """
    base = monthly_team_score(tournaments, scrims)  # §8.1 "same formula as monthly"
    prize = quarterly_team_prize_money_points(prize_money_naira)
    social = quarterly_team_social_media_points(combined_followers)
    return TeamQuarterlyResult(
        tournament_pts=base.tournament_pts,
        scrim_pts=base.scrim_pts,
        prize_money_pts=prize,
        social_media_pts=social,
        total=base.total + prize + social,
    )


# ===========================================================================
# Player aggregates — spec §7 (monthly) + §9 (quarterly)
# ===========================================================================
def _player_components(
    tournaments: list[PlayerTournamentInput],
    scrims: PlayerScrimInput | None,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Shared component computation for monthly & quarterly player scoring.

    Per-tournament compression (kills AND placement) then summed — matches the
    locked team-path convention so the two stay symmetric. MVP/finals/team-win/
    participation are flat per spec §7 (and §2: team win = 5, not 20).
    """
    kill_pts = sum(compress_kills(t.personal_kills) for t in tournaments)
    placement_pts_total = sum(
        compress_placement(t.personal_placement_pts) for t in tournaments
    )
    mvp_pts = PLAYER_MVP_PTS * sum(t.mvp_count for t in tournaments)
    finals_pts = PLAYER_FINALS_PTS * sum(t.finals_appearances for t in tournaments)
    team_win_pts = PLAYER_TEAM_WIN_PTS * sum(1 for t in tournaments if t.team_won)
    participation_pts = PLAYER_PARTICIPATION_PTS * sum(
        1 for t in tournaments if t.participated
    )
    if scrims is not None:
        scrim_kill_pts = PLAYER_SCRIM_KILL_WEIGHT * compress_kills(scrims.scrim_kills)
        scrim_win_pts = PLAYER_SCRIM_WIN_PTS * scrims.scrim_wins
    else:
        scrim_kill_pts = 0.0
        scrim_win_pts = 0.0
    return (
        kill_pts,
        placement_pts_total,
        mvp_pts,
        finals_pts,
        team_win_pts,
        participation_pts,
        scrim_kill_pts,
        scrim_win_pts,
    )


def monthly_player_score(
    tournaments: list[PlayerTournamentInput],
    scrims: PlayerScrimInput | None = None,
) -> PlayerScoreResult:
    """Monthly player score. Spec §7.

    Components: compressed personal kills (§4.2), personal placement points
    (§4.3/§9.1), MVP awards (5 each), finals appearances (3 each), team wins
    (5 each), participation (1/tournament), scrim kills (0.5x compressed),
    scrim wins (1 each). No prize money, no social media (player ranking has
    neither at the monthly level).
    """
    (
        kill_pts,
        placement_pts_total,
        mvp_pts,
        finals_pts,
        team_win_pts,
        participation_pts,
        scrim_kill_pts,
        scrim_win_pts,
    ) = _player_components(tournaments, scrims)
    total = (
        kill_pts
        + placement_pts_total
        + mvp_pts
        + finals_pts
        + team_win_pts
        + participation_pts
        + scrim_kill_pts
        + scrim_win_pts
    )
    return PlayerScoreResult(
        kill_pts=kill_pts,
        placement_pts=placement_pts_total,
        mvp_pts=mvp_pts,
        finals_pts=finals_pts,
        team_win_pts=team_win_pts,
        participation_pts=participation_pts,
        scrim_kill_pts=scrim_kill_pts,
        scrim_win_pts=scrim_win_pts,
        total=total,
    )


def quarterly_player_score(
    tournaments: list[PlayerTournamentInput],
    scrims: PlayerScrimInput | None = None,
    inherited_prize_money_naira: float = 0.0,
) -> PlayerQuarterlyResult:
    """Quarterly personal player score. Spec §9.

    Same per-tournament components as monthly (over the full 3-month raw data)
    PLUS prize money inherited from any team the player rostered for (§6.2 /
    §9.1) — applied only at the quarterly level.

    This is the player's INDIVIDUAL score. Whether it is used (unattached) or
    overridden by team-tier inheritance (attached) is decided by
    ``player_tier`` — not here.
    """
    (
        kill_pts,
        placement_pts_total,
        mvp_pts,
        finals_pts,
        team_win_pts,
        participation_pts,
        scrim_kill_pts,
        scrim_win_pts,
    ) = _player_components(tournaments, scrims)
    prize = prize_money_points(inherited_prize_money_naira)
    total = (
        kill_pts
        + placement_pts_total
        + mvp_pts
        + finals_pts
        + team_win_pts
        + participation_pts
        + scrim_kill_pts
        + scrim_win_pts
        + prize
    )
    return PlayerQuarterlyResult(
        kill_pts=kill_pts,
        placement_pts=placement_pts_total,
        mvp_pts=mvp_pts,
        finals_pts=finals_pts,
        team_win_pts=team_win_pts,
        participation_pts=participation_pts,
        scrim_kill_pts=scrim_kill_pts,
        scrim_win_pts=scrim_win_pts,
        prize_money_pts=prize,
        total=total,
    )


# ===========================================================================
# Tier classification — spec §11 + §9.1
# ===========================================================================
def score_to_tier(score: float) -> int:
    """Map a quarterly score to a tier int 0..3 by raw threshold. Spec §11.

    Elite(0) >= 150, Competitive(1) 90-149, Rising(2) 40-89, Entry(3) < 40.
    Uses strict ``>=`` on raw floats; no rounding (150.0 -> 0, 149.99 -> 1).
    """
    for min_score, tier in TIER_THRESHOLDS:
        if score >= min_score:
            return tier
    return TIER_DEFAULT


# Public alias matching the orchestrator's requested name.
def classify_tier(score: float) -> int:
    """Alias of ``score_to_tier``. Spec §11. Returns 0..3."""
    return score_to_tier(score)


def assign_tier(score: float, meets_participation_floor: bool) -> int:
    """Assign a tier with the participation floor applied. Spec §7.4 / §9.2.

    If the floor is not met, force Tier 3 (Entry) regardless of score.
    """
    if not meets_participation_floor:
        return TIER_DEFAULT
    return score_to_tier(score)


def player_tier(
    is_attached: bool,
    team_tier: int | None,
    individual_score: float,
    meets_floor: bool,
) -> tuple[int, str]:
    """Resolve a player's quarterly tier and its source. Spec §9.1 / §9.2.

    Attached (on a registered team at evaluation): inherit the team's tier,
    source = "team", no personal modifier — regardless of individual score.

    Unattached: tier from the player's individual score via ``assign_tier``
    (the §9.2 floor of >=1 tournament applies), source = "individual".

    Returns ``(tier_int, source)`` where source ∈ {"team", "individual"}.
    """
    if is_attached:
        if team_tier is None:
            raise ValueError("attached player requires a team_tier")
        return team_tier, "team"
    return assign_tier(individual_score, meets_floor), "individual"


# ===========================================================================
# Annual — spec §10
# ===========================================================================
def annual_score(q1: float, q2: float, q3: float, q4: float) -> float:
    """Annual leaderboard score = sum of the four quarterly scores. Spec §10.

    Zero-activity quarters simply contribute 0. The annual track assigns NO
    tier (§10.3 — ranking only), so this engine intentionally exposes no
    annual_tier function.
    """
    return q1 + q2 + q3 + q4
