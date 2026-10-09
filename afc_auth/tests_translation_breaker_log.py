"""
afc_auth/tests_translation_breaker_log.py - an open DeepL breaker logs ONE line per window, no
traceback; a fresh engine failure still logs its traceback (inbox #27, owner rule R81).

On 2026-09-18 the production journal carried 84 tracebacks per French or Portuguese page view
(944 lines in eight minutes) because every string translated while the free quota was gone
logged `logger.warning(..., exc_info=True)`. Requests were fine (the original text is served);
the log was not: that noise is where a real error hides.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from afc_auth import translation


@override_settings(DEEPL_API_KEY="x:fx", CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class BreakerLogTests(TestCase):
    # A TestCase, not a SimpleTestCase: translate() reads TranslationCache (an empty table here)
    # before it reaches the engine.

    def setUp(self):
        cache.clear()

    def test_an_open_breaker_logs_once_and_never_a_traceback(self):
        cache.set(translation._ENGINE_DOWN_KEY, True, 300)
        with self.assertLogs("afc_auth.translation", level="WARNING") as logs:
            for i in range(20):
                self.assertEqual(translation.translate(f"Hello {i}", "fr"), f"Hello {i}")
        self.assertEqual(len(logs.records), 1, [r.getMessage() for r in logs.records])
        self.assertIn("circuit breaker is open", logs.records[0].getMessage())
        self.assertIsNone(logs.records[0].exc_info)

    def test_a_fresh_failure_still_logs_its_traceback(self):
        with patch.object(translation, "_call_deepl", side_effect=RuntimeError("boom")):
            with self.assertLogs("afc_auth.translation", level="WARNING") as logs:
                self.assertEqual(translation.translate("Hello", "fr"), "Hello")
        self.assertEqual(len(logs.records), 1)
        self.assertIsNotNone(logs.records[0].exc_info)


class TestRunnerReachesNoDeepLTests(TestCase):
    """Inbox #210 (2026-10-09): a local run on 2026-10-08 logged "DeepL translate failed with HTTP 456:
    Quota exceeded", so a test had reached the live API with the real key from .env. afc/settings.py
    now blanks DEEPL_API_KEY under the test runner, the way it already holds outbound mail. These tests
    have no override_settings on purpose: they read what every other test gets. On a machine whose .env
    carries a key, both fail without that guard (proven 2026-10-09)."""

    def setUp(self):
        cache.clear()

    def test_the_test_runner_holds_no_deepl_key(self):
        from django.conf import settings

        self.assertEqual(settings.DEEPL_API_KEY, "")

    def test_a_translation_under_test_never_calls_out(self):
        with patch("afc_auth.translation.requests.post") as post:
            self.assertEqual(translation.translate("Hello from the tests", "fr"), "Hello from the tests")
        post.assert_not_called()
