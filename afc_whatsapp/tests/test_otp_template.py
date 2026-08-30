"""The AUTHENTICATION template: the OTP button Meta requires, and the code that must not leak.

WHY THIS FILE EXISTS (2026-08-30)
    AFC's account-recovery code template was submitted to Meta as UTILITY, worded about
    account access rather than as a bare "here is your code", in the hope of staying out of
    Meta's authentication rules. Meta refused it INSTANTLY:

        afc_account_recovery_code  en  REJECTED   rejected_reason: INCORRECT_CATEGORY

    A one-time code is authentication content and Meta will not take it as anything else. An
    authentication template cannot be SENT without a button component carrying the code, and
    client.send_template built no such component. That gap was documented in the code as a
    known, un-guessed risk for three weeks; this is it arriving.

    So WhatsApp account recovery has never delivered a code in production, and could not have.

WHAT IS PINNED HERE
    1. The wire payload gains the OTP button, in Meta's exact shape, and ONLY for an OTP send.
    2. The code is never carried through the broker twice. It travels once, in body_params,
       where redact_variables already keeps it out of the message log. A second copy would sit
       in the Celery payload with nothing redacting it.
    3. An OTP send with no code is refused LOCALLY rather than sent malformed, because Meta's
       answer to a missing OTP parameter is a 1320xx that names no cause.
    4. The template builder emits Meta's options-only AUTHENTICATION shape, with no body text.

Run: python manage.py test afc_whatsapp.tests.test_otp_template
"""
from unittest.mock import patch

from django.test import TestCase


def _payload(**kwargs):
    """The JSON handed to Meta for one send. _post is the single boundary every send goes
    through, so patching it is the narrowest honest seam (same as test_client.py)."""
    from afc_whatsapp import client

    with patch.object(client, "_post") as post:
        post.return_value = {"ok": True}
        client.send_template("+2348051234567", "afc_account_recovery_code", "en", **kwargs)
    return post.call_args.args[0]


def _buttons(payload):
    return [c for c in payload["template"]["components"] if c["type"] == "button"]


class OtpButtonPayloadTests(TestCase):
    def test_an_otp_send_carries_the_button_component_meta_requires(self):
        """The bug, as one assertion. Without this component Meta refuses the send."""
        payload = _payload(body_params=["481902"], otp_code="481902")
        buttons = _buttons(payload)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["sub_type"], "copy_code")
        self.assertEqual(buttons[0]["index"], 0)
        # "coupon_code" really is Meta's name for the parameter on a COPY_CODE button. It
        # reads like a mistake and is not one.
        self.assertEqual(
            buttons[0]["parameters"], [{"type": "coupon_code", "coupon_code": "481902"}]
        )

    def test_the_code_is_in_the_body_TOO_because_meta_wants_it_in_both(self):
        payload = _payload(body_params=["481902"], otp_code="481902")
        body = [c for c in payload["template"]["components"] if c["type"] == "body"][0]
        self.assertEqual(body["parameters"], [{"type": "text", "text": "481902"}])

    def test_otp_code_alone_fills_the_body_so_the_two_cannot_disagree(self):
        """Meta rejects the send when the body and the button carry different codes. Two
        arguments that must match is two chances to get it wrong, so one derives the other."""
        payload = _payload(otp_code="481902")
        body = [c for c in payload["template"]["components"] if c["type"] == "body"][0]
        self.assertEqual(body["parameters"], [{"type": "text", "text": "481902"}])
        self.assertEqual(_buttons(payload)[0]["parameters"][0]["coupon_code"], "481902")

    def test_an_ORDINARY_template_send_is_unchanged(self):
        """The regression that would matter most: six approved templates already send fine,
        and none of them may grow a button because this one needed one."""
        payload = _payload(body_params=["p", "e", "r", "pw", "map", "x"])
        self.assertEqual(_buttons(payload), [])


