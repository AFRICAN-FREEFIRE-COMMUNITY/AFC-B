# ──────────────────────────────────────────────────────────────────────────────
# "Did the players actually get the room ID?"
#
# THE PROBLEM THIS SOLVES: room details are the most time critical message AFC sends.
# A player who never receives the room password does not play the match. Until now the
# send returned a count that was thrown away, so nobody, organizer or admin, could
# answer that question for a single player.
#
# Two endpoints, both consumed by the "Send to players" area of the match editor
# (frontend app/(a)/a/events/[slug]/edit/_components/EditMatchModal.tsx):
#   GET  events/match-room-delivery/?match_id=<id>   who got it, who read it, who failed
#   POST events/resend-room-details/                 retry ONLY the failures
#
# The data comes from afc_whatsapp.WhatsAppMessage rows, which are written before each
# send and then advanced by Meta's status callbacks (afc_whatsapp/webhooks.py).
#
# A NOTE ON "read": WhatsApp only reports a read receipt when the recipient has them
# switched on. A player who has them off shows as delivered forever. So DELIVERED is
# the signal to trust, and read is a bonus. The UI says so, otherwise an organizer
# reads "not read" as "did not arrive" and resends unnecessarily.
# ──────────────────────────────────────────────────────────────────────────────
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import canonical_profile
from afc_auth.views import validate_token
from afc_whatsapp.models import WhatsAppMessage

from afc_organizers.permissions import org_can_event

from .models import Match
from .views import _group_recipient_users, _is_event_admin


def _authenticate(request):
    """Bearer SessionToken, the house pattern. Returns (user, error_response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _may_manage(user, event):
    """Same permission set that is allowed to SEND room details in the first place."""
    return (
        _is_event_admin(user)
        or org_can_event(user, "can_edit_events", event)
        or org_can_event(user, "can_upload_results", event)
    )


@api_view(["GET"])
def match_room_delivery(request):
    """Per-player WhatsApp delivery state for one match's room details.

    Request:  GET events/match-room-delivery/?match_id=<int>
              Header Authorization: Bearer <SessionToken>
    Auth:     event admin, or an organizer with can_edit_events / can_upload_results.
    Response: 200 {
                "match_id": 12,
                "summary": {"total": 40, "queued": 0, "sent": 2, "delivered": 30,
                            "read": 5, "failed": 1, "no_number": 2},
                "players": [{"user_id", "username", "status", "error_code",
                             "error_title", "sent_at", "delivered_at", "read_at"}]
              }
              400 no match_id, 403 not permitted, 404 unknown match.
    Consumed by: EditMatchModal's room-details panel.
    """
    user, err = _authenticate(request)
    if err:
        return err

    match_id = request.query_params.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)
    if not (match.group and match.group.stage):
        return Response({"message": "This match is not linked to a group/stage."}, status=400)
    event = match.group.stage.event

    if not _may_manage(user, event):
        return Response(
            {"message": "You do not have permission to view this."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Newest row per player: a resend creates another row, and the organizer cares about
    # the latest attempt, not the history.
    latest = {}
    for row in (WhatsAppMessage.objects
                .filter(match=match, context="room_details")
                .select_related("user")
                .order_by("created_at")):
        if row.user_id:
            latest[row.user_id] = row

    players = []
    counts = {"queued": 0, "sent": 0, "delivered": 0, "read": 0, "failed": 0, "no_number": 0}

    for recipient in _group_recipient_users(event, match.group):
        row = latest.get(recipient.pk)
        if row is None:
            # No row at all means we never had a number to send to (or they opted out),
            # which is a DIFFERENT problem from a failed send and must not be hidden
            # inside "failed": the fix is asking the player for a number, not resending.
            profile = canonical_profile(recipient)
            has_number = bool((getattr(profile, "whatsapp_number", "") or "").strip())
            counts["no_number"] += 1
            players.append({
                "user_id": recipient.pk,
                "username": recipient.username,
                "status": "no_number",
                "has_number": has_number,
                "error_code": None,
                "error_title": "",
                "sent_at": None, "delivered_at": None, "read_at": None,
            })
            continue

        counts[row.status] = counts.get(row.status, 0) + 1
        players.append({
            "user_id": recipient.pk,
            "username": recipient.username,
            "status": row.status,
            "has_number": True,
            "error_code": row.error_code,
            "error_title": row.error_title,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
            "read_at": row.read_at.isoformat() if row.read_at else None,
        })

    # Worst state first: the organizer opens this to find the problems, not to admire
    # the successes.
    order = {"failed": 0, "no_number": 1, "queued": 2, "sent": 3, "delivered": 4, "read": 5}
    players.sort(key=lambda p: (order.get(p["status"], 9), p["username"].lower()))

    return Response(
        {"match_id": match.match_id, "summary": {"total": len(players), **counts},
         "players": players},
        status=200,
    )


@api_view(["POST"])
def resend_room_details(request):
    """Resend this match's room details to ONLY the players whose message failed.

    Request:  POST events/resend-room-details/  {"match_id": <int>}
              Header Authorization: Bearer <SessionToken>
    Auth:     same as match_room_delivery.
    Response: 200 {"message", "resent": <int>, "skipped": <int>}
              400 no match_id or no failures, 403 not permitted, 404 unknown match.
    Consumed by: the "Resend to failures" button in EditMatchModal.

    Deliberately NOT a blanket resend: messaging forty players again because one failed
    trains people to ignore AFC's messages, and Meta charges per conversation.
    """
    user, err = _authenticate(request)
    if err:
        return err

    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)
    if not (match.group and match.group.stage):
        return Response({"message": "This match is not linked to a group/stage."}, status=400)
    event = match.group.stage.event

    if not _may_manage(user, event):
        return Response(
            {"message": "You do not have permission to send room details for this event."},
            status=status.HTTP_403_FORBIDDEN,
        )

    failed_user_ids = set(
        WhatsAppMessage.objects
        .filter(match=match, context="room_details", status="failed")
        .exclude(user_id=None)
        .values_list("user_id", flat=True)
    )
    if not failed_user_ids:
        return Response({"message": "Nothing to resend, no message failed.", "resent": 0,
                         "skipped": 0}, status=400)

    # Resolve back to user objects through the recipient list so a player who has since
    # left the group is not messaged.
    targets = [u for u in _group_recipient_users(event, match.group) if u.pk in failed_user_ids]

    from .whatsapp_room_details import send_room_details
    resent, skipped = send_room_details(targets, event, match)

    return Response(
        {"message": f"Resent to {resent} player(s).", "resent": resent, "skipped": skipped},
        status=200,
    )
