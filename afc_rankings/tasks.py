"""
Recalc dispatch + Celery tasks (§18 real-time recalculation).

Local dev: settings.RANKINGS_RECALC_SYNC (defaults to DEBUG) runs recalc inline on
commit - no Celery worker needed. Production: set it False + run
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
_DIRTY_TTL = 300  # seconds; outlives _LOCK_TTL so a marker can never expire mid-run
# How many extra passes one lock holder will make for edits that arrived while it was working.
# Two is enough for the bursts this actually sees (a stats upload commits in one transaction); the
# cap is there so a pathological stream of edits cannot hold a worker on a single entity forever.
_MAX_TRAILING_RUNS = 2


def _sync() -> bool:
    return getattr(settings, "RANKINGS_RECALC_SYNC", getattr(settings, "DEBUG", False))


def _with_lock(key, fn):
    """Run ``fn`` at most once at a time per key, and never lose the work of a collapsed run.

    DEBOUNCE, NOT DROP. The lock exists so a burst of edits (a whole map's stats uploaded row by
    row) collapses into one recalculation instead of one per row. It used to do that by simply
    RETURNING when the lock was held, which loses an update: the run in progress read the database
    before the later edit committed, the later edit's task was thrown away, and the entity kept a
    score computed from stale data until something unrelated touched it again.

    So a collapsed call now leaves a ``recalc_dirty`` marker instead of vanishing, and the holder
    checks it after finishing and runs once more. One extra pass covers any number of collapsed
    calls, because they all set the same marker - the debounce still holds. The marker is cleared
    BEFORE the re-run, so an edit landing during that second pass sets it again and is picked up
    rather than being swallowed by the clear.

    The loop is bounded (``_MAX_TRAILING_RUNS``) so a continuous stream of edits cannot pin a
    worker on one entity forever; whatever is still pending is then picked up by the next enqueue
    or by the nightly ``sweep_rankings`` backstop below.
    """
    lock = f"recalc_lock:{key}"
    dirty = f"recalc_dirty:{key}"
    if not cache.add(lock, 1, _LOCK_TTL):
        # Someone else is mid-run for this key. Mark the state as changed since they started.
        cache.set(dirty, 1, _DIRTY_TTL)
        return
    try:
        fn()
        for _ in range(_MAX_TRAILING_RUNS):
            if not cache.get(dirty):
                break
            cache.delete(dirty)   # clear FIRST so an edit during the re-run is not swallowed
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
        # run inline (no worker); skip the Redis lock - caller is already debounced via on_commit
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


# ───────────────────────── nightly backstop (§18, worker-down insurance) ─────────────────────────
@shared_task
def sweep_rankings():
    """Recompute the CURRENT month and the ACTIVE season from scratch, nightly.

    WHY THIS EXISTS. Everything above is event-driven: signals.py fires on a result / marker /
    prize edit and enqueues a per-entity recalculation. That is the fast path and it is what makes
    the ladders update by themselves. But it has one failure mode that is invisible from the
    outside: if nothing drains the ``rankings_recalc`` queue, every enqueued recalculation is
    accepted and silently never runs, so the ladders quietly freeze at whatever they last were
    while the site keeps serving them as current. That has already happened on this deployment -
    the queue reached six figures with no consumer - and nothing in the product surfaced it.

    So this is the repair pass, and the reason it is deliberately NOT on the ``rankings_recalc``
    queue: a backstop that queues behind the very worker it is insuring against is no backstop at
    all. It runs on the DEFAULT queue, which the ordinary ``celery -A afc worker`` drains, and it
    calls recalc.recalc_month / recalc_season DIRECTLY rather than enqueueing per-entity tasks -
    so a night with the rankings worker down still ends with correct ladders.

    Scope is the current month + active season only. That is the window anybody is looking at, and
    it keeps the cost proportional to recent activity: recalc_month / recalc_season walk the teams
    and players who actually have match stats inside the window, not the whole user table. Closed
    seasons are left alone on purpose - they are frozen by their SeasonScoringConfig pin and
    re-deriving them nightly is exactly the retroactive rewrite that pin exists to prevent.

    Idempotent: it recomputes from the same source rows the event-driven path reads, so running it
    when nothing has changed rewrites the same numbers. Scheduled from afc/celery_config.py
    (``rankings_sweep_nightly``). Safe to run by hand: ``recalc.recalc_month()`` /
    ``recalc.recalc_season()`` are the same calls the admin recalculate endpoints make.
    """
    recalc.recalc_month()
    season = recalc.current_season()
    if season:
        recalc.recalc_season(season)
