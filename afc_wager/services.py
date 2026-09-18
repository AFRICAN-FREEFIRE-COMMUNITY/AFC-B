"""
afc_wager.services - every operation that moves a market or a kobo. Views call these; nothing
else writes Wager, Market status, WinningsAccount or LedgerEntry.

WHY ONE MODULE: money has to be moved under one lock per market or per account, with the
cached pool counts, the ledger line and the balance-after written in the same transaction. A
view that did any of that itself would be the second copy that drifts. Every function here
either succeeds atomically or raises WagerError with a `code` the view answers as
`{"message", "code"}` (R35 / R44).

MAP
    §1 errors and the guards (settings, age, limits, market state)
    §2 place / activate / expire / cancel
    §3 lifecycle: lock sweep, suggestion, settle, void
    §4 winnings: credit / debit / hold, withdrawals, adjustments, freeze
    §5 limits and KYC reads

CONNECTS TO
    afc_wager.engine (the arithmetic), afc_wager.payments (Paystack for stakes and transfers),
    afc_wager.suggest (reads match stats), afc_wager.notify (bell / email / WhatsApp / Discord),
    afc_auth.two_factor (the KYC WhatsApp code), afc_tournament_and_scrims models (event, match).
"""
import logging
from datetime import date, timedelta

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from afc_auth.models import canonical_profile

