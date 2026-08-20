# ── Event Check-in (owner 2026-07-04) ───────────────────────────────────────────
# When an admin/organizer enables check-in on an event, every registered competitor must LOG IN and
# tap "check in" inside the check-in window to stay eligible. A SQUAD is eligible only when EVERY
# one of its registered roster members checks in. Whoever does not check in by checkin_end is
# RELEGATED to the waitlist (is_waitlisted=True on their RegisteredCompetitors + TournamentTeam).
#
# WINDOW RULES (validated in set_event_checkin): the window can only OPEN after registration ends and
# must CLOSE before the event starts (and end > start) - so an event can never start with an open
# check-in window.
#
# ENDPOINTS (wired in urls.py):
#   PATCH events/checkin/settings/        set_event_checkin        (admin/organizer)
#   POST  events/checkin/                 player_checkin           (a registered user taps "I'm here")
#   GET   events/checkin/status/          get_event_checkin_status (user's own + team status; admins get all)
#   POST  events/checkin/relegate/        checkin_relegate_now     (admin/organizer force the sweep)
# CONSUMED BY: the admin/organizer event-edit "Check-in" settings + the user event page Check-in button.
# The window-close sweep also runs from the daily status task (update_event_and_stage_statuses).

from datetime import datetime as _dt, time as _time

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Event, EventCheckIn, RegisteredCompetitors, TournamentTeam, TournamentTeamMember
from .views import _is_event_admin, org_can_event
from afc_auth.views import validate_token


# ── auth helpers ────────────────────────────────────────────────────────────────
def _auth_user(request):
    """The Bearer-token user, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ")[1])


def _is_checkin_manager(user, event):
    """Who may configure/force check-in: AFC event admins OR the owning organizer holding
    can_manage_registrations (same gate as seeding/registration actions). Native (org=None) events
    stay admin-only via org_can_event."""
    return _is_event_admin(user) or org_can_event(user, "can_manage_registrations", event)


def _aware(dt):
    """Make a naive datetime tz-aware in the server timezone; pass through aware/None."""
    if dt and timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _local_label(event, dt):
    """An instant written in the EVENT's own timezone, for an error message an organizer reads.

    The organizer typed a wall-clock time in the host's timezone, so telling them the boundary in
    UTC would be answering a question they did not ask. The zone is named so there is no doubt.
    """
    from .views import _event_zone

    zone = _event_zone(event)
    local = dt.astimezone(zone)
    label = getattr(zone, "key", None) or str(zone)
    return f"{local.strftime('%d %b %Y, %H:%M')} ({label})"


def _registration_end_dt(event):
    """When registration actually closes, as one absolute instant.

    BUG (owner reported 2026-08-04, screenshot): an organizer set registration to end at 6:59pm
    and check-in to open at 7:15pm, and was refused with "Check-in can only begin after
    registration ends."

    Two faults, both here, and both the same root cause: this function had its own idea of when
    registration ends, different from the one the rest of the codebase uses.

      1. IT IGNORED THE EVENT'S TIMEZONE. An event's registration_end_time is the HOST's wall
         clock (Event.timezone), but this combined it in the SERVER timezone, which is UTC in
         production. For a Lagos event closing at 18:59 local, the true instant is 17:59 UTC and
         this computed 18:59 UTC, an hour late. The admin's typed 19:15 Lagos arrives as 18:15
         UTC, which is "before" that wrong 18:59, so a perfectly valid window was refused. This
         is the same class of bug as the Ethiopia registration one, in a second copy of the
         logic that was never updated.

      2. THE FALLBACK WAS END OF DAY. With no registration_end_time set, _time.max made
         registration close at 23:59:59, so check-in could never be scheduled on the same day at
         all, whatever the organizer typed.

    Both go away by calling the ONE function that already answers this question in the event's
    own timezone. Imported lazily because views.py imports this module's siblings and a
    top-level import would close the cycle.
    """
    if not event.registration_end_date:
        return None
    from .views import registration_window_instants

    return registration_window_instants(event)[1]


def _event_start_dt(event):
    """When the event starts, in the EVENT's timezone rather than the server's, for the same
    reason as above: a Lagos event starting at 20:00 starts at 19:00 UTC, and comparing a
    check-in close time against 20:00 UTC would let it run an hour into the event."""
    if not event.start_date:
        return None
    from .views import _event_zone

    return timezone.make_aware(
        _dt.combine(event.start_date, event.event_start_time or _time.min),
        _event_zone(event),
    )


def _window_open(event, now=None):
    """True when check-in is enabled and now is within [checkin_start, checkin_end]."""
    if not event.checkin_enabled or not event.checkin_start or not event.checkin_end:
        return False
    now = now or timezone.now()
    return event.checkin_start <= now <= event.checkin_end


# ── eligibility resolution ────────────────────────────────────────────────────────
def _user_squad(event, user):
    """The TournamentTeam this user is a rostered member of for the event, or None.

    WAITLISTED TEAMS ARE INCLUDED, deliberately, and the docstring used to claim the opposite
    while the query never filtered on it. The behaviour was right and the description was wrong,
    which is worse than either: a reader trusting it would have "fixed" the query and broken the
    feature. A waitlisted competitor MUST be able to check in, because promotion into a freed
    slot only goes to competitors who did (promote_checked_in_waitlist). A waitlist that cannot
    check in can never be promoted, which would make the whole replacement rule dead code.
    """
    m = (TournamentTeamMember.objects
         .filter(user=user, tournament_team__event=event)
         .exclude(status="rejected")
         .select_related("tournament_team")
         .first())
    return m.tournament_team if m else None


def _user_solo_registration(event, user):
    """The user's own solo RegisteredCompetitors row for the event (user-based registration), or None."""
    return (RegisteredCompetitors.objects
            .filter(event=event, user=user)
            .exclude(status__in=["withdrawn", "left", "rejected"])
            .first())


