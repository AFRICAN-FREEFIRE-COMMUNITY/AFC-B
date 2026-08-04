"""Organisations applying to become AFC partners, end to end.

WHAT THIS SUITE IS PROTECTING, in the order the design decisions were made:

  1. THE APPLICANT'S MISTAKES ARE CAUGHT WHILE THEY ARE STILL LOOKING AT THE FORM. The whole
     reason this app exists is that a bad redirect URI used to be discovered by the owner days
     later, or by the partner at their first failed sign-in. If a wildcard, a query string or a
     plain-http URI can be SUBMITTED, this app has not earned its keep.
  2. AN APPLICANT NEVER REQUESTS A SCOPE. The eight share_* toggles are a trust decision taken by
     the owner at review time; a submitted toggle must not reach the model.
  3. APPROVAL PROVISIONS THROUGH THE SHARED PATH, so an approved application and a hand-typed one
     are the same kind of row, with the same rules applied.
  4. NO SECRET IS EVER EMAILED, AND A CLAIM LINK WORKS ONCE. That is the one property that
     justifies the extra machinery over "just email it".
  5. THE PUBLIC WRITE CANNOT BE ABUSED: rate limited, one open application per contact email, and
     an image guard that decodes the bytes rather than believing the filename.

Emails are patched out everywhere: afc_partner_apply/emails.py sends on a daemon thread through
Office365 SMTP, and a test suite must neither wait for that nor depend on it. Where the CONTENT of
an email matters (does the approval mail carry a secret?), the send is patched at the module
boundary and the arguments are asserted.
"""
import io
import json
from unittest.mock import patch

from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from oauth2_provider.models import get_application_model

from afc_auth.models import Roles, SessionToken, UserRoles
from afc_partner_api.models import Partner, PartnerApiKey
from afc_partner_apply.models import PartnerApplication, hash_token

Application = get_application_model()
User = get_user_model()

SUBMIT_URL = "/partner-apply/applications/"
ADMIN_LIST_URL = "/partner-apply/admin/applications/"


def _secret_works(sso_application, plaintext):
    """Does this plaintext secret actually authenticate as that application?

    django-oauth-toolkit stores client_secret hashed and verifies it with Django's
    check_password (oauth2_validators.py _check_secret), so that is exactly what is asserted here
    rather than a string comparison, which would pass against the stored hash and prove nothing.
    """
    from django.contrib.auth.hashers import check_password

    sso_application.refresh_from_db()
    return check_password(plaintext, sso_application.client_secret)


