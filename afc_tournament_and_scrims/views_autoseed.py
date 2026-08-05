# ── Fully-automatic event seeding (owner 2026-07-04) ─────────────────────────────
# When an event has auto_seed_on_start=True, the moment its start instant passes the daily status
# sweep calls run_auto_seed(event): it seeds the AVAILABLE teams into the entry stage's groups so the
# organizer only has to type each group's room ID + PASS. "Available" = registered + NOT waitlisted;
# and when check-in is enabled, only squads whose EVERY registered roster member checked in (the same
# eligibility the check-in feature enforces). Faithful to the manual team seed
# (seed_stage_competitors_to_groups_team): StageCompetitor per team, then a round-robin distribution
# into the stage's groups. NEVER re-seeds: it no-ops if the entry stage already has group competitors
# (seeded manually or a prior auto run), and stamps event.auto_seeded_at.
#
#   POST events/auto-seed/now/   auto_seed_now   (admin/organizer force it; the sweep also calls it)

import random

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    Event, Stages, StageGroups, TournamentTeam, TournamentTeamMember,
    StageCompetitor, StageGroupCompetitor, EventCheckIn,
)
from .views import _is_event_admin, org_can_event
from afc_auth.views import validate_token


def _entry_stage(event):
    """The stage teams enter first: the earliest in the canonical order (manual stage_order wins,
    else start_date, else stage_id) - mirrors how the app orders stages everywhere else."""
    return (Stages.objects.filter(event=event)
            .order_by("stage_order", "start_date", "stage_id")
            .first())


def _available_teams(event):
    """Registered, NON-waitlisted TournamentTeams that are eligible to be seeded. When check-in is on,
    a squad counts only if EVERY one of its active roster members has checked in (same rule as the
    check-in feature); when off, every non-waitlisted registered team is available."""
    teams = list(TournamentTeam.objects.filter(event=event, is_waitlisted=False).select_related("team"))
    if not event.checkin_enabled:
        return teams
    checked = set(EventCheckIn.objects.filter(event=event).values_list("user_id", flat=True))
    out = []
    for tt in teams:
        roster = list(TournamentTeamMember.objects.filter(tournament_team=tt)
                      .exclude(status="rejected").values_list("user_id", flat=True))
        if roster and all(uid in checked for uid in roster):
            out.append(tt)
    return out


def auto_seed_due_at(event):
    """The instant this event's automatic draw becomes due, or None when it cannot be worked out.

    Reads Event.auto_seed_trigger (owner 2026-08-05). None means "not due, and never will be from
    the data we have", so the caller skips the event rather than seeding it early. Returning the
    event start as a silent fallback for a missing date would be worse: an organizer who picked
    "when registration closes" would get a draw at a moment they did not ask for.

    THE EVENT'S OWN TIMEZONE, not the server's. The date and time columns store the HOST's wall
    clock, so combining them against `timezone.get_current_timezone()` gives the right answer only
    when the server happens to sit in the host's zone. That mistake has already cost this codebase
    twice: backlog item 38, where players in Ethiopia were told a live event was closed, and the
    transfer-window hint fixed earlier today.
    """
    from datetime import datetime as _dt, time as _time

    from .views import _event_zone

    zone = _event_zone(event)
    trigger = (getattr(event, "auto_seed_trigger", "") or "event_start").strip()

    def _combine(day, clock):
        if not day:
            return None
        try:
            return timezone.make_aware(_dt.combine(day, clock or _time.min), zone)
        except Exception:
            return None

    if trigger == "registration_close":
        return _combine(event.registration_end_date, getattr(event, "registration_end_time", None))

    if trigger == "checkin_close":
        # Only meaningful while check-in is switched ON. When it is not, fall through to the event
        # start rather than returning None: the organizer asked for an automatic draw, and a
        # trigger that can never fire would quietly mean "never seed at all".
        if getattr(event, "checkin_enabled", False) and getattr(event, "checkin_end", None):
            return event.checkin_end

    return _combine(event.start_date, getattr(event, "event_start_time", None))


def stages_to_seed(event):
    """The stages this event's automatic draw applies to, in play order.

    The stages an organizer has TICKED, and when they have ticked none, the entry stage on its own.
    That fallback is what keeps every event created before Stages.auto_seed existed behaving
    exactly as it did: auto-seed used to mean "the entry stage", full stop, so an empty selection
    has to keep meaning that rather than meaning "nothing".
    """
    chosen = list(
        Stages.objects.filter(event=event, auto_seed=True)
        .order_by("stage_order", "start_date", "stage_id"))
    if chosen:
        return chosen
    entry = _entry_stage(event)
    return [entry] if entry is not None else []


