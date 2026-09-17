"""
afc_auth.tests_account_deletion - a person deletes their own account, a head admin brings it back.

Pins the owner's three promises (2026-09-14, inbox #20): SOFT (the row stays and every FK still
points at it), RELEASED (a new signup can take the old in-game name, email, UID and WhatsApp),
RESTORABLE (a head admin puts it back exactly, or is told which field is taken). Plus the
guards around it: what must be settled first, the confirmation, the login sentence, and the
public surfaces that stop showing the account.

HOW IT CONNECTS
    Drives afc_auth/views_account_deletion.py through the test client with the house Bearer
    idiom, and reads the effect back through afc_auth.account_deletion, the User row, the
    DeletedAccount archive and the public player read (afc_player.views.get_public_player_stats).
    Emails go through afc_auth.views.send_email, patched here.
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from afc_auth.account_deletion import (
    RestoreConflict, deletion_blockers, restore_user, soft_delete_user, tombstone_email,
    tombstone_username,
)
from afc_auth.models import (
    ConnectedAccount, DeletedAccount, Roles, SessionToken, User, UserProfile, UserRoles,
)
from afc_team.models import Team, TeamMembers


def _user(username, *, password="pw-secret-1", uid=None, whatsapp=""):
    user = User.objects.create(username=username, email=f"{username}@x.com",
                               full_name=username.title(), uid=uid, is_active=True)
    user.set_password(password)
    user.save()
    UserProfile.objects.create(user=user, whatsapp_number=whatsapp)
    token = SessionToken.objects.create(user=user, token=f"tok_{username}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _head_admin(username="headadmin"):
    user, auth = _user(username)
    role, _ = Roles.objects.get_or_create(role_name="head_admin")
    UserRoles.objects.create(user=user, role=role)
    return user, auth


class _Base(TestCase):
    def setUp(self):
        mail = patch("afc_auth.views.send_email", return_value=True)
        self.send_email = mail.start()
        self.addCleanup(mail.stop)
        self.player, self.player_auth = _user("ghostrider", uid="1234567890", whatsapp="+2348012345678")
        ConnectedAccount.objects.create(user=self.player, provider="discord",
                                        provider_user_id="disc-777", username="ghost#1")
        self.admin, self.admin_auth = _head_admin()

    def _delete(self, auth=None, **body):
        payload = {"confirm_username": "ghostrider", "password": "pw-secret-1"}
        payload.update(body)
        return self.client.post("/auth/delete-account/", payload,
                                content_type="application/json", **(auth or self.player_auth))

    def _restore(self, user_id=None, auth=None):
        return self.client.post(f"/auth/admin/deleted-accounts/{user_id or self.player.user_id}/restore/",
                                {}, content_type="application/json", **(auth or self.admin_auth))


# ═════════════════════════ soft, released, restorable ═════════════════════════
class SoftDeleteTests(_Base):
    def test_the_row_stays_and_every_unique_column_is_released(self):
        original_pk = self.player.user_id
        # The emails are sent on commit; a TestCase never commits, so run the callbacks here.
        with self.captureOnCommitCallbacks(execute=True):
            r = self._delete()
        self.assertEqual(r.status_code, 200, r.content)

        user = User.objects.get(pk=original_pk)         # SOFT: still there
        self.assertEqual(user.status, "deleted")
        self.assertIsNotNone(user.deleted_at)
        self.assertEqual(user.username, tombstone_username(original_pk))
        self.assertEqual(user.email, tombstone_email(original_pk))
        self.assertIsNone(user.uid)
        self.assertIsNone(user.discord_id)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(UserProfile.objects.get(user=user).whatsapp_number, "")
        self.assertFalse(ConnectedAccount.objects.filter(user=user).exists())
        self.assertFalse(SessionToken.objects.filter(user=user).exists())   # signed out everywhere

        archive = DeletedAccount.objects.get(user=user, restored_at__isnull=True)
        self.assertEqual((archive.username, archive.email, archive.uid, archive.whatsapp_number),
                         ("ghostrider", "ghostrider@x.com", "1234567890", "+2348012345678"))
        self.assertEqual(archive.connected_accounts[0]["provider_user_id"], "disc-777")
        self.assertEqual(archive.deleted_by_id, original_pk)
        self.send_email.assert_called()   # the deleted-account notice, to the OLD address
        self.assertEqual(self.send_email.call_args[0][0], "ghostrider@x.com")

    def test_every_unique_identity_column_on_user_is_covered_by_the_release(self):
        """The catcher for the shape: a NEW unique column on User that the release forgets
        would silently hold the old value hostage. Read the model, not a list."""
        released = {"username", "email", "uid", "discord_id"}
        unique_cols = {f.name for f in User._meta.fields if f.unique and f.name != "user_id"}
        self.assertEqual(unique_cols - released, set(),
                         f"soft_delete_user does not release {unique_cols - released}")

    def test_a_new_signup_can_take_the_old_email_name_uid_and_whatsapp(self):
        self._delete()
        fresh = User.objects.create(username="ghostrider", email="ghostrider@x.com",
                                    uid="1234567890", full_name="New Ghost")
        UserProfile.objects.create(user=fresh, whatsapp_number="+2348012345678")
        self.assertNotEqual(fresh.pk, self.player.pk)

    def test_the_public_profile_and_the_search_stop_showing_it(self):
        self._delete()
        gone = self.client.post("/player/get-public-player-stats/",
                                {"player_ign": tombstone_username(self.player.user_id)},
                                content_type="application/json")
        self.assertEqual(gone.status_code, 404, gone.content)
        old = self.client.post("/player/get-public-player-stats/", {"player_ign": "ghostrider"},
                               content_type="application/json")
        self.assertEqual(old.status_code, 404, old.content)
        found = self.client.get("/auth/search-users/?q=deleted", **self.admin_auth)
        self.assertEqual(found.status_code, 200, found.content)
        self.assertNotIn(tombstone_username(self.player.user_id), found.content.decode())

    def test_login_with_the_old_identity_says_deleted(self):
        self._delete()
        for identifier in ("ghostrider", "ghostrider@x.com", "1234567890"):
            r = self.client.post("/auth/login/", {"ign_or_uid": identifier, "password": "pw-secret-1"},
                                 content_type="application/json")
            self.assertEqual(r.status_code, 403, (identifier, r.content))
            self.assertEqual(r.json()["code"], "account_deleted")
        # And the tombstone cannot sign in either, with any password.
        r = self.client.post("/auth/login/", {"ign_or_uid": tombstone_email(self.player.user_id),
                                              "password": "pw-secret-1"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 401, r.content)

    def test_the_old_session_is_dead(self):
        self._delete()
        r = self.client.get("/auth/delete-account/", **self.player_auth)
        self.assertEqual(r.status_code, 401, r.content)


# ═════════════════════════ the confirmation ═════════════════════════
class ConfirmationTests(_Base):
    def test_preflight_names_what_is_needed(self):
        r = self.client.get("/auth/delete-account/", **self.player_auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {"can_delete": True, "blockers": [], "needs_password": True,
                                    "username": "ghostrider"})

    def test_wrong_name_or_wrong_password_refuses_and_changes_nothing(self):
        r = self._delete(confirm_username="ghost rider")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "confirm_mismatch"))
        r = self._delete(password="nope")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "password_wrong"))
        r = self._delete(password="")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "password_required"))
        self.player.refresh_from_db()
        self.assertEqual(self.player.status, "active")
        self.assertFalse(DeletedAccount.objects.exists())

    def test_a_provider_account_with_no_password_confirms_by_name_alone(self):
        self.player.set_unusable_password()
        self.player.save()
        pre = self.client.get("/auth/delete-account/", **self.player_auth).json()
        self.assertFalse(pre["needs_password"])
        r = self._delete(password="")
        self.assertEqual(r.status_code, 200, r.content)


# ═════════════════════════ what must be settled first ═════════════════════════
class BlockerTests(_Base):
    def test_a_team_member_must_leave_first_and_an_owner_must_hand_over(self):
        owner, _ = _user("captain")
        team = Team.objects.create(team_name="Ghosts", join_settings="open", team_creator=owner,
                                   team_owner=owner, country="NG")
        TeamMembers.objects.create(team=team, member=self.player, management_role="member")
        r = self._delete()
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["code"], "deletion_blocked")
        self.assertEqual([b["code"] for b in r.json()["blockers"]], ["in_team"])
        # The owner of that team is told to hand it over, not to leave.
        self.assertEqual([b["code"] for b in deletion_blockers(owner)], ["owns_team"])

    def test_staff_suspended_and_banned_accounts_are_refused(self):
        self.assertEqual([b["code"] for b in deletion_blockers(self.admin)], ["account_is_staff"])
        self.player.status = "suspended"
        self.player.save()
        self.assertIn("account_suspended", [b["code"] for b in deletion_blockers(self.player)])

    def test_the_preflight_lists_the_same_blockers(self):
        owner, owner_auth = _user("captain2")
        Team.objects.create(team_name="Ghosts 2", join_settings="open", team_creator=owner,
                            team_owner=owner, country="NG")
        r = self.client.get("/auth/delete-account/", **owner_auth)
        self.assertFalse(r.json()["can_delete"])
        self.assertEqual(r.json()["blockers"][0]["code"], "owns_team")


# ═════════════════════════ head admins bring it back ═════════════════════════
class RestoreTests(_Base):
    def test_restore_puts_everything_back_and_is_audited(self):
        self._delete()
        with self.captureOnCommitCallbacks(execute=True):
            r = self._restore()
        self.assertEqual(r.status_code, 200, r.content)
        user = User.objects.get(pk=self.player.pk)
        self.assertEqual((user.status, user.username, user.email, user.uid, user.deleted_at),
                         ("active", "ghostrider", "ghostrider@x.com", "1234567890", None))
        self.assertTrue(user.check_password("pw-secret-1"))
        self.assertEqual(UserProfile.objects.get(user=user).whatsapp_number, "+2348012345678")
        self.assertTrue(ConnectedAccount.objects.filter(user=user, provider_user_id="disc-777").exists())
        archive = DeletedAccount.objects.get(user=user)
        self.assertIsNotNone(archive.restored_at)
        self.assertEqual(archive.restored_by_id, self.admin.pk)
        self.assertEqual(r.json()["account"]["restored_by"], "headadmin")
        # The restored-account email went to the real address.
        self.assertEqual(self.send_email.call_args[0][0], "ghostrider@x.com")
        # And they can sign in again.
        login = self.client.post("/auth/login/", {"ign_or_uid": "ghostrider", "password": "pw-secret-1"},
                                 content_type="application/json")
        self.assertEqual(login.status_code, 200, login.content)

    def test_restore_is_refused_and_names_the_field_when_the_name_was_taken(self):
        self._delete()
        User.objects.create(username="ghostrider", email="someone-else@x.com", full_name="Taker")
        r = self._restore()
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["code"], "restore_conflict")
        self.assertEqual(r.json()["fields"], ["username"])
        user = User.objects.get(pk=self.player.pk)
        self.assertEqual(user.status, "deleted")      # nothing half-restored
        # The list shows the conflict too, so the admin knows before pressing Restore.
        listed = self.client.get("/auth/admin/deleted-accounts/", **self.admin_auth).json()
        self.assertEqual(listed["results"][0]["conflicts"], ["username"])

    def test_the_list_is_head_admin_only_and_paged(self):
        self._delete()
        stranger, stranger_auth = _user("stranger")
        self.assertEqual(self.client.get("/auth/admin/deleted-accounts/", **stranger_auth).status_code, 403)
        self.assertEqual(self._restore(auth=stranger_auth).status_code, 403)
        body = self.client.get("/auth/admin/deleted-accounts/?limit=1", **self.admin_auth).json()
        self.assertEqual((body["total_count"], body["has_more"], len(body["results"])), (1, False, 1))
        self.assertEqual(body["results"][0]["username"], "ghostrider")
        self.assertTrue(body["results"][0]["self_service"])
        # After the restore it leaves the default list and shows with include_restored.
        self._restore()
        self.assertEqual(self.client.get("/auth/admin/deleted-accounts/", **self.admin_auth).json()["total_count"], 0)
        self.assertEqual(self.client.get("/auth/admin/deleted-accounts/?include_restored=1",
                                         **self.admin_auth).json()["total_count"], 1)

    def test_restoring_a_live_account_is_a_404(self):
        r = self._restore()
        self.assertEqual(r.status_code, 404, r.content)
        self.assertEqual(r.json()["code"], "not_deleted")

    def test_delete_restore_delete_keeps_one_archive_row_per_cycle(self):
        self._delete()
        restore_user(User.objects.get(pk=self.player.pk), by=self.admin)
        # A fresh session, the way a real second deletion would have one.
        user = User.objects.get(pk=self.player.pk)
        token = SessionToken.objects.create(user=user, token="tok_again").token
        r = self._delete(auth={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(DeletedAccount.objects.filter(user=user).count(), 2)
        self.assertEqual(DeletedAccount.objects.filter(user=user, restored_at__isnull=True).count(), 1)

    def test_soft_delete_is_idempotent_and_restore_conflict_raises_with_fields(self):
        archive = soft_delete_user(self.player, reason="testing")
        self.assertEqual(soft_delete_user(User.objects.get(pk=self.player.pk)).pk, archive.pk)
        User.objects.create(username="other", email="ghostrider@x.com", full_name="Taker")
        with self.assertRaises(RestoreConflict) as ctx:
            restore_user(User.objects.get(pk=self.player.pk), by=self.admin)
        self.assertEqual(ctx.exception.fields, ["email"])
