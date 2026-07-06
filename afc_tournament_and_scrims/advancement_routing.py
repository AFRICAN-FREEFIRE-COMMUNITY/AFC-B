"""
afc_tournament_and_scrims.advancement_routing — BRANCHING ADVANCEMENT ENGINE (feature #9).

PURPOSE (owner plan WEBSITE/tasks/advancement-routing-plan.md)
    Run a stage's StageAdvancementRule rows: rank each rule's scope (one group, or the whole
    stage), take positions [position_from..position_to], and seed those finishers into the rule's
    target stage. This is the BRANCHING alternative to the hardcoded-linear legacy advance
    (views.advance_group_competitors_to_next_stage / advance_round_robin), which can only push one
    group into the single next stage. A rule can split one stage's table into several later stages
    (top 1-8 -> Finals, 9-16 -> Play-In) and can skip ahead.

WHY A SEPARATE MODULE
    Same isolation rationale as seeding_management.py / event_links.py / head_to_head.py: keep the
    new logic out of the 21k-line views.py (regression risk + concurrent-edit safety). The only
    views.py touch-points are the tiny author-time pieces that MUST live next to create_event /
    edit_event (the _validate_advancement_rules guard, the 2nd-pass FK wiring, the
    get_event_details echo). The execution engine + its endpoint live here.

HOW IT CONNECTS (trace end-to-end)
    - Model: StageAdvancementRule (models.py) — source_stage / source_group(null=stage-wide) /
      target_stage / position_from / position_to / order. Presence of rules = "branching mode".
    - Standings: reuses the CANONICAL aggregators so a routed cut matches exactly what users see
      on the leaderboard / TournamentStructure. Teams -> round_robin._aggregate_team_standings
      (effective_total -> booyah -> kills -> name); solo -> _solo_standings below (Sum(total_points)
      -> Sum(kills), mirroring the legacy solo advance).
    - Seeding: StageCompetitor.get_or_create(stage=target, ...) + the SAME DiscordRoleAssignment
      queue + assign_stage_roles_from_db_task worker the legacy advance uses, so downstream
      (groups seeding, role assignment, standings) behaves identically. get_or_create => idempotent
      (re-running never double-seeds). Seeds ONLY the advanced rows — it does NOT autoseed the
      whole field (which would dump every team into the Finals).
    - Permissions: _advance_gate = AFC event admin OR organizer with can_manage_registrations on
      the owning org (same gate shape as seeding_management._seeding_gate), so admins AND organizers
      can fire it.
    - Consumed by: the shared Event Actions tab (ActionsTab.tsx) on the admin + organizer event-edit
      pages — a "Branching advancement" card that previews (dry_run) then advances a stage.

ENDPOINT (mounted under events/ via afc_tournament_and_scrims/urls.py)
    POST events/advance-stage-by-rules/   advance_stage_by_rules   {event_id, stage_id, dry_run?}
        dry_run=true  -> compute "who routes where" WITHOUT writing (the preview).
        dry_run=false -> seed the winners into their target stages (+ queue Discord roles).
    The LEGACY advance endpoints are untouched; this one only fires when the stage has rules.
"""
from django.db import transaction
from django.db.models import Case, F, IntegerField, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_auth.models import DiscordRoleAssignment, DiscordStageRoleAssignmentProgress
from afc_organizers.permissions import org_can_event

from . import round_robin
from .models import (
    Event, Stages, StageGroups, StageCompetitor, StageAdvancementRule,
    RegisteredCompetitors, TournamentTeam,
    TournamentTeamMatchStats, SoloPlayerMatchStats,
)


# ── auth / permission (mirrors seeding_management) ───────────────────────────────────────────────
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
    """AFC event admin (base role admin/moderator/support, or head_admin/super_admin/event_admin
    granular). Local copy of the views.py helper to avoid importing the 21k-line module at load
    time (same pattern as seeding_management._is_event_admin)."""
    if user.role in ("admin", "moderator", "support"):
        return True
    return user.userroles.filter(
        role__role_name__in=("head_admin", "super_admin", "event_admin"),
    ).exists()


