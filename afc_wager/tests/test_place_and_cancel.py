"""Placing (with the guards), paying (webhook and verify, idempotent), expiring, cancelling."""
from datetime import date, timedelta
from unittest.mock import patch

from django.utils import timezone

from afc_auth.models import UserProfile
from afc_wager import services
from afc_wager.models import LedgerEntry, Market, Wager, WagerSettings
from afc_wager.payments import handle_charge_success

from .fixtures import WagerTestCase


class PlaceTests(WagerTestCase):

    def setUp(self):
        super().setUp()
        self.market = self.make_market()

    def test_place_creates_an_unpaid_wager_and_a_payment_url_and_touches_no_pool(self):
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertTrue(body["payment_url"].startswith("http"))
        self.assertEqual(body["wager"]["status"], Wager.PENDING_PAYMENT)
        self.market.refresh_from_db()
        self.assertEqual(self.market.cached_pool_kobo, 0)
        self.assertEqual(self.market.cached_wager_count, 0)

    def test_payment_return_follows_the_api_host(self):
        """A stake placed against a localhost API comes back to the local frontend, the same rule
        as the Discord and v-ent SSO bounces; any other host returns to production."""
        with self.settings(FRONTEND_URL="https://prod.example", FRONTEND_URL_LOCAL="http://localhost:3000",
                           ALLOWED_HOSTS=["testserver", "localhost"]):
            local = self.client.post(f"/wagers/markets/{self.market.slug}/place/",
                                     {"lines": [{"option_id": self.opt_a.id, "stake_kobo": 100_000}]},
                                     content_type="application/json", HTTP_HOST="localhost:8010", **self.player_auth)
            self.assertEqual(local.status_code, 201, local.content)
            self.assertTrue(local.json()["payment_url"].startswith("http://localhost:3000/wagers/"), local.json()["payment_url"])
            prod = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
            self.assertTrue(prod.json()["payment_url"].startswith("https://prod.example/wagers/"), prod.json()["payment_url"])

    def test_paying_activates_once_and_adds_to_the_pool(self):
        token = self.place_and_pay(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000},
                                                                    {"option_id": self.opt_b.id, "stake_kobo": 50_000}])
        w = Wager.objects.get(public_token=token)
        self.assertEqual(w.status, Wager.ACTIVE)
        self.market.refresh_from_db()
        self.opt_a.refresh_from_db()
        self.assertEqual(self.market.cached_pool_kobo, 150_000)
        self.assertEqual(self.market.cached_wager_count, 1)
        self.assertEqual(self.opt_a.cached_pool_kobo, 100_000)
        # the webhook arriving after the verify is a no-op
        handle_charge_success({"reference": w.paystack_reference, "amount": 150_000, "id": 42})
        self.market.refresh_from_db()
        self.assertEqual(self.market.cached_pool_kobo, 150_000)
        self.assertEqual(self.market.cached_wager_count, 1)

    def test_the_webhook_alone_activates_and_a_wrong_amount_does_not(self):
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        ref = Wager.objects.get(public_token=resp.json()["wager"]["token"]).paystack_reference
        self.assertFalse(handle_charge_success({"reference": ref, "amount": 99_000, "id": 1}))
        self.assertEqual(Wager.objects.get(paystack_reference=ref).status, Wager.PENDING_PAYMENT)
        self.assertTrue(handle_charge_success({"reference": ref, "amount": 100_000, "id": 1}))
        self.assertEqual(Wager.objects.get(paystack_reference=ref).status, Wager.ACTIVE)

    def test_verify_is_the_owners_only(self):
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        ref = Wager.objects.get(public_token=resp.json()["wager"]["token"]).paystack_reference
        other = self.client.get(f"/wagers/payments/verify/?reference={ref}", **self.other_auth)
        self.assertEqual(other.status_code, 404)
        self.assertEqual(other.json()["code"], "payment_not_found")

    def test_unpaid_wagers_expire_and_a_late_payment_after_lock_is_refunded(self):
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        w = Wager.objects.get(public_token=resp.json()["wager"]["token"])
        self.assertEqual(services.expire_unpaid(now=timezone.now() + timedelta(hours=1)), 1)
        w.refresh_from_db()
        self.assertEqual(w.status, Wager.EXPIRED)
        # the market locks, then the charge lands anyway
        services.lock_market(self.market)
        services.activate_wager(w, charge_id="late")
        w.refresh_from_db()
        self.assertEqual(w.status, Wager.REFUNDED)
        self.assertEqual(w.refund_kobo, 100_000)
        self.assertEqual(services.account_for(self.player).balance_kobo, 100_000)
        self.market.refresh_from_db()
        self.assertEqual(self.market.cached_pool_kobo, 0)

    # ── guards ──
    def test_signed_out_cannot_place(self):
        resp = self.client.post(f"/wagers/markets/{self.market.slug}/place/", {"lines": []}, content_type="application/json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "auth_required")

    def test_no_date_of_birth_is_refused_and_underage_is_refused(self):
        profile = UserProfile.objects.get(user=self.player)
        profile.date_of_birth = None
        profile.save()
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.assertEqual((resp.status_code, resp.json()["code"]), (403, "age_required"))
        profile.date_of_birth = date.today() - timedelta(days=365 * 16)
        profile.save()
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.assertEqual((resp.status_code, resp.json()["code"]), (403, "underage"))

    def test_kill_switch(self):
        cfg = WagerSettings.get()
        cfg.wagering_enabled = False
        cfg.maintenance_message = "Back at 6."
        cfg.save()
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.assertEqual((resp.status_code, resp.json()["code"], resp.json()["message"]), (503, "wagering_paused", "Back at 6."))

    def test_locked_market_and_past_lock_at_refuse(self):
        past = self.make_market(title="Past", lock_in_minutes=-1)
        resp = self.place(self.player_auth, past, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.assertEqual((resp.status_code, resp.json()["code"]), (409, "market_locked"))
        draft = self.make_market(title="Draft", status=Market.DRAFT)
        resp = self.place(self.player_auth, draft, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        self.assertEqual(resp.status_code, 404)

    def test_stake_bounds(self):
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 10}])
        self.assertEqual(resp.json()["code"], "stake_below_minimum")
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 60_000_000}])
        self.assertEqual(resp.json()["code"], "stake_above_maximum")
        resp = self.place(self.player_auth, self.market, [{"option_id": 999999, "stake_kobo": 100_000}])
        self.assertEqual(resp.json()["code"], "option_not_found")
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": "abc"}])
        self.assertEqual(resp.json()["code"], "lines_invalid")
        resp = self.place(self.player_auth, self.market, [])
        self.assertEqual(resp.json()["code"], "lines_required")

    def test_daily_cap_and_self_exclusion(self):
        services.set_limits(self.player, caps={"daily_stake_cap_kobo": 150_000})
        self.place_and_pay(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_b.id, "stake_kobo": 100_000}])
        self.assertEqual(resp.json()["code"], "daily_stake_cap")
        services.set_self_exclusion(self.player, months=1)
        resp = self.place(self.player_auth, self.market, [{"option_id": self.opt_b.id, "stake_kobo": 50_000}])
        self.assertEqual((resp.status_code, resp.json()["code"]), (403, "self_excluded"))


