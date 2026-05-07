"""Tests for admin override + audit log behavior on settle_market."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from afc_tournament_and_scrims.models import Event
from afc_wallet.models import (
    FxSnapshot,
    SourceTag,
    Wallet,
    WalletTxnKind,
)
from afc_wallet.services import credit, get_or_create_house_user

from afc_wager.models import (
    Market,
    MarketOption,
    MarketStatus,
    MarketTemplate,
    OptionSource,
    Settlement,
    SettlementResolution,
    Wager,
    WagerLine,
)
from afc_wager.settlement import settle_market


User = get_user_model()


class AdminOverrideAuditTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="test"
        )
        cls.house = get_or_create_house_user()
        Wallet.objects.create(user=cls.house)
        cls.admin = User.objects.create_user(
            username="admin",
            email="a@x.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=cls.admin)

    def _setup_market(self, suggested_option_idx: int = 0):
        alice = User.objects.create_user(
            username=f"alice_{timezone.now().timestamp()}",
            email=f"al{timezone.now().timestamp()}@x.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=alice)
        bob = User.objects.create_user(
            username=f"bob_{timezone.now().timestamp()}",
            email=f"bo{timezone.now().timestamp()}@x.com",
            password="x",
            full_name="B",
            country="NG",
        )
        Wallet.objects.create(user=bob)
        credit(
            user=alice,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key=f"alice:{timezone.now().timestamp()}",
        )
        credit(
            user=bob,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key=f"bob:{timezone.now().timestamp()}",
        )

        event = Event.objects.create(
            event_name=f"Event_{timezone.now().timestamp()}",
            slug=f"event-{timezone.now().timestamp()}",
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
            event_rules="-",
            event_status="upcoming",
            registration_link="https://example.com/r",
            number_of_stages=1,
        )
        template, _ = MarketTemplate.objects.get_or_create(
            code="match_winner",
            defaults=dict(
                display_name="Match Winner",
                option_source=OptionSource.TEAMS,
                auto_gradable=True,
                grader_key="match_winner",
            ),
        )
        market = Market.objects.create(
            event=event,
            template=template,
            title="Match 1",
            status=MarketStatus.PENDING_SETTLEMENT,
            opens_at=timezone.now() - timedelta(hours=1),
            lock_at=timezone.now() + timedelta(hours=1),
            total_pool_kobo=20_000,
            created_by_admin=self.admin,
        )
        opt_a = MarketOption.objects.create(
            market=market, label="A", sort_order=0
        )
        opt_b = MarketOption.objects.create(
            market=market, label="B", sort_order=1
        )
        # Set the suggested option to index 0 (A).
        market.suggested_option = [opt_a, opt_b][suggested_option_idx]
        market.save()

        wa = Wager.objects.create(
            user=alice, market=market, total_stake_kobo=10_000
        )
        WagerLine.objects.create(wager=wa, option=opt_a, stake_kobo=10_000)
        wb = Wager.objects.create(
            user=bob, market=market, total_stake_kobo=10_000
        )
        WagerLine.objects.create(wager=wb, option=opt_b, stake_kobo=10_000)

        return market, opt_a, opt_b, alice, bob

    def test_settlement_records_suggested_and_final(self):
        market, opt_a, opt_b, alice, bob = self._setup_market(
            suggested_option_idx=0
        )
        s = settle_market(
            market_id=market.pk,
            final_option_id=opt_a.pk,
            admin_user=self.admin,
        )
        self.assertEqual(s.suggested_option_id, opt_a.pk)
        self.assertEqual(s.final_option_id, opt_a.pk)
        self.assertEqual(s.override_reason, "")  # auto-confirm

    def test_admin_override_records_reason(self):
        market, opt_a, opt_b, alice, bob = self._setup_market(
            suggested_option_idx=0
        )
        # Suggestion is A; admin overrides with B + reason.
        s = settle_market(
            market_id=market.pk,
            final_option_id=opt_b.pk,
            admin_user=self.admin,
            override_reason="Replay shows B took the booyah",
        )
        self.assertEqual(s.suggested_option_id, opt_a.pk)
        self.assertEqual(s.final_option_id, opt_b.pk)
        self.assertEqual(
            s.override_reason, "Replay shows B took the booyah"
        )

    def test_settlement_records_admin_user(self):
        market, opt_a, opt_b, alice, bob = self._setup_market()
        s = settle_market(
            market_id=market.pk,
            final_option_id=opt_a.pk,
            admin_user=self.admin,
        )
        self.assertEqual(s.confirmed_by_admin_id, self.admin.pk)

    def test_winner_settlement_invariants(self):
        market, opt_a, opt_b, alice, bob = self._setup_market()
        s = settle_market(
            market_id=market.pk,
            final_option_id=opt_a.pk,
            admin_user=self.admin,
        )
        self.assertEqual(s.resolution, SettlementResolution.WINNER)
        # rake = 20_000 * 500 / 10000 = 1_000. paid_out = 19_000. dust = 0.
        self.assertEqual(s.rake_kobo, 1_000)
        self.assertEqual(s.paid_out_kobo, 19_000)
        self.assertEqual(s.winners_count, 1)  # alice only
        self.assertEqual(s.lines_count, 2)  # alice + bob

        # Wallet check: alice receives the full net pool (sole winner).
        alice.refresh_from_db()
        alice_w = Wallet.objects.get(user=alice)
        # Alice started with 10_000 PURCHASED, spent on wager (not yet
        # tracked through actual placement), got 19_000 WON from settlement.
        self.assertEqual(alice_w.balance_won_kobo, 19_000)
        # House gets rake + dust.
        house_w = Wallet.objects.get(user=self.house)
        self.assertEqual(house_w.balance_kobo, 1_000)

    def test_void_no_winner_refunds_all(self):
        # Create market where neither option has any wagers, but pool > 0
        # (shouldn't happen in practice but tests the VOID_NO_WINNER path).
        # Easier: settle to an option with no winning lines, but our setup
        # always has lines on both. Use the 3rd option scheme:
        market, opt_a, opt_b, alice, bob = self._setup_market()
        # Add a 3rd option no one bet on.
        opt_c = MarketOption.objects.create(
            market=market, label="C", sort_order=2
        )
        s = settle_market(
            market_id=market.pk,
            final_option_id=opt_c.pk,
            admin_user=self.admin,
        )
        self.assertEqual(s.resolution, SettlementResolution.VOID_NO_WINNER)
        self.assertEqual(s.rake_kobo, 0)
        # Both alice and bob refunded their 10K.
        alice_w = Wallet.objects.get(user=alice)
        bob_w = Wallet.objects.get(user=bob)
        # Each had 10K PURCHASED initially + 10K refund.
        # Refund tag is PURCHASED so balance_purchased_kobo = 20K.
        self.assertEqual(alice_w.balance_purchased_kobo, 20_000)
        self.assertEqual(bob_w.balance_purchased_kobo, 20_000)
