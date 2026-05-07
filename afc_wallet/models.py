"""afc_wallet — wallet, ledger, KYC, FX, deposits, vouchers, withdrawals, audit.

All money is stored as integer kobo (1 NGN = 100 kobo, 1 coin = 50,000 kobo).
WalletTxn is an immutable ledger; balances on Wallet are caches recomputable
from txns. `idempotency_key` on WalletTxn is unique to make webhook retries
and double-submits safe.

Spec: WEBSITE/docs/superpowers/specs/2026-05-07-wager-feature-design.md Section 4.
"""

from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Enum-like CHOICES
# ---------------------------------------------------------------------------


class WalletStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    FROZEN = "FROZEN", "Frozen"


class SourceTag(models.TextChoices):
    PURCHASED = "PURCHASED", "Purchased"
    WON = "WON", "Won"
    GIFT = "GIFT", "Gift"


class WalletTxnKind(models.TextChoices):
    DEPOSIT_PAYSTACK = "DEPOSIT_PAYSTACK", "Deposit Paystack"
    DEPOSIT_STRIPE = "DEPOSIT_STRIPE", "Deposit Stripe"
    DEPOSIT_CRYPTO = "DEPOSIT_CRYPTO", "Deposit Crypto"
    DEPOSIT_VOUCHER = "DEPOSIT_VOUCHER", "Deposit Voucher"
    WAGER_PLACE = "WAGER_PLACE", "Wager Place"
    WAGER_REFUND = "WAGER_REFUND", "Wager Refund"
    WAGER_PAYOUT = "WAGER_PAYOUT", "Wager Payout"
    WAGER_CANCEL_FEE = "WAGER_CANCEL_FEE", "Wager Cancel Fee"
    HOUSE_RAKE = "HOUSE_RAKE", "House Rake"
    P2P_OUT = "P2P_OUT", "P2P Out"
    P2P_IN = "P2P_IN", "P2P In"
    P2P_FEE = "P2P_FEE", "P2P Fee"
    WITHDRAW_HOLD = "WITHDRAW_HOLD", "Withdraw Hold"
    WITHDRAW_REVERSAL = "WITHDRAW_REVERSAL", "Withdraw Reversal"
    REDEMPTION_DEBIT = "REDEMPTION_DEBIT", "Redemption Debit"
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"


class KYCTierLevel(models.TextChoices):
    TIER_0 = "TIER_0", "Tier 0"
    TIER_LITE = "TIER_LITE", "Tier Lite"


class DepositRail(models.TextChoices):
    PAYSTACK = "PAYSTACK", "Paystack"
    STRIPE = "STRIPE", "Stripe"
    CRYPTO = "CRYPTO", "Crypto"
    VOUCHER = "VOUCHER", "Voucher"


class DepositStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PAID = "PAID", "Paid"
    FAILED = "FAILED", "Failed"
    EXPIRED = "EXPIRED", "Expired"


class WithdrawRail(models.TextChoices):
    PAYSTACK_TRANSFER = "PAYSTACK_TRANSFER", "Paystack Transfer"
    CRYPTO_USDT = "CRYPTO_USDT", "Crypto USDT"


class WithdrawStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Requested"
    APPROVED = "APPROVED", "Approved"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


class CosignStatus(models.TextChoices):
    NOT_REQUIRED = "NOT_REQUIRED", "Not Required"
    AWAITING = "AWAITING", "Awaiting"
    APPROVED = "APPROVED", "Approved"


