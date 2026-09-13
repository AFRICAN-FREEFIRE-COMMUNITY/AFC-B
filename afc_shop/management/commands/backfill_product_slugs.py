"""
backfill_product_slugs - give every product an address from its name (owner rule R22, 2026-09-13).

Product.slug existed but was set by hand on 4 of 13 rows; the save() hook now fills it on every
write, and resolve_or_redirect fills it lazily on the first /shop/<id> visit. This command does it
for all rows at once, so the shop list carries slugs from the first deploy. Run once after the
deploy: `python manage.py backfill_product_slugs`. Safe to re-run (a product with a slug derived
from its name is left alone).
"""
from django.core.management.base import BaseCommand

from afc_shop.models import Product


class Command(BaseCommand):
    help = "Fill Product.slug from name for every product that has none (idempotent)."

    def handle(self, *args, **options):
        done = 0
        for product in Product.objects.all().order_by("pk"):
            before = product.slug
            product.save(update_fields=["slug"])
            if product.slug != before:
                done += 1
                self.stdout.write(f"{product.pk}: {before or '(none)'} -> {product.slug}")
        self.stdout.write(f"backfill_product_slugs: {done} changed, {Product.objects.count()} products")