# ── endpoints ──────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def set_event_checkin(request):
    """Set an event's check-in settings. Body: {event_id, checkin_enabled: bool, checkin_start,
    checkin_end} (ISO datetimes). Validates the window opens AFTER registration ends and closes
    BEFORE the event starts. Auth: AFC event admin OR organizer with can_manage_registrations."""
    user = _auth_user(request)
    if not user:
        return Response({"message": "Invalid or missing session token."}, status=401)
    event = get_object_or_404(Event, event_id=request.data.get("event_id"))
    if not _is_checkin_manager(user, event):
        return Response({"message": "You do not have permission to configure check-in."}, status=403)

    enabled = bool(request.data.get("checkin_enabled"))
    if enabled:
        start = _aware(parse_datetime(request.data.get("checkin_start") or ""))
        end = _aware(parse_datetime(request.data.get("checkin_end") or ""))
        if not start or not end:
            return Response({"message": "checkin_start and checkin_end are required when check-in is on."}, status=400)
        if end <= start:
            return Response({"message": "Check-in end time must be after its start time."}, status=400)
        reg_end = _registration_end_dt(event)
        ev_start = _event_start_dt(event)
        # The refusals NAME the boundary they are refusing against, in the event's own timezone.
        # "Check-in can only begin after registration ends" on its own sent an organizer hunting
        # for a mistake they had not made: the real reason was usually that registration has no
        # end TIME and therefore runs to 23:59 that day, which nothing on the screen said.
        if reg_end and start < reg_end:
            return Response({"message": (
                "Check-in can only begin after registration ends. Registration for this event "
                f"closes at {_local_label(event, reg_end)}"
                + ("." if event.registration_end_time else
                   ", because no registration end time is set, so it runs to the end of that day. "
                   "Set a registration end time first, or open check-in after that.")
            )}, status=400)
        if ev_start and end > ev_start:
            return Response({"message": (
                "Check-in must close before the event starts. This event starts at "
                f"{_local_label(event, ev_start)}."
            )}, status=400)
        event.checkin_start = start
        event.checkin_end = end
    event.checkin_enabled = enabled
    event.save(update_fields=["checkin_enabled", "checkin_start", "checkin_end"])
    return Response({
        "message": "Check-in settings saved.",
        "checkin_enabled": event.checkin_enabled,
        "checkin_start": event.checkin_start.isoformat() if event.checkin_start else None,
        "checkin_end": event.checkin_end.isoformat() if event.checkin_end else None,
    })


