"""
afc_rankings.test_tier_rule_room_conditions - a tier rule can ask how the room was set up.

Owner, 2026-08-16: an event should be able to be tiered on whether weapon skins were allowed,
whether the blue zone was on, and whether ammo was unlimited. A tournament played with weapon skins
on is not the same competition as one played without them, and that was previously a note somebody
read afterwards rather than something the classifier could see.

THREE NEW CONDITION FIELDS, all yes/no, sharing the ops is_on / is_off:
    weapon_skins    from the room's `weapon_skins` toggle
    blue_zone       from the room's `blue_zone` toggle
    unlimited_ammo  DERIVED: the room's `ammo_limit` toggle, inverted

THE CASE THAT MATTERS MOST IS "NOBODY RECORDED IT". Most events have no room settings saved at all,
and an unrecorded setting is not the same as a setting that was off. Every one of these conditions
fails closed on a missing value, so a rule about weapon skins does not fire for an event whose room
was never filled in - rather than classifying it on evidence nobody entered. UnrecordedTests pins
that from both directions, because getting it wrong in the is_off direction would silently promote
or demote every event on the platform that has no room row, which is most of them.

HOW IT CONNECTS
    afc_rankings.admin_tournament_tiers._validate_conditions (what the write endpoint accepts) and
    ._eval_condition / .classify (the first-match-wins classifier), plus
    afc_tournament_and_scrims.views._room_flags, which is what reads the saved room and is
    therefore the only place "unlimited ammo means ammo_limit off" is written down.
"""
from django.test import TestCase

from afc_rankings.admin_tournament_tiers import (
    _eval_condition,
    _validate_conditions,
    classify,
)
from afc_rankings.models import EventTierRule


def _rule(conditions, tier, match="all", priority=0):
    """A saved rule. classify() walks EventTierRule OBJECTS (attribute access), not the plain
    dicts the contradiction validator takes - the two halves of this system read rules
    differently, which is worth knowing before writing a test against either."""
    return EventTierRule.objects.create(
        priority=priority, match=match, tier=tier, enabled=True, conditions=conditions,
    )


class ValidationTests(TestCase):
    def test_the_three_fields_are_accepted_with_on_and_off(self):
        clean, error = _validate_conditions([
            {"field": "weapon_skins", "op": "is_off"},
            {"field": "blue_zone", "op": "is_on"},
            {"field": "unlimited_ammo", "op": "is_on"},
        ])
        self.assertIsNone(error)
        # Stored in the same three-key shape a format condition uses, value nulled: there is no
        # number to keep, and leaving whatever the client sent would put junk in the blob.
        self.assertEqual([c["value"] for c in clean], [None, None, None])

    def test_a_numeric_operator_on_a_yes_no_field_is_refused(self):
        """`weapon_skins >= 5` is meaningless, and saving it would produce a rule that silently
        never matches."""
        _, error = _validate_conditions([{"field": "weapon_skins", "op": "gte", "value": 5}])
        self.assertIn("is_on", error)

    def test_an_unknown_field_still_names_every_field_that_IS_allowed(self):
        _, error = _validate_conditions([{"field": "gun_skins", "op": "is_on"}])
        self.assertIn("weapon_skins", error)


class EvaluationTests(TestCase):
    def test_is_off_matches_only_when_the_setting_was_off(self):
        cond = {"field": "weapon_skins", "op": "is_off"}
        self.assertTrue(_eval_condition(cond, {"weapon_skins": False}))
        self.assertFalse(_eval_condition(cond, {"weapon_skins": True}))

    def test_is_on_matches_only_when_the_setting_was_on(self):
        cond = {"field": "unlimited_ammo", "op": "is_on"}
        self.assertTrue(_eval_condition(cond, {"unlimited_ammo": True}))
        self.assertFalse(_eval_condition(cond, {"unlimited_ammo": False}))


class UnrecordedTests(TestCase):
    """An event whose room was never filled in must not be classified on it."""

    def test_a_missing_value_matches_NEITHER_on_nor_off(self):
        for op in ("is_on", "is_off"):
            self.assertFalse(
                _eval_condition({"field": "blue_zone", "op": op}, {"blue_zone": None}),
                f"blue_zone {op} should not fire when nothing was recorded")

    def test_an_absent_key_behaves_the_same_as_an_explicit_none(self):
        """A sample built before these fields existed has no key at all."""
        self.assertFalse(_eval_condition({"field": "blue_zone", "op": "is_off"}, {}))

    def test_an_event_with_no_room_falls_through_to_the_default_tier(self):
        rules = [_rule([{"field": "weapon_skins", "op": "is_off"}], tier=1)]
        result = classify(rules, default_tier=3, sample={"prize": 5_000_000, "weapon_skins": None})
        # Rich event, but the rule asks a question the data cannot answer, so it does not fire.
        self.assertEqual(result["tier"], 3)


class CombinedTests(TestCase):
    """The point of the feature: money AND how it was played, together."""

    RULE = [{"field": "prize", "op": "gte", "value": 1_000_000},
            {"field": "weapon_skins", "op": "is_off"}]

    def test_match_all_needs_both(self):
        rules = [_rule(self.RULE, tier=1, match="all")]
        both = classify(rules, 3, {"prize": 2_000_000, "weapon_skins": False})
        self.assertEqual(both["tier"], 1)
        # Same money, skins allowed: a different competition, so a different tier.
        skins_on = classify(rules, 3, {"prize": 2_000_000, "weapon_skins": True})
        self.assertEqual(skins_on["tier"], 3)

    def test_a_more_specific_rule_ABOVE_a_looser_one_is_what_makes_the_pair_useful(self):
        """The owner's case: 1,000,000 with skins off is Tier 1, 1,000,000 on its own is Tier 2.
        Order is what does it - first match wins, so the specific rule has to be checked first."""
        rules = [
            _rule(self.RULE, tier=1, match="all", priority=0),
            _rule([{"field": "prize", "op": "gte", "value": 1_000_000}], tier=2, priority=1),
        ]
        self.assertEqual(classify(rules, 3, {"prize": 1_500_000, "weapon_skins": False})["tier"], 1)
        self.assertEqual(classify(rules, 3, {"prize": 1_500_000, "weapon_skins": True})["tier"], 2)
