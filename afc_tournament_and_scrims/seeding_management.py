"""
afc_tournament_and_scrims.seeding_management — SEEDING UNDO/REDO + DELETE-AND-RESEED.

PURPOSE (owner feature 2026-06-15, spec: WEBSITE/tasks/seeding-undo-redo-delete-reseed-plan.md)
    Let admins AND organizers reorganise competitor placement AFTER the initial seed:
      - UNDO group seeding for a stage (back to stage-seeded-but-ungrouped) and REDO it
        (re-distribute, optionally with a fresh shuffle).
      - DELETE a group, choosing what happens to its competitors:
          auto      = redistribute them into the stage's remaining groups,
          manual    = leave them stage-seeded but ungrouped (admin places them later),
          delete_all = purge them from the stage entirely.
      - DELETE a stage with the same disposition choice; auto/manual MOVE the stage's
        competitors into a chosen target stage (then auto also distributes into its groups).

WHY A SEPARATE MODULE
    The existing seed endpoints in views.py are admin-only and inline. Rather than refactor
    that 19k-line file (regression risk), this module is self-contained — same isolation
    pattern as event_links.py / event_payments.py. It re-implements only the small, proven
    seed-into-groups algorithm (shuffle + round-robin modulo) and reuses the live data model.

HOW IT CONNECTS
    - Models: StageCompetitor (stage-level seed) + StageGroupCompetitor (group-level seed) +
      Stages / StageGroups / Match (played-results guard). Deleting a StageGroups CASCADE-deletes
      its StageGroupCompetitor + Match + Leaderboard, but NOT the stage-level StageCompetitor, so
      a deleted group's competitors stay stage-seeded and a reseed pass picks them up.
    - Standings are computed ON READ (views.get_all_leaderboard_details_for_event); matches are
      generated once at event-create and never regenerated, so reseeding needs NO match work.
    - Permissions: AFC event admins always pass; organizers pass with can_manage_registrations on
      the owning org (org_can_event). Native (org=None) AFC events stay admin-only.
    - Discord: best-effort reuse of views.remove_group_role_task / reconcile_group_roles_for_stage
      (imported lazily, wrapped in try/except — Discord is non-critical and reconcile rebuilds
      from DB state on the next seed).
    - Consumed by: frontend lib/seedingManagement.ts -> the admin event-edit ActionsTab
      "Seeding management" section and the organizer event groups page.

ENDPOINTS (mounted under events/ via afc_tournament_and_scrims/urls.py)
    POST events/seeding/undo/          undo_seeding         {stage_id, force}
    POST events/seeding/reseed/        reseed_into_groups   {stage_id, shuffle, clear_existing}
    POST events/seeding/delete-group/  delete_group_managed {group_id, mode, force}
    POST events/seeding/delete-stage/  delete_stage_managed {stage_id, mode, target_stage_id, force}
"""
import random

from django.db import transaction
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from .models import (
    Match, Stages, StageGroups, StageCompetitor, StageGroupCompetitor,
)


