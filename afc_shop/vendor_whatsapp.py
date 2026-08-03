"""
afc_shop/vendor_whatsapp.py
================================================================================
MARKETPLACE side of the WhatsApp fulfilment channel: what happens when a VENDOR
messages the AFC WhatsApp number back.

This is the RECEIVING half of the two-way vendor flow. The SENDING half lives in
afc_shop/fulfilment.py notify_vendor, which queues the approved "vendor_new_order"
template with three quick-reply buttons whose payloads encode "<action>:<order_id>".
When the vendor TAPS one, or REPLIES with a photo/document (a shipping receipt),
Meta delivers it here and we map it back to the order and advance the SAME
fulfilment state machine the vendor web page uses. The DB is the single source of
truth; there is NO fulfilment logic in this file that the page does not also have,
only the channel-specific parsing and routing.

WHY THIS FILE EXISTS AT ALL (the Kapso cutover, 2026-08-03)
    Until now afc_shop owned its own WhatsApp webhook endpoint at
    /shop/whatsapp/webhook/, because the Kapso middleman was configured to deliver
    the marketplace's events there. AFC now talks to Meta directly, and Meta allows
    ONE callback URL per WhatsApp number: /whatsapp/webhook/ (afc_whatsapp/webhooks.py).
    Every inbound message for the whole site arrives there.

    Rather than move marketplace logic into afc_whatsapp (which must stay generic:
    it owns the message log, consent, and delivery receipts, and knows nothing about
    orders), afc_whatsapp offers each inbound message to the app handlers registered
    in its INBOUND_HANDLERS list. This module is that handler for afc_shop, so the
    marketplace keeps owning its own business logic and afc_shop keeps its own tests.

HOW IT CONNECTS
  - CALLED BY : afc_whatsapp/webhooks.py _dispatch_to_app_handlers, once per inbound
                message, AFTER that app has logged the message and honoured any
                opt-out. It is registered there by dotted path as
                "afc_shop.vendor_whatsapp.handle_inbound_message".
  - CALLS     : afc_shop/fulfilment.py apply_acknowledge / apply_set_ship_date /
                apply_mark_shipped (the shared transition cores) + _order_vendor +
                vendor_country. afc_shop/services/whatsapp_media.py download_media
                (the bytes behind an inbound photo). afc_whatsapp/phone.py to_e164 /
                to_wa_id (so a sender number and a stored number are compared in the
                same normalised form).
  - MODELS    : Order (the target), Vendor (the sender match), FulfillmentEvidence
                (inbound media stored as proof of shipment).
  - CONFIG    : none of its own. The webhook's signature verification
                (WHATSAPP_APP_SECRET) and the media download's credentials
                (WHATSAPP_ACCESS_TOKEN) are checked upstream of this module.

SECURITY
    The endpoint itself is already proven to be Meta before we are called: every POST
    to /whatsapp/webhook/ must carry a valid X-Hub-Signature-256 HMAC or it is
    rejected with 403 (afc_whatsapp/webhooks.py verify_signature). On top of that we
    check the SENDER: an action only runs when the sender's number is the number on
    the order's own vendor, so a stranger (or another vendor) messaging our number
    cannot drive somebody else's order. A mismatch is logged and ignored.

ROBUSTNESS
    handle_inbound_message NEVER raises. Meta escalates its retries against any
    response that is not 200, so a crash in here must not become a retry storm; the
    caller also wraps us, and we wrap ourselves, so a failure is logged and dropped.
"""

import logging

from django.core.files.base import ContentFile

from afc_whatsapp.phone import to_e164, to_wa_id

from .models import FulfillmentEvidence, Order, Vendor
from .fulfilment import (
    _order_vendor,
    apply_acknowledge,
    apply_mark_shipped,
    vendor_country,
)
from .services.whatsapp_media import download_media

logger = logging.getLogger(__name__)


# ── button-payload action map ──────────────────────────────────────────────────
# The payloads notify_vendor (fulfilment.py) encodes as "<action>:<order_id>". The
# KEY is the action prefix; the comment names which transition the tap drives. The
# two files MUST agree on these prefixes (a tap is meaningless if they drift).
#   ack      -> received        -> acknowledged
#   shipped  -> ship_scheduled  -> shipped
#   shipdate -> acknowledged    -> ship_scheduled  (needs a date; handled specially)
BUTTON_ACTIONS = {"ack", "shipdate", "shipped"}

# Inbound media types (Meta) we accept as shipment evidence and how each maps to our
# FulfillmentEvidence.kind. "document" (a scanned receipt or label) is stored as an
# image kind for display purposes; "audio" is not evidence and is ignored.
MEDIA_KINDS = {"image": "image", "video": "video", "document": "image"}

# Fulfilment states in which an inbound photo/document is meaningfully "shipment
# evidence". Outside these a photo is noise and is not stored.
EVIDENCE_STATES = {"ship_scheduled", "shipped"}


