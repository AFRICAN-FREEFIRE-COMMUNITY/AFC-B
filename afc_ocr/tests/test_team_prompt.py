"""
Task 2.3 - tests for the team-standings Gemini prompt variant (build_prompt + call_gemini).

The standalone team-format OCR flow needs Gemini to ALSO read a team_name per placement (and the
summed team kills), so build_prompt(prompt_kind="team_standings") asks for it. The solo/default
prompt is unchanged. The parser (call_gemini) tolerates team_name present OR absent (it just
json-loads whatever Gemini returns). The Gemini HTTP call is mocked - never the live API.
"""
import json
from unittest import mock

from django.test import TestCase, override_settings

from afc_ocr.services import gemini


class BuildPromptVariantTests(TestCase):
    def test_team_standings_prompt_requests_team_name(self):
        prompt = gemini.build_prompt([], [], prompt_kind="team_standings")
        self.assertIn("team_name", prompt)

    def test_default_prompt_unchanged_no_team_name_request(self):
        # The default/solo prompt must NOT be altered by the new variant.
        default = gemini.build_prompt([], [])
        solo = gemini.build_prompt([], [], prompt_kind=None)
        self.assertEqual(default, solo)
        # The default prompt is the existing player prompt; it does not ask for a per-placement
        # team_name field (that is the team_standings addition).
        self.assertNotIn('"team_name"', default)


@override_settings(GEMINI_API_KEY="test-key")
class CallGeminiParserTests(TestCase):
    """call_gemini threads prompt_kind into build_prompt and json-parses Gemini's reply. The parse
    must tolerate team_name present OR absent."""

    def _mock_response(self, payload):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]
        }
        return resp

    def test_parses_reply_with_team_name(self):
        payload = {"match_type": "team", "placements": [
            {"placement": 1, "team_name": "Alpha", "players": [{"name": "a", "kills": 3}]},
        ]}
        with mock.patch.object(gemini.requests, "post", return_value=self._mock_response(payload)) as posted:
            out = gemini.call_gemini(b"x", "image/png", [], [], prompt_kind="team_standings")
        # The prompt sent to Gemini must have requested team_name.
        sent_text = posted.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("team_name", sent_text)
        self.assertEqual(out["placements"][0]["team_name"], "Alpha")

    def test_parses_reply_without_team_name(self):
        # Gemini may omit team_name for a row it could not read; the parser must not choke.
        payload = {"match_type": "team", "placements": [
            {"placement": 1, "players": [{"name": "a", "kills": 3}]},
        ]}
        with mock.patch.object(gemini.requests, "post", return_value=self._mock_response(payload)):
            out = gemini.call_gemini(b"x", "image/png", [], [], prompt_kind="team_standings")
        self.assertNotIn("team_name", out["placements"][0])
