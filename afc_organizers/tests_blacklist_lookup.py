# afc_organizers/tests_blacklist_lookup.py
# ──────────────────────────────────────────────────────────────────────────────
# Tests for BLACKLIST VISIBILITY (owner ask 2026-06-12) - views_blacklist_lookup.py.
#
# Covers the two new read surfaces:
#   - GET organizers/blacklist-lookup/  (organizer cross-org lookup of one team / one player)
#       * an organizer (active org member of a DIFFERENT org) sees counts + orgs + dates
#         but NOT the reasons (the owner privacy rule);
#       * a platform admin gets the same entries WITH reasons;
#       * the start/end date window filters on each blacklist's start_date;
#       * a player lookup counts their snapshot rows (follows-the-player), including after
#         they leave the team, and a never-snapshotted player counts zero;
#       * a plain player (no org membership) is 403'd.
#   - GET organizers/admin/blacklists/  (the AFC dashboard feed)
#       * platform-admin only (an organizer is 403'd);
#       * rows carry reasons + effective status; aggregates count totals/actives/top lists.
#
# Mirrors the sibling tests_blacklist.py idiom: auth is a real bearer SessionToken
# (afc_auth.SessionToken) validated by afc_auth.views.validate_token, minted per request via
# _token_for/_auth (no DRF auth classes on these function-based views). Blacklist fixtures are
# created at the MODEL level with explicit start/end dates so the window assertions are
# deterministic. Nothing here touches the network.
# ──────────────────────────────────────────────────────────────────────────────
import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserRoles
from afc_team.models import Team, TeamMembers

from .models import (
    Organization,
    OrganizationMember,
    OrganizerBlacklist,
    OrganizerBlacklistPlayer,
)


class BlacklistLookupTestBase(TestCase):
    """Shared fixtures: two orgs (A blacklists; B's owner is the "other organizer" who looks
    things up), an AFC platform admin, a plain player, and Team Alpha with three members."""

    # ── auth helpers (same shape as tests_blacklist.py) ───────────────────────
    def _token_for(self, user):
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}",
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    def setUp(self):
        now = timezone.now()

        # Org A: the org that DOES the blacklisting.
        self.owner_a = User.objects.create_user(
            username="owner_a", email="a@x.com", password="x", role="player"
        )
        self.org_a = Organization.objects.create(slug="org-a", name="Org Alpha Events")
        OrganizationMember.objects.create(
            organization=self.org_a, user=self.owner_a, role="owner", status="active"
        )

        # Org B: a DIFFERENT org whose owner performs the cross-org lookups.
        self.owner_b = User.objects.create_user(
            username="owner_b", email="b@x.com", password="x", role="player"
        )
        self.org_b = Organization.objects.create(slug="org-b", name="Org Bravo Events")
        OrganizationMember.objects.create(
            organization=self.org_b, user=self.owner_b, role="owner", status="active"
        )

        # AFC platform admin (role="admin" + the organizer_admin granular role -> passes
        # is_platform_org_admin, the same fixture shape afc_organizers/tests.py uses).
        admin_role, _ = Roles.objects.get_or_create(role_name="organizer_admin")
        self.afc_admin = User.objects.create_user(
            username="afcadmin", email="admin@x.com", password="x", role="admin"
        )
        UserRoles.objects.create(user=self.afc_admin, role=admin_role)

        # A plain player with NO org membership - must be locked out of the lookup.
        self.plain_player = User.objects.create_user(
            username="plainplayer", email="pp@x.com", password="x", role="player"
        )

        # Team Alpha: captain + two members (the snapshot fodder).
        self.captain = User.objects.create_user(
            username="captain", email="cap@x.com", password="x", role="player", country="Nigeria"
        )
        self.player_a = User.objects.create_user(
            username="player_a", email="pa@x.com", password="x", role="player", country="Nigeria"
        )
        self.player_b = User.objects.create_user(
            username="player_b", email="pb@x.com", password="x", role="player", country="Nigeria"
        )
        self.team = Team.objects.create(
            team_name="Team Alpha", join_settings="open",
            team_creator=self.captain, team_owner=self.captain, country="Nigeria",
        )
        TeamMembers.objects.create(team=self.team, member=self.captain,
                                   management_role="team_captain")
        TeamMembers.objects.create(team=self.team, member=self.player_a,
                                   management_role="member")
        TeamMembers.objects.create(team=self.team, member=self.player_b,
                                   management_role="member")

        # ── two blacklists of Team Alpha by Org A, at known points in time ──
        # OLD: started 100 days ago, expired 70 days ago (status left "active" so the views'
        # live-expiry handling is what flips its effective status to "expired").
        self.bl_old = OrganizerBlacklist.objects.create(
            organization=self.org_a, team=self.team, reason="Old offence",
            start_date=now - timedelta(days=100), end_date=now - timedelta(days=70),
            created_by=self.owner_a, status="active",
        )
        # CURRENT: started yesterday, runs 30 more days - blocking right now.
        self.bl_current = OrganizerBlacklist.objects.create(
            organization=self.org_a, team=self.team, reason="Current offence",
            start_date=now - timedelta(days=1), end_date=now + timedelta(days=30),
            created_by=self.owner_a, status="active",
        )
        # Snapshot the roster under BOTH (mirrors what the create endpoint does).
        for bl in (self.bl_old, self.bl_current):
            for uid in TeamMembers.objects.filter(team=self.team).values_list(
                "member_id", flat=True
            ):
                OrganizerBlacklistPlayer.objects.create(blacklist=bl, user_id=uid)

    # ── request helpers ───────────────────────────────────────────────────────
    def _lookup(self, user, **query):
        return self.client.get(
            reverse("organizers_blacklist_lookup"), query, **self._auth(user)
        )

    def _dashboard(self, user, **query):
        return self.client.get(
            reverse("organizers_admin_blacklists"), query, **self._auth(user)
        )


