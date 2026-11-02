"""
afc_polls.tasks - the poll engine's scheduled work.

refresh_geoip_db keeps the vote tracking's location file current (inbox #453, owner 2 Nov 2026). DB-IP
publishes a new free city database every month; a stale one slowly mislabels re-assigned address
ranges. The task asks for a refresh only when the file is older than 25 days, so the weekly schedule
in afc/celery_config.py downloads about once a month and quietly does nothing the other weeks.

WHY THE DEFAULT QUEUE: a plain `celery -A afc worker` drains it (see afc_bot.tasks for the history of
dedicated queues nobody consumed).

CONNECTS TO: afc_polls/management/commands/refresh_geoip_db.py (the work), afc_polls.geo (the reader
it resets), settings.GEOIP_CITY_DB (the file).
"""
import logging

from celery import shared_task
from django.core.management import call_command
from django.core.management.base import CommandError

logger = logging.getLogger(__name__)

REFRESH_AFTER_DAYS = 25


@shared_task
def refresh_geoip_db():
    """Refresh the DB-IP city file when it is older than REFRESH_AFTER_DAYS. Never raises."""
    try:
        call_command("refresh_geoip_db", max_age=REFRESH_AFTER_DAYS)
    except CommandError:
        # The previous file stays in place and keeps answering; the next weekly run tries again.
        logger.warning("GeoIP refresh failed; keeping the current file", exc_info=True)
