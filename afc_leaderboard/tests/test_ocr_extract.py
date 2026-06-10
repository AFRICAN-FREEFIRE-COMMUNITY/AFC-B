"""
Task 2.4 - endpoint tests for POST leaderboards/standalone/<id>/ocr/ (ocr_extract).

The standalone-leaderboard OCR assist runs the shared extraction service, then matches the read
names against the WHOLE platform (teams for a team LB, users for a solo LB) and returns a stateless
draft for the FE review table. extract.extract_rows is mocked here so no OCR engine / Gemini HTTP
fires - the test exercises the matching + row-shaping + auth, not the model.

Covers: non-manager 403, missing screenshot 400, team LB returns team-matched rows, solo LB returns
user-matched rows.
"""
import io
from unittest import mock

from django.test import TestCase, Client

from afc_auth.models import User
from afc_leaderboard.models import StandaloneLeaderboard

from ._helpers import make_afc_admin, make_user, make_team, bearer


def _png_upload(name="shot.png"):
    """A tiny in-memory file the multipart client can post as `screenshot`."""
    f = io.BytesIO(b"\x89PNG\r\n\x1a\n fake bytes")
    f.name = name
    return f


# Canonical extract.extract_rows return values for each format (mocked - no engine runs).
_TEAM_RAW = (
    {"match_type": "team", "placements": [
        {"placement": 1, "team_name": "Alpha Squad", "kills": 7,
         "players": [{"name": "a", "kills": 4}, {"name": "b", "kills": 3}]},
        {"placement": 2, "team_name": "Nobody United", "kills": 2,
         "players": [{"name": "c", "kills": 2}]},
    ]},
    "gemini-2.5-pro",
)
_SOLO_RAW = (
    {"match_type": "solo", "placements": [
        {"placement": 1, "players": [{"name": "soloman", "kills": 5}]},
        {"placement": 2, "players": [{"name": "ghostipep", "kills": 1}]},
    ]},
    "gemini-2.5-pro",
)


class OcrExtractTeamTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.stranger, self.stranger_tok = make_user("stranger")
        self.lb = StandaloneLeaderboard.objects.create(
            name="TeamLB", format="team", placement_points={"1": 12}, creator=self.admin,
        )
        # A real team on the platform that the OCR team_name should resolve to.
        self.alpha = make_team("Alpha Squad", self.admin)

    def _post(self, lb_id, tok=None, with_file=True):
        data = {"screenshot": _png_upload()} if with_file else {}
        return self.client.post(
            f"/leaderboards/standalone/{lb_id}/ocr/", data=data,
            **bearer(tok or self.admin_tok),
        )

    @mock.patch("afc_leaderboard.views.extract.extract_rows", return_value=_TEAM_RAW)
    def test_team_lb_returns_team_matched_rows(self, _mock):
        resp = self._post(self.lb.id)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["format"], "team")
        self.assertIn("draft_id", body)
        rows = body["rows"]
        self.assertEqual(len(rows), 2)
        r0 = rows[0]
        self.assertEqual(r0["raw_name"], "Alpha Squad")
        self.assertEqual(r0["placement"], 1)
        self.assertEqual(r0["kills"], 7)                    # placement-level summed team kills
        self.assertEqual(r0["matched_team_id"], self.alpha.team_id)
        self.assertFalse(r0["is_unmatched"])
        self.assertNotIn("matched_user_id", r0)             # team flow keys, not user keys
        # The unmatched ghost team has no real team to resolve to.
        r1 = rows[1]
        self.assertIsNone(r1["matched_team_id"])
        self.assertTrue(r1["is_unmatched"])

    @mock.patch("afc_leaderboard.views.extract.extract_rows", return_value=_TEAM_RAW)
    def test_non_manager_403(self, _mock):
        self.assertEqual(self._post(self.lb.id, tok=self.stranger_tok).status_code, 403)

    @mock.patch("afc_leaderboard.views.extract.extract_rows", return_value=_TEAM_RAW)
    def test_missing_screenshot_400(self, _mock):
        self.assertEqual(self._post(self.lb.id, with_file=False).status_code, 400)


class OcrExtractSoloTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="SoloLB", format="solo", placement_points={"1": 12}, creator=self.admin,
        )
        # A real user on the platform the OCR name should resolve to.
        self.soloman, _ = make_user("soloman")

    @mock.patch("afc_leaderboard.views.extract.extract_rows", return_value=_SOLO_RAW)
    def test_solo_lb_returns_user_matched_rows(self, _mock):
        resp = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/",
            data={"screenshot": _png_upload()}, **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["format"], "solo")
        rows = body["rows"]
        self.assertEqual(len(rows), 2)
        r0 = rows[0]
        self.assertEqual(r0["raw_name"], "soloman")
        self.assertEqual(r0["matched_user_id"], self.soloman.user_id)
        self.assertEqual(r0["kills"], 5)
        self.assertFalse(r0["is_unmatched"])
        self.assertNotIn("matched_team_id", r0)             # solo flow keys, not team keys
        # The unrecognized name resolves to nobody.
        self.assertIsNone(rows[1]["matched_user_id"])
        self.assertTrue(rows[1]["is_unmatched"])
