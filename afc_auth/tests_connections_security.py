"""
Security tests for the connected-accounts layer.

THESE PIN THREE LIVE PROBLEMS this work fixes, so they cannot come back:

1. A LIVE SESSION TOKEN WAS SENT TO DISCORD. The old connect flow put the player's session token in
   the OAuth `state` parameter (afc_auth/views.py, connect_discord_account) and in the query string
   of the URL the frontend opened (ProfileContent.tsx). It therefore landed in a third-party query
   string, in browser history, and in any Referer header. The code's own comment said it should be
   a short-lived nonce. It is one now.
2. AN OPEN REDIRECT. `return_to` was taken from the query string and redirected to unvalidated,
   which turns an AFC URL into a redirector to any site on the internet.
3. A LAST-CREDENTIAL DISCONNECT COULD LOCK A PLAYER OUT PERMANENTLY. google_auth calls
   set_unusable_password() on every account it creates, so a Google-created account has no password
   until the player sets one.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_auth.tests_connections_security
"""
from django.test import TestCase, override_settings

from afc_auth.connections import state
from afc_auth.connections.links import LastCredentialError, link_account, unlink_account
from afc_auth.connections.redirects import safe_return_to
from afc_auth.models import ConnectedAccount, User


class NonceTests(TestCase):
    def test_a_nonce_is_single_use(self):
        nonce = state.mint(user_id=7, provider="discord", return_to="/profile/connected-apps")
        first = state.consume(nonce)
        self.assertEqual(first["user_id"], 7)
        self.assertIsNone(state.consume(nonce), "a replayed nonce must not resolve")

    def test_an_unknown_nonce_resolves_to_nothing(self):
        self.assertIsNone(state.consume("not-a-real-nonce"))

    def test_an_empty_nonce_resolves_to_nothing(self):
        self.assertIsNone(state.consume(""))
        self.assertIsNone(state.consume(None))

    def test_a_nonce_carries_no_session_token(self):
        nonce = state.mint(user_id=7, provider="discord", return_to="/profile")
        self.assertNotIn("tok_", nonce)
        payload = state.consume(nonce)
        self.assertNotIn("token", payload)


@override_settings(FRONTEND_URL="https://africanfreefirecommunity.com")
class ReturnToAllowlistTests(TestCase):
    """The old connect endpoint redirected to whatever `return_to` said. That is the standard shape
    used to make a phishing link look like it came from AFC."""

    def test_an_internal_path_is_kept(self):
        self.assertEqual(
            safe_return_to("/profile/connected-apps"),
            "https://africanfreefirecommunity.com/profile/connected-apps",
        )

    def test_an_external_host_is_refused(self):
        self.assertEqual(
            safe_return_to("https://evil.example/steal"),
            "https://africanfreefirecommunity.com/profile/connected-apps",
        )

    def test_a_protocol_relative_url_is_refused(self):
        self.assertEqual(
            safe_return_to("//evil.example/steal"),
            "https://africanfreefirecommunity.com/profile/connected-apps",
        )

    def test_a_same_origin_absolute_url_is_kept(self):
        self.assertEqual(
            safe_return_to("https://africanfreefirecommunity.com/profile"),
            "https://africanfreefirecommunity.com/profile",
        )

    def test_empty_falls_back_to_the_default(self):
        self.assertEqual(
            safe_return_to(""),
            "https://africanfreefirecommunity.com/profile/connected-apps",
        )

    def test_a_lookalike_host_is_refused(self):
        """africanfreefirecommunity.com.evil.example ends with the real host as a PREFIX of its own
        label, which a naive `in` or endswith check would wave through."""
        self.assertEqual(
            safe_return_to("https://africanfreefirecommunity.com.evil.example/x"),
            "https://africanfreefirecommunity.com/profile/connected-apps",
        )


