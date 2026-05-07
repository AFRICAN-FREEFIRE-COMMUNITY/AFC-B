"""Property tests for the settlement engine — mirror of fast-check tests
in `frontend/lib/mock-wager/__tests__/settlement-property.test.ts`.

Same 4 invariants:
1. payouts + house = pool (when there's a winner and losers)
2. dust >= 0 and dust <= len(winners)
3. all payouts >= 0
4. resolution is deterministic — same input -> same output
"""

from django.test import SimpleTestCase
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from afc_wager.settlement import (
    SettlementResolution,
    WinningLine,
    compute_settlement,
)


# Hypothesis strategies — bounded to keep tests fast.
WINNER_STRAT = st.builds(
    WinningLine,
    user=st.text(min_size=1, max_size=10),
    stake_kobo=st.integers(min_value=100, max_value=1_000_000_000),
)


class SettlementPropertyTestCase(SimpleTestCase):
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )
    @given(
        winners=st.lists(WINNER_STRAT, min_size=1, max_size=50),
        loser_total_kobo=st.integers(
            min_value=1, max_value=1_000_000_000
        ),
        rake_bps=st.sampled_from([100, 250, 500, 1000]),
    )
    def test_winner_invariants_payouts_plus_house_equals_pool(
        self, winners, loser_total_kobo, rake_bps
    ):
        winner_total = sum(w.stake_kobo for w in winners)
        pool = winner_total + loser_total_kobo
        result = compute_settlement(
            pool_kobo=pool,
            rake_bps=rake_bps,
            winning_lines=winners,
            loser_total_kobo=loser_total_kobo,
        )
        if result.resolution != SettlementResolution.WINNER:
            return  # void cases out of scope for this property
        # Invariant 1: payouts + house = pool.
        total = sum(result.payouts.values()) + result.house_total
        self.assertEqual(total, pool, "payouts + house != pool")
        # Invariant 2: 0 <= dust <= number of winning LINES (matches the
        # frontend fast-check property; floor-division can leave at most
        # one sub-kobo per division step).
        self.assertGreaterEqual(result.dust_kobo, 0)
        self.assertLessEqual(result.dust_kobo, len(winners))
        # Invariant 3: all payouts non-negative.
        for v in result.payouts.values():
            self.assertGreaterEqual(v, 0)

    @settings(max_examples=100, deadline=None)
    @given(
        rake_bps=st.sampled_from([100, 250, 500, 1000]),
        loser_total_kobo=st.integers(
            min_value=0, max_value=1_000_000_000
        ),
    )
    def test_void_no_winner_when_no_winning_lines(
        self, rake_bps, loser_total_kobo
    ):
        result = compute_settlement(
            pool_kobo=loser_total_kobo,
            rake_bps=rake_bps,
            winning_lines=[],
            loser_total_kobo=loser_total_kobo,
        )
        self.assertEqual(
            result.resolution, SettlementResolution.VOID_NO_WINNER
        )
        self.assertEqual(result.rake_kobo, 0)
        self.assertEqual(result.refund_all_kobo, loser_total_kobo)

    @settings(max_examples=100, deadline=None)
    @given(
        winners=st.lists(WINNER_STRAT, min_size=1, max_size=20),
        rake_bps=st.sampled_from([100, 250, 500, 1000]),
    )
    def test_void_solo_wager_when_no_losers(self, winners, rake_bps):
        winner_total = sum(w.stake_kobo for w in winners)
        result = compute_settlement(
            pool_kobo=winner_total,
            rake_bps=rake_bps,
            winning_lines=winners,
            loser_total_kobo=0,
        )
        self.assertEqual(
            result.resolution, SettlementResolution.VOID_SOLO_WAGER
        )
        self.assertEqual(result.rake_kobo, 0)
        self.assertEqual(result.refund_all_kobo, winner_total)

    @settings(max_examples=50, deadline=None)
    @given(
        winners=st.lists(WINNER_STRAT, min_size=1, max_size=20),
        loser_total_kobo=st.integers(
            min_value=1, max_value=100_000_000
        ),
        rake_bps=st.sampled_from([100, 250, 500, 1000]),
    )
    def test_determinism_same_input_same_output(
        self, winners, loser_total_kobo, rake_bps
    ):
        winner_total = sum(w.stake_kobo for w in winners)
        pool = winner_total + loser_total_kobo
        a = compute_settlement(
            pool_kobo=pool,
            rake_bps=rake_bps,
            winning_lines=winners,
            loser_total_kobo=loser_total_kobo,
        )
        b = compute_settlement(
            pool_kobo=pool,
            rake_bps=rake_bps,
            winning_lines=winners,
            loser_total_kobo=loser_total_kobo,
        )
        self.assertEqual(a, b)
