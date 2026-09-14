"""
afc_auth/test_contact_us.py - the Contact Us form sends what the visitor actually wrote.

Owner 2026-09-14: "when they use this, the only email we get in our email is 'valid email'". The
view validated the address with `is_valid, message = is_valid_email(email)`, which overwrote the
visitor's own `message` two lines before the body was built, so every contact email AFC has ever
received read "Message: Valid email." while the sender was told it had been sent.

This file is the catcher for that: the message must survive into the body, whatever the validator
says, and the endpoint must not report success when the send was refused.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_auth.test_contact_us
"""
import os
from unittest.mock import patch

from django.test import Client, TestCase


class ContactUsSendsTheMessageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.sent = []
        p = patch("afc_auth.views.send_email",
                  side_effect=lambda to, subject, body, language="en", prelocalized=False: (
                      self.sent.append((to, subject, body)) or True))
        p.start()
        self.addCleanup(p.stop)

    def _post(self, **over):
        body = {"name": "Layo", "email": "ladilawalt@gmail.com",
                "message": "does this actually work?"}
        body.update(over)
        return self.client.post("/auth/contact-us/", body, content_type="application/json")

    def test_the_visitors_words_reach_the_support_inbox(self):
        r = self._post()
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertEqual(len(self.sent), 1)
        to, subject, mail_body = self.sent[0]
        self.assertIn("does this actually work?", mail_body)
        self.assertNotIn("Valid email", mail_body)  # the bug, named
        self.assertIn("ladilawalt@gmail.com", mail_body)
        self.assertIn("Layo", subject)

    def test_it_goes_to_the_published_support_address(self):
        # Owner 2026-09-14: "i did not get the mail, instead it sent to
        # africanfreefirecommunity1@gmail.com". The Contact page publishes info@, so that is where
        # contact mail lands unless the server names another address.
        self._post()
        self.assertEqual(self.sent[0][0], "info@africanfreefirecommunity.com")

    def test_the_server_can_move_the_support_address(self):
        with patch.dict(os.environ, {"SUPPORT_EMAIL": "support@africanfreefirecommunity.com"}):
            self._post()
        self.assertEqual(self.sent[0][0], "support@africanfreefirecommunity.com")

    def test_line_breaks_survive(self):
        self._post(message="line one\nline two")
        _to, _s, mail_body = self.sent[0]
        self.assertIn("line one<br>line two", mail_body)

    def test_html_in_the_message_is_escaped_not_rendered(self):
        self._post(message="<script>alert(1)</script>")
        _to, _s, mail_body = self.sent[0]
        self.assertNotIn("<script>", mail_body)
        self.assertIn("&lt;script&gt;", mail_body)

    def test_a_work_address_can_write_to_support(self):
        # is_valid_email enforces the SIGNUP provider allowlist; support must hear from anybody,
        # especially somebody locked out of the account tied to their personal address.
        r = self._post(email="temilayo@some-company.co.uk")
        self.assertEqual(r.status_code, 200, r.content[:200])
        self.assertIn("some-company.co.uk", self.sent[0][2])

    def test_a_malformed_address_is_refused_with_a_code(self):
        r = self._post(email="not-an-address")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "contact_email_invalid")
        self.assertEqual(self.sent, [])

    def test_missing_fields_are_refused_with_a_code(self):
        r = self._post(message="")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "contact_fields_required")

    def test_a_refused_send_is_not_reported_as_success(self):
        with patch("afc_auth.views.send_email", return_value=False):
            r = self._post()
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["code"], "contact_send_failed")
