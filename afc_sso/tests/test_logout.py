"""RP-initiated logout, and the one thing it must never do.

AFC enables OIDC_RP_INITIATED_LOGOUT_ENABLED so a partner can offer "sign out of AFC
too". The library's own view (site-packages/oauth2_provider/views/oidc.py do_logout)
deletes EVERY token the player holds, filtered on user + client_type + grant_type and
never on the application that asked. Every AFC partner is a confidential
authorization-code client, so that filter matches all of them: partner A calling
/sso/logout/ would disconnect the player from partner B as well.

test_logout_does_not_disconnect_other_partners is the reason afc_sso/views.py
AFCRPInitiatedLogoutView exists. If somebody deletes that override, that test fails.
"""
import base64
import hashlib
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken
from oauth2_provider.models import (
    Grant,
    get_access_token_model,
    get_application_model,
    get_id_token_model,
    get_refresh_token_model,
)

Application = get_application_model()
AccessToken = get_access_token_model()
RefreshToken = get_refresh_token_model()
IDToken = get_id_token_model()
User = get_user_model()

LOGOUT_URL = "/sso/logout/"

VERIFIER = "c" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()
).decode().rstrip("=")


class RPInitiatedLogoutTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.player = User.objects.create_user(
            username="logoutplayer", email="logout@afc.test", password="x")
        # AFC players have no Django session: they hold an `auth_token` cookie backed by a
        # SessionToken row, which SSOSessionTokenMiddleware resolves on every /sso/ path.
        # force_login would be silently discarded by that middleware, so signing a player
        # in for these tests means setting the cookie, exactly as test_consent.py does.
        SessionToken.objects.create(user=self.player, token="tok-logout")

        self.partner_a = self._application("Partner A", "https://a.test/cb", "https://a.test/bye")
        self.partner_b = self._application("Partner B", "https://b.test/cb", "https://b.test/bye")

    def _application(self, name, redirect_uri, post_logout_uri):
        return Application.objects.create(
            name=name,
            display_name=name,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            algorithm=Application.RS256_ALGORITHM,
            redirect_uris=redirect_uri,
            post_logout_redirect_uris=post_logout_uri,
            client_secret=f"{name}-secret",
        )

    def _sign_in(self, application, redirect_uri, code):
        """Run the real code exchange so the tokens under test are the ones AFC issues,
        including a genuine signed ID token to use as the id_token_hint."""
        Grant.objects.create(
            user=self.player, code=code, application=application,
            expires=timezone.now() + timezone.timedelta(minutes=5),
            redirect_uri=redirect_uri, scope="openid",
            code_challenge=CHALLENGE, code_challenge_method="S256", nonce="n",
        )
        resp = self.client.post("/sso/token/", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": application.client_id,
            "client_secret": f"{application.name}-secret",
            "code_verifier": VERIFIER,
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        return json.loads(resp.content)

    def _token_counts(self, application):
        return (
            AccessToken.objects.filter(user=self.player, application=application).count(),
            RefreshToken.objects.filter(user=self.player, application=application).count(),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # It is switched on at all
    # ──────────────────────────────────────────────────────────────────────────
    def test_discovery_now_advertises_the_end_session_endpoint(self):
        """A partner drives its integration from discovery, so enabling logout is only
        real once the document says so."""
        doc = self.client.get("/sso/.well-known/openid-configuration").json()
        self.assertIn("end_session_endpoint", doc)
        self.assertTrue(doc["end_session_endpoint"].endswith("/sso/logout/"))

    def test_the_endpoint_no_longer_404s(self):
        resp = self.client.get(LOGOUT_URL)
        self.assertNotEqual(resp.status_code, 404)

    # ──────────────────────────────────────────────────────────────────────────
    # It works with a valid id_token_hint
    # ──────────────────────────────────────────────────────────────────────────
    def test_logout_with_a_valid_hint_disconnects_that_partner(self):
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-1")
        self.assertEqual(self._token_counts(self.partner_a), (1, 1))

        resp = self.client.get(LOGOUT_URL, {
            "id_token_hint": tokens["id_token"],
            "post_logout_redirect_uri": "https://a.test/bye",
            "state": "xyz",
        })

        # No AFC browser session here (the partner's server made the call), so there is
        # nobody to prompt and the logout completes straight away.
        self.assertEqual(resp.status_code, 302)
        self.assertIn("https://a.test/bye", resp["Location"])
        self.assertIn("state=xyz", resp["Location"])
        self.assertEqual(self._token_counts(self.partner_a), (0, 0))

    def test_logout_removes_the_id_tokens_too(self):
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-idt")
        self.assertTrue(IDToken.objects.filter(user=self.player).exists())

        self.client.get(LOGOUT_URL, {"id_token_hint": tokens["id_token"]})

        self.assertFalse(
            IDToken.objects.filter(user=self.player, application=self.partner_a).exists())

    # ──────────────────────────────────────────────────────────────────────────
    # THE SECURITY PROPERTY
    # ──────────────────────────────────────────────────────────────────────────
    def test_logout_does_not_disconnect_other_partners(self):
        """Partner A signing a player out must not cut partner B off.

        This is what AFCRPInitiatedLogoutView is for. The library's do_logout deletes
        every token the user holds regardless of which application asked, so without the
        override this fails and one partner can disconnect all the others.
        """
        tokens_a = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-2")
        self._sign_in(self.partner_b, "https://b.test/cb", "code-b-2")
        self.assertEqual(self._token_counts(self.partner_b), (1, 1))

        self.client.get(LOGOUT_URL, {"id_token_hint": tokens_a["id_token"]})

        self.assertEqual(self._token_counts(self.partner_a), (0, 0))
        self.assertEqual(self._token_counts(self.partner_b), (1, 1))

    # ──────────────────────────────────────────────────────────────────────────
    # It refuses what it should refuse
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_garbage_id_token_hint_is_refused_and_deletes_nothing(self):
        self._sign_in(self.partner_a, "https://a.test/cb", "code-a-3")

        resp = self.client.get(LOGOUT_URL, {"id_token_hint": "not-a-jwt"})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._token_counts(self.partner_a), (1, 1))

    def test_a_hint_that_does_not_match_the_client_id_is_refused(self):
        """Sending partner B's client_id with partner A's ID token is either a mistake or
        an attempt to have one partner act as another."""
        tokens_a = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-4")

        resp = self.client.get(LOGOUT_URL, {
            "id_token_hint": tokens_a["id_token"],
            "client_id": self.partner_b.client_id,
        })

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._token_counts(self.partner_a), (1, 1))

    def test_an_unregistered_post_logout_redirect_uri_is_refused(self):
        """Redirecting to whatever URL is in the query string would make AFC an open
        redirector, exactly as the four authorize refusals avoid."""
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-5")

        resp = self.client.get(LOGOUT_URL, {
            "id_token_hint": tokens["id_token"],
            "post_logout_redirect_uri": "https://attacker.example/phish",
        })

        self.assertEqual(resp.status_code, 400)
        self.assertNotEqual(resp.status_code, 302)
        self.assertEqual(self._token_counts(self.partner_a), (1, 1))

    def test_without_a_hint_or_a_session_nothing_is_deleted(self):
        """No id_token_hint and no client_id means AFC cannot tell which partner is
        asking, so there is no partner to disconnect. It must not guess."""
        self._sign_in(self.partner_a, "https://a.test/cb", "code-a-6")

        resp = self.client.get(LOGOUT_URL)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._token_counts(self.partner_a), (1, 1))

    # ──────────────────────────────────────────────────────────────────────────
    # The player is asked before their AFC session ends
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_signed_in_player_is_shown_the_confirm_page_first(self):
        """OIDC_RP_INITIATED_LOGOUT_ALWAYS_PROMPT: a partner must not be able to end an
        AFC session silently, for instance from a hidden iframe. The page has to render,
        and nothing may be deleted until the player agrees."""
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-7")
        self.client.cookies["auth_token"] = "tok-logout"

        resp = self.client.get(LOGOUT_URL, {"id_token_hint": tokens["id_token"]})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Sign out of AFC", resp.content)
        self.assertEqual(self._token_counts(self.partner_a), (1, 1))

    def test_confirming_on_that_page_completes_the_logout(self):
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-8")
        self.client.cookies["auth_token"] = "tok-logout"

        resp = self.client.post(LOGOUT_URL, {
            "id_token_hint": tokens["id_token"],
            "post_logout_redirect_uri": "https://a.test/bye",
            "state": "",
            "client_id": "",
            "allow": "Logout",
        })

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._token_counts(self.partner_a), (0, 0))

    # ──────────────────────────────────────────────────────────────────────────
    # Ending the AFC session, which is a SessionToken and not a Django session
    # ──────────────────────────────────────────────────────────────────────────
    def test_confirming_ends_the_afc_session_on_this_device(self):
        """django.contrib.auth.logout clears a Django session, and an AFC player has
        none. Without AFC ending the SessionToken the player would stay signed in to AFC
        while the partner had been told they were signed out."""
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-9")
        self.client.cookies["auth_token"] = "tok-logout"

        self.client.post(LOGOUT_URL, {
            "id_token_hint": tokens["id_token"],
            "post_logout_redirect_uri": "",
            "state": "",
            "client_id": "",
            "allow": "Logout",
        })

        self.assertFalse(SessionToken.objects.filter(token="tok-logout").exists())

    def test_only_this_device_is_signed_out(self):
        """The button says "on this device", so the player's other sessions survive."""
        SessionToken.objects.create(user=self.player, token="tok-logout-phone")
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-10")
        self.client.cookies["auth_token"] = "tok-logout"

        self.client.post(LOGOUT_URL, {
            "id_token_hint": tokens["id_token"],
            "post_logout_redirect_uri": "",
            "state": "",
            "client_id": "",
            "allow": "Logout",
        })

        self.assertTrue(SessionToken.objects.filter(token="tok-logout-phone").exists())

    def test_a_server_side_call_cannot_end_the_players_afc_session(self):
        """A partner's server can reach the logout endpoint with nothing but an
        id_token_hint and no browser present. That may disconnect THAT partner, but it
        must not sign the player out of AFC: otherwise every partner holding a token
        would have a remote sign-out button for that player."""
        tokens = self._sign_in(self.partner_a, "https://a.test/cb", "code-a-11")

        resp = self.client.get(LOGOUT_URL, {"id_token_hint": tokens["id_token"]})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._token_counts(self.partner_a), (0, 0))
        self.assertTrue(SessionToken.objects.filter(token="tok-logout").exists())
