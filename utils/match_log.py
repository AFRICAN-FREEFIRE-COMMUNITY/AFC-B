# utils/match_log.py — shared parser for the in-game TEAM match-log text export.
#
# WHY THIS EXISTS
# The event flow has parsed this file format for a while (afc_tournament_and_scrims.views
# TEAM_BLOCK_RE / PLAYER_RE + upload_team_match_result). The standalone-leaderboard flow now offers
# the same "upload result file" option (owner 2026-06-12: every upload option event leaderboards
# have must exist on standalone leaderboards too), so the format knowledge moves here where both
# flows can share it. The event view keeps its own regex copies untouched (deliberately - no risk to
# the live event path); they are byte-identical to these.
#
# FILE FORMAT (as exported by the game)
#   TeamName: <name>  Rank: <placement> ... KillScore: <team kills> ... RankScore: ... TotalScore: ...
#     NAME: <player>  ID: <uid>  KILL: <kills>
#     NAME: ...                                  (players repeat until the next TeamName: block)
#
# HOW IT CONNECTS
# - afc_leaderboard.views.results_file_extract parses an uploaded file with parse_team_match_log and
#   builds OCR-review-shaped rows from it (afc_leaderboard.ocr.build_rows_from_match_log), so the FE
#   reuses the exact same review table + apply pipeline the screenshot flow uses.

import re

TEAM_BLOCK_RE = re.compile(
    r"TeamName:\s*(?P<team_name>.+?)\s+Rank:\s*(?P<placement>\d+).*?"
    r"KillScore:\s*(?P<team_kills>\d+).*?"
    r"RankScore:\s*(?P<rank_score>\d+).*?"
    r"TotalScore:\s*(?P<total_score>\d+)(?P<players_block>.*?)(?=TeamName:|$)",
    re.DOTALL,
)

PLAYER_RE = re.compile(
    r"NAME:\s*(?P<name>.+?)\s+ID:\s*(?P<uid>\d+)\s+KILL:\s*(?P<kills>\d+)"
)


def parse_team_match_log(text):
    """Parse the match-log text into placement blocks.

    Returns [{team_name, placement, team_kills, players: [{name, uid, kills}]}], ordered as they
    appear in the file. Empty list when nothing parses (caller decides how to error)."""
    parsed = []
    for block in TEAM_BLOCK_RE.finditer(text or ""):
        players = [
            {
                "name": p.group("name").strip(),
                "uid": p.group("uid").strip(),
                "kills": int(p.group("kills")),
            }
            for p in PLAYER_RE.finditer(block.group("players_block"))
        ]
        parsed.append({
            "team_name": block.group("team_name").strip(),
            "placement": int(block.group("placement")),
            "team_kills": int(block.group("team_kills")),
            "players": players,
        })
    return parsed
