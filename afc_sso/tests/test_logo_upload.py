"""The partner logo AFC hosts itself, and refuses to host anything that is not an image.

Covers afc_sso/admin_api.py sso_application_logo (POST /sso/admin/apps/<id>/logo/ and
DELETE on the same path), AFCSSOApplication.logo / resolved_logo_url, and what the consent
screen does with the result.

WHY THIS FILE IS AS PARANOID AS IT IS. The logo is rendered on the consent screen, the page
where a player decides whether to trust a partner with their data, and it is served from
AFC's own media origin. So the four things that have to hold are:

  1. ONLY AFC STAFF can put a file there. A player with a real session cannot, on either verb.
  2. ONLY A REAL IMAGE gets stored. The filename and the Content-Type header are attacker
     controlled and prove nothing, so the guard decodes the bytes. An SVG or an HTML page
     named logo.png is a stored-XSS attempt, not a logo.
  3. THE UPLOAD WINS over a legacy third-party logo_url, so the switch from URL to upload
     needs no data migration and cannot leave two logos disagreeing.
  4. THE CONSENT SCREEN NEVER BREAKS. A partner with no logo at all still renders a page a
     player can read and act on.

MEDIA_ROOT is redirected to a throwaway directory (same idiom as
afc_auth/test_news_overhaul.py) so a passing test never writes into the repo's media/ folder.
"""
import io
import json
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from oauth2_provider.models import get_application_model

from afc_auth.models import Roles, SessionToken, UserRoles

Application = get_application_model()
User = get_user_model()

APPS_URL = "/sso/admin/apps/"

_MEDIA = tempfile.mkdtemp(prefix="afc_sso_logo_media_")


