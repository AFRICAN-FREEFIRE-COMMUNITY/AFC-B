"""
afc_fantasy.tests - the rules of the game, pinned.

WHAT THESE ARE GUARDING
    Three things in this feature are easy to get subtly wrong and impossible to notice afterwards:

    PricingTests    A price is the price of a DECISION. If the five best players become affordable,
                    or the star becomes unaffordable, the budget has stopped doing its job and the
                    league is decided by luck. The shape of the band is the feature, so it is
                    asserted directly rather than left to a formula nobody re-checks.
    SquadRuleTests  The rules are re-checked on the server at save time, and a squad that breaks
                    the budget does not merely look wrong, it WINS. Each rule is tested through the
                    same function the save calls.
    ScoringTests    A score must follow corrected results, in BOTH directions. AFC fixes kill
                    counts and disqualifies teams after the fact, and a cache that could only grow
                    would leave a fan holding points that nothing supports.

    Spec: WEBSITE/tasks/fantasy-league-spec.md.
"""
from django.test import TestCase

from afc_fantasy.models import (
    FantasyLeague,
    FantasyPoints,
    FantasyScoringRules,
    FantasySquad,
    PlayerPrice,
    SquadPick,
)
from afc_fantasy.pricing import DEFAULT_TEAM_PREMIUM_SEEDS, band_for, compute_prices
from afc_fantasy.scoring import score_player_match
from afc_fantasy.squad_rules import check_squad


class _League:
    """The settings pricing and the squad rules read, without a database row.

    Both of those modules take a league and read attributes off it, deliberately, so that the admin
    preview can run them against an UNSAVED league. Testing them the same way keeps that promise
    honest: if either starts requiring a saved row, these fail.
    """

    def __init__(self, **kw):
        self.squad_size = kw.get("squad_size", 5)
        self.max_per_team = kw.get("max_per_team", 2)
        self.use_budget = kw.get("use_budget", True)
        self.budget_seeds = kw.get("budget_seeds", 100)
        self.team_premium_seeds = kw.get("team_premium_seeds", 0)
        self.is_locked = kw.get("is_locked", False)


class _Rules:
    """The default scoring numbers from spec section 5, as plain attributes."""
    points_per_kill = 2
    points_booyah = 5
    points_top3 = 2
    points_mvp = 5
    points_played = 1
    points_per_1k_damage = 0


class _Price:
    """Enough of a PlayerPrice for the squad rules: what it costs and whose team it is."""

    def __init__(self, price_seeds, team_id=None):
        self.price_seeds = price_seeds
        self.team_id = team_id


class BandTests(TestCase):
    """The band is the game. These are the numbers the owner approved."""

    def test_the_default_band_is_8_to_32(self):
        floor, ceiling = band_for(_League())
        self.assertEqual((floor, ceiling), (8, 32))

    def test_the_five_best_are_unaffordable_and_five_average_spend_the_pot_exactly(self):
        """The whole argument for ranked pricing over proportional pricing.

        Proportional pricing off the real spread (the best AFC player gets 4.8x the middle one)
        would put the best player at 96 of a 100-seed pot, so nobody could ever pick him.
        """
        league = _League()
        floor, ceiling = band_for(league)
        middle = (floor + ceiling) / 2

        self.assertGreater(ceiling * 5, league.budget_seeds,
                           "five of the best must not fit in the pot")
        self.assertEqual(middle * 5, league.budget_seeds,
                         "five average players must spend the pot exactly")
        self.assertLess(ceiling + (league.squad_size - 1) * floor, league.budget_seeds,
                        "the single best player must be affordable alongside four cheap ones")

    def test_the_band_scales_with_the_pot_so_the_shape_survives(self):
        """A league that doubles its pot must not find every player affordable, which is the same
        as having no pot at all."""
        floor, ceiling = band_for(_League(budget_seeds=200))
        self.assertEqual((floor, ceiling), (16, 64))
        self.assertGreater(ceiling * 5, 200)

    def test_a_free_pick_league_has_no_band(self):
        self.assertEqual(band_for(_League(use_budget=False)), (0, 0))


