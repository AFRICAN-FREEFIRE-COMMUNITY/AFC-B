# afc_tournament_and_scrims/test_private_event_invitation.py
# ──────────────────────────────────────────────────────────────────────────────
# AN INVITED TEAM CAN REGISTER FOR A PRIVATE EVENT WITHOUT AN INVITE LINK.
#
# THE BUG THIS EXISTS FOR, and it was mine. v7.1.80 taught the event page to offer registration to
# a team holding an addressed invitation (my_invitation made canRegister true). The private-event
# gate in register_for_event still demanded an EventInviteToken and nothing else, so the page
# offered the button and the endpoint refused it:
#
#     "This is a private event. You need an invite link to register."
#
# The button opened and the door did not. The owner hit it within a day of the ship.
#
# WHY AN INVITATION SATISFIES THE GATE. The gate exists so a private event admits only people who
# were let in. An EventInviteToken is an ANONYMOUS link and proves only that somebody was given a
# URL. An EventTeamInvitation is ADDRESSED: it names this exact team, written by an admin or the
# organizer. It is the stronger credential, so honouring it widens nothing.
#
# These tests are written from the PRODUCTION shape in one specific way that matters: the
# invitation here carries NO invite_token. Some rows do and some do not, and an earlier attempt at
# this fix leaned on the token being present, which would have left exactly those rows still
# broken. The fixture omits it deliberately.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import json
import uuid

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import Event, EventTeamInvitation

REGISTER_URL = "/events/register-for-event/"


class PrivateEventInvitationTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = timezone.localdate()
        self.event = Event.objects.create(
            event_name="Private Invite Cup", slug="private-invite-cup",
            competition_type="tournament", participant_type="squad", event_type="virtual",
            event_mode="br", max_teams_or_players=16, number_of_stages=1,
            # PRIVATE. That is the whole point of the fixture.
            is_public=False, is_draft=False,
            start_date=today + datetime.timedelta(days=7),
            end_date=today + datetime.timedelta(days=8),
            registration_open_date=today - datetime.timedelta(days=1),
            registration_end_date=today + datetime.timedelta(days=5),
        )
        self.owner = self._user("priv_owner")
        self.team = Team.objects.create(
            team_name="Priv Team", join_settings="open",
            team_creator=self.owner, team_owner=self.owner, country="Nigeria",
        )
        TeamMembers.objects.create(team=self.team, member=self.owner,
                                   management_role="team_captain")
        self.roster = [self.owner]
        for i in range(3):
            player = self._user(f"priv_p{i}")
            TeamMembers.objects.create(team=self.team, member=player, management_role="member")
            self.roster.append(player)

    def _user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria",
        )

    def _auth(self, user):
        token = SessionToken.objects.create(
            user=user, token=f"pv-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _register(self, user, **extra):
        body = {
            "event_id": self.event.event_id,
            "team_id": self.team.team_id,
            "roster_member_ids": [u.user_id for u in self.roster],
            **extra,
        }
        return self.client.post(REGISTER_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth(user))

    def _invite(self):
        # NO invite_token on purpose: this is the row shape the token-based fix would have missed.
        return EventTeamInvitation.objects.create(
            event=self.event, team=self.team, status="pending",
            message="We saved you a slot.",
        )

    def test_without_an_invitation_a_private_event_still_refuses(self):
        # The gate must still be a gate. If this ever goes green by accident, the fix below has
        # opened a private event to everybody.
        res = self._register(self.owner)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("invite_token", res.json()["message"])

    def test_an_invited_team_gets_past_the_private_gate_without_a_token(self):
        self._invite()
        res = self._register(self.owner)
        # The assertion is NOT "201". Registration runs a long tail of other gates (Discord,
        # roster rules, capacity) that this fixture does not satisfy, and pinning this test to a
        # full success would make it a test of all of them. What matters is that it is no longer
        # refused AT THE PRIVATE-EVENT DOOR.
        self.assertNotEqual(res.status_code, 400, res.content)
        self.assertNotIn("invite_token is required", res.content.decode())

    def test_an_invitation_to_a_DIFFERENT_team_does_not_open_the_door(self):
        other_owner = self._user("priv_other_owner")
        other = Team.objects.create(
            team_name="Other Team", join_settings="open",
            team_creator=other_owner, team_owner=other_owner, country="Nigeria")
        EventTeamInvitation.objects.create(event=self.event, team=other, status="pending")
        res = self._register(self.owner)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("invite_token", res.json()["message"])

    def test_an_ANSWERED_invitation_does_not_open_the_door(self):
        # Only a PENDING invitation is a live credential. A declined or cancelled one is a record
        # of a door that was closed.
        for status in ("declined", "cancelled", "expired"):
            EventTeamInvitation.objects.filter(event=self.event).delete()
            EventTeamInvitation.objects.create(
                event=self.event, team=self.team, status=status)
            res = self._register(self.owner)
            self.assertEqual(res.status_code, 400, f"{status}: {res.content}")

    def test_a_public_event_is_unaffected(self):
        # The gate only exists for private events; this proves the change did not leak into the
        # ordinary path.
        self.event.is_public = True
        self.event.save(update_fields=["is_public"])
        res = self._register(self.owner)
        self.assertNotIn("invite_token is required", res.content.decode())
