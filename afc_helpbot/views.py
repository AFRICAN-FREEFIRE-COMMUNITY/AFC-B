"""
afc_helpbot.views - the website Help panel (inbox #109; owner approved the preview 2026-10-04).

Owner, 1 Oct 2026: "Just like discord, can we set a bot that works and replies or helps with support
on the website?" Picks: a Help button on every page, open to everyone, signed in or not, answers about
the signed-in person's own account, GPT-4o like the Discord bot.

THE ENDPOINTS (mounted at help-bot/ in afc/urls.py)
    GET  help-bot/status/    is the assistant on, and how many questions are left today
    POST help-bot/chat/      ask a question, get the answer
    POST help-bot/handoff/   "Talk to a person": opens a support ticket with the chat attached
All three are consumed by the frontend's components/help/HelpBot.tsx through lib/api/helpBot.ts.

WHO DECIDES WHAT
    This module is the GATE. The answer itself comes from the Discord bot's process (brain.py
    explains why). Everything about WHO may ask is settled here first, in this order:
      1. the input is parsed (type, length, format) and refused with a code if wrong (R69);
      2. the caller is identified by the server: a Bearer SessionToken through validate_token, or,
         signed out, the random browser id the panel keeps plus a salted hash of the network (R64);
      3. a conversation is only ever continued by its owner: the same account, or the same browser
         while signed out; anything else is the same 404 as a token that does not exist (R58, R88);
      4. the daily allowance (R75): 30 questions a day signed in, 10 per browser and 60 per network
         signed out, counted from the stored questions themselves, 429 with a code when spent;
      5. a burst limit, 6 questions a minute per person, 429 (R59);
      6. a new signed-out conversation, and every signed-out handoff, must pass the Cloudflare
         Turnstile check on the server (R68, afc_auth.bot_protection.require_human);
      7. at most HELP_BOT_MAX_INFLIGHT questions are with the model at once across the whole API.
         The API runs 5 synchronous gunicorn workers (deploy/vps/django_app.service) and a model can
         take 15 seconds; without this cap a handful of questions at the same moment would hold
         every worker and stall the whole website. Past the cap the panel is told "busy" (503).
    Only then are the person's own account facts built (facts.py) and the question sent.

WHAT IS STORED: the question and the answer (models.py), deleted after HELP_BOT_RETENTION_DAYS.
    A question that fails to get an answer is not stored and does not count against the allowance.

CONNECTS TO: afc_helpbot/brain.py (the answer), afc_helpbot/facts.py (the account facts),
afc_support.views.create_ticket_from_contact + acknowledge_new_ticket (the ticket, its emails and
Discord DM, exactly as the contact form does), afc_auth.bot_protection (Turnstile),
afc_auth.client_ip (the visitor's address, for the per-network allowance).
"""
import hashlib
import logging
import re
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.bot_protection import require_human
from afc_auth.bot_protection import is_configured as bot_check_configured
from afc_auth.models import User
from afc_auth.views import validate_token
from afc_support import notify
from afc_support.models import SupportMessage, SupportTicket
from afc_support.views import acknowledge_new_ticket, create_ticket_from_contact

from . import brain
from .facts import account_facts
from .models import HelpConversation, HelpInputLog, HelpMessage

log = logging.getLogger(__name__)

# ── Limits. Every number can be changed in the server .env without a deploy of code (afc/settings.py). ──
MAX_MESSAGE_CHARS = 1000          # one question
MAX_HANDOFF_CHARS = 2000          # the "what do you need help with" box, when there is no chat yet
HISTORY_TURNS = 12                # earlier turns sent with a question, so follow-ups make sense
TRANSCRIPT_TURNS = 40             # turns copied to a ticket
HANDOFFS_PER_HOUR = 5             # tickets one person can open from the panel in an hour

TOKEN_RE = re.compile(r"^h_[0-9a-f]{24}$")
VISITOR_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# The contact form's own rule (afc_support.views.support_contact), so both doors accept the same addresses.
EMAIL_RE = re.compile(r"^[\w\.-]+@[\w\.-]+\.\w+$")


