"""THE BOARD: the one place every leaderboard reader takes its team numbers from (owner, 11 Nov 2026,
inbox #540).

WHY THIS EXISTS
---------------
Owner, 11 Nov 2026: "afc capture upodates the admin leaderboard section and then every other thing,
including the live overlays and afc live, should pull theri data from what it coimputes and nt be
calculating on theri own ... icluding the mvp, top killers and the rest".

Before this module a group's table was built three ways: the admin leaderboard
(views.get_all_leaderboard_details_for_event: seeded teams + carry-over + tie-breakers + champion pin),
the site overlays (views._overlay_standings_rows: seeded + carry-over, no champion pin) and AFC LIVE
(views._capture_live_standings: final_standings.official_group_standings, no seeded teams). And during a
map the live overlay threw the group's table away and showed ONLY the map being played, with that map's
points, so a team on 133 after five maps read 22 on air; once the map ended AFC Capture kept pushing its
last board and the overlay went on counting it as "live" after the official upload had landed.

WHAT IT GIVES
-------------
group_table(group)       the group's OFFICIAL table: official_group_standings (carry-over, tie-breakers,
                         champion pin) plus every team seeded into the group that has not played yet, at 0.
                         The admin page's table is held to it by tests_board (parity).
live_state(group)        the map in progress, as AFC Capture last pushed it and live_push scored it with the
                         event's own rule, or None when no map is in progress (nothing pushed for 15 s, or
                         the pushed board is the map that has since been uploaded: see LIVE MAP below).
live_group_table(group)  group_table + the map in progress: each team's official numbers, its live map
                         (placement, kills, points, players alive) and the two added together, re-ranked.
                         This is what the site's live overlay, AFC LIVE and the admin Live panel show.

LIVE MAP (which pushed board counts)
------------------------------------
live_push stores, beside the snapshot, a small state per group: how many maps were scored when the map
being pushed began (`scored_at_start`) and the last board's kills and wiped-team count. A board starts a
NEW map when its kills or its wiped teams go DOWN (a fresh map starts from 0). The pushed board counts as
live only while the group's scored-map count is still `scored_at_start`: the moment the map's official
result is uploaded the count rises and the board, which Capture keeps re-sending, stops being added on top
of the official numbers that now contain it.

CONNECTS TO
-----------
- final_standings.official_group_standings (the official table), round_robin rows (the row keys).
- views._overlay_live_key (the cache key Capture's live board is stored under), views.live_push (writer of
  the snapshot and of the live state), views.overlay_feed (site overlays), views._capture_live_standings
  (AFC LIVE through capture/context ?standings=1), views_board.event_board (the admin / organizer page).
- tests_board.py: the parity and live rules.
"""

import re

from django.core.cache import cache

# The live state outlives the 15 s snapshot: it must remember which map was in progress across a pause in
# the pushes (a client restart, a dropped connection). Six hours covers a broadcast day.
LIVE_STATE_TTL = 6 * 60 * 60


def _norm(name):
    """Team names compared the way people read them: case, spaces and punctuation ignored."""
    return re.sub(r"[^0-9a-z]+", "", str(name or "").casefold())


def _int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _num(value):
    """A point value as an int when it is whole, else a float rounded to 2 places."""
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(f) if f == int(f) else round(f, 2)


# ── official table ──────────────────────────────────────────────────────────────────────────────────
def group_table(group):
    """The group's official table (see the module docstring), rows in final order.

    Seeded-but-unplayed teams are appended at 0 exactly the way the admin leaderboard and the overlay
    always did (StageGroupCompetitor of this group), after the played teams, in seeding order."""
    from .final_standings import official_group_standings
    from .models import StageGroupCompetitor

    from .views import _carry_over_for_stage

    rows = [dict(r) for r in official_group_standings(group)]
    present = {r.get("tournament_team_id") for r in rows}
    seeds = (StageGroupCompetitor.objects.filter(stage_group=group, tournament_team__isnull=False)
             .select_related("tournament_team__team", "tournament_team__ghost_team")
             .order_by("pk"))
    # A seeded team that has not played still carries its Point-Rush carry-over (a Finals group before its
    # first map shows every qualifier with the points they bring), exactly as the admin page and the overlay
    # always showed it. official_group_standings only folds carry into teams that have played.
    carry = None
    landed = False
    for sc in seeds:
        if sc.tournament_team_id in present:
            continue
        present.add(sc.tournament_team_id)
        if carry is None:
            carry = _carry_over_for_stage(group.stage, group.stage.event.participant_type)
        bonus = carry.get(sc.tournament_team_id, 0)
        landed = landed or bool(bonus)
        tt = sc.tournament_team
        rows.append({
            "tournament_team_id": sc.tournament_team_id,
            "team_name": tt.display_name if tt else "",
            "team_country": (tt.competitor.country or "") if tt else "",
            "games_played": 0, "total_kills": 0, "total_booyah": 0,
            "placement_sum": 0, "kill_sum": 0, "bonus_sum": 0, "penalty_sum": 0,
            "effective_total": bonus, "carry_over_points": bonus, "is_champion": False,
            "last_match_placement": 999,
        })
    if landed:
        # Same chain as official_group_standings; a crowned champion stays first.
        rows.sort(key=lambda r: (-float(r.get("effective_total") or 0), -int(r.get("total_booyah") or 0),
                                 -int(r.get("total_kills") or 0), int(r.get("last_match_placement") or 999),
                                 r.get("team_name") or ""))
        rows.sort(key=lambda r: 0 if r.get("is_champion") else 1)
    return rows


