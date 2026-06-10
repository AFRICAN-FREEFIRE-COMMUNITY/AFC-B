"""
afc_rankings.test_ghost_claims — the ghost-team / ghost-player CLAIM PROCESS + re-attribution.

Sibling flat test module to afc_rankings/tests.py + test_standalone_feed.py + test_ghost_rankings.py
(afc_rankings uses flat tests modules, so all are auto-discovered). Covers the claim process built
to WEBSITE/tasks/ghost-claim-process-design.md:

  - REQUEST (user-facing initiate step): a team owner sets the ghost pending + claimed_by + note; a
    non-owner is 403; a non-unclaimed ghost is 400; a conflict (the real team already shares a
    leaderboard with the ghost) is 400 with the ghost untouched. A player self-request goes pending.
  - APPROVE TEAM re-attributes: a published+counting team LB with a ghost participant that scores ->
    the ghost holds a monthly + quarterly score row -> request -> approve -> the ghost's
    LeaderboardParticipant now points at the REAL team, the ghost score rows are GONE, and the real
    team now holds the monthly + seasonal score row with the inherited points + is ranked.
  - APPROVE PLAYER re-attributes (solo LB, ghost_player -> real user), same proof.
  - APPROVE with NO leaderboard history just flips to claimed (no recompute crash).
  - REJECT resets to unclaimed; approve without a pending claim -> 400; double approve -> 400.
  - Audit rows written for approve + reject.

HOW IT CONNECTS
    - Drives the endpoints in afc_rankings.admin_ghost (ghost_team_request_claim, ghost_approve_claim,
      ghost_reject_claim, and the ghost_player_* siblings) via the Django test Client with a Bearer
      token, mirroring afc_leaderboard's _helpers idiom.
    - Exercises the re-attribution service afc_rankings.claims.reattribute_ghost_team /
      reattribute_ghost_player (the core), which re-points afc_leaderboard.LeaderboardParticipant rows
      and recomputes via afc_rankings.recalc.
    - Reuses the published+counting-LB builders from test_standalone_feed so the ghost actually scores
      (the same compute path the FE + the view use), making the inherited points honest.
"""
import datetime

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model

from afc_team.models import Team, TeamMembers
from afc_rankings.models import (
    GhostTeam, GhostPlayer, RankingAuditLog,
    TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
)
from afc_rankings import recalc, standalone
from afc_leaderboard.models import LeaderboardMatch, LeaderboardParticipant
from afc_auth.models import SessionToken, Roles, UserRoles

# Reuse the standalone-feed builders verbatim (same conventions, no duplication).
from afc_rankings.test_standalone_feed import (
    PLAYED_MONTH, _make_season, _make_team, _make_lb,
    _add_team_participant, _add_solo_participant, _save_result,
)

User = get_user_model()


# ───────────────────────── local helpers (auth + ghost-player participant) ─────────────────────────
def _token(user, label):
    """A live SessionToken string for `user` (the house Bearer idiom, afc_leaderboard _helpers)."""
    return SessionToken.objects.create(user=user, token=f"tok_{label}").token


def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _make_admin(username="claimadmin"):
    """A ranking admin (granular head_admin role -> passes admin_views._auth). Returns (user, token)."""
    u = User.objects.create(username=username, email=f"{username}@x.com")
    r, _ = Roles.objects.get_or_create(role_name="head_admin")
    UserRoles.objects.create(user=u, role=r)
    return u, _token(u, username)


def _add_ghost_team_participant(lb, ghost_team):
    return LeaderboardParticipant.objects.create(leaderboard=lb, ghost_team=ghost_team)


def _add_ghost_player_participant(lb, ghost_player):
    return LeaderboardParticipant.objects.create(leaderboard=lb, ghost_player=ghost_player)


