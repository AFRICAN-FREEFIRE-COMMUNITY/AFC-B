# backend/afc_whatsapp/webhooks.py
# ──────────────────────────────────────────────────────────────────────────────
# THE inbound endpoint. One public URL, two jobs.
#
#   GET  /whatsapp/webhook/   Meta's verification handshake. Meta calls this once,
#                             when the webhook is registered in the Meta app, with
#                             hub.mode / hub.verify_token / hub.challenge, and
#                             expects the bare challenge echoed back as plain text.
#
#   POST /whatsapp/webhook/   Everything Meta tells us afterwards:
#                             (a) STATUS CALLBACKS for messages WE sent, matched to
#                                 a WhatsAppMessage row on `wamid` and advanced to
#                                 sent / delivered / read / failed. This is the only
#                                 way AFC ever learns a message actually arrived.
#                             (b) INBOUND MESSAGES from players and vendors. Each is
#                                 recorded, and a STOP style opt-out flips
#                                 UserProfile.whatsapp_opt_in to False. Meta REQUIRES
#                                 opt-outs be honoured; ignoring them gets the
#                                 business number rated down and eventually blocked.
#
# SIGNATURE VERIFICATION IS MANDATORY
#   Every POST must carry X-Hub-Signature-256: sha256=<hex>, the HMAC-SHA256 of the
#   RAW request body keyed with the Meta app secret (settings.WHATSAPP_APP_SECRET).
#   No signature, a wrong signature, or NO CONFIGURED SECRET all get 403.
#
#   That last case is the deliberate difference from the Kapso webhook this replaces
#   (afc_shop/whatsapp_webhook.py), which accepts unsigned POSTs whenever no secret
#   is set. This URL can flip a user's notification consent and rewrite delivery
#   history from an unauthenticated request, so "no secret configured" has to mean
#   "refuse", not "trust everyone". A missing secret is a deployment error and is
#   logged as one.
#
# NEVER 500 ON A VALID POST
#   Meta retries a webhook that does not return 2xx, with increasing frequency, so a
#   crash turns into a retry storm. Every message is processed defensively: one bad
#   entry is logged and skipped, and the response is always 200.
#
# HOW IT CONNECTS
#   ROUTE   : afc_whatsapp/urls.py, mounted at "whatsapp/" by afc/urls.py.
#   MODELS  : afc_whatsapp.WhatsAppMessage (rows created by afc_whatsapp/tasks.py
#             send_whatsapp_message are advanced here), afc_auth.UserProfile (the
#             opt-in flag and the numbers used to identify a sender).
#   HELPERS : afc_whatsapp/phone.py to_e164 / to_wa_id, so an inbound number and a
#             stored number are compared in the same normalised form.
#   APPS    : INBOUND_HANDLERS below, the one seam through which another app acts on
#             an inbound message (today: the marketplace's vendor button taps).
#   CONFIG  : WHATSAPP_APP_SECRET, WHATSAPP_WEBHOOK_VERIFY_TOKEN (afc/settings.py).
# ──────────────────────────────────────────────────────────────────────────────
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import WhatsAppMessage
from .phone import to_e164, to_wa_id

logger = logging.getLogger(__name__)


# Words that mean "stop messaging me". Meta's own guidance is to honour the common
# English keywords; French and Portuguese are here because AFC runs in three
# languages (see the i18n rules in CLAUDE.md) and a player who types "PARAR" means
# exactly the same thing. Matched against the whole trimmed, upper-cased body, so an
# ordinary sentence containing the word "stop" is not treated as an opt-out.
OPT_OUT_KEYWORDS = {
    # English
    "STOP", "STOP PROMOTIONS", "UNSUBSCRIBE", "CANCEL", "END", "QUIT",
    # French
    "ARRET", "ARRÊT", "ARRETER", "ARRÊTER", "STOP PROMOTIONS", "DESABONNER",
    # Portuguese
    "PARAR", "PARE", "SAIR", "CANCELAR", "DESCADASTRAR",
}

