"""
afc_wager.tasks - the beat sweep. One task, every minute, three steps in order:

    1. expire_unpaid        PENDING_PAYMENT wagers past their window -> EXPIRED
    2. lock_due_markets     OPEN markets whose lock_at has passed -> LOCKED (players notified)
    3. suggest_due_markets  LOCKED markets whose match has a result -> PENDING_SETTLEMENT with the
                            suggestion computed from the stats (afc_wager/suggest.py)

Scheduled in afc/celery_config.py (`wager_sweep_every_minute`). Each step is idempotent and
callable by hand (`manage.py wager_sweep`), which is how the scratch walk drives the clock.
Nothing here settles: a human confirms every settlement (services.settle_market).
"""
import logging

from celery import shared_task

from . import services

logger = logging.getLogger(__name__)


@shared_task(name="afc_wager.tasks.wager_sweep")
def wager_sweep():
    expired = services.expire_unpaid()
    locked = services.lock_due_markets()
    suggested = services.suggest_due_markets()
    if expired or locked or suggested:
        logger.info("wager sweep: expired %s, locked %s, suggested %s", expired, locked, suggested)
    return {"expired": expired, "locked": locked, "suggested": suggested}
