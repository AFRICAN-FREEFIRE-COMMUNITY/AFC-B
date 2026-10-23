"""afc_auth/tests_client_ip.py - the visitor's address cannot be chosen by the visitor (inbox #141).

The rule (afc_auth/client_ip.py): nginx's X-Real-IP is the visitor; only a caller on a private or
loopback address (our own servers, such as the website's QR scan route on the Docker bridge) may name
a different visitor in X-Forwarded-For. The end-to-end test drives the feedback form's cooldown, which
a script used to walk past by sending a new X-Forwarded-For on every post.

Run: ..\\backend\\.venv\\Scripts\\python.exe manage.py test afc_auth.tests_client_ip --noinput
"""
from types import SimpleNamespace

from django.test import SimpleTestCase
from rest_framework import status

from afc_auth.client_ip import client_ip
from afc_auth.views import get_client_ip
from afc_feedback.tests import FeedbackTestBase


def req(**meta):
    return SimpleNamespace(META=meta)


class ClientIpRuleTests(SimpleTestCase):
    def test_a_public_caller_cannot_choose_its_address(self):
        # nginx says the connection came from 41.58.100.7; the visitor wrote its own X-Forwarded-For.
        r = req(HTTP_X_REAL_IP="41.58.100.7", HTTP_X_FORWARDED_FOR="1.2.3.4, 41.58.100.7", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(client_ip(r), "41.58.100.7")

    def test_without_nginx_the_connection_is_the_caller(self):
        self.assertEqual(client_ip(req(REMOTE_ADDR="102.89.0.5", HTTP_X_FORWARDED_FOR="9.9.9.9")), "102.89.0.5")

    def test_our_own_server_may_forward_the_visitor(self):
        # The website's QR route on the Docker bridge, forwarding the scanner it was told about by nginx.
        r = req(HTTP_X_REAL_IP="172.17.0.3", HTTP_X_FORWARDED_FOR="105.119.14.66, 172.17.0.3", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(client_ip(r), "105.119.14.66")

    def test_loopback_and_ipv6(self):
        self.assertEqual(client_ip(req(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="2c0f:f248::1")), "2c0f:f248::1")
        self.assertEqual(client_ip(req(HTTP_X_REAL_IP="2c0f:f248::7", HTTP_X_FORWARDED_FOR="1.1.1.1")), "2c0f:f248::7")

    def test_nonsense_from_our_server_falls_back_to_the_caller(self):
        self.assertEqual(client_ip(req(HTTP_X_REAL_IP="172.17.0.3", HTTP_X_FORWARDED_FOR="not-an-ip")), "172.17.0.3")
        self.assertEqual(client_ip(req(HTTP_X_REAL_IP="10.0.0.1")), "10.0.0.1")

    def test_nothing_known_is_empty_never_an_error(self):
        self.assertEqual(client_ip(req()), "")
        self.assertEqual(client_ip(SimpleNamespace()), "")

    def test_the_old_name_gives_the_same_answer(self):
        r = req(HTTP_X_REAL_IP="41.58.100.7", HTTP_X_FORWARDED_FOR="1.2.3.4")
        self.assertEqual(get_client_ip(r), "41.58.100.7")


class FeedbackLimitCannotBeDodgedTests(FeedbackTestBase):
    """Before the fix, a new X-Forwarded-For per post was a new person to the cooldown."""

    def post(self, forwarded):
        return self.client.post(self.submit_url, {"answers": {"comment": "hi"}}, format="json",
                                HTTP_X_REAL_IP="41.58.100.7", HTTP_X_FORWARDED_FOR=forwarded)

    def test_a_new_forwarded_address_is_still_the_same_visitor(self):
        first = self.post("1.1.1.1")
        second = self.post("2.2.2.2")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(second.data["reason"], "cooldown")

    def test_another_real_visitor_is_not_blocked(self):
        self.post("1.1.1.1")
        other = self.client.post(self.submit_url, {"answers": {"comment": "hi"}}, format="json",
                                 HTTP_X_REAL_IP="102.89.0.5")
        self.assertEqual(other.status_code, status.HTTP_201_CREATED)
