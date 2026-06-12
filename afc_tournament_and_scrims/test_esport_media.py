"""
Tests for the ESPORT MEDIA feature set (owner 2026-06-12):
  - POST /auth/upload-esport-image/  : replace-only upload of UserProfile.esports_pic.
  - POST /events/download-esport-media/ : ZIP export of team logos + player esport images
    (sets, or event-scoped), admins + organizers only.
  - register_for_event media criteria: require_team_logo / require_esport_images block
    registration until the assets exist.

Run: python manage.py test afc_tournament_and_scrims.test_esport_media
"""
import io
import zipfile
from datetime import date, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client

from afc_auth.models import Roles, SessionToken, User, UserProfile, UserRoles
from afc_team.models import Team
from afc_tournament_and_scrims.models import Event

# A minimal valid PNG (1x1 transparent) so ImageField accepts the upload.
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05"
    b"\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _png(name="img.png"):
    return SimpleUploadedFile(name, _PNG, content_type="image/png")


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x",
    )
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _event(creator, **overrides):
    fields = dict(
        event_name="Media Cup", competition_type="tournament", participant_type="solo",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


class UploadEsportImageTests(TestCase):
    """POST /auth/upload-esport-image/ : auth gate, file required, replace-only."""

    def setUp(self):
        self.user, self.token = _user("imguser")

    def _post(self, file=None, token=None):
        data = {"esport_image": file} if file else {}
        return Client().post(
            "/auth/upload-esport-image/", data,
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        )

    def test_requires_file(self):
        resp = self._post(file=None)
        self.assertEqual(resp.status_code, 400)

    def test_upload_and_replace(self):
        resp = self._post(file=_png("first.png"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["esport_image_url"])
        first = UserProfile.objects.get(user=self.user).esports_pic.name

        # Replace-only: a second upload swaps the image (no delete path exists at all).
        resp2 = self._post(file=_png("second.png"))
        self.assertEqual(resp2.status_code, 200)
        second = UserProfile.objects.get(user=self.user).esports_pic.name
        self.assertNotEqual(first, second)

    def test_rejects_bad_token(self):
        resp = self._post(file=_png(), token="bogus")
        self.assertEqual(resp.status_code, 401)


class DownloadEsportMediaTests(TestCase):
    """POST /events/download-esport-media/ : role gate + zip contents + event scoping."""

    def setUp(self):
        self.admin, self.admin_token = _user("mediaadmin", role="admin")
        self.plain, self.plain_token = _user("plainuser")
        self.organizer, self.org_token = _user("orgdownloader")
        r, _ = Roles.objects.get_or_create(role_name="organizer")
        UserRoles.objects.create(user=self.organizer, role=r)

        self.team = Team.objects.create(
            team_name="Logo Team", team_owner=self.admin, team_creator=self.admin,
            join_settings="open", country="NG", team_logo=_png("logo.png"),
        )
        self.bare_team = Team.objects.create(
            team_name="Bare Team", team_owner=self.admin, team_creator=self.admin,
            join_settings="open", country="NG",
        )
        self.player, _ = _user("picplayer")
        UserProfile.objects.create(user=self.player, esports_pic=_png("esport.png"))

    def _post(self, body, token):
        return Client().post(
            "/events/download-esport-media/", body,
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_plain_user_403(self):
        resp = self._post({"team_ids": [self.team.team_id]}, self.plain_token)
        self.assertEqual(resp.status_code, 403)

    def test_requires_a_selector(self):
        resp = self._post({}, self.admin_token)
        self.assertEqual(resp.status_code, 400)

    def test_admin_zip_contains_assets_and_manifest(self):
        resp = self._post(
            {"team_ids": [self.team.team_id, self.bare_team.team_id],
             "player_ids": [self.player.user_id]},
            self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        self.assertTrue(any(n.startswith("team_logos/Logo_Team") for n in names))
        self.assertTrue(any(n.startswith("esport_images/picplayer") for n in names))
        manifest = zf.read("manifest.txt").decode()
        # The logo-less team is reported, never an error.
        self.assertIn("Bare Team", manifest)

    def test_organizer_role_allowed(self):
        resp = self._post({"player_ids": [self.player.user_id]}, self.org_token)
        self.assertEqual(resp.status_code, 200)


class RegistrationMediaCriteriaTests(TestCase):
    """register_for_event blocks on the event's media criteria (solo esport image + team logo).
    The gates fire BEFORE the Discord checks, so these tests need no Discord mocking."""

    def setUp(self):
        self.admin, _ = _user("critadmin", role="admin")
        self.player, self.player_token = _user("critplayer")

    def _register(self, event, token, **extra):
        body = {"event_id": event.event_id, **extra}
        return Client().post(
            "/events/register-for-event/", body,
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_solo_blocked_without_esport_image(self):
        event = _event(self.admin, require_esport_images=True)
        resp = self._register(event, self.player_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json().get("code"), "esport_image_required")

    def test_solo_passes_gate_with_esport_image(self):
        event = _event(self.admin, require_esport_images=True)
        UserProfile.objects.create(user=self.player, esports_pic=_png())
        resp = self._register(event, self.player_token)
        # The media gate passes; the next gate (Discord) is what blocks now, proving the
        # esport-image criterion itself was satisfied.
        self.assertNotEqual(resp.json().get("code"), "esport_image_required")

    def test_team_blocked_without_logo(self):
        event = _event(self.admin, participant_type="squad", require_team_logo=True)
        team = Team.objects.create(
            team_name="No Logo FC", team_owner=self.player, team_creator=self.player,
            join_settings="open", country="NG",
        )
        from afc_team.models import TeamMembers
        TeamMembers.objects.create(team=team, member=self.player, management_role="team_captain")
        resp = self._register(event, self.player_token, team_id=team.team_id)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json().get("code"), "team_logo_required")
