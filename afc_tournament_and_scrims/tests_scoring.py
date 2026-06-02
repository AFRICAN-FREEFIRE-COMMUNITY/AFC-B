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

    def test_team_not_played_zeroes_real_input(self):
        # Stronger than "0 in -> 0 out": feed a winning placement + kills with played=False
        # and prove the guard zeroes everything (no placement_points, no kill_points leak through).
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=1, kills=5, damage=0, assists=0,
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

    def test_solo_not_played_zeroes_real_input(self):
        # Stronger than "0 in -> 0 out": feed a winning placement + kills with played=False
        # and prove the guard zeroes everything.
        r = scoring.compute_solo_points(
            placement_points=scoring.DEFAULT_PLACEMENT, kill_point=1.0,
            placement=1, kills=5, played=False,
        )
        self.assertEqual(r, {"placement_points": 0, "kill_points": 0, "total_points": 0})


class NormalizePlacementPointsTests(SimpleTestCase):
    """scoring.normalize_placement_points is now the single normalizer (replaces the old
    per-call-site _normalize_* copies). These pin its three branches: empty->default,
    str-keys->int-keys, non-dict->loud ValueError."""

    def test_empty_falls_back_to_default(self):
        # empty / falsy input -> the canonical FF table, not an empty dict
        self.assertEqual(scoring.normalize_placement_points({}), scoring.DEFAULT_PLACEMENT)
        self.assertEqual(scoring.normalize_placement_points(None), scoring.DEFAULT_PLACEMENT)

    def test_string_keys_and_values_coerced_to_int(self):
        # stored JSON arrives with string keys/values; normalizer returns int->int
        self.assertEqual(scoring.normalize_placement_points({"1": "12"}), {1: 12})

    def test_non_dict_raises_value_error(self):
        # fail loud on a structurally wrong payload (e.g. a list) instead of silently defaulting
        with self.assertRaises(ValueError):
            scoring.normalize_placement_points([1, 2, 3])
