"""
The admin endpoints for event requirement waivers.

PERMISSIONS reuse the existing gate verbatim: _is_event_admin(user) or org_can_event(user,
"can_manage_registrations", event). That single expression is already shared by
event_invites._can_invite and add_teams_to_event, and its own docstring gives the reason: "inviting
a team and force-adding one are the same authority exercised more politely." Granting a waiver is
that same authority again, so it gets the same gate rather than a fourth copy of the rule.

AUDIT: every request here is a mutating request by an admin, so afc_auth.AuditLogMiddleware records
it automatically with no per-view code. That log deliberately excludes request bodies, which is why
`reason` is stored on the waiver row itself.

CONSUMED BY: the admin event registrations surface, through frontend lib/waivers.ts.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import User
from afc_auth.views import validate_token
from afc_team.models import Team

from . import waivers
from .models import Event, EventRequirementWaiver
from .views import _is_event_admin, org_can_event


def _require_event_manager(request, event):
    """(user, None) when the caller may manage this event's registrations, else (None, Response)."""
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        return None, Response({"message": "Invalid token."}, status=400)
    user = validate_token(header.split(" ")[1])
    if not user:
        return None, Response({"message": "Unauthorized."}, status=403)
    if not _is_event_admin(user) and not org_can_event(user, "can_manage_registrations", event):
        return None, Response({"message": "Unauthorized."}, status=403)
    return user, None


@api_view(["GET"])
def list_event_waivers(request, event_id):
    """Active waivers on one event.

    AUTH     Bearer SessionToken, event admin or organizer with can_manage_registrations
    RESPONSE 200 {"waivers": [{waiver_id, event_id, team_id, user_id, waived_codes, reason,
                               created_by, created_at}]}
    CONSUMED BY the admin registrations surface, to show which teams are excused and by whom.
    """
    event = get_object_or_404(Event, event_id=event_id)
    _user, refusal = _require_event_manager(request, event)
    if refusal:
        return refusal

    rows = EventRequirementWaiver.objects.filter(event=event, active=True).select_related(
        "created_by", "team", "user"
    )
    return Response({"waivers": [waivers.serialize(row) for row in rows]}, status=200)


@api_view(["POST"])
def create_event_waiver(request):
    """Grant or replace the active waiver for one competitor.

    AUTH     Bearer SessionToken, event admin or organizer with can_manage_registrations
    REQUEST  {"event_id": int, "team_id": int|null, "user_id": int|null,
              "codes": ["team_logo_required", ...], "reason": "why"}
    RESPONSE 201 {"message", "waiver": {...}} | 400 with a message naming the problem
    CONSUMED BY the waiver dialog, from the registrations list and the bulk-add refusal panel.
    """
    event = get_object_or_404(Event, event_id=request.data.get("event_id"))
    actor, refusal = _require_event_manager(request, event)
    if refusal:
        return refusal

    team = None
    user = None
    if request.data.get("team_id"):
        team = get_object_or_404(Team, team_id=request.data["team_id"])
    if request.data.get("user_id"):
        user = get_object_or_404(User, user_id=request.data["user_id"])

    try:
        waiver = waivers.grant(
            event, actor=actor, reason=request.data.get("reason"),
            codes=request.data.get("codes"), team=team, user=user,
        )
    except ValueError as exc:
        # The message names the offending code or the missing reason, so the dialog can show it
        # rather than a generic failure.
        return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {"message": "Waiver saved.", "waiver": waivers.serialize(waiver)},
        status=status.HTTP_201_CREATED,
    )


@api_view(["DELETE"])
def revoke_event_waiver(request, waiver_id):
    """Retire a waiver. The row survives, so the record of what was excused outlives the event.

    AUTH     Bearer SessionToken, event admin or organizer with can_manage_registrations
    RESPONSE 200 {"message"}
    IDEMPOTENT: revoking an already-revoked waiver is a 200 that changes nothing.
    """
    waiver = get_object_or_404(EventRequirementWaiver, waiver_id=waiver_id)
    actor, refusal = _require_event_manager(request, waiver.event)
    if refusal:
        return refusal
    if waiver.active:
        waivers.revoke(waiver, actor=actor)
    return Response({"message": "Waiver revoked."}, status=200)
