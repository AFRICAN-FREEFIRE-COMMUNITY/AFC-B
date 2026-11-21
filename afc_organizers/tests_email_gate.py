"""Event email only for AFC's own and AFC Partner events (owner 16 Nov 2026, AFC inbox #599 to #601; open
organizations plan 1 Task 4).

send_email(event=...) is the gate: a Community or Verified organizer's event sends no email of any kind (the players
get site notifications, and the registration step says so through the event payload's `sends_email`); an AFC event
and an AFC Partner event with the `promo` perk on do. Under the test runner every send goes to afc_auth.outbox, so
"sent" and "skipped" are read there. The AST test is the catcher: every send_email call in the event code must pass
event=, so a new sender cannot forget the rule.
"""
import ast
import datetime
from pathlib import Path

from django.test import RequestFactory, TestCase

from afc_auth import outbox
from afc_auth.views import send_email
from afc_tournament_and_scrims.event_contract import PUBLIC, serialize_event
from afc_tournament_and_scrims.models import Event

from .models import Organization

ROOT = Path(__file__).resolve().parent.parent
EVENT_SENDER_DIRS = ("afc_tournament_and_scrims", "afc_draws")
KEYS = ("organization_badge", "sends_email")


def make_event(name, org=None):
    return Event.objects.create(
        competition_type="tournament", participant_type="squad", event_type="internal", max_teams_or_players=16,
        event_name=name, event_mode="virtual", start_date=datetime.date(2026, 12, 12),
        end_date=datetime.date(2026, 12, 13), registration_open_date=datetime.date(2026, 11, 1),
        registration_end_date=datetime.date(2026, 12, 1), prizepool="0", event_rules="rules",
        event_status="upcoming", registration_link="https://example.com/reg", number_of_stages=1, organization=org)


class EmailGateTests(TestCase):
    def setUp(self):
        outbox.drain()
        self.org = Organization.objects.create(slug="kopa", name="Kopa Esports", badge="community")
        self.event = make_event("KOPA CUP", org=self.org)

    def send(self, event):
        return send_email("player@gmail.com", "You are in", "<p>Room at 18:00</p>", event=event)

    def set_org(self, badge, perks=None):
        Organization.objects.filter(pk=self.org.pk).update(badge=badge, perks=perks or {})
        self.event = Event.objects.select_related("organization").get(pk=self.event.pk)

    def test_send_email_skips_community_event(self):
        self.assertFalse(self.send(self.event))
        self.assertEqual(outbox.drain(), [])

    def test_send_email_skips_verified_event(self):
        self.set_org("verified")
        self.assertFalse(self.send(self.event))
        self.assertEqual(outbox.drain(), [])

    def test_send_email_sends_for_afc_event(self):
        self.assertTrue(self.send(make_event("AFC CUP")))
        self.assertEqual(len(outbox.drain()), 1)

    def test_send_email_sends_for_partner_with_promo(self):
        self.set_org("partner", {"promo": True})
        self.assertTrue(self.send(self.event))
        self.assertEqual(len(outbox.drain()), 1)

    def test_send_email_skips_partner_without_promo(self):
        self.set_org("partner", {"promo": False})
        self.assertFalse(self.send(self.event))
        self.assertEqual(outbox.drain(), [])

    def test_event_broadcast_keeps_the_notification_drops_the_email(self):
        from afc_auth.models import Notifications, User
        from afc_auth.views import deliver_broadcast
        player = User.objects.create(username="dayo", email="dayo@gmail.com", full_name="Dayo", password="x")
        pushed, emailed = deliver_broadcast([player], "Room details", "Room 1234, password 99", delivery="both",
                                            related_event=self.event, scope="room_details", log=False)
        self.assertEqual((pushed, emailed), (1, 0))
        self.assertEqual(outbox.drain(), [])
        self.assertTrue(Notifications.objects.filter(user=player, related_event=self.event).exists())
        self.set_org("partner", {"promo": True})
        pushed, emailed = deliver_broadcast([player], "Room details", "Room 1234, password 99", delivery="both",
                                            related_event=self.event, scope="room_details", log=False)
        self.assertEqual((pushed, emailed), (1, 1))
        # deliver_broadcast sends its emails on a background thread: wait for it.
        import time
        sent = []
        for _ in range(50):
            sent += outbox.drain()
            if sent:
                break
            time.sleep(0.1)
        self.assertEqual(len(sent), 1)

    def test_mail_not_about_an_event_is_untouched(self):
        self.assertTrue(send_email("player@gmail.com", "Your code", "<p>123456</p>"))
        self.assertEqual(len(outbox.drain()), 1)

    def test_event_payload_has_badge_and_sends_email(self):
        request = RequestFactory().get("/")
        out = serialize_event(self.event, request=request, role=PUBLIC, fields=KEYS)
        self.assertEqual((out["organization_badge"], out["sends_email"]), ("community", False))
        self.set_org("partner", {"promo": True})
        out = serialize_event(self.event, request=request, role=PUBLIC, fields=KEYS)
        self.assertEqual((out["organization_badge"], out["sends_email"]), ("partner", True))
        afc = serialize_event(make_event("AFC CUP"), request=request, role=PUBLIC, fields=KEYS)
        self.assertEqual((afc["organization_badge"], afc["sends_email"]), (None, True))


class EverySenderPassesTheEvent(TestCase):
    """The catcher (R28): a send_email call in the event code with no event= keyword fails here, naming the line."""

    def calls_without_event(self, text, name="x.py"):
        tree = ast.parse(text, filename=name)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if called == "send_email" and not any(k.arg == "event" for k in node.keywords):
                missing.append("%s:%s" % (name, node.lineno))
        return missing

    def test_every_event_sender_passes_event(self):
        missing = []
        for folder in EVENT_SENDER_DIRS:
            for path in sorted((ROOT / folder).glob("*.py")):
                if path.name.startswith("test"):
                    continue
                missing += self.calls_without_event(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
        self.assertEqual(missing, [], "event email must pass event= so the badge rule applies")

    def test_the_catcher_catches(self):
        self.assertEqual(self.calls_without_event("send_email(a, b, c)\nviews.send_email(a, b, c, event=e)\n"),
                         ["x.py:1"])
