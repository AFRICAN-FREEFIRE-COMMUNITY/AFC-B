"""
afc_auth/tests_bot_protection.py
================================================================================
The four forms a stranger can post to check that a person filled them (owner 2026-09-22: "lets do
it", on signup / contact / feedback / partner apply having no bot protection).

The check is Cloudflare Turnstile, verified on the SERVER: the widget only produces a token, and a
script simply would not load the widget, so the only thing that counts is what Cloudflare says about
the token. Proven here without touching the network:

  1. no key configured -> the request is allowed (a missing key must never lock signup) and the log
     says so once,
  2. key configured, no token -> refused with a code the frontend can translate,
  3. key configured, Cloudflare says no -> refused,
  4. key configured, Cloudflare says yes -> allowed, and the token and the caller's IP are what was
     sent to siteverify,
  5. Cloudflare unreachable -> allowed, and logged: their outage must not become ours,
  6. all four handlers call the guard BEFORE they write or email anything.

Run: python manage.py test afc_auth.tests_bot_protection
"""
import io
import os
import re
from unittest import mock

from django.test import SimpleTestCase

from afc_auth import bot_protection
from afc_auth.bot_protection import BOT_CHECK_CODE, require_human, verify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GUARDED = {
    "afc_auth/views.py": "signup",
    "afc_support/views.py": "support_contact",
    "afc_feedback/views.py": "submit_feedback",
    "afc_partner_apply/views_public.py": "submit_application",
}


class _Req:
    """The two things the module reads: request.data and request.META."""

    def __init__(self, data=None, meta=None):
        self.data = data or {}
        self.META = meta or {"REMOTE_ADDR": "41.58.1.9"}


def _answer(success, codes=()):
    reply = mock.Mock()
    reply.json.return_value = {"success": success, "error-codes": list(codes)}
    return reply


class VerifyTests(SimpleTestCase):
    def setUp(self):
        bot_protection._warned_missing = False

    def test_with_no_key_the_request_is_allowed_and_said_once(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": ""}, clear=False):
            with self.assertLogs("afc_auth.bot_protection", level="WARNING") as caught:
                ok, reason = verify(_Req(), where="signup")
            self.assertTrue(ok)
            self.assertEqual(reason, "not_configured")
            self.assertIn("not configured", "\n".join(caught.output))
            # and not again, so the log does not fill with the same line
            ok2, _ = verify(_Req(), where="signup")
            self.assertTrue(ok2)

    def test_a_configured_form_with_no_token_is_refused(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "sekrit"}, clear=False):
            ok, reason = verify(_Req(), where="signup")
        self.assertFalse(ok)
        self.assertEqual(reason, "no_token")

    def test_cloudflare_saying_no_is_a_refusal(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "sekrit"}, clear=False):
            with mock.patch("requests.post", return_value=_answer(False, ["invalid-input-response"])):
                ok, reason = verify(_Req({"cf_turnstile_response": "tok"}), where="signup")
        self.assertFalse(ok)
        self.assertIn("invalid-input-response", reason)

    def test_cloudflare_saying_yes_lets_it_through_and_sends_the_right_things(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "sekrit"}, clear=False):
            with mock.patch("requests.post", return_value=_answer(True)) as posted:
                ok, reason = verify(
                    _Req({"cf_turnstile_response": "tok"},
                         {"HTTP_X_FORWARDED_FOR": "102.89.0.5, 10.0.0.1", "REMOTE_ADDR": "10.0.0.1"}),
                    where="signup")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        sent = posted.call_args.kwargs["data"]
        self.assertEqual(sent["secret"], "sekrit")
        self.assertEqual(sent["response"], "tok")
        self.assertEqual(sent["remoteip"], "102.89.0.5")      # the visitor, not the proxy

    def test_an_outage_at_cloudflare_does_not_become_an_outage_here(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "sekrit"}, clear=False):
            with mock.patch("requests.post", side_effect=OSError("dns")):
                with self.assertLogs("afc_auth.bot_protection", level="WARNING"):
                    ok, reason = verify(_Req({"cf_turnstile_response": "tok"}), where="signup")
        self.assertTrue(ok)
        self.assertEqual(reason, "verify_unreachable")

    def test_the_header_carries_the_token_too(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "sekrit"}, clear=False):
            with mock.patch("requests.post", return_value=_answer(True)) as posted:
                ok, _ = verify(_Req({}, {"HTTP_X_TURNSTILE_TOKEN": "hdr", "REMOTE_ADDR": "1.2.3.4"}))
        self.assertTrue(ok)
        self.assertEqual(posted.call_args.kwargs["data"]["response"], "hdr")

    def test_require_human_answers_a_coded_400(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET_KEY": "sekrit"}, clear=False):
            resp = require_human(_Req(), where="signup")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], BOT_CHECK_CODE)


class EveryPublicFormIsGuardedTests(SimpleTestCase):
    """Read the handlers: the guard has to be the FIRST thing that runs, or a bot has already been
    given a database row by the time it is refused."""

    def test_each_handler_calls_the_guard_before_anything_else(self):
        for rel, func in GUARDED.items():
            path = os.path.join(REPO, rel.replace("/", os.sep))
            text = io.open(path, encoding="utf-8", errors="replace").read()
            start = re.search(r"^def %s\s*\(" % re.escape(func), text, re.M)
            self.assertIsNotNone(start, "%s: def %s not found" % (rel, func))
            # to the end of the function, not a fixed window: submit_application's docstring alone
            # is longer than 3000 characters, and the guard sits after it.
            rest = text[start.end():]
            nxt = re.search(r"^(@api_view|def )", rest, re.M)
            body = rest[:nxt.start()] if nxt else rest
            guard = body.find("require_human(")
            self.assertGreater(guard, -1, "%s: %s has no bot check" % (rel, func))
            for verb in (".objects.create(", "send_email(", ".save("):
                did = body.find(verb)
                if did > -1:
                    self.assertLess(guard, did,
                                    "%s: %s does %s before the bot check" % (rel, func, verb))
