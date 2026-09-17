"""
afc_auth.views_account_deletion - the HTTP surface of account deletion (inbox #20).

ROUTES (mounted by afc_auth/urls.py under the ``auth/`` prefix)
    GET  delete-account/                       -> delete_account_preflight  (what stands in the way)
    POST delete-account/                       -> delete_account            (the person deletes their own)
    GET  admin/deleted-accounts/               -> admin_list_deleted_accounts (head admins)
    POST admin/deleted-accounts/<int:user_id>/restore/ -> admin_restore_account (head admins)

The rules live in afc_auth/account_deletion.py; these views only authenticate, validate the
confirmation, translate the outcomes into status codes with a ``code`` (R35 / R44), and audit.

Consumed by: the "Delete my account" card on /profile/security (frontend
app/(user)/profile/_components/DeleteAccountCard.tsx) and the admin page
/a/players/deleted (app/(a)/a/players/deleted/page.tsx).
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .account_deletion import (
    RestoreConflict, deletion_blockers, restore_user, serialize_deleted_account,
    soft_delete_user,
)
from .audit import set_audit
from .models import AdminHistory, DeletedAccount, User
from .views import validate_token

_HEAD_GRANULAR_ROLES = ("super_admin", "head_admin")


def _actor(request):
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ", 1)[1].strip())


def _require_user(request):
    user = _actor(request)
    if not user:
        return None, Response({"message": "Invalid or expired session token.", "code": "auth_required"},
                              status=status.HTTP_401_UNAUTHORIZED)
    return user, None


def _is_head_admin(user) -> bool:
    """Head admins and above, the same test afc_support uses for its audit."""
    if getattr(user, "is_superuser", False):
        return True
    try:
        return user.userroles.filter(role__role_name__in=_HEAD_GRANULAR_ROLES).exists()
    except Exception:
        return False


def _require_head_admin(request):
    user, err = _require_user(request)
    if err:
        return None, err
    if not _is_head_admin(user):
        return None, Response({"message": "Only a head admin can do this.", "code": "head_admin_required"},
                              status=status.HTTP_403_FORBIDDEN)
    return user, None


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  The person's own account
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def delete_account_preflight(request):
    """What stands between this person and deleting their account.

    Response 200 ``{"can_delete": bool, "blockers": [{"code", "message"}],
                    "needs_password": bool}``. ``needs_password`` is False for an account that
    signed up through Google or Discord and never set a password; such a person confirms by
    typing their in-game name instead. The card reads this on open so the dialog can say
    "leave your team first" before anybody types anything.
    """
    user, err = _require_user(request)
    if err:
        return err
    blockers = deletion_blockers(user)
    return Response({
        "can_delete": not blockers,
        "blockers": blockers,
        "needs_password": user.has_usable_password(),
        "username": user.username,
    })


@api_view(["POST"])
def delete_account(request):
    """Soft-delete the signed-in account.

    Request:: {"password": "...", "confirm_username": "their in-game name", "reason": "optional"}
        ``password`` is required when the account has one; ``confirm_username`` always.
    Response 200 ``{"message", "deleted_at"}``; every session is gone, so the frontend signs out.
    Response 400 ``code`` = ``confirm_mismatch`` / ``password_required`` / ``password_wrong``.
    Response 409 ``{"code": "deletion_blocked", "blockers": [...]}`` while something must be
             settled first (a team, an organization, a ban, a staff role).
    """
    user, err = _require_user(request)
    if err:
        return err

    blockers = deletion_blockers(user)
    if blockers:
        return Response({"message": blockers[0]["message"], "code": "deletion_blocked",
                         "blockers": blockers}, status=status.HTTP_409_CONFLICT)

    confirm = (request.data.get("confirm_username") or "").strip()
    if confirm != user.username:
        return Response({"message": "Type your in-game name exactly to confirm.",
                         "code": "confirm_mismatch"}, status=status.HTTP_400_BAD_REQUEST)
    if user.has_usable_password():
        password = request.data.get("password") or ""
        if not password:
            return Response({"message": "Your password is required.", "code": "password_required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not user.check_password(password):
            return Response({"message": "That password is wrong.", "code": "password_wrong"},
                            status=status.HTTP_400_BAD_REQUEST)

    reason = (request.data.get("reason") or "").strip()
    archive = soft_delete_user(user, reason=reason, by=user)
    return Response({"message": "Your account has been deleted.",
                     "deleted_at": archive.deleted_at.isoformat()})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Head admins: the list, and the way back
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_deleted_accounts(request):
    """Every deletion, newest first, with whether it can be restored as it was.

    Query: ``q`` (matches the archived username, email or UID), ``include_restored`` (default
    false), ``limit`` (default 50, max 200), ``offset``.
    Response 200 ``{"results": [serialize_deleted_account...], "total_count", "has_more",
                    "next_offset"}``.
    """
    user, err = _require_head_admin(request)
    if err:
        return err
    qs = DeletedAccount.objects.select_related("user", "restored_by")
    if (request.query_params.get("include_restored") or "").lower() not in ("1", "true", "yes"):
        qs = qs.filter(restored_at__isnull=True)
    q = (request.query_params.get("q") or "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(uid__icontains=q)
                       | Q(full_name__icontains=q))
    try:
        limit = max(1, min(int(request.query_params.get("limit", 50)), 200))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        return Response({"message": "limit and offset must be integers.", "code": "bad_paging"},
                        status=status.HTTP_400_BAD_REQUEST)
    total = qs.count()
    rows = [serialize_deleted_account(a) for a in qs[offset:offset + limit]]
    return Response({"results": rows, "total_count": total,
                     "has_more": offset + limit < total, "next_offset": offset + limit})


@api_view(["POST"])
def admin_restore_account(request, user_id):
    """Restore a deleted account exactly as it was.

    Response 200 ``{"message", "account": serialize_deleted_account}`` (the closed archive row).
    Response 404 when no such user or it is not deleted; 409 ``{"code": "restore_conflict",
             "fields": [...]}`` when another account has taken a released value since (the
             person must be reached another way, or the newer account renamed first).
    Audited: AdminHistory row + the admin audit middleware (set_audit).
    """
    admin, err = _require_head_admin(request)
    if err:
        return err
    target = User.objects.filter(pk=user_id, status="deleted").first()
    if target is None:
        return Response({"message": "No deleted account with that id.", "code": "not_deleted"},
                        status=status.HTTP_404_NOT_FOUND)
    try:
        archive = restore_user(target, by=admin)
    except RestoreConflict as conflict:
        return Response({
            "message": "Another account now uses: " + ", ".join(conflict.fields)
                       + ". Resolve that first, then restore.",
            "code": "restore_conflict", "fields": conflict.fields,
        }, status=status.HTTP_409_CONFLICT)
    except ValueError as exc:
        return Response({"message": str(exc), "code": "not_deleted"}, status=status.HTTP_404_NOT_FOUND)

    set_audit(request, f"Restored the deleted account {archive.username} (ID: {target.user_id})")
    AdminHistory.objects.create(
        admin_user=admin, action="restored_account",
        description=f"Restored the deleted account {archive.username} (ID: {target.user_id})",
    )
    return Response({"message": f"{archive.username} is back.",
                     "account": serialize_deleted_account(archive)})
