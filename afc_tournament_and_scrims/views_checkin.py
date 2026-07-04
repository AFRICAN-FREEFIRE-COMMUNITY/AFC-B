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


def _registration_end_dt(event):
    return _aware(_dt.combine(event.registration_end_date, event.registration_end_time or _time.max))


def _event_start_dt(event):
    return _aware(_dt.combine(event.start_date, event.event_start_time or _time.min))


def _window_open(event, now=None):
    """True when check-in is enabled and now is within [checkin_start, checkin_end]."""
    if not event.checkin_enabled or not event.checkin_start or not event.checkin_end:
        return False
    now = now or timezone.now()
    return event.checkin_start <= now <= event.checkin_end


# ── eligibility resolution ────────────────────────────────────────────────────────
def _user_squad(event, user):
    """The active (non-waitlisted) TournamentTeam this user is a registered roster member of for the
    event, or None. Squad events register per-team; membership lives in TournamentTeamMember."""
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
        if reg_end and start < reg_end:
            return Response({"message": "Check-in can only begin after registration ends."}, status=400)
        if ev_start and end > ev_start:
            return Response({"message": "Check-in must close before the event starts."}, status=400)
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
    user = _auth_user(request)
    if not user:
        return Response({"message": "Invalid or missing session token."}, status=401)
    event = get_object_or_404(Event, event_id=request.query_params.get("event_id"))

    base = {
        "checkin_enabled": event.checkin_enabled,
        "checkin_start": event.checkin_start.isoformat() if event.checkin_start else None,
        "checkin_end": event.checkin_end.isoformat() if event.checkin_end else None,
        "window_open": _window_open(event),
    }

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
        teams = []
        for tt in TournamentTeam.objects.filter(event=event).select_related("team"):
            roster = list(TournamentTeamMember.objects.filter(tournament_team=tt).exclude(status="rejected")
                          .values_list("user_id", flat=True))
            done = sum(1 for uid in roster if uid in checked_user_ids)
            teams.append({
                "tournament_team_id": tt.tournament_team_id,
                "team_name": tt.team.team_name if tt.team else "?",
                "is_waitlisted": tt.is_waitlisted,
                "roster_total": len(roster),
                "roster_checked_in": done,
                "eligible": (len(roster) > 0 and done == len(roster)),
            })
        solos = []
        for r in RegisteredCompetitors.objects.filter(event=event, user__isnull=False).select_related("user"):
            solos.append({
                "user_id": r.user_id,
                "username": r.user.username if r.user else "?",
                "is_waitlisted": r.is_waitlisted,
                "checked_in": r.user_id in checked_user_ids,
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
            RegisteredCompetitors.objects.filter(event=event, team=tt.team, is_waitlisted=False).update(is_waitlisted=True)
            moved += 1
    # Solos: relegate a user with no check-in.
    for r in RegisteredCompetitors.objects.filter(event=event, user__isnull=False, is_waitlisted=False):
        if r.user_id not in checked:
            r.is_waitlisted = True
            r.save(update_fields=["is_waitlisted"])
            moved += 1
    return moved


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
    return Response({"message": f"{moved} competitor(s) moved to the waitlist.", "relegated": moved})
