# ── GAME FILES: every debugger and result file of an evening, read as games (owner 2026-10-24) ─────────
# Owner, inbox #386: "An organizer or admin should be able to upload both debugger (more than one debugger
# file, because the client can be shut down mid-game and restarted) and match result files simultaneously
# at once and get them fixed and even compare them to get the best data and ensure the correct games were
# picked." Mockup mockups/game-files-upload/ approved (#394) with one note: kills, bleed-outs included,
# are counted the way the game counts them, so the two files agree.
#
# WHAT A FILE HOLDS (measured on 6 debugger + 20 result files of 3, 19 and 20 Oct 2026):
#   debugger-*.log   one observer client session, several games. A game opens at "EventTypeEnterGame"
#                    and its match id is only printed at the END ("matchend matchid = <id>"). A client
#                    restart leaves a piece with no end; the next file opens the same game again.
#   MatchResult_<matchid>_<YYYY-MM-DD-HH-MM-SS>.log   the official score of ONE game, written at its end.
#
# THE KILL RULE (the #394 correction, measured): a kill goes to the player the log names in "Player <v>
# Dead, killed by <k>", EXCEPT when <k> had already left the game for good ("Player quit revive, <k>"):
# a knocked player who bleeds out after the knocker is gone earns nobody a kill. With that rule the
# debugger agrees with the result file for every player of the 19 games it saw whole (757 rows); without
# it one player in one game was a kill over. "Dead" alone is not out: this mode brings players back.
# Knocks are "Player '<v>' Knock Down, by '<k>'" (122 in one game), NOT the gun trace, which also fires
# on direct kills (189 in the same game); the trace only tells whether a knock or kill was a headshot.
#
# WHAT IT CANNOT GIVE: placement. The elimination lines matched the official rank for 41 of 221 teams, so
# a game with no result file asks the organizer for each team's placement (game_result_text below).
#
# FLOW (consumed by the website's Game files box on the Upload Results tab, GameFilesUpload.tsx):
#   1. POST events/<id>/game-files/read/   one file per request (the browser gzips debugger files: a
#      35 MB log is 2.9 MB, under nginx's 25 MB). Parsed into a compact form, kept in the cache for
#      BATCH_TTL under an opaque file id; the file itself is never stored.
#   2. POST events/<id>/game-files/games/  {file_ids} -> the games: pieces joined across files, paired
#      with result files by match id, a suggested match, the checks, and the differences to compare.
#   3. Per game the browser posts the text from POST .../game-files/result-text/ through the EXISTING
#      upload_team_match_result (roster by UID, flags, scoring, MatchResultLog), then
#      POST .../game-files/debugger/ fills knocks, deaths, revives, headshots, survival and the kill feed.
# Also read by: debugger_ingest.parse_debugger_log and kill_feed.parse_kill_events (one parser, so the
# old Debugger log panel and the kill feed count knocks and kills the same way).

import gzip
import io
import re
import secrets
from datetime import datetime

MAX_UPLOAD_BYTES = 24 * 1024 * 1024      # one request; nginx allows 25 MB on the API host
MAX_TEXT_BYTES = 200 * 1024 * 1024       # a gzipped log is unpacked up to this, never further
BATCH_TTL = 6 * 60 * 60                  # an evening's upload session
STITCH_GAP_SECONDS = 15 * 60             # a restarted client is back well inside this
STITCH_MIN_SHARED = 0.7                  # share of players two pieces of one game have in common
ROSTER_MIN_SHARE = 0.5                   # below this a game is not this event's (the 19 Oct mix-up)

_RE_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]")
_ENTER = "EventTypeEnterGame"
_RE_JOIN = re.compile(r"Player Join, (\d+), (\d+), (.+?),")
_RE_DEAD = re.compile(r"Player (\d+) Dead, killed by (\d+)")
_RE_KNOCK = re.compile(r"Player '(\d+)' Knock Down, by '(\d+)'")
_RE_TRACE = re.compile(r"PlayKnockDownGunTrace killer=(\d+) victim=(\d+) headshot=(True|False)")
_RE_WEAPON = re.compile(r"OnGameAchievementNotify killerId:(\d+) beKilled:(\d+) weapon:(-?\d+)")
_RE_RESPAWN = re.compile(r"Revive Player (\d+)")
_RE_OUT = re.compile(r"Player quit revive, (\d+)")
_RE_END = re.compile(r"matchend matchid = (\d+)")
_RE_RESULT_NAME = re.compile(r"MatchResult_(\d+)_(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})")
# the game a stored upload came from (MatchResultLog.file_name): a result file, or one this box wrote
_RE_SAVED_GAME = re.compile(r"MatchResult_(?:from_debugger_)?(\d{6,})")
TRACE_WINDOW = 1.0   # a gun trace and its knock or kill land in the same frame or the next

