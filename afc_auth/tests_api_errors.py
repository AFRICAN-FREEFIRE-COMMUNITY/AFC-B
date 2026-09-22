"""
afc_auth/tests_api_errors.py
================================================================================
A handler that blows up tells the user nothing about our database (owner rule R79, 2026-09-22).

Nine handlers answered with `str(e)`: eight in afc_team/views.py and one in afc_player_market.
That text is written by MySQL or by a library, so a failed team action could answer
"(1054, \"Unknown column 'afc_team_teammembers.role' in 'field list'\")" - a table name, a column
name, and nothing the person could act on. They now answer one generic coded sentence while the
exception and its traceback go to the log.

Proven here:
  1. the helper answers the generic sentence and a code, never the exception's own text,
  2. it logs the exception WITH its traceback, so nothing is lost,
  3. no handler in the two files that were fixed still sends str(e) to the client.

Run: python manage.py test afc_auth.tests_api_errors
"""
import io
import os
import re

from django.test import SimpleTestCase

from afc_auth.api_errors import GENERIC_CODE, GENERIC_MESSAGE, internal_error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXED_FILES = ("afc_team/views.py", "afc_player_market/views.py")

# `{"error": str(e)}` / `'error': str(e)` inside a Response: the shape that leaked.
LEAK = re.compile(r"Response\(\s*\{[^}]*str\(\s*e\w*\s*\)", re.S)


class InternalErrorTests(SimpleTestCase):
    def test_the_client_gets_a_sentence_and_a_code_not_the_exception(self):
        exc = ValueError("(1054, \"Unknown column 'afc_team_teammembers.role' in 'field list'\")")
        with self.assertLogs("afc_auth.api_errors", level="ERROR"):
            resp = internal_error(exc, where="a_handler")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.data["message"], GENERIC_MESSAGE)
        self.assertEqual(resp.data["code"], GENERIC_CODE)
        self.assertNotIn("afc_team_teammembers", str(resp.data))
        self.assertNotIn("1054", str(resp.data))

    def test_the_log_keeps_the_exception_and_its_traceback(self):
        try:
            raise KeyError("discord_id")
        except KeyError as exc:
            with self.assertLogs("afc_auth.api_errors", level="ERROR") as caught:
                internal_error(exc, where="assign_role")
        blob = "\n".join(caught.output)
        self.assertIn("assign_role failed", blob)
        self.assertIn("KeyError", blob)
        self.assertIn("Traceback", blob)

    def test_a_caller_can_name_its_own_code(self):
        with self.assertLogs("afc_auth.api_errors", level="ERROR"):
            resp = internal_error(RuntimeError("x"), where="exit_team", code="exit_team_failed")
        self.assertEqual(resp.data["code"], "exit_team_failed")
        self.assertEqual(resp.data["message"], GENERIC_MESSAGE)


class NoHandlerStillLeaksTests(SimpleTestCase):
    """Read the files that were fixed: a new handler must not reintroduce the shape."""

    def test_the_fixed_files_send_no_exception_text(self):
        offenders = []
        for rel in FIXED_FILES:
            path = os.path.join(REPO, rel.replace("/", os.sep))
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                for number, line in enumerate(fh, start=1):
                    if line.lstrip().startswith("#"):
                        continue                      # commented-out code is not shipped code
                    if LEAK.search(line):
                        offenders.append("%s:%d %s" % (rel, number, line.strip()[:90]))
        self.assertEqual(offenders, [], "these answer with the exception's own text; use "
                                        "afc_auth.api_errors.internal_error instead:\n" +
                                        "\n".join(offenders))
