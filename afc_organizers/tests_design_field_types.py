"""
afc_organizers/tests_design_field_types.py
================================================================================
Every stat AFC Capture records can be PLACED as an overlay column (owner 2026-09-22).

Why this test exists: on 2026-09-22 the frontend palette started offering knocked, grenades thrown
and grenade kills the same day the capture client learned to count them, but
OrgLeaderboardDesignField.FIELD_CHOICES was never extended. Placing one answered "Unknown field
type" and the column could not be saved, which nothing would have caught: the palette is a
frontend list and the gate is a backend set, and no test held the two together.

So this asserts the two ends agree, from the two sources of truth themselves:
  * the stats the capture upload stores (capture_rich_stats.RICH_PLAYER_FIELDS) are all placeable,
  * the keys the OFFICIAL overlay rows carry are all placeable,
so a new stat cannot be added on one side and forgotten on the other.

Run: python manage.py test afc_organizers.tests_design_field_types
"""
from django.test import TestCase

from afc_organizers.models import OrgLeaderboardDesignField
from afc_organizers.views_leaderboard_design import FIELD_TYPES
from afc_tournament_and_scrims.capture_rich_stats import RICH_PLAYER_FIELDS

# Stored per player, but not a column anybody places: the flag and the provenance are about the ROW,
# not a number to show, and survival_seconds is placed under its row-key name survival_time.
NOT_COLUMNS = {"rich_stats_filled", "rich_stats_source", "survival_seconds"}


class CaptureStatsArePlaceableTests(TestCase):
    def test_every_stored_capture_stat_can_be_placed_as_a_column(self):
        missing = sorted(
            f for f in RICH_PLAYER_FIELDS
            if f not in NOT_COLUMNS and f not in FIELD_TYPES
        )
        self.assertEqual(
            missing, [],
            "AFC Capture stores these per player but no overlay column can be placed for them: %s. "
            "Add them to OrgLeaderboardDesignField.FIELD_CHOICES (and to the frontend palette in "
            "lib/leaderboardDesigns.ts)." % missing,
        )

    def test_survival_time_is_the_column_name_for_survival_seconds(self):
        # The stored column is survival_seconds; the overlay row key (and therefore the placeable
        # column) is survival_time. Both must exist, or the stat is stored and unshowable.
        self.assertIn("survival_seconds", RICH_PLAYER_FIELDS)
        self.assertIn("survival_time", FIELD_TYPES)

    def test_the_choices_list_has_no_duplicates(self):
        keys = [c[0] for c in OrgLeaderboardDesignField.FIELD_CHOICES]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(dupes, [], "duplicated field types: %s" % dupes)

    def test_the_three_2026_09_22_counters_are_placeable(self):
        for key in ("knocked", "grenades_used", "grenade_kills"):
            self.assertIn(key, FIELD_TYPES)