# Event codes kept per piece: [t, code, victim, killer, extra]
#   "D" dead (extra: weapon id or None), "K" knock (extra: headshot or None), "H" headshot trace
#   (extra: True/False), "R" back in the game, "O" out for good. t = seconds from the piece's start.


def _ts(line):
    m = _RE_TS.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f") if m else None


def unpack_upload(data):
    """Bytes of an uploaded file -> text. A gzip upload (the browser compresses debugger logs) is unpacked
    with a cap, so a small file can never grow without end. Raises ValueError("too_large")."""
    if data[:2] == b"\x1f\x8b":
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while True:
                chunk = gz.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                if out.tell() > MAX_TEXT_BYTES:
                    raise ValueError("too_large")
        data = out.getvalue()
    return data.decode("utf-8-sig", errors="replace")


# ── 1. reading ─────────────────────────────────────────────────────────────────────────────────────────

def read_debugger(text):
    """A debugger log -> its pieces in file order. A piece runs from one "EventTypeEnterGame" to the next
    (or to the file's end); a file that opens mid-game starts with an implicit piece. Pieces nobody
    joined (menus, lobby) are dropped, the same rounds debugger_ingest always counted.
    Each piece: {"start", "last" (ISO), "match_id" (None when the file stops before the game ended),
    "implicit" (opened mid-game), "players": {eid: [uid, ign]}, "events": [[t, code, victim, killer, extra]]}"""
    pieces, cur = [], None
    pending_trace = {}   # (killer, victim) -> (t, headshot): a trace waiting for its knock or kill

    def close():
        if cur and cur["players"]:
            cur["start"] = cur["start"].isoformat() if cur["start"] else None
            cur["last"] = cur["last"].isoformat() if cur["last"] else cur["start"]
            pieces.append(cur)

    def new_piece(ts, implicit):
        return {"start": ts, "last": ts, "match_id": None, "implicit": implicit, "players": {}, "events": []}

    def headshot_for(killer, victim, t):
        hit = pending_trace.pop((killer, victim), None)
        return hit[1] if hit and t - hit[0] <= TRACE_WINDOW else None

    for line in (text or "").splitlines():
        ts = _ts(line)
        if _ENTER in line:
            close()
            cur, pending_trace = new_piece(ts, False), {}
            continue
        if cur is None:
            cur = new_piece(ts, True)
        if ts:
            if cur["start"] is None:
                cur["start"] = ts
            cur["last"] = ts
        t = round((ts - cur["start"]).total_seconds(), 3) if ts and cur["start"] else 0.0
        m = _RE_JOIN.search(line)
        if m:
            cur["players"][m.group(2)] = [m.group(1), m.group(3).strip()]
            continue
        m = _RE_TRACE.search(line)
        if m:
            killer, victim, hs = m.group(1), m.group(2), m.group(3) == "True"
            cur["events"].append([t, "H", victim, killer, hs])
            # the trace can follow its knock or kill in the same frame: give it back to that event
            for ev in reversed(cur["events"][-6:]):
                if ev[1] in ("K", "D") and ev[2] == victim and ev[3] == killer and t - ev[0] <= TRACE_WINDOW:
                    if ev[1] == "K" and ev[4] is None:
                        ev[4] = hs
                    break
            else:
                pending_trace[(killer, victim)] = (t, hs)
            continue
        m = _RE_KNOCK.search(line)
        if m:
            victim, killer = m.group(1), m.group(2)
            cur["events"].append([t, "K", victim, killer, headshot_for(killer, victim, t)])
            continue
        m = _RE_DEAD.search(line)
        if m:
            victim, killer = m.group(1), m.group(2)
            pending_trace.pop((killer, victim), None)
            cur["events"].append([t, "D", victim, killer, None])
            continue
        m = _RE_WEAPON.search(line)
        if m:
            killer, victim, weapon = m.group(1), m.group(2), int(m.group(3))
            # the weapon line follows its kill in the same frame; a weapon below 0 (zone, fall, bleed) is no gun
            for ev in reversed(cur["events"][-8:]):
                if ev[1] == "D" and ev[2] == victim and ev[3] == killer:
                    if ev[4] is None and weapon >= 0:
                        ev[4] = weapon
                    break
            continue
        m = _RE_RESPAWN.search(line)
        if m:
            cur["events"].append([t, "R", m.group(1), None, None])
            continue
        m = _RE_OUT.search(line)
        if m:
            cur["events"].append([t, "O", m.group(1), None, None])
            continue
        m = _RE_END.search(line)
        if m:
            cur["match_id"] = m.group(1)
    close()
    return pieces


