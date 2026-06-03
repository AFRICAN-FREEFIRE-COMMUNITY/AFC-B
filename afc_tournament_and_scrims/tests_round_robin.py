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
    StageGroupCompetitor,
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


class RoundRobinCreateEventTests(TestCase):
    """End-to-end test for building a round-robin stage through `create-event` (Task 4).

    The admin builds a round-robin stage by sending, instead of plain `groups`, a
    `round_robin_groups` list (the BASE groups A/B/C + the team ids in each) plus a
    `generate_schedule:true` flag. `create_event` must then:
      • create the `RoundRobinGroup` rows (A/B/C) and wire their `teams` M2M (resolving the
        sent team ids → this event's `TournamentTeam` rows, creating any that don't exist),
      • run the pure `round_robin_schedule` over those base-group ids and materialise EACH
        spec into a game-day `StageGroups` LOBBY — carrying `game_day` + `source_groups`,
        its own auto-created `Leaderboard`, the `match_count` `Match` rows, and
      • seed `StageGroupCompetitor` from the MERGED base groups' teams (so the lobby's
        roster is exactly the union of the two base groups it merges).

    Fixture: 3 base groups A/B/C of 2 teams each. C(3,2)=3 game-day lobbies (A+B, A+C, B+C),
    each merging 4 teams. We assert the groups, the lobbies (game_day + source_groups), and
    the seeded competitors all land.
    """

    def setUp(self):
        self.client = Client()
        self.D = datetime.date(2026, 6, 1)

        # Admin + a live session token so create_event's _is_event_admin gate passes.
        self.admin = User.objects.create(
            username="rr_create_admin", email="rr_create_admin@afc.test",
            full_name="RR Create Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="rr-create-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))

        # Six real teams, two per base group. We send their Team ids as `team_ids`; the
        # endpoint must turn each into a TournamentTeam for the freshly-created event.
        self.teams = [self._make_team(f"RR Team {i}") for i in range(1, 7)]

    def _make_team(self, name):
        return Team.objects.create(
            team_name=name, join_settings="open", team_creator=self.admin,
            team_owner=self.admin, team_captain=self.admin, country="Nigeria")

    def _payload(self):
        """A minimal create-event payload whose single stage is round-robin with A/B/C."""
        d = str(self.D)
        a, b, c = self.teams[0:2], self.teams[2:4], self.teams[4:6]
        return {
            "competition_type": "tournament",
            "participant_type": "squad",
            "event_type": "internal",
            "max_teams_or_players": 16,
            "event_name": "RR Create Cup",
            "event_mode": "virtual",
            "start_date": d, "end_date": d,
            "registration_open_date": d, "registration_end_date": d,
            "prizepool": "$1000",
            "number_of_stages": 1,
            "is_draft": False,
            "stages": [
                {
                    "stage_name": "Group Stage",
                    "start_date": d, "end_date": d,
                    "number_of_groups": 3,
                    "stage_format": "br - round robin",
                    "teams_qualifying_from_stage": 4,
                    # Base groups (A/B/C) — each carries the Team ids that belong to it.
                    "round_robin_groups": [
                        {"label": "A", "order": 0, "team_ids": [t.team_id for t in a]},
                        {"label": "B", "order": 1, "team_ids": [t.team_id for t in b]},
                        {"label": "C", "order": 2, "team_ids": [t.team_id for t in c]},
                    ],
                    # Auto-generate the round-robin schedule of game-day lobbies.
                    "generate_schedule": True,
                    "games_per_day": 2,
                    "round_robin_maps": ["bermuda", "purgatory"],
                }
            ],
        }

    def _create(self):
        return self.client.post(
            "/events/create-event/",
            data=self._payload(),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_create_builds_base_groups(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        event = Event.objects.get(event_id=resp.json()["event_id"])
        stage = event.stages.get()

        # The three base groups landed, in order, each with its two teams attached.
        groups = list(stage.round_robin_groups.all())
        self.assertEqual([g.label for g in groups], ["A", "B", "C"])
        for g in groups:
            self.assertEqual(g.teams.count(), 2)

        # team_ids resolved into THIS event's TournamentTeam rows (6 teams → 6 TTs).
        self.assertEqual(TournamentTeam.objects.filter(event=event).count(), 6)

    def test_create_generates_game_day_lobbies(self):
        resp = self._create()
        event = Event.objects.get(event_id=resp.json()["event_id"])
        stage = event.stages.get()

        # C(3,2)=3 lobbies, one per game day 1..3, each merging exactly two base groups.
        lobbies = list(stage.groups.filter(game_day__isnull=False).order_by("game_day"))
        self.assertEqual(len(lobbies), 3)
        self.assertEqual([lb.game_day for lb in lobbies], [1, 2, 3])
        for lb in lobbies:
            self.assertEqual(lb.source_groups.count(), 2)
            # games_per_day=2 → match_count=2 and two Match rows materialised.
            self.assertEqual(lb.match_count, 2)
            self.assertEqual(lb.matches.count(), 2)
            self.assertEqual(lb.match_maps, ["bermuda", "purgatory"])
            # Each lobby gets its own auto-created leaderboard.
            self.assertTrue(Leaderboard.objects.filter(group=lb).exists())

        # The first lobby (day 1) merges base groups A+B.
        day1 = lobbies[0]
        self.assertEqual(
            {g.label for g in day1.source_groups.all()}, {"A", "B"})

    def test_create_seeds_competitors_from_merged_groups(self):
        resp = self._create()
        event = Event.objects.get(event_id=resp.json()["event_id"])
        stage = event.stages.get()

        # Day-1 lobby merges A+B (2+2 teams) → 4 seeded StageGroupCompetitor rows, and the
        # seeded teams are exactly the union of base groups A and B.
        day1 = stage.groups.filter(game_day=1).get()
        seeded = StageGroupCompetitor.objects.filter(stage_group=day1)
        self.assertEqual(seeded.count(), 4)
        seeded_names = set(
            seeded.values_list("tournament_team__team__team_name", flat=True))
        expected = {t.team_name for t in self.teams[0:4]}  # A (0,1) + B (2,3)
        self.assertEqual(seeded_names, expected)

    def test_event_details_echoes_round_robin_structure(self):
        # get-event-details must echo the round-robin block (base groups + game-day lobbies)
        # so the FE editor can rehydrate the builder. Other formats get round_robin=None.
        resp = self._create()
        event = Event.objects.get(event_id=resp.json()["event_id"])

        details = self.client.post(
            "/events/get-event-details/",
            data={"slug": event.slug},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(details.status_code, 200, details.content)

        stage_echo = details.json()["event_details"]["stages"][0]["round_robin"]
        self.assertIsNotNone(stage_echo)
        self.assertEqual({g["label"] for g in stage_echo["round_robin_groups"]},
                         {"A", "B", "C"})
        # Three game days, each echoing its lobby's source group ids.
        self.assertEqual([d["game_day"] for d in stage_echo["game_days"]], [1, 2, 3])
        day1 = stage_echo["game_days"][0]["lobbies"][0]
        self.assertEqual(len(day1["source_group_ids"]), 2)


class RoundRobinEditEventTests(TestCase):
    """`edit-event` rebuilds round-robin lobbies but PRESERVES played ones (Task 4 landmine).

    The critical rule: regenerating the schedule on edit must NOT wipe a game-day lobby that
    already has entered results. We build a stage, mark ONE lobby's match as played (with a
    stat), then re-submit the same stage through edit-event with generate_schedule. The
    played lobby must survive verbatim (same group_id, still result_inputted + stats), while
    the unplayed lobbies are regenerated.
    """

    def setUp(self):
        self.client = Client()
        self.D = datetime.date(2026, 6, 1)
        self.admin = User.objects.create(
            username="rr_edit_admin", email="rr_edit_admin@afc.test",
            full_name="RR Edit Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="rr-edit-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.teams = [self._make_team(f"RR Edit Team {i}") for i in range(1, 7)]

        # Build a 3-group round-robin event via create-event (reuse the same payload shape).
        create = self.client.post(
            "/events/create-event/",
            data=self._stage_payload(extra_event={
                "competition_type": "tournament", "participant_type": "squad",
                "event_type": "internal", "max_teams_or_players": 16,
                "event_name": "RR Edit Cup", "event_mode": "virtual",
                "number_of_stages": 1, "is_draft": False, "prizepool": "$1000"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(create.status_code, 201, create.content)
        self.event = Event.objects.get(event_id=create.json()["event_id"])
        self.stage = self.event.stages.get()

    def _make_team(self, name):
        return Team.objects.create(
            team_name=name, join_settings="open", team_creator=self.admin,
            team_owner=self.admin, team_captain=self.admin, country="Nigeria")

    def _stage(self, with_stage_id=False):
        d = str(self.D)
        a, b, c = self.teams[0:2], self.teams[2:4], self.teams[4:6]
        stage = {
            "stage_name": "Group Stage",
            "start_date": d, "end_date": d,
            "number_of_groups": 3,
            "stage_format": "br - round robin",
            "teams_qualifying_from_stage": 4,
            "round_robin_groups": [
                {"label": "A", "order": 0, "team_ids": [t.team_id for t in a]},
                {"label": "B", "order": 1, "team_ids": [t.team_id for t in b]},
                {"label": "C", "order": 2, "team_ids": [t.team_id for t in c]},
            ],
            "generate_schedule": True,
            "games_per_day": 1,
            "round_robin_maps": ["bermuda"],
        }
        if with_stage_id:
            stage["stage_id"] = self.stage.stage_id
        return stage

    def _stage_payload(self, extra_event):
        d = str(self.D)
        return {
            **extra_event,
            "start_date": d, "end_date": d,
            "registration_open_date": d, "registration_end_date": d,
            "stages": [self._stage()],
        }

    def test_edit_preserves_played_lobby_and_regenerates_rest(self):
        # Mark the day-1 lobby as PLAYED: flip its match to result_inputted + add a stat.
        day1 = self.stage.groups.filter(game_day=1).get()
        played_match = day1.matches.first()
        played_match.result_inputted = True
        played_match.save(update_fields=["result_inputted"])
        played_tt = day1.competitors.first().tournament_team
        TournamentTeamMatchStats.objects.create(
            match=played_match, tournament_team=played_tt, placement=1, kills=3,
            placement_points=12, kill_points=3, total_points=15)

        day1_id = day1.group_id
        unplayed_ids_before = set(
            self.stage.groups.filter(game_day__isnull=False)
            .exclude(group_id=day1_id).values_list("group_id", flat=True))

        # Re-submit the SAME stage (with its stage_id) through edit-event, regenerating.
        edit = self.client.post(
            "/events/edit-event/",
            data={"event_id": self.event.event_id, "stages": [self._stage(with_stage_id=True)]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(edit.status_code, 200, edit.content)

        # The played day-1 lobby survives verbatim: same id, still played, stat intact.
        survived = StageGroups.objects.filter(group_id=day1_id).first()
        self.assertIsNotNone(survived, "played lobby must not be deleted on edit")
        self.assertTrue(survived.matches.filter(result_inputted=True).exists())
        self.assertEqual(
            TournamentTeamMatchStats.objects.filter(match__group=survived).count(), 1)

        # The unplayed lobbies were regenerated → their old ids are gone, day 2 & 3 still exist.
        lobbies_after = self.stage.groups.filter(game_day__isnull=False)
        self.assertFalse(
            lobbies_after.filter(group_id__in=unplayed_ids_before).exists(),
            "unplayed lobbies should be replaced (old ids gone)")
        self.assertEqual(
            set(lobbies_after.values_list("game_day", flat=True)), {1, 2, 3})
        # Still exactly C(3,2)=3 lobbies — no duplicate day 1 created alongside the kept one.
        self.assertEqual(lobbies_after.count(), 3)
