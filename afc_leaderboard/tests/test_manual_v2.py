"""
Endpoint tests for the MANUAL-ENTRY v2 additions (owner 2026-06-12):

  1. save_match_results accepts a per-player kill breakdown (results[].players): the TEAM kills
     are the SERVER-computed sum (mirroring the event flow's enter_team_match_result_manual) and
     the breakdown is stored on ParticipantMatchResult.player_kills; saving WITHOUT a breakdown
     clears it (the row is replaced wholesale on upsert).
  2. participant_roster returns a REAL team's TeamMembers and a GHOST team's GhostPlayer slots,
     manager-gated.
  3. search_ghost_teams / search_ghost_players typeahead endpoints (Bearer auth, q>=2,
     punctuation/leet-insensitive like /team/search-teams/).
"""
import json
from django.test import TestCase, Client

from afc_team.models import TeamMembers
from afc_rankings.models import GhostTeam, GhostPlayer
from afc_leaderboard.models import (
    StandaloneLeaderboard, LeaderboardParticipant, ParticipantMatchResult,
)

from ._helpers import make_afc_admin, make_user, make_team, bearer


class PlayerKillsSaveTests(TestCase):
    """results[].players -> kills summed server-side + breakdown persisted."""

    def setUp(self):
        self.client = Client()
        self.admin, self.tok = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="LB", format="team", placement_points={"1": 12, "2": 9}, kill_point=1.0,
            creator=self.admin,
        )
        self.team = make_team("Alpha", self.admin)
        self.p = LeaderboardParticipant.objects.create(leaderboard=self.lb, team=self.team)
        self.mid = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/matches/",
            data=json.dumps({}), content_type="application/json", **bearer(self.tok),
        ).json()["match"]["id"]

    def _save(self, results):
        return self.client.post(
            f"/leaderboards/standalone/matches/{self.mid}/results/",
            data=json.dumps({"results": results}), content_type="application/json",
            **bearer(self.tok),
        )

    def test_players_breakdown_sums_team_kills_and_persists(self):
        resp = self._save([{
            "participant_id": self.p.id, "placement": 1,
            # The row-level kills is IGNORED when a breakdown is present (server sums 3+2+0=5).
            "kills": 99,
            "players": [
                {"name": "P1", "user_id": self.admin.user_id, "kills": 3},
                {"name": "P2", "user_id": None, "kills": 2},
                {"name": "P3", "kills": 0},
            ],
        }])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["results"][0]["kills"], 5)
        row = ParticipantMatchResult.objects.get(match_id=self.mid, participant=self.p)
        self.assertEqual(row.kills, 5)
        self.assertEqual(
            row.player_kills,
            [
                {"name": "P1", "user_id": self.admin.user_id, "kills": 3},
                {"name": "P2", "user_id": None, "kills": 2},
                {"name": "P3", "user_id": None, "kills": 0},
            ],
        )

    def test_resave_without_breakdown_clears_it(self):
        self._save([{
            "participant_id": self.p.id, "placement": 1, "kills": 0,
            "players": [{"name": "P1", "kills": 4}],
        }])
        self._save([{"participant_id": self.p.id, "placement": 2, "kills": 7}])
        row = ParticipantMatchResult.objects.get(match_id=self.mid, participant=self.p)
        self.assertEqual(row.kills, 7)
        self.assertIsNone(row.player_kills)

    def test_blank_or_malformed_players_fall_back_to_row_kills(self):
        resp = self._save([{
            "participant_id": self.p.id, "placement": 1, "kills": 6,
            # Nameless/non-dict entries are skipped; nothing valid left -> plain row kills.
            "players": [{"name": "   ", "kills": 4}, "junk"],
        }])
        self.assertEqual(resp.status_code, 200)
        row = ParticipantMatchResult.objects.get(match_id=self.mid, participant=self.p)
        self.assertEqual(row.kills, 6)
        self.assertIsNone(row.player_kills)


