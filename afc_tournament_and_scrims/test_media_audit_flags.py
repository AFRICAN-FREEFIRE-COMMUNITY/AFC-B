"""
afc_tournament_and_scrims/test_media_audit_flags.py - the flagged esport images reach the people
who can fix them, and only for their own event (owner 2026-09-13).

The picture check records a verdict on the profile (afc_auth/face_check.py). This is the half that
makes it worth recording: the per-event media audit lists the rostered players whose image was
flagged, and an admin can say "this one is fine" once and never see it again.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_media_audit_flags
"""
import shutil
import tempfile
from datetime import date, timedelta
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from PIL import Image

from afc_auth.models import SessionToken, User, UserProfile
from afc_team.models import Team
from afc_tournament_and_scrims.models import Event, TournamentTeam, TournamentTeamMember

_MEDIA = tempfile.mkdtemp(prefix="afc_media_audit_")


def _jpeg():
    buf = BytesIO()
    Image.new("RGB", (200, 200), (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _user(username, role="player"):
    user = User.objects.create(username=username, email=f"{username}@x.com",
                               full_name=username.title(), role=role, password="x")
    token = SessionToken.objects.create(user=user, token=f"tok_{username}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _event(creator, name="Media Cup"):
    return Event.objects.create(
        event_name=name, competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )


@override_settings(MEDIA_ROOT=_MEDIA)
class MediaAuditFlagTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.admin, self.admin_auth = _user("mediaadmin", role="admin")
        self.player, _ = _user("flagged_player")
        self.clean, _ = _user("clean_player")
        self.stranger, self.stranger_auth = _user("stranger")
        self.event = _event(self.admin)
        self.other_event = _event(self.admin, name="Other Cup")
        team = Team.objects.create(team_name="Roster", team_tag="RST", country="NG",
                                   join_settings="open", team_owner=self.admin, team_creator=self.admin)
        tt = TournamentTeam.objects.create(event=self.event, team=team)
        for user in (self.player, self.clean):
            TournamentTeamMember.objects.create(tournament_team=tt, user=user, event=self.event)
        self.profile = self._profile(self.player, "no_face")
        self._profile(self.clean, "ok")
        self.client = Client()

    def _profile(self, user, check):
        profile = UserProfile.objects.create(user=user, esports_pic_check=check)
        profile.esports_pic.save(f"{user.username}.jpg", SimpleUploadedFile(f"{user.username}.jpg", _jpeg()), save=True)
        return profile

    def _audit(self, event=None, auth=None):
        ev = event or self.event
        return self.client.get(f"/events/{ev.event_id}/media-audit/", **(auth or self.admin_auth))

    def _row(self, body, user):
        return next(p for p in body["players"] if p["user_id"] == user.user_id)

    def test_the_audit_says_which_player_needs_a_look(self):
        r = self._audit()
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertEqual(body["players_image_needs_review"], 1)
        flagged = self._row(body, self.player)
        self.assertTrue(flagged["image_needs_review"])
        self.assertEqual(flagged["image_check"], "no_face")
        clean = self._row(body, self.clean)
        self.assertFalse(clean["image_needs_review"])
        self.assertEqual(clean["image_check"], "ok")

    def test_a_player_with_no_image_is_missing_not_flagged(self):
        # An image that is not there cannot be wrong about its content; that is the other column.
        self.profile.esports_pic.delete(save=False)
        self.profile.esports_pic = ""
        self.profile.save(update_fields=["esports_pic"])
        body = self._audit().json()
        row = self._row(body, self.player)
        self.assertFalse(row["image_needs_review"])
        self.assertFalse(row["has_image"])
        self.assertEqual(body["players_image_needs_review"], 0)

    def test_looks_fine_clears_it_for_good(self):
        r = self.client.post(f"/events/{self.event.event_id}/media-image-check/clear/",
                             {"user_id": self.player.user_id},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.esports_pic_check, "cleared")
        self.assertIsNotNone(self.profile.esports_pic_checked_at)
        body = self._audit().json()
        self.assertEqual(body["players_image_needs_review"], 0)
        self.assertFalse(self._row(body, self.player)["image_needs_review"])

    def test_the_backfill_never_puts_a_cleared_image_back_in_the_queue(self):
        from django.core.management import call_command
        self.profile.esports_pic_check = "cleared"
        self.profile.save(update_fields=["esports_pic_check"])
        call_command("check_esport_images", "--recheck")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.esports_pic_check, "cleared")

    def test_a_stranger_gets_nothing(self):
        self.assertEqual(self._audit(auth=self.stranger_auth).status_code, 403)
        r = self.client.post(f"/events/{self.event.event_id}/media-image-check/clear/",
                             {"user_id": self.player.user_id},
                             content_type="application/json", **self.stranger_auth)
        self.assertEqual(r.status_code, 403)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.esports_pic_check, "no_face")

    def test_another_event_does_not_list_this_roster(self):
        body = self._audit(event=self.other_event).json()
        self.assertEqual(body["players"], [])
        self.assertEqual(body["players_image_needs_review"], 0)

    def test_clearing_an_unknown_player_is_a_404_not_a_500(self):
        r = self.client.post(f"/events/{self.event.event_id}/media-image-check/clear/",
                             {"user_id": 9999999}, content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 404)
