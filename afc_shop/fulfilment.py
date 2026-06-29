"""
afc_shop/fulfilment.py
================================================================================
The MARKETPLACE ORDER FULFILMENT STATE MACHINE (Phase A, spec:
WEBSITE/tasks/marketplace-design.md). ONE state machine + ONE API; the per-order
vendor web page and the WhatsApp (Kapso) bot are both thin clients of THIS module.
Kept isolated from views.py so the large legacy shop file is not churned.

THE STATE MACHINE (only physical/vendor orders enter it; digital diamond orders
never do):

    received  --vendor ack-->        acknowledged
    acknowledged  --vendor sets date-->  ship_scheduled
    ship_scheduled  --vendor marks shipped (+evidence)-->  shipped
    shipped  --admin/vendor completes-->  completed
    (any non-terminal) --cancel-->  cancelled     [cancel = future follow-up]

Each transition: (a) is a Bearer-auth endpoint here, (b) is permission-gated to the
order's vendor OR an AFC admin, (c) emits a buyer email where relevant
(afc_shop/emails.py), (d) is reachable from BOTH the future vendor page AND the
future Kapso webhook (they POST the same endpoints; the DB is the single source of
truth, logic is never duplicated in the channel).

WHO CALLS WHAT
  - `notify_order_paid(order)` is called from the TWO paid paths:
      * afc_shop.views.verify_paystack_payment (after the order is marked paid)
      * afc_shop.stripe_checkout._mark_paid_and_fulfil (the Stripe paid path)
    It is the entry point into the lifecycle: if the order has any vendor product
    it sets fulfilment_state="received", emails the buyer, and notifies the vendor.
  - The transition endpoints (vendor_acknowledge_order, vendor_set_ship_date,
    vendor_mark_shipped, order_mark_completed) + the vendor order list
    (vendor_my_orders) are registered in afc_shop/urls.py and will be consumed by
    the SEPARATE per-order vendor page + the SEPARATE Kapso WhatsApp flow.

MODELS TOUCHED
  Order (fulfilment_state / ship_date / acknowledged_at / shipped_at /
  completed_at), OrderItem -> ProductVariant -> Product.vendor -> Vendor.user (the
  permission edge), FulfillmentEvidence (shipped-proof media). Emails:
  afc_shop/emails.py (built on the afc_auth branded shell). Auth: afc_auth
  validate_token / require_admin.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
# Super-admin god-mode: a head_admin/super_admin managing-as a vendor (X-Act-As-Vendor
# header) sees that vendor's order queue. Inert for everyone else. See act_as.py.
from afc_auth.act_as import resolve_acting_vendor
from .models import FulfillmentEvidence, Order, Vendor
from . import emails

# Module logger. notify_vendor logs here today (the Kapso WhatsApp send plugs in
# at the marked seam); transition errors also log here for ops visibility.
logger = logging.getLogger(__name__)


# ── Valid forward transitions (the ONLY legal state jumps) ─────────────────────
# Maps a current fulfilment_state -> the set of states it may move to. Any jump not
# listed here is rejected with 400 (e.g. trying to ship an order that was never
# acknowledged). "cancelled" is reachable from any non-terminal state but the
# cancel endpoint itself is a SEPARATE follow-up, so it is not wired here yet.
VALID_TRANSITIONS = {
    "received": {"acknowledged"},
    "acknowledged": {"ship_scheduled"},
    "ship_scheduled": {"shipped"},
    "shipped": {"completed"},
    # "completed" / "cancelled" are terminal: no onward transitions.
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────
def _order_has_vendor_product(order):
    """True if ANY line in `order` is a vendor (marketplace) product.

    Walks order.items -> variant -> product.vendor. A diamond/AFC order has no
    vendor product, so it never enters the fulfilment lifecycle. Used by
    notify_order_paid to decide whether to start the state machine at all."""
    for item in order.items.all():
        # variant is PROTECT'd on OrderItem so it always exists; product.vendor_id
        # is the cheap nullable FK column (no extra query needed for the id).
        if item.variant.product.vendor_id:
            return True
    return False


def _order_vendor(order):
    """Return the Vendor that owns this order's products, or None.

    Phase A assumes an order's vendor products belong to a SINGLE vendor (the
    storefront groups a cart by vendor at checkout in a later phase). We return the
    first vendor found on any line, which is the vendor allowed to fulfil it."""
    for item in order.items.all():
        product = item.variant.product
        if product.vendor_id:
            return product.vendor
    return None


def _is_afc_admin(user):
    """True if `user` is an AFC admin. Reuses the SAME rule as
    afc_auth.require_admin (User.role == "admin"), also honouring Django staff/
    superuser flags from AbstractUser. Admins can drive any order's transitions."""
    if not user:
        return False
    return user.role == "admin" or user.is_staff or user.is_superuser


