"""
Task 2.3 - tests for the team-standings Gemini prompt variant (build_prompt + call_gemini).

The standalone team-format OCR flow needs Gemini to ALSO read a team_name per placement (and the
summed team kills), so build_prompt(prompt_kind="team_standings") asks for it. The solo/default
prompt is unchanged. The parser (call_gemini) tolerates team_name present OR absent (it just
json-loads whatever Gemini returns). The Gemini HTTP call is mocked - never the live API.
"""
import json
from unittest import mock

import requests
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


@override_settings(GEMINI_API_KEY="super-secret-key")
class CallGeminiErrorSanitizationTests(TestCase):
    """An HTTP error from Gemini must NEVER surface the request URL: requests puts the full URL
    (incl. ?key=<GEMINI_API_KEY>) in the HTTPError message, and that string is persisted on OCR
    job rows (afc_leaderboard.ocr.process_job -> job.error) and rendered in the review dialog.
    call_gemini must re-raise a key-free RuntimeError carrying only the status + Gemini's own
    error detail (found live 2026-06-11: a 400 leaked the key into the organizer's UI)."""

    def _error_response(self, status_code=400, detail="Invalid image data."):
        import requests as _requests
        resp = mock.Mock()
        url_with_key = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent?key=super-secret-key"
        )
        resp.status_code = status_code
        resp.raise_for_status.side_effect = _requests.HTTPError(
            f"{status_code} Client Error: Bad Request for url: {url_with_key}"
        )
        resp.json.return_value = {"error": {"message": detail}}
        return resp

    def test_http_error_message_is_key_free(self):
        with mock.patch.object(gemini.requests, "post", return_value=self._error_response()):
            with self.assertRaises(RuntimeError) as ctx:
                gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertNotIn("super-secret-key", msg)
        self.assertNotIn("key=", msg)
        self.assertIn("HTTP 400", msg)
        # Gemini's own (key-free) detail is kept so the admin still sees WHY it failed.
        self.assertIn("Invalid image data.", msg)

    def test_http_error_without_json_body_still_key_free(self):
        # A non-JSON error body (e.g. an HTML 502 page) must not break the sanitizer.
        resp = self._error_response(status_code=502)
        resp.json.side_effect = ValueError("not json")
        with mock.patch.object(gemini.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertNotIn("super-secret-key", msg)
        self.assertIn("HTTP 502", msg)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# A5 / A6 / A9 - reliability fixes on call_gemini (timeout cap, bounded 429/503 retry, defensive
# parse). Every case mocks the requests.post boundary so NO live Gemini call is made, and asserts
# the raised message is FRIENDLY + key-free (the URL carries ?key=<GEMINI_API_KEY>, so a leak would
# expose the secret in the review dialog). A distinctive fake key is used so any leak is obvious.
# Follows the CallGeminiErrorSanitizationTests idiom above.
# ══════════════════════════════════════════════════════════════════════════════════════════════

_FAKE_KEY = "leaky-secret-key-xyz"


def _ok_resp(payload):
    """A healthy 200 Gemini response carrying `payload` as the candidate JSON text."""
    resp = mock.Mock()
    resp.status_code = 200
    resp.headers = {}
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}
    return resp


def _status_resp(status_code, detail="upstream detail"):
    """A non-2xx response whose raise_for_status raises an HTTPError embedding the ?key= URL (as
    real requests does), so we can prove call_gemini strips it. json() carries Gemini's own detail."""
    resp = mock.Mock()
    resp.status_code = status_code
    resp.headers = {}
    resp.raise_for_status.side_effect = requests.HTTPError(
        f"{status_code} Client Error for url: https://x/y:generateContent?key={_FAKE_KEY}"
    )
    resp.json.return_value = {"error": {"message": detail}}
    return resp


def _raw_json_resp(body):
    """A 200 response with an arbitrary parsed body (for the A9 defensive-parse cases)."""
    resp = mock.Mock()
    resp.status_code = 200
    resp.headers = {}
    resp.raise_for_status.return_value = None
    resp.json.return_value = body
    return resp


