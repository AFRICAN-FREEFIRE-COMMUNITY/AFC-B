"""
Tests for the first-time WELCOME tour persistence.

Covers the three contract points the feature relies on:
  - User.has_seen_welcome defaults to False (so a brand-new user sees the tour).
  - POST /auth/mark-welcome-seen/ flips it True and is Bearer-auth gated (afc_auth.views.mark_welcome_seen).
  - GET  /auth/get-user-profile/ includes has_seen_welcome in its payload (read by the frontend AuthContext).

Run: python manage.py test afc_auth.test_welcome
"""
from django.test import TestCase, Client

from afc_auth.models import SessionToken, User


class WelcomeFlagDefaultTests(TestCase):
    """The model-level default: a fresh user has not seen the welcome tour."""

    def test_flag_defaults_false(self):
        # Arrange + Act: create a plain user.
        user = User.objects.create(
            username="newbie", email="newbie@x.com", full_name="New Bie",
            role="player", password="x",
        )
        # Assert: the welcome tour has not been seen yet.
        self.assertFalse(user.has_seen_welcome)


class MarkWelcomeSeenEndpointTests(TestCase):
    """POST /auth/mark-welcome-seen/ : auth gating + flag flip + idempotency."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create(
            username="player1", email="player1@x.com", full_name="Player One",
            role="player", password="x",
        )
        self.token = SessionToken.objects.create(user=self.user, token="tok_welcome")

    def _post(self, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post("/auth/mark-welcome-seen/", **headers)

    def test_requires_authorization_header(self):
        # No Authorization header -> 400, flag untouched.
        resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_seen_welcome)

    def test_rejects_invalid_token(self):
        # A bogus Bearer token -> 401, flag untouched.
        resp = self._post(token="not-a-real-token")
        self.assertEqual(resp.status_code, 401)
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_seen_welcome)

    def test_marks_seen_true(self):
        # Arrange: flag starts False.
        self.assertFalse(self.user.has_seen_welcome)
        # Act: a valid token flips it.
        resp = self._post(token="tok_welcome")
        # Assert: 200 + persisted True + echoed back in the body.
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["has_seen_welcome"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_seen_welcome)

    def test_is_idempotent(self):
        # Calling twice is a harmless no-op 200 the second time.
        self.assertEqual(self._post(token="tok_welcome").status_code, 200)
        resp = self._post(token="tok_welcome")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_seen_welcome)


class ProfilePayloadIncludesWelcomeFlagTests(TestCase):
    """GET /auth/get-user-profile/ must surface has_seen_welcome so the client can decide to show it."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create(
            username="player2", email="player2@x.com", full_name="Player Two",
            role="player", password="x",
        )
        self.token = SessionToken.objects.create(user=self.user, token="tok_profile")

    def _profile(self):
        return self.client.get(
            "/auth/get-user-profile/", HTTP_AUTHORIZATION="Bearer tok_profile",
        )

    def test_payload_includes_flag_false_by_default(self):
        body = self._profile().json()
        self.assertIn("has_seen_welcome", body)
        self.assertFalse(body["has_seen_welcome"])

    def test_payload_reflects_flag_after_marking_seen(self):
        # Flip it, then confirm the profile payload reports it as seen.
        self.client.post("/auth/mark-welcome-seen/", HTTP_AUTHORIZATION="Bearer tok_profile")
        body = self._profile().json()
        self.assertTrue(body["has_seen_welcome"])
