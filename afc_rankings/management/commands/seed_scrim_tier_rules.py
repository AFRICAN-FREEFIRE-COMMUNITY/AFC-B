"""
seed_scrim_tier_rules - give the scrims tier rules a starting point on the day the split lands.

WHY A COMMAND AND NOT A MIGRATION
    This repo does not commit migrations (see .gitignore: they are generated on the server), so a
    data migration written here would never reach production - the server's own `makemigrations`
    produces the schema change and nothing else. A management command ships as ordinary code and
    runs as an explicit deploy step.

WHAT IT PREVENTS
    Scrims now classify against their own rule set, and a brand-new set is empty. An empty set
    matches nothing, so without this every live scrim would fall through to the default tier the
    first time it was re-classified - a silent re-tiering caused by a feature that was meant to add
    control, not remove it. Copying the tournament rules across means scrims behave exactly as they
    did the day before, and the owner edits from there.

SAFE TO RUN TWICE
    It refuses a set that already has rules (copy_rule_set does the checking), so re-running it
    after the owner has edited the scrims rules changes nothing and overwrites nothing.

DEPLOY
    python manage.py makemigrations && python manage.py migrate && \
        python manage.py seed_scrim_tier_rules

HOW IT CONNECTS
    afc_rankings.admin_tournament_tiers.copy_rule_set is the one implementation; the admin page's
    empty-state button calls the same function through event-tier-rules/copy-from/.
"""
from django.core.management.base import BaseCommand

from afc_rankings.admin_tournament_tiers import (
    COMPETITIONS,
    DEFAULT_COMPETITION,
    copy_rule_set,
    _get_config,
    _rules_for,
)


class Command(BaseCommand):
    help = "Seed the scrims tier rules from the tournament rules, once, if scrims has none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--target", default="scrims", choices=list(COMPETITIONS),
            help="The set to fill. Default: scrims.")
        parser.add_argument(
            "--source", default=DEFAULT_COMPETITION, choices=list(COMPETITIONS),
            help="The set to copy from. Default: tournament.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Say what would be copied without writing anything.")

    def handle(self, *args, **options):
        source, target = options["source"], options["target"]
        if source == target:
            self.stderr.write("--source and --target must be different sets.")
            return

        existing = _rules_for(target).count()
        if existing:
            self.stdout.write(self.style.WARNING(
                f"{target} already has {existing} rule(s). Nothing copied - this is a one-time "
                f"seed, not a sync."))
            return

        source_count = _rules_for(source).count()
        if options["dry_run"]:
            self.stdout.write(
                f"Would copy {source_count} rule(s) from {source} to {target}, and set {target}'s "
                f"fall-through tier to {_get_config(source).default_tier}.")
            return

        copied = copy_rule_set(source, target)
        self.stdout.write(self.style.SUCCESS(
            f"Copied {copied} rule(s) from {source} to {target}. Fall-through tier: "
            f"{_get_config(target).default_tier}. The two sets are independent from now on."))
