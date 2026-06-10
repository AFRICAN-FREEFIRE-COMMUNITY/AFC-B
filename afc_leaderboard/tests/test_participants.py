"""
Endpoint tests for add_participant / remove_participant.

Covers: real team add, ghost_new (creates a GhostTeam + the participant), ghost_existing reuse,
wrong-kind-for-format rejection (400), duplicate participant (400), 403 for a non-manager, and
removal. Also covers the solo paths (real user + ghost_new player).
"""
import json
from django.test import TestCase, Client

from afc_rankings.models import GhostTeam, GhostPlayer
from afc_leaderboard.models import StandaloneLeaderboard, LeaderboardParticipant

from ._helpers import make_afc_admin, make_user, make_team, bearer


class ParticipantEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.stranger, self.stranger_tok = make_user("stranger")
        self.team_lb = StandaloneLeaderboard.objects.create(
            name="TeamLB", format="team", placement_points={"1": 12}, creator=self.admin
        )
        self.solo_lb = StandaloneLeaderboard.objects.create(
            name="SoloLB", format="solo", placement_points={"1": 12}, creator=self.admin
        )
        self.team = make_team("Dynasty", self.admin)
        self.user, _ = make_user("soloman")

    def _add(self, lb_id, body, tok=None):
        return self.client.post(
            f"/leaderboards/standalone/{lb_id}/participants/",
            data=json.dumps(body), content_type="application/json",
            **bearer(tok or self.admin_tok),
        )

    # ── real ────────────────────────────────────────────────────────────────────────────────
    def test_add_real_team(self):
        resp = self._add(self.team_lb.id, {"kind": "real", "team_id": self.team.team_id})
        self.assertEqual(resp.status_code, 201)
        p = resp.json()["participant"]
        self.assertEqual(p["kind"], "real_team")
        self.assertEqual(p["name"], "Dynasty")
        self.assertFalse(p["is_ghost"])

    def test_add_real_user_solo(self):
        resp = self._add(self.solo_lb.id, {"kind": "real", "user_id": self.user.user_id})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["participant"]["kind"], "real_user")

    def test_duplicate_real_team_rejected(self):
        self._add(self.team_lb.id, {"kind": "real", "team_id": self.team.team_id})
        resp = self._add(self.team_lb.id, {"kind": "real", "team_id": self.team.team_id})
        self.assertEqual(resp.status_code, 400)

    # ── ghost_new ───────────────────────────────────────────────────────────────────────────
    def test_ghost_new_team_creates_ghost_and_participant(self):
        before = GhostTeam.objects.count()
        resp = self._add(self.team_lb.id, {"kind": "ghost_new", "name": "Phantoms", "country": "GH",
                                           "players": ["ign1", "ign2"]})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(GhostTeam.objects.count(), before + 1)
        ghost = GhostTeam.objects.get(team_name="Phantoms")
        self.assertEqual(ghost.created_by_id, self.admin.user_id)   # provenance stamped
        self.assertEqual(ghost.players.count(), 2)                  # roster IGNs created
        p = resp.json()["participant"]
        self.assertEqual(p["kind"], "ghost_team")
        self.assertTrue(p["is_ghost"])

    def test_ghost_new_player_solo(self):
        before = GhostPlayer.objects.count()
        resp = self._add(self.solo_lb.id, {"kind": "ghost_new", "ign": "ShadowIGN"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(GhostPlayer.objects.count(), before + 1)
        self.assertEqual(resp.json()["participant"]["kind"], "ghost_player")

    # ── ghost_existing ──────────────────────────────────────────────────────────────────────
    def test_ghost_existing_team(self):
        ghost = GhostTeam.objects.create(team_name="Reuse", country="NG", created_by=self.admin)
        resp = self._add(self.team_lb.id, {"kind": "ghost_existing", "ghost_team_id": str(ghost.ghost_team_id)})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["participant"]["ghost_team_id"], str(ghost.ghost_team_id))

    def test_ghost_existing_player(self):
        gp = GhostPlayer.objects.create(ign="ExistingIGN")
        resp = self._add(self.solo_lb.id, {"kind": "ghost_existing", "ghost_player_id": gp.id})
        self.assertEqual(resp.status_code, 201)

    # ── wrong kind for format ───────────────────────────────────────────────────────────────
    def test_user_id_on_team_leaderboard_rejected(self):
        # Team format expects team_id, not user_id -> the team_id branch fires and 400s on missing team_id.
        resp = self._add(self.team_lb.id, {"kind": "real", "user_id": self.user.user_id})
        self.assertEqual(resp.status_code, 400)

    def test_team_id_on_solo_leaderboard_rejected(self):
        resp = self._add(self.solo_lb.id, {"kind": "real", "team_id": self.team.team_id})
        self.assertEqual(resp.status_code, 400)

    def test_bad_kind_rejected(self):
        self.assertEqual(self._add(self.team_lb.id, {"kind": "wat"}).status_code, 400)

    # ── permission ──────────────────────────────────────────────────────────────────────────
    def test_non_manager_cannot_add(self):
        resp = self._add(self.team_lb.id, {"kind": "real", "team_id": self.team.team_id}, tok=self.stranger_tok)
        self.assertEqual(resp.status_code, 403)

    # ── remove ──────────────────────────────────────────────────────────────────────────────
    def test_remove_participant(self):
        pid = self._add(self.team_lb.id, {"kind": "real", "team_id": self.team.team_id}).json()["participant"]["id"]
        resp = self.client.delete(f"/leaderboards/standalone/{self.team_lb.id}/participants/{pid}/", **bearer(self.admin_tok))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(LeaderboardParticipant.objects.filter(id=pid).exists())

    def test_remove_non_manager_403(self):
        pid = self._add(self.team_lb.id, {"kind": "real", "team_id": self.team.team_id}).json()["participant"]["id"]
        resp = self.client.delete(f"/leaderboards/standalone/{self.team_lb.id}/participants/{pid}/", **bearer(self.stranger_tok))
        self.assertEqual(resp.status_code, 403)
