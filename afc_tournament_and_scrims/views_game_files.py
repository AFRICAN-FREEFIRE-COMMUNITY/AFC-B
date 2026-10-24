# ── GAME FILES UPLOAD: the endpoints (owner 2026-10-24, inbox #386 #391 #394 #396) ──────────────────────
# The reading, joining, pairing and counting live in game_files.py; this module is the door. Consumed by
# the website's Game files box on the Upload Results tab (components/leaderboards/GameFilesUpload.tsx),
# for admins and the event's organizers (_broadcast_gate, the same gate as the old Debugger log panel).
#
#   POST events/<event_id>/game-files/read/          multipart `file` (plain, or gzip from the browser)
#        -> {file_id, name, kind, games, start, last, match_id?, teams?}
#   POST events/<event_id>/game-files/games/         {file_ids: [...]}
#        -> {games: [...], slots: [...]}  (each game: key, when, last, sources, gaps, has_result,
#           result_file, players, on_roster, checks, state, match_id (suggested), diffs, why, kills, teams)
#   POST events/<event_id>/game-files/result-text/   {file_ids, key, keep_debugger: [uid], placements: {team: rank}}
#        -> {text, filename}: what the browser then uploads through upload_team_match_result
#   POST events/<event_id>/game-files/debugger/      {file_ids, key, match_id}
#        -> {updated_rows, kill_events}: knocks, deaths, revives, headshots, survival + the kill feed
#
# Files are parsed once and kept in the cache for game_files.BATCH_TTL under an opaque id; nothing of
# the file itself is stored. An id that has expired is refused with `game_files_expired`.

import json

from django.core.cache import cache
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from . import game_files as gf
from .views import _broadcast_gate

MAX_FILES = 60   # an evening is a handful of debugger files and one result file per game


def _key(event, file_id):
    return f"gamefiles:{event.pk}:{file_id}"


def _refuse(message, code, status=400):
    return Response({"message": message, "code": code}, status=status)


def _load(event, raw_ids):
    """The cached files of this upload, in the order given. (files, None) or (None, refusal)."""
    if isinstance(raw_ids, str):
        try:
            raw_ids = json.loads(raw_ids)
        except ValueError:
            raw_ids = None
    if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > MAX_FILES:
        return None, _refuse("Send the ids of the files you added.", "game_files_ids_required")
    files = []
    for fid in raw_ids:
        entry = cache.get(_key(event, str(fid))) if isinstance(fid, str) and fid.isalnum() else None
        if entry is None:
            return None, _refuse("These files were read too long ago. Add them again.", "game_files_expired", 410)
        files.append(entry)
    return files, None


