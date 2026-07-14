"""
afc_tournament_and_scrims.head_to_head_views - the CLASH-SQUAD BRACKET endpoints
(bracket sub-project C; the engine itself lives in head_to_head.py).

Kept out of the 19k-line views.py the same way event_payments.py / event_links.py are:
feature endpoints in their own module, pure logic in its sibling (mirroring the
round_robin.py logic + views.py endpoint split of sub-project B, but with the endpoints
here because views.py is owned by other in-flight work).

ENDPOINTS (the app is mounted at events/, see urls.py)
    POST events/stages/<stage_id>/bracket/generate/   generate_h2h_bracket
        body: {"team_ids": [tournament_team_id, ...] in SEED order (best first),
               "fmt": optional override - else derived from stage.stage_format}
        auth: AFC event admin OR org member with can_edit_events on the event's org.
        Regeneration is allowed only while no REAL match (both teams present) has a
        completed result - auto-completed byes do not block it.
        201 -> {"message", "bracket": <tree, same shape as the GET>}

    GET  events/stages/<stage_id>/bracket/            get_h2h_bracket
        PUBLIC read (no auth) - the bracket page is a spectator surface, like the public
        event pages. 200 -> the full bracket tree + standings (shape documented on the view).

    POST events/h2h-matches/<match_id>/result/        report_h2h_match_result
        body: {"score_a": int, "score_b": int}  (round wins in the CS set)
        auth: AFC event admin OR org member with can_upload_results (the org permission
        documented as "results + leaderboards") on the event's org.
        200 -> {"message", "match": <match object>, "bracket_complete": bool}

CONSUMED BY: the FE Clash Squad bracket surface (admin event page bracket tab + the public
bracket view); the sub-project D bridge (head_to_head.write_placement_stats) then carries
completed-bracket placements into the leaderboard + afc_rankings pipelines automatically.
"""
from django.db import transaction
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from . import head_to_head
from .models import HeadToHeadMatch, Stages, TournamentTeam


