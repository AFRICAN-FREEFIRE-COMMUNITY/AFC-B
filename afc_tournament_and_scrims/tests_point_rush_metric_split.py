"""
Regression guard for the TEAM point-rush metric split (owner 2026-06-29 review fix #2).

THE BUG
    Team qualification used to rank on TWO different metrics:
      • public leaderboard + advance_group  -> Sum(total_points)  (placement+kill+ASSIST+DAMAGE+bonus-penalty)
      • advance_round_robin + advancement_routing(team) + event_links + admin editor
                                            -> effective_total = Sum(placement+kill+bonus-penalty)
                                               (DROPS assist + damage points)
    So an event that awarded assist/damage points could send DIFFERENT teams forward depending on
    which surface you looked at — the leaderboard could show team B on top while round-robin /
    branching / cross-event advancement promoted team A.

THE FIX
    Every TEAM-ranking surface now ranks on the stored Sum(total_points) (which already includes
    assist + damage). The shared round_robin._aggregate_team_standings now sets
    effective_total = Sum(total_points), and the admin editor / per-group OVERALL builder do the
    same; advance_round_robin, advancement_routing(team) and event_links all consume that aggregator,
    so they inherit it. SOLO is unchanged (its stored total_points is placement+kill only).

THIS TEST
    Builds a one-match group where ASSIST points flip the order: team A out-scores team B on the OLD
    metric (placement+kill+bonus-penalty: A=12 > B=9) but team B wins once assists count
    (total_points: B=13 > A=12). It then asserts that EVERY team-ranking surface puts B first:
      1. round_robin.cumulative_standings  (the aggregator advance_round_robin + advancement_routing
         + event_links all rank on)         -> B, then A; B.effective_total == 13.
      2. the exact _fold_carry_over(...effective_total...) sort those three apply -> B first.
      3. the public leaderboard endpoint    -> overall_leaderboard[0] is B (effective_total 13).
      4. the advance_group endpoint          -> advances B (and NOT A) into the next stage.

    DB-backed (TestCase) because the whole point is the ORM aggregation + the endpoints; the pure
    formula already has parity tests in tests_scoring.py.
"""

import datetime
import json

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import round_robin
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageCompetitor,
    StageGroups,
    Stages,
    TournamentTeam,
)
from afc_tournament_and_scrims.views import _fold_carry_over


