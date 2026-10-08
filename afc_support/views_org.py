"""
afc_support/views_org.py - "Ask the organizer": a player sends a question to an organization
(inbox #167 / #175).

Owner 2026-10-08: "We want to give organizers their own support feature, how can that work? people
will be able to ask them questions and they should be able to answer and view things sent to them,
including attachments." Decisions the same day: "Signed-in players only" may ask, and on the AFC
side "Only head admin and super admins can see stuff of organizer".

HOW IT WORKS
    The question is an ordinary SupportTicket with `organization` set (and `event` when it was asked
    from an event page), source "organizer". Everything after that is the support desk the team
    already uses, scoped by afc_support/org_scope.py:
      - the organization's members who are owner or hold "Answer support" work it on their portal's
        Support page (the same desk code, views_people.py, with ?organization=<slug>);
      - AFC head / super admins can open that desk too; ordinary AFC support staff never see it;
      - the player follows it on /support (My tickets) and the ticket page, exactly like a question
        to AFC, and replies are signed with the organization's name (notify "support_org_reply").

ENDPOINT
    POST support/organizations/<slug>/ask/
        multipart: message (required, at most 4000 characters), event (optional event slug, must
                   be one of THIS organization's events), files[] (optional, same rules as the
                   contact form: views._save_attachments)
        200 { message, ticket_number, token, rejected_files[] }
        400 { message, code }  question_empty | question_too_long | event_not_found
        401 { message, code }  auth_required (signed-in players only)
        404 { message, code }  organization_not_found (unknown, suspended or deleted)
        429 { message, code }  too_many_questions (QUESTIONS_PER_HOUR per player per organization)
    Consumed by the frontend's components/support/AskOrganizerDialog.tsx, opened from the event
    page and the organization page (wrapped in NeedsAccount, so a signed-out visitor is asked to
    sign in instead of seeing a form that would refuse them, R26).

NOTIFIES
    Each answerer (org_scope.answerers): a site notification in their language that opens
    /organizer/support?ticket=<number>, and an email (notify.email_org_new_question). AFC's support
    inbox is NOT emailed: this is the organizer's conversation, not AFC's.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from afc_auth.models import Notifications
from afc_organizers.models import Organization
from afc_support import notify
from afc_support.models import SupportTicket
from afc_support.org_scope import answerers
from afc_support.views import _actor, create_ticket_from_contact

MAX_QUESTION_LENGTH = 4000
QUESTIONS_PER_HOUR = 5
NOTIFICATION_SNIPPET = 140

# The site notification an answerer gets, in their language (hand-written, owner 2026-09-11).
NEW_QUESTION_TEXT = {
    "en": {"title": "New question for {org}", "body": "{name} asked: {snippet}"},
    "fr": {"title": "Nouvelle question pour {org}", "body": "{name} demande : {snippet}"},
    "pt": {"title": "Nova pergunta para {org}", "body": "{name} perguntou: {snippet}"},
}


def _notify_answerers(ticket, message):
    """Site notification + email to everybody who may answer. Never fails the caller: the
    question is already saved, and a notification that does not go is not a lost question."""
    snippet = message.body.strip().replace("\n", " ")
    if len(snippet) > NOTIFICATION_SNIPPET:
        snippet = snippet[:NOTIFICATION_SNIPPET].rstrip() + "..."
    for person in answerers(ticket.organization):
        try:
            text = NEW_QUESTION_TEXT.get(getattr(person, "language", "") or "en", NEW_QUESTION_TEXT["en"])
            Notifications.objects.create(
                user=person, notification_type="support",
                title=text["title"].format(org=ticket.organization.name),
                message=text["body"].format(name=ticket.name, snippet=snippet),
                related_event=ticket.event,
                target_type="custom", target_id=f"/organizer/support?ticket={ticket.ticket_number}",
            )
        except Exception:
            pass
        notify.email_org_new_question(ticket, message, person)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def ask_organizer(request, slug):
    """POST support/organizations/<slug>/ask/ - see the module docstring for the full contract."""
    user = _actor(request)
    if not user:
        return Response({"message": "Sign in to ask the organizer a question.", "code": "auth_required"},
                        status=status.HTTP_401_UNAUTHORIZED)
    org = Organization.objects.filter(slug=slug, status="active").first()
    if not org:
        return Response({"message": "We could not find that organizer.", "code": "organization_not_found"},
                        status=status.HTTP_404_NOT_FOUND)

    body = (request.data.get("message") or "").strip()
    if not body:
        return Response({"message": "Write your question first.", "code": "question_empty"},
                        status=status.HTTP_400_BAD_REQUEST)
    if len(body) > MAX_QUESTION_LENGTH:
        return Response({"message": f"Keep your question under {MAX_QUESTION_LENGTH} characters.",
                         "code": "question_too_long", "limit": MAX_QUESTION_LENGTH},
                        status=status.HTTP_400_BAD_REQUEST)

    event = None
    event_slug = (request.data.get("event") or "").strip()
    if event_slug:
        from afc_tournament_and_scrims.models import Event

        # Only the event's OWN organization answers questions about it (a co-organizer does not).
        event = Event.objects.filter(slug=event_slug, organization=org).first()
        if not event:
            return Response({"message": "That event is not one of this organizer's events.",
                             "code": "event_not_found"}, status=status.HTTP_400_BAD_REQUEST)

    # A cap per player per organization, so one person cannot bury an organizer's desk.
    recent = SupportTicket.objects.filter(
        user=user, organization=org, created_at__gte=timezone.now() - timedelta(hours=1)).count()
    if recent >= QUESTIONS_PER_HOUR:
        return Response({"message": "You have sent this organizer several questions in the last hour. "
                                    "Please wait for an answer before asking again.",
                         "code": "too_many_questions", "limit": QUESTIONS_PER_HOUR},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)

    ticket, message, rejected = create_ticket_from_contact(
        (getattr(user, "full_name", "") or user.username), user.email, body,
        files=request.FILES.getlist("files"), request_user=user,
        source=SupportTicket.SOURCE_ORGANIZER,
    )
    # The ticket is linked to the SIGNED-IN account, whatever create_ticket_from_contact matched by
    # email, and addressed to the organization (and the event, when asked from one).
    ticket.user = user
    ticket.discord_id = getattr(user, "discord_id", "") or ""
    ticket.organization = org
    ticket.event = event
    ticket.save(update_fields=["user", "discord_id", "organization", "event", "updated_at"])
    _notify_answerers(ticket, message)

    return Response({"message": "Your question has been sent to the organizer.",
                     "ticket_number": ticket.ticket_number,
                     "token": ticket.public_token,
                     "rejected_files": rejected},
                    status=status.HTTP_200_OK)