def _advance_gate(user, event):
    """Who may run branching advancement for this event: AFC event admins always; organizers with
    can_manage_registrations on the owning org. Native (org=None) events are admin-only (handled
    inside org_can_event). Identical shape to seeding_management._seeding_gate so the two
    admin/organizer surfaces share one permission model."""
    return _is_event_admin(user) or org_can_event(user, "can_manage_registrations", event)


# ── standings per scope ──────────────────────────────────────────────────────────────────────────
def _solo_standings(qs):
    """Fold a SoloPlayerMatchStats queryset into a per-competitor table, ranked the SAME way the
    legacy solo advance (views.advance_group_competitors_to_next_stage) ranks: by summed
    total_points then summed kills. Returns plain dicts each carrying `competitor_id` (for seeding)
    + the display `username` + `placement` (1-based, filled by the caller). Mirrors
    round_robin._aggregate_team_standings, but for the solo stat table (which stores total_points
    directly, so we sum it rather than recomputing placement/kill/bonus/penalty)."""
    rows = (
        qs
        .values("competitor_id", username=F("competitor__user__username"))
        .annotate(
            total_points=Coalesce(Sum("total_points"), 0),
            total_kills=Coalesce(Sum("kills"), 0),
            total_booyah=Coalesce(Sum(
                Case(When(placement=1, then=Value(1)), default=Value(0),
                     output_field=IntegerField())
            ), 0),
        )
        .order_by("-total_points", "-total_kills", "username")
    )
    return list(rows)


def _ranking_for_rule(rule, event):
    """Build the ranked competitor list for ONE rule's scope.

    Returns (rows, kind) where kind is "team" or "solo" and each row is a dict carrying the seed id
    (`tournament_team_id` for team events, `competitor_id` for solo) plus a display name. Scope:
      • rule.source_group set  -> rank only that group's standings.
      • rule.source_group null -> rank the WHOLE source stage's standings (stage-wide). For a team
        event this is exactly round_robin.cumulative_standings(stage) (== filter on
        match__group__stage), which also covers the round-robin format correctly.
    Reuses the canonical aggregators so the routed cut equals what users see on the leaderboard.

    OWNER OVERRIDE (2026-06-29): Point-Rush carry-over counts toward QUALIFICATION, so when the
    source stage being ranked is itself a Point-Rush TARGET we fold each competitor's banked bonus
    into its ranking total before the caller slices position ranges. Reuses the SAME views helper the
    two legacy advance endpoints use (lazy-imported to keep this module free of the 21k-line views at
    load time, mirroring the assign_stage_roles_from_db_task import). No-op when the source stage has
    no Point-Rush source, so a normal branching rule routes byte-identically."""
    # Lazy import (avoids a load-time cycle: views imports this module). _fold_carry_over re-ranks the
    # rows in place when rule.source_stage is a Point-Rush target; otherwise returns them untouched.
    from .views import _fold_carry_over

    if event.participant_type == "solo":
        if rule.source_group_id:
            qs = SoloPlayerMatchStats.objects.filter(match__group=rule.source_group)
        else:
            qs = SoloPlayerMatchStats.objects.filter(match__group__stage=rule.source_stage)
        rows = _solo_standings(qs)
        rows = _fold_carry_over(
            rows, rule.source_stage, "solo",
            id_key="competitor_id", metric_key="total_points",
            sort_key=lambda r: (-int(r.get("total_points") or 0),
                                -int(r.get("total_kills") or 0),
                                r.get("username") or ""),
        )
        return rows, "solo"

    # team events
    if rule.source_group_id:
        rows = round_robin.group_standings(rule.source_group)
    else:
        rows = round_robin.cumulative_standings(rule.source_stage)
    rows = _fold_carry_over(
        rows, rule.source_stage, event.participant_type,
        id_key="tournament_team_id", metric_key="effective_total",
        sort_key=lambda r: (-int(r.get("effective_total") or 0),
                            -int(r.get("total_booyah") or 0),
                            -int(r.get("total_kills") or 0),
                            r.get("team_name") or ""),
    )
    return rows, "team"


