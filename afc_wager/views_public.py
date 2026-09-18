"""
afc_wager.views_public - what a visitor and a signed-in player can do. Mounted at `wagers/`.

Every view here answers `{"message", "code", ...}` on a refusal (R35 / R44), reads only the
session user's own rows (R58), and lets services.py do every write. The list and the market
page answer signed out (R25); placing, cancelling, Winnings, withdrawals, limits and KYC need a
session (401 `auth_required`).

ENDPOINTS (consumed by frontend app/(user)/wagers and app/(user)/winnings)
    GET  wagers/settings/                     the public dials + the kill switch
    GET  wagers/markets/                      ?status=open|locked|settled|mine  ?event=<slug>  ?q=
    GET  wagers/markets/<slug>/               detail + my wagers on it
    POST wagers/markets/<slug>/place/         {lines:[{option_id, stake_kobo}]} -> {wager, payment_url}
    GET  wagers/payments/verify/?reference=   after Paystack sends the player back
    GET  wagers/mine/                         my wagers, newest first, with totals
    POST wagers/<token>/cancel/               pre-lock cancel
    GET  wagers/winnings/                     balance + pending withdrawal + KYC + limits
    GET  wagers/winnings/ledger/              ?limit ?offset
    GET  wagers/winnings/banks/               Paystack bank list
    GET/POST wagers/winnings/bank-accounts/   saved payout accounts / add one (resolved with Paystack)
    POST wagers/winnings/withdraw/            {amount_kobo, bank_account_id}
    POST wagers/winnings/withdrawals/<token>/cancel/
    GET/POST wagers/limits/                   read / set caps
    POST wagers/limits/cooloff/               {days}
    POST wagers/limits/self-exclude/          {months}
    GET  wagers/kyc/                          the three facts
    POST wagers/kyc/whatsapp/start/           sends the code (rate limited)
    POST wagers/kyc/whatsapp/verify/          {challenge_token, code}
"""
import logging

from django.core.cache import cache
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from afc_auth.slugs import resolve_or_redirect
from afc_auth.views import validate_token
from afc_tournament_and_scrims.models import Event

from . import payments, serializers as ser, services
from .models import (
    LedgerEntry, Market, PayoutBankAccount, Wager, WagerSettings, Withdrawal, WinningsAccount,
)
from .services import WagerError

logger = logging.getLogger(__name__)


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────
def _actor(request):
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ", 1)[1].strip())


def _require_user(request):
    user = _actor(request)
    if not user:
        return None, Response({"message": "Sign in to do that.", "code": "auth_required"},
                              status=status.HTTP_401_UNAUTHORIZED)
    return user, None


def _refused(exc):
    return Response(exc.as_dict(), status=exc.status)


