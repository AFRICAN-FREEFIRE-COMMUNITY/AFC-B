"""Lifecycle FSM tests for Market.

Spec Section 7 transitions:
DRAFT -> OPEN -> LOCKED -> PENDING_SETTLEMENT -> SETTLED|VOIDED

Invalid transitions must raise. Valid transitions update status atomically.
For v1, the FSM is enforced by the service layer (settle_market checks
status==PENDING_SETTLEMENT, etc.). These tests assert that contract.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from afc_tournament_and_scrims.models import Event
from afc_wallet.models import FxSnapshot, Wallet
from afc_wallet.services import (
    credit,
    get_or_create_house_user,
)
from afc_wallet.models import SourceTag, WalletTxnKind
from afc_wager.models import (
    Market,
    MarketOption,
    MarketStatus,
    MarketTemplate,
    OptionSource,
    Wager,
    WagerLine,
)
from afc_wager.settlement import settle_market


User = get_user_model()


class LifecycleFSMTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="test"
        )

        cls.house = get_or_create_house_user()
        Wallet.objects.create(user=cls.house)

        cls.admin = User.objects.create_user(
            username="admin",
            email="admin@x.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=cls.admin)

        cls.alice = User.objects.create_user(
            username="alice",
            email="a@x.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=cls.alice)

        cls.event = Event.objects.create(
            event_name="Test Event",
            slug="test-event",
            competition_type="tournament",
            participant_type="squad",
            event_type="internal",
            max_teams_or_players=12,
            event_mode="virtual",
            start_date=timezone.now().date(),
            end_date=(timezone.now() + timedelta(days=7)).date(),
            registration_open_date=timezone.now().date(),
            registration_end_date=(timezone.now() + timedelta(days=1)).date(),
            prizepool="N1M",
            event_rules="See Discord",
            event_status="upcoming",
            registration_link="https://example.com/r",
            number_of_stages=1,
        )

        cls.template = MarketTemplate.objects.create(
            code="match_winner",
            display_name="Match Winner",
            option_source=OptionSource.TEAMS,
            auto_gradable=True,
            grader_key="match_winner",
        )

    def _make_market(self, status=MarketStatus.OPEN, pool=0):
        m = Market.objects.create(
            event=self.event,
            template=self.template,
            title="Match 1 Winner",
            description="",
            status=status,
            opens_at=timezone.now() - timedelta(minutes=10),
            lock_at=timezone.now() + timedelta(minutes=30),
            min_stake_kobo=10_000,
            rake_bps=500,
            cancel_fee_bps=100,
            total_pool_kobo=pool,
            created_by_admin=self.admin,
        )
        opt_a = MarketOption.objects.create(
            market=m, label="Team A", sort_order=0
        )
        opt_b = MarketOption.objects.create(
            market=m, label="Team B", sort_order=1
        )
        return m, opt_a, opt_b

    # ── settle from invalid status raises ────────────────────────────

    def test_settle_from_DRAFT_raises(self):
        m, a, b = self._make_market(status=MarketStatus.DRAFT)
        with self.assertRaises(ValueError):
            settle_market(
                market_id=m.pk,
                final_option_id=a.pk,
                admin_user=self.admin,
            )

    def test_settle_from_OPEN_raises(self):
        m, a, b = self._make_market(status=MarketStatus.OPEN)
        with self.assertRaises(ValueError):
            settle_market(
                market_id=m.pk,
                final_option_id=a.pk,
                admin_user=self.admin,
            )

    def test_settle_from_LOCKED_raises(self):
        m, a, b = self._make_market(status=MarketStatus.LOCKED)
        with self.assertRaises(ValueError):
            settle_market(
                market_id=m.pk,
                final_option_id=a.pk,
                admin_user=self.admin,
            )

    def test_settle_from_SETTLED_raises(self):
        m, a, b = self._make_market(status=MarketStatus.SETTLED)
        with self.assertRaises(ValueError):
            settle_market(
                market_id=m.pk,
                final_option_id=a.pk,
                admin_user=self.admin,
            )

    def test_settle_from_VOIDED_raises(self):
        m, a, b = self._make_market(status=MarketStatus.VOIDED)
        with self.assertRaises(ValueError):
            settle_market(
                market_id=m.pk,
                final_option_id=a.pk,
                admin_user=self.admin,
            )

    # ── settle from PENDING_SETTLEMENT succeeds ──────────────────────

    def test_settle_from_PENDING_SETTLEMENT_promotes_to_SETTLED(self):
        # Need actual stakes to satisfy WINNER resolution. Set up Alice w/
        # a wager on Option A; loser stakes are simulated via total_pool_kobo.
        # First seed Alice's wallet so she can afford the stake.
        credit(
            user=self.alice,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:alice",
        )
        m, opt_a, opt_b = self._make_market(
            status=MarketStatus.PENDING_SETTLEMENT, pool=20_000
        )
        # Alice wagers 10K on Team A (winning). Other 10K assumed on B.
        # Create matching Wager + WagerLine + losing line for B.
        bob = User.objects.create_user(
            username="bob",
            email="b@x.com",
            password="x",
            full_name="B",
            country="NG",
        )
        Wallet.objects.create(user=bob)
        credit(
            user=bob,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:bob",
        )
        wa = Wager.objects.create(
            user=self.alice, market=m, total_stake_kobo=10_000
        )
        WagerLine.objects.create(
            wager=wa, option=opt_a, stake_kobo=10_000
        )
        wb = Wager.objects.create(
            user=bob, market=m, total_stake_kobo=10_000
        )
        WagerLine.objects.create(
            wager=wb, option=opt_b, stake_kobo=10_000
        )

        settlement = settle_market(
            market_id=m.pk,
            final_option_id=opt_a.pk,
            admin_user=self.admin,
        )

        m.refresh_from_db()
        self.assertEqual(m.status, MarketStatus.SETTLED)
        self.assertEqual(m.winning_option_id, opt_a.pk)
        self.assertEqual(settlement.resolution, "WINNER")

    def test_double_settle_blocked(self):
        """Re-settling an already-SETTLED market raises."""
        # Reuse the prior test's path then attempt re-settle.
        credit(
            user=self.alice,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:alice2",
        )
        bob = User.objects.create_user(
            username="bob2",
            email="b2@x.com",
            password="x",
            full_name="B",
            country="NG",
        )
        Wallet.objects.create(user=bob)
        credit(
            user=bob,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="seed:bob2",
        )
        m, opt_a, opt_b = self._make_market(
            status=MarketStatus.PENDING_SETTLEMENT, pool=20_000
        )
        wa = Wager.objects.create(
            user=self.alice, market=m, total_stake_kobo=10_000
        )
        WagerLine.objects.create(
            wager=wa, option=opt_a, stake_kobo=10_000
        )
        wb = Wager.objects.create(
            user=bob, market=m, total_stake_kobo=10_000
        )
        WagerLine.objects.create(
            wager=wb, option=opt_b, stake_kobo=10_000
        )

        settle_market(
            market_id=m.pk,
            final_option_id=opt_a.pk,
            admin_user=self.admin,
        )

        # Second settle must raise — market is now SETTLED.
        with self.assertRaises(ValueError):
            settle_market(
                market_id=m.pk,
                final_option_id=opt_a.pk,
                admin_user=self.admin,
            )
