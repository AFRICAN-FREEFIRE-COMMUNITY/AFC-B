"""
Tests for ADMIN IDENTITY REPAIR (owner 2026-08-07) - afc_auth/views_admin_identity.py.

Head admins fixing what a user cannot fix themselves: a wrong Free Fire UID, and a wrong or dead
account email. Changing somebody's email is an account-takeover primitive, so most of this file is
about the guards rather than the happy path.

WHAT IS COVERED, AND WHY EACH ONE IS HERE

  THE PERMISSION GATE (the reason this endpoint was rewritten)
    - A role=="admin" account WITHOUT head_admin is refused on all three endpoints. The previous
      gate was exactly `role == "admin"`, which let ~40 news/shop/sponsor admins change any email.
    - A plain player is refused. A missing token is 400, a bad token is 401.
    - A head_admin cannot act on a super_admin; a super_admin can.
    - Nobody can point either capability at their own account.

  UID
    - Edit and remove both work and are read back OUT OF THE DATABASE.
    - Removal stores NULL, not "", because the column is UNIQUE.
    - An ABSENT uid key is refused instead of being treated as a removal (this is the shape of the
      June 2026 bug where a partial save silently wiped UIDs).
    - Non-numeric, over-length, unchanged and already-taken values are all refused.

  EMAIL
    - Duplicates are refused CASE-INSENSITIVELY, before anything is written.
    - Both the old and the new address are emailed.
    - Every session is destroyed, and a pending self-serve email change goes with them.
    - A never-verified account is reactivated.

  2FA
    - With 2FA on, the change is REFUSED (409) unless it carries the acknowledgement, and nothing
      at all is written on that refusal.
    - With the acknowledgement, 2FA is properly torn down: setting off, recovery codes deleted,
      live challenges burned.

  AUDIT
    - Every write produces an AuditLog row naming the actor, the target, the before, the after and
      the typed reason, plus the matching AdminHistory row.
    - No endpoint accepts a blank reason.

Mail is stubbed at the SERVICE BOUNDARY (the send_email name inside views_admin_identity), so the
suite records what WOULD have gone out without touching Office365.

Run: python manage.py test afc_auth.tests_admin_identity
"""
import json
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import (
    AdminHistory,
    AuditLog,
    EmailChangeRequest,
    Roles,
    SessionToken,
    TwoFactorBackupCode,
    TwoFactorChallenge,
    TwoFactorSettings,
    User,
    UserRoles,
)

PASSWORD = "CorrectHorse!9"


