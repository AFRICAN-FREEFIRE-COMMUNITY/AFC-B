"""afc_wager — Market, MarketOption, MarketTemplate, Wager, WagerLine,
Settlement, Payout, RakeTxn.

The parimutuel wager engine. Money for stakes lives on `afc_wallet.WalletTxn`;
this app owns the *contracts* (markets) and the *positions* (wager lines).

Spec: WEBSITE/docs/superpowers/specs/2026-05-07-wager-feature-design.md
Section 4 (data model), Section 5 (settlement math), Section 7 (lifecycle).
"""

from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Enum-like CHOICES
# ---------------------------------------------------------------------------


class OptionSource(models.TextChoices):
    TEAMS = "TEAMS", "Teams"
    PLAYERS = "PLAYERS", "Players"
    NUMERIC = "NUMERIC", "Numeric"
    FREEFORM = "FREEFORM", "Freeform"


class MarketStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    OPEN = "OPEN", "Open"
    LOCKED = "LOCKED", "Locked"
    PENDING_SETTLEMENT = "PENDING_SETTLEMENT", "Pending Settlement"
    SETTLED = "SETTLED", "Settled"
    VOIDED = "VOIDED", "Voided"


class WagerStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    SETTLED = "SETTLED", "Settled"
    VOIDED = "VOIDED", "Voided"
    CANCELLED = "CANCELLED", "Cancelled"


class WagerLineOutcome(models.TextChoices):
    WIN = "WIN", "Win"
    LOSS = "LOSS", "Loss"
    VOID = "VOID", "Void"


class SettlementResolution(models.TextChoices):
    WINNER = "WINNER", "Winner"
    VOID_NO_WINNER = "VOID_NO_WINNER", "Void (No Winner)"
    VOID_ADMIN = "VOID_ADMIN", "Void (Admin)"
    VOID_SOLO_WAGER = "VOID_SOLO_WAGER", "Void (Solo Wager)"


# ---------------------------------------------------------------------------
# MarketTemplate — seed data, ~9 rows
# ---------------------------------------------------------------------------


class MarketTemplate(models.Model):
    """Pre-defined market types. `code` is the stable enum string used in
    `Market.template_id` lookups. `grader_key` selects the auto-grader
    function in `afc_wager.adapters.stats_reader`."""

    code = models.CharField(max_length=32, unique=True)
    display_name = models.CharField(max_length=80)
    option_source = models.CharField(
        max_length=16, choices=OptionSource.choices
    )
    auto_gradable = models.BooleanField(default=False)
    grader_key = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        help_text="Key into stats_reader.GRADERS; null for `custom`.",
    )

    def __str__(self):
        return f"MarketTemplate<{self.code}>"


class Market(models.Model):
    """A betting market. Created by a wager_admin in DRAFT, published to
    OPEN, locks at lock_at, transitions to PENDING_SETTLEMENT when the
    underlying match resolves, then SETTLED or VOIDED.

    `total_pool_kobo` and `total_lines` are caches; the source of truth is
    `sum(WagerLine.stake_kobo where wager.market = self)`. Spec invariants
    in Section 5 enforce parity at every settle.
    """

    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event",
        on_delete=models.PROTECT,
        related_name="markets",
    )
    match = models.ForeignKey(
        "afc_tournament_and_scrims.Match",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="markets",
    )
    template = models.ForeignKey(
        MarketTemplate, on_delete=models.PROTECT, related_name="markets"
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=24,
        choices=MarketStatus.choices,
        default=MarketStatus.DRAFT,
    )
    opens_at = models.DateTimeField()
    lock_at = models.DateTimeField()

    # Money rules (frozen at create-time per spec Section 4 / Decision 6).
    min_stake_kobo = models.BigIntegerField(default=10_000)
    max_per_user_kobo = models.BigIntegerField(null=True, blank=True)
    cancel_fee_bps = models.PositiveIntegerField(default=100)
    rake_bps = models.PositiveIntegerField(default=500)

    suggested_option = models.ForeignKey(
        "MarketOption",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    winning_option = models.ForeignKey(
        "MarketOption",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    total_pool_kobo = models.BigIntegerField(default=0)
    total_lines = models.PositiveIntegerField(default=0)

    created_by_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="markets_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "lock_at"]),
            models.Index(fields=["event", "status"]),
        ]

    def __str__(self):
        return f"Market<{self.pk} {self.title} {self.status}>"


class MarketOption(models.Model):
    """A possible outcome of a market. Cached pool/wager counts.

    `ref_team_id` / `ref_player_id` / `ref_numeric` link the option back to
    the underlying tournament data so the auto-grader can resolve it.
    """

    market = models.ForeignKey(
        Market, on_delete=models.CASCADE, related_name="options"
    )
    label = models.CharField(max_length=255)

    ref_team_id = models.IntegerField(null=True, blank=True)
    ref_player_id = models.IntegerField(null=True, blank=True)
    ref_numeric = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True
    )
    image = models.URLField(blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)

    cached_pool_kobo = models.BigIntegerField(default=0)
    cached_wager_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["market", "sort_order"]

    def __str__(self):
        return f"MarketOption<{self.pk} {self.label}>"


