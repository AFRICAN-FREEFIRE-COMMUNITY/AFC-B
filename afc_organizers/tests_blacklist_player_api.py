# afc_organizers/tests_blacklist_player_api.py
# ──────────────────────────────────────────────────────────────────────────────
# API tests for PLAYER-TARGET organizer blacklists (owner backlog item 1, 2026-08-03).
#
# Covers the create/list/lift/lift-request endpoints in views_blacklist.py for the NEW
# target_type="player" shape, which reuses the same OrganizerBlacklist model with team=NULL and
# exactly one OrganizerBlacklistPlayer row:
#   - create with target_type="player" stores team=NULL and one player row;
#   - the same permission gate applies (a sub-organizer without can_manage_registrations is 403'd);
#   - creating the same block twice is a 409 rather than two overlapping blocks to unpick;
#   - the LIFECYCLE matches the team version: the organizer can lift early, and the affected
#     PLAYER can raise a player-scope lift request which the organizer approves;
#   - a team-scope lift request against a player-target blacklist is a clean 400, not a crash on
#     the NULL team;
#   - the affected player DISCOVERS their own block through blacklists/mine/ even with no team.
#
# The REGISTRATION ENFORCEMENT tests (that the block actually stops a registration, on both the
# team and solo paths) live in afc_tournament_and_scrims/tests_blacklist_player.py.
#
# Auth is a real bearer SessionToken (afc_auth.SessionToken) validated by
# afc_auth.views.validate_token, matching the sibling tests_blacklist.py. Nothing hits the network.
# ──────────────────────────────────────────────────────────────────────────────
import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import (
    Organization,
    OrganizationMember,
    OrganizerBlacklist,
    OrganizerBlacklistPlayer,
    BlacklistLiftRequest,
)


