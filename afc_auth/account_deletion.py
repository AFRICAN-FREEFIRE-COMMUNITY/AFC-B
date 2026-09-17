"""
afc_auth.account_deletion - soft-deleting an account, and putting it back.

WHAT THE OWNER ASKED FOR (2026-09-14, inbox #20)
    "please let there be an option for AFC accounts to be deleted, users can delete their
    accounts, of course its to be soft deleted, head admins should be able to restore it back
    and a new user will be able to use some info from that account."

THE THREE PROMISES, and how each is kept here
    SOFT       The User row is never removed. Everything that points at it (results, transfer
               history, orders, audit rows, support tickets) keeps pointing at it. The row is
               marked status="deleted" with deleted_at set, every SessionToken is dropped, the
               password is made unusable and the outside accounts are unlinked, so nobody can
               sign in as it or through it.
    RELEASED   The unique identity columns are copied to a DeletedAccount row and replaced by
               tombstones on the User row: username "Deleted player <id>", email
               "deleted-<id>@deleted.invalid", uid and discord_id NULL, the profile's WhatsApp
               number blank, ConnectedAccount rows deleted (archived as JSON). A fresh signup with
               the same in-game name, email, UID, Discord or WhatsApp then passes the ordinary
               uniqueness checks untouched: nothing in signup() had to learn about deletion.
    RESTORABLE A head admin restores from the DeletedAccount row. The restore is all or nothing:
               if a new account has since taken one of the released values, it is refused and
               names the field (RestoreConflict), because an account restored with a tombstone
               email cannot receive mail and an account restored under someone else's in-game
               name is a different bug.

WHAT A PERSON MUST SETTLE FIRST (deletion_blockers)
    Deletion is refused while the account is a team member (leave first: the team's own
    leave rules, roster locks and the transfer window then apply exactly once, in exit_team),
    owns an organization, a sponsor or a shop, is suspended, is banned, or holds a staff role.
    Each blocker carries a code the frontend translates. A player with nothing to hand over
    deletes in one step.

HOW IT CONNECTS
    Called by afc_auth/views_account_deletion.py (POST auth/delete-account/, the admin list and
    restore). Reads/writes User, UserProfile (via canonical_profile), SessionToken,
    ConnectedAccount and DeletedAccount from afc_auth.models; reads TeamMembers,
    OrganizationMember, SponsorMember, Vendor and BannedPlayer to decide the blockers. Login
    (afc_auth.views.login) asks find_deleted_by_identifier so a person who tries their old
    email is told the account was deleted rather than "invalid credentials". The public player
    read (afc_player.views.get_public_player_stats), search_users and the broadcast audience
    exclude status="deleted".
"""
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    BannedPlayer, ConnectedAccount, DeletedAccount, SessionToken, User, UserProfile,
    canonical_profile,
)

logger = logging.getLogger(__name__)

TOMBSTONE_EMAIL_DOMAIN = "deleted.invalid"   # RFC 2606 reserved: can never receive mail


def tombstone_username(user_id: int) -> str:
    """What every leaderboard, result table and history row prints for a deleted account.

    82 backend sites and 83 frontend files print `.username` as it is, so the name itself has to
    read as what it is rather than every site learning about deletion: "Deleted player 145"
    (owner follow-up 2026-09-17). The number keeps it unique (the column is) and lets a head admin
    find the archive row; the old address /players/deleted-145 was already a 404.
    """
    return f"Deleted player {user_id}"


def tombstone_email(user_id: int) -> str:
    return f"deleted-{user_id}@{TOMBSTONE_EMAIL_DOMAIN}"