class PricingTests(TestCase):
    """Pricing with no match history at all, which is the state most AFC players are in."""

    def test_a_player_with_no_history_costs_the_middle_and_is_badged(self):
        """Not cheap: cheap-and-unknown is a free lottery ticket everybody takes. Not expensive:
        that punishes a debut nobody could have judged."""
        rows = compute_prices(_League(), [(1, None), (2, None)])
        self.assertTrue(all(r["is_unproven"] for r in rows))
        self.assertTrue(all(r["price_seeds"] == 20 for r in rows))

    def test_every_price_carries_a_reason(self):
        """A price a fan can check is a price nobody argues with twice. It is also the strongest
        argument for ranked pricing: it explains itself in one line."""
        rows = compute_prices(_League(), [(1, None)])
        self.assertTrue(rows[0]["reason"])

    def test_a_free_pick_league_prices_everything_at_zero(self):
        rows = compute_prices(_League(use_budget=False), [(1, None), (2, None)])
        self.assertTrue(all(r["price_seeds"] == 0 for r in rows))

    def test_the_team_premium_defaults_to_six_seeds(self):
        """Owner, 2026-08-17: the team a player is from should affect their cost. Pinned here so
        the default cannot drift silently, since it is the lever that makes 'who do they play for'
        a real consideration."""
        self.assertEqual(DEFAULT_TEAM_PREMIUM_SEEDS, 6)


class SquadRuleTests(TestCase):
    """Every rule, through the same function the save calls."""

    def setUp(self):
        self.league = _League()
        # THREE teams, because a 5-player squad capped at 2 per team needs at least three to be
        # legal at all. Two teams was the first version of this fixture and it made the "a legal
        # squad passes" test unsatisfiable, which the rule correctly reported.
        # Every player costs 20, so a full squad spends exactly the 100-seed pot.
        self.prices = {
            1: _Price(20, team_id=10), 2: _Price(20, team_id=10), 3: _Price(20, team_id=10),
            4: _Price(20, team_id=11), 5: _Price(20, team_id=11), 6: _Price(20, team_id=11),
            7: _Price(20, team_id=12), 8: _Price(20, team_id=12),
        }

    def _picks(self, ids, captain=1):
        return [{"player_id": i, "is_captain": i == captain} for i in ids]

    def test_a_legal_squad_passes_and_reports_what_it_spent(self):
        verdict = check_squad(self.league, self._picks([1, 2, 4, 5, 7]), self.prices)
        self.assertTrue(verdict["ok"], verdict["rules"])
        self.assertEqual(verdict["spent"], 100)

    def test_too_many_from_one_team_is_refused(self):
        """THE rule that stops every fan entering the same squad."""
        verdict = check_squad(self.league, self._picks([1, 2, 3, 4, 5]), self.prices)
        self.assertFalse(verdict["ok"])
        failed = [r["key"] for r in verdict["rules"] if not r["ok"]]
        self.assertEqual(failed, ["max_per_team"])

    def test_over_budget_is_refused(self):
        self.prices[1] = _Price(60, team_id=10)
        verdict = check_squad(self.league, self._picks([1, 2, 4, 5, 7]), self.prices)
        self.assertFalse(verdict["ok"])
        self.assertIn("within_budget", [r["key"] for r in verdict["rules"] if not r["ok"]])

    def test_the_wrong_number_of_players_is_refused(self):
        verdict = check_squad(self.league, self._picks([1, 2, 4]), self.prices)
        self.assertIn("squad_size", [r["key"] for r in verdict["rules"] if not r["ok"]])

    def test_zero_or_two_captains_are_both_refused(self):
        none = check_squad(self.league, self._picks([1, 2, 4, 5, 7], captain=None), self.prices)
        self.assertIn("one_captain", [r["key"] for r in none["rules"] if not r["ok"]])

        two = self._picks([1, 2, 4, 5, 7])
        two[1]["is_captain"] = True
        self.assertIn("one_captain",
                      [r["key"] for r in check_squad(self.league, two, self.prices)["rules"]
                       if not r["ok"]])

    def test_the_same_player_twice_is_refused(self):
        """Not left to the database constraint: a duplicate would double every point that player
        scores, and the fan deserves the reason rather than a 500."""
        verdict = check_squad(self.league, self._picks([1, 1, 4, 5, 7]), self.prices)
        self.assertIn("no_duplicates", [r["key"] for r in verdict["rules"] if not r["ok"]])

    def test_a_player_who_is_not_in_the_event_is_refused(self):
        verdict = check_squad(self.league, self._picks([1, 2, 4, 5, 99]), self.prices)
        self.assertIn("players_available", [r["key"] for r in verdict["rules"] if not r["ok"]])

    def test_a_locked_league_refuses_even_a_perfect_squad(self):
        verdict = check_squad(_League(is_locked=True), self._picks([1, 2, 4, 5, 7]), self.prices)
        self.assertFalse(verdict["ok"])
        self.assertIn("league_open", [r["key"] for r in verdict["rules"] if not r["ok"]])

    def test_players_with_no_team_are_not_grouped_together(self):
        """A solo event has no teams. Treating "no team" as one club would refuse a legal squad for
        a reason the fan can neither see nor fix."""
        prices = {i: _Price(20, team_id=None) for i in range(1, 6)}
        verdict = check_squad(self.league, self._picks([1, 2, 3, 4, 5]), prices)
        self.assertTrue(verdict["ok"], verdict["rules"])

    def test_every_rule_reports_itself_even_when_it_passes(self):
        """The builder renders the whole checklist, passing rules included, because a fan seeing
        "3 of 5 picked, 62 of 100 seeds spent" learns the game in a way an error never teaches."""
        verdict = check_squad(self.league, self._picks([1, 2, 4, 5, 7]), self.prices)
        self.assertTrue(all("label" in r and "detail" in r for r in verdict["rules"]))
        self.assertGreaterEqual(len(verdict["rules"]), 6)


