"""
Backfill: every user already holding a Discord link gets a ConnectedAccount row.

WHY A COMMAND AND NOT A DATA MIGRATION: migrations are gitignored in this repo and generated on the
server, so a data migration written here would never reach production.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_auth.tests_connections_backfill
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from afc_auth.models import ConnectedAccount, User


def _discord_user(username, discord_id):
    return User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role="player", password="x", country="Nigeria",
        discord_id=discord_id, discord_username=f"{username}tag", discord_connected=True,
    )


class BackfillTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("backfill_connected_accounts", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing_but_reports_the_count(self):
        _discord_user("bf1", "111")
        output = self._run()
        self.assertIn("would write 1", output)
        self.assertEqual(ConnectedAccount.objects.count(), 0)

    def test_apply_creates_one_row_per_linked_user(self):
        _discord_user("bf2", "222")
        _discord_user("bf3", "333")
        self._run("--apply")
        self.assertEqual(ConnectedAccount.objects.filter(provider="discord").count(), 2)

    def test_the_copied_row_carries_the_username_and_id(self):
        _discord_user("bf6", "666")
        self._run("--apply")
        row = ConnectedAccount.objects.get(provider="discord", provider_user_id="666")
        self.assertEqual(row.username, "bf6tag")

    def test_running_twice_does_not_duplicate(self):
        _discord_user("bf4", "444")
        self._run("--apply")
        self._run("--apply")
        self.assertEqual(ConnectedAccount.objects.filter(provider="discord").count(), 1)

    def test_a_user_flagged_connected_with_no_id_is_skipped_and_reported(self):
        User.objects.create(
            username="bf5", email="bf5@x.com", full_name="BF5", role="player",
            password="x", country="Nigeria", discord_connected=True, discord_id=None,
        )
        output = self._run("--apply")
        self.assertEqual(ConnectedAccount.objects.count(), 0)
        self.assertIn("skipped 1", output)

    def test_users_without_discord_are_left_alone(self):
        User.objects.create(
            username="bf7", email="bf7@x.com", full_name="BF7", role="player",
            password="x", country="Nigeria",
        )
        self._run("--apply")
        self.assertEqual(ConnectedAccount.objects.count(), 0)
