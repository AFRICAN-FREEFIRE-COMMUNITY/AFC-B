"""
afc_tournament_and_scrims/test_roster_discord.py - endpoint tests for
POST events/roster-discord-status/ (roster_discord.py).

Covers the three feedback states the registration SPONSOR step renders:
  1. connected + in the AFC server          -> in_server True
  2. Discord not connected                  -> in_server None, no API call made
  3. connected but the Discord API errors   -> in_server None + error noted
plus the auth gates and input validation.

check_discord_membership is mocked with unittest.mock.patch at
afc_tournament_and_scrims.views.check_discord_membership - the endpoint
lazy-imports it from .views at call time, so patching the views attribute is
what the request actually sees. No live Discord calls in tests.

Run: python manage.py test afc_tournament_and_scrims.test_roster_discord
"""
import json
from unittest.mock import patch

from django.test import TestCase, Client

from afc_auth.models import SessionToken, User


def _user(username, **extra):
    """Create a user + session token (same fixture idiom as test_event_links)."""
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role="player", password="x", **extra,
    )
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


URL = "/events/roster-discord-status/"
# Patch target: the name the endpoint's lazy `from .views import ...` resolves.
PATCH_TARGET = "afc_tournament_and_scrims.views.check_discord_membership"


class RosterDiscordStatusTests(TestCase):
    """One caller + a three-player roster covering all three Discord states."""

    def setUp(self):
        self.client = Client()
        # The caller (e.g. the team captain doing the registration).
        self.caller, self.caller_tok = _user("captain")
        # 1. connected, will be IN the server.
        self.in_server_user, _ = _user(
            "alpha", discord_connected=True, discord_id="111",
            discord_username="alpha#1",
        )
        # 2. never connected Discord.
        self.unconnected_user, _ = _user("bravo")
        # 3. connected, but the live Discord check will blow up.
        self.error_user, _ = _user(
            "charlie", discord_connected=True, discord_id="333",
            discord_username="charlie#3",
        )

    def _post(self, user_ids, tok=None):
        return self.client.post(
            URL,
            data=json.dumps({"user_ids": user_ids}),
            content_type="application/json",
            **bearer(tok or self.caller_tok),
        )

    def _result_for(self, body, user_id):
        return next(r for r in body["results"] if r["user_id"] == user_id)

    # ── Case 1: connected + in the server ────────────────────────────────────
    def test_connected_and_in_server(self):
        with patch(PATCH_TARGET, return_value=True) as mock_check:
            res = self._post([self.in_server_user.user_id])
        self.assertEqual(res.status_code, 200)
        r = self._result_for(res.json(), self.in_server_user.user_id)
        self.assertEqual(r["username"], "alpha")
        self.assertTrue(r["discord_connected"])
        self.assertEqual(r["discord_id"], "111")
        self.assertEqual(r["discord_username"], "alpha#1")
        self.assertIs(r["in_server"], True)
        self.assertIsNone(r["error"])
        mock_check.assert_called_once_with("111")

    # ── Case 2: Discord not connected ─────────────────────────────────────────
    def test_not_connected(self):
        with patch(PATCH_TARGET, return_value=True) as mock_check:
            res = self._post([self.unconnected_user.user_id])
        self.assertEqual(res.status_code, 200)
        r = self._result_for(res.json(), self.unconnected_user.user_id)
        self.assertFalse(r["discord_connected"])
        self.assertIsNone(r["discord_id"])
        self.assertIsNone(r["in_server"])  # nothing to check
        self.assertIsNone(r["error"])
        mock_check.assert_not_called()  # no pointless Discord API call

    # ── Case 3: Discord API error -> in_server null + error noted ────────────
    def test_discord_api_error_returns_null_in_server(self):
        with patch(PATCH_TARGET, side_effect=Exception("discord down")):
            res = self._post([self.error_user.user_id])
        self.assertEqual(res.status_code, 200)
        r = self._result_for(res.json(), self.error_user.user_id)
        self.assertTrue(r["discord_connected"])
        self.assertIsNone(r["in_server"])
        self.assertIn("discord down", r["error"])

    # ── Mixed roster: all three states in one call, order preserved ──────────
    def test_mixed_roster_one_call(self):
        def fake_check(discord_id):
            if discord_id == "111":
                return True
            raise Exception("boom")

        ids = [
            self.in_server_user.user_id,
            self.unconnected_user.user_id,
            self.error_user.user_id,
        ]
        with patch(PATCH_TARGET, side_effect=fake_check):
            res = self._post(ids)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual([r["user_id"] for r in body["results"]], ids)
        self.assertIs(self._result_for(body, ids[0])["in_server"], True)
        self.assertIsNone(self._result_for(body, ids[1])["in_server"])
        self.assertIsNone(self._result_for(body, ids[2])["in_server"])

    # ── Connected but NOT in the server (helper returns False) ───────────────
    def test_connected_not_in_server(self):
        with patch(PATCH_TARGET, return_value=False):
            res = self._post([self.in_server_user.user_id])
        r = self._result_for(res.json(), self.in_server_user.user_id)
        self.assertTrue(r["discord_connected"])
        self.assertIs(r["in_server"], False)
        self.assertIsNone(r["error"])

    # ── Unknown user id: reported per-row, doesn't fail the roster ───────────
    def test_unknown_user_id_reported_inline(self):
        with patch(PATCH_TARGET, return_value=True):
            res = self._post([999999, self.in_server_user.user_id])
        self.assertEqual(res.status_code, 200)
        body = res.json()
        missing = self._result_for(body, 999999)
        self.assertIsNone(missing["username"])
        self.assertIsNone(missing["in_server"])
        self.assertEqual(missing["error"], "User not found.")
        # The real user is still answered normally in the same response.
        self.assertIs(
            self._result_for(body, self.in_server_user.user_id)["in_server"], True
        )

    # ── Auth + input gates ────────────────────────────────────────────────────
    def test_missing_token_400(self):
        res = self.client.post(
            URL, data=json.dumps({"user_ids": [1]}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_bad_token_401(self):
        res = self._post([1], tok="not_a_real_token")
        self.assertEqual(res.status_code, 401)

    def test_user_ids_required(self):
        for bad in ({}, {"user_ids": []}, {"user_ids": "1,2"}):
            res = self.client.post(
                URL, data=json.dumps(bad), content_type="application/json",
                **bearer(self.caller_tok),
            )
            self.assertEqual(res.status_code, 400, bad)

    def test_non_numeric_user_id_400(self):
        res = self._post(["abc"])
        self.assertEqual(res.status_code, 400)

    def test_roster_cap_enforced(self):
        res = self._post(list(range(1, 23)))  # 22 ids > MAX_ROSTER_IDS (20)
        self.assertEqual(res.status_code, 400)
