"""
Tests for inviting SOLO PLAYERS to a solo event (owner 2026-08-26).

WHY the feature exists: a solo event could not invite anybody. create_team_invitations refused it
outright ("This is a solo event. Teams can only be invited to duo or squad events."), and
add_teams_to_event is team-only, so the only way to get a named player into a solo event was to ask
them to register themselves.

WHAT IS COVERED
  1. an admin invites players to a solo event, and rows are written against the PLAYER
  2. the wrong shape is refused both ways (teams to a solo event, players to a squad event)
  3. skip reasons work for players: already registered, already invited, banned, not found
  4. only the INVITED player may answer, and accepting replays through register_for_event with no
     team_id, so a solo invitee passes exactly the gates a self-registering solo player passes
  5. a waiver granted to that player is honoured on the invited registration, which is the second
     half of the owner's ask

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_tournament_and_scrims.test_solo_invitations
"""
from datetime import date, timedelta

from django.test import Client, TestCase, override_settings

from afc_auth.models import BannedPlayer, SessionToken, User, UserProfile
from afc_team.models import Team
from afc_tournament_and_scrims import waivers
from afc_tournament_and_scrims.models import (
    Event,
    EventTeamInvitation,
    RegisteredCompetitors,
)


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _event(creator, **overrides):
    fields = dict(
        event_name="Solo Invite Cup", competition_type="tournament", participant_type="solo",
        event_type="online", max_teams_or_players=16, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator, is_public=True,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


class CreateSoloInvitationTests(TestCase):
    def setUp(self):
        self.admin, self.admin_token = _user("siadmin", role="admin")
        self.player, self.player_token = _user("siplayer")
        self.other, _ = _user("siother")
        self.event = _event(self.admin)

    def _create(self, **body):
        payload = {"event_id": self.event.event_id, "delivery": "push"}
        payload.update(body)
        return Client().post(
            "/events/team-invitations/create/", payload,
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )

    def test_a_solo_event_can_invite_players(self):
        resp = self._create(user_ids=[self.player.user_id, self.other.user_id])
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(len(resp.json()["invited"]), 2)
        rows = EventTeamInvitation.objects.filter(event=self.event)
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(r.user_id and r.team_id is None for r in rows))

    def test_the_serialized_row_names_the_player(self):
        body = self._create(user_ids=[self.player.user_id]).json()
        row = body["invited"][0]
        self.assertEqual(row["username"], "siplayer")
        self.assertTrue(row["is_solo"])
        self.assertIsNone(row["team_id"])

    def test_the_default_kind_becomes_per_player(self):
        """An older client sends no kind at all and means "one each". On a solo event per_team is
        meaningless, so it is translated rather than refused."""
        self._create(user_ids=[self.player.user_id])
        campaign = EventTeamInvitation.objects.get(event=self.event).campaign
        self.assertEqual(campaign.kind, "per_player")

    def test_inviting_teams_to_a_solo_event_is_refused(self):
        team = Team.objects.create(
            team_name="Wrong Shape FC", team_owner=self.admin, team_creator=self.admin,
        )
        resp = self._create(team_ids=[team.team_id])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invite players", resp.json()["message"])

    def test_inviting_players_to_a_team_event_is_refused(self):
        squad = _event(self.admin, participant_type="squad", event_name="Squad Shape Cup")
        resp = Client().post(
            "/events/team-invitations/create/",
            {"event_id": squad.event_id, "user_ids": [self.player.user_id], "delivery": "push"},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invite teams", resp.json()["message"])

    def test_an_already_registered_player_is_skipped_with_a_reason(self):
        RegisteredCompetitors.objects.create(
            event=self.event, user=self.player, status="registered",
        )
        body = self._create(user_ids=[self.player.user_id]).json()
        self.assertEqual(body["skipped"][0]["reason"], "already_registered")
        self.assertEqual(body["skipped"][0]["username"], "siplayer")

    def test_a_second_invitation_to_the_same_player_is_skipped(self):
        self._create(user_ids=[self.player.user_id])
        body = self._create(user_ids=[self.player.user_id]).json()
        self.assertEqual(body["skipped"][0]["reason"], "already_invited")

    def test_a_banned_player_is_skipped(self):
        BannedPlayer.objects.create(
            banned_player=self.player, is_active=True,
            ban_duration=30,  # NOT NULL on the model
            ban_end_date=date.today() + timedelta(days=30),
        )
        body = self._create(user_ids=[self.player.user_id]).json()
        self.assertEqual(body["skipped"][0]["reason"], "banned")

    def test_an_unknown_player_id_is_skipped_not_a_500(self):
        body = self._create(user_ids=[99999999]).json()
        self.assertEqual(body["skipped"][0]["reason"], "not_found")

    def test_an_empty_pick_is_refused(self):
        resp = self._create(user_ids=[])
        self.assertEqual(resp.status_code, 400)


class AnswerSoloInvitationTests(TestCase):
    def setUp(self):
        self.admin, self.admin_token = _user("saadmin", role="admin")
        self.player, self.player_token = _user("saplayer")
        self.stranger, self.stranger_token = _user("sastranger")
        self.event = _event(self.admin, event_name="Solo Answer Cup")
        Client().post(
            "/events/team-invitations/create/",
            {"event_id": self.event.event_id, "user_ids": [self.player.user_id],
             "delivery": "push"},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.invitation = EventTeamInvitation.objects.get(event=self.event)

    def _accept(self, token):
        return Client().post(
            f"/events/team-invitations/{self.invitation.id}/accept/", {},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_a_stranger_cannot_answer_someone_elses_invitation(self):
        resp = self._accept(self.stranger_token)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("invited player", resp.json()["message"])

    def test_the_invited_player_reaches_the_real_registration_gates(self):
        """Accepting replays through register_for_event with NO team_id. The solo path always
        demands a connected Discord account, so reaching THAT refusal is the proof the replay took
        the solo branch and got as far as the ordinary gates."""
        resp = self._accept(self.player_token)
        self.assertEqual(resp.json().get("message"), "Connect your Discord account first.")

    def test_declining_records_the_answer(self):
        resp = Client().post(
            f"/events/team-invitations/{self.invitation.id}/decline/",
            {"reason": "Busy that weekend"},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.player_token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.status, "declined")
        self.assertEqual(self.invitation.decline_reason, "Busy that weekend")


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
class SoloWaiverOnInvitationTests(TestCase):
    """The second half of the owner's ask: an invited SOLO player can be excused from a requirement,
    exactly as an invited team can."""

    def setUp(self):
        self.admin, self.admin_token = _user("swadmin", role="admin")
        self.player, self.player_token = _user("swplayer")
        self.event = _event(
            self.admin, event_name="Solo Waiver Cup", required_connections=["google"],
        )

    def _register(self):
        return Client().post(
            "/events/register-for-event/", {"event_id": self.event.event_id},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.player_token}",
        )

    def test_without_a_waiver_the_player_is_refused_by_name(self):
        body = self._register().json()
        self.assertEqual(body["code"], "registration_requirements_unmet")
        self.assertIn("connection:google", body["missing"][0]["fields"])

    def test_a_waiver_naming_the_player_lets_them_past(self):
        waivers.grant(
            self.event, actor=self.admin, reason="Invited by AFC",
            codes=["registration_requirements_unmet"], user=self.player,
        )
        body = self._register().json()
        self.assertNotEqual(body.get("code"), "registration_requirements_unmet", body)

    def test_the_waiver_api_accepts_a_user_id(self):
        resp = Client().post(
            "/events/waivers/",
            {"event_id": self.event.event_id, "user_id": self.player.user_id,
             "codes": ["registration_requirements_unmet"], "reason": "Invited by AFC"},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["waiver"]["user_id"], self.player.user_id)
        self.assertIsNone(resp.json()["waiver"]["team_id"])
