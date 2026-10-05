"""
Tests for the Help panel's history (inbox #147) and the staff input log (inbox #156), 2026-10-05.

Owner: "conversations should survive a sign out and sign in back, aso creating a new conversation
should not limit the last one." and "Please log all inputs to tthe help centre please and from what
user, date, time, waht was inpoutted etc."

HISTORY: an account lists and reopens its own conversations (so they are there after signing back
in, and starting a new one leaves the old ones reachable); a browser lists its own signed-out ones;
nobody reaches anybody else's, and a stranger's token answers exactly like a missing one (R88).
LOG: every chat and handoff request is logged with who, when, what was typed and what came back,
answered or refused; writing the log never changes the answer; only support staff read it.

Run: python manage.py test afc_helpbot.tests_history_log
"""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from afc_auth.models import Roles, UserRoles

from .models import HelpConversation, HelpInputLog
from .tasks import purge_old_help_chats
from .tests import VISITOR_A, VISITOR_B, HelpBotTestBase


class HistoryTests(HelpBotTestBase):
    def list(self, headers=None, **params):
        return self.client.get("/help-bot/conversations/", params, **(headers or {}))

    def one(self, token, headers=None, **params):
        return self.client.get(f"/help-bot/conversations/{token}/", params, **(headers or {}))

    def test_an_account_gets_its_chats_back_after_signing_in_again_newest_first(self):
        first = self.chat("How do tiers work?", headers=self.bearer(self.kofi)).json()["conversation"]
        second = self.chat("When does the window open?", headers=self.bearer(self.kofi)).json()["conversation"]
        HelpConversation.objects.filter(public_token=first).update(last_message_at=timezone.now() - timedelta(hours=2))
        # A brand-new session, as after signing out and back in.
        body = self.list(headers=self.bearer(self.kofi)).json()
        self.assertEqual([r["conversation"] for r in body["results"]], [second, first])
        self.assertEqual(body["results"][1]["preview"], "How do tiers work?")
        self.assertEqual((body["total_count"], body["has_more"], body["next_offset"]), (2, False, None))
        convo = self.one(second, headers=self.bearer(self.kofi)).json()
        self.assertEqual([m["role"] for m in convo["messages"]], ["user", "assistant"])
        self.assertEqual(convo["messages"][0]["text"], "When does the window open?")

    def test_another_account_sees_none_of_them_and_a_stranger_token_is_a_plain_404(self):
        token = self.chat(headers=self.bearer(self.kofi)).json()["conversation"]
        self.assertEqual(self.list(headers=self.bearer(self.ada)).json()["total_count"], 0)
        stranger = self.one(token, headers=self.bearer(self.ada))
        missing = self.one("h_" + "0" * 24, headers=self.bearer(self.ada))
        self.assertEqual((stranger.status_code, missing.status_code), (404, 404))
        self.assertEqual(stranger.json(), missing.json())
        self.assertEqual(self.one(token).status_code, 404)  # signed out, no browser id

    def test_a_browser_lists_only_its_own_signed_out_chats(self):
        mine = self.chat(visitor=VISITOR_A).json()["conversation"]
        self.chat(visitor=VISITOR_B)
        body = self.list(visitor=VISITOR_A).json()
        self.assertEqual([r["conversation"] for r in body["results"]], [mine])
        self.assertEqual(self.one(mine, visitor=VISITOR_B).status_code, 404)
        self.assertEqual(self.one(mine, visitor=VISITOR_A).status_code, 200)
        self.assertEqual(self.list().json()["total_count"], 0)  # no browser id: nothing

    def test_pages_of_results(self):
        for n in range(3):
            self.chat(f"question {n}", headers=self.bearer(self.kofi))
        body = self.list(headers=self.bearer(self.kofi), limit=2).json()
        self.assertEqual((len(body["results"]), body["has_more"], body["next_offset"]), (2, True, 2))
        rest = self.list(headers=self.bearer(self.kofi), limit=2, offset=2).json()
        self.assertEqual((len(rest["results"]), rest["has_more"]), (1, False))