# ── live state (written by live_push, read here) ────────────────────────────────────────────────────
def live_state_key(event_id, group_id):
    return "overlay:livestate:{}:{}".format(event_id, group_id)


def scored_map_count(group_id):
    """How many of the group's maps hold a result."""
    from .models import Match
    return (Match.objects.filter(group_id=group_id, team_stats__isnull=False)
            .distinct().count())


def note_push(event_id, group_id, rows):
    """Called by live_push for every board it stores: keep track of which map the board belongs to.

    Returns the live state it saved: {scored_at_start, kills, wiped}. See LIVE MAP in the docstring."""
    if not group_id:
        return None
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    kills = sum(_int(r.get("kills")) for r in rows)
    wiped = sum(1 for r in rows if r.get("eliminated"))
    still_playing = sum(1 for r in rows if not r.get("eliminated")) > 1
    scored = scored_map_count(group_id)
    key = live_state_key(event_id, group_id)
    state = cache.get(key)
    new_map = (not isinstance(state, dict)
               or kills < _int(state.get("kills"))
               or wiped < _int(state.get("wiped")))
    if new_map:
        state = {"scored_at_start": scored}
    elif still_playing and scored > _int(state.get("scored_at_start")):
        # A result landed while this map is still being played, so it is the PREVIOUS map's (uploaded
        # late): this map has not been counted yet and stays live.
        state["scored_at_start"] = scored
    state["kills"] = kills
    state["wiped"] = wiped
    cache.set(key, state, timeout=LIVE_STATE_TTL)
    return state


def live_state(group):
    """The map in progress for this group, or None.

    {"rows": the scored live rows as live_push stored them, "map_number": the map being played (scored
    maps + 1)}. None when nothing was pushed in the last 15 s, or when the pushed board belongs to a map
    whose official result has since been uploaded (see LIVE MAP)."""
    from .views import _overlay_live_key

    event_id = group.stage.event_id
    rows = cache.get(_overlay_live_key(event_id, group.stage_id, group.group_id))
    if not isinstance(rows, list):
        # A push that carried no stage is stored under a blank stage segment.
        rows = cache.get(_overlay_live_key(event_id, None, group.group_id))
    if not isinstance(rows, list) or not rows:
        return None
    state = cache.get(live_state_key(event_id, group.group_id))
    scored = scored_map_count(group.group_id)
    if isinstance(state, dict) and _int(state.get("scored_at_start")) != scored:
        return None          # the official result of this map has landed: the board is already counted
    start = _int(state.get("scored_at_start")) if isinstance(state, dict) else scored
    return {"rows": rows, "map_number": start + 1}


# ── official + live ─────────────────────────────────────────────────────────────────────────────────
def _live_players(row):
    out = []
    for p in row.get("players") or []:
        if not isinstance(p, dict):
            continue
        out.append({
            "name": p.get("name") or "",
            "uid": str(p.get("uid") or ""),
            "alive": bool(p.get("alive", True)),
            "kills": _int(p.get("kills")),
            "knockdowns": _int(p.get("knockdowns")),
            "knocked": _int(p.get("knocked")),
            "headshots": _int(p.get("headshots")),
            "assists": _int(p.get("assists")),
            "damage": _int(p.get("damage")),
            "most_used_weapon": p.get("most_used_weapon") or "",
        })
    return out


