"""
afc_rankings.test_scrim_tier_rules - tournaments and scrims keep separate tier rules.

Owner, 2026-08-16: "there should be a place we control rules for scrims like we do for tournaments".
A scrim and a tournament are not the same competition, so one list of rules could only ever be right
for one of them. Each now has its own ordered rule set and its own fall-through tier, and the two
never see each other.

WHAT THESE TESTS ARE ACTUALLY GUARDING
    Splitting a shared list in two is the kind of change that looks harmless and quietly re-tiers
    live events, so the tests here are mostly about what must NOT move:

    * NothingChangedTests   - a rule written before the split still means tournaments, and a caller
                              that never mentions a competition still gets tournaments. If this
                              breaks, every existing API client and every saved rule changes meaning
                              at once.
    * IsolationTests        - a scrims rule cannot classify a tournament, and vice versa. This is
                              the whole point of the feature: making scrims easier to tier must not
                              be able to touch a tournament.
    * SeedTests             - the scrims set starts empty, and an empty set classifies nothing. The
                              seed is what stops 13 live scrims dropping to the fall-through tier on
                              deploy day. It must also refuse to run twice, or a second deploy would
                              double every rule.

HOW IT CONNECTS
    afc_rankings.admin_tournament_tiers (_rules_for / _get_config / copy_rule_set / classify) and
    afc_tournament_and_scrims.views.auto_classify_event, which is the production caller that picks a
    set from the event's own competition_type.
"""
from django.test import TestCase

from afc_rankings.admin_tournament_tiers import (
    COMPETITIONS,
    DEFAULT_COMPETITION,
    classify,
    copy_rule_set,
    _get_config,
    _rules_for,
)
from afc_rankings.models import EventTierRule, EventTierConfig


def _rule(tier, prize, competition_type="tournament", priority=0):
    """A live rule that fires on a prize pool at or above ``prize`` naira."""
    return EventTierRule.objects.create(
        competition_type=competition_type,
        priority=priority,
        match="all",
        tier=tier,
        enabled=True,
        conditions=[{"field": "prize", "op": "gte", "value": prize, "currency": "NGN"}],
    )


class NothingChangedTests(TestCase):
    """The split must be invisible to everything written before it."""

    def test_a_rule_created_without_saying_which_set_is_a_tournament_rule(self):
        """Every rule in the database predates the split and meant tournaments. The model default
        is what makes that true after the migration rather than requiring a backfill."""
        rule = EventTierRule.objects.create(
            priority=0, match="all", tier=1, enabled=True,
            conditions=[{"field": "prize", "op": "gte", "value": 1_000_000}],
        )
        self.assertEqual(rule.competition_type, DEFAULT_COMPETITION)
        self.assertEqual(rule.competition_type, "tournament")

    def test_the_default_scope_is_tournaments(self):
        self.assertEqual(DEFAULT_COMPETITION, "tournament")
        self.assertIn("scrims", COMPETITIONS)

    def test_a_config_row_exists_per_competition_not_one_shared_row(self):
        """A shared fall-through would mean changing the scrims default silently changed the
        tournament default, which is the opposite of the point."""
        tournaments = _get_config("tournament")
        scrims = _get_config("scrims")
        self.assertNotEqual(tournaments.pk, scrims.pk)
        self.assertEqual(EventTierConfig.objects.filter(competition_type="scrims").count(), 1)

    def test_fetching_a_config_twice_does_not_create_a_second_row(self):
        _get_config("scrims")
        _get_config("scrims")
        self.assertEqual(EventTierConfig.objects.filter(competition_type="scrims").count(), 1)