# ── auth ───────────────────────────────────────────────────────────────────────────────────────
def _auth_user(request):
    """Resolve the Bearer-token user, or return (None, error Response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _is_event_admin(user):
    """AFC event admin (base role admin, or head_admin/super_admin/event_admin granular). Local copy
    of the views.py helper to avoid importing the 19k-line module at load time."""
    if user.role in ("admin", "moderator", "support"):
        return True
    return user.userroles.filter(
        role__role_name__in=("head_admin", "super_admin", "event_admin"),
    ).exists()


def _seeding_gate(user, event):
    """Who may reorganise seeding for this event: AFC event admins always; organizers with
    can_manage_registrations on the owning org. Native (org=None) events are admin-only (handled
    inside org_can_event, which returns the platform-admin check when event.organization is None)."""
    return _is_event_admin(user) or org_can_event(user, "can_manage_registrations", event)


# ── Discord (best-effort; never raises into the request path) ────────────────────────────────────
def _queue_group_role_removal(group):
    """Queue Discord group-role removal for every competitor in a group (team OR solo). Best-effort:
    Discord/celery problems must not break a seeding operation, so everything is wrapped."""
    try:
        from .views import remove_group_role_task  # lazy: avoid load-time circular import
        role_id = group.group_discord_role_id
        if not role_id:
            return
        for comp in StageGroupCompetitor.objects.filter(stage_group=group).select_related(
            "player__user", "tournament_team",
        ):
            users = []
            if comp.player_id and comp.player and comp.player.user:
                users = [comp.player.user]
            elif comp.tournament_team_id and comp.tournament_team:
                users = [m.user for m in comp.tournament_team.members.select_related("user").all()]
            for user in users:
                if user and getattr(user, "discord_id", None):
                    remove_group_role_task.delay(user.discord_id, role_id)
    except Exception:
        pass  # non-critical


def _reconcile_group_roles(stage):
    """Best-effort rebuild of Discord group-role assignments after a (re)seed."""
    try:
        from .views import reconcile_group_roles_for_stage  # lazy
        reconcile_group_roles_for_stage(stage)
    except Exception:
        pass  # non-critical


# ── played-results guard ─────────────────────────────────────────────────────────────────────────
def _played_maps_in_stage(stage):
    """Count of maps (Match rows) in a stage that already have entered results."""
    return Match.objects.filter(group__stage=stage, result_inputted=True).count()


def _played_maps_in_group(group):
    """Count of maps in a single group that already have entered results."""
    return Match.objects.filter(group=group, result_inputted=True).count()


# ── core seeding helpers ─────────────────────────────────────────────────────────────────────────
def _clear_group_seeding(stage):
    """Undo group seeding for a stage: queue Discord removal then delete every StageGroupCompetitor
    in the stage. Leaves StageCompetitor (stage-level seed) intact so a redo can re-distribute.
    Returns the number of group-seed rows removed."""
    for group in StageGroups.objects.filter(stage=stage):
        _queue_group_role_removal(group)
    deleted, _ = StageGroupCompetitor.objects.filter(stage_group__stage=stage).delete()
    return deleted


def _distribute_into_groups(stage, shuffle=True, only_ungrouped=False):
    """Distribute the stage's active competitors into its groups (shuffle + round-robin modulo —
    the same algorithm as views.seed_stage_competitors_to_groups_team). Handles team and solo
    transparently (StageCompetitor carries tournament_team OR player).

    only_ungrouped=True seeds ONLY competitors that currently have no StageGroupCompetitor in the
    stage (used after a group delete, so already-placed competitors are not duplicated/moved).

    Returns the number of group-seed rows created. Reconciles Discord roles best-effort.
    """
    groups = list(StageGroups.objects.filter(stage=stage).order_by("group_id"))
    if not groups:
        return 0

    competitors = list(StageCompetitor.objects.filter(stage=stage, status="active"))
    if not competitors:
        return 0

    if only_ungrouped:
        grouped_team_ids = set(
            StageGroupCompetitor.objects.filter(
                stage_group__stage=stage, tournament_team__isnull=False,
            ).values_list("tournament_team_id", flat=True)
        )
        grouped_player_ids = set(
            StageGroupCompetitor.objects.filter(
                stage_group__stage=stage, player__isnull=False,
            ).values_list("player_id", flat=True)
        )
        competitors = [
            c for c in competitors
            if (c.tournament_team_id not in grouped_team_ids)
            and (c.player_id not in grouped_player_ids)
        ]
        if not competitors:
            return 0

    if shuffle:
        random.shuffle(competitors)

    entries = []
    for index, competitor in enumerate(competitors):
        group = groups[index % len(groups)]
        if competitor.tournament_team_id:
            entries.append(StageGroupCompetitor(stage_group=group, tournament_team=competitor.tournament_team))
        else:
            entries.append(StageGroupCompetitor(stage_group=group, player=competitor.player))

    StageGroupCompetitor.objects.bulk_create(entries, ignore_conflicts=True)
    _reconcile_group_roles(stage)
    return len(entries)


def _group_competitor_keys(group):
    """The (team_ids, player_ids) currently seeded into a group — captured BEFORE deletion so the
    caller can act on those specific competitors (auto redistribute / delete_all purge)."""
    rows = StageGroupCompetitor.objects.filter(stage_group=group)
    team_ids = set(r.tournament_team_id for r in rows if r.tournament_team_id)
    player_ids = set(r.player_id for r in rows if r.player_id)
    return team_ids, player_ids


# ── endpoints ────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def undo_seeding(request):
    """UNDO group seeding for a stage (all groups). Competitors fall back to stage-seeded-but-
    ungrouped; results are NOT touched unless the caller forces past the played-results guard.

    Request:  {stage_id, force?}
    Response: 200 {cleared, stage_id}; 400 {played_maps, requires_force} if results exist and not force.
    """
    user, err = _auth_user(request)
    if err:
        return err

    stage = get_object_or_404(Stages, stage_id=request.data.get("stage_id"))
    event = stage.event
    if not _seeding_gate(user, event):
        return Response({"message": "You do not have permission to manage seeding for this event."}, status=403)

    force = str(request.data.get("force", "false")).lower() in ("1", "true", "yes")
    played = _played_maps_in_stage(stage)
    if played and not force:
        return Response({
            "message": f"This stage has {played} map(s) with entered results. Undoing seeding will "
                       f"keep the results rows but unassign all competitors. Confirm to proceed.",
            "played_maps": played,
            "requires_force": True,
        }, status=400)

    with transaction.atomic():
        cleared = _clear_group_seeding(stage)

    return Response({"message": "Group seeding undone. Competitors are back to stage-seeded (ungrouped).",
                     "cleared": cleared, "stage_id": stage.stage_id}, status=200)


@api_view(["POST"])
def reseed_into_groups(request):
    """REDO / (re)seed the stage's competitors into its groups.

    Request:  {stage_id, shuffle? (default true), clear_existing? (default true)}
    clear_existing=true  -> fresh reshuffle (clear all group seeding, then distribute everyone).
    clear_existing=false -> only fill competitors that are currently ungrouped (append).
    Response: 200 {seeded, stage_id, groups}; 400 {played_maps, requires_force} when clear_existing
              would wipe entered results and force is not set.
    """
    user, err = _auth_user(request)
    if err:
        return err

    stage = get_object_or_404(Stages, stage_id=request.data.get("stage_id"))
    event = stage.event
    if not _seeding_gate(user, event):
        return Response({"message": "You do not have permission to manage seeding for this event."}, status=403)

    shuffle = str(request.data.get("shuffle", "true")).lower() in ("1", "true", "yes")
    clear_existing = str(request.data.get("clear_existing", "true")).lower() in ("1", "true", "yes")
    force = str(request.data.get("force", "false")).lower() in ("1", "true", "yes")

    if clear_existing:
        played = _played_maps_in_stage(stage)
        if played and not force:
            return Response({
                "message": f"This stage has {played} map(s) with entered results. A fresh reseed will "
                           f"re-distribute all competitors (existing results become unreachable). Confirm to proceed.",
                "played_maps": played,
                "requires_force": True,
            }, status=400)

    with transaction.atomic():
        if clear_existing:
            _clear_group_seeding(stage)
        seeded = _distribute_into_groups(stage, shuffle=shuffle, only_ungrouped=not clear_existing)

    groups = StageGroups.objects.filter(stage=stage).count()
    return Response({"message": "Competitors seeded into groups.",
                     "seeded": seeded, "stage_id": stage.stage_id, "groups": groups}, status=200)


@api_view(["POST"])
def delete_group_managed(request):
    """DELETE a group with a disposition choice for its competitors.

    Request: {group_id, mode ∈ auto|manual|delete_all, force?}
      auto       -> delete the group, then redistribute its competitors into the remaining groups.
      manual     -> delete the group; competitors stay stage-seeded but ungrouped (place later).
      delete_all -> delete the group AND remove its competitors from the stage entirely.
    Response: 200 {mode, redistributed, remaining_groups}; 400 {played_maps, requires_force}.
    """
    user, err = _auth_user(request)
    if err:
        return err

    group = get_object_or_404(StageGroups, group_id=request.data.get("group_id"))
    mode = (request.data.get("mode") or "manual").strip().lower()
    if mode not in ("auto", "manual", "delete_all"):
        return Response({"message": "mode must be one of: auto, manual, delete_all."}, status=400)

    stage = group.stage
    event = stage.event
    if not _seeding_gate(user, event):
        return Response({"message": "You do not have permission to manage seeding for this event."}, status=403)

    force = str(request.data.get("force", "false")).lower() in ("1", "true", "yes")
    played = _played_maps_in_group(group)
    if played and not force:
        return Response({
            "message": f"This group has {played} map(s) with entered results. Deleting it permanently "
                       f"removes those results. Confirm to proceed.",
            "played_maps": played,
            "requires_force": True,
        }, status=400)

    with transaction.atomic():
        # Capture the group's competitors BEFORE the cascade delete (needed for delete_all purge).
        team_ids, player_ids = _group_competitor_keys(group)

        _queue_group_role_removal(group)
        group.delete()  # cascades StageGroupCompetitor + Match + Leaderboard for this group

        redistributed = 0
        if mode == "auto":
            # The just-freed competitors are now the stage's only ungrouped ones -> redistribute them.
            redistributed = _distribute_into_groups(stage, shuffle=False, only_ungrouped=True)
        elif mode == "delete_all":
            # Purge those competitors from the stage entirely, but only if they are not still placed
            # in another group (defensive — a competitor should only be in one group).
            still_grouped_teams = set(
                StageGroupCompetitor.objects.filter(
                    stage_group__stage=stage, tournament_team_id__in=team_ids,
                ).values_list("tournament_team_id", flat=True)
            )
            still_grouped_players = set(
                StageGroupCompetitor.objects.filter(
                    stage_group__stage=stage, player_id__in=player_ids,
                ).values_list("player_id", flat=True)
            )
            purge_teams = team_ids - still_grouped_teams
            purge_players = player_ids - still_grouped_players
            if purge_teams:
                StageCompetitor.objects.filter(stage=stage, tournament_team_id__in=purge_teams).delete()
            if purge_players:
                StageCompetitor.objects.filter(stage=stage, player_id__in=purge_players).delete()
        # mode == "manual": nothing more — competitors remain stage-seeded, ungrouped.

    remaining = StageGroups.objects.filter(stage=stage).count()
    return Response({"message": f"Group deleted ({mode}).", "mode": mode,
                     "redistributed": redistributed, "remaining_groups": remaining}, status=200)


@api_view(["POST"])
def delete_stage_managed(request):
    """DELETE a stage with a disposition choice for its competitors.

    Request: {stage_id, mode ∈ auto|manual|delete_all, target_stage_id?, force?}
      auto       -> move the stage's competitors into target_stage_id, delete this stage,
                    then distribute them into the target stage's groups.
      manual     -> move the stage's competitors into target_stage_id (ungrouped), delete this stage.
      delete_all -> delete the stage and everything in it (no move).
    target_stage_id is required for auto/manual and must be another stage in the same event.
    Response: 200 {mode, moved, redistributed}; 400 {played_maps, requires_force} or validation.
    """
    user, err = _auth_user(request)
    if err:
        return err

    stage = get_object_or_404(Stages, stage_id=request.data.get("stage_id"))
    mode = (request.data.get("mode") or "delete_all").strip().lower()
    if mode not in ("auto", "manual", "delete_all"):
        return Response({"message": "mode must be one of: auto, manual, delete_all."}, status=400)

    event = stage.event
    if not _seeding_gate(user, event):
        return Response({"message": "You do not have permission to manage seeding for this event."}, status=403)

    force = str(request.data.get("force", "false")).lower() in ("1", "true", "yes")
    played = _played_maps_in_stage(stage)
    if played and not force:
        return Response({
            "message": f"This stage has {played} map(s) with entered results. Deleting it permanently "
                       f"removes those results. Confirm to proceed.",
            "played_maps": played,
            "requires_force": True,
        }, status=400)

    target_stage = None
    if mode in ("auto", "manual"):
        target_stage_id = request.data.get("target_stage_id")
        if not target_stage_id:
            return Response({"message": "target_stage_id is required to move competitors."}, status=400)
        target_stage = get_object_or_404(Stages, stage_id=target_stage_id)
        if target_stage.stage_id == stage.stage_id:
            return Response({"message": "Target stage must be different from the stage being deleted."}, status=400)
        if target_stage.event_id != event.event_id:
            return Response({"message": "Target stage must belong to the same event."}, status=400)

    with transaction.atomic():
        moved = 0
        if target_stage is not None:
            # MOVE = create the source stage's competitors in the target stage (get_or_create dedupes).
            for comp in StageCompetitor.objects.filter(stage=stage):
                _, created = StageCompetitor.objects.get_or_create(
                    stage=target_stage,
                    tournament_team=comp.tournament_team,
                    player=comp.player,
                    defaults={"status": "active"},
                )
                if created:
                    moved += 1

        # Discord cleanup for the source stage's groups, then delete the stage (cascade).
        for group in StageGroups.objects.filter(stage=stage):
            _queue_group_role_removal(group)
        stage.delete()

        redistributed = 0
        if mode == "auto" and target_stage is not None:
            redistributed = _distribute_into_groups(target_stage, shuffle=False, only_ungrouped=True)

    return Response({
        "message": f"Stage deleted ({mode}).",
        "mode": mode,
        "moved": moved,
        "moved_to": (target_stage.stage_id if target_stage else None),
        "redistributed": redistributed,
    }, status=200)
