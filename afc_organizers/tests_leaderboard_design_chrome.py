"""
Board-chrome settings on a leaderboard design - owner 2026-08-05, backlog #2.

The exported graphic was missing a column-header row, grid rules, and the event/stage header. Those
are now three booleans on OrgLeaderboardDesign that the renderer reads off the baked field_layout.
This file guards the SETTINGS half of that (the drawing half is
afc_leaderboard.tests.test_graphic_chrome):

    • the three flags default OFF, so every design that already exists renders exactly as before
    • the editor can turn them on over the design PATCH, and they come back on the serialized design
    • build_field_layout / build_pages_for_export BAKE them into the layout the renderer consumes,
      including on every page of a multi-page design
    • the one-click AFC default is created with all three ON, since that is the graphic the owner
      reported as missing them, and it places the MP + points columns the headers name

Run: .venv\\Scripts\\python.exe manage.py test afc_organizers.tests_leaderboard_design_chrome
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_organizers.models import OrgLeaderboardDesign, OrgLeaderboardDesignField
from afc_organizers.views_leaderboard_design import build_field_layout, build_pages_for_export


class DesignChromeSettingsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="chrome_admin", email="chrome_admin@x.com", full_name="Chrome Admin",
            role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="chrome-admin-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token.token}"}
        # A design in the AFC-native library (organization=null) with one placed column, which is
        # what switches the renderer to its field-layout path.
        self.design = OrgLeaderboardDesign.objects.create(
            organization=None, name="Plain board", created_by=self.admin,
            column_groups=[{"row_start_pct": 30.0, "row_height_pct": 5.0,
                            "row_count": 8, "start_rank": 1}])
        OrgLeaderboardDesignField.objects.create(
            design=self.design, field_type="total_points", x_pct=90.0, align="center")

    def test_flags_default_off_so_existing_designs_are_unchanged(self):
        self.assertFalse(self.design.show_column_headers)
        self.assertFalse(self.design.show_grid)
        self.assertFalse(self.design.show_board_header)
        layout = build_field_layout(self.design)
        self.assertFalse(layout["show_column_headers"])
        self.assertFalse(layout["show_grid"])
        self.assertFalse(layout["show_board_header"])

    def test_editor_can_switch_them_on_over_the_design_patch(self):
        resp = self.client.patch(
            f"/organizers/leaderboard-designs/by-id/{self.design.id}/",
            {"show_column_headers": "true", "show_grid": "true", "show_board_header": "true"},
            format="multipart", **self.auth)
        self.assertEqual(resp.status_code, 200)
        design = resp.json()["design"]
        self.assertTrue(design["show_column_headers"])
        self.assertTrue(design["show_grid"])
        self.assertTrue(design["show_board_header"])

        self.design.refresh_from_db()
        layout = build_field_layout(self.design)
        self.assertTrue(layout["show_column_headers"])
        self.assertTrue(layout["show_grid"])
        self.assertTrue(layout["show_board_header"])

    def test_switching_one_off_again_sticks(self):
        self.client.patch(
            f"/organizers/leaderboard-designs/by-id/{self.design.id}/",
            {"show_grid": "true"}, format="multipart", **self.auth)
        resp = self.client.patch(
            f"/organizers/leaderboard-designs/by-id/{self.design.id}/",
            {"show_grid": "false"}, format="multipart", **self.auth)
        self.assertFalse(resp.json()["design"]["show_grid"])

    def test_every_export_page_carries_the_chrome(self):
        # Chrome is a design-level setting, so a multi-page export must not lose it on page 2.
        self.design.show_column_headers = True
        self.design.show_grid = True
        self.design.save(update_fields=["show_column_headers", "show_grid"])
        self.client.post(f"/organizers/leaderboard-designs/by-id/{self.design.id}/pages/",
                         {}, format="multipart", **self.auth)
        self.design.refresh_from_db()
        pages = build_pages_for_export(self.design)
        self.assertGreater(len(pages), 1)
        for page in pages:
            layout = page["field_layout"]
            if layout is None:
                continue  # a page with no placed column has no layout, which is expected
            self.assertTrue(layout["show_column_headers"])
            self.assertTrue(layout["show_grid"])

    def test_one_click_afc_default_ships_with_the_chrome_and_the_named_columns(self):
        resp = self.client.post("/organizers/leaderboard-designs/create-default/",
                                {"preset": "12"}, format="multipart", **self.auth)
        self.assertEqual(resp.status_code, 201)
        design = resp.json()["design"]
        self.assertTrue(design["show_column_headers"])
        self.assertTrue(design["show_grid"])
        self.assertTrue(design["show_board_header"])
        placed = {f["field_type"] for f in design["fields"]}
        # The stat columns the owner named as needing headers, plus maps played (backlog #17).
        for field_type in ("matches", "booyah", "kill_points", "placement_points", "total_points"):
            self.assertIn(field_type, placed)
