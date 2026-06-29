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
    StageGroupCompetitor,
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
            # The 4 event/registration times are compulsory on create (owner 2026-06-21); without
            # them create-event 400s on the times check before any of this stage config is stored.
            "event_start_time": "18:00", "event_end_time": "20:00",
            "registration_start_time": "10:00", "registration_end_time": "17:00",
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
            # Times compulsory on create (owner 2026-06-21). Supply them so the 400 this test asserts
            # comes from the missing champion threshold, NOT the earlier required-times guard.
            "event_start_time": "18:00", "event_end_time": "20:00",
            "registration_start_time": "10:00", "registration_end_time": "17:00",
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
            # Times compulsory on create (owner 2026-06-21). Supply them so the 400 this test asserts
            # comes from the Point-Rush self-target rule, NOT the earlier required-times guard.
            "event_start_time": "18:00", "event_end_time": "20:00",
            "registration_start_time": "10:00", "registration_end_time": "17:00",
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


# ── Phase 2 (Task 4): the two PURE on-read helpers — no DB, no Django ORM. ──
# These pin the Champion-Point win rule and the Point-Rush per-lobby reward mapping in
# isolation, so the standings builder (Task 5) can lean on them without re-deriving the
# logic. SimpleTestCase: they touch no model, so the suite half that exercises them needs
# no MySQL.


class ChampionForGroupTests(SimpleTestCase):
    def _m(self, rows):
        # rows: list of (id, placement, points) for one match, in any order
        return {"rows": [{"id": i, "placement": p, "points": pts} for (i, p, pts) in rows]}

    def test_users_worked_example(self):
        # Pre-state: A=91, B=102, C=81 (all >= 80). Next match C booyahs -> C champion,
        # even though B has more points.
        m_pre = self._m([("A", 2, 91), ("B", 1, 102), ("C", 3, 81)])  # seeds running totals
        m_decide = self._m([("C", 1, 13), ("A", 2, 9), ("B", 3, 8)])  # C booyahs while >= 80
        self.assertEqual(scoring.champion_for_group([m_pre, m_decide], threshold=80), "C")

    def test_booyah_that_crosses_does_not_win(self):
        # A starts at 75 (below), booyahs to 88 in match1 -> NOT champion (crossed during it).
        # A booyahs again in match2 (now already >= 80 entering) -> champion.
        m1 = self._m([("A", 1, 13), ("B", 2, 9)])              # A: 0->13 ... below threshold pre-match
        # to make pre-match A=75 we seed via carry_over instead:
        champ = scoring.champion_for_group([m1], threshold=80, carry_over={"A": 75})
        self.assertIsNone(champ)  # pre-match A=75 < 80, so the crossing booyah doesn't win
        m2 = self._m([("A", 1, 5), ("B", 2, 9)])               # entering m2 A=88 >= 80, booyah -> win
        self.assertEqual(scoring.champion_for_group([m1, m2], threshold=80, carry_over={"A": 75}), "A")

    def test_carry_over_head_start_can_win_match_one(self):
        # carry_over alone >= threshold -> on match point before match 1; match-1 booyah wins.
        m1 = self._m([("A", 1, 5), ("B", 2, 9)])
        self.assertEqual(scoring.champion_for_group([m1], threshold=80, carry_over={"A": 80}), "A")

    def test_no_champion_returns_none(self):
        m1 = self._m([("A", 1, 12), ("B", 2, 9)])
        self.assertIsNone(scoring.champion_for_group([m1], threshold=80))


class RewardsFromStandingsTests(SimpleTestCase):
    def test_maps_reward_table_onto_ranked_ids(self):
        ranked = ["T1", "T2", "T3", "T4", "T5", "T6"]
        reward = {"1": 10, "2": 7, "3": 5}
        self.assertEqual(
            scoring.rewards_from_standings(ranked, reward),
            {"T1": 10, "T2": 7, "T3": 5},
        )

    def test_skips_placements_beyond_field_size(self):
        self.assertEqual(scoring.rewards_from_standings(["T1"], {"1": 10, "2": 7}), {"T1": 10})


