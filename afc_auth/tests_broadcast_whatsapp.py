# afc_auth/tests_broadcast_whatsapp.py
# ──────────────────────────────────────────────────────────────────────────────
# Tests for BROADCASTS ON WHATSAPP, the third channel (owner 2026-08-05).
#
# Three things matter more than the feature itself, and each is tested first-class:
#
#   1. NOTHING CHANGES FOR THE TWO CHANNELS THAT ALREADY WORKED. "push", "email" and "both" have
#      to mean exactly what they meant yesterday, on every one of the eight call sites that pass
#      them. test_legacy_delivery_values_are_unchanged and test_push_and_email_are_untouched_*
#      pin that down.
#
#   2. THE CAP IS A REFUSAL, NOT A TRIM. AFC has ONE WhatsApp number and it is the number that
#      carries room IDs; a marketing blast people mute or report drags its quality rating down,
#      and every message is paid for. test_over_cap_* prove an oversized audience sends NOTHING
#      and is refused with the limit named, rather than reaching an arbitrary first N.
#
#   3. AN UNCONFIGURED DEPLOYMENT STAYS SILENT. A blank template name means "do not send", so a
#      server without the approved template never fails a send per recipient. test_blank_template_*.
#
# The rest covers reach (who has a number, who opted out) and best-effort-per-recipient.
#
# Auth is a real bearer SessionToken, matching tests_broadcast_audience.py. queue_template is
# patched at the afc_auth.broadcast_whatsapp boundary in EVERY test, so nothing here touches Meta,
# Celery or the network, and afc_auth.views.send_email is patched wherever the email leg runs.
# ──────────────────────────────────────────────────────────────────────────────
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .audience import EMAIL, PUSH, WHATSAPP, delivery_token, parse_delivery
from .broadcast_whatsapp import (
    MAX_BODY_CHARS,
    send_broadcast_whatsapp,
    whatsapp_max_recipients,
    whatsapp_volume_assessment,
)
from .models import (
    Notifications,
    Roles,
    SentBroadcast,
    SessionToken,
    User,
    UserProfile,
    UserRoles,
)
from .views import deliver_broadcast