class InputLogTests(HelpBotTestBase):
    def staff(self):
        role, _ = Roles.objects.get_or_create(role_name="support_admin", defaults={"description": "support"})
        UserRoles.objects.get_or_create(user=self.ada, role=role)
        return self.bearer(self.ada)

    def test_an_answered_question_is_logged_with_who_what_and_the_answer(self):
        body = self.chat("How do tiers work?", headers=self.bearer(self.kofi), page="/tournaments").json()
        row = HelpInputLog.objects.get()
        self.assertEqual((row.kind, row.username, row.user_id, row.outcome, row.http_status),
                         ("question", "Kofi_FF", self.kofi.pk, "answered", 200))
        self.assertEqual((row.text, row.answer, row.conversation_token, row.page),
                         ("How do tiers work?", "Tier 1 doubles your points.", body["conversation"], "/tournaments"))

    def test_a_refused_question_is_logged_with_its_code_and_does_not_count(self):
        with self.settings(HELP_BOT_DAILY_SIGNED_OUT=0):
            r = self.chat("Can I play?", visitor=VISITOR_A)
        self.assertEqual(r.status_code, 429)
        row = HelpInputLog.objects.get()
        self.assertEqual((row.outcome, row.http_status, row.text, row.username), ("help_daily_limit", 429, "Can I play?", ""))
        self.assertEqual(len(row.visitor_hash), 64)
        self.assertNotIn(VISITOR_A, row.visitor_hash)
        self.assertEqual(HelpConversation.objects.count(), 0)

    def test_a_handoff_is_logged_with_its_ticket(self):
        token = self.chat(headers=self.bearer(self.kofi)).json()["conversation"]
        r = self.handoff(headers=self.bearer(self.kofi), conversation=token, message="Please help")
        self.assertEqual(r.status_code, 200, r.content)
        row = HelpInputLog.objects.filter(kind="handoff").get()
        self.assertEqual(row.outcome, "ticket:" + r.json()["ticket_number"])
        self.assertEqual(row.text, "Please help")

    def test_a_failing_log_never_changes_the_answer(self):
        with patch("afc_helpbot.views.HelpInputLog.objects.create", side_effect=RuntimeError("db down")):
            r = self.chat(headers=self.bearer(self.kofi))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reply"], "Tier 1 doubles your points.")

    def test_only_support_staff_read_the_log_and_the_filters_work(self):
        self.chat("How do tiers work?", headers=self.bearer(self.kofi))
        with self.settings(HELP_BOT_DAILY_SIGNED_OUT=0):
            self.chat("Can I play?", visitor=VISITOR_A)
        self.assertEqual(self.client.get("/help-bot/admin/log/").status_code, 401)
        self.assertEqual(self.client.get("/help-bot/admin/log/", **self.bearer(self.kofi)).status_code, 403)
        staff = self.staff()
        everything = self.client.get("/help-bot/admin/log/", **staff).json()
        self.assertEqual(everything["total_count"], 2)
        self.assertEqual(everything["results"][0]["who"], None)          # newest: the signed-out visitor
        self.assertEqual(len(everything["results"][0]["visitor"]), 8)    # a short hash, never the id
        refused = self.client.get("/help-bot/admin/log/", {"outcome": "refused"}, **staff).json()
        self.assertEqual([r["outcome"] for r in refused["results"]], ["help_daily_limit"])
        kofi = self.client.get("/help-bot/admin/log/", {"who": "kofi_ff"}, **staff).json()
        self.assertEqual([r["text"] for r in kofi["results"]], ["How do tiers work?"])
        words = self.client.get("/help-bot/admin/log/", {"q": "doubles"}, **staff).json()
        self.assertEqual(words["total_count"], 1)
        visitors = self.client.get("/help-bot/admin/log/", {"who": "visitors"}, **staff).json()
        self.assertEqual(visitors["total_count"], 1)

    def test_the_log_outlives_the_chats_and_is_purged_on_its_own_clock(self):
        self.chat(headers=self.bearer(self.kofi))
        old = timezone.now() - timedelta(days=40)
        HelpConversation.objects.update(last_message_at=old)
        HelpInputLog.objects.update(created_at=old)
        purge_old_help_chats()
        self.assertEqual((HelpConversation.objects.count(), HelpInputLog.objects.count()), (0, 1))
        HelpInputLog.objects.update(created_at=timezone.now() - timedelta(days=91))
        purge_old_help_chats()
        self.assertEqual(HelpInputLog.objects.count(), 0)
