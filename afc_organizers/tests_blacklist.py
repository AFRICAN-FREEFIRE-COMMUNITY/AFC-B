# afc_organizers/tests_blacklist.py
# ──────────────────────────────────────────────────────────────────────────────
# Tests for the ORGANIZER BLACKLIST feature (2026-06-10).
#
# Covers the endpoints in views_blacklist.py and the snapshot/lift behaviour of the models:
#   - create snapshots the team's CURRENT players; a non-manager (no can_manage_registrations)
#     is 403'd.
#   - organizer early-lift clears the blacklist + all player rows.
#   - lift REQUESTS: a team manager raises one; the organizer approves (team scope -> full lift)
#     or denies; a duplicate pending request is rejected (400).
#   - player-scope lift: approving one player's request unblocks only that player.
#
# The REGISTRATION ENFORCEMENT tests (a blacklisted team / a player who left the team is still
# blocked / an expired or different-org blacklist does not block) live in
# afc_tournament_and_scrims/tests_blacklist.py, next to register_for_event.
#
# Auth in this codebase is a bearer SessionToken (afc_auth.SessionToken) validated by
# afc_auth.views.validate_token, so every test mints a real SessionToken and sends it in the
# Authorization header (no DRF auth classes are wired for these function-based views). Nothing
# in these endpoints touches the network.
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


