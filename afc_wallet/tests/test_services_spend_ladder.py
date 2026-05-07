"""Spend-ladder tests — debit consumes GIFT, then WON, then PURCHASED."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_wallet.models import (
    FxSnapshot,
    SourceTag,
    Wallet,
    WalletTxnKind,
)
from afc_wallet.services import credit, debit


User = get_user_model()


class SpendLadderTestCase(TestCase):
    def setUp(self):
        FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="test"
        )
        self.user = User.objects.create_user(
            username="laddertest",
            email="l@example.com",
            password="x",
            full_name="L",
            country="NG",
        )
        Wallet.objects.create(user=self.user)
        # Seed: 100 GIFT + 200 WON + 300 PURCHASED.
        credit(
            user=self.user,
            amount_kobo=100,
            kind=WalletTxnKind.DEPOSIT_VOUCHER,
            source_tag=SourceTag.GIFT,
            idempotency_key="g",
        )
        credit(
            user=self.user,
            amount_kobo=200,
            kind=WalletTxnKind.WAGER_PAYOUT,
            source_tag=SourceTag.WON,
            idempotency_key="w",
        )
        credit(
            user=self.user,
            amount_kobo=300,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="p",
        )

    def _state(self):
        w = Wallet.objects.get(user=self.user)
        return (
            w.balance_gift_kobo,
            w.balance_won_kobo,
            w.balance_purchased_kobo,
            w.balance_kobo,
        )

    def test_spend_50_consumes_only_gift(self):
        debit(
            user=self.user,
            amount_kobo=50,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="d50",
        )
        self.assertEqual(self._state(), (50, 200, 300, 550))

    def test_spend_100_drains_gift_only(self):
        debit(
            user=self.user,
            amount_kobo=100,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="d100",
        )
        self.assertEqual(self._state(), (0, 200, 300, 500))

    def test_spend_150_drains_gift_dips_into_won(self):
        debit(
            user=self.user,
            amount_kobo=150,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="d150",
        )
        self.assertEqual(self._state(), (0, 150, 300, 450))

    def test_spend_300_drains_gift_and_won(self):
        debit(
            user=self.user,
            amount_kobo=300,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="d300",
        )
        self.assertEqual(self._state(), (0, 0, 300, 300))

    def test_spend_500_drains_all_three(self):
        out = debit(
            user=self.user,
            amount_kobo=500,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="d500",
        )
        self.assertEqual(self._state(), (0, 0, 100, 100))
        # 3 txns produced — one per bucket.
        self.assertEqual(len(out), 3)
        tags = {t.source_tag for t in out}
        self.assertEqual(
            tags, {SourceTag.GIFT, SourceTag.WON, SourceTag.PURCHASED}
        )

    def test_spend_more_than_balance_fails_atomically(self):
        from afc_wallet.services import InsufficientFunds

        with self.assertRaises(InsufficientFunds):
            debit(
                user=self.user,
                amount_kobo=999_999,
                kind=WalletTxnKind.WAGER_PLACE,
                idempotency_key="overspend",
            )
        self.assertEqual(self._state(), (100, 200, 300, 600))
