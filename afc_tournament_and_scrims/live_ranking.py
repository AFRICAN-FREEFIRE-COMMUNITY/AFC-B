"""Ranking + scoring of a LIVE capture snapshot - the server side of "the capture client only
observes" (owner, 2026-09-22).

WHY THIS EXISTS
---------------
Owner, in session 2026-09-22: "its not the capture that runs any calculation its the events and
leaderboard models that do all of that and all overlays pick from there." The desktop capture client
(afc-capture) tails the Free Fire debugger log and reports what it SAW: per-team and per-player
counts, who is still alive, and the order teams were wiped in. It does not decide positions and it
does not award points. This module turns those observations into the board:

    observations (kills, eliminated, elimination_order, alive_count)
        -> placement   (1 = best; the same rule the live overlay has always shown)
        -> points       via scoring.compute_team_points, from the EVENT's own scoring config
        -> pos + row order

so the live overlay and the official export are produced by ONE implementation of the scoring rule
(scoring.py), never by a copy baked into an installer on an observer's PC.

THE PLACEMENT RULE (ported verbatim from the client's old _placements, which is now deleted)
--------------------------------------------------------------------------------------------
A team wiped o-th (1 = first out) is LOCKED at placement ``N - o + 1``: the first team out finishes
last. The teams still alive take the remaining top placements, ordered by this match's kills, then by
how many of their players are still standing, then by name so the order is stable between two pushes
that are otherwise equal. The result is a gap-free permutation of 1..N. POINTS for a placement are
only given once it is final (owner 12 Nov 2026, inbox #558): a wiped team scores its locked place, a team
still alive scores only its kills until it is out, and the last team standing scores 1st when it is alone. The game's own TeamScore
plays no part: it is the room's cumulative score across maps, not this match's (measured 2026-09-22
over 24 matches).

OLD CLIENTS
-----------
Installed copies below 1.4.0 still push their own placement / pos / point columns. They are not
trusted for points (they never were: live_push has recomputed those since 2026-07-05), but a row that
carries no elimination facts at all still needs an order, so we fall back to the row's own placement,
then pos, then its position in the pushed list. A 1.3.x observer PC keeps working unchanged while it
waits for the auto-update.

CONNECTS TO
-----------
- views.py:live_push - the only caller; it resolves the event's scoring config and hands it here.
- scoring.py:compute_team_points / normalize_placement_points - the one implementation of the rule,
  shared with upload_team_match_result (the official path).
- afc-capture/afc_capture/tailer.py:snapshot - produces the rows this reads.
- views.py:overlay_feed - serves what this returns, under the shared live cache key.
- weapons.py:weapon_label - names most_used_weapon (team row and each player) so the live overlay
  prints the gun, never the game's id (inbox #468, 3 Nov 2026). Naming is not a calculation: the
  observation stays the client's, only the words change.
"""

from .scoring import compute_team_points
from .weapons import admin_names, weapon_label


def _named_weapons(row, names):
    """The row's most_used_weapon (and each player's) as gun names, only for the keys the row carries,
    so a snapshot without the field keeps its shape."""
    named = {}
    if "most_used_weapon" in row:
        named["most_used_weapon"] = weapon_label(row.get("most_used_weapon"), names)
    players = row.get("players")
    if isinstance(players, list):
        named["players"] = [
            {**p, "most_used_weapon": weapon_label(p.get("most_used_weapon"), names)}
            if isinstance(p, dict) and "most_used_weapon" in p else p
            for p in players]
    return named


def _int(value, default=0):
    """A count from a machine client, coerced. A blank or garbage value is 0, never an exception:
    one malformed row must never drop a whole live snapshot."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_true(value):
    """JSON booleans arrive as bool; be tolerant of "true" / 1 from a hand-rolled caller."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "yes", "1")


def _has_facts(row):
    """A row from afc-capture 1.4.0 or later: it says whether the team is out (pre-1.4.0 rows do not)."""
    return isinstance(row, dict) and (("eliminated" in row) or ("elimination_order" in row) or ("alive_count" in row))


def _is_out(row):
    """The team is wiped and its place is locked (rank_rows' own test for a locked team)."""
    return _is_true(row.get("eliminated")) and _int(row.get("elimination_order"), 0) > 0


