"""
A3 - tests that commit_solo_result surfaces skipped rows instead of silently dropping them, and that
commit_ocr_session echoes them in the 200 body under "skipped_rows".

Before A3, a solo row whose matched user had no RegisteredCompetitors entry for the event was silently
`continue`d past: the player vanished with no record and no signal to the reviewer (the solo-path
parallel of the team path's #14 silent-drop bug). commit_solo_result now returns (lb, skipped) where
each skipped row is {"raw_name","matched_user_id","placement","kills","reason"} with reason
"not_registered" (matched user not on the roster) or "no_user" (no matched_user_id at all), and
commit_ocr_session forwards that list as "skipped_rows" (kept DISTINCT from the team path's
"missing_teams" so the FE labels players vs teams correctly).

Fixture idiom mirrors test_ringer_flag (real Event + Leaderboard + Match), driven for a SOLO event.
The endpoint test mints a real SessionToken and hits POST /events/ocr-session/<id>/commit/.
"""
import datetime
import json

from django.test import TestCase, Client

from afc_auth.models import User, SessionToken
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, Leaderboard,
    RegisteredCompetitors, SoloPlayerMatchStats,
)
from afc_ocr.models import OCRSession
from afc_ocr.services.commit import commit_solo_result


class _SoloFixtureMixin:
    """Builds one solo event -> stage -> group -> leaderboard -> match, plus an admin User + token."""

    def _build_solo_match(self):
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="ocr_solo_admin", email="ocr_solo_admin@x.com",
            full_name="OCR Solo Admin", role="admin", password="x")
        self.token = SessionToken.objects.create(user=self.admin, token="tok_ocr_solo_admin").token
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="solo", event_type="internal",
            max_teams_or_players=48, event_name="OCR Solo Cup", event_mode="virtual",
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
            leaderboard_name="Solo LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="image_upload")
        self.match = Match.objects.create(
            leaderboard=self.lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})

    def _competitor(self, username):
        """A registered solo competitor (a User + a RegisteredCompetitors row for this event)."""
        u = User.objects.create(
            username=username, email=f"{username}@x.com", full_name=username.title(),
            role="player", password="x")
        RegisteredCompetitors.objects.create(event=self.event, user=u, status="registered")
        return u

    def _solo_row(self, user, placement, kills, row_id=None):
        # The already-resolved OCR row shape the review step feeds to commit: every row carries a
        # matched_user_id (identity resolved) plus placement/kills. row_id lets save_name_corrections
        # (run by the commit view) diff against the draft without a KeyError.
        return {
            "row_id": row_id or f"r{user.pk}",
            "raw_name": user.full_name,
            "matched_user_id": user.pk,
            "placement": placement,
            "kills": kills,
            "team_mismatch": False,
            "admin_confirmed_sub": False,
        }


class CommitSoloSkippedServiceTests(_SoloFixtureMixin, TestCase):
    """Direct commit_solo_result(match, final_rows) calls - no HTTP."""

    def setUp(self):
        self._build_solo_match()

    def test_solo_skips_unregistered_user_and_reports_it(self):
        registered = self._competitor("reg_solo")
        # A real user with NO RegisteredCompetitors row for this event.
        unregistered = User.objects.create(
            username="unreg_solo", email="unreg_solo@x.com", full_name="Unreg Solo",
            role="player", password="x")

        final_rows = [
            self._solo_row(registered, placement=1, kills=5),
            self._solo_row(unregistered, placement=2, kills=3),
        ]
        lb, skipped = commit_solo_result(self.match, final_rows)

        # Only the registered competitor gets a stat row written.
        stats = SoloPlayerMatchStats.objects.filter(match=self.match)
        self.assertEqual(stats.count(), 1)
        self.assertEqual(stats.first().competitor.user_id, registered.pk)

        # The unregistered row is surfaced (not silently dropped) with the right reason + identity.
        self.assertEqual(len(skipped), 1)
        drop = skipped[0]
        self.assertEqual(drop["reason"], "not_registered")
        self.assertEqual(drop["matched_user_id"], unregistered.pk)
        self.assertEqual(drop["raw_name"], unregistered.full_name)
        self.assertEqual(drop["placement"], 2)
        self.assertEqual(drop["kills"], 3)

    def test_solo_all_registered_returns_empty_skipped(self):
        a = self._competitor("reg_a")
        b = self._competitor("reg_b")
        final_rows = [
            self._solo_row(a, placement=1, kills=4),
            self._solo_row(b, placement=2, kills=1),
        ]
        lb, skipped = commit_solo_result(self.match, final_rows)

        self.assertEqual(skipped, [])
        self.assertEqual(SoloPlayerMatchStats.objects.filter(match=self.match).count(), 2)


class CommitSoloSkippedViewTests(_SoloFixtureMixin, TestCase):
    """Full POST /events/ocr-session/<id>/commit/ path - asserts skipped_rows rides in the 200 body."""

    def setUp(self):
        self.client = Client()
        self._build_solo_match()

    def test_commit_view_returns_skipped_rows(self):
        registered = self._competitor("reg_view")
        unregistered = User.objects.create(
            username="unreg_view", email="unreg_view@x.com", full_name="Unreg View",
            role="player", password="x")

        draft_rows = [
            self._solo_row(registered, placement=1, kills=6),
            self._solo_row(unregistered, placement=2, kills=2),
        ]
        session = OCRSession.objects.create(
            match=self.match, map_index=1, created_by=self.admin, event_type="solo",
            raw_output={"placements": []}, draft_rows=draft_rows)

        resp = self.client.post(
            f"/events/ocr-session/{session.session_id}/commit/",
            data=json.dumps({}), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # The dropped unregistered player is reported in the DISTINCT solo channel.
        self.assertIn("skipped_rows", body)
        self.assertEqual(len(body["skipped_rows"]), 1)
        self.assertEqual(body["skipped_rows"][0]["reason"], "not_registered")
        self.assertEqual(body["skipped_rows"][0]["matched_user_id"], unregistered.pk)
        # The registered player still committed.
        self.assertEqual(SoloPlayerMatchStats.objects.filter(match=self.match).count(), 1)
