"""Success answers carry a stable `code` beside the English sentence (inbox #257, 2026-10-16).

The site showed these server sentences in English to people reading it in French or Portuguese
("Login successful", "Team successfully registered (squad). Discord roles queued.", seen in the
fr/pt how-to clips). The frontend now prints its own en/fr/pt text for the code
(AFC_Frontend lib/i18n/serverMessage.ts, messages/<locale>/serverMessages.json) and falls back to
`message` for a code it does not know, so the English sentence must stay exactly as it was.

Run: .venv\\Scripts\\python.exe manage.py test afc_auth.tests_success_codes
"""
import json

from django.test import TestCase

from afc_auth.models import User
from afc_tournament_and_scrims.tests import TeamRegistrationBanTests


class LoginSuccessCodeTests(TestCase):
    def test_login_answers_login_ok_and_keeps_the_sentence(self):
        User.objects.create_user(username="codeuser", email="codeuser@example.com", password="Secret-123",
                                 role="player", status="active")
        res = self.client.post("/auth/login/", data=json.dumps({"ign_or_uid": "codeuser", "password": "Secret-123"}),
                               content_type="application/json")
        self.assertEqual(res.status_code, 200, res.content[:300])
        self.assertEqual(res.json()["code"], "login_ok")
        self.assertEqual(res.json()["message"], "Login successful")


class RegistrationSuccessCodeTests(TestCase):
    """Borrows TeamRegistrationBanTests' fixture (a clean squad, an open free event) without
    inheriting its tests, so they do not run twice."""

    setUp = TeamRegistrationBanTests.setUp
    _token_for = TeamRegistrationBanTests._token_for
    _auth = TeamRegistrationBanTests._auth
    _register = TeamRegistrationBanTests._register

    def test_team_registration_answers_registration_ok_team(self):
        res = self._register()
        self.assertEqual(res.status_code, 201, res.content[:300])
        self.assertEqual(res.json()["code"], "registration_ok_team")
        self.assertTrue(res.json()["message"].startswith("Team successfully registered"))
