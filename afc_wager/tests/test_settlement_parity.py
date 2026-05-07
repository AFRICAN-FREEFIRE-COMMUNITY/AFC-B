"""Settlement engine parity test — drives off shared-fixtures/wager-scenarios.json.

Both the TS engine (frontend/lib/mock-wager/settlement-engine.ts) and the
Python engine (afc_wager/settlement.py::compute_settlement) must produce the
SAME `expected` payload for every scenario in this file. CI runs both sides;
divergence breaks the build.
"""

import json
from pathlib import Path

from django.test import SimpleTestCase

from afc_wager.settlement import (
    SettlementResolution,
    WinningLine,
    compute_settlement,
)


SHARED_FIXTURES = (
    Path(__file__).resolve().parent.parent.parent
    / "shared-fixtures"
    / "wager-scenarios.json"
)


def _load_scenarios():
    with open(SHARED_FIXTURES, encoding="utf-8") as f:
        return json.load(f)


class SettlementParityTestCase(SimpleTestCase):
    """One assertion-rich test method per scenario, dynamically generated."""

    pass


def _make_test(scenario):
    name = scenario["name"]
    desc = scenario["description"]

    def test(self):
        expected = scenario["expected"]
        winning_lines = [
            WinningLine(user=l["user"], stake_kobo=l["stake_kobo"])
            for l in scenario["winning_lines"]
        ]
        result = compute_settlement(
            pool_kobo=scenario["pool_kobo"],
            rake_bps=scenario["rake_bps"],
            winning_lines=winning_lines,
            loser_total_kobo=scenario["loser_total_kobo"],
        )

        self.assertEqual(
            result.resolution, expected["resolution"], f"{name}: resolution"
        )

        if expected["resolution"] == SettlementResolution.WINNER:
            self.assertEqual(
                result.rake_kobo, expected["rake_kobo"], f"{name}: rake"
            )
            self.assertEqual(
                result.net_pool, expected["net_pool"], f"{name}: net_pool"
            )
            self.assertEqual(
                result.dust_kobo, expected["dust_kobo"], f"{name}: dust"
            )
            self.assertEqual(
                result.house_total,
                expected["house_total"],
                f"{name}: house_total",
            )
            for user, expected_amt in expected["payouts"].items():
                self.assertEqual(
                    result.payouts.get(user),
                    expected_amt,
                    f"{name}: payout for {user}",
                )
            # Invariant: payouts + house = pool
            total = (
                sum(result.payouts.values()) + result.house_total
            )
            self.assertEqual(
                total,
                scenario["pool_kobo"],
                f"{name}: payouts + house != pool",
            )
        else:
            self.assertEqual(result.rake_kobo, 0, f"{name}: rake should be 0")
            self.assertEqual(
                result.refund_all_kobo,
                expected["refund_all_kobo"],
                f"{name}: refund_all_kobo",
            )

    test.__name__ = f"test_{name.replace('-', '_')}"
    test.__doc__ = desc
    return test


# Dynamically attach a test method per scenario.
for sc in _load_scenarios():
    test_method = _make_test(sc)
    setattr(SettlementParityTestCase, test_method.__name__, test_method)
