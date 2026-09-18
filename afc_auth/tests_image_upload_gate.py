"""
afc_auth/tests_image_upload_gate.py - require_image_upload() lets a real image through and refuses
the rest, at the door (owner rule R70, 2026-09-18).

Both ways (R28): a PNG whose extension lies passes on its bytes; a text file named .png, an SVG,
and an oversized image are refused with a code. Then the door as a screen sees it: the player
report evidence endpoint answers 400 with the code for a fake image and 201 for a real one.
"""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from PIL import Image
from rest_framework.test import APIClient

from afc_auth.image_utils import IMAGE_TOO_LARGE, NOT_AN_IMAGE, require_image_upload


def _png_bytes(size=(4, 4)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class GateTests(SimpleTestCase):
    def test_a_png_passes_whatever_its_name_says(self):
        up = SimpleUploadedFile("shot.jpg", _png_bytes(), content_type="text/plain")
        out, bad = require_image_upload(up)
        self.assertIsNone(bad)
        self.assertIsNotNone(out)

    def test_text_named_png_is_refused(self):
        up = SimpleUploadedFile("evidence.png", b"<script>alert(1)</script>", content_type="image/png")
        out, bad = require_image_upload(up)
        self.assertEqual((out, bad), (None, NOT_AN_IMAGE))

    def test_svg_is_refused(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        out, bad = require_image_upload(SimpleUploadedFile("logo.svg", svg, content_type="image/svg+xml"))
        self.assertEqual((out, bad), (None, NOT_AN_IMAGE))

    def test_over_the_cap_is_refused_before_decoding(self):
        up = SimpleUploadedFile("big.png", _png_bytes(), content_type="image/png")
        out, bad = require_image_upload(up, max_bytes=10)
        self.assertEqual((out, bad), (None, IMAGE_TOO_LARGE))

    def test_nothing_is_refused(self):
        self.assertEqual(require_image_upload(None), (None, NOT_AN_IMAGE))


class PlayerReportDoorTests(TestCase):
    """The evidence upload on POST auth/report-player/ (file_player_report) goes through the gate."""

    def setUp(self):
        import secrets
        from datetime import timedelta
        from django.utils import timezone
        from afc_auth.models import SessionToken, User

        self.reporter = User.objects.create_user(username="gate_reporter", email="gate.reporter@gmail.com", password="x")
        self.target = User.objects.create_user(username="gate_target", email="gate.target@gmail.com", password="x")
        tok = SessionToken.objects.create(user=self.reporter, token=secrets.token_hex(8),
                                          expires_at=timezone.now() + timedelta(hours=1))
        self.client = APIClient(HTTP_HOST="127.0.0.1")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + tok.token)

    def _post(self, evidence):
        from django.urls import reverse
        return self.client.post(reverse("file_player_report"), {
            "reported_username": self.target.username,
            "category": "cheating",
            "details": "used a wall hack in match 2",
            "evidence": evidence,
        }, format="multipart")

    def test_a_fake_image_is_refused_with_the_code(self):
        r = self._post(SimpleUploadedFile("proof.png", b"not an image at all", content_type="image/png"))
        self.assertEqual(r.status_code, 400, r.content[:200])
        self.assertEqual(r.json().get("code"), NOT_AN_IMAGE)

    def test_a_real_image_is_accepted(self):
        r = self._post(SimpleUploadedFile("proof.png", _png_bytes(), content_type="image/png"))
        self.assertEqual(r.status_code, 201, r.content[:200])
