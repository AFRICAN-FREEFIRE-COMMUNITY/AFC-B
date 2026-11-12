"""Broadcast emails are paced and a refused one is tried again (12 Nov 2026, AFC inbox #557).

The LCQ room details at 17:43 UTC lost 31 of 55 emails to Microsoft 365 resetting the connection; nothing
waited and nothing retried. These tests drive afc_auth.views.send_broadcast_emails with a fake mail server
and a fake clock, so they need no database and no SMTP. SimpleTestCase: runs under the Django test runner,
and also with plain unittest after django.setup().
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from afc_auth import views as V


def _targets(n):
    return [(f"p{i}@example.test", "en", SimpleNamespace(username=f"p{i}", language="en")) for i in range(n)]


class BroadcastEmailPacingTests(SimpleTestCase):
    def test_paced_one_message_at_a_time(self):
        slept, sent_to = [], []
        out = V.send_broadcast_emails(_targets(4), "Room", "Room", "Room ID: 1",
                                      sleep=slept.append, send=lambda a, *x, **k: sent_to.append(a) or True)
        self.assertEqual(out, (4, 0))
        self.assertEqual(sent_to, [f"p{i}@example.test" for i in range(4)])
        # a pause between messages (none before the first), each at least the pace
        self.assertEqual(slept, [V.BROADCAST_EMAIL_PACE_SECONDS] * 3)

    def test_refused_address_is_tried_again_after_the_waits(self):
        tries, slept = {}, []

        def flaky(addr, *a, **k):
            tries[addr] = tries.get(addr, 0) + 1
            return not (addr == "p1@example.test" and tries[addr] < 3)    # p1 refused twice, then accepted

        out = V.send_broadcast_emails(_targets(3), "Room", "Room", "Room ID: 1", sleep=slept.append, send=flaky)
        self.assertEqual(out, (3, 0))
        self.assertEqual(tries["p1@example.test"], 3)
        for w in V.BROADCAST_EMAIL_RETRY_WAITS:
            self.assertIn(w, slept)

    def test_an_address_that_never_goes_is_counted_and_the_rest_still_go(self):
        def dead_for_p0(addr, *a, **k):
            if addr == "p0@example.test":
                raise OSError("Connection reset by peer")
            return True

        out = V.send_broadcast_emails(_targets(3), "Room", "Room", "Room ID: 1", sleep=lambda s: None, send=dead_for_p0)
        self.assertEqual(out, (2, 1))