# Meta status values we understand. Anything else (a new value Meta adds later) is
# logged and ignored rather than guessed at.
KNOWN_STATUSES = {"sent", "delivered", "read", "failed"}

# ── App seam: inbound messages that carry ANOTHER app's business logic ────────────
# This app stays generic. It records every inbound message and honours opt-outs, and
# it knows nothing about orders, matches, or any other domain. Apps that DO own logic
# for some inbound messages register a handler here by dotted path, and every inbound
# message is offered to each in turn.
#
# It exists because Meta allows ONE callback URL per WhatsApp number, so this endpoint
# receives the whole site's inbound traffic. Without the seam, either afc_whatsapp
# would have to learn about orders, or the marketplace would silently stop working
# when its own /shop/whatsapp/webhook/ endpoint was retired.
#
# Registered today:
#   afc_shop.vendor_whatsapp.handle_inbound_message
#       marketplace vendor quick-reply taps ("ack:<order_id>", "shipdate:<order_id>",
#       "shipped:<order_id>") and inbound shipment-evidence photos. Replaces the old
#       Kapso-era /shop/whatsapp/webhook/ endpoint (deleted 2026-08-03).
#
# A handler is handed Meta's RAW message entry, decides for itself whether the message
# is any of its business, and must not raise. It is imported LAZILY (on the first
# inbound message, not at startup) so the dependency stays one-way: apps import
# afc_whatsapp, never the reverse, and no import cycle can form.
INBOUND_HANDLERS = ["afc_shop.vendor_whatsapp.handle_inbound_message"]


def _dispatch_to_app_handlers(message_entry):
    """Offer one inbound message to every registered app handler.

    Each handler is isolated: an import error or a crash in one is logged, the next
    still runs, and neither can turn a valid webhook POST into a non-2xx (Meta
    escalates its retries against anything that is not 200)."""
    for dotted_path in INBOUND_HANDLERS:
        try:
            import_string(dotted_path)(message_entry)
        except Exception as exc:
            logger.error("whatsapp webhook: inbound handler %s failed: %s", dotted_path, exc)


# ──────────────────────────────────────────────────────────────────────────────
# GET: the verification handshake
# ──────────────────────────────────────────────────────────────────────────────
def _verify_handshake(request):
    """Echo hub.challenge back so Meta will accept the webhook URL.

    Meta calls GET ...?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=<n>
    once, at registration time. The token is one the OWNER invents and types into
    both places: the Meta app config and WHATSAPP_WEBHOOK_VERIFY_TOKEN here. We
    require it to match. With no token configured we refuse, because an unverified
    handshake would let anyone point their own Meta app at this URL."""
    expected = getattr(settings, "WHATSAPP_WEBHOOK_VERIFY_TOKEN", "") or ""
    if not expected:
        logger.error(
            "whatsapp webhook: WHATSAPP_WEBHOOK_VERIFY_TOKEN is not set, refusing the "
            "verification handshake."
        )
        return Response({"message": "Webhook verification is not configured."}, status=403)

    if request.GET.get("hub.verify_token") != expected:
        logger.warning("whatsapp webhook: verification token mismatch.")
        return Response({"message": "Verification token mismatch."}, status=403)

    challenge = request.GET.get("hub.challenge", "")
    if request.GET.get("hub.mode") == "subscribe" and challenge:
        # Meta does a literal string comparison on the body, so this must be the bare
        # challenge with no JSON wrapper and no trailing whitespace.
        return HttpResponse(challenge, content_type="text/plain", status=200)

    return Response({"message": "Missing handshake parameters."}, status=400)


