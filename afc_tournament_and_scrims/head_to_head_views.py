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
               "fmt": optional override - else derived from stage.stage_format,
               "third_place": optional bool - single elimination only, adds the bronze
                              match between the two semifinal losers so 3rd and 4th are
                              played for instead of shared}
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
import datetime

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from . import cs_room
from . import h2h_notifications
from . import head_to_head
from .models import (
    CSRoomConfig,
    HeadToHeadMatch,
    StageCompetitor,
    StageGroupCompetitor,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMember,
)


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


def _optional_user(request):
    """The caller when they sent a valid token, else None. Used by the PUBLIC bracket read so a
    manager sees the room credentials on the same page a spectator reads without them."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ")[1])


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
    # display_name (not .team.team_name): a bracket slot can hold a ghost competitor
    # (owner 2026-08-20, external results import), which has no .team row.
    return {"tournament_team_id": tt.tournament_team_id, "team_name": tt.display_name}


def _match_payload(m, *, room_scopes=None, show_credentials=False):
    """One HeadToHeadMatch as the FE consumes it. is_bye is derived (completed with a
    missing team) rather than stored - see head_to_head._resolve_byes for the convention.

    room_scopes: the {scope: CSRoomConfig} map _bracket_payload prepared once for the whole
    stage, so resolving "the room settings for this match" costs no extra query per match.
    show_credentials: whether the caller may see the room ID and password (a manager always, a
    spectator only once the organizer published them).
    """
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
        # How the result came about: "normal" for a played set, else forfeit / walkover / dq with
        # the organizer's one-line reason (owner 2026-08-12).
        "result_type": m.result_type,
        "result_note": m.result_note,
        # The room settings that apply to THIS set, and where they were inherited from, so a match
        # card can print "13 rounds, Bermuda, inherited from Stage 1" without another request.
        # Null when the event has no room settings configured anywhere.
        "room": _match_room_payload(m, room_scopes, show_credentials),
        # Per-player lines for this set, when the organizer entered them (owner 2026-08-12).
        # Empty list = only the set score was recorded. The FE result dialog pre-fills from this
        # so a correction starts from what was entered last time rather than from blank boxes.
        "player_stats": [
            {
                "player_id": ps.player_id,
                "tournament_team_id": ps.tournament_team_id,
                "kills": ps.kills,
                "damage": ps.damage,
                "assists": ps.assists,
                "played": ps.played,
            }
            for ps in m.player_stats.all()
        ],
    }


def _match_room_payload(match, room_scopes, show_credentials):
    """The room settings in force for one match, resolved match -> stage -> event.

    room_scopes is the pre-loaded {scope: CSRoomConfig|None} map for this stage (see
    _room_scopes_for_stage), so this is pure dictionary work: a 32-match bracket resolves its
    rooms without a single extra query. Returns None when nothing is configured anywhere, which
    is what every event that predates room settings looks like.
    """
    if not room_scopes:
        return None
    # match -> GROUP -> stage -> event, the same ladder cs_room.resolve_for_match walks, done
    # against the pre-loaded map so a 32-match bracket still costs no extra query.
    own = room_scopes.get("match", {}).get(match.h2h_match_id)
    group_config = room_scopes.get("group", {}).get(match.group_id) if match.group_id else None
    config = own or group_config or room_scopes.get("stage") or room_scopes.get("event")
    if config is None:
        return None
    source = ("match" if own else
              "group" if group_config else
              "stage" if room_scopes.get("stage") else "event")
    visible = bool(show_credentials or config.is_published)
    return {
        "source_scope": source,
        "summary": cs_room.summary(config),
        "room_id": config.room_id if visible else "",
        "room_password": config.room_password if visible else "",
        "notes": config.notes if visible else "",
        "is_published": config.is_published,
        "has_room_credentials": bool(config.room_id or config.room_password),
    }


def _room_scopes_for_stage(stage, matches):
    """Load every room configuration that could apply inside this stage, in 2 queries.

    {"match": {h2h_match_id: config}, "stage": config|None, "event": config|None}. Built once per
    bracket read because a public bracket page is the single hottest CS surface: resolving per
    match would be N+2 queries on a 32-match bracket.
    """
    match_ids = [m.h2h_match_id for m in matches]
    per_match = {
        c.h2h_match_id: c
        for c in CSRoomConfig.objects.filter(h2h_match_id__in=match_ids)
    } if match_ids else {}
    # Group-scoped rooms (owner 2026-08-13): one query covering every group in this read, so
    # "Group A plays 13 rounds, Group B plays 7" is one row each rather than a copy per match.
    group_ids = {m.group_id for m in matches if m.group_id}
    per_group = {
        c.group_id: c
        for c in CSRoomConfig.objects.filter(group_id__in=group_ids)
    } if group_ids else {}
    stage_or_event = list(CSRoomConfig.objects.filter(
        Q(stage_id=stage.stage_id) | Q(event_id=stage.event_id)))
    scopes = {
        "match": per_match,
        "group": per_group,
        "stage": next((c for c in stage_or_event if c.stage_id), None),
        "event": next((c for c in stage_or_event if c.event_id), None),
    }
    if not per_match and not per_group and not scopes["stage"] and not scopes["event"]:
        return None  # nothing configured: every match reports room = null
    return scopes


def _league_sit_outs(rounds, stage_team_ids, team_names):
    """Which team sits out each matchday of an odd-numbered round robin.

    WHY (owner 2026-08-12): with an odd number of teams the circle method leaves exactly one team
    unpaired per matchday, and the bracket never said who. A team looking at "Matchday 3" with no
    row of its own could not tell whether it was resting or whether a fixture had been forgotten.

    Returns {round_number: {"tournament_team_id", "team_name"}} for the matchdays that have one.
    """
    sit_outs = {}
    for entry in rounds:
        playing = set()
        for m in entry["matches"]:
            for side in ("team_a", "team_b"):
                if m.get(side):
                    playing.add(m[side]["tournament_team_id"])
        resting = [tid for tid in stage_team_ids if tid not in playing]
        # Exactly one resting team is the odd-count case worth naming. Two or more means the
        # matchday is simply incomplete, which is a different problem and not ours to narrate.
        if len(resting) == 1:
            tid = resting[0]
            sit_outs[entry["round"]] = {
                "tournament_team_id": tid,
                "team_name": team_names.get(tid, ""),
            }
    return sit_outs


def _bracket_payload(stage, group=None, *, show_credentials=False):
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
                     rounds_won, rounds_lost}, ...],
      "stage_competitors": [{tournament_team_id, team_name}, ...]  # the stage's OWN pool
    }
    """
    matches = list(
        head_to_head.bracket_matches(stage, group.group_id if group else None)
        # ghost_team alongside team (owner 2026-08-20): _team_payload/display_name below reads
        # whichever of the two is set, so both must be selected or a ghost slot fires an
        # extra query per match.
        .select_related("team_a__team", "team_a__ghost_team", "team_b__team", "team_b__ghost_team")
        # prefetch, not a join: every match now carries its per-player lines, and without this
        # the payload would fire one extra query per match on a bracket that can hold 30+.
        .prefetch_related("player_stats")
        .order_by("round_number", "position")
    )

    # Derive the engine format from the stored matches (authoritative even when fmt was
    # passed explicitly at generation), falling back to the stage_format mapping.
    #
    # League and Round Robin both store bracket="league" (they share the pairing engine), so the
    # stored rows alone cannot tell them apart and a round robin stage used to render titled
    # "League" (owner 2026-08-12, finding #9). When the stage_format itself names one of the two,
    # that wins: it is what the organizer picked and what every other surface calls the stage.
    # The group's own mode is authoritative; the stage format is only a legacy fallback for a
    # bracket that predates per-group modes (owner 2026-08-13).
    stage_fmt = (group.bracket_format if group and group.bracket_format
                 else head_to_head.FORMAT_FROM_STAGE.get(stage.stage_format))
    if any(m.bracket == "league" for m in matches):
        fmt = stage_fmt if stage_fmt in head_to_head.LEAGUE_FORMATS else "league"
    elif any(m.bracket == "losers" for m in matches):
        fmt = "double_elim"
    elif matches:
        fmt = "single_elim"
    else:
        fmt = stage_fmt

    # Room settings that could apply anywhere in this stage, loaded once (see the helper).
    room_scopes = _room_scopes_for_stage(stage, matches)

    # "third" holds the optional bronze match (single elimination only). It is its own list rather
    # than part of "winners" so the FE can draw it beside the final without it being mistaken for
    # another final round. Always present, usually empty.
    rounds = {"winners": [], "losers": [], "league": [], "third": []}
    by_round = {}
    for m in matches:
        by_round.setdefault((m.bracket, m.round_number), []).append(m)
    for (bracket, round_number) in sorted(by_round):
        rounds[bracket].append({
            "round": round_number,
            "matches": [
                _match_payload(m, room_scopes=room_scopes, show_credentials=show_credentials)
                for m in by_round[(bracket, round_number)]
            ],
        })

    competitors = _stage_competitor_payload(stage, group)

    # Who rests on each matchday of an odd-numbered round robin. Team names come from the bracket
    # itself (every team in a league plays somebody), falling back to the stage pool.
    team_names = {c["tournament_team_id"]: c["team_name"] for c in competitors}
    for m in matches:
        if m.team_a_id:
            team_names[m.team_a_id] = m.team_a.display_name
        if m.team_b_id:
            team_names[m.team_b_id] = m.team_b.display_name
    sit_outs = _league_sit_outs(rounds["league"], list(team_names), team_names) \
        if rounds["league"] else {}

    # The room configuration shown above the tree. For a GROUP bracket that is the group's own
    # (resolved group -> stage -> event); for the legacy stage-wide bracket it is the stage's.
    if group is not None:
        stage_room, stage_room_scope = cs_room.resolve_for_group(group)
    else:
        stage_room, stage_room_scope = cs_room.resolve_for_stage(stage)

    return {
        "stage_id": stage.stage_id,
        "stage_name": stage.stage_name,
        "stage_format": stage.stage_format,
        # ── which bracket this payload IS, and what else the stage holds (owner 2026-08-13) ──
        # group is null for the legacy stage-wide bracket. `stage_brackets` lists every bracket in
        # the stage so the frontend can draw one card per group without a second request - it is
        # one row per group, not the trees, so it stays cheap on the public page.
        "group_id": group.group_id if group else None,
        "group_name": group.group_name if group else None,
        "stage_brackets": [
            {
                "group_id": g.group_id,
                "group_name": g.group_name,
                "bracket_format": g.bracket_format,
                "third_place": g.bracket_third_place,
            }
            for g in head_to_head.bracket_groups(stage)
        ],
        "fmt": fmt,
        "generated": bool(matches),
        "rounds": rounds,
        # {round_number: {tournament_team_id, team_name}} - only for matchdays where exactly one
        # team is unpaired, i.e. an odd-sized round robin.
        "sit_outs": sit_outs,
        "standings": head_to_head.standings(stage, group.group_id if group else None) if matches else [],
        "stage_competitors": competitors,
        # League points, so the FE labels its table with the same scale the backend ranked by.
        "league_points": head_to_head.LEAGUE_POINTS,
        "room": {
            "source_scope": stage_room_scope,
            "summary": cs_room.summary(stage_room),
            "is_published": bool(stage_room and stage_room.is_published),
            # Whether a room ID exists AT ALL, even when it is being withheld. Lets the public
            # card say "the organizer has not opened the room yet" instead of either pretending
            # there is nothing or implying a room exists when none was ever entered.
            "has_room_credentials": bool(
                stage_room and (stage_room.room_id or stage_room.room_password)),
            "room_id": stage_room.room_id if (
                stage_room and (show_credentials or stage_room.is_published)) else "",
            "room_password": stage_room.room_password if (
                stage_room and (show_credentials or stage_room.is_published)) else "",
            "notes": stage_room.notes if (
                stage_room and (show_credentials or stage_room.is_published)) else "",
        } if stage_room else None,
    }


