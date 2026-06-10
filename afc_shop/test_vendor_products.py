"""
afc_shop/test_vendor_products.py
================================================================================
Tests for the BUGFIX (2026-06-10) on the admin vendor-product approval surface
(afc_shop/vendors.py cluster B):

  1. _serialize_vendor_product now returns `approved_by` (the approver's username
     for an approved product, None otherwise) so the admin "Product approvals"
     table can show WHO accepted a product, not just WHEN.
  2. admin_list_pending_products now accepts an optional ?status= query param
     (submitted [default, back-compat] / approved / rejected / all) so an approved
     product no longer vanishes from the requests view the moment it is approved.

These tests drive the real HTTP endpoints (GET /shop/admin/products/pending/) with a
Bearer admin token, exactly as the frontend does, so the serialiser shape + the new
filter are both exercised end-to-end. No network is touched: everything is local DB.

Run: python manage.py test afc_shop
"""
from django.test import TestCase, Client

from afc_auth.models import SessionToken, User
from afc_shop.models import Product, ProductVariant, Vendor


class AdminVendorProductApprovalTests(TestCase):
    """The admin approval-queue read path: approved_by surfaced + ?status= filter."""

    def setUp(self):
        # Arrange: an admin caller (role == "admin") with a live session token. The
        # endpoint is require_admin, so we authenticate with a Bearer SessionToken just
        # like the live frontend client (lib/marketplaceAdmin.ts).
        self.client = Client()
        self.admin = User.objects.create(
            username="shopadmin", email="shopadmin@x.com",
            full_name="Shop Admin", role="admin", password="x",
        )
        # SessionToken.save() auto-fills expires_at (7-day lifetime), so the token is live.
        self.token = SessionToken.objects.create(user=self.admin, token="tok_shopadmin")

        # A vendor whose products move through the approval lifecycle.
        self.vendor_user = User.objects.create(
            username="vendoruser", email="vendor@x.com",
            full_name="Vendor User", role="player", password="x",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user, display_name="Vendor Test Co", status="active",
        )

        # Three products, one per relevant approval state, each with a variant so the
        # price/variant columns the queue renders are populated.
        # SUBMITTED: still in the pending queue (the default view).
        self.submitted = self._make_product("Pending Hoodie", "submitted")
        # APPROVED: approved BY the admin (this is the row that previously disappeared
        # AND whose approver was never surfaced).
        self.approved = self._make_product(
            "Vendor Test Hoodie", "approved", approved_by=self.admin,
        )
        # REJECTED: carries a rejection_reason for the rejected tab.
        self.rejected = self._make_product(
            "Rejected Hoodie", "rejected", rejection_reason="Price mismatch",
        )

    def _make_product(self, name, approval_status, approved_by=None, rejection_reason=""):
        """Create a vendor product in a given approval state with one variant."""
        p = Product.objects.create(
            name=name,
            product_type="bundle",
            status="active",
            vendor=self.vendor,
            approval_status=approval_status,
            approved_by=approved_by,
            rejection_reason=rejection_reason,
        )
        ProductVariant.objects.create(product=p, sku=f"{name}-sku", price="10.00")
        return p

    def _get_pending(self, status=None):
        """Call GET /shop/admin/products/pending/ as the admin, optionally with ?status=."""
        path = "/shop/admin/products/pending/"
        if status is not None:
            path = f"{path}?status={status}"
        return self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    # ── Fix 1: approved_by is now in the serialised payload ─────────────────────────
    def test_serializer_includes_approved_by_for_approved_product(self):
        # Act: list the approved products.
        resp = self._get_pending(status="approved")
        # Assert: the approved row carries the approver's username (not just approved_at).
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["products"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Vendor Test Hoodie")
        self.assertIn("approved_by", row)
        self.assertEqual(row["approved_by"], "shopadmin")

    def test_serializer_approved_by_is_none_when_not_approved(self):
        # A submitted (never-approved) product reports approved_by = None, not a crash.
        resp = self._get_pending(status="submitted")
        self.assertEqual(resp.status_code, 200)
        row = resp.json()["products"][0]
        self.assertIn("approved_by", row)
        self.assertIsNone(row["approved_by"])

    # ── Fix 2: the ?status= filter ──────────────────────────────────────────────────
    def test_default_returns_only_submitted(self):
        # Back-compat: no param -> only the pending (submitted) queue, as before.
        resp = self._get_pending()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "submitted")
        names = {p["name"] for p in body["products"]}
        self.assertEqual(names, {"Pending Hoodie"})

    def test_status_approved_returns_approved_with_approver(self):
        # The approved tab returns the approved product WITH its approver surfaced,
        # the exact case from the bug report (it used to vanish from this view).
        resp = self._get_pending(status="approved")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "approved")
        self.assertEqual(len(body["products"]), 1)
        self.assertEqual(body["products"][0]["name"], "Vendor Test Hoodie")
        self.assertEqual(body["products"][0]["approved_by"], "shopadmin")

    def test_status_rejected_returns_rejected_with_reason(self):
        resp = self._get_pending(status="rejected")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(len(body["products"]), 1)
        self.assertEqual(body["products"][0]["name"], "Rejected Hoodie")
        self.assertEqual(body["products"][0]["rejection_reason"], "Price mismatch")

    def test_status_all_returns_every_state(self):
        # all -> no approval_status filter: every vendor product in any state.
        resp = self._get_pending(status="all")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "all")
        names = {p["name"] for p in body["products"]}
        self.assertEqual(
            names, {"Pending Hoodie", "Vendor Test Hoodie", "Rejected Hoodie"},
        )

    def test_unknown_status_falls_back_to_submitted(self):
        # A typo must never widen the result set: it falls back to the submitted queue.
        resp = self._get_pending(status="bogus")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "submitted")
        names = {p["name"] for p in body["products"]}
        self.assertEqual(names, {"Pending Hoodie"})

    def test_requires_admin(self):
        # A non-admin caller is refused (require_admin), so the new filter can't leak.
        non_admin = User.objects.create(
            username="plainuser", email="plain@x.com",
            full_name="Plain User", role="player", password="x",
        )
        ntoken = SessionToken.objects.create(user=non_admin, token="tok_plain")
        resp = self.client.get(
            "/shop/admin/products/pending/?status=approved",
            HTTP_AUTHORIZATION=f"Bearer {ntoken.token}",
        )
        self.assertEqual(resp.status_code, 403)
