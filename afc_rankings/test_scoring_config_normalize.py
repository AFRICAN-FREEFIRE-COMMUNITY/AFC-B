"""
Regression tests for afc_rankings.scoring.tables.normalize_config.

WHY THIS FILE EXISTS
    A scoring config is stored as a JSON blob, so it is whatever shape the build that saved it
    emitted. The admin editor (frontend .../scoring-config/page.tsx, ScalarGroup) draws ONE INPUT
    PER KEY IT FINDS in that blob, while validation.py demands a number for every field it knows
    about. A blob missing a key therefore produced a save that was refused by a field with no
    control anywhere on the page - reported by the owner as "Flat scrim allowance must be a
    number" with nothing to edit.

    normalize_config closes that gap on the way OUT (the GET the editor loads) and on the way IN
    (validate + save), by filling any missing scalar from the shipped defaults - which is the
    value tables_from_config was already scoring with - and by folding the pre-v2 top-level
    ``scrim_flat_cap`` spelling into ``scrim.flat_cap``.

    These tests pin the four behaviours that bug depended on. They are pure functions over dicts,
    so no database or fixtures are involved.
"""
from django.test import SimpleTestCase

from afc_rankings.scoring import constants as C
from afc_rankings.scoring.tables import defaults_config, normalize_config
from afc_rankings.scoring.validation import validate_config


def _messages(blob):
    return [e["message"] for e in validate_config(blob)["errors"]]


class NormalizeConfigTests(SimpleTestCase):
    def test_a_missing_scalar_is_filled_and_the_save_stops_being_refused(self):
        """The owner's bug: `scrim` present, `flat_cap` absent -> refused, with no field to fix."""
        blob = defaults_config()
        blob["scrim"] = {k: v for k, v in blob["scrim"].items() if k != "flat_cap"}

        # before: refused, and the editor would draw no input for the field it names
        self.assertIn("Flat scrim allowance must be a number.", _messages(blob))

        fixed = normalize_config(blob)
        self.assertEqual(fixed["scrim"]["flat_cap"], float(C.SCRIM_FLAT_CAP))
        self.assertEqual(_messages(fixed), [])

    def test_the_pre_v2_spelling_is_folded_in_and_the_legacy_key_dropped(self):
        """`scrim_flat_cap` at the top level is what v1 blobs carry. One editable value, not two."""
        blob = defaults_config()
        blob["scrim"].pop("flat_cap")
        blob["scrim_flat_cap"] = 8

        fixed = normalize_config(blob)
        self.assertEqual(fixed["scrim"]["flat_cap"], 8)
        self.assertNotIn("scrim_flat_cap", fixed)
        self.assertEqual(_messages(fixed), [])

    def test_a_stale_legacy_key_never_blocks_a_save_on_its_own(self):
        """tables_from_config reads scrim.flat_cap FIRST, so a junk legacy key is dead weight.

        Validation used to check it unconditionally, which meant a value the engine never looks
        at could refuse every save with an error naming a field the editor does not draw.
        """
        blob = defaults_config()
        blob["scrim"]["flat_cap"] = 30
        blob["scrim_flat_cap"] = None
        self.assertEqual(_messages(blob), [])

    def test_lists_and_unknown_keys_are_left_alone(self):
        """Only scalars are filled. A missing list is a structural problem to REPORT, not invent,
        and a key this build does not recognise must survive a round trip (the editor's own
        contract: never drop what you do not understand)."""
        blob = defaults_config()
        blob["something_a_later_build_added"] = {"keep": "me"}
        tiers_before = list(blob["tiers"])

        fixed = normalize_config(blob)
        self.assertEqual(fixed["tiers"], tiers_before)
        self.assertEqual(fixed["something_a_later_build_added"], {"keep": "me"})

    def test_a_null_value_is_treated_as_absent(self):
        """A null reaches the engine as the default anyway, so showing the default is honest."""
        blob = defaults_config()
        blob["player_weights"]["mvp_pts"] = None

        fixed = normalize_config(blob)
        self.assertEqual(fixed["player_weights"]["mvp_pts"], float(C.PLAYER_MVP_PTS))
        self.assertEqual(_messages(fixed), [])
