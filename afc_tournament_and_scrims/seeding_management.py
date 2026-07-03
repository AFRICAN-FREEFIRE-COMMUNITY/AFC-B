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
    Event, Match, Stages, StageGroups, StageCompetitor, StageGroupCompetitor,
    RegisteredCompetitors, TournamentTeam,
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


# ── AUTO-SEED added/registered competitors into the bracket (owner 2026-06-21) ────────────────────
# WHY: registration (public register_for_event, admin add_teams_to_event, qualifier promotion) only
# creates EVENT-level rows (RegisteredCompetitors / TournamentTeam). It does NOT place a competitor
# into a stage's groups (StageCompetitor -> StageGroupCompetitor). So added teams never showed up in
# the groups and could not have stats entered until an admin MANUALLY ran "Seed to groups". The owner
# wants that to happen automatically: instantly when teams are added, and as a safety-net when the
# Stats/leaderboard page opens. autoseed_stage() is the engine; it does EXACTLY what the manual seed
# does (StageCompetitor pool + the round-robin _distribute_into_groups above), just automatically and
# idempotently. Callers: views.add_teams_to_event/add_teams_to_stage (on add) + sync_entry_stage_seeding
# (the Stats-page safety-net) below.

# Only these formats place competitors into StageGroups via the round-robin algorithm. Round-robin
# stages use RoundRobinGroup base groups (assigned in the RoundRobinPanel, a different subsystem) and
# CS knockout/double-elim/league use fixed head-to-head brackets, so auto-distributing registrations
# into them would be wrong. We restrict auto-seed to the group-standings formats and leave the rest to
# their format-specific tools. (The MANUAL seed endpoints stay unrestricted, as before.)
GROUP_DISTRIBUTABLE_FORMATS = {"br - normal", "cs - normal"}


def entry_stage(event):
    """The event's ENTRY stage = the first by display order (stage_order, then start_date, then id) —
    the stage that takes REGISTRATION-based seeding. Later stages fill from results/qualifiers, never
    from raw registrations, so registration auto-seed only ever targets this one stage (owner choice
    2026-06-21: "entry stage only"). Returns a Stages or None when the event has no stages yet."""
    return (
        Stages.objects.filter(event=event)
        .order_by("stage_order", "start_date", "stage_id")
        .first()
    )


def _missing_stage_competitor_entries(stage):
    """The StageCompetitor rows that SHOULD exist for `stage` but don't yet, built from the event's
    CONFIRMED registrations (mirrors views.seed_event_competitors_to_stage's competitor selection):
      - solo events -> every RegisteredCompetitors(status="registered")
      - team events -> every TournamentTeam(status="active") that is not waitlisted
    Returns a list of UNSAVED StageCompetitor (only the missing ones, so it is idempotent)."""
    event = stage.event
    if event.participant_type == "solo":
        regs = RegisteredCompetitors.objects.filter(event=event, status="registered")
        existing = set(
            StageCompetitor.objects.filter(stage=stage).values_list("player_id", flat=True)
        )
        return [StageCompetitor(stage=stage, player=r) for r in regs if r.id not in existing]
    teams = (
        TournamentTeam.objects.filter(event=event, status="active")
        .exclude(is_waitlisted=True)  # waitlisted teams are not confirmed participants yet
    )
    existing = set(
        StageCompetitor.objects.filter(stage=stage).values_list("tournament_team_id", flat=True)
    )
    return [
        StageCompetitor(stage=stage, tournament_team=t)
        for t in teams if t.tournament_team_id not in existing
    ]


