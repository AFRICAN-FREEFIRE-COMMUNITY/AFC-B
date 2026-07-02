# ── manage.py gemini_healthcheck ────────────────────────────────────────────────
# Owner 2026-07-02: "confirm the Gemini key works" (the OCR verifier). Run this ON PROD to check the
# LIVE key WITHOUT pasting the secret anywhere: it reads settings.GEMINI_API_KEY, makes one tiny
# generateContent call, and prints OK or the exact failure reason. The key is masked in all output.
#
#   python manage.py gemini_healthcheck
#
# Interpreting failures:
#   • API_KEY_INVALID / 400        → the key itself is wrong/rotated. Set a valid GEMINI_API_KEY.
#   • PERMISSION_DENIED + "dunning" → the Google Cloud PROJECT's BILLING is suspended/unpaid (this is
#                                     what the current key hits). Fix billing on that GCP project; the
#                                     key is fine.
#   • SERVICE_DISABLED             → enable the "Generative Language API" for the key's project.
#   • 429 RESOURCE_EXHAUSTED       → quota/rate limit, not a key problem.
#
# CONNECTS TO: afc_ocr/services/gemini.py (same key + model + endpoint the OCR uses via call_gemini).

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from afc_ocr.services.gemini import GEMINI_MODEL, GEMINI_URL_TMPL


class Command(BaseCommand):
    help = "Verify the configured Gemini API key works (the OCR verifier), masking the secret."

    def handle(self, *args, **opts):
        key = getattr(settings, "GEMINI_API_KEY", "") or ""
        model = getattr(settings, "GEMINI_MODEL", "") or GEMINI_MODEL

        masked = f"...{key[-4:]} (len {len(key)})" if key else "NOT SET"
        self.stdout.write(f"GEMINI_API_KEY: {masked}")
        self.stdout.write(f"model:          {model}")

        if not key:
            self.stdout.write(self.style.ERROR("FAIL: no GEMINI_API_KEY configured."))
            return

        url = GEMINI_URL_TMPL.format(model=model, api_key=key)
        try:
            r = requests.post(
                url,
                json={"contents": [{"parts": [{"text": "Reply with the single word OK"}]}]},
                timeout=30,
            )
        except Exception as ex:  # network/DNS/timeout
            self.stdout.write(self.style.ERROR(f"FAIL: request error: {ex!r}"))
            return

        if r.status_code == 200:
            try:
                txt = (
                    r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                )
            except Exception:
                txt = "(200 but unexpected body)"
            self.stdout.write(self.style.SUCCESS(f"OK: key works. Model replied: {txt[:60]!r}"))
            return

        # Non-200: surface the exact status + reason so ops knows key-vs-billing-vs-quota.
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {}
        status = err.get("status") or f"HTTP {r.status_code}"
        message = (err.get("message") or "")[:300]
        self.stdout.write(self.style.ERROR(f"FAIL: HTTP {r.status_code} {status}"))
        if message:
            self.stdout.write(f"reason: {message}")
        if "dunning" in message.lower() or "billing" in message.lower():
            self.stdout.write(
                self.style.WARNING(
                    "→ This is a BILLING problem on the key's Google Cloud project, not a bad key. "
                    "Settle the billing on that GCP project and the key will work again."
                )
            )
