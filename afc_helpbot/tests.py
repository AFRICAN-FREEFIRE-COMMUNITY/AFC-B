"""afc_helpbot tests: the website Help panel's gate (views.py), its account facts (facts.py) and its
housekeeping (tasks.py).

The answer itself is mocked at the service boundary, afc_helpbot.brain.ask (the Discord bot's process
is a separate program with its own walk). Mail and Discord are mocked at afc_support.notify, the bot
check at afc_helpbot.views.require_human, exactly as the support desk's own tests do, so nothing here
leaves the machine.

Run: ..\\backend\\.venv\\Scripts\\python.exe manage.py test afc_helpbot --noinput
"""
import os
import uuid
from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_rankings.models import Season
from afc_support.models import SupportMessage, SupportTicket
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import Event, RegisteredCompetitors

from . import brain
from .facts import account_facts
from .models import HelpConversation, HelpMessage
from .tasks import purge_old_help_chats

VISITOR_A = "visitor-aaaaaaaaaaaaaaaa"
VISITOR_B = "visitor-bbbbbbbbbbbbbbbb"
ANSWER = {"reply": "Tier 1 doubles your points.", "needs_person": False, "used_account": False,
          "needs_sign_in": False}

SETTINGS = dict(
    BOT_CONTROL_URL="http://127.0.0.1:8099", BOT_CONTROL_TOKEN="test-token", HELP_BOT_ENABLED=True,
    HELP_BOT_DAILY_SIGNED_IN=30, HELP_BOT_DAILY_SIGNED_OUT=10, HELP_BOT_DAILY_PER_NETWORK=60,
    HELP_BOT_BURST_PER_MINUTE=50, HELP_BOT_MAX_INFLIGHT=2, HELP_BOT_RETENTION_DAYS=30,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "helpbot-tests"}},
)


@override_settings(**SETTINGS)
class HelpBotTestBase(TestCase):
    def setUp(self):
        cache.clear()  # the burst and in-flight counters live in the cache, which outlives a test
        self.asked = []

        def fake_ask(**kwargs):
            self.asked.append(kwargs)
            return dict(self.answer)

        self.answer = dict(ANSWER)
        for target, kwargs in (
            ("afc_helpbot.views.brain.ask", {"side_effect": fake_ask}),
            ("afc_helpbot.views.require_human", {"return_value": None}),
            ("afc_support.notify.send_email", {"return_value": True}),
            ("afc_support.notify.send_discord_dm", {"return_value": True}),
        ):
            p = patch(target, **kwargs)
            setattr(self, "mock_" + target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)

        self.kofi = User.objects.create(username="Kofi_FF", email="kofi@example.test", full_name="Kofi Mensah",
                                        password="x", uid="693701479", country="Ghana")
        self.ada = User.objects.create(username="AdaPlays", email="ada@example.test", full_name="Ada Obi",
                                       password="x", uid="555000111", country="Nigeria")
        self.rebels = Team.objects.create(team_name="REBELS ESPORT", team_owner=self.kofi, team_creator=self.kofi,
                                          join_settings="open")
        TeamMembers.objects.create(team=self.rebels, member=self.kofi, management_role="team_captain")
        self.other = Team.objects.create(team_name="ADA SQUAD", team_owner=self.ada, team_creator=self.ada,
                                         join_settings="open")
        TeamMembers.objects.create(team=self.other, member=self.ada, management_role="team_captain")

    # ── helpers ──────────────────────────────────────────────────────────────────────────────
    def bearer(self, user):
        session = SessionToken.objects.create(user=user, token=f"helpbot-{user.user_id}-{uuid.uuid4().hex[:12]}",
                                              expires_at=timezone.now() + timedelta(hours=3))
        return {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}

    def chat(self, message="How do tiers work?", headers=None, ip="10.0.0.1", **body):
        payload = {"message": message, **body}
        return self.client.post("/help-bot/chat/", payload, content_type="application/json",
                                HTTP_X_REAL_IP=ip, **(headers or {}))

    def handoff(self, headers=None, ip="10.0.0.1", **body):
        return self.client.post("/help-bot/handoff/", body, content_type="application/json",
                                HTTP_X_REAL_IP=ip, **(headers or {}))


