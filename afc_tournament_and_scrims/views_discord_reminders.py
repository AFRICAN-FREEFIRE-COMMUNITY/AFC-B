"""
afc_tournament_and_scrims.views_discord_reminders - the Actions tab's Discord reminders card.

ROUTE (afc_tournament_and_scrims/urls.py, under events/)
    GET  <event_id>/discord-reminders/   the cadence, the note, the plan with each moment's state,
                                         the recipient count right now
    POST <event_id>/discord-reminders/   {"frequency": <key>, "note": "..."} saves both

Auth: the same gate as reopen_event / edit_event: an AFC event admin, or an organizer holding
can_edit_events on the owning org (accepted co-owners included through org_can_event). Every
refusal carries a code (R35 / R44). A save writes the two Event fields declared in
event_contract.py through their cleaners, so this view and edit_event can never disagree about
what a valid cadence is.

Consumed by: DiscordRemindersCard.tsx on the shared Actions tab (admin + organizer edit pages).
"""
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.audit import set_audit
from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from .discord_reminders import clean_frequency, clean_note, serialize_settings
from .models import Event


def _gate(request, event_id):
    """(user, event, None) or (None, None, Response)."""
    from .views import _is_event_admin

    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None, None, Response({"message": "Authorization header is required.", "code": "auth_required"},
                                    status=status.HTTP_401_UNAUTHORIZED)
    user = validate_token(auth.split(" ", 1)[1].strip())
    if not user:
        return None, None, Response({"message": "Invalid or expired session token.", "code": "auth_required"},
                                    status=status.HTTP_401_UNAUTHORIZED)
    event = get_object_or_404(Event, event_id=event_id)
    if not (_is_event_admin(user) or org_can_event(user, "can_edit_events", event)):
        return None, None, Response({"message": "You do not have permission to edit this event.",
                                     "code": "event_forbidden"}, status=status.HTTP_403_FORBIDDEN)
    return user, event, None


@api_view(["GET"])
def discord_reminders_read(request, event_id):
    user, event, err = _gate(request, event_id)
    if err:
        return err
    return Response(serialize_settings(event))


@api_view(["POST"])
def discord_reminders_save(request, event_id):
    user, event, err = _gate(request, event_id)
    if err:
        return err
    try:
        frequency = clean_frequency(request.data.get("frequency"))
        note = clean_note(request.data.get("note"))
    except ValueError as exc:
        return Response({"message": str(exc), "code": "reminder_invalid"}, status=status.HTTP_400_BAD_REQUEST)
    event.discord_reminder_frequency = frequency
    event.discord_reminder_note = note
    event.save(update_fields=["discord_reminder_frequency", "discord_reminder_note"])
    set_audit(request, f"Set Discord reminders on {event.event_name} to {frequency}")
    body = serialize_settings(event)
    body["message"] = "Discord reminders saved."
    return Response(body)


@csrf_exempt
def discord_reminders_view(request, event_id):
    """One address, two verbs (the same shape afc_auth's delete-account route uses)."""
    if request.method == "GET":
        return discord_reminders_read(request, event_id)
    return discord_reminders_save(request, event_id)