def _live_part(row):
    """The live map's numbers for one team, as the board shows them."""
    return {
        "placement": _int(row.get("placement")),
        "kills": _int(row.get("kills")),
        "placement_points": _num(row.get("placement_points")),
        "kill_points": _num(row.get("kill_points")),
        "points": _num(row.get("total_points")),
        "alive_count": _int(row.get("alive_count")),
        "roster_size": _int(row.get("roster_size")),
        "eliminated": bool(row.get("eliminated")),
        "elimination_order": row.get("elimination_order"),
        "knockdowns": _int(row.get("knockdowns")),
        "assists": _int(row.get("assists")),
        "headshots": _int(row.get("headshots")),
        "players": _live_players(row),
    }


def board_row(rank, r, live=None):
    """One team of the board, official numbers + live map (None when the team is not in a live map)."""
    total = _num(r.get("effective_total"))
    kills = _int(r.get("total_kills"))
    row = {
        "rank": rank,
        "tournament_team_id": r.get("tournament_team_id"),
        "team_name": r.get("team_name") or "",
        "team_country": r.get("team_country") or "",
        "games_played": _int(r.get("games_played")),
        "booyahs": _int(r.get("total_booyah")),
        "kills": kills,
        "placement_points": _num(r.get("placement_sum")),
        "kill_points": _num(r.get("kill_sum")),
        "bonus": _num(r.get("bonus_sum")),
        "penalty": _num(r.get("penalty_sum")),
        "carry_over": _num(r.get("carry_over_points")),
        "total": total,
        "is_champion": bool(r.get("is_champion")),
        "live": live,
        "total_with_live": _num(total + (live["points"] if live else 0)),
        "kills_with_live": kills + (live["kills"] if live else 0),
    }
    return row


def live_top_killers(official, rows, limit=10):
    """Top killers with the map in progress: `official` is the site's list (views_mvp.compute_top_killers
    reshaped: name, uid, team_name, kills, damage, assists, matches), `rows` the live_group_table rows.
    Each live player adds the kills of the map being played, matched by UID (by name when the game gave
    no UID); a player only on the live map joins the list. Ranked by kills, then damage, then name."""
    merged = {}
    for p in official or []:
        key = ("uid", p["uid"]) if p.get("uid") else ("name", _norm(p.get("name")))
        merged[key] = dict(p, live_kills=0)
    for r in rows or []:
        live = r.get("live")
        if not live:
            continue
        for lp in live["players"]:
            key = ("uid", lp["uid"]) if lp["uid"] else ("name", _norm(lp["name"]))
            if key not in merged and lp["uid"]:
                # Same person under the name key (the site list had no UID for them).
                alt = ("name", _norm(lp["name"]))
                if alt in merged:
                    key = alt
            cur = merged.get(key)
            if cur is None:
                cur = merged[key] = {"name": lp["name"], "uid": lp["uid"], "team_name": r["team_name"],
                                     "kills": 0, "damage": 0, "assists": 0, "matches": 0, "live_kills": 0}
            cur["live_kills"] = lp["kills"]
    out = []
    for p in merged.values():
        p["kills_with_live"] = int(p.get("kills") or 0) + int(p.get("live_kills") or 0)
        out.append(p)
    out.sort(key=lambda p: (-p["kills_with_live"], -int(p.get("damage") or 0), str(p.get("name") or "")))
    for i, p in enumerate(out[:limit]):
        p["rank"] = i + 1
    return out[:limit]


def live_map_mvp(rows, criteria):
    """The map in progress's provisional MVP by the event's own MVP rule (`criteria`, the ordered list from
    views_mvp._mvp_criteria): the live player first on those numbers the live board carries (kills, assists,
    headshots, knockdowns; damage reads 0 until the map's files are in). None with no live player."""
    best, best_key = None, None
    for r in rows or []:
        live = r.get("live")
        if not live:
            continue
        for p in live["players"]:
            key = tuple(int(p.get(c) or 0) for c in criteria if c in ("kills", "assists", "headshots", "knockdowns",
                                                                        "damage")) + (int(p.get("kills") or 0),)
            if best_key is None or key > best_key:
                best_key = key
                best = {"name": p["name"], "uid": p["uid"], "team_name": r["team_name"], "kills": p["kills"],
                        "assists": p["assists"], "damage": p["damage"]}
    return best


