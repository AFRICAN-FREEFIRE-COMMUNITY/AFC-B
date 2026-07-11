"""
recount_zero_uid_matches - find (and repair) matches under-counted by the ID:0 sentinel bug.

WHY: Free Fire exports a player it can't resolve a UID for as `ID: 0` (a "UID unknown" sentinel, not
a real identity). Before the 2026-07-11 fix, upload_team_match_result keyed players by their UID
string, so every `ID: 0` player in a team block collapsed onto the single key "0": the per-block
de-dupe kept only the FIRST and silently dropped the rest BEFORE they became counted flags. Their
kills vanished and "count flagged kills" could not recover them (DYNASTY CUP "RUSH POINT" map 6:
FROZEN 9->5, FORSE 6->2, ALPHA 5->1, SPACE X 3->1). The fix rewrites each sentinel UID to a stable
per-player synthetic key so they are attributed and counted individually.

This command repairs matches that were ALREADY uploaded before the fix. It uses the stored .log
(MatchResultLog, the per-match audit trail kept since 2026-07-07) as the source of truth:

  • SCAN (default, dry-run): re-parse each match's latest stored .log, find team blocks with >=2
    sentinel (ID:0 / blank) players, and print the file's team KillScore vs the currently-stored kills
    (delta = kills lost). Read-only.
  • --apply: RE-UPLOAD each affected match's stored .log through the (now fixed) upload endpoint, which
    re-derives the standings correctly. Idempotent (the upload path clears + rebuilds the match first).

Only matches WITH a stored .log can be repaired (uploads from 2026-07-07 on). A match whose .log file
is missing on disk is reported and skipped. Re-uploading appends one audit-trail MatchResultLog row.

Consumes: MatchResultLog, Match, TournamentTeamMatchStats (afc_tournament_and_scrims.models),
utils.match_log.parse_team_match_log, the upload_team_match_result endpoint (driven via a short-lived
SessionToken for the match's event creator / a superuser). Sibling of recount_uid_changed_flags.

Usage:
    python manage.py recount_zero_uid_matches                    # dry-run scan, ALL events
    python manage.py recount_zero_uid_matches --event-id 172     # dry-run, one event
    python manage.py recount_zero_uid_matches --match-id 3713    # dry-run, one match
    python manage.py recount_zero_uid_matches --apply            # repair every affected match
    python manage.py recount_zero_uid_matches --event-id 201 --apply
"""
import datetime
import re

from django.core.management.base import BaseCommand
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from afc_auth.models import User, SessionToken
from afc_tournament_and_scrims.models import Match, MatchResultLog, TournamentTeamMatchStats
from utils.match_log import parse_team_match_log


def _is_sentinel_uid(uid):
    """A file UID that is NOT a real identity: blank, or all-zero ('0'/'00'/...)."""
    u = (uid or "").strip()
    return (u == "") or (u.isdigit() and int(u) == 0)


