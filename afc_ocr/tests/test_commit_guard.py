"""
A10 - the commit/extract guards: a malformed final_rows row must not 500, and extraction/commit
failures must return GENERIC client messages that never leak the underlying exception text (which,
pre-A6, could carry the Gemini api key).

Three cases straight from the spec A10 test block:
  1. A final_rows row missing BOTH matched_user_id and raw_name -> 400 (unresolved contains ""),
     NOT a raw 500 from `r["raw_name"]` (now `.get("raw_name","")`, computed before the try).
  2. Extraction failure (extract_rows raises a generic Exception) -> 503 with the generic
     "Could not read that screenshot" message, NOT the exception's own text.
  3. Commit failure (commit_team_result raises) -> 500 with the generic "Could not commit the
     results" message, no {exc} interpolation.

The Gemini boundary is mocked in every case; MEDIA_ROOT is redirected for the upload path's
best-effort image persistence.
"""
import datetime
import json
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings

from afc_auth.models import User, SessionToken
from afc_tournament_and_scrims.models import Event, Stages, StageGroups, Match, Leaderboard
from afc_ocr.models import OCRSession


def _png(name="shot.png"):
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\nfake", content_type="image/png")


class _MatchFixtureMixin:
    """One team event -> stage -> group -> leaderboard -> match, plus an admin User + token."""

    def _build(self):
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="ocr_guard_admin", email="ocr_guard_admin@x.com",
            full_name="OCR Guard Admin", role="admin", password="x")
        self.token = SessionToken.objects.create(user=self.admin, token="tok_ocr_guard_admin").token
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="OCR Guard Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.lb = Leaderboard.objects.create(
            leaderboard_name="Guard LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="image_upload")
        self.match = Match.objects.create(
            leaderboard=self.lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})

    def _enable_temp_media(self):
        self._media = tempfile.mkdtemp(prefix="ocr_guard_media_")
        self.addCleanup(shutil.rmtree, self._media, ignore_errors=True)
        ov = override_settings(MEDIA_ROOT=self._media)
        ov.enable()
        self.addCleanup(ov.disable)


class CommitUnresolvedGuardTests(_MatchFixtureMixin, TestCase):
    def setUp(self):
        self.client = Client()
        self._build()

    def test_unresolved_row_without_raw_name_no_500(self):
        # A hand-built final_rows row with NEITHER matched_user_id NOR raw_name used to KeyError
        # (raw 500) on r["raw_name"]. It must now be a clean 400 whose `unresolved` list carries "".
        session = OCRSession.objects.create(
            match=self.match, map_index=1, created_by=self.admin, event_type="solo",
            raw_output={"placements": []}, draft_rows=[])

        resp = self.client.post(
            f"/events/ocr-session/{session.session_id}/commit/",
            data=json.dumps({"final_rows": [{"placement": 1, "kills": 1}]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}")

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertIn("unresolved", body)
        self.assertIn("", body["unresolved"])   # missing raw_name -> "" placeholder, never a 500


class ExtractionFailureGuardTests(_MatchFixtureMixin, TestCase):
    def setUp(self):
        self.client = Client()
        self._build()
        self._enable_temp_media()

    def test_extraction_failure_message_is_generic(self):
        # A generic (non-friendly) extraction error must be logged server-side and returned as the
        # generic client message - the raw exception text must NEVER reach the response body.
        secret = "SECRET_EXC_TEXT_boom_stacktrace"
        with mock.patch("afc_ocr.services.extract.extract_rows", side_effect=Exception(secret)):
            resp = self.client.post(
                "/events/ocr-match-result/",
                data={"match_id": self.match.match_id, "map_index": 1, "screenshot": _png()},
                HTTP_AUTHORIZATION=f"Bearer {self.token}")

        self.assertEqual(resp.status_code, 503)
        msg = resp.json()["message"]
        self.assertEqual(msg, "Could not read that screenshot. Please try again.")
        self.assertNotIn(secret, msg)


class CommitFailureGuardTests(_MatchFixtureMixin, TestCase):
    def setUp(self):
        self.client = Client()
        self._build()
        self.player = User.objects.create(
            username="committer_player", email="committer_player@x.com",
            full_name="Committer Player", role="player", password="x")

    def test_commit_failure_message_is_generic(self):
        # A fully-resolved team session that passes validation, but the commit service blows up.
        # The view must log the detail and return the generic 500 message with NO {exc} leak.
        row = {
            "row_id": "r1", "raw_name": "Player One", "matched_user_id": self.player.pk,
            "matched_team_id": 123, "placement": 1, "kills": 1,
            "team_mismatch": False, "admin_confirmed_sub": False,
        }
        session = OCRSession.objects.create(
            match=self.match, map_index=1, created_by=self.admin, event_type="team",
            raw_output={"placements": []}, draft_rows=[row])

        secret = "BOOM_SECRET_COMMIT_DETAIL"
        with mock.patch("afc_ocr.services.commit.commit_team_result", side_effect=Exception(secret)):
            resp = self.client.post(
                f"/events/ocr-session/{session.session_id}/commit/",
                data=json.dumps({}), content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {self.token}")

        self.assertEqual(resp.status_code, 500)
        msg = resp.json()["message"]
        self.assertEqual(msg, "Could not commit the results. Please try again.")
        self.assertNotIn(secret, msg)
