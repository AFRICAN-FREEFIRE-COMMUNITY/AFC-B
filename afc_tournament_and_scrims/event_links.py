"""
afc_tournament_and_scrims.event_links — EVENT LINKING / QUALIFICATION CHAINS (P1).

PURPOSE (owner-approved design 2026-06-12, spec: WEBSITE/tasks/event-linking-design.md v2 +
feedback round 1; mockup: frontend/public/_event_linking_preview.html)
    Per-STAGE qualification rules: "the top N of SOURCE STAGE qualify into TARGET EVENT"
    (top 6 of Semis -> event A, top 2 of Finals -> event B; 10 country qualifiers -> one
    Grand Finals). When a link FIRES, the stage's consolidated standings are read, the top N
    become EventQualification rows, and (auto_promote) each is registered into the target
    through the SAME rows register_for_event writes. Every decision is UNDOable; standings
    edited after a fire are DIFFED against the fire-time snapshot and the link creator is
    notified in-app.

HOW IT CONNECTS
    - Models: EventLink / EventQualification (models.py) + the registration rows
      (TournamentTeam / TournamentTeamMember / RegisteredCompetitors).
    - Standings: round_robin.cumulative_standings(stage) for TEAM stages (the same ordered
      effective_total table the stage leaderboards render); a SoloPlayerMatchStats twin below
      for solo events.
    - Fire triggers: the manual fire endpoint (admin presses once standings are final) AND
      views.complete_event, which best-effort fires any still-active links of the event.
    - Permissions: AFC event admins link anything; organizers only events whose org they can
      edit on BOTH ends (org_can_event "can_edit_events").
    - Consumed by: frontend lib/eventLinks.ts -> the "Linked events" card on the admin event
      page and the organizer event page.

ENDPOINTS (mounted under events/ via afc_tournament_and_scrims/urls.py)
    POST   events/<event_id>/links/create/      create_link
    GET    events/<event_id>/links/             list_links      (outbound + inbound + diffs)
    DELETE events/links/<link_id>/              cancel_link
    POST   events/links/<link_id>/fire/         fire_link
    POST   events/links/<link_id>/decide/       decide          (allow|reject|decline|
                                                                 replace_next|replace_team|undo)
"""
from django.db import models, transaction, IntegrityError
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.models import Notifications, User
from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event
from afc_team.models import Team, TeamMembers

from . import round_robin
from .models import (
    Event, EventLink, EventQualification, RegisteredCompetitors, SoloPlayerMatchStats,
    Stages, TournamentTeam, TournamentTeamMember,
)