class OtpCodeDoesNotLeakTests(TestCase):
    """The code travels ONCE. Everything else about the send is still recorded."""

    def test_queue_template_puts_no_second_copy_of_the_code_on_the_broker(self):
        import afc_whatsapp.tasks as tasks

        with patch.object(tasks, "_dispatch") as dispatch:
            tasks.queue_template(
                "+2348051234567", "afc_account_recovery_code", "en",
                body_params=["481902"], context="account_recovery_code",
                redact_variables=True, otp_button=True,
            )
        kwargs = dispatch.call_args.args[0]
        # A FLAG, never the value: redact_variables covers body_params and nothing else, so a
        # second copy of a live one-time code would sit in the payload unprotected.
        self.assertIs(kwargs["otp_button"], True)
        self.assertNotIn("otp_code", kwargs)
        self.assertEqual(
            [k for k, v in kwargs.items() if v == "481902"], [],
            "the code appears somewhere other than body_params",
        )
        self.assertEqual(kwargs["body_params"], ["481902"])
        self.assertIs(kwargs["redact_variables"], True)

    def test_the_recovery_sender_asks_for_the_button(self):
        """afc_auth.two_factor.WhatsAppCodeMethod is the only caller that may set this."""
        from django.test import override_settings

        from afc_auth import two_factor

        with override_settings(WHATSAPP_LOGIN_CODE_TEMPLATE="afc_account_recovery_code",
                               WHATSAPP_LOGIN_CODE_LANG="en"):
            with patch("afc_whatsapp.tasks.queue_template") as queue:
                queue.return_value = 1
                method = two_factor.METHODS["whatsapp"]
                with patch.object(method, "_number", return_value="+2348051234567"):
                    ok = method.deliver(_FakeUser(), "481902")
        self.assertTrue(ok)
        self.assertIs(queue.call_args.kwargs["otp_button"], True)
        self.assertIs(queue.call_args.kwargs["redact_variables"], True)
        self.assertEqual(queue.call_args.kwargs["body_params"], ["481902"])


class _FakeUser:
    """Enough of a User for deliver(). A real row would drag in a profile and a migration for
    a test about which arguments are passed."""
    username = "recovering"
    language = "en"
    ip_country = "NG"
    country = "NG"
    pk = 1


class OtpSendWithNoCodeIsRefusedTests(TestCase):
    def test_an_otp_send_with_an_empty_body_never_reaches_meta(self):
        """Meta answers a missing OTP parameter with a 1320xx that names no cause, so this is
        caught here where the message can say what actually happened."""
        import afc_whatsapp.tasks as tasks
        from afc_whatsapp.models import WhatsAppMessage

        with patch("afc_whatsapp.client.send_template") as send:
            message_id = tasks.send_whatsapp_message(
                to="+2348051234567", template_name="afc_account_recovery_code",
                language="en", body_params=[], otp_button=True,
                context="account_recovery_code",
            )
        send.assert_not_called()
        row = WhatsAppMessage.objects.get(pk=message_id)
        self.assertEqual(row.status, "failed")
        self.assertIn("no code", row.error_title.lower())


class AuthenticationTemplateShapeTests(TestCase):
    """What gets SUBMITTED to Meta. An authentication template is built from options, never
    from text: Meta owns the copy and refuses a body of our own, which is what
    INCORRECT_CATEGORY was telling us."""

    def _spec(self):
        from afc_whatsapp.management.commands.create_whatsapp_templates import _templates

        for spec in _templates():
            if spec["setting"] == "WHATSAPP_LOGIN_CODE_TEMPLATE":
                return spec
        self.fail("the account recovery template spec is gone")

    def test_the_recovery_template_is_AUTHENTICATION_and_carries_no_body_text(self):
        spec = self._spec()
        self.assertEqual(spec["category"], "AUTHENTICATION")
        self.assertNotIn("body", spec, "Meta refuses custom copy on an authentication template")
        self.assertNotIn("example", spec)
        self.assertEqual(spec["auth"]["otp_type"], "COPY_CODE")

    def test_the_expiry_it_promises_matches_the_challenge_that_backs_it(self):
        """The footer tells the player how long the code lasts. If it ever disagrees with the
        real lifetime, the message is lying to them."""
        from afc_auth.models import TwoFactorChallenge

        minutes = self._spec()["auth"]["code_expiration_minutes"]
        self.assertEqual(
            minutes * 60, int(TwoFactorChallenge.CODE_LIFETIME.total_seconds())
        )

    def test_every_OTHER_template_still_carries_its_own_body(self):
        """The branch must not swallow the six templates that were already approved."""
        from afc_whatsapp.management.commands.create_whatsapp_templates import _templates

        others = [s for s in _templates() if "auth" not in s]
        self.assertGreaterEqual(len(others), 5)
        for spec in others:
            self.assertIn("body", spec, spec["setting"])
            self.assertIn("{{1}}", spec["body"], spec["setting"])
