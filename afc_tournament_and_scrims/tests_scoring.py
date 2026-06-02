"""
Parity tests for afc_tournament_and_scrims.scoring.

These pin the new shared point formula (scoring.compute_team_points /
compute_solo_points) to the exact numbers the live inline code in views.py has
always produced — so the upcoming refactor that routes every call site through
scoring.* cannot silently change a single stored score.

They are SimpleTestCase (no DB) on purpose: the formula is pure arithmetic, so
the suite runs without MySQL / migrations.
"""

from django.test import SimpleTestCase

from afc_tournament_and_scrims import scoring


class ComputeTeamPointsTests(SimpleTestCase):
    def setUp(self):
        # the canonical FF table = scoring.DEFAULT_PLACEMENT
        self.pp = scoring.DEFAULT_PLACEMENT

    def test_team_played_placement_plus_kills(self):
        # mirrors views.py:12596-12611 exactly
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=1, kills=8, damage=0, assists=0,
            bonus=0, penalty=0, played=True,
        )
        self.assertEqual(r, {"placement_points": 12, "kill_points": 8, "total_points": 20})

    def test_team_bonus_and_penalty(self):
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=3, kills=4, damage=0, assists=0,
            bonus=5, penalty=2, played=True,
        )
        # 8 + 4 + 5 - 2 = 15
        self.assertEqual(r["total_points"], 15)

    def test_team_assist_and_damage_points(self):
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.5,
            points_per_1000_damage=2.0, placement=2, kills=3, damage=3000, assists=4,
            bonus=0, penalty=0, played=True,
        )
        # 9 + 3 + (4*0.5=2) + (3000/1000*2=6) = 20 -> int
        self.assertEqual(r["total_points"], 20)

    def test_team_not_played_is_zero_minus_penalty(self):
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=0, kills=0, damage=0, assists=0,
            bonus=0, penalty=0, played=False,
        )
        self.assertEqual(r, {"placement_points": 0, "kill_points": 0, "total_points": 0})


class ComputeSoloPointsTests(SimpleTestCase):
    def test_solo_played(self):
        # mirrors views.py:13118-13120
        r = scoring.compute_solo_points(
            placement_points=scoring.DEFAULT_PLACEMENT, kill_point=1.0,
            placement=1, kills=5, played=True,
        )
        self.assertEqual(r, {"placement_points": 12, "kill_points": 5, "total_points": 17})

    def test_solo_not_played(self):
        r = scoring.compute_solo_points(
            placement_points=scoring.DEFAULT_PLACEMENT, kill_point=1.0,
            placement=0, kills=0, played=False,
        )
        self.assertEqual(r, {"placement_points": 0, "kill_points": 0, "total_points": 0})
