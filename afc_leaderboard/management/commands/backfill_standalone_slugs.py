"""
backfill_standalone_slugs - give every standalone leaderboard an address from its name (owner
rule R22, 2026-09-13).

StandaloneLeaderboard.slug is filled by save() on every write and lazily by
afc_auth.slugs.resolve_or_redirect on the first legacy-id visit. This command fills it for every
row at once, so the admin and organizer lists carry slugs from the first deploy. Run once after
the deploy: `python manage.py backfill_standalone_slugs`. Safe to re-run (a leaderboard whose
slug derives from its name is left alone).
"""
from django.core.management.base import BaseCommand

from afc_leaderboard.models import StandaloneLeaderboard


class Command(BaseCommand):
    help = "Fill StandaloneLeaderboard.slug from name for every leaderboard that has none (idempotent)."

    def handle(self, *args, **options):
        done = 0
        for lb in StandaloneLeaderboard.objects.all().order_by("pk"):
            before = lb.slug
            lb.save(update_fields=["slug"])
            if lb.slug != before:
                done += 1
                self.stdout.write(f"{lb.pk}: {before or '(none)'} -> {lb.slug}")
        self.stdout.write(f"backfill_standalone_slugs: {done} changed, {StandaloneLeaderboard.objects.count()} leaderboards")