def _setting(name, default):
    return getattr(settings, name, default)


def _enabled() -> bool:
    return bool(_setting("HELP_BOT_ENABLED", True)) and brain.is_configured()


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  Who is asking
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _actor(request):
    """The signed-in user behind a Bearer SessionToken, or None (signed out). The panel works for
    both, so a missing or expired token is simply "signed out", never an error."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ", 1)[1].strip())


# The visitor's address: one rule for the whole API (afc_auth/client_ip.py, inbox #141). It trusts
# nginx's X-Real-IP, never a visitor-written X-Forwarded-For, so a script cannot choose a new
# "network" (and a fresh 60-a-day allowance) on every request.
from afc_auth.client_ip import client_ip as _client_ip  # noqa: E402


def _hash(kind, value):
    """A salted one-way hash (SECRET_KEY is the salt): enough to count and to match, useless outside
    this deployment, and never the raw address or browser id on a row."""
    if not value:
        return ""
    return hashlib.sha256(f"{settings.SECRET_KEY}:helpbot:{kind}:{value}".encode("utf-8")).hexdigest()


def _locale(request):
    locale = (getattr(request, "locale", "") or "en")[:2]
    return locale if locale in ("en", "fr", "pt") else "en"


def _refuse(message, code, http_status, **extra):
    return Response({"message": message, "code": code, **extra}, status=http_status)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Input
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _validate_text(value, max_chars):
    """A non-empty string of at most max_chars, trimmed. None when it is not."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_chars:
        return None
    return value


def _validate_token(value):
    """A conversation token, or "" when none was sent. None when one was sent and is malformed."""
    if value in (None, ""):
        return ""
    return value if isinstance(value, str) and TOKEN_RE.match(value) else None