class PlayerTargetBlacklistApiTests(TestCase):
    # ── shared fixtures ──────────────────────────────────────────────────────
    def _token_for(self, user):
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    def setUp(self):
        self.organizer = User.objects.create_user(
            username="organizer", email="org@x.com", password="x", role="player"
        )
        # A sub-organizer WITHOUT can_manage_registrations -> must be 403'd on create.
        self.weak_sub = User.objects.create_user(
            username="weaksub", email="weak@x.com", password="x", role="player"
        )
        self.org = Organization.objects.create(slug="acme", name="Acme Esports")
        OrganizationMember.objects.create(
            organization=self.org, user=self.organizer, role="owner", status="active"
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.weak_sub, role="sub_organizer", status="active",
            can_manage_registrations=False,
        )

        # The person being blacklisted. Deliberately NOT on any team for most tests: the whole
        # point of the feature is that a player can be blacklisted without a team being involved.
        self.target = User.objects.create_user(
            username="target_player", email="tp@x.com", password="x", role="player"
        )
        self.bystander = User.objects.create_user(
            username="bystander", email="by@x.com", password="x", role="player"
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _create_player_blacklist(self, actor, user=None, end_date=None):
        """POST a player-target create: target_type="player" + user_id, no team_id."""
        today = timezone.now().date()
        body = {
            "organization_id": self.org.pk,
            "target_type": "player",
            "user_id": (user or self.target).user_id,
            "start_date": today.isoformat(),
            "end_date": (end_date or (today + timedelta(days=30)).isoformat()),
            "reason": "Toxic behaviour in chat",
        }
        return self.client.post(
            reverse("organizers_blacklists"),
            data=body,
            content_type="application/json",
            **self._auth(actor),
        )

    # ── §1 create stores team=NULL + exactly one player row ───────────────────
    def test_create_player_blacklist(self):
        resp = self._create_player_blacklist(self.organizer)
        self.assertEqual(resp.status_code, 201, resp.content)

        blacklist = OrganizerBlacklist.objects.get(organization=self.org, target_type="player")
        self.assertIsNone(blacklist.team_id)          # no team is implicated
        self.assertEqual(blacklist.status, "active")
        self.assertTrue(blacklist.is_currently_active())
        self.assertEqual(blacklist.reason, "Toxic behaviour in chat")

        rows = list(OrganizerBlacklistPlayer.objects.filter(blacklist=blacklist))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_id, self.target.user_id)
        self.assertTrue(rows[0].is_active)

        # The serialized payload tells the FE what shape this is without re-deriving it.
        body = resp.json()["blacklist"]
        self.assertEqual(body["target_type"], "player")
        self.assertEqual(body["target_username"], "target_player")
        self.assertIsNone(body["team_id"])

    # ── §1b the SAME permission gate as the team version ──────────────────────
    def test_create_player_blacklist_requires_can_manage_registrations(self):
        resp = self._create_player_blacklist(self.weak_sub)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(OrganizerBlacklist.objects.filter(target_type="player").exists())

    # ── §1c user_id is required on the player path ────────────────────────────
    def test_create_player_blacklist_requires_user_id(self):
        today = timezone.now().date()
        resp = self.client.post(
            reverse("organizers_blacklists"),
            data={
                "organization_id": self.org.pk,
                "target_type": "player",
                "end_date": (today + timedelta(days=30)).isoformat(),
                # user_id omitted -> 400
            },
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("user_id", resp.json()["message"])

    # ── §1d double-blacklisting the same person is a 409, not two blocks ──────
    def test_duplicate_active_player_blacklist_is_conflict(self):
        self.assertEqual(self._create_player_blacklist(self.organizer).status_code, 201)
        resp = self._create_player_blacklist(self.organizer)
        self.assertEqual(resp.status_code, 409, resp.content)
        # Still exactly one block to reason about (and to lift).
        self.assertEqual(
            OrganizerBlacklist.objects.filter(organization=self.org, target_type="player").count(),
            1,
        )

    # ── §1e the team path is untouched by the new branch ──────────────────────
    # A create with no target_type still means "team", so every existing caller is unchanged.
    def test_default_target_type_is_team(self):
        captain = User.objects.create_user(
            username="captain", email="cap@x.com", password="x", role="player"
        )
        team = Team.objects.create(
            team_name="Team Alpha", join_settings="open",
            team_creator=captain, team_owner=captain,
        )
        TeamMembers.objects.create(team=team, member=captain, management_role="team_captain")
        today = timezone.now().date()
        resp = self.client.post(
            reverse("organizers_blacklists"),
            data={
                "organization_id": self.org.pk,
                "team_id": team.team_id,          # no target_type sent
                "end_date": (today + timedelta(days=30)).isoformat(),
            },
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        blacklist = OrganizerBlacklist.objects.get(team=team)
        self.assertEqual(blacklist.target_type, "team")
        self.assertEqual(blacklist.players.count(), 1)   # the captain, snapshotted

    # ── §2 the organizer can lift a player blacklist early ────────────────────
    def test_organizer_lifts_player_blacklist(self):
        self._create_player_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(target_type="player")

        resp = self.client.post(
            reverse("organizers_blacklist_lift", args=[blacklist.pk]),
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        blacklist.refresh_from_db()
        self.assertEqual(blacklist.status, "lifted")
        self.assertFalse(blacklist.players.filter(is_active=True).exists())

    # ── §3 the affected player DISCOVERS their own block (no team needed) ─────
    # blacklists/mine/ is the affected party's view. A blacklisted player with no team must still
    # find their block, otherwise they could never request a lift.
    def test_player_sees_own_blacklist_in_mine(self):
        self._create_player_blacklist(self.organizer)
        resp = self.client.get(
            reverse("organizers_blacklists_mine"), **self._auth(self.target)
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row["target_type"], "player")
        self.assertIsNone(row["team_id"])
        # They can ask for themselves, but there is no team lift to ask for.
        self.assertTrue(row["can_request_self_lift"])
        self.assertFalse(row["can_request_team_lift"])

        # A bystander sees nothing.
        other = self.client.get(
            reverse("organizers_blacklists_mine"), **self._auth(self.bystander)
        )
        self.assertEqual(other.json()["total_count"], 0)

    # ── §4 player-scope lift request -> approve unblocks them and lifts the row ─
    def test_player_lift_request_approved_unblocks(self):
        self._create_player_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(target_type="player")

        # The player asks for themselves.
        req_resp = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.pk]),
            data={"scope": "player", "target_user_id": self.target.user_id,
                  "reason": "I have apologised"},
            content_type="application/json",
            **self._auth(self.target),
        )
        self.assertEqual(req_resp.status_code, 201, req_resp.content)
        lift_request = BlacklistLiftRequest.objects.get(blacklist=blacklist)
        self.assertEqual(lift_request.scope, "player")

        # The organizer approves.
        decide = self.client.post(
            reverse("organizers_blacklist_decide_lift", args=[lift_request.pk]),
            data={"decision": "approve"},
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(decide.status_code, 200, decide.content)

        blacklist.refresh_from_db()
        # Their row is retired, and since it was the ONLY player row the blacklist lifts itself.
        self.assertFalse(blacklist.players.filter(is_active=True).exists())
        self.assertEqual(blacklist.status, "lifted")

    # ── §4b a TEAM-scope request against a player blacklist is a clean 400 ────
    # There is no team on this row, so the request is meaningless. It must be refused with a clear
    # message rather than crashing on the NULL team.
    def test_team_scope_lift_request_rejected_on_player_blacklist(self):
        self._create_player_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(target_type="player")

        resp = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.pk]),
            data={"scope": "team"},
            content_type="application/json",
            **self._auth(self.target),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("player", resp.json()["message"].lower())
        self.assertFalse(BlacklistLiftRequest.objects.exists())

    # ── §4c a bystander cannot request a lift for someone else ───────────────
    def test_bystander_cannot_request_lift_for_target(self):
        self._create_player_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(target_type="player")

        resp = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.pk]),
            data={"scope": "player", "target_user_id": self.target.user_id},
            content_type="application/json",
            **self._auth(self.bystander),
        )
        self.assertEqual(resp.status_code, 403, resp.content)

    # ── §5 the organizer list shows the player row with its target ────────────
    def test_list_includes_player_target(self):
        self._create_player_blacklist(self.organizer)
        resp = self.client.get(
            reverse("organizers_blacklists"),
            {"organization_id": self.org.pk},
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["target_type"], "player")
        self.assertEqual(results[0]["target_username"], "target_player")
        self.assertIsNone(results[0]["team_name"])
