# afc_auth/views_admin_identity.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ADMIN IDENTITY REPAIR - fixing a user's Free Fire UID and their account email (owner 2026-08-07)
#
# WHY THIS MODULE EXISTS
#   Support gets two recurring tickets it could not previously close:
#     1. "My UID is wrong / that UID is not mine." User.uid is unique and, in the live database,
#        9 rows carry values a spreadsheet import mangled (".4646454948", "-668075761",
#        "527.0848242", "7353194371.0" and one empty string), while 1,218 accounts have no UID at
#        all. The owner of a bad row cannot always fix it themselves: the identity lock in
#        edit_profile freezes username + uid while the player is committed to a live event, and a
#        value already taken by another row cannot be typed in anyway.
#     2. "I cannot get into my account." Before 2FA shipped there was no second factor to fall back
#        on, so a wrong or dead signup address is a permanent lockout: the user cannot run the
#        self-serve change-email flow (request_email_change) because that flow requires them to be
#        signed in and to read a code sent to the NEW address.
#
#   Changing somebody's email is an ACCOUNT TAKEOVER PRIMITIVE - password resets follow the address.
#   So it is built like one: the narrowest role gate on the site, a typed reason, a full audit trail,
#   every session killed, and 2FA that has to be taken down deliberately rather than walked around.
#
# ENDPOINTS (prefix auth/, wired in afc_auth/urls.py)
#   • GET  auth/admin/user-identity/<user_id>/   admin_user_identity   read UID + email + 2FA state
#   • POST auth/admin/set-user-uid/              admin_set_user_uid    edit OR remove a UID
#   • POST auth/admin/set-user-email/            admin_set_user_email  change an account email
#
# PERMISSION GATE (reused, not invented)
#   Every endpoint here calls views.require_head_admin - the SAME gate the audit log itself uses.
#   It allows head_admin, super_admin and a Django superuser, and nothing else. A plain
#   role=="admin" account, a moderator and a support account are all rejected with 403. On top of
#   that, the super_admin protection from views.edit_user_roles is reused verbatim: only a
#   super_admin may touch a user who already holds super_admin, so a head_admin cannot move a
#   super_admin's account onto an address they control.
#
# HOW IT CONNECTS
#   models   : User.uid / User.email / User.is_active, SessionToken, EmailChangeRequest,
#              TwoFactorSettings + TwoFactorChallenge + TwoFactorBackupCode, AdminHistory.
#   audit    : afc_auth.audit.set_audit supplies the human summary + the before/after detail fields
#              that AuditLogMiddleware writes onto the AuditLog row for this request. The legacy
#              AdminHistory row is written too, matching every other sensitive action in views.py.
#   email    : views.send_email (the single localized SMTP chokepoint) with the hand-authored fr/pt
#              copy from email_i18n ("admin_email_changed" + subject "email_updated_admin").
#   2FA      : afc_auth.two_factor.is_enabled_for decides whether the acknowledgement is required.
#   frontend : app/(a)/a/players/[id]/page.tsx - the "Edit email" and "Edit UID" controls on the
#              admin player-detail page, both rendered only for head_admin / super_admin.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import two_factor
from .audit import set_audit
from .models import (
    AdminHistory,
    EmailChangeRequest,
    SessionToken,
    TwoFactorChallenge,
    TwoFactorSettings,
    User,
)
from .views import (
    _has_active_event_registration,
    _is_super_admin,
    _user_role_names,
    email_admin_email_changed,
    is_valid_email,
    language_for_country,
    require_head_admin,
    send_email,
)
from .email_i18n import subject_for


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §0  Shared rules
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# A Free Fire UID is numeric, and User.uid is a CharField(max_length=15). Both new values typed here
# are held to that: digits only, 1 to 15 of them. This is deliberately STRICTER than edit_profile
# (which only checks uniqueness), because the whole reason this endpoint exists is to clean up rows
# where a spreadsheet turned a UID into ".4646454948" or "527.0848242". Letting an admin type
# another one of those would defeat the point. Reading an existing dirty value is untouched - the
# rule only applies to what gets written.
UID_MAX_LENGTH = 15