class ChatTests(HelpBotTestBase):
    def test_signed_out_question_is_answered_and_stored_without_the_raw_browser_id(self):
        r = self.chat(visitor=VISITOR_A)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["conversation"].startswith("h_"))
        self.assertEqual(body["reply"], ANSWER["reply"])
        self.assertEqual((body["limit"], body["remaining"]), (10, 9))
        conv = HelpConversation.objects.get(public_token=body["conversation"])
        self.assertIsNone(conv.user)
        self.assertEqual(len(conv.visitor_hash), 64)
        self.assertNotIn(VISITOR_A, conv.visitor_hash)
        self.assertNotIn("10.0.0.1", conv.ip_hash)
        self.assertEqual(list(conv.messages.values_list("role", flat=True)), ["user", "assistant"])
        # Signed out: no account facts are sent, and the brain is told so.
        self.assertIsNone(self.asked[0]["facts"])
        self.assertFalse(self.asked[0]["signed_in"])

    def test_a_follow_up_sends_the_earlier_turns(self):
        first = self.chat(visitor=VISITOR_A).json()["conversation"]
        r = self.chat("And Tier 2?", visitor=VISITOR_A, conversation=first)
        self.assertEqual(r.status_code, 200, r.content)
        sent = self.asked[-1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        self.assertEqual(sent[-1]["content"], "And Tier 2?")

    def test_another_browser_cannot_continue_a_signed_out_conversation(self):
        token = self.chat(visitor=VISITOR_A).json()["conversation"]
        r = self.chat(visitor=VISITOR_B, conversation=token)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "help_conversation_not_found")

    def test_another_account_cannot_continue_my_conversation_and_gets_the_same_answer_as_a_missing_one(self):
        token = self.chat(headers=self.bearer(self.kofi)).json()["conversation"]
        theirs = self.chat(headers=self.bearer(self.ada), conversation=token)
        missing = self.chat(headers=self.bearer(self.ada), conversation="h_" + "0" * 24)
        self.assertEqual((theirs.status_code, missing.status_code), (404, 404))
        self.assertEqual(theirs.json(), missing.json())

    def test_signing_in_mid_chat_keeps_the_conversation(self):
        token = self.chat(visitor=VISITOR_A).json()["conversation"]
        r = self.chat("Why can't I leave my team?", headers=self.bearer(self.kofi), visitor=VISITOR_A,
                      conversation=token)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(HelpConversation.objects.get(public_token=token).user, self.kofi)

    def test_signed_in_question_carries_only_my_own_account_facts(self):
        self.answer["used_account"] = True
        r = self.chat("Why can't I leave my team?", headers=self.bearer(self.kofi))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["used_account"])
        facts = self.asked[0]["facts"]
        self.assertEqual(facts["team"]["name"], "REBELS ESPORT")
        self.assertEqual(facts["account"]["in_game_name"], "Kofi_FF")
        self.assertNotIn("ADA SQUAD", str(facts))
        self.assertNotIn("ada@example.test", str(facts))
        self.assertNotIn("kofi@example.test", str(facts))  # no email address in the facts at all
        self.assertTrue(HelpMessage.objects.get(role="assistant").used_account)

    def test_a_signed_out_answer_never_claims_to_have_used_an_account(self):
        self.answer.update(used_account=True, needs_sign_in=True)
        body = self.chat(visitor=VISITOR_A).json()
        self.assertFalse(body["used_account"])
        self.assertTrue(body["needs_sign_in"])

    def test_input_is_validated_with_codes(self):
        self.assertEqual(self.chat("", visitor=VISITOR_A).json()["code"], "help_message_invalid")
        self.assertEqual(self.chat("x" * 1001, visitor=VISITOR_A).json()["code"], "help_message_invalid")
        r = self.client.post("/help-bot/chat/", {"message": 5, "visitor": VISITOR_A}, content_type="application/json")
        self.assertEqual(r.json()["code"], "help_message_invalid")
        self.assertEqual(self.chat(visitor=VISITOR_A, conversation="42").json()["code"], "help_conversation_invalid")
        self.assertEqual(self.chat().json()["code"], "help_visitor_required")
        self.assertEqual(self.chat(visitor="short").json()["code"], "help_visitor_required")
        self.assertEqual(self.asked, [])

    def test_a_new_signed_out_chat_needs_the_bot_check(self):
        # The real require_human, with a secret configured and no token sent: refused before any call.
        from afc_auth.bot_protection import require_human
        self.mock_require_human.side_effect = require_human
        with patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "configured"}):
            r = self.chat(visitor=VISITOR_A)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "bot_check_failed")
        self.assertEqual(self.asked, [])
        self.assertFalse(HelpConversation.objects.exists())

    def test_signed_in_people_skip_the_bot_check(self):
        self.mock_require_human.return_value = "refused"
        r = self.chat(headers=self.bearer(self.kofi))
        self.assertEqual(r.status_code, 200, r.content)
        self.mock_require_human.assert_not_called()


