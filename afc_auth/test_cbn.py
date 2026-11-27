"""
test_cbn.py
───────────
Covers the CBN rates layer (afc_auth.cbn) and its plug-in to afc_auth.fx.refresh_fx_rates()
(owner 2026-11-27, inbox #649: "CBN where it can").

No network: fixtures are small JSON strings shaped like the real CBN answers and requests.get is
mocked by URL. Checks parsing, the label map, USD from NFEM vs the selling-rate fallback, a
conflicting day being skipped, the latest date winning, the override math, CBN or provider being down,
and the /auth/fx-rates/ answer carrying sources and credit.
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from afc_auth import cbn, fx
from afc_auth.models import FxRate

RATES = json.loads("""[
  {"id": 1, "currency": "US DOLLAR", "ratedate": "2026-11-26", "buyingrate": "1386.8883", "centralrate": "1387.3883", "sellingrate": "1387.8883"},
  {"id": 2, "currency": "EURO\\t", "ratedate": "2026-11-26", "buyingrate": "1599.0000", "centralrate": "1600.0000", "sellingrate": "1601.0000"},
  {"id": 3, "currency": "EURO", "ratedate": "2026-11-25", "buyingrate": "1500.0000", "centralrate": "1510.0000", "sellingrate": "1520.0000"},
  {"id": 4, "currency": "POUND STERLING", "ratedate": "2026-11-26", "buyingrate": "1819.0000", "centralrate": "1820.5309", "sellingrate": "1821.0000"},
  {"id": 5, "currency": "CFA", "ratedate": "2026-11-26", "buyingrate": "2.0000", "centralrate": "2.4000", "sellingrate": "2.5000"},
  {"id": 6, "currency": "NAIRA", "ratedate": "2026-11-26", "buyingrate": "1.0000", "centralrate": "1.0000", "sellingrate": "1.0000"},
  {"id": 7, "currency": "SDR", "ratedate": "2026-11-26", "buyingrate": "1900.0000", "centralrate": "1910.0000", "sellingrate": "1920.0000"},
  {"id": 8, "currency": "YEN ", "ratedate": "2026-11-26", "buyingrate": "8.0000", "centralrate": "9.0000", "sellingrate": "10.0000"},
  {"id": 9, "currency": "YEN", "ratedate": "2026-11-26", "buyingrate": "8.0000", "centralrate": "9.0000", "sellingrate": "10.0000"}
]""")
NFEM = json.loads("""[
  {"id": 10, "ratedate": "November-26-2026", "weightedAvgRate": "1387.0000", "closingrate": "1388.0000"},
  {"id": 11, "ratedate": "November-25-2026", "weightedAvgRate": "1380.0000", "closingrate": "1381.0000"}
]""")


def _fake_get(rates, nfem, provider=None):
    """requests.get stand-in routed by URL; a value of None means that source is down."""
    def get(url, **kwargs):
        if url == cbn.CBN_RATES_URL:
            body = rates
        elif url == cbn.CBN_NFEM_URL:
            body = nfem
        else:
            body = provider
        if body is None:
            raise RuntimeError("down")
        resp = mock.Mock()
        resp.json.return_value = body
        resp.raise_for_status.return_value = None
        return resp
    return get


class ParseTests(TestCase):
    def test_usd_uses_nfem_for_its_date_and_latest_date_wins(self):
        out = cbn.parse_rates(RATES, NFEM)
        self.assertEqual(out["USD"], (Decimal("1387.0000"), date(2026, 11, 26)))
        # EUR has rows for the 25th and the 26th: the 26th, central rate, label trimmed of its tab.
        self.assertEqual(out["EUR"], (Decimal("1600.0000"), date(2026, 11, 26)))

    def test_usd_falls_back_to_selling_without_nfem_row(self):
        out = cbn.parse_rates(RATES, [])
        self.assertEqual(out["USD"], (Decimal("1387.8883"), date(2026, 11, 26)))

    def test_label_map_skips_unmapped_and_merges_identical_duplicates(self):
        out = cbn.parse_rates(RATES, NFEM)
        self.assertEqual(out["GBP"][0], Decimal("1820.5309"))
        self.assertEqual(out["XOF"][0], Decimal("2.4000"))
        self.assertEqual(out["JPY"][0], Decimal("9.0000"))  # two identical YEN rows merged, not a conflict
        self.assertNotIn("NGN", out)
        self.assertNotIn("XDR", out)
        self.assertEqual(set(out), {"USD", "EUR", "GBP", "XOF", "JPY"})

    def test_conflicting_day_is_skipped_for_the_day_before(self):
        rows = RATES + [{"id": 20, "currency": "EURO", "ratedate": "2026-11-26",
                         "buyingrate": "1700.0000", "centralrate": "1754.4258", "sellingrate": "1800.0000"}]
        out = cbn.parse_rates(rows, NFEM)
        self.assertEqual(out["EUR"], (Decimal("1510.0000"), date(2026, 11, 25)))

    def test_bad_rows_are_ignored(self):
        rows = [{"currency": "EURO", "ratedate": "nope", "buyingrate": "1", "centralrate": "1", "sellingrate": "1"},
                {"currency": "EURO", "ratedate": "2026-11-26", "buyingrate": "x", "centralrate": "1", "sellingrate": "1"},
                "junk"]
        self.assertEqual(cbn.parse_rates(rows, ["junk"]), {})

    def test_nfem_date(self):
        self.assertEqual(cbn.nfem_date("November-03-2026"), date(2026, 11, 3))
        self.assertIsNone(cbn.nfem_date("Smarch-03-2026"))

    def test_fetch_fail_soft(self):
        with mock.patch("afc_auth.cbn.requests.get", side_effect=RuntimeError("down")):
            self.assertEqual(cbn.fetch_cbn_rates(), {})


class RefreshTests(TestCase):
    PROVIDER = {"result": "success", "rates": {"USD": 1, "NGN": 1500, "EUR": 0.9, "GHS": 12}}

    def _refresh(self, rates, nfem, provider):
        with mock.patch("requests.get", side_effect=_fake_get(rates, nfem, provider)):
            return fx.refresh_fx_rates()

    def test_cbn_overrides_provider_and_others_keep_provider(self):
        self._refresh(RATES, NFEM, self.PROVIDER)
        ngn = FxRate.objects.get(currency="NGN")
        self.assertEqual((ngn.rate, ngn.source, ngn.rate_date), (Decimal("1387.00000000"), "cbn", date(2026, 11, 26)))
        eur = FxRate.objects.get(currency="EUR")
        # 1387 / 1600 = 0.866875
        self.assertEqual((eur.rate, eur.source), (Decimal("0.86687500"), "cbn"))
        self.assertEqual(FxRate.objects.get(currency="USD").rate, Decimal("1"))
        ghs = FxRate.objects.get(currency="GHS")
        self.assertEqual((ghs.rate, ghs.source, ghs.rate_date), (Decimal("12"), "provider", None))

    def test_override_math_with_selling_fallback(self):
        # No NFEM: USD 1387.8883 (selling); EUR 1600 -> 0.8674...
        self._refresh(RATES, [], self.PROVIDER)
        self.assertEqual(FxRate.objects.get(currency="NGN").rate, Decimal("1387.88830000"))
        self.assertEqual(FxRate.objects.get(currency="EUR").rate, Decimal("0.86743019"))

    def test_cbn_down_keeps_last_good_cbn_rows(self):
        self._refresh(RATES, NFEM, self.PROVIDER)
        self._refresh(None, None, self.PROVIDER)
        ngn = FxRate.objects.get(currency="NGN")
        self.assertEqual((ngn.rate, ngn.source), (Decimal("1387.00000000"), "cbn"))  # provider's 1500 did not win
        self.assertEqual(FxRate.objects.get(currency="EUR").source, "cbn")

    def test_a_cbn_row_the_cbn_stopped_publishing_goes_back_to_the_provider(self):
        # A CBN row dated weeks ago, for a currency today's CBN feed does not carry: the provider takes it over.
        FxRate.objects.create(currency="GHS", rate=Decimal("9.9"), source="cbn",
                              rate_date=timezone.localdate() - timedelta(days=fx.CBN_STALE_DAYS + 5))
        self._refresh(RATES, NFEM, self.PROVIDER)
        self.assertEqual(FxRate.objects.get(currency="GHS").source, "provider")
        # a recent CBN row for a currency missing today still holds (a short gap keeps the last good rate)
        FxRate.objects.filter(currency="GHS").update(source="cbn", rate=Decimal("9.9"), rate_date=timezone.localdate())
        self._refresh(RATES, NFEM, self.PROVIDER)
        self.assertEqual(FxRate.objects.get(currency="GHS").rate, Decimal("9.90000000"))

    def test_provider_down_still_writes_cbn_rows(self):
        self._refresh(RATES, NFEM, None)
        self.assertEqual(FxRate.objects.get(currency="NGN").source, "cbn")
        self.assertFalse(FxRate.objects.filter(currency="GHS").exists())

    def test_both_down_changes_nothing(self):
        self.assertEqual(self._refresh(None, None, None), {})
        self.assertEqual(FxRate.objects.count(), 0)

    def test_cbn_without_usd_is_not_used(self):
        rows = [r for r in RATES if r["currency"] != "US DOLLAR"]
        self._refresh(rows, NFEM, self.PROVIDER)
        self.assertEqual(FxRate.objects.get(currency="NGN").source, "provider")

    def test_default_ttl_is_hourly(self):
        # test_fx.py widens fx.FX_TTL globally while it runs; the shipped default is in the source text.
        import inspect
        self.assertIn("FX_TTL = timedelta(hours=1)", inspect.getsource(fx))


class FxRatesEndpointTests(TestCase):
    def setUp(self):
        fx.FX_TTL = timedelta(days=3650)  # no lazy refresh
        FxRate.objects.create(currency="NGN", rate=Decimal("1387"), source="cbn", rate_date=date(2026, 11, 26))
        FxRate.objects.create(currency="GHS", rate=Decimal("12"))

    def test_answer_carries_sources_and_credit_and_old_fields(self):
        resp = self.client.get("/auth/fx-rates/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("NGN", data["rates"])
        self.assertEqual(data["base"], "USD")
        self.assertEqual(data["sources"]["NGN"], {"source": "cbn", "date": "2026-11-26"})
        self.assertEqual(data["sources"]["GHS"], {"source": "provider", "date": None})
        self.assertEqual(data["credit"], "Source: Central Bank of Nigeria (www.cbn.gov.ng)")
