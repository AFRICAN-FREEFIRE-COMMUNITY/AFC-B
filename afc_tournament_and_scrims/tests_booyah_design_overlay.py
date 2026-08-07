"""
The DESIGN-DRIVEN booyah overlay - owner 2026-08-06.

THE ASK
    "For the booyah design under live overlays, let it work like the way leaderboard works, that
    whatever will populate on the design will come from the leaderboard, so the overlays should be
    based off what's on the design and what was set to come up there. The booyah overlay came with
    preset designs which we don't want."

WHAT CHANGED
    The booyah scene used to be a hard-coded banner that took four values off the picked design
    (background + two colours + the transparent flag) and drew its own fixed layout. It now ALSO has
    a design-driven path: bind a design whose design_type is "booyah" and the public config poll
    ships that whole design plus the resolved rows, which the frontend renders through DesignBoard -
    the exact same board renderer the live leaderboard overlay uses.

    The row contract (views_overlays._booyah_board):
        slot 1        -> the winning TEAM, its numbers lifted out of the live leaderboard
                         (_overlay_standings_rows), plus `match_map`
        slots 2..N+1  -> that team's PLAYERS with their stats from the map they won

WHAT THESE TESTS GUARD
    • MIGRATION SAFETY, the thing that must not break: an overlay with no design, or with a
      LEADERBOARD design, gets board=None and keeps its legacy banner payload untouched
    • the board only appears for a design_type="booyah" design
    • the team row really is the LEADERBOARD row (cumulative across the group), not the won map
    • `slot` and `pos` are separate: slot places the row, pos stays a displayable rank
    • players are ordered by kills, carry their map stats, and inherit the team context
    • the roster fallback fills the block when a match has no per-player rows yet
    • a player who opted out of broadcast media keeps their photo off the board
    • the design editor accepts the field types a booyah design needs (match_map, team_flag and
      the player columns)

Run: .venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_booyah_design_overlay
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_organizers.models import OrgLeaderboardDesign, OrgLeaderboardDesignField
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, EventMediaOptOut, EventOverlay, Leaderboard, Match, Stages, StageGroups,
    TournamentTeam, TournamentTeamMember, TournamentPlayerMatchStats, TournamentTeamMatchStats,
)
from afc_tournament_and_scrims.views_overlays import (
    BOOYAH_FIRST_PLAYER_SLOT, BOOYAH_TEAM_SLOT,
)


class BooyahDesignOverlayTests(TestCase):
    """One squad event, one group, two maps. Alpha places 2nd on map 1 and wins map 2 (the booyah),
    so the group cumulative and the won map's own numbers are DIFFERENT - which is what makes the
    "the team row comes from the leaderboard" assertion mean something."""

    def setUp(self):
        self.client = APIClient()
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="boo_admin", email="boo_admin@x.com", full_name="Boo Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="boo-admin-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Booyah Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin,
            overlay_token="boo-overlay-token")
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Lobby A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=1, match_count=2)
        lb = Leaderboard.objects.create(
            leaderboard_name="Finals LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")
        scoring = {"placement_points": {"1": 12, "2": 9}, "kill_point": 1}
        self.map1 = Match.objects.create(
            leaderboard=lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings=scoring)
        self.map2 = Match.objects.create(
            leaderboard=lb, group=self.group, match_number=2, match_map="purgatory",
            scoring_settings=scoring)

        self.team = Team.objects.create(
            team_name="Alpha", team_tag="ALP", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG")
        self.tt = TournamentTeam.objects.create(
            event=self.event, team=self.team, registered_by=self.admin)
        # A rival, so the standings have more than one row and a real ordering.
        self.rival = TournamentTeam.objects.create(
            event=self.event,
            team=Team.objects.create(team_name="Bravo", team_tag="BRV", join_settings="open",
                                     team_creator=self.admin, team_owner=self.admin, country="GH"),
            registered_by=self.admin)

        # Map 1: Alpha 2nd (9 + 4 = 13), Bravo 1st (12 + 2 = 14).
        TournamentTeamMatchStats.objects.create(
            match=self.map1, tournament_team=self.tt, placement=2, kills=4,
            placement_points=9, kill_points=4, total_points=13, played=True)
        TournamentTeamMatchStats.objects.create(
            match=self.map1, tournament_team=self.rival, placement=1, kills=2,
            placement_points=12, kill_points=2, total_points=14, played=True)
        # Map 2: Alpha BOOYAH (12 + 8 = 20), Bravo 2nd (9 + 1 = 10).
        self.win = TournamentTeamMatchStats.objects.create(
            match=self.map2, tournament_team=self.tt, placement=1, kills=8,
            placement_points=12, kill_points=8, total_points=20, played=True)
        TournamentTeamMatchStats.objects.create(
            match=self.map2, tournament_team=self.rival, placement=2, kills=1,
            placement_points=9, kill_points=1, total_points=10, played=True)

        # Alpha's squad, with their map-2 stats. Deliberately created out of kill order so the
        # "ordered by kills" assertion cannot pass by insertion accident.
        # NOTE: the display name is the USERNAME. `in_game_name` is not a column on User (it is
        # commented out in afc_auth.models), which is why every producer of a player row - here,
        # views_mvp._player_design_row, the legacy booyah roster - reads it with a getattr fallback
        # to username. Naming these users after their IGN keeps the assertions readable.
        self.players = []
        for name, kills, damage, assists in (
            ("SLOWHAND", 1, 400, 0),
            ("ACE", 5, 1800, 3),
            ("MID", 2, 900, 1),
        ):
            u = User.objects.create(
                username=name, email=f"boo_{name.lower()}@x.com",
                full_name=name, role="player")
            self.players.append(u)
            TournamentTeamMember.objects.create(tournament_team=self.tt, user=u)
            TournamentPlayerMatchStats.objects.create(
                team_stats=self.win, player=u, kills=kills, damage=damage, assists=assists,
                played=True)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _design(self, design_type):
        """A minimal design of the given type with one placed column, so it is a real design rather
        than an empty shell (the board ships whatever is placed; one field is enough to prove it)."""
        d = OrgLeaderboardDesign.objects.create(
            name=f"{design_type} design", design_type=design_type,
            column_groups=[{"row_start_pct": 30.0, "row_height_pct": 8.0,
                            "row_count": 1, "start_rank": 1},
                           {"row_start_pct": 55.0, "row_height_pct": 8.0,
                            "row_count": 6, "start_rank": 2}])
        OrgLeaderboardDesignField.objects.create(
            design=d, field_type="team_name", column_group=0, x_pct=30.0)
        OrgLeaderboardDesignField.objects.create(
            design=d, field_type="player_name", column_group=1, x_pct=30.0)
        return d

    def _overlay(self, config):
        return EventOverlay.objects.create(
            event=self.event, name="Booyah", kind="booyah", config=config, active=True)

    def _poll(self, overlay):
        """The PUBLIC config poll the OBS browser source hits once a second."""
        return self.client.get(
            "/events/overlay/config/",
            {"token": self.event.overlay_token, "overlay": overlay.id})

    # ── migration safety: existing overlays must not change ────────────────────
    def test_overlay_with_no_design_keeps_the_legacy_banner_payload(self):
        overlay = self._overlay({"team_name": "Alpha", "match_map": "purgatory"})

        resp = self._poll(overlay)

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["booyah"]["board"])
        # The legacy banner's inputs are untouched, so the old renderer still has what it needs.
        self.assertEqual([p["name"] for p in resp.data["booyah"]["roster"]],
                         ["SLOWHAND", "ACE", "MID"])

    def test_overlay_bound_to_a_leaderboard_design_keeps_the_legacy_banner(self):
        # This is the state EVERY booyah overlay configured before 2026-08-06 is in.
        design = self._design("leaderboard")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})

        resp = self._poll(overlay)

        self.assertIsNone(resp.data["booyah"]["board"])
        # ...and it still gets the 4-key look the banner styles itself with.
        self.assertIsNotNone(resp.data["booyah"]["design"])

    def test_board_appears_only_for_a_booyah_type_design(self):
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})

        resp = self._poll(overlay)

        board = resp.data["booyah"]["board"]
        self.assertIsNotNone(board)
        self.assertEqual(board["design"]["id"], design.id)
        self.assertEqual(board["design"]["design_type"], "booyah")
        self.assertEqual(board["size"], "youtube")
        # The FULL design rides along, not a 4-key look: the FE needs the placed fields to draw it.
        self.assertEqual(sorted(f["field_type"] for f in board["design"]["fields"]),
                         ["player_name", "team_name"])

    # ── the row contract ───────────────────────────────────────────────────────
    def test_team_row_comes_from_the_leaderboard_not_the_won_map(self):
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})

        team_row = self._poll(overlay).data["booyah"]["board"]["rows"][0]

        # Alpha's GROUP cumulative is 13 (map 1) + 20 (map 2) = 33, and 4 + 8 = 12 kills over 2
        # maps. The won map alone would have said 20 and 8 - so this proves the numbers are the
        # leaderboard's, which is the whole point of the owner's request.
        self.assertEqual(team_row["total_points"], 33)
        self.assertEqual(team_row["kills"], 12)
        self.assertEqual(team_row["matches"], 2)
        self.assertEqual(team_row["team_name"], "Alpha")
        # The one value a standings row cannot carry: which map was won.
        self.assertEqual(team_row["match_map"], "purgatory")

    def test_team_row_matches_the_leaderboard_overlay_row_exactly(self):
        from afc_tournament_and_scrims.views import _overlay_standings_rows

        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})
        request = self.client.get("/").wsgi_request

        team_row = self._poll(overlay).data["booyah"]["board"]["rows"][0]
        leaderboard_rows = _overlay_standings_rows(self.event, None, self.group, 50, request)
        alpha = next(r for r in leaderboard_rows if r["team_name"] == "Alpha")

        # Every standings key agrees with the live leaderboard; only the booyah-only additions
        # (slot / row_key / match_map) are extra.
        for key, value in alpha.items():
            self.assertEqual(team_row[key], value, f"{key} drifted from the leaderboard")

    def test_slot_places_the_row_while_pos_stays_a_displayable_rank(self):
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})

        rows = self._poll(overlay).data["booyah"]["board"]["rows"]

        # Slot 1 = the team, and its `pos` is its rank on the leaderboard (Alpha leads on 33 pts).
        self.assertEqual(rows[0]["slot"], BOOYAH_TEAM_SLOT)
        self.assertEqual(rows[0]["pos"], 1)
        # Slots 2, 3, 4 = the squad, and each `pos` is the rank WITHIN the squad: 1, 2, 3.
        self.assertEqual([r["slot"] for r in rows[1:]],
                         [BOOYAH_FIRST_PLAYER_SLOT, BOOYAH_FIRST_PLAYER_SLOT + 1,
                          BOOYAH_FIRST_PLAYER_SLOT + 2])
        self.assertEqual([r["pos"] for r in rows[1:]], [1, 2, 3])
        # Every row is uniquely identified, or the FE would collapse the squad onto one React key
        # (they all share a team_name).
        self.assertEqual(len({r["row_key"] for r in rows}), len(rows))

    def test_players_carry_their_map_stats_ordered_by_kills(self):
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})

        players = self._poll(overlay).data["booyah"]["board"]["rows"][1:]

        self.assertEqual([p["player_name"] for p in players], ["ACE", "MID", "SLOWHAND"])
        self.assertEqual([p["kills"] for p in players], [5, 2, 1])
        self.assertEqual([p["damage"] for p in players], [1800, 900, 400])
        self.assertEqual([p["assists"] for p in players], [3, 1, 0])
        # Team context is repeated per player, so a design may show the crest beside each of them.
        self.assertTrue(all(p["team_name"] == "Alpha" for p in players))
        self.assertTrue(all(p["match_map"] == "purgatory" for p in players))

    def test_map_name_picks_the_right_win_when_a_team_won_twice(self):
        # Alpha also wins map 1 (an edit/re-upload turns its 2nd place into a booyah). With two wins
        # on record, config.match_map is what tells the two apart.
        TournamentTeamMatchStats.objects.filter(
            match=self.map1, tournament_team=self.tt).update(placement=1)
        design = self._design("booyah")
        overlay = self._overlay(
            {"design_id": design.id, "team_name": "Alpha", "match_map": "bermuda"})

        rows = self._poll(overlay).data["booyah"]["board"]["rows"]

        self.assertEqual(rows[0]["match_map"], "bermuda")
        # Map 1 has no per-player rows in this fixture, so the roster fallback fills the block.
        self.assertEqual(len(rows), 4)

    def test_live_mode_resolves_the_latest_booyah(self):
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "live": True})

        resp = self._poll(overlay)

        # No team was ever picked on this card: live mode resolved the event's most recent win.
        self.assertEqual(resp.data["config"]["team_name"], "Alpha")
        self.assertEqual(resp.data["booyah"]["board"]["rows"][0]["match_map"], "purgatory")

    def test_roster_fallback_when_the_map_has_no_player_rows(self):
        TournamentPlayerMatchStats.objects.filter(team_stats=self.win).delete()
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})

        players = self._poll(overlay).data["booyah"]["board"]["rows"][1:]

        # The block is filled from the event roster instead of being empty, with zeroed stats.
        self.assertEqual(sorted(p["player_name"] for p in players), ["ACE", "MID", "SLOWHAND"])
        self.assertTrue(all(p["kills"] == 0 for p in players))

    def test_no_board_until_a_booyah_exists(self):
        TournamentTeamMatchStats.objects.filter(placement=1).update(placement=3)
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "live": True})

        # Nothing has been won yet, so there is nothing to draw and the source stays clean.
        self.assertIsNone(self._poll(overlay).data["booyah"]["board"])

    def test_media_opt_out_keeps_a_players_photo_off_the_board(self):
        ace = next(u for u in self.players if u.username == "ACE")
        EventMediaOptOut.objects.create(
            event=self.event, kind="esports_image", user=ace)
        design = self._design("booyah")
        overlay = self._overlay({"design_id": design.id, "team_name": "Alpha"})

        players = self._poll(overlay).data["booyah"]["board"]["rows"][1:]

        self.assertIsNone(next(p for p in players if p["player_name"] == "ACE")["esports_image"])


class BooyahDesignFieldTypeTests(TestCase):
    """The design editor has to be able to PLACE the columns a booyah design needs. `match_map` is
    new; `team_flag` and the player columns were implemented in both renderers but `team_flag` was
    never added to the backend choice list, so the palette chip 400'd."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="boo_fld_admin", email="boo_fld@x.com", full_name="Fld Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="boo-fld-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.design = OrgLeaderboardDesign.objects.create(name="Booyah art", design_type="booyah")

    def _add(self, field_type):
        return self.client.post(
            f"/organizers/leaderboard-designs/by-id/{self.design.id}/fields/",
            {"field_type": field_type, "column_group": 0, "x_pct": 40.0},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_booyah_columns_can_be_placed(self):
        for field_type in ("match_map", "team_flag", "player_name", "damage", "assists"):
            with self.subTest(field_type=field_type):
                resp = self._add(field_type)
                self.assertEqual(resp.status_code, 201, resp.data)
                self.assertEqual(resp.data["field"]["field_type"], field_type)

    def test_unknown_column_is_still_rejected(self):
        self.assertEqual(self._add("not_a_stat").status_code, 400)

    def test_booyah_is_an_accepted_design_type(self):
        resp = self.client.patch(
            f"/organizers/leaderboard-designs/by-id/{self.design.id}/",
            {"design_type": "booyah"}, format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.design.refresh_from_db()
        self.assertEqual(self.design.design_type, "booyah")