def live_group_table(group):
    """(rows, live_meta): the group's board with the map in progress folded in.

    rows       board_row dicts in on-air order. With no map in progress they are the official table
               (live None, total_with_live == total). With one, every team that plays the map carries its
               live part and the order is by total_with_live, then booyahs, then kills_with_live, then
               name; a crowned champion stays first, as on the official table.
    live_meta  None, or {"map_number", "teams_alive", "unmatched": [names on the pushed board that match
               no team of the group]}.
    """
    official = group_table(group)
    state = live_state(group)
    if state is None:
        return [board_row(i + 1, r) for i, r in enumerate(official)], None

    by_name = {}
    for row in state["rows"]:
        if isinstance(row, dict):
            by_name.setdefault(_norm(row.get("team_name")), row)

    rows = []
    for r in official:
        pushed = by_name.pop(_norm(r.get("team_name")), None)
        rows.append(board_row(0, r, _live_part(pushed) if pushed else None))
    # A team on the pushed board that is not on the group's table (AFC Capture could not name it, or it
    # is not seeded here): shown, at 0 official points, so nothing on air is lost.
    unmatched = []
    for row in by_name.values():
        unmatched.append(row.get("team_name") or "")
        rows.append(board_row(0, {"team_name": row.get("team_name") or "", "tournament_team_id": None},
                              _live_part(row)))

    champion = next((r for r in rows if r["is_champion"]), None)
    rows.sort(key=lambda r: (-float(r["total_with_live"]), -r["booyahs"], -r["kills_with_live"],
                             r["team_name"]))
    if champion is not None:
        rows.sort(key=lambda r: 0 if r is champion else 1)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    alive = sum(1 for r in rows if r["live"] and not r["live"]["eliminated"])
    return rows, {"map_number": state["map_number"], "teams_alive": alive, "unmatched": unmatched}


# ── per-map scope: teams and players over any chosen maps ───────────────────────────────────────────
# Owner, 11 Nov 2026 (inbox #538 #539): "damage still doesnt show for each individual player per map and
# per roup" / "export the leaderboard for not only a grop, but also each map or specific maps in a group".
# Both boards below take the list of maps (Match ids) in scope: a whole group = all its maps, one map = one
# id, any choice of maps = those ids. The admin / organizer Stats view and its CSV export read them
# (views_board.py), so what is on screen and what is downloaded are the same numbers.

PLAYER_SUM_FIELDS = (
    "kills", "damage", "assists", "deaths", "knockdowns", "knocked", "headshots", "revives_received",
    "grenades_used", "grenade_kills", "gloowall_used", "medkit_used", "survival_seconds",
)


def team_board(event, match_ids):
    """The team table over exactly `match_ids`, ranked on the site's chain (round_robin's aggregator, the
    event's tie-breakers), plus every team's damage and the rich team sums over the same maps."""
    from django.db.models import Sum
    from . import round_robin
    from .models import TournamentTeamMatchStats
    from .views import _overlay_rich_team_sums

    ids = list(match_ids)
    qs = TournamentTeamMatchStats.objects.filter(match_id__in=ids)
    rows = [dict(r) for r in round_robin._aggregate_team_standings(qs, event=event)]
    sums = {r["tournament_team_id"]: r for r in
            qs.values("tournament_team_id").annotate(d=Sum("damage"), a=Sum("assists"))}
    rich = _overlay_rich_team_sums({"team_stats__match_id__in": ids})
    out = []
    for i, r in enumerate(rows):
        tt = r.get("tournament_team_id")
        extra = rich.get(tt, {})
        s = sums.get(tt, {})
        out.append({
            "rank": i + 1,
            "tournament_team_id": tt,
            "team_name": r.get("team_name") or "",
            "team_country": r.get("team_country") or "",
            "games_played": _int(r.get("games_played")),
            "booyahs": _int(r.get("total_booyah")),
            "kills": _int(r.get("total_kills")),
            "placement_points": _num(r.get("placement_sum")),
            "kill_points": _num(r.get("kill_sum")),
            "bonus": _num(r.get("bonus_sum")),
            "penalty": _num(r.get("penalty_sum")),
            "total": _num(r.get("effective_total")),
            "damage": _int(s.get("d")),
            "assists": _int(s.get("a")),
            "knockdowns": _int(extra.get("knockdowns")),
            "knocked": _int(extra.get("knocked")),
            "headshots": _int(extra.get("headshots")),
            "deaths": _int(extra.get("deaths")),
            "revives_received": _int(extra.get("revives_received")),
            "grenades_used": _int(extra.get("grenades_used")),
            "grenade_kills": _int(extra.get("grenade_kills")),
            "gloowall_used": _int(extra.get("gloowall_used")),
            "medkit_used": _int(extra.get("medkit_used")),
            "survival_seconds": _int(extra.get("survival_time")),
            "most_used_weapon": extra.get("most_used_weapon") or "",
        })
    return out