def _validate_visitor(value):
    return value if isinstance(value, str) and VISITOR_RE.match(value) else None


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Whose conversation
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _owned_conversation(token, user, visitor_hash):
    """The conversation if the caller owns it, else None.

    Signed in: it is theirs, or it was started in this same browser while signed out (then it is
    claimed for the account, so a visitor who signs in mid-chat keeps the chat). Signed out: it has
    no account and was started in this browser. Every other case looks exactly like a token that
    does not exist (R88: a real conversation and a missing one answer the same)."""
    conv = HelpConversation.objects.filter(public_token=token).first()
    if conv is None:
        return None
    if user is not None:
        if conv.user_id == user.pk:
            return conv
        if conv.user_id is None and visitor_hash and conv.visitor_hash == visitor_hash:
            conv.user = user
            conv.save(update_fields=["user"])
            return conv
        return None
    if conv.user_id is None and visitor_hash and conv.visitor_hash == visitor_hash:
        return conv
    return None


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  The daily allowance (R75) and the rate limits (R59)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _day_start():
    """Midnight UTC today: the allowance resets at the same moment for everybody."""
    return timezone.now().astimezone(dt_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _questions_today(**conversation_filter):
    return HelpMessage.objects.filter(
        role=HelpMessage.ROLE_USER, created_at__gte=_day_start(),
        **{f"conversation__{k}": v for k, v in conversation_filter.items()},
    ).count()


def _allowance(user, visitor_hash, ip_hash):
    """(limit, remaining, code_when_spent) for this caller today.

    Signed in: the account's own questions. Signed out: this browser's questions, AND a ceiling for
    the whole network so clearing the browser does not reset the count; whichever runs out first."""
    if user is not None:
        limit = int(_setting("HELP_BOT_DAILY_SIGNED_IN", 30))
        used = _questions_today(user=user)
        return limit, max(0, limit - used), "help_daily_limit"
    limit = int(_setting("HELP_BOT_DAILY_SIGNED_OUT", 10))
    used = _questions_today(user__isnull=True, visitor_hash=visitor_hash) if visitor_hash else 0
    remaining = max(0, limit - used)
    if ip_hash:
        network_limit = int(_setting("HELP_BOT_DAILY_PER_NETWORK", 60))
        network_used = _questions_today(user__isnull=True, ip_hash=ip_hash)
        if network_used >= network_limit:
            return limit, 0, "help_network_limit"
    return limit, remaining, "help_daily_limit"


def _counter(key, timeout):
    """Add one to a cache counter and return the new value. The cache is Redis in production
    (afc/settings.py CACHES), shared by every gunicorn worker, so the count is site-wide."""
    cache.add(key, 0, timeout)
    try:
        return cache.incr(key)
    except ValueError:  # it expired between add and incr
        cache.set(key, 1, timeout)
        return 1


def _rate_limited(bucket, identity, per_window, window_secs):
    """True once `identity` has gone over `per_window` in the current window (429)."""
    window = int(timezone.now().timestamp() // window_secs)
    return _counter(f"helpbot:{bucket}:{identity}:{window}", window_secs * 2) > per_window


class _InFlight:
    """At most HELP_BOT_MAX_INFLIGHT questions with the model at once (see §WHO DECIDES WHAT, 7).

    The counter expires after 120 s on its own, so a worker killed mid-question cannot leave the
    panel "busy" for good."""
    KEY = "helpbot:inflight"

    def __enter__(self):
        self.entered = _counter(self.KEY, 120) <= int(_setting("HELP_BOT_MAX_INFLIGHT", 2))
        if not self.entered:
            self._release()
        return self

    def __exit__(self, *exc):
        if self.entered:
            self._release()
        return False

    def _release(self):
        try:
            if cache.decr(self.KEY) < 0:
                cache.set(self.KEY, 0, 120)
        except ValueError:
            pass  # already expired: nothing to give back


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5  GET help-bot/status/
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def help_status(request):
    """GET help-bot/status/?visitor=<browser id>

    REQUEST   optional Bearer SessionToken; signed out, ?visitor= the panel's browser id.
    RESPONSE  200 {online, signed_in, limit, remaining, bot_check, spent}
              `bot_check` is true when a signed-out visitor must pass Turnstile to start a chat.
              `spent` names the allowance that ran out (help_daily_limit / help_network_limit) when
              remaining is 0, else null, so the panel shows the right sentence.
    AUTH      none: the panel is on every page, for everyone.
    CONSUMED  components/help/HelpBot.tsx when the panel opens (the "N questions left today" line
              and the offline state).
    """
    user = _actor(request)
    visitor = _validate_visitor(request.query_params.get("visitor"))
    visitor_hash = _hash("visitor", visitor) if visitor and user is None else ""
    ip_hash = _hash("ip", _client_ip(request)) if user is None else ""
    limit, remaining, spent_code = _allowance(user, visitor_hash, ip_hash)
    return Response({
        "online": _enabled(),
        "signed_in": user is not None,
        "limit": limit,
        "remaining": remaining,
        "bot_check": user is None and bot_check_configured(),
        "spent": spent_code if remaining <= 0 else None,
    })


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6  POST help-bot/chat/
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _help_chat(request):
    """POST help-bot/chat/ (logged by the help_chat wrapper in §8)

    REQUEST   JSON {message, conversation?, visitor?, cf_turnstile_response?}
              message       the question, 1 to 1000 characters
              conversation  h_<24 hex> to continue a chat; omitted to start one
              visitor       the panel's random browser id; required when signed out
              cf_turnstile_response  required when signed out AND starting a conversation
    RESPONSE  200 {conversation, reply, needs_person, used_account, needs_sign_in, limit, remaining}
              400 help_message_invalid | help_conversation_invalid | help_visitor_required | bot_check_failed
              404 help_conversation_not_found   not yours, or gone (the panel starts a new one)
              429 help_daily_limit | help_network_limit (with limit, remaining 0) | help_slow_down
              503 help_ai_offline | help_busy     504 help_ai_timeout     502 help_ai_failed
    AUTH      optional Bearer SessionToken. Signed in, the answer may use the person's own account.
    CONSUMED  components/help/HelpBot.tsx (lib/api/helpBot.ts askHelpBot).
    """
    data = request.data if hasattr(request.data, "get") else {}
    message = _validate_text(data.get("message"), MAX_MESSAGE_CHARS)
    if message is None:
        return _refuse(f"Write a question of up to {MAX_MESSAGE_CHARS} characters.", "help_message_invalid",
                       status.HTTP_400_BAD_REQUEST)
    if not _enabled():
        return _refuse("The assistant is offline right now.", "help_ai_offline", status.HTTP_503_SERVICE_UNAVAILABLE)
    token = _validate_token(data.get("conversation"))
    if token is None:
        return _refuse("That conversation reference is not valid.", "help_conversation_invalid",
                       status.HTTP_400_BAD_REQUEST)

    user = _actor(request)
    visitor = _validate_visitor(data.get("visitor"))
    if user is None and not visitor:
        return _refuse("Reload the page and try again.", "help_visitor_required", status.HTTP_400_BAD_REQUEST)
    visitor_hash = _hash("visitor", visitor) if visitor else ""
    ip_hash = _hash("ip", _client_ip(request))

    conv = None
    if token:
        conv = _owned_conversation(token, user, visitor_hash)
        if conv is None:
            return _refuse("That conversation was not found. Start a new one.", "help_conversation_not_found",
                           status.HTTP_404_NOT_FOUND)

    limit, remaining, spent_code = _allowance(user, visitor_hash if user is None else "", ip_hash if user is None else "")
    if remaining <= 0:
        return _refuse("You have used today's questions.", spent_code, status.HTTP_429_TOO_MANY_REQUESTS,
                       limit=limit, remaining=0)
    identity = f"u{user.pk}" if user is not None else f"v{visitor_hash[:24]}"
    if _rate_limited("burst", identity, int(_setting("HELP_BOT_BURST_PER_MINUTE", 6)), 60):
        return _refuse("Slow down a little, then ask again.", "help_slow_down", status.HTTP_429_TOO_MANY_REQUESTS)

    # Turnstile, before anything is written: starting a signed-out conversation costs a model call.
    if user is None and conv is None:
        refused = require_human(request, where="help_chat")
        if refused is not None:
            return refused

    history = []
    if conv is not None:
        earlier = list(conv.messages.order_by("-created_at", "-id")[:HISTORY_TURNS])
        history = [{"role": m.role, "content": m.body} for m in reversed(earlier) if m.body.strip()]
    history.append({"role": HelpMessage.ROLE_USER, "content": message})
    facts = account_facts(user) if user is not None else None

    with _InFlight() as slot:
        if not slot.entered:
            return _refuse("The assistant is busy. Try again in a few seconds.", "help_busy",
                           status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            answer = brain.ask(messages=history, facts=facts, locale=_locale(request), signed_in=user is not None)
        except brain.BrainOffline:
            return _refuse("The assistant is offline right now.", "help_ai_offline", status.HTTP_503_SERVICE_UNAVAILABLE)
        except brain.BrainTimeout:
            return _refuse("The assistant took too long. Try again.", "help_ai_timeout", status.HTTP_504_GATEWAY_TIMEOUT)
        except brain.BrainError:
            return _refuse("The assistant could not answer. Try again.", "help_ai_failed", status.HTTP_502_BAD_GATEWAY)

    with transaction.atomic():
        if conv is None:
            conv = HelpConversation.objects.create(
                user=user, visitor_hash=visitor_hash if user is None else "",
                ip_hash=ip_hash if user is None else "", locale=_locale(request),
            )
        HelpMessage.objects.create(conversation=conv, role=HelpMessage.ROLE_USER, body=message)
        HelpMessage.objects.create(conversation=conv, role=HelpMessage.ROLE_ASSISTANT, body=answer["reply"],
                                   used_account=answer["used_account"] and user is not None)
        conv.last_message_at = timezone.now()
        conv.save(update_fields=["last_message_at"])

    return Response({
        "conversation": conv.public_token,
        "reply": answer["reply"],
        "needs_person": answer["needs_person"],
        # Only a signed-in person has account facts; a model that claims otherwise is ignored.
        "used_account": answer["used_account"] and user is not None,
        "needs_sign_in": answer["needs_sign_in"] and user is None,
        "limit": limit,
        "remaining": max(0, remaining - 1),
    })


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §7  POST help-bot/handoff/
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _transcript(conv, name_label, after=None):
    """The chat as plain text for a ticket, oldest first. `after` limits it to what came later."""
    rows = conv.messages.all()
    if after is not None:
        rows = rows.filter(created_at__gt=after)
    rows = list(rows.order_by("-created_at", "-id")[:TRANSCRIPT_TURNS])
    rows.reverse()
    return "\n\n".join(
        f"{name_label if m.role == HelpMessage.ROLE_USER else 'AFC Help'}: {m.body}" for m in rows if m.body.strip()
    )


def _help_handoff(request):
    """POST help-bot/handoff/ - "Talk to a person" (logged by the help_handoff wrapper in §8).

    REQUEST   JSON {conversation?, visitor?, email?, message?, cf_turnstile_response?}
              conversation  the chat to attach (h_<24 hex)
              email         signed out only: where the support team replies
              message       what they need help with; required when there is no conversation
              cf_turnstile_response  required when signed out
    RESPONSE  200 {ticket_number, ticket_url, existing}
              existing is true when this chat already had a ticket: the newer turns are added to it
              instead of opening a second one.
              400 help_email_invalid | help_handoff_empty | help_conversation_invalid | bot_check_failed
              404 help_conversation_not_found     429 help_handoff_limit
    AUTH      optional Bearer SessionToken. Signed in, the ticket uses the account's name and email.
    CONSUMED  components/help/HelpBot.tsx (lib/api/helpBot.ts handOffToPerson).
    """
    data = request.data if hasattr(request.data, "get") else {}
    user = _actor(request)
    if user is None:
        # FIRST, before anything is written or emailed: a ticket sends mail to the team and the person.
        refused = require_human(request, where="help_handoff")
        if refused is not None:
            return refused

    token = _validate_token(data.get("conversation"))
    if token is None:
        return _refuse("That conversation reference is not valid.", "help_conversation_invalid",
                       status.HTTP_400_BAD_REQUEST)
    visitor = _validate_visitor(data.get("visitor"))
    visitor_hash = _hash("visitor", visitor) if visitor else ""

    if user is not None:
        email, name, label = user.email, (user.full_name or user.username), "Player"
    else:
        email = _validate_text(data.get("email"), 254)
        if not email or not EMAIL_RE.match(email):
            return _refuse("Please enter an email address we can reply to.", "help_email_invalid",
                           status.HTTP_400_BAD_REQUEST)
        account = User.objects.filter(email__iexact=email).order_by("user_id").first()
        name, label = (account.username if account else ""), "Visitor"

    conv = None
    if token:
        conv = _owned_conversation(token, user, visitor_hash)
        if conv is None:
            return _refuse("That conversation was not found. Start a new one.", "help_conversation_not_found",
                           status.HTTP_404_NOT_FOUND)
    extra = _validate_text(data.get("message"), MAX_HANDOFF_CHARS) or ""
    if (conv is None or not conv.messages.exists()) and not extra:
        return _refuse("Tell us what you need help with.", "help_handoff_empty", status.HTTP_400_BAD_REQUEST)

    identity = f"u{user.pk}" if user is not None else _hash("ip", _client_ip(request))[:24]
    if _rate_limited("handoff", identity, HANDOFFS_PER_HOUR, 3600):
        return _refuse("You have opened several tickets in the last hour. A person will reply to those.",
                       "help_handoff_limit", status.HTTP_429_TOO_MANY_REQUESTS)

    # The same chat handed over twice: one ticket, with whatever was said since added to it.
    if conv is not None and conv.ticket_id:
        ticket = conv.ticket
        newer = _transcript(conv, label, after=conv.handed_off_at)
        added = "\n\n".join(p for p in (extra, newer) if p)
        if added:
            SupportMessage.objects.create(
                ticket=ticket, direction=SupportMessage.DIRECTION_IN, channel=SupportMessage.CHANNEL_WEB,
                author=user, author_name=ticket.name, body="More from the Help panel:\n\n" + added,
            )
            ticket.last_message_at = timezone.now()
            if ticket.status in (SupportTicket.STATUS_WAITING, SupportTicket.STATUS_RESOLVED):
                ticket.status = SupportTicket.STATUS_OPEN
            ticket.save(update_fields=["last_message_at", "status", "updated_at"])
            conv.handed_off_at = timezone.now()
            conv.save(update_fields=["handed_off_at"])
        return Response({"ticket_number": ticket.ticket_number, "ticket_url": notify.ticket_url(ticket),
                         "existing": True})

    parts = ["Opened from the Help panel on the website."]
    first_question = ""
    if conv is not None:
        first_question = (conv.messages.filter(role=HelpMessage.ROLE_USER).order_by("created_at", "id")
                          .values_list("body", flat=True).first() or "")
        parts.append("The conversation with the AFC Help assistant:\n\n" + _transcript(conv, label))
    if extra:
        parts.insert(1, extra)
    body = "\n\n".join(parts)

    with transaction.atomic():
        ticket, message, _rejected = create_ticket_from_contact(
            name, email, body, request_user=user, source=SupportTicket.SOURCE_HELP_BOT,
            subject=first_question or extra,
        )
        if conv is not None:
            conv.ticket = ticket
            conv.handed_off_at = timezone.now()
            conv.save(update_fields=["ticket", "handed_off_at"])
    acknowledge_new_ticket(ticket, message)
    return Response({"ticket_number": ticket.ticket_number, "ticket_url": notify.ticket_url(ticket),
                     "existing": False})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §8  The input log (inbox #156) and the two public doors that write it
# ─────────────────────────────────────────────────────────────────────────────────────────────────
LOG_TEXT_CHARS = 4000
PAGE_RE = re.compile(r"^/[^\s]{0,299}$")


def _log_input(request, kind, response):
    """One HelpInputLog row for a chat or handoff request, AFTER its response is decided: who (the
    account, or a signed-out visitor's salted hashes), when, the page they were on, what they typed
    and what came back (the answer, the ticket number, or the refusal code). It never changes the
    response, and a failure to write it is logged and swallowed, so the panel cannot break on it."""
    try:
        data = request.data if hasattr(request.data, "get") else {}
        raw = data.get("message")
        text = raw[:LOG_TEXT_CHARS] if isinstance(raw, str) else ""
        user = _actor(request)
        visitor = _validate_visitor(data.get("visitor"))
        body = response.data if isinstance(getattr(response, "data", None), dict) else {}
        answer, outcome, token = "", "", ""
        if response.status_code == 200 and kind == HelpInputLog.KIND_QUESTION:
            answer, outcome, token = str(body.get("reply") or ""), HelpInputLog.OUTCOME_ANSWERED, str(body.get("conversation") or "")
        elif response.status_code == 200:
            outcome = f"ticket:{body.get('ticket_number', '')}" + (" (added)" if body.get("existing") else "")
        else:
            outcome = str(body.get("code") or f"http_{response.status_code}")
        if not token:
            token = _validate_token(data.get("conversation")) or ""
        page = data.get("page")
        HelpInputLog.objects.create(
            kind=kind, user=user, username=(user.username if user else ""),
            visitor_hash=_hash("visitor", visitor) if (visitor and user is None) else "",
            ip_hash=_hash("ip", _client_ip(request)) if user is None else "",
            conversation_token=token[:32], text=text, answer=answer[:LOG_TEXT_CHARS], outcome=outcome[:60],
            http_status=response.status_code, locale=_locale(request),
            page=page if isinstance(page, str) and PAGE_RE.match(page) else "",
        )
    except Exception:
        log.exception("help bot: could not write the input log")


@api_view(["POST"])
def help_chat(request):
    """POST help-bot/chat/: see _help_chat for the contract. Every call is logged (§8)."""
    response = _help_chat(request)
    _log_input(request, HelpInputLog.KIND_QUESTION, response)
    return response


@api_view(["POST"])
def help_handoff(request):
    """POST help-bot/handoff/: see _help_handoff for the contract. Every call is logged (§8)."""
    response = _help_handoff(request)
    _log_input(request, HelpInputLog.KIND_HANDOFF, response)
    return response


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §9  Your own conversations (inbox #147)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Owner, 2026-10-05: "conversations should survive a sign out and sign in back, aso creating a new
# conversation should not limit the last one." The chats were always stored (models.py); the panel
# simply had no way to read them back, so signing out or the "new chat" button lost them on screen.
LIST_LIMIT_DEFAULT = 20
LIST_LIMIT_MAX = 50


def _conversations_of(user, visitor_hash):
    """The caller's conversations, the same ownership as _owned_conversation: an account's own, or,
    signed out, this browser's that have no account."""
    if user is not None:
        return HelpConversation.objects.filter(user=user)
    if visitor_hash:
        return HelpConversation.objects.filter(user__isnull=True, visitor_hash=visitor_hash)
    return HelpConversation.objects.none()


def _int_param(request, name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.query_params.get(name, default))))
    except (TypeError, ValueError):
        return default


@api_view(["GET"])
def help_conversations(request):
    """GET help-bot/conversations/?visitor=<browser id>&limit=20&offset=0

    The caller's earlier conversations, newest first, for the panel's History view.
    REQUEST   optional Bearer SessionToken; signed out, ?visitor= the panel's browser id.
    RESPONSE  200 {results: [{conversation, started_at, last_message_at, preview, questions}],
                   total_count, has_more, next_offset}
              preview is the first question (up to 120 characters).
    AUTH      none beyond ownership: an account sees its own, a browser its own signed-out chats.
    CONSUMED  components/help/HelpBot.tsx (lib/api/helpBot.ts listHelpConversations).
    """
    user = _actor(request)
    visitor = _validate_visitor(request.query_params.get("visitor"))
    visitor_hash = _hash("visitor", visitor) if (visitor and user is None) else ""
    limit = _int_param(request, "limit", LIST_LIMIT_DEFAULT, 1, LIST_LIMIT_MAX)
    offset = _int_param(request, "offset", 0, 0, 100000)
    first_question = (HelpMessage.objects.filter(conversation=OuterRef("pk"), role=HelpMessage.ROLE_USER)
                      .order_by("created_at", "id").values("body")[:1])
    qs = (_conversations_of(user, visitor_hash)
          .annotate(preview=Subquery(first_question),
                    questions=Count("messages", filter=Q(messages__role=HelpMessage.ROLE_USER)))
          .filter(questions__gt=0)
          .order_by("-last_message_at", "-id"))
    total = qs.count()
    rows = list(qs[offset:offset + limit])
    more = offset + len(rows) < total
    return Response({
        "results": [{
            "conversation": c.public_token,
            "started_at": c.created_at.isoformat(),
            "last_message_at": c.last_message_at.isoformat(),
            "preview": (c.preview or "")[:120],
            "questions": c.questions,
        } for c in rows],
        "total_count": total,
        "has_more": more,
        "next_offset": offset + len(rows) if more else None,
    })


@api_view(["GET"])
def help_conversation(request, token):
    """GET help-bot/conversations/<token>/?visitor=<browser id>

    One of the caller's conversations with its messages, oldest first, to reopen it in the panel.
    RESPONSE  200 {conversation, started_at, ticket_number, messages: [{role, text, used_account, at}]}
              404 help_conversation_not_found   not theirs, or gone: the same answer for both (R88)
    AUTH      ownership (_owned_conversation), exactly as continuing the chat.
    CONSUMED  components/help/HelpBot.tsx (lib/api/helpBot.ts getHelpConversation).
    """
    user = _actor(request)
    visitor = _validate_visitor(request.query_params.get("visitor"))
    visitor_hash = _hash("visitor", visitor) if visitor else ""
    conv = _owned_conversation(token, user, visitor_hash) if TOKEN_RE.match(token or "") else None
    if conv is None:
        return _refuse("That conversation was not found. Start a new one.", "help_conversation_not_found",
                       status.HTTP_404_NOT_FOUND)
    return Response({
        "conversation": conv.public_token,
        "started_at": conv.created_at.isoformat(),
        "ticket_number": conv.ticket.ticket_number if conv.ticket_id else None,
        "messages": [{
            "role": m.role, "text": m.body, "used_account": m.used_account, "at": m.created_at.isoformat(),
        } for m in conv.messages.order_by("created_at", "id")],
    })


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §10  The staff Help log (inbox #156)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def help_admin_log(request):
    """GET help-bot/admin/log/?q=&outcome=&kind=&who=&limit=50&offset=0

    Every input to the Help panel, newest first, for support staff (support admins, head admins,
    super admins: afc_support.views._require_staff, the same people who work the support desk).
    FILTERS   q        words in what was typed or answered
              outcome  "answered", "refused" (any refusal code), "ticket", or one exact code
              kind     question | handoff
              who      an account's in-game name (exact), or "visitors" for signed-out only
    RESPONSE  200 {results: [{id, at, kind, who, signed_in, visitor, conversation, page, locale,
                   text, answer, outcome, http_status}], total_count, has_more, next_offset}
              `visitor` is the first 8 characters of the salted browser hash: enough to see that two
              signed-out questions came from the same browser, never the browser id or an address.
              401 auth_required, 403 support_forbidden
    CONSUMED  frontend app/(a)/a/support/help-log/page.tsx.
    """
    from afc_support.views import _require_staff

    _staff, refused = _require_staff(request)
    if refused is not None:
        return refused
    qs = HelpInputLog.objects.all()
    q = (request.query_params.get("q") or "").strip()[:100]
    if q:
        qs = qs.filter(Q(text__icontains=q) | Q(answer__icontains=q))
    outcome = (request.query_params.get("outcome") or "").strip()[:60]
    if outcome == "refused":
        qs = qs.exclude(outcome=HelpInputLog.OUTCOME_ANSWERED).exclude(outcome__startswith="ticket:")
    elif outcome == "ticket":
        qs = qs.filter(outcome__startswith="ticket:")
    elif outcome:
        qs = qs.filter(outcome=outcome)
    kind = request.query_params.get("kind")
    if kind in (HelpInputLog.KIND_QUESTION, HelpInputLog.KIND_HANDOFF):
        qs = qs.filter(kind=kind)
    who = (request.query_params.get("who") or "").strip()[:150]
    if who == "visitors":
        qs = qs.filter(username="")
    elif who:
        qs = qs.filter(username__iexact=who)
    limit = _int_param(request, "limit", 50, 1, 100)
    offset = _int_param(request, "offset", 0, 0, 1000000)
    total = qs.count()
    rows = list(qs.order_by("-created_at", "-id")[offset:offset + limit])
    more = offset + len(rows) < total
    return Response({
        "results": [{
            "id": r.pk, "at": r.created_at.isoformat(), "kind": r.kind,
            "who": r.username or None, "signed_in": bool(r.username),
            "visitor": r.visitor_hash[:8] if r.visitor_hash else None,
            "conversation": r.conversation_token or None, "page": r.page or None, "locale": r.locale,
            "text": r.text, "answer": r.answer, "outcome": r.outcome, "http_status": r.http_status,
        } for r in rows],
        "total_count": total,
        "has_more": more,
        "next_offset": offset + len(rows) if more else None,
    })
