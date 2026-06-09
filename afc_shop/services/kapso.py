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
    (c) Approved message TEMPLATE (for COLD contact, outside the 24h window):
        {
          "messaging_product": "whatsapp",
          "recipient_type": "individual",
          "to": "<E.164 digits, no +>",
          "type": "template",
          "template": {
            "name": "<approved template name>",
            "language": {"code": "<exact approved lang, e.g. en_US>"},
            "components": [
              {"type": "body", "parameters": [{"type": "text", "text": "<{{1}}>"}, ...]},
              {"type": "button", "sub_type": "quick_reply", "index": 0,
               "parameters": [{"type": "payload", "payload": "<echoed back on tap>"}]}
            ]
          }
        }
Note on the 24-hour window: WhatsApp only allows free-form (text / interactive)
messages within 24h of the recipient's last inbound message. Outside that
window Meta returns an error and an approved message TEMPLATE must be used
instead. This client surfaces that error in the returned dict (it does not
silently fail). send_whatsapp_template below sends that template; notify_vendor
sends the template first and falls back to free-form buttons if it fails.
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


def send_whatsapp_template(
    to_number: str,
    template_name: str,
    body_params: list,
    button_payloads: list = None,
    language_code: str = "en_US",
) -> dict:
    """
    Send an APPROVED WhatsApp message TEMPLATE to a vendor (Meta Cloud API type "template").

    This is the COLD-CONTACT path. WhatsApp only allows free-form text/interactive
    messages within 24h of the recipient's last inbound message; a business-initiated
    ("you have a new order") message OUTSIDE that window MUST be an approved template.
    afc_shop/fulfilment.py notify_vendor calls THIS first for a new paid order, and
    falls back to send_whatsapp_buttons only if it returns ok=False (most commonly the
    template is not approved yet -> Meta error 132001).

    The template `template_name` must be registered + approved in Meta with:
      - a BODY holding N positional variables {{1}}..{{N}} (filled from body_params, in
        order), and
      - up to 3 QUICK-REPLY buttons (their dynamic payloads filled from button_payloads,
        in order). When a vendor taps one, Meta/Kapso POSTs an INBOUND message of
        type "button" carrying button.payload == the payload we set here. The webhook
        (afc_shop/whatsapp_webhook.py) maps that "<action>:<order_id>" payload back to the
        order. NOTE: a TEMPLATE button tap arrives as type "button", NOT the
        "interactive.button_reply" shape that send_whatsapp_buttons' taps use, so the
        webhook handles both shapes.

    Args:
        to_number:       recipient phone in any format; normalized to E.164 digits.
        template_name:   the approved template name (e.g. "vendor_new_order").
        body_params:     ordered list of strings for the body's {{1}}..{{N}} variables.
        button_payloads: ordered list of custom payload strings, one per quick-reply
                         button (max 3, capped here). Each becomes button.payload on the
                         inbound tap. Pass None/[] for a template with no quick-reply buttons.
        language_code:   the language the template was APPROVED under. MUST match exactly
                         ("en_US" != "en"); a mismatch returns Meta error 132001. The caller
                         passes settings.KAPSO_TEMPLATE_LANG.

    Returns the dict from `_send_message` ({"ok": True, "message_id", "data"} or
    {"ok": False, "error", ...}). Never raises.
    """
    to = _normalize_to_number(to_number)
    if not to:
        return {"ok": False, "error": "Missing or invalid recipient number.", "status_code": None, "data": None}
    if not template_name:
        return {"ok": False, "error": "Missing template name.", "status_code": None, "data": None}

    # ── BODY component: one {"type":"text","text":...} per positional {{n}} variable, in
    # order. Meta fills {{1}} from the first entry, {{2}} from the second, and so on. ──
    components = [{
        "type": "body",
        "parameters": [{"type": "text", "text": str(p)} for p in (body_params or [])],
    }]

    # ── QUICK-REPLY button components: ONE component per button. Each carries its own
    # 0-based INTEGER index (the button's position in the approved template) and the
    # dynamic payload that is echoed back to our webhook on tap. Meta allows at most 3. ──
    for idx, payload_value in enumerate((button_payloads or [])[:3]):
        components.append({
            "type": "button",
            "sub_type": "quick_reply",
            "index": idx,  # integer, canonical Meta form (0,1,2) = button position
            "parameters": [{"type": "payload", "payload": str(payload_value)}],
        })

    # Standard Meta Cloud API template payload (see module docstring, case (c)).
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": components,
        },
    }
    return _send_message(payload)


