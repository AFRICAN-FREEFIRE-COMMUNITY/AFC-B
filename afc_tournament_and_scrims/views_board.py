"""The leaderboard section's Stats view and its export (owner, 11 Nov 2026, inbox #538 #539 #540).

Owner's words: "damage still doesnt show for each individual player per map and per roup, it only shows
for total"; "i also want to be able to export the leaderboard for not only a grop, but also each map or
specific maps in a group that were played"; "all other data should be visible in that leaderboard section".

ENDPOINTS
---------
GET events/<event_id>/board/?group_id=<id>[&match_ids=<id,id>]
    Without group_id: only the event's groups and maps (the page's first call), scope None.
    Auth: Bearer; AFC event admin, or an organizer who can edit the event (views._broadcast_gate, the gate
    every leaderboard tool on these pages uses). A group_id that is not this event's -> 404 coded.
    Scope: every scored map of the group, or only the listed maps (each must be this group's).
    Answer: {
      "event": {"event_id", "event_name"},
      "groups": [{"group_id", "group_name", "stage_id", "stage_name",
                  "maps": [{"match_id", "match_number", "map", "scored"}]}],   every group of the event
      "scope": {"group_id", "match_ids", "all_maps"},
      "teams": board.team_board rows over the maps in scope,
      "players": board.player_board rows over the maps in scope (registered, ghost and flagged players),
      "live": null, or (whole group only) {"map_number", "teams_alive", "unmatched", "standings":
              board.live_group_table rows, "top_killers": the site's top killers with the map in progress}
    }
GET events/<event_id>/board/export/?group_id=&match_ids=&kind=teams|players
    The same scope and numbers as a CSV download (UTF-8 with a BOM so Excel reads names right).

CONSUMED BY: AFC_Frontend components/leaderboards/BoardStatsPanel.tsx, mounted on the admin leaderboard
page (app/(a)/a/leaderboards/[id]/edit) and the organizer one (app/(organizer)/organizer/events/[slug]/
leaderboard), tab "Stats".
CONNECTS TO: board.py (team_board, player_board, live_group_table, live_top_killers), views_mvp
(compute_top_killers for the live top killers), views._broadcast_gate.
"""

import csv
import io

from django.http import HttpResponse
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .board import live_group_table, live_top_killers, player_board, session_status, team_board
from .models import Match, StageGroups, TournamentTeamMatchStats
from .views import _broadcast_gate


class BoardQuery(serializers.Serializer):
    """The query of both endpoints: which group, which maps (comma separated ids), which list to download."""
    group_id = serializers.IntegerField(min_value=1, required=False)
    match_ids = serializers.RegexField(r"^\d{1,10}(,\d{1,10}){0,99}$", max_length=1100, required=False,
                                       allow_blank=True)
    kind = serializers.ChoiceField(choices=("teams", "players"), required=False)


def _query(request):
    """(validated query, None) or (None, coded 400). An unknown kind or a malformed map list is refused here."""
    q = BoardQuery(data=request.query_params)
    if q.is_valid():
        return q.validated_data, None
    if "kind" in q.errors:
        return None, Response({"message": "Choose teams or players.", "code": "board_export_kind"}, status=400)
    if "match_ids" in q.errors:
        return None, Response({"message": "Maps must be chosen from this group.", "code": "board_maps_invalid"},
                              status=400)
    return None, Response({"message": "Choose a group.", "code": "board_group_required"}, status=400)


