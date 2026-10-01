"""
afc_rankings.test_tier_codes - tiers can be ADDED at any time, never renumbered.

WHY (inbox #108, 2026-10-01): on 2026-09-14 the active ScoringConfig was saved with its tier
cutoffs numbered 1..4 (the numbers people read, "Tier 1".."Tier 4") and a default of 4. The
validator only checked that each tier had a name, so it passed. Nobody could reach code 0, every
team recalculated after that landed one code low, and the admin rankings page, the overrides page
and the public Tiers tab crashed on a code they had never drawn ("Something went wrong").

The owner built the tier table so a new tier can be added whenever wanted, so the number of tiers
stays open. What is fixed is the NUMBERING: the top cutoff row is code 0 (Tier 1), each row below
is the next code, and the default is the code just below the last row. These tests pin:
  * the exact blob that broke production is refused, on the rows and on the default;
  * adding a fifth tier the right way is accepted;
  * the shipped defaults still pass, and renaming a tier is still allowed;
  * normalize_config drops a name left behind by a tier no row uses, so the editor (which has
    no control to delete one) can save the correction.
"""

import copy

from django.test import SimpleTestCase

from afc_rankings.scoring.tables import defaults_config, normalize_config
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

# The correction offered to the owner: the same cutoffs, numbered from 0, bottom tier "Entry".
CORRECTED = {
    "labels": {"0": "Elite", "1": "Competitive", "2": "Rising", "3": "Entry"},
    "brackets": [{"min": 150, "tier": 0}, {"min": 90, "tier": 1}, {"min": 40, "tier": 2}],
    "default_tier": 3,
}


def _with_tiers(block):
    config = copy.deepcopy(defaults_config())
    config["tier_thresholds"] = {**config["tier_thresholds"], **copy.deepcopy(block)}
    return config


def _errors(config):
    return validate_config(config)["errors"]


def _paths(config, code):
    return {e["path"] for e in _errors(config) if e["code"] == code}


class TiersAreNumberedFromTheTop(SimpleTestCase):
    def test_the_config_that_broke_production_is_refused(self):
        # Every row is one code too high. (Its default of 4 happens to sit just under its last
        # row, so the rows are where the shift shows.)
        paths = _paths(_with_tiers(PRODUCTION_2026_09_14), "tier_out_of_order")
        self.assertEqual(paths, {f"tier_thresholds.brackets[{i}].tier" for i in range(4)})

    def test_a_gap_in_the_middle_is_refused(self):
        block = copy.deepcopy(CORRECTED)
        block["brackets"][2]["tier"] = 3
        block["labels"]["4"] = "Spare"
        self.assertIn("tier_thresholds.brackets[2].tier",
                      _paths(_with_tiers(block), "tier_out_of_order"))

    def test_a_default_that_is_not_the_next_tier_down_is_refused(self):
        block = copy.deepcopy(CORRECTED)
        block["default_tier"] = 2
        self.assertIn("tier_thresholds.default_tier",
                      _paths(_with_tiers(block), "tier_out_of_order"))

    def test_the_correction_passes(self):
        self.assertEqual(_errors(_with_tiers(CORRECTED)), [])

    def test_the_shipped_defaults_still_pass(self):
        self.assertEqual(_errors(defaults_config()), [])

    def test_renaming_a_tier_is_allowed(self):
        block = copy.deepcopy(CORRECTED)
        block["labels"]["3"] = "Beginner"
        self.assertEqual(_errors(_with_tiers(block)), [])


class ATierCanBeAdded(SimpleTestCase):
    """The owner's design: a new tier can be added any time. Adding one the right way (a new
    row at the bottom, the default moving down one) must keep passing."""

    def test_a_fifth_tier_added_at_the_bottom_passes(self):
        block = copy.deepcopy(CORRECTED)
        block["brackets"].append({"min": 10, "tier": 3})
        block["labels"]["4"] = "Beginner"
        block["default_tier"] = 4
        self.assertEqual(_errors(_with_tiers(block)), [])


class TheEditorCanSaveTheFix(SimpleTestCase):
    def test_normalize_keeps_names_for_every_tier_in_use(self):
        fixed = normalize_config(_with_tiers(PRODUCTION_2026_09_14))
        self.assertEqual(set(fixed["tier_thresholds"]["labels"]), {"0", "1", "2", "3", "4"})

    def test_normalize_drops_a_name_no_tier_uses_once_the_rows_are_renumbered(self):
        block = copy.deepcopy(CORRECTED)
        block["labels"]["4"] = "Beginner"
        fixed = normalize_config(_with_tiers(block))
        self.assertEqual(set(fixed["tier_thresholds"]["labels"]), {"0", "1", "2", "3"})
        self.assertEqual(_errors(fixed), [])
