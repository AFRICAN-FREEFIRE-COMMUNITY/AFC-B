# afc_tournament_and_scrims/tests.py
# ──────────────────────────────────────────────────────────────────────────────
# Tests for EVENT DUPLICATION (feature "event-duplicate", 2026-06-10).
#
# Covers the POST events/<event_id>/duplicate-event/ endpoint (views.duplicate_event):
# an organizer or AFC admin clones an existing event's CONFIG + stage/group/round-robin
# STRUCTURE into a fresh draft, WITHOUT carrying over any results, registrations, teams,
# matches, leaderboards, payments, invite tokens, sponsors, or analytics.
#
# Auth in this codebase is a bearer SessionToken (afc_auth.SessionToken) validated by
# afc_auth.views.validate_token, so every test mints a real SessionToken and sends it in
# the Authorization header (no DRF auth classes are wired for these views).
#
# These tests never hit the network: duplication is a pure DB deep-copy in one
# transaction.atomic(); nothing in duplicate_event calls Discord/Stripe/email.
# ──────────────────────────────────────────────────────────────────────────────
import uuid
from datetime import date, time, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserRoles
from afc_organizers.models import Organization, OrganizationMember
from afc_team.models import Team, TeamMembers

from .models import (
    Event,
    Leaderboard,
    Match,
    RegisteredCompetitors,
    SponsorEvent,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMember,
)