# ─────────────────────────────────────────────────────────────────────────────
# Sender identification
# ─────────────────────────────────────────────────────────────────────────────
def _vendor_wa_id(vendor):
    """A vendor's stored WhatsApp number in the form Meta uses on the wire: digits,
    no leading "+". Returns "" when the vendor has no number.

    Normalised through afc_whatsapp.phone the SAME way the outbound send normalises
    it, anchored on the vendor's country, so a number stored locally ("08051234567")
    still compares equal to the sender id Meta reports ("2348051234567"). The old
    Kapso comparison stripped punctuation only, so those two never matched and a
    vendor with a locally-written number could never drive their own order.

    Falls back to the raw digits when the number cannot be normalised (no country on
    the account), which is no worse than the behaviour it replaces."""
    raw = getattr(vendor, "whatsapp_number", "") or ""
    if not raw:
        return ""
    normalised = to_e164(raw, vendor_country(vendor))
    return to_wa_id(normalised) if normalised else to_wa_id(raw)


def _sender_is_order_vendor(order, sender_wa_id):
    """Return the Vendor when `sender_wa_id` is the number on THIS order's vendor,
    else None so the caller can log and skip an impostor.

    Both sides are reduced to bare digits first: the inbound `from` never carries a
    "+", while the stored number may be written any way at all."""
    vendor = _order_vendor(order)
    if not vendor:
        return None
    vendor_digits = _vendor_wa_id(vendor)
    if vendor_digits and vendor_digits == to_wa_id(sender_wa_id):
        return vendor
    return None


def _vendor_for_sender(sender_wa_id):
    """The Vendor whose stored WhatsApp number is this sender, or None.

    Vendor numbers are stored RAW, in every shape a human types, so the comparison has
    to happen in Python after normalisation rather than in SQL. Scanning the vendors is
    cheap and stays cheap: vendors are INVITE-ONLY (an admin creates each one), so this
    is tens of rows, not a user table."""
    digits = to_wa_id(sender_wa_id)
    if not digits:
        return None
    vendors = Vendor.objects.exclude(whatsapp_number="").select_related("user")
    for vendor in vendors:
        if _vendor_wa_id(vendor) == digits:
            return vendor
    return None