from . import engine
from .models import (
    Adjustment, KycStatus, LedgerEntry, Market, MarketOption, MarketTemplate, Payout,
    PayoutBankAccount, PlayerLimits, Settlement, Wager, WagerLine, WagerSettings, Withdrawal,
    WinningsAccount,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1 Errors and guards
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class WagerError(Exception):
    """A refusal with a code the screen can translate. `status` is the HTTP status the view
    should answer; 400 unless the refusal is about who you are (401 / 403) or what exists (404)."""

    def __init__(self, code, message, status=400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def as_dict(self):
        out = {"message": self.message, "code": self.code}
        out.update(self.extra)
        return out


def age_on(dob, today=None):
    today = today or timezone.localdate()
    if not dob:
        return None
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def check_age(user, cfg=None):
    """The age gate. Raises `age_required` (no date of birth on the profile) or `underage`."""
    cfg = cfg or WagerSettings.get()
    profile = canonical_profile(user)
    dob = getattr(profile, "date_of_birth", None) if profile else None
    if not dob:
        raise WagerError("age_required", "Add your date of birth to your profile before you wager.", 403)
    years = age_on(dob)
    if years is None or years < cfg.min_age:
        raise WagerError("underage", f"You must be {cfg.min_age} or older to wager.", 403)


def effective_limits(user, cfg=None):
    """The player's limits as they stand NOW: promotes a pending loosening whose 24 hours have
    passed, then falls back to the settings defaults for any cap the player left at 0."""
    cfg = cfg or WagerSettings.get()
    limits, _ = PlayerLimits.objects.get_or_create(user=user)
    now = timezone.now()
    if limits.pending_effective_at and limits.pending_effective_at <= now:
        changed = []
        for f in PlayerLimits.CAP_FIELDS:
            pending = getattr(limits, f"pending_{f}")
            if pending is not None:
                setattr(limits, f, pending)
                setattr(limits, f"pending_{f}", None)
                changed += [f, f"pending_{f}"]
        limits.pending_effective_at = None
        limits.save(update_fields=changed + ["pending_effective_at", "updated_at"])
    return limits, {
        "daily_stake_cap_kobo": limits.daily_stake_cap_kobo or cfg.default_daily_stake_cap_kobo,
        "weekly_stake_cap_kobo": limits.weekly_stake_cap_kobo or cfg.default_weekly_stake_cap_kobo,
        "daily_loss_cap_kobo": limits.daily_loss_cap_kobo or cfg.default_daily_loss_cap_kobo,
    }


def _stakes_since(user, since):
    return (Wager.objects.filter(user=user, created_at__gte=since,
                                 status__in=(Wager.ACTIVE, Wager.WON, Wager.LOST, Wager.REFUNDED))
            .aggregate(s=Sum("total_stake_kobo"))["s"] or 0)


def _losses_since(user, since):
    """Net loss in the window: stakes on settled-lost wagers minus winnings on won ones."""
    lost = (Wager.objects.filter(user=user, status=Wager.LOST, updated_at__gte=since)
            .aggregate(s=Sum("total_stake_kobo"))["s"] or 0)
    won = (Wager.objects.filter(user=user, status=Wager.WON, updated_at__gte=since)
           .aggregate(s=Sum(F("payout_kobo") - F("total_stake_kobo")))["s"] or 0)
    return max(lost - won, 0)


def check_limits(user, stake_kobo, cfg=None):
    """Cool-off, self-exclusion, then the three caps. Raises with a code naming the cap."""
    cfg = cfg or WagerSettings.get()
    limits, caps = effective_limits(user, cfg)
    now = timezone.now()
    if limits.self_excluded_until and limits.self_excluded_until > now:
        raise WagerError("self_excluded", "You have excluded yourself from wagering.", 403,
                         until=limits.self_excluded_until.isoformat())
    if limits.cooloff_until and limits.cooloff_until > now:
        raise WagerError("cooling_off", "You are on a cool-off break.", 403,
                         until=limits.cooloff_until.isoformat())
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    cap = caps["daily_stake_cap_kobo"]
    if cap and _stakes_since(user, day_ago) + stake_kobo > cap:
        raise WagerError("daily_stake_cap", "This stake would take you past your daily limit.", 400, cap_kobo=cap)
    cap = caps["weekly_stake_cap_kobo"]
    if cap and _stakes_since(user, week_ago) + stake_kobo > cap:
        raise WagerError("weekly_stake_cap", "This stake would take you past your weekly limit.", 400, cap_kobo=cap)
    cap = caps["daily_loss_cap_kobo"]
    if cap and _losses_since(user, day_ago) >= cap:
        raise WagerError("daily_loss_cap", "You have reached your daily loss limit.", 400, cap_kobo=cap)


def check_market_open(market, now=None):
    now = now or timezone.now()
    if market.status == Market.DRAFT:
        raise WagerError("market_not_found", "We could not find that market.", 404)
    if market.status != Market.OPEN or market.is_past_lock(now):
        raise WagerError("market_locked", "This market is closed to new stakes.", 409)
    if market.open_at and now < market.open_at:
        raise WagerError("market_not_open_yet", "This market has not opened yet.", 409)


def check_switch(cfg=None):
    cfg = cfg or WagerSettings.get()
    if not cfg.wagering_enabled:
        raise WagerError("wagering_paused", cfg.maintenance_message or "Wagering is paused for now.", 503)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2 Place, pay, expire, cancel
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _clean_lines(market, raw_lines):
    """[{option_id, stake_kobo}] -> [(MarketOption, int)], every option of THIS market, every
    stake a positive integer. Refuses with a code; never trusts a float."""
    if not isinstance(raw_lines, list) or not raw_lines:
        raise WagerError("lines_required", "Pick at least one option and a stake.")
    options = {o.id: o for o in market.options.all()}
    out = {}
    for item in raw_lines:
        if not isinstance(item, dict):
            raise WagerError("lines_invalid", "Each line needs an option and a stake.")
        try:
            option_id = int(item.get("option_id"))
            stake = int(item.get("stake_kobo"))
        except (TypeError, ValueError):
            raise WagerError("lines_invalid", "Each line needs an option and a stake.")
        if option_id not in options:
            raise WagerError("option_not_found", "One of those options is not on this market.")
        if stake <= 0:
            continue
        out[option_id] = out.get(option_id, 0) + stake
    if not out:
        raise WagerError("lines_required", "Pick at least one option and a stake.")
    return [(options[oid], stake) for oid, stake in out.items()]


def place_wager(user, market, raw_lines, *, cfg=None, now=None, request=None):
    """Create a PENDING_PAYMENT wager and its Paystack checkout. Returns the Wager (with
    `paystack_authorization_url` set). Nothing is in the pool until activate_wager runs."""
    from . import payments

    cfg = cfg or WagerSettings.get()
    now = now or timezone.now()
    check_switch(cfg)
    check_age(user, cfg)
    check_market_open(market, now)
    lines = _clean_lines(market, raw_lines)
    total = sum(stake for _, stake in lines)

    if total < market.min_stake_kobo:
        raise WagerError("stake_below_minimum", "That stake is below the minimum for this market.",
                         400, min_stake_kobo=market.min_stake_kobo)
    already = (Wager.objects.filter(user=user, market=market, status=Wager.ACTIVE)
               .aggregate(s=Sum("total_stake_kobo"))["s"] or 0)
    if market.max_stake_per_user_kobo and already + total > market.max_stake_per_user_kobo:
        raise WagerError("stake_above_maximum", "That stake would take you past the maximum for this market.",
                         400, max_stake_per_user_kobo=market.max_stake_per_user_kobo)
    if market.max_pool_kobo and market.cached_pool_kobo + total > market.max_pool_kobo:
        raise WagerError("pool_full", "This market's pool is full.", 409)
    check_limits(user, total, cfg)

    wager = Wager(
        user=user, market=market, total_stake_kobo=total,
        paystack_reference=payments.new_reference(),
        payment_expires_at=now + timedelta(minutes=cfg.payment_expiry_minutes),
    )
    wager.save()
    WagerLine.objects.bulk_create([
        WagerLine(wager=wager, option=option, stake_kobo=stake) for option, stake in lines
    ])
    url = payments.initialize_stake(user, wager, request)
    wager.paystack_authorization_url = url
    wager.save(update_fields=["paystack_authorization_url", "updated_at"])
    return wager


def _add_to_pool(wager):
    """Cached counts up for an ACTIVE wager. Called inside the activating transaction."""
    Market.objects.filter(pk=wager.market_id).update(
        cached_pool_kobo=F("cached_pool_kobo") + wager.total_stake_kobo,
        cached_wager_count=F("cached_wager_count") + 1,
    )
    for line in wager.lines.all():
        MarketOption.objects.filter(pk=line.option_id).update(
            cached_pool_kobo=F("cached_pool_kobo") + line.stake_kobo,
            cached_line_count=F("cached_line_count") + 1,
        )


def _remove_from_pool(wager):
    Market.objects.filter(pk=wager.market_id).update(
        cached_pool_kobo=F("cached_pool_kobo") - wager.total_stake_kobo,
        cached_wager_count=F("cached_wager_count") - 1,
    )
    for line in wager.lines.all():
        MarketOption.objects.filter(pk=line.option_id).update(
            cached_pool_kobo=F("cached_pool_kobo") - line.stake_kobo,
            cached_line_count=F("cached_line_count") - 1,
        )


def activate_wager(wager, *, charge_id="", now=None):
    """Paystack confirmed the charge (webhook or verify). Idempotent: a second call on an ACTIVE
    wager is a no-op. A payment that arrives after the market locked is still honoured if it
    arrived before lock_at (the charge is what it is); one that arrives for a market already
    locked, settled or voided is refunded to Winnings in full, because the pool is closed."""
    from . import notify

    now = now or timezone.now()
    with transaction.atomic():
        w = Wager.objects.select_for_update().select_related("market").get(pk=wager.pk)
        if w.status == Wager.ACTIVE:
            return w
        if w.status != Wager.PENDING_PAYMENT and w.status != Wager.EXPIRED:
            return w
        market = Market.objects.select_for_update().get(pk=w.market_id)
        w.paid_at = now
        w.paystack_charge_id = charge_id or w.paystack_charge_id
        if market.status == Market.OPEN and not market.is_past_lock(now):
            w.status = Wager.ACTIVE
            w.save(update_fields=["status", "paid_at", "paystack_charge_id", "updated_at"])
            _add_to_pool(w)
        else:
            # Paid too late for the pool: money back in full, no fee (it was never a stake).
            w.status = Wager.REFUNDED
            w.refund_kobo = w.total_stake_kobo
            w.save(update_fields=["status", "paid_at", "paystack_charge_id", "refund_kobo", "updated_at"])
            credit(w.user, w.total_stake_kobo, LedgerEntry.VOID_REFUND, ref_kind="wager",
                   ref=w.public_token, note="Paid after the market closed; stake returned")
            w.lines.update(outcome=WagerLine.REFUNDED)
    notify.wager_activated(w) if w.status == Wager.ACTIVE else notify.wager_refunded_late(w)
    return w


def expire_unpaid(now=None):
    """The beat sweep: PENDING_PAYMENT past its window becomes EXPIRED. Returns the count. A
    charge that still lands later is handled by activate_wager (it accepts EXPIRED)."""
    now = now or timezone.now()
    return Wager.objects.filter(status=Wager.PENDING_PAYMENT, payment_expires_at__lt=now).update(
        status=Wager.EXPIRED, updated_at=now)


def cancel_wager(user, wager, *, cfg=None, now=None):
    """The player pulls an ACTIVE wager before lock. Fee to the house, the rest to Winnings."""
    from . import notify

    cfg = cfg or WagerSettings.get()
    now = now or timezone.now()
    check_switch(cfg)
    with transaction.atomic():
        w = Wager.objects.select_for_update().select_related("market").get(pk=wager.pk)
        if w.user_id != user.pk:
            raise WagerError("wager_not_found", "We could not find that wager.", 404)
        if w.status != Wager.ACTIVE:
            raise WagerError("wager_not_active", "Only an active wager can be cancelled.", 409)
        market = Market.objects.select_for_update().get(pk=w.market_id)
        if market.status != Market.OPEN or market.is_past_lock(now):
            raise WagerError("market_locked", "This market has locked; wagers can no longer be cancelled.", 409)
        fee = (w.total_stake_kobo * market.cancel_fee_bps) // 10000
        refund = w.total_stake_kobo - fee
        w.status = Wager.CANCELLED
        w.cancelled_at = now
        w.cancel_fee_kobo = fee
        w.refund_kobo = refund
        w.save(update_fields=["status", "cancelled_at", "cancel_fee_kobo", "refund_kobo", "updated_at"])
        w.lines.update(outcome=WagerLine.REFUNDED)
        _remove_from_pool(w)
        credit(w.user, refund, LedgerEntry.CANCEL_REFUND, ref_kind="wager", ref=w.public_token,
               note=f"Cancelled before lock; {market.cancel_fee_bps / 100:g}% fee")
        if fee:
            house_line(LedgerEntry.HOUSE_CANCEL_FEE, fee, ref_kind="wager", ref=w.public_token)
    notify.wager_cancelled(w)
    return w


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3 Lifecycle: lock, suggest, settle, void
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def lock_market(market, *, by=None, now=None):
    """OPEN -> LOCKED. Idempotent. Called by the beat sweep at lock_at and by an admin."""
    from . import notify

    now = now or timezone.now()
    with transaction.atomic():
        m = Market.objects.select_for_update().get(pk=market.pk)
        if m.status != Market.OPEN:
            return m
        m.status = Market.LOCKED
        m.locked_at = now
        if by is not None and m.lock_at > now:
            m.lock_at = now   # an early admin lock: the record says when stakes really stopped
        m.save(update_fields=["status", "locked_at", "lock_at", "updated_at"])
    notify.market_locked(m)
    return m


def lock_due_markets(now=None):
    """The beat sweep. Returns how many locked."""
    now = now or timezone.now()
    count = 0
    for m in Market.objects.filter(status=Market.OPEN, lock_at__lte=now):
        lock_market(m, now=now)
        count += 1
    return count


def reopen_market(market, *, new_lock_at, by, reason):
    """LOCKED -> OPEN with a later lock_at (a match was postponed). Refused once suggested."""
    with transaction.atomic():
        m = Market.objects.select_for_update().get(pk=market.pk)
        if m.status != Market.LOCKED:
            raise WagerError("market_not_locked", "Only a locked market can be reopened.", 409)
        if new_lock_at <= timezone.now():
            raise WagerError("lock_at_past", "The new lock time must be in the future.")
        m.status = Market.OPEN
        m.locked_at = None
        m.lock_at = new_lock_at
        m.save(update_fields=["status", "locked_at", "lock_at", "updated_at"])
    return m


def suggest_settlement(market, *, now=None):
    """Read the match stats and record the suggestion. LOCKED -> PENDING_SETTLEMENT. Returns the
    market; leaves it LOCKED (with evidence saying why) when the stats are not in yet."""
    from . import suggest

    now = now or timezone.now()
    with transaction.atomic():
        m = Market.objects.select_for_update().select_related("template", "match").get(pk=market.pk)
        if m.status not in (Market.LOCKED, Market.PENDING_SETTLEMENT):
            raise WagerError("market_not_locked", "Only a locked market can be suggested.", 409)
        option, evidence = suggest.compute(m)
        m.suggestion_evidence = evidence
        m.suggested_at = now
        if option is None and not evidence.get("manual"):
            # Results not in yet: stay LOCKED, keep the reason on the row for the queue to show.
            m.save(update_fields=["suggestion_evidence", "suggested_at", "updated_at"])
            return m
        m.suggested_option = option
        m.status = Market.PENDING_SETTLEMENT
        m.save(update_fields=["suggestion_evidence", "suggested_at", "suggested_option", "status", "updated_at"])
    return m


def suggest_due_markets(now=None):
    """The beat sweep after locks: every LOCKED market with a match whose results are in."""
    count = 0
    for m in Market.objects.filter(status=Market.LOCKED).select_related("template", "match"):
        try:
            if suggest_settlement(m, now=now).status == Market.PENDING_SETTLEMENT:
                count += 1
        except WagerError:
            continue
    return count


def settle_market(market, *, final_option, by, override_reason="", now=None):
    """The decision. Runs the engine over the ACTIVE lines, pays every winner into Winnings,
    books the rake and dust to the house, marks lines and wagers. Idempotent by status: a
    SETTLED or VOID market is refused with `market_settled`."""
    from . import notify

    now = now or timezone.now()
    with transaction.atomic():
        m = Market.objects.select_for_update().get(pk=market.pk)
        if m.status not in (Market.LOCKED, Market.PENDING_SETTLEMENT):
            raise WagerError("market_settled" if m.is_terminal else "market_not_locked",
                             "This market cannot be settled from its current state.", 409)
        if final_option.market_id != m.pk:
            raise WagerError("option_not_found", "That option is not on this market.")
        if m.suggested_option_id and m.suggested_option_id != final_option.pk and not override_reason.strip():
            raise WagerError("override_reason_required",
                             "Tell us why you are overriding the suggestion.")

        lines = list(WagerLine.objects.select_related("wager").filter(
            wager__market=m, wager__status=Wager.ACTIVE))
        pool = sum(l.stake_kobo for l in lines)
        winning = [engine.WinningLine(user=l.wager.user_id, stake_kobo=l.stake_kobo)
                   for l in lines if l.option_id == final_option.pk]
        loser_total = sum(l.stake_kobo for l in lines if l.option_id != final_option.pk)
        result = engine.compute_settlement(pool_kobo=pool, rake_bps=m.rake_bps,
                                           winning_lines=winning, loser_total_kobo=loser_total)

        settlement = Settlement.objects.create(
            market=m, resolution=result.resolution, final_option=final_option,
            suggested_option_id=m.suggested_option_id, override_reason=override_reason.strip()[:240],
            evidence=m.suggestion_evidence or {}, pool_kobo=pool, rake_kobo=result.rake_kobo,
            net_pool_kobo=result.net_pool_kobo, dust_kobo=result.dust_kobo, confirmed_by=by,
        )
        wagers = {l.wager_id: l.wager for l in lines}

        if result.resolution == engine.RESOLUTION_WINNER:
            paid_total = 0
            winners = 0
            for w in wagers.values():
                amount = result.payouts.get(w.user_id, 0)
                if amount > 0 and any(l.option_id == final_option.pk for l in lines if l.wager_id == w.pk):
                    w.status = Wager.WON
                    w.payout_kobo = amount
                    entry = credit(w.user, amount, LedgerEntry.PAYOUT, ref_kind="market", ref=m.slug,
                                   note=f"Won on {m.title}")
                    Payout.objects.create(settlement=settlement, wager=w, user=w.user, amount_kobo=amount,
                                          ledger_entry=entry)
                    paid_total += amount
                    winners += 1
                    # the payout dict is per user; make sure a user with two wagers is paid once
                    result.payouts[w.user_id] = 0
                else:
                    w.status = Wager.LOST
                w.save(update_fields=["status", "payout_kobo", "updated_at"])
            for l in lines:
                l.outcome = WagerLine.WON if l.option_id == final_option.pk else WagerLine.LOST
            WagerLine.objects.bulk_update(lines, ["outcome"])
            if result.rake_kobo:
                house_line(LedgerEntry.HOUSE_RAKE, result.rake_kobo, ref_kind="market", ref=m.slug)
            if result.dust_kobo:
                house_line(LedgerEntry.HOUSE_DUST, result.dust_kobo, ref_kind="market", ref=m.slug)
            settlement.paid_total_kobo = paid_total
            settlement.winners_count = winners
        else:
            refund_total = 0
            for w in wagers.values():
                w.status = Wager.REFUNDED
                w.refund_kobo = w.total_stake_kobo
                w.save(update_fields=["status", "refund_kobo", "updated_at"])
                entry = credit(w.user, w.total_stake_kobo, LedgerEntry.VOID_REFUND, ref_kind="market",
                               ref=m.slug, note=f"{m.title}: no contest, stake returned")
                Payout.objects.create(settlement=settlement, wager=w, user=w.user,
                                      amount_kobo=w.total_stake_kobo, is_refund=True, ledger_entry=entry)
                refund_total += w.total_stake_kobo
            WagerLine.objects.filter(wager__in=wagers.keys()).update(outcome=WagerLine.REFUNDED)
            settlement.refund_total_kobo = refund_total
        settlement.save()

        m.status = Market.SETTLED
        m.settled_option = final_option
        m.settled_at = now
        m.save(update_fields=["status", "settled_option", "settled_at", "updated_at"])
    notify.market_settled(m, settlement)
    return settlement


def void_market(market, *, by, reason, now=None):
    """Any non-terminal market -> VOID. Every ACTIVE wager refunded in full, no fee, no rake."""
    from . import notify

    now = now or timezone.now()
    if not (reason or "").strip():
        raise WagerError("reason_required", "Say why the market is being voided.")
    with transaction.atomic():
        m = Market.objects.select_for_update().get(pk=market.pk)
        if m.is_terminal:
            raise WagerError("market_settled", "This market has already been settled or voided.", 409)
        settlement = Settlement.objects.create(
            market=m, resolution=Settlement.VOID_ADMIN, suggested_option_id=m.suggested_option_id,
            override_reason=reason.strip()[:240], evidence=m.suggestion_evidence or {},
            pool_kobo=m.cached_pool_kobo, confirmed_by=by,
        )
        refund_total = 0
        for w in Wager.objects.select_for_update().filter(market=m, status=Wager.ACTIVE):
            w.status = Wager.REFUNDED
            w.refund_kobo = w.total_stake_kobo
            w.save(update_fields=["status", "refund_kobo", "updated_at"])
            entry = credit(w.user, w.total_stake_kobo, LedgerEntry.VOID_REFUND, ref_kind="market",
                           ref=m.slug, note=f"{m.title} was voided: stake returned")
            Payout.objects.create(settlement=settlement, wager=w, user=w.user,
                                  amount_kobo=w.total_stake_kobo, is_refund=True, ledger_entry=entry)
            w.lines.update(outcome=WagerLine.REFUNDED)
            refund_total += w.total_stake_kobo
        # Unpaid ones can never join a voided pool.
        Wager.objects.filter(market=m, status=Wager.PENDING_PAYMENT).update(status=Wager.EXPIRED, updated_at=now)
        settlement.refund_total_kobo = refund_total
        settlement.save(update_fields=["refund_total_kobo"])
        m.status = Market.VOID
        m.voided_at = now
        m.void_reason = reason.strip()[:240]
        m.save(update_fields=["status", "voided_at", "void_reason", "updated_at"])
    notify.market_voided(m, settlement)
    return settlement


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4 Winnings: the ledger, withdrawals, adjustments, freeze
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def account_for(user):
    account, _ = WinningsAccount.objects.get_or_create(user=user)
    return account


def credit(user, amount_kobo, kind, *, ref_kind="", ref="", note="", by=None):
    """Balance up by `amount_kobo`, with the ledger line. Inside the caller's transaction."""
    if amount_kobo <= 0:
        raise WagerError("amount_invalid", "The amount must be positive.")
    account = WinningsAccount.objects.select_for_update().get_or_create(user=user)[0]
    account.balance_kobo += amount_kobo
    account.save(update_fields=["balance_kobo", "updated_at"])
    return LedgerEntry.objects.create(
        account=account, kind=kind, amount_kobo=amount_kobo, balance_after_kobo=account.balance_kobo,
        ref_kind=ref_kind, ref=ref, note=note[:240], created_by=by)


def debit(user, amount_kobo, kind, *, ref_kind="", ref="", note="", by=None, release_hold=False):
    """Balance down. Refuses to overdraw (`insufficient_winnings`). `release_hold` also lowers
    held_kobo by the same amount (a withdrawal being paid)."""
    if amount_kobo <= 0:
        raise WagerError("amount_invalid", "The amount must be positive.")
    account = WinningsAccount.objects.select_for_update().get_or_create(user=user)[0]
    if account.balance_kobo < amount_kobo:
        raise WagerError("insufficient_winnings", "There is not enough in Winnings for that.", 409)
    account.balance_kobo -= amount_kobo
    held_delta = 0
    if release_hold:
        held_delta = -min(account.held_kobo, amount_kobo)
        account.held_kobo += held_delta
    account.save(update_fields=["balance_kobo", "held_kobo", "updated_at"])
    return LedgerEntry.objects.create(
        account=account, kind=kind, amount_kobo=-amount_kobo, held_delta_kobo=held_delta,
        balance_after_kobo=account.balance_kobo, ref_kind=ref_kind, ref=ref, note=note[:240], created_by=by)


def hold(account, amount_kobo, kind, *, ref_kind="", ref="", note=""):
    """Reserve part of the balance (a withdrawal request). Ledger line with amount 0."""
    if account.available_kobo < amount_kobo:
        raise WagerError("insufficient_winnings", "There is not enough available in Winnings for that.", 409)
    account.held_kobo += amount_kobo
    account.save(update_fields=["held_kobo", "updated_at"])
    return LedgerEntry.objects.create(
        account=account, kind=kind, amount_kobo=0, held_delta_kobo=amount_kobo,
        balance_after_kobo=account.balance_kobo, ref_kind=ref_kind, ref=ref, note=note[:240])


def release(account, amount_kobo, *, ref_kind="", ref="", note=""):
    account.held_kobo = max(account.held_kobo - amount_kobo, 0)
    account.save(update_fields=["held_kobo", "updated_at"])
    return LedgerEntry.objects.create(
        account=account, kind=LedgerEntry.WITHDRAWAL_RELEASED, amount_kobo=0, held_delta_kobo=-amount_kobo,
        balance_after_kobo=account.balance_kobo, ref_kind=ref_kind, ref=ref, note=note[:240])


def house_line(kind, amount_kobo, *, ref_kind="", ref="", note=""):
    """House revenue: a ledger line with no account."""
    if amount_kobo <= 0:
        return None
    return LedgerEntry.objects.create(account=None, kind=kind, amount_kobo=amount_kobo,
                                      ref_kind=ref_kind, ref=ref, note=note[:240])


def house_totals():
    rows = (LedgerEntry.objects.filter(account__isnull=True, kind__in=LedgerEntry.HOUSE_KINDS)
            .values("kind").annotate(total=Sum("amount_kobo")))
    out = {k: 0 for k in LedgerEntry.HOUSE_KINDS}
    for r in rows:
        out[r["kind"]] = r["total"] or 0
    out["total_kobo"] = sum(out[k] for k in LedgerEntry.HOUSE_KINDS)
    return out


def save_bank_account(user, *, bank_code, account_number):
    """Resolve the account with Paystack (the name comes from the bank), create the transfer
    recipient, store it. Returns the PayoutBankAccount. Raises with a code on any refusal."""
    from . import payments

    bank_code = str(bank_code or "").strip()
    account_number = "".join(ch for ch in str(account_number or "") if ch.isdigit())
    if not bank_code or len(account_number) != 10:
        raise WagerError("bank_account_invalid", "Enter a bank and a 10-digit account number.")
    existing = PayoutBankAccount.objects.filter(user=user, bank_code=bank_code, account_number=account_number).first()
    if existing:
        return existing
    account_name, bank_name = payments.resolve_bank_account(bank_code, account_number)
    recipient_code = payments.create_transfer_recipient(account_name, account_number, bank_code)
    PayoutBankAccount.objects.filter(user=user, is_default=True).update(is_default=False)
    return PayoutBankAccount.objects.create(
        user=user, bank_code=bank_code, bank_name=bank_name, account_number=account_number,
        account_name=account_name, recipient_code=recipient_code, is_default=True)


def request_withdrawal(user, *, amount_kobo, bank_account, cfg=None):
    """Hold the amount and open a request. KYC-Lite, minimum, one open request at a time."""
    from . import notify

    cfg = cfg or WagerSettings.get()
    kyc = kyc_state(user, cfg)
    if not kyc["tier_lite"]:
        raise WagerError("kyc_required", "Confirm your WhatsApp number and connect Discord before you withdraw.", 403)
    try:
        amount_kobo = int(amount_kobo)
    except (TypeError, ValueError):
        raise WagerError("amount_invalid", "Enter an amount.")
    if amount_kobo < cfg.min_withdrawal_kobo:
        raise WagerError("below_minimum_withdrawal", "That is below the minimum withdrawal.", 400,
                         min_withdrawal_kobo=cfg.min_withdrawal_kobo)
    if bank_account.user_id != user.pk:
        raise WagerError("bank_account_not_found", "We could not find that bank account.", 404)
    with transaction.atomic():
        account = WinningsAccount.objects.select_for_update().get_or_create(user=user)[0]
        if account.frozen:
            raise WagerError("winnings_frozen", "Your Winnings are frozen. Contact support.", 403)
        if Withdrawal.objects.filter(user=user, status__in=Withdrawal.OPEN_STATUSES).exists():
            raise WagerError("withdrawal_in_progress", "You already have a withdrawal in progress.", 409)
        wd = Withdrawal(user=user, account=account, bank_account=bank_account, amount_kobo=amount_kobo)
        if amount_kobo >= cfg.cosign_threshold_kobo:
            wd.status = Withdrawal.PENDING_COSIGN
        wd.save()
        hold(account, amount_kobo, LedgerEntry.WITHDRAWAL_HOLD, ref_kind="withdrawal", ref=wd.public_token,
             note=f"Withdrawal to {bank_account.bank_name} {bank_account.masked_number}")
    notify.withdrawal_requested(wd)
    return wd


def cancel_withdrawal(user, wd):
    """The player takes back a REQUESTED (not yet reviewed) withdrawal."""
    with transaction.atomic():
        w = Withdrawal.objects.select_for_update().select_related("account").get(pk=wd.pk)
        if w.user_id != user.pk:
            raise WagerError("withdrawal_not_found", "We could not find that withdrawal.", 404)
        if w.status not in (Withdrawal.REQUESTED, Withdrawal.PENDING_COSIGN):
            raise WagerError("withdrawal_not_cancellable", "This withdrawal is already being processed.", 409)
        account = WinningsAccount.objects.select_for_update().get(pk=w.account_id)
        release(account, w.amount_kobo, ref_kind="withdrawal", ref=w.public_token, note="Cancelled by you")
        w.status = Withdrawal.CANCELLED
        w.save(update_fields=["status", "updated_at"])
    return w


def approve_withdrawal(wd, *, by, cfg=None):
    """Admin (finance) approves: above the threshold it needs a co-sign by a DIFFERENT admin
    first; otherwise the Paystack transfer is submitted now. Money leaves Winnings only when the
    transfer is confirmed (mark_withdrawal_paid), so a failed transfer costs the player nothing."""
    from . import notify, payments

    cfg = cfg or WagerSettings.get()
    with transaction.atomic():
        w = Withdrawal.objects.select_for_update().select_related("bank_account", "account", "user").get(pk=wd.pk)
        if w.status == Withdrawal.PENDING_COSIGN:
            if w.reviewed_by_id is None:
                w.reviewed_by = by
                w.reviewed_at = timezone.now()
                w.save(update_fields=["reviewed_by", "reviewed_at", "updated_at"])
                return w   # first key: waits for the second
            if w.reviewed_by_id == by.pk:
                raise WagerError("cosign_same_admin", "A different admin has to co-sign this one.", 403)
            w.cosigned_by = by
            w.cosigned_at = timezone.now()
        elif w.status == Withdrawal.REQUESTED:
            if w.amount_kobo >= cfg.cosign_threshold_kobo:
                w.status = Withdrawal.PENDING_COSIGN
                w.reviewed_by = by
                w.reviewed_at = timezone.now()
                w.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
                return w
            w.reviewed_by = by
            w.reviewed_at = timezone.now()
        elif w.status == Withdrawal.FAILED:
            pass   # a retry
        else:
            raise WagerError("withdrawal_not_open", "This withdrawal is not awaiting approval.", 409)
        ok, detail = payments.submit_transfer(w)
        if not ok:
            w.status = Withdrawal.FAILED
            w.failure_reason = str(detail)[:240]
            w.save(update_fields=["status", "failure_reason", "reviewed_by", "reviewed_at",
                                  "cosigned_by", "cosigned_at", "updated_at"])
            raise WagerError("transfer_failed", f"Paystack refused the transfer: {detail}", 502)
        w.status = Withdrawal.APPROVED
        w.transfer_code = detail.get("transfer_code", "")[:40]
        w.transfer_reference = detail.get("reference", "")[:64]
        w.failure_reason = ""
        w.save(update_fields=["status", "transfer_code", "transfer_reference", "failure_reason",
                              "reviewed_by", "reviewed_at", "cosigned_by", "cosigned_at", "updated_at"])
        # Paystack transfers usually succeed at once; a transfer.success webhook (or the admin
        # poll) confirms and moves the money. Sandbox and OTP-less accounts answer "success"
        # synchronously, which mark_withdrawal_paid handles here.
        if detail.get("status") == "success":
            _mark_paid_locked(w)
    notify.withdrawal_approved(w)
    return w


def _mark_paid_locked(w):
    account = WinningsAccount.objects.select_for_update().get(pk=w.account_id)
    debit(w.user, w.amount_kobo, LedgerEntry.WITHDRAWAL_PAID, ref_kind="withdrawal", ref=w.public_token,
          note=f"Paid to {w.bank_account.bank_name} {w.bank_account.masked_number}", release_hold=True)
    w.status = Withdrawal.PAID
    w.paid_at = timezone.now()
    w.save(update_fields=["status", "paid_at", "updated_at"])


def mark_withdrawal_paid(wd):
    from . import notify
    with transaction.atomic():
        w = Withdrawal.objects.select_for_update().select_related("bank_account", "user").get(pk=wd.pk)
        if w.status == Withdrawal.PAID:
            return w
        if w.status != Withdrawal.APPROVED:
            raise WagerError("withdrawal_not_approved", "Only an approved withdrawal can be marked paid.", 409)
        _mark_paid_locked(w)
    notify.withdrawal_paid(w)
    return w


def mark_withdrawal_failed(wd, reason):
    with transaction.atomic():
        w = Withdrawal.objects.select_for_update().select_related("account").get(pk=wd.pk)
        if w.status != Withdrawal.APPROVED:
            return w
        w.status = Withdrawal.FAILED
        w.failure_reason = str(reason or "")[:240]
        w.save(update_fields=["status", "failure_reason", "updated_at"])
    return w


def reject_withdrawal(wd, *, by, reason):
    from . import notify
    if not (reason or "").strip():
        raise WagerError("reason_required", "Give the player a reason.")
    with transaction.atomic():
        w = Withdrawal.objects.select_for_update().select_related("account").get(pk=wd.pk)
        if w.status not in (Withdrawal.REQUESTED, Withdrawal.PENDING_COSIGN, Withdrawal.FAILED):
            raise WagerError("withdrawal_not_open", "This withdrawal is not awaiting a decision.", 409)
        account = WinningsAccount.objects.select_for_update().get(pk=w.account_id)
        release(account, w.amount_kobo, ref_kind="withdrawal", ref=w.public_token, note=f"Rejected: {reason.strip()[:200]}")
        w.status = Withdrawal.REJECTED
        w.reject_reason = reason.strip()[:240]
        w.reviewed_by = by
        w.reviewed_at = timezone.now()
        w.save(update_fields=["status", "reject_reason", "reviewed_by", "reviewed_at", "updated_at"])
    notify.withdrawal_rejected(w)
    return w


def adjust_winnings(user, *, direction, amount_kobo, reason, by, cfg=None):
    """An admin correction. Above the threshold it waits for a second admin."""
    from . import notify

    cfg = cfg or WagerSettings.get()
    if direction not in (Adjustment.CREDIT, Adjustment.DEBIT):
        raise WagerError("direction_invalid", "Direction must be CREDIT or DEBIT.")
    try:
        amount_kobo = int(amount_kobo)
    except (TypeError, ValueError):
        raise WagerError("amount_invalid", "Enter an amount.")
    if amount_kobo <= 0:
        raise WagerError("amount_invalid", "The amount must be positive.")
    if not (reason or "").strip():
        raise WagerError("reason_required", "Every adjustment needs a reason.")
    adj = Adjustment(user=user, direction=direction, amount_kobo=amount_kobo, reason=reason.strip()[:240],
                     submitted_by=by)
    if amount_kobo >= cfg.cosign_threshold_kobo:
        adj.status = Adjustment.PENDING_COSIGN
        adj.save()
        return adj
    with transaction.atomic():
        adj.save()
        _execute_adjustment_locked(adj, by)
    notify.adjustment_made(adj)
    return adj


def _execute_adjustment_locked(adj, by):
    if adj.direction == Adjustment.CREDIT:
        entry = credit(adj.user, adj.amount_kobo, LedgerEntry.ADJUSTMENT_CREDIT, ref_kind="adjustment",
                       ref=str(adj.pk), note=adj.reason, by=by)
    else:
        entry = debit(adj.user, adj.amount_kobo, LedgerEntry.ADJUSTMENT_DEBIT, ref_kind="adjustment",
                      ref=str(adj.pk), note=adj.reason, by=by)
    adj.status = Adjustment.EXECUTED
    adj.executed_at = timezone.now()
    adj.ledger_entry = entry
    adj.save(update_fields=["status", "executed_at", "ledger_entry"])


def cosign_adjustment(adj, *, by, approve, reason=""):
    from . import notify
    with transaction.atomic():
        a = Adjustment.objects.select_for_update().get(pk=adj.pk)
        if a.status != Adjustment.PENDING_COSIGN:
            raise WagerError("adjustment_not_pending", "This adjustment is not awaiting a co-sign.", 409)
        if a.submitted_by_id == by.pk:
            raise WagerError("cosign_same_admin", "The admin who submitted it cannot co-sign it.", 403)
        a.cosigned_by = by
        a.cosigned_at = timezone.now()
        if approve:
            a.save(update_fields=["cosigned_by", "cosigned_at"])
            _execute_adjustment_locked(a, by)
        else:
            a.status = Adjustment.REJECTED
            a.reject_reason = (reason or "").strip()[:240]
            a.save(update_fields=["cosigned_by", "cosigned_at", "status", "reject_reason"])
    if approve:
        notify.adjustment_made(a)
    return a


def set_frozen(user, *, frozen, reason, by):
    from . import notify
    account = account_for(user)
    account.frozen = bool(frozen)
    account.frozen_reason = (reason or "").strip()[:240] if frozen else ""
    account.frozen_by = by if frozen else None
    account.frozen_at = timezone.now() if frozen else None
    account.save(update_fields=["frozen", "frozen_reason", "frozen_by", "frozen_at", "updated_at"])
    notify.winnings_frozen(account) if frozen else notify.winnings_unfrozen(account)
    return account


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5 Limits and KYC
# ─────────────────────────────────────────────────────────────────────────────────────────────────
LOOSEN_DELAY = timedelta(hours=24)


def set_limits(user, *, caps):
    """caps: {field: kobo or 0}. Tightening (lower non-zero, or from 0 to a number) applies now;
    loosening (higher, or to 0) applies after LOOSEN_DELAY. Returns the row."""
    limits, _ = PlayerLimits.objects.get_or_create(user=user)
    now = timezone.now()
    immediate, deferred = [], []
    for f in PlayerLimits.CAP_FIELDS:
        if f not in caps:
            continue
        try:
            value = int(caps[f])
        except (TypeError, ValueError):
            raise WagerError("limit_invalid", "Limits must be whole amounts.")
        if value < 0:
            raise WagerError("limit_invalid", "Limits must be whole amounts.")
        current = getattr(limits, f)
        tightening = (current == 0 and value > 0) or (value != 0 and value < current)
        if value == current:
            setattr(limits, f"pending_{f}", None)
            deferred.append(f"pending_{f}")
        elif tightening:
            setattr(limits, f, value)
            setattr(limits, f"pending_{f}", None)
            immediate += [f, f"pending_{f}"]
        else:
            setattr(limits, f"pending_{f}", value)
            deferred.append(f"pending_{f}")
    if any(getattr(limits, f"pending_{f}") is not None for f in PlayerLimits.CAP_FIELDS):
        limits.pending_effective_at = now + LOOSEN_DELAY
    else:
        limits.pending_effective_at = None
    limits.save(update_fields=list(set(immediate + deferred)) + ["pending_effective_at", "updated_at"])
    return limits


def set_cooloff(user, *, days):
    limits, _ = PlayerLimits.objects.get_or_create(user=user)
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise WagerError("period_invalid", "Pick a number of days.")
    if days < 1 or days > 30:
        raise WagerError("period_invalid", "A cool-off is between 1 and 30 days.")
    limits.cooloff_until = timezone.now() + timedelta(days=days)
    limits.save(update_fields=["cooloff_until", "updated_at"])
    return limits


def set_self_exclusion(user, *, months):
    """Cannot be shortened or lifted by the player once set (that is the point)."""
    limits, _ = PlayerLimits.objects.get_or_create(user=user)
    try:
        months = int(months)
    except (TypeError, ValueError):
        raise WagerError("period_invalid", "Pick a number of months.")
    if months < 1 or months > 60:
        raise WagerError("period_invalid", "Self-exclusion is between 1 and 60 months.")
    until = timezone.now() + timedelta(days=30 * months)
    if limits.self_excluded_until and limits.self_excluded_until > until:
        raise WagerError("exclusion_cannot_shorten", "An existing self-exclusion cannot be shortened.", 409)
    limits.self_excluded_until = until
    limits.self_excluded_at = timezone.now()
    limits.save(update_fields=["self_excluded_until", "self_excluded_at", "updated_at"])
    return limits


def kyc_state(user, cfg=None):
    """The three facts, read live, plus the tier. Never cached: a changed number or an unlinked
    Discord shows at once."""
    cfg = cfg or WagerSettings.get()
    row, _ = KycStatus.objects.get_or_create(user=user)
    profile = canonical_profile(user)
    number = (getattr(profile, "whatsapp_number", "") or "") if profile else ""
    dob = getattr(profile, "date_of_birth", None) if profile else None
    whatsapp_ok = bool(row.whatsapp_verified_at and number and row.whatsapp_verified_number == number)
    discord_ok = bool(getattr(user, "discord_id", None))
    years = age_on(dob)
    age_ok = years is not None and years >= cfg.min_age
    return {
        "whatsapp_number": number,
        "whatsapp_verified": whatsapp_ok,
        "whatsapp_verified_at": row.whatsapp_verified_at.isoformat() if whatsapp_ok else None,
        "discord_linked": discord_ok,
        "date_of_birth_on_file": bool(dob),
        "age_ok": age_ok,
        "min_age": cfg.min_age,
        "tier_lite": whatsapp_ok and discord_ok,
        "forced": bool(row.forced_at),
    }


def kyc_start_whatsapp(user):
    """Send the code to the profile number through the existing 2FA WhatsApp method."""
    from afc_auth import two_factor

    profile = canonical_profile(user)
    number = (getattr(profile, "whatsapp_number", "") or "") if profile else ""
    if not number:
        raise WagerError("whatsapp_number_required", "Add a WhatsApp number to your profile first.")
    result = two_factor.issue_challenge(user, purpose="wager_kyc", method_code="whatsapp")
    challenge = result.get("challenge")
    if challenge is None:
        raise WagerError("kyc_rate_limited", "Too many codes sent. Try again later.", 429,
                         retry_after=result.get("retry_after", 0))
    return {
        "challenge_token": challenge.token,
        "sent": result.get("sent", False),
        "destination": result.get("destination", ""),
        "retry_after": result.get("retry_after", 0),
    }


def kyc_verify_whatsapp(user, *, challenge_token, code):
    from afc_auth import two_factor

    challenge = two_factor.get_challenge(challenge_token, purpose="wager_kyc")
    if challenge is None or challenge.user_id != user.pk:
        raise WagerError("kyc_challenge_invalid", "That code has expired. Send a new one.", 400)
    ok, reason = two_factor.verify_code(challenge, str(code or "").strip())
    if not ok:
        raise WagerError(f"kyc_code_{reason or 'invalid'}", "That code is not right.", 400)
    profile = canonical_profile(user)
    number = (getattr(profile, "whatsapp_number", "") or "") if profile else ""
    row, _ = KycStatus.objects.get_or_create(user=user)
    row.whatsapp_verified_at = timezone.now()
    row.whatsapp_verified_number = number
    row.save(update_fields=["whatsapp_verified_at", "whatsapp_verified_number", "updated_at"])
    return kyc_state(user)


def kyc_force(user, *, by, verified, reason):
    if not (reason or "").strip():
        raise WagerError("reason_required", "Give a reason for the override.")
    profile = canonical_profile(user)
    number = (getattr(profile, "whatsapp_number", "") or "") if profile else ""
    row, _ = KycStatus.objects.get_or_create(user=user)
    if verified:
        row.whatsapp_verified_at = timezone.now()
        row.whatsapp_verified_number = number
    else:
        row.whatsapp_verified_at = None
        row.whatsapp_verified_number = ""
    row.forced_by = by
    row.forced_at = timezone.now()
    row.force_reason = reason.strip()[:240]
    row.save()
    return kyc_state(user)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6 Market creation (admin), shared by create and edit
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def build_options(market, template, raw_options):
    """[{label, team_id?, player_id?, side?}] -> MarketOption rows. For over / under the two
    sides are made from the line. Custom labels are required for the custom source."""
    from afc_auth.models import User
    from afc_tournament_and_scrims.models import TournamentTeam

    if template.option_source == MarketTemplate.OPTIONS_OVER_UNDER:
        if not market.over_under_line:
            raise WagerError("line_required", "An over / under market needs a line.")
        return [
            MarketOption(market=market, label=f"Over {market.over_under_line}", side="over", sort_order=0),
            MarketOption(market=market, label=f"Under {market.over_under_line}", side="under", sort_order=1),
        ]
    if not isinstance(raw_options, list) or len(raw_options) < 2:
        raise WagerError("options_required", "A market needs at least two options.")
    rows = []
    seen = set()
    for i, item in enumerate(raw_options):
        if not isinstance(item, dict):
            raise WagerError("options_invalid", "Each option needs a label.")
        label = str(item.get("label") or "").strip()[:80]
        team = player = None
        if template.option_source == MarketTemplate.OPTIONS_TEAMS:
            team = TournamentTeam.objects.filter(pk=item.get("team_id"), event=market.event).first()
            if team is None:
                raise WagerError("options_invalid", "Every option must be a team registered for this event.")
            label = label or team.display_name
        elif template.option_source == MarketTemplate.OPTIONS_PLAYERS:
            player = User.objects.filter(pk=item.get("player_id")).first()
            if player is None:
                raise WagerError("options_invalid", "Every option must be a player.")
            label = label or player.username
        if not label:
            raise WagerError("options_invalid", "Each option needs a label.")
        key = label.lower()
        if key in seen:
            raise WagerError("options_duplicate", f"'{label}' appears twice.")
        seen.add(key)
        rows.append(MarketOption(market=market, label=label, team=team, player=player, sort_order=i))
    return rows
