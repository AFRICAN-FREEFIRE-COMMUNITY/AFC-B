"""
Teams submit their own per-map results, organizers approve them (owner 2026-08-04, item 6).

WHAT THIS IS: five endpoints. A player on a registered team proposes their team's row for one
map; the organizer sees a queue of proposals for the match, and approves (optionally after
correcting it) or rejects with a reason. Approval is the only thing here that touches the
standings, and it does so through result_writes.write_team_result_row, the same function
views.enter_team_match_result_manual uses, so an approved submission produces exactly the rows
the organizer would have produced by typing it themselves.

WHY IT IS BUILT THIS WAY: see the TeamMapResultSubmission docstring in models.py for the state
machine, why a team submits only its OWN row, and what stays auditable.

WHO CALLS WHAT
    POST   events/team-map-results/submit/               team member       -> submit or replace
    GET    events/team-map-results/mine/?match_id=       team member       -> my team's rows
    GET    events/team-map-results/queue/?match_id=      organizer / admin -> the review queue
    POST   events/team-map-results/<id>/approve/         organizer / admin -> write the result
    POST   events/team-map-results/<id>/reject/          organizer / admin -> refuse, with why

FRONTEND SURFACES THAT CONSUME THEM
    * the team's own event page, "Submit our result" for a map, and the list of what it has
      already submitted with each row's status and any rejection note;
    * the organizer's event results page, a "Team submissions" queue beside the existing manual
      entry and OCR review, so a map can be assembled from what the teams sent.

CONVENTION NOTE: function-based @api_view views with the inline Authorization header +
validate_token preamble, the house idiom in views.py and afc_sso/admin_api.py. Admin gating uses
role__role_name__in through the existing _is_event_admin / org_can_event helpers, never the buggy
role_name__in (UserRoles has no such column, it lives on Roles).
"""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token

from . import result_writes, roster_roles
from .models import (
    Match,
    TeamMapResultSubmission,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)


