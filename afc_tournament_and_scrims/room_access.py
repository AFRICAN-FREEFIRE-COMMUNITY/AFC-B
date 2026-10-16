"""
afc_tournament_and_scrims/room_access.py - who may see a Battle Royale match's room details. ONE rule.

THE RULE (owner 2026-06-17, unchanged): a match's room id, room name and password are shown only to
the competitors of that match's GROUP (the solo player, or anyone on the team's event roster), and only
after the organizer releases them (Match.room_details_released_at, set by the room-details broadcast).
For an event whose waitlist mode is "fcfs_room", waitlisted competitors see every released room too:
first to join the room takes the empty slot. Event staff (super, head and event admins) see every
group's room on the event page; organizers are deliberately not staff here (owner 2026-06-29).

WHY A MODULE (inbox #280, owner 2026-10-16): the Help assistant now answers "what is our room ID and
password?", and it must give the answer to exactly the people the event page shows it to and nobody
else. The rule lived inline in get_event_details; two copies would drift, so both read it from here:
  - afc_tournament_and_scrims.views.get_event_details: viewer_room_context + can_see_room, per match;
  - afc_helpbot.facts: released_rooms_for(user), the person's own released rooms across their events,
    WITHOUT the staff exception: through the assistant an admin gets only their own rooms.
"""
from django.utils import timezone

# Events still worth a room: the assistant never hands out a room for an event that has finished.
ACTIVE_EVENT_STATUSES = ("upcoming", "ongoing")
# Facts are capped by the bot process (afcbot/bot.py WEB_CHAT_MAX_FACTS_CHARS); a handful of rooms is
# plenty for "what is our room for today", and the nearest ones come first.
MAX_ROOMS = 8


def viewer_room_context(event, user):
    """(group_ids, is_waitlisted) for `user` in `event`: the groups they compete in, and whether they
    (or a team they are on) sit on the event's waitlist. Empty for nobody signed in."""
    from .models import RegisteredCompetitors, StageGroupCompetitor, TournamentTeam

    if not user:
        return set(), False
    if event.participant_type == "solo":
        group_ids = set(StageGroupCompetitor.objects.filter(
            stage_group__stage__event=event, player__user=user,
        ).values_list("stage_group_id", flat=True))
        waitlisted = RegisteredCompetitors.objects.filter(
            event=event, user=user, is_waitlisted=True).exists()
        return group_ids, waitlisted
    tt_ids = list(TournamentTeam.objects.filter(
        event=event, members__user=user).values_list("tournament_team_id", flat=True))
    group_ids = set(StageGroupCompetitor.objects.filter(
        stage_group__stage__event=event, tournament_team_id__in=tt_ids,
    ).values_list("stage_group_id", flat=True))
    waitlisted = TournamentTeam.objects.filter(
        event=event, members__user=user, is_waitlisted=True).exists()
    return group_ids, waitlisted


def can_see_room(event, group_id, match, *, staff, group_ids, waitlisted):
    """May this viewer see this match's room details? `staff`, `group_ids` and `waitlisted` come from
    the caller (viewer_room_context, plus the page's own staff check)."""
    if staff:
        return True
    if match.room_details_released_at is None:
        return False  # not posted yet: nobody on the player side sees it
    if group_id in group_ids:
        return True
    return event.waitlist_mode == "fcfs_room" and waitlisted


def _has_room(match):
    return bool(match.room_id or match.room_name or match.room_password)


def _events_of(user):
    """The active events `user` competes in, as a team member or a solo player, soonest first."""
    from .models import Event, RegisteredCompetitors, TournamentTeam

    ids = set(TournamentTeam.objects.filter(members__user=user).values_list("event_id", flat=True))
    ids |= set(RegisteredCompetitors.objects.filter(user=user).values_list("event_id", flat=True))
    return (Event.objects.filter(pk__in=ids, event_status__in=ACTIVE_EVENT_STATUSES,
                                 end_date__gte=timezone.localdate())
            .order_by("start_date"))


def released_rooms_for(user):
    """The person's own rooms, for the Help assistant (afc_helpbot.facts).

    Returns {"released": [...], "not_posted_yet": [...]}. A released entry carries the credentials;
    a not-posted entry names the match only, so the assistant can say "your organizer has not posted
    it yet" instead of "I cannot see that". Only matches not yet played (no result entered) count.
    Never the staff path: the assistant shows an admin only their own rooms.
    """
    from .models import Match

    released, waiting = [], []
    if not user:
        return {"released": released, "not_posted_yet": waiting}
    for event in _events_of(user):
        group_ids, waitlisted = viewer_room_context(event, user)
        if not group_ids and not (event.waitlist_mode == "fcfs_room" and waitlisted):
            continue
        matches = (Match.objects.filter(group__stage__event=event, result_inputted=False)
                   .select_related("group__stage")
                   .order_by("group__playing_date", "group__playing_time", "match_number"))
        for match in matches:
            group = match.group
            mine = group.group_id in group_ids
            if can_see_room(event, group.group_id, match, staff=False, group_ids=group_ids,
                            waitlisted=waitlisted):
                if not _has_room(match):
                    continue
                released.append({
                    "event": event.event_name,
                    "page": f"/tournaments/{event.slug}" if event.slug else "",
                    "stage": group.stage.stage_name,
                    "group": group.group_name,
                    "plays_on": group.playing_date.isoformat() if group.playing_date else None,
                    "group_start_time": group.playing_time.strftime("%H:%M") if group.playing_time else None,
                    "event_timezone": event.timezone or None,
                    "match_number": match.match_number,
                    "map": match.match_map or None,
                    "room_id": match.room_id or "",
                    "room_name": match.room_name or "",
                    "room_password": match.room_password or "",
                    "room_is_3d": bool(match.room_is_3d),
                    "my_group": mine,
                })
            elif mine:
                waiting.append({
                    "event": event.event_name,
                    "page": f"/tournaments/{event.slug}" if event.slug else "",
                    "group": group.group_name,
                    "match_number": match.match_number,
                    "plays_on": group.playing_date.isoformat() if group.playing_date else None,
                })
    return {"released": released[:MAX_ROOMS], "not_posted_yet": waiting[:MAX_ROOMS]}
