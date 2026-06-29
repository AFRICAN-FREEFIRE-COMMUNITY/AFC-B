"""
afc_shop/stripe_checkout.py
================================================================================
Stripe Checkout as a SECOND payment provider for the AFC diamond shop, ADDED ALONGSIDE the
existing Paystack flow (it does not replace or touch Paystack). A buyer at checkout can pick
Stripe instead of Paystack; this module mirrors afc_shop.views.buy_now's validation + coupon
discount + fulfilment loop exactly, only swapping the gateway from Paystack to a Stripe Checkout
Session (mode=payment, Adaptive Pricing so each buyer is shown + charged in their local currency).

WHY A SEPARATE MODULE
  buy_now / verify_paystack_payment / paystack_webhook live in views.py and stay untouched. The
  Stripe path lives here so the original Paystack code is never at risk, while the shared business
  rules (item validation, the SERVER-SIDE coupon discount, the per-item-quantity fulfilment loop)
  are factored into helpers below so Paystack and Stripe stay in lockstep.

FLOW (mirrors the Paystack 3-step flow)
  1. stripe_buy_now  (POST /shop/stripe-buy-now/) -> validate items + delivery fields, re-apply the
     coupon discount SERVER-SIDE (never trust the client amount), create Order(provider="stripe",
     status="pending") + OrderItems, open a Stripe Checkout Session, return { checkout_url,
     order_id, session_id }. The FE (CartDetails.tsx) redirects the buyer to checkout_url.
  2. buyer pays on Stripe Checkout -> Stripe redirects to the shop success page with the session id.
  3. stripe_verify   (POST /shop/stripe-verify/) -> retrieve the session; if paid and the order is
     not already paid, mark it paid + bump the coupon used_count + run the SAME fulfilment loop as
     verify_paystack_payment (one Fulfillment + purchase_voucher per item-quantity). Idempotent.
  3b. stripe_webhook (POST /shop/stripe-webhook/) -> a server-side backstop (closed tab). Verifies
     the Stripe-Signature HMAC, and on checkout.session.completed runs the exact same fulfilment
     path. Returns 200 fast and never 500s.

STYLE: raw `requests` against the Stripe REST API (no SDK dep), mirroring
afc_tournament_and_scrims/event_payments.py (_stripe/_amount_minor helpers, HMAC webhook verify).
Keys come from settings.STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET (env-driven: TEST locally, LIVE
on prod).

CONSUMED BY
  - afc_shop/urls.py            -> the /shop/stripe-buy-now/, /shop/stripe-verify/,
                                   /shop/stripe-webhook/ routes.
  - frontend .../shop/_components/CartDetails.tsx -> the checkout UI: provider selector POSTs to
                                   stripe-buy-now and redirects to checkout_url; the success return
                                   calls stripe-verify with the session id.

MODELS TOUCHED
  Order (provider/status/subtotal/tax/discount_total/coupon/total + stripe_session_id/
  stripe_payment_intent/paid_at), OrderItem (per-line coupon_code snapshot), Coupon
  (is_valid_now / used_count), Fulfillment (one row per item-quantity), plus
  afc_shop.services.mintroute.purchase_voucher for the actual voucher delivery.
"""

import hashlib
import hmac
import json
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_auth.views import validate_token
from .models import Coupon, Fulfillment, Order, OrderItem, ProductVariant
from .services.mintroute import purchase_voucher

# 7.5% tax, identical to afc_shop.views.TAX_RATE. Re-declared here (not imported) to avoid pulling
# the whole views module into this gateway file; the two MUST stay equal (the shop is NGN-priced).
TAX_RATE = Decimal("0.075")

# Shop checkout currency. The Paystack flow charges in NGN (kobo, amount*100); Stripe must charge
# in the same currency so the two providers cost the buyer the same. Env-overridable in case the
# shop currency ever changes, defaulting to NGN to match Paystack.
SHOP_CURRENCY = getattr(settings, "SHOP_CURRENCY", "NGN")

STRIPE_API = "https://api.stripe.com/v1"
# Zero-decimal currencies charge in whole units; everything else (NGN/USD/...) in the minor unit
# (cents/kobo). Mirrors afc_tournament_and_scrims.event_payments._ZERO_DECIMAL.
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "XOF", "XAF"}


