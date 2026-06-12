"""
migrate_legacy_sponsors — bridge the LEGACY sponsor accounts into the new entity system.

WHAT IT DOES (idempotent, additive only - safe to re-run)
    For every legacy sponsor USER (role=admin + the sponsor_admin granular role, the old
    "a sponsor is a user account" model):
      1. Ensure a Sponsor ENTITY exists for them. Name = the account's full_name (or username);
         an admin renames it to the real brand (e.g. "ydpay") on /a/sponsors afterwards.
      2. Ensure the user is an ACTIVE owner-member of that entity (so the scoped dashboard
         replaces their legacy one on next load; the data shown is identical).
      3. Attach every event from their legacy SponsorEvent rows via EventSponsorship.

    NOTHING is deleted or modified on the legacy side - the old SponsorEvent rows and the
    legacy dashboard keep working until the P2 cutover removes them.

USAGE
    python manage.py migrate_legacy_sponsors            # do it
    python manage.py migrate_legacy_sponsors --dry-run  # print the plan only

HOW IT CONNECTS: afc_sponsors.models (the new entities), afc_auth (User/Roles),
afc_tournament_and_scrims.SponsorEvent (the legacy links). Part of the sponsor-system
redesign P1 deploy steps (spec: WEBSITE/tasks/sponsors-redesign-design.md).
"""
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from afc_auth.models import User
from afc_tournament_and_scrims.models import SponsorEvent
from afc_sponsors.models import Sponsor, SponsorMember, EventSponsorship


class Command(BaseCommand):
    help = "Create Sponsor entities + memberships + event attachments from the legacy sponsor accounts."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        legacy_users = User.objects.filter(
            role="admin", userroles__role__role_name="sponsor_admin",
        ).distinct()

        if not legacy_users.exists():
            self.stdout.write("No legacy sponsor accounts found. Nothing to do.")
            return

        created_sponsors = linked_members = attached_events = 0
        for u in legacy_users:
            name = (u.full_name or u.username).strip()
            sponsor = Sponsor.objects.filter(members__user=u).first() or Sponsor.objects.filter(
                name__iexact=name,
            ).first()

            if sponsor is None:
                base = slugify(name) or f"sponsor-{u.user_id}"
                slug = base
                n = 2
                while Sponsor.objects.filter(slug=slug).exists():
                    slug = f"{base}-{n}"
                    n += 1
                self.stdout.write(f"+ Sponsor entity '{name}' (slug {slug}) for @{u.username}")
                if not dry:
                    sponsor = Sponsor.objects.create(name=name, slug=slug, created_by=u)
                created_sponsors += 1

            if sponsor is not None or not dry:
                member_exists = sponsor and SponsorMember.objects.filter(
                    sponsor=sponsor, user=u, status="active",
                ).exists()
                if not member_exists:
                    self.stdout.write(f"  + member @{u.username} (owner)")
                    if not dry and sponsor:
                        SponsorMember.objects.update_or_create(
                            sponsor=sponsor, user=u, defaults={"role": "owner", "status": "active"},
                        )
                    linked_members += 1

            for se in SponsorEvent.objects.filter(sponsor=u).select_related("event"):
                already = sponsor and EventSponsorship.objects.filter(
                    sponsor=sponsor, event=se.event,
                ).exists()
                if not already:
                    self.stdout.write(f"  + attach event '{se.event.event_name}'")
                    if not dry and sponsor:
                        EventSponsorship.objects.get_or_create(sponsor=sponsor, event=se.event)
                    attached_events += 1

        verb = "Would create" if dry else "Created"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {created_sponsors} sponsor(s), {linked_members} membership(s), "
            f"{attached_events} event attachment(s). Legacy data untouched."
        ))
        if not dry and created_sponsors:
            self.stdout.write("Rename the auto-named entities to their real brands on /a/sponsors.")
