"""
Kapso WhatsApp send client (AFC marketplace).
=============================================

Purpose
-------
Self-contained client for sending WhatsApp messages to marketplace product
owners / vendors through Kapso. Kapso is a thin proxy in front of the Meta
WhatsApp Cloud API: we POST the standard Meta message payload to a Kapso URL,
and Kapso forwards it to Meta using the WhatsApp number we have connected.

How this connects to the rest of the system
--------------------------------------------
- CONSUMED BY: the marketplace fulfilment notifier, `afc_shop/fulfilment.py`
  `notify_vendor(...)` (built by another agent). When a buyer's order is paid,
  fulfilment calls `send_whatsapp_text(...)` to tell the vendor a new order
  came in, and later `send_whatsapp_buttons(...)` to give the vendor tappable
  actions ("Order received", "Set ship date", "Mark shipped").
- READS FROM: Django settings (`settings.KAPSO_API_KEY`,
  `settings.KAPSO_PHONE_NUMBER_ID`), which are loaded from the gitignored
  backend/.env (see settings.py, near the STRIPE_* block). Nothing secret lives
  in this file.
- DOES NOT TOUCH: afc_shop models/views/urls/stripe_checkout/fulfilment. This
  module only knows how to put a message on the wire; the caller decides who to
  message and what to say.

The INBOUND side (a vendor tapping a button, or replying with media such as a
shipping receipt) is NOT handled here. Kapso/Meta delivers those as a webhook
POST to our server; a separate webhook view will parse it. The exact inbound
payload shape we discovered is documented at the bottom of this file so the next
agent can build that handler.

API discovered (Kapso WhatsApp Cloud API proxy)
-----------------------------------------------
Endpoint (send a message):
    POST https://api.kapso.ai/meta/whatsapp/v23.0/<PHONE_NUMBER_ID>/messages
Auth header:
    X-API-Key: <KAPSO_API_KEY>
Body: the standard Meta WhatsApp Cloud API JSON.
    (a) Plain text:
        {
          "messaging_product": "whatsapp",
          "recipient_type": "individual",
          "to": "<E.164 digits, no +>",
          "type": "text",
          "text": {"body": "<message>"}
        }
    (b) Interactive reply buttons (max 3, button label max 20 chars):
        {
          "messaging_product": "whatsapp",
          "recipient_type": "individual",
          "to": "<E.164 digits, no +>",
          "type": "interactive",
          "interactive": {
            "type": "button",
            "body": {"text": "<message>"},
            "action": {"buttons": [
              {"type": "reply", "reply": {"id": "<id>", "title": "<label>"}}
            ]}
          }
        }
Success response: Meta's standard message-accepted body, e.g.
    {"messaging_product": "whatsapp",
     "contacts": [{"input": "<to>", "wa_id": "<wa_id>"}],
     "messages": [{"id": "wamid.XXXX"}]}
Note on the 24-hour window: WhatsApp only allows free-form (text / interactive)
messages within 24h of the recipient's last inbound message. Outside that
window Meta returns an error and an approved message template must be used
instead. This client surfaces that error in the returned dict (it does not
silently fail). Template sending is out of scope for this module.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# ── Kapso proxy endpoint constants ─────────────────────────────────────────────
# Base URL of the Kapso proxy that fronts the Meta WhatsApp Cloud API, plus the
# Graph API version we target. The full send URL is built per-call from these and
# the phone-number ID. (Discovered from the Kapso CLI's bundled
# @kapso/whatsapp-cloud-api client: baseUrl + "/" + graphVersion + "/<id>/messages".)
KAPSO_BASE_URL = "https://api.kapso.ai/meta/whatsapp"
KAPSO_GRAPH_VERSION = "v23.0"

# Network timeout (seconds) for the send call. Kept short so a slow Kapso/Meta
# response never blocks the fulfilment flow for long; the caller treats a failure
# as "vendor not notified yet" and can retry.
REQUEST_TIMEOUT = 20


def _normalize_to_number(to_number: str) -> str:
    """
    Normalize a recipient phone number to the digits-only E.164 form Meta expects.

    Meta's WhatsApp Cloud API wants the number as country-code + national number
    with NO leading "+" and no spaces/dashes (e.g. "+1 207-886-5837" -> "12078865837").
    We strip every non-digit character. A leading "00" international prefix (used in
    some regions instead of "+") is converted to nothing so "00234..." -> "234...".
    """
    if not to_number:
        return ""

    # Keep digits only. This drops "+", spaces, dashes, and parentheses.
    digits = "".join(ch for ch in str(to_number) if ch.isdigit())

    # Some numbers are written with a "00" international call prefix instead of "+".
    # Meta wants neither, so collapse a leading "00" to the bare country code.
    if digits.startswith("00"):
        digits = digits[2:]

    return digits


def _send_message(payload: dict) -> dict:
    """
    Low-level POST to the Kapso send endpoint. Shared by the text and button helpers.

    Builds the URL from settings.KAPSO_PHONE_NUMBER_ID, attaches the X-API-Key
    auth header from settings.KAPSO_API_KEY, posts `payload` as JSON, and returns
    a normalized result dict. NEVER raises: network errors, bad config, and Meta-
    level errors are all caught and returned as {"ok": False, "error": ...} so the
    fulfilment caller can decide what to do (log, retry, fall back) without a try/except.

    Returns:
        {"ok": True,  "message_id": "wamid....", "data": <raw Meta JSON>}  on success
        {"ok": False, "error": "<reason>", "status_code": <int|None>, "data": <raw>}  on failure
    """
    # ── config guard: fail clearly if the Kapso env vars are missing ──
    api_key = getattr(settings, "KAPSO_API_KEY", None)
    phone_number_id = getattr(settings, "KAPSO_PHONE_NUMBER_ID", None)
    if not api_key or not phone_number_id:
        # Misconfiguration (env not loaded). Surface it instead of sending a broken request.
        logger.error("Kapso send skipped: KAPSO_API_KEY / KAPSO_PHONE_NUMBER_ID not configured.")
        return {
            "ok": False,
            "error": "Kapso not configured (KAPSO_API_KEY / KAPSO_PHONE_NUMBER_ID missing).",
            "status_code": None,
            "data": None,
        }

    url = f"{KAPSO_BASE_URL}/{KAPSO_GRAPH_VERSION}/{phone_number_id}/messages"
    headers = {
        "X-API-Key": api_key,          # Kapso proxy auth (NOT a Meta bearer token)
        "Content-Type": "application/json",
    }

    # ── send: catch every network-level failure so this never raises into fulfilment ──
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        # DNS failure, connection refused, timeout, etc.
        logger.error("Kapso send network error: %s", exc)
        return {"ok": False, "error": f"Network error: {exc}", "status_code": None, "data": None}

    # ── parse: Kapso/Meta normally return JSON; guard against a non-JSON body ──
    try:
        data = response.json()
    except ValueError:
        data = {"raw_response": response.text}

    # ── interpret the result ──
    # Meta's success body always carries a "messages" array with the new message id.
    # Any "error" key (Meta style {"error": {...}} or Kapso style {"error": "..."}) means failure.
    if response.ok and not data.get("error") and data.get("messages"):
        message_id = (data.get("messages") or [{}])[0].get("id")
        return {"ok": True, "message_id": message_id, "data": data}

    # Failure: pull out the most useful human-readable error string we can find.
    error = data.get("error")
    if isinstance(error, dict):
        # Meta error shape: {"error": {"message": "...", "code": 131058, ...}}
        error = error.get("message") or str(error)
    if not error:
        error = f"Unexpected response (HTTP {response.status_code})."

    logger.warning("Kapso send failed (HTTP %s): %s", response.status_code, error)
    return {"ok": False, "error": error, "status_code": response.status_code, "data": data}


def send_whatsapp_text(to_number: str, body: str) -> dict:
    """
    Send a plain WhatsApp text message to a vendor.

    Used by fulfilment's notify_vendor to send the initial "you have a new paid
    order" message (and any later free-form status text).

    Args:
        to_number: recipient phone number in any human format; normalized to E.164 digits.
        body: the message text.

    Returns the dict from `_send_message` ({"ok": True, "message_id", "data"} or
    {"ok": False, "error", ...}). Never raises.
    """
    to = _normalize_to_number(to_number)
    if not to:
        return {"ok": False, "error": "Missing or invalid recipient number.", "status_code": None, "data": None}
    if not body:
        return {"ok": False, "error": "Message body is empty.", "status_code": None, "data": None}

    # Standard Meta Cloud API text payload (see module docstring, case (a)).
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    return _send_message(payload)


def send_whatsapp_buttons(to_number: str, body: str, buttons: list) -> dict:
    """
    Send an interactive WhatsApp message with up to 3 tappable reply buttons.

    Used by the fulfilment flow to give the vendor one-tap actions on an order,
    e.g. buttons for "Order received", "Set ship date", and "Mark shipped". When
    the vendor taps a button, Meta/Kapso POSTs a webhook to our server carrying
    the button's id and title (see "INBOUND WEBHOOK SHAPE" at the bottom of this
    file) so the next agent's webhook handler can advance the order state.

    Args:
        to_number: recipient phone number in any human format; normalized to E.164 digits.
        body: the message text shown above the buttons.
        buttons: list describing the reply buttons. Each item may be either:
                   - a dict {"id": "<reply_id>", "title": "<label>"}, or
                   - a (id, title) / [id, title] pair, or
                   - a plain string (used as both id and title).
                 WhatsApp allows AT MOST 3 buttons; titles are capped at 20 chars
                 (Meta rejects longer ones). We enforce both here.

    Returns the dict from `_send_message`. Never raises.
    """
    to = _normalize_to_number(to_number)
    if not to:
        return {"ok": False, "error": "Missing or invalid recipient number.", "status_code": None, "data": None}
    if not body:
        return {"ok": False, "error": "Message body is empty.", "status_code": None, "data": None}
    if not buttons:
        return {"ok": False, "error": "No buttons provided.", "status_code": None, "data": None}

    # ── normalize each button into Meta's reply-button shape ──
    # WhatsApp interactive "button" messages allow a maximum of 3 buttons; trim to 3.
    reply_buttons = []
    for btn in buttons[:3]:
        if isinstance(btn, dict):
            btn_id = str(btn.get("id") or btn.get("title") or "")
            title = str(btn.get("title") or btn.get("id") or "")
        elif isinstance(btn, (list, tuple)) and len(btn) >= 2:
            btn_id, title = str(btn[0]), str(btn[1])
        else:
            # Plain string: use it as both the reply id and the visible label.
            btn_id = title = str(btn)

        if not btn_id or not title:
            continue  # skip malformed entries rather than send an invalid button

        # Meta caps button labels at 20 characters; truncate defensively so the
        # whole send does not get rejected over one long label.
        reply_buttons.append({
            "type": "reply",
            "reply": {"id": btn_id, "title": title[:20]},
        })

    if not reply_buttons:
        return {"ok": False, "error": "No valid buttons after normalization.", "status_code": None, "data": None}

    # Standard Meta Cloud API interactive payload (see module docstring, case (b)).
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": reply_buttons},
        },
    }
    return _send_message(payload)


# ════════════════════════════════════════════════════════════════════════════════
# INBOUND WEBHOOK SHAPE (for the NEXT agent — webhook handler is NOT built here)
# ════════════════════════════════════════════════════════════════════════════════
# When a vendor replies, taps a button, or sends media (e.g. a shipping receipt),
# Kapso/Meta POSTs a webhook to our server. It is the standard Meta WhatsApp Cloud
# API webhook envelope (snake_case from the wire). The events we care about live at
#   entry[].changes[].value
# with value.metadata.phone_number_id identifying OUR business number, and:
#   - value.contacts[]  -> [{"profile": {"name": "<vendor name>"}, "wa_id": "<sender>"}]
#   - value.messages[]  -> inbound messages from the vendor
#   - value.statuses[]  -> delivery/read receipts for messages WE sent
#
# (a) A button TAP (from send_whatsapp_buttons) arrives as one messages[] entry:
#     {
#       "from": "<vendor wa_id>",
#       "id": "wamid....",
#       "timestamp": "...",
#       "type": "interactive",
#       "interactive": {
#         "type": "button_reply",
#         "button_reply": {"id": "<the reply id we set>", "title": "<the label>"}
#       }
#     }
#   -> the handler routes on interactive.button_reply.id (e.g. "order_received",
#      "set_ship_date", "mark_shipped") to advance the order. (A list-picker reply
#      instead carries interactive.list_reply.{id,title}.)
#
# (b) MEDIA (vendor sends a photo / document such as a shipping receipt):
#     {
#       "from": "<vendor wa_id>",
#       "id": "wamid....",
#       "type": "image",            # or "document", "video", "audio"
#       "image": {                  # key matches "type"
#         "id": "<media id>",       # download via the media endpoint using this id
#         "mime_type": "image/jpeg",
#         "sha256": "...",
#         "caption": "<optional caption>"
#       }
#     }
#   -> the handler reads the media id, fetches the bytes from the media endpoint,
#      and attaches it to the order as proof of shipment.
#
# (c) A plain TEXT reply:
#     {"from": "<vendor wa_id>", "type": "text", "text": {"body": "<message>"}}
#
# Webhook verification: Meta first sends a GET with hub.mode / hub.challenge /
# hub.verify_token to verify the endpoint; the handler echoes hub.challenge back.
# Inbound processing is enabled on this number; the receiving URL is configured on
# the Kapso side (KAPSO_PHONE_NUMBER_ID / KAPSO_WHATSAPP_CONFIG_ID).
# ════════════════════════════════════════════════════════════════════════════════
