"""Lock at lock_at, suggest from the stats, settle (winner / void paths), void by admin, and the
admin endpoints that drive them. The link the May branch never had."""
from datetime import timedelta

from django.utils import timezone

from afc_auth.models import Notifications
from afc_wager import services
from afc_wager.models import LedgerEntry, Market, Settlement, Wager, WagerLine
from afc_wager.services import WagerError

from .fixtures import WagerTestCase


class LifecycleTests(WagerTestCase):

    def setUp(self):
        super().setUp()
        self.market = self.make_market()
        # player: 100k on Alpha; other: 50k on Bravo
        self.t_player = self.place_and_pay(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.t_other = self.place_and_pay(self.other_auth, self.market, [{"option_id": self.opt_b.id, "stake_kobo": 50_000}])

    def test_the_sweep_locks_at_lock_at_and_tells_the_players(self):
        self.assertEqual(services.lock_due_markets(now=timezone.now()), 0)
        self.assertEqual(services.lock_due_markets(now=timezone.now() + timedelta(hours=2)), 1)
        self.market.refresh_from_db()
        self.assertEqual(self.market.status, Market.LOCKED)
        self.assertTrue(Notifications.objects.filter(user=self.player, notification_type="wager", title="Market locked").exists())
        # idempotent
        self.assertEqual(services.lock_due_markets(now=timezone.now() + timedelta(hours=2)), 0)

    def test_suggestion_waits_for_the_result_then_reads_placement_1(self):
        services.lock_market(self.market)
        m = services.suggest_settlement(self.market)
        self.assertEqual(m.status, Market.LOCKED)
        self.assertTrue(m.suggestion_evidence.get("waiting"))
        self.record_result(alpha_placement=1, bravo_placement=2)
        m = services.suggest_settlement(self.market)
        self.assertEqual(m.status, Market.PENDING_SETTLEMENT)
        self.assertEqual(m.suggested_option_id, self.opt_a.id)
        self.assertEqual(m.suggestion_evidence["winner"], "Alpha")

    def test_settle_pays_the_winner_the_net_pool_and_books_the_rake(self):
        services.lock_market(self.market)
        self.record_result()
        services.suggest_settlement(self.market)
        settlement = services.settle_market(self.market, final_option=self.opt_a, by=self.admin)
        self.assertEqual(settlement.resolution, Settlement.WINNER)
        # pool 150k, rake 5% = 7.5k, net 142.5k, one winner with all of the winning side
        self.assertEqual((settlement.pool_kobo, settlement.rake_kobo, settlement.paid_total_kobo), (150_000, 7_500, 142_500))
        self.assertEqual(services.account_for(self.player).balance_kobo, 142_500)
        self.assertEqual(services.account_for(self.other).balance_kobo, 0)
        self.assertEqual(Wager.objects.get(public_token=self.t_player).status, Wager.WON)
        self.assertEqual(Wager.objects.get(public_token=self.t_other).status, Wager.LOST)
        self.assertEqual(WagerLine.objects.get(wager__public_token=self.t_player).outcome, WagerLine.WON)
        self.assertEqual(LedgerEntry.objects.get(kind=LedgerEntry.HOUSE_RAKE).amount_kobo, 7_500)
        self.market.refresh_from_db()
        self.assertEqual((self.market.status, self.market.settled_option_id), (Market.SETTLED, self.opt_a.id))
        # notifications: winner told, loser told
        self.assertTrue(Notifications.objects.filter(user=self.player, title="You won").exists())
        self.assertTrue(Notifications.objects.filter(user=self.other, title="Not this time").exists())
        # the player's ledger names the market as data, for the page to phrase in its language
        recent = self.client.get("/wagers/winnings/", **self.player_auth).json()["recent"]
        self.assertEqual((recent[0]["kind"], recent[0]["label"]), ("PAYOUT", "Match 1 winner"))

    def test_a_second_settle_is_refused(self):
        services.lock_market(self.market)
        services.settle_market(self.market, final_option=self.opt_a, by=self.admin)
        with self.assertRaises(WagerError) as ctx:
            services.settle_market(self.market, final_option=self.opt_b, by=self.admin)
        self.assertEqual(ctx.exception.code, "market_settled")
        self.assertEqual(services.account_for(self.player).balance_kobo, 142_500)

    def test_override_of_the_suggestion_needs_a_reason(self):
        services.lock_market(self.market)
        self.record_result()
        services.suggest_settlement(self.market)
        with self.assertRaises(WagerError) as ctx:
            services.settle_market(self.market, final_option=self.opt_b, by=self.admin)
        self.assertEqual(ctx.exception.code, "override_reason_required")
        s = services.settle_market(self.market, final_option=self.opt_b, by=self.admin, override_reason="Alpha disqualified")
        self.assertEqual(s.override_reason, "Alpha disqualified")
        self.assertEqual(services.account_for(self.other).balance_kobo, 142_500)

    def test_nobody_on_the_winner_refunds_everyone(self):
        services.lock_market(self.market)
        other_option = self.opt_b
        # settle on an option nobody backed: create one more option with no stakes
        from afc_wager.models import MarketOption
        charlie = MarketOption.objects.create(market=self.market, label="Charlie", sort_order=2)
        s = services.settle_market(self.market, final_option=charlie, by=self.admin)
        self.assertEqual(s.resolution, Settlement.VOID_NO_WINNER)
        self.assertEqual(services.account_for(self.player).balance_kobo, 100_000)
        self.assertEqual(services.account_for(self.other).balance_kobo, 50_000)
        self.assertEqual(LedgerEntry.objects.filter(kind=LedgerEntry.HOUSE_RAKE).count(), 0)
        self.assertEqual(Wager.objects.get(public_token=self.t_player).status, Wager.REFUNDED)

    def test_everyone_on_the_winner_refunds_everyone(self):
        market = self.make_market(title="Solo")
        a = self.place_and_pay(self.player_auth, market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        b = self.place_and_pay(self.other_auth, market, [{"option_id": self.opt_a.id, "stake_kobo": 70_000}])
        services.lock_market(market)
        s = services.settle_market(market, final_option=self.opt_a, by=self.admin)
        self.assertEqual(s.resolution, Settlement.VOID_SOLO_WAGER)
        self.assertEqual(s.refund_total_kobo, 170_000)

    def test_void_refunds_in_full_and_expires_the_unpaid(self):
        unpaid = self.place(self.player_auth, self.market, [{"option_id": self.opt_b.id, "stake_kobo": 60_000}]).json()["wager"]["token"]
        s = services.void_market(self.market, by=self.admin, reason="Match cancelled")
        self.assertEqual(s.resolution, Settlement.VOID_ADMIN)
        self.assertEqual(s.refund_total_kobo, 150_000)
        self.assertEqual(services.account_for(self.player).balance_kobo, 100_000)
        self.assertEqual(Wager.objects.get(public_token=unpaid).status, Wager.EXPIRED)
        self.market.refresh_from_db()
        self.assertEqual((self.market.status, self.market.void_reason), (Market.VOID, "Match cancelled"))
        with self.assertRaises(WagerError):
            services.void_market(self.market, by=self.admin, reason="again")

    def test_reopen_a_locked_market(self):
        services.lock_market(self.market, by=self.admin)
        later = timezone.now() + timedelta(hours=5)
        m = services.reopen_market(self.market, new_lock_at=later, by=self.admin, reason="postponed")
        self.assertEqual(m.status, Market.OPEN)
        self.assertEqual(m.lock_at, later)


class LifecycleEndpointTests(WagerTestCase):
    """The same through the CMS: the queue shows LOCKED and PENDING rows with evidence; settle
    takes an option id; a plain admin is refused."""

    def setUp(self):
        super().setUp()
        self.market = self.make_market()
        self.place_and_pay(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.place_and_pay(self.other_auth, self.market, [{"option_id": self.opt_b.id, "stake_kobo": 50_000}])

    def test_lock_suggest_settle_over_http(self):
        r = self.client.post(f"/wagers/admin/markets/{self.market.slug}/lock/", **self.admin_auth)
        self.assertEqual(r.status_code, 200, r.content)
        q = self.client.get("/wagers/admin/queue/", **self.admin_auth).json()
        self.assertEqual((q["locked"], q["pending"]), (1, 0))
        self.record_result()
        r = self.client.post(f"/wagers/admin/markets/{self.market.slug}/suggest/", **self.admin_auth)
        self.assertEqual(r.json()["market"]["status"], Market.PENDING_SETTLEMENT)
        self.assertEqual(r.json()["market"]["suggested_option"], "Alpha")
        self.assertEqual(r.json()["market"]["suggestion_evidence"]["winner"], "Alpha")
        q = self.client.get("/wagers/admin/queue/", **self.admin_auth).json()
        self.assertEqual((q["locked"], q["pending"]), (0, 1))
        r = self.client.post(f"/wagers/admin/markets/{self.market.slug}/settle/", {"option_id": self.opt_a.id},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["settlement"]["paid_total_kobo"], 142_500)
        # the player's page shows it
        page = self.client.get(f"/wagers/markets/{self.market.slug}/", **self.player_auth).json()
        self.assertEqual(page["status"], Market.SETTLED)
        self.assertEqual(page["my_wagers"][0]["status"], Wager.WON)
        self.assertEqual(page["my_wagers"][0]["payout_kobo"], 142_500)

    def test_plain_admin_and_player_are_refused(self):
        for auth, code in ((self.plain_auth, 403), (self.player_auth, 403)):
            r = self.client.post(f"/wagers/admin/markets/{self.market.slug}/lock/", **auth)
            self.assertEqual(r.status_code, code)
            self.assertEqual(r.json()["code"], "wager_admin_required")
        r = self.client.post(f"/wagers/admin/markets/{self.market.slug}/lock/")
        self.assertEqual(r.status_code, 401)

    def test_void_over_http_needs_a_reason(self):
        r = self.client.post(f"/wagers/admin/markets/{self.market.slug}/void/", {"reason": ""},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.json()["code"], "reason_required")
        r = self.client.post(f"/wagers/admin/markets/{self.market.slug}/void/", {"reason": "Server crash"},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["market"]["status"], Market.VOID)
