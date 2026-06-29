"""Tests for BRANCHING ADVANCEMENT ROUTING (feature #9).

Covers the engine (afc_tournament_and_scrims.advancement_routing.route_stage_advancement) and the
author-time validator (views._validate_advancement_rules). The engine ranks each
StageAdvancementRule's scope (one group, or the whole stage), slices [position_from..position_to],
and seeds those finishers into the rule's target stage via StageCompetitor.

Scenarios (owner plan WEBSITE/tasks/advancement-routing-plan.md):
  • per-group vs stage-wide ranking
  • position clamp (to past the field)
  • skip-ahead target (route into a stage two ahead)
  • idempotent (re-run never double-seeds)
  • target distribution does NOT pull raw registrations (only the routed rows land)
  • validator: overlap reject, cycle reject, clamp-not-error

Fixture idiom mirrors tests_round_robin.py (Event/Stages/Team/TournamentTeam/Match creates).

⚠️ These tests need the StageAdvancementRule table, created by migration
   0028_stageadvancementrule. The test runner builds its own test DB from all migrations, so it is
   safe to run WITHOUT touching the dev DB:

     .venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.tests_advancement_routing -v 2
"""
import datetime

from django.test import SimpleTestCase, TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, StageCompetitor, StageAdvancementRule,
    Leaderboard, Match, TournamentTeam, TournamentTeamMatchStats,
)
from afc_tournament_and_scrims.advancement_routing import route_stage_advancement
from afc_tournament_and_scrims.views import (
    _validate_advancement_rules, _wire_advancement_rules, _advancement_rules_echo,
)


D = datetime.date(2026, 6, 1)


def _event(creator, participant_type="squad"):
    return Event.objects.create(
        event_name="Advance Cup", competition_type="tournament",
        participant_type=participant_type, event_type="internal", max_teams_or_players=32,
        event_mode="virtual", start_date=D, end_date=D, registration_open_date=D,
        registration_end_date=D, prizepool="$1000", event_rules="rules",
        event_status="ongoing", registration_link="https://afc.test/reg",
        number_of_stages=2, creator=creator, is_draft=False)


def _stage(event, name, order, fmt="br - normal", qualifying=2):
    return Stages.objects.create(
        event=event, stage_name=name, start_date=D, end_date=D, number_of_groups=1,
        stage_format=fmt, teams_qualifying_from_stage=qualifying, stage_order=order)