class AllowanceTests(HelpBotTestBase):
    @override_settings(HELP_BOT_DAILY_SIGNED_IN=2)
    def test_signed_in_daily_allowance(self):
        h = self.bearer(self.kofi)
        self.assertEqual(self.chat(headers=h).json()["remaining"], 1)
        self.assertEqual(self.chat(headers=h).json()["remaining"], 0)
        r = self.chat(headers=h)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json(), {"message": "You have used today's questions.", "code": "help_daily_limit",
                                    "limit": 2, "remaining": 0})
        self.assertEqual(len(self.asked), 2)
        # Somebody else's allowance is their own.
        self.assertEqual(self.chat(headers=self.bearer(self.ada)).status_code, 200)

    @override_settings(HELP_BOT_DAILY_SIGNED_OUT=1)
    def test_signed_out_allowance_is_per_browser(self):
        self.assertEqual(self.chat(visitor=VISITOR_A).status_code, 200)
        self.assertEqual(self.chat(visitor=VISITOR_A).json()["code"], "help_daily_limit")
        self.assertEqual(self.chat(visitor=VISITOR_B).status_code, 200)

    @override_settings(HELP_BOT_DAILY_PER_NETWORK=2)
    def test_clearing_the_browser_does_not_reset_a_network(self):
        for i in range(2):
            self.assertEqual(self.chat(visitor=f"visitor-{i:016d}").status_code, 200)
        r = self.chat(visitor="visitor-9999999999999999")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["code"], "help_network_limit")
        status = self.client.get("/help-bot/status/?visitor=visitor-9999999999999999", HTTP_X_REAL_IP="10.0.0.1").json()
        self.assertEqual((status["remaining"], status["spent"]), (0, "help_network_limit"))
        # Another network is not affected.
        self.assertEqual(self.chat(visitor="visitor-8888888888888888", ip="10.0.0.2").status_code, 200)

    def test_yesterdays_questions_do_not_count(self):
        h = self.bearer(self.kofi)
        self.chat(headers=h)
        HelpMessage.objects.update(created_at=timezone.now() - timedelta(days=1, hours=1))
        self.assertEqual(self.chat(headers=h).json()["remaining"], 29)

    @override_settings(HELP_BOT_BURST_PER_MINUTE=2)
    def test_burst_limit(self):
        h = self.bearer(self.kofi)
        self.chat(headers=h)
        self.chat(headers=h)
        r = self.chat(headers=h)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["code"], "help_slow_down")

    def test_busy_when_the_in_flight_cap_is_reached_and_the_slot_is_given_back(self):
        cache.set("helpbot:inflight", 2, 120)
        r = self.chat(headers=self.bearer(self.kofi))
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "help_busy")
        self.assertEqual(cache.get("helpbot:inflight"), 2)
        self.assertEqual(self.asked, [])
        cache.set("helpbot:inflight", 0, 120)
        self.assertEqual(self.chat(headers=self.bearer(self.ada)).status_code, 200)
        self.assertEqual(cache.get("helpbot:inflight"), 0)


class BrainFailureTests(HelpBotTestBase):
    def _fail_with(self, exc):
        self.mock_ask.side_effect = exc
        return self.chat(headers=self.bearer(self.kofi))

    def test_offline_timeout_and_failure_have_their_own_codes_and_store_nothing(self):
        for exc, http, code in ((brain.BrainOffline("x"), 503, "help_ai_offline"),
                                (brain.BrainTimeout("x"), 504, "help_ai_timeout"),
                                (brain.BrainFailed("x"), 502, "help_ai_failed")):
            r = self._fail_with(exc)
            self.assertEqual((r.status_code, r.json()["code"]), (http, code))
        self.assertFalse(HelpMessage.objects.exists())
        self.assertEqual(cache.get("helpbot:inflight"), 0)  # every failure gave its slot back
        self.mock_ask.side_effect = lambda **kw: dict(ANSWER)
        self.assertEqual(self.chat(headers=self.bearer(self.kofi)).json()["remaining"], 29)

    @override_settings(BOT_CONTROL_URL="")
    def test_not_configured_means_offline(self):
        status = self.client.get("/help-bot/status/").json()
        self.assertFalse(status["online"])
        r = self.chat(visitor=VISITOR_A)
        self.assertEqual((r.status_code, r.json()["code"]), (503, "help_ai_offline"))

    @override_settings(HELP_BOT_ENABLED=False)
    def test_switched_off(self):
        self.assertFalse(self.client.get("/help-bot/status/").json()["online"])