class DuplicateEventTests(TestCase):
    """End-to-end tests for the duplicate-event endpoint."""

    # ── shared fixtures ──────────────────────────────────────────────────────
    def _token_for(self, user):
        """Mint a real bearer SessionToken for `user` and return its raw value. uuid4 keeps
        the token unique even when one test authenticates the same user twice (the token
        column is UNIQUE)."""
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}",
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        """Authorization header dict for a user's bearer token."""
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    def _url(self, event_id):
        """Resolve the duplicate-event route by name so a path change can't silently
        break the tests."""
        return reverse("duplicate_event", args=[event_id])

    def setUp(self):
        # An AFC admin (role=admin) — passes _is_event_admin, may duplicate anything.
        self.admin = User.objects.create_user(
            username="afc_admin", email="admin@example.com", password="x", role="admin"
        )
        # A plain player who is NOT an admin and NOT an org member — must be 403'd.
        self.outsider = User.objects.create_user(
            username="outsider", email="out@example.com", password="x", role="player"
        )
        # An organizer who owns an org and may create events for it.
        self.organizer = User.objects.create_user(
            username="organizer", email="org@example.com", password="x", role="player"
        )
        self.org = Organization.objects.create(slug="acme-esports", name="Acme Esports")
        OrganizationMember.objects.create(
            organization=self.org, user=self.organizer, role="owner", status="active"
        )

    def _make_event(self, *, creator, organization=None, with_results=True):
        """Build a SOURCE event with 2 stages, groups under each, a leaderboard + a
        match (results-side rows), plus a registration and a tournament team — exactly
        the kinds of rows the duplicate MUST NOT copy. Stage 1 is a Point-Rush source
        whose carry-over target is Stage 2, so the second-pass target resolution is
        exercised. Returns the Event."""
        today = date.today()
        event = Event.objects.create(
            competition_type="tournament",
            participant_type="squad",
            event_type="internal",
            max_teams_or_players=16,
            event_name="Source Cup",
            event_mode="virtual",
            start_date=today,
            end_date=today + timedelta(days=2),
            registration_open_date=today,
            registration_end_date=today + timedelta(days=1),
            prizepool="$1000",
            prizepool_cash_value=1000,
            prize_distribution={"1": "50%", "2": "30%", "3": "20%"},
            event_rules="No cheating",
            event_status="ongoing",        # source is live — copy must reset to upcoming
            registration_link="https://example.com/reg",
            tournament_tier="tier_1",
            number_of_stages=2,
            creator=creator,
            organization=organization,
            is_draft=False,                # source is published — copy must be a draft
            is_public=True,                # source is public — copy must be private
            rankings_verified=True,        # source verified — copy must reset
            partner_published=True,        # source published to partners — copy must reset
        )

        # ── Stage 1 (Point-Rush source) + a group with a leaderboard + a match ──
        stage1 = Stages.objects.create(
            event=event,
            stage_name="Group Stage",
            start_date=today,
            end_date=today + timedelta(days=1),
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=4,
            point_rush_enabled=True,
            point_rush_reward={"1": 10, "2": 7},
        )
        group1 = StageGroups.objects.create(
            stage=stage1,
            group_name="Group A",
            playing_date=today,
            playing_time=time(19, 0),
            teams_qualifying=4,
            match_count=2,
            match_maps=["bermuda", "purgatory"],
        )
        # Results-side rows that MUST NOT be copied.
        leaderboard = Leaderboard.objects.create(
            leaderboard_name="Group Stage - Group A",
            event=event,
            stage=stage1,
            group=group1,
            creator=creator,
            leaderboard_method="manual",
            placement_points={},
            kill_point=1.0,
        )
        Match.objects.create(
            leaderboard=leaderboard, group=group1, match_map="bermuda", match_number=1
        )

        # ── Stage 2 (the Point-Rush target) + a group ──
        stage2 = Stages.objects.create(
            event=event,
            stage_name="Finals",
            start_date=today + timedelta(days=1),
            end_date=today + timedelta(days=2),
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=1,
            is_finals_stage=True,
        )
        StageGroups.objects.create(
            stage=stage2,
            group_name="Finals Group",
            playing_date=today + timedelta(days=2),
            playing_time=time(20, 0),
            teams_qualifying=1,
            match_count=3,
            match_maps=["bermuda"],
        )

        # Wire Point-Rush carry-over: stage1 -> stage2.
        stage1.point_rush_target_stage = stage2
        stage1.save(update_fields=["point_rush_target_stage"])

        if with_results:
            # A registration + a tournament team on the SOURCE — these must NOT survive
            # the duplicate.
            team = Team.objects.create(
                team_name="Source Team",
                join_settings="open",
                team_creator=creator,
                team_owner=creator,
                team_captain=creator,
                country="Nigeria",
            )
            TournamentTeam.objects.create(
                event=event, team=team, registered_by=creator, status="active"
            )
            RegisteredCompetitors.objects.create(
                event=event, user=creator, status="registered"
            )

        return event

    # ── tests ────────────────────────────────────────────────────────────────
    def test_admin_duplicates_event_copies_structure_not_results(self):
        """An AFC admin clones a 2-stage event: the new event is a draft with the same
        stage/group config and NO matches/registrations/teams/leaderboards."""
        src = self._make_event(creator=self.admin)
        src_stage_count = src.stages.count()
        src_group_count = StageGroups.objects.filter(stage__event=src).count()

        resp = self.client.post(self._url(src.event_id), **self._auth(self.admin))
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        new_id = body["event_id"]
        self.assertNotEqual(new_id, src.event_id)

        new = Event.objects.get(event_id=new_id)

        # Draft + private + reset status flags.
        self.assertTrue(new.is_draft)
        self.assertFalse(new.is_public)
        self.assertEqual(new.event_status, "upcoming")
        self.assertFalse(new.rankings_verified)
        self.assertFalse(new.partner_published)
        self.assertEqual(new.creator, self.admin)

        # Config copied across.
        self.assertEqual(new.competition_type, src.competition_type)
        self.assertEqual(new.max_teams_or_players, src.max_teams_or_players)
        self.assertEqual(new.prize_distribution, src.prize_distribution)
        self.assertEqual(new.tournament_tier, src.tournament_tier)

        # Same stage + group STRUCTURE on the copy.
        self.assertEqual(new.stages.count(), src_stage_count)
        self.assertEqual(
            StageGroups.objects.filter(stage__event=new).count(), src_group_count
        )
        new_stage1 = new.stages.order_by("stage_id").first()
        self.assertEqual(new_stage1.stage_name, "Group Stage")
        self.assertEqual(new_stage1.point_rush_reward, {"1": 10, "2": 7})

        # CRITICAL: results-side rows NOT copied for the new event.
        self.assertEqual(Match.objects.filter(group__stage__event=new).count(), 0)
        self.assertEqual(Leaderboard.objects.filter(event=new).count(), 0)
        self.assertEqual(RegisteredCompetitors.objects.filter(event=new).count(), 0)
        self.assertEqual(TournamentTeam.objects.filter(event=new).count(), 0)

        # The SOURCE event is untouched (its results still exist).
        self.assertEqual(Match.objects.filter(group__stage__event=src).count(), 1)
        self.assertEqual(RegisteredCompetitors.objects.filter(event=src).count(), 1)
        self.assertEqual(TournamentTeam.objects.filter(event=src).count(), 1)

    def test_point_rush_target_resolves_to_new_stage(self):
        """The copied Stage 1 must carry over into the COPIED Stage 2, never the source's
        stage (the second-pass remap)."""
        src = self._make_event(creator=self.admin)
        resp = self.client.post(self._url(src.event_id), **self._auth(self.admin))
        self.assertEqual(resp.status_code, 201, resp.content)
        new = Event.objects.get(event_id=resp.json()["event_id"])

        new_stages = list(new.stages.order_by("stage_id"))
        src_stage_ids = set(src.stages.values_list("stage_id", flat=True))
        new_stage1, new_stage2 = new_stages[0], new_stages[1]

        # Target points at the NEW stage 2, not the old one.
        self.assertEqual(new_stage1.point_rush_target_stage_id, new_stage2.stage_id)
        self.assertNotIn(new_stage1.point_rush_target_stage_id, src_stage_ids)

    def test_new_slug_is_unique(self):
        """Duplicating twice yields two events with distinct, unique slugs."""
        src = self._make_event(creator=self.admin)
        first = self.client.post(self._url(src.event_id), **self._auth(self.admin)).json()
        second = self.client.post(self._url(src.event_id), **self._auth(self.admin)).json()

        a = Event.objects.get(event_id=first["event_id"])
        b = Event.objects.get(event_id=second["event_id"])
        self.assertNotEqual(a.slug, src.slug)
        self.assertNotEqual(a.slug, b.slug)
        # Slug uniqueness invariant holds across the whole table.
        self.assertEqual(Event.objects.filter(slug=a.slug).count(), 1)
        self.assertEqual(Event.objects.filter(slug=b.slug).count(), 1)

    def test_organizer_can_duplicate_own_org_event(self):
        """An org owner with can_create_events may duplicate an event their org owns; the
        copy keeps the same organization."""
        src = self._make_event(creator=self.organizer, organization=self.org)
        resp = self.client.post(self._url(src.event_id), **self._auth(self.organizer))
        self.assertEqual(resp.status_code, 201, resp.content)
        new = Event.objects.get(event_id=resp.json()["event_id"])
        self.assertEqual(new.organization_id, self.org.organization_id)
        self.assertEqual(new.creator, self.organizer)

    def test_outsider_denied(self):
        """A non-admin, non-org user gets 403 and no new event is created."""
        src = self._make_event(creator=self.admin)
        before = Event.objects.count()
        resp = self.client.post(self._url(src.event_id), **self._auth(self.outsider))
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(Event.objects.count(), before)

    def test_organizer_cannot_duplicate_native_afc_event(self):
        """An organizer may NOT duplicate a native AFC event (organization=None) — that
        path is admin-only."""
        src = self._make_event(creator=self.admin, organization=None)
        resp = self.client.post(self._url(src.event_id), **self._auth(self.organizer))
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_missing_event_returns_404(self):
        """Duplicating a non-existent event id is a 404."""
        resp = self.client.post(self._url(999999), **self._auth(self.admin))
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_requires_auth(self):
        """No bearer token → 400/401, never a silent duplicate."""
        src = self._make_event(creator=self.admin)
        resp = self.client.post(self._url(src.event_id))
        self.assertIn(resp.status_code, (400, 401))


