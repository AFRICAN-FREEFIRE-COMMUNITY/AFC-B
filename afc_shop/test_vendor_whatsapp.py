"""
afc_shop/test_vendor_whatsapp.py
================================================================================
The marketplace vendor WhatsApp channel, both directions, after the Kapso cutover
(2026-08-03). Covers the three things the channel is trusted to do:

  1. OUTBOUND: a paid vendor order queues the approved "vendor_new_order" template
     with the right body variables and the right three button payloads, through
     afc_whatsapp (AFC's own Meta Cloud API integration), not through any middleman.
  2. NORMALISATION: a vendor number stored the way vendors actually type it
     ("0805 123 4567") reaches Meta as "+2348051234567". Kapso's normaliser only
     stripped punctuation and shipped "8051234567", which is a different subscriber
     or nothing at all. This is the regression the cutover exists to fix.
  3. INBOUND: a vendor's button tap still drives the SAME fulfilment transition it
     drove before, now arriving at the ONE site-wide webhook (/whatsapp/webhook/)
     and dispatched into afc_shop/vendor_whatsapp.py.

The inbound tests drive the REAL endpoint with the Django test client and a REAL
Meta HMAC signature, so they exercise the whole path (signature -> envelope walk ->
app dispatch -> state machine) rather than calling the handler directly. Meta is
never touched: the outbound tests patch afc_whatsapp.client.send_template, which is
the single function that would put a request on the wire.

Run: AFC_TEST_DB_NAME=test_afc_shopwa ./.venv/Scripts/python.exe manage.py test afc_shop.test_vendor_whatsapp
"""
import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from afc_whatsapp.models import WhatsAppMessage

from .fulfilment import notify_order_paid, notify_vendor
from .models import Order, OrderItem, Product, ProductVariant, Vendor

User = get_user_model()

# The site-wide inbound webhook (afc_whatsapp/urls.py, mounted at /whatsapp/ by
# afc/urls.py). The marketplace no longer has an endpoint of its own.
WEBHOOK_URL = "/whatsapp/webhook/"
APP_SECRET = "test-app-secret"

# The vendor's number as Meta reports it on an inbound message: digits, no "+".
VENDOR_WA_ID = "2348051234567"


def _make_vendor_order(whatsapp_number="+2348051234567", country="Nigeria"):
    """Build a paid marketplace order: buyer, vendor, vendor product, one line.

    Returns (order, vendor). The vendor's LOGIN carries the country, because that is
    what anchors a locally-written number to a numbering plan (fulfilment.vendor_country)."""
    buyer = User.objects.create_user(
        username="wa_buyer", email="wa_buyer@afc.test", password="x",
    )
    buyer.first_name, buyer.last_name = "Ada", "Obi"
    buyer.save()

    vendor_user = User.objects.create_user(
        username="wa_vendor", email="wa_vendor@afc.test", password="x",
    )
    vendor_user.country = country
    vendor_user.save(update_fields=["country"])

    vendor = Vendor.objects.create(
        user=vendor_user,
        display_name="Lagos Gear",
        # contact_email deliberately blank: the email heads-up is a separate concern
        # and leaving it unset keeps these tests off the mail path entirely.
        whatsapp_number=whatsapp_number,
    )

    product = Product.objects.create(
        name="AFC Jersey", product_type="bundle", vendor=vendor,
    )
    variant = ProductVariant.objects.create(
        product=product, sku="AFC-JERSEY-M", price=Decimal("10000.00"),
    )

    order = Order.objects.create(
        user=buyer, status="paid", total=Decimal("10000.00"),
        first_name="Ada", last_name="Obi",
        address="12 Admiralty Way", city="Lekki", state="Lagos",
    )
    OrderItem.objects.create(
        order=order, variant=variant, quantity=1,
        unit_price=Decimal("10000.00"), line_total=Decimal("10000.00"),
        product_name_snapshot="AFC Jersey",
    )
    return order, vendor


