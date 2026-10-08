"""
afc_support/views_people.py - the support desk by PERSON (inbox #169 / #174).

Owner 2026-10-08: "Support admins or admins should be able to view all messages from each user in
a single place without having to scroll, they should still be able to filter requests by dates,
time, country etc. They should be able to reply all messages together or at least reply one by
one." The approved preview is WEBSITE/mockups/support-desk-v2/support-desk-preview.html.

The ticket queue (views.support_tickets) listed TICKETS, so somebody who wrote five times was five
rows. These endpoints list PEOPLE: everything one person sent, across all their tickets, in one
answer, and a reply can be recorded on several of their requests at once.

WHO IS ONE PERSON
    A ticket linked to an account belongs to that account; a ticket sent signed out belongs to its
    email address (lower-cased). The person is addressed by an OPAQUE key, sha256 over "u:<user id>"
    or "e:<email>", so neither an email address nor a database id ever sits in a URL (owner rules
    R22 / R54). The key is resolved by recomputing it, never stored.

ENDPOINTS (the gate is _desk below: AFC support staff, or an organization's answerers)
    GET  support/people/                 the people, filtered and paged
    GET  support/people/<key>/           one person: who they are and every ticket with messages
    POST support/people/<key>/reply/     one reply recorded on several of their tickets, one email
    POST support/people/bulk-reply/      the same message to several people, each on all their
                                         open requests, each emailed on their own

HOW IT CONNECTS
    Shapes come from views._ticket_dict / _message_dict (one serializer per shape, R24), so files
    arrive as the staff signed links of inbox #168. Replies go out through afc_support.notify
    (email_ticket_reply / dm_ticket_reply with `also=`). A staff reply is an admin mutation, so the
    sitewide History page records it through afc_auth.middleware.AuditLogMiddleware.
    Consumed by frontend app/(a)/a/support/page.tsx through lib/api/support.ts.

TWO DESKS, ONE CODE (inbox #175). Every endpoint takes an optional `organization` (a slug, in the
query or the body). Without it the caller works the AFC desk: AFC support staff only, and only
tickets addressed to nobody. With it the caller works that organization's desk: its answerers and
AFC head / super admins only (afc_support/org_scope.py), and only that organization's tickets.
"""
import hashlib
from datetime import datetime

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from afc_auth.country_grouping import canonical_country, country_label
from afc_support import notify
from afc_support.models import SupportMessage, SupportTicket
from afc_support.org_scope import desk_access, is_head_or_super, reply_refusal
from afc_support.views import _actor, _is_support_staff, _save_attachments, _ticket_dict, _touch

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 120
MAX_BULK_PEOPLE = 50
SNIPPET_LENGTH = 160

# Worst first: a person shows the most urgent state among their requests.
STATUS_ORDER = [SupportTicket.STATUS_OPEN, SupportTicket.STATUS_WAITING,
                SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED]
ANSWERABLE = (SupportTicket.STATUS_OPEN, SupportTicket.STATUS_WAITING)


# ── who a ticket belongs to ────────────────────────────────────────────────────────────────────
def _identity(ticket):
    return f"u:{ticket.user_id}" if ticket.user_id else f"e:{(ticket.email or '').strip().lower()}"


def person_key(ticket):
    """The opaque address of the person a ticket belongs to (no email, no id in a URL)."""
    return hashlib.sha256(_identity(ticket).encode("utf-8")).hexdigest()[:24]


def _person_country(ticket):
    """(canonical key, label) of the person's country, or ("", "") when unknown. An account's
    profile country wins, then the country seen at sign-in, the same order the public profile
    uses; a signed-out sender has none."""
    user = ticket.user if ticket.user_id else None
    raw = ((getattr(user, "country", "") or getattr(user, "ip_country", "")) if user else "") or ""
    key = canonical_country(raw) if raw else ""
    return (key or "", country_label(key, {raw}) if key else "")


def _scoped(org):
    """The tickets of one desk: an organization's, or AFC's own (addressed to nobody)."""
    qs = SupportTicket.objects.select_related("user", "assigned_to", "organization", "event")
    return qs.filter(organization=org) if org is not None else qs.filter(organization__isnull=True)


def _desk(request):
    """(user, organization | None, error) for the desk the request is working."""
    user = _actor(request)
    slug = request.GET.get("organization") or (
        request.data.get("organization") if request.method != "GET" else "") or ""
    org, err = desk_access(request, user, _is_support_staff, slug=str(slug))
    return user, org, err


def _tickets_of(key, org):
    """Every ticket of the person `key` on this desk, newest activity first."""
    rows = _scoped(org).order_by("-last_message_at", "-created_at")
    return [t for t in rows if person_key(t) == key]


