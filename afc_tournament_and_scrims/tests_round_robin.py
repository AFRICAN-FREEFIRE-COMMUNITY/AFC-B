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

from django.test import Client, SimpleTestCase, TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Stages,
    StageGroups,
    Leaderboard,
    Match,
    TournamentTeam,
    TournamentTeamMatchStats,
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


class RoundRobinStandingsTests(TestCase):
    """End-to-end test for the three-view standings + the admin endpoint (Task 3).

    Round-Robin standings come in three shapes, all read-time aggregates over the same
    `TournamentTeamMatchStats` rows (matches/stats are unchanged by the format):
      • per-lobby   — handled by the existing leaderboard view (one StageGroups = a lobby),
      • per-day     — `day_standings(stage, game_day)`: sum a single game day's lobbies,
      • cumulative  — `cumulative_standings(stage)`: sum the WHOLE stage across every lobby.
    The crux the format introduces: a team can appear in MORE THAN ONE lobby across the
    stage (one per game day it plays), so cumulative must SUM a team across lobbies — not
    treat each lobby in isolation the way the per-group leaderboard does.

    Fixture: 2 base groups (A, B), 2 lobbies on game_days 1 and 2, and one team that plays
    BOTH lobbies. We assert cumulative sums that team across both days, per_day filters to a
    single day, and the structural `groups` / `game_days` blocks are present.
    """

    def setUp(self):
        self.client = Client()
        D = datetime.date(2026, 6, 1)

        # Admin + a live session token so the endpoint's _is_event_admin gate passes
        # (validate_token resolves the Bearer token to this user).
        self.admin = User.objects.create(
            username="rr_std_admin", email="rr_std_admin@afc.test",
            full_name="RR Standings Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="rr-standings-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))

        # Event + a single round-robin stage.
        self.event = Event.objects.create(
            event_name="Round Robin Standings Cup", competition_type="tournament",
            participant_type="squad", event_type="internal", max_teams_or_players=16,
            event_mode="virtual", start_date=D, end_date=D, registration_open_date=D,
            registration_end_date=D, prizepool="$1000", event_rules="rules",
            event_status="ongoing", registration_link="https://afc.test/reg",
            number_of_stages=1, creator=self.admin, is_draft=False)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Group Stage", start_date=D, end_date=D,
            number_of_groups=2, stage_format="br - round robin",
            teams_qualifying_from_stage=4)

        # Two base groups A and B.
        self.grp_a = RoundRobinGroup.objects.create(stage=self.stage, label="A", order=0)
        self.grp_b = RoundRobinGroup.objects.create(stage=self.stage, label="B", order=1)

        # Three tournament teams: the "cross" team plays BOTH lobbies (the case the
        # cumulative sum must handle); the other two play one lobby each.
        self.tt_cross = self._make_tt("Cross Team")
        self.tt_a = self._make_tt("A Only")
        self.tt_b = self._make_tt("B Only")
        self.grp_a.teams.add(self.tt_cross, self.tt_a)
        self.grp_b.teams.add(self.tt_cross, self.tt_b)

        # Two lobbies: day 1 merges {A, B}, day 2 merges {A, B} again (two game days of
        # round-robin play). Each lobby is a StageGroups row carrying game_day + source_groups.
        self.lobby1 = self._make_lobby("Day 1 Lobby", game_day=1,
                                       sources=[self.grp_a, self.grp_b])
        self.lobby2 = self._make_lobby("Day 2 Lobby", game_day=2,
                                       sources=[self.grp_a, self.grp_b])

        # One match per lobby with entered team stats.
        # Day 1: cross team books 10 placement + 5 kill pts (1 booyah), kills=5.
        m1 = self._make_match(self.lobby1, match_number=1)
        self._stat(m1, self.tt_cross, placement=1, kills=5,
                   placement_points=10, kill_points=5)
        self._stat(m1, self.tt_a, placement=2, kills=2,
                   placement_points=6, kill_points=2)
        self._stat(m1, self.tt_b, placement=3, kills=1,
                   placement_points=4, kill_points=1)

        # Day 2: cross team books 8 placement + 3 kill pts, kills=3 (no booyah).
        m2 = self._make_match(self.lobby2, match_number=1)
        self._stat(m2, self.tt_cross, placement=2, kills=3,
                   placement_points=8, kill_points=3)
        self._stat(m2, self.tt_a, placement=1, kills=4,
                   placement_points=12, kill_points=4)
        self._stat(m2, self.tt_b, placement=3, kills=2,
                   placement_points=4, kill_points=2)

    # ── tiny fixture builders (keep setUp readable; mirror seed_scoring_demo.py creates) ──
    def _make_tt(self, name):
        team = Team.objects.create(
            team_name=name, join_settings="open", team_creator=self.admin,
            team_owner=self.admin, team_captain=self.admin, country="Nigeria")
        return TournamentTeam.objects.create(event=self.event, team=team)

    def _make_lobby(self, group_name, game_day, sources):
        lobby = StageGroups.objects.create(
            stage=self.stage, group_name=group_name,
            playing_date=datetime.date(2026, 6, 1), playing_time=datetime.time(19, 0),
            teams_qualifying=4, match_count=1, match_maps=["bermuda"], game_day=game_day)
        lobby.source_groups.add(*sources)
        return lobby

    def _make_match(self, lobby, match_number):
        # A Match must hang off a Leaderboard+group in this schema; build the minimal pair.
        leaderboard = Leaderboard.objects.create(
            leaderboard_name=f"{lobby.group_name} LB", event=self.event, stage=self.stage,
            group=lobby, creator=self.admin, leaderboard_method="manual")
        return Match.objects.create(
            leaderboard=leaderboard, group=lobby, match_number=match_number,
            match_map="bermuda", result_inputted=True)

    def _stat(self, match, tt, placement, kills, placement_points, kill_points):
        return TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=placement, kills=kills,
            placement_points=placement_points, kill_points=kill_points,
            total_points=placement_points + kill_points)

    def _post(self):
        return self.client.post(
            "/events/get-round-robin-standings/",
            data={"event_id": self.event.event_id, "stage_id": self.stage.stage_id},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_cumulative_sums_team_across_both_lobbies(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()

        # Cumulative is one row PER TEAM for the whole stage — the cross team appears once,
        # with day1 + day2 summed: effective_total 10+5+8+3 = 26, kills 5+3 = 8,
        # booyah 1 (only day 1 was a placement==1), games_played 2 (one match each day).
        cumulative = body["cumulative"]
        cross_rows = [r for r in cumulative if r["team_name"] == "Cross Team"]
        self.assertEqual(len(cross_rows), 1, "team must collapse to a single cumulative row")
        cross = cross_rows[0]
        self.assertEqual(cross["effective_total"], 26)
        self.assertEqual(cross["total_kills"], 8)
        self.assertEqual(cross["total_booyah"], 1)
        self.assertEqual(cross["games_played"], 2)
        # Cumulative covers every team that played either lobby (A-only + B-only + cross).
        self.assertEqual({r["team_name"] for r in cumulative},
                         {"Cross Team", "A Only", "B Only"})

    def test_per_day_filters_to_one_game_day(self):
        body = self._post().json()
        per_day = body["per_day"]

        # Per-day is keyed by game day (json keys are strings). Day 1 only sees day-1 stats.
        day1 = {r["team_name"]: r for r in per_day["1"]}
        self.assertEqual(day1["Cross Team"]["effective_total"], 15)  # 10 + 5, NOT 26
        self.assertEqual(day1["Cross Team"]["games_played"], 1)
        # Day 2 is the other slice: cross team's day-2-only points.
        day2 = {r["team_name"]: r for r in per_day["2"]}
        self.assertEqual(day2["Cross Team"]["effective_total"], 11)  # 8 + 3

    def test_groups_and_game_days_blocks_present(self):
        body = self._post().json()

        # `groups` echoes the base-group structure (A/B + their team names) for the UI.
        group_labels = {g["label"] for g in body["groups"]}
        self.assertEqual(group_labels, {"A", "B"})

        # `game_days` lists each day and the lobby (StageGroups) ids merged into it.
        days = {g["day"]: g for g in body["game_days"]}
        self.assertEqual(set(days.keys()), {1, 2})
        self.assertIn(self.lobby1.group_id, days[1]["lobbies"])
        self.assertIn(self.lobby2.group_id, days[2]["lobbies"])

    def test_non_admin_is_rejected(self):
        # The endpoint is admin-gated: a plain player token must be refused (403).
        player = User.objects.create(
            username="rr_player", email="rr_player@afc.test",
            full_name="RR Player", role="player")
        player_token = SessionToken.objects.create(
            user=player, token="rr-player-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        resp = self.client.post(
            "/events/get-round-robin-standings/",
            data={"event_id": self.event.event_id, "stage_id": self.stage.stage_id},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {player_token.token}")
        self.assertEqual(resp.status_code, 403, resp.content)
