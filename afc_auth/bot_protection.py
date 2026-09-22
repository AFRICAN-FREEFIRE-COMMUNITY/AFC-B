"""Bot protection for the forms anyone on the internet can post to (owner 2026-09-22: "lets do it").

THE PROBLEM
-----------
Four forms accept a POST from a stranger and then DO something expensive or visible: signup creates
an account and sends an email, the support contact form opens a ticket and emails staff, the
feedback form stores a row, and the partner application creates a record a human then reviews.
Nothing stood between a script and any of them. One loop fills the support queue, the partner queue
and the feedback table, and burns the mail quota on the way.

THE CHECK
---------
Cloudflare Turnstile, verified HERE, on the server. The widget on the page produces a token; this
module posts that token to Cloudflare's siteverify with the secret key and believes only the answer
Cloudflare gives. A browser-only widget protects nothing: a script simply does not load it.

Turnstile rather than hCaptcha or reCAPTCHA because AFC's DNS already sits on Cloudflare, it is free
at any volume, it shows no puzzle to a normal visitor, and it sets no advertising cookie.

TWO KEYS, AND WHAT HAPPENS WITHOUT THEM
---------------------------------------
    TURNSTILE_SECRET_KEY    server, this module. Never in the frontend.
    NEXT_PUBLIC_TURNSTILE_SITE_KEY   the public key the widget renders with.

When the secret is NOT set, this module allows the request and says so in the log once per process.
That is deliberate: a missing key must never lock signup for everybody, and an environment that has
not been given the keys yet (a developer's laptop, the scratch server) has to keep working. The
security checker counts an unconfigured production as debt, which is the right place for that
pressure - not in a 403 aimed at a real person.

Fail CLOSED on a bad token, fail OPEN on a Cloudflare outage: a token that Cloudflare rejects is
refused, but if siteverify itself cannot be reached the request is allowed and the failure logged.
Turning Cloudflare's availability into AFC's availability would be a worse bargain than the bots.

CALLERS
-------
    afc_auth/views.py signup
    afc_support/views.py support_contact
    afc_feedback/views.py submit_feedback
    afc_partner_apply/views_public.py submit_application
Each one calls `require_human(request, where=...)` FIRST, before it writes anything or sends any
mail, and returns the Response it hands back when that is not None.
"""

import logging
import os

from rest_framework import status
from rest_framework.response import Response

log = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TOKEN_FIELDS = ("cf_turnstile_response", "cf-turnstile-response", "turnstile_token")
TOKEN_HEADER = "HTTP_X_TURNSTILE_TOKEN"

BOT_CHECK_CODE = "bot_check_failed"
BOT_CHECK_MESSAGE = (
    "We could not confirm you are a person. Please reload the page and try again."
)

_warned_missing = False


def _secret():
    """The Turnstile secret, from the environment. Settings are not read directly so this works the
    same in the bot process, a management command and a test."""
    return (os.getenv("TURNSTILE_SECRET_KEY") or "").strip()


def is_configured():
    return bool(_secret())


def _token_from(request):
    data = getattr(request, "data", None) or {}
    for field in TOKEN_FIELDS:
        value = data.get(field) if hasattr(data, "get") else None
        if value:
            return str(value).strip()
    return (request.META.get(TOKEN_HEADER) or "").strip()


def _client_ip(request):
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return forwarded or (request.META.get("REMOTE_ADDR") or "")


def verify(request, where=""):
    """(ok, reason). True when the request may proceed; `reason` explains a False for the log."""
    global _warned_missing
    secret = _secret()
    if not secret:
        if not _warned_missing:
            log.warning("Turnstile is not configured (TURNSTILE_SECRET_KEY unset): public forms "
                        "are accepting posts with no bot check.")
            _warned_missing = True
        return True, "not_configured"

    token = _token_from(request)
    if not token:
        return False, "no_token"

    try:
        import requests
        answer = requests.post(
            SITEVERIFY_URL,
            data={"secret": secret, "response": token, "remoteip": _client_ip(request)},
            timeout=5,
        ).json()
    except Exception as exc:       # Cloudflare unreachable: allow, and say so loudly in the log.
        log.warning("Turnstile siteverify failed for %s (%s); allowing the request",
                    where or "a public form", exc.__class__.__name__)
        return True, "verify_unreachable"

    if answer.get("success"):
        return True, "ok"
    codes = ",".join(answer.get("error-codes") or []) or "rejected"
    log.info("Turnstile refused a post to %s: %s", where or "a public form", codes)
    return False, codes


def require_human(request, where=""):
    """None when the request may proceed, or the Response to return.

    Call it FIRST in the handler, before anything is written or emailed."""
    ok, _reason = verify(request, where=where)
    if ok:
        return None
    return Response({"message": BOT_CHECK_MESSAGE, "code": BOT_CHECK_CODE},
                    status=status.HTTP_400_BAD_REQUEST)