# ── filters ────────────────────────────────────────────────────────────────────────────────────
def _parse_moment(raw):
    """An ISO date-time (the frontend sends the viewer's local choice as UTC ISO), or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    value = parse_datetime(raw)
    if value is None:
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.utc)
    return value


def _filtered_tickets(request, user, org):
    """The tickets the filters keep, or (None, Response) on a bad value."""
    qs = _scoped(org)
    q = (request.GET.get("q") or "").strip()
    if len(q) > MAX_QUERY_LENGTH:
        return None, Response({"message": "That search is too long.", "code": "query_too_long"},
                              status=status.HTTP_400_BAD_REQUEST)
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(email__icontains=q) | Q(ticket_number__icontains=q)
            | Q(subject__icontains=q) | Q(messages__body__icontains=q) | Q(user__username__icontains=q)
        ).distinct()
    statuses = [s for s in (request.GET.get("status") or "").split(",") if s]
    if any(s not in dict(SupportTicket.STATUS_CHOICES) for s in statuses):
        return None, Response({"message": "That is not a ticket status.", "code": "bad_status"},
                              status=status.HTTP_400_BAD_REQUEST)
    if statuses:
        qs = qs.filter(status__in=statuses)
    start, end = request.GET.get("date_from"), request.GET.get("date_to")
    start_at, end_at = _parse_moment(start), _parse_moment(end)
    if (start and start_at is None) or (end and end_at is None):
        return None, Response({"message": "Dates must be date and time values.", "code": "bad_date"},
                              status=status.HTTP_400_BAD_REQUEST)
    # A request is in the window when anything happened on it inside the window.
    if start_at:
        qs = qs.filter(Q(last_message_at__gte=start_at) | Q(last_message_at__isnull=True, created_at__gte=start_at))
    if end_at:
        qs = qs.filter(created_at__lte=end_at)
    source = (request.GET.get("source") or "").strip()
    if source:
        if source not in dict(SupportTicket.SOURCE_CHOICES):
            return None, Response({"message": "That is not a source.", "code": "bad_source"},
                                  status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(source=source)
    assigned = (request.GET.get("assigned") or "").strip()
    if assigned == "me":
        qs = qs.filter(assigned_to=user)
    elif assigned == "none":
        qs = qs.filter(assigned_to__isnull=True)
    if (request.GET.get("has_files") or "") in ("1", "true"):
        qs = qs.filter(messages__attachments__isnull=False).distinct()
    return list(qs), None


# ── shapes ─────────────────────────────────────────────────────────────────────────────────────
def _private(user, org):
    """True on an organizer's desk worked by the organization's own members: they are not shown
    the player's email address (inbox #175, data minimisation, R71). The conversation happens on
    the desk and replies go out through AFC, so an organizer never needs it. AFC head / super
    admins looking at the same desk still see it."""
    return org is not None and not is_head_or_super(user)


def _person_row(key, tickets, private=False):
    """One row of the people list, from that person's tickets (newest activity first)."""
    newest = tickets[0]
    country_key, country_name = _person_country(newest)
    statuses = [t.status for t in tickets]
    worst = next((s for s in STATUS_ORDER if s in statuses), newest.status)
    last_in = (SupportMessage.objects.filter(ticket__in=tickets, direction=SupportMessage.DIRECTION_IN)
               .order_by("-created_at").values_list("body", flat=True).first()) or ""
    latest_message = (SupportMessage.objects.filter(ticket__in=[t for t in tickets if t.status in ANSWERABLE])
                      .exclude(channel=SupportMessage.CHANNEL_AUTO)
                      .order_by("-created_at").values_list("direction", flat=True).first())
    return {
        "key": key,
        "name": newest.name,
        "username": newest.user.username if newest.user_id else "",
        "email": "" if private else newest.email,
        "country": country_key,
        "country_name": country_name,
        "has_discord": any(t.discord_id for t in tickets),
        "ticket_count": len(tickets),
        "open_count": sum(1 for t in tickets if t.status in ANSWERABLE),
        "status": worst,
        # The person wrote last on something still open: the desk should answer.
        "needs_reply": latest_message == SupportMessage.DIRECTION_IN,
        "last_at": (newest.last_message_at or newest.created_at).isoformat(),
        "snippet": last_in[:SNIPPET_LENGTH],
        "ticket_numbers": [t.ticket_number for t in tickets],
    }


def _person_detail(key, tickets, private=False):
    rows = [_ticket_dict(t, for_staff=True, with_messages=True) for t in tickets]
    if private:
        for row in rows:
            row["email"] = ""
    return {"person": _person_row(key, tickets, private), "tickets": rows}


