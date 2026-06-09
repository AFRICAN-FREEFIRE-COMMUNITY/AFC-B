"""
afc_shop/connect.py
================================================================================
STRIPE CONNECT VENDOR PAYOUTS (marketplace Phase B3, spec:
WEBSITE/tasks/marketplace-design.md "Payouts via Stripe Connect").

AFC is the CUSTODIAN of marketplace money, the SAME posture as the event paid-events
escrow (afc_tournament_and_scrims/event_payments.py): the buyer pays AFC (Stripe
Checkout or Paystack -> the charge lands in AFC's Stripe/Paystack balance), the
vendor fulfils the order, and THEN AFC TRANSFERS the vendor's share out to the
vendor's connected Stripe account. AFC never holds the cash in its own bank and is
not a money transmitter (same legal posture as the event escrow). This module owns
the three Connect pieces:

  1. ONBOARDING  (vendor_connect_onboard / vendor_connect_status)
     A vendor creates/refreshes a Stripe Connect EXPRESS account and completes
     Stripe's hosted onboarding. We store the resulting account id on
     Vendor.stripe_account_id and surface whether payouts are enabled.

  2. PAYOUT      (settle_order_payout)
     When an order reaches fulfilment_state="completed" (hooked into
     afc_shop/fulfilment.py order_mark_completed), we compute the vendor's share
     (order.total minus the settings.MARKETPLACE_FEE_PERCENT platform fee) and create
     a Stripe TRANSFER to the vendor's connected account, recording a VendorPayout
     (status="paid" + stripe_transfer_id). If the vendor has NOT finished Connect
     onboarding yet, we record the payout as status="owed" so an admin can release it
     later, once the vendor is onboarded.

  3. ADMIN LEDGER (admin_list_vendor_payouts / admin_release_owed_payouts)
     List every payout + retry ("release") the owed ones once the vendor is onboarded.

STYLE: raw `requests` against the Stripe REST API (no SDK dep), mirroring
event_payments._stripe / _amount_minor and afc_shop/stripe_checkout.py. Keys come
from settings.STRIPE_SECRET_KEY (env-driven: TEST locally, LIVE on prod).

HOW IT CONNECTS
  - MODELS: Vendor (stripe_account_id, the connected-account id this module writes),
    Order (total / fulfilment_state / vendor_payout reverse FK), VendorPayout (the
    ledger row written here). All in afc_shop/models.py.
  - CALLED BY:
      * afc_shop/fulfilment.py order_mark_completed -> settle_order_payout(order) on
        the shipped -> completed transition (best-effort; never blocks the transition).
      * afc_shop/urls.py -> the vendor onboarding/status endpoints (vendor portal,
        Phase B2 frontend) + the admin payouts dashboard endpoints.
  - AUTH: afc_shop/fulfilment.py _order_vendor / the vendor gate pattern + afc_auth
    require_admin (the admin endpoints), identical to afc_shop/vendors.py.
  - MIRRORS: afc_tournament_and_scrims/event_payments.py (the events escrow) and the
    events Phase 3 organizer-transfer design; the `transfers` capability requested on
    the connected account is what makes the payout transfer possible.
"""

import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import require_admin, validate_token
from .models import Order, Vendor, VendorPayout

logger = logging.getLogger(__name__)

STRIPE_API = "https://api.stripe.com/v1"
# Zero-decimal currencies charge/transfer in whole units; everything else (NGN/USD/...)
# in the minor unit (kobo/cents). Mirrors event_payments._ZERO_DECIMAL exactly.
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "XOF", "XAF"}

# The shop's settlement currency. Transfers go out in the SAME currency the buyer was
# charged in (SHOP_CURRENCY, default NGN), so the payout amount matches order.total.
SHOP_CURRENCY = getattr(settings, "SHOP_CURRENCY", "NGN")


# ── Stripe REST helpers (mirror event_payments._stripe / _amount_minor) ────────────
def _stripe(method, path, data=None):
    """Call the Stripe REST API with the secret key. Returns (ok, json). Never raises.
    Identical shape to event_payments._stripe / stripe_checkout._stripe so the three
    Stripe surfaces behave the same."""
    key = getattr(settings, "STRIPE_SECRET_KEY", None)
    if not key:
        return False, {"error": {"message": "Stripe is not configured (no STRIPE_SECRET_KEY)."}}
    try:
        fn = requests.post if method == "POST" else requests.get
        r = fn(f"{STRIPE_API}{path}", headers={"Authorization": f"Bearer {key}"}, data=data, timeout=30)
        return r.status_code == 200, r.json()
    except Exception as e:  # network / Stripe down -> caller surfaces a clean error
        return False, {"error": {"message": f"Stripe request failed: {e}"}}


