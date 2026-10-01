"""
afc_rankings.test_tier_codes - a scoring config can rename the four tiers, never renumber them.

WHY (inbox #108, 2026-10-01): on 2026-09-14 the active ScoringConfig was saved with its tier
cutoffs numbered 1..4 (the numbers people read, "Tier 1".."Tier 4") plus a label for 4. The
validator only checked that each tier had a label, so it passed. Every team recalculated after
that landed one code low, and code 4 exists nowhere else: the admin rankings page, the admin
overrides page and the public Tiers tab all crashed on it ("Something went wrong").

The fixed set is ``scoring.constants.TIER_CODES`` (0 = Tier 1 ... 3 = Tier 4). These tests pin:
  * the exact blob that broke production is refused, on every field that carried a bad code;
  * renaming a tier is still allowed, and the shipped defaults still pass;
  * the models, the serializer labels and the constants agree on the four codes, so the next
    place that grows a fifth tier fails here instead of on a page.
"""

import copy

from django.test import SimpleTestCase

from afc_rankings import serializers
from afc_rankings.models import PlayerQuarterlyScore, TeamQuarterlyScore
from afc_rankings.scoring.constants import TIER_CODES
from afc_rankings.scoring.tables import defaults_config
from afc_rankings.scoring.validation import validate_config

# The tier block of ScoringConfig pk 2 exactly as production stored it on 2026-09-14.
PRODUCTION_2026_09_14 = {
    "labels": {"0": "Elite", "1": "Competitive", "2": "Rising", "3": "Entry", "4": "Beginner"},
    "brackets": [
        {"min": 150, "tier": 1},
        {"min": 90, "tier": 2},
        {"min": 40, "tier": 3},
        {"min": 10, "tier": 4, "count": None},
    ],
    "default_tier": 4,
}


def _with_tiers(block):
    config = copy.deepcopy(defaults_config())
    config["tier_thresholds"] = {**config["tier_thresholds"], **copy.deepcopy(block)}
    return config


def _errors(config):
    return validate_config(config)["errors"]


class TierCodesAreFixed(SimpleTestCase):
    def test_the_config_that_broke_production_is_refused(self):
        paths = {e["path"] for e in _errors(_with_tiers(PRODUCTION_2026_09_14))
                 if e["code"] == "unknown_tier"}
        self.assertIn("tier_thresholds.labels.4", paths)
        self.assertIn("tier_thresholds.default_tier", paths)
        self.assertIn("tier_thresholds.brackets[3].tier", paths)

    def test_a_bracket_code_above_three_is_refused_even_without_a_label(self):
        block = {"brackets": [{"min": 150, "tier": 0}, {"min": 40, "tier": 5}], "default_tier": 3}
        paths = {e["path"] for e in _errors(_with_tiers(block))}
        self.assertIn("tier_thresholds.brackets[1].tier", paths)

    def test_a_negative_default_is_refused(self):
        paths = {e["path"] for e in _errors(_with_tiers({"default_tier": -1}))}
        self.assertIn("tier_thresholds.default_tier", paths)

    def test_renaming_a_tier_is_allowed(self):
        block = {"labels": {"0": "Elite", "1": "Competitive", "2": "Rising", "3": "Beginner"}}
        self.assertEqual(_errors(_with_tiers(block)), [])

    def test_the_corrected_production_config_passes(self):
        """The repair the owner is offered: the same cutoffs, numbered 0..3."""
        block = {
            "labels": {"0": "Elite", "1": "Competitive", "2": "Rising", "3": "Entry"},
            "brackets": [{"min": 150, "tier": 0}, {"min": 90, "tier": 1}, {"min": 40, "tier": 2}],
            "default_tier": 3,
        }
        self.assertEqual(_errors(_with_tiers(block)), [])

    def test_the_shipped_defaults_still_pass(self):
        self.assertEqual(_errors(defaults_config()), [])


class EveryLayerAgreesOnTheFourCodes(SimpleTestCase):
    def test_models_serializer_and_constants_name_the_same_codes(self):
        codes = set(TIER_CODES)
        self.assertEqual({c for c, _ in TeamQuarterlyScore.TIER_CHOICES}, codes)
        self.assertEqual({c for c, _ in PlayerQuarterlyScore.TIER_CHOICES}, codes)
        self.assertEqual(set(serializers.TIER_LABELS), codes)


class TheEditorCanSaveTheFix(SimpleTestCase):
    """The editor has no control for a label whose tier no row uses, so the stray "4" label
    must not block the admin who is correcting the codes: normalize_config drops it."""

    def test_normalize_drops_a_label_for_a_code_that_does_not_exist(self):
        from afc_rankings.scoring.tables import normalize_config
        fixed = normalize_config(_with_tiers(PRODUCTION_2026_09_14))
        self.assertEqual(set(fixed["tier_thresholds"]["labels"]), {"0", "1", "2", "3"})

    def test_the_production_rows_renumbered_by_the_editor_then_save_cleanly(self):
        from afc_rankings.scoring.tables import normalize_config
        block = copy.deepcopy(PRODUCTION_2026_09_14)
        block["brackets"] = [{"min": 150, "tier": 0}, {"min": 90, "tier": 1}, {"min": 40, "tier": 2}]
        block["default_tier"] = 3
        self.assertEqual(_errors(normalize_config(_with_tiers(block))), [])
