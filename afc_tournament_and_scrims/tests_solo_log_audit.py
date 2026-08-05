r"""The uploaded match file is kept for SOLO events too.

THE BUG (owner backlog item 16): "bulk result upload in 3d-room setups does not list all match
files under stored files". The honest answer was that it listed NONE of them. The team upload has
stored the uploaded .log as a MatchResultLog since 2026-07-07 as an audit trail, and the SOLO
upload never did. A 3D room setup is a solo event, so every file uploaded through it was parsed,
scored and thrown away, leaving nothing to re-check when a result was disputed.

Run: .venv\Scripts\python.exe manage.py test afc_tournament_and_scrims.tests_solo_log_audit
"""
import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    MatchResultLog,
    RegisteredCompetitors,
    StageGroups,
    Stages,
)

URL = "/events/upload-solo-match-result/"

# Two players in the shape SOLO_BLOCK_RE actually reads: a "Rank:" line, then a line carrying
# NAME, ID and KILL together. The exact scoring does not matter here; what is under test is whether
# the FILE survives the upload.
LOG_TEXT = "\r\n".join([
    "Rank: 1",
    "NAME: SoloOne ID: 111111 KILL: 5",
    "",
    "Rank: 2",
    "NAME: SoloTwo ID: 222222 KILL: 3",
    "",
])


class SoloUploadKeepsTheFileTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = datetime.date.today()

        self.admin = User.objects.create(
            username="solo_log_admin", email="solo_log_admin@x.com",
            full_name="Solo Log Admin", role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="solo-log-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="solo", event_type="internal",
            max_teams_or_players=48, event_name="Solo Log Cup", event_mode="virtual",
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
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="Solo LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda")

        for index, uid in enumerate(("111111", "222222"), start=1):
            player = User.objects.create(
                username=f"solo_player_{index}", email=f"solo_player_{index}@x.com",
                full_name=f"Solo Player {index}", role="player", password="x", uid=uid)
            RegisteredCompetitors.objects.create(
                event=self.event, user=player, status="approved")

    def _upload(self, *, dry_run=False, name="MatchResult_2026.log"):
        payload = {
            "match_id": self.match.match_id,
            "file": SimpleUploadedFile(name, LOG_TEXT.encode("utf-8"), content_type="text/plain"),
        }
        if dry_run:
            payload["dry_run"] = "true"
        return self.client.post(
            URL, data=payload, HTTP_AUTHORIZATION="Bearer solo-log-token")

    def test_a_real_solo_upload_keeps_the_file(self):
        resp = self._upload()

        self.assertEqual(resp.status_code, 200, resp.content)
        stored = MatchResultLog.objects.filter(match=self.match)
        self.assertEqual(stored.count(), 1)
        self.assertEqual(stored.first().file_name, "MatchResult_2026.log")

    def test_the_stored_bytes_are_the_file_that_was_uploaded(self):
        """Stored from the raw bytes, not from a re-encode of the decoded text. The parser decodes
        with errors="ignore", so re-encoding would quietly rewrite whatever it dropped, and the
        difference is exactly what somebody re-checking a disputed result needs to see."""
        self._upload()

        stored = MatchResultLog.objects.get(match=self.match)
        stored.file.open("rb")
        try:
            self.assertEqual(stored.file.read(), LOG_TEXT.encode("utf-8"))
        finally:
            stored.file.close()

    def test_a_preview_stores_nothing(self):
        """A dry run exists to show an organizer what WOULD happen. Filing the file as evidence of
        a result that was never saved would put a lie in the audit trail."""
        resp = self._upload(dry_run=True)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(MatchResultLog.objects.filter(match=self.match).count(), 0)

    def test_every_upload_appends_rather_than_replacing(self):
        """The point of the trail is the HISTORY. A re-upload that overwrote the previous file
        would destroy the evidence of what the first attempt contained, which is the case somebody
        is most likely to be arguing about."""
        self._upload(name="first.log")
        self._upload(name="second.log")

        names = list(MatchResultLog.objects.filter(match=self.match)
                     .order_by("log_id").values_list("file_name", flat=True))
        self.assertEqual(names, ["first.log", "second.log"])