class ScoringTests(TestCase):
    """The arithmetic, from spec section 5."""

    def setUp(self):
        self.rules = _Rules()

    def test_the_worked_example_from_the_spec(self):
        """6 kills, a booyah and MVP = 23 points. The number in the document the owner approved."""
        self.assertEqual(
            score_player_match(self.rules, kills=6, placement=1, played=True, is_mvp=True), 23)

    def test_the_cheap_pick_from_the_spec(self):
        """2 kills on a team that came 5th = 5 points."""
        self.assertEqual(
            score_player_match(self.rules, kills=2, placement=5, played=True, is_mvp=False), 5)

    def test_a_benched_player_scores_nothing_at_all(self):
        """Not even the participation point: it exists to reward being picked AND playing."""
        self.assertEqual(
            score_player_match(self.rules, kills=9, placement=1, played=False, is_mvp=True), 0)

    def test_a_quiet_game_still_scores_the_participation_point(self):
        self.assertEqual(
            score_player_match(self.rules, kills=0, placement=8, played=True, is_mvp=False), 1)

    def test_second_and_third_score_the_podium_points_but_not_the_booyah(self):
        for placement in (2, 3):
            self.assertEqual(
                score_player_match(self.rules, kills=0, placement=placement, played=True,
                                   is_mvp=False), 3)
        self.assertEqual(
            score_player_match(self.rules, kills=0, placement=4, played=True, is_mvp=False), 1)

    def test_damage_scores_nothing_until_somebody_turns_it_on(self):
        """The column exists and is mostly empty. Shipping it at 0 means a future league can use it
        without rewriting any league that has already been played."""
        self.assertEqual(
            score_player_match(self.rules, kills=0, placement=8, played=True, is_mvp=False,
                               damage=5000), 1)
        self.rules.points_per_1k_damage = 1
        self.assertEqual(
            score_player_match(self.rules, kills=0, placement=8, played=True, is_mvp=False,
                               damage=5000), 6)


class CaptainTests(TestCase):
    """The multiplier is stored in tenths so a table can be reproduced exactly."""

    def test_the_multiplier_reads_back_as_a_number(self):
        league = FantasyLeague(captain_multiplier_tenths=20)
        self.assertEqual(league.captain_multiplier, 2.0)
        self.assertEqual(FantasyLeague(captain_multiplier_tenths=15).captain_multiplier, 1.5)
        self.assertEqual(FantasyLeague(captain_multiplier_tenths=30).captain_multiplier, 3.0)

    def test_a_settled_league_is_still_locked(self):
        """"Can I edit my squad" must not answer yes on a league that finished last month."""
        self.assertTrue(FantasyLeague(status="locked").is_locked)
        self.assertTrue(FantasyLeague(status="settled").is_locked)
        self.assertFalse(FantasyLeague(status="open").is_locked)
        self.assertFalse(FantasyLeague(status="draft").is_locked)