# ═════════════════════════ TEAM REQUEST (user-facing initiate step) ═════════════════════════
class TeamRequestClaimTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="towner", email="to@x.com")
        self.owner_tok = _token(self.owner, "towner")
        self.other = User.objects.create(username="tother", email="tt@x.com")
        self.other_tok = _token(self.other, "tother")
        self.team = _make_team("Owned FC", self.owner)
        self.ghost = GhostTeam.objects.create(team_name="Ghost FC", country="NG", created_by=self.owner)

    def _request(self, token, **body):
        return self.client.post(
            reverse("rankings_ghost_team_request_claim", args=[self.ghost.pk]),
            data=body, content_type="application/json", **_bearer(token),
        )

    def test_owner_sets_pending_with_target_and_note(self):
        resp = self._request(self.owner_tok, team_id=self.team.team_id, evidence="we are this squad")
        self.assertEqual(resp.status_code, 200)
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "pending")
        self.assertEqual(self.ghost.claimed_by_id, self.team.team_id)
        self.assertEqual(self.ghost.claim_requested_by_id, self.owner.pk)
        self.assertEqual(self.ghost.claim_note, "we are this squad")
        self.assertIsNotNone(self.ghost.claim_requested_at)

    def test_captain_role_member_can_request(self):
        # a roster captain (not the owner) also passes the team-role gate.
        cap = User.objects.create(username="cap", email="cap@x.com")
        cap_tok = _token(cap, "cap")
        TeamMembers.objects.create(team=self.team, member=cap, management_role="team_captain")
        resp = self._request(cap_tok, team_id=self.team.team_id)
        self.assertEqual(resp.status_code, 200)

    def test_non_owner_forbidden(self):
        resp = self._request(self.other_tok, team_id=self.team.team_id)
        self.assertEqual(resp.status_code, 403)
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "unclaimed", "a 403 must not touch the ghost")

    def test_not_unclaimed_rejected(self):
        self.ghost.claim_status = "pending"
        self.ghost.save(update_fields=["claim_status"])
        resp = self._request(self.owner_tok, team_id=self.team.team_id)
        self.assertEqual(resp.status_code, 400)

    def test_conflict_rejected_and_ghost_untouched(self):
        # the real team is ALREADY a participant in a leaderboard the ghost also plays -> conflict.
        lb = _make_lb(self.owner, tier="tier_3")
        _add_team_participant(lb, team=self.team)
        _add_ghost_team_participant(lb, self.ghost)
        resp = self._request(self.owner_tok, team_id=self.team.team_id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already a participant", resp.json()["message"].lower())
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "unclaimed", "a conflict must leave the ghost unclaimed")
        self.assertIsNone(self.ghost.claimed_by_id)


