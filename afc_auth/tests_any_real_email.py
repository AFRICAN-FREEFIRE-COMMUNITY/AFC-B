"""Any real email address is accepted; a likely typo is asked about once at sign-up (inbox #273).

Owner 2026-10-16, answer "A" to #238 ("accept any real address, catch typos"). Before: sign-up took
any address (its provider check was commented out on 2026-04-01) while verify-code, resend-code, the
password reset steps and the email change still refused everything outside 16 providers, so a person
at leadsageafrica.com or a .edu.ng address signed up and could never verify. And typos (gamil.com 10,
gmail.con 9, gmail.co 8 on production) went straight through, so the code never arrived.

  - afc_auth/email_typos.py suggest_email_domain: production's typos map to the right provider; real
    providers one keystroke from gmail.com (mail.com, email.com) are never questioned.
  - signup answers email_domain_typo with the corrected address ONCE; email_domain_confirmed lets the
    same address through.
  - verify-code and resend-verification-code accept a company address.

Mail goes to the outbox under the test runner; nothing reaches SMTP.
Run: python manage.py test afc_auth.tests_any_real_email
"""
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase

from afc_auth.email_typos import suggest_email_domain
from afc_auth.models import User

PASSWORD = "CorrectHorse!9"


class SuggestEmailDomainTests(SimpleTestCase):
    def test_production_typos_get_the_right_provider(self):
        cases = {
            "gamil.com": "gmail.com", "gmail.con": "gmail.com", "gmail.co": "gmail.com",
            "gmail.come": "gmail.com", "gmai.com": "gmail.com", "gemail.com": "gmail.com",
            "gmial.com": "gmail.com", "g.mail.com": "gmail.com", "gmail.vom": "gmail.com",
            "gmail.comol": "gmail.com", "GMAIL.CON": "gmail.com", "ivloud.com": "icloud.com",
        }
        for typo, provider in cases.items():
            self.assertEqual(suggest_email_domain(f"kofi@{typo}"), f"kofi@{provider}", typo)

    def test_real_domains_are_never_questioned(self):
        for domain in ("gmail.com", "icloud.com", "yahoo.fr", "mail.com", "email.com", "ymail.com",
                       "leadsageafrica.com", "meadowhallschool.org", "student.oauife.edu.ng",
                       "nexalgaming.co", "yahoo.co.uk", "petamail.com"):
            self.assertIsNone(suggest_email_domain(f"kofi@{domain}"), domain)

    def test_the_part_before_the_at_is_kept_exactly(self):
        self.assertEqual(suggest_email_domain("Kofi.Mensah+afc@gamil.com"), "Kofi.Mensah+afc@gmail.com")

    def test_not_an_address_gives_nothing(self):
        for text in ("", None, "kofi", "a@b@gamil.com", "@gamil.com"):
            self.assertIsNone(suggest_email_domain(text), repr(text))


@patch("afc_auth.views.require_human", return_value=None)
@patch("afc_auth.views.geo_for_ip", return_value={})
class SignupEmailTests(TestCase):
    def setUp(self):
        self.client = Client()

    def _signup(self, email, **extra):
        body = {"in_game_name": "kofi_afc", "full_name": "Kofi Mensah", "email": email,
                "password": PASSWORD, "confirm_password": PASSWORD, **extra}
        return self.client.post("/auth/signup/", data=json.dumps(body), content_type="application/json")

    def test_a_typo_is_asked_about_once_and_nobody_is_created(self, _geo, _human):
        resp = self._signup("kofi@gamil.com")
        self.assertEqual(resp.status_code, 400, resp.content[:300])
        self.assertEqual(resp.json()["code"], "email_domain_typo")
        self.assertEqual(resp.json()["suggestion"], "kofi@gmail.com")
        self.assertFalse(User.objects.filter(username="kofi_afc").exists())

    def test_the_same_address_goes_through_when_confirmed(self, _geo, _human):
        resp = self._signup("kofi@gamil.com", email_domain_confirmed=True)
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertTrue(User.objects.filter(email="kofi@gamil.com").exists())

    def test_a_company_address_signs_up_without_a_question(self, _geo, _human):
        resp = self._signup("kofi@leadsageafrica.com")
        self.assertEqual(resp.status_code, 201, resp.content[:300])

    def test_a_malformed_address_is_refused_with_a_code(self, _geo, _human):
        resp = self._signup("kofi-at-gmail")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "email_invalid")


class VerifyWithACompanyAddressTests(TestCase):
    """THE BUG (#238): these two answered "Please use a valid email provider" for a real address."""

    def setUp(self):
        self.client = Client()
        # is_active=False is how AFC marks an account that has not verified its address yet.
        self.user = User.objects.create(username="lead_sage", email="ops@leadsageafrica.com",
                                        full_name="Lead Sage", role="player", password="x",
                                        is_active=False)

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body), content_type="application/json")

    def test_verify_code_accepts_a_company_address(self):
        cache.set(f"verification_code_{self.user.user_id}", "123456", timeout=600)
        resp = self._post("/auth/verify-code/", {"email": "ops@leadsageafrica.com", "code": "123456"})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["code"], "account_verified")

    def test_resend_accepts_a_company_address(self):
        resp = self._post("/auth/resend-verification-code/", {"email": "ops@leadsageafrica.com"})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["code"], "verification_code_resent")
