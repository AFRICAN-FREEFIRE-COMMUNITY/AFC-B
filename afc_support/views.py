"""
afc_support/views.py - the support desk: the public form, the requester's thread, the staff
dashboard, and the head-admin audit.

THE ENDPOINTS, AND WHO OPENS EACH

  PUBLIC (no token at all, because somebody locked out of their account must still reach us)
    POST support/contact/                 the Contact Us form. Creates the ticket, stores the
                                          message and any files, emails the acknowledgement and
                                          DMs Discord when we can match an account.
                                          Consumed by frontend app/(root)/contact.
    GET  support/t/<token>/               the requester's own thread, addressed by the opaque token
                                          in their email (R22). Consumed by app/(root)/support/t/[token].
    POST support/t/<token>/reply/         they answer, with files. Same page.

  STAFF (Bearer SessionToken; support role, see _is_support_staff)
    GET  support/tickets/                 the queue, filterable, paginated with the house envelope.
    GET  support/tickets/<number>/        one conversation in full.
    POST support/tickets/<number>/reply/  answer, which emails and DMs the person.
    POST support/tickets/<number>/status/ set the status or assign it to somebody.

  HEAD ADMIN ONLY
    GET  support/audit/                   every message and every attachment ever, with full
                                          timestamps. Owner: "give the support page its own audit
                                          page, which only head admins and above can see, they see
                                          all messages and every attached, the full history of date
                                          and time and al that too."

  EITHER (staff, or the ticket's own token)
    GET  support/attachments/<id>/        streams one file. Support attachments carry ID cards and
                                          payment screenshots, so they are NOT served from a public
                                          media URL; this view is the only way to read one.

STAFF ACTIONS ARE AUDITED AUTOMATICALLY. Every POST here by a signed-in admin is picked up by
afc_auth.middleware.AuditLogMiddleware and lands on the sitewide History page, which is the owner's
"add this to the general audit page" without a second audit system to keep in step.
"""
import mimetypes
import os

from django.db.models import Count, Max, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from afc_auth.bot_protection import require_human
from django.http import FileResponse, Http404

from afc_auth.models import User
from afc_auth.views import canonical_profile, header_safe, validate_token
from afc_support.models import SupportAttachment, SupportMessage, SupportTicket
from afc_support import notify

# ── Upload limits ────────────────────────────────────────────────────────────────────────────────
# The owner asked for "documents, pictures videos". Video is why the per-file cap is generous; the
# per-message count keeps one synchronous upload bounded. Both are deliberate, small numbers rather
# than a global Django setting, because other endpoints (shop, design assets) have their own.
MAX_FILES_PER_MESSAGE = 6
# 20 MB per file. The ceiling is nginx, not us: the API server allows a 25M body
# (deploy/vps nginx `client_max_body_size 25M`), and a file bigger than that is refused with a 413
# before Django ever runs, which would look like a broken form. Raise nginx first if this grows.
MAX_FILE_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    # pictures
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp",
    # video
    ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv",
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".rtf", ".odt", ".zip",
}

