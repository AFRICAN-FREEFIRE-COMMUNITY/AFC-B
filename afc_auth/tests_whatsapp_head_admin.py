r"""Only a head admin may spend money on a WhatsApp broadcast.

WHY (owner 2026-08-05): "broadcasts for whatsapp that is not room id and pass should be available
only to head admins then."

This is a SPENDING control rather than an ordinary permission, which is why it is stricter than
the gate on the endpoint itself. A general broadcast goes out on the `broadcast` template, which
Meta categorises as MARKETING: on the rate card effective 2026-04-01, Nigeria - about 69% of AFC -
is $0.0516 per marketing message against $0.0067 for utility. One broadcast to the 500-recipient
cap is therefore around $26, and 1,000 messages a day is roughly $1,548 a month.

Every other admin keeps in-app and email, which cost nothing. Room details are deliberately
untouched: they go out on `room_details`, a UTILITY template at an eighth of the price, from the
event surfaces, and an organizer has to be able to send them without asking anybody.

Run: .venv\Scripts\python.exe manage.py test afc_auth.tests_whatsapp_head_admin
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import Roles, SessionToken, User, UserRoles

SEND = "/auth/admin/broadcast-audience/send/"
PREVIEW = "/auth/admin/broadcast-audience/preview/"


class WhatsappBroadcastIsHeadAdminOnlyTests(TestCase):
    def setUp(self):
        self.client = Client()
        # A plain role="admin" WITHOUT the head_admin row: allowed on this surface, and the exact
        # account this restriction is about.
        self.admin = self._staff("wa_plain_admin", role_names=[])
        self.head = self._staff("wa_head_admin", role_names=["head_admin"])
        # Somebody to receive, so the audience is not empty.
        User.objects.create(
            username="wa_target", email="wa_target@x.com", full_name="Target",
            role="player", password="x", country="Nigeria")

    def _staff(self, username, role_names):
        user = User.objects.create(
            username=username, email=f"{username}@x.com", full_name=username,
            role="admin", password="x")
        for name in role_names:
            role, _ = Roles.objects.get_or_create(role_name=name)
            UserRoles.objects.create(user=user, role=role)
        SessionToken.objects.create(
            user=user, token=f"tok-{username}",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        return user

    def _send(self, username, delivery):
        return self.client.post(
            SEND,
            data={"everyone": True, "message": "Hello AFC.", "delivery": delivery,
                  "confirmed_count": User.objects.filter(is_active=True).count()},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer tok-{username}")

    # ── the restriction ──
    def test_a_plain_admin_cannot_send_on_whatsapp(self):
        resp = self._send("wa_plain_admin", "whatsapp")

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(resp.json().get("code"), "whatsapp_requires_head_admin")

    def test_a_plain_admin_cannot_smuggle_whatsapp_in_a_combination(self):
        """"both,whatsapp" is three channels. Gating only the bare "whatsapp" value would leave
        the expensive one reachable by writing it alongside two free ones."""
        resp = self._send("wa_plain_admin", "both,whatsapp")

        self.assertEqual(resp.status_code, 403, resp.content)

    def test_a_head_admin_may_send_on_whatsapp(self):
        resp = self._send("wa_head_admin", "whatsapp")

        self.assertNotEqual(resp.status_code, 403, resp.content)

    # ── everything else stays open ──
    def test_a_plain_admin_can_still_send_in_app(self):
        """The restriction is about COST. Channels that cost nothing must not become harder to
        use, or this reads as a demotion rather than a spending control."""
        resp = self._send("wa_plain_admin", "push")

        self.assertNotEqual(resp.status_code, 403, resp.content)

    def test_a_plain_admin_can_still_send_email(self):
        resp = self._send("wa_plain_admin", "email")

        self.assertNotEqual(resp.status_code, 403, resp.content)

    # ── the composer is told up front ──
    def test_the_preview_tells_a_plain_admin_whatsapp_is_not_theirs(self):
        """So the option can be greyed out with a reason. Finding out at the send, after writing
        the message and picking the audience, is the version of this that annoys people."""
        resp = self.client.post(
            PREVIEW, data={"everyone": True}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-wa_plain_admin")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["whatsapp_allowed"], False)

    def test_the_preview_tells_a_head_admin_it_is(self):
        resp = self.client.post(
            PREVIEW, data={"everyone": True}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-wa_head_admin")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["whatsapp_allowed"], True)
