"""Unit tests for the AFC scoring engine (pure, Django-free).

Run with the project venv:

    cd C:\\Users\\Sweez\\Desktop\\LAYO\\CLAUDE\\AFC\\WEBSITE\\backend
    .venv/Scripts/python.exe -m unittest afc_rankings.scoring.tests_scoring -v

These tests cover every scale boundary (both sides of every bracket edge), the
spec's named worked examples, the tier-classification boundaries, scrim caps,
the participation-floor override, player team-win = 5 (not 20), and zero-
activity edge cases. The engine imports without Django setup; this module
imports it directly to prove that.
"""

import unittest

from afc_rankings.scoring import (
    PlayerScrimInput,
    PlayerTournamentInput,
    ScrimInput,
    TournamentInput,
    annual_score,
    assign_tier,
    capped_scrim_points,
    classify_tier,
    compress_kills,
    compress_placement,
    finals_bonus,
    monthly_player_score,
    monthly_team_score,
    placement_points,
    player_tier,
    prize_money_points,
    quarterly_player_score,
    quarterly_team_score,
    raw_scrim_points,
    score_to_tier,
    social_media_points,
    tier_multiplier,
    tournament_score,
    win_bonus,
)


class TestNoDjangoImport(unittest.TestCase):
    """The engine must be pure — no django / ORM / celery imported anywhere."""

    def test_no_forbidden_imports(self):
        import sys

        import afc_rankings.scoring as pkg  # noqa: F401
        from afc_rankings.scoring import constants, engine  # noqa: F401

        forbidden = ("django", "celery", "requests")
        for name in list(sys.modules):
            for bad in forbidden:
                self.assertFalse(
                    name == bad or name.startswith(bad + "."),
                    msg=f"forbidden module loaded by engine import: {name}",
                )


