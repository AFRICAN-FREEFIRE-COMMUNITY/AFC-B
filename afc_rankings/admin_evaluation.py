"""
Evaluation + recalc admin write API (Phase 2) — drive the quarterly tier lock and
manual recalcs.

This module is the "run the maths again" side of the ranking admin API. It sits on
top of the same shared foundation every other ranking admin write surface uses
(``admin_views._auth`` / ``_require_reason`` / ``_audit`` / ``RANKING_ADMIN_ROLES``)
and deliberately mirrors the house style (see ``admin_seasons.py`` for the canonical
example) so the original dev reads it without surprises:

  * function-based DRF views (``@api_view``), NOT class-based — same as ``views.py``.
  * manual-dict serialization (inline dicts here — no separate ``serialize_*`` needed,
    because ``recalc.run_evaluation`` already returns a ready-to-serialise summary dict),
    NO DRF Serializer classes — same as ``serializers.py``.
  * ``Response({"message": ...}, status=...)`` for every validation/error path — same
    message-dict shape as ``afc_auth.views``.
  * every *state-changing* endpoint runs: (1) auth gate, (2) reason gate, (3) the write
    inside ``transaction.atomic()``, then (4) a ``RankingAuditLog`` row via ``_audit``
    (§16 audit trail). The read-only ``recalc-status`` endpoint skips reason + audit.

Endpoints (mounted by the coordinator under the existing ``rankings/`` prefix):

    POST   seasons/<int:season_id>/run-evaluation/   run_evaluation   (head_admin OR metrics_admin)
    GET    admin/recalc-status/                       recalc_status    (read-only)
    POST   admin/recalc/                              recalc_entity    (head_admin OR metrics_admin)

WHY THE HEAVY LIFTING LIVES ELSEWHERE
-------------------------------------
The actual quarterly evaluation (tier the teams, inherit tiers to players, freeze the
season) is implemented once in ``recalc.run_evaluation`` — it already handles the
re-run guard, the dry-run path, the ``select_for_update`` season lock, and returns a
summary dict. These views are thin: they gate auth + reason, call into ``recalc`` /
``tasks``, audit the result, and serialise. They never re-implement scoring or recalc.

RECALC IS NEVER RUN INLINE FROM A REQUEST
-----------------------------------------
``run-evaluation`` runs the evaluation itself (that IS the request's job — it is fast:
just re-tiering already-computed scores). But a per-entity *score* recalc
(``admin/recalc/``) is enqueued on commit via ``tasks.enqueue_*`` so it runs through the
same debounced ``rankings_recalc`` pipeline the live edits use — never inline, never
blocking the request. (``run_evaluation`` does NOT recompute raw scores; it only locks
tiers, so it has no recalc to enqueue.)
"""
from django.core.cache import cache
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import recalc, tasks
from . import serializers as S
from .views import _resolve_season           # reuse the public season resolver (?season_id= or active)
from .admin_views import _auth, _require_reason, _audit
from .models import Season


# ───────────────────────── POST seasons/<id>/run-evaluation/  (lock tiers) ─────────────────────────
# Links to recalc.run_evaluation: the real work — tier the teams, inherit tiers to
# players, freeze the season, PLUS the re-run guard, the dry-run branch, and the
# select_for_update season lock — all lives in ``recalc.run_evaluation``. This view
# stays thin: gate (auth + reason), call recalc, audit the real run, serialise the
# summary. Do NOT move tiering logic here — it belongs in recalc, called once.
@api_view(["POST"])
def run_evaluation(request, season_id):
    """Run the quarterly tier EVALUATION for a season (§16 tier lock).

    Ranking admins (head_admin OR metrics_admin — the default ``_auth`` set). Body:
      * ``dry_run``  optional bool (default False). A dry run computes the would-be tier
                     changes and returns them WITHOUT writing anything — and WITHOUT
                     auditing (nothing changed). Use it to preview before committing.
      * ``force``    optional bool (default False). Re-run an already-evaluated season,
                     overwriting the previously locked tiers. Without it a second real
                     run is rejected (``recalc.run_evaluation`` returns ``ok: False``).
      * ``reason``   mandatory audit reason — ONLY for a real (non-dry) run. A dry run
                     writes nothing, so it needs no reason.

    The heavy lifting (tier teams → inherit to players → freeze the season, preserving
    zeroed/overridden rows) lives in ``recalc.run_evaluation``; this view only gates,
    audits the real run, and returns that function's summary dict.

    Responses:
      * 200 — success (dry run OR a real run that committed). Body is the summary dict.
      * 409 — ``recalc.run_evaluation`` returned ``ok: False`` (e.g. the re-run guard
              fired: already evaluated and ``force`` was not set). Conflict, not a
              validation error, so 409 rather than 400.
    """
    # (1) auth — default ranking-admin set (head_admin OR metrics_admin).
    user, err = _auth(request)
    if err:
        return err

    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)

    # Coerce the two optional flags. Anything truthy in the JSON body → True; absent → False.
    dry_run = bool(request.data.get("dry_run", False))
    force = bool(request.data.get("force", False))

    # (2) reason gate — required ONLY for a real run (a dry run changes/persists nothing).
    #     We pull it before calling run_evaluation so a real run never half-commits then
    #     fails the reason check.
    reason = None
    if not dry_run:
        reason, err = _require_reason(request)
        if err:
            return err

    # Run the evaluation. ``recalc.run_evaluation`` owns the re-run guard, the dry-run
    # branch, and (for a real run) its own ``select_for_update`` season lock + writes.
    summary = recalc.run_evaluation(season, user, dry_run=dry_run, force=force)

    # A guard inside run_evaluation (e.g. "already evaluated — re-run with force") returns
    # ok:False with a human message. Surface it as a 409 Conflict.
    if not summary.get("ok"):
        return Response(
            {"message": summary.get("error", "Evaluation could not be run.")},
            status=status.HTTP_409_CONFLICT,
        )

    # (4) audit — ONLY for a real, successful run. A dry run is a no-op preview, so it is
    #     deliberately NOT logged. ``after`` records the resulting tier distribution so the
    #     audit row alone shows the shape of the lock that was applied. ``before`` is the
    #     prior eval marker so the row is self-explanatory (was it a first run or a force?).
    if not dry_run:
        _audit(
            user, "evaluation", "run", reason,
            object_ref=f"season:{season.season_id}",
            before={"tier_eval_run": season.tier_eval_run, "forced": force},
            after={
                "tier_distribution": summary.get("tier_distribution"),
                "teams_evaluated": summary.get("teams_evaluated"),
                "players_evaluated": summary.get("players_evaluated"),
            },
            season=season,
        )

    # Return run_evaluation's summary dict verbatim — it already has the shape the admin
    # surface renders (ok, dry_run, teams_evaluated, players_evaluated, tier_distribution,
    # team_changes, player_changes).
    return Response(summary, status=status.HTTP_200_OK)


