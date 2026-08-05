"""The 3D room switch: who can set it, and where the joining steps end up.

WHY THIS EXISTS (owner 2026-08-04). A 3D room is not joined the way an ordinary custom room is:
the squad has to be a complete group first, and the leader goes in through Customs and League
rather than typing a room id on the lobby screen. Players who did not know that simply failed to
join, so a per-map switch now decides whether the joining steps travel WITH the room id and
password.

WHAT IS COVERED HERE: the switch itself (default, saving, and the permission gate it inherits from
the room credentials beside it) and the append helper that puts the steps onto the notification and
email bodies. The event page renders its own translated copy from the frontend message files, which
is verified in the browser rather than here.

Run: .venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_room_3d
"""
import datetime
import json

from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from afc_auth.models import SessionToken, User
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageGroups,
    Stages,
)
from afc_tournament_and_scrims.room_join_help import ROOM_JOIN_3D_HELP, append_3d_help

EDIT_URL = "/events/edit-match-details/"


class Room3dSwitchTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = datetime.date.today()

        self.admin = User.objects.create(
            username="room3d_admin", email="room3d_admin@x.com", full_name="Room 3D Admin",
            role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="room3d-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        # A player with no rights over this event, to prove the switch is behind the same gate the
        # room credentials it sits beside are.
        self.outsider = User.objects.create(
            username="room3d_outsider", email="room3d_outsider@x.com", full_name="Outsider",
            role="player", password="x")
        SessionToken.objects.create(
            user=self.outsider, token="room3d-outsider-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Room 3D Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=2)
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, kill_point=1.0, leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda")
        self.other_match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=2, match_map="purgatory")

    def _post(self, body, token="room3d-admin-token"):
        return self.client.post(
            EDIT_URL, data=json.dumps(body), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}")

    # ── the switch ──
    def test_a_room_is_not_3d_until_somebody_says_so(self):
        """Default off. Every map that already exists predates this switch, and silently deciding
        they were all 3D rooms would put eight joining steps under every room id on the site."""
        self.assertFalse(self.match.room_is_3d)

    def test_an_organizer_can_turn_it_on_and_off_beside_the_room_details(self):
        resp = self._post({"match_id": self.match.match_id, "room_is_3d": True})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertTrue(self.match.room_is_3d)

        resp = self._post({"match_id": self.match.match_id, "room_is_3d": False})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertFalse(self.match.room_is_3d)

    def test_the_string_false_turns_it_off_rather_than_on(self):
        """A form-encoded caller sends the STRING "false", and bool("false") is True. Without the
        shared _as_bool this endpoint would turn the switch ON when asked to turn it off."""
        self.match.room_is_3d = True
        self.match.save(update_fields=["room_is_3d"])

        resp = self._post({"match_id": self.match.match_id, "room_is_3d": "false"})

        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertFalse(self.match.room_is_3d)

    def test_leaving_it_out_of_the_body_does_not_change_it(self):
        """Same contract the three room fields beside it already use: absent means "not being
        edited". The credentials and the switch auto-save together, and a caller that only sends
        one of them must not silently reset the rest."""
        self.match.room_is_3d = True
        self.match.save(update_fields=["room_is_3d"])

        resp = self._post({"match_id": self.match.match_id, "room_id": "ABC123"})

        self.assertEqual(resp.status_code, 200, resp.content)
        self.match.refresh_from_db()
        self.assertTrue(self.match.room_is_3d)
        self.assertEqual(self.match.room_id, "ABC123")

    def test_somebody_with_no_rights_over_the_event_cannot_set_it(self):
        """It rides on the same endpoint as the room credentials, so it inherits the same gate."""
        resp = self._post(
            {"match_id": self.match.match_id, "room_is_3d": True}, token="room3d-outsider-token")

        self.assertEqual(resp.status_code, 403, resp.content)
        self.match.refresh_from_db()
        self.assertFalse(self.match.room_is_3d)


class Append3dHelpTests(TestCase):
    """The steps reaching the notification and email bodies, which are plain strings."""

    def test_nothing_is_appended_when_no_map_is_a_3d_room(self):
        body = "Room ID: 123"
        self.assertEqual(append_3d_help(body, [_FakeMatch(False), _FakeMatch(False)]), body)

    def test_the_steps_are_appended_when_any_map_is_a_3d_room(self):
        """ANY, not all: a group broadcast covers every map at once, and a player who needs the
        steps for one of them needs them in that message."""
        out = append_3d_help("Room ID: 123", [_FakeMatch(False), _FakeMatch(True)])

        self.assertIn(ROOM_JOIN_3D_HELP, out)
        self.assertTrue(out.startswith("Room ID: 123"), "the room details must still come first")

    def test_the_steps_are_appended_only_once_for_a_whole_group(self):
        """Five maps must not produce five copies of the same eight steps, which would bury the
        room ids the message exists to deliver."""
        out = append_3d_help("Room ID: 123", [_FakeMatch(True) for _ in range(5)])

        self.assertEqual(out.count("How to join the 3D room"), 1)

    def test_an_empty_body_is_left_alone(self):
        """A message with no room details is not sent at all, so appending steps to it would
        create a message that is nothing but instructions."""
        for empty in ("", None):
            self.assertEqual(append_3d_help(empty, [_FakeMatch(True)]), empty)


class _FakeMatch:
    """A stand-in for a Match row: append_3d_help only ever reads room_is_3d, and building real
    matches here would test the ORM rather than the helper."""

    def __init__(self, room_is_3d):
        self.room_is_3d = room_is_3d


class Room3dWhatsAppFollowUpTests(TestCase):
    """The 3D joining steps as a SECOND WhatsApp template (owner 2026-08-05).

    A template's wording is frozen when Meta approves it, so the steps cannot be appended to the
    room-details message. They go as their own send, and the rules that matter are: only for a 3D
    room, only to a player who actually received the room details, and never at the cost of the
    room details themselves.
    """

    def setUp(self):
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="wa3d_admin", email="wa3d_admin@x.com", full_name="WA 3D Admin",
            role="admin", password="x")
        self.player = User.objects.create(
            username="wa3d_player", email="wa3d_player@x.com", full_name="WA 3D Player",
            role="player", password="x")

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="WA 3D Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, kill_point=1.0, leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda",
            room_id="RID", room_password="PW", room_is_3d=True)

        profile = self.player.userprofile_set.first() if hasattr(
            self.player, "userprofile_set") else None
        if profile is None:
            from afc_auth.models import UserProfile
            profile = UserProfile.objects.create(user=self.player)
        profile.whatsapp_number = "+2348051234567"
        profile.save()

    def _send(self, **settings_kwargs):
        from afc_tournament_and_scrims import whatsapp_room_details

        base = {"WHATSAPP_ROOM_TEMPLATE": "room_details",
                "WHATSAPP_ROOM_TEMPLATE_LANG": "en_US"}
        base.update(settings_kwargs)
        with override_settings(**base):
            with patch.object(whatsapp_room_details, "queue_template") as queue:
                queue.return_value = "wamid.test"
                whatsapp_room_details.send_room_details(
                    [self.player], self.event, self.match)
        return queue

    def test_the_steps_go_as_their_own_message_after_the_room_details(self):
        queue = self._send(WHATSAPP_ROOM_3D_TEMPLATE="afc_room_3d_help")

        self.assertEqual(queue.call_count, 2, "expected the room details AND the follow-up")
        first, second = queue.call_args_list
        self.assertEqual(first.args[1], "room_details")
        self.assertEqual(second.args[1], "afc_room_3d_help")
        self.assertEqual(
            second.kwargs["context"], "room_3d_help",
            "the log row has to say which of the two messages this was")

    def test_nothing_extra_is_sent_for_an_ordinary_room(self):
        self.match.room_is_3d = False
        self.match.save(update_fields=["room_is_3d"])

        queue = self._send(WHATSAPP_ROOM_3D_TEMPLATE="afc_room_3d_help")

        self.assertEqual(queue.call_count, 1)
        self.assertEqual(queue.call_args_list[0].args[1], "room_details")

    def test_an_unapproved_template_name_does_not_cost_anybody_their_room_password(self):
        """THE REASON THE SETTING DEFAULTS TO EMPTY. Meta fails a send to a template it has not
        approved, and until the owner has a name to put here, an eager follow-up would put that
        failure next to the one message a player cannot play without."""
        queue = self._send(WHATSAPP_ROOM_3D_TEMPLATE="")

        self.assertEqual(queue.call_count, 1)
        self.assertEqual(queue.call_args_list[0].args[1], "room_details")

    def test_a_player_who_did_not_get_the_room_details_gets_no_instructions(self):
        """An opted-out player returns None from queue_template. Sending them joining steps for a
        room whose id they never received would be noise about a room they cannot enter."""
        from afc_tournament_and_scrims import whatsapp_room_details

        with override_settings(WHATSAPP_ROOM_TEMPLATE="room_details",
                               WHATSAPP_ROOM_TEMPLATE_LANG="en_US",
                               WHATSAPP_ROOM_3D_TEMPLATE="afc_room_3d_help"):
            with patch.object(whatsapp_room_details, "queue_template") as queue:
                queue.return_value = None  # opted out
                queued, skipped = whatsapp_room_details.send_room_details(
                    [self.player], self.event, self.match)

        self.assertEqual(queue.call_count, 1)
        self.assertEqual((queued, skipped), (0, 1))


