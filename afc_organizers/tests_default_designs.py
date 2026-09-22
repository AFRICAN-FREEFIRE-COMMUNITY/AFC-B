"""
afc_organizers/tests_default_designs.py
================================================================================
Default overlay designs for every live scene (owner 2026-09-22, inbox #38).

Owner: "I also want us to design default designs that can be used for any event as overlay."

Before this, one click produced a STANDINGS board only (12 / 15 / 24 teams); the booyah moment, the
MVP, the top killers and the head to head could only be laid out by hand, so most events ran them on
the built-in fallback. What is proven here:

  1. each live-scene preset creates a design carrying that scene's marker, its row tiling and its
     placed columns, so the overlay renders THROUGH it instead of the built-in layout,
  2. "set" creates the whole kit in one request, and the standings board (not a scene) is the one
     that becomes the library default,
  3. the scene boards actually use what AFC Capture records (knocks and headshots beside kills),
  4. an unknown preset is refused with a sentence naming the choices.

Run: python manage.py test afc_organizers.tests_default_designs
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_organizers.models import OrgLeaderboardDesign
from afc_organizers.views_leaderboard_design import _DEFAULT_SET, _SCENE_PRESETS

URL = "/organizers/leaderboard-designs/create-default/"


class DefaultDesignPresetTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="designadmin", email="designadmin@x.com", full_name="Design Admin",
            role="admin", password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin, token="design-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )

    def _create(self, preset):
        return self.client.post(URL, data={"preset": preset},
                                HTTP_AUTHORIZATION="Bearer %s" % self.token.token)

    def test_each_scene_preset_creates_a_design_marked_for_that_scene(self):
        for preset, spec in _SCENE_PRESETS.items():
            resp = self._create(preset)
            self.assertEqual(resp.status_code, 201, (preset, resp.data))
            design = resp.data["design"]
            self.assertEqual(design["design_type"], spec["design_type"], preset)
            self.assertEqual(design["max_rows"], spec["max_rows"], preset)
            self.assertEqual(len(design["column_groups"]), len(spec["column_groups"]), preset)
            placed = sum(len(c) for c in spec["columns_by_group"])
            self.assertEqual(len(design["fields"]), placed, preset)
            self.assertIn(spec["label"], design["name"], preset)

    def test_the_booyah_default_is_the_team_then_its_players(self):
        resp = self._create("booyah")
        design = resp.data["design"]
        groups = design["column_groups"]
        self.assertEqual(groups[0]["row_count"], 1)          # the winning team
        self.assertEqual(groups[1]["row_count"], 4)          # its players
        self.assertEqual(groups[1]["start_rank"], 2)
        by_group = {}
        for f in design["fields"]:
            by_group.setdefault(f["column_group"], set()).add(f["field_type"])
        self.assertIn("team_name", by_group[0])
        self.assertIn("player_name", by_group[1])

    def test_the_head_to_head_default_is_two_sides_at_the_same_height(self):
        design = self._create("h2h").data["design"]
        groups = design["column_groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["row_start_pct"], groups[1]["row_start_pct"])
        self.assertEqual((groups[0]["row_count"], groups[1]["row_count"]), (1, 1))
        xs = {f["column_group"]: [] for f in design["fields"]}
        for f in design["fields"]:
            xs[f["column_group"]].append(f["x_pct"])
        self.assertTrue(max(xs[0]) < 50 < min(xs[1]), xs)     # left side, then right side

    def test_the_scene_boards_show_what_afc_capture_records(self):
        # The live stream carries more than kills, so the defaults show knocks and headshots beside
        # them. Grenade kills is NOT in a default layout on purpose: the walk on 2026-09-22 showed
        # that a fourth stat column pushes the header labels into each other at portrait width. It
        # stays in the palette, one drag away, for an operator who wants it.
        placed = set()
        for preset in _SCENE_PRESETS:
            for f in self._create(preset).data["design"]["fields"]:
                placed.add(f["field_type"])
        for key in ("kills", "knockdowns", "headshots"):
            self.assertIn(key, placed)
        # and the boards are built from player rows, not team rows
        self.assertIn("player_name", placed)
        self.assertIn("esports_image", placed)

    def test_set_creates_the_whole_kit_and_the_standings_board_is_the_default(self):
        resp = self._create("set")
        self.assertEqual(resp.status_code, 201, resp.data)
        designs = resp.data["designs"]
        self.assertEqual(len(designs), len(_DEFAULT_SET))
        types = [d["design_type"] for d in designs]
        self.assertEqual(types[0], "leaderboard")
        self.assertEqual(sorted(types[1:]),
                         sorted(_SCENE_PRESETS[p]["design_type"] for p in _DEFAULT_SET[1:]))
        default_rows = OrgLeaderboardDesign.objects.filter(organization=None, is_default=True)
        self.assertEqual(default_rows.count(), 1)
        self.assertEqual(default_rows.first().design_type, "leaderboard")
        self.assertIn("Created 5 designs", resp.data["note"])

    def test_an_unknown_preset_is_refused_with_the_choices(self):
        resp = self._create("banner")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("booyah", resp.data["message"])

    def test_the_standings_presets_still_work_unchanged(self):
        for preset, rows in (("12", 12), ("15", 15), ("24", 24)):
            design = self._create(preset).data["design"]
            self.assertEqual(design["design_type"], "leaderboard")
            self.assertEqual(design["max_rows"], rows)
