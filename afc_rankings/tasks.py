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

from . import recalc

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
# All four run on the dedicated rankings_recalc queue; in prod run a worker for
# it (celery -A afc worker -Q rankings_recalc). These are wrappers over recalc.py;
# enqueue_team / enqueue_player below are the only public entry points and are
# what signals.py calls.
@shared_task(queue="rankings_recalc")
def recalculate_team_monthly(team_id, month_str):
    month = datetime.date.fromisoformat(month_str)
    _with_lock(f"tm:{team_id}:{month_str}", lambda: recalc.recalc_team_monthly(team_id, month))


@shared_task(queue="rankings_recalc")
def recalculate_team_quarterly(team_id, season_id):
    _with_lock(f"tq:{team_id}:{season_id}", lambda: recalc.recalc_team_quarterly(team_id, season_id))


@shared_task(queue="rankings_recalc")
def recalculate_player_monthly(player_id, month_str):
    month = datetime.date.fromisoformat(month_str)
    _with_lock(f"pm:{player_id}:{month_str}", lambda: recalc.recalc_player_monthly(player_id, month))


@shared_task(queue="rankings_recalc")
def recalculate_player_quarterly(player_id, season_id):
    _with_lock(f"pq:{player_id}:{season_id}", lambda: recalc.recalc_player_quarterly(player_id, season_id))


# ── P3: ghost-team recalc wrappers (standalone-leaderboard feed) ──
# A ghost team's score lives only in standalone leaderboards (it has no event activity), so its
# recalc lives in afc_rankings.standalone, not recalc.py. These wrappers mirror the team wrappers
# above but call standalone.recalc_ghost_team_*; enqueue_ghost_team below is their public entry
# point, fired from standalone.recompute_for_leaderboard for each ghost-team participant.
@shared_task(queue="rankings_recalc")
def recalculate_ghost_team_monthly(ghost_team_id, month_str):
    from . import standalone  # lazy: standalone imports recalc/aggregation; keep tasks import-light
    month = datetime.date.fromisoformat(month_str)
    _with_lock(f"gtm:{ghost_team_id}:{month_str}",
               lambda: standalone.recalc_ghost_team_monthly(ghost_team_id, month))


@shared_task(queue="rankings_recalc")
def recalculate_ghost_team_quarterly(ghost_team_id, season_id):
    from . import standalone
    _with_lock(f"gtq:{ghost_team_id}:{season_id}",
               lambda: standalone.recalc_ghost_team_quarterly(ghost_team_id, season_id))


# ── ghost-PLAYER recalc wrappers (standalone solo-LB feed) ──
# A ghost player's score also lives only in standalone solo leaderboards, so its recalc lives in
# afc_rankings.standalone too. These mirror the ghost-team wrappers above but call
# standalone.recalc_ghost_player_*; enqueue_ghost_player below is their public entry point, fired
# from standalone.recompute_for_leaderboard for each ghost-player participant of a counting solo LB.
@shared_task(queue="rankings_recalc")
def recalculate_ghost_player_monthly(ghost_player_id, month_str):
    from . import standalone  # lazy: standalone imports recalc/aggregation; keep tasks import-light
    month = datetime.date.fromisoformat(month_str)
    _with_lock(f"gpm:{ghost_player_id}:{month_str}",
               lambda: standalone.recalc_ghost_player_monthly(ghost_player_id, month))


@shared_task(queue="rankings_recalc")
def recalculate_ghost_player_quarterly(ghost_player_id, season_id):
    from . import standalone
    _with_lock(f"gpq:{ghost_player_id}:{season_id}",
               lambda: standalone.recalc_ghost_player_quarterly(ghost_player_id, season_id))


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


def enqueue_ghost_team(ghost_team_id, month: datetime.date, season_id=None):
    """Public entry point for a ghost team's standalone-LB recalc (mirrors enqueue_team). Called by
    standalone.recompute_for_leaderboard for each ghost-team participant of a counting LB. Dispatches
    inline in dev (RANKINGS_RECALC_SYNC) or on the rankings_recalc Celery queue in prod."""
    if not ghost_team_id:
        return
    _dispatch(recalculate_ghost_team_monthly, str(ghost_team_id), month.replace(day=1).isoformat())
    if season_id:
        _dispatch(recalculate_ghost_team_quarterly, str(ghost_team_id), season_id)


def enqueue_ghost_player(ghost_player_id, month: datetime.date, season_id=None):
    """Public entry point for a ghost player's standalone solo-LB recalc (mirrors enqueue_player).
    Called by standalone.recompute_for_leaderboard for each ghost-player participant of a counting
    solo LB. Dispatches inline in dev (RANKINGS_RECALC_SYNC) or on the rankings_recalc Celery queue in
    prod. ghost_player_id is an int PK (GhostPlayer uses the default AutoField id)."""
    if not ghost_player_id:
        return
    _dispatch(recalculate_ghost_player_monthly, ghost_player_id, month.replace(day=1).isoformat())
    if season_id:
        _dispatch(recalculate_ghost_player_quarterly, ghost_player_id, season_id)