# ── Stripe REST helpers (mirror event_payments._stripe / _amount_minor) ────────────────────────
def _stripe(method, path, data=None):
    """Call the Stripe REST API with the secret key. Returns (ok, json). Never raises.
    Identical shape to event_payments._stripe so the two payment surfaces behave the same."""
    key = getattr(settings, "STRIPE_SECRET_KEY", None)
    if not key:
        return False, {"error": {"message": "Stripe is not configured (no STRIPE_SECRET_KEY)."}}
    try:
        fn = requests.post if method == "POST" else requests.get
        r = fn(f"{STRIPE_API}{path}", headers={"Authorization": f"Bearer {key}"}, data=data, timeout=30)
        return r.status_code == 200, r.json()
    except Exception as e:  # network / Stripe down -> caller surfaces a clean 502
        return False, {"error": {"message": f"Stripe request failed: {e}"}}


def _amount_minor(amount, currency):
    """Stripe charges in the smallest currency unit (kobo/cents for 2-decimal currencies)."""
    if currency.upper() in _ZERO_DECIMAL:
        return int(round(float(amount)))
    return int(round(float(amount) * 100))


# ── Shared business helpers (kept in lockstep with afc_shop.views buy_now / verify) ────────────
def _price_items(items):
    """Validate the requested items and price them, EXACTLY like buy_now does.

    Mirrors afc_shop.views.buy_now: each item is {variant_id, quantity, coupon_code?}; we re-look
    up the active variant server-side, check stock, and compute base_price + 7.5% tax per line.

    Returns (subtotal, total_tax, order_items, error_response). On any bad input order_items is None
    and error_response is a DRF Response the caller returns as-is (so the two providers reject the
    same inputs the same way)."""
    subtotal = Decimal("0.00")
    total_tax = Decimal("0.00")
    order_items = []

    for item in items:
        variant = ProductVariant.objects.filter(id=item.get("variant_id"), is_active=True).first()
        if not variant:
            return None, None, None, Response({"message": "Invalid product"}, status=404)

        quantity = int(item.get("quantity", 1))
        if quantity <= 0:
            return None, None, None, Response({"message": "Invalid quantity"}, status=400)

        if variant.product.is_limited_stock and variant.stock_qty < quantity:
            return None, None, None, Response({"message": "Insufficient stock"}, status=400)

        base_price = (variant.price * quantity).quantize(Decimal("0.01"))
        tax = (base_price * TAX_RATE).quantize(Decimal("0.01"))

        subtotal += base_price
        total_tax += tax

        order_items.append({
            "variant": variant,
            "quantity": quantity,
            "unit_price": variant.price,
            "line_total": base_price + tax,
        })

    return subtotal, total_tax, order_items, None


def _apply_coupon(items, subtotal):
    """Re-validate + apply the order-level coupon SERVER-SIDE, EXACTLY like buy_now does.

    The client attaches the applied coupon to each item as coupon_code; the amount we charge must
    NEVER trust the client, so we re-validate here and compute the discount off the pre-tax
    subtotal (percent -> share, fixed -> flat, capped at subtotal). On payment success
    stripe_verify / stripe_webhook bump this coupon's used_count (mirroring the Paystack path).

    Returns (coupon, discount, error_response). On a bad coupon code/state, coupon is None and
    error_response is a DRF Response to return as-is."""
    coupon = None
    coupon_code = ""
    for _it in items:
        _cc = (_it.get("coupon_code") or "").strip()
        if _cc:
            coupon_code = _cc.upper()
            break

    discount = Decimal("0.00")
    if coupon_code:
        coupon = Coupon.objects.filter(code=coupon_code).first()
        if not coupon:
            return None, discount, Response({"message": "Invalid coupon code."}, status=400)
        if not coupon.is_valid_now():
            return None, discount, Response({"message": "This coupon is not valid at the moment."}, status=400)
        if subtotal < coupon.min_order_amount:
            return None, discount, Response(
                {"message": f"This coupon needs a minimum order of {coupon.min_order_amount}."},
                status=400,
            )
        # percent -> share of the pre-tax subtotal; fixed -> flat amount. Cap at subtotal.
        if coupon.discount_type == "percent":
            discount = (subtotal * (coupon.discount_value / Decimal("100"))).quantize(Decimal("0.01"))
        else:
            discount = coupon.discount_value
        discount = min(discount, subtotal)  # never push the order below zero

    return coupon, discount, None


