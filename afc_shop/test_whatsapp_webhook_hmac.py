"""
afc_shop/test_whatsapp_webhook_hmac.py
================================================================================
Tests for the OPTIONAL HMAC signature check (2026-06-12 hardening) on the WhatsApp
inbound webhook (afc_shop/whatsapp_webhook.py _verify_signature):

  - settings.KAPSO_WEBHOOK_SECRET set + a CORRECT "X-Webhook-Signature" header
    (HMAC-SHA256 hex of the raw body) -> the POST is accepted (200, processed).
  - secret set + a WRONG signature  -> rejected 403 before any parsing.
  - secret set + NO signature header -> rejected 403 (unsigned calls are forgeries
    once a secret is configured).
  - secret NOT configured -> unsigned POSTs keep working exactly as before
    (backward compatible; the pre-hardening behaviour).

These drive the REAL endpoint (POST /shop/whatsapp/webhook/, mounted in
afc_shop/urls.py) with the Django test Client - the same all-the-way-through style
as afc_shop/test_paystack_payout.py. No Kapso/Meta is ever touched: the payload is
an empty Meta envelope ({"entry": []}), so the handler verifies + parses + walks
zero messages and acks 200, which is exactly the seam the signature gate sits on.

Run: python manage.py test afc_shop.test_whatsapp_webhook_hmac
"""
import hashlib
import hmac
import json

from django.test import TestCase, Client, override_settings

# The shared secret used by the signed-webhook tests; the signature below is keyed
# with it via override_settings(KAPSO_WEBHOOK_SECRET=WEBHOOK_SECRET).
WEBHOOK_SECRET = "kapso_test_secret"

# The webhook route (afc_shop/urls.py "whatsapp/webhook/" under the /shop/ prefix).
WEBHOOK_URL = "/shop/whatsapp/webhook/"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Compute the signature the way the sender (Kapso/proxy) is documented to:
    HMAC-SHA256 hex digest of the RAW request body, keyed with the shared secret.
    Mirrors _verify_signature in afc_shop/whatsapp_webhook.py exactly."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WhatsappWebhookHmacTests(TestCase):
    """POST /shop/whatsapp/webhook/ must honour the optional HMAC signature gate."""

    def setUp(self):
        self.client = Client()
        # A minimal, valid Meta envelope: zero messages, so the handler acks 200
        # without needing any Order/Vendor rows. json.dumps gives us the EXACT raw
        # bytes the signature is computed over.
        self.body = json.dumps({"entry": []}).encode()

    def _post(self, signature=None):
        """POST the raw envelope, optionally with an X-Webhook-Signature header."""
        extra = {}
        if signature is not None:
            extra["HTTP_X_WEBHOOK_SIGNATURE"] = signature
        return self.client.post(
            WEBHOOK_URL,
            data=self.body,
            content_type="application/json",
            **extra,
        )

    @override_settings(KAPSO_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_valid_signature_passes(self):
        # A correctly signed POST is processed and acked 200 like before.
        resp = self._post(signature=_sign(self.body))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("received"))

    @override_settings(KAPSO_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_invalid_signature_rejected_403(self):
        # A signature keyed with the WRONG secret (or over different bytes) is a
        # forgery: rejected 403, nothing processed.
        resp = self._post(signature=_sign(self.body, secret="attacker_guess"))
        self.assertEqual(resp.status_code, 403)
        self.assertIn("signature", resp.json().get("message", "").lower())

    @override_settings(KAPSO_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_missing_signature_rejected_403(self):
        # Once a secret is configured, an UNSIGNED POST is treated as a forgery too.
        resp = self._post(signature=None)
        self.assertEqual(resp.status_code, 403)

    @override_settings(KAPSO_WEBHOOK_SECRET=None)
    def test_no_secret_configured_passes_unsigned(self):
        # BACKWARD COMPATIBILITY: with no secret configured (the default, e.g. local
        # dev), unsigned POSTs behave exactly as before the hardening.
        resp = self._post(signature=None)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("received"))
