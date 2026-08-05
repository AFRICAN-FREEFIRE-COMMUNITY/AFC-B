# ── afc_organizers/management/commands/enable_board_chrome.py ────────────────────────────────
# Turn the leaderboard board chrome ON for designs created BEFORE it existed.
#
#   python manage.py enable_board_chrome            # show what would change
#   python manage.py enable_board_chrome --apply    # change it
#
# WHY A COMMAND AND NOT A DATA MIGRATION. Backlog item 2 asked for column headers, grid lines and
# an event/stage header on the DEFAULT exported graphic. Those arrived as three opt-in fields on
# OrgLeaderboardDesign, defaulting FALSE so no design anybody had already tuned silently changed
# shape. New defaults are created with them ON.
#
# That leaves the designs already sitting in production, which are the ones the owner was looking
# at when they reported the graphic as missing its headers. The obvious fix is a data migration,
# and it cannot be used here: this repo gitignores `**/migrations/*.py` and generates migrations on
# the server, so a data migration written locally would never reach production. A management
# command is the same work in a file that actually ships.
#
# SCOPED TO THE AFC DEFAULTS, deliberately. It matches designs named like the ones
# create_default_design produces ("AFC Default (12)", "AFC Default (15)", "AFC Default (24)"). A
# design somebody built and positioned by hand is THEIR layout, and switching grid lines on under
# them is not a fix, it is a surprise. Use --all to include those, having decided you want it.
#
# IDEMPOTENT: a design that already has the three flags on is left alone and reported as skipped,
# so this is safe to run on every deploy.
#
# CONNECTS TO: afc_organizers/models.py OrgLeaderboardDesign (the three fields),
# afc_organizers/views_leaderboard_design.py create_default_design (which sets them on new ones),
# and afc_leaderboard/graphic.py, which reads them at render time.
from django.core.management.base import BaseCommand

from afc_organizers.models import OrgLeaderboardDesign

CHROME_FIELDS = ("show_column_headers", "show_grid", "show_board_header")


class Command(BaseCommand):
    help = "Switch column headers, grid lines and the board header on for AFC default designs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write the change. Without it the command only reports what it would do.")
        parser.add_argument(
            "--all", action="store_true",
            help="Include hand-built designs, not just the AFC defaults. Think first: somebody "
                 "positioned those columns themselves.")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        include_all = options["all"]

        designs = OrgLeaderboardDesign.objects.all()
        if not include_all:
            designs = designs.filter(name__startswith="AFC Default")

        changed = skipped = 0
        for design in designs.order_by("pk"):
            already_on = all(getattr(design, field, False) for field in CHROME_FIELDS)
            if already_on:
                skipped += 1
                continue

            changed += 1
            self.stdout.write(f"  {design.pk}  {design.name}")
            if apply_changes:
                for field in CHROME_FIELDS:
                    setattr(design, field, True)
                design.save(update_fields=list(CHROME_FIELDS))

        scope = "every design" if include_all else "AFC default designs"
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN over {scope}: {changed} would change, {skipped} already on. "
                "Re-run with --apply to write it."))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Board chrome enabled on {changed} of {scope}, {skipped} already on."))
