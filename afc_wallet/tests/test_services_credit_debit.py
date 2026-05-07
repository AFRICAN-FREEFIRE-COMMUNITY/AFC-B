"""TDD tests for afc_wallet.services.credit() and debit()."""

from decimal import Decimal

import pytest
from django.test import TestCase

from afc_wallet import services
from afc_wallet.models import (
    FxSnapshot,
    SourceTag,
    Wallet,
    WalletStatus,
    WalletTxn,
    WalletTxnKind,
)
from afc_wallet.services import (
    InsufficientFunds,
    WalletFrozen,
    credit,
    debit,
    get_current_fx,
)
from django.contrib.auth import get_user_model


User = get_user_model()


class CreditDebitTestCase(TestCase):
    """Plain Django TestCase — does not need pytest-django wiring."""

    def setUp(self):
        FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="test"
        )
        self.alice = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            password="x",
            full_name="Alice",
            country="NG",
        )
        Wallet.objects.create(user=self.alice)

    def test_credit_purchased_increments_correct_buckets(self):
        txn = credit(
            user=self.alice,
            amount_kobo=50_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            ref_type="paystack",
            ref_id="ref_001",
            idempotency_key="paystack:ref_001",
        )
        self.assertEqual(txn.amount_kobo, 50_000)
        wallet = Wallet.objects.get(user=self.alice)
        self.assertEqual(wallet.balance_kobo, 50_000)
        self.assertEqual(wallet.balance_purchased_kobo, 50_000)
        self.assertEqual(wallet.balance_won_kobo, 0)
        self.assertEqual(wallet.balance_gift_kobo, 0)

    def test_credit_won_increments_won_bucket(self):
        credit(
            user=self.alice,
            amount_kobo=20_000,
            kind=WalletTxnKind.WAGER_PAYOUT,
            source_tag=SourceTag.WON,
            idempotency_key="payout:001",
        )
        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_won_kobo, 20_000)

    def test_credit_gift_increments_gift_bucket(self):
        credit(
            user=self.alice,
            amount_kobo=5_000,
            kind=WalletTxnKind.DEPOSIT_VOUCHER,
            source_tag=SourceTag.GIFT,
            idempotency_key="vou:001",
        )
        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_gift_kobo, 5_000)

    def test_credit_amount_must_be_positive(self):
        with self.assertRaises(services.WalletError):
            credit(
                user=self.alice,
                amount_kobo=0,
                kind=WalletTxnKind.DEPOSIT_PAYSTACK,
                source_tag=SourceTag.PURCHASED,
                idempotency_key="zero",
            )
        with self.assertRaises(services.WalletError):
            credit(
                user=self.alice,
                amount_kobo=-10,
                kind=WalletTxnKind.DEPOSIT_PAYSTACK,
                source_tag=SourceTag.PURCHASED,
                idempotency_key="neg",
            )

    def test_credit_to_frozen_wallet_blocked(self):
        Wallet.objects.filter(user=self.alice).update(
            status=WalletStatus.FROZEN
        )
        with self.assertRaises(WalletFrozen):
            credit(
                user=self.alice,
                amount_kobo=1000,
                kind=WalletTxnKind.DEPOSIT_PAYSTACK,
                source_tag=SourceTag.PURCHASED,
                idempotency_key="frozen:1",
            )

    def test_debit_consumes_gift_first_then_won_then_purchased(self):
        # Seed: 100 GIFT + 200 WON + 500 PURCHASED.
        credit(
            user=self.alice,
            amount_kobo=100,
            kind=WalletTxnKind.DEPOSIT_VOUCHER,
            source_tag=SourceTag.GIFT,
            idempotency_key="seed:gift",
        )
        credit(
            user=self.alice,
            amount_kobo=200,
            kind=WalletTxnKind.WAGER_PAYOUT,
            source_tag=SourceTag.WON,
            idempotency_key="seed:won",
        )
        credit(
            user=self.alice,
            amount_kobo=500,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:pur",
        )

        # Spend 250: should consume all 100 GIFT + 150 WON.
        out = debit(
            user=self.alice,
            amount_kobo=250,
            kind=WalletTxnKind.WAGER_PLACE,
            ref_type="wager",
            ref_id="w1",
            idempotency_key="dbt:1",
        )
        self.assertEqual(len(out), 2)
        # First txn (GIFT) gets the raw key, second gets ":WON".
        kinds_amounts = [(t.source_tag, t.amount_kobo) for t in out]
        self.assertIn((SourceTag.GIFT, -100), kinds_amounts)
        self.assertIn((SourceTag.WON, -150), kinds_amounts)

        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_gift_kobo, 0)
        self.assertEqual(w.balance_won_kobo, 50)
        self.assertEqual(w.balance_purchased_kobo, 500)
        self.assertEqual(w.balance_kobo, 550)

    def test_debit_insufficient_funds_atomically_fails(self):
        credit(
            user=self.alice,
            amount_kobo=100,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:1",
        )
        with self.assertRaises(InsufficientFunds):
            debit(
                user=self.alice,
                amount_kobo=200,
                kind=WalletTxnKind.WAGER_PLACE,
                idempotency_key="overspend:1",
            )
        # Wallet untouched.
        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_kobo, 100)
        # No debit txns recorded.
        self.assertFalse(
            WalletTxn.objects.filter(
                wallet=w, kind=WalletTxnKind.WAGER_PLACE
            ).exists()
        )

    def test_debit_uses_purchased_only_when_gift_won_empty(self):
        credit(
            user=self.alice,
            amount_kobo=1000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:p",
        )
        out = debit(
            user=self.alice,
            amount_kobo=300,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="dbt:p",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].source_tag, SourceTag.PURCHASED)
        self.assertEqual(out[0].amount_kobo, -300)

    def test_credit_stamps_fx_snapshot(self):
        fx = FxSnapshot.objects.first()
        txn = credit(
            user=self.alice,
            amount_kobo=1000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="fx:1",
        )
        self.assertEqual(txn.fx_snapshot_id, fx.pk)
