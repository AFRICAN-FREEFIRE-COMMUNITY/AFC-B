from datetime import date
import json

from celery import shared_task
import requests
from afc import settings
from django.shortcuts import get_object_or_404, render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_date

from afc_auth.views import assign_discord_role, check_discord_membership, check_discord_membership_v3, discord_member_has_role, get_client_ip, remove_discord_role, validate_token
# from afc_leaderboard_calc.models import Match, MatchLeaderboard
from afc_team.models import Team, TeamMembers
# Single source of truth for the per-match point formula (see scoring.py). Imported
# as `scoring_lib` (not bare `scoring`) on purpose: several result-entry views already
# use a LOCAL variable named `scoring` for `match.scoring_settings`, which would shadow
# a bare module import and break `scoring.compute_*` inside those functions.
from afc_tournament_and_scrims import scoring as scoring_lib
from .models import Event, EventInviteToken, EventPageView, MatchResultImage, RegisteredCompetitors, RoundRobinGroup, SoloPlayerMatchStats, SponsorEvent, StageCompetitor, StageGroupCompetitor, StageGroups, Stages, StreamChannel, TournamentPlayerMatchStats, TournamentTeam, Leaderboard, TournamentTeamMatchStats, Match, TournamentTeamMember
from afc_auth.models import AdminHistory, BannedPlayer, DiscordRoleAssignment, DiscordStageRoleAssignmentProgress, LoginHistory, News, Notifications, Roles, User, UserRoles
# set_audit -> supply a SPECIFIC human audit summary (entity name + before/after) that the
# AuditLogMiddleware records, e.g. "Changed Detty December: event type from internal to external".
from afc_auth.audit import set_audit
# organizers: org-scope permission helpers + the Organization tenant model. These let org
# members (owner / sub_organizer) manage their OWN org's events while AFC admins keep full
# oversight. All gating goes through org_can / org_can_event so the owner/admin-bypass rules
# stay in afc_organizers/permissions.py (single source of truth).
from afc_organizers.permissions import org_can, org_can_event, is_platform_org_admin
from afc_organizers.models import Organization
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from datetime import datetime, date, timedelta
from django.utils import timezone
from django.db.models import Count, Q, F, Sum, Max
from django.db.models.functions import TruncDate
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
# Create your views here.

from rest_framework.pagination import PageNumberPagination

from afc_auth.views import send_email

def paginate_queryset(request, queryset, serializer_func):
    paginator = PageNumberPagination()
    paginator.page_size = int(request.GET.get("page_size", 10))  # default 10
    result_page = paginator.paginate_queryset(queryset, request)
    serialized = [serializer_func(obj) for obj in result_page]
    return paginator.get_paginated_response(serialized)


# ── event-admin check (AFC platform side) ──
# Single helper replacing the duplicated inline admin checks scattered across the event
# endpoints. True for the core staff roles OR users carrying the event_admin / head_admin
# granular role. Org-scope permissions (organizer owners / sub_organizers) are handled
# separately via org_can / org_can_event — this only answers "is this an AFC event admin?".
def _is_event_admin(user):
    # NOTE: UserRoles has no `role_name` field — the granular role name lives on the related
    # Roles row, so the correct lookup is `role__role_name__in` (the older inline gates in this
    # file use the buggy `role_name__in`, which FieldErrors for a non-staff event_admin; this
    # helper uses the correct path so org/event-admin checks actually work).
    return user.role in ["admin", "moderator", "support"] or \
        user.userroles.filter(role__role_name__in=["event_admin", "head_admin"]).exists()


from celery import shared_task
from django.utils import timezone

@shared_task
def update_event_and_stage_statuses():
    today = timezone.localdate()

    # EVENTS
    Event.objects.filter(is_draft=False, end_date__lt=today).exclude(event_status="completed").update(event_status="completed")
    Event.objects.filter(is_draft=False, start_date__lte=today, end_date__gte=today).exclude(event_status="ongoing").update(event_status="ongoing")
    Event.objects.filter(is_draft=False, start_date__gt=today).exclude(event_status="upcoming").update(event_status="upcoming")

    # STAGES
    Stages.objects.filter(end_date__lt=today).exclude(stage_status="completed").update(stage_status="completed")
    Stages.objects.filter(start_date__lte=today, end_date__gte=today).exclude(stage_status="ongoing").update(stage_status="ongoing")
    Stages.objects.filter(start_date__gt=today).exclude(stage_status="upcoming").update(stage_status="upcoming")



# @api_view(["POST"])
# def create_leaderboard(request):
#     session_token = request.headers.get("Authorization")
    
#     if not session_token:
#         return Response({"error": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

#     user = validate_token(session_token)
#     if not user:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     # Ensure only admins and moderators can create leaderboards
#     if user.role not in ["admin", "moderator"]:
#         return Response({"error": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)

#     leaderboard_name = request.data.get("leaderboard_name")
#     event_id = request.data.get("event_id")
#     stage = request.data.get("stage")
#     group = request.data.get("group", None)  # Optional field

#     if not all([leaderboard_name, event_id, stage]):
#         return Response({"error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except ObjectDoesNotExist:
#         return Response({"error": "Event not found"}, status=status.HTTP_404_NOT_FOUND)

#     leaderboard = Leaderboard.objects.create(
#         leaderboard_name=leaderboard_name,
#         event=event,
#         stage=stage,
#         group=group,
#         creator=user
#     )

#     return Response({"message": "Leaderboard created successfully", "leaderboard_id": leaderboard.leaderboard_id}, status=status.HTTP_201_CREATED)


# ── public event visibility (owner rule 2026-06-11) ──────────────────────────────────────────────
# An event is publicly visible only when it is published (is_draft=False) AND its owning organization
# is not hidden. A SUSPENDED or DELETED organization must vanish from every public surface, INCLUDING
# its events (the owner: "when they suspend an organization it should no longer show publicly, same for
# their events"). AFC-native events (organization IS NULL) are always allowed. Applied to every public
# event LIST + DETAIL endpoint below; admin/stats surfaces intentionally do NOT use it (admins still see
# everything). The org public page + the organizer directory already enforce the same rule on their side.
_ACTIVE_ORG_EVENT = Q(organization__isnull=True) | Q(organization__status="active")


def _org_hidden(event):
    """True when this event's owning org is suspended/deleted (so a public detail view must 404).
    AFC-native events (no org) are never hidden by this."""
    return bool(event.organization_id) and event.organization.status != "active"


@api_view(["GET"])
def get_all_events(request):
    # select_related("organization") avoids an N+1 when we read each event's org name/slug.
    # _ACTIVE_ORG_EVENT hides events whose org was suspended/deleted (keeps AFC + active-org events).
    events = Event.objects.filter(is_draft=False).filter(_ACTIVE_ORG_EVENT).select_related("organization")
    # optional org filter: when present, scope the list to one organization's events.
    organization_id = request.GET.get("organization_id")
    if organization_id:
        events = events.filter(organization_id=organization_id)
    event_list = []
    for event in events:
        event_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_banner": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
            "event_date": event.start_date,
            "event_status": event.event_status,
            "competition_type": event.competition_type,
            "number_of_participants": event.max_teams_or_players,
            "prizepool": event.prizepool,
            "prizepool_cash_value": event.prizepool_cash_value,
            "prize_distribution": event.prize_distribution,
            # Paid-registration: cards can show a "Paid" badge + the fee.
            "registration_type": event.registration_type,
            "registration_fee": event.registration_fee,
            "registration_fee_currency": event.registration_fee_currency,
            "total_registered_competitors": RegisteredCompetitors.objects.filter(event=event).count(),
            "slug": event.slug,
            "is_public": event.is_public,
            # organizer context: null for native AFC events, populated for org-owned events.
            "organization_id": event.organization_id,
            "organization_name": event.organization.name if event.organization_id else None,
            "organization_slug": event.organization.slug if event.organization_id else None,
            "rankings_verified": event.rankings_verified,
        })
    return Response({"events": event_list}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_events_paginated(request):
    limit = int(request.GET.get("limit", 10))
    offset = int(request.GET.get("offset", 0))

    # select_related("organization") avoids an N+1 when we read each event's org name/slug.
    events = Event.objects.filter(is_draft=False).filter(_ACTIVE_ORG_EVENT).select_related("organization").order_by("-start_date")
    # optional org filter: when present, scope the list to one organization's events.
    organization_id = request.GET.get("organization_id")
    if organization_id:
        events = events.filter(organization_id=organization_id)
    total = events.count()

    # slice manually (faster than Paginator for large tables)
    paginated = events[offset: offset + limit]

    event_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_banner": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
        "event_date": event.start_date,
        "event_status": event.event_status,
        "competition_type": event.competition_type,
        "number_of_participants": event.max_teams_or_players,
        "prizepool": event.prizepool,
        "prizepool_cash_value": event.prizepool_cash_value,
        "prize_distribution": event.prize_distribution,
        "total_registered_competitors": RegisteredCompetitors.objects.filter(event=event).count(),
        # organizer context: null for native AFC events, populated for org-owned events.
        "organization_id": event.organization_id,
        "organization_name": event.organization.name if event.organization_id else None,
        "organization_slug": event.organization.slug if event.organization_id else None,
        "rankings_verified": event.rankings_verified,
    } for event in paginated]

    return Response({
        "count": total,
        "limit": limit,
        "offset": offset,
        "next": offset + limit if offset + limit < total else None,
        "previous": offset - limit if offset - limit >= 0 else None,
        "events": event_list,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_tournaments_and_scrims(request):
    events = Event.objects.filter(is_draft=False).filter(_ACTIVE_ORG_EVENT)  # hide suspended-org events
    event_list = []
    for event in events:
        event_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.start_date,
            "event_status": event.event_status,
        })
    return Response({"events": event_list}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_tournaments_and_scrims_paginated(request):
    limit = int(request.GET.get("limit", 10))
    offset = int(request.GET.get("offset", 0))

    events = Event.objects.filter(is_draft=False).filter(_ACTIVE_ORG_EVENT).order_by("-start_date")  # hide suspended-org events
    total = events.count()

    paginated = events[offset: offset + limit]

    event_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_date": event.start_date,
        "event_status": event.event_status,
    } for event in paginated]

    return Response({
        "count": total,
        "limit": limit,
        "offset": offset,
        "next": offset + limit if offset + limit < total else None,
        "previous": offset - limit if offset - limit >= 0 else None,
        "events": event_list,
    }, status=status.HTTP_200_OK)



@api_view(["GET"])
def get_all_tournaments_and_scrims_separated(request):
    tournaments = Event.objects.filter(competition_type="tournament", is_draft=False).filter(_ACTIVE_ORG_EVENT)
    scrims = Event.objects.filter(competition_type="scrim", is_draft=False).filter(_ACTIVE_ORG_EVENT)

    tournament_list = []
    for event in tournaments:
        tournament_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.start_date,
            "event_status": event.event_status,
        })

    scrim_list = []
    for event in scrims:
        scrim_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.start_date,
            "event_status": event.event_status,
        })

    return Response({
        "tournaments": tournament_list,
        "scrims": scrim_list
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_tournaments_and_scrims_separated_paginated(request):
    limit = int(request.GET.get("limit", 10))
    offset = int(request.GET.get("offset", 0))

    tournaments = Event.objects.filter(competition_type="tournament", is_draft=False).filter(_ACTIVE_ORG_EVENT).order_by("-start_date")
    scrims = Event.objects.filter(competition_type="scrim", is_draft=False).filter(_ACTIVE_ORG_EVENT).order_by("-start_date")

    total_tournaments = tournaments.count()
    total_scrims = scrims.count()

    paginated_tournaments = tournaments[offset: offset + limit]
    paginated_scrims = scrims[offset: offset + limit]

    tournament_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_date": event.start_date,
        "event_status": event.event_status,
    } for event in paginated_tournaments]

    scrim_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_date": event.start_date,
        "event_status": event.event_status,
    } for event in paginated_scrims]

    return Response({
        "tournaments": {
            "count": total_tournaments,
            "limit": limit,
            "offset": offset,
            "next": offset + limit if offset + limit < total_tournaments else None,
            "previous": offset - limit if offset - limit >= 0 else None,
            "items": tournament_list
        },
        "scrims": {
            "count": total_scrims,
            "limit": limit,
            "offset": offset,
            "next": offset + limit if offset + limit < total_scrims else None,
            "previous": offset - limit if offset - limit >= 0 else None,
            "items": scrim_list
        }
    }, status=status.HTTP_200_OK)


import json
from datetime import date
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

def _maybe_json(val, default=None):
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default if default is not None else val
    return val

def _as_list(val):
    v = _maybe_json(val, default=[])
    return v if isinstance(v, list) else []


def _as_bool(val):
    """Coerce a form/JSON boolean: real bools pass through, strings accept the usual
    truthy spellings ("1"/"true"/"yes", any case). Missing/None/anything else -> False.
    Used by the event media-criteria toggles (require_team_logo / require_esport_images)."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes")
    return False


def _validate_scoring_modes(stages_data):
    """Validate the per-stage scoring-mode config (Champion-Point / Point-Rush) for a
    create/edit payload. `stages_data` is the submit-ordered list of stage dicts; the
    Point-Rush target is referenced by `point_rush_target_index` (0-based position in this
    same list). Returns an error message string on the first problem, or None if all good.

    Rules (scoring-modes sub-project A):
      • Champion-Point on  → champion_point_threshold must be a positive int.
      • Point-Rush on      → point_rush_reward must be a non-empty mapping AND a
                             point_rush_target_index must be supplied.
      • A Point-Rush stage may not target itself (carry-over only flows to a LATER stage).
    Callers turn the returned message into a 400 Response so no partial event is written."""
    n = len(stages_data)
    for idx, stage_data in enumerate(stages_data):
        # ── Champion-Point: needs a threshold > 0 ──
        if stage_data.get("champion_point_enabled"):
            threshold = stage_data.get("champion_point_threshold")
            try:
                threshold = int(threshold) if threshold not in (None, "") else 0
            except (TypeError, ValueError):
                threshold = 0
            if threshold <= 0:
                return "Champion-Point stages need a threshold > 0."

        # ── Point-Rush: needs a reward table AND a target stage index ──
        if stage_data.get("point_rush_enabled"):
            reward = stage_data.get("point_rush_reward")
            if not isinstance(reward, dict) or not reward:
                return "Point-Rush stages need a non-empty reward table."
            tgt_idx = stage_data.get("point_rush_target_index")
            if tgt_idx is None or tgt_idx == "":
                return "Point-Rush stages need a target stage to carry over into."
            try:
                tgt_idx = int(tgt_idx)
            except (TypeError, ValueError):
                return "Point-Rush target stage index must be a number."
            if tgt_idx == idx:
                return "A Point-Rush stage cannot carry over into itself."
            if not (0 <= tgt_idx < n):
                return "Point-Rush target stage index is out of range."
    return None


# ── BR Round-Robin (sub-project B, Task 4) ────────────────────────────────────────────
# The literal a stage's `stage_format` must equal for the round-robin builders below to
# fire. Distinct from the dead "br - roundrobin" (mislabelled "Knockout") choice; see the
# RoundRobinGroup / Stages.STAGE_FORMAT_CHOICES comments in models.py.
ROUND_ROBIN_FORMAT = "br - round robin"


def _validate_round_robin_groups(stages_data):
    """Validate the base-group team assignments in a create/edit payload (Task 4 landmine #3).

    The plan requires: "A team must belong to exactly one base group." `_build_round_robin_groups`
    and `_resolve_round_robin_team_ids` would otherwise silently accept the same team listed in
    two base groups, which corrupts per-group advancement (a team would sit in two groups) and
    the cumulative standings. So we guard it here, BEFORE the transaction opens, mirroring
    `_validate_scoring_modes`: only round-robin stages are checked; the first duplicate team
    found in two groups of the SAME stage returns an error message (callers turn it into a 400
    so no partial event is written). `stages_data` is the submit-ordered list of stage dicts.

    Returns an error message string on the first problem, or None if every round-robin stage is
    clean. (Unknown/blank team ids are ignored here — `_resolve_round_robin_team_ids` already
    skips them; we only police the uniqueness invariant the plan calls out.)
    """
    for stage_data in stages_data:
        if stage_data.get("stage_format") != ROUND_ROBIN_FORMAT:
            continue
        seen_team_ids = set()  # team ids already claimed by an earlier base group in this stage
        for grp_data in stage_data.get("round_robin_groups", []):
            for raw_id in grp_data.get("team_ids") or []:
                try:
                    team_id = int(raw_id)
                except (TypeError, ValueError):
                    continue  # mirror _resolve_round_robin_team_ids: skip un-parseable ids
                if team_id in seen_team_ids:
                    return (
                        "A team can only belong to one base group in a round-robin stage "
                        f"(team id {team_id} appears in more than one group)."
                    )
                seen_team_ids.add(team_id)
    return None


def _resolve_round_robin_team_ids(team_ids, event, user):
    """Turn the FE-supplied `team_ids` into THIS event's TournamentTeam rows.

    A base group carries `team_ids` (Team primary keys). The round-robin stage lives on a
    just-created/edited event, so a Team may not yet have a TournamentTeam for it — we
    create one on demand (status "active", registered_by=user), exactly the shape
    `register_*` uses elsewhere in this file. Already-linked teams are reused, so calling
    this for overlapping groups (or re-editing) never duplicates a TournamentTeam.

    Returns the TournamentTeam rows in the SAME order as `team_ids` (skipping ids whose
    Team doesn't exist — a bad id can't poison the whole stage build).
    """
    resolved = []
    for team_id in team_ids or []:
        try:
            team_id = int(team_id)
        except (TypeError, ValueError):
            continue
        team = Team.objects.filter(team_id=team_id).first()
        if not team:
            continue  # unknown team id → skip rather than 500 the whole create
        # get_or_create keys on (event, team) so a team already entered (e.g. via the
        # registration flow) is reused; only genuinely-new entries get a fresh TT.
        tt, _ = TournamentTeam.objects.get_or_create(
            event=event,
            team=team,
            defaults={"status": "active", "registered_by": user,
                      "country": team.country},
        )
        resolved.append(tt)
    return resolved


def _materialise_round_robin_lobby(stage, event, user, lobby_spec, source_groups, default_date, default_time):
    """Create ONE game-day lobby (a StageGroups row) for a round-robin stage.

    Mirrors the lobby/leaderboard/match creation already in create_event, plus the two
    round-robin extras: it stamps `game_day` + `source_groups` on the StageGroups and seeds
    StageGroupCompetitor from the UNION of the merged base groups' teams (so the lobby's
    roster is exactly the teams of the groups that were merged into it).

    `lobby_spec` is a dict shaped like a `round_robin_schedule` spec OR a manual game-day:
      {game_day, match_count, match_maps, group_name?(optional label)}.
    `source_groups` is the list of RoundRobinGroup rows this lobby merges.
    `default_date`/`default_time` fill StageGroups' required playing_date/time (a generated
    schedule has no per-lobby date, so we reuse the stage start as a sensible placeholder).
    """
    game_day = lobby_spec["game_day"]
    match_maps = list(lobby_spec.get("match_maps") or ["bermuda"])
    match_count = int(lobby_spec.get("match_count") or 0)

    # Default lobby name encodes the day + the merged group labels (e.g. "Day 1: A+B"),
    # unless the caller passed an explicit name (manual game-day mode may).
    group_name = lobby_spec.get("group_name") or (
        f"Day {game_day}: " + "+".join(g.label for g in source_groups)
    )

    lobby = StageGroups.objects.create(
        stage=stage,
        group_name=group_name,
        playing_date=default_date,
        playing_time=default_time,
        teams_qualifying=int(lobby_spec.get("teams_qualifying", stage.teams_qualifying_from_stage)),
        match_count=match_count,
        match_maps=match_maps,
        game_day=game_day,
    )
    # Stamp which base groups were merged into this lobby (the round-robin link).
    lobby.source_groups.set(source_groups)

    # Auto-create a leaderboard for the lobby (same shape as the normal-group path).
    leaderboard = Leaderboard.objects.create(
        leaderboard_name=f"{stage.stage_name} - {lobby.group_name}",
        event=event,
        stage=stage,
        group=lobby,
        creator=user,
        leaderboard_method="manual",
        placement_points={},
        kill_point=1.0,
    )

    # Create exactly match_count matches, cycling the lobby's maps (mirrors create_event).
    default_map = match_maps[0] if match_maps else "bermuda"
    matches_to_create = [
        Match(
            leaderboard=leaderboard,
            group=lobby,
            match_map=match_maps[(num - 1) % len(match_maps)] if match_maps else default_map,
            match_number=num,
        )
        for num in range(1, match_count + 1)
    ]
    if matches_to_create:
        Match.objects.bulk_create(matches_to_create, batch_size=500)

    # Seed the lobby's competitors from the UNION of its merged base groups' teams. A team
    # that sits in two merged groups (shouldn't happen — a team belongs to one base group —
    # but be defensive) collapses to a single competitor via the dedupe + ignore_conflicts.
    seen = set()
    sgc_rows = []
    for grp in source_groups:
        for tt in grp.teams.all():
            if tt.tournament_team_id in seen:
                continue
            seen.add(tt.tournament_team_id)
            sgc_rows.append(StageGroupCompetitor(
                stage_group=lobby, tournament_team=tt, status="active"))
    if sgc_rows:
        StageGroupCompetitor.objects.bulk_create(
            sgc_rows, batch_size=500, ignore_conflicts=True)

    return lobby


def _build_round_robin_groups(stage, event, user, stage_data, reuse_groups_by_label=None):
    """Create (or reuse) the base groups (A/B/C…) for a round-robin stage and wire their teams.

    `stage_data["round_robin_groups"]` is `[{label, order, team_ids}]`. Returns the resulting
    RoundRobinGroup rows in submit order so the caller can hand their ids to the schedule
    generator (order matters: it fixes which pairing falls on which game day).

    `reuse_groups_by_label` (edit path only): a `{label: RoundRobinGroup}` map of base groups
    that MUST survive because a played lobby still points at them (see `_edit_round_robin_stage`).
    A payload group whose label matches one of these REUSES that exact row (re-attaching teams +
    updating order) instead of creating a new one. Without this, the edit path would keep the
    protected rows AND create fresh duplicates of the same labels — corrupting the base-group set
    (Task 4 bug: 3 base groups → 5 with duplicate A/B). When None (create path), every group is
    created fresh as before.
    """
    reuse_groups_by_label = reuse_groups_by_label or {}
    created_groups = []
    for idx, grp_data in enumerate(stage_data.get("round_robin_groups", [])):
        label = grp_data.get("label") or chr(ord("A") + idx)  # A, B, C… fallback
        order = int(grp_data.get("order", idx))

        # Reuse a protected (played-lobby-sourced) group of this label so the played lobby keeps
        # pointing at the SAME row; otherwise create a fresh base group.
        grp = reuse_groups_by_label.get(label)
        if grp is not None:
            if grp.order != order:
                grp.order = order
                grp.save(update_fields=["order"])
        else:
            grp = RoundRobinGroup.objects.create(stage=stage, label=label, order=order)

        # Resolve the sent Team ids → this event's TournamentTeam rows and attach them. .set()
        # replaces the membership, so a reused group's roster is refreshed from the payload too.
        tts = _resolve_round_robin_team_ids(grp_data.get("team_ids"), event, user)
        grp.teams.set(tts)
        created_groups.append(grp)
    return created_groups


def _round_robin_lobby_is_played(lobby):
    """True if any match in this lobby has an entered result.

    Edit-time guard (Task 4 / landmine): regenerating the schedule must NOT wipe a lobby
    that already has results entered. result_inputted=True marks a match whose stats the
    admin has saved, so a lobby with ANY such match is "played" and stays untouched.
    """
    return lobby.matches.filter(result_inputted=True).exists()


def _edit_round_robin_stage(stage, event, user, stage_data):
    """Rebuild a round-robin stage's base groups + lobbies on EDIT, preserving played play.

    Mirrors `_build_round_robin_stage` but is non-destructive about real results:
      • lobbies with entered results (result_inputted=True on any match) are KEPT verbatim —
        their base groups, matches, stats and competitors are never touched;
      • every UNPLAYED lobby is deleted and regenerated from the (possibly edited) payload.

    Returns the StageGroups ids of every round-robin lobby that should survive the edit
    (kept-played + freshly-regenerated), so the caller can add them to `kept_group_ids` and
    stop `delete_missing` from sweeping the just-built lobbies away.
    """
    from . import round_robin  # local import: this file imports per-function throughout

    # Played lobbies are sacred — keep them and the base groups they reference.
    existing_lobbies = list(stage.groups.filter(game_day__isnull=False))
    played_lobbies = [lb for lb in existing_lobbies if _round_robin_lobby_is_played(lb)]
    kept_lobby_ids = [lb.group_id for lb in played_lobbies]

    # Base groups still referenced by a kept (played) lobby must survive too, so we don't
    # orphan a played lobby's source_groups. Everything else is rebuildable. We key these
    # protected groups by LABEL: the payload below re-sends the same labels (A/B/C…), and a
    # played lobby points at the protected A/B rows — so the regenerated lobbies must reuse
    # those exact rows, not freshly-created duplicates of the same labels (Task 4 bug:
    # recreating the full set alongside the survivors leaked 3 base groups → 5).
    protected_group_ids = set()
    protected_groups_by_label = {}
    for lb in played_lobbies:
        for src in lb.source_groups.all():
            protected_group_ids.add(src.group_id)
            protected_groups_by_label[src.label] = src

    # Delete the UNPLAYED lobbies (cascade drops their matches/leaderboard/competitors and
    # clears the source_groups M2M) so we can regenerate them cleanly below.
    unplayed_ids = [
        lb.group_id for lb in existing_lobbies if lb.group_id not in kept_lobby_ids]
    if unplayed_ids:
        StageGroups.objects.filter(group_id__in=unplayed_ids).delete()

    # Drop the base groups that are NOT protected by a played lobby. Protected ones stay so the
    # played lobbies keep their identity; the payload rebuild below REUSES them by label (rather
    # than recreating duplicates) and creates only the genuinely-new labels.
    stage.round_robin_groups.exclude(group_id__in=protected_group_ids).delete()

    # Rebuild the base groups from the payload (same helper as create), reusing the protected
    # rows for labels a played lobby still depends on. Result: exactly the payload's base-group
    # set (no duplicate labels), with the played lobby's groups preserved verbatim.
    created_groups = _build_round_robin_groups(
        stage, event, user, stage_data, reuse_groups_by_label=protected_groups_by_label)

    default_date = stage.start_date
    default_time = parse_time("19:00")

    # Regenerate lobbies for the (edited) base groups, but SKIP any game day already covered
    # by a kept played lobby so we never duplicate a day that has results.
    played_days = {lb.game_day for lb in played_lobbies}

    if stage_data.get("generate_schedule"):
        games_per_day = int(stage_data.get("games_per_day", 1) or 1)
        maps = stage_data.get("round_robin_maps") or stage_data.get("match_maps")
        specs = round_robin.round_robin_schedule(
            [g.group_id for g in created_groups], games_per_day=games_per_day, maps=maps)
        by_id = {g.group_id: g for g in created_groups}
        for spec in specs:
            if spec["game_day"] in played_days:
                continue  # that day already has a played lobby → don't overwrite it
            sources = [by_id[gid] for gid in spec["source_group_ids"] if gid in by_id]
            lobby = _materialise_round_robin_lobby(
                stage, event, user, spec, sources, default_date, default_time)
            kept_lobby_ids.append(lobby.group_id)
    else:
        for gd in stage_data.get("game_days", []):
            if gd.get("game_day") in played_days:
                continue
            sources = [
                created_groups[i]
                for i in gd.get("source_group_indexes", [])
                if isinstance(i, int) and 0 <= i < len(created_groups)
            ]
            if not sources:
                continue
            lobby = _materialise_round_robin_lobby(
                stage, event, user, gd, sources, default_date, default_time)
            kept_lobby_ids.append(lobby.group_id)

    return kept_lobby_ids


def _build_round_robin_stage(stage, event, user, stage_data):
    """Build a whole round-robin stage's groups + game-day lobbies on CREATE.

    Two ways to get lobbies (the FE picks one):
      • generate_schedule:true → run the pure `round_robin_schedule` over the base-group ids
        (in order) and materialise every C(n,2) pairing into a game-day lobby; or
      • game_days:[{game_day, source_group_indexes, match_count, match_maps}] → a MANUAL
        list of lobbies, each referencing base groups by their 0-based index in
        round_robin_groups (lets an admin hand-build the schedule instead of generating it).
    """
    from . import round_robin  # local import: this file imports per-function throughout

    created_groups = _build_round_robin_groups(stage, event, user, stage_data)

    # Reuse the stage's start date + a default lobby time for the generated lobbies (a
    # generated schedule carries no per-lobby date/time; the admin sets real ones on edit).
    # parse_time is imported at module level (used by edit_event); "19:00" is the standard
    # AFC evening lobby slot, just a placeholder the admin can override later.
    default_date = stage.start_date
    default_time = parse_time("19:00")

    if stage_data.get("generate_schedule"):
        games_per_day = int(stage_data.get("games_per_day", 1) or 1)
        maps = stage_data.get("round_robin_maps") or stage_data.get("match_maps")
        specs = round_robin.round_robin_schedule(
            [g.group_id for g in created_groups], games_per_day=games_per_day, maps=maps)
        # Map group_id → RoundRobinGroup so each spec's source_group_ids become rows.
        by_id = {g.group_id: g for g in created_groups}
        for spec in specs:
            sources = [by_id[gid] for gid in spec["source_group_ids"] if gid in by_id]
            _materialise_round_robin_lobby(
                stage, event, user, spec, sources, default_date, default_time)
    else:
        # Manual game-day list: each lobby names its source base groups by index.
        for gd in stage_data.get("game_days", []):
            sources = [
                created_groups[i]
                for i in gd.get("source_group_indexes", [])
                if isinstance(i, int) and 0 <= i < len(created_groups)
            ]
            if not sources:
                continue  # a lobby with no source groups has nothing to merge → skip
            _materialise_round_robin_lobby(
                stage, event, user, gd, sources, default_date, default_time)


def _round_robin_stage_echo(stage):
    """Echo a round-robin stage's structure so the FE event editor can rehydrate it.

    Returns None for any non-round-robin stage (so callers can drop the key for them), or a
    dict the stage builder reads back:
      {
        "round_robin_groups": [{group_id, label, order, team_ids, team_names}],  # base groups
        "game_days": [                                                            # lobbies by day
          {"game_day": N, "lobbies": [{group_id, source_group_ids}]}
        ],
      }
    `team_ids` are TournamentTeam ids (what the builder seeds from); `source_group_ids` are
    RoundRobinGroup ids — together they let the FE redraw the groups + the schedule exactly.
    """
    if stage.stage_format != ROUND_ROBIN_FORMAT:
        return None

    # Base groups A/B/C… (Meta.ordering = ["order"] keeps them in order). Echo each group's
    # member teams as both id (for seeding) and name (for display).
    groups_echo = []
    for grp in stage.round_robin_groups.all():
        teams = list(grp.teams.values("tournament_team_id", "team__team_name"))
        groups_echo.append({
            "group_id": grp.group_id,
            "label": grp.label,
            "order": grp.order,
            "team_ids": [t["tournament_team_id"] for t in teams],
            "team_names": [t["team__team_name"] for t in teams],
        })

    # Lobbies bucketed by game day, each with the base-group ids it merged.
    days = {}
    lobbies = stage.groups.filter(game_day__isnull=False).order_by("game_day", "group_id")
    for lobby in lobbies:
        days.setdefault(lobby.game_day, []).append({
            "group_id": lobby.group_id,
            "source_group_ids": list(
                lobby.source_groups.values_list("group_id", flat=True)),
        })
    game_days_echo = [
        {"game_day": day, "lobbies": lobby_list}
        for day, lobby_list in sorted(days.items())
    ]

    return {"round_robin_groups": groups_echo, "game_days": game_days_echo}


@api_view(["POST"])
def create_event(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]
    user = validate_token(token)
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # ── org-aware permission gate ──
    # AFC event admins can always create native (org=None) events. If an organization_id is
    # supplied, the request is an organizer creating an event under THEIR org — allow it only
    # when the user is an AFC admin OR has can_create_events on that org (org_can also lets
    # owners + platform admins through). No org_id + not an AFC admin → blocked.
    is_admin_creator = _is_event_admin(user)
    org = None
    organization_id = request.data.get("organization_id")
    if organization_id:
        org = Organization.objects.filter(organization_id=organization_id).first()
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        if not (is_admin_creator or org_can(user, "can_create_events", org)):
            return Response({"message": "You do not have permission to create events for this organization."}, status=403)
    elif not is_admin_creator:
        return Response({"message": "You do not have permission to create an event."}, status=403)

    # ---------------- REQUIRED FIELDS ----------------
    required_fields = [
        "competition_type", "participant_type", "event_type",
        "max_teams_or_players", "event_name",
        "event_mode", "start_date", "end_date",
        "registration_open_date", "registration_end_date",
        "prizepool", "number_of_stages", "is_draft"
    ]
    for field in required_fields:
        if field not in request.data:
            return Response({"message": f"Missing field: {field}"}, status=400)

    is_sponsored = request.data.get("is_sponsored", False)
    if isinstance(is_sponsored, str):
        is_sponsored = is_sponsored.lower() in ("1", "true", "yes")

    sponsor_usernames = request.data.get("sponsor_usernames", [])
    sponsor_name = request.data.get("sponsor_name")
    sponsor_field_label = request.data.get("sponsor_field_label")
    sponsor_requirement_description = request.data.get("sponsor_requirement_description")

    if is_sponsored:
        if not sponsor_name:
            return Response({"message": "sponsor_name is required for sponsored events."}, status=400)
        
        if not sponsor_usernames:
            return Response({"message": "sponsor_usernames is required for sponsored events."}, status=400)

        if not sponsor_field_label:
            return Response({"message": "sponsor_field_label is required for sponsored events."}, status=400)

    
    if is_waitlist_enabled := request.data.get("is_waitlist_enabled", False):
        if isinstance(is_waitlist_enabled, str):
            is_waitlist_enabled = is_waitlist_enabled.lower() in ("1", "true", "yes")
        waitlist_capacity = request.data.get("waitlist_capacity")
        waitlist_discord_role_id = request.data.get("waitlist_discord_role_id")
        if is_waitlist_enabled:
            if not waitlist_capacity:
                return Response({"message": "waitlist_capacity is required when waitlist is enabled."}, status=400)
            try:
                waitlist_capacity = int(waitlist_capacity)
            except ValueError:
                return Response({"message": "waitlist_capacity must be an integer."}, status=400)
            if waitlist_capacity <= 0:
                return Response({"message": "waitlist_capacity must be greater than 0."}, status=400)
            # waitlist_discord_role_id is optional

    # ---------------- PARSE DATES ----------------
    start_date = parse_date(request.data.get("start_date"))
    end_date = parse_date(request.data.get("end_date"))
    open_date = parse_date(request.data.get("registration_open_date"))
    close_date = parse_date(request.data.get("registration_end_date"))

    if not start_date or not end_date or not open_date or not close_date:
        return Response({"message": "Invalid date format provided."}, status=400)

    if open_date > close_date:
        return Response({"message": "Registration open date cannot be after registration end date."}, status=400)

    if start_date > end_date:
        return Response({"message": "Event start date cannot be after event end date."}, status=400)

    # ---------------- PRIZEPOOL ----------------
    
    prizepool = request.data.get("prizepool")
    

    try:
        prizepool_cash_value = float(request.data.get("prizepool_cash_value", 0) or 0)
    except Exception:
        return Response({"message": "prizepool_cash_value must be a number."}, status=400)

    # ---------------- PRIZE DISTRIBUTION ----------------
    prize_distribution = _maybe_json(request.data.get("prize_distribution"), default={})
    if not isinstance(prize_distribution, dict):
        return Response({"message": "prize_distribution must be a JSON object."}, status=400)

    # ---------------- REGISTRATION RESTRICTION ----------------
    registration_restriction = request.data.get("registration_restriction", "none")
    restriction_mode = request.data.get("restriction_mode")  # allow_only / block_selected

    # restricted_regions = _as_list(request.data.get("restricted_regions"))
    restricted_countries = _as_list(request.data.get("restricted_countries"))

    if registration_restriction not in ["none", "by_region", "by_country"]:
        return Response({"message": "registration_restriction must be one of: none, by_region, by_country."}, status=400)

    if registration_restriction == "none":
        restriction_mode = None
        restricted_regions = []
        restricted_countries = []
    else:
        if restriction_mode not in ["allow_only", "block_selected"]:
            return Response({"message": "restriction_mode must be allow_only or block_selected."}, status=400)

        if registration_restriction == "by_region":
        #     if not restricted_regions:
        #         return Response({"message": "restricted_regions is required when registration_restriction=by_region."}, status=400)

        #     # You’ll enforce using countries, so countries MUST be sent too (final list after frontend removals)
            if not restricted_countries:
                return Response({"message": "restricted_countries is required when restricting by region (final selected countries list)."}, status=400)

        if registration_restriction == "by_country":
            if not restricted_countries:
                return Response({"message": "restricted_countries is required when registration_restriction=by_country."}, status=400)
            # regions optional here
            restricted_regions = []

    # ---------------- STREAM CHANNELS ----------------
    stream_channels = _as_list(request.data.get("stream_channels"))

    # ---------------- STAGES DATA ----------------
    stages_data = _as_list(request.data.get("stages"))

    # Validate the per-stage scoring-mode config BEFORE the transaction so a bad payload
    # fails fast with a 400 and never writes a half-built event.
    scoring_mode_error = _validate_scoring_modes(stages_data)
    if scoring_mode_error:
        return Response({"message": scoring_mode_error}, status=400)

    # Same pre-transaction guard for round-robin base groups: a team must belong to exactly
    # one base group (Task 4 landmine #3). Fails fast with a 400 before any write.
    round_robin_groups_error = _validate_round_robin_groups(stages_data)
    if round_robin_groups_error:
        return Response({"message": round_robin_groups_error}, status=400)

    is_draft = request.data.get("is_draft", True)
    if isinstance(is_draft, str):
        is_draft = is_draft.lower() in ("1", "true", "yes")

    is_public = request.data.get("is_public", True)
    if isinstance(is_public, str):
        is_public = is_public.lower() in ("1", "true", "yes")

    # ── Paid registration parse + validate (feature "paid-events", 2026-06-08) ──
    # "free" keeps instant registration; "paid" requires a positive fee, and for an
    # organizer-owned event the org must have accepted the paid-event terms first (recorded on
    # the Organization). The actual charge/escrow is the payment phase; here we only persist the
    # fee config + gate the terms acceptance.
    from decimal import Decimal, InvalidOperation
    registration_type = request.data.get("registration_type", "free")
    registration_fee_currency = (request.data.get("registration_fee_currency") or "USD").upper()[:3]
    registration_fee = None
    if registration_type not in ("free", "paid"):
        return Response({"error": "registration_type must be 'free' or 'paid'."}, status=400)
    if registration_type == "paid":
        try:
            registration_fee = Decimal(str(request.data.get("registration_fee")))
        except (InvalidOperation, TypeError):
            return Response({"error": "A valid registration_fee is required for a paid event."}, status=400)
        if registration_fee <= 0:
            return Response({"error": "registration_fee must be greater than 0 for a paid event."}, status=400)
        # Organizer paid events: require + record acceptance of the paid-event terms (once per org).
        if org is not None and not org.paid_terms_accepted_at:
            accepted = str(request.data.get("paid_terms_accepted", "")).lower() in ("true", "1", "yes")
            if not accepted:
                return Response(
                    {"error": "You must accept the paid-event terms before creating a paid event.",
                     "code": "paid_terms_required"},
                    status=400,
                )
            org.paid_terms_accepted_at = timezone.now()
            org.paid_terms_accepted_by = user
            org.paid_terms_version = "2026-06-08"
            org.save(update_fields=["paid_terms_accepted_at", "paid_terms_accepted_by", "paid_terms_version"])

    # ---------------- CREATE EVERYTHING ----------------
    with transaction.atomic():
        event = Event.objects.create(
            registration_type=registration_type,
            registration_fee=registration_fee,
            registration_fee_currency=registration_fee_currency,
            competition_type=request.data.get("competition_type"),
            participant_type=request.data.get("participant_type"),
            event_type=request.data.get("event_type"),
            max_teams_or_players=int(request.data.get("max_teams_or_players")),
            event_name=request.data.get("event_name"),
            event_mode=request.data.get("event_mode"),
            start_date=start_date,
            end_date=end_date,
            registration_open_date=open_date,
            registration_end_date=close_date,
            prizepool=str(prizepool),  # your model uses CharField
            prizepool_cash_value=prizepool_cash_value,
            prize_distribution=prize_distribution,
            event_rules=request.data.get("event_rules", ""),
            event_status=request.data.get("event_status", "upcoming"),
            registration_link=request.data.get("registration_link", ""),
            tournament_tier=request.data.get("tournament_tier", "tier_3"),
            event_banner=request.FILES.get("event_banner"),
            number_of_stages=int(request.data.get("number_of_stages")),
            uploaded_rules=request.FILES.get("uploaded_rules"),
            is_draft=is_draft,
            creator = user,
            organization=org,  # owning org for organizer-created events; None for native AFC events

            # ✅ restriction fields
            registration_restriction=registration_restriction,
            restriction_mode=restriction_mode,
            # restricted_regions=restricted_regions,
            restricted_countries=restricted_countries,
            is_public = is_public,
            is_sponsored=is_sponsored,
            sponsor_name=sponsor_name,
            sponsor_field_label=sponsor_field_label,
            sponsor_requirement_description=sponsor_requirement_description,
            event_start_time=request.data.get("event_start_time") or None,
            event_end_time=request.data.get("event_end_time") or None,
            registration_start_time=request.data.get("registration_start_time") or None,
            registration_end_time=request.data.get("registration_end_time") or None,

            # ── Media registration criteria (owner 2026-06-12) ── booleans arrive as
            # "true"/"1"/bool from the wizard toggles; enforced in register_for_event.
            require_team_logo=_as_bool(request.data.get("require_team_logo")),
            require_esport_images=_as_bool(request.data.get("require_esport_images")),
        )

        # change sponsor_usernames to a list
        sponsor_usernames = _as_list(sponsor_usernames)
        for sponsor_username in sponsor_usernames:
            try:
                sponsor_user = User.objects.get(username=sponsor_username)
                SponsorEvent.objects.create(
                    event=event,
                    sponsor=sponsor_user
                )
            except User.DoesNotExist:
                return Response({"message": f"Sponsor user '{sponsor_username}' not found."}, status=400)

        # stream channels
        if stream_channels:
            StreamChannel.objects.bulk_create(
                [StreamChannel(event=event, channel_url=url) for url in stream_channels if url],
                batch_size=200
            )

        # stages + groups + matches
        # Track every created Stages row in submit order so we can wire Point-Rush
        # carry-over targets in a SECOND PASS below (a stage may point at a LATER stage
        # that does not exist yet while we are still building this one).
        created_stages = []
        for stage_data in stages_data:
            stage = Stages.objects.create(
                event=event,
                stage_name=stage_data["stage_name"],
                start_date=parse_date(stage_data["start_date"]),
                end_date=parse_date(stage_data["end_date"]),
                number_of_groups=int(stage_data["number_of_groups"]),
                stage_format=stage_data["stage_format"],
                teams_qualifying_from_stage=int(stage_data["teams_qualifying_from_stage"]),
                stage_discord_role_id=stage_data.get("stage_discord_role_id"),
                prizepool=stage_data.get("prizepool"),
                prizepool_cash_value=stage_data.get("prizepool_cash_value") if stage_data.get("prizepool_cash_value") else 0,
                prize_distribution=stage_data.get("prize_distribution", {}),
                # ── Scoring-mode config (scoring-modes sub-project A). The 4 scalars store
                # directly; point_rush_target_stage is resolved in the second pass below. ──
                champion_point_enabled=bool(stage_data.get("champion_point_enabled", False)),
                champion_point_threshold=stage_data.get("champion_point_threshold") or None,
                point_rush_enabled=bool(stage_data.get("point_rush_enabled", False)),
                point_rush_reward=stage_data.get("point_rush_reward") or {},
            )
            created_stages.append(stage)

            # ── BR Round-Robin (sub-project B, Task 4): a round-robin stage sends BASE
            # groups (round_robin_groups) instead of plain `groups`, and we build the base
            # groups + game-day lobbies from them. The normal `groups` loop below then runs
            # over an empty list, so the two paths don't collide. ──
            if stage_data.get("stage_format") == ROUND_ROBIN_FORMAT:
                _build_round_robin_stage(stage, event, user, stage_data)

            for group_data in stage_data.get("groups", []):
                group = StageGroups.objects.create(
                    stage=stage,
                    group_name=group_data["group_name"],
                    playing_date=parse_date(group_data["playing_date"]),
                    playing_time=group_data["playing_time"],
                    teams_qualifying=int(group_data["teams_qualifying"]),
                    group_discord_role_id=group_data.get("group_discord_role_id"),
                    match_count=int(group_data.get("match_count", 0)),
                    match_maps=group_data.get("match_maps", []),
                    prizepool=group_data.get("prizepool"),
                    prizepool_cash_value=group_data.get("prizepool_cash_value"),
                    prize_distribution=group_data.get("prize_distribution", {})
                )

                # Auto-create a leaderboard for this group
                leaderboard = Leaderboard.objects.create(
                    leaderboard_name=f"{stage.stage_name} - {group.group_name}",
                    event=event,
                    stage=stage,
                    group=group,
                    creator=user,
                    leaderboard_method="manual",
                    placement_points={},
                    kill_point=1.0,
                )

                # Create exactly match_count matches, cycle maps if provided
                match_count = group.match_count or 0
                match_maps = group.match_maps or []
                default_map = match_maps[0] if match_maps else "bermuda"

                matches_to_create = []
                for num in range(1, match_count + 1):
                    chosen_map = match_maps[(num - 1) % len(match_maps)] if match_maps else default_map
                    matches_to_create.append(Match(
                        leaderboard=leaderboard,
                        group=group,
                        match_map=chosen_map,
                        match_number=num
                    ))
                if matches_to_create:
                    Match.objects.bulk_create(matches_to_create, batch_size=500)

        # ── Second pass: wire Point-Rush carry-over targets now that every Stages row
        # exists. The FE sends point_rush_target_index = the 0-based position of the target
        # stage in the submitted `stages` array, so we zip created_stages (built in submit
        # order above) against stages_data. (Self-target / out-of-range were already rejected
        # by _validate_scoring_modes before the transaction.) ──
        for src_stage, stage_data in zip(created_stages, stages_data):
            tgt_idx = stage_data.get("point_rush_target_index")
            if src_stage.point_rush_enabled and tgt_idx is not None and tgt_idx != "":
                tgt_idx = int(tgt_idx)
                if 0 <= tgt_idx < len(created_stages):
                    src_stage.point_rush_target_stage = created_stages[tgt_idx]
                    src_stage.save(update_fields=["point_rush_target_stage"])

        AdminHistory.objects.create(
            admin_user=user,
            action="create_event",
            description=f"Created event {event.event_name} (ID: {event.event_id})"
        )
        set_audit(request, f"Created the event {event.event_name}")

    return Response({
        "message": "Event created successfully.",
        "event_id": event.event_id
    }, status=201)


# ── EVENT DUPLICATION (feature "event-duplicate", 2026-06-10) ──────────────────────────
@api_view(["POST"])
def duplicate_event(request, event_id):
    """Clone an existing event's CONFIG + stage/group/round-robin STRUCTURE into a fresh draft.

    PURPOSE
        Let an organizer (or an AFC admin) spin up a new tournament from a previous one's
        setup without re-entering every field. The clone copies the event's configuration and
        its stage -> group -> round-robin-base-group skeleton, but starts EMPTY: no results,
        no registrations, no teams, no matches. Mirrors how create_event builds the same tree
        (same field set, same Point-Rush second-pass target resolution, same slug de-dupe).

    REQUEST
        POST events/<int:event_id>/duplicate-event/
        Path param: event_id = the SOURCE event to clone. No body required.
        Auth header: Authorization: Bearer <session token>.

    RESPONSE
        201 {"message", "event_id", "slug", "event_name"} -> the NEW draft event.
        400 missing/invalid token. 401 expired token. 403 no permission. 404 source not found.

    AUTH / PERMISSION
        Bearer SessionToken (validate_token). The actor may duplicate the source event if they
        are an AFC event admin (_is_event_admin) OR the event belongs to an organization and
        the actor has can_create_events on THAT org (org_can). An organizer therefore can only
        clone events owned by an org they may create events for; native AFC events
        (organization=None) are admin-only. Same gate shape as create_event's create path.

    CLONES (config + structure only)
        • Event: every config field is copied; identity + lifecycle are RESET — new event_id,
          fresh unique slug (base "-copy" then "-2", "-3"... like create_event), creator=actor,
          is_draft=True, is_public=False, event_status="upcoming", rankings_verified=False,
          partner_published=False. organization is KEPT. event_name gets a " (Copy)" suffix.
          Media (event_banner / uploaded_rules) reference the SAME stored file path (the file is
          not re-uploaded); this matches create_event's default of just storing the path.
        • Stages: config only (names, dates, format, qualifying counts, prizepool, the 4
          scoring-mode scalars). Point-Rush carry-over targets are re-pointed at the NEW stages
          in a SECOND pass (old stage -> new stage, matched by submit/index order), exactly like
          create_event resolves point_rush_target_index.
        • StageGroups: config only (names, dates/times, qualifying, match_count, match_maps,
          prizepool). NO leaderboard/match rows are created (unlike create_event, which seeds an
          empty leaderboard + matches — a clone starts with nothing on the results side).
        • RoundRobinGroup: base-group skeleton (label, order) is cloned WITHOUT its teams, and
          game-day lobby groups are skipped (they reference TournamentTeam rows we never copy).

    DOES NOT CLONE
        RegisteredCompetitors, TournamentTeam / TournamentTeamMember, Match and all match stats
        (Tournament/SoloPlayer/Team), Leaderboard, EventRegistrationPayment, EventInviteToken,
        SponsorEvent, StreamChannel, EventPageView, SocialShare. The source event is untouched.

    FRONTEND CONSUMER
        The "Duplicate" row/card action on the organizer events list
        (app/(organizer)/organizer/events/page.tsx) and the admin events list
        (app/(a)/a/events/page.tsx), via lib/api/events.duplicateEvent(eventId). On success the
        FE toasts and routes to the new event's edit page (using the returned event_id/slug).
    """
    # ---------------- AUTH ----------------
    # Same bearer-token shape as create_event / edit_event (no DRF auth classes are wired).
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]
    user = validate_token(token)
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # ---------------- LOAD SOURCE ----------------
    source = Event.objects.filter(event_id=event_id).first()
    if not source:
        return Response({"message": "Event not found."}, status=404)

    # ── permission gate (org-aware) ──
    # AFC event admins may duplicate ANY event. Otherwise the event must belong to an org the
    # actor may create events for (org_can also lets owners + platform admins through). Native
    # AFC events (organization=None) for a non-admin fall through to 403. Mirrors the create
    # path's gate so "who can clone" == "who could have created it under this org".
    is_admin_actor = _is_event_admin(user)
    if not is_admin_actor:
        if source.organization_id is None or not org_can(user, "can_create_events", source.organization):
            return Response(
                {"message": "You do not have permission to duplicate this event."}, status=403
            )

    # ---------------- DEEP COPY ----------------
    # One transaction so a partially-built clone can never be left behind (same guarantee as
    # create_event). Nothing in here touches the network (no Discord/Stripe/email), so a clone
    # is a pure DB operation.
    with transaction.atomic():
        # ── 1) Clone the Event row: copy every CONFIG field, RESET identity + lifecycle ──
        # Field list mirrors create_event's Event.objects.create(...) so the two stay in lockstep
        # (any new config field added to create_event should be added here too).
        new_event = Event.objects.create(
            registration_type=source.registration_type,
            registration_fee=source.registration_fee,
            registration_fee_currency=source.registration_fee_currency,
            competition_type=source.competition_type,
            participant_type=source.participant_type,
            event_type=source.event_type,
            max_teams_or_players=source.max_teams_or_players,
            # " (Copy)" makes the clone obvious in the events list; trimmed to the field's
            # 40-char max so a long source name can't overflow event_name.
            event_name=(f"{source.event_name} (Copy)")[:40],
            event_mode=source.event_mode,
            start_date=source.start_date,
            end_date=source.end_date,
            registration_open_date=source.registration_open_date,
            registration_end_date=source.registration_end_date,
            prizepool=source.prizepool,
            prizepool_cash_value=source.prizepool_cash_value,
            prize_distribution=source.prize_distribution,
            event_rules=source.event_rules,
            # Lifecycle RESET: a clone is always a fresh upcoming draft.
            event_status="upcoming",
            registration_link=source.registration_link,
            tournament_tier=source.tournament_tier,
            prize_currency=source.prize_currency,
            usd_to_ngn_rate=source.usd_to_ngn_rate,
            prizepool_ngn_value=source.prizepool_ngn_value,
            # Media: reference the SAME stored file path (don't re-upload the file). create_event
            # defaults these from request.FILES; here we carry the existing file reference.
            event_banner=source.event_banner,
            number_of_stages=source.number_of_stages,
            uploaded_rules=source.uploaded_rules,
            creator=user,                       # the actor owns the clone
            organization=source.organization,   # KEEP the owning org (null = native AFC event)
            is_draft=True,                      # always a draft
            is_public=False,                    # private until the owner publishes
            rankings_verified=False,            # reset the rankings gate
            partner_published=False,            # reset the partner-API gate
            registration_restriction=source.registration_restriction,
            restriction_mode=source.restriction_mode,
            restricted_countries=source.restricted_countries,
            is_sponsored=source.is_sponsored,
            sponsor_name=source.sponsor_name,
            sponsor_field_label=source.sponsor_field_label,
            sponsor_requirement_description=source.sponsor_requirement_description,
            is_waitlist_enabled=source.is_waitlist_enabled,
            waitlist_capacity=source.waitlist_capacity,
            waitlist_discord_role_id=source.waitlist_discord_role_id,
            event_start_time=source.event_start_time,
            event_end_time=source.event_end_time,
            registration_start_time=source.registration_start_time,
            registration_end_time=source.registration_end_time,
        )

        # ── 2) Clone each Stage (config only) ──
        # Track the source stage -> new stage mapping so the Point-Rush SECOND PASS can re-point
        # carry-over targets at the NEW stages (a target stage may not exist yet on the first
        # pass), exactly how create_event uses created_stages + point_rush_target_index.
        old_stages = list(source.stages.order_by("stage_id"))
        old_to_new_stage = {}
        for old_stage in old_stages:
            new_stage = Stages.objects.create(
                event=new_event,
                stage_name=old_stage.stage_name,
                start_date=old_stage.start_date,
                end_date=old_stage.end_date,
                number_of_groups=old_stage.number_of_groups,
                stage_format=old_stage.stage_format,
                teams_qualifying_from_stage=old_stage.teams_qualifying_from_stage,
                stage_discord_role_id=old_stage.stage_discord_role_id,
                # Stage status resets with the event — a cloned stage hasn't run.
                stage_status="upcoming",
                prizepool=old_stage.prizepool,
                prizepool_cash_value=old_stage.prizepool_cash_value,
                prize_distribution=old_stage.prize_distribution,
                is_finals_stage=old_stage.is_finals_stage,
                # Scoring-mode scalars copy directly; point_rush_target_stage is resolved below.
                champion_point_enabled=old_stage.champion_point_enabled,
                champion_point_threshold=old_stage.champion_point_threshold,
                point_rush_enabled=old_stage.point_rush_enabled,
                point_rush_reward=old_stage.point_rush_reward,
            )
            old_to_new_stage[old_stage.stage_id] = new_stage

            # ── 2a) Clone the StageGroups under this stage (config only, NO leaderboard/matches) ──
            for old_group in old_stage.groups.order_by("group_id"):
                # Skip round-robin game-day LOBBY rows: they carry game_day + source_groups that
                # point at TournamentTeam-backed base groups we don't copy. Only the plain
                # config groups are cloned here; round-robin base groups are handled in 2b.
                if old_group.game_day is not None:
                    continue
                StageGroups.objects.create(
                    stage=new_stage,
                    group_name=old_group.group_name,
                    playing_date=old_group.playing_date,
                    playing_time=old_group.playing_time,
                    teams_qualifying=old_group.teams_qualifying,
                    group_discord_role_id=old_group.group_discord_role_id,
                    match_count=old_group.match_count,
                    match_maps=old_group.match_maps,
                    prizepool=old_group.prizepool,
                    prizepool_cash_value=old_group.prizepool_cash_value,
                    prize_distribution=old_group.prize_distribution,
                )

            # ── 2b) Clone the round-robin BASE-GROUP skeleton (label + order) WITHOUT teams ──
            # RoundRobinGroup.teams is a M2M of TournamentTeam rows (never copied), so we clone
            # only the empty A/B/C structure. Game-day lobbies are intentionally NOT recreated
            # (they merge teams) — the owner regenerates the schedule after re-seeding teams.
            for old_rr in old_stage.round_robin_groups.order_by("order"):
                RoundRobinGroup.objects.create(
                    stage=new_stage,
                    label=old_rr.label,
                    order=old_rr.order,
                )

        # ── 3) SECOND PASS: re-point Point-Rush carry-over targets at the NEW stages ──
        # Same idea as create_event's second pass, but keyed on the source stage's existing
        # target rather than a submitted index. on_delete=SET_NULL means a missing mapping
        # just nulls the link instead of pointing back at the source event.
        for old_stage in old_stages:
            if old_stage.point_rush_enabled and old_stage.point_rush_target_stage_id:
                new_src = old_to_new_stage.get(old_stage.stage_id)
                new_tgt = old_to_new_stage.get(old_stage.point_rush_target_stage_id)
                if new_src and new_tgt:
                    new_src.point_rush_target_stage = new_tgt
                    new_src.save(update_fields=["point_rush_target_stage"])

        # Audit trail, matching create_event (AdminHistory row + a human audit summary).
        AdminHistory.objects.create(
            admin_user=user,
            action="duplicate_event",
            description=f"Duplicated event {source.event_name} (ID: {source.event_id}) "
                        f"into {new_event.event_name} (ID: {new_event.event_id})",
        )
        set_audit(request, f"Duplicated the event {source.event_name} into {new_event.event_name}")

    return Response({
        "message": "Event duplicated successfully.",
        "event_id": new_event.event_id,
        "slug": new_event.slug,
        "event_name": new_event.event_name,
    }, status=201)


# @api_view(["POST"])
# def create_event(request):
#     # Retrieve session token
#     session_token = request.headers.get("Authorization")

#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]

#     # Authenticate user
#     user = validate_token(token)
#     if not user:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     # Permissions
#     if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
#         return Response({"message": "You do not have permission to create an event."}, status=403)

#     # Extract event data
#     required_fields = [
#         "competition_type", "participant_type", "event_type",
#         "max_teams_or_players", "event_name",
#         "event_mode", "start_date", "end_date",
#         "registration_open_date", "registration_end_date",
#         "prizepool", "number_of_stages", "is_draft"
#     ]

#     for field in required_fields:
#         if field not in request.data:
#             return Response({"message": f"Missing field: {field}"}, status=400)

#     # Parse dates
#     start_date = parse_date(request.data.get("start_date"))
#     end_date = parse_date(request.data.get("end_date"))
#     open_date = parse_date(request.data.get("registration_open_date"))
#     close_date = parse_date(request.data.get("registration_end_date"))

#     if open_date > close_date:
#         return Response({"message": "Registration open date cannot be after end date."}, status=400)

#     if start_date > end_date:
#         return Response({"message": "Event start date cannot be after end date."}, status=400)

#     # Parse prizepool
#     try:
#         prizepool_cash_value = float(request.data.get("prizepool_cash_value", 0))
#     except:
#         return Response({"message": "Prizepool must be a number."}, status=400)
    
#     prizepool = float(request.data.get("prizepool"))

#     # Parse prize distribution
#     prize_distribution = request.data.get("prize_distribution")
#     prize_distribution = json.loads(prize_distribution) if isinstance(prize_distribution, str) else prize_distribution
#     if not isinstance(prize_distribution, dict):
#         return Response({"message": "Prize distribution must be a JSON object."}, status=400)

#     # Create Event
#     event = Event.objects.create(
#         competition_type=request.data.get("competition_type"),
#         participant_type=request.data.get("participant_type"),
#         event_type=request.data.get("event_type"),
#         max_teams_or_players=request.data.get("max_teams_or_players"),
#         event_name=request.data.get("event_name"),
#         # format=request.data.get("format"),
#         event_mode=request.data.get("event_mode"),
#         start_date=start_date,
#         end_date=end_date,
#         registration_open_date=open_date,
#         registration_end_date=close_date,
#         prizepool=prizepool,
#         prizepool_cash_value=prizepool_cash_value,
#         prize_distribution=prize_distribution,
#         event_rules=request.data.get("event_rules"),
#         event_status=request.data.get("event_status", "upcoming"),
#         registration_link=request.data.get("registration_link") if "registration_link" in request.data else "",
#         tournament_tier=request.data.get("tournament_tier", "tier_3"),
#         event_banner=request.FILES.get("event_banner"),
#         number_of_stages=request.data.get("number_of_stages"),
#         uploaded_rules=request.FILES.get("uploaded_rules"),
#         is_draft=request.data.get("is_draft", True)
#     )

#     # Create stream channels
#     stream_channels = request.data.get("stream_channels", [])

#     if isinstance(stream_channels, str):
#         stream_channels = json.loads(stream_channels)
#     for url in stream_channels:
#         StreamChannel.objects.create(event=event, channel_url=url)

#     # Create stages + groups
#     stages_data = request.data.get("stages", [])

#     if isinstance(stages_data, str):
#         stages_data = json.loads(stages_data)


#     for stage_data in stages_data:

#         stage = Stages.objects.create(
#             event=event,
#             stage_name=stage_data["stage_name"],
#             start_date=parse_date(stage_data["start_date"]),
#             end_date=parse_date(stage_data["end_date"]),
#             number_of_groups=stage_data["number_of_groups"],
#             stage_format=stage_data["stage_format"],
#             teams_qualifying_from_stage=stage_data["teams_qualifying_from_stage"],
#             stage_discord_role_id = stage_data["stage_discord_role_id"],
#         )

#         # Create groups inside this stage
#         groups = stage_data.get("groups", [])
        
#         for group in groups:
#             StageGroups.objects.create(
#                 stage=stage,
#                 group_name=group["group_name"],
#                 playing_date=parse_date(group["playing_date"]),
#                 playing_time=group["playing_time"],
#                 teams_qualifying=group["teams_qualifying"],
#                 group_discord_role_id =  group["group_discord_role_id"],
#                 match_count = group["match_count"],
#                 match_maps = group["match_maps"],
#             )

#             # create matches for the group

#             total_number_of_matches_to_be_played = group.get("match_count", 0)
#             match_maps = group.get("match_maps", [])

#             for match_map in match_maps:
#                 for match_number in range(1, total_number_of_matches_to_be_played + 1):
#                     Match.objects.create(
#                         leaderboard=None,
#                         group=StageGroups.objects.get(stage=stage, group_name=group["group_name"]),
#                         match_map=match_map,
#                         match_number=match_number
#                     )

#     AdminHistory.objects.create(
#         admin_user=user,
#         action="create_event",
#         description=f"Created event {event.event_name} (ID: {event.event_id})"
#     )

#     return Response({
#         "message": "Event created successfully.",
#         "event_id": event.event_id
#     }, status=201)


@api_view(["POST"])
def delete_event(request):
    session_token = request.headers.get("Authorization")

    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]

    # Authenticate user
    user = validate_token(token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # Permission check (org-aware, resolved AFTER we have the event below)
    is_admin = _is_event_admin(user)

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    try:
        event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)

    # AFC admins may delete any event; an org member needs can_edit_events on the event's
    # owning org. org_can_event treats native (org=None) events as admin-only, so org
    # members can never delete AFC events.
    if not is_admin and not org_can_event(user, "can_edit_events", event):
        return Response({"message": "You do not have permission to modify this event."}, status=403)

    set_audit(request, f"Deleted the event {event.event_name}")
    AdminHistory.objects.create(
        admin_user=user,
        action="delete_event",
        description=f"Deleted event {event.event_name} (ID: {event.event_id})"
    )

    event.delete()

    

    return Response({"message": "Event deleted successfully."}, status=200)

# @api_view(["POST"])
# def edit_event(request):
#     session_token = request.headers.get("Authorization")

#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]

#     # Authenticate user
#     try:
#         user = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     # Permission check
#     if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
#         return Response({"message": "You do not have permission to edit an event."}, status=403)

#     # Event ID needed
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     # Fetch event
#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # Helper function to update only if provided
#     def update_field(field_name, parser=None):
#         if field_name in request.data:
#             value = request.data.get(field_name)
#             if parser:
#                 value = parser(value)
#             setattr(event, field_name, value)

#     # Update simple fields
#     update_field("competition_type")
#     update_field("participant_type")
#     update_field("event_type")
#     update_field("max_teams_or_players")
#     update_field("event_name")
#     update_field("event_mode")
#     update_field("event_status")
#     update_field("registration_link")
#     update_field("tournament_tier")
#     update_field("rules")
#     update_field("event_rules")

#     # Date fields
#     update_field("start_date", parse_date)
#     update_field("end_date", parse_date)
#     update_field("registration_open_date", parse_date)
#     update_field("registration_end_date", parse_date)

#     # Validate dates
#     if event.registration_open_date > event.registration_end_date:
#         return Response({"message": "Registration open date cannot be after registration end date."}, status=400)

#     if event.start_date > event.end_date:
#         return Response({"message": "Event start date cannot be after end date."}, status=400)

#     # Prizepool
#     if "prizepool" in request.data:
#         try:
#             event.prizepool = float(request.data.get("prizepool"))
#         except:
#             return Response({"message": "Prizepool must be a number."}, status=400)

#     # Prize distribution
#     if "prize_distribution" in request.data:
#         prize_distribution = request.data.get("prize_distribution")
#         prize_distribution = json.loads(prize_distribution) if isinstance(prize_distribution, str) else prize_distribution
#         if not isinstance(prize_distribution, dict):
#             return Response({"message": "Prize distribution must be a JSON object."}, status=400)
#         event.prize_distribution = prize_distribution

#     # Update event banner (optional)
#     if "event_banner" in request.FILES:
#         event.event_banner = request.FILES.get("event_banner")

#     # Update number_of_stages if provided
#     if "number_of_stages" in request.data:
#         event.number_of_stages = int(request.data.get("number_of_stages"))

#     event.save()

#     # ============================
#     # STREAM CHANNEL UPDATES
#     # ============================
#     if "stream_channels" in request.data:
#         StreamChannel.objects.filter(event=event).delete()
#         if isinstance(stream_channels, str):
#             stream_channels = json.loads(stream_channels)
#         for url in request.data.get("stream_channels", []):
#             StreamChannel.objects.create(event=event, channel_url=url)

#     # ============================
#     # STAGES + GROUPS UPDATE
#     # ============================
#     if "stages" in request.data:
#         stages_data = request.data["stages"]
#         if isinstance(stages_data, str):
#             stages_data = json.loads(stages_data)

#         # Remove old stages + groups
#         Stages.objects.filter(event=event).delete()

#         for stage_data in stages_data:
#             stage = Stages.objects.create(
#                 event=event,
#                 stage_name=stage_data["stage_name"],
#                 start_date=parse_date(stage_data["start_date"]),
#                 end_date=parse_date(stage_data["end_date"]),
#                 number_of_groups=stage_data["number_of_groups"],
#                 stage_format=stage_data["stage_format"],
#                 teams_qualifying_from_stage=stage_data["teams_qualifying_from_stage"]
#             )

#             # Add groups under this stage
#             for group in stage_data.get("groups", []):
#                 StageGroups.objects.create(
#                     stage=stage,
#                     group_name=group["group_name"],
#                     playing_date=parse_date(group["playing_date"]),
#                     playing_time=group["playing_time"],
#                     teams_qualifying=group["teams_qualifying"]
#                 )

#     return Response({
#         "message": "Event updated successfully.",
#         "event_id": event.event_id
#     }, status=200)





# @api_view(["POST"])
# def edit_event(request):
#     session_token = request.headers.get("Authorization")

#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]

#     # Authenticate user
#     user = validate_token(token)
#     if not user:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     # Permission check
#     if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
#         return Response({"message": "You do not have permission to edit an event."}, status=403)

#     # Event ID needed
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     # Fetch event
#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # Helper function to update only if provided
#     def update_field(field_name, parser=None):
#         if field_name in request.data:
#             value = request.data.get(field_name)
#             if parser:
#                 value = parser(value)
#             setattr(event, field_name, value)

#     # Update simple fields
#     for field in [
#         "competition_type", "participant_type", "event_type",
#         "max_teams_or_players", "event_name", "event_mode",
#         "event_status", "registration_link", "tournament_tier",
#         "rules", "event_rules"
#     ]:
#         update_field(field)

#     # Date fields
#     for date_field in [
#         "start_date", "end_date",
#         "registration_open_date", "registration_end_date"
#     ]:
#         update_field(date_field, parse_date)

#     # Date validation
#     if event.registration_open_date and event.registration_end_date:
#         if event.registration_open_date > event.registration_end_date:
#             return Response({"message": "Registration open date cannot be after registration end date."}, status=400)

#     if event.start_date and event.end_date:
#         if event.start_date > event.end_date:
#             return Response({"message": "Event start date cannot be after end date."}, status=400)

#     # Prizepool
#     if "prizepool" in request.data:
#         try:
#             event.prizepool = float(request.data.get("prizepool"))
#         except:
#             return Response({"message": "Prizepool must be a number."}, status=400)

#     # Prize distribution
#     if "prize_distribution" in request.data:
#         prize_distribution = request.data.get("prize_distribution")
#         if isinstance(prize_distribution, str):
#             prize_distribution = json.loads(prize_distribution)
#         if not isinstance(prize_distribution, dict):
#             return Response({"message": "Prize distribution must be a JSON object."}, status=400)
#         event.prize_distribution = prize_distribution

#     # Banner
#     if "event_banner" in request.FILES:
#         event.event_banner = request.FILES.get("event_banner")

#     # Uploaded Rules
#     if "uploaded_rules" in request.FILES:
#         event.uploaded_rules = request.FILES.get("uploaded_rules")

#     # Number of stages
#     if "number_of_stages" in request.data:
#         event.number_of_stages = int(request.data.get("number_of_stages"))

#     event.save()

#     # ============================
#     # STREAM CHANNEL UPDATES
#     # ============================
#     if "stream_channels" in request.data:
#         StreamChannel.objects.filter(event=event).delete()

#         stream_channels = request.data.get("stream_channels")

#         # Parse JSON if string
#         if isinstance(stream_channels, str):
#             stream_channels = json.loads(stream_channels)

#         if isinstance(stream_channels, list):
#             for url in stream_channels:
#                 StreamChannel.objects.create(event=event, channel_url=url)

#     # ============================
#     # STAGES + GROUPS UPDATE
#     # ============================
#     if "stages" in request.data:
#         stages_data = request.data.get("stages")

#         if isinstance(stages_data, str):
#             stages_data = json.loads(stages_data)


#         # Recreate stages + groups
#         for stage_data in stages_data:
#             stage, created = Stages.objects.update_or_create(
#                 event=event,
#                 stage_id=stage_data.get("stage_id"),  # use existing ID if provided
#                 defaults={
#                     "stage_name": stage_data["stage_name"],
#                     "start_date": parse_date(stage_data["start_date"]),
#                     "end_date": parse_date(stage_data["end_date"]),
#                     "number_of_groups": stage_data["number_of_groups"],
#                     "stage_format": stage_data["stage_format"],
#                     "teams_qualifying_from_stage": stage_data["teams_qualifying_from_stage"],
#                     "stage_discord_role_id": stage_data.get("stage_discord_role_id")
#                 }
#             )

#             # Groups
#             for group_data in stage_data.get("groups", []):
#                 group, created = StageGroups.objects.update_or_create(
#                     stage=stage,
#                     group_id=group_data.get("group_id"),  # use existing ID if provided
#                     defaults={
#                         "group_name": group_data["group_name"],
#                         "playing_date": parse_date(group_data["playing_date"]),
#                         "playing_time": group_data["playing_time"],
#                         "teams_qualifying": group_data["teams_qualifying"],
#                         "group_discord_role_id": group_data.get("group_discord_role_id"),
#                         "match_count": group_data.get("match_count"),
#                         "match_maps": group_data.get("match_maps"),
#                     }
#                 )

#                 # Matches
#                 total_matches = group_data.get("match_count", 0)
#                 match_maps = group_data.get("match_maps", [])
#                 for match_map in match_maps:
#                     for match_number in range(1, total_matches + 1):
#                         Match.objects.update_or_create(
#                             group=group,
#                             match_number=match_number,
#                             match_map=match_map,
#                             defaults={"leaderboard": None}
#                         )

#     return Response({
#         "message": "Event updated successfully.",
#         "event_id": event.event_id
#     }, status=200)


import json
from django.db import transaction
from django.utils.dateparse import parse_date, parse_time
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

def snapshot_event(event):
    return {
        "event": {
            "event_name": event.event_name,
            "event_type": event.event_type,
            "event_mode": event.event_mode,
            "participant_type": event.participant_type,
            "competition_type": event.competition_type,
            "max_teams_or_players": event.max_teams_or_players,
            "is_public": event.is_public,
            "is_sponsored": event.is_sponsored,
            "event_status": event.event_status,
            "tournament_tier": event.tournament_tier,
            "is_draft": event.is_draft,
            # Dates + times are editable too; snapshotting them means the audit summary can say e.g.
            # "registration start time from 09:00 to 10:30" (str() so diff_dict compares cleanly).
            "start_date": str(event.start_date),
            "end_date": str(event.end_date),
            "registration_open_date": str(event.registration_open_date),
            "registration_end_date": str(event.registration_end_date),
            "registration_start_time": str(event.registration_start_time),
            "registration_end_time": str(event.registration_end_time),
            "event_start_time": str(event.event_start_time),
            "event_end_time": str(event.event_end_time),
        },
        "sponsors": list(
            SponsorEvent.objects.filter(event=event)
            .values_list("sponsor__username", flat=True)
        ),
        "streams": list(
            StreamChannel.objects.filter(event=event)
            .values_list("channel_url", flat=True)
        ),
        "stages": [
            {
                "stage_id": s.stage_id,
                "stage_name": s.stage_name,
                "groups": [
                    {
                        "group_id": g.group_id,
                        "group_name": g.group_name,
                        "match_count": g.match_count,
                        "match_maps": g.match_maps,
                    }
                    for g in s.groups.all()
                ]
            }
            for s in event.stages.all()
        ]
    }


def diff_dict(old, new):
    changes = []
    for k in old:
        if str(old[k]) != str(new[k]):
            changes.append(f"{k}: '{old[k]}' → '{new[k]}'")
    return changes


def _humanize_change(c):
    """Turn a diff_dict entry "event_type: 'internal' → 'external'" into the plain-English
    "event type from internal to external" for the audit summary. Non-field diffs (sponsor/stage
    add/remove strings) are returned as-is."""
    import re
    m = re.match(r"^(.+?):\s*'(.*)'\s*→\s*'(.*)'$", str(c))
    if m:
        field, old, new = m.groups()
        return f"{field.replace('_', ' ').strip()} from {old} to {new}"
    return str(c)


def _event_edit_summary(name, changes):
    """Build the audit summary for an event edit, e.g.
    "Changed Detty December: event type from internal to external; registration start time from 09:00 to 10:30"."""
    label = name or "an event"
    if not changes:
        return f"Edited {label} (no changes)"
    pretty = [_humanize_change(c) for c in changes]
    shown = "; ".join(pretty[:3])
    if len(pretty) > 3:
        shown += f"; and {len(pretty) - 3} more change(s)"
    return f"Changed {label}: {shown}"


def diff_list(name, old_list, new_list):
    old_set = set(old_list)
    new_set = set(new_list)

    added = new_set - old_set
    removed = old_set - new_set

    changes = []
    if added:
        changes.append(f"{name} added: {list(added)}")
    if removed:
        changes.append(f"{name} removed: {list(removed)}")

    return changes


def diff_stages(old_stages, new_stages):
    changes = []

    old_map = {s["stage_id"]: s for s in old_stages}
    new_map = {s["stage_id"]: s for s in new_stages}

    # -------- STAGES --------
    old_ids = set(old_map.keys())
    new_ids = set(new_map.keys())

    added_stages = new_ids - old_ids
    removed_stages = old_ids - new_ids

    if added_stages:
        changes.append(f"Stages added: {list(added_stages)}")

    if removed_stages:
        changes.append(f"Stages removed: {list(removed_stages)}")

    # -------- EXISTING STAGES --------
    for sid in old_ids & new_ids:
        old_s = old_map[sid]
        new_s = new_map[sid]

        if old_s["stage_name"] != new_s["stage_name"]:
            changes.append(
                f"Stage {sid} name: '{old_s['stage_name']}' → '{new_s['stage_name']}'"
            )

        # -------- GROUPS --------
        old_groups = {g["group_id"]: g for g in old_s["groups"]}
        new_groups = {g["group_id"]: g for g in new_s["groups"]}

        old_g_ids = set(old_groups.keys())
        new_g_ids = set(new_groups.keys())

        if new_g_ids - old_g_ids:
            changes.append(f"Stage {sid}: groups added {list(new_g_ids - old_g_ids)}")

        if old_g_ids - new_g_ids:
            changes.append(f"Stage {sid}: groups removed {list(old_g_ids - new_g_ids)}")

        for gid in old_g_ids & new_g_ids:
            og = old_groups[gid]
            ng = new_groups[gid]

            if og["group_name"] != ng["group_name"]:
                changes.append(
                    f"Group {gid} name: '{og['group_name']}' → '{ng['group_name']}'"
                )

            if og["match_count"] != ng["match_count"]:
                changes.append(
                    f"Group {gid} match_count: {og['match_count']} → {ng['match_count']}"
                )

            if og["match_maps"] != ng["match_maps"]:
                changes.append(
                    f"Group {gid} maps changed"
                )

    return changes


@api_view(["POST"])
def edit_event(request):
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]
    user = validate_token(token)
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Permission check (org-aware, resolved AFTER we have the event below)
    is_admin = _is_event_admin(user)

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = Event.objects.filter(event_id=event_id).first()
    if not event:
        return Response({"message": "Event not found."}, status=404)

    # AFC admins may edit any event; an org member needs can_edit_events on the event's
    # owning org. org_can_event treats native (org=None) events as admin-only, so org
    # members can never edit AFC events.
    if not is_admin and not org_can_event(user, "can_edit_events", event):
        return Response({"message": "You do not have permission to modify this event."}, status=403)

    old_snapshot = snapshot_event(event)

    # old_data = {
    #     "event_name": event.event_name,
    #     "event_mode": event.event_mode,
    #     "participant_type": event.participant_type,
    #     "competition_type": event.competition_type,
    #     "max_teams_or_players": event.max_teams_or_players,
    #     "start_date": str(event.start_date),
    #     "end_date": str(event.end_date),
    #     "registration_open_date": str(event.registration_open_date),
    #     "registration_end_date": str(event.registration_end_date),
    #     "is_public": event.is_public,
    #     "is_sponsored": event.is_sponsored,
    #     "event_status": event.event_status,
    # }

    def maybe_json(val):
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val

    def as_list(val):
        val = maybe_json(val)
        return val if isinstance(val, list) else []

    def update_field(field_name, parser=None):
        if field_name in request.data:
            value = request.data.get(field_name)
            if parser:
                value = parser(value)
            setattr(event, field_name, value)

    # --------------------------------------------------
    # BASIC FIELD UPDATES
    # --------------------------------------------------

    for field in [
        "competition_type", "participant_type",
        "max_teams_or_players", "event_name", "event_mode",
        "event_status", "registration_link", "tournament_tier",
        "event_rules", "is_draft", "is_public", "event_type"
    ]:
        update_field(field)

    for date_field in ["start_date", "end_date", "registration_open_date", "registration_end_date"]:
        update_field(date_field, parse_date)

    # Times were saved on create but were missing here, so editing an event silently
    # dropped any time change. Persist them too (raw "HH:MM" string, or None to clear),
    # matching how create_event stores them.
    for time_field in [
        "registration_start_time", "registration_end_time",
        "event_start_time", "event_end_time",
    ]:
        if time_field in request.data:
            setattr(event, time_field, request.data.get(time_field) or None)

    if event.registration_open_date and event.registration_end_date:
        if event.registration_open_date > event.registration_end_date:
            return Response({"message": "registration_open_date cannot be after registration_end_date."}, status=400)

    if event.start_date and event.end_date:
        if event.start_date > event.end_date:
            return Response({"message": "start_date cannot be after end_date."}, status=400)

    if "prizepool" in request.data:
        event.prizepool = str(request.data.get("prizepool"))

    if "prizepool_cash_value" in request.data:
        try:
            event.prizepool_cash_value = float(request.data.get("prizepool_cash_value"))
        except:
            return Response({"message": "prizepool_cash_value must be a number."}, status=400)

    if "prize_distribution" in request.data:
        pd = maybe_json(request.data.get("prize_distribution"))
        if not isinstance(pd, dict):
            return Response({"message": "prize_distribution must be a JSON object."}, status=400)
        event.prize_distribution = pd

    # ── Paid registration (feature "paid-events") ──
    if "registration_type" in request.data:
        rt = request.data.get("registration_type")
        if rt not in ("free", "paid"):
            return Response({"message": "registration_type must be 'free' or 'paid'."}, status=400)
        event.registration_type = rt
    if "registration_fee_currency" in request.data:
        event.registration_fee_currency = (request.data.get("registration_fee_currency") or "USD").upper()[:3]
    if "registration_fee" in request.data:
        raw = request.data.get("registration_fee")
        if raw in (None, "", "null"):
            event.registration_fee = None
        else:
            from decimal import Decimal, InvalidOperation
            try:
                event.registration_fee = Decimal(str(raw))
            except (InvalidOperation, TypeError):
                return Response({"message": "registration_fee must be a number."}, status=400)
    # A paid event must end up with a positive fee.
    if event.registration_type == "paid" and (event.registration_fee is None or event.registration_fee <= 0):
        return Response({"message": "A paid event needs a registration_fee greater than 0."}, status=400)

    if "event_banner" in request.FILES:
        event.event_banner = request.FILES.get("event_banner")

    if "uploaded_rules" in request.FILES:
        event.uploaded_rules = request.FILES.get("uploaded_rules")

    if "number_of_stages" in request.data:
        event.number_of_stages = int(request.data.get("number_of_stages"))

    if "is_public" in request.data:
        is_public = request.data.get("is_public")
        if isinstance(is_public, str):
            is_public = is_public.lower() in ("1", "true", "yes")
        event.is_public = is_public

    if "is_sponsored" in request.data:

        is_sponsored = request.data.get("is_sponsored")

        # normalize to boolean
        if isinstance(is_sponsored, str):
            is_sponsored = is_sponsored.lower() in ("1", "true", "yes")
        else:
            is_sponsored = bool(is_sponsored)

        # if turning OFF sponsorship → clean up
        if not is_sponsored:
            SponsorEvent.objects.filter(event=event).delete()

            # optional but recommended cleanup
            event.sponsor = None
            event.sponsor_name = None
            event.sponsor_field_label = None
            event.sponsor_requirement_description = None

        event.is_sponsored = is_sponsored

    if "sponsor_usernames" in request.data:
        sponsor_usernames = as_list(request.data.get("sponsor_usernames"))
        existing_sponsors = set(SponsorEvent.objects.filter(event=event).values_list("sponsor__username", flat=True))
        new_sponsors = set(sponsor_usernames)
        # Remove old sponsors not in new list
        for username in existing_sponsors - new_sponsors:
            user = User.objects.filter(username=username).first()
            if user:
                SponsorEvent.objects.filter(event=event, sponsor=user).delete()
        # Add new sponsors
        for username in new_sponsors - existing_sponsors:
            user = User.objects.filter(username=username).first()
            if user:
                SponsorEvent.objects.create(event=event, sponsor=user)

        
    if "sponsor_name" in request.data:
        event.sponsor_name = request.data.get("sponsor_name")

    if "sponsor_field_label" in request.data:
        event.sponsor_field_label = request.data.get("sponsor_field_label")

    if "sponsor_requirement_description" in request.data:
        event.sponsor_requirement_description = request.data.get("sponsor_requirement_description")

    if "is_waitlist_enabled" in request.data:
        is_waitlist_enabled = request.data.get("is_waitlist_enabled")
        if isinstance(is_waitlist_enabled, str):
            is_waitlist_enabled = is_waitlist_enabled.lower() in ("1", "true", "yes")
        event.is_waitlist_enabled = is_waitlist_enabled

    # ── Media registration criteria (owner 2026-06-12) ── editable like the other toggles;
    # enforcement lives in register_for_event so changing them only affects FUTURE registrations.
    if "require_team_logo" in request.data:
        event.require_team_logo = _as_bool(request.data.get("require_team_logo"))
    if "require_esport_images" in request.data:
        event.require_esport_images = _as_bool(request.data.get("require_esport_images"))

    if "waitlist_capacity" in request.data:
        try:
            event.waitlist_capacity = int(request.data.get("waitlist_capacity"))
        except:
            return Response({"message": "waitlist_capacity must be an integer."}, status=400)
        

    if "waitlist_discord_role_id" in request.data:
        event.waitlist_discord_role_id = request.data.get("waitlist_discord_role_id")


    
    # ensure the evnt hasnt started if they wanna chnage the event type

    if "event_type" in request.data and request.data.get("event_type") != event.event_type:
        if event.start_date and event.start_date <= timezone.now().date():
            return Response({"message": "Cannot change event_type after the event has started."}, status=400)

        else:
            # delete all current registred teams/players if changing type, as they would be invalid,check if its a solo or team event and delete accordingly
            if event.participant_type == "team":
                # Delete Tournament Team members first then tournament team, filter tournament team member using the foriegn key tournament team.
                TournamentTeamMember.objects.filter(event=event).delete()
                TournamentTeam.objects.filter(event=event).delete()
                RegisteredCompetitors.objects.filter(event=event).delete()
                
            else:
                RegisteredCompetitors.objects.filter(event=event).delete()
            
        event.event_type = request.data.get("event_type")
        

    # --------------------------------------------------
    # ✅ REGISTRATION RESTRICTION UPDATE
    # --------------------------------------------------

    if "registration_restriction" in request.data:
        registration_restriction = request.data.get("registration_restriction")

        if registration_restriction not in ["none", "by_region", "by_country"]:
            return Response({
                "message": "registration_restriction must be none, by_region, or by_country."
            }, status=400)

        event.registration_restriction = registration_restriction

        if registration_restriction == "none":
            event.restriction_mode = None
            event.restricted_countries = []

        else:
            restriction_mode = request.data.get("restriction_mode")
            if restriction_mode not in ["allow_only", "block_selected"]:
                return Response({
                    "message": "restriction_mode must be allow_only or block_selected."
                }, status=400)

            restricted_countries = as_list(request.data.get("restricted_countries"))

            if not restricted_countries:
                return Response({
                    "message": "restricted_countries is required when restriction is enabled."
                }, status=400)

            event.restriction_mode = restriction_mode
            event.restricted_countries = restricted_countries

    # --------------------------------------------------
    # SAVE + CHILD OBJECTS
    # --------------------------------------------------

    delete_missing = str(request.data.get("delete_missing", "false")).lower() in ("1", "true", "yes")

    # Validate the per-stage scoring-mode config BEFORE the transaction opens so a bad
    # payload fails fast with a 400 and never partially mutates the event (returning a
    # non-2xx INSIDE transaction.atomic would still COMMIT, so the guard must live here).
    if "stages" in request.data:
        scoring_mode_error = _validate_scoring_modes(as_list(request.data.get("stages")))
        if scoring_mode_error:
            return Response({"message": scoring_mode_error}, status=400)

        # Same pre-transaction guard for round-robin base groups: a team must belong to
        # exactly one base group (Task 4 landmine #3). Fails fast before the edit commits.
        round_robin_groups_error = _validate_round_robin_groups(as_list(request.data.get("stages")))
        if round_robin_groups_error:
            return Response({"message": round_robin_groups_error}, status=400)

    with transaction.atomic():
        event.save()

        # new_data = {
        #     "event_name": event.event_name,
        #     "event_mode": event.event_mode,
        #     "participant_type": event.participant_type,
        #     "competition_type": event.competition_type,
        #     "max_teams_or_players": event.max_teams_or_players,
        #     "start_date": str(event.start_date),
        #     "end_date": str(event.end_date),
        #     "registration_open_date": str(event.registration_open_date),
        #     "registration_end_date": str(event.registration_end_date),
        #     "is_public": event.is_public,
        #     "is_sponsored": event.is_sponsored,
        #     "event_status": event.event_status,
        # }

        # changes = []

        # for field in old_data.keys():
        #     old_val = old_data[field]
        #     new_val = new_data[field]

        #     if str(old_val) != str(new_val):
        #         changes.append(f"{field}: '{old_val}' → '{new_val}'")

        # ---- Stream channels ----
        if "stream_channels" in request.data:
            StreamChannel.objects.filter(event=event).delete()
            stream_channels = as_list(request.data.get("stream_channels"))
            StreamChannel.objects.bulk_create(
                [StreamChannel(event=event, channel_url=url) for url in stream_channels if url],
                batch_size=200
            )

        # ---- Stages + Groups + Matches ----
        if "stages" in request.data:
            stages_data = as_list(request.data.get("stages"))

            kept_stage_ids = []
            kept_group_ids = []
            # Track upserted Stages in submit order for the Point-Rush target second pass
            # below (a stage may target a LATER stage not yet upserted while we process this one).
            upserted_stages = []

            for stage_data in stages_data:
                stage_id = stage_data.get("stage_id")

                stage_defaults = {
                    "stage_name": stage_data["stage_name"],
                    "start_date": parse_date(stage_data["start_date"]),
                    "end_date": parse_date(stage_data["end_date"]),
                    "number_of_groups": int(stage_data["number_of_groups"]),
                    "stage_format": stage_data["stage_format"],
                    "teams_qualifying_from_stage": int(stage_data["teams_qualifying_from_stage"]),
                    "stage_discord_role_id": stage_data.get("stage_discord_role_id"),
                    "stage_status": stage_data.get("stage_status", "upcoming"),
                    # if prizepool is provided, convert to string to avoid issues with large numbers, otherwise set to None to avoid overwriting existing value
                    "prizepool": str(stage_data.get("prizepool")) if stage_data.get("prizepool") else None,
                    # if prizepool_cash_value is provided, convert to float, otherwise set to None to avoid overwriting existing value
                    "prizepool_cash_value": float(stage_data.get("prizepool_cash_value")) if stage_data.get("prizepool_cash_value") else None,
                    "prize_distribution": stage_data.get("prize_distribution", {}),
                    # ── Scoring-mode config (scoring-modes sub-project A). 4 scalars upsert
                    # directly; point_rush_target_stage is resolved in the second pass below. ──
                    "champion_point_enabled": bool(stage_data.get("champion_point_enabled", False)),
                    "champion_point_threshold": stage_data.get("champion_point_threshold") or None,
                    "point_rush_enabled": bool(stage_data.get("point_rush_enabled", False)),
                    "point_rush_reward": stage_data.get("point_rush_reward") or {},
                }

                if stage_id:
                    stage, _ = Stages.objects.update_or_create(
                        stage_id=stage_id,
                        defaults={**stage_defaults, "event": event},
                    )
                else:
                    stage = Stages.objects.create(event=event, **stage_defaults)

                kept_stage_ids.append(stage.stage_id)
                upserted_stages.append(stage)

                # ── BR Round-Robin (sub-project B, Task 4): rebuild base groups + lobbies
                # from round_robin_groups, KEEPING lobbies that already have entered results.
                # The returned lobby ids go into kept_group_ids so delete_missing below does
                # not sweep the freshly-built lobbies (they carry no group_id in the payload).
                if stage_data.get("stage_format") == ROUND_ROBIN_FORMAT:
                    kept_group_ids.extend(
                        _edit_round_robin_stage(stage, event, user, stage_data))

                for group_data in stage_data.get("groups", []):
                    group_id = group_data.get("group_id")

                    group_defaults = {
                        "group_name": group_data["group_name"],
                        "playing_date": parse_date(group_data["playing_date"]),
                        "playing_time": parse_time(group_data["playing_time"]) if isinstance(group_data["playing_time"], str) else group_data["playing_time"],
                        "teams_qualifying": int(group_data["teams_qualifying"]),
                        "group_discord_role_id": group_data.get("group_discord_role_id"),
                        "match_count": int(group_data.get("match_count", 0)),
                        "match_maps": group_data.get("match_maps", []),
                        # if prizepool is provided, convert to string to avoid issues with large numbers, otherwise set to None to avoid overwriting existing value
                        "prizepool": str(group_data.get("prizepool")) if group_data.get("prizepool") else None,
                        # if prizepool_cash_value is provided, convert to float, otherwise set to None to avoid overwriting existing value
                        "prizepool_cash_value": float(group_data.get("prizepool_cash_value")) if group_data.get("prizepool_cash_value") else None,
                        "prize_distribution": group_data.get("prize_distribution", {}),
                    }

                    if group_id:
                        group, _ = StageGroups.objects.update_or_create(
                            group_id=group_id,
                            defaults={**group_defaults, "stage": stage},
                        )
                    else:
                        group = StageGroups.objects.create(stage=stage, **group_defaults)

                    kept_group_ids.append(group.group_id)
                    # ---- Sync Matches + Leaderboard ----

                    match_count = group.match_count or 0
                    match_maps = group.match_maps or []
                    default_map = match_maps[0] if match_maps else "bermuda"

                    # If match_count drops to 0, remove the leaderboard and all matches
                    if match_count == 0:
                        Leaderboard.objects.filter(event=event, stage=stage, group=group).delete()
                        Match.objects.filter(group=group).delete()
                        continue

                    # Ensure a leaderboard exists for this group; create one if not
                    leaderboard, _ = Leaderboard.objects.get_or_create(
                        event=event,
                        stage=stage,
                        group=group,
                        defaults={
                            "leaderboard_name": f"{stage.stage_name} - {group.group_name}",
                            "creator": user,
                            "leaderboard_method": "manual",
                            "placement_points": {},
                            "kill_point": 1.0,
                        }
                    )

                    existing_matches = {
                        m.match_number: m
                        for m in Match.objects.filter(group=group)
                    }

                    matches_to_create = []
                    matches_to_update = []
                    kept_match_numbers = []

                    for num in range(1, match_count + 1):
                        chosen_map = (
                            match_maps[(num - 1) % len(match_maps)]
                            if match_maps else default_map
                        )
                        kept_match_numbers.append(num)

                        if num in existing_matches:
                            m = existing_matches[num]
                            if m.match_map != chosen_map or m.leaderboard_id != leaderboard.leaderboard_id:
                                m.match_map = chosen_map
                                m.leaderboard = leaderboard
                                matches_to_update.append(m)
                        else:
                            matches_to_create.append(Match(
                                group=group,
                                match_number=num,
                                match_map=chosen_map,
                                leaderboard=leaderboard
                            ))

                    if matches_to_create:
                        Match.objects.bulk_create(matches_to_create, batch_size=200)

                    if matches_to_update:
                        Match.objects.bulk_update(matches_to_update, ["match_map", "leaderboard"])

                    # Remove matches beyond the new match_count
                    Match.objects.filter(group=group)\
                        .exclude(match_number__in=kept_match_numbers)\
                        .delete()

            # ── Second pass: wire Point-Rush carry-over targets now that every stage is
            # upserted. point_rush_target_index is the 0-based position of the target stage
            # in the submitted `stages` array, which maps 1:1 to upserted_stages (built in
            # submit order above). We always set/clear the link so an edit that turns the
            # toggle off, or repoints it, is reflected (None clears a stale target). ──
            for src_stage, stage_data in zip(upserted_stages, stages_data):
                target_stage = None
                if src_stage.point_rush_enabled:
                    tgt_idx = stage_data.get("point_rush_target_index")
                    if tgt_idx is not None and tgt_idx != "":
                        tgt_idx = int(tgt_idx)
                        if 0 <= tgt_idx < len(upserted_stages):
                            target_stage = upserted_stages[tgt_idx]
                if src_stage.point_rush_target_stage_id != (target_stage.stage_id if target_stage else None):
                    src_stage.point_rush_target_stage = target_stage
                    src_stage.save(update_fields=["point_rush_target_stage"])

            if delete_missing:
                StageGroups.objects.filter(stage__event=event).exclude(group_id__in=kept_group_ids).delete()
                Stages.objects.filter(event=event).exclude(stage_id__in=kept_stage_ids).delete()

        new_snapshot = snapshot_event(event)

        changes = []

        # event fields
        changes += diff_dict(old_snapshot["event"], new_snapshot["event"])

        # sponsors
        changes += diff_list("Sponsors", old_snapshot["sponsors"], new_snapshot["sponsors"])

        # streams
        changes += diff_list("Stream channels", old_snapshot["streams"], new_snapshot["streams"])

        # stages/groups
        changes += diff_stages(old_snapshot["stages"], new_snapshot["stages"])

    # AdminHistory.objects.create(
    #     admin_user=user,
    #     action="edit_event",
    #     description=f"Edited event {event.event_name} (ID: {event.event_id})"
    # )
    # description = "No changes made."

    # if changes:
    #     description = " | ".join(changes)

    
    AdminHistory.objects.create(
        admin_user=user,
        action="edit_event",
        description=json.dumps({
            "event_id": event.event_id,
            "changes": changes
        }, indent=2)
    )

    # Rich, specific audit summary for the admin activity log, e.g.
    # "Changed Detty December: event type from internal to external". The full change list rides
    # along in the expandable details.
    set_audit(request, _event_edit_summary(event.event_name, changes), changes=changes)

    # AdminHistory.objects.create(
    #     admin_user=user,
    #     action="edit_event",
    #     description=f"Edited event {event.event_name} (ID: {event.event_id}) | {description}"
    # )
    # AdminHistory.objects.create(
    #     admin_user=user,
    #     action="edit_event",
    #     description=json.dumps({
    #         "event_id": event.event_id,
    #         "changes": changes
    #     })
    # )

    return Response({
        "message": "Event updated successfully.",
        "event_id": event.event_id
    }, status=200)



# @api_view(["POST"])
# def edit_event(request):
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]
#     user = validate_token(token)
#     if not user:
#         return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

#     if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(
#         role_name__in=["event_admin", "head_admin"]
#     ).exists():
#         return Response({"message": "You do not have permission to edit an event."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = Event.objects.filter(event_id=event_id).first()
#     if not event:
#         return Response({"message": "Event not found."}, status=404)

#     def maybe_json(val):
#         if isinstance(val, str):
#             try:
#                 return json.loads(val)
#             except Exception:
#                 return val
#         return val

#     def update_field(field_name, parser=None):
#         if field_name in request.data:
#             value = request.data.get(field_name)
#             if parser:
#                 value = parser(value)
#             setattr(event, field_name, value)

#     # ---- Update event fields (only if provided) ----
#     for field in [
#         "competition_type", "participant_type", "event_type",
#         "max_teams_or_players", "event_name", "event_mode",
#         "event_status", "registration_link", "tournament_tier",
#         "event_rules", "is_draft",
#     ]:
#         update_field(field)

#     for date_field in ["start_date", "end_date", "registration_open_date", "registration_end_date"]:
#         update_field(date_field, parse_date)

#     # validate dates (only if both exist)
#     if event.registration_open_date and event.registration_end_date:
#         if event.registration_open_date > event.registration_end_date:
#             return Response({"message": "registration_open_date cannot be after registration_end_date."}, status=400)

#     if event.start_date and event.end_date:
#         if event.start_date > event.end_date:
#             return Response({"message": "start_date cannot be after end_date."}, status=400)

#     if "prizepool" in request.data:
#         try:
#             event.prizepool = str(request.data.get("prizepool"))
#         except Exception:
#             return Response({"message": "prizepool."}, status=400)
    
    
#     if "prizepool_cash_value" in request.data:
#         try:
#             event.prizepool_cash_value = float(request.data.get("prizepool_cash_value"))
#         except Exception:
#             return Response({"message": "prizepool_cash_value must be a number."}, status=400)


#     if "prize_distribution" in request.data:
#         pd = maybe_json(request.data.get("prize_distribution"))
#         if not isinstance(pd, dict):
#             return Response({"message": "prize_distribution must be a JSON object."}, status=400)
#         event.prize_distribution = pd

#     if "event_banner" in request.FILES:
#         event.event_banner = request.FILES.get("event_banner")

#     if "uploaded_rules" in request.FILES:
#         event.uploaded_rules = request.FILES.get("uploaded_rules")

#     if "number_of_stages" in request.data:
#         event.number_of_stages = int(request.data.get("number_of_stages"))

#     # optionally delete items not in payload
#     delete_missing = str(request.data.get("delete_missing", "false")).lower() in ("1", "true", "yes")

#     with transaction.atomic():
#         event.save()

#         # ---- Stream channels ----
#         if "stream_channels" in request.data:
#             StreamChannel.objects.filter(event=event).delete()
#             stream_channels = maybe_json(request.data.get("stream_channels"))
#             if isinstance(stream_channels, list):
#                 StreamChannel.objects.bulk_create(
#                     [StreamChannel(event=event, channel_url=url) for url in stream_channels if url],
#                     batch_size=200
#                 )

#         # ---- Stages + Groups + Matches ----
#         if "stages" in request.data:
#             stages_data = maybe_json(request.data.get("stages"))
#             if not isinstance(stages_data, list):
#                 return Response({"message": "stages must be a JSON list."}, status=400)

#             kept_stage_ids = []
#             kept_group_ids = []

#             for stage_data in stages_data:
#                 stage_id = stage_data.get("stage_id")

#                 stage_defaults = {
#                     "stage_name": stage_data["stage_name"],
#                     "start_date": parse_date(stage_data["start_date"]),
#                     "end_date": parse_date(stage_data["end_date"]),
#                     "number_of_groups": int(stage_data["number_of_groups"]),
#                     "stage_format": stage_data["stage_format"],
#                     "teams_qualifying_from_stage": int(stage_data["teams_qualifying_from_stage"]),
#                     "stage_discord_role_id": stage_data.get("stage_discord_role_id"),
#                     "stage_status": stage_data.get("stage_status", "upcoming"),
#                 }

#                 if stage_id:
#                     stage, _ = Stages.objects.update_or_create(
#                         stage_id=stage_id,
#                         defaults={**stage_defaults, "event": event},
#                     )
#                 else:
#                     stage = Stages.objects.create(event=event, **stage_defaults)

#                 kept_stage_ids.append(stage.stage_id)

#                 # ---- groups ----
#                 groups = stage_data.get("groups", [])
#                 if not isinstance(groups, list):
#                     groups = []

#                 for group_data in groups:
#                     group_id = group_data.get("group_id")

#                     group_defaults = {
#                         "group_name": group_data["group_name"],
#                         "playing_date": parse_date(group_data["playing_date"]),
#                         "playing_time": parse_time(group_data["playing_time"]) if isinstance(group_data["playing_time"], str) else group_data["playing_time"],
#                         "teams_qualifying": int(group_data["teams_qualifying"]),
#                         "group_discord_role_id": group_data.get("group_discord_role_id"),
#                         "match_count": int(group_data.get("match_count", 0)),
#                         "match_maps": group_data.get("match_maps", []),
#                     }

#                     if group_id:
#                         group, _ = StageGroups.objects.update_or_create(
#                             group_id=group_id,
#                             defaults={**group_defaults, "stage": stage},
#                         )
#                     else:
#                         group = StageGroups.objects.create(stage=stage, **group_defaults)

#                     kept_group_ids.append(group.group_id)

#                     # ---- Matches (FIXED) ----
#                     # Create exactly match_count matches total.
#                     # Map selection: match_maps[i % len(match_maps)] if provided else keep existing or default to 'bermuda'
#                     match_count = group.match_count or 0
#                     match_maps = group.match_maps or []
#                     default_map = match_maps[0] if match_maps else "bermuda"

#                     existing = {m.match_number: m for m in Match.objects.filter(group=group)}
#                     want_numbers = set(range(1, match_count + 1))

#                     # delete missing matches if enabled
#                     force_delete_results = str(request.data.get("force_delete_results", "false")).lower() in ("1","true","yes")

#                     if delete_missing:
#                         qs = Match.objects.filter(group=group).exclude(match_number__in=want_numbers)
#                         if not force_delete_results:
#                             qs = qs.filter(result_inputted=False)
#                         qs.delete()

#                     # if delete_missing:
#                     #     Match.objects.filter(group=group).exclude(match_number__in=want_numbers).delete()

#                     for num in range(1, match_count + 1):
#                         chosen_map = default_map
#                         if match_maps:
#                             chosen_map = match_maps[(num - 1) % len(match_maps)]

#                         if num in existing:
#                             m = existing[num]
#                             # only update map if not already set or you want to force update
#                             m.match_map = chosen_map
#                             m.save(update_fields=["match_map"])
#                         else:
#                             Match.objects.create(
#                                 group=group,
#                                 match_number=num,
#                                 match_map=chosen_map,
#                                 leaderboard=None
#                             )

#             # delete stages/groups not present in payload if enabled
#             if delete_missing:
#                 StageGroups.objects.filter(stage__event=event).exclude(group_id__in=kept_group_ids).delete()
#                 Stages.objects.filter(event=event).exclude(stage_id__in=kept_stage_ids).delete()
    
#     AdminHistory.objects.create(
#         admin_user=user,
#         action="edit_event",
#         description=f"Edited event {event.event_name} (ID: {event.event_id})"
#     )

#     return Response({"message": "Event updated successfully.", "event_id": event.event_id}, status=200)



@api_view(["GET"])
def get_total_events_count(request):
    # FIX: QuerySet.count() takes no kwargs — the is_draft predicate must go in .filter()
    # (matches sibling count views below, e.g. get_total_tournaments_count). Counts published (non-draft) events.
    total_events = Event.objects.filter(is_draft=False).count()
    return Response({"total_events": total_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_total_tournaments_count(request):
    total_tournaments = Event.objects.filter(competition_type="tournament", is_draft=False).count()
    return Response({"total_tournaments": total_tournaments}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_total_scrims_count(request):
    total_scrims = Event.objects.filter(competition_type="scrim", is_draft=False).count()
    return Response({"total_scrims": total_scrims}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_upcoming_events_count(request):
    upcoming_events = Event.objects.filter(event_status="upcoming", is_draft=False).count()
    return Response({"upcoming_events": upcoming_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_ongoing_events_count(request):
    ongoing_events = Event.objects.filter(event_status="ongoing", is_draft=False).count()
    return Response({"ongoing_events": ongoing_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_completed_events_count(request):
    completed_events = Event.objects.filter(event_status="completed", is_draft=False).count()
    return Response({"completed_events": completed_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_average_participants_per_event(request):
    events = Event.objects.all()
    total_participants = 0
    event_count = events.count()

    if event_count == 0:
        return Response({"average_participants": 0}, status=status.HTTP_200_OK)

    for event in events:
        total_participants += event.max_teams_or_players

    average_participants = total_participants / event_count
    return Response({"average_participants": average_participants}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_most_popular_event_format(request):
    format_counts = {}
    events = Event.objects.all()

    for event in events:
        # Event has no `format` field (event.format raised AttributeError -> 500).
        # Use event_mode, the real Event field for how the event runs.
        # Stored choice keys: 'virtual' (Online), 'physical(lan)', 'hybrid' (models.py L25-29, L61).
        fmt = event.event_mode
        if fmt in format_counts:
            format_counts[fmt] += 1
        else:
            format_counts[fmt] = 1

    if not format_counts:
        return Response({"most_popular_format": None}, status=status.HTTP_200_OK)

    most_popular_format = max(format_counts, key=format_counts.get)
    return Response({"most_popular_format": most_popular_format}, status=status.HTTP_200_OK)


# @api_view(["POST"])
# def get_event_details(request):
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = (
#             Event.objects
#             .prefetch_related(
#                 "streamchannel_set",
#                 "stages_set__stagegroups_set"
#             )
#             .get(event_id=event_id)
#         )
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # Base Event Data
#     event_data = {
#         "event_id": event.event_id,
#         "competition_type": event.competition_type,
#         "participant_type": event.participant_type,
#         "event_type": event.event_type,
#         "max_teams_or_players": event.max_teams_or_players,
#         "event_name": event.event_name,
#         "event_mode": event.event_mode,
#         "start_date": event.start_date,
#         "end_date": event.end_date,
#         "registration_open_date": event.registration_open_date,
#         "registration_end_date": event.registration_end_date,
#         "prizepool": event.prizepool,
#         "prize_distribution": event.prize_distribution,
#         "event_rules": event.event_rules,
#         "event_status": event.event_status,
#         "registration_link": event.registration_link,
#         "tournament_tier": event.tournament_tier,
#         "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
#         "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
#         "number_of_stages": event.number_of_stages,
#         "created_at": event.created_at,
#     }

#     # Stream Channels
#     event_data["stream_channels"] = [
#         channel.channel_url
#         for channel in event.streamchannel_set.all()
#     ]

#     # Stages + Groups
#     stages = event.stages_set.all().order_by("start_date")
#     stage_list = []

#     for stage in stages:
#         groups = stage.stagegroups_set.all().order_by("group_name")
#         group_list = [{
#             "id": group.group_id,
#             "group_name": group.group_name,
#             "playing_date": group.playing_date,
#             "playing_time": group.playing_time,
#             "teams_qualifying": group.teams_qualifying,
#         } for group in groups]

#         stage_list.append({
#             "id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "stage_format": stage.stage_format,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": group_list,
#         })

#     event_data["stages"] = stage_list

#     return Response({"event_details": event_data}, status=200)

# @api_view(["POST"])
# def get_event_details(request):
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.prefetch_related(
#             "stream_channels",
#             "stages__groups__leaderboards__matches__team_stats__player_stats"
#         ).get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # Base Event Data
#     event_data = {
#         "event_id": event.event_id,
#         "competition_type": event.competition_type,
#         "participant_type": event.participant_type,
#         "event_type": event.event_type,
#         "max_teams_or_players": event.max_teams_or_players,
#         "event_name": event.event_name,
#         "event_mode": event.event_mode,
#         "start_date": event.start_date,
#         "end_date": event.end_date,
#         "registration_open_date": event.registration_open_date,
#         "registration_end_date": event.registration_end_date,
#         "prizepool": event.prizepool,
#         "prize_distribution": event.prize_distribution,
#         "event_rules": event.event_rules,
#         "event_status": event.event_status,
#         "registration_link": event.registration_link,
#         "tournament_tier": event.tournament_tier,
#         "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
#         "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
#         "number_of_stages": event.number_of_stages,
#         "created_at": event.created_at,
#     }

#     # Stream Channels
#     event_data["stream_channels"] = [
#         channel.channel_url for channel in event.stream_channels.all()
#     ]

#     # Stages + Groups + Match Stats
#     stage_list = []
#     for stage in event.stages.all().order_by("start_date"):
#         group_list = []
#         for group in stage.groups.all().order_by("group_name"):
#             matches_data = []
#             for lb in group.leaderboards.all():
#                 for match in lb.matches.all():
#                     team_stats_data = []
#                     for team_stat in match.team_stats.all():
#                         player_stats_data = [{
#                             "player_id": ps.player.id,
#                             "username": ps.player.username,
#                             "kills": ps.kills,
#                             "damage": ps.damage
#                         } for ps in team_stat.player_stats.all()]

#                         team_stats_data.append({
#                             "team_id": team_stat.team.team_id,
#                             "team_name": team_stat.team.team_name,
#                             "placement": team_stat.placement,
#                             "players": player_stats_data
#                         })

#                     matches_data.append({
#                         "match_id": match.match_id,
#                         "map": match.map,
#                         "mvp": match.mvp.username,
#                         "teams": team_stats_data
#                     })

#             group_list.append({
#                 "id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "matches": matches_data
#             })

#         stage_list.append({
#             "id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "stage_format": stage.stage_format,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": group_list
#         })

#     event_data["stages"] = stage_list

#     return Response({"event_details": event_data}, status=200)


# @api_view(["POST"])
# def get_event_details(request):
#     session_token = request.headers.get("Authorization")

#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]

#     # Authenticate user
#     user = validate_token(token)
#     if not user:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )
    
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = (
#             Event.objects.prefetch_related(
#                 "stream_channels",
#                 "stages__groups__leaderboards__matches__team_stats__player_stats",

#                 # Registered competitors (solo or team)
#                 "registrations__user",
#                 "registrations__team__teammembers__member",

#                 # Tournament teams + members
#                 "tournament_teams__team__teammembers__member",
#                 "tournament_teams__members__user",
#             )
#             .get(event_id=event_id)
#         )
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)
    
#     # check if user is registered for the event
#     is_registered = False
#     if event.participant_type == "solo":
#         is_registered = RegisteredCompetitors.objects.filter(user=user).exists()


#     # Base Event Data
#     event_data = {
#         "event_id": event.event_id,
#         "competition_type": event.competition_type,
#         "participant_type": event.participant_type,
#         "event_type": event.event_type,
#         "max_teams_or_players": event.max_teams_or_players,
#         "event_name": event.event_name,
#         "event_mode": event.event_mode,
#         "start_date": event.start_date,
#         "end_date": event.end_date,
#         "registration_open_date": event.registration_open_date,
#         "registration_end_date": event.registration_end_date,
#         "prizepool": event.prizepool,
#         "prize_distribution": event.prize_distribution,
#         "event_rules": event.event_rules,
#         "event_status": event.event_status,
#         "registration_link": event.registration_link,
#         "tournament_tier": event.tournament_tier,
#         "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
#         "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
#         "number_of_stages": event.number_of_stages,
#         "created_at": event.created_at,
#         "is_registered": is_registered
#     }

#     # Stream channels
#     event_data["stream_channels"] = [
#         ch.channel_url for ch in event.stream_channels.all()
#     ]

#     # Registered Competitors (for event registration)
#     registered = []
#     if event.participant_type == "solo":
#         for reg in event.registrations.all():
#             if reg.user:
#                 registered.append({
#                     "player_id": reg.user.user_id,
#                     "username": reg.user.username,
#                     "status": reg.status
#                 })
#     else:  # duo or squad
#         for reg in event.registrations.all():
#             if reg.team:
#                 members = reg.team.teammembers.all()
#                 registered.append({
#                     "team_id": reg.team.team_id,
#                     "team_name": reg.team.team_name,
#                     "status": "registered",
#                     "members": [
#                         {
#                             "player_id": m.member.id,
#                             "username": m.member.username,
#                             "role": m.in_game_role
#                         }
#                         for m in members
#                     ]
#                 })

#     event_data["registered_competitors"] = registered

#     # Tournament Teams (official accepted teams)
#     tournament_teams_list = []
#     for tt in event.tournament_teams.all():
#         members = tt.members.all()
#         tournament_teams_list.append({
#             "tournament_team_id": tt.tournament_team_id,
#             "team_id": tt.team.team_id,
#             "team_name": tt.team.team_name,
#             "members": [
#                 {
#                     "player_id": m.user.id,
#                     "username": m.user.username
#                 }
#                 for m in members
#             ]
#         })

#     event_data["tournament_teams"] = tournament_teams_list

#     # Stages, Groups, Matches
#     stage_list = []
#     for stage in event.stages.all().order_by("start_date"):
#         group_list = []
#         for group in stage.groups.all().order_by("group_name"):
#             matches_data = []

#             for lb in group.leaderboards.all():
#                 for match in lb.matches.all():
#                     team_stats_data = []
#                     for team_stat in match.team_stats.all():

#                         player_stats_data = [
#                             {
#                                 "player_id": ps.player.id,
#                                 "username": ps.player.username,
#                                 "kills": ps.kills,
#                                 "damage": ps.damage
#                             }
#                             for ps in team_stat.player_stats.all()
#                         ]

#                         team_stats_data.append({
#                             "tournament_team_id": team_stat.tournament_team.tournament_team_id,
#                             "team_name": team_stat.tournament_team.team.team_name,
#                             "placement": team_stat.placement,
#                             "players": player_stats_data
#                         })

#                     matches_data.append({
#                         "match_id": match.match_id,
#                         "map_name": match.match_map,
#                         "mvp": match.mvp.username if match.mvp else None,
#                         "teams": team_stats_data
#                     })

#             group_list.append({
#                 "id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "matches": matches_data,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#             })

#         stage_list.append({
#             "id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "stage_format": stage.stage_format,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": group_list
#         })

#     event_data["stages"] = stage_list

#     return Response({"event_details": event_data}, status=200)

from django.db.models import Sum, Count, F
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


# ── Event page-view tracking (hardened) ──────────────────────────────────────────
# User-agent fragments that mark a request as a bot / link-unfurl crawler. Their hits
# must NOT inflate event view counts. This list also covers the link-embed crawlers
# (Discord, X/Twitter, WhatsApp, Slack, Telegram, Facebook) that fetch a page when its
# link is shared, so the new OG embeds don't pollute the numbers.
_VIEW_BOT_UA_MARKERS = (
    "bot", "crawler", "spider", "slurp", "facebookexternalhit", "discordbot",
    "twitterbot", "whatsapp", "telegrambot", "slackbot", "embedly", "preview",
    "headlesschrome", "python-requests", "curl", "wget", "go-http-client",
)
# Do not re-count the SAME viewer (a logged-in user, or an IP for anonymous) on the
# SAME event within this window, so refreshes / quick re-opens do not inflate views.
_VIEW_DEDUPE_WINDOW = timedelta(minutes=30)


def _record_event_view(request, event, user):
    """Record ONE EventPageView for a real, human, non-staff, non-owner visit.

    Skips, so the count reflects genuine audience interest:
      • bots / link-unfurl crawlers (matched on the user-agent),
      • AFC platform admins (their own browsing should not inflate counts),
      • members of the organization that OWNS the event (an org can't pad its own views),
      • a repeat view by the same viewer on the same event inside the dedupe window.

    Best-effort: any failure here is swallowed so view tracking can never break the
    event page load. Called by get_event_details (public) and get_event_details_for_admin
    (admins are filtered out by the role check, so admin loads never count).
    """
    try:
        ua = (request.META.get("HTTP_USER_AGENT") or "").lower()
        if any(marker in ua for marker in _VIEW_BOT_UA_MARKERS):
            return  # bot / crawler / link-preview fetch

        # AFC staff: don't count their own browsing.
        if user is not None and getattr(user, "role", None) == "admin":
            return

        # Members of the org that owns this event: don't let an org inflate its own
        # event's views. AFC-official events have no organization, so this is skipped.
        if user is not None and event.organization_id:
            from afc_organizers.models import OrganizationMember
            if OrganizationMember.objects.filter(
                organization_id=event.organization_id, user=user
            ).exists():
                return

        ip = get_client_ip(request)
        since = timezone.now() - _VIEW_DEDUPE_WINDOW
        recent = EventPageView.objects.filter(event=event, viewed_at__gte=since)
        if user is not None:
            recent = recent.filter(user=user)            # same logged-in viewer
        elif ip:
            recent = recent.filter(user__isnull=True, ip_address=ip)  # same anon IP
        else:
            recent = None  # cannot identify the viewer -> always record
        if recent is not None and recent.exists():
            return  # already counted this viewer recently

        EventPageView.objects.create(
            event=event, user=user or None, ip_address=ip, viewed_at=timezone.now()
        )
    except Exception:
        pass  # view tracking is best-effort; never break the response


@api_view(["POST"])
def get_event_details(request):
    user = None

    # -------- OPTIONAL AUTH --------
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        user = validate_token(token)
        if not user:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

    # -------- INPUT --------
    slug = request.data.get("slug")
    if not slug:
        return Response({"message": "slug is required."}, status=400)

    # select_related("organization") avoids an extra query when we echo the owning
    # org's id/name/slug below (used by the organizer edit page's ownership guard).
    event = get_object_or_404(
        Event.objects.select_related("organization"), slug=slug
    )

    # Owner rule 2026-06-11: a suspended/deleted organization's events must not show publicly. Hide them
    # here (404) for everyone except an AFC admin (who manages via the admin surface and may still need
    # to preview). AFC-native events (no org) are unaffected. Mirrors _ACTIVE_ORG_EVENT on the lists.
    if _org_hidden(event) and not (user and _is_event_admin(user)):
        return Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)

    # -------- IS REGISTERED CHECK --------
    is_registered = False
    if user:
        if event.participant_type == "solo":
            is_registered = RegisteredCompetitors.objects.filter(
                event=event,
                user=user,
                status="registered"
            ).exists()
        else:
            # ✅ Use TournamentTeamMember for squad/duo
            is_registered = TournamentTeamMember.objects.filter(
                tournament_team__event=event,
                user=user
            ).exists()

    # -------- SLOT LEFT --------
    # total_registered = RegisteredCompetitors(event=event).all()
    # total_slots= event.max_teams_or_players

    # slots_left = total_slots - total_registered


    sponsors = SponsorEvent.objects.filter(event=event).select_related("sponsor")

    # -------- CAPACITY SNAPSHOT (M: waitlist) --------
    # active_registered = non-waitlisted registrations, counted exactly the way
    # register_for_event enforces the cap, so the "is_full" flag below stays in
    # lock-step with what actually blocks registration:
    #   solo -> RegisteredCompetitors (registered/approved, not waitlisted)
    #   team -> TournamentTeam (not waitlisted)
    if event.participant_type == "solo":
        active_registered = RegisteredCompetitors.objects.filter(
            event=event, status__in=["registered", "approved"], is_waitlisted=False
        ).count()
    else:
        active_registered = TournamentTeam.objects.filter(
            event=event, is_waitlisted=False
        ).count()

    # -------- BASIC EVENT DATA --------
    event_data = {
        "event_id": event.event_id,
        "slug": event.slug,
        # ── Owning-organization context (additive; null for native AFC events) ──
        # Lets an organizer surface verify an event belongs to the selected org
        # before opening it for edit (the organizer edit page guards on
        # organization_slug == the selected org's slug). The org-scoped events list
        # (get_all_events) already exposes these same three keys, but that list omits
        # drafts; this endpoint returns drafts too, so it's the reliable guard source
        # for the organizer edit page. Consumed by:
        #   frontend app/(organizer)/organizer/events/[slug]/edit/page.tsx
        "organization_id": event.organization_id,
        "organization_name": event.organization.name if event.organization_id else None,
        "organization_slug": event.organization.slug if event.organization_id else None,
        "competition_type": event.competition_type,
        "participant_type": event.participant_type,
        "event_type": event.event_type,
        "max_teams_or_players": event.max_teams_or_players,
        "event_name": event.event_name,
        "event_mode": event.event_mode,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "registration_open_date": event.registration_open_date,
        "registration_end_date": event.registration_end_date,
        "prizepool": event.prizepool,
        "prize_distribution": event.prize_distribution,
        # Paid registration (feature "paid-events"): the event page decides free vs paid + fee.
        "registration_type": event.registration_type,
        "registration_fee": event.registration_fee,
        "registration_fee_currency": event.registration_fee_currency,
        "event_rules": event.event_rules,
        "event_status": event.event_status,
        "registration_link": event.registration_link,
        "tournament_tier": event.tournament_tier,
        "registration_restriction": event.registration_restriction,
        "restriction_mode": event.restriction_mode,
        "restricted_countries": event.restricted_countries,
        "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
        "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
        "number_of_stages": event.number_of_stages,
        "created_at": event.created_at,
        "is_registered": is_registered,
        "stream_channels": list(event.stream_channels.values_list("channel_url", flat=True)),
        "is_public": event.is_public,
        "is_sponsored": event.is_sponsored,
        "sponsor_name": event.sponsor_name,
        "sponsor_field_label": event.sponsor_field_label,
        "sponsor_requirement_description": event.sponsor_requirement_description,
        "sponsors": [
            {
                "sponsor_id": se.sponsor.user_id,
                "sponsor_name": se.sponsor.full_name,
                "sponsor_username": se.sponsor.username
            }
            for se in sponsors
        ],
        # M: waitlist flags. Keys were previously emitted with stray spaces
        # ("is_waitlist enabled") so the frontend could never read them — fixed to
        # clean snake_case keys the EventDetails interface can consume.
        "is_waitlist_enabled": event.is_waitlist_enabled,
        # Media registration criteria (owner 2026-06-12): shown on the event pages + wizard toggles.
        "require_team_logo": event.require_team_logo,
        "require_esport_images": event.require_esport_images,
        "waitlist_capacity": event.waitlist_capacity,
        "waitlist_discord_role_id": event.waitlist_discord_role_id,
        # K: registration/event window times ("HH:MM"). The frontend already combines
        # these with the *_date fields to gate the Register button; they simply were
        # never serialized here, so the gate always fell back to date-only.
        "registration_start_time": event.registration_start_time,
        "registration_end_time": event.registration_end_time,
        "event_start_time": event.event_start_time,
        "event_end_time": event.event_end_time,
        # M: capacity snapshot so the frontend can switch Register -> Join Waitlist
        # once the active roster is full (matches register_for_event enforcement).
        "registered_count": active_registered,
        "is_full": active_registered >= event.max_teams_or_players,
    }

    # ============================================================
    # REGISTERED COMPETITORS (SOLO ONLY)
    # ============================================================
    registered = []

    if event.participant_type == "solo":
        regs = (
            RegisteredCompetitors.objects
            .select_related("user")
            .filter(event=event)
        )

        for reg in regs:
            if reg.user:
                registered.append({
                    "registered_competitor_id": reg.id,
                    "player_id": reg.user.user_id,
                    "username": reg.user.username,
                    # uid + full_name so the admin "Registered Teams/Players" view can show
                    # full player identity, not just the in-game name (owner request 2026-06-09).
                    "uid": reg.user.uid,
                    "full_name": reg.user.full_name,
                    "status": reg.status
                })

    event_data["registered_competitors"] = registered

    

    # ============================================================
    # TOURNAMENT TEAMS (DUO / SQUAD)
    # ============================================================
    tournament_teams_list = []

    if event.participant_type in ["duo", "squad"]:
        tournament_teams = (
            event.tournament_teams
            .select_related("team")
            .prefetch_related("members__user")
            .all()
        )

        for tt in tournament_teams:
            tournament_teams_list.append({
                "tournament_team_id": tt.tournament_team_id,
                "team_id": tt.team.team_id,
                "team_name": tt.team.team_name,
                "status": tt.status,
                # Full player roster of THIS registered team so the admin "Registered
                # Teams" view can expand a team to its players (owner request 2026-06-09).
                # username is the in-game name; uid + full_name give full identity; the
                # per-member status is the roster snapshot status (TournamentTeamMember).
                "members": [
                    {
                        "player_id": m.user.user_id,
                        "username": m.user.username,
                        "uid": m.user.uid,
                        "full_name": m.user.full_name,
                        "status": m.status,
                    }
                    for m in tt.members.all()
                ]
            })

    event_data["tournament_teams"] = tournament_teams_list


    # get waitlist competitors
    waitlist = []
    if event.participant_type == "solo":
        waitlist_regs = (
            RegisteredCompetitors.objects
            .select_related("user")

            .filter(event=event, status="waitlist")
        )
        for reg in waitlist_regs:
            if reg.user:
                waitlist.append({
                    "registered_competitor_id": reg.id,
                    "player_id": reg.user.user_id,
                    "username": reg.user.username,
                    "status": reg.status
                })
    event_data["waitlist_competitors"] = waitlist

    # ============================================================
    # STAGES / GROUPS / MATCHES
    # ============================================================
    stages_payload = []

    stages = event.stages.all().order_by("start_date", "stage_id")

    for stage in stages:
        groups_payload = []

        groups = stage.groups.all().order_by("group_name", "group_id")

        for group in groups:
            lb = Leaderboard.objects.filter(
                event=event,
                stage=stage,
                group=group
            ).first()

            matches = Match.objects.filter(group=group).order_by("match_number")

            matches_payload = []

            for match in matches:

                if event.participant_type == "solo":
                    stats = (
                        SoloPlayerMatchStats.objects
                        .filter(match=match)
                        .select_related("competitor__user")
                        .values(
                            "id",
                            "competitor_id",
                            "competitor__user__username",
                            "placement",
                            "kills",
                            "placement_points",
                            "kill_points",
                            "total_points",
                        )
                        .order_by("-total_points", "-kills", "competitor__user__username")
                    )
                else:
                    stats = (
                        TournamentTeamMatchStats.objects
                        .filter(match=match)
                        .select_related("tournament_team__team")
                        .values(
                            "team_stats_id",
                            "tournament_team_id",
                            "tournament_team__team__team_name",
                            "placement",
                            "kills",
                            "placement_points",
                            "kill_points",
                            "total_points",
                        )
                        .order_by("-total_points", "-kills", "tournament_team__team__team_name")
                    )

                matches_payload.append({
                    "match_id": match.match_id,
                    "match_number": match.match_number,
                    "match_map": match.match_map,
                    "result_inputted": match.result_inputted,
                    "room_id": match.room_id,
                    "room_name": match.room_name,
                    "room_password": match.room_password,
                    "stats": list(stats),
                })

            # -------- OVERALL LEADERBOARD --------
            if event.participant_type == "solo":
                overall = (
                    SoloPlayerMatchStats.objects
                    .filter(match__group=group)
                    .values("competitor_id", "competitor__user__username")
                    .annotate(
                        matches_played=Count("match_id", distinct=True),
                        total_kills=Sum("kills"),
                        total_points=Sum("total_points"),
                    )
                    .order_by("-total_points", "-total_kills", "competitor__user__username")
                )
            else:
                overall = (
                    TournamentTeamMatchStats.objects
                    .filter(match__group=group)
                    .values(
                        "tournament_team_id",
                        "tournament_team__team__team_name"
                    )
                    .annotate(
                        matches_played=Count("match_id", distinct=True),
                        total_kills=Sum("kills"),
                        total_points=Sum("total_points"),
                    )
                    .order_by("-total_points", "-total_kills", "tournament_team__team__team_name")
                )

            groups_payload.append({
                "group_id": group.group_id,
                "group_name": group.group_name,
                "playing_date": group.playing_date,
                "playing_time": group.playing_time,
                "teams_qualifying": group.teams_qualifying,
                "prizepool": group.prizepool,
                "prizepool_cash_value": group.prizepool_cash_value,
                "prize_distribution": group.prize_distribution,
                "match_count": group.match_count,
                "match_maps": group.match_maps,
                "leaderboard": None if not lb else {
                    "leaderboard_id": lb.leaderboard_id,
                    "leaderboard_name": lb.leaderboard_name,
                    "placement_points": lb.placement_points,
                    "kill_point": lb.kill_point,
                    "leaderboard_method": lb.leaderboard_method,
                    "file_type": lb.file_type,
                    "last_updated": lb.last_updated,
                },
                "matches": matches_payload,
                "overall_leaderboard": list(overall)
            })

        stages_payload.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "start_date": stage.start_date,
            "end_date": stage.end_date,
            "prizepool": stage.prizepool,
            "prizepool_cash_value": stage.prizepool_cash_value,
            "prize_distribution": stage.prize_distribution,
            "stage_format": stage.stage_format,
            # ── Scoring-mode config echo so the edit form can re-hydrate the toggles.
            # point_rush_target_stage_id is the stage_id of the target; the FE maps it
            # back to a 0-based index when populating point_rush_target_index. ──
            "champion_point_enabled": stage.champion_point_enabled,
            "champion_point_threshold": stage.champion_point_threshold,
            "point_rush_enabled": stage.point_rush_enabled,
            "point_rush_reward": stage.point_rush_reward or {},
            "point_rush_target_stage_id": stage.point_rush_target_stage_id,
            "stage_status": stage.stage_status,
            "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
            "groups": groups_payload,
            # ── BR Round-Robin echo (None for every other format): base groups + the
            # game-day lobbies' source group ids, so the FE stage builder can rehydrate. ──
            "round_robin": _round_robin_stage_echo(stage),
        })

    event_data["stages"] = stages_payload

    # -------- PAGE VIEW TRACKING (hardened: bots, staff, org members + refreshes excluded) --------
    _record_event_view(request, event, user)

    return Response({"event_details": event_data}, status=200)



# @api_view(["POST"])
# def get_event_details(request):
#     user = None  # ✅ prevent UnboundLocalError

#     # -------- OPTIONAL AUTH --------
#     auth = request.headers.get("Authorization")
#     if auth and auth.startswith("Bearer "):
#         token = auth.split(" ")[1]
#         user = validate_token(token)
#         if not user:
#             return Response(
#                 {"message": "Invalid or expired session token."},
#                 status=status.HTTP_401_UNAUTHORIZED
#             )

#     # -------- INPUT --------
#     slug = request.data.get("slug")
#     if not slug:
#         return Response({"message": "slug is required."}, status=400)

#     event = get_object_or_404(Event, slug=slug)

#     # -------- IS REGISTERED --------
#     is_registered = False
#     if user:
#         if event.participant_type == "solo":
#             is_registered = RegisteredCompetitors.objects.filter(
#                 event=event,
#                 user=user,
#                 status="registered"
#             ).exists()
#         else:
#             is_registered = RegisteredCompetitors.objects.filter(
#                 event=event,
#                 team__teammembers_set__member=user,  # ✅ FIXED
#                 status="registered"
#             ).exists()

#     # -------- BASIC EVENT DATA --------
#     event_data = {
#         "event_id": event.event_id,
#         "competition_type": event.competition_type,
#         "participant_type": event.participant_type,
#         "event_type": event.event_type,
#         "max_teams_or_players": event.max_teams_or_players,
#         "event_name": event.event_name,
#         "event_mode": event.event_mode,
#         "start_date": event.start_date,
#         "end_date": event.end_date,
#         "registration_open_date": event.registration_open_date,
#         "registration_end_date": event.registration_end_date,
#         "prizepool": event.prizepool,
#         "prize_distribution": event.prize_distribution,
#         "event_rules": event.event_rules,
#         "event_status": event.event_status,
#         "registration_link": event.registration_link,
#         "tournament_tier": event.tournament_tier,
#         "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
#         "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
#         "number_of_stages": event.number_of_stages,
#         "created_at": event.created_at,
#         "is_registered": is_registered,
#         "stream_channels": list(event.stream_channels.values_list("channel_url", flat=True)),
#     }

#     # -------- REGISTERED COMPETITORS --------
#     registered = []

#     if event.participant_type == "solo":
#         regs = (
#             RegisteredCompetitors.objects
#             .select_related("user")
#             .filter(event=event)
#         )

#         for reg in regs:
#             if reg.user:
#                 registered.append({
#                     "registered_competitor_id": reg.id,
#                     "player_id": reg.user.user_id,
#                     "username": reg.user.username,
#                     "status": reg.status
#                 })
#     else:
#         regs = (
#             RegisteredCompetitors.objects
#             .select_related("team")
#             .prefetch_related("team__teammembers_set__member")  # ✅ FIXED
#             .filter(event=event)
#         )

#         for reg in regs:
#             if reg.team:
#                 members = reg.team.teammembers_set.all()  # ✅ FIXED

#                 registered.append({
#                     "registered_competitor_id": reg.id,
#                     "team_id": reg.team.team_id,
#                     "team_name": reg.team.team_name,
#                     "status": reg.status,
#                     "members": [
#                         {
#                             "player_id": m.member.user_id,
#                             "username": m.member.username,
#                             "role": m.in_game_role
#                         }
#                         for m in members
#                     ]
#                 })

#     event_data["registered_competitors"] = registered

#     # -------- TOURNAMENT TEAMS --------
#     tournament_teams_list = []

#     for tt in (
#         event.tournament_teams
#         .select_related("team")
#         .prefetch_related("members__user")
#         .all()
#     ):
#         tournament_teams_list.append({
#             "tournament_team_id": tt.tournament_team_id,
#             "team_id": tt.team.team_id,
#             "team_name": tt.team.team_name,
#             "members": [
#                 {
#                     "player_id": m.user.user_id,
#                     "username": m.user.username
#                 }
#                 for m in tt.members.all()
#             ]
#         })

#     event_data["tournament_teams"] = tournament_teams_list

#     # -------- STAGES / GROUPS / MATCHES --------
#     stages_payload = []

#     stages = event.stages.all().order_by("start_date", "stage_id")

#     for stage in stages:
#         groups_payload = []

#         groups = stage.groups.all().order_by("group_name", "group_id")

#         for group in groups:
#             lb = Leaderboard.objects.filter(
#                 event=event,
#                 stage=stage,
#                 group=group
#             ).first()

#             matches = Match.objects.filter(group=group).order_by("match_number")

#             matches_payload = []

#             for match in matches:

#                 if event.participant_type == "solo":
#                     stats = (
#                         SoloPlayerMatchStats.objects
#                         .filter(match=match)
#                         .select_related("competitor__user")
#                         .values(
#                             "id",
#                             "competitor_id",
#                             "competitor__user__username",
#                             "placement",
#                             "kills",
#                             "placement_points",
#                             "kill_points",
#                             "total_points",
#                         )
#                         .order_by("-total_points", "-kills", "competitor__user__username")
#                     )
#                 else:
#                     stats = (
#                         TournamentTeamMatchStats.objects
#                         .filter(match=match)
#                         .select_related("tournament_team__team")
#                         .values(
#                             "team_stats_id",
#                             "tournament_team_id",
#                             "tournament_team__team__team_name",
#                             "placement",
#                             "kills",
#                             "placement_points",
#                             "kill_points",
#                             "total_points",
#                         )
#                         .order_by("-total_points", "-kills", "tournament_team__team__team_name")
#                     )

#                 matches_payload.append({
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "result_inputted": match.result_inputted,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "stats": list(stats),
#                 })

#             # -------- OVERALL --------
#             if event.participant_type == "solo":
#                 overall = (
#                     SoloPlayerMatchStats.objects
#                     .filter(match__group=group)
#                     .values("competitor_id", "competitor__user__username")
#                     .annotate(
#                         matches_played=Count("match_id", distinct=True),
#                         total_kills=Sum("kills"),
#                         total_points=Sum("total_points"),
#                     )
#                     .order_by("-total_points", "-total_kills", "competitor__user__username")
#                 )
#             else:
#                 overall = (
#                     TournamentTeamMatchStats.objects
#                     .filter(match__group=group)
#                     .values("tournament_team_id", "tournament_team__team__team_name")
#                     .annotate(
#                         matches_played=Count("match_id", distinct=True),
#                         total_kills=Sum("kills"),
#                         total_points=Sum("total_points"),
#                     )
#                     .order_by("-total_points", "-total_kills", "tournament_team__team__team_name")
#                 )

#             groups_payload.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": None if not lb else {
#                     "leaderboard_id": lb.leaderboard_id,
#                     "leaderboard_name": lb.leaderboard_name,
#                     "placement_points": lb.placement_points,
#                     "kill_point": lb.kill_point,
#                     "leaderboard_method": lb.leaderboard_method,
#                     "file_type": lb.file_type,
#                     "last_updated": lb.last_updated,
#                 },
#                 "matches": matches_payload,
#                 "overall_leaderboard": list(overall),
#             })

#         stages_payload.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": groups_payload,
#         })

#     event_data["stages"] = stages_payload

#     # -------- PAGE VIEW --------
#     EventPageView.objects.create(
#         event=event,
#         user=user,
#         ip_address=get_client_ip(request),
#         viewed_at=timezone.now()
#     )

#     return Response({"event_details": event_data}, status=200)


# @api_view(["POST"])
# def get_event_details(request):
#     if request.headers.get("Authorization") is not None:
#         session_token = request.headers.get("Authorization")
#         if session_token and session_token.startswith("Bearer "):
#             token = session_token.split(" ")[1]
#             user = validate_token(token)
#             if not user:
#                 return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

#     # if not session_token or not session_token.startswith("Bearer "):
#     #     return Response({"message": "Invalid or missing Authorization token."}, status=400)


#     # event_id = request.data.get("event_id")
#     slug = request.data.get("slug")
#     if not slug:
#         return Response({"message": "slug is required."}, status=400)

#     event = get_object_or_404(Event, slug=slug)

#     # ✅ correct "is_registered" (must include event)
#     is_registered = False
#     if event.participant_type == "solo":
#         is_registered = RegisteredCompetitors.objects.filter(event=event, user=user, status="registered").exists()
#     else:
#         # optional: for team events you can check if user is in any registered team for this event
#         is_registered = RegisteredCompetitors.objects.filter(event=event, team__teammembers__member=user, status="registered").exists()

#     event_data = {
#         "event_id": event.event_id,
#         "competition_type": event.competition_type,
#         "participant_type": event.participant_type,
#         "event_type": event.event_type,
#         "max_teams_or_players": event.max_teams_or_players,
#         "event_name": event.event_name,
#         "event_mode": event.event_mode,
#         "start_date": event.start_date,
#         "end_date": event.end_date,
#         "registration_open_date": event.registration_open_date,
#         "registration_end_date": event.registration_end_date,
#         "prizepool": event.prizepool,
#         "prize_distribution": event.prize_distribution,
#         "event_rules": event.event_rules,
#         "event_status": event.event_status,
#         "registration_link": event.registration_link,
#         "tournament_tier": event.tournament_tier,
#         "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
#         "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
#         "number_of_stages": event.number_of_stages,
#         "created_at": event.created_at,
#         "is_registered": is_registered,
#         "stream_channels": list(event.stream_channels.values_list("channel_url", flat=True)),
#     }

#     # ✅ KEEP registered competitors section (as you requested)
#     registered = []
#     if event.participant_type == "solo":
#         regs = (RegisteredCompetitors.objects
#                 .select_related("user")
#                 .filter(event=event))
#         for reg in regs:
#             if reg.user:
#                 registered.append({
#                     "registered_competitor_id": reg.id,
#                     "player_id": reg.user.user_id,
#                     "username": reg.user.username,
#                     "status": reg.status
#                 })
#     else:
#         regs = (RegisteredCompetitors.objects
#                 .select_related("team")
#                 .prefetch_related("team__teammembers_set__member")
#                 .filter(event=event))
#         for reg in regs:
#             if reg.team:
#                 members = reg.team.teammembers.all()
#                 registered.append({
#                     "registered_competitor_id": reg.id,
#                     "team_id": reg.team.team_id,
#                     "team_name": reg.team.team_name,
#                     "status": reg.status,
#                     "members": [
#                         {"player_id": m.member.id, "username": m.member.username, "role": m.in_game_role}
#                         for m in members
#                     ]
#                 })
#     event_data["registered_competitors"] = registered

#     # Tournament teams (accepted)
#     tournament_teams_list = []
#     for tt in event.tournament_teams.select_related("team").prefetch_related("members__user").all():
#         tournament_teams_list.append({
#             "tournament_team_id": tt.tournament_team_id,
#             "team_id": tt.team.team_id,
#             "team_name": tt.team.team_name,
#             "members": [{"player_id": m.user.id, "username": m.user.username} for m in tt.members.all()]
#         })
#     event_data["tournament_teams"] = tournament_teams_list

#     # -------- stages / groups / leaderboards / matches (+ stats) --------
#     stages_payload = []
#     stages = event.stages.all().order_by("start_date", "stage_id")

#     for stage in stages:
#         groups_payload = []
#         groups = stage.groups.all().order_by("group_name", "group_id")

#         for group in groups:
#             lb = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

#             matches = Match.objects.filter(group=group).order_by("match_number")

#             matches_payload = []
#             for match in matches:
#                 if event.participant_type == "solo":
#                     stats = (SoloPlayerMatchStats.objects
#                              .filter(match=match)
#                              .select_related("competitor__user")
#                              .values(
#                                  "id",
#                                  "competitor_id",
#                                  "competitor__user__username",
#                                  "placement",
#                                  "kills",
#                                  "placement_points",
#                                  "kill_points",
#                                  "total_points",
#                              )
#                              .order_by("-total_points", "-kills", "competitor__user__username"))
#                 else:
#                     stats = (TournamentTeamMatchStats.objects
#                              .filter(match=match)
#                              .select_related("tournament_team__team")
#                              .values(
#                                  "team_stats_id",
#                                  "tournament_team_id",
#                                  "tournament_team__team__team_name",
#                                  "placement",
#                                  "kills",
#                                  "placement_points",
#                                  "kill_points",
#                                  "total_points",
#                              )
#                              .order_by("-total_points", "-kills", "tournament_team__team__team_name"))

#                 matches_payload.append({
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "result_inputted": match.result_inputted,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "stats": list(stats),
#                 })

#             # overall leaderboard for group
#             if event.participant_type == "solo":
#                 overall = (SoloPlayerMatchStats.objects
#                            .filter(match__group=group)
#                            .values("competitor_id", "competitor__user__username")
#                            .annotate(
#                                matches_played=Count("match_id", distinct=True),
#                                total_kills=Sum("kills"),
#                                total_points=Sum("total_points"),
#                            )
#                            .order_by("-total_points", "-total_kills", "competitor__user__username"))
#             else:
#                 overall = (TournamentTeamMatchStats.objects
#                            .filter(match__group=group)
#                            .values("tournament_team_id", "tournament_team__team__team_name")
#                            .annotate(
#                                matches_played=Count("match_id", distinct=True),
#                                total_kills=Sum("kills"),
#                                total_points=Sum("total_points"),
#                            )
#                            .order_by("-total_points", "-total_kills", "tournament_team__team__team_name"))

#             groups_payload.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": None if not lb else {
#                     "leaderboard_id": lb.leaderboard_id,
#                     "leaderboard_name": lb.leaderboard_name,
#                     "placement_points": lb.placement_points,
#                     "kill_point": lb.kill_point,
#                     "leaderboard_method": lb.leaderboard_method,
#                     "file_type": lb.file_type,
#                     "last_updated": lb.last_updated,
#                 },
#                 "matches": matches_payload,
#                 "overall_leaderboard": list(overall),
#             })

#         stages_payload.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": groups_payload,
#         })

#     event_data["stages"] = stages_payload

#     EventPageView.objects.create(
#         event=event,
#         user=user,
#         ip_address=get_client_ip(request),
#         viewed_at=timezone.now()
#     )
#     return Response({"event_details": event_data}, status=200)



@api_view(["POST"])
def get_event_details_not_logged_in(request):
    slug = request.data.get("slug")
    if not slug:
        return Response({"message": "slug is required."}, status=400)

    event = get_object_or_404(Event.objects.select_related("organization"), slug=slug)

    # Owner rule 2026-06-11: a suspended/deleted org's events must not show publicly. This is the
    # logged-out public detail endpoint, so a hidden org always 404s here. AFC-native events unaffected.
    if _org_hidden(event):
        return Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)

    sponsors = SponsorEvent.objects.filter(event=event).select_related("sponsor").all()

    # M: capacity snapshot (same counting rule as register_for_event) so the public,
    # logged-out event page can show "Registration full / Join Waitlist" too.
    if event.participant_type == "solo":
        active_registered = RegisteredCompetitors.objects.filter(
            event=event, status__in=["registered", "approved"], is_waitlisted=False
        ).count()
    else:
        active_registered = TournamentTeam.objects.filter(
            event=event, is_waitlisted=False
        ).count()

    event_data = {
        "event_id": event.event_id,
        "competition_type": event.competition_type,
        "participant_type": event.participant_type,
        "event_type": event.event_type,
        "max_teams_or_players": event.max_teams_or_players,
        "event_name": event.event_name,
        "event_mode": event.event_mode,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "registration_open_date": event.registration_open_date,
        "registration_end_date": event.registration_end_date,
        "prizepool": event.prizepool,
        "prize_distribution": event.prize_distribution,
        # Paid registration (feature "paid-events"): the event page decides free vs paid + fee.
        "registration_type": event.registration_type,
        "registration_fee": event.registration_fee,
        "registration_fee_currency": event.registration_fee_currency,
        "event_rules": event.event_rules,
        "event_status": event.event_status,
        "registration_link": event.registration_link,
        "tournament_tier": event.tournament_tier,
        "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
        "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
        "number_of_stages": event.number_of_stages,
        "created_at": event.created_at,
        # "is_registered": is_registered,
        "stream_channels": list(event.stream_channels.values_list("channel_url", flat=True)),
        "is_public": event.is_public,
        "is_sponsored": event.is_sponsored,
        "sponsor_name": event.sponsor_name,
        "sponsor_field_label": event.sponsor_field_label,
        "sponsor_requirement_description": event.sponsor_requirement_description,
        "sponsors": [
            {
                "sponsor_id": se.sponsor.user_id,
                "sponsor_name": se.sponsor.full_name,
                "sponsor_username": se.sponsor.username
            }
            for se in sponsors
        ],
        # K: registration/event window times ("HH:MM"), mirrored from the logged-in
        # response so the public page gates registration on the same window.
        "registration_start_time": event.registration_start_time,
        "registration_end_time": event.registration_end_time,
        "event_start_time": event.event_start_time,
        "event_end_time": event.event_end_time,
        # M: waitlist flags + capacity snapshot for the logged-out register CTA.
        "is_waitlist_enabled": event.is_waitlist_enabled,
        # Media registration criteria (owner 2026-06-12): shown on the event pages + wizard toggles.
        "require_team_logo": event.require_team_logo,
        "require_esport_images": event.require_esport_images,
        "waitlist_capacity": event.waitlist_capacity,
        "registered_count": active_registered,
        "is_full": active_registered >= event.max_teams_or_players,
    }

    # ✅ KEEP registered competitors section (as you requested)
    registered = []
    if event.participant_type == "solo":
        regs = (RegisteredCompetitors.objects
                .select_related("user")
                .filter(event=event))
        for reg in regs:
            if reg.user:
                registered.append({
                    "registered_competitor_id": reg.id,
                    "player_id": reg.user.user_id,
                    "username": reg.user.username,
                    "status": reg.status
                })
    else:
        regs = (RegisteredCompetitors.objects
                .select_related("team")
                .prefetch_related("team__memberships__member")
                .filter(event=event))
        for reg in regs:
            if reg.team:
                members = reg.team.memberships.all()
                registered.append({
                    "registered_competitor_id": reg.id,
                    "team_id": reg.team.team_id,
                    "team_name": reg.team.team_name,
                    "status": reg.status,
                    "members": [
                        {"player_id": m.member.user_id, "username": m.member.username, "role": m.in_game_role}
                        for m in members
                    ]
                })
    event_data["registered_competitors"] = registered

    # Tournament teams (accepted)
    tournament_teams_list = []
    for tt in event.tournament_teams.select_related("team").prefetch_related("members__user").all():
        tournament_teams_list.append({
            "tournament_team_id": tt.tournament_team_id,
            "team_id": tt.team.team_id,
            "team_name": tt.team.team_name,
            "members": [{"player_id": m.user.user_id, "username": m.user.username} for m in tt.members.all()]
        })
    event_data["tournament_teams"] = tournament_teams_list

    # -------- stages / groups / leaderboards / matches (+ stats) --------
    stages_payload = []
    stages = event.stages.all().order_by("start_date", "stage_id")

    for stage in stages:
        groups_payload = []
        groups = stage.groups.all().order_by("group_name", "group_id")

        for group in groups:
            lb = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

            matches = Match.objects.filter(group=group).order_by("match_number")

            matches_payload = []
            for match in matches:
                if event.participant_type == "solo":
                    stats = (SoloPlayerMatchStats.objects
                             .filter(match=match)
                             .select_related("competitor__user")
                             .values(
                                 "id",
                                 "competitor_id",
                                 "competitor__user__username",
                                 "placement",
                                 "kills",
                                 "placement_points",
                                 "kill_points",
                                 "total_points",
                             )
                             .order_by("-total_points", "-kills", "competitor__user__username"))
                else:
                    stats = (TournamentTeamMatchStats.objects
                             .filter(match=match)
                             .select_related("tournament_team__team")
                             .values(
                                 "team_stats_id",
                                 "tournament_team_id",
                                 "tournament_team__team__team_name",
                                 "placement",
                                 "kills",
                                 "placement_points",
                                 "kill_points",
                                 "total_points",
                             )
                             .order_by("-total_points", "-kills", "tournament_team__team__team_name"))

                matches_payload.append({
                    "match_id": match.match_id,
                    "match_number": match.match_number,
                    "match_map": match.match_map,
                    "result_inputted": match.result_inputted,
                    "room_id": match.room_id,
                    "room_name": match.room_name,
                    "room_password": match.room_password,
                    "stats": list(stats),
                })

            # overall leaderboard for group
            if event.participant_type == "solo":
                overall = (SoloPlayerMatchStats.objects
                           .filter(match__group=group)
                           .values("competitor_id", "competitor__user__username")
                           .annotate(
                               matches_played=Count("match_id", distinct=True),
                               total_kills=Sum("kills"),
                               total_points=Sum("total_points"),
                           )
                           .order_by("-total_points", "-total_kills", "competitor__user__username"))
            else:
                overall = (TournamentTeamMatchStats.objects
                           .filter(match__group=group)
                           .values("tournament_team_id", "tournament_team__team__team_name")
                           .annotate(
                               matches_played=Count("match_id", distinct=True),
                               total_kills=Sum("kills"),
                               total_points=Sum("total_points"),
                           )
                           .order_by("-total_points", "-total_kills", "tournament_team__team__team_name"))

            groups_payload.append({
                "group_id": group.group_id,
                "group_name": group.group_name,
                "playing_date": group.playing_date,
                "playing_time": group.playing_time,
                "teams_qualifying": group.teams_qualifying,
                "match_count": group.match_count,
                "match_maps": group.match_maps,
                "leaderboard": None if not lb else {
                    "leaderboard_id": lb.leaderboard_id,
                    "leaderboard_name": lb.leaderboard_name,
                    "placement_points": lb.placement_points,
                    "kill_point": lb.kill_point,
                    "leaderboard_method": lb.leaderboard_method,
                    "file_type": lb.file_type,
                    "last_updated": lb.last_updated,
                },
                "matches": matches_payload,
                "overall_leaderboard": list(overall),
            })

        stages_payload.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "start_date": stage.start_date,
            "end_date": stage.end_date,
            "stage_format": stage.stage_format,
            "stage_status": stage.stage_status,
            "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
            "groups": groups_payload,
        })

    event_data["stages"] = stages_payload

    # EventPageView.objects.create(
    #     event=event,
    #     user=user,
    #     ip_address=get_client_ip(request),
    #     viewed_at=timezone.now()
    # )
    return Response({"event_details": event_data}, status=200)



import json
from datetime import date
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings

ALLOWED_REGISTER_ROLES = ["team_captain", "vice_captain"]

def _maybe_json_list(val):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []

# def _user_is_team_captain_or_owner(user, team) -> bool:
#     if user.user_id == team.team_owner_id:
#         return True
#     return TeamMembers.objects.filter(
#         team=team,
#         member=user,
#         management_role__in=ALLOWED_REGISTER_ROLES
#     ).exists()

def _user_is_team_captain_or_owner(user, team) -> bool:

    # ✅ Check owner from Team model
    if user == team.team_owner:
        return True

    # ✅ Check captain / vice captain from TeamMembers
    return TeamMembers.objects.filter(
        team=team,
        member=user,
        management_role__in=ALLOWED_REGISTER_ROLES
    ).exists()


# def _passes_event_country_restriction(event, country: str) -> bool:
#     restriction = (event.registration_restriction or "none").lower()

#     if restriction == "none":
#         return True

#     # normalize
#     user_country = (country or "").strip().lower()

#     if not user_country:
#         return False  # cannot verify → reject

#     restricted = set(
#         c.strip().lower()
#         for c in (event.restricted_countries or [])
#         if c
#     )

#     mode = (event.restriction_mode or "").lower()

#     # ---------------- ALLOW ONLY ----------------
#     if mode == "allow_only":
#         return user_country in restricted

#     # ---------------- BLOCK SELECTED ----------------
#     if mode == "block_selected":
#         return user_country not in restricted

#     # fallback safety
#     return False

def _passes_event_country_restriction(event, country: str) -> bool:

    restriction = (event.registration_restriction or "none").lower()

    if restriction == "none":
        return True

    # normalize user country
    user_country = normalize_country(country)

    if not user_country:
        return False

    # normalize restricted countries
    restricted = set(
        normalize_country(c)
        for c in (event.restricted_countries or [])
        if c
    )

    mode = (event.restriction_mode or "").lower()

    # ---------------- ALLOW ONLY ----------------
    if mode == "allow_only":
        return user_country in restricted

    # ---------------- BLOCK SELECTED ----------------
    if mode == "block_selected":
        return user_country not in restricted

    return False

# def _passes_event_country_restriction(event, country: str) -> bool:
#     """
#     Assumes:
#       event.registration_restriction: none/by_region/by_country
#       event.restriction_mode: allow_only/block_selected
#       event.restricted_countries: JSON list[str]
#     """
#     restriction = getattr(event, "registration_restriction", "none") or "none"
#     if restriction == "none":
#         return True

#     mode = getattr(event, "restriction_mode", None)
#     allowed = set([c.strip().lower() for c in (getattr(event, "restricted_countries", []) or [])])
#     user_country = (country or "").strip().lower()

#     # if restrictions enabled and no country stored -> fail
#     if not user_country:
#         return False

#     if mode == "allow_only":
#         return user_country in allowed
#     if mode == "block_selected":
#         return user_country not in allowed

#     return False


from collections import Counter

import pycountry

import pycountry

def normalize_country(country):
    if not country:
        return ""

    country = country.strip()

    # -------- TRY ISO CODE (NG, US, GB) --------
    c = pycountry.countries.get(alpha_2=country.upper())
    if c:
        return c.name.lower()

    # -------- TRY FULL NAME / FUZZY --------
    try:
        return pycountry.countries.lookup(country).name.lower()
    except LookupError:
        return country.lower()

def determine_team_country(roster_users, team_owner):

    countries = [
        u.country.strip().lower()
        for u in roster_users
        if u.country and u.country.strip()
    ]

    owner_country = (team_owner.country or "").strip().lower()

    # no valid player country → fallback
    if not countries:
        return owner_country

    counts = Counter(countries)
    most_common = counts.most_common()

    # only one country
    if len(most_common) == 1:
        return most_common[0][0]

    # tie
    if most_common[0][1] == most_common[1][1]:
        return owner_country

    return most_common[0][0]

# def determine_team_country(roster_users, team_owner):
#     countries = [u.country for u in roster_users if u.country]

#     if not countries:
#         return team_owner.country

#     counts = Counter(countries)
#     most_common = counts.most_common()

#     if len(most_common) == 1:
#         return most_common[0][0]

#     # tie
#     if most_common[0][1] == most_common[1][1]:
#         return team_owner.country

#     return most_common[0][0]


@api_view(["POST"])
def register_for_event(request):
    # -------------------------
    # AUTH
    # -------------------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    if user.status != "active":
        return Response({"message": "Your account is not active."}, status=403)

    # -------------------------
    # INPUT
    # -------------------------
    event_id = request.data.get("event_id")
    team_id = request.data.get("team_id")  # for duo/squad
    roster_member_ids = _maybe_json_list(request.data.get("roster_member_ids"))
    sponsor_ids = _maybe_json(request.data.get("sponsor_ids"), default={})

    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    participant_type = event.participant_type  # solo/duo/squad
    is_public = event.is_public  # True/False

    # -------------------------
    # REG WINDOW CHECK
    # -------------------------
    today = date.today()
    if not (event.registration_open_date <= today <= event.registration_end_date):
        return Response({"message": "Registration is closed."}, status=403)

    # -------------------------
    # PAID EVENT GATE (feature "paid-events", Phase 1)
    # -------------------------
    # A paid event can only be registered for AFTER the entry fee is paid. The fee is charged via
    # Stripe (held in AFC's Stripe balance) by event_payments.init_registration_payment; once that
    # row is status="paid", registration is allowed. This makes a paid registration safe to
    # complete even if the user closed the tab after paying (their paid record persists).
    if event.registration_type == "paid":
        from .models import EventRegistrationPayment
        has_paid = EventRegistrationPayment.objects.filter(
            event=event, user=user, status="paid"
        ).exclude(release_status="refunded").exists()
        if not has_paid:
            return Response(
                {"message": "Payment required. Please pay the entry fee to register.",
                 "code": "payment_required"},
                status=402,
            )

    # -------------------------
    # SOLO
    # -------------------------
    if participant_type == "solo":
        # ✅ restriction enforcement
        if not _passes_event_country_restriction(event, user.country):
            return Response({"message": "You are not eligible to register for this event (country restriction)."}, status=403)

        existing_registration = RegisteredCompetitors.objects.filter(event=event, user=user).first()
        if existing_registration and existing_registration.status != "registered":
            return Response({"message": "You cannot rejoin this event."}, status=400)
        
        # Check If the player is banned
        if BannedPlayer.objects.filter(banned_player=user, is_active=True).exists():
            return Response({"message": "You are banned from registering for this event."}, status=403)

        # ── ESPORT-IMAGE CRITERIA (owner 2026-06-12) ──
        # When the event creator required esport images, a solo player cannot register until
        # their UserProfile.esports_pic is uploaded (replace-only asset; see
        # afc_auth.views.upload_esport_image). code lets the FE deep-link to the profile editor.
        if event.require_esport_images:
            from afc_auth.models import UserProfile
            has_esport_image = UserProfile.objects.filter(
                user=user, esports_pic__isnull=False
            ).exclude(esports_pic="").exists()
            if not has_esport_image:
                return Response({
                    "message": "This event requires an esport image. Upload yours on your profile before registering.",
                    "code": "esport_image_required",
                }, status=403)

        
        if is_public == False:
            # ── private-event invite gate (SOLO) ──
            # Mirrors the TEAM gate below — keep both in sync.
            invite_token = request.data.get("invite_token")
            if not invite_token:
                return Response({"message": "invite_token is required for private events."}, status=400)

            # Fetch the token row ONCE so shared/expiry checks all read the same record.
            invite = EventInviteToken.objects.filter(event=event, token=invite_token).first()
            if not invite:
                return Response({"message": "Invalid invite token."}, status=403)

            # Enforce expiry (previously ignored): an expired link cannot register anyone.
            if invite.expires_at and timezone.now() > invite.expires_at:
                return Response({"message": "This invite link has expired."}, status=403)

            # Single-use tokens are consumed after one registration; a SHARED token is the
            # reusable FCFS link, so it is accepted regardless of is_used. The event's
            # capacity check below (active_count >= max_teams_or_players) is what closes a
            # shared link once all slots are filled.
            if not invite.is_shared and invite.is_used:
                return Response({"message": "Invite token has already been used."}, status=403)

        # Discord checks
        if not user.discord_connected or not user.discord_id:
            return Response({"message": "Connect your Discord account first."}, status=403)

        if not check_discord_membership(user.discord_id):
            return Response({"message": "You must join the Discord server before registering."}, status=403)

        # Prevent duplicate solo registration
        if RegisteredCompetitors.objects.filter(event=event, user=user).exists():
            return Response({"message": "You are already registered."}, status=409)

        # Capacity check
        if RegisteredCompetitors.objects.filter(event=event, status="registered").count() >= event.max_teams_or_players:
            return Response({"message": "Registration limit reached."}, status=403)

        
        # -------------------------
        # SPONSOR ID UNIQUENESS CHECK
        # -------------------------
        if event.is_sponsored:

            provided_ids = [
                sponsor_ids.get(str(uid))
                for uid in roster_member_ids
                if sponsor_ids.get(str(uid))
            ]

            # Check duplicates inside the same roster request
            if len(provided_ids) != len(set(provided_ids)):
                return Response({
                    "message": "Duplicate sponsor IDs detected in roster."
                }, status=400)

            # Check duplicates already registered in this event
            existing_ids = set(
                TournamentTeamMember.objects.filter(
                    tournament_team__event=event,
                    user_id_from_sponsor__in=provided_ids
                ).values_list("user_id_from_sponsor", flat=True)
            )

            if existing_ids:
                return Response({
                    "message": "Some sponsor IDs are already used in this event.",
                    "conflicting_ids": list(existing_ids)
                }, status=409)

        with transaction.atomic():
            active_count = RegisteredCompetitors.objects.filter(
                event=event,
                status__in=["registered", "approved"],
                is_waitlisted=False
            ).count()

            if active_count >= event.max_teams_or_players:

                if not event.is_waitlist_enabled:
                    return Response({"message": "Registration limit reached."}, status=403)

                waitlist_count = RegisteredCompetitors.objects.filter(
                    event=event,
                    is_waitlisted=True
                ).count()

                if event.waitlist_capacity and waitlist_count >= event.waitlist_capacity:
                    return Response({"message": "Waitlist is full."}, status=403)

                # CREATE WAITLIST ENTRY
                competitor = RegisteredCompetitors.objects.create(
                    event=event,
                    user=user,
                    status="pending",
                    is_waitlisted=True
                )

                # assign waitlist discord role
                role_id = event.waitlist_discord_role_id

                if role_id:
                    DiscordRoleAssignment.objects.get_or_create(
                        user=user,
                        discord_id=user.discord_id,
                        role_id=role_id,
                        defaults={"status": "pending"}
                    )

                return Response({
                    "message": "Event is full. You have been added to the waitlist.",
                    "waitlisted": True
                }, status=201)
            
            competitor = RegisteredCompetitors.objects.create(
                event=event,
                user=user,
                status="registered"
            )

            # ── SPONSOR ENGAGEMENTS (sponsor redesign P3/P4, SOLO) ──
            # Entity sponsorships (afc_sponsors.EventSponsorship) carry engagement asks the
            # registrant answers in the body's `sponsorships` list:
            #   [{sponsorship_id, submissions: [{engagement_index, payload}]}]
            # Validation + writes live in afc_sponsors.engagements (one source of truth with
            # the wizard's config validation). On any problem we roll the whole registration
            # back (atomic). When a sponsorship requires approval the registration parks
            # "pending" until the sponsor approves it (decide flow in that module).
            sponsorship_entries = _maybe_json_list(request.data.get("sponsorships"))
            from afc_sponsors.engagements import create_submissions_for_registration
            solo_payloads = {user.user_id: []}
            for sp_entry in (sponsorship_entries or []):
                for sub in (sp_entry.get("submissions") or []):
                    solo_payloads[user.user_id].append({
                        "sponsorship_id": sp_entry.get("sponsorship_id"),
                        "engagement_index": sub.get("engagement_index"),
                        "payload": sub.get("payload") or {},
                    })
            sponsor_error, needs_sponsor_approval = create_submissions_for_registration(
                event, solo_payloads, user,
            )
            if sponsor_error:
                transaction.set_rollback(True)
                return Response({"message": sponsor_error, "code": "sponsor_submission_invalid"}, status=400)
            if needs_sponsor_approval:
                competitor.status = "pending"
                competitor.save(update_fields=["status"])

            # Mark the token used — but ONLY for single-use tokens. A SHARED token
            # (is_shared=True) is the reusable FCFS link and must stay open for the next
            # registrant; the capacity check above is what eventually closes it.
            if is_public == False and not invite.is_shared:
                EventInviteToken.objects.filter(event=event, token=invite_token).update(is_used=True, used_by=user, used_at=timezone.now())


            # Queue discord role
            role_id = getattr(settings, "DISCORD_TOURNAMENT_SOLO_ROLE_ID", None)
            if role_id:
                DiscordRoleAssignment.objects.get_or_create(
                    user=user,
                    discord_id=user.discord_id,
                    role_id=role_id,
                    stage=None,
                    group=None,
                    defaults={"status": "pending"}
                )
        
        

        return Response({
            "message": (
                "Registered. Your registration is pending sponsor approval."
                if needs_sponsor_approval
                else "Successfully registered (solo). Discord role queued."
            ),
            "registration_id": competitor.id,
            "pending_sponsor_approval": needs_sponsor_approval,
        }, status=201)

    # -------------------------
    # TEAM (DUO/SQUAD)
    # -------------------------
    if participant_type in ["duo", "squad"]:
        if not team_id:
            return Response({"message": "team_id is required for duo/squad."}, status=400)

        team = get_object_or_404(Team, team_id=team_id)

        # captain/owner check
        if not _user_is_team_captain_or_owner(user, team):
            return Response({"message": "Only captain/vice-captain/team owner can register the team."}, status=403)

        # ── ban guard (afc_auth.BannedPlayer + Team.is_banned) ──
        # A team event registration is blocked if the team is banned (TeamBan -> Team.is_banned)
        # or the acting/registering user is banned. Ban rule: a player is currently banned when an
        # is_active=True BannedPlayer row exists AND its ban_end_date is still in the future
        # (mirrors afc_team.views._is_player_banned; replicated inline here to avoid a cross-app
        # import in this hot path). The per-roster-member check runs later, once roster_users is
        # resolved. The SOLO path above has its own inline BannedPlayer check.
        if team.is_banned:
            return Response({"message": "Your team is banned and cannot register for events."}, status=403)
        if BannedPlayer.objects.filter(banned_player=user, is_active=True, ban_end_date__gt=timezone.now()).exists():
            return Response({"message": "You are banned and cannot register for events."}, status=403)

        # ── TEAM-LOGO CRITERIA (owner 2026-06-12) ──
        # When the event creator required team logos, a team cannot register until its logo is
        # uploaded. code lets the FE deep-link the captain to the team edit page.
        if event.require_team_logo and not team.team_logo:
            return Response({
                "message": "This event requires a team logo. Upload your team's logo before registering.",
                "code": "team_logo_required",
            }, status=403)

        # Ensure requester is in team
        if not TeamMembers.objects.filter(team=team, member=user).exists():
            return Response({"message": "You are not a member of this team."}, status=403)

        existing_registration = TournamentTeam.objects.filter(event=event, team=team).first()
        
        if existing_registration and existing_registration.status != "registered":
            return Response({"message": "You cannot rejoin this event."}, status=400)


        # Prevent duplicate team registration
        if RegisteredCompetitors.objects.filter(event=event, team=team).exists():
            return Response({"message": "Team already registered."}, status=409)

        if event.is_sponsored:
            if not sponsor_ids:
                return Response({
                    "message": "Sponsor IDs are required for sponsored events."
                }, status=400)

        # if event.is_waitlist_enabled:
        #     # check the waitlist capacity and ensure it isnt  full before allowing team to register (either active or waitlist)
        #     active_count = TournamentTeam.objects.filter(event=event, status="registered").count()
        #     if active_count >= event.max_teams_or_players:
        #         return Response({"message": "Waitlist is full."}, status=403)

        # # Capacity check
        # else:
        #     if RegisteredCompetitors.objects.filter(event=event, status="registered").count() >= event.max_teams_or_players:
        #         return Response({"message": "Registration limit reached."}, status=403)

        # roster rules
        if participant_type == "duo":
            min_size, max_size = 2, 2
        else:
            min_size, max_size = 4, 6

        if not roster_member_ids:
            return Response({"message": "roster_member_ids is required for team events."}, status=400)

        roster_member_ids = list(dict.fromkeys(roster_member_ids))

        if not (min_size <= len(roster_member_ids) <= max_size):
            return Response({"message": f"Roster must contain {min_size} to {max_size} players."}, status=400)

        # Ensure all selected are members of this team
        team_member_ids = set(
            TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
        )
        if not set(roster_member_ids).issubset(team_member_ids):
            return Response({"message": "One or more roster players are not members of this team."}, status=400)

        # ── ESPORT-IMAGE CRITERIA (owner 2026-06-12) ──
        # When required, EVERY roster member must have their esport image uploaded
        # (UserProfile.esports_pic). The error NAMES the missing players so the captain knows
        # exactly who must upload before retrying.
        if event.require_esport_images:
            from afc_auth.models import UserProfile
            with_image = set(
                UserProfile.objects.filter(
                    user_id__in=roster_member_ids, esports_pic__isnull=False
                ).exclude(esports_pic="").values_list("user_id", flat=True)
            )
            missing_ids = [uid for uid in roster_member_ids if uid not in with_image]
            if missing_ids:
                missing_names = list(
                    User.objects.filter(user_id__in=missing_ids).values_list("username", flat=True)
                )
                return Response({
                    "message": (
                        "This event requires every rostered player to have an esport image. "
                        f"Missing: {', '.join(missing_names)}."
                    ),
                    "code": "esport_image_required",
                    "missing_players": missing_names,
                }, status=403)

        # Prevent players being in two rosters for the same event.
        # Fetch the conflicting roster rows WITH the player + the team they are
        # already registered in (select_related = one query, no N+1), so the error
        # can name WHO is the problem and WHERE they're already registered — a
        # captain needs that to know which player to drop. (Previously this only
        # returned a generic message + bare user_ids.)
        conflicting_members = list(
            TournamentTeamMember.objects.filter(
                user_id__in=roster_member_ids,
                tournament_team__event=event,
            ).select_related("user", "tournament_team__team")
        )
        if conflicting_members:
            conflicts = [
                {
                    "user_id": m.user_id,
                    "username": m.user.username,
                    "team_name": m.tournament_team.team.team_name,
                }
                for m in conflicting_members
            ]
            # Human-readable: "PlayerX (already registered in Team Alpha), PlayerY (…)"
            detail = ", ".join(
                f"{c['username']} (already registered in {c['team_name']})"
                for c in conflicts
            )
            return Response({
                "message": f"Cannot register: {detail}.",
                "conflicts": conflicts,
                "user_ids": [c["user_id"] for c in conflicts],  # kept for backwards-compat
            }, status=409)

        roster_users = list(User.objects.filter(user_id__in=roster_member_ids))
        if roster_users is None or len(roster_users) == 0:
            return Response({"message": "Roster users not found."}, status=400)
        roster_users_by_id = {u.user_id: u for u in roster_users}

        missing_ids = [uid for uid in roster_member_ids if uid not in roster_users_by_id]
        if missing_ids:
            return Response({"message": "Some roster users do not exist.", "missing_user_ids": missing_ids}, status=400)

        # ── ban guard: per-roster-member (afc_auth.BannedPlayer) ──
        # No banned player may be entered onto a tournament roster. Now that the roster users are
        # resolved, reject the registration if ANY of them is currently banned (is_active=True AND
        # ban_end_date still in the future, same rule as the team/user check above). The first
        # banned member is named so the captain knows who to drop. One grouped query over the
        # whole roster (no per-member N+1).
        banned_roster_member = (
            BannedPlayer.objects.filter(
                banned_player_id__in=roster_member_ids,
                is_active=True,
                ban_end_date__gt=timezone.now(),
            )
            .select_related("banned_player")
            .first()
        )
        if banned_roster_member:
            return Response(
                {"message": f"A player in your roster is banned: {banned_roster_member.banned_player.username}."},
                status=403,
            )

        # ── organizer blacklist guard (afc_organizers.OrganizerBlacklist) ──
        # An organizer can blacklist a team for a duration; while it is active the team AND the
        # players who were on it at blacklist time cannot register for THAT organizer's events,
        # even after a snapshotted player leaves the team and joins another (the blacklist
        # follows the player). Only relevant for events that have an owning Organization (native
        # AFC events have event.organization_id=None and are never blacklisted). We call the
        # single helper organizer_blacklist_block(org, team, roster_user_ids); it returns a 403
        # message (team-level OR any blacklisted player) or None. Lazy import to avoid an
        # afc_organizers <-> afc_tournament_and_scrims import cycle on this hot path. Spec:
        # WEBSITE/tasks/organizer-blacklist-design.md.
        if event.organization_id:
            from afc_organizers.blacklist import organizer_blacklist_block
            blacklist_message = organizer_blacklist_block(
                event.organization, team, roster_member_ids
            )
            if blacklist_message:
                return Response({"message": blacklist_message}, status=403)

        # check team country and then check the restrictions
        team_country = determine_team_country(roster_users, user)

        if not _passes_event_country_restriction(event, team_country):
            return Response({
                "message": f"Your team is not eligible for this event based on country restriction. Team Country({team_country})",
                "team_country": team_country
            }, status=403)

        # ✅ restriction enforcement for each roster member
        # restricted = []
        # for u in roster_users:
        #     if not _passes_event_country_restriction(event, u.country):
        #         restricted.append({"user_id": u.user_id, "username": u.username, "country": u.country})
        # if restricted:
        #     return Response({
        #         "message": "One or more roster players are not eligible for this event (country restriction).",
        #         "restricted_players": restricted
        #     }, status=403)

        # Discord checks
        # for u in roster_users:
        #     if u.status != "active":
        #         return Response({"message": f"{u.username} is not active."}, status=403)
        #     if not u.discord_connected or not u.discord_id:
        #         return Response({"message": f"{u.username} has not connected Discord."}, status=403)
        #     if not check_discord_membership_v3(str(u.discord_id)):
        #         return Response({"message": f"{u.username} has not joined the Discord server."}, status=403)

        
        # Other verification
        if is_public == False:
            # ── private-event invite gate (TEAM) ──
            # Mirrors the SOLO gate above — keep both in sync.
            invite_token = request.data.get("invite_token")
            if not invite_token:
                return Response({"message": "invite_token is required for private events."}, status=400)

            # Fetch the token row ONCE so shared/expiry checks all read the same record.
            invite = EventInviteToken.objects.filter(event=event, token=invite_token).first()
            if not invite:
                return Response({"message": "Invalid invite token."}, status=403)

            # Enforce expiry (previously ignored): an expired link cannot register anyone.
            if invite.expires_at and timezone.now() > invite.expires_at:
                return Response({"message": "This invite link has expired."}, status=403)

            # Single-use tokens are consumed after one registration; a SHARED token is the
            # reusable FCFS link, so it is accepted regardless of is_used. The event's
            # capacity check below (active_count >= max_teams_or_players) is what closes a
            # shared link once all slots are filled.
            if not invite.is_shared and invite.is_used:
                return Response({"message": "Invite token has already been used."}, status=403)


        # Register
        with transaction.atomic():
            active_count = TournamentTeam.objects.filter(
                event=event,
                is_waitlisted=False
            ).count()

            if active_count >= event.max_teams_or_players:

                if not event.is_waitlist_enabled:
                    return Response({"message": "Registration limit reached."}, status=403)

                waitlist_count = TournamentTeam.objects.filter(
                    event=event,
                    is_waitlisted=True
                ).count()

                if event.waitlist_capacity and waitlist_count >= event.waitlist_capacity:
                    return Response({"message": "Waitlist is full."}, status=403)

                # CREATE WAITLIST TEAM
                tt = TournamentTeam.objects.create(
                    event=event,
                    team=team,
                    registered_by=user,
                    is_waitlisted=True
                )

                TournamentTeamMember.objects.bulk_create([
                    TournamentTeamMember(
                        tournament_team=tt,
                        user=roster_users_by_id[uid],
                        event=event
                    )
                    for uid in roster_member_ids
                ])

                # waitlist discord role
                role_id = event.waitlist_discord_role_id

                if role_id:
                    # Duplicate-safe queue (afc_auth.discord_roles): plain bulk_create
                    # piled up duplicate rows (no unique constraint possible on MySQL),
                    # which later 500'd the start-event reconcile.
                    from afc_auth.discord_roles import queue_discord_role_assignments
                    queue_discord_role_assignments([
                        DiscordRoleAssignment(
                            user=u,
                            discord_id=u.discord_id,
                            role_id=role_id,
                            status="pending"
                        )
                        for u in roster_users
                    ])

                # ---------------- AUTO ASSIGN ROLES FOR NON-SPONSORED ----------------
                if not event.is_sponsored:

                    stage = role_id_stage

                    user_ids = [u.user_id for u in roster_users]

                    assignments_qs = DiscordRoleAssignment.objects.filter(
                        stage=stage,
                        group__isnull=True,
                        status="pending",
                        user_id__in=user_ids
                    )

                    total = assignments_qs.count()

                    if total > 0:
                        progress = DiscordStageRoleAssignmentProgress.objects.create(
                            stage=stage,
                            total=total,
                            completed=0,
                            failed=0,
                            status="running"
                        )

                        assign_stage_roles_for_team_task.delay(
                            progress.id,
                            stage.stage_id,
                            user_ids
                        )

                return Response({
                    "message": "Event is full. Team added to waitlist.",
                    "waitlisted": True,
                    "tournament_team_id": tt.tournament_team_id
                }, status=201)
            
            competitor = RegisteredCompetitors.objects.create(
                event=event,
                team=team,
                status="registered"
            )

            tt = TournamentTeam.objects.create(
                event=event,
                team=team,
                status="pending" if event.is_sponsored else "active",
                registered_by=user,
                country=team_country
            )

            # TournamentTeamMember.objects.bulk_create(
            #     [TournamentTeamMember(tournament_team=tt, user=roster_users_by_id[uid], event=event) for uid in roster_member_ids],
            #     batch_size=200
            # )
            member_rows = []

            for uid in roster_member_ids:

                sponsor_uid = None
                if event.is_sponsored:
                    sponsor_uid = sponsor_ids.get(str(uid))

                member_rows.append(
                    TournamentTeamMember(
                        tournament_team=tt,
                        user=roster_users_by_id[uid],
                        event=event,
                        user_id_from_sponsor=sponsor_uid,
                        status="pending" if event.is_sponsored else "active"
                    )
                )

            TournamentTeamMember.objects.bulk_create(member_rows, batch_size=200)

            # ── SPONSOR ENGAGEMENTS (sponsor redesign P3/P4, TEAM) ──
            # Per-player engagement answers ride the body's `sponsorships` list:
            #   [{sponsorship_id, submissions_by_user: {"<uid>": [{engagement_index, payload}]}}]
            # The captain fills per-rostered-player values (spec section 4). EVERY rostered
            # player must answer every engagement; the helper enforces it (we seed every
            # roster uid so a wholly-omitted player is caught too). On any problem the whole
            # registration rolls back. When approval is required the team parks "pending"
            # exactly like the legacy is_sponsored flow (check_and_activate_team reuses it).
            sponsorship_entries = _maybe_json_list(request.data.get("sponsorships"))
            from afc_sponsors.engagements import create_submissions_for_registration
            team_payloads = {uid: [] for uid in roster_member_ids}
            for sp_entry in (sponsorship_entries or []):
                by_user = sp_entry.get("submissions_by_user") or {}
                for uid_str, subs in by_user.items():
                    try:
                        uid = int(uid_str)
                    except (TypeError, ValueError):
                        transaction.set_rollback(True)
                        return Response({"message": "Bad sponsorships payload."}, status=400)
                    if uid not in roster_users_by_id:
                        transaction.set_rollback(True)
                        return Response({"message": "Sponsor submission for a non-rostered player."}, status=400)
                    for sub in (subs or []):
                        team_payloads[uid].append({
                            "sponsorship_id": sp_entry.get("sponsorship_id"),
                            "engagement_index": sub.get("engagement_index"),
                            "payload": sub.get("payload") or {},
                        })
            sponsor_error, needs_sponsor_approval = create_submissions_for_registration(
                event, team_payloads, user,
            )
            if sponsor_error:
                transaction.set_rollback(True)
                return Response({"message": sponsor_error, "code": "sponsor_submission_invalid"}, status=400)
            if needs_sponsor_approval:
                # Park the whole team for sponsor review: members "pending" + team "pending"
                # + the RC row un-registered, mirroring the legacy is_sponsored baseline so
                # check_and_activate_team's forward direction activates everything once the
                # sponsor approves every member's submissions.
                TournamentTeamMember.objects.filter(tournament_team=tt).update(status="pending")
                if tt.status != "pending":
                    tt.status = "pending"
                    tt.save(update_fields=["status"])
                RegisteredCompetitors.objects.filter(
                    event=event, team=team, status="registered",
                ).update(status="pending")

            # Mark the token used — but ONLY for single-use tokens. A SHARED token
            # (is_shared=True) is the reusable FCFS link and must stay open for the next
            # team; the capacity check above is what eventually closes it.
            if is_public == False and not invite.is_shared:
                EventInviteToken.objects.filter(event=event, token=invite_token).update(is_used=True, used_by=user, used_at=timezone.now())




            # Queue discord roles
            # role_id = getattr(settings, "DISCORD_TOURNAMENT_TEAM_ROLE_ID", None)
            role_id_stage= Stages.objects.filter(event=event).first()
            role_id = role_id_stage.stage_discord_role_id
            if role_id:
                # Duplicate-safe queue (afc_auth.discord_roles): see the waitlist branch
                # above; a re-registered roster must not insert twin assignment rows.
                from afc_auth.discord_roles import queue_discord_role_assignments
                queue_discord_role_assignments([
                    DiscordRoleAssignment(
                        user=u,
                        discord_id=u.discord_id,
                        role_id=role_id,
                        stage=role_id_stage,
                        group=None,
                        status="pending"
                    )
                    for u in roster_users
                ])


        return Response({
            "message": f"Team successfully registered ({participant_type}). Discord roles queued.",
            "registration_id": competitor.id,
            "tournament_team_id": tt.tournament_team_id,
            "roster_size": len(roster_member_ids),
        }, status=201)

    return Response({"message": "Invalid participant type."}, status=400)

def check_and_activate_team(tournament_team):
    # Re-derive the TEAM-level approval state from its members' statuses.
    #
    # WHY (bug "sponsor-edit-roster", 2026-06-10): this used to ONLY move the team
    # FORWARD (pending -> active when every member is "active"). It had no reverse
    # direction, so once a sponsored team was approved and a player was later swapped
    # in/out (or a kept player's sponsor id changed -> that member reset to "pending"),
    # the team stayed stale "active" even though the live roster no longer matched what
    # the sponsor approved. We now make this function BIDIRECTIONAL and call it at the
    # end of edit_roster so the team status always tracks the members:
    #
    #   all members "active"        -> team "active",  RegisteredCompetitors "registered"
    #   any member NOT "active"     -> team "pending",  RegisteredCompetitors un-registered
    #
    # Callers:
    #   - confirm_player (~L5595): after flipping a member to "active". The all-active
    #     branch below is byte-for-byte the old forward behavior (team active + RC
    #     registered + activation email + Discord role celery task), so confirm_player's
    #     observable behavior is unchanged.
    #   - edit_roster (~end of function): after the roster member writes, to reopen the
    #     team for sponsor re-review whenever the edit left any member non-active.
    #
    # Data it reads/writes: TournamentTeamMember.status (the source of truth),
    # TournamentTeam.status, and the (event, team) RegisteredCompetitors row that
    # register_for_event creates with status "registered" for a sponsored team event.

    total = tournament_team.members.count()
    confirmed = tournament_team.members.filter(status="active").count()

    # ---------------- REVERSE DIRECTION: not fully approved -> reopen ----------------
    # If ANY member is not "active" (pending after a swap / a reset sponsor id, or
    # rejected), the team is no longer fully approved. Drop it back to "pending" and
    # un-register its RegisteredCompetitors row so the team reads as "awaiting sponsor
    # review" everywhere, instead of silently keeping a stale "active"/"registered".
    # We mirror register_for_event's sponsored baseline: TournamentTeam is created
    # "pending" (views.py ~L5179) while its RC row is "registered". The cleanest
    # un-registered value here is the RegisteredCompetitors model default "pending"
    # (RegisteredCompetitors.STATUS_CHOICES), which is what register/the model uses for
    # a not-yet-confirmed competitor. (total == 0 should not happen for a real roster,
    # but if it did we treat an empty team as not-active too.)
    if total == 0 or total != confirmed:
        if tournament_team.status != "pending":
            tournament_team.status = "pending"
            tournament_team.save(update_fields=["status"])

        # Only touch rows currently "registered" so we never clobber an explicit
        # disqualified/withdrawn/left state an admin may have set.
        RegisteredCompetitors.objects.filter(
            event=tournament_team.event,
            team=tournament_team.team,
            status="registered",
        ).update(status="pending")
        return

    if total == confirmed:

        # Was the team ALREADY active before this call? If so this is a no-op refresh
        # (e.g. edit_roster re-derived state after an all-active no-op edit) and we must
        # NOT re-send the "fully registered" email or re-queue Discord roles every time
        # the captain saves an unchanged roster. confirm_player always reaches here while
        # the team is still "pending" (the member it just flipped is what completes the
        # set), so for confirm_player was_already_active is False and the email/Discord
        # side effects fire exactly as before. (bug "sponsor-edit-roster", 2026-06-10)
        was_already_active = tournament_team.status == "active"

        # ---------------- ACTIVATE TEAM ----------------
        tournament_team.status = "active"
        tournament_team.save(update_fields=["status"])

        RegisteredCompetitors.objects.filter(
            event=tournament_team.event,
            team=tournament_team.team
        ).update(status="registered")

        # Idempotent no-op refresh: team + RC are already in the approved state, so stop
        # here without re-notifying anyone.
        if was_already_active:
            return


        # ---------------- SEND AN EMAIL TO THE TEAM OWNER NOTIFYING THEM -----------------
        team_name = tournament_team.team.team_name
        event_name = tournament_team.event.event_name
        team_leader_username = tournament_team.team.team_owner.username
        email = tournament_team.team.team_owner.email

        

        subject = f'AFC Registration Update – Your Team {team_name} is now Fully Registered for {event_name}'
        message = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>

<body style="margin:0;padding:0;background-color:#050505;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#050505;padding:40px 0;">
    <tr>
      <td align="center">

        <table width="600" cellpadding="0" cellspacing="0" style="background:#0a0a0a;border:1px solid #1f1f1f;max-width:600px;width:100%;">
          
          <tr>
            <td style="background:#f5a623;height:4px;"></td>
          </tr>

          <tr>
            <td align="center" style="padding:30px;border-bottom:1px solid #1a1a1a;">
              <img src="https://yourdomain.com/static/logo.png" alt="AFC Logo" style="max-height:50px;">
            </td>
          </tr>

          <tr>
            <td style="padding:40px 35px;color:#b0b0b0;font-size:15px;line-height:1.6;">

              <h1 style="color:#ffffff;margin-bottom:20px;">Congratulations 🎉</h1>

              <p>Dear <strong style="color:#ffffff;">{team_leader_username}</strong> (Team {team_name}),</p>

              <p>We are pleased to inform you that all members of your team have been successfully verified and accepted.</p>

              <table width="100%" style="border:1px solid #f5a623;background:#14110a;margin:25px 0;">
                <tr>
                  <td style="padding:20px;text-align:center;color:#ffffff;">
                    Your team <strong style="color:#f5a623;">{team_name}</strong> is now fully registered for 
                    <strong>{event_name}</strong>.
                  </td>
                </tr>
              </table>

              <p>
                All match details (room IDs, passwords, schedules) will be available in your AFC dashboard notifications.
              </p>

              <p>
                Stay prepared and keep checking the platform regularly.
              </p>

              <p>
                Need help? Contact us at 
                <a href="mailto:info@africanfreefirecommunity.com" style="color:#f5a623;">
                  info@africanfreefirecommunity.com
                </a>
              </p>

              <p style="color:#ffffff;font-weight:bold;">
                We look forward to seeing your team compete!
              </p>

              <p>
                Best regards,<br>
                <strong style="color:#ffffff;">AFC Management Board</strong>
              </p>

            </td>
          </tr>

          <tr>
            <td style="background:#080808;padding:25px;border-top:1px solid #1a1a1a;">
              <table width="100%">
                <tr>
                  <td style="color:#888;font-size:12px;">
                    <strong>African Freefire Community</strong><br>
                    <a href="https://www.africanfreefirecommunity.com" style="color:#f5a623;">
                      Visit Website
                    </a>
                  </td>
                  <td align="right">
                    <a href="https://discord.gg/YOUR_LINK"
                       style="border:1px solid #333;color:#fff;padding:10px 15px;text-decoration:none;font-size:11px;">
                      Join Discord
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>

      </td>
    </tr>
  </table>
</body>
</html>
"""
        send_email(email, subject, message)

        # ---------------- PREPARE ROLE ASSIGNMENT ----------------
        stage = Stages.objects.filter(event=tournament_team.event).first()

        if not stage:
            return

        user_ids = list(
            tournament_team.members.values_list("user_id", flat=True)
        )

        assignments_qs = DiscordRoleAssignment.objects.filter(
            stage=stage,
            group__isnull=True,
            status="pending",
            user_id__in=user_ids
        )

        total = assignments_qs.count()

        if total == 0:
            return

        progress = DiscordStageRoleAssignmentProgress.objects.create(
            stage=stage,
            total=total,
            completed=0,
            failed=0,
            status="running"
        )

        # 🔥 TRIGGER CELERY
        assign_stage_roles_for_team_task.delay(
            progress.id,
            stage.stage_id,
            user_ids
        )

from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

@api_view(["POST"])
def confirm_player(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(session_token.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    member_id = request.data.get("member_id")
    member = get_object_or_404(TournamentTeamMember, id=member_id)
    event = member.tournament_team.event

    # ── registration gate ──
    # AFC admins manage registrations for any event; org members need
    # can_manage_registrations on the event's owning org (native AFC events stay admin-only).
    if not _is_event_admin(user) and not org_can_event(user, "can_manage_registrations", event):
        return Response({"message": "You do not have permission to manage registrations for this event."}, status=403)

    # جلوگیری از تکرار
    if member.status == "active":
        return Response({"message": "Player already confirmed."}, status=200)

    member.status = "active"
    member.save(update_fields=["status"])

    # ---------------- DATA ----------------
    player_username = member.user.username
    email = member.user.email
    event_name = member.tournament_team.event.event_name
    team_leader_username = member.tournament_team.team.team_owner.username
    team_name = member.tournament_team.team.team_name
    team_owner_email = member.tournament_team.team.team_owner.email

    # =========================
    # 📧 EMAIL TO PLAYER
    # =========================
    subject = f'AFC Registration Update – Your Application for {event_name} Has Been Accepted'

    player_message = f"""
<!DOCTYPE html>
<html>
<body style="margin: 0; padding: 0; background-color: #050505; font-family: Arial, sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #050505; padding: 40px 0;">
    <tr>
      <td align="center">
        <table width="600" style="background-color: #0a0a0a; border: 1px solid #1f1f1f;">
          
          <tr><td height="4" style="background-color: #15a84e;"></td></tr>

          <tr>
            <td align="center" style="padding: 30px;">
              <img src="https://africanfreefirecommunity.com/static/logo.png"
                   alt="AFC Logo"
                   style="max-height: 50px; display:block;">
            </td>
          </tr>
          
          <tr>
            <td style="padding: 40px; color: #d0d0d0; font-size: 15px;">
              
              <h1 style="color: #ffffff;">Registration Accepted</h1>
              
              <p>Dear <strong style="color:#ffffff;">{player_username}</strong>,</p>
              
              <p>Your registration for <strong>{event_name}</strong> has been 
              <span style="color:#15a84e;"><strong>verified and accepted!</strong></span></p>

              <p>You are now eligible to participate. Match details will be available in your dashboard.</p>

              <p>If you have questions, contact:
                <a href="mailto:info@africanfreefirecommunity.com" style="color:#15a84e;">
                  info@africanfreefirecommunity.com
                </a>
              </p>

              <p style="color:#ffffff;"><strong>Good luck in the tournament!</strong></p>

              <p>
                Best regards,<br>
                <strong style="color:#ffffff;">AFC Management Board</strong>
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    try:
        send_email(email, subject, player_message)
    except Exception as e:
        print(f"Player email failed: {e}")

    # =========================
    # 📧 EMAIL TO TEAM OWNER
    # =========================
    subject = f'AFC Registration Update – Player {player_username} Accepted for {event_name}'

    owner_message = f"""
<!DOCTYPE html>
<html>
<body style="margin: 0; padding: 0; background-color: #050505; font-family: Arial, sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #050505; padding: 40px 0;">
    <tr>
      <td align="center">
        <table width="600" style="background-color: #0a0a0a; border: 1px solid #1f1f1f;">
          
          <tr><td height="4" style="background-color: #ffffff;"></td></tr>

          <tr>
            <td align="center" style="padding: 30px;">
              <img src="https://africanfreefirecommunity.com/static/logo.png"
                   alt="AFC Logo"
                   style="max-height: 50px; display:block;">
            </td>
          </tr>
          
          <tr>
            <td style="padding: 40px; color: #d0d0d0; font-size: 15px;">
              
              <h1 style="color:#ffffff;">Player Status Update</h1>
              
              <p>
                Dear <strong style="color:#ffffff;">{team_leader_username}</strong>
                (Team {team_name}),
              </p>
              
              <p>
                Player <strong style="color:#ffffff;">{player_username}</strong> 
                has been reviewed for <strong>{event_name}</strong>.
              </p>

              <table width="100%" style="border:1px solid #333; background:#0f0f0f; margin:20px 0;">
                <tr>
                  <td style="padding:20px;">
                    <strong style="color:#ffffff;">
                      Status: <span style="color:#15a84e;">Accepted</span>
                    </strong>
                  </td>
                </tr>
              </table>

              <p>You can track all players in your dashboard.</p>

              <p>
                Need help? 
                <a href="mailto:info@africanfreefirecommunity.com" style="color:#ffffff;">
                  Contact support
                </a>
              </p>

              <p style="color:#ffffff;"><strong>Thanks for your participation.</strong></p>

              <p>
                Best regards,<br>
                <strong style="color:#ffffff;">AFC Management Board</strong>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    try:
        send_email(team_owner_email, subject, owner_message)
    except Exception as e:
        print(f"Owner email failed: {e}")

    # ---------------- TEAM ACTIVATION CHECK ----------------
    check_and_activate_team(member.tournament_team)

    return Response({
        "message": "Player confirmed."
    }, status=200)


# @api_view(["POST"])
# def confirm_player(request):

#     member_id = request.data.get("member_id")

#     member = get_object_or_404(TournamentTeamMember, id=member_id)

#     member.status = "active"
#     member.save(update_fields=["status"])

#     #---- SEND CONFIRMATION MAIL TO TEAM OWNER ----
#     player_username = member.user.username
#     email = member.user.email
#     event_name = member.tournament_team.event.event_name
#     team_leader_username = member.tournament_team.team.team_owner.username
#     team_name = member.tournament_team.team.team_name
#     team_owner_email = member.tournament_team.team.team_owner.email


#     subject = f'AFC Registration Update – Player {player_username} has been Accepted for {event_name}'
#     message = f'''Dear {team_leader_username} (Team {team_name}),

# We wanted to inform you that player {player_username} from your team has had their individual registration reviewed for the AFC {event_name}.

# Status: Accepted


# The player has already been notified directly. You can view the current status of all your team members in your AFC dashboard under Team Management.
# We strongly encourage the player to correct any issues and re-submit if needed. All registrations are processed on a first-come, first-served basis.

# If you have any questions, please contact the support team at info@africanfreefirecommunity.com or visit the support channels on our Discord server.
# Thank you for your continued participation in the African Freefire Community.

# Best regards,
# AFC Management Board
# African Freefire Community (AFC)
# Website: www.africanfreefirecommunity.com
# Discord: [Join AFC Discord]
#         '''
#     send_email(team_owner_email, subject, message)

#     #----- SEND CONFIRMATION MAIL TO USER -----
#     subject = f'AFC Registration Update – Your Application for {event_name} Has Been Accepted'
#     message = f'''Dear {player_username} (or {team_name}),

# Thank you for submitting your registration for the AFC {event_name}.
# We are pleased to inform you that your registration has been verified and accepted! 🎉
# You are now officially eligible to participate in the tournament. You will receive all further details (room IDs, passwords, match schedules, etc.) directly in your AFC account Notifications tab. Please keep a close eye on your dashboard.

# We strongly encourage you to prepare and stay updated. All confirmed players are processed on a first-come, first-served basis for future stages.
# If you have any questions, please contact the support team at info@africanfreefirecommunity.com or visit the support channels on our Discord server.

# We appreciate your interest in the African Freefire Community and look forward to seeing you compete soon!

# Best regards,
# AFC Management Board
# African Freefire Community (AFC)
# Website: www.africanfreefirecommunity.com
# Discord: [Join AFC Discord]
#         '''
#     send_email(email, subject, message)


#     #send notification

#     check_and_activate_team(member.tournament_team)

#     return Response({
#         "message": "Player confirmed."
#     }, status=200)

from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

@api_view(["POST"])
def reject_player(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(session_token.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    member_id = request.data.get("member_id")
    reason = request.data.get("reason", "No reason provided")

    member = get_object_or_404(TournamentTeamMember, id=member_id)
    event = member.tournament_team.event

    # ── registration gate ──
    # AFC admins manage registrations for any event; org members need
    # can_manage_registrations on the event's owning org (native AFC events stay admin-only).
    if not _is_event_admin(user) and not org_can_event(user, "can_manage_registrations", event):
        return Response({"message": "You do not have permission to manage registrations for this event."}, status=403)

    # Prevent duplicate rejection
    if member.status == "rejected":
        return Response({"message": "Player already rejected."}, status=200)

    member.status = "rejected"
    member.reason = reason if reason else "No reason provided"
    member.save(update_fields=["status", "reason"])

    # ---------------- DATA ----------------
    player_username = member.user.username
    email = member.user.email
    event_name = member.tournament_team.event.event_name
    team_leader_username = member.tournament_team.team.team_owner.username
    team_name = member.tournament_team.team.team_name
    team_owner_email = member.tournament_team.team.team_owner.email

    # =========================
    # 📧 EMAIL TO PLAYER
    # =========================
    subject = f'AFC Registration Update – Your Application for {event_name} Has Been Rejected'

    player_message = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background-color:#050505;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#050505;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" style="background-color:#0a0a0a;border:1px solid #1f1f1f;">
          
          <tr><td height="4" style="background-color:#dc2626;"></td></tr>

          <tr>
            <td align="center" style="padding:30px;">
              <img src="https://africanfreefirecommunity.com/static/logo.png"
                   alt="AFC Logo"
                   style="max-height:50px;display:block;">
            </td>
          </tr>
          
          <tr>
            <td style="padding:40px;color:#d0d0d0;font-size:15px;">
              
              <h1 style="color:#ffffff;">Registration Update</h1>
              
              <p>Dear <strong style="color:#ffffff;">{player_username}</strong>,</p>
              
              <p>Your application for <strong>{event_name}</strong> has been 
              <span style="color:#dc2626;"><strong>rejected</strong></span>.</p>

              <table width="100%" style="border-left:3px solid #dc2626;background:#1a0b0b;margin:20px 0;">
                <tr>
                  <td style="padding:20px;">
                    <p style="color:#dc2626;font-weight:bold;">Reason:</p>
                    <p style="color:#ffffff;">{reason}</p>
                  </td>
                </tr>
              </table>

              <p>Please correct the issue and re-submit your registration.</p>

              <p>
                <a href="https://www.africanfreefirecommunity.com"
                   style="background:#dc2626;color:#fff;padding:12px 20px;text-decoration:none;">
                   Update Registration
                </a>
              </p>

              <p>
                Need help? 
                <a href="mailto:info@africanfreefirecommunity.com" style="color:#dc2626;">
                  Contact support
                </a>
              </p>

              <p>
                Best regards,<br>
                <strong style="color:#ffffff;">AFC Management Board</strong>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    try:
        send_email(email, subject, player_message)
    except Exception as e:
        print(f"Player rejection email failed: {e}")

    # =========================
    # 📧 EMAIL TO TEAM OWNER
    # =========================
    subject = f'AFC Registration Update – Player {player_username} Rejected for {event_name}'

    owner_message = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background-color:#050505;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#050505;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" style="background-color:#0a0a0a;border:1px solid #1f1f1f;">
          
          <tr><td height="4" style="background-color:#ffffff;"></td></tr>

          <tr>
            <td align="center" style="padding:30px;">
              <img src="https://africanfreefirecommunity.com/static/logo.png"
                   alt="AFC Logo"
                   style="max-height:50px;display:block;">
            </td>
          </tr>
          
          <tr>
            <td style="padding:40px;color:#d0d0d0;font-size:15px;">
              
              <h1 style="color:#ffffff;">Player Status Update</h1>
              
              <p>
                Dear <strong style="color:#ffffff;">{team_leader_username}</strong>
                (Team {team_name}),
              </p>
              
              <p>
                Player <strong style="color:#ffffff;">{player_username}</strong> 
                has been reviewed for <strong>{event_name}</strong>.
              </p>

              <table width="100%" style="border:1px solid #333;background:#0f0f0f;margin:20px 0;">
                <tr>
                  <td style="padding:20px;">
                    <p style="color:#ffffff;font-weight:bold;">
                      Status: <span style="color:#dc2626;">Rejected</span>
                    </p>

                    <div style="margin-top:15px;border-top:1px solid #333;padding-top:15px;">
                      <p style="color:#dc2626;font-weight:bold;">Reason:</p>
                      <p style="color:#ffffff;">{reason}</p>
                    </div>
                  </td>
                </tr>
              </table>

              <p>You can monitor your team in the dashboard.</p>

              <p>
                Need help? 
                <a href="mailto:info@africanfreefirecommunity.com" style="color:#ffffff;">
                  Contact support
                </a>
              </p>

              <p>
                Best regards,<br>
                <strong style="color:#ffffff;">AFC Management Board</strong>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    try:
        send_email(team_owner_email, subject, owner_message)
    except Exception as e:
        print(f"Owner rejection email failed: {e}")

    return Response({
        "message": "Player rejected."
    }, status=200)


# @api_view(["POST"])
# def reject_player(request):
#     member_id = request.data.get("member_id")
#     member = get_object_or_404(TournamentTeamMember, id=member_id)
#     member.status = "rejected"
#     member.reason = request.data.get("reason")
#     member.save(update_fields=["status", "reason"])


#     #---- SEND REJECTION MAIL TO TEAM OWNER ----
#     player_username = member.user.username
#     email = member.user.email
#     event_name = member.tournament_team.event.event_name
#     team_leader_username = member.tournament_team.team.team_owner.username
#     team_name = member.tournament_team.team.team_name
#     reason = member.reason
#     team_owner_email = member.tournament_team.team.team_owner.email


#     subject = f'AFC Registration Update – Player {player_username} has been Rejected for {event_name}'
#     message = f'''Dear {team_leader_username} (Team {team_name}),

# We wanted to inform you that player {player_username} from your team has had their individual registration reviewed for the AFC {event_name}.

# Status: Rejected
# Reason:
# {reason}

# The player has already been notified directly. You can view the current status of all your team members in your AFC dashboard under Team Management.
# We strongly encourage the player to correct any issues and re-submit if needed. All registrations are processed on a first-come, first-served basis.

# If you have any questions, please contact the support team at info@africanfreefirecommunity.com or visit the support channels on our Discord server.
# Thank you for your continued participation in the African Freefire Community.

# Best regards,
# AFC Management Board
# African Freefire Community (AFC)
# Website: www.africanfreefirecommunity.com
# Discord: [Join AFC Discord]
#         '''
#     send_email(team_owner_email, subject, message)

#     #------ SEND REJECTION EMAIL TO USER -----


#     subject = f'AFC Registration Update – Your Application for {event_name} Has Been Rejected'
#     message = f'''Dear {player_username} (or {team_name}),

# Thank you for submitting your registration for the AFC {event_name}.
# Unfortunately, your application has been rejected.

# *Reason for rejection:*
# {reason}

# We strongly encourage you to correct the issue and re-submit your registration as soon as possible. All pending slots are being processed on a first-come, first-served basis.

# If you have any questions or need clarification on the required documents, please contact the support team at info@africanfreefirecommunity.com or visit the support channels on our Discord server.

# We appreciate your interest in the African Freefire Community and look forward to seeing you compete soon.

# Best regards,
# AFC Management Board
# African Freefire Community (AFC)
# Website: www.africanfreefirecommunity.com
# Discord: [Join AFC Discord]
#         '''
#     send_email(email, subject, message)



#     # Notifications.objects.create()

#     return Response({
#         "message": "Player rejected."
#     }, status=200)


@api_view(["POST"])
def get_all_competitors_and_their_sponsor_id(request):
    event_id = request.data.get("event_id")
    event = get_object_or_404(Event, event_id=event_id)
    competitors = TournamentTeamMember.objects.filter(event=event).select_related("user", "tournament_team__team").all()

    data = []
    for c in competitors:
        data.append({
            "competitor_id": c.id,
            "user_id": c.user.user_id,
            "username": c.user.username,
            "team_id": c.tournament_team.team.team_id,
            "team_name": c.tournament_team.team.team_name,
            "sponsor_id": c.user_id_from_sponsor,
            "status": c.status,
        })

    return Response({
        "competitors": data
    }, status=200)
    

# def check_and_activate_team(tournament_team):

#     total = tournament_team.members.count()
#     confirmed = tournament_team.members.filter(status="active").count()

#     if total == confirmed:
#         tournament_team.status = "active"
#         tournament_team.save(update_fields=["status"])

#         RegisteredCompetitors.objects.filter(
#             event=tournament_team.event,
#             team=tournament_team.team
#         ).update(status="registered")

def assign_discord_role_v7(discord_id, role_id):
    url = f"https://discord.com/api/guilds/{settings.DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
    
    headers = {
        "Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}"
    }

    r = requests.put(url, headers=headers)

    if r.status_code not in [204]:
        print("Failed to assign role:", discord_id, role_id, r.text)


@shared_task(bind=True, max_retries=50)
def assign_stage_roles_for_team_task(self, progress_id, stage_id, user_ids, batch_size=10):

    progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
    stage = Stages.objects.get(stage_id=stage_id)

    if progress.status != "running":
        return

    # ---------------- CLAIM BATCH ----------------
    with transaction.atomic():
        qs = (
            DiscordRoleAssignment.objects
            .select_for_update(skip_locked=True)
            .filter(
                stage=stage,
                group__isnull=True,
                status="pending",
                user_id__in=user_ids   # 🔥 IMPORTANT
            )
            .order_by("created_at")[:batch_size]
        )

        assignments = list(qs)

        if not assignments:
            progress.refresh_from_db()
            if progress.completed + progress.failed >= progress.total:
                progress.status = "done"
                progress.save(update_fields=["status"])
            return

        DiscordRoleAssignment.objects.filter(
            id__in=[a.id for a in assignments]
        ).update(status="processing")

    # ---------------- PROCESS ----------------
    processed_ids = []

    try:
        for idx, a in enumerate(assignments):

            r = assign_discord_role(a.discord_id, a.role_id)

            # -------- RATE LIMIT --------
            if r.status_code == 429:
                retry_after = 2.0
                try:
                    retry_after = float(r.json().get("retry_after", 2))
                except Exception:
                    pass

                remaining_ids = [x.id for x in assignments[idx:]]

                DiscordRoleAssignment.objects.filter(
                    id__in=remaining_ids
                ).update(
                    status="pending",
                    error_message="429 rate limited"
                )

                raise self.retry(countdown=retry_after)

            # -------- SUCCESS --------
            if r.status_code in (200, 204):
                DiscordRoleAssignment.objects.filter(id=a.id).update(
                    status="success",
                    error_message=None
                )

                DiscordStageRoleAssignmentProgress.objects.filter(
                    id=progress_id
                ).update(completed=F("completed") + 1)

            # -------- FAILURE --------
            else:
                DiscordRoleAssignment.objects.filter(id=a.id).update(
                    status="failed",
                    error_message=f"{r.status_code} {r.text[:200]}"
                )

                DiscordStageRoleAssignmentProgress.objects.filter(
                    id=progress_id
                ).update(failed=F("failed") + 1)

            processed_ids.append(a.id)

    finally:
        # 🔥 safety reset
        stuck = [a.id for a in assignments if a.id not in processed_ids]
        if stuck:
            DiscordRoleAssignment.objects.filter(
                id__in=stuck,
                status="processing"
            ).update(
                status="pending",
                error_message="reset after crash"
            )

    # ---------------- NEXT BATCH ----------------
    assign_stage_roles_for_team_task.apply_async(
        args=[progress_id, stage_id, user_ids, batch_size],
        countdown=1
    )




# def check_and_activate_team(tournament_team):

#     total = tournament_team.members.count()
#     confirmed = tournament_team.members.filter(status="active").count()

#     if total == confirmed:

#         # ---------------- ACTIVATE TEAM ----------------
#         tournament_team.status = "active"
#         tournament_team.save(update_fields=["status"])

#         RegisteredCompetitors.objects.filter(
#             event=tournament_team.event,
#             team=tournament_team.team
#         ).update(status="registered")

#         # ---------------- ASSIGN DISCORD ROLES ----------------
#         # assignments = DiscordRoleAssignment.objects.filter(
#         #     user__in=tournament_team.members.values_list("user", flat=True),
#         #     stage__event=tournament_team.event,
#         #     status="pending"
#         # )

#         assignments = DiscordRoleAssignment.objects.filter(
#             user__in=tournament_team.members.values_list("user", flat=True),
#             stage__event=tournament_team.event,
#             status="pending"
#         )

#         for a in assignments:
#             a.status = "processing"  # optional but good practice
#             a.save(update_fields=["status"])

#             # 🔥 CALL YOUR DISCORD ROLE FUNCTION HERE
#             assign_discord_role_v7.delay(a.discord_id, a.role_id)

#             a.status = "completed"
#             a.save(update_fields=["status"])


# import json
# from datetime import date
# from django.db import transaction
# from django.shortcuts import get_object_or_404
# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from rest_framework import status

# from django.conf import settings
# from afc_team.models import Team, TeamMembers

# # your helpers
# # validate_token(token) -> returns User or None
# # check_discord_membership(discord_id) -> bool
# # (do NOT call assign_discord_role directly here, we queue instead)


# ALLOWED_REGISTER_ROLES = ["team_owner", "team_captain", "vice_captain"]


# def _maybe_json_list(val):
#     if val is None:
#         return []
#     if isinstance(val, list):
#         return val
#     if isinstance(val, str):
#         try:
#             parsed = json.loads(val)
#             return parsed if isinstance(parsed, list) else []
#         except Exception:
#             return []
#     return []


# def _user_is_team_captain_or_owner(user, team: Team) -> bool:
#     if user.user_id == team.team_owner_id:
#         return True
#     return TeamMembers.objects.filter(
#         team=team,
#         member=user,
#         management_role__in=ALLOWED_REGISTER_ROLES
#     ).exists()


# @api_view(["POST"])
# def register_for_event(request):
#     # -------------------------
#     # AUTH
#     # -------------------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

#     if user.status != "active":
#         return Response({"message": "Your account is not active."}, status=403)

#     # -------------------------
#     # INPUT
#     # -------------------------
#     event_id = request.data.get("event_id")
#     team_id = request.data.get("team_id")  # for duo/squad
#     roster_member_ids = _maybe_json_list(request.data.get("roster_member_ids"))

#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     participant_type = event.participant_type  # solo/duo/squad

#     # -------------------------
#     # REG WINDOW CHECK
#     # -------------------------
#     today = date.today()
#     if not (event.registration_open_date <= today <= event.registration_end_date):
#         return Response({"message": "Registration is closed."}, status=403)

#     # -------------------------
#     # SOLO
#     # -------------------------
#     if participant_type == "solo":
#         # Discord checks
#         if not user.discord_connected or not user.discord_id:
#             return Response({"message": "Connect your Discord account first."}, status=403)

#         if not check_discord_membership(user.discord_id):
#             return Response({"message": "You must join the Discord server before registering."}, status=403)

#         # Prevent duplicate solo registration
#         if RegisteredCompetitors.objects.filter(event=event, user=user).exists():
#             return Response({"message": "You are already registered."}, status=409)

#         # Capacity check
#         if RegisteredCompetitors.objects.filter(event=event, status="registered").count() >= event.max_teams_or_players:
#             return Response({"message": "Registration limit reached."}, status=403)

#         with transaction.atomic():
#             competitor = RegisteredCompetitors.objects.create(
#                 event=event,
#                 user=user,
#                 status="registered"
#             )

#             # Queue discord role (event-level role id)
#             # Put your real event role id in settings or event model.
#             # Example: settings.DISCORD_TOURNAMENT_SOLO_ROLE_ID
#             role_id = getattr(settings, "DISCORD_TOURNAMENT_SOLO_ROLE_ID", None)
#             if role_id:
#                 DiscordRoleAssignment.objects.get_or_create(
#                     user=user,
#                     discord_id=user.discord_id,
#                     role_id=role_id,
#                     stage=None,
#                     group=None,
#                     defaults={"status": "pending"}
#                 )

#         return Response({
#             "message": "Successfully registered (solo). Discord role queued.",
#             "registration_id": competitor.id
#         }, status=201)

#     # -------------------------
#     # TEAM (DUO/SQUAD)
#     # -------------------------
#     if participant_type in ["duo", "squad"]:
#         if not team_id:
#             return Response({"message": "team_id is required for duo/squad."}, status=400)

#         team = get_object_or_404(Team, team_id=team_id)

#         # captain/owner check
#         if not _user_is_team_captain_or_owner(user, team):
#             return Response({"message": "Only captain/vice-captain/team owner can register the team."}, status=403)

#         # Ensure user is in team
#         if not TeamMembers.objects.filter(team=team, member=user).exists():
#             return Response({"message": "You are not a member of this team."}, status=403)

#         # Prevent duplicate team registration (same team already registered)
#         if RegisteredCompetitors.objects.filter(event=event, team=team).exists():
#             return Response({"message": "Team already registered."}, status=409)

#         # Capacity check (by number of registered teams)
#         if RegisteredCompetitors.objects.filter(event=event, status="registered").count() >= event.max_teams_or_players:
#             return Response({"message": "Registration limit reached."}, status=403)

#         # roster rules
#         if participant_type == "duo":
#             min_size, max_size = 2, 2
#         else:
#             min_size, max_size = 4, 6

#         if not roster_member_ids:
#             return Response({"message": "roster_member_ids is required for team events."}, status=400)

#         # remove duplicates while keeping order
#         roster_member_ids = list(dict.fromkeys(roster_member_ids))

#         if not (min_size <= len(roster_member_ids) <= max_size):
#             return Response({"message": f"Roster must contain {min_size} to {max_size} players."}, status=400)

#         # Ensure all selected are members of this team
#         team_member_ids = set(
#             TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
#         )
#         if not set(roster_member_ids).issubset(team_member_ids):
#             return Response({"message": "One or more roster players are not members of this team."}, status=400)

#         # Prevent players being in two rosters for the same event
#         already_in_event_roster_ids = set(
#             TournamentTeamMember.objects.filter(
#                 user_id__in=roster_member_ids,
#                 tournament_team__event=event
#             ).values_list("user_id", flat=True)
#         )
#         if already_in_event_roster_ids:
#             return Response({
#                 "message": "One or more players are already in another roster for this event.",
#                 "user_ids": list(already_in_event_roster_ids)
#             }, status=409)

#         # Fetch users and discord checks
#         roster_users = list(User.objects.filter(user_id__in=roster_member_ids))
#         roster_users_by_id = {u.user_id: u for u in roster_users}

#         # Ensure all ids exist
#         missing_ids = [uid for uid in roster_member_ids if uid not in roster_users_by_id]
#         if missing_ids:
#             return Response({"message": "Some roster users do not exist.", "missing_user_ids": missing_ids}, status=400)

#         for u in roster_users:
#             if u.status != "active":
#                 return Response({"message": f"{u.username} is not active."}, status=403)
#             if not u.discord_connected or not u.discord_id:
#                 return Response({"message": f"{u.username} has not connected Discord."}, status=403)
#             if not check_discord_membership(u.discord_id):
#                 return Response({"message": f"{u.username} has not joined the Discord server."}, status=403)

#         # Register
#         with transaction.atomic():
#             # create event registration row
#             competitor = RegisteredCompetitors.objects.create(
#                 event=event,
#                 team=team,
#                 status="registered"
#             )

#             # create TournamentTeam (event roster container)
#             tt = TournamentTeam.objects.create(
#                 event=event,
#                 team=team,
#                 status="active"
#             )

#             # bulk create roster members
#             TournamentTeamMember.objects.bulk_create(
#                 [TournamentTeamMember(tournament_team=tt, user=roster_users_by_id[uid]) for uid in roster_member_ids],
#                 batch_size=200
#             )

#             # Queue discord roles for all roster members (event-level team role)
#             role_id = getattr(settings, "DISCORD_TOURNAMENT_TEAM_ROLE_ID", None)
#             assignments = []
#             if role_id:
#                 for u in roster_users:
#                     assignments.append(DiscordRoleAssignment(
#                         user=u,
#                         discord_id=u.discord_id,
#                         role_id=role_id,
#                         stage=None,
#                         group=None,
#                         status="pending"
#                     ))
#                 DiscordRoleAssignment.objects.bulk_create(assignments, ignore_conflicts=True, batch_size=500)

#         # OPTIONAL: kick off a worker if you want immediate processing
#         # If you have a dedicated task for event-level roles, use that.
#         # If not, you can create a generic task, or leave it for admin "sync" API.
#         # Example:
#         # assign_event_roles_from_db_task.delay(event.event_id)

#         return Response({
#             "message": f"Team successfully registered ({participant_type}). Discord roles queued.",
#             "registration_id": competitor.id,
#             "tournament_team_id": tt.tournament_team_id,
#             "roster_size": len(roster_member_ids),
#         }, status=201)

#     return Response({"message": "Invalid participant type."}, status=400)


from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

# expects you already have:
# - validate_token(token) -> User or None
# - check_discord_membership(discord_id) -> bool
# - _maybe_json_list(value) -> list (handles string JSON / list)
# - _user_is_team_captain_or_owner(user, team) -> bool
# models: Event, Team, TeamMembers, User

@api_view(["POST"])
def validate_team_roster_discord(request):
    """
    Checks roster Discord readiness BEFORE registration:
    - roster players must be members of team
    - each player: active, discord_connected + discord_id, and is in your Discord server
    Returns a per-player breakdown so frontend can show who is failing.
    """
    # -------- AUTH --------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    if user.status != "active":
        return Response({"message": "Your account is not active."}, status=403)

    # -------- INPUT --------
    event_id = request.data.get("event_id")
    team_id = request.data.get("team_id")
    roster_member_ids = _maybe_json_list(request.data.get("roster_member_ids"))

    if not event_id or not team_id:
        return Response({"message": "event_id and team_id are required."}, status=400)

    if not roster_member_ids:
        return Response({"message": "roster_member_ids is required."}, status=400)

    # remove duplicates while keeping order
    roster_member_ids = list(dict.fromkeys(roster_member_ids))

    event = get_object_or_404(Event, event_id=event_id)
    if event.participant_type not in ["duo", "squad"]:
        return Response({"message": "This validation is for duo/squad events only."}, status=400)

    team = get_object_or_404(Team, team_id=team_id)

    # captain/owner check (optional but recommended, so random members can’t spam checks)
    if not _user_is_team_captain_or_owner(user, team):
        return Response({"message": "Only captain/vice-captain/team owner can validate the roster."}, status=403)

    # ensure requester is in team
    if not TeamMembers.objects.filter(team=team, member=user).exists():
        return Response({"message": "You are not a member of this team."}, status=403)

    # -------- ROSTER SIZE RULES --------
    if event.participant_type == "duo":
        min_size, max_size = 2, 2
    else:
        min_size, max_size = 4, 6

    if not (min_size <= len(roster_member_ids) <= max_size):
        return Response({"message": f"Roster must contain {min_size} to {max_size} players."}, status=400)

    # -------- MEMBERSHIP CHECK --------
    team_member_ids = set(
        TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
    )
    not_in_team = [uid for uid in roster_member_ids if uid not in team_member_ids]
    if not_in_team:
        return Response({
            "message": "One or more roster players are not members of this team.",
            "not_in_team_user_ids": not_in_team
        }, status=400)

    # -------- FETCH USERS --------
    roster_users = list(User.objects.filter(user_id__in=roster_member_ids))
    by_id = {u.user_id: u for u in roster_users}
    missing_ids = [uid for uid in roster_member_ids if uid not in by_id]
    if missing_ids:
        return Response({"message": "Some roster users do not exist.", "missing_user_ids": missing_ids}, status=400)

    # -------- DISCORD CHECKS (DETAILED) --------
    results = []
    all_ok = True

    checked = {}

    for uid in roster_member_ids:
        u = by_id[uid]

        is_active = (u.status == "active")
        has_discord = bool(u.discord_connected and u.discord_id)

        membership_error = None

        in_server = False

        if has_discord:
            try:
                discord_id = str(u.discord_id)

                if discord_id not in checked:
                    checked[discord_id] = check_discord_membership_v3(str(discord_id))

                in_server = checked[discord_id]

            except Exception as e:
                membership_error = str(e)
                in_server = False
        ok = is_active and has_discord and in_server and (membership_error is None)

        if not ok:
            all_ok = False

        results.append({
            "user_id": u.user_id,
            "username": u.username,
            "is_active": is_active,
            "discord_connected": bool(u.discord_connected),
            "discord_id": u.discord_id,
            "in_discord_server": in_server,
            "membership_error": membership_error,
            "ok": ok,
            "reasons": (
                []
                + ([] if is_active else ["inactive_user"])
                + ([] if has_discord else ["discord_not_connected"])
                + ([] if (has_discord and in_server) else (["not_in_discord_server"] if has_discord else []))
                + ([] if membership_error is None else ["discord_check_error"])
            )
        })

    return Response({
        "event_id": event.event_id,
        "team_id": team.team_id,
        "participant_type": event.participant_type,
        "roster_size": len(roster_member_ids),
        "all_ok": all_ok,
        "results": results,
    }, status=200)



# @api_view(["POST"])
# def register_for_event(request):
#     # -------------------------
#     # 1. GET USER FROM SESSION TOKEN
#     # -------------------------
#     session_token = request.headers.get("Authorization")

#     if not session_token:
#         return Response({'status': 'error', 'message': 'Authorization header is required'}, status=400)

#     if not session_token.startswith("Bearer "):
#         return Response({'status': 'error', 'message': 'Invalid token format'}, status=400)

#     session_token = session_token.split(" ")[1]

#     # Identify logged-in user
#     user = validate_token(session_token)
#     if not user:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     # -------------------------
#     # 2. GET EVENT & TEAM
#     # -------------------------
#     event_id = request.data.get("event_id")
#     team_id = request.data.get("team_id")  # only for team events

#     if not event_id:
#         return Response({"message": "event_id is required"}, status=400)

#     # Fetch event
#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found"}, status=404)

#     participant_type = event.participant_type  # solo, duo, squad

#     # -------------------------
#     # 3. CHECK REGISTRATION WINDOW
#     # -------------------------
#     today = date.today()
#     if not (event.registration_open_date <= today <= event.registration_end_date):
#         return Response({"message": "Registration is closed."}, status=403)

#     # ======================================================
#     #                    SOLO REGISTRATION
#     # ======================================================
#     if participant_type == "solo":

#         # Discord must be connected
#         if not user.discord_connected:
#             return Response({"message": "Connect your Discord account first."}, status=403)

#         # Must join the Discord server
#         if not check_discord_membership(user.discord_id):
#             return Response({"message": "You must join the Discord server before registering."}, status=403)

#         # Prevent duplicate registration
#         if RegisteredCompetitors.objects.filter(event=event, user=user).exists():
#             return Response({"message": "You are already registered."}, status=409)

#         # Check event capacity
#         if RegisteredCompetitors.objects.filter(event=event).count() >= event.max_teams_or_players:
#             return Response({"message": "Registration limit reached."}, status=403)

#         # Register user
#         competitor = RegisteredCompetitors.objects.create(event=event, user=user)

#         # Assign Discord role
#         assign_discord_role(user.discord_id, settings.DISCORD_TOURNAMENT_DETTY_SOLOS_ROLE_ID)

#         return Response({
#             "message": "Successfully registered.",
#             "registration_id": competitor.id
#         }, status=201)

#     # ======================================================
#     #                 TEAM (DUO / SQUAD) REGISTRATION
#     # ======================================================
#     if participant_type in ["duo", "squad"]:

#         if not team_id:
#             return Response({"message": "team_id is required."}, status=400)

#         try:
#             team = Team.objects.get(team_id=team_id)
#         except Team.DoesNotExist:
#             return Response({"message": "Team not found"}, status=404)

#         # The user must be part of the team
#         if not TeamMembers.objects.filter(team=team, user=user).exists():
#             return Response({"message": "You are not a member of this team."}, status=403)

#         # Prevent duplicate team registration
#         if RegisteredCompetitors.objects.filter(event=event, team=team).exists():
#             return Response({"message": "Team already registered."}, status=409)

#         # Check event capacity
#         if RegisteredCompetitors.objects.filter(event=event).count() >= event.max_teams_or_players:
#             return Response({"message": "Registration limit reached."}, status=403)

#         # Validate Discord for all team members
#         members = TeamMembers.objects.filter(team=team)

#         for m in members:
#             if not m.user.discord_connected:
#                 return Response({
#                     "message": f"{m.user.username} has not connected Discord.",
#                     "user_id": m.user.id
#                 }, status=403)

#             if not check_discord_membership(m.user.discord_id):
#                 return Response({
#                     "message": f"{m.user.username} has not joined the Discord server.",
#                     "user_id": m.user.id
#                 }, status=403)

#         # Register the team
#         competitor = RegisteredCompetitors.objects.create(event=event, team=team)

#         # Assign roles to all members
#         for m in members:
#             assign_discord_role(m.user.discord_id, settings.DISCORD_TOURNAMENT_ROLE_ID)

#         return Response({
#             "message": "Team successfully registered.",
#             "registration_id": competitor.id
#         }, status=201)

#     return Response({"message": "Invalid event participant type."}, status=400)


from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404



@api_view(["POST"])
def sync_event_registrations_with_discord_roles(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid Authorization"}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized"}, status=403)

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required"}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    if event.participant_type == "solo":
        role_id = getattr(settings, "DISCORD_TOURNAMENT_SOLO_ROLE_ID", None)
        if not role_id:
            return Response({"message": "Solo role id not configured."}, status=400)

        regs = RegisteredCompetitors.objects.select_related("user").filter(event=event, user__isnull=False, status="registered")

        queued = 0
        missing_discord = 0

        for r in regs:
            u = r.user
            if not u or not u.discord_id or not u.discord_connected:
                missing_discord += 1
                continue

            DiscordRoleAssignment.objects.get_or_create(
                user=u,
                discord_id=u.discord_id,
                role_id=role_id,
                stage=None,
                group=None,
                defaults={"status": "pending"}
            )
            queued += 1

        return Response({
            "message": "Queued event roles for registered solo competitors.",
            "queued": queued,
            "missing_discord": missing_discord,
        }, status=200)

    # duo/squad
    role_id = getattr(settings, "DISCORD_TOURNAMENT_TEAM_ROLE_ID", None)
    if not role_id:
        return Response({"message": "Team role id not configured."}, status=400)

    # take from TournamentTeam roster (the actual event roster)
    members = TournamentTeamMember.objects.select_related("user", "tournament_team").filter(
        tournament_team__event=event
    )

    queued = 0
    missing_discord = 0
    for m in members:
        u = m.user
        if not u or not u.discord_id or not u.discord_connected:
            missing_discord += 1
            continue

        DiscordRoleAssignment.objects.get_or_create(
            user=u,
            discord_id=u.discord_id,
            role_id=role_id,
            stage=None,
            group=None,
            defaults={"status": "pending"}
        )
        queued += 1

    return Response({
        "message": "Queued event roles for registered team rosters.",
        "queued": queued,
        "missing_discord": missing_discord,
    }, status=200)



from celery import shared_task
from django.db import transaction

@shared_task(bind=True, max_retries=50)
def assign_event_roles_from_db_task(self, batch_size=10):
    with transaction.atomic():
        qs = (DiscordRoleAssignment.objects
              .select_for_update(skip_locked=True)
              .filter(stage__isnull=True, group__isnull=True, status="pending")
              .order_by("created_at")[:batch_size])

        assignments = list(qs)

        if not assignments:
            return {"message": "No pending event role assignments."}

        DiscordRoleAssignment.objects.filter(id__in=[a.id for a in assignments]).update(status="processing")

    for a in assignments:
        r = assign_discord_role(a.discord_id, a.role_id)

        if r.status_code == 429:
            DiscordRoleAssignment.objects.filter(id=a.id).update(status="pending", error_message="429 rate limited")
            retry_after = 2.0
            try:
                retry_after = float(r.json().get("retry_after", 2))
            except Exception:
                pass
            raise self.retry(countdown=retry_after)

        if r.status_code in (200, 204):
            DiscordRoleAssignment.objects.filter(id=a.id).update(status="success", error_message=None)
        else:
            DiscordRoleAssignment.objects.filter(id=a.id).update(status="failed", error_message=f"{r.status_code} {r.text[:300]}")

    remaining = DiscordRoleAssignment.objects.filter(stage__isnull=True, group__isnull=True, status="pending").count()
    if remaining > 0:
        assign_event_roles_from_db_task.apply_async(args=[], countdown=1)

    return {"processed": len(assignments), "remaining": remaining}



# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # --- auth + basic checks ---
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     # TODO: optionally check admin role here
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)
#     # if not admin.is_staff: return 403 etc.

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # --- Overview metrics ---
#     # total registered competitors (only count active registrations)
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     # expected max competitors
#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0.0

#     # days until start
#     today = timezone.localdate()
#     days_until_start = (event.start_date - today).days if event.start_date else None
#     # event duration in days (inclusive)
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None

#     # registrations close date and days left
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     # average registered competitors per day (since registration opened)
#     try:
#         reg_open = event.registration_open_date
#         if reg_open:
#             days_since_open = max(1, (today - reg_open).days + 1)
#             avg_reg_per_day = round(total_registered / days_since_open, 2)
#         else:
#             avg_reg_per_day = 0.0
#     except Exception:
#         avg_reg_per_day = 0.0

#     # prizepool (string in model) → try numeric
#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # --- registration timeline & statistics ---
#     # registration window days and days left
#     registration_window_days = (event.registration_end_date - event.registration_open_date).days + 1 if event.registration_open_date and event.registration_end_date else None

#     # registration counts per day (for peak registration)
#     reg_by_day = (
#         reg_qs
#         .annotate(reg_date=TruncDate("registration_date"))
#         .values("reg_date")
#         .annotate(count=Count("id"))
#         .order_by("-count")
#     )
#     peak_registration = reg_by_day[0]["count"] if reg_by_day else 0
#     # prepare timeseries (optional) - last 30 days
#     timeseries = []
#     # we can build full timeseries between open and now
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         while current <= end_ts:
#             day_count = next((r["count"] for r in reg_by_day if r["reg_date"] == current), 0)
#             timeseries.append({"date": current, "count": day_count})
#             current = current + timedelta(days=1)

#     # --- team status counts (for squad tournaments using platform Team/TournamentTeam) ---
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # --- stage progress ---
#     total_stages = event.stages.count()
#     # define stage status: completed if end_date < today, ongoing if start_date <= today <= end_date, upcoming if start_date > today
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # --- registration section (restate some metrics) ---
#     registration_rate_pct = registration_percentage
#     avg_registration_per_day = avg_reg_per_day

#     # --- stages detail (groups & team counts) ---
#     stages_data = []
#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0
#         for group in groups:
#             # compute total teams in this group via leaderboards / registered teams in leaderboards or via TournamentTeam entries
#             # We'll assume teams in group are those in leaderboards -> or you can count registered tournament_teams assigned to that stage via leaderboards
#             teams_in_group = 0
#             # try using leaderboards -> teams recorded in matches/stats:
#             # count distinct tournament_team referenced in TournamentTeamMatchStats for matches in this group
#             leaderboards = group.leaderboards.all()

#             matches_qs = Match.objects.filter(
#             leaderboard__group=group
#             )


#             teams_in_group = (
#                 TournamentTeamMatchStats.objects
#                 .filter(match__in=matches_qs)
#                 .values("tournament_team")
#                 .distinct()
#                 .count()
#             )
#             # fallback: if none, use how many teams exist in tournament_teams (approx)
#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details,
#         })

#     # --- engagement metrics ---
#     pageviews = event.pageviews.count()
#     # unique visitors: based on unique user id if present, otherwise unique ip
#     unique_visitors_by_user = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_visitors_by_ip = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_visitors_by_user + unique_visitors_by_ip

#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0.0

#     social_shares = event.social_shares.count()

#     # stream links
#     streams = [ch.channel_url for ch in event.stream_channels.all()]

#     # --- Final payload ---
#     payload = {
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": [
#                 {"date": str(item["date"]), "count": item["count"]} for item in timeseries
#             ],
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "registration_stats": {
#             "registration_rate_pct": registration_rate_pct,
#             "average_registration_per_day": avg_registration_per_day,
#             "peak_registration": peak_registration
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }

#     return Response(payload, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (
#         (event.end_date - event.start_date).days + 1
#         if event.start_date and event.end_date else None
#     )

#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (
#         (registration_close_date - today).days if registration_close_date else None
#     )

#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)
#     else:
#         avg_reg_per_day = 0

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )

#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}

#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []

#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             matches_qs = Match.objects.filter(
#                 leaderboard__group=group
#             )

#             teams_in_group = (
#                 TournamentTeamMatchStats.objects
#                 .filter(match__in=matches_qs)
#                 .values("tournament_team")
#                 .distinct()
#                 .count()
#             )

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()

#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips

#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()

#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     avg_reg_per_day = 0
#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )
#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}

#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []

#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             # Get all leaderboards for this group
#             leaderboards_qs = group.leaderboards.all()

#             # Get all matches under these leaderboards
#             matches_qs = Match.objects.filter(leaderboard__in=leaderboards_qs)

#             # Count distinct teams in this group
#             teams_in_group = (
#                 TournamentTeamMatchStats.objects
#                 .filter(match__in=matches_qs)
#                 .values("tournament_team")
#                 .distinct()
#                 .count()
#             )

#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()
#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips
#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()
#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.prefetch_related(
#             "stages__groups__leaderboards__matches__team_stats"
#         ).get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     avg_reg_per_day = 0
#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )
#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}

#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []

#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             # Get all leaderboards for this group
#             leaderboards = group.leaderboards.all()
#             matches = Match.objects.filter(leaderboard__in=leaderboards)

#             # Count distinct teams in matches
#             teams_in_group = TournamentTeamMatchStats.objects.filter(
#                 match__in=matches
#             ).values("tournament_team").distinct().count()

#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()
#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips
#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()
#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     admin = validate_token(token)
#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()
#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0
#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)
#     else:
#         avg_reg_per_day = 0

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )
#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}
#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

    
#     # Recent Registrations (last 5)
#     recent_registrations = (
#         reg_qs
#         .order_by("-registration_date")[:5]
#         .values("competitor_name", "registration_date", "status")
#     )

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []
#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             teams_in_group = 0
#             # iterate over leaderboards manually
#             for leaderboard in group.leaderboards.all():
#                 for match in leaderboard.matches.all():
#                     teams_in_group += TournamentTeamMatchStats.objects.filter(match=match).values("tournament_team").distinct().count()

#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()
#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips
#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()
#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration,
#             "recent_registrations": list(recent_registrations)
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)

from django.utils import timezone
from django.db.models import Count, Case, When, Value, CharField, F
from django.db.models.functions import TruncDate
from datetime import timedelta
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

from django.db.models import Count, Sum, F, Value, Case, When, CharField
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=status.HTTP_403_FORBIDDEN)

#     # ---------------- EVENT ----------------
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None

#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)
#     else:
#         avg_reg_per_day = 0

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs.annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )

#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}
#         while current <= end_ts:
#             timeseries.append({"date": str(current), "count": reg_map.get(current, 0)})
#             current += timedelta(days=1)

#     recent_registrations = (
#         reg_qs.annotate(
#             competitor_name=Case(
#                 When(user__isnull=False, then=F("user__username")),
#                 When(team__isnull=False, then=F("team__team_name")),
#                 default=Value("Unknown"),
#                 output_field=CharField()
#             )
#         )
#         .order_by("-registration_date")[:5]
#         .values("competitor_name", "registration_date", "status")
#     )

#     all_registrations = (
#         reg_qs.annotate(
#             competitor_name=Case(
#                 When(user__isnull=False, then=F("user__username")),
#                 When(team__isnull=False, then=F("team__team_name")),
#                 default=Value("Unknown"),
#                 output_field=CharField()
#             )
#         )
#         .order_by("-registration_date")
#         .values("competitor_name", "registration_date", "status")
#     )

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []

#     stages = event.stages.all().order_by("start_date", "stage_id")
#     for stage in stages:
#         groups = stage.groups.all().order_by("group_id")
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             # leaderboard (unique_together => at most 1)
#             leaderboard = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

#             group_matches_qs = group.matches.order_by("match_number", "match_id")
#             group_matches = []
#             for match in group_matches_qs:
#                 match_obj = {
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "result_inputted": match.result_inputted,
#                     "match_date": match.match_date,
#                 }

#                 # Attach stats (this is what you were missing)
#                 if event.participant_type == "solo":
#                     stats = (SoloPlayerMatchStats.objects
#                              .filter(match=match)
#                              .select_related("competitor__user")
#                              .values(
#                                  "competitor_id",
#                                  "competitor__user__username",
#                                  "placement",
#                                  "kills",
#                                  "placement_points",
#                                  "kill_points",
#                                  "total_points",
#                              )
#                              .order_by("-total_points", "-kills", "placement"))
#                     match_obj["stats"] = list(stats)
#                     total_teams_in_stage += 0  # solo doesn't count as teams
#                 else:
#                     stats = (TournamentTeamMatchStats.objects
#                              .filter(match=match)
#                              .select_related("tournament_team__team")
#                              .values(
#                                  "tournament_team_id",
#                                  "tournament_team__team__team_name",
#                                  "placement",
#                                  "kills",
#                                  "placement_points",
#                                  "kill_points",
#                                  "total_points",
#                              )
#                              .order_by("-total_points", "-kills", "placement"))
#                     match_obj["stats"] = list(stats)

#                 group_matches.append(match_obj)

#             # Count teams in group (for team events)
#             if event.participant_type != "solo":
#                 teams_in_group = (StageGroupCompetitor.objects
#                                   .filter(stage_group=group, tournament_team__isnull=False, status="active")
#                                   .values("tournament_team_id")
#                                   .distinct()
#                                   .count())
#                 if teams_in_group == 0:
#                     teams_in_group = event.tournament_teams.filter(status="active").count()
#                 total_teams_in_stage += teams_in_group
#             else:
#                 teams_in_group = (StageGroupCompetitor.objects
#                                   .filter(stage_group=group, player__isnull=False, status="active")
#                                   .values("player_id")
#                                   .distinct()
#                                   .count())

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_competitors_in_group": teams_in_group,
#                 "group_discord_role_id": group.group_discord_role_id,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": None if not leaderboard else {
#                     "leaderboard_id": leaderboard.leaderboard_id,
#                     "leaderboard_name": leaderboard.leaderboard_name,
#                     "kill_point": leaderboard.kill_point,
#                     "placement_points": leaderboard.placement_points,
#                     "leaderboard_method": leaderboard.leaderboard_method,
#                     "file_type": leaderboard.file_type,
#                     "last_updated": leaderboard.last_updated,
#                 },
#                 "matches": group_matches,
#                 "competitors_in_group": list(
#                     StageGroupCompetitor.objects.filter(stage_group=group, status="active", player__isnull=False)
#                     .values_list("player__user__username", flat=True)
#                 ) if event.participant_type == "solo" else [],
#                 "teams_in_group": list(
#                     StageGroupCompetitor.objects.filter(stage_group=group, status="active", tournament_team__isnull=False)
#                     .values_list("tournament_team__team__team_name", flat=True)
#                 ) if event.participant_type != "solo" else [],
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage if event.participant_type != "solo" else None,
#             "stage_discord_role_id": stage.stage_discord_role_id,
#             "groups": group_details,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "competitors_in_stage": list(
#                 StageCompetitor.objects.filter(stage=stage, status="active", player__isnull=False)
#                 .values_list("player__user__username", flat=True)
#             ) if event.participant_type == "solo" else [],
#             "teams_in_stage": list(
#                 StageCompetitor.objects.filter(stage=stage, status="active", tournament_team__isnull=False)
#                 .values_list("tournament_team__team__team_name", flat=True)
#             ) if event.participant_type != "solo" else [],
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()
#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips
#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0

#     social_shares = event.social_shares.count()
#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "participant_type": event.participant_type,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#             "prize_distribution": event.prize_distribution,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration,
#             "recent_registrations": list(recent_registrations),
#             "all_registrations": list(all_registrations),
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams,
#         } if event.participant_type != "solo" else None,
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages,
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams,
#         }
#     }, status=200)


from django.db.models import Sum, Count, F, Value, Case, When, CharField
from django.db.models.functions import TruncDate
from datetime import timedelta
from django.utils import timezone

@api_view(["POST"])
def get_event_details_for_admin(request):
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)
    if admin.role != "admin":
        return Response({"message": "You do not have permission to access this data."}, status=status.HTTP_403_FORBIDDEN)

    # event_id = request.data.get("event_id")
    slug = request.data.get("slug")
    if not slug:
        return Response({"message": "slug is required."}, status=400)

    event = get_object_or_404(Event, slug=slug)
    today = timezone.localdate()

    reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
    # reg_qs = RegisteredCompetitors.objects.all(event=event, status="registered")
    total_registered = reg_qs.count()

    max_competitors = event.max_teams_or_players or 0
    registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

    days_until_start = (event.start_date - today).days if event.start_date else None
    event_duration_days = ((event.end_date - event.start_date).days + 1) if event.start_date and event.end_date else None

    registration_close_date = event.registration_end_date
    days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

    if event.registration_open_date:
        days_since_open = max(1, (today - event.registration_open_date).days + 1)
        avg_reg_per_day = round(total_registered / days_since_open, 2)
    else:
        avg_reg_per_day = 0

    try:
        prizepool_val = float(event.prizepool)
    except Exception:
        prizepool_val = event.prizepool

    registration_window_days = (
        (event.registration_end_date - event.registration_open_date).days + 1
        if event.registration_open_date and event.registration_end_date else None
    )

    reg_by_day = (
        reg_qs.annotate(day=TruncDate("registration_date"))
        .values("day")
        .annotate(count=Count("id"))
    )

    peak_registration = max([r["count"] for r in reg_by_day], default=0)

    timeseries = []
    if event.registration_open_date:
        current = event.registration_open_date
        end_ts = min(event.registration_end_date or today, today)
        reg_map = {r["day"]: r["count"] for r in reg_by_day}
        while current <= end_ts:
            timeseries.append({"date": str(current), "count": reg_map.get(current, 0)})
            current += timedelta(days=1)

    recent_registrations = (
        reg_qs.annotate(
            competitor_name=Case(
                When(user__isnull=False, then=F("user__username")),
                When(team__isnull=False, then=F("team__team_name")),
                default=Value("Unknown"),
                output_field=CharField()
            )
        )
        .order_by("-registration_date")[:5]
        .values("competitor_name", "registration_date", "status")
    )

    all_registrations = (
        reg_qs.annotate(
            competitor_name=Case(
                When(user__isnull=False, then=F("user__username")),
                When(team__isnull=False, then=F("team__team_name")),
                default=Value("Unknown"),
                output_field=CharField()
            )
        )
        .order_by("-registration_date")
        .values("competitor_name", "registration_date", "status")
    )

    active_teams = event.tournament_teams.filter(status="active").count()
    disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
    withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

    total_stages = event.stages.count()
    completed_stages = event.stages.filter(end_date__lt=today).count()
    ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
    upcoming_stages = event.stages.filter(start_date__gt=today).count()

    # ---------------- STAGES DETAIL (FIXED + LEADERBOARD + SOLO SUPPORT) ----------------
    stages_data = []
    for stage in event.stages.all().order_by("start_date", "stage_id"):
        groups = stage.groups.all()
        group_details = []

        for group in groups:
            group_matches = list(
                group.matches.order_by("match_number").values(
                    "match_id", "match_number", "match_map",
                    "room_id", "room_name", "room_password",
                    "result_inputted", "match_date",
                )
            )

            leaderboard = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

            # ✅ include competitors list depending on participant type
            if event.participant_type == "solo":
                competitors_in_group = list(
                    StageGroupCompetitor.objects.filter(stage_group=group, player__isnull=False)
                    .values_list("player__user__username", flat=True)
                )
            else:
                competitors_in_group = list(
                    StageGroupCompetitor.objects.filter(stage_group=group, tournament_team__isnull=False)
                    .values_list("tournament_team__team__team_name", flat=True)
                )

            group_details.append({
                "group_id": group.group_id,
                "group_name": group.group_name,
                "playing_date": group.playing_date,
                "playing_time": group.playing_time,
                "teams_qualifying": group.teams_qualifying,
                "group_discord_role_id": group.group_discord_role_id,
                "match_count": group.match_count,
                "match_maps": group.match_maps,
                "matches": group_matches,
                "leaderboard": None if not leaderboard else {
                    "leaderboard_id": leaderboard.leaderboard_id,
                    "leaderboard_name": leaderboard.leaderboard_name,
                    "placement_points": leaderboard.placement_points,
                    "kill_point": leaderboard.kill_point,
                    "leaderboard_method": leaderboard.leaderboard_method,
                    "file_type": leaderboard.file_type,
                    "last_updated": leaderboard.last_updated,
                },
                "competitors_in_group": competitors_in_group,
            })

        stages_data.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "start_date": stage.start_date,
            "end_date": stage.end_date,
            "number_of_groups": stage.number_of_groups,
            "total_groups": groups.count(),
            "stage_discord_role_id": stage.stage_discord_role_id,
            "groups": group_details,
            "stage_format": stage.stage_format,
            "stage_status": stage.stage_status,
            "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
            # ── Scoring-mode config echo so the edit form re-hydrates the toggles. The edit
            # page merges this admin payload's stages over get-event-details, so these must
            # be present here too (mirrors get_event_details). point_rush_target_stage_id is
            # the stage_id of the target; the FE maps it back to a 0-based index.
            "champion_point_enabled": stage.champion_point_enabled,
            "champion_point_threshold": stage.champion_point_threshold,
            "point_rush_enabled": stage.point_rush_enabled,
            "point_rush_reward": stage.point_rush_reward or {},
            "point_rush_target_stage_id": stage.point_rush_target_stage_id,
            # ── BR Round-Robin echo (None for every other format): base groups + the
            # game-day lobbies' source group ids, so the admin editor can rehydrate. ──
            "round_robin": _round_robin_stage_echo(stage),
        })

    pageviews = event.pageviews.count()
    unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
    unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
    unique_visitors = unique_users + unique_ips
    conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
    social_shares = event.social_shares.count()
    streams = list(event.stream_channels.values_list("channel_url", flat=True))

    # Hardened recorder; the admin who loads this admin view is filtered out by the
    # staff check inside, so opening the admin event page never inflates view counts.
    _record_event_view(request, event, admin)

    sponsors = SponsorEvent.objects.filter(event=event).select_related("sponsor")

    return Response({
        "overview": {
            "event_id": event.event_id,
            "event_name": event.event_name,
            "total_registered": total_registered,
            "max_competitors": max_competitors,
            "registration_percentage": registration_percentage,
            "days_until_start": days_until_start,
            "event_duration_days": event_duration_days,
            "registration_close_date": registration_close_date,
            "days_until_registration_close": days_until_registration_close,
            "average_registrations_per_day": avg_reg_per_day,
            "prizepool": prizepool_val,
            "prize_distribution": event.prize_distribution,
            "is_public": event.is_public,
            "is_sponsored": event.is_sponsored,
            "sponsor_name": event.sponsor_name,
            "sponsor_field_label": event.sponsor_field_label,
            "sponsor_requirement_description": event.sponsor_requirement_description,
            "sponsors": [
            {
                "sponsor_id": se.sponsor.user_id,
                "sponsor_name": se.sponsor.full_name,
                "sponsor_username": se.sponsor.username
            }
            for se in sponsors
            ],
            # M: fixed key names (were emitted with stray spaces, unreadable by clients).
            "is_waitlist_enabled": event.is_waitlist_enabled,
        # Media registration criteria (owner 2026-06-12): shown on the event pages + wizard toggles.
        "require_team_logo": event.require_team_logo,
        "require_esport_images": event.require_esport_images,
            "waitlist_capacity": event.waitlist_capacity,
            "waitlist_discord_role_id": event.waitlist_discord_role_id,
            # K: event start/end times so the admin analytics view can show them.
            "event_start_time": event.event_start_time,
            "event_end_time": event.event_end_time,
            },
        "registration_timeline": {
            "registration_start_date": event.registration_open_date,
            "registration_end_date": event.registration_end_date,
            # K: registration window times alongside the dates.
            "registration_start_time": event.registration_start_time,
            "registration_end_time": event.registration_end_time,
            "registration_window_days": registration_window_days,
            "days_left_for_registration": days_until_registration_close,
            "registration_timeseries": timeseries,
            "peak_registration": peak_registration,
            "recent_registrations": list(recent_registrations),
            "all_registrations": list(all_registrations),
        },
        "team_status": {
            "active": active_teams,
            "disqualified": disqualified_teams,
            "withdrawn": withdrawn_teams,
        },
        "stage_progress": {
            "total_stages": total_stages,
            "completed": completed_stages,
            "ongoing": ongoing_stages,
            "upcoming": upcoming_stages,
        },
        "stages": stages_data,
        "engagement": {
            "pageviews": pageviews,
            "unique_visitors": unique_visitors,
            "conversion_rate": conversion_rate,
            "social_shares": social_shares,
            "stream_links": streams,
        }
    }
    , status=200)



# @shared_task(bind=True, rate_limit="1/s")
# def assign_stage_role_task(self, discord_id, role_id):
#     assign_discord_role(discord_id, role_id)

# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_kwargs={"max_retries": 5})
# def assign_stage_role_task(self, progress_id, discord_id, role_id):
#     from afc_auth.models import DiscordStageRoleAssignmentProgress

#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)

#     try:
#         assign_discord_role(discord_id, role_id)
#         progress.completed += 1
#     except Exception:
#         progress.failed += 1
#         raise
#     finally:
#         progress.save()

#     if progress.completed + progress.failed >= progress.total:
#         progress.status = "done"
#         progress.save()


# @shared_task(bind=True, rate_limit="1/s", max_retries=10)          
# def assign_stage_role_task(self, progress_id, discord_id, role_id):
#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)

#     result = assign_discord_role(discord_id, role_id)

#     if result.get("rate_limited"):
#         countdown = int(result.get("retry_after", 2)) + 1
#         raise self.retry(countdown=countdown)

#     if result.get("ok"):
#         progress.completed += 1
#     else:
#         progress.failed += 1

#     # mark done when finished
#     if progress.completed + progress.failed >= progress.total:
#         progress.status = "done"

#     progress.save()



# @shared_task(bind=True, rate_limit="1/s")
# def assign_group_role_task(self, discord_id, role_id):
#     assign_discord_role(discord_id, role_id)

# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_kwargs={"max_retries": 5, "countdown": 10})
# def assign_group_role_task(self, discord_id, role_id):
#     assign_discord_role(discord_id, role_id)


# @shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=10, retry_kwargs={"max_retries": 5})
# def assign_group_role_task(self, assignment_id):
#     assignment = DiscordRoleAssignment.objects.get(id=assignment_id)

#     try:
#         success = assign_discord_role(
#             assignment.discord_id,
#             assignment.role_id
#         )

#         if success:
#             assignment.status = "success"
#         else:
#             assignment.status = "failed"
#             assignment.error_message = "Discord API returned non-204"

#     except Exception as e:
#         assignment.status = "failed"
#         assignment.error_message = str(e)
#         raise  # allows retry

#     finally:
#         assignment.save()

# @shared_task(bind=True, rate_limit="1/s", max_retries=10)
# def assign_group_role_task(self, assignment_id):
#     assignment = DiscordRoleAssignment.objects.select_related("user").get(id=assignment_id)

#     # already done?
#     if assignment.status == "success":
#         return

#     result = assign_discord_role(assignment.discord_id, assignment.role_id)

#     if result.get("rate_limited"):
#         # don’t mark failed; just retry after Discord says so
#         countdown = int(result.get("retry_after", 2)) + 1
#         raise self.retry(countdown=countdown)

#     if result.get("ok"):
#         assignment.status = "success"
#         assignment.error_message = ""
#         assignment.save(update_fields=["status", "error_message"])
#         return

#     assignment.status = "failed"
#     assignment.error_message = f'{result.get("status")} {result.get("text","")}'
#     assignment.save(update_fields=["status", "error_message"])



@shared_task(bind=True, rate_limit="1/s")
def remove_group_role_task(self, discord_id, role_id):  
    remove_discord_role(discord_id, role_id)


@api_view(["POST"])
def discord_role_progress(request):
    stage_id = request.data.get("stage_id")

    qs = DiscordRoleAssignment.objects.filter(stage_id=stage_id)

    return Response({
        "total": qs.count(),
        "pending": qs.filter(status="pending").count(),
        "success": qs.filter(status="success").count(),
        "failed": qs.filter(status="failed").count(),
    })


@api_view(["POST"])
def get_stage_role_assignment_progress(request):
    progress_id = request.data.get("progress_id")
    progress = get_object_or_404(DiscordStageRoleAssignmentProgress, id=progress_id)

    return Response({
        "total": progress.total,
        "completed": progress.completed,
        "failed": progress.failed,
        "status": progress.status,
        "percentage": round((progress.completed / progress.total) * 100, 2) if progress.total else 0
    })


@api_view(["GET"])
def get_all_role_progress(request):
    progresses = DiscordStageRoleAssignmentProgress.objects.all().order_by("-created_at")[:10]

    data = []
    for progress in progresses:
        data.append({
            "id": progress.id,
            "stage_id": progress.stage.stage_id,
            "stage_name": progress.stage.stage_name,
            "total": progress.total,
            "completed": progress.completed,
            "failed": progress.failed,
            "status": progress.status,
            "created_at": progress.created_at,
        })

    return Response(data)


@api_view(["POST"])
def retry_failed_discord_roles(request):
    stage_id = request.data.get("stage_id")

    failed = DiscordRoleAssignment.objects.filter(
        stage_id=stage_id,
        status="failed"
    )

    for assignment in failed:
        assignment.status = "pending"
        assignment.save()
        assign_group_roles_from_db_task.delay(assignment.id)

    return Response({"message": f"Retrying {failed.count()} failed assignments"})


from celery import shared_task
from django.db import transaction
from django.db.models import F

from celery import shared_task
from django.db import transaction
from django.db.models import F

from django.db import transaction
from django.db.models import F
from celery import shared_task

from celery import shared_task
from django.db import transaction
from django.db.models import F

@shared_task(bind=True, max_retries=50)
def assign_stage_roles_from_db_task(self, progress_id, stage_id, batch_size=10):
    progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
    stage = Stages.objects.get(stage_id=stage_id)

    if progress.status != "running":
        return

    # 1) claim a batch fast (lock + mark processing)
    with transaction.atomic():
        qs = (DiscordRoleAssignment.objects
              .select_for_update(skip_locked=True)
              .filter(stage=stage, group__isnull=True, status="pending")
              .order_by("created_at")[:batch_size])

        assignments = list(qs)
        if not assignments:
            progress.refresh_from_db()
            if progress.completed + progress.failed >= progress.total:
                progress.status = "done"
                progress.save(update_fields=["status"])
            return

        DiscordRoleAssignment.objects.filter(id__in=[a.id for a in assignments]).update(status="processing")

    # 2) do network calls OUTSIDE transaction
    processed_ids = []
    try:
        for idx, a in enumerate(assignments):
            r = assign_discord_role(a.discord_id, a.role_id)

            if r.status_code == 429:
                retry_after = 2.0
                try:
                    retry_after = float(r.json().get("retry_after", 2))
                except Exception:
                    pass

                # IMPORTANT: release all not-yet-processed back to pending
                remaining_ids = [x.id for x in assignments[idx:]]
                DiscordRoleAssignment.objects.filter(id__in=remaining_ids).update(
                    status="pending",
                    error_message="429 rate limited"
                )
                raise self.retry(countdown=retry_after)

            if r.status_code in (200, 204):
                DiscordRoleAssignment.objects.filter(id=a.id).update(status="success", error_message=None)
                DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(
                    completed=F("completed") + 1
                )
            else:
                DiscordRoleAssignment.objects.filter(id=a.id).update(
                    status="failed",
                    error_message=f"{r.status_code} {r.text[:300]}"
                )
                DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(
                    failed=F("failed") + 1
                )

            processed_ids.append(a.id)

    finally:
        # safety: if anything crashes mid-loop, don’t leave “processing” stranded
        stuck = [a.id for a in assignments if a.id not in processed_ids]
        if stuck:
            DiscordRoleAssignment.objects.filter(id__in=stuck, status="processing").update(
                status="pending",
                error_message="Reset from processing after crash"
            )

    # 3) schedule next batch
    assign_stage_roles_from_db_task.apply_async(args=[progress_id, stage_id, batch_size], countdown=1)


# @shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 50})
# def assign_stage_roles_from_db_task(self, progress_id, stage_id):
#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
#     stage = Stages.objects.get(stage_id=stage_id)

#     if progress.status != "running":
#         return

#     batch_size = 10

#     # 1) lock + claim a batch fast
#     with transaction.atomic():
#         qs = (DiscordRoleAssignment.objects
#               .select_for_update(skip_locked=True)
#               .filter(stage=stage, group__isnull=True, status="pending")
#               .order_by("created_at")[:batch_size])

#         assignments = list(qs)
#         if not assignments:
#             # no more pending
#             progress.refresh_from_db()
#             if progress.completed + progress.failed >= progress.total:
#                 progress.status = "done"
#                 progress.save(update_fields=["status"])
#             return

#         DiscordRoleAssignment.objects.filter(id__in=[a.id for a in assignments]).update(status="processing")

#     # 2) do network calls OUTSIDE the transaction
#     for assignment in assignments:
#         r = assign_discord_role(assignment.discord_id, assignment.role_id)

#         if r.status_code == 429:
#             retry_after = 2
#             try:
#                 retry_after = float(r.json().get("retry_after", 2))
#             except Exception:
#                 pass

#             # put them back to pending so another run can pick them later
#             DiscordRoleAssignment.objects.filter(id=assignment.id).update(
#                 status="pending",
#                 error_message=f"429 rate limited"
#             )
#             raise self.retry(countdown=retry_after)

#         if r.status_code in (200, 204):
#             DiscordRoleAssignment.objects.filter(id=assignment.id).update(status="success", error_message=None)
#             DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(completed=F("completed") + 1)
#         else:
#             DiscordRoleAssignment.objects.filter(id=assignment.id).update(
#                 status="failed",
#                 error_message=f"{r.status_code} {r.text[:300]}"
#             )
#             DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(failed=F("failed") + 1)

#     # 3) schedule next batch
#     assign_stage_roles_from_db_task.apply_async(args=[progress_id, stage_id], countdown=1)


# @shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 10})
# def assign_stage_roles_from_db_task(self, progress_id, stage_id):
#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
#     stage = Stages.objects.get(stage_id=stage_id)

#     if progress.status != "running":
#         return

#     batch_size = 25  # keep small to be safe with discord rate limits

#     with transaction.atomic():
#         qs = (DiscordRoleAssignment.objects
#               .select_for_update(skip_locked=True)
#               .filter(stage=stage, group__isnull=True, status="pending")
#               .order_by("created_at")[:batch_size])

#         assignments = list(qs)

#         if not assignments:
#             progress.refresh_from_db()
#             if progress.completed + progress.failed >= progress.total:
#                 progress.status = "done"
#                 progress.save(update_fields=["status"])
#             return

#         for assignment in assignments:
#             r = assign_discord_role(assignment.discord_id, assignment.role_id)

#             if r.status_code == 429:
#                 retry_after = 2
#                 try:
#                     retry_after = float(r.json().get("retry_after", 2))
#                 except Exception:
#                     pass
#                 raise self.retry(countdown=retry_after)

#             if r.status_code in (200, 204):
#                 assignment.status = "success"
#                 assignment.error_message = None
#                 assignment.save(update_fields=["status", "error_message", "updated_at"])
#                 DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(
#                     completed=F("completed") + 1
#                 )
#             else:
#                 assignment.status = "failed"
#                 assignment.error_message = f"{r.status_code} {r.text[:300]}"
#                 assignment.save(update_fields=["status", "error_message", "updated_at"])
#                 DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(
#                     failed=F("failed") + 1
#                 )

#     # keep going
#     assign_stage_roles_from_db_task.delay(progress_id, stage_id)


# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 10})
# def assign_stage_roles_from_db_task(self, progress_id, stage_id):
#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
#     stage = Stages.objects.get(stage_id=stage_id)

#     # get a small batch of pending assignments
#     qs = DiscordRoleAssignment.objects.filter(stage=stage, status="pending").order_by("created_at")[:50]
#     if not qs.exists():
#         # maybe done
#         with transaction.atomic():
#             progress.refresh_from_db()
#             if progress.completed + progress.failed >= progress.total:
#                 progress.status = "done"
#                 progress.save(update_fields=["status"])
#         return

#     for assignment in qs:
#         r = assign_discord_role(assignment.discord_id, assignment.role_id)

#         if r.status_code == 429:
#             retry_after = 2
#             try:
#                 retry_after = float(r.json().get("retry_after", 2))
#             except Exception:
#                 pass
#             raise self.retry(countdown=retry_after)

#         if r.status_code in (200, 204):
#             assignment.status = "success"
#             assignment.error_message = None
#             assignment.save(update_fields=["status", "error_message", "updated_at"])
#             DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(completed=F("completed") + 1)
#         else:
#             assignment.status = "failed"
#             assignment.error_message = f"{r.status_code} {r.text[:300]}"
#             assignment.save(update_fields=["status", "error_message", "updated_at"])
#             DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(failed=F("failed") + 1)

#     # re-run until queue empty
#     assign_stage_roles_from_db_task.delay(progress_id, stage_id)



# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 10})
# def assign_group_roles_from_db_task(self, stage_id):
#     stage = Stages.objects.get(stage_id=stage_id)

#     qs = DiscordRoleAssignment.objects.filter(stage=stage, group__isnull=False, status="pending").order_by("created_at")[:50]
#     if not qs.exists():
#         return

#     for assignment in qs:
#         r = assign_discord_role(assignment.discord_id, assignment.role_id)

#         if r.status_code == 429:
#             retry_after = 2
#             try:
#                 retry_after = float(r.json().get("retry_after", 2))
#             except Exception:
#                 pass
#             raise self.retry(countdown=retry_after)

#         if r.status_code in (200, 204):
#             assignment.status = "success"
#             assignment.error_message = None
#         else:
#             assignment.status = "failed"
#             assignment.error_message = f"{r.status_code} {r.text[:300]}"

#         assignment.save(update_fields=["status", "error_message", "updated_at"])

#     assign_group_roles_from_db_task.delay(stage_id)


from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone


@shared_task(bind=True, max_retries=50)
def assign_group_roles_from_db_task(self, stage_id, batch_size=10):
    stage = Stages.objects.get(stage_id=stage_id)

    with transaction.atomic():
        qs = (DiscordRoleAssignment.objects
              .select_for_update(skip_locked=True)
              .filter(stage=stage, group__isnull=False, status="pending")
              .order_by("created_at")[:batch_size])

        assignments = list(qs)
        if not assignments:
            return {"message": "No pending assignments."}

        DiscordRoleAssignment.objects.filter(id__in=[a.id for a in assignments]).update(status="processing")

    processed_ids = []
    try:
        for idx, a in enumerate(assignments):
            r = assign_discord_role(a.discord_id, a.role_id)

            if r.status_code == 429:
                retry_after = 2.0
                try:
                    retry_after = float(r.json().get("retry_after", 2))
                except Exception:
                    pass

                remaining_ids = [x.id for x in assignments[idx:]]
                DiscordRoleAssignment.objects.filter(id__in=remaining_ids).update(
                    status="pending",
                    error_message="429 rate limited"
                )
                raise self.retry(countdown=retry_after)

            if r.status_code in (200, 204):
                DiscordRoleAssignment.objects.filter(id=a.id).update(status="success", error_message=None)
            else:
                DiscordRoleAssignment.objects.filter(id=a.id).update(
                    status="failed",
                    error_message=f"{r.status_code} {r.text[:300]}"
                )

            processed_ids.append(a.id)

    finally:
        stuck = [a.id for a in assignments if a.id not in processed_ids]
        if stuck:
            DiscordRoleAssignment.objects.filter(id__in=stuck, status="processing").update(
                status="pending",
                error_message="Reset from processing after crash"
            )

    remaining = DiscordRoleAssignment.objects.filter(stage=stage, group__isnull=False, status="pending").count()
    if remaining > 0:
        assign_group_roles_from_db_task.apply_async(args=[stage_id, batch_size], countdown=1)

    return {"processed": len(processed_ids), "remaining": remaining}



# @shared_task(bind=True, max_retries=50)
# def assign_group_roles_from_db_task(self, stage_id, batch_size=10):
#     from afc_tournament_and_scrims.models import Stages
#     from afc_auth.models import DiscordRoleAssignment

#     stage = Stages.objects.get(stage_id=stage_id)

#     with transaction.atomic():
#         qs = (
#             DiscordRoleAssignment.objects
#             .select_for_update(skip_locked=True)
#             .filter(stage=stage, group__isnull=False, status="pending")
#             .order_by("created_at")[:batch_size]
#         )
#         assignments = list(qs)

#         # mark these as "processing" to avoid re-picking (optional)
#         # if you want, add "processing" to STATUS_CHOICES.
#         # DiscordRoleAssignment.objects.filter(id__in=[a.id for a in assignments]).update(status="processing")

#     if not assignments:
#         return {"message": "No pending assignments."}

#     for a in assignments:
#         r = assign_discord_role(a.discord_id, a.role_id)

#         # RATE LIMIT
#         if r.status_code == 429:
#             retry_after = 2.0
#             try:
#                 retry_after = float(r.json().get("retry_after", 2))
#             except Exception:
#                 pass

#             # put it back to pending
#             a.status = "pending"
#             a.error_message = f"429 rate limited: {r.text[:200]}"
#             a.save(update_fields=["status", "error_message", "updated_at"])

#             # IMPORTANT: stop processing, retry later
#             raise self.retry(countdown=retry_after)

#         # SUCCESS
#         if r.status_code in (200, 204):
#             a.status = "success"
#             a.error_message = None
#             a.save(update_fields=["status", "error_message", "updated_at"])
#         else:
#             a.status = "failed"
#             a.error_message = f"{r.status_code} {r.text[:300]}"
#             a.save(update_fields=["status", "error_message", "updated_at"])

#     # If there might be more pending, requeue gently
#     remaining = DiscordRoleAssignment.objects.filter(stage=stage, group__isnull=False, status="pending").count()
#     if remaining > 0:
#         assign_group_roles_from_db_task.apply_async(args=[stage_id], countdown=1)

#     return {"processed": len(assignments), "remaining": remaining}



# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")

#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     solo_players = RegisteredCompetitors.objects.filter(
#         event=event,
#         user__isnull=False,
#         team__isnull=True,
#         status="registered"
#     )

#     seeded_count = 0

#     error_disc = []

#     for reg in solo_players:
#         # ✅ Avoid duplicate StageCompetitor
#         obj, created = StageCompetitor.objects.get_or_create(
#             stage=stage,
#             player=reg,
#             defaults={"status": "active"}
#         )

#         if created:
#             seeded_count += 1

#             # ✅ Assign Discord role in background
#             if reg.user.discord_id and stage.stage_discord_role_id:
#                 progress = DiscordStageRoleAssignmentProgress.objects.create(
#                     stage=stage,
#                     total=solo_players.count(),
#                     status="running"
#                 )

#                 try:
#                     assign_stage_role_task.delay(
#                         progress.id,
#                         reg.user.discord_id,
#                         stage.stage_discord_role_id
#                     )
#                 except Exception as e:
#                     # Log error, but don't fail the whole seeding
#                     error_disc.append(f"Failed to queue Discord role for {reg.user.username}: {e}")
#                     print(f"Failed to queue Discord role for {reg.user.username}: {e}")

#     stage.stage_status = "ongoing"
#     stage.save()

#     return Response({
#         "message": f"Seeded {seeded_count} solo players into stage '{stage.stage_name}'.",
#         "errors": error_disc
#     }, status=200)


# import math
# from django.db import transaction
# from django.shortcuts import get_object_or_404
# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from rest_framework import status

# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     # all registered solo players
#     solo_regs = list(
#         RegisteredCompetitors.objects.select_related("user").filter(
#             event=event,
#             status="registered",
#             user__isnull=False,
#             team__isnull=True
#         )
#     )

#     if not solo_regs:
#         return Response({"message": "No solo registrations found."}, status=400)

#     # Existing StageCompetitors -> avoid duplicates without per-row get_or_create
#     existing_reg_ids = set(
#         StageCompetitor.objects.filter(stage=stage, player__in=solo_regs)
#         .values_list("player_id", flat=True)
#     )

#     to_create = [
#         StageCompetitor(stage=stage, player=reg, status="active")
#         for reg in solo_regs
#         if reg.id not in existing_reg_ids
#     ]

#     # Create ONE progress row (NOT inside the loop)
#     progress = DiscordStageRoleAssignmentProgress.objects.create(
#         stage=stage,
#         total=len(solo_regs),
#         completed=0,
#         failed=0,
#         status="running"
#     )

#     # Create StageCompetitors in bulk
#     with transaction.atomic():
#         if to_create:
#             StageCompetitor.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=1000)

#     # Queue role assignments (Celery handles rate-limit safely)
#     role_id = stage.stage_discord_role_id
#     if role_id:
#         for reg in solo_regs:
#             if reg.user and reg.user.discord_id:
#                 assign_stage_role_task.delay(progress.id, reg.user.discord_id, role_id)
#             else:
#                 # still count it so progress finishes
#                 progress.failed += 1
#         progress.save()

#     stage.stage_status = "ongoing"
#     stage.save(update_fields=["stage_status"])

#     return Response({
#         "message": f"Seed complete for stage '{stage.stage_name}'. Roles queued.",
#         "stage_id": stage.stage_id,
#         "total_registrations": len(solo_regs),
#         "created_stage_competitors": len(to_create),
#         "progress_id": str(progress.id),
#     }, status=200)


from django.db import transaction
from django.db.models import F
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


from django.db import transaction
from django.db.models import F


from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

@api_view(["POST"])
def seed_solo_players_to_stage(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized"}, status=403)

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    if not event_id or not stage_id:
        return Response({"message": "event_id and stage_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

    if event.participant_type != "solo":
        return Response({"message": "This event is not a solo event."}, status=400)

    solo_players = RegisteredCompetitors.objects.select_related("user").filter(
        event=event,
        user__isnull=False,
        team__isnull=True,
        status="registered"
    )

    total = solo_players.count()
    if total == 0:
        return Response({"message": "No registered solo players found."}, status=400)

    assignments = []
    created_stagecompetitors = 0

    with transaction.atomic():
        for reg in solo_players:
            _, created = StageCompetitor.objects.get_or_create(
                stage=stage,
                player=reg,
                tournament_team=None,
                defaults={"status": "active"}
            )
            if created:
                created_stagecompetitors += 1

            if reg.user and reg.user.discord_id and stage.stage_discord_role_id:
                assignments.append(DiscordRoleAssignment(
                    user=reg.user,
                    discord_id=reg.user.discord_id,
                    role_id=stage.stage_discord_role_id,
                    stage=stage,
                    group=None,
                    status="pending",
                ))

        # Duplicate-safe queue (afc_auth.discord_roles): only truly-new tuples insert,
        # so a re-seed never doubles rows. queued = what actually landed, keeping the
        # progress total honest.
        queued = 0
        if assignments:
            from afc_auth.discord_roles import queue_discord_role_assignments
            queued = queue_discord_role_assignments(assignments, batch_size=1000)

    if queued > 0:
        progress = DiscordStageRoleAssignmentProgress.objects.create(
            stage=stage,
            total=queued,
            status="running"
        )
        assign_stage_roles_from_db_task.delay(str(progress.id), stage.stage_id)
        progress_id = str(progress.id)
    else:
        progress_id = None

    stage.stage_status = "ongoing"
    stage.save(update_fields=["stage_status"])

    return Response({
        "message": "Stage competitors seeded. Discord assignments queued (if applicable).",
        "total_registered": total,
        "stagecompetitors_created": created_stagecompetitors,
        "assignments_queued": queued,
        "progress_id": progress_id,
    }, status=200)


# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized"}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     solo_players = RegisteredCompetitors.objects.select_related("user").filter(
#         event=event,
#         user__isnull=False,
#         team__isnull=True,
#         status="registered"
#     )

#     total = solo_players.count()
#     if total == 0:
#         return Response({"message": "No registered solo players found."}, status=400)

#     # ✅ create ONE progress row
#     progress = DiscordStageRoleAssignmentProgress.objects.create(
#         stage=stage,
#         total=total,
#         status="running"
#     )

#     assignments = []
#     created_stagecompetitors = 0

#     with transaction.atomic():
#         for reg in solo_players:
#             _, created = StageCompetitor.objects.get_or_create(
#                 stage=stage,
#                 player=reg,
#                 defaults={"status": "active"}
#             )
#             if created:
#                 created_stagecompetitors += 1

#             # queue role assignment only if we have discord + role id
#             if reg.user and reg.user.discord_id and stage.stage_discord_role_id:
#                 assignments.append(DiscordRoleAssignment(
#                     user=reg.user,
#                     discord_id=reg.user.discord_id,
#                     role_id=stage.stage_discord_role_id,
#                     stage=stage,
#                     group=None,
#                     status="pending"
#                 ))

#         # bulk create assignments
#         DiscordRoleAssignment.objects.bulk_create(assignments, ignore_conflicts=True, batch_size=1000)

#     # ✅ start the batch worker loop ONCE
#     assign_stage_roles_from_db_task.delay(str(progress.id), stage.stage_id)

#     stage.stage_status = "ongoing"
#     stage.save(update_fields=["stage_status"])

#     return Response({
#         "message": "Stage competitors seeded and Discord assignments queued.",
#         "total_registered": total,
#         "stagecompetitors_created": created_stagecompetitors,
#         "assignments_queued": len(assignments),
#         "progress_id": str(progress.id),
#     }, status=200)


# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     solo_players_qs = RegisteredCompetitors.objects.select_related("user").filter(
#         event=event,
#         user__isnull=False,
#         team__isnull=True,
#         status="registered"
#     )

#     total = solo_players_qs.count()
#     if total == 0:
#         return Response({"message": "No registered solo players found."}, status=400)

#     # ✅ Create ONE progress row
#     progress = DiscordStageRoleAssignmentProgress.objects.create(
#         stage=stage,
#         total=total,
#         status="running"
#     )

#     seeded_count = 0
#     role_assignments = []

#     # ✅ Preload existing StageCompetitor to avoid duplicates
#     existing_ids = set(
#         StageCompetitor.objects.filter(stage=stage, player__in=solo_players_qs)
#         .values_list("player_id", flat=True)
#     )

#     # ✅ Seed + queue role assignments
#     for reg in solo_players_qs:
#         if reg.id not in existing_ids:
#             StageCompetitor.objects.create(stage=stage, player=reg, status="active")
#             seeded_count += 1

#         if reg.user and reg.user.discord_id and stage.stage_discord_role_id:
#             role_assignments.append(
#                 DiscordRoleAssignment(
#                     user=reg.user,
#                     discord_id=reg.user.discord_id,
#                     role_id=stage.stage_discord_role_id,
#                     stage=stage,
#                     status="pending",
#                 )
#             )

#     if role_assignments:
#         DiscordRoleAssignment.objects.bulk_create(role_assignments, batch_size=500)

#         # enqueue processing (chunking helps huge loads)
#         assign_stage_roles_from_db_task.delay(str(progress.id), stage.stage_id)

#     stage.stage_status = "ongoing"
#     stage.save()

#     return Response({
#         "message": f"Seeded {seeded_count} solo players into stage '{stage.stage_name}'.",
#         "total_registered": total,
#         "progress_id": str(progress.id),
#         "queued_role_assignments": len(role_assignments),
#     }, status=200)



# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     if admin.role != "admin":
#         return Response(
#             {"message": "You do not have permission to perform this action."},
#             status=status.HTTP_403_FORBIDDEN
#         )


#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")

#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     # ✅ ENSURE SOLO EVENT
#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     solo_players = RegisteredCompetitors.objects.filter(
#         event=event,
#         user__isnull=False,
#         team__isnull=True,
#         status="registered"
#     )

#     seeded_count = 0

#     for reg in solo_players:

#         if reg.user.discord_id and stage.stage_discord_role_id:
#             try:
#                 assign_stage_role_task.delay(
#                     reg.user.discord_id,
#                     stage.stage_discord_role_id
#                 )
#             except Exception as e:
#                 return Response({"message": f"Failed to assign Discord role: {str(e)}"}, status=500)

#             seeded_count += 1


#     return Response({
#         "message": f"Seeded {seeded_count} solo players into stage '{stage.stage_name}'."
#     }, status=200)



from random import shuffle
from afc_tournament_and_scrims.models import StageGroups, StageCompetitor, StageGroupCompetitor

# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     if admin.role != "admin":
#         return Response(
#             {"message": "You do not have permission to perform this action."},
#             status=status.HTTP_403_FORBIDDEN
#         )
#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)

#     groups = list(stage.groups.all())
#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     competitors = list(
#         stage.competitors.filter(status="active", player__isnull=False)
#     )

#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     shuffle(competitors)

#     group_count = len(groups)
#     seeded_count = 0

#     for idx, competitor in enumerate(competitors):
#         group = groups[idx % group_count]

#         _, created = StageGroupCompetitor.objects.get_or_create(
#             stage_group=group,
#             player=competitor.player,
#             defaults={"status": "active"}
#         )

#         if created:
#             seeded_count += 1

#             # ✅ SAFE DISCORD ASSIGN
#             user = competitor.player.user
#             if user.discord_id and group.group_discord_role_id:
#                 assign_group_role_task.delay(user.discord_id, group.group_discord_role_id)

#     return Response({
#         "message": f"Seeded {seeded_count} competitors into {group_count} groups for stage '{stage.stage_name}'."
#     }, status=200)


# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)
#     groups = list(stage.groups.all())
#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     # Get all active StageCompetitors (players only)
#     competitors = list(stage.competitors.filter(status="active", player__isnull=False))
#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     shuffle(competitors)  # randomize

#     group_count = len(groups)
#     seeded_count = 0

#     for idx, competitor in enumerate(competitors):
#         group = groups[idx % group_count]

#         # Create or skip if already in group
#         obj, created = StageGroupCompetitor.objects.get_or_create(
#             stage_group=group,
#             player=competitor.player,
#             defaults={"status": "active"}
#         )

#         if created:
#             seeded_count += 1

#             # Assign discord role safely
#             if competitor.player.user and competitor.player.user.discord_id and group.group_discord_role_id:
#                 assign_group_role_task.delay(competitor.player.user.discord_id, group.group_discord_role_id)

#     return Response({
#         "message": f"Seeded {seeded_count} competitors into {group_count} groups for stage '{stage.stage_name}'."
#     }, status=200)

# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)
#     groups = list(stage.groups.all())

#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     competitors = list(
#         stage.competitors.filter(status="active", player__isnull=False)
#     )

#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     shuffle(competitors)

#     group_count = len(groups)
#     seeded_count = 0

#     for idx, competitor in enumerate(competitors):
#         player = competitor.player

#         # 🚫 Prevent player being in more than one group
#         if StageGroupCompetitor.objects.filter(
#             stage_group__stage=stage,
#             player=player
#         ).exists():
#             continue

#         group = groups[idx % group_count]

#         StageGroupCompetitor.objects.create(
#             stage_group=group,
#             player=player,
#             status="active"
#         )

#         seeded_count += 1

#         # ✅ Discord role (async)
#         user = player.user
#         if user and user.discord_id and group.group_discord_role_id:
#             assignment = DiscordRoleAssignment.objects.create(
#             user=competitor.player.user,
#             discord_id=competitor.player.user.discord_id,
#             role_id=group.group_discord_role_id,
#             stage=stage,
#             group=group,
#             status="pending"
#         )

#         assign_group_role_task.delay(assignment.id)

#             # assign_group_role_task.delay(
#             #     user.discord_id,
#             #     group.group_discord_role_id
#             # )

#     return Response({
#         "message": f"Seeded {seeded_count} competitors into {group_count} groups for stage '{stage.stage_name}'."
#     }, status=200)


# from random import shuffle
# from django.db import transaction

# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)
#     groups = list(stage.groups.all().order_by("group_id"))
#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     # stage competitors (solo)
#     competitors = list(
#         StageCompetitor.objects.select_related("player__user").filter(
#             stage=stage, status="active", player__isnull=False
#         )
#     )
#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     # players already seeded into ANY group in THIS stage
#     already_seeded_ids = set(
#         StageGroupCompetitor.objects.filter(stage_group__stage=stage, player__isnull=False)
#         .values_list("player_id", flat=True)
#     )

#     unseeded = [c for c in competitors if c.player_id not in already_seeded_ids]
#     if not unseeded:
#         return Response({"message": "All competitors are already seeded into groups for this stage."}, status=200)

#     shuffle(unseeded)

#     group_count = len(groups)
#     group_role_jobs = []   # (discord_id, role_id, stage, group)
#     to_create = []

#     for idx, comp in enumerate(unseeded):
#         group = groups[idx % group_count]
#         to_create.append(StageGroupCompetitor(
#             stage_group=group,
#             player=comp.player,
#             status="active"
#         ))

#         user = comp.player.user
#         if user and user.discord_id and group.group_discord_role_id:
#             group_role_jobs.append((user, user.discord_id, group.group_discord_role_id, stage, group))

#     with transaction.atomic():
#         StageGroupCompetitor.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=1000)

#         # create assignment rows in bulk too (fast)
#         assignments = [
#             DiscordRoleAssignment(
#                 user=u,
#                 discord_id=discord_id,
#                 role_id=role_id,
#                 stage=stage,
#                 group=group,
#                 status="pending"
#             )
#             for (u, discord_id, role_id, stage, group) in group_role_jobs
#         ]
#         DiscordRoleAssignment.objects.bulk_create(assignments, batch_size=1000)

#     # queue celery jobs
#     for a in DiscordRoleAssignment.objects.filter(stage=stage, group__isnull=False, status="pending"):
#         assign_group_role_task.delay(a.id)

#     return Response({
#         "message": f"Seeded {len(unseeded)} competitors into {group_count} groups for stage '{stage.stage_name}'.",
#         "seeded": len(unseeded),
#         "groups": group_count,
#         "discord_assignments_queued": len(group_role_jobs),
#     }, status=200)


from random import shuffle

from random import shuffle
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

@api_view(["POST"])
def seed_stage_competitors_to_groups(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)
    groups = list(stage.groups.all().order_by("group_id"))
    if not groups:
        return Response({"message": "No groups found for this stage."}, status=400)

    competitors = list(
        stage.competitors.select_related("player__user")
        .filter(status="active", player__isnull=False)
    )
    if not competitors:
        return Response({"message": "No competitors found to seed."}, status=400)

    # already in ANY group for this stage
    already_seeded_player_ids = set(
        StageGroupCompetitor.objects.filter(stage_group__stage=stage, player__isnull=False)
        .values_list("player_id", flat=True)
    )

    # only seed the unseeded ones
    to_seed = [c for c in competitors if c.player_id not in already_seeded_player_ids]
    if not to_seed:
        return Response({
            "message": f"All competitors are already seeded for stage '{stage.stage_name}'.",
            "seeded": 0,
            "queued_role_assignments": 0,
        }, status=200)

    shuffle(to_seed)

    seeded_idx = 0
    sgc_rows = []
    role_rows = []

    for comp in to_seed:
        player = comp.player
        group = groups[seeded_idx % len(groups)]
        seeded_idx += 1

        sgc_rows.append(StageGroupCompetitor(
            stage_group=group,
            player=player,
            status="active"
        ))

        user = player.user
        if user and user.discord_id and group.group_discord_role_id:
            role_rows.append(DiscordRoleAssignment(
                user=user,
                discord_id=user.discord_id,
                role_id=group.group_discord_role_id,
                stage=stage,
                group=group,
                status="pending",
            ))

    with transaction.atomic():
        StageGroupCompetitor.objects.bulk_create(sgc_rows, batch_size=500, ignore_conflicts=True)
        # Duplicate-safe queue (afc_auth.discord_roles): a re-seed must not insert twin
        # assignment rows (no unique constraint possible on MySQL across nullable cols).
        queued_roles = 0
        if role_rows:
            from afc_auth.discord_roles import queue_discord_role_assignments
            queued_roles = queue_discord_role_assignments(role_rows)

    if role_rows:
        assign_group_roles_from_db_task.delay(stage.stage_id)

    return Response({
        "message": f"Seeded {len(sgc_rows)} competitors into {len(groups)} groups for stage '{stage.stage_name}'.",
        "seeded": len(sgc_rows),
        "queued_role_assignments": queued_roles,
    }, status=200)


# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)
#     groups = list(stage.groups.all())
#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     competitors = list(stage.competitors.select_related("player__user").filter(status="active", player__isnull=False))
#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     shuffle(competitors)

#     # ✅ already seeded in ANY group in this stage
#     already_seeded_player_ids = set(
#         StageGroupCompetitor.objects.filter(stage_group__stage=stage, player__isnull=False)
#         .values_list("player_id", flat=True)
#     )

#     seeded_idx = 0
#     to_create = []
#     role_assignments = []

#     for competitor in competitors:
#         player = competitor.player
#         if player.id in already_seeded_player_ids:
#             continue

#         group = groups[seeded_idx % len(groups)]
#         seeded_idx += 1
#         already_seeded_player_ids.add(player.id)

#         to_create.append(StageGroupCompetitor(stage_group=group, player=player, status="active"))

#         user = player.user
#         if user and user.discord_id and group.group_discord_role_id:
#             role_assignments.append(
#                 DiscordRoleAssignment(
#                     user=user,
#                     discord_id=user.discord_id,
#                     role_id=group.group_discord_role_id,
#                     stage=stage,
#                     group=group,
#                     status="pending",
#                 )
#             )

#     StageGroupCompetitor.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
#     if role_assignments:
#         DiscordRoleAssignment.objects.bulk_create(role_assignments, batch_size=500)

#         assign_group_roles_from_db_task.delay(stage.stage_id)  # process pending for this stage

#     return Response({
#         "message": f"Seeded {len(to_create)} competitors into {len(groups)} groups for stage '{stage.stage_name}'.",
#         "queued_role_assignments": len(role_assignments),
#     }, status=200)



@api_view(["POST"])
def sync_group_discord_roles(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid Authorization"}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized"}, status=403)

    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required"}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)
    role_id = group.group_discord_role_id
    if not role_id:
        return Response({"message": "This group has no discord role id."}, status=400)

    competitors = StageGroupCompetitor.objects.select_related("player__user").filter(
        stage_group=group, player__isnull=False, status="active"
    )

    checked = 0
    queued = 0
    missing_discord = 0
    discord_errors = 0

    for sc in competitors:
        user = sc.player.user
        if not user or not user.discord_id:
            missing_discord += 1
            continue

        has_role, resp = discord_member_has_role(user.discord_id, role_id)
        checked += 1

        if has_role is None:
            discord_errors += 1
            continue

        if not has_role:
            DiscordRoleAssignment.objects.get_or_create(
                user=user,
                discord_id=user.discord_id,
                role_id=role_id,
                stage=group.stage,
                group=group,
                defaults={"status": "pending"}
            )
            queued += 1

    if queued:
        assign_group_roles_from_db_task.delay(group.stage.stage_id)

    return Response({
        "message": "Sync complete. Missing roles were queued.",
        "checked": checked,
        "queued": queued,
        "missing_discord_id": missing_discord,
        "discord_errors": discord_errors,
    }, status=200)




@api_view(["POST"])
def disqualify_registered_competitor(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    competitor_id = request.data.get("competitor_id")
    event_id = request.data.get("event_id")

    if not competitor_id or not event_id:
        return Response({"message": "competitor_id, event_id, and stage_id are required."}, status=400)
    
    user = get_object_or_404(User, user_id=competitor_id)

    event = get_object_or_404(Event, event_id=event_id)
    competitor = get_object_or_404(RegisteredCompetitors, user=user, event=event)

    
    # remove all stage and group roles related to this event
    stages = Stages.objects.filter(event=event)
    for stage in stages:
        if user.discord_id and stage.stage_discord_role_id:
            remove_discord_role(user.discord_id, stage.stage_discord_role_id)
        groups = StageGroups.objects.filter(stage=stage)
        for group in groups:
            if user.discord_id and group.group_discord_role_id:
                remove_discord_role(user.discord_id, group.group_discord_role_id)
    
    competitor.status = "disqualified"
    competitor.save()

    # Notify user of disqualification
    message = f"You have been disqualified from the event '{competitor.event.event_name}'. Please contact the event organizers for more information."
    Notifications.objects.create(user=user, message=message)

    return Response({
        "message": f"Competitor '{user.username}' has been disqualified from event '{competitor.event.event_name}'."
    }, status=200)


@api_view(["POST"])
def reactivate_registered_competitor(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    # stage_id = request.data.get("stage_id")
    competitor_id = request.data.get("competitor_id")
    event_id = request.data.get("event_id")

    if not competitor_id or not event_id:
        return Response({"message": "competitor_id and event_id are required."}, status=400)

    # stage = get_object_or_404(Stages, stage_id=stage_id)

    user = get_object_or_404(User, user_id=competitor_id)
    event = get_object_or_404(Event, event_id=event_id)

    competitor = get_object_or_404(RegisteredCompetitors, user=user, event=event)

    competitor.status = "registered"
    competitor.save()

    return Response({
        "message": f"Competitor '{competitor.player.competitor_name}' has been reactivated for event '{competitor.event.event_name}'."
    }, status=200)


# @api_view(["POST"])
# def send_match_room_details_notification_to_competitor(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
    
#     token = session_token.split(" ")[1]
#     admin = validate_token(token)
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
    
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     # ---------------- EVENT ----------------
#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     # ---------------- GET MATCHES ----------------
#     matches = Match.objects.filter(group=group_id)
#     if not matches.exists():
#         return Response({"message": "No matches found for this event."}, status=400)

#     total_notifications = 0

#     for match in matches:
#         # Ensure match has room details
#         if not match.room_id or not match.room_name or not match.room_password:
#             continue

#         # Solo event
#         if event.participant_type == "solo":
#             competitors = StageGroupCompetitor.objects.filter(stage_group=match.group, player__isnull=False)
#             for sc in competitors:
#                 user = sc.player.user
#                 if user:
#                     message = (
#                         f"Hello {user.username}, your match details for '{event.event_name}' (Stage: {match.group.stage.stage_name}, "
#                         f"Group: {match.group.group_name}, Match: {match.match_number}) are:\n"
#                         f"Room ID: {match.room_id}\n"
#                         f"Room Name: {match.room_name}\n"
#                         f"Password: {match.room_password}"
#                     )
#                     Notifications.objects.create(user=user, message=message)
#                     total_notifications += 1

#         # Team event
#         else:
#             teams = StageGroupCompetitor.objects.filter(stage_group=match.group, tournament_team__isnull=False)
#             for sgc in teams:
#                 team = sgc.tournament_team
#                 for player in team.players.all():
#                     if player.user:
#                         message = (
#                             f"Hello {player.user.username}, your match details for '{event.event_name}' (Stage: {match.group.stage.stage_name}, "
#                             f"Group: {match.group.group_name}, Match: {match.match_number}) are:\n"
#                             f"Room ID: {match.room_id}\n"
#                             f"Room Name: {match.room_name}\n"
#                             f"Password: {match.room_password}"
#                         )
#                         Notifications.objects.create(user=player.user, message=message)
#                         total_notifications += 1

#     return Response({
#         "message": f"Sent match room notifications to {total_notifications} users for event '{event.event_name}'."
#     }, status=200)

# @api_view(["POST"])
# def send_match_room_details_notification_to_competitor(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     # ---------------- INPUT ----------------
#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")

#     if not event_id or not group_id:
#         return Response({"message": "event_id and group_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     group = get_object_or_404(StageGroups, group_id=group_id)

#     matches = Match.objects.filter(group=group)
#     if not matches.exists():
#         return Response({"message": "No matches found for this group."}, status=400)

#     total_notifications = 0

#     for match in matches:
#         if not all([match.room_id, match.room_name, match.room_password]):
#             continue

#         # SOLO EVENT
#         if event.participant_type == "solo":
#             competitors = StageGroupCompetitor.objects.select_related(
#                 "player__user"
#             ).filter(stage_group=group, player__isnull=False)

#             for sc in competitors:
#                 user = sc.player.user
#                 if not user:
#                     continue

#                 Notifications.objects.create(
#                     user=user,
#                     message=(
#                         f"Hello {user.username}, your match details for '{event.event_name}'\n"
#                         f"Stage: {group.stage.stage_name}\n"
#                         f"Group: {group.group_name}\n"
#                         f"Match: {match.match_number}\n\n"
#                         f"Room ID: {match.room_id}\n"
#                         f"Room Name: {match.room_name}\n"
#                         f"Password: {match.room_password}"
#                     )
#                 )
#                 total_notifications += 1

#         # TEAM EVENT
#         else:
#             teams = StageGroupCompetitor.objects.select_related(
#                 "tournament_team"
#             ).filter(stage_group=group, tournament_team__isnull=False)

#             for sgc in teams:
#                 for player in sgc.tournament_team.players.select_related("user").all():
#                     if not player.user:
#                         continue

#                     Notifications.objects.create(
#                         user=player.user,
#                         message=(
#                             f"Hello {player.user.username}, your match details for '{event.event_name}'\n"
#                             f"Stage: {group.stage.stage_name}\n"
#                             f"Group: {group.group_name}\n"
#                             f"Match: {match.match_number}\n\n"
#                             f"Room ID: {match.room_id}\n"
#                             f"Room Name: {match.room_name}\n"
#                             f"Password: {match.room_password}"
#                         )
#                     )
#                     total_notifications += 1

#     return Response({
#         "message": f"Sent match room notifications to {total_notifications} users."
#     }, status=200)



@api_view(["POST"])
def send_match_room_details_notification_to_competitor(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    event_id = request.data.get("event_id")
    group_id = request.data.get("group_id")
    if not event_id or not group_id:
        return Response({"message": "event_id and group_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    group = get_object_or_404(StageGroups, group_id=group_id)

    matches = Match.objects.filter(group=group).order_by("match_number")
    if not matches.exists():
        return Response({"message": "No matches found for this group."}, status=400)

    total_notifications = 0

    for match in matches:
        if not (match.room_id and match.room_name and match.room_password):
            continue

        if event.participant_type == "solo":
            competitors = (StageGroupCompetitor.objects
                           .select_related("player__user")
                           .filter(stage_group=group, player__isnull=False))

            for sc in competitors:
                user = sc.player.user
                if not user:
                    continue

                Notifications.objects.create(
                    user=user,
                    message=(
                        f"Hello {user.username}, your match details for '{event.event_name}'\n"
                        f"Stage: {group.stage.stage_name}\n"
                        f"Group: {group.group_name}\n"
                        f"Match: {match.match_number}\n\n"
                        f"Room ID: {match.room_id}\n"
                        f"Room Name: {match.room_name}\n"
                        f"Password: {match.room_password}"
                    )
                )
                total_notifications += 1
        else:
            teams = (StageGroupCompetitor.objects
                     .select_related("tournament_team")
                     .filter(stage_group=group, tournament_team__isnull=False))

            for sgc in teams:
                members = sgc.tournament_team.members.select_related("user").all()
                for m in members:
                    if not m.user:
                        continue

                    Notifications.objects.create(
                        user=m.user,
                        message=(
                            f"Hello {m.user.username}, your match details for '{event.event_name}'\n"
                            f"Stage: {group.stage.stage_name}\n"
                            f"Group: {group.group_name}\n"
                            f"Match: {match.match_number}\n\n"
                            f"Room ID: {match.room_id}\n"
                            f"Room Name: {match.room_name}\n"
                            f"Password: {match.room_password}"
                        )
                    )
                    total_notifications += 1

    return Response({"message": f"Sent match room notifications to {total_notifications} users."}, status=200)


@api_view(["POST"])
def remove_all_stage_competitors_from_groups_and_their_discord_roles(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    groups = stage.groups.all()
    total_removed = 0

    for group in groups:
        competitors = StageGroupCompetitor.objects.filter(stage_group=group)
        for competitor in competitors:
            user = competitor.player.user
            if user.discord_id and group.group_discord_role_id:
                remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)
            competitor.delete()
            total_removed += 1

    return Response({
        "message": f"Removed {total_removed} competitors from all groups in stage '{stage.stage_name}' and their Discord roles."
    }, status=200)


@api_view(["POST"])
def delete_stage(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    # Remove all group roles from competitors
    groups = stage.groups.all()
    for group in groups:
        competitors = StageGroupCompetitor.objects.filter(stage_group=group)
        for competitor in competitors:
            user = competitor.player.user
            if user.discord_id and group.group_discord_role_id:
                remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

    stage.delete()

    return Response({
        "message": f"Stage '{stage.stage_name}' has been removed along with all associated groups and competitor roles."
    }, status=200)



@api_view(["POST"])
def delete_group(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required."}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)

    # Remove all group roles from competitors
    competitors = StageGroupCompetitor.objects.filter(stage_group=group)
    for competitor in competitors:
        user = competitor.player.user
        if user.discord_id and group.group_discord_role_id:
            remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

    group.delete()

    return Response({
        "message": f"Group '{group.group_name}' has been removed along with all associated competitor roles."
    }, status=200)


@api_view(["POST"])
def get_all_user_id_in_stage(request):
    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    competitors = StageCompetitor.objects.filter(
        stage=stage,
        player__isnull=False
    ).select_related("player__user")

    user_ids = [
        competitor.player.user.user_id
        for competitor in competitors
        if competitor.player.user is not None
    ]

    return Response({
        "stage_id": stage.stage_id,
        "stage_name": stage.stage_name,
        "user_ids": user_ids
    }, status=200)


@api_view(["POST"])
def get_all_user_id_in_group(request):
    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required."}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)

    competitors = StageGroupCompetitor.objects.filter(
        stage_group=group,
        player__isnull=False
    ).select_related("player__user")

    user_ids = [
        competitor.player.user.user_id
        for competitor in competitors
        if competitor.player.user is not None
    ]

    return Response({
        "group_id": group.group_id,
        "group_name": group.group_name,
        "user_ids": user_ids
    }, status=200)


@api_view(["POST"])
def delete_notifications_from_users_in_a_group(request):
    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required."}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)

    competitors = StageGroupCompetitor.objects.filter(
        stage_group=group,
        player__isnull=False
    ).select_related("player__user")

    user_ids = [
        competitor.player.user.user_id
        for competitor in competitors
        if competitor.player.user is not None
    ]

    deleted_count = Notifications.objects.filter(user__user_id__in=user_ids).delete()[0]

    return Response({
        "message": f"Deleted {deleted_count} notifications for users in group '{group.group_name}'."
    }, status=200)


# @api_view(["POST"])
# def create_leaderboard(request):
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]
#     admin = validate_token(token)
#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)
    
#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     group_id = request.data.get("group_id")


@api_view(["POST"])
def check_if_user_registered_in_event(request):
    email = request.data.get("email")
    event_id = request.data.get("event_id")

    if not email or not event_id:
        return Response({"message": "email and event_id are required."}, status=400)
    event = get_object_or_404(Event, event_id=event_id)
    user = get_object_or_404(User, email=email)
    is_registered = RegisteredCompetitors.objects.filter(
        event=event,
        user=user,
        status="registered"
    ).exists()
    return Response({
        "email": email,
        "event_id": event_id,
        "is_registered": is_registered
    }, status=200)


from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db import transaction

@api_view(["POST"])
def create_leaderboard(request):
    # DEPRECATED (no longer in use), same as create_leaderboard_manually. Leaderboards
    # are created AUTOMATICALLY for every group when an event's stages/groups/maps are set
    # up (create_event ~L1055 + edit_event group sync ~L2153). The URL route for this view
    # is commented out in urls.py so it is unreachable; its only FE caller
    # (UpdatedConfigurePointSystem) is dead code. Retained only to avoid churn.
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, once the owning event is resolved from event_id —
    # org members with can_upload_results may create leaderboards for THEIR org's events.

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    group_id = request.data.get("group_id")
    leaderboard_name = request.data.get("leaderboard_name")
    leaderboard_method = request.data.get("leaderboard_method")  # optional
    file_type = request.data.get("file_type")  # optional

    if not event_id or not stage_id or not group_id:
        return Response({"message": "event_id, stage_id and group_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
    group = get_object_or_404(StageGroups, group_id=group_id, stage=stage)

    # ── AUTH (event-scoped): event derived directly from request event_id (then stage/group
    # are validated to belong to it). AFC event admins always pass; otherwise allow org
    # members holding can_upload_results on the event's owning org. org_can_event treats
    # native (org=None) events as admin-only, so organizers never touch events outside their org.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    if not leaderboard_name:
        leaderboard_name = f"{event.event_name} - {stage.stage_name} - {group.group_name}"

    # Optional: placement_points passed from frontend
    placement_points = request.data.get("placement_points", {})
    if isinstance(placement_points, str):
        import json
        placement_points = json.loads(placement_points)

    kill_point = request.data.get("kill_point", 1)
    try:
        kill_point = float(kill_point)
    except:
        kill_point = 1.0

    with transaction.atomic():
        leaderboard, created = Leaderboard.objects.get_or_create(
            event=event,
            stage=stage,
            group=group,
            defaults={
                "leaderboard_name": leaderboard_name,
                "creator": admin,
                "placement_points": placement_points or {},
                "kill_point": kill_point,
            },
            leaderboard_method=leaderboard_method,
            file_type=file_type
        )

        # If it already exists, you may want to update the points config:
        if not created:
            if placement_points:
                leaderboard.placement_points = placement_points
            leaderboard.kill_point = kill_point
            leaderboard.leaderboard_name = leaderboard_name
            leaderboard.save()

        # OPTIONAL: attach leaderboard to this group's matches (only if not already set)
        Match.objects.filter(group=group, leaderboard__isnull=True).update(leaderboard=leaderboard)

    return Response({
        "message": "Leaderboard created successfully." if created else "Leaderboard already existed. Updated config.",
        "leaderboard_id": leaderboard.leaderboard_id,
        "created": created,
    }, status=200)

# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from rest_framework import status
# from django.shortcuts import get_object_or_404
# from django.db import transaction

# @api_view(["POST"])
# def create_leaderboard(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     group_id = request.data.get("group_id")
#     leaderboard_name = request.data.get("leaderboard_name")  # optional
#     leaderboard_method = request.data.get("leaderboard_method")  # optional
#     file_type = request.data.get("file_type")  # optional

#     if not event_id or not stage_id or not group_id:
#         return Response({"message": "event_id, stage_id and group_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
#     group = get_object_or_404(StageGroups, group_id=group_id, stage=stage)

#     if not leaderboard_name:
#         leaderboard_name = f"{event.event_name} - {stage.stage_name} - {group.group_name}"

#     with transaction.atomic():
#         leaderboard, created = Leaderboard.objects.get_or_create(
#             event=event,
#             stage=stage,
#             group=group,
#             leaderboard_method=leaderboard_method,
#             file_type=file_type,
#             defaults={
#                 "leaderboard_name": leaderboard_name,
#                 "creator": admin,
#             }
#         )

#         # attach matches for this group to this leaderboard (no N+1)
#         updated = (
#             Match.objects
#             .filter(group=group)
#             .exclude(leaderboard=leaderboard)
#             .update(leaderboard=leaderboard)
#         )

#     return Response({
#         "message": "Leaderboard created." if created else "Leaderboard already existed; updated matches.",
#         "leaderboard_id": leaderboard.leaderboard_id,
#         "matches_linked": updated,
#     }, status=200)



# import re
# import json
# from django.db import transaction
# from rest_framework.decorators import api_view, parser_classes
# from rest_framework.parsers import MultiPartParser, FormParser
# from rest_framework.response import Response
# from rest_framework import status
# from django.shortcuts import get_object_or_404

# SOLO_BLOCK_RE = re.compile(
#     r"Rank:\s*(?P<rank>\d+).*?"
#     r"KillScore:\s*(?P<kill_score>\d+).*?"
#     r"RankScore:\s*(?P<rank_score>\d+).*?"
#     r"TotalScore:\s*(?P<total_score>\d+).*?\n"
#     r"NAME:\s*(?P<name>.+?)\s+ID:\s*(?P<uid>\d+)\s+KILL:\s*(?P<kills>\d+)",
#     re.DOTALL
# )

# @api_view(["POST"])
# @parser_classes([MultiPartParser, FormParser])
# def upload_solo_match_result(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     match_id = request.data.get("match_id")
#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)

#     uploaded_file = request.FILES.get("file")
#     if not uploaded_file:
#         return Response({"message": "file is required."}, status=400)

#     # placement_points can be JSON string or omitted (use default)
#     placement_points_raw = request.data.get("placement_points")
#     if placement_points_raw:
#         if isinstance(placement_points_raw, str):
#             placement_points = json.loads(placement_points_raw)
#         else:
#             placement_points = placement_points_raw
#         # normalize keys to int
#         placement_points = {int(k): int(v) for k, v in placement_points.items()}
#     else:
#         placement_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

#     kill_point_value = int(request.data.get("kill_point_value", 1))  # 1 point per kill by default

#     match = get_object_or_404(Match, match_id=match_id)
#     event = match.group.stage.event if match.group else (match.leaderboard.event if match.leaderboard else None)
#     if not event:
#         return Response({"message": "Match is not linked to a group/leaderboard with an event."}, status=400)

#     if event.participant_type != "solo":
#         return Response({"message": "This API is for SOLO events only."}, status=400)

#     text = uploaded_file.read().decode("utf-8", errors="ignore")

#     parsed = []
#     for m in SOLO_BLOCK_RE.finditer(text):
#         parsed.append({
#             "placement": int(m.group("rank")),
#             "kills": int(m.group("kills")),
#             "uid": m.group("uid").strip(),
#             "name": m.group("name").strip(),
#             "raw_kill_score": int(m.group("kill_score")),
#             "raw_rank_score": int(m.group("rank_score")),
#             "raw_total_score": int(m.group("total_score")),
#         })

#     if not parsed:
#         return Response({
#             "message": "No results parsed from file. File format may not match expected SOLO layout."
#         }, status=400)

#     # Map uid -> RegisteredCompetitors
#     uids = [p["uid"] for p in parsed]
#     reg_map = {
#         rc.user.uid: rc
#         for rc in RegisteredCompetitors.objects.select_related("user")
#         .filter(event=event, status="registered", user__uid__in=uids)
#     }

#     missing = [p for p in parsed if p["uid"] not in reg_map]

#     stats_to_create = []
#     for p in parsed:
#         rc = reg_map.get(p["uid"])
#         if not rc:
#             continue

#         placement_pts = placement_points.get(p["placement"], 0)
#         kill_pts = p["kills"] * kill_point_value
#         total_pts = placement_pts + kill_pts

#         stats_to_create.append(SoloPlayerMatchStats(
#             match=match,
#             competitor=rc,
#             placement=p["placement"],
#             kills=p["kills"],
#             placement_points=placement_pts,
#             kill_points=kill_pts,
#             total_points=total_pts,
#             raw_kill_score=p["raw_kill_score"],
#             raw_rank_score=p["raw_rank_score"],
#             raw_total_score=p["raw_total_score"],
#         ))

#     with transaction.atomic():
#         # wipe and replace ensures no duplicates and makes re-upload safe
#         SoloPlayerMatchStats.objects.filter(match=match).delete()
#         SoloPlayerMatchStats.objects.bulk_create(stats_to_create, batch_size=500)

#     return Response({
#         "message": "Solo match results uploaded.",
#         "match_id": match.match_id,
#         "parsed_rows": len(parsed),
#         "saved_rows": len(stats_to_create),
#         "missing_registered_competitors": [
#             {"uid": p["uid"], "name": p["name"], "placement": p["placement"]}
#             for p in missing[:30]
#         ],
#         "missing_count": len(missing),
#     }, status=200)



# import json
# import re
# from django.db import transaction
# from django.shortcuts import get_object_or_404
# from rest_framework.decorators import api_view, parser_classes
# from rest_framework.parsers import MultiPartParser, FormParser
# from rest_framework.response import Response
# from rest_framework import status

# # Matches the 2-line SOLO blocks in your log file:
# # TeamName: Rank: X KillScore: Y RankScore: Z TotalScore: T
# # NAME: <name> ID: <uid> KILL: <kills>
# SOLO_BLOCK_RE = re.compile(
#     r"TeamName:\s*Rank:\s*(?P<rank>\d+)\s*"
#     r"KillScore:\s*(?P<kill_score>\d+)\s*"
#     r"RankScore:\s*(?P<rank_score>\d+)\s*"
#     r"TotalScore:\s*(?P<total_score>\d+)\s*"
#     r"\s*[\r\n]+"
#     r"NAME:\s*(?P<name>.*?)\s*"
#     r"ID:\s*(?P<uid>\d+)\s*"
#     r"KILL:\s*(?P<kills>\d+)",
#     re.MULTILINE
# )

# @api_view(["POST"])
# @parser_classes([MultiPartParser, FormParser])
# def upload_solo_match_result(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     # ---------------- INPUT ----------------
#     match_id = request.data.get("match_id")
#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)

#     uploaded_file = request.FILES.get("file")
#     if not uploaded_file:
#         return Response({"message": "file is required."}, status=400)

#     match = get_object_or_404(Match, match_id=match_id)

#     # ---------------- EVENT + LEADERBOARD ----------------
#     if not match.group:
#         return Response({"message": "This match is not linked to a group."}, status=400)

#     event = match.group.stage.event
#     if event.participant_type != "solo":
#         return Response({"message": "This API is for SOLO events only."}, status=400)

#     # Prefer match.leaderboard; else use unique leaderboard (event, stage, group)
#     leaderboard = match.leaderboard
#     if not leaderboard:
#         leaderboard = Leaderboard.objects.filter(
#             event=event,
#             stage=match.group.stage,
#             group=match.group
#         ).first()

#     if not leaderboard:
#         return Response(
#             {"message": "No leaderboard found for this group. Create/link leaderboard first."},
#             status=400
#         )

#     # placement_points stored as JSONField like {"1": 12, "2": 9, ...}
#     placement_points_raw = leaderboard.placement_points or {}
#     try:
#         placement_points = {int(k): int(v) for k, v in placement_points_raw.items()}
#     except Exception:
#         return Response(
#             {"message": "Leaderboard placement_points must be a JSON object like {'1': 12, '2': 9, ...}"},
#             status=400
#         )

#     if not placement_points:
#         # fallback default (rank 1-10)
#         placement_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

#     kill_point_value = float(getattr(leaderboard, "kill_point", 1.0) or 1.0)

#     # ---------------- PARSE FILE ----------------
#     text = uploaded_file.read().decode("utf-8", errors="ignore")

#     parsed = []
#     for m in SOLO_BLOCK_RE.finditer(text):
#         parsed.append({
#             "placement": int(m.group("rank")),
#             "uid": m.group("uid").strip(),
#             "name": m.group("name").strip(),
#             "kills": int(m.group("kills")),
#             "raw_kill_score": int(m.group("kill_score")),
#             "raw_rank_score": int(m.group("rank_score")),
#             "raw_total_score": int(m.group("total_score")),
#         })

#     if not parsed:
#         return Response(
#             {"message": "No results parsed. Your log format didn't match SOLO_BLOCK_RE."},
#             status=400
#         )

#     # ---------------- MAP UID -> REGISTERED COMPETITOR ----------------
#     uids = [p["uid"] for p in parsed]

#     reg_qs = RegisteredCompetitors.objects.select_related("user").filter(
#         event=event,
#         status="registered",
#         user__uid__in=uids
#     )
#     reg_map = {str(rc.user.uid): rc for rc in reg_qs}

#     missing = [p for p in parsed if p["uid"] not in reg_map]

#     # ---------------- SAVE ----------------
#     stats_to_create = []
#     for p in parsed:
#         rc = reg_map.get(p["uid"])
#         if not rc:
#             continue

#         placement_pts = placement_points.get(p["placement"], 0)
#         kill_pts = p["kills"] * kill_point_value  # keep float support
#         total_pts = placement_pts + kill_pts

#         stats_to_create.append(SoloPlayerMatchStats(
#             match=match,
#             competitor=rc,
#             placement=p["placement"],
#             kills=p["kills"],
#             placement_points=placement_pts,
#             kill_points=kill_pts,
#             total_points=total_pts,
#             raw_kill_score=p["raw_kill_score"],
#             raw_rank_score=p["raw_rank_score"],
#             raw_total_score=p["raw_total_score"],
#         ))

#     with transaction.atomic():
#         # re-upload safe
#         SoloPlayerMatchStats.objects.filter(match=match).delete()
#         SoloPlayerMatchStats.objects.bulk_create(stats_to_create, batch_size=500)

#     return Response({
#         "message": "Solo match results uploaded using leaderboard scoring config.",
#         "match_id": match.match_id,
#         "leaderboard_id": leaderboard.leaderboard_id,
#         "kill_point": kill_point_value,
#         "parsed_rows": len(parsed),
#         "saved_rows": len(stats_to_create),
#         "missing_count": len(missing),
#         "missing_registered_competitors_preview": [
#             {"uid": p["uid"], "name": p["name"], "placement": p["placement"]}
#             for p in missing[:30]
#         ],
#     }, status=200)


import json
import re
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status

# Matches your file format like:
# Rank: 1  ... TotalScore: 120
# NAME: VT KingHydra  ID: 490350075  KILL: 6
SOLO_BLOCK_RE = re.compile(
    r"Rank:\s*(?P<placement>\d+).*?\n"
    r"NAME:\s*(?P<name>.+?)\s+ID:\s*(?P<uid>\d+)\s+KILL:\s*(?P<kills>\d+)",
    re.DOTALL
)

@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def upload_solo_match_result(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: auth is finalised below, after the owning event is resolved — org members with
    # can_upload_results may upload for THEIR org's events, so we need the event in hand first.

    # ---------------- INPUT ----------------
    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return Response({"message": "file is required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)

    # ---------------- EVENT + LEADERBOARD ----------------
    # In your DB, SOLO matches should be linked to a group
    if not match.group:
        return Response({"message": "This match is not linked to a group."}, status=400)

    event = match.group.stage.event

    # ── AUTH (event-scoped): AFC event admins always pass; otherwise allow org members
    # holding can_upload_results on the event's owning org. org_can_event treats native
    # (org=None) events as admin-only, so organizers can never touch events outside their org.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    if event.participant_type != "solo":
        return Response({"message": "This API is for SOLO events only."}, status=400)

    # Find leaderboard (match.leaderboard preferred)
    leaderboard = match.leaderboard
    if not leaderboard:
        leaderboard = Leaderboard.objects.filter(
            event=event,
            stage=match.group.stage,
            group=match.group
        ).first()

    if not leaderboard:
        return Response(
            {"message": "No leaderboard found for this group. Create leaderboard first."},
            status=400
        )

    # Use scoring config from leaderboard
    placement_points_raw = leaderboard.placement_points or {}
    try:
        placement_points = {int(k): int(v) for k, v in placement_points_raw.items()}
    except Exception:
        return Response(
            {"message": "Leaderboard placement_points must be a JSON object like {'1':12,'2':9,...}"},
            status=400
        )

    if not placement_points:
        placement_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

    kill_point_value = float(getattr(leaderboard, "kill_point", 1.0) or 1.0)

    # ---------------- PARSE FILE ----------------
    text = uploaded_file.read().decode("utf-8", errors="ignore")

    parsed = []
    for m in SOLO_BLOCK_RE.finditer(text):
        parsed.append({
            "placement": int(m.group("placement")),
            "kills": int(m.group("kills")),
            "uid": m.group("uid").strip(),
            "name": m.group("name").strip(),
        })

    if not parsed:
        return Response(
            {"message": "No results parsed. Your log format may differ from SOLO_BLOCK_RE."},
            status=400
        )

    # ---------------- MAP UID -> RegisteredCompetitors ----------------
    uids = [p["uid"] for p in parsed]

    reg_qs = RegisteredCompetitors.objects.select_related("user").filter(
        event=event,
        status="registered",
        user__uid__in=uids
    )
    reg_map = {rc.user.uid: rc for rc in reg_qs}

    missing = [p for p in parsed if p["uid"] not in reg_map]

    # ---------------- SAVE ----------------
    stats_to_create = []
    for p in parsed:
        rc = reg_map.get(p["uid"])
        if not rc:
            continue

        # Route through the shared solo formula (kills always counted on an upload row).
        # NOTE: this drops the old int(round(...)) on kill points in favour of scoring's
        # int() truncation, so all call sites agree. Identical for the default kill_point=1.0;
        # only differs if a leaderboard uses a fractional kill_point (see report).
        pts = scoring_lib.compute_solo_points(
            placement_points=placement_points, kill_point=kill_point_value,
            placement=p["placement"], kills=p["kills"], played=True,
        )
        placement_pts = pts["placement_points"]
        kill_pts = pts["kill_points"]
        total_pts = pts["total_points"]

        stats_to_create.append(
            SoloPlayerMatchStats(
                match=match,
                competitor=rc,
                placement=p["placement"],
                kills=p["kills"],
                placement_points=placement_pts,
                total_points=total_pts,
                kill_points=kill_pts,
            )
        )

    with transaction.atomic():
        # re-upload safe: prevents duplicates and makes upload idempotent
        SoloPlayerMatchStats.objects.filter(match=match).delete()
        SoloPlayerMatchStats.objects.bulk_create(stats_to_create, batch_size=500)

    match.result_inputted = True
    match.save()

    return Response({
        "message": "Solo match results uploaded (scoring pulled from leaderboard).",
        "match_id": match.match_id,
        "leaderboard_id": leaderboard.leaderboard_id,
        "kill_point": kill_point_value,
        "placement_points_used": placement_points,
        "parsed_rows": len(parsed),
        "saved_rows": len(stats_to_create),
        "missing_count": len(missing),
        "missing_registered_competitors_preview": [
            {"uid": p["uid"], "name": p["name"], "placement": p["placement"]}
            for p in missing[:30]
        ],
    }, status=200)


@api_view(["GET"])
def get_all_leaderboards(request):
    leaderboards = Leaderboard.objects.select_related("event", "stage", "group", "creator").all()
    data = []
    for lb in leaderboards:
        data.append({
            "leaderboard_id": lb.leaderboard_id,
            "leaderboard_name": lb.leaderboard_name,
            "event": {
                "event_id": lb.event.event_id,
                "event_name": lb.event.event_name,
                # competition_type ("tournament" / "scrims") lets the admin Leaderboards
                # page (frontend app/(a)/a/leaderboards/page.tsx) split the total count into
                # the "Tournament Leaderboards" vs "Scrim Leaderboards" stat cards. The value
                # comes from Event.competition_type (see Event.COMPETITION_TYPE_CHOICES).
                "competition_type": lb.event.competition_type,
            },
            "stage": {
                "stage_id": lb.stage.stage_id,
                "stage_name": lb.stage.stage_name,
            },
            "group": {
                "group_id": lb.group.group_id,
                "group_name": lb.group.group_name,
            },
            "creator": {
                "user_id": lb.creator.user_id,
                "username": lb.creator.username,
            } if lb.creator else None,
            "placement_points": lb.placement_points,
            "kill_point": lb.kill_point,
            "file_type": lb.file_type,
            "leaderboard_method": lb.leaderboard_method,
            "created_at": lb.creation_date
        })
    return Response({"leaderboards": data}, status=200) 


# @api_view(["POST"])
# def reconcile_group_discord_roles(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=403)

#     group_id = request.data.get("group_id")
#     if not group_id:
#         return Response({"message": "group_id is required."}, status=400)

#     group = get_object_or_404(StageGroups, group_id=group_id)
#     role_id = group.group_discord_role_id
#     if not role_id:
#         return Response({"message": "This group has no group_discord_role_id set."}, status=400)

#     competitors = (
#         StageGroupCompetitor.objects
#         .filter(stage_group=group, player__isnull=False)
#         .select_related("player__user")
#     )

#     checked = 0
#     queued = 0
#     already_ok = 0
#     not_in_server = 0
#     failed_check = 0

#     for c in competitors:
#         user = c.player.user
#         if not user or not user.discord_id:
#             continue

#         checked += 1

#         try:
#             has_role = member_has_role(user.discord_id, role_id)
#         except Exception:
#             failed_check += 1
#             # If you want: queue anyway
#             has_role = False

#         if has_role:
#             already_ok += 1
#             continue

#         # queue assignment (idempotent-ish using DB)
#         assignment, created = DiscordRoleAssignment.objects.get_or_create(
#             user=user,
#             discord_id=user.discord_id,
#             role_id=role_id,
#             stage=group.stage,
#             group=group,
#             defaults={"status": "pending"}
#         )

#         # If it existed but failed earlier, retry it:
#         if assignment.status in ("failed", "pending"):
#             assignment.status = "pending"
#             assignment.error_message = None
#             assignment.save()
#             assign_role_from_assignment_task.delay(assignment.id)
#             queued += 1

#     return Response({
#         "message": "Reconcile finished.",
#         "group_id": group.group_id,
#         "checked": checked,
#         "already_ok": already_ok,
#         "queued": queued,
#         "failed_check": failed_check
#     }, status=200)





# @api_view(["GET"])
# def get_all_leaderboard_details_for_event(request):


@api_view(["POST"])
def reconcile_group_roles(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Forbidden."}, status=403)

    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    created = 0
    skipped = 0

    # all players in groups
    qs = StageGroupCompetitor.objects.select_related("player__user", "stage_group").filter(
        stage_group__stage=stage,
        player__isnull=False
    )

    for sgc in qs:
        user = sgc.player.user
        group = sgc.stage_group

        if not user or not user.discord_id or not group.group_discord_role_id:
            skipped += 1
            continue

        # already success?
        exists = DiscordRoleAssignment.objects.filter(
            user=user,
            stage=stage,
            group=group,
            role_id=group.group_discord_role_id,
            status="success"
        ).exists()

        if exists:
            skipped += 1
            continue

        DiscordRoleAssignment.objects.get_or_create(
            user=user,
            discord_id=user.discord_id,
            role_id=group.group_discord_role_id,
            stage=stage,
            group=group,
            defaults={"status": "pending"}
        )
        created += 1

    # kick worker
    assign_group_roles_from_db_task.delay(stage.stage_id)

    return Response({
        "message": "Reconcile started.",
        "created_pending": created,
        "skipped": skipped
    }, status=200)


import json
from collections import defaultdict

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework import status

# helpers
def _normalize_points_json(points_raw):
    if not points_raw:
        return {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}
    try:
        return {int(k): int(v) for k, v in (points_raw or {}).items()}
    except Exception:
        return {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}
    

# @api_view(["POST"])
# def get_all_leaderboard_details_for_event(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=401)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     # Prefetch to reduce queries
#     stages = (
#         event.stages.all()
#         .order_by("start_date")
#         .prefetch_related(
#             "groups",
#             "groups__matches",
#             "groups__leaderboards",
#         )
#     )

#     # We'll also pull stats in bulk to avoid N+1
#     # All matches in event groups:
#     all_match_ids = []
#     for st in stages:
#         for grp in st.groups.all():
#             for m in grp.matches.all():
#                 all_match_ids.append(m.match_id)

#     # SOLO stats bulk
#     solo_stats_by_match = defaultdict(list)
#     if event.participant_type == "solo" and all_match_ids:
#         for s in SoloPlayerMatchStats.objects.select_related("competitor__user", "match").filter(match_id__in=all_match_ids):
#             solo_stats_by_match[s.match_id].append(s)

#     # TEAM stats bulk
#     team_stats_by_match = defaultdict(list)
#     if event.participant_type != "solo" and all_match_ids:
#         for s in TournamentTeamMatchStats.objects.select_related("tournament_team__team", "match").filter(match_id__in=all_match_ids):
#             team_stats_by_match[s.match_id].append(s)

#     response_stages = []

#     for stage in stages:
#         stage_groups_payload = []

#         for group in stage.groups.all():
#             # Get leaderboard for this group (unique_together = event,stage,group)
#             leaderboard = group.leaderboards.filter(event=event, stage=stage, group=group).first()

#             placement_points = _normalize_points_json(getattr(leaderboard, "placement_points", {}) if leaderboard else {})
#             kill_point = float(getattr(leaderboard, "kill_point", 1.0) if leaderboard else 1.0)

#             matches_payload = []
#             overall = {}  # key -> accumulator

#             for match in group.matches.all().order_by("match_number"):
#                 match_payload = {
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "stats": []
#                 }

#                 if event.participant_type == "solo":
#                     stats = solo_stats_by_match.get(match.match_id, [])
#                     for s in stats:
#                         username = s.competitor.user.username if s.competitor and s.competitor.user else "Unknown"
#                         uid = s.competitor.user.uid if s.competitor and s.competitor.user else None

#                         # total_points already stored in SoloPlayerMatchStats (recommended)
#                         total_pts = int(getattr(s, "total_points", 0) or 0)

#                         match_payload["stats"].append({
#                             "competitor_id": s.competitor.id,
#                             "username": username,
#                             "uid": uid,
#                             "placement": s.placement,
#                             "kills": s.kills,
#                             "placement_points": s.placement_points,
#                             "kill_points": getattr(s, "kill_points", s.kills * kill_point),
#                             "total_points": total_pts,
#                         })

#                         key = s.competitor.id
#                         if key not in overall:
#                             overall[key] = {
#                                 "competitor_id": s.competitor.id,
#                                 "username": username,
#                                 "uid": uid,
#                                 "total_points": 0,
#                                 "total_kills": 0,
#                                 "matches_played": 0,
#                             }
#                         overall[key]["total_points"] += total_pts
#                         overall[key]["total_kills"] += int(s.kills or 0)
#                         overall[key]["matches_played"] += 1

#                 else:
#                     stats = team_stats_by_match.get(match.match_id, [])
#                     for s in stats:
#                         team_name = s.tournament_team.team.team_name if s.tournament_team and s.tournament_team.team else "Unknown Team"
#                         team_id = s.tournament_team.tournament_team_id if s.tournament_team else None

#                         # if you added total_points fields on model, use them.
#                         # otherwise compute on the fly from placement_points + kills * kill_point
#                         placement_pts = int(getattr(s, "placement_points", None) or placement_points.get(int(s.placement), 0))
#                         kill_pts = int(getattr(s, "kill_points", None) or (int(s.kills or 0) * kill_point))
#                         total_pts = int(getattr(s, "total_points", None) or (placement_pts + kill_pts))

#                         match_payload["stats"].append({
#                             "tournament_team_id": team_id,
#                             "team_name": team_name,
#                             "placement": s.placement,
#                             "kills": s.kills,
#                             "placement_points": placement_pts,
#                             "kill_points": kill_pts,
#                             "total_points": total_pts,
#                         })

#                         key = team_id
#                         if key not in overall:
#                             overall[key] = {
#                                 "tournament_team_id": team_id,
#                                 "team_name": team_name,
#                                 "total_points": 0,
#                                 "total_kills": 0,
#                                 "matches_played": 0,
#                             }
#                         overall[key]["total_points"] += total_pts
#                         overall[key]["total_kills"] += int(s.kills or 0)
#                         overall[key]["matches_played"] += 1

#                 matches_payload.append(match_payload)

#             # rank overall: points desc, kills desc
#             overall_list = list(overall.values())
#             overall_list.sort(
#                 key=lambda x: (x["total_points"], x["total_kills"]),
#                 reverse=True
#             )
#             for i, row in enumerate(overall_list, start=1):
#                 row["rank"] = i

#             stage_groups_payload.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "group_discord_role_id": group.group_discord_role_id,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": {
#                     "leaderboard_id": leaderboard.leaderboard_id if leaderboard else None,
#                     "placement_points": placement_points,
#                     "kill_point": kill_point,
#                 },
#                 "matches": matches_payload,
#                 "overall_leaderboard": overall_list,
#             })

#         response_stages.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "stage_discord_role_id": stage.stage_discord_role_id,
#             "groups": stage_groups_payload,
#         })

#     return Response({
#         "event": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "participant_type": event.participant_type,
#         },
#         "stages": response_stages
#     }, status=200)



from django.db.models import Sum, Count, F
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

# @api_view(["POST"])
# def get_all_leaderboard_details_for_event(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     stages_payload = []

#     stages = event.stages.all().order_by("stage_id")
#     for stage in stages:
#         groups_payload = []
#         groups = stage.groups.all().order_by("group_id")

#         for group in groups:
#             # leaderboard config (unique_together means at most 1)
#             leaderboard = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

#             matches = Match.objects.filter(group=group).order_by("match_number")

#             matches_payload = []
#             for match in matches:
#                 if event.participant_type == "solo":
#                     match_stats = (SoloPlayerMatchStats.objects
#                                    .filter(match=match)
#                                    .select_related("competitor__user")
#                                    .values(
#                                        "competitor_id",
#                                        "competitor__user__username",
#                                        "placement",
#                                        "kills",
#                                        "placement_points",
#                                        "kill_points",
#                                        "total_points",
#                                    )
#                                    .order_by("placement"))
#                 else:
#                     match_stats = (TournamentTeamMatchStats.objects
#                                    .filter(match=match)
#                                    .select_related("tournament_team__team")
#                                    .values(
#                                        "tournament_team_id",
#                                        "tournament_team__team__team_name",
#                                        "placement",
#                                        "kills",
#                                        "placement_points",
#                                        "kill_points",
#                                        "total_points",
#                                    )
#                                    .order_by("placement"))

#                 matches_payload.append({
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "result_inputted": match.result_inputted,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "stats": list(match_stats),
#                 })

#             # overall leaderboard for this group
#             if event.participant_type == "solo":
#                 overall = SoloPlayerMatchStats.objects.filter(match__group__stage__event=event).values(
#                     "match__group_id",
#                     "competitor_id",  # keep the real field name here in values()
#                     "competitor__user__username",
#                     "matches_played",
#                     "total_kills"
#                     ).annotate(
#                         comp_id=F("competitor_id"),          # ✅ safe alias
#                         total_pts=Sum("total_points"),
#                     ).order_by("-total_points", "-kills", "competitor__user__username")
#             else:
#                 overall = (TournamentTeamMatchStats.objects
#                            .filter(match__group=group)
#                            .values(
#                                tournament_team_id=F("tournament_team_id"),
#                                team_name=F("tournament_team__team__team_name"),
#                            )
#                            .annotate(
#                                matches_played=Count("match_id"),
#                                total_points=Sum("total_points"),
#                                total_kills=Sum("kills"),
#                            )
#                            .order_by("-total_points", "-total_kills", "team_name"))

#             groups_payload.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "teams_qualifying": group.teams_qualifying,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": None if not leaderboard else {
#                     "leaderboard_id": leaderboard.leaderboard_id,
#                     "leaderboard_name": leaderboard.leaderboard_name,
#                     "kill_point": leaderboard.kill_point,
#                     "placement_points": leaderboard.placement_points,
#                     "leaderboard_method": leaderboard.leaderboard_method,
#                     "file_type": leaderboard.file_type,
#                     "last_updated": leaderboard.last_updated,
#                 },
#                 "matches": matches_payload,
#                 "overall_leaderboard": list(overall),
#             })

#         stages_payload.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": groups_payload
#         })

#     return Response({
#         "event_id": event.event_id,
#         "event_name": event.event_name,
#         "participant_type": event.participant_type,
#         "stages": stages_payload
#     }, status=200)



from django.db.models import Sum, Count, F, Value, IntegerField
from django.db.models.functions import Coalesce


from django.db.models import (
    F, Sum, Count, Value, IntegerField,
    Case, When, Subquery, OuterRef
)
from django.db.models.functions import Coalesce
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404


# ── Scoring-modes (sub-project A): on-read helpers for the standings builder below. ──
# Champion-Point + Point-Rush are computed ON READ (nothing derived is persisted), so an
# admin edit auto-corrects the leaderboard — these two helpers feed the pure logic in
# scoring.py (champion_for_group / rewards_from_standings) the per-lobby data it needs.
# Both live at module scope (not nested) so the builder can call them per group cheaply.


def _group_ranked_ids(group, participant_type):
    """Return this lobby's competitor ids in current finishing order (1st..last).

    The id is the tournament_team_id (team events) or competitor_id (solo events). The sort
    mirrors the standings sort the builder uses — total effective points first, then kills —
    so Point-Rush rewards land on whoever is actually 1st/2nd/3rd in the lobby right now.
    `effective_total` = placement + kill + bonus - penalty, computed the same way as in the
    per-group OVERALL aggregate below (kept in sync deliberately)."""
    if participant_type == "solo":
        rows = (
            SoloPlayerMatchStats.objects.filter(match__group=group)
            .values("competitor_id")
            .annotate(
                total=Coalesce(Sum("placement_points"), 0)
                + Coalesce(Sum("kill_points"), 0)
                + Coalesce(Sum("bonus_points"), 0)
                - Coalesce(Sum("penalty_points"), 0),
                kills=Coalesce(Sum("kills"), 0),
            )
            .order_by("-total", "-kills")
        )
        return [r["competitor_id"] for r in rows]

    rows = (
        TournamentTeamMatchStats.objects.filter(match__group=group)
        .values("tournament_team_id")
        .annotate(
            total=Coalesce(Sum("placement_points"), 0)
            + Coalesce(Sum("kill_points"), 0)
            + Coalesce(Sum("bonus_points"), 0)
            - Coalesce(Sum("penalty_points"), 0),
            kills=Coalesce(Sum("kills"), 0),
        )
        .order_by("-total", "-kills")
    )
    return [r["tournament_team_id"] for r in rows]


def _carry_over_for_stage(stage, participant_type):
    """Sum Point-Rush bonuses banked for `stage` by every source stage that targets it.

    A source stage hands out its `point_rush_reward` table (placement->bonus) PER LOBBY of
    that source stage — so a 2-lobby source seeds two sets of 1st/2nd/3rd bonuses. We walk
    `stage.point_rush_sources` (the reverse FK of point_rush_target_stage) and accumulate,
    keyed by competitor id, into a {id: bonus} dict the builder folds into each row's total."""
    from collections import defaultdict

    out = defaultdict(int)
    for src in stage.point_rush_sources.all():  # stages whose point_rush_target_stage == stage
        if not src.point_rush_enabled:
            continue
        # Per lobby of the SOURCE stage: rank its standings, map the reward table onto them.
        for grp in src.groups.all():
            ranked = _group_ranked_ids(grp, participant_type)
            rewards = scoring_lib.rewards_from_standings(ranked, src.point_rush_reward or {})
            for cid, bonus in rewards.items():
                out[cid] += bonus
    return dict(out)


@api_view(["POST"])
def get_all_leaderboard_details_for_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, once the owning event is resolved from event_id.
    # This is a READ of the full per-event leaderboard detail (admin/organizer surface), so we
    # scope it: org members managing THEIR org's results may view it.

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    # ── AUTH (event-scoped, read): event derived directly from request event_id. AFC event
    # admins always pass; otherwise allow org members holding can_upload_results on the event's
    # owning org. Native (org=None) events stay admin-only.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    stages_payload = []
    stages = event.stages.all().order_by("stage_id")

    for stage in stages:
        groups_payload = []
        groups = stage.groups.all().order_by("group_id")

        for group in groups:
            leaderboard = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()
            matches = Match.objects.filter(group=group).order_by("match_number")

            # ---------------- MATCHES (per match stats) ----------------
            matches_payload = []
            for match in matches:
                if event.participant_type == "solo":
                    match_stats = (
                        SoloPlayerMatchStats.objects
                        .filter(match=match)
                        .select_related("competitor__user")
                        .annotate(
                            username=F("competitor__user__username"),
                            effective_total=(
                                Coalesce(F("placement_points"), Value(0), output_field=IntegerField()) +
                                Coalesce(F("kill_points"), Value(0), output_field=IntegerField()) +
                                Coalesce(F("bonus_points"), Value(0), output_field=IntegerField()) -
                                Coalesce(F("penalty_points"), Value(0), output_field=IntegerField())
                            )
                        )
                        .values(
                            "competitor_id",
                            "username",
                            "placement",
                            "kills",
                            "placement_points",
                            "kill_points",
                            "bonus_points",
                            "penalty_points",
                            "total_points",
                            "effective_total",
                        )
                        # ✅ sorted by points not placement
                        .order_by("-effective_total", "-kills", "username")
                    )
                else:
                    team_stats_qs = (
                        TournamentTeamMatchStats.objects
                        .filter(match=match)
                        .select_related("tournament_team__team")
                        .annotate(
                            team_name=F("tournament_team__team__team_name"),
                            effective_total=(
                                Coalesce(F("placement_points"), 0) +
                                Coalesce(F("kill_points"), 0) +
                                Coalesce(F("bonus_points"), 0) -
                                Coalesce(F("penalty_points"), 0)
                            )
                        )
                        .order_by("-effective_total", "-kills", "team_name")
                    )

                    match_stats = []

                    for team_stat in team_stats_qs:

                        # 🔥 Fetch players for this team in this match
                        player_stats = (
                            TournamentPlayerMatchStats.objects
                            .filter(team_stats=team_stat)
                            .select_related("player")
                            .annotate(username=F("player__username"))
                            .values(
                                "player_id",
                                "username",
                                "kills",
                                "damage",
                                "assists",
                            )
                        )

                        match_stats.append({
                            "tournament_team_id": team_stat.tournament_team_id,
                            "team_name": team_stat.team_name,
                            "placement": team_stat.placement,
                            "kills": team_stat.kills,
                            "placement_points": team_stat.placement_points,
                            "kill_points": team_stat.kill_points,
                            "bonus_points": team_stat.bonus_points,
                            "penalty_points": team_stat.penalty_points,
                            "total_points": team_stat.total_points,
                            "effective_total": team_stat.effective_total,
                            "players": list(player_stats)  # 🔥 nested players here
                        })

                matches_payload.append({
                    "match_id": match.match_id,
                    "match_number": match.match_number,
                    "match_map": match.match_map,
                    "result_inputted": match.result_inputted,
                    "room_id": match.room_id,
                    "room_name": match.room_name,
                    "room_password": match.room_password,
                    "stats": list(match_stats),
                    "scoring_settings": match.scoring_settings or {},
                })

            # ---------------- OVERALL LEADERBOARD (PER GROUP) ----------------
            if event.participant_type == "solo":
                last_placement_subq = Subquery(
                    SoloPlayerMatchStats.objects
                    .filter(match__group=group, competitor_id=OuterRef("competitor_id"))
                    .order_by("-match__match_number")
                    .values("placement")[:1]
                )

                overall = (
                    SoloPlayerMatchStats.objects
                    .filter(match__group=group)
                    .values(
                        "competitor_id",
                        "competitor__user__username",
                    )
                    .annotate(
                        matches_played=Count("match_id"),
                        total_kills=Coalesce(Sum("kills"), 0),
                        total_booyah=Coalesce(Sum(
                            Case(
                                When(placement=1, then=Value(1)),
                                default=Value(0),
                                output_field=IntegerField()
                            )
                        ), 0),

                        placement_sum=Coalesce(Sum("placement_points"), 0),
                        kill_sum=Coalesce(Sum("kill_points"), 0),
                        bonus_sum=Coalesce(Sum("bonus_points"), 0),
                        penalty_sum=Coalesce(Sum("penalty_points"), 0),

                        total_points=Coalesce(Sum("total_points"), 0),

                        effective_total=(
                            Coalesce(Sum("placement_points"), 0) +
                            Coalesce(Sum("kill_points"), 0) +
                            Coalesce(Sum("bonus_points"), 0) -
                            Coalesce(Sum("penalty_points"), 0)
                        ),

                        last_match_placement=Coalesce(last_placement_subq, Value(999), output_field=IntegerField()),
                    )
                    .order_by(
                        "-effective_total",
                        "-total_booyah",
                        "-total_kills",
                        "last_match_placement",
                        "competitor__user__username",
                    )
                )

            else:
                last_placement_subq = Subquery(
                    TournamentTeamMatchStats.objects
                    .filter(match__group=group, tournament_team_id=OuterRef("tournament_team_id"))
                    .order_by("-match__match_number")
                    .values("placement")[:1]
                )

                overall = (
                    TournamentTeamMatchStats.objects
                    .filter(match__group=group)
                    .values(
                        "tournament_team_id",
                        team_name=F("tournament_team__team__team_name"),
                    )
                    .annotate(
                        matches_played=Count("match_id"),
                        total_kills=Coalesce(Sum("kills"), 0),
                        total_booyah=Coalesce(Sum(
                            Case(
                                When(placement=1, then=Value(1)),
                                default=Value(0),
                                output_field=IntegerField()
                            )
                        ), 0),

                        placement_sum=Coalesce(Sum("placement_points"), 0),
                        kill_sum=Coalesce(Sum("kill_points"), 0),
                        bonus_sum=Coalesce(Sum("bonus_points"), 0),
                        penalty_sum=Coalesce(Sum("penalty_points"), 0),

                        total_points=Coalesce(Sum("total_points"), 0),

                        effective_total=(
                            Coalesce(Sum("placement_points"), 0) +
                            Coalesce(Sum("kill_points"), 0) +
                            Coalesce(Sum("bonus_points"), 0) -
                            Coalesce(Sum("penalty_points"), 0)
                        ),

                        last_match_placement=Coalesce(
                            last_placement_subq,
                            Value(999),
                            output_field=IntegerField()
                        ),
                    )
                    .order_by(
                        "-effective_total",
                        "-total_booyah",
                        "-total_kills",
                        "last_match_placement",
                        "team_name",
                    )
                )

            # ── Scoring-modes overlay (sub-project A): applied ON READ, per lobby. ──
            # `overall` is a QuerySet of dict rows; materialize it so we can mutate each row
            # (add carry-over, re-sort, flag the champion). The id key is competitor_id for
            # solo and tournament_team_id for team — both already present in the rows above.
            overall = list(overall)
            id_key = "competitor_id" if event.participant_type == "solo" else "tournament_team_id"
            # Name tiebreaker key matches the DB .order_by() tail for each branch:
            # team rows expose the `team_name` alias, solo rows `competitor__user__username`.
            name_key = "competitor__user__username" if event.participant_type == "solo" else "team_name"

            # (1) Point-Rush carry-over: seed each competitor's running total with the bonus
            # banked from any earlier stage whose point_rush_target_stage is THIS stage. We add
            # it into effective_total so the standings (and the champion replay below) see it.
            carry_over = _carry_over_for_stage(stage, event.participant_type)
            carry_over_changed_order = False  # did any nonzero bonus land? -> standings may shift
            for row in overall:
                bonus = carry_over.get(row[id_key], 0)
                row["carry_over_points"] = bonus
                row["effective_total"] = int(row.get("effective_total", 0)) + bonus
                if bonus:
                    carry_over_changed_order = True

            # (2) Champion-Point: replay this lobby's matches in play order (matches_payload is
            # already match_number-ordered) through the pure win-rule helper. A team is champion
            # the first time it Booyahs while its running total ENTERING that match was already
            # at/over the threshold. carry_over counts toward the threshold (head start).
            champion_id = None
            stage_is_decided = False
            if stage.champion_point_enabled and stage.champion_point_threshold:
                replay = [
                    {"rows": [
                        {"id": s[id_key], "placement": s["placement"], "points": s["effective_total"]}
                        for s in m["stats"]
                    ]}
                    for m in matches_payload
                ]
                champion_id = scoring_lib.champion_for_group(
                    replay, int(stage.champion_point_threshold), carry_over=dict(carry_over),
                )
                stage_is_decided = champion_id is not None

            # (3) Re-sort ONLY when a scoring-mode overlay actually changed something — i.e. a
            # nonzero carry-over bonus landed, or a champion was crowned. On a plain stage (both
            # toggles off, no carry-over) the DB `.order_by()` above is already authoritative and
            # we MUST NOT touch it: re-sorting here would drop the DB's full tiebreaker chain
            # (-total_booyah, last_match_placement, name) and silently reorder ties, which flips
            # the FE's positional qualified/eliminated badges.
            #
            # When we do re-sort, replicate that exact chain on top of the carry-over-adjusted
            # effective_total so two competitors level on points break the same way the DB would:
            #   -effective_total, -total_booyah, -total_kills, last_match_placement, name.
            if carry_over_changed_order or champion_id is not None:
                overall.sort(key=lambda r: (
                    -int(r.get("effective_total", 0)),
                    -int(r.get("total_booyah", 0)),
                    -int(r.get("total_kills", 0)),
                    int(r.get("last_match_placement", 999)),
                    r.get(name_key) or "",
                ))
            # Pin the champion to #0 if one exists — the FE renders server order verbatim
            # (qualified/eliminated badges are positional), so the champion must physically lead
            # the list, not just be flagged. Stable sort preserves the tiebreaker order above for
            # everyone else.
            if champion_id is not None:
                overall.sort(key=lambda r: 0 if r[id_key] == champion_id else 1)
            for r in overall:
                r["is_champion"] = (champion_id is not None and r[id_key] == champion_id)

            groups_payload.append({
                "group_id": group.group_id,
                "group_name": group.group_name,
                "teams_qualifying": group.teams_qualifying,
                "match_count": group.match_count,
                "match_maps": group.match_maps,
                "leaderboard": None if not leaderboard else {
                    "leaderboard_id": leaderboard.leaderboard_id,
                    "leaderboard_name": leaderboard.leaderboard_name,
                    "kill_point": leaderboard.kill_point,
                    "placement_points": leaderboard.placement_points,
                    "default_scoring": None,  # scoring now stored per match
                    "leaderboard_method": leaderboard.leaderboard_method,
                    "file_type": leaderboard.file_type,
                    "last_updated": leaderboard.last_updated,
                },
                "matches": matches_payload,
                "overall_leaderboard": overall,
                # ── Scoring-modes flags consumed by the results UI (crown + decided banner) ──
                "champion_point_enabled": stage.champion_point_enabled,
                "champion_point_threshold": stage.champion_point_threshold,
                "is_decided": stage_is_decided,
                "champion_id": champion_id,
            })

        stages_payload.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "stage_format": stage.stage_format,
            "stage_status": stage.stage_status,
            "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
            "groups": groups_payload
        })

    return Response({
        "event_id": event.event_id,
        "event_name": event.event_name,
        "participant_type": event.participant_type,
        "stages": stages_payload
    }, status=200)


# ──────────────────────────────────────────────────────────────────────────────
# get_event_group_rosters  (event group-roster view)
# ──────────────────────────────────────────────────────────────────────────────
# PURPOSE
#   Live-event seeding check: for one event, return the full structural tree
#   stage -> group -> teams (and their per-event rosters) -> players, or for solo
#   events stage -> group -> players. Lets an organizer or AFC admin confirm WHO is
#   in WHICH group while a tournament is running. This is a pure READ; it never
#   touches match stats or standings (that is get_all_leaderboard_details_for_event).
#
# REQUEST  (POST body, JSON)
#   { "event_id": <int> }   preferred, OR
#   { "slug": "<event-slug>" }   fallback (event_id wins when both are sent).
#   Authorization header: "Bearer <session token>".
#
# RESPONSE  (200)
#   {
#     event_id, event_name, participant_type, is_solo,
#     stages: [
#       { stage_id, stage_name, stage_format, stage_status, groups: [
#           # TEAM events (duo/squad):
#           { group_id, group_name, teams_qualifying?, team_count, player_count,
#             total_in_group, teams: [
#               { tournament_team_id, team_id, team_name, team_tag,
#                 competitor_status, players: [
#                   { user_id, username (the IGN), uid, full_name, status } ] } ] }
#           # SOLO events:
#           { group_id, group_name, teams_qualifying?, team_count(=0),
#             player_count, total_in_group, players: [
#               { user_id, username (IGN), uid, full_name, status,
#                 competitor_status } ] }
#       ] }
#     ]
#   }
#   An empty/unseeded stage or group is still emitted (teams: [] / players: []) so
#   the FE can render a "not yet seeded" state instead of a missing section.
#
# AUTH
#   AFC event admin (_is_event_admin) OR an org member holding
#   can_manage_registrations on the event's owning org (org_can_event). Native AFC
#   events (organization=None) stay admin-only via org_can_event's own rule. We use
#   _is_event_admin / org_can_event on purpose, NOT the buggy
#   `userroles.filter(role_name__in=...)` inline pattern found elsewhere in this file.
#
# CONSUMED BY  (frontend surfaces)
#   - Organizer page: app/(organizer)/organizer/events/[slug]/groups/page.tsx
#     (POSTs { slug }), gated client-side on can_manage_registrations || isOwner.
#   - Admin tab:      app/(a)/a/events/[slug]/page.tsx -> "Group Rosters" tab
#     (POSTs { event_id }).
#
# DATA MODEL NOTES (why the branches exist)
#   - participant_type "solo" => players come from RegisteredCompetitors via the
#     StageGroupCompetitor.player FK; "duo"/"squad" => teams come via the
#     StageGroupCompetitor.tournament_team FK, and each team's per-event roster is
#     TournamentTeamMember (NOT afc_team.TeamMembers, which carries roles we must not
#     leak here). A StageGroupCompetitor row sets exactly one of those two FKs.
#   - ROUND-ROBIN edge: a round-robin stage stores its team->group mapping on
#     RoundRobinGroup.teams (M2M), NOT on StageGroupCompetitor, so a
#     StageGroupCompetitor-only query returns EMPTY for an RR stage. We branch on
#     stage.round_robin_groups.exists() and read the base groups instead. RR groups
#     have no per-competitor status, so competitor_status defaults to "active".
#
# PERFORMANCE
#   The naive shape is group x team x member (an N+1 per member). We avoid it by
#   batching ALL of the event's TournamentTeamMember rows into one query, grouped
#   into a {tournament_team_id: [member_dict, ...]} map BEFORE the stage/group loops,
#   and by select_related on the per-group StageGroupCompetitor query (and on the RR
#   M2M) so team/player rows carry their joined Team / User in the same query.
@api_view(["POST"])
def get_event_group_rosters(request):
    # ── AUTH: identical preamble to get_all_leaderboard_details_for_event ──
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # Resolve the event from EITHER event_id (preferred) OR slug (fallback). event_id
    # wins when both are present, matching how the organizer FE sends slug while the
    # admin FE sends event_id.
    event_id = request.data.get("event_id")
    slug = request.data.get("slug")
    if event_id:
        event = get_object_or_404(Event, event_id=event_id)
    elif slug:
        event = get_object_or_404(Event, slug=slug)
    else:
        return Response({"message": "event_id or slug is required."}, status=400)

    # ── AUTH gate (event-scoped, read): AFC event admins always pass; otherwise the
    # caller must hold can_manage_registrations on the event's owning org. org_can_event
    # treats native (org=None) events as admin-only, so org members never see foreign
    # events. We deliberately reuse _is_event_admin / org_can_event (single source of
    # truth) rather than re-reading role rows inline. ──
    if not _is_event_admin(admin) and not org_can_event(admin, "can_manage_registrations", event):
        return Response(
            {"message": "You do not have permission to view rosters for this event."},
            status=403,
        )

    is_solo = (event.participant_type == "solo")

    # ── PREFETCH: batch every per-event team roster up front, ONE query for the whole
    # event, grouped by tournament_team_id. This is what keeps the loops from doing a
    # query per team. Only meaningful for team events; harmless (empty) for solo. We
    # carry only the roster fields TournamentTeamMember actually has (it has NO
    # management_role / in_game_role — those live on afc_team.TeamMembers and must not
    # be exposed here). username IS the in-game name (User.in_game_name is commented
    # out, USERNAME_FIELD == "username"). ──
    members_by_team = defaultdict(list)
    if not is_solo:
        all_members = (
            TournamentTeamMember.objects
            .filter(event=event)
            .select_related("user")
        )
        for m in all_members:
            members_by_team[m.tournament_team_id].append({
                "user_id": m.user.user_id,
                "username": m.user.username,   # the IGN
                "uid": m.user.uid,             # nullable game UID
                "full_name": m.user.full_name,
                "status": m.status,            # member status on TournamentTeamMember
            })

    # Small helper: build a team's payload dict from a TournamentTeam + its competitor
    # status. Shared by the StageGroupCompetitor branch (real per-competitor status)
    # and the round-robin branch (status defaults "active", RR M2M has none).
    def _team_payload(tournament_team, competitor_status):
        team = tournament_team.team
        return {
            "tournament_team_id": tournament_team.tournament_team_id,
            "team_id": team.team_id,
            "team_name": team.team_name,
            "team_tag": team.team_tag,
            "competitor_status": competitor_status,
            "players": members_by_team.get(tournament_team.tournament_team_id, []),
        }

    stages_payload = []
    # Order stages deterministically (creation order) so the FE renders Stage 1..N.
    for stage in event.stages.all().order_by("stage_id"):
        groups_payload = []

        # ── ROUND-ROBIN branch ──
        # An RR stage maps teams to base groups on RoundRobinGroup.teams (M2M), so the
        # StageGroupCompetitor table is empty for it. Read the base groups instead.
        # RR is a team-only format, so there is no solo sub-branch here.
        if stage.round_robin_groups.exists():
            # prefetch_related on the teams M2M (and the joined Team) so each rr.teams
            # iteration does not re-hit the DB.
            rr_groups = (
                stage.round_robin_groups
                .all()
                .order_by("order")
                .prefetch_related("teams__team")
            )
            for rr in rr_groups:
                teams_payload = [
                    # RR M2M has no per-competitor status -> default "active".
                    _team_payload(tt, "active")
                    for tt in rr.teams.all()
                ]
                team_count = len(teams_payload)
                player_count = sum(len(t["players"]) for t in teams_payload)
                groups_payload.append({
                    "group_id": rr.group_id,
                    "group_name": rr.label,          # RR groups use `label` (A/B/C)
                    "teams_qualifying": None,        # RR base groups carry no per-group quota
                    "team_count": team_count,
                    "player_count": player_count,
                    "total_in_group": team_count,
                    "teams": teams_payload,
                })

            stages_payload.append({
                "stage_id": stage.stage_id,
                "stage_name": stage.stage_name,
                "stage_format": stage.stage_format,
                "stage_status": stage.stage_status,
                "groups": groups_payload,
            })
            continue  # RR stage fully handled; skip the StageGroups path below.

        # ── STANDARD branch (non-RR): groups are StageGroups rows ──
        for group in stage.groups.all().order_by("group_id"):

            if is_solo:
                # ── SOLO sub-branch: competitors carry a `player` FK to
                # RegisteredCompetitors; the actual user is player.user. ──
                competitors = (
                    StageGroupCompetitor.objects
                    .filter(stage_group=group, player__isnull=False)
                    .select_related("player__user")
                )
                players_payload = []
                for c in competitors:
                    # Skip malformed rows where the expected slot is null.
                    if c.player is None or c.player.user is None:
                        continue
                    u = c.player.user
                    players_payload.append({
                        "user_id": u.user_id,
                        "username": u.username,        # the IGN
                        "uid": u.uid,                  # nullable game UID
                        "full_name": u.full_name,
                        "status": c.player.status,         # RegisteredCompetitors.status
                        "competitor_status": c.status,     # StageGroupCompetitor.status
                    })
                groups_payload.append({
                    "group_id": group.group_id,
                    "group_name": group.group_name,
                    "teams_qualifying": group.teams_qualifying,
                    "team_count": 0,
                    "player_count": len(players_payload),
                    "total_in_group": len(players_payload),
                    "players": players_payload,
                })

            else:
                # ── TEAM sub-branch (duo/squad): competitors carry a `tournament_team`
                # FK; the roster comes from the prefetched members_by_team map. ──
                competitors = (
                    StageGroupCompetitor.objects
                    .filter(stage_group=group, tournament_team__isnull=False)
                    .select_related("tournament_team__team")
                )
                teams_payload = []
                for c in competitors:
                    # Skip malformed rows where the expected slot is null.
                    if c.tournament_team is None or c.tournament_team.team is None:
                        continue
                    teams_payload.append(_team_payload(c.tournament_team, c.status))
                team_count = len(teams_payload)
                player_count = sum(len(t["players"]) for t in teams_payload)
                groups_payload.append({
                    "group_id": group.group_id,
                    "group_name": group.group_name,
                    "teams_qualifying": group.teams_qualifying,
                    "team_count": team_count,
                    "player_count": player_count,
                    "total_in_group": team_count,
                    "teams": teams_payload,
                })

        stages_payload.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "stage_format": stage.stage_format,
            "stage_status": stage.stage_status,
            "groups": groups_payload,
        })

    return Response({
        "event_id": event.event_id,
        "event_name": event.event_name,
        "participant_type": event.participant_type,
        "is_solo": is_solo,
        "stages": stages_payload,
    }, status=200)


# ── BR Round-Robin (sub-project B, Task 3): three-view standings endpoint ──
# A round-robin stage's results read three ways off the SAME match stats:
#   • per-lobby   — already served by get_all_leaderboard_details_for_event (a lobby is a
#                   StageGroups row), so we do NOT duplicate it here;
#   • per-day     — round_robin.day_standings(stage, day): sum one game day's lobbies;
#   • cumulative  — round_robin.cumulative_standings(stage): sum the whole stage, the
#                   round-robin table the format is built around.
# This endpoint bundles the per-day + cumulative tables plus the structural blocks the UI
# needs to render the toggle (`groups` = base A/B/C identity, `game_days` = which lobbies
# fall on which day). Admin-gated like the other event-results endpoints.
@api_view(["POST"])
def get_round_robin_standings(request):
    # round_robin holds the pure aggregators; imported locally to keep the module-level
    # import list untouched (this file imports per-function throughout).
    from . import round_robin

    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # Results are admin-facing → gate on _is_event_admin (which uses the correct
    # role__role_name__in path; NEVER role_name__in, which FieldErrors).
    if not _is_event_admin(user):
        return Response({"message": "You do not have permission."}, status=403)

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    if not event_id or not stage_id:
        return Response({"message": "event_id and stage_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    # Scope the stage to the event so a mismatched pair can't read another event's stage.
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

    # (1) Base-group structure (A/B/C…). RoundRobinGroup has Meta.ordering = ["order"], so
    # `round_robin_groups.all()` is already A→B→C; we just echo label + member team names.
    groups_payload = [
        {
            "label": grp.label,
            "team_names": list(
                grp.teams.values_list("team__team_name", flat=True)
            ),
        }
        for grp in stage.round_robin_groups.all()
    ]

    # (2) Game-day map: each day → the lobby (StageGroups) ids that fall on it. A day can
    # hold MULTIPLE lobbies (multiple group-merges per day), so we bucket by game_day.
    # Only round-robin lobbies carry a game_day; exclude the nulls defensively.
    game_days = {}
    day_lobbies = (
        stage.groups.filter(game_day__isnull=False).order_by("game_day", "group_id")
    )
    for lobby in day_lobbies:
        game_days.setdefault(lobby.game_day, []).append(lobby.group_id)
    game_days_payload = [
        {"day": day, "lobbies": lobbies}
        for day, lobbies in sorted(game_days.items())
    ]

    # (3) Per-day standings: one summed table per game day (each filters to that day only).
    per_day_payload = {
        day: round_robin.day_standings(stage, day) for day in game_days.keys()
    }

    # (4) Cumulative standings: the whole-stage round-robin table (teams summed across
    # every lobby they played).
    cumulative_payload = round_robin.cumulative_standings(stage)

    return Response({
        "groups": groups_payload,
        "game_days": game_days_payload,
        "per_day": per_day_payload,
        "cumulative": cumulative_payload,
    }, status=200)


# @api_view(["POST"])
# def get_all_leaderboard_details_for_event(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     stages_payload = []
#     stages = event.stages.all().order_by("stage_id")

#     for stage in stages:
#         groups_payload = []
#         groups = stage.groups.all().order_by("group_id")

#         for group in groups:
#             leaderboard = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()
#             matches = Match.objects.filter(group=group).order_by("match_number")

#             matches_payload = []
#             for match in matches:
#                 if event.participant_type == "solo":
#                     match_stats = (
#                         SoloPlayerMatchStats.objects
#                         .filter(match=match)
#                         .select_related("competitor__user")
#                         .annotate(
#                             username=F("competitor__user__username"),
#                             effective_total=(
#                                 F("placement_points") + F("kill_points") +
#                                 Coalesce(F("bonus_points"), Value(0), output_field=IntegerField()) -
#                                 Coalesce(F("penalty_points"), Value(0), output_field=IntegerField())
#                             )
#                         )
#                         .values(
#                             "competitor_id",
#                             "username",
#                             "placement",
#                             "kills",
#                             "placement_points",
#                             "kill_points",
#                             "bonus_points",
#                             "penalty_points",
#                             "total_points",
#                             "effective_total",
#                         )
#                         .order_by("-effective_total", "-kills", "username")
#                     )
#                 else:
#                     match_stats = (
#                         TournamentTeamMatchStats.objects
#                         .filter(match=match)
#                         .select_related("tournament_team__team")
#                         .annotate(
#                             team_name=F("tournament_team__team__team_name"),
#                         )
#                         .values(
#                             "tournament_team_id",
#                             "team_name",
#                             "placement",
#                             "kills",
#                             "placement_points",
#                             "kill_points",
#                             "total_points",
#                         )
#                         .order_by("-total_points", "-kills", "team_name")
#                     )

#                 matches_payload.append({
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "result_inputted": match.result_inputted,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "stats": list(match_stats),
#                 })

#             # ✅ overall leaderboard PER GROUP (not whole event)
#             if event.participant_type == "solo":
#                 overall = (
#                     SoloPlayerMatchStats.objects
#                     .filter(match__group=group)
#                     .select_related("competitor__user")
#                     .values(
#                         "competitor_id",
#                         "competitor__user__username",
#                     )
#                     .annotate(
#                         matches_played=Count("match_id"),
#                         total_kills=Coalesce(Sum("kills"), 0),
#                         bonus_sum=Coalesce(Sum("bonus_points"), 0),
#                         penalty_sum=Coalesce(Sum("penalty_points"), 0),
#                         total_points=Coalesce(Sum("total_points"), 0),
#                         effective_total=(
#                             Coalesce(Sum("placement_points"), 0) +
#                             Coalesce(Sum("kill_points"), 0) +
#                             Coalesce(Sum("bonus_points"), 0) -
#                             Coalesce(Sum("penalty_points"), 0)
#                         )
#                     )
#                     .order_by("-effective_total", "-total_kills", "competitor__user__username")
#                 )
#             else:
#                 overall = (
#                     TournamentTeamMatchStats.objects
#                     .filter(match__group=group)
#                     .values(
#                         "tournament_team_id",
#                         team_name=F("tournament_team__team__team_name"),
#                     )
#                     .annotate(
#                         matches_played=Count("match_id"),
#                         total_kills=Coalesce(Sum("kills"), 0),
#                         total_points=Coalesce(Sum("total_points"), 0),
#                     )
#                     .order_by("-total_points", "-total_kills", "team_name")
#                 )

#             groups_payload.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "teams_qualifying": group.teams_qualifying,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": None if not leaderboard else {
#                     "leaderboard_id": leaderboard.leaderboard_id,
#                     "leaderboard_name": leaderboard.leaderboard_name,
#                     "kill_point": leaderboard.kill_point,
#                     "placement_points": leaderboard.placement_points,
#                     "leaderboard_method": leaderboard.leaderboard_method,
#                     "file_type": leaderboard.file_type,
#                     "last_updated": leaderboard.last_updated,
#                 },
#                 "matches": matches_payload,
#                 "overall_leaderboard": list(overall),
#             })

#         stages_payload.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": groups_payload
#         })

#     return Response({
#         "event_id": event.event_id,
#         "event_name": event.event_name,
#         "participant_type": event.participant_type,
#         "stages": stages_payload
#     }, status=200)




import uuid
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

def _get_group_leaderboard(event, stage, group):
    return Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

# @api_view(["POST"])
# def advance_group_competitors_to_next_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=401)

#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")
#     next_stage_id = request.data.get("next_stage_id")
#     remove_old_group_role = bool(request.data.get("remove_old_group_role", False))

#     if not event_id or not group_id or not next_stage_id:
#         return Response({"message": "event_id, group_id, next_stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     group = get_object_or_404(StageGroups, group_id=group_id)
#     current_stage = group.stage

#     if current_stage.event_id != event.event_id:
#         return Response({"message": "This group does not belong to the provided event."}, status=400)

#     next_stage = get_object_or_404(Stages, stage_id=next_stage_id, event=event)

#     matches = list(group.matches.all().order_by("match_number"))
#     if not matches:
#         return Response({"message": "No matches found for this group."}, status=400)

#     # Validate results uploaded for ALL matches
#     if event.participant_type == "solo":
#         missing_matches = []
#         for m in matches:
#             if not SoloPlayerMatchStats.objects.filter(match=m).exists():
#                 missing_matches.append(m.match_number)
#         if missing_matches:
#             return Response({
#                 "message": "Cannot advance. Some matches have no uploaded results.",
#                 "missing_match_numbers": missing_matches
#             }, status=400)
#     else:
#         missing_matches = []
#         for m in matches:
#             if not TournamentTeamMatchStats.objects.filter(match=m).exists():
#                 missing_matches.append(m.match_number)
#         if missing_matches:
#             return Response({
#                 "message": "Cannot advance. Some matches have no uploaded results.",
#                 "missing_match_numbers": missing_matches
#             }, status=400)

#     leaderboard = _get_group_leaderboard(event, current_stage, group)
#     if not leaderboard:
#         return Response({"message": "Leaderboard not found for this group."}, status=400)

#     placement_points = _normalize_points_json(leaderboard.placement_points or {})
#     kill_point = float(leaderboard.kill_point or 1.0)

#     # Compute OVERALL standings for this group
#     if event.participant_type == "solo":
#         # Use stored total_points (fast)
#         totals = {}
#         qs = (
#             SoloPlayerMatchStats.objects
#             .select_related("competitor__user", "match")
#             .filter(match__group=group)
#         )
#         for s in qs:
#             cid = s.competitor_id
#             username = s.competitor.user.username if s.competitor and s.competitor.user else "Unknown"
#             uid = s.competitor.user.uid if s.competitor and s.competitor.user else None

#             total_pts = int(getattr(s, "total_points", 0) or 0)
#             kills = int(s.kills or 0)

#             if cid not in totals:
#                 totals[cid] = {
#                     "competitor": s.competitor,
#                     "competitor_id": cid,
#                     "username": username,
#                     "uid": uid,
#                     "total_points": 0,
#                     "total_kills": 0,
#                 }
#             totals[cid]["total_points"] += total_pts
#             totals[cid]["total_kills"] += kills

#         ranked = list(totals.values())
#         ranked.sort(key=lambda x: (x["total_points"], x["total_kills"]), reverse=True)

#         qualifying_n = int(group.teams_qualifying or 0)
#         qualified = ranked[:qualifying_n]

#     else:
#         totals = {}
#         qs = (
#             TournamentTeamMatchStats.objects
#             .select_related("tournament_team__team", "match")
#             .filter(match__group=group)
#         )
#         for s in qs:
#             tid = s.tournament_team_id
#             team_name = s.tournament_team.team.team_name if s.tournament_team and s.tournament_team.team else "Unknown Team"

#             placement_pts = int(getattr(s, "placement_points", None) or placement_points.get(int(s.placement), 0))
#             kill_pts = int(getattr(s, "kill_points", None) or (int(s.kills or 0) * kill_point))
#             total_pts = int(getattr(s, "total_points", None) or (placement_pts + kill_pts))

#             if tid not in totals:
#                 totals[tid] = {
#                     "tournament_team": s.tournament_team,
#                     "tournament_team_id": tid,
#                     "team_name": team_name,
#                     "total_points": 0,
#                     "total_kills": 0,
#                 }
#             totals[tid]["total_points"] += total_pts
#             totals[tid]["total_kills"] += int(s.kills or 0)

#         ranked = list(totals.values())
#         ranked.sort(key=lambda x: (x["total_points"], x["total_kills"]), reverse=True)

#         qualifying_n = int(group.teams_qualifying or 0)
#         qualified = ranked[:qualifying_n]

#     if qualifying_n <= 0:
#         return Response({"message": "group.teams_qualifying must be > 0."}, status=400)

#     # Seed into next stage + queue Discord role assignments
#     created_stage_competitors = 0
#     queued_roles = 0

#     with transaction.atomic():
#         # progress row for next-stage role assignment
#         progress = DiscordStageRoleAssignmentProgress.objects.create(
#             stage=next_stage,
#             total=len(qualified),
#             status="running"
#         )

#         for row in qualified:
#             if event.participant_type == "solo":
#                 reg = row["competitor"]

#                 sc, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     player=reg,
#                     defaults={"status": "active"}
#                 )
#                 if created:
#                     created_stage_competitors += 1

#                 user = reg.user if reg else None
#                 if user and user.discord_id and next_stage.stage_discord_role_id:
#                     DiscordRoleAssignment.objects.get_or_create(
#                         user=user,
#                         discord_id=user.discord_id,
#                         role_id=next_stage.stage_discord_role_id,
#                         stage=next_stage,
#                         group=None,
#                         defaults={"status": "pending"}
#                     )
#                     queued_roles += 1

#                 # optionally remove old group role
#                 if remove_old_group_role and user and user.discord_id and group.group_discord_role_id:
#                     remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

#             else:
#                 tteam = row["tournament_team"]

#                 sc, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     tournament_team=tteam,
#                     defaults={"status": "active"}
#                 )
#                 if created:
#                     created_stage_competitors += 1

#                 # assign next stage role to ALL members of the team
#                 members = TournamentTeamMember.objects.select_related("user").filter(tournament_team=tteam)
#                 for tm in members:
#                     user = tm.user
#                     if user and user.discord_id and next_stage.stage_discord_role_id:
#                         DiscordRoleAssignment.objects.get_or_create(
#                             user=user,
#                             discord_id=user.discord_id,
#                             role_id=next_stage.stage_discord_role_id,
#                             stage=next_stage,
#                             group=None,
#                             defaults={"status": "pending"}
#                         )
#                         queued_roles += 1

#                     if remove_old_group_role and user and user.discord_id and group.group_discord_role_id:
#                         remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

#     # kick DB-worker to process queued roles
#     # (this is your safer “100% chance” style because it retries + tracks status in DB)
#     assign_stage_roles_from_db_task.delay(str(progress.id), next_stage.stage_id)

#     return Response({
#         "message": "Advanced group to next stage.",
#         "event_id": event.event_id,
#         "from_group": {"group_id": group.group_id, "group_name": group.group_name},
#         "to_stage": {"stage_id": next_stage.stage_id, "stage_name": next_stage.stage_name},
#         "qualified_count": len(qualified),
#         "created_stage_competitors": created_stage_competitors,
#         "queued_next_stage_role_assignments": queued_roles,
#         "progress_id": str(progress.id),
#     }, status=200)


from django.db import transaction
from django.db.models import Sum, F
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from django.db.models import Q

def get_next_stage(event, current_stage):
    # best: next by start_date
    if current_stage.start_date:
        next_by_date = (Stages.objects
            .filter(event=event, start_date__gt=current_stage.start_date)
            .order_by("start_date", "stage_id")
            .first()
        )
        if next_by_date:
            return next_by_date

        # if none strictly after, maybe same date but later stage (rare)
        next_same_date = (Stages.objects
            .filter(event=event, start_date=current_stage.start_date, stage_id__gt=current_stage.stage_id)
            .order_by("stage_id")
            .first()
        )
        if next_same_date:
            return next_same_date

    # fallback: next by stage_id
    return (Stages.objects
        .filter(event=event, stage_id__gt=current_stage.stage_id)
        .order_by("stage_id")
        .first()
    )


# @api_view(["POST"])
# def advance_group_competitors_to_next_stage(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")
#     # next_stage_id = request.data.get("next_stage_id")

#     if not event_id or not group_id:
#         return Response({"message": "event_id and group_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     group = get_object_or_404(StageGroups, group_id=group_id, stage__event=event)
#     stage = group.stage

#     # 1) ensure all match results uploaded
#     matches = Match.objects.filter(group=group).order_by("match_number")
#     if not matches.exists():
#         return Response({"message": "No matches in this group."}, status=400)

#     not_done = matches.filter(result_inputted=False).count()
#     if not_done > 0:
#         return Response({
#             "message": "Cannot advance yet. Some matches have no results uploaded.",
#             "missing_results_matches_count": not_done
#         }, status=400)

#     # 2) get next stage
#     next_stage = (Stages.objects
#                   .filter(event=event, stage_id__gt=stage.stage_id)
#                   .order_by("stage_id")
#                   .first())
#     # if next_stage_id:
#     #     next_stage = get_object_or_404(Stages, stage_id=next_stage_id, event=event)
#     if not next_stage:
#         return Response({"message": "No next stage found after this stage."}, status=400)

#     qualify_n = int(group.teams_qualifying or 0)
#     if qualify_n <= 0:
#         return Response({"message": "group.teams_qualifying must be > 0."}, status=400)

#     # 3) compute winners
#     if event.participant_type == "solo":
#         overall = (SoloPlayerMatchStats.objects
#                    .filter(match__group=group)
#                    .values("competitor_id")
#                    .annotate(total_points=Sum("total_points"), total_kills=Sum("kills"))
#                    .order_by("-total_points", "-total_kills")[:qualify_n])

#         winner_ids = [row["competitor_id"] for row in overall]  # RegisteredCompetitors IDs
#     else:
#         overall = (TournamentTeamMatchStats.objects
#                    .filter(match__group=group)
#                    .values("tournament_team_id")
#                    .annotate(total_points=Sum("total_points"), total_kills=Sum("kills"))
#                    .order_by("-total_points", "-total_kills")[:qualify_n])

#         winner_ids = [row["tournament_team_id"] for row in overall]  # TournamentTeam IDs

#     if not winner_ids:
#         return Response({"message": "No winners found (no stats?)."}, status=400)

#     # 4) seed into next stage + queue discord roles
#     created_count = 0
#     queued_roles = 0

#     with transaction.atomic():
#         if event.participant_type == "solo":
#             winners = RegisteredCompetitors.objects.select_related("user").filter(id__in=winner_ids)
#             for rc in winners:
#                 obj, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     player=rc,
#                     tournament_team=None,
#                     defaults={"status": "active"}
#                 )
#                 if created:
#                     created_count += 1

#                 if rc.user and rc.user.discord_id and next_stage.stage_discord_role_id:
#                     DiscordRoleAssignment.objects.get_or_create(
#                         user=rc.user,
#                         discord_id=rc.user.discord_id,
#                         role_id=next_stage.stage_discord_role_id,
#                         stage=next_stage,
#                         group=None,
#                         defaults={"status": "pending"}
#                     )
#                     queued_roles += 1
#         else:
#             winners = TournamentTeam.objects.select_related("team").filter(tournament_team_id__in=winner_ids)
#             for tt in winners:
#                 obj, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     tournament_team=tt,
#                     player=None,
#                     defaults={"status": "active"}
#                 )
#                 if created:
#                     created_count += 1

#                 # queue stage role for each team member
#                 members = tt.members.select_related("user").all()
#                 for m in members:
#                     if m.user and m.user.discord_id and next_stage.stage_discord_role_id:
#                         DiscordRoleAssignment.objects.get_or_create(
#                             user=m.user,
#                             discord_id=m.user.discord_id,
#                             role_id=next_stage.stage_discord_role_id,
#                             stage=next_stage,
#                             group=None,
#                             defaults={"status": "pending"}
#                         )
#                         queued_roles += 1

#     # 5) kick off worker batch processing
#     if next_stage.stage_discord_role_id:
#         progress = DiscordStageRoleAssignmentProgress.objects.create(
#             stage=next_stage,
#             total=queued_roles,
#             status="running"
#         )
#         assign_stage_roles_from_db_task.delay(str(progress.id), next_stage.stage_id)

#     return Response({
#         "message": "Advanced winners to next stage.",
#         "from_group": group.group_name,
#         "to_stage": next_stage.stage_name,
#         "qualified": qualify_n,
#         "seeded_into_next_stage": created_count,
#         "discord_roles_queued": queued_roles,
#     }, status=200)


from django.db import transaction
from django.db.models import Sum, F
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


from django.db import transaction
from django.db.models import Sum
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

@api_view(["POST"])
def advance_group_competitors_to_next_stage(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission."}, status=403)

    event_id = request.data.get("event_id")
    group_id = request.data.get("group_id")
    if not event_id or not group_id:
        return Response({"message": "event_id and group_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    group = get_object_or_404(StageGroups, group_id=group_id, stage__event=event)
    stage = group.stage

    # 1) Ensure all match results uploaded
    matches = Match.objects.filter(group=group).order_by("match_number")
    if not matches.exists():
        return Response({"message": "No matches in this group."}, status=400)

    not_done = matches.filter(result_inputted=False).count()
    if not_done > 0:
        return Response({
            "message": "Cannot advance yet. Some matches have no results uploaded.",
            "missing_results_matches_count": not_done,
        }, status=400)

    # 2) Find next stage
    if stage.start_date:
        next_stage = (Stages.objects
                      .filter(event=event, start_date__gt=stage.start_date)
                      .order_by("start_date", "stage_id")
                      .first())
    else:
        next_stage = (Stages.objects
                      .filter(event=event, stage_id__gt=stage.stage_id)
                      .order_by("stage_id")
                      .first())

    if not next_stage:
        return Response({"message": "No next stage found after this stage."}, status=400)

    qualify_n = int(group.teams_qualifying or 0)
    if qualify_n <= 0:
        return Response({"message": "group.teams_qualifying must be > 0."}, status=400)

    created_count = 0
    queued_roles = 0
    already_advanced_count = 0

    # 3) Winners + advance
    with transaction.atomic():
        if event.participant_type == "solo":
            overall = (SoloPlayerMatchStats.objects
                       .filter(match__group=group)
                       .values("competitor_id")
                       .annotate(
                           total_points=Sum("total_points"),
                           total_kills=Sum("kills"),
                       )
                       .order_by("-total_points", "-total_kills")[:qualify_n])

            winner_ids = [row["competitor_id"] for row in overall]
            if not winner_ids:
                return Response({"message": "No winners found (no stats?)."}, status=400)

            already_ids = set(
                StageCompetitor.objects.filter(stage=next_stage, player_id__in=winner_ids)
                .values_list("player_id", flat=True)
            )

            winners = RegisteredCompetitors.objects.select_related("user").filter(id__in=winner_ids)
            to_advance = [rc for rc in winners if rc.id not in already_ids]
            already_advanced_count = len(winner_ids) - len(to_advance)

            for rc in to_advance:
                _, created = StageCompetitor.objects.get_or_create(
                    stage=next_stage,
                    player=rc,
                    tournament_team=None,
                    defaults={"status": "active"},
                )
                if created:
                    created_count += 1

                if rc.user and rc.user.discord_id and next_stage.stage_discord_role_id:
                    DiscordRoleAssignment.objects.get_or_create(
                        user=rc.user,
                        discord_id=rc.user.discord_id,
                        role_id=next_stage.stage_discord_role_id,
                        stage=next_stage,
                        group=None,
                        defaults={"status": "pending"},
                    )
                    queued_roles += 1

        else:
            overall = (TournamentTeamMatchStats.objects
                       .filter(match__group=group)
                       .values("tournament_team_id")
                       .annotate(
                           total_points=Sum("total_points"),
                           total_kills=Sum("kills"),
                       )
                       .order_by("-total_points", "-total_kills")[:qualify_n])

            winner_ids = [row["tournament_team_id"] for row in overall]
            if not winner_ids:
                return Response({"message": "No winners found (no stats?)."}, status=400)

            already_ids = set(
                StageCompetitor.objects.filter(stage=next_stage, tournament_team_id__in=winner_ids)
                .values_list("tournament_team_id", flat=True)
            )

            winners = TournamentTeam.objects.prefetch_related("members__user").filter(
                tournament_team_id__in=winner_ids
            )
            to_advance = [tt for tt in winners if tt.tournament_team_id not in already_ids]
            already_advanced_count = len(winner_ids) - len(to_advance)

            for tt in to_advance:
                _, created = StageCompetitor.objects.get_or_create(
                    stage=next_stage,
                    tournament_team=tt,
                    player=None,
                    defaults={"status": "active"},
                )
                if created:
                    created_count += 1

                if next_stage.stage_discord_role_id:
                    for m in tt.members.all():
                        u = m.user
                        if u and u.discord_id:
                            DiscordRoleAssignment.objects.get_or_create(
                                user=u,
                                discord_id=u.discord_id,
                                role_id=next_stage.stage_discord_role_id,
                                stage=next_stage,
                                group=None,
                                defaults={"status": "pending"},
                            )
                            queued_roles += 1

    # 4) Start worker only if queued
    progress_id = None
    if queued_roles > 0 and next_stage.stage_discord_role_id:
        progress = DiscordStageRoleAssignmentProgress.objects.create(
            stage=next_stage,
            total=queued_roles,
            status="running",
        )
        assign_stage_roles_from_db_task.delay(str(progress.id), next_stage.stage_id)
        progress_id = str(progress.id)

    return Response({
        "message": "Advance complete.",
        "from_group": group.group_name,
        "from_stage": stage.stage_name,
        "to_stage": next_stage.stage_name,
        "qualified_count": qualify_n,
        "newly_advanced": created_count,
        "already_advanced": already_advanced_count,
        "discord_roles_queued": queued_roles,
        "progress_id": progress_id,
    }, status=200)


@api_view(["POST"])
def advance_round_robin(request):
    """Advance teams out of a finished BR Round-Robin stage into the next stage (Task 5).

    A round-robin stage is decided by its CUMULATIVE table (every team summed across every
    lobby it played) — NOT by any one lobby — so it can't reuse
    `advance_group_competitors_to_next_stage`, which advances from a single StageGroups/lobby.
    This is the parallel path for the whole stage:

      • default (overall): take the top `stage.teams_qualifying_from_stage` teams of
        `round_robin.cumulative_standings(stage)` (the same authoritative, server-sorted table
        the standings endpoint returns), then
      • mode="per_group": instead take the top `qualify_per_group` teams of EACH base group
        (RoundRobinGroup), by ranking that group's members within the cumulative table — for
        formats that guarantee per-group qualifiers rather than a single overall cut.

    Either way the winners are seeded into the NEXT stage using the SAME plumbing the existing
    advance uses: `StageCompetitor.get_or_create(stage=next_stage, tournament_team=..., player=None)`
    plus the per-member Discord-role queue, so advancement behaves identically downstream.
    """
    # round_robin holds the cumulative aggregator; imported locally to match this file's
    # per-function import idiom (the standings endpoint imports it the same way).
    from . import round_robin

    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # Admin-gated via _is_event_admin (correct role__role_name__in path; never role_name__in).
    if not _is_event_admin(user):
        return Response({"message": "You do not have permission."}, status=403)

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    if not event_id or not stage_id:
        return Response({"message": "event_id and stage_id are required."}, status=400)

    # mode + qualify_per_group drive per-group vs overall selection.
    mode = request.data.get("mode", "overall")
    if mode not in ("overall", "per_group"):
        return Response({"message": "mode must be 'overall' or 'per_group'."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    # Scope the stage to the event so a mismatched pair can't advance another event's stage.
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
    if stage.stage_format != "br - round robin":
        return Response({"message": "Stage is not a Round-Robin stage."}, status=400)

    # Find the next stage exactly the way the existing advance does (start_date first,
    # falling back to stage_id ordering), so both paths agree on "the next stage".
    if stage.start_date:
        next_stage = (Stages.objects
                      .filter(event=event, start_date__gt=stage.start_date)
                      .order_by("start_date", "stage_id")
                      .first())
    else:
        next_stage = (Stages.objects
                      .filter(event=event, stage_id__gt=stage.stage_id)
                      .order_by("stage_id")
                      .first())

    if not next_stage:
        return Response({"message": "No next stage found after this stage."}, status=400)

    # The cumulative table is the single source of truth for ranking (already server-sorted by
    # effective_total → booyahs → kills → name). Each row carries tournament_team_id.
    cumulative = round_robin.cumulative_standings(stage)
    if not cumulative:
        return Response({"message": "No standings found (no results entered?)."}, status=400)

    # Pick the advancing tournament_team_ids per mode.
    if mode == "per_group":
        # Top-K of EACH base group. K from the payload; default 1 (a single qualifier/group).
        try:
            qualify_per_group = int(request.data.get("qualify_per_group", 1))
        except (TypeError, ValueError):
            return Response({"message": "qualify_per_group must be an integer."}, status=400)
        if qualify_per_group <= 0:
            return Response({"message": "qualify_per_group must be > 0."}, status=400)

        # Cumulative order is authoritative, so a team's rank WITHIN its group is just its
        # position in the cumulative list filtered to that group's members. Walk the
        # cumulative rows once (already sorted) and keep the first K seen per base group.
        winner_ids = []
        for grp in stage.round_robin_groups.all():
            member_ids = set(grp.teams.values_list("tournament_team_id", flat=True))
            taken = 0
            for row in cumulative:
                if taken >= qualify_per_group:
                    break
                if row["tournament_team_id"] in member_ids:
                    winner_ids.append(row["tournament_team_id"])
                    taken += 1
    else:
        # Overall: the top N of the whole-stage cumulative table.
        qualify_n = int(stage.teams_qualifying_from_stage or 0)
        if qualify_n <= 0:
            return Response(
                {"message": "stage.teams_qualifying_from_stage must be > 0."}, status=400)
        winner_ids = [row["tournament_team_id"] for row in cumulative[:qualify_n]]

    if not winner_ids:
        return Response({"message": "No winners selected."}, status=400)

    created_count = 0
    queued_roles = 0
    already_advanced_count = 0

    # Seed the winners into the next stage with the SAME plumbing as the group-level advance.
    with transaction.atomic():
        already_ids = set(
            StageCompetitor.objects.filter(stage=next_stage, tournament_team_id__in=winner_ids)
            .values_list("tournament_team_id", flat=True)
        )

        winners = TournamentTeam.objects.prefetch_related("members__user").filter(
            tournament_team_id__in=winner_ids
        )
        to_advance = [tt for tt in winners if tt.tournament_team_id not in already_ids]
        already_advanced_count = len(set(winner_ids)) - len(to_advance)

        for tt in to_advance:
            _, created = StageCompetitor.objects.get_or_create(
                stage=next_stage,
                tournament_team=tt,
                player=None,
                defaults={"status": "active"},
            )
            if created:
                created_count += 1

            # Queue the next stage's Discord role for every team member (same as the
            # group-level advance), so role assignment carries over unchanged.
            if next_stage.stage_discord_role_id:
                for m in tt.members.all():
                    u = m.user
                    if u and u.discord_id:
                        DiscordRoleAssignment.objects.get_or_create(
                            user=u,
                            discord_id=u.discord_id,
                            role_id=next_stage.stage_discord_role_id,
                            stage=next_stage,
                            group=None,
                            defaults={"status": "pending"},
                        )
                        queued_roles += 1

    # Kick off the role-assignment worker only if anything was queued (mirrors the existing advance).
    progress_id = None
    if queued_roles > 0 and next_stage.stage_discord_role_id:
        progress = DiscordStageRoleAssignmentProgress.objects.create(
            stage=next_stage,
            total=queued_roles,
            status="running",
        )
        assign_stage_roles_from_db_task.delay(str(progress.id), next_stage.stage_id)
        progress_id = str(progress.id)

    return Response({
        "message": "Advance complete.",
        "from_stage": stage.stage_name,
        "to_stage": next_stage.stage_name,
        "mode": mode,
        "qualified_count": len(set(winner_ids)),
        "newly_advanced": created_count,
        "already_advanced": already_advanced_count,
        "discord_roles_queued": queued_roles,
        "progress_id": progress_id,
    }, status=200)


# @api_view(["POST"])
# def advance_group_competitors_to_next_stage(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")

#     if not event_id or not group_id:
#         return Response({"message": "event_id and group_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     group = get_object_or_404(StageGroups, group_id=group_id, stage__event=event)
#     stage = group.stage

#     # 1) Ensure all match results uploaded
#     matches = Match.objects.filter(group=group).order_by("match_number")
#     if not matches.exists():
#         return Response({"message": "No matches in this group."}, status=400)

#     not_done = matches.filter(result_inputted=False).count()
#     if not_done > 0:
#         return Response({
#             "message": "Cannot advance yet. Some matches have no results uploaded.",
#             "missing_results_matches_count": not_done,
#         }, status=400)

#     # 2) Find next stage (best: closest later start_date)
#     # Prefer start_date ordering; fall back to stage_id if start_date missing.
#     if stage.start_date:
#         next_stage = (Stages.objects
#                       .filter(event=event, start_date__gt=stage.start_date)
#                       .order_by("start_date", "stage_id")
#                       .first())
#     else:
#         next_stage = (Stages.objects
#                       .filter(event=event, stage_id__gt=stage.stage_id)
#                       .order_by("stage_id")
#                       .first())

#     if not next_stage:
#         return Response({"message": "No next stage found after this stage."}, status=400)

#     qualify_n = int(group.teams_qualifying or 0)
#     if qualify_n <= 0:
#         return Response({"message": "group.teams_qualifying must be > 0."}, status=400)

#     # 3) Compute winners from this group's overall leaderboard
#     if event.participant_type == "solo":
#         overall = (SoloPlayerMatchStats.objects
#                    .filter(match__group=group)
#                    .values("competitor_id")
#                    .annotate(
#                        total_points=Sum("total_points"),
#                        total_kills=Sum("kills"),
#                    )
#                    .order_by("-total_points", "-total_kills")[:qualify_n])

#         winner_ids = [row["competitor_id"] for row in overall]  # RegisteredCompetitors.id
#         if not winner_ids:
#             return Response({"message": "No winners found (no stats?)."}, status=400)

#         winners_qs = RegisteredCompetitors.objects.select_related("user").filter(id__in=winner_ids)

#         # Already advanced check
#         already_ids = set(StageCompetitor.objects.filter(
#             stage=next_stage,
#             player_id__in=winner_ids,
#         ).values_list("player_id", flat=True))

#         to_advance = [rc for rc in winners_qs if rc.id not in already_ids]
#         already_advanced = [rc.id for rc in winners_qs if rc.id in already_ids]

#         if not to_advance:
#             return Response({
#                 "message": "All qualified competitors are already advanced to the next stage.",
#                 "to_stage": next_stage.stage_name,
#                 "already_advanced_count": len(already_advanced),
#                 "already_advanced_ids": already_advanced[:50],
#             }, status=200)

#         created_count = 0
#         queued_roles = 0

#         with transaction.atomic():
#             for rc in to_advance:
#                 obj, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     player=rc,
#                     tournament_team=None,
#                     defaults={"status": "active"},
#                 )
#                 if created:
#                     created_count += 1

#                 if rc.user and rc.user.discord_id and next_stage.stage_discord_role_id:
#                     DiscordRoleAssignment.objects.get_or_create(
#                         user=rc.user,
#                         discord_id=rc.user.discord_id,
#                         role_id=next_stage.stage_discord_role_id,
#                         stage=next_stage,
#                         group=None,
#                         defaults={"status": "pending"},
#                     )
#                     queued_roles += 1

#     else:
#         overall = (TournamentTeamMatchStats.objects
#                    .filter(match__group=group)
#                    .values("tournament_team_id")
#                    .annotate(
#                        total_points=Sum("total_points"),
#                        total_kills=Sum("kills"),
#                    )
#                    .order_by("-total_points", "-total_kills")[:qualify_n])

#         winner_ids = [row["tournament_team_id"] for row in overall]  # TournamentTeam.tournament_team_id
#         if not winner_ids:
#             return Response({"message": "No winners found (no stats?)."}, status=400)

#         winners_qs = TournamentTeam.objects.prefetch_related("members__user").filter(
#             tournament_team_id__in=winner_ids
#         )

#         already_ids = set(StageCompetitor.objects.filter(
#             stage=next_stage,
#             tournament_team_id__in=winner_ids,
#         ).values_list("tournament_team_id", flat=True))

#         to_advance = [tt for tt in winners_qs if tt.tournament_team_id not in already_ids]
#         already_advanced = [tt.tournament_team_id for tt in winners_qs if tt.tournament_team_id in already_ids]

#         if not to_advance:
#             return Response({
#                 "message": "All qualified teams are already advanced to the next stage.",
#                 "to_stage": next_stage.stage_name,
#                 "already_advanced_count": len(already_advanced),
#                 "already_advanced_team_ids": already_advanced[:50],
#             }, status=200)

#         created_count = 0
#         queued_roles = 0

#         with transaction.atomic():
#             for tt in to_advance:
#                 obj, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     tournament_team=tt,
#                     player=None,
#                     defaults={"status": "active"},
#                 )
#                 if created:
#                     created_count += 1

#                 # Queue stage role for each team member
#                 if next_stage.stage_discord_role_id:
#                     for member in tt.members.all():
#                         u = member.user
#                         if u and u.discord_id:
#                             DiscordRoleAssignment.objects.get_or_create(
#                                 user=u,
#                                 discord_id=u.discord_id,
#                                 role_id=next_stage.stage_discord_role_id,
#                                 stage=next_stage,
#                                 group=None,
#                                 defaults={"status": "pending"},
#                             )
#                             queued_roles += 1

#     # 4) Kick off worker batch processing (only if we queued something)
#     if queued_roles > 0 and next_stage.stage_discord_role_id:
#         progress = DiscordStageRoleAssignmentProgress.objects.create(
#             stage=next_stage,
#             total=queued_roles,
#             status="running",
#         )
#         assign_stage_roles_from_db_task.delay(str(progress.id), next_stage.stage_id)

#     return Response({
#         "message": "Advanced winners to next stage.",
#         "from_group": group.group_name,
#         "from_stage": stage.stage_name,
#         "to_stage": next_stage.stage_name,
#         "qualified": qualify_n,
#         "newly_seeded_into_next_stage": created_count,
#         "already_advanced_count": len(already_advanced),
#         "discord_roles_queued": queued_roles,
#     }, status=200)



@api_view(["POST"])
def edit_leaderboard(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Unauthorized."}, status=403)

    leaderboard_id = request.data.get("leaderboard_id")
    if not leaderboard_id:
        return Response({"message": "leaderboard_id is required."}, status=400)

    lb = get_object_or_404(Leaderboard, leaderboard_id=leaderboard_id)

    # ── AUTH (event-scoped): event derived from the leaderboard's own FK (lb.event). AFC event
    # admins always pass; otherwise allow org members holding can_upload_results on the event's
    # owning org. Native (org=None) events stay admin-only.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", lb.event):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    # optional updates
    new_stage_id = request.data.get("stage_id")
    new_group_id = request.data.get("group_id")
    new_name = request.data.get("leaderboard_name")
    placement_points_raw = request.data.get("placement_points")
    kill_point_raw = request.data.get("kill_point")

    if new_name is not None:
        lb.leaderboard_name = new_name

    if kill_point_raw is not None:
        lb.kill_point = float(kill_point_raw)

    if placement_points_raw is not None:
        if isinstance(placement_points_raw, str):
            placement_points_raw = json.loads(placement_points_raw)
        if not isinstance(placement_points_raw, dict):
            return Response({"message": "placement_points must be a JSON object."}, status=400)
        lb.placement_points = placement_points_raw

    if new_stage_id is not None:
        stage = get_object_or_404(Stages, stage_id=new_stage_id, event=lb.event)
        lb.stage = stage

    if new_group_id is not None:
        group = get_object_or_404(StageGroups, group_id=new_group_id, stage=lb.stage)
        lb.group = group

    # ensure unique_together not violated
    exists = Leaderboard.objects.filter(
        event=lb.event,
        stage=lb.stage,
        group=lb.group
    ).exclude(leaderboard_id=lb.leaderboard_id).exists()
    if exists:
        return Response({"message": "A leaderboard already exists for this event/stage/group."}, status=400)

    lb.save()

    # Optional: link matches in that group to this leaderboard
    if lb.group:
        Match.objects.filter(group=lb.group).update(leaderboard=lb)

    return Response({"message": "Leaderboard updated.", "leaderboard_id": lb.leaderboard_id}, status=200)



@api_view(["POST"])
def edit_solo_match_result(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Unauthorized."}, status=403)

    # NOTE: permission is finalised below, once the owning event is resolved via
    # match_id -> match.group.stage.event — org members with can_upload_results may edit
    # solo results for THEIR org's events.

    match_id = request.data.get("match_id")
    rows = request.data.get("rows")

    if not match_id or not isinstance(rows, list):
        return Response({"message": "match_id and rows(list) are required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)
    if not match.group:
        return Response({"message": "Match must be linked to a group."}, status=400)

    event = match.group.stage.event

    # ── AUTH (event-scoped): event derived via match_id -> match.group.stage.event. AFC event
    # admins always pass; otherwise allow org members holding can_upload_results on the event's
    # owning org. Native (org=None) events stay admin-only.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    if event.participant_type != "solo":
        return Response({"message": "This endpoint is for solo only."}, status=400)

    # leaderboard scoring config
    lb = match.leaderboard or Leaderboard.objects.filter(
        event=event, stage=match.group.stage, group=match.group
    ).first()
    if not lb:
        return Response({"message": "Leaderboard not found for this match."}, status=400)

    placement_points = {int(k): int(v) for k, v in (lb.placement_points or {}).items()}
    if not placement_points:
        placement_points = {1:12,2:9,3:8,4:7,5:6,6:5,7:4,8:3,9:2,10:1}
    kill_point = float(lb.kill_point or 1.0)

    with transaction.atomic():
        for r in rows:
            competitor_id = r.get("competitor_id")
            if not competitor_id:
                continue

            stats = get_object_or_404(SoloPlayerMatchStats, match=match, competitor_id=competitor_id)

            if "placement" in r:
                stats.placement = int(r["placement"])
            if "kills" in r:
                stats.kills = int(r["kills"])

            if "bonus_points" in r:
                stats.bonus_points = int(r["bonus_points"] or 0)
            if "penalty_points" in r:
                stats.penalty_points = int(r["penalty_points"] or 0)

            # Recalc points through the shared solo formula so this edit path can never
            # drift from manual/OCR solo entry (Task 2 — route EVERY live point-calc site
            # through scoring.compute_*). This row is always being re-scored, so played=True.
            #
            # NOTE: this endpoint (unlike the manual-entry solo path) folds bonus/penalty
            # into total_points. compute_solo_points intentionally returns placement+kills
            # only, so we add bonus - penalty back on top here to preserve the exact stored
            # total this endpoint produced before the refactor.
            pts = scoring_lib.compute_solo_points(
                placement_points=placement_points, kill_point=kill_point,
                placement=stats.placement, kills=stats.kills, played=True,
            )
            stats.placement_points = pts["placement_points"]
            stats.kill_points = pts["kill_points"]
            stats.total_points = pts["total_points"] + stats.bonus_points - stats.penalty_points

            stats.save()

        match.result_inputted = True
        match.save(update_fields=["result_inputted"])

    return Response({"message": "Solo match result updated.", "match_id": match.match_id}, status=200)


from django.db import transaction
from django.db.models import OuterRef, Subquery
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

@api_view(["POST"])
def remove_non_nigeria_registered_competitors(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Not authorized."}, status=403)

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    dry_run = str(request.data.get("dry_run", "true")).lower() in ("1", "true", "yes")

    event = Event.objects.filter(event_id=event_id).first()
    if not event:
        return Response({"message": "Event not found."}, status=404)

    # latest country per user
    latest_country = LoginHistory.objects.filter(
        user_id=OuterRef("user_id")
    ).order_by("-created_at").values("country")[:1]

    # event regs that have users
    regs = (RegisteredCompetitors.objects
            .select_related("user")
            .filter(event=event, user__isnull=False, status="registered")
            .annotate(last_country=Subquery(latest_country)))

    to_remove = regs.exclude(last_country__iexact="NG")

    preview = list(to_remove.values("id", "user__user_id", "user__username", "last_country")[:50])

    if dry_run:
        return Response({
            "message": "Dry run only. Set dry_run=false to actually delete.",
            "event_id": event.event_id,
            "would_remove_count": to_remove.count(),
            "preview_first_50": preview
        }, status=200)

    # Actually delete + clean related tables
    reg_ids = list(to_remove.values_list("id", flat=True))
    user_ids = list(to_remove.values_list("user_id", flat=True))

    with transaction.atomic():
        # remove stats linked to those competitors (solo)
        SoloPlayerMatchStats.objects.filter(competitor_id__in=reg_ids).delete()

        # remove stage/group competitor rows
        StageGroupCompetitor.objects.filter(player_id__in=reg_ids).delete()
        StageCompetitor.objects.filter(player_id__in=reg_ids).delete()

        # finally remove registrations
        deleted, _ = RegisteredCompetitors.objects.filter(id__in=reg_ids).delete()

    return Response({
        "message": "Removed non-Nigeria registered competitors (based on latest login country).",
        "event_id": event.event_id,
        "removed_registrations_count": len(reg_ids),
        "preview_first_50": preview
    }, status=200)


from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

@api_view(["POST"])
def delete_match(request):
    # -------------- AUTH --------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, once the owning event is resolved via
    # match_id -> match.group.stage.event — org members with can_upload_results may delete
    # matches for THEIR org's events.

    # -------------- INPUT --------------
    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    force = str(request.data.get("force", "false")).lower() in ("1", "true", "yes")
    renumber = str(request.data.get("renumber", "true")).lower() in ("1", "true", "yes")

    match = get_object_or_404(Match, match_id=match_id)

    # ── AUTH (event-scoped): event derived via match_id -> match.group.stage.event (a match is
    # always linked to a group -> stage -> event). AFC event admins always pass; otherwise allow
    # org members holding can_upload_results on the event's owning org. Native (org=None) events
    # stay admin-only.
    _del_event = match.group.stage.event if match.group else None
    if not _is_event_admin(admin) and not (_del_event and org_can_event(admin, "can_upload_results", _del_event)):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    if match.result_inputted and not force:
        return Response({
            "message": "This match already has results. Pass force=true to delete anyway.",
            "match_id": match.match_id,
        }, status=400)

    group = match.group
    deleted_number = match.match_number

    with transaction.atomic():
        # delete the match (stats will cascade because of FK on_delete=models.CASCADE)
        match.delete()

        # optional: renumber remaining matches in that group to avoid gaps
        if renumber and group:
            # shift down any match_number greater than deleted one
            Match.objects.filter(group=group, match_number__gt=deleted_number).update(
                match_number=F("match_number") - 1
            )

            # keep group.match_count aligned (optional, but recommended)
            if group.match_count and group.match_count > 0:
                group.match_count = max(0, group.match_count - 1)
                group.save(update_fields=["match_count"])

    return Response({
        "message": "Match deleted successfully.",
        "deleted_match_number": deleted_number,
        "group_id": getattr(group, "group_id", None),
        "renumbered": renumber,
        "force": force,
    }, status=200)


@api_view(["POST"])
def edit_match_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized."}, status=403)

    match_id = request.data.get("match_id")
    room_name = request.data.get("room_name")
    room_id = request.data.get("room_id")
    room_password = request.data.get("room_password")

    if not match_id:
        return Response({"message": "match_id is required."}, status=400)
    match = get_object_or_404(Match, match_id=match_id)
    updated_fields = []
    if room_name is not None:
        match.room_name = room_name
        updated_fields.append("room_name")
    if room_id is not None:
        match.room_id = room_id
        updated_fields.append("room_id")
    if room_password is not None:
        match.room_password = room_password
        updated_fields.append("room_password")
    if updated_fields:
        match.save(update_fields=updated_fields)

    return Response({"message": "Match details updated.", "match_id": match.match_id}, status=200)


# @api_view(["POST"])
#  def share_event_link(request):


import json
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

DEFAULT_PLACEMENT = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

def _parse_json_or_value(val, default=None):
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default if default is not None else val
    return val

def _normalize_placement_points(pp):
    if not pp:
        return DEFAULT_PLACEMENT
    if not isinstance(pp, dict):
        raise ValueError("placement_points must be a JSON object/dict")
    # store in DB as strings is fine, but normalize to ints for computation
    return {int(k): int(v) for k, v in pp.items()}

# @api_view(["POST"])
# def create_leaderboard_manually(request):
#     # ---- AUTH (your existing pattern) ----
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     # ---- INPUT ----
#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     group_id = request.data.get("group_id")

#     if not (event_id and stage_id and group_id):
#         return Response({"message": "event_id, stage_id, group_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
#     group = get_object_or_404(StageGroups, group_id=group_id, stage=stage)

#     placement_points_raw = _parse_json_or_value(request.data.get("placement_points"), default=None)
#     kill_point_raw = request.data.get("kill_point", 1.0)

#     try:
#         placement_points = _normalize_placement_points(placement_points_raw)
#     except ValueError as e:
#         return Response({"message": str(e)}, status=400)

#     try:
#         kill_point = float(kill_point_raw)
#     except Exception:
#         return Response({"message": "kill_point must be a number."}, status=400)

#     leaderboard_name = request.data.get("leaderboard_name") or f"{event.event_name} - {stage.stage_name} - {group.group_name}"

#     with transaction.atomic():
#         lb, created = Leaderboard.objects.update_or_create(
#             event=event,
#             stage=stage,
#             group=group,
#             defaults={
#                 "leaderboard_name": leaderboard_name,
#                 "creator": admin,
#                 "placement_points": {str(k): int(v) for k, v in placement_points.items()},  # store as JSON
#                 "kill_point": kill_point,
#                 "leaderboard_method": "manual",
#                 "file_type": None,
#             }
#         )

#         # Ensure matches exist for this group (match_count) and link them to leaderboard
#         match_count = int(group.match_count or 0)
#         if match_count <= 0:
#             return Response({"message": "group.match_count must be > 0 to create matches."}, status=400)

#         existing = {m.match_number: m for m in Match.objects.filter(group=group)}
#         for num in range(1, match_count + 1):
#             if num in existing:
#                 m = existing[num]
#                 if m.leaderboard_id != lb.leaderboard_id:
#                     m.leaderboard = lb
#                     m.save(update_fields=["leaderboard"])
#             else:
#                 Match.objects.create(
#                     leaderboard=lb,
#                     group=group,
#                     match_number=num,
#                     match_map=(group.match_maps[0] if group.match_maps else "bermuda"),
#                 )

#     return Response({
#         "message": "Leaderboard created/updated (manual).",
#         "leaderboard_id": lb.leaderboard_id,
#         "created": created,
#         "event_id": event.event_id,
#         "stage_id": stage.stage_id,
#         "group_id": group.group_id,
#         "kill_point": lb.kill_point,
#         "placement_points": lb.placement_points,
#     }, status=200)


@api_view(["POST"])
def create_leaderboard_manually(request):
    # DEPRECATED (no longer in use). Leaderboards are created AUTOMATICALLY for every
    # group when an event's stages/groups/maps are set up (see create_event ~L1055 and
    # the edit_event group sync ~L2153). The URL route for this view is commented out in
    # urls.py so it is unreachable; the function is retained only to avoid churn. Do not
    # wire it back up without removing the auto-create paths first.
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "No permission"}, status=403)

    # NOTE: permission is finalised below, once the owning event is resolved from event_id —
    # org members with can_upload_results may create leaderboards for THEIR org's events.

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    group_id = request.data.get("group_id")

    if not (event_id and stage_id and group_id):
        return Response({"message": "event_id, stage_id, group_id required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
    group = get_object_or_404(StageGroups, group_id=group_id, stage=stage)

    # ── AUTH (event-scoped): event derived directly from request event_id (stage/group are
    # validated to belong to it). AFC event admins always pass; otherwise allow org members
    # holding can_upload_results on the event's owning org. Native (org=None) events stay admin-only.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    apply_to_all = str(request.data.get("apply_to_all")).lower() == "true"

    placement_points = request.data.get("placement_points") or {}
    placement_points_list = request.data.get("placement_points_list") or []

    kill_point = float(request.data.get("kill_point", 1))
    points_per_assist = float(request.data.get("points_per_assist", 0))
    points_per_1000_damage = float(request.data.get("points_per_1000_damage", 0))

    leaderboard_name = (
        request.data.get("leaderboard_name")
        or f"{event.event_name} - {stage.stage_name} - {group.group_name}"
    )

    with transaction.atomic():

        lb, created = Leaderboard.objects.update_or_create(
            event=event,
            stage=stage,
            group=group,
            defaults={
                "leaderboard_name": leaderboard_name,
                "creator": admin,
                "leaderboard_method": "manual",
                "placement_points": {},  # now unused for scoring
                "kill_point": 0,  # no longer primary scoring
            }
        )

        match_count = int(group.match_count or 0)
        if match_count <= 0:
            return Response({"message": "match_count must be > 0"}, status=400)

        matches = []
        for num in range(1, match_count + 1):
            match, _ = Match.objects.get_or_create(
                group=group,
                match_number=num,
                defaults={
                    "leaderboard": lb,
                    "match_map": group.match_maps[0] if group.match_maps else "bermuda",
                }
            )
            matches.append(match)

        # ---------------- APPLY SINGLE RULESET ----------------
        if apply_to_all:

            scoring = {
                "placement_points": placement_points,
                "kill_point": kill_point,
                "points_per_assist": points_per_assist,
                "points_per_1000_damage": points_per_1000_damage,
            }

            for match in matches:
                match.scoring_settings = scoring
                match.leaderboard = lb
                match.save(update_fields=["scoring_settings", "leaderboard"])

        # ---------------- APPLY PER MATCH RULESET ----------------
        else:

            if len(placement_points_list) != match_count:
                return Response(
                    {"message": "placement_points_list must match match_count"},
                    status=400
                )

            for index, match in enumerate(matches):
                rule = placement_points_list[index]

                scoring = {
                    "placement_points": rule.get("placement_points", {}),
                    "kill_point": float(rule.get("kill_point", 1)),
                    "points_per_assist": float(rule.get("points_per_assist", 0)),
                    "points_per_1000_damage": float(rule.get("points_per_dmg", 0)),
                }

                match.scoring_settings = scoring
                match.leaderboard = lb
                match.save(update_fields=["scoring_settings", "leaderboard"])

        # -------- SEED PLACEHOLDER STATS (all zeros) --------
        # Gives the edit page something to render immediately after creation.
        # Prefer group-specific competitors; fall back to all event-level ones.
        if event.participant_type == "solo":
            group_comps = list(
                StageGroupCompetitor.objects.filter(
                    stage_group=group, player__isnull=False, status="active"
                ).select_related("player")
            )
            competitors = (
                [gc.player for gc in group_comps]
                if group_comps
                else list(RegisteredCompetitors.objects.filter(
                    event=event, status__in=["registered", "approved"]
                ))
            )
            if competitors:
                rows = []
                for match in matches:
                    existing = set(
                        SoloPlayerMatchStats.objects.filter(match=match)
                        .values_list("competitor_id", flat=True)
                    )
                    for comp in competitors:
                        if comp.id not in existing:
                            rows.append(SoloPlayerMatchStats(
                                match=match,
                                competitor=comp,
                                placement=0,
                                kills=0,
                                placement_points=0,
                                kill_points=0,
                                bonus_points=0,
                                penalty_points=0,
                                total_points=0,
                                played=False,
                            ))
                if rows:
                    SoloPlayerMatchStats.objects.bulk_create(
                        rows, batch_size=500, ignore_conflicts=True
                    )
        else:
            # Team event (duo / squad)
            group_team_comps = list(
                StageGroupCompetitor.objects.filter(
                    stage_group=group, tournament_team__isnull=False, status="active"
                ).select_related("tournament_team")
            )
            teams = (
                [gc.tournament_team for gc in group_team_comps]
                if group_team_comps
                else list(TournamentTeam.objects.filter(event=event, status="active"))
            )
            if teams:
                members_by_team = {}
                for m in TournamentTeamMember.objects.filter(
                    tournament_team__in=teams, status="active"
                ).select_related("user"):
                    members_by_team.setdefault(m.tournament_team_id, []).append(m)

                for match in matches:
                    existing_team_ids = set(
                        TournamentTeamMatchStats.objects.filter(match=match)
                        .values_list("tournament_team_id", flat=True)
                    )
                    for team in teams:
                        if team.tournament_team_id in existing_team_ids:
                            continue
                        ts = TournamentTeamMatchStats.objects.create(
                            match=match,
                            tournament_team=team,
                            placement=0,
                            kills=0,
                            damage=0,
                            assists=0,
                            placement_points=0,
                            kill_points=0,
                            bonus_points=0,
                            penalty_points=0,
                            total_points=0,
                            played=False,
                        )
                        team_members = members_by_team.get(team.tournament_team_id, [])
                        if team_members:
                            TournamentPlayerMatchStats.objects.bulk_create([
                                TournamentPlayerMatchStats(
                                    team_stats=ts,
                                    player=member.user,
                                    kills=0,
                                    damage=0,
                                    assists=0,
                                    played=False,
                                )
                                for member in team_members
                            ], batch_size=200, ignore_conflicts=True)

    return Response({
        "message": "Leaderboard created successfully.",
        "leaderboard_id": lb.leaderboard_id,
        "apply_to_all": apply_to_all,
    }, status=200)


from django.db.models import Sum
from rest_framework.decorators import api_view
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.response import Response

def _get_lb_for_match(match: Match) -> Leaderboard | None:
    if match.leaderboard_id:
        return match.leaderboard
    if match.group_id:
        return Leaderboard.objects.filter(
            event=match.group.stage.event,
            stage=match.group.stage,
            group=match.group
        ).first()
    return None



from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(["POST"])
def enter_team_match_result_manual(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, after the owning event is resolved — org members
    # with can_upload_results may enter results for THEIR org's events.

    # ---------------- INPUT ----------------
    match_id = request.data.get("match_id")
    results_payload = _parse_json_or_value(request.data.get("results"), default=None)

    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    if not isinstance(results_payload, list) or not results_payload:
        return Response({"message": "results must be a non-empty list."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)
    lb = _get_lb_for_match(match)
    if not lb:
        return Response({"message": "No leaderboard linked/found for this match."}, status=400)

    event = lb.event

    # ── AUTH (event-scoped): AFC event admins always pass; otherwise allow org members
    # holding can_upload_results on the event's owning org (native events stay admin-only).
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission."}, status=403)

    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for TEAM events only."}, status=400)

    # ---------------- SCORING ----------------
    scoring = match.scoring_settings or {}

    try:
        placement_points = {
            int(k): int(v)
            for k, v in (scoring.get("placement_points") or {}).items()
        }
    except Exception:
        return Response({"message": "Invalid match scoring placement_points."}, status=400)

    kill_point = float(scoring.get("kill_point", 1))
    points_per_assist = float(scoring.get("points_per_assist", 0))
    points_per_1000_damage = float(scoring.get("points_per_1000_damage", 0))

    # ---------------- VALIDATE TEAMS ----------------
    team_ids = [
        int(t.get("tournament_team_id"))
        for t in results_payload
        if t.get("tournament_team_id")
    ]

    teams = TournamentTeam.objects.filter(
        event=event,
        tournament_team_id__in=team_ids
    )

    team_map = {tt.tournament_team_id: tt for tt in teams}

    # ---------------- VALIDATE UNIQUE PLACEMENTS ----------------
    played_rows = [t for t in results_payload if t.get("played", True)]
    placements = [t.get("placement") for t in played_rows]

    if any(p is None for p in placements):
        return Response({"message": "Each played team must have placement."}, status=400)

    if len(set(placements)) != len(placements):
        return Response({"message": "Placements must be unique among played teams."}, status=400)

    with transaction.atomic():

        # Safe re-entry
        TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
        TournamentTeamMatchStats.objects.filter(match=match).delete()

        team_stats_to_create = []

        # ---------------- CREATE TEAM STATS ----------------
        for team_item in results_payload:
            tid = team_item.get("tournament_team_id")
            tt = team_map.get(int(tid)) if tid else None
            if not tt:
                continue

            team_played = bool(team_item.get("played", True))
            placement = int(team_item.get("placement") or 0) if team_played else 0

            players = team_item.get("players") or []
            if not isinstance(players, list):
                players = []

            if not team_played:
                for p in players:
                    p["played"] = False

            played_players = [p for p in players if p.get("played", True)]

            if len(played_players) > 4:
                return Response(
                    {"message": f"Team {tid}: max 4 played players allowed."},
                    status=400
                )

            total_kills = sum(int(p.get("kills") or 0) for p in played_players)
            total_damage = sum(int(p.get("damage") or 0) for p in played_players)
            total_assists = sum(int(p.get("assists") or 0) for p in played_players)

            bonus = int(team_item.get("bonus_points") or 0)
            penalty = int(team_item.get("penalty_points") or 0)

            # Shared team formula — the three point columns now come from scoring (no inline copy).
            pts = scoring_lib.compute_team_points(
                placement_points=placement_points, kill_point=kill_point,
                points_per_assist=points_per_assist, points_per_1000_damage=points_per_1000_damage,
                placement=placement, kills=total_kills, damage=total_damage, assists=total_assists,
                bonus=bonus, penalty=penalty, played=team_played,
            )

            team_stats_to_create.append(
                TournamentTeamMatchStats(
                    match=match,
                    tournament_team_id=tt.tournament_team_id,
                    placement=placement,
                    kills=total_kills,
                    damage=total_damage,
                    assists=total_assists,
                    placement_points=pts["placement_points"],
                    kill_points=pts["kill_points"],
                    bonus_points=bonus,
                    penalty_points=penalty,
                    total_points=pts["total_points"],
                )
            )

        # created_team_stats = TournamentTeamMatchStats.objects.bulk_create(
        #     team_stats_to_create,
        #     batch_size=200
        # )

        # # 🔥 IMPORTANT: use *_id fields only
        # created_map = {
        #     ts.tournament_team_id: ts.team_stats_id
        #     for ts in created_team_stats
        # }

        TournamentTeamMatchStats.objects.bulk_create(
            team_stats_to_create,
            batch_size=200
        )

        # 🔥 Re-fetch from DB to guarantee IDs
        created_stats_qs = TournamentTeamMatchStats.objects.filter(
            match=match,
            tournament_team_id__in=team_ids
        )

        created_map = {
            ts.tournament_team.tournament_team_id: ts.team_stats_id
            for ts in created_stats_qs
        }

        # ---------------- CREATE PLAYER STATS ----------------
        player_rows = []

        for team_item in results_payload:
            tid = team_item.get("tournament_team_id")
            ts_id = created_map.get(int(tid)) if tid else None
            if not ts_id:
                continue

            team_played = bool(team_item.get("played", True))
            players = team_item.get("players") or []

            for p in players:
                user_id = p.get("user_id")
                if not user_id:
                    continue

                played = bool(p.get("played", True)) and team_played
                # user = User.objects.filter(user_id=int(user_id)).first()
                # if not user:
                #     return Response(
                #         {"message": f"User with user_id {user_id} not found."},
                #         status=400
                #     )

                player_rows.append(
                    TournamentPlayerMatchStats(
                        team_stats_id=ts_id,  # ✅ FK safe
                        player_id=user_id,  # ✅ your PK
                        kills=int(p.get("kills") or 0) if played else 0,
                        damage=int(p.get("damage") or 0) if played else 0,
                        assists=int(p.get("assists") or 0) if played else 0,
                    )
                )

        TournamentPlayerMatchStats.objects.bulk_create(
            player_rows,
            batch_size=500
        )

        match.result_inputted = True
        if not match.leaderboard_id:
            match.leaderboard = lb

        match.save(update_fields=["result_inputted", "leaderboard"])

    return Response({
        "message": "Match result saved (manual team entry).",
        "match_id": match.match_id,
        "leaderboard_id": lb.leaderboard_id,
        "teams_saved": len(created_stats_qs),
        "player_rows_saved": len(player_rows),
    }, status=200)

# @api_view(["POST"])
# def enter_team_match_result_manual(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     # ---------------- INPUT ----------------
#     match_id = request.data.get("match_id")
#     teams_payload = _parse_json_or_value(request.data.get("results"), default=None)

#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)

#     if not isinstance(teams_payload, list) or not teams_payload:
#         return Response({"message": "teams must be a non-empty list."}, status=400)

#     match = get_object_or_404(Match, match_id=match_id)
#     lb = _get_lb_for_match(match)
#     if not lb:
#         return Response({"message": "No leaderboard linked/found for this match."}, status=400)

#     event = lb.event
#     if event.participant_type == "solo":
#         return Response({"message": "This endpoint is for TEAM events only."}, status=400)

#     # ---------------- SCORING ----------------
#     scoring = match.scoring_settings or {}

#     try:
#         placement_points = {
#             int(k): int(v)
#             for k, v in (scoring.get("placement_points") or {}).items()
#         }
#     except Exception:
#         return Response({"message": "Invalid match scoring placement_points."}, status=400)

#     kill_point = float(scoring.get("kill_point", 1))
#     points_per_assist = float(scoring.get("points_per_assist", 0))
#     points_per_1000_damage = float(scoring.get("points_per_1000_damage", 0))

#     # ---------------- VALIDATE TEAMS ----------------
#     team_ids = [t.get("tournament_team_id") for t in teams_payload if t.get("tournament_team_id")]

#     tt_map = {
#         tt.tournament_team_id: tt
#         for tt in TournamentTeam.objects
#             .filter(event=event, tournament_team_id__in=team_ids)
#             .prefetch_related("members__user")
#     }

#     # ---------------- VALIDATE UNIQUE PLACEMENTS ----------------
#     played_rows = [t for t in teams_payload if t.get("played", True)]
#     placements = [t.get("placement") for t in played_rows]

#     if any(p is None for p in placements):
#         return Response({"message": "Each played team must have placement."}, status=400)

#     if len(set(placements)) != len(placements):
#         return Response({"message": "Placements must be unique among played teams."}, status=400)

#     stats_rows = []
#     player_rows = []

#     with transaction.atomic():

#         # Make re-entry safe
#         TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
#         TournamentTeamMatchStats.objects.filter(match=match).delete()

#         # ---------------- CREATE TEAM STATS ----------------
#         for team_item in teams_payload:
#             tid = team_item.get("tournament_team_id")
#             tt = tt_map.get(tid)
#             if not tt:
#                 continue

#             team_played = bool(team_item.get("played", True))
#             placement = int(team_item.get("placement") or 0) if team_played else 0

#             players = team_item.get("players") or []
#             if not isinstance(players, list):
#                 players = []

#             if not team_played:
#                 for p in players:
#                     p["played"] = False

#             played_players = [p for p in players if p.get("played", True)]

#             # Enforce max 4 played players (BR safety)
#             if len(played_players) > 4:
#                 return Response(
#                     {"message": f"Team {tid}: max 4 played players allowed."},
#                     status=400
#                 )

#             total_kills = sum(int(p.get("kills") or 0) for p in played_players)
#             total_damage = sum(int(p.get("damage") or 0) for p in played_players)
#             total_assists = sum(int(p.get("assists") or 0) for p in played_players)

#             placement_pts = placement_points.get(placement, 0) if team_played else 0
#             kill_pts = total_kills * kill_point
#             assist_pts = total_assists * points_per_assist
#             damage_pts = (total_damage / 1000) * points_per_1000_damage

#             bonus = int(team_item.get("bonus_points") or 0)
#             penalty = int(team_item.get("penalty_points") or 0)

#             total_pts = placement_pts + kill_pts + assist_pts + damage_pts + bonus - penalty

#             stats_rows.append(TournamentTeamMatchStats(
#                 match=match,
#                 tournament_team=tt,
#                 placement=placement,
#                 kills=total_kills,
#                 damage=total_damage,
#                 assists=total_assists,
#                 placement_points=int(placement_pts),
#                 kill_points=int(kill_pts),
#                 bonus_points=bonus,
#                 penalty_points=penalty,
#                 total_points=int(total_pts),
#             ))

#         created_team_stats = TournamentTeamMatchStats.objects.bulk_create(stats_rows, batch_size=200)

#         # Map for FK linking
#         created_map = {
#             row.tournament_team.tournament_team_id: row.team_stats_id
#             for row in created_team_stats
#         }

#         # ---------------- CREATE PLAYER STATS ----------------
#         for team_item in teams_payload:
#             tid = team_item.get("tournament_team_id")
#             ts_id = created_map.get(tid)
#             if not ts_id:
#                 continue

#             team_played = bool(team_item.get("played", True))
#             players = team_item.get("players") or []

#             for p in players:
#                 user_id = p.get("user_id")
#                 if not user_id:
#                     continue

#                 user = User.objects.get(user_id=user_id)

#                 played = bool(p.get("played", True)) and team_played

#                 player_rows.append(TournamentPlayerMatchStats(
#                     team_stats_id=ts_id,  # 🔥 FK SAFE FIX
#                     player=user,
#                     kills=int(p.get("kills") or 0) if played else 0,
#                     damage=int(p.get("damage") or 0) if played else 0,
#                     assists=int(p.get("assists") or 0) if played else 0,
#                 ))

#         TournamentPlayerMatchStats.objects.bulk_create(player_rows, batch_size=500)

#         match.result_inputted = True
#         if not match.leaderboard.leaderboard_id:
#             match.leaderboard = lb

#         match.save(update_fields=["result_inputted", "leaderboard"])

#     return Response({
#         "message": "Match result saved (manual team entry).",
#         "match_id": match.match_id,
#         "leaderboard_id": lb.leaderboard_id,
#         "teams_saved": len(created_team_stats),
#         "player_rows_saved": len(player_rows),
#     }, status=200)


# @api_view(["POST"])
# def enter_team_match_result_manual(request):
#     # ---- AUTH ----
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     match_id = request.data.get("match_id")
#     teams_payload = _parse_json_or_value(request.data.get("teams"), default=None)

#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)
#     if not isinstance(teams_payload, list) or not teams_payload:
#         return Response({"message": "teams must be a non-empty list."}, status=400)

#     match = get_object_or_404(Match, match_id=match_id)
#     lb = _get_lb_for_match(match)
#     if not lb:
#         return Response({"message": "No leaderboard linked/found for this match."}, status=400)

#     event = lb.event
#     if event.participant_type == "solo":
#         return Response({"message": "This endpoint is for TEAM events only."}, status=400)

#     scoring = match.scoring_settings or {}

#     try:
#         placement_points = {
#             int(k): int(v)
#             for k, v in (scoring.get("placement_points") or {}).items()
#         }
#     except Exception:
#         return Response({"message": "Invalid match scoring placement_points."}, status=400)

#     kill_point = float(scoring.get("kill_point", 1))
#     points_per_assist = float(scoring.get("points_per_assist", 0))
#     points_per_1000_damage = float(scoring.get("points_per_1000_damage", 0))

#     # Validate tournament teams exist for event
#     team_ids = [t.get("tournament_team_id") for t in teams_payload]
#     team_ids = [tid for tid in team_ids if tid is not None]
#     tt_map = {
#         tt.tournament_team_id: tt
#         for tt in TournamentTeam.objects.filter(event=event, tournament_team_id__in=team_ids).prefetch_related("members__user")
#     }

#     stats_rows = []
#     player_rows = []

#     with transaction.atomic():
#         # wipe existing match stats to make re-entry safe
#         TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
#         TournamentTeamMatchStats.objects.filter(match=match).delete()

#         for team_item in teams_payload:
#             tid = team_item.get("tournament_team_id")
#             if tid not in tt_map:
#                 continue

#             team_played = bool(team_item.get("played", True))
#             placement = int(team_item.get("placement") or 0) if team_played else 0

#             # kills come from players who played=True
#             players = team_item.get("players") or []
#             if not isinstance(players, list):
#                 players = []

#             total_kills = 0
#             total_damage = 0
#             total_assists = 0

#             for p in players:
#                 p_played = bool(p.get("played", True)) and team_played
#                 k = int(p.get("kills") or 0) if p_played else 0
#                 d = int(p.get("damage") or 0) if p_played else 0
#                 a = int(p.get("assists") or 0) if p_played else 0
#                 total_kills += k
#                 total_damage += d
#                 total_assists += a

#             placement_pts = placement_points.get(placement, 0) if team_played else 0
#             # kill_pts = int(total_kills * kill_point) if team_played else 0
#             # total_pts = placement_pts + kill_pts

#             kill_pts = total_kills * kill_point
#             assist_pts = total_assists * points_per_assist
#             damage_pts = (total_damage / 1000) * points_per_1000_damage

#             total_pts = placement_pts + kill_pts + assist_pts + damage_pts

#             # team_stat = TournamentTeamMatchStats(
#             #     match=match,
#             #     tournament_team=tt_map[tid],
#             #     placement=placement,
#             #     kills=total_kills,
#             #     damage=total_damage,
#             #     assists=total_assists,
#             #     placement_points=placement_pts,
#             #     kill_points=kill_pts,
#             #     total_points=total_pts,
#             # )

#             team_stat = TournamentTeamMatchStats(
#                 match=match,
#                 tournament_team=tt_map[tid],
#                 placement=placement,
#                 kills=total_kills,
#                 damage=total_damage,
#                 assists=total_assists,
#                 placement_points=placement_pts,
#                 kill_points=int(kill_pts),
#                 total_points=int(total_pts),
#             )
#             stats_rows.append(team_stat)

#         created_team_stats = TournamentTeamMatchStats.objects.bulk_create(stats_rows, batch_size=200)

#         # Build a quick map so we can attach player_stats
#         created_map = {row.tournament_team_id: row for row in created_team_stats}

#         for team_item in teams_payload:
#             tid = team_item.get("tournament_team_id")
#             if tid not in created_map:
#                 continue

#             team_played = bool(team_item.get("played", True))
#             players = team_item.get("players") or []

#             for p in players:
#                 user_id = p.get("user_id")
#                 if not user_id:
#                     continue
#                 p_played = bool(p.get("played", True)) and team_played

#                 player_rows.append(TournamentPlayerMatchStats(
#                     team_stats=created_map[tid],
#                     player_id=int(user_id),
#                     kills=int(p.get("kills") or 0) if p_played else 0,
#                     damage=int(p.get("damage") or 0) if p_played else 0,
#                     assists=int(p.get("assists") or 0) if p_played else 0,
#                 ))

#         TournamentPlayerMatchStats.objects.bulk_create(player_rows, batch_size=500)

#         match.result_inputted = True
#         # keep match linked to leaderboard (important if it wasn’t)
#         if not match.leaderboard_id:
#             match.leaderboard = lb
#         match.save(update_fields=["result_inputted", "leaderboard"])

#     return Response({
#         "message": "Match result saved (manual team entry).",
#         "match_id": match.match_id,
#         "leaderboard_id": lb.leaderboard_id,
#         "teams_saved": len(created_team_stats),
#         "player_rows_saved": len(player_rows),
#     }, status=200)


@api_view(["POST"])
def enter_solo_match_result_manual(request):
    # ---- AUTH ----
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, after the owning event is resolved — org members
    # with can_upload_results may enter results for THEIR org's events.

    match_id = request.data.get("match_id")
    players_payload = _parse_json_or_value(request.data.get("players"), default=None)

    if not match_id:
        return Response({"message": "match_id is required."}, status=400)
    if not isinstance(players_payload, list) or not players_payload:
        return Response({"message": "players must be a non-empty list."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)
    lb = _get_lb_for_match(match)
    if not lb:
        return Response({"message": "No leaderboard linked/found for this match."}, status=400)

    event = lb.event

    # ── AUTH (event-scoped): AFC event admins always pass; otherwise allow org members
    # holding can_upload_results on the event's owning org (native events stay admin-only).
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission."}, status=403)

    if event.participant_type != "solo":
        return Response({"message": "This endpoint is for SOLO events only."}, status=400)

    placement_points = _normalize_placement_points(lb.placement_points or {})
    kill_point = float(lb.kill_point or 1.0)

    comp_ids = [p.get("competitor_id") for p in players_payload if p.get("competitor_id")]
    comp_map = {
        rc.id: rc
        for rc in RegisteredCompetitors.objects.filter(event=event, id__in=comp_ids).select_related("user")
    }

    rows = []
    with transaction.atomic():
        SoloPlayerMatchStats.objects.filter(match=match).delete()

        for p in players_payload:
            cid = p.get("competitor_id")
            if cid not in comp_map:
                continue

            played = bool(p.get("played", True))
            placement = int(p.get("placement") or 0) if played else 0
            kills = int(p.get("kills") or 0) if played else 0

            # Shared solo formula (was the inline placement+kills copy at this line).
            pts = scoring_lib.compute_solo_points(
                placement_points=placement_points, kill_point=kill_point,
                placement=placement, kills=kills, played=played,
            )
            placement_pts = pts["placement_points"]
            kill_pts = pts["kill_points"]
            total_pts = pts["total_points"]

            rows.append(SoloPlayerMatchStats(
                match=match,
                competitor=comp_map[cid],
                placement=placement,
                kills=kills,
                placement_points=placement_pts,
                kill_points=kill_pts,
                total_points=total_pts,
            ))

        SoloPlayerMatchStats.objects.bulk_create(rows, batch_size=500)

        match.result_inputted = True
        if not match.leaderboard_id:
            match.leaderboard = lb
        match.save(update_fields=["result_inputted", "leaderboard"])

    return Response({
        "message": "Solo match result saved (manual entry).",
        "match_id": match.match_id,
        "leaderboard_id": lb.leaderboard_id,
        "players_saved": len(rows),
    }, status=200)


import json
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

def _get_leaderboard_for_match(match):
    # prefer match.leaderboard, else infer from (event, stage, group)
    if match.leaderboard:
        return match.leaderboard
    if not match.group:
        return None
    stage = match.group.stage
    event = stage.event
    return Leaderboard.objects.filter(event=event, stage=stage, group=match.group).first()


@api_view(["POST"])
def edit_match_result(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, once the owning event is resolved via the match's
    # leaderboard (lb.event) — org members with can_upload_results may edit results for THEIR
    # org's events.

    # ---------------- INPUT ----------------
    match_id = request.data.get("match_id")
    results_payload = _parse_json_or_value(request.data.get("results"), default=None)

    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    if not isinstance(results_payload, list) or not results_payload:
        return Response({"message": "results must be a non-empty list."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)

    lb = _get_lb_for_match(match)
    if not lb:
        return Response({"message": "No leaderboard linked/found for this match."}, status=400)

    event = lb.event

    # ── AUTH (event-scoped): event derived via match_id -> _get_lb_for_match(match).event. AFC
    # event admins always pass; otherwise allow org members holding can_upload_results on the
    # event's owning org. Native (org=None) events stay admin-only.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for TEAM events only."}, status=400)

    # ---------------- SCORING ----------------
    scoring = match.scoring_settings or {}

    placement_points = {
        int(k): int(v)
        for k, v in (scoring.get("placement_points") or {}).items()
    }

    kill_point = float(scoring.get("kill_point", 1))
    points_per_assist = float(scoring.get("points_per_assist", 0))
    points_per_1000_damage = float(scoring.get("points_per_1000_damage", 0))

    # ---------------- TEAM VALIDATION ----------------
    team_ids = [
        int(t.get("tournament_team_id"))
        for t in results_payload
        if t.get("tournament_team_id")
    ]

    teams = TournamentTeam.objects.filter(
        event=event,
        tournament_team_id__in=team_ids
    )

    team_map = {tt.tournament_team_id: tt for tt in teams}

    # ---------------- PLACEMENT VALIDATION ----------------
    played_rows = [t for t in results_payload if t.get("played", True)]
    placements = [t.get("placement") for t in played_rows]

    if any(p is None for p in placements):
        return Response({"message": "Each played team must have placement."}, status=400)

    if len(set(placements)) != len(placements):
        return Response({"message": "Placements must be unique among played teams."}, status=400)

    # ---------------- TRANSACTION ----------------
    with transaction.atomic():

        # DELETE OLD RESULTS
        TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
        TournamentTeamMatchStats.objects.filter(match=match).delete()

        team_stats_to_create = []

        # ---------------- CREATE TEAM STATS ----------------
        for team_item in results_payload:

            tid = team_item.get("tournament_team_id")
            tt = team_map.get(int(tid)) if tid else None

            if not tt:
                continue

            team_played = bool(team_item.get("played", True))
            placement = int(team_item.get("placement") or 0) if team_played else 0

            players = team_item.get("players") or []

            if not isinstance(players, list):
                players = []

            if not team_played:
                for p in players:
                    p["played"] = False

            played_players = [p for p in players if p.get("played", True)]

            if len(played_players) > 4:
                return Response(
                    {"message": f"Team {tid}: max 4 played players allowed."},
                    status=400
                )

            total_kills = sum(int(p.get("kills") or 0) for p in played_players)
            total_damage = sum(int(p.get("damage") or 0) for p in played_players)
            total_assists = sum(int(p.get("assists") or 0) for p in played_players)

            bonus = int(team_item.get("bonus_points") or 0)
            penalty = int(team_item.get("penalty_points") or 0)

            # Shared team formula — the three point columns now come from scoring (no inline copy).
            pts = scoring_lib.compute_team_points(
                placement_points=placement_points, kill_point=kill_point,
                points_per_assist=points_per_assist, points_per_1000_damage=points_per_1000_damage,
                placement=placement, kills=total_kills, damage=total_damage, assists=total_assists,
                bonus=bonus, penalty=penalty, played=team_played,
            )

            team_stats_to_create.append(
                TournamentTeamMatchStats(
                    match=match,
                    tournament_team_id=tt.tournament_team_id,
                    placement=placement,
                    kills=total_kills,
                    damage=total_damage,
                    assists=total_assists,
                    placement_points=pts["placement_points"],
                    kill_points=pts["kill_points"],
                    bonus_points=bonus,
                    penalty_points=penalty,
                    total_points=pts["total_points"],
                )
            )

        TournamentTeamMatchStats.objects.bulk_create(team_stats_to_create, batch_size=200)

        # ---------------- FETCH CREATED TEAM STATS ----------------
        created_stats_qs = TournamentTeamMatchStats.objects.filter(
            match=match,
            tournament_team_id__in=team_ids
        )

        created_map = {
            ts.tournament_team_id: ts.team_stats_id
            for ts in created_stats_qs
        }

        # ---------------- CREATE PLAYER STATS ----------------
        player_rows = []

        for team_item in results_payload:

            tid = team_item.get("tournament_team_id")
            ts_id = created_map.get(int(tid)) if tid else None

            if not ts_id:
                continue

            team_played = bool(team_item.get("played", True))
            players = team_item.get("players") or []

            for p in players:

                user_id = p.get("user_id")

                if not user_id:
                    continue

                played = bool(p.get("played", True)) and team_played

                player_rows.append(
                    TournamentPlayerMatchStats(
                        team_stats_id=ts_id,
                        player_id=int(user_id),
                        kills=int(p.get("kills") or 0) if played else 0,
                        damage=int(p.get("damage") or 0) if played else 0,
                        assists=int(p.get("assists") or 0) if played else 0,
                    )
                )

        TournamentPlayerMatchStats.objects.bulk_create(player_rows, batch_size=500)

        match.result_inputted = True

        if not match.leaderboard_id:
            match.leaderboard = lb

        match.save(update_fields=["result_inputted", "leaderboard"])

    return Response({
        "message": "Match result updated successfully.",
        "match_id": match.match_id,
        "leaderboard_id": lb.leaderboard_id,
        "teams_saved": len(created_stats_qs),
        "player_rows_saved": len(player_rows),
    }, status=200)


# @api_view(["POST"])
# def edit_match_result(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=403)

#     # ---------------- INPUT ----------------
#     match_id = request.data.get("match_id")
#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)

#     results = request.data.get("results")

#     if isinstance(results, str):
#         results = json.loads(results)

#     if not isinstance(results, list) or not results:
#         return Response({"message": "results must be a list."}, status=400)

#     match = get_object_or_404(Match, match_id=match_id)

#     if not match.group or not match.group.stage or not match.group.stage.event:
#         return Response({"message": "Match not linked to event."}, status=400)

#     event = match.group.stage.event

#     leaderboard = _get_leaderboard_for_match(match)
#     if not leaderboard:
#         return Response({"message": "Leaderboard not found."}, status=400)

#     # ---------------- SCORING ----------------
#     scoring = match.scoring_settings or {}

#     placement_points = {
#         int(k): int(v)
#         for k, v in (scoring.get("placement_points") or {}).items()
#     }

#     kill_point = float(scoring.get("kill_point", 1))
#     assist_point = float(scoring.get("points_per_assist", 0))
#     damage_point = float(scoring.get("points_per_1000_damage", 0))

#     # ---------------- VALIDATE PLACEMENTS ----------------
#     played_rows = [r for r in results if r.get("played", True)]
#     placements = [r.get("placement") for r in played_rows]

#     if any(p is None for p in placements):
#         return Response({"message": "Played teams must have placement."}, status=400)

#     if len(set(placements)) != len(placements):
#         return Response({"message": "Placements must be unique."}, status=400)

#     # ---------------- TRANSACTION ----------------
#     with transaction.atomic():

#         # delete previous stats
#         TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
#         TournamentTeamMatchStats.objects.filter(match=match).delete()

#         # get teams
#         team_ids = [int(r["tournament_team_id"]) for r in results if r.get("tournament_team_id")]

#         teams = TournamentTeam.objects.filter(
#             event=event,
#             tournament_team_id__in=team_ids
#         )

#         team_map = {t.tournament_team_id: t for t in teams}

#         team_rows = []
#         player_rows = []

#         # ---------------- CREATE TEAM STATS ----------------
#         for r in results:

#             ttid = r.get("tournament_team_id")
#             if not ttid:
#                 continue

#             tt = team_map.get(int(ttid))
#             if not tt:
#                 continue

#             team_played = bool(r.get("played", True))
#             placement = int(r.get("placement") or 0) if team_played else 0

#             players = r.get("players") or []

#             if isinstance(players, str):
#                 players = json.loads(players)

#             if not isinstance(players, list):
#                 players = []

#             if not team_played:
#                 for p in players:
#                     p["played"] = False

#             played_players = [p for p in players if p.get("played", True)]

#             if len(played_players) > 4:
#                 return Response(
#                     {"message": f"Team {ttid}: max 4 played players allowed."},
#                     status=400
#                 )

#             team_kills = sum(int(p.get("kills") or 0) for p in played_players)
#             team_damage = sum(int(p.get("damage") or 0) for p in played_players)
#             team_assists = sum(int(p.get("assists") or 0) for p in played_players)

#             placement_pts = placement_points.get(placement, 0)
#             kill_pts = team_kills * kill_point
#             assist_pts = team_assists * assist_point
#             damage_pts = (team_damage / 1000) * damage_point

#             total_pts = placement_pts + kill_pts + assist_pts + damage_pts

#             team_rows.append(
#                 TournamentTeamMatchStats(
#                     match=match,
#                     tournament_team=tt,
#                     placement=placement,
#                     kills=team_kills,
#                     damage=team_damage,
#                     assists=team_assists,
#                     placement_points=placement_pts,
#                     kill_points=kill_pts,
#                     total_points=int(total_pts),
#                 )
#             )

#         created_team_stats = TournamentTeamMatchStats.objects.bulk_create(team_rows)

#         team_stats_map = {
#             ts.tournament_team_id: ts.team_stats_id
#             for ts in created_team_stats
#         }

#         # ---------------- CREATE PLAYER STATS ----------------
#         for r in results:

#             ttid = r.get("tournament_team_id")
#             ts_id = team_stats_map.get(int(ttid)) if ttid else None

#             if not ts_id:
#                 continue

#             team_played = bool(r.get("played", True))

#             players = r.get("players") or []

#             if isinstance(players, str):
#                 players = json.loads(players)

#             if not isinstance(players, list):
#                 players = []

#             if not team_played:
#                 for p in players:
#                     p["played"] = False

#             for p in players:

#                 uid = p.get("user_id")
#                 if not uid:
#                     continue

#                 played = bool(p.get("played", True)) and team_played

#                 player_rows.append(
#                     TournamentPlayerMatchStats(
#                         team_stats_id=ts_id,
#                         player_id=int(uid),
#                         kills=int(p.get("kills") or 0) if played else 0,
#                         damage=int(p.get("damage") or 0) if played else 0,
#                         assists=int(p.get("assists") or 0) if played else 0,
#                     )
#                 )

#         TournamentPlayerMatchStats.objects.bulk_create(player_rows)

#         match.result_inputted = True
#         match.leaderboard = leaderboard
#         match.save(update_fields=["result_inputted", "leaderboard"])

#     return Response({
#         "message": "Team match result updated.",
#         "match_id": match.match_id,
#         "saved_team_rows": len(created_team_stats),
#         "saved_player_rows": len(player_rows)
#     }, status=200)


# @api_view(["POST"])
# def edit_match_result(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=403)

#     match_id = request.data.get("match_id")
#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)

#     match = get_object_or_404(Match, match_id=match_id)

#     if not match.group or not match.group.stage or not match.group.stage.event:
#         return Response({"message": "Match is not linked to a valid group/stage/event."}, status=400)

#     event = match.group.stage.event
#     leaderboard = _get_leaderboard_for_match(match)
#     if not leaderboard:
#         return Response({"message": "No leaderboard found for this match/group."}, status=400)

#     # placement_points_raw = leaderboard.placement_points or {}
#     # try:
#     #     placement_points = {int(k): int(v) for k, v in placement_points_raw.items()}
#     # except Exception:
#     #     return Response({"message": "Invalid leaderboard placement_points."}, status=400)

#     # kill_point = float(getattr(leaderboard, "kill_point", 1.0) or 1.0)

#     scoring = match.scoring_settings or {}

#     placement_points = {
#         int(k): int(v)
#         for k, v in (scoring.get("placement_points") or {}).items()
#     }

#     kill_point = float(scoring.get("kill_point", 1))
#     points_per_assist = float(scoring.get("points_per_assist", 0))
#     points_per_1000_damage = float(scoring.get("points_per_1000_damage", 0))

#     results = request.data.get("results")
#     if isinstance(results, str):
#         results = json.loads(results or "[]")

#     if not isinstance(results, list) or not results:
#         return Response({"message": "results must be a non-empty list."}, status=400)

#     # basic validation: unique placements among PLAYED competitors
#     played_rows = [r for r in results if r.get("played", True)]
#     placements = [r.get("placement") for r in played_rows]
#     if any(p is None for p in placements):
#         return Response({"message": "Each played row must have placement."}, status=400)
#     if len(set(placements)) != len(placements):
#         return Response({"message": "Placements must be unique (among played rows)."}, status=400)

#     with transaction.atomic():
#         if event.participant_type == "solo":
#             # wipe old
#             SoloPlayerMatchStats.objects.filter(match=match).delete()

#             # build map of competitors in this event
#             competitor_ids = [int(r["competitor_id"]) for r in results if r.get("competitor_id")]
#             regs = RegisteredCompetitors.objects.select_related("user").filter(
#                 event=event, id__in=competitor_ids, status="registered"
#             )
#             reg_map = {rc.id: rc for rc in regs}

#             create_rows = []
#             missing = []

#             for r in results:
#                 cid = r.get("competitor_id")
#                 if not cid:
#                     continue
#                 cid = int(cid)
#                 rc = reg_map.get(cid)
#                 if not rc:
#                     missing.append(cid)
#                     continue

#                 played = bool(r.get("played", True))
#                 placement = int(r.get("placement") or 0) if played else 0
#                 kills = int(r.get("kills") or 0) if played else 0

#                 bonus = int(r.get("bonus_points") or 0)
#                 penalty = int(r.get("penalty_points") or 0)
#                 if bonus < 0 or penalty < 0:
#                     return Response({"message": "bonus_points and penalty_points must be >= 0."}, status=400)

#                 place_pts = placement_points.get(placement, 0) if played else 0
#                 # kill_pts = int(kills * kill_point) if played else 0
#                 # total_pts = place_pts + kill_pts  # base total (adjustment added later in leaderboard calc)
#                 kill_pts = kills * kill_point
#                 total_pts = place_pts + kill_pts

#                 create_rows.append(SoloPlayerMatchStats(
#                     match=match,
#                     competitor=rc,
#                     placement=placement,
#                     kills=kills,
#                     placement_points=place_pts,
#                     kill_points=kill_pts,
#                     total_points=total_pts,
#                     bonus_points=bonus,
#                     penalty_points=penalty,
#                 ))

#             SoloPlayerMatchStats.objects.bulk_create(create_rows, batch_size=500)
#             match.result_inputted = True
#             match.leaderboard = leaderboard
#             match.save(update_fields=["result_inputted", "leaderboard"])

#             return Response({
#                 "message": "Solo match result updated.",
#                 "match_id": match.match_id,
#                 "saved_rows": len(create_rows),
#                 "missing_registered_competitor_ids": missing[:30],
#                 "missing_count": len(missing),
#             }, status=200)

#         else:
#             # TEAM (duo/squad)
#             TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
#             TournamentTeamMatchStats.objects.filter(match=match).delete()

#             team_ids = [int(r["tournament_team_id"]) for r in results if r.get("tournament_team_id")]
#             teams = TournamentTeam.objects.select_related("team").filter(event=event, tournament_team_id__in=team_ids)
#             team_map = {tt.tournament_team_id: tt for tt in teams}

#             team_stats_to_create = []
#             player_stats_to_create = []
#             missing = []

#             for r in results:
#                 ttid = r.get("tournament_team_id")
#                 if not ttid:
#                     continue
#                 ttid = int(ttid)
#                 tt = team_map.get(ttid)
#                 if not tt:
#                     missing.append(ttid)
#                     continue

#                 team_played = bool(r.get("played", True))
#                 placement = int(r.get("placement") or 0) if team_played else 0

#                 players = r.get("players") or []
#                 if isinstance(players, str):
#                     players = json.loads(players or "[]")
#                 if not isinstance(players, list):
#                     players = []

#                 # if team_played is False -> force players played False
#                 if not team_played:
#                     for p in players:
#                         p["played"] = False

#                 # optional: enforce max 4 played (BR squad)
#                 played_players = [p for p in players if p.get("played", True)]
#                 if len(played_players) > 4:
#                     return Response({"message": f"Team {ttid}: max 4 played players allowed."}, status=400)

#                 # compute team totals from played players
#                 team_kills = sum(int(p.get("kills") or 0) for p in played_players) if team_played else 0
#                 team_damage = sum(int(p.get("damage") or 0) for p in played_players) if team_played else 0
#                 team_assists = sum(int(p.get("assists") or 0) for p in played_players) if team_played else 0

#                 place_pts = placement_points.get(placement, 0) if team_played else 0
#                 kill_pts = team_kills * kill_point
#                 assist_pts = team_assists * points_per_assist
#                 damage_pts = (team_damage / 1000) * points_per_1000_damage

#                 total_pts = place_pts + kill_pts + assist_pts + damage_pts

#                 ts = TournamentTeamMatchStats(
#                     match=match,
#                     tournament_team_id=ttid,
#                     placement=placement,
#                     kills=team_kills,
#                     damage=team_damage,
#                     assists=team_assists,
#                     placement_points=place_pts,
#                     kill_points=kill_pts,
#                     total_points=total_pts,
#                 )
#                 team_stats_to_create.append(ts)

#             created_team_stats = TournamentTeamMatchStats.objects.bulk_create(
#                 team_stats_to_create,
#                 batch_size=200
#             )

#             # Map using ID (NOT object)
#             ts_by_team = {
#                 ts.tournament_team_id: ts.team_stats_id
#                 for ts in created_team_stats
#             }

#             for r in results:
#                 ttid = r.get("tournament_team_id")
#                 if not ttid:
#                     continue

#                 ttid = int(ttid)
#                 ts_id = ts_by_team.get(ttid)
#                 if not ts_id:
#                     continue

#                 team_played = bool(r.get("played", True))
#                 players = r.get("players") or []


#                 if isinstance(players, str):
#                     players = json.loads(players or "[]")
#                 if not isinstance(players, list):
#                     players = []

#                 if not team_played:
#                     for p in players:
#                         p["played"] = False
                
                

#                 for p in players:
#                     played = bool(p.get("played", True)) and team_played

#                     uid = p.get("user_id")
#                     if not uid:
#                         continue


#                     player_stats_to_create.append(
#                         TournamentPlayerMatchStats(
#                             team_stats_id=ts_id,
#                             player_id=int(uid),
#                             kills=int(p.get("kills") or 0) if played else 0,
#                             damage=int(p.get("damage") or 0) if played else 0,
#                             assists=int(p.get("assists") or 0) if played else 0,
#                         )
#                     )

#             TournamentPlayerMatchStats.objects.bulk_create(
#                 player_stats_to_create,
#                 batch_size=500
#             )

#             match.result_inputted = True
#             match.leaderboard = leaderboard
#             match.save(update_fields=["result_inputted", "leaderboard"])

#             return Response({
#                 "message": "Team match result updated.",
#                 "match_id": match.match_id,
#                 "saved_team_rows": len(created_team_stats),
#                 "saved_player_rows": len(player_stats_to_create),
#                 "missing_tournament_team_ids": missing[:30],
#                 "missing_count": len(missing)
#             }, status=200)


from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["POST"])
def disqualify_player(request):
    # -------- AUTH --------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # -------- INPUT --------
    event_id = request.data.get("event_id")
    rc_id = request.data.get("registered_competitor_id")
    user_id = request.data.get("user_id")
    reason = (request.data.get("reason") or "").strip()

    if not event_id:
        return Response({"message": "event_id is required."}, status=400)
    if not rc_id and not user_id:
        return Response({"message": "registered_competitor_id or user_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    # ── registration gate (org-aware, resolved after we have the event) ──
    # AFC admins manage registrations for any event; org members need
    # can_manage_registrations on the event's owning org (native AFC events stay admin-only).
    if not _is_event_admin(admin) and not org_can_event(admin, "can_manage_registrations", event):
        return Response({"message": "You do not have permission to manage registrations for this event."}, status=403)

    if event.participant_type != "solo":
        return Response({"message": "This endpoint is for SOLO events only."}, status=400)

    # -------- TARGET --------
    if rc_id:
        rc = get_object_or_404(RegisteredCompetitors, id=rc_id, event=event, user__isnull=False)
    else:
        rc = get_object_or_404(RegisteredCompetitors, event=event, user__user_id=user_id)

    if rc.status == "disqualified":
        return Response({"message": "Player is already disqualified.", "registered_competitor_id": rc.id}, status=200)

    # -------- UPDATE --------
    with transaction.atomic():
        # Event registration status
        rc.status = "disqualified"
        rc.save(update_fields=["status"])

        # Remove from any stage(s)
        stage_rows = StageCompetitor.objects.filter(stage__event=event, player=rc).update(status="disqualified")

        # Remove from any group(s)
        group_rows = StageGroupCompetitor.objects.filter(stage_group__stage__event=event, player=rc).update(status="disqualified")

        # Optional: store reason somewhere (if you add a model field later)
        # For now we just return it in response.


    # Notify Player
    Notifications.objects.create(
        user=rc.user,
        title="Disqualified from Tournament",
        message=f"You have been disqualified from the tournament '{event.event_name}'. Reason: {reason or 'No reason provided.'}",
        notification_type="tournament_disqualification",
        related_event=event,
    )

    return Response({
        "message": "Player disqualified.",
        "event_id": event.event_id,
        "registered_competitor_id": rc.id,
        "username": getattr(rc.user, "username", None),
        "reason": reason or None,
        "updated": {
            "registered_competitors": 1,
            "stage_competitors_rows": stage_rows,
            "stage_group_competitors_rows": group_rows,
        }
    }, status=200)



from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["POST"])
def disqualify_team(request):
    # -------- AUTH --------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # -------- INPUT --------
    event_id = request.data.get("event_id")
    tournament_team_id = request.data.get("tournament_team_id")
    team_id = request.data.get("team_id")
    reason = (request.data.get("reason") or "").strip()

    if not event_id:
        return Response({"message": "event_id is required."}, status=400)
    if not tournament_team_id and not team_id:
        return Response({"message": "tournament_team_id or team_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    # ── registration gate (org-aware, resolved after we have the event) ──
    # AFC admins manage registrations for any event; org members need
    # can_manage_registrations on the event's owning org (native AFC events stay admin-only).
    if not _is_event_admin(admin) and not org_can_event(admin, "can_manage_registrations", event):
        return Response({"message": "You do not have permission to manage registrations for this event."}, status=403)

    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for TEAM events only (duo/squad)."}, status=400)

    # -------- TARGET --------
    if tournament_team_id:
        tt = get_object_or_404(TournamentTeam, tournament_team_id=tournament_team_id, event=event)
    else:
        tt = get_object_or_404(TournamentTeam, event=event, team__team_id=team_id)

    if tt.status == "disqualified":
        return Response({"message": "Team is already disqualified.", "tournament_team_id": tt.tournament_team_id}, status=200)

    # -------- UPDATE --------
    with transaction.atomic():
        # Tournament team status
        tt.status = "disqualified"
        tt.save(update_fields=["status"])

        # Also disqualify the registration row (if you use it for validation)
        reg_rows = RegisteredCompetitors.objects.filter(event=event, team=tt.team).update(status="disqualified")

        # Remove from stage(s)
        stage_rows = StageCompetitor.objects.filter(stage__event=event, tournament_team=tt).update(status="disqualified")

        # Remove from group(s)
        group_rows = StageGroupCompetitor.objects.filter(stage_group__stage__event=event, tournament_team=tt).update(status="disqualified")

    
    # Notify Team Members
    member_users = [m.user for m in tt.members.all() if m.user]
    for user in member_users:
        Notifications.objects.create(
            user=user,
            title="Team Disqualified from Tournament",
            message=f"Your team '{tt.team.team_name}' has been disqualified from the tournament '{event.event_name}'. Reason: {reason or 'No reason provided.'}",
            notification_type="tournament_disqualification",
            related_event=event,
        )
    

    return Response({
        "message": "Team disqualified.",
        "event_id": event.event_id,
        "tournament_team_id": tt.tournament_team_id,
        "team_id": tt.team.team_id,
        "team_name": tt.team.team_name,
        "reason": reason or None,
        "updated": {
            "tournament_team": 1,
            "registered_competitors_rows": reg_rows, 
            "stage_competitors_rows": stage_rows,
            "stage_group_competitors_rows": group_rows,
        }
    }, status=200)


@api_view(["GET"])
def get_drafted_events(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    qs = Event.objects.filter(is_draft=True)

    # ORGANIZER DRAFTS: when ?organization_id is given, scope the drafts to that one org
    # and require the caller to be an AFC admin OR an org member who can create/edit its
    # events (so an organizer sees ONLY their own org's drafts, and cannot read another
    # org's). Consumed by the organizer Drafts page (app/(organizer)/organizer/events/drafts).
    organization_id = request.GET.get("organization_id")
    if organization_id:
        org = Organization.objects.filter(organization_id=organization_id).first()
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        if not (
            _is_event_admin(user)
            or org_can(user, "can_create_events", org)
            or org_can(user, "can_edit_events", org)
        ):
            return Response(
                {"message": "You do not have permission to view this organization's drafts."},
                status=403,
            )
        qs = qs.filter(organization=org)

    events = qs.order_by("-created_at")
    event_list = []
    for event in events:
        event_list.append({
            "event_id": event.event_id,
            "event_slug": event.slug,
            "event_name": event.event_name,
            "participant_type": event.participant_type,
            "created_at": event.created_at,
        })
    return Response({
        "message": "Drafted events retrieved.",
        "drafted_events": event_list,
    }, status=200)



@api_view(["GET"])
def get_my_drafted_events(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)
    events = Event.objects.filter(is_draft=True, creator=user).order_by("-created_at")
    event_list = []
    for event in events:
        event_list.append({
            "event_id": event.event_id,
            "event_slug": event.slug,
            "event_name": event.event_name,
            "participant_type": event.participant_type,
            "created_at": event.created_at,
        })
    return Response({
        "message": "Your drafted events retrieved.",
        "drafted_events": event_list,
    }, status=200)


@api_view(["GET"])
def get_total_kills(request):
    total_solo_kills = SoloPlayerMatchStats.objects.aggregate(total=Sum("kills"))["total"] or 0
    total_team_kills = TournamentPlayerMatchStats.objects.aggregate(total=Sum("kills"))["total"] or 0
    total_kills = total_solo_kills + total_team_kills

    return Response({
        "total_solo_kills": total_solo_kills,
        "total_team_kills": total_team_kills,
        "total_kills": total_kills
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def generate_single_use_invite_link_for_private_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission."}, status=403)
    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)
    event = get_object_or_404(Event, event_id=event_id)
    if event.is_draft:
        return Response({"message": "Cannot generate invite link for draft event."}, status=400
    )
    if event.is_public:
        return Response({"message": "Event is already public. No invite link needed."}, status=400)

    # SHARED vs single-use:
    #   is_shared=True  -> ONE reusable first-come-first-serve link; many people register
    #                      through it and it is never consumed. FCFS is enforced by the
    #                      event capacity cap in register_for_event (active_count >=
    #                      max_teams_or_players), which closes the link once full.
    #   is_shared=False -> today's behavior: a single-use link consumed by one registration.
    # Accept truthy strings ("true"/"1") as well as real booleans from JSON clients.
    is_shared = request.data.get("is_shared", False)
    if isinstance(is_shared, str):
        is_shared = is_shared.strip().lower() in ("true", "1", "yes")

    # Optional expiry: after expires_in_days the link stops registering anyone. The
    # register_for_event invite gate enforces this (it was previously ignored).
    expires_at = None
    expires_in_days = request.data.get("expires_in_days")
    if expires_in_days not in (None, ""):
        try:
            days = int(expires_in_days)
        except (TypeError, ValueError):
            return Response({"message": "expires_in_days must be an integer number of days."}, status=400)
        if days < 1:
            return Response({"message": "expires_in_days must be at least 1."}, status=400)
        expires_at = timezone.now() + timedelta(days=days)

    # Generate a unique token (you can use UUID or any other method)
    import uuid
    event_slug = event.slug
    token = str(uuid.uuid4())
    # Store the token with an association to the event (you may want to create a model for this)
    EventInviteToken.objects.create(
        event=event,
        token=token,
        created_by=admin,
        is_shared=is_shared,
        expires_at=expires_at,
    )
    # Construct the invite link (replace with your frontend URL). The user-side
    # registration page reads ?invitation=<token>, so a shared link works with the
    # existing flow — it just is not consumed.
    invite_link = f"https://africanfreefirecommunity.com/tournaments/{event_slug}?invitation={token}"
    return Response({
        "message": "Invite link generated.",
        "event_id": event.event_id,
        "invite_link": invite_link,
        "is_shared": is_shared,
        "expires_at": expires_at,
    }, status=200)


@api_view(["POST"])
def generate_multiple_single_use_invite_links_for_private_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission."}, status=403)
    event_id = request.data.get("event_id")
    count = request.data.get("count", 1)
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)
    if not isinstance(count, int) or count < 1 or count > 100:
        return Response({"message": "count must be an integer between 1 and 100."}, status=400)
    event = get_object_or_404(Event, event_id=event_id)
    if event.is_draft:
        return Response({"message": "Cannot generate invite links for draft event."}, status=400)
    if event.is_public:
        return Response({"message": "Event is already public. No invite links needed."}, status=400)
    invite_links = []
    import uuid
    event_slug = event.slug
    for _ in range(count):
        token = str(uuid.uuid4())
        EventInviteToken.objects.create(event=event, token=token, created_by=admin)
        invite_link = f"https://africanfreefirecommunity.com/tournaments/{event_slug}?invitation={token}"
        invite_links.append(invite_link)
    return Response({
        "message": f"{len(invite_links)} invite links generated.",
        "event_id": event.event_id,
        "invite_links": invite_links,
    }, status=200)


@api_view(["POST"])
def get_all_invite_links_for_private_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission."}, status=403)
    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)
    event = get_object_or_404(Event, event_id=event_id)
    if event.is_draft:
        return Response({"message": "Draft event does not have invite links."}, status=400)
    if event.is_public:
        return Response({"message": "Public event does not have invite links."}, status=400)
    tokens = EventInviteToken.objects.filter(event=event).order_by("-created_at")
    invite_links = []
    for token in tokens:
        invite_link = f"https://africanfreefirecommunity.com/tournaments/{event.slug}?invitation={token.token}"
        invite_links.append({
            "invite_link": invite_link,
            "created_at": token.created_at,
            "created_by": token.created_by.username if token.created_by else None,
            "is_used": token.is_used,
            "used_by": token.used_by.username if token.used_by else None,
            "used_at": token.used_at,
            # is_shared distinguishes the reusable FCFS link from single-use links so the
            # admin UI can label it; expires_at lets the UI show when a link stops working.
            "is_shared": token.is_shared,
            "expires_at": token.expires_at,
        })
    return Response({
        "message": f"{len(invite_links)} invite links retrieved.",
        "event_id": event.event_id,
        "invite_links": invite_links,
    }, status=200)


from django.utils import timezone
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404


# @api_view(["POST"])
# def leave_event(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     if event.is_draft:
#         return Response({"message": "Cannot leave a draft event."}, status=400)

#     today = timezone.now().date()

#     # ✅ Registration period check
#     if today > event.registration_end_date:
#         return Response(
#             {"message": "Registration period has ended. You cannot leave this event."},
#             status=400
#         )

#     if today < event.registration_open_date:
#         return Response(
#             {"message": "Registration has not opened yet."},
#             status=400
#         )

#     with transaction.atomic():

#         # ---------------- SOLO ----------------
#         if event.participant_type == "solo":
#             registration = RegisteredCompetitors.objects.filter(
#                 event=event,
#                 user=user
#             ).first()

#             if not registration:
#                 return Response({"message": "You are not registered in this event."}, status=400)

#             if registration.status != "registered":
#                 return Response(
#                     {"message": f"You cannot leave. Current status: {registration.status}"},
#                     status=400
#                 )

#             registration.status = "withdrawn"   # or "left" if you add it
#             registration.save(update_fields=["status"])

#         # ---------------- TEAM / SQUAD ----------------
#         else:
#             # find tournament team where user is a member
#             tournament_team = TournamentTeam.objects.filter(
#                 event=event,
#                 members__user=user
#             ).first()

#             if not tournament_team:
#                 return Response({"message": "You are not part of any team in this event."}, status=400)

#             if tournament_team.status != "active":
#                 return Response(
#                     {"message": f"Team cannot leave. Current status: {tournament_team.status}"},
#                     status=400
#                 )

#             tournament_team.status = "withdrawn"
#             tournament_team.save(update_fields=["status"])

#     return Response({
#         "message": "You have successfully left the event.",
#         "event_id": event.event_id
#     }, status=200)


from django.utils import timezone
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404


@api_view(["POST"])
def leave_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    if event.is_draft:
        return Response({"message": "Cannot leave a draft event."}, status=400)

    today = timezone.now().date()

    if today < event.registration_open_date or today > event.registration_end_date:
        return Response(
            {"message": "You can only leave during the registration period."},
            status=400
        )

    with transaction.atomic():

        # ---------------- SOLO EVENT ----------------
        if event.participant_type == "solo":

            registration = RegisteredCompetitors.objects.filter(
                event=event,
                user=user,
                status="registered"
            ).first()

            if not registration:
                return Response({"message": "You are not registered in this event."}, status=400)

            # 🔥 Delete registration completely
            registration.delete()

            return Response({
                "message": "You have successfully left the event.",
                "event_id": event.event_id
            }, status=200)

        # ---------------- SQUAD / DUO EVENT ----------------
        else:

            tournament_team = TournamentTeam.objects.filter(
                event=event,
                members__user=user
            ).select_related("team").first()

            if not tournament_team:
                return Response({"message": "You are not part of any team in this event."}, status=400)

            # 🔐 Only captain (registered_by) can leave
            if tournament_team.team.team_owner != user:
                return Response({
                    "message": "Only the team captain can leave the event."
                }, status=403)
            
            # Delete All Tournament Team Members
            TournamentTeamMember.objects.filter(tournament_team=tournament_team).delete()
            RegisteredCompetitors.objects.filter(event=event, team=tournament_team.team).delete()

            # 🔥 Delete entire team entry
            tournament_team.delete()

            return Response({
                "message": "Team has been successfully removed from the event.",
                "event_id": event.event_id
            }, status=200)



@api_view(["POST"])
def check_invite_token_status(request):
    token = request.data.get("invite_token")
    if not token:
        return Response({"message": "token is required."}, status=400)
    invite = EventInviteToken.objects.filter(token=token).first()
    if not invite:
        return Response({"message": "Invalid token."}, status=404)
    # Surface is_shared/expiry so the user-side page can tell a reusable FCFS link
    # (still valid even when is_used is True) apart from a consumed single-use link,
    # and so it can show an "expired" state. is_expired mirrors the register_for_event
    # gate so the frontend and backend agree on when a link stops working.
    is_expired = bool(invite.expires_at and timezone.now() > invite.expires_at)
    return Response({
        "token": invite.token,
        "is_used": invite.is_used,
        "used_by": invite.used_by.username if invite.used_by else None,
        "used_at": invite.used_at,
        "is_shared": invite.is_shared,
        "expires_at": invite.expires_at,
        "is_expired": is_expired,
    }, status=200)


@api_view(["POST"])
def seed_event_competitors_to_stage(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid session"}, status=401)

    if admin.role != "admin":
        return Response({"message": "No permission"}, status=403)

    stage_id = request.data.get("stage_id")
    clear_existing = request.data.get("clear_existing", False)

    if not stage_id:
        return Response({"message": "stage_id required"}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)
    event = stage.event

    # Check the registration end date, and prevent seeding if registration has not closed.
    today = timezone.now().date()
    if today < event.registration_end_date:
        return Response(
            {"message": "Cannot seed competitors to stage until registration period has ended."},
            status=400
        )

    with transaction.atomic():

        if clear_existing:
            StageCompetitor.objects.filter(stage=stage).delete()

        # Prevent accidental reseeding
        if StageCompetitor.objects.filter(stage=stage).exists() and not clear_existing:
            return Response(
                {"message": "Stage already has competitors. Use clear_existing=True to reseed."},
                status=400
            )

        seeded = 0

        # -------- SOLO --------
        if event.participant_type == "solo":

            competitors = RegisteredCompetitors.objects.filter(
                event=event,
                status="registered"
            )

            existing_ids = StageCompetitor.objects.filter(stage=stage).values_list("player_id", flat=True)

            new_entries = [
                StageCompetitor(stage=stage, player=comp)
                for comp in competitors
                if comp.id not in existing_ids
            ]

        # -------- SQUAD --------
        else:

            teams = TournamentTeam.objects.filter(
                event=event,
                status="active"
            )

            existing_ids = StageCompetitor.objects.filter(stage=stage).values_list("tournament_team_id", flat=True)

            new_entries = [
                StageCompetitor(stage=stage, tournament_team=team)
                for team in teams
                if team.tournament_team_id not in existing_ids
            ]

        StageCompetitor.objects.bulk_create(new_entries)
        seeded = len(new_entries)

        created, skipped = reconcile_stage_roles(stage.stage_id)

    return Response({
        "message": "Event competitors seeded to stage successfully.",
        "stage_id": stage.stage_id,
        "total_seeded": seeded,
        "stage_roles_created": created,
        "stage_roles_skipped": skipped,
    }, status=200)

import random

@api_view(["POST"])
def seed_stage_competitors_to_groups_team(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid session"}, status=401)

    if admin.role != "admin":
        return Response({"message": "No permission"}, status=403)

    stage_id = request.data.get("stage_id")
    shuffle = request.data.get("shuffle", True)
    clear_existing = request.data.get("clear_existing", False)

    if not stage_id:
        return Response({"message": "stage_id required"}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)
    groups = StageGroups.objects.filter(stage=stage).order_by("group_id")

    if not groups.exists():
        return Response({"message": "No groups found."}, status=400)

    with transaction.atomic():

        if clear_existing:
            StageGroupCompetitor.objects.filter(
                stage_group__stage=stage
            ).delete()

        # Prevent accidental reseed
        if StageGroupCompetitor.objects.filter(
            stage_group__stage=stage
        ).exists() and not clear_existing:
            return Response(
                {"message": "Groups already seeded. Use clear_existing=True to reseed."},
                status=400
            )

        competitors = list(
            StageCompetitor.objects.filter(
                stage=stage,
                status="active"
            )
        )

        if not competitors:
            return Response({"message": "No stage competitors found."}, status=400)

        if shuffle:
            random.shuffle(competitors)

        group_list = list(groups)
        group_count = len(group_list)

        new_entries = []

        for index, competitor in enumerate(competitors):
            group = group_list[index % group_count]

            if competitor.tournament_team:
                new_entries.append(
                    StageGroupCompetitor(
                        stage_group=group,
                        tournament_team=competitor.tournament_team
                    )
                )
            else:
                new_entries.append(
                    StageGroupCompetitor(
                        stage_group=group,
                        player=competitor.player
                    )
                )

        StageGroupCompetitor.objects.bulk_create(new_entries)

        result = reconcile_group_roles_for_stage(stage)


    return Response({
        "message": "Stage competitors seeded to groups successfully.",
        "stage_id": stage.stage_id,
        "groups": group_count,
        "total_seeded": len(new_entries),
        "roles_created": result["created_pending"],
        "roles_skipped": result["skipped"],
    }, status=200)


def reconcile_stage_roles(stage_id):
    created = 0
    skipped = 0

    stage = get_object_or_404(Stages, stage_id=stage_id)

    competitors = StageCompetitor.objects.select_related(
        "player__user",
        "tournament_team"
    ).filter(stage=stage)

    for competitor in competitors:

        # SOLO
        if competitor.player:
            players = [competitor.player]

        # TEAM
        else:
            players = competitor.tournament_team.members.select_related("user").all()

        for player in players:
            user = player.user

            if not user or not user.discord_id or not stage.stage_discord_role_id:
                skipped += 1
                continue

            exists = DiscordRoleAssignment.objects.filter(
                user=user,
                stage=stage,
                group=None,
                role_id=stage.stage_discord_role_id,
                status="success"
            ).exists()

            if exists:
                skipped += 1
                continue

            # Duplicate-tolerant upsert (bug "failed to start", 2026-06-12): the table has no
            # unique constraint on this tuple and the register/seed paths bulk_create rows, so
            # DUPLICATES exist in real data. get_or_create's get() raised
            # MultipleObjectsReturned (3 rows) and 500'd the whole event start. filter().first()
            # treats any existing row (whatever its status) as "already tracked"; only a truly
            # missing assignment creates a new pending row.
            existing_row = DiscordRoleAssignment.objects.filter(
                user=user,
                discord_id=user.discord_id,
                role_id=stage.stage_discord_role_id,
                stage=stage,
                group=None,
            ).first()
            if existing_row is None:
                DiscordRoleAssignment.objects.create(
                    user=user,
                    discord_id=user.discord_id,
                    role_id=stage.stage_discord_role_id,
                    stage=stage,
                    group=None,
                    status="pending",
                )

            created += 1

    pending_count = DiscordRoleAssignment.objects.filter(
        stage=stage,
        group__isnull=True,
        status="pending"
    ).count()

    if pending_count == 0:
        return {"created_pending": 0, "message": "No pending roles."}

    progress = DiscordStageRoleAssignmentProgress.objects.create(
        stage=stage,
        total=pending_count,
        completed=0,
        failed=0,
        status="running"
    )

    assign_stage_roles_from_db_task.delay(
        progress.id,
        stage.stage_id
    )

    return created, skipped


from django.db import transaction

def reconcile_group_roles_for_stage(stage):
    """
    Creates pending DiscordRoleAssignment records for all players
    in stage groups (solo or team).
    Safe to call multiple times.
    """

    created = 0
    skipped = 0

    with transaction.atomic():

        group_competitors = (
            StageGroupCompetitor.objects
            .select_related(
                "stage_group",
                "player__user",
                "tournament_team"
            )
            .prefetch_related(
                "tournament_team__members__user"
            )
            .filter(stage_group__stage=stage)
        )

        for sgc in group_competitors:
            group = sgc.stage_group

            # No role configured
            if not group.group_discord_role_id:
                skipped += 1
                continue

            # -------- SOLO --------
            if sgc.player:
                players = [sgc.player]

            # -------- TEAM --------
            elif sgc.tournament_team:
                players = sgc.tournament_team.members.select_related("user").all()

            else:
                skipped += 1
                continue

            for player in players:
                user = getattr(player, "user", None)

                if not user or not user.discord_id:
                    skipped += 1
                    continue

                # Already successfully assigned?
                already_success = DiscordRoleAssignment.objects.filter(
                    user=user,
                    stage=stage,
                    group=group,
                    role_id=group.group_discord_role_id,
                    status="success"
                ).exists()

                if already_success:
                    skipped += 1
                    continue

                # Duplicate-tolerant upsert (same bug + fix as reconcile_stage_roles above):
                # duplicate assignment rows exist in real data and get_or_create's get()
                # raises MultipleObjectsReturned on them, 500ing the group reconcile.
                existing_row = DiscordRoleAssignment.objects.filter(
                    user=user,
                    discord_id=user.discord_id,
                    role_id=group.group_discord_role_id,
                    stage=stage,
                    group=group,
                ).first()
                if existing_row is None:
                    DiscordRoleAssignment.objects.create(
                        user=user,
                        discord_id=user.discord_id,
                        role_id=group.group_discord_role_id,
                        stage=stage,
                        group=group,
                        status="pending",
                    )

                created += 1

    # Trigger async worker
    assign_group_roles_from_db_task.delay(stage.stage_id)

    return {
        "created_pending": created,
        "skipped": skipped
    }


@api_view(["POST"])
def add_teams_to_stage(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid session"}, status=401)
    if admin.role != "admin":
        return Response({"message": "No permission"}, status=403)
    stage_id = request.data.get("stage_id")
    team_ids = request.data.get("team_ids", [])
    if not stage_id:
        return Response({"message": "stage_id required"}, status=400)
    if not isinstance(team_ids, list) or not all(isinstance(tid, int) for tid in team_ids):
        return Response({"message": "team_ids must be a list of integers"}, status=400)
    stage = get_object_or_404(Stages, stage_id=stage_id)
    event = stage.event
    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for team events only."}, status=400)
    teams = TournamentTeam.objects.filter(team_id__in=team_ids, event=event, status="active")
    if not teams.exists():
        return Response({"message": "No valid teams found for the provided team_ids."}, status=400)
    existing_team_ids = StageCompetitor.objects.filter(stage=stage, tournament_team__team_id__in=team_ids).values_list("tournament_team__team_id", flat=True)
    new_entries = []
    for team in teams:
        if team.team_id in existing_team_ids:
            continue
        new_entries.append(StageCompetitor(stage=stage, tournament_team=team))
    StageCompetitor.objects.bulk_create(new_entries)
    return Response({
        "message": f"{len(new_entries)} teams added to stage.",
        "stage_id": stage.stage_id,
        "added_team_ids": [entry.tournament_team.team_id for entry in new_entries],
    }, status=200)


import re
from django.db import transaction
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from django.shortcuts import get_object_or_404


TEAM_BLOCK_RE = re.compile(
    r"TeamName:\s*(?P<team_name>.+?)\s+Rank:\s*(?P<placement>\d+).*?"
    r"KillScore:\s*(?P<team_kills>\d+).*?"
    r"RankScore:\s*(?P<rank_score>\d+).*?"
    r"TotalScore:\s*(?P<total_score>\d+)(?P<players_block>.*?)(?=TeamName:|$)",
    re.DOTALL
)

PLAYER_RE = re.compile(
    r"NAME:\s*(?P<name>.+?)\s+ID:\s*(?P<uid>\d+)\s+KILL:\s*(?P<kills>\d+)"
)


# @api_view(["POST"])
# @parser_classes([MultiPartParser, FormParser])
# def upload_team_match_result(request):

#     # -------- AUTH --------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=403)

#     match_id = request.data.get("match_id")
#     if not match_id:
#         return Response({"message": "match_id required."}, status=400)

#     uploaded_file = request.FILES.get("file")
#     if not uploaded_file:
#         return Response({"message": "file required."}, status=400)

#     match = get_object_or_404(Match, match_id=match_id)

#     if not match.group:
#         return Response({"message": "Match not linked to group."}, status=400)

#     event = match.group.stage.event
#     if event.participant_type == "solo":
#         return Response({"message": "This endpoint is for TEAM events only."}, status=400)

#     # -------- SCORING --------
#     scoring = match.scoring_settings or {}

#     try:
#         placement_points = {
#             int(k): int(v)
#             for k, v in (scoring.get("placement_points") or {}).items()
#         }
#     except Exception:
#         return Response({"message": "Invalid scoring placement_points."}, status=400)

#     kill_point = float(scoring.get("kill_point", 1))
#     points_per_assist = float(scoring.get("points_per_assist", 0))
#     points_per_1000_damage = float(scoring.get("points_per_1000_damage", 0))

#     # -------- PARSE FILE --------
#     text = uploaded_file.read().decode("utf-8", errors="ignore")

#     parsed_teams = []

#     for block in TEAM_BLOCK_RE.finditer(text):
#         players = []
#         players_block = block.group("players_block")

#         for p in PLAYER_RE.finditer(players_block):
#             players.append({
#                 "uid": p.group("uid").strip(),
#                 "kills": int(p.group("kills")),
#                 "name": p.group("name").strip()
#             })

#         parsed_teams.append({
#             "team_name": block.group("team_name").strip(),
#             "placement": int(block.group("placement")),
#             "players": players
#         })

#     if not parsed_teams:
#         return Response({"message": "No team data parsed."}, status=400)

#     # -------- MAP USERS --------
#     all_uids = [p["uid"] for t in parsed_teams for p in t["players"]]

#     members = TournamentTeamMember.objects.select_related(
#         "tournament_team", "user"
#     ).filter(
#         tournament_team__event=event,
#         user__uid__in=all_uids
#     )

#     uid_to_member = {m.user.uid: m for m in members}

#     # -------- SAVE --------
#     team_stats_to_create = []
#     player_stats_to_create = []
#     missing_players = []

#     with transaction.atomic():

#         TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
#         TournamentTeamMatchStats.objects.filter(match=match).delete()

#         for team_data in parsed_teams:

#             placement = team_data["placement"]
#             players = team_data["players"]

#             # find team via first valid member
#             team_obj = None
#             team_members = []

#             for p in players:
#                 member = uid_to_member.get(p["uid"])
#                 if member:
#                     team_obj = member.tournament_team
#                     break

#             if not team_obj:
#                 missing_players.append(team_data["team_name"])
#                 continue

#             total_kills = sum(p["kills"] for p in players)

#             placement_pts = placement_points.get(placement, 0)
#             kill_pts = total_kills * kill_point
#             assist_pts = 0  # not in file
#             damage_pts = 0  # not in file

#             total_pts = placement_pts + kill_pts + assist_pts + damage_pts

#             team_stat = TournamentTeamMatchStats(
#                 match=match,
#                 tournament_team=team_obj,
#                 placement=placement,
#                 kills=total_kills,
#                 damage=0,
#                 assists=0,
#                 placement_points=placement_pts,
#                 kill_points=int(kill_pts),
#                 total_points=int(total_pts),
#             )
#             team_stats_to_create.append(team_stat)

#         created_team_stats = TournamentTeamMatchStats.objects.bulk_create(team_stats_to_create)

#         ts_map = {ts.tournament_team_id: ts for ts in created_team_stats}

#         for team_data in parsed_teams:
#             for p in team_data["players"]:
#                 member = uid_to_member.get(p["uid"])
#                 if not member:
#                     continue

#                 ts = ts_map.get(member.tournament_team_id)
#                 if not ts:
#                     continue

#                 player_stats_to_create.append(
#                     TournamentPlayerMatchStats(
#                         team_stats=ts,
#                         player=member.user,
#                         kills=p["kills"],
#                         damage=0,
#                         assists=0,
#                     )
#                 )

#         TournamentPlayerMatchStats.objects.bulk_create(player_stats_to_create)

#         match.result_inputted = True
#         match.save(update_fields=["result_inputted"])

#     return Response({
#         "message": "Team match results uploaded successfully.",
#         "match_id": match.match_id,
#         "parsed_teams": len(parsed_teams),
#         "saved_teams": len(created_team_stats),
#         "saved_players": len(player_stats_to_create),
#         "missing_teams": missing_players[:20]
#     }, status=200)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def upload_team_match_result(request):

    # -------- AUTH --------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Unauthorized."}, status=403)

    # NOTE: permission is finalised below, after the owning event is resolved — org members
    # with can_upload_results may upload for THEIR org's events.

    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id required."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return Response({"message": "file required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)

    if not match.group:
        return Response({"message": "Match not linked to group."}, status=400)

    event = match.group.stage.event

    # ── AUTH (event-scoped): AFC event admins always pass; otherwise allow org members
    # holding can_upload_results on the event's owning org (native events stay admin-only).
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "Unauthorized."}, status=403)

    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for TEAM events only."}, status=400)

    # -------- SCORING --------
    scoring = match.scoring_settings or {}

    try:
        placement_points = {
            int(k): int(v)
            for k, v in (scoring.get("placement_points") or {}).items()
        }
    except Exception:
        return Response({"message": "Invalid scoring placement_points."}, status=400)

    kill_point = float(scoring.get("kill_point", 1))

    # -------- PARSE FILE --------
    text = uploaded_file.read().decode("utf-8", errors="ignore")

    parsed_teams = []

    for block in TEAM_BLOCK_RE.finditer(text):
        players = []
        players_block = block.group("players_block")

        for p in PLAYER_RE.finditer(players_block):
            players.append({
                "uid": p.group("uid").strip(),
                "kills": int(p.group("kills")),
                "name": p.group("name").strip()
            })

        parsed_teams.append({
            "team_name": block.group("team_name").strip(),
            "placement": int(block.group("placement")),
            "players": players
        })

    if not parsed_teams:
        return Response({"message": "No team data parsed."}, status=400)

    # -------- MAP USERS --------
    all_uids = [p["uid"] for t in parsed_teams for p in t["players"]]

    members = TournamentTeamMember.objects.select_related(
        "tournament_team", "user"
    ).filter(
        tournament_team__event=event,
        user__uid__in=all_uids
    )

    uid_to_member = {m.user.uid: m for m in members}

    team_stats_to_create = []
    player_stats_to_create = []
    missing_teams = []

    with transaction.atomic():

        # Safe re-upload
        TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
        TournamentTeamMatchStats.objects.filter(match=match).delete()

        # -------- CREATE TEAM STATS --------
        for team_data in parsed_teams:

            placement = team_data["placement"]
            players = team_data["players"]

            team_obj = None

            # Find team via first valid member
            for p in players:
                member = uid_to_member.get(p["uid"])
                if member:
                    team_obj = member.tournament_team
                    break

            if not team_obj:
                missing_teams.append(team_data["team_name"])
                continue

            total_kills = sum(p["kills"] for p in players)

            # Shared team formula. This log-upload path carries no assists/damage/bonus/penalty,
            # so they pass through as 0 (placement + kills only) — same result as the old inline calc.
            pts = scoring_lib.compute_team_points(
                placement_points=placement_points, kill_point=kill_point,
                points_per_assist=0, points_per_1000_damage=0,
                placement=placement, kills=total_kills, damage=0, assists=0,
                bonus=0, penalty=0, played=True,
            )

            team_stats_to_create.append(
                TournamentTeamMatchStats(
                    match=match,
                    tournament_team=team_obj,
                    placement=placement,
                    kills=total_kills,
                    damage=0,
                    assists=0,
                    placement_points=pts["placement_points"],
                    kill_points=pts["kill_points"],
                    total_points=pts["total_points"],
                )
            )

        created_team_stats = TournamentTeamMatchStats.objects.bulk_create(
            team_stats_to_create,
            batch_size=200
        )

        # Build safe FK map using IDs
        ts_map = {
            ts.tournament_team_id: ts.team_stats_id
            for ts in created_team_stats
        }

        # -------- CREATE PLAYER STATS --------
        for team_data in parsed_teams:
            for p in team_data["players"]:
                member = uid_to_member.get(p["uid"])
                if not member:
                    continue

                ts_id = ts_map.get(member.tournament_team_id)
                if not ts_id:
                    continue

                player_stats_to_create.append(
                    TournamentPlayerMatchStats(
                        team_stats_id=ts_id,  # 🔥 SAFE FIX
                        player_id=member.user_id,
                        kills=p["kills"],
                        damage=0,
                        assists=0,
                    )
                )

        TournamentPlayerMatchStats.objects.bulk_create(
            player_stats_to_create,
            batch_size=500
        )

        match.result_inputted = True
        match.save(update_fields=["result_inputted"])

    return Response({
        "message": "Team match results uploaded successfully.",
        "match_id": match.match_id,
        "parsed_teams": len(parsed_teams),
        "saved_teams": len(created_team_stats),
        "saved_players": len(player_stats_to_create),
        "missing_teams": missing_teams[:20]
    }, status=200)


@api_view(["POST"])
def add_teams_to_event(request):

    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized."}, status=403)

    event_id = request.data.get("event_id")
    team_ids = request.data.get("team_ids", [])

    if not event_id:
        return Response({"message": "event_id required."}, status=400)

    if not isinstance(team_ids, list) or not all(isinstance(tid, int) for tid in team_ids):
        return Response({"message": "team_ids must be a list of integers"}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for team events only."}, status=400)

    teams = Team.objects.filter(team_id__in=team_ids)

    new_registrations = []
    new_tournament_teams = []

    with transaction.atomic():

        for team in teams:

            # RegisteredCompetitors
            if not RegisteredCompetitors.objects.filter(event=event, team=team).exists():
                new_registrations.append(
                    RegisteredCompetitors(
                        event=event,
                        team=team,
                        status="registered"
                    )
                )

            # TournamentTeam
            tt = TournamentTeam.objects.filter(event=event, team=team).first()

            if not tt:
                tt = TournamentTeam.objects.create(
                    event=event,
                    team=team,
                    status="active"
                )
                new_tournament_teams.append(tt)

            # Add team members
            members = TeamMembers.objects.filter(team=team).select_related("member")

            for member in members:

                if not TournamentTeamMember.objects.filter(
                    tournament_team=tt,
                    user=member.member
                ).exists():

                    TournamentTeamMember.objects.create(
                        tournament_team=tt,
                        user=member.member,
                        event=event
                    )

        if new_registrations:
            RegisteredCompetitors.objects.bulk_create(new_registrations)

    return Response({
        "message": f"{len(new_registrations)} teams registered and {len(new_tournament_teams)} teams added.",
        "event_id": event.event_id,
        "added_team_ids": [team.team_id for team in teams],
    }, status=200)

# @api_view(["POST"])
# def add_teams_to_event(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid token."}, status=400)
#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=403)
#     event_id = request.data.get("event_id")
#     team_ids = request.data.get("team_ids", [])
#     if not event_id:
#         return Response({"message": "event_id required."}, status=400)
#     if not isinstance(team_ids, list) or not all(isinstance(tid, int) for tid in team_ids):
#         return Response({"message": "team_ids must be a list of integers"}, status=400
#     )
#     event = get_object_or_404(Event, event_id=event_id)
#     if event.participant_type == "solo":
#         return Response({"message": "This endpoint is for team events only."}, status=400)
    
#     # Get all teams using the team ids
#     teams = Team.objects.filter(team_id__in=team_ids)

#     # add each team to RegisteredCompetitors with status "registered" and also to TournamentTeam with status "active" but confirm they arent already currently there
#     new_registrations = []
#     new_tournament_teams = []
#     for team in teams:
#         if not RegisteredCompetitors.objects.filter(event=event, team=team).exists():
#             new_registrations.append(RegisteredCompetitors(
#                 event=event,
#                 team=team,
#                 status="registered"
#             ))
#         if not TournamentTeam.objects.filter(event=event, team=team).exists():
#             new_tournament_teams.append(TournamentTeam(
#                 event=event,
#                 team=team,
#                 status="active"
#             ))

#         RegisteredCompetitors.objects.bulk_create(new_registrations)
#         TournamentTeam.objects.bulk_create(new_tournament_teams)

#         # add the members of the team to TournamentTeamMember if they are not already there
#         for member in TeamMembers.objects.filter(team=team).select_related("member"):
#             if not TournamentTeamMember.objects.filter(tournament_team__event=event, tournament_team__team=team, user=member.member).exists():
#                 TournamentTeamMember.objects.create(
#                     tournament_team=new_tournament_teams[-1],  # reference the newly created TournamentTeam
#                     user=member.member
#                 )
    
#     return Response({
#         "message": f"{len(new_registrations)} teams registered and {len(new_tournament_teams)} teams added to tournament for event.",
#         "event_id": event.event_id,
#         "added_team_ids": [team.team_id for team in teams],
#     }, status=200)


@api_view(["POST"])
def add_teams_to_group(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized."}, status=403)
    group_id = request.data.get("group_id")
    team_ids = request.data.get("team_ids", [])
    if not group_id:
        return Response({"message": "group_id required."}, status=400)
    if not isinstance(team_ids, list) or not all(isinstance(tid, int) for tid in team_ids):
        return Response({"message": "team_ids must be a list of integers"}, status=400)
    group = get_object_or_404(StageGroups, group_id=group_id)
    if group.stage.event.participant_type == "solo":
        return Response({"message": "This endpoint is for team events only."}, status=400)
    teams = TournamentTeam.objects.filter(team_id__in=team_ids, event=group.stage.event,
        status="active")
    if not teams.exists():
        return Response({"message": "No valid teams found for the provided team_ids."}, status=400)
    existing_team_ids = StageGroupCompetitor.objects.filter(stage_group=group, tournament_team__team_id__in=team_ids).values_list("tournament_team__team_id", flat=True)
    new_entries = []
    for team in teams:
        if team.team_id in existing_team_ids:
            continue
        new_entries.append(StageGroupCompetitor(stage_group=group, tournament_team=team))
    StageGroupCompetitor.objects.bulk_create(new_entries)
    return Response({
        "message": f"{len(new_entries)} teams added to group.",
        "group_id": group.group_id,
        "added_team_ids": [entry.tournament_team.team_id for entry in new_entries],
    }, status=200)


@api_view(["GET"])
def get_all_tournament_player_match_stats(requests):
    stats = TournamentPlayerMatchStats.objects.all()
    data = []
    for stat in stats:
        data.append({
            "player_id": stat.player_id,
            "player_username": stat.player.username,
            "match_id": stat.team_stats.match_id,
            "kills": stat.kills,
            "damage": stat.damage,
            "assists": stat.assists,
        })

    return Response(data, status=200)

@api_view(["POST"])
def create_sponsor_account(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized."}, status=403)

    # Only head admin can create sponsor accounts
    if admin.userroles.filter(role__role_name='head_admin'):
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to create sponsor account."}, status=status.HTTP_403_FORBIDDEN)
    
    fullname = request.data.get("fullname")
    email = request.data.get("email")
    username = request.data.get("username")
    uid = request.data.get("uid")
    password = request.data.get("password")
    confirm_password = request.data.get("confirm_password")

    if not all([fullname, email, username, uid, password, confirm_password]):
        return Response({"message": "All fields are required."}, status=400)
    if password != confirm_password:
        return Response({"message": "Passwords do not match."}, status=400)
    if User.objects.filter(username=username).exists():
        return Response({"message": "Username already exists."}, status=400)
    if User.objects.filter(uid=uid).exists():
        return Response({"message": "UID already exists."}, status=400)
    if User.objects.filter(email=email).exists():
        return Response({"message": "Email already exists."}, status=400)
    
    user = User.objects.create_user(
        username=username,
        email=email,
        uid=uid,
        password=password,
        role="admin",
        full_name=fullname,
        country="Unknown",
        status="active"
    )

    role = Roles.objects.get(role_name="sponsor_admin")

    userrole = UserRoles.objects.create(
        user=user,
        role=role
    )
    return Response({"message": "Sponsor account created successfully."}, status=201)


@api_view(["POST"])
def assign_sponsor_to_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized."}, status=403)

    if admin.userroles.filter(role__role_name='head_admin'):
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to create sponsor account."}, status=status.HTTP_403_FORBIDDEN)

    sponsor_username = request.data.get("sponsor_username")
    event_ids = request.data.get("event_ids", [])

    if not sponsor_username:
        return Response({"message": "sponsor_username required."}, status=400)

    if not isinstance(event_ids, list) or not all(isinstance(eid, int) for eid in event_ids):
        return Response({"message": "event_ids must be a list of integers"}, status=400)
    
    role = Roles.objects.get(role_name="sponsor_admin")
    
    sponsor = get_object_or_404(User, username=sponsor_username, role="admin", userroles__role=role)
    events = Event.objects.filter(event_id__in=event_ids)
    if not events.exists():
        return Response({"message": "No valid events found for the provided event_ids."}, status=400)
    for event in events:
        event.sponsor = sponsor

        SponsorEvent.objects.update_or_create(
            sponsor=sponsor,
            event=event
        )
    Event.objects.bulk_update(events, ["sponsor"])



    return Response({"message": "Events assigned to sponsor successfully."}, status=200)


@api_view(["GET"])
def get_all_sponsors(request):
    role = Roles.objects.get(role_name="sponsor_admin")
    sponsors = User.objects.filter(role="admin", userroles__role=role).values("user_id", "username", "email", "full_name")
    return Response(list(sponsors), status=200)



@api_view(["POST"])
def get_list_of_players_in_sponsor_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    sponsor = validate_token(auth.split(" ")[1])

    role = Roles.objects.get(role_name="sponsor_admin")
    if not sponsor or sponsor.role != "admin" or not sponsor.userroles.filter(role=role).exists():
        return Response({"message": "Unauthorized."}, status=403)

    # use the sponsor to get all events they are connected to, then get all players in those events

    sponsor_events = SponsorEvent.objects.filter(sponsor=sponsor).select_related("event").values_list("event", flat=True)
    data = []
    for sponsor_event in sponsor_events:
        event = Event.objects.get(event_id=sponsor_event)
        if event.participant_type == "solo":
            competitors = RegisteredCompetitors.objects.filter(event=event, user__isnull=False).select_related("user")
            for comp in competitors:
                data.append({
                    "event_id": event.event_id,
                    "event_name": event.event_name,
                    "player_id": comp.id,
                    "player_username": comp.user.username,
                    "user_id_from_sponsor": comp.user_id_from_sponsor,
                    "status": comp.status,
                    "email": comp.user.email,
                })
        else:
            teams = TournamentTeam.objects.filter(event=event).prefetch_related("members__user")

            # Then use tournament teams to get all tournament team members in those teams and their user info and user id from sponsor
            for team in teams:
                members = TournamentTeamMember.objects.filter(tournament_team=team).select_related("user").prefetch_related("tournament_team__event")
                for member in members:
                    data.append({
                        "event_id": event.event_id,
                        "event_name": event.event_name,
                        "team_id": team.team.team_id,
                        "team_name": team.team.team_name,
                        "member_id": member.id,
                        "member_username": member.user.username,
                        "user_id_from_sponsor": member.user_id_from_sponsor,
                        "status": member.status,
                        "email": member.user.email,
                    })
    return Response(data, status=200)


@api_view(["POST"])
def edit_match_scoring_config(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Unauthorized."}, status=403)

    # NOTE: permission is finalised below, once the owning event is resolved via
    # match_id -> match.group.stage.event — org members with can_upload_results may edit a
    # match's scoring config for THEIR org's events.

    match_id = request.data.get("match_id")
    scoring_settings = request.data.get("scoring_settings")
    if not match_id:
        return Response({"message": "match_id required."}, status=400)
    if not isinstance(scoring_settings, dict):
        return Response({"message": "scoring_settings must be a dictionary."}, status=400)
    match = get_object_or_404(Match, match_id=match_id)

    # ── AUTH (event-scoped): event derived via match_id -> match.group.stage.event. AFC event
    # admins always pass; otherwise allow org members holding can_upload_results on the event's
    # owning org. Native (org=None) events stay admin-only.
    _cfg_event = match.group.stage.event if match.group else None
    if not _is_event_admin(admin) and not (_cfg_event and org_can_event(admin, "can_upload_results", _cfg_event)):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    match.scoring_settings = scoring_settings
    match.save(update_fields=["scoring_settings"])
    
    return Response({"message": "Match scoring settings updated successfully."}, status=200)

@api_view(["POST"])
def get_sponsor_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    admin = validate_token(auth.split(" ")[1])

    sponsor_username = request.data.get("sponsor_username")
    sponsor = get_object_or_404(User, username=sponsor_username, role="admin", userroles__role__role_name="sponsor_admin")

    if not sponsor:
        return Response({"message": "Invalid token."}, status=400)

    # get events that the sponsr is linked to
    sponsor_events = SponsorEvent.objects.filter(sponsor=sponsor).select_related("event")


    return Response({
        "sponsor_id": sponsor.user_id,
        "username": sponsor.username,
        "email": sponsor.email,
        "full_name": sponsor.full_name,
        "events": [
            {
                "event_id": se.event.event_id,
                "event_name": se.event.event_name
            } for se in sponsor_events
        ]
    }, status=200)


@api_view(["POST"])
def edit_sponsor_details(request):

    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Unauthorized."}, status=403)

    # allow admin OR sponsor_admin
    is_admin = admin.role == "admin"
    is_sponsor_admin = admin.userroles.filter(role__role_name="sponsor_admin").exists()

    if not (is_admin or is_sponsor_admin):
        return Response({"message": "Unauthorized."}, status=403)

    sponsor_username = request.data.get("sponsor_username")
    sponsor = get_object_or_404(User, username=sponsor_username, role="admin")

    role = Roles.objects.get(role_name="sponsor_admin")
    if not sponsor.userroles.filter(role=role).exists():
        return Response({"message": "User is not a sponsor admin."}, status=400)

    full_name = request.data.get("full_name")
    email = request.data.get("email")
    username = request.data.get("username")
    password = request.data.get("password")


    update_fields = []

    if full_name:
        sponsor.full_name = full_name
        update_fields.append("full_name")

    if email:
        if User.objects.filter(email=email).exclude(user_id=sponsor.user_id).exists():
            return Response({"message": "Email already in use."}, status=400)
        sponsor.email = email
        update_fields.append("email")

    if username:
        if User.objects.filter(username=username).exclude(user_id=sponsor.user_id).exists():
            return Response({"message": "Username already in use."}, status=400)
        sponsor.username = username
        update_fields.append("username")

    if password:
        sponsor.set_password(password)
        update_fields.append("password")

    if update_fields:
        sponsor.save(update_fields=update_fields)

    # ------------------------
    # Update events
    # ------------------------

    event_ids = request.data.get("event_ids")

    if event_ids is not None:

        if not isinstance(event_ids, list) or not all(isinstance(eid, int) for eid in event_ids):
            return Response({"message": "event_ids must be a list of integers"}, status=400)

        events = Event.objects.filter(event_id__in=event_ids)

        # remove old sponsor links
        SponsorEvent.objects.filter(sponsor=sponsor).exclude(event_id__in=event_ids).delete()

        for event in events:

            SponsorEvent.objects.update_or_create(
                sponsor=sponsor,
                event=event
            )

            
    return Response({
        "message": "Sponsor details updated successfully."
    }, status=200)


# @api_view(["POST"])
# def edit_roster(request):

#     # -------------------------
#     # AUTH
#     # -------------------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     if user.status != "active":
#         return Response({"message": "Your account is not active."}, status=403)

#     # -------------------------
#     # INPUT
#     # -------------------------
#     event_id = request.data.get("event_id")
#     team_id = request.data.get("team_id")

#     roster_member_ids = _maybe_json_list(request.data.get("roster_member_ids"))
#     sponsor_ids = _maybe_json(request.data.get("sponsor_ids"), default={})

#     if not event_id or not team_id:
#         return Response({"message": "event_id and team_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     team = get_object_or_404(Team, team_id=team_id)

#     if event.participant_type not in ["duo", "squad"]:
#         return Response({"message": "Roster editing only allowed for team events."}, status=400)

#     # -------------------------
#     # REGISTRATION WINDOW
#     # -------------------------
#     if date.today() > event.registration_end_date:
#         return Response({"message": "Registration period has ended. Roster cannot be edited."}, status=403)

#     # -------------------------
#     # PERMISSION CHECK
#     # -------------------------
#     if not _user_is_team_captain_or_owner(user, team):
#         return Response({"message": "Only captain/vice-captain/team owner can edit roster."}, status=403)

#     # -------------------------
#     # GET TOURNAMENT TEAM
#     # -------------------------
#     tt = TournamentTeam.objects.filter(event=event, team=team).first()

#     if not tt:
#         return Response({"message": "Team is not registered for this event."}, status=404)

#     # -------------------------
#     # ROSTER SIZE RULES
#     # -------------------------
#     if event.participant_type == "duo":
#         min_size, max_size = 2, 2
#     else:
#         min_size, max_size = 4, 6

#     if not roster_member_ids:
#         return Response({"message": "roster_member_ids is required."}, status=400)

#     roster_member_ids = list(dict.fromkeys(roster_member_ids))

#     if not (min_size <= len(roster_member_ids) <= max_size):
#         return Response({
#             "message": f"Roster must contain {min_size} to {max_size} players."
#         }, status=400)

#     # -------------------------
#     # VALIDATE TEAM MEMBERS
#     # -------------------------
#     team_member_ids = set(
#         TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
#     )

#     if not set(roster_member_ids).issubset(team_member_ids):
#         return Response({"message": "One or more roster players are not members of this team."}, status=400)

#     # -------------------------
#     # CHECK OTHER ROSTERS
#     # -------------------------
#     conflict_players = set(
#         TournamentTeamMember.objects.filter(
#             user_id__in=roster_member_ids,
#             tournament_team__event=event
#         ).exclude(tournament_team=tt).values_list("user_id", flat=True)
#     )

#     if conflict_players:
#         return Response({
#             "message": "One or more players are already in another roster.",
#             "user_ids": list(conflict_players)
#         }, status=409)

#     # -------------------------
#     # LOAD USERS
#     # -------------------------
#     roster_users = list(User.objects.filter(user_id__in=roster_member_ids))

#     roster_users_by_id = {u.user_id: u for u in roster_users}

#     missing_ids = [uid for uid in roster_member_ids if uid not in roster_users_by_id]

#     if missing_ids:
#         return Response({
#             "message": "Some users not found.",
#             "missing_user_ids": missing_ids
#         }, status=400)

#     # -------------------------
#     # UPDATE ROSTER
#     # -------------------------
#     with transaction.atomic():

#         # remove old roster
#         TournamentTeamMember.objects.filter(tournament_team=tt).delete()

#         rows = []

#         for uid in roster_member_ids:

#             sponsor_uid = None

#             if event.is_sponsored:
#                 sponsor_uid = sponsor_ids.get(str(uid))

#             rows.append(
#                 TournamentTeamMember(
#                     tournament_team=tt,
#                     user=roster_users_by_id[uid],
#                     event=event,
#                     user_id_from_sponsor=sponsor_uid,
#                     status="pending" if event.is_sponsored else "active"
#                 )
#             )

#         TournamentTeamMember.objects.bulk_create(rows, batch_size=200)

#     return Response({
#         "message": "Roster updated successfully.",
#         "tournament_team_id": tt.tournament_team_id,
#         "roster_size": len(rows)
#     }, status=200)

@api_view(["POST"])
def edit_roster(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    if user.status != "active":
        return Response({"message": "Your account is not active."}, status=403)

    # ---------------- INPUT ----------------
    event_id = request.data.get("event_id")
    team_id = request.data.get("team_id")
    roster_member_ids = _maybe_json_list(request.data.get("roster_member_ids"))
    sponsor_ids = _maybe_json(request.data.get("sponsor_ids"), default={})

    if not event_id or not team_id:
        return Response({"message": "event_id and team_id required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    team = get_object_or_404(Team, team_id=team_id)

    # ---------------- STAFF (MANAGER) OVERRIDE FLAG ----------------
    # Feature "staff-edit-roster-after-close" (2026-06-10): AFC staff must be able to
    # CORRECT a team's roster even after registration closes (e.g. a team registered the
    # wrong player and the captain can no longer self-serve once the window shut). A
    # "manager" is an AFC event admin OR an organizer with can_manage_registrations on
    # the event's owning org. This is the SAME org-aware gate already used by
    # disqualify_team / confirm_player / reject_player in this file (~L15898), reused here
    # so registration-management authority is consistent across the roster state machine.
    # The override only relaxes the registration-window and captain/owner guards below;
    # it deliberately does NOT bypass the match-start lock, and the sponsor re-approval
    # re-derivation (the "sponsor-edit-roster" bug fix) still runs, so a manager's
    # post-close swap still reopens the team for sponsor review.
    is_manager = _is_event_admin(user) or org_can_event(user, "can_manage_registrations", event)

    # ---------------- REGISTRATION WINDOW ----------------
    # Managers may edit after close (staff correction); a normal captain/owner cannot.
    if date.today() > event.registration_end_date and not is_manager:
        return Response({"message": "Registration closed. Cannot edit roster."}, status=403)

    # ---------------- MATCH START CHECK ----------------
    # NOT bypassed for managers: editing a roster after any match has results would orphan
    # the match stats, so this lock applies to everyone, staff included.
    if Match.objects.filter(group__stage__event=event, result_inputted=True).exists():
        return Response({
            "message": "Roster cannot be edited after matches have started."
        }, status=403)

    # ---------------- PERMISSION ----------------
    # A manager who is NOT on the team can still edit it (staff correction); otherwise the
    # editor must be the team's captain/owner.
    if not (_user_is_team_captain_or_owner(user, team) or is_manager):
        return Response({"message": "Only captain/owner can edit roster."}, status=403)

    tt = TournamentTeam.objects.filter(event=event, team=team).first()

    if not tt:
        return Response({"message": "Team not registered."}, status=404)

    # ---------------- ROSTER RULES ----------------
    if event.participant_type == "duo":
        min_size, max_size = 2, 2
    else:
        min_size, max_size = 4, 6

    roster_member_ids = list(dict.fromkeys(roster_member_ids or []))

    if not (min_size <= len(roster_member_ids) <= max_size):
        return Response({
            "message": f"Roster must contain {min_size}-{max_size} players."
        }, status=400)

    # ---------------- VALIDATE TEAM MEMBERS ----------------
    team_member_ids = set(
        TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
    )

    if not set(roster_member_ids).issubset(team_member_ids):
        return Response({"message": "Roster players must belong to team."}, status=400)

    # ---------------- LOAD USERS ----------------
    users = User.objects.filter(user_id__in=roster_member_ids)
    users_by_id = {u.user_id: u for u in users}

    # ---------------- TEAM COUNTRY + RESTRICTION ----------------
    roster_users = list(users_by_id.values())

    team_country = determine_team_country(roster_users, user)

    if not _passes_event_country_restriction(event, team_country):
        return Response({
            "message": "Your team is not eligible for this event based on country restriction.",
            "team_country": team_country
        }, status=403)

    missing = [uid for uid in roster_member_ids if uid not in users_by_id]

    if missing:
        return Response({
            "message": "Some users do not exist.",
            "missing_user_ids": missing
        }, status=400)

    # ---------------- SPONSOR VALIDATION ----------------
    if event.is_sponsored:

        sponsor_values = [
            sponsor_ids.get(str(uid))
            for uid in roster_member_ids
            if sponsor_ids.get(str(uid))
        ]

        # duplicates inside request
        if len(sponsor_values) != len(set(sponsor_values)):
            return Response({
                "message": "Duplicate sponsor IDs in roster."
            }, status=400)

        # duplicates already used in event
        existing_ids = set(
            TournamentTeamMember.objects.filter(
                tournament_team__event=event,
                user_id_from_sponsor__in=sponsor_values
            ).exclude(tournament_team=tt).values_list(
                "user_id_from_sponsor", flat=True
            )
        )

        if existing_ids:
            return Response({
                "message": "Some sponsor IDs already exist in this event.",
                "conflicting_ids": list(existing_ids)
            }, status=409)

    # ---------------- EXISTING ROSTER ----------------
    existing_members = list(
        TournamentTeamMember.objects.filter(tournament_team=tt)
    )

    existing_ids = {m.user_id for m in existing_members}
    new_ids = set(roster_member_ids)

    removed_ids = existing_ids - new_ids
    added_ids = new_ids - existing_ids
    kept_ids = existing_ids & new_ids

    with transaction.atomic():

        # ---------------- REMOVE PLAYERS ----------------
        # BUG FIX ("sponsor-edit-roster", 2026-06-10): this block used to 403 when a
        # removed player was "active"/"approved" ("Cannot remove confirmed player ...").
        # Because the whole edit is one transaction.atomic(), that 403 rolled back the
        # ENTIRE edit, so the team's NEW roster never saved and the OLD approved roster
        # (and the sponsor dashboard reading it) stayed stale. Owner decision is
        # "allow + re-approve changes": removing an approved player is now allowed; the
        # team is reopened for sponsor re-review afterwards (see check_and_activate_team
        # call at the end of this transaction).
        for member in existing_members:

            if member.user_id in removed_ids:
                member.delete()

        # ---------------- UPDATE EXISTING PLAYERS ----------------
        for member in existing_members:

            if member.user_id in kept_ids:

                sponsor_uid = None
                if event.is_sponsored:
                    sponsor_uid = sponsor_ids.get(str(member.user_id))

                if sponsor_uid != member.user_id_from_sponsor:

                    # BUG FIX ("sponsor-edit-roster", 2026-06-10): a kept player whose
                    # sponsor id CHANGED used to 403 if "active" ("Cannot change sponsor
                    # ID for confirmed player ..."), again rolling back the whole edit.
                    # Per the "allow + re-approve" decision we now ALLOW the change for
                    # any status and reset THAT member to "pending": the new sponsor id
                    # was never approved, so it must be re-reviewed. (A kept player whose
                    # sponsor id is UNCHANGED never enters this branch and keeps its
                    # status, so an already-active player stays active.)
                    member.user_id_from_sponsor = sponsor_uid

                    # The changed id needs sponsor re-approval -> back to "pending"
                    # (covers active -> pending and rejected -> pending alike).
                    member.status = "pending"
                    # member.reason = None

                    member.save(update_fields=["user_id_from_sponsor", "status"])

        # ---------------- ADD NEW PLAYERS ----------------
        new_rows = []

        for uid in added_ids:

            sponsor_uid = None
            if event.is_sponsored:
                sponsor_uid = sponsor_ids.get(str(uid))

            new_rows.append(
                TournamentTeamMember(
                    tournament_team=tt,
                    user=users_by_id[uid],
                    event=event,
                    user_id_from_sponsor=sponsor_uid,
                    status="pending" if event.is_sponsored else "active"
                )
            )

        if new_rows:
            TournamentTeamMember.objects.bulk_create(new_rows)
        tt.country = team_country
        tt.save(update_fields=["country"])

        # ---------------- RE-DERIVE TEAM APPROVAL STATE ----------------
        # BUG FIX ("sponsor-edit-roster", 2026-06-10): after the member writes above the
        # team's status could be STALE. If the edit added/swapped a player (new member
        # "pending") or changed a kept player's sponsor id (that member reset to
        # "pending"), an already-approved team must NOT silently remain "active". We
        # re-derive the team state from the live member statuses via the now-bidirectional
        # check_and_activate_team:
        #   - any member not "active" -> team "pending" + RegisteredCompetitors un-registered
        #     (reopens the team for sponsor re-review; the sponsor dashboard, which reads
        #     these live TournamentTeamMember rows, then shows the NEW roster).
        #   - all members still "active" (e.g. an unrelated no-op edit) -> team stays
        #     "active" + RC "registered" (idempotent refresh, no duplicate emails).
        # For a NON-sponsored event new members are created "active", so a non-sponsored
        # team that was active stays active. Done inside the transaction so the team
        # status and the roster commit atomically.
        check_and_activate_team(tt)

    return Response({
        "message": "Roster updated successfully.",
        "added_players": list(added_ids),
        "removed_players": list(removed_ids),
        "kept_players": list(kept_ids)
    }, status=200)


# @api_view(["POST"])
# def edit_roster(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid token."}, status=400)

#     user = validate_token(auth.split(" ")[1])
#     if not user:
#         return Response({"message": "Invalid session."}, status=401)

#     if user.status != "active":
#         return Response({"message": "Your account is not active."}, status=403)

#     # ---------------- INPUT ----------------
#     event_id = request.data.get("event_id")
#     team_id = request.data.get("team_id")

#     roster_member_ids = _maybe_json_list(request.data.get("roster_member_ids"))
#     sponsor_ids = _maybe_json(request.data.get("sponsor_ids"), default={})

#     if not event_id or not team_id:
#         return Response({"message": "event_id and team_id required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     team = get_object_or_404(Team, team_id=team_id)

#     if date.today() > event.registration_end_date:
#         return Response({"message": "Registration closed. Cannot edit roster."}, status=403)

#     if not _user_is_team_captain_or_owner(user, team):
#         return Response({"message": "Only captain/owner can edit roster."}, status=403)

#     tt = TournamentTeam.objects.filter(event=event, team=team).first()

#     if not tt:
#         return Response({"message": "Team not registered."}, status=404)

#     # ---------------- ROSTER RULES ----------------
#     if event.participant_type == "duo":
#         min_size, max_size = 2, 2
#     else:
#         min_size, max_size = 4, 6

#     roster_member_ids = list(dict.fromkeys(roster_member_ids))

#     if not (min_size <= len(roster_member_ids) <= max_size):
#         return Response({
#             "message": f"Roster must contain {min_size}-{max_size} players."
#         }, status=400)

#     # ---------------- VALIDATE TEAM MEMBERS ----------------
#     team_member_ids = set(
#         TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
#     )

#     if not set(roster_member_ids).issubset(team_member_ids):
#         return Response({"message": "Roster players must belong to team."}, status=400)

#     # ---------------- LOAD USERS ----------------
#     users = User.objects.filter(user_id__in=roster_member_ids)
#     users_by_id = {u.user_id: u for u in users}

#     missing = [uid for uid in roster_member_ids if uid not in users_by_id]

#     if missing:
#         return Response({
#             "message": "Some users do not exist.",
#             "missing_user_ids": missing
#         }, status=400)

#     # ---------------- EXISTING ROSTER ----------------
#     existing_members = list(
#         TournamentTeamMember.objects.filter(tournament_team=tt)
#     )

#     existing_ids = {m.user_id for m in existing_members}
#     new_ids = set(roster_member_ids)

#     removed_ids = existing_ids - new_ids
#     added_ids = new_ids - existing_ids
#     kept_ids = existing_ids & new_ids

#     with transaction.atomic():

#         # ---------------- REMOVE PLAYERS ----------------
#         for member in existing_members:

#             if member.user_id in removed_ids:

#                 if member.status in ["active", "approved"]:
#                     return Response({
#                         "message": f"Cannot remove confirmed player {member.user.username}"
#                     }, status=403)

#                 member.delete()

#         # ---------------- UPDATE KEPT PLAYERS ----------------
#         for member in existing_members:

#             if member.user_id in kept_ids:

#                 sponsor_uid = None
#                 if event.is_sponsored:
#                     sponsor_uid = sponsor_ids.get(str(member.user_id))

#                 if sponsor_uid != member.user_id_from_sponsor:
#                     member.user_id_from_sponsor = sponsor_uid
#                     member.save(update_fields=["user_id_from_sponsor"])

#         # ---------------- ADD NEW PLAYERS ----------------
#         new_rows = []

#         for uid in added_ids:

#             sponsor_uid = None
#             if event.is_sponsored:
#                 sponsor_uid = sponsor_ids.get(str(uid))

#             new_rows.append(
#                 TournamentTeamMember(
#                     tournament_team=tt,
#                     user=users_by_id[uid],
#                     event=event,
#                     user_id_from_sponsor=sponsor_uid,
#                     status="pending" if event.is_sponsored else "active"
#                 )
#             )

#         if new_rows:
#             TournamentTeamMember.objects.bulk_create(new_rows)

#     return Response({
#         "message": "Roster updated successfully.",
#         "added_players": list(added_ids),
#         "removed_players": list(removed_ids),
#         "kept_players": list(kept_ids)
#     }, status=200)


@api_view(["POST"])
def get_roster_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)
    if user.status != "active":
        return Response({"message": "Your account is not active."}, status=403)
    event_id = request.data.get("event_id")

    if not event_id:
        return Response({"message": "event_id required."}, status=400)
    event = get_object_or_404(Event, event_id=event_id)
    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for team events only."}, status=400
    )
    team = Team.objects.filter(memberships__member=user).first()
    if not team:
        return Response({"message": "You are not part of any team."}, status=404)
    tournament_teams = TournamentTeam.objects.filter(event=event, team=team).select_related("team")
    if not tournament_teams.exists():
        return Response({"message": "Your team is not registered for this event."}, status=404)
    tournament_team = tournament_teams.first()
    members = TournamentTeamMember.objects.filter(tournament_team=tournament_team).select_related("user")
    roster = []
    for member in members:
        roster.append({
            "user_id": member.user.user_id,
            "username": member.user.username,
            "full_name": member.user.full_name,
            "user_id_from_sponsor": member.user_id_from_sponsor,
            "status": member.status,
            "reason": member.reason
        })
    return Response({
        "event_id": event.event_id,
        "event_name": event.event_name,
        "team_id": team.team_id,
        "team_name": team.team_name,
        "roster": roster
    }, status=200)


# @api_view(["POST"])
# def kick_team_from_event(request):


from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["GET"])
def total_members_this_month(request):
    now = timezone.now()

    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    count = User.objects.filter(
        created_at__gte=start_of_month
    ).count()

    return Response({
        "total_members_this_month": count
    })

@api_view(["GET"])
def total_teams_this_month(request):
    now = timezone.now()

    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    count = Team.objects.filter(
        creation_date__gte=start_of_month
    ).count()

    return Response({
        "total_teams_this_month": count
    })

from django.utils import timezone

@api_view(["GET"])
def total_active_tournaments(request):

    now = timezone.now()

    count = Event.objects.filter(
        competition_type="tournament",
        start_date__lte=now,
        end_date__gte=now
    ).count()

    return Response({
        "total_active_tournaments": count
    })


@api_view(["GET"])
def total_published_news(request):
    count = News.objects.all().count()

    return Response({
        "total_published_news": count
    })


def _extract_results_from_image(image_file, participant_type):
    """
    Send a match result screenshot to GPT-4o and return structured JSON.
    Returns a list of dicts. Raises ValueError if extraction fails.
    """
    import base64
    import openai as _openai

    image_bytes = image_file.read()
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    # Detect mime type from first bytes
    mime = "image/jpeg"
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        mime = "image/png"
    elif image_bytes[:4] == b'RIFF':
        mime = "image/webp"

    if participant_type == "solo":
        schema_hint = (
            'a JSON array where each element is: '
            '{"placement": <int>, "name": "<ign>", "kills": <int>}'
        )
        extra = "Each row is one player."
    else:
        schema_hint = (
            'a JSON array where each element is: '
            '{"placement": <int>, "players": [{"name": "<ign>", "kills": <int>}, ...]}'
        )
        extra = "Group players by their team (same placement number = same team)."

    prompt = (
        f"This is a Free Fire match result screen. "
        f"Extract all visible results and return ONLY {schema_hint}. "
        f"{extra} "
        "Use the exact in-game names shown. "
        "The 'kills' field is the Eliminations count shown next to each player. "
        "Return only valid JSON with no markdown, no explanation."
    )

    client = _openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        max_tokens=2000,
    )

    raw = resp.choices[0].message.content.strip()
    # Strip possible markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _merge_team_results(all_results):
    """Merge results from multiple images, deduplicating by placement."""
    seen = {}
    for team in all_results:
        p = team.get("placement")
        if p and p not in seen:
            seen[p] = team
    return list(seen.values())


def _merge_solo_results(all_results):
    """Merge solo results from multiple images, deduplicating by placement."""
    seen = {}
    for row in all_results:
        p = row.get("placement")
        if p and p not in seen:
            seen[p] = row
    return list(seen.values())


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def upload_match_result_image(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, once the owning event is resolved via
    # match_id -> match.group.stage.event — org members with can_upload_results may run OCR
    # result uploads for THEIR org's events.

    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    images = request.FILES.getlist("images")
    if not images:
        return Response({"message": "At least one image file is required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)

    if not match.group:
        return Response({"message": "Match is not linked to a group."}, status=400)

    event = match.group.stage.event
    participant_type = event.participant_type  # "solo", "duo", or "squad"

    # ── AUTH (event-scoped): event derived via match_id -> match.group.stage.event. AFC event
    # admins always pass; otherwise allow org members holding can_upload_results on the event's
    # owning org. Native (org=None) events stay admin-only.
    if not _is_event_admin(admin) and not org_can_event(admin, "can_upload_results", event):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    # -------- LEADERBOARD / SCORING --------
    leaderboard = match.leaderboard or Leaderboard.objects.filter(
        event=event, stage=match.group.stage, group=match.group
    ).first()

    if not leaderboard:
        return Response({"message": "No leaderboard found for this group. Create one first."}, status=400)

    try:
        placement_points = {int(k): int(v) for k, v in (leaderboard.placement_points or {}).items()}
    except Exception:
        return Response({"message": "Leaderboard placement_points must be a JSON object like {'1':12,'2':9,...}"}, status=400)

    if not placement_points:
        placement_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

    kill_point = float(getattr(leaderboard, "kill_point", 1.0) or 1.0)

    # -------- EXTRACT RESULTS FROM EACH IMAGE --------
    all_raw = []
    extraction_errors = []

    for img_file in images:
        img_file.seek(0)
        try:
            extracted = _extract_results_from_image(img_file, participant_type)
            if isinstance(extracted, list):
                all_raw.extend(extracted)
        except Exception as e:
            extraction_errors.append(str(e))

    if not all_raw and extraction_errors:
        return Response({
            "message": "Failed to extract results from image(s).",
            "errors": extraction_errors,
        }, status=400)

    if not all_raw:
        return Response({"message": "No results could be extracted from the provided image(s)."}, status=400)

    # -------- INSERT STATS --------
    if participant_type == "solo":
        merged = _merge_solo_results(all_raw)
        merged.sort(key=lambda r: r.get("placement", 999))

        reg_qs = RegisteredCompetitors.objects.select_related("user").filter(
            event=event, status="registered"
        )
        # Build case-insensitive name → competitor map
        name_to_rc = {rc.user.username.lower(): rc for rc in reg_qs if rc.user}

        stats_to_create = []
        unmatched = []

        for row in merged:
            name = (row.get("name") or "").strip()
            placement = row.get("placement")
            kills = int(row.get("kills") or 0)

            rc = name_to_rc.get(name.lower())
            if not rc:
                unmatched.append({"name": name, "placement": placement})
                continue

            # Shared solo formula. NOTE: drops the old int(round(...)) on kill points in favour
            # of scoring's int() truncation so every call site agrees (identical at kill_point=1.0).
            pts = scoring_lib.compute_solo_points(
                placement_points=placement_points, kill_point=kill_point,
                placement=placement, kills=kills, played=True,
            )
            placement_pts = pts["placement_points"]
            kill_pts = pts["kill_points"]
            total_pts = pts["total_points"]

            stats_to_create.append(
                SoloPlayerMatchStats(
                    match=match,
                    competitor=rc,
                    placement=placement,
                    kills=kills,
                    placement_points=placement_pts,
                    kill_points=kill_pts,
                    total_points=total_pts,
                )
            )

        with transaction.atomic():
            SoloPlayerMatchStats.objects.filter(match=match).delete()
            SoloPlayerMatchStats.objects.bulk_create(stats_to_create, batch_size=500)
            match.result_inputted = True
            match.save(update_fields=["result_inputted"])

        saved_count = len(stats_to_create)

    else:
        # Team (duo/squad)
        merged = _merge_team_results(all_raw)
        merged.sort(key=lambda t: t.get("placement", 999))

        # Fetch all members for this event and build a case-insensitive name map
        all_members = TournamentTeamMember.objects.select_related(
            "tournament_team", "user"
        ).filter(tournament_team__event=event)

        name_to_member = {m.user.username.lower(): m for m in all_members if m.user}

        team_stats_to_create = []
        player_stats_to_create = []
        unmatched = []

        with transaction.atomic():
            TournamentPlayerMatchStats.objects.filter(team_stats__match=match).delete()
            TournamentTeamMatchStats.objects.filter(match=match).delete()

            for team_data in merged:
                placement = team_data.get("placement")
                players = team_data.get("players", [])

                # Identify the TournamentTeam via first matched player
                team_obj = None
                for p in players:
                    member = name_to_member.get((p.get("name") or "").lower())
                    if member:
                        team_obj = member.tournament_team
                        break

                if not team_obj:
                    unmatched.append({
                        "placement": placement,
                        "players": [p.get("name") for p in players],
                    })
                    continue

                total_kills = sum(int(p.get("kills") or 0) for p in players)
                # Shared team formula (image-upload path: no assists/damage/bonus/penalty -> 0).
                # NOTE: drops the old int(round(...)) on kill points in favour of scoring's int()
                # truncation so every call site agrees (identical at the default kill_point=1.0).
                pts = scoring_lib.compute_team_points(
                    placement_points=placement_points, kill_point=kill_point,
                    points_per_assist=0, points_per_1000_damage=0,
                    placement=placement, kills=total_kills, damage=0, assists=0,
                    bonus=0, penalty=0, played=True,
                )
                placement_pts = pts["placement_points"]
                kill_pts = pts["kill_points"]
                total_pts = pts["total_points"]

                team_stats_to_create.append(
                    TournamentTeamMatchStats(
                        match=match,
                        tournament_team=team_obj,
                        placement=placement,
                        kills=total_kills,
                        damage=0,
                        assists=0,
                        placement_points=placement_pts,
                        kill_points=kill_pts,
                        total_points=total_pts,
                    )
                )

            created_team_stats = TournamentTeamMatchStats.objects.bulk_create(
                team_stats_to_create, batch_size=200
            )
            ts_map = {ts.tournament_team_id: ts.team_stats_id for ts in created_team_stats}

            for team_data in merged:
                for p in team_data.get("players", []):
                    member = name_to_member.get((p.get("name") or "").lower())
                    if not member:
                        unmatched.append({"name": p.get("name")})
                        continue
                    ts_id = ts_map.get(member.tournament_team_id)
                    if not ts_id:
                        continue
                    player_stats_to_create.append(
                        TournamentPlayerMatchStats(
                            team_stats_id=ts_id,
                            player_id=member.user_id,
                            kills=int(p.get("kills") or 0),
                            damage=0,
                            assists=0,
                        )
                    )

            TournamentPlayerMatchStats.objects.bulk_create(player_stats_to_create, batch_size=500)
            match.result_inputted = True
            match.save(update_fields=["result_inputted"])

        saved_count = len(team_stats_to_create)

    # -------- SAVE IMAGES FOR REFERENCE --------
    saved_images = []
    note = request.data.get("note", "")
    for img_file in images:
        img_file.seek(0)
        obj = MatchResultImage.objects.create(
            match=match,
            image=img_file,
            uploaded_by=admin,
            note=note or None,
        )
        saved_images.append({"image_id": obj.image_id, "url": obj.image.url})

    return Response({
        "message": "Match results extracted from image(s) and saved.",
        "match_id": match.match_id,
        "participant_type": participant_type,
        "teams_or_players_saved": saved_count,
        "unmatched": unmatched,
        "extraction_errors": extraction_errors,
        "images": saved_images,
    }, status=201)


@api_view(["POST"])
def get_match_result_images(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, once the owning event is resolved via
    # match_id -> match.group.stage.event. This is a READ, but result images are admin/organizer
    # surface only, so we still scope it: org members managing THEIR org's results may view them.

    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)

    # ── AUTH (event-scoped, read): event derived via match_id -> match.group.stage.event. AFC
    # event admins always pass; otherwise allow org members holding can_upload_results on the
    # event's owning org (the same role that manages results may view their evidence images).
    # Native (org=None) events stay admin-only.
    _img_event = match.group.stage.event if match.group else None
    if not _is_event_admin(admin) and not (_img_event and org_can_event(admin, "can_upload_results", _img_event)):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    images = MatchResultImage.objects.filter(match=match).order_by("uploaded_at")

    data = [
        {
            "image_id": img.image_id,
            "image_url": request.build_absolute_uri(img.image.url),
            "note": img.note,
            "uploaded_by": img.uploaded_by.username if img.uploaded_by else None,
            "uploaded_at": img.uploaded_at,
        }
        for img in images
    ]

    return Response({"match_id": match.match_id, "images": data})


@api_view(["DELETE"])
def delete_match_result_image(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # NOTE: permission is finalised below, once the owning event is resolved via the image's
    # match (image -> match -> group.stage.event) — org members with can_upload_results may
    # delete result images for THEIR org's events.

    image_id = request.data.get("image_id")
    if not image_id:
        return Response({"message": "image_id is required."}, status=400)

    img = get_object_or_404(MatchResultImage, image_id=image_id)

    # ── AUTH (event-scoped): event derived via image_id -> img.match.group.stage.event. AFC
    # event admins always pass; otherwise allow org members holding can_upload_results on the
    # event's owning org. Native (org=None) events stay admin-only.
    _img_event = img.match.group.stage.event if (img.match and img.match.group) else None
    if not _is_event_admin(admin) and not (_img_event and org_can_event(admin, "can_upload_results", _img_event)):
        return Response({"message": "You do not have permission to manage results for this event."}, status=403)

    img.image.delete(save=False)
    img.delete()


# ── Event Actions ──────────────────────────────────────────────────────────────

def _get_event_action_user(request):
    """Validate auth and return (user, error_response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    allowed = user.role in ["admin", "moderator", "support"]
    if not allowed:
        allowed = user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists()
    if not allowed:
        return None, Response({"message": "You do not have permission to perform this action."}, status=403)
    return user, None


def _notify_all_registered(event, title, message):
    """Create in-app Notifications for every active registered competitor."""
    notified = set()
    if event.participant_type == "solo":
        for rc in RegisteredCompetitors.objects.select_related("user").filter(
            event=event, status__in=["registered", "approved"]
        ):
            if rc.user and rc.user_id not in notified:
                notified.add(rc.user_id)
                Notifications.objects.create(
                    user=rc.user, title=title, message=message, related_event=event
                )
    else:
        for tt in TournamentTeam.objects.filter(event=event, status="active"):
            for member in TournamentTeamMember.objects.select_related("user").filter(
                tournament_team=tt, status__in=["active", "approved"]
            ):
                if member.user and member.user_id not in notified:
                    notified.add(member.user_id)
                    Notifications.objects.create(
                        user=member.user, title=title, message=message, related_event=event
                    )
    return len(notified)


@api_view(["POST"])
def cancel_event(request):
    user, err = _get_event_action_user(request)
    if err:
        return err

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    if event.event_status in ["cancelled", "completed"]:
        return Response({"message": f"Event is already {event.event_status}."}, status=400)

    event.event_status = "cancelled"
    event.save(update_fields=["event_status"])

    count = _notify_all_registered(
        event,
        title=f"Event Cancelled: {event.event_name}",
        message=(
            f"The event '{event.event_name}' has been cancelled. "
            "We apologize for the inconvenience. Registrations have been frozen."
        ),
    )

    AdminHistory.objects.create(
        admin_user=user,
        action="cancel_event",
        description=f"Cancelled event {event.event_name} (ID: {event.event_id})",
    )

    return Response(
        {"message": f"Event '{event.event_name}' has been cancelled.", "notifications_sent": count},
        status=200,
    )


@api_view(["POST"])
def complete_event(request):
    user, err = _get_event_action_user(request)
    if err:
        return err

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    if event.event_status in ["completed", "cancelled"]:
        return Response({"message": f"Event is already {event.event_status}."}, status=400)

    event.event_status = "completed"
    event.save(update_fields=["event_status"])

    # ── Event linking (feature "event-linking" P1): completing a source event FIRES any of its
    # still-active qualification links (the safety net behind the manual Fire button) - the top
    # N of each linked stage flow into their target events. Best-effort: a linking hiccup must
    # never block the completion itself. Lazy import avoids a module-load cycle.
    try:
        from .event_links import fire_links_for_event
        fire_links_for_event(event, user)
    except Exception:
        pass

    count = _notify_all_registered(
        event,
        title=f"Tournament Complete: {event.event_name}",
        message=(
            f"The tournament '{event.event_name}' has officially concluded. "
            "Results are now locked. Thank you for participating!"
        ),
    )

    AdminHistory.objects.create(
        admin_user=user,
        action="complete_event",
        description=f"Marked event {event.event_name} (ID: {event.event_id}) as completed",
    )

    return Response(
        {"message": f"Event '{event.event_name}' has been marked as complete.", "notifications_sent": count},
        status=200,
    )


@api_view(["POST"])
def broadcast_announcement(request):
    user, err = _get_event_action_user(request)
    if err:
        return err

    event_id = request.data.get("event_id")
    title = request.data.get("title", "").strip()
    message = request.data.get("message", "").strip()

    if not event_id or not title or not message:
        return Response({"message": "event_id, title, and message are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    count = _notify_all_registered(event, title=title, message=message)

    AdminHistory.objects.create(
        admin_user=user,
        action="broadcast_announcement",
        description=f"Broadcast announcement to event {event.event_name} (ID: {event.event_id}): {title}",
    )

    return Response({"message": f"Announcement sent to {count} users.", "recipients": count}, status=200)


# ── per-GROUP broadcast (AFC official + organizer) ─────────────────────────────
# Why this exists: the event-edit "Stages & Groups" tab (StagesGroupsTab.tsx, reused
# by BOTH the AFC admin edit page and the organizer edit page) had an UNLABELLED bell
# icon per group that only fired the fixed room-details push (send_match_room_details_
# notification_to_competitor) AND was hard-gated to admins-only (so it 403'd for
# organizers). The user asked for a clearer per-group broadcast on both surfaces. This
# endpoint backs the upgraded SendNotificationModal "Message group" composer.
#
# Difference vs the two neighbours:
#   - broadcast_announcement (above) = event-WIDE, custom title/message, AFC-staff gate.
#   - send_match_room_details_notification_to_competitor = per-group, FIXED room text,
#     admin-only, one Notification PER MATCH per user (spammy).
#   - broadcast_to_group (this) = per-GROUP, EITHER a custom title/message OR an
#     auto room-details summary, deduped to ONE Notification per recipient, and gated
#     so AFC event admins OR an organizer who can edit THIS event can use it.
#
# Recipients = every player (solo) or team-member (squad) currently slotted into the
# given StageGroup (via StageGroupCompetitor), deduped by user.
# Consumed by: frontend app/(a)/a/events/_components/SendNotificationModal.tsx
#   (rendered inside StagesGroupsTab on /a/events/<slug>/edit and
#    /organizer/events/<slug>/edit).
def _group_recipient_users(event, group):
    """Deduped list of Users in a StageGroup. Solo -> each competitor's user;
    team/squad -> each member of each team in the group. Mirrors the recipient
    resolution in send_match_room_details_notification_to_competitor but returns a
    de-duplicated set so a custom broadcast lands once per person."""
    users = {}
    if event.participant_type == "solo":
        competitors = (StageGroupCompetitor.objects
                       .select_related("player__user")
                       .filter(stage_group=group, player__isnull=False))
        for sc in competitors:
            u = sc.player.user
            if u and u.user_id not in users:
                users[u.user_id] = u
    else:
        teams = (StageGroupCompetitor.objects
                 .select_related("tournament_team")
                 .filter(stage_group=group, tournament_team__isnull=False))
        for sgc in teams:
            for m in sgc.tournament_team.members.select_related("user").all():
                if m.user and m.user.user_id not in users:
                    users[m.user.user_id] = m.user
    return list(users.values())


def _group_room_details_text(event, group):
    """Build a single room-details summary message for a whole group (all its maps
    that have room info set), or None when no map has room details yet."""
    matches = Match.objects.filter(group=group).order_by("match_number")
    blocks = []
    for match in matches:
        if match.room_id and match.room_name and match.room_password:
            blocks.append(
                f"Match {match.match_number}"
                f"{f' ({match.match_map})' if match.match_map else ''}:\n"
                f"  Room ID: {match.room_id}\n"
                f"  Room Name: {match.room_name}\n"
                f"  Password: {match.room_password}"
            )
    if not blocks:
        return None
    header = (
        f"Room details for '{event.event_name}'\n"
        f"Stage: {group.stage.stage_name}\n"
        f"Group: {group.group_name}\n"
    )
    return header + "\n" + "\n\n".join(blocks)


@api_view(["POST"])
def broadcast_to_group(request):
    """POST /events/broadcast-to-group/
    Body: { event_id, group_id, mode: 'custom'|'room_details', title?, message? }
      - mode='custom'       -> sends {title?, message} (message required).
      - mode='room_details' -> auto-builds the group's room-details summary.
    Auth: Bearer token; allowed for AFC event admins (_is_event_admin) OR an organizer
    who can edit this event (org_can_event(can_edit_events)). Sends ONE in-app
    Notification per recipient (deduped). Returns { message, recipients }.
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    event_id = request.data.get("event_id")
    group_id = request.data.get("group_id")
    if not event_id or not group_id:
        return Response({"message": "event_id and group_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    # group must belong to a stage of THIS event (stops a caller broadcasting into a
    # group from a different event by smuggling a foreign group_id).
    group = get_object_or_404(StageGroups, group_id=group_id, stage__event=event)

    # AUTH: AFC event admin OR an organizer who can edit this event (owner always can).
    # Native (org=None) events fall to AFC-admin-only via org_can_event.
    if not (_is_event_admin(user) or org_can_event(user, "can_edit_events", event)):
        return Response(
            {"message": "You do not have permission to message this group."}, status=403
        )

    mode = (request.data.get("mode") or "custom").strip()

    if mode == "room_details":
        title = f"Match Room Details: {event.event_name}"
        message = _group_room_details_text(event, group)
        if not message:
            return Response(
                {"message": "No room details have been set for this group's maps yet."},
                status=400,
            )
    else:
        title = (request.data.get("title") or "").strip()
        message = (request.data.get("message") or "").strip()
        if not message:
            return Response({"message": "A message is required."}, status=400)

    recipients = _group_recipient_users(event, group)
    if not recipients:
        return Response(
            {"message": "This group has no players to message yet.", "recipients": 0},
            status=400,
        )

    # ONE Notification per recipient (deduped), tagged group_broadcast + linked to the
    # event so it threads under the event in the user's notifications.
    Notifications.objects.bulk_create([
        Notifications(
            user=u,
            title=title or None,
            message=message,
            related_event=event,
            notification_type="group_broadcast",
        )
        for u in recipients
    ])

    AdminHistory.objects.create(
        admin_user=user,
        action="broadcast_to_group",
        description=(
            f"Group broadcast ({mode}) to {group.stage.stage_name} > {group.group_name} "
            f"in {event.event_name} (ID: {event.event_id}): {len(recipients)} recipients"
        ),
    )

    # Rich audit summary, e.g. 'Sent broadcast "time for the game" to 56 players in Finals > Group A
    # (Detty December)'. The full message body rides along in the expandable details.
    set_audit(
        request,
        (
            f"Sent broadcast \"{title or 'Group message'}\" to {len(recipients)} players in "
            f"{group.stage.stage_name} > {group.group_name} ({event.event_name})"
        ),
        recipients=len(recipients),
        message=message,
    )

    return Response(
        {"message": f"Message sent to {len(recipients)} players in {group.group_name}.",
         "recipients": len(recipients)},
        status=200,
    )


# ── export_participants renderers ──
# This endpoint takes its OWN `?format=csv|xlsx` business parameter. DRF, however,
# reserves the `format` query param for content-negotiation (format suffixes): if
# the value does not match any configured renderer's `.format`, DRF raises Http404
# DURING DISPATCH, so the view body never runs and the client gets a confusing
# 404 (`?format=csv` 404s, `?format=xlsx` 404s) instead of the export. To stop that
# collision we register passthrough renderers whose `.format` matches our own
# values, so negotiation always succeeds and our view builds the real HttpResponse.
# (Without this, the entire export feature is unreachable via its documented
# `?format=` contract.)
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.decorators import renderer_classes


class _PassthroughCSVRenderer(BaseRenderer):
    media_type = "text/csv"
    format = "csv"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


class _PassthroughXLSXRenderer(BaseRenderer):
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    format = "xlsx"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


# JSONRenderer LAST/included so the error Responses ({"message": ...}, 400) this
# view returns for bad input still serialize as JSON. The csv/xlsx renderers only
# exist to satisfy negotiation on `?format=csv|xlsx`; the happy path returns a raw
# HttpResponse and never uses them.
@api_view(["GET"])
@renderer_classes([JSONRenderer, _PassthroughCSVRenderer, _PassthroughXLSXRenderer])
def export_participants(request):
    # NOTE: openpyxl is imported LAZILY (inside the xlsx branch below), NOT here.
    # The CSV export uses only the stdlib `csv` module, so a prod host that is
    # missing openpyxl must still be able to export CSV. Importing openpyxl at the
    # top of the view made the ENTIRE endpoint 500 with ModuleNotFoundError on
    # prod even for csv requests. (openpyxl is listed in requirements*.txt so a
    # clean deploy installs it; this just stops a missing wheel from breaking csv.)
    import csv
    from io import BytesIO
    from django.http import HttpResponse, JsonResponse

    # Error responses use Django's JsonResponse rather than DRF Response. WHY:
    # this view advertises csv/xlsx passthrough renderers (see above) so the
    # `?format=` business param does not 404 in negotiation. But those passthrough
    # renderers would then mangle a DRF Response error body (rendering the dict as
    # raw bytes under text/csv). JsonResponse sidesteps DRF content negotiation
    # entirely, so every bad-input branch returns clean JSON with the right status.

    user, err = _get_event_action_user(request)
    if err:
        # err is a DRF Response built by the auth helper; re-emit it as JSON so the
        # csv/xlsx passthrough renderer cannot mangle the 400/401/403 body.
        return JsonResponse(err.data, status=err.status_code)

    event_id = request.query_params.get("event_id")
    fmt = request.query_params.get("format", "csv").lower()

    if not event_id:
        return JsonResponse({"message": "event_id is required."}, status=400)
    if fmt not in ("csv", "xlsx"):
        return JsonResponse({"message": "format must be csv or xlsx."}, status=400)

    # Explicit lookup instead of get_object_or_404 so a missing event returns a
    # clean JSON 404 (get_object_or_404 raises Http404, which DRF would otherwise
    # render through the negotiated csv/xlsx passthrough renderer).
    event = Event.objects.filter(event_id=event_id).first()
    if not event:
        return JsonResponse({"message": "Event not found."}, status=404)

    rows = []
    if event.participant_type == "solo":
        for rc in RegisteredCompetitors.objects.select_related("user").filter(event=event).order_by("registration_date"):
            u = rc.user
            rows.append({
                "Username": u.username if u else "",
                "Full Name": u.full_name if u else "",
                "Email": u.email if u else "",
                "Discord ID": u.discord_id if u else "",
                "Discord Username": u.discord_username if u else "",
                "Status": rc.status,
                "Waitlisted": rc.is_waitlisted,
                "Registration Date": rc.registration_date.strftime("%Y-%m-%d %H:%M") if rc.registration_date else "",
            })
    else:
        for tt in TournamentTeam.objects.select_related("team").filter(event=event).order_by("registration_date"):
            team_name = tt.team.team_name if tt.team else ""
            for member in TournamentTeamMember.objects.select_related("user").filter(tournament_team=tt):
                u = member.user
                rows.append({
                    "Team Name": team_name,
                    "Team Status": tt.status,
                    "Waitlisted": tt.is_waitlisted,
                    "Player Username": u.username if u else "",
                    "Player Full Name": u.full_name if u else "",
                    "Player Email": u.email if u else "",
                    "Discord ID": u.discord_id if u else "",
                    "Discord Username": u.discord_username if u else "",
                    "Member Status": member.status,
                    "Registration Date": tt.registration_date.strftime("%Y-%m-%d %H:%M") if tt.registration_date else "",
                })

    headers = list(rows[0].keys()) if rows else []
    safe_name = event.event_name.replace(" ", "_")

    if fmt == "xlsx":
        # Lazy import + graceful degradation: if openpyxl is not installed on this
        # host, do not 500 with ModuleNotFoundError. Tell the caller to use csv
        # instead. (Prevents ModuleNotFoundError: No module named 'openpyxl'.)
        try:
            import openpyxl
        except ModuleNotFoundError:
            # JsonResponse (not DRF Response) so the csv/xlsx passthrough renderer
            # cannot mangle this error body. (Prevents ModuleNotFoundError 500.)
            return JsonResponse(
                {"message": "xlsx export is unavailable on this server. Please use format=csv."},
                status=503,
            )
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Participants"
        if headers:
            ws.append(headers)
            for row in rows:
                ws.append([row[h] for h in headers])
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        response = HttpResponse(
            output.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{safe_name}_participants.xlsx"'
        return response
    else:
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{safe_name}_participants.csv"'
        if headers:
            writer = csv.DictWriter(response, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        return response


# ── verify an event for rankings integrity ──
# Toggles Event.rankings_verified. This is the AFC oversight gate that decides whether an
# (organizer-run) event counts toward afc_rankings, so it is restricted to PLATFORM org
# admins only (head_admin / organizer_admin) via is_platform_org_admin — not the broader
# _is_event_admin set. AFC keeps the final say on what feeds the ranking system.
@api_view(["POST"])
def verify_event(request):
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    user = validate_token(session_token.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # rankings-integrity decision → platform org admins only.
    if not is_platform_org_admin(user):
        return Response({"message": "You do not have permission to verify events."}, status=403)

    event = Event.objects.filter(event_id=request.data.get("event_id")).first()
    if not event:
        return Response({"message": "Event not found."}, status=404)

    # default to verifying when the flag is omitted; accept an explicit false to un-verify.
    event.rankings_verified = bool(request.data.get("verified", True))
    event.save(update_fields=["rankings_verified"])

    return Response({
        "message": "Event verification updated.",
        "event_id": event.event_id,
        "rankings_verified": event.rankings_verified,
    }, status=200)

    return Response({"message": "Image deleted successfully."})

# ??????????????????????????????????????????????????????????????????????????????
#  ESPORT MEDIA EXPORT (owner 2026-06-12)
#  Admins AND organizers can download team logos + player esport images as a ZIP:
#  either an arbitrary SET of teams/players, or everything REGISTERED for an event.
# ??????????????????????????????????????????????????????????????????????????????
@api_view(["POST"])
def download_esport_media(request):
    """
    POST events/download-esport-media/  - bundle team logos + player esport images into a ZIP.

    PURPOSE
        Owner 2026-06-12: "a way for admins to download the logos or esport images of any team &
        player or set of teams & players", plus the same scoped to everything registered for one
        event - for BOTH admins and organizers (who use the esport images in event graphics).

    AUTH
        Bearer SessionToken. Allowed: AFC event admins (_is_event_admin) OR any user holding the
        platform `organizer` role (org members - the role every active OrganizationMember gets).

    REQUEST (JSON; at least one selector required)
        { "team_ids": [int, ...],      # logos of these teams
          "player_ids": [int, ...],    # esport images of these users
          "event_id": int }            # OR: every registered team's logo + every rostered/solo
                                       #     player's esport image for this event
    RESPONSE
        200 application/zip attachment:
            team_logos/<team_name>.<ext>, esport_images/<username>.<ext>, manifest.txt
            (the manifest lists what was included and who had NO uploaded asset - missing assets
            are skipped, never an error, so one logo-less team cannot block the export)
        400 no selector / unknown event; 401/403 auth.

    HOW IT CONNECTS
        - Reads Team.team_logo (afc_team) + UserProfile.esports_pic (afc_auth, uploaded via
          /auth/upload-esport-image/, replace-only).
        - Event scope walks RegisteredCompetitors (teams + solo users) and TournamentTeamMember
          (the event rosters) - the same tables registration writes.
        - Consumed by the admin Teams & Players page + the admin/organizer event pages
          ("Download media" buttons).
    """
    import io
    import os
    import re as _re
    import zipfile

    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=401)

    # Admins always pass; otherwise the platform organizer role (held by org members).
    is_organizer = UserRoles.objects.filter(user=user, role__role_name="organizer").exists()
    if not _is_event_admin(user) and not is_organizer:
        return Response({"message": "Only admins and organizers can download esport media."}, status=403)

    team_ids = request.data.get("team_ids") or []
    player_ids = request.data.get("player_ids") or []
    event_id = request.data.get("event_id")

    if not team_ids and not player_ids and not event_id:
        return Response({"message": "Provide team_ids, player_ids, or event_id."}, status=400)

    zip_label = "esport-media"
    if event_id:
        event = Event.objects.filter(event_id=event_id).first()
        if not event:
            return Response({"message": "Event not found."}, status=400)
        zip_label = event.event_name or f"event-{event_id}"
        regs = RegisteredCompetitors.objects.filter(event=event).select_related("team", "user")
        team_ids = list({r.team_id for r in regs if r.team_id})
        # Players = solo registrants + every rostered member of the event's tournament teams.
        solo_ids = [r.user_id for r in regs if r.user_id]
        roster_ids = list(
            TournamentTeamMember.objects.filter(tournament_team__event=event)
            .values_list("user_id", flat=True)
        )
        player_ids = list({*solo_ids, *roster_ids})

    def _safe(name, fallback):
        cleaned = _re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("_")
        return cleaned or str(fallback)

    included, missing = [], []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for team in Team.objects.filter(team_id__in=team_ids):
            if team.team_logo:
                try:
                    ext = os.path.splitext(team.team_logo.name)[1] or ".png"
                    zf.writestr(f"team_logos/{_safe(team.team_name, team.team_id)}{ext}", team.team_logo.read())
                    included.append(f"team logo: {team.team_name}")
                    continue
                except Exception:
                    pass  # unreadable file on disk -> treat as missing, never block the export
            missing.append(f"team logo MISSING: {team.team_name}")

        from afc_auth.models import UserProfile
        profiles = {
            p.user_id: p
            for p in UserProfile.objects.filter(user_id__in=player_ids).select_related("user")
        }
        for u in User.objects.filter(user_id__in=player_ids):
            profile = profiles.get(u.user_id)
            if profile and profile.esports_pic:
                try:
                    ext = os.path.splitext(profile.esports_pic.name)[1] or ".png"
                    # Filename = IGN_UID (owner 2026-06-12): the in-game name plus the Free Fire UID, so
                    # graphics teams can match files to game accounts without opening the platform.
                    zf.writestr(
                        f"esport_images/{_safe(u.username, u.user_id)}_{_safe(u.uid, u.user_id)}{ext}",
                        profile.esports_pic.read(),
                    )
                    included.append(f"esport image: {u.username}")
                    continue
                except Exception:
                    pass
            missing.append(f"esport image MISSING: {u.username}")

        manifest = ["AFC esport media export", f"included: {len(included)}", ""]
        manifest += included + [""] + (["Not uploaded yet:"] + missing if missing else [])
        zf.writestr("manifest.txt", "\n".join(manifest))

    from django.http import HttpResponse
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    safe_label = _re.sub(r"[^A-Za-z0-9._-]+", "-", zip_label).strip("-") or "esport-media"
    response["Content-Disposition"] = f'attachment; filename="{safe_label}-media.zip"'
    return response