class RestoreConflict(Exception):
    """A released identity value has been taken by another account since the deletion."""

    def __init__(self, fields):
        self.fields = list(fields)
        super().__init__("taken: " + ", ".join(self.fields))


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  What must be settled before an account can go
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def deletion_blockers(user):
    """Everything that stops this person deleting their account right now.

    Returns a list of ``{"code", "message"}`` dicts, empty when they may go ahead. The message
    is the English fallback; the frontend translates by code (R35 / R44).
    """
    from afc_organizers.models import OrganizationMember
    from afc_shop.models import Vendor
    from afc_sponsors.models import SponsorMember
    from afc_team.models import Team, TeamMembers

    blockers = []
    if user.status == "suspended":
        blockers.append({"code": "account_suspended",
                         "message": "A suspended account cannot be deleted. Contact support."})
    if BannedPlayer.objects.filter(banned_player=user, is_active=True,
                                   ban_end_date__gt=timezone.now()).exists():
        blockers.append({"code": "account_banned",
                         "message": "A banned account cannot be deleted while the ban runs."})
    if (user.role or "player") != "player" or user.userroles.exists():
        blockers.append({"code": "account_is_staff",
                         "message": "Staff accounts are closed by a head admin. Ask them to remove your role first."})
    if Team.objects.filter(team_owner=user).exists():
        blockers.append({"code": "owns_team",
                         "message": "You own a team. Transfer ownership or disband it first."})
    elif TeamMembers.objects.filter(member=user).exists():
        blockers.append({"code": "in_team",
                         "message": "Leave your team first."})
    if OrganizationMember.objects.filter(user=user, role="owner", status="active").exists():
        blockers.append({"code": "owns_organization",
                         "message": "You own an organization. Hand it to another member first."})
    if SponsorMember.objects.filter(user=user, role="owner", status="active").exists():
        blockers.append({"code": "owns_sponsor",
                         "message": "You own a sponsor account. Hand it to another member first."})
    if Vendor.objects.filter(user=user).exists():
        blockers.append({"code": "owns_shop",
                         "message": "You have a shop. Ask support to close it first."})
    return blockers


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Deleting
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _profiles(user):
    """Every UserProfile row of the user. Duplicate rows exist in production (see
    canonical_profile), and a released WhatsApp number must leave all of them."""
    return UserProfile.objects.filter(user=user)


def soft_delete_user(user, *, reason="", by=None):
    """Mark the account deleted, archive its identity, release every unique column.

    Idempotent guard: a user already deleted is returned as is (the open archive row). The
    caller has checked deletion_blockers; this function only does the write. Runs in one
    transaction so an account is never half-deleted: either every column is released and the
    archive exists, or nothing changed.
    """
    if user.status == "deleted":
        return DeletedAccount.objects.filter(user=user, restored_at__isnull=True).first()

    with transaction.atomic():
        profile = canonical_profile(user)
        linked = [
            {
                "provider": c.provider, "provider_user_id": c.provider_user_id,
                "username": c.username, "email": c.email, "avatar_url": c.avatar_url,
                "scopes": c.scopes or [],
            }
            for c in ConnectedAccount.objects.filter(user=user)
        ]
        archive = DeletedAccount.objects.create(
            user=user,
            username=user.username,
            email=user.email,
            full_name=user.full_name or "",
            uid=user.uid,
            discord_id=user.discord_id,
            discord_username=user.discord_username,
            whatsapp_number=(profile.whatsapp_number if profile else "") or "",
            password_hash=user.password or "",
            connected_accounts=linked,
            reason=(reason or "")[:500],
            deleted_by=by or user,
        )

        # Release: tombstone every unique column, so a new signup can take the old values.
        user.username = tombstone_username(user.user_id)
        user.email = tombstone_email(user.user_id)
        user.uid = None
        user.discord_id = None
        user.discord_username = None
        user.discord_avatar = None
        user.discord_connected = False
        user.status = "deleted"
        user.deleted_at = timezone.now()
        user.set_unusable_password()
        user.save()

        _profiles(user).update(whatsapp_number="")
        ConnectedAccount.objects.filter(user=user).delete()
        # Signed out everywhere. A token that survived would still resolve to this row.
        SessionToken.objects.filter(user=user).delete()

        # Memberships that are not ownerships (ownership is a blocker): a deleted person is not
        # somebody's sub-organizer or sponsor member any more. They can be invited again later.
        from afc_organizers.models import OrganizationMember
        from afc_sponsors.models import SponsorMember
        OrganizationMember.objects.filter(user=user).delete()
        SponsorMember.objects.filter(user=user).delete()

    transaction.on_commit(lambda: _send_deleted_email(archive))
    return archive


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Restoring
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def restore_conflicts(archive):
    """The released values another account has taken since the deletion. Empty = safe."""
    others = User.objects.exclude(pk=archive.user_id)
    taken = []
    if others.filter(username=archive.username).exists():
        taken.append("username")
    if others.filter(email__iexact=archive.email).exists():
        taken.append("email")
    if archive.uid and others.filter(uid=archive.uid).exists():
        taken.append("uid")
    if archive.discord_id and others.filter(discord_id=archive.discord_id).exists():
        taken.append("discord_id")
    if archive.whatsapp_number and UserProfile.objects.exclude(user_id=archive.user_id).filter(
            whatsapp_number=archive.whatsapp_number).exists():
        taken.append("whatsapp_number")
    for linked in archive.connected_accounts or []:
        if ConnectedAccount.objects.filter(
            provider=linked.get("provider", ""), provider_user_id=linked.get("provider_user_id", ""),
        ).exclude(user_id=archive.user_id).exists():
            taken.append(f"connected_account:{linked.get('provider', '')}")
    return taken


