# afc_tournament_and_scrims/tests_blacklist.py
# ──────────────────────────────────────────────────────────────────────────────
# REGISTRATION ENFORCEMENT tests for the ORGANIZER BLACKLIST feature (2026-06-10).
#
# These exercise register_for_event (TEAM path) against organizer blacklists, proving the
# follows-the-player rule end-to-end:
#   - a blacklisted team cannot register for that organizer's event (team-level block);
#   - a SNAPSHOT PLAYER who LEFT the blacklisted team and JOINED ANOTHER team STILL cannot
#     register for that organizer's events (player-level block, the core rule);
#   - a player who was never on the blacklisted team registers fine;
#   - a blacklist for a DIFFERENT organization does not block;
#   - an EXPIRED blacklist (end_date in the past) does not block;
#   - after an organizer lift, the team + players can register again.
#
# The enforcement helper is afc_organizers.blacklist.organizer_blacklist_block, wired into
# register_for_event after the existing ban checks (only for events with an owning Organization).
#
# We use a DUO event (roster size exactly 2) because the duo team path has the fewest unrelated
# gates: there are no Discord membership calls on the team path (they are commented out in
# register_for_event), and country defaults to a shared value, so nothing here hits the network.
# Auth is a real bearer SessionToken, sent in the Authorization header.
# ──────────────────────────────────────────────────────────────────────────────
import uuid
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_organizers.models import (
    Organization,
    OrganizationMember,
    OrganizerBlacklist,
    OrganizerBlacklistPlayer,
)
from afc_team.models import Team, TeamMembers

from .models import (
    Event, RegisteredCompetitors, Stages, TournamentTeam, TournamentTeamMember,
)