def _authorise(request, order):
    """Resolve the Bearer caller and check they may act on `order`.

    Permission rule (spec): the caller must be EITHER this order's vendor
    (order.items -> variant.product.vendor.user == caller) OR an AFC admin. Returns
    (user, vendor, error_response). On any auth failure `user` is None and
    error_response is a DRF Response to return as-is; on success error_response is
    None and (user, vendor) are set (vendor may be None when an admin acts).

    Called by every transition endpoint below so the gate is identical everywhere
    (and so the future vendor page + Kapso webhook share one auth path)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, None, Response({"message": "Invalid token"}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, None, Response({"message": "Invalid session"}, status=401)

    vendor = _order_vendor(order)

    # Admins may act on any order.
    if _is_afc_admin(user):
        return user, vendor, None

    # Otherwise the caller must be THIS order's vendor login.
    if vendor and vendor.user_id == user.user_id:
        # A suspended vendor cannot drive transitions (access revoked by admin).
        if vendor.status != "active":
            return None, None, Response({"message": "Your vendor access is suspended."}, status=403)
        return user, vendor, None

    return None, None, Response({"message": "You do not have permission for this order."}, status=403)


def _transition(order, target, *, set_fields=None):
    """Move `order.fulfilment_state` to `target`, enforcing VALID_TRANSITIONS.

    Returns (ok, error_response). If the jump is illegal (or the order is not in the
    lifecycle at all), ok is False and error_response is a 400 explaining why. On a
    legal jump it sets fulfilment_state=target plus any extra `set_fields`
    (e.g. {"shipped_at": now}) and saves only the touched columns. The save is the
    single mutation point so every transition records consistently.

    Locks the row inside a transaction so two channels (page + WhatsApp) racing the
    same transition can never double-apply: the second caller re-reads the freshly
    committed state and is rejected by the VALID_TRANSITIONS check."""
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        current = locked.fulfilment_state
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            return False, Response(
                {
                    "message": f"Cannot move order from '{current}' to '{target}'.",
                    "current_state": current,
                },
                status=400,
            )

        locked.fulfilment_state = target
        update_fields = ["fulfilment_state"]
        if set_fields:
            for field, value in set_fields.items():
                setattr(locked, field, value)
                update_fields.append(field)
        locked.save(update_fields=update_fields)

    # Hand back the refreshed instance so the caller's follow-up (emails) sees the
    # new state + timestamps.
    order.refresh_from_db()
    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# notify_vendor — STUB seam for the WhatsApp (Kapso) send (SEPARATE follow-up)
# ─────────────────────────────────────────────────────────────────────────────
def notify_vendor(order, event="received"):
    """Notify the order's vendor that something happened (best-effort WhatsApp BUTTONS
    via Kapso + an email heads-up).

    THE WHATSAPP CHANNEL (Kapso) is now two-way: this send puts TAPPABLE BUTTONS in
    front of the vendor whose reply ids ENCODE the order + action ("ack:<order_id>",
    "shipdate:<order_id>", "shipped:<order_id>"). When the vendor taps one, Meta/Kapso
    POSTs the tap to our INBOUND webhook (afc_shop/whatsapp_webhook.py), which parses
    "action:order_id" back out and drives the SAME _transition helpers below, so a tap
    advances the order exactly like the fulfilment page would. (See the button-id map
    in BUTTON_ACTIONS in whatsapp_webhook.py — the two sides MUST agree on these ids.)

    For now it:
      1. logs the event (so ops can see vendor notifications firing),
      2. if the vendor has a whatsapp_number, sends the approved "vendor_new_order"
         TEMPLATE via send_whatsapp_template (the only thing WhatsApp allows for COLD
         contact, outside the 24h window) carrying the 3 action buttons; if that send
         fails (e.g. template not approved yet), it FALLS BACK to send_whatsapp_buttons
         (free-form, in-window only) so nothing breaks before approval. Best-effort, and
      3. if the vendor has a contact_email, sends them a plain heads-up via the shared
         afc_auth send_email (best-effort; a mail failure never blocks the order).

    Called by notify_order_paid (event="received"). Safe to extend with more events
    (e.g. "cancelled") without touching the state machine."""
    vendor = _order_vendor(order)
    if not vendor:
        return  # not a vendor order; nothing to notify

    logger.info("notify_vendor: order #%s event=%s vendor=%s", order.id, event, vendor.display_name)

    # ── WhatsApp heads-up to the vendor via Kapso (best-effort, never blocks). ──
    # We send the order summary plus 3 reply BUTTONS. Each button's reply id encodes the
    # action and the order id as "action:order_id" so the inbound webhook can map a tap
    # straight back to THIS order (titles are capped at 20 chars by Meta + the kapso
    # client, so they stay short and human-readable). afc_shop.services.kapso.* never
    # raises (it returns {"ok": False, ...} on failure), but we still guard so notify
    # cannot block the payment/fulfilment path.
    if vendor.whatsapp_number:
        try:
            from django.conf import settings
            from afc_shop.services.kapso import send_whatsapp_template, send_whatsapp_buttons

            full_name = f"{order.first_name} {order.last_name}".strip()
            address = f"{order.address}, {order.city}, {order.state}".strip(", ")

            # reply-id / payload encoding = "<action>:<order_id>". Used IDENTICALLY by the
            # template (button.payload) and the free-form fallback (interactive.button_reply.id),
            # so a tap maps back to THIS order whichever channel delivered it. The inbound
            # webhook (whatsapp_webhook.py) enforces the real state machine, so an out-of-order
            # tap is harmlessly rejected. Titles <=20 chars (Meta limit) on the fallback buttons.
            ack_id = f"ack:{order.id}"
            shipdate_id = f"shipdate:{order.id}"
            shipped_id = f"shipped:{order.id}"

            # ── PRIMARY: the approved WhatsApp TEMPLATE. This is the ONLY thing WhatsApp
            # allows for COLD contact (outside the recipient's 24h window), so it is the
            # correct first-contact path for a brand-new order alert. Body vars
            # {{1}}..{{4}} = vendor name, order #, buyer, delivery address; the 3 quick-reply
            # buttons carry the action payloads. The template name + language come from
            # settings (KAPSO_TEMPLATE_NAME / KAPSO_TEMPLATE_LANG); the language MUST match
            # the template's approved locale in Meta (en_US != en) or Meta returns 132001. ──
            res = send_whatsapp_template(
                vendor.whatsapp_number,
                getattr(settings, "KAPSO_TEMPLATE_NAME", "vendor_new_order"),
                body_params=[vendor.display_name, str(order.id), full_name, address],
                button_payloads=[ack_id, shipdate_id, shipped_id],
                language_code=getattr(settings, "KAPSO_TEMPLATE_LANG", "en_US"),
            )

            # ── FALLBACK: if the template send failed (most commonly it is not approved yet
            # -> Meta 132001, or a language mismatch), drop to free-form interactive buttons.
            # These deliver ONLY inside the 24h window, but that covers active testing and any
            # vendor who messaged us recently, so the flow keeps working before the template is
            # live. Once the template is approved the PRIMARY path succeeds and this is skipped. ──
            if not res.get("ok"):
                logger.info(
                    "notify_vendor: template send failed for order #%s (%s); "
                    "falling back to interactive buttons.",
                    order.id, res.get("error"),
                )
                msg = (
                    f"New AFC order #{order.id} to fulfil.\n"
                    f"Buyer: {full_name}\n"
                    f"Deliver to: {address}\n"
                    f"Tap a button below to act on it, or open your fulfilment page on "
                    f"africanfreefirecommunity.com."
                )
                buttons = [
                    {"id": ack_id, "title": "Order received"},
                    {"id": shipdate_id, "title": "Set ship date"},
                    {"id": shipped_id, "title": "Mark shipped"},
                ]
                send_whatsapp_buttons(vendor.whatsapp_number, msg, buttons)
        except Exception as e:  # WhatsApp must never block the order
            logger.warning("notify_vendor whatsapp failed for order #%s: %s", order.id, e)

    # Best-effort email heads-up to the vendor in the meantime. Reuses the shared
    # SMTP sender; swallow any failure so notify never blocks the order.
    if vendor.contact_email:
        try:
            from afc_auth.views import send_email, _email_shell, SITE_URL
            full_name = f"{order.first_name} {order.last_name}".strip()
            inner = f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">You have a new order</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">Order #{order.id} is paid and ready to fulfil. Buyer: <span style="color:#e8efe9;font-weight:600;">{full_name}</span>. Open your fulfilment page on <a href="{SITE_URL}" style="color:#34d27b;text-decoration:none;">africanfreefirecommunity.com</a> to acknowledge it and set a ship date.</div>
  </td></tr>"""
            # i18n (owner 2026-06-15): localize the vendor heads-up to the vendor account's saved
            # language (Vendor.user -> afc_auth.User.language), falling back to English. send_email
            # translates the subject + visible body text.
            vendor_lang = (getattr(getattr(vendor, "user", None), "language", "") or "en")
            send_email(vendor.contact_email, f"New AFC order #{order.id} to fulfil", _email_shell(inner, "green"), language=vendor_lang)
        except Exception as e:  # mail must never block the order
            logger.warning("notify_vendor email failed for order #%s: %s", order.id, e)