def _item(event, request):
    """The one game a result-text or debugger call is about. (items, item, None) or (.., .., refusal)."""
    files, err = _load(event, request.data.get("file_ids"))
    if err:
        return None, None, err
    items = gf.build_games(files)
    key = str(request.data.get("key") or "")
    item = next((i for i in items if i["key"] == key), None)
    if item is None:
        return items, None, _refuse("That game is not in these files.", "game_not_in_files", 404)
    return items, item, None


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def game_files_read(request, event_id):
    """Read ONE debugger or result file into the cache. The browser sends each file on its own."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    up = request.FILES.get("file")
    if not up:
        return _refuse("Attach a debugger or result file.", "attach_game_file")
    if up.size > gf.MAX_UPLOAD_BYTES:
        return _refuse("That file is too large to send. Add it again from the box, it compresses it first.",
                       "game_file_too_large")
    name = str(request.data.get("name") or up.name or "")[:200]
    try:
        text = gf.unpack_upload(up.read())
    except (ValueError, OSError, EOFError):
        return _refuse("That file is too large or damaged.", "game_file_unreadable")
    kind = gf.looks_like(name, text)
    if kind is None:
        return _refuse("That is not a debugger file or a match result file.", "not_a_game_file")
    entry = {"id": gf.new_file_id(), "name": name, "kind": kind}
    out = {"file_id": entry["id"], "name": name, "kind": kind}
    if kind == "debugger":
        entry["pieces"] = gf.read_debugger(text)
        played = [p for p in entry["pieces"] if any(ev[1] in ("D", "K") for ev in p["events"])]
        out.update(games=len(played), start=entry["pieces"][0]["start"] if entry["pieces"] else None,
                   last=entry["pieces"][-1]["last"] if entry["pieces"] else None,
                   ended=sum(1 for p in entry["pieces"] if p["match_id"]))
    else:
        entry["result"] = gf.read_result(text, name)
        if not entry["result"]["teams"]:
            return _refuse("No teams were found in that result file.", "no_team_data_parsed")
        out.update(games=1, match_id=entry["result"]["match_id"], start=entry["result"]["played_at"],
                   teams=len(entry["result"]["teams"]))
    cache.set(_key(event, entry["id"]), entry, gf.BATCH_TTL)
    return Response(out)


@api_view(["POST"])
@parser_classes([JSONParser])
def game_files_games(request, event_id):
    """Every game in the files added so far, with a suggested match and the checks."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    files, err = _load(event, request.data.get("file_ids"))
    if err:
        return err
    items = gf.build_games(files)
    slots = gf.suggest(event, items)
    games = []
    for it in items:
        d, r = it["debugger"], it["result"]
        diffs, kills = gf.compare(it)
        why = None
        if diffs:
            why = "partial" if (d["gaps"] or kills["debugger_kills"] < kills["result_kills"]) else "counted_differently"
        games.append({
            "key": it["key"], "when": it["when"], "last": d["last"] if d else None,
            "debugger_files": [s["file"] for s in d["sources"]] if d else [],
            "gaps": d["gaps"] if d else [], "ended": bool(d and d["match_id"]),
            "has_result": bool(r), "result_file": r["file_name"] if r else None,
            "players": it["players"], "on_roster": it["on_roster"], "checks": it["checks"],
            "state": it["state"], "match_id": it["match_id"], "saved_before": it["saved_before"],
            "diffs": diffs, "why": why, "kills": kills,
            "teams": gf.game_teams(event, it) if (d and not r) else [],
        })
    return Response({"games": games, "slots": slots})


@api_view(["POST"])
@parser_classes([JSONParser])
def game_files_result_text(request, event_id):
    """The result text to upload for one game: the result file with the kills the organizer chose to
    take from the debugger, or (no result file) written from the debugger with their placements."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    _, item, err = _item(event, request)
    if err:
        return err
    keep = request.data.get("keep_debugger") or []
    placements = request.data.get("placements") or {}
    if not isinstance(keep, list) or not isinstance(placements, dict):
        return _refuse("keep_debugger must be a list and placements an object.", "game_files_bad_choices")
    names = {t["team"]: t["name"] for t in gf.game_teams(event, item)} if not item["result"] else None
    text, code = gf.game_result_text(item, keep, placements, names)
    if code:
        messages = {"placement_missing": "Give every team its placement.",
                    "placements_not_one_each": "Each placement can be used once: 1 for the winner, then 2, 3 and so on.",
                    "no_files_for_game": "There is nothing to upload for that game."}
        return _refuse(messages[code], code)
    name = item["result"]["file_name"] if item["result"] else f"MatchResult_from_debugger_{item['key']}.log"
    return Response({"text": text, "filename": name})


@api_view(["POST"])
@parser_classes([JSONParser])
def game_files_debugger(request, event_id):
    """Fill one match with what only the debugger knows: knocks, deaths, revives, headshots, survival
    (by player UID on the rows the result upload wrote) and the kill feed. Run after the result upload."""
    from .debugger_ingest import apply_rich_stats
    from .models import Match
    from . import kill_feed

    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    _, item, err = _item(event, request)
    if err:
        return err
    d = item["debugger"]
    if not d:
        return Response({"updated_rows": 0, "kill_events": 0})
    try:
        match_id = int(request.data.get("match_id"))
    except (TypeError, ValueError):
        return _refuse("match_id required.", "match_required")
    match = Match.objects.filter(match_id=match_id, group__stage__event=event).first()
    if match is None:
        return _refuse("That match is not on this event.", "match_not_on_event", 404)
    stats, feed = gf.game_stats(d["players"], d["events"])
    updated = apply_rich_stats(match, {s["uid"]: s for s in stats.values()})
    stored = kill_feed.store_events(match, event, feed, "backfill")
    return Response({"updated_rows": updated, "kill_events": stored})