def autoseed_stage(stage, *, shuffle=False):
    """Idempotently fold ALL confirmed registrations of stage.event into `stage`'s bracket:
      1. create any missing StageCompetitor rows (the stage pool), then
      2. distribute the still-UNGROUPED competitors into the stage's groups (StageGroupCompetitor),
         using the same round-robin algorithm as the manual "Seed to groups" action.
    Safe to call repeatedly: missing-only creation + only_ungrouped distribution means existing
    placements are never moved or duplicated (admins can still manually reseed/move afterwards).

    Restricted to GROUP_DISTRIBUTABLE_FORMATS (br/cs normal); other formats (round-robin, CS brackets)
    are left to their own tools and return a no-op with a `skipped` note. BEST-EFFORT: any failure is
    swallowed + logged so it never breaks the operation that triggered it (Discord side-effect pattern).
    Returns a dict of counts.
    """
    result = {
        "stage_id": getattr(stage, "stage_id", None),
        "stage_competitors_added": 0,
        "group_seeds_added": 0,
    }
    if not stage:
        result["skipped"] = "no_stage"
        return result
    if stage.stage_format not in GROUP_DISTRIBUTABLE_FORMATS:
        # RR base groups / CS brackets are placed by their own subsystems, not registration auto-seed.
        result["skipped"] = f"format:{stage.stage_format}"
        return result
    try:
        with transaction.atomic():
            new_sc = _missing_stage_competitor_entries(stage)
            if new_sc:
                StageCompetitor.objects.bulk_create(new_sc, ignore_conflicts=True)
                result["stage_competitors_added"] = len(new_sc)
            # Distribute the still-ungrouped pool into the stage's groups (no-op if there are no groups).
            result["group_seeds_added"] = _distribute_into_groups(
                stage, shuffle=shuffle, only_ungrouped=True,
            )
        # Best-effort Discord STAGE-role reconcile for the newly pooled competitors (group roles are
        # reconciled inside _distribute_into_groups). Mirrors views.seed_event_competitors_to_stage.
        if result["stage_competitors_added"]:
            try:
                from .views import reconcile_stage_roles  # lazy: avoid load-time circular import
                reconcile_stage_roles(stage.stage_id)
            except Exception:
                pass  # non-critical
    except Exception as exc:  # never break the triggering add/stat operation
        import logging
        logging.getLogger(__name__).warning(
            "autoseed_stage failed for stage %s: %s", result["stage_id"], exc,
        )
    return result


def autoseed_entry_stage(event, *, shuffle=False):
    """autoseed_stage() on the event's ENTRY stage (see entry_stage). No-op when the event has no
    stages yet. This is what the on-add hooks + the Stats-page safety-net call for an event."""
    return autoseed_stage(entry_stage(event), shuffle=shuffle)


@api_view(["POST"])
def sync_entry_stage_seeding(request):
    """POST events/seeding/sync-entry-stage/ {event_id} — the Stats-page SAFETY-NET for auto-seeding.

    Idempotently seeds every confirmed registration of the event into its ENTRY stage's groups (see
    autoseed_entry_stage). Fired by the admin + organizer Stats/leaderboard pages when they open
    (owner 2026-06-21: "automatically seed those added teams ALSO when they click on Stat"), so teams
    added by ANY path — admin add_teams_to_event, public/organizer register_for_event, or qualifier
    promotion — appear in the groups and can have stats entered WITHOUT a manual seed step.

    AUTH: gated exactly like the other seeding endpoints (_seeding_gate = AFC event admin OR organizer
    with can_manage_registrations on the owning org), so it covers admins AND organizers. Underneath,
    autoseed_* is best-effort, so this returns 200 with counts even when there is nothing to do.
    Consumed by: frontend lib/seedingManagement.ts -> the admin + organizer leaderboard pages."""
    user, err = _auth_user(request)
    if err:
        return err
    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id required."}, status=400)
    event = get_object_or_404(Event, event_id=event_id)
    if not _seeding_gate(user, event):
        return Response(
            {"message": "You do not have permission to manage seeding for this event."},
            status=403,
        )
    result = autoseed_entry_stage(event)
    return Response({"message": "Entry-stage seeding synced.", **result}, status=200)


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


