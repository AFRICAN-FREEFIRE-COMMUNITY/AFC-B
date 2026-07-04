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


def run_auto_seed(event):
    """Seed available teams into the entry stage's groups. Returns a dict describing what happened.
    Idempotent + safe: no-ops (and still stamps auto_seeded_at) when there is nothing to do or the
    stage is already seeded, so the sweep can call it repeatedly without harm."""
    result = {"seeded": 0, "groups": 0, "stage_id": None, "skipped": None}
    stage = _entry_stage(event)
    if stage is None:
        result["skipped"] = "no_stage"
        return result
    groups = list(StageGroups.objects.filter(stage=stage).order_by("group_id"))
    if not groups:
        result["skipped"] = "no_groups"
        return result
    # NEVER clobber an existing seed (manual or a previous auto run).
    if StageGroupCompetitor.objects.filter(stage_group__stage=stage).exists():
        result["skipped"] = "already_seeded"
        _stamp(event)
        return result

    teams = _available_teams(event)
    if not teams:
        result["skipped"] = "no_available_teams"
        _stamp(event)
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
        _stamp(event)
    return result


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
