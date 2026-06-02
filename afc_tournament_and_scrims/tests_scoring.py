"""
Parity tests for afc_tournament_and_scrims.scoring.

These pin the new shared point formula (scoring.compute_team_points /
compute_solo_points) to the exact numbers the live inline code in views.py has
always produced — so the upcoming refactor that routes every call site through
scoring.* cannot silently change a single stored score.

They are SimpleTestCase (no DB) on purpose: the formula is pure arithmetic, so
the suite runs without MySQL / migrations.
"""

import datetime
import json

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import scoring
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMatchStats,
)


class ComputeTeamPointsTests(SimpleTestCase):
    def setUp(self):
        # the canonical FF table = scoring.DEFAULT_PLACEMENT
        self.pp = scoring.DEFAULT_PLACEMENT

    def test_team_played_placement_plus_kills(self):
        # mirrors views.py:12596-12611 exactly
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=1, kills=8, damage=0, assists=0,
            bonus=0, penalty=0, played=True,
        )
        self.assertEqual(r, {"placement_points": 12, "kill_points": 8, "total_points": 20})

    def test_team_bonus_and_penalty(self):
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=3, kills=4, damage=0, assists=0,
            bonus=5, penalty=2, played=True,
        )
        # 8 + 4 + 5 - 2 = 15
        self.assertEqual(r["total_points"], 15)

    def test_team_assist_and_damage_points(self):
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.5,
            points_per_1000_damage=2.0, placement=2, kills=3, damage=3000, assists=4,
            bonus=0, penalty=0, played=True,
        )
        # 9 + 3 + (4*0.5=2) + (3000/1000*2=6) = 20 -> int
        self.assertEqual(r["total_points"], 20)

    def test_team_not_played_is_zero_minus_penalty(self):
        # Pre-refactor parity: a not-played team scores no placement points (played=False
        # zeroes placement_pts), and the live manual/edit callers pre-zero kills/damage/assists
        # (played_players is filtered to played==True), so those terms are 0 too. But bonus/penalty
        # still fold into the total — the live paths read bonus_points/penalty_points off the
        # payload even for a not-played team and stored total_points = bonus - penalty. Mirror that
        # call shape (zeroed kills/damage/assists, a real penalty) and prove (a) a winning placement
        # does not leak through, (b) the total is exactly bonus - penalty so it reconciles with the
        # stored bonus_points/penalty_points columns. This is the original Task-1 parity test
        # (named test_team_not_played_is_zero_minus_penalty), restored after a guard regression.
        r = scoring.compute_team_points(
            placement_points=self.pp, kill_point=1.0, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=1, kills=0, damage=0, assists=0,
            bonus=0, penalty=3, played=False,
        )
        # placement_pts=0 (not played) + kill_pts=0 + bonus 0 - penalty 3 => total = -3
        self.assertEqual(r, {"placement_points": 0, "kill_points": 0, "total_points": -3})


class ComputeSoloPointsTests(SimpleTestCase):
    def test_solo_played(self):
        # mirrors views.py:13118-13120
        r = scoring.compute_solo_points(
            placement_points=scoring.DEFAULT_PLACEMENT, kill_point=1.0,
            placement=1, kills=5, played=True,
        )
        self.assertEqual(r, {"placement_points": 12, "kill_points": 5, "total_points": 17})

    def test_solo_not_played_zeroes_real_input(self):
        # Stronger than "0 in -> 0 out": feed a winning placement + kills with played=False
        # and prove the guard zeroes everything.
        r = scoring.compute_solo_points(
            placement_points=scoring.DEFAULT_PLACEMENT, kill_point=1.0,
            placement=1, kills=5, played=False,
        )
        self.assertEqual(r, {"placement_points": 0, "kill_points": 0, "total_points": 0})


class NormalizePlacementPointsTests(SimpleTestCase):
    """scoring.normalize_placement_points is now the single normalizer (replaces the old
    per-call-site _normalize_* copies). These pin its three branches: empty->default,
    str-keys->int-keys, non-dict->loud ValueError."""

    def test_empty_falls_back_to_default(self):
        # empty / falsy input -> the canonical FF table, not an empty dict
        self.assertEqual(scoring.normalize_placement_points({}), scoring.DEFAULT_PLACEMENT)
        self.assertEqual(scoring.normalize_placement_points(None), scoring.DEFAULT_PLACEMENT)

    def test_string_keys_and_values_coerced_to_int(self):
        # stored JSON arrives with string keys/values; normalizer returns int->int
        self.assertEqual(scoring.normalize_placement_points({"1": "12"}), {1: 12})

    def test_non_dict_raises_value_error(self):
        # fail loud on a structurally wrong payload (e.g. a list) instead of silently defaulting
        with self.assertRaises(ValueError):
            scoring.normalize_placement_points([1, 2, 3])