@api_view(["POST"])
def player_checkin(request):
    """A registered user taps "check in" for an event. Body: {event_id}. Requires the check-in window
    to be OPEN and the user to be an active registrant (solo row or a squad roster member). Idempotent
    (the (event,user) unique constraint means a double-tap just returns the existing row)."""
    user = _auth_user(request)
    if not user:
        return Response({"message": "Invalid or missing session token."}, status=401)
    event = get_object_or_404(Event, event_id=request.data.get("event_id"))
    if not event.checkin_enabled:
        return Response({"message": "Check-in is not enabled for this event."}, status=400)
    now = timezone.now()
    if not event.checkin_start or now < event.checkin_start:
        return Response({"message": "Check-in has not opened yet."}, status=400)
    if not event.checkin_end or now > event.checkin_end:
        return Response({"message": "Check-in has closed."}, status=400)

    squad = _user_squad(event, user)
    solo = None if squad else _user_solo_registration(event, user)
    if not squad and not solo:
        return Response({"message": "You are not registered for this event."}, status=403)

    obj, created = EventCheckIn.objects.get_or_create(
        event=event, user=user, defaults={"tournament_team": squad})
    return Response({
        "message": "You are checked in." if created else "You are already checked in.",
        "checked_in": True,
        "checked_in_at": obj.checked_in_at.isoformat(),
    }, status=200)


@api_view(["GET"])
def get_event_checkin_status(request):
    """Check-in status for ?event_id=. For a normal user: their own checked-in flag + (for a squad)
    how many of their roster have checked in. For an admin/organizer: the full per-competitor list."""
    # THE SCHEDULE IS PUBLIC, the personal status is not (owner 2026-08-04: "if check-in is
    # enabled for an event let it show on the event page"). Requiring a token for the whole
    # response hid the check-in requirement from exactly the people who most need to see it: a
    # visitor deciding whether to enter, and a registrant who is signed out on their phone. Those
    # are the competitors who register without noticing and lose their slot for missing it.
    #
    # So an anonymous caller gets whether check-in is on and when the window runs, and nothing
    # else: no roster, no per-competitor list, no other player's state.
    user = _auth_user(request)
    event = get_object_or_404(Event, event_id=request.query_params.get("event_id"))

    base = {
        "checkin_enabled": event.checkin_enabled,
        "checkin_start": event.checkin_start.isoformat() if event.checkin_start else None,
        "checkin_end": event.checkin_end.isoformat() if event.checkin_end else None,
        "window_open": _window_open(event),
    }

    if not user:
        # `me` is absent rather than a fabricated "not registered": the caller genuinely does not
        # know who this is, and saying "you are not registered" to a signed-out registrant would
        # be a lie the UI would then act on.
        return Response(base)

    checked_user_ids = set(EventCheckIn.objects.filter(event=event).values_list("user_id", flat=True))

    # ── this viewer's own status ──
    squad = _user_squad(event, user)
    solo = None if squad else _user_solo_registration(event, user)
    my = {"registered": bool(squad or solo), "checked_in": user.user_id in checked_user_ids, "is_squad": bool(squad)}
    if squad:
        roster = list(TournamentTeamMember.objects.filter(tournament_team=squad).exclude(status="rejected")
                      .values_list("user_id", flat=True))
        my["team_id"] = squad.tournament_team_id
        my["roster_total"] = len(roster)
        my["roster_checked_in"] = sum(1 for uid in roster if uid in checked_user_ids)
        my["team_eligible"] = all(uid in checked_user_ids for uid in roster) if roster else False
    base["me"] = my

    # ── admin/organizer full breakdown ──
    if _is_checkin_manager(user, event):
        # WHEN each player checked in, not merely whether. An organizer chasing a squad that is
        # one player short needs the NAME of the player who has not checked in (owner
        # 2026-08-04: "admins or organizers should also be able to see who has checked in for
        # each team and who hasn't"). A count of 3 of 4 tells them a problem exists and nothing
        # about who to message.
        checkin_times = dict(
            EventCheckIn.objects.filter(event=event).values_list("user_id", "checked_in_at"))
        teams = []
        for tt in TournamentTeam.objects.filter(event=event).select_related("team"):
            members = list(
                TournamentTeamMember.objects.filter(tournament_team=tt)
                .exclude(status="rejected").select_related("user"))
            roster = [m.user_id for m in members]
            done = sum(1 for uid in roster if uid in checked_user_ids)
            teams.append({
                "tournament_team_id": tt.tournament_team_id,
                # display_name: a ghost has no .team, and "?" on a check-in list is a
                # competitor nobody can identify (owner 2026-08-20).
                "team_name": tt.display_name,
                "is_waitlisted": tt.is_waitlisted,
                "roster_total": len(roster),
                "roster_checked_in": done,
                "eligible": (len(roster) > 0 and done == len(roster)),
                # Named, per player, with the missing ones listed as plainly as the present
                # ones. checked_in_at is ISO UTC and the UI renders it in the viewer's own
                # timezone; it is null for anyone who has not checked in.
                "players": [
                    {
                        "user_id": m.user_id,
                        "username": getattr(m.user, "username", "?"),
                        "checked_in": m.user_id in checked_user_ids,
                        "checked_in_at": (
                            checkin_times[m.user_id].isoformat()
                            if m.user_id in checkin_times else None),
                    }
                    for m in members
                ],
            })
        solos = []
        for r in RegisteredCompetitors.objects.filter(event=event, user__isnull=False).select_related("user"):
            solos.append({
                "user_id": r.user_id,
                "username": r.user.username if r.user else "?",
                "is_waitlisted": r.is_waitlisted,
                "checked_in": r.user_id in checked_user_ids,
                "checked_in_at": (
                    checkin_times[r.user_id].isoformat()
                    if r.user_id in checkin_times else None),
            })
        base["teams"] = teams
        base["solos"] = solos
        base["is_manager"] = True
    return Response(base)


