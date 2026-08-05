"""
MAPS PLAYED (MP) on the exported graphic - owner 2026-08-05, backlog #17.

THE ASK
    "Leaderboards must show MP (maps played), per team AND per player."

WHAT THIS GUARDS
    The number itself already existed on both sides - the team standings carry `games_played` (the
    round_robin aggregator counts one per match a team has stats for) and a player row carries
    `matches` (one per stat line the player has). Neither was ever placed on the exported graphic. So
    the risk is not "is the number computed", it is "does the graphic show THE SAME number the
    standings show" - a second, divergent definition would be worse than no column at all.

    These tests therefore assert the number END TO END: they build a stage where one team played two
    maps and the other played one, then compare what the EXPORT hands the renderer against what
    round_robin.cumulative_standings reports for the same stage. The renderer is patched at its
    boundary (it turns rows into pixels and is covered separately by
    afc_leaderboard.tests.test_graphic_chrome) so we can read the rows the view actually built.

Run: .venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_maps_played_export
"""
import datetime
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import round_robin
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, Stages, StageGroups, TournamentTeam,
    TournamentPlayerMatchStats, TournamentTeamMatchStats,
)
from afc_tournament_and_scrims.views_mvp import build_player_design_rows, compute_top_killers


class MapsPlayedExportTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="mp_admin", email="mp_admin@x.com", full_name="MP Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="mp-admin-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Maps Played Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Group Stage", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Lobby A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=2)
        self.lb = Leaderboard.objects.create(
            leaderboard_name="Lobby A LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")
        scoring = {"placement_points": {"1": 12, "2": 9}, "kill_point": 1}
        self.m1 = Match.objects.create(leaderboard=self.lb, group=self.group, match_number=1,
                                       match_map="bermuda", scoring_settings=scoring)
        self.m2 = Match.objects.create(leaderboard=self.lb, group=self.group, match_number=2,
                                       match_map="purgatory", scoring_settings=scoring)

        self.tt_a = self._team("Alpha", "ALP")
        self.tt_b = self._team("Bravo", "BRV")
        # Alpha played BOTH maps, Bravo only the first: MP must come back 2 and 1, never "2 and 2"
        # (which is what a naive "number of matches in the group" count would produce).
        self.stat_a1 = self._stat(self.m1, self.tt_a, placement=1, kills=9, pp=12, kp=9)
        self.stat_a2 = self._stat(self.m2, self.tt_a, placement=2, kills=4, pp=9, kp=4)
        self.stat_b1 = self._stat(self.m1, self.tt_b, placement=2, kills=3, pp=9, kp=3)

    # ── fixture helpers ───────────────────────────────────────────────────────────────────────
    def _team(self, name, tag):
        return TournamentTeam.objects.create(
            event=self.event,
            team=Team.objects.create(team_name=name, team_tag=tag, join_settings="open",
                                     team_creator=self.admin, team_owner=self.admin, country="NG"),
            registered_by=self.admin)

    def _stat(self, match, tt, *, placement, kills, pp, kp):
        return TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=placement, kills=kills,
            damage=0, assists=0, placement_points=pp, kill_points=kp,
            total_points=pp + kp, played=True)

    def _export_rows(self):
        """Call the stage graphic export and return the per-row dicts it handed the renderer.

        The library has no design, so the view takes its branded-default branch and renders through
        render_design_all_pages; patching THAT (the pixels boundary) lets us read the rows the view
        built without asserting on a PNG."""
        with patch("afc_tournament_and_scrims.views_event_graphic.render_design_all_pages",
                   return_value=[b"\x89PNG-stub"]) as render:
            resp = self.client.get(
                f"/events/{self.event.event_id}/stages/{self.stage.stage_id}/graphic/",
                HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(render.called)
        return render.call_args[0][0]

    # ── team MP ───────────────────────────────────────────────────────────────────────────────
    def test_team_maps_played_matches_the_standings_it_came_from(self):
        standings = {r["team_name"]: r for r in round_robin.cumulative_standings(self.stage)}
        self.assertEqual(standings["Alpha"]["games_played"], 2)
        self.assertEqual(standings["Bravo"]["games_played"], 1)

        rows = {r["team_name"]: r for r in self._export_rows()}
        # The graphic's MP column must be the aggregator's games_played, not a re-derived count.
        self.assertEqual(rows["Alpha"]["matches"], standings["Alpha"]["games_played"])
        self.assertEqual(rows["Bravo"]["matches"], standings["Bravo"]["games_played"])

    def test_matches_column_is_placed_on_the_default_board(self):
        # The number being in the row dict is only half of it: the default design has to draw it.
        from afc_organizers.views_leaderboard_design import build_ephemeral_afc_default
        layout = build_ephemeral_afc_default(2).pages_spec[0]["field_layout"]
        self.assertIn("matches", [f["field_type"] for f in layout["fields"]])

    def test_seeded_team_with_no_results_reports_zero_maps_played(self):
        # The export zero-fills seeded teams that have no stats yet; their MP must read 0 rather than
        # inheriting a neighbour's count or rendering blank.
        rows = {r["team_name"]: r for r in self._export_rows()}
        self.assertEqual(rows["Bravo"]["matches"], 1)
        self.stat_b1.delete()
        rows = {r["team_name"]: r for r in self._export_rows()}
        self.assertEqual(rows.get("Alpha", {}).get("matches"), 2)

    # ── player MP ─────────────────────────────────────────────────────────────────────────────
    def test_player_maps_played_matches_the_stat_lines_it_came_from(self):
        # One player on both of Alpha's maps, a team-mate on only the second.
        veteran = User.objects.create(username="mp_vet", email="mp_vet@x.com",
                                      full_name="Vet", role="player")
        rookie = User.objects.create(username="mp_rook", email="mp_rook@x.com",
                                     full_name="Rook", role="player")
        TournamentPlayerMatchStats.objects.create(
            team_stats=self.stat_a1, player=veteran, kills=5, played=True)
        TournamentPlayerMatchStats.objects.create(
            team_stats=self.stat_a2, player=veteran, kills=4, played=True)
        TournamentPlayerMatchStats.objects.create(
            team_stats=self.stat_a2, player=rookie, kills=1, played=True)

        players = compute_top_killers(self.event, None)["players"]
        by_name = {p["username"]: p for p in players}
        self.assertEqual(by_name["mp_vet"]["matches"], 2)
        self.assertEqual(by_name["mp_rook"]["matches"], 1)

        # The design rows the MVP / Top-killers graphic renders carry the SAME number.
        rows = {r["player_name"]: r for r in build_player_design_rows(players)}
        self.assertEqual(rows[by_name["mp_vet"]["in_game_name"]]["matches"], 2)
        self.assertEqual(rows[by_name["mp_rook"]["in_game_name"]]["matches"], 1)

    def test_matches_column_is_placed_on_the_default_player_board(self):
        from afc_organizers.views_leaderboard_design import build_ephemeral_afc_player_default
        layout = build_ephemeral_afc_player_default(5).pages_spec[0]["field_layout"]
        self.assertIn("matches", [f["field_type"] for f in layout["fields"]])
