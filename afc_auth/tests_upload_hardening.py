"""
Tests for R87, "an uploaded file never runs and never opens as a page" (owner 2026-09-30, inbox #98).

Every path is tried two ways: with a SCRIPT wearing an image's name and declared type (the attack
in the video the rule came from: "upload malicious JavaScript and add .jpg"), which must be refused,
and with a real file, which must be stored under an opaque name whose extension came from its bytes.

WHAT IS COVERED
  - sniff_upload reads bytes, not names: a PNG called "x.txt" is a PNG, HTML called "x.png" is nothing.
  - require_image_upload stores "x.php.jpg" as "<32 hex>.jpg": no client name reaches storage.
  - OCR screenshots: validate_ocr_images refuses HTML declared image/png.
  - Support attachments: HTML named notes.txt is refused; a PDF is stored opaque, typed by its bytes,
    and downloaded (attachment + nosniff), never shown inline; a picture is shown inline.
  - Market report evidence: HTML declared image/png is refused; a real picture is re-encoded.
  - Ghost-claim evidence: a script declared image/png is refused.
  - Leaderboard fonts: a text file named x.ttf is refused; a real TrueType header is stored opaque.

Run: python manage.py test afc_auth.tests_upload_hardening
"""
import io
import json
import re

from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone
from PIL import Image

from afc_auth.image_utils import require_image_upload
from afc_auth.models import SessionToken, User
from afc_auth.upload_safety import sniff_upload

OPAQUE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")
HTML = b"<html><script>alert(document.cookie)</script></html>"


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 160, 80)).save(buf, "PNG")
    return buf.getvalue()


def upload(name, data, content_type):
    return SimpleUploadedFile(name, data, content_type=content_type)


class SniffTests(SimpleTestCase):
    def test_bytes_decide_not_the_name(self):
        self.assertEqual(sniff_upload(upload("x.txt", png_bytes(), "text/plain"), {"image"})[:2], ("image", ".png"))
        self.assertEqual(sniff_upload(upload("x.png", HTML, "image/png"), {"image", "video", "pdf", "office", "text"}),
                         (None, None, None))
        self.assertEqual(sniff_upload(upload("x.ttf", b"\x00\x01\x00\x00rest", "font/ttf"), {"font"})[:2],
                         ("font", ".ttf"))
        self.assertEqual(sniff_upload(upload("x.ttf", b"just some text", "font/ttf"), {"font"})[0], None)

    def test_image_gate_stores_an_opaque_name(self):
        cleaned, bad = require_image_upload(upload("x.php.jpg", png_bytes(), "image/jpeg"))
        self.assertIsNone(bad)
        self.assertRegex(cleaned.name, OPAQUE)
        self.assertNotIn("php", cleaned.name)
        _none, bad = require_image_upload(upload("x.jpg", HTML, "image/jpeg"))
        self.assertIsNotNone(bad)

    def test_ocr_validator_reads_the_bytes(self):
        from afc_ocr.services.image_validate import validate_ocr_images
        self.assertIsNotNone(validate_ocr_images([upload("board.png", HTML, "image/png")]))
        self.assertIsNone(validate_ocr_images([upload("board.png", png_bytes(), "image/png")]))

    def test_report_evidence_reads_the_bytes(self):
        from afc_player_market.views_moderation import _clean_report_evidence, _validate_report_evidence
        self.assertIsNotNone(_validate_report_evidence([upload("x.png", HTML, "image/png")]))
        err, cleaned = _clean_report_evidence([upload("shot.png", png_bytes(), "image/png")])
        self.assertIsNone(err)
        self.assertRegex(cleaned[0].name, OPAQUE)

    def test_ghost_claim_evidence_reads_the_bytes(self):
        from rest_framework.test import APIRequestFactory

        from afc_rankings.admin_ghost import _read_claim_evidence
        factory = APIRequestFactory()
        bad = factory.post("/", {"evidence_file": upload("x.png", HTML, "image/png")}, format="multipart")
        from rest_framework.request import Request
        from rest_framework.parsers import MultiPartParser
        f, err = _read_claim_evidence(Request(bad, parsers=[MultiPartParser()]))
        self.assertIsNone(f)
        self.assertEqual(err.status_code, 400)
        good = factory.post("/", {"evidence_file": upload("x.png", png_bytes(), "image/png")}, format="multipart")
        f, err = _read_claim_evidence(Request(good, parsers=[MultiPartParser()]))
        self.assertIsNone(err)
        self.assertRegex(f.name, OPAQUE)


class SupportAttachmentTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create(username="desk", email="desk@gmail.com", full_name="Desk",
                                         role="support", password=make_password("x"), is_active=True)
        self.token = SessionToken.objects.create(
            user=self.staff, token=f"tok-desk-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME).token

    def _ticket_with(self, *files):
        from afc_support.views import create_ticket_from_contact
        ticket, message, rejected = create_ticket_from_contact("Ada", "ada@gmail.com", "help", list(files))
        return message, rejected

    def _get(self, att):
        return self.client.get(f"/support/attachments/{att.pk}/", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_html_wearing_txt_is_refused(self):
        message, rejected = self._ticket_with(upload("notes.txt", HTML, "text/plain"))
        self.assertEqual(message.attachments.count(), 0)
        self.assertEqual(rejected[0]["reason"], "type")

    def test_pdf_is_stored_opaque_and_downloaded(self):
        message, rejected = self._ticket_with(upload("receipt.pdf", b"%PDF-1.4\n%fake\n", "text/html"))
        self.assertEqual(rejected, [])
        att = message.attachments.get()
        self.assertRegex(att.file.name.rsplit("/", 1)[-1], OPAQUE)
        self.assertEqual(att.content_type, "application/pdf")
        self.assertEqual(att.original_name, "receipt.pdf")
        resp = self._get(att)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(resp["Content-Type"], "application/octet-stream")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")

    def test_a_picture_opens_inline(self):
        message, _ = self._ticket_with(upload("proof.png", png_bytes(), "image/png"))
        att = message.attachments.get()
        resp = self._get(att)
        self.assertTrue(resp["Content-Disposition"].startswith("inline;"))
        self.assertIn(resp["Content-Type"], ("image/png", "image/jpeg"))

    def test_an_old_row_with_a_declared_html_type_is_never_served_as_html(self):
        message, _ = self._ticket_with(upload("proof.png", png_bytes(), "image/png"))
        att = message.attachments.get()
        type(att).objects.filter(pk=att.pk).update(content_type="text/html")
        resp = self._get(att)
        self.assertEqual(resp["Content-Type"], "application/octet-stream")
        self.assertTrue(resp["Content-Disposition"].startswith("attachment;"))


class FontUploadTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create(username="fontadmin", email="fa@gmail.com", full_name="Fa",
                                         role="admin", password=make_password("x"), is_active=True)
        self.token = SessionToken.objects.create(
            user=self.admin, token=f"tok-fa-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME).token

    def _post(self, f):
        return self.client.post("/organizers/leaderboard-fonts/", {"file": f, "name": "Display"},
                                HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_text_named_ttf_is_refused(self):
        resp = self._post(upload("x.ttf", b"<?php system($_GET['c']); ?>", "font/ttf"))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(json.loads(resp.content)["code"], "ttf_otf_font_files")

    def test_real_font_header_is_stored_opaque(self):
        resp = self._post(upload("Display.ttf", b"\x00\x01\x00\x00" + b"\x00" * 60, "font/ttf"))
        self.assertIn(resp.status_code, (200, 201), resp.content)
        from afc_organizers.models import OrgLeaderboardDesignFont
        stored = OrgLeaderboardDesignFont.objects.latest("pk").file.name.rsplit("/", 1)[-1]
        self.assertRegex(stored, OPAQUE)
        self.assertTrue(stored.endswith(".ttf"))
