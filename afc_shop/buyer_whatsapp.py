"""
afc_shop/buyer_whatsapp.py
================================================================================
BUYER side of the shop's WhatsApp channel: the three order updates AFC sends to
the person who PAID, plus the one reply they can send back.

This is the sibling of afc_shop/fulfilment.py notify_vendor. That module messages
the VENDOR (a business number, "here is a new order to fulfil"); this one messages
the BUYER (their own phone, "here is what is happening with your order"). The two
deliberately look the same, because they do the same job in opposite directions,
and a reader who understands one should not have to relearn the other.

WHY WHATSAPP AND NOT JUST EMAIL
    afc_shop/emails.py already sends the buyer a branded email at each milestone.
    Email in this market is the channel people check least: a large share of AFC
    buyers read WhatsApp within minutes and their inbox within days, if at all. So
    WhatsApp is added ALONGSIDE the email, never instead of it. Both are best
    effort and neither can block or undo a fulfilment transition.

THE THREE EVENTS (each is one Meta-approved template, all in language "en")

    "received"        -> WHATSAPP_ORDER_RECEIVED_TEMPLATE  ("order_received")
        {{1}} buyer name  {{2}} order reference  {{3}} what they ordered
        {{4}} order total with its currency
        Sent when the VENDOR ACKNOWLEDGES the order, not the instant it is paid.
        The gateway already confirms payment on screen and by email; the message
        the buyer actually wants is "a human has seen this and is packing it",
        which is exactly what the acknowledged transition means.

    "shipped"         -> WHATSAPP_ORDER_SHIPPED_TEMPLATE   ("order_shipped")
        {{1}} buyer name  {{2}} order reference  {{3}} what happens next
        {{3}} differs for a DIGITAL order (diamonds land on the game account, so
        there is nothing to track) and a PHYSICAL one (a parcel with a courier).
        Derived from the order's own lines, see _next_step_sentence.

    "delivered_check" -> WHATSAPP_ORDER_DELIVERED_TEMPLATE ("order_delivered_check")
        {{1}} buyer name  {{2}} order reference
        Sent on completion, and it is a QUESTION, not an announcement: "completed"
        is set by the vendor or an admin, which is a claim rather than proof. The
        two quick-reply buttons are how AFC finds out whether the buyer agrees.

A BLANK TEMPLATE NAME MEANS "DO NOT SEND", exactly as WHATSAPP_BROADCAST_TEMPLATE
works. An unconfigured deployment (a fresh server, a local dev box) then stays
silent instead of queueing sends Meta refuses with a template-not-found.

HOW IT CONNECTS
  - CALLED BY : afc_shop/fulfilment.py, at three points, always AFTER the state
                change has committed: apply_acknowledge -> "received",
                apply_mark_shipped -> "shipped", order_mark_completed ->
                "delivered_check".
  - CALLED BY : afc_whatsapp/webhooks.py for the inbound half.
                handle_inbound_message is registered in its INBOUND_HANDLERS list by
                dotted path, next to afc_shop.vendor_whatsapp.handle_inbound_message.
                Meta allows ONE callback URL per number, so every inbound message for
                the whole site is offered to both handlers and each ignores what is
                not its own.
  - CALLS     : afc_whatsapp.tasks.queue_template (the ONE outbound send surface: it
                writes the WhatsAppMessage row, honours the buyer's opt-in, normalises
                the number, hands the send to the "whatsapp" Celery queue, and never
                raises). afc_whatsapp.phone to_e164 / to_wa_id for the inbound sender
                match. afc_auth.canonical_profile for the fallback number.
  - MODELS    : Order (phone_number, first_name, total, items, buyer_confirmed_at),
                OrderItem -> ProductVariant -> Product -> Category (digital vs
                physical), afc_auth.UserProfile (the fallback WhatsApp number).
  - CONFIG    : WHATSAPP_ORDER_RECEIVED_TEMPLATE, WHATSAPP_ORDER_SHIPPED_TEMPLATE,
                WHATSAPP_ORDER_DELIVERED_TEMPLATE, WHATSAPP_ORDER_TEMPLATE_LANG
                (afc/settings.py, all env-driven).

COPY RULE: no em/en dashes in anything the buyer reads (AFC hard rule); commas,
colons, parentheses or a spaced hyphen instead.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from afc_auth.models import canonical_profile
from afc_whatsapp.phone import to_e164, to_wa_id
from afc_whatsapp.tasks import queue_template

from .models import Order

logger = logging.getLogger(__name__)


# ── The three templates, by event ──────────────────────────────────────────────
# event -> (settings attribute, the name Meta approved). Read at CALL time rather
# than import time (see _template_for) so override_settings works in tests, and so a
# server can repoint a template with an env var and a restart, no code change.
_TEMPLATE_SETTINGS = {
    "received": ("WHATSAPP_ORDER_RECEIVED_TEMPLATE", "order_received"),
    "shipped": ("WHATSAPP_ORDER_SHIPPED_TEMPLATE", "order_shipped"),
    "delivered_check": ("WHATSAPP_ORDER_DELIVERED_TEMPLATE", "order_delivered_check"),
}

# All three were approved together under language "en". Meta treats "en" and "en_US"
# as DIFFERENT templates and rejects a mismatch with error 132001, so the language is
# explicit here and shared by all three rather than guessed per send.
_TEMPLATE_LANG_DEFAULT = "en"

# ── The delivery-check quick replies ───────────────────────────────────────────
# The same "<action>:<order_id>" payload shape notify_vendor uses, for the same
# reason: Meta echoes the payload back verbatim on a tap, and the order id inside it
# is the only thing that maps a tap back to an order. The two ACTION prefixes are
# distinct from the vendor's ("ack", "shipdate", "shipped") so the two inbound
# handlers hanging off the one webhook can never claim each other's taps.
#   gotit  -> "Yes, received"  -> records Order.buyer_confirmed_at
#   notyet -> "No, not yet"    -> records NOTHING, logs loudly so ops can chase it
BUTTON_ACTIONS = {"gotit", "notyet"}

# How many line items the "what they ordered" variable names before it summarises the
# rest. A template variable is one short string in a chat bubble, and a ten line cart
# pasted into it would be unreadable as well as at risk of Meta's parameter length
# limit, so a long order reads "Jersey x1, Cooler x2, Mousepad x1 and 4 more items".
_ITEMS_NAMED = 3


# ─────────────────────────────────────────────────────────────────────────────
# Small render helpers (one per template variable)
# ─────────────────────────────────────────────────────────────────────────────
def _param(value):
    """One template variable, never empty.

    Meta REJECTS a template send whose parameter is an empty string, and it rejects
    the WHOLE message rather than the one variable, so a buyer who checked out without
    a first name would otherwise get nothing at all. A dash is the smallest thing that
    reads as "not set" in a chat bubble. It also collapses newlines, which Meta refuses
    in a parameter and which a pasted product name can easily carry.

    Same helper, same reasoning as afc_tournament_and_scrims/whatsapp_room_details.py.
    """
    text = " ".join(str(value or "").split())
    return text or "-"


def _buyer_name(order):
    """What to call the buyer: the checkout first name, falling back to their AFC
    username. Mirrors afc_shop/emails.py, which resolves the buyer the same way, so
    the email and the WhatsApp message greet them identically."""
    return order.first_name or getattr(order.user, "username", "")


def _order_reference(order):
    """The order reference the buyer sees. The shop has no separate human reference
    column, and the order id is what /orders, the vendor queue and every order email
    already show, so "#42" is the reference the buyer can actually quote back."""
    return f"#{order.id}"


def _items_summary(order):
    """A short human summary of what was bought, e.g. "AFC Jersey (M) x1, Cooler x2".

    Built from the OrderItem SNAPSHOT fields rather than the live product, so the
    message describes what the buyer actually paid for even if the product has been
    renamed since. Long orders are truncated, see _ITEMS_NAMED."""
    parts = []
    for item in order.items.all():
        name = item.product_name_snapshot
        if item.variant_title_snapshot:
            name = f"{name} ({item.variant_title_snapshot})"
        parts.append(f"{name} x{item.quantity}")

    if len(parts) > _ITEMS_NAMED:
        remaining = len(parts) - _ITEMS_NAMED
        noun = "item" if remaining == 1 else "items"
        return f"{', '.join(parts[:_ITEMS_NAMED])} and {remaining} more {noun}"
    return ", ".join(parts)


def _order_total(order):
    """The order total with the currency it was charged in, e.g. "NGN 10,000.00".

    The Order row carries no currency column because the shop charges through ONE
    currency (settings.SHOP_CURRENCY, NGN today) on both payment rails. That is the
    same assumption afc_shop/connect.py and afc_shop/paystack_payout.py make when they
    pay a vendor out of order.total, so this reads it from the same place rather than
    inventing a second answer."""
    currency = getattr(settings, "SHOP_CURRENCY", "NGN")
    try:
        amount = f"{Decimal(order.total):,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        # A malformed total must degrade to something readable, not lose the message.
        amount = str(order.total or "0")
    return f"{currency} {amount}"


def _order_is_digital(order):
    """True when NOTHING in this order gets shipped (a pure diamonds/top-up order).

    Decided per line, from the same signal the storefront uses for its shipping copy:
    Category.is_physical when the product has a category, falling back to the legacy
    `product_type` string for the pre-Category diamond rows that were never backfilled
    (see the Category model header in models.py). ONE physical line makes the whole
    order physical, because that line still has to reach an address.

    Only used to word the "shipped" message, so an order with no lines at all (which
    should not exist) reads as physical: the generic parcel sentence is the safer
    thing to tell somebody than promising diamonds that are not coming."""
    lines = list(order.items.all())
    if not lines:
        return False
    for item in lines:
        product = item.variant.product
        category = product.category
        if category is not None:
            physical = bool(category.is_physical)
        else:
            physical = product.product_type != "diamonds"
        if physical:
            return False
    return True


def _next_step_sentence(order):
    """The "what happens next" variable of the shipped template, one sentence.

    A DIGITAL order has no courier, no address and nothing to track, so telling that
    buyer to watch for a delivery is actively wrong. A PHYSICAL one does. The template
    is a single approved wording for both, so the difference has to live in the
    variable, which is why this exists at all."""
    if _order_is_digital(order):
        return (
            "There is nothing to track for this one: your diamonds go straight to "
            "your game account, usually within a few minutes."
        )
    return (
        "Your parcel is on its way with the courier, and we will check in with you "
        "once it should have arrived."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Who to message, and where
# ─────────────────────────────────────────────────────────────────────────────
def buyer_country(order):
    """The country to normalise this buyer's number against, or None.

    Neither number we might use is guaranteed to be in international form: the
    checkout phone field is free text, and a large share of AFC users type a local
    number ("08051234567"). afc_whatsapp.phone.to_e164 can only resolve that when it
    is told which numbering plan the number belongs to, so we take the country off the
    buyer's ACCOUNT using the SAME precedence fulfilment.vendor_country uses (and
    afc_auth.fx.user_currency before it): where the person actually is, falling back
    to the country on their account.

    Shared with the INBOUND side below so an outbound recipient and an inbound sender
    are normalised identically, which is what makes "is this tap from the buyer we
    messaged?" a reliable comparison."""
    user = getattr(order, "user", None)
    if user is None:
        return None
    return getattr(user, "ip_country", "") or getattr(user, "country", "") or None


