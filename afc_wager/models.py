"""
afc_wager.models - pari-mutuel wagers on AFC matches, paid in naira, no coins.

WHY THIS APP EXISTS, AND WHY THERE ARE NO COINS
    The May 2026 `feature/wager` branch built a coin economy: a wallet with three buckets, top-ups
    by card, P2P sends, vouchers, and wagers spent from the wallet. The owner walked it on
    2026-09-18 and decided (inbox #33): "remove coins for now ... no wallets to deposit into, they
    just wager the cash directly". So:

      * A STAKE IS PAID AT PLACEMENT. Place -> a Wager in PENDING_PAYMENT plus a Paystack
        transaction for exactly the stake -> the player pays -> charge.success (webhook, or the
        server-side verify when they come back) makes it ACTIVE and adds it to the pool. Unpaid
        after WagerSettings.payment_expiry_minutes it EXPIRES and never touches the pool.
      * WINNINGS ARE WITHDRAW-ONLY. Payouts, void refunds and cancel refunds land in the player's
        WinningsAccount. Nothing can be paid INTO it, so it is not a wallet in the sense the owner
        rejected: it holds what AFC owes the player until they take it to their bank.
      * The house keeps the rake, the cancel fees and the rounding dust, on its own ledger lines.

THE SHAPE
    WagerSettings      one row: rake, fees, minimums, the kill switch, the co-sign threshold.
    MarketTemplate     what kind of question a market asks and how it settles itself.
    Market ──┬── MarketOption   (the answers; a team or a player of the event when the template
             │                   says so, free labels otherwise)
             ├── Wager ── WagerLine   (one player's stake, split over options, paid once)
             └── Settlement ── Payout (what the engine decided, and who was paid)
    WinningsAccount ── LedgerEntry     (every kobo in or out, with the balance after)
    Withdrawal, Adjustment            (money leaving, and admin corrections; both two-key above
                                       the threshold)
    KycStatus, PlayerLimits, PayoutBankAccount  (who may withdraw, how much they may stake, where
                                       the money goes)

HOW IT CONNECTS
    - Markets hang off afc_tournament_and_scrims.Event (always) and Stages / Match (when the
      template settles from match stats). Options point at TournamentTeam or a User so the
      suggestion can read TournamentTeamMatchStats / TournamentPlayerMatchStats.
    - Money moves through afc_wager/services.py only; views never touch balances.
    - Paystack: afc_wager/payments.py (stakes) and afc_shop.paystack_payout._paystack (transfers).
    - Identity for KYC: UserProfile.whatsapp_number (confirmed by afc_auth.two_factor, purpose
      wager_kyc), User.discord_id, UserProfile.date_of_birth.
    - Slugs and tokens: afc_auth.slugs (sync_slug + SlugHistory for markets, public tokens for
      wagers and withdrawals), so no numeric id ever reaches a URL (R22).
    - Audit: every admin write calls afc_auth.audit.set_audit; the AuditLogMiddleware records it.
    - Frontend: app/(user)/wagers, app/(user)/winnings, app/(a)/a/wagers, app/(a)/a/winnings.

AMOUNTS
    Every amount is an integer in KOBO. Percentages are basis points (500 = 5%). Never a float.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from afc_auth.slugs import ensure_public_token, sync_slug


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1 Settings: one row the admin edits, read by every guard
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class WagerSettings(models.Model):
    """The dials. One row (pk=1), created on first read by `WagerSettings.get()`.

    Read by services.place_wager (the guards), services.cancel_wager (the fee), the settlement
    (the rake), the withdrawal flow (minimum and co-sign threshold) and the frontend (shown on
    the place sheet). Written by views_admin.update_settings, head_admin only, audit logged."""

    # Off = nobody can place or cancel. Settled and voided markets still pay out. The banner is
    # what players read while it is off.
    wagering_enabled = models.BooleanField(default=True)
    maintenance_message = models.CharField(max_length=240, blank=True, default="")

    rake_bps = models.PositiveIntegerField(default=500)          # 5% of the pool to the house
    cancel_fee_bps = models.PositiveIntegerField(default=100)    # 1% of the stake on a pre-lock cancel
    min_stake_kobo = models.BigIntegerField(default=50_000)      # ₦500
    max_stake_per_user_kobo = models.BigIntegerField(default=50_000_000)   # ₦500,000 per market
    max_pool_kobo = models.BigIntegerField(default=0)            # 0 = no cap
    payment_expiry_minutes = models.PositiveIntegerField(default=15)

    min_withdrawal_kobo = models.BigIntegerField(default=250_000)          # ₦2,500
    cosign_threshold_kobo = models.BigIntegerField(default=500_000_000)     # ₦5,000,000
    min_age = models.PositiveIntegerField(default=18)

    # Default responsible-gaming caps applied to a player who never set their own. 0 = none.
    default_daily_stake_cap_kobo = models.BigIntegerField(default=0)
    default_weekly_stake_cap_kobo = models.BigIntegerField(default=0)
    default_daily_loss_cap_kobo = models.BigIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    EDITABLE_FIELDS = (
        "wagering_enabled", "maintenance_message", "rake_bps", "cancel_fee_bps", "min_stake_kobo",
        "max_stake_per_user_kobo", "max_pool_kobo", "payment_expiry_minutes", "min_withdrawal_kobo",
        "cosign_threshold_kobo", "min_age", "default_daily_stake_cap_kobo",
        "default_weekly_stake_cap_kobo", "default_daily_loss_cap_kobo",
    )

    @classmethod
    def get(cls):
        row, _ = cls.objects.get_or_create(pk=1)
        return row

    def __str__(self):
        return "Wager settings"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2 Templates: what a market asks, where its options come from, how it settles itself
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class MarketTemplate(models.Model):
    """A kind of question. The template decides two things the admin never has to type twice:
    where the options come from (the teams of a match, the players of a match, custom labels, or
    the two sides of an over / under line) and how the suggestion is computed from the match
    stats when results land. Seeded by `manage.py seed_wager_templates`; editable in the CMS."""

    OPTIONS_TEAMS = "teams_in_match"
    OPTIONS_PLAYERS = "players_in_match"
    OPTIONS_CUSTOM = "custom"
    OPTIONS_OVER_UNDER = "over_under"
    OPTION_SOURCES = [
        (OPTIONS_TEAMS, "Teams in the match"),
        (OPTIONS_PLAYERS, "Players in the match"),
        (OPTIONS_CUSTOM, "Custom labels"),
        (OPTIONS_OVER_UNDER, "Over / under a line"),
    ]

    SETTLE_TEAM_PLACEMENT_1 = "team_placement_1"     # the team placed first in the match
    SETTLE_TEAM_MOST_KILLS = "team_most_kills"
    SETTLE_PLAYER_MOST_KILLS = "player_most_kills"
    SETTLE_MATCH_MVP = "match_mvp"                    # Match.mvp
    SETTLE_TOTAL_KILLS_OVER_UNDER = "total_kills_over_under"
    SETTLE_MANUAL = "manual"                          # no suggestion; a human picks
    SETTLE_RULES = [
        (SETTLE_TEAM_PLACEMENT_1, "Team that wins the match (placement 1)"),
        (SETTLE_TEAM_MOST_KILLS, "Team with the most kills"),
        (SETTLE_PLAYER_MOST_KILLS, "Player with the most kills"),
        (SETTLE_MATCH_MVP, "Match MVP"),
        (SETTLE_TOTAL_KILLS_OVER_UNDER, "Total kills over or under the line"),
        (SETTLE_MANUAL, "Manual (a human picks)"),
    ]

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=240, blank=True, default="")
    option_source = models.CharField(max_length=24, choices=OPTION_SOURCES, default=OPTIONS_CUSTOM)
    settle_rule = models.CharField(max_length=32, choices=SETTLE_RULES, default=SETTLE_MANUAL)
    # Whether markets of this kind need a match to be picked (True for every rule that reads
    # match stats). The create view refuses a match-less market on such a template.
    needs_match = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3 Markets and their options
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class Market(models.Model):
    """One question players stake on, tied to a real AFC event and, usually, a match.

    STATUSES, in the order they happen:
        DRAFT               written, invisible to players.
        OPEN                between open_at and lock_at: stakes accepted.
        LOCKED              lock_at passed (the beat sweep) or an admin locked it: no stakes, no
                            cancels, waiting for the result.
        PENDING_SETTLEMENT  the suggestion has been computed from the match stats (or an admin
                            asked for manual settlement); a wager admin must confirm or override.
        SETTLED             paid. Terminal.
        VOID                cancelled, every ACTIVE wager refunded in full. Terminal.
    Only services.py moves a market between these; views call services."""

    DRAFT = "DRAFT"
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    PENDING_SETTLEMENT = "PENDING_SETTLEMENT"
    SETTLED = "SETTLED"
    VOID = "VOID"
    STATUS_CHOICES = [
        (DRAFT, "Draft"), (OPEN, "Open"), (LOCKED, "Locked"),
        (PENDING_SETTLEMENT, "Pending settlement"), (SETTLED, "Settled"), (VOID, "Void"),
    ]
    PLAYER_VISIBLE = (OPEN, LOCKED, PENDING_SETTLEMENT, SETTLED, VOID)

    VISIBILITY_PUBLIC = "public"       # anyone can read; signed-in players stake
    VISIBILITY_SIGNED_IN = "signed_in"  # only signed-in players can even see it
    VISIBILITY_CHOICES = [(VISIBILITY_PUBLIC, "Public"), (VISIBILITY_SIGNED_IN, "Signed-in only")]

    # Addressed by slug everywhere (R22). sync_slug keeps SlugHistory when the title changes.
    slug = models.SlugField(max_length=90, unique=True, blank=True, db_index=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    rules_text = models.TextField(blank=True, default="")
    image = models.ImageField(upload_to="wager_markets/", null=True, blank=True)

    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event", on_delete=models.PROTECT, related_name="wager_markets")
    stage = models.ForeignKey(
        "afc_tournament_and_scrims.Stages", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="wager_markets")
    match = models.ForeignKey(
        "afc_tournament_and_scrims.Match", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="wager_markets")
    template = models.ForeignKey(MarketTemplate, on_delete=models.PROTECT, related_name="markets")
    # For over / under templates: the line (e.g. 60 kills). Ignored elsewhere.
    over_under_line = models.PositiveIntegerField(null=True, blank=True)

    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    visibility = models.CharField(max_length=12, choices=VISIBILITY_CHOICES, default=VISIBILITY_PUBLIC)
    featured = models.BooleanField(default=False)

    open_at = models.DateTimeField(null=True, blank=True)
    lock_at = models.DateTimeField(db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=240, blank=True, default="")

    # Per-market overrides of the settings; copied from WagerSettings at create time so a later
    # settings change never rewrites a live market's terms.
    rake_bps = models.PositiveIntegerField(default=500)
    cancel_fee_bps = models.PositiveIntegerField(default=100)
    min_stake_kobo = models.BigIntegerField(default=50_000)
    max_stake_per_user_kobo = models.BigIntegerField(default=50_000_000)
    max_pool_kobo = models.BigIntegerField(default=0)

    # Cached from the ACTIVE lines so lists never sum. Maintained by services only.
    cached_pool_kobo = models.BigIntegerField(default=0)
    cached_wager_count = models.PositiveIntegerField(default=0)

    # The suggestion and the decision. `suggested_option` is what the stats said; `settled_option`
    # is what an admin confirmed (equal unless overridden, then `override_reason` says why).
    suggested_option = models.ForeignKey(
        "MarketOption", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    suggestion_evidence = models.JSONField(default=dict, blank=True)
    suggested_at = models.DateTimeField(null=True, blank=True)
    settled_option = models.ForeignKey(
        "MarketOption", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="wager_markets_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "lock_at"])]

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = sync_slug(self, "title", kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.status})"

    # ── predicates, so views and services never re-derive them ──
    def is_open_for_stakes(self, now=None):
        now = now or timezone.now()
        if self.status != self.OPEN:
            return False
        if self.open_at and now < self.open_at:
            return False
        return now < self.lock_at

    def is_past_lock(self, now=None):
        return (now or timezone.now()) >= self.lock_at

    @property
    def is_terminal(self):
        return self.status in (self.SETTLED, self.VOID)


class MarketOption(models.Model):
    """One answer. `team` or `player` is set when the template reads match stats, so the
    suggestion can match a stats row to the option; a custom label has neither."""

    market = models.ForeignKey(Market, on_delete=models.CASCADE, related_name="options")
    label = models.CharField(max_length=80)
    sort_order = models.PositiveIntegerField(default=0)
    team = models.ForeignKey(
        "afc_tournament_and_scrims.TournamentTeam", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="wager_options")
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="wager_options")
    # For over / under: "over" or "under". Blank elsewhere.
    side = models.CharField(max_length=8, blank=True, default="")

    cached_pool_kobo = models.BigIntegerField(default=0)
    cached_line_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.label


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4 Wagers: one player's paid stake on a market, split over options
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class Wager(models.Model):
    """One placement. A wager is PENDING_PAYMENT until Paystack confirms the charge; only ACTIVE
    wagers are in the pool. Addressed by `public_token` (w_<hex>), never by pk (R22)."""

    PENDING_PAYMENT = "PENDING_PAYMENT"
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    WON = "WON"
    LOST = "LOST"
    REFUNDED = "REFUNDED"
    STATUS_CHOICES = [
        (PENDING_PAYMENT, "Awaiting payment"), (ACTIVE, "Active"), (CANCELLED, "Cancelled"),
        (EXPIRED, "Expired"), (WON, "Won"), (LOST, "Lost"), (REFUNDED, "Refunded"),
    ]

    public_token = models.CharField(max_length=24, unique=True, blank=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wagers")
    market = models.ForeignKey(Market, on_delete=models.PROTECT, related_name="wagers")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING_PAYMENT, db_index=True)
    total_stake_kobo = models.BigIntegerField()

    # Paystack: the reference AFC generated (metadata.kind = "wager"), the checkout URL handed to
    # the browser, and the charge id once paid.
    paystack_reference = models.CharField(max_length=64, unique=True, db_index=True)
    paystack_authorization_url = models.URLField(blank=True, default="")
    paystack_charge_id = models.CharField(max_length=40, blank=True, default="")
    payment_expires_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_fee_kobo = models.BigIntegerField(default=0)
    refund_kobo = models.BigIntegerField(default=0)     # cancel or void refund credited to Winnings
    payout_kobo = models.BigIntegerField(default=0)     # settlement payout credited to Winnings

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"]), models.Index(fields=["market", "status"])]

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = ensure_public_token(self, "w", "public_token", kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.public_token} {self.status} {self.total_stake_kobo}"


class WagerLine(models.Model):
    """One option's share of a wager. `outcome` is set at settlement."""

    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    REFUNDED = "REFUNDED"
    OUTCOME_CHOICES = [(PENDING, "Pending"), (WON, "Won"), (LOST, "Lost"), (REFUNDED, "Refunded")]

    wager = models.ForeignKey(Wager, on_delete=models.CASCADE, related_name="lines")
    option = models.ForeignKey(MarketOption, on_delete=models.PROTECT, related_name="lines")
    stake_kobo = models.BigIntegerField()
    outcome = models.CharField(max_length=10, choices=OUTCOME_CHOICES, default=PENDING)
    payout_kobo = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["id"]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5 Settlement: what the engine decided and who was paid
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class Settlement(models.Model):
    """One per settled or voided market. The numbers here are the audit trail of the payout; the
    LedgerEntry rows are the money."""

    WINNER = "WINNER"
    VOID_NO_WINNER = "VOID_NO_WINNER"       # nobody staked on the winning option
    VOID_SOLO_WAGER = "VOID_SOLO_WAGER"     # everybody staked on the winning option
    VOID_ADMIN = "VOID_ADMIN"               # an admin voided it (disputed, cancelled match)
    RESOLUTIONS = [
        (WINNER, "Winner paid"), (VOID_NO_WINNER, "Void: no winner"),
        (VOID_SOLO_WAGER, "Void: solo wager"), (VOID_ADMIN, "Void: by admin"),
    ]

    market = models.OneToOneField(Market, on_delete=models.CASCADE, related_name="settlement")
    resolution = models.CharField(max_length=20, choices=RESOLUTIONS)
    final_option = models.ForeignKey(MarketOption, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    suggested_option = models.ForeignKey(MarketOption, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    override_reason = models.CharField(max_length=240, blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)

    pool_kobo = models.BigIntegerField(default=0)
    rake_kobo = models.BigIntegerField(default=0)
    net_pool_kobo = models.BigIntegerField(default=0)
    dust_kobo = models.BigIntegerField(default=0)
    paid_total_kobo = models.BigIntegerField(default=0)
    refund_total_kobo = models.BigIntegerField(default=0)
    winners_count = models.PositiveIntegerField(default=0)

    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    confirmed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.market_id} {self.resolution}"


class Payout(models.Model):
    """One winner's share (or one refund on a void). Points at the ledger line that carried it."""

    settlement = models.ForeignKey(Settlement, on_delete=models.CASCADE, related_name="payouts")
    wager = models.ForeignKey(Wager, on_delete=models.CASCADE, related_name="payouts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wager_payouts")
    amount_kobo = models.BigIntegerField()
    is_refund = models.BooleanField(default=False)
    ledger_entry = models.ForeignKey("LedgerEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6 Winnings: the withdraw-only balance and its ledger
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class WinningsAccount(models.Model):
    """What AFC owes a player. Credited by settlement, refunds and admin credits; debited by
    withdrawals and admin debits. There is no deposit path by design (inbox #33)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="winnings")
    balance_kobo = models.BigIntegerField(default=0)
    # Held for withdrawals in flight. Available = balance - held.
    held_kobo = models.BigIntegerField(default=0)
    frozen = models.BooleanField(default=False)
    frozen_reason = models.CharField(max_length=240, blank=True, default="")
    frozen_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    frozen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def available_kobo(self):
        return max(self.balance_kobo - self.held_kobo, 0)

    def __str__(self):
        return f"{self.user_id}: {self.balance_kobo}"


class LedgerEntry(models.Model):
    """Every kobo that moved, with the balance after. `account` is None for the house lines
    (rake, fees, dust), which is how the admin overview totals house revenue."""

    PAYOUT = "PAYOUT"                       # settlement winnings
    VOID_REFUND = "VOID_REFUND"             # market voided, stake back
    CANCEL_REFUND = "CANCEL_REFUND"         # pre-lock cancel, stake minus fee back
    WITHDRAWAL_HOLD = "WITHDRAWAL_HOLD"     # requested: held (shown, not moved)
    WITHDRAWAL_PAID = "WITHDRAWAL_PAID"     # transfer succeeded: balance down
    WITHDRAWAL_RELEASED = "WITHDRAWAL_RELEASED"  # rejected / cancelled / failed: hold released
    ADJUSTMENT_CREDIT = "ADJUSTMENT_CREDIT"
    ADJUSTMENT_DEBIT = "ADJUSTMENT_DEBIT"
    HOUSE_RAKE = "HOUSE_RAKE"
    HOUSE_CANCEL_FEE = "HOUSE_CANCEL_FEE"
    HOUSE_DUST = "HOUSE_DUST"
    KIND_CHOICES = [
        (PAYOUT, "Winnings paid"), (VOID_REFUND, "Refund (market void)"),
        (CANCEL_REFUND, "Refund (wager cancelled)"), (WITHDRAWAL_HOLD, "Withdrawal requested"),
        (WITHDRAWAL_PAID, "Withdrawal paid"), (WITHDRAWAL_RELEASED, "Withdrawal released"),
        (ADJUSTMENT_CREDIT, "Adjustment (credit)"), (ADJUSTMENT_DEBIT, "Adjustment (debit)"),
        (HOUSE_RAKE, "House rake"), (HOUSE_CANCEL_FEE, "House cancel fee"), (HOUSE_DUST, "House rounding"),
    ]
    HOUSE_KINDS = (HOUSE_RAKE, HOUSE_CANCEL_FEE, HOUSE_DUST)

    account = models.ForeignKey(WinningsAccount, null=True, blank=True, on_delete=models.CASCADE, related_name="entries")
    kind = models.CharField(max_length=24, choices=KIND_CHOICES, db_index=True)
    # Signed: credits positive, debits negative. Holds are recorded with amount 0 and the held
    # value in `held_delta_kobo` so the balance column stays honest.
    amount_kobo = models.BigIntegerField(default=0)
    held_delta_kobo = models.BigIntegerField(default=0)
    balance_after_kobo = models.BigIntegerField(default=0)
    # What this line is about: a market slug, a wager token, a withdrawal token, an adjustment id.
    ref_kind = models.CharField(max_length=20, blank=True, default="")
    ref = models.CharField(max_length=64, blank=True, default="", db_index=True)
    note = models.CharField(max_length=240, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["account", "-created_at"])]


class PayoutBankAccount(models.Model):
    """Where a player's withdrawals go. Verified through Paystack's account resolve at save time
    (the name comes back from the bank, never typed) and turned into a transfer recipient."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payout_bank_accounts")
    bank_code = models.CharField(max_length=12)
    bank_name = models.CharField(max_length=80)
    account_number = models.CharField(max_length=20)
    account_name = models.CharField(max_length=120)
    recipient_code = models.CharField(max_length=40, blank=True, default="")   # Paystack RCP_...
    is_default = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "bank_code", "account_number")]

    @property
    def masked_number(self):
        n = self.account_number
        return f"{'*' * max(len(n) - 4, 0)}{n[-4:]}"


