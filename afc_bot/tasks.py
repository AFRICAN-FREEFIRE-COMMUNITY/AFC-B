"""
afc_bot.tasks - the Discord bot's scheduled work, now that the bot lives in this repo.

WHAT THIS REPLACES (owner 2026-08-18, absorbing AFCBot into the backend)
    A GitHub Action used to re-scrape the AFC website every 3 hours and COMMIT the result to the
    bot's repository. That was reasonable while the bot had a repo of its own. Here it would mean
    eight machine commits a day landing in the backend repo, churning its history and triggering
    whatever runs on a push.

    The backend already runs `celery beat`. A scheduled task writing to disk on the server does the
    same job with no commits at all, and the bot reads knowledge_base.txt from disk on every reply,
    so a fresh scrape is live the moment it lands. Nothing needs a restart.

WHY THE DEFAULT QUEUE. A plain `celery -A afc worker` consumes only the default queue, and four
dedicated queues in this project once sat with no consumer in production for weeks. A knowledge
refresh nobody drains is indistinguishable from one nobody wrote.

CONNECTS TO: afc/celery_config.py (the schedule), afcbot/afc_scraper.py (the scrape itself,
unchanged from when it ran in CI), and afcbot/knowledge_base.txt, which the bot reads live.
"""
import logging
import os
import sys

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


def _afcbot_dir():
    """Where the bot lives inside this repo."""
    return os.path.join(settings.BASE_DIR, "afcbot")


@shared_task
def refresh_bot_knowledge():
    """Re-scrape the AFC website into afcbot/knowledge_base.txt.

    Returns the number of characters written, or 0 when the scrape came back too thin to trust.

    THE GUARD THAT MATTERS is inside afc_scraper.write_knowledge_base, and it is kept rather than
    reimplemented here: a scrape yielding under 1000 characters leaves the existing file ALONE. The
    site being down must not empty the bot's knowledge, because a bot that has forgotten everything
    answers confidently and wrongly rather than failing visibly.

    Never raises. A failed refresh means the bot keeps answering from the knowledge it already has,
    which is a much better outcome than a retry storm against the website.
    """
    directory = _afcbot_dir()
    if not os.path.isdir(directory):
        logger.warning("afcbot/ is not present at %s; skipping knowledge refresh", directory)
        return 0
    if directory not in sys.path:
        sys.path.insert(0, directory)
    try:
        import afc_scraper

        chars = afc_scraper.write_knowledge_base(os.path.join(directory, "knowledge_base.txt"))
        logger.info("bot knowledge refreshed: %s characters", chars)
        return chars
    except Exception:
        logger.exception("bot knowledge refresh failed; the existing knowledge base is untouched")
        return 0
