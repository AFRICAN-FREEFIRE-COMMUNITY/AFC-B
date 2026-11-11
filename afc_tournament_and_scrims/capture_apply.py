"""Read uploaded game files the way AFC Capture reads them live, and store the result the way its upload does
(owner, 11 Nov 2026, inbox #541: "when you upload a debugger file, you should be able to get all stats that
capture wold have given ypou and if not, the site should request for all files it needs for upload").

THE THREE FILES OF A MAP (all in the game's Free Fire_64_Data folder on the observer PC)
  debugger-<date>.log            Debugger\\   who played, kills, knocks, deaths, revives, headshots, grenades,
                                              gloo walls, medkits, survival, the kill feed, the order teams
                                              were wiped (so the placements); one log can hold many maps
  MatchResult_<id>_<date>.log    (the folder) the game's official placements and kills per player
  ReplayInfo_<id>_<date>.bin     Replays\\    every player's damage and the gun of every kill and knock

WHAT IS HERE
  read_replay(data, name)      a replay file's bytes -> the small part a map needs (damage + guns), so the
                               20 MB file is never kept: {"ff_match_id", "damage", "deaths", "knocks"}
  entry_for_game(logs, ff, replays)
                               the debugger log(s) holding map `ff` (texts, oldest first) + its replays ->
                               Capture's own per-map payload {"rich_stats", "kill_events"} through the copied
                               reader (capture_reader.tailer.LiveMatchState, the same code as the observer PC),
                               or (None, reason) when the log never reaches the map's end
  placements_for_game(...)     the placement of every team the log saw wiped, by the players' game UIDs
  apply_entry(match, entry)    store it: player rows' rich stats and damage (matched by UID; flagged players
                               keep theirs on MatchKillFlag.stats), the kill feed with guns, each team's
                               damage and its points recomputed from stored values. Results (kills,
                               placements, flags) are NOT touched: the MatchResult upload owns those.

CALLERS: views_game_files.game_files_read / game_files_games / game_files_debugger (the Game files box),
management/commands/backfill_capture_stats.py (apply_entry, team_damage_and_points).
"""

import os
import tempfile

from django.db import transaction

from . import capture_rich_stats, kill_feed, result_writes
from . import scoring as scoring_lib
from .models import MatchKillFlag, TournamentTeamMatchStats

REPLAY_PREFIX = "ReplayInfo_"


def ff_id_from_replay_name(name):
    """ReplayInfo_<game id>_<date>.bin -> "<game id>" ("" when the name does not say)."""
    base = os.path.basename(str(name or ""))
    if not base.startswith(REPLAY_PREFIX):
        return ""
    part = base[len(REPLAY_PREFIX):].split("_", 1)[0]
    return part if part.isdigit() else ""


def read_replay(data, name):
    """A replay file -> {"ff_match_id", "damage": {eid: damage}, "deaths": [...], "knocks": [...]}.
    Raises ValueError("not_a_replay") when nothing in it reads as replay records."""
    from .capture_reader.replay import GameDamage, KillGuns, ReplayReader

    reader = ReplayReader("")
    records = reader.feed(bytes(data))
    if not records:
        raise ValueError("not_a_replay")
    guns, damage = KillGuns(), GameDamage()
    guns.feed(records)
    damage.feed(records)
    return {"ff_match_id": ff_id_from_replay_name(name), "damage": dict(damage.damage),
            "deaths": list(guns.deaths), "knocks": list(guns.knocks)}


def _segment(texts, ff):
    """Every line of map `ff` across the given debugger logs, oldest first (a client crash leaves a map in
    two logs, joined the way the observer PC joins them: tailer.match_segment)."""
    from .capture_reader.tailer import match_segment

    lines = []
    for text in texts:
        fd, path = tempfile.mkstemp(suffix=".log")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            seg, _start = match_segment(path, ff, newest_older=False)
        finally:
            os.unlink(path)
        if seg:
            lines.extend(seg)
    return lines


