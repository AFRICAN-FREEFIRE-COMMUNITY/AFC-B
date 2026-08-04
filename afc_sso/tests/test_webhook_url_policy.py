"""The disconnection webhook URL: the one address AFC'S OWN SERVER fetches.

WHY THIS IS ITS OWN POLICY, separate from the redirect URI rules next door. Every other URL a
partner registers is followed by the PLAYER's browser, so `http://localhost:3000/cb` reaches the
developer's own laptop and is a perfectly ordinary thing to register. The deletion webhook is
different in kind: AFC's server POSTs to it, from inside AFC's network, on a schedule nobody
outside AFC controls. The same string that means "my laptop" to a browser means "AFC's own
infrastructure" to AFC.

And the address arrives on a PUBLIC, UNAUTHENTICATED form: anybody may apply to be a partner
(afc_partner_apply). So `http://169.254.169.254/latest/meta-data/` typed into a web form would
have AFC fetch its own cloud metadata service and hand the response's fate to the applicant. That
is server-side request forgery, and the field is the whole attack surface.

TWO HALVES, and both are tested here because either alone is defeated:

  1. AT THE DOOR - `_clean_outbound_url` requires https and a PUBLIC address, on every path a
     webhook URL can arrive or be edited by: the public application form, that applicant editing
     their own draft, staff creating a partner, and staff PATCHing an approved one. A rule applied
     on three doors out of four is one PATCH away from not existing.
  2. AT SEND TIME - `deliver_disconnect_signal` does not follow redirects. Validation at
     registration time cannot see the future: a host that is a normal partner server on the day
     staff approve it can answer with a 302 to a private address any time afterwards, and requests
     follows redirects by default. Nobody can inspect where a URL will redirect on the day.

A NAME IS DELIBERATELY NOT RESOLVED at registration time, and there is a test saying so, because
DNS answers differently later: resolving would add a network call to a form submission and buy a
false sense of safety. Half two is what covers the name case.
"""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from oauth2_provider.models import get_application_model

from afc_auth.models import Roles, SessionToken, UserRoles
from afc_sso.provisioning import _clean_outbound_url, _clean_url

Application = get_application_model()
User = get_user_model()

APPS_URL = "/sso/admin/apps/"

# Addresses that must never be reachable from a partner-supplied webhook. The metadata address is
# listed first because it is the one that turns an SSRF into stolen cloud credentials.
PRIVATE_URLS = (
    "https://169.254.169.254/latest/meta-data/",   # cloud instance metadata, link-local
    "https://10.0.0.5/hook",                       # private range
    "https://192.168.1.10/hook",                   # private range
    "https://172.16.4.4/hook",                     # private range
    "https://127.0.0.1/hook",                      # loopback
    "http://127.0.0.1/hook",                       # loopback, and http
    "https://[::1]/hook",                          # loopback, IPv6
    "http://localhost/hook",                       # loopback by name
    "https://localhost:8000/hook",                 # ...and on a port, with https
    "https://api.localhost/hook",                  # a .localhost subdomain still resolves local
)


class OutboundUrlRuleTests(TestCase):
    """The rule itself, with no HTTP and no database in the way."""

    def test_a_public_https_url_is_accepted(self):
        cleaned, err = _clean_outbound_url("https://partner.example/afc/disconnected", "Webhook")
        self.assertIsNone(err)
        self.assertEqual(cleaned, "https://partner.example/afc/disconnected")

    def test_a_public_ip_literal_is_accepted(self):
        """The rule refuses addresses that are unreachable from AFC, not IP literals as such. A
        partner running on a bare public IP is unusual and not wrong."""
        cleaned, err = _clean_outbound_url("https://93.184.216.34/hook", "Webhook")
        self.assertIsNone(err)
        self.assertEqual(cleaned, "https://93.184.216.34/hook")

    def test_every_private_loopback_and_link_local_address_is_refused(self):
        for url in PRIVATE_URLS:
            with self.subTest(url=url):
                cleaned, err = _clean_outbound_url(url, "Webhook")
                self.assertIsNone(cleaned)
                self.assertIsNotNone(err, f"{url} was accepted")
                # The refusal says WHY, so an applicant who typed their dev address fixes it
                # themselves instead of filing a support ticket about a rejected form.
                self.assertIn("Webhook", err)

    def test_plain_http_is_refused_even_for_a_public_host(self):
        """A signed token is POSTed to this address. Sending it in clear text would hand it to
        anybody on the path, so http is refused here with no loopback exemption at all."""
        cleaned, err = _clean_outbound_url("http://partner.example/hook", "Webhook")
        self.assertIsNone(cleaned)
        self.assertIn("https", err)

    def test_an_empty_value_stays_legal(self):
        """The webhook is optional: most partners never register one. Refusing blank would make
        every other rule here unreachable, because the field would be mandatory."""
        for value in ("", None, "   "):
            cleaned, err = _clean_outbound_url(value, "Webhook")
            self.assertIsNone(err)
            self.assertEqual(cleaned, "")

    def test_a_hostname_is_not_resolved_at_registration_time(self):
        """Deliberate, and asserted so nobody adds resolution later believing it closes the hole.

        `localtest.me` and its kin resolve to 127.0.0.1 in public DNS. This rule lets such a name
        through, because resolving at registration time proves nothing about what the name will
        answer on the day the webhook actually fires. The no-redirects rule at send time is the
        defence that does not depend on when it is checked.
        """
        cleaned, err = _clean_outbound_url("https://anything.localtest.me/hook", "Webhook")
        self.assertIsNone(err)
        self.assertEqual(cleaned, "https://anything.localtest.me/hook")

    def test_the_browser_facing_cleaner_is_deliberately_more_permissive(self):
        """The contrast that gives this module its reason to exist. A logo or homepage URL is
        fetched by the PLAYER's browser, where localhost means the player's own machine, so
        _clean_url allows what _clean_outbound_url must refuse. If these two ever agree, one of
        them is wrong."""
        cleaned, err = _clean_url("http://localhost:3000/logo.png", "Logo URL")
        self.assertIsNone(err)
        self.assertEqual(cleaned, "http://localhost:3000/logo.png")
        self.assertIsNone(_clean_outbound_url("http://localhost:3000/logo.png", "Webhook")[0])


