"""The arithmetic, with the four invariants held over a sweep of pools (no DB)."""
import random

from django.test import SimpleTestCase

from afc_wager import engine
from afc_wager.engine import WinningLine, compute_settlement, projected_payout_kobo


class EngineTests(SimpleTestCase):

    def test_two_sided_pool_pays_the_winners_the_net_pool(self):
        r = compute_settlement(pool_kobo=1_000_000, rake_bps=500,
                               winning_lines=[WinningLine("a", 500_000)], loser_total_kobo=500_000)
        self.assertEqual(r.resolution, engine.RESOLUTION_WINNER)
        self.assertEqual(r.rake_kobo, 50_000)
        self.assertEqual(r.net_pool_kobo, 950_000)
        self.assertEqual(r.payouts, {"a": 950_000})
        self.assertEqual(r.dust_kobo, 0)

    def test_shares_are_floored_and_the_dust_goes_to_the_house(self):
        r = compute_settlement(pool_kobo=1_000, rake_bps=0,
                               winning_lines=[WinningLine("a", 1), WinningLine("b", 1), WinningLine("c", 1)],
                               loser_total_kobo=997)
        self.assertEqual(sum(r.payouts.values()) + r.rake_kobo + r.dust_kobo, 1_000)
        self.assertLess(r.dust_kobo, 3)

    def test_nobody_on_the_winner_is_a_full_refund(self):
        r = compute_settlement(pool_kobo=700, rake_bps=500, winning_lines=[], loser_total_kobo=700)
        self.assertEqual(r.resolution, engine.RESOLUTION_VOID_NO_WINNER)
        self.assertEqual(r.refund_all_kobo, 700)
        self.assertEqual(r.rake_kobo, 0)

    def test_everybody_on_the_winner_is_a_full_refund(self):
        r = compute_settlement(pool_kobo=700, rake_bps=500,
                               winning_lines=[WinningLine("a", 700)], loser_total_kobo=0)
        self.assertEqual(r.resolution, engine.RESOLUTION_VOID_SOLO_WAGER)
        self.assertEqual(r.refund_all_kobo, 700)

    def test_two_lines_of_one_player_are_paid_once(self):
        r = compute_settlement(pool_kobo=400, rake_bps=0,
                               winning_lines=[WinningLine("a", 100), WinningLine("a", 100)], loser_total_kobo=200)
        self.assertEqual(r.payouts, {"a": 400})

    def test_invariants_over_a_sweep(self):
        rng = random.Random(2026)
        for _ in range(500):
            winners = [WinningLine(f"u{i}", rng.randint(1, 5_000_00)) for i in range(rng.randint(1, 8))]
            losers = rng.randint(0, 5_000_000)
            pool = sum(w.stake_kobo for w in winners) + losers
            rake_bps = rng.choice([0, 100, 500, 1000])
            r = compute_settlement(pool_kobo=pool, rake_bps=rake_bps, winning_lines=winners, loser_total_kobo=losers)
            if r.resolution == engine.RESOLUTION_WINNER:
                self.assertEqual(sum(r.payouts.values()) + r.rake_kobo + r.dust_kobo, pool)
                self.assertTrue(all(v >= 0 for v in r.payouts.values()))
                self.assertLess(r.dust_kobo, len(winners))
            else:
                self.assertEqual(r.refund_all_kobo, pool)

    def test_projection_counts_the_players_own_other_option_stakes(self):
        # 5 on the option, 2 on another (the walk's row 3.3): pool 7, rake 5%, winner total 5.
        self.assertEqual(projected_payout_kobo(pool_kobo=700, rake_bps=500, option_pool_kobo=500,
                                               my_stake_on_option_kobo=500), 665)
        # A solo pool would be refunded, so the projection is the stake back, not a gain.
        self.assertEqual(projected_payout_kobo(pool_kobo=500, rake_bps=500, option_pool_kobo=500,
                                               my_stake_on_option_kobo=500), 500)
        self.assertEqual(projected_payout_kobo(pool_kobo=0, rake_bps=500, option_pool_kobo=0, my_stake_on_option_kobo=0), 0)
