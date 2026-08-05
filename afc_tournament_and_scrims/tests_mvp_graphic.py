"""
The downloadable MVP graphic - owner 2026-08-05, backlog #3.

THE ASK
    "A downloadable MVP graphic, which does not exist yet. Default design: player images (with a
    placeholder when a player has none), kills, and matches played."

WHAT WAS ACTUALLY MISSING
    The download ROUTE has existed since 2026-07-05 (GET events/<id>/player-board-graphic/), but it
    only rendered through a SAVED design, and no design in any library places PLAYER columns - so the
    button returned a background with nothing on it. The fix is a branded default for player boards
    (build_ephemeral_afc_player_default), taken whenever the design places no fields.

WHAT THESE TESTS GUARD
    • the endpoint returns a real PNG for both kinds (mvp + top_killers) with no design in the library
    • it renders THROUGH the player default, with the player rows (photo / kills / MP) - not an empty
      board and not the legacy bare table
    • the header reads EVENT NAME over the board label, matching the team export's header rule
    • a player with NO esport photo still gets a portrait (the placeholder), which is the part the
      owner called out; the pixel-level proof of the placeholder art lives in
      afc_leaderboard.tests.test_graphic_chrome

Run: .venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_mvp_graphic
"""
import datetime
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, Stages, StageGroups, TournamentTeam,
    TournamentPlayerMatchStats, TournamentTeamMatchStats,
)


class MvpGraphicTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="mvpg_admin", email="mvpg_admin@x.com", full_name="MVP Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="mvpg-admin-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="MVP Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        stage = Stages.objects.create(
            event=self.event, stage_name="Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
            stage_order=1)
        group = StageGroups.objects.create(
            stage=stage, group_name="Lobby A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=1, match_count=1)
        lb = Leaderboard.objects.create(
            leaderboard_name="Finals LB", event=self.event, stage=stage, group=group,
            creator=self.admin, placement_points={"1": 12}, kill_point=1.0,
            leaderboard_method="manual")
        match = Match.objects.create(
            leaderboard=lb, group=group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12}, "kill_point": 1})
        tt = TournamentTeam.objects.create(
            event=self.event,
            team=Team.objects.create(team_name="Alpha", team_tag="ALP", join_settings="open",
                                     team_creator=self.admin, team_owner=self.admin, country="NG"),
            registered_by=self.admin)
        team_stats = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=9, damage=0, assists=0,
            placement_points=12, kill_points=9, total_points=21, played=True)
        # Neither player has an esports_pic, which is the normal case and the one the placeholder
        # exists for.
        for i, kills in enumerate((6, 3)):
            player = User.objects.create(
                username=f"mvpg_p{i}", email=f"mvpg_p{i}@x.com", full_name=f"P{i}", role="player")
            TournamentPlayerMatchStats.objects.create(
                team_stats=team_stats, player=player, kills=kills, played=True)

    def _get(self, **params):
        return self.client.get(
            f"/events/{self.event.event_id}/player-board-graphic/", params,
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_mvp_download_returns_a_png_with_no_design_in_the_library(self):
        resp = self._get(kind="mvp", size="instagram")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertIn("attachment;", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"\x89PNG"))

    def test_top_killers_download_returns_a_png(self):
        resp = self._get(kind="top_killers", size="youtube")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.startswith(b"\x89PNG"))

    def test_renders_through_the_player_default_with_photo_kills_and_maps_played(self):
        with patch("afc_leaderboard.graphic.render_design_all_pages",
                   return_value=[b"\x89PNG-stub"]) as render:
            resp = self._get(kind="mvp", size="instagram")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(render.called, "the MVP export must go through the branded player default")
        rows, pages_spec = render.call_args[0][0], render.call_args[0][1]
        # Every ranked player is handed to the renderer, keyed by the player field types.
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("esports_image", row)
            self.assertIn("kills", row)
            self.assertEqual(row["matches"], 1)   # one map played
        # The page places the three columns the owner named, plus the portrait placeholder.
        layout = pages_spec[0]["field_layout"]
        placed = [f["field_type"] for f in layout["fields"]]
        for field_type in ("esports_image", "kills", "matches"):
            self.assertIn(field_type, placed)
        self.assertIn("esports_image", layout["image_placeholders"])

    def test_header_reads_event_name_over_the_board_label(self):
        with patch("afc_leaderboard.graphic.render_design_all_pages",
                   return_value=[b"\x89PNG-stub"]) as render:
            self._get(kind="mvp")
        kwargs = render.call_args[1]
        self.assertEqual(kwargs["title"], "MVP Cup")
        self.assertEqual(kwargs["subtitle"], "MVP")

    def test_players_without_a_photo_still_get_a_portrait(self):
        # Nobody in this fixture has an esports_pic, so every row's image is empty and the board
        # relies entirely on the placeholder the layout supplies.
        with patch("afc_leaderboard.graphic.render_design_all_pages",
                   return_value=[b"\x89PNG-stub"]) as render:
            self._get(kind="mvp")
        rows, pages_spec = render.call_args[0][0], render.call_args[0][1]
        self.assertTrue(all(not r["esports_image"] for r in rows))
        placeholder = pages_spec[0]["field_layout"]["image_placeholders"]["esports_image"]
        self.assertTrue(placeholder.endswith(".png"))
