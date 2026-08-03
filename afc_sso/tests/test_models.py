"""AFCSSOApplication is the per-org permission record. Everything an org may ever see
is described here and nowhere else, mirroring afc_partner_api.Partner."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from oauth2_provider.models import get_application_model

from afc_sso.models import SSO_FIELD_TOGGLES

Application = get_application_model()
User = get_user_model()


class ApplicationToggleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="admin_sso", email="admin_sso@afc.test", password="x"
        )

    def _app(self, **toggles):
        return Application.objects.create(
            name="Test Org",
            user=self.owner,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/callback",
            algorithm=Application.RS256_ALGORITHM,
            **toggles,
        )

    def test_every_toggle_defaults_to_off(self):
        app = self._app()
        for field in SSO_FIELD_TOGGLES:
            self.assertFalse(getattr(app, field), f"{field} must default to False")

    def test_allowed_scopes_always_includes_openid(self):
        self.assertEqual(self._app().allowed_scopes(), {"openid"})

    def test_allowed_scopes_reflects_the_toggles(self):
        app = self._app(share_profile=True, share_freefire_uid=True)
        self.assertEqual(app.allowed_scopes(), {"openid", "profile", "afc.freefire"})

    def test_suspended_application_is_not_active(self):
        app = self._app()
        self.assertTrue(app.is_active_partner())
        app.status = "suspended"
        app.save()
        self.assertFalse(app.is_active_partner())
