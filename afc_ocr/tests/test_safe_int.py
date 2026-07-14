"""
A7 - unit tests for afc_ocr.views._safe_int.

Gemini can emit a kills/placement cell as null, "", "3 kills", a float string, or plain garbage.
A bare int() on that used to 500 the whole upload. _safe_int coerces every one of those to a
sane int (or the given default) and NEVER raises. Pure function, no DB, so this is a SimpleTestCase
driven by a table of (input -> expected) cases straight from the spec's A7 edge-case list.

The helper is consumed by upload_ocr_session + ocr_from_stored_image (draft build) and by the solo
commit path (services/commit.commit_solo_result imports it), so keeping it correct here guards all
three call sites at once.
"""
from django.test import SimpleTestCase

from afc_ocr.views import _safe_int


class SafeIntTests(SimpleTestCase):
    def test_safe_int_variants(self):
        # (input, expected) - the exact edge cases the spec A7 lists, plus a couple of obvious
        # pass-throughs so a regression in the happy path is caught too.
        cases = [
            (None, 0),          # null cell -> default
            ("", 0),            # blank string -> default
            ("3 kills", 3),     # leading integer run is parsed ("3 kills" -> 3)
            (2.0, 2),           # float -> truncated int
            ("abc", 0),         # no leading digits -> default
            (True, 0),          # bool must NOT become 1 (guarded explicitly)
            (5, 5),             # plain int passes through
            ("7", 7),           # numeric string parses
            ("  12  ", 12),     # surrounding whitespace tolerated
            ("-4 pts", -4),     # a signed leading run is honoured
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_safe_int(value), expected)

    def test_default_is_configurable(self):
        # The caller can pick a non-zero fallback; garbage then returns that instead of 0.
        self.assertEqual(_safe_int(None, default=1), 1)
        self.assertEqual(_safe_int("abc", default=9), 9)
        # A parseable value ignores the default entirely.
        self.assertEqual(_safe_int("6", default=9), 6)