def read_result(text, filename):
    """A MatchResult file -> {"match_id", "played_at" (ISO, from the file name), "teams": [{"name", "rank",
    "kills", "players": [[name, uid, kills]]}], "text"}. The text is kept: it is what gets uploaded."""
    from .views import TEAM_BLOCK_RE, PLAYER_RE

    m = _RE_RESULT_NAME.search(filename or "")
    match_id = m.group(1) if m else None
    played_at = None
    if m:
        try:
            played_at = datetime(*(int(x) for x in m.groups()[1:])).isoformat()
        except ValueError:
            played_at = None
    teams = []
    for block in TEAM_BLOCK_RE.finditer(text or ""):
        players = [[p.group("name").strip(), p.group("uid"), int(p.group("kills"))]
                   for p in PLAYER_RE.finditer(block.group("players_block"))]
        teams.append({"name": block.group("team_name").strip(), "rank": int(block.group("placement")),
                      "kills": int(block.group("team_kills")), "players": players})
    return {"match_id": match_id, "played_at": played_at, "teams": teams, "text": text}


def looks_like(filename, text):
    """'debugger' | 'result' | None, by content first (a renamed file still reads), then by name."""
    head = (text or "")[:200000]
    if "TeamName:" in head and "NAME:" in head:
        return "result"
    if _ENTER in head or "Player Join," in head or _RE_TS.match(head):
        return "debugger"
    name = (filename or "").lower()
    if name.startswith("matchresult_"):
        return "result"
    if name.startswith("debugger"):
        return "debugger"
    return None


# ── 2. counting one game ───────────────────────────────────────────────────────────────────────────────

def game_stats(players, events):
    """Replay a game's events in order. Returns (per_player, kill_events):
    per_player {eid: {uid, ign, team, kills, deaths, knockdowns, headshots, revives_received,
    survival_seconds}} and the kill feed list kill_feed.store_events takes (credited kills and knocks)."""
    out_for_good, last_death, end_t = set(), {}, 0.0
    stats = {eid: {"uid": uid, "ign": ign, "team": int(eid) >> 24, "kills": 0, "deaths": 0, "knockdowns": 0,
                   "headshots": 0, "revives_received": 0, "survival_seconds": 0}
             for eid, (uid, ign) in players.items()}
    feed = []

    def uid(eid):
        return players.get(eid, [None])[0]

    for t, code, victim, killer, extra in events:
        end_t = max(end_t, t)
        if code == "O":
            out_for_good.add(victim)
        elif code == "R":
            if victim in stats:
                stats[victim]["revives_received"] += 1
        elif code == "H":
            if extra and killer in stats:
                stats[killer]["headshots"] += 1
        elif code == "K":
            if killer in stats and killer != victim:
                stats[killer]["knockdowns"] += 1
            if uid(killer) and uid(victim) and killer != victim:
                feed.append({"t": t, "kind": "knock", "killer_uid": uid(killer), "victim_uid": uid(victim),
                             "headshot": extra, "weapon": None})
        elif code == "D":
            if victim in stats:
                stats[victim]["deaths"] += 1
                last_death[victim] = t
            # the #394 rule: no kill for a knocker who already left for good (a bleed-out after they died)
            if killer != victim and killer not in out_for_good:
                if killer in stats:
                    stats[killer]["kills"] += 1
                if uid(killer) and uid(victim):
                    feed.append({"t": t, "kind": "kill", "killer_uid": uid(killer), "victim_uid": uid(victim),
                                 "headshot": None, "weapon": extra})
    for eid, s in stats.items():
        s["survival_seconds"] = int(last_death.get(eid, end_t))
    return stats, feed


