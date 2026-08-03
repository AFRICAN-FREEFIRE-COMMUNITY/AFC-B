"""The inbound webhook (afc_whatsapp/webhooks.py).

Covers the three things this endpoint is trusted to do:
  1. prove the caller is Meta before believing a word of the body,
  2. advance a message through sent -> delivered -> read, and record Meta's error
     code when it fails,
  3. honour a STOP by clearing UserProfile.whatsapp_opt_in, which Meta requires.

Signatures are computed here exactly as Meta computes them, so the tests exercise the
real HMAC path rather than a bypass.
"""
import hashlib
import hmac
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from afc_auth.models import UserProfile
from afc_whatsapp.models import WhatsAppMessage

User = get_user_model()

WEBHOOK_URL = "/whatsapp/webhook/"
APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def _envelope(**value):
    """Meta's webhook envelope with `value` dropped in the one place that matters."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {"messaging_product": "whatsapp",
                          "metadata": {"phone_number_id": "1234567890"},
                          **value},
            }],
        }],
    }


@override_settings(WHATSAPP_APP_SECRET=APP_SECRET, WHATSAPP_WEBHOOK_VERIFY_TOKEN=VERIFY_TOKEN)
class WebhookTestCase(TestCase):
    """Shared helpers: post a signed body the way Meta would."""

    def _post(self, payload, *, secret=APP_SECRET, header=True):
        body = json.dumps(payload)
        headers = {}
        if header:
            digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["HTTP_X_HUB_SIGNATURE_256"] = f"sha256={digest}"
        return self.client.post(
            WEBHOOK_URL, data=body, content_type="application/json", **headers
        )


class SignatureTests(WebhookTestCase):
    def test_unsigned_post_is_rejected(self):
        response = self._post(_envelope(statuses=[]), header=False)
        self.assertEqual(response.status_code, 403)

    def test_wrongly_signed_post_is_rejected(self):
        response = self._post(_envelope(statuses=[]), secret="not-the-secret")
        self.assertEqual(response.status_code, 403)

    def test_a_forged_post_changes_nothing(self):
        # The point of the signature: an unauthenticated caller must not be able to
        # rewrite delivery history.
        message = WhatsAppMessage.objects.create(
            phone="+2348051234567", wamid="wamid.SIG", status="sent",
            sent_at=timezone.now(),
        )
        self._post(_envelope(statuses=[{"id": "wamid.SIG", "status": "read",
                                        "timestamp": "1718000000"}]), header=False)
        message.refresh_from_db()
        self.assertEqual(message.status, "sent")

    @override_settings(WHATSAPP_APP_SECRET="")
    def test_no_configured_secret_rejects_everything(self):
        # Deliberately stricter than the Kapso webhook this replaces, which accepts
        # unsigned posts whenever no secret is set.
        response = self._post(_envelope(statuses=[]))
        self.assertEqual(response.status_code, 403)

    def test_correctly_signed_post_is_accepted(self):
        response = self._post(_envelope(statuses=[]))
        self.assertEqual(response.status_code, 200)


class HandshakeTests(WebhookTestCase):
    def test_correct_token_echoes_the_challenge(self):
        response = self.client.get(WEBHOOK_URL, {
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        })
        self.assertEqual(response.status_code, 200)
        # Meta string-compares the body, so it must be the bare challenge.
        self.assertEqual(response.content.decode(), "1158201444")

    def test_wrong_token_is_refused(self):
        response = self.client.get(WEBHOOK_URL, {
            "hub.mode": "subscribe",
            "hub.verify_token": "guessed",
            "hub.challenge": "1158201444",
        })
        self.assertEqual(response.status_code, 403)


class StatusCallbackTests(WebhookTestCase):
    def setUp(self):
        self.message = WhatsAppMessage.objects.create(
            phone="+2348051234567", wamid="wamid.ABC", status="sent",
            template_name="room_details", sent_at=timezone.now(),
        )

    def _status(self, status, **extra):
        return self._post(_envelope(statuses=[{
            "id": "wamid.ABC",
            "status": status,
            "timestamp": "1718000000",
            "recipient_id": "2348051234567",
            **extra,
        }]))

    def test_sent_then_delivered_then_read(self):
        self.assertEqual(self._status("delivered").status_code, 200)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, "delivered")
        self.assertIsNotNone(self.message.delivered_at)

        self.assertEqual(self._status("read").status_code, 200)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, "read")
        self.assertIsNotNone(self.message.read_at)

    def test_a_late_callback_never_moves_the_row_backwards(self):
        # Meta does not order its callbacks: a stale "sent" arriving after "read"
        # must not undo the "read".
        self._status("read")
        self._status("sent")
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, "read")

    def test_failure_records_metas_error_code(self):
        response = self._status("failed", errors=[{
            "code": 131047,
            "title": "Re-engagement message",
            "message": "(#131047) Re-engagement message",
            "error_data": {"details": "More than 24 hours have passed."},
        }])
        self.assertEqual(response.status_code, 200)

        self.message.refresh_from_db()
        self.assertEqual(self.message.status, "failed")
        self.assertEqual(self.message.error_code, 131047)
        self.assertEqual(self.message.error_title, "Re-engagement message")
        self.assertIsNotNone(self.message.failed_at)

    def test_a_status_for_an_unknown_wamid_is_ignored_quietly(self):
        response = self._post(_envelope(statuses=[
            {"id": "wamid.SOMEONE_ELSE", "status": "delivered", "timestamp": "1718000000"},
        ]))
        self.assertEqual(response.status_code, 200)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, "sent")


class InboundMessageTests(WebhookTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="wa_stop", email="wa_stop@afc.test", password="x",
        )
        self.user.country = "Nigeria"
        self.user.save(update_fields=["country"])
        # Stored in the LOCAL form, which is how a third of AFC's numbers are stored.
        # Matching the inbound sender against it is what makes the opt-out work at all.
        self.profile = UserProfile.objects.create(
            user=self.user, whatsapp_number="08051234567", whatsapp_opt_in=True,
        )

    def _inbound(self, body, wamid="wamid.IN1"):
        return self._post(_envelope(
            contacts=[{"profile": {"name": "Layott"}, "wa_id": "2348051234567"}],
            messages=[{
                "from": "2348051234567",
                "id": wamid,
                "timestamp": "1718000000",
                "type": "text",
                "text": {"body": body},
            }],
        ))

    def test_stop_clears_the_opt_in(self):
        self.assertEqual(self._inbound("STOP").status_code, 200)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.whatsapp_opt_in)

    def test_opt_out_is_case_and_space_insensitive(self):
        self.assertEqual(self._inbound("  stop  ").status_code, 200)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.whatsapp_opt_in)

    def test_portuguese_opt_out_is_honoured(self):
        # The site runs in en/fr/pt; "PARAR" means the same thing as "STOP".
        self._inbound("PARAR")
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.whatsapp_opt_in)

    def test_an_ordinary_reply_does_not_opt_out(self):
        self._inbound("please stop the tournament clock")
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.whatsapp_opt_in)

    def test_the_inbound_message_is_logged_against_the_user(self):
        self._inbound("hello there")
        message = WhatsAppMessage.objects.get(wamid="wamid.IN1")
        self.assertEqual(message.direction, "inbound")
        self.assertEqual(message.body, "hello there")
        self.assertEqual(message.user_id, self.user.pk)
        self.assertEqual(message.phone, "+2348051234567")

    def test_a_redelivered_webhook_logs_the_message_once(self):
        # Meta retries whenever it does not get a 200, so the same wamid can arrive twice.
        self._inbound("hello", wamid="wamid.DUP")
        self._inbound("hello", wamid="wamid.DUP")
        self.assertEqual(WhatsAppMessage.objects.filter(wamid="wamid.DUP").count(), 1)


class MalformedPayloadTests(WebhookTestCase):
    def test_unparseable_body_still_acks(self):
        # Anything other than a 2xx makes Meta escalate its retries, so a bad payload
        # is logged and acknowledged, never surfaced as a 500.
        body = "this is not json"
        digest = hmac.new(APP_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        response = self.client.post(
            WEBHOOK_URL, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=f"sha256={digest}",
        )
        self.assertEqual(response.status_code, 200)

    def test_an_envelope_with_no_changes_acks(self):
        response = self._post({"object": "whatsapp_business_account", "entry": []})
        self.assertEqual(response.status_code, 200)
