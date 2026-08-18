"""
afc_bot.tests - the gate, and the behaviour when the bot is not there.

WHAT IS WORTH PINNING HERE

    THE GATE. These endpoints decide where room IDs, ban notices and every automated announcement
    are delivered. Somebody who can reach them can silently redirect all of it, so "head admins
    only" is the security boundary of this feature and is tested from every angle: signed out, a
    plain player, a plain admin with no head_admin role, and an organizer_admin (who passes the
    platform-admin check used elsewhere and must still be refused here).

    THE BOT BEING DOWN. This page is the thing an admin opens BECAUSE the bot looks wrong, so an
    unreachable bot has to produce a readable 503 rather than a 500 or a hang. That is a normal
    state for this screen, not an error.

    THE TOKEN NEVER LEAVING. The whole reason these views exist rather than letting the browser
    call the bot is that the control token stays server-side. A test asserts it is never in a
    response body.

Requests to the bot are mocked at the `requests` boundary; nothing here needs a live bot.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserRoles

BOT_SETTINGS = {"BOT_CONTROL_URL": "http://127.0.0.1:8099", "BOT_CONTROL_TOKEN": "secret-token"}


def make_user(username, role="player", granular=None):
    user = User.objects.create(
        username=username, email=f"{username}@example.com", password="x",
        full_name=username.title(), role=role,
    )
    if granular:
        role_row, _ = Roles.objects.get_or_create(role_name=granular)
        UserRoles.objects.create(user=user, role=role_row)
    return user


def token_for(user):
    session, _ = SessionToken.objects.get_or_create(
        token=f"tok-{user.pk}",
        defaults={"user": user, "expires_at": timezone.now() + timedelta(hours=3)},
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}


class _FakeResponse:
    """Just enough of requests.Response for the proxy."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise ValueError("not json")
        return self._payload


@override_settings(**BOT_SETTINGS)
class BotGateTests(TestCase):
    """Head admins only. Everybody else, including other kinds of admin."""

    def setUp(self):
        self.head = make_user("bot_head", role="admin", granular="head_admin")
        self.plain_admin = make_user("bot_admin", role="admin")
        self.org_admin = make_user("bot_orgadmin", role="admin", granular="organizer_admin")
        self.player = make_user("bot_player")

    def test_signed_out_is_refused(self):
        self.assertEqual(self.client.get("/bot/status/").status_code, 401)

    def test_a_player_is_refused(self):
        res = self.client.get("/bot/status/", **token_for(self.player))
        self.assertEqual(res.status_code, 403)
        self.assertIn("head admins", res.json()["message"])

    def test_an_admin_without_the_head_admin_role_is_refused(self):
        """role="admin" alone is not enough. The granular role is the actual gate."""
        self.assertEqual(
            self.client.get("/bot/status/", **token_for(self.plain_admin)).status_code, 403)

    def test_an_organizer_admin_is_refused(self):
        """organizer_admin passes is_platform_org_admin, which most admin surfaces accept. It is
        deliberately NOT enough here: overseeing organizations has nothing to do with the bot."""
        self.assertEqual(
            self.client.get("/bot/status/", **token_for(self.org_admin)).status_code, 403)

    def test_a_head_admin_gets_through(self):
        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse({"online": True})) as call:
            res = self.client.get("/bot/status/", **token_for(self.head))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["online"])
        self.assertTrue(call.called)

    def test_every_endpoint_is_behind_the_same_gate(self):
        """A new endpoint that forgets the gate is the way this leaks, so all of them are checked."""
        for method, path in (("get", "/bot/status/"), ("get", "/bot/config/"),
                             ("post", "/bot/config/"), ("get", "/bot/knowledge/"),
                             ("post", "/bot/rescrape/"), ("get", "/bot/approvals/"),
                             ("post", "/bot/approvals/")):
            with self.subTest(path=path, method=method):
                res = getattr(self.client, method)(path, **token_for(self.player))
                self.assertEqual(res.status_code, 403)