def _scope(event, request):
    """(group, match_ids, all_maps) for the request, or (None, None, error Response)."""
    q, err = _query(request)
    if err:
        return None, None, err
    if not q.get("group_id"):
        return None, None, Response({"message": "Choose a group.", "code": "board_group_required"}, status=400)
    group = (StageGroups.objects.select_related("stage")
             .filter(group_id=q["group_id"], stage__event=event).first())
    if group is None:
        return None, None, Response({"message": "That group is not part of this event.",
                                     "code": "board_group_not_found"}, status=404)
    scored = list(Match.objects.filter(group=group, team_stats__isnull=False)
                  .distinct().order_by("match_number").values_list("match_id", flat=True))
    wanted = [s for s in (q.get("match_ids") or "").split(",") if s]
    if not wanted:
        return group, (scored, True), None
    ids = [int(s) for s in wanted]
    own = set(Match.objects.filter(group=group, match_id__in=ids).values_list("match_id", flat=True))
    if len(own) != len(set(ids)):
        return None, None, Response({"message": "Maps must be chosen from this group.",
                                     "code": "board_maps_invalid"}, status=400)
    return group, (sorted(own), set(own) >= set(scored)), None


def _groups(event):
    scored = set(TournamentTeamMatchStats.objects.filter(match__group__stage__event=event)
                 .values_list("match_id", flat=True).distinct())
    out = []
    for g in (StageGroups.objects.select_related("stage").filter(stage__event=event)
              .order_by("stage__stage_order", "stage_id", "group_order", "group_id")):
        maps = [{"match_id": m.match_id, "match_number": m.match_number, "map": m.match_map or "",
                 "scored": m.match_id in scored}
                for m in Match.objects.filter(group=g).order_by("match_number")]
        out.append({"group_id": g.group_id, "group_name": g.group_name, "stage_id": g.stage_id,
                    "stage_name": g.stage.stage_name, "maps": maps})
    return out


