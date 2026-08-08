# afc_auth/views_devices.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# DEVICES AND SESSIONS - the HTTP layer (owner 2026-08-08)
#
# WHAT THIS MODULE IS
#   The "where am I signed in, and what gets to skip my second factor" half of /profile/security.
#   Two different things live here, and keeping the difference straight is the whole point of the
#   page, so it is stated once, plainly, and the UI copy says the same thing:
#
#     A TRUSTED DEVICE is permission to SKIP THE SECOND STEP on one browser for 30 days. It is not
#     a sign-in. Whoever holds it still needs the password. Removing one means that browser is
#     asked for a code again, the very next time.
#
#     A SESSION is being signed in RIGHT NOW. It expires after 3 hours of inactivity
#     (SessionToken.SESSION_LIFETIME). Ending one signs that browser out immediately.
#
#   Somebody who lends a friend their phone wants the first. Somebody who left themselves signed in
#   on a cybercafe machine wants the second. Offering only one of them would leave one of those two
#   people with no way to fix their problem.
#
# WHY ITS OWN MODULE
#   afc_auth/views_two_factor.py is about codes and challenges; SessionToken is not a 2FA concept at
#   all. Same reasoning that put the watchlist in views_watchlist.py and recovery in
#   views_recovery.py: one file per surface, function-based @api_view, inline Bearer auth via
#   validate_token, inline dict responses.
#
# ENDPOINTS (prefix auth/) - all BEARER, all about the CALLER'S OWN account. There is deliberately
# no admin variant of any of these: an admin who needs to lock an account out has
# admin_set_user_email (afc_auth/views_admin_identity.py), which already ends every session and
# every trusted device with a typed reason and an audit row.
#   • GET  auth/devices/trusted/                 trusted_devices_list      what can skip the factor
#   • POST auth/devices/trusted/revoke/          trusted_device_revoke     forget one, or all
#   • GET  auth/devices/sessions/                sessions_list             where you are signed in
#   • POST auth/devices/sessions/sign-out-others/ sessions_sign_out_others sign out everywhere else
#
# HOW IT CONNECTS
#   logic    : afc_auth/trusted_devices.py (every trusted-device rule; nothing here re-implements
#              one) and afc_auth.models.SessionToken for the session half.
#   login    : the tokens listed here are what afc_auth.views.login_or_challenge consults, so
#              revoking one takes effect on the very next sign-in with no cache to wait out.
#   frontend : lib/twoFactor.ts (listTrustedDevices / revokeTrustedDevice / listSessions /
#              signOutOtherSessions) -> app/(user)/profile/_components/TrustedDevices.tsx, rendered
#              on /profile/security under TwoFactorSecurity.
#   tests    : afc_auth/tests_trusted_devices.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import trusted_devices
from .models import SessionToken
from .views import get_client_ip, validate_token


