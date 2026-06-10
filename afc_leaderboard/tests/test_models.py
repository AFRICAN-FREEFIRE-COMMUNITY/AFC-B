"""
Model-level tests for afc_leaderboard: the participant team-XOR-ghost CheckConstraint and the
display_name / is_ghost / kind helpers.
"""
from django.test import TestCase
from django.db import IntegrityError, transaction

from afc_rankings.models import GhostTeam, GhostPlayer
from afc_leaderboard.models import StandaloneLeaderboard, LeaderboardParticipant

from ._helpers import make_afc_admin, make_team, make_user


class ParticipantConstraintTests(TestCase):
    def setUp(self):
        self.admin, _ = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="LB", format="team", placement_points={"1": 12}, creator=self.admin
        )
        self.team = make_team("Real", self.admin)
        self.ghost = GhostTeam.objects.create(team_name="Ghost", country="NG", created_by=self.admin)

    def test_real_team_participant_ok(self):
        p = LeaderboardParticipant.objects.create(leaderboard=self.lb, team=self.team)
        self.assertEqual(p.team_id, self.team.team_id)
        self.assertFalse(p.is_ghost)
        self.assertEqual(p.kind, "real_team")
        self.assertEqual(p.display_name, "Real")

    def test_ghost_team_participant_ok(self):
        p = LeaderboardParticipant.objects.create(leaderboard=self.lb, ghost_team=self.ghost)
        self.assertIsNotNone(p.ghost_team_id)
        self.assertTrue(p.is_ghost)
        self.assertEqual(p.kind, "ghost_team")
        self.assertEqual(p.display_name, "Ghost")

    def test_cannot_set_both_team_and_ghost(self):
        # The CheckConstraint must reject a row with two entity FKs set.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LeaderboardParticipant.objects.create(
                    leaderboard=self.lb, team=self.team, ghost_team=self.ghost
                )

    def test_cannot_set_zero_entities(self):
        # The CheckConstraint must reject a row with NO entity FK set.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LeaderboardParticipant.objects.create(leaderboard=self.lb)

    def test_solo_user_and_ghost_player(self):
        solo_lb = StandaloneLeaderboard.objects.create(
            name="SoloLB", format="solo", placement_points={"1": 12}, creator=self.admin
        )
        u, _ = make_user("soloplayer")
        gp = GhostPlayer.objects.create(ign="GhostIGN")
        p_user = LeaderboardParticipant.objects.create(leaderboard=solo_lb, user=u)
        p_gp = LeaderboardParticipant.objects.create(leaderboard=solo_lb, ghost_player=gp)
        self.assertEqual(p_user.kind, "real_user")
        self.assertEqual(p_user.display_name, "soloplayer")
        self.assertEqual(p_gp.kind, "ghost_player")
        self.assertTrue(p_gp.is_ghost)
        self.assertEqual(p_gp.display_name, "GhostIGN")
