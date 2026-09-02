# afc_shop/test_vendor_contact.py
# ──────────────────────────────────────────────────────────────────────────────
# A vendor seeing who ordered, and being able to write to them (owner 2026-09-02).
#
# WHAT THESE TESTS ARE ACTUALLY GUARDING
#
#   1. THE RELAXATION IS EXACTLY TWO FIELDS. The PII firewall was deliberate, and the owner
#      reversed it for email and phone ONLY. So there is a test that the withheld things are STILL
#      withheld: account id, payment references, money internals. A "vendor sees the buyer" feature
#      that quietly starts returning the whole Order is a different, much worse feature.
#   2. SCOPE. A vendor sees THEIR orders. The interesting case is not "my order appears", it is
#      "somebody else's does NOT", so both vendors exist in setUp and each is asked what they see.
#   3. THE MESSAGE CHANNEL IS BOUNDED AND RECORDED. Wrong vendor refused, cap enforced, and a row
#      written even when delivery fails, because a log that only records successes cannot answer
#      the question you ask a log.
#
# The email sender is patched as afc_shop.emails.send_email, NOT afc_auth.views.send_email.
# emails.py does `from afc_auth.views import send_email`, which binds the name in THAT module at
# import time, so patching the source module changes nothing the code under test can see. The
# first version of this file made exactly that mistake: the real sender ran, rejected the
# @afc.test domain, and an assertion passed vacuously until a sibling test caught it.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Notifications, SessionToken, User

from .models import (
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Vendor,
    VendorOrderMessage,
)

ORDERS_URL = "/shop/fulfilment/my-orders/"


def _message_url(order_id):
    return f"/shop/fulfilment/orders/{order_id}/message/"


