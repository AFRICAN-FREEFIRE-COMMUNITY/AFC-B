"""
dedupe_discord_role_assignments — one-off data repair for duplicate Discord role rows.

WHY (bug "failed to start", 2026-06-12): DiscordRoleAssignment has no unique constraint
on (user, role_id, stage, group) - MySQL cannot enforce one across the nullable
stage/group columns (NULLs are distinct in MySQL unique indexes) - so years of
bulk_create'd registrations piled up duplicate rows (one stage had 5 copies of a single
assignment) until get_or_create raised MultipleObjectsReturned and 500'd event start.

WHAT IT DOES: for every (user, role_id, stage, group) tuple with more than one row,
keep the BEST row and delete the rest. Best = highest status rank
(success > processing > pending > failed), then the most recently updated. Idempotent:
a second run finds nothing.

GOING FORWARD: the writers route through afc_auth.discord_roles.
queue_discord_role_assignments, which never inserts an existing tuple again - this
command is the repair for what already accumulated.

RUN:  python manage.py dedupe_discord_role_assignments [--dry-run]
PROD: run once at the next deploy, before anything else touches role assignments.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from afc_auth.models import DiscordRoleAssignment

# Higher = better to keep. A success row must survive over a pending twin so the
# reconcile endpoints keep skipping already-granted roles.
_STATUS_RANK = {"success": 3, "processing": 2, "pending": 1, "failed": 0}


class Command(BaseCommand):
    help = "Delete duplicate DiscordRoleAssignment rows, keeping the best row per (user, role_id, stage, group)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be deleted without touching anything.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]

        dup_tuples = (
            DiscordRoleAssignment.objects
            .values("user_id", "role_id", "stage_id", "group_id")
            .annotate(c=Count("id"))
            .filter(c__gt=1)
        )
        total_tuples, total_deleted = 0, 0

        for t in dup_tuples:
            rows = list(DiscordRoleAssignment.objects.filter(
                user_id=t["user_id"], role_id=t["role_id"],
                stage_id=t["stage_id"], group_id=t["group_id"],
            ))
            # Keep the best: status rank first, then the freshest update wins.
            rows.sort(
                key=lambda r: (_STATUS_RANK.get(r.status, -1), r.updated_at or r.created_at),
                reverse=True,
            )
            keep, drop = rows[0], rows[1:]
            total_tuples += 1
            total_deleted += len(drop)
            if dry:
                self.stdout.write(
                    f"would keep #{keep.id} ({keep.status}) and delete "
                    f"{[r.id for r in drop]} for user={t['user_id']} role={t['role_id']} "
                    f"stage={t['stage_id']} group={t['group_id']}"
                )
            else:
                DiscordRoleAssignment.objects.filter(id__in=[r.id for r in drop]).delete()

        verb = "Would delete" if dry else "Deleted"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {total_deleted} duplicate row(s) across {total_tuples} tuple(s)."
        ))
