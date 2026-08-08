# afc_auth/trusted_devices.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# "REMEMBER THIS DEVICE" - the logic layer (owner 2026-08-08)
#
# WHAT THIS MODULE IS
#   Everything about trusted devices that is NOT HTTP: minting a device token, checking one, listing
#   them for the user, and revoking them. The views (afc_auth/views_two_factor.py) and the login gate
#   (afc_auth/views.py::login_or_challenge) call in here and do nothing themselves, so the rules
#   cannot drift between the place trust is GRANTED and the place it is SPENT. Same split, and the
#   same reasoning, as afc_auth/two_factor.py.
#
# WHY THE FEATURE EXISTS
#   The owner's complaint about two-factor authentication was "logging in each time with a code is
#   stressful". The friction is how OFTEN, not which channel, so changing the channel would not have
#   answered it. A user who ticks the box on their own phone meets the second factor about once a
#   month instead of every single sign-in, and a device nobody ticked is challenged exactly as it is
#   today. afc_auth/models.py TrustedDevice carries the full design note: why 30 days, why the token
#   is split into a selector and a hashed verifier, and what a stolen cookie is and is not worth.
#
# THE TOKEN
#   The browser holds "<selector>.<verifier>". The selector is public and indexed (it says WHICH
#   row); the verifier is the secret and exists in the database only as make_password(verifier), the
#   same hasher the one-time codes and the recovery codes use. So a database dump yields no usable
#   device tokens, and the lookup is still one indexed query.
#
# THE THREE RULES THAT MAKE THIS SAFE, all enforced in is_trusted() below:
#   1. The token must match a live, unexpired row (check_password, constant time).
#   2. That row must belong to the user who just proved their password. A valid token for account A
#      presented while signing in as account B is refused, and the mismatch is not distinguishable
#      from a bad token by the caller.
#   3. Trust is never granted automatically. A row exists only because someone ticked a box.
#
# HOW IT CONNECTS
#   models    : afc_auth.models.TrustedDevice
#   login gate: afc_auth.views.login_or_challenge - the single added block, which asks is_trusted()
#               before it issues a challenge. Every sign-in path (password, Google, Discord) goes
#               through that one function, so no provider can quietly skip or quietly gain trust.
#   HTTP      : afc_auth/views_two_factor.py two_factor_verify MINTS one; everything after that
#               (listing, revoking, and the neighbouring session controls) is in
#               afc_auth/views_devices.py, because a SessionToken is not a 2FA concept.
#   revoked by: afc_auth/two_factor.py consume_backup_code (every recovery-code spend),
#               afc_auth/views_two_factor.py two_factor_disable, afc_auth/views.py change_password +
#               reset_password, afc_auth/views_admin_identity.py admin_set_user_email,
#               afc_auth/views_recovery.py recovery_reset_password. All of them call revoke_all().
#   frontend  : lib/twoFactor.ts holds the cookie; app/(auth)/_components/TwoFactorStep.tsx has the
#               tick; app/(user)/profile/_components/TrustedDevices.tsx lists and revokes, rendered
#               by TwoFactorSecurity.tsx on /profile/security.
#   tests     : afc_auth/tests_trusted_devices.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from .models import TrustedDevice

# The body field the browser sends its device token in, on POST /auth/login/ (and on the Google /
# Discord sign-in paths, which share the same gate). Named as a constant because it is spelled in
# three places: here, the login gate, and lib/twoFactor.ts.
DEVICE_TOKEN_FIELD = "device_token"

# 16 bytes of CSPRNG for the public half, 32 for the secret half. The selector only has to be
# unique and unguessable enough that it cannot be enumerated; the verifier is the part that is
# actually a credential, so it gets the full 256 bits. `secrets`, not `random`, for the same reason
# two_factor._generate_challenge_token uses it: these are bearer values.
_SELECTOR_BYTES = 16
_VERIFIER_BYTES = 32

# The separator between the two halves. A dot cannot appear in token_urlsafe output (which is
# [A-Za-z0-9_-]), so splitting on it is unambiguous.
_SEPARATOR = "."


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  Naming a device so the list is actionable
#
# A user looking at "revoke a device" needs to recognise which one is theirs. We deliberately do NOT
# add a user-agent parsing library for this: the label is a convenience, a wrong one costs nothing,
# and the raw string is stored alongside it anyway. What matters is that the common cases read
# correctly on the phones AFC users actually hold.
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# Browser families, checked IN ORDER because the strings nest: every Chromium browser also says
# "Chrome", and Chrome/Edge/Opera all also say "Safari". So the most specific claim has to win, and
# plain "Chrome"/"Safari" are only reached once the impostors have been ruled out.
_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser", "Samsung Internet"),
    ("UCBrowser", "UC Browser"),
    ("Firefox", "Firefox"),
    ("CriOS", "Chrome"),          # Chrome on iOS, which does not say "Chrome"
    ("FxiOS", "Firefox"),         # Firefox on iOS, same
    ("Chrome", "Chrome"),
    ("Safari", "Safari"),
)

