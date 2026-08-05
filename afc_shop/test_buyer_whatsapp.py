"""
afc_shop/test_buyer_whatsapp.py
================================================================================
The BUYER's side of the shop WhatsApp channel (afc_shop/buyer_whatsapp.py), both
directions. Sibling of test_vendor_whatsapp.py, which covers the vendor's side.

What these tests are actually protecting:

  1. THE VARIABLE ORDER. Meta freezes a template's body at approval, so the order of
     body_params is a contract with a message the code cannot see. Get it wrong and
     every buyer is told their order number is "Ada" and their total is "#42". Each
     event asserts the exact list, in order.
  2. THE OFF SWITCH. A blank template name must mean "send nothing", so a deployment
     that has not registered these templates stays silent instead of leaving a failed
     message row behind on every order.
  3. NO NUMBER IS NORMAL. Many AFC accounts have no phone at all. That must skip the
     send, not raise, because the send happens inside a fulfilment transition.
  4. THE TAP. The delivery check is only worth sending if the answer is recorded, so a
     "Yes, received" tap stamps buyer_confirmed_at and a "No, not yet" tap must NOT
     (they would be indistinguishable afterwards otherwise).

The outbound tests patch queue_template, the single boundary between this module and
the send pipeline, so nothing goes near the network. The inbound tests drive the REAL
/whatsapp/webhook/ with a REAL Meta HMAC signature, which is the only way to prove the
handler is actually registered in INBOUND_HANDLERS: a handler nobody calls passes every
direct-call test in the world.

Run: ./.venv/Scripts/python.exe manage.py test afc_shop.test_buyer_whatsapp
"""
import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from afc_auth.models import SessionToken

from .fulfilment import apply_acknowledge, apply_mark_shipped
from .models import Category, Order, OrderItem, Product, ProductVariant, Vendor

User = get_user_model()

# The site-wide inbound webhook (afc_whatsapp/urls.py, mounted at "whatsapp/" by afc/urls.py).
WEBHOOK_URL = "/whatsapp/webhook/"
APP_SECRET = "test-app-secret"

# The buyer's number as Meta reports it on an inbound message: digits, no "+".
BUYER_WA_ID = "2348051234567"


def _make_order(phone_number="+2348051234567", country="Nigeria", digital=False,
                profile_number=""):
    """Build a paid order with one line, plus the buyer account behind it.

    Returns (order, buyer). `digital=True` gives the product a non-physical Category
    (a diamonds top-up), which is what changes the "what happens next" sentence. The
    buyer's LOGIN carries the country, because that is what anchors a locally written
    number to a numbering plan (buyer_whatsapp.buyer_country)."""
    buyer = User.objects.create_user(
        username="wa_shopper", email="wa_shopper@afc.test", password="x",
    )
    buyer.country = country
    buyer.save(update_fields=["country"])
    if profile_number:
        # The fallback number, on the buyer's profile rather than the order.
        from afc_auth.models import UserProfile
        UserProfile.objects.create(user=buyer, whatsapp_number=profile_number)

    vendor_user = User.objects.create_user(
        username="wa_seller", email="wa_seller@afc.test", password="x",
    )
    vendor = Vendor.objects.create(user=vendor_user, display_name="Lagos Gear")

    category = Category.objects.create(
        name="Diamonds" if digital else "Apparel", is_physical=not digital,
    )
    product = Product.objects.create(
        name="Free Fire Diamonds" if digital else "AFC Jersey",
        product_type="diamonds" if digital else "bundle",
        category=category,
        vendor=vendor,
    )
    variant = ProductVariant.objects.create(
        product=product, sku="AFC-1", price=Decimal("10000.00"),
    )

    order = Order.objects.create(
        user=buyer, status="paid", total=Decimal("10000.00"),
        first_name="Ada", last_name="Obi", phone_number=phone_number,
        address="12 Admiralty Way", city="Lekki", state="Lagos",
        fulfilment_state="received",
    )
    OrderItem.objects.create(
        order=order, variant=variant, quantity=1,
        unit_price=Decimal("10000.00"), line_total=Decimal("10000.00"),
        product_name_snapshot="Free Fire Diamonds" if digital else "AFC Jersey",
        variant_title_snapshot="" if digital else "Medium",
    )
    return order, buyer