# ── endpoints ──────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def support_people(request):
    """GET support/people/ - the desk's left column: PEOPLE, not tickets.

    Query  : q (name, email, username, ticket number, words), status (comma list: open, waiting,
             resolved, closed), date_from / date_to (ISO date-time), country (a key from
             `countries`), source (contact_form, help_bot, staff), assigned (me | none),
             has_files (1), limit (1..100, default 30), offset.
    Answer : 200 {results: [person rows], total_count, has_more, next_offset, limit, offset,
             countries: [{value, label}], status_counts: {open, waiting, resolved, closed}}
             Newest activity first. A person is kept when at least one of their tickets passes
             the filters; their counts describe ALL their tickets.
    Auth   : _desk (AFC support staff, or with ?organization=<slug> that organization's answerers
             and AFC head / super admins). Consumed by app/(a)/a/support/page.tsx and the
             organizer desk app/(organizer)/organizer/support/page.tsx.
    """
    user, org, err = _desk(request)
    if err:
        return err
    kept, err = _filtered_tickets(request, user, org)
    if err:
        return err
    country = (request.GET.get("country") or "").strip()
    try:
        limit = max(1, min(int(request.GET.get("limit", DEFAULT_PAGE_SIZE)), MAX_PAGE_SIZE))
        offset = max(0, int(request.GET.get("offset", 0)))
    except (TypeError, ValueError):
        return Response({"message": "limit and offset must be numbers.", "code": "limit_offset_numbers"},
                        status=status.HTTP_400_BAD_REQUEST)

    # Every ticket of every person, so a row's counts are the whole person, not the filtered part.
    everyone = {}
    for t in _scoped(org).order_by("-last_message_at", "-created_at"):
        everyone.setdefault(person_key(t), []).append(t)
    keys_kept = []
    seen = set()
    for t in sorted(kept, key=lambda x: (x.last_message_at or x.created_at), reverse=True):
        k = person_key(t)
        if k not in seen:
            seen.add(k)
            keys_kept.append(k)
    countries = {}
    for k in everyone:
        ck, cn = _person_country(everyone[k][0])
        if ck:
            countries[ck] = cn
    if country:
        keys_kept = [k for k in keys_kept if _person_country(everyone[k][0])[0] == country]

    page = keys_kept[offset:offset + limit]
    status_counts = {s: 0 for s in STATUS_ORDER}
    for t in _scoped(org).values_list("status", flat=True):
        status_counts[t] = status_counts.get(t, 0) + 1
    return Response({
        "results": [_person_row(k, everyone[k], _private(user, org)) for k in page],
        "total_count": len(keys_kept),
        "has_more": offset + len(page) < len(keys_kept),
        "next_offset": offset + len(page),
        "limit": limit,
        "offset": offset,
        "countries": sorted(({"value": k, "label": v} for k, v in countries.items()),
                            key=lambda c: c["label"].casefold()),
        "status_counts": status_counts,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def support_person(request, key):
    """GET support/people/<key>/ - one person, every ticket and message, newest first.

    Also accepts ?ticket=<number> in place of a key's ticket list check: the staff email and Discord
    heads-up link to a TICKET, and the page opens that ticket's person.
    Auth: _desk (see support_people). 404 {code: person_not_found}.
    """
    user, org, err = _desk(request)
    if err:
        return err
    if key == "by-ticket":
        ticket = _scoped(org).filter(ticket_number=(request.GET.get("ticket") or "").strip()).first()
        if not ticket:
            return Response({"message": "We could not find that ticket.", "code": "ticket_not_found"},
                            status=status.HTTP_404_NOT_FOUND)
        key = person_key(ticket)
    tickets = _tickets_of(key, org)
    if not tickets:
        return Response({"message": "We could not find that person.", "code": "person_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(_person_detail(key, tickets, _private(user, org)), status=status.HTTP_200_OK)


def _reply_to(tickets, author, body, files, resolve, org=None):
    """Record `body` on each ticket in `tickets` and tell the person ONCE.

    Each ticket gets its own message row, so every ticket page shows the answer and the audit has
    one row per ticket. Files are stored once, on the newest request's message (they would
    otherwise be copied once per request). Returns (rejected_files, emailed, dmed).
    """
    first_message, rejected = None, []
    for i, ticket in enumerate(tickets):
        message = SupportMessage.objects.create(
            ticket=ticket, direction=SupportMessage.DIRECTION_OUT, channel=SupportMessage.CHANNEL_WEB,
            # On an organizer desk the reply is signed by the organization (inbox #175).
            author=author, author_name=(org.name if org is not None else "AFC Support"), body=body,
        )
        if i == 0:
            first_message = message
            _saved, rejected = _save_attachments(message, files or [])
        ticket.status = SupportTicket.STATUS_RESOLVED if resolve else SupportTicket.STATUS_WAITING
        if not ticket.assigned_to_id:
            ticket.assigned_to = author
        ticket.save(update_fields=["status", "assigned_to", "updated_at"])
        _touch(ticket, message.created_at)
    lead = tickets[0]
    others = [t.ticket_number for t in tickets[1:]]
    lang = (getattr(lead.user, "language", "") or "en") if lead.user_id else "en"
    emailed = notify.email_ticket_reply(lead, first_message, lang=lang, also=others)
    dmed = notify.dm_ticket_reply(lead, first_message, also=others)
    return rejected, bool(emailed), bool(dmed)


def _truthy(raw):
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def support_person_reply(request, key):
    """POST support/people/<key>/reply/ - one reply, recorded on several of this person's requests.

    Body   : message (required), ticket_numbers (comma list or repeated field; default: every
             open or waiting request of this person), files (optional, up to the ticket limit),
             resolve ("true" marks them resolved; otherwise "waiting on the sender").
    Answer : 200 {message, emailed, discord_dm, rejected_files, answered: [numbers], ...person}
             400 reply_empty / no_open_requests / ticket_not_theirs; 404 person_not_found.
    The person gets ONE email (on the newest request, naming the others) and one Discord DM.
    Auth   : _desk (see support_people).
    """
    user, org, err = _desk(request)
    if err:
        return err
    err = reply_refusal(user, org)
    if err:
        return err
    tickets = _tickets_of(key, org)
    if not tickets:
        return Response({"message": "We could not find that person.", "code": "person_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    body = (request.data.get("message") or "").strip()
    if not body:
        return Response({"message": "Write a reply first.", "code": "reply_empty"},
                        status=status.HTTP_400_BAD_REQUEST)
    raw = request.data.getlist("ticket_numbers") if hasattr(request.data, "getlist") else request.data.get("ticket_numbers")
    if isinstance(raw, str):
        raw = [raw]
    wanted = [n.strip() for item in (raw or []) for n in str(item).split(",") if n.strip()]
    mine = {t.ticket_number: t for t in tickets}
    if wanted:
        if any(n not in mine for n in wanted):
            return Response({"message": "One of those requests is not from this person.",
                             "code": "ticket_not_theirs"}, status=status.HTTP_400_BAD_REQUEST)
        targets = [mine[n] for n in wanted]
    else:
        targets = [t for t in tickets if t.status in ANSWERABLE]
    if not targets:
        return Response({"message": "This person has no open requests to answer.",
                         "code": "no_open_requests"}, status=status.HTTP_400_BAD_REQUEST)
    targets.sort(key=lambda t: (t.last_message_at or t.created_at), reverse=True)
    rejected, emailed, dmed = _reply_to(targets, user, body, request.FILES.getlist("files"),
                                        _truthy(request.data.get("resolve")), org)
    data = _person_detail(key, _tickets_of(key, org), _private(user, org))
    data.update({"message": "Reply sent.", "emailed": emailed, "discord_dm": dmed,
                 "rejected_files": rejected, "answered": [t.ticket_number for t in targets]})
    return Response(data, status=status.HTTP_200_OK)


@api_view(["POST"])
def support_bulk_reply(request):
    """POST support/people/bulk-reply/ - the same answer to several people at once.

    Body   : {keys: [person keys] (1..50), message, resolve?}. Each person's open and waiting
             requests get the message; each person is emailed and DMed on their own, so nobody
             sees who else got it. People with nothing open are skipped and counted.
    Answer : 200 {message, people_sent, requests_answered, skipped}
             400 reply_empty / no_people / too_many_people.
    Auth   : _desk (see support_people).
    """
    user, org, err = _desk(request)
    if err:
        return err
    err = reply_refusal(user, org)
    if err:
        return err
    body = (request.data.get("message") or "").strip()
    if not body:
        return Response({"message": "Write a reply first.", "code": "reply_empty"},
                        status=status.HTTP_400_BAD_REQUEST)
    keys = [str(k) for k in (request.data.get("keys") or []) if str(k).strip()]
    if not keys:
        return Response({"message": "Pick at least one person.", "code": "no_people"},
                        status=status.HTTP_400_BAD_REQUEST)
    if len(keys) > MAX_BULK_PEOPLE:
        return Response({"message": f"Pick at most {MAX_BULK_PEOPLE} people at a time.",
                         "code": "too_many_people"}, status=status.HTTP_400_BAD_REQUEST)
    resolve = _truthy(request.data.get("resolve"))
    people_sent = requests_answered = skipped = 0
    for key in dict.fromkeys(keys):
        targets = [t for t in _tickets_of(key, org) if t.status in ANSWERABLE]
        if not targets:
            skipped += 1
            continue
        _reply_to(targets, user, body, [], resolve, org)
        people_sent += 1
        requests_answered += len(targets)
    return Response({"message": "Sent.", "people_sent": people_sent,
                     "requests_answered": requests_answered, "skipped": skipped},
                    status=status.HTTP_200_OK)