class RoomDetailsTemplateParamsTests(TestCase):
    """The six variables the room-details template is sent, and their order.

    The order is a CONTRACT with a template Meta approved: the template body says which variable
    goes where, and nothing at send time can detect a mismatch. Getting it wrong delivers a message
    that looks fine and tells the player the wrong room. So it is asserted.
    """

    def setUp(self):
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="params_admin", email="params_admin@x.com", full_name="Params Admin",
            role="admin", password="x")
        self.player = User.objects.create(
            username="params_player", email="params_player@x.com", full_name="Params Player",
            role="player", password="x")
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Params Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin,
            slug="params-cup")
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, kill_point=1.0, leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda",
            room_id="RID1", room_name="AFC LOBBY 1", room_password="284915")

        from afc_auth.models import UserProfile
        profile = UserProfile.objects.filter(user=self.player).first() or \
            UserProfile.objects.create(user=self.player)
        profile.whatsapp_number = "+2348051234567"
        profile.save()

    def _params(self):
        from afc_tournament_and_scrims import whatsapp_room_details

        with override_settings(WHATSAPP_ROOM_TEMPLATE="room_details",
                               WHATSAPP_ROOM_TEMPLATE_LANG="en_US",
                               WHATSAPP_ROOM_3D_TEMPLATE=""):
            with patch.object(whatsapp_room_details, "queue_template") as queue:
                queue.return_value = "wamid.x"
                whatsapp_room_details.send_room_details([self.player], self.event, self.match)
        return queue.call_args.kwargs["body_params"]

    def test_the_six_variables_are_sent_in_the_approved_order(self):
        self.assertEqual(
            self._params(),
            ["params_player", "Params Cup", "bermuda", "AFC LOBBY 1", "RID1", "284915"])

    def test_a_blank_room_name_does_not_cost_the_group_its_room_id(self):
        """Room name is optional on a Match and is routinely blank. Meta rejects a send whose
        parameter is an empty string, and it rejects the WHOLE message, so an unguarded blank here
        would stop the room ID reaching anybody."""
        self.match.room_name = ""
        self.match.save(update_fields=["room_name"])

        params = self._params()

        self.assertEqual(params[3], "-")
        self.assertEqual(params[4], "RID1", "the room id must still go out")

    def test_a_newline_in_a_room_name_is_flattened(self):
        """Meta refuses a parameter containing a newline, and an organizer pasting a room name out
        of the game client can easily bring one along."""
        self.match.room_name = "AFC\nLOBBY  1"
        self.match.save(update_fields=["room_name"])

        self.assertEqual(self._params()[3], "AFC LOBBY 1")