# WHATSAPP_SYNC=True runs the send inline instead of handing it to a Celery worker,
# so the assertions can read the outcome straight after the call (there is no broker
# in the test environment; without this the dispatch is swallowed and nothing sends).
@override_settings(WHATSAPP_SYNC=True)
class VendorOrderSendTests(TestCase):
    """OUTBOUND: notify_vendor / notify_order_paid -> afc_whatsapp -> Meta."""

    def setUp(self):
        # The one function that would talk to graph.facebook.com. Patching it here
        # proves what we WOULD have sent, without a network call.
        patcher = mock.patch("afc_whatsapp.client.send_template")
        self.send_template = patcher.start()
        self.addCleanup(patcher.stop)
        self.send_template.return_value = {"ok": True, "wamid": "wamid.TEST"}

    def test_a_paid_vendor_order_sends_the_approved_template(self):
        order, vendor = _make_vendor_order()

        # The real entry point both paid paths (Paystack, Stripe) call. The buyer
        # email is patched out: it is not what this test is about and it would try
        # to reach an SMTP server.
        with mock.patch("afc_shop.emails.send_order_received"):
            notify_order_paid(order)

        self.assertEqual(self.send_template.call_count, 1)
        args, kwargs = self.send_template.call_args

        # Positional: recipient, template name, approved language.
        self.assertEqual(args[0], "+2348051234567")
        self.assertEqual(args[1], "vendor_new_order")
        self.assertEqual(args[2], "en_US")

        # Body variables {{1}}..{{4}}, IN ORDER: vendor, order number, buyer, address.
        self.assertEqual(
            kwargs["body_params"],
            ["Lagos Gear", str(order.id), "Ada Obi", "12 Admiralty Way, Lekki, Lagos"],
        )

        # Button payloads, in the order the approved template declares its buttons.
        # These are echoed back verbatim on a tap and are how it maps to THIS order.
        self.assertEqual(
            kwargs["button_payloads"],
            [f"ack:{order.id}", f"shipdate:{order.id}", f"shipped:{order.id}"],
        )

    def test_the_send_is_recorded_on_the_message_log(self):
        # The whole point of owning the integration: "did the vendor get it?" is now
        # an answerable question. Kapso recorded nothing.
        order, vendor = _make_vendor_order()
        notify_vendor(order)

        message = WhatsAppMessage.objects.get(template_name="vendor_new_order")
        self.assertEqual(message.phone, "+2348051234567")
        self.assertEqual(message.status, "sent")
        self.assertEqual(message.wamid, "wamid.TEST")
        self.assertEqual(message.context, "vendor_order_received")
        # A vendor is messaged on their business number, not as an AFC account holder.
        self.assertIsNone(message.user_id)
        self.assertEqual(message.variables["body"][0], "Lagos Gear")
        self.assertEqual(message.variables["buttons"][0], f"ack:{order.id}")

    def test_a_locally_written_number_is_normalised_to_e164(self):
        # THE Kapso regression: "0805 123 4567" is how a third of AFC's numbers are
        # stored. Kapso stripped punctuation and sent "8051234567". The country on the
        # vendor's account is what lets to_e164 resolve it properly.
        order, _vendor = _make_vendor_order(whatsapp_number="0805 123 4567")
        notify_vendor(order)

        self.assertEqual(self.send_template.call_args[0][0], "+2348051234567")
        self.assertEqual(
            WhatsAppMessage.objects.get(template_name="vendor_new_order").phone,
            "+2348051234567",
        )

    def test_a_number_that_cannot_be_resolved_fails_visibly(self):
        # No country on the account, so a local number could belong to any of a dozen
        # numbering plans. Refusing beats messaging a stranger, and the reason is
        # recorded rather than lost.
        order, _vendor = _make_vendor_order(whatsapp_number="08051234567", country="")
        notify_vendor(order)

        self.send_template.assert_not_called()
        message = WhatsAppMessage.objects.get(template_name="vendor_new_order")
        self.assertEqual(message.status, "failed")
        self.assertIn("international format", message.error_title)

    def test_a_vendor_with_no_whatsapp_number_is_not_messaged(self):
        order, _vendor = _make_vendor_order(whatsapp_number="")
        notify_vendor(order)

        self.send_template.assert_not_called()
        self.assertFalse(WhatsAppMessage.objects.exists())


