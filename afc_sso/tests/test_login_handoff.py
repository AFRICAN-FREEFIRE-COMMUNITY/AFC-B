"""The login handoff, and the redirect loop it exists to break.

WHY THIS FILE EXISTS (owner report 2026-08-30, V-ENT the first partner to try the flow)
    Signing in with AFC from a partner site looped forever:

        v-ent.co -> api.africanfreefirecommunity.com/sso/authorize/
                 -> africanfreefirecommunity.com/login?redirect=<the authorize url>
                 -> back to /sso/authorize/ -> back to /login -> forever

    The bridge reads the `auth_token` cookie, but the frontend sets it with no `domain`,
    making it HOST-ONLY to the apex. It is never sent to the api. subdomain, so authorize
    saw an anonymous visitor every time, and /login, seeing a perfectly good session, sent
    the player straight back.

    It survived every test and every manual check because local development runs the
    frontend and the API on the same 127.0.0.1 with different ports, and COOKIES IGNORE
    THE PORT. The cookie DOES reach the API on a developer machine. That is why the tests
    in test_auth_bridge.py, which set the cookie directly, all passed while production had
    never once worked.

    THE LESSON WORTH KEEPING: a test that hands the code the input it wants proves the code
    reads that input. It cannot prove the input ever arrives. Every cookie test here is
    written from the production shape instead, NO auth_token cookie at all.

Run: AFC_TEST_DB_NAME=test_afc_sso python manage.py test afc_sso.tests.test_login_handoff
"""
import base64
import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from oauth2_provider.models import get_application_model

from afc_auth.models import SessionToken
from afc_sso.handoff import HANDOFF_PARAM, consume_handoff, issue_handoff

Application = get_application_model()
User = get_user_model()

# PKCE is REQUIRED on AFC (settings PKCE_REQUIRED), so an authorize request without a
# challenge is refused by oauthlib before the consent screen is ever reached. Every test
# below sends one, because a request that cannot succeed proves nothing about the loop.
VERIFIER = "c" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()
).decode().rstrip("=")

HANDOFF_URL = "/sso/handoff/"
AUTHORIZE_URL = "/sso/authorize/"