def restore_user(user, *, by):
    """Put a deleted account back exactly as it was, or refuse with the fields that are taken.

    Raises RestoreConflict (with the field names) when any released value now belongs to
    another account; nothing is written in that case. Returns the closed archive row.
    """
    archive = DeletedAccount.objects.filter(user=user, restored_at__isnull=True).order_by("-deleted_at").first()
    if user.status != "deleted" or archive is None:
        raise ValueError("This account is not deleted.")
    taken = restore_conflicts(archive)
    if taken:
        raise RestoreConflict(taken)

    with transaction.atomic():
        user.username = archive.username
        user.email = archive.email
        user.full_name = archive.full_name or user.full_name
        user.uid = archive.uid
        user.discord_id = archive.discord_id
        user.discord_username = archive.discord_username
        user.discord_connected = bool(archive.discord_id)
        user.status = "active"
        user.deleted_at = None
        if archive.password_hash:
            user.password = archive.password_hash
        user.save()
        if archive.whatsapp_number:
            profile = canonical_profile(user, create=True)
            profile.whatsapp_number = archive.whatsapp_number
            profile.save(update_fields=["whatsapp_number"])
        for linked in archive.connected_accounts or []:
            ConnectedAccount.objects.create(
                user=user,
                provider=linked.get("provider", ""),
                provider_user_id=linked.get("provider_user_id", ""),
                username=linked.get("username", "") or "",
                email=linked.get("email", "") or "",
                avatar_url=linked.get("avatar_url", "") or "",
                scopes=linked.get("scopes") or [],
            )
        archive.restored_at = timezone.now()
        archive.restored_by = by
        archive.save(update_fields=["restored_at", "restored_by"])

    transaction.on_commit(lambda: _send_restored_email(user))
    return archive


def find_deleted_by_identifier(value):
    """The open archive row whose email, in-game name or UID matches a sign-in identifier, or
    None. Lets login say "this account was deleted" instead of "invalid credentials"."""
    value = (value or "").strip()
    if not value:
        return None
    return (
        DeletedAccount.objects.filter(restored_at__isnull=True)
        .filter(Q(email__iexact=value) | Q(username=value) | Q(uid=value))
        .order_by("-deleted_at")
        .first()
    )


def serialize_deleted_account(archive):
    """One shape for the admin list and the restore answer (R24)."""
    return {
        "user_id": archive.user_id,
        "username": archive.username,
        "email": archive.email,
        "full_name": archive.full_name,
        "uid": archive.uid,
        "discord_username": archive.discord_username,
        "whatsapp_number": archive.whatsapp_number,
        "reason": archive.reason,
        "deleted_at": archive.deleted_at.isoformat() if archive.deleted_at else None,
        "self_service": archive.deleted_by_id == archive.user_id,
        "restored_at": archive.restored_at.isoformat() if archive.restored_at else None,
        "restored_by": archive.restored_by.username if archive.restored_by_id else None,
        "conflicts": restore_conflicts(archive) if archive.restored_at is None else [],
    }


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  The two emails (hand-written en/fr/pt in afc_auth/email_i18n.py, sent through the
#     single chokepoint send_email; imported lazily because views imports this module's models)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _send_deleted_email(archive):
    try:
        from afc_support.notify import _shell_rows
        from .email_i18n import copy_for, subject_for
        from .views import SITE_URL, _email_shell, send_email

        lang = archive.user.language or "en"
        c = copy_for("account_deleted", lang)
        html = _email_shell(
            _shell_rows(c["heading"], [c["intro"], c["restore"], c["reuse"]],
                        f"{SITE_URL}/support", c["cta"], c["disclaimer"]),
            "green",
        )
        send_email(archive.email, subject_for("account_deleted", lang), html,
                   language=lang, prelocalized=True)
    except Exception:
        logger.exception("account_deletion: could not email the deleted-account notice for user %s",
                         archive.user_id)


def _send_restored_email(user):
    try:
        from afc_support.notify import _shell_rows
        from .email_i18n import copy_for, subject_for
        from .views import SITE_URL, _email_shell, send_email

        lang = user.language or "en"
        c = copy_for("account_restored", lang)
        html = _email_shell(
            _shell_rows(c["heading"], [c["intro"].format(username=user.username), c["password"]],
                        f"{SITE_URL}/login", c["cta"], c["disclaimer"]),
            "green",
        )
        send_email(user.email, subject_for("account_restored", lang), html,
                   language=lang, prelocalized=True)
    except Exception:
        logger.exception("account_deletion: could not email the restored-account notice for user %s",
                         user.user_id)
