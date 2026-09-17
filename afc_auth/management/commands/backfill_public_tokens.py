"""
backfill_public_tokens - give every order and every market application its opaque public address
(owner rule R22, 2026-09-13: no numeric id in a URL a person can see).

Order.public_token and RecruitmentApplication.public_token are filled by save() on every write and
lazily by afc_auth.slugs.resolve_by_token on the first legacy-id visit. This command fills them for
every existing row at once, so the orders list and the applications lists carry token addresses
from the first deploy instead of falling back to ids. Run once after the deploy:
`python manage.py backfill_public_tokens`. Safe to re-run (a row with a token is left alone).

HOW IT CONNECTS
  - afc_auth/slugs.py ensure_public_token mints the token; the models' save() call it.
  - afc_shop/views.py get_my_orders / get_order_details and afc_player_market/views.py
    view_my_applications / view_applications / view_all_trials_and_applications /
    view_application_details read it back.
"""
from django.core.management.base import BaseCommand

from afc_player_market.models import RecruitmentApplication
from afc_shop.models import Order


class Command(BaseCommand):
    help = "Mint a public token for every order and market application that has none (idempotent)."

    def handle(self, *args, **options):
        for model, label in ((Order, "orders"), (RecruitmentApplication, "applications")):
            rows = model.objects.filter(public_token__isnull=True).order_by("pk")
            done = 0
            for row in rows:
                row.save(update_fields=["public_token"])
                done += 1
            self.stdout.write(f"backfill_public_tokens: {done} {label} minted, {model.objects.count()} total")