def _png_bytes(size=(24, 24)):
    """A real PNG, produced by Pillow, because the upload guard decodes the bytes and a
    hand-written stub would be refused for the right reason and pass the test for the wrong one."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (20, 200, 120)).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.read()


class PartnerApplyTestCase(TestCase):
    """Shared setup: one staff reviewer, one valid application body, and no real emails."""

    def setUp(self):
        self.client = Client()
        # The rate limiter is a real Redis-or-locmem cache shared across tests in a run, and its
        # buckets are keyed on a hashed IP that every test shares. Clearing here is what stops
        # test three failing because tests one and two spent the hourly allowance.
        cache.clear()

        self.admin = User.objects.create_user(
            username="applyadmin", email="applyadmin@afc.test", password="x")
        partner_admin, _ = Roles.objects.get_or_create(role_name="partner_admin")
        UserRoles.objects.create(user=self.admin, role=partner_admin)
        SessionToken.objects.create(user=self.admin, token="tok-apply-admin")

        self.player = User.objects.create_user(
            username="applyplayer", email="applyplayer@afc.test", password="x")
        SessionToken.objects.create(user=self.player, token="tok-apply-player")

        # Patched for the whole suite. Individual tests that care re-patch with a spy.
        self._email_patch = patch("afc_partner_apply.emails._send")
        self._email_patch.start()
        self.addCleanup(self._email_patch.stop)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": "Bearer tok-apply-admin"}

    def _body(self, **overrides):
        body = {
            "organisation_name": "Kite Esports",
            "homepage_url": "https://kite.example",
            "contact_name": "Ama Mensah",
            "contact_email": "ama@kite.example",
            "wants_sso": True,
            "wants_data_api": False,
            "redirect_uris": "https://kite.example/auth/afc/callback",
            "use_case": "We run a Free Fire community site and want players to sign in with AFC.",
            "data_needed": "Their AFC name and their current rank, shown on their profile page.",
            "locale": "en",
        }
        body.update(overrides)
        return body

    def _submit(self, **overrides):
        return self.client.post(
            SUBMIT_URL, data=json.dumps(self._body(**overrides)),
            content_type="application/json")

    def _approve(self, application, **extra):
        payload = {"action": "approve"}
        payload.update(extra)
        return self.client.post(
            f"{ADMIN_LIST_URL}{application.pk}/decide/",
            data=json.dumps(payload), content_type="application/json", **self._auth())


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 1) Submitting
# ──────────────────────────────────────────────────────────────────────────────────────────────
class SubmitApplicationTests(PartnerApplyTestCase):
    def test_a_complete_application_is_accepted_and_gets_a_reference(self):
        resp = self._submit()

        self.assertEqual(resp.status_code, 201, resp.content)
        reference = resp.json()["reference"]
        self.assertTrue(reference.startswith("AFC-P-"))
        application = PartnerApplication.objects.get(reference=reference)
        self.assertEqual(application.status, PartnerApplication.PENDING)
        self.assertEqual(application.contact_email, "ama@kite.example")

    def test_the_access_token_is_emailed_and_never_returned(self):
        """The token is what proves the applicant owns the address they typed, so putting it in
        the HTTP response would defeat the point of mailing it."""
        with patch("afc_partner_apply.emails.send_received") as send:
            resp = self._submit()

        self.assertNotIn("token", json.dumps(resp.json()))
        send.assert_called_once()
        # Sent, and stored only as a hash.
        _application, token = send.call_args.args
        self.assertTrue(token)
        self.assertEqual(
            PartnerApplication.objects.get().access_token_hash, hash_token(token))

    # ── the reason this app exists ──
    def test_a_wildcard_redirect_uri_is_refused_at_submit_time(self):
        resp = self._submit(redirect_uris="https://*.kite.example/cb")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Wildcards", resp.json()["message"])
        self.assertFalse(PartnerApplication.objects.exists())

    def test_a_query_string_in_a_redirect_uri_is_refused_at_submit_time(self):
        """'?' is a pattern character in AFC's policy, so an ordinary query string is refused.
        Catching it here is exactly the friction this app removes from the owner."""
        resp = self._submit(redirect_uris="https://kite.example/cb?tenant=kite")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PartnerApplication.objects.exists())

    def test_plain_http_is_refused_outside_loopback_but_accepted_for_localhost(self):
        self.assertEqual(self._submit(redirect_uris="http://kite.example/cb").status_code, 400)
        cache.clear()
        resp = self._submit(redirect_uris="http://localhost:3000/cb")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_at_least_one_product_must_be_chosen(self):
        resp = self._submit(wants_sso=False, wants_data_api=False)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least one product", resp.json()["message"].lower())

    def test_a_data_api_only_application_needs_no_redirect_uris(self):
        resp = self._submit(wants_sso=False, wants_data_api=True, redirect_uris="")

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(PartnerApplication.objects.get().redirect_uris, "")

    def test_a_one_word_answer_is_refused(self):
        """The two prose answers are what the grant decision is made from. 'data' is not an
        answer, and letting it through would turn every application into a follow-up email."""
        resp = self._submit(data_needed="stats")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("more detail", resp.json()["message"])

    def test_a_bad_contact_email_is_refused(self):
        self.assertEqual(self._submit(contact_email="not-an-address").status_code, 400)

    # ── the applicant cannot grant themselves anything ──
    def test_a_submitted_scope_toggle_is_ignored(self):
        """An applicant describes what they need in prose; they never tick a share_* box. Even if
        one is posted, nothing on the application can carry it."""
        resp = self._submit(share_email=True, share_freefire_uid=True, scopes=["email"])

        self.assertEqual(resp.status_code, 201, resp.content)
        application = PartnerApplication.objects.get()
        self.assertFalse(hasattr(application, "share_email"))
        # And approving it with no toggles still produces a partner that shares nothing.
        self._approve(application)
        application.refresh_from_db()
        self.assertEqual(application.sso_application.allowed_scopes(), {"openid"})

    def test_a_submitted_status_cannot_bypass_review(self):
        resp = self._submit(status="approved")

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(PartnerApplication.objects.get().status, PartnerApplication.PENDING)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 2) Abuse resistance on an anonymous write
# ──────────────────────────────────────────────────────────────────────────────────────────────
class SubmitAbuseTests(PartnerApplyTestCase):
    def test_a_second_open_application_returns_the_first_reference(self):
        """A double-clicked form is the common case and must not put a second row in the queue."""
        first = self._submit()
        cache.clear()  # isolate this from the cooldown; the email rule is what is under test

        second = self._submit(organisation_name="Kite Esports Again")

        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["already_pending"])
        self.assertEqual(second.json()["reference"], first.json()["reference"])
        self.assertEqual(PartnerApplication.objects.count(), 1)

    def test_the_cooldown_blocks_an_immediate_second_send(self):
        self.assertEqual(self._submit().status_code, 201)

        resp = self._submit(contact_email="other@kite.example")

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["reason"], "cooldown")

    def test_a_validation_failure_does_not_consume_the_allowance(self):
        """A real applicant fixing a typo must not be locked out by their own mistake."""
        self.assertEqual(self._submit(redirect_uris="https://*.kite.example/cb").status_code, 400)

        self.assertEqual(self._submit().status_code, 201)

    def test_a_rejected_applicant_may_apply_again(self):
        """Rejection is terminal for the ROW, not for the organisation."""
        application = PartnerApplication.objects.get(
            reference=self._submit().json()["reference"])
        self.client.post(
            f"{ADMIN_LIST_URL}{application.pk}/decide/",
            data=json.dumps({"action": "reject", "note": "Not enough detail about your product."}),
            content_type="application/json", **self._auth())
        cache.clear()

        resp = self._submit()

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(PartnerApplication.objects.count(), 2)

    def test_html_renamed_as_a_png_is_refused(self):
        """The logo may end up on the consent screen, the page a player reads before trusting an
        organisation with their data, so the guard decodes the bytes rather than the filename."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        body = self._body()
        body["logo"] = SimpleUploadedFile(
            "logo.png", b"<html><script>alert(1)</script></html>", content_type="image/png")
        body["wants_sso"] = "true"
        body["wants_data_api"] = "false"

        resp = self.client.post(SUBMIT_URL, data=body)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("not a readable image", resp.json()["message"])
        self.assertFalse(PartnerApplication.objects.exists())

    def test_a_real_png_logo_is_accepted_and_stored(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        body = self._body()
        body["logo"] = SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png")
        body["wants_sso"] = "true"
        body["wants_data_api"] = "false"

        resp = self.client.post(SUBMIT_URL, data=body)

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(PartnerApplication.objects.get().logo)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 3) The applicant's own status page
# ──────────────────────────────────────────────────────────────────────────────────────────────
class ApplicationStatusTests(PartnerApplyTestCase):
    def setUp(self):
        super().setUp()
        with patch("afc_partner_apply.emails.send_received") as send:
            self.reference = self._submit().json()["reference"]
        self.application = PartnerApplication.objects.get(reference=self.reference)
        self.token = send.call_args.args[1]

    def _status_url(self, token=None, reference=None):
        return (
            f"/partner-apply/applications/{reference or self.reference}/"
            f"?token={token if token is not None else self.token}"
        )

    def test_the_right_token_reads_the_application(self):
        resp = self.client.get(self._status_url())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["application"]["reference"], self.reference)

    def test_a_wrong_token_and_an_unknown_reference_are_indistinguishable(self):
        """Otherwise the endpoint is an oracle for which organisations have applied to AFC."""
        wrong = self.client.get(self._status_url(token="not-the-token"))
        unknown = self.client.get(self._status_url(reference="AFC-P-ZZZZZZ"))

        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(wrong.json()["message"], unknown.json()["message"])

    def test_the_applicant_never_sees_the_internal_note(self):
        self.application.internal_note = "Founder is a friend of a banned organiser."
        self.application.save()

        body = self.client.get(self._status_url()).json()["application"]

        self.assertNotIn("internal_note", body)
        self.assertNotIn("banned organiser", json.dumps(body))

    def test_a_pending_application_cannot_be_edited(self):
        """The owner may be reading it. An answer that changes underneath them is worse than a
        second application."""
        resp = self.client.patch(
            self._status_url(), data=json.dumps({"use_case": "A" * 60}),
            content_type="application/json")

        self.assertEqual(resp.status_code, 409)

    def test_changes_requested_makes_it_editable_and_a_fix_returns_it_to_pending(self):
        self.client.post(
            f"{ADMIN_LIST_URL}{self.application.pk}/decide/",
            data=json.dumps({
                "action": "request_changes",
                "note": "Your redirect URI points at a staging host over plain http.",
            }),
            content_type="application/json", **self._auth())
        self.application.refresh_from_db()
        # request_changes re-issues the access token, so the link in the OLD email stops working
        # on purpose. The plaintext only ever existed inside that send, so the test mints its own
        # the same way the view did.
        new_token = self.application.issue_access_token()

        resp = self.client.patch(
            self._status_url(token=new_token),
            data=json.dumps({"redirect_uris": "https://kite.example/auth/afc/cb"}),
            content_type="application/json")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, PartnerApplication.PENDING)
        self.assertEqual(self.application.redirect_uris, "https://kite.example/auth/afc/cb")
        # The note described the OLD answers; leaving it up would tell them they still owe a fix.
        self.assertEqual(self.application.decision_note, "")

    def test_an_edit_is_validated_by_the_same_policy_as_a_submission(self):
        self.application.status = PartnerApplication.CHANGES_REQUESTED
        self.application.save()
        token = self.application.issue_access_token()

        resp = self.client.patch(
            f"/partner-apply/applications/{self.reference}/?token={token}",
            data=json.dumps({"redirect_uris": "https://kite.example/cb#tokens"}),
            content_type="application/json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("fragment", resp.json()["message"])

    def test_the_contact_email_cannot_be_changed(self):
        """It is the address the token was mailed to, so changing it would let a token holder
        redirect every future decision email, credentials included."""
        self.application.status = PartnerApplication.CHANGES_REQUESTED
        self.application.save()
        token = self.application.issue_access_token()

        resp = self.client.patch(
            f"/partner-apply/applications/{self.reference}/?token={token}",
            data=json.dumps({"contact_email": "attacker@evil.example"}),
            content_type="application/json")

        self.assertEqual(resp.status_code, 400)  # nothing editable was sent
        self.application.refresh_from_db()
        self.assertEqual(self.application.contact_email, "ama@kite.example")


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 4) The admin queue and the decision
# ──────────────────────────────────────────────────────────────────────────────────────────────
class DecisionTests(PartnerApplyTestCase):
    def setUp(self):
        super().setUp()
        self.application = PartnerApplication.objects.get(
            reference=self._submit().json()["reference"])

    # ── the gate ──
    def test_a_player_cannot_read_the_queue(self):
        resp = self.client.get(ADMIN_LIST_URL, HTTP_AUTHORIZATION="Bearer tok-apply-player")
        self.assertEqual(resp.status_code, 403)

    def test_an_anonymous_caller_cannot_read_the_queue(self):
        self.assertEqual(self.client.get(ADMIN_LIST_URL).status_code, 400)

    def test_a_player_cannot_decide(self):
        resp = self.client.post(
            f"{ADMIN_LIST_URL}{self.application.pk}/decide/",
            data=json.dumps({"action": "approve"}), content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-apply-player")
        self.assertEqual(resp.status_code, 403)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, PartnerApplication.PENDING)

    # ── approval provisions through the shared path ──
    def test_approving_provisions_an_sso_application_with_afcs_fixed_protocol_shape(self):
        resp = self._approve(self.application)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, PartnerApplication.APPROVED)
        sso = self.application.sso_application
        self.assertIsNotNone(sso)
        # The same shape provision_sso_application pins for a hand-typed partner.
        self.assertEqual(sso.client_type, Application.CLIENT_CONFIDENTIAL)
        self.assertEqual(sso.authorization_grant_type, Application.GRANT_AUTHORIZATION_CODE)
        self.assertEqual(sso.algorithm, Application.RS256_ALGORITHM)
        self.assertFalse(sso.skip_authorization)
        self.assertEqual(sso.redirect_uris, "https://kite.example/auth/afc/callback")
        self.assertEqual(sso.user_id, self.admin.pk)

    def test_the_owner_grants_the_scopes_at_review_time(self):
        self._approve(self.application, share_profile=True, share_ranking=True)

        self.application.refresh_from_db()
        self.assertEqual(
            self.application.sso_application.allowed_scopes(),
            {"openid", "profile", "afc.ranking"},
        )

    def test_the_owner_can_correct_what_the_applicant_typed(self):
        """The review screen prefills and stays editable, so what goes live on the consent screen
        is what the owner approved, not what an unknown organisation typed."""
        self._approve(
            self.application,
            display_name="Kite Esports (verified)",
            redirect_uris="https://kite.example/auth/afc/callback https://staging.kite.example/cb",
        )

        self.application.refresh_from_db()
        sso = self.application.sso_application
        self.assertEqual(sso.display_name, "Kite Esports (verified)")
        self.assertIn("staging.kite.example", sso.redirect_uris)

    def test_an_edited_redirect_uri_is_still_policy_checked_at_approval(self):
        resp = self._approve(self.application, redirect_uris="http://kite.example/cb")

        self.assertEqual(resp.status_code, 400)
        self.application.refresh_from_db()
        # Refused BEFORE anything was written, so the application is untouched and re-approvable.
        self.assertEqual(self.application.status, PartnerApplication.PENDING)
        self.assertIsNone(self.application.sso_application)

    def test_a_data_api_application_provisions_a_partner_with_toggles_off_by_default(self):
        application = PartnerApplication.objects.get(reference=PartnerApplication.objects.filter(
            pk=self.application.pk).values_list("reference", flat=True)[0])
        application.wants_sso = False
        application.wants_data_api = True
        application.save()

        self._approve(application)

        application.refresh_from_db()
        partner = application.data_partner
        self.assertIsNotNone(partner)
        self.assertIsNone(application.sso_application)
        self.assertFalse(partner.can_read_events)
        self.assertFalse(partner.include_kills)

    def test_both_products_at_once(self):
        self.application.wants_data_api = True
        self.application.save()

        self._approve(self.application, can_read_events=True)

        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.sso_application)
        self.assertIsNotNone(self.application.data_partner)
        self.assertTrue(self.application.data_partner.can_read_events)

    def test_approving_twice_is_refused_rather_than_provisioning_twice(self):
        self.assertEqual(self._approve(self.application).status_code, 200)

        resp = self._approve(self.application)

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(Application.objects.count(), 1)

    # ── rejection and changes ──
    def test_rejecting_without_a_reason_is_refused(self):
        resp = self.client.post(
            f"{ADMIN_LIST_URL}{self.application.pk}/decide/",
            data=json.dumps({"action": "reject"}), content_type="application/json",
            **self._auth())

        self.assertEqual(resp.status_code, 400)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, PartnerApplication.PENDING)

    def test_rejecting_stores_the_reason_and_provisions_nothing(self):
        resp = self.client.post(
            f"{ADMIN_LIST_URL}{self.application.pk}/decide/",
            data=json.dumps({"action": "reject", "note": "We cannot verify your organisation."}),
            content_type="application/json", **self._auth())

        self.assertEqual(resp.status_code, 200, resp.content)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, PartnerApplication.REJECTED)
        self.assertEqual(self.application.reviewed_by_id, self.admin.pk)
        self.assertFalse(Application.objects.exists())
        self.assertFalse(Partner.objects.exists())

    def test_the_queue_counts_outstanding_work_regardless_of_the_filter(self):
        """The badge on the tab must not change when the owner filters the table."""
        body = self.client.get(f"{ADMIN_LIST_URL}?status=approved", **self._auth()).json()

        self.assertEqual(body["total_count"], 0)
        self.assertEqual(body["pending_count"], 1)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# 5) Credentials: the property that justifies the machinery
