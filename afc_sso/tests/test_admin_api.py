"""The staff surface that decides what every partner org may learn about a player.

Covers afc_sso/admin_api.py, reached at /sso/admin/. The four things it has to get right,
and the reason each test below exists:

  1. NOBODY BUT AFC STAFF gets in. A player, an organizer, a bad token and a missing
     header all bounce, on every route.
  2. A NEW PARTNER SHARES NOTHING. Every share_* toggle is off at creation, and turning
     one on has to actually persist and actually widen allowed_scopes().
  3. THE SECRET IS SHOWN ONCE. It comes back from create and from rotate and from nowhere
     else, it is not what is stored, and rotating really does replace it.
  4. A SUSPENDED PARTNER CANNOT SIGN ANYONE IN. Suspending through this API has to stop
     the authorize view, not just change a column.
"""
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.test import Client, TestCase
from oauth2_provider.models import get_application_model

from afc_auth.models import Roles, SessionToken, UserRoles

Application = get_application_model()
User = get_user_model()

APPS_URL = "/sso/admin/apps/"
SCOPES_URL = "/sso/admin/scopes/"
GUIDE_URL = "/sso/admin/integration-guide/"


class SSOAdminApiTests(TestCase):
    def setUp(self):
        self.client = Client()

        # ── The AFC staff member. _is_sso_admin gates on the granular UserRoles row, so
        # that is what we attach (the coarse User.role tier is deliberately not required).
        self.admin = User.objects.create_user(
            username="ssoadmin", email="ssoadmin@afc.test", password="x"
        )
        head_admin, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=self.admin, role=head_admin)
        SessionToken.objects.create(user=self.admin, token="tok-admin")

        # ── An ordinary player. Holds a perfectly valid session and no role at all: this
        # is the account every 403 assertion below is made with.
        self.player = User.objects.create_user(
            username="ssoplayer", email="ssoplayer@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.player, token="tok-player")

    # ── helpers ──

    def _auth(self, token="tok-admin"):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _create(self, token="tok-admin", **overrides):
        body = {
            "name": "Partner Org",
            "display_name": "Partner Org Display",
            "redirect_uris": "https://partner.test/cb",
        }
        body.update(overrides)
        return self.client.post(
            APPS_URL, data=json.dumps(body),
            content_type="application/json", **self._auth(token)
        )

    def _created_app(self, **overrides):
        """Create one partner through the API and return (response_json, application)."""
        resp = self._create(**overrides)
        self.assertEqual(resp.status_code, 201, resp.content)
        payload = resp.json()
        return payload, Application.objects.get(pk=payload["application"]["application_id"])

    def _patch(self, application_id, body, token="tok-admin"):
        return self.client.patch(
            f"{APPS_URL}{application_id}/", data=json.dumps(body),
            content_type="application/json", **self._auth(token)
        )

    def _suspend(self, application_id, suspend, token="tok-admin"):
        return self.client.post(
            f"{APPS_URL}{application_id}/suspend/", data=json.dumps({"suspend": suspend}),
            content_type="application/json", **self._auth(token)
        )

    def _rotate(self, application_id, token="tok-admin"):
        return self.client.post(
            f"{APPS_URL}{application_id}/rotate-secret/", data="{}",
            content_type="application/json", **self._auth(token)
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 1) The gate
    # ──────────────────────────────────────────────────────────────────────────
    def test_every_route_refuses_a_player_without_the_role(self):
        """A logged-in player with a real session token gets 403 everywhere, including
        the read-only routes. Knowing which orgs AFC has approved is itself staff-only."""
        _, app = self._created_app()

        cases = [
            ("get", APPS_URL, None),
            ("post", APPS_URL, {"name": "X", "redirect_uris": "https://x.test/cb"}),
            ("get", f"{APPS_URL}{app.pk}/", None),
            ("patch", f"{APPS_URL}{app.pk}/", {"share_email": True}),
            ("post", f"{APPS_URL}{app.pk}/suspend/", {"suspend": True}),
            ("post", f"{APPS_URL}{app.pk}/rotate-secret/", {}),
            ("get", SCOPES_URL, None),
            ("get", GUIDE_URL, None),
        ]
        for method, url, body in cases:
            call = getattr(self.client, method)
            kwargs = {**self._auth("tok-player")}
            if body is not None:
                kwargs.update(data=json.dumps(body), content_type="application/json")
            resp = call(url, **kwargs)
            self.assertEqual(resp.status_code, 403, f"{method.upper()} {url} -> {resp.status_code}")

    def test_a_refused_player_changes_nothing(self):
        """The 403 is not cosmetic: the toggle the player tried to flip is still off."""
        _, app = self._created_app()
        self._patch(app.pk, {"share_email": True}, token="tok-player")
        app.refresh_from_db()
        self.assertFalse(app.share_email)

    def test_missing_and_malformed_and_dead_tokens_are_refused(self):
        resp = self.client.get(APPS_URL)
        self.assertEqual(resp.status_code, 400)

        resp = self.client.get(APPS_URL, HTTP_AUTHORIZATION="tok-admin")
        self.assertEqual(resp.status_code, 400)

        resp = self.client.get(APPS_URL, **self._auth("not-a-real-token"))
        self.assertEqual(resp.status_code, 401)

    def test_partner_admin_role_is_also_allowed(self):
        """head_admin is the catch-all; partner_admin is the dedicated grant for staff who
        only run the partner program. Both manage this page."""
        staff = User.objects.create_user(
            username="partneradmin", email="pa@afc.test", password="x")
        role, _ = Roles.objects.get_or_create(role_name="partner_admin")
        UserRoles.objects.create(user=staff, role=role)
        SessionToken.objects.create(user=staff, token="tok-pa")

        resp = self.client.get(APPS_URL, **self._auth("tok-pa"))
        self.assertEqual(resp.status_code, 200)

    # ──────────────────────────────────────────────────────────────────────────
    # 2) Create, list, detail
    # ──────────────────────────────────────────────────────────────────────────
    def test_create_starts_with_every_toggle_off(self):
        """Least privilege, asserted field by field rather than trusting the model default:
        a partner AFC has just approved can sign a player in and learn nothing about them."""
        payload, app = self._created_app()

        from afc_sso.models import SSO_FIELD_TOGGLES
        for field in SSO_FIELD_TOGGLES:
            self.assertFalse(getattr(app, field), field)
            self.assertFalse(payload["application"][field], field)

        # openid alone: it carries the pairwise sub and nothing else.
        self.assertEqual(app.allowed_scopes(), {"openid"})
        self.assertEqual(payload["application"]["scopes"], ["openid"])
        self.assertEqual(payload["application"]["shared_field_count"], 0)

    def test_create_fixes_the_protocol_shape_and_stamps_the_admin(self):
        _, app = self._created_app()
        self.assertEqual(app.client_type, Application.CLIENT_CONFIDENTIAL)
        self.assertEqual(app.authorization_grant_type, Application.GRANT_AUTHORIZATION_CODE)
        self.assertEqual(app.algorithm, Application.RS256_ALGORITHM)
        self.assertEqual(app.user, self.admin)
        # The consent-screen bypass is pinned off by the model and must stay off.
        self.assertFalse(app.skip_authorization)

    def test_create_requires_a_name_and_a_usable_redirect_uri(self):
        for body, reason in [
            ({"name": "", "redirect_uris": "https://p.test/cb"}, "no name"),
            ({"name": "X", "redirect_uris": ""}, "no redirect uri"),
            ({"name": "X", "redirect_uris": "not-a-url"}, "junk redirect uri"),
            ({"name": "X", "redirect_uris": "http://partner.test/cb"}, "plain http"),
        ]:
            resp = self.client.post(
                APPS_URL, data=json.dumps(body),
                content_type="application/json", **self._auth()
            )
            self.assertEqual(resp.status_code, 400, reason)
        self.assertEqual(Application.objects.count(), 0)

    def test_localhost_redirect_uri_is_allowed_over_http(self):
        """A partner has to be able to develop against their own machine; every host that
        is not localhost still has to be https."""
        _, app = self._created_app(redirect_uris="http://localhost:3000/cb")
        self.assertEqual(app.redirect_uris, "http://localhost:3000/cb")

    def test_display_name_falls_back_to_the_internal_name(self):
        """The consent screen must never ask a player to trust a nameless org."""
        _, app = self._created_app(display_name="")
        self.assertEqual(app.display_name, "Partner Org")

    def test_list_searches_and_paginates(self):
        self._created_app(name="Alpha Org", redirect_uris="https://a.test/cb")
        self._created_app(name="Beta Org", redirect_uris="https://b.test/cb")

        resp = self.client.get(APPS_URL, {"search": "alpha"}, **self._auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["name"], "Alpha Org")

        resp = self.client.get(APPS_URL, {"limit": 1}, **self._auth())
        body = resp.json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["total_count"], 2)
        self.assertTrue(body["has_more"])

    def test_neither_list_nor_detail_ever_carries_a_secret(self):
        payload, app = self._created_app()

        resp = self.client.get(APPS_URL, **self._auth())
        self.assertNotIn("client_secret", json.dumps(resp.json()))

        resp = self.client.get(f"{APPS_URL}{app.pk}/", **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("client_secret", resp.json()["application"])
        # The plaintext handed out at creation must not be recoverable from any read.
        self.assertNotIn(payload["client_secret"], json.dumps(resp.json()))

    def test_detail_404s_for_an_unknown_application(self):
        resp = self.client.get(f"{APPS_URL}99999/", **self._auth())
        self.assertEqual(resp.status_code, 404)

    # ──────────────────────────────────────────────────────────────────────────
    # 3) The toggles
    # ──────────────────────────────────────────────────────────────────────────
    def test_toggles_persist_and_widen_the_allowed_scopes(self):
        """The whole point of the page: flipping a switch has to reach the database AND
        change what the partner is permitted to ask for."""
        _, app = self._created_app()

        resp = self._patch(app.pk, {"share_profile": True, "share_email": True})
        self.assertEqual(resp.status_code, 200, resp.content)

        app.refresh_from_db()
        self.assertTrue(app.share_profile)
        self.assertTrue(app.share_email)
        self.assertEqual(app.allowed_scopes(), {"openid", "profile", "email"})
        self.assertEqual(resp.json()["application"]["scopes"], ["email", "openid", "profile"])
        self.assertEqual(resp.json()["application"]["shared_field_count"], 2)

        # ...and turning one back off narrows it again, in the same breath.
        resp = self._patch(app.pk, {"share_email": False})
        app.refresh_from_db()
        self.assertFalse(app.share_email)
        self.assertEqual(app.allowed_scopes(), {"openid", "profile"})

    def test_patch_only_touches_the_keys_it_is_given(self):
        _, app = self._created_app()
        self._patch(app.pk, {"share_profile": True, "share_team": True})
        self._patch(app.pk, {"display_name": "Renamed"})

        app.refresh_from_db()
        self.assertTrue(app.share_profile)
        self.assertTrue(app.share_team)
        self.assertEqual(app.display_name, "Renamed")

    def test_patch_refuses_the_whole_request_on_an_unknown_field(self):
        """A typo must not look like it worked, so an unknown key fails the entire body
        rather than being quietly dropped alongside the fields that were understood."""
        _, app = self._created_app()
        resp = self._patch(app.pk, {"share_profile": True, "share_evrything": True})
        self.assertEqual(resp.status_code, 400)
        app.refresh_from_db()
        self.assertFalse(app.share_profile)

    def test_status_cannot_be_set_through_patch(self):
        """Suspension is a separate, deliberate action; it must not be reachable by
        smuggling a status field into an edit."""
        _, app = self._created_app()
        resp = self._patch(app.pk, {"status": "suspended"})
        self.assertEqual(resp.status_code, 400)
        app.refresh_from_db()
        self.assertEqual(app.status, "active")

    def test_client_credentials_cannot_be_set_through_patch(self):
        _, app = self._created_app()
        for field in ("client_id", "client_secret", "skip_authorization",
                      "authorization_grant_type", "algorithm"):
            resp = self._patch(app.pk, {field: "x"})
            self.assertEqual(resp.status_code, 400, field)

    def test_patch_validates_urls(self):
        _, app = self._created_app()
        for body in (
            {"logo_url": "not-a-url"},
            {"homepage_url": "javascript:alert(1)"},
            {"deletion_webhook_url": "http://partner.test/gone"},
            {"redirect_uris": "https://ok.test/cb junk"},
        ):
            resp = self._patch(app.pk, body)
            self.assertEqual(resp.status_code, 400, body)

    # ──────────────────────────────────────────────────────────────────────────
    # 4) The secret
    # ──────────────────────────────────────────────────────────────────────────
    def test_the_secret_is_returned_once_and_only_the_hash_is_stored(self):
        payload, app = self._created_app()
        secret = payload["client_secret"]

        self.assertTrue(secret)
        # What is stored is a hash OF that secret, not the secret.
        self.assertNotEqual(app.client_secret, secret)
        self.assertTrue(check_password(secret, app.client_secret))

    def test_editing_a_partner_does_not_break_its_secret(self):
        """The trap this locks down: the stored value is a hash, and every edit calls
        save() again. If save() ever re-hashed the hash, the partner's secret would stop
        working the first time an admin flipped a toggle, with nothing in the UI to say
        so. django-oauth-toolkit guards against it (ClientSecretField.pre_save recognises
        an already-hashed value), and this asserts the guard holds for our model too."""
        payload, app = self._created_app()
        secret = payload["client_secret"]

        self._patch(app.pk, {"share_profile": True, "display_name": "Renamed"})
        self._suspend(app.pk, True)
        self._suspend(app.pk, False)

        app.refresh_from_db()
        self.assertTrue(check_password(secret, app.client_secret))

    def test_a_body_that_is_not_an_object_is_a_400_not_a_500(self):
        for url in (APPS_URL, f"{APPS_URL}1/suspend/"):
            resp = self.client.post(
                url, data=json.dumps(["not", "an", "object"]),
                content_type="application/json", **self._auth()
            )
            self.assertIn(resp.status_code, (400, 404), url)

    def test_redirect_uris_may_be_sent_as_a_list(self):
        """The admin UI edits them one per line; either shape normalises to the single
        space-separated string django-oauth-toolkit matches against."""
        _, app = self._created_app(
            redirect_uris=["https://partner.test/cb", "https://partner.test/cb2"])
        self.assertEqual(
            app.redirect_uris, "https://partner.test/cb https://partner.test/cb2")

    def test_rotation_replaces_the_secret(self):
        payload, app = self._created_app()
        first = payload["client_secret"]
        first_hash = app.client_secret

        resp = self._rotate(app.pk)
        self.assertEqual(resp.status_code, 200, resp.content)
        second = resp.json()["client_secret"]

        self.assertNotEqual(second, first)
        app.refresh_from_db()
        self.assertNotEqual(app.client_secret, first_hash)
        # The new one verifies, the old one no longer does: rotation IS the invalidation,
        # there is no second live secret and no grace period.
        self.assertTrue(check_password(second, app.client_secret))
        self.assertFalse(check_password(first, app.client_secret))

    def test_rotation_leaves_everything_else_alone(self):
        """Rotating a credential must not silently re-grant or re-scope a partner."""
        _, app = self._created_app()
        self._patch(app.pk, {"share_profile": True})

        self._rotate(app.pk)
        app.refresh_from_db()
        self.assertTrue(app.share_profile)
        self.assertEqual(app.status, "active")
        self.assertEqual(app.redirect_uris, "https://partner.test/cb")

    # ──────────────────────────────────────────────────────────────────────────
    # 5) Suspension, end to end
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_suspended_application_cannot_authorize(self):
        """Not just the column: suspending here has to actually stop a sign-in at
        /sso/authorize/, and unsuspending has to let it through again."""
        _, app = self._created_app()
        self._patch(app.pk, {"share_profile": True})

        def authorize():
            return self.client.get(
                "/sso/authorize/",
                {
                    "client_id": app.client_id,
                    "response_type": "code",
                    "redirect_uri": "https://partner.test/cb",
                    "scope": "openid profile",
                    "code_challenge": "x" * 43,
                    "code_challenge_method": "S256",
                },
                headers={"cookie": "auth_token=tok-player"},
            )

        # Baseline: while active, the player is shown the consent screen.
        self.assertEqual(authorize().status_code, 200)

        resp = self._suspend(app.pk, True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "suspended")
        self.assertNotEqual(authorize().status_code, 200)

        # Reversible by design: the partner comes back exactly as it was, toggles included.
        resp = self._suspend(app.pk, False)
        self.assertEqual(resp.json()["status"], "active")
        app.refresh_from_db()
        self.assertTrue(app.share_profile)
        self.assertEqual(authorize().status_code, 200)

    # ──────────────────────────────────────────────────────────────────────────
    # 6) The scope catalogue
    # ──────────────────────────────────────────────────────────────────────────
    def test_scope_catalogue_describes_all_eight_toggles(self):
        """Staff have to be able to see, per switch, the sentence the player will read."""
        from afc_sso.models import SSO_FIELD_TOGGLES

        resp = self.client.get(SCOPES_URL, **self._auth())
        self.assertEqual(resp.status_code, 200)
        toggles = resp.json()["toggles"]

        self.assertEqual([t["field"] for t in toggles], list(SSO_FIELD_TOGGLES))
        for entry in toggles:
            self.assertTrue(entry["scope"])
            self.assertTrue(entry["description"], entry["field"])

    # ──────────────────────────────────────────────────────────────────────────
    # 7) The partner integration guide PDF
    # ──────────────────────────────────────────────────────────────────────────
    # This is the file an admin forwards to a partner org, so the two things worth
    # asserting are that staff really get a PDF and that it is the built one rather than
    # a placeholder. The 403 for a player is covered by the sweep above.
    def test_integration_guide_downloads_as_a_pdf(self):
        resp = self.client.get(GUIDE_URL, **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn(".pdf", resp["Content-Disposition"])

        body = b"".join(resp.streaming_content)
        # A real PDF, not an error page or an empty placeholder file. The length floor is
        # deliberately loose: the guide is over a megabyte, so anything this small means
        # the build output never made it into the deployment.
        self.assertTrue(body.startswith(b"%PDF-"))
        self.assertGreater(len(body), 100_000)

    def test_integration_guide_ships_inside_the_app(self):
        """The served copy has to live in afc_sso/docs/, because the backend deploys on its
        own and cannot read the workspace docs/ folder that docs/build-sso-guide-pdf.mjs
        writes to. If this fails, the build script's copy step did not run."""
        import os

        from afc_sso.admin_api import GUIDE_PATH

        self.assertTrue(os.path.exists(GUIDE_PATH), GUIDE_PATH)
        self.assertTrue(GUIDE_PATH.replace("\\", "/").endswith(
            "afc_sso/docs/afc-sso-integration-guide.pdf"))