@api_view(["POST"])
def move_team_between_groups(request):
    """MOVE one team (or solo player) from one group to another WITHIN a stage (F2, owner 2026-06-19).

    Backs the drag-and-drop "move a team card from Group A into Group B" in the Stages & Groups
    editor. Standard stages only: delete the team's StageGroupCompetitor row in the source group and
    create one in the target group (the unique_together(stage_group, tournament_team, player) keeps a
    team in exactly one group per stage). Match rows are immutable, so the team's PAST results stay
    linked to the OLD group's matches — if it already has entered results there, we require `force`
    (warn-and-allow): those old-group stats remain in the old group's standings after the move.

    Round-robin stages store teams on RoundRobinGroup.teams (M2M) + rebuild lobbies, NOT on
    StageGroupCompetitor, so a DnD move there needs a different path — we fail SAFE with a clear
    message rather than corrupt the lobby structure (use Reseed for RR for now).

    Request:  {from_group_id, to_group_id, tournament_team_id? | player_id?, force?}
    Response: 200 {message, from_group_id, to_group_id}; 409 {requires_force} when results exist;
              400/403/404 on validation / permission / not-found.
    """
    user, err = _auth_user(request)
    if err:
        return err

    _from_id = request.data.get("from_group_id")
    _to_id = request.data.get("to_group_id")

    # Round-robin fail-safe #1 — reject RR group ids BEFORE the StageGroups lookup. RoundRobinGroup
    # and StageGroups both use independent AutoField PKs whose integer ranges OVERLAP, so an RR
    # group_id sent by a stale/buggy client would otherwise `get_object_or_404(StageGroups, ...)` into
    # an UNRELATED StageGroups row (possibly in a different, non-RR stage) and the substring guard
    # below would inspect the wrong stage and be bypassed — silently mutating the wrong stage's groups.
    # Detecting the id against RoundRobinGroup first closes that collision. (Adversarial-review fix,
    # owner 2026-06-19.) RR teams live on RoundRobinGroup.teams (M2M) + drive generated lobbies, so a
    # DnD move there is not supported — fail SAFE (use Reseed).
    from .models import RoundRobinGroup, StageCompetitor

    # ── ROUND-ROBIN branch (owner 2026-07-03: RR stages now appear in the move panel) ──────────
    # RR teams live on RoundRobinGroup.teams (M2M); game-day lobbies DERIVE from base groups, so a
    # base-group move automatically reshapes future lobbies. Supported moves:
    #   • base group -> base group        (both ids are RoundRobinGroup pks)
    #   • "rr-unassigned-<stage_id>" -> base group   (assign from the stage's seeded pool)
    #   • base group -> "rr-unassigned-<stage_id>"   (unassign back to the pool)
    # Guard: when any lobby fed by the touched base group(s) already has entered results, we warn +
    # require force (mirrors the standard-stage results guard; past lobby results stay untouched).
    def _rr_ref(gid):
        """Resolve a mover group id to ('pool', stage_id) | ('group', RoundRobinGroup) | None."""
        if isinstance(gid, str) and gid.startswith("rr-unassigned-"):
            try:
                return ("pool", int(gid.rsplit("-", 1)[1]))
            except ValueError:
                return None
        try:
            rr = RoundRobinGroup.objects.select_related("stage__event").get(group_id=gid)
            return ("group", rr)
        except (RoundRobinGroup.DoesNotExist, ValueError, TypeError):
            return None

    _from_rr = _rr_ref(_from_id)
    _to_rr = _rr_ref(_to_id)
    if _from_rr or _to_rr:
        # Mixed RR/standard ids are invalid; a pool-to-pool move is a no-op.
        if not (_from_rr and _to_rr):
            return Response({"message": "Round-robin groups can only be moved within their own stage."}, status=400)
        rr_stage = (_from_rr[1].stage if _from_rr[0] == "group" else None) or (_to_rr[1].stage if _to_rr[0] == "group" else None)
        if rr_stage is None:
            return Response({"message": "Source and target groups are the same."}, status=400)
        for ref in (_from_rr, _to_rr):
            sid = ref[1] if ref[0] == "pool" else ref[1].stage_id
            if sid != rr_stage.stage_id:
                return Response({"message": "Both groups must be in the same stage."}, status=400)
        event = rr_stage.event
        if not _seeding_gate(user, event):
            return Response({"message": "You do not have permission to manage seeding for this event."}, status=403)
        tt_id = request.data.get("tournament_team_id")
        if not tt_id:
            return Response({"message": "tournament_team_id is required."}, status=400)
        force = str(request.data.get("force", "false")).lower() in ("1", "true", "yes")
        # The team must be a competitor of this stage (the pool + base groups both draw from it).
        if not StageCompetitor.objects.filter(stage=rr_stage, tournament_team_id=tt_id).exists():
            return Response({"message": "That team is not a competitor of this stage."}, status=404)
        # Played-lobby guard: any result-entered lobby sourced from a touched base group -> force.
        touched = [r[1] for r in (_from_rr, _to_rr) if r[0] == "group"]
        if not force:
            for rr in touched:
                for lobby in rr_stage.groups.filter(source_groups=rr):
                    if lobby.matches.filter(result_inputted=True).exists():
                        return Response(
                            {"message": f"Lobbies fed by base group {rr.label} already have entered "
                                        "results. Past results stay with those lobbies; future "
                                        "lobbies will use the new grouping. Move anyway?",
                             "requires_force": True},
                            status=409)
        # Apply: remove from the source base group (pool = nothing to remove), add to the target.
        if _from_rr[0] == "group":
            if not _from_rr[1].teams.filter(tournament_team_id=tt_id).exists():
                return Response({"message": "That competitor is not in the source group."}, status=404)
            _from_rr[1].teams.remove(tt_id)
        else:
            # Pool source: the team must not already sit in a base group of this stage.
            existing = RoundRobinGroup.objects.filter(stage=rr_stage, teams__tournament_team_id=tt_id).first()
            if existing is not None:
                return Response({"message": f"That team is already in base group {existing.label}."}, status=400)
        if _to_rr[0] == "group":
            if _to_rr[1].teams.filter(tournament_team_id=tt_id).exists():
                return Response({"message": "That competitor is already in the target group."}, status=400)
            _to_rr[1].teams.add(tt_id)
        return Response({"message": "Team moved.", "from_group_id": _from_id, "to_group_id": _to_id})

    _rr_msg = ("This is a round-robin stage; teams there are managed on the base groups. "
               "Use Reseed to reorganise round-robin teams.")

    from_group = get_object_or_404(StageGroups, group_id=_from_id)
    to_group = get_object_or_404(StageGroups, group_id=_to_id)
    if from_group.stage_id != to_group.stage_id:
        return Response({"message": "Both groups must be in the same stage."}, status=400)
    if from_group.group_id == to_group.group_id:
        return Response({"message": "Source and target groups are the same."}, status=400)

    stage = from_group.stage
    event = stage.event
    if not _seeding_gate(user, event):
        return Response({"message": "You do not have permission to manage seeding for this event."}, status=403)

    # Round-robin fail-safe #2 — STRUCTURAL guard on the RESOLVED stage (authoritative). The old check
    # was `"round" in stage_format` which both over-matches (legacy 'br - roundrobin' Knockout) and
    # under-matches ('cs - league'); the presence of RoundRobinGroup rows is the real signal that a
    # stage's teams live on the RR M2M (same source of truth get_event_group_rosters uses).
    if stage.round_robin_groups.exists():
        return Response({"message": _rr_msg}, status=400)

    tt_id = request.data.get("tournament_team_id")
    player_id = request.data.get("player_id")
    if not tt_id and not player_id:
        return Response({"message": "tournament_team_id or player_id is required."}, status=400)
    force = str(request.data.get("force", "false")).lower() in ("1", "true", "yes")

    # Locate the competitor's row in the SOURCE group.
    src_filter = {"stage_group": from_group}
    tgt_filter = {"stage_group": to_group}
    if tt_id:
        src_filter["tournament_team_id"] = tt_id
        tgt_filter["tournament_team_id"] = tt_id
    else:
        src_filter["player_id"] = player_id
        tgt_filter["player_id"] = player_id

    row = StageGroupCompetitor.objects.filter(**src_filter).first()
    if not row:
        return Response({"message": "That competitor is not in the source group."}, status=404)
    if StageGroupCompetitor.objects.filter(**tgt_filter).exists():
        return Response({"message": "That competitor is already in the target group."}, status=400)

    # Results guard (team only — solo stats are out of scope for the guard): if the team already has
    # entered results in the source group, warn + require force (old-group stats stay behind).
    if tt_id:
        from .models import TournamentTeamMatchStats
        has_results = TournamentTeamMatchStats.objects.filter(
            match__group=from_group, tournament_team_id=tt_id,
        ).exists()
        if has_results and not force:
            return Response({
                "message": "This team already has results entered in its current group. Moving it "
                           "leaves those results in the old group's standings. Move anyway?",
                "requires_force": True,
            }, status=409)

    with transaction.atomic():
        row.delete()
        if tt_id:
            StageGroupCompetitor.objects.create(stage_group=to_group, tournament_team_id=tt_id)
        else:
            StageGroupCompetitor.objects.create(stage_group=to_group, player_id=player_id)

    _reconcile_group_roles(stage)
    return Response({
        "message": "Moved to the new group.",
        "from_group_id": from_group.group_id,
        "to_group_id": to_group.group_id,
    }, status=200)