class CancelTests(WagerTestCase):

    def setUp(self):
        super().setUp()
        self.market = self.make_market()
        self.token = self.place_and_pay(self.player_auth, self.market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])

    def test_cancel_refunds_minus_the_fee_and_books_the_fee_to_the_house(self):
        resp = self.client.post(f"/wagers/wager/{self.token}/cancel/", **self.player_auth)
        self.assertEqual(resp.status_code, 200, resp.content)
        w = Wager.objects.get(public_token=self.token)
        self.assertEqual((w.status, w.cancel_fee_kobo, w.refund_kobo), (Wager.CANCELLED, 1_000, 99_000))
        self.assertEqual(services.account_for(self.player).balance_kobo, 99_000)
        self.assertEqual(LedgerEntry.objects.filter(kind=LedgerEntry.HOUSE_CANCEL_FEE).get().amount_kobo, 1_000)
        self.market.refresh_from_db()
        self.assertEqual((self.market.cached_pool_kobo, self.market.cached_wager_count), (0, 0))

    def test_cancel_after_lock_is_refused_and_the_money_stays(self):
        services.lock_market(self.market)
        resp = self.client.post(f"/wagers/wager/{self.token}/cancel/", **self.player_auth)
        self.assertEqual((resp.status_code, resp.json()["code"]), (409, "market_locked"))
        self.assertEqual(Wager.objects.get(public_token=self.token).status, Wager.ACTIVE)

    def test_another_player_cannot_cancel_it(self):
        resp = self.client.post(f"/wagers/wager/{self.token}/cancel/", **self.other_auth)
        self.assertEqual((resp.status_code, resp.json()["code"]), (404, "wager_not_found"))

    def test_a_second_cancel_is_refused(self):
        self.client.post(f"/wagers/wager/{self.token}/cancel/", **self.player_auth)
        resp = self.client.post(f"/wagers/wager/{self.token}/cancel/", **self.player_auth)
        self.assertEqual(resp.json()["code"], "wager_not_active")
        self.assertEqual(services.account_for(self.player).balance_kobo, 99_000)
