# ── KILL FEED ON THE SITE: sniper award + team head to head (owner 2026-10-21 / 2026-10-23, inbox #326,
# #350, #376) ──────────────────────────────────────────────────────────────────────────────────────────
# NFL Season 2 promotion asked for "best grenadier and sniper GFX" and "the number of times have met
# themselves and the number of wins each has had against the other (a win would count as what team had
# more players left after the bout)". Both need every kill, not per-player totals. The owner's answers
# (23 Oct, tasks/kill-feed-awards-h2h-brief-2026-10-21.md "Owner's answers"):
#   1. sniper = bolt action only (AWM, M82B, Kar98k, M24): GameWeapon.is_sniper, filled by an admin as the
#      ids are matched to the guns seen on stream (only 601 = grenade is known today);
#   2. count only kills whose weapon the game reported; the wording everywhere is "sniper kills recorded";
#   3. a fight starts with a kill or knock between two teams; a third team that fights either of them
#      while it is still going joins the SAME fight; it ends after FIGHT_GAP_SECONDS of game time with no
#      kill or knock among its teams (the gap was not answered: a default, kept as a setting here);
#   4. players left counts FINISHED kills only, a knock does not lower it;
#   5. Season 1 logs are gone: head to head starts with Season 2;
#   6. an event tab, the overlays and a GFX export, each switched on or off per event
#      (Event.kill_feed_tab / kill_feed_overlay / kill_feed_export, declared in event_contract.py).
#
# WHERE THE EVENTS COME FROM
#   * the debugger-log backfill (debugger_ingest.py): parse_kill_events() reads the same log the
#     backfill reads, one round at a time, and the apply step stores them for the round's match;
#   * AFC Capture, later: an optional `kill_events` field on the map upload, same shape as
#     parse_kill_events() returns (the client change is the next step, see the brief).
# Identity is the Free Fire UID (`Player Join, <UID>, <entity>, <IGN>`), and the team is the event
# roster's team for that UID (TournamentTeamMember), never the game's entity or room numbering.
#
# CONSUMED BY: views_kill_feed.py (GET events/<slug>/kill-stats/), debugger_ingest.py, tests_kill_feed.py.
import re
from collections import defaultdict
from datetime import datetime

FIGHT_GAP_SECONDS = 30.0

# ── Weapon ids (24 Oct 2026, inbox #350) ──────────────────────────────────────────────────────────────
# The debugger log's `weapon:<id>` uses the same numbers as Craftland's ItemGoodsIDType enum. Proven by joining the
# enum (FREEFIRE/CRAFTLAND-RESEARCH/_work/js/D5v4pxjR.js) with the 2,705 weapon ids of 22 debugger logs on the
# observer PC: 601 comes out as Grenade (independently known: every 601 kill followed the killer's grenade counter
# dropping), 41 M1887, 50 MAG7, 9 EAGLE, 18 SKS, 58 UZI, 8 MP5, 15 MP40. Evidence and scripts:
# WEBSITE/tasks/kill-feed-weapon-ids-2026-10-24/. The owner's sniper set is bolt action only (AWM, M82B, Kar98k,
# M24): every enum entry of those, gold variants included. The enum has no "M82B"; XM82 (159) is most likely it,
# so it is listed but NOT counted until someone confirms it on stream and ticks it in /admin/.
# Used two ways: `manage.py seed_game_weapons` fills the GameWeapon table from it (never overwriting an admin's
# edit), and the kill-stats view falls back to it while that table is still empty.
KNOWN_WEAPONS = {
    4: ("AWM", True), 172: ("AWM (variant 2)", True), 65: ("Gold AWM", True),
    21: ("Kar98k", True), 64: ("Gold Kar98k", True), 128: ("Gold Kar98k (2)", True),
    129: ("Gold Kar98k (3)", True), 175: ("Gold Kar98k (3, 2)", True),
    75: ("M24", True),
    159: ("XM82, most likely the M82B: confirm on stream before counting", False),
    26: ("SVD (marksman, not counted)", False), 170: ("SVD (variant 2)", False), 72: ("Gold SVD", False),
    89: ("AC80 (marksman, not counted)", False), 197: ("VSK94 (marksman, not counted)", False),
    18: ("SKS (marksman, not counted)", False), 11: ("M14 (marksman, not counted)", False),
    13: ("VSS (marksman, not counted)", False), 601: ("Grenade", False),
}


def default_sniper_ids():
    return {wid for wid, (_, sniper) in KNOWN_WEAPONS.items() if sniper}

