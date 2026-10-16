"""
afc_auth/tests_outbound_recipient.py - AFC can write to anybody with an address, not only to
people on a "popular provider".

THE FAULT, three times over. `send_email` (afc_auth/views.py), the one chokepoint for every
outgoing email, ran each recipient through `is_valid_email`, the SIGNUP gate whose allowlist
(gmail, yahoo, outlook...) exists to keep throwaway providers out of the user table. Applied to
OUTGOING mail, the same list meant AFC could not email anybody at their own domain:

    2026-08-05  the new-application notice to info@africanfreefirecommunity.com never sent;
                the allowlist grew AFC's own domain.
    2026-08-14  sponsor invitations to company addresses never sent; the allowlist grew an
                exception for a pending SponsorMemberInvite.
    2026-09-18  the owner approved partner application AFC-P-F5C35A; its contact at
                nexalgaming.co received neither the "received" email at submission nor the
                credentials link at approval, while the admin screen said they had been emailed.
                Journal: "Invalid email address: paul@nexalgaming.co. Error: Please use a valid
                email provider (e.g., Gmail, Yahoo)."

Each time one exception was added; the fault was the gate being on the outgoing side at all.
Outgoing mail now asks one question, is this shaped like an address (is_deliverable_address).
Since 2026-10-16 (owner, inbox #273: "A", accept any real address and catch typos) the provider
allowlist is gone from the incoming side too: is_valid_email checks the format only, and a likely
typo is asked about at sign-up (afc_auth/email_typos.py, afc_auth/tests_any_real_email.py).
(R28: proven on the offender and on the innocent.) Under the test runner OUTBOUND_DELIVERY is
outbox, so "sent" means "recorded in afc_auth.outbox"; nothing reaches SMTP.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from afc_auth import outbox
from afc_auth.views import is_deliverable_address, is_valid_email, send_email


class OutgoingMailReachesOwnDomains(SimpleTestCase):

    def setUp(self):
        outbox.drain()

    def _send(self, to):
        with patch("afc_auth.views.smtplib.SMTP") as smtp:
            ok = send_email(to, "Your AFC partner application is approved", "<p>hello</p>")
        smtp.assert_not_called()
        return ok, outbox.drain()

    def test_a_partner_at_their_own_domain_is_written_to(self):
        # The exact address the 2026-09-18 approval lost.
        ok, sent = self._send("paul@nexalgaming.co")
        self.assertTrue(ok)
        self.assertEqual([m["to"] for m in sent], ["paul@nexalgaming.co"])

    def test_a_company_address_and_a_subdomain_are_written_to(self):
        for to in ("press@some-company.example", "ops@mail.esl-africa.co.za", "a.b@x.io"):
            ok, sent = self._send(to)
            self.assertTrue(ok, to)
            self.assertEqual([m["to"] for m in sent], [to])

    def test_a_malformed_address_is_refused_and_nothing_is_recorded(self):
        for to in ("", None, "not-an-address", "two@@x.com", "a@b", "name@", "@x.com",
                   "a@x.com\nBcc: b@y.com"):
            ok, sent = self._send(to)
            self.assertFalse(ok, repr(to))
            self.assertEqual(sent, [], repr(to))

    def test_the_gate_is_the_format_helper(self):
        # The offender: a good address at an unknown provider. Deliverable, and since #273 usable.
        self.assertTrue(is_deliverable_address("paul@nexalgaming.co"))
        self.assertTrue(is_valid_email("paul@nexalgaming.co")[0])
        # The innocent: a known provider, both ways.
        self.assertTrue(is_valid_email("someone@gmail.com")[0])
        self.assertTrue(is_deliverable_address("someone@gmail.com"))


class IncomingAddressesAreFormatOnly(SimpleTestCase):
    """Owner 2026-10-16 (#273, "A"): any real address is accepted on the way IN as well. The
    provider list and its invited-address exception are gone; only the shape is checked."""

    def test_a_company_or_school_address_is_accepted(self):
        for email in ("sponsor@acme-drinks.example", "someone@leadsageafrica.com",
                      "pupil@student.oauife.edu.ng"):
            ok, _ = is_valid_email(email)
            self.assertTrue(ok, email)

    def test_a_malformed_address_is_still_refused(self):
        for email in ("", None, "not-an-address", "a@b", "name@", "two@@x.com"):
            ok, _ = is_valid_email(email)
            self.assertFalse(ok, repr(email))
