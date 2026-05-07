"""DRF serializers for afc_wager. Output mirrors frontend types.ts 1:1."""

from rest_framework import serializers

from .models import (
    Market,
    MarketOption,
    MarketTemplate,
    Payout,
    RakeTxn,
    Settlement,
    Wager,
    WagerLine,
)


class MarketTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketTemplate
        fields = [
            "code",
            "display_name",
            "option_source",
            "auto_gradable",
            "grader_key",
        ]


class MarketOptionSerializer(serializers.ModelSerializer):
    market_id = serializers.IntegerField(read_only=True)
    ref_team_id = serializers.IntegerField(allow_null=True)
    ref_player_id = serializers.IntegerField(allow_null=True)
    ref_numeric = serializers.DecimalField(
        max_digits=20, decimal_places=4, allow_null=True
    )

    class Meta:
        model = MarketOption
        fields = [
            "id",
            "market_id",
            "label",
            "ref_team_id",
            "ref_player_id",
            "ref_numeric",
            "image",
            "sort_order",
            "cached_pool_kobo",
            "cached_wager_count",
        ]


class MarketSerializer(serializers.ModelSerializer):
    event_id = serializers.IntegerField(read_only=True)
    match_id = serializers.IntegerField(read_only=True, allow_null=True)
    template_code = serializers.CharField(
        source="template.code", read_only=True
    )
    suggested_option_id = serializers.IntegerField(
        source="suggested_option_id", read_only=True, allow_null=True
    )
    winning_option_id = serializers.IntegerField(
        source="winning_option_id", read_only=True, allow_null=True
    )
    created_by_admin_id = serializers.IntegerField(read_only=True)
    options = MarketOptionSerializer(many=True, read_only=True)

    class Meta:
        model = Market
        fields = [
            "id",
            "event_id",
            "match_id",
            "template_code",
            "title",
            "description",
            "status",
            "opens_at",
            "lock_at",
            "min_stake_kobo",
            "max_per_user_kobo",
            "cancel_fee_bps",
            "rake_bps",
            "suggested_option_id",
            "winning_option_id",
            "total_pool_kobo",
            "total_lines",
            "options",
            "created_by_admin_id",
        ]


class WagerLineSerializer(serializers.ModelSerializer):
    wager_id = serializers.IntegerField(read_only=True)
    option_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = WagerLine
        fields = [
            "id",
            "wager_id",
            "option_id",
            "stake_kobo",
            "payout_kobo",
            "outcome",
        ]


class WagerSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(read_only=True)
    market_id = serializers.IntegerField(read_only=True)
    debit_txn_id = serializers.IntegerField(allow_null=True)
    lines = WagerLineSerializer(many=True, read_only=True)

    class Meta:
        model = Wager
        fields = [
            "id",
            "user_id",
            "market_id",
            "total_stake_kobo",
            "status",
            "placed_at",
            "cancelled_at",
            "debit_txn_id",
            "lines",
        ]


class SettlementSerializer(serializers.ModelSerializer):
    market_id = serializers.IntegerField(read_only=True)
    suggested_option_id = serializers.IntegerField(
        read_only=True, allow_null=True
    )
    final_option_id = serializers.IntegerField(
        read_only=True, allow_null=True
    )
    confirmed_by_admin_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Settlement
        fields = [
            "id",
            "market_id",
            "suggested_option_id",
            "final_option_id",
            "resolution",
            "override_reason",
            "total_pool_kobo",
            "rake_kobo",
            "paid_out_kobo",
            "winners_count",
            "lines_count",
            "confirmed_by_admin_id",
            "confirmed_at",
        ]


class PayoutSerializer(serializers.ModelSerializer):
    settlement_id = serializers.IntegerField(read_only=True)
    wager_line_id = serializers.IntegerField(read_only=True)
    user_id = serializers.IntegerField(read_only=True)
    credit_txn_id = serializers.IntegerField(allow_null=True)

    class Meta:
        model = Payout
        fields = [
            "id",
            "settlement_id",
            "wager_line_id",
            "user_id",
            "amount_kobo",
            "credit_txn_id",
        ]


class RakeTxnSerializer(serializers.ModelSerializer):
    settlement_id = serializers.IntegerField(read_only=True)
    credit_txn_id = serializers.IntegerField(allow_null=True)

    class Meta:
        model = RakeTxn
        fields = ["id", "settlement_id", "amount_kobo", "credit_txn_id"]