def _latest_evidence_order_for_sender(sender_wa_id):
    """Find the sender-vendor's most recent order that is awaiting shipment evidence.

    Inbound media carries no order id, so we attach it to the vendor's newest order in
    ship_scheduled/shipped: a vendor can simply reply with a photo to the order they
    were last asked about. Returns the Order, or None for an unknown sender or when
    they have nothing awaiting evidence.

    Resolves the SENDER to a vendor first and then filters that vendor's orders in SQL,
    rather than walking every order awaiting evidence and asking who owns it. Otherwise
    any stranger's photo would drag the whole open-order set through Python."""
    vendor = _vendor_for_sender(sender_wa_id)
    if vendor is None:
        return None

    return (
        Order.objects.filter(
            fulfilment_state__in=EVIDENCE_STATES,
            items__variant__product__vendor=vendor,
        )
        .prefetch_related("items__variant__product__vendor")
        .order_by("-created_at")
        .distinct()
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
# The two things a vendor can do from WhatsApp
# ─────────────────────────────────────────────────────────────────────────────
def _handle_button_tap(order, reply_id):
    """Route a tapped button ("<action>:<order_id>") to the matching transition core.

    Advances the order through the SAME cores the vendor page uses, so the two front
    doors can never drift. Returns a short result string for the log. The caller has
    already checked that the sender is this order's vendor. An out-of-order tap (say
    "shipped" before a ship date was set) is refused by the core's VALID_TRANSITIONS
    guard, so a wrong tap is a harmless no-op rather than a corrupt state."""
    action = reply_id.split(":", 1)[0]

    if action == "ack":
        ok, _err = apply_acknowledge(order)
        return f"ack -> {'acknowledged' if ok else 'rejected'}"

    if action == "shipped":
        ok, _err = apply_mark_shipped(order)
        return f"shipped -> {'shipped' if ok else 'rejected'}"

    if action == "shipdate":
        # A quick-reply button carries no date, so ship_scheduled cannot be set from a
        # bare tap. The vendor sets the date on the fulfilment page (or an admin does).
        # No state change here; logged so ops can see the tap happened.
        return "shipdate -> prompt (no date in a button tap)"

    return f"unknown action '{action}'"


def _handle_inbound_media(order, message_entry, media_type):
    """Download an inbound photo/document/video and store it as FulfillmentEvidence.

    Only meaningful while the order is in EVIDENCE_STATES; a photo at any other stage
    is ignored as noise. Returns a short result string for the log. Best-effort: a
    failed download is logged and skipped, never raised."""
    if order.fulfilment_state not in EVIDENCE_STATES:
        return f"media ignored (state={order.fulfilment_state})"

    media_obj = message_entry.get(media_type) or {}
    media_id = media_obj.get("id")
    if not media_id:
        return "media ignored (no media id)"

    result = download_media(media_id)
    if not result.get("ok"):
        logger.warning(
            "vendor whatsapp: media download failed for order #%s: %s",
            order.id, result.get("error"),
        )
        return "media download failed"

    # Pick a sensible file extension from the mime type for the stored file name.
    mime = result.get("mime_type", "") or media_obj.get("mime_type", "")
    ext = mime.split("/")[-1].split(";")[0] if "/" in mime else "bin"
    kind = MEDIA_KINDS.get(media_type, "image")

    FulfillmentEvidence.objects.create(
        order=order,
        media=ContentFile(result["content"], name=f"whatsapp_order{order.id}_{media_id}.{ext}"),
        kind=kind,
        uploaded_by=None,  # inbound from WhatsApp: no AFC/vendor session to attribute
        note=f"Inbound WhatsApp media ({media_type}) from the vendor.",
    )
    return f"evidence stored ({kind})"


# ─────────────────────────────────────────────────────────────────────────────
# The entry point afc_whatsapp calls
# ─────────────────────────────────────────────────────────────────────────────
def handle_inbound_message(message_entry):
    """Act on ONE inbound WhatsApp message if it is marketplace business.

    PURPOSE
        The marketplace's half of the shared inbound webhook. Decides for itself
        whether a message concerns an order at all, and silently ignores everything
        else (a player's reply, a stranger, an opt-out: all already handled generically
        by afc_whatsapp).

    REQUEST SHAPE (Meta's raw message entry, as it appears in
    entry[].changes[].value.messages[]; passed straight through by the caller)
        template quick-reply tap:
            {"from": "2348051234567", "id": "wamid...", "type": "button",
             "button": {"payload": "ack:42", "text": "Order received"}}
        free-form interactive button tap (kept for completeness; nothing sends these
        today, but a tap on an older message can still arrive):
            {"from": "...", "type": "interactive",
             "interactive": {"type": "button_reply",
                             "button_reply": {"id": "ack:42", "title": "..."}}}
        media:
            {"from": "...", "type": "image",
             "image": {"id": "<media id>", "mime_type": "image/jpeg"}}

    RESPONSE SHAPE
        None. Side effects only (a fulfilment transition and/or a FulfillmentEvidence
        row) plus a log line saying what it did. The HTTP response belongs to
        afc_whatsapp/webhooks.py, which always answers Meta 200.

    AUTH
        None of its own, by design. The caller has already proven the POST came from
        Meta with the X-Hub-Signature-256 HMAC. Authorisation for the ACTION is the
        sender check below: the number must be the one on the order's vendor.

    CALLER
        afc_whatsapp/webhooks.py, via its INBOUND_HANDLERS registry.

    NEVER RAISES: the whole body is guarded, because an exception escaping into the
    webhook would risk a non-200 and Meta escalating its retries.
    """
    try:
        sender = message_entry.get("from", "") or ""
        msg_type = message_entry.get("type", "") or ""

        # ── (a) a tapped button whose payload encodes "<action>:<order_id>" ──
        # TWO wire shapes, same meaning, so we read whichever arrived:
        #   TEMPLATE quick-reply (what notify_vendor sends) -> type "button",
        #       button.payload == "<action>:<order_id>" (button.text is the label).
        #   FREE-FORM interactive button -> type "interactive",
        #       interactive.button_reply.id == "<action>:<order_id>".
        if msg_type in ("interactive", "button"):
            if msg_type == "interactive":
                interactive = message_entry.get("interactive") or {}
                reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                reply_id = reply.get("id", "") or ""
            else:
                reply_id = (message_entry.get("button") or {}).get("payload", "") or ""

            action, _, order_id = reply_id.partition(":")
            if not order_id or action not in BUTTON_ACTIONS or not order_id.isdigit():
                # Not ours: another app's button, or a tap with no order encoding.
                logger.info("vendor whatsapp: ignoring reply id '%s'", reply_id)
                return

            order = (
                Order.objects.prefetch_related("items__variant__product__vendor")
                .filter(id=int(order_id))
                .first()
            )
            if not order:
                logger.info("vendor whatsapp: order #%s not found for reply '%s'", order_id, reply_id)
                return
            if not _sender_is_order_vendor(order, sender):
                logger.warning(
                    "vendor whatsapp: sender %s is NOT order #%s vendor; ignoring tap.",
                    sender, order_id,
                )
                return

            outcome = _handle_button_tap(order, reply_id)
            logger.info("vendor whatsapp: order #%s tap '%s' -> %s", order_id, reply_id, outcome)
            return

        # ── (b) inbound media: store it as shipment evidence ──
        if msg_type in MEDIA_KINDS:
            order = _latest_evidence_order_for_sender(sender)
            if not order:
                # Normal for anyone who is not a vendor mid-shipment, so this is info,
                # not a warning: players send photos to this number too.
                logger.info(
                    "vendor whatsapp: media from %s but no order awaiting evidence; ignoring.",
                    sender,
                )
                return
            outcome = _handle_inbound_media(order, message_entry, msg_type)
            logger.info("vendor whatsapp: order #%s media -> %s", order.id, outcome)
            return

        # ── (c) anything else (plain text, location, contacts): not our business ──
        # afc_whatsapp has already logged it and handled any opt-out.
    except Exception as exc:
        # Defence in depth: the caller also wraps us, but a marketplace failure must
        # never be able to turn a valid Meta webhook into a non-200.
        logger.error("vendor whatsapp: error handling an inbound message: %s", exc)