# ── auth helpers (local copies, event_links.py idiom: avoid importing 19k-line views.py) ────
def _auth_user(request):
    """Resolve the Bearer token to a user. Returns (user, None) or (None, error Response)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


def _is_event_admin(user):
    """AFC event admin (base role admin/moderator/support, or head_admin/super_admin/
    event_admin granular). Same correct role__role_name__in path as the views.py helper."""
    if user.role in ("admin", "moderator", "support"):
        return True
    return user.userroles.filter(
        role__role_name__in=("head_admin", "super_admin", "event_admin")).exists()


# ── serialization ────────────────────────────────────────────────────────────────────────────
def _team_payload(tt):
    """Minimal team object for a bracket slot; None when the slot is empty/bye."""
    if tt is None:
        return None
    return {"tournament_team_id": tt.tournament_team_id, "team_name": tt.team.team_name}


def _match_payload(m):
    """One HeadToHeadMatch as the FE consumes it. is_bye is derived (completed with a
    missing team) rather than stored - see head_to_head._resolve_byes for the convention."""
    return {
        "h2h_match_id": m.h2h_match_id,
        "bracket": m.bracket,
        "round_number": m.round_number,
        "position": m.position,
        "team_a": _team_payload(m.team_a),
        "team_b": _team_payload(m.team_b),
        "score_a": m.score_a,
        "score_b": m.score_b,
        "winner_id": m.winner_id,
        "status": m.status,
        "is_bye": m.status == "completed" and (m.team_a_id is None or m.team_b_id is None),
        "next_match_id": m.next_match_id,
        "next_match_slot": m.next_match_slot,
        "loser_next_match_id": m.loser_next_match_id,
        "loser_next_match_slot": m.loser_next_match_slot,
        "scheduled_date": m.scheduled_date,
        "scheduled_time": m.scheduled_time,
    }


def _bracket_payload(stage):
    """The full bracket tree + standings for a stage (the GET response body, also echoed
    by generate). Matches are grouped per bracket side, then per round:

    {
      "stage_id", "stage_name", "stage_format",
      "fmt": derived engine format ("single_elim" | "double_elim" | "league" | null),
      "generated": bool,
      "rounds": {
        "winners": [{"round": 1, "matches": [<match>, ...]}, ...],   # incl. the grand final
        "losers":  [...],                                            # double elim only
        "league":  [...]                                             # league / RR H2H only
      },
      "standings": [{tournament_team_id, team_name, placement, wins, losses,
                     rounds_won, rounds_lost}, ...]
    }
    """
    matches = list(
        stage.h2h_matches.select_related("team_a__team", "team_b__team")
        .order_by("round_number", "position")
    )

    # Derive the engine format from the stored matches (authoritative even when fmt was
    # passed explicitly at generation), falling back to the stage_format mapping.
    if any(m.bracket == "league" for m in matches):
        fmt = "league"
    elif any(m.bracket == "losers" for m in matches):
        fmt = "double_elim"
    elif matches:
        fmt = "single_elim"
    else:
        fmt = head_to_head.FORMAT_FROM_STAGE.get(stage.stage_format)

    rounds = {"winners": [], "losers": [], "league": []}
    by_round = {}
    for m in matches:
        by_round.setdefault((m.bracket, m.round_number), []).append(m)
    for (bracket, round_number) in sorted(by_round):
        rounds[bracket].append({
            "round": round_number,
            "matches": [_match_payload(m) for m in by_round[(bracket, round_number)]],
        })

    return {
        "stage_id": stage.stage_id,
        "stage_name": stage.stage_name,
        "stage_format": stage.stage_format,
        "fmt": fmt,
        "generated": bool(matches),
        "rounds": rounds,
        "standings": head_to_head.standings(stage) if matches else [],
    }


# ── endpoints ────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def generate_h2h_bracket(request, stage_id):
    """POST events/stages/<stage_id>/bracket/generate/ - build (or rebuild) the bracket.

    Request : {"team_ids": [tournament_team_id, ...] in seed order (index 0 = seed 1),
               "fmt": optional "single_elim"|"double_elim"|"league"|"round_robin_h2h"}
    Auth    : AFC event admin OR org_can_event("can_edit_events") on the stage's event.
    Guards  : team_ids must be >= 2 unique TournamentTeam ids belonging to the stage's
              event; fmt must resolve (CS stage_format or explicit); regeneration is
              refused once any REAL match has a completed result (byes do not count).
    Response: 201 {"message", "bracket": <_bracket_payload tree>}.
    Consumed by the admin/organizer "Generate bracket" action on the CS stage surface.
    """
    user, err = _auth_user(request)
    if err:
        return err

    stage = get_object_or_404(Stages.objects.select_related("event"), stage_id=stage_id)
    event = stage.event

    # Gate: AFC event admins always; otherwise org members who may edit this org's events.
    if not _is_event_admin(user) and not org_can_event(user, "can_edit_events", event):
        return Response({"message": "You do not have permission to manage this event's bracket."},
                        status=403)

    # fmt: explicit override wins, else derive from the CS stage_format strings.
    fmt = request.data.get("fmt") or head_to_head.FORMAT_FROM_STAGE.get(stage.stage_format)
    if not fmt:
        return Response(
            {"message": "This stage is not a Clash Squad format; pass fmt explicitly "
                        "(single_elim, double_elim, league, round_robin_h2h)."},
            status=400)
    if fmt not in head_to_head.VALID_FORMATS:
        return Response({"message": f"Unknown bracket format '{fmt}'."}, status=400)

    # team_ids: a non-empty, duplicate-free list of this event's TournamentTeam ids.
    team_ids = request.data.get("team_ids")
    if not isinstance(team_ids, list) or len(team_ids) < 2:
        return Response({"message": "team_ids must be a list of at least 2 tournament team ids "
                                    "in seed order."}, status=400)
    # Coerce to ints up front (P2, owner 2026-07-13): a non-numeric id (e.g. "abc", null, a float)
    # used to reach the `__in=team_ids` query and raise an uncaught 500. Reject it as a clean 400
    # instead. Booleans are ints in Python but never a real team id, so refuse them explicitly.
    try:
        team_ids = [int(t) for t in team_ids if not isinstance(t, bool)]
        if len(team_ids) != len(request.data.get("team_ids")):
            raise ValueError
    except (TypeError, ValueError):
        return Response({"message": "team_ids must all be integer tournament team ids."}, status=400)
    if len(set(team_ids)) != len(team_ids):
        return Response({"message": "team_ids contains duplicates: each team can only be "
                                    "seeded once."}, status=400)
    if fmt == "double_elim" and len(team_ids) < 3:
        return Response({"message": "Double elimination needs at least 3 teams."}, status=400)
    valid_ids = set(
        TournamentTeam.objects.filter(event=event, tournament_team_id__in=team_ids)
        .values_list("tournament_team_id", flat=True))
    unknown = [t for t in team_ids if t not in valid_ids]
    if unknown:
        return Response({"message": f"These tournament team ids do not belong to this event: "
                                    f"{unknown}."}, status=400)

    # Regeneration guard: only while no REAL result has been entered. A bye is completed
    # with one empty slot, so requiring both teams filters byes out.
    if HeadToHeadMatch.objects.filter(
            stage=stage, status="completed",
            team_a__isnull=False, team_b__isnull=False).exists():
        return Response({"message": "Results have already been entered for this bracket; "
                                    "it can no longer be regenerated."}, status=400)

    # Replace any previous (result-free) bracket atomically.
    with transaction.atomic():
        HeadToHeadMatch.objects.filter(stage=stage).delete()
        try:
            head_to_head.generate_bracket(stage, team_ids, fmt)
        except head_to_head.BracketError as e:
            # atomic() rolls the delete back too, so a failed generate leaves the old
            # bracket untouched.
            transaction.set_rollback(True)
            return Response({"message": str(e)}, status=400)

    return Response({"message": "Bracket generated.",
                     "bracket": _bracket_payload(stage)}, status=201)


@api_view(["GET"])
def get_h2h_bracket(request, stage_id):
    """GET events/stages/<stage_id>/bracket/ - the full bracket tree + standings.

    PUBLIC (no auth): this is the spectator bracket page, the H2H counterpart of the
    public event leaderboards. Response shape: see _bracket_payload's docstring.
    Consumed by the FE bracket renderer (public event page + the admin bracket tab,
    which layers its controls over the same read).
    """
    stage = get_object_or_404(Stages, stage_id=stage_id)
    return Response(_bracket_payload(stage), status=200)


@api_view(["POST"])
def report_h2h_match_result(request, match_id):
    """POST events/h2h-matches/<match_id>/result/ - record a Clash Squad set result.

    Request : {"score_a": int, "score_b": int} (round wins; no ties in elimination).
    Auth    : AFC event admin OR org_can_event("can_upload_results") on the match's event
              (can_upload_results is the org toggle documented as "results + leaderboards",
              matching how get_all_leaderboard_details_for_event gates its result surface).
    Behavior: delegates to head_to_head.report_result - sets winner, advances winner/loser,
              cascades any byes this reveals, and (when the bracket completes) refreshes
              the sub-project D synthetic placement stats automatically. Re-reporting is
              allowed until a downstream match completes.
    Response: 200 {"message", "match": <match object>, "bracket_complete": bool};
              validation failures come back 400 with the BracketError message.
    Consumed by the admin/organizer "Enter result" action on each bracket match card.
    """
    user, err = _auth_user(request)
    if err:
        return err

    match = get_object_or_404(
        HeadToHeadMatch.objects.select_related(
            "stage__event", "team_a__team", "team_b__team", "next_match", "loser_next_match"),
        h2h_match_id=match_id)
    event = match.stage.event

    if not _is_event_admin(user) and not org_can_event(user, "can_upload_results", event):
        return Response({"message": "You do not have permission to enter results for this event."},
                        status=403)

    score_a = request.data.get("score_a")
    score_b = request.data.get("score_b")
    if score_a is None or score_b is None:
        return Response({"message": "score_a and score_b are required."}, status=400)

    try:
        bracket_complete = head_to_head.report_result(match, score_a, score_b, acting_user=user)
    except head_to_head.BracketError as e:
        return Response({"message": str(e)}, status=400)

    # Re-fetch so the echoed match carries the propagation-fresh team objects.
    match.refresh_from_db()
    return Response({
        "message": "Result recorded.",
        "match": _match_payload(match),
        "bracket_complete": bracket_complete,
    }, status=200)
