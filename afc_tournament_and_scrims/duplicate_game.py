"""
afc_tournament_and_scrims.duplicate_game - ONE GAME IS SCORED ONCE PER EVENT (inbox #623, owner 2026-11-18).

WHY THIS EXISTS
    Owner, 18 Nov 2026: "the website needs to be able to recognize when the stats that have already been uploaded
    is been uploaded twice and not allow it in and then even tell the capture". That night the first game of the
    NATIONAL FREE FIRE PRO LEAGUE semi finals Day 3 was counted as MATCH 1 AND MATCH 2. The nginx log shows why:
    AFC Capture's POST waited 30 s for an answer and gave up (499 at 18:28:51 UTC) while the server was still
    scoring MATCH 1; Capture sent the same file again, the server saw MATCH 1 scored and filled "the next unscored
    slot", MATCH 2, with the same game. Nothing on the server ever asked "is this game already in?".

HOW A GAME IS RECOGNISED (two keys, either one is enough)
    1. The Free Fire game id. The game names every result file MatchResult_<game id>_<date-time>.log and AFC
       Capture uploads it under that name, so MatchResultLog.file_name already carries the id of the game that
       scored each map. The same rule backfill_capture_stats.find_match has used since 11 Nov to find a map by
       its game. An id of 0 (some observer exports) is no id at all.
    2. The result itself, for a file renamed on the way (a website upload of "map 1.log") or with no id: every
       team block's name, rank, KillScore and TotalScore. Two different games with the same twelve names, ranks
       and kill counts do not happen; a re-sent file always matches.
    Only the LATEST result file of a map counts (a map re-uploaded with another game holds that other game), and
    only maps that hold a result (result_inputted): clearing a map frees its game for another slot.

WHAT IS REFUSED
    scored_holder(event, ...) returns the scored map already holding the game, anywhere in the EVENT (the same
    game cannot be two maps of one tournament, in any group). The caller decides what "the same map" means:
    a website upload into the map that already holds the game passes exclude_match_id (a re-score of the same
    map, kept allowed); an automatic upload excludes nothing.

CONNECTS TO
    - views.upload_team_match_result: checks before anything is parked, created or scored, and again under a row
      lock on the group inside the write transaction, so a retry that arrives while the first request is still
      scoring waits for it and is then refused (the 18 Nov race). Answers duplicate_answer() as a 409.
    - views_capture_pending.resolve_pending_capture: re-invokes the upload view, so a parked copy of a scored game
      is refused the same way; discard_pending_duplicates() retires parked copies once their game is scored.
    - AFC Capture 1.6.1 (afc-capture watcher.py) reads code capture_duplicate_game as "already on the website".
    - AFC_Frontend lib/i18n/serverMessage.ts prints the refusal in en/fr/pt (serverMessages.capture_duplicate_game,
      with the params below).
    - tests_duplicate_game.py proves each rule.
"""

import os
import re

from django.utils import timezone

# The game's own file name: MatchResult_<game id>_<yyyy-mm-dd-hh-mm-ss>.log
GAME_ID_RE = re.compile(r"MatchResult_(\d+)_", re.IGNORECASE)

DUPLICATE_CODE = "capture_duplicate_game"


def game_id_from_name(name):
    """The Free Fire game id in a result file's name, or "" (no id, or the id 0 some exports carry)."""
    m = GAME_ID_RE.search(os.path.basename(str(name or "")))
    if not m:
        return ""
    gid = m.group(1)
    return "" if not gid.strip("0") else gid


def _norm_team(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def result_signature(text):
    """The game's result as a comparable value: sorted (team name, rank, KillScore, TotalScore) of every team block,
    read with the SAME regex the scoring path uses. () when the text holds no team block."""
    from .views import TEAM_BLOCK_RE  # lazy: views imports this module

    rows = []
    for block in TEAM_BLOCK_RE.finditer(text or ""):
        rows.append((_norm_team(block.group("team_name")), int(block.group("placement")),
                     int(block.group("team_kills")), int(block.group("total_score"))))
    return tuple(sorted(rows))


def _stored_text(log):
    """A stored result file's text, or None when the file cannot be read (a missing file never blocks an upload)."""
    try:
        with log.file.open("rb") as fh:
            return fh.read().decode("utf-8", errors="ignore")
    except Exception:  # storage errors: treat as unknown, never as a match
        return None


def _latest_logs(event, exclude_match_id=None):
    """{match_id: its newest MatchResultLog} for every map of the event that holds a result."""
    from .models import MatchResultLog

    latest = {}
    logs = (MatchResultLog.objects
            .filter(match__group__stage__event=event, match__result_inputted=True)
            .select_related("match", "match__group")
            .order_by("match_id", "-uploaded_at", "-log_id"))
    for log in logs:
        latest.setdefault(log.match_id, log)
    if exclude_match_id is not None:
        latest.pop(int(exclude_match_id), None)
    return latest


def scored_holder(event, *, file_name, text, exclude_match_id=None):
    """The scored Match of ``event`` that already holds this game, or None. See the module docstring."""
    latest = _latest_logs(event, exclude_match_id)
    if not latest:
        return None
    gid = game_id_from_name(file_name)
    if gid:
        for log in latest.values():
            if game_id_from_name(log.file_name) == gid:
                return log.match
    sig = result_signature(text)
    if not sig:
        return None
    for log in latest.values():
        other = game_id_from_name(log.file_name)
        if gid and other and other != gid:
            continue          # two real, different game ids: two different games, no need to read the file
        stored = _stored_text(log)
        if stored is not None and result_signature(stored) == sig:
            return log.match
    return None


def _map_label(match):
    return str(match.match_map or "").strip().capitalize()


def duplicate_answer(holder, file_name=""):
    """The 409 body for a game already scored as ``holder``. ``params`` feed the website's translated sentence
    (serverMessages.capture_duplicate_game); the English sentence is what AFC Capture 1.6.0 and older print."""
    group = holder.group
    number = holder.match_number
    label = _map_label(holder)
    where = f"MATCH {number}" + (f" ({label})" if label else "")
    group_name = group.group_name if group else ""
    return {
        "duplicate_game": True,
        "code": DUPLICATE_CODE,
        "message": (f"This game is already on the website as {where}"
                    + (f" in {group_name}" if group_name else "")
                    + ". It was not counted again."),
        "params": {"match_number": number, "map": label or "-", "group": group_name or "-"},
        "existing_match_id": holder.match_id,
        "existing_match_number": number,
        "existing_match_map": label,
        "existing_group_id": group.group_id if group else None,
        "existing_group_name": group_name,
        "game_id": game_id_from_name(file_name),
    }


def discard_pending_duplicates(event, file_name, scored_match):
    """Retire the event's parked ("decide later") copies of a game that is now scored, so nobody resolves the same
    game into a second map later. Matched by the game id only. Returns how many were retired."""
    from .models import PendingCaptureUpload

    gid = game_id_from_name(file_name)
    if not gid:
        return 0
    return (PendingCaptureUpload.objects
            .filter(event=event, status="pending", file_name__startswith=f"MatchResult_{gid}_")
            .update(status="discarded", resolution=f"duplicate of match {scored_match.match_id}",
                    resolved_match=scored_match, resolved_at=timezone.now()))
