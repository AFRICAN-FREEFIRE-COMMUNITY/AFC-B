"""
afc_wager.serializers - one dict per shape (R24). The player pages, the admin pages and the tests
all read these; a field added here appears everywhere the same day.

Nothing here carries a numeric primary key to a player: markets by slug, wagers and withdrawals by
public token, options by id ONLY because an option id is what a player posts back when placing
(it names an answer, not a record with a page). Admin shapes add the ids the CMS needs.
"""
from django.utils import timezone

from .models import LedgerEntry, Market, Wager, Withdrawal


def _iso(dt):
    return dt.isoformat() if dt else None


def option_dict(o, *, pool_kobo=None):
    pool = o.cached_pool_kobo
    share = (pool * 10000 // pool_kobo) / 100 if pool_kobo else 0.0
    return {
        "id": o.pk,
        "label": o.label,
        "side": o.side or None,
        "team": o.team.display_name if o.team_id and o.team else None,
        "player": o.player.username if o.player_id and o.player else None,
        "pool_kobo": pool,
        "line_count": o.cached_line_count,
        "share_percent": share,
    }


def market_summary(m, *, now=None, my_stake_kobo=None):
    now = now or timezone.now()
    return {
        "slug": m.slug,
        "title": m.title,
        "status": m.status,
        "is_open_for_stakes": m.is_open_for_stakes(now),
        "event": {"slug": m.event.slug, "name": m.event.event_name},
        "template": m.template.code,
        "featured": m.featured,
        "image": m.image.url if m.image else None,
        "open_at": _iso(m.open_at),
        "lock_at": _iso(m.lock_at),
        "settled_at": _iso(m.settled_at),
        "pool_kobo": m.cached_pool_kobo,
        "wager_count": m.cached_wager_count,
        "rake_bps": m.rake_bps,
        "cancel_fee_bps": m.cancel_fee_bps,
        "min_stake_kobo": m.min_stake_kobo,
        "max_stake_per_user_kobo": m.max_stake_per_user_kobo,
        "settled_option": m.settled_option.label if m.settled_option_id and m.settled_option else None,
        "my_stake_kobo": my_stake_kobo,
    }


def market_detail(m, *, now=None, my_wagers=None):
    d = market_summary(m, now=now)
    d.update({
        "description": m.description,
        "rules_text": m.rules_text,
        "visibility": m.visibility,
        "stage": m.stage.stage_name if m.stage_id and m.stage else None,
        "match_number": m.match.match_number if m.match_id and m.match else None,
        "over_under_line": m.over_under_line,
        "max_pool_kobo": m.max_pool_kobo,
        "options": [option_dict(o, pool_kobo=m.cached_pool_kobo) for o in m.options.all()],
        "settled_option_id": m.settled_option_id,
        "void_reason": m.void_reason if m.status == Market.VOID else "",
        "my_wagers": [wager_dict(w) for w in (my_wagers or [])],
    })
    return d


def wager_dict(w, *, with_market=True):
    d = {
        "token": w.public_token,
        "status": w.status,
        "total_stake_kobo": w.total_stake_kobo,
        "payout_kobo": w.payout_kobo,
        "refund_kobo": w.refund_kobo,
        "cancel_fee_kobo": w.cancel_fee_kobo,
        "created_at": _iso(w.created_at),
        "paid_at": _iso(w.paid_at),
        "cancelled_at": _iso(w.cancelled_at),
        "payment_expires_at": _iso(w.payment_expires_at) if w.status == Wager.PENDING_PAYMENT else None,
        "payment_url": w.paystack_authorization_url if w.status == Wager.PENDING_PAYMENT else None,
        "lines": [
            {"option_id": l.option_id, "option": l.option.label, "stake_kobo": l.stake_kobo,
             "outcome": l.outcome, "payout_kobo": l.payout_kobo}
            for l in w.lines.select_related("option").all()
        ],
    }
    if with_market:
        d["market"] = {"slug": w.market.slug, "title": w.market.title, "status": w.market.status,
                       "lock_at": _iso(w.market.lock_at),
                       "settled_option": w.market.settled_option.label
                       if w.market.settled_option_id and w.market.settled_option else None}
    return d


def ledger_dict(e, *, label="", reason=""):
    """One ledger line. `note` is the English audit sentence the CMS reads; the player's page
    phrases the row itself from `kind`, so it also gets `label` (the market title or the bank
    the money went to) and `reason` (a human-written sentence: an admin's adjustment reason or
    a rejection reason), both plain data. Use ledger_rows() to fill those in for a list."""
    return {
        "id": e.pk,
        "kind": e.kind,
        "amount_kobo": e.amount_kobo,
        "held_delta_kobo": e.held_delta_kobo,
        "balance_after_kobo": e.balance_after_kobo,
        "ref_kind": e.ref_kind,
        "ref": e.ref,
        "note": e.note,
        "label": label,
        "reason": reason,
        "created_at": _iso(e.created_at),
    }


def ledger_rows(rows):
    """ledger_dict for a list, with label and reason resolved in three queries rather than one
    per row: wager tokens to market titles, market slugs to titles, withdrawal tokens to the
    bank (and the rejection reason), adjustment ids to their reason."""
    from .models import Adjustment, Market, Wager, Withdrawal

    rows = list(rows)
    by_kind = {}
    for e in rows:
        by_kind.setdefault(e.ref_kind, set()).add(e.ref)
    titles = {}
    if by_kind.get("wager"):
        titles.update({w.public_token: w.market.title for w in
                       Wager.objects.filter(public_token__in=by_kind["wager"]).select_related("market")})
    if by_kind.get("market"):
        titles.update(dict(Market.objects.filter(slug__in=by_kind["market"]).values_list("slug", "title")))
    banks, reasons = {}, {}
    if by_kind.get("withdrawal"):
        for w in Withdrawal.objects.filter(public_token__in=by_kind["withdrawal"]).select_related("bank_account"):
            banks[w.public_token] = f"{w.bank_account.bank_name} {w.bank_account.masked_number}"
            reasons[w.public_token] = w.reject_reason or w.failure_reason or ""
    if by_kind.get("adjustment"):
        ids = [int(r) for r in by_kind["adjustment"] if str(r).isdigit()]
        reasons.update({str(a.pk): a.reason for a in Adjustment.objects.filter(pk__in=ids)})
    out = []
    for e in rows:
        if e.ref_kind in ("wager", "market"):
            out.append(ledger_dict(e, label=titles.get(e.ref, "")))
        elif e.ref_kind == "withdrawal":
            out.append(ledger_dict(e, label=banks.get(e.ref, ""),
                                   reason=reasons.get(e.ref, "") if e.kind == e.WITHDRAWAL_RELEASED else ""))
        elif e.ref_kind == "adjustment":
            out.append(ledger_dict(e, reason=reasons.get(e.ref, "")))
        else:
            out.append(ledger_dict(e))
    return out


def account_dict(a, *, pending_withdrawal=None):
    return {
        "balance_kobo": a.balance_kobo,
        "held_kobo": a.held_kobo,
        "available_kobo": a.available_kobo,
        "frozen": a.frozen,
        "frozen_reason": a.frozen_reason if a.frozen else "",
        "pending_withdrawal": withdrawal_dict(pending_withdrawal) if pending_withdrawal else None,
    }


def bank_account_dict(b):
    return {"id": b.pk, "bank_code": b.bank_code, "bank_name": b.bank_name,
            "account_number_masked": b.masked_number, "account_name": b.account_name,
            "is_default": b.is_default}


def withdrawal_dict(w, *, for_staff=False):
    d = {
        "token": w.public_token,
        "status": w.status,
        "amount_kobo": w.amount_kobo,
        "bank": {"bank_name": w.bank_account.bank_name, "account_number_masked": w.bank_account.masked_number,
                 "account_name": w.bank_account.account_name},
        "reject_reason": w.reject_reason,
        "failure_reason": w.failure_reason if for_staff else "",
        "created_at": _iso(w.created_at),
        "reviewed_at": _iso(w.reviewed_at),
        "paid_at": _iso(w.paid_at),
        "transfer_reference": w.transfer_reference,
    }
    if for_staff:
        d["user"] = w.user.username
        d["bank"]["account_number"] = w.bank_account.account_number
        d["reviewed_by"] = w.reviewed_by.username if w.reviewed_by_id and w.reviewed_by else None
        d["cosigned_by"] = w.cosigned_by.username if w.cosigned_by_id and w.cosigned_by else None
        d["needs_cosign"] = w.status == Withdrawal.PENDING_COSIGN
    return d


def limits_dict(limits, caps):
    return {
        "daily_stake_cap_kobo": limits.daily_stake_cap_kobo,
        "weekly_stake_cap_kobo": limits.weekly_stake_cap_kobo,
        "daily_loss_cap_kobo": limits.daily_loss_cap_kobo,
        "effective": caps,
        "pending": {
            "daily_stake_cap_kobo": limits.pending_daily_stake_cap_kobo,
            "weekly_stake_cap_kobo": limits.pending_weekly_stake_cap_kobo,
            "daily_loss_cap_kobo": limits.pending_daily_loss_cap_kobo,
            "effective_at": _iso(limits.pending_effective_at),
        },
        "cooloff_until": _iso(limits.cooloff_until),
        "self_excluded_until": _iso(limits.self_excluded_until),
    }


def settings_dict(cfg):
    return {f: getattr(cfg, f) for f in cfg.EDITABLE_FIELDS} | {"updated_at": _iso(cfg.updated_at)}


def template_dict(t):
    return {"id": t.pk, "code": t.code, "name": t.name, "description": t.description,
            "option_source": t.option_source, "settle_rule": t.settle_rule, "needs_match": t.needs_match,
            "is_active": t.is_active, "sort_order": t.sort_order}


# ── admin shapes ────────────────────────────────────────────────────────────────────────────────
def admin_market_row(m):
    d = market_summary(m)
    d.update({
        "id": m.pk,
        "visibility": m.visibility,
        "suggested_option": m.suggested_option.label if m.suggested_option_id and m.suggested_option else None,
        "suggested_at": _iso(m.suggested_at),
        "locked_at": _iso(m.locked_at),
        "created_by": m.created_by.username if m.created_by_id and m.created_by else None,
        "created_at": _iso(m.created_at),
        "match_id": m.match_id,
        "stage_id": m.stage_id,
        "event_id": m.event_id,
    })
    return d


def admin_market_detail(m):
    d = market_detail(m)
    d.update(admin_market_row(m))
    d["suggestion_evidence"] = m.suggestion_evidence or {}
    d["suggested_option_id"] = m.suggested_option_id
    d["template_detail"] = template_dict(m.template)
    settlement = getattr(m, "settlement", None) if m.is_terminal else None
    d["settlement"] = settlement_dict(settlement) if settlement else None
    return d


def settlement_dict(s):
    return {
        "resolution": s.resolution,
        "final_option": s.final_option.label if s.final_option_id and s.final_option else None,
        "suggested_option": s.suggested_option.label if s.suggested_option_id and s.suggested_option else None,
        "override_reason": s.override_reason,
        "evidence": s.evidence or {},
        "pool_kobo": s.pool_kobo, "rake_kobo": s.rake_kobo, "net_pool_kobo": s.net_pool_kobo,
        "dust_kobo": s.dust_kobo, "paid_total_kobo": s.paid_total_kobo,
        "refund_total_kobo": s.refund_total_kobo, "winners_count": s.winners_count,
        "confirmed_by": s.confirmed_by.username if s.confirmed_by_id and s.confirmed_by else None,
        "confirmed_at": _iso(s.confirmed_at),
    }


def admin_wager_row(w):
    d = wager_dict(w, with_market=False)
    d["user"] = w.user.username
    return d


def admin_account_row(a, *, kyc=None, limits=None):
    return {
        "username": a.user.username,
        "email": a.user.email,
        "balance_kobo": a.balance_kobo,
        "held_kobo": a.held_kobo,
        "frozen": a.frozen,
        "frozen_reason": a.frozen_reason,
        "kyc": kyc,
        "limits": limits,
        "updated_at": _iso(a.updated_at),
    }


def adjustment_dict(adj):
    return {
        "id": adj.pk,
        "user": adj.user.username,
        "direction": adj.direction,
        "amount_kobo": adj.amount_kobo,
        "reason": adj.reason,
        "status": adj.status,
        "submitted_by": adj.submitted_by.username if adj.submitted_by_id and adj.submitted_by else None,
        "cosigned_by": adj.cosigned_by.username if adj.cosigned_by_id and adj.cosigned_by else None,
        "reject_reason": adj.reject_reason,
        "created_at": _iso(adj.created_at),
        "executed_at": _iso(adj.executed_at),
    }


def admin_ledger_row(e, base=None):
    """A ledger line for staff: the player's row (with label and reason when `base` comes from
    ledger_rows) plus who the account belongs to and which admin wrote it."""
    d = dict(base) if base is not None else ledger_dict(e)
    d["user"] = e.account.user.username if e.account_id and e.account else None
    d["is_house"] = e.account_id is None
    d["created_by"] = e.created_by.username if e.created_by_id and e.created_by else None
    return d


def admin_ledger_rows(rows):
    rows = list(rows)
    return [admin_ledger_row(e, base) for e, base in zip(rows, ledger_rows(rows))]