class Command(BaseCommand):
    help = "Find/repair matches under-counted by the ID:0 sentinel de-dupe bug (from stored .log files)."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", type=int, default=None, help="Limit to one event.")
        parser.add_argument("--match-id", type=int, default=None, help="Limit to one match.")
        parser.add_argument("--apply", action="store_true",
                            help="Re-upload each affected match's stored .log to re-score. Default: dry-run.")

    def handle(self, *args, **opts):
        apply = opts["apply"]

        # Every match that has at least one stored .log (newest first per match via the model ordering).
        matches = Match.objects.all()
        if opts["match_id"]:
            matches = matches.filter(match_id=opts["match_id"])
        if opts["event_id"]:
            matches = matches.filter(group__stage__event_id=opts["event_id"])
        match_ids = list(matches.filter(result_logs__isnull=False)
                         .values_list("match_id", flat=True).distinct())

        self.stdout.write(f"Scanning {len(match_ids)} match(es) that have a stored .log...\n")

        affected = []       # (match, [(team_name, sentinels, file_kills, stored_kills, delta)])
        missing_file = []   # matches whose stored .log file is not on disk

        for mid in match_ids:
            m = Match.objects.select_related("group__stage__event").get(match_id=mid)
            mrl = MatchResultLog.objects.filter(match=m).first()  # newest (model Meta ordering)
            try:
                mrl.file.open("rb")
                text = mrl.file.read().decode("utf-8", errors="ignore")
                mrl.file.close()
            except Exception:
                missing_file.append(m)
                continue

            parsed = parse_team_match_log(text)
            # Key stored kills by NORMALIZED team name so a file block "ALPHA WOLVES" still maps to the
            # registered "Alpha Wolves" (case/spacing differ). An abbreviated in-game name that doesn't
            # normalize to any registered team (e.g. "BERSERK GEN" vs "BERSERK GENERATION" — an
            # unmatched/attributed block) stays None, which is correct: --apply re-derives it via re-upload.
            _n = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
            stored = {_n(ts.tournament_team.team.team_name): ts.kills
                      for ts in TournamentTeamMatchStats.objects.select_related(
                          "tournament_team__team").filter(match=m)}

            rows = []
            for blk in parsed:
                sentinels = [p for p in blk["players"] if _is_sentinel_uid(p["uid"])]
                if len(sentinels) >= 2:  # the drop only happens when a block has 2+ sentinel players
                    file_kills = blk["team_kills"]
                    # stored kills for this block's team (normalized name match; None if unresolved)
                    sk = stored.get(_n(blk["team_name"]))
                    rows.append((blk["team_name"], len(sentinels), file_kills, sk,
                                 (file_kills - sk) if sk is not None else None))
            if rows:
                affected.append((m, rows))

        # ---- report ----
        if missing_file:
            self.stdout.write(self.style.WARNING(
                f"{len(missing_file)} match(es) have a stored .log ROW but the file is missing on disk "
                f"(cannot analyze/repair here): " + ", ".join(str(x.match_id) for x in missing_file)))

        if not affected:
            self.stdout.write(self.style.SUCCESS("No matches with 2+ ID:0 players in a block. Nothing to do."))
            return

        self.stdout.write(f"\n{len(affected)} affected match(es):")
        for m, rows in affected:
            ev = m.group.stage.event
            self.stdout.write(
                f"\n  match {m.match_id}  event {ev.event_id} {ev.event_name[:30]!r}  map={m.match_map}")
            for name, ns, fk, sk, delta in rows:
                d = "?" if delta is None else (f"-{delta}" if delta > 0 else str(-delta))
                self.stdout.write(
                    f"     {name[:22]:22} sentinels={ns} file_KillScore={fk} "
                    f"stored={sk} lost={d}")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDRY-RUN. Re-run with --apply to re-upload each match's stored .log and re-score."))
            return

        # ---- repair: re-upload each affected match's stored .log through the fixed endpoint ----
        self.stdout.write(self.style.WARNING("\nAPPLYING (re-uploading stored .log per affected match)..."))
        client = Client()
        repaired = 0
        for m, _rows in affected:
            ev = m.group.stage.event
            admin = ev.creator or User.objects.filter(is_superuser=True).first()
            if admin is None:
                self.stdout.write(f"  match {m.match_id}: no admin to act as, SKIPPED")
                continue
            tok = SessionToken.objects.create(
                user=admin, token=f"recount0uid-{m.match_id}",
                expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10),
            )
            try:
                mrl = MatchResultLog.objects.filter(match=m).first()
                mrl.file.open("rb"); data = mrl.file.read(); mrl.file.close()
                f = SimpleUploadedFile(mrl.file_name or "match.log", data, content_type="text/plain")
                resp = client.post("/events/upload-team-match-result/",
                                   data={"match_id": m.match_id, "file": f},
                                   HTTP_AUTHORIZATION=f"Bearer {tok.token}")
                if resp.status_code == 200:
                    repaired += 1
                    after = {ts.tournament_team.team.team_name: ts.kills
                             for ts in TournamentTeamMatchStats.objects.select_related(
                                 "tournament_team__team").filter(match=m)}
                    self.stdout.write(self.style.SUCCESS(
                        f"  match {m.match_id}: re-scored OK -> " +
                        ", ".join(f"{n[:14]}={after.get(n)}" for n, *_ in _rows)))
                else:
                    self.stdout.write(self.style.ERROR(
                        f"  match {m.match_id}: upload returned {resp.status_code} {resp.content[:160]}"))
            finally:
                tok.delete()

        self.stdout.write(self.style.SUCCESS(f"\nAPPLIED: re-scored {repaired}/{len(affected)} match(es)."))