def _bearer(request):
    """(user, session_token, None) for a valid Bearer request, or (None, None, Response) on failure.

    Returns the RAW TOKEN as well as the user, which _bearer_user in views_two_factor.py does not:
    signing out "everywhere else" has to know which session is the one asking, and that is the only
    thing that identifies it. Same two status codes as every other afc_auth auth gate."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = auth.split(" ")[1]
    user = validate_token(token)
    if not user:
        return None, None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, token, None


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  Trusted devices - what may skip the second factor
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def trusted_devices_list(request):
    """GET /auth/devices/trusted/  Bearer auth. No body.

    Every browser this user has told AFC to remember, most recently used first. Expired ones are
    already filtered out (trusted_devices.live_devices), so what comes back is exactly the set of
    devices that can skip the second step right now - nothing hidden behind a status column.

    RESPONSE 200
      { devices: [ { id, label, last_ip, created_at, last_used_at, expires_at } ], count,
        trust_days }
      - label      "Chrome on Android", so the user can tell their own phone from anything else.
      - trust_days how long trust lasts, echoed so the page can say "30 days" without hardcoding a
                   number that lives in TrustedDevice.TRUST_LIFETIME.
      NO TOKEN, not even a fragment of one: the secret half exists only as a hash (see
      afc_auth/trusted_devices.py), and the id is all the revoke call needs.

    NOT PAGINATED, deliberately, and this is the one place in the codebase where that is the right
    call: the list is one row per browser the user has personally ticked a box on. A person with
    twenty is already an outlier, and a "load more" on a security page would mean a device could be
    hiding on page two. The user must be able to see all of them at once or the page does not do its
    job.

    Consumed by: lib/twoFactor.ts listTrustedDevices(), from
    app/(user)/profile/_components/TrustedDevices.tsx."""
    user, _token, err = _bearer(request)
    if err:
        return err

    devices = [
        {
            "id": d.id,
            "label": d.label or "Unknown device",
            # Shown so a user can spot a device that was used from somewhere they have never been.
            # Blank rather than null when we never resolved one, so the client renders "" not "null".
            "last_ip": d.last_ip or "",
            "created_at": d.created_at.isoformat(),
            "last_used_at": d.last_used_at.isoformat(),
            "expires_at": d.expires_at.isoformat(),
        }
        for d in trusted_devices.live_devices(user)
    ]
    return Response({
        "devices": devices,
        "count": len(devices),
        "trust_days": trusted_devices.TrustedDevice.TRUST_LIFETIME.days,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def trusted_device_revoke(request):
    """POST /auth/devices/trusted/revoke/  Bearer auth. Body: { device_id } or { all: true }.

    Stop trusting a device. The very next sign-in from it asks for a code again: there is no cache
    and no grace period, because the login gate reads the row every time (see
    afc_auth.views.login_or_challenge).

    ONE ENDPOINT FOR BOTH, rather than a DELETE per id plus a separate "forget all": they are the
    same action over a different set, and the panic case ("I do not recognise one of these") is
    usually answered by removing the lot. Two endpoints would be two places for the ownership check
    to be forgotten.

    WHAT IT DOES NOT DO: it does not sign that device out. Trust and session are different things
    (see the module header), and quietly ending someone's session because they tidied their device
    list would be a surprise. The page offers signing out separately, and says so.

    RESPONSE
      • 200 { message, revoked }  - `revoked` is how many rows went. Zero is a 200, not an error:
             revoking something already gone is a no-op, which is what makes a double tap on a
             phone harmless.
      • 400 - neither device_id nor all was given.

    AUTH: Bearer, and the delete is scoped to the caller in the query itself
    (trusted_devices.revoke_one), so guessing another user's device id removes nothing.

    Consumed by: lib/twoFactor.ts revokeTrustedDevice() / revokeAllTrustedDevices()."""
    user, _token, err = _bearer(request)
    if err:
        return err

    revoke_all = request.data.get("all")
    revoke_all = revoke_all is True or str(revoke_all or "").strip().lower() in ("true", "1", "yes")
    if revoke_all:
        return Response({"message": "Those devices will be asked for a code next time.",
                         "revoked": trusted_devices.revoke_all(user)},
                        status=status.HTTP_200_OK)

    device_id = request.data.get("device_id")
    try:
        device_id = int(device_id)
    except (TypeError, ValueError):
        return Response({"message": "device_id or all is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    removed = trusted_devices.revoke_one(user, device_id)
    return Response({"message": "That device will be asked for a code next time.",
                     "revoked": 1 if removed else 0},
                    status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Sessions - where the account is signed in right now
#
# WHY THIS EXISTS ALONGSIDE THE DEVICE LIST: since 2026-07-04 a new sign-in no longer clears the
# other sessions (afc_auth.views.establish_session), so a user genuinely can be signed in on four
# things at once and had, until now, no way to see or stop that. It also completes the trusted-device
# story: forgetting a device stops it skipping the factor NEXT time, but if that browser is signed in
# at this moment, it stays signed in for up to three more hours. Someone who has lost a phone needs
# both, so the page offers both and explains which does what.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def sessions_list(request):
    """GET /auth/devices/sessions/  Bearer auth. No body.

    How many live sessions this account has, and when they expire. Newest first.

    RESPONSE 200
      { sessions: [ { created_at, expires_at, current } ], count, others }
      - current  true for the session making this request, so the page can say "this device" and
                 never invite somebody to sign themselves out by accident.
      - others   count minus the current one: exactly the number the sign-out control will end, so
                 the button can say the true number instead of a vague "everywhere else".
      NO TOKEN and NO ID. Individual sessions are not addressable on purpose: a session token is a
      live credential, and this endpoint exists to answer "how many" and to feed one blunt,
      unambiguous control. Per-session sign-out would need a handle for each, which is a new
      identifier for a credential, to solve a problem "sign out everywhere else" already solves.

    Consumed by: lib/twoFactor.ts listSessions(), from
    app/(user)/profile/_components/TrustedDevices.tsx."""
    user, token, err = _bearer(request)
    if err:
        return err

    now = timezone.now()
    rows = (SessionToken.objects
            .filter(user=user, expires_at__gt=now)
            .order_by("-created_at"))
    sessions = [
        {
            "created_at": s.created_at.isoformat(),
            "expires_at": s.expires_at.isoformat(),
            "current": s.token == token,
        }
        for s in rows
    ]
    return Response({
        "sessions": sessions,
        "count": len(sessions),
        "others": sum(1 for s in sessions if not s["current"]),
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def sessions_sign_out_others(request):
    """POST /auth/devices/sessions/sign-out-others/  Bearer auth. No body.

    End every session for this account EXCEPT the one making the request. The caller stays signed in
    (which is what makes this safe to offer as a single tap: nobody can lock themselves out with
    it), and every other browser 401s on its next request and is shown the sign-in modal by the
    frontend's existing auth:session-expired handling.

    IDEMPOTENT: running it twice ends nothing the second time and still returns 200 with a count of
    zero, so a double tap is harmless.

    NOTE WHAT IT DELIBERATELY LEAVES ALONE: trusted devices. Signing a browser out does not make it
    untrusted, because they answer different questions - the user may well want their own phone
    signed out and still remembered. The two controls sit next to each other on the page and each
    says what it does. Someone who wants both takes both, which is one extra tap and no ambiguity.

    RESPONSE 200 { message, signed_out } - how many sessions were ended.

    Consumed by: lib/twoFactor.ts signOutOtherSessions()."""
    user, token, err = _bearer(request)
    if err:
        return err

    # .exclude(token=...) rather than deleting-then-recreating: the caller's own row is untouched,
    # so their next request validates against exactly the token they are already holding.
    signed_out = SessionToken.objects.filter(user=user).exclude(token=token).delete()[0]

    # Logged with the IP for the same reason LoginHistory records one: "somebody signed all my
    # devices out" is a question support gets, and this is the only trace of the answer. Never the
    # token, which is a live credential.
    print(f"Sessions signed out elsewhere for {user.username} from {get_client_ip(request)}: {signed_out}")

    return Response({"message": "Signed out everywhere else.", "signed_out": signed_out},
                    status=status.HTTP_200_OK)
