"""
afc_auth/test_discord_role_dedupe.py — tests for the duplicate-row cleanup
(dedupe_discord_role_assignments command) and the duplicate-safe queue helper
(afc_auth.discord_roles.queue_discord_role_assignments).

Background: DiscordRoleAssignment has no unique constraint (MySQL cannot enforce one
across the nullable stage/group columns), so duplicates accumulated and 500'd event
start. Run: python manage.py test afc_auth.test_discord_role_dedupe
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from .discord_roles import queue_discord_role_assignments
from .models import DiscordRoleAssignment, User


def _user(username):
    return User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role="player", password="x", discord_connected=True, discord_id=f"d_{username}",
    )


class DedupeCommandTests(TestCase):
    def setUp(self):
        self.u = _user("dupuser")
        # Three copies of one tuple: the SUCCESS row must be the survivor.
        for status in ("pending", "success", "pending"):
            DiscordRoleAssignment.objects.create(
                user=self.u, discord_id=self.u.discord_id, role_id="role123",
                stage=None, group=None, status=status,
            )
        # A clean single row for another role: untouched.
        DiscordRoleAssignment.objects.create(
            user=self.u, discord_id=self.u.discord_id, role_id="role999",
            stage=None, group=None, status="pending",
        )

    def test_dry_run_deletes_nothing(self):
        out = StringIO()
        call_command("dedupe_discord_role_assignments", "--dry-run", stdout=out)
        self.assertIn("Would delete 2", out.getvalue())
        self.assertEqual(DiscordRoleAssignment.objects.count(), 4)

    def test_real_run_keeps_best_and_is_idempotent(self):
        out = StringIO()
        call_command("dedupe_discord_role_assignments", stdout=out)
        self.assertIn("Deleted 2", out.getvalue())
        rows = DiscordRoleAssignment.objects.filter(role_id="role123")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().status, "success")  # best row survived
        self.assertEqual(DiscordRoleAssignment.objects.filter(role_id="role999").count(), 1)
        # Second run finds nothing.
        out2 = StringIO()
        call_command("dedupe_discord_role_assignments", stdout=out2)
        self.assertIn("Deleted 0", out2.getvalue())


class QueueHelperTests(TestCase):
    def test_queue_skips_existing_and_in_batch_duplicates(self):
        u = _user("queueuser")
        DiscordRoleAssignment.objects.create(
            user=u, discord_id=u.discord_id, role_id="roleA",
            stage=None, group=None, status="failed",  # ANY status counts as existing
        )
        created = queue_discord_role_assignments([
            # existing tuple: skipped (even though the row is "failed")
            DiscordRoleAssignment(user=u, discord_id=u.discord_id, role_id="roleA",
                                  stage=None, group=None, status="pending"),
            # new tuple: created
            DiscordRoleAssignment(user=u, discord_id=u.discord_id, role_id="roleB",
                                  stage=None, group=None, status="pending"),
            # same new tuple again inside the batch: skipped
            DiscordRoleAssignment(user=u, discord_id=u.discord_id, role_id="roleB",
                                  stage=None, group=None, status="pending"),
        ])
        self.assertEqual(created, 1)
        self.assertEqual(DiscordRoleAssignment.objects.filter(user=u).count(), 2)