def download_whatsapp_media(media_id: str) -> dict:
    """
    Download the bytes of an INBOUND WhatsApp media item (a photo / document a vendor
    sent, e.g. a shipping receipt) by its media id.

    Used by the INBOUND webhook (afc_shop/whatsapp_webhook.py) to turn a vendor's
    inbound photo/video into a FulfillmentEvidence file. Meta media download is a
    TWO-STEP flow, both proxied through Kapso the same way the send endpoint is:
      1. GET  <base>/<graph_version>/<media_id>   -> JSON { "url": "<download url>",
                                                            "mime_type": ..., ... }
      2. GET  <that url>                           -> the raw bytes.
    Both calls carry the Kapso X-API-Key auth header (the Kapso proxy, not a Meta
    bearer token), mirroring _send_message.

    Args:
        media_id: the inbound message's media id (value.messages[].image.id, etc.).

    Returns (NEVER raises; the webhook treats a failure as "no evidence stored"):
        {"ok": True,  "content": <bytes>, "mime_type": "<str>", "url": "<str>"}  on success
        {"ok": False, "error": "<reason>", "status_code": <int|None>}            on failure
    """
    api_key = getattr(settings, "KAPSO_API_KEY", None)
    if not api_key or not media_id:
        return {"ok": False, "error": "Kapso not configured or media_id missing.", "status_code": None}

    headers = {"X-API-Key": api_key}

    # ── step 1: resolve the media id to a download URL + mime type ──
    lookup_url = f"{KAPSO_BASE_URL}/{KAPSO_GRAPH_VERSION}/{media_id}"
    try:
        meta_resp = requests.get(lookup_url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Kapso media lookup network error: %s", exc)
        return {"ok": False, "error": f"Network error: {exc}", "status_code": None}

    if not meta_resp.ok:
        return {"ok": False, "error": f"Media lookup failed (HTTP {meta_resp.status_code}).",
                "status_code": meta_resp.status_code}

    try:
        meta = meta_resp.json()
    except ValueError:
        return {"ok": False, "error": "Media lookup returned a non-JSON body.", "status_code": meta_resp.status_code}

    download_url = meta.get("url")
    mime_type = meta.get("mime_type", "")
    if not download_url:
        return {"ok": False, "error": "Media lookup response had no download url.", "status_code": meta_resp.status_code}

    # ── step 2: fetch the raw bytes from the resolved URL (same auth header) ──
    try:
        bin_resp = requests.get(download_url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Kapso media download network error: %s", exc)
        return {"ok": False, "error": f"Network error: {exc}", "status_code": None}

    if not bin_resp.ok:
        return {"ok": False, "error": f"Media download failed (HTTP {bin_resp.status_code}).",
                "status_code": bin_resp.status_code}

    # Prefer the mime type from the lookup; fall back to the binary response's header.
    if not mime_type:
        mime_type = bin_resp.headers.get("Content-Type", "")

    return {"ok": True, "content": bin_resp.content, "mime_type": mime_type, "url": download_url}


# ════════════════════════════════════════════════════════════════════════════════
# INBOUND WEBHOOK SHAPE (consumed by afc_shop/whatsapp_webhook.py — handler built there)
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
# (a2) A TEMPLATE quick-reply button TAP (from send_whatsapp_template) arrives DIFFERENTLY
#      - as type "button" with a TOP-LEVEL button object (NOT interactive.button_reply):
#     {
#       "from": "<vendor wa_id>",
#       "type": "button",
#       "button": {"payload": "<the payload we set, e.g. ack:123>", "text": "<button label>"}
#     }
#   -> the handler reads button.payload (same "<action>:<order_id>" encoding) to advance
#      the order. The webhook handles BOTH (a) and (a2).
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
