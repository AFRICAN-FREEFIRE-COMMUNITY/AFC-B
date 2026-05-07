"""Voucher redemption tests including concurrency."""

from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Thread

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from afc_wallet.models import (
    FxSnapshot,
    SourceTag,
    Voucher,
    VoucherRedemption,
    Wallet,
    WalletTxnKind,
)
from afc_wallet.services import (
    VoucherAlreadyRedeemed,
    VoucherExhausted,
    VoucherExpired,
    VoucherInvalid,
    redeem_voucher,
)


User = get_user_model()


class VoucherTestCase(TestCase):
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

    def test_redeem_credits_gift_bucket(self):
        v = Voucher.objects.create(code="WELCOME500", amount_kobo=50_000)
        voucher, txn = redeem_voucher(user=self.alice, code="welcome500")
        self.assertEqual(voucher.code, "WELCOME500")
        self.assertEqual(voucher.used_count, 1)
        self.assertEqual(txn.amount_kobo, 50_000)
        self.assertEqual(txn.source_tag, SourceTag.GIFT)
        w = Wallet.objects.get(user=self.alice)
        self.assertEqual(w.balance_gift_kobo, 50_000)

    def test_redeem_normalizes_code_uppercase_trim(self):
        Voucher.objects.create(code="HYPE1K", amount_kobo=100_000)
        redeem_voucher(user=self.alice, code="  hype1k  ")
        v = Voucher.objects.get(code="HYPE1K")
        self.assertEqual(v.used_count, 1)

    def test_redeem_unknown_code_raises(self):
        with self.assertRaises(VoucherInvalid):
            redeem_voucher(user=self.alice, code="DOESNOTEXIST")

    def test_redeem_empty_code_raises(self):
        with self.assertRaises(VoucherInvalid):
            redeem_voucher(user=self.alice, code="")

    def test_redeem_expired_raises(self):
        Voucher.objects.create(
            code="EXPIRED",
            amount_kobo=100_000,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        with self.assertRaises(VoucherExpired):
            redeem_voucher(user=self.alice, code="EXPIRED")

    def test_redeem_exhausted_raises(self):
        v = Voucher.objects.create(
            code="ONESHOT", amount_kobo=10_000, max_uses=1, used_count=1
        )
        with self.assertRaises(VoucherExhausted):
            redeem_voucher(user=self.alice, code="ONESHOT")

    def test_redeem_twice_by_same_user_raises(self):
        Voucher.objects.create(
            code="MULTI", amount_kobo=10_000, max_uses=10
        )
        redeem_voucher(user=self.alice, code="MULTI")
        with self.assertRaises(VoucherAlreadyRedeemed):
            redeem_voucher(user=self.alice, code="MULTI")

    def test_redeem_by_different_users_succeeds(self):
        v = Voucher.objects.create(
            code="SHARED", amount_kobo=5_000, max_uses=5
        )
        bob = User.objects.create_user(
            username="bob",
            email="b@example.com",
            password="x",
            full_name="B",
            country="NG",
        )
        Wallet.objects.create(user=bob)
        redeem_voucher(user=self.alice, code="SHARED")
        redeem_voucher(user=bob, code="SHARED")
        v.refresh_from_db()
        self.assertEqual(v.used_count, 2)
        self.assertEqual(VoucherRedemption.objects.count(), 2)


class VoucherConcurrencyTestCase(TransactionTestCase):
    """TransactionTestCase so threads can see each other's commits.

    Note: sqlite serializes writes anyway, so this primarily exercises the
    `unique(voucher, user)` constraint and our select_for_update guard. On
    MySQL/Postgres the same code path scales to true row-level locking.
    """

    available_apps = [
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "afc_auth",
        "afc_team",
        "afc_tournament_and_scrims",
        "afc_wallet",
    ]

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
        self.voucher = Voucher.objects.create(
            code="ONESHOT", amount_kobo=10_000, max_uses=1
        )

    def test_two_threads_redeeming_same_single_use_voucher(self):
        """Two parallel calls — exactly one wins, the other raises.

        On sqlite, the BEGIN IMMEDIATE / lock semantics mean one thread will
        wait for the other to commit, then see used_count == max_uses and
        raise VoucherExhausted.
        """
        results = []
        errors = []
        barrier = Barrier(2)

        def worker():
            barrier.wait()
            try:
                voucher, txn = redeem_voucher(
                    user=self.alice, code="ONESHOT"
                )
                results.append(voucher.pk)
            except Exception as e:
                errors.append(type(e).__name__)
            finally:
                close_old_connections()

        # On sqlite with multiple users, this would need separate users to
        # actually exercise the race (since same-user is blocked by
        # uniq_voucher_redemption_per_user). Test the more realistic case:
        bob = User.objects.create_user(
            username="bob",
            email="b@example.com",
            password="x",
            full_name="B",
            country="NG",
        )
        Wallet.objects.create(user=bob)

        results = []
        errors = []
        users = [self.alice, bob]

        def worker2(u):
            barrier.wait()
            try:
                voucher, txn = redeem_voucher(user=u, code="ONESHOT")
                results.append(voucher.pk)
            except Exception as e:
                errors.append(type(e).__name__)
            finally:
                close_old_connections()

        ts = [Thread(target=worker2, args=(u,)) for u in users]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        # Exactly one redemption should succeed.
        self.assertEqual(
            len(results) + len(errors),
            2,
            f"results={results} errors={errors}",
        )
        self.assertEqual(
            len(results), 1, f"expected 1 winner, got {len(results)}"
        )
        self.assertEqual(len(errors), 1)
        self.assertIn(errors[0], {"VoucherExhausted", "OperationalError"})

        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.used_count, 1)
        self.assertEqual(VoucherRedemption.objects.count(), 1)
