"""AFC has no Django session. This bridge is the ONLY thing that makes request.user
work for the OIDC authorize view, so its blast radius must stay confined to /sso/."""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from afc_auth.models import SessionToken
from afc_sso.middleware import SSOSessionTokenMiddleware

User = get_user_model()


class AuthBridgeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="bridge_player", email="bridge@afc.test", password="x"
        )
        self.session = SessionToken.objects.create(user=self.user, token="tok-valid")
        self.factory = RequestFactory()

    def _run(self, path, cookies=None):
        request = self.factory.get(path)
        request.COOKIES.update(cookies or {})
        SSOSessionTokenMiddleware(lambda r: r)(request)
        return request

    def test_valid_cookie_populates_request_user_on_sso_paths(self):
        request = self._run("/sso/authorize/", {"auth_token": "tok-valid"})
        self.assertEqual(request.user, self.user)
        self.assertTrue(request.user.is_authenticated)

    def test_no_cookie_leaves_the_user_anonymous(self):
        self.assertFalse(self._run("/sso/authorize/").user.is_authenticated)

    def test_unknown_token_leaves_the_user_anonymous(self):
        request = self._run("/sso/authorize/", {"auth_token": "tok-nonsense"})
        self.assertFalse(request.user.is_authenticated)

    def test_expired_token_leaves_the_user_anonymous(self):
        from django.utils import timezone
        self.session.expires_at = timezone.now() - timezone.timedelta(hours=1)
        self.session.save()
        request = self._run("/sso/authorize/", {"auth_token": "tok-valid"})
        self.assertFalse(request.user.is_authenticated)

    def test_non_sso_paths_are_untouched(self):
        """Confining the bridge is the point: every other AFC endpoint keeps using
        validate_token explicitly, and gains no implicit cookie auth from this."""
        request = self._run("/events/get-all-events/", {"auth_token": "tok-valid"})
        self.assertFalse(hasattr(request, "user") and request.user.is_authenticated)
