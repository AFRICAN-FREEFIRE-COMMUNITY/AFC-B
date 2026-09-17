"""
afc_auth/tests_outbox.py - nothing leaves the process under the test runner; everything does when live.

WHY (owner 2026-09-17, a screenshot of seven Outlook bounces): the suite run on the VPS rig mailed
a fixture address for real, because send_email uses smtplib rather than Django's mail backend.
afc_auth/outbox.py and the OUTBOUND_DELIVERY switch are the fix; these tests prove the switch both
ways (R28: a catcher is proven on the offender AND on the innocent), for each of the three
chokepoints, at the transport boundary: smtplib.SMTP and requests.post are mocked, and the
assertion is whether they were touched at all.
"""
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from afc_auth import outbox
from afc_auth.views import send_email
from afc_support.notify import send_discord_dm
from afc_whatsapp import client as wa


class OutboxUnderTheRunner(SimpleTestCase):
    """The runner forces OUTBOUND_DELIVERY to outbox by argv (afc/settings.py), so this is the
    state every other test in the suite runs in."""

    def setUp(self):
        outbox.drain()

    def test_the_runner_is_in_outbox_mode(self):
        self.assertFalse(outbox.is_live())

    def test_send_email_never_opens_smtp_and_records_the_message(self):
        with patch("afc_auth.views.smtplib.SMTP") as smtp:
            ok = send_email("old@gmail.com", "Your AFC sign-in code", "<p>123456</p>")
        self.assertTrue(ok)
        smtp.assert_not_called()
        sent = outbox.drain()
        self.assertEqual([(m["channel"], m["to"], m["subject"]) for m in sent],
                         [("email", "old@gmail.com", "Your AFC sign-in code")])
        self.assertIn("123456", sent[0]["body"])

    @override_settings(WHATSAPP_PHONE_NUMBER_ID="1", WHATSAPP_ACCESS_TOKEN="t")
    def test_whatsapp_never_posts_to_meta_and_answers_the_success_shape(self):
        with patch("afc_whatsapp.client.requests.post") as post:
            result = wa.send_text("+2348012345678", "hello")
        post.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertTrue(result["wamid"])
        self.assertEqual([m["channel"] for m in outbox.drain()], ["whatsapp"])

    @override_settings(DISCORD_BOT_TOKEN="bot-token")
    def test_discord_dm_never_posts_and_reports_sent(self):
        with patch("afc_support.notify.requests.post") as post:
            ok = send_discord_dm("123456789012345678", "Match in one hour")
        post.assert_not_called()
        self.assertTrue(ok)
        sent = outbox.drain()
        self.assertEqual((sent[0]["channel"], sent[0]["to"], sent[0]["body"]),
                         ("discord_dm", "123456789012345678", "Match in one hour"))


@override_settings(OUTBOUND_DELIVERY="live")
class OutboxWhenLive(SimpleTestCase):
    """Production's state: the same three calls reach their transports."""

    def setUp(self):
        outbox.drain()

    def test_live_is_live(self):
        self.assertTrue(outbox.is_live())

    def test_send_email_opens_smtp(self):
        with patch("afc_auth.views.smtplib.SMTP") as smtp:
            server = Mock()
            smtp.return_value = server
            # a gmail address: send_email refuses providers it does not know before it sends
            ok = send_email("someone@gmail.com", "Subject", "<p>body</p>")
        self.assertTrue(ok)
        smtp.assert_called_once()
        server.sendmail.assert_called_once()
        self.assertEqual(outbox.drain(), [])

    @override_settings(WHATSAPP_PHONE_NUMBER_ID="1", WHATSAPP_ACCESS_TOKEN="t")
    def test_whatsapp_posts_to_meta(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"messages": [{"id": "wamid.1"}]}
        with patch("afc_whatsapp.client.requests.post", return_value=response) as post:
            result = wa.send_text("+2348012345678", "hello")
        post.assert_called_once()
        self.assertEqual(result["wamid"], "wamid.1")
        self.assertEqual(outbox.drain(), [])

    @override_settings(DISCORD_BOT_TOKEN="bot-token")
    def test_discord_dm_posts(self):
        channel = Mock(status_code=200)
        channel.json.return_value = {"id": "c1"}
        message = Mock(status_code=200)
        with patch("afc_support.notify.requests.post", side_effect=[channel, message]) as post:
            ok = send_discord_dm("123456789012345678", "Match in one hour")
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(outbox.drain(), [])
