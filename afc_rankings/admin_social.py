"""
Social-media admin write API (Phase 2) — manage the §7.3 team social snapshots.

This is the social-media write surface of the Phase-2 admin API. Teams self-link
their Instagram / TikTok handles (the captain-facing "connect" lives on the team
dashboard); an admin then verifies the follower counts. Only a *verified* snapshot
contributes ``social_media_pts`` to a team's quarterly score (the aggregation layer
reads ``combined_followers`` only when ``is_verified`` is True), so verify / unverify
move real points and therefore trigger a recalc.

It builds on the shared foundation in ``admin_views.py`` (``_auth`` / ``_require_reason``
/ ``_audit`` / ``RANKING_ADMIN_ROLES``) and deliberately mirrors the existing house
style so the original dev reads it without surprises:

  * function-based DRF views (``@api_view``), NOT class-based — same as ``views.py``.
  * manual-dict serialization (local ``serialize_*`` functions), NO DRF Serializer
    classes — same as ``serializers.py``.
  * ``Response({"message": ...}, status=...)`` for every validation/error path — same
    message-dict shape as ``afc_auth.views``.
  * every mutating endpoint runs: (1) auth gate, (2) reason gate, (3) the write inside
    ``transaction.atomic()``, then (4) a ``RankingAuditLog`` row via ``_audit`` (§16),
    and (5) — because social points feed the quarterly score — an ``enqueue_team``
    quarterly recalc scheduled on ``transaction.on_commit`` (never inline, never before
    the row is committed).

Endpoints (mounted by the coordinator under the existing ``rankings/`` prefix). Season
is taken from the URL path (``<int:season_id>``) — 404 if that season does not exist:

    GET   seasons/<int:season_id>/social/                    social_list      (read-only)
    PATCH seasons/<int:season_id>/social/<int:team_id>/      social_edit      (admin corrects counts/handles)
    POST  seasons/<int:season_id>/social/<int:team_id>/verify/    social_verify    (verified ⇒ counts toward score)
    POST  seasons/<int:season_id>/social/<int:team_id>/unverify/  social_unverify  (drops social back to 0)
    POST  seasons/<int:season_id>/social/<int:team_id>/connect/   social_connect   (link handles, pending verify)

Auth on EVERY endpoint here: head_admin OR metrics_admin (the default ``_auth`` set).
NOTE: ``social_connect`` is the ranking-admin self-connect form; the captain-facing
version of connect lives on the team dashboard (a separate, team-scoped surface) and
is not part of this admin module.

THE FOLLOWERS → POINTS INVARIANT
--------------------------------
Whenever we touch the follower counts we MUST keep three fields consistent on the
snapshot, in the same write:
    combined_followers = instagram_followers + tiktok_followers
    social_media_pts   = engine.social_media_points(combined_followers)   # 0..10
``_recompute_social`` centralises that so no endpoint can set followers and forget to
re-derive the combined total and the points. (The aggregation layer only *uses* the
points when ``is_verified`` is True — but we always keep the stored value correct so
the admin table shows the would-be points even before verification.)

A NOTE ON ``verified_at``
-------------------------
``TeamSocialSnapshot.verified_at`` is declared ``auto_now_add=True`` on the model, so
it is stamped once at row creation and is NOT writable afterwards. We therefore cannot
bump it on verify/unverify without a model+migration change (out of scope for this
file). It is serialised honestly as the snapshot's creation timestamp; treat the audit
log's ``changed_at`` as the authoritative "when was this verified" record.
"""
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_team.models import Team
from . import recalc, tasks
from .admin_views import _auth, _require_reason, _audit
from .models import Season, TeamSocialSnapshot
from .scoring import engine
from .serializers import paginate