def _amount_minor(amount, currency):
    """Stripe transfers in the smallest currency unit (kobo/cents for 2-decimal
    currencies). Mirrors event_payments._amount_minor / stripe_checkout._amount_minor."""
    if currency.upper() in _ZERO_DECIMAL:
        return int(round(float(amount)))
    return int(round(float(amount) * 100))


def _platform_fee_for(total):
    """Compute AFC's platform fee for an order total from settings.MARKETPLACE_FEE_PERCENT.

    The percent is read fresh from settings (env-driven, default 0 = no cut) and applied
    to the order total. Returned as a 2-decimal Decimal so it can be stored on the
    VendorPayout row. Guards a bad/garbage env value back to 0 so a typo can never break
    a payout."""
    try:
        percent = Decimal(str(getattr(settings, "MARKETPLACE_FEE_PERCENT", "0")))
    except Exception:
        percent = Decimal("0")
    if percent <= 0:
        return Decimal("0.00")
    return (Decimal(str(total)) * percent / Decimal("100")).quantize(Decimal("0.01"))


# ── vendor auth gate (mirrors afc_shop/vendors._require_active_vendor) ─────────────
def _require_active_vendor(request):
    """Resolve the Bearer caller and return their ACTIVE Vendor account.

    The SAME gate afc_shop/vendors.py + afc_shop/fulfilment.py use (Vendor.user ==
    caller, vendor must be active). Returns (user, vendor, error_response): on any auth
    failure user/vendor are None and error_response is a DRF Response to return as-is.
    Used by the onboarding/status endpoints so only the vendor themselves can connect
    their own Stripe account."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, None, Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, None, Response({"message": "Invalid or expired session token."}, status=401)

    vendor = Vendor.objects.filter(user=user).first()
    if not vendor:
        return None, None, Response({"message": "You are not a vendor."}, status=403)
    if vendor.status != "active":
        return None, None, Response({"message": "Your vendor access is suspended."}, status=403)

    return user, vendor, None


# ═════════════════════════════════════════════════════════════════════════════
# 1. ONBOARDING — vendor connects (or refreshes) their Stripe Connect account
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def vendor_connect_onboard(request):
    """POST /shop/connect/onboard/

    Create (or reuse) a Stripe Connect EXPRESS account for the caller's vendor and
    return a Stripe-hosted ONBOARDING URL the vendor opens to finish KYC + add a
    payout bank account. After they finish, payouts to them become possible.

    Purpose:  A vendor links their bank so AFC can transfer their share out. Mirrors
              the events Phase 3 organizer-transfer onboarding; the `transfers`
              capability requested here is what lets settle_order_payout transfer to
              this account later.
    Auth:     Bearer -> _require_active_vendor (only a vendor can connect their OWN
              account; a suspended vendor is blocked).
    Request:  {} (no body needed; the vendor is resolved from the token).
    Response: 200 { onboarding_url, account_id }  |  502 (Stripe error / not configured).
    Consumed by: the vendor self-serve dashboard "Connect payouts" button (Phase B2 FE).

    IDEMPOTENT: if the vendor already has a stripe_account_id we REUSE it (create only a
    fresh account link), so re-clicking "Connect" never creates duplicate accounts.
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    account_id = vendor.stripe_account_id

    # ── 1a. Create the connected account once (reuse it on re-onboard) ──
    # Express account in AFC's platform; request the `transfers` capability so AFC can
    # push the vendor's share to them (the events Phase 3 transfer capability).
    if not account_id:
        create_data = {
            "type": "express",
            "email": vendor.contact_email or user.email or "",
            "capabilities[transfers][requested]": "true",
            # Tie the account back to this vendor for support/audit (Stripe metadata).
            "metadata[vendor_id]": str(vendor.id),
            "metadata[afc_user_id]": str(user.user_id),
        }
        ok, acct = _stripe("POST", "/accounts", create_data)
        if not ok:
            return Response(
                {"message": "Could not start Stripe onboarding.",
                 "detail": acct.get("error", {}).get("message", "")},
                status=502,
            )
        account_id = acct.get("id", "")
        # Persist the connected-account id immediately so a later onboarding refresh +
        # settle_order_payout can find it even if the vendor abandons onboarding now.
        vendor.stripe_account_id = account_id
        vendor.save(update_fields=["stripe_account_id"])

    # ── 1b. Create a one-time hosted onboarding link for this account ──
    # The return/refresh URLs point back at the vendor dashboard; Stripe bounces the
    # vendor there when they finish or the link expires.
    base = getattr(settings, "FRONTEND_URL", "https://africanfreefirecommunity.com").rstrip("/")
    link_data = {
        "account": account_id,
        "refresh_url": f"{base}/vendor/payouts?connect=refresh",
        "return_url": f"{base}/vendor/payouts?connect=done",
        "type": "account_onboarding",
    }
    ok, link = _stripe("POST", "/account_links", link_data)
    if not ok:
        return Response(
            {"message": "Could not create onboarding link.",
             "detail": link.get("error", {}).get("message", "")},
            status=502,
        )

    return Response({"onboarding_url": link.get("url"), "account_id": account_id}, status=200)