def relegate_unchecked_competitors(event):
    """Move every competitor who did NOT check in to the waitlist (owner 2026-07-04). A SQUAD is
    relegated when ANY of its registered roster members has no check-in; a SOLO when the user has no
    check-in. Only runs for a check-in-enabled event whose window has CLOSED. Returns the count moved.
    Idempotent: an already-waitlisted competitor is skipped. Called by checkin_relegate_now + the
    daily status sweep."""
    if not event.checkin_enabled or not event.checkin_end or timezone.now() < event.checkin_end:
        return 0
    checked = set(EventCheckIn.objects.filter(event=event).values_list("user_id", flat=True))
    moved = 0
    # Squads: relegate a team missing any roster check-in.
    for tt in TournamentTeam.objects.filter(event=event, is_waitlisted=False):
        roster = list(TournamentTeamMember.objects.filter(tournament_team=tt).exclude(status="rejected")
                      .values_list("user_id", flat=True))
        if not roster or not all(uid in checked for uid in roster):
            tt.is_waitlisted = True
            tt.save(update_fields=["is_waitlisted"])
            # GUARD (owner 2026-08-20, ghost competitors): RegisteredCompetitors.team has no ghost
            # counterpart, so a ghost TournamentTeam has NO matching RC row - team=tt.team would
            # become team=None, which matches every SOLO registration in the event instead
            # (RegisteredCompetitors.team is null=True and that is how solo entries are stored).
            # Skip the RC sync for a ghost; only real teams have an RC row to keep in sync.
            if tt.team_id:
                RegisteredCompetitors.objects.filter(event=event, team=tt.team, is_waitlisted=False).update(is_waitlisted=True)
            moved += 1
    # Solos: relegate a user with no check-in.
    for r in RegisteredCompetitors.objects.filter(event=event, user__isnull=False, is_waitlisted=False):
        if r.user_id not in checked:
            r.is_waitlisted = True
            r.save(update_fields=["is_waitlisted"])
            moved += 1
    return moved