# Pinned for the whole class so the assertions do not depend on whatever the developer's .env
# happens to hold. Individual tests override the cap or blank the template on top of these.
@override_settings(
    WHATSAPP_BROADCAST_TEMPLATE="broadcast",
    WHATSAPP_BROADCAST_TEMPLATE_LANG="en",
    WHATSAPP_BROADCAST_MAX_RECIPIENTS=500,
)
class BroadcastWhatsAppTests(TestCase):
    # ── auth helpers (same shape as tests_broadcast_audience.py) ─────────────
    def _token_for(self, user):
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    def _send(self, actor, body):
        return self.client.post(
            reverse("broadcast_audience_send"),
            data=body, content_type="application/json", **self._auth(actor),
        )

    def _preview(self, actor, spec):
        return self.client.post(
            reverse("broadcast_audience_preview"),
            data=spec, content_type="application/json", **self._auth(actor),
        )

    # ── fixtures ─────────────────────────────────────────────────────────────
    # A hand-countable population, deliberately mixed so "reachable on WhatsApp" is a SMALLER set
    # than "in the audience", which is the whole point of counting the two separately:
    #
    #   admin       has a number, opted in     -> reachable
    #   player_a    has a number, opted in     -> reachable
    #   player_b    has a number, OPTED OUT    -> skipped
    #   player_c    profile with a BLANK number-> skipped
    #   player_d    NO profile row at all      -> skipped
    #
    # Eligible audience = 5. WhatsApp-reachable = 2.
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_user", email="admin@afc.test", password="x",
            role="admin", country="Nigeria", language="en",
        )
        # head_admin, because the WhatsApp channel became head-admin-only on 2026-08-05: it is
        # billed per message, so sending on it is a spending decision (see
        # tests_whatsapp_head_admin.py). This suite is about DELIVERY, not permission, so it gives
        # its admin the role the feature now needs rather than asserting the old, open behaviour.
        _head_role, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=self.admin, role=_head_role)
        self.player_a = User.objects.create_user(
            username="player_a", email="a@afc.test", password="x",
            role="player", country="Nigeria", language="en",
        )
        self.player_b = User.objects.create_user(
            username="player_b", email="b@afc.test", password="x",
            role="player", country="Ghana", language="en",
        )
        self.player_c = User.objects.create_user(
            username="player_c", email="c@afc.test", password="x",
            role="player", country="Nigeria", language="en",
        )
        self.player_d = User.objects.create_user(
            username="player_d", email="d@afc.test", password="x",
            role="player", country="Nigeria", language="en",
        )

        UserProfile.objects.create(user=self.admin, whatsapp_number="+2348010000001")
        UserProfile.objects.create(user=self.player_a, whatsapp_number="+2348010000002")
        UserProfile.objects.create(user=self.player_b, whatsapp_number="+2348010000003",
                                   whatsapp_opt_in=False)
        UserProfile.objects.create(user=self.player_c, whatsapp_number="")
        # player_d deliberately has no UserProfile row at all.

        self.everyone = [self.admin, self.player_a, self.player_b, self.player_c, self.player_d]

    # ══════════════════════════════════════════════════════════════════════════
    # §1  THE DELIVERY VOCABULARY - the old values must not move
    # ══════════════════════════════════════════════════════════════════════════

    def test_legacy_delivery_values_are_unchanged(self):
        # The three tokens that existed before WhatsApp select exactly the channels they always
        # did. "both" is app + email, NOT "all of them" - widening it would have turned every
        # broadcast already in flight into a WhatsApp blast.
        self.assertEqual(parse_delivery("push"), frozenset({PUSH}))
        self.assertEqual(parse_delivery("email"), frozenset({EMAIL}))
        self.assertEqual(parse_delivery("both"), frozenset({PUSH, EMAIL}))
        self.assertEqual(delivery_token(parse_delivery("both")), "both")

    def test_whatsapp_combinations_parse(self):
        self.assertEqual(parse_delivery("whatsapp"), frozenset({WHATSAPP}))
        self.assertEqual(parse_delivery("push,whatsapp"), frozenset({PUSH, WHATSAPP}))
        self.assertEqual(parse_delivery("both,whatsapp"), frozenset({PUSH, EMAIL, WHATSAPP}))
        # Whitespace and case are the composer's problem, not the caller's.
        self.assertEqual(parse_delivery(" BOTH , WhatsApp "), frozenset({PUSH, EMAIL, WHATSAPP}))
        # A list is accepted too, so a future frontend sending an array is not a backend change.
        self.assertEqual(parse_delivery(["push", "whatsapp"]), frozenset({PUSH, WHATSAPP}))

    def test_delivery_token_round_trips(self):
        # The canonical token is what lands on SentBroadcast.delivery, so it has to parse back to
        # the same channels or the history row would describe a different send.
        for value in ("push", "email", "both", "whatsapp",
                      "push,whatsapp", "email,whatsapp", "both,whatsapp"):
            channels = parse_delivery(value)
            self.assertEqual(delivery_token(channels), value)
            self.assertEqual(parse_delivery(delivery_token(channels)), channels)

    def test_unrecognised_delivery_selects_no_channels(self):
        # Same outcome a junk value had before this existed: nothing is sent. The endpoints turn
        # the empty set into a 400 so the admin is told, rather than silently reaching nobody.
        self.assertEqual(parse_delivery("carrier-pigeon"), frozenset())
        self.assertEqual(parse_delivery(""), frozenset())
        self.assertEqual(parse_delivery(None), frozenset())

    # ══════════════════════════════════════════════════════════════════════════
    # §2  SENDING - who gets the template, who is skipped
    # ══════════════════════════════════════════════════════════════════════════

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_whatsapp_selected_sends_the_template(self, mock_queue):
        queued, skipped = send_broadcast_whatsapp(self.everyone, "Heads up", "Server maintenance")

        # Only the two reachable accounts. The opted-out, the blank number and the profileless one
        # are skipped, and skipping is not an error.
        self.assertEqual((queued, skipped), (2, 3))
        self.assertEqual(mock_queue.call_count, 2)

        numbers = {call.args[0] for call in mock_queue.call_args_list}
        self.assertEqual(numbers, {"+2348010000001", "+2348010000002"})

        # Template name and language come from settings, and the language is explicit because `en`
        # and `en_US` are different templates to Meta.
        args, kwargs = mock_queue.call_args_list[0]
        self.assertEqual(args[1], "broadcast")
        self.assertEqual(args[2], "en")
        self.assertEqual(kwargs["context"], "broadcast")

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_body_params_are_name_then_message(self, mock_queue):
        # {{1}} recipient name, {{2}} the body. The title is folded into the front of the body
        # because the template has nowhere else to put it, and a title usually carries the point.
        send_broadcast_whatsapp([self.player_a], "Registration closes tonight",
                                "Sign up before 9pm.")
        params = mock_queue.call_args.kwargs["body_params"]
        self.assertEqual(params[0], "player_a")
        self.assertEqual(params[1], "Registration closes tonight: Sign up before 9pm.")

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_multi_line_body_is_collapsed_and_never_empty(self, mock_queue):
        # Meta rejects a parameter containing a newline, and rejects an empty one, and in both
        # cases it rejects the WHOLE message rather than the one variable.
        send_broadcast_whatsapp([self.player_a], "", "line one\n\nline two")
        self.assertEqual(mock_queue.call_args.kwargs["body_params"][1], "line one line two")

        mock_queue.reset_mock()
        send_broadcast_whatsapp([self.player_a], "", "")
        self.assertEqual(mock_queue.call_args.kwargs["body_params"][1], "-")

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_long_body_is_trimmed_rather_than_rejected(self, mock_queue):
        # An over-long body makes Meta refuse the message outright, so the WhatsApp copy is
        # trimmed. The full text still goes out in-app and by email.
        send_broadcast_whatsapp([self.player_a], "", "x" * (MAX_BODY_CHARS + 200))
        body = mock_queue.call_args.kwargs["body_params"][1]
        self.assertTrue(body.endswith("..."))
        self.assertLessEqual(len(body), MAX_BODY_CHARS + 3)

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_recipient_without_a_number_is_skipped_without_raising(self, mock_queue):
        queued, skipped = send_broadcast_whatsapp([self.player_c, self.player_d], "t", "m")
        self.assertEqual((queued, skipped), (0, 2))
        self.assertFalse(mock_queue.called)

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_opted_out_recipient_is_skipped(self, mock_queue):
        # Consent is Meta policy, not a nicety: keep messaging after an opt-out and the number
        # gets rated down and eventually blocked.
        queued, skipped = send_broadcast_whatsapp([self.player_b], "t", "m")
        self.assertEqual((queued, skipped), (0, 1))
        self.assertFalse(mock_queue.called)

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_one_bad_recipient_does_not_cost_the_rest_their_message(self, mock_queue):
        mock_queue.side_effect = [RuntimeError("meta is down"), "ok"]
        # Both recipients are reachable, so the only skip is the one that blew up.
        queued, skipped = send_broadcast_whatsapp([self.admin, self.player_a], "t", "m")
        self.assertEqual((queued, skipped), (1, 1))
        self.assertEqual(mock_queue.call_count, 2)

    @override_settings(WHATSAPP_BROADCAST_TEMPLATE="")
    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_blank_template_name_sends_nothing(self, mock_queue):
        # A deployment without the approved template stays silent instead of failing a send per
        # recipient, the rule every WhatsApp template name follows.
        queued, skipped = send_broadcast_whatsapp(self.everyone, "t", "m")
        self.assertEqual((queued, skipped), (0, 5))
        self.assertFalse(mock_queue.called)

    # ══════════════════════════════════════════════════════════════════════════
    # §3  THE CAP - refused whole, never truncated
    # ══════════════════════════════════════════════════════════════════════════

    @override_settings(WHATSAPP_BROADCAST_MAX_RECIPIENTS=1)
    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_over_cap_audience_sends_nothing_at_all(self, mock_queue):
        # Two reachable recipients against a cap of one. The point of the assertion is the ZERO:
        # a broadcast that reached one of the two would be impossible to reason about afterwards.
        queued, skipped = send_broadcast_whatsapp(self.everyone, "t", "m")
        self.assertEqual(queued, 0)
        self.assertEqual(skipped, 5)
        self.assertFalse(mock_queue.called)

    @override_settings(WHATSAPP_BROADCAST_MAX_RECIPIENTS=250)
    def test_volume_assessment_levels(self):
        self.assertEqual(whatsapp_max_recipients(), 250)

        ok = whatsapp_volume_assessment(100)
        self.assertEqual(ok["level"], "ok")
        self.assertFalse(ok["blocked"])

        blocked = whatsapp_volume_assessment(251)
        self.assertEqual(blocked["level"], "blocked")
        self.assertTrue(blocked["blocked"])
        # The refusal has to NAME the number, or the admin cannot act on it.
        self.assertIn("250", blocked["message"])
        self.assertIn("251", blocked["message"])

    @override_settings(WHATSAPP_BROADCAST_MAX_RECIPIENTS=1)
    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_send_endpoint_refuses_an_oversized_whatsapp_audience(self, mock_queue):
        resp = self._send(
            self.admin,
            {"everyone": True, "message": "Big news", "delivery": "push,whatsapp",
             "confirmed_count": 5},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(resp.json()["whatsapp_volume"]["blocked"])
        self.assertIn("1-per-broadcast", resp.json()["message"])
        self.assertEqual(resp.json()["recommended_delivery"], "push")
        # Refused means nothing happened AT ALL, not "the in-app half went out".
        self.assertFalse(mock_queue.called)
        self.assertEqual(Notifications.objects.count(), 0)

    @override_settings(WHATSAPP_BROADCAST_MAX_RECIPIENTS=1)
    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_the_same_audience_can_still_be_pushed_in_app(self, mock_queue):
        # The cap is on the WhatsApp channel, not on broadcasting. In-app still reaches everyone.
        resp = self._send(
            self.admin,
            {"everyone": True, "message": "Big news", "delivery": "push", "confirmed_count": 5},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Notifications.objects.count(), 5)
        self.assertFalse(mock_queue.called)

    # ══════════════════════════════════════════════════════════════════════════
    # §4  THROUGH THE CHOKEPOINT - deliver_broadcast and the send endpoint
    # ══════════════════════════════════════════════════════════════════════════

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_deliver_broadcast_sends_whatsapp_alongside_push(self, mock_queue):
        result = deliver_broadcast(self.everyone, "Heads up", "Server maintenance",
                                   delivery="push,whatsapp", sender=self.admin)
        pushed, emailed = result                     # the pair every existing caller unpacks
        self.assertEqual((pushed, emailed), (5, 0))
        self.assertEqual(result.whatsapp_queued, 2)
        self.assertEqual(result.whatsapp_skipped, 3)
        self.assertEqual(mock_queue.call_count, 2)
        # Every recipient still gets the in-app notification; WhatsApp is an extra, never a swap.
        self.assertEqual(Notifications.objects.count(), 5)
        self.assertEqual(SentBroadcast.objects.get().delivery, "push,whatsapp")

    # send_email is patched because the email leg runs on a daemon thread; nothing here is allowed
    # to open an SMTP connection.
    @patch("afc_auth.views.send_email")
    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_push_and_email_are_untouched_when_whatsapp_is_not_selected(self, mock_queue, _email):
        pushed, emailed = deliver_broadcast(self.everyone, "Heads up", "Nothing to see",
                                            delivery="both", sender=self.admin)
        self.assertEqual((pushed, emailed), (5, 5))
        self.assertEqual(Notifications.objects.count(), 5)
        self.assertFalse(mock_queue.called)          # the whole point of this test
        self.assertEqual(SentBroadcast.objects.get().delivery, "both")

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_push_only_is_untouched(self, mock_queue):
        pushed, emailed = deliver_broadcast(self.everyone, "Heads up", "Nothing to see",
                                            delivery="push", sender=self.admin)
        self.assertEqual((pushed, emailed), (5, 0))
        self.assertFalse(mock_queue.called)
        self.assertEqual(SentBroadcast.objects.get().delivery, "push")

    @patch("afc_auth.broadcast_whatsapp.queue_template")
    def test_send_endpoint_delivers_whatsapp_and_reports_both_numbers(self, mock_queue):
        resp = self._send(
            self.admin,
            {"everyone": True, "message": "Finals tonight", "title": "AFC",
             "delivery": "push,whatsapp", "confirmed_count": 5},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["pushed"], 5)
        # "We messaged 2 of your 5 players" - the sentence an admin needs, because this channel
        # reaches a fraction of the audience the other two do.
        self.assertEqual(body["whatsapp_queued"], 2)
        self.assertEqual(body["whatsapp_skipped"], 3)
        self.assertEqual(body["delivery"], "push,whatsapp")
        self.assertEqual(mock_queue.call_count, 2)

    def test_send_endpoint_rejects_an_unknown_channel(self):
        resp = self._send(
            self.admin,
            {"everyone": True, "message": "Hello", "delivery": "carrier-pigeon",
             "confirmed_count": 5},
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Notifications.objects.count(), 0)

    def test_preview_reports_whatsapp_reach(self):
        body = self._preview(self.admin, {"everyone": True}).json()
        self.assertEqual(body["recipient_count"], 5)
        self.assertEqual(body["whatsapp_recipient_count"], 2)   # a number on file AND consent
        self.assertEqual(body["whatsapp_volume"]["level"], "ok")
        self.assertEqual(body["whatsapp_volume"]["max_recipients"], whatsapp_max_recipients())
