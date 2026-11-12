"""One WhatsApp number, one ballot (owner, 2 Nov 2026, inbox #447 #448 #449; round 6 and 7 mockups approved 11 Nov 2026,
inbox #537).

Owner: "if we say we want people who vote to have a Whatsapp number saved and then they must verify it to vote" and
"what if we open a pop-up where they can like click and it take them to whatsapp". An edition with
AwardsEdition.whatsapp_required asks a voter to PROVE a WhatsApp number before the ballot is accepted; one number backs
one account (VoterWhatsApp.number is unique).

TWO WAYS TO PROVE IT
  1. The voter sends US a code (the vote sheet's "Open WhatsApp": wa.me/<AFC number>?text=AFC <code>). Free for the voter
     (they message a business) and for AFC (a user-started conversation costs nothing), and the number that counts is
     the one WhatsApp says sent it, so nobody can claim a number they do not hold. Meta posts the message to
     /whatsapp/webhook/ and afc_whatsapp.webhooks hands it to handle_inbound_message below (INBOUND_HANDLERS).
     Needs WHATSAPP_BUSINESS_NUMBER (the AFC number's digits) and Meta delivering inbound messages to the webhook
     (on 2 Nov production had never received one: the owner's Meta app setting, HANDOVER-2026-11-02).
  2. "Send me a code instead": AFC sends a six-digit code to a number the voter types (the approved login-code
     template, WHATSAPP_LOGIN_CODE_TEMPLATE, through afc_whatsapp.tasks.queue_template, the one send chokepoint) and the
     voter types it back. The vote sheet offers it after three tries of the first way, or at once when the first way
     is unavailable.

ENDPOINTS (afc_polls/urls.py, prefix polls/; Bearer session; consumed by the KOL vote sheet,
frontend app/(user)/awards/_kol/KolVoteSheet.tsx)
  GET  polls/whatsapp/            {verified, last4, inbound: bool, method}
  POST polls/whatsapp/start/      {code, wa_link, expires_in}      a code to SEND to AFC (way 1)
  POST polls/whatsapp/send-code/  {number}  -> {sent: true, last4} AFC sends a code (way 2); 3 an hour (R59)
  POST polls/whatsapp/verify/     {code}    -> {verified: true, last4}
Refusals carry a code the sheet translates (serverMessages): whatsapp_number_invalid, whatsapp_number_taken,
whatsapp_code_wrong, whatsapp_code_expired, whatsapp_too_many_codes, whatsapp_code_not_sent, whatsapp_too_many_tries.

Codes are stored HASHED (R63) and expire after CODE_TTL; a code is used once.
CONNECTS TO: models.VoterWhatsApp / VoterWhatsAppCode, views.submit_response (refuses whatsapp_check_required),
afc_whatsapp.phone (to_e164, to_wa_id), afc_whatsapp.tasks.queue_template, tests_whatsapp_votes.py.
"""

import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import VoterWhatsApp, VoterWhatsAppCode

log = logging.getLogger("afc_polls")

CODE_TTL = timedelta(minutes=30)
SEND_LIMIT_PER_HOUR = 3            # AFC-sent codes per account an hour (each one is a paid template message)
MAX_TRIES = 5                      # wrong typed codes before a sent code is spent
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"    # no 0/O, 1/I: read off a screen and typed on a phone
_INBOUND_RE = re.compile(r"\bAFC[\s:#-]*([A-Z0-9]{6})\b", re.IGNORECASE)


def _hash(code):
    key = (settings.SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, code.strip().upper().encode("utf-8"), hashlib.sha256).hexdigest()


def _refuse(message, code, status=400):
    return Response({"message": message, "code": code}, status=status)


def _user(request):
    from .views import _user_from_request
    return _user_from_request(request)


def business_number():
    """The AFC WhatsApp number voters send their code to, digits only ("" when not configured)."""
    return re.sub(r"\D", "", getattr(settings, "WHATSAPP_BUSINESS_NUMBER", "") or "")


