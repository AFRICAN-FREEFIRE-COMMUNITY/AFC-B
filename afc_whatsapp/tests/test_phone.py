"""Phone normalisation (afc_whatsapp/phone.py).

These cases are the reason the module exists: 34 of the 133 WhatsApp numbers stored
on AFC profiles are written in national form ("08051234567") rather than
international ("+2348051234567"), and Meta cannot deliver to the former. Each test
below is one of the shapes actually found in the data.
"""
from django.test import SimpleTestCase

from afc_whatsapp.phone import to_e164, to_wa_id


class ToE164Tests(SimpleTestCase):
    def test_local_nigerian_number_with_country(self):
        # THE case: national form with the "0" trunk prefix plus the account's country.
        self.assertEqual(to_e164("08051234567", "NG"), "+2348051234567")

    def test_country_may_be_a_name_not_a_code(self):
        # Profiles store a human label ("Nigeria"); the geo lookup stores "NG". Both work.
        self.assertEqual(to_e164("08051234567", "Nigeria"), "+2348051234567")

    def test_already_international_is_left_alone(self):
        self.assertEqual(to_e164("+2348051234567"), "+2348051234567")
        self.assertEqual(to_e164("+234 805 123 4567"), "+2348051234567")
        self.assertEqual(to_e164("+234-805-123-4567", "NG"), "+2348051234567")

    def test_international_without_the_plus(self):
        # Stored as bare digits, country code included. Length is what proves it is
        # not a national number that happens to start with 234.
        self.assertEqual(to_e164("2348051234567", "NG"), "+2348051234567")

    def test_double_zero_dial_out_prefix(self):
        self.assertEqual(to_e164("002348051234567"), "+2348051234567")

    def test_national_number_without_a_trunk_prefix(self):
        self.assertEqual(to_e164("8051234567", "NG"), "+2348051234567")

    def test_junk_returns_none(self):
        for junk in ["hello", "", "   ", None, "abc-def", "12"]:
            self.assertIsNone(to_e164(junk, "NG"), f"{junk!r} should not normalise")

    def test_local_number_without_a_country_is_refused(self):
        # Refusing is the point: "08051234567" belongs to a dozen numbering plans, and
        # messaging the wrong subscriber is worse than not messaging at all.
        self.assertIsNone(to_e164("08051234567"))

    def test_other_african_plans(self):
        self.assertEqual(to_e164("0244123456", "GH"), "+233244123456")
        self.assertEqual(to_e164("0712345678", "Kenya"), "+254712345678")
        self.assertEqual(to_e164("0821234567", "South Africa"), "+27821234567")

    def test_number_that_does_not_fit_the_plan_is_refused(self):
        # 6 digits is not a Nigerian subscriber number.
        self.assertIsNone(to_e164("080512", "NG"))

    def test_over_long_number_is_refused(self):
        # E.164 tops out at 15 digits.
        self.assertIsNone(to_e164("+12345678901234567"))


class ToWaIdTests(SimpleTestCase):
    def test_strips_the_plus_for_the_wire(self):
        # Meta addresses recipients by digits only; we keep the readable "+" on the row.
        self.assertEqual(to_wa_id("+2348051234567"), "2348051234567")
        self.assertEqual(to_wa_id("2348051234567"), "2348051234567")
        self.assertEqual(to_wa_id(None), "")
