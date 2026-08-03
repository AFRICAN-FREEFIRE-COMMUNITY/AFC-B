"""AFC Ranking & Tiering - pure scoring functions.

Every function maps to a spec section (cited in its docstring). The module is
pure: no Django, no ORM, no I/O, no global mutable state. Same input -> same
output, always. This Django-free purity is a hard requirement - do not import
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

Participation floors (spec §5.2, §7.4, §9.2) are NOT enforced here - the score
functions are pure arithmetic. The caller passes an explicit ``meets_floor``
flag to ``assign_tier`` / ``player_tier`` where the floor matters.

The §12 daily (4/day) and monthly (60/month) scrim COUNT caps are enforced
UPSTREAM by the aggregation subsystem when it builds ``ScrimInput`` - only the
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
    SCRIM_FLAT_CAP,
    SCRIM_WEIGHT,
    SCRIM_WIN_FLAT,
    SOCIAL_MEDIA_POINTS,
    TIER_DEFAULT,
    # re-exported so the Django layer can branch on the mode without importing constants
    # directly (``engine.TIER_MODE_TOP_N`` in recalc.py)
    TIER_MODE_THRESHOLD,
    TIER_MODE_TOP_N,
    TIER_MULTIPLIER,
    TIER_THRESHOLDS,
    WIN_BONUS,
)
from .tables import DEFAULT_TABLES, ScoringTables

# ── admin-editable scales (owner 2026-08-03) ──
# Every function below takes ``tables``, a frozen ScoringTables carrying the numbers to use.
# It defaults to DEFAULT_TABLES, which is exactly the constants.py values above, so every
# existing call site keeps its behaviour and every test that calls the engine directly still
# reads the shipped scales.
#
# The Django-aware layer (afc_rankings/aggregation.py) is what decides WHICH tables apply to
# a given month or season - it looks up the admin-saved ScoringConfig bound to that period
# and passes the result down. This module still never reads a database, which is the hard
# requirement in the module docstring above: it is handed its numbers, it does not fetch them.
# That is also what keeps a closed season on the rules it was scored under: the caller
# resolves the season's own config and passes those tables in.

TierStr = str  # "tier_1" | "tier_2" | "tier_3" (matches Event.tournament_tier)


# ===========================================================================
# Input dataclasses - the I/O contract the aggregation subsystem builds.
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
# Result dataclasses - mirror the DB score-model field breakdowns (§19).
# ===========================================================================
@dataclass(frozen=True)
class TeamScoreResult:
    """Monthly team score breakdown - mirrors TeamMonthlyScore (§19.5)."""

    tournament_pts: float
    scrim_pts: float
    total: float


@dataclass(frozen=True)
class TeamQuarterlyResult:
    """Quarterly team score breakdown - mirrors TeamQuarterlyScore (§19.6)."""

    tournament_pts: float
    scrim_pts: float
    prize_money_pts: float
    social_media_pts: float
    total: float


@dataclass(frozen=True)
class PlayerScoreResult:
    """Monthly player score breakdown - mirrors PlayerMonthlyScore (§19.7)."""

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
    """Quarterly player score breakdown - mirrors PlayerQuarterlyScore (§19.8).

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
def compress_kills(raw_kills: float, tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Compress a raw kill total to its bracket points. Spec §4.2.

    The bracket determines the value (not additive): 120 raw kills -> 12.

    Zero-stat floor (product decision): raw == 0 returns 0 - a no-kill
    appearance scores nothing. Any raw > 0 uses the bracket table unchanged
    (1 -> 3, 50 -> 3, 51 -> 7, ...).
    """
    if raw_kills == 0:
        return 0
    return _bracket_lookup(tables.kill_compression, raw_kills)


def compress_placement(raw_placement_pts: float,
                       tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Compress a raw placement-points total to its bracket points. Spec §4.3.

    Zero-stat floor (product decision): raw == 0 returns 0. Any raw > 0 uses the
    bracket table unchanged (1 -> 5, 50 -> 5, 51 -> 10, ...).
    """
    if raw_placement_pts == 0:
        return 0
    return _bracket_lookup(tables.placement_compression, raw_placement_pts)


def prize_money_points(total_naira: float,
                       tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Points for total prize money won across the quarter. Spec §7.2.

    The argument is in NAIRA, and so are the table's thresholds. Callers holding an amount
    in another currency must convert first (afc_rankings.prize_sync._amount_ngn is the one
    converter this system uses) - comparing a bare foreign amount against a naira threshold
    is the exact mistake that mis-tiered a $400 event on 2026-08-03.
    """
    return _bracket_lookup(tables.prize_money_points, total_naira)


def social_media_points(combined_followers: float,
                        tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Points for combined IG+TikTok followers (capped by the top band). Spec §7.3."""
    return _bracket_lookup(tables.social_media_points, combined_followers)


# ===========================================================================
# Building blocks
# ===========================================================================
def placement_points(finish: int, tables: ScoringTables = DEFAULT_TABLES) -> float:
    """Raw placement points for a single match finish. Spec §4.1.

    Any finish not in the table awards 0 (by default 11th and below). This is the canonical
    mapping - callers must NOT trust any legacy ``placement_points`` column on existing models.
    """
    return tables.placement_points.get(finish, 0)


def tier_multiplier(tier: TierStr, tables: ScoringTables = DEFAULT_TABLES) -> float:
    """Tournament tier multiplier. Spec §4. Raises ValueError on an unknown tier.

    A RETIRED tier still resolves here, deliberately: events already classified under it
    must keep scoring. Retirement only removes a tier from the choices offered for new work
    (``ScoringTables.active_tier_keys``).
    """
    return tables.tier(tier).multiplier


def win_bonus(tier: TierStr, tables: ScoringTables = DEFAULT_TABLES) -> float:
    """Flat win bonus for the tournament winner (not multiplied). Spec §4.4.

    Raises ValueError on an unknown tier; retired tiers resolve (see tier_multiplier).
    """
    return tables.tier(tier).win_bonus


def finals_bonus(tier: TierStr, appearances: int = 1,
                 tables: ScoringTables = DEFAULT_TABLES) -> float:
    """Finals appearance bonus = finals_base * tier_multiplier * appearances. Spec §4.5."""
    return tables.finals_base * tier_multiplier(tier, tables) * appearances


# ===========================================================================
# Per-tournament team score - spec §5.1 Step 1
# ===========================================================================
def tournament_score(t: TournamentInput, tables: ScoringTables = DEFAULT_TABLES) -> float:
    """Score for ONE tournament for a team. Spec §5.1 Step 1.

        tournament_score = (compress_placement(raw_placement)
                            + compress_kills(raw_kills)) * tier_multiplier
                         + win_bonus           (if won)
                         + finals_base * tier_multiplier * finals_appearances

    The tier multiplier applies to placement, kills, and finals - NOT to the
    flat win bonus (spec §4).
    """
    mult = tier_multiplier(t.tier, tables)
    base = (compress_placement(t.raw_placement_pts, tables)
            + compress_kills(t.raw_kills, tables)) * mult
    bonus = win_bonus(t.tier, tables) if t.won else 0
    finals = finals_bonus(t.tier, t.finals_appearances, tables)
    return base + bonus + finals


# ===========================================================================
# Scrims - spec §5.1 Step 3 + §12
# ===========================================================================
def raw_scrim_points(s: ScrimInput, tables: ScoringTables = DEFAULT_TABLES) -> float:
    """Raw (uncapped) scrim points for a team. Spec §5.1 Step 3 / §12.

        raw = scrim_placement * weight + scrim_kills * weight + scrim_wins * win_flat
    """
    return (
        s.scrim_placement_pts * tables.scrim_weight
        + s.scrim_kills * tables.scrim_weight
        + s.scrim_wins * tables.scrim_win_flat
    )


def capped_scrim_points(
    raw_scrim: float,
    total_tournament_pts: float,
    flat_cap: float | None = None,
    tables: ScoringTables = DEFAULT_TABLES,
) -> float:
    """Cap a team's scrim contribution. Spec §5.1 Step 3, amended by the owner 2026-08-03.

    The cap is the HIGHER of two limits:
      - a flat allowance (SCRIM_FLAT_CAP, overridable per deployment), and
      - 30% of the team's tournament points (the original spec rule).

    WHY IT CHANGED: the rule used to be the 30% ratio alone, and 30% of zero is zero, so
    a team that played nothing but scrims scored nothing no matter how well it did, and
    the participation floor then removed it from the ladder entirely. Scrims are meant to
    count toward rankings, so they now have a floor of their own.

    Taking the HIGHER of the two, rather than switching between them, means there is no
    cliff. A team with no tournament results can earn up to the flat allowance. As its
    tournament points grow, nothing changes until 30% of them exceeds that allowance, and
    from then on the proportional rule governs and keeps scaling. A team can never lose
    scrim points by performing better in tournaments.

    `flat_cap` is INJECTED by the caller, which is how the admin-configured value reaches
    this function. When omitted it comes from ``tables`` (the whole admin config), which in
    turn defaults to the shipped constant. This module is deliberately Django-free (see the
    module docstring), so it never reads the database itself: the aggregation layer resolves
    the configured value and passes it down.
    """
    if flat_cap is None:
        flat_cap = tables.scrim_flat_cap
    return min(raw_scrim,
               max(float(flat_cap), total_tournament_pts * tables.scrim_cap_ratio))


# ===========================================================================
# Team aggregates - spec §6 (monthly) + §8 (quarterly)
# ===========================================================================
def monthly_team_score(
    tournaments: list[TournamentInput],
    scrims: ScrimInput | None = None,
    scrim_flat_cap: float | None = None,
    tables: ScoringTables = DEFAULT_TABLES,
) -> TeamScoreResult:
    """Monthly team score. Spec §6.

    Sums per-tournament scores, then adds the capped scrim contribution. The scrim cap is
    the higher of the flat allowance and the configured share of the tournament total, see
    capped_scrim_points. `scrim_flat_cap` comes from the caller so an admin-configured
    value can reach the pure engine without it touching the database; when omitted it is
    read from ``tables``, which carries the whole admin config.
    """
    total_tournament_pts = sum(tournament_score(t, tables) for t in tournaments)
    raw = raw_scrim_points(scrims, tables) if scrims is not None else 0.0
    counted = capped_scrim_points(raw, total_tournament_pts, scrim_flat_cap, tables)
    return TeamScoreResult(
        tournament_pts=total_tournament_pts,
        scrim_pts=counted,
        total=total_tournament_pts + counted,
    )


def quarterly_team_prize_money_points(prize_money_naira: float,
                                      tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Prize-money points for the quarter, in NAIRA. Spec §7.2 (thin alias of the scale)."""
    return prize_money_points(prize_money_naira, tables)


def quarterly_team_social_media_points(combined_followers: float,
                                       tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Social-media points for the quarter. Spec §7.3 (thin alias)."""
    return social_media_points(combined_followers, tables)


def quarterly_team_score(
    tournaments: list[TournamentInput],
    scrims: ScrimInput | None = None,
    prize_money_naira: float = 0.0,
    combined_followers: float = 0,
    tables: ScoringTables = DEFAULT_TABLES,
) -> TeamQuarterlyResult:
    """Quarterly team score. Spec §8.

    Uses the SAME per-tournament formula as monthly (§8.1) over the full
    3-month raw dataset (the ``tournaments`` list spans all 3 months), then adds
    prize money (§7.2, in naira) and social media (§7.3). Tier assignment /
    participation floor are NOT applied here - see ``assign_tier``.
    """
    base = monthly_team_score(tournaments, scrims, None, tables)  # §8.1 "same formula as monthly"
    prize = quarterly_team_prize_money_points(prize_money_naira, tables)
    social = quarterly_team_social_media_points(combined_followers, tables)
    return TeamQuarterlyResult(
        tournament_pts=base.tournament_pts,
        scrim_pts=base.scrim_pts,
        prize_money_pts=prize,
        social_media_pts=social,
        total=base.total + prize + social,
    )


# ===========================================================================
# Player aggregates - spec §7 (monthly) + §9 (quarterly)
# ===========================================================================
def _player_components(
    tournaments: list[PlayerTournamentInput],
    scrims: PlayerScrimInput | None,
    tables: ScoringTables = DEFAULT_TABLES,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Shared component computation for monthly & quarterly player scoring.

    Per-tournament compression (kills AND placement) then summed - matches the
    locked team-path convention so the two stay symmetric. MVP/finals/team-win/
    participation are flat per spec §7 (and §2: team win = 5, not 20); every one of
    those flat weights comes from ``tables`` so an admin can change them.
    """
    kill_pts = sum(compress_kills(t.personal_kills, tables) for t in tournaments)
    placement_pts_total = sum(
        compress_placement(t.personal_placement_pts, tables) for t in tournaments
    )
    mvp_pts = tables.player_mvp_pts * sum(t.mvp_count for t in tournaments)
    finals_pts = tables.player_finals_pts * sum(t.finals_appearances for t in tournaments)
    team_win_pts = tables.player_team_win_pts * sum(1 for t in tournaments if t.team_won)
    participation_pts = tables.player_participation_pts * sum(
        1 for t in tournaments if t.participated
    )
    if scrims is not None:
        scrim_kill_pts = tables.player_scrim_kill_weight * compress_kills(
            scrims.scrim_kills, tables)
        scrim_win_pts = tables.player_scrim_win_pts * scrims.scrim_wins
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
    tables: ScoringTables = DEFAULT_TABLES,
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
    ) = _player_components(tournaments, scrims, tables)
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
    tables: ScoringTables = DEFAULT_TABLES,
) -> PlayerQuarterlyResult:
    """Quarterly personal player score. Spec §9.

    Same per-tournament components as monthly (over the full 3-month raw data)
    PLUS prize money inherited from any team the player rostered for (§6.2 /
    §9.1) - applied only at the quarterly level.

    This is the player's INDIVIDUAL score. Whether it is used (unattached) or
    overridden by team-tier inheritance (attached) is decided by
    ``player_tier`` - not here.
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
    ) = _player_components(tournaments, scrims, tables)
    prize = prize_money_points(inherited_prize_money_naira, tables)
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
# Tier classification - spec §11 + §9.1
# ===========================================================================
def score_to_tier(score: float, tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Map a quarterly score to a tier int by raw threshold. Spec §11.

    Defaults: Elite(0) >= 150, Competitive(1) 90-149, Rising(2) 40-89, Entry(3) < 40.
    Cutoffs are read top down and the first one the score clears wins, so the table must
    descend - an out-of-order cutoff is reported by ``scoring/validation.py`` as an
    unreachable cutoff rather than silently reordered here. Uses strict ``>=`` on raw
    floats; no rounding (150.0 -> 0, 149.99 -> 1).
    """
    for min_score, tier in tables.tier_thresholds:
        if score >= min_score:
            return tier
    return tables.tier_default


# Public alias matching the orchestrator's requested name.
def classify_tier(score: float, tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Alias of ``score_to_tier``. Spec §11."""
    return score_to_tier(score, tables)


def assign_tier(score: float, meets_participation_floor: bool,
                tables: ScoringTables = DEFAULT_TABLES) -> int:
    """Assign a tier with the participation floor applied. Spec §7.4 / §9.2.

    If the floor is not met, force the default tier (Entry) regardless of score.

    THRESHOLD MODE ONLY. A tier decided by top-N depends on the whole ladder, not on one
    team's score, so it cannot be answered here - use ``assign_tiers_top_n`` for that. The
    Django layer (``afc_rankings/recalc.py``) reads ``tables.tier_mode`` and picks.
    """
    if not meets_participation_floor:
        return tables.tier_default
    return score_to_tier(score, tables)


# ---------------------------------------------------------------------------
# Top-N tiering - owner request 2026-08-03
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LadderEntry:
    """One competitor's place in the ladder being tiered.

    ``key``   - whatever the caller wants back in the result map (a score-row primary key,
                in practice). The engine never interprets it.
    ``score`` - the number the ladder is ordered by. For teams that is the EFFECTIVE score
                (total minus any manual deduction), which is what recalc already ranks on,
                so the tiers can never disagree with the printed rank.
    ``meets_floor`` - the participation floor, already evaluated by the caller.
    """

    key: object
    score: float
    meets_floor: bool


# Two scores this close together are the same score. Quarterly scores are sums of floats,
# so two teams that genuinely earned the same points can differ in the last bit or two;
# a bare ``==`` would then split a real tie and hand one of them the better tier.
_TIE_EPSILON = 1e-9


def assign_tiers_top_n(entries, tables: ScoringTables = DEFAULT_TABLES) -> dict:
    """Tier a whole ladder by POSITION: the best N are tier 1, the next M tier 2, and so on.

    Returns ``{key: tier_int}`` covering every entry passed in. The three rules that decide
    the awkward cases, all deliberate and all tested:

    1. THE PARTICIPATION FLOOR IS APPLIED FIRST, BEFORE THE COUNT IS TAKEN.
       A team that has not met the floor is not eligible for any tier and does not occupy a
       place. Otherwise a floor-failing team sitting third would burn a Tier 1 slot and give
       it to nobody, so "the top 10" would quietly become nine teams. "Top N" means the top N
       teams that qualify to be ranked at all, which is also what threshold mode does - there
       the floor sends a team to the default tier whatever it scored.

    2. A TIE ON THE BOUNDARY GOES UP, FOR EVERY TIED TEAM.
       If Tier 1 is the top 10 and the 10th and 11th teams have the same score, both are
       Tier 1 (and the tier finishes with 11 teams). Two teams that scored exactly the same
       cannot be given different tiers for a whole season on the strength of an alphabetical
       tiebreak - that is the one outcome nobody can explain to the team that lost out.
       Dropping both instead would punish each of them for the other's existence and leave
       the tier short. So the count is a minimum size, not a maximum. Ties are only ever
       absorbed upward, so a tie can never straddle two tiers.

    3. WHAT THE COUNTS DO NOT COVER FALLS TO THE DEFAULT TIER.
       If the counts add up to fewer teams than are ranked, everyone past the last count
       gets ``tables.tier_default`` - the same fall-through threshold mode already uses for a
       team below every cutoff. If they add up to more, the lower tiers simply run out of
       teams and finish short; that is not an error.

    ORDER: entries are ranked by score, descending, with a STABLE sort - so the order the
    caller passes them in is the tiebreak. Callers pass rows already in ladder order (the
    same ``-effective, -wins, -kills, name`` order that produced the printed rank), which is
    what keeps a team's tier and its rank telling the same story.

    A tier whose count is None (unset) or 0 is simply skipped and ends up empty. Validation
    refuses to SAVE an unset count in top-N mode; this is the fail-soft path for a config
    that reached the engine anyway.
    """
    entries = list(entries)
    result = {}

    # ── rule 1: the floor, before anything is counted ──
    eligible = []
    for entry in entries:
        if entry.meets_floor:
            eligible.append(entry)
        else:
            result[entry.key] = tables.tier_default

    # Stable sort on score alone: equal scores keep the caller's ladder order.
    ordered = sorted(eligible, key=lambda e: -e.score)

    taken = 0
    for count, tier in tables.tier_counts:
        if taken >= len(ordered):
            break
        if not count or count <= 0:
            continue  # an unset or zero size means "no team is placed in this tier"
        cut = min(taken + count, len(ordered))
        # ── rule 2: pull in everyone tied with the last team inside the cut ──
        boundary = ordered[cut - 1].score
        while cut < len(ordered) and abs(ordered[cut].score - boundary) <= _TIE_EPSILON:
            cut += 1
        for entry in ordered[taken:cut]:
            result[entry.key] = tier
        taken = cut

    # ── rule 3: everyone the counts did not reach ──
    for entry in ordered[taken:]:
        result[entry.key] = tables.tier_default

    return result


def player_tier(
    is_attached: bool,
    team_tier: int | None,
    individual_score: float,
    meets_floor: bool,
    tables: ScoringTables = DEFAULT_TABLES,
    individual_tier: int | None = None,
) -> tuple[int, str]:
    """Resolve a player's quarterly tier and its source. Spec §9.1 / §9.2.

    Attached (on a registered team at evaluation): inherit the team's tier,
    source = "team", no personal modifier - regardless of individual score.

    Unattached: tier from the player's individual score via ``assign_tier``
    (the §9.2 floor of >=1 tournament applies), source = "individual".

    ``individual_tier`` is the already-decided unattached answer, supplied when the tier
    could not be worked out from one score on its own - top-N mode, where a player's tier
    is their position on the player ladder (``assign_tiers_top_n``). It is ignored for an
    attached player, who inherits either way. Left None (the default and every existing
    caller), the score is used exactly as before.

    Returns ``(tier_int, source)`` where source ∈ {"team", "individual"}.
    """
    if is_attached:
        if team_tier is None:
            raise ValueError("attached player requires a team_tier")
        return team_tier, "team"
    if individual_tier is not None:
        return individual_tier, "individual"
    return assign_tier(individual_score, meets_floor, tables), "individual"


# ===========================================================================
# Annual - spec §10
# ===========================================================================
def annual_score(q1: float, q2: float, q3: float, q4: float) -> float:
    """Annual leaderboard score = sum of the four quarterly scores. Spec §10.

    Zero-activity quarters simply contribute 0. The annual track assigns NO
    tier (§10.3 - ranking only), so this engine intentionally exposes no
    annual_tier function.
    """
    return q1 + q2 + q3 + q4
