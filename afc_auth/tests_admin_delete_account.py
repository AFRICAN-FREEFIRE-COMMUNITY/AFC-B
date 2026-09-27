"""
afc_auth/tests_admin_delete_account.py - a head admin deletes an account for a person who asked.

WHY (owner 2026-09-27, inbox #59: admins "could ... help users delete their accounts"; approved
mockup mockups/account-and-2fa). The same soft delete as the person's own button, restorable the
same way; the admin records how the person asked and why; no password, since the admin is not them.

Drives GET/POST auth/admin/users/<id>/delete-account/ (views_account_deletion.admin_delete_account).
Mail is patched at afc_auth.views.send_email.
Run: python manage.py test afc_auth.tests_admin_delete_account --keepdb
"""
from unittest.mock import patch

from django.test import TestCase

from afc_auth.models import AdminHistory, DeletedAccount, User
from afc_auth.tests_account_deletion import _head_admin, _user
from afc_team.models import Team, TeamMembers


class AdminDeleteTests(TestCase):
    def setUp(self):
        mail = patch("afc_auth.views.send_email", return_value=True)
        self.send_email = mail.start()
        self.addCleanup(mail.stop)
        self.player, _ = _user("ghostrider", uid="1234567890")
        self.admin, self.admin_auth = _head_admin()
        self.url = f"/auth/admin/users/{self.player.user_id}/delete-account/"

    def _post(self, auth=None, **body):
        payload = {"channel": "ticket", "reference": "AFC-30D8E2", "reason": "Asked to close it",
                   "confirm_username": "ghostrider"}
        payload.update(body)
        return self.client.post(self.url, payload, content_type="application/json",
                                **(auth or self.admin_auth))

    def test_head_admin_deletes_on_request_and_it_is_recorded(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self._post()
        self.assertEqual(r.status_code, 200, r.content)
        user = User.objects.get(pk=self.player.user_id)
        self.assertEqual(user.status, "deleted")
        archive = DeletedAccount.objects.get(user=user)
        self.assertEqual(archive.deleted_by_id, self.admin.user_id)
        self.assertIn("ticket", archive.reason)
        self.assertIn("AFC-30D8E2", archive.reason)
        self.assertTrue(AdminHistory.objects.filter(action="deleted_account_on_request",
                                                    admin_user=self.admin).exists())
        # The "at your request" email went to the person's real address, once.
        self.assertEqual(self.send_email.call_count, 1)
        self.assertEqual(self.send_email.call_args[0][0], "ghostrider@x.com")
        self.assertIn("head admin", self.send_email.call_args[0][2])

    def test_restore_brings_it_back(self):
        self._post()
        r = self.client.post(f"/auth/admin/deleted-accounts/{self.player.user_id}/restore/", {},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(User.objects.get(pk=self.player.user_id).username, "ghostrider")

    def test_notify_off_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self._post(notify=False)
        self.assertEqual(r.status_code, 200, r.content)
        self.send_email.assert_not_called()

    def test_only_head_admins(self):
        _other, other_auth = _user("someone")
        self.assertEqual(self._post(auth=other_auth).status_code, 403)
        self.assertEqual(self.client.get(self.url, **other_auth).status_code, 403)
        self.assertEqual(User.objects.get(pk=self.player.user_id).status, "active")

    def test_the_same_blockers_apply(self):
        owner, _ = _user("captain")
        team = Team.objects.create(team_name="Ghosts", join_settings="open", team_creator=owner,
                                   team_owner=owner, country="NG")
        TeamMembers.objects.create(team=team, member=self.player, management_role="member")
        pre = self.client.get(self.url, **self.admin_auth).json()
        self.assertFalse(pre["can_delete"])
        self.assertEqual([b["code"] for b in pre["blockers"]], ["in_team"])
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "deletion_blocked")

    def test_bad_input_is_refused_with_codes(self):
        for body, code in (({"channel": "pigeon"}, "channel_invalid"),
                           ({"reason": "  "}, "reason_required"),
                           ({"confirm_username": "ghost"}, "confirm_mismatch")):
            r = self._post(**body)
            self.assertEqual(r.status_code, 400, r.content)
            self.assertEqual(r.json()["code"], code)
        self.assertEqual(User.objects.get(pk=self.player.user_id).status, "active")

    def test_unknown_or_already_deleted(self):
        self._post()
        self.assertEqual(self._post().status_code, 404)
