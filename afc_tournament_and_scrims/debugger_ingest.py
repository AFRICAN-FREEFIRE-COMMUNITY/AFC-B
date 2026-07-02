# ── DEBUGGER-LOG BACKFILL (owner 2026-07-02) ────────────────────────────────────
# "Uploading the debugger file even when not live can still give all the needed data" — CONFIRMED on
# real logs (memory project_freefire_live_capture): the Free Fire 3D observer client's
# Free Fire_64_Data/Debugger/debugger-*.log PERSISTS per session and carries, per round:
#   • the identity mapping  "Player Join, <UID>, <slotId>, <IGN>"        (UID ↔ in-match slot)
#   • kills/deaths          "Player <slot> Dead, killed by <slot>"
#   • knockdowns+headshots  "PlayKnockDownGunTrace killer=<s> victim=<s> headshot=True|False"
#   • revives received      "Revive Player <slot>"
#   • round boundaries      "EventTypeEnterGame"  (same token afc-capture's live tailer resets on)
# One log spans a whole client session = MULTIPLE rounds, so ingest is two-step, mirroring the
# multi-map .log upload UX:
#   1. POST events/<event_id>/debugger-backfill/  (multipart `file`)             → DRY RUN: parsed
#      rounds, each with per-player rich stats + how many UIDs matched site accounts.
#   2. POST ... with `apply` = JSON [{"round_index": i, "match_id": m}, ...]      → for each mapped
#      round, fill deaths/knockdowns/headshots/revives_received/survival_seconds on that match's
#      TournamentPlayerMatchStats rows (matched by player UID) + set rich_stats_filled.
# Gate: _broadcast_gate (AFC event admin OR org can_edit_events) — same as the other overlay tooling.
# Unlocks: MVP criteria deaths/survival_time/headshots/kdr + the design columns of the same names.
# CONSUMED BY: the "Debugger log" panel on the leaderboard edit Upload tab (DebuggerBackfillPanel).

import json
import re
from datetime import datetime

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import TournamentPlayerMatchStats, Match
from .views import _broadcast_gate

# Tokens verified against real logs (mirrors afc-capture/afc_capture/tailer.py).
_RE_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]")
_RE_JOIN = re.compile(r"Player Join, (\d+), (\d+), (.+?),")
_RE_KILL = re.compile(r"Player (\d+) Dead, killed by (\d+)")
_RE_KNOCK = re.compile(r"PlayKnockDownGunTrace killer=(\d+) victim=(\d+) headshot=(True|False)")
_RE_REVIVE = re.compile(r"Revive Player (\d+)")
_ROUND_TOKEN = "EventTypeEnterGame"


def _ts(line):
    m = _RE_TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


class _Round:
    """Accumulators for ONE round of the log (between EventTypeEnterGame boundaries)."""

    def __init__(self, start_ts):
        self.start_ts = start_ts
        self.end_ts = start_ts
        self.uid_by_slot = {}   # slot -> uid
        self.ign_by_slot = {}   # slot -> ign
        self.kills = {}
        self.deaths = {}
        self.knockdowns = {}
        self.headshots = {}
        self.revives = {}
        self.death_ts = {}      # slot -> last death timestamp (drives survival)

    def bump(self, d, slot):
        d[slot] = d.get(slot, 0) + 1

    def players(self):
        """Per-player rich stats, UID-attributed. Slots with no Join line (spectator noise) drop."""
        out = []
        for slot, uid in self.uid_by_slot.items():
            died = self.death_ts.get(slot)
            # Survival: own death minus round start; still alive at round end = full round length.
            end = died or self.end_ts
            survival = int(max(0, (end - self.start_ts).total_seconds())) if (end and self.start_ts) else 0
            out.append({
                "uid": str(uid),
                "ign": self.ign_by_slot.get(slot, ""),
                "kills": self.kills.get(slot, 0),
                "deaths": self.deaths.get(slot, 0),
                "knockdowns": self.knockdowns.get(slot, 0),
                "headshots": self.headshots.get(slot, 0),
                "revives_received": self.revives.get(slot, 0),
                "survival_seconds": survival,
            })
        return out


def parse_debugger_log(text):
    """Split a whole-session debugger log into rounds of UID-attributed rich stats.
    Rounds with no joined players (menu/lobby segments) are dropped."""
    rounds = []
    cur = None
    for line in text.splitlines():
        ts = _ts(line)
        if _ROUND_TOKEN in line:
            if cur and cur.uid_by_slot:
                rounds.append(cur)
            cur = _Round(ts)
            continue
        if cur is None:
            # Tolerate logs that open mid-round (client restarted): start an implicit round.
            cur = _Round(ts)
        if ts:
            cur.end_ts = ts
        m = _RE_JOIN.search(line)
        if m:
            uid, slot, ign = m.group(1), int(m.group(2)), m.group(3).strip()
            cur.uid_by_slot[slot] = uid
            cur.ign_by_slot[slot] = ign
            continue
        m = _RE_KILL.search(line)
        if m:
            victim, killer = int(m.group(1)), int(m.group(2))
            cur.bump(cur.kills, killer)
            cur.bump(cur.deaths, victim)
            if ts:
                cur.death_ts[victim] = ts
            continue
        m = _RE_KNOCK.search(line)
        if m:
            killer = int(m.group(1))
            cur.bump(cur.knockdowns, killer)
            if m.group(3) == "True":
                cur.bump(cur.headshots, killer)
            continue
        m = _RE_REVIVE.search(line)
        if m:
            cur.bump(cur.revives, int(m.group(1)))
    if cur and cur.uid_by_slot:
        rounds.append(cur)
    return rounds


@api_view(["POST"])
def debugger_backfill(request, event_id):
    """POST events/<event_id>/debugger-backfill/ — multipart `file` (+ optional `apply` JSON).
    Without `apply`: DRY RUN (parse + report). With `apply` = [{"round_index", "match_id"}, ...]:
    fill each mapped match's player rows (by UID) with the round's rich stats."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err

    up = request.FILES.get("file")
    if not up:
        return Response({"message": "Attach the debugger .log file."}, status=400)
    try:
        text = up.read().decode("utf-8", errors="replace")
    except Exception:
        return Response({"message": "Could not read the file."}, status=400)

    rounds = parse_debugger_log(text)
    if not rounds:
        return Response({"message": "No rounds with players found in this log."}, status=400)

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
            by_uid = {p["uid"]: p for p in rounds[ri].players()}
            updated = 0
            # Update the match's existing player rows by UID (rows come from the MatchResult upload).
            for row in TournamentPlayerMatchStats.objects.filter(
                team_stats__match=match
            ).select_related("player"):
                stats = by_uid.get(str(getattr(row.player, "uid", "") or ""))
                if not stats:
                    continue
                row.deaths = stats["deaths"]
                row.knockdowns = stats["knockdowns"]
                row.headshots = stats["headshots"]
                row.revives_received = stats["revives_received"]
                row.survival_seconds = stats["survival_seconds"]
                row.rich_stats_filled = True
                row.save(update_fields=[
                    "deaths", "knockdowns", "headshots",
                    "revives_received", "survival_seconds", "rich_stats_filled",
                ])
                updated += 1
            applied.append({"round_index": ri, "match_id": match_id, "updated_rows": updated})

    return Response({
        "rounds": rounds_out,
        "known_account_uids": len(known_uids),
        "applied": applied,
    }, status=200)
