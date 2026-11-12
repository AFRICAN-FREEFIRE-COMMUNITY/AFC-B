"""Live debugger tailer - Tier 2 (true in-round live standings).

WHAT IT DOES
------------
Follows the newest ``Free Fire_64_Data\\Debugger\\debugger-*.log`` (the game's
append-only debug stream, ms latency), reconstructs per-TEAM and per-PLAYER live
stats from the verified debugger tokens, and hands a standings snapshot to a pusher
every few seconds. This is the local half of Tier 2 (LIVE_LEADERBOARD_SPEC.md §5); it
is the live-in-round sibling of ``watcher.py`` (Tier 1, per-round MatchResult upload).

WHERE THE SNAPSHOT GOES
-----------------------
``LiveTailer`` is given a *pusher* (uploader.LivePusher) and calls
``pusher.push(rows, updated_at)`` on a timer. That PUTs the rows to
``<backend>/events/live/push/`` with the event-scoped ``X-Upload-Token``; the backend
RANKS AND SCORES them there (live_ranking.normalize_live_standings, from the event's own
scoring config - this client sends no positions and no points, owner 2026-09-22), caches
the result in Redis under ``overlay:live:<event>:<stage>:<group>`` (short TTL) and the public
``overlay/feed/?live=1`` serves them until the authoritative MatchResult (Tier 1)
supersedes them. The pusher is INJECTED (not imported here) so this module stays
stdlib-only and unit-testable without ``requests``.

VERIFIED TOKENS (OB55, re-measured 2026-09-22 on 24 matches + two live matches; the
gates are WEBSITE/GATES-ff-live-2026-09-22.md, the facts are memory
project_freefire_live_capture). Actor = in-game ENTITY id (eid); the observer PC sees
ALL teams. Every line is ``[YYYY-MM-DD HH:MM:SS.mmm][frame][seq] msg``; lines of one
game frame share the same ``seq``.
  - identity (match start)  : ``Player Join, <UID>, <eid>, <IGN>,<bool><teamIndex>``
                              (the ACCOUNT UID and the exact in-game name, 1 per player)
  - roster (name fallback)  : ``AddPlayer id(\\d+),name(.*?),gsTeam(\\d+)``
  - team name -> TeamID     : ``OnTeamScoreInited -> TeamName: (.*?) TeamID: (\\d+)``
  - team score burst        : ``OnTeamScoreChanged -> TeamID: (\\d+) TeamScore: (\\d+)``
  - knockdown               : ``Player '(\\d+)' Knock Down, by '(\\d+)'``
  - headshot flag           : ``PlayKnockDownGunTrace killer=(\\d+) victim=(\\d+) headshot=(True|False)``
                              (fires on knocks AND on direct kills)
  - kill / death            : ``Player (\\d+) Dead, killed by (\\d+)``  (killer == victim: zone / fall)
  - weapon per kill         : ``OnGameAchievementNotify killerId:(\\d+) beKilled:(\\d+) weapon:(-?\\d+)``
                              (PARTIAL: ~60% of kills carry it)
  - respawn after death     : ``Revive Player (\\d+), revivePosition=...``  (this mode lets a dead
                              player be brought back by the team; a player can die several times)
  - item counters           : ``SpectatorItemCountSync : ->(\\d+) ->EITEM_TYPE_<KIND> ->(\\d+)``
                              for EVERY player (a decrease = that many used / thrown)
  - team eliminated         : ``PCInGameFlagManager: Team (\\d+) eliminated``  (entity-team space)
  - match segment           : ``EventTypeEnterGame`` .. ``EventType(CompleteGame|LeaveGame) .. "match_id":(\\d+)``
  - match end (freeze)      : ``[MatchResult] receive S2C_RUDP_MatchEnd_Res``

TWO ID SPACES (the 2026-09-22 finding; the whole reason this file was rebuilt):
  - ENTITY space: eids. ``eid >> 24`` is the entity team = the order teams joined the match.
    Kills, knocks, joins, items and ``Team N eliminated`` all live here.
  - SCORE space: the ``TeamID`` of OnTeamScoreInited / OnTeamScoreChanged = the custom room's
    team slot. Team NAMES and TeamScore live here. It equals ``eid >> 24`` by chance only
    (30 of 283 team rows over 24 matches), so joining the two by number named ~9 of 12
    teams wrong on the live board. ``TeamScore`` is also CUMULATIVE across the room's
    matches (its per-match delta is the official TotalScore), so it is never this match's
    points and never the ranking here.
  The bridge between the spaces is LEARNED: in a frame with exactly one enemy kill and
  exactly one TeamScore that rose, the killer's entity team IS that TeamID, decided by
  MAJORITY over the match's such frames (264 of 264 correct over 24 matches, 0 wrong; a
  single frame can lie). See LiveMatchState._end_frame and TeamResolver.bridge.

TEAM NAMING (owner 2026-09-22: "very flexible ... one or two current players that have
proper UIDs, or based off what other teams have ... or wait for the game to end for the
first map"): every entity team is named by TeamResolver from, in order,
  1. the AFC roster by UID (one member on the roster names the whole team),
  2. the in-log bridge (the team's first kill teaches the game's own name),
  3. memory: a MatchResult of an EARLIER map this session, or an earlier map's bridge (team
     name -> UIDs), counted only when the name is one of THIS match's game team names,
  4. exclusion (every other team named -> the last unclaimed name),
and stays "Team N" (``team_name_pending``) until one of those lands. Names learned by 2
and 3 are written back to the UID memory so the NEXT map names the team at the join.

PER-PLAYER STATS (owner 2026-09-22): each team row carries ``players`` with kills, deaths,
knockdowns, knocked, headshots, assists (knock assists: A knocked, B finished), revives
(respawns), grenades_used, grenade_kills, gloowall_used, medkit_used, most_used_weapon,
alive. NOT available for other players in the observer stream, verified in the log and in
the binary's format strings: damage / HP dealt, who rescued a knocked teammate.

PRIVACY (spec §7 / memory SECURITY)
-----------------------------------
The tailer only ever OPENS files whose basename matches ``debugger-*.log``, plus the
``MatchResult_*.log`` the controller hands it after Tier 1 has uploaded it (the same two
file kinds the README promises), plus, from 1.5.4, the game's own replay of the match being
played (``Replays\\ReplayInfo_<match id>_*.bin``, inbox #506: the gun of every kill and knock). It never reads the plaintext-credential
``GuestCacheProduction.json`` or anything else in the folder.

CONSUMERS
---------
- app.py's CaptureController builds a LivePusher + one LiveTailer and start()/stop()s
  it alongside the MatchResultWatcher, gated by config.live_enabled; it also calls
  ``learn_match_result(path)`` after each Tier-1 upload and reads ``attribution_text()``
  for the tray's live status line.
- tests/test_tailer.py feeds scripted lines to LiveMatchState (pure, no threads/IO).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Optional

from .events import emit
from .replay import GameDamage, KillGuns, ReplayReader
from .weapons import weapon_label
from .roster import _norm_name

log = logging.getLogger("afc_capture")

# --------------------------------------------------------------------------- #
# Verified token regexes (see module docstring).
# --------------------------------------------------------------------------- #
_RE_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]")
_RE_SEQ = re.compile(r"^\[[^\]]*\]\[[^\]]*\]\[(\d+)\]")
_RE_JOIN = re.compile(r"Player Join, (\d+), (\d+), (.*?),(?:True|False)\d+\s*$")
_RE_ADD_PLAYER = re.compile(r"AddPlayer id(\d+),name(.*?),gsTeam(\d+)")
_RE_TEAM_NAME = re.compile(r"OnTeamScoreInited -> TeamName: (.*?) TeamID: (\d+)")
_RE_TEAM_SCORE = re.compile(r"OnTeamScoreChanged -> TeamID: (\d+) TeamScore: (\d+)")
_RE_KILL = re.compile(r"Player (\d+) Dead, killed by (\d+)")
_RE_KNOCK = re.compile(r"Player '(\d+)' Knock Down, by '(\d+)'")
_RE_GUN_TRACE = re.compile(r"PlayKnockDownGunTrace killer=(\d+) victim=(\d+) headshot=(True|False)")
_RE_WEAPON = re.compile(r"OnGameAchievementNotify killerId:(\d+) beKilled:(\d+) weapon:(-?\d+)")
_RE_REVIVE = re.compile(r"Revive Player (\d+)")
# "Player quit revive, <eid>": that player has left the game for good (inbox #397). A knocked player
# who bleeds out AFTER their knocker left earns nobody a kill: the site's game files reader
# (AFC-B game_files.py, the #394 rule) and the official result file agree on it, so Capture must too.
_RE_OUT = re.compile(r"Player quit revive, (\d+)")
# "NotifyPlayerEnterPendingRevive -> <eid>": that player is dead and waits for a respawn. In play it follows every
# death but the one that ends a team (37 of 43 deaths on 6 Nov 2026; the other 6 each ended a team); in the Player
# Join burst of a rejoin it is the ONLY sign
# of who was dead at that moment (YUNG, LYNX and SHEFCO of NOTFOUND: LYNX never came back, so the team was out when
# its last two died, and no "Team N eliminated" line said so).
_RE_PENDING = re.compile(r"NotifyPlayerEnterPendingRevive -> (\d+)")
_RE_ITEM = re.compile(r"SpectatorItemCountSync : ->(\d+)\s+->EITEM_TYPE_(BUILDING|MEDKIT|GRENADE\w*)\s+->(\d+)")
_RE_TEAM_ELIM = re.compile(r"PCInGameFlagManager: Team (\d+) eliminated")
_RE_ENTER = re.compile(r"EventTypeEnterGame")
# A game client that lost its match and was restarted rejoins it (inbox #507, 6 Nov 2026, live): the new session's
# log prints ``rejoin last battle <match id>`` and then a fresh EventTypeEnterGame plus a Player Join for every
# player still in the match (same UIDs, entity ids, names and teams as before the crash, checked on 24 of 24).
_RE_REJOIN = re.compile(r"rejoin last battle (\d+)")
# Any line naming the match being played (EventTypeMicVoiceTime and others, mid-match): tells a rejoin into THIS
# match from a rejoin into another one.
_RE_ANY_MATCH_ID = re.compile(r'"match_id":(\d+)')
_RE_END_SEG = re.compile(r'EventType(?:CompleteGame|LeaveGame).*?"match_id":(\d+)')
# The observer opened an INSTANT REPLAY (6 Nov 2026, NFL Group D map 3, 21:07:39): the client reloads the match and
# RE-PRINTS every event of it into the log (kills, knocks, respawns, quits, wipes, gun lines, item counters, Player
# Join, all in the original order with new timestamps), then prints the second line on its way back to live. Nothing
# between the two is live: the live match goes on unseen (the official result fills that gap at the end).
_RE_REPLAY_START = re.compile(r"\[InstantReplay\] SaveAndLoadReplay")
_RE_REPLAY_END = re.compile(r"\[InstantReplay TryReconnectToMatch\] Start")
_RE_MATCH_END = re.compile(r"\[MatchResult\] receive S2C_RUDP_MatchEnd_Res")

# MatchResult_*.log (Tier 1 file) tokens, for learn_match_result(): team name -> its players' UIDs.
_RE_MR_TEAM = re.compile(r"TeamName:\s*(.+?)\s+Rank:")
_RE_MR_PLAYER = re.compile(r"NAME:\s*.+?\s+ID:\s*(\d+)\s+KILL:")

# Which files the tailer is allowed to open (privacy gate). ONLY debugger-*.log.
_DEBUGGER_RE = re.compile(r"^debugger-.*\.log$", re.IGNORECASE)
_MATCH_RESULT_RE = re.compile(r"^MatchResult_.*\.log$", re.IGNORECASE)

# On first attach, read the whole file from the start when it is smaller than this (a current match
# session is only a few MB), so a mid-match capture restart re-captures the round's joins; a bigger
# relic (an old 100MB+ session) gets seek-to-END to skip its history. (owner 2026-07-03)
_FIRST_ATTACH_FULL_READ_BYTES = 50 * 1024 * 1024  # 50 MB
# While catching up on an existing log, a read that returns less than this is the tail of what was on
# disk at attach time (fh.read() hands back everything available at once; the live stream then
# arrives in small appends).
_CATCH_UP_CHUNK_MIN = 64 * 1024

# Entity team from an in-game entity id (verified: entityId >> 24, the order teams joined).
_TEAM_SHIFT = 24

# Weapon ids that mean "grenade" in OnGameAchievementNotify. Evidence (2026-09-22, six matches):
# every kill with weapon 601 was preceded within 6 s by the killer's GRENADE counter dropping (5 of
# 5); no gun id ever was. Extend here when another throwable id is proven the same way.
GRENADE_WEAPON_IDS = frozenset({601})

# A knock older than this is not credited as an assist when the victim finally dies: the knocked
# player was rescued in between (no token for that) and died later to someone else.
_ASSIST_WINDOW_SECONDS = 90.0


def _team_of(entity_id: int) -> int:
    """The ENTITY team an entity belongs to. Verified rule: ``entityId >> 24``."""
    return entity_id >> _TEAM_SHIFT


# --------------------------------------------------------------------------- #
# NO SCORING IN THIS CLIENT (owner, 2026-09-22).
# --------------------------------------------------------------------------- #
# "its not the capture that runs any calculation its the events and leaderboard models that do all of
# that and all overlays pick from there."
#
# So this client OBSERVES and nothing more: it counts what the debugger stream shows, and it reports
# who is still alive and the order teams were wiped in. Placement, position and every point column
# belong to the backend, which computes them in
# afc_tournament_and_scrims/live_ranking.py:normalize_live_standings from the event's own scoring
# config (match.scoring_settings, else the group Leaderboard row) through scoring.compute_team_points
# - the same function the official MatchResult upload scores with. One implementation of the rule,
# server side, so the live board and the official export can never disagree.
#
# A placement table baked into this installer would be a copy nobody could correct without shipping a
# new build, and it would silently diverge the moment an organizer customized scoring. There is none.


def parse_match_result_teams(text: str) -> dict:
    """``{team_name: [uid, ...]}`` from the text of a MatchResult_*.log (the Tier-1 file). The same
    tokens the backend's TEAM_BLOCK_RE / PLAYER_RE and roster.parse_match_result read; kept here so
    LiveMatchState.learn_match_result needs nothing but the text."""
    teams: dict = {}
    current: Optional[str] = None
    for line in (text or "").splitlines():
        m = _RE_MR_TEAM.search(line)
        if m:
            current = m.group(1).strip()
            teams.setdefault(current, [])
            continue
        m = _RE_MR_PLAYER.search(line)
        if m and current is not None:
            teams[current].append(m.group(1).strip())
    return teams


# --------------------------------------------------------------------------- #
# Per-player accumulator (one per entity id, reset each match).
# --------------------------------------------------------------------------- #
def _new_player(name: str = "", uid: str = "") -> dict:
    """The per-player stat dict a snapshot row's ``players`` entry is built from. Every key is
    present from the first sighting so a row never has a missing column."""
    return {
        "uid": uid,
        "name": name,
        "kills": 0,
        "deaths": 0,
        "knockdowns": 0,
        "knocked": 0,
        "headshots": 0,
        "assists": 0,
        "revives": 0,
        "grenades_used": 0,
        "grenade_kills": 0,
        "gloowall_used": 0,
        "medkit_used": 0,
        "weapons": {},          # weapon id -> kills with it (most_used_weapon = its mode)
        "alive": True,
        "_last_death": None,    # timestamp of the last death (survival time), never emitted
    }


# --------------------------------------------------------------------------- #
# TeamResolver - names an ENTITY team from four sources (owner 2026-09-22).
# --------------------------------------------------------------------------- #
class TeamResolver:
    """Session-long memory of ``uid -> (team name, source)`` plus the per-match bridge, answering
    "what is entity team N called" for LiveMatchState.snapshot.

    Sources, by priority:
      roster    : the AFC expected roster (set_roster), team names as AFC spells them; one rostered
                  member names the whole team
      bridge    : the in-log bridge learned in THIS match (learn_bridge)
      memory    : a MatchResult of an earlier map this session (learn_match_result), or a bridge
                  learned in an earlier map, both written to the UID memory so the next map knows
                  the team at the join. A remembered name only counts if it is one of THIS match's
                  game team names (OnTeamScoreInited): players who moved to a differently named
                  team between maps must not drag the old name along (seen 2026-09-22, map 2)
      exclusion : every other entity team of this match is named and exactly one game team name is
                  unclaimed -> it is this one
    The UID memory survives reset() (it is per session); the bridge and the game's TeamID -> name
    table are per match and are cleared by reset().
    """

    def __init__(self) -> None:
        self.uid_team: dict = {}        # uid -> (team name, source)
        self.reset()

    def reset(self) -> None:
        self.tid_name: dict = {}        # score-space TeamID -> game team name (OnTeamScoreInited)
        self.bridge_votes: dict = {}    # entity team -> Counter(score-space TeamID -> frames that said so)

    @property
    def bridge(self) -> dict:
        """``{entity team: TeamID}`` by MAJORITY of the single-kill frames seen this match. A single
        frame can lie (a rank-point bump for another team landing in the same frame as a kill), the
        majority did not once over 24 matches; the latest frame alone swapped two teams (19 Sep, map 3)."""
        return {t: max(c, key=lambda k: (c[k], -k)) for t, c in self.bridge_votes.items() if c}

    # ---- feeding ---- #
    def set_roster(self, team_by_uid) -> None:
        """Install the AFC roster (``{uid: team_name}``). Roster names win over every other source."""
        for uid, name in (team_by_uid or {}).items():
            u = str(uid).strip()
            if u and name:
                self.uid_team[u] = (str(name).strip(), "roster")

    def learn_match_result(self, teams: dict) -> int:
        """Remember ``{team_name: [uids]}`` from a finished map so the next map names those teams the
        moment their players join. Never overrides a roster name. Returns how many UIDs were added."""
        added = 0
        for name, uids in (teams or {}).items():
            for uid in uids:
                u = str(uid).strip()
                if not u or not name:
                    continue
                if self.uid_team.get(u, ("", ""))[1] == "roster":
                    continue
                self.uid_team[u] = (str(name).strip(), "memory")
                added += 1
        return added

    def learn_bridge(self, entity_team: int, tid: int, member_uids) -> None:
        """A single-kill frame says entity team -> TeamID: one vote. When the majority answer for the
        team has a name, write it back to every member UID we know (memory), so the next map names
        the team at the join; a later majority change rewrites it."""
        votes = self.bridge_votes.setdefault(entity_team, Counter())
        votes[tid] += 1
        winner = self.bridge.get(entity_team)
        name = self.tid_name.get(winner) if winner is not None else None
        if not name:
            return
        for uid in member_uids:
            if uid and self.uid_team.get(uid, ("", ""))[1] != "roster":
                self.uid_team[uid] = (name, "memory")

    # ---- answering ---- #
    def resolve(self, entity_team: int, member_uids) -> tuple:
        """``(name, source)`` for one entity team, or ``("", "")`` when nothing names it yet:
        roster votes, then this match's bridge, then memory votes limited to this match's names."""
        roster_votes: Counter = Counter()
        memory_votes: Counter = Counter()
        for uid in member_uids:
            hit = self.uid_team.get(uid)
            if not hit:
                continue
            (roster_votes if hit[1] == "roster" else memory_votes)[hit[0]] += 1
        if roster_votes:
            return max(roster_votes, key=lambda n: (roster_votes[n], n)), "roster"
        tid = self.bridge.get(entity_team)
        if tid is not None and tid in self.tid_name:
            return self.tid_name[tid], "bridge"
        if memory_votes:
            if self.tid_name:
                current = {_norm_name(n) for n in self.tid_name.values()}
                memory_votes = Counter({n: c for n, c in memory_votes.items() if _norm_name(n) in current})
            if memory_votes:
                return max(memory_votes, key=lambda n: (memory_votes[n], n)), "memory"
        return "", ""

    def resolve_all(self, members_by_team: dict) -> dict:
        """``{entity_team: (name, source)}`` for every entity team of the match, with EXCLUSION
        applied: when exactly one team is unnamed and exactly one game team name (OnTeamScoreInited)
        is unclaimed by the named ones, the unnamed team takes it. Unnamed teams answer ("", "")."""
        out = {t: self.resolve(t, uids) for t, uids in members_by_team.items()}
        unnamed = [t for t, (n, _) in out.items() if not n]
        if len(unnamed) == 1 and self.tid_name:
            claimed = {_norm_name(n) for n, _ in out.values() if n}
            free = [n for n in self.tid_name.values() if _norm_name(n) not in claimed]
            if len(free) == 1:
                out[unnamed[0]] = (free[0], "exclusion")
        return out


# --------------------------------------------------------------------------- #
# LiveMatchState - pure, stdlib-only, no threads/IO (so tests feed it directly).
# --------------------------------------------------------------------------- #
class LiveMatchState:
    """Accumulates per-TEAM and per-PLAYER live standings for the current match from debugger lines.

    Feed it one debugger line at a time via ``feed_line`` and read the current
    standings with ``snapshot``. It is intentionally free of threading and file IO
    - LiveTailer owns those and calls feed_line/snapshot under a lock, and the unit
    test drives it directly with scripted lines.

    Match segmentation: accumulators RESET on ``EventTypeEnterGame`` (new round) and
    FREEZE on the match-end token (the authoritative Tier-1 MatchResult supersedes
    live), so a snapshot taken between rounds still reflects the round just played
    until the next EnterGame clears it. The TeamResolver's UID memory is the one thing
    that survives a reset (it is per session, by design).

    WHAT IT DOES NOT DO (owner 2026-09-22): it does not rank the teams and it does not score them.
    A per-TEAM ALIVE set and the order teams were wiped in are OBSERVATIONS and are reported as such
    (``eliminated`` / ``elimination_order`` / ``alive_count`` / ``roster_size``); the backend turns
    them into a placement and into points from the event's own scoring config. The game's own
    cumulative TeamScore is never reported as this match's score either: it is the room's running
    total across maps (measured over 24 matches, 2026-09-22).
    """

    def __init__(self, resolver: Optional[TeamResolver] = None) -> None:
        # Team naming memory (per session): shared with the controller so a MatchResult learned by
        # Tier 1 names the next map's teams here.
        self.resolver = resolver if resolver is not None else TeamResolver()
        # Finished maps' rich-stats payloads by game match id (survive reset(): the upload of a map's
        # MatchResult file happens after the next map may already have started).
        self.finished: dict = {}
        # Finished maps' kill logs by game match id (inbox #376): every kill and knock with UIDs and game
        # time, for the site's kill feed (AFC-B kill_feed.py). Same life as `finished`: last 8 maps.
        self.finished_kills: dict = {}
        # Finished maps' joined players by game match id (inbox #509 #510): [(uid, in-game name, entity team)] from
        # Player Join, for result_repair.repair_match_result (the game's file can lose account ids after a
        # reconnect). Same life as `finished`: last 8 maps.
        self.finished_players: dict = {}
        # The tailer's clock: the timestamp of the most recently seen line. Survives
        # reset() so survival-time for a still-alive team can grow across the match.
        self._last_ts: Optional[datetime] = None
        # Reconnect (inbox #507 #508). backfill_source(match_id) -> the lines of that match from EARLIER debugger
        # logs, oldest first (LiveTailer._earlier_lines); used when a rejoin arrives and this state does not hold
        # the match (AFC Capture was restarted too). _rejoin_id: the match a ``rejoin last battle`` line named,
        # waiting for its EventTypeEnterGame.
        self.backfill_source: Optional[Callable[[str], list]] = None
        self._rejoin_id: Optional[str] = None
        self._backfilling = False
        self.reset(match_start=None)

    # ---- lifecycle ---- #
    def reset(self, match_start: Optional[datetime]) -> None:
        """Clear all per-match accumulators for a fresh round (on EventTypeEnterGame)."""
        self.resolver.reset()
        self.players: dict = {}                    # eid -> _new_player() dict
        self.entity_team: dict = {}                # eid -> entity team (eid >> 24)
        self.team_score: dict = {}                 # score-space TeamID -> latest cumulative TeamScore
        # Per-team stat counters (offensive stats credited to the acting ENTITY team).
        self.kills: dict = {}
        self.deaths: dict = {}
        self.knockdowns: dict = {}
        self.headshots: dict = {}
        self.revives_received: dict = {}
        self.gloowall_used: dict = {}
        self.medkit_used: dict = {}
        self.weapon_counts: dict = {}              # team -> {weapon_id: n}
        self.last_death_ts: dict = {}              # team -> last death time
        self._item_count: dict = {}                # (eid, kind) -> last count
        self._last_knock: dict = {}                # victim eid -> (knocker eid, ts) for knock assists
        # The map's kill log (inbox #376): [{t, kind, killer, victim, headshot, weapon}] in entity ids,
        # turned into UIDs by kill_events_payload(). _pending_headshot holds a gun-trace flag seen before
        # its knock or kill line (a direct kill prints the trace first).
        self.kill_log: list = []
        self._pending_headshot: dict = {}
        # inbox #512: the game's own damage figure per eid, from the replay (LiveTailer._poll_replay, replay.GameDamage).
        # Empty when no replay was read: the upload then carries no damage key and the site keeps what it has.
        self.damage_by_eid: dict = {}
        # inbox #397: eids that left the game for good ("Player quit revive"), and the time of the latest
        # kill / knock / trace / respawn / quit line: the site's survival time for a player who never
        # died runs to that line (game_files.game_stats end_t), not to the last line of any kind.
        self.out_for_good: set = set()
        self._last_event_ts: Optional[datetime] = None
        self.teams: set = set()                    # every ENTITY team seen this match
        # Live placement (complaint E): alive[team] = eids still in the game; roster[team] = every eid
        # that ever joined it. A player leaves alive ONLY on ``Player X Dead`` (a finished kill) or
        # ``Team X eliminated``, never on a knockdown, and comes back on ``Revive Player`` (a respawn).
        # When a team's alive set empties it is WIPED and its slot in eliminated_order is LOCKED.
        self.alive: dict = {}
        self.roster: dict = {}
        self.eliminated_order: dict = {}           # team -> 1-based order it was wiped (locked)
        self._elim_counter: int = 0
        # Frame bookkeeping for the bridge: the kills and the TeamIDs whose score rose in the frame
        # (same ``[seq]``) being read; evaluated when the seq changes (see _end_frame).
        self._cur_seq: Optional[str] = None
        self._frame_kills: list = []
        self._frame_rose: list = []
        self.match_start: Optional[datetime] = match_start
        self.match_id: Optional[str] = None
        # The match being played, from the first mid-match line that names it (or the rejoin line).
        self.live_match_id: Optional[str] = None
        # Rejoin window (inbox #508): the eids that re-joined after a reconnect, until the first kill / knock /
        # respawn / quit line or 10 s; then everyone still counted alive who did NOT re-join went out while the
        # game was down, and a team with nobody back is out (during_reconnect).
        self._rejoin_joins: Optional[set] = None
        self._rejoin_ts: Optional[datetime] = None
        # Instant replay (6 Nov 2026): in_replay between the two InstantReplay lines, where nothing is counted.
        # _event_seq is every event line counted this match, in order, as (kind, ids...) (see _event_sig). The
        # re-print guard (_reprint_at, a cursor into it) is armed whenever the client re-enters the match: back from
        # a replay, a rejoin after a crash, a Player Join for someone who already joined. A re-print of the match
        # matches _event_seq event for event and is skipped; the first event that does not match is new and disarms
        # it. While armed, headshot traces and item counters are HELD (_held: their parsed values, in order) until the
        # next event decides: a match applies nothing (they were the re-print's), a new event or the match end
        # applies them (they were live).
        self.in_replay = False
        self._event_seq: list = []
        self._reprint_at: Optional[int] = None
        self._held: list = []
        self.frozen = False                        # True after match-end token
        # True while the lines being fed are HISTORY (a log re-read from its start on attach), cleared
        # by the next EventTypeEnterGame. A match that ended before this tailer was watching is never
        # pushed: after a group change the restart replayed the previous group's last map and pushed
        # it under the NEW group's key until the next map began (owner 2026-09-22, inbox #37).
        self.historical = False
        # Wrong-event guard (complaint F): the observed (uid, IGN) pairs and game team names of this
        # match, compared against the expected roster by LiveTailer once per match.
        self.observed_players: dict = {}           # eid -> (uid or None, name)
        self.observed_team_names: set = set()
        self._mismatch_warned = False

    # ---- ingest one line ---- #
    def feed_line(self, line: str, historical: bool = False) -> None:
        """Parse a single debugger line and update the accumulators. ``historical`` marks a line
        re-read from the log's past (see ``self.historical``); it still feeds identity and the team
        memory, but a match that ends while historical is never pushed."""
        self._feeding_history = historical   # read by _eliminate: a re-read wipe is not announced
        ts = self._parse_ts(line)
        if ts is not None:
            self._last_ts = ts
        seq_m = _RE_SEQ.match(line)
        seq = seq_m.group(1) if seq_m else None
        if seq != self._cur_seq:
            self._end_frame()
            self._cur_seq = seq

        # --- reconnect into the same match (inbox #507 #508) ---------------- #
        m = _RE_REJOIN.search(line)
        if m:
            self._rejoin_id = m.group(1)
            return

        # --- match segmentation (reset / freeze / match id) --------------- #
        if _RE_ENTER.search(line):
            rejoin, self._rejoin_id = self._rejoin_id, None
            if rejoin and not self._holds_match(rejoin) and not self._backfilling:
                self._backfill(rejoin)
                self._feeding_history = historical
            if rejoin and self._holds_match(rejoin):
                # Same match after a client restart: keep every count, re-read who is still in it.
                self._end_frame()
                self._cur_seq = seq
                self.live_match_id = rejoin
                # A match seen live stays live: the new session's log is first read as catch-up history.
                self.historical = self.historical and historical
                self._rejoin_joins = set()
                self._rejoin_ts = ts or self._last_ts
                self.in_replay = False           # a client that died inside a replay comes back live
                self._arm_reprint_guard()
                if not historical:
                    emit("match.resumed", match_id=rejoin, t=self._rel_t(ts))
                return
            self.reset(match_start=ts or self._last_ts)
            # The EnterGame line names its match (OB55: "match_id" in its JSON); older fixtures carry none.
            named = _RE_ANY_MATCH_ID.search(line)
            self.live_match_id = rejoin or (named.group(1) if named else None)
            self._cur_seq = seq
            self.historical = historical
            if not historical:
                emit("match.start")
            return
        if historical and not self.historical and self.match_start is None:
            self.historical = True
        if _RE_MATCH_END.search(line):
            if self._reprint_at is not None:
                self._release_held(apply=self._reprint_at == 0)
            self._end_frame()
            self.frozen = True
            if not historical:
                emit("match.frozen", teams=len(self.teams), players=len(self.players))
            return
        m = _RE_END_SEG.search(line)
        if m:
            self.match_id = m.group(1)
            self._end_frame()
            if self.frozen:
                # The game writes CompleteGame AND LeaveGame for the same match: the second refreshes what is kept,
                # only the first announces the end (the 9 Nov walk showed "Match finished" twice, two maps counted).
                first_end = self.match_id not in self.finished
                # Keep this map's per-player stats under the game's match id: the Tier-1 upload of
                # MatchResult_<this id>_*.log attaches them (watcher.extra_fields_for). Last 8 maps.
                self.finished[self.match_id] = self.rich_stats_payload()
                for old in list(self.finished)[:-8]:
                    self.finished.pop(old, None)
                self.finished_kills[self.match_id] = self.kill_events_payload()
                for old in list(self.finished_kills)[:-8]:
                    self.finished_kills.pop(old, None)
                self.finished_players[self.match_id] = [(p["uid"], p["name"], _team_of(eid))
                                                        for eid, p in self.players.items() if p["uid"]]
                for old in list(self.finished_players)[:-8]:
                    self.finished_players.pop(old, None)
                if not historical and first_end:
                    emit("match.end", match_id=self.match_id, kills=sum(1 for e in self.finished_kills[self.match_id] if e["kind"] == "kill"),
                         knocks=sum(1 for e in self.finished_kills[self.match_id] if e["kind"] == "knock"),
                         players=len(self.finished[self.match_id]["players"]))
            return

        # After a match ends, ignore stat lines until the next EnterGame resets us.
        if self.frozen:
            return

        # --- instant replay: the match re-printed, never counted twice (6 Nov 2026) --- #
        if _RE_REPLAY_START.search(line):
            if self.match_start is not None:
                self.in_replay = True
                if not historical:
                    emit("replay.start", t=self._rel_t(ts))
            return
        if _RE_REPLAY_END.search(line):
            if self.in_replay:
                self.in_replay = False
                self._arm_reprint_guard()        # in case the way back re-prints the match again
                if not historical:
                    emit("replay.end", t=self._rel_t(ts))
            return
        if self.in_replay:
            return

        m = _RE_ANY_MATCH_ID.search(line)
        if m and self.match_start is not None and self.live_match_id is None:
            self.live_match_id = m.group(1)

        # The rejoin window closes on the first event line or 10 s after the reconnect.
        if self._rejoin_joins is not None and (
                (ts is not None and self._rejoin_ts is not None and (ts - self._rejoin_ts).total_seconds() > 10)
                or _RE_KILL.search(line) or _RE_KNOCK.search(line) or _RE_REVIVE.search(line) or _RE_OUT.search(line)):
            self._close_rejoin()

        # --- re-print guard (6 Nov 2026): skip a re-print of what was already counted --- #
        sig = self._event_sig(line)
        if self._reprint_at is not None:
            if sig is not None:
                if self._reprint_at < len(self._event_seq) and self._event_seq[self._reprint_at] == sig:
                    self._reprint_at += 1
                    self._release_held(apply=False)      # the re-print's traces and counters
                    if self._reprint_at >= len(self._event_seq):
                        self._reprint_at = None          # the re-print has caught up: live from here
                    return
                # A new event: live again. What was held was live too (a direct kill prints its trace first).
                self._release_held(apply=self._reprint_at == 0)
                self._reprint_at = None
            elif _RE_GUN_TRACE.search(line) or _RE_ITEM.search(line) or _RE_PENDING.search(line):
                self._held.append(line)
                return
            elif _RE_TEAM_SCORE.search(line):
                return       # cumulative and re-sent in full on the next burst; only the bridge reads it
        if sig is not None:
            self._event_seq.append(sig)

        # --- identity: Player Join carries the account UID + exact IGN --- #
        m = _RE_JOIN.search(line)
        if m:
            uid, eid, name = m.group(1), int(m.group(2)), m.group(3).strip()
            p = self._player(eid)
            p["uid"] = uid
            p["name"] = name or p["name"]
            self.observed_players[eid] = (uid, p["name"])
            if self._rejoin_joins is not None:
                self._rejoin_joins.add(eid)
            elif eid in self.roster.get(_team_of(eid), ()):
                # Joined before and no rejoin window open: the client re-entered the match (a re-print may follow).
                # Not a respawn either: a dead player comes back only on ``Revive Player``.
                self._arm_reprint_guard()
                return
            self._join(eid)
            if not historical:
                emit("player.join", eid=eid, name=p["name"], uid=uid, team=_team_of(eid), players=len(self.observed_players))
            return

        # --- roster fallback (AddPlayer: name only, mangles some glyphs) --- #
        m = _RE_ADD_PLAYER.search(line)
        if m:
            eid = int(m.group(1))
            p = self._player(eid)
            if not p["name"]:
                p["name"] = m.group(2).strip()
            if eid not in self.observed_players:
                self.observed_players[eid] = (p["uid"] or None, p["name"])
            self._join(eid)
            return

        # --- game team name -> score-space TeamID ------------------------ #
        m = _RE_TEAM_NAME.search(line)
        if m:
            name = m.group(1).strip()
            self.resolver.tid_name[int(m.group(2))] = name
            self.observed_team_names.add(name)
            return

        # --- cumulative team score burst (only a RISE matters, for the bridge) --- #
        # The first burst of a match arrives WITH its first kill and has no baseline to rise from, so
        # that kill teaches nothing; every later single-kill frame does.
        m = _RE_TEAM_SCORE.search(line)
        if m:
            tid, score = int(m.group(1)), int(m.group(2))
            if score > self.team_score.get(tid, score):
                self._frame_rose.append(tid)
            self.team_score[tid] = score
            return

        # --- kills / deaths ---------------------------------------------- #
        m = _RE_KILL.search(line)
        if m:
            victim, killer = int(m.group(1)), int(m.group(2))
            kteam, vteam = _team_of(killer), _team_of(victim)
            self._note_event(ts)
            # inbox #397: the site's credit rule. A kill goes to the player the log names, EXCEPT when
            # that player had already left for good (a bleed-out after the knocker quit): nobody's kill.
            credited = killer != victim and killer not in self.out_for_good
            if credited:
                self._log_kill("kill", killer, victim, ts)
            elif killer != victim:
                self._announce("bleedout", killer, victim, ts, historical)
            v = self._player(victim)
            v["deaths"] += 1
            v["alive"] = False
            v["_last_death"] = ts
            self.deaths[vteam] = self.deaths.get(vteam, 0) + 1
            self.teams.add(vteam)
            if ts is not None:
                self.last_death_ts[vteam] = ts
            if credited and kteam != vteam:
                k = self._player(killer)
                k["kills"] += 1
                self.kills[kteam] = self.kills.get(kteam, 0) + 1
                self.teams.add(kteam)
                self._frame_kills.append((victim, killer))
                self._announce("kill", killer, victim, ts, historical)
                # Knock assist: someone ELSE (an enemy of the victim) knocked them shortly before.
                knock = self._last_knock.pop(victim, None)
                if knock is not None:
                    knocker, kts = knock
                    recent = ts is None or kts is None or (ts - kts).total_seconds() <= _ASSIST_WINDOW_SECONDS
                    if recent and knocker != killer and _team_of(knocker) != vteam:
                        self._player(knocker)["assists"] += 1
            else:
                self._last_knock.pop(victim, None)
            # A FINISHED kill: the victim is out (until a respawn puts them back).
            self._mark_dead(vteam, victim)
            return

        # --- knockdowns ---------------------------------------------------- #
        m = _RE_KNOCK.search(line)
        if m:
            victim, knocker = int(m.group(1)), int(m.group(2))
            kteam = _team_of(knocker)
            self._note_event(ts)
            if knocker != victim:
                self._log_kill("knock", knocker, victim, ts)
                self._announce("knock", knocker, victim, ts, historical)
                # the site counts a knock only when the knocker is somebody else (game_files.game_stats)
                self._player(knocker)["knockdowns"] += 1
                self.knockdowns[kteam] = self.knockdowns.get(kteam, 0) + 1
            self._player(victim)["knocked"] += 1
            self.teams.update((kteam, _team_of(victim)))
            if _team_of(victim) != kteam:
                self._last_knock[victim] = (knocker, ts)
            return

        # --- headshot flag (on knocks and on direct kills) ---------------- #
        m = _RE_GUN_TRACE.search(line)
        if m:
            self._note_event(ts)
            self._on_trace(m)
            return

        # --- weapon per kill (partial coverage) --------------------------- #
        m = _RE_WEAPON.search(line)
        if m:
            weapon = int(m.group(3))
            if weapon >= 0:
                self._log_weapon(int(m.group(1)), int(m.group(2)), weapon)
                if not historical:
                    emit("kill.detail", killer=self._name(int(m.group(1))), victim=self._name(int(m.group(2))), weapon=weapon,
                         weapon_name=weapon_label(weapon))
            if weapon >= 0:   # negative weapon id = zone/fall/bleed, not a weapon
                killer = int(m.group(1))
                kteam = _team_of(killer)
                k = self._player(killer)
                k["weapons"][weapon] = k["weapons"].get(weapon, 0) + 1
                if weapon in GRENADE_WEAPON_IDS:
                    k["grenade_kills"] += 1
                bucket = self.weapon_counts.setdefault(kteam, {})
                bucket[weapon] = bucket.get(weapon, 0) + 1
                self.teams.add(kteam)
            return

        # --- respawn after death ------------------------------------------ #
        m = _RE_REVIVE.search(line)
        if m:
            target = int(m.group(1))
            tteam = _team_of(target)
            self._note_event(ts)
            p = self._player(target)
            p["revives"] += 1
            if not historical:
                emit("revive", player=self._name(target), t=self._rel_t(ts))
            self.revives_received[tteam] = self.revives_received.get(tteam, 0) + 1
            self.teams.add(tteam)
            # Back in the game: restore to the alive set unless the team is already locked out.
            if tteam not in self.eliminated_order:
                p["alive"] = True
                self.roster.setdefault(tteam, set()).add(target)
                self.alive.setdefault(tteam, set()).add(target)
            return

        # --- left the game for good (inbox #397) --------------------------- #
        m = _RE_OUT.search(line)
        if m:
            self._note_event(ts)
            self.out_for_good.add(int(m.group(1)))
            if not historical:
                emit("left", player=self._name(int(m.group(1))), t=self._rel_t(ts))
            return

        # --- dead, waiting for a respawn (6 Nov 2026) ----------------------- #
        m = _RE_PENDING.search(line)
        if m:
            self._on_pending(int(m.group(1)))
            return

        # --- item counters: gloowall / medkit / grenades used ------------- #
        m = _RE_ITEM.search(line)
        if m:
            self._on_item(m)
            return

        # --- team eliminated (entity-team space; survival anchor + wipe backstop) --- #
        m = _RE_TEAM_ELIM.search(line)
        if m:
            tid = int(m.group(1))
            if ts is not None:
                self.last_death_ts[tid] = ts
            self.teams.add(tid)
            self._eliminate(tid)
            return

    # ---- read current standings ---- #
    @property
    def pushable(self) -> bool:
        """False for a match this tailer only ever saw as history (ended before it attached): its
        rows must not reach the overlay. True for a match seen live, ended or not, and for a
        historical match that is STILL RUNNING (a capture restarted mid-map catches up and pushes)."""
        return not (self.historical and self.frozen)

    def snapshot(self) -> list:
        """Build the current OBSERVATION rows - what this client saw, never a ranking or a score.

        Row keys mirror the OFFICIAL overlay feed rows (_overlay_standings_rows in
        afc_tournament_and_scrims/views.py) so the FE DesignBoard renders live and official
        identically, PLUS the rich live-only stat keys (spec §12), PLUS the round facts the backend
        ranks from (eliminated / elimination_order / alive_count / roster_size), PLUS the 2026-09-22
        additions: team naming provenance (``team_name_source``, ``team_name_pending``),
        grenades_used / grenade_kills / assists / knocked, and ``players`` (per-player stats).

        NO pos, NO placement, NO point columns (owner 2026-09-22). The rows go out in a stable order
        - by this match's kills, then players still standing, then name - purely so two pushes of the
        same state look the same in a log; the board's real order and every point column are decided
        by afc_tournament_and_scrims/live_ranking.py on the server, from the event's scoring config.
        """
        names = self.team_names()

        def _stable_key(tid):
            name, _src = names.get(tid, ("", ""))
            return (-self.kills.get(tid, 0), -len(self.alive.get(tid, ())),
                    name or "Team {}".format(tid))

        rows: list = []
        for tid in sorted(self.teams, key=_stable_key):
            kills = self.kills.get(tid, 0)
            elim_order = self.eliminated_order.get(tid)
            name, source = names.get(tid, ("", ""))
            members = sorted(e for e, t in self.entity_team.items() if t == tid)
            players = [self._player_row(e) for e in members]
            rows.append({
                # ── keys shared with the official feed (so both render the same) ──
                "team_name": name or "Team {}".format(tid),
                "team_name_source": source,          # roster | bridge | memory | exclusion | ""
                "team_name_pending": not name,       # True until one of the four sources names it
                # Logo isn't resolvable client-side (we don't know AFC URLs); it stays
                # blank in live mode unless the backend push endpoint joins it by name.
                "team_logo": None,
                "kills": kills,
                # ── the round facts the SERVER ranks from ──
                "eliminated": elim_order is not None,
                "elimination_order": elim_order,
                "alive_count": len(self.alive.get(tid, ())),
                "roster_size": len(self.roster.get(tid, ())),
                # ── rich LIVE-ONLY stats (spec §12; 0/blank in the official feed) ──
                "deaths": self.deaths.get(tid, 0),
                "knockdowns": self.knockdowns.get(tid, 0),
                "knocked": sum(p["knocked"] for p in players),
                "headshots": self.headshots.get(tid, 0),
                "assists": sum(p["assists"] for p in players),
                "revives_received": self.revives_received.get(tid, 0),
                "revives": self.revives_received.get(tid, 0),
                "gloowall_used": self.gloowall_used.get(tid, 0),
                "medkit_used": self.medkit_used.get(tid, 0),
                "grenades_used": sum(p["grenades_used"] for p in players),
                "grenade_kills": sum(p["grenade_kills"] for p in players),
                "survival_time": self._survival_seconds(tid),
                "most_used_weapon": self._most_used_weapon(tid),
                # ── per-player stats (owner 2026-09-22) ──
                "players": players,
            })
        return rows

    def dashboard_board(self) -> list:
        """What the dashboard needs to draw each team (owner 2 Nov 2026, inbox #461 #462 #464): its entity id
        (the key the dashboard's team cards use), the resolved name, whether and in what order it was wiped,
        and each player's counters. The same facts the push sends the website, plus the ids; nothing ranked."""
        names = self.team_names()
        board = []
        for tid in sorted(self.teams):
            name, _src = names.get(tid, ("", ""))
            members = sorted(e for e, t in self.entity_team.items() if t == tid)
            players = []
            for eid in members:
                row = self._player_row(eid)
                # "alive": the tailer's own word, so the dashboard also knows who went out with no kill line to
                # say so (out while the game was reconnecting, inbox #510; the 9 Nov walk read "Alive 8").
                players.append({"eid": eid, "name": row["name"], "alive": bool(self.players[eid]["alive"]),
                                "kills": row.get("kills", 0),
                                "knockdowns": row.get("knockdowns", 0), "headshots": row.get("headshots", 0),
                                "grenades_used": row.get("grenades_used", 0),
                                "grenade_kills": row.get("grenade_kills", 0),
                                "gloowall_used": row.get("gloowall_used", 0),
                                "medkit_used": row.get("medkit_used", 0),
                                "weapon": weapon_label(row["most_used_weapon"]) if row.get("most_used_weapon") else None})
            board.append({"team": tid, "name": name or "Team {}".format(tid), "named": bool(name),
                          "eliminated": tid in self.eliminated_order, "order": self.eliminated_order.get(tid),
                          "alive": len(self.alive.get(tid, ())), "players": players})
        return board

    def team_names(self) -> dict:
        """``{entity_team: (name, source)}`` for every team of this match, via the resolver (UID
        votes, this match's bridge, then exclusion)."""
        members_by_team: dict = {}
        for eid, t in self.entity_team.items():
            members_by_team.setdefault(t, []).append(self.players[eid]["uid"])
        for t in self.teams:
            members_by_team.setdefault(t, [])
        return self.resolver.resolve_all(members_by_team)

    def attribution(self) -> tuple:
        """``(named, total, pending_names)``: how many entity teams have a name, out of all teams
        seen, and the placeholder labels of the ones still waiting (for the tray status line)."""
        names = self.team_names()
        total = len(names)
        pending = sorted("Team {}".format(t) for t, (n, _) in names.items() if not n)
        return total - len(pending), total, pending

    # ---- wrong-event guard (complaint F) ---- #
    def mismatch_report(self, expected):
        """Compare THIS match's observed players (UID + IGN, from Player Join) and game team names
        to the expected roster and return a roster.MismatchReport. A player whose UID is on the
        roster is expected whatever their IGN; a player seen only through AddPlayer (no UID yet)
        is matched by name. ``expected`` is a roster.ExpectedRoster."""
        players = list(self.observed_players.values())
        return expected.check(players=players, team_names=self.observed_team_names)

    # ---- learning from the previous map ---- #
    def learn_match_result(self, text: str) -> int:
        """Feed the text of a finished map's MatchResult so its team -> UID pairs name the next
        map's teams at the join. Returns the number of UIDs learned."""
        return self.resolver.learn_match_result(parse_match_result_teams(text))

    # ---- internals ---- #
    def _player(self, eid: int) -> dict:
        p = self.players.get(eid)
        if p is None:
            p = self.players[eid] = _new_player()
            self.entity_team[eid] = _team_of(eid)
            self.teams.add(_team_of(eid))
        return p

    def _join(self, eid: int) -> None:
        """A joining player starts ALIVE and counts toward its team's roster size."""
        tid = _team_of(eid)
        self.entity_team[eid] = tid
        self.teams.add(tid)
        self.roster.setdefault(tid, set()).add(eid)
        self.alive.setdefault(tid, set()).add(eid)

    def _player_row(self, eid: int) -> dict:
        p = self.players[eid]
        weapons = p["weapons"]
        most = str(max(weapons, key=lambda w: (weapons[w], -w))) if weapons else ""
        row = {k: v for k, v in p.items() if k != "weapons" and not k.startswith("_")}
        row["most_used_weapon"] = most
        row["name"] = p["name"] or str(eid)
        row["survival_seconds"] = self._player_survival_seconds(p)
        return row

    def _player_survival_seconds(self, p: dict) -> int:
        """Seconds this player was in the game: to the last death for a dead player, to the latest
        line (the match end, once frozen) for one still alive. 0 without a match-start anchor."""
        if self.match_start is None:
            return 0
        end = self._last_ts if p["alive"] or p["_last_death"] is None else p["_last_death"]
        if end is None:
            return 0
        delta = (end - self.match_start).total_seconds()
        return int(delta) if delta > 0 else 0

    # ---- dashboard feed (live view) ---- #
    def _note_event(self, ts) -> None:
        """Remember the time of the latest kill / knock / trace / respawn / quit line (see reset())."""
        if ts is not None:
            self._last_event_ts = ts

    def _name(self, eid: int) -> dict:
        """Who an entity id is, for the dashboard: name, UID and team (best name the resolver has now)."""
        p = self.players.get(eid) or {}
        tid = _team_of(eid)
        try:
            team_name = self.team_names().get(tid, ("", ""))[0]
        except Exception:  # pragma: no cover - naming is decoration here
            team_name = ""
        return {"eid": eid, "name": p.get("name") or str(eid), "uid": p.get("uid") or "", "team": tid,
                "team_name": team_name or "Team {}".format(tid)}

    def _rel_t(self, ts) -> float:
        """Game seconds since the match started (0 when either time is unknown)."""
        if ts is None or self.match_start is None:
            return 0.0
        return round(max(0.0, (ts - self.match_start).total_seconds()), 1)

    def _announce(self, kind: str, killer: int, victim: int, ts, historical: bool) -> None:
        """Tell the dashboard a kill / knock / bleed-out just landed. A line re-read from history is not
        announced (it would flood the live feed after a restart). Never raises."""
        if historical:
            return
        try:
            t = self._rel_t(ts)
            emit(kind, t=t, killer=self._name(killer), victim=self._name(victim),
                 alive_left=sum(len(s) for s in self.alive.values()))
        except Exception:  # pragma: no cover
            pass

    # ---- kill log (inbox #376) ---- #
    def _log_kill(self, kind: str, killer: int, victim: int, ts) -> None:
        t = 0.0
        if ts is not None and self.match_start is not None:
            t = round(max(0.0, (ts - self.match_start).total_seconds()), 3)
        headshot = self._pending_headshot.pop((killer, victim), None)
        self.kill_log.append({"t": t, "kind": kind, "killer": killer, "victim": victim,
                              "headshot": headshot, "weapon": None})

    def _log_headshot(self, killer: int, victim: int, headshot: bool) -> None:
        # the trace follows a knock line, or comes before a direct kill's line
        if self.kill_log:
            last = self.kill_log[-1]
            if last["killer"] == killer and last["victim"] == victim and last["headshot"] is None:
                last["headshot"] = headshot
                return
        self._pending_headshot[(killer, victim)] = headshot

    def _log_weapon(self, killer: int, victim: int, weapon: int) -> None:
        for ev in reversed(self.kill_log):
            if ev["kind"] == "kill" and ev["killer"] == killer and ev["victim"] == victim:
                if ev["weapon"] is None:
                    ev["weapon"] = weapon
                return

    def apply_replay_damage(self, by_eid: dict, match_id: str = "") -> None:
        """#512: the game's damage figure per eid (replay.GameDamage, the later file's figure over the earlier) for the
        match ``match_id`` the replay belongs to. If that map has already ended, the stats its upload carries get it too
        (the replay can trail the end line). Picked by the replay's match id, never self.match_id: that still names the
        previous map until this one's end line."""
        self.damage_by_eid = dict(by_eid)
        stored = self.finished.get(match_id) if match_id else None
        if stored is not None:
            for eid, value in self.damage_by_eid.items():
                p = self.players.get(eid)
                if p and p["uid"] in stored["players"]:
                    stored["players"][p["uid"]]["damage"] = int(value)

    def apply_replay_guns(self, deaths: list, knocks: list, announce: bool = True) -> int:
        """#506: give the kill log's kills and knocks the gun the replay names (replay.KillGuns: lists of (killer,
        victim, gun) in the game's order). The nth kill of a (killer, victim) pair in the log is the nth death of that
        pair in the replay, the same for knocks. A gun the log already named stays. A map that has ended gets its
        stored kill events rebuilt, so the upload carries the guns. Returns how many were filled by this call."""
        queues: dict = {}
        for kind, rows in (("kill", deaths), ("knock", knocks)):
            for killer, victim, gun in rows:
                queues.setdefault((kind, killer, victim), []).append(gun)
        seen: Counter = Counter()
        filled = 0
        for ev in self.kill_log:
            key = (ev["kind"], ev["killer"], ev["victim"])
            n = seen[key]
            seen[key] += 1
            guns = queues.get(key) or []
            if ev["weapon"] is None and n < len(guns) and guns[n]:
                ev["weapon"] = guns[n]
                filled += 1
                if announce:
                    emit("kill.detail", of=ev["kind"], killer=self._name(ev["killer"]),
                         victim=self._name(ev["victim"]), weapon=guns[n], weapon_name=weapon_label(guns[n]))
        if filled and self.frozen and self.match_id and self.match_id in self.finished_kills:
            self.finished_kills[self.match_id] = self.kill_events_payload()
        return filled

    def kill_events_payload(self) -> list:
        """The map's kills and knocks in the shape the site stores (AFC-B kill_feed.clean_events):
        [{t, kind, killer_uid, victim_uid, headshot, weapon}]. An event whose player never joined
        through Player Join (no UID) is left out: the site keys a player by UID only."""
        out = []
        for ev in self.kill_log:
            killer_uid = self.players.get(ev["killer"], {}).get("uid")
            victim_uid = self.players.get(ev["victim"], {}).get("uid")
            if not killer_uid or not victim_uid:
                continue
            out.append({"t": ev["t"], "kind": ev["kind"], "killer_uid": killer_uid,
                        "victim_uid": victim_uid, "headshot": ev["headshot"], "weapon": ev["weapon"]})
        return out

    def rich_stats_payload(self) -> dict:
        """The per-player stats of the current match in the shape the site stores
        (backend capture_rich_stats.py): players keyed by Free Fire UID, teams by name. Players with
        no UID (never joined through Player Join) are left out: the site matches by UID only."""
        names = self.team_names()
        players: dict = {}
        teams: dict = {}
        for eid, p in self.players.items():
            if not p["uid"]:
                continue
            row = self._player_row(eid)
            row["survival_seconds"] = self._game_survival_seconds(p)
            players[p["uid"]] = {k: row[k] for k in (
                "deaths", "knockdowns", "knocked", "headshots", "assists", "revives", "grenades_used",
                "grenade_kills", "gloowall_used", "medkit_used", "most_used_weapon", "survival_seconds")}
            if eid in self.damage_by_eid:
                players[p["uid"]]["damage"] = int(self.damage_by_eid[eid])
        for tid in self.teams:
            name = names.get(tid, ("", ""))[0]
            if name:
                teams[name] = {"kills": self.kills.get(tid, 0), "survival_seconds": int(self._survival_seconds(tid))}
        return {"ff_match_id": self.match_id or "", "players": players, "teams": teams}

    def _game_survival_seconds(self, p: dict) -> int:
        """Survival the way the site's game files reader counts it (game_files.game_stats): to the
        player's LAST death, or, for a player who never died, to the latest kill / knock / trace /
        respawn / quit line. Used for the rich_stats a finished map uploads, so a Capture upload and a
        game files upload of the same map store the same number."""
        if self.match_start is None:
            return 0
        end = p["_last_death"] if p["_last_death"] is not None else self._last_event_ts
        if end is None:
            return 0
        delta = (end - self.match_start).total_seconds()
        return int(delta) if delta > 0 else 0

    def _end_frame(self) -> None:
        """The bridge: a frame with exactly one enemy kill and exactly one TeamScore that rose ties the
        killer's ENTITY team to that score-space TeamID (verified 264 of 264, 0 wrong). Any frame with
        two kills, or a rank-point bump alongside, is ambiguous and skipped."""
        if len(self._frame_kills) == 1 and len(self._frame_rose) == 1:
            killer = self._frame_kills[0][1]
            team = _team_of(killer)
            tid = self._frame_rose[0]
            uids = [self.players[e]["uid"] for e, t in self.entity_team.items() if t == team]
            self.resolver.learn_bridge(team, tid, uids)
        self._frame_kills = []
        self._frame_rose = []

    def _mark_dead(self, team: int, entity: int) -> None:
        """One player of ``team`` is fully dead: drop it from the alive set. A team we saw join whose
        alive set is now empty is WIPED -> lock its elimination order. Called ONLY on the finished
        kill token, never on a knockdown (a knocked player is still revivable)."""
        s = self.alive.get(team)
        if s is not None:
            s.discard(entity)
        if team in self.roster and not self.alive.get(team) and team not in self.eliminated_order:
            self._eliminate(team)

    # ---- reconnect (inbox #507 #508) ---- #
    def _holds_match(self, match_id: str) -> bool:
        """True when this state is the unfinished match ``match_id`` (or an unfinished match that has not
        named itself yet: a crash in the first minutes, before any line carries the id)."""
        return (self.match_start is not None and not self.frozen
                and self.live_match_id in (None, match_id))

    def _backfill(self, match_id: str) -> None:
        """Read the match's earlier part back from the previous debugger log(s): AFC Capture was not holding
        it (started or restarted after the game began). Fed as history: counted, never announced."""
        if self.backfill_source is None:
            return
        try:
            lines = self.backfill_source(match_id) or []
        except Exception as exc:  # pragma: no cover - a missing log must never stop the live read
            log.warning("Reconnect: could not read the earlier part of match %s: %s", match_id, exc)
            return
        if not lines:
            log.info("Reconnect: no earlier log holds match %s; counting from the reconnect", match_id)
            return
        self._backfilling = True
        try:
            for ln in lines:
                self.feed_line(ln, historical=True)
            self._end_frame()
        finally:
            self._backfilling = False
        log.info("Reconnect: read %d earlier line(s) of match %s back from the previous log", len(lines), match_id)

    def _on_trace(self, m) -> None:
        """A headshot trace (``_RE_GUN_TRACE`` match): flags the knock or kill it belongs to, counts a headshot."""
        self._log_headshot(int(m.group(1)), int(m.group(2)), m.group(3) == "True")
        if m.group(3) == "True":
            killer = int(m.group(1))
            kteam = _team_of(killer)
            self._player(killer)["headshots"] += 1
            self.headshots[kteam] = self.headshots.get(kteam, 0) + 1
            self.teams.add(kteam)

    def _on_item(self, m) -> None:
        """An item counter (``_RE_ITEM`` match): a decrease is that many used / thrown."""
        eid, kind, count = int(m.group(1)), m.group(2), int(m.group(3))
        tteam = _team_of(eid)
        key = (eid, kind)
        prev = self._item_count.get(key)
        if prev is not None and count < prev:
            used = prev - count
            p = self._player(eid)
            if kind == "BUILDING":
                p["gloowall_used"] += used
                self.gloowall_used[tteam] = self.gloowall_used.get(tteam, 0) + used
            elif kind == "MEDKIT":
                p["medkit_used"] += used
                self.medkit_used[tteam] = self.medkit_used.get(tteam, 0) + used
            else:  # GRENADE, GRENADE_FROZEN, GRENADE_MULTIEXPLOSIVE, GRENADE_DEICEWALL, ...
                p["grenades_used"] += used
        self._item_count[key] = count
        self.teams.add(tteam)

    def _on_pending(self, eid: int) -> None:
        """``eid`` is dead and waits for a respawn: not alive, not back from a rejoin; the team is out if nobody
        else is left (``Revive Player`` brings them back)."""
        if eid in self.players:
            self.players[eid]["alive"] = False
        if self._rejoin_joins is not None:
            self._rejoin_joins.discard(eid)
        self._mark_dead(_team_of(eid), eid)

    def _release_held(self, apply: bool) -> None:
        """Apply (they were live) or drop (they were a re-print's) the traces and counters held while armed."""
        held, self._held = self._held, []
        if not apply:
            return
        for ln in held:
            m = _RE_GUN_TRACE.search(ln)
            if m:
                self._on_trace(m)
                continue
            m = _RE_ITEM.search(ln)
            if m:
                self._on_item(m)
                continue
            m = _RE_PENDING.search(ln)
            if m:
                self._on_pending(int(m.group(1)))

    def _arm_reprint_guard(self) -> None:
        """Expect a possible re-print of this match from its first event (see reset()). Already armed: keep the
        cursor where it is (a Player Join burst arms it once)."""
        if self._event_seq and self._reprint_at is None:
            self._reprint_at = 0
            self._held = []

    @staticmethod
    def _event_sig(line: str) -> Optional[tuple]:
        """The identity of an event line, for the re-print guard: kills, knocks, respawns, quits, wipes and gun lines
        re-print in exactly the original order (measured 56 of 56 on 6 Nov 2026); headshot traces do not (27 vs 22)
        and are not part of it."""
        for kind, rx in (("kill", _RE_KILL), ("knock", _RE_KNOCK), ("revive", _RE_REVIVE), ("out", _RE_OUT),
                         ("wiped", _RE_TEAM_ELIM), ("weapon", _RE_WEAPON)):
            m = rx.search(line)
            if m:
                return (kind,) + m.groups()
        return None

    def _close_rejoin(self) -> None:
        """End of the rejoin burst: whoever was counted alive and did not come back went out while the game was
        down; a team with nobody back is out (order: after every team already out, the game does not say)."""
        back, self._rejoin_joins = self._rejoin_joins, None
        if not back:
            return   # no Player Join burst was seen: change nothing rather than wipe everyone
        gone_teams = []
        for team in sorted(self.alive):
            if team in self.eliminated_order:
                continue
            members = self.alive[team]
            still = {e for e in members if e in back}
            for e in members - still:
                if e in self.players:
                    self.players[e]["alive"] = False
            self.alive[team] = still
            if members and not still:
                gone_teams.append(team)
        for team in gone_teams:
            self._eliminate(team, during_reconnect=True)
        if gone_teams:
            log.info("Reconnect: %d team(s) went out while the game was down", len(gone_teams))

    def _eliminate(self, team: int, during_reconnect: bool = False) -> None:
        """Lock a team's ELIMINATION ORDER (idempotent - the first call wins). The 1st team wiped gets
        order 1, the next 2, ... ; the SERVER turns order o into placement ``N - o + 1``
        (live_ranking.rank_rows). This client only records the order it saw."""
        if team in self.eliminated_order:
            return
        self._elim_counter += 1
        self.eliminated_order[team] = self._elim_counter
        if not getattr(self, "_feeding_history", False):
            # The dashboard's "team out" line (owner 2 Nov 2026, inbox #462 #464). The ORDER it went out is an
            # observation; the place it finishes is decided on the server (live_ranking.rank_rows).
            try:
                name = self.team_names().get(team, ("", ""))[0] or "Team {}".format(team)
                emit("team.wiped", team=team, team_name=name, order=self._elim_counter, t=self._rel_t(self._last_ts),
                     during_reconnect=during_reconnect)
            except Exception:  # pragma: no cover - the dashboard is decoration, capture goes on
                pass
        self.alive[team] = set()
        for eid, t in self.entity_team.items():
            if t == team:
                self.players[eid]["alive"] = False

    # NOTE (owner 2026-09-22): the placement rule that used to live here - a wiped team locked at
    # N - elimination_order + 1, the living taking the top slots by this match's kills - now lives in
    # afc_tournament_and_scrims/live_ranking.py:rank_rows, on the server, beside the scoring it feeds.
    # This client reports the facts it saw (eliminated / elimination_order / alive_count) and stops.

    def _survival_seconds(self, tid: int) -> float:
        """Seconds the team survived: last-member death time - match start. For a still-alive team the
        latest line time (survived at least this long so far). 0 with no match-start anchor."""
        if self.match_start is None:
            return 0.0
        end = self.last_death_ts.get(tid) or self._last_ts
        if end is None:
            return 0.0
        delta = (end - self.match_start).total_seconds()
        return round(delta, 3) if delta > 0 else 0.0

    def _most_used_weapon(self, tid: int) -> str:
        """The team's most-used weapon id (mode of weapon ids among its kills that carried one), as a
        string; "" when none. Ties break to the smallest id. Partial by nature: only ~60% of kills
        carry a weapon id."""
        counts = self.weapon_counts.get(tid)
        if not counts:
            return ""
        weapon = max(counts, key=lambda w: (counts[w], -w))
        return str(weapon)

    @staticmethod
    def _parse_ts(line: str) -> Optional[datetime]:
        """Parse the ``[YYYY-MM-DD HH:MM:SS.mmm]`` prefix into a naive datetime, or None if the line
        has no timestamp prefix (naive is fine - we only ever take differences within one log)."""
        m = _RE_TS.match(line)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Debugger-log discovery (privacy-gated to debugger-*.log only).
# --------------------------------------------------------------------------- #
def match_segment(path: str, match_id: str, newest_older: bool) -> tuple:
    """The lines of match ``match_id`` in one older debugger log, and whether they include its real start.

    Returns (lines, has_start) or (None, False) when the log holds nothing of it. The segment runs from the
    match's EventTypeEnterGame (or, in a log that itself began with a rejoin, from that ``rejoin last battle``
    line) to the next EnterGame or the end of the log. A crash in the first minutes can leave no line naming the
    match; then the newest older log's last match is taken when it never ended (no match-end token after it)."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        lines = [ln.rstrip("\r") for ln in fh.read().split("\n") if ln.strip()]
    enters = [i for i, ln in enumerate(lines) if _RE_ENTER.search(ln)]
    rejoin = next((i for i, ln in enumerate(lines) if ("rejoin last battle " + match_id) in ln), None)
    mention = next((i for i, ln in enumerate(lines) if match_id in ln), None)
    if mention is None:
        if not newest_older or not enters:
            return None, False
        start = enters[-1]
        if any(_RE_MATCH_END.search(ln) for ln in lines[start:]):
            return None, False
        return lines[start:], True
    before = [i for i in enters if i <= mention]
    if rejoin is not None and (not before or rejoin <= before[-1]) and not [i for i in enters if i < rejoin]:
        start, has_start = rejoin, False
    elif before:
        start, has_start = before[-1], True
    else:
        start, has_start = 0, False
    # The segment ends at the next EnterGame of ANOTHER match. An EnterGame naming this match is a rejoin's
    # own (the log began with ``rejoin last battle``, or the client crashed again inside this log), and one
    # naming no match right after the rejoin line is that rejoin's too.
    rejoin_enter = next((i for i in enters if i > start), None) if start == rejoin else None

    def _other_match(i: int) -> bool:
        named = _RE_ANY_MATCH_ID.search(lines[i])
        if named:
            return named.group(1) != match_id
        return i != rejoin_enter

    after = [i for i in enters if i > start and _other_match(i)]
    end = after[0] if after else len(lines)
    return lines[start:end], has_start


def find_newest_debugger_log(folder: str) -> Optional[str]:
    """Return the path of the most recently modified ``debugger-*.log`` in ``folder``,
    or None if there is none / the folder is unreadable.

    Privacy: only files whose basename matches ``debugger-*.log`` are considered - the
    plaintext-credential ``GuestCacheProduction.json`` and everything else are skipped
    and never opened (spec §7)."""
    try:
        best: Optional[tuple] = None
        for name in os.listdir(folder):
            if not _DEBUGGER_RE.match(name):
                continue
            path = os.path.join(folder, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, path)
        return best[1] if best else None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# LiveTailer - follows the debugger log + pushes snapshots on a timer.
# --------------------------------------------------------------------------- #
# Callback fired after each push so the tray can show last-push status:
#   (UploadResult, row_count)
PushCallback = Callable[[object, int], None]


class LiveTailer:
    """Follows the newest debugger log and pushes live standings every ``push_interval``.

    Threading model (mirrors watcher.MatchResultWatcher):
      - a FOLLOW thread tails the newest debugger-*.log (seek-to-end, poll appended
        bytes; switches to a newer file if one appears) and feeds each line into a
        shared LiveMatchState under a lock,
      - a PUSH thread snapshots that state every ``push_interval`` seconds and calls
        the injected ``pusher.push(rows, updated_at)``.
    Keeping the push off the follow thread means a slow/retrying push never stalls
    log reading, and vice-versa.

    ``pusher`` is duck-typed (anything with ``push(rows: list, updated_at: str)``),
    which is why this module needs no ``requests`` import - app.py injects an
    uploader.LivePusher; tests can inject a fake.

    TEAM NAMING WIRING: ``expected`` (a roster.ExpectedRoster) seeds the resolver with the AFC
    roster's ``team_by_uid`` so every team whose player is on the roster is named at the join;
    the controller calls ``learn_match_result(path)`` after each Tier-1 upload so a map's result
    names the next map's teams; ``attribution_text()`` is the one-line status for the tray.

    NO SCORING WIRING (owner 2026-09-22): there is nothing to inject. The snapshot carries counts
    and round facts; the backend's live_ranking module ranks and scores them from the event's own
    config on every push.
    """

    def __init__(
        self,
        debugger_folder: str,
        pusher,
        *,
        push_interval: float = 2.0,
        poll_interval: float = 0.5,
        on_push: Optional[PushCallback] = None,
        expected=None,
        on_mismatch: Optional[Callable] = None,
    ):
        self.debugger_folder = debugger_folder
        self.pusher = pusher
        self.push_interval = max(0.5, float(push_interval))
        self.poll_interval = poll_interval
        self.on_push = on_push
        # Complaint F (wrong-event guard, LIVE side): the push loop checks the live board's observed
        # players (UID + IGN) and team names against the expected roster and fires on_mismatch at most
        # once per match. NON-BLOCKING - it runs AFTER the snapshot has already been pushed.
        self.expected = expected
        self.on_mismatch = on_mismatch

        self.state = LiveMatchState()
        self.state.backfill_source = self._earlier_lines
        team_by_uid = getattr(expected, "team_by_uid", None)
        if team_by_uid:
            self.state.resolver.set_roster(team_by_uid)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._follow_thread: Optional[threading.Thread] = None
        self._push_thread: Optional[threading.Thread] = None
        self._current_path: Optional[str] = None
        # #506: the replay of the match being played (Replays\ReplayInfo_<match id>_*.bin, one per client session):
        # a reader per file and the guns read so far, for state.live_match_id only.
        self._replay_match: Optional[str] = None
        self._replay_readers: dict = {}
        self._guns = KillGuns()
        self._damage: dict = {}                    # replay file name -> GameDamage (ids are bound per file, #512)
        self._replay_polled = 0.0

    # ---- lifecycle ---- #
    def start(self) -> None:
        """Begin tailing + pushing. Raises ValueError if the folder is missing so the
        tray can surface a clear error instead of silently doing nothing."""
        if not self.debugger_folder or not os.path.isdir(self.debugger_folder):
            raise ValueError(f"Debugger folder does not exist: {self.debugger_folder!r}")
        self._stop.clear()
        self._follow_thread = threading.Thread(
            target=self._run_follow, name="afc-capture-tailer", daemon=True
        )
        self._push_thread = threading.Thread(
            target=self._run_push, name="afc-capture-livepush", daemon=True
        )
        self._follow_thread.start()
        self._push_thread.start()
        log.info(
            "Live tailer started on %s (push every %.1fs).",
            self.debugger_folder, self.push_interval,
        )

    def stop(self) -> None:
        """Stop both threads (best-effort, bounded joins)."""
        self._stop.set()
        for t in (self._push_thread, self._follow_thread):
            if t is not None:
                t.join(timeout=5)
        self._push_thread = None
        self._follow_thread = None
        log.info("Live tailer stopped.")

    @property
    def is_running(self) -> bool:
        return self._follow_thread is not None and self._follow_thread.is_alive()

    @property
    def current_log(self) -> Optional[str]:
        return self._current_path

    # ---- team naming helpers for the controller ---- #
    def learn_match_result(self, path: str) -> int:
        """Read a just-uploaded ``MatchResult_*.log`` (the only other file kind this app opens) and
        remember its team -> UID pairs so the next map's teams are named at the join. Returns the
        number of UIDs learned; 0 (never raises) on any problem."""
        try:
            if not _MATCH_RESULT_RE.match(os.path.basename(path)):
                return 0
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            log.warning("Could not read %s for team naming: %s", path, exc)
            return 0
        with self._lock:
            n = self.state.learn_match_result(text)
        if n:
            log.info("Team naming: learned %d player(s) from %s", n, os.path.basename(path))
        return n

    def kill_events_for(self, match_id: str):
        """JSON of the finished map ``match_id``'s kill log, or None if this tailer never saw that map
        end. Handed to the watcher (app.py) for the map's upload as `kill_events` (inbox #376)."""
        import json
        with self._lock:
            payload = self.state.finished_kills.get(str(match_id))
        return json.dumps(payload, ensure_ascii=False) if payload else None

    def players_for(self, match_id: str) -> list:
        """[(uid, in-game name, entity team)] of everyone who joined the finished map ``match_id`` ([] if this
        tailer never saw it end). Handed to the watcher (app.py) to repair the map's MatchResult before upload."""
        with self._lock:
            return list(self.state.finished_players.get(str(match_id)) or [])

    def rich_stats_for(self, match_id: str):
        """JSON of the finished map ``match_id``'s per-player stats, or None if this tailer never saw
        that map end. Handed to the watcher (app.py) for the map's upload."""
        import json
        with self._lock:
            payload = self.state.finished.get(str(match_id))
        return json.dumps(payload, ensure_ascii=False) if payload else None

    def dashboard_board(self) -> list:
        """The live board for the dashboard (LiveMatchState.dashboard_board), read under the lock."""
        with self._lock:
            return self.state.dashboard_board()

    def attribution_text(self) -> str:
        """One plain line for the tray: how many teams are named and what the rest are waiting for."""
        with self._lock:
            named, total, pending = self.state.attribution()
        if total == 0:
            return "no match in progress"
        if not pending:
            return "{} of {} teams named".format(named, total)
        return "{} of {} teams named; {} waiting for a first kill or the map result".format(
            named, total, ", ".join(pending))

    # ---- the gun of every kill and knock (inbox #506) ---- #
    def _replays_folder(self) -> str:
        """``Free Fire_64_Data\\Replays``: the Debugger folder's sibling."""
        return os.path.join(os.path.dirname(os.path.abspath(self.debugger_folder)), "Replays")

    def _poll_replay(self, announce: bool = True, every: float = 1.0) -> int:
        """Read what the game appended to the current match's replay file(s) and fill the kill log's guns. At most
        once a ``every`` seconds; never raises (the guns are a detail, the counts go on without them)."""
        now = time.monotonic()
        if now - self._replay_polled < every:
            return 0
        self._replay_polled = now
        try:
            with self._lock:
                mid = self.state.live_match_id or self.state.match_id
            if not mid:
                return 0
            if mid != self._replay_match:
                self._replay_match, self._replay_readers, self._guns, self._damage = mid, {}, KillGuns(), {}
            folder = self._replays_folder()
            try:
                names = sorted(n for n in os.listdir(folder)
                               if n.startswith("ReplayInfo_%s_" % mid) and n.endswith(".bin"))
            except OSError:
                return 0
            added = damaged = False
            for name in names:       # oldest session first: the name carries the start time
                reader = self._replay_readers.setdefault(name, ReplayReader(os.path.join(folder, name)))
                records = reader.read_new()
                added |= self._guns.feed(records)
                damaged |= self._damage.setdefault(name, GameDamage()).feed(records)
            if damaged:
                merged: dict = {}
                for name in names:   # the later session's figure is the newer one
                    merged.update(self._damage[name].damage)
                with self._lock:
                    self.state.apply_replay_damage(merged, mid)
            if not added:
                return 0
            with self._lock:
                return self.state.apply_replay_guns(self._guns.deaths, self._guns.knocks, announce=announce)
        except Exception:  # pragma: no cover - never let the replay stop the live read
            log.exception("Reading the replay for guns failed (ignored)")
            return 0

    # ---- reconnect backfill (inbox #508) ---- #
    def _earlier_lines(self, match_id: str) -> list:
        """Every line of match ``match_id`` in the debugger logs OLDER than the one being tailed, oldest first:
        walks back at most four sessions and stops at the log that holds the match's real start."""
        try:
            names = sorted(n for n in os.listdir(self.debugger_folder) if _DEBUGGER_RE.match(n))
        except OSError:
            return []
        cur = os.path.basename(self._current_path or "")
        older = [n for n in names if n < cur] if cur else names[:-1]
        segments: list = []
        for i, name in enumerate(reversed(older[-4:])):
            seg, has_start = match_segment(os.path.join(self.debugger_folder, name), match_id, newest_older=(i == 0))
            if seg is None:
                break
            segments.insert(0, seg)
            if has_start:
                break
        return [ln for seg in segments for ln in seg]

    # ---- follow thread ---- #
    def _run_follow(self) -> None:
        """Tail the newest debugger log, switching if a newer one appears."""
        fh = None
        buf = ""
        catching_up = False
        # First attach = the log that already existed when capture started: seek to END so we skip
        # its (possibly huge, 100MB+) pre-capture history. Every NEW file that appears AFTER is a
        # fresh SESSION whose whole content is live-relevant - CRITICALLY the round-start burst
        # (EventTypeEnterGame + the Player Join lines that map each player to a team) sits ~4000 lines
        # in. The old code seek-END'd every switch, so on a new file it skipped that burst and had no
        # player->team mapping -> collection stalled (owner 2026-07-03, live). So: first file -> END
        # (unless small); subsequent new files -> read from the START.
        first_attach = True
        try:
            while not self._stop.is_set():
                newest = find_newest_debugger_log(self.debugger_folder)
                # Switch to a newer session's log (or wait if there is none yet).
                if newest != self._current_path:
                    if fh is not None:
                        try:
                            fh.close()
                        except OSError:
                            pass
                        fh = None
                    buf = ""
                    if newest is not None:
                        try:
                            # utf-8-sig strips the BOM; errors=replace so a byte split
                            # at a chunk boundary can never crash the tailer.
                            fh = open(newest, "r", encoding="utf-8-sig", errors="replace")
                            if first_attach:
                                # Startup: read from the START if this log is SMALL (a current/recent
                                # match, so restarting the capture mid-match re-captures the round's
                                # joins), but seek to END for a huge relic (an old 100MB+ session) to
                                # avoid a long re-read. New logs (below) always read from start.
                                try:
                                    small = os.path.getsize(newest) < _FIRST_ATTACH_FULL_READ_BYTES
                                except OSError:
                                    small = False
                                if not small:
                                    fh.seek(0, os.SEEK_END)
                                first_attach = False
                            # Reading from the START (a small first log, or any new log) replays what
                            # is already on disk: history, never pushed if its last match had ended.
                            catching_up = fh.tell() == 0 and os.path.getsize(newest) > 0
                            self._current_path = newest
                            log.info("Live: tailing %s (from %s)", os.path.basename(newest),
                                     "end" if fh.tell() else "start")
                        except OSError as exc:
                            log.warning("Could not open debugger log %s: %s", newest, exc)
                            fh = None
                            self._current_path = None
                    else:
                        self._current_path = None

                if fh is None:
                    self._stop.wait(1.0)   # no log yet - wait for the game to write one
                    continue

                # Read whatever has been appended since last poll.
                try:
                    chunk = fh.read()
                except (OSError, ValueError):
                    # File went away / handle invalidated - drop it and re-discover.
                    try:
                        fh.close()
                    except OSError:
                        pass
                    fh = None
                    self._current_path = None
                    continue

                if chunk:
                    buf += chunk
                    parts = buf.split("\n")
                    buf = parts[-1]           # keep the (possibly partial) last line
                    if len(parts) > 1:
                        with self._lock:
                            for raw in parts[:-1]:
                                ln = raw.rstrip("\r")
                                if ln:
                                    self.state.feed_line(ln, historical=catching_up)
                    # A first read from the start of an existing log is HISTORY until it reaches the
                    # end of what was already on disk; everything after that is live.
                    if catching_up and len(chunk) < _CATCH_UP_CHUNK_MIN:
                        catching_up = False
                else:
                    catching_up = False

                self._poll_replay(announce=not catching_up)
                self._stop.wait(self.poll_interval)
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass

    # ---- push thread ---- #
    def _run_push(self) -> None:
        """Every ``push_interval`` seconds, snapshot the state and push it."""
        while not self._stop.wait(self.push_interval):
            with self._lock:
                rows = self.state.snapshot() if self.state.pushable else []
            # Complaint F: after each tick, check the live board against the expected roster and warn
            # the operator (once/match) if it looks like the wrong event. Done here, off the follow
            # thread, so a warning never stalls log reading; it runs AFTER the push and never blocks it.
            if self.expected is not None and not getattr(self.expected, "is_empty", True):
                try:
                    self._maybe_warn_mismatch()
                except Exception:  # pragma: no cover - the check must NEVER kill the push thread
                    log.exception("Live roster mismatch check failed (ignored)")
            # Nothing to show yet (no teams) - skip so we don't overwrite a useful Redis snapshot
            # with an empty one; the feed then falls back to official standings (and the Redis TTL
            # clears stale live data on its own).
            if not rows:
                continue
            updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            try:
                result = self.pusher.push(rows, updated_at)
            except Exception:  # pragma: no cover - a pusher must never kill the thread
                log.exception("Live push raised unexpectedly")
                continue
            if self.on_push is not None:
                try:
                    self.on_push(result, len(rows))
                except Exception:  # pragma: no cover - tray callback best-effort
                    pass

    # ---- complaint F: wrong-event guard (live side) ---- #
    def _maybe_warn_mismatch(self) -> None:
        """Compare the live board's observed players (UID + IGN) and team names to the expected roster
        ONCE per match and, above threshold, fire on_mismatch. Never blocks the live push (it runs
        after the push)."""
        with self._lock:
            if self.state.frozen or self.state._mismatch_warned:
                return
            report = self.state.mismatch_report(self.expected)
        if not report.should_warn():
            return
        with self._lock:
            # Re-check under the lock (a reset() could have cleared it) and latch so we warn once/match.
            if self.state._mismatch_warned:
                return
            self.state._mismatch_warned = True
        log.warning("Live roster mismatch: %s", report.summary())
        if self.on_mismatch is not None:
            try:
                self.on_mismatch(report, None)
            except Exception:  # pragma: no cover - tray callback best-effort
                pass