class StatusTests(HelpBotTestBase):
    def test_status_counts_what_is_left(self):
        self.chat(visitor=VISITOR_A)
        r = self.client.get(f"/help-bot/status/?visitor={VISITOR_A}", HTTP_X_REAL_IP="10.0.0.1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"online": True, "signed_in": False, "limit": 10, "remaining": 9,
                                    "bot_check": False, "spent": None})
        signed_in = self.client.get("/help-bot/status/", **self.bearer(self.kofi)).json()
        self.assertEqual((signed_in["signed_in"], signed_in["limit"], signed_in["remaining"]), (True, 30, 30))

    def test_status_says_when_a_bot_check_is_needed(self):
        with patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "configured"}):
            self.assertTrue(self.client.get("/help-bot/status/").json()["bot_check"])
            self.assertFalse(self.client.get("/help-bot/status/", **self.bearer(self.kofi)).json()["bot_check"])


class HandoffTests(HelpBotTestBase):
    def test_signed_in_handoff_opens_one_ticket_with_the_chat(self):
        h = self.bearer(self.kofi)
        token = self.chat("Why can't I leave my team?", headers=h).json()["conversation"]
        r = self.handoff(headers=h, conversation=token)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertFalse(body["existing"])
        ticket = SupportTicket.objects.get(ticket_number=body["ticket_number"])
        self.assertEqual(ticket.source, SupportTicket.SOURCE_HELP_BOT)
        self.assertEqual((ticket.user, ticket.email), (self.kofi, "kofi@example.test"))
        self.assertEqual(ticket.subject, "Why can't I leave my team?")
        first = ticket.messages.filter(direction="in").first().body
        self.assertIn("Player: Why can't I leave my team?", first)
        self.assertIn("AFC Help: Tier 1 doubles your points.", first)
        self.assertEqual(HelpConversation.objects.get(public_token=token).ticket, ticket)
        # The acknowledgement went the same way as the contact form's.
        self.assertTrue(ticket.messages.filter(channel="auto").exists())

        # Asked again after more chat: the same ticket, with only the newer turns added.
        self.chat("Still stuck", headers=h, conversation=token)
        again = self.handoff(headers=h, conversation=token).json()
        self.assertEqual((again["ticket_number"], again["existing"]), (ticket.ticket_number, True))
        self.assertEqual(SupportTicket.objects.count(), 1)
        added = ticket.messages.filter(direction="in").order_by("-created_at").first().body
        self.assertIn("Player: Still stuck", added)
        self.assertNotIn("Why can't I leave my team?", added)

    def test_signed_out_handoff_needs_an_email(self):
        token = self.chat(visitor=VISITOR_A).json()["conversation"]
        self.assertEqual(self.handoff(visitor=VISITOR_A, conversation=token).json()["code"], "help_email_invalid")
        self.assertEqual(self.handoff(visitor=VISITOR_A, conversation=token, email="nope").json()["code"],
                         "help_email_invalid")
        r = self.handoff(visitor=VISITOR_A, conversation=token, email="visitor@example.test")
        self.assertEqual(r.status_code, 200, r.content)
        ticket = SupportTicket.objects.get(ticket_number=r.json()["ticket_number"])
        self.assertEqual((ticket.email, ticket.user), ("visitor@example.test", None))
        self.assertIn("Visitor: How do tiers work?", ticket.messages.first().body)

    def test_signed_out_handoff_always_needs_the_bot_check(self):
        from afc_auth.bot_protection import require_human
        self.mock_require_human.side_effect = require_human
        with patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "configured"}):
            r = self.handoff(email="visitor@example.test", message="I can't sign in")
        self.assertEqual(r.json()["code"], "bot_check_failed")
        self.assertFalse(SupportTicket.objects.exists())

    def test_handoff_without_a_chat_needs_a_message(self):
        h = self.bearer(self.kofi)
        self.assertEqual(self.handoff(headers=h).json()["code"], "help_handoff_empty")
        r = self.handoff(headers=h, message="The assistant is offline and my payment failed")
        self.assertEqual(r.status_code, 200, r.content)
        ticket = SupportTicket.objects.get()
        self.assertEqual(ticket.subject, "The assistant is offline and my payment failed")

    def test_cannot_attach_somebody_elses_chat(self):
        token = self.chat(headers=self.bearer(self.kofi)).json()["conversation"]
        r = self.handoff(headers=self.bearer(self.ada), conversation=token)
        self.assertEqual((r.status_code, r.json()["code"]), (404, "help_conversation_not_found"))
        r = self.handoff(visitor=VISITOR_A, conversation=token, email="x@example.test")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(SupportTicket.objects.exists())

    def test_handoffs_are_rate_limited(self):
        h = self.bearer(self.kofi)
        for i in range(5):
            self.assertEqual(self.handoff(headers=h, message=f"problem {i}").status_code, 200)
        r = self.handoff(headers=h, message="problem 6")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "help_handoff_limit"))