_RE_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]")
_RE_ENTER = re.compile(r"EventTypeEnterGame")
# Same join pattern and round rules as debugger_ingest.parse_debugger_log, so round i here is round i there.
_RE_JOIN = re.compile(r"Player Join, (\d+), (\d+), (.+?),")
_RE_KILL = re.compile(r"Player (\d+) Dead, killed by (\d+)")
_RE_GUN_TRACE = re.compile(r"PlayKnockDownGunTrace killer=(\d+) victim=(\d+) headshot=(True|False)")
_RE_WEAPON = re.compile(r"OnGameAchievementNotify killerId:(\d+) beKilled:(\d+) weapon:(-?\d+)")


def _ts(line):
    m = _RE_TS.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f") if m else None


def parse_kill_events(text):
    """Every round of a debugger log -> a list of rounds, each a list of events in game order:
    {"t": seconds since the round started, "kind": "kill" | "knock", "killer_uid", "victim_uid",
     "headshot": bool | None, "weapon": int | None}. A weapon id below 0 (zone, fall, bleed out) is kept
    as None: it is not a gun. Self kills and players with no UID line are dropped."""
    rounds, start, uid_of, events = [], None, {}, None
    for line in (text or "").splitlines():
        ts = _ts(line)
        if _RE_ENTER.search(line):
            if events is not None and uid_of:   # a round with nobody joined (menu, lobby) is dropped
                rounds.append(events)
            start, uid_of, events = ts, {}, []
            continue
        if events is None:                      # a log that opens mid-round: an implicit round
            start, uid_of, events = ts, {}, []
        t = round((ts - start).total_seconds(), 3) if ts and start else 0.0
        m = _RE_JOIN.search(line)
        if m:
            uid_of[m.group(2)] = m.group(1)
            continue
        m = _RE_GUN_TRACE.search(line)
        if m:
            killer, victim = uid_of.get(m.group(1)), uid_of.get(m.group(2))
            if killer and victim and killer != victim:
                events.append({"t": t, "kind": "knock", "killer_uid": killer, "victim_uid": victim,
                               "headshot": m.group(3) == "True", "weapon": None})
            continue
        m = _RE_KILL.search(line)
        if m:
            victim, killer = uid_of.get(m.group(1)), uid_of.get(m.group(2))
            if killer and victim and killer != victim:
                events.append({"t": t, "kind": "kill", "killer_uid": killer, "victim_uid": victim,
                               "headshot": None, "weapon": None})
            continue
        m = _RE_WEAPON.search(line)
        if m:
            killer, victim, weapon = uid_of.get(m.group(1)), uid_of.get(m.group(2)), int(m.group(3))
            # the weapon line follows its kill line in the same frame: give it to the latest matching kill
            for ev in reversed(events):
                if ev["kind"] == "kill" and ev["killer_uid"] == killer and ev["victim_uid"] == victim:
                    if ev["weapon"] is None and weapon >= 0:
                        ev["weapon"] = weapon
                    break
    if events is not None and uid_of:
        rounds.append(events)
    return rounds


def clean_events(raw):
    """Validate a `kill_events` payload (a list of the dicts parse_kill_events makes). Returns the cleaned
    list or None when the shape is wrong. Never trusts a team: teams come from the roster."""
    if not isinstance(raw, list):
        return None
    out = []
    for ev in raw[:2000]:
        if not isinstance(ev, dict) or ev.get("kind") not in ("kill", "knock"):
            return None
        killer, victim = str(ev.get("killer_uid") or "").strip(), str(ev.get("victim_uid") or "").strip()
        if not killer.isdigit() or not victim.isdigit() or killer == victim:
            continue
        try:
            t = max(0.0, float(ev.get("t") or 0))
        except (TypeError, ValueError):
            return None
        weapon = ev.get("weapon")
        weapon = int(weapon) if isinstance(weapon, int) and weapon >= 0 else None
        headshot = ev.get("headshot")
        out.append({"t": t, "kind": ev["kind"], "killer_uid": killer, "victim_uid": victim,
                    "headshot": headshot if isinstance(headshot, bool) else None, "weapon": weapon})
    return out


def roster_teams(event):
    """UID -> tournament_team_id for this event's roster (numbers only, leading zeros dropped)."""
    from .models import TournamentTeamMember

    out = {}
    for uid, tt in (TournamentTeamMember.objects.filter(tournament_team__event=event)
                    .exclude(user__uid__isnull=True).values_list("user__uid", "tournament_team_id")):
        key = str(uid).strip().lstrip("0")
        if key:
            out[key] = tt
    return out


