"""The send path (afc_whatsapp/client.py + afc_whatsapp/tasks.py).

HTTP is mocked at the boundary: every test patches requests.post inside the client
module, so no test can reach graph.facebook.com. What is asserted is the contract the
rest of the system depends on:
  - a template send records a WhatsAppMessage row carrying Meta's wamid, because the
    wamid is the only thing the status webhook can match on later;
  - the row exists even when the send fails, so a message that never left is still
    visible;
  - Meta's error code and title survive onto the row;
  - the template registry blocks a send that names an unapproved template;
  - an opted-out recipient is never messaged.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from afc_auth.models import UserProfile
from afc_whatsapp.models import WhatsAppMessage, WhatsAppTemplate
from afc_whatsapp.tasks import queue_template, send_whatsapp_message

User = get_user_model()

# A minimal stand-in for requests' Response. Only the three members the client reads.
class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = str(payload)

    def json(self):
        return self._payload


ACCEPTED = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "2348051234567", "wa_id": "2348051234567"}],
    "messages": [{"id": "wamid.TEST123"}],
}

# Meta's real rejection shape for "the 24 hour window has closed".
REJECTED = {
    "error": {
        "message": "(#131047) Re-engagement message",
        "type": "OAuthException",
        "code": 131047,
        "error_data": {"details": "Message failed to send because more than 24 hours "
                                  "have passed since the customer last replied."},
        "error_user_title": "Message not delivered",
        "fbtrace_id": "Axxxx",
    }
}


@override_settings(
    WHATSAPP_PHONE_NUMBER_ID="1234567890",
    WHATSAPP_ACCESS_TOKEN="test-token",
    WHATSAPP_SYNC=True,          # run the task inline, no Celery worker in tests
)
class TemplateSendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="wa_player", email="wa_player@afc.test", password="x",
        )
        self.user.country = "Nigeria"
        self.user.save(update_fields=["country"])
        self.profile = UserProfile.objects.create(
            user=self.user, whatsapp_number="08051234567", whatsapp_opt_in=True,
        )

    @patch("afc_whatsapp.client.requests.post")
    def test_template_send_records_a_row_with_the_wamid(self, mock_post):
        mock_post.return_value = FakeResponse(ACCEPTED)

        message_id = queue_template(
            "08051234567", "room_details", "en_US",
            body_params=["Layott", "DYNASTY CUP", "12345", "pass", "Bermuda"],
            user=self.user, context="room_details",
        )

        message = WhatsAppMessage.objects.get(id=message_id)
        self.assertEqual(message.wamid, "wamid.TEST123")
        self.assertEqual(message.status, "sent")
        self.assertIsNotNone(message.sent_at)
        # The number was normalised from the stored local form before it was sent.
        self.assertEqual(message.phone, "+2348051234567")
        self.assertEqual(message.template_name, "room_details")
        self.assertEqual(message.variables["body"][0], "Layott")
        self.assertEqual(message.user_id, self.user.pk)

    @patch("afc_whatsapp.client.requests.post")
    def test_the_payload_sent_to_meta_is_metas_shape(self, mock_post):
        mock_post.return_value = FakeResponse(ACCEPTED)
        queue_template("+2348051234567", "room_details", "en_US",
                       body_params=["a", "b"], button_payloads=["ack:1"])

        _args, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertEqual(payload["type"], "template")
        self.assertEqual(payload["to"], "2348051234567")   # digits only on the wire
        self.assertEqual(payload["template"]["language"], {"code": "en_US"})
        body = payload["template"]["components"][0]
        self.assertEqual(body["parameters"], [{"type": "text", "text": "a"},
                                              {"type": "text", "text": "b"}])
        button = payload["template"]["components"][1]
        self.assertEqual(button["sub_type"], "quick_reply")
        self.assertEqual(button["index"], 0)
        self.assertEqual(button["parameters"][0]["payload"], "ack:1")
        # Bearer auth, not a proxy API key.
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")

    @patch("afc_whatsapp.client.requests.post")
    def test_a_rejected_send_keeps_metas_error_code_on_the_row(self, mock_post):
        mock_post.return_value = FakeResponse(REJECTED, status_code=400)

        message_id = queue_template("+2348051234567", "room_details", "en_US",
                                    context="room_details")

        message = WhatsAppMessage.objects.get(id=message_id)
        self.assertEqual(message.status, "failed")
        self.assertEqual(message.error_code, 131047)
        self.assertEqual(message.error_title, "Message not delivered")
        self.assertIsNone(message.wamid)

    @patch("afc_whatsapp.client.requests.post")
    def test_an_unusable_number_is_recorded_not_sent(self, mock_post):
        # No country to anchor a local number: the row records why, and Meta is never called.
        message_id = send_whatsapp_message(
            to="08051234567", template_name="room_details", language="en_US",
            context="room_details",
        )
        message = WhatsAppMessage.objects.get(id=message_id)
        self.assertEqual(message.status, "failed")
        self.assertIn("international format", message.error_title)
        mock_post.assert_not_called()

    @patch("afc_whatsapp.client.requests.post")
    def test_an_unapproved_template_is_refused_before_meta_is_called(self, mock_post):
        WhatsAppTemplate.objects.create(name="room_details", language="en_US", approved=False)

        message_id = queue_template("+2348051234567", "room_details", "en_US")

        message = WhatsAppMessage.objects.get(id=message_id)
        self.assertEqual(message.status, "failed")
        self.assertIn("not approved", message.error_title)
        mock_post.assert_not_called()

    @patch("afc_whatsapp.client.requests.post")
    def test_an_approved_template_passes_the_registry(self, mock_post):
        mock_post.return_value = FakeResponse(ACCEPTED)
        WhatsAppTemplate.objects.create(name="room_details", language="en_US", approved=True)

        message_id = queue_template("+2348051234567", "room_details", "en_US")

        self.assertEqual(WhatsAppMessage.objects.get(id=message_id).status, "sent")

    @patch("afc_whatsapp.client.requests.post")
    def test_an_opted_out_recipient_is_never_messaged(self, mock_post):
        self.profile.whatsapp_opt_in = False
        self.profile.save(update_fields=["whatsapp_opt_in"])

        self.assertIsNone(queue_template("08051234567", "room_details", "en_US",
                                         user=self.user))
        mock_post.assert_not_called()
        self.assertEqual(WhatsAppMessage.objects.count(), 0)


@override_settings(WHATSAPP_PHONE_NUMBER_ID="", WHATSAPP_ACCESS_TOKEN="", WHATSAPP_SYNC=True)
class UnconfiguredTests(TestCase):
    @patch("afc_whatsapp.client.requests.post")
    def test_missing_credentials_fail_the_row_without_calling_meta(self, mock_post):
        message_id = send_whatsapp_message(
            to="+2348051234567", template_name="room_details", language="en_US",
        )
        message = WhatsAppMessage.objects.get(id=message_id)
        self.assertEqual(message.status, "failed")
        self.assertIn("not configured", message.error_title)
        mock_post.assert_not_called()
