"""
afc_shop/test_delivery_wishlist.py
================================================================================
Tests for the 2026-06-29 shop batch:
  • Saved delivery profiles (afc_shop/delivery.py) — owner-scoped CRUD, default
    uniqueness, per-user cap, the checkout save helpers, AND the SUPER-ADMIN-ONLY
    delivery-PII view (require_head_admin: head_admin/super_admin pass; shop_admin
    and players are 403; the list masks email/phone, the reveal does not).
  • Wishlist (afc_shop/wishlist.py) — toggle add/remove, owner-scoped list + ids.

Drives the real HTTP endpoints with a Bearer SessionToken (like the frontend). No
network is touched.

Run: python manage.py test afc_shop.test_delivery_wishlist
"""
from django.test import TestCase, Client

from afc_auth.models import User, Roles, UserRoles, SessionToken
from afc_shop.models import (
    Product, ProductVariant, Order, SavedDeliveryProfile, Wishlist,
)
from afc_shop import delivery


def _profile_payload(**over):
    base = {
        "first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.com",
        "phone_number": "08012345678", "address": "1 Algorithm Way", "city": "Lagos",
        "state": "Lagos", "postcode": "100001",
    }
    base.update(over)
    return base


class _Base(TestCase):
    def setUp(self):
        self.client = Client()
        self.r_head, _ = Roles.objects.get_or_create(role_name="head_admin")
        self.r_super, _ = Roles.objects.get_or_create(role_name="super_admin")
        self.r_shop, _ = Roles.objects.get_or_create(role_name="shop_admin")

        self.user = self._user("buyer")
        self.user_tok = self._token(self.user)
        self.other = self._user("other")
        self.other_tok = self._token(self.other)

        self.head = self._user("head", role="admin")
        UserRoles.objects.create(user=self.head, role=self.r_head)
        self.head_tok = self._token(self.head)

        self.shopadmin = self._user("shopadmin", role="admin")
        UserRoles.objects.create(user=self.shopadmin, role=self.r_shop)
        self.shopadmin_tok = self._token(self.shopadmin)

        # A product for the wishlist tests.
        self.product = Product.objects.create(
            name="Gaming Headset", product_type="bundle", status="active",
        )
        ProductVariant.objects.create(product=self.product, sku="gh-1", price="50.00")

    def _user(self, name, role="player"):
        return User.objects.create(
            username=name, email=f"{name}@x.com", full_name=name.title(),
            role=role, password="x",
        )

    def _token(self, user):
        return SessionToken.objects.create(user=user, token=f"tok_{user.username}").token

    def _auth(self, tok):
        return {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


# ──────────────────────────────────────────────────────────────────────────────
# Saved delivery profiles — owner CRUD
# ──────────────────────────────────────────────────────────────────────────────
class DeliveryProfileCrudTests(_Base):
    def test_first_profile_becomes_default(self):
        resp = self.client.post(
            "/shop/delivery-profiles/create/", _profile_payload(),
            content_type="application/json", **self._auth(self.user_tok),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["profile"]["is_default"])

    def test_second_profile_not_default_unless_requested(self):
        self.client.post("/shop/delivery-profiles/create/", _profile_payload(),
                         content_type="application/json", **self._auth(self.user_tok))
        resp = self.client.post(
            "/shop/delivery-profiles/create/", _profile_payload(label="Office", address="2 Office Rd"),
            content_type="application/json", **self._auth(self.user_tok),
        )
        self.assertFalse(resp.json()["profile"]["is_default"])

    def test_set_default_moves_the_flag(self):
        p1 = SavedDeliveryProfile.objects.create(user=self.user, is_default=True, **_profile_payload())
        p2 = SavedDeliveryProfile.objects.create(user=self.user, **_profile_payload(address="2 Rd"))
        resp = self.client.post(
            "/shop/delivery-profiles/set-default/", {"profile_id": p2.id},
            content_type="application/json", **self._auth(self.user_tok),
        )
        self.assertEqual(resp.status_code, 200)
        p1.refresh_from_db(); p2.refresh_from_db()
        self.assertFalse(p1.is_default)
        self.assertTrue(p2.is_default)

    def test_missing_required_field_is_400(self):
        resp = self.client.post(
            "/shop/delivery-profiles/create/", _profile_payload(email=""),
            content_type="application/json", **self._auth(self.user_tok),
        )
        self.assertEqual(resp.status_code, 400)

    def test_cannot_touch_another_users_profile(self):
        p = SavedDeliveryProfile.objects.create(user=self.other, **_profile_payload())
        resp = self.client.post(
            "/shop/delivery-profiles/update/", {"profile_id": p.id, "city": "Abuja"},
            content_type="application/json", **self._auth(self.user_tok),
        )
        self.assertEqual(resp.status_code, 404)

    def test_per_user_cap(self):
        for i in range(delivery.MAX_PROFILES_PER_USER):
            SavedDeliveryProfile.objects.create(user=self.user, **_profile_payload(address=f"{i} Rd"))
        resp = self.client.post(
            "/shop/delivery-profiles/create/", _profile_payload(address="overflow Rd"),
            content_type="application/json", **self._auth(self.user_tok),
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_promotes_next_default(self):
        p1 = SavedDeliveryProfile.objects.create(user=self.user, is_default=True, **_profile_payload())
        p2 = SavedDeliveryProfile.objects.create(user=self.user, **_profile_payload(address="2 Rd"))
        resp = self.client.post(
            "/shop/delivery-profiles/delete/", {"profile_id": p1.id},
            content_type="application/json", **self._auth(self.user_tok),
        )
        self.assertEqual(resp.status_code, 200)
        p2.refresh_from_db()
        self.assertTrue(p2.is_default)

    def test_list_returns_only_my_profiles(self):
        SavedDeliveryProfile.objects.create(user=self.user, **_profile_payload())
        SavedDeliveryProfile.objects.create(user=self.other, **_profile_payload())
        resp = self.client.get("/shop/delivery-profiles/", **self._auth(self.user_tok))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["profiles"]), 1)