# ───────────────────────── GET admin/recalc-status/  (read-only) ─────────────────────────
# The recalc dedup locks live in the Django cache as keys named ``recalc_lock:{key}``
# (see ``tasks._with_lock``). ``{key}`` is one of the four documented shapes:
#     tm:{team_id}:{YYYY-MM-DD}    team monthly
#     tq:{team_id}:{season_id}     team quarterly
#     pm:{player_id}:{YYYY-MM-DD}  player monthly
#     pq:{player_id}:{season_id}   player quarterly
# A bare ``cache`` backend (e.g. LocMem) can't enumerate keys, and the Redis backend has
# no portable "scan by prefix" through Django's cache API. So a true "is anything
# recalculating right now?" answer would require probing every possible key — not
# feasible. We therefore take the documented best-effort posture: probe the locks that
# are knowable for THIS season (the team/player quarterly locks for entities that have a
# stored score row), and fall back to "idle" (with a note) when nothing is determinable.
_RECALC_PROBE_LIMIT = 500  # cap how many per-entity quarterly lock probes we issue


def _probe_season_recalc_running(season):
    """Best-effort: is a quarterly recalc currently locked for any entity in this season?

    Reads the ``recalc_lock:tq:{team_id}:{season_id}`` / ``...:pq:{player_id}:{season_id}``
    keys for the teams/players that already have a stored quarterly row this season, using
    ``cache.get_many`` (one round-trip). Returns ``(running, determinable)``:

      * ``running``       True if ANY probed lock is currently held.
      * ``determinable``  True if we were actually able to probe (we had entities to check);
                          False means "nothing to probe" → the caller reports idle + a note
                          rather than a confident False.

    This is intentionally scoped to quarterly locks for this season — the only lock keys
    whose ``{key}`` we can fully reconstruct without scanning. Monthly locks
    (``tm:``/``pm:`` keyed by an arbitrary month) are not probed; a manual recalc of those
    is short-lived and the run-evaluation flow is the status that matters here.
    """
    from .models import TeamQuarterlyScore, PlayerQuarterlyScore

    team_ids = list(
        TeamQuarterlyScore.objects.filter(season=season, team__isnull=False)
        .values_list("team_id", flat=True)[:_RECALC_PROBE_LIMIT]
    )
    player_ids = list(
        PlayerQuarterlyScore.objects.filter(season=season)
        .values_list("player_id", flat=True)[:_RECALC_PROBE_LIMIT]
    )

    keys = (
        [f"recalc_lock:tq:{tid}:{season.season_id}" for tid in team_ids]
        + [f"recalc_lock:pq:{pid}:{season.season_id}" for pid in player_ids]
    )
    if not keys:
        return False, False  # nothing to probe → not determinable

    # get_many returns only the keys that are SET, so a non-empty result means a lock is held.
    held = cache.get_many(keys)
    return bool(held), True