class IsolationTests(TestCase):
    """Neither set can reach into the other."""

    def test_a_scrims_rule_is_not_in_the_tournament_set(self):
        _rule(tier=1, prize=1_000, competition_type="scrims")
        self.assertEqual(_rules_for("tournament").count(), 0)
        self.assertEqual(_rules_for("scrims").count(), 1)

    def test_a_generous_scrims_rule_cannot_promote_a_tournament(self):
        """The case that would hurt: scrims get a low bar for Tier 1, and every small tournament
        silently becomes Tier 1 too."""
        _rule(tier=1, prize=1_000, competition_type="scrims")
        _rule(tier=2, prize=1_000_000, competition_type="tournament")

        sample = {"prize": 50_000}
        as_tournament = classify(list(_rules_for("tournament")), 3, sample)
        as_scrim = classify(list(_rules_for("scrims")), 3, sample)

        self.assertEqual(as_tournament["tier"], 3, "a 50,000 naira tournament must still fall through")
        self.assertEqual(as_scrim["tier"], 1, "the same pool is Tier 1 under the scrims rules")

    def test_each_set_falls_through_to_its_own_default_tier(self):
        scrims_config = _get_config("scrims")
        scrims_config.default_tier = 2
        scrims_config.save(update_fields=["default_tier"])

        # No rules anywhere, so both fall through - to different numbers.
        self.assertEqual(classify([], _get_config("tournament").default_tier, {"prize": 0})["tier"], 3)
        self.assertEqual(classify([], _get_config("scrims").default_tier, {"prize": 0})["tier"], 2)

    def test_retiring_a_rule_in_one_set_leaves_the_other_untouched(self):
        tournament_rule = _rule(tier=1, prize=1_000_000, competition_type="tournament")
        copy_rule_set("tournament", "scrims")

        tournament_rule.retired_at = tournament_rule.created_at
        tournament_rule.save(update_fields=["retired_at"])

        self.assertEqual(_rules_for("tournament").count(), 0)
        self.assertEqual(_rules_for("scrims").count(), 1, "the copy is its own row, not a pointer")


class SeedTests(TestCase):
    """The seed is what stops deploy day re-tiering every live scrim."""

    def setUp(self):
        _rule(tier=1, prize=1_000_000, priority=0)
        _rule(tier=2, prize=100_000, priority=1)
        config = _get_config("tournament")
        config.default_tier = 2
        config.save(update_fields=["default_tier"])

    def test_an_unseeded_scrims_set_classifies_nothing(self):
        """Stated so the reason the seed exists is written down as a fact, not an opinion."""
        self.assertEqual(_rules_for("scrims").count(), 0)
        result = classify(list(_rules_for("scrims")), _get_config("scrims").default_tier,
                          {"prize": 5_000_000})
        self.assertIsNone(result["matched_rule_id"])

    def test_seeding_reproduces_the_tournament_answer_exactly(self):
        copy_rule_set("tournament", "scrims")
        for prize in (50_000, 100_000, 999_999, 1_000_000, 5_000_000):
            sample = {"prize": prize}
            self.assertEqual(
                classify(list(_rules_for("scrims")), _get_config("scrims").default_tier, sample)["tier"],
                classify(list(_rules_for("tournament")), _get_config("tournament").default_tier, sample)["tier"],
                f"a {prize} naira scrim must tier the same as it did before the split",
            )

    def test_the_fall_through_tier_is_copied_too(self):
        """A copied set with the same rules but a different fall-through is not the same set."""
        copy_rule_set("tournament", "scrims")
        self.assertEqual(_get_config("scrims").default_tier, 2)

    def test_running_it_twice_copies_nothing_the_second_time(self):
        self.assertEqual(copy_rule_set("tournament", "scrims"), 2)
        self.assertEqual(copy_rule_set("tournament", "scrims"), 0)
        self.assertEqual(_rules_for("scrims").count(), 2)

    def test_it_will_not_overwrite_rules_somebody_wrote(self):
        _rule(tier=3, prize=1, competition_type="scrims")
        self.assertEqual(copy_rule_set("tournament", "scrims"), 0)
        self.assertEqual(_rules_for("scrims").count(), 1, "the admin's own rule survives")

    def test_a_retired_rule_is_not_carried_across(self):
        """A retired rule exists to explain the past of the set it was retired from. Copying it
        into a set that never used it would be inventing history."""
        retired = _rule(tier=1, prize=7_000_000, priority=2)
        retired.retired_at = retired.created_at
        retired.save(update_fields=["retired_at"])
        self.assertEqual(copy_rule_set("tournament", "scrims"), 2)


class ProductionClassifierTests(TestCase):
    """auto_classify_event picks the set from the EVENT, not from whoever is calling."""

    def test_an_event_is_classified_by_its_own_competition_type(self):
        from afc_tournament_and_scrims.views import auto_classify_event

        _rule(tier=1, prize=1_000, competition_type="scrims")

        class _Event:
            """Only the fields auto_classify_event reads. A real Event needs a creator, an
            organization and half a dozen dates, none of which this behaviour depends on."""
            participant_type = "squad"
            max_teams_or_players = 16
            event_mode = "virtual"
            prizepool = "50000"
            prize_currency = "NGN"
            prizepool_cash_value = 50_000
            prizepool_ngn_value = 50_000
            tournament_tier = "tier_3"
            event_id = None

            def __init__(self, competition_type):
                self.competition_type = competition_type

        self.assertEqual(auto_classify_event(_Event("scrims")), "tier_1")
        self.assertEqual(auto_classify_event(_Event("tournament")), "tier_3")
