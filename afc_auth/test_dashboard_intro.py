"""
Tests for the one-time DASHBOARD intro callouts (owner 2026-06-12).

When a user is granted access to a role dashboard (admin / sponsor / organizer / vendor), their
next login shows a one-time callout pointing at the nav menu where that dashboard lives. Covers
the contract points the feature relies on:
  - User.seen_dashboard_intros defaults to {} (so a fresh grant shows the callout).
  - POST /auth/mark-dashboard-intro-seen/ flips ONE key, validates the dashboard id, is
    Bearer-auth gated, and is idempotent (afc_auth.views.mark_dashboard_intro_seen).
  - GET  /auth/get-user-profile/ includes seen_dashboard_intros (read by the frontend
    AuthContext -> DashboardIntroCoachmark).

Run: python manage.py test afc_auth.test_dashboard_intro
"""
import json

from django.test import TestCase, Client

from afc_auth.models import SessionToken, User


class SeenDashboardIntrosDefaultTests(TestCase):
    """The model-level default: a fresh user has dismissed no dashboard intros."""

    def test_defaults_to_empty_dict(self):
        user = User.objects.create(
            username="fresh", email="fresh@x.com", full_name="Fresh One",
            role="player", password="x",
        )
        self.assertEqual(user.seen_dashboard_intros, {})


class MarkDashboardIntroSeenEndpointTests(TestCase):
    """POST /auth/mark-dashboard-intro-seen/ : auth gating + key validation + flip + idempotency."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create(
            username="sponsor1", email="sponsor1@x.com", full_name="Sponsor One",
            role="player", password="x",
        )
        self.token = SessionToken.objects.create(user=self.user, token="tok_dash_intro")

    def _post(self, body=None, token="tok_dash_intro"):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            "/auth/mark-dashboard-intro-seen/",
            data=json.dumps(body or {}),
            content_type="application/json",
            **headers,
        )

    def test_requires_authorization_header(self):
        resp = self._post(body={"dashboard": "sponsor"}, token=None)
        self.assertEqual(resp.status_code, 400)

    def test_rejects_invalid_token(self):
        resp = self._post(body={"dashboard": "sponsor"}, token="bogus")
        self.assertEqual(resp.status_code, 401)

    def test_rejects_unknown_dashboard(self):
        # Only the four known dashboard ids are accepted; anything else is a 400, nothing stored.
        resp = self._post(body={"dashboard": "moonbase"})
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.seen_dashboard_intros, {})

    def test_marks_one_dashboard_seen(self):
        resp = self._post(body={"dashboard": "sponsor"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.seen_dashboard_intros, {"sponsor": True})
        # A SECOND dashboard granted later keeps its own key independent.
        resp2 = self._post(body={"dashboard": "organizer"})
        self.assertEqual(resp2.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(
            self.user.seen_dashboard_intros, {"sponsor": True, "organizer": True},
        )

    def test_idempotent_repeat(self):
        self._post(body={"dashboard": "vendor"})
        resp = self._post(body={"dashboard": "vendor"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.seen_dashboard_intros, {"vendor": True})


class ProfilePayloadTests(TestCase):
    """GET /auth/get-user-profile/ carries seen_dashboard_intros for the AuthContext."""

    def test_profile_includes_seen_dashboard_intros(self):
        user = User.objects.create(
            username="org1", email="org1@x.com", full_name="Org One",
            role="player", password="x",
            seen_dashboard_intros={"organizer": True},
        )
        SessionToken.objects.create(user=user, token="tok_profile_intros")
        resp = Client().get(
            "/auth/get-user-profile/", HTTP_AUTHORIZATION="Bearer tok_profile_intros",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["seen_dashboard_intros"], {"organizer": True})
