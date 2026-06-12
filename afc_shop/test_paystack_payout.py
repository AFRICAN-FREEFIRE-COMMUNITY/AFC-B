"""
afc_shop/test_paystack_payout.py
================================================================================
Tests for the BUGFIX (2026-06-12) on the Paystack payout helper `_paystack`
(afc_shop/paystack_payout.py):

Owner bug report: saving a vendor bank on prod failed with
  "Could not save your bank for payouts. Please try again.
   (Paystack request failed: Expecting value: line 1 column 1 (char 0))"
i.e. Paystack (or an edge proxy) returned a NON-JSON body and the raw json-decode
error leaked to the vendor while the logs captured neither the HTTP status nor the
body. `_paystack` now parses the body separately from the network call: a non-JSON
response logs the status + a body snippet and surfaces a readable message; a network
failure surfaces "Could not reach Paystack (...)".

These tests drive the REAL endpoint (POST /shop/vendor/bank/) with a Bearer vendor
token and mock `requests` at the module boundary - no live Paystack is ever touched.

Run: python manage.py test afc_shop
"""
from unittest import mock

import requests as requests_lib
from django.test import TestCase, Client, override_settings

from afc_auth.models import SessionToken, User
from afc_shop.models import Vendor


class _FakeResponse:
    """A minimal stand-in for requests.Response: non-JSON body raising on .json()."""

    def __init__(self, status_code=502, text="<html>Bad gateway</html>", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            # Mirrors what requests raises on an empty/HTML body.
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json


@override_settings(PAYSTACK_SECRET_KEY="sk_test_x")
class PaystackNonJsonGuardTests(TestCase):
    """vendor_save_bank must surface readable errors when Paystack misbehaves."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create(
            username="payvendor", email="payvendor@x.com",
            full_name="Pay Vendor", role="player", password="x",
        )
        self.token = SessionToken.objects.create(user=self.user, token="tok_payvendor")
        self.vendor = Vendor.objects.create(
            user=self.user, display_name="Pay Vendor Co", status="active",
        )

    def _save_bank(self):
        return self.client.post(
            "/shop/vendor/bank/",
            data={"account_number": "0123456789", "bank_code": "058", "bank_name": "GTBank"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    def test_non_json_response_returns_readable_502(self):
        # The resolve step (a GET) returns an HTML 502 page -> the vendor sees a clean
        # message naming the HTTP status, NOT the raw "Expecting value..." decode error.
        with mock.patch(
            "afc_shop.paystack_payout.requests.get",
            return_value=_FakeResponse(status_code=502),
        ):
            resp = self._save_bank()
        self.assertEqual(resp.status_code, 502)
        detail = resp.json().get("detail", "")
        self.assertIn("HTTP 502", detail)
        self.assertNotIn("Expecting value", detail)

    def test_network_error_returns_readable_502(self):
        with mock.patch(
            "afc_shop.paystack_payout.requests.get",
            side_effect=requests_lib.ConnectionError("dns down"),
        ):
            resp = self._save_bank()
        self.assertEqual(resp.status_code, 502)
        self.assertIn("Could not reach Paystack", resp.json().get("detail", ""))

    def test_happy_path_saves_recipient(self):
        # Resolve (GET) and transferrecipient (POST) both succeed -> bank persisted.
        resolve_ok = _FakeResponse(
            status_code=200,
            json_data={"status": True, "data": {"account_name": "PAY VENDOR", "account_number": "0123456789"}},
        )
        recipient_ok = _FakeResponse(
            status_code=200,
            json_data={"status": True, "data": {"recipient_code": "RCP_test123"}},
        )
        with mock.patch("afc_shop.paystack_payout.requests.get", return_value=resolve_ok), \
             mock.patch("afc_shop.paystack_payout.requests.post", return_value=recipient_ok):
            resp = self._save_bank()
        self.assertEqual(resp.status_code, 200)
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.paystack_recipient_code, "RCP_test123")
        self.assertEqual(self.vendor.payout_provider, "paystack")
        self.assertEqual(self.vendor.account_name, "PAY VENDOR")

    def test_missing_key_is_explicit(self):
        with override_settings(PAYSTACK_SECRET_KEY=None):
            resp = self._save_bank()
        self.assertEqual(resp.status_code, 502)
        self.assertIn("not configured", resp.json().get("detail", ""))
