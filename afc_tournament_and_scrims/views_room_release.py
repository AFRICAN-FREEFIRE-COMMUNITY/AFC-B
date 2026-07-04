# ── Release room details to the WAITLIST on a no-show (owner 2026-07-04) ─────────
# When a registered team is a no-show (mark_no_show), the organizer/admin can release the map's room
# ID + PASS to the WAITLIST so a backup takes the slot - either to whichever waitlist teams the
# event's waitlist_mode implies, or to ONE hand-picked team.
#
#   POST events/room/release-to-waitlist/   release_room_details_to_waitlist
#     body: { match_id, tournament_team_id? }
#       - tournament_team_id given -> send ONLY to that team (the manual "you pick" choice).
#       - else by event.waitlist_mode:
#           fcfs_room        -> ALL waitlisted teams (first to join the room claims it).
#           first_registered -> the earliest-registered waitlisted team.
#           manual_admin     -> requires tournament_team_id (400 if missing - the admin must choose).
# Reuses deliver_broadcast (afc_auth) + the one-map room message; sets room_details_released_at so the
# room also surfaces on those teams' user event page. Gate: AFC event admin OR organizer with
# can_edit_events / can_upload_results (same as broadcast_match_room_details).

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Match, TournamentTeam, Event
from .views import _is_event_admin, org_can_event
from afc_auth.views import validate_token, deliver_broadcast


def _auth(request):
    auth = request.headers.get("Authorization", "")
    return validate_token(auth.split(" ")[1]) if auth.startswith("Bearer ") else None


def _can_manage(user, event):
    return (_is_event_admin(user)
            or org_can_event(user, "can_edit_events", event)
            or org_can_event(user, "can_upload_results", event))


def _team_users(tt):
    """Deduped Users on a TournamentTeam's roster."""
    out = {}
    for m in tt.members.select_related("user").all():
        if m.user and m.user.user_id not in out:
            out[m.user.user_id] = m.user
    return list(out.values())


@api_view(["POST"])
def release_room_details_to_waitlist(request):
    user = _auth(request)
    if not user:
        return Response({"message": "Invalid or missing session token."}, status=401)

    match = get_object_or_404(Match, match_id=request.data.get("match_id"))
    if not (match.group and match.group.stage):
        return Response({"message": "This match is not linked to a group/stage."}, status=400)
    group = match.group
    event = group.stage.event
    if not _can_manage(user, event):
        return Response({"message": "You do not have permission."}, status=403)
    if not (match.room_id or match.room_name or match.room_password):
        return Response({"message": "Set this map's room ID/name/password first."}, status=400)

    # ── choose the recipient waitlist team(s) ──
    chosen_id = request.data.get("tournament_team_id")
    wl = TournamentTeam.objects.filter(event=event, is_waitlisted=True)
    if chosen_id:
        tt = wl.filter(tournament_team_id=chosen_id).first() or \
            TournamentTeam.objects.filter(event=event, tournament_team_id=chosen_id).first()
        if not tt:
            return Response({"message": "That team is not registered for this event."}, status=404)
        teams = [tt]
    else:
        mode = event.waitlist_mode or "first_registered"
        if mode == "manual_admin":
            return Response({"message": "This event's waitlist is set to manual: pick a team to send to."}, status=400)
        if mode == "fcfs_room":
            teams = list(wl.select_related("team").order_by("tournament_team_id"))
        else:  # first_registered
            nxt = wl.order_by("tournament_team_id").first()
            teams = [nxt] if nxt else []
    if not teams:
        return Response({"message": "No waitlisted teams to send the room to."}, status=400)

    # ── recipients + message ──
    recipients = {}
    for tt in teams:
        for u in _team_users(tt):
            recipients[u.user_id] = u
    recipients = list(recipients.values())
    if not recipients:
        return Response({"message": "The selected waitlist team(s) have no players to message."}, status=400)

    title = f"Room details - {event.event_name}"
    message = (
        f"You are being moved in from the waitlist for {event.event_name}.\n"
        f"Stage: {group.stage.stage_name}\n"
        f"Group: {group.group_name}\n"
        f"Map: {match.match_map or ('Match ' + str(match.match_number))}\n"
        f"  Room ID: {match.room_id}\n"
        f"  Room: {match.room_name}\n"
        f"  Password: {match.room_password}\n"
        "Join the room now to claim the slot."
    )

    delivery = request.data.get("delivery") or "both"
    pushed, emailed = deliver_broadcast(
        recipients, title, message, delivery=delivery,
        notification_type="group_broadcast", related_event=event,
        sender=user, scope="room_details",
        stage_id=group.stage_id, stage_name=group.stage.stage_name,
    )
    # Surface the room on the recipients' user event page too.
    if not match.room_details_released_at:
        match.room_details_released_at = timezone.now()
        match.save(update_fields=["room_details_released_at"])

    return Response({
        "message": f"Room details sent to {len(recipients)} player(s) across {len(teams)} waitlist team(s).",
        "recipients": len(recipients),
        "teams": len(teams),
        "pushed": pushed,
        "emailed": emailed,
    })