# ═════════════════════════ PLAYER REQUEST (self) ═════════════════════════
class PlayerRequestClaimTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="psolo", email="ps@x.com")
        self.user_tok = _token(self.user, "psolo")
        self.ghost = GhostPlayer.objects.create(ign="GhostIGN", slot=1)

    def test_self_request_goes_pending(self):
        resp = self.client.post(
            reverse("rankings_ghost_player_request_claim", args=[self.ghost.pk]),
            data={"evidence": "this is my smurf"}, content_type="application/json",
            **_bearer(self.user_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "pending")
        self.assertEqual(self.ghost.claimed_by_id, self.user.pk, "a player claims itself")
        self.assertEqual(self.ghost.claim_requested_by_id, self.user.pk)
        self.assertEqual(self.ghost.claim_note, "this is my smurf")


# ═════════════════════════ APPROVE TEAM re-attributes ═════════════════════════
class ApproveTeamReattributionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="aowner", email="ao@x.com")
        self.owner_tok = _token(self.owner, "aowner")
        self.admin, self.admin_tok = _make_admin("teamclaimadmin")
        self.season = _make_season()
        self.team = _make_team("Real Inheritor", self.owner)
        self.ghost = GhostTeam.objects.create(team_name="Ghost Donor", country="NG", created_by=self.owner)

        # A published+counting TEAM LB where ONLY the ghost participates (no conflict). It places 1st
        # with kills, so it earns a real ranked monthly + quarterly score row.
        self.lb = _make_lb(self.owner, tier="tier_2")
        self.pGhost = _add_ghost_team_participant(self.lb, self.ghost)
        m1 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=1)
        _save_result(self.lb, m1, self.pGhost, placement=1, kills=18)
        standalone.recalc_ghost_team_monthly(self.ghost.pk, PLAYED_MONTH)
        standalone.recalc_ghost_team_quarterly(self.ghost.pk, self.season.season_id)

        # the requester (owner) files the claim, putting the ghost pending + claimed_by = the team.
        self.ghost.claim_status = "pending"
        self.ghost.claim_requested_by = self.owner
        self.ghost.claim_requested_at = datetime.datetime(2099, 2, 12, tzinfo=datetime.timezone.utc)
        self.ghost.claimed_by = self.team
        self.ghost.save()

    def test_ghost_has_score_before_approve(self):
        # sanity: the ghost actually earned ranked score rows from the LB before any claim.
        gm = TeamMonthlyScore.objects.get(ghost_team=self.ghost, month=PLAYED_MONTH)
        gq = TeamQuarterlyScore.objects.get(ghost_team=self.ghost, season=self.season)
        self.assertGreater(gm.total_score, 0)
        self.assertGreater(gq.total_score, 0)
        self.assertEqual(gm.rank, 1, "the lone ghost ranks 1 in the month")

    def test_approve_reattributes_history_to_real_team(self):
        # capture the ghost's pre-approval points so we can prove they transfer intact.
        ghost_month_pts = TeamMonthlyScore.objects.get(ghost_team=self.ghost, month=PLAYED_MONTH).total_score
        ghost_qtr_pts = TeamQuarterlyScore.objects.get(ghost_team=self.ghost, season=self.season).total_score

        resp = self.client.post(
            reverse("rankings_ghost_approve_claim", args=[self.ghost.pk]),
            data={"reason": "verified evidence, approving claim"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # 1) the ghost is now claimed.
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "claimed")
        self.assertIsNotNone(self.ghost.claimed_at)
        self.assertEqual(self.ghost.claim_approved_by_id, self.admin.pk)

        # 2) the LB participant row now points at the REAL team (re-attributed), not the ghost.
        self.pGhost.refresh_from_db()
        self.assertEqual(self.pGhost.team_id, self.team.team_id, "participant re-pointed to the real team")
        self.assertIsNone(self.pGhost.ghost_team_id, "the ghost side is cleared")

        # 3) the ghost's score rows are GONE.
        self.assertFalse(TeamMonthlyScore.objects.filter(ghost_team=self.ghost).exists())
        self.assertFalse(TeamQuarterlyScore.objects.filter(ghost_team=self.ghost).exists())

        # 4) the REAL team now holds the monthly + seasonal score rows with the inherited points + a rank.
        rm = TeamMonthlyScore.objects.get(team=self.team, month=PLAYED_MONTH)
        rq = TeamQuarterlyScore.objects.get(team=self.team, season=self.season)
        self.assertEqual(rm.total_score, ghost_month_pts, "the real team inherits the ghost's monthly points")
        self.assertEqual(rq.total_score, ghost_qtr_pts, "the real team inherits the ghost's quarterly points")
        self.assertEqual(rm.rank, 1, "the real team is ranked (the lone scorer)")
        self.assertEqual(rq.rank, 1)

    def test_audit_row_written_on_approve(self):
        before = RankingAuditLog.objects.filter(object_type="ghost_claim", action="approve").count()
        self.client.post(
            reverse("rankings_ghost_approve_claim", args=[self.ghost.pk]),
            data={"reason": "verified evidence, approving claim"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        after = RankingAuditLog.objects.filter(object_type="ghost_claim", action="approve").count()
        self.assertEqual(after, before + 1)

    def test_double_approve_guarded(self):
        # first approve flips to claimed; a second approve has no pending claim -> 400.
        self.client.post(
            reverse("rankings_ghost_approve_claim", args=[self.ghost.pk]),
            data={"reason": "first approval pass here"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        resp2 = self.client.post(
            reverse("rankings_ghost_approve_claim", args=[self.ghost.pk]),
            data={"reason": "second approval attempt here"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        self.assertEqual(resp2.status_code, 400)
        self.assertIn("no pending claim", resp2.json()["message"].lower())


# ═════════════════════════ APPROVE PLAYER re-attributes ═════════════════════════
class ApprovePlayerReattributionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="powner", email="po@x.com")
        self.admin, self.admin_tok = _make_admin("playerclaimadmin")
        self.season = _make_season()
        self.realuser = User.objects.create(username="RealClaimer", email="rc@x.com")

        # A published+counting SOLO LB where only the ghost player participates.
        self.ghost = GhostPlayer.objects.create(ign="GhostSoloDonor", slot=1)
        self.lb = _make_lb(self.owner, fmt="solo", tier="tier_2")
        self.pGhost = _add_ghost_player_participant(self.lb, self.ghost)
        m1 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=1)
        _save_result(self.lb, m1, self.pGhost, placement=1, kills=14)
        standalone.recalc_ghost_player_monthly(self.ghost.pk, PLAYED_MONTH)
        standalone.recalc_ghost_player_quarterly(self.ghost.pk, self.season.season_id)

        # the user self-files the claim (pending + claimed_by = the user).
        self.ghost.claim_status = "pending"
        self.ghost.claim_requested_by = self.realuser
        self.ghost.claimed_by = self.realuser
        self.ghost.claim_requested_at = datetime.datetime(2099, 2, 12, tzinfo=datetime.timezone.utc)
        self.ghost.save()

    def test_approve_reattributes_solo_history_to_user(self):
        ghost_month_pts = PlayerMonthlyScore.objects.get(ghost_player=self.ghost, month=PLAYED_MONTH).total_score
        ghost_qtr_pts = PlayerQuarterlyScore.objects.get(ghost_player=self.ghost, season=self.season).total_score
        self.assertGreater(ghost_month_pts, 0)

        resp = self.client.post(
            reverse("rankings_ghost_player_approve_claim", args=[self.ghost.pk]),
            data={"reason": "confirmed this is the player"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "claimed")
        self.assertEqual(self.ghost.claim_approved_by_id, self.admin.pk)

        # the participant now points at the real user.
        self.pGhost.refresh_from_db()
        self.assertEqual(self.pGhost.user_id, self.realuser.pk)
        self.assertIsNone(self.pGhost.ghost_player_id)

        # the ghost player's score rows are gone; the real user inherited them ranked.
        self.assertFalse(PlayerMonthlyScore.objects.filter(ghost_player=self.ghost).exists())
        self.assertFalse(PlayerQuarterlyScore.objects.filter(ghost_player=self.ghost).exists())
        rm = PlayerMonthlyScore.objects.get(player=self.realuser, month=PLAYED_MONTH)
        rq = PlayerQuarterlyScore.objects.get(player=self.realuser, season=self.season)
        self.assertEqual(rm.total_score, ghost_month_pts)
        self.assertEqual(rq.total_score, ghost_qtr_pts)
        self.assertEqual(rm.rank, 1)


# ═════════════════════════ APPROVE with NO history + REJECT + guards ═════════════════════════
class ApproveNoHistoryAndRejectTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="nowner", email="no@x.com")
        self.admin, self.admin_tok = _make_admin("noclaimadmin")
        self.team = _make_team("History-less FC", self.owner)
        # a ghost with a pending claim but NO leaderboard participation at all.
        self.ghost = GhostTeam.objects.create(team_name="Empty Ghost", country="NG", created_by=self.owner)
        self.ghost.claim_status = "pending"
        self.ghost.claim_requested_by = self.owner
        self.ghost.claimed_by = self.team
        self.ghost.save()

    def test_approve_with_no_history_just_flips_to_claimed(self):
        resp = self.client.post(
            reverse("rankings_ghost_approve_claim", args=[self.ghost.pk]),
            data={"reason": "approving a history-less ghost"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "claimed")
        # the re-attribution summary reports zero moved participants (no crash on empty history).
        self.assertEqual(resp.json()["reattribution"]["reattributed_participants"], 0)

    def test_reject_resets_to_unclaimed(self):
        resp = self.client.post(
            reverse("rankings_ghost_reject_claim", args=[self.ghost.pk]),
            data={"reason": "evidence insufficient, rejecting"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "unclaimed")
        self.assertIsNone(self.ghost.claimed_by_id)
        self.assertIsNone(self.ghost.claim_requested_by_id)
        self.assertIsNotNone(self.ghost.claim_revoked_at)

    def test_reject_audit_row_written(self):
        before = RankingAuditLog.objects.filter(object_type="ghost_claim", action="reject").count()
        self.client.post(
            reverse("rankings_ghost_reject_claim", args=[self.ghost.pk]),
            data={"reason": "evidence insufficient, rejecting"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        after = RankingAuditLog.objects.filter(object_type="ghost_claim", action="reject").count()
        self.assertEqual(after, before + 1)

    def test_approve_without_pending_claim_400(self):
        # an unclaimed ghost has nothing to approve.
        unclaimed = GhostTeam.objects.create(team_name="Still Unclaimed", country="NG", created_by=self.owner)
        resp = self.client.post(
            reverse("rankings_ghost_approve_claim", args=[unclaimed.pk]),
            data={"reason": "trying to approve an unclaimed ghost"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no pending claim", resp.json()["message"].lower())

    def test_reject_without_pending_claim_400(self):
        unclaimed = GhostTeam.objects.create(team_name="Nope Ghost", country="NG", created_by=self.owner)
        resp = self.client.post(
            reverse("rankings_ghost_reject_claim", args=[unclaimed.pk]),
            data={"reason": "trying to reject a non-pending ghost"},
            content_type="application/json", **_bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 400)


# ═════════════════════════ pending-claim listing filter (admin queue) ═════════════════════════
class PendingClaimListingTests(TestCase):
    def setUp(self):
        self.admin, self.admin_tok = _make_admin("queueadmin")
        # one pending ghost player + one unclaimed ghost player.
        self.pending = GhostPlayer.objects.create(ign="PendingIGN", slot=1, claim_status="pending")
        self.unclaimed = GhostPlayer.objects.create(ign="UnclaimedIGN", slot=1)

    def test_ghost_players_list_filters_pending(self):
        resp = self.client.get(
            reverse("rankings_ghost_players"), {"claim_status": "pending"}, **_bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        igns = [r["ign"] for r in resp.json()["results"]]
        self.assertIn("PendingIGN", igns)
        self.assertNotIn("UnclaimedIGN", igns)
