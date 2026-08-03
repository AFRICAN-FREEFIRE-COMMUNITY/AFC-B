# afc_tournament_and_scrims/tests_blacklist_player.py
# ──────────────────────────────────────────────────────────────────────────────
# REGISTRATION ENFORCEMENT tests for PLAYER-TARGET organizer blacklists
# (owner backlog item 1, 2026-08-03: "organizers and admins can blacklist a PLAYER, not only a
# team").
#
# A player-target blacklist is the SAME OrganizerBlacklist model as the team one, with
# target_type="player", team=NULL, and exactly one OrganizerBlacklistPlayer row. Because
# enforcement already keys off the per-player rows by (organization, player), it flows through the
# SAME helper - afc_organizers.blacklist.organizer_blacklist_block. These tests prove the two
# things that make the feature real rather than merely recorded:
#
#   1. a player-blacklisted person is BLOCKED on the TEAM path, even on an otherwise clean team;
#   2. they are blocked on the SOLO path too. This is the hole the feature closes: before this
#      change register_for_event only called the blacklist guard on the TEAM path, so anyone
#      blacklisted could simply register solo for the same organizer's event.
#
# Plus the negatives that stop the block being over-broad: an unrelated solo registrant is NOT
# caught (a NULL team must never read as "matches every team"), another org's blacklist does not
# apply, and lifted / expired blocks stop blocking.
#
# The team-target enforcement tests (blacklisted team, follows-the-player after a transfer,
# other-org, expired, lifted) live in the sibling tests_blacklist.py. Auth is a real bearer
# SessionToken. Nothing here touches the network: the blacklist guard runs BEFORE the solo path's
# Discord checks, so every assertion lands before any outbound call would be made.
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

from .models import Event, RegisteredCompetitors, Stages, TournamentTeam


