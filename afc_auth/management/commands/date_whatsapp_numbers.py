# afc_auth/management/commands/date_whatsapp_numbers.py
# ──────────────────────────────────────────────────────────────────────────────
# One-off backfill: date the WhatsApp numbers that were saved before we started
# recording when they were typed (owner 2026-08-08).
#
# WHY THIS IS A COMMAND AND NOT JUST THE MIGRATION
#   Migration 0039 does carry this backfill, and it runs correctly on a local
#   database. It will never run on the server: **backend/.gitignore excludes
#   `**/migrations/*.py`** (line 29) because migrations are generated ON the server
#   with `makemigrations`, so the file that reaches production contains only the
#   AddField that `makemigrations` regenerates. The RunPython step, and the whole
#   argument in the migration header, would be silently dropped.
#
#   So the backfill ships here instead, where it is a committed file, and the
#   deploy runs it once after `migrate`.
#
# WHAT IT DOES
#   Sets UserProfile.whatsapp_number_updated_at = now for every profile that HOLDS
#   a number but has no date on it. That timestamp is not the truth (we have no
#   record of when those numbers were typed) and does not pretend to be: it is a
#   decision about which way to be wrong.
#
#     leave them NULL -> afc_auth/views_recovery._number_too_stale treats NULL as
#       FRESH, so the ~119 accounts that already have a number would simply never
#       expire as a recovery factor. The guard would be permanently inert for
#       exactly the rows most likely to be stale.
#     stamp them now -> a number that was ALREADY stale on 2026-08-08 keeps working
#       as a recovery factor for one more year, then stops. That is a real hole and
#       it is bounded, stated, and shrinks every time somebody re-saves a number.
#
#   The second is chosen because it is the only one where the guard ever does
#   anything for existing users. See views_recovery.RECOVERY_NUMBER_MAX_AGE.
#
# IDEMPOTENT: it only touches rows where the date is NULL, so running it twice is
# harmless and running it months later only catches rows added by some path that
# forgot to stamp (signup and edit_profile both stamp already).
#
# SAFE BY DEFAULT: dry-run unless --apply, matching clean_name_whitespace and every
# other backfill command in this directory.
#
# Usage:
#   python manage.py date_whatsapp_numbers            # dry-run (report only)
#   python manage.py date_whatsapp_numbers --apply    # write the dates
# ──────────────────────────────────────────────────────────────────────────────
from django.core.management.base import BaseCommand
from django.utils import timezone

from afc_auth.models import UserProfile


class Command(BaseCommand):
    help = (
        "Date WhatsApp numbers saved before we recorded when they were typed, so the "
        "recovery staleness guard has a clock to measure from (dry-run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the dates. Without this the command only reports.",
        )

    def handle(self, *args, **options):
        # Rows that hold a number but carry no date. `exclude(whatsapp_number="")` is the
        # same filter the migration uses; a profile with no number needs no date, because
        # it cannot use WhatsApp recovery at all until one is typed (and typing one stamps
        # it through views.signup / views.edit_profile).
        undated = UserProfile.objects.exclude(whatsapp_number="").filter(
            whatsapp_number_updated_at__isnull=True
        )
        count = undated.count()

        total_with_number = UserProfile.objects.exclude(whatsapp_number="").count()
        self.stdout.write(
            f"Profiles holding a WhatsApp number: {total_with_number}\n"
            f"Of those, undated (would be stamped): {count}"
        )

        if not count:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run. Re-run with --apply to write the dates."
            ))
            return

        # .update() rather than save() per row: no signals to fire, one statement, and
        # nothing here needs model-level logic.
        stamped = undated.update(whatsapp_number_updated_at=timezone.now())
        self.stdout.write(self.style.SUCCESS(
            f"Dated {stamped} number(s). They now count as a recovery factor for "
            f"RECOVERY_NUMBER_MAX_AGE from today."
        ))
