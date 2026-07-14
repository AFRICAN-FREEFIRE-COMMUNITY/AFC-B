"""
GET /events/ocr-session/<id>/roster/ - the roster-scoped player picker endpoint.

Proves the new ocr_session_roster view (afc_ocr.views) returns the players REGISTERED for the
session's event, so OCRReviewTable.tsx (lib/api/ocr.ts getSessionRoster) can populate a
roster-gated "matched player" searchable picker. It mirrors the solo-event fixture idiom in
test_image_validation.py (real Event + Stage + Group + Leaderboard + Match, admin + SessionToken)
and the register idiom in test_ringer_flag.py, then builds a solo OCRSession + a
RegisteredCompetitors row and hits the endpoint over HTTP.

Asserts:
  - 200 with the registered player present in "players" and event_type == "solo".
  - a NON-registered user never appears in "players" (roster gate is real).
  - a bad Bearer token returns 401 (reuses the shared _auth handshake).

Run: venv\\Scripts\\python.exe manage.py test afc_ocr.tests.test_session_roster
"""
import datetime

from django.test import TestCase, Client

from afc_auth.models import User, SessionToken
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, Leaderboard, RegisteredCompetitors,
)
from afc_ocr.models import OCRSession


class OcrSessionRosterTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = datetime.date.today()

        # Admin caller + a real SessionToken (the endpoint authenticates via Bearer + validate_token,
        # same as every other OCR session view). We mint the token rather than typing a password.
        self.admin = User.objects.create(
            username="ocr_roster_admin", email="ocr_roster_admin@x.com",
            full_name="OCR Roster Admin", role="admin", password="x")
        self.token = SessionToken.objects.create(
            user=self.admin, token="tok_ocr_roster_admin").token

        # A SOLO event so get_registered_players walks the RegisteredCompetitors branch and returns
        # team_id/team_name = null; event_type resolves to "solo".
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="solo", event_type="internal",
            max_teams_or_players=48, event_name="OCR Roster Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.lb = Leaderboard.objects.create(
            leaderboard_name="Roster LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12}, kill_point=1.0,
            leaderboard_method="image_upload")
        self.match = Match.objects.create(
            leaderboard=self.lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12}, "kill_point": 1})

        # One REGISTERED player (should appear in the roster) and one NON-registered player
        # (must never appear). Solo registrations link the user directly.
        self.registered_player = User.objects.create(
            username="registered_pro", email="registered_pro@x.com",
            full_name="Registered Pro", role="player", password="x", uid="regpro1")
        RegisteredCompetitors.objects.create(
            event=self.event, user=self.registered_player, status="registered")

        self.outsider = User.objects.create(
            username="not_registered", email="not_registered@x.com",
            full_name="Not Registered", role="player", password="x", uid="outsider1")

        # A solo OCRSession hanging off the match (map 1). raw_output/draft_rows shape does not
        # matter for the roster read; the endpoint only resolves match -> event -> roster.
        self.session = OCRSession.objects.create(
            match=self.match, map_index=1, created_by=self.admin, event_type="solo",
            raw_output={"placements": []}, draft_rows=[])

    def _url(self):
        return f"/events/ocr-session/{self.session.session_id}/roster/"

    def test_returns_registered_player_and_solo_type(self):
        resp = self.client.get(self._url(), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(resp.status_code, 200, resp.content)

        body = resp.json()
        self.assertEqual(body["event_type"], "solo")

        user_ids = {p["user_id"] for p in body["players"]}
        self.assertIn(self.registered_player.pk, user_ids)

        # Solo rows carry no team context.
        me = next(p for p in body["players"] if p["user_id"] == self.registered_player.pk)
        self.assertEqual(me["username"], "registered_pro")
        self.assertIsNone(me["team_id"])
        self.assertIsNone(me["team_name"])

    def test_non_registered_user_absent(self):
        resp = self.client.get(self._url(), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(resp.status_code, 200, resp.content)

        user_ids = {p["user_id"] for p in resp.json()["players"]}
        self.assertNotIn(self.outsider.pk, user_ids)

    def test_bad_token_401(self):
        resp = self.client.get(self._url(), HTTP_AUTHORIZATION="Bearer not_a_real_token")
        self.assertEqual(resp.status_code, 401, resp.content)