class PlayerTargetBlacklistEnforcementTests(TestCase):
    # ── auth helpers (real bearer SessionToken, same as the sibling test module) ──
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

        # Two organizations: one owns the events under test, the other proves scoping.
        self.org = Organization.objects.create(slug="acme", name="Acme Esports")
        self.other_org = Organization.objects.create(slug="globex", name="Globex Esports")
        self.organizer = User.objects.create_user(
            username="organizer", email="org@x.com", password="x", role="player",
            country=self.country,
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.organizer, role="owner", status="active"
        )

        # One clean team of two. Neither the TEAM nor its members are blacklisted at setUp;
        # individual tests blacklist the PERSON, which is the whole point.
        self.captain = User.objects.create_user(
            username="captain", email="cap@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        self.teammate = User.objects.create_user(
            username="teammate", email="tm@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        self.team = Team.objects.create(
            team_name="Team Alpha", join_settings="open",
            team_creator=self.captain, team_owner=self.captain, country=self.country,
        )
        TeamMembers.objects.create(team=self.team, member=self.captain,
                                   management_role="team_captain")
        TeamMembers.objects.create(team=self.team, member=self.teammate,
                                   management_role="member")

        # An outsider used for the "unrelated registrant is not caught" case.
        self.outsider = User.objects.create_user(
            username="outsider", email="out@x.com", password="x", role="player",
            status="active", country=self.country,
        )

        # A DUO event (team path) and a SOLO event (solo path), both owned by self.org. A
        # player-target block has to hold on BOTH registration doors.
        self.duo_event = self._make_event(self.org, "Acme Duo Cup", "duo")
        self.solo_event = self._make_event(self.org, "Acme Solo Cup", "solo")

    def _make_event(self, organization, name, participant_type):
        """A public event with an OPEN registration window. The duo and solo fixtures differ only
        in participant_type, so the two paths differ only in the thing under test."""
        today = date.today()
        event = Event.objects.create(
            competition_type="tournament",
            participant_type=participant_type,
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
        # The TEAM success path reads the first Stage to queue a per-stage Discord role, so a 201
        # needs at least one Stage to exist. (No network call fires: role_id is None here.)
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

    def _blacklist_player(self, user, organization=None, days=30):
        """Create a PLAYER-target blacklist exactly as views_blacklist._create_blacklist does for
        target_type="player": team is NULL, target_type="player", and ONE player row. Built at
        model level so this enforcement test does not depend on the create view."""
        organization = organization or self.org
        blacklist = OrganizerBlacklist.objects.create(
            organization=organization, target_type="player", team=None,
            reason="Toxic behaviour", end_date=timezone.now() + timedelta(days=days),
            created_by=self.organizer, status="active",
        )
        OrganizerBlacklistPlayer.objects.create(blacklist=blacklist, user=user, is_active=True)
        return blacklist

    def _register_team(self, *, actor, event, team, roster):
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

    def _register_solo(self, *, actor, event):
        return self.client.post(
            reverse("register_for_event"),
            data={"event_id": event.event_id},
            content_type="application/json",
            **self._auth(actor),
        )

    # ── sanity: with NO blacklist the team registers fine ──────────────────────
    def test_no_blacklist_team_registers(self):
        resp = self._register_team(
            actor=self.captain, event=self.duo_event, team=self.team,
            roster=[self.captain, self.teammate],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── TEAM path: a player-blacklisted person blocks their clean team ─────────
    def test_player_blacklist_blocks_team_registration(self):
        # The TEAM was never blacklisted. The PERSON was.
        self._blacklist_player(self.captain)
        resp = self._register_team(
            actor=self.captain, event=self.duo_event, team=self.team,
            roster=[self.captain, self.teammate],
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        # The message names WHO is blocked so a captain knows who to drop.
        self.assertIn("captain", resp.json()["message"].lower())
        self.assertFalse(
            TournamentTeam.objects.filter(event=self.duo_event, team=self.team).exists()
        )

    # ── TEAM path: their team-mates are NOT blocked ────────────────────────────
    # The block must follow the one person, not spread to everyone standing near them.
    def test_player_blacklist_does_not_block_teammates(self):
        self._blacklist_player(self.captain)
        clean = User.objects.create_user(
            username="clean_mate", email="cm@x.com", password="x", role="player",
            status="active", country=self.country,
        )
        TeamMembers.objects.create(team=self.team, member=clean, management_role="member")
        # The blacklisted captain cannot register the team, so a vice-captain does it instead
        # (register_for_event only accepts an owner/captain/vice-captain/manager/coach as the
        # actor - a plain member is refused for reasons unrelated to blacklists).
        TeamMembers.objects.filter(team=self.team, member=self.teammate).update(
            management_role="vice_captain"
        )
        # Same team, same event, roster WITHOUT the blacklisted captain -> goes through.
        resp = self._register_team(
            actor=self.teammate, event=self.duo_event, team=self.team,
            roster=[self.teammate, clean],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── SOLO path: the door this feature closes ────────────────────────────────
    def test_player_blacklist_blocks_solo_registration(self):
        self._blacklist_player(self.captain)
        resp = self._register_solo(actor=self.captain, event=self.solo_event)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("blacklisted", resp.json()["message"].lower())
        self.assertFalse(
            RegisteredCompetitors.objects.filter(
                event=self.solo_event, user=self.captain
            ).exists()
        )

    # ── SOLO path: a TEAM-snapshotted player is blocked solo too ───────────────
    # The follows-the-player rule has to hold on both doors, not just the one it was written for.
    def test_team_snapshot_player_blocked_on_solo_registration(self):
        blacklist = OrganizerBlacklist.objects.create(
            organization=self.org, target_type="team", team=self.team,
            reason="Smurfing", end_date=timezone.now() + timedelta(days=30),
            created_by=self.organizer, status="active",
        )
        for uid in TeamMembers.objects.filter(team=self.team).values_list("member_id", flat=True):
            OrganizerBlacklistPlayer.objects.create(blacklist=blacklist, user_id=uid)

        resp = self._register_solo(actor=self.teammate, event=self.solo_event)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("blacklisted", resp.json()["message"].lower())

    # ── SOLO path: a NULL team must not be read as "every team" ────────────────
    # organizer_blacklist_block is called with team=None on the solo path, and a PLAYER-target row
    # also stores team=NULL. A naive filter(team=team) would match it and 403 EVERY solo registrant
    # with "your team is blacklisted". This proves an unrelated player gets PAST the blacklist
    # guard: they stop at the later Discord gate, which is the next check in the view.
    def test_solo_registration_not_blocked_for_unrelated_player(self):
        self._blacklist_player(self.captain)          # somebody ELSE is blacklisted
        resp = self._register_solo(actor=self.outsider, event=self.solo_event)
        body = resp.json().get("message", "").lower()
        self.assertNotIn("blacklisted", body)
        # Registration still does not complete (this fixture user has no Discord connected), but it
        # failed for the RIGHT reason, which is what proves the guard let them through.
        self.assertIn("discord", body)

    # ── a player blacklisted by a DIFFERENT organization is not blocked here ───
    def test_player_blacklist_for_other_org_does_not_block(self):
        self._blacklist_player(self.captain, organization=self.other_org)
        resp = self._register_team(
            actor=self.captain, event=self.duo_event, team=self.team,
            roster=[self.captain, self.teammate],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── a LIFTED player blacklist stops blocking ───────────────────────────────
    def test_lifted_player_blacklist_lets_player_register_again(self):
        blacklist = self._blacklist_player(self.captain)
        # Mirror lift_blacklist: status lifted + the player row deactivated.
        blacklist.status = "lifted"
        blacklist.save(update_fields=["status"])
        blacklist.players.update(is_active=False)

        resp = self._register_team(
            actor=self.captain, event=self.duo_event, team=self.team,
            roster=[self.captain, self.teammate],
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    # ── an EXPIRED player blacklist stops blocking ─────────────────────────────
    # status stays "active" but end_date has lapsed: enforcement must honour expiry live, with no
    # sweep required (same rule as is_currently_active()).
    def test_expired_player_blacklist_does_not_block(self):
        blacklist = self._blacklist_player(self.captain)
        OrganizerBlacklist.objects.filter(pk=blacklist.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        resp = self._register_team(
            actor=self.captain, event=self.duo_event, team=self.team,
            roster=[self.captain, self.teammate],
        )
        self.assertEqual(resp.status_code, 201, resp.content)
