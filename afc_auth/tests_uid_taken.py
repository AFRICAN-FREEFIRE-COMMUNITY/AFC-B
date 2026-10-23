"""
Tests for naming the account that already holds a UID (inbox #152, owner 2026-10-05: "for users who
want to input a uid into their accunt, let it also show the user using their UID").

Both doors that take a UID from the person themselves refuse a UID on another account, and now say
whose it is and link that player's public page (which already shows the UID, so nothing private is
revealed): edit_profile (the profile page, onboarding and the tournament UID prompt post here) with
code uid_already_use_user, and signup with code uid_taken. The frontend turns the code into the
sentence in the reader's language (lib/uidTaken.ts).

Run: python manage.py test afc_auth.tests_uid_taken
"""
import json
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User

PASSWORD = "CorrectHorse!9"


class UidTakenNamesTheHolderTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.holder = self._user("NG.KILLA #1", "holder@gmail.com", uid="5566778899")
        self.player = self._user("uid_player", "uidplayer@gmail.com", uid="1234567890")

    def _user(self, username, email, uid=None):
        return User.objects.create(
            username=username, email=email, full_name="Someone", role="player",
            password=make_password(PASSWORD), is_active=True, uid=uid, language="en",
        )

    def _post(self, path, body, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(path, data=json.dumps(body), content_type="application/json", **headers)

    def _session(self, user):
        return SessionToken.objects.create(
            user=user, token=f"tok-{user.pk}-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME,
        ).token

    def test_edit_profile_names_the_holder_and_links_their_page(self):
        resp = self._post("/auth/edit-profile/", {
            "full_name": self.player.full_name, "in_game_name": self.player.username,
            "email": self.player.email, "uid": "5566778899",
        }, token=self._session(self.player))
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["code"], "uid_already_use_user")
        self.assertEqual(body["taken_by"], "NG.KILLA #1")
        self.assertEqual(body["taken_by_page"], "/players/NG.KILLA%20%231")
        self.assertIn("NG.KILLA #1", body["message"])
        self.player.refresh_from_db()
        self.assertEqual(self.player.uid, "1234567890")

    def test_your_own_uid_resent_is_not_a_clash(self):
        resp = self._post("/auth/edit-profile/", {
            "full_name": self.player.full_name, "in_game_name": self.player.username,
            "email": self.player.email, "uid": "1234567890",
        }, token=self._session(self.player))
        self.assertEqual(resp.status_code, 200, resp.content)

    @patch("afc_auth.views.require_human", return_value=None)
    @patch("afc_auth.views.geo_for_ip", return_value={})
    def test_signup_names_the_holder_and_creates_nobody(self, _geo, _human):
        resp = self._post("/auth/signup/", {
            "in_game_name": "new_guy", "full_name": "New Guy", "email": "newguy@gmail.com",
            "password": PASSWORD, "confirm_password": PASSWORD, "uid": "5566778899",
        })
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual((body["code"], body["taken_by"], body["taken_by_page"]),
                         ("uid_taken", "NG.KILLA #1", "/players/NG.KILLA%20%231"))
        self.assertFalse(User.objects.filter(email="newguy@gmail.com").exists())