class AdvancementEngineTests(TestCase):
    """End-to-end engine behaviour over a squad event: Group Stage -> Finals."""

    def setUp(self):
        self.admin = User.objects.create(
            username="adv_admin", email="adv_admin@afc.test", full_name="Adv Admin", role="admin")
        self.event = _event(self.admin)
        self.s0 = _stage(self.event, "Group Stage", order=1)
        self.s1 = _stage(self.event, "Play-In", order=2)
        self.s2 = _stage(self.event, "Finals", order=3)

        # One group in the entry stage + its leaderboard + one match to hang stats on.
        self.group = StageGroups.objects.create(
            stage=self.s0, group_name="Group A", playing_date=D,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1,
            match_maps=["bermuda"])
        self.lb = Leaderboard.objects.create(
            leaderboard_name="GS A", event=self.event, stage=self.s0, group=self.group,
            creator=self.admin, leaderboard_method="manual", placement_points={}, kill_point=1.0)
        self.match = Match.objects.create(
            leaderboard=self.lb, group=self.group, match_map="bermuda", match_number=1,
            result_inputted=True)

        # Four teams with DISTINCT scores so ranking is deterministic: T1 > T2 > T3 > T4.
        self.tts = []
        for i, pts in enumerate([40, 30, 20, 10], start=1):
            team = Team.objects.create(
                team_name=f"Team {i}", join_settings="open", team_creator=self.admin,
                team_owner=self.admin, team_captain=self.admin, country="Nigeria")
            tt = TournamentTeam.objects.create(event=self.event, team=team, status="active")
            TournamentTeamMatchStats.objects.create(
                match=self.match, tournament_team=tt, placement=i, kills=0,
                placement_points=pts, kill_points=0, total_points=pts)
            self.tts.append(tt)

    def _rule(self, frm, to, target, source_group=None, order=0):
        return StageAdvancementRule.objects.create(
            source_stage=self.s0, source_group=source_group, target_stage=target,
            position_from=frm, position_to=to, order=order)

    def test_stage_wide_top2_routes_into_target(self):
        # Top 1-2 of the whole stage -> Play-In. Exactly T1 + T2 should land there.
        self._rule(1, 2, self.s1)
        res = route_stage_advancement(self.s0)
        self.assertTrue(res["branching"])
        self.assertEqual(res["newly_seeded"], 2)
        seeded = set(StageCompetitor.objects.filter(stage=self.s1).values_list(
            "tournament_team_id", flat=True))
        self.assertEqual(seeded, {self.tts[0].tournament_team_id, self.tts[1].tournament_team_id})

    def test_split_routing_into_two_targets(self):
        # The branching headline: top 1-2 -> Finals, 3-4 -> Play-In, from one stage.
        self._rule(1, 2, self.s2, order=0)
        self._rule(3, 4, self.s1, order=1)
        res = route_stage_advancement(self.s0)
        finals = set(StageCompetitor.objects.filter(stage=self.s2).values_list(
            "tournament_team_id", flat=True))
        playin = set(StageCompetitor.objects.filter(stage=self.s1).values_list(
            "tournament_team_id", flat=True))
        self.assertEqual(finals, {self.tts[0].tournament_team_id, self.tts[1].tournament_team_id})
        self.assertEqual(playin, {self.tts[2].tournament_team_id, self.tts[3].tournament_team_id})

    def test_position_to_past_field_is_clamped(self):
        # to=99 with only 4 teams -> all 4 routed (no error, Python slice clamps).
        self._rule(1, 99, self.s1)
        res = route_stage_advancement(self.s0)
        self.assertEqual(res["newly_seeded"], 4)

    def test_per_group_scope_ranks_only_that_group(self):
        # A per-group rule ranks only the group's standings. Here the only group holds all four,
        # so top-1 = T1. (Proves the source_group path is exercised, not just stage-wide.)
        self._rule(1, 1, self.s1, source_group=self.group)
        res = route_stage_advancement(self.s0)
        seeded = list(StageCompetitor.objects.filter(stage=self.s1).values_list(
            "tournament_team_id", flat=True))
        self.assertEqual(seeded, [self.tts[0].tournament_team_id])

    def test_skip_ahead_target(self):
        # Route straight into Finals (two stages ahead), skipping Play-In.
        self._rule(1, 1, self.s2)
        route_stage_advancement(self.s0)
        self.assertTrue(StageCompetitor.objects.filter(
            stage=self.s2, tournament_team=self.tts[0]).exists())
        self.assertFalse(StageCompetitor.objects.filter(stage=self.s1).exists())

    def test_idempotent_rerun_does_not_double_seed(self):
        self._rule(1, 2, self.s1)
        route_stage_advancement(self.s0)
        res2 = route_stage_advancement(self.s0)
        self.assertEqual(res2["newly_seeded"], 0)
        self.assertEqual(res2["already_seeded"], 2)
        self.assertEqual(StageCompetitor.objects.filter(stage=self.s1).count(), 2)

    def test_dry_run_writes_nothing(self):
        self._rule(1, 2, self.s1)
        res = route_stage_advancement(self.s0, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["newly_seeded"], 0)
        self.assertEqual(StageCompetitor.objects.filter(stage=self.s1).count(), 0)
        # The preview still reports who WOULD route.
        names = [c["name"] for blk in res["routed"] for c in blk["competitors"]]
        self.assertIn("Team 1", names)
        self.assertIn("Team 2", names)

    def test_no_rules_is_noop(self):
        # A stage with no rules => engine returns branching=False and writes nothing (legacy path).
        res = route_stage_advancement(self.s0)
        self.assertFalse(res["branching"])
        self.assertEqual(StageCompetitor.objects.filter(stage=self.s1).count(), 0)

    def test_target_does_not_pull_all_registrations(self):
        # Critical: routing seeds ONLY the advanced rows, NOT the whole field. Top-1 only -> the
        # target stage must hold exactly ONE competitor even though 4 teams are registered.
        self._rule(1, 1, self.s1)
        route_stage_advancement(self.s0)
        self.assertEqual(StageCompetitor.objects.filter(stage=self.s1).count(), 1)
        # And the registrations themselves (TournamentTeam) are untouched (still 4 active).
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, status="active").count(), 4)


