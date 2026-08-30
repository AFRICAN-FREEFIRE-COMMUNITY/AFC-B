# backend/afc_whatsapp/client.py
# ──────────────────────────────────────────────────────────────────────────────
# The Meta WhatsApp Cloud API client. The ONLY module in AFC that talks to
# graph.facebook.com.
#
# WHAT IT IS
#   A thin HTTP layer, nothing more. It knows how to build Meta's message payloads
#   and how to read Meta's reply. It holds no state, writes no rows, and decides
#   nothing about WHO to message or WHETHER to message them: that is tasks.py.
#
# HOW IT CONNECTS
#   CALLED BY : afc_whatsapp/tasks.py send_whatsapp_message (the only caller in
#               normal operation) and the sync_whatsapp_templates management
#               command (list_templates).
#   READS     : Django settings, which read os.getenv (afc/settings.py, the
#               WhatsApp Cloud API block). Nothing secret is ever in this file.
#   RETURNS   : the wamid. Meta's message id is the primary key of the whole
#               system: the status webhook (afc_whatsapp/webhooks.py) reports
#               delivered/read/failed against it, so a send that discards the wamid
#               can never be tracked. Every function here returns it.
#
# ERRORS NEVER RAISE PAST THE CALLER
#   Network failure, bad config, and Meta-level rejection all come back as
#   {"ok": False, ...} carrying Meta's numeric CODE and TITLE. Those two fields are
#   what tells an organizer why a player was not reached ("131047: re-engagement
#   message" means the 24 hour window closed; "132001" means the template is not
#   approved), so they are preserved verbatim rather than flattened to a string.
#
# RESULT SHAPE (identical for every function below)
#   {
#     "ok":            bool,
#     "wamid":         "wamid.HBgM..." | None,   Meta's message id
#     "raw":           <the raw JSON Meta returned> | None,
#     "status_code":   <HTTP status> | None,     None means the request never landed
#     "error_code":    <Meta numeric code, e.g. 131047> | None,
#     "error_title":   "<short human title>" | None,
#     "error_detail":  "<longer explanation>" | None,
#     "retryable":     bool,                     see _is_retryable
#   }
# ──────────────────────────────────────────────────────────────────────────────
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Network timeout (seconds) for a single Graph call. Kept short: a send is best
# effort and a hung Meta must never hold a Celery worker (or a web request, in the
# WHATSAPP_SYNC local-dev path) for long. The task retries on timeout.
REQUEST_TIMEOUT = 20


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
def _config():
    """Read the WhatsApp settings AT CALL TIME (never at import).

    Call-time reads are what make @override_settings work in the tests and let the
    owner rotate the access token with a restart instead of a redeploy."""
    return {
        "phone_number_id": getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "") or "",
        "access_token": getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "",
        "waba_id": getattr(settings, "WHATSAPP_BUSINESS_ACCOUNT_ID", "") or "",
        "api_version": getattr(settings, "WHATSAPP_API_VERSION", "v21.0") or "v21.0",
    }


def is_configured():
    """True when a send can actually be attempted. Callers use this to skip quietly
    (local dev, CI) instead of logging a failure for every message."""
    cfg = _config()
    return bool(cfg["phone_number_id"] and cfg["access_token"])


def _messages_url(cfg):
    """https://graph.facebook.com/<version>/<phone number id>/messages"""
    return (
        f"https://graph.facebook.com/{cfg['api_version']}"
        f"/{cfg['phone_number_id']}/messages"
    )


