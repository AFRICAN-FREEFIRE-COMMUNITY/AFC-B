"""
Tests for the per-event WHATSAPP NUMBER registration requirement (owner 2026-08-03).

WHY the feature exists: AFC sends room ID / password over WhatsApp, but only a tiny fraction of
players have a number on their profile, so those messages reach almost nobody. Rather than nag every
player at registration, an event that relies on WhatsApp room details can switch on
Event.require_whatsapp and refuse registrations from players with no number.

WHAT IS COVERED (the three states the owner asked for, on BOTH the solo and the team path):
  1. requirement OFF                                   -> registration proceeds
  2. requirement ON, someone has no number              -> 403, and the body NAMES that player
  3. requirement ON, everyone has a number              -> registration proceeds

The gate itself lives in afc_tournament_and_scrims.views._missing_registration_assets (shared with
the esports-image / profile-image / Free Fire UID requirements and with the event_links qualification
promotion gate), and the 403 body is built by _registration_requirements_response. The number is read
off afc_auth.UserProfile.whatsapp_number.

Run: python manage.py test afc_tournament_and_scrims.test_whatsapp_requirement
"""
from datetime import date, timedelta

from django.test import TestCase, Client

from afc_auth.models import SessionToken, User, UserProfile
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import Event


def _user(username, role="player"):
    """A registerable player + a bearer token. Password never matters: register_for_event
    authenticates off SessionToken, so tests mint one directly (see the repo's other suites)."""
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _profile(user, whatsapp=""):
    """The user's UserProfile row. whatsapp="" mirrors the model default (a player who never
    filled the field in), which is exactly the state the requirement is meant to catch."""
    return UserProfile.objects.create(user=user, whatsapp_number=whatsapp)


def _event(creator, **overrides):
    fields = dict(
        event_name="WhatsApp Cup", competition_type="tournament", participant_type="solo",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


def _register(event, token, **extra):
    body = {"event_id": event.event_id, **extra}
    return Client().post(
        "/events/register-for-event/", body,
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
    )


class SoloWhatsAppRequirementTests(TestCase):
    """A SOLO registrant is judged on their own UserProfile.whatsapp_number.

    NOTE on the "proceeds" cases: the solo path ALWAYS demands a connected Discord account
    (register_for_event ~line 6490, unconditional - not the per-event require_discord toggle), and
    that check sits AFTER the per-player requirement gate. So a solo registration in a test can never
    reach 201 without mocking Discord. Asserting that the response is the DISCORD message instead of
    the requirements block is therefore the precise proof that the WhatsApp gate let the player past,
    and it is the same technique test_esport_media.py uses for the esports-image gate."""

    #: What register_for_event answers once the per-player requirement gate has been cleared.
    NEXT_GATE_MESSAGE = "Connect your Discord account first."

    def setUp(self):
        self.admin, _ = _user("waadmin", role="admin")
        self.player, self.player_token = _user("waplayer")

    def assertClearedWhatsAppGate(self, resp):
        body = resp.json()
        self.assertNotEqual(body.get("code"), "registration_requirements_unmet", body)
        self.assertEqual(body.get("message"), self.NEXT_GATE_MESSAGE, body)

    def test_off_lets_registration_through(self):
        """Requirement OFF: a player with no number is not stopped by the requirement gate (the
        default for every pre-existing event, so this is the no-regression case)."""
        event = _event(self.admin, require_whatsapp=False)
        _profile(self.player, whatsapp="")
        resp = _register(event, self.player_token)
        self.assertClearedWhatsAppGate(resp)

    def test_on_blocks_and_names_the_player_without_a_number(self):
        """Requirement ON + blank number -> 403 whose `missing` list names the player and the
        field, so the FE roster panel can badge exactly who has to go add one."""
        event = _event(self.admin, require_whatsapp=True)
        _profile(self.player, whatsapp="")
        resp = _register(event, self.player_token)
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body.get("code"), "registration_requirements_unmet")
        self.assertEqual(len(body["missing"]), 1)
        self.assertEqual(body["missing"][0]["username"], "waplayer")
        self.assertIn("whatsapp", body["missing"][0]["fields"])

    def test_on_blocks_when_the_player_has_no_profile_row_at_all(self):
        """No UserProfile row is the same as a blank number: the player still cannot be messaged."""
        event = _event(self.admin, require_whatsapp=True)
        resp = _register(event, self.player_token)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("whatsapp", resp.json()["missing"][0]["fields"])

    def test_on_blocks_a_whitespace_only_number(self):
        """A number of only spaces is not a number: the gate strips before judging."""
        event = _event(self.admin, require_whatsapp=True)
        _profile(self.player, whatsapp="   ")
        resp = _register(event, self.player_token)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("whatsapp", resp.json()["missing"][0]["fields"])

    def test_on_lets_a_player_with_a_number_through(self):
        event = _event(self.admin, require_whatsapp=True)
        _profile(self.player, whatsapp="+2348012345678")
        resp = _register(event, self.player_token)
        self.assertClearedWhatsAppGate(resp)

    def test_duplicate_profile_rows_resolve_to_the_canonical_one(self):
        """Prod has users with SEVERAL UserProfile rows (UserProfile.user is a plain FK). The gate
        must read the canonical row afc_auth.canonical_profile() resolves - the LOWEST profile_id,
        which is the row the profile editor writes - or a player who just saved a number would still
        be blocked by a stale duplicate."""
        event = _event(self.admin, require_whatsapp=True)
        _profile(self.player, whatsapp="+2348012345678")  # canonical (created first = lowest id)
        _profile(self.player, whatsapp="")               # stray duplicate
        resp = _register(event, self.player_token)
        self.assertClearedWhatsAppGate(resp)


