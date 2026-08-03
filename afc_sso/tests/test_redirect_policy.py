"""AFC's redirect URI policy: what a partner may register, and what it may not.

The policy (afc_sso/redirect_policy.py) is deliberately MORE generous and MORE strict
than what came before: several URIs per partner so they get production, staging and local
development without asking, and in exchange plain http survives only for loopback.

Every rule is asserted on BOTH paths a URI can arrive by, because the whole point of
putting the rules in one module was that the two could not drift:
  * the staff API,   POST/PATCH /sso/admin/apps/   (afc_sso/admin_api.py)
  * the model,       full_clean()                  (afc_sso/models.py clean), which is
    what the Django admin runs, so a superuser at /admin/ cannot bypass the API's rules.
"""
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from oauth2_provider.models import get_application_model

from afc_auth.models import Roles, SessionToken, UserRoles
from afc_sso.redirect_policy import RedirectURIPolicyError, validate_redirect_uris

Application = get_application_model()
User = get_user_model()

APPS_URL = "/sso/admin/apps/"


class RedirectPolicyUnitTests(TestCase):
    """The rules themselves, with no HTTP and no database in the way."""

    def test_https_is_accepted_anywhere(self):
        self.assertEqual(
            validate_redirect_uris("https://partner.example/auth/afc/callback"),
            "https://partner.example/auth/afc/callback",
        )

    def test_several_uris_are_accepted_and_normalised_to_one_string(self):
        """A partner needs production, staging and local development. All three at once,
        given as separate lines, come back as the single space-separated string
        django-oauth-toolkit stores."""
        cleaned = validate_redirect_uris(
            "https://partner.example/cb\n"
            "https://staging.partner.example/cb\n"
            "http://localhost:3000/cb"
        )
        self.assertEqual(
            cleaned,
            "https://partner.example/cb https://staging.partner.example/cb "
            "http://localhost:3000/cb",
        )

    def test_a_list_is_accepted_as_well_as_a_string(self):
        self.assertEqual(
            validate_redirect_uris(["https://a.test/cb", "https://b.test/cb"]),
            "https://a.test/cb https://b.test/cb",
        )

    def test_http_is_allowed_for_loopback_only(self):
        for uri in (
            "http://localhost/cb",
            "http://localhost:3000/auth/afc/callback",
            "http://127.0.0.1:8000/cb",
            "http://[::1]:3000/cb",
        ):
            with self.subTest(uri=uri):
                self.assertEqual(validate_redirect_uris(uri), uri)

    def test_http_is_refused_for_a_remote_host(self):
        with self.assertRaises(RedirectURIPolicyError) as ctx:
            validate_redirect_uris("http://partner.example/cb")
        # The message has to name the offending URI: an admin who pasted three of them
        # needs to know which one.
        self.assertIn("http://partner.example/cb", str(ctx.exception))
        self.assertIn("https", str(ctx.exception))

    def test_a_lookalike_loopback_host_is_still_refused(self):
        """"localhost.attacker.example" is a remote host that merely starts with the word,
        and "127.0.0.1.attacker.example" likewise. Matching has to be on the whole host."""
        for uri in (
            "http://localhost.attacker.example/cb",
            "http://127.0.0.1.attacker.example/cb",
            "http://notlocalhost/cb",
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(RedirectURIPolicyError):
                    validate_redirect_uris(uri)

    def test_wildcards_are_refused(self):
        for uri in (
            "https://*.partner.example/cb",
            "https://partner.example/*",
            "https://partner.example/cb?next=*",
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(RedirectURIPolicyError) as ctx:
                    validate_redirect_uris(uri)
                self.assertIn("Wildcards", str(ctx.exception))

    def test_fragments_are_refused(self):
        for uri in ("https://partner.example/cb#token", "https://partner.example/cb#"):
            with self.subTest(uri=uri):
                with self.assertRaises(RedirectURIPolicyError) as ctx:
                    validate_redirect_uris(uri)
                self.assertIn("fragment", str(ctx.exception))

    def test_a_scheme_that_is_not_http_or_https_is_refused(self):
        for uri in ("ftp://partner.example/cb", "javascript:alert(1)", "partner.example/cb"):
            with self.subTest(uri=uri):
                with self.assertRaises(RedirectURIPolicyError):
                    validate_redirect_uris(uri)

    def test_a_uri_with_no_host_is_refused(self):
        with self.assertRaises(RedirectURIPolicyError):
            validate_redirect_uris("https:///cb")

    def test_empty_is_refused_when_required_and_allowed_when_not(self):
        with self.assertRaises(RedirectURIPolicyError):
            validate_redirect_uris("", required=True)
        # post_logout_redirect_uris: a partner that does not use RP-initiated logout has
        # no reason to register anything.
        self.assertEqual(validate_redirect_uris("", required=False), "")

    def test_one_bad_uri_fails_the_whole_list(self):
        """Partial acceptance would leave the partner with a set they did not ask for."""
        with self.assertRaises(RedirectURIPolicyError) as ctx:
            validate_redirect_uris(
                "https://good.example/cb http://bad.example/cb https://also-good.example/cb")
        self.assertIn("http://bad.example/cb", str(ctx.exception))


class RedirectPolicyModelTests(TestCase):
    """full_clean(), which is the path the Django admin takes."""

    def _application(self, **overrides):
        fields = {
            "name": "Policy Org",
            "redirect_uris": "https://partner.test/cb",
            "client_type": Application.CLIENT_CONFIDENTIAL,
            "authorization_grant_type": Application.GRANT_AUTHORIZATION_CODE,
        }
        fields.update(overrides)
        return Application(**fields)

    def test_clean_accepts_a_legal_set(self):
        app = self._application(
            redirect_uris="https://partner.test/cb http://localhost:3000/cb",
            post_logout_redirect_uris="https://partner.test/",
        )
        app.full_clean(exclude=["client_secret"])  # must not raise

    def test_clean_refuses_remote_http(self):
        app = self._application(redirect_uris="http://partner.test/cb")
        with self.assertRaises(ValidationError) as ctx:
            app.full_clean(exclude=["client_secret"])
        self.assertIn("redirect_uris", ctx.exception.error_dict)

    def test_clean_refuses_a_wildcard(self):
        app = self._application(redirect_uris="https://*.partner.test/cb")
        with self.assertRaises(ValidationError) as ctx:
            app.full_clean(exclude=["client_secret"])
        self.assertIn("redirect_uris", ctx.exception.error_dict)

    def test_clean_applies_the_same_policy_to_post_logout_uris(self):
        """The post-logout URI is somewhere AFC sends a real player, so it is the same
        problem and gets the same rules. The error is reported against ITS field, so the
        admin form prints it next to the right textarea."""
        app = self._application(post_logout_redirect_uris="http://partner.test/bye")
        with self.assertRaises(ValidationError) as ctx:
            app.full_clean(exclude=["client_secret"])
        self.assertIn("post_logout_redirect_uris", ctx.exception.error_dict)
        self.assertNotIn("redirect_uris", ctx.exception.error_dict)


class RedirectPolicyApiTests(TestCase):
    """The staff API, which is where these actually get typed."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="policyadmin", email="policyadmin@afc.test", password="x")
        head_admin, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=self.admin, role=head_admin)
        SessionToken.objects.create(user=self.admin, token="tok-policy-admin")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": "Bearer tok-policy-admin"}

    def _create(self, **overrides):
        body = {"name": "Policy Partner", "redirect_uris": "https://partner.test/cb"}
        body.update(overrides)
        return self.client.post(
            APPS_URL, data=json.dumps(body), content_type="application/json", **self._auth())

    def test_create_accepts_three_environments_at_once(self):
        resp = self._create(redirect_uris=[
            "https://partner.test/cb",
            "https://staging.partner.test/cb",
            "http://localhost:3000/cb",
        ])
        self.assertEqual(resp.status_code, 201, resp.content)
        app = Application.objects.get(pk=resp.json()["application"]["application_id"])
        self.assertEqual(
            app.redirect_uris,
            "https://partner.test/cb https://staging.partner.test/cb http://localhost:3000/cb",
        )

    def test_create_refuses_remote_http_and_creates_nothing(self):
        before = Application.objects.count()
        resp = self._create(redirect_uris="http://partner.test/cb")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("http://partner.test/cb", resp.json()["message"])
        self.assertEqual(Application.objects.count(), before)

    def test_create_refuses_a_wildcard_and_a_fragment(self):
        self.assertEqual(self._create(redirect_uris="https://*.partner.test/cb").status_code, 400)
        self.assertEqual(self._create(redirect_uris="https://partner.test/cb#x").status_code, 400)

    def test_create_accepts_and_stores_post_logout_uris(self):
        resp = self._create(post_logout_redirect_uris="https://partner.test/ https://partner.test/bye")
        self.assertEqual(resp.status_code, 201, resp.content)
        app = Application.objects.get(pk=resp.json()["application"]["application_id"])
        self.assertEqual(
            app.post_logout_redirect_uris, "https://partner.test/ https://partner.test/bye")

    def test_create_refuses_a_bad_post_logout_uri(self):
        resp = self._create(post_logout_redirect_uris="http://partner.test/bye")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("http://partner.test/bye", resp.json()["message"])

    def test_edit_applies_the_policy_and_leaves_the_old_value_alone_on_refusal(self):
        app = Application.objects.get(
            pk=self._create().json()["application"]["application_id"])

        resp = self.client.patch(
            f"{APPS_URL}{app.pk}/",
            data=json.dumps({"redirect_uris": "http://partner.test/cb"}),
            content_type="application/json", **self._auth())
        self.assertEqual(resp.status_code, 400)
        app.refresh_from_db()
        self.assertEqual(app.redirect_uris, "https://partner.test/cb")

        resp = self.client.patch(
            f"{APPS_URL}{app.pk}/",
            data=json.dumps({"redirect_uris": ["https://partner.test/cb", "http://127.0.0.1:3000/cb"]}),
            content_type="application/json", **self._auth())
        self.assertEqual(resp.status_code, 200, resp.content)
        app.refresh_from_db()
        self.assertEqual(app.redirect_uris, "https://partner.test/cb http://127.0.0.1:3000/cb")

    def test_edit_can_withdraw_post_logout_uris(self):
        """A partner dropping RP-initiated logout should be able to clear them rather than
        leave stale URLs registered."""
        app = Application.objects.get(
            pk=self._create(post_logout_redirect_uris="https://partner.test/").json()
            ["application"]["application_id"])

        resp = self.client.patch(
            f"{APPS_URL}{app.pk}/",
            data=json.dumps({"post_logout_redirect_uris": ""}),
            content_type="application/json", **self._auth())
        self.assertEqual(resp.status_code, 200, resp.content)
        app.refresh_from_db()
        self.assertEqual(app.post_logout_redirect_uris, "")

    def test_the_detail_response_exposes_both_lists(self):
        app_id = self._create(post_logout_redirect_uris="https://partner.test/").json()[
            "application"]["application_id"]
        detail = self.client.get(f"{APPS_URL}{app_id}/", **self._auth()).json()["application"]
        self.assertEqual(detail["redirect_uris"], "https://partner.test/cb")
        self.assertEqual(detail["post_logout_redirect_uris"], "https://partner.test/")
