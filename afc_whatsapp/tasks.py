# backend/afc_whatsapp/tasks.py
# ──────────────────────────────────────────────────────────────────────────────
# The send pipeline: the Celery task that puts ONE message on the wire, plus the
# two helpers the rest of AFC calls.
#
# THE PUBLIC INTERFACE (this is what other apps import; nothing else)
#   queue_template(to, template_name, language, *, body_params=None,
#                  button_payloads=None, user=None, country=None, event=None,
#                  match=None, context="")
#   queue_text(to, body, *, user=None, country=None, event=None, match=None,
#              context="")
# Both return the WhatsAppMessage id that will carry the send (or None when the
# send was refused before a row made sense, e.g. the recipient opted out). Neither
# raises: WhatsApp is a bonus channel and must never break the flow that triggered
# it, exactly as the Zernio and Kapso callers assumed.
#
# ROW BEFORE SEND (the rule that makes the log trustworthy)
#   send_whatsapp_message writes the WhatsAppMessage row with status "queued"
#   BEFORE it calls Meta. A worker that dies mid-send therefore leaves visible
#   evidence ("queued", never advanced) instead of nothing at all. On retry the task
#   is handed the SAME message_id so a retried send updates one row rather than
#   spawning duplicates.
#
# QUEUE
#   Dedicated "whatsapp" queue, following the afc_ocr ("ocr_ml") and afc_rankings
#   ("rankings_recalc") convention in afc/celery_config.py, so a slow Meta never
#   blocks the default queue:
#       celery -A afc worker -Q whatsapp
#   Local dev: settings.WHATSAPP_SYNC (defaults to DEBUG) runs sends inline, no
#   worker needed, mirroring RANKINGS_RECALC_SYNC / OCR_ML_SYNC.
#
# HOW IT CONNECTS
#   CALLS  : afc_whatsapp/client.py (the HTTP layer), afc_whatsapp/phone.py
#            (normalisation), afc_whatsapp/models.py (the log + template registry).
#   READS  : afc_auth.UserProfile.whatsapp_opt_in / whatsapp_number (consent, which
#            Meta policy requires us to honour) via afc_auth.models.profile_of.
#   FEEDS  : afc_whatsapp/webhooks.py, which finds the row this task created by its
#            wamid and advances it to delivered / read / failed.
#   FUTURE CALLERS (a separate cutover task repoints them):
#            afc_shop/fulfilment.py notify_vendor, and the tournament room-details
#            broadcast that currently goes through whatsapp_zernio.
# ──────────────────────────────────────────────────────────────────────────────
import logging
import random

from celery import shared_task
from django.conf import settings

from . import client
from .models import WhatsAppMessage, WhatsAppTemplate
from .phone import to_e164

logger = logging.getLogger(__name__)

# Retry policy for TRANSIENT failures only (network down, Meta 429/5xx). Exponential
# backoff with jitter, per the transient-fault rule: 20s, 40s, 80s, 160s, 320s,
# each plus up to 20s of jitter so a burst of failed sends does not retry in
# lockstep and re-hammer Meta at the same instant. Capped in both count and delay so
# a permanently broken send cannot circle forever.
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 20      # seconds
_RETRY_MAX_DELAY = 600      # seconds (10 minutes)


def _sync() -> bool:
    """Run sends inline instead of through a worker. Defaults to DEBUG, matching
    RANKINGS_RECALC_SYNC and OCR_ML_SYNC, so local dev needs no Celery."""
    return getattr(settings, "WHATSAPP_SYNC", getattr(settings, "DEBUG", False))


# ──────────────────────────────────────────────────────────────────────────────
# Guards
# ──────────────────────────────────────────────────────────────────────────────
def is_send_allowed(template_name, language):
    """Should this template send even be attempted? Returns (allowed, reason).

    The registry (WhatsAppTemplate, filled by `manage.py sync_whatsapp_templates`)
    exists so a send cannot reference a template nobody approved: Meta would reject
    it with 132001 after the fact, and the recipient would simply never hear from us.

    Three cases:
      - a row exists and says approved      -> allowed.
      - a row exists and says NOT approved  -> refused here, no Meta call, and the
                                               row records why.
      - no row at all                       -> allowed ONLY when the registry is
                                               completely empty, i.e. the sync has
                                               never run on this server. An empty
                                               table means "not configured", and
                                               blocking every message on a missing
                                               ops step would be a far worse failure
                                               than letting Meta be the judge. Once
                                               the registry has ANY row it is
                                               authoritative and an unknown template
                                               is refused.
    Free-form text (no template_name) is never gated here: Meta's 24 hour service
    window is the only rule that applies to it."""
    if not template_name:
        return True, ""

    known, approved = WhatsAppTemplate.approval_state(template_name, language)
    if known:
        if approved:
            return True, ""
        return False, f"Template '{template_name}' [{language}] is not approved."

    if not WhatsAppTemplate.objects.exists():
        # Registry never synced: fall through and let Meta validate.
        return True, ""
    return False, f"Template '{template_name}' [{language}] is not in the approved registry."


