"""
afc_auth.test_currencies - guards the ONE currency source of truth (backlog item 28).

WHY THESE TESTS EXIST
    Item 28 was reported because four hand-maintained currency arrays had drifted apart, so the same
    person saw a different menu on the prize-pool screen than on the broadcast composer. Collapsing
    them into one list fixes it once; these tests are what stop it happening again:

      1. the frontend list and the backend list are IDENTICAL, in the same order,
      2. no picker anywhere reintroduces its own hardcoded array,
      3. every currency on the menu has FX rate data, because a code without a rate converts silently
         and wrongly rather than failing loudly,
      4. every country the FX layer maps points at a currency that is actually on the menu.

    Test 1 parses the TypeScript file from Python, which looks unusual but is the only way to assert
    a cross-language invariant without a build step. It is a plain regex over a literal array, so it
    breaks loudly if someone restructures the file, which is the desired behaviour.
"""
import os
import re

from django.test import TestCase

from afc_auth import fx
from afc_auth.currencies import (
    AFC_CURRENCIES,
    CURRENCY_CODES,
    DEFAULT_CURRENCY,
    LEGACY_CURRENCY_CODES,
    check_currency_fx_coverage,
    is_known_currency,
    is_supported_currency,
    normalize_currency,
)
from afc_auth.models import FxRate

# backend/afc_auth/test_currencies.py -> backend/ -> WEBSITE/ -> WEBSITE/frontend
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND_DIR = os.path.join(os.path.dirname(_BACKEND_DIR), "frontend")
_CURRENCIES_TS = os.path.join(_FRONTEND_DIR, "lib", "currencies.ts")

# Frontend files that offer a currency CHOICE to a human. Each must import the shared list rather
# than declare its own. Paths are relative to the frontend root.
_CURRENCY_PICKER_FILES = [
    os.path.join("components", "CurrencyPicker.tsx"),
    os.path.join("components", "CountryPaymentRulesEditor.tsx"),
    os.path.join("app", "(a)", "a", "_components", "BroadcastTokenInserts.tsx"),
    os.path.join("app", "(a)", "a", "events", "create", "_components", "types.ts"),
    os.path.join("app", "(a)", "a", "events", "create", "_components", "Step5PrizePool.tsx"),
]


def _read_frontend_codes():
    """Parse the ISO codes out of frontend/lib/currencies.ts, in declaration order.

    Matches the `{ code: "XXX", name: "..." }` entries of the AFC_CURRENCIES literal. The narrower
    COUNTRY_PAYMENT_RULE_CODES array further down the file is a plain string list, not objects, so
    this pattern does not pick it up."""
    with open(_CURRENCIES_TS, encoding="utf-8") as fh:
        source = fh.read()
    return re.findall(r'\{\s*code:\s*"([A-Z]{3})"\s*,\s*name:', source)


class CurrencySourceOfTruthTests(TestCase):
    """The frontend and backend menus must be the same list."""

    def test_frontend_and_backend_lists_are_identical(self):
        backend_codes = [code for code, _name in AFC_CURRENCIES]

        frontend_codes = _read_frontend_codes()

        self.assertEqual(
            frontend_codes,
            backend_codes,
            "frontend/lib/currencies.ts and backend/afc_auth/currencies.py have drifted. They must "
            "hold the same codes in the same order.",
        )

    def test_the_list_is_not_empty_and_has_no_duplicates(self):
        codes = [code for code, _name in AFC_CURRENCIES]

        self.assertGreater(len(codes), 40)
        self.assertEqual(len(codes), len(set(codes)), "duplicate currency code on the menu")

    def test_every_entry_is_a_well_formed_iso_code_with_a_name(self):
        for code, name in AFC_CURRENCIES:
            self.assertRegex(code, r"^[A-Z]{3}$", f"{code} is not a 3-letter uppercase ISO code")
            self.assertTrue(name.strip(), f"{code} has no display name")

    def test_the_owner_required_currencies_are_present(self):
        # The explicit ask was "African currencies plus USD and EUR". Spot-check the base pair plus
        # the two CFA francs, whose absence from the broadcast list is what prompted the report.
        for code in ("USD", "EUR", "XOF", "XAF", "NGN", "GHS", "KES", "ZAR"):
            self.assertIn(code, CURRENCY_CODES)

    def test_no_currency_was_dropped_from_any_legacy_picker(self):
        # Removing a code would orphan events and broadcast tokens already saved with it, so the new
        # menu must be a SUPERSET of every list it replaced.
        previously_offered = {
            # components/CurrencyPicker.tsx
            "USD", "NGN", "GHS", "KES", "ZAR", "XOF", "XAF", "TZS", "UGX", "EGP", "MAD", "EUR", "GBP",
            # Step5PrizePool.tsx
            "RWF", "ETB", "DZD", "AOA", "MZN", "ZMW", "INR",
            # BroadcastTokenInserts.tsx
            "BRL",
        }

        self.assertTrue(
            previously_offered.issubset(CURRENCY_CODES),
            f"dropped from the menu: {sorted(previously_offered - CURRENCY_CODES)}",
        )