# ─────────────────────────────────────────────────────────────────────────────
# notify_order_paid — the lifecycle ENTRY POINT (called from both paid paths)
# ─────────────────────────────────────────────────────────────────────────────
def notify_order_paid(order):
    """Start the fulfilment lifecycle for a JUST-PAID order. Idempotent.

    CALLED BY (one line each, after the existing paid logic):
      - afc_shop.views.verify_paystack_payment  (Paystack paid path)
      - afc_shop.stripe_checkout._mark_paid_and_fulfil  (Stripe paid path)

    Behaviour:
      - If the order has NO vendor product (a pure diamond/AFC order), do nothing
        (digital orders never enter this lifecycle; they use the voucher
        Fulfillment flow).
      - Otherwise, if the order is not already in the lifecycle, set
        fulfilment_state="received", email the BUYER the "order received" email,
        and call notify_vendor(event="received").

    IDEMPOTENCY: guarded on fulfilment_state being unset. If notify_order_paid is
    called twice (verify + webhook race, or a re-verify), the second call sees a
    non-null fulfilment_state and returns immediately, so the buyer is emailed and
    the vendor is notified exactly once. Wrapped so a notify/email hiccup can never
    bubble a 500 back into the payment-verify path."""
    try:
        # Re-fetch with items prefetched so the vendor walk + email render are cheap.
        order = Order.objects.prefetch_related("items__variant__product__vendor").get(pk=order.pk)

        # Not a marketplace order, or already started -> nothing to do (idempotent).
        if not _order_has_vendor_product(order):
            return
        if order.fulfilment_state:
            return

        # Enter the lifecycle: mark received.
        order.fulfilment_state = "received"
        order.save(update_fields=["fulfilment_state"])

        # Email the BUYER ("we received your order"). Best-effort: emails.* swallows
        # send failures and we wrap defensively so mail never blocks payment.
        try:
            emails.send_order_received(order)
        except Exception as e:
            logger.warning("order-received email failed for order #%s: %s", order.id, e)

        # Notify the vendor (log + email today; Kapso WhatsApp send later).
        notify_vendor(order, event="received")
    except Exception as e:
        # This runs INSIDE the payment-verify path; a failure here must never turn a
        # successful payment into an error response. Log and move on.
        logger.error("notify_order_paid failed for order #%s: %s", getattr(order, "id", "?"), e)


