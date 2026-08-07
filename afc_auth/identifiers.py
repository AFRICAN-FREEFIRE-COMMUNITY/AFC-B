# afc_auth/identifiers.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# LOGIN IDENTIFIERS - the three columns a typed sign-in string may match, the ORDER they are tried
# in, and the guard that stops one string ever landing in two of them (owner 2026-08-07).
#
# THE BUG THIS MODULE EXISTS TO END
#   Sign-in lets a player type their email, their in-game name OR their Free Fire UID into one box.
#   That used to be resolved with a single query across all three columns at once:
#
#       User.objects.get(Q(username=x) | Q(uid=x) | Q(email__iexact=x))
#
#   Each of those columns is individually UNIQUE, so a string appears at most once PER COLUMN - but
#   nothing stopped the SAME string being one row's username and a DIFFERENT row's uid. When that
#   happened the query matched two rows, .get() raised MultipleObjectsReturned, and the backend
#   refused the login for BOTH people. Uniqueness cannot catch it: the collision is ACROSS columns.
#
#   It was not hypothetical. On 2026-08-07 the live table held 10 such pairs (20 accounts), every
#   one the same shape: somebody typed their UID into the in-game name box at signup, and a second
#   player genuinely owns that number as their UID. 116 accounts have an all-digits username and
#   106 have a username that is a well-formed email address, so the fuel is still there.
#
# THE TWO HALVES OF THE FIX, both in this module so they cannot drift apart
#   1. RESOLUTION - resolve_login_identifier() tries one column at a time in a declared order and
#      returns a single deterministic user. Ambiguity becomes precedence instead of a refusal.
#   2. PREVENTION - cross_field_conflict() lets every write path refuse to put a value into one
#      login column when another row already holds it in a different one. Without this the
#      collision set keeps growing and resolution is forever papering over it.
#
# HOW IT CONNECTS
#   resolution  : afc_auth/backends.py EmailOrUsernameModelBackend.authenticate, the only caller,
#                 reached from afc_auth/views.py login (the single authenticate() call site).
#                 Everything downstream takes an already-resolved user: login_or_challenge issues
#                 the 2FA challenge against it, and views_two_factor.two_factor_verify reads
#                 challenge.user rather than re-resolving, so this module cannot mis-route a second
#                 factor. Google and Discord SSO resolve by email and never call authenticate().
#   prevention  : afc_auth/views.py register, edit_profile and _unique_username_from_email
#                 (the Google SSO username generator), plus afc_auth/views_admin_identity.py
#                 admin_set_user_uid and admin_set_user_email.
#   tests       : afc_auth/tests_login_identifiers.py.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
from django.contrib.auth import get_user_model


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  The precedence order
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# (model field, ORM lookup) in the order a typed identifier is tried. THE ORDER IS A DECISION, not
# an accident, and it is ranked by how strongly the field was PROVEN and how transferable it is.
# Do not reorder these without re-reading this block.
#
#   1. email     Non-null and unique, and for any account that can actually sign in it has been
#                PROVEN: is_active only becomes True after a verification code is entered, an SSO
#                provider vouched for the address, or support asserted it out of band. It is also
#                the password-reset channel, so taking it away hurts most.
#   2. username  Non-null, unique and canonical (it is USERNAME_FIELD), but SELF-ASSERTED and
#                RECYCLABLE: register() deliberately hands an abandoned unverified account's
#                username to a new signup. It is typed text, not a proven claim.
#   3. uid       Nullable, EXTERNAL to AFC (it names a Free Fire account, not an AFC one),
#                self-asserted, and TRANSFERABLE between accounts - views_admin_identity exists
#                partly to move one. Weakest claim, so it loses every tie.
#
# The case that settles email above username: 106 accounts have a username that is a well-formed
# email address. If one of those ever equals another account's real email, username-first would
# hand the string to whoever typed it as a display name and take the login away from the person who
# verified that inbox. Email-first fails in the gentler direction.
LOGIN_IDENTIFIER_PRECEDENCE = (
    ("email", "email__iexact"),
    ("username", "username"),
    ("uid", "uid"),
)