class Wager(models.Model):
    """A user's wager on a market. ONE per (user, market) per Decision 12 —
    multiple options become multiple `WagerLine` rows under the same Wager.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wagers",
    )
    market = models.ForeignKey(
        Market, on_delete=models.PROTECT, related_name="wagers"
    )
    total_stake_kobo = models.BigIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=WagerStatus.choices,
        default=WagerStatus.ACTIVE,
    )
    placed_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    debit_txn_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="afc_wallet.WalletTxn.id of the WAGER_PLACE debit",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "market"],
                name="uniq_wager_per_user_market",
            )
        ]
        indexes = [
            models.Index(fields=["user", "-placed_at"]),
            models.Index(fields=["market", "status"]),
        ]

    def __str__(self):
        return f"Wager<{self.pk} u={self.user_id} m={self.market_id}>"


class WagerLine(models.Model):
    """A single (option, stake) tuple within a Wager. unique(wager, option)
    so a user can't double-stake the same option."""

    wager = models.ForeignKey(
        Wager, on_delete=models.CASCADE, related_name="lines"
    )
    option = models.ForeignKey(
        MarketOption, on_delete=models.PROTECT, related_name="lines"
    )
    stake_kobo = models.BigIntegerField()
    payout_kobo = models.BigIntegerField(null=True, blank=True)
    outcome = models.CharField(
        max_length=8,
        choices=WagerLineOutcome.choices,
        null=True,
        blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["wager", "option"], name="uniq_line_per_wager_option"
            )
        ]

    def __str__(self):
        return (
            f"WagerLine<{self.pk} w={self.wager_id} o={self.option_id} "
            f"{self.stake_kobo}>"
        )


class Settlement(models.Model):
    """Settlement record — one per market. Persists the (suggested, final)
    pair plus the override_reason if admin overrode the auto-suggestion."""

    market = models.OneToOneField(
        Market, on_delete=models.PROTECT, related_name="settlement"
    )
    suggested_option = models.ForeignKey(
        MarketOption,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    final_option = models.ForeignKey(
        MarketOption,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    resolution = models.CharField(
        max_length=24, choices=SettlementResolution.choices
    )
    override_reason = models.TextField(blank=True, default="")
    total_pool_kobo = models.BigIntegerField(default=0)
    rake_kobo = models.BigIntegerField(default=0)
    paid_out_kobo = models.BigIntegerField(default=0)
    winners_count = models.PositiveIntegerField(default=0)
    lines_count = models.PositiveIntegerField(default=0)
    confirmed_by_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="settlements_confirmed",
    )
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["resolution", "-confirmed_at"])]

    def __str__(self):
        return f"Settlement<{self.pk} m={self.market_id} {self.resolution}>"


class Payout(models.Model):
    """A single user's payout from a settlement. unique on wager_line so the
    same line can't be double-paid by a re-run of settle()."""

    settlement = models.ForeignKey(
        Settlement, on_delete=models.PROTECT, related_name="payouts"
    )
    wager_line = models.OneToOneField(
        WagerLine, on_delete=models.PROTECT, related_name="payout"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payouts_received",
    )
    amount_kobo = models.BigIntegerField()
    credit_txn_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="afc_wallet.WalletTxn.id of the WAGER_PAYOUT credit",
    )

    class Meta:
        indexes = [models.Index(fields=["user", "-id"])]

    def __str__(self):
        return f"Payout<{self.pk} u={self.user_id} {self.amount_kobo}>"


class RakeTxn(models.Model):
    """House rake for a settled market. unique per settlement."""

    settlement = models.OneToOneField(
        Settlement, on_delete=models.PROTECT, related_name="rake"
    )
    amount_kobo = models.BigIntegerField()
    credit_txn_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="afc_wallet.WalletTxn.id of the HOUSE_RAKE credit",
    )

    def __str__(self):
        return f"RakeTxn<{self.pk} s={self.settlement_id} {self.amount_kobo}>"
