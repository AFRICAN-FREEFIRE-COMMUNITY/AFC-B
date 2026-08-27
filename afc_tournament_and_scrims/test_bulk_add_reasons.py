"""Adding a team BY HAND bypasses the event's requirements. Invitations do not.

THE RULE (owner, 2026-08-27): "when adding a team to an event, if an organizer or admin is adding
them, they automatically bypass those requirements. Invitations is different."

    DIRECT ADD (this endpoint)   requirements do not apply. The person clicking Add is the
                                 authority those requirements exist to serve.
    INVITATION                   unchanged. An invited team registers ITSELF through
                                 register_for_event, which still enforces everything, because
                                 there the team asserts it qualifies rather than an admin
                                 deciding it may play.
    BANS                         still refuse. A ban is not a requirement, it is an enforcement
                                 decision already taken.

WHAT IS STILL RECORDED
    The gate this replaces existed because a direct add used to skip bans, requirements and
    capacity with NO record. That concern survives: when a direct add steps over something that
    would have stopped a self-registration, a waiver row is written naming what and by whom. The
    admin is asked for nothing; the record still exists afterwards.

Run: AFC_TEST_DB_NAME=test_afc_bulkadd python manage.py test afc_tournament_and_scrims.test_bulk_add_reasons
"""
import json
from datetime import date, timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from afc_auth.models import BannedPlayer, SessionToken, User, UserProfile
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import (
    Event,
    EventRequirementWaiver,
    TournamentTeam,
)