class CurrencyPickersUseTheSharedListTests(TestCase):
    """No picker may reintroduce a hardcoded array. This is the regression that item 28 IS."""

    def test_every_picker_imports_the_shared_list(self):
        for rel_path in _CURRENCY_PICKER_FILES:
            path = os.path.join(_FRONTEND_DIR, rel_path)
            with self.subTest(file=rel_path):
                self.assertTrue(os.path.exists(path), f"{rel_path} has moved or been deleted")
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                self.assertIn(
                    "@/lib/currencies",
                    source,
                    f"{rel_path} offers a currency choice but does not import the shared list.",
                )

    def test_no_picker_declares_its_own_inline_currency_array(self):
        # Catches the exact shape the four legacy lists had: three or more bare ISO codes in a row
        # inside an array literal, e.g. ["NGN", "USD", "GBP", ...].
        inline_array = re.compile(r'\[\s*"[A-Z]{3}"\s*,\s*"[A-Z]{3}"\s*,\s*"[A-Z]{3}"')

        for rel_path in _CURRENCY_PICKER_FILES:
            path = os.path.join(_FRONTEND_DIR, rel_path)
            with self.subTest(file=rel_path):
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                # The one permitted exception is the documented COUNTRY_PAYMENT_RULE_CODES subset,
                # which lives in lib/currencies.ts itself, not in any picker.
                self.assertIsNone(
                    inline_array.search(source),
                    f"{rel_path} declares an inline currency array again. Import it from "
                    f"@/lib/currencies instead.",
                )


class CurrencyHelperTests(TestCase):
    """The guards other endpoints are meant to call."""

    def test_is_supported_currency_is_case_insensitive(self):
        self.assertTrue(is_supported_currency("ngn"))
        self.assertTrue(is_supported_currency("  XOF  "))
        self.assertFalse(is_supported_currency("XYZ"))
        self.assertFalse(is_supported_currency(""))
        self.assertFalse(is_supported_currency(None))

    def test_legacy_codes_are_readable_but_not_selectable(self):
        # SLL and ZWL were redenominated. Old rows must still convert, but nothing new may be
        # created on them.
        for legacy in LEGACY_CURRENCY_CODES:
            self.assertFalse(is_supported_currency(legacy), f"{legacy} must not be selectable")
            self.assertTrue(is_known_currency(legacy), f"{legacy} must still be readable")

    def test_normalize_currency_falls_back_rather_than_raising(self):
        # A money render must never 500 because a stale client sent a retired code.
        self.assertEqual(normalize_currency("ngn"), "NGN")
        self.assertEqual(normalize_currency("XYZ"), DEFAULT_CURRENCY)
        self.assertEqual(normalize_currency(None), DEFAULT_CURRENCY)
        self.assertEqual(normalize_currency("XYZ", default="NGN"), "NGN")


class CurrencyFxCoverageTests(TestCase):
    """A currency with no FX rate converts silently and wrongly. That is the trap to keep shut."""

    def test_coverage_helper_reports_codes_with_no_rate(self):
        # Arrange: seed rates for everything EXCEPT two codes.
        missing = {"XOF", "ZMW"}
        for code in CURRENCY_CODES - missing:
            FxRate.objects.create(currency=code, rate=1)

        # Act
        reported = check_currency_fx_coverage()

        # Assert
        self.assertEqual(set(reported), missing)

    def test_every_menu_currency_converts_once_rates_are_present(self):
        # Arrange: the healthy production state, where every menu code has a rate row.
        for code in CURRENCY_CODES:
            FxRate.objects.create(currency=code, rate=2)

        # Assert: nothing is reported missing, and every code genuinely round-trips through the FX
        # layer instead of silently passing the amount through unconverted.
        self.assertEqual(check_currency_fx_coverage(), [])
        for code in CURRENCY_CODES:
            converted = fx.from_usd(10, code)
            expected = 10 if code == "USD" else 20
            self.assertEqual(int(converted), expected, f"{code} did not convert")

    def test_every_country_maps_to_a_currency_on_the_menu(self):
        # fx._COUNTRY_CCY decides a user's DEFAULT display currency from their country. If it points
        # at a code the menu does not carry, that user lands on a currency they can never re-select.
        mapped = set(fx._COUNTRY_CCY.values())

        unlisted = mapped - CURRENCY_CODES

        self.assertEqual(
            unlisted,
            set(),
            f"fx._COUNTRY_CCY maps countries to currencies missing from the menu: {sorted(unlisted)}",
        )

    def test_redenominated_countries_map_to_the_current_code(self):
        # Sierra Leone (SLL -> SLE, 1000:1 in 2022) and Zimbabwe (ZWL -> ZWG, 2024). Mapping either
        # country to its old code showed every amount at the wrong magnitude.
        self.assertEqual(fx.country_to_currency("Sierra Leone"), "SLE")
        self.assertEqual(fx.country_to_currency("sl"), "SLE")
        self.assertEqual(fx.country_to_currency("Zimbabwe"), "ZWG")
        self.assertEqual(fx.country_to_currency("zw"), "ZWG")
