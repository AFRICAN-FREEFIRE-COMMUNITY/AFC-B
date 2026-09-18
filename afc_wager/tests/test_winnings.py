"""Winnings: the ledger and balance-after, withdrawals (KYC gate, minimum, hold, approve, reject,
co-sign by a different admin, cancel by the player), adjustments (two-key), freeze."""
from unittest.mock import patch

from django.utils import timezone

from afc_wager import services
from afc_wager.models import Adjustment, KycStatus, LedgerEntry, PayoutBankAccount, Withdrawal, WagerSettings
from afc_wager.services import WagerError

from .fixtures import WagerTestCase


def verify_kyc(user):
    """Both facts true: the profile number confirmed and Discord linked (the fixture sets a
    discord_id and a number; this stamps the confirmation)."""
    from afc_auth.models import canonical_profile
    row, _ = KycStatus.objects.get_or_create(user=user)
    row.whatsapp_verified_at = timezone.now()
    row.whatsapp_verified_number = canonical_profile(user).whatsapp_number
    row.save()


class WinningsTests(WagerTestCase):

    def setUp(self):
        super().setUp()
        with patch("afc_wager.notify.adjustment_made"):
            services.adjust_winnings(self.player, direction="CREDIT", amount_kobo=1_000_000, reason="seed", by=self.admin)

    def test_balance_after_is_the_ledger_balance_not_a_filtered_sum(self):
        services.adjust_winnings(self.player, direction="DEBIT", amount_kobo=200_000, reason="test", by=self.admin)
        rows = self.client.get("/wagers/winnings/ledger/?kind=ADJUSTMENT_DEBIT", **self.player_auth).json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["balance_after_kobo"], 800_000)

    def test_winnings_page_is_the_owners_and_signed_out_is_401(self):
        r = self.client.get("/wagers/winnings/")
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/wagers/winnings/", **self.player_auth).json()
        self.assertEqual(r["account"]["balance_kobo"], 1_000_000)
        r = self.client.get("/wagers/winnings/", **self.other_auth).json()
        self.assertEqual(r["account"]["balance_kobo"], 0)

    def test_withdrawal_needs_kyc_then_a_bank_account_then_holds_the_amount(self):
        r = self.client.post("/wagers/winnings/withdraw/", {"amount_kobo": 300_000, "bank_account_id": 0},
                             content_type="application/json", **self.player_auth)
        self.assertEqual(r.json()["code"], "bank_account_required")
        bank = self.client.post("/wagers/winnings/bank-accounts/", {"bank_code": "058", "account_number": "0123456789"},
                                content_type="application/json", **self.player_auth)
        self.assertEqual(bank.status_code, 201, bank.content)
        bank_id = bank.json()["bank_account"]["id"]
        self.assertEqual(bank.json()["bank_account"]["account_name"], "MOCK ACCOUNT HOLDER")
        r = self.client.post("/wagers/winnings/withdraw/", {"amount_kobo": 300_000, "bank_account_id": bank_id},
                             content_type="application/json", **self.player_auth)
        self.assertEqual((r.status_code, r.json()["code"]), (403, "kyc_required"))
        verify_kyc(self.player)
        r = self.client.post("/wagers/winnings/withdraw/", {"amount_kobo": 1_000, "bank_account_id": bank_id},
                             content_type="application/json", **self.player_auth)
        self.assertEqual(r.json()["code"], "below_minimum_withdrawal")
        r = self.client.post("/wagers/winnings/withdraw/", {"amount_kobo": 300_000, "bank_account_id": bank_id},
                             content_type="application/json", **self.player_auth)
        self.assertEqual(r.status_code, 201, r.content)
        account = services.account_for(self.player)
        self.assertEqual((account.balance_kobo, account.held_kobo, account.available_kobo), (1_000_000, 300_000, 700_000))
        # a second one while the first is open
        r = self.client.post("/wagers/winnings/withdraw/", {"amount_kobo": 300_000, "bank_account_id": bank_id},
                             content_type="application/json", **self.player_auth)
        self.assertEqual(r.json()["code"], "withdrawal_in_progress")
        # more than available
        token = Withdrawal.objects.get(user=self.player).public_token
        self.client.post(f"/wagers/winnings/withdrawals/{token}/cancel/", **self.player_auth)
        r = self.client.post("/wagers/winnings/withdraw/", {"amount_kobo": 2_000_000, "bank_account_id": bank_id},
                             content_type="application/json", **self.player_auth)
        self.assertEqual(r.json()["code"], "insufficient_winnings")

    def _withdrawal(self, amount=300_000):
        verify_kyc(self.player)
        bank = services.save_bank_account(self.player, bank_code="058", account_number="0123456789")
        return services.request_withdrawal(self.player, amount_kobo=amount, bank_account=bank)

    def test_player_cancels_a_requested_withdrawal_and_the_hold_is_released(self):
        wd = self._withdrawal()
        r = self.client.post(f"/wagers/winnings/withdrawals/{wd.public_token}/cancel/", **self.player_auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(services.account_for(self.player).held_kobo, 0)
        r = self.client.post(f"/wagers/winnings/withdrawals/{wd.public_token}/cancel/", **self.other_auth)
        self.assertEqual(r.status_code, 404)

    def test_admin_approves_and_the_money_leaves_on_transfer_success(self):
        wd = self._withdrawal()
        r = self.client.post(f"/wagers/admin/withdrawals/{wd.public_token}/approve/", **self.admin_auth)
        self.assertEqual(r.status_code, 200, r.content)
        wd.refresh_from_db()
        # outbox transfer answers "success" at once -> PAID
        self.assertEqual(wd.status, Withdrawal.PAID)
        account = services.account_for(self.player)
        self.assertEqual((account.balance_kobo, account.held_kobo), (700_000, 0))
        self.assertEqual(LedgerEntry.objects.get(kind=LedgerEntry.WITHDRAWAL_PAID).amount_kobo, -300_000)

    def test_admin_rejects_with_a_reason_and_the_hold_is_released(self):
        wd = self._withdrawal()
        r = self.client.post(f"/wagers/admin/withdrawals/{wd.public_token}/reject/", {"reason": ""},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.json()["code"], "reason_required")
        r = self.client.post(f"/wagers/admin/withdrawals/{wd.public_token}/reject/", {"reason": "Name mismatch"},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 200)
        account = services.account_for(self.player)
        self.assertEqual((account.balance_kobo, account.held_kobo), (1_000_000, 0))
        self.assertEqual(Withdrawal.objects.get(pk=wd.pk).reject_reason, "Name mismatch")

    def test_large_withdrawal_needs_two_different_admins(self):
        with patch("afc_wager.notify.adjustment_made"):
            services.adjust_winnings(self.player, direction="CREDIT", amount_kobo=10_000_000_00, reason="big", by=self.head)
        # the credit itself was over the threshold, so it is pending co-sign, not executed
        adj = Adjustment.objects.get(reason="big")
        self.assertEqual(adj.status, Adjustment.PENDING_COSIGN)
        services.cosign_adjustment(adj, by=self.admin, approve=True)
        wd = self._withdrawal(amount=600_000_000)
        self.assertEqual(wd.status, Withdrawal.PENDING_COSIGN)
        services.approve_withdrawal(wd, by=self.admin)      # first key
        wd.refresh_from_db()
        self.assertEqual(wd.status, Withdrawal.PENDING_COSIGN)
        with self.assertRaises(WagerError) as ctx:
            services.approve_withdrawal(wd, by=self.admin)  # same admin again
        self.assertEqual(ctx.exception.code, "cosign_same_admin")
        services.approve_withdrawal(wd, by=self.head)       # second key
        wd.refresh_from_db()
        self.assertEqual(wd.status, Withdrawal.PAID)

    def test_adjustment_cosign_refuses_the_submitter(self):
        adj = services.adjust_winnings(self.player, direction="CREDIT", amount_kobo=600_000_000, reason="comp", by=self.admin)
        self.assertEqual(adj.status, Adjustment.PENDING_COSIGN)
        r = self.client.post(f"/wagers/admin/adjustments/{adj.pk}/cosign/", {"approve": True},
                             content_type="application/json", **self.admin_auth)
        self.assertEqual(r.status_code, 403)    # finance_admin is not head_admin
        with self.assertRaises(WagerError) as ctx:
            services.cosign_adjustment(adj, by=self.admin, approve=True)
        self.assertEqual(ctx.exception.code, "cosign_same_admin")
        r = self.client.post(f"/wagers/admin/adjustments/{adj.pk}/cosign/", {"approve": True},
                             content_type="application/json", **self.head_auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(services.account_for(self.player).balance_kobo, 601_000_000)

    def test_debit_cannot_overdraw(self):
        with self.assertRaises(WagerError) as ctx:
            services.adjust_winnings(self.player, direction="DEBIT", amount_kobo=5_000_000, reason="oops", by=self.admin)
        self.assertEqual(ctx.exception.code, "insufficient_winnings")
        self.assertEqual(services.account_for(self.player).balance_kobo, 1_000_000)

    def test_frozen_account_cannot_withdraw(self):
        services.set_frozen(self.player, frozen=True, reason="chargeback review", by=self.admin)
        with self.assertRaises(WagerError) as ctx:
            self._withdrawal()
        self.assertEqual(ctx.exception.code, "winnings_frozen")
        r = self.client.get("/wagers/winnings/", **self.player_auth).json()
        self.assertEqual(r["account"]["frozen_reason"], "chargeback review")

    def test_finance_gate(self):
        for auth in (self.plain_auth, self.player_auth):
            r = self.client.get("/wagers/admin/users/", **auth)
            self.assertEqual((r.status_code, r.json()["code"]), (403, "finance_admin_required"))
        r = self.client.get("/wagers/admin/users/", **self.admin_auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"][0]["username"], "wplayer")