# ── Phase 3 (Task 5): the standings BUILDER applies the two helpers ON READ. ──
# The pure helpers above are pinned in isolation; this exercises the full
# get_all_leaderboard_details_for_event endpoint (request -> ORM aggregate ->
# champion/carry-over overlay -> JSON) so a wiring bug in the view (wrong id key,
# unmaterialized queryset, missing re-sort) is caught. Squad event, real DB.


class GetAllLeaderboardDetailsScoringModesDBTests(TestCase):
    """End-to-end DB test for Task 5.

    Champion-Point case: a 2-match Finals lobby with threshold 20. Team B Booyahs
    match 1 to reach exactly 20 (crossing during the match, so NOT champion yet), then
    Booyahs match 2 while already at 20 entering it -> B is crowned. The endpoint must:
      - pin B to overall[0] with is_champion True (even though A could otherwise tie/beat
        it on raw points in a different scenario — here B leads anyway, but the crown flag
        and the is_decided group flag are what we assert),
      - mark the group payload is_decided True with champion_id == B.

    A separate plain stage (both toggles off) must be unchanged: standings ordered purely
    by points, no crown, no carry-over line. That guards the regression path.
    """

    def setUp(self):
        self.client = APIClient()

        # Admin user + forged session token (same pattern as the other DB tests in this
        # module: role="admin" passes the endpoint's `admin.role != "admin"` gate directly).
        self.admin = User.objects.create(
            username="lb_admin",
            email="lb_admin@example.com",
            full_name="Leaderboard Admin",
            role="admin",
            password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin,
            token="lb-admin-token-1234567890",
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1),
        )

        self.today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament",
            participant_type="squad",  # squad -> the team branch of the standings builder
            event_type="internal",
            max_teams_or_players=16,
            event_name="Champion Point Cup",
            event_mode="virtual",
            start_date=self.today,
            end_date=self.today,
            registration_open_date=self.today,
            registration_end_date=self.today,
            prizepool="0",
            event_rules="rules",
            event_status="ongoing",
            registration_link="https://example.com/reg",
            number_of_stages=1,
            creator=self.admin,
        )

        # Two teams that will receive results in both matches.
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

    def _make_stage_group(self, *, stage_name, champion_enabled, threshold, match_count):
        """Create a stage + one group + a leaderboard + `match_count` matches, all sharing
        the canonical scoring config. Returns (stage, group, [matches])."""
        stage = Stages.objects.create(
            event=self.event,
            stage_name=stage_name,
            start_date=self.today,
            end_date=self.today,
            number_of_groups=1,
            stage_format="br - normal",
            teams_qualifying_from_stage=1,
            champion_point_enabled=champion_enabled,
            champion_point_threshold=threshold,
        )
        group = StageGroups.objects.create(
            stage=stage,
            group_name=f"{stage_name} Lobby",
            playing_date=self.today,
            playing_time=datetime.time(18, 0),
            teams_qualifying=1,
            match_count=match_count,
        )
        leaderboard = Leaderboard.objects.create(
            leaderboard_name=f"{stage_name} LB",
            event=self.event,
            stage=stage,
            group=group,
            creator=self.admin,
            placement_points={"1": 12, "2": 9},
            kill_point=1.0,
            leaderboard_method="manual",
        )
        matches = [
            Match.objects.create(
                leaderboard=leaderboard,
                group=group,
                match_number=n,
                match_map="bermuda",
                scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1},
            )
            for n in range(1, match_count + 1)
        ]
        return stage, group, matches

    def _enter(self, match, rows):
        """POST one manual team result. `rows` is a list of (tournament_team, placement, kills)."""
        results = [
            {
                "tournament_team_id": tt.tournament_team_id,
                "placement": placement,
                "played": True,
                "players": [{"kills": kills, "damage": 0, "assists": 0, "played": True}],
            }
            for (tt, placement, kills) in rows
        ]
        resp = self.client.post(
            "/events/enter-team-match-result-manual/",
            data={"match_id": match.match_id, "results": json.dumps(results)},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def _fetch_details(self):
        resp = self.client.post(
            "/events/get-all-leaderboard-details-for-event/",
            data={"event_id": self.event.event_id},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _group_for_stage(self, payload, stage_id):
        stage = next(s for s in payload["stages"] if s["stage_id"] == stage_id)
        return stage["groups"][0]

    def test_champion_point_stage_crowns_and_marks_decided(self):
        # Champion-Point Finals: threshold 20, two matches.
        stage, group, matches = self._make_stage_group(
            stage_name="Finals", champion_enabled=True, threshold=20, match_count=2,
        )

        # Match 1: B booyahs to exactly 20 (12 placement + 8 kills). B was 0 entering, so this
        # crossing booyah must NOT crown B yet. A places 2nd (9 pts).
        self._enter(matches[0], [(self.tt_b, 1, 8), (self.tt_a, 2, 0)])
        # Match 2: B booyahs again while ALREADY at 20 entering -> B is the champion.
        self._enter(matches[1], [(self.tt_b, 1, 0), (self.tt_a, 2, 0)])

        grp = self._group_for_stage(self._fetch_details(), stage.stage_id)
        overall = grp["overall_leaderboard"]

        # B sits at #0 and is flagged champion; the group reports the stage as decided.
        self.assertEqual(overall[0]["tournament_team_id"], self.tt_b.tournament_team_id)
        self.assertTrue(overall[0]["is_champion"])
        self.assertTrue(grp["is_decided"])
        self.assertEqual(grp["champion_id"], self.tt_b.tournament_team_id)
        self.assertEqual(grp["champion_point_threshold"], 20)
        # A is not the champion.
        a_row = next(r for r in overall if r["tournament_team_id"] == self.tt_a.tournament_team_id)
        self.assertFalse(a_row["is_champion"])

    def test_plain_stage_is_unchanged(self):
        # Both toggles off: pure points order, no crown, not decided.
        stage, group, matches = self._make_stage_group(
            stage_name="Group Stage", champion_enabled=False, threshold=None, match_count=1,
        )
        # A wins the lobby on points (12 placement + 5 kills = 17); B trails (9 + 0 = 9).
        self._enter(matches[0], [(self.tt_a, 1, 5), (self.tt_b, 2, 0)])

        grp = self._group_for_stage(self._fetch_details(), stage.stage_id)
        overall = grp["overall_leaderboard"]

        # Ordered by points: A first, B second — exactly as before this feature.
        self.assertEqual(overall[0]["tournament_team_id"], self.tt_a.tournament_team_id)
        self.assertEqual(overall[0]["effective_total"], 17)
        self.assertEqual(overall[1]["tournament_team_id"], self.tt_b.tournament_team_id)
        # No champion crowned, group not decided, carry-over is zero for every row.
        self.assertFalse(grp["is_decided"])
        self.assertIsNone(grp["champion_id"])
        self.assertFalse(grp["champion_point_enabled"])
        for r in overall:
            self.assertFalse(r["is_champion"])
            self.assertEqual(r["carry_over_points"], 0)

    def test_plain_stage_tie_preserves_db_tiebreaker_order(self):
        # Regression guard for the unconditional-resort bug: on a PLAIN stage (both toggles
        # off, no carry-over), two teams tied on effective_total must stay in the DB's full
        # tiebreaker order (-total_booyah, then -total_kills, ...). The old code re-sorted
        # every stage by (effective_total, total_kills) only — which promotes kills ABOVE
        # booyahs and can flip which team leads (and therefore which team the FE's positional
        # qualified/eliminated badge marks). test_plain_stage_is_unchanged never hits this
        # path because its two teams differ on points (17 vs 9).
        #
        # Construct the tie: 2 matches, both teams end on effective_total 21, but A has the
        # Booyah edge (1 booyah) while B has the kill edge (more total kills). The DB orders
        # by -total_booyah BEFORE -total_kills, so A must lead.
        #   Team A: M1 placement 1 (12) + 0 kills = 12 ; M2 placement 2 (9) + 0 kills = 9  -> 21, booyah=1, kills=0
        #   Team B: M1 placement 2 (9)  + 0 kills = 9  ; M2 placement 2... can't both be 2nd.
        # Use distinct placements per match so each match is internally consistent:
        #   M1: A=1st (12 pts, booyah), B=2nd (9 pts)            -> A:12  B:9
        #   M2: A=2nd (9 pts), B=1st (12 pts, booyah)            -> A:21  B:21  (now both booyah=1)
        # That ties booyah too. To isolate the kills-vs-booyah ordering we instead give B the
        # kills and keep A's single booyah unique:
        #   M1: A=1st (12, booyah, 0 kills), B=2nd (9, 0 kills)  -> A:12 (b=1,k=0)  B:9  (b=0,k=0)
        #   M2: A=3rd (8, 0 kills),          B=2nd (9, 3 kills)  -> A:20 (b=1,k=0)  B:21 (b=0,k=3)
        # That doesn't tie effective_total. Simplest clean tie that separates booyah from kills:
        #   M1: A=1st (12, booyah, 0k), B=2nd (9, 3k)            -> A:12 (b=1,k=0)  B:12 (b=0,k=3)
        # Single match, both at 12, A has the booyah and B has the kills. DB ranks A first
        # (booyah beats kills); the buggy resort would rank B first (kills only).
        stage, group, matches = self._make_stage_group(
            stage_name="Tie Stage", champion_enabled=False, threshold=None, match_count=1,
        )
        # A: placement 1 (12 pts) + 0 kills = 12, booyah=1.  B: placement 2 (9 pts) + 3 kills = 12, booyah=0.
        self._enter(matches[0], [(self.tt_a, 1, 0), (self.tt_b, 2, 3)])

        grp = self._group_for_stage(self._fetch_details(), stage.stage_id)
        overall = grp["overall_leaderboard"]

        # Both tied on effective_total = 12.
        self.assertEqual(overall[0]["effective_total"], 12)
        self.assertEqual(overall[1]["effective_total"], 12)
        # A must lead on the booyah tiebreaker (DB order), NOT B on kills (the old buggy order).
        self.assertEqual(overall[0]["tournament_team_id"], self.tt_a.tournament_team_id)
        self.assertEqual(overall[1]["tournament_team_id"], self.tt_b.tournament_team_id)


# ── Gap 2 (Point-Rush carry-over on the PUBLIC leaderboard). ─────────────────────────────────────
# The Point-Rush carry-over is computed ON READ (never persisted) by views._carry_over_for_stage and
# was already folded into the ADMIN results editor (get_all_leaderboard_details_for_event). It was
# MISSING from the two user-facing endpoints (get_event_details / get_event_details_not_logged_in),
# so from a normal viewer's seat the banked points "didn't carry over" into the connected stage.
# views._apply_public_carry_over now applies the SAME overlay to the public rows. These tests build a
# real 2-stage squad event (Semis -> Finals, Point-Rush reward {1:10, 2:7}), enter Semis results, seed
# both teams into Finals, and prove the carry-over surfaces on the public path. TestCase => rolled back.


class PublicCarryOverDBTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Admin user + forged session token (role="admin" -> the manual-entry endpoint's _is_event_admin
        # short-circuits True; get_event_details is public/optional-auth so it needs no token at all).
        self.admin = User.objects.create(
            username="pub_admin", email="pub_admin@example.com",
            full_name="Pub Admin", role="admin", password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin, token="pub-admin-token-1234567890",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )

        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Carry Over Public Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="rules", event_status="ongoing",
            registration_link="https://example.com/reg", number_of_stages=2, creator=self.admin,
            results_published=True,  # public overall_leaderboard is withheld ([]) when this is False
        )

        # Finals is the carry-over TARGET; Semis is the Point-Rush SOURCE pointing at it.
        self.finals = Stages.objects.create(
            event=self.event, stage_name="Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1, stage_order=2,
        )
        self.semis = Stages.objects.create(
            event=self.event, stage_name="Semis", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1,
            point_rush_enabled=True, point_rush_reward={"1": 10, "2": 7},
            point_rush_target_stage=self.finals,
        )

        # Semis group + leaderboard + one match (real results entered here).
        self.semis_group = StageGroups.objects.create(
            stage=self.semis, group_name="Semis A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1,
        )
        self.semis_lb = Leaderboard.objects.create(
            leaderboard_name="Semis LB", event=self.event, stage=self.semis, group=self.semis_group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual",
        )
        self.semis_match = Match.objects.create(
            leaderboard=self.semis_lb, group=self.semis_group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1},
        )

        # Finals group + leaderboard. No Finals results: teams are SEEDED in (StageGroupCompetitor),
        # so they show on the public Finals standings at 0 points + then receive the carry-over.
        self.finals_group = StageGroups.objects.create(
            stage=self.finals, group_name="Finals A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=1, match_count=1,
        )
        self.finals_lb = Leaderboard.objects.create(
            leaderboard_name="Finals LB", event=self.event, stage=self.finals, group=self.finals_group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual",
        )

        self.team_a = Team.objects.create(
            team_name="Alpha", team_tag="ALP", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.team_b = Team.objects.create(
            team_name="Bravo", team_tag="BRV", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.tt_a = TournamentTeam.objects.create(event=self.event, team=self.team_a, registered_by=self.admin)
        self.tt_b = TournamentTeam.objects.create(event=self.event, team=self.team_b, registered_by=self.admin)

        # Seed both into the Finals group so they appear in the public Finals standings at 0.
        StageGroupCompetitor.objects.create(stage_group=self.finals_group, tournament_team=self.tt_a)
        StageGroupCompetitor.objects.create(stage_group=self.finals_group, tournament_team=self.tt_b)

    def _enter_semis(self):
        # A wins Semis (placement 1 -> 12 pts), B 2nd (placement 2 -> 9 pts). So in the Semis group
        # standings A ranks 1st (reward 10) and B 2nd (reward 7).
        results = [
            {"tournament_team_id": self.tt_a.tournament_team_id, "placement": 1, "played": True,
             "players": [{"kills": 0, "damage": 0, "assists": 0, "played": True}]},
            {"tournament_team_id": self.tt_b.tournament_team_id, "placement": 2, "played": True,
             "players": [{"kills": 0, "damage": 0, "assists": 0, "played": True}]},
        ]
        resp = self.client.post(
            "/events/enter-team-match-result-manual/",
            data={"match_id": self.semis_match.match_id, "results": json.dumps(results)},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_carry_over_dict_off_live_semis_standings(self):
        # _carry_over_for_stage(Finals) must read the LIVE Semis lobby standings and map the reward
        # table onto them: A (1st) -> 10, B (2nd) -> 7.
        from afc_tournament_and_scrims import views
        self._enter_semis()
        carry = views._carry_over_for_stage(self.finals, "squad")
        self.assertEqual(carry.get(self.tt_a.tournament_team_id), 10)
        self.assertEqual(carry.get(self.tt_b.tournament_team_id), 7)

    def test_apply_public_carry_over_folds_and_reorders(self):
        # The helper stamps carry_over_points, folds it into total_points, and re-sorts so the bigger
        # carry-over leads — even though the input list had B first.
        from afc_tournament_and_scrims import views
        self._enter_semis()
        rows = [
            {"tournament_team_id": self.tt_b.tournament_team_id,
             "tournament_team__team__team_name": "Bravo", "total_kills": 0, "total_points": 0},
            {"tournament_team_id": self.tt_a.tournament_team_id,
             "tournament_team__team__team_name": "Alpha", "total_kills": 0, "total_points": 0},
        ]
        views._apply_public_carry_over(self.finals, rows, "squad")
        by_id = {r["tournament_team_id"]: r for r in rows}
        self.assertEqual(by_id[self.tt_a.tournament_team_id]["carry_over_points"], 10)
        self.assertEqual(by_id[self.tt_a.tournament_team_id]["total_points"], 10)
        self.assertEqual(by_id[self.tt_b.tournament_team_id]["carry_over_points"], 7)
        self.assertEqual(by_id[self.tt_b.tournament_team_id]["total_points"], 7)
        # A (10) now leads B (7), despite B being first in the input list.
        self.assertEqual(rows[0]["tournament_team_id"], self.tt_a.tournament_team_id)

    def test_no_point_rush_source_is_noop(self):
        # Applied to a stage that NO source targets (Semis itself), the overlay stamps carry_over_points
        # 0 everywhere, leaves total_points untouched, and preserves the existing order. Guards the
        # common no-Point-Rush event from any reordering regression.
        from afc_tournament_and_scrims import views
        self._enter_semis()
        rows = [
            {"tournament_team_id": self.tt_b.tournament_team_id,
             "tournament_team__team__team_name": "Bravo", "total_kills": 5, "total_points": 9},
            {"tournament_team_id": self.tt_a.tournament_team_id,
             "tournament_team__team__team_name": "Alpha", "total_kills": 0, "total_points": 12},
        ]
        before = [r["tournament_team_id"] for r in rows]
        views._apply_public_carry_over(self.semis, rows, "squad")
        self.assertTrue(all(r["carry_over_points"] == 0 for r in rows))
        self.assertEqual([r["tournament_team_id"] for r in rows], before)  # order preserved
        self.assertEqual(rows[0]["total_points"], 9)  # unchanged

    def test_public_endpoint_surfaces_carry_over(self):
        # End-to-end: the PUBLIC get_event_details endpoint must now carry the bonus into the Finals
        # group standings (id + name + total_points + carry_over_points), with A leading on its 10.
        self._enter_semis()
        resp = self.client.post("/events/get-event-details/", data={"slug": self.event.slug})
        self.assertEqual(resp.status_code, 200, resp.content)
        stages = resp.json()["event_details"]["stages"]
        finals = next(s for s in stages if s["stage_id"] == self.finals.stage_id)
        overall = finals["groups"][0]["overall_leaderboard"]
        by_id = {r["tournament_team_id"]: r for r in overall}
        self.assertEqual(by_id[self.tt_a.tournament_team_id]["carry_over_points"], 10)
        self.assertEqual(by_id[self.tt_a.tournament_team_id]["total_points"], 10)
        self.assertEqual(by_id[self.tt_b.tournament_team_id]["carry_over_points"], 7)
        self.assertEqual(by_id[self.tt_b.tournament_team_id]["total_points"], 7)
        self.assertEqual(overall[0]["tournament_team_id"], self.tt_a.tournament_team_id)

    def test_public_endpoint_withholds_when_results_unpublished(self):
        # When results are not published the public overall is []; the carry-over overlay must not
        # resurrect rows (regression guard for the results-visibility gate).
        self._enter_semis()
        self.event.results_published = False
        self.event.save(update_fields=["results_published"])
        resp = self.client.post("/events/get-event-details/", data={"slug": self.event.slug})
        self.assertEqual(resp.status_code, 200, resp.content)
        stages = resp.json()["event_details"]["stages"]
        finals = next(s for s in stages if s["stage_id"] == self.finals.stage_id)
        self.assertEqual(finals["groups"][0]["overall_leaderboard"], [])