# ── 3. games out of many files ─────────────────────────────────────────────────────────────────────────

def _dt(iso):
    return datetime.fromisoformat(iso) if iso else None


def _shared(a, b):
    ua, ub = {v[0] for v in a.values()}, {v[0] for v in b.values()}
    return len(ua & ub) / max(1, min(len(ua), len(ub)))


def build_games(files):
    """files: [{"id", "name", "kind", "pieces" | "result"}] -> games in time order. A piece with no end is
    joined to the next piece of the same players that opens within STITCH_GAP_SECONDS (a restarted
    client), then debugger games and result files are paired by the game's own match id."""
    pieces = []
    for f in files:
        for i, p in enumerate(f.get("pieces") or []):
            pieces.append(dict(p, file_id=f["id"], file_name=f["name"], piece=i))
    pieces.sort(key=lambda p: (p["start"] or "", p["file_name"]))

    games, open_game = [], None
    for p in pieces:
        if open_game:
            gap = (_dt(p["start"]) - _dt(open_game["last"])).total_seconds() if p["start"] and open_game["last"] else None
            if gap is not None and 0 <= gap <= STITCH_GAP_SECONDS and _shared(open_game["players"], p["players"]) >= STITCH_MIN_SHARED:
                offset = (_dt(p["start"]) - _dt(open_game["start"])).total_seconds()
                seen_kills = any(ev[1] in ("D", "K") for ev in open_game["events"])
                open_game["events"] += [[round(ev[0] + offset, 3)] + ev[1:] for ev in p["events"]]
                open_game["players"] = {**open_game["players"], **p["players"]}
                open_game["sources"].append({"file": p["file_name"], "piece": p["piece"]})
                if seen_kills:
                    open_game["gaps"].append(int(gap))
                open_game["last"], open_game["match_id"] = p["last"], p["match_id"]
                if p["match_id"]:
                    open_game = None
                continue
            open_game = None
        g = {"key": None, "start": p["start"], "last": p["last"], "match_id": p["match_id"],
             "implicit": p["implicit"], "players": dict(p["players"]), "events": list(p["events"]),
             "sources": [{"file": p["file_name"], "piece": p["piece"]}], "gaps": []}
        games.append(g)
        if not p["match_id"]:
            open_game = g

    results = {}
    loose_results = []
    for f in files:
        r = f.get("result")
        if not r:
            continue
        entry = dict(r, file_name=f["name"])
        if r.get("match_id") and r["match_id"] not in results:
            results[r["match_id"]] = entry
        else:
            loose_results.append(entry)

    out = []
    for g in games:
        r = results.pop(g["match_id"], None) if g["match_id"] else None
        # a piece that never ended and saw no knock or kill is a lobby (3 Oct: 59 players waiting, then a
        # different room); with no result file it has nothing to save
        if not r and not g["match_id"] and not any(ev[1] in ("D", "K") for ev in g["events"]):
            continue
        out.append({"debugger": g, "result": r})
    for r in list(results.values()) + loose_results:
        out.append({"debugger": None, "result": r})

    def when(item):
        d, r = item["debugger"], item["result"]
        return (d and d["start"]) or (r and r.get("played_at")) or ""

    out.sort(key=when)
    for i, item in enumerate(out):
        d, r = item["debugger"], item["result"]
        item["key"] = (d and d["match_id"]) or (r and r.get("match_id")) or f"g{i}"
    return out


# ── 4. one game against the other file ────────────────────────────────────────────────────────────────

