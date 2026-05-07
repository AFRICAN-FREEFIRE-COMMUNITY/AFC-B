"""Parimutuel settlement engine.

Mirrors `frontend/lib/mock-wager/settlement-engine.ts` byte-for-byte. Both
implementations are tested against `shared-fixtures/wager-scenarios.json` —
divergence breaks CI.

Two layers:
1. **Pure math:** `compute_settlement()` is deterministic, no DB. Takes the
   pool, rake, winning lines, loser total — returns the resolution + per-user
   payouts + dust. This is what the TS engine implements.
2. **DB integration:** `settle_market()` wraps `compute_settlement()` with
   `transaction.atomic` + `select_for_update` on the Market row, applies the
   payouts via `afc_wallet.services.credit`, and writes the Settlement +
   Payout + RakeTxn rows.

Spec: WEBSITE/docs/superpowers/specs/2026-05-07-wager-feature-design.md
Section 5.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.db import transaction
from django.db.models import Sum

from afc_wallet.models import SourceTag, WalletTxnKind
from afc_wallet.services import credit, get_or_create_house_user

from .models import (
    Market,
    MarketOption,
    MarketStatus,
    Payout,
    RakeTxn,
    Settlement,
    SettlementResolution,
    Wager,
    WagerLine,
    WagerLineOutcome,
    WagerStatus,
)


# ---------------------------------------------------------------------------
# Pure math layer — mirrors TS settle()
# ---------------------------------------------------------------------------


@dataclass
class WinningLine:
    """One winner's stake — `user` is any hashable identifier (str/int)."""

    user: str
    stake_kobo: int


@dataclass
class SettleResult:
    resolution: str
    rake_kobo: int = 0
    net_pool: int = 0
    payouts: Dict[str, int] = field(default_factory=dict)
    dust_kobo: int = 0
    house_total: int = 0
    refund_all_kobo: Optional[int] = None


def compute_settlement(
    *,
    pool_kobo: int,
    rake_bps: int,
    winning_lines: List[WinningLine],
    loser_total_kobo: int,
) -> SettleResult:
    """Pure parimutuel settlement math. Returns the breakdown.

    Mirror of `frontend/lib/mock-wager/settlement-engine.ts::settle()`. ANY
    behavioral divergence here breaks the shared-fixture parity tests.
    """
    winner_total = sum(l.stake_kobo for l in winning_lines)

    # No winners on the winning option → full refund, no rake.
    if winner_total == 0:
        return SettleResult(
            resolution=SettlementResolution.VOID_NO_WINNER,
            refund_all_kobo=pool_kobo,
        )

    # All stakes on the winning option (no losers) → full refund.
    if loser_total_kobo == 0:
        return SettleResult(
            resolution=SettlementResolution.VOID_SOLO_WAGER,
            refund_all_kobo=pool_kobo,
        )

    rake = (pool_kobo * rake_bps) // 10000  # floor
    net_pool = pool_kobo - rake

    # Aggregate by user (multiple lines from same user collapse).
    payouts: Dict[str, int] = defaultdict(int)
    paid_total = 0
    for line in winning_lines:
        share = (net_pool * line.stake_kobo) // winner_total  # floor
        payouts[line.user] += share
        paid_total += share

    dust = net_pool - paid_total
    house_total = rake + dust

    return SettleResult(
        resolution=SettlementResolution.WINNER,
        rake_kobo=rake,
        net_pool=net_pool,
        payouts=dict(payouts),
        dust_kobo=dust,
        house_total=house_total,
    )


# ---------------------------------------------------------------------------
# DB integration layer
# ---------------------------------------------------------------------------


def settle_market(
    *,
    market_id: int,
    final_option_id: int,
    admin_user,
    override_reason: str = "",
) -> Settlement:
    """Settle a market against `final_option_id`.

    Locks the Market row via select_for_update so concurrent settle attempts
    serialize. The second caller sees status=SETTLED and raises.

    Side effects:
    - Creates Settlement + Payouts + RakeTxn
    - Credits each winner's wallet via afc_wallet.services.credit
    - Credits HOUSE wallet for rake + dust
    - Marks WagerLines WIN/LOSS, Wagers SETTLED
    - Flips Market.status to SETTLED, sets winning_option

    Raises:
        ValueError: if market not in PENDING_SETTLEMENT
    """
    with transaction.atomic():
        market = (
            Market.objects.select_for_update()
            .select_related("template")
            .get(pk=market_id)
        )
        if market.status != MarketStatus.PENDING_SETTLEMENT:
            raise ValueError(
                f"market {market_id} status={market.status}, expected "
                "PENDING_SETTLEMENT"
            )

        final_option = MarketOption.objects.select_for_update().get(
            pk=final_option_id, market_id=market_id
        )

        winning_qs = WagerLine.objects.select_for_update().filter(
            wager__market_id=market_id, option_id=final_option_id
        )
        winning_lines = [
            WinningLine(user=str(l.wager.user_id), stake_kobo=l.stake_kobo)
            for l in winning_qs.select_related("wager")
        ]

        total_pool_kobo = market.total_pool_kobo
        winner_total = sum(l.stake_kobo for l in winning_lines)
        loser_total = total_pool_kobo - winner_total

        result = compute_settlement(
            pool_kobo=total_pool_kobo,
            rake_bps=market.rake_bps,
            winning_lines=winning_lines,
            loser_total_kobo=loser_total,
        )

        all_lines_qs = WagerLine.objects.select_for_update().filter(
            wager__market_id=market_id
        )
        lines_count = all_lines_qs.count()

        settlement = Settlement.objects.create(
            market=market,
            suggested_option=market.suggested_option,
            final_option=final_option,
            resolution=result.resolution,
            override_reason=override_reason,
            total_pool_kobo=total_pool_kobo,
            rake_kobo=result.rake_kobo + result.dust_kobo,
            paid_out_kobo=result.net_pool - result.dust_kobo
            if result.resolution == SettlementResolution.WINNER
            else 0,
            winners_count=len(result.payouts),
            lines_count=lines_count,
            confirmed_by_admin=admin_user,
        )

        if result.resolution == SettlementResolution.WINNER:
            _apply_winner_settlement(
                market=market,
                settlement=settlement,
                final_option=final_option,
                result=result,
                winning_qs=winning_qs,
                all_lines_qs=all_lines_qs,
            )
        elif result.resolution in {
            SettlementResolution.VOID_NO_WINNER,
            SettlementResolution.VOID_SOLO_WAGER,
        }:
            _apply_void_refund(
                market=market,
                settlement=settlement,
                all_lines_qs=all_lines_qs,
                refund_all_kobo=result.refund_all_kobo or 0,
            )

        market.status = (
            MarketStatus.SETTLED
            if result.resolution == SettlementResolution.WINNER
            else MarketStatus.VOIDED
        )
        market.winning_option = (
            final_option
            if result.resolution == SettlementResolution.WINNER
            else None
        )
        market.save(update_fields=["status", "winning_option", "updated_at"])

        # Update Wager statuses.
        Wager.objects.filter(market_id=market_id).update(
            status=(
                WagerStatus.SETTLED
                if result.resolution == SettlementResolution.WINNER
                else WagerStatus.VOIDED
            )
        )

        return settlement


