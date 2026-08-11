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
# WHAT 2026-08-11 ADDED, AND WHY IT IS THE SAME MODULE
#   Support kept hitting three more fields it could not touch, and all three share this module's
#   one idea - a person other than the account owner changing something the owner normally owns -
#   so they share its gate, its mandatory reason and its audit trail rather than growing three
#   private rules elsewhere:
#     3. "That is not my name." The in-game name is the THIRD login identifier, it is FROZEN for
#        the player's own edits mid-event, and a name that is another account's UID or email
#        cannot be typed in by a player at all (they cannot see the other account to resolve it).
#     4. "I am not in that country." Not a login field, but User.country decides which broadcast
#        audience the account lands in, and the column already holds `Nigeria` AND `NG` for one
#        country. Typed values are therefore validated and stored in ONE canonical spelling.
#     5. "That is not my WhatsApp number." Since 2026-08-08 that number PROVES ownership in
#        self-serve recovery, so a wrong one is both a dead rescue path and a live risk. Writing
#        it is close to handing over a key - which is why it sits behind THIS gate and not a
#        broader one, and why the account owner is emailed every time.
#
# ENDPOINTS (prefix auth/, wired in afc_auth/urls.py)
#   • GET  auth/admin/user-identity/<user_id>/   admin_user_identity     read the whole identity
#   • POST auth/admin/set-user-uid/              admin_set_user_uid      edit OR remove a UID
#   • POST auth/admin/set-user-email/            admin_set_user_email    change an account email
#   • POST auth/admin/set-user-username/         admin_set_user_username change the in-game name
#   • POST auth/admin/set-user-country/          admin_set_user_country  change the country
#   • POST auth/admin/set-user-whatsapp/         admin_set_user_whatsapp edit OR remove the number
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
#   models   : User.uid / User.email / User.username / User.country / User.is_active,
#              UserProfile.whatsapp_number + .whatsapp_number_updated_at (through
#              models.canonical_profile - duplicate profile rows exist in prod), SessionToken,
#              EmailChangeRequest, TwoFactorSettings + TwoFactorChallenge + TwoFactorBackupCode,
#              AdminHistory.
#   country  : afc_auth/country_grouping.py canonical_country (compare) + canonical_country_name
#              (store), the same vocabulary the broadcast audience builder folds by.
#   phone    : afc_whatsapp/phone.py require_international (validate) + mask_e164 (what the audit
#              row and every response carry, never the dialable number).
#   recovery : afc_auth/views_recovery.py reads the number and its freshness stamp; that is why
#              set-user-whatsapp writes whatsapp_number_updated_at rather than the number alone.
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

from afc_whatsapp.phone import mask_e164, require_international

