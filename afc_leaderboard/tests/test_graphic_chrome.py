"""
Tests for the exported graphic's BOARD CHROME (owner 2026-08-05, backlog #2, #3, #17).

WHAT IS COVERED
    1. Column headers   - the label row above each placed column appears in the rendered PNG, only
                          when the layout asks for it, and never disturbs the standings rows.
    2. Grid lines       - hairline rules land on the row boundaries AND the column boundaries.
    3. Board header     - the event name / stage name are drawn at the top of a field-layout board.
    4. Column geometry  - _column_edges gives a LEFT-aligned name column the whole wide gap in front
                          of the numbers (a plain midpoint would rule straight through the names).
    5. Labels           - every column the AFC default places has a header, and `matches` reads MP.
    6. MVP graphic      - the player board renders with a real photo AND with none (placeholder).

HOW A "DID IT DRAW?" ASSERTION WORKS
    The output is a PNG, so we compare TWO renders that differ only by the switch under test and
    count the pixels that changed inside the band the layer is supposed to occupy. That proves the
    layer drew where it should and, just as importantly, that it left the rest of the board alone.

SimpleTestCase: pure rendering, no ORM. The layouts come from the real builders in
afc_organizers.views_leaderboard_design so the test breaks if the default board's geometry drifts.
"""
import io

from django.test import SimpleTestCase
from PIL import Image

from afc_leaderboard.graphic import (
    COLUMN_HEADER_LABELS, _column_edges, render_leaderboard_graphic,
)
from afc_organizers.views_leaderboard_design import (
    _AFC_LOGO_ASSET, build_ephemeral_afc_default, build_ephemeral_afc_player_default,
)

# Instagram canvas, the size the default board is authored against.
W, H = 1080, 1350


def _pct_box(x0, y0, x1, y1):
    """A crop box from percentages of the canvas, so bands are expressed in the same units the
    layout uses (row_start_pct etc.) rather than as magic pixel numbers."""
    return (int(x0 / 100 * W), int(y0 / 100 * H), int(x1 / 100 * W), int(y1 / 100 * H))


def _changed_pixels(png_a, png_b, box):
    """How many pixels differ between two renders inside `box`. Zero means the two renders are
    identical there; a large number means something was drawn in one and not the other."""
    a = Image.open(io.BytesIO(png_a)).convert("RGB").crop(box)
    b = Image.open(io.BytesIO(png_b)).convert("RGB").crop(box)
    return sum(1 for pa, pb in zip(a.getdata(), b.getdata()) if pa != pb)


