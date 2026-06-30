"""
test_fx.py
──────────
Covers the multi-currency FX layer (afc_auth.fx) — owner 2026-06-30.

No live network: FxRate rows are seeded directly + FX_TTL is made huge so get_rates() never tries to
re-fetch. Verifies the conversion math, the protective CHARGE buffer (so we don't lose money on the
gateway spread), the country->currency map, and the user display-currency resolver.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from afc_auth import fx
from afc_auth.models import FxRate

User = get_user_model()


class FxTests(TestCase):
    def setUp(self):
        # 1 USD = 1500 NGN (seeded; no API call). Fresh so get_rates() won't try to refresh.
        FxRate.objects.create(currency="NGN", rate=Decimal("1500"))
        FxRate.objects.create(currency="GHS", rate=Decimal("12"))
        fx.FX_TTL = timedelta(days=3650)  # disable lazy refresh during tests

    def test_from_usd_display_rate(self):
        # Display: 10 USD -> 15000 NGN at the raw mid-market rate (no buffer).
        self.assertEqual(fx.from_usd(10, "NGN"), Decimal("15000"))

    def test_to_usd(self):
        self.assertEqual(fx.to_usd(15000, "NGN"), Decimal("10"))

    def test_usd_is_identity(self):
        self.assertEqual(fx.from_usd(10, "USD"), Decimal("10"))
        self.assertEqual(fx.to_usd(10, "USD"), Decimal("10"))

    def test_charge_buffer_protects_margin(self):
        # CHARGING: 10 USD -> 15000 × (1 + 0.03) = 15450 NGN, so the gateway spread doesn't lose us money.
        fx.FX_CHARGE_MARKUP = Decimal("0.03")
        self.assertEqual(fx.from_usd_for_charge(10, "NGN"), Decimal("15450.00"))
        # USD charge has no buffer (no conversion).
        self.assertEqual(fx.from_usd_for_charge(10, "USD"), Decimal("10"))

    def test_unknown_currency_falls_back_to_usd_identity(self):
        # No rate for "XYZ" -> don't fabricate a conversion.
        self.assertEqual(fx.from_usd(10, "XYZ"), Decimal("10"))

    def test_country_to_currency(self):
        self.assertEqual(fx.country_to_currency("Nigeria"), "NGN")
        self.assertEqual(fx.country_to_currency("NG"), "NGN")
        self.assertEqual(fx.country_to_currency("Ghana"), "GHS")
        self.assertEqual(fx.country_to_currency("Narnia"), "USD")   # unknown -> USD
        self.assertEqual(fx.country_to_currency(""), "USD")

    def test_user_currency_resolution(self):
        # explicit pick wins
        u = User.objects.create(username="picky", email="p@x.com", preferred_currency="GHS", country="Nigeria")
        self.assertEqual(fx.user_currency(u), "GHS")
        # no pick -> derive from ip_country, then country
        u2 = User.objects.create(username="ipuser", email="i@x.com", ip_country="Kenya", country="Nigeria")
        self.assertEqual(fx.user_currency(u2), "KES")
        u3 = User.objects.create(username="ctry", email="c@x.com", country="Ghana")
        self.assertEqual(fx.user_currency(u3), "GHS")
        # nothing -> USD
        u4 = User.objects.create(username="none", email="n@x.com")
        self.assertEqual(fx.user_currency(u4), "USD")
        self.assertEqual(fx.user_currency(None), "USD")