@override_settings(GEMINI_API_KEY=_FAKE_KEY)
class CallGeminiRetryTests(TestCase):
    """A6: bounded exponential backoff on transient 429/503 only; never retry 400/401/404."""

    def test_retries_once_then_succeeds_on_429(self):
        payload = {"match_type": "team", "placements": []}
        responses = [_status_resp(429), _ok_resp(payload)]
        with mock.patch("afc_ocr.services.gemini.time.sleep"):   # do not actually sleep
            with mock.patch.object(gemini.requests, "post", side_effect=responses) as posted:
                out = gemini.call_gemini(b"x", "image/png", [], [])
        self.assertEqual(posted.call_count, 2)   # one retry after the 429
        self.assertEqual(out, payload)

    def test_no_retry_on_400(self):
        with mock.patch("afc_ocr.services.gemini.time.sleep") as slept:
            with mock.patch.object(gemini.requests, "post", return_value=_status_resp(400)) as posted:
                with self.assertRaises(RuntimeError) as ctx:
                    gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertEqual(posted.call_count, 1)   # 400 is permanent -> no retry
        slept.assert_not_called()
        self.assertIn("HTTP 400", msg)
        self.assertNotIn(_FAKE_KEY, msg)
        self.assertNotIn("key=", msg)

    def test_gives_up_after_max_retries_503(self):
        # Persistent 503: total attempts = 1 + GEMINI_MAX_RETRIES (default 2) = 3, then the key-free
        # HTTP 503 error is raised.
        with mock.patch("afc_ocr.services.gemini.time.sleep"):
            with mock.patch.object(gemini.requests, "post", return_value=_status_resp(503)) as posted:
                with self.assertRaises(RuntimeError) as ctx:
                    gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertEqual(posted.call_count, 3)
        self.assertIn("HTTP 503", msg)
        self.assertNotIn(_FAKE_KEY, msg)

    def test_backoff_capped(self):
        # Each slept delay must stay under the 4.0s cap so the worst case fits the gateway budget.
        delays = []
        with mock.patch("afc_ocr.services.gemini.time.sleep", side_effect=lambda d: delays.append(d)):
            with mock.patch.object(gemini.requests, "post", return_value=_status_resp(503)):
                with self.assertRaises(RuntimeError):
                    gemini.call_gemini(b"x", "image/png", [], [])
        self.assertTrue(delays)                       # it did back off
        for d in delays:
            self.assertLessEqual(d, 4.0)


@override_settings(GEMINI_API_KEY=_FAKE_KEY)
class CallGeminiDefensiveParseTests(TestCase):
    """A9: a SAFETY block / empty read / non-JSON body must raise a FRIENDLY, key-free message
    instead of a KeyError/IndexError that the caller turns into a cryptic 503."""

    def test_empty_candidates_raises_friendly(self):
        body = {"promptFeedback": {"blockReason": "SAFETY"}}
        with mock.patch.object(gemini.requests, "post", return_value=_raw_json_resp(body)):
            with self.assertRaises(RuntimeError) as ctx:
                gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertIn("no result", msg)
        self.assertIn("SAFETY", msg)
        self.assertNotIn(_FAKE_KEY, msg)

    def test_candidate_without_parts_raises_friendly(self):
        body = {"candidates": [{"finishReason": "SAFETY", "content": {}}]}
        with mock.patch.object(gemini.requests, "post", return_value=_raw_json_resp(body)):
            with self.assertRaises(RuntimeError) as ctx:
                gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertIn("could not read this image", msg)
        self.assertNotIn(_FAKE_KEY, msg)

    def test_non_json_text_raises_friendly(self):
        body = {"candidates": [{"content": {"parts": [{"text": "not json at all"}]}}]}
        with mock.patch.object(gemini.requests, "post", return_value=_raw_json_resp(body)):
            with self.assertRaises(RuntimeError) as ctx:
                gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertIn("unreadable", msg)
        self.assertNotIn(_FAKE_KEY, msg)


@override_settings(GEMINI_API_KEY=_FAKE_KEY)
class CallGeminiTimeoutTests(TestCase):
    """A5: a socket timeout raises a friendly key-free message, and the socket timeout is read from
    settings.GEMINI_HTTP_TIMEOUT so ops can tune it under the prod gateway budget."""

    def test_timeout_raises_friendly(self):
        with mock.patch.object(gemini.requests, "post", side_effect=requests.Timeout("read timed out")):
            with self.assertRaises(RuntimeError) as ctx:
                gemini.call_gemini(b"x", "image/png", [], [])
        msg = str(ctx.exception)
        self.assertIn("took too long", msg)
        self.assertNotIn(_FAKE_KEY, msg)

    @override_settings(GEMINI_HTTP_TIMEOUT=5)
    def test_timeout_setting_is_read(self):
        payload = {"match_type": "team", "placements": []}
        with mock.patch.object(gemini.requests, "post", return_value=_ok_resp(payload)) as posted:
            gemini.call_gemini(b"x", "image/png", [], [])
        # requests.post must be called with the overridden socket timeout.
        self.assertEqual(posted.call_args.kwargs["timeout"], 5)