def rank_rows(rows):
    """Return the rows ordered best first, each paired with its placement: [(row, placement)].

    Reads only OBSERVED fields - eliminated, elimination_order, kills, alive_count - so the client has
    no say in the order. Rows that carry no elimination facts (an old client) keep their pushed order
    behind the ones that do, so a mixed snapshot still renders."""
    total = len(rows)
    placed = {}      # index -> placement, for teams that are out
    alive_idx = []   # indexes of teams still in the game
    legacy_idx = []  # indexes of rows with no live facts at all (old client)

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            legacy_idx.append(i)
            continue
        has_facts = ("eliminated" in row) or ("elimination_order" in row) or ("alive_count" in row)
        if not has_facts:
            legacy_idx.append(i)
            continue
        if _is_true(row.get("eliminated")) and _int(row.get("elimination_order"), 0) > 0:
            # Wiped teams are locked: first out finishes last.
            placed[i] = total - _int(row.get("elimination_order")) + 1
        else:
            alive_idx.append(i)

    def _alive_key(i):
        row = rows[i]
        name = str(row.get("team_name") or "")
        return (-_int(row.get("kills")), -_int(row.get("alive_count")), name)

    # The living take the top placements; anything already locked keeps the slot it earned.
    taken = set(placed.values())
    free = [p for p in range(1, total + 1) if p not in taken]
    for rank_i, i in enumerate(sorted(alive_idx, key=_alive_key)):
        placed[i] = free[rank_i] if rank_i < len(free) else total

    # An old client's rows: its own placement or pos when it sent one, else its pushed position.
    for i in legacy_idx:
        row = rows[i] if isinstance(rows[i], dict) else {}
        placed[i] = _int(row.get("placement") or row.get("pos"), i + 1)

    ordered = sorted(range(len(rows)), key=lambda i: (placed.get(i, total + 1), i))
    return [(rows[i], placed.get(i, idx + 1)) for idx, i in enumerate(ordered)]


def normalize_live_standings(rows, *, placement_points, kill_point,
                             points_per_assist=0.0, points_per_1000_damage=0.0):
    """Order a pushed live snapshot and score it with the EVENT's scoring config.

    placement_points is an int->int table (already through scoring.normalize_placement_points). Every
    point column on the returned rows comes from scoring.compute_team_points, the same function the
    official upload path scores with; the client's own point columns are overwritten, never read.
    Damage is 0: the debugger stream carries no damage for other players (confirmed on OB55,
    2026-09-22), so an event that scores damage scores that term as 0 live and gets the real number
    when the MatchResult upload lands.

    Every live-only stat the client sent (deaths, knockdowns, knocked, headshots, assists, revives,
    gloowall_used, medkit_used, grenades_used, grenade_kills, survival_time, most_used_weapon,
    players) is passed through untouched: those are observations, and they are what the rich overlay
    columns render. The one change is words: most_used_weapon arrives as the game's id ("9") and
    leaves as the gun's name ("Desert Eagle"), through weapons.weapon_label."""
    out = []
    names = admin_names()                  # one query per push, not one per row
    rows = list(rows or [])
    # PLACEMENT POINTS ONLY ONCE A TEAM'S PLACE IS FINAL (owner 12 Nov 2026, AFC inbox #558: "IT IS GIVIGN
    # PLACEMENT POINTS WHEN THE ROUND HAS NOT YET ENDED, MEANWHILE IT SHOULD ONLY CALCULATED PLACEMENT FOR TEAMS
    # ALREADY ELIMINATED ONLY AND NOT TEAMS STILL ALIVE"). A wiped team's place is locked, so it scores it; a
    # team still in the game keeps its live rank on the board (placement) but scores only its kills until it
    # is out, and the last team standing scores 1st once it is the only one left, the map being over. A
    # pre-1.4.0 row (no elimination facts) is scored as before: there is nothing to tell its state by.
    standing = sum(1 for r in rows if _has_facts(r) and not _is_out(r))
    for row, placement in rank_rows(rows):
        if not isinstance(row, dict):
            continue
        try:
            final = (not _has_facts(row)) or _is_out(row) or standing <= 1
            kills = _int(row.get("kills"))
            assists = _int(row.get("assists")) if points_per_assist else 0
            points = compute_team_points(
                placement_points=placement_points if final else {},
                kill_point=kill_point,
                points_per_assist=points_per_assist,
                points_per_1000_damage=points_per_1000_damage,
                placement=placement,
                kills=kills,
                damage=0,
                assists=assists,
                bonus=0,
                penalty=0,
                played=True,
            )
            out.append({
                **row,                       # the observations, kept as they arrived
                **_named_weapons(row, names),
                "kills": kills,
                "placement": placement,
                "placement_final": final,    # False: still alive, its placement points come when it is out
                "pos": len(out) + 1,
                "kill_points": points["kill_points"],
                "placement_points": points["placement_points"],
                "total_points": points["total_points"],
                "base_total": points["total_points"],
                # A live round has no booyah, bonus or penalty and counts as no completed match yet;
                # the official board fills these when the MatchResult upload lands.
                "booyah": 0,
                "matches": 0,
                "bonus": 0,
                "penalty": 0,
            })
        except Exception:
            # One bad row never drops the snapshot; it keeps its pushed shape, at the end.
            out.append(row)
    return out