class EnterTeamMatchResultManualDBTests(TestCase):
    """End-to-end DB regression for the manual team-result entry endpoint.

    Task 2 routes enter_team_match_result_manual's point calc through
    scoring.compute_team_points. The pure ComputeTeamPointsTests above pin the
    formula in isolation; this one proves the *endpoint* still stores the exact
    pre-refactor numbers through the full request → transaction → DB write path
    (the SimpleTestCase suite never touches the DB or the view, so a wiring bug
    in the view — wrong column, swapped argument — would slip past it).

    Canonical case from the plan: placement 1 (=12 placement points) + 8 kills
    at kill_point 1 => total_points 20.
    """

    def setUp(self):
        self.client = APIClient()

        # ── Admin user + a live session token (validate_token reads SessionToken). ──
        self.admin = User.objects.create(
            username="score_admin",
            email="score_admin@example.com",
            full_name="Score Admin",
            role="admin",  # _is_event_admin short-circuits to True, so the org path is skipped
            password="x",  # not used: we forge the session token directly, no login round-trip
        )
        self.token = SessionToken.objects.create(
            user=self.admin,
            token="score-admin-token-1234567890",
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1),
        )

        # ── A minimal squad event → stage → group → leaderboard → match. ──
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament",
            participant_type="squad",  # team endpoint rejects participant_type == "solo"
            event_type="internal",
            max_teams_or_players=16,
            event_name="Scoring Regression Cup",
            event_mode="virtual",
            start_date=today,
            end_date=today,
            registration_open_date=today,
            registration_end_date=today,
            prizepool="0",
            event_rules="rules",
            event_status="ongoing",
            registration_link="https://example.com/reg",
            number_of_stages=1,
            creator=self.admin,
        )
        self.stage = Stages.objects.create(
            event=self.event,
            stage_name="Group Stage",
            start_date=today,
            end_date=today,
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=1,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage,
            group_name="Group A",
            playing_date=today,
            playing_time=datetime.time(18, 0),
            teams_qualifying=1,
            match_count=1,
        )
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="Group A LB",
            event=self.event,
            stage=self.stage,
            group=self.group,
            creator=self.admin,
            placement_points={"1": 12, "2": 9, "3": 8},
            kill_point=1.0,
            leaderboard_method="manual",
        )
        # The endpoint reads the per-match scoring config from match.scoring_settings,
        # so seed it with the canonical placement table + kill_point.
        self.match = Match.objects.create(
            leaderboard=self.leaderboard,
            group=self.group,
            match_number=1,
            match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9, "3": 8}, "kill_point": 1},
        )

        # ── One tournament team to receive the result. ──
        self.team = Team.objects.create(
            team_name="Alpha",
            team_tag="ALP",
            join_settings="open",
            team_creator=self.admin,
            team_owner=self.admin,
            country="NG",
        )
        self.tt = TournamentTeam.objects.create(
            event=self.event,
            team=self.team,
            registered_by=self.admin,
        )

    def test_manual_team_entry_stores_canonical_total(self):
        # Arrange: placement 1 + 8 kills (kill_point 1) => 12 + 8 = 20.
        payload = {
            "match_id": self.match.match_id,
            "results": json.dumps([
                {
                    "tournament_team_id": self.tt.tournament_team_id,
                    "placement": 1,
                    "played": True,
                    "players": [{"kills": 8, "damage": 0, "assists": 0, "played": True}],
                }
            ]),
        }

        # Act
        resp = self.client.post(
            "/events/enter-team-match-result-manual/",
            data=payload,
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

        # Assert: request succeeded and the stored row carries the pre-refactor numbers.
        self.assertEqual(resp.status_code, 200, resp.content)
        stat = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=self.tt
        )
        self.assertEqual(stat.placement_points, 12)
        self.assertEqual(stat.kill_points, 8)
        self.assertEqual(stat.total_points, 20)