@override_settings(WHATSAPP_APP_SECRET=APP_SECRET)
class VendorButtonTapTests(TestCase):
    """INBOUND: a tap on the AFC number drives the fulfilment state machine.

    Posted to the real /whatsapp/webhook/ with a real Meta signature, so the test
    covers the seam (afc_whatsapp dispatching into afc_shop) as well as the routing."""

    def setUp(self):
        self.order, self.vendor = _make_vendor_order()
        # The state a just-paid marketplace order lands in.
        self.order.fulfilment_state = "received"
        self.order.save(update_fields=["fulfilment_state"])

    def _tap(self, payload, sender=VENDOR_WA_ID, shape="button"):
        """POST a signed inbound envelope carrying ONE button tap.

        shape="button"      -> a TEMPLATE quick-reply tap (what notify_vendor's
                               buttons produce): type "button", button.payload.
        shape="interactive" -> a free-form reply-button tap: type "interactive",
                               interactive.button_reply.id. Nothing sends these
                               today, but a tap on an older message still arrives."""
        if shape == "button":
            message = {"type": "button", "button": {"payload": payload, "text": "Tap"}}
        else:
            message = {
                "type": "interactive",
                "interactive": {"type": "button_reply",
                                "button_reply": {"id": payload, "title": "Tap"}},
            }
        message.update({"from": sender, "id": f"wamid.{payload}.{sender}",
                        "timestamp": "1718000000"})

        body = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{
                "field": "messages",
                "value": {"messaging_product": "whatsapp",
                          "metadata": {"phone_number_id": "1234567890"},
                          "messages": [message]},
            }]}],
        })
        digest = hmac.new(APP_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        return self.client.post(
            WEBHOOK_URL, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=f"sha256={digest}",
        )

    def _state(self):
        self.order.refresh_from_db()
        return self.order.fulfilment_state

    def test_an_ack_tap_acknowledges_the_order(self):
        response = self._tap(f"ack:{self.order.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._state(), "acknowledged")
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.acknowledged_at)

    def test_the_free_form_button_shape_works_too(self):
        self._tap(f"ack:{self.order.id}", shape="interactive")
        self.assertEqual(self._state(), "acknowledged")

    def test_a_tap_from_a_stranger_changes_nothing(self):
        # Anyone can message the AFC business number. Only the order's OWN vendor may
        # move it, or one vendor could fulfil another's orders.
        response = self._tap(f"ack:{self.order.id}", sender="2349099999999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._state(), "received")

    def test_an_out_of_order_tap_is_refused(self):
        # "Mark shipped" before a ship date was ever set: the state machine's own
        # guard rejects it, so a mistaken tap is a harmless no-op.
        self._tap(f"shipped:{self.order.id}")
        self.assertEqual(self._state(), "received")

    def test_a_shipdate_tap_leaves_the_state_alone(self):
        # A quick-reply button cannot carry a date, so this only prompts.
        self._tap(f"shipdate:{self.order.id}")
        self.assertEqual(self._state(), "received")

    def test_a_payload_that_is_not_ours_is_ignored(self):
        # Another app's button, or junk. It must not crash the shared webhook: a
        # non-200 makes Meta escalate its retries against every AFC message.
        self.assertEqual(self._tap("roomdetails:99").status_code, 200)
        self.assertEqual(self._tap("no-colon-here").status_code, 200)
        self.assertEqual(self._state(), "received")

    def test_a_tap_for_a_missing_order_is_ignored(self):
        self.assertEqual(self._tap("ack:99999999").status_code, 200)

    def test_the_tap_is_still_logged_as_an_inbound_message(self):
        # afc_whatsapp's own duties run whatever the marketplace does with the tap:
        # the inbound row is what defines the 24 hour service window.
        self._tap(f"ack:{self.order.id}")
        message = WhatsAppMessage.objects.get(wamid=f"wamid.ack:{self.order.id}.{VENDOR_WA_ID}")
        self.assertEqual(message.direction, "inbound")


@override_settings(WHATSAPP_APP_SECRET=APP_SECRET)
class VendorEvidencePhotoTests(TestCase):
    """INBOUND: a photo from a vendor mid-shipment is stored as proof of dispatch.

    The media bytes used to be fetched through the Kapso proxy; they now come from
    Meta directly (afc_shop/services/whatsapp_media.py), which is the only part
    patched here."""

    def setUp(self):
        self.order, self.vendor = _make_vendor_order()
        self.order.fulfilment_state = "ship_scheduled"
        self.order.save(update_fields=["fulfilment_state"])

        patcher = mock.patch("afc_shop.vendor_whatsapp.download_media")
        self.download = patcher.start()
        self.addCleanup(patcher.stop)
        self.download.return_value = {
            "ok": True, "content": b"jpeg-bytes", "mime_type": "image/jpeg",
        }

    def _photo(self, sender=VENDOR_WA_ID):
        body = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{
                "field": "messages",
                "value": {"messaging_product": "whatsapp",
                          "metadata": {"phone_number_id": "1234567890"},
                          "messages": [{
                              "from": sender, "id": f"wamid.PHOTO.{sender}",
                              "timestamp": "1718000000", "type": "image",
                              "image": {"id": "media-123", "mime_type": "image/jpeg"},
                          }]},
            }]}],
        })
        digest = hmac.new(APP_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        return self.client.post(
            WEBHOOK_URL, data=body, content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=f"sha256={digest}",
        )

    def test_a_vendor_photo_becomes_fulfilment_evidence(self):
        self.assertEqual(self._photo().status_code, 200)
        self.download.assert_called_once_with("media-123")

        evidence = self.order.evidence.get()
        self.assertEqual(evidence.kind, "image")
        self.assertIsNone(evidence.uploaded_by)
        self.assertIn("whatsapp_order", evidence.media.name)
        self.addCleanup(evidence.media.delete, save=False)

    def test_a_photo_from_a_stranger_is_ignored(self):
        self.assertEqual(self._photo(sender="2349099999999").status_code, 200)
        self.download.assert_not_called()
        self.assertEqual(self.order.evidence.count(), 0)