def _stage_competitor_payload(stage, group=None):
    """The stage's OWN competitor pool (StageCompetitor rows), in the order they were added.

    WHY THIS EXISTS (owner 2026-08-12): the generate-bracket dialog used to offer every team
    registered to the EVENT, which is wrong for any stage after the first. Two existing flows
    already say exactly who belongs in THIS stage, and both write StageCompetitor rows:
      • "Add Teams to Stage" on the admin event page (views.add_teams_to_stage), and
      • advancing qualifiers out of a previous stage (views.advance_group_competitors_to_next_stage
        and advancement_routing.advance_stage_by_rules).
    So a Clash Squad finals stage fed by a Battle Royale group stage already knows its 8 qualifiers;
    the bracket just never read them. Serving them here lets the FE seed the dialog from this list
    (falling back to the full registration list when a stage has no pool yet, e.g. a one-stage
    event where nobody ran "Add Teams to Stage").

    Order: StageCompetitor id, i.e. the order competitors entered the stage. For an advanced
    stage that is placement order from the previous stage, which is the seed order an organizer
    would pick by hand anyway. Solo (player) competitors are skipped: an H2H bracket is between
    TournamentTeam rows.
    WHEN A GROUP IS GIVEN (owner 2026-08-13) its OWN membership comes first: the
    StageGroupCompetitor rows written by "Add Teams to Group". A stage split into groups seeds each
    bracket from the teams actually in that group, which is the whole point of splitting it. The
    stage pool remains the fallback, so a group nobody has filled in yet still offers something
    sensible rather than an empty dialog.

    CONSUMED BY: components/h2h-bracket.tsx (the "Generate bracket" seed list).
    """
    if group is not None:
        group_rows = (
            StageGroupCompetitor.objects
            .filter(stage_group=group, tournament_team__isnull=False,
                    # Confirmed participants only (owner backlog item 11, 2026-08-14): a team that
                    # withdrew, was disqualified or sits on the waitlist stays in the pool rows but
                    # must not be offered in the draw. generate_h2h_bracket refuses them anyway, so
                    # showing them here would only produce a 400 the organizer cannot act on.
                    tournament_team__status="active", tournament_team__is_waitlisted=False)
            .select_related("tournament_team__team", "tournament_team__ghost_team")
            .order_by("id")
        )
        payload = [
            {
                "tournament_team_id": gc.tournament_team.tournament_team_id,
                "team_name": gc.tournament_team.display_name,
            }
            for gc in group_rows
        ]
        if payload:
            return payload

    rows = (
        StageCompetitor.objects
        .filter(stage=stage, tournament_team__isnull=False,
                # Same confirmed-participants rule as the group branch above.
                tournament_team__status="active", tournament_team__is_waitlisted=False)
        .select_related("tournament_team__team", "tournament_team__ghost_team")
        .order_by("id")
    )
    return [
        {
            "tournament_team_id": sc.tournament_team.tournament_team_id,
            "team_name": sc.tournament_team.display_name,
        }
        for sc in rows
    ]


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

    # ── which bracket, and in which mode? (owner 2026-08-13) ──────────────────────────────────
    # These two answers depend on each other, so they are resolved together:
    #   • an explicit group_id names one of a split stage's brackets, and THAT GROUP'S mode wins,
    #     because it is what the organizer picked and what the bracket is read back as;
    #   • no group_id means the simple one-bracket stage - ensure_bracket_group hands back its
    #     single group, creating it the first time, so the data has one shape either way. It
    #     refuses when the stage is split, since "the stage's bracket" is ambiguous there.
    # The mode itself: the group's, else the body's, else the legacy stage_format mapping. A
    # generation-3 "cs" stage carries no mode of its own at all - that is the point of item 21.
    group_id = request.data.get("group_id")
    named_group = (get_object_or_404(StageGroups, stage=stage, group_id=group_id)
                   if group_id else None)

    # Ambiguity beats a mode complaint: on a stage that has been split, "generate the bracket"
    # without saying which group is the mistake worth reporting, and reporting a missing mode
    # instead would send the caller off fixing the wrong thing.
    if named_group is None and len(list(head_to_head.bracket_groups(stage)[:2])) > 1:
        return Response(
            {"message": "This stage is split into groups, so say which group's bracket you are "
                        "generating."},
            status=400)

    fmt = (named_group.bracket_format if named_group and named_group.bracket_format else None) \
        or request.data.get("fmt") \
        or head_to_head.FORMAT_FROM_STAGE.get(stage.stage_format)
    if not fmt:
        return Response(
            {"message": "Pick a mode for this bracket (single_elim, double_elim, league or "
                        "round_robin_h2h)."},
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
    # Only CONFIRMED participants may be drawn into a bracket (owner backlog item 11, 2026-08-14).
    # Everywhere else in the system a confirmed team is status="active" AND not waitlisted - see
    # seeding_management._missing_stage_competitor_entries, which builds every stage pool that way.
    # This endpoint checked only that the id belonged to the event, so a withdrawn, disqualified,
    # "left" or still-waitlisted team could be seeded into a real match and then never turn up.
    # Refused with the names rather than filtered out silently: an organizer who picked a team on
    # purpose needs to know WHY it is not in the draw, and a bracket generated with fewer teams than
    # asked for is a different bracket (byes move).
    unconfirmed = list(
        TournamentTeam.objects
        .filter(event=event, tournament_team_id__in=team_ids)
        .exclude(status="active", is_waitlisted=False)
        .select_related("team", "ghost_team")
    )
    if unconfirmed:
        names = ", ".join(f"{t.display_name} ({t.status}"
                          f"{', waitlisted' if t.is_waitlisted else ''})" for t in unconfirmed)
        return Response({"message": f"These teams are not confirmed participants and cannot be "
                                    f"seeded: {names}."}, status=400)

    # Optional bronze match, single elimination only (owner 2026-08-12). Anything truthy in the
    # body turns it on; the engine ignores it for the other formats and for a 2-team bracket,
    # which has no semifinals to feed it.
    third_place = bool(request.data.get("third_place"))

    try:
        if named_group is not None:
            group = named_group
            # Persist the mode on the group the first time it is generated, and keep the bronze
            # flag in step with what was just asked for.
            if group.bracket_format != fmt or group.bracket_third_place != third_place:
                group.bracket_format = fmt
                group.bracket_third_place = third_place
                group.save(update_fields=["bracket_format", "bracket_third_place"])
        else:
            group = head_to_head.ensure_bracket_group(stage, fmt, third_place=third_place)
    except head_to_head.BracketError as e:
        return Response({"message": str(e)}, status=400)

    # Regeneration guard: only while no REAL result has been entered IN THIS BRACKET. A bye is
    # completed with one empty slot, so requiring both teams filters byes out. Scoped to the group
    # so a played Group A never blocks Group B from being drawn.
    if head_to_head.bracket_matches(stage, group.group_id).filter(
            status="completed",
            team_a__isnull=False, team_b__isnull=False).exists():
        return Response({"message": "Results have already been entered for this bracket; "
                                    "it can no longer be regenerated."}, status=400)

    # Replace any previous (result-free) bracket atomically. Scoped to this group: regenerating
    # Group A must not wipe Group B.
    with transaction.atomic():
        head_to_head.bracket_matches(stage, group.group_id).delete()
        try:
            head_to_head.generate_bracket(
                stage, team_ids, fmt, third_place=third_place, group=group)
        except head_to_head.BracketError as e:
            # atomic() rolls the delete back too, so a failed generate leaves the old
            # bracket untouched.
            transaction.set_rollback(True)
            return Response({"message": str(e)}, status=400)

    # Tell every team the bracket exists and who they open against (owner 2026-08-12: nothing in
    # the Clash Squad path notified a player of anything). Best-effort inside the helper, so a
    # notification failure can never undo a bracket that generated fine.
    h2h_notifications.notify_bracket_generated(stage)

    return Response({"message": "Bracket generated.",
                     "bracket": _bracket_payload(stage, group, show_credentials=True)}, status=201)


@api_view(["GET"])
def get_h2h_bracket(request, stage_id):
    """GET events/stages/<stage_id>/bracket/ - the full bracket tree + standings.

    PUBLIC (no auth): this is the spectator bracket page, the H2H counterpart of the
    public event leaderboards. Response shape: see _bracket_payload's docstring.
    Consumed by the FE bracket renderer (public event page + the admin bracket tab,
    which layers its controls over the same read).

    ?group_id=<id> reads ONE group's bracket (owner 2026-08-13). Omitted, the response describes
    the stage's single bracket - or, when the stage has been split into groups, its FIRST one,
    with every bracket listed in `stage_brackets` so the caller can fetch the rest. That default
    keeps every existing caller working without knowing groups exist.
    """
    stage = get_object_or_404(Stages.objects.select_related("event"), stage_id=stage_id)

    group = None
    group_id = request.GET.get("group_id")
    if group_id:
        group = get_object_or_404(StageGroups, stage=stage, group_id=group_id)
    else:
        # No explicit group: the stage's own bracket if it still has one (legacy rows carry a NULL
        # group), else its first group bracket.
        if not head_to_head.bracket_matches(stage, None).exists():
            group = head_to_head.bracket_groups(stage).first()

    # Anonymous reads are the norm here, but a manager viewing the same page must see the room ID
    # and password even before they are published - that is the whole point of an unpublished
    # config. So we resolve the token when one was sent, and never require it.
    user = _optional_user(request)
    can_manage = bool(user) and (
        _is_event_admin(user) or org_can_event(user, "can_edit_events", stage.event))
    return Response(_bracket_payload(stage, group, show_credentials=can_manage), status=200)


@api_view(["POST"])
def report_h2h_match_result(request, match_id):
    """POST events/h2h-matches/<match_id>/result/ - record a Clash Squad set result.

    Request : {"score_a": int, "score_b": int,           (round wins; no ties in elimination)
               "player_stats": [                          (optional, owner 2026-08-12)
                   {"player_id", "tournament_team_id", "kills", "damage", "assists", "played"},
                   ...]}
              player_stats REPLACES this set's lines wholesale; omit the key to leave them alone,
              send [] to clear them. Every player must be on that team's roster for the event.
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

    # ── the set was never played: forfeit / walkover / disqualification (owner 2026-08-12) ──
    # Sending an outcome instead of a scoreline records WHO advances and WHY, without inventing
    # a scoreline that then feeds the round-difference tiebreak as if a real set had happened.
    outcome = request.data.get("outcome")
    if outcome and outcome != "normal":
        try:
            bracket_complete = head_to_head.award_walkover(
                match,
                request.data.get("winner_id"),
                result_type=outcome,
                note=request.data.get("result_note") or "",
                acting_user=user,
            )
        except head_to_head.BracketError as e:
            return Response({"message": str(e)}, status=400)
        match.refresh_from_db()
        h2h_notifications.notify_match_result(match)
        return Response({
            "message": "Result recorded.",
            "match": _match_payload(match),
            "bracket_complete": bracket_complete,
        }, status=200)

    score_a = request.data.get("score_a")
    score_b = request.data.get("score_b")
    if score_a is None or score_b is None:
        return Response({"message": "score_a and score_b are required."}, status=400)

    # Optional per-player lines for this set (owner 2026-08-12). Omitting the key leaves any
    # existing lines untouched; sending [] clears them. Written inside report_result's transaction,
    # so a refused player line rolls the score back with it.
    player_stats = request.data.get("player_stats")

    try:
        bracket_complete = head_to_head.report_result(
            match, score_a, score_b, acting_user=user, player_stats=player_stats)
    except head_to_head.BracketError as e:
        return Response({"message": str(e)}, status=400)

    # Re-fetch so the echoed match carries the propagation-fresh team objects.
    match.refresh_from_db()
    # Both teams hear the result, and the winner hears who they play next.
    h2h_notifications.notify_match_result(match)
    return Response({
        "message": "Result recorded.",
        "match": _match_payload(match),
        "bracket_complete": bracket_complete,
    }, status=200)


@api_view(["PATCH"])
def update_h2h_match(request, match_id):
    """PATCH events/h2h-matches/<match_id>/ - set a match's kick-off time or mark it live.

    WHY (owner 2026-08-12, findings #6 and #20): HeadToHeadMatch has carried scheduled_date,
    scheduled_time and a "live" status since it was written, and NOTHING ever set them. A Clash
    Squad match had no kick-off time anywhere on the site and could not be marked in progress,
    so a spectator page could not tell a finished bracket from one about to start.

    Request : {"scheduled_date": "YYYY-MM-DD" | null,
               "scheduled_time": "HH:MM" | null,
               "status": "pending" | "live"}     (completed is set by reporting a result, never here)
              Every field optional; omitted fields are left alone, null clears a date/time.
    Auth    : AFC event admin OR org_can_event("can_edit_events") - scheduling is event editing,
              the same gate the room settings use, not the results permission.
    Response: 200 {"message", "match": <match object>}
    Consumed by: components/h2h-bracket.tsx (the match card's "Schedule" control).
    """
    user, err = _auth_user(request)
    if err:
        return err

    match = get_object_or_404(
        HeadToHeadMatch.objects.select_related("stage__event", "team_a__team", "team_b__team"),
        h2h_match_id=match_id)
    if not _is_event_admin(user) and not org_can_event(user, "can_edit_events", match.stage.event):
        return Response({"message": "You do not have permission to schedule this match."},
                        status=403)

    fields = []
    if "scheduled_date" in request.data:
        raw = request.data.get("scheduled_date")
        try:
            match.scheduled_date = datetime.date.fromisoformat(raw) if raw else None
        except (TypeError, ValueError):
            return Response({"message": "scheduled_date must look like 2026-08-20."}, status=400)
        fields.append("scheduled_date")
    if "scheduled_time" in request.data:
        raw = request.data.get("scheduled_time")
        try:
            # Accept both "18:30" and "18:30:00" - a browser time input sends the short form.
            match.scheduled_time = datetime.time.fromisoformat(raw) if raw else None
        except (TypeError, ValueError):
            return Response({"message": "scheduled_time must look like 18:30."}, status=400)
        fields.append("scheduled_time")
    if "status" in request.data:
        status_value = request.data.get("status")
        if status_value not in ("pending", "live"):
            # "completed" is a consequence of a result, never a switch somebody flips: allowing it
            # here would leave a match marked finished with no winner and no advancement.
            return Response(
                {"message": "status can only be set to pending or live. Enter a result to "
                            "complete a match."}, status=400)
        if match.status == "completed":
            return Response(
                {"message": "This match already has a result. Change the result instead."},
                status=400)
        match.status = status_value
        fields.append("status")

    if not fields:
        return Response({"message": "Nothing to update."}, status=400)

    match.save(update_fields=fields + ["updated_at"])

    # Tell the two teams. Going live is its own message ("join the room now"); a new or changed
    # kick-off time is the other. Silent when only, say, the date was cleared.
    if "status" in fields and match.status == "live":
        h2h_notifications.notify_match_scheduled(match, went_live=True)
    elif "scheduled_date" in fields or "scheduled_time" in fields:
        h2h_notifications.notify_match_scheduled(match)

    return Response({"message": "Match updated.", "match": _match_payload(match)}, status=200)


@api_view(["GET"])
def get_h2h_match_rosters(request, match_id):
    """GET events/h2h-matches/<match_id>/rosters/ - who can be given a stat line in this set.

    WHY (owner 2026-08-12: "when entering results you should be able to enter for each player
    also"): the result dialog needs the two teams' rosters to draw a row per player. The bracket
    payload deliberately does not carry them - it is a public spectator read fetched on every
    page load, and rosters would bloat it for the 99% of views that never enter a result - so
    this is a separate call the dialog makes when it opens.

    Auth    : AFC event admin OR org_can_event("can_upload_results"), the same gate as entering
              the result itself. Rosters name real people, so this is not a public read.
    Response: 200 {"teams": [{"tournament_team_id", "team_name",
                              "players": [{"player_id", "username", "in_game_name",
                                           "in_game_role"}, ...]}, ...]}
              Ordered team_a first, then team_b. A side with no team yet is omitted.
    Consumed by components/h2h-bracket.tsx (the per-player section of "Enter result").
    """
    user, err = _auth_user(request)
    if err:
        return err

    match = get_object_or_404(
        HeadToHeadMatch.objects.select_related(
            "stage__event", "team_a__team", "team_a__ghost_team",
            "team_b__team", "team_b__ghost_team"),
        h2h_match_id=match_id)
    event = match.stage.event

    if not _is_event_admin(user) and not org_can_event(user, "can_upload_results", event):
        return Response({"message": "You do not have permission to enter results for this event."},
                        status=403)

    teams = []
    for tt in (match.team_a, match.team_b):
        if tt is None:
            continue
        members = (
            TournamentTeamMember.objects
            .filter(tournament_team=tt, status__in=("active", "approved"))
            .select_related("user")
            .order_by("user__username")
        )
        teams.append({
            "tournament_team_id": tt.tournament_team_id,
            "team_name": tt.display_name,
            "players": [
                {
                    "player_id": m.user_id,
                    "username": m.user.username,
                    # in_game_name is what an organizer reads off the end-of-set screen, so show
                    # it next to the username rather than making them map one to the other.
                    "in_game_name": getattr(m.user, "in_game_name", "") or "",
                    "in_game_role": m.in_game_role or "",
                }
                for m in members
            ],
        })

    return Response({"teams": teams}, status=200)
