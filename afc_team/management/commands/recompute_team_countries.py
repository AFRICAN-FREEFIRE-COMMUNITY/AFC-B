# One-off backfill: re-derive Team.country for every team from its players' locations (owner 2026-06-20).
# Run once after deploying the auto-country feature so existing teams (whose country was set manually at
# creation) match the new rule. Idempotent + safe to re-run. After this, the TeamMembers signal
# (afc_team/signals.py) + recompute_team_country() keep each team's country live on every roster change.
#
# Usage:
#   python manage.py recompute_team_countries            # apply
#   python manage.py recompute_team_countries --dry-run  # preview only
from django.core.management.base import BaseCommand

from afc_team.models import Team
from afc_team.views import _derive_team_country


class Command(BaseCommand):
    help = "Re-derive every team's country from its players' locations (auto-country backfill)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would change without saving.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        changed = 0
        for team in Team.objects.select_related("team_owner").all():
            old = team.country or ""
            new = (_derive_team_country(team) or "")[:64]
            if new != old:
                changed += 1
                self.stdout.write(f"  #{team.team_id} {team.team_name}: {old!r} -> {new!r}")
                if not dry:
                    team.country = new
                    team.save(update_fields=["country"])
        verb = "Would change" if dry else "Changed"
        self.stdout.write(self.style.SUCCESS(f"{verb} {changed} team(s)."))
