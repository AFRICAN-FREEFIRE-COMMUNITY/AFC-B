"""
Backfill: force every ORGANIZER event to event_type="internal".

WHY THIS EXISTS (owner 2026-06-21): the organizer create wizard used to default
`event_type="external"` (a backward default). "external" means registration happens
off-platform via a `registration_link`, and the user-facing event page renders a
green "Register (External Link)" button for it. Organizers must NEVER have an
external-events option, so organizer events were wrongly surfacing that button.

The create/edit_event views now force org events to "internal" going forward, but
events already created under the old default still carry event_type="external" in
the DB. This command fixes those existing rows.

An organizer event = one whose `organization` FK is set (organization_id NOT NULL).
Native AFC-admin events (organization IS NULL) are left untouched: only THEY may be
legitimately external.

SAFE BY DEFAULT: dry-run unless --apply is passed. Run it, READ the report, then
re-run with --apply.

USAGE (prod, inside the backend venv):
    python manage.py fix_organizer_event_types            # dry-run, report only
    python manage.py fix_organizer_event_types --apply    # flip them to internal

Read by: ops. Touches afc_tournament_and_scrims.Event.event_type only. Pairs with
the create_event/edit_event change that forces `event_type="internal" if org`.
"""
from django.core.management.base import BaseCommand

from afc_tournament_and_scrims.models import Event


class Command(BaseCommand):
    help = "Force all organizer (org-owned) events to event_type='internal'."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the change. Without this flag the command only reports (dry-run).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        # Org events still marked external = the rows to fix. organization_id NOT NULL
        # is what makes an event an organizer event (vs a native AFC-admin event).
        qs = Event.objects.filter(organization__isnull=False).exclude(event_type="internal")
        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to fix: all organizer events are already internal."))
            return

        self.stdout.write(f"Found {total} organizer event(s) not marked internal:")
        for ev in qs.values("event_id", "event_name", "event_type", "organization_id")[:200]:
            self.stdout.write(
                f"  - #{ev['event_id']} \"{ev['event_name']}\" "
                f"(org {ev['organization_id']}, event_type={ev['event_type']}) -> internal"
            )

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY-RUN. Re-run with --apply to persist."))
            return

        updated = qs.update(event_type="internal")
        self.stdout.write(self.style.SUCCESS(f"\nDone. Set event_type='internal' on {updated} organizer event(s)."))