# Roles that may work the desk. "support" has existed as a coarse User.role since before this app;
# "support_admin" is the granular role the owner asked for, so somebody can be given the desk and
# nothing else.
_SUPPORT_COARSE_ROLES = ("admin", "moderator", "support")
_SUPPORT_GRANULAR_ROLES = ("super_admin", "head_admin", "support_admin")
_HEAD_GRANULAR_ROLES = ("super_admin", "head_admin")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  Who is asking
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _actor(request):
    """The signed-in user behind a Bearer SessionToken, or None. Mirrors the pattern every other
    module here uses (afc_auth.views.validate_token)."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ", 1)[1].strip())


def _is_support_staff(user) -> bool:
    """May this person read and answer tickets?"""
    if not user:
        return False
    if (getattr(user, "role", "") or "").lower() in _SUPPORT_COARSE_ROLES:
        return True
    try:
        return user.userroles.filter(role__role_name__in=_SUPPORT_GRANULAR_ROLES).exists()
    except Exception:
        return False


def _is_head_admin(user) -> bool:
    """May this person open the support audit? Head admins and above, exactly as asked."""
    if not user:
        return False
    if getattr(user, "is_superuser", False):
        return True
    try:
        return user.userroles.filter(role__role_name__in=_HEAD_GRANULAR_ROLES).exists()
    except Exception:
        return False


def _require_staff(request):
    """(user, None) when they may work the desk, else (None, Response)."""
    user = _actor(request)
    if not user:
        return None, Response({"message": "Authorization header is required.",
                               "code": "auth_required"},
                              status=status.HTTP_401_UNAUTHORIZED)
    if not _is_support_staff(user):
        return None, Response({"message": "You do not have access to the support desk.",
                               "code": "support_forbidden"},
                              status=status.HTTP_403_FORBIDDEN)
    return user, None


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Shapes (one serializer per shape, R24: the dashboard, the public thread and the audit all
#     read the SAME dicts, so a field added here appears everywhere the same day)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _attachment_dict(att):
    return {
        "id": att.id,
        "name": att.original_name,
        "content_type": att.content_type,
        "size_bytes": att.size_bytes,
        # Always through the view, never the raw media path: these files are private.
        "url": f"/support/attachments/{att.id}/",
    }


def _message_dict(msg, for_staff=False):
    data = {
        "id": msg.id,
        "direction": msg.direction,
        "channel": msg.channel,
        "body": msg.body,
        "author_name": msg.author_name or ("AFC Support" if msg.direction == "out" else ""),
        "created_at": msg.created_at.isoformat(),
        "attachments": [_attachment_dict(a) for a in msg.attachments.all()],
    }
    if for_staff:
        # Who on the team wrote it. Never sent to the requester: they see "AFC Support".
        data["author_username"] = msg.author.username if msg.author_id else ""
    return data


def _ticket_dict(ticket, for_staff=False, with_messages=False):
    data = {
        "ticket_number": ticket.ticket_number,
        "name": ticket.name,
        "email": ticket.email,
        "subject": ticket.subject,
        "status": ticket.status,
        "source": ticket.source,
        "created_at": ticket.created_at.isoformat(),
        "last_message_at": (ticket.last_message_at or ticket.created_at).isoformat(),
    }
    if for_staff:
        data.update({
            "user_id": ticket.user_id,
            "username": ticket.user.username if ticket.user_id else "",
            "has_discord": bool(ticket.discord_id),
            "assigned_to": ticket.assigned_to.username if ticket.assigned_to_id else "",
        })
    if with_messages:
        data["messages"] = [
            _message_dict(m, for_staff=for_staff)
            for m in ticket.messages.all().prefetch_related("attachments")
        ]
    return data


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Storing what somebody sent
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _save_attachments(message, files):
    """Store the uploaded files on `message`. Returns (saved, rejected) where rejected names the
    files we refused and why, so the caller can tell the person instead of silently dropping them.

    A file is refused for its EXTENSION, not its declared content type: a browser will happily send
    application/octet-stream for a video, and the type a client claims is not evidence anyway.
    """
    saved, rejected = [], []
    for f in files[:MAX_FILES_PER_MESSAGE]:
        name = getattr(f, "name", "") or "file"
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            rejected.append({"name": name, "reason": "type"})
            continue
        if getattr(f, "size", 0) > MAX_FILE_BYTES:
            rejected.append({"name": name, "reason": "size"})
            continue
        att = SupportAttachment.objects.create(
            message=message,
            file=f,
            original_name=name[:255],
            content_type=(getattr(f, "content_type", "") or
                          mimetypes.guess_type(name)[0] or "")[:120],
            size_bytes=getattr(f, "size", 0) or 0,
        )
        saved.append(att)
    if len(files) > MAX_FILES_PER_MESSAGE:
        for f in files[MAX_FILES_PER_MESSAGE:]:
            rejected.append({"name": getattr(f, "name", "file"), "reason": "count"})
    return saved, rejected


def _touch(ticket, when=None):
    ticket.last_message_at = when or timezone.now()
    ticket.save(update_fields=["last_message_at", "updated_at"])


def create_ticket_from_contact(name, email, body, files=None, request_user=None):
    """The one place a contact-form ticket is born.

    Called by support_contact below AND by the legacy afc_auth.contact_us, so a post to either
    address is stored the same way. Returns (ticket, message, rejected_files).

    Matching an account: by EMAIL, case-insensitively, preferring the canonical profile's user. That
    is what lets the acknowledgement go out in their language and the Discord DM find them.
    """
    account = None
    if email:
        account = User.objects.filter(email__iexact=email).order_by("user_id").first()
    if request_user and not account:
        account = request_user

    discord_id = ""
    if account is not None:
        discord_id = (getattr(account, "discord_id", "") or "")

    ticket = SupportTicket.objects.create(
        name=header_safe(name, limit=120) or "Someone",
        email=email,
        user=account,
        discord_id=discord_id,
        subject=header_safe(body, limit=80),
        source=SupportTicket.SOURCE_CONTACT_FORM,
    )
    message = SupportMessage.objects.create(
        ticket=ticket,
        direction=SupportMessage.DIRECTION_IN,
        channel=SupportMessage.CHANNEL_WEB,
        author=account,
        author_name=ticket.name,
        body=body,
    )
    _saved, rejected = _save_attachments(message, files or [])
    _touch(ticket, message.created_at)
    return ticket, message, rejected


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  Public: the contact form
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def support_contact(request):
    """POST support/contact/ - the Contact Us form, with attachments.

    REQUEST   multipart: name, email, message, files[] (repeatable, up to 6, 20 MB each)
    RESPONSE  200 { message, ticket_number, ticket_url, rejected_files[] }
              400 { message, code } missing fields or a malformed address
    AUTH      none. Somebody locked out of their account has to be able to reach us.
    CONSUMED  frontend app/(root)/contact.

    The ticket is SAVED FIRST and notified second, deliberately: the email and the Discord DM are
    both allowed to fail without losing what the person wrote. That is the whole reason this app
    exists (see afc_support/models.py).
    """
    # Bot protection (owner 2026-09-22). FIRST, before anything is written or emailed:
    # this form is open to the whole internet and a script filling it costs a queue, a
    # database row and mail quota. Verified server side against Cloudflare Turnstile; with
    # no key configured it allows the request and the checker counts that as debt.
    refused = require_human(request, where="support_contact")
    if refused is not None:
        return refused

    import re

    name = (request.data.get("name") or "").strip()
    email = (request.data.get("email") or "").strip()
    body = (request.data.get("message") or "").strip()

    if not all([name, email, body]):
        return Response({"message": "Email, name, and message are required.",
                         "code": "contact_fields_required"},
                        status=status.HTTP_400_BAD_REQUEST)
    if not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email):
        return Response({"message": "Please enter an email address we can reply to.",
                         "code": "contact_email_invalid"},
                        status=status.HTTP_400_BAD_REQUEST)

    files = request.FILES.getlist("files")
    ticket, message, rejected = create_ticket_from_contact(
        name, email, body, files, request_user=_actor(request))

    # Notify, never fail. Both results are recorded as an automatic OUT message so the audit shows
    # what left the building.
    lang = (getattr(ticket.user, "language", "") or "en") if ticket.user_id else "en"
    emailed = notify.email_ticket_received(ticket, lang=lang)
    dmed = notify.dm_ticket_received(ticket)
    # And the support inbox, where the team already looks.
    notify.email_staff_new_ticket(ticket, message, attachment_count=message.attachments.count())
    SupportMessage.objects.create(
        ticket=ticket,
        direction=SupportMessage.DIRECTION_OUT,
        channel=SupportMessage.CHANNEL_AUTO,
        author_name="AFC",
        body=("Acknowledgement sent: "
              f"email {'delivered' if emailed else 'not delivered'}, "
              f"Discord DM {'delivered' if dmed else ('not delivered' if ticket.discord_id else 'no Discord on file')}."),
    )

    return Response(
        {
            "message": "Your message has been sent successfully.",
            "ticket_number": ticket.ticket_number,
            "ticket_url": notify.ticket_url(ticket),
            "rejected_files": rejected,
        },
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5  Public: the requester's own thread
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _ticket_by_token(token):
    return SupportTicket.objects.filter(public_token=(token or "").strip()).first()


@api_view(["GET"])
def support_thread(request, token):
    """GET support/t/<token>/ - the conversation, for the person who wrote it.

    The token IS the credential, so nothing here leaks who at AFC answered: author_name reads
    "AFC Support". Consumed by frontend app/(root)/support/t/[token].
    """
    ticket = _ticket_by_token(token)
    if not ticket:
        return Response({"message": "We could not find that ticket.", "code": "ticket_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(_ticket_dict(ticket, for_staff=False, with_messages=True),
                    status=status.HTTP_200_OK)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def support_thread_reply(request, token):
    """POST support/t/<token>/reply/ - the person adds to their own ticket, with files.

    This is the "see replies from people there also" half: their answer becomes an IN message, the
    dashboard shows it in the same thread, and the ticket reopens if it had been resolved.
    """
    ticket = _ticket_by_token(token)
    if not ticket:
        return Response({"message": "We could not find that ticket.", "code": "ticket_not_found"},
                        status=status.HTTP_404_NOT_FOUND)

    body = (request.data.get("message") or "").strip()
    files = request.FILES.getlist("files")
    if not body and not files:
        return Response({"message": "Write a message or attach a file.",
                         "code": "reply_empty"},
                        status=status.HTTP_400_BAD_REQUEST)

    message = SupportMessage.objects.create(
        ticket=ticket,
        direction=SupportMessage.DIRECTION_IN,
        channel=SupportMessage.CHANNEL_WEB,
        author=ticket.user,
        author_name=ticket.name,
        body=body or "(files attached)",
    )
    _saved, rejected = _save_attachments(message, files)
    # Somebody answering a resolved ticket is reopening it, whatever the label said.
    if ticket.status in (SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED,
                         SupportTicket.STATUS_WAITING):
        ticket.status = SupportTicket.STATUS_OPEN
        ticket.save(update_fields=["status", "updated_at"])
    _touch(ticket, message.created_at)

    return Response({"message": "Your reply has been added.",
                     "rejected_files": rejected,
                     "ticket": _ticket_dict(ticket, for_staff=False, with_messages=True)},
                    status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6  Staff: the queue and one conversation
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def support_tickets(request):
    """GET support/tickets/ - the queue.

    Query: q (name / email / ticket number / message body), status, assigned ("me"), limit, offset.
    Returns the house envelope {results, has_more, next_offset, total_count}, same as the audit log
    and the partner API, so the frontend table pages exactly like the others.
    """
    user, err = _require_staff(request)
    if err:
        return err

    qs = SupportTicket.objects.all().select_related("user", "assigned_to")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(email__icontains=q) | Q(ticket_number__icontains=q)
            | Q(subject__icontains=q) | Q(messages__body__icontains=q)
        ).distinct()
    state = (request.GET.get("status") or "").strip()
    if state:
        qs = qs.filter(status=state)
    if (request.GET.get("assigned") or "") == "me":
        qs = qs.filter(assigned_to=user)

    try:
        limit = max(1, min(int(request.GET.get("limit", 25)), 100))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    total = qs.count()
    rows = list(qs[offset:offset + limit])
    counts = dict(
        SupportTicket.objects.values_list("status").annotate(n=Count("id"))
    )
    return Response(
        {
            "results": [_ticket_dict(t, for_staff=True) for t in rows],
            "has_more": offset + len(rows) < total,
            "next_offset": offset + len(rows),
            "total_count": total,
            # The queue badge: how many are waiting on us right now.
            "open_count": counts.get(SupportTicket.STATUS_OPEN, 0),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
def support_ticket_detail(request, number):
    """GET support/tickets/<ticket_number>/ - one conversation in full, for staff."""
    user, err = _require_staff(request)
    if err:
        return err
    ticket = SupportTicket.objects.filter(ticket_number=number).select_related(
        "user", "assigned_to").first()
    if not ticket:
        return Response({"message": "We could not find that ticket.", "code": "ticket_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    data = _ticket_dict(ticket, for_staff=True, with_messages=True)
    data["ticket_url"] = notify.ticket_url(ticket)
    return Response(data, status=status.HTTP_200_OK)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def support_ticket_reply(request, number):
    """POST support/tickets/<ticket_number>/reply/ - a human at AFC answers.

    Emails the person and DMs them on Discord when we have it. The ticket moves to "waiting on the
    sender" unless the caller says otherwise, because a queue where everything stays open is a queue
    nobody can read.
    """
    user, err = _require_staff(request)
    if err:
        return err
    ticket = SupportTicket.objects.filter(ticket_number=number).first()
    if not ticket:
        return Response({"message": "We could not find that ticket.", "code": "ticket_not_found"},
                        status=status.HTTP_404_NOT_FOUND)

    body = (request.data.get("message") or "").strip()
    if not body:
        return Response({"message": "Write a reply first.", "code": "reply_empty"},
                        status=status.HTTP_400_BAD_REQUEST)

    message = SupportMessage.objects.create(
        ticket=ticket,
        direction=SupportMessage.DIRECTION_OUT,
        channel=SupportMessage.CHANNEL_WEB,
        author=user,
        author_name="AFC Support",
        body=body,
    )
    _saved, rejected = _save_attachments(message, request.FILES.getlist("files"))

    lang = (getattr(ticket.user, "language", "") or "en") if ticket.user_id else "en"
    emailed = notify.email_ticket_reply(ticket, message, lang=lang)
    dmed = notify.dm_ticket_reply(ticket, message)

    new_status = (request.data.get("status") or SupportTicket.STATUS_WAITING).strip()
    if new_status in dict(SupportTicket.STATUS_CHOICES):
        ticket.status = new_status
    if not ticket.assigned_to_id:
        ticket.assigned_to = user
    ticket.save(update_fields=["status", "assigned_to", "updated_at"])
    _touch(ticket, message.created_at)

    return Response({"message": "Reply sent.",
                     "emailed": bool(emailed),
                     "discord_dm": bool(dmed),
                     "rejected_files": rejected,
                     "ticket": _ticket_dict(ticket, for_staff=True, with_messages=True)},
                    status=status.HTTP_200_OK)


@api_view(["POST"])
def support_ticket_status(request, number):
    """POST support/tickets/<ticket_number>/status/ - { status?, assign_to_me? }."""
    user, err = _require_staff(request)
    if err:
        return err
    ticket = SupportTicket.objects.filter(ticket_number=number).first()
    if not ticket:
        return Response({"message": "We could not find that ticket.", "code": "ticket_not_found"},
                        status=status.HTTP_404_NOT_FOUND)

    changed = []
    new_status = (request.data.get("status") or "").strip()
    if new_status:
        if new_status not in dict(SupportTicket.STATUS_CHOICES):
            return Response({"message": "That is not a ticket status.", "code": "bad_status"},
                            status=status.HTTP_400_BAD_REQUEST)
        ticket.status = new_status
        changed.append("status")
    if request.data.get("assign_to_me"):
        ticket.assigned_to = user
        changed.append("assigned_to")
    if not changed:
        return Response({"message": "Nothing to change.", "code": "no_change"},
                        status=status.HTTP_400_BAD_REQUEST)
    ticket.save(update_fields=changed + ["updated_at"])
    return Response({"message": "Ticket updated.",
                     "ticket": _ticket_dict(ticket, for_staff=True)},
                    status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §7  Attachments
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def support_attachment(request, attachment_id):
    """GET support/attachments/<id>/ - stream one file.

    Two ways in, and no third: a support-role session, or the ticket's own opaque token passed as
    ?t=<token> (the requester's thread page appends it). Everything else is a 404, not a 403: a
    stranger should not even learn that the id exists.
    """
    att = SupportAttachment.objects.filter(id=attachment_id).select_related(
        "message__ticket").first()
    if not att:
        raise Http404

    ticket = att.message.ticket
    token = (request.GET.get("t") or "").strip()
    if not (_is_support_staff(_actor(request)) or (token and token == ticket.public_token)):
        raise Http404

    try:
        handle = att.file.open("rb")
    except Exception:
        raise Http404
    response = FileResponse(handle, content_type=att.content_type or "application/octet-stream")
    # inline so a picture opens in the tab and a document offers itself, with the ORIGINAL name.
    response["Content-Disposition"] = f'inline; filename="{header_safe(att.original_name, 200)}"'
    return response


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §8  The support audit (head admins and above)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def support_audit(request):
    """GET support/audit/ - every message and every attachment, newest first.

    Owner 2026-09-14: "give the support page its own audit page, which only head admins and above
    can see, they see all messages and every attached, the full history of date and time and al that
    too." So this is the MESSAGE stream, not the ticket list: one row per message, with who wrote
    it, which ticket it belongs to, the full text, every attached file, and the exact timestamp.

    Query: q (body / ticket number / email), direction (in|out), date_from, date_to, limit, offset.
    """
    user = _actor(request)
    if not user:
        return Response({"message": "Authorization header is required.", "code": "auth_required"},
                        status=status.HTTP_401_UNAUTHORIZED)
    if not _is_head_admin(user):
        return Response({"message": "Only head admins can open the support audit.",
                         "code": "support_audit_forbidden"},
                        status=status.HTTP_403_FORBIDDEN)

    qs = (SupportMessage.objects.all()
          .select_related("ticket", "author")
          .prefetch_related("attachments")
          .order_by("-created_at", "-id"))

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(body__icontains=q) | Q(ticket__ticket_number__icontains=q)
                       | Q(ticket__email__icontains=q) | Q(ticket__name__icontains=q))
    direction = (request.GET.get("direction") or "").strip()
    if direction in ("in", "out"):
        qs = qs.filter(direction=direction)
    date_from = (request.GET.get("date_from") or "").strip()
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    date_to = (request.GET.get("date_to") or "").strip()
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    try:
        limit = max(1, min(int(request.GET.get("limit", 50)), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    total = qs.count()
    rows = list(qs[offset:offset + limit])
    results = []
    for m in rows:
        results.append({
            "id": m.id,
            "ticket_number": m.ticket.ticket_number,
            "ticket_status": m.ticket.status,
            "from_name": m.ticket.name,
            "from_email": m.ticket.email,
            "direction": m.direction,
            "channel": m.channel,
            "author_username": m.author.username if m.author_id else "",
            "body": m.body,
            "attachments": [_attachment_dict(a) for a in m.attachments.all()],
            "created_at": m.created_at.isoformat(),
        })
    return Response(
        {
            "results": results,
            "has_more": offset + len(rows) < total,
            "next_offset": offset + len(rows),
            "total_count": total,
            "attachment_count": SupportAttachment.objects.count(),
            "ticket_count": SupportTicket.objects.count(),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
def support_access(request):
    """GET support/access/ - what may the caller do here?

    The frontend asks this once to decide whether to draw the Support item in the admin sidebar and
    whether to offer the audit tab, instead of guessing from role names it would have to keep in
    step with this file (R26: never draw a control somebody cannot use).
    """
    user = _actor(request)
    return Response(
        {
            "can_work_tickets": _is_support_staff(user),
            "can_read_audit": _is_head_admin(user),
        },
        status=status.HTTP_200_OK,
    )