@api_view(["GET"])
def vendor_connect_status(request):
    """GET /shop/connect/status/

    Report whether the caller-vendor's Stripe Connect account is fully onboarded and
    can RECEIVE payouts. The vendor dashboard uses this to show "Connected / payouts
    enabled" vs "Finish onboarding".

    Auth:     Bearer -> _require_active_vendor.
    Response: 200 { connected: bool, charges_enabled: bool, payouts_enabled: bool,
                    details_submitted: bool, account_id }.
    Consumed by: the vendor self-serve dashboard payouts panel (Phase B2 FE).

    `connected` is False (with everything else False) when the vendor has not started
    onboarding (no stripe_account_id). When an account exists we ask Stripe for the
    live capability flags so a stale local copy never misreports payout-readiness.
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    if not vendor.stripe_account_id:
        return Response({
            "connected": False,
            "charges_enabled": False,
            "payouts_enabled": False,
            "details_submitted": False,
            "account_id": "",
        }, status=200)

    ok, acct = _stripe("GET", f"/accounts/{vendor.stripe_account_id}")
    if not ok:
        # Account id stored but Stripe can't be reached / account gone: report not-ready
        # rather than 500, so the dashboard degrades gracefully.
        return Response({
            "connected": True,
            "charges_enabled": False,
            "payouts_enabled": False,
            "details_submitted": False,
            "account_id": vendor.stripe_account_id,
            "detail": acct.get("error", {}).get("message", ""),
        }, status=200)

    return Response({
        "connected": True,
        "charges_enabled": bool(acct.get("charges_enabled")),
        "payouts_enabled": bool(acct.get("payouts_enabled")),
        "details_submitted": bool(acct.get("details_submitted")),
        "account_id": vendor.stripe_account_id,
    }, status=200)


# ═════════════════════════════════════════════════════════════════════════════
# 2. PAYOUT — settle a completed order's vendor share (called from fulfilment.py)
# ═════════════════════════════════════════════════════════════════════════════
def settle_order_payout(order):
    """Create / record the VendorPayout for a JUST-COMPLETED marketplace order.

    CALLED BY: afc_shop/fulfilment.py order_mark_completed, right after the
    shipped -> completed transition commits (best-effort; wrapped so a payout hiccup
    can NEVER 500 the completion request or roll back the completed state).

    Behaviour:
      - No-op unless the order has a vendor (a pure diamond/AFC order is never paid out).
      - IDEMPOTENT: one VendorPayout per order (OneToOne + get_or_create). If a payout
        row already exists (a re-completed/retried order, or a verify+webhook race),
        return it untouched -> the vendor is never paid twice.
      - Compute platform_fee = order.total * MARKETPLACE_FEE_PERCENT (default 0) and the
        vendor amount = order.total - platform_fee.
      - If the vendor IS onboarded (stripe_account_id set), create a Stripe Transfer to
        their connected account and mark the row status="paid" + stripe_transfer_id.
      - If the vendor is NOT onboarded yet (no stripe_account_id), or the transfer
        fails, leave the row status="owed" so admin_release_owed_payouts can retry it
        once the vendor finishes onboarding.

    Returns the VendorPayout (or None for a non-vendor order). Never raises.
    """
    try:
        # Lazy import to avoid any import cycle (fulfilment imports connect, connect
        # imports nothing from fulfilment at module load except this function's caller).
        from .fulfilment import _order_vendor

        vendor = _order_vendor(order)
        if not vendor:
            return None  # not a marketplace order; nothing to pay out

        # Idempotency: one payout per order. get_or_create on the OneToOne FK means a
        # second completion (or a retry) finds the existing row and does not re-pay.
        platform_fee = _platform_fee_for(order.total)
        amount = (Decimal(str(order.total)) - platform_fee).quantize(Decimal("0.01"))
        if amount < Decimal("0.00"):
            amount = Decimal("0.00")

        payout, created = VendorPayout.objects.get_or_create(
            order=order,
            defaults={
                "vendor": vendor,
                "amount": amount,
                "platform_fee": platform_fee,
                "status": "owed",  # default; flipped to "paid" below if the transfer lands
            },
        )
        if not created:
            # Already settled (or owed) for this order -> do not touch it again.
            return payout

        # ── Attempt the Stripe Transfer if (and only if) the vendor is onboarded ──
        # No connected account yet -> leave it "owed"; an admin releases it later.
        if not vendor.stripe_account_id:
            logger.info(
                "settle_order_payout: vendor %s not onboarded; order #%s payout recorded as owed.",
                vendor.display_name, order.id,
            )
            return payout

        ok, resp = _create_transfer(payout)
        if ok:
            payout.status = "paid"
            payout.stripe_transfer_id = resp.get("id", "")
            payout.paid_at = timezone.now()
            payout.save(update_fields=["status", "stripe_transfer_id", "paid_at"])
        else:
            # Transfer failed (e.g. the connected account exists but payouts are not yet
            # enabled). Keep it "owed"; admin_release_owed_payouts will retry it.
            logger.warning(
                "settle_order_payout: transfer failed for order #%s vendor %s: %s",
                order.id, vendor.display_name,
                (resp.get("error") or {}).get("message", resp),
            )
        return payout
    except Exception as e:
        # This runs inside the completion path; a failure must NEVER turn a successful
        # order completion into an error. Log and move on (the order stays completed; a
        # payout can be settled later by an admin).
        logger.error("settle_order_payout failed for order #%s: %s", getattr(order, "id", "?"), e)
        return None


def _create_transfer(payout):
    """Create the Stripe Transfer that moves `payout.amount` to the vendor's connected
    account. Returns (ok, json) from _stripe. Shared by settle_order_payout (the
    completion-time attempt) and admin_release_owed_payouts (the retry).

    The transfer is in SHOP_CURRENCY (the buyer's charge currency) so the amount equals
    order.total minus the fee. transfer_group + metadata tie it back to the order for
    reconciliation in the Stripe dashboard. Skips a zero-amount transfer (Stripe rejects
    those) and treats a 100%-fee order as already-settled."""
    amount_minor = _amount_minor(payout.amount, SHOP_CURRENCY)
    if amount_minor <= 0:
        # Nothing to send (e.g. a 100% platform fee or a zero-total order). Treat as a
        # no-op success so the row can be marked paid without a Stripe call.
        return True, {"id": "", "note": "zero-amount payout (no transfer needed)"}

    data = {
        "amount": amount_minor,
        "currency": SHOP_CURRENCY.lower(),
        "destination": payout.vendor.stripe_account_id,
        "transfer_group": f"afc_order_{payout.order_id}",
        "metadata[order_id]": str(payout.order_id),
        "metadata[vendor_id]": str(payout.vendor_id),
    }
    return _stripe("POST", "/transfers", data)


# ═════════════════════════════════════════════════════════════════════════════
# 3. ADMIN LEDGER — list payouts + release the owed ones
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["GET"])
def admin_list_vendor_payouts(request):
    """GET /shop/admin/payouts/?vendor_id=&status=

    List marketplace vendor payouts for the admin payouts dashboard, with quick
    totals (how much is owed vs paid).

    Auth:     require_admin.
    Query:    vendor_id (filter to one vendor), status (owed|released|paid).
    Response: 200 { count, payouts: [ {id,vendor_id,vendor_name,order_id,amount,
                    platform_fee,status,stripe_transfer_id,paid_at,created_at} ],
                    summary: { owed_count, owed_amount, paid_count, paid_amount } }.
    Consumed by: the admin shop "Vendor payouts" surface.
    """
    admin, err = require_admin(request)
    if err:
        return err

    qs = VendorPayout.objects.select_related("vendor", "order").order_by("-created_at")
    vendor_id = request.GET.get("vendor_id")
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    status_f = request.GET.get("status")
    if status_f:
        qs = qs.filter(status=status_f)

    payouts = list(qs[:500])
    data = [{
        "id": p.id,
        "vendor_id": p.vendor_id,
        "vendor_name": p.vendor.display_name,
        "order_id": p.order_id,
        "amount": str(p.amount),
        "platform_fee": str(p.platform_fee),
        "status": p.status,
        "stripe_transfer_id": p.stripe_transfer_id,
        "paid_at": p.paid_at,
        "created_at": p.created_at,
    } for p in payouts]

    # Escrow-style totals across the (filtered) ledger, for the dashboard header.
    owed = qs.filter(status__in=("owed", "released"))
    paid = qs.filter(status="paid")
    summary = {
        "owed_count": owed.count(),
        "owed_amount": str(sum((p.amount for p in owed), Decimal("0.00"))),
        "paid_count": paid.count(),
        "paid_amount": str(sum((p.amount for p in paid), Decimal("0.00"))),
    }
    return Response({"count": len(data), "payouts": data, "summary": summary}, status=200)


@api_view(["POST"])
def admin_release_owed_payouts(request):
    """POST /shop/admin/payouts/release/  { payout_id }  OR  { vendor_id }

    RELEASE owed payouts by (re)attempting the Stripe Transfer now that the vendor is
    onboarded. Either release ONE payout (payout_id) or every owed payout for a vendor
    (vendor_id, e.g. right after they finish Connect onboarding).

    Auth:     require_admin.
    Request:  { payout_id } to release a single payout, OR { vendor_id } to release all
              of that vendor's owed payouts.
    Response: 200 { released: int, still_owed: int, results: [...] }
              | 400 (neither id given) | 404 (payout/vendor not found).
    Consumed by: the admin shop "Vendor payouts" surface (Release button).

    For each owed payout: if the vendor still has no stripe_account_id we leave it owed
    (cannot transfer without a destination); otherwise we create the transfer and flip
    it to "paid" on success. A failed transfer stays owed (reported in `still_owed`) so
    it can be retried again. Never 500s on a Stripe error."""
    admin, err = require_admin(request)
    if err:
        return err

    payout_id = request.data.get("payout_id")
    vendor_id = request.data.get("vendor_id")

    if payout_id:
        payouts = list(VendorPayout.objects.select_related("vendor").filter(id=payout_id))
        if not payouts:
            return Response({"message": "Payout not found."}, status=404)
    elif vendor_id:
        vendor = get_object_or_404(Vendor, id=vendor_id)
        payouts = list(
            VendorPayout.objects.select_related("vendor")
            .filter(vendor=vendor, status__in=("owed", "released"))
        )
    else:
        return Response({"message": "Provide payout_id or vendor_id."}, status=400)

    released = 0
    still_owed = 0
    results = []
    for payout in payouts:
        # Already paid -> nothing to do (idempotent; e.g. releasing a vendor twice).
        if payout.status == "paid":
            results.append({"payout_id": payout.id, "status": "paid", "note": "already paid"})
            continue

        # No connected account -> cannot transfer; keep it owed.
        if not payout.vendor.stripe_account_id:
            still_owed += 1
            results.append({"payout_id": payout.id, "status": "owed", "note": "vendor not onboarded"})
            continue

        ok, resp = _create_transfer(payout)
        if ok:
            payout.status = "paid"
            payout.stripe_transfer_id = resp.get("id", "")
            payout.paid_at = timezone.now()
            payout.save(update_fields=["status", "stripe_transfer_id", "paid_at"])
            released += 1
            results.append({"payout_id": payout.id, "status": "paid",
                            "stripe_transfer_id": payout.stripe_transfer_id})
        else:
            still_owed += 1
            results.append({"payout_id": payout.id, "status": "owed",
                            "detail": (resp.get("error") or {}).get("message", "transfer failed")})

    return Response({"released": released, "still_owed": still_owed, "results": results}, status=200)