def _seed_id(row, kind):
    """The competitor PK to seed from a standings row, per scope kind."""
    return row["tournament_team_id"] if kind == "team" else row["competitor_id"]


def _row_name(row, kind):
    """A human label for a standings row (team name / username), for the dry-run preview."""
    if kind == "team":
        return row.get("team_name") or f"Team {row.get('tournament_team_id')}"
    return row.get("username") or f"Player {row.get('competitor_id')}"


# ── Discord role queue (mirrors the legacy advance; best-effort) ─────────────────────────────────
def _queue_stage_roles(target_stage, *, team=None, comp=None):
    """Queue the target stage's Discord role for a seeded competitor (one team's members, or one
    solo player), exactly like advance_group_competitors_to_next_stage. Returns the number of
    DiscordRoleAssignment rows queued. No-op when the target stage has no role id. Wrapped by the
    caller; Discord is non-critical."""
    role_id = target_stage.stage_discord_role_id
    if not role_id:
        return 0
    queued = 0
    users = []
    if team is not None:
        users = [m.user for m in team.members.all()]
    elif comp is not None and comp.user_id:
        users = [comp.user]
    for u in users:
        if u and getattr(u, "discord_id", None):
            DiscordRoleAssignment.objects.get_or_create(
                user=u, discord_id=u.discord_id, role_id=role_id,
                stage=target_stage, group=None, defaults={"status": "pending"},
            )
            queued += 1
    return queued


def _kick_role_workers(queued_by_stage):
    """Start the role-assignment worker once per target stage that got queued rows (mirrors the
    legacy advance's single assign_stage_roles_from_db_task kick). Best-effort: celery/Discord
    must never break the advance, so it is wrapped. Returns {stage_id: progress_id}."""
    progress_ids = {}
    try:
        # The celery task lives in views.py (alongside the legacy advance that also kicks it).
        # Lazy import to avoid the load-time cost/circularity of pulling in the 21k-line module.
        from .views import assign_stage_roles_from_db_task
    except Exception:
        return progress_ids
    for stage, queued in queued_by_stage.items():
        if queued <= 0 or not stage.stage_discord_role_id:
            continue
        try:
            progress = DiscordStageRoleAssignmentProgress.objects.create(
                stage=stage, total=queued, status="running")
            assign_stage_roles_from_db_task.delay(str(progress.id), stage.stage_id)
            progress_ids[stage.stage_id] = str(progress.id)
        except Exception:
            pass  # non-critical
    return progress_ids


