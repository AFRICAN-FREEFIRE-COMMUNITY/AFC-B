"""
Tests for the sitewide automatic admin audit log.

Covers both halves of the feature:
  - afc_auth.middleware.AuditLogMiddleware  (the automatic capture)
  - afc_auth.views.get_audit_log            (GET /auth/get-audit-log/, the gated read API)

Run: python manage.py test afc_auth.test_audit_log
"""
import json
from datetime import timedelta
from types import SimpleNamespace

from django.http import HttpResponse
from django.test import TestCase, RequestFactory, Client
from django.utils import timezone

from afc_auth.middleware import AuditLogMiddleware
from afc_auth.models import AuditLog, SessionToken, Roles, UserRoles, User


def _ok(_request):
    """Dummy downstream handler returning 200, used to drive the middleware in unit tests."""
    return HttpResponse(status=200)


class AuditMiddlewareTests(TestCase):
    """The capture side: WHO gets logged and WHAT is recorded."""

    def setUp(self):
        self.factory = RequestFactory()
        # An admin (User.role == "admin") and an ordinary player, each with a live SessionToken.
        self.admin = User.objects.create(username="adminuser", email="admin@x.com",
                                         full_name="Admin User", role="admin", password="x")
        self.player = User.objects.create(username="playeruser", email="player@x.com",
                                          full_name="Player User", role="player", password="x")
        self.admin_token = SessionToken.objects.create(user=self.admin, token="tok_admin")
        self.player_token = SessionToken.objects.create(user=self.player, token="tok_player")
        self.mw = AuditLogMiddleware(_ok)

    def _run(self, method, path, token=None, resolver=None, json_body=None, **extra):
        """Build a request, attach an optional Bearer token + resolver_match (+ optional JSON body),
        run it through the middleware, and return the response."""
        if json_body is not None:
            req = getattr(self.factory, method.lower())(
                path, data=json.dumps(json_body), content_type="application/json"
            )
        else:
            req = getattr(self.factory, method.lower())(path, **extra)
        if token:
            req.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        if resolver is not None:
            req.resolver_match = resolver
        return self.mw(req)

    # ── Arrange-Act-Assert: admin mutation IS logged ────────────────────────────────────────────
    def test_admin_post_is_logged(self):
        resolver = SimpleNamespace(url_name="delete_product",
                                   view_name="afc_shop.views.delete_product",
                                   kwargs={"product_id": 7})
        self._run("post", "/shop/delete-product/", token="tok_admin", resolver=resolver)

        self.assertEqual(AuditLog.objects.count(), 1)
        row = AuditLog.objects.first()
        self.assertEqual(row.actor, self.admin)
        self.assertEqual(row.actor_username, "adminuser")
        self.assertEqual(row.method, "POST")
        self.assertEqual(row.action, "delete_product")
        self.assertEqual(row.status_code, 200)
        self.assertEqual(row.target_type, "product_id")
        self.assertEqual(row.target_id, "7")
        self.assertIn("admin", row.actor_role)
        # Human-readable summary from the slug map + target (not "POST /shop/...").
        self.assertEqual(row.summary, "Deleted a shop product #7")

    def test_summary_falls_back_to_humanized_slug(self):
        """Unmapped slugs become a readable label, never a raw method/path."""
        resolver = SimpleNamespace(url_name="edit_solo_match_result", view_name="x", kwargs={})
        self._run("post", "/events/edit-solo/", token="tok_admin", resolver=resolver)
        self.assertEqual(AuditLog.objects.first().summary, "Edit solo match result")

    def test_view_supplied_summary_overrides_generic(self):
        """When a view calls set_audit() the middleware records THAT specific summary (entity name +
        before/after) instead of the generic slug one, and merges audit_details into metadata."""
        from afc_auth.audit import set_audit
        resolver = SimpleNamespace(url_name="edit_event", view_name="x", kwargs={"event_id": 9})

        def get_response(req):
            set_audit(req, "Changed Detty December from internal to external",
                      changes=["event_type: internal -> external"])
            return HttpResponse(status=200)

        mw = AuditLogMiddleware(get_response)
        req = self.factory.post("/events/edit-event/", data=json.dumps({"event_id": 9}),
                                content_type="application/json")
        req.META["HTTP_AUTHORIZATION"] = "Bearer tok_admin"
        req.resolver_match = resolver
        mw(req)

        row = AuditLog.objects.first()
        self.assertEqual(row.summary, "Changed Detty December from internal to external")
        self.assertEqual(row.metadata["details"]["changes"], ["event_type: internal -> external"])

    def test_json_body_captured_and_redacted_in_details(self):
        """The expandable details come from the captured JSON body; secrets are masked there too."""
        resolver = SimpleNamespace(url_name="edit_event", view_name="x", kwargs={"event_id": 5})
        self._run(
            "post", "/events/edit/", token="tok_admin", resolver=resolver,
            json_body={"registration_start_time": "10:30", "password": "hunter2"},
        )
        row = AuditLog.objects.first()
        self.assertEqual(row.metadata["body"].get("registration_start_time"), "10:30")
        self.assertEqual(row.metadata["body"].get("password"), "***")
        self.assertEqual(row.summary, "Edited an event #5")

    def test_player_post_is_not_logged(self):
        """An ordinary user mutating data is NOT an admin action."""
        self._run("post", "/team/create/", token="tok_player")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_admin_get_is_not_logged(self):
        """Reads are never audited."""
        self._run("get", "/shop/view-products/", token="tok_admin")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_anonymous_post_is_not_logged(self):
        """No token -> cannot be an admin action."""
        self._run("post", "/auth/login/")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_expired_token_is_not_logged(self):
        """An expired session resolves to no actor, so nothing is logged."""
        self.admin_token.expires_at = timezone.now() - timedelta(days=1)
        self.admin_token.save()
        self._run("post", "/shop/delete-product/", token="tok_admin")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_self_endpoint_is_skipped(self):
        """The audit reader's own path is excluded to avoid self-noise."""
        self._run("post", "/auth/get-audit-log/", token="tok_admin")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_granular_only_admin_is_logged(self):
        """A user whose base role is 'player' but who holds a granular UserRoles admin role IS
        logged (granular-only admins must be covered)."""
        role = Roles.objects.create(role_name="shop_admin")
        UserRoles.objects.create(user=self.player, role=role)
        self._run("post", "/shop/edit-product/", token="tok_player")
        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertIn("shop_admin", AuditLog.objects.first().actor_role)

    def test_sensitive_query_params_are_redacted(self):
        """Secret-looking query keys must be masked in metadata (never store secrets)."""
        self._run("post", "/shop/x/?password=hunter2&page=2", token="tok_admin")
        row = AuditLog.objects.first()
        self.assertEqual(row.metadata["query"].get("password"), "***")
        self.assertEqual(row.metadata["query"].get("page"), "2")

    def test_non_2xx_response_still_logged_with_status(self):
        """A failed admin mutation (e.g. 403/500) is still recorded, with its real status."""
        mw = AuditLogMiddleware(lambda r: HttpResponse(status=403))
        req = self.factory.post("/shop/delete-product/")
        req.META["HTTP_AUTHORIZATION"] = "Bearer tok_admin"
        mw(req)
        self.assertEqual(AuditLog.objects.count(), 1)
        self.assertEqual(AuditLog.objects.first().status_code, 403)

    def test_logging_failure_never_breaks_request(self):
        """Best-effort contract: even if capture blows up, the response passes through unharmed."""
        mw = AuditLogMiddleware(_ok)
        req = self.factory.post("/shop/x/")
        req.META["HTTP_AUTHORIZATION"] = "Bearer tok_admin"
        # Force _maybe_log to raise; __call__ must swallow it and still return the 200.
        mw._maybe_log = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        resp = mw(req)
        self.assertEqual(resp.status_code, 200)


