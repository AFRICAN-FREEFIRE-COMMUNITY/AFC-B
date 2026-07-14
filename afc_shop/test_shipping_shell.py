"""
test_shipping_shell.py
──────────────────────
Covers the SAFE-WHEN-DISABLED contract of the provider-agnostic shipping shell
(afc_shop/services/shipping.py + views_shipping.py). Owner ask 2026-06-29.

The shell must be completely inert until a provider + key are configured:
  - get_provider() returns None,
  - quote_rates() reports enabled=False (never raises),
  - book_shipment() is a no-op,
  - POST /shop/shipping/quote/ 200s with {"enabled": false, "couriers": []}.

It must also report enabled=False when a provider name is set but no client is wired
yet (the deferred provider-client state) so a half-configured prod env can't crash
checkout. No network is touched in any of these paths.
"""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from afc_auth.models import User, SessionToken
from afc_auth.views import generate_session_token
from afc_shop.services import shipping


class ShippingShellDisabledTests(TestCase):
    def test_get_provider_none_when_unconfigured(self):
        with override_settings(SHIPPING_PROVIDER="", SHIPPING_API_KEY=None):
            self.assertIsNone(shipping.get_provider())

    def test_get_provider_none_when_provider_set_but_no_key(self):
        # Half-configured (name but no key) must stay disabled, not error.
        with override_settings(SHIPPING_PROVIDER="terminal", SHIPPING_API_KEY=None):
            self.assertIsNone(shipping.get_provider())

    def test_get_provider_none_when_client_not_wired(self):
        # Provider + key set, but no concrete client exists yet (deferred) -> still None.
        with override_settings(SHIPPING_PROVIDER="terminal", SHIPPING_API_KEY="sk_test_x"):
            self.assertIsNone(shipping.get_provider())

    def test_quote_rates_disabled_and_never_raises(self):
        with override_settings(SHIPPING_PROVIDER="", SHIPPING_API_KEY=None):
            quote = shipping.quote_rates({"state": "Lagos"}, [])
            self.assertFalse(quote.enabled)
            self.assertEqual(quote.couriers, [])
            self.assertEqual(quote.to_dict()["enabled"], False)

    def test_book_shipment_noop_when_disabled(self):
        with override_settings(SHIPPING_PROVIDER="", SHIPPING_API_KEY=None):
            self.assertIsNone(shipping.book_shipment(object()))


class ShippingQuoteEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create(
            username="buyer", email="buyer@example.com", password="x", full_name="Buyer",
        )
        self.token = generate_session_token()
        SessionToken.objects.create(user=self.user, token=self.token)

    def _post(self):
        return self.client.post(
            "/shop/shipping/quote/",
            {"address": "1 Test St", "city": "Ikeja", "state": "Lagos",
             "postcode": "100001", "items": [{"variant_id": 1, "quantity": 1}]},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_quote_requires_auth(self):
        resp = self.client.post("/shop/shipping/quote/", {}, format="json")
        self.assertEqual(resp.status_code, 400)  # no Bearer header

    @override_settings(SHIPPING_PROVIDER="", SHIPPING_API_KEY=None)
    def test_quote_returns_disabled_when_unconfigured(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["enabled"], False)
        self.assertEqual(resp.data["couriers"], [])
