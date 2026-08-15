"""
afc_sponsors.invites - inviting a sponsor's own people onto AFC (owner 2026-08-14).

WHY THIS MODULE EXISTS
    A Sponsor could only ever gain a member by picking an EXISTING AFC user by username. Brand
    contacts do not have AFC accounts, so in practice sponsors had nobody attached, and a
    sponsorship with "sponsor must approve registrations" on then had an approval queue that no
    one on the platform could clear. Inviting by EMAIL closes that: the sponsor's contact gets a
    mail, signs up normally, and lands with the access already granted.

THE TWO PATHS
    invite_contact()      the address already belongs to an AFC account  -> membership NOW, plus a
                          notification and a mail telling them where the dashboard is.
                          Nobody on AFC yet                              -> a pending
                          SponsorMemberInvite and a mail with the sign-up link.
    claim_invites_for_user()  called when an account is verified (afc_auth.views.verify_code).
                          Every pending, unexpired invite for that address becomes a real
                          SponsorMember, so the new account is already a sponsor manager on its
                          first login. Idempotent: claiming twice is a no-op.

WHO CAN INVITE: sponsor-admin, same as adding a member by username (afc_sponsors.views).

CONNECTS TO
    - models.SponsorMemberInvite / SponsorMember / Sponsor
    - afc_auth.views.send_email (THE email chokepoint; language is the recipient's own) and
      afc_auth.models.Notifications for the in-app copy
    - afc_sponsors.views.invite_member / list_invites / revoke_invite (the endpoints)
    - frontend: the Manage-sponsor dialog on /a/sponsors, via lib/sponsors.ts
"""
import logging
import secrets
import threading
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from afc_auth.models import Notifications, User

from .models import Sponsor, SponsorMember, SponsorMemberInvite

logger = logging.getLogger(__name__)

# How long an emailed invite stays usable. Long enough for a brand contact to get round to it,
# short enough that a forwarded inbox cannot hand out sponsor access next season.
INVITE_TTL_DAYS = 14


def _portal_url():
    """Where an invitee ends up once they have an account."""
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/sponsor/dashboard"


def _signup_url(token):
    """The sign-up link carrying the invite token.

    The token is not required to claim the invite (claiming matches on the verified EMAIL), so a
    mangled link still works as long as they sign up with the address the invite was sent to. It
    rides along so the front end can show whose invite this is.
    """
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/register?sponsor_invite={token}"


def _send(to_address, subject, html, language="en"):
    """Best-effort mail, on a DAEMON THREAD: a failing or slow SMTP must never fail (or stall)
    the invite that was just recorded.

    The thread matters as much as the try/except. send_email connects to SMTP synchronously, and
    a timing-out server has been measured holding a request for over a minute
    (afc_sponsors.engagements._notify_rejection carries the same note and the same fix). The
    invitation row is already written by the time this runs, so the admin gets their answer
    immediately and the mail catches up; if it never arrives, the invite can simply be re-sent,
    which the endpoint already handles without duplicating the row.
    """
    def _deliver():
        try:
            from afc_auth.views import send_email
            send_email(to_address, subject, html, language=language)
        except Exception as exc:  # noqa: BLE001 - mail is advisory here
            logger.warning("sponsor invite mail to %s failed: %s", to_address, exc)

    threading.Thread(target=_deliver, daemon=True).start()


def _existing_member_html(sponsor, portal):
    return (
        f"<p>You have been given access to the <strong>{sponsor.name}</strong> sponsor dashboard "
        f"on AFC.</p>"
        f"<p>Sign in and open <a href=\"{portal}\">your sponsor dashboard</a> to see the events "
        f"{sponsor.name} is sponsoring and to approve or reject the players registering for them."
        f"</p>"
    )


def _invitee_html(sponsor, signup, inviter_name):
    who = f"{inviter_name} at AFC" if inviter_name else "AFC"
    return (
        f"<p>{who} has invited you to manage <strong>{sponsor.name}</strong> on the African Free "
        f"Fire Community platform.</p>"
        f"<p><a href=\"{signup}\">Create your account</a> with this email address and you will "
        f"land straight on the {sponsor.name} sponsor dashboard, where you can see the events you "
        f"are sponsoring and approve or reject the players registering for them.</p>"
        f"<p>This invitation expires in {INVITE_TTL_DAYS} days.</p>"
    )


