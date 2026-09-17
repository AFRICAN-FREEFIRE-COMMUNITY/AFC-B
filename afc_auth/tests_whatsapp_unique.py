"""
afc_auth.tests_whatsapp_unique - one WhatsApp number, one account (owner 2026-09-14, inbox #21).

"Hope that two users cannot use/have the same whatsapp number, just like emails and usernames."
The number lives on UserProfile as a blank-by-default column that MySQL cannot make partially
unique, so the rule is afc_auth.identifiers.whatsapp_number_holder, applied at the three doors
that write the field. Each test here holds one door, plus the helper's own edges.

HOW IT CONNECTS
    Drives signup (afc_auth.views.signup), edit_profile and admin_set_user_whatsapp
    (afc_auth.views_admin_identity) through the test client, with the same fixtures the
    recovery and admin-identity suites use.
"""
import json
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.identifiers import WHATSAPP_TAKEN_CODE, whatsapp_number_holder
from afc_auth.models import Roles, SessionToken, User, UserProfile, UserRoles

PASSWORD = "CorrectHorse!9"
NUMBER = "+2348051234567"


def _user(username, *, whatsapp="", granular=(), status="active"):
    user = User.objects.create(username=username, email=f"{username}@gmail.com",
                               full_name=username.title(), password=make_password(PASSWORD),
                               is_active=True, country="Nigeria", status=status, language="en")
    UserProfile.objects.create(user=user, whatsapp_number=whatsapp)
    for name in granular:
        role, _ = Roles.objects.get_or_create(role_name=name, defaults={"description": name})
        UserRoles.objects.create(user=user, role=role)
    return user


def _session(user):
    return SessionToken.objects.create(
        user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
        expires_at=timezone.now() + SessionToken.SESSION_LIFETIME,
    ).token


class WhatsappUniqueTests(TestCase):
    def setUp(self):
        self.client = Client()
        for dotted in ("afc_auth.views.send_email", "afc_auth.views_admin_identity.send_email"):
            p = patch(dotted, return_value=True)
            p.start()
            self.addCleanup(p.stop)
        geo = patch("afc_auth.views.geo_for_ip", return_value={"country": "Nigeria"})
        geo.start()
        self.addCleanup(geo.stop)
        self.holder = _user("holder", whatsapp=NUMBER)

    # ── the helper ──
    def test_the_helper_finds_the_holder_and_ignores_blank_self_and_deleted(self):
        self.assertEqual(whatsapp_number_holder(NUMBER).pk, self.holder.pk)
        self.assertIsNone(whatsapp_number_holder(NUMBER, exclude_pk=self.holder.pk))
        self.assertIsNone(whatsapp_number_holder(""))
        self.assertIsNone(whatsapp_number_holder(None))
        User.objects.filter(pk=self.holder.pk).update(status="deleted")
        self.assertIsNone(whatsapp_number_holder(NUMBER), "a deleted account holds nothing")

    # ── door 1: signup ──
    def _signup(self, **extra):
        body = {"in_game_name": "newplayer", "email": "newplayer@gmail.com", "password": PASSWORD,
                "confirm_password": PASSWORD, "full_name": "New Player"}
        body.update(extra)
        return self.client.post("/auth/signup/", body, content_type="application/json")

    def test_signup_refuses_a_number_another_account_holds(self):
        r = self._signup(whatsapp_number="+234 805 123 4567")   # normalises to NUMBER
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["code"], WHATSAPP_TAKEN_CODE)
        self.assertNotIn("holder", r.json()["message"])          # never names the holder
        self.assertFalse(User.objects.filter(username="newplayer").exists())

    def test_signup_with_a_free_number_still_works(self):
        r = self._signup(whatsapp_number="+2348099900022")
        self.assertEqual(r.status_code, 201, r.content)

    # ── door 2: the person's own profile ──
    def _edit_profile(self, user, number):
        return self.client.post(
            "/auth/edit-profile/",
            {"full_name": user.full_name, "in_game_name": user.username, "email": user.email,
             "whatsapp_number": number},
            HTTP_AUTHORIZATION=f"Bearer {_session(user)}",
        )

    def test_profile_edit_refuses_a_number_another_account_holds_and_keeps_its_own(self):
        editor = _user("editor", whatsapp="+2348077700033")
        r = self._edit_profile(editor, NUMBER)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["code"], WHATSAPP_TAKEN_CODE)
        self.assertEqual(UserProfile.objects.get(user=editor).whatsapp_number, "+2348077700033")

    def test_profile_edit_may_resave_its_own_number(self):
        r = self._edit_profile(self.holder, "+234 805 123 4567")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(UserProfile.objects.get(user=self.holder).whatsapp_number, NUMBER)

    # ── door 3: an admin setting it on somebody's account ──
    def test_admin_is_refused_and_told_whose_it_is(self):
        head = _user("head_boss", granular=["head_admin"])
        head.role = "admin"
        head.save()
        target = _user("victim")
        r = self.client.post(
            "/auth/admin/set-user-whatsapp/",
            data=json.dumps({"user_id": target.user_id, "whatsapp_number": NUMBER,
                             "reason": "Support ticket 412"}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_session(head)}",
        )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["code"], WHATSAPP_TAKEN_CODE)
        self.assertIn("holder", r.json()["message"])     # admins ARE told where to go
        self.assertEqual(r.json()["holder_user_id"], self.holder.user_id)
        self.assertEqual(UserProfile.objects.get(user=target).whatsapp_number, "")