def buyer_number(order):
    """The number to message this buyer on, or "".

    Order.phone_number FIRST: the buyer typed it at checkout for THIS order, so it is
    the number they expect to be contacted on about it, and a delivery phone is the
    one number a buyer double-checks. Their profile whatsapp_number is the fallback
    for the orders that carry no phone at all (digital top-ups skip the delivery form).

    canonical_profile, not .get(): UserProfile.user is a plain FK and duplicate rows
    exist in production, where .get() raises MultipleObjectsReturned."""
    number = (getattr(order, "phone_number", "") or "").strip()
    if number:
        return number

    user = getattr(order, "user", None)
    if user is None:
        return ""
    profile = canonical_profile(user)
    return (getattr(profile, "whatsapp_number", "") or "").strip()


def _template_for(event):
    """(template name, language) for one event, or ("", "") when it must not send.

    A BLANK name is a deliberate off switch, not a misconfiguration: a template Meta
    has not approved fails at send time and leaves a failed row behind for every
    order, so a deployment that has not registered these templates yet stays silent
    and keeps the email path it already had."""
    setting_name, default = _TEMPLATE_SETTINGS.get(event, ("", ""))
    if not setting_name:
        return "", ""
    template = getattr(settings, setting_name, default)
    if not template:
        return "", ""
    language = getattr(settings, "WHATSAPP_ORDER_TEMPLATE_LANG", _TEMPLATE_LANG_DEFAULT)
    return template, language


