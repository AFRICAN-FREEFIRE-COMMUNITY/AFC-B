"""Tests for the BR Round-Robin stage format (sub-project B).

A Round-Robin stage keeps *base groups* (A/B/C…) as the stable team identity, while
each game-day *lobby* (a `StageGroups` row) is formed by merging one or more base
groups. This test pins down the new schema introduced in Task 1:
  • the `RoundRobinGroup` model (base group + its `teams` M2M), and
  • the `StageGroups.game_day` / `StageGroups.source_groups` fields (the lobby side),
asserting every reverse accessor the rest of the feature relies on resolves.

Fixture idiom mirrors `seed_scoring_demo.py` (Event/Stages/Team/TournamentTeam creates).

Run: .venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.tests_round_robin -v 2
"""
import datetime

from django.test import SimpleTestCase, TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Stages,
    StageGroups,
    TournamentTeam,
    RoundRobinGroup,
)
from afc_tournament_and_scrims.round_robin import round_robin_schedule


class RoundRobinSchemaTests(TestCase):
    """Schema-level test: base group ↔ teams and lobby ↔ source_groups wiring."""

    def setUp(self):
        # Minimal admin/creator — Team/Event both need a user FK.
        self.admin = User.objects.create(
            username="rr_admin", email="rr_admin@afc.test", full_name="RR Admin", role="admin")
        D = datetime.date(2026, 6, 1)

        # Minimal event + a single round-robin stage to hang the group/lobby off.
        self.event = Event.objects.create(
            event_name="Round Robin Cup", competition_type="tournament",
            participant_type="squad", event_type="internal", max_teams_or_players=16,
            event_mode="virtual", start_date=D, end_date=D, registration_open_date=D,
            registration_end_date=D, prizepool="$1000", event_rules="rules",
            event_status="upcoming", registration_link="https://afc.test/reg",
            number_of_stages=1, creator=self.admin, is_draft=False)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Group Stage", start_date=D, end_date=D,
            number_of_groups=1, stage_format="br - round robin",
            teams_qualifying_from_stage=4)

        # One team entered in the tournament — goes into base group A.
        team = Team.objects.create(
            team_name="RR Team A1", join_settings="open", team_creator=self.admin,
            team_owner=self.admin, team_captain=self.admin, country="Nigeria")
        self.tt = TournamentTeam.objects.create(event=self.event, team=team)

    def test_base_group_and_lobby_accessors_resolve(self):
        # Base group A in this stage, carrying one team.
        grp = RoundRobinGroup.objects.create(stage=self.stage, label="A", order=0)
        grp.teams.add(self.tt)

        # A game-day-1 lobby sourced from base group A.
        lobby = StageGroups.objects.create(
            stage=self.stage, group_name="Day 1 Lobby",
            playing_date=datetime.date(2026, 6, 1), playing_time=datetime.time(19, 0),
            teams_qualifying=4, match_count=1, match_maps=["bermuda"], game_day=1)
        lobby.source_groups.add(grp)

        # Reverse accessor: stage → its base groups.
        self.assertIn(grp, self.stage.round_robin_groups.all())
        # Reverse accessor: base group → the lobbies that merge it.
        self.assertIn(lobby, grp.lobbies.all())
        # Forward M2M: base group → its teams.
        self.assertIn(self.tt, grp.teams.all())
        # Forward M2M + persisted game_day on the lobby side.
        self.assertEqual(lobby.game_day, 1)
        self.assertIn(grp, lobby.source_groups.all())
        # Reverse accessor: team → the base groups it belongs to.
        self.assertIn(grp, self.tt.round_robin_groups.all())


class RoundRobinScheduleTests(SimpleTestCase):
    """Unit tests for the pure schedule generator (Task 2).

    `round_robin_schedule` is intentionally DB-free: it takes base-group ids and
    emits one lobby spec per *unordered* pairing of groups, one pairing per
    game-day. So N base groups → C(N, 2) lobbies (round-robin of group merges).
    These tests use `SimpleTestCase` (no DB) since the function never touches the ORM.
    """

    def test_three_groups_make_three_pairings(self):
        # A,B,C → the three unordered pairings A+B, A+C, B+C, on game-days 1..3.
        specs = round_robin_schedule(["A", "B", "C"])

        self.assertEqual(len(specs), 3)
        self.assertEqual([s["game_day"] for s in specs], [1, 2, 3])
        self.assertEqual(
            [s["source_group_ids"] for s in specs],
            [["A", "B"], ["A", "C"], ["B", "C"]],
        )

    def test_four_groups_make_six_pairings(self):
        # C(4, 2) = 6 lobbies, game-days numbered contiguously 1..6.
        specs = round_robin_schedule(["A", "B", "C", "D"])

        self.assertEqual(len(specs), 6)
        self.assertEqual([s["game_day"] for s in specs], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [s["source_group_ids"] for s in specs],
            [["A", "B"], ["A", "C"], ["A", "D"], ["B", "C"], ["B", "D"], ["C", "D"]],
        )

    def test_games_per_day_and_maps_propagate(self):
        # games_per_day → each lobby's match_count; maps → each lobby's match_maps.
        specs = round_robin_schedule(
            ["A", "B"], games_per_day=3, maps=["bermuda", "purgatory"])

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["match_count"], 3)
        self.assertEqual(specs[0]["match_maps"], ["bermuda", "purgatory"])

    def test_maps_default_to_bermuda(self):
        # No maps given → default to the single Bermuda map (BR default lobby map).
        specs = round_robin_schedule(["A", "B"])

        self.assertEqual(specs[0]["match_maps"], ["bermuda"])
        self.assertEqual(specs[0]["match_count"], 1)  # games_per_day default is 1

    def test_maps_are_copied_not_aliased(self):
        # Each spec must own a fresh list so mutating one lobby's maps can't
        # bleed into the caller's input or another spec (defensive: `list(...)`).
        src = ["bermuda"]
        specs = round_robin_schedule(["A", "B", "C"], maps=src)

        specs[0]["match_maps"].append("kalahari")
        self.assertEqual(src, ["bermuda"])  # caller's list untouched
        self.assertEqual(specs[1]["match_maps"], ["bermuda"])  # sibling untouched

    def test_single_group_has_nothing_to_merge(self):
        # One base group can't form a pairing → no lobbies to schedule.
        self.assertEqual(round_robin_schedule(["A"]), [])

    def test_empty_groups_return_empty(self):
        # Degenerate input is safe: no groups → no schedule.
        self.assertEqual(round_robin_schedule([]), [])