class AuditActionKind(models.TextChoices):
    MARKET_CREATE = "MARKET_CREATE", "Market Create"
    MARKET_LOCK = "MARKET_LOCK", "Market Lock"
    MARKET_VOID = "MARKET_VOID", "Market Void"
    MARKET_SETTLE_AUTO = "MARKET_SETTLE_AUTO", "Market Settle (Auto)"
    MARKET_SETTLE_OVERRIDE = (
        "MARKET_SETTLE_OVERRIDE",
        "Market Settle (Override)",
    )
    WALLET_FREEZE = "WALLET_FREEZE", "Wallet Freeze"
    WALLET_UNFREEZE = "WALLET_UNFREEZE", "Wallet Unfreeze"
    WALLET_ADJUSTMENT = "WALLET_ADJUSTMENT", "Wallet Adjustment"
    WITHDRAWAL_APPROVE = "WITHDRAWAL_APPROVE", "Withdrawal Approve"
    WITHDRAWAL_REJECT = "WITHDRAWAL_REJECT", "Withdrawal Reject"
    WITHDRAWAL_COSIGN = "WITHDRAWAL_COSIGN", "Withdrawal Cosign"
    VOUCHER_GENERATE = "VOUCHER_GENERATE", "Voucher Generate"
    VOUCHER_REVOKE = "VOUCHER_REVOKE", "Voucher Revoke"
    KYC_MANUAL_VERIFY = "KYC_MANUAL_VERIFY", "KYC Manual Verify"
    KYC_REVOKE = "KYC_REVOKE", "KYC Revoke"
    ROLE_ASSIGN = "ROLE_ASSIGN", "Role Assign"
    ROLE_REVOKE = "ROLE_REVOKE", "Role Revoke"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FxSnapshot(models.Model):
    """Captured NGN/USD rate at a point in time. Every WalletTxn references one."""

    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ngn_per_usd = models.DecimalField(max_digits=10, decimal_places=4)
    source = models.CharField(max_length=64, default="manual")

    class Meta:
        ordering = ["-captured_at"]

    def __str__(self):
        return f"FX@{self.captured_at.isoformat()} 1USD={self.ngn_per_usd}NGN"


class Wallet(models.Model):
    """Per-user wallet. Sum of WalletTxn.amount_kobo for this wallet equals
    balance_kobo. The four bucket caches let us read without aggregation."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallet",
    )
    balance_kobo = models.BigIntegerField(default=0)
    locked_kobo = models.BigIntegerField(
        default=0,
        help_text="Amount held in active wagers; subset of balance_kobo.",
    )
    balance_purchased_kobo = models.BigIntegerField(default=0)
    balance_won_kobo = models.BigIntegerField(default=0)
    balance_gift_kobo = models.BigIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=WalletStatus.choices,
        default=WalletStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet<{self.user_id} balance={self.balance_kobo}>"


class WalletTxn(models.Model):
    """Immutable ledger row. Every credit/debit produces exactly one row.

    `idempotency_key` is unique — the same key returns the existing row instead
    of double-applying. Webhook retries, double-clicks, and replay attacks are
    all neutralized by this single constraint.
    """

    wallet = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, related_name="txns"
    )
    amount_kobo = models.BigIntegerField(
        help_text="Signed: positive=credit, negative=debit."
    )
    kind = models.CharField(max_length=32, choices=WalletTxnKind.choices)
    source_tag = models.CharField(
        max_length=16,
        choices=SourceTag.choices,
        help_text="Origin bucket — drives spend ladder + gift-cap accounting.",
    )
    ref_type = models.CharField(max_length=64, blank=True, default="")
    ref_id = models.CharField(max_length=128, blank=True, default="")
    fx_snapshot = models.ForeignKey(
        FxSnapshot, on_delete=models.PROTECT, related_name="+"
    )
    idempotency_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["wallet", "-created_at"]),
            models.Index(fields=["wallet", "kind"]),
            models.Index(fields=["wallet", "source_tag"]),
        ]

    def __str__(self):
        return (
            f"Txn<{self.kind} {self.amount_kobo} wallet={self.wallet_id}>"
        )


class KYCTier(models.Model):
    """Per-user KYC-Lite status. Auto-promoted to TIER_LITE when both
    whatsapp_verified_at and discord_linked_at are non-null."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="kyc_tier",
    )
    tier = models.CharField(
        max_length=16,
        choices=KYCTierLevel.choices,
        default=KYCTierLevel.TIER_0,
    )
    # Authoritative copies — UserProfile mirrors them for the auth app's use.
    whatsapp_number = models.CharField(
        max_length=24, unique=True, null=True, blank=True
    )
    whatsapp_verified_at = models.DateTimeField(null=True, blank=True)
    discord_user_id = models.CharField(
        max_length=64, unique=True, null=True, blank=True
    )
    discord_linked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"KYC<{self.user_id} tier={self.tier}>"