def compare(item):
    """Differences between the result file and the debugger for a game that has both: team kills and
    player kills, keyed by UID. Empty when they agree (the usual case, see the kill rule above).
    Returns (diffs, {"result_kills", "debugger_kills"}) or ([], None) when the game has only one file.
    views_game_files reads "debugger saw fewer kills, or was joined across a restart" as `partial`."""
    d, r = item["debugger"], item["result"]
    if not (d and r):
        return [], None
    stats, _ = game_stats(d["players"], d["events"])
    dbg_by_uid = {s["uid"]: s["kills"] for s in stats.values()}
    diffs = []
    result_total = 0
    for team in r["teams"]:
        # the players' kills, not KillScore: in some modes KillScore is points (3 Oct: 560 for 56 kills)
        listed = sum(k for _, _, k in team["players"])
        result_total += listed
        dbg_team = sum(dbg_by_uid.get(uid, 0) for _, uid, _ in team["players"])
        if dbg_team != listed:
            diffs.append({"scope": "team", "name": team["name"], "uid": None, "result": listed, "debugger": dbg_team})
        for name, uid, kills in team["players"]:
            if dbg_by_uid.get(uid, 0) != kills:
                diffs.append({"scope": "player", "name": name, "uid": uid, "team": team["name"],
                              "result": kills, "debugger": dbg_by_uid.get(uid, 0)})
    # over the players the result file lists (a 60-player mode lists fewer players than played)
    dbg_total = sum(dbg_by_uid.get(uid, 0) for t in r["teams"] for _, uid, _ in t["players"])
    return diffs, {"result_kills": result_total, "debugger_kills": dbg_total}


# ── 5. which match each game belongs to ────────────────────────────────────────────────────────────────

def event_slots(event):
    """Every match of the event in play order, with what the suggestion needs."""
    from .models import Match, MatchResultLog, StageGroupCompetitor

    groups = {}
    for c in StageGroupCompetitor.objects.filter(stage_group__stage__event=event).exclude(tournament_team=None):
        groups.setdefault(c.stage_group_id, set()).add(c.tournament_team_id)
    # which game each match was last scored from (the newest stored upload names it), so uploading the
    # same evening again sends every game back to its own match
    saved_game = {}
    for match_id, name in (MatchResultLog.objects.filter(match__group__stage__event=event)
                           .order_by("uploaded_at", "log_id").values_list("match_id", "file_name")):
        m = _RE_SAVED_GAME.search(name or "")
        saved_game[match_id] = m.group(1) if m else None
    slots = []
    for m in (Match.objects.filter(group__stage__event=event)
              .select_related("group", "group__stage").order_by("group__stage_id", "group_id", "match_number")):
        g = m.group
        slots.append({"match_id": m.match_id, "match_number": m.match_number, "map": m.match_map or "",
                      "stage": g.stage.stage_name, "group": g.group_name, "group_id": g.group_id,
                      "playing_date": g.playing_date.isoformat() if g.playing_date else None,
                      "teams": groups.get(g.group_id, set()), "filled": bool(m.result_inputted),
                      "game": saved_game.get(m.match_id)})
    return slots


def suggest(event, items):
    """Give each game a suggested match and a state: "ok" (ready), "look" (no result file: placements
    needed) or "skip" (not this event's game, so it is set to Don't use until somebody picks a match)."""
    from . import kill_feed

    roster = kill_feed.roster_teams(event)
    slots = event_slots(event)
    # games already saved on a match keep it, so they are placed first; the rest fill free matches in order
    taken = {s["match_id"] for s in slots if s["game"] and any(i["key"] == s["game"] for i in items)}
    for item in items:
        d, r = item["debugger"], item["result"]
        uids = ({v[0] for v in d["players"].values()} if d
                else {uid for t in r["teams"] for _, uid, _ in t["players"]})
        on = [roster[u.lstrip("0")] for u in uids if u.lstrip("0") in roster]
        when = _dt((d and d["start"]) or (r and r.get("played_at")))
        item["players"] = len(uids)
        item["on_roster"] = len(on)
        item["when"] = when.isoformat() if when else None
        reasons = []
        day = when.date() if when else None
        if day and event.start_date and day < event.start_date:
            reasons.append("before_event")
        elif day and event.end_date and (day - event.end_date).days > 1:
            reasons.append("after_event")
        if not uids or len(on) / len(uids) < ROSTER_MIN_SHARE:
            reasons.append("not_on_roster")
        item["checks"] = reasons
        # the group whose teams these players belong to; on a tie the one played that day
        teams_here = set(on)
        best, best_score = None, (-1, -1)
        for s in slots:
            score = (len(teams_here & s["teams"]), 1 if day and s["playing_date"] == day.isoformat() else 0)
            if score > best_score:
                best, best_score = s["group_id"], score
        pick = None
        again = next((s for s in slots if s["game"] == item["key"]), None)
        item["saved_before"] = bool(again)
        if again:
            pick = again          # this very game is already on that match: same place again
        elif not reasons and best is not None:
            group_slots = [s for s in slots if s["group_id"] == best and s["match_id"] not in taken]
            free = [s for s in group_slots if not s["filled"]]
            pick = (free or group_slots or [None])[0]
        item["match_id"] = pick["match_id"] if pick else None
        if pick:
            taken.add(pick["match_id"])
        item["state"] = "skip" if not pick else ("ok" if r else "look")
    return [{k: v for k, v in s.items() if k not in ("teams", "game")} for s in slots]