# The typed reason is mandatory and is what makes the audit trail answer "why", not just "what".
# Capped so the summary sentence it is folded into stays inside AuditLog.summary (255 chars); the
# full text is also stored verbatim in the audit metadata via set_audit(**details).
REASON_MAX_LENGTH = 200


def _login_ambiguity_clash(target, value):
    """The account (if any) whose USERNAME is `value`, which would make `value` an ambiguous login.

    WHY THIS EXISTS. Sign-in resolves ONE identifier against three columns at once
    (afc_auth/backends.py EmailOrUsernameModelBackend):

        User.objects.get(Q(username=x) | Q(uid=x) | Q(email__iexact=x))

    so if account A's uid (or email) equals account B's username, that string matches TWO rows,
    .get() raises MultipleObjectsReturned, and the backend refuses the login - for BOTH accounts,
    not just one. Uniqueness on the uid and email columns does NOT catch this: the collision is
    ACROSS columns.

    This is not hypothetical. In the live table 10 accounts already sit in exactly that state
    (their uid is another player's username, e.g. uid "9137457129" against the username
    "Kinglarry21"), and 106 usernames are well-formed email addresses. Since the whole point of
    this module is to END a lockout, it must not be able to create one, so both writes call this
    and refuse rather than hand the admin a fresh pair of locked-out users.

    Matching mirrors the backend deliberately: `username=` (not __iexact) because that is the exact
    lookup sign-in performs, and MySQL's case-insensitive collation makes both behave the same way.
    """
    return User.objects.exclude(pk=target.pk).filter(username=value).first()


def _reason(request):
    """The mandatory typed reason for this action, or (None, Response) when it is missing.

    Mirrors the watchlist add endpoint (views_watchlist.add_to_watchlist), which is the existing
    precedent on this codebase for "a sensitive admin action must carry a reason"."""
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return None, Response({"message": "A reason is required."}, status=status.HTTP_400_BAD_REQUEST)
    return reason[:REASON_MAX_LENGTH], None


