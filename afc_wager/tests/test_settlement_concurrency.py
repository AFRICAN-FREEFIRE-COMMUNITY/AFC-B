"""Concurrency tests for settle_market() — double-settle blocked."""

from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Thread

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase
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
    Wager,
    WagerLine,
)
from afc_wager.settlement import settle_market


User = get_user_model()


class SettlementConcurrencyTestCase(TransactionTestCase):
    available_apps = [
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "afc_auth",
        "afc_team",
        "afc_tournament_and_scrims",
        "afc_wallet",
        "afc_wager",
    ]

    def setUp(self):
        FxSnapshot.objects.create(
            ngn_per_usd=Decimal("1500.0000"), source="test"
        )
        self.house = get_or_create_house_user()
        Wallet.objects.create(user=self.house)
        self.admin = User.objects.create_user(
            username="admin",
            email="a@x.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=self.admin)
        self.alice = User.objects.create_user(
            username="alice",
            email="al@x.com",
            password="x",
            full_name="A",
            country="NG",
        )
        Wallet.objects.create(user=self.alice)
        self.bob = User.objects.create_user(
            username="bob",
            email="b@x.com",
            password="x",
            full_name="B",
            country="NG",
        )
        Wallet.objects.create(user=self.bob)

        credit(
            user=self.alice,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="alice:seed",
        )
        credit(
            user=self.bob,
            amount_kobo=10_000,
            kind=WalletTxnKind.DEPOSIT_PAYSTACK,
            source_tag=SourceTag.PURCHASED,
            idempotency_key="bob:seed",
        )

        self.event = Event.objects.create(
            event_name="Test Event",
            slug="test-event-conc",
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
        self.template = MarketTemplate.objects.create(
            code="match_winner",
            display_name="Match Winner",
            option_source=OptionSource.TEAMS,
            auto_gradable=True,
            grader_key="match_winner",
        )
        self.market = Market.objects.create(
            event=self.event,
            template=self.template,
            title="Match 1",
            status=MarketStatus.PENDING_SETTLEMENT,
            opens_at=timezone.now() - timedelta(hours=1),
            lock_at=timezone.now() + timedelta(hours=1),
            total_pool_kobo=20_000,
            created_by_admin=self.admin,
        )
        self.opt_a = MarketOption.objects.create(
            market=self.market, label="A", sort_order=0
        )
        self.opt_b = MarketOption.objects.create(
            market=self.market, label="B", sort_order=1
        )
        wa = Wager.objects.create(
            user=self.alice, market=self.market, total_stake_kobo=10_000
        )
        WagerLine.objects.create(
            wager=wa, option=self.opt_a, stake_kobo=10_000
        )
        wb = Wager.objects.create(
            user=self.bob, market=self.market, total_stake_kobo=10_000
        )
        WagerLine.objects.create(
            wager=wb, option=self.opt_b, stake_kobo=10_000
        )

    def test_double_settle_blocked(self):
        """Two threads racing to settle the same market: exactly 1 wins."""
        barrier = Barrier(2)
        results = []
        errors = []

        def worker():
            barrier.wait()
            try:
                s = settle_market(
                    market_id=self.market.pk,
                    final_option_id=self.opt_a.pk,
                    admin_user=self.admin,
                )
                results.append(s.pk)
            except (ValueError, Exception) as e:
                errors.append(type(e).__name__)
            finally:
                close_old_connections()

        a = Thread(target=worker)
        b = Thread(target=worker)
        a.start()
        b.start()
        a.join()
        b.join()

        # Exactly one settlement created.
        self.assertEqual(len(results), 1, f"results={results} errors={errors}")
        self.assertEqual(len(errors), 1)

        from afc_wager.models import Settlement

        self.assertEqual(Settlement.objects.count(), 1)
        # Market status promoted to SETTLED.
        self.market.refresh_from_db()
        self.assertEqual(self.market.status, MarketStatus.SETTLED)