class PointRushMetricSplitTeamTests(TestCase):
    """All TEAM qualification surfaces must rank on Sum(total_points) (assist/damage included)."""

    def setUp(self):
        self.client = APIClient()

        # Admin user + forged session token (same pattern as the other DB tests in this app:
        # role="admin" clears the endpoints' `admin.role != "admin"` gate directly).
        self.admin = User.objects.create(
            username="prm_admin",
            email="prm_admin@example.com",
            full_name="Point Rush Admin",
            role="admin",
            password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin,
            token="prm-admin-token-1234567890",
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1),
        )

        self.today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament",
            participant_type="squad",  # squad -> the team branch of every ranking surface
            event_type="internal",
            max_teams_or_players=16,
            event_name="Assist Points Cup",
            event_mode="virtual",
            start_date=self.today,
            end_date=self.today,
            registration_open_date=self.today,
            registration_end_date=self.today,
            prizepool="0",
            event_rules="rules",
            event_status="ongoing",
            registration_link="https://example.com/reg",
            number_of_stages=2,
            creator=self.admin,
        )

        # Two teams. A out-places B (so A wins on the OLD placement-only metric); B out-assists A
        # by enough to overtake A once assist points count.
        self.team_a = Team.objects.create(
            team_name="Alpha", team_tag="ALP", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.team_b = Team.objects.create(
            team_name="Bravo", team_tag="BRV", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.tt_a = TournamentTeam.objects.create(
            event=self.event, team=self.team_a, registered_by=self.admin,
        )
        self.tt_b = TournamentTeam.objects.create(
            event=self.event, team=self.team_b, registered_by=self.admin,
        )

        # Scoring config carried on the match: 1 point per assist is what makes B overtake A.
        self.scoring = {
            "placement_points": {"1": 12, "2": 9, "3": 8},
            "kill_point": 1,
            "points_per_assist": 1,
            "points_per_1000_damage": 0,
        }

        # ── Stage 1: the group stage whose standings decide who advances ──
        self.stage1 = Stages.objects.create(
            event=self.event,
            stage_name="Group Stage",
            start_date=self.today,
            end_date=self.today,
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=1,
            stage_order=1,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage1,
            group_name="Lobby A",
            playing_date=self.today,
            playing_time=datetime.time(18, 0),
            teams_qualifying=1,  # top 1 advances -> exactly the team the metric decides
            match_count=1,
        )
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="Group Stage LB",
            event=self.event,
            stage=self.stage1,
            group=self.group,
            creator=self.admin,
            placement_points={"1": 12, "2": 9, "3": 8},
            kill_point=1.0,
            leaderboard_method="manual",
        )
        self.match = Match.objects.create(
            leaderboard=self.leaderboard,
            group=self.group,
            match_number=1,
            match_map="bermuda",
            scoring_settings=self.scoring,
        )

        # ── Stage 2: the target the group advances into (must exist for advance_group) ──
        self.stage2 = Stages.objects.create(
            event=self.event,
            stage_name="Finals",
            start_date=self.today,
            end_date=self.today,
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=1,
            stage_order=2,
        )

    # ── helpers ──────────────────────────────────────────────────────────────────────────────
    def _enter(self, rows):
        """POST one manual team result. `rows` = [(tournament_team, placement, kills, assists)].
        Assists are summed at the team level by the endpoint and folded into total_points via the
        match's points_per_assist, so we don't need real roster players here."""
        results = [
            {
                "tournament_team_id": tt.tournament_team_id,
                "placement": placement,
                "played": True,
                "players": [{"kills": kills, "damage": 0, "assists": assists, "played": True}],
            }
            for (tt, placement, kills, assists) in rows
        ]
        resp = self.client.post(
            "/events/enter-team-match-result-manual/",
            data={"match_id": self.match.match_id, "results": json.dumps(results)},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def _overall_leaderboard(self):
        """The stage-1 group's overall_leaderboard rows from the public endpoint."""
        resp = self.client.post(
            "/events/get-all-leaderboard-details-for-event/",
            data={"event_id": self.event.event_id},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()
        stage = next(s for s in payload["stages"] if s["stage_id"] == self.stage1.stage_id)
        return stage["groups"][0]["overall_leaderboard"]

    # ── the test ─────────────────────────────────────────────────────────────────────────────
    def test_assist_points_unify_team_ranking_across_every_surface(self):
        # A: placement 1 (12 pts), 0 kills, 0 assists -> total_points 12.
        # B: placement 2 (9 pts),  0 kills, 4 assists -> total_points 9 + 4 = 13.
        # OLD metric (placement+kill+bonus-penalty): A 12 > B 9  -> A would lead/advance.
        # NEW metric (Sum total_points):             B 13 > A 12 -> B leads/advances everywhere.
        self._enter([(self.tt_a, 1, 0, 0), (self.tt_b, 2, 0, 4)])

        # 1) The shared aggregator (advance_round_robin + advancement_routing(team) + event_links
        #    all rank on exactly this list). B must lead on the assist-inclusive total.
        standings = round_robin.cumulative_standings(self.stage1)
        self.assertEqual(standings[0]["tournament_team_id"], self.tt_b.tournament_team_id)
        self.assertEqual(standings[0]["effective_total"], 13)
        self.assertEqual(standings[1]["tournament_team_id"], self.tt_a.tournament_team_id)
        self.assertEqual(standings[1]["effective_total"], 12)

        # The bug only bites because assists count: confirm B would have LOST on the OLD metric
        # (placement+kill+bonus-penalty), i.e. the display columns still show B trailing A there.
        b_row = standings[0]
        a_row = standings[1]
        b_old = b_row["placement_sum"] + b_row["kill_sum"] + b_row["bonus_sum"] - b_row["penalty_sum"]
        a_old = a_row["placement_sum"] + a_row["kill_sum"] + a_row["bonus_sum"] - a_row["penalty_sum"]
        self.assertEqual((b_old, a_old), (9, 12))  # OLD metric reverses the new order
        self.assertLess(b_old, a_old)              # so B leads ONLY because total_points counts assists

        # 2) The exact re-rank step advance_round_robin / advancement_routing / event_links apply on
        #    those rows (no Point-Rush source here -> _fold_carry_over is a no-op, order preserved).
        ranked = _fold_carry_over(
            round_robin.cumulative_standings(self.stage1), self.stage1, "squad",
            id_key="tournament_team_id", metric_key="effective_total",
            sort_key=lambda r: (-int(r.get("effective_total") or 0),
                                -int(r.get("total_booyah") or 0),
                                -int(r.get("total_kills") or 0),
                                r.get("team_name") or ""),
        )
        self.assertEqual(ranked[0]["tournament_team_id"], self.tt_b.tournament_team_id)

        # 3) The public leaderboard endpoint must agree (overall_leaderboard top row is B at 13).
        overall = self._overall_leaderboard()
        self.assertEqual(overall[0]["tournament_team_id"], self.tt_b.tournament_team_id)
        self.assertEqual(overall[0]["effective_total"], 13)
        self.assertEqual(overall[1]["tournament_team_id"], self.tt_a.tournament_team_id)

        # 4) advance_group (the one surface that ranks Sum(total_points) directly, not via the
        #    aggregator) must advance B into the next stage — and not A.
        resp = self.client.post(
            "/events/advance-group-competitors-to-next-stage/",
            data={"event_id": self.event.event_id, "group_id": self.group.group_id},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        advanced_team_ids = set(
            StageCompetitor.objects.filter(stage=self.stage2)
            .values_list("tournament_team_id", flat=True)
        )
        self.assertIn(self.tt_b.tournament_team_id, advanced_team_ids)
        self.assertNotIn(self.tt_a.tournament_team_id, advanced_team_ids)
