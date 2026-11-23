"""Copy every result file the site has kept since July 2026 (MatchResultLog) into the game file archive.

Owner 2026-11-18, inbox #603 #611 ("save everything"): the archive starts with the result files we already have,
filed under the map they scored, so the Archive tab is not empty for past events. Safe to run any number of times:
the archive keeps each content once per event (game_archive.store_bytes), so a second run adds nothing.

Run on the server after the deploy:  python manage.py archive_result_logs
Plan: tasks/game-archive-plan-2026-11-18.md, Task 2.
"""
from django.core.management.base import BaseCommand

from afc_tournament_and_scrims import game_archive as ga
from afc_tournament_and_scrims.models import MatchResultLog


class Command(BaseCommand):
    help = "Copy the kept MatchResultLog files into the game file archive (idempotent)."

    def handle(self, *args, **options):
        counts = {"stored": 0, "duplicate": 0, "missing": 0, "refused": 0}
        logs = MatchResultLog.objects.select_related("match__group__stage__event", "uploaded_by").order_by("pk")
        for log in logs.iterator(chunk_size=200):
            try:
                with log.file.open("rb") as fh:
                    data = fh.read()
            except (OSError, ValueError):
                counts["missing"] += 1
                continue
            name = log.file_name or f"MatchResult_{log.match_id}.log"
            try:
                _row, result = ga.store_bytes(log.match.group.stage.event, name, data, source="backfill",
                                              user=log.uploaded_by, match=log.match, kind="result")
            except ga.ArchiveRefused as refused:
                counts["refused"] += 1
                self.stderr.write(f"log {log.pk}: {refused.code}")
                if refused.code == "archive_space_low":
                    break
                continue
            counts["stored" if result == "stored" else "duplicate"] += 1
            named = ga.classify(name)
            if named and named[0] == "result" and named[1]:
                ga.link_waiting(log.match, named[1])
        self.stdout.write(", ".join(f"{k} {v}" for k, v in counts.items()))