class TestCompressKills(unittest.TestCase):
    """Spec §4.2 — kill compression brackets, both sides of every edge."""

    def test_brackets(self):
        cases = [
            (0, 0),        # zero-stat floor: no kills -> 0 (product decision)
            (1, 3),        # first non-zero kill enters lowest bracket
            (50, 3),       # boundary inclusive
            (51, 7),       # boundary + 1
            (100, 7),
            (101, 12),
            (120, 12),     # ★ named spec example (§4.2 note): 120 -> 12
            (200, 12),
            (201, 17),
            (300, 17),
            (301, 23),
            (500, 23),
            (501, 28),
            (750, 28),
            (751, 33),
            (1000, 33),
            (1001, 38),
            (1500, 38),
            (1501, 43),
            (2000, 43),
            (2001, 50),
            (3000, 50),
            (3001, 58),
            (5000, 58),
            (5001, 65),    # open top
            (999_999, 65),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(compress_kills(raw), expected)


class TestCompressPlacement(unittest.TestCase):
    """Spec §4.3 — placement compression brackets, both sides of every edge."""

    def test_brackets(self):
        cases = [
            (0, 0),        # zero-stat floor: no placement pts -> 0 (product decision)
            (1, 5),        # first non-zero placement pt enters lowest bracket
            (50, 5),
            (51, 10),
            (100, 10),
            (101, 17),
            (200, 17),
            (201, 24),
            (300, 24),
            (301, 31),
            (500, 31),
            (501, 38),
            (750, 38),
            (751, 44),
            (1000, 44),
            (1001, 50),
            (1500, 50),
            (1501, 56),
            (2000, 56),
            (2001, 62),
            (3000, 62),
            (3001, 70),    # open top
            (1_000_000, 70),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(compress_placement(raw), expected)


class TestPrizeMoneyPoints(unittest.TestCase):
    """Spec §7.2 — prize money brackets (inclusive-upper-bound gap closure)."""

    def test_brackets(self):
        cases = [
            (0, 5),
            (100_000, 5),       # boundary
            (100_001, 10),      # gap row 100,001-100,999 -> next band (FLAG B resolution)
            (150_000, 10),
            (300_000, 10),
            (300_001, 15),
            (500_000, 15),
            (500_001, 20),
            (750_000, 20),
            (750_001, 25),
            (1_000_000, 25),
            (1_000_001, 30),
            (1_500_000, 30),
            (1_500_001, 35),
            (2_000_000, 35),
            (2_000_001, 40),
            (2_500_000, 40),
            (2_500_001, 45),
            (3_000_000, 45),
            (3_000_001, 50),
            (3_500_000, 50),
            (3_500_001, 55),
            (4_000_000, 55),
            (4_000_001, 60),
            (4_500_000, 60),
            (4_500_001, 65),    # open top
            (5_000_000, 65),
            (9_999_999, 65),
        ]
        for naira, expected in cases:
            with self.subTest(naira=naira):
                self.assertEqual(prize_money_points(naira), expected)


class TestSocialMediaPoints(unittest.TestCase):
    """Spec §7.3 — social media brackets, capped at 10."""

    def test_brackets(self):
        cases = [
            (0, 1),
            (1_000, 1),
            (1_001, 3),
            (5_000, 3),
            (5_001, 5),
            (10_000, 5),
            (10_001, 7),
            (25_000, 7),
            (25_001, 9),
            (50_000, 9),
            (50_001, 10),   # cap
            (9_000_000, 10),  # cap holds
        ]
        for followers, expected in cases:
            with self.subTest(followers=followers):
                self.assertEqual(social_media_points(followers), expected)


class TestPlacementPoints(unittest.TestCase):
    """Spec §4.1 — per-match finish points."""

    def test_finishes(self):
        cases = [
            (1, 12),
            (2, 9),
            (3, 8),
            (4, 7),
            (5, 6),
            (6, 5),
            (7, 4),
            (8, 3),
            (9, 2),
            (10, 1),
            (11, 0),
            (12, 0),
            (50, 0),
        ]
        for finish, expected in cases:
            with self.subTest(finish=finish):
                self.assertEqual(placement_points(finish), expected)


class TestBuildingBlocks(unittest.TestCase):
    """Spec §4 / §4.4 / §4.5 — multipliers, win bonus, finals bonus."""

    def test_tier_multiplier(self):
        self.assertEqual(tier_multiplier("tier_1"), 2.0)
        self.assertEqual(tier_multiplier("tier_2"), 1.5)
        self.assertEqual(tier_multiplier("tier_3"), 1.0)

    def test_tier_multiplier_unknown_raises(self):
        with self.assertRaises(ValueError):
            tier_multiplier("tier_4")
        with self.assertRaises(ValueError):
            tier_multiplier("")

    def test_win_bonus(self):
        self.assertEqual(win_bonus("tier_1"), 30)
        self.assertEqual(win_bonus("tier_2"), 20)
        self.assertEqual(win_bonus("tier_3"), 12)

    def test_win_bonus_unknown_raises(self):
        with self.assertRaises(ValueError):
            win_bonus("nope")

    def test_finals_bonus(self):
        self.assertEqual(finals_bonus("tier_1", 1), 10.0)   # 5 * 2.0
        self.assertEqual(finals_bonus("tier_3", 2), 10.0)   # 5 * 1.0 * 2
        self.assertEqual(finals_bonus("tier_2", 0), 0.0)    # no finals
        self.assertEqual(finals_bonus("tier_2", 1), 7.5)    # 5 * 1.5
        # default appearances = 1
        self.assertEqual(finals_bonus("tier_1"), 10.0)


class TestTournamentScore(unittest.TestCase):
    """Spec §5.1 Step 1 — per-tournament team score."""

    def test_tier3_no_win_no_finals(self):
        t = TournamentInput("tier_3", raw_placement_pts=120, raw_kills=120)
        # (compress_pl(120)=17 + compress_k(120)=12) * 1.0 = 29
        self.assertEqual(tournament_score(t), 29.0)

    def test_tier1_win_one_finals(self):
        t = TournamentInput(
            "tier_1", raw_placement_pts=120, raw_kills=120, won=True, finals_appearances=1
        )
        # (17+12)*2.0=58 + win 30 + finals(5*2.0*1=10) = 98
        self.assertEqual(tournament_score(t), 98.0)

    def test_tier2_win_no_finals(self):
        t = TournamentInput("tier_2", raw_placement_pts=51, raw_kills=51, won=True)
        # (compress_pl(51)=10 + compress_k(51)=7)*1.5=25.5 + win 20 = 45.5
        self.assertEqual(tournament_score(t), 45.5)

    def test_tier1_finals_only_x2(self):
        t = TournamentInput(
            "tier_1", raw_placement_pts=0, raw_kills=0, won=False, finals_appearances=2
        )
        # zero-stat floor: compress_pl(0)=0 + compress_k(0)=0 -> (0)*2.0=0;
        # finals(5*2.0*2=20) still applies (flag implies actual play) = 20
        self.assertEqual(tournament_score(t), 20.0)

    def test_zero_stat_tournament_scores_zero(self):
        # Product decision: a tournament with 0 kills AND 0 placement contributes
        # 0 from the compression term. No win/finals flags set -> total 0.
        t = TournamentInput("tier_3", raw_placement_pts=0, raw_kills=0)
        self.assertEqual(tournament_score(t), 0.0)


class TestScrims(unittest.TestCase):
    """Spec §5.1 Step 3 / §12 — scrim raw points and 30% cap."""

    def test_raw_scrim_points(self):
        s = ScrimInput(scrim_placement_pts=100, scrim_kills=80, scrim_wins=4)
        # 100*.5 + 80*.5 + 4*3 = 50 + 40 + 12 = 102
        self.assertEqual(raw_scrim_points(s), 102.0)

    def test_raw_scrim_points_zero(self):
        self.assertEqual(raw_scrim_points(ScrimInput()), 0.0)

    def test_cap_binds(self):
        # cap = 200 * 0.30 = 60; raw 102 > 60 -> 60
        self.assertEqual(capped_scrim_points(102, 200), 60.0)

    def test_cap_does_not_bind(self):
        self.assertEqual(capped_scrim_points(40, 200), 40.0)

    def test_cap_zero_tournament_total(self):
        # no tournaments -> cap 0 -> no scrim credit
        self.assertEqual(capped_scrim_points(102, 0), 0.0)


class TestMonthlyTeamScore(unittest.TestCase):
    """Spec §6 — monthly team score (per-tournament sum + capped scrims)."""

    def test_tier3_with_capped_scrim(self):
        t = TournamentInput("tier_3", raw_placement_pts=120, raw_kills=120)
        s = ScrimInput(scrim_placement_pts=100, scrim_kills=80, scrim_wins=4)
        result = monthly_team_score([t], s)
        # tourn = 29; raw_scrim = 102; cap = 29 * 0.30 = 8.7; counted = 8.7
        self.assertEqual(result.tournament_pts, 29.0)
        self.assertAlmostEqual(result.scrim_pts, 8.7)
        self.assertAlmostEqual(result.total, 37.7)

    def test_tier1_win_no_scrim(self):
        t = TournamentInput(
            "tier_1", raw_placement_pts=120, raw_kills=120, won=True, finals_appearances=1
        )
        result = monthly_team_score([t])
        self.assertEqual(result.tournament_pts, 98.0)
        self.assertEqual(result.scrim_pts, 0.0)
        self.assertEqual(result.total, 98.0)

    def test_no_tournaments_scrim_zeroed(self):
        # participation floor effect: no tournaments -> scrim cap 0 -> total 0
        s = ScrimInput(scrim_placement_pts=100, scrim_kills=80, scrim_wins=4)
        result = monthly_team_score([], s)
        self.assertEqual(result.tournament_pts, 0.0)
        self.assertEqual(result.scrim_pts, 0.0)
        self.assertEqual(result.total, 0.0)

    def test_zero_activity_no_scrim(self):
        result = monthly_team_score([])
        self.assertEqual(result.total, 0.0)

    def test_multiple_tournaments_per_tournament_compression_summed(self):
        # LOCKED CONVENTION: compression is per-tournament then summed.
        # Two tier_3 tournaments each (pl=120, k=120) -> 29 each -> 58 total.
        # (Cumulative would be compress(240)=24+... — NOT what we do.)
        t = TournamentInput("tier_3", raw_placement_pts=120, raw_kills=120)
        result = monthly_team_score([t, t])
        self.assertEqual(result.tournament_pts, 58.0)
        self.assertEqual(result.total, 58.0)


class TestQuarterlyTeamScore(unittest.TestCase):
    """Spec §8 — quarterly team score (same formula + prize + social)."""

    def test_basic(self):
        t = TournamentInput("tier_3", raw_placement_pts=120, raw_kills=120)
        result = quarterly_team_score(
            [t], scrims=None, prize_money_naira=200_000, combined_followers=6_000
        )
        # 29 + prize(200000)=10 + social(6000)=5 = 44
        self.assertEqual(result.tournament_pts, 29.0)
        self.assertEqual(result.scrim_pts, 0.0)
        self.assertEqual(result.prize_money_pts, 10)
        self.assertEqual(result.social_media_pts, 5)
        self.assertEqual(result.total, 44.0)

    def test_quarterly_reruns_raw_per_tournament(self):
        # 3 months of raw per-tournament data, never summed from monthlies.
        t = TournamentInput("tier_2", raw_placement_pts=120, raw_kills=120)
        # per-tournament: (17+12)*1.5 = 43.5 each
        result = quarterly_team_score([t, t, t])
        self.assertAlmostEqual(result.tournament_pts, 130.5)
        self.assertEqual(result.prize_money_pts, 5)   # 0 naira -> bracket 1 -> 5
        self.assertEqual(result.social_media_pts, 1)  # 0 followers -> bracket 1 -> 1
        self.assertAlmostEqual(result.total, 136.5)

    def test_no_activity(self):
        result = quarterly_team_score([])
        # 0 tournaments, 0 prize -> 5, 0 followers -> 1
        self.assertEqual(result.tournament_pts, 0.0)
        self.assertEqual(result.prize_money_pts, 5)
        self.assertEqual(result.social_media_pts, 1)
        self.assertEqual(result.total, 6.0)


class TestMonthlyPlayerScore(unittest.TestCase):
    """Spec §7 — monthly player score (team win = 5, includes placement §9.1)."""

    def test_full_single_tournament(self):
        pt = PlayerTournamentInput(
            "tier_1",
            personal_kills=120,
            personal_placement_pts=0,
            mvp_count=2,
            finals_appearances=1,
            team_won=True,
            participated=True,
        )
        result = monthly_player_score([pt])
        # kill compress(120)=12; placement compress(0)=0 (zero-stat floor);
        # mvp 2*5=10; finals 1*3=3; team_win 5; participation 1 = 31
        self.assertEqual(result.kill_pts, 12)
        self.assertEqual(result.placement_pts, 0)
        self.assertEqual(result.mvp_pts, 10)
        self.assertEqual(result.finals_pts, 3)
        self.assertEqual(result.team_win_pts, 5)   # ★ §2 key change: 5, NOT 20
        self.assertEqual(result.participation_pts, 1)
        self.assertEqual(result.total, 31.0)

    def test_team_win_is_five_not_twenty(self):
        pt = PlayerTournamentInput("tier_1", personal_kills=0, personal_placement_pts=0, team_won=True)
        result = monthly_player_score([pt])
        # kills(0)=0 + placement(0)=0 (zero-stat floor) + team_win 5 = 5
        self.assertEqual(result.team_win_pts, 5)
        self.assertEqual(result.total, 5.0)

    def test_two_tournaments_per_tournament_compression(self):
        # Two tournaments, 60 kills each, 30 placement pts each, both played,
        # one team win. Per-tournament compression then summed:
        # kills: compress(60)=7 each -> 14; placement: compress(30)=5 each -> 10;
        # participation 2; team_win 5 -> 31.
        pt_a = PlayerTournamentInput(
            "tier_1", personal_kills=60, personal_placement_pts=30, team_won=True, participated=True
        )
        pt_b = PlayerTournamentInput(
            "tier_1", personal_kills=60, personal_placement_pts=30, participated=True
        )
        result = monthly_player_score([pt_a, pt_b])
        self.assertEqual(result.kill_pts, 14)
        self.assertEqual(result.placement_pts, 10)
        self.assertEqual(result.participation_pts, 2)
        self.assertEqual(result.team_win_pts, 5)
        self.assertEqual(result.total, 31.0)

    def test_scrim_only(self):
        s = PlayerScrimInput(scrim_kills=120, scrim_wins=4)
        result = monthly_player_score([], s)
        # 0.5 * compress(120)=6 + wins 4*1=4 = 10
        self.assertEqual(result.scrim_kill_pts, 6.0)
        self.assertEqual(result.scrim_win_pts, 4)
        self.assertEqual(result.total, 10.0)

    def test_zero_activity(self):
        result = monthly_player_score([])
        self.assertEqual(result.total, 0.0)


class TestQuarterlyPlayerScore(unittest.TestCase):
    """Spec §9 — quarterly personal player score (+ inherited prize money)."""

    def test_includes_inherited_prize_money(self):
        pt = PlayerTournamentInput(
            "tier_1",
            personal_kills=120,
            personal_placement_pts=0,
            mvp_count=2,
            finals_appearances=1,
            team_won=True,
            participated=True,
        )
        result = quarterly_player_score([pt], inherited_prize_money_naira=200_000)
        # base 31 (as monthly, placement(0)=0 zero-stat floor) + prize(200000)=10 = 41
        self.assertEqual(result.prize_money_pts, 10)
        self.assertEqual(result.total, 41.0)

    def test_includes_personal_placement_points(self):
        # §9.1: personal placement points MUST be included.
        pt = PlayerTournamentInput(
            "tier_1", personal_kills=0, personal_placement_pts=120, participated=True
        )
        result = quarterly_player_score([pt])
        # kills(0)=0 (zero-stat floor) + placement compress(120)=17
        #   + participation 1 + prize(0 naira)=5 = 23
        self.assertEqual(result.placement_pts, 17)
        self.assertEqual(result.total, 23.0)

    def test_no_prize_defaults_to_lowest_band(self):
        pt = PlayerTournamentInput("tier_1", personal_kills=0, personal_placement_pts=0, participated=True)
        result = quarterly_player_score([pt])
        # kills(0)=0 + placement(0)=0 (zero-stat floor) + participation 1
        #   + prize(0)=5 = 6
        self.assertEqual(result.prize_money_pts, 5)
        self.assertEqual(result.total, 6.0)


class TestTierClassification(unittest.TestCase):
    """Spec §11 — 4-tier thresholds, both sides of every boundary."""

    def test_score_to_tier_boundaries(self):
        cases = [
            (150, 0),       # Elite floor
            (150.0, 0),
            (200, 0),
            (149.99, 1),    # FLAG C: just below 150 -> Competitive
            (149, 1),
            (90, 1),        # Competitive floor
            (89.99, 2),     # Rising
            (89, 2),
            (40, 2),        # Rising floor
            (39.99, 3),     # Entry
            (39, 3),
            (0, 3),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(score_to_tier(score), expected)

    def test_classify_tier_alias(self):
        # classify_tier is the orchestrator-requested name; same behaviour.
        for score in (150, 149.99, 90, 89.99, 40, 39.99, 0):
            with self.subTest(score=score):
                self.assertEqual(classify_tier(score), score_to_tier(score))

    def test_assign_tier_floor_override(self):
        # §7.4 / §9.2: floor not met -> forced Entry (3) regardless of score.
        self.assertEqual(assign_tier(200, meets_participation_floor=False), 3)
        self.assertEqual(assign_tier(200, meets_participation_floor=True), 0)
        self.assertEqual(assign_tier(95, meets_participation_floor=True), 1)
        self.assertEqual(assign_tier(0, meets_participation_floor=True), 3)


class TestPlayerTier(unittest.TestCase):
    """Spec §9.1 / §9.2 — team-tier inheritance vs individual scoring."""

    def test_attached_inherits_team_tier(self):
        # Attached: inherit team tier regardless of individual score.
        tier, source = player_tier(
            is_attached=True, team_tier=1, individual_score=200, meets_floor=True
        )
        self.assertEqual(tier, 1)
        self.assertEqual(source, "team")

    def test_attached_inherits_even_with_zero_individual(self):
        tier, source = player_tier(
            is_attached=True, team_tier=0, individual_score=0, meets_floor=False
        )
        self.assertEqual(tier, 0)
        self.assertEqual(source, "team")

    def test_attached_without_team_tier_raises(self):
        with self.assertRaises(ValueError):
            player_tier(is_attached=True, team_tier=None, individual_score=50, meets_floor=True)

    def test_unattached_uses_individual_score(self):
        tier, source = player_tier(
            is_attached=False, team_tier=None, individual_score=95, meets_floor=True
        )
        self.assertEqual(tier, 1)
        self.assertEqual(source, "individual")

    def test_unattached_floor_override(self):
        # §9.2: unattached with floor unmet -> Entry regardless of score.
        tier, source = player_tier(
            is_attached=False, team_tier=None, individual_score=200, meets_floor=False
        )
        self.assertEqual(tier, 3)
        self.assertEqual(source, "individual")


class TestAnnualScore(unittest.TestCase):
    """Spec §10 — annual = sum of four quarterly scores."""

    def test_sum(self):
        self.assertEqual(annual_score(40, 0, 90, 150), 280.0)

    def test_zero_activity(self):
        self.assertEqual(annual_score(0, 0, 0, 0), 0.0)

    def test_floats(self):
        self.assertAlmostEqual(annual_score(37.7, 44.0, 0.0, 98.0), 179.7)


if __name__ == "__main__":
    unittest.main()