class FactsTests(HelpBotTestBase):
    def test_facts_hold_my_window_registrations_and_tickets_and_nobody_elses(self):
        today = timezone.localdate()
        Season.objects.create(name="SEASON 4 2026", quarter=4, year=2026, start_date=today - timedelta(days=30),
                              end_date=today + timedelta(days=60), transfer_window_open=today - timedelta(days=20),
                              transfer_window_close=today - timedelta(days=5), is_active=True)
        Season.objects.create(name="SEASON 1 2027", quarter=1, year=2027, start_date=today + timedelta(days=61),
                              end_date=today + timedelta(days=150), transfer_window_open=today + timedelta(days=61),
                              transfer_window_close=today + timedelta(days=75))
        cup = Event.objects.create(
            competition_type="tournament", participant_type="team", event_type="online",
            max_teams_or_players=12, event_name="Rebels Cup", event_mode="br",
            start_date=today + timedelta(days=3), end_date=today + timedelta(days=4),
            registration_open_date=today - timedelta(days=5), registration_end_date=today + timedelta(days=1),
            prizepool="1000", prize_distribution={}, event_rules="none", event_status="upcoming",
            registration_link="", number_of_stages=1, slug="rebels-cup", is_draft=False,
        )
        RegisteredCompetitors.objects.create(event=cup, team=self.rebels, status="registered")
        RegisteredCompetitors.objects.create(event=cup, team=self.other, status="registered")
        SupportTicket.objects.create(name="Kofi", email="kofi@example.test", user=self.kofi, subject="UID typo")
        SupportTicket.objects.create(name="Ada", email="ada@example.test", user=self.ada, subject="Ada's own")

        facts = account_facts(self.kofi)

        self.assertEqual(facts["transfer_window"]["season"], "SEASON 4 2026")
        self.assertFalse(facts["transfer_window"]["is_open_today"])
        self.assertEqual(facts["transfer_window"]["next_window_opens"], (today + timedelta(days=61)).isoformat())
        self.assertEqual([r["event"] for r in facts["my_registrations"]], ["Rebels Cup"])
        self.assertEqual(facts["my_registrations"][0]["entered_as"], "team")
        self.assertEqual([t["subject"] for t in facts["my_open_support_tickets"]], ["UID typo"])
        self.assertEqual(facts["team"]["team_page"], "/teams/REBELS%20ESPORT")
        self.assertTrue(facts["team"]["i_am_owner"])
        self.assertIsNone(facts["my_active_ban"])
        self.assertNotIn("Ada", str(facts))

    def test_a_player_with_no_team(self):
        loner = User.objects.create(username="Loner", email="loner@example.test", password="x")
        facts = account_facts(loner)
        self.assertIsNone(facts["team"])
        self.assertEqual(facts["events_holding_me_in_my_team"], [])
        self.assertEqual(facts["my_registrations"], [])

    def test_one_broken_section_does_not_lose_the_rest(self):
        with patch("afc_helpbot.facts._tickets", side_effect=RuntimeError("boom")):
            facts = account_facts(self.kofi)
        self.assertNotIn("my_open_support_tickets", facts)
        self.assertEqual(facts["team"]["name"], "REBELS ESPORT")


class PurgeTests(HelpBotTestBase):
    def test_old_chats_are_deleted_and_recent_ones_kept(self):
        old = self.chat(visitor=VISITOR_A).json()["conversation"]
        recent = self.chat(visitor=VISITOR_B).json()["conversation"]
        HelpConversation.objects.filter(public_token=old).update(last_message_at=timezone.now() - timedelta(days=31))
        self.assertEqual(purge_old_help_chats(), 1)
        self.assertEqual(list(HelpConversation.objects.values_list("public_token", flat=True)), [recent])
        self.assertEqual(HelpMessage.objects.count(), 2)