class ParticipantRosterTests(TestCase):
    """GET /<id>/participants/<pid>/roster/ - real TeamMembers vs ghost GhostPlayer slots."""

    def setUp(self):
        self.client = Client()
        self.admin, self.tok = make_afc_admin()
        self.stranger, self.stranger_tok = make_user("stranger")
        self.lb = StandaloneLeaderboard.objects.create(
            name="LB", format="team", placement_points={"1": 12}, kill_point=1.0,
            creator=self.admin,
        )
        # Real team with two members.
        self.team = make_team("Alpha", self.admin)
        self.m1, _ = make_user("alpha_one")
        self.m2, _ = make_user("alpha_two")
        TeamMembers.objects.create(team=self.team, member=self.m1, management_role="team_captain")
        TeamMembers.objects.create(team=self.team, member=self.m2)
        self.p_real = LeaderboardParticipant.objects.create(leaderboard=self.lb, team=self.team)
        # Ghost team with two roster slots (created_by is a required FK on GhostTeam).
        self.gt = GhostTeam.objects.create(team_name="Phantoms", country="NG", created_by=self.admin)
        GhostPlayer.objects.create(ghost_team=self.gt, ign="Spooky", slot=1)
        GhostPlayer.objects.create(ghost_team=self.gt, ign="Wraith", slot=2)
        self.p_ghost = LeaderboardParticipant.objects.create(leaderboard=self.lb, ghost_team=self.gt)

    def _roster(self, pid, tok=None):
        return self.client.get(
            f"/leaderboards/standalone/{self.lb.id}/participants/{pid}/roster/",
            **bearer(tok or self.tok),
        )

    def test_real_team_roster(self):
        resp = self._roster(self.p_real.id)
        self.assertEqual(resp.status_code, 200)
        players = resp.json()["players"]
        self.assertEqual(
            sorted(p["name"] for p in players), ["alpha_one", "alpha_two"],
        )
        # Real members carry their platform user_id.
        self.assertTrue(all(p["user_id"] is not None for p in players))

    def test_ghost_team_roster_in_slot_order(self):
        resp = self._roster(self.p_ghost.id)
        self.assertEqual(resp.status_code, 200)
        players = resp.json()["players"]
        self.assertEqual([p["name"] for p in players], ["Spooky", "Wraith"])
        self.assertTrue(all(p["user_id"] is None for p in players))

    def test_non_manager_gets_403(self):
        self.assertEqual(self._roster(self.p_real.id, tok=self.stranger_tok).status_code, 403)

    def test_unknown_participant_404(self):
        self.assertEqual(self._roster(999999).status_code, 404)


class GhostSearchTests(TestCase):
    """The ghost typeahead endpoints (Bearer auth, widen-only normalized matching)."""

    def setUp(self):
        self.client = Client()
        self.user, self.tok = make_user("searcher")
        self.gt = GhostTeam.objects.create(
            team_name="V-ENT PHANTOMS", country="NG", created_by=self.user,
        )
        GhostPlayer.objects.create(ghost_team=self.gt, ign="PH.SHED005", slot=1)
        # Inactive ghosts are hidden from the picker.
        GhostTeam.objects.create(
            team_name="V-ENT RETIRED", country="NG", is_active=False, created_by=self.user,
        )

    def test_ghost_team_search_plain_and_normalized(self):
        resp = self.client.get(
            "/leaderboards/standalone/search-ghost-teams/", {"q": "phantom"}, **bearer(self.tok),
        )
        self.assertEqual(resp.status_code, 200)
        names = [r["team_name"] for r in resp.json()["results"]]
        self.assertIn("V-ENT PHANTOMS", names)
        # Punctuation-insensitive: "vent" matches "V-ENT ..." via the normalized column.
        resp2 = self.client.get(
            "/leaderboards/standalone/search-ghost-teams/", {"q": "vent"}, **bearer(self.tok),
        )
        names2 = [r["team_name"] for r in resp2.json()["results"]]
        self.assertIn("V-ENT PHANTOMS", names2)
        self.assertNotIn("V-ENT RETIRED", names2)  # inactive hidden

    def test_ghost_player_search_with_team_name(self):
        # Leet fold: "shedoo" finds "SHED005" (0->o, 5->s), same behaviour as sitewide search.
        resp = self.client.get(
            "/leaderboards/standalone/search-ghost-players/", {"q": "shedoo"}, **bearer(self.tok),
        )
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ign"], "PH.SHED005")
        self.assertEqual(results[0]["ghost_team_name"], "V-ENT PHANTOMS")

    def test_short_query_returns_empty(self):
        resp = self.client.get(
            "/leaderboards/standalone/search-ghost-teams/", {"q": "v"}, **bearer(self.tok),
        )
        self.assertEqual(resp.json(), {"results": [], "total_count": 0})

    def test_auth_required(self):
        resp = self.client.get("/leaderboards/standalone/search-ghost-teams/", {"q": "phantom"})
        self.assertIn(resp.status_code, (400, 401, 403))
