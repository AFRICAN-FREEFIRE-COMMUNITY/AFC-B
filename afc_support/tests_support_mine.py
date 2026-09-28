"""
afc_support/tests_support_mine.py - GET support/mine/, the signed-in player's own tickets (inbox #71).

WHY EACH TEST IS HERE (owner, 28 Sep 2026: "Account + same email")
  - A ticket linked to my account is mine, wherever it was sent from.
  - A ticket sent signed out from MY email, linked to no account, is mine too (they wrote before they
    had an account, or while locked out).
  - A ticket linked to ANOTHER account is never mine, even when it carries my address: an email can
    be typed by anybody, the account link cannot (R58).
  - Signed out: 401 with a code, never a list.
  - waiting_count counts only what AFC answered and waits on me; pagination follows the house envelope.

Run: python manage.py test afc_support.tests_support_mine
"""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_support.models import SupportMessage, SupportTicket


class SupportMineTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.me = User.objects.create(username="kofi", email="Kofi.FF@gmail.com", full_name="Kofi", password="x")
        self.other = User.objects.create(username="ama", email="ama@gmail.com", full_name="Ama", password="x")
        now = timezone.now()
        # linked to me, answered by AFC and waiting on me
        self.linked = self._ticket("AFC-000001", "kofi.ff@gmail.com", self.me, "waiting", now - timedelta(hours=1))
        # sent signed out from my address (case differs), linked to nobody
        self.by_email = self._ticket("AFC-000002", "KOFI.FF@GMAIL.COM", None, "open", now - timedelta(hours=2),
                                     subject="", body="\n  Diamonds did not arrive\nsecond line")
        # carries my address but linked to ANOTHER account: not mine
        self.foreign = self._ticket("AFC-000003", "kofi.ff@gmail.com", self.other, "waiting", now)
        # someone else's, nothing to do with me
        self.theirs = self._ticket("AFC-000004", "ama@gmail.com", self.other, "open", now)

    def _ticket(self, number, email, user, state, when, subject="Roster locked", body="Hello"):
        t = SupportTicket.objects.create(ticket_number=number, public_token=f"t_{number[-6:]}0000000000000",
                                         name="N", email=email, user=user, subject=subject, status=state)
        SupportMessage.objects.create(ticket=t, direction="in", body=body)
        SupportTicket.objects.filter(pk=t.pk).update(last_message_at=when)
        t.refresh_from_db()
        return t

    def _get(self, user=None, query=""):
        headers = {}
        if user:
            tok = SessionToken.objects.create(user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
                                              expires_at=timezone.now() + SessionToken.SESSION_LIFETIME).token
            headers["HTTP_AUTHORIZATION"] = f"Bearer {tok}"
        return self.client.get(f"/support/mine/{query}", **headers)

    def test_signed_out_is_refused_with_a_code(self):
        r = self._get()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "auth_required")
        self.assertNotIn("results", r.json())

    def test_lists_linked_and_same_email_tickets_newest_first(self):
        r = self._get(self.me)
        self.assertEqual(r.status_code, 200)
        numbers = [row["ticket_number"] for row in r.json()["results"]]
        self.assertEqual(numbers, ["AFC-000001", "AFC-000002"])

    def test_a_ticket_linked_to_another_account_is_never_mine(self):
        numbers = [row["ticket_number"] for row in self._get(self.me).json()["results"]]
        self.assertNotIn("AFC-000003", numbers)
        self.assertNotIn("AFC-000004", numbers)
        other = [row["ticket_number"] for row in self._get(self.other).json()["results"]]
        self.assertEqual(sorted(other), ["AFC-000003", "AFC-000004"])

    def test_row_carries_only_what_the_list_shows(self):
        row = self._get(self.me).json()["results"][0]
        self.assertEqual(set(row), {"ticket_number", "token", "subject", "status", "created_at", "last_message_at"})
        self.assertEqual(row["token"], self.linked.public_token)

    def test_subject_falls_back_to_the_first_line_of_the_first_message(self):
        rows = {r["ticket_number"]: r for r in self._get(self.me).json()["results"]}
        self.assertEqual(rows["AFC-000002"]["subject"], "Diamonds did not arrive")

    def test_waiting_count_and_pagination(self):
        body = self._get(self.me, "?limit=1").json()
        self.assertEqual(body["waiting_count"], 1)
        self.assertEqual(body["total_count"], 2)
        self.assertEqual(len(body["results"]), 1)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], 1)
        rest = self._get(self.me, "?limit=1&offset=1").json()
        self.assertEqual([r["ticket_number"] for r in rest["results"]], ["AFC-000002"])
        self.assertFalse(rest["has_more"])

    def test_bad_paging_values_fall_back_safely(self):
        body = self._get(self.me, "?limit=abc&offset=-5").json()
        self.assertEqual(len(body["results"]), 2)

    def test_a_player_without_an_email_sees_only_linked_tickets(self):
        me = User.objects.create(username="noemail", email="", full_name="X", password="x")
        self._ticket("AFC-000005", "", None, "open", timezone.now())
        self.assertEqual(self._get(me).json()["total_count"], 0)