# ── the engine ───────────────────────────────────────────────────────────────────────────────────
def route_stage_advancement(stage, *, dry_run=False):
    """Run every StageAdvancementRule of `stage`: rank each rule's scope, slice
    [position_from-1 : position_to] (Python slicing auto-CLAMPS positions past the field), and seed
    the sliced finishers into the rule's target stage.

    Returns a dict:
      {
        "branching": bool,            # False => no rules; legacy endpoints serve this stage
        "dry_run": bool,
        "routed": [                   # one block per rule, in author order
          {"target_stage_id", "target_stage_name", "from", "to",
           "scope": "group"|"stage", "source_group_id", "source_group_name",
           "competitors": [{"id", "name", "placement", "type"}]}
        ],
        "newly_seeded": int,          # StageCompetitor rows actually created (0 on dry_run)
        "already_seeded": int,        # competitors already present in their target (idempotent)
        "discord_roles_queued": int,
        "progress_ids": {stage_id: progress_id},
      }

    Idempotent: get_or_create means a re-run never double-seeds. dry_run writes NOTHING (no
    StageCompetitor, no Discord) - it only reports who WOULD route where (the ActionsTab preview).
    Seeds ONLY the advanced rows; it never calls autoseed_stage, so a target stage receives exactly
    the routed competitors, not the whole field."""
    rules = list(
        StageAdvancementRule.objects
        .filter(source_stage=stage)
        .select_related("source_group", "target_stage")
        .order_by("order", "id")
    )
    result = {
        "branching": bool(rules),
        "dry_run": dry_run,
        "routed": [],
        "newly_seeded": 0,
        "already_seeded": 0,
        "removed_stale": 0,
        "discord_roles_queued": 0,
        "progress_ids": {},
    }
    if not rules:
        # No rules => legacy linear advance still owns this stage. Caller decides what to do.
        return result

    event = stage.event
    queued_by_stage = {}  # target Stages -> queued role count (for the worker kick)
    # #11 (owner 2026-07-06): the CURRENT qualifying set per target stage, unioned across all rules that
    # feed it, so a RE-RUN can remove competitors that dropped out of the cut (get_or_create only ADDS).
    from collections import defaultdict as _dd
    seeded_teams_by_stage = _dd(set)    # target Stage -> {tournament_team_id currently qualifying}
    seeded_players_by_stage = _dd(set)  # target Stage -> {RegisteredCompetitors.id currently qualifying}

    # All writes for a real run go in one transaction so a mid-apply failure rolls back cleanly.
    ctx = transaction.atomic() if not dry_run else _nullcontext()
    with ctx:
        for rule in rules:
            rows, kind = _ranking_for_rule(rule, event)
            # Ensure this target stage is reconciled even if its current cut is EMPTY (all qualifiers
            # dropped): touch the key so the removal pass below runs for it.
            (seeded_teams_by_stage if kind == "team" else seeded_players_by_stage)[rule.target_stage]
            # 1-based inclusive slice with auto-clamp. position_from-1 past the end => empty slice.
            lo = max(rule.position_from - 1, 0)
            hi = rule.position_to  # exclusive end; slicing clamps if hi > len(rows)
            sliced = rows[lo:hi]

            block = {
                "target_stage_id": rule.target_stage_id,
                "target_stage_name": rule.target_stage.stage_name,
                "from": rule.position_from,
                "to": rule.position_to,
                "scope": "group" if rule.source_group_id else "stage",
                "source_group_id": rule.source_group_id,
                "source_group_name": (rule.source_group.group_name
                                      if rule.source_group_id else None),
                "competitors": [],
            }

            for offset, row in enumerate(sliced):
                placement = lo + offset + 1  # 1-based finishing position in the scope
                seed_id = _seed_id(row, kind)
                block["competitors"].append({
                    "id": seed_id,
                    "name": _row_name(row, kind),
                    "placement": placement,
                    "type": kind,
                })
                if dry_run:
                    continue

                # ── real seed into the target stage (idempotent) ──
                if kind == "team":
                    tt = TournamentTeam.objects.prefetch_related("members__user").filter(
                        tournament_team_id=seed_id).first()
                    if not tt:
                        continue
                    seeded_teams_by_stage[rule.target_stage].add(tt.tournament_team_id)  # #11: in the cut
                    _, created = StageCompetitor.objects.get_or_create(
                        stage=rule.target_stage, tournament_team=tt, player=None,
                        defaults={"status": "active"})
                    if created:
                        result["newly_seeded"] += 1
                        q = _queue_stage_roles(rule.target_stage, team=tt)
                        queued_by_stage[rule.target_stage] = (
                            queued_by_stage.get(rule.target_stage, 0) + q)
                        result["discord_roles_queued"] += q
                    else:
                        result["already_seeded"] += 1
                else:
                    comp = RegisteredCompetitors.objects.select_related("user").filter(
                        id=seed_id).first()
                    if not comp:
                        continue
                    seeded_players_by_stage[rule.target_stage].add(comp.id)  # #11: in the cut
                    _, created = StageCompetitor.objects.get_or_create(
                        stage=rule.target_stage, player=comp, tournament_team=None,
                        defaults={"status": "active"})
                    if created:
                        result["newly_seeded"] += 1
                        q = _queue_stage_roles(rule.target_stage, comp=comp)
                        queued_by_stage[rule.target_stage] = (
                            queued_by_stage.get(rule.target_stage, 0) + q)
                        result["discord_roles_queued"] += q
                    else:
                        result["already_seeded"] += 1

            result["routed"].append(block)

        # ── #11 reconcile (owner 2026-07-06): remove competitors that DROPPED OUT of the cut on a
        # re-run. get_or_create above only ADDS current qualifiers, so after a standings correction the
        # previously-seeded (now-dropped) team was left in the target stage, over-filling it. Remove a
        # target-stage StageCompetitor only when it is (a) NOT in the current qualifying set for that
        # stage AND (b) has NOT played any match in that stage — a competitor that already competed is
        # never auto-removed (data-safe, mirrors the promotion-withdraw guard #16). Only stages this run
        # seeded are touched. Runs inside the same transaction so a failure rolls the removals back too.
        if not dry_run:
            from .models import TournamentTeamMatchStats, SoloPlayerMatchStats
            for tgt_stage, keep_team_ids in seeded_teams_by_stage.items():
                for sc in StageCompetitor.objects.filter(stage=tgt_stage, tournament_team__isnull=False):
                    if sc.tournament_team_id in keep_team_ids:
                        continue
                    if TournamentTeamMatchStats.objects.filter(
                            match__group__stage=tgt_stage, tournament_team_id=sc.tournament_team_id).exists():
                        continue  # already played in this stage -> keep (never destroy real results)
                    sc.delete()
                    result["removed_stale"] += 1
            for tgt_stage, keep_player_ids in seeded_players_by_stage.items():
                for sc in StageCompetitor.objects.filter(stage=tgt_stage, player__isnull=False):
                    if sc.player_id in keep_player_ids:
                        continue
                    if SoloPlayerMatchStats.objects.filter(
                            match__group__stage=tgt_stage, competitor_id=sc.player_id).exists():
                        continue
                    sc.delete()
                    result["removed_stale"] += 1

    # Kick Discord workers AFTER the transaction commits (real runs only).
    if not dry_run and queued_by_stage:
        result["progress_ids"] = _kick_role_workers(queued_by_stage)
    return result