# ── auth helpers ─────────────────────────────────────────────────────────────────────────────
def _auth_user(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _is_event_admin(user):
    """AFC event admin (base role admin, or head_admin/event_admin granular). Local copy of the
    views.py helper to avoid importing the 19k-line module at load time."""
    if user.role == "admin":
        return True
    return user.userroles.filter(role__role_name__in=("head_admin", "super_admin", "event_admin")).exists()


def _can_manage_link_events(user, source_event, target_event):
    """Admins link ANY events; organizers only events whose org they can edit on BOTH ends
    (owner: 'admins can link any events, organizers only their own')."""
    if _is_event_admin(user):
        return True
    return org_can_event(user, "can_edit_events", source_event) and org_can_event(
        user, "can_edit_events", target_event,
    )


def _can_manage_link(user, link):
    return _can_manage_link_events(user, link.source_event, link.target_event)


# ── standings: ordered top rows of a stage ───────────────────────────────────────────────────
def _stage_top_rows(stage, participant_type):
    """The stage's consolidated standings, ordered, as
    [{"team_id"|"user_id": int, "name": str}] (best first).

    TEAM stages reuse round_robin.cumulative_standings (the SAME effective_total table the
    stage leaderboards render; tournament_team_id is resolved back to the platform team).
    SOLO stages aggregate SoloPlayerMatchStats with the identical points formula.

    OWNER 2026-06-29: when `stage` is a Point-Rush TARGET (has point_rush_sources) the banked
    carry-over is folded into the ranking BEFORE the caller slices the top N (fire_link takes
    rows[:qualify_count]). So CROSS-EVENT linked qualification ranks on the SAME carry-over-inclusive
    total as in-event advancement (advance_*) and the leaderboard, per the owner ("the point adds to
    the team's total points"). No-op for a stage with no Point-Rush source, so a normal link fires
    byte-identically."""
    # Lazy imports dodge the views<->event_links cycle (same pattern as _registration_gates below).
    from .views import _carry_over_for_stage, _fold_carry_over

    if participant_type == "solo":
        from django.db.models import Case, Count, F, IntegerField, Sum, Value, When
        from django.db.models.functions import Coalesce

        rows = list(
            SoloPlayerMatchStats.objects.filter(match__group__stage=stage)
            .values("competitor__user_id", name=F("competitor__user__username"))
            .annotate(
                effective_total=(
                    Coalesce(Sum("placement_points"), 0)
                    + Coalesce(Sum("kill_points"), 0)
                    + Coalesce(Sum("bonus_points"), 0)
                    - Coalesce(Sum("penalty_points"), 0)
                ),
                total_booyah=Coalesce(Sum(
                    Case(When(placement=1, then=Value(1)), default=Value(0), output_field=IntegerField()),
                ), 0),
                total_kills=Coalesce(Sum("kills"), 0),
            )
            .order_by("-effective_total", "-total_booyah", "-total_kills", "name")
        )
        # _carry_over_for_stage keys by competitor_id, but this solo table is keyed by user_id. Re-key
        # the bonus to user_id (one RegisteredCompetitors per user per event) so the fold lands on the
        # right player, then fold + re-rank through the shared helper (carry= passes the re-keyed dict).
        carry = _carry_over_for_stage(stage, "solo")
        if carry:
            uid_by_comp = dict(
                RegisteredCompetitors.objects.filter(id__in=list(carry.keys()))
                .values_list("id", "user_id")
            )
            carry_by_user = {}
            for comp_id, bonus in carry.items():
                uid = uid_by_comp.get(comp_id)
                if uid is not None:
                    carry_by_user[uid] = carry_by_user.get(uid, 0) + bonus
            _fold_carry_over(
                rows, stage, "solo",
                id_key="competitor__user_id", metric_key="effective_total",
                sort_key=lambda r: (-int(r.get("effective_total") or 0),
                                    -int(r.get("total_booyah") or 0),
                                    -int(r.get("total_kills") or 0),
                                    r.get("name") or ""),
                carry=carry_by_user,
            )
        return [{"user_id": r["competitor__user_id"], "name": r["name"]} for r in rows]

    # TEAM: fold carry-over into the cumulative table (keyed by tournament_team_id) before resolving
    # each row back to its platform team. The helper resolves the carry dict itself for team scope.
    rows = _fold_carry_over(
        round_robin.cumulative_standings(stage), stage, participant_type,
        id_key="tournament_team_id", metric_key="effective_total",
        sort_key=lambda r: (-int(r.get("effective_total") or 0),
                            -int(r.get("total_booyah") or 0),
                            -int(r.get("total_kills") or 0),
                            r.get("team_name") or ""),
    )
    out = []
    for r in rows:
        tt = TournamentTeam.objects.select_related("team").filter(
            tournament_team_id=r["tournament_team_id"],
        ).first()
        if tt and tt.team_id:
            out.append({"team_id": tt.team_id, "name": r["team_name"]})
    return out


# ── promotion: register a qualification into the target event ───────────────────────────────
def _registration_gates(link, qual):
    """The target's registration CRITERIA still apply to qualified entries (window bypass
    does NOT bypass gates, owner decision). Returns a human reason string, or None when clear."""
    target = link.target_event
    # F3 (owner 2026-06-19): the per-player requirement checks (esports image / profile image /
    # Free Fire UID) now go through the SAME shared helper register_for_event uses, so qualification
    # promotions enforce the exact same gates. Lazy import avoids a views<->event_links cycle.
    from .views import _missing_registration_assets
    from afc_team.views import STAFF_ROLES  # coach/manager/analyst never play -> never on a roster
    if qual.team_id:
        team = qual.team
        if target.require_team_logo and not team.team_logo:
            return "target requires a team logo"
        # Mirror register_for_event (views.py ~5205): EXCLUDE staff before the per-player asset check.
        # Staff are support-only and never appear on the event roster that _promote actually copies, so
        # checking their assets here made the qualification gate STRICTER than registration — a single
        # staffer missing a UID/image would falsely hold a qualifying team pending. (Adversarial-review
        # fix, owner 2026-06-19.)
        staff_ids = set(TeamMembers.objects.filter(
            team=team, management_role__in=STAFF_ROLES,
        ).values_list("member_id", flat=True))
        member_ids = [m for m in TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
                      if m not in staff_ids]
        if _missing_registration_assets(member_ids, target):
            return "target requires every rostered player to complete their profile (esports image / profile image / Free Fire UID)"
    else:
        if qual.user_id and _missing_registration_assets([qual.user_id], target):
            return "target requires you to complete your profile (esports image / profile image / Free Fire UID)"
    return None


def _window_closed(event):
    today = timezone.localdate()
    return bool(event.registration_end_date and event.registration_end_date < today)


def _promote(qual, actor, bypass_window=False):
    """Register the qualification into the target. Returns (ok, reason). Pending reasons:
    duplicate handled as success; closed window (unless an admin Allow bypasses it) and
    failing gates hold the row pending."""
    link = qual.link
    target = link.target_event

    gate = _registration_gates(link, qual)
    if gate:
        return False, gate
    if not bypass_window and _window_closed(target):
        return False, "registration window closed: allow or reject"

    with transaction.atomic():
        if qual.team_id:
            # Already in the target (registered directly or via another qualifier): point at
            # the existing registration, never duplicate.
            if RegisteredCompetitors.objects.filter(event=target, team=qual.team).exists():
                qual.status = "promoted"
                qual.note = "already registered in the target"
                qual.save(update_fields=["status", "note"])
                return True, None

            RegisteredCompetitors.objects.create(event=target, team=qual.team, status="registered")
            tt = TournamentTeam.objects.create(
                event=target, team=qual.team, status="active",
                registered_by=actor or link.created_by, country=qual.team.country,
            )
            # Roster: copy the SOURCE event's finishing roster when it exists, else the team's
            # current members. roster_mode=captain_repick copies too, then tells the captain to
            # confirm/edit via the existing Edit Registration flow before roster lock.
            src_member_ids = list(
                TournamentTeamMember.objects.filter(
                    tournament_team__event=link.source_event, tournament_team__team=qual.team,
                ).values_list("user_id", flat=True)
            ) or list(TeamMembers.objects.filter(team=qual.team).values_list("member_id", flat=True))
            TournamentTeamMember.objects.bulk_create([
                TournamentTeamMember(tournament_team=tt, user_id=uid, event=target, status="active")
                for uid in dict.fromkeys(src_member_ids)
            ], batch_size=200)
            qual.promoted_tournament_team = tt

            # Notify the captain: qualified (+ the re-pick ask when the link demands it).
            captain = TeamMembers.objects.filter(
                team=qual.team, management_role="team_captain",
            ).select_related("member").first()
            notify_user = captain.member if captain else (qual.team.team_owner if qual.team.team_owner_id else None)
            if notify_user:
                extra = (
                    " Review and confirm your roster via Edit Registration before the roster locks."
                    if link.roster_mode == "captain_repick" else ""
                )
                Notifications.objects.create(
                    user=notify_user,
                    notification_type="qualification",
                    title=f"Qualified for {target.event_name}",
                    message=(
                        f"{qual.team.team_name} finished #{qual.placement} in "
                        f"{link.source_stage.stage_name} of {link.source_event.event_name} and has "
                        f"been entered into {target.event_name}.{extra} You can decline the slot "
                        "from the event page."
                    ),
                    related_event=target,
                )
        else:
            if RegisteredCompetitors.objects.filter(event=target, user=qual.user).exists():
                qual.status = "promoted"
                qual.note = "already registered in the target"
                qual.save(update_fields=["status", "note"])
                return True, None
            qual.promoted_competitor = RegisteredCompetitors.objects.create(
                event=target, user=qual.user, status="registered",
            )
            Notifications.objects.create(
                user=qual.user,
                notification_type="qualification",
                title=f"Qualified for {target.event_name}",
                message=(
                    f"You finished #{qual.placement} in {link.source_stage.stage_name} of "
                    f"{link.source_event.event_name} and have been entered into {target.event_name}."
                ),
                related_event=target,
            )

        qual.status = "promoted"
        qual.note = "roster copied" if qual.team_id else "registered"
        qual.decided_by = actor
        qual.decided_at = timezone.now()
        qual.save()
    return True, None


def _withdraw_promotion(qual):
    """Delete the registration rows a promotion created (used by undo / reject-after-allow).
    Only rows TRACKED on the qualification are touched - a pre-existing registration
    ('already registered in the target') is never deleted."""
    if qual.promoted_tournament_team_id:
        tt = qual.promoted_tournament_team
        TournamentTeamMember.objects.filter(tournament_team=tt).delete()
        RegisteredCompetitors.objects.filter(event=qual.link.target_event, team=tt.team).delete()
        tt.delete()
        qual.promoted_tournament_team = None
    if qual.promoted_competitor_id:
        qual.promoted_competitor.delete()
        qual.promoted_competitor = None


def fire_link(link, actor=None):
    """Read the source stage's standings, create the top-N qualifications, auto-promote when
    the link says so, snapshot the top-N for later edit-diffs. Idempotent: an already-fired
    link only APPENDS qualifications for placements it does not have yet (a re-fire after a
    standings edit goes through the diff/apply flow instead)."""
    rows = _stage_top_rows(link.source_stage, link.source_event.participant_type)
    top = rows[: link.qualify_count]
    if not top:
        return [], "The stage has no standings yet."

    created = []
    for i, row in enumerate(top, start=1):
        qual, was_created = EventQualification.objects.get_or_create(
            link=link, placement=i,
            defaults={"team_id": row.get("team_id"), "user_id": row.get("user_id")},
        )
        if was_created:
            created.append(qual)
            if link.auto_promote:
                ok, reason = _promote(qual, actor)
                if not ok:
                    qual.status = "pending"
                    qual.note = reason
                    qual.save(update_fields=["status", "note"])

    link.status = "fired"
    link.fired_snapshot = {"top": top, "change_notified": False}
    link.save(update_fields=["status", "fired_snapshot"])
    return created, None


def fire_links_for_event(event, actor=None):
    """Best-effort: fire every still-active link of `event` (called from complete_event so a
    completed source pushes its qualifiers out even if nobody pressed Fire). Never raises."""
    fired = 0
    for link in EventLink.objects.filter(source_event=event, status="active"):
        try:
            created, err = fire_link(link, actor)
            if not err:
                fired += 1
        except Exception:
            continue
    return fired


# ── serialization ────────────────────────────────────────────────────────────────────────────
def _serialize_qual(q):
    return {
        "id": q.id,
        "placement": q.placement,
        "team_id": q.team_id,
        "user_id": q.user_id,
        "name": (q.team.team_name if q.team_id else (q.user.username if q.user_id else "?")),
        "status": q.status,
        "note": q.note,
        "can_undo": bool(q.prev_status),
    }


def _link_diff(link):
    """Diff the CURRENT stage top-N against the fire-time snapshot. Non-empty = the source
    standings were edited after the link fired (the 'standings edited' banner)."""
    if link.status != "fired" or not link.fired_snapshot:
        return []
    current = _stage_top_rows(link.source_stage, link.source_event.participant_type)[: link.qualify_count]
    old = link.fired_snapshot.get("top") or []
    diff = []
    for i in range(max(len(current), len(old))):
        new_row = current[i] if i < len(current) else None
        old_row = old[i] if i < len(old) else None
        if (new_row or {}).get("team_id") != (old_row or {}).get("team_id") or \
           (new_row or {}).get("user_id") != (old_row or {}).get("user_id"):
            diff.append({
                "placement": i + 1,
                "was": (old_row or {}).get("name"),
                "now": (new_row or {}).get("name"),
                "now_team_id": (new_row or {}).get("team_id"),
                "now_user_id": (new_row or {}).get("user_id"),
            })
    return diff


def _serialize_link(link, with_quals=True, check_diff=False, requester=None):
    out = {
        "id": link.id,
        "source_event_id": link.source_event_id,
        "source_event_name": link.source_event.event_name,
        "source_stage_id": link.source_stage_id,
        "source_stage_name": link.source_stage.stage_name,
        "target_event_id": link.target_event_id,
        "target_event_name": link.target_event.event_name,
        "qualify_count": link.qualify_count,
        "auto_promote": link.auto_promote,
        "roster_mode": link.roster_mode,
        "status": link.status,
        "created_by": link.created_by.username if link.created_by_id else None,
    }
    # ── capacity heads-up (linking P2) ──
    # Warn BEFORE a fire would push the target past its cap: registered+approved competitors
    # already in the target plus this link's qualify_count vs max_teams_or_players. Promotion
    # itself never blocks on capacity (the admin Allow path can still overrule); this is the
    # advisory the card renders as a gold "target nearly full" badge.
    cap = link.target_event.max_teams_or_players or 0
    taken = RegisteredCompetitors.objects.filter(
        event=link.target_event, status__in=("registered", "approved"),
    ).count()
    out["target_capacity"] = cap
    out["target_registered"] = taken
    out["capacity_warning"] = bool(cap) and link.status == "active" and (taken + link.qualify_count) > cap
    if with_quals:
        out["qualifications"] = [
            _serialize_qual(q) for q in link.qualifications.select_related("team", "user").order_by("placement")
        ]
    if check_diff:
        diff = _link_diff(link)
        out["standings_changed"] = bool(diff)
        out["diff"] = diff
        # Notify the link creator ONCE per drift (the in-app "standings edited" ping the
        # owner asked for). The flag rides inside fired_snapshot so no extra column.
        if diff and link.created_by_id and not (link.fired_snapshot or {}).get("change_notified"):
            Notifications.objects.create(
                user=link.created_by,
                notification_type="link_standings_changed",
                title="Linked-event standings changed",
                message=(
                    f"The {link.source_stage.stage_name} standings of {link.source_event.event_name} "
                    f"changed after your qualification link into {link.target_event.event_name} fired. "
                    "Review the difference on the event page."
                ),
                related_event=link.target_event,
            )
            link.fired_snapshot["change_notified"] = True
            link.save(update_fields=["fired_snapshot"])
    return out


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def create_link(request, event_id):
    """POST events/<event_id>/links/create/
    body: {source_stage_id, target_event_id, qualify_count?, auto_promote?, roster_mode?}

    Create a per-stage qualification link from THIS event's stage into a target event.
    Validation: stage belongs to this event; no self-link; participant types match; no cycles
    (walking the link graph from the target back must never reach this event); unique per
    (stage, target). Auth: AFC event admin, or organizer with can_edit_events on BOTH events.
    Consumed by: the Linked events card's create dialog."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        source_event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)
    try:
        # Stages' PK is stage_id, so look up by pk (an `id=` lookup would FieldError).
        stage = Stages.objects.get(pk=request.data.get("source_stage_id"), event=source_event)
    except (Stages.DoesNotExist, ValueError, TypeError):
        return Response({"message": "That stage does not belong to this event."}, status=404)
    try:
        target = Event.objects.get(event_id=request.data.get("target_event_id"))
    except Event.DoesNotExist:
        return Response({"message": "Target event not found."}, status=404)

    if target.event_id == source_event.event_id:
        return Response({"message": "An event cannot qualify into itself."}, status=400)
    if target.participant_type != source_event.participant_type:
        return Response({"message": "Source and target must have the same participant type."}, status=400)
    if not _can_manage_link_events(user, source_event, target):
        return Response({"message": "You do not have permission to link these events."}, status=403)

    # Cycle guard: from the target, walk outbound links; reaching the source event = cycle.
    seen, frontier = set(), {target.event_id}
    while frontier:
        nxt = set(
            EventLink.objects.filter(source_event_id__in=frontier)
            .exclude(status="cancelled")
            .values_list("target_event_id", flat=True)
        )
        if source_event.event_id in nxt:
            return Response({"message": "That link would create a qualification cycle."}, status=400)
        seen |= frontier
        frontier = nxt - seen

    try:
        qualify_count = max(1, int(request.data.get("qualify_count", 2)))
    except (TypeError, ValueError):
        qualify_count = 2
    roster_mode = request.data.get("roster_mode") or "copy"
    if roster_mode not in ("copy", "captain_repick"):
        return Response({"message": "roster_mode must be copy or captain_repick."}, status=400)

    if EventLink.objects.filter(source_stage=stage, target_event=target).exclude(status="cancelled").exists():
        return Response({"message": "That stage is already linked to that event."}, status=400)

    # A previously-CANCELLED link for this (stage, target) still occupies the DB unique constraint
    # (uniq_stage_target_link is unconditional, and MySQL cannot do a partial/filtered index). The
    # duplicate guard above only looks at non-cancelled links, so re-creating after an Unlink would
    # otherwise hit an IntegrityError -> an opaque 500 ("Failed to create the link"). Its promotions
    # were already withdrawn on cancel, so its qualifications carry no live registrations; drop the
    # stale row (CASCADE clears those quals) before inserting the fresh link. (owner bug 2026-06-29)
    with transaction.atomic():
        EventLink.objects.filter(
            source_stage=stage, target_event=target, status="cancelled"
        ).delete()
        try:
            link = EventLink.objects.create(
                source_event=source_event,
                source_stage=stage,
                target_event=target,
                qualify_count=qualify_count,
                auto_promote=bool(request.data.get("auto_promote", True)),
                roster_mode=roster_mode,
                created_by=user,
            )
        except IntegrityError:
            # Defensive: a concurrent create raced us to the unique pair. Surface a clean message
            # instead of a 500 so the dialog shows why.
            return Response(
                {"message": "That stage is already linked to that event."}, status=400
            )
    return Response({"message": "Link created.", "link": _serialize_link(link)}, status=201)


@api_view(["GET"])
def list_links(request, event_id):
    """GET events/<event_id>/links/  — the event's OUTBOUND links (with qualifications + the
    standings-edited diff check) and INBOUND links (who feeds this event). Auth: any manager
    of this event (admin / org can_edit_events)."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)
    if not (_is_event_admin(user) or org_can_event(user, "can_edit_events", event)):
        return Response({"message": "You do not have permission to view this event's links."}, status=403)

    outbound = [
        _serialize_link(l, check_diff=True)
        for l in EventLink.objects.filter(source_event=event).exclude(status="cancelled")
        .select_related("source_event", "source_stage", "target_event", "created_by")
    ]
    inbound = [
        _serialize_link(l)
        for l in EventLink.objects.filter(target_event=event).exclude(status="cancelled")
        .select_related("source_event", "source_stage", "target_event", "created_by")
    ]
    return Response({"outbound": outbound, "inbound": inbound})