class AdvancementWiringTests(TestCase):
    """The create/edit SECOND-PASS glue: _wire_advancement_rules resolves payload indices to
    StageAdvancementRule rows, and _advancement_rules_echo serializes them back (resolving names +
    mapping FKs to the ids the edit form re-maps to indices)."""

    def setUp(self):
        self.admin = User.objects.create(
            username="wire_admin", email="wire@afc.test", full_name="Wire", role="admin")
        self.event = _event(self.admin)
        self.s0 = _stage(self.event, "Group Stage", order=1)
        self.s1 = _stage(self.event, "Finals", order=2)
        self.g0 = StageGroups.objects.create(
            stage=self.s0, group_name="Group A", playing_date=D,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1,
            match_maps=["bermuda"])

    def test_wire_then_echo_round_trip(self):
        # Payload: stage 0 has a stage-wide rule (top1-2 -> stage1) + a per-group rule (group0
        # top1 -> stage1). _wire_advancement_rules resolves indices -> FK rows.
        stages_data = [
            {
                "stage_name": "Group Stage",
                "groups": [{}],  # one group at index 0
                "advancement_rules": [
                    {"position_from": 1, "position_to": 2,
                     "source_group_index": None, "target_stage_index": 1},
                    {"position_from": 1, "position_to": 1,
                     "source_group_index": 0, "target_stage_index": 1},
                ],
            },
            {"stage_name": "Finals", "groups": []},
        ]
        _wire_advancement_rules([self.s0, self.s1], [[self.g0], []], stages_data)

        rules = list(StageAdvancementRule.objects.filter(source_stage=self.s0).order_by("order"))
        self.assertEqual(len(rules), 2)
        # rule 0: stage-wide -> source_group None, target = s1
        self.assertIsNone(rules[0].source_group_id)
        self.assertEqual(rules[0].target_stage_id, self.s1.stage_id)
        self.assertEqual((rules[0].position_from, rules[0].position_to), (1, 2))
        # rule 1: per-group -> source_group = g0
        self.assertEqual(rules[1].source_group_id, self.g0.group_id)

        # Echo resolves names + ids the FE re-maps.
        echo = _advancement_rules_echo(self.s0)
        self.assertEqual(len(echo), 2)
        self.assertEqual(echo[0]["target_stage_name"], "Finals")
        self.assertIsNone(echo[0]["source_group_id"])
        self.assertEqual(echo[1]["source_group_name"], "Group A")

    def test_wire_is_idempotent_replace(self):
        # Wiring twice (an edit re-save) REPLACES, never duplicates.
        sd = [
            {"stage_name": "GS", "groups": [{}], "advancement_rules": [
                {"position_from": 1, "position_to": 1,
                 "source_group_index": None, "target_stage_index": 1}]},
            {"stage_name": "Finals", "groups": []},
        ]
        _wire_advancement_rules([self.s0, self.s1], [[self.g0], []], sd)
        _wire_advancement_rules([self.s0, self.s1], [[self.g0], []], sd)
        self.assertEqual(
            StageAdvancementRule.objects.filter(source_stage=self.s0).count(), 1)

    def test_wire_empty_clears_rules(self):
        # An edit that removes all rules (advancement_rules omitted/empty) clears the stage's rows.
        StageAdvancementRule.objects.create(
            source_stage=self.s0, target_stage=self.s1, position_from=1, position_to=1)
        _wire_advancement_rules(
            [self.s0, self.s1],
            [[self.g0], []],
            [{"stage_name": "GS", "groups": [{}]}, {"stage_name": "Finals", "groups": []}],
        )
        self.assertEqual(
            StageAdvancementRule.objects.filter(source_stage=self.s0).count(), 0)


class AdvancementValidatorTests(SimpleTestCase):
    """Pure validator (no DB): overlap reject, cycle reject, clamp-not-error, range checks."""

    def _stage(self, name, rules=None, groups=2):
        s = {"stage_name": name, "groups": [{} for _ in range(groups)]}
        if rules is not None:
            s["advancement_rules"] = rules
        return s

    def test_valid_passes(self):
        data = [self._stage("GS", [{"position_from": 1, "position_to": 8,
                                    "source_group_index": None, "target_stage_index": 1}]),
                self._stage("Finals", groups=0)]
        self.assertIsNone(_validate_advancement_rules(data))

    def test_cycle_rejected(self):
        data = [self._stage("GS", [{"position_from": 1, "position_to": 8,
                                    "source_group_index": None, "target_stage_index": 0}]),
                self._stage("Finals", groups=0)]
        self.assertIsNotNone(_validate_advancement_rules(data))

    def test_overlap_rejected(self):
        data = [self._stage("GS", [
            {"position_from": 1, "position_to": 8, "source_group_index": None, "target_stage_index": 1},
            {"position_from": 5, "position_to": 10, "source_group_index": None, "target_stage_index": 1},
        ]), self._stage("Finals", groups=0)]
        self.assertIsNotNone(_validate_advancement_rules(data))

    def test_disjoint_ranges_same_scope_ok(self):
        data = [self._stage("GS", [
            {"position_from": 1, "position_to": 2, "source_group_index": None, "target_stage_index": 1},
            {"position_from": 3, "position_to": 4, "source_group_index": None, "target_stage_index": 1},
        ]), self._stage("Finals", groups=0)]
        self.assertIsNone(_validate_advancement_rules(data))

    def test_group_out_of_range_rejected(self):
        data = [self._stage("GS", [{"position_from": 1, "position_to": 2,
                                    "source_group_index": 5, "target_stage_index": 1}]),
                self._stage("Finals", groups=0)]
        self.assertIsNotNone(_validate_advancement_rules(data))
