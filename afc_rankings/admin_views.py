"""
Admin write API for rankings & tiering (Phase 2) — shared foundation.

This module holds the auth + audit helpers reused by every ranking admin write
endpoint (seasons, result markers, overrides, ghost teams, scoring config,
tournament tiers, run-evaluation). The public *read* API lives in ``views.py``;
mutations live here so the auth gate and the §16 audit trail are written once.

It deliberately matches the existing ``afc_auth.views`` style so the original dev
reads it without surprises:
  * function-based DRF views (``@api_view``), not class-based.
  * ``Bearer`` token -> ``validate_token()`` -> role gate -> ``Response({"message": ...})``.
  * manual-dict serialization (see ``serializers.py``), no DRF Serializer classes.

The only difference from ``afc_auth.views.require_admin`` is the role check: ranking
admin is gated on the granular ``UserRoles`` table (head_admin / metrics_admin), not
the coarse ``user.role``.

Standard shape of a mutating endpoint built on these helpers:

    @api_view(["POST"])
    def do_thing(request, ...):
        user, err = _auth(request)            # 401/403 short-circuit
        if err:
            return err
        reason, err = _require_reason(request)  # mandatory audit reason (>= 10 chars)
        if err:
            return err
        with transaction.atomic():
            before = {...}                    # snapshot for the audit log
            ... apply the write ...
            after = {...}
            _audit(user, "season", "create", reason, object_ref=obj.pk,
                   before=before, after=after, season=season)
        return Response({...})
"""
from rest_framework import status
from rest_framework.response import Response

from afc_auth.views import validate_token
from .models import RankingAuditLog

# Roles allowed to administer rankings (spec §15 / admin-synthesis §0.1).
# head_admin is the superset; metrics_admin co-administers the ranking system.
# Data-entry endpoints widen this set by passing e.g. roles=RANKING_ADMIN_ROLES + ("event_admin",).
RANKING_ADMIN_ROLES = ("head_admin", "metrics_admin")

# Every ranking write must carry a human reason — it is the body of the audit row (§16).
MIN_REASON_LEN = 10


def _has_role(user, *role_names):
    """True if the user holds ANY of the given granular roles (UserRoles table)."""
    return user.userroles.filter(role__role_name__in=role_names).exists()


def _auth(request, roles=RANKING_ADMIN_ROLES):
    """Validate the Bearer token and gate on ranking-admin roles.

    Returns ``(user, None)`` on success or ``(None, Response)`` on failure, so callers do::

        user, err = _auth(request)
        if err:
            return err

    Pass ``roles`` to override the allowed set for a specific endpoint (it should always
    include ``head_admin``). Mirrors ``afc_auth.views.require_admin`` but checks the
    granular ``UserRoles`` rather than the coarse ``user.role``.
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response(
            {"message": "Invalid or missing Authorization token."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if _has_role(user, *roles):
        return user, None
    return None, Response(
        {"message": "You do not have permission to manage rankings."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _require_reason(request, min_len=MIN_REASON_LEN):
    """Pull the mandatory audit reason off the request body, or return an error Response.

    Returns ``(reason, None)`` or ``(None, Response)``. Keeps the "every write is
    reason-gated" rule (§16) in one place so endpoints don't each re-implement it.
    """
    reason = (request.data.get("reason") or "").strip()
    if len(reason) < min_len:
        return None, Response(
            {"message": f"A reason of at least {min_len} characters is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return reason, None


def _audit(user, object_type, action, reason, *, object_ref="", before=None, after=None, season=None):
    """Write one ``RankingAuditLog`` row (§16 / §20) after a successful write.

    ``before`` / ``after`` are JSON snapshots of the changed object so the log is
    self-explanatory and hand-reversible. ``object_type`` must be one of
    ``RankingAuditLog.OBJECT_TYPES``. Keyword-only after ``reason`` to keep call sites tidy.
    """
    return RankingAuditLog.objects.create(
        object_type=object_type,
        object_ref=str(object_ref),
        action=action,
        reason=reason,
        before_snapshot=before or {},
        after_snapshot=after or {},
        changed_by=user,
        season=season,
    )