@api_view(["GET"])
def recalc_status(request):
    """Recalc / evaluation status for a season (read-only → no reason, no audit).

    Auth-gated (ranking admin) but not reason/audit-gated — reading status is not a
    ranking write. Season is resolved exactly like the public views: ``?season_id=``
    wins, else the active season.

    Returns:
      * ``recalculating``    best-effort bool — True if a quarterly recalc lock is held
                             for any entity in this season (see ``_probe_season_recalc_running``).
                             When the lock state can't be determined it returns False and
                             sets ``note`` so the caller knows the value is not authoritative.
      * ``last_evaluation``  the season's tier-eval marker: ``run`` (bool), ``at`` (ISO
                             timestamp or None), ``by`` (the admin's username or None).
      * ``frozen_at``        ``Season.scores_frozen_at`` (ISO) — when the scores were locked.
      * ``season``           the resolved season dict (so the caller knows what it asked about).
    """
    user, err = _auth(request)
    if err:
        return err

    season = _resolve_season(request)
    if not season:
        return Response({"message": "No active season."}, status=status.HTTP_404_NOT_FOUND)

    running, determinable = _probe_season_recalc_running(season)

    body = {
        "recalculating": running,
        "last_evaluation": {
            "run": season.tier_eval_run,
            "at": season.tier_eval_run_at.isoformat() if season.tier_eval_run_at else None,
            "by": season.tier_eval_run_by.username if season.tier_eval_run_by_id else None,
        },
        "frozen_at": season.scores_frozen_at.isoformat() if season.scores_frozen_at else None,
        "season": S.season(season),
    }
    if not determinable:
        # We had no entities to probe (e.g. an unevaluated/empty season), so "recalculating"
        # is a fallback False, not an observed one. Flag it so the UI doesn't over-trust it.
        body["note"] = "recalc lock state could not be determined; reported idle by default."
    return Response(body)


# ───────────────────────── POST admin/recalc/  (manual single recalc) ─────────────────────────
@api_view(["POST"])
def recalc_entity(request):
    """Manually trigger a single entity's recalc (head_admin OR metrics_admin).

    Body:
      * ``entity_type``  required — "team" or "player".
      * ``id``           required — the team_id / player_id (int).
      * ``season_id``    optional — recalc the quarterly score for this season too; absent
                         → only the current month is recomputed. (The monthly recalc always
                         runs; the season recalc only when ``season_id`` is given — matching
                         ``tasks.enqueue_team`` / ``enqueue_player``.)
      * ``reason``       optional — defaults to "Manual recalc triggered." A manual recalc
                         doesn't change any stored figure by admin fiat (it just re-derives
                         from the source data), so a reason is courtesy, not mandatory.

    Enqueues the recalc on commit through the same debounced ``rankings_recalc`` pipeline
    the live edits use (``tasks.enqueue_team`` / ``enqueue_player`` on
    ``transaction.on_commit``) — NEVER run inline. Audited as
    ``object_type="evaluation"``, ``action="recalc"``.

    Responses:
      * 200 — ``{"queued": true, ...}`` once the recalc is enqueued.
      * 400 — bad/missing ``entity_type`` or ``id`` (validation).
      * 404 — the season_id (when given) doesn't resolve.
    """
    # (1) auth — default ranking-admin set (head_admin OR metrics_admin).
    user, err = _auth(request)
    if err:
        return err

    data = request.data

    # ── validate entity_type ──
    entity_type = (data.get("entity_type") or "").strip().lower()
    if entity_type not in ("team", "player"):
        return Response(
            {"message": "entity_type must be one of: team, player."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── validate id ──
    try:
        entity_id = int(data.get("id"))
    except (TypeError, ValueError):
        return Response({"message": "id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

    # ── optional season_id: if present it must resolve to a real season ──
    season = None
    if data.get("season_id") not in (None, ""):
        season = Season.objects.filter(pk=data.get("season_id")).first()
        if not season:
            return Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)
    season_id = season.season_id if season else None

    # (2) reason — OPTIONAL for a manual recalc (it re-derives from source data, it doesn't
    #     impose a value). Default a stand-in so the audit row is never blank.
    reason = (data.get("reason") or "").strip() or "Manual recalc triggered."

    # The current month is the period the monthly recalc targets (tasks.enqueue_* re-clamps
    # to day=1, but compute it here so the audit row records exactly what was scheduled).
    month = recalc.current_month()

    # (3) enqueue on commit + audit, atomically. The recalc itself runs AFTER this
    #     transaction commits (on_commit) so it reads the latest committed state and never
    #     runs inline inside the request. The audit row is written in the same transaction
    #     as the on_commit registration, so "we logged a recalc" and "we scheduled it" are
    #     atomic — if the audit write rolls back, the recalc is never enqueued.
    with transaction.atomic():
        if entity_type == "team":
            transaction.on_commit(
                lambda: tasks.enqueue_team(entity_id, month, season_id)
            )
        else:  # "player"
            transaction.on_commit(
                lambda: tasks.enqueue_player(entity_id, month, season_id)
            )

        # (4) audit — object_type="evaluation", action="recalc" (per the surface spec).
        _audit(
            user, "evaluation", "recalc", reason,
            object_ref=f"{entity_type}:{entity_id}",
            before={},
            after={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "month": month.isoformat(),
                "season_id": season_id,
            },
            season=season,
        )

    return Response({
        "queued": True,
        "entity_type": entity_type,
        "id": entity_id,
        "month": month.isoformat(),
        "season_id": season_id,
    }, status=status.HTTP_200_OK)