class AdminIdentityTestBase(TestCase):
    """Fixtures: one head admin, one super admin, one ordinary role=="admin" (the account the old
    gate wrongly trusted), one plain player, and the target being repaired."""

    def setUp(self):
        self.client = Client()
        self.sent = []  # (to_address, subject, body, language)

        # Mail is stubbed where views_admin_identity looks it up, not where it is defined, so the
        # real send_email is untouched for every other module in the same test run.
        mail_patcher = patch(
            "afc_auth.views_admin_identity.send_email",
            side_effect=lambda to, subject, body, language="en", prelocalized=False: (
                self.sent.append((to, subject, body, language)) or True),
        )
        mail_patcher.start()
        self.addCleanup(mail_patcher.stop)

        self.head_admin = self._user("head_boss", "head@gmail.com", role="admin",
                                     granular=["head_admin"])
        self.super_admin = self._user("super_boss", "super@gmail.com", role="admin",
                                      granular=["super_admin"])
        # The account the OLD role=="admin" gate let through. It must now be refused.
        self.news_admin = self._user("news_guy", "news@gmail.com", role="admin",
                                     granular=["news_admin", "event_admin"])
        self.player = self._user("just_a_player", "player@gmail.com", role="player")

        self.target = self._user("victim", "old@gmail.com", role="player", uid="1234567890")
        self.target_token = self._session(self.target)

    # ── fixture helpers ──────────────────────────────────────────────────────────────────────
    def _user(self, username, email, role="player", granular=(), uid=None, is_active=True):
        user = User.objects.create(
            username=username, email=email, full_name=username.title(), role=role,
            password=make_password(PASSWORD), is_active=is_active, uid=uid, language="en",
        )
        for name in granular:
            role_row, _ = Roles.objects.get_or_create(
                role_name=name, defaults={"description": name})
            UserRoles.objects.create(user=user, role=role_row)
        return user

    def _session(self, user):
        """Mint a SessionToken directly (the project rule: never type a password to get a session)."""
        return SessionToken.objects.create(
            user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME,
        ).token

    # ── request helpers ──────────────────────────────────────────────────────────────────────
    def post(self, path, body=None, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def get(self, path, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(path, **headers)

    def set_uid(self, uid, token=None, reason="Support ticket 412", user_id=None, omit_uid=False):
        body = {"user_id": user_id or self.target.user_id, "reason": reason}
        if not omit_uid:
            body["uid"] = uid
        return self.post("/auth/admin/set-user-uid/", body,
                         token=token or self._session(self.head_admin))

    def set_email(self, new_email, token=None, reason="Support ticket 412", user_id=None, ack=None):
        body = {"user_id": user_id or self.target.user_id, "new_email": new_email,
                "reason": reason}
        if ack is not None:
            body["disable_two_factor"] = ack
        return self.post("/auth/admin/set-user-email/", body,
                         token=token or self._session(self.head_admin))

    # ── assertion helpers ────────────────────────────────────────────────────────────────────
    def latest_audit(self, action):
        return AuditLog.objects.filter(action=action).order_by("-id").first()


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  The permission gate
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class PermissionGateTests(AdminIdentityTestBase):

    def test_role_admin_without_head_admin_is_refused_everywhere(self):
        """The whole reason this was rewritten: role=="admin" alone is NOT enough any more."""
        token = self._session(self.news_admin)

        self.assertEqual(self.set_uid("9999999999", token=token).status_code, 403)
        self.assertEqual(self.set_email("new@gmail.com", token=token).status_code, 403)
        self.assertEqual(
            self.get(f"/auth/admin/user-identity/{self.target.user_id}/", token=token).status_code,
            403)

        # And nothing moved.
        self.target.refresh_from_db()
        self.assertEqual(self.target.uid, "1234567890")
        self.assertEqual(self.target.email, "old@gmail.com")

    def test_plain_player_is_refused(self):
        token = self._session(self.player)
        self.assertEqual(self.set_uid("9999999999", token=token).status_code, 403)
        self.assertEqual(self.set_email("new@gmail.com", token=token).status_code, 403)

    def test_missing_and_invalid_tokens(self):
        self.assertEqual(self.post("/auth/admin/set-user-uid/", {}).status_code, 400)
        self.assertEqual(self.set_uid("9999999999", token="not-a-real-token").status_code, 401)

    def test_head_admin_accepted(self):
        resp = self.set_uid("9999999999", token=self._session(self.head_admin))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_head_admin_cannot_touch_a_super_admin_but_a_super_admin_can(self):
        """Reused verbatim from views.edit_user_roles: only a super_admin may act on a super_admin."""
        refused = self.set_email("moved@gmail.com", token=self._session(self.head_admin),
                                 user_id=self.super_admin.user_id)
        self.assertEqual(refused.status_code, 403)
        self.super_admin.refresh_from_db()
        self.assertEqual(self.super_admin.email, "super@gmail.com")

        allowed = self.set_email("moved@gmail.com", token=self._session(self.super_admin),
                                 user_id=self.head_admin.user_id)
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_nobody_can_target_their_own_account(self):
        """Self-service already proves ownership; this endpoint deliberately does not."""
        token = self._session(self.head_admin)
        resp = self.set_email("mine@gmail.com", token=token, user_id=self.head_admin.user_id)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("own account", resp.json()["message"])

        resp = self.set_uid("5555555555", token=token, user_id=self.head_admin.user_id)
        self.assertEqual(resp.status_code, 403)

    def test_unknown_user_is_404(self):
        self.assertEqual(self.set_uid("9999999999", user_id=999999).status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  UID edit / remove
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class UidTests(AdminIdentityTestBase):

    def test_edit_uid_writes_the_database_and_the_audit_trail(self):
        resp = self.set_uid("2233445566", reason="Player sent a screenshot of the right UID")
        self.assertEqual(resp.status_code, 200, resp.content)

        self.target.refresh_from_db()
        self.assertEqual(self.target.uid, "2233445566")

        row = self.latest_audit("admin_set_user_uid")
        self.assertIsNotNone(row)
        self.assertEqual(row.actor_username, "head_boss")
        details = row.metadata["details"]
        self.assertEqual(details["target_user"], "victim")
        self.assertEqual(details["before"], "1234567890")
        self.assertEqual(details["after"], "2233445566")
        self.assertEqual(details["reason"], "Player sent a screenshot of the right UID")
        self.assertIn("1234567890 -> 2233445566", row.summary)

        history = AdminHistory.objects.filter(action="set_user_uid").latest("action_id")
        self.assertEqual(history.admin_user, self.head_admin)
        self.assertIn("Reason: Player sent a screenshot of the right UID", history.description)

    def test_remove_uid_stores_null_not_empty_string(self):
        """The column is UNIQUE, so a second "" would collide with the next removal."""
        resp = self.set_uid("", reason="UID belongs to another player")
        self.assertEqual(resp.status_code, 200, resp.content)

        self.target.refresh_from_db()
        self.assertIsNone(self.target.uid)

        # Proof the NULL choice matters: a second account can be cleared too.
        other = self._user("victim2", "v2@gmail.com", uid="7777777777")
        self.assertEqual(self.set_uid("", user_id=other.user_id).status_code, 200)
        other.refresh_from_db()
        self.assertIsNone(other.uid)

        details = self.latest_audit("admin_set_user_uid").metadata["details"]
        self.assertEqual(details["before"], "7777777777")
        self.assertIsNone(details["after"])

    def test_removed_uid_frees_the_value_for_its_real_owner(self):
        """The support case that motivates removal: the UID is on the wrong account."""
        real_owner = self._user("real_owner", "owner@gmail.com")
        self.assertEqual(self.set_uid("", user_id=self.target.user_id).status_code, 200)
        self.assertEqual(self.set_uid("1234567890", user_id=real_owner.user_id).status_code, 200)
        real_owner.refresh_from_db()
        self.assertEqual(real_owner.uid, "1234567890")

    def test_absent_uid_key_is_refused_and_does_not_wipe(self):
        """June 2026 bug shape: a partial save that omits `uid` must never be read as "remove"."""
        resp = self.set_uid(None, omit_uid=True)
        self.assertEqual(resp.status_code, 400)
        self.target.refresh_from_db()
        self.assertEqual(self.target.uid, "1234567890")

    def test_non_numeric_and_over_length_are_refused(self):
        # The exact shapes a spreadsheet import left in the live table.
        for bad in (".4646454948", "-668075761", "527.0848242", "7353194371.0", "ABCDEF"):
            resp = self.set_uid(bad)
            self.assertEqual(resp.status_code, 400, f"{bad} should be refused")
        self.assertEqual(self.set_uid("1" * 16).status_code, 400)

        self.target.refresh_from_db()
        self.assertEqual(self.target.uid, "1234567890")

    def test_duplicate_uid_names_the_holder(self):
        self._user("holder", "holder@gmail.com", uid="8888888888")
        resp = self.set_uid("8888888888")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("holder", resp.json()["message"])

    def test_uid_that_is_another_players_in_game_name_is_refused(self):
        """Cross-column login collision: sign-in matches username OR uid OR email in one query, so a
        UID equal to somebody's username makes that string match two rows and locks BOTH out. The
        uid column being unique does not catch it. 116 live accounts have an all-digits username."""
        self._user("9137457129", "digitsname@gmail.com")

        resp = self.set_uid("9137457129")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("in-game name", resp.json()["message"])

        self.target.refresh_from_db()
        self.assertEqual(self.target.uid, "1234567890")

    def test_a_uid_matching_the_targets_own_name_is_still_allowed(self):
        """Only ANOTHER account's name is ambiguous. One row matching itself resolves fine."""
        self.target.username = "4004004004"
        self.target.save(update_fields=["username"])
        self.assertEqual(self.set_uid("4004004004").status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(self.target.uid, "4004004004")

    def test_unchanged_uid_and_empty_removal_are_refused(self):
        self.assertEqual(self.set_uid("1234567890").status_code, 400)
        blank = self._user("no_uid", "nouid@gmail.com")
        self.assertEqual(self.set_uid("", user_id=blank.user_id).status_code, 400)

    def test_reason_is_mandatory(self):
        for reason in ("", "   ", None):
            resp = self.set_uid("2233445566", reason=reason)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["message"], "A reason is required.")
        self.target.refresh_from_db()
        self.assertEqual(self.target.uid, "1234567890")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Email change
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class EmailTests(AdminIdentityTestBase):

    def test_change_email_end_to_end(self):
        resp = self.set_email("new@gmail.com", reason="Locked out, ID confirmed on WhatsApp")
        self.assertEqual(resp.status_code, 200, resp.content)

        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "new@gmail.com")
        self.assertTrue(self.target.is_active)

        # BOTH addresses were told, old one first.
        self.assertEqual([to for to, _s, _b, _l in self.sent], ["old@gmail.com", "new@gmail.com"])
        body = self.sent[0][2]
        self.assertIn("new@gmail.com", body)
        self.assertIn("AFC support", body)
        # The OLD address is never named in the body (it also goes to the new inbox).
        self.assertNotIn("old@gmail.com", body)

        # Every session is gone, so a live cookie cannot outlive the change.
        self.assertFalse(SessionToken.objects.filter(user=self.target).exists())
        self.assertEqual(resp.json()["sessions_ended"], 1)

        row = self.latest_audit("admin_set_user_email")
        details = row.metadata["details"]
        self.assertEqual(details["before"], "old@gmail.com")
        self.assertEqual(details["after"], "new@gmail.com")
        self.assertEqual(details["reason"], "Locked out, ID confirmed on WhatsApp")
        self.assertEqual(details["sessions_ended"], 1)
        self.assertFalse(details["two_factor_disabled"])
        self.assertEqual(row.actor_username, "head_boss")

        self.assertTrue(AdminHistory.objects.filter(action="set_user_email").exists())

    def test_old_session_token_stops_working(self):
        """The point of the purge, proven from the user's side rather than the row count."""
        before = self.get("/auth/get-user-profile/", token=self.target_token)
        self.assertEqual(before.status_code, 200)

        self.assertEqual(self.set_email("new@gmail.com").status_code, 200)

        after = self.get("/auth/get-user-profile/", token=self.target_token)
        self.assertEqual(after.status_code, 401)

    def test_pending_self_serve_change_is_dropped(self):
        """A code minted against the OLD address must not be spendable after the address moves."""
        EmailChangeRequest.objects.create(
            user=self.target, new_email="someoneelse@gmail.com", token="123456")
        self.assertEqual(self.set_email("new@gmail.com").status_code, 200)
        self.assertFalse(EmailChangeRequest.objects.filter(user=self.target).exists())

    def test_duplicate_email_is_refused_case_insensitively(self):
        self._user("other", "taken@gmail.com")
        for attempt in ("taken@gmail.com", "TAKEN@Gmail.com", "Taken@GMAIL.COM"):
            resp = self.set_email(attempt)
            self.assertEqual(resp.status_code, 400, f"{attempt} should be refused")
            self.assertIn("already registered", resp.json()["message"])

        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "old@gmail.com")
        self.assertEqual(self.sent, [])
        self.assertTrue(SessionToken.objects.filter(user=self.target).exists())

    def test_email_that_is_another_players_in_game_name_is_refused(self):
        """Same cross-column trap as the UID: 106 live usernames are well-formed email addresses,
        so an address nobody holds as an email can still collide with somebody's in-game name."""
        self._user("nameislike@gmail.com", "realaddress@gmail.com")

        resp = self.set_email("nameislike@gmail.com")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("in-game name", resp.json()["message"])

        # Refused BEFORE anything is written: no mail, no session purge.
        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "old@gmail.com")
        self.assertEqual(self.sent, [])
        self.assertTrue(SessionToken.objects.filter(user=self.target).exists())

    def test_unchanged_and_malformed_addresses_are_refused(self):
        self.assertEqual(self.set_email("OLD@gmail.com").status_code, 400)
        self.assertEqual(self.set_email("not-an-email").status_code, 400)
        self.assertEqual(self.set_email("").status_code, 400)

    def test_reason_is_mandatory(self):
        resp = self.set_email("new@gmail.com", reason="  ")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["message"], "A reason is required.")
        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "old@gmail.com")

    def test_never_verified_account_is_reactivated(self):
        locked = self._user("never_verified", "typo@gmail.com", is_active=False)
        resp = self.set_email("fixed@gmail.com", user_id=locked.user_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        locked.refresh_from_db()
        self.assertTrue(locked.is_active)
        self.assertTrue(resp.json()["reactivated"])


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  Two-factor interaction
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TwoFactorInteractionTests(AdminIdentityTestBase):

    def setUp(self):
        super().setUp()
        TwoFactorSettings.objects.create(
            user=self.target, is_enabled=True, method="email", enabled_at=timezone.now())
        TwoFactorBackupCode.objects.create(user=self.target, code_hash=make_password("ABCDE-FGHIJ"))
        now = timezone.now()
        self.challenge = TwoFactorChallenge.objects.create(
            user=self.target, purpose="login", method="email", token="live-challenge-token",
            code_hash=make_password("123456"), created_at=now,
            expires_at=now + TwoFactorChallenge.CODE_LIFETIME,
        )

    def test_refused_without_the_acknowledgement_and_nothing_is_written(self):
        resp = self.set_email("new@gmail.com")
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["requires_two_factor_ack"])

        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "old@gmail.com")
        self.assertTrue(TwoFactorSettings.objects.get(user=self.target).is_enabled)
        self.assertTrue(SessionToken.objects.filter(user=self.target).exists())
        self.assertEqual(self.sent, [])

    def test_with_the_acknowledgement_two_factor_is_properly_torn_down(self):
        resp = self.set_email("new@gmail.com", ack=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["two_factor_disabled"])

        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "new@gmail.com")

        row = TwoFactorSettings.objects.get(user=self.target)
        self.assertFalse(row.is_enabled)
        self.assertIsNone(row.enabled_at)
        # Recovery codes and any live challenge go with it: neither may survive the address move.
        self.assertFalse(TwoFactorBackupCode.objects.filter(user=self.target).exists())
        self.challenge.refresh_from_db()
        self.assertIsNotNone(self.challenge.consumed_at)

        # Both emails say 2FA came down, so the owner knows to switch it back on.
        self.assertEqual(len(self.sent), 2)
        for _to, _subject, body, _lang in self.sent:
            self.assertIn("Two-factor authentication was switched off", body)

        details = self.latest_audit("admin_set_user_email").metadata["details"]
        self.assertTrue(details["two_factor_disabled"])

    def test_the_notice_is_localized_to_the_recipient(self):
        """The whole point of the localized chokepoint: a French user is not emailed English."""
        self.target.language = "fr"
        self.target.save(update_fields=["language"])

        self.assertEqual(self.set_email("new@gmail.com", ack=True).status_code, 200)
        for _to, subject, body, lang in self.sent:
            self.assertEqual(lang, "fr")
            self.assertIn("Le support AFC a modifié", body)
            self.assertIn("La double authentification a été désactivée", body)
            self.assertEqual(subject, "L'adresse e-mail de votre compte AFC a été mise à jour")

    def test_no_two_factor_means_no_teardown_line_in_the_email(self):
        TwoFactorSettings.objects.filter(user=self.target).update(is_enabled=False)
        TwoFactorBackupCode.objects.filter(user=self.target).delete()

        resp = self.set_email("new@gmail.com")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["two_factor_disabled"])
        self.assertNotIn("Two-factor authentication was switched off", self.sent[0][2])


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5  The read endpoint the dialogs open onto
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class IdentityReadTests(AdminIdentityTestBase):

    def test_returns_the_state_the_dialogs_need(self):
        resp = self.get(f"/auth/admin/user-identity/{self.target.user_id}/",
                        token=self._session(self.head_admin))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data["email"], "old@gmail.com")
        self.assertEqual(data["uid"], "1234567890")
        self.assertFalse(data["two_factor_enabled"])
        self.assertEqual(data["active_sessions"], 1)
        self.assertFalse(data["identity_locked"])
        self.assertFalse(data["is_super_admin"])

    def test_reports_two_factor_so_the_dialog_can_warn_first(self):
        TwoFactorSettings.objects.create(
            user=self.target, is_enabled=True, method="email", enabled_at=timezone.now())
        resp = self.get(f"/auth/admin/user-identity/{self.target.user_id}/",
                        token=self._session(self.head_admin))
        self.assertTrue(resp.json()["two_factor_enabled"])