# ─────────────────────────────────────────────────────────────────────────────
# Transition endpoints (vendor page + Kapso bot both POST these)
# ─────────────────────────────────────────────────────────────────────────────
def _get_order_or_404(order_id):
    """Fetch an order with its items prefetched (for the vendor walk + emails), or
    None. Shared by the transition endpoints below."""
    return (
        Order.objects.prefetch_related("items__variant__product__vendor")
        .filter(id=order_id)
        .first()
    )


# ── Shared transition CORES (called by BOTH the HTTP endpoints AND the Kapso webhook) ──
# The HTTP endpoints do their OWN Bearer auth (_authorise); the Kapso inbound webhook
# resolves the vendor from the sender's WhatsApp number instead (no token). Both then
# call these cores so the state change + buyer email happen IDENTICALLY no matter which
# front door drove it (the DB is the single source of truth; logic is never duplicated).
def apply_acknowledge(order):
    """received -> acknowledged (+ acknowledged_at). Returns (ok, err_response).
    No buyer email at this step (the buyer was already told "received")."""
    return _transition(order, "acknowledged", set_fields={"acknowledged_at": timezone.now()})


def apply_set_ship_date(order, ship_date):
    """acknowledged -> ship_scheduled (+ ship_date). Returns (ok, err_response)."""
    return _transition(order, "ship_scheduled", set_fields={"ship_date": ship_date})


