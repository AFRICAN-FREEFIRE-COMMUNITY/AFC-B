"""
Tests for the per-event REQUIRED CONNECTED ACCOUNTS rule (owner 2026-08-26).

WHY the feature exists: some events are run through a partner platform, and the organizer needs
every participant reachable there before the event starts, not after.

WHAT IS COVERED, mirroring test_whatsapp_requirement.py because this is the same SHAPE of rule:
  1. requirement empty                            -> registration proceeds
  2. requirement set, someone has not connected    -> 403, and the body NAMES that player
  3. requirement set, everyone has connected       -> registration proceeds
plus the write path: an unknown provider slug is refused rather than stored, and duplicate_event
carries the field (it copies every require_* BY HAND, so a new field that is not added there is
silently dropped from every duplicated event).

The gate lives in afc_tournament_and_scrims.views._missing_registration_assets, shared with the
esports-image / profile-image / UID / WhatsApp requirements AND with the event_links qualification
promotion gate. The link is read off afc_auth.ConnectedAccount.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_tournament_and_scrims.test_required_connections
"""
from datetime import date, timedelta

from django.test import Client, TestCase, override_settings

from afc_auth.models import ConnectedAccount, SessionToken, User, UserProfile
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import Event


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _connect(user, provider="google"):
    return ConnectedAccount.objects.create(
        user=user, provider=provider,
        provider_user_id=f"{provider}-{user.user_id}", username=user.username,
    )