def invite_contact(sponsor: Sponsor, email: str, role: str, invited_by=None):
    """Invite `email` to manage `sponsor`. Returns (result_dict, error_message).

    result_dict["outcome"] is one of:
      "member_added"   the address already had an AFC account, so access is live now
      "invited"        a pending invite was created and mailed
      "already_member" nothing to do
      "already_invited" the pending invite was re-sent rather than duplicated
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None, "A valid email address is required."
    if role not in ("owner", "member"):
        return None, "role must be owner or member."

    existing_user = User.objects.filter(email__iexact=email).first()
    if existing_user:
        member, created = SponsorMember.objects.get_or_create(
            sponsor=sponsor, user=existing_user,
            defaults={"role": role, "status": "active"},
        )
        if not created and member.status == "active":
            return {"outcome": "already_member", "username": existing_user.username}, None
        if not created:
            member.status, member.role = "active", role
            member.save(update_fields=["status", "role"])
        Notifications.objects.create(
            user=existing_user,
            notification_type="sponsor_access",
            title="Sponsor dashboard access",
            message=f"You now have access to the {sponsor.name} sponsor dashboard.",
        )
        _send(
            existing_user.email,
            f"You now manage {sponsor.name} on AFC",
            _existing_member_html(sponsor, _portal_url()),
            language=getattr(existing_user, "language", None) or "en",
        )
        return {
            "outcome": "member_added",
            "username": existing_user.username,
            "member_id": member.id,
        }, None

    pending = SponsorMemberInvite.objects.filter(
        sponsor=sponsor, email__iexact=email, status="pending",
    ).first()
    if pending:
        # Re-send rather than pile up rows, and push the expiry out from now.
        pending.expires_at = timezone.now() + timedelta(days=INVITE_TTL_DAYS)
        pending.role = role
        pending.save(update_fields=["expires_at", "role"])
        invite = pending
        outcome = "already_invited"
    else:
        invite = SponsorMemberInvite.objects.create(
            sponsor=sponsor, email=email, role=role,
            token=secrets.token_urlsafe(32)[:64],
            invited_by=invited_by,
            expires_at=timezone.now() + timedelta(days=INVITE_TTL_DAYS),
        )
        outcome = "invited"

    inviter_name = getattr(invited_by, "full_name", "") or getattr(invited_by, "username", "")
    _send(
        email,
        f"You have been invited to manage {sponsor.name} on AFC",
        _invitee_html(sponsor, _signup_url(invite.token), inviter_name),
    )
    return {"outcome": outcome, "invite_id": invite.id, "email": invite.email}, None


def claim_invites_for_user(user):
    """Turn every pending, unexpired invite for this user's email into a real membership.

    Called from the account-verification path, so a person invited before they had an account is
    already a sponsor manager the first time they sign in. Best-effort and idempotent: it never
    raises into the caller, because failing to attach a sponsor must not block a signup.
    Returns the number of memberships created.
    """
    created = 0
    try:
        now = timezone.now()
        invites = SponsorMemberInvite.objects.select_related("sponsor").filter(
            email__iexact=(user.email or ""), status="pending", expires_at__gt=now,
        )
        for invite in invites:
            member, was_created = SponsorMember.objects.get_or_create(
                sponsor=invite.sponsor, user=user,
                defaults={"role": invite.role, "status": "active"},
            )
            if not was_created and member.status != "active":
                member.status, member.role = "active", invite.role
                member.save(update_fields=["status", "role"])
            invite.status = "accepted"
            invite.accepted_at = now
            invite.accepted_user = user
            invite.save(update_fields=["status", "accepted_at", "accepted_user"])
            Notifications.objects.create(
                user=user,
                notification_type="sponsor_access",
                title="Sponsor dashboard access",
                message=f"You now have access to the {invite.sponsor.name} sponsor dashboard.",
            )
            created += 1
    except Exception as exc:  # noqa: BLE001 - never block a signup on this
        logger.warning("claiming sponsor invites for %s failed: %s", getattr(user, "email", "?"), exc)
    return created