def _fulfil_order(order):
    """Count the coupon use + run the voucher fulfilment loop. Same logic as
    verify_paystack_payment / paystack_webhook: the single source of truth shared by
    stripe_verify and stripe_webhook so Stripe behaves identically to Paystack.

    MUST be called only AFTER the order is flipped to status="paid" (the callers do this in a
    SEPARATE committed step first, so a delivery failure here can never roll back the paid status,
    and the webhook can never 500 on a Mintroute hiccup). The already-paid guard in the callers
    makes the coupon count + fulfilment run exactly once per order even if verify + webhook race.

    For each OrderItem, one Fulfillment row + one purchase_voucher call PER UNIT of quantity
    (the "IMPORTANT FIX" in verify_paystack_payment), so a qty-3 line delivers 3 vouchers. Each
    voucher call is wrapped so a single failed/raising delivery becomes a "failed" Fulfillment row
    rather than aborting the rest (the buyer already paid, so we record every attempt)."""
    # Count the coupon use now that the order is paid (runs once per order; the caller's
    # already-paid guard prevents a double count when verify + webhook both fire).
    if order.coupon_id:
        order.coupon.used_count = (order.coupon.used_count or 0) + 1
        order.coupon.save(update_fields=["used_count"])

    for item in order.items.all():
        for _ in range(item.quantity):  # one voucher per unit (mirrors the Paystack loop)
            fulfillment = Fulfillment.objects.create(
                order=order,
                item=item,
                status="processing",
            )

            try:
                response = purchase_voucher(item.variant, order)

                if response.get("status"):
                    fulfillment.status = "delivered"
                    fulfillment.provider_payload = response["data"]
                else:
                    fulfillment.status = "failed"
                    fulfillment.provider_payload = {
                        "status": f"{response.get('status')}",
                        "error": f"{response.get('error')}",
                        "code": f"{response.get('code')}",
                    }
            except Exception as e:
                # The buyer already paid; a delivery exception (e.g. the voucher provider is down
                # or misconfigured) must NOT bubble up to a 500 webhook. Record it as failed so an
                # admin can re-issue, and keep going with the rest of the order's vouchers.
                fulfillment.status = "failed"
                fulfillment.notes = str(e)

            fulfillment.save()


def _mark_paid_and_fulfil(order, payment_intent=None):
    """Idempotently flip an order to paid, THEN fulfil it. Shared by stripe_verify + stripe_webhook.

    Two separate steps on purpose:
      1. Mark paid in its own committed transaction (the money was captured -> record it first).
      2. Fulfil (issue vouchers). If step 2 fails, the order stays paid and the failure is recorded
         on the Fulfillment rows; the webhook still returns 200 (never 500) so Stripe stops retrying.

    The already-paid guard makes the whole thing a no-op on a second call (a closed-tab webhook
    arriving after a successful verify, or vice versa), so vouchers are never double-issued."""
    with transaction.atomic():
        # Lock the order row + re-read its status inside the transaction. select_for_update is the
        # idempotency guard: if verify and the webhook race, the second one blocks here, then sees
        # status=="paid" and bails -> no double coupon count, no double vouchers.
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status == "paid":
            return
        locked.status = "paid"
        locked.paid_at = timezone.now()
        if payment_intent:
            locked.stripe_payment_intent = payment_intent
        locked.save(update_fields=["status", "paid_at", "stripe_payment_intent"])

    # Fulfil AFTER the paid status is committed (step 1 above), so a delivery problem cannot undo
    # the payment record. _fulfil_order guards each voucher call internally. Re-fetch with the
    # prefetched items so the loop is efficient (the locked instance was a bare row lock).
    order = Order.objects.prefetch_related("items__variant").get(pk=order.pk)
    _fulfil_order(order)

    # ── Marketplace fulfilment hook (Phase A) ──────────────────────────────────
    # Mirror of the Paystack path: if this order has a vendor (marketplace) product,
    # start the order fulfilment lifecycle (state="received" + buyer email + vendor
    # notify). No-op + idempotent for pure diamond/AFC orders, and never raises (so a
    # closed-tab webhook can never 500 on it). Imported lazily to avoid any import
    # cycle between this gateway module and fulfilment.py.
    from .fulfilment import notify_order_paid
    notify_order_paid(order)


