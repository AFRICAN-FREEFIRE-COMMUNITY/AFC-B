"""DRF serializers for afc_wallet.

Output shapes intentionally mirror `frontend/lib/mock-wager/types.ts` 1:1.
The frontend's IndexedDB mock and these serializers must produce identical
JSON so swapping `NEXT_PUBLIC_WAGER_MOCK=0` is a no-op for callers.
"""

from rest_framework import serializers

from .models import (
    AdminAuditLog,
    DepositIntent,
    FxSnapshot,
    KYCTier,
    Voucher,
    VoucherRedemption,
    Wallet,
    WalletTxn,
    WithdrawalRequest,
)


class FxSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = FxSnapshot
        fields = ["id", "captured_at", "ngn_per_usd", "source"]


class BalanceSerializer(serializers.Serializer):
    """Mirrors `interface Balance` in frontend types.ts."""

    total_kobo = serializers.IntegerField(source="balance_kobo")
    purchased_kobo = serializers.IntegerField(
        source="balance_purchased_kobo"
    )
    won_kobo = serializers.IntegerField(source="balance_won_kobo")
    gift_kobo = serializers.IntegerField(source="balance_gift_kobo")
    locked_kobo = serializers.IntegerField()
    fx = FxSnapshotSerializer(read_only=True)

    class Meta:
        model = Wallet


class WalletTxnSerializer(serializers.ModelSerializer):
    wallet_id = serializers.IntegerField(read_only=True)
    fx_snapshot_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = WalletTxn
        fields = [
            "id",
            "wallet_id",
            "amount_kobo",
            "kind",
            "source_tag",
            "ref_type",
            "ref_id",
            "fx_snapshot_id",
            "idempotency_key",
            "created_at",
        ]


class KYCStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = KYCTier
        fields = [
            "tier",
            "whatsapp_number",
            "whatsapp_verified_at",
            "discord_user_id",
            "discord_linked_at",
        ]


class DepositIntentSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(read_only=True)
    resulting_txn_id = serializers.IntegerField(
        source="resulting_txn", read_only=True, allow_null=True
    )

    class Meta:
        model = DepositIntent
        fields = [
            "id",
            "user_id",
            "rail",
            "amount_kobo",
            "provider_ref",
            "status",
            "resulting_txn_id",
            "created_at",
        ]


class VoucherSerializer(serializers.ModelSerializer):
    class Meta:
        model = Voucher
        fields = [
            "id",
            "code",
            "amount_kobo",
            "max_uses",
            "used_count",
            "expires_at",
        ]


class VoucherRedemptionSerializer(serializers.ModelSerializer):
    voucher_id = serializers.IntegerField(read_only=True)
    user_id = serializers.IntegerField(read_only=True)
    txn_id = serializers.IntegerField(source="txn", read_only=True)

    class Meta:
        model = VoucherRedemption
        fields = ["id", "voucher_id", "user_id", "redeemed_at", "txn_id"]


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(read_only=True)
    approved_by_admin_id = serializers.IntegerField(
        source="approved_by_admin", read_only=True, allow_null=True
    )

    class Meta:
        model = WithdrawalRequest
        fields = [
            "id",
            "user_id",
            "amount_kobo",
            "rail",
            "destination",
            "status",
            "approved_by_admin_id",
            "cosign_status",
            "created_at",
        ]


class AdminAuditLogSerializer(serializers.ModelSerializer):
    admin_user_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = AdminAuditLog
        fields = [
            "id",
            "admin_user_id",
            "action_kind",
            "target_type",
            "target_id",
            "payload",
            "ip",
            "ua",
            "signed_hmac",
            "created_at",
        ]


class P2PResultSerializer(serializers.Serializer):
    """Mirrors `interface P2PResult` in frontend types.ts."""

    transfer_id = serializers.CharField()
    amount_kobo = serializers.IntegerField()
    fee_kobo = serializers.IntegerField()
    recipient_username = serializers.CharField()
    receipt_txn_ids = serializers.ListField(child=serializers.IntegerField())
