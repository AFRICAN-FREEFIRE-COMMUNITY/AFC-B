"""python manage.py seed_game_weapons [--dry-run]

Two jobs, both idempotent, run once after a deploy that changes the weapon table:

1. Keeps the kill feed's GameWeapon table (inbox #350, the sniper award; #468, every gun by name; #474, the names
   the game shows its players) in step with kill_feed.KNOWN_WEAPONS: every id of weapon_names.WEAPON_NAMES
   (generated from the client's own English text by afc-capture/dev/build_weapons.py) with its sniper tick
   (the owner's set: AWM, M82B, Kar98k, M24 and every version of them).
   - a missing id is added;
   - a row whose name a machine wrote (the current name, a retired one such as "EVO3" or "BLT", or the unknown
     placeholder; weapons.is_machine_name) takes the current name, or the unknown placeholder when the game's
     text no longer names the id;
   - its sniper tick follows the table only while it is still the tick the seed itself wrote before 3 Nov 2026
     (SEEDED_SNIPER_IDS): a tick an admin changed is theirs;
   - a row an admin named is never touched.
2. Names the most-used weapon stored on player rows: a bare id ("9"), "unknown gun (id N)" or a name an older table
   wrote ("EVO3", "BLT": weapons.RETIRED_IDS) becomes the gun's name through weapons.weapon_label, so a gun an
   admin names in /admin/ later reaches old rows on the next run. Any other name is left alone.

The kill-stats view uses the same defaults while the table is still empty.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from afc_tournament_and_scrims.kill_feed import KNOWN_WEAPONS
from afc_tournament_and_scrims.models import GameWeapon, TournamentPlayerMatchStats
from afc_tournament_and_scrims.weapons import (RETIRED_IDS, UNKNOWN, admin_names, is_machine_name, weapon_id_of,
                                               weapon_label)

# The ids the seed ticked as snipers before the names came from the game (kill_feed.KNOWN_WEAPONS until 3 Nov
# 2026). A machine-named row still carrying exactly that tick was never changed by an admin, so it follows the
# table now; any other tick is an admin's and stays.
SEEDED_SNIPER_IDS = frozenset({4, 172, 65, 21, 64, 128, 129, 175, 75})


class Command(BaseCommand):
    help = "Keep GameWeapon in step with the game's own weapon names, and name stored most-used weapons."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Say what would change, change nothing.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        verb = "would " if dry else ""

        # ── 1. the weapon table ──
        have = {w.weapon_id: w for w in GameWeapon.objects.all()}
        new = [GameWeapon(weapon_id=wid, name=name, is_sniper=sniper)
               for wid, (name, sniper) in sorted(KNOWN_WEAPONS.items()) if wid not in have]
        for w in new:
            self.stdout.write(f"{verb}add {w.weapon_id} {w.name} sniper={w.is_sniper}")
        changed = []
        for wid, row in sorted(have.items()):
            if not is_machine_name(wid, row.name):
                continue                                    # an admin's name: theirs
            name, sniper = KNOWN_WEAPONS.get(wid, (UNKNOWN.format(wid), False))
            tick = sniper if row.is_sniper == (wid in SEEDED_SNIPER_IDS) else row.is_sniper
            if (row.name, row.is_sniper) != (name, tick):
                self.stdout.write(f"{verb}update {wid} {row.name!r} sniper={row.is_sniper} -> {name!r} sniper={tick}")
                row.name, row.is_sniper = name, tick
                changed.append(row)
        if not dry:
            GameWeapon.objects.bulk_create(new)
            GameWeapon.objects.bulk_update(changed, ["name", "is_sniper"])
        self.stdout.write(f"{len(new)} new, {len(changed)} updated, {len(have) - len(changed)} kept as they were")

        # ── 2. stored most-used weapons that are still a code or an old machine name ──
        names = admin_names()
        coded = (TournamentPlayerMatchStats.objects
                 .filter(Q(most_used_weapon__regex=r"^[0-9]+$") | Q(most_used_weapon__startswith="unknown gun (id ")
                         | Q(most_used_weapon__in=list(RETIRED_IDS)))
                 .values_list("most_used_weapon", flat=True).distinct())
        renamed = 0
        for value in sorted(set(coded)):    # one UPDATE per distinct value, not one per row
            if weapon_id_of(value) is None:
                continue
            label = weapon_label(value, names)
            if label == value:
                continue                    # still unknown, nothing new to say
            same = TournamentPlayerMatchStats.objects.filter(most_used_weapon=value)
            count = same.count() if dry else same.update(most_used_weapon=label)
            renamed += count
            self.stdout.write(f"{verb}name {value!r} -> {label!r} on {count} rows")
        self.stdout.write(f"{renamed} stored most-used weapons {'would be ' if dry else ''}named")