class BlacklistLookupApiTests(BlacklistLookupTestBase):
    """GET organizers/blacklist-lookup/ - the organizer cross-org lookup."""

    # ── §1 PRIVACY: another org's organizer sees counts but NOT reasons ────────
    def test_organizer_sees_counts_but_not_reasons(self):
        resp = self._lookup(self.owner_b, team_id=self.team.team_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        # Both blacklists counted, only the current one active.
        self.assertEqual(body["total_count"], 2)
        self.assertEqual(body["active_count"], 1)
        self.assertEqual(body["target"]["type"], "team")
        self.assertEqual(body["target"]["team_name"], "Team Alpha")
        self.assertEqual(len(body["entries"]), 2)
        for entry in body["entries"]:
            # WHO blacklisted (org identity) + WHEN (dates) are visible...
            self.assertEqual(entry["organization_name"], "Org Alpha Events")
            self.assertIn("start_date", entry)
            self.assertIn("end_date", entry)
            # ...but WHY is not: no reason key at all for organizers (owner rule).
            self.assertNotIn("reason", entry)
        # Newest first: the current blacklist leads, and its effective statuses are live
        # (the lapsed "active" row reads "expired" without any sweep).
        self.assertEqual(body["entries"][0]["status"], "active")
        self.assertEqual(body["entries"][1]["status"], "expired")

    # ── §2 a platform admin gets the same entries WITH reasons ─────────────────
    def test_admin_sees_reasons(self):
        resp = self._lookup(self.afc_admin, team_id=self.team.team_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 2)
        reasons = {entry["reason"] for entry in body["entries"]}
        self.assertEqual(reasons, {"Old offence", "Current offence"})

    # ── §3 the date window filters on start_date ───────────────────────────────
    def test_time_window_filters_on_start_date(self):
        today = timezone.now().date()
        # Window covering only the last week -> only the CURRENT blacklist (started yesterday).
        resp = self._lookup(
            self.owner_b, team_id=self.team.team_id,
            start=(today - timedelta(days=7)).isoformat(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["entries"][0]["status"], "active")

        # Window covering only ~110..50 days ago -> only the OLD blacklist.
        resp = self._lookup(
            self.owner_b, team_id=self.team.team_id,
            start=(today - timedelta(days=110)).isoformat(),
            end=(today - timedelta(days=50)).isoformat(),
        )
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["entries"][0]["status"], "expired")

        # No bounds = all time (both rows).
        resp = self._lookup(self.owner_b, team_id=self.team.team_id)
        self.assertEqual(resp.json()["total_count"], 2)

    # ── §3b a malformed window date is a 400, not a silent all-time query ──────
    def test_malformed_window_date_rejected(self):
        resp = self._lookup(self.owner_b, team_id=self.team.team_id, start="not-a-date")
        self.assertEqual(resp.status_code, 400, resp.content)

    # ── §4 PLAYER lookup counts snapshot rows - even after leaving the team ────
    def test_player_lookup_counts_snapshot_rows_after_leaving_team(self):
        # player_a leaves Team Alpha entirely - the snapshot rows still bind them.
        TeamMembers.objects.filter(team=self.team, member=self.player_a).delete()

        resp = self._lookup(self.owner_b, user_id=self.player_a.user_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["target"]["type"], "player")
        self.assertEqual(body["target"]["username"], "player_a")
        # Snapshotted under both blacklists -> counted twice; only the current one blocks.
        self.assertEqual(body["total_count"], 2)
        self.assertEqual(body["active_count"], 1)
        # The team they were snapshotted on rides along for context.
        self.assertEqual(body["entries"][0]["team_name"], "Team Alpha")
        # Privacy holds on player lookups too.
        for entry in body["entries"]:
            self.assertNotIn("reason", entry)

    # ── §4b a never-snapshotted player counts ZERO (joined after the blacklist) ─
    def test_player_never_snapshotted_counts_zero(self):
        late_joiner = User.objects.create_user(
            username="latejoiner", email="lj@x.com", password="x", role="player",
            country="Nigeria",
        )
        TeamMembers.objects.create(team=self.team, member=late_joiner,
                                   management_role="member")
        resp = self._lookup(self.owner_b, user_id=late_joiner.user_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 0)
        self.assertEqual(body["active_count"], 0)

    # ── §4c an individually-lifted player is not counted as ACTIVE ─────────────
    def test_individually_lifted_player_not_active(self):
        # Retire player_a's row under the CURRENT blacklist (their lift was approved).
        OrganizerBlacklistPlayer.objects.filter(
            blacklist=self.bl_current, user=self.player_a
        ).update(is_active=False)

        resp = self._lookup(self.owner_b, user_id=self.player_a.user_id)
        body = resp.json()
        # History still shows both rows, but nothing blocks them right now...
        self.assertEqual(body["total_count"], 2)
        self.assertEqual(body["active_count"], 0)
        # ...and the current blacklist's entry reads "lifted" FOR THIS PLAYER.
        newest = body["entries"][0]
        self.assertEqual(newest["id"], self.bl_current.id)
        self.assertEqual(newest["status"], "lifted")

    # ── §5 a plain player (no org membership) is locked out ────────────────────
    def test_plain_player_403(self):
        resp = self._lookup(self.plain_player, team_id=self.team.team_id)
        self.assertEqual(resp.status_code, 403, resp.content)

    # ── §5b exactly one of team_id / user_id is required ───────────────────────
    def test_requires_exactly_one_target(self):
        # Neither target -> 400.
        resp = self._lookup(self.owner_b)
        self.assertEqual(resp.status_code, 400, resp.content)
        # Both targets -> 400.
        resp = self._lookup(
            self.owner_b, team_id=self.team.team_id, user_id=self.player_a.user_id
        )
        self.assertEqual(resp.status_code, 400, resp.content)


class AdminBlacklistDashboardApiTests(BlacklistLookupTestBase):
    """GET organizers/admin/blacklists/ - the AFC dashboard feed (platform-admin only)."""

    # ── §6 the dashboard is admin-only: organizers are 403'd ───────────────────
    def test_dashboard_admin_only(self):
        # An organizer (even an org OWNER) is not AFC staff -> 403.
        resp = self._dashboard(self.owner_a)
        self.assertEqual(resp.status_code, 403, resp.content)
        # A plain player too.
        resp = self._dashboard(self.plain_player)
        self.assertEqual(resp.status_code, 403, resp.content)
        # The platform admin gets through.
        resp = self._dashboard(self.afc_admin)
        self.assertEqual(resp.status_code, 200, resp.content)

    # ── §7 rows carry reasons + lifecycle fields; aggregates add up ────────────
    def test_dashboard_rows_and_aggregates(self):
        resp = self._dashboard(self.afc_admin)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()

        # Both rows, newest (by created_at) first, each with the full admin payload.
        self.assertEqual(body["total_count"], 2)
        row = body["results"][0]
        self.assertEqual(row["organization_name"], "Org Alpha Events")
        self.assertEqual(row["team_name"], "Team Alpha")
        self.assertIn(row["reason"], {"Old offence", "Current offence"})
        self.assertEqual(row["player_snapshot_count"], 3)
        self.assertIn(row["status"], {"active", "expired"})

        # Aggregates over the (unfiltered) set: 2 total, 1 blocking right now, Org Alpha
        # tops the by-organization list and Team Alpha the most-blacklisted list.
        agg = body["aggregates"]
        self.assertEqual(agg["total"], 2)
        self.assertEqual(agg["active"], 1)
        self.assertEqual(agg["by_organization"][0]["organization_name"], "Org Alpha Events")
        self.assertEqual(agg["by_organization"][0]["count"], 2)
        self.assertEqual(agg["most_blacklisted_teams"][0]["team_name"], "Team Alpha")
        self.assertEqual(agg["most_blacklisted_teams"][0]["count"], 2)

    # ── §7b the status filter uses EFFECTIVE status (lapsed active row = expired) ─
    def test_dashboard_status_filter_effective(self):
        # "active" -> only the current row (the old one lapsed even though status says active).
        body = self._dashboard(self.afc_admin, status="active").json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["id"], self.bl_current.id)

        # "expired" -> only the lapsed row, no sweep needed.
        body = self._dashboard(self.afc_admin, status="expired").json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["id"], self.bl_old.id)

    # ── §7c search matches team name AND org name ──────────────────────────────
    def test_dashboard_search(self):
        # By team name.
        body = self._dashboard(self.afc_admin, search="Team Alpha").json()
        self.assertEqual(body["total_count"], 2)
        # By org name.
        body = self._dashboard(self.afc_admin, search="Bravo").json()
        self.assertEqual(body["total_count"], 0)
        body = self._dashboard(self.afc_admin, search="Org Alpha").json()
        self.assertEqual(body["total_count"], 2)

    # ── §7d the date window narrows BOTH the rows and the aggregates ───────────
    def test_dashboard_window_narrows_rows_and_aggregates(self):
        today = timezone.now().date()
        body = self._dashboard(
            self.afc_admin, start=(today - timedelta(days=7)).isoformat()
        ).json()
        # Only the current blacklist started inside the window...
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["id"], self.bl_current.id)
        # ...and the stat-card aggregates follow the same window.
        self.assertEqual(body["aggregates"]["total"], 1)
        self.assertEqual(body["aggregates"]["active"], 1)
