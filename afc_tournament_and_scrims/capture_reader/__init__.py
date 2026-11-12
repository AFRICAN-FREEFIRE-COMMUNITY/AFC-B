"""AFC Capture's own game-log reader, on the server (owner, 11 Nov 2026, inbox #541).

Owner: "when you upload a debugger file, you should be able to get all stats that capture wold have given ypou
and if not, the site should request for all files it needs for upload."

WHAT THIS IS
------------
A COPY of AFC Capture 1.5.4's reader (WEBSITE/afc-capture/afc_capture, not a git repository): tailer.py (the
debugger log, LiveMatchState: kills, knocks, deaths, revives, headshots, grenades, gloo walls, medkits, survival,
the kill feed, a client restart mid-map), replay.py (the game's replay file: every player's damage and the gun of
every kill and knock), weapons.py + weapons_table.py (gun names), events.py (the dashboard's event bus, a no-op
here). roster.py is not copied: only its name normaliser is, below in roster.py.

Copied so a debugger log uploaded on the website is read by the SAME code that read it live on the observer PC,
never by a second reader that drifts (R24). The files are byte for byte Capture's, line endings aside; the check
tests_capture_reader.py holds the copy to the sha256 recorded in SOURCE_SHA256 and fails when someone edits one side
only. To update: copy the five files from afc-capture/afc_capture, convert to LF, update SOURCE_SHA256 from
`python -I tools/capture_reader_sha.py` (prints the LF sha256 of each file), run the tests.

USED BY: capture_apply.py (builds Capture's per-map payload from uploaded files and applies it the way the Capture
upload and `manage.py backfill_capture_stats` do), views_game_files.py (the Game files box).
"""

# sha256 of each file's bytes with LF line endings, as copied from AFC Capture 1.5.4 on 11 Nov 2026.
SOURCE_VERSION = "1.5.4"
SOURCE_SHA256 = {
    "tailer.py": "a126dc56229ef60452b8c5792881428dd9cef9c7ccbd103373e783766352fa88",
    "replay.py": "bec1bf3240e9d4d7d37f8d9769d355536347c2f7accdef22b37989f13fcbf06e",
    "events.py": "0be39963013a4e9e689db4d3af11a1c1bd1bf70fe22daceeb366d8764cca4335",
    "weapons.py": "44cc8a78f5013530cb95094158fe63869210a005d40c026558b88aac8ed9a949",
    "weapons_table.py": "936d216d6474f388d97f68efb5cac2de9c3ffe8263cbad413d9fe256a301b68f",
}
