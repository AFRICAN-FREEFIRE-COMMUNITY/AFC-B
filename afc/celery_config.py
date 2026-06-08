# afc/celery_config.py
import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'afc.settings')

app = Celery('afc')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# ──────────────────────────────────────────────────────────────────────────────
# Beat schedule.
# This is the LIVE Celery app (afc/__init__.py imports it; `celery -A afc worker`
# and `celery -A afc beat` both load THIS module). The periodic OCR retrain-loop
# tasks (Phase 4) are wired here so they actually fire under beat.
#
# Queue strategy (mirrors afc_rankings' rankings_recalc pattern): the OCR ML tasks
# declare queue="ocr_ml" via @shared_task(queue="ocr_ml") in afc_ocr/tasks.py, so a
# dedicated worker drains them WITHOUT competing with the default web/celery queue:
#     celery -A afc worker -Q ocr_ml          # the OCR ML worker (nightly autolabel + weekly trigger)
#     celery -A afc beat                      # the scheduler that enqueues them
# In local dev these need no worker for normal site use; they only run when beat +
# an ocr_ml worker are actually started (autolabel also no-ops without GEMINI_API_KEY,
# and check_retrain_trigger is safe to run inline - see afc_ocr.tasks OCR_ML_SYNC).
# ──────────────────────────────────────────────────────────────────────────────
app.conf.beat_schedule = {
    # ── OCR learning loop (Phase 4) ──────────────────────────────────────────
    # Nightly autolabel: teacher-label the backlog of un-labelled match screenshots
    # via two-read Gemini consensus (silver training data). Runs at 02:30 server time,
    # off peak, when the Gemini budget and DB are quiet.
    'ocr_autolabel_backlog_nightly': {
        'task': 'afc_ocr.tasks.autolabel_backlog',
        'schedule': crontab(minute=30, hour=2),     # 02:30 every day
        'options': {'queue': 'ocr_ml'},
    },
    # Weekly retrain trigger: hysteresis check for whether enough NEW gold (admin-confirmed)
    # data has accumulated to request an off-box retrain. Runs Monday 03:00. Does NOT train;
    # it only emits a retrain_requested marker when the thresholds are met.
    'ocr_check_retrain_trigger_weekly': {
        'task': 'afc_ocr.tasks.check_retrain_trigger',
        'schedule': crontab(minute=0, hour=3, day_of_week=1),   # Mondays 03:00
        'options': {'queue': 'ocr_ml'},
    },
}

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
