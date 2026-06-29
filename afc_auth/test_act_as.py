"""
afc_auth/test_act_as.py
================================================================================
Tests for the SUPER-ADMIN "act-as" (god-mode) feature (afc_auth/act_as.py + the
patched org-shell reads and vendor gates). Security core: a head_admin/super_admin
may step INTO any organizer or vendor dashboard via the X-Act-As-Org /
X-Act-As-Vendor headers, and NOBODY else can.

What these tests pin down (owner decisions 2026-06-29):
  • Gate breadth: ONLY head_admin / super_admin / Django superuser are god-mode.
    organizer_admin and plain role=="admin" are NOT — the header is inert for them.
  • The header only SELECTS a target; it grants nothing on its own (a non-god-mode
    caller sending the header is treated exactly as if they had not sent it).
  • Bank / payout is OUT of scope: the paystack_payout vendor gate must NOT honor
    act-as, even for a god-mode admin (products + orders yes, bank/payout no).

These drive the real HTTP endpoints with a Bearer SessionToken (like the frontend)
plus a couple of direct function calls for the resolver + bank-gate guard. No
network is touched; everything is local DB.

Run: python manage.py test afc_auth.test_act_as
"""
from django.test import TestCase, Client, RequestFactory

from afc_auth.models import User, Roles, UserRoles, SessionToken
from afc_auth import act_as
from afc_organizers.models import Organization, OrganizationMember
from afc_shop.models import Product, ProductVariant, Vendor
from afc_shop import paystack_payout


# ──────────────────────────────────────────────────────────────────────────────
# Shared fixture builder
# ──────────────────────────────────────────────────────────────────────────────
class _Base(TestCase):
    def setUp(self):
        self.client = Client()
        self.rf = RequestFactory()

        # Granular roles used by the gate.
        self.r_head, _ = Roles.objects.get_or_create(role_name="head_admin")
        self.r_super, _ = Roles.objects.get_or_create(role_name="super_admin")
        self.r_orgadmin, _ = Roles.objects.get_or_create(role_name="organizer_admin")

        # ── the cast of callers ──
        # head_admin (god-mode)
        self.head = self._user("head", role="admin")
        UserRoles.objects.create(user=self.head, role=self.r_head)
        self.head_tok = self._token(self.head)

        # super_admin (god-mode)
        self.superadmin = self._user("superadmin", role="admin")
        UserRoles.objects.create(user=self.superadmin, role=self.r_super)
        self.super_tok = self._token(self.superadmin)

        # Django superuser flag (god-mode) — no granular role at all
        self.djsuper = self._user("djsuper", role="player", is_superuser=True)
        self.djsuper_tok = self._token(self.djsuper)

        # organizer_admin — NOT god-mode (excluded by owner decision)
        self.orgadmin = self._user("orgadmin", role="admin")
        UserRoles.objects.create(user=self.orgadmin, role=self.r_orgadmin)
        self.orgadmin_tok = self._token(self.orgadmin)

        # plain player — NOT god-mode
        self.player = self._user("player", role="player")
        self.player_tok = self._token(self.player)

        # ── the org being managed (the caller is NOT a member) ──
        self.org = Organization.objects.create(slug="acme", name="Acme Esports")
        self.org_owner = self._user("owner", role="player")
        OrganizationMember.objects.create(organization=self.org, user=self.org_owner, role="owner")

        # ── the vendor being managed (the caller does NOT own it) ──
        self.vendor_user = self._user("vendoruser", role="player")
        self.vendor = Vendor.objects.create(
            user=self.vendor_user, display_name="Vendor Test Co", status="active",
        )
        self.vproduct = Product.objects.create(
            name="Vendor Headset", product_type="bundle", status="active",
            vendor=self.vendor, approval_status="approved",
        )
        ProductVariant.objects.create(product=self.vproduct, sku="vh-sku", price="10.00")

    # — helpers —
    def _user(self, name, role="player", is_superuser=False):
        return User.objects.create(
            username=name, email=f"{name}@x.com", full_name=name.title(),
            role=role, password="x", is_superuser=is_superuser,
        )

    def _token(self, user):
        # SessionToken.save() auto-fills expires_at, so the token is live.
        return SessionToken.objects.create(user=user, token=f"tok_{user.username}").token