def _headers(cfg):
    """Bearer auth. The token is a Meta system-user token from the environment."""
    return {
        "Authorization": f"Bearer {cfg['access_token']}",
        "Content-Type": "application/json",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Result helpers
# ──────────────────────────────────────────────────────────────────────────────
def _failure(error_title, *, status_code=None, error_code=None, error_detail=None,
             raw=None, retryable=False):
    """Build the failure result. One constructor so every path returns the same keys."""
    return {
        "ok": False,
        "wamid": None,
        "raw": raw,
        "status_code": status_code,
        "error_code": error_code,
        "error_title": error_title,
        "error_detail": error_detail,
        "retryable": retryable,
    }


def _is_retryable(status_code):
    """Which HTTP outcomes are worth trying again (best-practice rule 13).

    Transient: the request never landed (status_code None: DNS, connection reset,
    timeout), Meta rate limiting (429), or Meta being unwell (5xx).
    NOT transient: any other 4xx. A malformed template, an unapproved template, or
    an invalid recipient will fail identically forever, so retrying only delays the
    failure being visible."""
    if status_code is None:
        return True
    return status_code == 429 or status_code >= 500


def _parse_error(data, status_code):
    """Pull Meta's error code/title/detail out of a Graph error body.

    Meta's shape:
        {"error": {"message": "...", "type": "OAuthException", "code": 131047,
                   "error_subcode": 2494010, "error_data": {"details": "..."},
                   "error_user_title": "...", "error_user_msg": "...",
                   "fbtrace_id": "..."}}
    `error_user_title` is the phrase Meta writes for humans; `message` is the
    developer-facing one. We prefer the human title and keep the developer text as
    the detail, so the message log reads well without losing anything."""
    error = (data or {}).get("error")
    if not isinstance(error, dict):
        return None, f"Unexpected response (HTTP {status_code}).", None

    code = error.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None

    title = error.get("error_user_title") or error.get("message") or "WhatsApp send failed."
    detail = (
        error.get("error_user_msg")
        or (error.get("error_data") or {}).get("details")
        or error.get("message")
    )
    return code, title, detail


def _post(payload):
    """POST one message payload to the Cloud API and normalise the outcome.

    Shared by send_template and send_text so the auth, timeout, error parsing, and
    result shape exist exactly once. NEVER raises."""
    cfg = _config()
    if not (cfg["phone_number_id"] and cfg["access_token"]):
        # Misconfiguration, not a Meta failure. Not retryable: a retry cannot
        # conjure an env var.
        logger.error(
            "whatsapp send skipped: WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN "
            "not configured."
        )
        return _failure("WhatsApp is not configured on this server.",
                        error_detail="WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN missing.")

    try:
        response = requests.post(
            _messages_url(cfg), json=payload, headers=_headers(cfg), timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        # DNS failure, connection refused, read timeout. The message may or may not
        # have been accepted, but with no wamid we have to treat it as unsent.
        logger.warning("whatsapp send network error: %s", exc)
        return _failure("Could not reach WhatsApp.", error_detail=str(exc), retryable=True)

    try:
        data = response.json()
    except ValueError:
        data = {"raw_response": response.text[:500]}

    # Success: Meta always returns a messages[] array carrying the new message id.
    if response.ok and not data.get("error") and data.get("messages"):
        wamid = (data.get("messages") or [{}])[0].get("id")
        return {
            "ok": True,
            "wamid": wamid,
            "raw": data,
            "status_code": response.status_code,
            "error_code": None,
            "error_title": None,
            "error_detail": None,
            "retryable": False,
        }

    code, title, detail = _parse_error(data, response.status_code)
    # Log the DETAIL, not just the title (owner-reported 2026-08-06). Meta's titles are useless on
    # their own - "(#100) Invalid parameter" is true of a dozen different mistakes - while
    # error_data.details names the actual one ("template param count mismatch", "invalid language",
    # and so on). It was already being parsed and then dropped on the floor here, so a real failure
    # cost a round trip to the database to find out what it had been.
    logger.warning(
        "whatsapp send failed (HTTP %s, meta code %s): %s | detail: %s",
        response.status_code, code, title, detail or "(none given)",
    )
    return _failure(
        title,
        status_code=response.status_code,
        error_code=code,
        error_detail=detail,
        raw=data,
        retryable=_is_retryable(response.status_code),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public sends
# ──────────────────────────────────────────────────────────────────────────────
def _wa_id(number):
    """The recipient as Meta's Cloud API wants it: digits only, no leading plus.

    to_e164() returns a display-style "+2348132533372", which is the right thing for showing a
    number to a person and the wrong thing to put in the `to` field. Meta answers a plus-prefixed
    recipient with a bare "(#100) Invalid parameter" and NO detail, which is indistinguishable
    from a wrong template name or a bad body - it cost several rounds of debugging on 2026-08-06
    to separate the two.

    Applied at the payload boundary rather than inside to_e164, because to_e164 is also used for
    display and storage where the plus belongs.
    """
    return str(number or "").strip().lstrip("+").replace(" ", "")


def send_template(to, template_name, language, body_params=None, button_payloads=None,
                  url_button_suffix=None, otp_code=None):
    """Send an APPROVED message TEMPLATE. The business-initiated path.

    WhatsApp only permits free-form messages inside 24 hours of the recipient's last
    inbound message. Anything AFC starts (a new order for a vendor, room details for
    a player) is business-initiated and MUST be a template, so this is the function
    almost every AFC send uses.

    Args:
        to:              recipient in E.164 ("+2348051234567"). Normalise with
                         afc_whatsapp.phone.to_e164 BEFORE calling; this function
                         only strips the "+" for the wire.
        template_name:   the template's name as approved in WhatsApp Manager.
        language:        the language code it was approved UNDER. Meta treats "en"
                         and "en_US" as different templates and rejects a mismatch
                         with error 132001, so this must match exactly.
        body_params:     ordered values for the body's {{1}}..{{N}} variables.
        button_payloads: ordered payload strings, one per quick-reply button (max 3).
                         Each is echoed back to our webhook when the recipient taps,
                         which is how a tap is mapped to the thing it acts on.
        otp_code:        the one-time code, for an AUTHENTICATION template ONLY. Meta owns
                         the copy of those templates and REQUIRES a button component carrying
                         the code on top of the body parameter; a send without it is refused.
                         Passing this also fills body_params when they were not given, because
                         Meta requires the same code in both places and two arguments that
                         must match is two chances to get it wrong.
        url_button_suffix: the value for a DYNAMIC URL button. A template approved with
                         a dynamic "Visit website" button stores a fixed base URL ending
                         in {{1}}, and this is what gets appended at send time, e.g. an
                         event slug onto ".../tournaments/". Meta allows the variable
                         only at the END of the URL, so this is a suffix and never a
                         whole address: the base is frozen at approval, which is what
                         stops an approved template being repointed at another domain.

    Returns the standard result dict (see the module header). Never raises.
    """
    wa_id = "".join(ch for ch in str(to or "") if ch.isdigit())
    if not wa_id:
        return _failure("Missing or invalid recipient number.")
    if not template_name:
        return _failure("Missing template name.")

    # An authentication template carries the code TWICE, in the body and on the button, and
    # Meta rejects the send if they disagree. Deriving the body from otp_code when the caller
    # gave none removes the chance of that.
    if otp_code is not None and not body_params:
        body_params = [otp_code]

    # BODY component: one {"type": "text"} entry per positional variable, in order.
    components = [{
        "type": "body",
        "parameters": [{"type": "text", "text": str(p)} for p in (body_params or [])],
    }]

    # OTP button, for AUTHENTICATION templates. Added 2026-08-30 after Meta refused AFC's
    # account-recovery template as INCORRECT_CATEGORY: a one-time code is authentication
    # content, Meta will not accept it as utility, and an authentication template cannot be
    # sent without this component. The parameter type really is "coupon_code" for a COPY_CODE
    # button; that is Meta's own naming and not a mistake here.
    if otp_code is not None:
        components.append({
            "type": "button",
            "sub_type": "copy_code",
            "index": 0,
            "parameters": [{"type": "coupon_code", "coupon_code": str(otp_code)}],
        })

    # QUICK-REPLY buttons: one component each, carrying its 0-based position in the
    # approved template and the payload echoed back on tap. Meta allows at most 3.
    for index, payload_value in enumerate((button_payloads or [])[:3]):
        components.append({
            "type": "button",
            "sub_type": "quick_reply",
            "index": index,
            "parameters": [{"type": "payload", "payload": str(payload_value)}],
        })

    # DYNAMIC URL button. Same "button" component type as above but sub_type "url", and its
    # parameter is a plain text value rather than a payload: Meta appends it to the base URL
    # frozen in the approved template. index 0 because a template may carry only one URL button,
    # and it is always the first of its kind.
    if url_button_suffix:
        components.append({
            "type": "button",
            "sub_type": "url",
            "index": 0,
            "parameters": [{"type": "text", "text": str(url_button_suffix)}],
        })

    return _post({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _wa_id(wa_id),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    })


def send_text(to, body):
    """Send a free-form text message.

    VALID ONLY INSIDE THE 24 HOUR SERVICE WINDOW, meaning the recipient messaged us
    within the last 24 hours. Outside it Meta rejects the send with error 131047
    ("re-engagement message"), which this returns as a normal failure result rather
    than an exception. Use send_template for anything AFC initiates.

    Args:
        to:   recipient in E.164 (see send_template).
        body: the message text.

    Returns the standard result dict. Never raises.
    """
    wa_id = "".join(ch for ch in str(to or "") if ch.isdigit())
    if not wa_id:
        return _failure("Missing or invalid recipient number.")
    if not body:
        return _failure("Message body is empty.")

    return _post({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _wa_id(wa_id),
        "type": "text",
        "text": {"body": str(body)},
    })


def list_templates():
    """Fetch the message templates registered on our WhatsApp Business Account.

    Used by the `sync_whatsapp_templates` management command to populate the
    WhatsAppTemplate registry, which is what stops a send from referencing a
    template nobody approved. Needs WHATSAPP_BUSINESS_ACCOUNT_ID (the WABA the
    phone number belongs to), which is not used by the send path.

    Returns:
        {"ok": True, "templates": [<raw Meta template dicts>]} on success,
        the standard failure dict otherwise. Never raises.
    """
    cfg = _config()
    if not (cfg["waba_id"] and cfg["access_token"]):
        return _failure("WhatsApp business account is not configured.",
                        error_detail="WHATSAPP_BUSINESS_ACCOUNT_ID / WHATSAPP_ACCESS_TOKEN missing.")

    url = f"https://graph.facebook.com/{cfg['api_version']}/{cfg['waba_id']}/message_templates"
    try:
        response = requests.get(
            url, headers=_headers(cfg), params={"limit": 200}, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        return _failure("Could not reach WhatsApp.", error_detail=str(exc), retryable=True)

    try:
        data = response.json()
    except ValueError:
        data = {"raw_response": response.text[:500]}

    if response.ok and not data.get("error"):
        return {"ok": True, "templates": data.get("data") or [], "raw": data}

    code, title, detail = _parse_error(data, response.status_code)
    return _failure(title, status_code=response.status_code, error_code=code,
                    error_detail=detail, raw=data,
                    retryable=_is_retryable(response.status_code))
