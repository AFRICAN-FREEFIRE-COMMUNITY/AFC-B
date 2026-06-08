"""
Seasons admin write API (Phase 2) — create / edit seasons + drive the transfer window.

This is the first of the Phase-2 admin *write* surfaces. It builds on the shared
foundation in ``admin_views.py`` (``_auth`` / ``_require_reason`` / ``_audit`` /
``RANKING_ADMIN_ROLES``) and deliberately mirrors the existing house style so the
original dev reads it without surprises:

  * function-based DRF views (``@api_view``), NOT class-based — same as ``views.py``.
  * manual-dict serialization (local ``serialize_*`` functions), NO DRF Serializer
    classes — same as ``serializers.py``.
  * ``Response({"message": ...}, status=...)`` for every validation/error path —
    same message-dict shape as ``afc_auth.views``.
  * every mutating endpoint runs: (1) auth gate, (2) reason gate (for writes that
    change ranking state), (3) the write inside ``transaction.atomic()``, then
    (4) a ``RankingAuditLog`` row via ``_audit`` (§16 audit trail).

Endpoints (mounted by the coordinator under the existing ``rankings/`` prefix):

    POST   seasons/                              season_create        (head_admin only)
    PATCH  seasons/<int:season_id>/              season_update        (ranking admins)
    PATCH  seasons/<int:season_id>/transfer-window/   transfer_window_action (ranking admins)
    GET    seasons/<int:season_id>/transfer-log/      transfer_log_list    (read-only)

THE ONE-ACTIVE-SEASON INVARIANT
-------------------------------
At most one ``Season`` may have ``is_active=True`` at a time (the "current season"
the public read API resolves to in ``views._resolve_season``). Whenever a write sets
``is_active=True`` we deactivate every *other* season in the same transaction
(``_deactivate_other_seasons``) so the invariant holds atomically.

NO RECALC HERE
--------------
Editing a season's metadata / dates / transfer window does NOT change any computed
score, so — unlike the result/prize/roster write surfaces — these endpoints never
enqueue a recalc. (Re-running quarterly tier evaluation is a separate Phase-2
surface, ``run-evaluation``.)
"""
import datetime

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .admin_views import _auth, _require_reason, _audit
from .models import Season, TransferWindowLog
from .serializers import paginate


# ───────────────────────── local serializers (manual-dict, per house style) ─────────────────────────
def serialize_season(s):
    """Full season dict for the admin surface.

    Mirrors ``serializers.season`` (the public read shape) but adds the two
    admin-only timestamps the public API omits: ``scores_frozen_at`` (set when a
    season's scores are locked) and the evaluation marker. ``transfer_window_open``
    / ``transfer_window_close`` are already in the public shape; kept here so the
    admin always sees them after a write.
    """
    return {
        "season_id": s.season_id,
        "name": s.name,
        "quarter": s.quarter,
        "year": s.year,
        "start_date": s.start_date.isoformat(),
        "end_date": s.end_date.isoformat(),
        "transfer_window_open": s.transfer_window_open.isoformat(),
        "transfer_window_close": s.transfer_window_close.isoformat(),
        "is_active": s.is_active,
        "tier_eval_run": s.tier_eval_run,
        # admin-only fields (not in the public serializer)
        "scores_frozen_at": s.scores_frozen_at.isoformat() if s.scores_frozen_at else None,
    }


def serialize_transfer_log(row):
    """One ``TransferWindowLog`` row — the prev/new window dates for an open/close/extend."""
    return {
        "id": row.id,
        "season_id": row.season_id,
        "action": row.action,
        "previous_open_date": row.previous_open_date.isoformat() if row.previous_open_date else None,
        "previous_close_date": row.previous_close_date.isoformat() if row.previous_close_date else None,
        "new_open_date": row.new_open_date.isoformat() if row.new_open_date else None,
        "new_close_date": row.new_close_date.isoformat() if row.new_close_date else None,
        "changed_by": row.changed_by_id,
        "changed_at": row.changed_at.isoformat() if row.changed_at else None,
        "reason": row.reason,
    }


# ───────────────────────── small parse/validation helpers ─────────────────────────
def _parse_date(value):
    """Parse an ISO ``YYYY-MM-DD`` string into a ``date``, or return ``None`` if absent/invalid.

    Uses ``datetime.date.fromisoformat`` to match how dates are parsed elsewhere in this
    app (see ``tasks.py``). Callers distinguish "field omitted" from "field present but
    bad" by checking the raw request value separately before calling this.
    """
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _deactivate_other_seasons(keep_pk):
    """Enforce the one-active-season invariant: deactivate every active season except ``keep_pk``.

    Call this INSIDE the same ``transaction.atomic()`` block as the write that set a
    season active, so the "exactly one active" state is committed atomically.
    """
    Season.objects.filter(is_active=True).exclude(pk=keep_pk).update(is_active=False)


