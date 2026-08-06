# afc_auth/views_two_factor.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# TWO-FACTOR AUTHENTICATION - the HTTP layer (owner 2026-08-06)
#
# Every rule (code lifetime, attempt cap, send limits, hashing, single use) lives in
# afc_auth/two_factor.py. This module is only routing, auth gating, and response shaping, exactly
# like afc_auth/views_watchlist.py is for the watchlist: function-based @api_view, inline Bearer
# auth via validate_token, inline dict responses. Route mounting is in afc_auth/urls.py.
#
# ENDPOINTS (prefix auth/)
#   PUBLIC - these run BEFORE a session exists, so they are gated by the challenge token alone:
#     • POST auth/two-factor/verify/     two_factor_verify   login step two -> session token
#     • POST auth/two-factor/resend/     two_factor_resend   send the login code again
#   BEARER - the user is already signed in and is managing their own 2FA:
#     • GET  auth/two-factor/status/           two_factor_status         is it on, codes left
#     • POST auth/two-factor/send-code/        two_factor_send_code      proof code for enable/disable
#     • POST auth/two-factor/enable/           two_factor_enable         flip on + backup codes ONCE
#     • POST auth/two-factor/disable/          two_factor_disable        flip off
#     • POST auth/two-factor/backup-codes/     two_factor_regenerate_backup_codes
#
# TWO RULES THAT SHAPE EVERY RESPONSE HERE
#   1. NEVER LEAK ACCOUNT EXISTENCE OR STATE. The public endpoints run pre-session, so an unknown,
#      expired, consumed and attempt-burned challenge token all produce the SAME message. A caller
#      cannot use them to learn whether an account exists or whether it has 2FA on.
#   2. NEVER LOG OR RETURN A CODE. The plaintext code exists only inside two_factor.issue_challenge
#      and the email it sends. Backup codes are returned exactly once, at the moment they are
#      generated, and never again.
#
# FRONTEND: lib/twoFactor.ts wraps all seven. The login second step is
# app/(auth)/_components/TwoFactorStep.tsx (used by both LoginForm.tsx and components/AuthModal.tsx);
# the management surface is app/(user)/profile/security/ -> _components/TwoFactorSecurity.tsx.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import two_factor
from .models import TwoFactorChallenge, TwoFactorSettings
from .views import establish_session, validate_token


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §0  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _bearer_user(request):
    """The signed-in user for a Bearer request, or (None, Response) on failure.

    Same shape and the same two status codes as require_admin/require_head_admin in views.py, so
    the auth failure modes are identical across the whole afc_auth surface."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


# The ONE message every failed public 2FA call returns. Deliberately says nothing about which part
# was wrong (unknown token vs expired vs consumed vs wrong code) because these endpoints answer
# unauthenticated callers. See rule 1 in the header.
_GENERIC_CHALLENGE_ERROR = "That code is not valid. Request a new one and try again."


def _send_message(issued):
    """The sentence to show for an issue_challenge result. Three different things can mean
    "no new code went out" and they need three different sentences:

      • sent            - a code really was emailed.
      • cooldown/hourly - we reused a code that is already in their inbox, so telling them to look
                          there is true and useful.
      • delivery_failed - the mail did NOT go out. Saying "a code is on its way" here would be a
                          lie that leaves someone waiting for an email that is never coming, so we
                          say so and point at the way back in (resend, or a recovery code).

    NOTE we still return the challenge on a delivery failure rather than refusing the request. That
    is deliberate: a user with recovery codes can still finish signing in through the same screen,
    whereas refusing outright would lock every 2FA user out for the length of an SMTP outage."""
    if issued["sent"]:
        return "We sent a code to your email."
    if issued["reason"] == "delivery_failed":
        return "We could not send the code just now. Try again, or use a recovery code."
    return "A code is already on its way. Check your inbox."


def _settings_payload(user):
    """The status body, shared by the status/enable/disable endpoints so the client always gets the
    same shape back and can just replace its local state with the response."""
    row = two_factor.settings_for(user)
    enabled = bool(row and row.is_enabled)
    return {
        "enabled": enabled,
        "method": (row.method if row else two_factor.DEFAULT_METHOD),
        "enabled_at": (row.enabled_at.isoformat() if (row and row.enabled_at) else None),
        "available_methods": list(two_factor.ENABLED_METHODS),
        "destination": two_factor.get_method(row.method if row else two_factor.DEFAULT_METHOD)
                                 .destination_hint(user),
        "backup_codes_remaining": two_factor.backup_codes_remaining(user) if enabled else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  Login step two (public - no session exists yet, by definition)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def two_factor_verify(request):
    """POST /auth/two-factor/verify/  PUBLIC. Body: { challenge_token, code } or
    { challenge_token, backup_code }.

    Step TWO of signing in: exchange the challenge token minted by /auth/login/ plus the emailed
    code for a real session. On success the response is byte-identical to a normal login response
    (it IS the same code path - afc_auth.views.establish_session), so the frontend AuthContext
    handles it without knowing 2FA happened.

    RESPONSE
      • 200 { message, session_token, user{id,username,language}, geo }
      • 400 { message, attempts_left } - wrong or dead code. `attempts_left` lets the UI warn
             someone before their fifth wrong guess burns the challenge; it is never a hint about
             the code itself.
      • 429 - the attempt cap is spent; start again from the login form.

    AUTH: the challenge token itself. It is single-purpose (purpose="login"), single-use, expires in
    10 minutes, and grants nothing on its own - it is not accepted anywhere a session token is.

    Consumed by: frontend lib/twoFactor.ts verifyTwoFactor(), called from
    app/(auth)/_components/TwoFactorStep.tsx."""
    token = (request.data.get("challenge_token") or "").strip()
    code = (request.data.get("code") or "").strip()
    backup_code = (request.data.get("backup_code") or "").strip()

    challenge = two_factor.get_challenge(token, purpose="login")
    if challenge is None:
        return Response({"message": _GENERIC_CHALLENGE_ERROR}, status=status.HTTP_400_BAD_REQUEST)

    user = challenge.user

    # ── Path A: a recovery code. The user has lost their inbox, which is the whole reason backup
    #    codes exist. A correct one burns the challenge too, so it cannot be paired with a guess. ──
    if backup_code:
        if two_factor.consume_backup_code(user, backup_code):
            challenge.consume()
            return Response(establish_session(request, user), status=status.HTTP_200_OK)
        # A wrong backup code costs an attempt exactly like a wrong emailed code, so this is not a
        # way around the cap.
        challenge.attempts += 1
        challenge.save(update_fields=["attempts"])
        if challenge.attempts >= TwoFactorChallenge.MAX_ATTEMPTS:
            challenge.consume()
            return Response({"message": _GENERIC_CHALLENGE_ERROR, "attempts_left": 0},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        return Response({"message": _GENERIC_CHALLENGE_ERROR,
                         "attempts_left": two_factor.attempts_left(challenge)},
                        status=status.HTTP_400_BAD_REQUEST)

    # ── Path B: the emailed code. ──
    ok, reason = two_factor.verify_code(challenge, code)
    if ok:
        return Response(establish_session(request, user), status=status.HTTP_200_OK)

    if reason == "locked":
        return Response({"message": _GENERIC_CHALLENGE_ERROR, "attempts_left": 0},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    return Response({"message": _GENERIC_CHALLENGE_ERROR,
                     "attempts_left": two_factor.attempts_left(challenge)},
                    status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
def two_factor_resend(request):
    """POST /auth/two-factor/resend/  PUBLIC. Body: { challenge_token }.

    Send the login code again when the first one did not arrive. Issuing a new code INVALIDATES the
    old one (two_factor.issue_challenge), so the response carries a NEW challenge_token that the
    client must swap in - otherwise the user would be typing a fresh code against a dead challenge.

    RESPONSE
      • 200 { message, challenge_token, code_sent, retry_after, destination } - when code_sent is
             false we are inside the 60s cooldown or past the hourly ceiling and reused the code
             already in their inbox; retry_after says how long until another send is possible.
      • 400 - unknown/expired challenge (same generic message as verify).

    AUTH: the challenge token. Consumed by lib/twoFactor.ts resendTwoFactorCode()."""
    token = (request.data.get("challenge_token") or "").strip()
    challenge = two_factor.get_challenge(token, purpose="login")
    if challenge is None:
        return Response({"message": _GENERIC_CHALLENGE_ERROR}, status=status.HTTP_400_BAD_REQUEST)

    issued = two_factor.issue_challenge(challenge.user, purpose="login")
    fresh = issued["challenge"] or challenge
    return Response({
        "message": "We sent another code." if issued["sent"] else _send_message(issued),
        "challenge_token": fresh.token,
        "code_sent": issued["sent"],
        # True only when the mail genuinely failed to go out, so the screen can say that instead of
        # telling someone to check an inbox nothing was sent to.
        "delivery_failed": issued["reason"] == "delivery_failed",
        "retry_after": issued["retry_after"],
        "destination": issued["destination"],
    }, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Managing your own 2FA (Bearer - the user is already signed in)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def two_factor_status(request):
    """GET /auth/two-factor/status/  Bearer auth. No body.

    Everything the security page needs to render: whether 2FA is on, which method, when it was
    switched on, how many recovery codes are left, and the masked destination a code would go to.

    RESPONSE 200 { enabled, method, enabled_at, available_methods, destination,
                   backup_codes_remaining }

    Deliberately a SEPARATE endpoint rather than new keys on /auth/get-user-profile/: that payload
    is fetched on every page load by AuthContext and is the single most load-bearing response on the
    site. 2FA state is needed on exactly one screen, so it is fetched from exactly one screen.

    Consumed by: lib/twoFactor.ts getTwoFactorStatus(), from
    app/(user)/profile/_components/TwoFactorSecurity.tsx and the admin/organizer nudge
    (components/TwoFactorPrompt.tsx)."""
    user, err = _bearer_user(request)
    if err:
        return err
    return Response(_settings_payload(user), status=status.HTTP_200_OK)


@api_view(["POST"])
def two_factor_send_code(request):
    """POST /auth/two-factor/send-code/  Bearer auth. Body: { purpose: "enable" | "disable" }.

    Sends the proof code that both the enable and the disable flow require. Turning 2FA ON has to
    prove the method actually reaches the user BEFORE the flag flips (otherwise we would lock
    someone out of their own account with a factor that never arrives); turning it OFF has to prove
    it is really them at the keyboard and not someone who walked up to an unlocked laptop.

    RESPONSE
      • 200 { message, challenge_token, code_sent, retry_after, destination, expires_in }
      • 400 - unknown purpose, or the method cannot reach this user (no email on the account).
      • 409 - purpose does not match the current state (enable when already on, disable when off).
      • 429 - the hourly send budget is spent and there is no live code to reuse.

    Consumed by: lib/twoFactor.ts sendTwoFactorProofCode()."""
    user, err = _bearer_user(request)
    if err:
        return err

    purpose = (request.data.get("purpose") or "").strip()
    if purpose not in ("enable", "disable"):
        return Response({"message": "purpose must be 'enable' or 'disable'."},
                        status=status.HTTP_400_BAD_REQUEST)

    already_on = two_factor.is_enabled_for(user)
    if purpose == "enable" and already_on:
        return Response({"message": "Two-factor authentication is already on for this account."},
                        status=status.HTTP_409_CONFLICT)
    if purpose == "disable" and not already_on:
        return Response({"message": "Two-factor authentication is not on for this account."},
                        status=status.HTTP_409_CONFLICT)

    issued = two_factor.issue_challenge(user, purpose=purpose)
    if issued["reason"] == "unavailable":
        return Response({"message": "We have no verified email address for this account."},
                        status=status.HTTP_400_BAD_REQUEST)
    if issued["challenge"] is None:
        return Response({"message": "Too many codes requested. Please try again in an hour.",
                         "retry_after": issued["retry_after"]},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)

    return Response({
        "message": _send_message(issued),
        "challenge_token": issued["challenge"].token,
        "code_sent": issued["sent"],
        "delivery_failed": issued["reason"] == "delivery_failed",
        "retry_after": issued["retry_after"],
        "destination": issued["destination"],
        "expires_in": int(TwoFactorChallenge.CODE_LIFETIME.total_seconds()),
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def two_factor_enable(request):
    """POST /auth/two-factor/enable/  Bearer auth. Body: { challenge_token, code }.

    Turns 2FA ON, but only after the code from /auth/two-factor/send-code/ (purpose "enable") checks
    out. That ordering is the whole point: the flag flips only once the user has DEMONSTRATED the
    method reaches them, so nobody can lock themselves behind a mailbox they cannot open.

    Returns the recovery codes, in PLAINTEXT, EXACTLY ONCE. Only hashes are stored, so this response
    is the only chance to save them; the security page makes the user confirm before it closes.

    RESPONSE
      • 200 { message, backup_codes: [...], ...status payload }
      • 400 { message, attempts_left } - wrong or dead code.
      • 409 - already on.

    Consumed by: lib/twoFactor.ts enableTwoFactor(), from TwoFactorSecurity.tsx."""
    user, err = _bearer_user(request)
    if err:
        return err

    if two_factor.is_enabled_for(user):
        return Response({"message": "Two-factor authentication is already on for this account."},
                        status=status.HTTP_409_CONFLICT)

    challenge = two_factor.get_challenge(
        (request.data.get("challenge_token") or "").strip(), purpose="enable")
    # Belt and braces: the token must belong to THIS user, not merely be a valid enable challenge.
    if challenge is None or challenge.user_id != user.user_id:
        return Response({"message": _GENERIC_CHALLENGE_ERROR}, status=status.HTTP_400_BAD_REQUEST)

    ok, _reason = two_factor.verify_code(challenge, request.data.get("code"))
    if not ok:
        return Response({"message": "That code is not correct. Check the latest email and try again.",
                         "attempts_left": two_factor.attempts_left(challenge)},
                        status=status.HTTP_400_BAD_REQUEST)

    row, _created = TwoFactorSettings.objects.get_or_create(user=user)
    row.is_enabled = True
    row.method = challenge.method
    row.enabled_at = timezone.now()
    row.save(update_fields=["is_enabled", "method", "enabled_at", "updated_at"])

    codes = two_factor.generate_backup_codes(user)

    payload = _settings_payload(user)
    payload["message"] = "Two-factor authentication is on. Save your recovery codes now."
    payload["backup_codes"] = codes
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
def two_factor_disable(request):
    """POST /auth/two-factor/disable/  Bearer auth. Body: { challenge_token, code } or
    { backup_code }.

    Turns 2FA OFF. Requires FRESH proof - either a code just sent by /auth/two-factor/send-code/
    (purpose "disable") or an unused recovery code. A live session alone is not enough: an unlocked
    laptop should not be able to strip the second factor off an account.

    Deletes the recovery codes along with the setting, so nothing from the old configuration can be
    used against the account later.

    RESPONSE
      • 200 { message, ...status payload }
      • 400 { message, attempts_left } - wrong or dead proof.
      • 409 - 2FA is not on.

    Consumed by: lib/twoFactor.ts disableTwoFactor(), from TwoFactorSecurity.tsx."""
    user, err = _bearer_user(request)
    if err:
        return err

    if not two_factor.is_enabled_for(user):
        return Response({"message": "Two-factor authentication is not on for this account."},
                        status=status.HTTP_409_CONFLICT)

    backup_code = (request.data.get("backup_code") or "").strip()
    if backup_code:
        if not two_factor.consume_backup_code(user, backup_code):
            return Response({"message": "That recovery code is not valid."},
                            status=status.HTTP_400_BAD_REQUEST)
    else:
        challenge = two_factor.get_challenge(
            (request.data.get("challenge_token") or "").strip(), purpose="disable")
        if challenge is None or challenge.user_id != user.user_id:
            return Response({"message": _GENERIC_CHALLENGE_ERROR},
                            status=status.HTTP_400_BAD_REQUEST)
        ok, _reason = two_factor.verify_code(challenge, request.data.get("code"))
        if not ok:
            return Response({"message": "That code is not correct. Check the latest email and try again.",
                             "attempts_left": two_factor.attempts_left(challenge)},
                            status=status.HTTP_400_BAD_REQUEST)

    TwoFactorSettings.objects.filter(user=user).update(
        is_enabled=False, enabled_at=None, updated_at=timezone.now())
    # Recovery codes are only meaningful while 2FA is on; leaving them behind would mean a code
    # printed months ago still worked against a freshly re-enabled account.
    user.two_factor_backup_codes.all().delete()

    payload = _settings_payload(user)
    payload["message"] = "Two-factor authentication is off."
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["POST"])
def two_factor_regenerate_backup_codes(request):
    """POST /auth/two-factor/backup-codes/  Bearer auth. Body: { challenge_token, code }.

    Issues a FRESH set of recovery codes and invalidates every previous one. This is what a user
    reaches for after spending most of their codes, or after a set has been seen by someone else.

    Requires the same fresh proof as disabling (a code from /auth/two-factor/send-code/ with purpose
    "disable"), because handing out a new set to whoever is holding the browser would defeat the
    factor entirely.

    RESPONSE
      • 200 { message, backup_codes: [...], ...status payload } - plaintext, shown once.
      • 400 { message, attempts_left } - wrong or dead code.
      • 409 - 2FA is not on (there is nothing to recover into).

    Consumed by: lib/twoFactor.ts regenerateBackupCodes(), from TwoFactorSecurity.tsx."""
    user, err = _bearer_user(request)
    if err:
        return err

    if not two_factor.is_enabled_for(user):
        return Response({"message": "Two-factor authentication is not on for this account."},
                        status=status.HTTP_409_CONFLICT)

    challenge = two_factor.get_challenge(
        (request.data.get("challenge_token") or "").strip(), purpose="disable")
    if challenge is None or challenge.user_id != user.user_id:
        return Response({"message": _GENERIC_CHALLENGE_ERROR}, status=status.HTTP_400_BAD_REQUEST)

    ok, _reason = two_factor.verify_code(challenge, request.data.get("code"))
    if not ok:
        return Response({"message": "That code is not correct. Check the latest email and try again.",
                         "attempts_left": two_factor.attempts_left(challenge)},
                        status=status.HTTP_400_BAD_REQUEST)

    codes = two_factor.generate_backup_codes(user)
    payload = _settings_payload(user)
    payload["message"] = "New recovery codes generated. The old ones no longer work."
    payload["backup_codes"] = codes
    return Response(payload, status=status.HTTP_200_OK)