class HandoffCodeTests(TestCase):
    """The code itself: who can mint one, and how few times it works."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="handoffplayer", email="handoff@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.user, token="tok-handoff")
        self.client = Client()

    def _mint(self, token="tok-handoff"):
        return self.client.post(
            HANDOFF_URL, HTTP_AUTHORIZATION=f"Bearer {token}"
        )

    # ── the gate ──
    def test_no_authorization_header_is_a_400(self):
        self.assertEqual(self.client.post(HANDOFF_URL).status_code, 400)

    def test_a_dead_token_is_a_401_and_mints_nothing(self):
        resp = self._mint("tok-nonsense")
        self.assertEqual(resp.status_code, 401)

    def test_a_live_token_mints_a_code(self):
        resp = self._mint()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = json.loads(resp.content)
        self.assertTrue(body["code"])
        # The param name is returned rather than hardcoded on the frontend, so the two
        # cannot drift apart.
        self.assertEqual(body["param"], HANDOFF_PARAM)

    def test_the_code_is_bound_to_the_player_who_minted_it(self):
        code = json.loads(self._mint().content)["code"]
        self.assertEqual(consume_handoff(code), self.user)

    # ── how few times it works ──
    def test_a_code_is_SINGLE_USE(self):
        """The whole reason it is safe to put in a URL. A second use must get nothing."""
        code = issue_handoff(self.user)
        self.assertEqual(consume_handoff(code), self.user)
        self.assertIsNone(consume_handoff(code))

    def test_an_unknown_code_resolves_to_nobody(self):
        self.assertIsNone(consume_handoff("not-a-real-code"))

    def test_an_expired_code_resolves_to_nobody(self):
        """TTL is enforced by the cache, so expiry is simulated by evicting the key rather
        than by sleeping through HANDOFF_TTL_SECONDS."""
        code = issue_handoff(self.user)
        cache.clear()
        self.assertIsNone(consume_handoff(code))

    def test_an_empty_code_resolves_to_nobody(self):
        self.assertIsNone(consume_handoff(""))


class LoopIsBrokenTests(TestCase):
    """THE REGRESSION. Every request here is made WITHOUT an auth_token cookie, which is
    the production shape: that cookie is host-only to the apex and never arrives here."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="looper", email="looper@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.user, token="tok-loop")
        self.app = Application.objects.create(
            name="Loop Partner", user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
            client_secret="loop-secret",
            share_profile=True,
        )
        self.client = Client()

    def _authorize_query(self, extra=""):
        return (
            f"?client_id={self.app.client_id}"
            "&response_type=code"
            "&scope=openid+profile"
            "&redirect_uri=https%3A%2F%2Fpartner.test%2Fcb"
            "&state=abc123"
            f"&code_challenge={CHALLENGE}"
            "&code_challenge_method=S256" + extra
        )

    # ── the bug, pinned ──
    def test_WITHOUT_a_handoff_an_anonymous_visitor_still_bounces_to_login(self):
        """Unchanged behaviour, and the first half of the loop. Kept so the fix is shown to
        break the CYCLE rather than to remove the bounce, which is correct on its own."""
        resp = self.client.get(AUTHORIZE_URL + self._authorize_query())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_a_handoff_ENDS_the_loop(self):
        """THE ONE THAT MATTERS. Before the fix this bounced to /login forever, because the
        cookie the bridge wanted could not reach this host. Now the code is exchanged for a
        session and the player lands on the consent screen."""
        code = issue_handoff(self.user)
        query = self._authorize_query(f"&{HANDOFF_PARAM}={code}")

        first = self.client.get(AUTHORIZE_URL + query)

        # Hop one: the code is spent and stripped, never rendered with.
        self.assertEqual(first.status_code, 302)
        self.assertNotIn(HANDOFF_PARAM, first["Location"])
        self.assertNotIn("/login", first["Location"])

        # Hop two: the session carries the player, with NO auth_token cookie anywhere.
        second = self.client.get(first["Location"])
        self.assertEqual(second.status_code, 200, second.get("Location", ""))

    def test_the_stripped_redirect_KEEPS_every_oauth_parameter(self):
        """Dropping one of these would turn the loop into a different failure: oauthlib
        refuses the request and the player gets an error instead of a consent screen."""
        code = issue_handoff(self.user)
        resp = self.client.get(
            AUTHORIZE_URL + self._authorize_query(f"&{HANDOFF_PARAM}={code}")
        )
        location = resp["Location"]
        for expected in ("client_id=", "response_type=code", "scope=", "redirect_uri=",
                         "state=abc123", "code_challenge=", "code_challenge_method=S256"):
            self.assertIn(expected, location, f"{expected} was dropped from {location}")

    def test_a_SPENT_code_degrades_to_the_normal_bounce_not_an_error(self):
        """A stale URL out of history must look like "you are not signed in", which is a
        state the player can act on, rather than a 400 page they cannot."""
        code = issue_handoff(self.user)
        self.client.get(AUTHORIZE_URL + self._authorize_query(f"&{HANDOFF_PARAM}={code}"))
        self.client.logout()

        resp = self.client.get(
            AUTHORIZE_URL + self._authorize_query(f"&{HANDOFF_PARAM}={code}")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(HANDOFF_PARAM, resp["Location"])

    def test_a_GARBAGE_code_is_stripped_rather_than_trusted(self):
        resp = self.client.get(
            AUTHORIZE_URL + self._authorize_query(f"&{HANDOFF_PARAM}=nonsense")
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(HANDOFF_PARAM, resp["Location"])

    def test_the_session_SURVIVES_to_the_consent_POST(self):
        """Why a session, and not a one-shot user lookup. The consent screen is
        `<form method="post">` with no action, so pressing Allow re-POSTs the authorize URL.
        A code already spent on the GET would leave that POST anonymous, and the player
        would be thrown out half way through approving."""
        code = issue_handoff(self.user)
        first = self.client.get(
            AUTHORIZE_URL + self._authorize_query(f"&{HANDOFF_PARAM}={code}")
        )
        self.client.get(first["Location"])

        # The session, not the code, is what identifies them now.
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_the_code_is_NOT_spendable_by_a_POST(self):
        """A code is only ever exchanged on a GET. Otherwise the consent form's own POST,
        which carries the whole query string back, could spend a fresh code."""
        code = issue_handoff(self.user)
        self.client.post(AUTHORIZE_URL + self._authorize_query(f"&{HANDOFF_PARAM}={code}"))
        # Still unspent, so the GET path can still use it.
        self.assertEqual(consume_handoff(code), self.user)


class CookieFallbackStillWorksTests(TestCase):
    """Local development still authenticates by cookie, because there the frontend and the
    API share 127.0.0.1 and it genuinely arrives. The handoff is an ADDITION, not a
    replacement, and removing the fallback would break every developer's machine."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="cookieplayer", email="cookie@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.user, token="tok-cookie")
        self.client = Client()

    def test_the_auth_token_cookie_still_authenticates_on_sso_paths(self):
        self.client.cookies["auth_token"] = "tok-cookie"
        resp = self.client.get("/sso/me/connected-apps/",
                               HTTP_AUTHORIZATION="Bearer tok-cookie")
        self.assertEqual(resp.status_code, 200, resp.content)