@api_view(["GET"])
def public_inbound_links(request, event_id):
    """GET events/<event_id>/links/public/  — PUBLIC provenance read (linking P2): the FIRED
    inbound links of an event with the names of who qualified through each. Powers the
    "Qualified field" banner on the public tournament page (components/qualified-from-banner
    .tsx); only promoted/replaced names are shown (pending/declined slots stay private).
    No auth: this is the public face of an already-public registration list."""
    rows = []
    for link in (
        EventLink.objects.filter(target_event_id=event_id, status="fired")
        .select_related("source_event", "source_stage")
    ):
        names = [
            (q.team.team_name if q.team_id else (q.user.username if q.user_id else None))
            for q in link.qualifications.filter(status__in=("promoted", "replaced"))
            .select_related("team", "user").order_by("placement")
        ]
        rows.append({
            "source_event_name": link.source_event.event_name,
            "source_event_id": link.source_event_id,
            "source_stage_name": link.source_stage.stage_name,
            "qualify_count": link.qualify_count,
            "qualifiers": [n for n in names if n],
        })
    return Response({"results": rows})


@api_view(["GET"])
def public_structure_links(request, event_id):
    """GET events/<event_id>/links/structure/  — PUBLIC, NO AUTH. Both directions of an event's
    qualification links so the public tournament page can render its place in the season:
      inbound  = events that qualify INTO this event (this event is the LINK TARGET); each row
                 names the SOURCE event (slug + name), the source stage, and how many qualify.
      outbound = events THIS event qualifies INTO (this event is the LINK SOURCE); each row
                 names the TARGET event (slug + name), this event's source stage, and the count.

    Only non-cancelled links (status active|fired) are returned, so users see the structure even
    before a link FIRES (a planned "top 6 of Semis qualify into the Finals" shows immediately).
    Unlike public_inbound_links (which lists who qualified, fired only), this is structural: the
    SHAPE of the chain, not the names of qualifiers. The `event_slug` on each row drives the chip
    navigation to /tournaments/<slug>.

    Mirrors list_links' source/target resolution exactly, plus the Event.slug field for routing.
    Consumed by: frontend lib/eventLinks.ts publicStructure() -> the "Qualification Links" section
    of app/(user)/tournaments/[slug]/_components/TournamentStructure.tsx."""
    # ── inbound: this event is the TARGET; list each SOURCE event that feeds it ──
    inbound = []
    for link in (
        EventLink.objects.filter(target_event_id=event_id)
        .exclude(status="cancelled")
        .select_related("source_event", "source_stage")
    ):
        inbound.append({
            "link_id": link.id,
            "event_id": link.source_event_id,            # the event to navigate to (the feeder)
            "event_slug": link.source_event.slug,         # Event.slug -> /tournaments/<slug>
            "event_name": link.source_event.event_name,
            "stage_name": link.source_stage.stage_name,   # the source stage whose top-N qualify
            "qualify_count": link.qualify_count,
            "status": link.status,
        })

    # ── outbound: this event is the SOURCE; list each TARGET event it qualifies into ──
    outbound = []
    for link in (
        EventLink.objects.filter(source_event_id=event_id)
        .exclude(status="cancelled")
        .select_related("target_event", "source_stage")
    ):
        outbound.append({
            "link_id": link.id,
            "event_id": link.target_event_id,            # the event to navigate to (the destination)
            "event_slug": link.target_event.slug,         # Event.slug -> /tournaments/<slug>
            "event_name": link.target_event.event_name,
            "stage_name": link.source_stage.stage_name,   # THIS event's stage whose top-N qualify
            "qualify_count": link.qualify_count,
            "status": link.status,
        })

    return Response({"inbound": inbound, "outbound": outbound})


