"""
afc_shop/services/whatsapp_media.py
================================================================================
Download the bytes behind an INBOUND WhatsApp media item, straight from Meta.

WHY THIS EXISTS
    A marketplace vendor proves a shipment by replying to the AFC WhatsApp number
    with a photo of the packed parcel. Meta's webhook does not carry the image: it
    carries a media ID, and the bytes have to be fetched in a second call. This
    module is that fetch.

    It replaces the same two-step fetch that used to run through the Kapso proxy
    (afc_shop/services/kapso.py download_whatsapp_media, deleted 2026-08-03). Kapso
    is gone, so the call goes to graph.facebook.com directly with AFC's own Meta
    credentials.

WHERE IT SHOULD EVENTUALLY LIVE
    afc_whatsapp/client.py is "the ONLY module in AFC that talks to
    graph.facebook.com", and a media download belongs there next to the sends. It is
    here for now only because afc_whatsapp had no media API when the marketplace was
    cut over and that app is owned by a separate workstream. When a media helper is
    added there, this module should become a one-line delegation and then disappear.
    Nothing else about the marketplace flow depends on where it lives.

HOW IT CONNECTS
  - CALLED BY : afc_shop/vendor_whatsapp.py _handle_inbound_media, which turns the
                returned bytes into a FulfillmentEvidence row on the order.
  - READS     : settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_API_VERSION,
                the same env-driven values afc_whatsapp/client.py sends with (see the
                WhatsApp Cloud API block in afc/settings.py). Nothing secret is in
                this file, and the values are read at CALL time so a rotated token
                needs only a restart.

THE TWO-STEP META FLOW
    1. GET https://graph.facebook.com/<version>/<media_id>
       -> {"url": "<short-lived download url>", "mime_type": "image/jpeg", ...}
    2. GET <that url>
       -> the raw bytes.
    BOTH calls need the Bearer token: step 2's URL is on a Meta CDN host that still
    authenticates, which is the step people usually get wrong.

NEVER RAISES. Every failure comes back as {"ok": False, "error": ...} so the caller
can treat it as "no evidence stored" without a try/except of its own.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Network timeout (seconds) per call. Kept short: this runs inside the inbound webhook
# request, and Meta escalates its retries against any response that is not a prompt
# 200, so a hung download must never hold the response open.
REQUEST_TIMEOUT = 20


def download_media(media_id):
    """Fetch the bytes of one inbound WhatsApp media item.

    Args:
        media_id: the id Meta put in the inbound message
                  (value.messages[].image.id, .document.id, .video.id).

    Returns:
        {"ok": True,  "content": <bytes>, "mime_type": "<str>"}   on success
        {"ok": False, "error": "<reason>", "status_code": <int|None>}  on failure
    """
    token = getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or ""
    version = getattr(settings, "WHATSAPP_API_VERSION", "v21.0") or "v21.0"
    if not token or not media_id:
        # Misconfiguration or a malformed webhook, not a Meta failure. Say which.
        return {
            "ok": False,
            "error": "WHATSAPP_ACCESS_TOKEN is not configured." if not token else "No media id.",
            "status_code": None,
        }

    headers = {"Authorization": f"Bearer {token}"}

    # ── step 1: resolve the media id to a short-lived download URL + mime type ──
    try:
        lookup = requests.get(
            f"https://graph.facebook.com/{version}/{media_id}",
            headers=headers, timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("whatsapp media lookup network error: %s", exc)
        return {"ok": False, "error": f"Network error: {exc}", "status_code": None}

    if not lookup.ok:
        return {"ok": False, "error": f"Media lookup failed (HTTP {lookup.status_code}).",
                "status_code": lookup.status_code}

    try:
        meta = lookup.json()
    except ValueError:
        return {"ok": False, "error": "Media lookup returned a non-JSON body.",
                "status_code": lookup.status_code}

    download_url = meta.get("url")
    mime_type = meta.get("mime_type", "")
    if not download_url:
        return {"ok": False, "error": "Media lookup response carried no download url.",
                "status_code": lookup.status_code}

    # ── step 2: fetch the raw bytes (SAME Bearer header; the CDN host authenticates) ──
    try:
        binary = requests.get(download_url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("whatsapp media download network error: %s", exc)
        return {"ok": False, "error": f"Network error: {exc}", "status_code": None}

    if not binary.ok:
        return {"ok": False, "error": f"Media download failed (HTTP {binary.status_code}).",
                "status_code": binary.status_code}

    # Prefer the mime type from the lookup; fall back to the binary response's header.
    return {
        "ok": True,
        "content": binary.content,
        "mime_type": mime_type or binary.headers.get("Content-Type", ""),
    }
