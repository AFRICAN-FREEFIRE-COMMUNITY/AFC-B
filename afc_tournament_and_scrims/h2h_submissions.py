"""
Teams submit their own Clash Squad set results; organizers approve them (owner 2026-08-12).

WHAT THIS IS: the head-to-head counterpart of views_team_submissions.py. That module let a
Battle Royale team send in its own row for a map; it is keyed to a BR `Match` with a placement and
kills, which a Clash Squad set does not have, so a CS event had no player-side path at all - the
organizer was the only person who could enter anything.

Four endpoints, mirroring the BR module's shape so both surfaces read the same way:

    POST events/h2h-matches/<match_id>/submit-result/   team member       -> submit or replace
    GET  events/h2h-matches/<match_id>/submissions/     anyone involved   -> what has been sent
    POST events/h2h-submissions/<id>/approve/           organizer / admin -> write the result
    POST events/h2h-submissions/<id>/reject/            organizer / admin -> refuse, with why

WHY BOTH SIDES MAY SUBMIT: a set has exactly two teams and one scoreline, and both know it
first-hand. Two submissions that AGREE are the strongest evidence an organizer can have, so the
queue says so explicitly ("both teams agree" / "the two teams disagree") instead of quietly taking
whichever arrived first. See the H2HResultSubmission docstring for the state machine.

APPROVAL IS THE ONLY THING HERE THAT TOUCHES THE BRACKET, and it goes through
head_to_head.report_result - the same function the organizer's own "Enter result" calls - so an
approved submission advances the tree, cascades byes and refreshes the placement bridge exactly as
a hand-typed result would.

CONVENTION NOTE: function-based @api_view views with the inline Authorization + validate_token
preamble, the house idiom. Admin gating reuses head_to_head_views._is_event_admin / org_can_event.
"""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_organizers.permissions import org_can_event

