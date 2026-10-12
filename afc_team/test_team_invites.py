"""Team invites that reach the invitee (inbox #240, #241, 2026-10-12).

Found while recording the how-to clip "create a team + invite" (AFC_Frontend howto/clips/
team-invite.mjs), both measured on production the same day:

  #240  The Create team form posts its invite list as ONE JSON string,
        '[{"player": "AmaPlays"}]', and create_team read that whole string as one username: it
        matched nobody and the invite was dropped without a word. 933 teams, 0 invites ever sent
        from the form.
  #241  A player invited by name got a bell message with no link, and no page lists pending team
        invites, so there was nothing to press. Only an invite LINK shared outside AFC worked.

Now create_team reads both shapes (views._invite_identifiers) and every invite, from the form or
from invite_member, gets a bell notification whose "View" opens /invite/<id> (views._notify_team_invite,
target_type "custom", built by afc_auth.notification_links.build_notification_link).
"""
import json

from django.test import Client, TestCase

from afc_auth.models import Notifications, SessionToken, User
from afc_auth.notification_links import build_notification_link
from afc_team.models import Invite, Team, TeamMembers
from afc_team.views import _invite_identifiers

CREATE = "/team/create-team/"
INVITE = "/team/invite-member/"


def _user(username):
    u = User.objects.create(username=username, email=f"{username}@x.com", full_name=username,
                            role="player", password="x")
    SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u


def _auth(user):
    return {"HTTP_AUTHORIZATION": f"Bearer tok_{user.username}"}


class InviteIdentifiersTests(TestCase):
    def test_the_form_shape_json_rows_with_blanks(self):
        raw = json.dumps([{"player": "AmaPlays"}, {"player": ""}, {"player": " YawPlays "}])
        self.assertEqual(_invite_identifiers([raw]), ["AmaPlays", "YawPlays"])

    def test_plain_repeated_identifiers(self):
        self.assertEqual(_invite_identifiers(["AmaPlays", "", "yaw@x.com"]), ["AmaPlays", "yaw@x.com"])

    def test_bad_json_is_dropped_never_raised(self):
        self.assertEqual(_invite_identifiers(["[not json"]), [])

    def test_each_person_once(self):
        raw = json.dumps([{"player": "AmaPlays"}, {"player": "AmaPlays"}])
        self.assertEqual(_invite_identifiers([raw]), ["AmaPlays"])


class CreateTeamInvitesTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.kofi = _user("KofiPlays")
        self.ama = _user("AmaPlays")

    def _create(self, invites):
        body = {"team_name": "Accra Hawks", "team_tag": "ACH", "team_description": "Squad from Accra",
                "join_settings": "open", "list_of_players_to_invite": invites}
        return self.client.post(CREATE, data=body, **_auth(self.kofi))

    def test_the_form_list_sends_the_invite_and_tells_the_invitee(self):
        resp = self._create(json.dumps([{"player": "AmaPlays"}, {"player": ""}]))
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertEqual(resp.json()["invited_players"], ["AmaPlays"])
        invite = Invite.objects.get(invitee=self.ama)
        self.assertEqual(invite.team.team_name, "Accra Hawks")
        note = Notifications.objects.get(user=self.ama, related_invite=invite)
        self.assertEqual(build_notification_link(note.target_type, note.target_id), f"/invite/{invite.invite_id}")

    def test_a_player_already_in_a_team_is_not_invited(self):
        other = Team.objects.create(team_name="Lagos Lions", team_tag="LGL", join_settings="open",
                                    team_creator=self.ama, team_owner=self.ama, country="NG")
        TeamMembers.objects.create(team=other, member=self.ama, management_role="team_captain")
        resp = self._create(json.dumps([{"player": "AmaPlays"}]))
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertEqual(resp.json()["invited_players"], [])
        self.assertFalse(Invite.objects.filter(invitee=self.ama).exists())


class InviteMemberNotificationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.kofi = _user("KofiPlays")
        self.ama = _user("AmaPlays")
        self.team = Team.objects.create(team_name="Accra Hawks", team_tag="ACH", join_settings="open",
                                        team_creator=self.kofi, team_owner=self.kofi, country="GH")
        TeamMembers.objects.create(team=self.team, member=self.kofi, management_role="team_captain")

    def test_the_bell_message_opens_the_invite(self):
        resp = self.client.post(INVITE, data={"invitee_email_or_ign": "AmaPlays", "team_id": self.team.team_id,
                                              "role": "member"}, **_auth(self.kofi))
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        invite = Invite.objects.get(invitee=self.ama, team=self.team)
        note = Notifications.objects.get(user=self.ama, related_invite=invite)
        self.assertEqual(note.notification_type, "team_invitation")
        self.assertEqual(build_notification_link(note.target_type, note.target_id), f"/invite/{invite.invite_id}")
