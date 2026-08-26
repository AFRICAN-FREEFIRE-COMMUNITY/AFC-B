"""
Tests for the generic authorization-code client shared by every redirect-style provider.

Mocked at the SERVICE BOUNDARY (requests.post / requests.get), never at our own internals, so the
tests prove what we send and how we read the reply without touching a live provider.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_auth.tests_connections_oauth
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from afc_auth.connections import get_provider, oauth


@override_settings(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret")
class AuthorizeUrlTests(TestCase):
    def test_the_url_carries_the_nonce_and_a_pkce_challenge_and_no_session_token(self):
        provider = get_provider("discord")
        verifier = oauth.make_code_verifier()
        url = oauth.authorize_url(
            provider, nonce="NONCE123", code_verifier=verifier,
            redirect_uri="https://api.afc.test/auth/connections/discord/callback/",
        )
        self.assertIn("state=NONCE123", url)
        self.assertIn("code_challenge=", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("client_id=cid", url)
        self.assertNotIn("session_token", url)
        self.assertNotIn(verifier, url, "the VERIFIER must never leave the server, only its hash")

    def test_the_challenge_is_the_sha256_of_the_verifier(self):
        verifier = "a-known-verifier"
        import base64
        import hashlib

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(oauth.code_challenge(verifier), expected)


@override_settings(DISCORD_CLIENT_ID="cid", DISCORD_CLIENT_SECRET="csecret")
class ExchangeTests(TestCase):
    def test_the_code_is_exchanged_at_the_token_endpoint(self):
        provider = get_provider("discord")
        with patch("afc_auth.connections.oauth.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"access_token": "at", "scope": "identify"}
            out = oauth.exchange_code(
                provider, code="THECODE", code_verifier="v",
                redirect_uri="https://api.afc.test/cb/",
            )
        self.assertEqual(out["access_token"], "at")
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["code"], "THECODE")
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code_verifier"], "v")

    def test_a_refused_exchange_raises_rather_than_returning_a_half_result(self):
        provider = get_provider("discord")
        with patch("afc_auth.connections.oauth.requests.post") as post:
            post.return_value.status_code = 400
            post.return_value.text = "invalid_grant"
            with self.assertRaises(oauth.OAuthError):
                oauth.exchange_code(
                    provider, code="BAD", code_verifier="v",
                    redirect_uri="https://api.afc.test/cb/",
                )

    def test_the_error_message_does_not_leak_the_provider_body(self):
        """A provider can echo the client secret back inside an error body. It goes to the log, not
        into an exception a view might render."""
        provider = get_provider("discord")
        with patch("afc_auth.connections.oauth.requests.post") as post:
            post.return_value.status_code = 401
            post.return_value.text = "secret=csecret leaked here"
            with self.assertRaises(oauth.OAuthError) as caught:
                oauth.exchange_code(
                    provider, code="BAD", code_verifier="v",
                    redirect_uri="https://api.afc.test/cb/",
                )
        self.assertNotIn("csecret", str(caught.exception))

    def test_a_refused_profile_fetch_raises(self):
        provider = get_provider("discord")
        with patch("afc_auth.connections.oauth.requests.get") as get:
            get.return_value.status_code = 403
            with self.assertRaises(oauth.OAuthError):
                oauth.fetch_profile(provider, "at")