@api_view(["GET"])
def link_chain(request, event_id):
    """GET events/<event_id>/links/chain/  — the WHOLE qualification graph this event belongs
    to (linking P3, the chain map). BFS both directions over non-cancelled links from this
    event; returns {nodes: [{event_id, event_name, event_status, is_focus}],
    edges: [{link_id, source_event_id, source_stage_name, target_event_id, qualify_count,
    status}]} for the frontend's cascade visualization (components/event-link-chain.tsx).
    Auth: same gate as list_links (event manager)."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)
    if not (_is_event_admin(user) or org_can_event(user, "can_edit_events", event)):
        return Response({"message": "You do not have permission to view this event's links."}, status=403)

    # Collect every event reachable through links in EITHER direction (the full season
    # cascade, not just direct neighbours). Bounded walk: each loop only follows edges that
    # touch the frontier, and seen-set growth terminates it.
    seen, frontier = set(), {event.event_id}
    edges_qs = []
    while frontier:
        batch = list(
            EventLink.objects.exclude(status="cancelled")
            .filter(
                models.Q(source_event_id__in=frontier) | models.Q(target_event_id__in=frontier)
            )
            .select_related("source_stage")
        )
        seen |= frontier
        nxt = set()
        for l in batch:
            edges_qs.append(l)
            nxt.add(l.source_event_id)
            nxt.add(l.target_event_id)
        frontier = nxt - seen

    # De-dupe edges (an edge is collected once per touched endpoint).
    edges, seen_edge_ids = [], set()
    for l in edges_qs:
        if l.id in seen_edge_ids:
            continue
        seen_edge_ids.add(l.id)
        edges.append({
            "link_id": l.id,
            "source_event_id": l.source_event_id,
            "source_stage_name": l.source_stage.stage_name,
            "target_event_id": l.target_event_id,
            "qualify_count": l.qualify_count,
            "status": l.status,
        })

    nodes = [
        {
            "event_id": e.event_id,
            "event_name": e.event_name,
            "event_status": e.event_status,
            "is_focus": e.event_id == event.event_id,
        }
        for e in Event.objects.filter(event_id__in=seen)
    ]
    return Response({"nodes": nodes, "edges": edges})


@api_view(["POST"])
def import_competitors(request, event_id):
    """POST events/<event_id>/import-competitors/  body: {source_event_ids: [int, ...]}

    EVENT MERGE (owner ask 2026-06-12): bulk-enter every confirmed competitor of the source
    events into THIS event, e.g. combine the Dynasty Cup Mozambique / Ghana / Tanzania fields
    into one continental event. Unlike a qualification link (top-N of a stage, fires when
    standings settle) this is an IMMEDIATE copy of the whole confirmed field.

    Rules
      - Every source must share the target's participant type; the target itself is excluded.
      - Confirmed = the source's RegisteredCompetitors rows with status registered/approved.
      - Duplicates are SKIPPED, never doubled (a team/player already in the target stays as
        is); per-source imported/skipped counts come back so the admin sees exactly what moved.
      - Team rosters copy the SOURCE event's finishing roster (TournamentTeamMember) when it
        exists, else the team's current members - the same rule _promote uses.
      - Captains/players get a "entered into <target>" notification.
      - Capacity is ADVISORY here (the response flags overflow); the admin chose the merge.
    Auth: AFC event admin, or organizer with can_edit_events on the target AND every source.
    Consumed by: the "Import from events" dialog on the Linked events card."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        target = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)

    raw_ids = request.data.get("source_event_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return Response({"message": "source_event_ids is required."}, status=400)
    sources = list(Event.objects.filter(event_id__in=raw_ids))
    if len(sources) != len(set(raw_ids)):
        return Response({"message": "One or more source events were not found."}, status=404)

    for src in sources:
        if src.event_id == target.event_id:
            return Response({"message": "An event cannot import from itself."}, status=400)
        if src.participant_type != target.participant_type:
            return Response({"message": f"{src.event_name} has a different participant type."}, status=400)
        if not _can_manage_link_events(user, src, target):
            return Response({"message": f"You do not have permission to merge {src.event_name}."}, status=403)

    report = []
    with transaction.atomic():
        for src in sources:
            imported, skipped = 0, 0

            # ── source field, TEAM events ──
            # TournamentTeam is the AUTHORITATIVE team-registration row (capacity checks and
            # the whole engine count it); RegisteredCompetitors per-team rows are inconsistent
            # in real data (bug found on the live Dynasty Cup merge 2026-06-13: Tanzania had 8
            # active TournamentTeams and ZERO RC rows, so an RC-only read imported nothing;
            # even Mozambique had 7 TT vs 5 RC). Take the UNION of both shapes, deduped by
            # team, excluding withdrawn/disqualified/left and waitlisted entries.
            confirmed_teams = {}
            for tt_src in (
                TournamentTeam.objects.filter(event=src, is_waitlisted=False)
                .exclude(status__in=("withdrawn", "disqualified", "left"))
                .select_related("team")
            ):
                confirmed_teams[tt_src.team_id] = tt_src.team
            for rc in (
                RegisteredCompetitors.objects.filter(
                    event=src, status__in=("registered", "approved"), team__isnull=False,
                ).select_related("team")
            ):
                confirmed_teams.setdefault(rc.team_id, rc.team)

            for team in confirmed_teams.values():
                # ── team merge (mirrors _promote's team interior) ──
                # Duplicate guard checks BOTH shapes in the target for the same reason.
                if (
                    RegisteredCompetitors.objects.filter(event=target, team=team).exists()
                    or TournamentTeam.objects.filter(event=target, team=team).exists()
                ):
                    skipped += 1
                    continue
                RegisteredCompetitors.objects.create(event=target, team=team, status="registered")
                tt = TournamentTeam.objects.create(
                    event=target, team=team, status="active",
                    registered_by=user, country=team.country,
                )
                src_member_ids = list(
                    TournamentTeamMember.objects.filter(
                        tournament_team__event=src, tournament_team__team=team,
                    ).values_list("user_id", flat=True)
                ) or list(TeamMembers.objects.filter(team=team).values_list("member_id", flat=True))
                TournamentTeamMember.objects.bulk_create([
                    TournamentTeamMember(tournament_team=tt, user_id=uid, event=target, status="active")
                    for uid in dict.fromkeys(src_member_ids)
                ], batch_size=200)
                captain = TeamMembers.objects.filter(
                    team=team, management_role="team_captain",
                ).select_related("member").first()
                notify_user = captain.member if captain else (
                    team.team_owner if team.team_owner_id else None)
                if notify_user:
                    Notifications.objects.create(
                        user=notify_user,
                        notification_type="qualification",
                        title=f"Entered into {target.event_name}",
                        message=(
                            f"{team.team_name} has been entered into {target.event_name} "
                            f"(merged from {src.event_name})."
                        ),
                        related_event=target,
                    )
                imported += 1

            # ── source field, SOLO events (RC rows are authoritative for solo) ──
            confirmed = (
                RegisteredCompetitors.objects.filter(
                    event=src, status__in=("registered", "approved"), user__isnull=False,
                ).select_related("user")
            )
            for rc in confirmed:
                if rc.user_id:
                    # ── solo merge ──
                    if RegisteredCompetitors.objects.filter(event=target, user=rc.user).exists():
                        skipped += 1
                        continue
                    RegisteredCompetitors.objects.create(event=target, user=rc.user, status="registered")
                    Notifications.objects.create(
                        user=rc.user,
                        notification_type="qualification",
                        title=f"Entered into {target.event_name}",
                        message=(
                            f"You have been entered into {target.event_name} "
                            f"(merged from {src.event_name})."
                        ),
                        related_event=target,
                    )
                    imported += 1
            report.append({
                "source_event_id": src.event_id,
                "source_event_name": src.event_name,
                "imported": imported,
                "skipped_duplicates": skipped,
            })

    total_now = RegisteredCompetitors.objects.filter(
        event=target, status__in=("registered", "approved"),
    ).count()
    return Response({
        "message": "Merge complete.",
        "report": report,
        "target_registered": total_now,
        "target_capacity": target.max_teams_or_players,
        "over_capacity": bool(target.max_teams_or_players) and total_now > target.max_teams_or_players,
    })


@api_view(["DELETE"])
def cancel_link(request, link_id):
    """DELETE events/links/<link_id>/ — cancel a qualification link AND withdraw the
    registrations it auto-promoted into the target event.

    Owner expectation (2026-06-29): unlinking should bring the qualified teams BACK OUT of the
    target event, not leave them registered there. So for every team/competitor THIS link
    promoted (status promoted/replaced), we call _withdraw_promotion — which removes ONLY the
    rows the link itself created (the promoted TournamentTeam + its members + the target
    RegisteredCompetitors row). A team that was ALREADY independently registered in the target
    is never touched. Pass ?keep_registrations=true to cancel the rule but LEAVE the promoted
    teams in place (the old behaviour).

    Consumed by: the Linked events card's Unlink action."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        link = EventLink.objects.select_related("source_event", "target_event").get(id=link_id)
    except EventLink.DoesNotExist:
        return Response({"message": "Link not found."}, status=404)
    if not _can_manage_link(user, link):
        return Response({"message": "You do not have permission to manage this link."}, status=403)

    # Opt-out: ?keep_registrations=true cancels the rule without removing the promoted teams.
    keep = str(request.GET.get("keep_registrations", "")).lower() in ("1", "true", "yes")

    withdrawn = 0
    with transaction.atomic():
        if not keep:
            for qual in link.qualifications.filter(
                status__in=("promoted", "replaced"),
            ).select_related(
                "promoted_tournament_team",
                "promoted_tournament_team__team",
                "promoted_competitor",
            ):
                if qual.promoted_tournament_team_id or qual.promoted_competitor_id:
                    # Remove the link-created registration from the target, then mark the
                    # qualification withdrawn (mirrors the decline/undo paths above).
                    _withdraw_promotion(qual)
                    qual.status = "withdrawn"
                    qual.note = "withdrawn: qualification link cancelled"
                    qual.decided_by, qual.decided_at = user, timezone.now()
                    qual.save()
                    withdrawn += 1
        link.status = "cancelled"
        link.save(update_fields=["status"])

    msg = "Link cancelled."
    if withdrawn:
        msg = (
            f"Link cancelled. {withdrawn} qualified "
            f"{'team' if withdrawn == 1 else 'teams'} removed from the target event."
        )
    return Response({"message": msg, "withdrawn": withdrawn})


@api_view(["POST"])
def fire_link_view(request, link_id):
    """POST events/links/<link_id>/fire/ — read the stage standings now and create/promote the
    top-N qualifications (the manual trigger once standings are final; complete_event also
    fires unfired links automatically)."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        link = EventLink.objects.select_related("source_event", "source_stage", "target_event").get(id=link_id)
    except EventLink.DoesNotExist:
        return Response({"message": "Link not found."}, status=404)
    if not _can_manage_link(user, link):
        return Response({"message": "You do not have permission to manage this link."}, status=403)

    created, fire_err = fire_link(link, user)
    if fire_err:
        return Response({"message": fire_err}, status=400)
    return Response({"message": f"Link fired: {len(created)} qualification(s).",
                     "link": _serialize_link(link)})


@api_view(["POST"])
def decide(request, link_id):
    """POST events/links/<link_id>/decide/  body: {qualification_id, action, team_id?}

    The whole decision surface of the qualification table:
      allow         promote a pending row, bypassing a closed registration window
      reject        reject a pending row (it never enters the target)
      decline       give up the slot (captain self-decline hits this too)
      replace_next  promote the stage's next-placed competitor in a declined row's place
      replace_team  promote a SPECIFIC team (body.team_id) in a declined row's place
      undo          one-step revert of the last decision (withdraws a created registration)
    Every action snapshots prev_status/prev_note first, so undo always works. Captains may
    ONLY decline their own team's row; managers can do everything."""
    user, err = _auth_user(request)
    if err:
        return err
    try:
        link = EventLink.objects.select_related("source_event", "source_stage", "target_event").get(id=link_id)
    except EventLink.DoesNotExist:
        return Response({"message": "Link not found."}, status=404)
    try:
        qual = EventQualification.objects.select_related("team", "user").get(
            id=request.data.get("qualification_id"), link=link,
        )
    except EventQualification.DoesNotExist:
        return Response({"message": "Qualification not found."}, status=404)

    action = request.data.get("action")
    is_manager = _can_manage_link(user, link)
    # Captain self-decline: a captain/owner of the qualified team may decline THEIR OWN slot.
    is_own_captain = bool(
        qual.team_id and TeamMembers.objects.filter(
            team=qual.team, member=user, management_role__in=("team_captain", "vice_captain"),
        ).exists()
    ) or (qual.team_id and qual.team.team_owner_id == user.user_id) or (qual.user_id == user.user_id)
    if not is_manager and not (action == "decline" and is_own_captain):
        return Response({"message": "You do not have permission to manage this link."}, status=403)

    def snapshot():
        qual.prev_status = qual.status
        qual.prev_note = qual.note

    if action == "allow":
        if qual.status != "pending":
            return Response({"message": "Only a pending qualification can be allowed."}, status=400)
        snapshot()
        ok, reason = _promote(qual, user, bypass_window=True)
        if not ok:
            return Response({"message": f"Cannot promote: {reason}."}, status=400)
        qual.prev_status, qual.prev_note = "pending", qual.prev_note
        qual.save(update_fields=["prev_status", "prev_note"])

    elif action == "reject":
        if qual.status != "pending":
            return Response({"message": "Only a pending qualification can be rejected."}, status=400)
        snapshot()
        qual.status = "rejected"
        qual.note = "rejected by admin"
        qual.decided_by, qual.decided_at = user, timezone.now()
        qual.save()

    elif action == "decline":
        if qual.status not in ("promoted", "pending"):
            return Response({"message": "Only a promoted or pending qualification can decline."}, status=400)
        snapshot()
        _withdraw_promotion(qual)
        qual.status = "declined"
        qual.note = "captain declined the slot" if not is_manager else "declined by admin"
        qual.decided_by, qual.decided_at = user, timezone.now()
        qual.save()

    elif action in ("replace_next", "replace_team"):
        if qual.status != "declined":
            return Response({"message": "Replace a slot after it has been declined."}, status=400)
        if action == "replace_next":
            # Next in line: the first standings row not already used by ANY of this link's
            # qualifications (so #N+1, then #N+2 if they were used as a replacement before).
            rows = _stage_top_rows(link.source_stage, link.source_event.participant_type)
            used_teams = set(link.qualifications.exclude(team__isnull=True).values_list("team_id", flat=True))
            used_users = set(link.qualifications.exclude(user__isnull=True).values_list("user_id", flat=True))
            replacement = next(
                (r for r in rows
                 if (r.get("team_id") and r["team_id"] not in used_teams)
                 or (r.get("user_id") and r["user_id"] not in used_users)),
                None,
            )
            if not replacement:
                return Response({"message": "No next-in-line competitor is available."}, status=400)
            new_team_id, new_user_id = replacement.get("team_id"), replacement.get("user_id")
            label = replacement["name"]
        else:
            try:
                team = Team.objects.get(team_id=request.data.get("team_id"))
            except Team.DoesNotExist:
                return Response({"message": "Replacement team not found."}, status=404)
            new_team_id, new_user_id, label = team.team_id, None, team.team_name

        snapshot()
        old_name = qual.team.team_name if qual.team_id else (qual.user.username if qual.user_id else "?")
        qual.team_id, qual.user_id = new_team_id, new_user_id
        ok, reason = _promote(qual, user, bypass_window=True)
        if not ok:
            qual.status = "pending"
            qual.note = f"replacement {label}: {reason}"
            qual.save()
        else:
            qual.status = "replaced"
            qual.note = f"{old_name} replaced by {label}"
            qual.decided_by, qual.decided_at = user, timezone.now()
            qual.save()

    elif action == "undo":
        if not qual.prev_status:
            return Response({"message": "Nothing to undo."}, status=400)
        _withdraw_promotion(qual)
        restored = qual.prev_status
        qual.status, qual.note = restored, (qual.prev_note + " (decision undone)").strip()
        qual.prev_status, qual.prev_note = "", ""
        qual.decided_by, qual.decided_at = user, timezone.now()
        qual.save()
        # Undoing a decline of a PROMOTED row must restore the actual registration too - the
        # decline withdrew it, and a "promoted" status with no registration rows would lie to
        # both the table and the target event. Re-promote (admin undo bypasses the window,
        # same as Allow); the competitor gets a fresh "Qualified" notification, which is the
        # right signal after a mistaken decline.
        if restored == "promoted" and not (qual.promoted_tournament_team_id or qual.promoted_competitor_id):
            ok, reason = _promote(qual, user, bypass_window=True)
            if ok:
                qual.note = (qual.note + " (decision undone)").strip()
                qual.save(update_fields=["note"])
            else:
                qual.status, qual.note = "pending", f"undo could not re-register: {reason}"
                qual.save(update_fields=["status", "note"])

    else:
        return Response({"message": "Unknown action."}, status=400)

    return Response({"message": "Done.", "qualification": _serialize_qual(qual)})
