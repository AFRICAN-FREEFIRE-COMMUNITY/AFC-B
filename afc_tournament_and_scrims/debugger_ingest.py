# ── DEBUGGER-LOG BACKFILL (owner 2026-07-02) ────────────────────────────────────
# "Uploading the debugger file even when not live can still give all the needed data" - CONFIRMED on
# real logs (memory project_freefire_live_capture): the Free Fire 3D observer client's
# Free Fire_64_Data/Debugger/debugger-*.log PERSISTS per session and carries, per round:
#   • the identity mapping  "Player Join, <UID>, <slotId>, <IGN>"        (UID ↔ in-match slot)
#   • kills/deaths          "Player <slot> Dead, killed by <slot>"
#   • knockdowns           "Player '<v>' Knock Down, by '<k>'"  (inbox #396; the gun trace below also
#                           fires on direct kills, so it only says whether a hit was a headshot)
#   • headshots            "PlayKnockDownGunTrace killer=<s> victim=<s> headshot=True|False"
#   • revives received      "Revive Player <slot>"
#   • round boundaries      "EventTypeEnterGame"  (same token afc-capture's live tailer resets on)
# One log spans a whole client session = MULTIPLE rounds, so ingest is two-step, mirroring the
# multi-map .log upload UX:
#   1. POST events/<event_id>/debugger-backfill/  (multipart `file`)             → DRY RUN: parsed
#      rounds, each with per-player rich stats + how many UIDs matched site accounts.
#   2. POST ... with `apply` = JSON [{"round_index": i, "match_id": m}, ...]      → for each mapped
#      round, fill deaths/knockdowns/headshots/revives_received/survival_seconds on that match's
#      TournamentPlayerMatchStats rows (matched by player UID) + set rich_stats_filled.
# Gate: _broadcast_gate (AFC event admin OR org can_edit_events) - same as the other overlay tooling.
# Unlocks: MVP criteria deaths/survival_time/headshots/kdr + the design columns of the same names.
# CONSUMED BY: the "Debugger log" panel on the leaderboard edit Upload tab (DebuggerBackfillPanel).

import json
from datetime import datetime

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import TournamentPlayerMatchStats, Match
from .views import _broadcast_gate

MAX_LOG_BYTES = 25 * 1024 * 1024  # a debugger .log is text; the cap only stops a mistake


class _Round:
    """One round of the log, as game_files.read_debugger splits it (inbox #396: ONE parser for this panel,
    the kill feed and the Game files box, so knocks come from "Knock Down" lines and a bleed-out after
    the knocker left for good earns nobody a kill, the rule that makes the debugger agree with the
    result file)."""

    def __init__(self, piece):
        from . import game_files

        self.start_ts = datetime.fromisoformat(piece["start"]) if piece["start"] else None
        self._stats, self.kill_events = game_files.game_stats(piece["players"], piece["events"])

    def players(self):
        """Per-player rich stats, UID-attributed."""
        return [{k: (str(v) if k == "uid" else v) for k, v in s.items() if k != "team"}
                for s in self._stats.values()]


def parse_debugger_log(text):
    """Split a whole-session debugger log into rounds of UID-attributed rich stats.
    Rounds with no joined players (menu/lobby segments) are dropped."""
    from . import game_files

    return [_Round(p) for p in game_files.read_debugger(text)]


def apply_rich_stats(match, by_uid):
    """Write one game's rich stats onto the match's player rows, matched by UID (the rows come from the
    result upload). Shared by this panel and the Game files box (views_game_files). Returns rows updated."""
    from django.db import transaction

    # one transaction, and the match and team loaded up front: every row save sets off the rankings
    # signal (afc_rankings/signals.py), which otherwise loads both again per row (7 to 9 s per game)
    rows = (TournamentPlayerMatchStats.objects.filter(team_stats__match=match)
            .select_related("player", "team_stats__match", "team_stats__tournament_team"))
    with transaction.atomic():
        updated = _fill_rows(rows, by_uid)
    return updated


