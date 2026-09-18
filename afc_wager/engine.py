"""
afc_wager.engine - the pari-mutuel settlement arithmetic, pure and deterministic.

Carried over from the May 2026 `feature/wager` branch (afc_wager/settlement.py, pure layer)
UNCHANGED in behaviour: it was tested there against 15 shared scenarios and four invariants, and
the frontend's projected-payout helper mirrors it. Nothing in here touches the database; the DB
integration lives in afc_wager/services.py settle_market.

THE RULES, in the order they apply
    1. Nobody staked on the winning option        -> VOID_NO_WINNER, every stake refunded, no rake.
    2. Everybody staked on the winning option     -> VOID_SOLO_WAGER, every stake refunded, no rake.
    3. Otherwise: rake = floor(pool * rake_bps / 10000); net = pool - rake; each winning line
       gets floor(net * stake / winner_total); lines of one player are summed; whatever the floors
       left over (dust, always smaller than the number of winning lines) goes to the house.

INVARIANTS the tests hold:
    sum(payouts) + rake + dust == pool          on a WINNER resolution
    every payout >= 0                           always
    dust < len(winning_lines)                   always
    refund_all == pool                          on either VOID
"""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

RESOLUTION_WINNER = "WINNER"
RESOLUTION_VOID_NO_WINNER = "VOID_NO_WINNER"
RESOLUTION_VOID_SOLO_WAGER = "VOID_SOLO_WAGER"


@dataclass
class WinningLine:
    """One winning line. `user` is any hashable identifier; lines of one user are summed."""
    user: object
    stake_kobo: int


@dataclass
class SettleResult:
    resolution: str
    rake_kobo: int = 0
    net_pool_kobo: int = 0
    payouts: Dict[object, int] = field(default_factory=dict)
    dust_kobo: int = 0
    house_total_kobo: int = 0
    refund_all_kobo: Optional[int] = None


def compute_settlement(*, pool_kobo: int, rake_bps: int, winning_lines: List[WinningLine],
                       loser_total_kobo: int) -> SettleResult:
    """The arithmetic. Integers in, integers out; never a float."""
    winner_total = sum(line.stake_kobo for line in winning_lines)

    if winner_total == 0:
        return SettleResult(resolution=RESOLUTION_VOID_NO_WINNER, refund_all_kobo=pool_kobo)
    if loser_total_kobo == 0:
        return SettleResult(resolution=RESOLUTION_VOID_SOLO_WAGER, refund_all_kobo=pool_kobo)

    rake = (pool_kobo * rake_bps) // 10000
    net_pool = pool_kobo - rake

    payouts: Dict[object, int] = defaultdict(int)
    paid_total = 0
    for line in winning_lines:
        share = (net_pool * line.stake_kobo) // winner_total
        payouts[line.user] += share
        paid_total += share

    dust = net_pool - paid_total
    return SettleResult(
        resolution=RESOLUTION_WINNER,
        rake_kobo=rake,
        net_pool_kobo=net_pool,
        payouts=dict(payouts),
        dust_kobo=dust,
        house_total_kobo=rake + dust,
    )


def projected_payout_kobo(*, pool_kobo: int, rake_bps: int, option_pool_kobo: int,
                          my_stake_on_option_kobo: int) -> int:
    """What a player would receive if this option won, given the pool as it would stand AFTER
    their stakes are in. The caller passes totals that already include the player's own lines on
    every option (the May frontend forgot its own other-option stakes; see the walk, row 3.3).
    Returns 0 when nothing would be paid (a solo pool refunds instead of paying)."""
    if option_pool_kobo <= 0 or my_stake_on_option_kobo <= 0:
        return 0
    if option_pool_kobo >= pool_kobo:
        # Everybody is on this option: a win would be VOID_SOLO_WAGER, stake back, no gain.
        return my_stake_on_option_kobo
    rake = (pool_kobo * rake_bps) // 10000
    net_pool = pool_kobo - rake
    return (net_pool * my_stake_on_option_kobo) // option_pool_kobo
