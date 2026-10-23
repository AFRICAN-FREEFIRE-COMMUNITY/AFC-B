"""
afc_leaderboard.tasks - Celery tasks for the standalone-leaderboard OCR batch (Phase 2.6).

process_leaderboard_ocr_job(job_id) backgrounds the screenshot read for ONE map so it never blocks an
HTTP request (the old synchronous read timed out on prod). It is a thin wrapper around
afc_leaderboard.ocr.process_job - the heavy logic lives there so it stays unit-testable and import-cheap.

QUEUE: the DEFAULT Celery queue (no queue=). A normal `celery -A afc worker` drains it. This is
deliberately NOT the nightly `ocr_ml` queue (autolabel/retrain) - those are low-priority batch jobs;
these interactive reads must run promptly while the admin watches the wizard poll.

ENQUEUED BY: afc_leaderboard.ocr_queue.dispatch only. ocr_job_run and ocr_run_all put maps in their
organizer's line, and dispatch starts at most OCR_QUEUE_SLOTS reads at once, in turn between organizers
(owner 2026-10-22, #334); when a read ends, this task calls dispatch for the next map. The FE
(OcrBatchDialog) polls ocr_job_list until each job is done/failed.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def process_leaderboard_ocr_job(job_id):
    """Read + match one LeaderboardOcrJob's screenshots in the background. Safe to retry: process_job
    re-reads the images and overwrites the rows. Missing job (deleted mid-flight) is a no-op."""
    from .models import LeaderboardOcrJob
    from .ocr import process_job

    from .ocr_queue import dispatch

    try:
        try:
            job = LeaderboardOcrJob.objects.select_related("leaderboard").get(id=job_id)
        except LeaderboardOcrJob.DoesNotExist:
            logger.warning("process_leaderboard_ocr_job: job %s no longer exists; skipping.", job_id)
            return
        process_job(job)
    finally:
        # This read's slot is free: hand it to the next map in line, in turn between organizers
        # (afc_leaderboard.ocr_queue, #334). Runs even when the read failed or the job was deleted.
        dispatch()
