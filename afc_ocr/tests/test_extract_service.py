"""
Task 2.1 - tests for the shared OCR extraction service afc_ocr.services.extract.extract_rows.

The body of views._extract_with_router was lifted into extract.extract_rows so BOTH the event
OCR views (upload_ocr_session / ocr_from_stored_image) and the new P2 standalone-leaderboard
endpoints (afc_leaderboard.views.ocr_extract) can share ONE local-first-then-Gemini router.

These tests prove the lift is behavior-preserving:
  - extract_rows routes through Gemini when local-first is off / unavailable (mocked HTTP);
  - views._extract_with_router still delegates to extract_rows and returns identical output.
The Gemini HTTP call is ALWAYS mocked here - a test must never hit the live API.
"""
from unittest import mock

from django.test import TestCase, override_settings

from afc_ocr.services import extract
from afc_ocr import views as ocr_views


# A canonical Gemini-shape raw output the mocked engine returns. extract_rows must pass it back
# untouched (the placement/player shape the draft-row build + match_name + commit path expect).
_RAW = {"match_type": "team", "placements": [{"placement": 1, "players": [{"name": "Foo", "kills": 3}]}]}


@override_settings(OCR_LOCAL_FIRST=False, OCR_GEMINI_FALLBACK=True, GEMINI_API_KEY="test-key")
class ExtractRowsTests(TestCase):
    """extract.extract_rows is the single extraction entry point. With local-first OFF and a key
    present it must go straight to Gemini and return (raw_output, "gemini-2.5-pro")."""

    def test_routes_to_gemini_when_local_first_off(self):
        # Mock the Gemini call inside the extract module (where it is imported) so no HTTP fires.
        with mock.patch.object(extract, "call_gemini", return_value=_RAW) as mocked:
            raw, engine = extract.extract_rows(
                image_bytes=b"\x89PNG", mime_type="image/png", event_type="team",
                aliases=[], team_notes=[],
            )
        mocked.assert_called_once()
        self.assertEqual(raw, _RAW)
        self.assertEqual(engine, "gemini-2.5-pro")

    def test_no_engine_available_raises(self):
        # Local-first off AND Gemini disabled => no engine ran => RuntimeError (the old 503 path).
        with override_settings(OCR_LOCAL_FIRST=False, OCR_GEMINI_FALLBACK=False):
            with self.assertRaises(RuntimeError):
                extract.extract_rows(b"x", "image/png", "team")


@override_settings(OCR_LOCAL_FIRST=False, OCR_GEMINI_FALLBACK=True, GEMINI_API_KEY="test-key")
class ViewsDelegateTests(TestCase):
    """views._extract_with_router must remain a thin delegate to extract.extract_rows so the event
    OCR flow's output is byte-identical after the lift."""

    def test_router_delegates_to_extract_rows(self):
        with mock.patch.object(extract, "extract_rows", return_value=(_RAW, "gemini-2.5-pro")) as mocked:
            raw, engine = ocr_views._extract_with_router(
                image_bytes=b"\x89PNG", mime_type="image/png",
                aliases=[], team_notes=[], event_type="team",
            )
        mocked.assert_called_once()
        # Delegation must preserve the (raw_output, engine) tuple exactly.
        self.assertEqual(raw, _RAW)
        self.assertEqual(engine, "gemini-2.5-pro")