@api_view(["GET"])
def event_board(request, event_id):
    """See the module docstring."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    if not str(request.query_params.get("group_id") or "").strip():
        # No group yet: the page's first call, to list the groups and their maps to choose from.
        return Response({"event": {"event_id": event.event_id, "event_name": event.event_name},
                         "groups": _groups(event), "scope": None, "teams": [], "players": [], "live": None},
                        status=200)
    group, scope, err = _scope(event, request)
    if err:
        return err
    match_ids, all_maps = scope
    live = None
    if all_maps:
        rows, meta = live_group_table(group)
        if meta is not None:
            from .views import _capture_live_standings
            # The site's top killers for the group, shaped as AFC LIVE gets them, + the map in progress.
            ctx = _capture_live_standings(event, group, request)
            live = dict(meta, standings=rows,
                        top_killers=(ctx.get("live") or {}).get("top_killers")
                        or live_top_killers(ctx.get("top_killers") or [], rows))
    return Response({
        "event": {"event_id": event.event_id, "event_name": event.event_name},
        "groups": _groups(event),
        "scope": {"group_id": group.group_id, "match_ids": match_ids, "all_maps": all_maps},
        "teams": team_board(event, match_ids),
        "players": player_board(event, match_ids),
        "live": live,
        "session": session_status(group),
    }, status=200)


class SessionBody(serializers.Serializer):
    """POST events/<event_id>/groups/<group_id>/session/ body (R69)."""
    done = serializers.BooleanField()


@api_view(["POST"])
def group_session(request, event_id, group_id):
    """POST events/<event_id>/groups/<group_id>/session/ - mark or unmark "that was the last map for now"
    (owner 4 Nov 2026, inbox #501).

    Body: {"done": true|false}. Answers board.session_status for the group:
    {"status": "playing"|"done_for_now"|"finished"|"waiting", "maps_played", "maps_total", "done_at"}.
    Auth: the leaderboard Stats screen's gate (views._broadcast_gate: an AFC event admin, or an organizer who can
    edit the event). The group must belong to the event in the address (404 otherwise).
    Marking stores how many maps hold a result now, so the mark lapses by itself once a later map is played.
    Refused while a map is in progress (a live board in the last 15 s): the map being played cannot be the last
    one already played (code session_map_in_progress).
    Consumed by components/leaderboards/BoardStatsPanel.tsx ("Last map for now"). Read back by AFC Capture and
    AFC LIVE through capture/context `session`, and by the Stats screen through events/<id>/board/ `session`.
    """
    from django.utils import timezone

    from .board import live_state, scored_map_count

    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    group = StageGroups.objects.select_related("stage").filter(group_id=group_id, stage__event=event).first()
    if group is None:
        return Response({"message": "That group is not part of this event.", "code": "board_group_not_found"},
                        status=404)
    body = SessionBody(data=request.data)
    if not body.is_valid():
        return Response({"message": "Say whether the last map has been played.", "code": "session_done_required"},
                        status=400)
    if body.validated_data["done"]:
        if live_state(group) is not None:
            return Response({"message": "A map is being played right now. Mark it once it has ended.",
                             "code": "session_map_in_progress"}, status=409)
        group.session_done_at = timezone.now()
        group.session_done_maps = scored_map_count(group.group_id)
    else:
        group.session_done_at = None
        group.session_done_maps = None
    group.save(update_fields=["session_done_at", "session_done_maps"])
    return Response(session_status(group), status=200)


TEAM_COLUMNS = (
    ("rank", "Rank"), ("team_name", "Team"), ("team_country", "Country"), ("games_played", "Maps"),
    ("booyahs", "Booyahs"), ("kills", "Kills"), ("placement_points", "Placement points"),
    ("kill_points", "Kill points"), ("bonus", "Bonus"), ("penalty", "Penalty"), ("total", "Total"),
    ("damage", "Damage"), ("assists", "Assists"), ("knockdowns", "Knockdowns"), ("knocked", "Knocked"),
    ("headshots", "Headshots"), ("deaths", "Deaths"), ("revives_received", "Revives"),
    ("grenades_used", "Grenades"), ("grenade_kills", "Grenade kills"), ("gloowall_used", "Gloo walls"),
    ("medkit_used", "Medkits"), ("survival_seconds", "Survival (s)"), ("most_used_weapon", "Most used gun"),
)
PLAYER_COLUMNS = (
    ("rank", "Rank"), ("in_game_name", "Player"), ("uid", "UID"), ("team_name", "Team"),
    ("registered", "On roster"), ("matches", "Maps"), ("kills", "Kills"), ("damage", "Damage"),
    ("assists", "Assists"), ("knockdowns", "Knockdowns"), ("knocked", "Knocked"), ("headshots", "Headshots"),
    ("deaths", "Deaths"), ("kdr", "K/D"), ("revives_received", "Revives"), ("grenades_used", "Grenades"),
    ("grenade_kills", "Grenade kills"), ("gloowall_used", "Gloo walls"), ("medkit_used", "Medkits"),
    ("survival_seconds", "Survival (s)"), ("most_used_weapon", "Most used gun"),
)


def _csv_cell(value):
    """A cell a spreadsheet will not run as a formula (a team called '=1+1' stays text)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") and not text.lstrip("-").replace(".", "").isdigit() \
        else text


@api_view(["GET"])
def event_board_export(request, event_id):
    """See the module docstring. kind=teams (default) or players."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    group, scope, err = _scope(event, request)
    if err:
        return err
    match_ids, all_maps = scope
    kind = request.query_params.get("kind") or "teams"      # validated by BoardQuery in _scope
    rows = team_board(event, match_ids) if kind == "teams" else player_board(event, match_ids)
    columns = TEAM_COLUMNS if kind == "teams" else PLAYER_COLUMNS
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _k, label in columns])
    for r in rows:
        writer.writerow([_csv_cell(r.get(k)) for k, _label in columns])
    numbers = {m.match_id: m.match_number for m in Match.objects.filter(match_id__in=match_ids)}
    which = "all-maps" if all_maps else "maps-" + "-".join(str(numbers[m]) for m in match_ids)
    name = "{}-{}-{}-{}.csv".format(event.slug or event.event_id, group.group_name, which, kind)
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-")
    resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="{}"'.format(safe)
    # The website (another origin) downloads it with fetch, which can read this header only when exposed.
    resp["Access-Control-Expose-Headers"] = "Content-Disposition"
    return resp
