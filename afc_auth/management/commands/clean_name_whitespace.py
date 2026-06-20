# afc_auth/management/commands/clean_name_whitespace.py
# ──────────────────────────────────────────────────────────────────────────────
# One-off backfill: strip leading/trailing whitespace from name fields (owner 2026-06-20).
#
# WHY: seed data carried stray whitespace in names (e.g. team 'FROZEN EMPIRE ',
# ~41% of teams + ~21% of users). That breaks name-based lookups: SQL `=` ignores
# only TRAILING spaces (MySQL PADSPACE), and `__iexact`/LIKE ignores neither. The
# models now strip on save() (going forward); this command cleans the EXISTING rows.
#
# SAFE BY DEFAULT: dry-run unless --apply is passed. For UNIQUE fields (username,
# uid, team_name) it pre-checks for collisions (where trimming would make two rows
# identical) and SKIPS + reports those rather than crashing the unique constraint -
# they need a manual decision. Uses .update() (no save()/signals) per row.
#
# Usage:
#   python manage.py clean_name_whitespace            # dry-run (report only)
#   python manage.py clean_name_whitespace --apply    # write the trims
# ──────────────────────────────────────────────────────────────────────────────
from django.core.management.base import BaseCommand
from django.db import transaction

from afc_auth.models import User
from afc_team.models import Team


# (Model, field, unique?) - the user/team name fields that feed name-based lookups.
TARGETS = [
    (User, "username", True),
    (User, "full_name", False),
    (User, "uid", True),
    (Team, "team_name", True),
    (Team, "team_tag", False),
]


class Command(BaseCommand):
    help = "Strip leading/trailing whitespace from user/team name fields (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write the trims. Without this flag the command only reports.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        mode = "APPLY" if apply else "DRY-RUN"
        self.stdout.write(self.style.WARNING(f"clean_name_whitespace [{mode}]"))

        grand_cleaned = grand_collisions = 0

        for model, field, unique in TARGETS:
            # Rows whose value differs from its stripped form (i.e. has stray whitespace).
            dirty = []
            for pk, val in model.objects.values_list("pk", field):
                if isinstance(val, str) and val != val.strip():
                    dirty.append((pk, val, val.strip()))

            cleaned = 0
            collisions = []
            to_update = []  # (pk, stripped) safe to write

            for pk, val, stripped in dirty:
                # SAFETY: never blank a field. If a value is pure whitespace (strips to
                # empty), leave it untouched - we only ever remove surrounding spaces from
                # a non-empty name, never erase content.
                if stripped == "":
                    collisions.append((pk, val, "all-whitespace, left untouched"))
                    continue
                if unique:
                    # Collision: another row already holds (or, processed earlier here,
                    # will hold) the clean value.
                    clash = (
                        model.objects.filter(**{field: stripped})
                        .exclude(pk=pk)
                        .exists()
                        or any(s == stripped for (_p, s) in [(p, s) for (p, s) in to_update])
                    )
                    if clash:
                        collisions.append((pk, val, f"clashes with existing '{stripped}'"))
                        continue
                to_update.append((pk, stripped))

            if apply:
                with transaction.atomic():
                    for pk, stripped in to_update:
                        model.objects.filter(pk=pk).update(**{field: stripped})
                cleaned = len(to_update)
            else:
                cleaned = len(to_update)

            grand_cleaned += cleaned
            grand_collisions += len(collisions)

            label = f"{model.__name__}.{field}"
            self.stdout.write(
                f"  {label:24s} dirty={len(dirty):4d}  "
                f"{'cleaned' if apply else 'would-clean'}={cleaned:4d}  collisions={len(collisions)}"
            )
            # Show up to 5 collision examples so they can be handled manually.
            for pk, val, why in collisions[:5]:
                self.stdout.write(self.style.NOTICE(f"      collision pk={pk} {val!r} -> {why}"))

        verb = "Cleaned" if apply else "Would clean"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {grand_cleaned} field value(s); {grand_collisions} collision(s) left for manual review."
        ))
        if not apply:
            self.stdout.write("Re-run with --apply to write the changes.")
