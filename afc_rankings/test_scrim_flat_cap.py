"""The scrim cap is the HIGHER of a flat allowance and 30% of tournament points.

Owner change, 2026-08-03. The rule used to be the 30% ratio alone, and 30% of zero is
zero, so a team that only played scrims scored nothing however well it did and was then
deleted by the participation floor. Scrims are meant to count toward rankings.

These tests pin the property that matters: a team can never lose scrim points by
performing better in tournaments, and the two rules meet without a cliff.
"""
import datetime

from django.test import TestCase

from afc_rankings.aggregation import scrim_flat_cap
from afc_rankings.models import ScoringConfig
from afc_rankings.scoring.engine import SCRIM_FLAT_CAP, capped_scrim_points


class ScrimCapTests(TestCase):
    def test_scrim_only_team_earns_up_to_the_flat_allowance(self):
        """The whole point of the change: 30% of zero used to be zero."""
        self.assertEqual(capped_scrim_points(40, 0, flat_cap=30), 30)

    def test_a_scrim_only_team_below_the_allowance_keeps_what_it_earned(self):
        self.assertEqual(capped_scrim_points(12, 0, flat_cap=30), 12)

    def test_the_flat_allowance_wins_while_tournament_points_are_small(self):
        # 30% of 34 is 10.2, so the flat allowance is the higher cap here.
        self.assertEqual(capped_scrim_points(40, 34, flat_cap=30), 30)

    def test_the_ratio_takes_over_once_it_exceeds_the_allowance(self):
        # 30% of 200 is 60, comfortably above the flat 30.
        self.assertEqual(capped_scrim_points(80, 200, flat_cap=30), 60)

    def test_the_cap_never_decreases_as_tournament_points_grow(self):
        """No cliff: competing more must never cost a team scrim points."""
        previous = 0.0
        for tournament_pts in range(0, 400, 25):
            capped = capped_scrim_points(9999, tournament_pts, flat_cap=30)
            self.assertGreaterEqual(capped, previous)
            previous = capped

    def test_raw_scrim_points_still_bound_the_result(self):
        """The cap is a ceiling, not a grant: a team cannot be given points it did not earn."""
        self.assertEqual(capped_scrim_points(4, 200, flat_cap=30), 4)

    def test_flat_cap_defaults_to_the_shipped_constant(self):
        self.assertEqual(capped_scrim_points(9999, 0), float(SCRIM_FLAT_CAP))


class ScrimFlatCapConfigTests(TestCase):
    """The allowance is admin-configurable, because the right value depends on how busy
    the calendar is and the owner expects to revisit it as more events run."""

    def test_falls_back_to_the_constant_with_no_config(self):
        self.assertEqual(scrim_flat_cap(), float(SCRIM_FLAT_CAP))

    def test_reads_an_admin_configured_value(self):
        ScoringConfig.objects.create(
            version=1, is_active=True, config={"scrim_flat_cap": 8}
        )
        self.assertEqual(scrim_flat_cap(), 8.0)

    def test_a_broken_value_does_not_break_scoring(self):
        """A scoring run must never fail over configuration."""
        ScoringConfig.objects.create(
            version=2, is_active=True, config={"scrim_flat_cap": "not a number"}
        )
        self.assertEqual(scrim_flat_cap(), float(SCRIM_FLAT_CAP))

    def test_an_unrelated_config_leaves_the_default_alone(self):
        ScoringConfig.objects.create(
            version=3, is_active=True, config={"something_else": 5}
        )
        self.assertEqual(scrim_flat_cap(), float(SCRIM_FLAT_CAP))