# ──────────────────────────────────────────────────────────────────────────────
# POST: signature verification (mandatory)
# ──────────────────────────────────────────────────────────────────────────────
def verify_signature(request):
    """True when this POST provably came from Meta.

    Meta signs every webhook delivery with the app secret and sends the result as
        X-Hub-Signature-256: sha256=<hex digest>
    computed over the RAW request body. We recompute it and compare with
    hmac.compare_digest, which is constant time, so the comparison itself leaks no
    timing signal an attacker could use to forge the digest byte by byte.

    Returns False (caller answers 403) when the secret is missing, the header is
    missing, or the digests differ. There is no permissive path.

    ORDERING NOTE: this touches request.body BEFORE anything reads request.data.
    Django caches the raw body on first access, so DRF's later JSON parse still
    works; doing it the other way round raises RawPostDataException. The view calls
    this at the very top of the POST branch to keep that order."""
    secret = getattr(settings, "WHATSAPP_APP_SECRET", "") or ""
    if not secret:
        logger.error(
            "whatsapp webhook: WHATSAPP_APP_SECRET is not set, rejecting the POST. "
            "An unsigned webhook could rewrite delivery history and flip a user's "
            "notification consent."
        )
        return False

    provided = (request.headers.get("X-Hub-Signature-256") or "").strip()
    if not provided.startswith("sha256="):
        logger.warning("whatsapp webhook: rejected POST with no sha256 signature header.")
        return False

    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, provided[len("sha256="):]):
        return True

    logger.warning("whatsapp webhook: rejected POST with a mismatching signature.")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Sender identification
# ──────────────────────────────────────────────────────────────────────────────
def _profiles_for_number(wa_id):
    """Every UserProfile whose stored WhatsApp number is this one.

    Inbound events identify the sender only by their wa_id (digits, no "+"), while
    the stored numbers are whatever the user typed, in any of the shapes phone.py
    exists to untangle. So we narrow in SQL on the last 9 digits (cheap, and enough
    to make the scan tiny) and then confirm each candidate by normalising it the same
    way we would to send to it.

    A LIST, not a single profile: duplicate UserProfile rows exist in prod for some
    users, and two accounts can legitimately share a number (a manager registering a
    younger sibling). An opt-out must silence all of them."""
    digits = to_wa_id(wa_id)
    if not digits:
        return []

    from afc_auth.models import UserProfile

    tail = digits[-9:] if len(digits) >= 9 else digits
    candidates = (
        UserProfile.objects
        .filter(whatsapp_number__endswith=tail)
        .exclude(whatsapp_number="")
        .select_related("user")
    )

    matches = []
    for profile in candidates:
        user = profile.user
        country = (getattr(user, "ip_country", "") or getattr(user, "country", "")) if user else ""
        normalised = to_e164(profile.whatsapp_number, country)
        # Compare on digits so "+234..." and "234..." are the same number. Fall back
        # to the raw digits when the stored value cannot be normalised, otherwise a
        # user with a broken country would be unreachable by an opt-out.
        stored = to_wa_id(normalised) if normalised else to_wa_id(profile.whatsapp_number)
        if stored and stored == digits:
            matches.append(profile)
    return matches