def _opted_out(user):
    """True when this AFC user has switched WhatsApp notifications OFF.

    Consent is Meta policy, not a nicety: a business that keeps messaging after an
    opt-out gets its number rated down and eventually blocked. The flag lives on
    UserProfile.whatsapp_opt_in (set on the profile settings page, and flipped to
    False by an inbound STOP in webhooks.py). Absence of a profile is NOT treated as
    an opt-out, matching the field's default of True.

    Resolved through afc_auth.canonical_profile, not profile_of: duplicate
    UserProfile rows exist in prod, and canonical_profile is the one resolver every
    reader and writer agrees on (lowest profile_id). It accepts a User or a raw id."""
    if user is None:
        return False
    try:
        from afc_auth.models import canonical_profile
        profile = canonical_profile(user)
    except Exception:  # a profile lookup must never break a send decision
        return False
    if profile is None:
        return False
    return not getattr(profile, "whatsapp_opt_in", True)


# ──────────────────────────────────────────────────────────────────────────────
# The task
# ──────────────────────────────────────────────────────────────────────────────
@shared_task(bind=True, queue="whatsapp", max_retries=_MAX_RETRIES)
def send_whatsapp_message(
    self,
    to,
    template_name="",
    language="",
    body_params=None,
    button_payloads=None,
    text="",
    user_id=None,
    country=None,
    event_id=None,
    match_id=None,
    context="",
    message_id=None,
):
    """Send exactly ONE WhatsApp message and record what happened.

    Args (all JSON-serialisable, because Celery has to carry them through the broker):
        to:              recipient number in ANY shape. Normalised here via to_e164.
        template_name:   approved template name. Empty means send `text` free-form.
        language:        the language the template was approved under ("en_US").
        body_params:     ordered values for the template's {{1}}..{{N}} variables.
        button_payloads: ordered quick-reply payloads (max 3), echoed back on tap.
        text:            body for a free-form send (only valid inside the 24 hour
                         service window).
        user_id:         AFC user this is going to, for the log and the opt-in check.
        country:         the recipient's country, used ONLY to resolve a local-form
                         number ("08051234567") to E.164. Pass
                         `user.ip_country or user.country`.
        event_id/match_id: what the message is about, for the log.
        context:         short label for what triggered it ("room_details").
        message_id:      set by the retry path so a retried send updates the SAME
                         WhatsAppMessage row instead of creating a new one. Callers
                         leave it None.

    Returns the WhatsAppMessage id, so a caller running inline (WHATSAPP_SYNC) can
    read the outcome straight off the row.
    """
    # ── 1. the row, BEFORE anything can fail ──────────────────────────────────
    # A first attempt creates it; a retry reuses it, so the log holds one row per
    # message rather than one per attempt.
    normalised = to_e164(to, country)
    if message_id:
        message = WhatsAppMessage.objects.filter(id=message_id).first()
    else:
        message = None
    if message is None:
        message = WhatsAppMessage.objects.create(
            user_id=user_id,
            phone=normalised or str(to or "")[:20],
            direction="outbound",
            template_name=template_name or "",
            template_language=language or "",
            variables={"body": list(body_params or []), "buttons": list(button_payloads or [])},
            body=text or "",
            event_id=event_id,
            match_id=match_id,
            context=context or "",
            status="queued",
        )

    # ── 2. refuse the sends that cannot work, with the reason on the row ──────
    if not normalised:
        # The 34-of-133 case when the account has no country to anchor the number.
        message.mark_failed(
            error_title="Could not resolve the phone number to international format."
        )
        logger.warning(
            "whatsapp: unusable number %r (country=%r) for message #%s",
            to, country, message.id,
        )
        return message.id

    allowed, reason = is_send_allowed(template_name, language)
    if not allowed:
        message.mark_failed(error_title=reason)
        logger.warning("whatsapp: refused message #%s: %s", message.id, reason)
        return message.id

    # ── 3. send ───────────────────────────────────────────────────────────────
    if template_name:
        result = client.send_template(
            normalised, template_name, language,
            body_params=body_params, button_payloads=button_payloads,
        )
    else:
        result = client.send_text(normalised, text)

    if result["ok"]:
        message.mark_sent(result["wamid"])
        logger.info(
            "whatsapp: message #%s sent to %s (wamid %s)",
            message.id, normalised, result["wamid"],
        )
        return message.id

    # ── 4. failure: retry the transient ones, record the rest ─────────────────
    # Meta's code and title are written to the row FIRST, so even a message still
    # circling through retries shows its latest reason instead of looking silent.
    message.mark_failed(
        error_code=result.get("error_code"),
        error_title=result.get("error_title") or "WhatsApp send failed.",
    )

    if result.get("retryable") and self.request.retries < _MAX_RETRIES:
        delay = min(_RETRY_MAX_DELAY, _RETRY_BASE_DELAY * (2 ** self.request.retries))
        delay += random.uniform(0, _RETRY_BASE_DELAY)  # jitter: spread a burst out
        logger.info(
            "whatsapp: message #%s failed transiently (%s), retry %s in %.0fs",
            message.id, result.get("error_title"), self.request.retries + 1, delay,
        )
        raise self.retry(countdown=delay, kwargs={
            "to": normalised,
            "template_name": template_name,
            "language": language,
            "body_params": body_params,
            "button_payloads": button_payloads,
            "text": text,
            "user_id": user_id,
            "country": country,
            "event_id": event_id,
            "match_id": match_id,
            "context": context,
            "message_id": message.id,   # same row on every attempt
        })

    logger.warning(
        "whatsapp: message #%s failed permanently (meta %s: %s)",
        message.id, result.get("error_code"), result.get("error_title"),
    )
    return message.id


