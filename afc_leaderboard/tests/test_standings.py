"""
Tests for afc_leaderboard.standings.standalone_standings — the on-read standings aggregator.

Verifies: summed totals, booyah count, the event-standings sort chain
(-effective_total, -booyahs, -kills, last_match_placement, name), and that a participant with no
results still appears (all-zero row, sorted last).
"""
from django.test import TestCase

from afc_leaderboard.models import (
    StandaloneLeaderboard, LeaderboardParticipant, LeaderboardMatch, ParticipantMatchResult,
)
from afc_leaderboard.standings import standalone_standings

from ._helpers import make_afc_admin, make_team


class StandingsTests(TestCase):
    def setUp(self):
        self.admin, _ = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="LB", format="team", placement_points={"1": 12, "2": 9}, kill_point=1.0,
            creator=self.admin,
        )
        self.t_alpha = make_team("Alpha", self.admin)
        self.t_bravo = make_team("Bravo", self.admin)
        self.p_alpha = LeaderboardParticipant.objects.create(leaderboard=self.lb, team=self.t_alpha)
        self.p_bravo = LeaderboardParticipant.objects.create(leaderboard=self.lb, team=self.t_bravo)
        self.m1 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=1)
        self.m2 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=2)

    def _result(self, match, participant, placement, kills, placement_pts, kill_pts, total):
        return ParticipantMatchResult.objects.create(
            match=match, participant=participant,
            placement=placement, kills=kills,
            placement_points=placement_pts, kill_points=kill_pts, total_points=total,
            played=True,
        )

    def test_order_and_totals(self):
        # Alpha: m1 placement1(12)+3 kills(3)=15 ; m2 placement2(9)+1 kill(1)=10  => total 25, booyahs 1
        self._result(self.m1, self.p_alpha, 1, 3, 12, 3, 15)
        self._result(self.m2, self.p_alpha, 2, 1, 9, 1, 10)
        # Bravo: m1 placement2(9)+0=9 ; m2 placement1(12)+2 kills(2)=14 => total 23, booyahs 1
        self._result(self.m1, self.p_bravo, 2, 0, 9, 0, 9)
        self._result(self.m2, self.p_bravo, 1, 2, 12, 2, 14)

        standings = standalone_standings(self.lb)
        self.assertEqual(len(standings), 2)
        # Alpha (25) ranks above Bravo (23).
        self.assertEqual(standings[0]["participant"]["name"], "Alpha")
        self.assertEqual(standings[0]["rank"], 1)
        self.assertEqual(standings[0]["total_points"], 25)
        self.assertEqual(standings[0]["kills"], 4)
        self.assertEqual(standings[0]["booyahs"], 1)
        self.assertEqual(standings[0]["played_count"], 2)
        self.assertEqual(standings[1]["participant"]["name"], "Bravo")
        self.assertEqual(standings[1]["total_points"], 23)

    def test_booyah_tiebreak(self):
        # Equal effective totals -> more booyahs wins. Alpha 2 booyahs, Bravo 0; both total 24.
        self._result(self.m1, self.p_alpha, 1, 0, 12, 0, 12)
        self._result(self.m2, self.p_alpha, 1, 0, 12, 0, 12)
        self._result(self.m1, self.p_bravo, 2, 3, 9, 3, 12)
        self._result(self.m2, self.p_bravo, 2, 3, 9, 3, 12)
        standings = standalone_standings(self.lb)
        self.assertEqual(standings[0]["participant"]["name"], "Alpha")
        self.assertEqual(standings[0]["booyahs"], 2)

    def test_participant_with_no_results_sorts_last(self):
        self._result(self.m1, self.p_alpha, 1, 0, 12, 0, 12)
        # Bravo has no results -> all-zero row, ranked last.
        standings = standalone_standings(self.lb)
        self.assertEqual(standings[0]["participant"]["name"], "Alpha")
        self.assertEqual(standings[1]["participant"]["name"], "Bravo")
        self.assertEqual(standings[1]["total_points"], 0)
        self.assertEqual(standings[1]["played_count"], 0)

    def test_per_match_breakdown(self):
        self._result(self.m1, self.p_alpha, 1, 3, 12, 3, 15)
        self._result(self.m2, self.p_alpha, 2, 1, 9, 1, 10)
        standings = standalone_standings(self.lb)
        alpha = standings[0]
        self.assertEqual(len(alpha["per_match"]), 2)
        self.assertEqual(alpha["per_match"][0]["match_number"], 1)
        self.assertEqual(alpha["per_match"][0]["total_points"], 15)
        self.assertEqual(alpha["per_match"][1]["match_number"], 2)