class TeamBoardChromeTests(SimpleTestCase):
    """The default TEAM leaderboard graphic: headers, grid, event/stage header."""

    def setUp(self):
        self.rows = [{
            "pos": i + 1,
            "team_name": f"TEAM NUMBER {i + 1}",
            "team_logo": None,
            "matches": 6,
            "booyah": 3 - (i % 3),
            "kill_points": 40 - i,
            "placement_points": 55 - i,
            "total_points": 99 - i * 3,
        } for i in range(12)]
        eph = build_ephemeral_afc_default(len(self.rows))
        self.page = eph.pages_spec[0]
        self.layout = self.page["field_layout"]
        self.group = self.layout["column_groups"][0]

    def _render(self, **layout_overrides):
        layout = {**self.layout, **layout_overrides}
        return render_leaderboard_graphic(
            self.rows, size="instagram",
            background_path=self.page["background_instagram"].path,
            title="AFC Dynasty Cup", subtitle="Grand Finals",
            field_layout=layout, rows=self.rows,
        )

    def test_default_layout_switches_all_chrome_on(self):
        # The graphic the owner reported as missing headers/grid/header-text is this one, so the
        # branded default must ship with all three on.
        self.assertTrue(self.layout["show_column_headers"])
        self.assertTrue(self.layout["show_grid"])
        self.assertTrue(self.layout["show_board_header"])

    def test_column_headers_draw_above_the_rows_and_leave_rows_untouched(self):
        with_headers = self._render()
        without = self._render(show_column_headers=False)
        # The header band sits one row-height above row 1 (HEADER_ROW_GAP), so look either side of it.
        rs, rh = self.group["row_start_pct"], self.group["row_height_pct"]
        header_band = _pct_box(0, rs - rh * 2, 100, rs - rh * 0.5)
        self.assertGreater(_changed_pixels(with_headers, without, header_band), 500)
        # The standings themselves must be byte-identical: headers add, they never move the data.
        rows_band = _pct_box(0, rs, 100, rs + rh * (self.group["row_count"] - 1))
        self.assertEqual(_changed_pixels(with_headers, without, rows_band), 0)

    def test_grid_draws_on_the_row_and_column_boundaries(self):
        with_grid = self._render()
        without = self._render(show_grid=False)
        rs, rh = self.group["row_start_pct"], self.group["row_height_pct"]
        # A horizontal rule sits exactly half a row above row 1 (the top of the table).
        top_rule = _pct_box(0, rs - rh * 0.55, 100, rs - rh * 0.45)
        self.assertGreater(_changed_pixels(with_grid, without, top_rule), 200)
        # A vertical rule sits on the boundary in front of the first numeric column. The band is
        # taken between two rows so only the vertical rule can be responsible for the difference.
        edges = _column_edges(self.layout["fields"])
        mp_edge = next(e for e in edges if e > 45)
        column_rule = _pct_box(mp_edge - 0.5, rs + rh * 0.1, mp_edge + 0.5, rs + rh * 0.4)
        self.assertGreater(_changed_pixels(with_grid, without, column_rule), 10)

    def test_board_header_draws_the_event_and_stage_names(self):
        with_header = self._render()
        without = self._render(show_board_header=False)
        # Title ~6.5% and subtitle ~12% of canvas height (BOARD_TITLE/SUBTITLE_DEFAULTS).
        self.assertGreater(_changed_pixels(with_header, without, _pct_box(20, 3, 80, 9)), 500)
        self.assertGreater(_changed_pixels(with_header, without, _pct_box(20, 9, 80, 15)), 200)

    def test_board_header_honours_show_title_and_show_subtitle(self):
        # The two existing per-line gates still apply, so an operator can keep the header row and
        # drop just the sub-header.
        both = self._render()
        title_only = render_leaderboard_graphic(
            self.rows, size="instagram",
            background_path=self.page["background_instagram"].path,
            title="AFC Dynasty Cup", subtitle="Grand Finals",
            show_subtitle=False, field_layout=self.layout, rows=self.rows,
        )
        self.assertEqual(_changed_pixels(both, title_only, _pct_box(20, 3, 80, 9)), 0)
        self.assertGreater(_changed_pixels(both, title_only, _pct_box(20, 9, 80, 15)), 200)

    def test_chrome_is_opt_in_so_an_old_layout_renders_unchanged(self):
        # A layout built before this feature carries none of the chrome keys. It must render exactly
        # as a layout with all three explicitly off - that is the "existing graphics do not change"
        # guarantee.
        legacy = {k: v for k, v in self.layout.items()
                  if k not in ("show_column_headers", "show_grid", "show_board_header")}
        as_legacy = render_leaderboard_graphic(
            self.rows, size="instagram",
            background_path=self.page["background_instagram"].path,
            title="AFC Dynasty Cup", subtitle="Grand Finals",
            field_layout=legacy, rows=self.rows,
        )
        all_off = self._render(show_column_headers=False, show_grid=False, show_board_header=False)
        self.assertEqual(as_legacy, all_off)


class ColumnHeaderLabelTests(SimpleTestCase):
    """The label map and the column geometry it is measured against."""

    def test_every_default_column_has_a_header_and_matches_reads_mp(self):
        layout = build_ephemeral_afc_default(12).pages_spec[0]["field_layout"]
        placed = {f["field_type"] for f in layout["fields"]}
        # Maps played is the owner's MP column (backlog #17) and must be placed by default.
        self.assertIn("matches", placed)
        self.assertEqual(COLUMN_HEADER_LABELS["matches"], "MP")
        for field_type in placed:
            self.assertIn(field_type, COLUMN_HEADER_LABELS,
                          f"{field_type} is placed by default but has no header label")
        # The image column carries no label, so nothing is stamped over the team logo.
        self.assertEqual(COLUMN_HEADER_LABELS["team_logo"], "")

    def test_left_aligned_name_column_owns_the_gap_in_front_of_the_numbers(self):
        # This is the case a plain midpoint gets wrong: TEAM is left-aligned at 14.5% with the first
        # numeric column at 50%, so a midpoint rule would land at ~32% - through the team names.
        fields = [
            {"field_type": "pos", "x_pct": 5.0, "align": "center"},
            {"field_type": "team_name", "x_pct": 14.5, "align": "left"},
            {"field_type": "matches", "x_pct": 50.0, "align": "center"},
            {"field_type": "total_points", "x_pct": 60.0, "align": "center"},
        ]
        edges = _column_edges(fields)
        self.assertEqual(edges, sorted(edges), "edges must be monotonic")
        # The name column starts just in front of its anchor and runs to the numbers' boundary.
        self.assertAlmostEqual(edges[1], 13.5, places=6)
        self.assertGreater(edges[2], 40.0)

    def test_column_edges_handles_degenerate_layouts(self):
        self.assertEqual(_column_edges([]), [])
        self.assertEqual(len(_column_edges([{"x_pct": 50.0, "align": "center"}])), 2)


