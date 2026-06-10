"""
Task 2.5 - endpoint tests for POST leaderboards/standalone/<id>/ocr/apply/ (ocr_apply) and the
shared helpers extracted from add_participant + save_match_results.

ocr_apply takes the reviewed OCR rows and, in ONE transaction, resolves each participant (real,
ghost_new, or ghost_existing) via the SAME _resolve_or_create_participant helper add_participant now
uses, creates one LeaderboardMatch, and writes a scored ParticipantMatchResult per row via the SAME
_save_one_result helper save_match_results now uses. No duplicate point math, no duplicate ghost
creation.

Covers: apply real + ghost_new + ghost_existing in one call (participants created, XOR honored), one
match + 3 results, points scored, standings ordered; non-manager 403. The pre-existing
add_participant + save_match_results tests (test_participants.py / test_results.py) must STILL pass
after the refactor - they assert behavior is unchanged.
"""
import json
from django.test import TestCase, Client

from afc_rankings.models import GhostTeam, GhostPlayer
from afc_leaderboard.models import (
    StandaloneLeaderboard, LeaderboardParticipant, LeaderboardMatch, ParticipantMatchResult,
)

from ._helpers import make_afc_admin, make_user, make_team, bearer


class OcrApplyTeamTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.stranger, self.stranger_tok = make_user("stranger")
        self.lb = StandaloneLeaderboard.objects.create(
            name="TeamLB", format="team", placement_points={"1": 12, "2": 9, "3": 8}, kill_point=1.0,
            creator=self.admin,
        )
        self.alpha = make_team("Alpha", self.admin)                       # real team for kind=real
        self.existing_ghost = GhostTeam.objects.create(
            team_name="ReuseGhost", country="NG", created_by=self.admin,  # kind=ghost_existing
        )

    def _apply(self, rows, tok=None, match_map=None):
        body = {"rows": rows}
        if match_map is not None:
            body["match_map"] = match_map
        return self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/apply/",
            data=json.dumps(body), content_type="application/json",
            **bearer(tok or self.admin_tok),
        )

    def test_apply_mixed_resolutions_creates_everything(self):
        rows = [
            {"placement": 1, "kills": 5, "resolution": {"kind": "real", "id": self.alpha.team_id}},
            {"placement": 2, "kills": 2, "resolution": {"kind": "ghost_new", "name": "Phantoms", "country": "GH"}},
            {"placement": 3, "kills": 0, "resolution": {"kind": "ghost_existing", "id": str(self.existing_ghost.ghost_team_id)}},
        ]
        resp = self._apply(rows)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        # Three participants created (one per row), XOR honored (each has exactly one entity FK).
        self.assertEqual(LeaderboardParticipant.objects.filter(leaderboard=self.lb).count(), 3)
        for p in LeaderboardParticipant.objects.filter(leaderboard=self.lb):
            set_fks = [bool(p.team_id), bool(p.ghost_team_id), bool(p.user_id), bool(p.ghost_player_id)]
            self.assertEqual(sum(set_fks), 1)

        # A brand-new ghost team was created for the ghost_new row.
        self.assertTrue(GhostTeam.objects.filter(team_name="Phantoms").exists())

        # Exactly one match + three results.
        self.assertEqual(LeaderboardMatch.objects.filter(leaderboard=self.lb).count(), 1)
        self.assertEqual(ParticipantMatchResult.objects.filter(match__leaderboard=self.lb).count(), 3)

        # Points were scored via the shared helper: placement 1 (12) + 5 kills = 17 for Alpha.
        match = LeaderboardMatch.objects.get(leaderboard=self.lb)
        alpha_p = LeaderboardParticipant.objects.get(leaderboard=self.lb, team=self.alpha)
        row = ParticipantMatchResult.objects.get(match=match, participant=alpha_p)
        self.assertEqual(row.total_points, 17)

        # Response carries the match, participants, and ordered standings (Alpha #1 with 17 pts).
        self.assertIn("match", body)
        self.assertEqual(len(body["participants"]), 3)
        self.assertEqual(body["standings"][0]["total_points"], 17)
        self.assertEqual(body["standings"][0]["rank"], 1)

    def test_apply_reuses_existing_participant(self):
        # If a team is already a participant, applying a row for it must NOT create a duplicate.
        LeaderboardParticipant.objects.create(leaderboard=self.lb, team=self.alpha)
        rows = [{"placement": 1, "kills": 3, "resolution": {"kind": "real", "id": self.alpha.team_id}}]
        resp = self._apply(rows)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            LeaderboardParticipant.objects.filter(leaderboard=self.lb, team=self.alpha).count(), 1,
        )

    def test_second_apply_creates_new_match(self):
        rows = [{"placement": 1, "kills": 1, "resolution": {"kind": "real", "id": self.alpha.team_id}}]
        self._apply(rows)
        self._apply(rows)
        # Each apply is one map; re-applying adds another match (not an overwrite).
        self.assertEqual(LeaderboardMatch.objects.filter(leaderboard=self.lb).count(), 2)

    def test_non_manager_403(self):
        rows = [{"placement": 1, "kills": 5, "resolution": {"kind": "real", "id": self.alpha.team_id}}]
        self.assertEqual(self._apply(rows, tok=self.stranger_tok).status_code, 403)

    def test_empty_rows_rejected(self):
        self.assertEqual(self._apply([]).status_code, 400)


class OcrApplySoloTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="SoloLB", format="solo", placement_points={"1": 12, "2": 9}, kill_point=1.0,
            creator=self.admin,
        )
        self.soloman, _ = make_user("soloman")
        self.existing_gp = GhostPlayer.objects.create(ign="ReuseIGN")

    def test_apply_solo_real_and_ghost(self):
        rows = [
            {"placement": 1, "kills": 4, "resolution": {"kind": "real", "id": self.soloman.user_id}},
            {"placement": 2, "kills": 1, "resolution": {"kind": "ghost_new", "name": "ShadowIGN"}},
            {"placement": 3, "kills": 0, "resolution": {"kind": "ghost_existing", "id": self.existing_gp.id}},
        ]
        resp = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/apply/",
            data=json.dumps({"rows": rows}), content_type="application/json",
            **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LeaderboardParticipant.objects.filter(leaderboard=self.lb).count(), 3)
        self.assertTrue(GhostPlayer.objects.filter(ign="ShadowIGN").exists())
        # solo: placement 1 (12) + 4 kills = 16 for soloman, the standings leader.
        body = resp.json()
        self.assertEqual(body["standings"][0]["total_points"], 16)