# ──────────────────────────────────────────────────────────────────────────────
# Checkout save helpers
# ──────────────────────────────────────────────────────────────────────────────
class CheckoutSaveHelperTests(_Base):
    def test_persist_creates_profile_and_dedupes(self):
        p1 = delivery.persist_delivery_profile(self.user, _profile_payload())
        self.assertIsNotNone(p1)
        # Same address + phone -> reuse, no duplicate.
        p2 = delivery.persist_delivery_profile(self.user, _profile_payload())
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(SavedDeliveryProfile.objects.filter(user=self.user).count(), 1)

    def test_persist_incomplete_returns_none(self):
        self.assertIsNone(delivery.persist_delivery_profile(self.user, _profile_payload(address="")))

    def test_attach_links_saved_profile_id(self):
        p = SavedDeliveryProfile.objects.create(user=self.user, **_profile_payload())
        order = Order.objects.create(user=self.user, total="10.00")
        delivery.attach_delivery_profile(order, self.user, {"saved_profile_id": p.id})
        self.assertEqual(order.saved_profile_id, p.id)

    def test_attach_save_flag_creates_and_links(self):
        order = Order.objects.create(user=self.user, total="10.00")
        delivery.attach_delivery_profile(order, self.user, {**_profile_payload(), "save_delivery_info": True})
        self.assertIsNotNone(order.saved_profile_id)