def _state_for(texts, ff):
    from .capture_reader.tailer import LiveMatchState

    lines = _segment(texts, ff)
    if not lines:
        return None, "no_debugger_lines"
    state = LiveMatchState()
    for ln in lines:
        state.feed_line(ln)
    if ff not in state.finished:
        return state, "log_never_reaches_the_end"
    return state, ""


def entry_for_game(texts, ff, replays=()):
    """(entry, "") or (None, reason). See the module docstring."""
    state, why = _state_for(texts, ff)
    if why:
        return None, why
    if replays:
        deaths, knocks, damage = [], [], {}
        for r in replays:                      # oldest session first; the later figure is the newer one
            deaths.extend(r["deaths"])
            knocks.extend(r["knocks"])
            damage.update(r["damage"])
        state.apply_replay_guns(deaths, knocks, announce=False)
        state.apply_replay_damage(damage, ff)
    return {"ff_match_id": ff, "rich_stats": state.finished[ff],
            "kill_events": state.finished_kills.get(ff, [])}, ""


def placements_for_game(texts, ff):
    """{frozenset(game UIDs of a team): placement} for every team of map `ff` the log saw, by the order teams
    were wiped (first out = last place; the last team standing = 1). {} when the log never reaches the end."""
    state, why = _state_for(texts, ff)
    if why or state is None:
        return {}
    teams = sorted(state.teams)
    total = len(teams)
    out = {}
    order = state.eliminated_order
    alive = [t for t in teams if t not in order]
    for t in teams:
        uids = frozenset(str(p.get("uid") or "").lstrip("0") for e, p in state.players.items()
                         if state.entity_team.get(e) == t and p.get("uid"))
        if not uids:
            continue
        if t in order:
            out[uids] = total - order[t] + 1
        elif len(alive) == 1:
            out[uids] = 1
    return out


def team_damage_and_points(match, damage_by_uid):
    """Set every team row's damage from its players (+ counted flags) and recompute total_points. Returns the
    rows. (Moved here from backfill_capture_stats so the Game files box applies maps the same way.)"""
    ctx = result_writes.scoring_context(match)
    flags = {}
    for f in MatchKillFlag.objects.filter(match=match).select_related("tournament_team__event"):
        if f.effective_count:
            flags.setdefault(f.tournament_team_id, []).append(str(f.uid).strip())
    rows = list(TournamentTeamMatchStats.objects.filter(match=match).prefetch_related("player_stats"))
    for ts in rows:
        damage = sum(p.damage for p in ts.player_stats.all() if p.played)
        damage += sum(damage_by_uid.get(uid, 0) for uid in flags.get(ts.tournament_team_id, []))
        pts = scoring_lib.compute_team_points(
            placement_points=ctx["placement_points"], kill_point=ctx["kill_point"],
            points_per_assist=ctx["points_per_assist"], points_per_1000_damage=ctx["points_per_1000_damage"],
            placement=ts.placement, kills=ts.kills, damage=damage, assists=ts.assists,
            bonus=ts.bonus_points or 0, penalty=ts.penalty_points or 0, played=ts.played,
        )
        ts.damage = damage
        ts.total_points = pts["total_points"]
        ts.save(update_fields=["damage", "total_points"])
    return rows


def apply_entry(match, entry, source="capture"):
    """Store one map's Capture payload on `match`. Returns {"players", "kill_events", "with_damage"}."""
    rich, reason = capture_rich_stats.parse_rich_stats(entry.get("rich_stats"))
    if rich is None:
        raise ValueError("rich_stats_" + reason)
    kills = entry.get("kill_events")
    kills = kill_feed.clean_events(kills) if kills is not None else None
    damage = capture_rich_stats.damage_by_uid(rich)
    with transaction.atomic():
        filled = capture_rich_stats.apply_capture_rich_stats(match, rich)
        stored = kill_feed.store_events(match, match.group.stage.event, kills, source) if kills else 0
        team_damage_and_points(match, damage)
    return {"players": filled, "kill_events": stored, "with_damage": sum(1 for v in damage.values() if v)}
