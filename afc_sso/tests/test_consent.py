"""Consent must MEAN something. The dangerous case is silent widening: a player who
approved 'name and country' in June must not find themselves having approved their
Free Fire UID in August because AFC flipped a toggle."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_application_model

from afc_auth.models import SessionToken
from afc_sso.views import consent_is_current

AccessToken = get_access_token_model()
Application = get_application_model()
User = get_user_model()

AUTH_COOKIE = {"cookie": "auth_token=tok-consent"}


class ConsentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="consenter", email="consent@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.user, token="tok-consent")
        self.app = Application.objects.create(
            name="Org", user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
            share_profile=True,
        )

    def _authorize(self, scope, headers=None, approval_prompt=None):
        params = {
            "client_id": self.app.client_id,
            "response_type": "code",
            "redirect_uri": "https://partner.test/cb",
            "scope": scope,
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
        }
        # approval_prompt=auto is the partner ASKING to skip the screen. The library
        # defaults to "force", so this parameter is the only way silent reuse happens,
        # which makes it the thing the widening guard has to survive.
        if approval_prompt:
            params["approval_prompt"] = approval_prompt
        return self.client.get(
            "/sso/authorize/",
            params,
            headers=headers if headers is not None else AUTH_COOKIE,
        )

    def _existing_token(self, scope):
        return AccessToken.objects.create(
            user=self.user, application=self.app, token="prior-token",
            expires=timezone.now() + timezone.timedelta(hours=1), scope=scope,
        )

    def test_consent_is_current_when_nothing_widened(self):
        self.assertTrue(consent_is_current({"openid", "profile"}, {"openid", "profile"}))

    def test_widening_invalidates_consent(self):
        self.assertFalse(
            consent_is_current({"openid", "profile"}, {"openid", "profile", "afc.freefire"})
        )

    def test_narrowing_does_not_re_prompt(self):
        self.assertTrue(consent_is_current({"openid", "profile", "email"}, {"openid"}))

    def test_consent_screen_names_the_org_and_lists_the_data(self):
        resp = self._authorize("openid profile")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Org", body)
        self.assertIn("in-game name", body)

    def test_anonymous_visitor_is_sent_to_login_not_shown_consent(self):
        resp = self._authorize("openid", headers={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_login_redirect_carries_the_whole_authorize_request_back(self):
        """The partner's flow has to survive the detour through login, so the redirect
        parameter must hold the full authorize URL, not just the path."""
        resp = self._authorize("openid", headers={})
        self.assertIn("redirect=", resp["Location"])
        self.assertIn("client_id", resp["Location"])

    def test_a_live_approval_is_reused_when_nothing_widened(self):
        """The baseline the next test contrasts with: a repeat of an approval the player
        already gave may be honoured silently when the partner asks for that."""
        self._existing_token("openid profile")
        resp = self._authorize("openid profile", approval_prompt="auto")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("partner.test/cb", resp["Location"])

    def test_widening_re_prompts_even_when_the_partner_asks_to_skip(self):
        """The whole point of the task: yesterday's Allow is not consent for a scope
        added today, and the partner cannot suppress the difference."""
        self._existing_token("openid profile")
        self.app.share_freefire_uid = True
        self.app.save()
        resp = self._authorize("openid profile afc.freefire", approval_prompt="auto")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Free Fire UID", resp.content.decode())

    def test_the_screen_is_shown_by_default_even_for_an_unchanged_request(self):
        """Silent reuse is opt-in by the partner, never the default."""
        self._existing_token("openid profile")
        self.assertEqual(self._authorize("openid profile").status_code, 200)

    def test_an_org_cannot_be_configured_to_skip_the_consent_screen(self):
        """skip_authorization is a first-party convenience in the library. Every org here
        is a third party, so the flag is pinned off on save."""
        self.app.skip_authorization = True
        self.app.save()
        self.app.refresh_from_db()
        self.assertFalse(self.app.skip_authorization)