# The three template names are set HERE rather than relied on as defaults. They default to empty
# in settings, because empty means "do not send" and a deployment must not start messaging real
# buyers just because the code landed. A test that leaned on a production default would also be
# testing the default rather than the behaviour, and would break the moment somebody changed it.
@override_settings(
    SHOP_CURRENCY="NGN",
    WHATSAPP_ORDER_RECEIVED_TEMPLATE="order_received",
    WHATSAPP_ORDER_SHIPPED_TEMPLATE="order_shipped",
    WHATSAPP_ORDER_DELIVERED_TEMPLATE="order_delivered_check",
    WHATSAPP_ORDER_TEMPLATE_LANG="en",
)
class BuyerOrderSendTests(TestCase):
    """OUTBOUND: each fulfilment transition sends the right template, right variables."""

    def setUp(self):
        # THE boundary: everything past this point is afc_whatsapp's job and is already
        # covered by its own tests. Patching here proves what we WOULD have sent.
        patcher = mock.patch("afc_shop.buyer_whatsapp.queue_template")
        self.queue_template = patcher.start()
        self.addCleanup(patcher.stop)

        # The buyer emails ride alongside every one of these transitions and would try to
        # reach an SMTP server. They are not what this file is about.
        for target in ("send_order_shipped", "send_order_completed"):
            mail_patcher = mock.patch(f"afc_shop.emails.{target}")
            mail_patcher.start()
            self.addCleanup(mail_patcher.stop)

    def _sent(self):
        """(args, kwargs) of the one queue_template call."""
        self.assertEqual(self.queue_template.call_count, 1)
        return self.queue_template.call_args

    def _complete(self, order):
        """Walk an order to completed, ending on the REAL mark-completed endpoint.

        The last step goes through HTTP on purpose: completion is the only one of the
        three sends that hangs off an endpoint rather than a shared core, so calling
        notify_buyer directly here would prove the message and not the wiring. The
        earlier sends are discarded so the assertions see only the last one."""
        apply_acknowledge(order)
        order.fulfilment_state = "ship_scheduled"
        order.save(update_fields=["fulfilment_state"])
        apply_mark_shipped(order)
        self.queue_template.reset_mock()

        admin = User.objects.create_user(
            username="wa_admin", email="wa_admin@afc.test", password="x", role="admin",
        )
        token = SessionToken.objects.create(user=admin, token="tok_wa_admin").token
        # The vendor payout fires on completion and would try to reach Paystack.
        with mock.patch("afc_shop.paystack_payout.settle_order_payout_paystack"):
            response = self.client.post(
                "/shop/fulfilment/mark-completed/", {"order_id": order.id},
                HTTP_AUTHORIZATION=f"Bearer {token}",
            )
        self.assertEqual(response.status_code, 200)

    def test_acknowledging_an_order_sends_the_received_template(self):
        order, _buyer = _make_order()

        ok, _err = apply_acknowledge(order)
        self.assertTrue(ok)

        args, kwargs = self._sent()
        # Positional: recipient, template name, the language Meta approved ("en", not "en_US").
        self.assertEqual(args[0], "+2348051234567")
        self.assertEqual(args[1], "order_received")
        self.assertEqual(args[2], "en")
        # Body variables {{1}}..{{4}}, IN ORDER: buyer, reference, items, total.
        self.assertEqual(
            kwargs["body_params"],
            ["Ada", f"#{order.id}", "AFC Jersey (Medium) x1", "NGN 10,000.00"],
        )
        # No buttons on this one: it announces, it does not ask.
        self.assertIsNone(kwargs["button_payloads"])
        # The buyer IS an AFC account holder, unlike a vendor, so the send is attributed
        # to them and their profile opt-in applies.
        self.assertEqual(kwargs["user"], order.user)
        self.assertEqual(kwargs["country"], "Nigeria")
        self.assertEqual(kwargs["context"], "buyer_order_received")

    def test_a_buyer_with_no_first_name_falls_back_to_the_username(self):
        order, buyer = _make_order()
        order.first_name = ""
        order.save(update_fields=["first_name"])

        apply_acknowledge(order)

        self.assertEqual(self._sent()[1]["body_params"][0], buyer.username)

    def test_shipping_a_physical_order_describes_the_courier(self):
        order, _buyer = _make_order()
        order.fulfilment_state = "ship_scheduled"
        order.save(update_fields=["fulfilment_state"])

        ok, _err = apply_mark_shipped(order)
        self.assertTrue(ok)

        args, kwargs = self._sent()
        self.assertEqual(args[1], "order_shipped")
        # {{1}} buyer, {{2}} reference, {{3}} what happens next.
        self.assertEqual(kwargs["body_params"][:2], ["Ada", f"#{order.id}"])
        self.assertIn("courier", kwargs["body_params"][2])
        self.assertEqual(len(kwargs["body_params"]), 3)

    def test_shipping_a_digital_order_promises_no_delivery(self):
        # A diamonds order has no courier and no address. Telling that buyer to watch
        # for a parcel is the wrong sentence, which is why {{3}} is derived per order.
        order, _buyer = _make_order(digital=True)
        order.fulfilment_state = "ship_scheduled"
        order.save(update_fields=["fulfilment_state"])

        apply_mark_shipped(order)

        next_step = self._sent()[1]["body_params"][2]
        self.assertIn("diamonds", next_step)
        self.assertNotIn("courier", next_step)

    def test_completing_an_order_asks_the_buyer_with_two_buttons(self):
        order, _buyer = _make_order()

        self._complete(order)

        args, kwargs = self._sent()
        self.assertEqual(args[1], "order_delivered_check")
        self.assertEqual(kwargs["body_params"], ["Ada", f"#{order.id}"])
        # The payloads come back verbatim on a tap and are the only thing tying an
        # answer to an order. Order matches the buttons the approved template declares.
        self.assertEqual(
            kwargs["button_payloads"],
            [f"gotit:{order.id}", f"notyet:{order.id}"],
        )

    def test_a_long_order_summarises_the_extra_lines(self):
        # One chat bubble, and Meta caps a parameter's length, so a big cart is named
        # up to _ITEMS_NAMED lines and then counted.
        order, _buyer = _make_order()
        variant = order.items.first().variant
        for i in range(4):
            OrderItem.objects.create(
                order=order, variant=variant, quantity=1,
                unit_price=Decimal("1.00"), line_total=Decimal("1.00"),
                product_name_snapshot=f"Extra {i}",
            )

        apply_acknowledge(order)

        self.assertTrue(self._sent()[1]["body_params"][2].endswith("and 2 more items"))

    @override_settings(WHATSAPP_ORDER_RECEIVED_TEMPLATE="")
    def test_a_blank_template_name_sends_nothing(self):
        # The off switch for a deployment that has not registered the templates yet.
        # The transition itself must still happen.
        order, _buyer = _make_order()

        ok, _err = apply_acknowledge(order)

        self.assertTrue(ok)
        self.queue_template.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_state, "acknowledged")

    def test_a_buyer_with_no_number_anywhere_is_skipped(self):
        # Common: plenty of accounts have no phone, and a digital order collects none.
        # It must not raise, because this runs inside a committed transition.
        order, _buyer = _make_order(phone_number="")

        ok, _err = apply_acknowledge(order)

        self.assertTrue(ok)
        self.queue_template.assert_not_called()

    def test_the_profile_number_is_used_when_the_order_has_none(self):
        order, _buyer = _make_order(phone_number="", profile_number="+2348051234567")

        apply_acknowledge(order)

        self.assertEqual(self._sent()[0][0], "+2348051234567")


