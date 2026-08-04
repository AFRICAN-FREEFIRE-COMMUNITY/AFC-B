# ──────────────────────────────────────────────────────────────────────────────
# Delivering the deletion signal, with retries.
#
# WHAT THIS IS: the Celery half of afc_sso/webhooks.py. The payload is built and signed
# there; everything here is about getting one already-signed token to one partner URL and
# not giving up the first time their server hiccups.
#
# THE ONE RULE THIS MODULE EXISTS TO KEEP: a partner being down must never cost the player
# their revoke. The revoke has already committed by the time anything here runs, so the
# only question left is how hard AFC tries to tell the partner. Nothing in this file is
# allowed to propagate an exception back to the caller; webhooks.notify_disconnected
# wraps the dispatch, and _dispatch below swallows a dead broker.
#
# RETRY POLICY, copied deliberately from afc_whatsapp/tasks.py so the two outbound
# integrations behave the same way under failure: exponential backoff with jitter,
# 20s, 40s, 80s, 160s, 320s, each plus up to 20s, capped at 10 minutes and 5 attempts.
# The jitter is what stops a hundred signals queued by one account deletion retrying in
# lockstep and re-hammering a partner that is already struggling.
#
# WHAT IS AND IS NOT RETRIED
#   retried      a connection error, a timeout, HTTP 429, any HTTP 5xx. The partner is
#                there but not answering properly right now.
#   not retried  any other 4xx. The partner answered and said no: a wrong URL, a rejected
#                signature or an unknown route will still be wrong in five minutes, and
#                hammering it is rude and pointless.
#
# THE SAME TOKEN IS SENT ON EVERY ATTEMPT. It is signed once, in webhooks.build_signal,
# and carried through the retries as a string, so its `jti` and `iat` never change and a
# partner can dedupe on `jti` when a redelivery follows a response AFC never saw.
#
# QUEUE: "sso_webhooks", following the afc_ocr ("ocr_ml"), afc_rankings
# ("rankings_recalc") and afc_whatsapp ("whatsapp") convention, so a slow partner cannot
# block the default queue:
#     celery -A afc worker -Q sso_webhooks
# Local dev and tests: settings.SSO_WEBHOOKS_SYNC (defaults to DEBUG) runs the delivery
# inline with no worker, mirroring WHATSAPP_SYNC.
# ──────────────────────────────────────────────────────────────────────────────
import logging
import random

import requests
from celery import shared_task
from django.conf import settings

from .webhooks import CONTENT_TYPE

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 20      # seconds
_RETRY_MAX_DELAY = 600      # seconds (10 minutes)

# A partner's endpoint gets ten seconds to accept the POST. It only has to acknowledge
# receipt, not finish deleting anything, so a slow answer is a broken answer.
_TIMEOUT_SECONDS = 10

# Answers that mean "try again later" rather than "never".
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _sync() -> bool:
    """Deliver inline instead of through a worker. Defaults to DEBUG, matching
    WHATSAPP_SYNC / RANKINGS_RECALC_SYNC / OCR_ML_SYNC, so local dev and the test suite
    need no Celery and no broker."""
    return getattr(settings, "SSO_WEBHOOKS_SYNC", getattr(settings, "DEBUG", False))


@shared_task(bind=True, queue="sso_webhooks", max_retries=_MAX_RETRIES)
def deliver_disconnect_signal(self, application_id, url, token):
    """POST one signed disconnection signal to one partner.

    Args (all JSON-serialisable, because Celery carries them through the broker):
        application_id: the AFCSSOApplication this is about, for the log only.
        url:            the partner's deletion_webhook_url, already validated when it was
                        saved by provisioning._clean_outbound_url: https, and a host that
                        is not localhost and not a literal private, loopback or link-local
                        address. Every writer of that field goes through it (the public
                        application form, the draft edit, provisioning, and the admin
                        PATCH), so this task can assume it rather than re-check it.
        token:          the compact JWS from webhooks.build_signal. Signed once and
                        reused unchanged on every retry.

    Returns True when the partner accepted it, False when AFC gave up. Never raises
    anything except celery's own Retry.
    """
    try:
        response = requests.post(
            url,
            data=token.encode("utf-8"),
            headers={"Content-Type": CONTENT_TYPE},
            timeout=_TIMEOUT_SECONDS,
            # ── do not follow redirects ──
            # This URL arrives from an organisation applying to be a partner, and AFC's server
            # makes the request from inside its own network. requests follows redirects by
            # default, so a partner host that looks perfectly ordinary at review time could
            # answer with a 302 to 169.254.169.254 or a private address and have AFC fetch it,
            # carrying the signed token along. Staff can inspect the URL they approve; nobody
            # can inspect where it will redirect on the day. A partner endpoint that cannot
            # receive a POST at the address it registered is misconfigured, so refusing to
            # chase redirects costs a legitimate partner nothing.
            allow_redirects=False,
        )
        status_code = response.status_code
        # Any 2xx is acceptance. The spec asks for 202; AFC does not insist, because a
        # partner returning 200 has still received it.
        if 200 <= status_code < 300:
            logger.info(
                "sso webhook: application #%s accepted the disconnect signal (%s)",
                application_id, status_code,
            )
            return True
        retryable = status_code in _RETRYABLE_STATUSES
        reason = f"HTTP {status_code}"
    except requests.RequestException as exc:
        # Connection refused, DNS failure, TLS error, timeout. The partner is not
        # answering at all, which is the case retrying is FOR.
        status_code = None
        retryable = True
        reason = str(exc)

    if retryable and self.request.retries < _MAX_RETRIES:
        delay = min(_RETRY_MAX_DELAY, _RETRY_BASE_DELAY * (2 ** self.request.retries))
        delay += random.uniform(0, _RETRY_BASE_DELAY)  # jitter: spread a burst out
        logger.info(
            "sso webhook: application #%s did not accept the signal (%s), retry %s in %.0fs",
            application_id, reason, self.request.retries + 1, delay,
        )
        raise self.retry(
            countdown=delay,
            kwargs={"application_id": application_id, "url": url, "token": token},
        )

    logger.warning(
        "sso webhook: giving up on the disconnect signal to application #%s (%s)",
        application_id, reason,
    )
    return False


def dispatch_disconnect_signal(application_id, url, token):
    """Hand the delivery to a worker, or run it inline in dev. Never raises.

    A broker that is down (no Redis locally, a restart in production) must not take the
    caller down with it: the player's revoke is far more important than its outbound side
    effect, exactly as afc_whatsapp/tasks.py _dispatch reasons about a WhatsApp send.
    """
    try:
        if _sync():
            return deliver_disconnect_signal(application_id, url, token)
        deliver_disconnect_signal.delay(application_id, url, token)
        return None
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning(
            "sso webhook: could not dispatch the disconnect signal for application #%s: %s",
            application_id, exc,
        )
        return None