def verified_number(user):
    """The proved number of `user`, or "" (never proved)."""
    if user is None:
        return ""
    row = VoterWhatsApp.objects.filter(user=user).first()
    return row.number if row else ""


def _claim(user, number, method):
    """Make `number` this user's proved number. Returns "" or the refusal code when another account holds it."""
    other = VoterWhatsApp.objects.filter(number=number).exclude(user=user).exists()
    if other:
        return "whatsapp_number_taken"
    try:
        with transaction.atomic():
            VoterWhatsApp.objects.update_or_create(user=user, defaults={"number": number, "method": method})
    except IntegrityError:
        return "whatsapp_number_taken"           # two accounts raced for the same number: the first one keeps it
    return ""


def _new_code(user, kind, number="", alphabet=_ALPHABET, length=6):
    code = "".join(secrets.choice(alphabet) for _ in range(length))
    VoterWhatsAppCode.objects.create(user=user, kind=kind, code_hash=_hash(code), number=number)
    return code


def _live_codes(**filters):
    return VoterWhatsAppCode.objects.filter(used_at__isnull=True, created_at__gte=timezone.now() - CODE_TTL, **filters)


# ── way 1: the voter sends us the code ──────────────────────────────────────────────────────────────
def handle_inbound_message(message_entry):
    """afc_whatsapp INBOUND_HANDLERS seam: a text "AFC <code>" from a number proves that number for the code's owner.
    Ignores everything else; never raises (a crash here must not turn Meta's webhook into a retry storm)."""
    try:
        if (message_entry or {}).get("type") != "text":
            return
        body = ((message_entry.get("text") or {}).get("body") or "")
        m = _INBOUND_RE.search(body)
        sender = re.sub(r"\D", "", str(message_entry.get("from") or ""))
        if not m or not sender:
            return
        row = _live_codes(kind=VoterWhatsApp.INBOUND, code_hash=_hash(m.group(1))).select_related("user").first()
        if row is None:
            log.info("whatsapp vote check: a code that matches nothing live (sender ...%s)", sender[-4:])
            return
        refused = _claim(row.user, "+" + sender, VoterWhatsApp.INBOUND)
        if refused:
            log.info("whatsapp vote check: number ...%s already backs another account", sender[-4:])
            return
        row.used_at = timezone.now()
        row.save(update_fields=["used_at"])
        log.info("whatsapp vote check: user %s proved ...%s by message", row.user_id, sender[-4:])
    except Exception:                                # pragma: no cover - logged, never raised to the webhook
        log.exception("whatsapp vote check: inbound handler failed")


class _SendBody(serializers.Serializer):
    number = serializers.CharField(max_length=24)


class _VerifyBody(serializers.Serializer):
    code = serializers.RegexField(r"^\s*\d{6}\s*$")


@api_view(["GET"])
def whatsapp_status(request):
    """See the module docstring."""
    user = _user(request)
    if user is None:
        return _refuse("Please sign in to continue.", "invalid_token", 401)
    row = VoterWhatsApp.objects.filter(user=user).first()
    return Response({"verified": bool(row), "last4": row.number[-4:] if row else "",
                     "method": row.method if row else "", "inbound": bool(business_number())})


@api_view(["POST"])
def whatsapp_start(request):
    """A fresh code for the voter to SEND to AFC (way 1). Each call makes a new one; older ones stay valid until they
    expire, so a voter who reopened the sheet can still send the code they copied."""
    user = _user(request)
    if user is None:
        return _refuse("Please sign in to continue.", "invalid_token", 401)
    number = business_number()
    if not number:
        return _refuse("Sending a code to AFC is not available. Ask for a code instead.", "whatsapp_inbound_off", 409)
    recent = VoterWhatsAppCode.objects.filter(user=user, kind=VoterWhatsApp.INBOUND,
                                              created_at__gte=timezone.now() - timedelta(hours=1)).count()
    if recent >= 20:
        return _refuse("Too many codes in an hour. Try again later.", "whatsapp_too_many_codes", 429)
    code = _new_code(user, VoterWhatsApp.INBOUND)
    text = "AFC %s" % code
    return Response({"code": code, "text": text, "wa_link": "https://wa.me/%s?text=%s" % (number, text.replace(" ", "%20")),
                     "expires_in": int(CODE_TTL.total_seconds())})