class OrganizerBlacklistEnforcementTests(TestCase):
    # ── auth helpers (real bearer SessionToken) ───────────────────────────────
    def _token_for(self, user):
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    # ── fixtures ──────────────────────────────────────────────────────────────
    def setUp(self):
        self.country = "Nigeria"

        # Two organizations, each owning a DUO event.
        self.org = Organization.objects.create(slug="acme", name="Acme Esports")
        self.other_org = Organization.objects.create(slug="globex", name="Globex Esports")
        self.organizer = User.objects.create_user(
            username="organizer", email="org@x.com", password="x", role="player",
            country=self.country,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.organizer, role="owner", status="active"
        )

        # Captain + two members of the BLACKLISTED team (Team Alpha).
        self.captain = User.objects.create_user(
            username="captain", email="cap@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        self.alpha_player = User.objects.create_user(
            username="alpha_player", email="ap@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        # The MOVER: starts on Team Alpha (so they get snapshotted), later leaves for Team Beta.
        self.mover = User.objects.create_user(
            username="mover", email="mover@x.com", password="x", role="player",
            status="active", country=self.country,
        )

        self.alpha = Team.objects.create(
            team_name="Team Alpha", join_settings="open",
            team_creator=self.captain, team_owner=self.captain, country=self.country,
        )
        TeamMembers.objects.create(team=self.alpha, member=self.captain,
                                   management_role="team_captain")
        TeamMembers.objects.create(team=self.alpha, member=self.alpha_player,
                                   management_role="member")
        TeamMembers.objects.create(team=self.alpha, member=self.mover,
                                   management_role="member")

        # A SECOND clean team (Team Beta) the mover will later join.
        self.beta_owner = User.objects.create_user(
            username="beta_owner", email="bo@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        self.beta = Team.objects.create(
            team_name="Team Beta", join_settings="open",
            team_creator=self.beta_owner, team_owner=self.beta_owner, country=self.country,
        )
        TeamMembers.objects.create(team=self.beta, member=self.beta_owner,
                                   management_role="team_captain")

        # Events: org and other_org each run a public DUO event with an OPEN reg window.
        self.event = self._make_duo_event(self.org, "Acme Cup")
        self.other_event = self._make_duo_event(self.other_org, "Globex Cup")

    def _make_duo_event(self, organization, name):
        today = date.today()
        event = Event.objects.create(
            competition_type="tournament",
            participant_type="duo",
            event_type="internal",
            max_teams_or_players=16,
            event_name=name,
            event_mode="virtual",
            start_date=today + timedelta(days=3),
            end_date=today + timedelta(days=5),
            registration_open_date=today - timedelta(days=1),
            registration_end_date=today + timedelta(days=2),
            prizepool="$1000",
            prizepool_cash_value=1000,
            prize_distribution={"1": "100%"},
            event_rules="No cheating",
            event_status="upcoming",
            registration_link="https://example.com/reg",
            tournament_tier="tier_1",
            number_of_stages=1,
            creator=self.organizer,
            organization=organization,
            is_draft=False,
            is_public=True,
            registration_type="free",
        )
        # register_for_event's TEAM success path reads Stages.objects.filter(event=event).first()
        # to queue a per-stage Discord role, so a successful (201) registration needs at least one
        # Stage to exist. (No Discord network call fires - role_id is None here.)
        Stages.objects.create(
            event=event,
            stage_name="Group Stage",
            start_date=today + timedelta(days=3),
            end_date=today + timedelta(days=4),
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=2,
        )
        return event

    def _blacklist_alpha(self, organization=None, days=30):
        """Blacklist Team Alpha under `organization` (default self.org) and snapshot its CURRENT
        members, mirroring exactly what the create endpoint does (model-level here so the
        enforcement test does not depend on the create view)."""
        organization = organization or self.org
        blacklist = OrganizerBlacklist.objects.create(
            organization=organization, team=self.alpha,
            reason="Smurfing", end_date=timezone.now() + timedelta(days=days),
            created_by=self.organizer, status="active",
        )
        for uid in TeamMembers.objects.filter(team=self.alpha).values_list("member_id", flat=True):
            OrganizerBlacklistPlayer.objects.create(blacklist=blacklist, user_id=uid)
        return blacklist

    def _register(self, *, actor, event, team, roster):
        return self.client.post(
            reverse("register_for_event"),
            data={
                "event_id": event.event_id,
                "team_id": team.team_id,
                "roster_member_ids": [u.user_id for u in roster],
            },
            content_type="application/json",
            **self._auth(actor),
        )

    # ── sanity: with NO blacklist, the team registers fine ─────────────────────
    def test_no_blacklist_team_registers(self):
        resp = self._register(
            actor=self.captain, event=self.event, team=self.alpha,
            roster=[self.captain, self.alpha_player],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── team-level block: a blacklisted team cannot register ───────────────────
    def test_blacklisted_team_cannot_register(self):
        self._blacklist_alpha()
        resp = self._register(
            actor=self.captain, event=self.event, team=self.alpha,
            roster=[self.captain, self.alpha_player],
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("blacklisted", resp.json()["message"].lower())
        self.assertFalse(TournamentTeam.objects.filter(event=self.event, team=self.alpha).exists())

    # ── THE CORE RULE: a snapshot player who LEFT the team and JOINED ANOTHER ───
    # team is STILL blocked from the organizer's events ─────────────────────────
    def test_snapshot_player_blocked_after_moving_to_another_team(self):
        # Blacklist Team Alpha -> the mover is snapshotted.
        self._blacklist_alpha()

        # The mover LEAVES Team Alpha and JOINS Team Beta (a totally separate, clean team).
        TeamMembers.objects.filter(team=self.alpha, member=self.mover).delete()
        TeamMembers.objects.create(team=self.beta, member=self.mover, management_role="member")

        # Beta tries to register for the SAME organizer's event with the mover on the roster.
        resp = self._register(
            actor=self.beta_owner, event=self.event, team=self.beta,
            roster=[self.beta_owner, self.mover],
        )

        # The mover stays blocked even though Team Beta is not blacklisted: the block follows the
        # PLAYER (queried by org + user), not the team.
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("mover", resp.json()["message"].lower())
        self.assertFalse(TournamentTeam.objects.filter(event=self.event, team=self.beta).exists())

    # ── a player who was NEVER on the blacklisted team registers fine ──────────
    def test_unrelated_player_team_registers_fine(self):
        # Blacklist Team Alpha (Beta's players were never on it).
        self._blacklist_alpha()
        resp = self._register(
            actor=self.beta_owner, event=self.event, team=self.beta,
            roster=[self.beta_owner],  # duo needs 2; add a clean second player below
        )
        # duo needs exactly 2 -> add a second clean player to Beta and retry.
        clean = User.objects.create_user(
            username="clean", email="clean@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        TeamMembers.objects.create(team=self.beta, member=clean, management_role="member")
        resp = self._register(
            actor=self.beta_owner, event=self.event, team=self.beta,
            roster=[self.beta_owner, clean],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── a blacklist for a DIFFERENT organization does not block ────────────────
    def test_blacklist_for_other_org_does_not_block(self):
        # Team Alpha is blacklisted by other_org, then registers for self.org's event.
        self._blacklist_alpha(organization=self.other_org)
        resp = self._register(
            actor=self.captain, event=self.event, team=self.alpha,
            roster=[self.captain, self.alpha_player],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── an EXPIRED blacklist (end_date in the past) does not block ──────────────
    def test_expired_blacklist_does_not_block(self):
        # Create the blacklist then force its end_date into the past (status still "active",
        # but is_currently_active() is False -> enforcement must let it through).
        blacklist = self._blacklist_alpha()
        OrganizerBlacklist.objects.filter(pk=blacklist.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        resp = self._register(
            actor=self.captain, event=self.event, team=self.alpha,
            roster=[self.captain, self.alpha_player],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── after an organizer lift, the team can register again ───────────────────
    def test_lifted_blacklist_lets_team_register_again(self):
        blacklist = self._blacklist_alpha()
        # Mirror lift_blacklist: status lifted + all player rows deactivated.
        blacklist.status = "lifted"
        blacklist.save(update_fields=["status"])
        blacklist.players.update(is_active=False)

        resp = self._register(
            actor=self.captain, event=self.event, team=self.alpha,
            roster=[self.captain, self.alpha_player],
        )
        self.assertEqual(resp.status_code, 201, resp.content)


# ──────────────────────────────────────────────────────────────────────────────
# THE ROSTER DOORS. Registration was the ONLY place the blacklist was ever checked, so a captain
# could register a clean roster, pass every gate, and then swap the barred player straight back
# in. Three endpoints created TournamentTeamMember rows and checked nothing: edit_roster,
# add_player_to_event_roster and add_teams_to_event. These tests hold the shared guard
# (afc_organizers.blacklist.roster_change_block) against all three, because a feature whose whole
# promise is "the block follows the player" is worth nothing if the player can be added later.
# ──────────────────────────────────────────────────────────────────────────────
class RosterChangeBlacklistTests(TestCase):
    def setUp(self):
        self.country = "Nigeria"
        self.org = Organization.objects.create(slug="roster-acme", name="Acme Esports")
        self.captain = User.objects.create_user(
            username="rc_captain", email="rc_cap@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        self.clean = User.objects.create_user(
            username="rc_clean", email="rc_clean@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        self.barred = User.objects.create_user(
            username="rc_barred", email="rc_barred@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        self.team = Team.objects.create(
            team_name="Roster Team", join_settings="open",
            team_creator=self.captain, team_owner=self.captain, country=self.country,
        )
        for u in (self.captain, self.clean, self.barred):
            TeamMembers.objects.create(team=self.team, member=u, management_role="member")

        self.event = Event.objects.create(
            event_name="Roster Cup", slug="roster-cup", organization=self.org,
            participant_type="duo", competition_type="tournament", event_type="virtual",
            max_teams_or_players=16, is_public=True, is_draft=False, number_of_stages=1,
            start_date=timezone.now().date(), end_date=timezone.now().date(),
            registration_open_date=timezone.now().date(),
            registration_end_date=timezone.now().date(),
        )

        # The organizer bars ONE player, not the team.
        bl = OrganizerBlacklist.objects.create(
            organization=self.org, team=None, target_type="player",
            reason="Roster-door test", status="active",
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=30),
        )
        OrganizerBlacklistPlayer.objects.create(blacklist=bl, user=self.barred)

    def test_the_guard_refuses_a_barred_player_and_passes_a_clean_one(self):
        """The shared gate itself, exercised directly. Every roster door calls this, so if it is
        right and every door calls it, the hole cannot reopen in one endpoint at a time."""
        from afc_organizers.blacklist import roster_change_block

        self.assertIsNone(roster_change_block(self.event, self.team, [self.clean.user_id]))
        message = roster_change_block(self.event, self.team, [self.barred.user_id])
        self.assertIsNotNone(message)
        self.assertIn("rc_barred", message)

    def test_adding_a_barred_player_to_an_existing_roster_is_refused(self):
        """The actual bypass: register clean, then add the barred player afterwards."""
        from afc_organizers.blacklist import roster_change_block

        tt = TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.captain, event=self.event, status="active")

        # The edit adds the barred player. Only the ADDITION is checked, which is why the
        # already-rostered captain does not matter here.
        self.assertIsNotNone(
            roster_change_block(self.event, self.team, [self.barred.user_id]))

    def test_players_already_on_the_roster_are_not_re_checked(self):
        """A blacklist created mid-event must not block an unrelated later edit. Callers pass
        only the players being ADDED, so an existing roster member is never re-judged, and the
        captain fixing a typo is not punished for somebody else's ban."""
        from afc_organizers.blacklist import roster_change_block

        self.assertIsNone(roster_change_block(self.event, self.team, []))
        self.assertIsNone(roster_change_block(self.event, self.team, [self.clean.user_id]))

    def test_an_event_with_no_organization_still_blocks_a_site_banned_player(self):
        """Native AFC events have no owning Organization, so no organizer blacklist can apply,
        but a site-wide ban must still stop the addition."""
        from afc_auth.models import BannedPlayer
        from afc_organizers.blacklist import roster_change_block

        native = Event.objects.create(
            event_name="Native Cup", slug="native-cup", organization=None,
            participant_type="duo", competition_type="tournament", event_type="virtual",
            max_teams_or_players=16, is_public=True, is_draft=False, number_of_stages=1,
            start_date=timezone.now().date(), end_date=timezone.now().date(),
            registration_open_date=timezone.now().date(),
            registration_end_date=timezone.now().date(),
        )
        # No organizer blacklist reaches a native event.
        self.assertIsNone(roster_change_block(native, self.team, [self.barred.user_id]))

        BannedPlayer.objects.create(
            banned_player=self.barred, ban_duration=30, is_active=True,
            ban_end_date=timezone.now() + timedelta(days=30),
        )
        message = roster_change_block(native, self.team, [self.barred.user_id])
        self.assertIsNotNone(message)
        self.assertIn("banned from AFC", message)

    def test_an_expired_site_ban_does_not_block(self):
        """is_active alone is not enough: a ban row stays is_active True past its end date until
        something sweeps it, so the window has to be checked or an expired ban blocks forever."""
        from afc_auth.models import BannedPlayer
        from afc_organizers.blacklist import roster_change_block

        native = Event.objects.create(
            event_name="Native Cup 2", slug="native-cup-2", organization=None,
            participant_type="duo", competition_type="tournament", event_type="virtual",
            max_teams_or_players=16, is_public=True, is_draft=False, number_of_stages=1,
            start_date=timezone.now().date(), end_date=timezone.now().date(),
            registration_open_date=timezone.now().date(),
            registration_end_date=timezone.now().date(),
        )
        BannedPlayer.objects.create(
            banned_player=self.clean, ban_duration=1, is_active=True,
            ban_end_date=timezone.now() - timedelta(days=1),
        )
        self.assertIsNone(roster_change_block(native, self.team, [self.clean.user_id]))
