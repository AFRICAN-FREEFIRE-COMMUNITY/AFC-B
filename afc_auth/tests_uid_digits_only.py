"""
Tests for DIGITS-ONLY UIDs (owner 2026-09-30, inbox #89) - afc_auth/identifiers.uid_format_error and
every door that writes a Free Fire UID.

Owner: "when users are inputting UIDs only numbers should be allowed, no special characters, full
stops, commas, alpahbets etc, only numbers are allowed."

WHAT IS COVERED, AND WHY EACH ONE IS HERE

  THE RULE ITSELF
    - Plain digits pass, up to the 15 the column holds.
    - Every shape the owner listed is refused with the same code: letters, full stops, commas,
      spaces, signs, and the spreadsheet damage the admin tool was built to clean (".4646454948",
      "527.0848242").
    - Unicode digits ("²", Arabic-Indic) are refused: str.isdigit() says True for them, which is why
      the rule compares against ASCII 0-9 instead.

  EDIT PROFILE (profile page, onboarding, the tournament UID prompt, member self-edit all post here)
    - A changed UID with a non-digit is refused and NOT written.
    - A clean new UID is written.
    - A legacy dirty UID that is merely resent unchanged does not block the save: the page resends
      the stored value on every save, and refusing it would stop a player changing their picture.

  SIGNUP and CREATE SPONSOR ACCOUNT
    - A non-digit UID is refused before any row is created.

The broadcast kit's caster UID calls the same function one line after reading it; its gate needs a
whole event fixture, so it is covered by the rule tests above rather than a request here.

Run: python manage.py test afc_auth.tests_uid_digits_only
"""
import json
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone

from afc_auth.identifiers import (
    UID_MAX_LENGTH, UID_NOT_DIGITS_CODE, UID_TOO_LONG_CODE, uid_format_error,
)
from afc_auth.models import Roles, SessionToken, User, UserRoles

PASSWORD = "CorrectHorse!9"


class UidFormatRuleTests(SimpleTestCase):
    """The pure rule, no database."""

    def test_plain_digits_pass(self):
        for good in ("1", "1234567890", "9" * UID_MAX_LENGTH):
            self.assertEqual(uid_format_error(good), (None, None), good)

    def test_empty_is_left_to_the_caller(self):
        self.assertEqual(uid_format_error(""), (None, None))
        self.assertEqual(uid_format_error(None), (None, None))

    def test_every_non_digit_shape_is_refused(self):
        for bad in ("12345abc", "ABCDEF", "123.456", "123,456", "123 456", "-668075761",
                    "+123456", "1e10", ".4646454948", "527.0848242", "7353194371.0", "123#",
                    "²³", "١٢٣"):
            message, code = uid_format_error(bad)
            self.assertEqual(code, UID_NOT_DIGITS_CODE, bad)
            self.assertTrue(message)

    def test_over_length_is_refused(self):
        self.assertEqual(uid_format_error("1" * (UID_MAX_LENGTH + 1))[1], UID_TOO_LONG_CODE)


class UidWritePathTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.player = self._user("uid_player", "uidplayer@gmail.com", uid="1234567890")

    def _user(self, username, email, role="player", granular=(), uid=None):
        user = User.objects.create(
            username=username, email=email, full_name=username.title(), role=role,
            password=make_password(PASSWORD), is_active=True, uid=uid, language="en",
        )
        for name in granular:
            role_row, _ = Roles.objects.get_or_create(role_name=name, defaults={"description": name})
            UserRoles.objects.create(user=user, role=role_row)
        return user

    def _session(self, user):
        """Mint a SessionToken directly (the project rule: never type a password to get a session)."""
        return SessionToken.objects.create(
            user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME,
        ).token

    def _post(self, path, body, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(path, data=json.dumps(body), content_type="application/json",
                                **headers)

    def _edit_profile(self, uid):
        return self._post("/auth/edit-profile/", {
            "full_name": self.player.full_name, "in_game_name": self.player.username,
            "email": self.player.email, "uid": uid,
        }, token=self._session(self.player))

    # ── edit profile ─────────────────────────────────────────────────────────────────────────
    def test_edit_profile_refuses_a_non_digit_uid_and_writes_nothing(self):
        for bad in ("98765abc", "987.654", "987,654", "987 654"):
            resp = self._edit_profile(bad)
            self.assertEqual(resp.status_code, 400, bad)
            self.assertEqual(resp.json()["code"], UID_NOT_DIGITS_CODE, bad)
        self.player.refresh_from_db()
        self.assertEqual(self.player.uid, "1234567890")

    def test_edit_profile_writes_a_clean_uid(self):
        resp = self._edit_profile("5566778899")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.player.refresh_from_db()
        self.assertEqual(self.player.uid, "5566778899")

    def test_a_legacy_dirty_uid_resent_unchanged_does_not_block_the_save(self):
        User.objects.filter(pk=self.player.pk).update(uid="527.0848242")
        self.player.refresh_from_db()
        resp = self._edit_profile("527.0848242")
        self.assertEqual(resp.status_code, 200, resp.content)

    # ── signup ───────────────────────────────────────────────────────────────────────────────
    # The bot check runs first and is stubbed at the name signup looks up (it has its own tests).
    @patch("afc_auth.views.require_human", return_value=None)
    @patch("afc_auth.views.geo_for_ip", return_value={})
    def test_signup_refuses_a_non_digit_uid_before_creating_anyone(self, _geo, _human):
        resp = self._post("/auth/signup/", {
            "in_game_name": "new_guy", "full_name": "New Guy", "email": "newguy@gmail.com",
            "password": PASSWORD, "confirm_password": PASSWORD, "uid": "12.34",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], UID_NOT_DIGITS_CODE)
        self.assertFalse(User.objects.filter(email="newguy@gmail.com").exists())

    # ── create sponsor account ───────────────────────────────────────────────────────────────
    def test_create_sponsor_account_refuses_a_non_digit_uid(self):
        head = self._user("head_boss", "head@gmail.com", role="admin", granular=["head_admin"])
        Roles.objects.get_or_create(role_name="sponsor_admin", defaults={"description": "s"})
        resp = self._post("/events/create-sponsor-account/", {
            "fullname": "Sponsor Co", "email": "sponsor@gmail.com", "username": "sponsorco",
            "uid": "SPONSOR1", "password": PASSWORD, "confirm_password": PASSWORD,
        }, token=self._session(head))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], UID_NOT_DIGITS_CODE)
        self.assertFalse(User.objects.filter(username="sponsorco").exists())