class OrganizerBlacklistApiTests(TestCase):
    # ── shared fixtures ──────────────────────────────────────────────────────
    def _token_for(self, user):
        """Mint a real bearer SessionToken for `user` and return its raw value."""
        st = SessionToken.objects.create(
            user=user,
            token=f"tok-{user.username}-{uuid.uuid4().hex}",
            expires_at=timezone.now() + timedelta(days=1),
        )
        return st.token

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self._token_for(user)}"}

    def setUp(self):
        # Organizer (owner of an org, implicitly can_manage_registrations).
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

        # A team with a captain + two members.
        self.captain = User.objects.create_user(
            username="captain", email="cap@x.com", password="x", role="player", country="Nigeria"
        )
        self.player_a = User.objects.create_user(
            username="player_a", email="a@x.com", password="x", role="player", country="Nigeria"
        )
        self.player_b = User.objects.create_user(
            username="player_b", email="b@x.com", password="x", role="player", country="Nigeria"
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

    # ── helpers ──────────────────────────────────────────────────────────────
    def _create_blacklist(self, actor, start_date=None, end_date=None):
        """POST a create using the NEW primary path: a calendar date RANGE (start_date/end_date as
        ISO YYYY-MM-DD). Defaults to today -> 30 days out so existing create tests keep working."""
        today = timezone.now().date()
        body = {
            "organization_id": self.org.pk,
            "team_id": self.team.team_id,
            "start_date": (start_date if start_date is not None else today.isoformat()),
            "end_date": (end_date if end_date is not None else (today + timedelta(days=30)).isoformat()),
            "reason": "Caught smurfing",
        }
        return self.client.post(
            reverse("organizers_blacklists"),
            data=body,
            content_type="application/json",
            **self._auth(actor),
        )

    # ── §1 create snapshots the team's current players ─────────────────────────
    def test_create_snapshots_current_players(self):
        resp = self._create_blacklist(self.organizer)
        self.assertEqual(resp.status_code, 201, resp.content)

        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)
        self.assertEqual(blacklist.status, "active")
        self.assertTrue(blacklist.is_currently_active())

        # All three current members were snapshotted, all active.
        snapshot_ids = set(
            OrganizerBlacklistPlayer.objects.filter(blacklist=blacklist, is_active=True)
            .values_list("user_id", flat=True)
        )
        self.assertEqual(
            snapshot_ids,
            {self.captain.user_id, self.player_a.user_id, self.player_b.user_id},
        )

    # ── §1b non-manager (no can_manage_registrations) is 403'd ─────────────────
    def test_create_requires_can_manage_registrations(self):
        resp = self._create_blacklist(self.weak_sub)
        self.assertEqual(resp.status_code, 403, resp.content)
        # Nothing was created.
        self.assertFalse(
            OrganizerBlacklist.objects.filter(organization=self.org, team=self.team).exists()
        )

    # ── §1c create with a calendar date RANGE stores the parsed dates ──────────
    def test_create_with_date_range_stores_dates(self):
        today = timezone.now().date()
        start = today
        end = today + timedelta(days=7)
        resp = self._create_blacklist(
            self.organizer, start_date=start.isoformat(), end_date=end.isoformat()
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)
        # start_date is the parsed start at day-start; end_date is the selected end day, end-of-day
        # (so the whole selected day is covered) -> still on the same calendar date.
        self.assertEqual(blacklist.start_date.date(), start)
        self.assertEqual(blacklist.end_date.date(), end)
        # end-of-day means the time component is the last second of the day, not midnight.
        self.assertEqual((blacklist.end_date.hour, blacklist.end_date.minute), (23, 59))
        self.assertTrue(blacklist.is_currently_active())

    # ── §1d end_date is required ───────────────────────────────────────────────
    def test_create_requires_end_date(self):
        today = timezone.now().date()
        resp = self.client.post(
            reverse("organizers_blacklists"),
            data={
                "organization_id": self.org.pk,
                "team_id": self.team.team_id,
                "start_date": today.isoformat(),
                # end_date omitted, and no duration_days fallback -> 400
            },
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            OrganizerBlacklist.objects.filter(organization=self.org, team=self.team).exists()
        )

    # ── §1e end_date must be strictly AFTER start_date ─────────────────────────
    def test_create_rejects_end_before_start(self):
        today = timezone.now().date()
        # end DAY before the start DAY -> rejected (must be strictly after). start_date parses to
        # day-start and end_date to day-end, so a same-day window is valid (covered separately);
        # this guards the genuinely-inverted range.
        start = today + timedelta(days=10)
        end = today + timedelta(days=3)
        resp = self._create_blacklist(
            self.organizer, start_date=start.isoformat(), end_date=end.isoformat()
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            OrganizerBlacklist.objects.filter(organization=self.org, team=self.team).exists()
        )

    # ── §1e2 a same-day window (start day == end day) is VALID ─────────────────
    def test_create_allows_same_day_window(self):
        today = timezone.now().date()
        # start parses to 00:00, end parses to 23:59:59 -> end strictly after start, same day.
        resp = self._create_blacklist(
            self.organizer, start_date=today.isoformat(), end_date=today.isoformat()
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)
        self.assertTrue(blacklist.is_currently_active())

    # ── §1f end_date must be in the FUTURE ─────────────────────────────────────
    def test_create_rejects_past_end_date(self):
        today = timezone.now().date()
        start = today - timedelta(days=10)
        end = today - timedelta(days=3)  # after start, but in the past -> rejected
        resp = self._create_blacklist(
            self.organizer, start_date=start.isoformat(), end_date=end.isoformat()
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            OrganizerBlacklist.objects.filter(organization=self.org, team=self.team).exists()
        )

    # ── §1g malformed date string -> 400 ──────────────────────────────────────
    def test_create_rejects_malformed_date(self):
        today = timezone.now().date()
        resp = self._create_blacklist(
            self.organizer, start_date="not-a-date", end_date=(today + timedelta(days=5)).isoformat()
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    # ── §1h BACKWARD-COMPAT: duration_days fallback still works ─────────────────
    def test_create_duration_days_fallback(self):
        before = timezone.now()
        resp = self.client.post(
            reverse("organizers_blacklists"),
            data={
                "organization_id": self.org.pk,
                "team_id": self.team.team_id,
                "duration_days": 14,  # old callers: no start_date/end_date
                "reason": "Caught smurfing",
            },
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)
        self.assertTrue(blacklist.is_currently_active())
        # end_date is roughly now + 14 days (fallback path), well in the future.
        self.assertGreater(blacklist.end_date, before + timedelta(days=13))
        self.assertLess(blacklist.end_date, before + timedelta(days=15))

    # ── §2 organizer early-lift clears blacklist + player rows ─────────────────
    def test_organizer_lift_clears_blacklist_and_players(self):
        self._create_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)

        resp = self.client.post(
            reverse("organizers_blacklist_lift", args=[blacklist.id]),
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        blacklist.refresh_from_db()
        self.assertEqual(blacklist.status, "lifted")
        self.assertFalse(blacklist.is_currently_active())
        self.assertFalse(
            OrganizerBlacklistPlayer.objects.filter(blacklist=blacklist, is_active=True).exists()
        )

    # ── §3 list returns the org's blacklists with nested players ───────────────
    def test_list_blacklists_returns_nested_players(self):
        self._create_blacklist(self.organizer)
        resp = self.client.get(
            reverse("organizers_blacklists"),
            {"organization_id": self.org.pk},
            **self._auth(self.organizer),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(len(body["results"][0]["players"]), 3)

    # ── §4 lift request: team manager -> organizer approves -> lifted ──────────
    def test_team_lift_request_approved_lifts_blacklist(self):
        self._create_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)

        # Captain (a team manager) requests a team-scope lift.
        req_resp = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.id]),
            data={"scope": "team", "reason": "We removed the offending player"},
            content_type="application/json",
            **self._auth(self.captain),
        )
        self.assertEqual(req_resp.status_code, 201, req_resp.content)
        lift_id = req_resp.json()["lift_request"]["id"]

        # Organizer approves -> whole blacklist lifted.
        dec_resp = self.client.post(
            reverse("organizers_blacklist_decide_lift", args=[lift_id]),
            data={"decision": "approve", "reason": "Accepted"},
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(dec_resp.status_code, 200, dec_resp.content)

        blacklist.refresh_from_db()
        self.assertEqual(blacklist.status, "lifted")
        self.assertFalse(
            OrganizerBlacklistPlayer.objects.filter(blacklist=blacklist, is_active=True).exists()
        )

    # ── §4b deny leaves the blacklist active ───────────────────────────────────
    def test_team_lift_request_denied_keeps_blacklist(self):
        self._create_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)
        req = BlacklistLiftRequest.objects.create(
            blacklist=blacklist, requested_by=self.captain, scope="team", status="pending"
        )

        dec_resp = self.client.post(
            reverse("organizers_blacklist_decide_lift", args=[req.id]),
            data={"decision": "deny", "reason": "Not convinced"},
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(dec_resp.status_code, 200, dec_resp.content)

        req.refresh_from_db()
        blacklist.refresh_from_db()
        self.assertEqual(req.status, "denied")
        self.assertEqual(blacklist.status, "active")
        self.assertTrue(blacklist.is_currently_active())

    # ── §4c duplicate pending lift request -> 400 ──────────────────────────────
    def test_duplicate_pending_lift_request_rejected(self):
        self._create_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)

        first = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.id]),
            data={"scope": "team"},
            content_type="application/json",
            **self._auth(self.captain),
        )
        self.assertEqual(first.status_code, 201, first.content)

        second = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.id]),
            data={"scope": "team"},
            content_type="application/json",
            **self._auth(self.captain),
        )
        self.assertEqual(second.status_code, 400, second.content)

    # ── §4d a non-manager member cannot raise a team-scope lift request ────────
    def test_non_manager_cannot_request_team_lift(self):
        self._create_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)

        resp = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.id]),
            data={"scope": "team"},
            content_type="application/json",
            **self._auth(self.player_a),  # a plain member, not a manager
        )
        self.assertEqual(resp.status_code, 403, resp.content)

    # ── §5 player-scope lift: approving one player unblocks only that player ────
    def test_player_scope_lift_unblocks_only_that_player(self):
        self._create_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)

        # player_a requests a lift for THEMSELVES.
        req_resp = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.id]),
            data={"scope": "player", "target_user_id": self.player_a.user_id,
                  "reason": "I never played that match"},
            content_type="application/json",
            **self._auth(self.player_a),
        )
        self.assertEqual(req_resp.status_code, 201, req_resp.content)
        lift_id = req_resp.json()["lift_request"]["id"]

        # Organizer approves the player-scope request.
        dec_resp = self.client.post(
            reverse("organizers_blacklist_decide_lift", args=[lift_id]),
            data={"decision": "approve"},
            content_type="application/json",
            **self._auth(self.organizer),
        )
        self.assertEqual(dec_resp.status_code, 200, dec_resp.content)

        # Only player_a is now inactive; the others stay blocked and the blacklist stays active.
        self.assertFalse(
            OrganizerBlacklistPlayer.objects.get(
                blacklist=blacklist, user=self.player_a
            ).is_active
        )
        self.assertTrue(
            OrganizerBlacklistPlayer.objects.get(
                blacklist=blacklist, user=self.player_b
            ).is_active
        )
        blacklist.refresh_from_db()
        self.assertEqual(blacklist.status, "active")

    # ── §5b a player cannot request a lift for a DIFFERENT player ──────────────
    def test_player_cannot_request_lift_for_another_player(self):
        self._create_blacklist(self.organizer)
        blacklist = OrganizerBlacklist.objects.get(organization=self.org, team=self.team)

        resp = self.client.post(
            reverse("organizers_blacklist_request_lift", args=[blacklist.id]),
            data={"scope": "player", "target_user_id": self.player_b.user_id},
            content_type="application/json",
            **self._auth(self.player_a),  # player_a asking for player_b -> not allowed
        )
        self.assertEqual(resp.status_code, 403, resp.content)