def _event(creator, **overrides):
    fields = dict(
        event_name="Connections Cup", competition_type="tournament", participant_type="solo",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


def _register(event, token, **extra):
    return Client().post(
        "/events/register-for-event/", {"event_id": event.event_id, **extra},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
    )


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", DISCORD_CLIENT_ID="", DISCORD_CLIENT_SECRET="")
class SoloRequiredConnectionTests(TestCase):
    """A SOLO registrant is judged on their own ConnectedAccount rows.

    NOTE on the "proceeds" cases: the solo path ALWAYS demands a connected Discord account
    (register_for_event, unconditional, NOT the per-event require_discord toggle), and that check
    sits AFTER the per-player requirement gate. So a solo registration in a test can never reach 201
    without mocking Discord. Asserting the response is the DISCORD message instead of the
    requirements block is the precise proof this gate let the player past. Same technique as
    test_whatsapp_requirement.py and test_esport_media.py.
    """

    NEXT_GATE_MESSAGE = "Connect your Discord account first."

    def setUp(self):
        self.admin, _ = _user("rcadmin", role="admin")
        self.player, self.player_token = _user("rcplayer")

    def assertClearedTheGate(self, resp):
        body = resp.json()
        self.assertNotEqual(body.get("code"), "registration_requirements_unmet", body)
        self.assertEqual(body.get("message"), self.NEXT_GATE_MESSAGE, body)

    def test_empty_requirement_lets_registration_through(self):
        event = _event(self.admin, required_connections=[])
        self.assertClearedTheGate(_register(event, self.player_token))

    def test_missing_connection_is_refused_and_names_the_player(self):
        event = _event(self.admin, required_connections=["google"])
        resp = _register(event, self.player_token)
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["code"], "registration_requirements_unmet")
        self.assertEqual(body["missing"][0]["username"], "rcplayer")
        self.assertIn("connection:google", body["missing"][0]["fields"])

    def test_connected_player_passes(self):
        event = _event(self.admin, required_connections=["google"])
        _connect(self.player, "google")
        self.assertClearedTheGate(_register(event, self.player_token))

    def test_a_different_provider_does_not_satisfy_the_rule(self):
        event = _event(self.admin, required_connections=["google"])
        _connect(self.player, "discord")
        resp = _register(event, self.player_token)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("connection:google", resp.json()["missing"][0]["fields"])


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
class TeamRequiredConnectionTests(TestCase):
    """EVERY roster member must have the account connected, not just the captain. Same as every
    other per-player require_* rule on the same form."""

    def setUp(self):
        self.admin, _ = _user("rctadmin", role="admin")
        self.captain, self.captain_token = _user("rccaptain")
        # A squad event demands 4 to 6 players, and that check runs BEFORE the requirements gate,
        # so a two-player roster would be refused for its size and never reach the rule under test.
        self.mates = [_user(f"rcmate{i}")[0] for i in range(3)]
        self.team = Team.objects.create(
            team_name="RC Team", team_owner=self.captain, team_creator=self.captain,
        )
        TeamMembers.objects.create(team=self.team, member=self.captain)
        for mate in self.mates:
            TeamMembers.objects.create(team=self.team, member=mate)

    def test_one_unconnected_member_blocks_the_registration_and_is_named(self):
        event = _event(
            self.admin, participant_type="squad", required_connections=["google"],
            event_name="RC Squad Cup",
        )
        # Everyone connects EXCEPT the last mate, so the refusal must name exactly that player.
        _connect(self.captain, "google")
        for mate in self.mates[:-1]:
            _connect(mate, "google")

        resp = _register(
            event, self.captain_token, team_id=self.team.team_id,
            roster_member_ids=[self.captain.user_id] + [m.user_id for m in self.mates],
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        body = resp.json()
        self.assertEqual(body.get("code"), "registration_requirements_unmet", body)
        named = [m["username"] for m in body["missing"]]
        self.assertIn(self.mates[-1].username, named)
        self.assertNotIn("rccaptain", named)

    def test_a_fully_connected_roster_clears_this_gate(self):
        event = _event(
            self.admin, participant_type="squad", required_connections=["google"],
            event_name="RC Squad Cup 2",
        )
        _connect(self.captain, "google")
        for mate in self.mates:
            _connect(mate, "google")

        resp = _register(
            event, self.captain_token, team_id=self.team.team_id,
            roster_member_ids=[self.captain.user_id] + [m.user_id for m in self.mates],
        )
        self.assertNotEqual(
            resp.json().get("code"), "registration_requirements_unmet", resp.content
        )


class WriteAndCloneTests(TestCase):
    """The field has to be carried by every place a require_ field is repeated. duplicate_event
    copies these BY HAND, so a new field not added there is silently dropped from every duplicated
    event, which is the trap this test exists to catch."""

    def setUp(self):
        self.admin, self.admin_token = _user("rcwadmin", role="admin")

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
    def test_an_unknown_provider_slug_is_refused_not_stored(self):
        event = _event(self.admin, event_name="RC Write Cup")
        resp = Client().post(
            "/events/edit-event/",
            {"event_id": event.event_id, "required_connections": ["myspace"]},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        event.refresh_from_db()
        self.assertEqual(event.required_connections, [])

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
    def test_a_valid_slug_is_stored(self):
        event = _event(self.admin, event_name="RC Write Cup 2")
        resp = Client().post(
            "/events/edit-event/",
            {"event_id": event.event_id, "required_connections": ["google"]},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertIn(resp.status_code, (200, 201), resp.content)
        event.refresh_from_db()
        self.assertEqual(event.required_connections, ["google"])

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
    def test_discord_is_not_selectable_here(self):
        """require_discord is its own field and means MORE than this one (connected AND a member of
        the event's server). Two switches for one idea is how an organizer sets one and gets the
        other's behaviour."""
        event = _event(self.admin, event_name="RC Write Cup 3")
        resp = Client().post(
            "/events/edit-event/",
            {"event_id": event.event_id, "required_connections": ["discord"]},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="gid")
    def test_duplicate_event_carries_the_requirement(self):
        event = _event(self.admin, required_connections=["google"], event_name="RC Clone Cup")
        resp = Client().post(
            f"/events/{event.event_id}/duplicate-event/", {},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertIn(resp.status_code, (200, 201), resp.content)
        clone = Event.objects.exclude(event_id=event.event_id).order_by("-event_id").first()
        self.assertEqual(clone.required_connections, ["google"])
