"""
afc_shop/whatsapp_webhook.py
================================================================================
KAPSO / WhatsApp INBOUND WEBHOOK (marketplace fulfilment, two-way channel).

This is the RECEIVING half of the WhatsApp fulfilment flow. The SENDING half lives
in afc_shop/fulfilment.py notify_vendor (which posts the order summary + 3 action
BUTTONS to the vendor via afc_shop/services/kapso.send_whatsapp_buttons). When the
vendor TAPS a button, or REPLIES with a photo/document (a shipping receipt), Meta/
Kapso POSTs the event here, and this handler maps it back to the order and advances
the SAME fulfilment state machine the web page uses. The DB is the single source of
truth; there is NO fulfilment logic here that the page does not also have, only the
channel-specific parsing + routing.

CORE PRINCIPLE (spec: WEBSITE/tasks/marketplace-design.md): one state machine + one
API, two front doors. This webhook is the WhatsApp front door; it drives the shared
transition CORES in fulfilment.py (apply_acknowledge / apply_set_ship_date /
apply_mark_shipped), exactly what the per-order vendor page drives.

WHAT IT HANDLES
  - GET  -> the Meta verification handshake. Meta calls our URL once with
            hub.mode=subscribe + hub.verify_token + hub.challenge; we echo
            hub.challenge back (verifying the token if one is configured).
  - POST -> the inbound event envelope (Meta WhatsApp Cloud API shape, documented at
            the bottom of afc_shop/services/kapso.py). We walk
            entry[].changes[].value.messages[] and, per message:
              * a tapped reply button encoded "<action>:<order_id>" -> advance the
                matching order (ack -> acknowledged, shipped -> mark shipped). TWO shapes:
                a FREE-FORM interactive button is type "interactive" (read
                interactive.button_reply.id); a TEMPLATE quick-reply button is type
                "button" (read button.payload). The "shipdate" tap can't carry a date in a
                plain button, so we ask the vendor to send the date as text / use the page.
              * image/document/video for an order in ship_scheduled or shipped ->
                download the bytes via Kapso and store a FulfillmentEvidence row.
              * text -> ignored / logged (no state change).

SECURITY (best-effort, per the brief): we resolve the order's vendor and only act if
the SENDER's WhatsApp number matches that vendor's whatsapp_number. A mismatch is
logged and ignored so a stranger messaging our number cannot drive someone's order.

SECURITY (optional HMAC, 2026-06-12 hardening): when settings.KAPSO_WEBHOOK_SECRET is
set (env var KAPSO_WEBHOOK_SECRET, defined next to the other KAPSO_* settings in
afc/settings.py), every POST must carry an "X-Webhook-Signature" header equal to the
HMAC-SHA256 HEX digest of the RAW request body keyed with that secret. The compare is
constant-time (hmac.compare_digest) and a mismatch/missing header is rejected with
403 BEFORE any parsing. When the setting is empty/absent the check is skipped and
behaviour is unchanged (backward compatible; mirrors the permissive no-secret stance
of WHATSAPP_WEBHOOK_VERIFY_TOKEN on the GET handshake). See _verify_signature below.

ROBUSTNESS: the POST handler ALWAYS returns 200 fast and NEVER 500s (a 500 makes
Meta/Kapso retry-storm the webhook). Every parse/transition is wrapped; failures are
logged and skipped.

HOW IT CONNECTS
  - CALLS: afc_shop/fulfilment.py apply_acknowledge / apply_set_ship_date /
    apply_mark_shipped (the shared transition cores) + _order_vendor (the order's
    vendor). afc_shop/services/kapso.download_whatsapp_media (inbound media bytes).
  - MODELS: Order (the target), Vendor (sender match), FulfillmentEvidence (inbound
    media stored as proof of shipment).
  - ROUTE: afc_shop/urls.py -> path "whatsapp/webhook/" (GET verify + POST inbound).
  - CONFIG: settings.KAPSO_* (for the media download), an optional
    settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN (the handshake token; if unset we accept
    the handshake, matching the permissive stance of the Stripe webhook with no secret),
    and an optional settings.KAPSO_WEBHOOK_SECRET (the POST HMAC secret above).
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import FulfillmentEvidence, Order
from .fulfilment import (
    _order_vendor,
    apply_acknowledge,
    apply_mark_shipped,
    apply_set_ship_date,
)
from .services.kapso import _normalize_to_number, download_whatsapp_media

logger = logging.getLogger(__name__)


# ── button-id action map ───────────────────────────────────────────────────────
# The reply ids notify_vendor (fulfilment.py) encodes as "<action>:<order_id>". The
# KEY is the action prefix; the VALUE names which transition the tap drives. The two
# files MUST agree on these ids (a tap is meaningless if the prefixes drift).
#   ack      -> received        -> acknowledged
#   shipped  -> ship_scheduled  -> shipped
#   shipdate -> acknowledged    -> ship_scheduled  (needs a date; handled specially)
BUTTON_ACTIONS = {"ack", "shipdate", "shipped"}

# Inbound media types (Meta) we accept as shipment evidence + how each maps to our
# FulfillmentEvidence.kind. "document" (a scanned receipt/label) is stored as an image
# kind for display purposes; "audio" is ignored (not evidence).
MEDIA_KINDS = {"image": "image", "video": "video", "document": "image"}

# Fulfilment states in which an inbound photo/document is meaningfully "shipment
# evidence". Outside these we still STORE nothing extra (a buyer-stage photo is noise).
EVIDENCE_STATES = {"ship_scheduled", "shipped"}


# ─────────────────────────────────────────────────────────────────────────────
# GET — Meta verification handshake
# ─────────────────────────────────────────────────────────────────────────────
def _verify_handshake(request):
    """Echo hub.challenge back to verify the webhook URL with Meta.

    Meta calls GET ...?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=<n>
    once when the webhook is registered. If WHATSAPP_WEBHOOK_VERIFY_TOKEN is set we
    require it to match (else 403); if it is unset we accept the handshake (the
    permissive local stance, mirroring the Stripe webhook accepting when no secret is
    configured). On success we return the raw hub.challenge as plain text (Meta expects
    the bare challenge value, not JSON)."""
    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token")
    challenge = request.GET.get("hub.challenge", "")

    expected = getattr(settings, "WHATSAPP_WEBHOOK_VERIFY_TOKEN", None)
    if expected and token != expected:
        return Response({"message": "Verification token mismatch."}, status=403)

    if mode == "subscribe" or challenge:
        # Plain-text bare challenge (Meta does a string compare on the response body).
        return Response(int(challenge) if str(challenge).isdigit() else challenge,
                        status=200, content_type="text/plain")

    return Response({"message": "Missing handshake parameters."}, status=400)


# ─────────────────────────────────────────────────────────────────────────────
# POST: optional HMAC signature verification (2026-06-12 hardening)
# ─────────────────────────────────────────────────────────────────────────────
def _verify_signature(request):
    """Verify the inbound POST against settings.KAPSO_WEBHOOK_SECRET, if configured.

    Contract (documented in the file header + afc/settings.py): the sender computes
    HMAC-SHA256 over the RAW request body with the shared secret and sends the HEX
    digest in the "X-Webhook-Signature" header. We recompute it and compare with
    hmac.compare_digest (constant-time, so the comparison itself leaks no timing
    signal an attacker could use to forge a signature byte by byte).

    Returns True when the request may proceed:
      - secret NOT configured -> True (backward compatible: unsigned webhooks keep
        working exactly as before; local dev / Kapso setups without the secret).
      - secret configured + header matches -> True.
      - secret configured + header missing or wrong -> False (caller rejects 403).

    IMPORTANT ordering note: this reads request.body BEFORE anyone touches
    request.data. Accessing the raw body first makes Django cache it, so DRF's later
    JSON parse in whatsapp_webhook still works; the reverse order would raise
    RawPostDataException. whatsapp_webhook calls this at the very top of the POST
    branch to preserve that ordering."""
    secret = getattr(settings, "KAPSO_WEBHOOK_SECRET", None)
    if not secret:
        return True  # no secret configured: behaviour unchanged (accept unsigned)

    provided = (request.headers.get("X-Webhook-Signature") or "").strip()
    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    if provided and hmac.compare_digest(expected, provided):
        return True

    # Forged / unsigned call while a secret is configured: log (no body contents, no
    # secret material) and let the caller reject it with 403.
    logger.warning("whatsapp inbound: rejected POST with %s signature.",
                   "a mismatching" if provided else "no")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Internal: per-message routing
# ─────────────────────────────────────────────────────────────────────────────
def _sender_is_order_vendor(order, sender_wa_id):
    """Best-effort sender check: True if `sender_wa_id` matches the order's vendor's
    whatsapp_number. Both sides are normalised to digits-only E.164 first (the inbound
    `from` has no "+"; the stored number may be written with "+"/spaces). Returns the
    Vendor on a match, else None (so the caller can log + skip an impostor)."""
    vendor = _order_vendor(order)
    if not vendor or not vendor.whatsapp_number:
        return None
    if _normalize_to_number(vendor.whatsapp_number) == _normalize_to_number(sender_wa_id):
        return vendor
    return None


def _handle_button_tap(order, sender_wa_id, reply_id):
    """Route a button tap ("<action>:<order_id>") to the matching transition core.

    Resolves the action from the reply id prefix and advances the order via the SAME
    cores the vendor page uses. Returns a short result string for the log. Only acts if
    the sender is the order's vendor (checked by the caller). An out-of-order tap (e.g.
    "shipped" before a ship date) is rejected by the core's VALID_TRANSITIONS guard, so
    a wrong tap is a harmless no-op."""
    action = reply_id.split(":", 1)[0]

    if action == "ack":
        ok, _err = apply_acknowledge(order)
        return f"ack -> {'acknowledged' if ok else 'rejected'}"

    if action == "shipped":
        ok, _err = apply_mark_shipped(order)
        return f"shipped -> {'shipped' if ok else 'rejected'}"

    if action == "shipdate":
        # A plain reply button carries no date. We cannot set ship_scheduled from a bare
        # tap; the vendor sends the date as text or uses the page. Log + leave state as
        # is (the next inbound text / page action sets the date). No state change here.
        return "shipdate -> prompt (no date in a button tap)"

    return f"unknown action '{action}'"


def _handle_inbound_media(order, message, media_type):
    """Download an inbound photo/document/video and store it as FulfillmentEvidence.

    Only meaningful while the order is in EVIDENCE_STATES (ship_scheduled/shipped); a
    photo at any other stage is ignored as noise. Returns a short result string for the
    log. Best-effort: a failed download is logged + skipped, never raised."""
    if order.fulfilment_state not in EVIDENCE_STATES:
        return f"media ignored (state={order.fulfilment_state})"

    media_obj = message.get(media_type) or {}
    media_id = media_obj.get("id")
    if not media_id:
        return "media ignored (no media id)"

    result = download_whatsapp_media(media_id)
    if not result.get("ok"):
        logger.warning("inbound media download failed for order #%s: %s", order.id, result.get("error"))
        return "media download failed"

    # Pick a sensible file extension from the mime type for the stored file name.
    mime = result.get("mime_type", "") or media_obj.get("mime_type", "")
    ext = mime.split("/")[-1].split(";")[0] if "/" in mime else "bin"
    kind = MEDIA_KINDS.get(media_type, "image")

    FulfillmentEvidence.objects.create(
        order=order,
        media=ContentFile(result["content"], name=f"whatsapp_order{order.id}_{media_id}.{ext}"),
        kind=kind,
        uploaded_by=None,  # inbound from WhatsApp: no AFC/vendor User session to attribute
        note=f"Inbound WhatsApp media ({media_type}) from the vendor.",
    )
    return f"evidence stored ({kind})"


def _process_message(message, contacts):
    """Process ONE inbound message entry. Wrapped by the caller so a bad message never
    aborts the rest of the batch. Logs what it did; returns nothing."""
    sender = message.get("from", "")
    msg_type = message.get("type", "")

    # ── (a) button tap: a tapped reply button whose id/payload encodes "<action>:<order_id>".
    # TWO wire shapes, same meaning, so we pull the encoded id out of whichever arrived:
    #   - FREE-FORM interactive buttons (send_whatsapp_buttons) -> type "interactive",
    #     interactive.button_reply.id == "<action>:<order_id>".
    #   - TEMPLATE quick-reply buttons (send_whatsapp_template)  -> type "button",
    #     button.payload == "<action>:<order_id>" (button.text is the visible label).
    # Both drive the SAME state machine via _handle_button_tap below. ──
    if msg_type in ("interactive", "button"):
        if msg_type == "interactive":
            interactive = message.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            reply_id = reply.get("id", "")
        else:  # msg_type == "button" -> template quick-reply tap
            reply_id = (message.get("button") or {}).get("payload", "")
        if ":" not in reply_id:
            logger.info("whatsapp inbound: ignoring reply id '%s' (no order encoding)", reply_id)
            return
        action, _, order_id = reply_id.partition(":")
        if action not in BUTTON_ACTIONS or not order_id.isdigit():
            logger.info("whatsapp inbound: ignoring reply id '%s' (unknown action)", reply_id)
            return

        order = Order.objects.prefetch_related("items__variant__product__vendor").filter(id=int(order_id)).first()
        if not order:
            logger.info("whatsapp inbound: order #%s not found for reply '%s'", order_id, reply_id)
            return
        if not _sender_is_order_vendor(order, sender):
            logger.warning("whatsapp inbound: sender %s is NOT order #%s vendor; ignoring tap.", sender, order_id)
            return

        outcome = _handle_button_tap(order, sender, reply_id)
        logger.info("whatsapp inbound: order #%s tap '%s' -> %s", order_id, reply_id, outcome)
        return

    # ── (b) inbound media (image/document/video): store as shipment evidence ──
    if msg_type in MEDIA_KINDS:
        # Inbound media has no order id in the payload. We attribute it to the sender's
        # MOST RECENT order that is awaiting evidence (ship_scheduled/shipped), so a
        # vendor can just reply with a photo to the order they were last asked about.
        order = _latest_evidence_order_for_sender(sender)
        if not order:
            logger.info("whatsapp inbound: media from %s but no order awaiting evidence; ignoring.", sender)
            return
        outcome = _handle_inbound_media(order, message, msg_type)
        logger.info("whatsapp inbound: order #%s media -> %s", order.id, outcome)
        return

    # ── (c) plain text (or anything else): log + ignore (no state change) ──
    logger.info("whatsapp inbound: ignoring '%s' message from %s", msg_type, sender)


def _latest_evidence_order_for_sender(sender_wa_id):
    """Find the sender-vendor's most recent order that is awaiting shipment evidence.

    Inbound media carries no order id, so we attach it to the vendor's newest order in
    ship_scheduled/shipped. We resolve the sender to a Vendor by whatsapp_number, then
    pick their latest such order. Returns the Order or None (unknown sender / nothing to
    attach to)."""
    digits = _normalize_to_number(sender_wa_id)
    if not digits:
        return None

    # Candidate orders awaiting evidence, newest first; match the one whose vendor's
    # number equals the sender. (Phase A: an order has a single vendor.)
    candidates = (
        Order.objects.filter(fulfilment_state__in=EVIDENCE_STATES)
        .prefetch_related("items__variant__product__vendor")
        .order_by("-created_at")
    )
    for order in candidates:
        if _sender_is_order_vendor(order, sender_wa_id):
            return order
    return None


# ─────────────────────────────────────────────────────────────────────────────
# The webhook view (GET verify + POST inbound)
# ─────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "POST"])
def whatsapp_webhook(request):
    """GET/POST /shop/whatsapp/webhook/

    GET  -> Meta verification handshake (echo hub.challenge).
    POST -> parse the Meta inbound envelope and drive the fulfilment state machine.

    Auth: none (a public webhook; Meta/Kapso is the caller). Inbound actions are gated
    by the best-effort sender-number == order-vendor check, not a session. The GET is
    gated by the optional WHATSAPP_WEBHOOK_VERIFY_TOKEN; the POST is gated by the
    optional KAPSO_WEBHOOK_SECRET HMAC check (_verify_signature above): a bad or
    missing signature while the secret is configured is rejected with 403.

    ALWAYS returns 200 on a SIGNATURE-VALID POST (never 500) so Meta/Kapso does not
    retry-storm us; every message is processed defensively and a failure is logged +
    skipped. (The 403 on a forged signature is fine: that caller is not Kapso, and a
    retry of a forged request should keep failing.)

    Consumed by: Kapso/Meta (configured webhook URL). Pairs with afc_shop/fulfilment.py
    notify_vendor (the outbound buttons whose taps arrive here)."""
    if request.method == "GET":
        return _verify_handshake(request)

    # ── POST: verify the HMAC signature FIRST (reads the raw body before request.data
    # is touched; see the ordering note on _verify_signature), then parse, never 500 ──
    if not _verify_signature(request):
        return Response({"message": "Invalid webhook signature."}, status=403)

    # ── POST: parse the envelope, never 500 ──
    try:
        # DRF has already parsed JSON bodies into request.data; fall back to the raw body
        # for any non-JSON content type Kapso might forward.
        payload = request.data if isinstance(request.data, dict) and request.data else json.loads(request.body.decode() or "{}")
    except Exception:
        logger.warning("whatsapp inbound: unparseable body; acking 200 to stop retries.")
        return Response({"received": True}, status=200)

    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                contacts = value.get("contacts", []) or []
                for message in value.get("messages", []) or []:
                    try:
                        _process_message(message, contacts)
                    except Exception as e:
                        # One bad message must not abort the batch or 500 the webhook.
                        logger.error("whatsapp inbound: error processing a message: %s", e)
    except Exception as e:
        # Malformed envelope: log + still 200 so Meta/Kapso stops retrying.
        logger.error("whatsapp inbound: error walking envelope: %s", e)

    return Response({"received": True}, status=200)