# ───────────────────────── local serializer (manual-dict, per house style) ─────────────────────────
def serialize_social_row(team, snap):
    """One row of the social-management table: a ``Team`` LEFT JOIN its season snapshot.

    ``snap`` is the team's ``TeamSocialSnapshot`` for the resolved season, or ``None``
    when the team has never connected/been entered. We surface the same shape either
    way so the admin table renders a clean "not connected" row instead of a hole.

    ``connected`` means the team self-submitted its handles (snapshot exists AND
    ``connected_by_team``) — distinct from an admin having typed the counts in directly.
    ``verified_by`` is the verifying admin's username (or None); ``verified_at`` is the
    snapshot's creation timestamp (see the module note — the column is auto_now_add and
    not writable on verify).
    """
    if snap is None:
        # No snapshot yet → all-empty row keyed on the team only.
        return {
            "team_id": team.team_id,
            "team_name": team.team_name,
            "connected": False,
            "instagram_handle": "",
            "tiktok_handle": "",
            "instagram_followers": 0,
            "tiktok_followers": 0,
            "combined": 0,
            "points": 0.0,
            "is_verified": False,
            "verified_by": None,
            "verified_at": None,
        }
    return {
        "team_id": team.team_id,
        "team_name": team.team_name,
        "connected": bool(snap.connected_by_team),
        "instagram_handle": snap.instagram_handle,
        "tiktok_handle": snap.tiktok_handle,
        "instagram_followers": snap.instagram_followers,
        "tiktok_followers": snap.tiktok_followers,
        "combined": snap.combined_followers,
        "points": round(snap.social_media_pts, 2),
        "is_verified": snap.is_verified,
        "verified_by": snap.verified_by.username if snap.verified_by_id else None,
        "verified_at": snap.verified_at.isoformat() if snap.verified_at else None,
    }


def _snapshot_audit_state(snap):
    """Compact before/after JSON snapshot of the fields these endpoints mutate (§16).

    Kept small + self-explanatory so the audit row is hand-reversible: the exact
    handles, follower counts, derived combined/points, and the verify flag.
    """
    return {
        "instagram_handle": snap.instagram_handle,
        "tiktok_handle": snap.tiktok_handle,
        "instagram_followers": snap.instagram_followers,
        "tiktok_followers": snap.tiktok_followers,
        "combined_followers": snap.combined_followers,
        "social_media_pts": snap.social_media_pts,
        "is_verified": snap.is_verified,
        "connected_by_team": snap.connected_by_team,
    }


# ───────────────────────── small helpers ─────────────────────────
def _get_season_or_404(season_id):
    """Resolve the season from the URL path. Returns ``(season, None)`` or ``(None, Response)``.

    Season is path-scoped for this surface (``seasons/<int:season_id>/social/...``), so
    unlike the public read views — which fall back to the active season — a missing path
    season is a hard 404.
    """
    season = Season.objects.filter(pk=season_id).first()
    if not season:
        return None, Response({"message": "Season not found."}, status=status.HTTP_404_NOT_FOUND)
    return season, None


def _get_team_or_404(team_id):
    """Resolve the team. Returns ``(team, None)`` or ``(None, Response)``."""
    team = Team.objects.filter(pk=team_id).first()
    if not team:
        return None, Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    return team, None


