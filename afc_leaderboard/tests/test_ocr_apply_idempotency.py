"""
A4 - idempotency guard for the legacy single-shot standalone apply (ocr_apply).

ocr_apply is stateless: each POST used to unconditionally create a new LeaderboardMatch, so a retry
(double click / network blip) produced a SECOND map that double-counted the standings. The fix keys
idempotency on the draft_id ocr_extract minted: _apply_ocr_rows stamps it onto the created map's
source_draft_id, and ocr_apply 409s (code "ocr_already_applied", handing back the existing
applied_match_id) when a map already exists for that draft_id. This mirrors ocr_job_apply's batch guard.

Mirrors the existing test_ocr_apply.py fixture idiom (StandaloneLeaderboard + a real Team, driven via
a minted SessionToken).
"""
import json

from django.test import TestCase, Client

from afc_leaderboard.models import StandaloneLeaderboard, LeaderboardMatch

from ._helpers import make_afc_admin, make_team, bearer


class OcrApplyIdempotencyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="IdemLB", format="team", placement_points={"1": 12, "2": 9}, kill_point=1.0,
            creator=self.admin,
        )
        self.alpha = make_team("Alpha", self.admin)
        # One row: Alpha at placement 1 with 5 kills -> 12 + 5 = 17 points.
        self.rows = [{"placement": 1, "kills": 5, "resolution": {"kind": "real", "id": self.alpha.team_id}}]

    def _apply(self, draft_id=None, rows=None):
        body = {"rows": rows if rows is not None else self.rows}
        if draft_id is not None:
            body["draft_id"] = draft_id
        return self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/apply/",
            data=json.dumps(body), content_type="application/json",
            **bearer(self.admin_tok),
        )

    def test_double_apply_same_draft_creates_one_map(self):
        first = self._apply(draft_id="draft-abc")
        self.assertEqual(first.status_code, 200)
        second = self._apply(draft_id="draft-abc")
        self.assertEqual(second.status_code, 409)   # the retry is blocked, no second map

        # Exactly one map, and the standings reflect one apply (17), not a doubled 34.
        self.assertEqual(LeaderboardMatch.objects.filter(leaderboard=self.lb).count(), 1)
        standings = first.json()["standings"]
        self.assertEqual(standings[0]["total_points"], 17)

    def test_apply_different_draft_creates_second_map(self):
        # A legitimate SECOND map (a different draft) must still be created - the guard only blocks
        # the SAME draft_id, never a genuine new upload.
        self.assertEqual(self._apply(draft_id="draft-1").status_code, 200)
        self.assertEqual(self._apply(draft_id="draft-2").status_code, 200)
        self.assertEqual(LeaderboardMatch.objects.filter(leaderboard=self.lb).count(), 2)

    def test_apply_response_reports_existing_on_retry(self):
        first = self._apply(draft_id="draft-xyz")
        self.assertEqual(first.status_code, 200)
        applied_id = LeaderboardMatch.objects.get(leaderboard=self.lb).id

        retry = self._apply(draft_id="draft-xyz")
        self.assertEqual(retry.status_code, 409)
        body = retry.json()
        self.assertEqual(body["code"], "ocr_already_applied")
        self.assertEqual(body["applied_match_id"], applied_id)