def promote_checked_in_waitlist(event, freed):
    """Fill the slots that relegation just freed, from the waitlist (owner 2026-08-04).

    THIS IS THE OTHER HALF OF THE RULE, and it was missing. The owner's words were that a team
    which does not complete check-in "gets replaced by a waitlist". Relegation on its own only
    does the first half: it empties the seat. Without this, an event that started with 16 teams
    and lost 3 to a missed check-in simply ran with 13, while teams who DID check in sat on the
    waitlist waiting for a promotion that never came. Both halves have to happen or the check-in
    requirement quietly shrinks the event instead of enforcing it.

    WHO GETS PROMOTED, and why only them: a waitlisted competitor is promoted ONLY if they are
    themselves fully checked in. Promoting somebody who did not check in either would seat a team
    that has shown no sign of turning up, which is the exact thing check-in exists to detect. This
    is why waitlisted competitors are allowed to check in during the window in the first place.

    ORDER IS THE ORGANIZER'S CHOICE, not a constant (owner 2026-08-04). Event.waitlist_mode has
    three settings and this honours all of them, because an organizer who chose one and silently
    got another would be worse served than one with no setting at all:

      first_registered  earliest registration first. The default, and the only one this used to
                        do regardless of what the organizer had picked.
      fcfs_room         first to join the room. There is no room to join at this point in the
                        flow, and the closest honest reading of "first come" here is who checked
                        in first, so it orders by check-in time. Stated plainly rather than
                        quietly falling back to registration order, which would have looked
                        identical on screen while ignoring the setting.
      manual_admin      the organizer picks. So this promotes NOBODY automatically and leaves the
                        freed seats for them to fill from the waitlist page. Auto-filling a seat
                        on an event whose organizer explicitly asked to choose would be the
                        rudest possible reading of the setting.

    `freed` caps how many are promoted, so this can never seat more teams than check-in removed
    and can never take an event over the size the organizer set.
    """
    if freed <= 0:
        return 0

    mode = (getattr(event, "waitlist_mode", None) or "first_registered").strip().lower()
    if mode == "manual_admin":
        # The organizer asked to decide. Leaving the seats empty IS the correct behaviour here.
        return 0

    checkins = dict(
        EventCheckIn.objects.filter(event=event).values_list("user_id", "checked_in_at"))
    checked = set(checkins)
    promoted = 0

    def _checked_in_order(user_ids):
        """When the LAST of these players checked in, for fcfs_room ordering.

        The last rather than the first, because a squad is only ready once everybody is in, so
        that instant is when the team actually became available for the slot.
        """
        stamps = [checkins[uid] for uid in user_ids if uid in checkins]
        return max(stamps) if stamps else None

    if (event.participant_type or "").lower() == "solo":
        candidates = list(RegisteredCompetitors.objects.filter(
            event=event, is_waitlisted=True, user__isnull=False).order_by("registration_date"))
        if mode == "fcfs_room":
            # Solo: the competitor's own check-in time. Anyone who did not check in sorts last
            # and is skipped by the eligibility test below anyway.
            candidates.sort(key=lambda r: (checkins.get(r.user_id) is None,
                                           checkins.get(r.user_id) or timezone.now()))
        for r in candidates:
            if promoted >= freed:
                break
            if r.user_id in checked:
                r.is_waitlisted = False
                r.save(update_fields=["is_waitlisted"])
                promoted += 1
        return promoted

    squads = list(TournamentTeam.objects.filter(
        event=event, is_waitlisted=True).order_by("registration_date"))
    rosters = {
        tt.tournament_team_id: list(
            TournamentTeamMember.objects.filter(tournament_team=tt)
            .exclude(status="rejected").values_list("user_id", flat=True))
        for tt in squads
    }
    if mode == "fcfs_room":
        # Ordered by when each squad BECAME ready, which is its last player's check-in.
        squads.sort(key=lambda tt: (
            _checked_in_order(rosters[tt.tournament_team_id]) is None,
            _checked_in_order(rosters[tt.tournament_team_id]) or timezone.now(),
        ))

    for tt in squads:
        if promoted >= freed:
            break
        roster = rosters[tt.tournament_team_id]
        # Same completeness rule relegation uses: a squad counts as checked in only when EVERY
        # rostered player did. An empty roster is not a team that can play, so it is skipped.
        if not roster or not all(uid in checked for uid in roster):
            continue
        tt.is_waitlisted = False
        tt.save(update_fields=["is_waitlisted"])
        # Same guard as relegate_unchecked_competitors above: a ghost has no RegisteredCompetitors
        # row, and team=None would otherwise catch every solo registration in the event.
        if tt.team_id:
            RegisteredCompetitors.objects.filter(
                event=event, team=tt.team, is_waitlisted=True).update(is_waitlisted=False)
        promoted += 1
    return promoted


@api_view(["POST"])
def checkin_relegate_now(request):
    """Admin/organizer force the relegation sweep NOW (also runs automatically once the window closes).
    Body: {event_id}. Returns how many competitors were moved to the waitlist."""
    user = _auth_user(request)
    if not user:
        return Response({"message": "Invalid or missing session token."}, status=401)
    event = get_object_or_404(Event, event_id=request.data.get("event_id"))
    if not _is_checkin_manager(user, event):
        return Response({"message": "You do not have permission."}, status=403)
    if not event.checkin_enabled:
        return Response({"message": "Check-in is not enabled for this event."}, status=400)
    if not event.checkin_end or timezone.now() < event.checkin_end:
        return Response({"message": "Check-in is still open; relegation runs after it closes."}, status=400)
    moved = relegate_unchecked_competitors(event)
    # Relegating empties a seat; this fills it. Both halves run together so an organizer pressing
    # the button once gets the whole rule, not the half that removes people.
    promoted = promote_checked_in_waitlist(event, moved)
    parts = [f"{moved} competitor(s) moved to the waitlist."]
    if promoted:
        parts.append(f"{promoted} checked-in waitlist entr{'y' if promoted == 1 else 'ies'} promoted into the freed slots.")
    elif moved:
        parts.append("No checked-in waitlist entry was available to take the freed slots.")
    return Response({
        "message": " ".join(parts),
        "relegated": moved,
        "promoted": promoted,
    })
