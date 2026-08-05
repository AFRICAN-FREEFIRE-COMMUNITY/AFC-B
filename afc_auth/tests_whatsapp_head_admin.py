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

from django.test import Client, TestCase, override_settings

from afc_auth.models import Roles, SessionToken, User, UserRoles

SEND = "/auth/admin/broadcast-audience/send/"
PREVIEW = "/auth/admin/broadcast-audience/preview/"


# The permission tests below assume the channel is SWITCHED ON. WHATSAPP_BROADCAST_TEMPLATE is
# empty by default (so a deploy can never message players before somebody chooses to), and the
# "not configured" refusal runs BEFORE the head-admin one - so without this override every test
# here would pass for the wrong reason, having been refused for being switched off rather than
# for who was asking. Caught by these tests failing the moment that check was added.
@override_settings(WHATSAPP_BROADCAST_TEMPLATE="broadcast")
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


@override_settings(WHATSAPP_BROADCAST_TEMPLATE="")
class WhatsappNotConfiguredTests(TestCase):
    """The channel is off until the server sets WHATSAPP_BROADCAST_TEMPLATE.

    Owner 2026-08-05: "add a disclaimer that the whatsapp is not available yet, but will be in due
    time soon". The disclaimer is DERIVED from this setting rather than hardcoded, so it clears
    itself the moment the env value lands and nobody has to remember a follow-up change.

    Refusing at the endpoint matters as much as the notice: send_broadcast_whatsapp skips every
    recipient and returns quietly when the template is missing, which is right mid-send and a
    terrible answer to somebody who deliberately ticked the box.
    """

    def setUp(self):
        self.client = Client()
        role, _ = Roles.objects.get_or_create(role_name="head_admin")
        self.head = User.objects.create(
            username="wa_off_head", email="wa_off_head@x.com", full_name="Head",
            role="admin", password="x")
        UserRoles.objects.create(user=self.head, role=role)
        SessionToken.objects.create(
            user=self.head, token="tok-wa_off_head",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        User.objects.create(
            username="wa_off_target", email="wa_off_target@x.com", full_name="T",
            role="player", password="x")

    def test_even_a_head_admin_is_refused_while_it_is_switched_off(self):
        resp = self.client.post(
            SEND,
            data={"everyone": True, "message": "hi", "delivery": "whatsapp",
                  "confirmed_count": User.objects.filter(is_active=True).count()},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-wa_off_head")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json().get("code"), "whatsapp_not_configured")

    def test_the_preview_reports_it_as_not_configured(self):
        resp = self.client.post(
            PREVIEW, data={"everyone": True}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-wa_off_head")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["whatsapp_configured"], False)

    def test_the_free_channels_are_unaffected(self):
        """Being switched off must not stop anybody sending in-app or email."""
        resp = self.client.post(
            SEND,
            data={"everyone": True, "message": "hi", "delivery": "push",
                  "confirmed_count": User.objects.filter(is_active=True).count()},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-wa_off_head")

        self.assertNotIn(resp.status_code, (400, 403), resp.content)