# ──────────────────────────────────────────────────────────────────────────────
# Super-admin delivery-PII view
# ──────────────────────────────────────────────────────────────────────────────
class AdminDeliveryInfoTests(_Base):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            user=self.user, total="99.00", status="paid",
            first_name="John", last_name="Doe", email="john@example.com",
            phone_number="08099998888", address="5 Secret St", city="Lagos", state="Lagos",
        )

    def test_head_admin_can_list_masked(self):
        resp = self.client.post("/shop/admin/delivery-info/", {},
                                content_type="application/json", **self._auth(self.head_tok))
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.json()["results"] if r["order_id"] == self.order.id)
        self.assertEqual(row["email"], "j***@example.com")   # masked
        self.assertTrue(row["phone_number"].startswith("***"))
        self.assertNotIn("address", row)                      # street not in the list

    def test_shop_admin_is_forbidden(self):
        resp = self.client.post("/shop/admin/delivery-info/", {},
                                content_type="application/json", **self._auth(self.shopadmin_tok))
        self.assertEqual(resp.status_code, 403)

    def test_player_is_forbidden(self):
        resp = self.client.post("/shop/admin/delivery-info/", {},
                                content_type="application/json", **self._auth(self.user_tok))
        self.assertEqual(resp.status_code, 403)

    def test_reveal_returns_full_pii_for_head_admin(self):
        resp = self.client.post("/shop/admin/delivery-info/reveal/", {"order_id": self.order.id},
                                content_type="application/json", **self._auth(self.head_tok))
        self.assertEqual(resp.status_code, 200)
        rec = resp.json()["record"]
        self.assertEqual(rec["email"], "john@example.com")    # unmasked
        self.assertEqual(rec["address"], "5 Secret St")

    def test_reveal_forbidden_for_player(self):
        resp = self.client.post("/shop/admin/delivery-info/reveal/", {"order_id": self.order.id},
                                content_type="application/json", **self._auth(self.user_tok))
        self.assertEqual(resp.status_code, 403)

    def test_search_filters_rows(self):
        resp = self.client.post("/shop/admin/delivery-info/", {"q": "nonexistentxyz"},
                                content_type="application/json", **self._auth(self.head_tok))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total_count"], 0)


# ──────────────────────────────────────────────────────────────────────────────
# Wishlist
# ──────────────────────────────────────────────────────────────────────────────
class WishlistTests(_Base):
    def test_toggle_adds_then_removes(self):
        add = self.client.post("/shop/wishlist/toggle/", {"product_id": self.product.id},
                               content_type="application/json", **self._auth(self.user_tok))
        self.assertEqual(add.status_code, 201)
        self.assertTrue(add.json()["saved"])
        self.assertTrue(Wishlist.objects.filter(user=self.user, product=self.product).exists())

        rem = self.client.post("/shop/wishlist/toggle/", {"product_id": self.product.id},
                               content_type="application/json", **self._auth(self.user_tok))
        self.assertEqual(rem.status_code, 200)
        self.assertFalse(rem.json()["saved"])
        self.assertFalse(Wishlist.objects.filter(user=self.user, product=self.product).exists())

    def test_list_and_ids_are_owner_scoped(self):
        Wishlist.objects.create(user=self.user, product=self.product)
        # other user's wishlist must not leak
        listed = self.client.get("/shop/wishlist/", **self._auth(self.other_tok))
        self.assertEqual(listed.json()["count"], 0)

        mine = self.client.get("/shop/wishlist/", **self._auth(self.user_tok))
        self.assertEqual(mine.json()["count"], 1)
        self.assertEqual(mine.json()["products"][0]["name"], "Gaming Headset")

        ids = self.client.get("/shop/wishlist/ids/", **self._auth(self.user_tok))
        self.assertEqual(ids.json()["product_ids"], [self.product.id])

    def test_toggle_requires_auth(self):
        resp = self.client.post("/shop/wishlist/toggle/", {"product_id": self.product.id},
                                content_type="application/json")
        self.assertIn(resp.status_code, (400, 401))

    def test_toggle_unknown_product_404(self):
        resp = self.client.post("/shop/wishlist/toggle/", {"product_id": 999999},
                                content_type="application/json", **self._auth(self.user_tok))
        self.assertEqual(resp.status_code, 404)