def _flag_counts(flag, event):
    """Whether a flagged player's kills count toward his team (the same rule as the team totals)."""
    if flag.count_kills is not None:
        return bool(flag.count_kills)
    return bool(getattr(event, "count_flagged_kills", True))


def player_board(event, match_ids):
    """Every player of the chosen maps, one row each, all numbers summed over those maps.

    Rows come from the player stat lines (registered players and ghost players) AND from the flagged
    players whose kills count (MatchKillFlag: a UID the roster does not hold). A flag that names a
    registered user (the name matched) is added to that user's row and marks it `pending`; one that names
    nobody gets its own row keyed by the game's UID, `registered` False, so the player is on the board and
    the reason is plain. Ranked by kills, then damage, then assists, then name. most_used_weapon is the
    gun most of the player's maps name."""
    from collections import Counter
    from .models import MatchKillFlag, TournamentPlayerMatchStats

    ids = list(match_ids)
    rows = {}
    weapons = {}

    def blank(key, **ident):
        row = {"key": key, "user_id": None, "username": "", "in_game_name": "", "uid": "",
               "team_name": "", "registered": True, "pending": False, "matches": 0}
        row.update({f: 0 for f in PLAYER_SUM_FIELDS})
        row.update(ident)
        return row

    def add(row, values, match_id):
        for f in PLAYER_SUM_FIELDS:
            row[f] += _int(values.get(f))
        row.setdefault("_maps", set()).add(match_id)
        gun = values.get("most_used_weapon") or ""
        if gun:
            weapons.setdefault(row["key"], Counter())[gun] += 1

    def user_row(key, user, team_name):
        return rows.get(key) or rows.setdefault(key, blank(
            key, user_id=user.pk, username=user.username,
            in_game_name=getattr(user, "in_game_name", "") or user.username,
            uid=str(getattr(user, "uid", "") or ""), team_name=team_name))

    lines = (TournamentPlayerMatchStats.objects
             .filter(team_stats__match_id__in=ids)
             .select_related("player", "ghost_player", "team_stats__tournament_team__team",
                             "team_stats__tournament_team__ghost_team"))
    for s in lines:
        if not s.played:
            continue
        tt = s.team_stats.tournament_team
        team_name = tt.display_name if tt else ""
        if s.player_id:
            row = user_row("u{}".format(s.player_id), s.player, team_name)
        elif s.ghost_player_id:
            key = "g{}".format(s.ghost_player_id)
            ign = getattr(s.ghost_player, "ign", "") or ""
            row = rows.get(key) or rows.setdefault(key, blank(key, username=ign, in_game_name=ign,
                                                             team_name=team_name))
        else:
            continue
        values = {f: getattr(s, f, 0) for f in PLAYER_SUM_FIELDS}
        values["most_used_weapon"] = s.most_used_weapon
        add(row, values, s.team_stats.match_id)

    flags = (MatchKillFlag.objects.filter(match_id__in=ids)
             .select_related("registered_user", "tournament_team__team", "tournament_team__ghost_team"))
    for f in flags:
        if not _flag_counts(f, event):
            continue
        values = dict(f.stats or {})
        values["kills"] = f.kills
        team_name = f.tournament_team.display_name if f.tournament_team else ""
        if f.registered_user_id:
            row = user_row("u{}".format(f.registered_user_id), f.registered_user, team_name)
            row["pending"] = True
        else:
            key = "f{}".format(f.uid)
            row = rows.get(key) or rows.setdefault(key, blank(
                key, username=f.name, in_game_name=f.name, uid=str(f.uid), team_name=team_name,
                registered=False))
        add(row, values, f.match_id)

    out = []
    for row in rows.values():
        row["matches"] = len(row.pop("_maps", ()))
        row["most_used_weapon"] = weapons[row["key"]].most_common(1)[0][0] if row["key"] in weapons else ""
        row["kdr"] = round(row["kills"] / max(1, row["deaths"]), 2)
        out.append(row)
    out.sort(key=lambda r: (-r["kills"], -r["damage"], -r["assists"], (r["in_game_name"] or "").casefold()))
    for i, row in enumerate(out):
        row["rank"] = i + 1
    return out