from . import trusted_devices, two_factor
from .audit import set_audit
# One country vocabulary for the whole site: the same fold the broadcast audience builder uses, so
# a country repaired here groups with everyone else from that country instead of becoming spelling
# number three. canonical_country_name is what gets STORED; canonical_country is used to compare.
from .country_grouping import canonical_country, canonical_country_name
# The ONE place the "a value may not be two different login identifiers" rule lives, shared with
# register / edit_profile / the Google SSO username generator in views.py (see identifiers.py).
from .identifiers import IDENTIFIER_LABELS, cross_field_conflict
from .models import (
    AdminHistory,
    EmailChangeRequest,
    SessionToken,
    TwoFactorChallenge,
    TwoFactorSettings,
    User,
    canonical_profile,
)
from .views import (
    _has_active_event_registration,
    _is_super_admin,
    _user_role_names,
    email_admin_email_changed,
    email_admin_username_changed,
    email_admin_whatsapp_changed,
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

# User.username is CharField(max_length=40) and IS the in-game name on this site. Checked here so an
# over-long value is refused with a sentence instead of a MySQL "Data too long" 500. No format rule
# beyond the length: in-game names legitimately carry spaces, punctuation and stylized Unicode
# (see utils/search_utils.py, which exists because of exactly that), so anything stricter would
# refuse names players really hold.
USERNAME_MAX_LENGTH = 40

# The typed reason is mandatory and is what makes the audit trail answer "why", not just "what".
# Capped so the summary sentence it is folded into stays inside AuditLog.summary (255 chars); the
# full text is also stored verbatim in the audit metadata via set_audit(**details).
REASON_MAX_LENGTH = 200


def _login_ambiguity_clash(target, value, field):
    """The account (if any) already using `value` as a DIFFERENT kind of login identifier.

    Thin wrapper over the shared guard in afc_auth/identifiers.py so this module, register,
    edit_profile and the Google SSO username generator all enforce ONE rule from ONE place.
    Returns (holder, held_as) or (None, None); `field` is the column being written here.

    WHY IT EXISTS. Sign-in resolves ONE typed identifier against email, username and uid
    (identifiers.resolve_login_identifier). If account A's uid equals account B's username, that
    string is ambiguous, and before the resolver was ordered it refused the login for BOTH of them.
    Uniqueness on each column does NOT catch this: the collision is ACROSS columns. In the live
    table 10 accounts already sit in exactly that state (e.g. uid "9137457129" against another
    player NAMED "9137457129"), and 106 usernames are well-formed email addresses. Since the whole
    point of this module is to END a lockout, it must not be able to create one.

    Unlike the player-facing surfaces, the messages built from this DO name the holder: an admin
    needs to know which account to go and fix, and is already trusted with that.
    """
    return cross_field_conflict(value, field, exclude_pk=target.pk)


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
      The state the FIVE edit dialogs need before they let an admin type anything: the current
      in-game name, UID, email, country and WhatsApp number, whether the account has two-factor
      authentication switched on (which forces an extra acknowledgement on an email change),
      whether the player is mid-event (their name + UID are frozen for their own edits), and how
      many live sessions an email change would end.

    REQUEST   no body. `user_id` is the User.user_id of the account being inspected.
    RESPONSE  200 {
                user_id, username, email, uid, country,
                whatsapp_number,      # MASKED ("+234 ***** 4567"), never the dialable number
                has_whatsapp_number,  # so a dialog can offer Remove without unmasking anything
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
    profile = canonical_profile(target)
    raw_number = (getattr(profile, "whatsapp_number", "") or "").strip()
    return Response(
        {
            "user_id": target.user_id,
            "username": target.username,
            "email": target.email,
            "uid": target.uid or "",
            "country": target.country or "",
            # MASKED, never the dialable number. This payload is what the dialogs open onto, and a
            # dialog only needs to show the admin WHICH number is on file so they can tell whether
            # the one the player is quoting is different. Same rule the audit row follows.
            "whatsapp_number": mask_e164(raw_number) if raw_number else "",
            "has_whatsapp_number": bool(raw_number),
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
        # Cross-column collision: this UID is somebody's in-game NAME (or email), and sign-in
        # matches a typed identifier against all three together. Saving it would make that string
        # ambiguous. See _login_ambiguity_clash - 116 accounts have an all-digits username, so a
        # UID landing on one is a live possibility, not a theoretical one.
        name_clash, held_as = _login_ambiguity_clash(target, new_uid, "uid")
        if name_clash:
            return Response(
                {"message": f"That UID is {name_clash.username}'s {IDENTIFIER_LABELS[held_as]}, and players can sign in with their name, their email or their UID. Using it here would lock both accounts out."},
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
    # even when no account holds it as an email. That string would then be ambiguous at sign-in.
    name_clash, held_as = _login_ambiguity_clash(target, new_email, "email")
    if name_clash:
        return Response(
            {"message": f"That address is {name_clash.username}'s {IDENTIFIER_LABELS[held_as]}, and players can sign in with their name, their email or their UID. Using it here would lock both accounts out."},
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
        # And every REMEMBERED DEVICE (owner 2026-08-08, afc_auth/trusted_devices.py). This
        # endpoint exists to RESCUE an account that somebody else has taken; a trusted device skips
        # the second factor, so leaving those rows behind would mean the rescue tool was the thing
        # that kept the attacker's way in open. Inside the atomic block, and NOT swallowed, for the
        # same reason the 2FA teardown below is not: if any part of this rescue cannot be written,
        # none of it should be, and an admin needs to see that rather than be told it worked.
        devices_forgotten = trusted_devices.revoke_all(target)

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


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  In-game name - the third login identifier (owner 2026-08-11)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def admin_set_user_username(request):
    """POST auth/admin/set-user-username/  Bearer auth, HEAD ADMIN / SUPER ADMIN only.

    PURPOSE
      Correct somebody's in-game name when they cannot. A player normally edits it themselves in
      profile settings, so this endpoint is for the two cases where that door is shut:
        • the identity lock froze the field because they are committed to a LIVE event
          (views.edit_profile, _has_active_event_registration);
        • the name they need is refused by the cross-column rule (it is another account's UID or
          email), which a player cannot resolve because they cannot see the other account.

    REQUEST   { user_id: int, username: str, reason: str }
    RESPONSE  200 { message, username, previous_username, identity_locked }
              400 missing/blank, unchanged, already taken, equal to another account's UID or email,
                  no reason · 401 bad session · 403 not a head admin / self / target is super_admin
              404 unknown user.

    WHAT THIS DOES NOT DO, AND WHY
      • It does NOT end sessions. SessionToken keys off the user, not the name, so nothing is
        invalidated by the rename, and killing sessions would log a player out MID-EVENT for what
        is usually somebody else's typo. Contrast admin_set_user_email, which must end them: an
        email change is a takeover primitive and the point is to lock the previous holder out.
      • It does NOT touch two-factor. No factor is delivered to the in-game name.

    IDENTITY LOCK
      Deliberately OVERRIDDEN rather than refused, exactly like admin_set_user_uid: the admin is
      the intended escape hatch from the lock. The lock state rides on the response, the audit row
      and the player's email, so a mid-event rename is visible after the fact instead of silent.
      Results already recorded keep the old name until that event is over.

    AUTH      views.require_head_admin. Audited via set_audit + AdminHistory (before AND after).
    CONSUMED BY  frontend app/(a)/a/_components/AccountIdentityMore.tsx -> EditUsernameDialog,
                 rendered on app/(a)/a/players/[id]/page.tsx beside the UID and email controls.
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

    new_name = (request.data.get("username") or "").strip()
    previous_name = (target.username or "").strip()

    # Unlike the UID there is no "remove" here: username is NOT NULL and is what every screen
    # displays a player by, so a blank one would leave an unnameable account.
    if not new_name:
        return Response({"message": "An in-game name is required."},
                        status=status.HTTP_400_BAD_REQUEST)
    if len(new_name) > USERNAME_MAX_LENGTH:
        return Response({"message": f"An in-game name can be at most {USERNAME_MAX_LENGTH} characters."},
                        status=status.HTTP_400_BAD_REQUEST)
    if new_name == previous_name:
        return Response({"message": "That is already this user's in-game name."},
                        status=status.HTTP_400_BAD_REQUEST)

    # Case-insensitive, matching admin_set_user_email's reasoning: MySQL's collation already
    # compares this way, and saying so in code keeps the rule from drifting with a collation change.
    clash = User.objects.exclude(pk=target.pk).filter(username__iexact=new_name).first()
    if clash:
        return Response(
            {"message": f"That in-game name is already taken (account ID {clash.user_id}). Free it there first."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Cross-column collision: this NAME is somebody's UID or email, and sign-in matches one typed
    # string against all three columns together. 116 accounts have an all-digits username and 106
    # have a username that is a well-formed email address, so this is a live case, not theory.
    other, held_as = _login_ambiguity_clash(target, new_name, "username")
    if other:
        return Response(
            {"message": f"That name is {other.username}'s {IDENTIFIER_LABELS[held_as]}, and players can sign in with their name, their email or their UID. Using it here would lock both accounts out."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    identity_locked = _has_active_event_registration(target)

    target.username = new_name
    target.save(update_fields=["username"])

    summary = (
        f"Changed {previous_name}'s in-game name "
        f"({previous_name} -> {new_name}): {reason}"
    )
    set_audit(
        request, summary,
        target_user=new_name,
        target_user_id=target.user_id,
        field="username",
        before=previous_name,
        after=new_name,
        reason=reason,
        identity_locked=identity_locked,
    )
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="set_user_username",
        description=(
            f"Changed in-game name for account ID {target.user_id}: "
            f"{previous_name} -> {new_name}. Reason: {reason}"
        ),
    )

    # ── tell the player: one of the three things they sign in with just changed ─────────────────
    lang = _recipient_language(target)
    when = timezone.now().strftime("%d %b %Y, %H:%M UTC")
    try:
        send_email(
            target.email,
            subject_for("username_updated_admin", lang),
            email_admin_username_changed(new_name, when, mid_event=identity_locked, lang=lang),
            language=lang, prelocalized=True,
        )
    except Exception as exc:
        # Never fail the repair on a mail error - the account is already correct, and this endpoint
        # exists precisely because some of these addresses are dead.
        print(f"Admin username-change notice failed for account {target.user_id}: {exc}")

    message = f"In-game name changed from {previous_name} to {new_name}."
    if identity_locked:
        message += " Note: this player is registered for a live event, so results already recorded there still show the old name."

    return Response(
        {"message": message, "username": new_name, "previous_username": previous_name,
         "identity_locked": identity_locked},
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5  Country - not a login field, but it decides who gets which broadcast (owner 2026-08-11)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def admin_set_user_country(request):
    """POST auth/admin/set-user-country/  Bearer auth, HEAD ADMIN / SUPER ADMIN only.

    PURPOSE
      Fix the country on an account. It is not a login identifier, so this is the mildest of the
      four repairs, but it is not cosmetic either: User.country is what the broadcast audience
      builder groups by (afc_auth/audience.py) and what fills a blank language at login
      (afc_auth/language_utils.py). A wrong value quietly sends someone the wrong announcements in
      the wrong language.

    REQUEST   { user_id: int, country: str, reason: str }
              `country` is a country NAME or ISO-2 code. An empty value CLEARS it.
    RESPONSE  200 { message, country, previous_country }
              400 unrecognised country, unchanged, no reason · 401 · 403 · 404.

    WHY THE VALUE IS VALIDATED RATHER THAN STORED AS TYPED
      The live column already holds two spellings for one country - 2,892 rows say `Nigeria` and
      1,817 say `NG`, and the same is true for a dozen more - which is why the audience builder had
      to start folding them (afc_auth/country_grouping.py). A free-text box here would keep adding
      new spellings to that pile. So the posted value is resolved through canonical_country (which
      is pycountry-backed), anything it cannot recognise is refused, and what gets STORED is the
      proper-cased pycountry name. From this endpoint onwards, one country is written one way.

      Refusing an unresolvable value is safe precisely because it is not a login field: nobody is
      locked out by having to pick a real country from the list.

    WHAT IT DOES NOT TOUCH
      UserProfile.country. A second column with the same name exists, nothing user-facing reads it
      today, and writing both from one control is how two columns start disagreeing. If it ever
      needs to move too, that is a deliberate change with its own reason, not a side effect here.
      User.ip_country is untouched for a different reason: it is EVIDENCE (where the account
      actually connects from), not a claim, and an admin overwriting it would erase the signal.

    AUTH      views.require_head_admin. Audited via set_audit + AdminHistory.
    CONSUMED BY  frontend AccountIdentityMore.tsx -> EditCountryDialog.
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

    if "country" not in request.data:
        return Response({"message": "country is required. Send an empty value to clear it."},
                        status=status.HTTP_400_BAD_REQUEST)

    typed = (request.data.get("country") or "").strip()
    previous_country = (target.country or "").strip()

    if typed:
        stored = canonical_country_name(typed)
        if not stored:
            return Response(
                {"message": f"'{typed}' is not a country we recognise. Pick one from the list so it groups with everyone else from there."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        stored = ""

    # Compared through the canonical fold, not as raw strings: with the column holding `NG` and
    # `Nigeria` for one country, a raw comparison would call `Nigeria` a change on an `NG` row and
    # write an audit entry for a rename that moves nobody.
    if canonical_country(stored) == canonical_country(previous_country):
        return Response({"message": "That is already this user's country."},
                        status=status.HTTP_400_BAD_REQUEST)

    target.country = stored
    target.save(update_fields=["country"])

    set_audit(
        request,
        f"Changed {target.username}'s country ({previous_country or 'none'} -> {stored or 'none'}): {reason}",
        target_user=target.username,
        target_user_id=target.user_id,
        field="country",
        before=previous_country or None,
        after=stored or None,
        reason=reason,
    )
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="set_user_country",
        description=(
            f"Changed country for {target.username} (ID: {target.user_id}): "
            f"{previous_country or 'none'} -> {stored or 'none'}. Reason: {reason}"
        ),
    )

    # No email on purpose: nothing about signing in changed, and a message saying "support fixed
    # your country" would train players to ignore the notices that DO matter.
    message = (
        f"Country cleared for {target.username}." if not stored
        else f"Country for {target.username} set to {stored}."
    )
    return Response(
        {"message": message, "country": stored, "previous_country": previous_country},
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6  WhatsApp number - a contact detail that became a door (owner 2026-08-11)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def admin_set_user_whatsapp(request):
    """POST auth/admin/set-user-whatsapp/  Bearer auth, HEAD ADMIN / SUPER ADMIN only.

    PURPOSE
      Correct or clear the WhatsApp number on somebody's account. Support needs it because the
      number is typed at signup and never checked, so a wrong one is invisible until the day it
      matters, and because a number that belonged to somebody else must be removable on request.

    ⚠ WHAT AN ADMIN IS BEING TRUSTED WITH HERE
      Since 2026-08-08 this number PROVES ownership in self-serve account recovery
      (afc_auth/views_recovery.py): whoever answers it can reset the password or move the email.
      So writing it is, in effect, handing somebody a key to the account.

      It is allowed anyway, and the reason is that it grants no power this role did not already
      have: admin_set_user_email in this same module lets the same head admin point the account's
      email wherever they like, and password resets follow the email. The gate is therefore the
      same one (require_head_admin), with the same typed reason, the same audit row, the same
      refusal to act on your own account or on a super_admin, and - unlike the email path - a
      notice to the account owner every single time.

    REQUEST   { user_id: int, whatsapp_number: str, reason: str }
              International form only ("+234..." or "00234..."). An EMPTY value REMOVES the number.
              The key being absent is rejected rather than treated as a removal, the same rule
              admin_set_user_uid follows and for the same reason (a June 2026 partial save silently
              wiped UIDs).
    RESPONSE  200 { message, whatsapp_number (MASKED), previous_number (MASKED), removed }
              400 missing key, unparseable/local-format number, unchanged, nothing to remove, no
                  reason · 401 · 403 · 404.

    THE FRESHNESS STAMP, AND WHY IT IS THE POINT
      Recovery refuses a number that has not been touched for RECOVERY_NUMBER_MAX_AGE (12 months),
      because mobile lines get recycled. A corrected number therefore has to be stamped as fresh,
      or support would "fix" it and the player still could not use it. So this endpoint writes
      whatsapp_number_updated_at = now, exactly as signup and edit_profile do.

    MASKING
      The response and the audit row carry the number MASKED (afc_whatsapp/phone.py mask_e164).
      The raw value lives on the profile, where the account owner and the send path read it; an
      audit log is read by more people and over a longer period, so it stores the recognisable
      form, not the dialable one.

    AUTH      views.require_head_admin. Audited via set_audit + AdminHistory.
    CONSUMED BY  frontend AccountIdentityMore.tsx -> EditWhatsappDialog.
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

    if "whatsapp_number" not in request.data:
        return Response({"message": "whatsapp_number is required. Send an empty value to remove it."},
                        status=status.HTTP_400_BAD_REQUEST)

    # canonical_profile, NOT profile_of: duplicate UserProfile rows exist in production, and the
    # lowest-profile_id row is the one every reader and writer in this codebase agrees on -
    # including two_factor.WhatsAppCodeMethod, which is what would MESSAGE this number. Writing any
    # other row would look like a silent no-op. create=True because ~1 in N accounts has no profile
    # row at all and support must still be able to put a number on them.
    profile = canonical_profile(target, create=True)
    previous_number = (profile.whatsapp_number or "").strip()
    typed = str(request.data.get("whatsapp_number") or "").strip()

    if typed:
        # require_international, not to_e164: a bare national number must be refused rather than
        # guessed at from the account's country, because the country on the account is exactly the
        # field the endpoint above exists to correct. Its own message names the real problem.
        new_number, phone_error = require_international(typed)
        if phone_error:
            return Response({"message": phone_error}, status=status.HTTP_400_BAD_REQUEST)
        if new_number == previous_number:
            return Response({"message": "That is already this user's WhatsApp number."},
                            status=status.HTTP_400_BAD_REQUEST)
    else:
        new_number = ""
        if not previous_number:
            return Response({"message": "This account has no WhatsApp number to remove."},
                            status=status.HTTP_400_BAD_REQUEST)

    profile.whatsapp_number = new_number
    # Stamped on a removal too, so "when was this field last touched" stays answerable, and so a
    # number typed back in later cannot inherit a stale date. See the docstring.
    profile.whatsapp_number_updated_at = timezone.now()
    profile.save(update_fields=["whatsapp_number", "whatsapp_number_updated_at"])

    masked_new = mask_e164(new_number) if new_number else ""
    masked_previous = mask_e164(previous_number) if previous_number else ""

    action_word = "Removed" if not new_number else "Changed"
    set_audit(
        request,
        f"{action_word} {target.username}'s WhatsApp number "
        f"({masked_previous or 'none'} -> {masked_new or 'none'}): {reason}",
        target_user=target.username,
        target_user_id=target.user_id,
        field="whatsapp_number",
        # MASKED on both sides - see the docstring. The audit trail records that the number moved
        # and roughly which number it is, never a dialable copy of it.
        before=masked_previous or None,
        after=masked_new or None,
        reason=reason,
    )
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="set_user_whatsapp",
        description=(
            f"{action_word} WhatsApp number for {target.username} (ID: {target.user_id}): "
            f"{masked_previous or 'none'} -> {masked_new or 'none'}. Reason: {reason}"
        ),
    )

    # ── tell the player, always: this changed how their account can be recovered ────────────────
    lang = _recipient_language(target)
    when = timezone.now().strftime("%d %b %Y, %H:%M UTC")
    try:
        send_email(
            target.email,
            subject_for("whatsapp_updated_admin", lang),
            email_admin_whatsapp_changed(masked_new, when, removed=not new_number, lang=lang),
            language=lang, prelocalized=True,
        )
    except Exception as exc:
        print(f"Admin WhatsApp-change notice failed for account {target.user_id}: {exc}")

    message = (
        f"WhatsApp number removed from {target.username}." if not new_number
        else f"WhatsApp number for {target.username} updated to {masked_new}."
    )
    return Response(
        {"message": message, "whatsapp_number": masked_new,
         "previous_number": masked_previous, "removed": not new_number},
        status=status.HTTP_200_OK,
    )