from . import h2h_notifications, head_to_head
from .head_to_head_views import _auth_user, _is_event_admin, _match_payload
from .models import (
    H2HResultSubmission,
    HeadToHeadMatch,
    TournamentTeamMember,
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────
def _my_team_in_match(user, match):
    """Which side of this match the caller plays for, or None.

    Membership is read from TournamentTeamMember - the roster frozen for THIS event - so a player
    who has since left the club cannot submit, and one who joined after registration closed cannot
    either. That is the same identity every other result surface uses.
    """
    for tt in (match.team_a, match.team_b):
        if tt is None:
            continue
        if TournamentTeamMember.objects.filter(
                tournament_team=tt, user=user, status__in=("active", "approved")).exists():
            return tt
    return None


def _can_review(user, event):
    """Who may approve or reject: AFC event admins, or org members with can_upload_results - the
    same gate as entering the result by hand."""
    return _is_event_admin(user) or org_can_event(user, "can_upload_results", event)


def _submission_payload(sub, *, include_players=True):
    """One submission for the API. `agreement` is added by the queue view, which can see both."""
    data = {
        "submission_id": sub.submission_id,
        "h2h_match_id": sub.h2h_match_id,
        "tournament_team_id": sub.tournament_team_id,
        # display_name (not .team.team_name): a submission's tournament_team can be a ghost
        # competitor (owner 2026-08-20) - in practice _my_team_in_match only resolves a real
        # AFC member's team, but the review queue below reads whatever is on the row, so this
        # must not assume a real team.
        "team_name": sub.tournament_team.display_name,
        "submitted_by": sub.submitted_by.username if sub.submitted_by_id else "",
        "submitted_at": sub.submitted_at,
        "score_a": (sub.submitted_payload or {}).get("score_a"),
        "score_b": (sub.submitted_payload or {}).get("score_b"),
        "note": sub.note,
        "status": sub.status,
        "reviewed_at": sub.reviewed_at,
        "review_note": sub.review_note,
    }
    if include_players:
        data["players"] = (sub.submitted_payload or {}).get("players") or []
    return data


def _clean_players(raw, tournament_team):
    """Validate the submitting team's own player lines. Returns the cleaned list.

    A team may only file lines for ITS OWN roster: the whole reason a team is trusted with this is
    first-hand knowledge of its own players, and letting it type the opponent's numbers would hand
    it a way to inflate or deflate a rival's stats.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("players must be a list.")
    roster = set(
        TournamentTeamMember.objects
        .filter(tournament_team=tournament_team, status__in=("active", "approved"))
        .values_list("user_id", flat=True))
    cleaned, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("Each player line must be an object.")
        try:
            player_id = int(row.get("player_id"))
            kills = int(row.get("kills") or 0)
            damage = int(row.get("damage") or 0)
            assists = int(row.get("assists") or 0)
        except (TypeError, ValueError):
            raise ValueError("Player lines need whole numbers.")
        if player_id not in roster:
            raise ValueError("You can only enter numbers for players on your own roster.")
        if player_id in seen:
            raise ValueError("The same player appears twice.")
        if min(kills, damage, assists) < 0:
            raise ValueError("Kills, damage and assists cannot be negative.")
        seen.add(player_id)
        cleaned.append({
            "player_id": player_id,
            "tournament_team_id": tournament_team.tournament_team_id,
            "kills": kills, "damage": damage, "assists": assists, "played": True,
        })
    return cleaned


# ── endpoints ────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def submit_h2h_result(request, match_id):
    """POST events/h2h-matches/<match_id>/submit-result/ - a team sends in its own set result.

    Request : {"score_a": int, "score_b": int,     (ALWAYS in the match's own a/b order, so the
                                                    two teams' submissions are comparable)
               "players": [{"player_id", "kills", "damage", "assists"}],   (own roster only)
               "note": "optional line for the organizer"}
    Auth    : a member of one of the two teams in this match (per-event roster).
    Guards  : the match must have both teams and must not already have an approved result -
              once the organizer has entered one, a submission would be arguing with a decision
              that has already advanced the bracket. Submitting again REPLACES this team's own
              pending submission (the old one becomes "superseded") rather than piling up.
    Response: 201 {"message", "submission"}
    Consumed by: components/h2h-bracket.tsx, the player-facing "Submit our result" dialog.
    """
    user, err = _auth_user(request)
    if err:
        return err

    match = get_object_or_404(
        HeadToHeadMatch.objects.select_related(
            "stage__event", "team_a__team", "team_b__team"),
        h2h_match_id=match_id)

    my_team = _my_team_in_match(user, match)
    if my_team is None:
        return Response(
            {"message": "Only a player on one of the two teams in this match can submit its "
                        "result."}, status=403)
    if not (match.team_a_id and match.team_b_id):
        return Response({"message": "This match does not have both teams yet."}, status=400)
    if match.status == "completed":
        return Response(
            {"message": "This match already has a result. Ask the organizer if it is wrong."},
            status=400)

    try:
        score_a = int(request.data.get("score_a"))
        score_b = int(request.data.get("score_b"))
    except (TypeError, ValueError):
        return Response({"message": "score_a and score_b must be whole numbers (round wins)."},
                        status=400)
    if score_a < 0 or score_b < 0:
        return Response({"message": "Scores cannot be negative."}, status=400)

    try:
        players = _clean_players(request.data.get("players"), my_team)
    except ValueError as e:
        return Response({"message": str(e)}, status=400)

    with transaction.atomic():
        # One live proposal per team per match: an edit replaces rather than appends.
        H2HResultSubmission.objects.filter(
            h2h_match=match, tournament_team=my_team, status="pending"
        ).update(status="superseded")
        sub = H2HResultSubmission.objects.create(
            h2h_match=match,
            tournament_team=my_team,
            submitted_by=user,
            submitted_payload={"score_a": score_a, "score_b": score_b, "players": players},
            note=str(request.data.get("note") or "")[:255],
        )

    return Response({
        "message": "Result submitted. The organizer will review it.",
        "submission": _submission_payload(sub),
    }, status=201)


@api_view(["GET"])
def list_h2h_submissions(request, match_id):
    """GET events/h2h-matches/<match_id>/submissions/ - what has been sent in for this set.

    Auth    : an organizer/admin who may review, OR a player on one of the two teams (who sees
              the queue too - a team should be able to tell whether its opponent has agreed with
              it, which is most of what a dispute is about).
    Response: 200 {"submissions": [...], "agreement": "agree"|"disagree"|"one_side"|"none",
                   "can_review": bool}
              `agreement` compares the two teams' PENDING scorelines: agreeing submissions are
              the strongest evidence an organizer can get, and a disagreement is worth showing
              rather than resolving silently.
    Consumed by: components/h2h-bracket.tsx (the organizer's review queue and the team's own
    "what we sent" list).
    """
    user, err = _auth_user(request)
    if err:
        return err

    match = get_object_or_404(
        HeadToHeadMatch.objects.select_related(
            "stage__event", "team_a__team", "team_b__team"),
        h2h_match_id=match_id)
    event = match.stage.event

    can_review = _can_review(user, event)
    if not can_review and _my_team_in_match(user, match) is None:
        return Response({"message": "You are not involved in this match."}, status=403)

    subs = list(
        H2HResultSubmission.objects
        .filter(h2h_match=match)
        .select_related("tournament_team__team", "tournament_team__ghost_team", "submitted_by")
        .order_by("-submitted_at"))

    pending = [s for s in subs if s.status == "pending"]
    by_team = {}
    for s in pending:
        by_team.setdefault(s.tournament_team_id, s)
    if len(by_team) >= 2:
        scores = [((s.submitted_payload or {}).get("score_a"),
                   (s.submitted_payload or {}).get("score_b")) for s in by_team.values()]
        agreement = "agree" if len(set(scores)) == 1 else "disagree"
    elif len(by_team) == 1:
        agreement = "one_side"
    else:
        agreement = "none"

    return Response({
        "submissions": [_submission_payload(s) for s in subs],
        "agreement": agreement,
        "can_review": can_review,
    }, status=200)


@api_view(["POST"])
def approve_h2h_submission(request, submission_id):
    """POST events/h2h-submissions/<submission_id>/approve/ - write the submitted result.

    Request : {"score_a": int, "score_b": int}   optional - send them to CORRECT the submission
                                                 before approving; omit to take it as sent
              {"review_note": "..."}             optional line back to the team
    Auth    : AFC event admin OR org_can_event("can_upload_results") - the same gate as entering
              the result by hand, because this IS entering the result.
    Behavior: delegates to head_to_head.report_result with the submitting team's player lines, so
              the bracket advances, byes cascade and placements refresh exactly as normal. The
              other side's pending submission is marked superseded, since the question it was
              answering is now settled. Both payloads are kept: what the team claimed and what was
              actually written.
    Response: 200 {"message", "match", "bracket_complete"}
    Consumed by: the organizer's review queue on components/h2h-bracket.tsx.
    """
    user, err = _auth_user(request)
    if err:
        return err

    sub = get_object_or_404(
        H2HResultSubmission.objects.select_related(
            "h2h_match__stage__event", "h2h_match__team_a__team", "h2h_match__team_b__team",
            "tournament_team__team", "tournament_team__ghost_team"),
        submission_id=submission_id)
    match = sub.h2h_match
    if not _can_review(user, match.stage.event):
        return Response({"message": "You do not have permission to review results for this "
                                    "event."}, status=403)
    if sub.status != "pending":
        return Response({"message": f"This submission is already {sub.status}."}, status=400)

    payload = sub.submitted_payload or {}
    # An organizer may correct the scoreline before approving; anything they do not send is taken
    # as submitted.
    try:
        score_a = int(request.data.get("score_a", payload.get("score_a")))
        score_b = int(request.data.get("score_b", payload.get("score_b")))
    except (TypeError, ValueError):
        return Response({"message": "score_a and score_b must be whole numbers."}, status=400)

    try:
        with transaction.atomic():
            bracket_complete = head_to_head.report_result(
                match, score_a, score_b, acting_user=user,
                player_stats=payload.get("players") or None)
            sub.status = "approved"
            sub.reviewed_by = user
            sub.reviewed_at = timezone.now()
            sub.review_note = str(request.data.get("review_note") or "")[:255]
            sub.approved_payload = {
                "score_a": score_a, "score_b": score_b,
                "players": payload.get("players") or [],
            }
            sub.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note",
                                    "approved_payload"])
            # The other side's proposal is now moot - the set has a result.
            H2HResultSubmission.objects.filter(
                h2h_match=match, status="pending").exclude(
                submission_id=sub.submission_id).update(status="superseded")
    except head_to_head.BracketError as e:
        return Response({"message": str(e)}, status=400)

    match.refresh_from_db()
    h2h_notifications.notify_match_result(match)
    return Response({
        "message": "Result approved and recorded.",
        "match": _match_payload(match),
        "bracket_complete": bracket_complete,
    }, status=200)


@api_view(["POST"])
def reject_h2h_submission(request, submission_id):
    """POST events/h2h-submissions/<submission_id>/reject/ - refuse a submission, with a reason.

    Request : {"review_note": "why"}  required - a rejection with no reason just tells a team to
              guess, and they will submit the same thing again.
    Auth    : the same review gate as approve.
    Response: 200 {"message", "submission"}
    Consumed by: the organizer's review queue on components/h2h-bracket.tsx.
    """
    user, err = _auth_user(request)
    if err:
        return err

    sub = get_object_or_404(
        H2HResultSubmission.objects.select_related(
            "h2h_match__stage__event", "tournament_team__team", "tournament_team__ghost_team",
            "submitted_by"),
        submission_id=submission_id)
    if not _can_review(user, sub.h2h_match.stage.event):
        return Response({"message": "You do not have permission to review results for this "
                                    "event."}, status=403)
    if sub.status != "pending":
        return Response({"message": f"This submission is already {sub.status}."}, status=400)

    note = str(request.data.get("review_note") or "").strip()
    if not note:
        return Response({"message": "Say why you are rejecting it, so the team can fix it."},
                        status=400)

    sub.status = "rejected"
    sub.reviewed_by = user
    sub.reviewed_at = timezone.now()
    sub.review_note = note[:255]
    sub.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
    return Response({"message": "Submission rejected.",
                     "submission": _submission_payload(sub)}, status=200)
