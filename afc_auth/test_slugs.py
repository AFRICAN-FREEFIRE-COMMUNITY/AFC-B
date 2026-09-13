"""
afc_auth/test_slugs.py - the address follows the name, and every old address keeps working.

Owner rule R22 (2026-09-13). Proves afc_auth/slugs.py on the first model that uses it, Product:
the slug is computed from the name, kept unique, follows a rename with the old one kept in
SlugHistory, a narrowed save still carries the new slug, a retired slug is never reissued, and
the public product endpoint answers a legacy id or a retired slug with the product plus
`moved_to` (200, never a 301).
"""
from django.test import Client, TestCase

from afc_auth.models import SlugHistory
from afc_auth.slugs import resolve_or_redirect, unique_slug
from afc_shop.models import Product


def _product(name, **extra):
    return Product.objects.create(name=name, product_type="bundle", **extra)


class SlugSyncTests(TestCase):
    def test_slug_follows_the_name(self):
        p = _product("Weekly Diamonds Pack")
        self.assertEqual(p.slug, "weekly-diamonds-pack")

    def test_same_name_twice_gets_a_suffix_never_a_collision(self):
        a = _product("Starter Bundle")
        b = _product("Starter Bundle")
        self.assertEqual((a.slug, b.slug), ("starter-bundle", "starter-bundle-2"))

    def test_a_digits_only_name_never_looks_like_an_id(self):
        p = _product("2026")
        self.assertEqual(p.slug, "2026-item")
        self.assertFalse(p.slug.isdigit())

    def test_rename_moves_the_address_and_keeps_the_old_one(self):
        p = _product("Old Name")
        p.name = "New Name"
        p.save()
        self.assertEqual(p.slug, "new-name")
        hist = SlugHistory.objects.get(app_label="afc_shop", model="product", old_slug="old-name")
        self.assertEqual(hist.object_pk, str(p.pk))

    def test_a_narrowed_save_still_carries_the_new_slug(self):
        p = _product("First")
        p.name = "Second"
        p.save(update_fields=["name"])  # without the hook adding "slug", the rename would be dropped
        p.refresh_from_db()
        self.assertEqual(p.slug, "second")

    def test_a_save_that_does_not_rename_keeps_the_slug(self):
        p = _product("Stable Name")
        p.description = "changed"
        p.save()
        self.assertEqual(p.slug, "stable-name")
        self.assertEqual(SlugHistory.objects.count(), 0)

    def test_a_retired_slug_is_never_reissued_to_another_product(self):
        p = _product("Taken")
        p.name = "Renamed"
        p.save()
        q = _product("Taken")
        self.assertEqual(q.slug, "taken-2", "the old address still points at the first product")
        self.assertEqual(unique_slug(Product, "Taken"), "taken-3", "taken is retired, taken-2 is the second product")


class ResolveTests(TestCase):
    def test_current_slug_resolves_without_a_move(self):
        p = _product("Gun Skin Crate")
        obj, moved = resolve_or_redirect(Product, "gun-skin-crate")
        self.assertEqual((obj.pk, moved), (p.pk, None))

    def test_legacy_id_resolves_with_a_move_to_the_slug(self):
        p = _product("Gun Skin Crate")
        obj, moved = resolve_or_redirect(Product, str(p.pk))
        self.assertEqual((obj.pk, moved), (p.pk, "gun-skin-crate"))

    def test_a_row_that_predates_slugs_gets_one_on_first_visit(self):
        p = _product("Legacy Row")
        Product.objects.filter(pk=p.pk).update(slug=None)  # as the 9 unslugged production rows are
        obj, moved = resolve_or_redirect(Product, str(p.pk))
        self.assertEqual((obj.pk, moved), (p.pk, "legacy-row"))
        self.assertEqual(Product.objects.get(pk=p.pk).slug, "legacy-row")

    def test_retired_slug_resolves_with_a_move(self):
        p = _product("Before")
        p.name = "After"
        p.save()
        obj, moved = resolve_or_redirect(Product, "before")
        self.assertEqual((obj.pk, moved), (p.pk, "after"))

    def test_nothing_matches(self):
        self.assertEqual(resolve_or_redirect(Product, "no-such-thing"), (None, None))
        self.assertEqual(resolve_or_redirect(Product, "999999"), (None, None))
        self.assertEqual(resolve_or_redirect(Product, ""), (None, None))


class ProductEndpointTests(TestCase):
    def test_detail_by_slug_id_and_retired_slug(self):
        p = _product("Season Pass")
        c = Client()
        r = c.get("/shop/view-product-details/", {"ref": "season-pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["slug"], "season-pass")
        self.assertNotIn("moved_to", r.json())
        # a legacy id link: the product, plus where it lives now
        r = c.get("/shop/view-product-details/", {"product_id": p.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["moved_to"], "/shop/season-pass")
        # a rename: the old address still answers, with the move
        p.name = "Season Pass 2"
        p.save()
        r = c.get("/shop/view-product-details/", {"ref": "season-pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["moved_to"], "/shop/season-pass-2")
        self.assertEqual(r.json()["product"]["id"], p.pk)
        # nothing: 404 with a sentence, not a stack
        r = c.get("/shop/view-product-details/", {"ref": "gone"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["message"], "Product not found.")

    def test_list_carries_the_slug(self):
        _product("Listed Thing")
        r = Client().get("/shop/view-active-products/")
        self.assertEqual(r.status_code, 200, r.content[:200])
        rows = r.json().get("products") or r.json().get("data") or []
        self.assertTrue(any(row.get("slug") == "listed-thing" for row in rows), r.content[:300])