def run_auto_seed(event):
    """Seed available teams into every stage this event's draw covers.

    Returns a dict describing what happened, with a per-stage breakdown under "stages". Idempotent
    and safe: a stage that is already seeded is skipped rather than clobbered, so the sweep can
    call this repeatedly without harm.
    """
    stages = stages_to_seed(event)
    if not stages:
        _stamp(event)
        return {"seeded": 0, "groups": 0, "stage_id": None, "skipped": "no_stage", "stages": []}

    # The event-level numbers stay the FIRST stage's, because every existing caller and test reads
    # them and an event with one seeded stage is still the ordinary case.
    overall = None
    per_stage = []
    for stage in stages:
        outcome = _seed_one_stage(event, stage)
        per_stage.append(outcome)
        if overall is None:
            overall = dict(outcome)
    _stamp(event)
    overall["stages"] = per_stage
    return overall


def _seed_one_stage(event, stage):
    """Seed one stage. Everything this function does used to be the body of run_auto_seed."""
    result = {"seeded": 0, "groups": 0, "stage_id": None, "skipped": None}
    if stage is None:
        result["skipped"] = "no_stage"
        return result
    result["stage_id"] = stage.stage_id
    # auto_seed_include: an organizer can hold a group back to fill by hand, for example a
    # bracket-only or invitational group. Excluding every group is treated as "no groups" rather
    # than as an error, which is the same no-op the stage would get with none at all.
    groups = list(StageGroups.objects.filter(stage=stage, auto_seed_include=True)
                  .order_by("group_id"))
    if not groups:
        result["skipped"] = "no_groups"
        return result
    # NEVER clobber an existing seed (manual or a previous auto run).
    if StageGroupCompetitor.objects.filter(stage_group__stage=stage).exists():
        result["skipped"] = "already_seeded"
        _stamp_stage(stage)
        return result

    teams = _available_teams(event)
    if not teams:
        result["skipped"] = "no_available_teams"
        _stamp_stage(stage)
        return result

    with transaction.atomic():
        # 1) StageCompetitor per available team (skip any that already exist).
        existing = set(StageCompetitor.objects.filter(stage=stage, tournament_team__isnull=False)
                       .values_list("tournament_team_id", flat=True))
        for tt in teams:
            if tt.tournament_team_id not in existing:
                StageCompetitor.objects.create(stage=stage, tournament_team=tt, status="active")
        # 2) Round-robin the active stage competitors into the groups (shuffled), exactly like the
        #    manual team seed (seed_stage_competitors_to_groups_team).
        competitors = list(StageCompetitor.objects.filter(stage=stage, status="active",
                                                          tournament_team__isnull=False))
        random.shuffle(competitors)
        gcount = len(groups)
        entries = [StageGroupCompetitor(stage_group=groups[i % gcount],
                                        tournament_team=c.tournament_team)
                   for i, c in enumerate(competitors)]
        StageGroupCompetitor.objects.bulk_create(entries)
        result["seeded"] = len(entries)
        result["groups"] = gcount
        result["stage_id"] = stage.stage_id
        _stamp_stage(stage)
    return result


def _stamp_stage(stage):
    """Mark THIS stage as having had its automatic draw. Per stage, so seeding one never stops
    another from being seeded later, which is the whole point of the selection being per stage."""
    if not stage.auto_seeded_at:
        stage.auto_seeded_at = timezone.now()
        stage.save(update_fields=["auto_seeded_at"])


def _stamp(event):
    if not event.auto_seeded_at:
        event.auto_seeded_at = timezone.now()
        event.save(update_fields=["auto_seeded_at"])


@api_view(["POST"])
def auto_seed_now(request):
    """Admin/organizer force the auto-seed immediately (the sweep also runs it at start). Body:
    {event_id}. Gate: AFC event admin OR organizer with can_manage_registrations."""
    auth = request.headers.get("Authorization", "")
    user = validate_token(auth.split(" ")[1]) if auth.startswith("Bearer ") else None
    if not user:
        return Response({"message": "Invalid or missing session token."}, status=401)
    event = get_object_or_404(Event, event_id=request.data.get("event_id"))
    if not _is_event_admin(user) and not org_can_event(user, "can_manage_registrations", event):
        return Response({"message": "You do not have permission."}, status=403)
    res = run_auto_seed(event)
    if res["skipped"] == "already_seeded":
        return Response({"message": "The entry stage is already seeded.", **res}, status=400)
    if res["skipped"] in ("no_stage", "no_groups"):
        return Response({"message": "Create a stage with groups before auto-seeding.", **res}, status=400)
    if res["skipped"] == "no_available_teams":
        return Response({"message": "No available teams to seed (check registrations / check-in).", **res}, status=400)
    return Response({"message": f"Seeded {res['seeded']} team(s) across {res['groups']} group(s).", **res})