# ──────────────────────────────────────────────────────────────────────────────
# What the rest of the codebase calls
# ──────────────────────────────────────────────────────────────────────────────
def _dispatch(kwargs):
    """Hand the send to a worker, or run it inline in dev. Never raises.

    A broker that is down (no Redis locally, a restart in prod) must not take the
    caller down with it, so a dispatch failure is logged and swallowed: the calling
    flow (an order being paid, room details being published) is far more important
    than its WhatsApp side effect."""
    try:
        if _sync():
            return send_whatsapp_message(**kwargs)
        send_whatsapp_message.delay(**kwargs)
        return None
    except Exception as exc:
        logger.warning("whatsapp: could not dispatch send (%s): %s", kwargs.get("context"), exc)
        return None


def _resolve(user, event, match):
    """Turn model instances into the primary keys Celery can serialise. Accepts an
    instance or a raw id for each, so callers do not have to care."""
    def pk(obj, attr):
        if obj is None:
            return None
        return getattr(obj, attr, obj)
    return pk(user, "pk"), pk(event, "event_id"), pk(match, "match_id")


def queue_template(to, template_name, language, *, body_params=None, button_payloads=None,
                   user=None, country=None, event=None, match=None, context=""):
    """Queue an approved TEMPLATE send. The entry point for anything AFC initiates.

    Args:
        to:              recipient number in any shape (normalised downstream).
        template_name:   approved template name.
        language:        the language it was approved under ("en_US" != "en").
        body_params:     ordered values for {{1}}..{{N}}.
        button_payloads: ordered quick-reply payloads (max 3).
        user:            the AFC User (or user id) being messaged. Drives the opt-in
                         check and, when `country` is not given, the country used to
                         normalise a local number.
        country:         override for that country (ISO-2 code or name).
        event/match:     the Event/Match (or ids) this is about, for the log.
        context:         short trigger label, e.g. "room_details".

    Returns the WhatsAppMessage id when the send ran inline, else None. Never raises.
    """
    if _opted_out(user):
        logger.info("whatsapp: skipping '%s' send, recipient has opted out.", context)
        return None

    if country is None and user is not None:
        # Same precedence afc_auth.fx.user_currency uses: where the player actually
        # is, falling back to the country on their profile.
        country = getattr(user, "ip_country", "") or getattr(user, "country", "") or None

    user_id, event_id, match_id = _resolve(user, event, match)
    return _dispatch({
        "to": to,
        "template_name": template_name,
        "language": language,
        "body_params": list(body_params or []),
        "button_payloads": list(button_payloads or []),
        "user_id": user_id,
        "country": country,
        "event_id": event_id,
        "match_id": match_id,
        "context": context,
    })


def queue_text(to, body, *, user=None, country=None, event=None, match=None, context=""):
    """Queue a free-form TEXT send.

    ONLY valid inside the 24 hour service window (the recipient messaged us in the
    last 24 hours). Outside it Meta rejects the send with code 131047 and the row is
    marked failed with that reason. Use queue_template for anything AFC initiates.

    Same arguments as queue_template minus the template ones. Returns the
    WhatsAppMessage id when run inline, else None. Never raises.
    """
    if _opted_out(user):
        logger.info("whatsapp: skipping '%s' text, recipient has opted out.", context)
        return None

    if country is None and user is not None:
        country = getattr(user, "ip_country", "") or getattr(user, "country", "") or None

    user_id, event_id, match_id = _resolve(user, event, match)
    return _dispatch({
        "to": to,
        "text": body,
        "user_id": user_id,
        "country": country,
        "event_id": event_id,
        "match_id": match_id,
        "context": context,
    })