# ──────────────────────────────────────────────────────────────────────────────
# POST handlers: statuses and messages
# ──────────────────────────────────────────────────────────────────────────────
def _timestamp(raw):
    """Meta sends unix seconds as a string. Returns an aware UTC datetime, or now()
    when the value is missing or unparseable (a missing timestamp must not lose the
    event). UTC because the backend stores UTC and the frontend renders each viewer's
    own timezone (the LocalTime rule in CLAUDE.md)."""
    try:
        return datetime.fromtimestamp(int(raw), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return timezone.now()


def _process_status(status_entry):
    """Advance the WhatsAppMessage this delivery receipt refers to.

    Shape:
        {"id": "wamid....", "status": "delivered", "timestamp": "1718...",
         "recipient_id": "234805...",
         "errors": [{"code": 131047, "title": "...", "message": "...",
                     "error_data": {"details": "..."}}]}

    The wamid is the ONLY link between Meta's receipt and our row, which is why
    client.py never discards it and tasks.py stores it the instant Meta accepts a
    send. A receipt for an unknown wamid is logged and dropped: it belongs to a
    message some other system sent from this number."""
    wamid = status_entry.get("id")
    status = (status_entry.get("status") or "").lower()
    if not wamid or status not in KNOWN_STATUSES:
        logger.info("whatsapp webhook: ignoring status %r for %r", status, wamid)
        return

    message = WhatsAppMessage.objects.filter(wamid=wamid).first()
    if message is None:
        logger.info("whatsapp webhook: status '%s' for unknown wamid %s", status, wamid)
        return

    # Meta's failure reason travels in errors[0]. Preserve the numeric code and the
    # title verbatim: the code is what tells an organizer WHY (131047 = the 24 hour
    # window closed, 131026 = the number is not on WhatsApp).
    error_code, error_title = None, ""
    errors = status_entry.get("errors") or []
    if errors and isinstance(errors[0], dict):
        first = errors[0]
        try:
            error_code = int(first.get("code")) if first.get("code") is not None else None
        except (TypeError, ValueError):
            error_code = None
        error_title = (
            first.get("title")
            or (first.get("error_data") or {}).get("details")
            or first.get("message")
            or ""
        )

    changed = message.apply_status_callback(
        status,
        when=_timestamp(status_entry.get("timestamp")),
        error_code=error_code,
        error_title=error_title,
    )
    logger.info(
        "whatsapp webhook: message #%s %s -> %s", message.id, wamid,
        status if changed else f"{status} (ignored, already {message.status})",
    )


def _inbound_body(message_entry):
    """The human-readable text of an inbound message, whatever shape it arrived in.

    Three shapes carry text we care about:
      text                        -> text.body
      interactive (free-form btn) -> interactive.button_reply.title
      button (template quick-reply) -> button.text / button.payload
    Media and everything else return "" and are recorded with their type only."""
    msg_type = message_entry.get("type") or ""

    if msg_type == "text":
        return (message_entry.get("text") or {}).get("body") or ""

    if msg_type == "interactive":
        interactive = message_entry.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title") or reply.get("id") or ""

    if msg_type == "button":
        button = message_entry.get("button") or {}
        return button.get("text") or button.get("payload") or ""

    return ""


def _is_opt_out(body):
    """True when this inbound text means "stop messaging me".

    Matched on the WHOLE trimmed message, upper-cased, so "STOP" and "  stop  " opt
    out while "please stop the tournament clock" does not."""
    return bool(body) and body.strip().upper() in OPT_OUT_KEYWORDS


def _apply_opt_out(wa_id):
    """Honour an opt-out: clear whatsapp_opt_in on every profile with this number.

    Returns how many profiles were changed (0 when the sender is not an AFC account,
    which is normal: vendors and strangers also message the number)."""
    profiles = _profiles_for_number(wa_id)
    changed = 0
    for profile in profiles:
        if profile.whatsapp_opt_in:
            profile.whatsapp_opt_in = False
            profile.save(update_fields=["whatsapp_opt_in"])
            changed += 1
    if changed:
        logger.info("whatsapp webhook: opt-out from %s cleared %s profile(s).", wa_id, changed)
    else:
        logger.info("whatsapp webhook: opt-out from %s matched no AFC profile.", wa_id)
    return changed


def _process_message(message_entry):
    """Record ONE inbound message and act on it if it is an opt-out.

    Every inbound message is logged as a WhatsAppMessage row (direction "inbound")
    because the 24 hour service window is defined by the recipient's LAST inbound
    message: without this record there is no way to know whether a free-form
    queue_text send is even legal.

    Shape:
        {"from": "234805...", "id": "wamid....", "timestamp": "1718...",
         "type": "text", "text": {"body": "STOP"}}"""
    wa_id = message_entry.get("from") or ""
    wamid = message_entry.get("id") or None
    body = _inbound_body(message_entry)
    when = _timestamp(message_entry.get("timestamp"))

    # Attribute the message to an AFC account when the number is one we know.
    profiles = _profiles_for_number(wa_id)
    user_id = profiles[0].user_id if profiles else None

    fields = {
        "user_id": user_id,
        "phone": to_e164(wa_id) or f"+{to_wa_id(wa_id)}",
        "direction": "inbound",
        "status": "delivered",          # it reached US; that is the only state inbound has
        "body": body,
        "context": f"inbound:{message_entry.get('type') or 'unknown'}",
        "delivered_at": when,
    }
    if wamid:
        # Keyed on the wamid so a REDELIVERED webhook (Meta retries whenever it does
        # not get a 200) logs the message once, not once per delivery.
        WhatsAppMessage.objects.get_or_create(wamid=wamid, defaults=fields)
    else:
        # No wamid to dedupe on. Plain create: get_or_create(wamid=None) would match
        # the first queued OUTBOUND row (they all have wamid NULL) and silently
        # swallow the inbound message.
        WhatsAppMessage.objects.create(**fields)

    if _is_opt_out(body):
        _apply_opt_out(wa_id)

    # Hand the message to whichever app owns this conversation (see INBOUND_HANDLERS).
    # Deliberately LAST: the log row and the opt-out above are this app's own duties
    # and must be done whatever an app handler goes on to do.
    _dispatch_to_app_handlers(message_entry)


# ──────────────────────────────────────────────────────────────────────────────
# The view
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "POST"])
def whatsapp_webhook(request):
    """GET/POST /whatsapp/webhook/ - the single Meta Cloud API webhook for AFC.

    PURPOSE
        GET  verifies the URL with Meta (echo hub.challenge).
        POST receives delivery receipts for messages AFC sent and messages people
             send to the AFC number.

    REQUEST (GET)
        ?hub.mode=subscribe&hub.verify_token=<WHATSAPP_WEBHOOK_VERIFY_TOKEN>
        &hub.challenge=<random string>

    RESPONSE (GET)
        200 text/plain, the bare hub.challenge value.
        403 when the token is missing or wrong. 400 when the parameters are absent.

    REQUEST (POST)
        Header X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body, keyed with
        WHATSAPP_APP_SECRET>. Body is Meta's envelope:
            {"object": "whatsapp_business_account",
             "entry": [{"id": "<WABA id>", "changes": [{"field": "messages",
                "value": {"metadata": {...},
                          "statuses": [ ...delivery receipts... ],
                          "messages":  [ ...inbound messages... ]}}]}]}

    RESPONSE (POST)
        200 {"received": true} for anything with a valid signature, INCLUDING a
        payload we could not parse. Meta escalates retries against non-2xx replies,
        so a parse failure is logged, not surfaced.
        403 {"message": "Invalid webhook signature."} otherwise.

    AUTH
        None in the AFC sense: there is no session and no bearer token, because the
        caller is Meta. The HMAC signature IS the authentication, and it is required.

    CALLER
        Meta (Facebook), at the callback URL configured on the WhatsApp product in
        the Meta app dashboard: https://api.africanfreefirecommunity.com/whatsapp/webhook/
    """
    if request.method == "GET":
        return _verify_handshake(request)

    # Signature FIRST, before anything parses or trusts the body.
    if not verify_signature(request):
        return Response({"message": "Invalid webhook signature."}, status=403)

    try:
        payload = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        logger.warning("whatsapp webhook: unparseable body, acking 200 to stop retries.")
        return Response({"received": True}, status=200)

    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}

                # (a) delivery receipts for messages WE sent
                for status_entry in value.get("statuses", []) or []:
                    try:
                        _process_status(status_entry)
                    except Exception as exc:
                        # One bad receipt must not abort the batch or 500 the webhook.
                        logger.error("whatsapp webhook: error on a status: %s", exc)

                # (b) messages people sent US
                for message_entry in value.get("messages", []) or []:
                    try:
                        _process_message(message_entry)
                    except Exception as exc:
                        logger.error("whatsapp webhook: error on a message: %s", exc)
    except Exception as exc:
        # A malformed envelope: log it and still ack, so Meta stops retrying.
        logger.error("whatsapp webhook: error walking the envelope: %s", exc)

    return Response({"received": True}, status=200)