class DepositIntent(models.Model):
    """Pre-payment intent. On webhook, status flips PENDING -> PAID and
    `resulting_txn` links the credited WalletTxn."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="deposit_intents",
    )
    rail = models.CharField(max_length=16, choices=DepositRail.choices)
    amount_kobo = models.BigIntegerField()
    provider_ref = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=DepositStatus.choices,
        default=DepositStatus.PENDING,
    )
    resulting_txn = models.ForeignKey(
        WalletTxn,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["provider_ref"]),
        ]

    def __str__(self):
        return f"DepositIntent<{self.id} {self.rail} {self.amount_kobo}>"


class Voucher(models.Model):
    """Pre-generated promo code. `used_count` increments atomically on redeem.

    Codes are stored uppercase; lookup is case-insensitive at the service
    boundary (we uppercase + trim before query)."""

    code = models.CharField(max_length=32, unique=True)
    amount_kobo = models.BigIntegerField()
    max_uses = models.PositiveIntegerField(default=1)
    used_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vouchers_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["expires_at"])]

    def __str__(self):
        return f"Voucher<{self.code} {self.amount_kobo}kobo {self.used_count}/{self.max_uses}>"


class VoucherRedemption(models.Model):
    """Junction — at most one redemption per (voucher, user)."""

    voucher = models.ForeignKey(
        Voucher, on_delete=models.PROTECT, related_name="redemptions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="voucher_redemptions",
    )
    redeemed_at = models.DateTimeField(auto_now_add=True)
    txn = models.ForeignKey(
        WalletTxn, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["voucher", "user"],
                name="uniq_voucher_redemption_per_user",
            )
        ]

    def __str__(self):
        return f"VoucherRedemption<{self.voucher_id}->{self.user_id}>"


class WithdrawalRequest(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="withdrawal_requests",
    )
    amount_kobo = models.BigIntegerField()
    rail = models.CharField(max_length=24, choices=WithdrawRail.choices)
    destination = models.JSONField(
        help_text="Bank account or crypto wallet — keys vary by rail."
    )
    status = models.CharField(
        max_length=16,
        choices=WithdrawStatus.choices,
        default=WithdrawStatus.REQUESTED,
    )
    approved_by_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="withdrawals_approved",
    )
    cosign_status = models.CharField(
        max_length=16,
        choices=CosignStatus.choices,
        default=CosignStatus.NOT_REQUIRED,
    )
    hold_txn = models.ForeignKey(
        WalletTxn,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The WITHDRAW_HOLD debit; reversed on reject.",
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self):
        return f"Withdrawal<{self.id} {self.status} {self.amount_kobo}>"


class AdminAuditLog(models.Model):
    """Tamper-evident log. signed_hmac is a HMAC-SHA256 over the canonical
    serialization of the row's identifying fields. Any post-write edit breaks
    the hash and is detectable by re-signing during audit replay."""

    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_log_entries",
    )
    action_kind = models.CharField(
        max_length=48, choices=AuditActionKind.choices
    )
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=128)
    payload = models.JSONField(default=dict, blank=True)
    ip = models.CharField(max_length=64, blank=True, default="")
    ua = models.TextField(blank=True, default="")
    signed_hmac = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["admin_user", "-created_at"]),
            models.Index(fields=["action_kind", "-created_at"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    def __str__(self):
        return (
            f"Audit<{self.admin_user_id} {self.action_kind} {self.target_id}>"
        )
