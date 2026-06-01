"""
Recalc dispatch + Celery tasks (§18 real-time recalculation).

Local dev: settings.RANKINGS_RECALC_SYNC (defaults to DEBUG) runs recalc inline on
commit — no Celery worker needed. Production: set it False + run
`celery -A afc worker -Q rankings_recalc` for async, deduplicated recalcs.

Dedup: a short Redis lock (recalc_lock:{key}) collapses bursts of edits to one
recalc per (entity, period) at a time; the run reads the latest committed state.
"""
import datetime

from celery import shared_task
from django.conf import settings
from django.core.cache import cache

from . import recalc as R

_LOCK_TTL = 120  # seconds


def _sync() -> bool:
    return getattr(settings, "RANKINGS_RECALC_SYNC", getattr(settings, "DEBUG", False))


def _with_lock(key, fn):
    lock = f"recalc_lock:{key}"
    if not cache.add(lock, 1, _LOCK_TTL):
        return  # already queued/running for this key
    try:
        fn()
    finally:
        cache.delete(lock)


# ───────────────────────── Celery tasks ─────────────────────────
@shared_task(queue="rankings_recalc")
def recalculate_team_monthly(team_id, month_str):
    month = datetime.date.fromisoformat(month_str)
    _with_lock(f"tm:{team_id}:{month_str}", lambda: R.recalc_team_monthly(team_id, month))


@shared_task(queue="rankings_recalc")
def recalculate_team_quarterly(team_id, season_id):
    _with_lock(f"tq:{team_id}:{season_id}", lambda: R.recalc_team_quarterly(team_id, season_id))


@shared_task(queue="rankings_recalc")
def recalculate_player_monthly(player_id, month_str):
    month = datetime.date.fromisoformat(month_str)
    _with_lock(f"pm:{player_id}:{month_str}", lambda: R.recalc_player_monthly(player_id, month))


@shared_task(queue="rankings_recalc")
def recalculate_player_quarterly(player_id, season_id):
    _with_lock(f"pq:{player_id}:{season_id}", lambda: R.recalc_player_quarterly(player_id, season_id))


# ───────────────────────── dispatch (sync-in-dev / async-in-prod) ─────────────────────────
def _dispatch(task, *args):
    if _sync():
        # run inline (no worker); skip the Redis lock — caller is already debounced via on_commit
        task.run(*args)
    else:
        task.delay(*args)


def enqueue_team(team_id, month: datetime.date, season_id=None):
    if not team_id:
        return
    _dispatch(recalculate_team_monthly, team_id, month.replace(day=1).isoformat())
    if season_id:
        _dispatch(recalculate_team_quarterly, team_id, season_id)


def enqueue_player(player_id, month: datetime.date, season_id=None):
    if not player_id:
        return
    _dispatch(recalculate_player_monthly, player_id, month.replace(day=1).isoformat())
    if season_id:
        _dispatch(recalculate_player_quarterly, player_id, season_id)
