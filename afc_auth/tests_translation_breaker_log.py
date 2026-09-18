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
