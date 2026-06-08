"""AFC Ranking & Tiering — pure scoring engine.

This package is the deterministic, side-effect-free computational core of the
AFC ranking system. It contains NO Django imports, NO ORM access, NO Celery,
and NO I/O. Every function takes plain numbers / frozen dataclasses in and
returns plain numbers / frozen dataclasses out, so the whole package is
unit-testable without a Django test database.

Aggregation from the ORM (turning Events / Matches / Scrims / prize payouts
into the ``*Input`` dataclasses below) is a SEPARATE subsystem and is NOT part
of this package. This package only defines the input contract it consumes.

Spec: ``AFC_RANKING_TIERING_SPEC.md`` (sections cited per-function).

Locked conventions (from the master plan, override any spec ambiguity):
  * Compression is applied PER-TOURNAMENT, then per-tournament scores are
    summed. This holds for BOTH monthly and quarterly. Quarterly re-runs the
    raw per-tournament data across all 3 months; it never sums monthly totals.
  * Tier multipliers: tier_1 -> 2.0, tier_2 -> 1.5, tier_3 -> 1.0.
  * 4-tier thresholds: Elite(0) >= 150, Competitive(1) 90-149,
    Rising(2) 40-89, Entry(3) < 40.
  * Player quarterly score includes personal placement points (spec §9.1).
"""

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
    SCRIM_DAILY_CAP,
    SCRIM_MONTHLY_CAP,
    SCRIM_WEIGHT,
    SCRIM_WIN_FLAT,
    SOCIAL_MEDIA_POINTS,
    TIER_MULTIPLIER,
    TIER_THRESHOLDS,
    WIN_BONUS,
)
from .engine import (
    # input dataclasses
    PlayerScrimInput,
    PlayerTournamentInput,
    ScrimInput,
    TournamentInput,
    # result dataclasses
    PlayerQuarterlyResult,
    PlayerScoreResult,
    TeamQuarterlyResult,
    TeamScoreResult,
    # primitives
    compress_kills,
    compress_placement,
    placement_points,
    prize_money_points,
    social_media_points,
    tier_multiplier,
    win_bonus,
    finals_bonus,
    # per-tournament
    tournament_score,
    # scrims
    raw_scrim_points,
    capped_scrim_points,
    # team aggregates
    monthly_team_score,
    quarterly_team_score,
    quarterly_team_prize_money_points,
    quarterly_team_social_media_points,
    # player aggregates
    monthly_player_score,
    quarterly_player_score,
    # tiering
    score_to_tier,
    classify_tier,
    assign_tier,
    player_tier,
    # annual
    annual_score,
)

__all__ = [
    # constants
    "FINALS_BASE",
    "KILL_COMPRESSION",
    "PLACEMENT_COMPRESSION",
    "PLACEMENT_POINTS",
    "PLAYER_FINALS_PTS",
    "PLAYER_MVP_PTS",
    "PLAYER_PARTICIPATION_PTS",
    "PLAYER_SCRIM_KILL_WEIGHT",
    "PLAYER_SCRIM_WIN_PTS",
    "PLAYER_TEAM_WIN_PTS",
    "PRIZE_MONEY_POINTS",
    "SCRIM_CAP_RATIO",
    "SCRIM_DAILY_CAP",
    "SCRIM_MONTHLY_CAP",
    "SCRIM_WEIGHT",
    "SCRIM_WIN_FLAT",
    "SOCIAL_MEDIA_POINTS",
    "TIER_MULTIPLIER",
    "TIER_THRESHOLDS",
    "WIN_BONUS",
    # inputs
    "PlayerScrimInput",
    "PlayerTournamentInput",
    "ScrimInput",
    "TournamentInput",
    # results
    "PlayerQuarterlyResult",
    "PlayerScoreResult",
    "TeamQuarterlyResult",
    "TeamScoreResult",
    # primitives
    "compress_kills",
    "compress_placement",
    "placement_points",
    "prize_money_points",
    "social_media_points",
    "tier_multiplier",
    "win_bonus",
    "finals_bonus",
    # per-tournament
    "tournament_score",
    # scrims
    "raw_scrim_points",
    "capped_scrim_points",
    # team aggregates
    "monthly_team_score",
    "quarterly_team_score",
    "quarterly_team_prize_money_points",
    "quarterly_team_social_media_points",
    # player aggregates
    "monthly_player_score",
    "quarterly_player_score",
    # tiering
    "score_to_tier",
    "classify_tier",
    "assign_tier",
    "player_tier",
    # annual
    "annual_score",
]