class PlayerBoardGraphicTests(SimpleTestCase):
    """The MVP / Top-killers graphic (backlog #3), which had no default layout at all before."""

    def setUp(self):
        self.eph = build_ephemeral_afc_player_default(10)
        self.page = self.eph.pages_spec[0]
        self.layout = self.page["field_layout"]

    def _render(self, rows, layout=None):
        return render_leaderboard_graphic(
            rows, size="instagram",
            background_path=self.page["background_instagram"].path,
            logos=self.eph.logos, title="AFC Dynasty Cup", subtitle="MVP",
            field_layout=layout or self.layout, rows=rows,
        )

    def _rows(self, image):
        return [{"pos": i + 1, "player_name": f"PLAYER{i + 1}", "esports_image": image,
                 "kills": 30 - i, "matches": 6} for i in range(10)]

    def test_places_the_columns_the_owner_asked_for(self):
        placed = [f["field_type"] for f in self.layout["fields"]]
        # Player images, kills and matches played are the three the owner named; pos + name make the
        # board readable.
        self.assertIn("esports_image", placed)
        self.assertIn("kills", placed)
        self.assertIn("matches", placed)
        self.assertIn("player_name", placed)

    def test_renders_with_a_player_image(self):
        # A real file on disk stands in for an uploaded esport photo.
        png = self._render(self._rows(_AFC_LOGO_ASSET))
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(Image.open(io.BytesIO(png)).size, (W, H))

    def test_renders_without_a_player_image_using_the_placeholder(self):
        # No photo at all: the board must still show a portrait in every slot.
        with_placeholder = self._render(self._rows(None))
        self.assertTrue(with_placeholder.startswith(b"\x89PNG"))
        # Same rows through a layout with NO placeholder configured leaves the photo cells empty, so
        # the difference between the two proves the placeholder art was actually pasted.
        bare = {k: v for k, v in self.layout.items() if k != "image_placeholders"}
        without = self._render(self._rows(None), layout=bare)
        photo = next(f for f in self.layout["fields"] if f["field_type"] == "esports_image")
        group = self.layout["column_groups"][0]
        cell = _pct_box(photo["x_pct"] - 4, group["row_start_pct"] - 2.5,
                        photo["x_pct"] + 4, group["row_start_pct"] + 2.5)
        self.assertGreater(_changed_pixels(with_placeholder, without, cell), 500)

    def test_a_real_photo_wins_over_the_placeholder(self):
        # The placeholder is a fallback, never an overlay: a player WITH a photo must render that
        # photo, so the two renders have to differ in the portrait cell.
        real = self._render(self._rows(_AFC_LOGO_ASSET))
        placeholder = self._render(self._rows(None))
        photo = next(f for f in self.layout["fields"] if f["field_type"] == "esports_image")
        group = self.layout["column_groups"][0]
        cell = _pct_box(photo["x_pct"] - 4, group["row_start_pct"] - 2.5,
                        photo["x_pct"] + 4, group["row_start_pct"] + 2.5)
        self.assertGreater(_changed_pixels(real, placeholder, cell), 500)

    def test_paginates_a_long_player_list(self):
        # 10 per page, so 23 ranked players need 3 pages and page 2 starts at rank 11.
        eph = build_ephemeral_afc_player_default(23)
        self.assertEqual(eph.page_count, 3)
        starts = [p["field_layout"]["column_groups"][0]["start_rank"] for p in eph.pages_spec]
        self.assertEqual(starts, [1, 11, 21])
