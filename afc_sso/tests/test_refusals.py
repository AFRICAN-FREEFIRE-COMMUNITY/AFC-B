"""How AFC says no. The open-redirect case is the one that turns a bug into a phishing
tool, so it gets an explicit test rather than being assumed."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from oauth2_provider.models import get_application_model

from afc_auth.models import SessionToken

Application = get_application_model()
User = get_user_model()


class RefusalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="refused", email="refused@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.user, token="tok-refuse")
        self.app = Application.objects.create(
            name="Org", user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
            share_profile=True,
        )

    def _authorize(self, **overrides):
        params = {
            "client_id": self.app.client_id,
            "response_type": "code",
            "redirect_uri": "https://partner.test/cb",
            "scope": "openid profile",
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
        }
        params.update(overrides)
        return self.client.get(
            "/sso/authorize/", params, headers={"cookie": "auth_token=tok-refuse"}
        )

    def test_mismatched_redirect_uri_does_not_redirect(self):
        resp = self._authorize(redirect_uri="https://evil.test/steal")
        self.assertNotEqual(resp.status_code, 302)
        self.assertNotIn("evil.test", resp.get("Location", ""))

    def test_suspended_application_cannot_authorize(self):
        self.app.status = "suspended"
        self.app.save()
        resp = self._authorize()
        self.assertNotEqual(resp.status_code, 200)

    def test_suspended_player_cannot_authorize(self):
        self.user.status = "suspended"
        self.user.save()
        resp = self._authorize()
        self.assertNotEqual(resp.status_code, 200)

    def test_scope_beyond_the_orgs_toggles_is_refused(self):
        """The org may only ever ask for what AFC granted it."""
        resp = self._authorize(scope="openid profile afc.freefire")
        self.assertNotEqual(resp.status_code, 200)

    def test_unknown_client_id_is_refused_without_redirecting(self):
        """An attacker probing client ids must not be handed a redirect to anywhere."""
        resp = self._authorize(client_id="not-a-real-client")
        self.assertNotEqual(resp.status_code, 302)

    def test_a_refusal_never_names_the_reason_in_a_redirect(self):
        """Refusals render on AFC's own page. If any of them ever redirected, the
        redirect target would be attacker-controlled."""
        for kwargs in (
            {"redirect_uri": "https://evil.test/steal"},
            {"client_id": "not-a-real-client"},
            {"scope": "openid profile afc.freefire"},
        ):
            resp = self._authorize(**kwargs)
            self.assertNotEqual(resp.status_code, 302, kwargs)