@override_settings(**BOT_SETTINGS)
class BotProxyTests(TestCase):
    def setUp(self):
        self.head = make_user("proxy_head", role="admin", granular="head_admin")
        self.auth = token_for(self.head)

    def test_an_unreachable_bot_is_a_readable_503(self):
        """The state this page exists to diagnose. Never a 500, never a hang."""
        import requests as rq
        with patch("afc_bot.views.requests.request",
                   side_effect=rq.ConnectionError("connection refused")):
            res = self.client.get("/bot/status/", **self.auth)
        self.assertEqual(res.status_code, 503)
        self.assertIn("Could not reach the bot", res.json()["message"])

    def test_the_bot_s_own_refusal_reaches_the_admin_verbatim(self):
        """The bot says "NEWS_POLL_INTERVAL_SECS must be between 30 and 86400". That sentence is
        more useful than anything this layer could invent, so it is passed through, not replaced."""
        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse({"message": "NEWS_POLL_INTERVAL_SECS must be "
                                                          "between 30 and 86400."}, 400)):
            res = self.client.post("/bot/config/", data={"values": {"NEWS_POLL_INTERVAL_SECS": 1}},
                                   content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 400)
        self.assertIn("between 30 and 86400", res.json()["message"])

    def test_the_control_token_never_appears_in_a_response(self):
        """The entire reason this proxy exists. If the token can reach a browser, the design has
        failed regardless of what else works."""
        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse({"online": True, "loops": {}})):
            res = self.client.get("/bot/status/", **self.auth)
        self.assertNotIn("secret-token", res.content.decode())

    def test_the_token_is_sent_to_the_bot_as_a_bearer_header(self):
        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse({"online": True})) as call:
            self.client.get("/bot/status/", **self.auth)
        self.assertEqual(call.call_args.kwargs["headers"]["Authorization"], "Bearer secret-token")

    def test_a_non_json_answer_from_the_bot_does_not_crash_the_page(self):
        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse(ValueError(), 502)):
            res = self.client.get("/bot/status/", **self.auth)
        self.assertEqual(res.status_code, 502)
        self.assertIn("message", res.json())

    @override_settings(BOT_CONTROL_URL="", BOT_CONTROL_TOKEN="")
    def test_an_unconfigured_server_says_so_rather_than_failing_oddly(self):
        res = self.client.get("/bot/status/", **self.auth)
        self.assertEqual(res.status_code, 503)
        self.assertIn("not configured", res.json()["message"])

    def test_uploading_a_knowledge_doc_forwards_the_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse({"message": "Added rules.txt."})) as call:
            res = self.client.post(
                "/bot/knowledge/",
                data={"file": SimpleUploadedFile("rules.txt", b"the rules"), "scope": "staff"},
                **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertIn("scope=staff", call.call_args.args[1])
        self.assertEqual(call.call_args.kwargs["files"]["file"][0], "rules.txt")

    def test_uploading_nothing_is_refused_before_the_bot_is_called(self):
        with patch("afc_bot.views.requests.request") as call:
            res = self.client.post("/bot/knowledge/", data={"scope": "public"}, **self.auth)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(call.called, "an empty upload must not reach the bot at all")

    def test_approving_an_announcement_forwards_the_decision(self):
        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse({"message": "Approved."})) as call:
            res = self.client.post("/bot/approvals/",
                                   data={"message_id": "123", "action": "approve"},
                                   content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(call.call_args.kwargs["json"]["action"], "approve")

    def test_resetting_a_setting_passes_the_name_through(self):
        with patch("afc_bot.views.requests.request",
                   return_value=_FakeResponse({"message": "reset"})) as call:
            self.client.delete("/bot/config/?name=NEWS_POLL_INTERVAL_SECS", **self.auth)
        self.assertIn("name=NEWS_POLL_INTERVAL_SECS", call.call_args.args[1])
