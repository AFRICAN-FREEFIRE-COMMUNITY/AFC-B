"""
afc_auth/tests_admin_roles_and_counts.py - granting a role opens the admin panel, and the menu
counts show only what the person may open.

WHY (owner 2026-09-27, inbox #56 / #57 / #58: "fix all bugs, build all unbuilt and when giving
admin roles to people they should automatically be able to see the admin dashboard")
  - assign_roles_to_user set User.role = "admin" BEFORE checking the role ids, so a bad request
    still made the person an admin, and ANY role (organizer too) made them one.
  - edit_user_roles deleted every role, then met an unknown id and answered 404, leaving the person
    with only the roles listed before it.
  - support_admin (the support desk role) was not in Roles.ROLES, so the Settings > Roles picker
    could not grant it unless somebody had typed the row in by hand.
  - GET auth/admin/nav-counts/ (the menu badges) answers each queue with that queue's own check.

Run: python manage.py test afc_auth.tests_admin_roles_and_counts --keepdb
"""
import secrets

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserReport, UserRoles
from afc_auth.views import _coarse_role_after_grant, ensure_role_rows
from afc_support.models import SupportTicket


def _role(name):
    row, _ = Roles.objects.get_or_create(role_name=name, defaults={"description": name})
    return row


class RoleGrantTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.boss = User.objects.create(username="boss", email="boss@gmail.com", full_name="Boss",
                                        password="x", role="admin")
        UserRoles.objects.create(user=self.boss, role=_role("head_admin"))
        self.person = User.objects.create(username="newbie", email="newbie@gmail.com",
                                          full_name="Newbie", password="x", role="player")

    def _auth(self, user):
        token = SessionToken.objects.create(user=user, token=secrets.token_hex(16),
                                            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME)
        return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}

    def _assign(self, role_ids):
        return self.client.post("/auth/assign-roles-to-user/", {
            "username": self.person.username, "email": self.person.email, "role_ids": role_ids,
        }, content_type="application/json", **self._auth(self.boss))

    def _edit(self, role_ids):
        return self.client.post("/auth/edit-user-roles/", {
            "username": self.person.username, "email": self.person.email, "new_role_ids": role_ids,
        }, content_type="application/json", **self._auth(self.boss))

    # ── the rule itself ──────────────────────────────────────────────────────────────────────
    def test_coarse_role_rule(self):
        self.assertEqual(_coarse_role_after_grant("player", {"support_admin"}), "admin")
        self.assertEqual(_coarse_role_after_grant("support", {"shop_admin", "organizer"}), "admin")
        self.assertEqual(_coarse_role_after_grant("player", {"organizer"}), "player")
        self.assertEqual(_coarse_role_after_grant("admin", {"organizer"}), "player")
        self.assertEqual(_coarse_role_after_grant("admin", set()), "player")
        self.assertEqual(_coarse_role_after_grant("moderator", set()), "moderator")

    # ── assign ───────────────────────────────────────────────────────────────────────────────
    def test_assign_staff_role_makes_admin(self):
        res = self._assign([_role("support_admin").role_id])
        self.assertEqual(res.status_code, 200, res.content)
        self.person.refresh_from_db()
        self.assertEqual(self.person.role, "admin")

    def test_assign_organizer_only_does_not_make_admin(self):
        res = self._assign([_role("organizer").role_id])
        self.assertEqual(res.status_code, 200, res.content)
        self.person.refresh_from_db()
        self.assertEqual(self.person.role, "player")

    def test_assign_bad_id_changes_nothing(self):
        res = self._assign([_role("shop_admin").role_id, 987654])
        self.assertEqual(res.status_code, 404)
        self.person.refresh_from_db()
        self.assertEqual(self.person.role, "player")
        self.assertFalse(UserRoles.objects.filter(user=self.person).exists())

    # ── edit ─────────────────────────────────────────────────────────────────────────────────
    def test_edit_bad_id_keeps_existing_roles(self):
        UserRoles.objects.create(user=self.person, role=_role("news_admin"))
        self.person.role = "admin"
        self.person.save()
        res = self._edit([_role("shop_admin").role_id, 987654])
        self.assertEqual(res.status_code, 404)
        held = set(self.person.userroles.values_list("role__role_name", flat=True))
        self.assertEqual(held, {"news_admin"})

    def test_edit_to_organizer_only_drops_admin(self):
        UserRoles.objects.create(user=self.person, role=_role("news_admin"))
        self.person.role = "admin"
        self.person.save()
        res = self._edit([_role("organizer").role_id])
        self.assertEqual(res.status_code, 200, res.content)
        self.person.refresh_from_db()
        self.assertEqual(self.person.role, "player")

    def test_edit_grants_admin_to_a_coarse_support_user(self):
        self.person.role = "support"
        self.person.save()
        res = self._edit([_role("teams_admin").role_id])
        self.assertEqual(res.status_code, 200, res.content)
        self.person.refresh_from_db()
        self.assertEqual(self.person.role, "admin")

    def test_edit_non_list_refused(self):
        res = self._edit("3")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "role_ids_list_integers")

    # ── roles table ──────────────────────────────────────────────────────────────────────────
    def test_every_code_role_is_grantable(self):
        Roles.objects.filter(role_name="support_admin").delete()
        res = self.client.get("/auth/get-all-roles/", **self._auth(self.boss))
        self.assertEqual(res.status_code, 200)
        names = {r["role_name"] for r in res.json()["roles"]}
        self.assertTrue({name for name, _ in Roles.ROLES} <= names)
        self.assertEqual(ensure_role_rows(), [])


class NavCountsTests(TestCase):
    def setUp(self):
        self.client = Client()
        SupportTicket.objects.create(name="A", email="a@gmail.com", status=SupportTicket.STATUS_OPEN)
        SupportTicket.objects.create(name="B", email="b@gmail.com", status=SupportTicket.STATUS_RESOLVED)
        UserReport.objects.create(details="x", status="open")
        UserReport.objects.create(details="y", status="reviewing")
        UserReport.objects.create(details="z", status="resolved")

    def _user(self, username, role="player", granular=None):
        user = User.objects.create(username=username, email=f"{username}@gmail.com",
                                   full_name=username, password="x", role=role)
        if granular:
            UserRoles.objects.create(user=user, role=_role(granular))
        token = SessionToken.objects.create(user=user, token=secrets.token_hex(16),
                                            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME)
        return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}

    def _counts(self, auth):
        res = self.client.get("/auth/admin/nav-counts/", **auth)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["counts"]

    def test_signed_out_refused(self):
        self.assertEqual(self.client.get("/auth/admin/nav-counts/").status_code, 401)

    def test_player_sees_nothing(self):
        self.assertEqual(self._counts(self._user("plain")), {})

    def test_head_admin_sees_every_queue(self):
        counts = self._counts(self._user("head", role="admin", granular="head_admin"))
        self.assertEqual(counts["tickets"], 1)
        self.assertEqual(counts["reports"], 2)
        self.assertIn("approvals", counts)

    def test_support_desk_only_sees_tickets(self):
        counts = self._counts(self._user("desk", role="player", granular="support_admin"))
        self.assertEqual(counts, {"tickets": 1})