class LastCredentialTests(TestCase):
    """google_auth calls set_unusable_password() on every account it creates, so a Google-created
    player has NO password until they set one. Letting them disconnect Google would lock them out of
    AFC permanently, with no self-service way back in."""

    def _sso_user(self, username):
        user = User.objects.create(
            username=username, email=f"{username}@x.com", full_name=username.title(),
            role="player", password="x", country="Nigeria",
        )
        user.set_unusable_password()
        user.save()
        return user

    def test_disconnecting_the_only_credential_is_refused(self):
        user = self._sso_user("onlygoogle")
        link_account(user, "google", {"provider_user_id": "g1", "username": "Only"})
        with self.assertRaises(LastCredentialError):
            unlink_account(user, "google")
        self.assertTrue(ConnectedAccount.objects.filter(user=user, provider="google").exists())

    def test_disconnecting_is_allowed_when_a_second_provider_remains(self):
        user = self._sso_user("twoproviders")
        link_account(user, "google", {"provider_user_id": "g2", "username": "Two"})
        link_account(user, "discord", {"provider_user_id": "d2", "username": "Two"})
        unlink_account(user, "google")
        self.assertFalse(ConnectedAccount.objects.filter(user=user, provider="google").exists())

    def test_disconnecting_is_allowed_when_a_password_exists(self):
        user = User.objects.create(
            username="haspassword", email="hp@x.com", full_name="HP",
            role="player", country="Nigeria",
        )
        user.set_password("a-real-password")
        user.save()
        link_account(user, "google", {"provider_user_id": "g3", "username": "HP"})
        unlink_account(user, "google")
        self.assertFalse(ConnectedAccount.objects.filter(user=user, provider="google").exists())

    def test_relinking_updates_the_existing_row_rather_than_stacking(self):
        user = self._sso_user("relink")
        link_account(user, "discord", {"provider_user_id": "d9", "username": "Old"})
        link_account(user, "discord", {"provider_user_id": "d9", "username": "New"})
        rows = ConnectedAccount.objects.filter(user=user, provider="discord")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().username, "New")

    def test_linking_discord_dual_writes_the_legacy_columns(self):
        """check_discord_membership*, DiscordRoleAssignment, roster_discord.py and the AFC bot all
        read User.discord_id directly. They must keep working untouched."""
        user = self._sso_user("dualwrite")
        link_account(user, "discord", {"provider_user_id": "d77", "username": "Ace"})
        user.refresh_from_db()
        self.assertEqual(user.discord_id, "d77")
        self.assertEqual(user.discord_username, "Ace")
        self.assertTrue(user.discord_connected)

    def test_unlinking_discord_clears_the_legacy_columns(self):
        user = self._sso_user("dualclear")
        user.set_password("a-real-password")
        user.save()
        link_account(user, "discord", {"provider_user_id": "d78", "username": "Ace"})
        unlink_account(user, "discord")
        user.refresh_from_db()
        self.assertIsNone(user.discord_id)
        self.assertFalse(user.discord_connected)


@override_settings(
    DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret",
    DISCORD_REDIRECT_URI="https://api.afc.test/auth/connect-discord/callback/",
    FRONTEND_URL="https://africanfreefirecommunity.com",
)
class LegacyDiscordConnectTests(TestCase):
    """The old endpoint took ?session_token= and put it in the OAuth state sent to discord.com. It
    now takes a Bearer header like every other AFC endpoint, and sends an opaque nonce."""

    def setUp(self):
        from afc_auth.models import SessionToken

        self.user = User.objects.create(
            username="legacyd", email="legacyd@x.com", full_name="Legacy",
            role="player", password="x", country="Nigeria",
        )
        self.token = SessionToken.objects.create(user=self.user, token="tok_legacyd").token

    def test_a_session_token_in_the_query_string_is_no_longer_accepted(self):
        from django.test import Client

        resp = Client().get(f"/auth/connect-discord-account/?session_token={self.token}")
        self.assertIn(resp.status_code, (400, 401))

    def test_the_state_sent_to_discord_does_not_contain_the_session_token(self):
        from django.test import Client

        resp = Client().get(
            "/auth/connect-discord-account/", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("discord.com", resp["Location"])
        self.assertNotIn(self.token, resp["Location"])

    def test_an_external_return_to_is_not_honoured(self):
        from django.test import Client

        resp = Client().get(
            "/auth/connect-discord-account/?return_to=https://evil.example/steal",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp["Location"])

    def test_the_callback_refuses_a_state_it_did_not_mint(self):
        from django.test import Client

        resp = Client().get("/auth/connect-discord/callback/?code=abc&state=made-up")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("discord=failed", resp["Location"])
        self.assertNotIn("evil", resp["Location"])
