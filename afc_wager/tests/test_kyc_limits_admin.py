"""KYC through the existing 2FA WhatsApp method, the limits rules (tighten now, loosen in 24 h),
the settings endpoint gate, market creation through the CMS, and the shop webhook delegate."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from afc_auth import outbox
from afc_wager import services
from afc_wager.models import Market, MarketOption, PlayerLimits, Wager, WagerSettings

from .fixtures import WagerTestCase


class KycTests(WagerTestCase):

    def test_state_reads_the_profile_the_discord_id_and_the_age(self):
        state = self.client.get("/wagers/kyc/", **self.player_auth).json()
        self.assertEqual(state["whatsapp_number"], "+2348012345678")
        self.assertFalse(state["whatsapp_verified"])
        self.assertTrue(state["discord_linked"])
        self.assertTrue(state["age_ok"])
        self.assertFalse(state["tier_lite"])

    def test_start_sends_a_code_through_the_2fa_method_and_verify_confirms(self):
        outbox.drain()
        r = self.client.post("/wagers/kyc/whatsapp/start/", **self.player_auth)
        self.assertEqual(r.status_code, 200, r.content)
        challenge_token = r.json()["challenge_token"]
        sent = outbox.drain()
        self.assertTrue(any(m["channel"] == "whatsapp" for m in sent), sent)
        # the code is not in the response; read it off the challenge the way the walk does
        from afc_auth.models import TwoFactorChallenge
        challenge = TwoFactorChallenge.objects.get(token=challenge_token)
        wrong = self.client.post("/wagers/kyc/whatsapp/verify/", {"challenge_token": challenge_token, "code": "000000"},
                                 content_type="application/json", **self.player_auth)
        self.assertEqual(wrong.status_code, 400)
        self.assertTrue(wrong.json()["code"].startswith("kyc_code_"))
        with patch("afc_auth.two_factor.WhatsAppCodeMethod.check_code", return_value=True):
            ok = self.client.post("/wagers/kyc/whatsapp/verify/", {"challenge_token": challenge_token, "code": "123456"},
                                  content_type="application/json", **self.player_auth)
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertTrue(ok.json()["tier_lite"])

    def test_a_changed_number_needs_a_new_confirmation(self):
        from afc_auth.models import canonical_profile
        services.kyc_force(self.player, by=self.admin, verified=True, reason="support")
        self.assertTrue(services.kyc_state(self.player)["tier_lite"])
        profile = canonical_profile(self.player)
        profile.whatsapp_number = "+2348099999999"
        profile.save()
        self.assertFalse(services.kyc_state(self.player)["whatsapp_verified"])

    def test_admin_force_and_unverify_need_a_reason(self):
        r = self.client.post(f"/wagers/admin/kyc/{self.player.username}/force/", {"verified": True, "reason": ""},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.json()["code"], "reason_required")
        r = self.client.post(f"/wagers/admin/kyc/{self.player.username}/force/", {"verified": True, "reason": "seen in person"},
                             content_type="application/json", **self.admin_auth)
        self.assertTrue(r.json()["kyc"]["whatsapp_verified"])
        rows = self.client.get("/wagers/admin/kyc/", **self.admin_auth).json()["results"]
        self.assertEqual(rows[0]["forced_by"], "wadmin")


class LimitsTests(WagerTestCase):

    def test_tightening_is_immediate_and_loosening_waits_24h(self):
        r = self.client.post("/wagers/limits/", {"daily_stake_cap_kobo": 500_000}, content_type="application/json", **self.player_auth)
        self.assertEqual(r.json()["limits"]["daily_stake_cap_kobo"], 500_000)
        r = self.client.post("/wagers/limits/", {"daily_stake_cap_kobo": 900_000}, content_type="application/json", **self.player_auth)
        lim = r.json()["limits"]
        self.assertEqual(lim["daily_stake_cap_kobo"], 500_000)
        self.assertEqual(lim["pending"]["daily_stake_cap_kobo"], 900_000)
        row = PlayerLimits.objects.get(user=self.player)
        row.pending_effective_at = timezone.now() - timedelta(minutes=1)
        row.save()
        _, caps = services.effective_limits(self.player)
        self.assertEqual(caps["daily_stake_cap_kobo"], 900_000)

    def test_cooloff_and_self_exclusion_bounds(self):
        r = self.client.post("/wagers/limits/cooloff/", {"days": 99}, content_type="application/json", **self.player_auth)
        self.assertEqual(r.json()["code"], "period_invalid")
        r = self.client.post("/wagers/limits/cooloff/", {"days": 2}, content_type="application/json", **self.player_auth)
        self.assertTrue(r.json()["limits"]["cooloff_until"])
        r = self.client.post("/wagers/limits/self-exclude/", {"months": 6}, content_type="application/json", **self.player_auth)
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/wagers/limits/self-exclude/", {"months": 1}, content_type="application/json", **self.player_auth)
        self.assertEqual(r.json()["code"], "exclusion_cannot_shorten")


class SettingsAndCreateTests(WagerTestCase):

    def test_settings_write_is_head_admin_only_and_named_fields(self):
        r = self.client.patch("/wagers/admin/settings/", {"rake_bps": 700}, content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 403)
        r = self.client.patch("/wagers/admin/settings/", {"rake_bps": 700, "nope": 1}, content_type="application/json", **self.head_auth)
        self.assertEqual(r.json()["code"], "unknown_fields")
        r = self.client.patch("/wagers/admin/settings/", {"rake_bps": 700, "wagering_enabled": False},
                              content_type="application/json", **self.head_auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((WagerSettings.get().rake_bps, WagerSettings.get().wagering_enabled), (700, False))
        pub = self.client.get("/wagers/settings/").json()
        self.assertFalse(pub["wagering_enabled"])

    def test_create_a_team_market_from_the_event_and_publish(self):
        lock = (timezone.now() + timedelta(hours=3)).isoformat()
        body = {"template": "match_winner", "event_id": self.event.pk, "match_id": self.match.pk, "title": "Who wins map 1",
                "lock_at": lock, "options": [{"team_id": self.tt_alpha.pk}, {"team_id": self.tt_bravo.pk}], "publish": True}
        r = self.client.post("/wagers/admin/markets/create/", body, content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 201, r.content)
        m = r.json()["market"]
        self.assertEqual((m["status"], m["slug"], [o["label"] for o in m["options"]]), ("OPEN", "who-wins-map-1", ["Alpha", "Bravo"]))
        self.assertEqual(m["rake_bps"], WagerSettings.get().rake_bps)
        # public list sees it
        lst = self.client.get("/wagers/markets/").json()
        self.assertIn("who-wins-map-1", [x["slug"] for x in lst["results"]])
        # a team from another event is refused; a match-needing template without a match is refused
        body["options"] = [{"team_id": 999999}, {"team_id": self.tt_bravo.pk}]
        r = self.client.post("/wagers/admin/markets/create/", body, content_type="application/json", **self.admin_auth)
        self.assertEqual(r.json()["code"], "options_invalid")
        body["options"] = [{"team_id": self.tt_alpha.pk}, {"team_id": self.tt_bravo.pk}]
        body.pop("match_id")
        r = self.client.post("/wagers/admin/markets/create/", body, content_type="application/json", **self.admin_auth)
        self.assertEqual(r.json()["code"], "match_required")

    def test_over_under_market_builds_its_two_sides_and_settles_from_total_kills(self):
        lock = (timezone.now() + timedelta(hours=3)).isoformat()
        body = {"template": "total_kills_over_under", "event_id": self.event.pk, "match_id": self.match.pk,
                "title": "Total kills", "lock_at": lock, "over_under_line": 12, "publish": True}
        r = self.client.post("/wagers/admin/markets/create/", body, content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 201, r.content)
        m = Market.objects.get(slug=r.json()["market"]["slug"])
        self.assertEqual([o.side for o in m.options.all()], ["over", "under"])
        services.lock_market(m)
        self.record_result(alpha_kills=10, bravo_kills=5)   # 15 > 12 -> over
        m = services.suggest_settlement(m)
        self.assertEqual(m.suggested_option.side, "over")

    def test_slug_history_answers_moved(self):
        m = self.make_market(title="First title")
        old = m.slug
        m.title = "Second title"
        m.save()
        r = self.client.get(f"/wagers/markets/{old}/").json()
        self.assertEqual((r["status"], r["slug"]), ("moved", m.slug))

    def test_edit_options_locked_once_staked(self):
        m = self.make_market()
        self.place_and_pay(self.player_auth, m, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        r = self.client.patch(f"/wagers/admin/markets/{m.slug}/", {"options": [{"label": "x"}, {"label": "y"}]},
                              content_type="application/json", **self.admin_auth)
        self.assertEqual(r.json()["code"], "options_locked")
        r = self.client.patch(f"/wagers/admin/markets/{m.slug}/", {"title": "Renamed"}, content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["market"]["slug"], "renamed")


class WebhookDelegateTests(WagerTestCase):

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_x")
    def test_the_shop_webhook_hands_a_wager_charge_to_afc_wager(self):
        market = self.make_market()
        resp = self.place(self.player_auth, market, [{"option_id": self.opt_a.id, "stake_kobo": 100_000}])
        ref = Wager.objects.get(public_token=resp.json()["wager"]["token"]).paystack_reference
        payload = json.dumps({"event": "charge.success",
                              "data": {"reference": ref, "amount": 100_000, "id": 7, "metadata": {"kind": "wager"}}}).encode()
        sig = hmac.new(b"sk_test_x", payload, hashlib.sha512).hexdigest()
        r = self.client.post("/shop/paystack-webhook/", payload, content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=sig)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Wager.objects.get(paystack_reference=ref).status, Wager.ACTIVE)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_x")
    def test_a_transfer_event_marks_the_withdrawal_paid(self):
        from .test_winnings import verify_kyc
        with patch("afc_wager.notify.adjustment_made"):
            services.adjust_winnings(self.player, direction="CREDIT", amount_kobo=1_000_000, reason="seed", by=self.admin)
        verify_kyc(self.player)
        bank = services.save_bank_account(self.player, bank_code="058", account_number="0123456789")
        wd = services.request_withdrawal(self.player, amount_kobo=300_000, bank_account=bank)
        with patch("afc_wager.payments.submit_transfer", return_value=(True, {"transfer_code": "TRF_1", "reference": wd.public_token, "status": "pending"})):
            services.approve_withdrawal(wd, by=self.admin)
        wd.refresh_from_db()
        self.assertEqual(wd.status, "APPROVED")
        payload = json.dumps({"event": "transfer.success", "data": {"reference": wd.public_token}}).encode()
        sig = hmac.new(b"sk_test_x", payload, hashlib.sha512).hexdigest()
        r = self.client.post("/shop/paystack-webhook/", payload, content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=sig)
        self.assertEqual(r.status_code, 200)
        wd.refresh_from_db()
        self.assertEqual(wd.status, "PAID")
        self.assertEqual(services.account_for(self.player).balance_kobo, 700_000)