# ──────────────────────────────────────────────────────────────────────────────
# 1. Unit: is_god_mode_admin gate breadth
# ──────────────────────────────────────────────────────────────────────────────
class IsGodModeAdminTests(_Base):
    def test_head_admin_is_god_mode(self):
        self.assertTrue(act_as.is_god_mode_admin(self.head))

    def test_super_admin_is_god_mode(self):
        self.assertTrue(act_as.is_god_mode_admin(self.superadmin))

    def test_django_superuser_is_god_mode(self):
        self.assertTrue(act_as.is_god_mode_admin(self.djsuper))

    def test_organizer_admin_is_NOT_god_mode(self):
        # Owner decision 2026-06-29: organizer_admin is excluded from the new dashboard
        # entry, even though it already bypasses org event gates elsewhere.
        self.assertFalse(act_as.is_god_mode_admin(self.orgadmin))

    def test_plain_player_is_NOT_god_mode(self):
        self.assertFalse(act_as.is_god_mode_admin(self.player))

    def test_none_is_NOT_god_mode(self):
        self.assertFalse(act_as.is_god_mode_admin(None))


# ──────────────────────────────────────────────────────────────────────────────
# 2. Org shell read: GET /organizers/get-organization/<slug>/
# ──────────────────────────────────────────────────────────────────────────────
class GetOrganizationActAsTests(_Base):
    URL = "/organizers/get-organization/acme/"

    def _get(self, token, act_as_org=None):
        extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if act_as_org is not None:
            extra["HTTP_X_ACT_AS_ORG"] = act_as_org
        return self.client.get(self.URL, **extra)

    def test_head_admin_acting_as_org_gets_full_access(self):
        resp = self._get(self.head_tok, act_as_org="acme")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["organization"]["slug"], "acme")
        # all-True permission map (the override has no member row)
        self.assertTrue(all(body["my_permissions"].values()))

    def test_super_admin_acting_as_org_gets_full_access(self):
        resp = self._get(self.super_tok, act_as_org="acme")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(all(resp.json()["my_permissions"].values()))

    def test_organizer_admin_with_header_is_forbidden(self):
        # organizer_admin is NOT god-mode -> the header is inert -> non-member -> 403.
        resp = self._get(self.orgadmin_tok, act_as_org="acme")
        self.assertEqual(resp.status_code, 403)

    def test_player_with_header_is_forbidden(self):
        # A normal user spoofing the header gains nothing.
        resp = self._get(self.player_tok, act_as_org="acme")
        self.assertEqual(resp.status_code, 403)

    def test_god_mode_without_header_is_forbidden(self):
        # A god-mode admin who is NOT acting-as (no header) is still a non-member -> 403.
        resp = self._get(self.head_tok, act_as_org=None)
        self.assertEqual(resp.status_code, 403)

    def test_god_mode_acting_as_different_org_is_forbidden(self):
        # Header targets a DIFFERENT org than the URL slug -> no override for THIS org.
        Organization.objects.create(slug="other", name="Other Org")
        resp = self._get(self.head_tok, act_as_org="other")
        self.assertEqual(resp.status_code, 403)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Org switcher feed: GET /organizers/get-my-organizations/
# ──────────────────────────────────────────────────────────────────────────────
class GetMyOrganizationsActAsTests(_Base):
    URL = "/organizers/get-my-organizations/"

    def _get(self, token, act_as_org=None):
        extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if act_as_org is not None:
            extra["HTTP_X_ACT_AS_ORG"] = act_as_org
        return self.client.get(self.URL, **extra)

    def test_god_mode_sees_override_row(self):
        resp = self._get(self.head_tok, act_as_org="acme")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        acme = [r for r in rows if r["organization"]["slug"] == "acme"]
        self.assertEqual(len(acme), 1)
        self.assertEqual(acme[0]["role"], "admin_override")
        self.assertTrue(all(acme[0]["permissions"].values()))

    def test_player_does_not_see_override_row(self):
        # Header is inert for a non-god-mode caller -> the org is not injected.
        resp = self._get(self.player_tok, act_as_org="acme")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        self.assertEqual([r for r in rows if r["organization"]["slug"] == "acme"], [])