def _png_bytes(size=(64, 64)):
    """A real, decodable PNG. Built with Pillow rather than hand-written bytes so the
    happy-path tests prove the guard accepts a genuine image, not a lucky byte string."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", size, (0, 200, 80, 255)).save(buf, format="PNG")
    return buf.getvalue()


@override_settings(MEDIA_ROOT=_MEDIA)
class SSOApplicationLogoTests(TestCase):
    def setUp(self):
        self.client = Client()

        # AFC staff. _is_sso_admin gates on the granular UserRoles row, so that is what we
        # attach - same setup as tests/test_admin_api.py.
        self.admin = User.objects.create_user(
            username="logoadmin", email="logoadmin@afc.test", password="x"
        )
        head_admin, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=self.admin, role=head_admin)
        SessionToken.objects.create(user=self.admin, token="tok-admin")

        # An ordinary player: a perfectly valid session, no role at all. Every 403 below is
        # made with this account.
        self.player = User.objects.create_user(
            username="logoplayer", email="logoplayer@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.player, token="tok-player")

        self.app = Application.objects.create(
            name="Logo Partner",
            display_name="Logo Partner",
            user=self.admin,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
        )

    @classmethod
    def tearDownClass(cls):
        # Remove every file the upload tests saved under the temp MEDIA_ROOT.
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    # ── helpers ──

    def _auth(self, token="tok-admin"):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _logo_url(self, application_id=None):
        return f"{APPS_URL}{application_id or self.app.pk}/logo/"

    def _upload(self, upload, token="tok-admin"):
        return self.client.post(
            self._logo_url(), data={"logo": upload}, **self._auth(token)
        )

    def _delete(self, token="tok-admin"):
        return self.client.delete(self._logo_url(), **self._auth(token))

    def _detail(self, token="tok-admin"):
        resp = self.client.get(f"{APPS_URL}{self.app.pk}/", **self._auth(token))
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["application"]

    # ──────────────────────────────────────────────────────────────────────────
    # 1) The happy path: a real image is stored and served back
    # ──────────────────────────────────────────────────────────────────────────
    def test_uploading_a_real_png_stores_it_and_serves_it_back(self):
        resp = self._upload(
            SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png")
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # The file really landed in storage, under the field's own folder.
        self.app.refresh_from_db()
        self.assertTrue(self.app.logo)
        self.assertIn("sso_partner_logos/", self.app.logo.name)
        self.assertTrue(self.app.logo.storage.exists(self.app.logo.name))

        # And the response hands the admin UI an ABSOLUTE url it can render straight into
        # an <img> from its own, different origin.
        payload = resp.json()["application"]
        self.assertTrue(payload["logo_image_url"].startswith("http"))
        self.assertIn("sso_partner_logos/", payload["logo_image_url"])
        # The resolved value is the same file: with an upload present, nothing else wins.
        self.assertEqual(payload["logo_display_url"], payload["logo_image_url"])

    def test_the_stored_logo_survives_a_fresh_read_of_the_detail_endpoint(self):
        """Served back means served back on a LATER request, not just echoed by the upload."""
        self._upload(SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png"))
        detail = self._detail()
        self.assertTrue(detail["logo_image_url"].startswith("http"))
        self.assertIn("sso_partner_logos/", detail["logo_display_url"])

    def test_the_stored_filename_is_ours_not_the_uploader_s(self):
        """The original filename is the one part of the upload an attacker fully controls,
        so it is rebuilt. A .html name must never reach the media directory, whatever the
        bytes turned out to be."""
        self._upload(
            SimpleUploadedFile("evil.html", _png_bytes(), content_type="image/png")
        )
        self.app.refresh_from_db()
        self.assertNotIn("evil", self.app.logo.name)
        self.assertNotIn(".html", self.app.logo.name)
        self.assertTrue(self.app.logo.name.endswith((".png", ".jpg")))

    def test_replacing_a_logo_drops_the_file_it_replaced(self):
        """Repeated uploads must not accumulate dead files under MEDIA_ROOT.

        The old file is deleted BEFORE the new one is written, which frees the name for
        reuse - so the invariant to assert is 'one file, and it is the new one', not 'the
        name changed'. A leak would show up here as a second file in the folder."""
        from PIL import Image

        self._upload(SimpleUploadedFile("a.png", _png_bytes(), content_type="image/png"))
        self._upload(
            SimpleUploadedFile("b.png", _png_bytes((80, 80)), content_type="image/png")
        )
        self.app.refresh_from_db()

        storage = self.app.logo.storage
        _dirs, files = storage.listdir("sso_partner_logos")
        self.assertEqual(len(files), 1, files)

        # The surviving file is the SECOND upload, not the first.
        with self.app.logo.open("rb") as fh:
            self.assertEqual(Image.open(fh).size, (80, 80))

    # ──────────────────────────────────────────────────────────────────────────
    # 2) What is refused, and why each one matters
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_non_image_is_rejected_however_it_labels_itself(self):
        """The filename says .png and the header says image/png. Neither is evidence: the
        guard decodes the bytes, and these do not decode."""
        resp = self._upload(
            SimpleUploadedFile("logo.png", b"this is not an image", content_type="image/png")
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not a readable image", resp.json()["message"])
        self.app.refresh_from_db()
        self.assertFalse(self.app.logo)

    def test_an_svg_is_rejected(self):
        """The concrete attack this guard exists for: an SVG can carry <script>, and it
        would be served from AFC's own media origin. Pillow cannot open one, so it is
        refused for the same reason any other non-raster file is."""
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        resp = self._upload(
            SimpleUploadedFile("logo.png", svg, content_type="image/svg+xml")
        )
        self.assertEqual(resp.status_code, 400)
        self.app.refresh_from_db()
        self.assertFalse(self.app.logo)

    def test_an_oversized_file_is_rejected(self):
        """Over the cap (10 MB since 2026-08-05, raised from 2 MB). The size check runs FIRST,
        before any decode, so the bytes need not be a valid image to exercise it, and the message
        says SIZE rather than format, which is what proves the ordering.

        The cap is read from MAX_LOGO_BYTES rather than hardcoded here. When it moved from 2 MB to
        10 MB this test kept passing for the wrong reason: a 2 MB blob of "x" sailed past the new
        size gate and was refused by the DECODE gate instead, so the assertion on the message was
        the only thing that caught it. Deriving the fixture from the constant means the next change
        to the cap cannot quietly stop testing the size path.
        """
        from afc_sso.provisioning import MAX_LOGO_BYTES

        big = SimpleUploadedFile(
            "big.png", b"x" * (MAX_LOGO_BYTES + 1), content_type="image/png"
        )
        resp = self._upload(big)
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            f"{MAX_LOGO_BYTES // (1024 * 1024)} MB or smaller", resp.json()["message"])
        self.app.refresh_from_db()
        self.assertFalse(self.app.logo)

    def test_an_upload_with_no_file_is_a_400_not_a_500(self):
        resp = self.client.post(self._logo_url(), data={}, **self._auth())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("required", resp.json()["message"])

    def test_a_missing_application_is_a_404(self):
        resp = self.client.post(
            self._logo_url(999999),
            data={"logo": SimpleUploadedFile("l.png", _png_bytes(), content_type="image/png")},
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 404)

    # ──────────────────────────────────────────────────────────────────────────
    # 3) The gate
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_player_cannot_upload_or_remove_a_logo(self):
        """A valid login without the role is refused on BOTH verbs. Deciding what players
        are shown on the consent screen is staff-only."""
        resp = self._upload(
            SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png"),
            token="tok-player",
        )
        self.assertEqual(resp.status_code, 403)

        resp = self._delete(token="tok-player")
        self.assertEqual(resp.status_code, 403)

    def test_a_refused_upload_changes_nothing(self):
        """The 403 is not cosmetic: no file was stored."""
        self._upload(
            SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png"),
            token="tok-player",
        )
        self.app.refresh_from_db()
        self.assertFalse(self.app.logo)

    def test_missing_and_dead_tokens_are_refused(self):
        resp = self.client.post(self._logo_url(), data={})
        self.assertEqual(resp.status_code, 400)

        resp = self.client.post(
            self._logo_url(), data={}, HTTP_AUTHORIZATION="Bearer nope"
        )
        self.assertEqual(resp.status_code, 401)

    # ──────────────────────────────────────────────────────────────────────────
    # 4) Resolution: the uploaded file beats a legacy URL
    # ──────────────────────────────────────────────────────────────────────────
    def test_the_uploaded_file_wins_over_a_legacy_url(self):
        """The rule that makes the URL-to-upload switch safe without a data migration."""
        self.app.logo_url = "https://partner.test/their-logo.png"
        self.app.save()

        self._upload(SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png"))
        self.app.refresh_from_db()

        resolved = self.app.resolved_logo_url()
        self.assertIn("sso_partner_logos/", resolved)
        self.assertNotIn("partner.test", resolved)

    def test_a_legacy_url_is_still_used_when_there_is_no_file(self):
        """Nothing breaks mid-migration: a row that only ever had a URL keeps rendering."""
        self.app.logo_url = "https://partner.test/their-logo.png"
        self.app.save()
        self.assertEqual(
            self.app.resolved_logo_url(), "https://partner.test/their-logo.png"
        )
        self.assertEqual(self.app.logo_file_url(), "")

    def test_an_application_with_neither_resolves_to_empty(self):
        self.assertEqual(self.app.resolved_logo_url(), "")
        self.assertEqual(self.app.logo_file_url(), "")

    def test_the_detail_endpoint_reports_a_legacy_url_distinctly(self):
        """The UI has to tell 'AFC hosts this' from 'the partner still hosts this', so it
        can prompt staff to upload a file. logo_image_url empty + logo_url set is that state."""
        self.app.logo_url = "https://partner.test/their-logo.png"
        self.app.save()
        detail = self._detail()
        self.assertEqual(detail["logo_image_url"], "")
        self.assertEqual(detail["logo_url"], "https://partner.test/their-logo.png")
        self.assertEqual(
            detail["logo_display_url"], "https://partner.test/their-logo.png"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 5) Removal
    # ──────────────────────────────────────────────────────────────────────────
    def test_delete_removes_the_file_and_the_legacy_url(self):
        """An admin sees ONE logo, so removing it removes it. Dropping only the file would
        let the legacy third-party URL pop back onto the consent screen."""
        self.app.logo_url = "https://partner.test/their-logo.png"
        self.app.save()
        self._upload(SimpleUploadedFile("logo.png", _png_bytes(), content_type="image/png"))
        self.app.refresh_from_db()
        stored = self.app.logo.name

        resp = self._delete()
        self.assertEqual(resp.status_code, 200, resp.content)

        self.app.refresh_from_db()
        self.assertFalse(self.app.logo)
        self.assertEqual(self.app.logo_url, "")
        self.assertEqual(self.app.resolved_logo_url(), "")
        self.assertFalse(self.app.logo.storage.exists(stored))

    def test_delete_on_an_application_with_no_logo_is_still_a_clean_200(self):
        """Idempotent: removing nothing is not an error."""
        resp = self._delete()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["application"]["logo_display_url"], "")

    # ──────────────────────────────────────────────────────────────────────────
    # 6) The PATCH whitelist still holds
    # ──────────────────────────────────────────────────────────────────────────
    def test_the_logo_file_field_cannot_be_set_through_the_json_patch(self):
        """`logo` is a file and is not in IDENTITY_FIELDS, so the detail endpoint refuses
        it like any other unknown key rather than silently ignoring it."""
        resp = self.client.patch(
            f"{APPS_URL}{self.app.pk}/",
            data=json.dumps({"logo": "https://evil.test/x.png"}),
            content_type="application/json",
            **self._auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown field", resp.json()["message"])


# ──────────────────────────────────────────────────────────────────────────────
# 7) The consent screen, which is the whole reason any of the above matters
# ──────────────────────────────────────────────────────────────────────────────
@override_settings(MEDIA_ROOT=_MEDIA)
class ConsentScreenLogoTests(TestCase):
    """The screen must render whatever the logo situation is: uploaded, legacy, or none."""

    AUTH_COOKIE = {"cookie": "auth_token=tok-consent-logo"}

    def setUp(self):
        self.user = User.objects.create_user(
            username="consentlogo", email="consentlogo@afc.test", password="x"
        )
        SessionToken.objects.create(user=self.user, token="tok-consent-logo")
        self.app = Application.objects.create(
            name="Logo Org",
            user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://partner.test/cb",
            algorithm=Application.RS256_ALGORITHM,
            share_profile=True,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def _authorize(self):
        return self.client.get(
            "/sso/authorize/",
            {
                "client_id": self.app.client_id,
                "response_type": "code",
                "redirect_uri": "https://partner.test/cb",
                "scope": "openid profile",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
            },
            headers=self.AUTH_COOKIE,
        )

    def test_a_partner_with_no_logo_still_renders_a_usable_screen(self):
        """The one that must never regress: a player who cannot read this page cannot make
        a decision on it, so a missing logo means no <img>, not an error."""
        resp = self._authorize()
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Logo Org", body)
        self.assertNotIn('class="org-logo"', body)

    def test_the_uploaded_logo_is_what_the_player_is_shown(self):
        self.app.logo.save("partner-logo.png", SimpleUploadedFile(
            "partner-logo.png", _png_bytes(), content_type="image/png"))
        resp = self._authorize()
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('class="org-logo"', body)
        self.assertIn("sso_partner_logos/", body)

    def test_a_legacy_url_still_renders_for_a_partner_with_no_file(self):
        self.app.logo_url = "https://partner.test/their-logo.png"
        self.app.save()
        body = self._authorize().content.decode()
        self.assertIn("https://partner.test/their-logo.png", body)

    def test_an_uploaded_file_replaces_the_legacy_url_on_the_screen(self):
        """The player sees AFC's copy, never the partner's server, once a file exists."""
        self.app.logo_url = "https://partner.test/their-logo.png"
        self.app.logo.save("partner-logo.png", SimpleUploadedFile(
            "partner-logo.png", _png_bytes(), content_type="image/png"))
        body = self._authorize().content.decode()
        self.assertIn("sso_partner_logos/", body)
        self.assertNotIn("partner.test/their-logo.png", body)