def apply_mark_shipped(order):
    """ship_scheduled -> shipped (+ shipped_at), then email the buyer "on the way".
    Returns (ok, err_response). Evidence is attached SEPARATELY by the caller (the
    HTTP endpoint stores request.FILES; the webhook stores the inbound media), so this
    core only owns the state change + the buyer email both paths share."""
    ok, err = _transition(order, "shipped", set_fields={"shipped_at": timezone.now()})
    if not ok:
        return ok, err
    try:
        emails.send_order_shipped(order)
    except Exception as e:  # best-effort; never block the transition on a mail failure
        logger.warning("order-shipped email failed for order #%s: %s", order.id, e)
    return True, None


@api_view(["POST"])
def vendor_acknowledge_order(request):
    """POST /shop/fulfilment/acknowledge/  body: { order_id }

    Transition: received -> acknowledged (+ sets acknowledged_at). The vendor (or an
    AFC admin) confirms they have seen the order and will fulfil it. No buyer email
    at this step (the buyer was already told "received"; the next email is at
    "shipped").

    AUTH: Bearer -> _authorise (this order's vendor OR an AFC admin).
    CONSUMED BY: the per-order vendor page "Order received" button + the Kapso
    "Order received" WhatsApp button (both SEPARATE follow-ups)."""
    order = _get_order_or_404(request.data.get("order_id"))
    if not order:
        return Response({"message": "Order not found"}, status=404)

    user, vendor, err = _authorise(request, order)
    if err:
        return err

    # Delegate the state change to the shared core (also used by the Kapso webhook).
    ok, err = apply_acknowledge(order)
    if not ok:
        return err

    return Response({"message": "Order acknowledged", "fulfilment_state": order.fulfilment_state}, status=200)


@api_view(["POST"])
def vendor_set_ship_date(request):
    """POST /shop/fulfilment/set-ship-date/  body: { order_id, ship_date: 'YYYY-MM-DD' }

    Transition: acknowledged -> ship_scheduled (+ stores the vendor-picked
    ship_date). No buyer email here (the ship date is surfaced in the later
    "shipped" email).

    AUTH: Bearer -> _authorise (vendor OR admin).
    CONSUMED BY: the vendor page date picker + the Kapso "Set ship date" flow."""
    order = _get_order_or_404(request.data.get("order_id"))
    if not order:
        return Response({"message": "Order not found"}, status=404)

    user, vendor, err = _authorise(request, order)
    if err:
        return err

    ship_date = request.data.get("ship_date")
    if not ship_date:
        return Response({"message": "ship_date is required"}, status=400)

    # Delegate the state change to the shared core (also used by the Kapso webhook).
    ok, err = apply_set_ship_date(order, ship_date)
    if not ok:
        return err

    return Response(
        {"message": "Ship date set", "fulfilment_state": order.fulfilment_state, "ship_date": str(order.ship_date)},
        status=200,
    )


