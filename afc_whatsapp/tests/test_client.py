"""The Meta Cloud API client's request shape.

Asserted at the PAYLOAD rather than over the wire: what matters here is that the JSON handed to
Meta is the JSON Meta documents, and a test that mocked the HTTP layer any lower would be testing
`requests` instead. _post is the single boundary every send goes through, so patching it is the
narrowest honest seam.
"""
from unittest.mock import patch

from django.test import TestCase


class DynamicUrlButtonTests(TestCase):
    """The "Visit website" button that points at a particular event.

    A template approved with a dynamic URL button stores a FIXED base URL ending in {{1}}, and the
    send supplies only the tail. Meta permits the variable at the end of the URL and nowhere else,
    which is the whole safety property: an approved template cannot later be repointed at another
    domain by whoever calls it.
    """

    def _sent_payload(self, **kwargs):
        from afc_whatsapp import client

        with patch.object(client, "_post") as post:
            post.return_value = {"ok": True}
            client.send_template("+2348051234567", "afc_room_details", "en_US", **kwargs)
        return post.call_args.args[0]

    def test_the_suffix_travels_as_a_url_button_component(self):
        payload = self._sent_payload(
            body_params=["p", "e", "r", "pw", "map"],
            url_button_suffix="legacy-scrims-day-13",
        )
        buttons = [c for c in payload["template"]["components"] if c["type"] == "button"]

        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["sub_type"], "url")
        self.assertEqual(buttons[0]["index"], 0)
        self.assertEqual(
            buttons[0]["parameters"], [{"type": "text", "text": "legacy-scrims-day-13"}])

    def test_no_button_component_when_there_is_no_suffix(self):
        """Templates without a URL button are the norm. Sending an empty button component for
        them would be a malformed request rather than a harmless extra."""
        payload = self._sent_payload(body_params=["p", "e", "r", "pw", "map"])

        self.assertEqual(
            [c for c in payload["template"]["components"] if c["type"] == "button"], [])

    def test_a_url_button_and_quick_replies_can_coexist(self):
        """Different sub_types, and both are indexed from 0 within their own kind, which is what
        Meta expects. Asserted because getting it wrong produces a send that Meta accepts and then
        renders with the wrong button doing the wrong thing."""
        payload = self._sent_payload(
            body_params=["p"], button_payloads=["yes", "no"],
            url_button_suffix="some-event",
        )
        buttons = [c for c in payload["template"]["components"] if c["type"] == "button"]

        self.assertEqual([b["sub_type"] for b in buttons], ["quick_reply", "quick_reply", "url"])
        self.assertEqual([b["index"] for b in buttons], [0, 1, 0])