def _apply_winner_settlement(
    *,
    market,
    settlement,
    final_option,
    result: SettleResult,
    winning_qs,
    all_lines_qs,
):
    """Distribute payouts + rake. Called only when resolution=WINNER."""
    house = get_or_create_house_user()

    # Group winning lines by user_id so we can attribute multi-line wins.
    by_user: Dict[int, List[WagerLine]] = defaultdict(list)
    for line in winning_qs.select_related("wager"):
        by_user[line.wager.user_id].append(line)

    for user_id_str, total_payout_kobo in result.payouts.items():
        user_id = int(user_id_str)
        lines = by_user[user_id]
        # Each line's payout is proportional to its stake within the user's
        # winning total. We allocate the user's total payout across their
        # lines, with any sub-dust from this division falling on the last
        # line — keeps Payout sum == user total.
        if not lines:
            continue
        user_total_stake = sum(l.stake_kobo for l in lines)

        # Credit the wallet ONCE per user with the aggregated payout.
        idem = f"settle:{settlement.pk}:user:{user_id}"
        credit_txn = credit(
            user=lines[0].wager.user,
            amount_kobo=total_payout_kobo,
            kind=WalletTxnKind.WAGER_PAYOUT,
            source_tag=SourceTag.WON,
            ref_type="settlement",
            ref_id=str(settlement.pk),
            idempotency_key=idem,
            enforce_gift_cap=False,
        )

        running = 0
        for idx, line in enumerate(lines):
            if idx == len(lines) - 1:
                share = total_payout_kobo - running
            else:
                share = (
                    total_payout_kobo * line.stake_kobo
                ) // user_total_stake
                running += share
            line.payout_kobo = share
            line.outcome = WagerLineOutcome.WIN
            line.save(update_fields=["payout_kobo", "outcome"])
            Payout.objects.create(
                settlement=settlement,
                wager_line=line,
                user_id=user_id,
                amount_kobo=share,
                credit_txn_id=credit_txn.pk,
            )

    # Mark losing lines.
    losing_qs = all_lines_qs.exclude(option_id=final_option.pk)
    losing_qs.update(outcome=WagerLineOutcome.LOSS, payout_kobo=0)

    # Credit HOUSE wallet for rake + dust.
    rake_total = result.rake_kobo + result.dust_kobo
    if rake_total > 0:
        rake_credit = credit(
            user=house,
            amount_kobo=rake_total,
            kind=WalletTxnKind.HOUSE_RAKE,
            source_tag=SourceTag.PURCHASED,
            ref_type="settlement",
            ref_id=str(settlement.pk),
            idempotency_key=f"settle:{settlement.pk}:house",
            enforce_gift_cap=False,
        )
        RakeTxn.objects.create(
            settlement=settlement,
            amount_kobo=rake_total,
            credit_txn_id=rake_credit.pk,
        )


def _apply_void_refund(
    *, market, settlement, all_lines_qs, refund_all_kobo: int
):
    """Refund every line at 100% — no rake, no fee. Used for VOID_NO_WINNER,
    VOID_SOLO_WAGER, and VOID_ADMIN."""
    # Group lines by user_id for one credit per user.
    user_refunds: Dict[int, int] = defaultdict(int)
    for line in all_lines_qs.select_related("wager"):
        user_refunds[line.wager.user_id] += line.stake_kobo

    user_objs = {}
    for line in all_lines_qs.select_related("wager__user"):
        user_objs[line.wager.user_id] = line.wager.user

    for user_id, refund_kobo in user_refunds.items():
        if refund_kobo <= 0:
            continue
        idem = f"settle:{settlement.pk}:refund:user:{user_id}"
        refund_txn = credit(
            user=user_objs[user_id],
            amount_kobo=refund_kobo,
            kind=WalletTxnKind.WAGER_REFUND,
            source_tag=SourceTag.PURCHASED,  # refund returns to original
            ref_type="settlement",
            ref_id=str(settlement.pk),
            idempotency_key=idem,
            enforce_gift_cap=False,
        )

    # Mark all lines VOID.
    all_lines_qs.update(outcome=WagerLineOutcome.VOID, payout_kobo=0)