def _body_params(order, event):
    """The ordered {{1}}..{{N}} values for this event's approved template.

    THE ORDER IS THE CONTRACT. Meta freezes a template's body at approval, so these
    lists may only change when the template itself is re-approved with a new body.
    Every value goes through _param so an empty one cannot fail the whole send."""
    name = _param(_buyer_name(order))
    reference = _param(_order_reference(order))

    if event == "received":
        # {{1}} buyer, {{2}} order reference, {{3}} what they ordered, {{4}} total.
        return [name, reference, _param(_items_summary(order)), _param(_order_total(order))]

    if event == "shipped":
        # {{1}} buyer, {{2}} order reference, {{3}} what happens next.
        return [name, reference, _param(_next_step_sentence(order))]

    if event == "delivered_check":
        # {{1}} buyer, {{2}} order reference. The question itself is in the approved body.
        return [name, reference]

    return []


# ─────────────────────────────────────────────────────────────────────────────
# notify_buyer - the outbound half
# ─────────────────────────────────────────────────────────────────────────────
def notify_buyer(order, event):
    """Send the buyer the WhatsApp update for `event`. Best effort, NEVER raises.

    `event` is one of "received", "shipped", "delivered_check" (see the module header
    for what each means and which template it maps to). Anything else is ignored.

    Called from afc_shop/fulfilment.py immediately AFTER the matching transition has
    committed, so this can only ever describe a state change that really happened. It
    is deliberately impossible for it to undo one: the whole body is guarded, exactly
    like notify_vendor's send block, because a WhatsApp hiccup (a bad order field, a
    broker restart) must never turn a completed transition into a 500 for the vendor
    who drove it.

    Silently does nothing when the template is switched off (see _template_for) or the
    buyer has no number anywhere. Both are normal, not errors: plenty of AFC accounts
    have never given a phone number."""
    try:
        template, language = _template_for(event)
        if not template:
            # Not configured on this deployment. Debug, not warning: on a server with
            # no templates registered this would otherwise log once per transition.
            logger.debug("notify_buyer: '%s' template not configured, skipping order #%s",
                         event, order.id)
            return

        number = buyer_number(order)
        if not number:
            logger.info("notify_buyer: order #%s buyer has no WhatsApp number, skipping.",
                        order.id)
            return

        logger.info("notify_buyer: order #%s event=%s template=%s", order.id, event, template)

        # The delivery check is the only one that asks a question, so it is the only one
        # that carries buttons. "<action>:<order_id>", read back by handle_inbound_message
        # below; the two sides MUST agree on these prefixes (see BUTTON_ACTIONS).
        button_payloads = None
        if event == "delivered_check":
            button_payloads = [f"gotit:{order.id}", f"notyet:{order.id}"]

        queue_template(
            number,
            template,
            language,
            body_params=_body_params(order, event),
            button_payloads=button_payloads,
            # user=the buyer, UNLIKE notify_vendor which passes None. A vendor is
            # messaged on a business number that has nothing to do with their AFC
            # profile; the buyer IS an AFC account holder, so their profile opt-in
            # applies (queue_template refuses the send when they have switched WhatsApp
            # notifications off) and the message belongs on their row in the log.
            user=order.user,
            # queue_template would derive the same country from `user`, but naming it
            # keeps this call readable beside notify_vendor's and keeps the precedence
            # in ONE documented place (buyer_country) that the inbound half also uses.
            country=buyer_country(order),
            context=f"buyer_order_{event}",
        )
    except Exception as exc:  # WhatsApp must never block or undo a transition
        logger.warning("notify_buyer %s failed for order #%s: %s",
                       event, getattr(order, "id", "?"), exc)