@override_settings(WHATSAPP_APP_SECRET=APP_SECRET)
class BuyerDeliveryCheckReplyTests(TestCase):
    """INBOUND: the buyer's answer to the delivery check reaches the order.

    Posted to the real /whatsapp/webhook/ with a real Meta signature, so this covers
    the seam (afc_whatsapp dispatching into afc_shop.buyer_whatsapp) as well as the
    routing. A handler that is not registered fails here and only here."""

    def setUp(self):
        self.order, self.buyer = _make_order()
        self.order.fulfilment_state = "completed"
        self.order.save(update_fields=["fulfilment_state"])

    def _tap(self, payload, sender=BUYER_WA_ID, shape="button"):
        """POST a signed inbound envelope carrying ONE button tap.

        shape="button"      -> a TEMPLATE quick-reply tap (what notify_buyer's buttons
                               produce): type "button", button.payload.
        shape="interactive" -> a free-form reply-button tap: type "interactive",
                               interactive.button_reply.id."""
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

    def _confirmed_at(self):
        self.order.refresh_from_db()
        return self.order.buyer_confirmed_at

    def test_yes_received_records_the_confirmation(self):
        response = self._tap(f"gotit:{self.order.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self._confirmed_at())

    def test_the_free_form_button_shape_works_too(self):
        self._tap(f"gotit:{self.order.id}", shape="interactive")

        self.assertIsNotNone(self._confirmed_at())

    def test_not_yet_records_nothing(self):
        # A dispute must stay distinguishable from a delivery. It is logged for ops
        # instead, and the timestamp keeps meaning "the buyer confirmed it".
        response = self._tap(f"notyet:{self.order.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._confirmed_at())

    def test_a_second_tap_does_not_move_the_timestamp(self):
        # Meta redelivers a webhook it did not get a 200 for, and a buyer can tap twice.
        # The FIRST confirmation is the true one.
        self._tap(f"gotit:{self.order.id}")
        first = self._confirmed_at()
        self._tap(f"gotit:{self.order.id}")

        self.assertEqual(self._confirmed_at(), first)

    def test_a_tap_from_a_stranger_changes_nothing(self):
        # Anyone can message the AFC business number; only the buyer may close their
        # own order, or a stranger could end the chase for a parcel that never came.
        response = self._tap(f"gotit:{self.order.id}", sender="2349099999999")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._confirmed_at())

    def test_a_vendor_tap_is_left_to_the_vendor_handler(self):
        # Both handlers see every inbound message. The buyer handler must ignore the
        # vendor's action prefixes rather than 500 on them.
        self.assertEqual(self._tap(f"ack:{self.order.id}").status_code, 200)
        self.assertIsNone(self._confirmed_at())

    def test_junk_and_missing_orders_are_ignored(self):
        # A non-200 makes Meta escalate its retries against every AFC message, not just
        # this one, so nothing in here may raise.
        self.assertEqual(self._tap("gotit:99999999").status_code, 200)
        self.assertEqual(self._tap("no-colon-here").status_code, 200)
        self.assertEqual(self._tap("gotit:not-a-number").status_code, 200)
