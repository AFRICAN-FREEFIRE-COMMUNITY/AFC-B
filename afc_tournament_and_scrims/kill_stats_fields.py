# ── KILL STATS AS DESIGN COLUMNS (owner 2026-11-01, inbox #399 / #421a) ────────────────────────────
# The Leaderboard designs editor offers three columns that read the kill events stored per map
# (MatchKillEvent, AFC-B #142 #144), so they work with AFC Capture OR with only a debugger file upload:
#   knocks_recorded  knocks the team's players made (a knock is not a kill; kill_feed.py rule 4)
#   sniper_kills     "sniper kills recorded": finished kills with a bolt-action weapon the game reported
#   h2h_record       the team's head to head record over every team it met, "wins-draws-losses"
#
# EMPTY IS NOT ZERO. A team whose maps in scope carry no kill events (an event run without Capture or a
# debugger upload) gets EMPTY_VALUE, never 0: a 0 would pretend a map was read. sniper_kills is empty as
# well when no kill in the team's covered maps names a weapon. h2h_record is empty when the team met nobody.
#
# SCOPE: the same slice the standings use. Callers pass the Match filter of that slice, for example
# {"group": g}, {"group__stage": s}, {"group__stage__event": e} or {"group_id__in": [...]}.
#
# CONNECTS TO: kill_feed.py (fights, head_to_head, sniper ids: the SAME rules as the Kill stats tab, so a
# design and the tab never disagree); views._overlay_rows_from_standings (overlay feed rows);
# views_event_graphic.event_stage_graphic (PNG export rows); the frontend DesignFieldsEditor palette and
# DesignBoard through lib/leaderboardDesigns.ts. Tests: tests_kill_stats_fields.py.
from . import kill_feed
from .models import GameWeapon, MatchKillEvent, TournamentPlayerMatchStats

# What a design cell shows when there is nothing recorded. A plain hyphen so every font draws it.
EMPTY_VALUE = "-"

PLAYERS_PER_MAP = {"solo": 1, "duo": 2, "squad": 4}

DESIGN_KEYS = ("knocks_recorded", "sniper_kills", "h2h_record")


def _sniper_ids():
    """The admin's GameWeapon table when it has rows, else the proven defaults (same as the tab)."""
    if GameWeapon.objects.exists():
        return set(GameWeapon.objects.filter(is_sniper=True).values_list("weapon_id", flat=True))
    return kill_feed.default_sniper_ids()


def team_kill_fields(event, match_filter):
    """{tournament_team_id: {knocks_recorded, sniper_kills, h2h_record}} for every team with at least
    one map in scope that carries kill events. A team absent from the result has nothing recorded:
    callers fill EMPTY_VALUE (see fill_row)."""
    rows = list(MatchKillEvent.objects.filter(match__in=_matches(event, match_filter))
                .order_by("match_id", "seq")
                .values("match_id", "t", "kind", "killer_uid", "victim_uid",
                        "killer_team_id", "victim_team_id", "weapon_id"))
    if not rows:
        return {}
    by_match = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append({
            "t": r["t"], "kind": r["kind"], "killer_uid": r["killer_uid"], "victim_uid": r["victim_uid"],
            "killer_team": r["killer_team_id"], "victim_team": r["victim_team_id"], "weapon": r["weapon_id"]})

    # Teams that played each covered map: a team on a covered map with no knocks really has 0.
    played = {}
    sizes = {}
    for match_id, team_id in (TournamentPlayerMatchStats.objects
                              .filter(team_stats__match_id__in=list(by_match), played=True)
                              .values_list("team_stats__match_id", "team_stats__tournament_team_id")):
        sizes.setdefault(match_id, {}).setdefault(team_id, 0)
        sizes[match_id][team_id] += 1
        played.setdefault(match_id, set()).add(team_id)

    default_size = PLAYERS_PER_MAP.get((event.participant_type or "").lower(), 4)
    sniper_ids = _sniper_ids()
    out = {}

    def row_of(team_id):
        return out.setdefault(team_id, {"knocks": 0, "sniper": 0, "weapon_seen": False,
                                        "wins": 0, "draws": 0, "losses": 0, "met": 0})

    all_fights = []
    for match_id, events in by_match.items():
        teams = {e["killer_team"] for e in events} | {e["victim_team"] for e in events}
        teams.discard(None)
        for t in played.get(match_id, set()) | teams:
            row_of(t)
        weapon_seen = any(e["weapon"] is not None for e in events if e["kind"] == "kill")
        if weapon_seen:
            for t in played.get(match_id, set()) | teams:
                row_of(t)["weapon_seen"] = True
        for e in events:
            killer = e["killer_team"]
            if killer is None or killer == e["victim_team"]:
                continue
            if e["kind"] == "knock":
                row_of(killer)["knocks"] += 1
            elif e["weapon"] in sniper_ids:
                row_of(killer)["sniper"] += 1
        size = {t: sizes.get(match_id, {}).get(t, default_size) for t in teams}
        all_fights.extend(kill_feed.fights_of_match(events, size))

    for (a, b), row in kill_feed.head_to_head([all_fights]).items():
        for me, other in ((a, b), (b, a)):
            r = row_of(me)
            r["met"] += row["met"]
            r["wins"] += row["wins"][me]
            r["losses"] += row["wins"][other]
            r["draws"] += row["draws"]

    return {t: {
        "knocks_recorded": r["knocks"],
        "sniper_kills": r["sniper"] if r["weapon_seen"] else EMPTY_VALUE,
        "h2h_record": f"{r['wins']}-{r['draws']}-{r['losses']}" if r["met"] else EMPTY_VALUE,
    } for t, r in out.items()}


def _matches(event, match_filter):
    """The Match rows of `event` inside the slice. The event guard keeps a forged group id from
    another event out of the numbers."""
    from .models import Match

    return Match.objects.filter(group__stage__event=event, **match_filter)


def fill_row(row, per_team, team_id):
    """Add the three design keys to one standings row dict, EMPTY_VALUE where nothing is recorded."""
    values = per_team.get(team_id) or {}
    for key in DESIGN_KEYS:
        row[key] = values.get(key, EMPTY_VALUE)
    return row
