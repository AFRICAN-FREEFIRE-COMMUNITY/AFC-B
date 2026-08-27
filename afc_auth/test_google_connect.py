"""Google CONNECT: linking Google to an existing AFC account.

WHY THIS FILE EXISTS
    Owner report 2026-08-27: clicking Connect on Google gave "We could not start connecting
    Google", from an account that had already signed in with Google. Google connect had never
    worked since it shipped in v7.1.64, and nothing in the suite noticed, because every test
    covered the ID-token shape that the frontend had stopped producing two months earlier.

    Two defects, both on the connect path:

      1. The page called the REDIRECT endpoint for every provider. Google is registered
         kind="id_token", and start_connection deliberately answers that with 400 "This provider
         is linked without a redirect". That 400 was the toast.
      2. link_google accepted ONLY a `credential` (a Google ID token). The sign-in button moved to
         the GIS popup CODE client on 2026-06-21, so the browser holds an auth CODE. Even wired
         correctly, the endpoint could not have understood what it was sent.

WHAT IS COVERED
    The shared exchange helper on its own (no network, no database), and the endpoint accepting
    BOTH shapes. The endpoint tests are what would have caught the original bug: they post the
    shape the real frontend actually sends.

Run: AFC_TEST_DB_NAME=test_afc_google python manage.py test afc_auth.test_google_connect
"""
import json
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase, override_settings

from afc_auth.connections.providers.google import (
    GoogleAuthError,
    resolve_id_token,
)
from afc_auth.models import ConnectedAccount, SessionToken, User, UserProfile


class _FakeResponse:
    """Stand-in for the requests.Response from Google's token endpoint."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


# ── the shared helper, with no database and no network ────────────────────────────────────────
class ResolveIdTokenTests(SimpleTestCase):
    def test_a_credential_is_returned_unchanged(self):
        """The ID-token shape still works: sign-in's older path must not regress."""
        self.assertEqual(
            resolve_id_token(credential="an-id-token", client_id="cid"),
            "an-id-token",
        )

    def test_a_code_is_exchanged_for_an_id_token(self):
        """THE SHAPE THE REAL FRONTEND SENDS. The GIS popup code client yields a CODE."""
        with patch(
            "requests.post",
            return_value=_FakeResponse(200, {"id_token": "exchanged-token"}),
        ) as posted:
            got = resolve_id_token(code="auth-code", client_id="cid", client_secret="sec")
        self.assertEqual(got, "exchanged-token")
        sent = posted.call_args.kwargs["data"]
        self.assertEqual(sent["code"], "auth-code")
        self.assertEqual(sent["grant_type"], "authorization_code")
        # "postmessage" is not a URL and must stay: the popup returns the code through
        # postMessage, so there is no redirect URI registered with Google to use instead.
        self.assertEqual(sent["redirect_uri"], "postmessage")

    def test_neither_shape_is_a_400(self):
        with self.assertRaises(GoogleAuthError) as caught:
            resolve_id_token(client_id="cid")
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_missing_client_id_is_a_400_not_a_401(self):
        """A server misconfiguration is not the player's fault, so it must not read as a rejected
        credential."""
        with self.assertRaises(GoogleAuthError) as caught:
            resolve_id_token(credential="x", client_id=None)
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_code_without_the_client_secret_is_a_400(self):
        """The code exchange needs the SECRET; the credential path does not. A deployment with the
        id but not the secret can sign in and could not connect, which is worth telling apart."""
        with self.assertRaises(GoogleAuthError) as caught:
            resolve_id_token(code="c", client_id="cid", client_secret=None)
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_rejected_exchange_is_a_401(self):
        with patch("requests.post", return_value=_FakeResponse(400, text="invalid_grant")):
            with self.assertRaises(GoogleAuthError) as caught:
                resolve_id_token(code="c", client_id="cid", client_secret="sec")
        self.assertEqual(caught.exception.status_code, 401)

    def test_an_exchange_that_returns_no_id_token_is_a_401(self):
        with patch("requests.post", return_value=_FakeResponse(200, {"access_token": "only"})):
            with self.assertRaises(GoogleAuthError) as caught:
                resolve_id_token(code="c", client_id="cid", client_secret="sec")
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_network_failure_is_a_401_not_a_500(self):
        with patch("requests.post", side_effect=OSError("no route to host")):
            with self.assertRaises(GoogleAuthError) as caught:
                resolve_id_token(code="c", client_id="cid", client_secret="sec")
        self.assertEqual(caught.exception.status_code, 401)