# Platforms, also most-specific-first: an Android user agent contains "Linux", and an iPad's
# contains "Mac OS X".
_PLATFORMS = (
    ("Android", "Android"),
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Windows", "Windows"),
    ("Macintosh", "Mac"),
    ("Mac OS X", "Mac"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def device_label(user_agent: str) -> str:
    """"Chrome on Android" from a user-agent string, or "Unknown device" when it says nothing useful.

    Computed ONCE, when the device is remembered, and stored on the row. Deriving it on every read
    instead would mean improving this function silently rewrites the labels in a list the user has
    already learned to recognise."""
    ua = (user_agent or "").strip()
    if not ua:
        return "Unknown device"

    browser = next((name for token, name in _BROWSERS if token in ua), "")
    platform = next((name for token, name in _PLATFORMS if token in ua), "")

    if browser and platform:
        return f"{browser} on {platform}"
    # One half is still worth showing: "Android" alone is more recognisable than "Unknown device".
    return browser or platform or "Unknown device"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Minting trust
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def remember_device(user, request):
    """Trust the device that made `request`, and return the token it must send back.

    Called from exactly one place: afc_auth.views_two_factor.two_factor_verify, and only when the
    second factor has JUST been satisfied and the user asked for it. Both halves matter - a factor
    the user did not pass cannot buy trust, and a factor they did pass does not buy it silently.

    The PLAINTEXT token is returned here and never again. Only the hash of its secret half is
    stored, exactly like a recovery code, so nobody (including AFC staff, and including anyone
    holding a database dump) can reconstruct a device token after this call returns."""
    selector = secrets.token_urlsafe(_SELECTOR_BYTES)
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)

    user_agent = (request.META.get("HTTP_USER_AGENT", "") or "")[:500]
    # Imported here rather than at module import time: afc_auth.views imports THIS module for the
    # login gate, so a top-level import of it would be circular. Same pattern as
    # two_factor.EmailCodeMethod.deliver.
    from .views import get_client_ip

    try:
        ip = get_client_ip(request) or ""
    except Exception:
        # A geo/proxy-header hiccup must never fail a sign-in that has already succeeded.
        ip = ""

    TrustedDevice.objects.create(
        user=user,
        selector=selector,
        verifier_hash=make_password(verifier),
        user_agent=user_agent,
        label=device_label(user_agent),
        last_ip=ip[:45],
    )
    return f"{selector}{_SEPARATOR}{verifier}"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Spending trust
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def is_trusted(user, token) -> bool:
    """Is `token` a live trusted-device token belonging to `user`?

    THE THREE THINGS THIS REFUSES, and every one of them silently, with the same False:
      • a token that does not parse, is unknown, or whose verifier does not check out
      • a token whose row has expired (the 30-day window is enforced here, not by a cron job, so a
        forgotten cleanup task can never quietly extend somebody's trust)
      • a VALID token that belongs to a DIFFERENT account. This is the one worth stating: the caller
        has just proved the password for `user`, and a device remembered by someone else must not
        skip THEIR second factor. Checking user_id here rather than filtering the query by it is
        deliberate - it makes the rule visible at the line that enforces it.

    Every database error is swallowed into False, for the same reason two_factor.is_enabled_for
    swallows one: migrations are generated on the server in this repo, so code can land before the
    table exists. Failing closed means "everybody gets challenged", which is the pre-feature
    behaviour and is always safe. Failing open would mean a missing table switched 2FA off."""
    if not token or not user:
        return False

    raw = str(token).strip()
    if _SEPARATOR not in raw:
        return False
    selector, _, verifier = raw.partition(_SEPARATOR)
    if not selector or not verifier:
        return False

    try:
        device = TrustedDevice.objects.filter(selector=selector).first()
    except Exception:
        return False

    if device is None or not device.is_live():
        return False
    # Rule 2: bound to the user. A token minted for account A cannot skip account B's factor.
    if device.user_id != getattr(user, "user_id", None):
        return False
    if not check_password(verifier, device.verifier_hash):
        return False

    try:
        device.touch()
    except Exception:
        # "Last used" is a nicety on a settings page. It must never be the reason a sign-in fails.
        pass
    return True


def device_from_request(request, user) -> bool:
    """is_trusted() for the token carried in a sign-in request body.

    Split out so the login gate reads as one question ("is this a device we remember?") and so the
    field name is spelled in exactly one place. `request.data` is safe on every sign-in path,
    including the Discord callback, which is a GET with no body: DRF hands back an empty mapping
    rather than raising, so that path simply finds no token and is challenged exactly as today."""
    try:
        token = request.data.get(DEVICE_TOKEN_FIELD)
    except Exception:
        # A non-DRF request, or an unparseable body. No token means no trust, which is the safe answer.
        return False
    return is_trusted(user, token)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  Listing and revoking
#
# REVOCATION DELETES THE ROW. It does not set a flag. A revoked credential that is still in the
# table is a credential waiting for the one query that forgot to filter it out, and "remove this
# device" is a promise the user should be able to take literally. Deleting also means the list they
# are looking at is exactly the set of things that can skip their second factor, with nothing hidden
# behind a status column.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def live_devices(user):
    """This user's still-trusted devices, most recently used first. Expired rows are excluded here
    rather than deleted, so the list is honest even if nothing has ever swept the table."""
    return (TrustedDevice.objects
            .filter(user=user, expires_at__gt=timezone.now())
            .order_by("-last_used_at"))


def revoke_one(user, device_id) -> bool:
    """Forget ONE device. True when a row was removed, False when there was nothing to remove.

    Scoped to `user` in the query itself, so a caller cannot revoke somebody else's device by
    guessing an id. Idempotent: revoking something already gone returns False rather than raising,
    which is what lets the UI treat a double tap on a phone as a no-op instead of an error."""
    deleted, _ = TrustedDevice.objects.filter(user=user, id=device_id).delete()
    return deleted > 0


def revoke_all(user) -> int:
    """Forget EVERY device for this user, and return how many were forgotten.

    THE CALL SITES, and why each one is not optional:
      • a RECOVERY CODE was spent (two_factor.consume_backup_code) - the sharpest of the five, and
        the one a reviewer should check first. Somebody reaching for a recovery code has lost their
        normal factor: the inbox, the phone, or the account itself. Every browser holding permission
        to skip the second step is suspect at that exact moment, so they all go. That call sits at
        the chokepoint, so it covers the login second step, the disable flow and the switch-to-
        authenticator flow at once.
      • two-factor authentication switched off (views_two_factor.two_factor_disable) - the trust
        only ever existed to skip a factor that no longer exists. Leaving the rows behind would mean
        switching 2FA back on next year silently re-honoured a device from last year.
      • password changed (views.change_password) and password reset (views.reset_password) - the
        usual reason somebody changes their password is that they think it leaked. If the devices
        survived, an attacker who had signed in and ticked the box would keep a standing pass around
        the second factor, and the one action the user knows to take would not have taken it away.
      • an admin moving the account's email (views_admin_identity.admin_set_user_email) - that
        endpoint exists to RESCUE an account. It already ends every session and takes 2FA down; if
        it left trusted devices standing, the rescue tool would be the thing that preserved the
        attacker's access.
      • a completed WhatsApp password recovery (views_recovery.recovery_reset_password) - the
        sharpest case of all: that flow deliberately does NOT ask for the second factor, on the
        grounds that the factor still stands at the next sign-in. A device left trusted is the one
        thing that would make that reasoning false, so the revocation is what the claim rests on.

    Deliberately NOT a call site: enabling 2FA (there is nothing to revoke, rows only exist after a
    factor has been passed) and switching method WITH THE CURRENT FACTOR (the device already proved
    that factor, and Google and GitHub both keep trust across a method change). Switching method
    with a RECOVERY CODE does revoke, through the chokepoint above, because that is not a preference
    change - it is somebody who no longer has their factor."""
    deleted, _ = TrustedDevice.objects.filter(user=user).delete()
    return deleted


def revoke_all_quietly(user) -> int:
    """revoke_all() that can never raise, for call sites where a failure must not undo the thing
    that was actually asked for.

    Used by the password, admin-email and recovery paths: the password HAS been changed by the time
    we get here, and blowing up on a missing table (migrations are generated on the server in this
    repo) would return a 500 for an action that succeeded, leaving the user thinking it failed. The
    2FA-disable path calls revoke_all directly instead, because there the whole point of the request
    is to take protection down and a silent partial failure would be worth knowing about."""
    try:
        return revoke_all(user)
    except Exception as exc:
        # Logs the exception and the username, never a token.
        print(f"Trusted-device revocation failed for {getattr(user, 'username', '?')}: {exc}")
        return 0