# ── 1. stripe_buy_now (FE checkout -> create order + Stripe Checkout Session) ───────────────────
@api_view(["POST"])
def stripe_buy_now(request):
    """POST /shop/stripe-buy-now/  body: same shape as buy_now
        { items: [{variant_id, quantity, coupon_code?}], first_name, last_name, email,
          phone_number, address, city, state, postcode }

    The Stripe twin of afc_shop.views.buy_now. Same Bearer auth, same item + delivery-field
    validation, the SAME server-side coupon discount, then create Order(provider="stripe",
    status="pending") + OrderItems and open a Stripe Checkout Session. Returns
    { checkout_url, order_id, session_id }; the FE (CartDetails.tsx) redirects to checkout_url.

    AUTH: Bearer token -> validate_token (afc_auth), identical to buy_now."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session"}, status=401)

    items = request.data.get("items", [])
    if not items:
        return Response({"message": "Items required"}, status=400)

    # Same required delivery fields as buy_now.
    required_fields = ["first_name", "last_name", "email", "phone_number", "address", "city", "state", "postcode"]
    for field in required_fields:
        if not request.data.get(field):
            return Response({"message": f"{field} is required"}, status=400)

    # Price + validate the items (shared helper -> same rules as buy_now).
    subtotal, total_tax, order_items, err = _price_items(items)
    if err:
        return err

    # Re-apply the coupon discount SERVER-SIDE (shared helper -> same rules as buy_now).
    coupon, discount, err = _apply_coupon(items, subtotal)
    if err:
        return err

    grand_total = (subtotal + total_tax - discount).quantize(Decimal("0.01"))
    if grand_total < Decimal("0.00"):
        grand_total = Decimal("0.00")

    # Stripe Checkout cannot charge a zero-amount line, so guard against a 100%-off order.
    if grand_total <= Decimal("0.00"):
        return Response({"message": "Order total must be greater than zero to pay with Stripe."}, status=400)

    # Create the order + items in one transaction (mirrors buy_now), tagged provider="stripe".
    with transaction.atomic():
        order = Order.objects.create(
            user=user,
            provider="stripe",
            subtotal=subtotal,
            tax=total_tax,
            discount_total=discount,
            coupon=coupon,
            total=grand_total,
            status="pending",
            first_name=request.data.get("first_name"),
            last_name=request.data.get("last_name"),
            email=request.data.get("email"),
            phone_number=request.data.get("phone_number"),
            address=request.data.get("address"),
            city=request.data.get("city"),
            state=request.data.get("state"),
            postcode=request.data.get("postcode"),
        )

        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                variant=i["variant"],
                quantity=i["quantity"],
                unit_price=i["unit_price"],
                line_total=i["line_total"],
                # snapshot the applied coupon code per line; the order-level coupon FK is the
                # source of truth (same as buy_now).
                coupon_code=(coupon.code if coupon else None),
            )
            for i in order_items
        ])

    # ── saved delivery info (owner request 2026-06-29, mirrors buy_now) ──
    # Persist/link a SavedDeliveryProfile when the buyer ticked "save my info" or picked a
    # saved entry. Best-effort; the later update_fields saves don't touch saved_profile, so
    # persist it explicitly here. See afc_shop/delivery.py.
    from afc_shop.delivery import attach_delivery_profile
    attach_delivery_profile(order, user, request.data)
    if order.saved_profile_id:
        order.save(update_fields=["saved_profile"])

    # ── Stripe Checkout Session ────────────────────────────────────────────────────────────────
    # One line item per order line, priced via price_data in the shop currency. Adaptive Pricing
    # shows + charges the buyer in their local currency (same as the events flow). success_url +
    # cancel_url point back at the shop carrying the session id + order id so the FE can verify.
    currency = SHOP_CURRENCY.upper()
    base = getattr(settings, "FRONTEND_URL", "https://africanfreefirecommunity.com").rstrip("/")
    # Return to the SAME success page Paystack uses (/orders/success -> OrderSuccess.tsx). That page
    # already branches: ?reference=... -> Paystack verify; our ?stripe=success&session_id=...&
    # order_id=... -> POST /shop/stripe-verify/. {CHECKOUT_SESSION_ID} is the literal Stripe
    # placeholder; Stripe substitutes the real session id on redirect.
    success_url = f"{base}/orders/success?stripe=success&session_id={{CHECKOUT_SESSION_ID}}&order_id={order.id}"
    cancel_url = f"{base}/shop/cart?stripe=cancelled&order_id={order.id}"

    data = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "customer_email": order.email,
        "client_reference_id": str(order.id),
        "adaptive_pricing[enabled]": "true",
        "metadata[order_id]": str(order.id),
        "metadata[user_id]": str(user.user_id),
    }
    # Build the line items from the order's priced items (line_total already includes tax + the
    # per-line share of the discount is folded into the order total; to keep Stripe's total equal
    # to order.total we charge ONE line for the whole order total, with a clear product name, plus
    # per-item names in the description for the buyer's receipt).
    #
    # NOTE: we deliberately charge a single Stripe line equal to order.total (subtotal + tax -
    # discount). Splitting tax/discount across N Stripe lines risks rounding drift vs the order
    # total we already computed; one authoritative line keeps Stripe's charge exactly == order.total.
    line_names = ", ".join(
        f"{i['quantity']} x {i['variant'].product.name}" for i in order_items
    )[:240] or "AFC Shop order"
    data["line_items[0][price_data][currency]"] = currency.lower()
    data["line_items[0][price_data][product_data][name]"] = f"AFC Shop order #{order.id}"
    data["line_items[0][price_data][product_data][description]"] = line_names
    data["line_items[0][price_data][unit_amount]"] = _amount_minor(order.total, currency)
    data["line_items[0][quantity]"] = "1"

    ok, resp = _stripe("POST", "/checkout/sessions", data)
    if not ok:
        order.status = "failed"
        order.save(update_fields=["status"])
        return Response(
            {"message": "Could not start payment.", "detail": resp.get("error", {}).get("message", "")},
            status=502,
        )

    order.stripe_session_id = resp.get("id", "")
    order.save(update_fields=["stripe_session_id"])

    return Response({
        "checkout_url": resp.get("url"),
        "order_id": order.id,
        "session_id": resp.get("id"),
    }, status=200)


# ── 2. stripe_verify (FE success-page callback) ────────────────────────────────────────────────
@api_view(["POST"])
def stripe_verify(request):
    """POST /shop/stripe-verify/  { session_id } or { order_id }
    Retrieve the Checkout Session from Stripe; if payment_status == "paid" and the order is not
    already paid, mark it paid + bump the coupon + run the SAME fulfilment loop as
    verify_paystack_payment. Idempotent (already-paid orders just return 200).

    Called by the FE shop success page (CartDetails.tsx success return) after Stripe redirects back
    with the session id. No Bearer auth required (mirrors verify_paystack_payment, which is also
    unauthenticated: the session id / order id is the proof, and fulfilment is idempotent)."""
    session_id = request.data.get("session_id")
    order_id = request.data.get("order_id")

    order = None
    if order_id:
        order = Order.objects.prefetch_related("items__variant").filter(id=order_id, provider="stripe").first()
    elif session_id:
        order = Order.objects.prefetch_related("items__variant").filter(stripe_session_id=session_id, provider="stripe").first()

    if not order:
        return Response({"message": "Order not found"}, status=404)
    if order.status == "paid":
        return Response({"message": "Already processed", "status": "paid"}, status=200)
    if not order.stripe_session_id:
        return Response({"message": "No Stripe session on this order."}, status=400)

    ok, sess = _stripe("GET", f"/checkout/sessions/{order.stripe_session_id}")
    if not ok:
        return Response(
            {"message": "Could not verify payment.", "detail": sess.get("error", {}).get("message", "")},
            status=502,
        )

    if sess.get("payment_status") == "paid":
        _mark_paid_and_fulfil(order, sess.get("payment_intent"))
        return Response({"message": "Payment verified", "status": "paid"}, status=200)

    return Response({"message": "Payment not completed", "status": sess.get("payment_status", "unpaid")}, status=200)


# ── 3. stripe_webhook (server-side backstop for a closed tab) ──────────────────────────────────
@api_view(["POST"])
def stripe_webhook(request):
    """POST /shop/stripe-webhook/  Stripe -> us. Verifies the Stripe-Signature HMAC-SHA256 of
    "timestamp.body" with STRIPE_WEBHOOK_SECRET (mirrors event_payments.stripe_webhook exactly),
    and on checkout.session.completed marks the order paid + fulfils via the SAME helper as
    stripe_verify. A backstop for when the buyer closes the tab. Returns 200 quickly; never 500s."""
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)
    sig_header = request.headers.get("Stripe-Signature", "")
    body = request.body
    if secret:
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
            signed = f"{parts.get('t','')}.".encode() + body
            expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, parts.get("v1", "")):
                return Response({"message": "Bad signature."}, status=400)
        except Exception:
            return Response({"message": "Bad signature."}, status=400)
    # else: no secret configured yet -> accept (test setup); set the secret in prod.

    try:
        event = json.loads(body.decode())
    except Exception:
        return Response({"message": "Bad payload."}, status=400)

    if event.get("type") == "checkout.session.completed":
        obj = event.get("data", {}).get("object", {})
        oid = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id")
        if oid:
            order = Order.objects.prefetch_related("items__variant").filter(id=oid, provider="stripe").first()
            if order and obj.get("payment_status") == "paid":
                # Backstop: even an unexpected error while marking-paid/fulfilling must NOT turn into
                # a 500 (Stripe would then retry-storm this webhook). _mark_paid_and_fulfil already
                # commits the paid status before fulfilling and guards each voucher, so at worst the
                # paid status is recorded and a delivery is logged failed. We still wrap defensively.
                try:
                    _mark_paid_and_fulfil(order, obj.get("payment_intent"))
                except Exception:
                    pass

    return Response({"received": True}, status=200)
