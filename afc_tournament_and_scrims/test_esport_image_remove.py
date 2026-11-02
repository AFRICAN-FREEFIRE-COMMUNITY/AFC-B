"""
Tests for REMOVING the esport image (owner 2026-10-25, inbox #400: "a user should be allowed to
completely remove their esports image").

Covers POST /auth/remove-esport-image/ (afc_auth.views.remove_esport_image):
  - auth is required and coded; it takes no id and only ever touches the caller's own profile (R58/R88)
  - the stored file is deleted and esports_pic / esports_pic_check are cleared; a repeat is a no-op
  - events the player is already in KEEP the image (EventPlayerImage), solo or roster seat
  - a NEW registration for an event with require_esport_images is refused until a new image is uploaded
  - a registered player STAYS registered
  - a REPLACE after a removal drops the kept copies (the new image shows everywhere at once)
  - the event-scoped readers (esports_pic_url(event=...), pic_for_event) read the copy first

Run: python manage.py test afc_tournament_and_scrims.test_esport_image_remove
"""
import os
import shutil
import tempfile
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from afc_auth.models import canonical_profile, esports_pic_url
from afc_team.models import Team
from afc_tournament_and_scrims.event_player_image import events_player_is_in, pic_for_event
from afc_tournament_and_scrims.models import (
    EventPlayerImage, RegisteredCompetitors, TournamentTeam, TournamentTeamMember,
)
from afc_tournament_and_scrims.test_esport_media import _event, _png, _user

_MEDIA = tempfile.mkdtemp(prefix="afc_esport_remove_")


