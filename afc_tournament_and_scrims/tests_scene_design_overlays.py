"""
DESIGN-DRIVEN MVP, TOP-KILLER and HEAD-TO-HEAD overlays - owner 2026-08-08.

THE ASK
    "Fix up the MVP, top killer and h2h overlays like we did the booyah work."

    The booyah work (2026-08-06, tests_booyah_design_overlay.py) stopped that scene being a
    hard-coded banner: bind a design whose design_type is "booyah" and the public config poll ships
    that whole design plus the resolved rows, which the frontend renders through DesignBoard - the
    same board renderer the live leaderboard overlay uses. This is the same move for the other three.

WHAT CHANGED
    Each scene gained its OWN design_type, and its payload gained ONE additive key, `board`:
        design_type "mvp"          -> _mvp_payload["board"]
        design_type "top_killers"  -> _top_killers_payload["board"]
        design_type "h2h"          -> _h2h_payload["board"]
    `board` is {design: <full _serialize_design>, rows: [...], size: "youtube"} or None. None means
    "keep the built-in layout", and it is None for every overlay that existed before this change.

THE ROW CONTRACTS
    MVP / top killers - the ranked player rows build_player_design_rows already produces, plus a
        stable `row_key`. No `slot`: a player board IS a ranked list, so `pos` is the slot and the
        DESIGN alone decides how much of the ranking shows (a one-row column group is the MVP alone).
    Head to head     - one row per side. `slot` 1 / 2 / 3 is WHICH SIDE, so a design lays two sides
        out as two column groups of one row each, at the same Y, with fields at left and right x.

WHAT THESE TESTS GUARD
    • MIGRATION SAFETY, the thing that must not break: an overlay with no design, and one with a
      leaderboard design, get board=None and keep their built-in payload untouched
    • a board appears only for a design of that scene's OWN type (an MVP design does not drive a
      top-killer overlay, and none of them drives a booyah)
    • the rows a design-driven board ships are the SAME numbers the built-in layout shows
    • every row is uniquely identified - without which teammates collapse onto one React key and a
      block of five players renders as one
    • head to head maps its stat vocabulary onto the ordinary design columns, and refuses to draw
      with fewer than two sides or in bracket mode
    • a player who opted out of broadcast media keeps their photo off every one of these boards
    • the design editor accepts the three new design types, and needs NO new field types

Run: .venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_scene_design_overlays
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User, UserProfile
from afc_organizers.models import OrgLeaderboardDesign, OrgLeaderboardDesignField
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, EventMediaOptOut, EventOverlay, Leaderboard, Match, Stages, StageGroups,
    TournamentTeam, TournamentTeamMember, TournamentPlayerMatchStats, TournamentTeamMatchStats,
)
from afc_tournament_and_scrims.views_overlays import H2H_FIRST_SLOT


class SceneDesignOverlayBase(TestCase):
    """One squad event, one group, two maps, two teams of two players each.

    Teammates matter: the row-identity bug these boards had to fix only shows up when two rows carry
    the same team_name, which is the normal case on a player board. Kill counts are deliberately
    interleaved across the two teams so a ranking assertion cannot pass by team order."""

    def setUp(self):
        self.client = APIClient()
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="scene_admin", email="scene_admin@x.com", full_name="Scene Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="scene-admin-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Scene Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin,
            overlay_token="scene-overlay-token")
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

        # Two teams, both with a logo on file so the h2h board has an image to suppress.
        self.alpha = Team.objects.create(
            team_name="Alpha", team_tag="ALP", join_settings="open", team_creator=self.admin,
            team_owner=self.admin, country="NG", team_logo="team_logos/alpha.png")
        self.bravo = Team.objects.create(
            team_name="Bravo", team_tag="BRV", join_settings="open", team_creator=self.admin,
            team_owner=self.admin, country="GH", team_logo="team_logos/bravo.png")
        self.tt_alpha = TournamentTeam.objects.create(
            event=self.event, team=self.alpha, registered_by=self.admin)
        self.tt_bravo = TournamentTeam.objects.create(
            event=self.event, team=self.bravo, registered_by=self.admin)

        # Map 1: Alpha 2nd, Bravo 1st. Map 2: Alpha 1st, Bravo 2nd.
        self.a1 = TournamentTeamMatchStats.objects.create(
            match=self.map1, tournament_team=self.tt_alpha, placement=2, kills=4,
            placement_points=9, kill_points=4, total_points=13, played=True)
        self.b1 = TournamentTeamMatchStats.objects.create(
            match=self.map1, tournament_team=self.tt_bravo, placement=1, kills=2,
            placement_points=12, kill_points=2, total_points=14, played=True)
        self.a2 = TournamentTeamMatchStats.objects.create(
            match=self.map2, tournament_team=self.tt_alpha, placement=1, kills=8,
            placement_points=12, kill_points=8, total_points=20, played=True)
        self.b2 = TournamentTeamMatchStats.objects.create(
            match=self.map2, tournament_team=self.tt_bravo, placement=2, kills=1,
            placement_points=9, kill_points=1, total_points=10, played=True)

        # Four players, two per team, every one with an esport photo on file.
        # Event totals over the two maps: ACE 9, BOLT 6, CRUX 3, DUSK 1 kills.
        #   ACE + BOLT are Alpha; CRUX + DUSK are Bravo - so the top two of the kills ranking are
        #   TEAMMATES, which is exactly the case a shared row identity would collapse.
        # NOTE: the display name is the USERNAME. `in_game_name` is not a column on User (it is
        # commented out in afc_auth.models), which is why every producer of a player row reads it
        # with a getattr fallback to username; naming the users after their IGN keeps this readable.
        self.players = {}
        for name, tt, per_map in (
            ("ACE", self.tt_alpha, ((self.a1, 4, 1200, 2), (self.a2, 5, 1500, 1))),
            ("BOLT", self.tt_alpha, ((self.a1, 0, 300, 0), (self.a2, 6, 1800, 3))),
            ("CRUX", self.tt_bravo, ((self.b1, 2, 900, 1), (self.b2, 1, 400, 0))),
            ("DUSK", self.tt_bravo, ((self.b1, 0, 100, 0), (self.b2, 1, 250, 2))),
        ):
            user = User.objects.create(
                username=name, email=f"scene_{name.lower()}@x.com", full_name=name, role="player")
            UserProfile.objects.create(
                user=user, esports_pic=f"esports_pictures/{name.lower()}.png")
            TournamentTeamMember.objects.create(tournament_team=tt, user=user)
            self.players[name] = user
            for team_stats, kills, damage, assists in per_map:
                TournamentPlayerMatchStats.objects.create(
                    team_stats=team_stats, player=user, kills=kills, damage=damage,
                    assists=assists, played=True)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _design(self, design_type, field_types=("player_name",)):
        """A minimal design of the given type with real placed columns, so the board ships something
        an operator could actually have laid out rather than an empty shell."""
        design = OrgLeaderboardDesign.objects.create(
            name=f"{design_type} design", design_type=design_type,
            column_groups=[{"row_start_pct": 30.0, "row_height_pct": 8.0,
                            "row_count": 1, "start_rank": 1},
                           {"row_start_pct": 30.0, "row_height_pct": 8.0,
                            "row_count": 1, "start_rank": 2}])
        for i, field_type in enumerate(field_types):
            OrgLeaderboardDesignField.objects.create(
                design=design, field_type=field_type, column_group=0, x_pct=20.0 + i * 10)
        return design

    def _overlay(self, kind, config):
        return EventOverlay.objects.create(
            event=self.event, name=kind, kind=kind, config=config, active=True)

    def _poll(self, overlay):
        """The PUBLIC config poll the OBS browser source hits once a second."""
        return self.client.get(
            "/events/overlay/config/",
            {"token": self.event.overlay_token, "overlay": overlay.id})


class PlayerBoardDesignOverlayTests(SceneDesignOverlayBase):
    """MVP (kind "mvp") and TOP KILLERS (kind "top_killers"): one ranked-player implementation, so
    these cover both and assert the two never leak into each other."""

    # ── migration safety: existing overlays must not change ────────────────────
    def test_overlay_with_no_design_keeps_the_built_in_board(self):
        overlay = self._overlay("top_killers", {})

        resp = self._poll(overlay)

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["top_killers"]["board"])
        # The built-in list's inputs are untouched, so the old renderer still has what it needs.
        self.assertEqual([p["player_name"] for p in resp.data["top_killers"]["players"]],
                         ["ACE", "BOLT", "CRUX", "DUSK"])

    def test_overlay_bound_to_a_leaderboard_design_keeps_the_built_in_board(self):
        # This is the state EVERY mvp / top-killer overlay configured before 2026-08-08 is in.
        design = self._design("leaderboard")
        overlay = self._overlay("mvp", {"design_id": design.id})

        resp = self._poll(overlay)

        self.assertIsNone(resp.data["mvp"]["board"])
        # ...and it still gets the 4-key look the built-in board styles itself with.
        self.assertIsNotNone(resp.data["mvp"]["design"])
        self.assertTrue(resp.data["mvp"]["players"])

    def test_board_appears_only_for_a_matching_design_type(self):
        design = self._design("mvp", field_types=("pos", "player_name", "mvp_count"))
        overlay = self._overlay("mvp", {"design_id": design.id})

        board = self._poll(overlay).data["mvp"]["board"]

        self.assertIsNotNone(board)
        self.assertEqual(board["design"]["id"], design.id)
        self.assertEqual(board["design"]["design_type"], "mvp")
        self.assertEqual(board["size"], "youtube")
        # The FULL design rides along, not a 4-key look: the FE needs the placed fields to draw it.
        self.assertEqual(sorted(f["field_type"] for f in board["design"]["fields"]),
                         ["mvp_count", "player_name", "pos"])

    def test_an_mvp_design_does_not_drive_a_top_killer_overlay(self):
        # Each scene reads its OWN type, so pointing a top-killer card at an MVP design leaves it on
        # the built-in board rather than silently rendering a layout meant for another scene.
        design = self._design("mvp")
        overlay = self._overlay("top_killers", {"design_id": design.id})

        self.assertIsNone(self._poll(overlay).data["top_killers"]["board"])

    def test_a_top_killer_design_does_not_drive_a_booyah_overlay(self):
        design = self._design("top_killers")
        overlay = self._overlay("booyah", {"design_id": design.id, "team_name": "Alpha"})

        self.assertIsNone(self._poll(overlay).data["booyah"]["board"])

    # ── the row contract ───────────────────────────────────────────────────────
    def test_board_rows_are_the_same_rows_the_built_in_board_shows(self):
        design = self._design("top_killers")
        overlay = self._overlay("top_killers", {"design_id": design.id})

        payload = self._poll(overlay).data["top_killers"]

        # Same list, same order, same numbers: the design changes how it is DRAWN, never what it says.
        self.assertEqual(len(payload["board"]["rows"]), len(payload["players"]))
        for board_row, built_in in zip(payload["board"]["rows"], payload["players"]):
            for key, value in built_in.items():
                self.assertEqual(board_row[key], value, f"{key} drifted from the built-in board")

    def test_ranking_and_stats_reach_the_board(self):
        design = self._design("top_killers")
        overlay = self._overlay("top_killers", {"design_id": design.id})

        rows = self._poll(overlay).data["top_killers"]["board"]["rows"]

        self.assertEqual([r["player_name"] for r in rows], ["ACE", "BOLT", "CRUX", "DUSK"])
        self.assertEqual([r["pos"] for r in rows], [1, 2, 3, 4])
        self.assertEqual([r["kills"] for r in rows], [9, 6, 3, 1])
        self.assertEqual([r["damage"] for r in rows], [2700, 2100, 1300, 350])
        self.assertEqual([r["team_name"] for r in rows], ["Alpha", "Alpha", "Bravo", "Bravo"])

    def test_teammates_get_distinct_row_identities(self):
        # THE bug this key exists for: DesignBoard identifies a row by row_key ?? team_name, and the
        # top two here are teammates. Without row_key both collapse onto one React element and the
        # board renders one player where the operator placed two.
        design = self._design("top_killers")
        overlay = self._overlay("top_killers", {"design_id": design.id})

        rows = self._poll(overlay).data["top_killers"]["board"]["rows"]

        self.assertEqual(rows[0]["team_name"], rows[1]["team_name"])
        self.assertEqual(len({r["row_key"] for r in rows}), len(rows))
        self.assertEqual(rows[0]["row_key"], f"player-{self.players['ACE'].user_id}")

    def test_no_slot_key_so_the_design_alone_decides_how_many_players_show(self):
        # A ranked list needs no slot: `pos` IS the slot (DesignBoard reads `slot ?? pos`). Leaving it
        # off is what makes a one-row column group mean "just the MVP" and a ten-row group mean the
        # top ten, with no server-side setting to keep in step.
        design = self._design("mvp")
        overlay = self._overlay("mvp", {"design_id": design.id})

        rows = self._poll(overlay).data["mvp"]["board"]["rows"]

        self.assertTrue(all("slot" not in r for r in rows))
        self.assertEqual(rows[0]["pos"], 1)

    def test_media_opt_out_keeps_a_players_photo_off_the_board(self):
        EventMediaOptOut.objects.create(
            event=self.event, kind="esports_image", user=self.players["ACE"])
        design = self._design("top_killers")
        overlay = self._overlay("top_killers", {"design_id": design.id})

        rows = self._poll(overlay).data["top_killers"]["board"]["rows"]

        self.assertIsNone(next(r for r in rows if r["player_name"] == "ACE")["esports_image"])
        # ...and nobody else's photo is touched.
        self.assertIsNotNone(next(r for r in rows if r["player_name"] == "BOLT")["esports_image"])

    def test_combine_scope_still_applies_to_the_board(self):
        # The design-driven path reuses the SAME computation, so an overlay scoped to one group is
        # scoped on the board too - a design never widens what the numbers cover.
        design = self._design("top_killers")
        overlay = self._overlay(
            "top_killers", {"design_id": design.id, "group_ids": [self.group.group_id]})

        payload = self._poll(overlay).data["top_killers"]

        self.assertTrue(payload["combine"]["combined"])
        self.assertEqual([r["kills"] for r in payload["board"]["rows"]], [9, 6, 3, 1])

    def test_no_board_when_the_event_has_no_players_yet(self):
        TournamentPlayerMatchStats.objects.all().delete()
        design = self._design("mvp")
        overlay = self._overlay("mvp", {"design_id": design.id})

        # Nothing to draw, so the source stays on the built-in board rather than showing an empty one.
        self.assertIsNone(self._poll(overlay).data["mvp"]["board"])


class TopKillerMapMvpCountTests(SceneDesignOverlayBase):
    """MAP MVPS on a TOP-KILLER board (owner 2026-08-08).

    Found while walking the design-driven boards: compute_top_killers left `mvp_count` at 0 for every
    player. That was invisible for a year because the top-killer overlay drew a built-in list of names
    and kills, but a design can PLACE a MAP MVPS column, and it would then have put 0 on air beside a
    player the MVP board - computed from the SAME stat lines - credited with 1. Both now award map
    MVPs through the shared _award_map_mvps with the event's own saved arrangement.

    The fixture makes this unambiguous: ACE wins map 1 on kills (4 to 2) and BOLT wins map 2 (6 to 5),
    so exactly two players hold one map MVP each, and the two boards must agree on which two."""

    def _rows(self, kind):
        design = self._design(kind)
        overlay = self._overlay(kind, {"design_id": design.id})
        return self._poll(overlay).data[kind]["board"]["rows"]

    def test_a_top_killer_board_reports_the_map_mvps_a_player_actually_won(self):
        rows = self._rows("top_killers")

        self.assertEqual([r["player_name"] for r in rows], ["ACE", "BOLT", "CRUX", "DUSK"])
        # ACE won map 1, BOLT won map 2, nobody else won one. A real 0 for CRUX and DUSK, not a gap.
        self.assertEqual([r["mvp_count"] for r in rows], [1, 1, 0, 0])

    def test_the_two_boards_never_disagree_about_a_players_map_mvps(self):
        by_player = {r["player_name"]: r["mvp_count"] for r in self._rows("mvp")}

        for row in self._rows("top_killers"):
            with self.subTest(player=row["player_name"]):
                self.assertEqual(row["mvp_count"], by_player[row["player_name"]])

    def test_the_top_killer_ranking_is_still_by_kills(self):
        # Awarding map MVPs must not reorder this board: it is a KILLS board, and BOLT holds a map MVP
        # yet has fewer kills than ACE, so a ranking that had quietly picked up mvp_count would show.
        rows = self._rows("top_killers")

        self.assertEqual([r["kills"] for r in rows], [9, 6, 3, 1])
        self.assertEqual([r["pos"] for r in rows], [1, 2, 3, 4])


class OverlayTokenExposureTests(SceneDesignOverlayBase):
    """WHAT THE UNAUTHENTICATED OBS URL HANDS OUT, and what happens when it is revoked.

    A design-driven board is a bigger payload than the 4-key `design` look these scenes used to ship:
    it carries the WHOLE design, including its background and logo URLs. The read capability is
    Event.overlay_token and nothing else, so these pin down that (a) the token is genuinely required,
    and (b) rotating it (events/<id>/overlay/token/ with regenerate, the only revocation there is)
    stops the old link dead rather than leaving it rendering."""

    def test_a_wrong_token_gets_nothing(self):
        design = self._design("mvp")
        overlay = self._overlay("mvp", {"design_id": design.id})

        resp = self.client.get(
            "/events/overlay/config/", {"token": "not-the-token", "overlay": overlay.id})

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("mvp", resp.data)

    def test_rotating_the_token_kills_the_old_link(self):
        # There is no expiry on an overlay token; rotation IS the revocation, so it has to bite.
        design = self._design("h2h")
        overlay = self._overlay("h2h", {"mode": "team", "design_id": design.id,
                                        "competitor_ids": [self.alpha.team_id, self.bravo.team_id]})
        old_token = self.event.overlay_token
        self.assertIsNotNone(self._poll(overlay).data["h2h"]["board"])

        from afc_tournament_and_scrims.models import _gen_overlay_token
        self.event.overlay_token = _gen_overlay_token()
        self.event.save(update_fields=["overlay_token"])

        stale = self.client.get(
            "/events/overlay/config/", {"token": old_token, "overlay": overlay.id})
        self.assertEqual(stale.status_code, 404)
        # ...and the new link renders the same board, so rotating costs the operator only a re-paste.
        self.event.refresh_from_db()
        self.assertIsNotNone(self._poll(overlay).data["h2h"]["board"])


class HeadToHeadDesignOverlayTests(SceneDesignOverlayBase):
    """HEAD TO HEAD (kind "h2h"): the awkward one, because two opposing sides are not a ranked list.

    The answer needed no new concept: a side is a ROW and `slot` says which side it is, so a design
    lays the two out as two column groups of one row each at the same Y, with their fields at
    left-hand and right-hand x. Three-way is a third group; a stacked versus is one group of two."""

    def _h2h(self, config):
        return self._overlay("h2h", config)

    # ── migration safety ───────────────────────────────────────────────────────
    def test_overlay_with_no_design_keeps_the_built_in_cards(self):
        overlay = self._h2h({"mode": "team",
                             "competitor_ids": [self.alpha.team_id, self.bravo.team_id]})

        resp = self._poll(overlay)

        self.assertIsNone(resp.data["h2h"]["board"])
        self.assertEqual([c["name"] for c in resp.data["h2h"]["competitors"]], ["Alpha", "Bravo"])

    def test_overlay_bound_to_a_versus_design_keeps_the_built_in_cards(self):
        # A "versus" design is the LEGACY head-to-head type: it lends its look + slot positions to the
        # built-in cards. Every h2h overlay configured before 2026-08-08 is on one of those (or none).
        design = self._design("versus")
        overlay = self._h2h({"mode": "team", "design_id": design.id,
                             "competitor_ids": [self.alpha.team_id, self.bravo.team_id]})

        resp = self._poll(overlay)

        self.assertIsNone(resp.data["h2h"]["board"])
        self.assertIsNotNone(resp.data["h2h"]["design"])
        self.assertEqual(len(resp.data["h2h"]["competitors"]), 2)

    # ── the row contract ───────────────────────────────────────────────────────
    def test_team_mode_board_puts_one_side_per_slot(self):
        design = self._design("h2h", field_types=("team_name", "kills", "total_points"))
        overlay = self._h2h({"mode": "team", "design_id": design.id,
                             "competitor_ids": [self.alpha.team_id, self.bravo.team_id]})

        board = self._poll(overlay).data["h2h"]["board"]

        self.assertEqual(board["design"]["design_type"], "h2h")
        rows = board["rows"]
        self.assertEqual([r["slot"] for r in rows], [H2H_FIRST_SLOT, H2H_FIRST_SLOT + 1])
        # `pos` carries the side number as something a design can actually print.
        self.assertEqual([r["pos"] for r in rows], [1, 2])
        self.assertEqual([r["team_name"] for r in rows], ["Alpha", "Bravo"])
        self.assertEqual(len({r["row_key"] for r in rows}), 2)

    def test_team_stats_map_onto_the_ordinary_design_columns(self):
        design = self._design("h2h", field_types=("team_name",))
        overlay = self._h2h({"mode": "team", "design_id": design.id,
                             "competitor_ids": [self.alpha.team_id, self.bravo.team_id]})

        alpha = self._poll(overlay).data["h2h"]["board"]["rows"][0]

        # The h2h aggregates use their own vocabulary (points / booyahs); the board translates them
        # to the field types an organizer already places on a leaderboard, so no new column is needed.
        self.assertEqual(alpha["kills"], 12)          # 4 + 8
        self.assertEqual(alpha["total_points"], 33)   # 13 + 20  (from "points")
        self.assertEqual(alpha["booyah"], 1)          # one map won (from "booyahs")
        self.assertEqual(alpha["matches"], 2)
        # Team identity, including the country a TEAM FLAG column resolves.
        self.assertEqual(alpha["team_country"], "NG")
        self.assertIn("alpha.png", alpha["team_logo"])
        # A player column on a team row is simply absent, and an absent key renders a blank cell.
        self.assertNotIn("player_name", alpha)

    def test_player_mode_board_carries_the_player_columns(self):
        design = self._design("h2h", field_types=("player_name", "damage"))
        overlay = self._h2h({
            "mode": "player", "design_id": design.id,
            "competitor_ids": [self.players["ACE"].user_id, self.players["CRUX"].user_id]})

        rows = self._poll(overlay).data["h2h"]["board"]["rows"]

        self.assertEqual([r["player_name"] for r in rows], ["ACE", "CRUX"])
        self.assertEqual([r["kills"] for r in rows], [9, 3])
        self.assertEqual([r["damage"] for r in rows], [2700, 1300])
        self.assertEqual([r["assists"] for r in rows], [3, 1])
        # survival_seconds is the aggregate's name; SURVIVAL TIME is the column's.
        self.assertEqual(rows[0]["survival_time"], 0)
        self.assertIn("ace.png", rows[0]["esports_image"])
        self.assertNotIn("team_name", rows[0])

    def test_three_way_comparison_gets_a_third_slot(self):
        charlie = Team.objects.create(
            team_name="Charlie", team_tag="CHR", join_settings="open", team_creator=self.admin,
            team_owner=self.admin, country="KE")
        TournamentTeam.objects.create(event=self.event, team=charlie, registered_by=self.admin)
        design = self._design("h2h")
        overlay = self._h2h({
            "mode": "team", "design_id": design.id,
            "competitor_ids": [self.alpha.team_id, self.bravo.team_id, charlie.team_id]})

        rows = self._poll(overlay).data["h2h"]["board"]["rows"]

        self.assertEqual([r["slot"] for r in rows], [1, 2, 3])

    def test_one_side_is_not_a_comparison(self):
        design = self._design("h2h")
        overlay = self._h2h({"mode": "team", "design_id": design.id,
                             "competitor_ids": [self.alpha.team_id]})

        # The built-in cards refuse to draw under two competitors for the same reason, so the two
        # paths agree on what "not ready" means and an operator never sees a half comparison.
        self.assertIsNone(self._poll(overlay).data["h2h"]["board"])

    def test_bracket_mode_never_goes_through_a_design(self):
        design = self._design("h2h")
        overlay = self._h2h({"mode": "bracket", "design_id": design.id})

        resp = self._poll(overlay)

        # A bracket is a tree, not rows, so it keeps its own renderer whatever design is bound.
        self.assertEqual(resp.data["h2h"]["mode"], "bracket")
        self.assertIsNone(resp.data["h2h"]["board"])

    def test_media_opt_out_keeps_a_teams_logo_off_the_board(self):
        EventMediaOptOut.objects.create(event=self.event, kind="team_logo", team=self.alpha)
        design = self._design("h2h")
        overlay = self._h2h({"mode": "team", "design_id": design.id,
                             "competitor_ids": [self.alpha.team_id, self.bravo.team_id]})

        rows = self._poll(overlay).data["h2h"]["board"]["rows"]

        self.assertIsNone(rows[0]["team_logo"])
        self.assertIsNotNone(rows[1]["team_logo"])


class SceneDesignTypeTests(TestCase):
    """The design editor has to be able to SAVE the three new types, and - unlike the booyah work,
    which needed a new `match_map` field type - has to need no new columns at all. Everything an MVP,
    top-killer or head-to-head board places already existed, which is why this change ships with no
    migration."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="scene_fld_admin", email="scene_fld@x.com", full_name="Fld Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="scene-fld-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.design = OrgLeaderboardDesign.objects.create(name="Scene art")

    def _patch(self, design_type):
        return self.client.patch(
            f"/organizers/leaderboard-designs/by-id/{self.design.id}/",
            {"design_type": design_type}, format="multipart",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_the_three_new_design_types_are_accepted(self):
        for design_type in ("mvp", "top_killers", "h2h"):
            with self.subTest(design_type=design_type):
                resp = self._patch(design_type)
                self.assertEqual(resp.status_code, 200, resp.data)
                self.design.refresh_from_db()
                self.assertEqual(self.design.design_type, design_type)

    def test_the_existing_design_types_still_save(self):
        for design_type in ("leaderboard", "versus", "booyah"):
            with self.subTest(design_type=design_type):
                self.assertEqual(self._patch(design_type).status_code, 200)
                self.design.refresh_from_db()
                self.assertEqual(self.design.design_type, design_type)

    def test_an_unknown_design_type_falls_back_to_leaderboard(self):
        self.assertEqual(self._patch("not_a_scene").status_code, 200)
        self.design.refresh_from_db()
        self.assertEqual(self.design.design_type, "leaderboard")

    def test_every_column_these_boards_need_can_already_be_placed(self):
        # The whole palette an MVP / top-killer / head-to-head design draws from. All of them were
        # already valid field types, so this change adds none and needs no migration.
        for field_type in ("pos", "player_name", "esports_image", "team_name", "team_logo",
                           "team_flag", "kills", "damage", "assists", "mvp_count", "matches",
                           "total_points", "booyah", "deaths", "headshots", "survival_time"):
            with self.subTest(field_type=field_type):
                resp = self.client.post(
                    f"/organizers/leaderboard-designs/by-id/{self.design.id}/fields/",
                    {"field_type": field_type, "column_group": 0, "x_pct": 40.0},
                    HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
                self.assertEqual(resp.status_code, 201, resp.data)
