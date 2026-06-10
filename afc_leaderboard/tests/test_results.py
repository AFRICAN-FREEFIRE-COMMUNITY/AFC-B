"""
Endpoint tests for add_match / delete_match / save_match_results.

Covers: add a map (auto-incrementing number), save results with points computed via the SAME
scoring helpers the events surface uses (compute_team_points / compute_solo_points), re-save
overwrites (no duplicate row), standings reflect the saved totals, a participant from another
leaderboard is rejected (400), and 403 for a non-manager.
"""
import json
from django.test import TestCase, Client

from afc_tournament_and_scrims.scoring import (
    normalize_placement_points, compute_team_points, compute_solo_points,
)
from afc_leaderboard.models import (
    StandaloneLeaderboard, LeaderboardParticipant, LeaderboardMatch, ParticipantMatchResult,
)
from afc_leaderboard.standings import standalone_standings

from ._helpers import make_afc_admin, make_user, make_team, bearer


class ResultsEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.stranger, self.stranger_tok = make_user("stranger")
        self.lb = StandaloneLeaderboard.objects.create(
            name="LB", format="team", placement_points={"1": 12, "2": 9, "3": 8}, kill_point=1.0,
            creator=self.admin,
        )
        self.team = make_team("Alpha", self.admin)
        self.p = LeaderboardParticipant.objects.create(leaderboard=self.lb, team=self.team)

    def _add_match(self, lb_id, body=None, tok=None):
        return self.client.post(
            f"/leaderboards/standalone/{lb_id}/matches/",
            data=json.dumps(body or {}), content_type="application/json",
            **bearer(tok or self.admin_tok),
        )

    def _save(self, mid, results, tok=None):
        return self.client.post(
            f"/leaderboards/standalone/matches/{mid}/results/",
            data=json.dumps({"results": results}), content_type="application/json",
            **bearer(tok or self.admin_tok),
        )

    # ── add / delete match ──────────────────────────────────────────────────────────────────
    def test_add_match_autoincrements(self):
        m1 = self._add_match(self.lb.id).json()["match"]
        m2 = self._add_match(self.lb.id).json()["match"]
        self.assertEqual(m1["match_number"], 1)
        self.assertEqual(m2["match_number"], 2)

    def test_delete_match(self):
        mid = self._add_match(self.lb.id).json()["match"]["id"]
        resp = self.client.delete(f"/leaderboards/standalone/matches/{mid}/", **bearer(self.admin_tok))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(LeaderboardMatch.objects.filter(id=mid).exists())

    def test_non_manager_cannot_add_match(self):
        self.assertEqual(self._add_match(self.lb.id, tok=self.stranger_tok).status_code, 403)

    # ── save results: points match scoring.* ────────────────────────────────────────────────
    def test_save_results_points_match_scoring(self):
        mid = self._add_match(self.lb.id).json()["match"]["id"]
        resp = self._save(mid, [{"participant_id": self.p.id, "placement": 1, "kills": 5,
                                 "damage": 2000, "assists": 0, "bonus": 0, "penalty": 0}])
        self.assertEqual(resp.status_code, 200)

        # Independently compute the expected points with the shared helper.
        pp = normalize_placement_points(self.lb.placement_points)
        expected = compute_team_points(
            placement_points=pp, kill_point=1.0, points_per_assist=0.0, points_per_1000_damage=0.0,
            placement=1, kills=5, damage=2000, assists=0, bonus=0, penalty=0, played=True,
        )
        row = ParticipantMatchResult.objects.get(match_id=mid, participant=self.p)
        self.assertEqual(row.placement_points, expected["placement_points"])
        self.assertEqual(row.kill_points, expected["kill_points"])
        self.assertEqual(row.total_points, expected["total_points"])
        # placement 1 = 12 + 5 kills = 17.
        self.assertEqual(row.total_points, 17)

    def test_resave_overwrites(self):
        mid = self._add_match(self.lb.id).json()["match"]["id"]
        self._save(mid, [{"participant_id": self.p.id, "placement": 3, "kills": 0}])
        self._save(mid, [{"participant_id": self.p.id, "placement": 1, "kills": 2}])
        # Exactly one row (unique per match+participant), reflecting the latest save.
        self.assertEqual(ParticipantMatchResult.objects.filter(match_id=mid, participant=self.p).count(), 1)
        row = ParticipantMatchResult.objects.get(match_id=mid, participant=self.p)
        self.assertEqual(row.placement, 1)
        self.assertEqual(row.total_points, 14)  # 12 + 2 kills

    def test_standings_reflect_saved_totals(self):
        mid = self._add_match(self.lb.id).json()["match"]["id"]
        self._save(mid, [{"participant_id": self.p.id, "placement": 1, "kills": 3}])
        standings = standalone_standings(self.lb)
        self.assertEqual(standings[0]["total_points"], 15)  # 12 + 3 kills
        self.assertEqual(standings[0]["booyahs"], 1)

    def test_foreign_participant_rejected(self):
        # A participant belonging to a DIFFERENT leaderboard must not be writable here.
        other_lb = StandaloneLeaderboard.objects.create(name="Other", format="team", placement_points={"1": 12}, creator=self.admin)
        other_team = make_team("Beta", self.admin)
        other_p = LeaderboardParticipant.objects.create(leaderboard=other_lb, team=other_team)
        mid = self._add_match(self.lb.id).json()["match"]["id"]
        resp = self._save(mid, [{"participant_id": other_p.id, "placement": 1, "kills": 0}])
        self.assertEqual(resp.status_code, 400)

    def test_empty_results_rejected(self):
        mid = self._add_match(self.lb.id).json()["match"]["id"]
        self.assertEqual(self._save(mid, []).status_code, 400)

    def test_non_manager_cannot_save(self):
        mid = self._add_match(self.lb.id).json()["match"]["id"]
        resp = self._save(mid, [{"participant_id": self.p.id, "placement": 1, "kills": 0}], tok=self.stranger_tok)
        self.assertEqual(resp.status_code, 403)


class SoloResultsTests(TestCase):
    """Solo format uses compute_solo_points (placement + kills only)."""
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="SoloLB", format="solo", placement_points={"1": 12, "2": 9}, kill_point=1.0, creator=self.admin
        )
        self.user, _ = make_user("solo1")
        self.p = LeaderboardParticipant.objects.create(leaderboard=self.lb, user=self.user)

    def test_solo_points_match_scoring(self):
        mid = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/matches/",
            data=json.dumps({}), content_type="application/json", **bearer(self.admin_tok),
        ).json()["match"]["id"]
        self.client.post(
            f"/leaderboards/standalone/matches/{mid}/results/",
            data=json.dumps({"results": [{"participant_id": self.p.id, "placement": 1, "kills": 4}]}),
            content_type="application/json", **bearer(self.admin_tok),
        )
        pp = normalize_placement_points(self.lb.placement_points)
        expected = compute_solo_points(placement_points=pp, kill_point=1.0, placement=1, kills=4, played=True)
        row = ParticipantMatchResult.objects.get(match_id=mid, participant=self.p)
        self.assertEqual(row.total_points, expected["total_points"])
        self.assertEqual(row.total_points, 16)  # 12 + 4 kills