@api_view(["POST"])
def vendor_mark_shipped(request):
    """POST /shop/fulfilment/mark-shipped/  (multipart) body: { order_id }
       files: any uploaded image/video fields -> stored as FulfillmentEvidence rows.

    Transition: ship_scheduled -> shipped (+ sets shipped_at). Stores each uploaded
    photo/video as a FulfillmentEvidence row (proof of dispatch), then emails the
    BUYER "your order is on the way".

    AUTH: Bearer -> _authorise (vendor OR admin).
    CONSUMED BY: the vendor page "Mark shipped" + evidence upload, and the Kapso
    "Mark shipped" flow (its inbound photo/video also lands as FulfillmentEvidence
    via the SEPARATE Kapso media webhook)."""
    order = _get_order_or_404(request.data.get("order_id"))
    if not order:
        return Response({"message": "Order not found"}, status=404)

    user, vendor, err = _authorise(request, order)
    if err:
        return err

    # Advance state first (shared core: state change + buyer "on the way" email). If it
    # is an illegal jump we do not store evidence. The Kapso webhook calls this same core.
    ok, err = apply_mark_shipped(order)
    if not ok:
        return err

    # Store any uploaded files as evidence. request.FILES is a MultiValueDict; accept
    # every uploaded file under any field name (the page may send "evidence",
    # multiple files, etc.). kind is inferred from the content type prefix. (The Kapso
    # webhook stores its inbound photo/video as evidence on its own side.)
    saved = 0
    note = request.data.get("note", "")
    for _field, upload in request.FILES.items():
        kind = "video" if (upload.content_type or "").startswith("video/") else "image"
        FulfillmentEvidence.objects.create(
            order=order,
            media=upload,
            kind=kind,
            uploaded_by=user,
            note=note,
        )
        saved += 1

    return Response(
        {"message": "Order marked shipped", "fulfilment_state": order.fulfilment_state, "evidence_saved": saved},
        status=200,
    )


@api_view(["POST"])
def order_mark_completed(request):
    """POST /shop/fulfilment/mark-completed/  body: { order_id }

    Transition: shipped -> completed (+ sets completed_at), then emails the BUYER
    "your order is complete". Either the vendor OR an AFC admin may complete an
    order (e.g. admin closes it out, or the buyer confirms delivery to the vendor
    who completes it).

    AUTH: Bearer -> _authorise (vendor OR admin).
    CONSUMED BY: the vendor page "Mark delivered" + an admin order action + the
    Kapso flow."""
    order = _get_order_or_404(request.data.get("order_id"))
    if not order:
        return Response({"message": "Order not found"}, status=404)

    user, vendor, err = _authorise(request, order)
    if err:
        return err

    ok, err = _transition(order, "completed", set_fields={"completed_at": timezone.now()})
    if not ok:
        return err

    try:
        emails.send_order_completed(order)
    except Exception as e:
        logger.warning("order-completed email failed for order #%s: %s", order.id, e)

    # ── PROVIDER-AWARE vendor payout hook (Phase B3) ───────────────────────────
    # The order is now completed -> AFC owes the vendor their share. AFC pays out on the
    # vendor's CHOSEN rail (Vendor.payout_provider):
    #   - "paystack" (DEFAULT, African/Nigerian vendors): settle_order_payout_paystack
    #     (afc_shop/paystack_payout.py) -> a Paystack Transfer to the vendor's saved bank.
    #     This is the primary rail because Stripe Connect cannot pay out to NGN/most-African
    #     banks, but Paystack can, and the shop already charges via Paystack.
    #   - "stripe" (non-African vendors only): settle_order_payout (afc_shop/connect.py)
    #     -> a Stripe Transfer to the vendor's connected Stripe account.
    # Both compute order.total minus the MARKETPLACE_FEE_PERCENT platform fee, write the
    # SAME VendorPayout ledger row (idempotent, one per order), and either pay it ("paid")
    # or record it "owed" for an admin to release/retry once the vendor's payout details
    # exist. Both are best-effort + NEVER raise, so a payout hiccup can never undo the
    # completion above. Imported lazily to avoid any import cycle.
    try:
        payout_vendor = _order_vendor(order)
        if payout_vendor and payout_vendor.payout_provider == "stripe":
            # Non-African vendor on the Stripe Connect rail.
            from .connect import settle_order_payout
            settle_order_payout(order)
        else:
            # Default rail: Paystack Transfers (covers payout_provider="paystack" and any
            # vendor without an explicit provider — the model defaults to "paystack").
            from .paystack_payout import settle_order_payout_paystack
            settle_order_payout_paystack(order)
    except Exception as e:  # payout must never block / 500 the completion
        logger.warning("vendor payout failed for order #%s: %s", order.id, e)

    return Response({"message": "Order completed", "fulfilment_state": order.fulfilment_state}, status=200)