def store_events(match, event, events, source):
    """Replace this map's kill events with `events` (a re-upload or a second backfill of the same map
    is idempotent). Returns how many were stored."""
    from .models import MatchKillEvent

    teams = roster_teams(event)
    rows = [MatchKillEvent(
        match=match, seq=i, t=ev["t"], kind=ev["kind"],
        killer_uid=ev["killer_uid"], victim_uid=ev["victim_uid"],
        killer_team_id=teams.get(ev["killer_uid"].lstrip("0")),
        victim_team_id=teams.get(ev["victim_uid"].lstrip("0")),
        headshot=ev["headshot"], weapon_id=ev["weapon"], source=source,
    ) for i, ev in enumerate(sorted(events, key=lambda e: e["t"]))]
    MatchKillEvent.objects.filter(match=match).delete()
    MatchKillEvent.objects.bulk_create(rows)
    return len(rows)


# ── fights and head to head ──────────────────────────────────────────────────────────────────────
def fights_of_match(events, team_sizes, gap=FIGHT_GAP_SECONDS):
    """Group one map's events (dicts with t, kind, killer_team, victim_team) into fights.

    team_sizes: {team_id: players who started the map}. Players left = team size minus the team's
    FINISHED deaths so far (a knock lowers nothing). Returns a list of fights:
    {"teams": [ids, in the order they joined], "started_by": [a, b], "start": t, "end": t,
     "pairs": {(a, b): {"winner": id | None}}, "left": {team: players left at the end},
     "events": [the fight's own kills and knocks, in game order]} (events: the event tab lists them,
     inbox #392).
    A pair is two teams that knocked or killed each other inside the fight; its winner is the one with
    more players left when the fight ended (equal = None, a draw)."""
    alive = {team: int(n) for team, n in team_sizes.items()}
    open_fights, done = [], []

    def close(f):
        # never below 0: in modes with a team revive a player can die twice
        f["left"] = {team: max(0, alive.get(team, 0)) for team in f["teams"]}
        for pair in f["pairs"]:
            a, b = pair
            la, lb = f["left"][a], f["left"][b]
            f["pairs"][pair] = {"winner": a if la > lb else b if lb > la else None}
        done.append(f)

    for ev in sorted(events, key=lambda e: e["t"]):
        a, b = ev.get("killer_team"), ev.get("victim_team")
        for f in [f for f in open_fights if ev["t"] - f["end"] > gap]:
            open_fights.remove(f)
            close(f)
        if ev["kind"] == "kill" and b is not None:
            alive[b] = alive.get(b, 0) - 1
        if a is None or b is None or a == b:
            continue
        touching = [f for f in open_fights if a in f["teams"] or b in f["teams"]]
        if touching:
            f = touching[0]
            for other in touching[1:]:   # two fights that meet become one
                for team in other["teams"]:
                    if team not in f["teams"]:
                        f["teams"].append(team)
                f["pairs"].update(other["pairs"])
                f["events"] = sorted(f["events"] + other["events"], key=lambda e: e["t"])
                f["start"] = min(f["start"], other["start"])
                open_fights.remove(other)
            for team in (a, b):
                if team not in f["teams"]:
                    f["teams"].append(team)      # the third party, in the order it came in
        else:
            f = {"teams": [a, b], "started_by": [a, b], "start": ev["t"], "end": ev["t"], "pairs": {},
                 "events": []}
            open_fights.append(f)
        f["end"] = ev["t"]
        f["events"].append(ev)
        f["pairs"].setdefault(tuple(sorted((a, b))), None)
    for f in open_fights:
        close(f)
    return sorted(done, key=lambda f: f["start"])


def head_to_head(fights_by_match):
    """Totals per pair of teams across maps: {(a, b): {"met": n, "wins": {a: n, b: n}, "draws": n}}."""
    totals = {}
    for fights in fights_by_match:
        for f in fights:
            for (a, b), result in f["pairs"].items():
                row = totals.setdefault((a, b), {"met": 0, "wins": {a: 0, b: 0}, "draws": 0})
                row["met"] += 1
                if result["winner"] is None:
                    row["draws"] += 1
                else:
                    row["wins"][result["winner"]] += 1
    return totals


def sniper_counts(events, sniper_ids):
    """Finished kills per killer UID with a weapon in `sniper_ids`. A kill with no weapon is not
    guessed: the count is "sniper kills recorded"."""
    counts = defaultdict(int)
    for ev in events:
        if ev["kind"] == "kill" and ev.get("weapon") in sniper_ids:
            counts[ev["killer_uid"]] += 1
    return dict(counts)