# ──────────────────────────────────────────────────────────────────────────────
# 4. Vendor product CRUD: GET /shop/vendor/products/
# ──────────────────────────────────────────────────────────────────────────────
class VendorProductsActAsTests(_Base):
    URL = "/shop/vendor/products/"

    def _get(self, token, act_as_vendor=None):
        extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if act_as_vendor is not None:
            extra["HTTP_X_ACT_AS_VENDOR"] = str(act_as_vendor)
        return self.client.get(self.URL, **extra)

    def test_head_admin_acting_as_vendor_lists_products(self):
        resp = self._get(self.head_tok, act_as_vendor=self.vendor.id)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["products"][0]["name"], "Vendor Headset")

    def test_player_with_vendor_header_is_forbidden(self):
        # Not god-mode + not a vendor -> header inert -> 403.
        resp = self._get(self.player_tok, act_as_vendor=self.vendor.id)
        self.assertEqual(resp.status_code, 403)

    def test_god_mode_without_header_is_not_a_vendor(self):
        # head_admin owns no Vendor; with no act-as header the gate gives the normal 403.
        resp = self._get(self.head_tok, act_as_vendor=None)
        self.assertEqual(resp.status_code, 403)

    def test_god_mode_acts_on_suspended_vendor(self):
        # An admin may operate a suspended vendor's shop to fix it.
        self.vendor.status = "suspended"
        self.vendor.save(update_fields=["status"])
        resp = self._get(self.head_tok, act_as_vendor=self.vendor.id)
        self.assertEqual(resp.status_code, 200)


# ──────────────────────────────────────────────────────────────────────────────
# 5. Vendor order queue: GET /shop/fulfilment/my-orders/
# ──────────────────────────────────────────────────────────────────────────────
class VendorOrdersActAsTests(_Base):
    URL = "/shop/fulfilment/my-orders/"

    def test_god_mode_acting_as_vendor_gets_queue(self):
        # Empty queue is fine — the point is a 200 (the gate resolved the target vendor)
        # rather than the 403 a non-vendor caller would get without act-as.
        resp = self.client.get(
            self.URL,
            HTTP_AUTHORIZATION=f"Bearer {self.head_tok}",
            HTTP_X_ACT_AS_VENDOR=str(self.vendor.id),
        )
        self.assertEqual(resp.status_code, 200)

    def test_player_with_header_is_forbidden(self):
        resp = self.client.get(
            self.URL,
            HTTP_AUTHORIZATION=f"Bearer {self.player_tok}",
            HTTP_X_ACT_AS_VENDOR=str(self.vendor.id),
        )
        self.assertEqual(resp.status_code, 403)


# ──────────────────────────────────────────────────────────────────────────────
# 6. GUARD: bank / payout gate must NOT honor act-as (owner decision: out of scope)
# ──────────────────────────────────────────────────────────────────────────────
class BankPayoutNotActAsTests(_Base):
    def test_paystack_bank_gate_ignores_act_as_for_god_mode(self):
        # A god-mode admin sending X-Act-As-Vendor at the PAYSTACK PAYOUT gate must NOT be
        # resolved to the target vendor — bank/payout is deliberately out of god-mode
        # scope. The head_admin owns no Vendor, so the gate returns its normal 403.
        req = self.rf.get(
            "/shop/payout/bank/",
            HTTP_AUTHORIZATION=f"Bearer {self.head_tok}",
            HTTP_X_ACT_AS_VENDOR=str(self.vendor.id),
        )
        user, vendor, err = paystack_payout._require_active_vendor(req)
        self.assertIsNone(vendor)          # did NOT act as the target vendor
        self.assertIsNotNone(err)          # got the normal "not a vendor" failure
        self.assertEqual(err.status_code, 403)

    def test_resolver_directly_ignores_non_god_mode(self):
        # Sanity: resolve_acting_vendor returns None for a non-god-mode caller even with a
        # valid vendor id in the header.
        req = self.rf.get("/x/", HTTP_X_ACT_AS_VENDOR=str(self.vendor.id))
        self.assertIsNone(act_as.resolve_acting_vendor(req, self.player))
        # ...and returns the Vendor for a god-mode caller.
        self.assertEqual(
            act_as.resolve_acting_vendor(req, self.head).id, self.vendor.id
        )