# ─────────────────────────────────────────────────────────────────────────────
# The inbound half: the buyer answers the delivery check
# ─────────────────────────────────────────────────────────────────────────────
def _sender_is_order_buyer(order, sender_wa_id):
    """True when `sender_wa_id` is the number we sent THIS order's check to.

    Anyone can message the AFC business number, so a tap only counts when it comes
    from the buyer's own number, or a stranger could mark somebody else's order as
    delivered and end the chase for a parcel that never arrived.

    Both sides are reduced to bare digits through the SAME normalisation the outbound
    send used (to_e164 anchored on buyer_country, then to_wa_id), because the inbound
    `from` never carries a "+" while the stored number may be written any way at all.
    Mirrors vendor_whatsapp._sender_is_order_vendor."""
    stored = buyer_number(order)
    if not stored:
        return False
    normalised = to_e164(stored, buyer_country(order))
    stored_digits = to_wa_id(normalised) if normalised else to_wa_id(stored)
    return bool(stored_digits) and stored_digits == to_wa_id(sender_wa_id)


def _handle_button_tap(order, action):
    """Record the buyer's answer to the delivery check. Returns a log string.

    "gotit" is the only answer that writes anything: it stamps buyer_confirmed_at,
    which is AFC's only INDEPENDENT evidence that an order actually arrived (the
    "completed" state is the vendor's own claim). Idempotent, because Meta redelivers
    a webhook it did not get a 200 for and a buyer can tap twice: the FIRST
    confirmation is the true one, so a later tap leaves the timestamp alone.

    "notyet" deliberately writes nothing. It is a dispute, and quietly stamping a
    field for it would make the two answers indistinguishable later. It is logged at
    WARNING instead, which is the level ops actually watch, so somebody can chase the
    vendor for an order the buyer says never came."""
    if action == "gotit":
        if order.buyer_confirmed_at:
            return "already confirmed"
        order.buyer_confirmed_at = timezone.now()
        order.save(update_fields=["buyer_confirmed_at"])
        return "confirmed received"

    if action == "notyet":
        logger.warning(
            "buyer whatsapp: buyer says order #%s has NOT arrived (state=%s). Needs chasing.",
            order.id, order.fulfilment_state,
        )
        return "reported NOT received"

    return f"unknown action '{action}'"