class Withdrawal(models.Model):
    """Money leaving Winnings for a bank account. Two-key above the co-sign threshold."""

    REQUESTED = "REQUESTED"
    PENDING_COSIGN = "PENDING_COSIGN"
    APPROVED = "APPROVED"          # transfer submitted to Paystack
    PAID = "PAID"                  # transfer succeeded
    FAILED = "FAILED"              # transfer failed; hold released, admin may retry
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"        # by the player, while REQUESTED
    STATUS_CHOICES = [
        (REQUESTED, "Requested"), (PENDING_COSIGN, "Awaiting co-sign"), (APPROVED, "Approved"),
        (PAID, "Paid"), (FAILED, "Failed"), (REJECTED, "Rejected"), (CANCELLED, "Cancelled"),
    ]
    OPEN_STATUSES = (REQUESTED, PENDING_COSIGN, APPROVED)

    public_token = models.CharField(max_length=24, unique=True, blank=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wager_withdrawals")
    account = models.ForeignKey(WinningsAccount, on_delete=models.CASCADE, related_name="withdrawals")
    bank_account = models.ForeignKey(PayoutBankAccount, on_delete=models.PROTECT, related_name="withdrawals")
    amount_kobo = models.BigIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=REQUESTED, db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    cosigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cosigned_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=240, blank=True, default="")

    transfer_code = models.CharField(max_length=40, blank=True, default="")
    transfer_reference = models.CharField(max_length=64, blank=True, default="", db_index=True)
    failure_reason = models.CharField(max_length=240, blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = ensure_public_token(self, "wd", "public_token", kwargs.get("update_fields"))
        super().save(*args, **kwargs)


class Adjustment(models.Model):
    """An admin correction to a player's Winnings, with a reason. Above the co-sign threshold it
    waits for a second admin who is NOT the submitter."""

    CREDIT = "CREDIT"
    DEBIT = "DEBIT"
    DIRECTIONS = [(CREDIT, "Credit"), (DEBIT, "Debit")]

    EXECUTED = "EXECUTED"
    PENDING_COSIGN = "PENDING_COSIGN"
    REJECTED = "REJECTED"
    STATUS_CHOICES = [(EXECUTED, "Executed"), (PENDING_COSIGN, "Awaiting co-sign"), (REJECTED, "Rejected")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wager_adjustments")
    direction = models.CharField(max_length=6, choices=DIRECTIONS)
    amount_kobo = models.BigIntegerField()
    reason = models.CharField(max_length=240)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=EXECUTED, db_index=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cosigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cosigned_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.CharField(max_length=240, blank=True, default="")
    executed_at = models.DateTimeField(null=True, blank=True)
    ledger_entry = models.ForeignKey(LedgerEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §7 Who may wager and withdraw: KYC-Lite and the responsible-gaming limits
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class KycStatus(models.Model):
    """KYC-Lite on top of what AFC already knows about the person.

    The three facts and where they live:
      * WhatsApp confirmed: the profile number (UserProfile.whatsapp_number) verified by a code
        through afc_auth.two_factor (purpose wager_kyc). Recorded here as whatsapp_verified_at +
        the number it was confirmed for (a changed number needs a new code).
      * Discord linked: read live from User.discord_id, never copied.
      * Age: UserProfile.date_of_birth, compared to WagerSettings.min_age at each check.
    Tier LITE = WhatsApp confirmed AND Discord linked. Wagering needs the age check only;
    withdrawing needs LITE."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wager_kyc")
    whatsapp_verified_at = models.DateTimeField(null=True, blank=True)
    whatsapp_verified_number = models.CharField(max_length=20, blank=True, default="")
    forced_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    forced_at = models.DateTimeField(null=True, blank=True)
    force_reason = models.CharField(max_length=240, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)


class PlayerLimits(models.Model):
    """The player's own responsible-gaming settings. Tightening applies at once; loosening is
    written to the `pending_*` columns with `pending_effective_at` 24 hours out, and promoted by
    `services.effective_limits` when that time has passed. 0 = no cap."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wager_limits")
    daily_stake_cap_kobo = models.BigIntegerField(default=0)
    weekly_stake_cap_kobo = models.BigIntegerField(default=0)
    daily_loss_cap_kobo = models.BigIntegerField(default=0)

    pending_daily_stake_cap_kobo = models.BigIntegerField(null=True, blank=True)
    pending_weekly_stake_cap_kobo = models.BigIntegerField(null=True, blank=True)
    pending_daily_loss_cap_kobo = models.BigIntegerField(null=True, blank=True)
    pending_effective_at = models.DateTimeField(null=True, blank=True)

    cooloff_until = models.DateTimeField(null=True, blank=True)
    self_excluded_until = models.DateTimeField(null=True, blank=True)
    self_excluded_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    CAP_FIELDS = ("daily_stake_cap_kobo", "weekly_stake_cap_kobo", "daily_loss_cap_kobo")