@api_view(["POST"])
def whatsapp_send_code(request):
    """Way 2: AFC sends a six-digit code to the number the voter typed."""
    from afc_whatsapp.phone import to_e164

    user = _user(request)
    if user is None:
        return _refuse("Please sign in to continue.", "invalid_token", 401)
    body = _SendBody(data=request.data)
    if not body.is_valid():
        return _refuse("That is not a WhatsApp number.", "whatsapp_number_invalid")
    country = getattr(user, "ip_country", "") or getattr(user, "country", "") or None
    number = to_e164(body.validated_data["number"], country) or ""
    if not number:
        return _refuse("That is not a WhatsApp number. Include the country code, like +234.", "whatsapp_number_invalid")
    if VoterWhatsApp.objects.filter(number=number).exclude(user=user).exists():
        return _refuse("That WhatsApp number already backs another account.", "whatsapp_number_taken", 409)
    sent_last_hour = VoterWhatsAppCode.objects.filter(user=user, kind=VoterWhatsApp.CODE,
                                                      created_at__gte=timezone.now() - timedelta(hours=1)).count()
    if sent_last_hour >= SEND_LIMIT_PER_HOUR:
        return _refuse("Three codes were sent in the last hour. Try again later.", "whatsapp_too_many_codes", 429)
    code = _new_code(user, VoterWhatsApp.CODE, number=number, alphabet="0123456789")
    if not _deliver(user, number, code):
        VoterWhatsAppCode.objects.filter(user=user, kind=VoterWhatsApp.CODE, used_at__isnull=True).update(
            used_at=timezone.now())
        return _refuse("The code could not be sent. Try again in a minute.", "whatsapp_code_not_sent", 502)
    return Response({"sent": True, "last4": number[-4:]})


def _deliver(user, number, code):
    """Send `code` on the approved login-code template. False when it could not go (no template set, refused)."""
    from afc_auth.two_factor import _template_language
    from afc_whatsapp.tasks import queue_template

    template = getattr(settings, "WHATSAPP_LOGIN_CODE_TEMPLATE", "") or ""
    if not template:
        log.warning("whatsapp vote check: WHATSAPP_LOGIN_CODE_TEMPLATE is not configured; no code sent")
        return False
    try:
        return bool(queue_template(number, template, _template_language(user), body_params=[code], user=user,
                                   context="vote_whatsapp_code", otp_button=True, redact_variables=True))
    except Exception:
        log.exception("whatsapp vote check: the code could not be queued")
        return False


@api_view(["POST"])
def whatsapp_verify(request):
    """Way 2, second half: the code the voter typed proves the number it was sent to."""
    user = _user(request)
    if user is None:
        return _refuse("Please sign in to continue.", "invalid_token", 401)
    body = _VerifyBody(data=request.data)
    if not body.is_valid():
        return _refuse("Type the six digits from WhatsApp.", "whatsapp_code_wrong")
    row = (_live_codes(user=user, kind=VoterWhatsApp.CODE).order_by("-created_at").first())
    if row is None:
        return _refuse("That code has expired. Ask for a new one.", "whatsapp_code_expired", 410)
    if row.attempts >= MAX_TRIES:
        return _refuse("Too many wrong codes. Ask for a new one.", "whatsapp_too_many_tries", 429)
    if not hmac.compare_digest(row.code_hash, _hash(body.validated_data["code"])):
        row.attempts += 1
        row.save(update_fields=["attempts"])
        return _refuse("That code is not the one we sent.", "whatsapp_code_wrong")
    refused = _claim(user, row.number, VoterWhatsApp.CODE)
    if refused:
        return _refuse("That WhatsApp number already backs another account.", refused, 409)
    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])
    return Response({"verified": True, "last4": row.number[-4:]})