def handle_inbound_message(message_entry):
    """Act on ONE inbound WhatsApp message if it answers a buyer delivery check.

    PURPOSE
        The buyer's half of the shared inbound webhook. Decides for itself whether a
        message is one of its taps and silently ignores everything else (a vendor's
        tap, a player's reply, a stranger), all of which afc_whatsapp has already
        logged and handled generically.

    REQUEST SHAPE (Meta's raw message entry, as it appears in
    entry[].changes[].value.messages[]; passed straight through by the caller)
        template quick-reply tap:
            {"from": "2348051234567", "id": "wamid...", "type": "button",
             "button": {"payload": "gotit:42", "text": "Yes, received"}}
        free-form interactive button tap (nothing sends these today, but a tap on an
        older message can still arrive):
            {"from": "...", "type": "interactive",
             "interactive": {"type": "button_reply",
                             "button_reply": {"id": "notyet:42", "title": "..."}}}

    RESPONSE SHAPE
        None. Side effects only (Order.buyer_confirmed_at, or a warning log) plus a
        line saying what it did. The HTTP response belongs to afc_whatsapp/webhooks.py,
        which always answers Meta 200.

    AUTH
        None of its own, by design: the caller has already proven the POST came from
        Meta with the X-Hub-Signature-256 HMAC. Authorisation for the ACTION is the
        sender check, the number must be the one this order's check was sent to.

    CALLER
        afc_whatsapp/webhooks.py, via its INBOUND_HANDLERS registry.

    NEVER RAISES: an exception escaping into the webhook would risk a non-200 and Meta
    escalating its retries against every AFC message, not just this one.
    """
    try:
        sender = message_entry.get("from", "") or ""
        msg_type = message_entry.get("type", "") or ""
        if msg_type not in ("interactive", "button"):
            return

        # TWO wire shapes, same meaning, so we read whichever arrived (identical to the
        # vendor handler: TEMPLATE quick-reply -> button.payload, free-form interactive
        # button -> interactive.button_reply.id).
        if msg_type == "interactive":
            interactive = message_entry.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            reply_id = reply.get("id", "") or ""
        else:
            reply_id = (message_entry.get("button") or {}).get("payload", "") or ""

        action, _, order_id = reply_id.partition(":")
        if not order_id or action not in BUTTON_ACTIONS or not order_id.isdigit():
            # Not ours: a vendor tap, another app's button, or junk. Not logged, because
            # every vendor tap on the site would otherwise produce a line here.
            return

        order = Order.objects.filter(id=int(order_id)).first()
        if not order:
            logger.info("buyer whatsapp: order #%s not found for reply '%s'", order_id, reply_id)
            return
        if not _sender_is_order_buyer(order, sender):
            logger.warning(
                "buyer whatsapp: sender %s is NOT order #%s buyer; ignoring tap.",
                sender, order_id,
            )
            return

        outcome = _handle_button_tap(order, action)
        logger.info("buyer whatsapp: order #%s tap '%s' -> %s", order_id, reply_id, outcome)
    except Exception as exc:
        # Defence in depth: the caller also wraps us, but a shop failure must never be
        # able to turn a valid Meta webhook into a non-200.
        logger.error("buyer whatsapp: error handling an inbound message: %s", exc)