# Human wording for each column, for the "you cannot use that here" messages callers build.
IDENTIFIER_LABELS = {
    "email": "email address",
    "username": "in-game name",
    "uid": "Free Fire UID",
}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Resolution - one typed string to at most one user
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def resolve_login_identifier(identifier):
    """The single User a typed sign-in identifier refers to, or None.

    WHY .first() IS SAFE HERE, AND THE ONE THING THAT WOULD BREAK IT
      Every field in LOGIN_IDENTIFIER_PRECEDENCE is declared unique=True on the User model
      (afc_auth/models.py: username line 43, uid line 45, email line 46). A filter on a unique
      column returns at most ONE row, so .first() is DETERMINISTIC here - it is not picking
      arbitrarily out of a multi-row match, there can never be a second row to pick from.

      That makes uniqueness a load-bearing assumption of this function rather than a detail. If
      somebody ever drops one of those unique constraints, this quietly turns into "whichever row
      the database felt like returning", which is precisely the class of bug this module was
      written to remove. tests_login_identifiers.UniqueColumnAssumptionTests asserts all three are
      still unique and fails loudly if that ever stops being true.

    WHAT THIS FUNCTION DELIBERATELY DOES NOT DO
      It does NOT take the password, and it does NOT fall through to the next column when a
      password does not match. That rejected design looks friendlier ("keep looking until one of
      them accepts the password") and is a real vulnerability: it turns a single typed string into
      a password probe against up to three different accounts per request, on an endpoint with NO
      rate limiting and NO failed-attempt lockout anywhere on the path. Resolution is decided here,
      in full, BEFORE the caller ever consults a password, and the caller checks that password
      exactly once against exactly one row.

    Matching is unchanged from the query this replaced: exact on username and uid, case-insensitive
    on email. No stripping is done, deliberately - trimming the input would silently change which
    strings match, and this is the login path.
    """
    if not identifier:
        return None

    User = get_user_model()
    for _field, lookup in LOGIN_IDENTIFIER_PRECEDENCE:
        match = User.objects.filter(**{lookup: identifier}).first()
        if match is not None:
            return match
    return None


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Prevention - stop a value landing in two login columns at once
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def cross_field_conflict(value, field, exclude_pk=None):
    """Is `value` already in use as a DIFFERENT kind of login identifier on another account?

    Returns (holder, held_as) - the User and the field name they hold it in - or (None, None).

    `field` is the column the caller is about to write `value` into, and it is SKIPPED: a clash
    inside the same column is an ordinary uniqueness problem, and every caller already checks that
    itself with a better-worded message ("That in-game name is already taken"). What this catches
    is the cross-column case uniqueness cannot see, e.g. writing "9137457129" into uid when another
    row is NAMED "9137457129".

    `exclude_pk` leaves the row being edited out, so re-saving your own profile unchanged, or an
    admin setting a UID that equals the target's OWN username, is not reported as a conflict.

    Empty values return no conflict: a blank uid is a legitimate "not set" (the column is nullable
    and 1,218 accounts are in that state), and filtering on it would match hundreds of rows.
    """
    if not value:
        return None, None

    User = get_user_model()
    for other_field, lookup in LOGIN_IDENTIFIER_PRECEDENCE:
        if other_field == field:
            continue
        qs = User.objects.filter(**{lookup: value})
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        holder = qs.first()
        if holder is not None:
            return holder, other_field
    return None, None


def anonymous_conflict_message(field, held_as):
    """Wording for a conflict shown to someone who is NOT an admin (registration, profile edit).

    It never names the holder or their value. The person hitting this is unauthenticated (signup)
    or is editing their own profile, and telling them "that is Kinglarry21's UID" would confirm the
    existence of another account to anyone who cares to probe. Admin surfaces DO name the holder,
    because an admin needs to know where to go and is already trusted with that.
    """
    return (
        f"That {IDENTIFIER_LABELS[field]} is already in use as another player's "
        f"{IDENTIFIER_LABELS[held_as]}. Players can sign in with their in-game name, their email "
        f"or their UID, so the same value cannot be used for both. Please use a different one."
    )