class VendorContactTestBase(TestCase):
    def setUp(self):
        self.client = Client()
        self.buyer = User.objects.create_user(
            username="vc_buyer", email="account@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria", language="en",
        )
        # TWO vendors, because the question worth asking is what the OTHER one can see.
        self.vendor_user = User.objects.create_user(
            username="vc_vendor", email="vendor@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria",
        )
        self.other_user = User.objects.create_user(
            username="vc_other", email="other@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user, display_name="Sweez Supplies", status="active")
        self.other_vendor = Vendor.objects.create(
            user=self.other_user, display_name="Someone Else", status="active")

        self.product = Product.objects.create(name="Jersey", vendor=self.vendor)
        self.variant = ProductVariant.objects.create(
            product=self.product, title="Large", price=Decimal("10000.00"), sku="J-L")

        # The CHECKOUT contact details, which is what a vendor needs and what the owner asked for.
        # Deliberately DIFFERENT from the account email, so the test can tell which one is returned.
        self.order = Order.objects.create(
            user=self.buyer, status="paid", subtotal=Decimal("10000.00"),
            total=Decimal("10750.00"), tax=Decimal("750.00"),
            first_name="Ada", last_name="Obi",
            email="checkout@afc.test", phone_number="+2348012345678",
            address="12 Marina", city="Lagos", state="Lagos", postcode="100001",
            paystack_reference="PSK-SECRET-REF",
        )
        OrderItem.objects.create(
            order=self.order, variant=self.variant, quantity=1,
            unit_price=Decimal("10000.00"), line_total=Decimal("10000.00"),
            product_name_snapshot="Jersey", variant_title_snapshot="Large",
        )

    def _auth(self, user):
        token = SessionToken.objects.create(
            user=user, token=f"vc-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class VendorSeesBuyerContactTests(VendorContactTestBase):
    def test_the_vendor_sees_the_buyer_email_and_phone(self):
        res = self.client.get(ORDERS_URL, **self._auth(self.vendor_user))
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.json()["results"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        # The CHECKOUT email, not the account one: that is the address the buyer gave for this
        # order, and it is where AFC's own order mail goes (emails._recipient).
        self.assertEqual(row["buyer_email"], "checkout@afc.test")
        self.assertEqual(row["buyer_phone"], "+2348012345678")
        self.assertEqual(row["buyer_name"], "Ada Obi")

    def test_the_email_falls_back_to_the_account_address(self):
        # A checkout with no email must not leave the vendor with a blank field when the platform
        # holds a perfectly good address.
        self.order.email = ""
        self.order.save(update_fields=["email"])
        row = self.client.get(ORDERS_URL, **self._auth(self.vendor_user)).json()["results"][0]
        self.assertEqual(row["buyer_email"], "account@afc.test")

    def test_another_vendor_sees_nothing_at_all(self):
        # The point of the scope, and the only test here that would catch a leak.
        res = self.client.get(ORDERS_URL, **self._auth(self.other_user))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["results"], [])

    def test_the_withheld_fields_are_still_withheld(self):
        # The relaxation was TWO fields. This asserts the rest of the firewall is intact, so a
        # later "while we are here" addition has to argue with a red test first.
        row = self.client.get(ORDERS_URL, **self._auth(self.vendor_user)).json()["results"][0]
        for forbidden in (
            "paystack_reference", "paystack_transaction_id", "stripe_session_id",
            "user_id", "buyer_user_id", "subtotal", "total", "tax", "discount_total", "coupon",
        ):
            self.assertNotIn(forbidden, row, f"{forbidden} must not reach a vendor")
        # And no nested money either: a "totals" object would pass the loop above.
        self.assertNotIn("PSK-SECRET-REF", str(row))

    def test_a_non_vendor_is_refused(self):
        res = self.client.get(ORDERS_URL, **self._auth(self.buyer))
        self.assertEqual(res.status_code, 403, res.content)


class VendorMessageTests(VendorContactTestBase):
    def test_the_vendor_can_message_the_buyer(self):
        sent = []
        with patch("afc_shop.emails.send_email",
                   side_effect=lambda *a, **k: sent.append((a, k)) or True):
            res = self.client.post(
                _message_url(self.order.id), {"message": "The large is out of stock, is medium ok?"},
                content_type="application/json", **self._auth(self.vendor_user),
            )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertTrue(body["notified"])
        self.assertTrue(body["emailed"], "the buyer was never emailed")
        self.assertEqual(body["remaining"], VendorOrderMessage.MAX_PER_ORDER - 1)

        row = VendorOrderMessage.objects.get(pk=body["message_id"])
        self.assertEqual(row.order_id, self.order.id)
        self.assertEqual(row.vendor_id, self.vendor.id)
        self.assertEqual(row.sent_by_id, self.vendor_user.user_id)

        note = Notifications.objects.filter(user=self.buyer).first()
        self.assertIsNotNone(note, "the buyer got no in-app notification")
        self.assertIn("out of stock", note.message)
        # The SELLER is named. A message from a third party that reads as if AFC wrote it is worse
        # than no message.
        self.assertIn("Sweez Supplies", note.message)

    def test_the_email_goes_to_the_checkout_address_in_the_buyers_language(self):
        self.buyer.language = "fr"
        self.buyer.save(update_fields=["language"])
        sent = []
        with patch("afc_shop.emails.send_email",
                   side_effect=lambda to, subj, html, **k: sent.append((to, subj, html, k)) or True):
            self.client.post(_message_url(self.order.id), {"message": "Bonjour"},
                             content_type="application/json", **self._auth(self.vendor_user))
        self.assertEqual(len(sent), 1, sent)
        to, subject, html, kwargs = sent[0]
        self.assertEqual(to, "checkout@afc.test")
        self.assertEqual(kwargs.get("language"), "fr")
        # prelocalized, so the machine-translation pass does not run over copy already written in
        # French and put words in the seller's mouth.
        self.assertTrue(kwargs.get("prelocalized"))
        self.assertIn("commande", subject)

    def test_another_vendor_cannot_message_about_this_order(self):
        res = self.client.post(_message_url(self.order.id), {"message": "hello"},
                               content_type="application/json", **self._auth(self.other_user))
        self.assertEqual(res.status_code, 403, res.content)
        self.assertEqual(VendorOrderMessage.objects.count(), 0)

    def test_an_empty_message_is_refused(self):
        res = self.client.post(_message_url(self.order.id), {"message": "   "},
                               content_type="application/json", **self._auth(self.vendor_user))
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(VendorOrderMessage.objects.count(), 0)

    def test_the_per_order_cap_is_enforced_and_names_itself(self):
        with patch("afc_shop.emails.send_email", return_value=True):
            for i in range(VendorOrderMessage.MAX_PER_ORDER):
                res = self.client.post(_message_url(self.order.id), {"message": f"note {i}"},
                                       content_type="application/json",
                                       **self._auth(self.vendor_user))
                self.assertEqual(res.status_code, 201, res.content)
            over = self.client.post(_message_url(self.order.id), {"message": "one too many"},
                                    content_type="application/json",
                                    **self._auth(self.vendor_user))
        self.assertEqual(over.status_code, 429, over.content)
        self.assertIn(str(VendorOrderMessage.MAX_PER_ORDER), over.json()["message"])
        self.assertEqual(VendorOrderMessage.objects.count(), VendorOrderMessage.MAX_PER_ORDER)

    def test_a_failed_email_still_leaves_a_record(self):
        # The reason the row is written BEFORE delivery. A log that only records successes cannot
        # answer the question you go to a log to ask.
        with patch("afc_shop.emails.send_email", side_effect=RuntimeError("smtp down")):
            res = self.client.post(_message_url(self.order.id), {"message": "still recorded"},
                                   content_type="application/json", **self._auth(self.vendor_user))
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(res.json()["emailed"])
        row = VendorOrderMessage.objects.get()
        self.assertEqual(row.message, "still recorded")
        self.assertFalse(row.emailed)
        # The in-app half is independent, so it still landed.
        self.assertTrue(row.notified)

    def test_an_unknown_order_is_a_404(self):
        res = self.client.post(_message_url(999999), {"message": "hello"},
                               content_type="application/json", **self._auth(self.vendor_user))
        self.assertEqual(res.status_code, 404, res.content)
