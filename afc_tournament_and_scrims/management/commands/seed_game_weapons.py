"""python manage.py seed_game_weapons [--dry-run]

Fills the kill feed's GameWeapon table (inbox #350, the sniper award) from kill_feed.KNOWN_WEAPONS: the weapon ids
proven to match the debugger log (WEBSITE/tasks/kill-feed-weapon-ids-2026-10-24/). Idempotent, and it never
overwrites a row an admin already has: a weapon that exists keeps its name and its "is sniper" tick. Run once after
the deploy that creates the table; the kill-stats view uses the same defaults until then.
"""
from django.core.management.base import BaseCommand

from afc_tournament_and_scrims.kill_feed import KNOWN_WEAPONS
from afc_tournament_and_scrims.models import GameWeapon


class Command(BaseCommand):
    help = "Fill GameWeapon from kill_feed.KNOWN_WEAPONS without overwriting admin edits."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Say what would be added, add nothing.")

    def handle(self, *args, **options):
        have = set(GameWeapon.objects.values_list("weapon_id", flat=True))
        new = [(wid, name, sniper) for wid, (name, sniper) in sorted(KNOWN_WEAPONS.items()) if wid not in have]
        for wid, name, sniper in new:
            self.stdout.write(f"{'would add' if options['dry_run'] else 'add'} {wid} {name} sniper={sniper}")
        if not options["dry_run"]:
            GameWeapon.objects.bulk_create(
                [GameWeapon(weapon_id=wid, name=name, is_sniper=sniper) for wid, name, sniper in new])
        self.stdout.write(f"{len(new)} new, {len(have)} kept as they were")
