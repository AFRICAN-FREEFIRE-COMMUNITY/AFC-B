# ── AFC CAPTURE RICH STATS (owner 2026-09-22, inbox #36) ─────────────────────────────────────────
# "These new metrics we can now count should have a place ... on the site ... somewhere under the
# event/leaderboard for the live overlays."
#
# WHAT THIS IS
#   The site-side home for the per-player stats the desktop capture client (afc-capture 1.4.0,
#   tailer.py) counts from the game's debugger log while a map runs: deaths, knockdowns, knocked,
#   headshots, knock assists, respawns, grenades thrown, grenade kills, gloowall / medkit used,
#   most-used weapon, survival time. They land on TournamentPlayerMatchStats, the same rows the
#   map's kills live on, so every reader of those rows (the leaderboard editor's per-map player
#   table, the official overlay feed's team sums, the booyah / player overlays, the MVP criteria)
#   sees them with no second lookup.
#
# HOW THEY ARRIVE
#   On the SAME request as the map's MatchResult file. upload_team_match_result accepts an optional
#   multipart field `rich_stats` (a JSON string) beside `file`; after the shared writer has created
#   the map's team + player rows, apply_capture_rich_stats() matches the payload's players to those
#   rows BY UID and fills the fields. One request, one Match row: no game-match-id to link later,
#   and a map uploaded by hand (no capture) simply has no rich stats, exactly as before.
#
# PAYLOAD (what afc-capture/afc_capture/watcher.py attaches, built from tailer.LiveMatchState):
#   {
#     "ff_match_id": "2102196355679832064",          # the game's own match id, for the record
#     "players": {                                     # keyed by Free Fire UID (User.uid)
#       "3757291052": {"deaths": 1, "knockdowns": 4, "knocked": 2, "headshots": 1, "assists": 0,
#                      "revives": 1, "grenades_used": 3, "grenade_kills": 0, "gloowall_used": 6,
#                      "medkit_used": 2, "most_used_weapon": "9", "survival_seconds": 1010}
#     },
#     "teams": {"UNDERGROUND": {"survival_seconds": 1010, ...}}   # informational, not stored
#   }
#
# RULES
#   - Never fails the upload: a missing, malformed or partial payload is reported in the answer
#     (`rich_stats_applied`, `rich_stats_error`) and the kills still save. The map result is the
#     load-bearing thing; these stats decorate it.
#   - Matched by UID only. A player row whose account has no UID, or a UID the payload does not
#     carry, keeps zeros and rich_stats_filled=False, so "0" and "no data" stay distinguishable.
#   - Values are clamped to sane non-negative ints; most_used_weapon to 16 chars.
#   - Marks rich_stats_filled=True and rich_stats_source="capture" on every row it fills. The
#     debugger-log backfill (debugger_ingest.py) marks "backfill" on its rows; a later capture
#     upload of the same map overwrites either (the freshest full source wins).
#
# CONSUMED BY
#   views.upload_team_match_result (the hook), tests_capture_rich_stats.py.

import json
import logging

from .models import TournamentPlayerMatchStats

log = logging.getLogger("afc_tournament_and_scrims")

# payload key -> model field. `revives` is what the capture client calls a respawn.
_FIELD_MAP = {
    "deaths": "deaths",
    "knockdowns": "knockdowns",
    "knocked": "knocked",
    "headshots": "headshots",
    "assists": "assists",
    "revives": "revives_received",
    "revives_received": "revives_received",
    "grenades_used": "grenades_used",
    "grenade_kills": "grenade_kills",
    "gloowall_used": "gloowall_used",
    "medkit_used": "medkit_used",
    "survival_seconds": "survival_seconds",
}
_INT_FIELDS = set(_FIELD_MAP.values())
_MAX_INT = 100000          # nothing in a 20-minute map gets near this; a bigger value is garbage
_WEAPON_MAX_LEN = 16


def parse_rich_stats(raw):
    """The `rich_stats` form field as a dict, or (None, reason). Accepts a JSON string or an
    already-parsed dict (DRF hands multipart text fields as strings)."""
    if raw in (None, ""):
        return None, "absent"
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None, "not_json"
    if not isinstance(data, dict) or not isinstance(data.get("players"), dict):
        return None, "bad_shape"
    return data, ""


def _clean_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, _MAX_INT))


def apply_capture_rich_stats(match, payload):
    """Fill the rich fields on the match's player rows from a parsed payload. Returns the number of
    player rows filled. Rows are matched by the player's Free Fire UID."""
    players = payload.get("players") or {}
    if not players:
        return 0
    by_uid = {str(k).strip(): v for k, v in players.items() if isinstance(v, dict)}
    rows = (
        TournamentPlayerMatchStats.objects
        .filter(team_stats__match=match)
        .select_related("player")
    )
    filled = 0
    for row in rows:
        uid = str(getattr(row.player, "uid", "") or "").strip()
        stats = by_uid.get(uid) if uid else None
        if not stats:
            continue
        for key, field in _FIELD_MAP.items():
            if key in stats:
                setattr(row, field, _clean_int(stats[key]))
        weapon = stats.get("most_used_weapon", "")
        row.most_used_weapon = str(weapon or "")[:_WEAPON_MAX_LEN]
        row.rich_stats_filled = True
        row.rich_stats_source = "capture"
        row.save(update_fields=sorted(_INT_FIELDS) + ["most_used_weapon", "rich_stats_filled", "rich_stats_source"])
        filled += 1
    log.info("capture rich stats: match %s, %d of %d player rows filled (ff match %s)",
             match.match_id, filled, len(by_uid), payload.get("ff_match_id"))
    return filled


# Every rich column a reader may show, in one place, so the leaderboard editor's players[] and the
# overlay sums name the same set (R24: one declaration).
RICH_PLAYER_FIELDS = (
    "deaths", "knockdowns", "knocked", "headshots", "assists", "revives_received",
    "grenades_used", "grenade_kills", "gloowall_used", "medkit_used", "survival_seconds",
    "most_used_weapon", "rich_stats_filled", "rich_stats_source",
)
# The ones a team row SUMS from its players (most_used_weapon is a mode, not a sum).
RICH_TEAM_SUM_FIELDS = (
    "deaths", "knockdowns", "knocked", "headshots", "assists", "revives_received",
    "grenades_used", "grenade_kills", "gloowall_used", "medkit_used",
)