class OrganizerBlacklistMineApiTests(TestCase):
    """Tests for GET blacklists/mine/ - the AFFECTED-PARTY discovery view (no org gate).

    It must surface the active blacklists that affect the caller two ways: as a TEAM manager
    (blacklists on a team they manage) and as a snapshotted PLAYER (an active OrganizerBlacklistPlayer
    under an active blacklist), the latter even after they leave the team. Expired/lifted blacklists
    must NOT appear, an unrelated user gets an empty list, and a caller's own pending lift request is
    reflected so the UI can disable re-requesting.
    """

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
        self.organizer = User.objects.create_user(
            username="organizer", email="org@x.com", password="x", role="player"
        )
        self.org = Organization.objects.create(slug="acme", name="Acme Esports")

        # Team Alpha (will be blacklisted): captain (manager) + a mover (plain member).
        self.captain = User.objects.create_user(
            username="captain", email="cap@x.com", password="x", role="player", country="Nigeria"
        )
        self.mover = User.objects.create_user(
            username="mover", email="mover@x.com", password="x", role="player", country="Nigeria"
        )
        self.alpha = Team.objects.create(
            team_name="Team Alpha", join_settings="open",
            team_creator=self.captain, team_owner=self.captain, country="Nigeria",
        )
        TeamMembers.objects.create(team=self.alpha, member=self.captain,
                                   management_role="team_captain")
        TeamMembers.objects.create(team=self.alpha, member=self.mover,
                                   management_role="member")

        # A clean second team the mover later joins.
        self.beta_owner = User.objects.create_user(
            username="beta_owner", email="bo@x.com", password="x", role="player", country="Nigeria"
        )
        self.beta = Team.objects.create(
            team_name="Team Beta", join_settings="open",
            team_creator=self.beta_owner, team_owner=self.beta_owner, country="Nigeria",
        )
        TeamMembers.objects.create(team=self.beta, member=self.beta_owner,
                                   management_role="team_captain")

        # An unrelated user on no blacklisted team.
        self.outsider = User.objects.create_user(
            username="outsider", email="out@x.com", password="x", role="player", country="Nigeria"
        )

    def _blacklist_alpha(self, days=30):
        """Create an active blacklist on Team Alpha and snapshot its current members (mirrors the
        create endpoint at the model level so this discovery test does not depend on that view)."""
        blacklist = OrganizerBlacklist.objects.create(
            organization=self.org, team=self.alpha, reason="Smurfing",
            end_date=timezone.now() + timedelta(days=days),
            created_by=self.organizer, status="active",
        )
        for uid in TeamMembers.objects.filter(team=self.alpha).values_list("member_id", flat=True):
            OrganizerBlacklistPlayer.objects.create(blacklist=blacklist, user_id=uid)
        return blacklist

    def _mine(self, user, **query):
        return self.client.get(reverse("organizers_blacklists_mine"), query, **self._auth(user))

    # ── a manager of a blacklisted team sees it with can_request_team_lift true ──
    def test_team_manager_sees_blacklist_with_team_lift_flag(self):
        blacklist = self._blacklist_alpha()
        resp = self._mine(self.captain)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        row = body["results"][0]
        self.assertEqual(row["id"], blacklist.id)
        self.assertEqual(row["team_id"], self.alpha.team_id)
        self.assertEqual(row["team_name"], "Team Alpha")
        self.assertEqual(row["organization_id"], self.org.pk)
        self.assertEqual(row["organization_name"], "Acme Esports")
        self.assertTrue(row["can_request_team_lift"])
        # The captain is also a snapshot player, so self-lift is available too.
        self.assertTrue(row["can_request_self_lift"])
        self.assertIsNone(row["my_pending_request"])

    # ── THE CORE: a snapshot player who LEFT the team still sees it via /mine/ ───
    def test_departed_snapshot_player_still_sees_blacklist(self):
        self._blacklist_alpha()
        # Mover leaves Team Alpha and joins the clean Team Beta.
        TeamMembers.objects.filter(team=self.alpha, member=self.mover).delete()
        TeamMembers.objects.create(team=self.beta, member=self.mover, management_role="member")

        resp = self._mine(self.mover)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        row = body["results"][0]
        # They are no longer a manager of the blacklisted team, but the player block follows them.
        self.assertFalse(row["can_request_team_lift"])
        self.assertTrue(row["can_request_self_lift"])

    # ── an unrelated user gets an empty list ───────────────────────────────────
    def test_unrelated_user_sees_empty_list(self):
        self._blacklist_alpha()
        resp = self._mine(self.outsider)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 0)
        self.assertEqual(body["results"], [])

    # ── an expired blacklist is not returned ───────────────────────────────────
    def test_expired_blacklist_not_returned(self):
        blacklist = self._blacklist_alpha()
        OrganizerBlacklist.objects.filter(pk=blacklist.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        resp = self._mine(self.captain)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["total_count"], 0)

    # ── a caller's pending lift request is reflected in the payload ────────────
    def test_pending_request_reflected_in_payload(self):
        blacklist = self._blacklist_alpha()
        req = BlacklistLiftRequest.objects.create(
            blacklist=blacklist, requested_by=self.captain, scope="team", status="pending"
        )
        resp = self._mine(self.captain)
        self.assertEqual(resp.status_code, 200, resp.content)
        row = resp.json()["results"][0]
        self.assertIsNotNone(row["my_pending_request"])
        self.assertEqual(row["my_pending_request"]["id"], req.id)
        self.assertEqual(row["my_pending_request"]["scope"], "team")
        self.assertEqual(row["my_pending_request"]["status"], "pending")

    # ── ?team_id= filter narrows to one team ───────────────────────────────────
    def test_team_id_filter(self):
        self._blacklist_alpha()
        # Captain owns only Team Alpha; filtering by Beta returns nothing for them.
        resp = self._mine(self.captain, team_id=self.beta.team_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["total_count"], 0)

        resp = self._mine(self.captain, team_id=self.alpha.team_id)
        self.assertEqual(resp.json()["total_count"], 1)

    # ── a dedupe guard: manager who is ALSO a snapshot player sees ONE row ─────
    def test_manager_and_player_deduped_to_single_row(self):
        self._blacklist_alpha()
        # The captain is both a manager of Team Alpha AND a snapshot player on it.
        resp = self._mine(self.captain)
        self.assertEqual(resp.json()["total_count"], 1)