def _player(username="googleconnect"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name="Google Connect",
        role="player", password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


CLAIMS = {
    "sub": "google-subject-123",
    "name": "Google Person",
    "email": "person@gmail.com",
    "email_verified": True,
    "picture": "https://example.invalid/a.png",
}


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="cid",
    GOOGLE_OAUTH_CLIENT_SECRET="sec",
    VENT_CLIENT_ID="",
    VENT_CLIENT_SECRET="",
)
class LinkGoogleEndpointTests(TestCase):
    """POST /auth/connections/google/ must accept what the BROWSER actually holds."""

    def setUp(self):
        self.user, self.token = _player()

    def _post(self, payload):
        return Client().post(
            "/auth/connections/google/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_linking_with_a_CODE_succeeds(self):
        """THE REGRESSION TEST FOR THE REPORTED BUG.

        This is the shape the real page sends, and before the fix the endpoint rejected it with
        "credential is required" because it only understood id tokens.
        """
        with patch(
            "requests.post",
            return_value=_FakeResponse(200, {"id_token": "tok"}),
        ), patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value=CLAIMS,
        ):
            resp = self._post({"code": "auth-code-from-the-popup"})
        self.assertEqual(resp.status_code, 200, resp.content)
        link = ConnectedAccount.objects.get(user=self.user, provider="google")
        self.assertEqual(link.provider_user_id, "google-subject-123")

    def test_linking_with_a_CREDENTIAL_still_succeeds(self):
        """The older shape keeps working, so a stale client is not broken by the fix."""
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=CLAIMS):
            resp = self._post({"credential": "an-id-token"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            ConnectedAccount.objects.filter(user=self.user, provider="google").exists()
        )

    def test_sending_neither_is_a_400(self):
        resp = self._post({})
        self.assertEqual(resp.status_code, 400)

    def test_an_unverifiable_credential_is_a_401_and_stores_nothing(self):
        with patch(
            "google.oauth2.id_token.verify_oauth2_token",
            side_effect=ValueError("bad signature"),
        ):
            resp = self._post({"credential": "forged"})
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(ConnectedAccount.objects.filter(user=self.user).exists())

    def test_a_google_account_already_linked_elsewhere_is_refused(self):
        """The identity is unique across AFC, so one Google account cannot back two players."""
        other, _ = _player("googleconnectother")
        ConnectedAccount.objects.create(
            user=other, provider="google", provider_user_id="google-subject-123",
            username="Someone Else",
        )
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=CLAIMS):
            resp = self._post({"code": "c"})
        with patch("requests.post", return_value=_FakeResponse(200, {"id_token": "tok"})), \
                patch("google.oauth2.id_token.verify_oauth2_token", return_value=CLAIMS):
            resp = self._post({"code": "c"})
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertFalse(
            ConnectedAccount.objects.filter(user=self.user, provider="google").exists()
        )


@override_settings(GOOGLE_OAUTH_CLIENT_ID="cid", GOOGLE_OAUTH_CLIENT_SECRET="sec")
class StartConnectionShapeTests(TestCase):
    """The other half of the bug: the page must not send Google down the REDIRECT path.

    The backend already refused it correctly, which is why this is pinned rather than changed. The
    frontend now branches on the `kind` the provider list already carries.
    """

    def setUp(self):
        self.user, self.token = _player("googlestart")

    def test_google_start_is_refused_because_it_is_not_a_redirect_provider(self):
        resp = Client().get(
            "/auth/connections/google/start/?return_to=/profile/connected-apps",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 400)

    def test_the_provider_list_tells_the_frontend_which_kind_each_provider_is(self):
        """This is what lets the page branch WITHOUT hardcoding "google"."""
        resp = Client().get(
            "/auth/connections/",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = {r["provider"]: r for r in resp.json()["connections"]}
        self.assertIn("google", rows)
        self.assertEqual(rows["google"]["kind"], "id_token")
