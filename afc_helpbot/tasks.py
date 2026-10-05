"""
afc_helpbot.tasks - the Help panel's housekeeping.

purge_old_help_chats deletes every conversation whose last message is older than
HELP_BOT_RETENTION_DAYS (30 by default), with its messages. A chat that was handed to a person is
safe to delete too: the ticket holds its own copy of the transcript (afc_helpbot.views.help_handoff).
Runs daily from celery beat (afc/celery_config.py) on the default queue, which a plain
`celery -A afc worker` drains (the same reasoning as afc_bot.tasks).
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import HelpConversation

logger = logging.getLogger(__name__)


@shared_task
def purge_old_help_chats():
    """Returns how many conversations were deleted."""
    days = int(getattr(settings, "HELP_BOT_RETENTION_DAYS", 30))
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _per_model = HelpConversation.objects.filter(last_message_at__lt=cutoff).delete()
    count = _per_model.get("afc_helpbot.HelpConversation", 0)
    logger.info("help bot: purged %s conversations older than %s days (%s rows)", count, days, deleted)
    return count