class TeamWhatsAppRequirementTests(TestCase):
    """A TEAM registration is judged on EVERY selected roster member, and the 403 must name each
    one so the captain knows who to chase."""

    def setUp(self):
        self.admin, _ = _user("wateamadmin", role="admin")
        self.captain, self.captain_token = _user("wacaptain")
        self.mates = [_user(f"wamate{i}")[0] for i in range(3)]
        self.team = Team.objects.create(
            team_name="WhatsApp FC", team_owner=self.captain, team_creator=self.captain,
            join_settings="open", country="Nigeria",
        )
        TeamMembers.objects.create(team=self.team, member=self.captain, management_role="team_captain")
        for m in self.mates:
            TeamMembers.objects.create(team=self.team, member=m, management_role="member")
        self.roster = [self.captain.user_id] + [m.user_id for m in self.mates]

    def _give_numbers(self, users, number="+2348012345678"):
        for u in users:
            _profile(u, whatsapp=number)

    def _register_team(self, event):
        return _register(event, self.captain_token, team_id=self.team.team_id,
                         roster_member_ids=self.roster)

    def test_off_lets_registration_through(self):
        """Requirement OFF: a roster where nobody has a number registers all the way through (201).
        Unlike the solo path there is no unconditional Discord check for teams, so this is a real
        end-to-end success, not just "cleared the gate"."""
        event = _event(self.admin, participant_type="squad", require_whatsapp=False)
        resp = self._register_team(event)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_on_blocks_and_names_only_the_members_without_a_number(self):
        """One offender out of four: the 403 must single that player out (not the whole roster),
        because the panel's whole job is telling the captain WHO to chase."""
        event = _event(self.admin, participant_type="squad", require_whatsapp=True)
        self._give_numbers([self.captain] + self.mates[:2])
        _profile(self.mates[2], whatsapp="")  # the only one with no number
        resp = self._register_team(event)
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body.get("code"), "registration_requirements_unmet")
        self.assertEqual([m["username"] for m in body["missing"]], [self.mates[2].username])
        self.assertEqual(body["missing"][0]["fields"], ["whatsapp"])

    def test_on_names_every_offender(self):
        event = _event(self.admin, participant_type="squad", require_whatsapp=True)
        self._give_numbers([self.captain])
        for m in self.mates:
            _profile(m, whatsapp="")
        resp = self._register_team(event)
        self.assertEqual(resp.status_code, 403)
        named = {m["username"] for m in resp.json()["missing"]}
        self.assertEqual(named, {m.username for m in self.mates})

    def test_on_lets_a_fully_covered_roster_through(self):
        event = _event(self.admin, participant_type="squad", require_whatsapp=True)
        self._give_numbers([self.captain] + self.mates)
        resp = self._register_team(event)
        self.assertEqual(resp.status_code, 201, resp.content)


class WhatsAppRequirementPayloadTests(TestCase):
    """The flag has to survive the round trip the wizards depend on: create_event stores it and
    get_event_details echoes it back so the edit form rehydrates its toggle."""

    def setUp(self):
        self.admin, self.admin_token = _user("wapayload", role="admin")

    def test_get_event_details_echoes_the_flag(self):
        event = _event(self.admin, require_whatsapp=True)
        # get_event_details is keyed by SLUG (not event_id) - it is the public event page's endpoint.
        resp = Client().post(
            "/events/get-event-details/", {"slug": event.slug},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["event_details"]["require_whatsapp"])

    def test_flag_defaults_off(self):
        """Every event created before this feature (and every one whose creator leaves the toggle
        alone) must behave exactly as it did: no WhatsApp gate."""
        self.assertFalse(_event(self.admin).require_whatsapp)