# ──────────────────────────────────────────────────────────────────────────────
# Shared preamble
# ──────────────────────────────────────────────────────────────────────────────
def _require_user(request):
    """(user, None) or (None, Response). Same status codes as the rest of the AFC API:
    400 for a missing or malformed header, 401 for a token that does not resolve."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."},
                              status=http.HTTP_400_BAD_REQUEST)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."},
                              status=http.HTTP_401_UNAUTHORIZED)
    return user, None


def _match_and_event(match_id):
    """The match plus the event that owns it, resolved through the match's leaderboard the
    same way the manual entry endpoint does. Returns (match, event, error_response)."""
    from .views import _get_lb_for_match

    match = get_object_or_404(Match, match_id=match_id)
    lb = _get_lb_for_match(match)
    if not lb:
        return match, None, Response(
            {"message": "No leaderboard linked/found for this match."},
            status=http.HTTP_400_BAD_REQUEST)
    return match, lb.event, None


def _can_review(user, event):
    """AFC event admins always; otherwise an org member holding can_upload_results on the
    event's owning org. Exactly the gate enter_team_match_result_manual applies, because
    approving a submission and typing the result by hand are the same act."""
    from .views import _is_event_admin, org_can_event

    return _is_event_admin(user) or org_can_event(user, "can_upload_results", event)


def _team_for_submitter(user, match, event):
    """The TournamentTeam this user may submit for on this match, or (None, reason).

    Two conditions, and both matter. The user has to be an ACTIVE member of the team's event
    roster, which is what stops a player submitting for a team they merely follow. The team has
    to be in THIS match, which is what stops a team in group A filing results for group B.
    """
    member_team_ids = list(
        TournamentTeamMember.objects
        .filter(user=user, tournament_team__event=event, status__in=["active", "approved"])
        .values_list("tournament_team_id", flat=True)
    )
    if not member_team_ids:
        return None, "You are not on a team registered for this event."

    # The teams actually playing this match, read from the group the match belongs to. A match
    # with no group (a bare fixture) falls back to the event's teams, which is the same
    # latitude the manual entry endpoint allows.
    playing_ids = _teams_in_match(match, event)
    for team_id in member_team_ids:
        if team_id in playing_ids:
            return TournamentTeam.objects.filter(pk=team_id).first(), None

    return None, "Your team is not playing in this match."


def _teams_in_match(match, event):
    """Set of tournament_team_ids eligible to appear in this match."""
    group = getattr(match, "stage_group", None) or getattr(match, "group", None)
    if group is not None:
        from .models import StageGroupCompetitor

        ids = set(
            StageGroupCompetitor.objects
            .filter(stage_group=group)
            .values_list("tournament_team_id", flat=True)
        )
        ids.discard(None)
        if ids:
            return ids
    return set(
        TournamentTeam.objects.filter(event=event).values_list("tournament_team_id", flat=True))


def _clean_payload(raw):
    """Validate a team's proposed row. Returns (payload, error_message).

    Deliberately narrow: placement, played, bonus/penalty and a player list. A team cannot send
    a tournament_team_id (it is taken from their membership, never from the body) and cannot
    send point columns (those are computed from the scoring settings at approval time). Anything
    else in the body is ignored rather than rejected, so a future field on the form does not
    break older clients.
    """
    if not isinstance(raw, dict):
        return None, "results must be an object describing your team's row."

    played = bool(raw.get("played", True))

    try:
        placement = int(raw.get("placement") or 0)
    except (TypeError, ValueError):
        return None, "placement must be a whole number."
    if played and placement <= 0:
        return None, "placement is required when your team played this map."

    players_in = raw.get("players")
    if not isinstance(players_in, list) or not players_in:
        return None, "players must be a non-empty list."

    players = []
    for entry in players_in:
        if not isinstance(entry, dict) or not entry.get("user_id"):
            continue
        try:
            players.append({
                "user_id": int(entry["user_id"]),
                "played": bool(entry.get("played", True)),
                "kills": max(0, int(entry.get("kills") or 0)),
                "damage": max(0, int(entry.get("damage") or 0)),
                "assists": max(0, int(entry.get("assists") or 0)),
            })
        except (TypeError, ValueError):
            return None, "kills, damage and assists must be whole numbers."

    if not players:
        return None, "players must include at least one player with a user_id."

    # A squad map is four players. More than four played is a data error the organizer should
    # never have to catch by eye, so it is refused at the door.
    if len([p for p in players if p["played"]]) > 4:
        return None, "At most four players can be marked as having played a map."

    return {
        "placement": placement if played else 0,
        "played": played,
        # Bonus and penalty are an ORGANIZER's judgement (a ruling, a sanction), never a
        # team's to propose, so they are pinned to zero here and can only be set by the
        # organizer when they approve.
        "bonus_points": 0,
        "penalty_points": 0,
        "players": players,
    }, None


def _serialize(submission, *, include_payload=True):
    team = submission.tournament_team
    out = {
        "submission_id": submission.submission_id,
        "match_id": submission.match_id,
        "tournament_team_id": submission.tournament_team_id,
        "team_name": getattr(getattr(team, "team", None), "team_name", "") or "",
        "status": submission.status,
        "submitted_by": submission.submitted_by_id,
        "submitted_by_username": getattr(submission.submitted_by, "username", ""),
        # ISO 8601 UTC. The frontend renders it through LocalTime so every viewer reads it in
        # their own timezone; nothing here is pre-formatted.
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
        "reviewed_by_username": getattr(submission.reviewed_by, "username", "") or "",
        "reviewed_at": submission.reviewed_at.isoformat() if submission.reviewed_at else None,
        "review_note": submission.review_note or "",
    }
    if include_payload:
        out["submitted_payload"] = submission.submitted_payload
        out["approved_payload"] = submission.approved_payload
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 1) submit_team_map_result  (POST events/team-map-results/submit/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def submit_team_map_result(request):
    """A team member proposes their own team's row for one map.

    REQUEST: {"match_id": 12, "results": {placement, played, players:[{user_id, kills, damage,
    assists, played}]}}.
    RESPONSE 201: {"message", "submission": {...}}.
    AUTH: Bearer SessionToken, and the caller must be an active member of a team playing this
    match. 403 when they are not on a team, or their team is not in this match.

    REFUSALS, and why each exists:
      * the event has not switched submissions on            -> 403, most organizers do not want this
      * the match already has an APPROVED result for the team -> 409, a team cannot overwrite a
        ruling by resubmitting; the organizer can still approve a later submission themselves
      * a second pending submission                           -> the first is REPLACED, not queued,
        so the organizer always sees one current answer per team

    CONSUMED BY: the team's own event page, "Submit our result" for a map.
    """
    user, err = _require_user(request)
    if err:
        return err

    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=http.HTTP_400_BAD_REQUEST)

    match, event, err = _match_and_event(match_id)
    if err:
        return err

    if event.participant_type == "solo":
        return Response({"message": "This endpoint is for TEAM events only."},
                        status=http.HTTP_400_BAD_REQUEST)

    if not event.allow_team_result_submissions:
        return Response(
            {"message": "This event is not accepting result submissions from teams."},
            status=http.HTTP_403_FORBIDDEN)

    team, reason = _team_for_submitter(user, match, event)
    if not team:
        return Response({"message": reason}, status=http.HTTP_403_FORBIDDEN)

    payload, err_msg = _clean_payload(request.data.get("results"))
    if err_msg:
        return Response({"message": err_msg}, status=http.HTTP_400_BAD_REQUEST)

    # An approved result is a decision the organizer already made. Letting a team resubmit over
    # it would mean the last team to press the button decides what happened.
    if TeamMapResultSubmission.objects.filter(
            match=match, tournament_team=team, status="approved").exists():
        return Response(
            {"message": "Your result for this map has already been approved. "
                        "Ask the organizer if it needs to change."},
            status=http.HTTP_409_CONFLICT)

    with transaction.atomic():
        # Replace rather than queue: one pending answer per team per map (enforced by the
        # partial unique constraint on the model, and done here so the team sees a clean
        # replacement instead of a constraint error).
        TeamMapResultSubmission.objects.filter(
            match=match, tournament_team=team, status="pending").delete()

        submission = TeamMapResultSubmission.objects.create(
            match=match,
            tournament_team=team,
            submitted_by=user,
            submitted_payload=payload,
            status="pending",
        )

    return Response(
        {"message": "Result submitted. The organizer will review it.",
         "submission": _serialize(submission)},
        status=http.HTTP_201_CREATED)


# ──────────────────────────────────────────────────────────────────────────────
# 2) my_team_map_results  (GET events/team-map-results/mine/?match_id=)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def my_team_map_results(request):
    """Every submission the caller's own team has made for one match.

    PURPOSE: so a team can see what it sent, whether it was approved, and, when it was not, the
    reason. Scoped to the caller's own team: a team cannot read another team's proposals.

    CONSUMED BY: the team's own event page, under the submit form.
    """
    user, err = _require_user(request)
    if err:
        return err

    match_id = request.GET.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=http.HTTP_400_BAD_REQUEST)

    match, event, err = _match_and_event(match_id)
    if err:
        return err

    team, reason = _team_for_submitter(user, match, event)
    if not team:
        return Response({"message": reason}, status=http.HTTP_403_FORBIDDEN)

    rows = (TeamMapResultSubmission.objects
            .filter(match=match, tournament_team=team)
            .select_related("submitted_by", "reviewed_by", "tournament_team__team"))
    return Response({"submissions": [_serialize(r) for r in rows]}, status=http.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 3) team_map_result_queue  (GET events/team-map-results/queue/?match_id=)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def team_map_result_queue(request):
    """The organizer's review queue for one match: every team's submission, newest first.

    Each row carries a `conflicts` list, which is the one thing an organizer cannot see by
    reading the submissions one at a time: another team claiming the same placement on this
    map. Two teams cannot both have come first, so a placement claimed twice means at least one
    of them is wrong and the organizer should look before approving either.

    Conflicts are reported, never blocked at submission time. Blocking would let one team's
    early mistake stop a second team from filing anything at all.

    ?status=pending narrows it; the default returns everything so the organizer can see what
    they already approved or rejected.

    CONSUMED BY: the organizer's event results page, "Team submissions".
    """
    user, err = _require_user(request)
    if err:
        return err

    match_id = request.GET.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=http.HTTP_400_BAD_REQUEST)

    match, event, err = _match_and_event(match_id)
    if err:
        return err

    if not _can_review(user, event):
        return Response({"message": "You do not have permission."}, status=http.HTTP_403_FORBIDDEN)

    rows = list(TeamMapResultSubmission.objects
                .filter(match=match)
                .select_related("submitted_by", "reviewed_by", "tournament_team__team"))

    wanted = (request.GET.get("status") or "").strip()
    if wanted:
        rows = [r for r in rows if r.status == wanted]

    # Placement -> the live claims on it. Only pending and approved rows can conflict; a
    # rejected or superseded row is not claiming anything any more.
    claims = {}
    for row in rows:
        if row.status not in ("pending", "approved"):
            continue
        placement = (row.approved_payload or row.submitted_payload or {}).get("placement")
        if placement:
            claims.setdefault(int(placement), []).append(row)

    out = []
    for row in rows:
        data = _serialize(row)
        placement = (row.approved_payload or row.submitted_payload or {}).get("placement")
        conflicts = []
        if placement and row.status in ("pending", "approved"):
            for other in claims.get(int(placement), []):
                if other.submission_id == row.submission_id:
                    continue
                conflicts.append({
                    "submission_id": other.submission_id,
                    "tournament_team_id": other.tournament_team_id,
                    "team_name": getattr(getattr(other.tournament_team, "team", None),
                                         "team_name", "") or "",
                    "status": other.status,
                    "placement": int(placement),
                })
        data["conflicts"] = conflicts
        out.append(data)

    return Response({"submissions": out, "match_id": match.match_id}, status=http.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 4) approve_team_map_submission  (POST events/team-map-results/<id>/approve/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def approve_team_map_submission(request, submission_id):
    """Approve one team's submission, writing it into the standings.

    REQUEST: optional {"results": {...}} to correct the row before approving, and optional
    bonus_points / penalty_points, which only an organizer may set. Send nothing and the team's
    own numbers are approved as sent.

    WHY THE ORGANIZER CAN EDIT: real submissions arrive with a transposed digit or a missing
    player. Forcing reject-and-resubmit would cost the organizer a round trip for a typo, which
    is the very cost this feature exists to remove. What was submitted and what was approved are
    both kept, so the correction is visible afterwards rather than silent.

    RESPONSE 200: {"message", "submission", "team_stats_id"}.
    AUTH: event admin, or an org member with can_upload_results, the same gate as manual entry.

    A team that already had an approved submission for this map has it marked `superseded`, and
    the stats row is replaced rather than duplicated (result_writes clears the team's row first),
    so approving a correction is safe and leaves the earlier decision in the audit trail.

    CONSUMED BY: the organizer's "Team submissions" queue.
    """
    user, err = _require_user(request)
    if err:
        return err

    submission = get_object_or_404(
        TeamMapResultSubmission.objects.select_related(
            "match", "tournament_team", "tournament_team__team"),
        pk=submission_id)

    match, event, err = _match_and_event(submission.match_id)
    if err:
        return err

    if not _can_review(user, event):
        return Response({"message": "You do not have permission."}, status=http.HTTP_403_FORBIDDEN)

    if submission.status not in ("pending", "rejected"):
        return Response(
            {"message": f"This submission is already {submission.status}."},
            status=http.HTTP_409_CONFLICT)

    # The organizer's corrections, when they sent any, otherwise the team's own row.
    payload = dict(submission.submitted_payload or {})
    if request.data.get("results") is not None:
        corrected, err_msg = _clean_payload(request.data.get("results"))
        if err_msg:
            return Response({"message": err_msg}, status=http.HTTP_400_BAD_REQUEST)
        payload = corrected

    # Bonus and penalty are the organizer's alone, so they are read from THIS request and never
    # from what the team sent (_clean_payload pins the team's values to zero).
    for field in ("bonus_points", "penalty_points"):
        if request.data.get(field) is not None:
            try:
                payload[field] = int(request.data.get(field))
            except (TypeError, ValueError):
                return Response({"message": f"{field} must be a whole number."},
                                status=http.HTTP_400_BAD_REQUEST)

    try:
        ctx = result_writes.scoring_context(match)
    except ValueError as exc:
        return Response({"message": str(exc)}, status=http.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        # An earlier approval for the same team and map is history now, not a live result.
        (TeamMapResultSubmission.objects
         .filter(match=match, tournament_team=submission.tournament_team, status="approved")
         .exclude(pk=submission.pk)
         .update(status="superseded"))

        team_stats = result_writes.write_team_result_row(
            match=match,
            tournament_team_id=submission.tournament_team_id,
            row=payload,
            ctx=ctx,
            frozen_roles=roster_roles.frozen_roles_for_match(match),
        )

        submission.status = "approved"
        submission.approved_payload = payload
        submission.reviewed_by = user
        submission.reviewed_at = timezone.now()
        submission.review_note = (request.data.get("note") or "").strip()
        submission.save(update_fields=[
            "status", "approved_payload", "reviewed_by", "reviewed_at", "review_note"])

        # The match counts as having a result once any team's row is in. Mirrors what the manual
        # entry endpoint sets, so downstream readers (standings, autocomplete) behave the same.
        if not match.result_inputted:
            match.result_inputted = True
            match.save(update_fields=["result_inputted"])

    return Response(
        {"message": "Submission approved and the result recorded.",
         "submission": _serialize(submission),
         "team_stats_id": team_stats.team_stats_id},
        status=http.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 5) reject_team_map_submission  (POST events/team-map-results/<id>/reject/)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def reject_team_map_submission(request, submission_id):
    """Refuse a submission, with a reason the team will read.

    REQUEST: {"note": "why"} and the note is REQUIRED. A team told only "rejected" resubmits the
    same numbers, and the organizer pays for the round trip twice.

    RESPONSE 200: {"message", "submission"}. Writes nothing to the standings.
    AUTH: event admin, or an org member with can_upload_results.

    CONSUMED BY: the organizer's "Team submissions" queue.
    """
    user, err = _require_user(request)
    if err:
        return err

    submission = get_object_or_404(
        TeamMapResultSubmission.objects.select_related("match", "tournament_team"),
        pk=submission_id)

    match, event, err = _match_and_event(submission.match_id)
    if err:
        return err

    if not _can_review(user, event):
        return Response({"message": "You do not have permission."}, status=http.HTTP_403_FORBIDDEN)

    if submission.status != "pending":
        return Response({"message": f"This submission is already {submission.status}."},
                        status=http.HTTP_409_CONFLICT)

    note = (request.data.get("note") or "").strip()
    if not note:
        return Response(
            {"message": "Tell the team why it was rejected, so they can correct it."},
            status=http.HTTP_400_BAD_REQUEST)

    submission.status = "rejected"
    submission.reviewed_by = user
    submission.reviewed_at = timezone.now()
    submission.review_note = note
    submission.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])

    return Response({"message": "Submission rejected.", "submission": _serialize(submission)},
                    status=http.HTTP_200_OK)
