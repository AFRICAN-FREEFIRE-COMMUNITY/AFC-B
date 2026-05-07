"""Idempotency tests — webhook retries and double-clicks must not double-apply."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_wallet.models import (
    FxSnapshot,
    SourceTag,
    Wallet,
    WalletTxn,
    WalletTxnKind,
)
from afc_wallet.services import credit, debit


User = get_user_model()


class IdempotencyTestCase(TestCase):
    def setUp(self):
        FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="test"
        )
        self.alice = User.objects.create_user(
            username="alice",
            email="a@example.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=self.alice)

    def test_credit_replay_returns_existing_txn_no_double_credit(self):
        # First call.
        first = credit(
            user=self.alice,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="paystack:abc123",
        )
        # Replay (webhook retry).
        second = credit(
            user=self.alice,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="paystack:abc123",
        )
        self.assertEqual(first.pk, second.pk)
        # Wallet only credited once.
        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_kobo, 10_000)
        self.assertEqual(WalletTxn.objects.filter(wallet=w).count(), 1)

    def test_debit_replay_returns_existing_chain_no_double_debit(self):
        credit(
            user=self.alice,
            amount_kobo=300,
            kind=WalletTxnKind.DEPOSIT_VOUCHER,
            source_tag=SourceTag.GIFT,
            idempotency_key="seed:g",
        )
        credit(
            user=self.alice,
            amount_kobo=500,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:p",
        )

        first_chain = debit(
            user=self.alice,
            amount_kobo=400,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="dbt:1",
        )
        # Replay.
        second_chain = debit(
            user=self.alice,
            amount_kobo=400,
            kind=WalletTxnKind.WAGER_PLACE,
            idempotency_key="dbt:1",
        )
        self.assertEqual(
            sorted(t.pk for t in first_chain),
            sorted(t.pk for t in second_chain),
        )
        # Wallet should reflect a single 400 debit, not 800.
        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_kobo, 400)

    def test_different_keys_produce_different_txns(self):
        a = credit(
            user=self.alice,
            amount_kobo=100,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="key:a",
        )
        b = credit(
            user=self.alice,
            amount_kobo=100,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="key:b",
        )
        self.assertNotEqual(a.pk, b.pk)
        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_kobo, 200)