# ──────────────────────────────────────────────────────────────────────────────────────────────
class CredentialClaimTests(PartnerApplyTestCase):
    def setUp(self):
        super().setUp()
        self.application = PartnerApplication.objects.get(
            reference=self._submit().json()["reference"])
        self.application.wants_data_api = True
        self.application.save()

        with patch("afc_partner_apply.emails.send_approved") as send:
            self._approve(self.application)
        self.application.refresh_from_db()
        self.claim_token = send.call_args.args[2]

    def _claim_url(self, token=None):
        return (
            f"/partner-apply/applications/{self.application.reference}/claim/"
            f"?token={token if token is not None else self.claim_token}"
        )

    def test_no_email_ever_carries_a_secret_or_a_key(self):
        """THE rule. The approval email carries a link; the credential is minted when it is
        opened."""
        with patch("afc_partner_apply.emails.send_approved") as send:
            self.client.post(
                f"{ADMIN_LIST_URL}{self.application.pk}/resend-credentials/", **self._auth())

        sent = json.dumps([str(a) for a in send.call_args.args])
        self.assertNotIn("client_secret", sent)
        # The claim token is a link component, not a credential: it mints nothing until opened.
        self.assertEqual(len(send.call_args.args), 3)

    def test_claiming_returns_the_credentials_exactly_once(self):
        first = self.client.post(self._claim_url())

        self.assertEqual(first.status_code, 200, first.content)
        body = first.json()
        self.assertTrue(body["client_secret"])
        self.assertTrue(body["api_key"])
        self.assertEqual(body["client_id"], self.application.sso_application.client_id)

        second = self.client.post(self._claim_url())

        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["reason"], "claimed")

    def test_the_claimed_secret_actually_authenticates(self):
        """A secret that is returned but does not work would pass every other test here."""
        secret = self.client.post(self._claim_url()).json()["client_secret"]

        self.assertTrue(_secret_works(self.application.sso_application, secret))

    def test_a_wrong_claim_token_reveals_nothing(self):
        resp = self.client.post(self._claim_url(token="not-the-token"))

        self.assertEqual(resp.status_code, 404)
        self.application.refresh_from_db()
        self.assertIsNone(self.application.claimed_at)

    def test_the_long_lived_access_token_cannot_mint_credentials(self):
        """Two tokens, two lifetimes. The one in every email must not be able to do this."""
        access_token = self.application.issue_access_token()

        resp = self.client.post(self._claim_url(token=access_token))

        self.assertEqual(resp.status_code, 404)

    def test_an_expired_link_is_refused(self):
        self.application.claim_expires_at = timezone.now() - timezone.timedelta(minutes=1)
        self.application.save()

        resp = self.client.post(self._claim_url())

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["reason"], "expired")

    def test_resending_invalidates_the_previous_link(self):
        """Pressing the button twice must leave exactly one working link, which is what somebody
        pressing it twice expects."""
        old_token = self.claim_token
        with patch("afc_partner_apply.emails.send_approved") as send:
            resp = self.client.post(
                f"{ADMIN_LIST_URL}{self.application.pk}/resend-credentials/", **self._auth())
        self.assertEqual(resp.status_code, 200, resp.content)
        new_token = send.call_args.args[2]

        self.assertEqual(self.client.post(self._claim_url(token=old_token)).status_code, 404)
        self.assertEqual(self.client.post(self._claim_url(token=new_token)).status_code, 200)

    def test_resending_rotates_the_secret_only_when_the_link_is_opened(self):
        """An owner who resends and is then told the old secret still works has learned something
        true: nobody opened the new link."""
        first_secret = self.client.post(self._claim_url()).json()["client_secret"]
        with patch("afc_partner_apply.emails.send_approved") as send:
            self.client.post(
                f"{ADMIN_LIST_URL}{self.application.pk}/resend-credentials/", **self._auth())
        self.assertTrue(_secret_works(self.application.sso_application, first_secret))

        second_secret = self.client.post(
            self._claim_url(token=send.call_args.args[2])).json()["client_secret"]

        self.assertNotEqual(first_secret, second_secret)
        self.assertTrue(_secret_works(self.application.sso_application, second_secret))
        self.assertFalse(_secret_works(self.application.sso_application, first_secret))

    def test_a_claim_on_an_unapproved_application_is_refused(self):
        other = PartnerApplication.objects.create(
            reference="AFC-P-TESTAA", organisation_name="Other", homepage_url="https://o.example",
            contact_name="X", contact_email="x@o.example", wants_sso=True,
            use_case="a" * 40, data_needed="b" * 40)
        token = other.issue_claim_token()

        resp = self.client.post(
            f"/partner-apply/applications/{other.reference}/claim/?token={token}")

        self.assertEqual(resp.status_code, 409)
        self.assertFalse(PartnerApiKey.objects.filter(partner__name="Other").exists())