class _nullcontext:
    """Tiny no-op context manager so the engine can wrap real runs in transaction.atomic() but
    dry-runs in nothing (a dry-run touches no DB writes, so it needs no transaction). Avoids
    importing contextlib for one use; mirrors the lightweight-helper style of this app."""
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ── endpoint ─────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def advance_stage_by_rules(request):
    """POST events/advance-stage-by-rules/ {event_id, stage_id, dry_run?} — run (or preview) a
    stage's branching advancement rules.

    REQUEST  {event_id, stage_id, dry_run? (default false)}.
    RESPONSE 200 route_stage_advancement(...) dict (see that function). 400 when the stage has no
             rules (use the per-group/round-robin legacy advance instead), or on missing input;
             403 when the caller may not manage this event; 404 unknown event/stage.
    AUTH     Bearer SessionToken; gate = AFC event admin OR organizer with can_manage_registrations
             on the owning org (_advance_gate) — admins AND organizers, same as seeding.
    FE CALLER  ActionsTab.tsx "Branching advancement" card (admin + organizer event-edit pages):
             a Preview button (dry_run=true) then an Advance button (dry_run=false), shown only for
             stages that have rules. The legacy advance endpoints stay for rule-less stages."""
    user, err = _auth_user(request)
    if err:
        return err

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    if not event_id or not stage_id:
        return Response({"message": "event_id and stage_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    # Scope the stage to the event so a mismatched pair can't advance another event's stage.
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
    if not _advance_gate(user, event):
        return Response(
            {"message": "You do not have permission to advance stages for this event."},
            status=403)

    if not StageAdvancementRule.objects.filter(source_stage=stage).exists():
        return Response({
            "message": "This stage has no branching advancement rules. Use the per-group "
                       "advance for a normal stage, or the round-robin advance for a "
                       "round-robin stage.",
            "branching": False,
        }, status=400)

    dry_run = str(request.data.get("dry_run", "false")).lower() in ("1", "true", "yes")
    result = route_stage_advancement(stage, dry_run=dry_run)
    result["message"] = (
        "Branching advancement preview." if dry_run
        else f"Branching advancement complete: {result['newly_seeded']} newly seeded.")
    return Response(result, status=200)
