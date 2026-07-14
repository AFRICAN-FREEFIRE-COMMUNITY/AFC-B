"""
A8 - server-side image validation on the OCR upload paths.

Two layers of coverage:
  1. UNIT: afc_ocr.services.image_validate.validate_ocr_images directly. It returns a client-safe
     error STRING (or None when every file passes) and NEVER raises. We assert each rejection branch:
     non-image mime, oversize file, too-many-files, and HEIC (by mime AND by extension), plus the
     happy path (valid png -> None).
  2. ENDPOINT SMOKE: POST a valid png to /events/ocr-match-result/ with the Gemini boundary mocked
     (extract_rows patched) and assert a clean 201. This proves the validator does not block a
     legitimate upload while still guarding the real view.

The Gemini HTTP call is never made: the endpoint test patches afc_ocr.services.extract.extract_rows,
which the view reaches via _extract_with_router. MEDIA_ROOT is redirected to a temp dir so the
best-effort MatchResultImage persistence leaves no artifact in the project media folder.
"""
import datetime
import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, SimpleTestCase, Client, override_settings

from afc_auth.models import User, SessionToken
from afc_tournament_and_scrims.models import Event, Stages, StageGroups, Match, Leaderboard
from afc_ocr.services.image_validate import validate_ocr_images


def _png(name="shot.png", body=b"\x89PNG\r\n\x1a\nfake"):
    return SimpleUploadedFile(name, body, content_type="image/png")


class ValidateOcrImagesUnitTests(SimpleTestCase):
    """Pure validator unit tests - deterministic, no DB, no HTTP."""

    def test_rejects_non_image_mime(self):
        f = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        msg = validate_ocr_images([f])
        self.assertIsNotNone(msg)
        self.assertIn("Only PNG", msg)

    @override_settings(OCR_MAX_IMAGE_BYTES=5)   # tiny cap so a 10-byte file is "oversize" without 10MB
    def test_rejects_oversize_file(self):
        f = SimpleUploadedFile("big.png", b"0123456789", content_type="image/png")  # size 10 > cap 5
        msg = validate_ocr_images([f])
        self.assertIsNotNone(msg)
        self.assertIn("smaller", msg)

    def test_rejects_too_many_files(self):
        files = [_png(name=f"s{i}.png") for i in range(9)]   # default OCR_MAX_IMAGES is 8
        msg = validate_ocr_images(files)
        self.assertIsNotNone(msg)
        self.assertIn("Upload up to", msg)

    def test_rejects_heic_by_mime(self):
        f = SimpleUploadedFile("photo", b"heicbytes", content_type="image/heic")
        msg = validate_ocr_images([f])
        self.assertIsNotNone(msg)
        self.assertIn("HEIC", msg)

    def test_rejects_heic_by_extension(self):
        # An odd/empty content type but a .heic name (phones often send this) must still be caught.
        f = SimpleUploadedFile("IMG_0001.heic", b"heicbytes", content_type="application/octet-stream")
        msg = validate_ocr_images([f])
        self.assertIsNotNone(msg)
        self.assertIn("HEIC", msg)

    def test_rejects_empty_list(self):
        msg = validate_ocr_images([])
        self.assertIsNotNone(msg)
        self.assertIn("at least one", msg)

    def test_accepts_valid_png(self):
        self.assertIsNone(validate_ocr_images([_png()]))


class UploadEndpointImageSmokeTests(TestCase):
    """A valid png makes it past validation into a 201 draft (Gemini mocked)."""

    def setUp(self):
        self.client = Client()
        # Redirect media writes to a throwaway dir (the view best-effort-persists a MatchResultImage).
        self._media = tempfile.mkdtemp(prefix="ocr_img_test_media_")
        self.addCleanup(shutil.rmtree, self._media, ignore_errors=True)
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.addCleanup(self._override.disable)

        today = datetime.date.today()
        self.admin = User.objects.create(
            username="ocr_img_admin", email="ocr_img_admin@x.com",
            full_name="OCR Img Admin", role="admin", password="x")
        self.token = SessionToken.objects.create(user=self.admin, token="tok_ocr_img_admin").token
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="solo", event_type="internal",
            max_teams_or_players=48, event_name="OCR Img Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.lb = Leaderboard.objects.create(
            leaderboard_name="Img LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12}, kill_point=1.0,
            leaderboard_method="image_upload")
        self.match = Match.objects.create(
            leaderboard=self.lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12}, "kill_point": 1})

    def test_accepts_valid_png_endpoint(self):
        # Mock the extraction engine so no Gemini/local call happens; the view still runs its
        # validate_ocr_images gate + draft build for real.
        fake = ({"placements": [{"placement": 1, "players": [{"name": " Player", "kills": 2}]}]},
                "gemini-2.5-flash")
        with mock.patch("afc_ocr.services.extract.extract_rows", return_value=fake):
            resp = self.client.post(
                "/events/ocr-match-result/",
                data={"match_id": self.match.match_id, "map_index": 1, "screenshot": _png()},
                HTTP_AUTHORIZATION=f"Bearer {self.token}")

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["status"], "pending_review")

    def test_rejects_more_than_4_screenshots_endpoint(self):
        # A5: upload_ocr_session fans out one Gemini call PER screenshot (concurrent), so N>4 per map
        # serialize into multiple waves and can exceed the ~30s prod gateway budget. The sync event
        # path caps at 4 and returns a 400 with a friendly "add the rest as a second read" message
        # BEFORE any extraction runs. (The shared validator's count cap is 8; this stricter 4-cap is
        # specific to the synchronous event flow. The async standalone batch flow is the path for
        # larger uploads.) Five valid PNGs pass the mime/size validator, then trip the 4-cap.
        five = [_png(name=f"s{i}.png") for i in range(5)]
        with mock.patch("afc_ocr.services.extract.extract_rows") as mocked:
            resp = self.client.post(
                "/events/ocr-match-result/",
                data={"match_id": self.match.match_id, "map_index": 1, "screenshot": five},
                HTTP_AUTHORIZATION=f"Bearer {self.token}")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("4 screenshots", resp.json()["message"])
        mocked.assert_not_called()   # capped before any Gemini/extraction work
