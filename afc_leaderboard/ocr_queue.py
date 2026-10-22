"""
afc_leaderboard/ocr_queue.py
================================================================================
Each organizer's own line for the standalone leaderboard screenshot batch (owner 2026-10-22,
inbox #334: "its own queue so one organizer's big batch doesn't hold up others").

WHY: run / run-all used to put every map straight on the shared Celery queue, first come first
served, and the worker runs four reads at once. One organizer's 30 maps filled all four ahead of
everyone else, and four on-site reads at once (two CPU threads each) starve the API on a 4-core box.

HOW:
  - enqueue(jobs): a map asked to be read waits in its organizer's line (status "pending" +
    LeaderboardOcrJob.queued_at). Nothing goes to Celery here.
  - dispatch(): at most settings.OCR_QUEUE_SLOTS reads run at once (2). A free slot goes to the
    organizer with the FEWEST reads running, the longest-waiting map first, so organizers take
    turns; one organizer alone uses every slot. AFC-run leaderboards (no organization) share one
    line. A read still "processing" after STALE_AFTER (a dead worker, a hung provider) is failed with
    a sentence and frees its slot.
  - dispatch() runs after every run request, after every finished read (tasks.py), and on every
    status poll (views.ocr_job_list), so a line never stalls waiting for one particular event.
  - estimates(): for the batch dialog, each waiting map's place in its organizer's line and the
    expected wait, from how long recent screenshots actually took.

Callers: afc_leaderboard.views (ocr_job_run, ocr_run_all, ocr_job_list) and
afc_leaderboard.tasks.process_leaderboard_ocr_job.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
STALE_MESSAGE = "Reading this map took too long and was stopped. Read it again."
# Used until recent reads give a measured figure (the on-site reader takes about 3 s a screenshot on
# the production box; an AI read is slower).
DEFAULT_SECONDS_PER_SCREENSHOT = 5.0


def slots() -> int:
    return max(1, int(getattr(settings, "OCR_QUEUE_SLOTS", 2) or 2))


def _owner(job):
    """Whose line a job stands in: its leaderboard's organization, or AFC for AFC-run leaderboards."""
    return job.leaderboard.organization_id or "afc"


def enqueue(jobs) -> None:
    """Put maps in their organizers' lines. A map already waiting keeps its place."""
    now = timezone.now()
    # One microsecond apart in the order given (oldest map first), so maps queued together still have
    # a strict order in line and a distinct place each.
    for i, job in enumerate(sorted(jobs, key=lambda j: (j.created_at, str(j.id)))):
        job.status = "pending"
        job.error = ""
        job.queued_at = job.queued_at or now + timedelta(microseconds=i)
        job.save(update_fields=["status", "error", "queued_at", "updated_at"])
    dispatch()


def dispatch() -> list:
    """Hand free reading slots to waiting maps, in turn between organizers. Returns the ids started."""
    from .models import LeaderboardOcrJob
    from .tasks import process_leaderboard_ocr_job

    started = []
    with transaction.atomic():
        now = timezone.now()
        running = list(LeaderboardOcrJob.objects.select_for_update()
                       .filter(status="processing").select_related("leaderboard"))
        live = []
        for job in running:
            if job.updated_at and job.updated_at < now - STALE_AFTER:
                job.status, job.error = "failed", STALE_MESSAGE
                job.save(update_fields=["status", "error", "updated_at"])
                logger.warning("ocr_queue: job %s was processing for over %s; failed it", job.id, STALE_AFTER)
            else:
                live.append(job)
        free = slots() - len(live)
        if free > 0:
            waiting = list(LeaderboardOcrJob.objects.select_for_update()
                           .filter(status="pending", queued_at__isnull=False)
                           .select_related("leaderboard").order_by("queued_at")[:500])
            busy = Counter(_owner(j) for j in live)
            while free > 0 and waiting:
                job = min(waiting, key=lambda j: (busy[_owner(j)], j.queued_at))
                waiting.remove(job)
                busy[_owner(job)] += 1
                free -= 1
                job.status = "processing"
                job.save(update_fields=["status", "updated_at"])
                started.append(str(job.id))
    # Sent once the block above has committed (its row locks are what stop two dispatches from
    # handing out the same slot), so a worker never picks up a map still marked "pending".
    for job_id in started:
        process_leaderboard_ocr_job.delay(job_id)
    return started


def _seconds_per_screenshot() -> float:
    """Average wall time of the last 50 screenshots read (LeaderboardOcrImage.raw_output._elapsed_ms,
    written by ocr.process_job), or DEFAULT_SECONDS_PER_SCREENSHOT before there are any."""
    from .models import LeaderboardOcrImage
    times = [
        (r or {}).get("_elapsed_ms")
        for r in LeaderboardOcrImage.objects.exclude(raw_output=None).order_by("-id").values_list("raw_output", flat=True)[:50]
    ]
    times = [t for t in times if isinstance(t, (int, float)) and t > 0]
    return (sum(times) / len(times) / 1000.0) if times else DEFAULT_SECONDS_PER_SCREENSHOT


def estimates(jobs) -> dict:
    """{job_id: (place_in_line, wait_seconds)} for the given jobs that are waiting in line.

    place_in_line counts the maps of the SAME organizer ahead of it, plus one: other organizers do
    not push it back, because slots are handed out in turn. wait_seconds = the screenshots ahead of
    it in its own line and its own, at the measured pace, shared over the slots this organizer gets
    (all of them alone, one while others are also waiting)."""
    from django.db.models import Count
    from .models import LeaderboardOcrJob

    mine = [j for j in jobs if j.status == "pending" and j.queued_at]
    if not mine:
        return {}
    line = list(LeaderboardOcrJob.objects.filter(status__in=["pending", "processing"])
                .exclude(status="pending", queued_at__isnull=True)
                .select_related("leaderboard").annotate(n_images=Count("images")).order_by("queued_at"))
    owners = {_owner(j) for j in line}
    pace = _seconds_per_screenshot()
    out = {}
    for job in mine:
        owner = _owner(job)
        ahead = [j for j in line if _owner(j) == owner and j.status == "pending" and j.queued_at < job.queued_at]
        running = [j for j in line if _owner(j) == owner and j.status == "processing"]
        own = next((j.n_images for j in line if j.id == job.id), 0) or 1
        share = slots() if len(owners) <= 1 else 1
        screenshots = sum(j.n_images for j in ahead) + sum(j.n_images for j in running) / 2.0
        wait = math.ceil((screenshots / share + own) * pace)
        out[str(job.id)] = (len(ahead) + 1, int(wait))
    return out