def _user(username, role="player", uid=None):
    # uid=None, NOT "": User.uid carries a UNIQUE index, so two users with an empty-string uid
    # collide. MySQL allows many NULLs in a unique index, and the requirement check treats NULL and
    # blank alike, so NULL is the correct way to say "has no uid".
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria", uid=uid,
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _team(owner, name, members):
    t = Team.objects.create(
        team_name=name, team_owner=owner, team_creator=owner,
        country="Nigeria", join_settings="open",
    )
    for m in members:
        TeamMembers.objects.create(team=t, member=m, management_role="member")
    return t


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class DirectAddBypassesRequirementsTests(TestCase):
    def setUp(self):
        self.admin, self.token = _user("bulkadmin", role="admin")
        # Two players with NO uid, so require_player_uid would block a self-registration.
        self.p1, _ = _user("bulkp1")
        self.p2, _ = _user("bulkp2")
        self.team = _team(self.admin, "CATALYST TEST", [self.p1, self.p2])
        self.event = Event.objects.create(
            event_name="Bulk Add Cup", slug="bulk-add-cup",
            competition_type="tournament", participant_type="squad",
            event_type="online", event_mode="single",
            max_teams_or_players=16, number_of_stages=1,
            start_date=date.today() + timedelta(days=10),
            end_date=date.today() + timedelta(days=11),
            registration_open_date=date.today(),
            registration_end_date=date.today() + timedelta(days=5),
            creator=self.admin,
        )

    def _add(self, **extra):
        return Client().post(
            "/events/add-teams-to-event/",
            data=json.dumps({
                "event_id": self.event.event_id,
                "team_ids": [self.team.team_id],
                **extra,
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def _added(self):
        return TournamentTeam.objects.filter(event=self.event, team=self.team).exists()

    # ── the rule ──────────────────────────────────────────────────────────────────────────────
    def test_with_no_requirements_the_team_is_added(self):
        self.assertIn(self._add().status_code, (200, 201))
        self.assertTrue(self._added())

    def test_unmet_PLAYER_requirements_do_not_block_a_direct_add(self):
        """THE RULE. Both players have no UID and the event demands one; the admin adds anyway."""
        self.event.require_player_uid = True
        self.event.require_whatsapp = True
        self.event.save()
        resp = self._add()
        self.assertIn(resp.status_code, (200, 201), resp.content)
        self.assertTrue(self._added())

    def test_a_missing_TEAM_LOGO_does_not_block_a_direct_add(self):
        self.event.require_team_logo = True
        self.event.save()
        self.assertIn(self._add().status_code, (200, 201))
        self.assertTrue(self._added())

    def test_a_required_CONNECTION_does_not_block_a_direct_add(self):
        """The case that started this: an event requiring Google could not be filled at all."""
        self.event.required_connections = ["google"]
        self.event.save()
        self.assertIn(self._add().status_code, (200, 201))
        self.assertTrue(self._added())

    def test_a_FULL_event_does_not_block_a_direct_add(self):
        """An admin adding one more team to a full event has decided to. Capacity is a default,
        not a wall, when a human is doing the adding."""
        self.event.max_teams_or_players = 1
        self.event.save()
        other, _ = _user("bulkother")
        filler = _team(self.admin, "FILLER TEAM", [other])
        TournamentTeam.objects.create(event=self.event, team=filler)
        self.assertIn(self._add().status_code, (200, 201))
        self.assertTrue(self._added())

    # ── what still refuses ────────────────────────────────────────────────────────────────────
    def test_a_BANNED_team_is_still_refused(self):
        """A ban is not a requirement. It is an enforcement decision already taken."""
        self.team.is_banned = True
        self.team.save()
        resp = self._add()
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("team_banned", resp.json()["blocked"][0]["codes"])
        self.assertFalse(self._added())

    def test_a_team_with_a_BANNED_PLAYER_is_still_refused(self):
        BannedPlayer.objects.create(
            banned_player=self.p1, is_active=True,
            ban_duration=30,   # required, no model default
            ban_end_date=timezone.now() + timedelta(days=30),
        )
        resp = self._add()
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("player_banned", resp.json()["blocked"][0]["codes"])
        self.assertFalse(self._added())

    def test_a_refusal_still_names_the_team(self):
        self.team.is_banned = True
        self.team.save()
        self.assertEqual(self._add().json()["blocked"][0]["team_name"], "CATALYST TEST")

    # ── the record ────────────────────────────────────────────────────────────────────────────
    def test_stepping_over_a_requirement_is_RECORDED_without_asking_the_admin(self):
        """The admin is asked for nothing, and the record still exists afterwards.

        This is what the previous gate was really protecting: a direct add used to skip everything
        with no trace of what had been skipped.
        """
        self.event.require_player_uid = True
        self.event.save()
        self.assertIn(self._add().status_code, (200, 201))

        waiver = EventRequirementWaiver.objects.get(event=self.event, team=self.team)
        self.assertIn("registration_requirements_unmet", waiver.waived_codes)
        self.assertEqual(waiver.created_by, self.admin)
        self.assertIn(self.admin.username, waiver.reason)

    def test_nothing_is_recorded_when_nothing_was_stepped_over(self):
        """A waiver on every ordinary add would be noise, and noise is how a record stops being
        read."""
        self.assertIn(self._add().status_code, (200, 201))
        self.assertFalse(EventRequirementWaiver.objects.filter(event=self.event).exists())


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class InvitationsStillEnforceRequirementsTests(TestCase):
    """The other half of the owner's rule: "invitations is different".

    An invited team registers ITSELF through register_for_event. That path is untouched by the
    direct-add change, and this pins it so a future edit to one cannot quietly change the other.
    """

    def setUp(self):
        self.admin, _ = _user("inviteadmin", role="admin")
        self.captain, self.captain_token = _user("invitecaptain")
        self.team = _team(self.captain, "INVITED TEAM", [self.captain])
        self.event = Event.objects.create(
            event_name="Invite Rule Cup", slug="invite-rule-cup",
            competition_type="tournament", participant_type="squad",
            event_type="online", event_mode="single",
            max_teams_or_players=16, number_of_stages=1,
            start_date=date.today() + timedelta(days=10),
            end_date=date.today() + timedelta(days=11),
            registration_open_date=date.today() - timedelta(days=1),
            registration_end_date=date.today() + timedelta(days=5),
            require_player_uid=True,
            creator=self.admin,
        )

    def test_a_team_registering_ITSELF_is_still_held_to_the_requirements(self):
        resp = Client().post(
            "/events/register-for-event/",
            data=json.dumps({"event_id": self.event.event_id, "team_id": self.team.team_id}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.captain_token}",
        )
        # The captain has no UID and the event requires one, so this must NOT succeed. The exact
        # status is register_for_event's business; what matters here is that it refused and the
        # team is not registered.
        self.assertNotIn(resp.status_code, (200, 201), resp.content)
        self.assertFalse(
            TournamentTeam.objects.filter(event=self.event, team=self.team).exists()
        )
