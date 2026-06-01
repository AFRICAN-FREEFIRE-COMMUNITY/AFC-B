"""Spec scales encoded as immutable data — no logic lives here.

Every table maps directly to a section of ``AFC_RANKING_TIERING_SPEC.md``.

Bracket-lookup tables are encoded as ascending lists of
``(upper_bound_inclusive, value)`` tuples. The final bracket uses ``None`` as
its upper bound, meaning "open-ended top". The lookup rule (in engine.py) is:
return the value of the FIRST bracket whose ``upper_bound is None`` or whose
``value <= upper_bound``. This makes every upper bound inclusive and closes the
1-naira gaps in the spec's prize table (e.g. 100,001-100,999) deterministically
by letting the next band cover everything up to its own upper bound.
"""

from __future__ import annotations

from types import MappingProxyType

# ---------------------------------------------------------------------------
# Tier multipliers — spec §4
# ---------------------------------------------------------------------------
# Applies to: placement points, kill points, finals appearance bonus.
# Does NOT apply to: win bonuses, scrim points, prize money, social media.
TIER_MULTIPLIER = MappingProxyType(
    {
        "tier_1": 2.0,
        "tier_2": 1.5,
        "tier_3": 1.0,
    }
)

# ---------------------------------------------------------------------------
# Placement points per match — spec §4.1
# ---------------------------------------------------------------------------
# Finishes 11th-12th (and beyond) award 0 — handled via ``.get(finish, 0)``.
PLACEMENT_POINTS = MappingProxyType(
    {
        1: 12,
        2: 9,
        3: 8,
        4: 7,
        5: 6,
        6: 5,
        7: 4,
        8: 3,
        9: 2,
        10: 1,
    }
)

# ---------------------------------------------------------------------------
# Kill compression scale — spec §4.2
# ---------------------------------------------------------------------------
# (cumulative_raw_kills_upper_bound_inclusive, compressed_points)
KILL_COMPRESSION: tuple[tuple[int | None, int], ...] = (
    (50, 3),
    (100, 7),
    (200, 12),
    (300, 17),
    (500, 23),
    (750, 28),
    (1000, 33),
    (1500, 38),
    (2000, 43),
    (3000, 50),
    (5000, 58),
    (None, 65),
)

# ---------------------------------------------------------------------------
# Placement compression scale — spec §4.3
# ---------------------------------------------------------------------------
# (cumulative_raw_placement_pts_upper_bound_inclusive, compressed_points)
PLACEMENT_COMPRESSION: tuple[tuple[int | None, int], ...] = (
    (50, 5),
    (100, 10),
    (200, 17),
    (300, 24),
    (500, 31),
    (750, 38),
    (1000, 44),
    (1500, 50),
    (2000, 56),
    (3000, 62),
    (None, 70),
)

# ---------------------------------------------------------------------------
# Win bonuses — spec §4.4 (flat, not compressed, not multiplied)
# ---------------------------------------------------------------------------
WIN_BONUS = MappingProxyType(
    {
        "tier_1": 30,
        "tier_2": 20,
        "tier_3": 12,
    }
)

# ---------------------------------------------------------------------------
# Finals appearance bonus base — spec §4.5  (finals_bonus = 5 * tier_multiplier)
# ---------------------------------------------------------------------------
FINALS_BASE = 5

# ---------------------------------------------------------------------------
# Prize money points (quarterly tiering only) — spec §7.2
# ---------------------------------------------------------------------------
# (total_prize_naira_upper_bound_inclusive, points)
# The spec's 1-naira band gaps (e.g. 100,001-100,999) are closed by the
# inclusive-upper-bound lookup: anything <= the next band's upper bound returns
# that band's value.
PRIZE_MONEY_POINTS: tuple[tuple[int | None, int], ...] = (
    (100_000, 5),
    (300_000, 10),
    (500_000, 15),
    (750_000, 20),
    (1_000_000, 25),
    (1_500_000, 30),
    (2_000_000, 35),
    (2_500_000, 40),
    (3_000_000, 45),
    (3_500_000, 50),
    (4_000_000, 55),
    (4_500_000, 60),
    (None, 65),
)

# ---------------------------------------------------------------------------
# Social media points (teams only, quarterly tiering only, capped at 10) — §7.3
# ---------------------------------------------------------------------------
# (combined_followers_upper_bound_inclusive, points)
SOCIAL_MEDIA_POINTS: tuple[tuple[int | None, int], ...] = (
    (1_000, 1),
    (5_000, 3),
    (10_000, 5),
    (25_000, 7),
    (50_000, 9),
    (None, 10),
)

# ---------------------------------------------------------------------------
# Tier thresholds (4 tiers) — spec §11
# ---------------------------------------------------------------------------
# Elite(0) >= 150, Competitive(1) 90-149, Rising(2) 40-89, Entry(3) < 40.
# Encoded as descending (min_score_inclusive, tier_int); default tier is 3.
TIER_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (150, 0),  # Elite
    (90, 1),   # Competitive
    (40, 2),   # Rising
)
TIER_DEFAULT = 3  # Entry — score below 40

# Human-readable labels, for callers that want them (not used in math).
TIER_LABELS = MappingProxyType(
    {
        0: "Elite",
        1: "Competitive",
        2: "Rising",
        3: "Entry",
    }
)

# ---------------------------------------------------------------------------
# Scrim rules — spec §6 (Step 3) + §12
# ---------------------------------------------------------------------------
SCRIM_WEIGHT = 0.5      # placement weight & kill weight (each 0.5x tournament value)
SCRIM_WIN_FLAT = 3      # flat points per scrim win
SCRIM_CAP_RATIO = 0.30  # max scrim contribution = 30% of tournament total
SCRIM_DAILY_CAP = 4     # max scrims/day counted (enforced UPSTREAM, not here)
SCRIM_MONTHLY_CAP = 60  # max scrims/month counted (enforced UPSTREAM, not here)

# ---------------------------------------------------------------------------
# Player ranking flat weights — spec §7 (and §2 "key changes")
# ---------------------------------------------------------------------------
PLAYER_MVP_PTS = 5            # per MVP award
PLAYER_FINALS_PTS = 3         # per finals appearance (player in lineup)
PLAYER_TEAM_WIN_PTS = 5       # per team tournament win (§2: was 20, now 5)
PLAYER_PARTICIPATION_PTS = 1  # per tournament played (>= 1 match)
PLAYER_SCRIM_WIN_PTS = 1      # per scrim win while in lineup
PLAYER_SCRIM_KILL_WEIGHT = 0.5  # 0.5x of compressed scrim-kill value