class AuditLogEndpointTests(TestCase):
    """The read side: GET /auth/get-audit-log/ gating, envelope, and filters."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create(username="adminuser", email="admin@x.com",
                                         full_name="Admin User", role="admin", password="x")
        self.player = User.objects.create(username="playeruser", email="player@x.com",
                                          full_name="Player User", role="player", password="x")
        # The audit log is head-admin-only, so the privileged caller needs the head_admin role.
        head = Roles.objects.create(role_name="head_admin")
        UserRoles.objects.create(user=self.admin, role=head)
        # A plain role=="admin" user WITHOUT head_admin must be denied (regression guard for the gate).
        self.plain_admin = User.objects.create(username="plainadmin", email="pa@x.com",
                                               full_name="Plain Admin", role="admin", password="x")
        self.admin_token = SessionToken.objects.create(user=self.admin, token="tok_admin")
        self.player_token = SessionToken.objects.create(user=self.player, token="tok_player")
        self.plain_token = SessionToken.objects.create(user=self.plain_admin, token="tok_plain")
        # Seed a few rows directly (decoupled from the middleware).
        for i in range(30):
            AuditLog.objects.create(
                actor=self.admin, actor_username="adminuser", actor_role="admin",
                action="delete_product" if i % 2 == 0 else "edit_event",
                method="POST" if i % 2 == 0 else "PATCH",
                path=f"/shop/x/{i}/", status_code=200,
            )

    def _get(self, token=None, **params):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get("/auth/get-audit-log/", params, **headers)

    def test_requires_head_admin(self):
        self.assertEqual(self._get().status_code, 400)                    # no token
        self.assertEqual(self._get(token="tok_player").status_code, 403)  # normal user
        self.assertEqual(self._get(token="tok_plain").status_code, 403)   # role==admin but NOT head_admin
        self.assertEqual(self._get(token="tok_admin").status_code, 200)   # head_admin

    def test_envelope_and_pagination(self):
        resp = self._get(token="tok_admin", limit=10, offset=0)
        body = resp.json()
        self.assertEqual(len(body["results"]), 10)
        self.assertEqual(body["total_count"], 30)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], 10)
        # Last page: no more.
        last = self._get(token="tok_admin", limit=10, offset=20).json()
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])

    def test_filter_by_action_and_method(self):
        body = self._get(token="tok_admin", action="edit_event", limit=100).json()
        self.assertEqual(body["total_count"], 15)
        self.assertTrue(all(r["action"] == "edit_event" for r in body["results"]))

        body = self._get(token="tok_admin", method="PATCH", limit=100).json()
        self.assertTrue(all(r["method"] == "PATCH" for r in body["results"]))

    def test_search_q(self):
        body = self._get(token="tok_admin", q="adminuser", limit=100).json()
        self.assertEqual(body["total_count"], 30)


class SuperAdminRoleProtectionTests(TestCase):
    """super_admin is the top role: a head_admin must NOT be able to grant it or strip it from a
    super_admin; only another super_admin can. Guards afc_auth.views.edit_user_roles."""

    def setUp(self):
        self.client = Client()
        self.super_role = Roles.objects.create(role_name="super_admin")
        self.head_role = Roles.objects.create(role_name="head_admin")
        self.shop_role = Roles.objects.create(role_name="shop_admin")

        # A head_admin actor (base role "admin" + head_admin granular).
        self.head = User.objects.create(username="head", email="head@x.com",
                                        full_name="Head", role="admin", password="x")
        UserRoles.objects.create(user=self.head, role=self.head_role)
        SessionToken.objects.create(user=self.head, token="t_head")

        # A super_admin actor.
        self.superu = User.objects.create(username="super", email="super@x.com",
                                          full_name="Super", role="admin", password="x")
        UserRoles.objects.create(user=self.superu, role=self.super_role)
        SessionToken.objects.create(user=self.superu, token="t_super")

        # A target user who already holds super_admin.
        self.target = User.objects.create(username="target", email="target@x.com",
                                          full_name="Target", role="admin", password="x")
        UserRoles.objects.create(user=self.target, role=self.super_role)

    def _edit_roles(self, token, email, username, role_ids):
        return self.client.post(
            "/auth/edit-user-roles/",
            data=json.dumps({"email": email, "username": username, "new_role_ids": role_ids}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_head_admin_cannot_strip_super_admin(self):
        resp = self._edit_roles("t_head", "target@x.com", "target", [self.shop_role.role_id])
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(UserRoles.objects.filter(user=self.target, role=self.super_role).exists())

    def test_head_admin_cannot_grant_super_admin(self):
        normal = User.objects.create(username="norm", email="norm@x.com",
                                     full_name="Norm", role="player", password="x")
        resp = self._edit_roles("t_head", "norm@x.com", "norm", [self.super_role.role_id])
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(UserRoles.objects.filter(user=normal, role=self.super_role).exists())

    def test_super_admin_can_manage_super_admin(self):
        resp = self._edit_roles("t_super", "target@x.com", "target", [self.shop_role.role_id])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(UserRoles.objects.filter(user=self.target, role=self.super_role).exists())