def _parse_followers(value, field_name):
    """Parse a non-negative integer follower count. Returns ``(int, None)`` or ``(None, Response)``.

    Follower counts are stored on a ``PositiveIntegerField``, so negatives are rejected
    here with a clean 400 rather than bubbling up as a DB error.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None, Response(
            {"message": f"{field_name} must be a non-negative integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if count < 0:
        return None, Response(
            {"message": f"{field_name} cannot be negative."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return count, None


# ── points curve lives in scoring/engine.py, never re-implemented here ──
# social_media_points (the spec curve) is owned by scoring/engine.py; this helper only calls it.
# aggregation ADDS these points to the quarterly score ONLY when the snapshot is_verified — which
# is why verify/unverify enqueue a recalc.
def _recompute_social(snap):
    """Re-derive ``combined_followers`` + ``social_media_pts`` from the two raw counts.

    The followers→points invariant (see module docstring): combined = ig + tiktok, then
    points = engine.social_media_points(combined) clamped 0..10. Call this AFTER setting
    the follower counts and BEFORE ``snap.save()`` so the stored row is always consistent.
    """
    snap.combined_followers = snap.instagram_followers + snap.tiktok_followers
    snap.social_media_pts = engine.social_media_points(snap.combined_followers)


def _enqueue_quarterly_recalc(team_id, season_id):
    """Schedule the team's quarterly recalc AFTER the current transaction commits (§18).

    Social points feed ONLY the quarterly score (§7.3), so we enqueue just the quarterly
    leg via ``enqueue_team`` (which also dispatches monthly, but a team with no monthly
    activity is harmlessly re-ranked). Never recalc inline — the on_commit hook reads the
    committed snapshot and dedups bursts via the Redis lock in ``tasks``.
    """
    transaction.on_commit(
        lambda: tasks.enqueue_team(team_id, recalc.current_month(), season_id)
    )


# ───────────────────────── GET seasons/<id>/social/  (list, read-only) ─────────────────────────
@api_view(["GET"])
def social_list(request, season_id):
    """List every team with its social snapshot for the season (newest-named last). Read-only.

    Auth required (ranking admins) but — being read-only — this endpoint skips the reason
    gate and the audit write. We list ALL teams LEFT JOIN their season snapshot so the
    admin sees not-yet-connected teams too (each renders as a "connected: false" row).

    Paginated with the canonical envelope ({"results": [...], "pagination": meta, ...}).
    The team list is the paginated query; we batch-load the matching snapshots for just
    that page (one query) to avoid an N+1 over ``TeamSocialSnapshot``.
    """
    # auth only — read-only endpoint skips the reason gate and the audit write.
    user, err = _auth(request)
    if err:
        return err

    season, err = _get_season_or_404(season_id)
    if err:
        return err

    # Page the TEAM list (stable order by name); snapshots are joined per page below.
    teams_qs = Team.objects.all().order_by("team_name")
    teams, meta = paginate(request, teams_qs)

    # Batch-load this page's snapshots in one query, keyed by team_id (no N+1).
    page_team_ids = [t.team_id for t in teams]
    snaps_by_team = {
        s.team_id: s
        for s in TeamSocialSnapshot.objects
        .filter(season=season, team_id__in=page_team_ids)
        .select_related("verified_by")
    }

    return Response({
        "results": [serialize_social_row(t, snaps_by_team.get(t.team_id)) for t in teams],
        "pagination": meta,
        "season_id": season.pk,
    })


# ───────────────────────── PATCH seasons/<id>/social/<team_id>/  (admin edit) ─────────────────────────
@api_view(["PATCH"])
def social_edit(request, season_id, team_id):
    """Admin corrects a team's follower counts / handles for the season.

    Ranking admins (head_admin OR metrics_admin — default ``_auth`` set). Body:
      * ``instagram_followers`` (required, non-negative int)
      * ``tiktok_followers``    (required, non-negative int)
      * ``instagram_handle``    (optional — only updated if present)
      * ``tiktok_handle``       (optional — only updated if present)
      * ``reason``              (mandatory audit reason)

    ``get_or_create`` the snapshot (an admin may enter counts before a team ever
    self-connects), set the fields, re-derive combined + points, audit as
    ``social_media``/``edit``, then enqueue the quarterly recalc on commit (the counts
    feed the score whenever the snapshot is verified).
    """
    # (1) auth — default ranking-admin set.
    user, err = _auth(request)
    if err:
        return err

    # (2) mandatory audit reason (this write can change the computed score).
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season_or_404(season_id)
    if err:
        return err
    team, err = _get_team_or_404(team_id)
    if err:
        return err

    data = request.data

    # ── validate the two required follower counts ──
    ig_followers, err = _parse_followers(data.get("instagram_followers"), "instagram_followers")
    if err:
        return err
    tt_followers, err = _parse_followers(data.get("tiktok_followers"), "tiktok_followers")
    if err:
        return err

    # get_or_create so an admin can enter counts before the team self-connects.
    snap, _created = TeamSocialSnapshot.objects.get_or_create(team=team, season=season)
    before = _snapshot_audit_state(snap)  # snapshot BEFORE any mutation for the audit log.

    # ── apply counts + optional handles, then re-derive combined + points ──
    snap.instagram_followers = ig_followers
    snap.tiktok_followers = tt_followers
    if "instagram_handle" in data:
        snap.instagram_handle = (data.get("instagram_handle") or "").strip()
    if "tiktok_handle" in data:
        snap.tiktok_handle = (data.get("tiktok_handle") or "").strip()
    _recompute_social(snap)  # combined = ig + tiktok; points = engine.social_media_points(combined)

    # (3) the write + (4) audit, atomically.
    with transaction.atomic():
        snap.save()
        after = _snapshot_audit_state(snap)
        _audit(
            user, "social_media", "edit", reason,
            object_ref=snap.pk, before=before, after=after, season=season,
        )

    # (5) social_media_pts feeds the quarterly score → recalc after commit.
    _enqueue_quarterly_recalc(team.team_id, season.season_id)

    return Response(serialize_social_row(team, snap))


# ───────────────────────── POST seasons/<id>/social/<team_id>/verify/ ─────────────────────────
@api_view(["POST"])
def social_verify(request, season_id, team_id):
    """Mark a team's social snapshot verified — its points now COUNT toward the score.

    Ranking admins (default ``_auth`` set). Body: ``reason`` (mandatory). The snapshot
    must already exist (a team self-connects or an admin enters counts first); verifying
    a never-connected team is a 404. Sets ``is_verified=True`` + ``verified_by=user``,
    re-derives points (defensive — keeps the invariant if counts drifted), audits as
    ``social_media``/``verify``, then enqueues a recalc on commit so the now-verified
    social points are folded into the quarterly score.
    """
    # (1) auth — default ranking-admin set.
    user, err = _auth(request)
    if err:
        return err

    # (2) mandatory audit reason.
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season_or_404(season_id)
    if err:
        return err
    team, err = _get_team_or_404(team_id)
    if err:
        return err

    # Must already have a snapshot to verify — don't conjure one on verify.
    snap = TeamSocialSnapshot.objects.filter(team=team, season=season).first()
    if not snap:
        return Response(
            {"message": "No social snapshot to verify — the team must connect (or an admin must enter counts) first."},
            status=status.HTTP_404_NOT_FOUND,
        )

    before = _snapshot_audit_state(snap)

    # mark verified + stamp the verifying admin; re-derive points defensively.
    snap.is_verified = True
    snap.verified_by = user
    _recompute_social(snap)

    # (3) the write + (4) audit, atomically.
    with transaction.atomic():
        snap.save()
        after = _snapshot_audit_state(snap)
        _audit(
            user, "social_media", "verify", reason,
            object_ref=snap.pk, before=before, after=after, season=season,
        )

    # (5) verified social now counts → recalc after commit.
    _enqueue_quarterly_recalc(team.team_id, season.season_id)

    return Response(serialize_social_row(team, snap))


# ───────────────────────── POST seasons/<id>/social/<team_id>/unverify/ ─────────────────────────
@api_view(["POST"])
def social_unverify(request, season_id, team_id):
    """Revoke verification — the team's social points DROP to 0 on the next recalc.

    Ranking admins (default ``_auth`` set). Body: ``reason`` (mandatory). Sets
    ``is_verified=False`` (the stored ``social_media_pts`` is kept for display, but the
    aggregation layer ignores it while unverified, so the contributed score becomes 0).
    404 if no snapshot exists. Audits as ``social_media``/``unverify``, then enqueues a
    recalc on commit so the score drop lands.
    """
    # (1) auth — default ranking-admin set.
    user, err = _auth(request)
    if err:
        return err

    # (2) mandatory audit reason.
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season_or_404(season_id)
    if err:
        return err
    team, err = _get_team_or_404(team_id)
    if err:
        return err

    snap = TeamSocialSnapshot.objects.filter(team=team, season=season).first()
    if not snap:
        return Response(
            {"message": "No social snapshot to unverify."},
            status=status.HTTP_404_NOT_FOUND,
        )

    before = _snapshot_audit_state(snap)
    snap.is_verified = False

    # (3) the write + (4) audit, atomically.
    with transaction.atomic():
        snap.save(update_fields=["is_verified"])
        after = _snapshot_audit_state(snap)
        _audit(
            user, "social_media", "unverify", reason,
            object_ref=snap.pk, before=before, after=after, season=season,
        )

    # (5) social drops to 0 (aggregation ignores unverified) → recalc after commit.
    _enqueue_quarterly_recalc(team.team_id, season.season_id)

    return Response(serialize_social_row(team, snap))


# ───────────────────────── POST seasons/<id>/social/<team_id>/connect/  (self-connect) ─────────────────────────
@api_view(["POST"])
def social_connect(request, season_id, team_id):
    """Self-connect — a team links its IG / TikTok handles (pending admin verification).

    Auth here stays on the ranking-admin set (head_admin OR metrics_admin) — this is the
    admin-side connect form. The captain-facing version of connect lives on the team
    dashboard (a separate, team-scoped surface) and is intentionally NOT in this module.

    Body:
      * ``instagram_handle``    (required)
      * ``tiktok_handle``       (required)
      * ``instagram_followers`` (optional, non-negative int — default 0)
      * ``tiktok_followers``    (optional, non-negative int — default 0)
      * ``reason``              (mandatory audit reason)

    ``get_or_create`` the snapshot, set the handles + ``connected_by_team=True``, force
    ``is_verified=False`` (a fresh/updated connect always re-enters the pending-verify
    state — admin must re-verify), re-derive combined + points, audit as
    ``social_media``/``connect``, then enqueue the recalc on commit (the connect resets
    verification, so any previously-counted social must be recomputed to 0).
    """
    # (1) auth — default ranking-admin set (admin-side connect; captain form lives on the team dashboard).
    user, err = _auth(request)
    if err:
        return err

    # (2) mandatory audit reason.
    reason, err = _require_reason(request)
    if err:
        return err

    season, err = _get_season_or_404(season_id)
    if err:
        return err
    team, err = _get_team_or_404(team_id)
    if err:
        return err

    data = request.data

    # ── handles are required for a connect ──
    ig_handle = (data.get("instagram_handle") or "").strip()
    tt_handle = (data.get("tiktok_handle") or "").strip()
    if not ig_handle and not tt_handle:
        return Response(
            {"message": "At least one of instagram_handle / tiktok_handle is required to connect."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── follower counts optional on connect (default 0 if omitted) ──
    if "instagram_followers" in data:
        ig_followers, err = _parse_followers(data.get("instagram_followers"), "instagram_followers")
        if err:
            return err
    else:
        ig_followers = 0
    if "tiktok_followers" in data:
        tt_followers, err = _parse_followers(data.get("tiktok_followers"), "tiktok_followers")
        if err:
            return err
    else:
        tt_followers = 0

    snap, _created = TeamSocialSnapshot.objects.get_or_create(team=team, season=season)
    before = _snapshot_audit_state(snap)

    # ── set handles + counts, flag self-connected, reset to pending-verify ──
    snap.instagram_handle = ig_handle
    snap.tiktok_handle = tt_handle
    snap.instagram_followers = ig_followers
    snap.tiktok_followers = tt_followers
    snap.connected_by_team = True
    snap.is_verified = False  # a (re)connect always needs a fresh admin verification
    _recompute_social(snap)

    # (3) the write + (4) audit, atomically.
    with transaction.atomic():
        snap.save()
        after = _snapshot_audit_state(snap)
        _audit(
            user, "social_media", "connect", reason,
            object_ref=snap.pk, before=before, after=after, season=season,
        )

    # (5) connect resets verification → recalc after commit (social recomputes to 0 until re-verified).
    _enqueue_quarterly_recalc(team.team_id, season.season_id)

    return Response(serialize_social_row(team, snap))