class CreateEventScoringModesDBTests(TestCase):
    """End-to-end DB test for Task 3: the create-event endpoint must store the new
    per-stage scoring-mode config and wire the Point-Rush carry-over target by index.

    The endpoint receives a JSON-stringified `stages` array. The target stage is
    referenced by `point_rush_target_index` (0-based position in that array) because the
    target stage row does not exist yet while the source stage is being created — so the
    view resolves it in a second pass. This proves that second pass links source→target.
    """

    def setUp(self):
        self.client = APIClient()

        # Admin user + forged session token (same pattern as the manual-entry DB test:
        # _is_event_admin short-circuits True for role="admin", skipping the org gate).
        self.admin = User.objects.create(
            username="event_admin",
            email="event_admin@example.com",
            full_name="Event Admin",
            role="admin",
            password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin,
            token="event-admin-token-1234567890",
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1),
        )

    def test_create_event_stores_scoring_modes_and_links_carry_over_target(self):
        # Arrange: a 2-stage squad event.
        #   stage 0 "Semis"  -> Point-Rush ON, reward {1:10,2:7,3:5}, target = index 1 (Finals)
        #   stage 1 "Finals" -> Champion-Point ON, threshold 80
        today = datetime.date.today().isoformat()
        stages = [
            {
                "stage_name": "Semis",
                "start_date": today,
                "end_date": today,
                "number_of_groups": 1,
                "stage_format": "br - normal",
                "teams_qualifying_from_stage": 4,
                "groups": [],
                "point_rush_enabled": True,
                "point_rush_reward": {"1": 10, "2": 7, "3": 5},
                "point_rush_target_index": 1,  # carry over into the Finals stage (index 1)
            },
            {
                "stage_name": "Finals",
                "start_date": today,
                "end_date": today,
                "number_of_groups": 1,
                "stage_format": "br - normal",
                "teams_qualifying_from_stage": 1,
                "groups": [],
                "champion_point_enabled": True,
                "champion_point_threshold": 80,
            },
        ]
        payload = {
            "competition_type": "tournament",
            "participant_type": "squad",
            "event_type": "internal",
            "max_teams_or_players": 16,
            "event_name": "Scoring Modes Cup",
            "event_mode": "virtual",
            "start_date": today,
            "end_date": today,
            "registration_open_date": today,
            "registration_end_date": today,
            "prizepool": "0",
            "number_of_stages": 2,
            "is_draft": "false",
            "stages": json.dumps(stages),
        }

        # Act
        resp = self.client.post(
            "/events/create-event/",
            data=payload,
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

        # Assert: request succeeded and both stages persisted with the right scoring config.
        self.assertEqual(resp.status_code, 201, resp.content)
        event_id = resp.json()["event_id"]

        semis = Stages.objects.get(event_id=event_id, stage_name="Semis")
        finals = Stages.objects.get(event_id=event_id, stage_name="Finals")

        # stage 0 (Semis): Point-Rush stored, champion-point left off/default
        self.assertTrue(semis.point_rush_enabled)
        self.assertEqual(semis.point_rush_reward, {"1": 10, "2": 7, "3": 5})
        self.assertFalse(semis.champion_point_enabled)
        self.assertIsNone(semis.champion_point_threshold)

        # stage 1 (Finals): Champion-Point stored, point-rush left off/default
        self.assertTrue(finals.champion_point_enabled)
        self.assertEqual(finals.champion_point_threshold, 80)
        self.assertFalse(finals.point_rush_enabled)

        # The second pass must link the Semis carry-over target to the Finals stage.
        self.assertEqual(semis.point_rush_target_stage_id, finals.stage_id)
        # ...and the reverse related_name resolves the source back from the target.
        self.assertIn(semis, list(finals.point_rush_sources.all()))

    def test_create_event_rejects_champion_point_without_threshold(self):
        # Champion-Point on but no (positive) threshold -> 400, nothing written.
        today = datetime.date.today().isoformat()
        stages = [{
            "stage_name": "Finals",
            "start_date": today,
            "end_date": today,
            "number_of_groups": 1,
            "stage_format": "br - normal",
            "teams_qualifying_from_stage": 1,
            "groups": [],
            "champion_point_enabled": True,
            # champion_point_threshold deliberately omitted
        }]
        payload = {
            "competition_type": "tournament",
            "participant_type": "squad",
            "event_type": "internal",
            "max_teams_or_players": 16,
            "event_name": "Bad Champion Cup",
            "event_mode": "virtual",
            "start_date": today,
            "end_date": today,
            "registration_open_date": today,
            "registration_end_date": today,
            "prizepool": "0",
            "number_of_stages": 1,
            "is_draft": "false",
            "stages": json.dumps(stages),
        }
        resp = self.client.post(
            "/events/create-event/",
            data=payload,
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        # fail-fast: no event row was created
        self.assertFalse(Event.objects.filter(event_name="Bad Champion Cup").exists())

    def test_create_event_rejects_point_rush_self_target(self):
        # Point-Rush targeting its own index -> 400 (carry-over only flows to a later stage).
        today = datetime.date.today().isoformat()
        stages = [{
            "stage_name": "Semis",
            "start_date": today,
            "end_date": today,
            "number_of_groups": 1,
            "stage_format": "br - normal",
            "teams_qualifying_from_stage": 4,
            "groups": [],
            "point_rush_enabled": True,
            "point_rush_reward": {"1": 10},
            "point_rush_target_index": 0,  # self-target -> rejected
        }]
        payload = {
            "competition_type": "tournament",
            "participant_type": "squad",
            "event_type": "internal",
            "max_teams_or_players": 16,
            "event_name": "Self Target Cup",
            "event_mode": "virtual",
            "start_date": today,
            "end_date": today,
            "registration_open_date": today,
            "registration_end_date": today,
            "prizepool": "0",
            "number_of_stages": 1,
            "is_draft": "false",
            "stages": json.dumps(stages),
        }
        resp = self.client.post(
            "/events/create-event/",
            data=payload,
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Event.objects.filter(event_name="Self Target Cup").exists())
