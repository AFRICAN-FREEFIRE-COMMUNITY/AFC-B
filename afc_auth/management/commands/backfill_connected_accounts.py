"""
Copy every live Discord link on afc_auth.User into the ConnectedAccount table.

WHY: Discord predates the connected-accounts layer and lives in four columns on User. Those columns
remain authoritative for existing readers (check_discord_membership*, DiscordRoleAssignment,
roster_discord.py, the AFC bot), and this command gives the new table the same picture so the
profile page and the per-event connection requirement see every already-linked player.

WHY A COMMAND AND NOT A DATA MIGRATION: migrations are gitignored in this repo (.gitignore:29) and
generated on the server, so a data migration written here would never reach production. A command is
explicit, re-runnable, and defaults to a dry run so the counts can be read before anything is
written.

RUN ON THE SERVER AFTER DEPLOY:
    python manage.py backfill_connected_accounts            # read the counts
    python manage.py backfill_connected_accounts --apply
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from afc_auth.models import ConnectedAccount, User


class Command(BaseCommand):
    help = "Create a ConnectedAccount row for every user with a live Discord link."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Without it, this only reports what it would do.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        candidates = User.objects.filter(discord_connected=True)

        written = 0
        skipped = 0
        for user in candidates.iterator():
            discord_id = (user.discord_id or "").strip()
            if not discord_id:
                # Flagged connected with no id: a broken row left by an interrupted old connect.
                # There is nothing to copy, and inventing an id would create a link nobody can use.
                skipped += 1
                continue
            if apply_changes:
                ConnectedAccount.objects.update_or_create(
                    user=user,
                    provider="discord",
                    defaults={
                        "provider_user_id": discord_id,
                        "username": (user.discord_username or "")[:190],
                        "avatar_url": user.discord_avatar or "",
                        "scopes": ["identify", "guilds.join"],
                        "last_verified_at": timezone.now(),
                    },
                )
            written += 1

        verb = "wrote" if apply_changes else "would write"
        self.stdout.write(f"{verb} {written} discord links, skipped {skipped} with no discord_id")
        if not apply_changes:
            self.stdout.write("dry run, nothing written. re-run with --apply")