def _target(user_id):
    """The user being acted on, or (None, Response) for a missing/unknown id."""
    if user_id in (None, ""):
        return None, Response({"message": "user_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        return User.objects.get(user_id=user_id), None
    except (User.DoesNotExist, ValueError, TypeError):
        return None, Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)


def _guard_target(admin_user, target):
    """The two rules about WHO may be acted on. Returns a Response to send, or None to proceed.

    1. Not yourself. Both capabilities here skip the ownership proof an ordinary user has to give
       (the self-serve flow re-checks the current password and the old address on file), so pointing
       them at your own account would be a way to move your own login somewhere else with no proof
       at all. Self-service already exists for both fields - profile settings for the UID, the
       "Change email" dialog for the email - so nothing is lost by refusing. It also keeps the audit
       trail honest: killing your own sessions mid-request would strip the actor off the audit row.
    2. Not a super_admin, unless you are one. Reused verbatim from views.edit_user_roles, where the
       same sentence already protects the top role from a head_admin.
    """
    if target.user_id == admin_user.user_id:
        return Response(
            {"message": "You can't use this on your own account. Change your own UID in profile settings and your own email with the Change email flow."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not _is_super_admin(admin_user) and "super_admin" in _user_role_names(target):
        return Response(
            {"message": "Only a super admin can change a super admin's account."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def _recipient_language(user):
    """The recipient's locale for send_email. Same three-step fallback every other transactional
    send in views.py uses: their explicit choice, else the language of their country, else English."""
    try:
        return user.language or language_for_country(user.country) or "en"
    except Exception:
        return "en"


def _two_factor_state(user):
    """(is_on, row) for a user's 2FA. `row` may be None when they never touched the setting."""
    return two_factor.is_enabled_for(user), two_factor.settings_for(user)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  Read - what the admin dialogs open onto
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_user_identity(request, user_id):
    """GET auth/admin/user-identity/<user_id>/  Bearer auth, HEAD ADMIN / SUPER ADMIN only.

    PURPOSE
      The state the two edit dialogs need before they let an admin type anything: the current UID
      and email, whether the account has two-factor authentication switched on (which forces an
      extra acknowledgement on an email change), whether the player is mid-event (their UID is
      frozen for their own edits), and how many live sessions the change would end.

    REQUEST   no body. `user_id` is the User.user_id of the account being inspected.
    RESPONSE  200 {
                user_id, username, email, uid,
                is_active,            # False = never-verified signup, an email change reactivates it
                two_factor_enabled,   # True -> set-user-email needs disable_two_factor: true
                active_sessions,      # how many logins would be ended by an email change
                identity_locked,      # player is registered for a live event
                is_super_admin        # target holds super_admin (only a super_admin may act on them)
              }
              400/401 bad or missing token · 403 not a head admin · 404 unknown user.

    AUTH      views.require_head_admin (head_admin, super_admin or a Django superuser).
    CONSUMED BY  frontend app/(a)/a/players/[id]/page.tsx, fetched when the page loads for a
                 head admin and re-fetched after either write so the dialogs never show stale state.
    """
    admin_user, err = require_head_admin(request)
    if err:
        return err

    target, err = _target(user_id)
    if err:
        return err

    two_factor_on, _row = _two_factor_state(target)
    return Response(
        {
            "user_id": target.user_id,
            "username": target.username,
            "email": target.email,
            "uid": target.uid or "",
            "is_active": bool(target.is_active),
            "two_factor_enabled": two_factor_on,
            "active_sessions": SessionToken.objects.filter(
                user=target, expires_at__gt=timezone.now()).count(),
            "identity_locked": _has_active_event_registration(target),
            "is_super_admin": "super_admin" in _user_role_names(target),
        },
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Free Fire UID - edit or remove
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def admin_set_user_uid(request):
    """POST auth/admin/set-user-uid/  Bearer auth, HEAD ADMIN / SUPER ADMIN only.

    PURPOSE
      Correct or clear the Free Fire UID on somebody else's account. Support needs both halves:
      "this UID is a typo" (edit) and "this UID belongs to another player, take it off me"
      (remove, which also frees the value for its real owner because User.uid is unique).

    REQUEST   { user_id: int, uid: str, reason: str }
              `uid` MUST be present. An empty string (or null) means REMOVE. The key being absent is
              rejected rather than treated as a removal - edit_profile was silently wiping UIDs
              exactly that way in June 2026, and one blanked UID fails the event "require player
              UID" gate, so this endpoint refuses to guess.
    RESPONSE  200 { message, uid, previous_uid, identity_locked }
              400 missing/invalid uid, non-numeric, too long, unchanged, already taken, equal to
                  another account's in-game name (see _login_ambiguity_clash), no reason
              401 bad session · 403 not a head admin / self / target is super_admin · 404 no user.

    WHAT REMOVAL LEAVES BEHIND (the account stays coherent, it is not half-registered)
      User.uid is null=True/blank=True, so NULL is a first-class "not set yet" - the same state
      every account has before its owner fills the field in, and the same state 1,218 live accounts
      are in right now. Nothing cascades off it: the user keeps their team, results and history
      (those key off user_id). NULL is stored rather than "" because the column is UNIQUE and a
      second empty string would collide with the one row that already holds "".

      Two consequences, and the player keeps a working account through both:
        • THEY LOSE ONE WAY TO SIGN IN. Sign-in accepts in-game name, UID or email
          (afc_auth/backends.py EmailOrUsernameModelBackend), so a player used to typing their UID
          must now type their name or their email. Both still work, and the email is the one this
          module guarantees is live, so nobody is locked out by a removal. The dialog copy says
          this (frontend messages/*/adminIdentity.json uid.removeBody) so the admin can tell them.
        • They fail the OPTIONAL per-event "Require player UID" gate
          (afc_tournament_and_scrims._missing_registration_assets) until they set a new one, which
          is the correct outcome when the UID we hold is known to be wrong.

    IDENTITY LOCK
      edit_profile freezes a player's own username + uid while they are committed to a live event
      (_has_active_event_registration) so match results stay attributable mid-tournament. An admin
      is the intended escape hatch from that lock, so this endpoint does NOT refuse. It reports the
      lock in the response and records it on the audit row, so a mid-event identity change is
      visible after the fact rather than silent.

    AUTH      views.require_head_admin. Audited via set_audit + AdminHistory (before AND after).
    CONSUMED BY  frontend app/(a)/a/players/[id]/page.tsx -> EditUidModal.
    """
    admin_user, err = require_head_admin(request)
    if err:
        return err

    target, err = _target(request.data.get("user_id"))
    if err:
        return err
    blocked = _guard_target(admin_user, target)
    if blocked:
        return blocked

    reason, err = _reason(request)
    if err:
        return err

    # The key must be PRESENT. See the docstring: an absent uid is the shape of the 2026-06-22 bug
    # where a partial save silently blanked a set UID, so "absent" is an error, never a removal.
    if "uid" not in request.data:
        return Response(
            {"message": "uid is required. Send an empty value to remove the UID."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    new_uid = (request.data.get("uid") or "").strip()
    previous_uid = (target.uid or "").strip()

    if new_uid:
        # Format: digits only, within the column width. See UID_MAX_LENGTH for why this is stricter
        # than edit_profile.
        if not new_uid.isdigit():
            return Response({"message": "A Free Fire UID is numbers only."},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(new_uid) > UID_MAX_LENGTH:
            return Response({"message": f"A UID can be at most {UID_MAX_LENGTH} digits."},
                            status=status.HTTP_400_BAD_REQUEST)
        if new_uid == previous_uid:
            return Response({"message": "That is already this user's UID."},
                            status=status.HTTP_400_BAD_REQUEST)
        # UNIQUE column: tell the admin who holds it, so they can go and clear it there first.
        clash = User.objects.exclude(pk=target.pk).filter(uid=new_uid).first()
        if clash:
            return Response(
                {"message": f"That UID is already on {clash.username}'s account. Remove it there first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Cross-column collision: this UID is somebody's in-game NAME, and sign-in matches a typed
        # identifier against username/uid/email together. Saving it would lock BOTH accounts out.
        # See _login_ambiguity_clash - 116 accounts have an all-digits username, so a UID landing on
        # one is a live possibility, not a theoretical one.
        name_clash = _login_ambiguity_clash(target, new_uid)
        if name_clash:
            return Response(
                {"message": f"That UID is {name_clash.username}'s in-game name, and players can sign in with their name or their UID. Using it here would lock both accounts out."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    elif not previous_uid:
        return Response({"message": "This account has no UID to remove."},
                        status=status.HTTP_400_BAD_REQUEST)

    identity_locked = _has_active_event_registration(target)

    # NULL, not "", for a removal - the column is UNIQUE (see the docstring).
    target.uid = new_uid or None
    target.save(update_fields=["uid"])

    # ── audit: who, to whom, before, after, why ─────────────────────────────────────────────────
    # The summary is the sentence the admin History page shows; the detail fields ride along in
    # AuditLog.metadata["details"] so the before/after survives even if the sentence is truncated.
    action_word = "Removed" if not new_uid else "Changed"
    summary = (
        f"{action_word} {target.username}'s Free Fire UID "
        f"({previous_uid or 'none'} -> {new_uid or 'none'}): {reason}"
    )
    set_audit(
        request, summary,
        target_user=target.username,
        target_user_id=target.user_id,
        field="uid",
        before=previous_uid or None,
        after=new_uid or None,
        reason=reason,
        identity_locked=identity_locked,
    )
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="set_user_uid",
        description=(
            f"{action_word} UID for {target.username} (ID: {target.user_id}): "
            f"{previous_uid or 'none'} -> {new_uid or 'none'}. Reason: {reason}"
        ),
    )

    message = (
        f"UID removed from {target.username}."
        if not new_uid else
        f"UID for {target.username} updated to {new_uid}."
    )
    if identity_locked:
        message += " Note: this player is registered for a live event, so their match results are attributed under the old UID."

    return Response(
        {"message": message, "uid": new_uid, "previous_uid": previous_uid,
         "identity_locked": identity_locked},
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Account email - the takeover primitive, built like one
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def admin_set_user_email(request):
    """POST auth/admin/set-user-email/  Bearer auth, HEAD ADMIN / SUPER ADMIN only.

    PURPOSE
      Move a locked-out account onto an address its owner can actually read. This is the ONLY path
      for a user who signed up with a wrong or dead email: the self-serve flow
      (request_email_change -> confirm_email_change) needs them signed in AND able to read a code at
      the new address, which a locked-out user is by definition not.

    REQUEST   { user_id: int, new_email: str, reason: str, disable_two_factor?: bool }
    RESPONSE  200 { message, email, previous_email, sessions_ended, two_factor_disabled, reactivated }
              400 bad/duplicate/unchanged address, address equal to another account's in-game name
                  (see _login_ambiguity_clash), no reason · 401 bad session
              403 not a head admin / self / target is super_admin · 404 no user
              409 { message, requires_two_factor_ack: true } - target has 2FA on and the caller did
                  not send disable_two_factor.

    THE NEW ADDRESS ARRIVES VERIFIED. WHY.
      The default elsewhere on this codebase is to prove ownership before switching, and the
      self-serve flow does exactly that. This endpoint deliberately does not, because requiring a
      confirmation click at the new address would re-lock the only user it exists for: support
      reaches for this when the account cannot receive or act on mail at all, and a bounce or a
      typo would leave the account stranded again with no way back. is_active is also flipped True,
      which is the same reasoning applied to a signup that never entered its verification code.
      Identity is proven OUT OF BAND by support before the call. What makes that safe is everything
      below rather than a click: the narrowest role gate on the site, a mandatory typed reason, an
      audit row carrying the before and after, mail to BOTH addresses, every session ended, and 2FA
      that cannot be walked around silently.

    BOTH ADDRESSES ARE EMAILED
      The OLD one first: if the real owner did not ask for this, that message is the only warning
      they will ever get, and it is the tripwire for an admin account that has been compromised.
      Then the NEW one, so whoever support is helping knows the change landed. Both are best-effort
      (a dead old address is the normal case here) and neither can fail the change.

    EVERY SESSION IS ENDED
      A changed address plus a live cookie is how a takeover survives the change. Deleting every
      SessionToken forces a fresh sign-in that goes through the new address. Any pending
      EmailChangeRequest is dropped with them, so a code minted against the old address cannot be
      spent afterwards.

    TWO-FACTOR IS NEVER SILENTLY BYPASSED
      The one shipped factor is a code to the account email (afc_auth.two_factor.EmailCodeMethod),
      so moving the address would quietly hand the second factor to whoever holds the new inbox -
      2FA still "on", and no longer protecting anyone. So: if the target has 2FA enabled the request
      is REFUSED with 409 unless it carries disable_two_factor: true. With the acknowledgement, 2FA
      is switched OFF properly (settings cleared, recovery codes deleted, live challenges burned -
      the same teardown as views_two_factor.two_factor_disable), the audit row records it, and both
      emails say so and tell the owner to switch it back on. The factor is taken down in the open,
      by a named admin, on the record, instead of being stepped around.

    AUTH      views.require_head_admin. This REPLACES the previous role=="admin" gate, which let any
              of the 40-odd role=="admin" accounts (news, shop, sponsor admins) change any email.
    CONSUMED BY  frontend app/(a)/a/players/[id]/page.tsx -> EditEmailModal.
    """
    admin_user, err = require_head_admin(request)
    if err:
        return err

    target, err = _target(request.data.get("user_id"))
    if err:
        return err
    blocked = _guard_target(admin_user, target)
    if blocked:
        return blocked

    reason, err = _reason(request)
    if err:
        return err

    new_email = (request.data.get("new_email") or "").strip()
    if not new_email:
        return Response({"message": "new_email is required."}, status=status.HTTP_400_BAD_REQUEST)

    ok, msg = is_valid_email(new_email)
    if not ok:
        return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

    previous_email = target.email or ""
    if new_email.lower() == previous_email.lower():
        return Response({"message": "That is already this user's email."},
                        status=status.HTTP_400_BAD_REQUEST)

    # Case-insensitive duplicate check: MySQL's default collation already compares this way, but
    # __iexact states the rule in the code so it cannot drift with a collation change, and it is the
    # same check the self-serve flow runs.
    clash = User.objects.exclude(pk=target.pk).filter(email__iexact=new_email).first()
    if clash:
        return Response({"message": "That email is already registered to another account."},
                        status=status.HTTP_400_BAD_REQUEST)
    # Same cross-column trap as the UID (see _login_ambiguity_clash): 106 accounts have a username
    # that IS a well-formed email address, so an address can collide with somebody's in-game name
    # even when no account holds it as an email. Sign-in would then match two rows and refuse both.
    name_clash = _login_ambiguity_clash(target, new_email)
    if name_clash:
        return Response(
            {"message": f"That address is {name_clash.username}'s in-game name, and players can sign in with their name or their email. Using it here would lock both accounts out."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── 2FA acknowledgement gate (see the docstring) ────────────────────────────────────────────
    two_factor_on, _row = _two_factor_state(target)
    ack = request.data.get("disable_two_factor")
    ack = ack if isinstance(ack, bool) else str(ack or "").strip().lower() in ("true", "1", "yes")
    if two_factor_on and not ack:
        return Response(
            {
                "message": "This account has two-factor authentication on, and the code goes to the email address. Changing the address would hand the second factor to the new inbox, so 2FA has to be switched off as part of this change. Confirm to continue.",
                "requires_two_factor_ack": True,
            },
            status=status.HTTP_409_CONFLICT,
        )

    # Everything above is validation. Only now do we write - a `return Response(...)` inside an
    # atomic block silently discards the writes that came before it on this codebase.
    reactivated = not target.is_active
    with transaction.atomic():
        target.email = new_email
        # A never-verified signup (is_active False = the code was never entered) is exactly the
        # legacy lockout this endpoint exists for, so the corrected address also unlocks the account.
        target.is_active = True
        target.save(update_fields=["email", "is_active"])

        # Sessions + any half-finished self-serve email change, gone together.
        sessions_ended = SessionToken.objects.filter(user=target).delete()[0]
        EmailChangeRequest.objects.filter(user=target).delete()

        two_factor_disabled = False
        if two_factor_on:
            TwoFactorSettings.objects.filter(user=target).update(
                is_enabled=False, enabled_at=None, updated_at=timezone.now())
            # Recovery codes and live challenges are only meaningful while 2FA is on; a code printed
            # months ago must not work against a freshly re-enabled account, and a challenge minted
            # against the OLD address must not be spendable after the address moves.
            target.two_factor_backup_codes.all().delete()
            TwoFactorChallenge.objects.filter(user=target, consumed_at__isnull=True).update(
                consumed_at=timezone.now())
            two_factor_disabled = True

    # ── audit: who, to whom, before, after, why, and what else it took down ─────────────────────
    set_audit(
        request,
        f"Changed {target.username}'s email ({previous_email or 'none'} -> {new_email}): {reason}",
        target_user=target.username,
        target_user_id=target.user_id,
        field="email",
        before=previous_email or None,
        after=new_email,
        reason=reason,
        sessions_ended=sessions_ended,
        two_factor_disabled=two_factor_disabled,
        reactivated=reactivated,
    )
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="set_user_email",
        description=(
            f"Set email for {target.username} (ID: {target.user_id}): "
            f"{previous_email or 'none'} -> {new_email}. Reason: {reason}. "
            f"Sessions ended: {sessions_ended}. 2FA disabled: {two_factor_disabled}."
        ),
    )

    # ── tell BOTH addresses (best-effort; a dead old address is the normal case here) ───────────
    lang = _recipient_language(target)
    when = timezone.now().strftime("%d %b %Y, %H:%M UTC")
    subject = subject_for("email_updated_admin", lang)
    body = email_admin_email_changed(
        target.username, new_email, when,
        two_factor_off=two_factor_disabled, lang=lang,
    )
    for address in (previous_email, new_email):
        if not address:
            continue
        try:
            send_email(address, subject, body, language=lang, prelocalized=True)
        except Exception as exc:
            # Never fail the change on a mail error, and never log the address itself alongside it.
            print(f"Admin email-change notice failed for {target.username}: {exc}")

    message = f"Email for {target.username} updated to {new_email}."
    if sessions_ended:
        message += f" {sessions_ended} session(s) ended."
    if two_factor_disabled:
        message += " Two-factor authentication was switched off."
    if reactivated:
        message += " The account was reactivated."

    return Response(
        {
            "message": message,
            "email": new_email,
            "previous_email": previous_email,
            "sessions_ended": sessions_ended,
            "two_factor_disabled": two_factor_disabled,
            "reactivated": reactivated,
        },
        status=status.HTTP_200_OK,
    )