# ──────────────────────────────────────────────────────────────────────────────
# Tests for the SPONSOR RE-APPROVAL roster bug (feature "sponsor-edit-roster", 2026-06-10).
#
# THE BUG (production): once a sponsor approved players on a sponsored event, the team
# went "active" and every approved member went "active". If the team THEN edited its
# roster (swapping a player, or changing a kept player's sponsor id), edit_roster
# HARD-REJECTED with 403 ("Cannot remove confirmed player" / "Cannot change sponsor ID
# for confirmed player"). Because the whole edit ran inside ONE transaction.atomic(),
# the 403 rolled back the ENTIRE edit, so the NEW roster never saved and the OLD,
# already-approved roster persisted. The sponsor dashboard
# (get_list_of_players_in_sponsor_event), which reads the live TournamentTeamMember
# rows, then kept showing the stale roster.
#
# THE FIX (owner decision "allow + re-approve changes"): let the edit through. Any
# added/swapped player is "pending"; a kept player whose sponsor id changed resets to
# "pending"; the TEAM drops back to "pending" so the sponsor re-reviews. Already-approved
# players who stay UNCHANGED keep their "active" status. A no-op edit on an all-active
# team keeps the team "active". The team can never silently stay "active" after its
# roster changed. See views.edit_roster + the now-bidirectional check_and_activate_team.
#
# Auth is the same bearer SessionToken pattern as DuplicateEventTests above. These tests
# never hit the network: send_email (called by confirm_player + check_and_activate_team)
# is patched to a no-op, and check_and_activate_team's Discord/celery path is never
# reached because the test fixtures create no Stages (its stage lookup returns None and
# the function returns before any .delay()).
# ──────────────────────────────────────────────────────────────────────────────
@patch("afc_tournament_and_scrims.views.send_email", return_value=True)
class SponsorEditRosterReapprovalTests(TestCase):
    """edit_roster on a sponsored event must ALLOW post-approval edits and reopen the
    team for sponsor re-review, instead of 403-rolling-back the whole edit."""

    # ── auth helpers (mirror DuplicateEventTests) ────────────────────────────
    def _token_for(self, user):
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}",
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    # ── fixtures ─────────────────────────────────────────────────────────────
    def setUp(self):
        today = date.today()

        # Sponsor admin (role=admin + sponsor_admin UserRole) — the only identity the
        # sponsor dashboard (get_list_of_players_in_sponsor_event) accepts.
        self.sponsor = User.objects.create_user(
            username="sponsor", email="sponsor@example.com", password="x", role="admin"
        )
        self.sponsor_role = Roles.objects.create(
            role_name="sponsor_admin", description="Sponsor admin"
        )
        UserRoles.objects.create(user=self.sponsor, role=self.sponsor_role)

        # AFC event admin (role=admin) — passes _is_event_admin, may confirm/reject players.
        self.admin = User.objects.create_user(
            username="afc_admin2", email="admin2@example.com", password="x", role="admin"
        )

        # Team owner (captain/owner) — the identity that edits the roster.
        self.owner = User.objects.create_user(
            username="owner", email="owner@example.com", password="x",
            role="player", country="Nigeria",
        )

        # Four starting players A,B,C,D + one bench player E for the swap.
        self.pA = User.objects.create_user(username="pA", email="a@example.com", password="x", country="Nigeria")
        self.pB = User.objects.create_user(username="pB", email="b@example.com", password="x", country="Nigeria")
        self.pC = User.objects.create_user(username="pC", email="c@example.com", password="x", country="Nigeria")
        self.pD = User.objects.create_user(username="pD", email="d@example.com", password="x", country="Nigeria")
        self.pE = User.objects.create_user(username="pE", email="e@example.com", password="x", country="Nigeria")

        # A SPONSORED squad event with an open registration window (so edit_roster's
        # window + match-start guards both pass).
        self.event = Event.objects.create(
            competition_type="tournament",
            participant_type="squad",
            event_type="internal",
            max_teams_or_players=16,
            event_name="Sponsored Cup",
            event_mode="virtual",
            start_date=today + timedelta(days=3),
            end_date=today + timedelta(days=5),
            registration_open_date=today - timedelta(days=1),
            registration_end_date=today + timedelta(days=2),
            prizepool="$500",
            prizepool_cash_value=500,
            prize_distribution={"1": "100%"},
            event_rules="No cheating",
            event_status="upcoming",
            registration_link="https://example.com/reg",
            number_of_stages=1,
            creator=self.admin,
            is_sponsored=True,
        )
        # Wire the sponsor to this event so the dashboard read returns its roster.
        SponsorEvent.objects.create(sponsor=self.sponsor, event=self.event)

        # The team + its memberships. The owner is owner/captain; A..E are members.
        self.team = Team.objects.create(
            team_name="Roster Team",
            join_settings="open",
            team_creator=self.owner,
            team_owner=self.owner,
            team_captain=self.owner,
            country="Nigeria",
        )
        for u in (self.pA, self.pB, self.pC, self.pD, self.pE):
            TeamMembers.objects.create(team=self.team, member=u, management_role="member")

        # ── Reproduce the post-register state for a SPONSORED team event EXACTLY as
        # register_for_event leaves it (views.py ~L5170-5206): TournamentTeam "pending",
        # one RegisteredCompetitors row "registered", and a "pending" TournamentTeamMember
        # per roster player carrying its sponsor id. Building this directly keeps the test
        # on the approval state machine + edit_roster without re-driving the heavy
        # register flow (invite tokens, Stages, Discord) which is out of scope here.
        self.tt = TournamentTeam.objects.create(
            event=self.event, team=self.team, registered_by=self.owner,
            status="pending", country="Nigeria",
        )
        self.rc = RegisteredCompetitors.objects.create(
            event=self.event, team=self.team, status="registered",
        )
        self.members = {}
        for u, sponsor_uid in ((self.pA, "SID-A"), (self.pB, "SID-B"), (self.pC, "SID-C"), (self.pD, "SID-D")):
            self.members[u.username] = TournamentTeamMember.objects.create(
                tournament_team=self.tt, user=u, event=self.event,
                user_id_from_sponsor=sponsor_uid, status="pending",
            )

    # ── small drivers over the REAL endpoints under test ─────────────────────
    def _confirm(self, member):
        """Approve a member through the real confirm_player endpoint (admin auth)."""
        return self.client.post(
            reverse("confirm_player"),
            data={"member_id": member.id},
            content_type="application/json",
            **self._auth(self.admin),
        )

    def _edit_roster(self, roster_member_ids, sponsor_ids, editor=None):
        """Hit the real edit_roster endpoint. Defaults to editing as the team owner;
        pass `editor` to edit as a different identity (e.g. an AFC admin or a random
        user) for the staff-override tests."""
        return self.client.post(
            reverse("edit_roster"),
            data={
                "event_id": self.event.event_id,
                "team_id": self.team.team_id,
                "roster_member_ids": roster_member_ids,
                "sponsor_ids": sponsor_ids,
            },
            content_type="application/json",
            **self._auth(editor or self.owner),
        )

    def _close_registration(self):
        """Move the event's registration window into the past so the registration-closed
        guard fires (date.today() > registration_end_date). Used by the staff-override
        tests: a manager may still edit after this point, a normal captain/owner may not."""
        today = date.today()
        self.event.registration_open_date = today - timedelta(days=10)
        self.event.registration_end_date = today - timedelta(days=1)
        self.event.save(update_fields=["registration_open_date", "registration_end_date"])

    def _approve_all_four(self):
        """Approve A,B,C,D -> last confirm flips the whole team to active."""
        for username in ("pA", "pB", "pC", "pD"):
            resp = self._confirm(self.members[username])
            self.assertEqual(resp.status_code, 200, resp.content)
        self.tt.refresh_from_db()
        self.assertEqual(self.tt.status, "active")

    def _sponsor_dashboard_rows(self):
        """The live roster the sponsor sees, scoped to this event/team. The endpoint is
        registered as POST (views.get_list_of_players_in_sponsor_event uses
        @api_view(["POST"]))."""
        resp = self.client.post(
            reverse("get_list_of_players_in_sponsor_event"),
            **self._auth(self.sponsor),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return [
            row for row in resp.json()
            if row.get("team_id") == self.team.team_id
            and row.get("event_id") == self.event.event_id
        ]

    # ── THE CORE REPRO ───────────────────────────────────────────────────────
    def test_swap_player_after_approval_saves_and_reopens_team(self, _send_email):
        """Register A,B,C,D (pending) -> approve all (team active) -> swap D for E.
        Before the fix this returned 403 and rolled back, leaving the OLD roster. After
        the fix: 200, D gone, E present+pending, A/B/C still active, team back to
        pending, and the sponsor dashboard shows the NEW roster (A,B,C,E)."""
        self._approve_all_four()

        # Swap D -> E. Keep A,B,C with their original sponsor ids; E gets a fresh id.
        resp = self._edit_roster(
            roster_member_ids=[self.pA.user_id, self.pB.user_id, self.pC.user_id, self.pE.user_id],
            sponsor_ids={
                str(self.pA.user_id): "SID-A",
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C",
                str(self.pE.user_id): "SID-E",
            },
        )

        # 200, NOT the old 403 — the swapped roster actually saved.
        self.assertEqual(resp.status_code, 200, resp.content)

        # D's member row is gone; E is present and PENDING.
        self.assertFalse(
            TournamentTeamMember.objects.filter(tournament_team=self.tt, user=self.pD).exists()
        )
        e_member = TournamentTeamMember.objects.get(tournament_team=self.tt, user=self.pE)
        self.assertEqual(e_member.status, "pending")

        # A/B/C kept their approval (unchanged kept players stay active).
        for username in ("pA", "pB", "pC"):
            self.members[username].refresh_from_db()
            self.assertEqual(self.members[username].status, "active")

        # The TEAM reopened for sponsor re-review, and its RC is no longer "registered".
        self.tt.refresh_from_db()
        self.rc.refresh_from_db()
        self.assertEqual(self.tt.status, "pending")
        self.assertEqual(self.rc.status, "pending")

        # The sponsor dashboard now shows the NEW roster A,B,C,E (not the stale D).
        usernames = {row["member_username"] for row in self._sponsor_dashboard_rows()}
        self.assertEqual(usernames, {"pA", "pB", "pC", "pE"})

        # Approving E returns the team to active.
        resp = self._confirm(e_member)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.tt.refresh_from_db()
        self.rc.refresh_from_db()
        self.assertEqual(self.tt.status, "active")
        self.assertEqual(self.rc.status, "registered")

    def test_changing_kept_active_player_sponsor_id_resets_to_pending(self, _send_email):
        """Editing a kept ACTIVE player's sponsor id must ALLOW it, reset THAT member to
        pending (the new id needs re-approval), and reopen the team. Before the fix this
        was a hard 403."""
        self._approve_all_four()

        # Same four players, but C's sponsor id changes from SID-C to SID-C2.
        resp = self._edit_roster(
            roster_member_ids=[self.pA.user_id, self.pB.user_id, self.pC.user_id, self.pD.user_id],
            sponsor_ids={
                str(self.pA.user_id): "SID-A",
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C2",
                str(self.pD.user_id): "SID-D",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # C reset to pending with the NEW sponsor id; A/B/D untouched (still active).
        self.members["pC"].refresh_from_db()
        self.assertEqual(self.members["pC"].status, "pending")
        self.assertEqual(self.members["pC"].user_id_from_sponsor, "SID-C2")
        for username in ("pA", "pB", "pD"):
            self.members[username].refresh_from_db()
            self.assertEqual(self.members[username].status, "active")

        # Team reopened for re-review.
        self.tt.refresh_from_db()
        self.rc.refresh_from_db()
        self.assertEqual(self.tt.status, "pending")
        self.assertEqual(self.rc.status, "pending")

    def test_noop_edit_keeps_team_active(self, _send_email):
        """A no-op edit (identical roster + identical sponsor ids) on an all-active team
        must KEEP the team active — re-derivation must not gratuitously downgrade it."""
        self._approve_all_four()

        resp = self._edit_roster(
            roster_member_ids=[self.pA.user_id, self.pB.user_id, self.pC.user_id, self.pD.user_id],
            sponsor_ids={
                str(self.pA.user_id): "SID-A",
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C",
                str(self.pD.user_id): "SID-D",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # All four members still active; team + RC stay in the approved state.
        for username in ("pA", "pB", "pC", "pD"):
            self.members[username].refresh_from_db()
            self.assertEqual(self.members[username].status, "active")
        self.tt.refresh_from_db()
        self.rc.refresh_from_db()
        self.assertEqual(self.tt.status, "active")
        self.assertEqual(self.rc.status, "registered")

    def test_remove_approved_player_is_allowed(self, _send_email):
        """Dropping below an all-active roster by removing an APPROVED player must be
        allowed (no 403) and reopen the team. (Roster floor is 4, so we drop A and add E,
        i.e. a remove+add that nets to 4 but specifically removes an APPROVED player.)"""
        self._approve_all_four()

        # Remove approved A, add pending E -> roster B,C,D,E.
        resp = self._edit_roster(
            roster_member_ids=[self.pB.user_id, self.pC.user_id, self.pD.user_id, self.pE.user_id],
            sponsor_ids={
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C",
                str(self.pD.user_id): "SID-D",
                str(self.pE.user_id): "SID-E",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # A (was active) is gone; E is present + pending; B/C/D still active.
        self.assertFalse(
            TournamentTeamMember.objects.filter(tournament_team=self.tt, user=self.pA).exists()
        )
        self.assertTrue(
            TournamentTeamMember.objects.filter(
                tournament_team=self.tt, user=self.pE, status="pending"
            ).exists()
        )
        for username in ("pB", "pC", "pD"):
            self.members[username].refresh_from_db()
            self.assertEqual(self.members[username].status, "active")

        self.tt.refresh_from_db()
        self.assertEqual(self.tt.status, "pending")

    def test_confirm_player_still_activates_team_when_all_active(self, _send_email):
        """Guard test for the check_and_activate_team change: the ORIGINAL all-active
        behavior of confirm_player must be unchanged — the last confirm still flips the
        team to active and its RC to registered."""
        # Team starts pending; RC starts registered (as register leaves it).
        self.assertEqual(self.tt.status, "pending")

        for username in ("pA", "pB", "pC"):
            self._confirm(self.members[username])
        # Not all active yet -> team must still be pending.
        self.tt.refresh_from_db()
        self.assertEqual(self.tt.status, "pending")

        # Final confirm -> team active + RC registered.
        self._confirm(self.members["pD"])
        self.tt.refresh_from_db()
        self.rc.refresh_from_db()
        self.assertEqual(self.tt.status, "active")
        self.assertEqual(self.rc.status, "registered")

    # ── STAFF OVERRIDE: edit roster AFTER registration closes ────────────────
    # Feature "staff-edit-roster-after-close" (2026-06-10): an AFC admin or an organizer
    # with can_manage_registrations must be able to correct a team's roster even after
    # registration closes, while a normal captain/owner stays blocked after close. The
    # match-start lock is NOT bypassed for anyone. Re-approval re-derivation still applies.

    def test_admin_edits_roster_after_close_saves_and_reopens_team(self, _send_email):
        """An AFC admin swaps a player AFTER registration_end_date has passed: the edit
        must save (200, swap persists) AND reopen the team for sponsor re-review (status
        pending). This is the staff-correction path the override exists for."""
        self._approve_all_four()      # team active before the edit
        self._close_registration()    # registration window is now in the past

        # Admin (NOT on the team) swaps D -> E after close.
        resp = self._edit_roster(
            roster_member_ids=[self.pA.user_id, self.pB.user_id, self.pC.user_id, self.pE.user_id],
            sponsor_ids={
                str(self.pA.user_id): "SID-A",
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C",
                str(self.pE.user_id): "SID-E",
            },
            editor=self.admin,
        )

        # 200 despite the closed window (manager override), and the swap saved.
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(
            TournamentTeamMember.objects.filter(tournament_team=self.tt, user=self.pD).exists()
        )
        self.assertTrue(
            TournamentTeamMember.objects.filter(
                tournament_team=self.tt, user=self.pE, status="pending"
            ).exists()
        )

        # The re-approval re-derivation still fired: team reopened for sponsor review.
        self.tt.refresh_from_db()
        self.rc.refresh_from_db()
        self.assertEqual(self.tt.status, "pending")
        self.assertEqual(self.rc.status, "pending")

    def test_captain_edit_after_close_still_blocked(self, _send_email):
        """A normal captain/owner editing AFTER registration closes is still 403
        'Registration closed' — the override is staff-only, not a general bypass."""
        self._close_registration()

        resp = self._edit_roster(
            roster_member_ids=[self.pA.user_id, self.pB.user_id, self.pC.user_id, self.pE.user_id],
            sponsor_ids={
                str(self.pA.user_id): "SID-A",
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C",
                str(self.pE.user_id): "SID-E",
            },
            # default editor = self.owner (the team owner/captain)
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("Registration closed", resp.json()["message"])

        # The edit rolled back: D is still on the roster, E never joined.
        self.assertTrue(
            TournamentTeamMember.objects.filter(tournament_team=self.tt, user=self.pD).exists()
        )
        self.assertFalse(
            TournamentTeamMember.objects.filter(tournament_team=self.tt, user=self.pE).exists()
        )

    def test_random_user_edit_still_blocked_permission(self, _send_email):
        """A non-manager, non-captain active user editing (window still OPEN) is still 403
        on the permission guard — the override only lifts the gate for managers."""
        outsider = User.objects.create_user(
            username="outsider_roster", email="outr@example.com", password="x",
            role="player", country="Nigeria",
        )
        resp = self._edit_roster(
            roster_member_ids=[self.pA.user_id, self.pB.user_id, self.pC.user_id, self.pE.user_id],
            sponsor_ids={
                str(self.pA.user_id): "SID-A",
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C",
                str(self.pE.user_id): "SID-E",
            },
            editor=outsider,
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("captain/owner", resp.json()["message"])

    def test_manager_edit_blocked_when_matches_have_results(self, _send_email):
        """The match-start lock is NOT bypassed by the manager override: once any match
        in the event has result_inputted=True, even an AFC admin gets 403, because
        editing the roster after results exist would orphan match stats."""
        # Build a stage + group + a match that already has results.
        today = date.today()
        stage = Stages.objects.create(
            event=self.event,
            stage_name="Group Stage",
            start_date=today,
            end_date=today + timedelta(days=1),
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage,
            group_name="Group A",
            playing_date=today,
            playing_time=time(19, 0),
            teams_qualifying=1,
            match_count=1,
        )
        Match.objects.create(
            group=group, match_number=1, match_map="bermuda", result_inputted=True
        )

        # Admin tries to swap a player while results exist -> still blocked.
        resp = self._edit_roster(
            roster_member_ids=[self.pA.user_id, self.pB.user_id, self.pC.user_id, self.pE.user_id],
            sponsor_ids={
                str(self.pA.user_id): "SID-A",
                str(self.pB.user_id): "SID-B",
                str(self.pC.user_id): "SID-C",
                str(self.pE.user_id): "SID-E",
            },
            editor=self.admin,
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("matches have started", resp.json()["message"])

        # The roster is unchanged: D stayed, E never joined.
        self.assertTrue(
            TournamentTeamMember.objects.filter(tournament_team=self.tt, user=self.pD).exists()
        )
        self.assertFalse(
            TournamentTeamMember.objects.filter(tournament_team=self.tt, user=self.pE).exists()
        )
