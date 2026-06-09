"""
afc_shop/paystack_payout.py
================================================================================
PAYSTACK TRANSFERS VENDOR PAYOUTS (marketplace Phase B3, the PRIMARY payout rail).

WHY THIS IS THE PRIMARY RAIL (and afc_shop/connect.py / Stripe Connect is the
fallback): AFC's marketplace vendors are MAJORITY AFRICAN. Stripe Connect does NOT
pay out to Nigerian / most-African bank accounts, but PAYSTACK does (Nigeria, Ghana,
South Africa, Kenya), AND the shop already CHARGES buyers via Paystack (the money
lands in AFC's Paystack balance). So Paystack Transfers is the DEFAULT way AFC pays a
vendor their share; Stripe Connect (connect.py) stays only for the non-African
vendors Stripe can actually reach.

This module is the Paystack MIRROR of afc_shop/connect.py. Same posture: AFC is the
CUSTODIAN of marketplace money (like the event escrow in
afc_tournament_and_scrims/event_payments.py) — the buyer pays AFC, the vendor fulfils,
and THEN AFC TRANSFERS the vendor's share out, here via a Paystack Transfer to the
vendor's LOCAL bank account. It owns four Paystack pieces:

  1. BANK PICKER   (list_banks / resolve_account)
     list_banks returns the Paystack bank list for the vendor's bank dropdown.
     resolve_account turns {account_number, bank_code} into the real account_name so
     the vendor CONFIRMS the holder name before saving (catches a typo'd number).

  2. SAVE BANK     (vendor_save_bank)
     Vendor-gated: store the vendor's bank_code/account_number, resolve + store the
     account_name, create a Paystack Transfer RECIPIENT (POST /transferrecipient,
     type=nuban) and store the resulting paystack_recipient_code, then set
     payout_provider="paystack". This recipient code is the transfer destination.

  3. PAYOUT        (settle_order_payout_paystack)
     On an order's shipped -> completed transition (hooked PROVIDER-AWARE in
     afc_shop/fulfilment.py order_mark_completed), compute order.total minus the
     settings.MARKETPLACE_FEE_PERCENT platform fee and POST /transfer (source=balance,
     amount in KOBO = NGN*100, recipient=paystack_recipient_code) -> record the SAME
     VendorPayout ledger row (status="paid" + the Paystack transfer code stored in the
     SAME stripe_transfer_id column the Stripe rail uses, so the admin ledger reads one
     field for both rails). If the vendor has no recipient yet, record "owed" so an
     admin can release it later. IDEMPOTENT via VendorPayout OneToOne(order). NEVER
     raises (runs inside the completion path).

  4. ADMIN RETRY   (admin_retry_owed_paystack_payouts)
     Re-attempt the Paystack Transfer for "owed" Paystack rows once the vendor has
     saved their bank (the Paystack twin of connect.admin_release_owed_payouts; the
     admin LEDGER list itself is shared — connect.admin_list_vendor_payouts shows BOTH
     rails since they write the one VendorPayout table).

STYLE: raw `requests` against the Paystack REST API (no SDK dep), Authorization:
Bearer settings.PAYSTACK_SECRET_KEY — IDENTICAL to the shop's existing Paystack usage
in afc_shop/views.py (buy_now POSTs to api.paystack.co/transaction/initialize;
verify_paystack_payment GETs /transaction/verify). Keys are env-driven (TEST locally,
LIVE on prod).

HOW IT CONNECTS
  - MODELS (afc_shop/models.py): Vendor (payout_provider + bank_code / bank_name /
    account_number / account_name / paystack_recipient_code — all added with this
    rail), Order (total / fulfilment_state / vendor_payout reverse FK), VendorPayout
    (the SHARED ledger row, also written by connect.py for the Stripe rail).
  - CALLED BY:
      * afc_shop/fulfilment.py order_mark_completed -> settle_order_payout_paystack(order)
        when the order's vendor.payout_provider == "paystack" (the Stripe path is the
        else branch). Best-effort; never blocks / 500s the completion.
      * afc_shop/urls.py -> the vendor bank-save + bank-list + resolve endpoints (vendor
        portal Payouts page) + the admin Paystack-retry endpoint.
  - AUTH: _require_active_vendor below (the SAME Vendor.user == caller gate used by
    afc_shop/vendors.py + afc_shop/connect.py) for the vendor endpoints; afc_auth
    require_admin for the admin retry.
  - MIRRORS: afc_shop/connect.py (the Stripe rail — same VendorPayout ledger, same
    _platform_fee_for math, same idempotency + best-effort posture) and the shop's
    own Paystack calls in afc_shop/views.py (same raw-requests + Bearer-key idiom).
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
from .models import Vendor, VendorPayout

logger = logging.getLogger(__name__)

PAYSTACK_API = "https://api.paystack.co"

# The shop's settlement currency. Paystack Transfers settle in the SAME currency the
# buyer was charged in (SHOP_CURRENCY, default NGN), so the payout amount matches
# order.total. Mirrors connect.SHOP_CURRENCY / stripe_checkout.SHOP_CURRENCY.
SHOP_CURRENCY = getattr(settings, "SHOP_CURRENCY", "NGN")


# ── Paystack REST helpers (mirror connect._stripe + the views.py Paystack idiom) ────
def _paystack(method, path, json_body=None):
    """Call the Paystack REST API with the secret key. Returns (ok, json). Never raises.

    Same shape as connect._stripe so the two payout rails behave identically; the auth
    is the SAME Authorization: Bearer settings.PAYSTACK_SECRET_KEY the shop already uses
    in afc_shop/views.py (buy_now / verify_paystack_payment). `ok` is True only when
    Paystack returns HTTP 200 AND its own JSON {"status": true} envelope (Paystack wraps
    success in a `status` boolean, unlike Stripe), so a caller can trust resp["data"]."""
    key = getattr(settings, "PAYSTACK_SECRET_KEY", None)
    if not key:
        return False, {"message": "Paystack is not configured (no PAYSTACK_SECRET_KEY)."}
    try:
        fn = requests.post if method == "POST" else requests.get
        r = fn(
            f"{PAYSTACK_API}{path}",
            headers={"Authorization": f"Bearer {key}"},
            json=json_body,
            timeout=30,
        )
        body = r.json()
        # Paystack signals success with HTTP 200 + {"status": true, "data": ...}. Treat
        # anything else (4xx/5xx, or status=false) as a failure the caller can surface.
        ok = r.status_code == 200 and bool(body.get("status"))
        return ok, body
    except Exception as e:  # network / Paystack down -> caller surfaces a clean error
        return False, {"message": f"Paystack request failed: {e}"}


def _amount_kobo(amount):
    """Paystack transfers in the smallest unit (KOBO for NGN = NGN * 100). Mirrors the
    `int(order.total * 100)` the shop already uses in views.buy_now for the charge, so
    the payout is in the same minor unit the buyer was charged in."""
    return int(round(float(amount) * 100))


def _platform_fee_for(total):
    """Compute AFC's platform fee for an order total from settings.MARKETPLACE_FEE_PERCENT.

    IDENTICAL to connect._platform_fee_for so both payout rails take the same cut: the
    percent is read fresh from settings (env-driven, default 0 = no cut), applied to the
    order total, returned as a 2-decimal Decimal, and a bad/garbage env value is guarded
    back to 0 so a typo can never break a payout."""
    try:
        percent = Decimal(str(getattr(settings, "MARKETPLACE_FEE_PERCENT", "0")))
    except Exception:
        percent = Decimal("0")
    if percent <= 0:
        return Decimal("0.00")
    return (Decimal(str(total)) * percent / Decimal("100")).quantize(Decimal("0.01"))


# ── vendor auth gate (mirrors connect._require_active_vendor / vendors._require_active_vendor) ──
def _require_active_vendor(request):
    """Resolve the Bearer caller and return their ACTIVE Vendor account.

    The SAME gate afc_shop/vendors.py + afc_shop/connect.py use (Vendor.user == caller,
    vendor must be active). Returns (user, vendor, error_response): on any auth failure
    user/vendor are None and error_response is a DRF Response to return as-is. Used by the
    bank-save / bank-list / resolve endpoints so only the vendor themselves can set up
    their own payout bank."""
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
# 1. BANK PICKER — list banks + resolve an account name
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["GET"])
def list_banks(request):
    """GET /shop/banks/

    Return the Paystack bank list for the vendor's bank-picker dropdown (a vendor
    chooses their bank here, then enters their account number).

    Purpose:  Populate the "Bank" <select> on the vendor Payouts page so the vendor
              picks a real bank (each option carries the Paystack bank `code` we store
              on Vendor.bank_code + later pass to /transferrecipient).
    Auth:     Bearer -> _require_active_vendor (only a vendor needs this; it is also
              cheap to gate so the bank list isn't a public scrape target).
    Query:    currency (optional; defaults to SHOP_CURRENCY, i.e. NGN) — Paystack lists
              banks per country/currency.
    Response: 200 { banks: [ {name, code} ] }  |  502 (Paystack error / not configured).
    Consumed by: app/(vendor)/vendor/payouts/page.tsx (the bank dropdown), via
                 lib/vendor.ts::vendorPayoutApi.listBanks.

    CACHEABLE: the bank list is large + changes rarely, so the frontend fetches it once
    when the Payouts page opens; we keep the response lean (name + code only)."""
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    currency = request.GET.get("currency") or SHOP_CURRENCY
    ok, body = _paystack("GET", f"/bank?currency={currency}")
    if not ok:
        return Response(
            {"message": "Could not load the bank list.", "detail": body.get("message", "")},
            status=502,
        )

    # Paystack returns a long record per bank; the picker only needs name + code.
    banks = [{"name": b.get("name"), "code": b.get("code")} for b in body.get("data", [])]
    return Response({"banks": banks}, status=200)


@api_view(["POST"])
def resolve_account(request):
    """POST /shop/resolve-account/  body: { account_number, bank_code }

    Resolve a {account_number, bank_code} pair to the real ACCOUNT HOLDER NAME via
    Paystack, so the vendor can CONFIRM the account is theirs before saving it.

    Purpose:  The "Resolve" step on the vendor Payouts page: the vendor types their
              account number + picks a bank, taps Resolve, and we show back the holder
              name Paystack has on file (a typo'd number resolves to the wrong name or
              fails, catching the error before any money moves).
    Auth:     Bearer -> _require_active_vendor.
    Request:  { account_number, bank_code } (both required).
    Response: 200 { account_name, account_number, bank_code }
              | 400 (missing field) | 502 (Paystack could not resolve it).
    Consumed by: app/(vendor)/vendor/payouts/page.tsx (the Resolve button), via
                 lib/vendor.ts::vendorPayoutApi.resolveAccount.

    This does NOT save anything; it is a pure lookup. vendor_save_bank persists the
    confirmed details (and re-resolves server-side, so saving never trusts a stale name)."""
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    account_number = (request.data.get("account_number") or "").strip()
    bank_code = (request.data.get("bank_code") or "").strip()
    if not account_number or not bank_code:
        return Response({"message": "account_number and bank_code are required."}, status=400)

    ok, body = _paystack(
        "GET", f"/bank/resolve?account_number={account_number}&bank_code={bank_code}"
    )
    if not ok:
        return Response(
            {"message": "Could not resolve that account. Check the number and bank.",
             "detail": body.get("message", "")},
            status=502,
        )

    data = body.get("data", {})
    return Response(
        {
            "account_name": data.get("account_name", ""),
            "account_number": data.get("account_number", account_number),
            "bank_code": bank_code,
        },
        status=200,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 2. SAVE BANK — store the bank, create a Paystack Transfer Recipient
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def vendor_save_bank(request):
    """POST /shop/vendor/bank/  body: { account_number, bank_code, bank_name? }

    Save the caller-vendor's payout BANK and create the Paystack Transfer RECIPIENT used
    to pay them out. This is the Paystack equivalent of connect.vendor_connect_onboard.

    Flow:
      1. Re-RESOLVE the account server-side (never trust a name the client sends) to get
         the holder account_name; a bad number fails here with 400/502.
      2. Create a Paystack Transfer Recipient (POST /transferrecipient, type=nuban) from
         {account_number, bank_code, currency}; store the returned recipient_code.
      3. Persist bank_code / bank_name / account_number / account_name /
         paystack_recipient_code on the Vendor and set payout_provider="paystack" (so the
         provider-aware payout hook in fulfilment.py routes this vendor down the Paystack
         path). IDEMPOTENT: re-saving the same/updated bank just refreshes the recipient.

    Auth:     Bearer -> _require_active_vendor (only a vendor sets their OWN bank).
    Request:  { account_number, bank_code, bank_name? } (bank_name is the display label
              from the picker; account_name is resolved server-side, not trusted).
    Response: 200 { message, payout_provider, bank_code, bank_name, account_number,
                    account_name, recipient_code }
              | 400 (missing field) | 502 (resolve / recipient creation failed).
    Consumed by: app/(vendor)/vendor/payouts/page.tsx (the Save button), via
                 lib/vendor.ts::vendorPayoutApi.saveBank.
    """
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    account_number = (request.data.get("account_number") or "").strip()
    bank_code = (request.data.get("bank_code") or "").strip()
    bank_name = (request.data.get("bank_name") or "").strip()
    if not account_number or not bank_code:
        return Response({"message": "account_number and bank_code are required."}, status=400)

    # ── 2a. Resolve the holder name server-side (do not trust a client-sent name). ──
    ok, body = _paystack(
        "GET", f"/bank/resolve?account_number={account_number}&bank_code={bank_code}"
    )
    if not ok:
        return Response(
            {"message": "Could not verify that account. Check the number and bank.",
             "detail": body.get("message", "")},
            status=502,
        )
    account_name = body.get("data", {}).get("account_name", "")

    # ── 2b. Create the Paystack Transfer Recipient (the transfer destination). ──
    # type=nuban is the Nigerian bank-account recipient; currency is the shop currency.
    # The recipient_code (RCP_...) is what POST /transfer takes as `recipient` later.
    ok, body = _paystack(
        "POST",
        "/transferrecipient",
        {
            "type": "nuban",
            "name": account_name or vendor.display_name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": SHOP_CURRENCY,
            # Tie the recipient back to this vendor for support/audit in the Paystack
            # dashboard (mirrors the metadata we set on the Stripe connected account).
            "metadata": {"vendor_id": str(vendor.id), "afc_user_id": str(user.user_id)},
        },
    )
    if not ok:
        # Log the REAL Paystack reason (e.g. "transfers not enabled", invalid bank) so prod
        # logs explain the failure; the vendor-facing `detail` carries it to the UI too.
        logger.warning(
            "vendor_save_bank: transferrecipient failed for vendor #%s: %s",
            vendor.id, body.get("message", body),
        )
        return Response(
            {"message": "Could not save your bank for payouts. Please try again.",
             "detail": body.get("message", "")},
            status=502,
        )
    recipient_code = body.get("data", {}).get("recipient_code", "")

    # ── 2c. Persist on the Vendor + switch the rail to Paystack. ──
    vendor.bank_code = bank_code
    vendor.bank_name = bank_name
    vendor.account_number = account_number
    vendor.account_name = account_name
    vendor.paystack_recipient_code = recipient_code
    vendor.payout_provider = "paystack"
    vendor.save(update_fields=[
        "bank_code", "bank_name", "account_number", "account_name",
        "paystack_recipient_code", "payout_provider",
    ])

    return Response(
        {
            "message": "Bank saved. You will be paid out to this account.",
            "payout_provider": vendor.payout_provider,
            "bank_code": vendor.bank_code,
            "bank_name": vendor.bank_name,
            "account_number": vendor.account_number,
            "account_name": vendor.account_name,
            "recipient_code": vendor.paystack_recipient_code,
        },
        status=200,
    )


@api_view(["GET"])
def vendor_payout_method(request):
    """GET /shop/vendor/payout-method/

    Report the caller-vendor's CURRENT saved payout method + readiness, so the vendor
    Payouts page can show "Paid out to GTBank ****1234" vs "Add your bank to get paid".

    Auth:     Bearer -> _require_active_vendor.
    Response: 200 { payout_provider, ready: bool, bank_code, bank_name,
                    account_number, account_name, has_recipient: bool }.
    Consumed by: app/(vendor)/vendor/payouts/page.tsx (the saved-method panel), via
                 lib/vendor.ts::vendorPayoutApi.getPayoutMethod.

    `ready` is True when, for the vendor's chosen provider, payouts can actually go out:
    a Paystack vendor is ready once they have a paystack_recipient_code; a Stripe vendor
    is ready once they have a stripe_account_id (the deeper Stripe capability check lives
    in connect.vendor_connect_status; here we only need the at-a-glance flag)."""
    user, vendor, err = _require_active_vendor(request)
    if err:
        return err

    has_recipient = bool(vendor.paystack_recipient_code)
    if vendor.payout_provider == "stripe":
        ready = bool(vendor.stripe_account_id)
    else:
        ready = has_recipient

    return Response(
        {
            "payout_provider": vendor.payout_provider,
            "ready": ready,
            "bank_code": vendor.bank_code,
            "bank_name": vendor.bank_name,
            "account_number": vendor.account_number,
            "account_name": vendor.account_name,
            "has_recipient": has_recipient,
        },
        status=200,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 3. PAYOUT — settle a completed order's vendor share via a Paystack Transfer
# ═════════════════════════════════════════════════════════════════════════════
def settle_order_payout_paystack(order):
    """Create / record the VendorPayout for a JUST-COMPLETED order via PAYSTACK TRANSFERS.

    The Paystack twin of connect.settle_order_payout. CALLED BY: afc_shop/fulfilment.py
    order_mark_completed, PROVIDER-AWARE — only when the order's vendor.payout_provider
    == "paystack" (the default / African vendors). Best-effort + idempotent; NEVER raises
    (a payout hiccup must never 500 the completion or roll back the completed state).

    Behaviour (mirrors the Stripe rail exactly, just a Paystack Transfer instead):
      - No-op unless the order has a vendor (a pure diamond/AFC order is never paid out).
      - IDEMPOTENT: one VendorPayout per order (OneToOne + get_or_create). If a row already
        exists (a re-completed/retried order, or a verify+webhook race), return it
        untouched -> the vendor is never paid twice.
      - Compute platform_fee = order.total * MARKETPLACE_FEE_PERCENT (default 0) and the
        vendor amount = order.total - platform_fee.
      - If the vendor HAS a paystack_recipient_code, POST /transfer and mark the row
        status="paid" + store the Paystack transfer code in stripe_transfer_id (the SHARED
        ledger column both rails write, so the admin payouts list reads one field).
      - If the vendor has NO recipient yet (bank not saved), or the transfer fails, leave
        the row status="owed" so admin_retry_owed_paystack_payouts can retry it once the
        vendor saves their bank.

    Returns the VendorPayout (or None for a non-vendor order). Never raises.
    """
    try:
        # Lazy import to avoid any import cycle (fulfilment imports this; this imports
        # nothing from fulfilment at module load except via this call).
        from .fulfilment import _order_vendor

        vendor = _order_vendor(order)
        if not vendor:
            return None  # not a marketplace order; nothing to pay out

        # Idempotency: one payout per order. get_or_create on the OneToOne FK means a
        # second completion (or a retry) finds the existing row and does not re-pay. This
        # is the SAME ledger row connect.py writes, so an order is never double-settled
        # across the two rails either.
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

        # ── Attempt the Paystack Transfer if (and only if) the vendor has a recipient ──
        # No recipient yet (bank not saved) -> leave it "owed"; an admin releases it later.
        if not vendor.paystack_recipient_code:
            logger.info(
                "settle_order_payout_paystack: vendor %s has no Paystack recipient; "
                "order #%s payout recorded as owed.",
                vendor.display_name, order.id,
            )
            return payout

        ok, resp = _create_transfer(payout)
        if ok:
            payout.status = "paid"
            # Store the Paystack transfer code in the shared ledger column (named
            # stripe_transfer_id for the Stripe rail; here it holds the Paystack
            # transfer_code so the admin list shows one reference per row regardless of rail).
            payout.stripe_transfer_id = resp.get("data", {}).get("transfer_code", "")
            payout.paid_at = timezone.now()
            payout.save(update_fields=["status", "stripe_transfer_id", "paid_at"])
        else:
            # Transfer failed (e.g. insufficient Paystack balance, OTP required on the
            # account). Keep it "owed"; admin_retry_owed_paystack_payouts will retry it.
            logger.warning(
                "settle_order_payout_paystack: transfer failed for order #%s vendor %s: %s",
                order.id, vendor.display_name, resp.get("message", resp),
            )
        return payout
    except Exception as e:
        # Runs inside the completion path; a failure must NEVER turn a successful order
        # completion into an error. Log and move on (the order stays completed; the payout
        # can be settled later by an admin).
        logger.error("settle_order_payout_paystack failed for order #%s: %s", getattr(order, "id", "?"), e)
        return None


def _create_transfer(payout):
    """Create the Paystack Transfer that moves `payout.amount` to the vendor's recipient.
    Returns (ok, json) from _paystack. Shared by settle_order_payout_paystack (the
    completion-time attempt) and admin_retry_owed_paystack_payouts (the retry) — the
    Paystack twin of connect._create_transfer.

    The transfer is source="balance" (AFC's Paystack balance the buyer charges land in),
    amount in KOBO (NGN*100), recipient = the vendor's paystack_recipient_code. `reference`
    ties it to the order for reconciliation in the Paystack dashboard. Skips a zero-amount
    transfer (Paystack rejects those) and treats a 100%-fee order as already-settled."""
    amount_kobo = _amount_kobo(payout.amount)
    if amount_kobo <= 0:
        # Nothing to send (e.g. a 100% platform fee or a zero-total order). Treat as a
        # no-op success so the row can be marked paid without a Paystack call.
        return True, {"data": {"transfer_code": ""}, "note": "zero-amount payout (no transfer needed)"}

    body = {
        "source": "balance",
        "amount": amount_kobo,
        "recipient": payout.vendor.paystack_recipient_code,
        "reason": f"AFC marketplace payout for order #{payout.order_id}",
        # Unique per payout so a retry is traceable; the OneToOne(order) guard upstream
        # already prevents a double-settle, so a stable per-order reference is safe.
        "reference": f"afc_payout_{payout.order_id}",
    }
    return _paystack("POST", "/transfer", body)


# ═════════════════════════════════════════════════════════════════════════════
# 4. ADMIN RETRY — release the owed PAYSTACK payouts
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def admin_retry_owed_paystack_payouts(request):
    """POST /shop/admin/payouts/retry-paystack/  { payout_id }  OR  { vendor_id }

    RETRY owed PAYSTACK payouts by (re)attempting the Paystack Transfer now that the
    vendor has saved their bank (so they have a paystack_recipient_code). The Paystack
    twin of connect.admin_release_owed_payouts (which handles the Stripe rail). The admin
    LEDGER list itself is shared — connect.admin_list_vendor_payouts shows BOTH rails'
    rows since both write the one VendorPayout table.

    Auth:     require_admin.
    Request:  { payout_id } to retry a single payout, OR { vendor_id } to retry all of
              that vendor's owed payouts.
    Response: 200 { released: int, still_owed: int, results: [...] }
              | 400 (neither id given) | 404 (payout/vendor not found).
    Consumed by: the admin shop "Vendor payouts" surface (Retry button on a Paystack row).

    For each owed payout: if the vendor still has no paystack_recipient_code we leave it
    owed (cannot transfer without a destination); otherwise we create the transfer and
    flip it to "paid" on success. A failed transfer stays owed (reported in `still_owed`)
    so it can be retried again. Never 500s on a Paystack error."""
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
        # Already paid -> nothing to do (idempotent; e.g. retrying a vendor twice).
        if payout.status == "paid":
            results.append({"payout_id": payout.id, "status": "paid", "note": "already paid"})
            continue

        # No Paystack recipient -> cannot transfer; keep it owed.
        if not payout.vendor.paystack_recipient_code:
            still_owed += 1
            results.append({"payout_id": payout.id, "status": "owed", "note": "vendor has no saved bank"})
            continue

        ok, resp = _create_transfer(payout)
        if ok:
            payout.status = "paid"
            payout.stripe_transfer_id = resp.get("data", {}).get("transfer_code", "")
            payout.paid_at = timezone.now()
            payout.save(update_fields=["status", "stripe_transfer_id", "paid_at"])
            released += 1
            results.append({"payout_id": payout.id, "status": "paid",
                            "transfer_code": payout.stripe_transfer_id})
        else:
            still_owed += 1
            results.append({"payout_id": payout.id, "status": "owed",
                            "detail": resp.get("message", "transfer failed")})

    return Response({"released": released, "still_owed": still_owed, "results": results}, status=200)
