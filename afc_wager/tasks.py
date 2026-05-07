"""Celery task shells. STUBBED in v1 — bodies fire NotImplementedError so
the scheduler never invokes a live settlement before we've reviewed it.
"""

# Celery task decorators are imported lazily so this module imports under
# test settings even if Celery's broker isn't configured.

try:
    from celery import shared_task
except ImportError:  # pragma: no cover

    def shared_task(*args, **kwargs):
        def deco(fn):
            return fn

        return deco


@shared_task
def lock_market_at_time(market_id: int) -> dict:
    """STUB — live impl will flip status DRAFT/OPEN -> LOCKED at lock_at."""
    return {"stubbed": True, "market_id": market_id}


@shared_task
def settle_market(market_id: int, admin_id: int) -> dict:
    """STUB — live impl will call afc_wager.settlement.settle()."""
    return {"stubbed": True, "market_id": market_id, "admin_id": admin_id}