def _fill_rows(rows, by_uid):
    updated = 0
    for row in rows:
        stats = by_uid.get(str(getattr(row.player, "uid", "") or ""))
        if not stats:
            continue
        row.deaths = stats["deaths"]
        row.knockdowns = stats["knockdowns"]
        row.headshots = stats["headshots"]
        row.revives_received = stats["revives_received"]
        row.survival_seconds = stats["survival_seconds"]
        row.rich_stats_filled = True
        row.rich_stats_source = "backfill"   # a capture upload of the same map overwrites this
        row.save(update_fields=[
            "deaths", "knockdowns", "headshots",
            "revives_received", "survival_seconds", "rich_stats_filled", "rich_stats_source",
        ])
        updated += 1
    return updated


@api_view(["POST"])
def debugger_backfill(request, event_id):
    """POST events/<event_id>/debugger-backfill/ - multipart `file` (+ optional `apply` JSON).
    Without `apply`: DRY RUN (parse + report). With `apply` = [{"round_index", "match_id"}, ...]:
    fill each mapped match's player rows (by UID) with the round's rich stats."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err

    up = request.FILES.get("file")
    if not up:
        return Response({"message": "Attach the debugger .log file.", "code": "attach_debugger_log_file"}, status=400)
    # The whole file is decoded in memory and parsed as text; it is never stored. A real
    # MatchResult log is well under a megabyte, so the cap only stops a mistake (R70).
    if up.size > MAX_LOG_BYTES:
        return Response({"message": "That file is too large for a debugger log (25 MB cap).",
                         "code": "log_too_large"}, status=400)
    try:
        text = up.read().decode("utf-8", errors="replace")
    except Exception:
        return Response({"message": "Could not read the file.", "code": "could_not_read_file"}, status=400)

    rounds = parse_debugger_log(text)
    if not rounds:
        return Response({"message": "No rounds with players found in this log.", "code": "no_rounds_players_found"}, status=400)

    # UIDs present in the log that match site accounts (User.uid), for the dry-run report.
    from afc_auth.models import User
    all_uids = {p["uid"] for r in rounds for p in r.players()}
    known_uids = set(
        User.objects.filter(uid__in=all_uids).values_list("uid", flat=True)
    )

    rounds_out = []
    for i, r in enumerate(rounds):
        plist = r.players()
        rounds_out.append({
            "round_index": i,
            "started_at": r.start_ts,
            "players": plist,
            "player_count": len(plist),
            "matched_accounts": sum(1 for p in plist if p["uid"] in known_uids),
            "total_kills": sum(p["kills"] for p in plist),
        })

    # ── APPLY: fill the mapped matches' player rows. ──
    raw_apply = request.data.get("apply")
    applied = []
    if raw_apply:
        try:
            mapping = json.loads(raw_apply) if isinstance(raw_apply, str) else raw_apply
            assert isinstance(mapping, list)
        except Exception:
            return Response({"message": "`apply` must be a list of {round_index, match_id}."}, status=400)
        for entry in mapping:
            try:
                ri = int(entry.get("round_index"))
                match_id = int(entry.get("match_id"))
            except (TypeError, ValueError):
                continue
            if not (0 <= ri < len(rounds)):
                continue
            match = Match.objects.filter(
                match_id=match_id, group__stage__event=event
            ).first()
            if not match:
                applied.append({"round_index": ri, "match_id": match_id,
                                "error": "Match not found on this event."})
                continue
            updated = apply_rich_stats(match, {p["uid"]: p for p in rounds[ri].players()})
            # Every kill and knock of the round, for the kill feed (sniper kills recorded, head to head;
            # inbox #350, kill_feed.py). Replaces the map's earlier events; never fails the backfill.
            stored_events = 0
            try:
                from . import kill_feed
                stored_events = kill_feed.store_events(match, event, rounds[ri].kill_events, "backfill")
            except Exception:
                import logging
                logging.getLogger("afc_tournament_and_scrims").exception(
                    "kill feed store failed for match %s", match.match_id)
            applied.append({"round_index": ri, "match_id": match_id, "updated_rows": updated,
                            "kill_events": stored_events})

    return Response({
        "rounds": rounds_out,
        "known_account_uids": len(known_uids),
        "applied": applied,
    }, status=200)
