"""
Endpoint tests for connected accounts.

AUTH NOTE, and the reason these live under /auth/ rather than /sso/: the existing connected-apps
endpoints sit under /sso/ and had to carry @authentication_classes([]) because
SSOSessionTokenMiddleware sets request.user for every /sso/ path from the auth_token cookie, which
makes DRF's SessionAuthentication run a CSRF check that 403s a DELETE from any browser holding that
cookie. It is invisible to the ordinary test client, which sets _dont_enforce_csrf_checks. Routing
these under /auth/ avoids that middleware entirely, and the CSRF-enforcing client below pins it, so
a future move back to /sso/ fails here instead of in production.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_auth.tests_connections_api
"""
from django.test import Client, TestCase, override_settings

from afc_auth.models import ConnectedAccount, SessionToken, User


def _player(username="connplayer"):
    user = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role="player", country="Nigeria",
    )
    user.set_password("a-real-password")
    user.save()
    token = SessionToken.objects.create(user=user, token=f"tok_{username}")
    return user, token.token


@override_settings(
    DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret",
    GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="",
    FRONTEND_URL="https://africanfreefirecommunity.com",
)
class ListConnectionsTests(TestCase):
    def setUp(self):
        self.user, self.token = _player()

    def _get(self):
        return Client().get("/auth/connections/", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_requires_a_session_token(self):
        self.assertEqual(Client().get("/auth/connections/").status_code, 400)

    def test_an_invalid_token_is_401(self):
        resp = Client().get("/auth/connections/", HTTP_AUTHORIZATION="Bearer nonsense")
        self.assertEqual(resp.status_code, 401)

    def test_lists_enabled_providers_only(self):
        slugs = [row["provider"] for row in self._get().json()["connections"]]
        self.assertIn("discord", slugs)
        self.assertIn("google", slugs)
        self.assertNotIn("vent", slugs, "an unconfigured provider must be invisible")

    def test_a_linked_provider_reports_connected_with_its_username(self):
        ConnectedAccount.objects.create(
            user=self.user, provider="discord", provider_user_id="777", username="ace",
        )
        row = next(r for r in self._get().json()["connections"] if r["provider"] == "discord")
        self.assertTrue(row["connected"])
        self.assertEqual(row["username"], "ace")
        self.assertTrue(row["can_disconnect"], "this user has a usable password")


@override_settings(
    DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret",
    GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="",
)
class ProviderListTests(TestCase):
    """The event-requirement picker reads this. It must offer exactly the providers a player could
    actually connect, or an organizer could require something nobody can satisfy."""

    def setUp(self):
        self.user, self.token = _player("pickerplayer")

    def test_lists_only_configured_providers(self):
        resp = Client().get(
            "/auth/connections/providers/", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            sorted(p["slug"] for p in resp.json()["providers"]), ["discord", "google"]
        )

    def test_requires_authentication(self):
        self.assertEqual(Client().get("/auth/connections/providers/").status_code, 400)


@override_settings(
    DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret",
    FRONTEND_URL="https://africanfreefirecommunity.com",
)
class DisconnectTests(TestCase):
    def setUp(self):
        self.user, self.token = _player("discplayer")
        ConnectedAccount.objects.create(
            user=self.user, provider="discord", provider_user_id="777", username="ace",
        )

    def _delete(self, client=None):
        return (client or Client()).delete(
            "/auth/connections/discord/", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_disconnect_removes_the_link(self):
        resp = self._delete()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(ConnectedAccount.objects.filter(user=self.user).exists())

    def test_disconnect_survives_a_csrf_enforcing_browser(self):
        """The trap the /sso/ endpoints hit. The ordinary test client hides it."""
        resp = self._delete(Client(enforce_csrf_checks=True))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_disconnecting_the_last_credential_is_refused_with_a_useful_code(self):
        self.user.set_unusable_password()
        self.user.save()
        resp = self._delete()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "last_credential")

    def test_disconnecting_something_not_connected_is_idempotent(self):
        self._delete()
        self.assertEqual(
            self._delete().status_code, 200, "a double tap on a phone must not error"
        )

    def test_an_unknown_provider_is_404(self):
        resp = Client().delete(
            "/auth/connections/myspace/", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(
    DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret",
    VENT_CLIENT_ID="", VENT_CLIENT_SECRET="",
    FRONTEND_URL="https://africanfreefirecommunity.com",
    AFC_API_BASE_URL="https://api.afc.test",
)
class StartConnectionTests(TestCase):
    def setUp(self):
        self.user, self.token = _player("startplayer")

    def test_start_redirects_to_the_provider_without_the_session_token(self):
        resp = Client().get(
            "/auth/connections/discord/start/", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("discord.com", resp["Location"])
        self.assertNotIn(self.token, resp["Location"])

    def test_start_on_a_disabled_provider_is_a_404(self):
        resp = Client().get(
            "/auth/connections/vent/start/", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 404)

    def test_start_on_an_id_token_provider_is_refused(self):
        """Google links in place from a credential; there is no consent screen to redirect to."""
        with override_settings(GOOGLE_OAUTH_CLIENT_ID="gid"):
            resp = Client().get(
                "/auth/connections/google/start/", HTTP_AUTHORIZATION=f"Bearer {self.token}",
            )
        self.assertEqual(resp.status_code, 400)


@override_settings(
    DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret",
    FRONTEND_URL="https://africanfreefirecommunity.com",
    AFC_API_BASE_URL="https://api.afc.test",
)
class FinishConnectionTests(TestCase):
    """The callback arrives from the PROVIDER, so it carries no Authorization header. Everything it
    is trusted with comes from the single-use nonce."""

    def setUp(self):
        self.user, self.token = _player("cbplayer")

    def _nonce(self, provider="discord", return_to="/profile/connected-apps"):
        from afc_auth.connections import state

        return state.mint(
            user_id=self.user.user_id, provider=provider,
            return_to=f"https://africanfreefirecommunity.com{return_to}", code_verifier="v",
        )

    def test_a_missing_nonce_lands_on_the_page_with_an_expired_flag(self):
        resp = Client().get("/auth/connections/discord/callback/?code=abc&state=nope")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("connect_error=expired", resp["Location"])

    def test_a_nonce_minted_for_another_provider_is_refused(self):
        nonce = self._nonce(provider="google")
        resp = Client().get(f"/auth/connections/discord/callback/?code=abc&state={nonce}")
        self.assertIn("connect_error=expired", resp["Location"])

    def test_a_cancelled_consent_screen_says_cancelled(self):
        nonce = self._nonce()
        resp = Client().get(
            f"/auth/connections/discord/callback/?error=access_denied&state={nonce}"
        )
        self.assertIn("connect_error=cancelled", resp["Location"])

    def test_a_successful_callback_links_the_account(self):
        from unittest.mock import patch

        nonce = self._nonce()
        with patch("afc_auth.connections.oauth.exchange_code", return_value={"access_token": "at"}), \
             patch(
                 "afc_auth.connections.oauth.fetch_profile",
                 return_value={"id": "d900", "username": "ace", "avatar": "hash"},
             ):
            resp = Client().get(f"/auth/connections/discord/callback/?code=abc&state={nonce}")
        self.assertIn("connected=discord", resp["Location"])
        row = ConnectedAccount.objects.get(user=self.user, provider="discord")
        self.assertEqual(row.provider_user_id, "d900")

    def test_an_account_already_linked_elsewhere_is_refused_by_name(self):
        from unittest.mock import patch

        other, _ = _player("cbother")
        ConnectedAccount.objects.create(
            user=other, provider="discord", provider_user_id="d901", username="taken",
        )
        nonce = self._nonce()
        with patch("afc_auth.connections.oauth.exchange_code", return_value={"access_token": "at"}), \
             patch(
                 "afc_auth.connections.oauth.fetch_profile",
                 return_value={"id": "d901", "username": "taken", "avatar": ""},
             ):
            resp = Client().get(f"/auth/connections/discord/callback/?code=abc&state={nonce}")
        self.assertIn("connect_error=already_linked", resp["Location"])
        self.assertFalse(ConnectedAccount.objects.filter(user=self.user).exists())

    def test_a_provider_failure_does_not_500(self):
        from unittest.mock import patch

        from afc_auth.connections import oauth

        nonce = self._nonce()
        with patch("afc_auth.connections.oauth.exchange_code", side_effect=oauth.OAuthError("no")):
            resp = Client().get(f"/auth/connections/discord/callback/?code=abc&state={nonce}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("connect_error=provider", resp["Location"])

    def test_the_nonce_cannot_be_replayed(self):
        from unittest.mock import patch

        nonce = self._nonce()
        with patch("afc_auth.connections.oauth.exchange_code", return_value={"access_token": "at"}), \
             patch(
                 "afc_auth.connections.oauth.fetch_profile",
                 return_value={"id": "d902", "username": "ace", "avatar": ""},
             ):
            Client().get(f"/auth/connections/discord/callback/?code=abc&state={nonce}")
            resp = Client().get(f"/auth/connections/discord/callback/?code=abc&state={nonce}")
        self.assertIn("connect_error=expired", resp["Location"])