@api_view(["GET"])
def vendor_my_orders(request):
    """GET /shop/fulfilment/my-orders/

    Return the CALLER-vendor's own orders + fulfilment state, PII-SCOPED to only
    what a vendor needs to fulfil. This is the vendor's fulfilment queue.

    AUTH: Bearer -> validate_token; the caller must have a Vendor account (else
    403). Admins are NOT the audience here (they have the admin order views); this
    endpoint is specifically the vendor's own list.

    PII FIREWALL (spec: like the partner API): a vendor sees the buyer NAME, the
    DELIVERY ADDRESS (they have to ship there), the items + quantities, and the
    fulfilment state. They do NOT get the buyer's email, phone, account id, payment
    references, or money internals.

    CONSUMED BY: the per-order vendor page / vendor dashboard order list (SEPARATE
    follow-up) and, indirectly, the Kapso flow when it lists a vendor's orders."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session"}, status=401)

    # ── super-admin god-mode (afc_auth.act_as) ──
    # A super admin managing-as a vendor (X-Act-As-Vendor header) sees THAT vendor's order
    # queue; resolve_acting_vendor is inert for non-god-mode callers, so a normal caller
    # still resolves only their own Vendor. Order STATE transitions already allow admins
    # via fulfilment._authorise, so no separate change is needed for those.
    vendor = resolve_acting_vendor(request, user) or Vendor.objects.filter(user=user).first()
    if not vendor:
        return Response({"message": "You are not a vendor."}, status=403)

    # All PAID orders that contain at least one of THIS vendor's products, with the
    # data needed to render the queue. distinct() because an order can have several
    # of the vendor's lines.
    orders = (
        Order.objects.filter(
            items__variant__product__vendor=vendor,
            status="paid",
        )
        .prefetch_related("items")
        .order_by("-created_at")
        .distinct()
    )

    results = []
    for order in orders:
        # PII firewall: only the buyer name + delivery address + items + state.
        results.append({
            "order_id": order.id,
            "fulfilment_state": order.fulfilment_state,
            "ship_date": str(order.ship_date) if order.ship_date else None,
            "buyer_name": f"{order.first_name} {order.last_name}".strip(),
            "delivery": {
                "address": order.address,
                "city": order.city,
                "state": order.state,
                "postcode": order.postcode,
            },
            "items": [
                {
                    "name": item.product_name_snapshot,
                    "variant": item.variant_title_snapshot,
                    "quantity": item.quantity,
                }
                for item in order.items.all()
            ],
            "created_at": order.created_at,
        })

    # Pagination metadata shape (best-practice): a small queue today, but return the
    # count so a future paginated vendor dashboard has it.
    return Response({"count": len(results), "results": results}, status=200)
