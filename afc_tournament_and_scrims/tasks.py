"""
Celery tasks for tournaments/scrims.

Auto-complete (owner 2026-06-16) has two halves:
  • RESULTS-based: fires inline at the end of each result-save endpoint via
    views.maybe_autocomplete_event (final stage all results in -> complete).
  • DATE-based: this module's `close_finished_events`, a daily beat sweep that
    completes any non-draft tournament whose end_date has passed.

Both route through views.complete_event_core so a completion behaves identically
however it is triggered (status -> completed, fire qualification links, notify
registered competitors). Scheduled in afc/celery_config.py (beat_schedule
'close_finished_events_daily'). Discovered via app.autodiscover_tasks().
"""
from celery import shared_task
from django.utils import timezone


@shared_task
def close_finished_events():
    """Daily sweep: auto-complete tournaments whose last match date has passed.

    Marks every Event with end_date < today that is NOT a draft and NOT already
    completed/cancelled as completed (scrims are skipped — they have no completion
    concept). Idempotent: an already-completed event is excluded, and
    complete_event_core no-ops if it somehow slips through. Best-effort per event so
    one bad row never blocks the rest. Runs as the system (by_user=None), so no
    AdminHistory row is written; the status flip + link firing are the observable
    effects. Returns the number of events completed this run.
    """
    # Lazy import: views imports a large surface; importing it at module load would risk a cycle.
    from .models import Event
    from .views import complete_event_core

    today = timezone.localdate()
    qs = (Event.objects
          .filter(end_date__lt=today, is_draft=False)
          .exclude(event_status__in=["completed", "cancelled"]))

    completed = 0
    for ev in qs:
        if getattr(ev, "competition_type", None) == "scrims":
            continue  # scrims don't auto-complete on date (only tournaments do)
        try:
            if complete_event_core(ev, None, source="auto-date"):
                completed += 1
        except Exception:
            # Never let one event's completion (e.g. a linking hiccup) abort the whole sweep.
            continue
    return completed