@override_settings(MEDIA_ROOT=_MEDIA)
class RemoveEsportImageTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.admin, _ = _user("rmadmin", role="admin")
        self.player, self.token = _user("rmplayer")
        self.other, self.other_token = _user("rmother")
        # The upload's face check is not what is under test here (see afc_auth/test_face_check.py).
        p = patch("afc_auth.face_check.check_esport_image",
                  return_value={"verdict": "ok", "reason": "ok", "confidence": 0.9,
                                "face_share": 0.3, "detector": "yunet"})
        p.start()
        self.addCleanup(p.stop)

    def _upload(self, token=None, name="pic.png"):
        return Client().post("/auth/upload-esport-image/", {"esport_image": _png(name)},
                             HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    def _remove(self, token=None):
        auth = {} if token == "" else {"HTTP_AUTHORIZATION": f"Bearer {token or self.token}"}
        return Client().post("/auth/remove-esport-image/", **auth)

    def _register(self, event, token=None):
        return Client().post("/events/register-for-event/", {"event_id": event.event_id},
                             content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    # ── auth + ownership (R58 / R88) ──────────────────────────────────────────────
    def test_requires_a_session_with_a_code(self):
        r = self._remove(token="")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "authorization_header_required")
        r = self._remove(token="bogus")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "invalid_expired_session_token")

    def test_only_touches_the_callers_own_profile(self):
        self._upload(self.token)
        self._upload(self.other_token, name="other.png")
        other_name = canonical_profile(self.other).esports_pic.name
        # The endpoint takes no id: even a body naming the other user changes nothing for them.
        r = Client().post("/auth/remove-esport-image/", {"user_id": self.other.user_id},
                          HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(canonical_profile(self.player).esports_pic)
        self.assertEqual(canonical_profile(self.other).esports_pic.name, other_name)
        self.assertTrue(os.path.exists(canonical_profile(self.other).esports_pic.path))

    # ── the removal itself ────────────────────────────────────────────────────────
    def test_deletes_the_file_and_clears_the_record(self):
        self._upload()
        prof = canonical_profile(self.player)
        path = prof.esports_pic.path
        self.assertTrue(os.path.exists(path))
        r = self._remove()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["code"], body["removed"], body["events_kept"]), ("esport_image_removed", True, 0))
        prof = canonical_profile(self.player)
        self.assertFalse(prof.esports_pic)
        self.assertEqual(prof.esports_pic_check, "")
        self.assertIsNone(prof.esports_pic_checked_at)
        self.assertFalse(os.path.exists(path))
        # The profile read now has no image.
        self.assertIsNone(esports_pic_url(self.player))

    def test_a_repeat_removal_is_a_harmless_no_op(self):
        r = self._remove()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["code"], r.json()["removed"]), ("esport_image_already_empty", False))
        self._upload()
        self._remove()
        r = self._remove()
        self.assertEqual(r.json()["code"], "esport_image_already_empty")

    # ── events they are already in keep the image ─────────────────────────────────
    def test_a_solo_event_keeps_its_copy_and_the_player_stays_registered(self):
        event = _event(self.admin, require_esport_images=True)
        self._upload()
        RegisteredCompetitors.objects.create(event=event, user=self.player, status="registered")
        r = self._remove()
        self.assertEqual(r.json()["events_kept"], 1)
        # The player is still registered.
        self.assertTrue(RegisteredCompetitors.objects.filter(event=event, user=self.player, status="registered").exists())
        # The event reads the kept copy; everything without an event does not.
        copy = EventPlayerImage.objects.get(event=event, user=self.player)
        self.assertTrue(os.path.exists(copy.image.path))
        self.assertEqual(pic_for_event(self.player, event).name, copy.image.name)
        self.assertIn("event_copies", esports_pic_url(self.player, event=event))
        self.assertIsNone(esports_pic_url(self.player))

    def test_a_roster_seat_keeps_a_copy_in_every_event_with_one_file(self):
        e1 = _event(self.admin, event_name="One")
        e2 = _event(self.admin, event_name="Two")
        team = Team.objects.create(team_name="RmTeam", team_creator=self.other, team_owner=self.other)
        for ev in (e1, e2):
            tt = TournamentTeam.objects.create(event=ev, team=team)
            TournamentTeamMember.objects.create(tournament_team=tt, user=self.player, event=ev)
        self._upload()
        self.assertEqual(events_player_is_in(self.player), {e1.event_id, e2.event_id})
        self.assertEqual(self._remove().json()["events_kept"], 2)
        names = set(EventPlayerImage.objects.filter(user=self.player).values_list("image", flat=True))
        self.assertEqual(len(names), 1)  # ONE file on disk, two rows
        self.assertEqual(EventPlayerImage.objects.filter(user=self.player).count(), 2)

    def test_events_the_player_left_or_never_joined_keep_nothing(self):
        gone = _event(self.admin, event_name="Gone")
        never = _event(self.admin, event_name="Never")
        self._upload()
        RegisteredCompetitors.objects.create(event=gone, user=self.player, status="withdrawn")
        self.assertEqual(self._remove().json()["events_kept"], 0)
        self.assertFalse(EventPlayerImage.objects.exists())
        self.assertIsNone(pic_for_event(self.player, never))

    # ── new registrations are refused until a new image is uploaded ───────────────
    def test_a_new_registration_needing_an_image_is_refused_until_a_new_one_is_uploaded(self):
        event = _event(self.admin, require_esport_images=True)
        self._upload()
        self._remove()
        r = self._register(event)
        self.assertEqual(r.status_code, 403)
        body = r.json()
        self.assertEqual(body["code"], "registration_requirements_unmet")
        self.assertIn("esports_image", body["missing"][0]["fields"])
        # A new image lifts the refusal at this gate (it no longer lists the image as missing).
        self._upload(name="new.png")
        r = self._register(event)
        self.assertNotIn("esports_image",
                         [f for m in (r.json().get("missing") or []) for f in m["fields"]])

    # ── replace after removal ─────────────────────────────────────────────────────
    def test_a_new_upload_drops_the_kept_copies_and_shows_everywhere(self):
        event = _event(self.admin)
        self._upload()
        RegisteredCompetitors.objects.create(event=event, user=self.player, status="registered")
        self._remove()
        copy_path = EventPlayerImage.objects.get(user=self.player).image.path
        self.assertTrue(os.path.exists(copy_path))
        self._upload(name="fresh.png")
        self.assertFalse(EventPlayerImage.objects.filter(user=self.player).exists())
        self.assertFalse(os.path.exists(copy_path))
        # The event now reads the live profile picture.
        self.assertEqual(pic_for_event(self.player, event).name, canonical_profile(self.player).esports_pic.name)

    # ── the event surfaces read the copy ──────────────────────────────────────────
    def test_the_event_media_zip_still_carries_a_removed_image_for_events_they_are_in(self):
        import io
        import zipfile
        from afc_auth.models import SessionToken

        event = _event(self.admin)
        mine = _event(self.admin, event_name="Other Cup")
        self._upload()
        RegisteredCompetitors.objects.create(event=event, user=self.player, status="registered")
        self._remove()
        admin_token = SessionToken.objects.create(user=self.admin, token="tok_rmadmin_zip").token

        def zip_names(ev):
            resp = Client().post("/events/download-esport-media/", {"event_id": ev.event_id},
                                 content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {admin_token}")
            self.assertEqual(resp.status_code, 200)
            return zipfile.ZipFile(io.BytesIO(resp.content)).namelist()

        # The event the player was in still has the image; an event they were never in has none.
        self.assertTrue(any(n.startswith("esport_images/rmplayer") for n in zip_names(event)))
        self.assertFalse(any(n.startswith("esport_images/") for n in zip_names(mine)))