class StaffApiWebhookTests(TestCase):
    """Both staff doors: creating a partner, and editing an approved one."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="hookadmin", email="hookadmin@afc.test", password="x")
        head_admin, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=self.admin, role=head_admin)
        SessionToken.objects.create(user=self.admin, token="tok-hook-admin")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": "Bearer tok-hook-admin"}

    def _create(self, **overrides):
        body = {
            "name": "Hook Partner",
            "display_name": "Hook Partner",
            "redirect_uris": "https://hook.example/cb",
        }
        body.update(overrides)
        return self.client.post(
            APPS_URL, data=json.dumps(body), content_type="application/json", **self._auth())

    def test_creating_a_partner_with_a_private_webhook_is_refused(self):
        resp = self._create(deletion_webhook_url="https://169.254.169.254/latest/meta-data/")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Application.objects.filter(name="Hook Partner").count(), 0)

    def test_creating_a_partner_with_a_public_webhook_works(self):
        resp = self._create(deletion_webhook_url="https://hook.example/afc/disconnected")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_patching_an_approved_partner_cannot_smuggle_a_private_webhook_in(self):
        """The hole this closes. Creation was checked and the PATCH beside it was not, so a
        partner could be approved with an innocent webhook and then edited to point at AFC's own
        metadata service, which is exactly the address the create path refuses."""
        created = self._create(deletion_webhook_url="https://hook.example/afc/disconnected")
        self.assertEqual(created.status_code, 201, created.content)
        application_id = created.json()["application"]["application_id"]

        resp = self.client.patch(
            f"{APPS_URL}{application_id}/",
            data=json.dumps({"deletion_webhook_url": "http://169.254.169.254/"}),
            content_type="application/json", **self._auth())
        self.assertEqual(resp.status_code, 400, resp.content)

        application = Application.objects.get(pk=application_id)
        self.assertEqual(
            application.deletion_webhook_url, "https://hook.example/afc/disconnected",
            "the refused PATCH must not have partially applied")

    def test_patching_a_logo_url_is_still_allowed_to_be_local(self):
        """The PATCH view now picks a cleaner per field. This proves it did not simply apply the
        strict rule to all three, which would break a partner registering a staging logo."""
        created = self._create()
        application_id = created.json()["application"]["application_id"]
        resp = self.client.patch(
            f"{APPS_URL}{application_id}/",
            data=json.dumps({"logo_url": "http://localhost:3000/logo.png"}),
            content_type="application/json", **self._auth())
        self.assertEqual(resp.status_code, 200, resp.content)


class DjangoAdminWebhookTests(TestCase):
    """The fifth door, and the last one that did not apply the rule.

    afc_sso/admin.py is break-glass only, but it edits the same column, and a plain URLField
    accepts https://127.0.0.1/ without complaint. Break-glass is precisely when a value gets
    typed in a hurry, so it takes the same rule as the four doors above.
    """

    def _errors_for(self, url):
        from afc_sso.admin import AFCSSOApplicationAdminForm

        # Only this field's verdict is under test, so the rest of the form is left empty on
        # purpose: an invalid form is fine, a WRONG verdict on this field is not.
        form = AFCSSOApplicationAdminForm(data={"deletion_webhook_url": url})
        form.is_valid()
        return form.errors.get("deletion_webhook_url", [])

    def test_a_private_webhook_is_refused_in_the_django_admin_too(self):
        for url in PRIVATE_URLS:
            with self.subTest(url=url):
                self.assertTrue(
                    self._errors_for(url), f"the django admin accepted {url}")

    def test_a_public_webhook_is_still_accepted_in_the_django_admin(self):
        self.assertEqual(self._errors_for("https://partner.example/afc/disconnected"), [])

    def test_leaving_the_webhook_blank_is_still_allowed(self):
        """Most partners register no webhook at all. A rule that made this field mandatory would
        be a different change from the one intended here."""
        self.assertEqual(self._errors_for(""), [])


class SendTimeRedirectTests(TestCase):
    """The half that validation cannot cover: where the URL goes on the day."""

    def test_the_disconnect_signal_does_not_follow_redirects(self):
        """A host that is a normal partner server at approval time can answer with a 302 to a
        private address at any point afterwards. requests follows redirects by default, so
        without this AFC would chase it, from inside its own network, carrying the signed token.
        A partner that cannot receive a POST at the address it registered is misconfigured, so
        refusing to chase costs a legitimate partner nothing.
        """
        from afc_sso.tasks import deliver_disconnect_signal

        application = Application.objects.create(
            name="Redirect Partner", client_type="confidential",
            authorization_grant_type="authorization-code",
            redirect_uris="https://redirect.example/cb",
        )

        with patch("afc_sso.tasks.requests.post") as post:
            post.return_value = type("R", (), {"status_code": 202})()
            deliver_disconnect_signal(application.pk, "https://redirect.example/hook", "token.jwt")

        self.assertTrue(post.called, "the signal never went out, so nothing was proved")
        self.assertIs(
            post.call_args.kwargs.get("allow_redirects"), False,
            "requests follows redirects unless told not to, which is the SSRF here")