# ───────────────────────── POST seasons/  (create) ─────────────────────────
@api_view(["POST"])
def season_create(request):
    """Create a season. head_admin ONLY (creating a season is a high-trust operation).

    Required body: ``name``, ``quarter`` (1-4), ``year``, ``start_date``, ``end_date``,
    ``transfer_window_open``, ``transfer_window_close`` (all dates ISO ``YYYY-MM-DD``),
    plus the mandatory audit ``reason``. Optional: ``is_active`` (default False).

    The ``(year, quarter)`` pair is unique at the DB level — a clashing pair surfaces as
    an ``IntegrityError`` which we translate to a clean 400 rather than a 500.
    """
    # (1) auth — head_admin only (override the default head_admin|metrics_admin set).
    user, err = _auth(request, roles=("head_admin",))
    if err:
        return err

    # (2) mandatory audit reason (this write changes ranking state).
    reason, err = _require_reason(request)
    if err:
        return err

    data = request.data

    # ── validate required text/number fields ──
    name = (data.get("name") or "").strip()
    if not name:
        return Response({"message": "name is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        quarter = int(data.get("quarter"))
    except (TypeError, ValueError):
        return Response({"message": "quarter must be an integer 1-4."}, status=status.HTTP_400_BAD_REQUEST)
    if quarter not in (1, 2, 3, 4):
        return Response({"message": "quarter must be one of 1, 2, 3, 4."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        year = int(data.get("year"))
    except (TypeError, ValueError):
        return Response({"message": "year must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

    # ── validate the four required dates ──
    start_date = _parse_date(data.get("start_date"))
    end_date = _parse_date(data.get("end_date"))
    tw_open = _parse_date(data.get("transfer_window_open"))
    tw_close = _parse_date(data.get("transfer_window_close"))
    missing = [
        field for field, val in (
            ("start_date", start_date), ("end_date", end_date),
            ("transfer_window_open", tw_open), ("transfer_window_close", tw_close),
        ) if val is None
    ]
    if missing:
        return Response(
            {"message": f"Missing or invalid date(s): {', '.join(missing)}. Use YYYY-MM-DD."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── sanity: ranges must not be inverted ──
    if end_date < start_date:
        return Response({"message": "end_date cannot be before start_date."}, status=status.HTTP_400_BAD_REQUEST)
    if tw_close < tw_open:
        return Response(
            {"message": "transfer_window_close cannot be before transfer_window_open."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    is_active = bool(data.get("is_active", False))

    # (3) the write — inside a single transaction so create + one-active invariant commit together.
    try:
        with transaction.atomic():
            season = Season.objects.create(
                name=name, quarter=quarter, year=year,
                start_date=start_date, end_date=end_date,
                transfer_window_open=tw_open, transfer_window_close=tw_close,
                is_active=is_active,
            )
            # enforce the one-active-season invariant if this season was created active.
            if is_active:
                _deactivate_other_seasons(season.pk)

            # (4) audit — before is empty (nothing existed), after is the created row.
            after = serialize_season(season)
            _audit(
                user, "season", "create", reason,
                object_ref=season.pk, before={}, after=after, season=season,
            )
    except IntegrityError:
        # the unique (year, quarter) DB constraint rejected the pair.
        return Response(
            {"message": f"A season for Q{quarter} {year} already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(serialize_season(season), status=status.HTTP_201_CREATED)


# ───────────────────────── PATCH seasons/<id>/  (edit) ─────────────────────────
@api_view(["PATCH"])
def season_update(request, season_id):
    """Edit a season's name / dates / transfer-window dates / ``is_active``.

    Ranking admins (head_admin OR metrics_admin — the default ``_auth`` set). Partial:
    only the fields present in the body are touched. If ``is_active`` is set true we
    re-assert the one-active-season invariant in the same transaction.
    """
    # (1) auth — default ranking-admin set.
    user, err = _auth(request)
    if err:
        return err

    # (2) mandatory audit reason.
    reason, err = _require_reason(request)
    if err:
        return err

    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)

    data = request.data
    before = serialize_season(season)  # snapshot BEFORE any mutation for the audit log.

    # ── name (optional) ──
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return Response({"message": "name cannot be blank."}, status=status.HTTP_400_BAD_REQUEST)
        season.name = name

    # ── dates (optional, each validated only if present) ──
    # We re-read the resulting values so the cross-field range checks below run against
    # the final state (mix of unchanged + newly supplied dates).
    for field in ("start_date", "end_date", "transfer_window_open", "transfer_window_close"):
        if field in data:
            parsed = _parse_date(data.get(field))
            if parsed is None:
                return Response(
                    {"message": f"Invalid {field}. Use YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            setattr(season, field, parsed)

    # ── cross-field range sanity on the (possibly updated) values ──
    if season.end_date < season.start_date:
        return Response({"message": "end_date cannot be before start_date."}, status=status.HTTP_400_BAD_REQUEST)
    if season.transfer_window_close < season.transfer_window_open:
        return Response(
            {"message": "transfer_window_close cannot be before transfer_window_open."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── is_active (optional) ──
    set_active = None
    if "is_active" in data:
        set_active = bool(data.get("is_active"))
        season.is_active = set_active

    # (3) the write — save + (if activated) re-assert the one-active invariant atomically.
    with transaction.atomic():
        season.save()
        if set_active:
            _deactivate_other_seasons(season.pk)

        # (4) audit — before/after snapshots make the change self-explanatory + reversible.
        after = serialize_season(season)
        _audit(
            user, "season", "update", reason,
            object_ref=season.pk, before=before, after=after, season=season,
        )

    return Response(serialize_season(season))


# ───────────────────────── PATCH seasons/<id>/transfer-window/  (open/close/extend) ─────────────────────────
@api_view(["PATCH"])
def transfer_window_action(request, season_id):
    """Open / close / extend a season's transfer window.

    Ranking admins (head_admin OR metrics_admin — default ``_auth`` set). Body:
      * ``action``         one of "opened" | "closed" | "extended" (matches
                           ``TransferWindowLog.ACTION_CHOICES``).
      * ``new_open_date``  optional ISO date — new window-open date.
      * ``new_close_date`` optional ISO date — new window-close date.
      * ``reason``         mandatory audit reason.

    Writes BOTH a ``TransferWindowLog`` row (capturing prev → new dates) AND a
    ``RankingAuditLog`` row with ``object_type="transfer_window"`` so the change shows
    up in the general ranking audit feed as well as the season's transfer-window history.
    """
    # (1) auth — default ranking-admin set.
    user, err = _auth(request)
    if err:
        return err

    # (2) mandatory audit reason.
    reason, err = _require_reason(request)
    if err:
        return err

    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)

    data = request.data

    # ── validate action against the model's allowed set ──
    action = (data.get("action") or "").strip()
    valid_actions = {choice for choice, _label in TransferWindowLog.ACTION_CHOICES}
    if action not in valid_actions:
        return Response(
            {"message": f"action must be one of: {', '.join(sorted(valid_actions))}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── capture the window dates BEFORE the change (logged as previous_*) ──
    prev_open = season.transfer_window_open
    prev_close = season.transfer_window_close

    # ── new dates: each optional; if present it must be a valid date. Absent → keep current. ──
    new_open = prev_open
    if "new_open_date" in data:
        parsed = _parse_date(data.get("new_open_date"))
        if parsed is None:
            return Response({"message": "Invalid new_open_date. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        new_open = parsed

    new_close = prev_close
    if "new_close_date" in data:
        parsed = _parse_date(data.get("new_close_date"))
        if parsed is None:
            return Response({"message": "Invalid new_close_date. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        new_close = parsed

    # ── range sanity on the resulting window ──
    if new_close < new_open:
        return Response(
            {"message": "transfer window close cannot be before open."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    before = serialize_season(season)

    # (3) the write — update the season's window + log the transition, atomically.
    with transaction.atomic():
        season.transfer_window_open = new_open
        season.transfer_window_close = new_close
        season.save(update_fields=["transfer_window_open", "transfer_window_close"])

        # the dedicated transfer-window history row (prev → new).
        log = TransferWindowLog.objects.create(
            season=season,
            action=action,
            previous_open_date=prev_open,
            previous_close_date=prev_close,
            new_open_date=new_open,
            new_close_date=new_close,
            changed_by=user,
            reason=reason,
        )

        # (4) audit — object_type="transfer_window" so it threads into the general audit feed too.
        after = serialize_season(season)
        _audit(
            user, "transfer_window", action, reason,
            object_ref=season.pk, before=before, after=after, season=season,
        )

    return Response({
        "season": serialize_season(season),
        "transfer_log": serialize_transfer_log(log),
    })


# ───────────────────────── GET seasons/<id>/transfer-log/  (read-only) ─────────────────────────
@api_view(["GET"])
def transfer_log_list(request, season_id):
    """List a season's transfer-window history (newest first). Read-only → no reason, no audit.

    Auth still required (admin surface), but using the default read gate via ``_auth``
    keeps it consistent with the write endpoints — only ranking admins see the log.
    Paginated with the canonical envelope ({"results": [...], "pagination": meta}).
    """
    # auth only — read-only endpoint skips the reason gate and the audit write.
    user, err = _auth(request)
    if err:
        return err

    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)

    # model Meta already orders by "-changed_at"; filter to this season and paginate.
    qs = TransferWindowLog.objects.filter(season=season)
    items, meta = paginate(request, qs)
    return Response({
        "results": [serialize_transfer_log(row) for row in items],
        "pagination": meta,
        "season_id": season.pk,
    })