def _paginate(request, qs, default=20, cap=100):
    try:
        limit = min(max(int(request.GET.get("limit", default)), 1), cap)
    except (TypeError, ValueError):
        limit = default
    try:
        offset = max(int(request.GET.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    total = qs.count()
    rows = list(qs[offset:offset + limit])
    return rows, {"has_more": offset + limit < total, "next_offset": offset + limit if offset + limit < total else None,
                  "total_count": total}


def _throttle(key, limit, window_seconds):
    """R59: a per-user, per-action fixed window on the Django cache. True when over the limit."""
    cache_key = f"wager:throttle:{key}"
    try:
        count = cache.get(cache_key, 0)
        if count >= limit:
            return True
        cache.set(cache_key, count + 1, timeout=window_seconds)
    except Exception:  # noqa: BLE001 - a cache outage must not block the site
        return False
    return False


def _too_many():
    return Response({"message": "Too many attempts. Wait a minute and try again.", "code": "rate_limited"},
                    status=status.HTTP_429_TOO_MANY_REQUESTS)


def _market_or_404(ref, user):
    """A market by slug (or an old slug, answered as moved). Drafts and signed-in-only markets are
    invisible to the people they are invisible to."""
    market, moved_to = resolve_or_redirect(Market, ref, "slug")
    if market is None:
        return None, Response({"message": "We could not find that market.", "code": "market_not_found"},
                              status=status.HTTP_404_NOT_FOUND)
    if moved_to and moved_to != ref:
        return None, Response({"status": "moved", "slug": moved_to}, status=status.HTTP_200_OK)
    if market.status == Market.DRAFT:
        return None, Response({"message": "We could not find that market.", "code": "market_not_found"},
                              status=status.HTTP_404_NOT_FOUND)
    if market.visibility == Market.VISIBILITY_SIGNED_IN and user is None:
        return None, Response({"message": "Sign in to see this market.", "code": "auth_required"},
                              status=status.HTTP_401_UNAUTHORIZED)
    return market, None


# ── settings ────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def public_settings(request):
    cfg = WagerSettings.get()
    return Response({
        "wagering_enabled": cfg.wagering_enabled,
        "maintenance_message": cfg.maintenance_message,
        "rake_bps": cfg.rake_bps, "cancel_fee_bps": cfg.cancel_fee_bps,
        "min_stake_kobo": cfg.min_stake_kobo, "max_stake_per_user_kobo": cfg.max_stake_per_user_kobo,
        "min_withdrawal_kobo": cfg.min_withdrawal_kobo, "min_age": cfg.min_age,
        "payment_expiry_minutes": cfg.payment_expiry_minutes,
    })


# ── markets ─────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def list_markets(request):
    user = _actor(request)
    now = timezone.now()
    tab = (request.GET.get("status") or "open").lower()
    qs = Market.objects.select_related("event", "template").exclude(status=Market.DRAFT)
    if user is None:
        qs = qs.filter(visibility=Market.VISIBILITY_PUBLIC)
    if tab == "open":
        qs = qs.filter(status=Market.OPEN, lock_at__gt=now)
    elif tab == "locked":
        qs = qs.filter(Q(status__in=(Market.LOCKED, Market.PENDING_SETTLEMENT)) | Q(status=Market.OPEN, lock_at__lte=now))
    elif tab == "settled":
        qs = qs.filter(status__in=(Market.SETTLED, Market.VOID))
    elif tab == "mine":
        if user is None:
            return Response({"message": "Sign in to see your wagers.", "code": "auth_required"},
                            status=status.HTTP_401_UNAUTHORIZED)
        qs = qs.filter(wagers__user=user).exclude(wagers__status__in=(Wager.EXPIRED,)).distinct()
    event_slug = (request.GET.get("event") or "").strip()
    if event_slug:
        qs = qs.filter(event__slug=event_slug)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(event__event_name__icontains=q))
    order = "-featured", ("lock_at" if tab == "open" else "-lock_at")
    qs = qs.order_by(*order)
    rows, page = _paginate(request, qs, default=24)
    my_stakes = {}
    if user is not None and rows:
        for r in (Wager.objects.filter(user=user, market__in=rows, status__in=(Wager.ACTIVE, Wager.WON, Wager.LOST, Wager.REFUNDED))
                  .values("market_id").annotate(s=Sum("total_stake_kobo"))):
            my_stakes[r["market_id"]] = r["s"]
    events = (Event.objects.filter(wager_markets__isnull=False).exclude(wager_markets__status=Market.DRAFT)
              .distinct().values("slug", "event_name").order_by("event_name"))
    return Response({
        "results": [ser.market_summary(m, now=now, my_stake_kobo=my_stakes.get(m.pk)) for m in rows],
        "events": [{"slug": e["slug"], "name": e["event_name"]} for e in events],
        **page,
    })


@api_view(["GET"])
@authentication_classes([])
def market_detail(request, slug):
    user = _actor(request)
    market, err = _market_or_404(slug, user)
    if err:
        return err
    my = []
    if user is not None:
        my = list(Wager.objects.filter(user=user, market=market).exclude(status=Wager.EXPIRED)
                  .select_related("market").order_by("-created_at"))
    return Response(ser.market_detail(market, my_wagers=my))


@api_view(["POST"])
@authentication_classes([])
def place(request, slug):
    user, err = _require_user(request)
    if err:
        return err
    market, err = _market_or_404(slug, user)
    if err:
        return err
    if _throttle(f"place:{user.pk}", 10, 60):
        return _too_many()
    try:
        wager = services.place_wager(user, market, request.data.get("lines"), request=request)
    except WagerError as exc:
        return _refused(exc)
    return Response({
        "message": "Pay to confirm your stake.",
        "wager": ser.wager_dict(wager),
        "payment_url": wager.paystack_authorization_url,
        "payment_expires_at": wager.payment_expires_at.isoformat(),
    }, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@authentication_classes([])
def verify_payment(request):
    """The browser comes back from Paystack with ?reference=. Verify with Paystack and activate.
    Idempotent with the webhook. Only the wager's owner may ask (R58)."""
    user, err = _require_user(request)
    if err:
        return err
    reference = (request.GET.get("reference") or "").strip()
    wager = Wager.objects.filter(paystack_reference=reference, user=user).select_related("market").first()
    if wager is None:
        return Response({"message": "We could not find that payment.", "code": "payment_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    if wager.status in (Wager.PENDING_PAYMENT, Wager.EXPIRED):
        ok, charge_id, amount = payments.verify_stake(reference)
        if ok and (amount is None or amount == wager.total_stake_kobo):
            wager = services.activate_wager(wager, charge_id=charge_id)
        else:
            return Response({"message": "The payment has not gone through yet.", "code": "payment_pending",
                             "wager": ser.wager_dict(wager)}, status=status.HTTP_202_ACCEPTED)
    return Response({"message": "Your stake is in.", "wager": ser.wager_dict(wager)})


@api_view(["GET"])
@authentication_classes([])
def my_wagers(request):
    user, err = _require_user(request)
    if err:
        return err
    qs = Wager.objects.filter(user=user).exclude(status=Wager.EXPIRED).select_related("market", "market__settled_option")
    rows, page = _paginate(request, qs)
    totals = Wager.objects.filter(user=user).aggregate(
        staked=Sum("total_stake_kobo", filter=Q(status__in=(Wager.ACTIVE, Wager.WON, Wager.LOST, Wager.REFUNDED))),
        won=Sum("payout_kobo", filter=Q(status=Wager.WON)),
        refunded=Sum("refund_kobo", filter=Q(status__in=(Wager.REFUNDED, Wager.CANCELLED))),
        lost=Sum("total_stake_kobo", filter=Q(status=Wager.LOST)),
    )
    return Response({
        "results": [ser.wager_dict(w) for w in rows],
        "totals": {k: (v or 0) for k, v in totals.items()},
        **page,
    })


@api_view(["POST"])
@authentication_classes([])
def cancel(request, token):
    user, err = _require_user(request)
    if err:
        return err
    if _throttle(f"cancel:{user.pk}", 10, 60):
        return _too_many()
    wager = Wager.objects.filter(public_token=token, user=user).select_related("market").first()
    if wager is None:
        return Response({"message": "We could not find that wager.", "code": "wager_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    try:
        wager = services.cancel_wager(user, wager)
    except WagerError as exc:
        return _refused(exc)
    return Response({"message": "Wager cancelled. The refund is in your Winnings.", "wager": ser.wager_dict(wager)})


# ── winnings ────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def winnings(request):
    user, err = _require_user(request)
    if err:
        return err
    account = services.account_for(user)
    pending = Withdrawal.objects.filter(user=user, status__in=Withdrawal.OPEN_STATUSES).select_related("bank_account").first()
    limits, caps = services.effective_limits(user)
    cfg = WagerSettings.get()
    return Response({
        "account": ser.account_dict(account, pending_withdrawal=pending),
        "kyc": services.kyc_state(user, cfg),
        "limits": ser.limits_dict(limits, caps),
        "min_withdrawal_kobo": cfg.min_withdrawal_kobo,
        "bank_accounts": [ser.bank_account_dict(b) for b in PayoutBankAccount.objects.filter(user=user).order_by("-is_default", "-created_at")],
        "recent": ser.ledger_rows(LedgerEntry.objects.filter(account=account)[:8]),
    })


@api_view(["GET"])
@authentication_classes([])
def ledger(request):
    user, err = _require_user(request)
    if err:
        return err
    account = services.account_for(user)
    qs = LedgerEntry.objects.filter(account=account)
    kind = (request.GET.get("kind") or "").strip().upper()
    if kind:
        qs = qs.filter(kind=kind)
    rows, page = _paginate(request, qs, default=25)
    return Response({"results": ser.ledger_rows(rows), **page})


@api_view(["GET"])
@authentication_classes([])
def banks(request):
    user, err = _require_user(request)
    if err:
        return err
    return Response({"results": payments.list_banks()})


@api_view(["GET", "POST"])
@authentication_classes([])
def bank_accounts(request):
    user, err = _require_user(request)
    if err:
        return err
    if request.method == "GET":
        return Response({"results": [ser.bank_account_dict(b) for b in PayoutBankAccount.objects.filter(user=user)]})
    if _throttle(f"bank:{user.pk}", 5, 300):
        return _too_many()
    try:
        row = services.save_bank_account(user, bank_code=request.data.get("bank_code"),
                                         account_number=request.data.get("account_number"))
    except WagerError as exc:
        return _refused(exc)
    return Response({"message": f"Account confirmed: {row.account_name}.", "bank_account": ser.bank_account_dict(row)},
                    status=status.HTTP_201_CREATED)


@api_view(["POST"])
@authentication_classes([])
def withdraw(request):
    user, err = _require_user(request)
    if err:
        return err
    if _throttle(f"withdraw:{user.pk}", 10, 600):
        return _too_many()
    bank = PayoutBankAccount.objects.filter(user=user, pk=request.data.get("bank_account_id")).first()
    if bank is None:
        return Response({"message": "Pick a bank account to pay into.", "code": "bank_account_required"},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        wd = services.request_withdrawal(user, amount_kobo=request.data.get("amount_kobo"), bank_account=bank)
    except WagerError as exc:
        return _refused(exc)
    return Response({"message": "Withdrawal requested. AFC will approve it shortly.",
                     "withdrawal": ser.withdrawal_dict(wd)}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@authentication_classes([])
def cancel_withdrawal(request, token):
    user, err = _require_user(request)
    if err:
        return err
    wd = Withdrawal.objects.filter(public_token=token, user=user).first()
    if wd is None:
        return Response({"message": "We could not find that withdrawal.", "code": "withdrawal_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    try:
        wd = services.cancel_withdrawal(user, wd)
    except WagerError as exc:
        return _refused(exc)
    return Response({"message": "Withdrawal cancelled.", "withdrawal": ser.withdrawal_dict(wd)})


# ── limits ──────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "POST"])
@authentication_classes([])
def limits(request):
    user, err = _require_user(request)
    if err:
        return err
    if request.method == "POST":
        caps = {f: request.data.get(f) for f in ("daily_stake_cap_kobo", "weekly_stake_cap_kobo", "daily_loss_cap_kobo")
                if f in request.data}
        try:
            services.set_limits(user, caps=caps)
        except WagerError as exc:
            return _refused(exc)
    row, caps = services.effective_limits(user)
    return Response({"message": "Limits saved." if request.method == "POST" else "", "limits": ser.limits_dict(row, caps)})


@api_view(["POST"])
@authentication_classes([])
def cooloff(request):
    user, err = _require_user(request)
    if err:
        return err
    try:
        services.set_cooloff(user, days=request.data.get("days"))
    except WagerError as exc:
        return _refused(exc)
    row, caps = services.effective_limits(user)
    return Response({"message": "Cool-off set.", "limits": ser.limits_dict(row, caps)})


@api_view(["POST"])
@authentication_classes([])
def self_exclude(request):
    user, err = _require_user(request)
    if err:
        return err
    try:
        services.set_self_exclusion(user, months=request.data.get("months"))
    except WagerError as exc:
        return _refused(exc)
    row, caps = services.effective_limits(user)
    return Response({"message": "You are excluded from wagering for the period you chose.",
                     "limits": ser.limits_dict(row, caps)})


# ── KYC ─────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def kyc(request):
    user, err = _require_user(request)
    if err:
        return err
    return Response(services.kyc_state(user))


@api_view(["POST"])
@authentication_classes([])
def kyc_whatsapp_start(request):
    user, err = _require_user(request)
    if err:
        return err
    if _throttle(f"kyc:{user.pk}", 5, 3600):
        return _too_many()
    try:
        out = services.kyc_start_whatsapp(user)
    except WagerError as exc:
        return _refused(exc)
    return Response({"message": "Code sent to your WhatsApp." if out["sent"] else "A code is already on its way.", **out})


@api_view(["POST"])
@authentication_classes([])
def kyc_whatsapp_verify(request):
    user, err = _require_user(request)
    if err:
        return err
    try:
        state = services.kyc_verify_whatsapp(user, challenge_token=str(request.data.get("challenge_token") or ""),
                                             code=request.data.get("code"))
    except WagerError as exc:
        return _refused(exc)
    return Response({"message": "WhatsApp number confirmed.", **state})