# ── 6. the text that goes through the normal result upload ────────────────────────────────────────────

def game_result_text(item, keep_debugger=(), placements=None, team_names=None):
    """The MatchResult text to upload for this game.
    With a result file: that file, with the kills of every player in `keep_debugger` (UIDs the organizer
    chose to take from the debugger) replaced, and the team's KillScore moved by the same amount.
    Without one: written from the debugger in the same format, one block per team (players grouped by
    the game's team, eid >> 24), placements[team] given by the organizer. Returns (text, None) or
    (None, error code)."""
    d, r = item["debugger"], item["result"]
    dbg = {}
    if d:
        stats, _ = game_stats(d["players"], d["events"])
        dbg = {s["uid"]: s["kills"] for s in stats.values()}
    if r:
        keep = {str(u) for u in keep_debugger or ()}
        if not keep:
            return r["text"], None
        lines = []
        for t in r["teams"]:
            players = [[n, u, dbg.get(u, k) if u in keep else k] for n, u, k in t["players"]]
            team_kills = t["kills"] + sum(p[2] for p in players) - sum(k for _, _, k in t["players"])
            lines.append(_team_block(t["name"], t["rank"], team_kills, players))
        return "\n".join(lines) + "\n", None
    if not d:
        return None, "no_files_for_game"
    placements = {str(k): v for k, v in (placements or {}).items()}
    teams = {}
    for s in stats.values():
        teams.setdefault(str(s["team"]), []).append([s["ign"], s["uid"], s["kills"]])
    ranks = {}
    for team in teams:
        try:
            ranks[team] = int(placements.get(team))
        except (TypeError, ValueError):
            return None, "placement_missing"
    if sorted(ranks.values()) != list(range(1, len(ranks) + 1)):
        return None, "placements_not_one_each"
    names = team_names or {}
    lines = [_team_block(names.get(team) or f"Team {team}", ranks[team], sum(p[2] for p in players), players)
             for team, players in sorted(teams.items(), key=lambda kv: ranks[kv[0]])]
    return "\n".join(lines) + "\n", None


def _team_block(name, rank, kills, players):
    head = f"TeamName: {name}   Rank: {rank}   KillScore: {kills}   RankScore: 0   TotalScore: {kills}"
    return "\n".join([head] + [f"NAME: {n}   ID: {u}   KILL: {k}" for n, u, k in players])


def game_teams(event, item):
    """For a game with no result file: its teams as the organizer will rank them, named after the
    registered team most of their players belong to."""
    from . import kill_feed
    from .models import TournamentTeam

    d = item["debugger"]
    if not d:
        return []
    roster = kill_feed.roster_teams(event)
    stats, _ = game_stats(d["players"], d["events"])
    teams = {}
    for s in stats.values():
        teams.setdefault(str(s["team"]), []).append(s)
    # display_name, never .team.team_name: a ghost team has no .team (models.TournamentTeam)
    names = {tt.tournament_team_id: tt.display_name
             for tt in TournamentTeam.objects.filter(event=event).select_related("team", "ghost_team")}
    out = []
    for key, members in sorted(teams.items(), key=lambda kv: int(kv[0])):
        ids = [roster.get(m["uid"].lstrip("0")) for m in members]
        ids = [i for i in ids if i]
        site = max(set(ids), key=ids.count) if ids else None
        out.append({"team": key, "name": names.get(site) or f"Team {key}", "registered": bool(site),
                    "kills": sum(m["kills"] for m in members), "players": [m["ign"] for m in members]})
    return out


def new_file_id():
    return secrets.token_hex(12)
