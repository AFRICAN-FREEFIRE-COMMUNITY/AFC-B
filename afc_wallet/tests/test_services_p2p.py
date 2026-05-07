"""P2P send tests — fees, source-tag inheritance, caps."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_wallet.constants import (
    GIFT_DAILY_CAP_KOBO,
    P2P_DAILY_CAP_KOBO,
    P2P_FEE_BPS,
)
from afc_wallet.models import (
    FxSnapshot,
    SourceTag,
    Wallet,
    WalletTxn,
    WalletTxnKind,
)
from afc_wallet.services import (
    GiftDailyCapExceeded,
    P2PDailyCapExceeded,
    P2PInvalidRecipient,
    credit,
    get_or_create_house_user,
    p2p_send,
)


User = get_user_model()


class P2PTestCase(TestCase):
    def setUp(self):
        FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="test"
        )
        # House wallet (rake/fee target).
        self.house = get_or_create_house_user()
        Wallet.objects.create(user=self.house)

        self.alice = User.objects.create_user(
            username="alice",
            email="a@example.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=self.alice)

        self.bob = User.objects.create_user(
            username="bob",
            email="b@example.com",
            password="x",
            full_name="B",
            country="NG",
        )
        Wallet.objects.create(user=self.bob)

    def _seed_alice(self, gift=0, won=0, purchased=0):
        if gift:
            credit(
                user=self.alice,
                amount_kobo=gift,
                kind=WalletTxnKind.DEPOSIT_VOUCHER,
                source_tag=SourceTag.GIFT,
                idempotency_key=f"seed:gift:{gift}",
            )
        if won:
            credit(
                user=self.alice,
                amount_kobo=won,
                kind=WalletTxnKind.WAGER_PAYOUT,
                source_tag=SourceTag.WON,
                idempotency_key=f"seed:won:{won}",
            )
        if purchased:
            credit(
                user=self.alice,
                amount_kobo=purchased,
                kind=WalletTxnKind.DEPOSIT_PAYSTACK,
                source_tag=SourceTag.PURCHASED,
                idempotency_key=f"seed:pur:{purchased}",
            )

    def test_p2p_basic_purchased_only(self):
        # Alice has 10,000 kobo PURCHASED. Sends 1,000. Fee = 10 (1%).
        self._seed_alice(purchased=10_000)
        result = p2p_send(
            sender=self.alice,
            recipient=self.bob,
            amount_kobo=1_000,
            transfer_id="t1",
        )
        self.assertEqual(result.amount_kobo, 1_000)
        self.assertEqual(result.fee_kobo, 10)

        alice_w = Wallet.objects.get(user=self.alice)
        bob_w = Wallet.objects.get(user=self.bob)
        house_w = Wallet.objects.get(user=self.house)
        self.assertEqual(alice_w.balance_kobo, 8_990)
        self.assertEqual(alice_w.balance_purchased_kobo, 8_990)
        self.assertEqual(bob_w.balance_kobo, 1_000)
        self.assertEqual(bob_w.balance_purchased_kobo, 1_000)
        self.assertEqual(house_w.balance_kobo, 10)

    def test_p2p_source_tag_inheritance_multi_bucket(self):
        # 100 GIFT + 200 WON + 500 PURCHASED. Send 250 -> 100 GIFT + 150 WON.
        self._seed_alice(gift=100, won=200, purchased=500)
        # Fee is 250 * 100 / 10000 = 2 (kobo, floor of 2.5).
        result = p2p_send(
            sender=self.alice,
            recipient=self.bob,
            amount_kobo=250,
            transfer_id="t2",
        )
        bob_w = Wallet.objects.get(user=self.bob)
        # Bob receives the same source-tag mix as Alice's spend ladder.
        self.assertEqual(bob_w.balance_gift_kobo, 100)
        self.assertEqual(bob_w.balance_won_kobo, 150)
        self.assertEqual(bob_w.balance_purchased_kobo, 0)

        # Alice now: 0 GIFT (drained), 50 WON minus fee 2 = 48, 500 PURCHASED.
        # Fee = floor(250 * 100 / 10000) = 2 kobo. Spend ladder for the fee
        # consumes from WON (next non-empty bucket after GIFT was drained
        # for the principal).
        alice_w = Wallet.objects.get(user=self.alice)
        self.assertEqual(alice_w.balance_gift_kobo, 0)
        self.assertEqual(alice_w.balance_won_kobo, 48)
        self.assertEqual(alice_w.balance_purchased_kobo, 500)
        self.assertEqual(result.fee_kobo, 2)

    def test_p2p_blocks_self_send(self):
        self._seed_alice(purchased=10_000)
        with self.assertRaises(P2PInvalidRecipient):
            p2p_send(
                sender=self.alice,
                recipient=self.alice,
                amount_kobo=100,
            )

    def test_p2p_blocks_send_to_house(self):
        self._seed_alice(purchased=10_000)
        with self.assertRaises(P2PInvalidRecipient):
            p2p_send(
                sender=self.alice,
                recipient=self.house,
                amount_kobo=100,
            )

    def test_p2p_recipient_gift_cap_blocks(self):
        # Alice has 11M kobo all GIFT. Sends 11M to Bob — exceeds Bob's
        # GIFT cap of 10M.
        # Bypass enforce_gift_cap on Alice's seed by writing two voucher
        # credits 5M each spaced under-cap... actually we need >10M GIFT
        # on Alice but the cap is RECIPIENT side. We can seed Alice via
        # backdoor (raw txn write) since we're testing the recipient guard.
        from django.utils import timezone
        from datetime import timedelta

        fx = FxSnapshot.objects.first()
        alice_w = Wallet.objects.get(user=self.alice)
        # Backdoor seed Alice with 11M GIFT — bypass the credit() cap
        # because we want to exercise the *recipient* check during P2P.
        WalletTxn.objects.create(
            wallet=alice_w,
            amount_kobo=11_000_000,
            kind=WalletTxnKind.DEPOSIT_VOUCHER,
            source_tag=SourceTag.GIFT,
            ref_type="seed",
            ref_id="seed",
            fx_snapshot=fx,
            idempotency_key="seed:bypass:gift",
        )
        # Push Alice's seed credit's created_at outside the 24h window so it
        # doesn't count against HER own gift cap.
        WalletTxn.objects.filter(idempotency_key="seed:bypass:gift").update(
            created_at=timezone.now() - timedelta(hours=48)
        )
        alice_w.balance_kobo = 11_000_000
        alice_w.balance_gift_kobo = 11_000_000
        alice_w.save()

        with self.assertRaises(GiftDailyCapExceeded):
            p2p_send(
                sender=self.alice,
                recipient=self.bob,
                amount_kobo=10_500_000,  # over Bob's cap of 10M
                transfer_id="cap",
            )
        # Nothing sent — Alice intact, Bob still 0.
        bob_w = Wallet.objects.get(user=self.bob)
        self.assertEqual(bob_w.balance_kobo, 0)

    def test_p2p_daily_cap(self):
        # Seed Alice with cap + cap*1% (fee) + buffer. Cap is 2.5B kobo,
        # fee on cap-sized transfer is 25M kobo, so fund 2.55B for headroom.
        seed = P2P_DAILY_CAP_KOBO + (P2P_DAILY_CAP_KOBO // 100) + 1_000_000
        self._seed_alice(purchased=seed)
        # First P2P right at the cap should succeed.
        p2p_send(
            sender=self.alice,
            recipient=self.bob,
            amount_kobo=P2P_DAILY_CAP_KOBO,
            transfer_id="cap1",
        )
        # Next P2P pushes over the cap — must surface as
        # P2PDailyCapExceeded BEFORE we try to debit.
        with self.assertRaises(P2PDailyCapExceeded):
            p2p_send(
                sender=self.alice,
                recipient=self.bob,
                amount_kobo=1,
                transfer_id="cap2",
            )

    def test_p2p_fee_calculation_matches_p2p_fee_bps(self):
        # Need 1_000_000 + 10_000 fee. Seed 1.1M.
        self._seed_alice(purchased=1_010_000)
        result = p2p_send(
            sender=self.alice,
            recipient=self.bob,
            amount_kobo=1_000_000,
            transfer_id="fee1",
        )
        expected_fee = (1_000_000 * P2P_FEE_BPS) // 10000
        self.assertEqual(result.fee_kobo, expected_fee)
        self.assertEqual(result.fee_kobo, 10_000)

    def test_p2p_idempotent_via_transfer_id(self):
        self._seed_alice(purchased=10_000)
        result_a = p2p_send(
            sender=self.alice,
            recipient=self.bob,
            amount_kobo=1_000,
            transfer_id="dup",
        )
        # Replay — same transfer_id, same idempotency_key on each txn.
        # We expect this to be a no-op (return same txn ids).
        result_b = p2p_send(
            sender=self.alice,
            recipient=self.bob,
            amount_kobo=1_000,
            transfer_id="dup",
        )
        self.assertEqual(
            sorted(result_a.sender_debit_txn_ids),
            sorted(result_b.sender_debit_txn_ids),
        )
        self.assertEqual(
            result_a.house_credit_txn_id, result_b.house_credit_txn_id
        )

        alice_w = Wallet.objects.get(user=self.alice)
        bob_w = Wallet.objects.get(user=self.bob)
        # Single P2P, not double.
        self.assertEqual(alice_w.balance_kobo, 8_990)
        self.assertEqual(bob_w.balance_kobo, 1_000)
