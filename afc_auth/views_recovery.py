# afc_auth/views_recovery.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# LOCKED OUT? PROVE YOUR WHATSAPP NUMBER (owner 2026-08-08)
#
# WHAT THIS IS
#   A way back in for the people the emailed reset cannot help at all: somebody who has lost access
#   to the inbox, or signed up under an address they mistyped, or simply never receives our mail.
#   The shipped reset emails a six-digit token, which is no use whatsoever to any of them. If they
#   saved a WhatsApp number on the account, they prove that number instead.
#
#   ONE PROOF, TWO ENDINGS, and this is the shape the owner asked for:
#     A. RESET THE PASSWORD  - the priority, and the ordinary case.
#     B. CHANGE THE EMAIL    - for the person whose inbox is dead, so that every future password
#                              reset, receipt and notice reaches them again.
#   Both start from the same three-step proof and diverge only at the end. B is deliberately the
#   narrower of the two: see §4 for the two-step-sign-in rule, which is STRICTER here than on the
#   admin-assisted path.
#
# THE FLOW, END TO END
#   1. POST auth/recovery/whatsapp/start/                 name the account -> an opaque recovery_token
#   2. POST auth/recovery/whatsapp/verify/                prove the number -> a grant_token
#   then ONE of:
#   3A. POST auth/recovery/whatsapp/reset-password/        set the password  -> the account is reset
#   3B. POST auth/recovery/whatsapp/request-email-change/  name the address  -> a code to the NEW one
#       POST auth/recovery/whatsapp/confirm-email-change/  prove the address -> the email is moved
#
#   Step 1 sends a six-digit code to the number on file. Step 2 spends that code. Because a code is
#   SINGLE USE, nothing survives step 2 to authorise step 3, so step 2 mints an AccountRecoveryGrant
#   (afc_auth/models.py) - a narrow, 15-minute, one-capability bearer value that is NOT a session
#   and is accepted nowhere else on the site. That model's header explains why a real SessionToken
#   was rejected outright: handing one back would SIGN THE CALLER IN, and signing in is exactly what
#   two-step verification exists to gate.
#
#   ONE GRANT BUYS ONE ENDING. Whichever branch completes CONSUMES the grant, so a single WhatsApp
#   code cannot both move the address and set the password. Somebody who genuinely wants both does
#   the proof twice, which costs them one extra code and removes a compounding step from an
#   attacker's hands. 3B spends its grant only at the CONFIRM call, because the request call has not
#   changed anything yet.
#
# NOT ONE LINE OF CODE MACHINERY IS RE-IMPLEMENTED HERE
#   Generation, hashing, expiry, single use, the attempt cap, the resend cooldown and the hourly
#   send ceiling all live in afc_auth/two_factor.py and are reached through issue_challenge /
#   get_challenge / verify_code with purpose="recovery". The only new thing is the METHOD that
#   carries the code (two_factor.WhatsAppCodeMethod), which itself sends through
#   afc_whatsapp.tasks.queue_template, the single WhatsApp chokepoint.
#
#   The PASSWORD half is not re-implemented either: §3 does the same things
#   afc_auth/views.py::reset_password does, in the same order, calling the same functions.
#
# ── HOW THIS CANNOT BECOME A WAY AROUND TWO-STEP SIGN-IN ────────────────────────────────────────
#   THE RULE, PLAINLY: a WhatsApp-proved reset sets the password and NOTHING else. Two-factor
#   authentication is never disabled, never reset, never stepped around, and is still demanded in
#   full at the next sign-in. The account comes out of this flow with exactly the protection it went
#   in with.
#
#   That is not a hope, it is a property of four things, each of which has a test in
#   afc_auth/tests_recovery_whatsapp.py:
#
#     1. A RESET IS NOT A SIGN-IN. This flow issues no SessionToken. The new password only gets you
#        as far as the login form, where views.login_or_challenge runs the 2FA gate exactly as it
#        does for anybody else. An attacker who reset the password on a 2FA account is standing at
#        the same locked door they were standing at before.
#     2. A PASSWORD CANNOT STRIP THE FACTOR. views_two_factor.two_factor_disable and totp_confirm
#        both demand FRESH PROOF of the factor as it currently stands (a code delivered by the
#        current method, or an unused recovery code). So "reset the password, then turn 2FA off" is
#        not a path. It could not be walked anyway, because of 1: both endpoints need a session.
#     3. TRUSTED DEVICES DIE WITH THE PASSWORD. This is the load-bearing one, and the only way the
#        feature could actually have become a bypass. A browser that has been remembered
#        (afc_auth/models.py TrustedDevice) SKIPS the second factor, so a reset that left those rows
#        behind would hand an attacker holding the cookie a signed-in session with no factor at all.
#        §3 revokes every one of them, inside the same transaction as the password write.
#     4. THE RECOVERY CODE IS NOT A LOGIN CODE. Every challenge here is minted with
#        purpose="recovery", and two_factor.get_challenge filters on purpose, so the six digits that
#        came over WhatsApp cannot be typed into the login second-step box.
#
#   WHY WE DO NOT ALSO DEMAND THE SECOND FACTOR HERE, which is the tempting thing to do:
#   because it would protect nothing and would lock out the people this is for. The factor already
#   stands between the attacker and the account (1 and 2 above); asking for it a second time, one
#   step earlier, adds no barrier. What it WOULD do is make "I forgot my password" unrecoverable for
#   any user whose factor is an emailed code and who has lost that inbox, which is a large share of
#   the very group this feature exists to serve. An earlier draft of this module demanded it. That
#   draft was solving a different problem: it MOVED THE ACCOUNT'S EMAIL, and for that the demand was
#   right, because handing over the inbox hands over an email-delivered second factor as a side
#   effect. Resetting a password hands over nothing of the sort, so the rule does not carry across.
#
#   THE SECOND ENDING IS GOVERNED SEPARATELY AND MORE HARSHLY. Everything above is about the
#   PASSWORD reset. Moving the account's EMAIL is a different animal, because the default second
#   factor is a code to that very address, and the full rule for it lives in §4: this flow REFUSES
#   outright on any account with two-step sign-in switched on, with no acknowledgement flag and no
#   override, which is stricter than what a head admin is allowed to do. The reasoning, including
#   what the strictness costs and why the narrower rule was rejected, is written out there.
#
#   WHAT THIS DOES COST, said plainly rather than left for a reviewer to find: for an account with
#   NO 2FA, control of the WhatsApp number is control of the account. That is exactly as true of the
#   email path today, and it is why §3 and §5 mail the account's address as a tripwire, why every
#   session goes, why the per-user and per-IP ceilings below are not optional, and why a number
#   nobody has confirmed for a year stops counting at all (see the recycled-number section).
#
# ── A NUMBER IS ONLY EVIDENCE WHILE IT IS STILL THEIRS ──────────────────────────────────────────
#   Mobile numbers are RECYCLED. When a line goes dead the operator eventually reissues it, and the
#   next subscriber inherits every account that still points at it. That is a live risk here and not
#   a theoretical one, because this number is now a route to the whole account, and because the very
#   users this feature serves are the ones who stopped keeping their AFC details up to date. The
#   ordinary tripwire (the notice email in §3 and §5) is weakest exactly here: the premise of the
#   whole flow is that the account's inbox may be dead, so warning it may warn nobody.
#
#   So the number expires AS A RECOVERY FACTOR. UserProfile.whatsapp_number_updated_at records when
#   its owner last typed it, and _number_too_stale refuses anything older than
#   RECOVERY_NUMBER_MAX_AGE. It stays perfectly usable for ordinary notifications; only its power to
#   open the account lapses. Re-saving the number in profile settings restarts the clock, which is
#   the only self-serve way back, and it is why edit_profile stamps the field on EVERY save
#   including a re-save of the same digits.
#
# ── NOTHING LEAKS WHETHER AN ACCOUNT EXISTS ─────────────────────────────────────────────────────
#   Step 1 answers a real account, an unknown identifier, an account with no number saved and an
#   account that has opted out of WhatsApp with the SAME message, the SAME status and the SAME
#   response shape, including a recovery_token that is simply not backed by anything. Step 2 then
#   answers that decoy token with the same generic error a wrong code gets, so the two are
#   indistinguishable from outside. Anything that WOULD name an account (the username, the masked
#   address) is disclosed only in step 2's response, which cannot be reached without the code.
#
#   Worth knowing while reading this: the LEGACY email reset does not have this property. Its
#   auth/send-verification-token/ answers an unknown address with a 404 and the sentence "User with
#   this email does not exist." That is a pre-existing leak in a different endpoint and closing it
#   is not this change; it is noted here so nobody reads the two side by side and assumes the
#   careful one is the accident.
#
# HOW IT CONNECTS
#   models   : TwoFactorChallenge (purpose "recovery"), AccountRecoveryGrant, SessionToken,
#              PasswordResetToken, TrustedDevice, EmailChangeRequest,
#              UserProfile.whatsapp_number + .whatsapp_number_updated_at.
#   2FA      : afc_auth/two_factor.py - issue_challenge / get_challenge / verify_code / mask_email.
#   WhatsApp : afc_auth.two_factor.WhatsAppCodeMethod -> afc_whatsapp.tasks.queue_template ->
#              afc_whatsapp.client.send_template. Template name + language from settings
#              (WHATSAPP_LOGIN_CODE_TEMPLATE / _LANG).
#   trail    : AccountRecoveryGrant.outcome / .outcome_detail, written inside the same transaction
#              as the change it authorised. This is the ONLY audit trail the feature has, and it has
#              to be, because afc_auth/middleware.py AuditLogMiddleware returns early when the
#              request has no actor - and every endpoint here is unauthenticated by definition, so
#              set_audit() would be silently ignored and no AuditLog row would ever exist. Grants
#              are marked consumed and never deleted, so the rows are durable.
#   identity : afc_auth/identifiers.py resolve_login_identifier for step 1 (the same resolver
#              sign-in uses, so "the account I log into" and "the account I recover" are the same
#              row).
#   password : afc_auth/views.py reset_password is the sibling §3 mirrors - set_password, then
#              trusted_devices revocation, then the notice email.
#   email    : afc_auth/views.py request_email_change / confirm_email_change are the siblings §4 and
#              §5 mirror; they share the SAME EmailChangeRequest model and the same 10-minute code,
#              and differ only in how the caller proved themselves (a session + the current password
#              there, a WhatsApp code here). afc_auth/views_admin_identity.py admin_set_user_email
#              is the third member of that family: same guards, looser 2FA rule, an admin behind it.
#   mail     : afc_auth.views.send_email (the single localized SMTP chokepoint) with hand-authored
#              copy from email_i18n - "recovery_password_reset" (+ subject "password_reset_recovery")
#              for §3, "recovery_email_changed" (+ subject "email_changed_recovery") for §5, and
#              subject "confirm_new_email_recovery" on the code §4 sends to the new address.
#   frontend : app/(auth)/recover-account/ -> _components/RecoverAccountForm.tsx, wrapped by
#              lib/recovery.ts. Offered as a choice on /forgot-password and linked from /login.
#   tests    : afc_auth/tests_recovery_whatsapp.py.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
import random
import re
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import trusted_devices, two_factor
from .email_i18n import subject_for
from .identifiers import IDENTIFIER_LABELS, cross_field_conflict, resolve_login_identifier
from .models import (
    AccountRecoveryGrant,
    EmailChangeRequest,
    PasswordResetToken,
    SessionToken,
    TwoFactorChallenge,
    User,
    canonical_profile,
)
from .views import (
    email_change_code,
    email_recovery_email_changed,
    email_recovery_password_reset,
    get_client_ip,
    is_valid_email,
    language_for_country,
    send_email,
)

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §0  Constants and shared helpers
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# The ONE sentence step 1 ever returns. It is deliberately conditional ("if ... we have sent"), so
# it is true for an account that got a code, for an unknown identifier, for an account with no
# number on file, and for one that opted out of WhatsApp. See the leak section in the header.
_START_MESSAGE = (
    "If that account has a WhatsApp number saved, we have sent a 6 digit code to it. "
    "Enter the code to carry on."
)

# The ONE error every failed step-2 call returns. Says nothing about which part was wrong (unknown
# token, decoy token, expired, consumed, attempt-burned, wrong code), because a caller who could
# tell those apart could use them to learn whether an account exists.
_GENERIC_CODE_ERROR = "That code is not valid. Request a new one and try again."

# The ONE error for a dead grant at step 3.
_GENERIC_GRANT_ERROR = (
    "That recovery session has expired. Start again from the recovery page."
)

# ── Per-IP throttle on step 1 ───────────────────────────────────────────────────────────────────
# The PER-USER ceiling that actually protects a person (5 codes an hour, and a 60 second cooldown
# between sends) is enforced inside two_factor.issue_challenge and is not re-implemented here. This
# is the other half of the problem: step 1 is unauthenticated, in-game names are public on the site,
# and every WhatsApp message costs real money, so one script could walk a list of usernames and
# spend AFC's Meta budget while pestering hundreds of players. Counted per clock hour in the shared
# Redis cache with the same add()-then-incr() idiom as afc_auth/broadcast_ratelimit.py.
#
# 20 an hour is far more than any honest person needs (a real user makes one or two attempts) and
# small enough that the abuse case is not worth running. It counts REQUESTS, not sends, so probing
# for which accounts have a number saved is throttled just as hard as real sends.
RECOVERY_START_PER_IP_PER_HOUR = 20

# ── How long a saved number stays good AS A RECOVERY FACTOR ─────────────────────────────────────
# See the "a number is only evidence while it is still theirs" section in the header for the
# argument. This is the number that decides it.
#
# WHY TWELVE MONTHS. Two forces pull opposite ways and neither has a clean answer:
#   SHORTER is safer. A recycled line can be reissued to a stranger, and every day the stored number
#     is stale is a day somebody else can open the account. I do NOT have a verified figure for how
#     quickly African operators reissue a dead number, and I am not going to invent one; what is
#     well established is that reissuing happens and that the window is measured in months.
#   LONGER is kinder. Cutting the window costs real rescues, and the person it costs is by
#     definition already locked out and already out of options.
# Twelve months sits where a normal player who saved a number and kept the same line never meets the
# wall at all, while a number nobody has touched for over a year - which is the profile of a number
# that has actually been given up - stops being accepted. It is deliberately generous, because the
# guard is not the only protection (the per-user and per-IP ceilings, the notice emails and the
# two-step refusal in §4 all still apply) and because a wall nobody can see is a cruel place to be
# strict.
#
# WHAT IT DOES NOT DO: it does not stop notifications. A stale number keeps receiving room details
# and every other WhatsApp AFC sends. Only its power to open the account lapses.
RECOVERY_NUMBER_MAX_AGE = timedelta(days=365)

# ── The password rule, server side ──────────────────────────────────────────────────────────────
# Mirrors ResetPasswordFormSchema in frontend/lib/zodSchemas.tsx rule for rule, so a password the
# form accepted is never refused here and nobody is told two different stories.
#
# WHY IT EXISTS AT ALL, when views.reset_password has no such check: because a client-side rule is
# not a rule. That endpoint predates this one and closing its gap is not this change (it is reached
# from a form that enforces the same thing), but a NEW public endpoint that sets a password is not
# the place to inherit a missing check. The cost of skipping it is an account whose password is "1"
# and whose owner reasonably believes AFC vetted it.
MIN_PASSWORD_LENGTH = 8
_PASSWORD_RULES = (
    (re.compile(r"[a-z]"), "one lowercase letter"),
    (re.compile(r"[A-Z]"), "one uppercase letter"),
    (re.compile(r"[0-9]"), "one number"),
    (re.compile(r"[!@#$%^&*(),.?\":{}|<>]"), "one special character"),
)


def _password_problem(password):
    """The reason `password` cannot be accepted, or None when it is fine.

    Returns a whole sentence rather than a code, because it goes straight to the screen and this
    flow has nothing to fall back on: somebody who cannot get past this line cannot get in.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Your new password needs at least {MIN_PASSWORD_LENGTH} characters."
    missing = [label for pattern, label in _PASSWORD_RULES if not pattern.search(password)]
    if missing:
        return f"Your new password needs at least {', '.join(missing)}."
    return None


def _generate_grant_token() -> str:
    """The opaque handle the browser carries between step 2 and step 3. 43 URL-safe characters of
    CSPRNG output, minted with `secrets` for the same reason two_factor._generate_challenge_token
    is: it is a bearer value, and the `random` module is not good enough for one."""
    return secrets.token_urlsafe(32)


def _decoy_token() -> str:
    """A recovery_token for a request we are not going to act on.

    It is a real random string that is backed by NOTHING, handed to an unknown identifier so the
    response is shaped identically to a real one. Step 2 will answer it with the same generic error
    a wrong code gets, so an attacker learns nothing from either call. Returning no token, or a
    differently shaped one, would be the leak this whole flow is careful to avoid."""
    return secrets.token_urlsafe(32)


def _ip_throttled(request) -> bool:
    """True when this IP has already made RECOVERY_START_PER_IP_PER_HOUR step-1 requests this hour.

    Fails OPEN: a cache that is down or misconfigured must not take password recovery offline for
    everybody, and the per-user ceiling inside issue_challenge is still in force underneath this.
    """
    try:
        ip = get_client_ip(request) or "unknown"
        key = f"recov_ip:{ip}:{timezone.now().strftime('%Y%m%d%H')}"
        # add() only sets when the key is absent, which is what makes the first request of the hour
        # start the 1 hour TTL; incr() is atomic afterwards.
        if cache.add(key, 1, timeout=3600):
            return False
        return cache.incr(key) > RECOVERY_START_PER_IP_PER_HOUR
    except Exception:
        return False


def _recipient_language(user):
    """The recipient's locale for send_email. Same three-step fallback every other transactional
    send uses: their explicit choice, else the language of their country, else English."""
    try:
        return user.language or language_for_country(user.country) or "en"
    except Exception:
        return "en"


def _number_too_stale(user) -> bool:
    """True when the WhatsApp number on `user` is too old to be treated as proof of anything.

    Reads UserProfile.whatsapp_number_updated_at, the moment its owner last typed the number, and
    compares it against RECOVERY_NUMBER_MAX_AGE. See the header section on recycled numbers.

    Resolved through canonical_profile, NOT profile_of: duplicate UserProfile rows exist in
    production, and canonical_profile (lowest profile_id) is the one row every reader and writer in
    this codebase agrees on. It is the same call two_factor.WhatsAppCodeMethod._number makes to find
    the number itself, so the date this reads always belongs to the number that would be messaged.

    A MISSING DATE COUNTS AS FRESH, which is the one judgement call in here. Migration 0039 stamped
    every profile that already held a number, so in practice NULL now means "no number saved at all"
    - and for such an account issue_challenge answers "unavailable" before this is ever consulted,
    so the branch is unreachable in the ordinary case. Treating an unexpected NULL as STALE would
    silently disable recovery for anybody a future code path creates a number for without stamping
    it, and a security control that turns itself off invisibly is worse than one that leans on the
    other four in this module.

    Fails OPEN on any lookup error, for the same reason _ip_throttled does: a profile query that
    blows up must not take account recovery offline for everybody.
    """
    try:
        profile = canonical_profile(user)
    except Exception:
        return False
    if profile is None:
        return False
    stamped = getattr(profile, "whatsapp_number_updated_at", None)
    if stamped is None:
        return False
    return timezone.now() - stamped > RECOVERY_NUMBER_MAX_AGE


def _live_grant(token):
    """The spendable grant for `token`, or None. Unknown, consumed and expired all return None and
    the caller cannot tell them apart, exactly like two_factor.get_challenge."""
    if not token:
        return None
    grant = AccountRecoveryGrant.objects.filter(token=token).first()
    if grant is None or not grant.is_live():
        return None
    return grant


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  Step one - name the account, get a code on WhatsApp
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def recovery_start(request):
    """POST auth/recovery/whatsapp/start/  PUBLIC. Body: { identifier }.

    PURPOSE
      Send a six-digit code to the WhatsApp number already saved on the account named by
      `identifier`, so its owner can prove the account is theirs and reset a forgotten password
      without the emailed reset token.

    REQUEST   { identifier } - an email address, an in-game name or a Free Fire UID. The SAME three
              a player can sign in with, resolved by the SAME function sign-in uses
              (identifiers.resolve_login_identifier), so "the account I log into" and "the account
              I recover" can never be two different rows.
    RESPONSE  200 { message, recovery_token } - ALWAYS, for every input. See below.
              400 identifier missing.
              429 { message } - this IP has made too many attempts this hour.

    THE RESPONSE IS THE SAME WHATEVER HAPPENS, AND THAT IS THE POINT
      A real account with a usable number, an identifier nobody holds, an account with no number
      saved, an account whose owner switched WhatsApp off, and an account whose number has not been
      confirmed for over a year all produce the identical body. The token handed back in the last
      four cases is a decoy backed by nothing, and step 2 answers it with the same generic error a
      wrong code gets. Anything else would turn this endpoint into an account-existence oracle, and
      in-game names are public on the site.

    THE NUMBER CAN BE TOO OLD TO COUNT
      A saved number stops being accepted as proof after RECOVERY_NUMBER_MAX_AGE, because a mobile
      line that has been given up is reissued to somebody else. It keeps receiving ordinary
      notifications; only its power to open the account lapses. Re-saving it in profile settings
      restarts the clock. See _number_too_stale.

    RATE LIMITS
      Per user: two_factor.issue_challenge enforces a 60 second cooldown and 5 sends an hour for
      purpose "recovery", counted in the database. Scoped to this purpose, so a recovery attempt
      cannot burn the sign-in code budget of a user who is simply signing in.
      Per IP: RECOVERY_START_PER_IP_PER_HOUR, because this endpoint is unauthenticated and every
      WhatsApp message is billed. See _ip_throttled.

    AUTH      none, by definition. The caller cannot sign in; that is why they are here.
    CONSUMED BY  frontend lib/recovery.ts startWhatsAppRecovery(), from
                 app/(auth)/_components/RecoverAccountForm.tsx.
    """
    identifier = (request.data.get("identifier") or "").strip()
    if not identifier:
        return Response({"message": "Enter your email, in-game name or UID."},
                        status=status.HTTP_400_BAD_REQUEST)

    if _ip_throttled(request):
        return Response(
            {"message": "Too many recovery attempts from this device. Try again in an hour."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = resolve_login_identifier(identifier)

    # Every "we are not sending anything" branch returns the SAME body as the success branch. The
    # branches are kept separate rather than collapsed because they are genuinely different facts
    # and a future reader needs to see that each one was considered.
    if user is None:
        return Response({"message": _START_MESSAGE, "recovery_token": _decoy_token()},
                        status=status.HTTP_200_OK)

    # The number is on the account but nobody has confirmed it for over a year, so it is no longer
    # treated as proof: a line that has been given up gets reissued, and the next subscriber would
    # inherit the account. Answered with the SAME decoy body as every other refusal, so this cannot
    # be used to work out which accounts hold an old number. See _number_too_stale.
    if _number_too_stale(user):
        return Response({"message": _START_MESSAGE, "recovery_token": _decoy_token()},
                        status=status.HTTP_200_OK)

    issued = two_factor.issue_challenge(user, purpose="recovery", method_code="whatsapp")

    # "unavailable" = no number saved, or the number is unusable, or the user opted out of WhatsApp
    # (two_factor.WhatsAppCodeMethod._number treats all three the same way). Nothing was minted.
    if issued["reason"] == "unavailable" or issued["challenge"] is None:
        return Response({"message": _START_MESSAGE, "recovery_token": _decoy_token()},
                        status=status.HTTP_200_OK)

    # A DELIVERY FAILURE still returns the challenge rather than refusing, the same call
    # login_or_challenge makes: the code may yet be resent, and refusing would strand the user on a
    # Meta outage. It is not reported either, because "the send failed" is itself a statement about
    # an account that exists.
    return Response(
        {"message": _START_MESSAGE, "recovery_token": issued["challenge"].token},
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Step two - prove the number
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def recovery_verify(request):
    """POST auth/recovery/whatsapp/verify/  PUBLIC. Body: { recovery_token, code }.

    PURPOSE
      Spend the WhatsApp code and, on success, mint the short-lived grant that authorises the
      password reset in step 3.

    REQUEST   { recovery_token, code }
    RESPONSE  200 { message, grant_token, expires_in, username, current_email }
                  `current_email` is MASKED (two_factor.mask_email). Both it and `username` are
                  there for RECOGNITION: they let somebody confirm they are about to reset the
                  right account, which matters because an in-game name can be mistyped into a real
                  stranger's account.
              400 { message, attempts_left } - wrong or dead code, or a decoy token.
              429 { message, attempts_left: 0 } - the attempt cap is spent; start again.

    WHY THE ACCOUNT IS NAMED HERE AND NOWHERE EARLIER
      Both fields say something about a real account: that it exists, and roughly which address it
      uses. Both are behind the code, which only reaches the phone already on the account. Step 1
      discloses neither.

    WHAT THIS DOES NOT GRANT
      No session, no token accepted anywhere else, and no change to the account. The grant
      authorises exactly one call to recovery_reset_password and expires in 15 minutes.

    AUTH      the recovery_token plus the code.
    CONSUMED BY  frontend lib/recovery.ts verifyWhatsAppRecovery().
    """
    token = (request.data.get("recovery_token") or "").strip()
    code = (request.data.get("code") or "").strip()

    # A decoy token from step 1 lands here, finds nothing, and gets the same answer a wrong code
    # gets. attempts_left is reported as the full cap so the shape matches a real first attempt.
    challenge = two_factor.get_challenge(token, purpose="recovery")
    if challenge is None:
        return Response(
            {"message": _GENERIC_CODE_ERROR, "attempts_left": TwoFactorChallenge.MAX_ATTEMPTS},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # verify_code owns the attempt counter, the per-challenge cap and consuming on success. Nothing
    # about "is this code right" or "has this been guessed too often" is decided in this module.
    ok, reason = two_factor.verify_code(challenge, code)
    if not ok:
        if reason == "locked":
            return Response({"message": _GENERIC_CODE_ERROR, "attempts_left": 0},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        return Response({"message": _GENERIC_CODE_ERROR,
                         "attempts_left": two_factor.attempts_left(challenge)},
                        status=status.HTTP_400_BAD_REQUEST)

    user = challenge.user

    # One live grant per user: a second verify replaces the first, so a token minted on an
    # abandoned attempt (or on a shared machine) cannot still be spent afterwards. Same
    # single-in-flight rule two_factor.issue_challenge applies to challenges.
    with transaction.atomic():
        AccountRecoveryGrant.objects.filter(
            user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())
        grant = AccountRecoveryGrant.objects.create(user=user, token=_generate_grant_token())

    return Response(
        {
            # Ending-NEUTRAL on purpose. This used to say "now choose a new password", which was
            # true when the reset was the only ending; it is now shown on the screen that offers
            # the email move as well, so naming one of the two would misdescribe the other.
            "message": "That's you. Now choose what you want to fix.",
            "grant_token": grant.token,
            "expires_in": int(AccountRecoveryGrant.LIFETIME.total_seconds()),
            "username": user.username,
            # Masked, never the full address: this screen is reachable by whoever is holding the
            # phone, and the point is recognition, not disclosure.
            "current_email": two_factor.mask_email(user.email or ""),
        },
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Step three - set the new password
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def recovery_reset_password(request):
    """POST auth/recovery/whatsapp/reset-password/  PUBLIC (the grant is the credential).
    Body: { grant_token, new_password }.

    PURPOSE
      Set a new password on the account whose WhatsApp number was just proved, and clear away
      everything that could let the account's previous state be used afterwards.

    REQUEST   grant_token   from recovery_verify. Single use, 15 minute life.
              new_password  at least 8 characters with a lowercase letter, an uppercase letter, a
                            number and a special character. Same rule the form enforces; see
                            _password_problem for why it is enforced here as well.
    RESPONSE  200 { message, sessions_ended, devices_forgotten }
              400 bad or dead grant, or a password that does not meet the rule.

    ── THIS DOES WHAT views.reset_password DOES, IN THE SAME ORDER ─────────────────────────────
      set the password, drop the pending reset token, revoke the trusted devices, mail the notice.
      It is deliberately not a new way to SET a password, only a new way to prove you may. The one
      thing it adds is the SessionToken purge, explained below.

    ── TWO-STEP SIGN-IN IS UNTOUCHED, AND STILL REQUIRED ───────────────────────────────────────
      Nothing in this function reads or writes TwoFactorSettings. An account with 2FA on comes out
      of here with 2FA on, and the next sign-in challenges for it exactly as before. The module
      header sets out the four reasons that makes this safe rather than a bypass; the one that had
      to be written into this function is the third, the trusted-device revocation below, because a
      remembered browser is the single thing that skips the factor.

    ── EVERY SESSION IS ENDED ──────────────────────────────────────────────────────────────────
      views.reset_password does not do this, and arguably should: a changed password plus a live
      cookie is how a takeover survives the reset. Here it is not optional, because the premise of
      the whole flow is that the person asking may not be the person currently holding a session on
      the account. Any pending PasswordResetToken goes with them, so a token emailed to an address
      the user no longer reads cannot be spent afterwards either.

    ── THE ACCOUNT'S ADDRESS IS TOLD, ALWAYS ───────────────────────────────────────────────────
      Best effort, and a dead address is the expected case for many of these users. It is sent
      anyway because it costs nothing and, for the accounts whose address DOES work, it is the only
      warning a real owner gets that somebody used their WhatsApp number. See
      views.email_recovery_password_reset.

    CONSUMED BY  frontend lib/recovery.ts resetPasswordWithWhatsApp().
    """
    grant = _live_grant((request.data.get("grant_token") or "").strip())
    if grant is None:
        return Response({"message": _GENERIC_GRANT_ERROR}, status=status.HTTP_400_BAD_REQUEST)

    user = grant.user

    # Validation first, and note that a failure here deliberately does NOT burn the grant: a weak
    # password is an honest mistake by somebody we have just authenticated, and making them redo the
    # WhatsApp code because they forgot a capital letter would be a cruel way to treat a person who
    # is already locked out. The grant's 15 minute clock is what bounds the window, not a counter.
    new_password = request.data.get("new_password") or ""
    problem = _password_problem(new_password)
    if problem:
        return Response({"message": problem}, status=status.HTTP_400_BAD_REQUEST)

    # Everything above is validation. Only now do we write: a `return Response(...)` from inside an
    # atomic block silently discards the writes that came before it on this codebase.
    with transaction.atomic():
        user.set_password(new_password)
        user.save(update_fields=["password"])

        sessions_ended = SessionToken.objects.filter(user=user).delete()[0]
        PasswordResetToken.objects.filter(user=user).delete()

        # THE LINE THAT KEEPS THIS FROM BEING A 2FA BYPASS. A remembered browser skips the second
        # factor, so the devices have to go with the password: leaving one standing would let
        # whoever held the account before this call walk straight back in past 2FA.
        #
        # revoke_all, INSIDE the transaction, rather than the revoke_all_quietly that
        # views.reset_password uses. There the password write is already committed by the time the
        # call happens, so swallowing the error is the only option that does not report a 500 for
        # work that succeeded. Here nothing is committed yet, so a failure can and should take the
        # whole reset down with it: "password changed, devices still trusted" is precisely the state
        # this feature must never produce, and refusing the reset is the safe way to fail.
        devices_forgotten = trusted_devices.revoke_all(user)

        # Record WHAT this proof bought, in the same transaction as the write it authorised. The
        # usual audit trail cannot see this endpoint (AuditLogMiddleware skips unauthenticated
        # mutations), so the grant row IS the trail. No detail: there is nothing to say beyond
        # "the password changed", and a password must never appear anywhere near here.
        grant.consume(outcome=AccountRecoveryGrant.OUTCOME_PASSWORD)

    # ── the tripwire. Best effort: a dead address is the normal case for these users. ───────────
    lang = _recipient_language(user)
    when = timezone.now().strftime("%d %b %Y, %H:%M UTC")
    try:
        send_email(
            user.email,
            subject_for("password_reset_recovery", lang),
            email_recovery_password_reset(user.username, when, lang=lang),
            language=lang,
            prelocalized=True,
        )
    except Exception as exc:
        # Never fail the reset on a mail error, and never log the address beside it.
        print(f"Recovery password-reset notice failed for {user.username}: {exc}")

    return Response(
        {
            "message": "Done. Your password has been changed. Sign in with it now.",
            "sessions_ended": sessions_ended,
            "devices_forgotten": devices_forgotten,
        },
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  The other ending - move the account onto an address the owner can actually read
#
# ── WHY THIS EXISTS AT ALL, when §3 already rescues the account ─────────────────────────────────
#   A password reset gets somebody back IN. It does not fix the thing that locked them out. An
#   account whose address is a typo or a dead mailbox still cannot receive a receipt, a result
#   notification, an event reminder or the NEXT password reset, and its owner has to come back here
#   every single time. Until 2026-08-08 the only cure was a support ticket and a head admin running
#   views_admin_identity.admin_set_user_email by hand. This is that same repair, self-serve, for the
#   subset of users who can prove a WhatsApp number.
#
# ── THE TWO-STEP SIGN-IN RULE, WHICH IS STRICTER HERE THAN FOR AN ADMIN ─────────────────────────
#   THE RULE: if the account has two-step sign-in switched ON, this endpoint REFUSES. There is no
#   acknowledgement flag, no override, and no way to reach the change from here. The password reset
#   in §3 stays open to those accounts; only the email move is closed.
#
#   Compare the admin path: admin_set_user_email refuses too, but a head admin may pass
#   disable_two_factor: true, which tears 2FA down in the open and proceeds. That difference is
#   deliberate and it is the point of this section.
#
#   WHY STRICTER, in the order the reasons actually matter:
#
#   1. THE PROOF IS WEAKER, AND THE ADMIN PATH KNOWS WHO IT IS TALKING TO. A head admin has checked
#      identity OUT OF BAND before they touch that endpoint. They are named on an audit row and an
#      AdminHistory row, they had to type a reason, and a compromised admin account is detectable
#      afterwards. Here there is no name and no out-of-band step: the caller proved possession of a
#      phone number, which the header's recycled-number section explains is not the same as being
#      the account's owner. Weaker proof must buy less.
#   2. ALLOWING IT WITH A TEAR-DOWN WOULD SIMPLY *BE* THE BYPASS. §3 is safe because the second
#      factor is still standing when the reset finishes: the attacker who resets a password on a 2FA
#      account is at the same locked door as before. Let this endpoint switch 2FA off and that
#      argument collapses in one move - number, then email, then the ordinary emailed reset, then in
#      with no factor at all. The whole module would have been undone by its own second ending.
#   3. ALLOWING IT AND LEAVING 2FA ON WOULD BE WORSE, NOT SAFER. The default factor is a code to the
#      ACCOUNT EMAIL (two_factor.EmailCodeMethod). Move the address and that code follows it, so the
#      factor would read as "on" while delivering to whoever asked for the change. A protection that
#      is present in the settings page and absent in reality is the most dangerous of the three
#      options, because nobody goes looking for it.
#
#   WHAT THE RULE COSTS, said plainly rather than left for a reviewer to find. TOTP is a real
#   option on this codebase (two_factor.ENABLED_METHODS is ("email", "totp")), and for a TOTP
#   account reason 3 does not apply: moving the address would not hand over the factor, and reason 2
#   holds anyway because TOTP would still be demanded at sign-in. So a narrower rule was available -
#   refuse only when the enabled factor is delivered by email - and it was NOT taken:
#     • it makes the safety of this endpoint depend on a property of ANOTHER module's method
#       registry. two_factor.py says adding "whatsapp" to ENABLED_METHODS is a one-line change; the
#       day somebody makes it, a narrower rule silently starts allowing a WhatsApp-proved email move
#       on a WhatsApp-guarded account, which is one proof doing both jobs.
#     • the population it would serve is close to empty: it is the intersection of "has a WhatsApp
#       number saved" (116 of 6,809 accounts), "has TOTP switched on" and "has lost the inbox".
#     • those users are not stranded. Support can still do it, which is the same answer everyone
#       had before this feature existed.
#   One rule that cannot rot, for a cost measured in single-digit users who keep a working path.
#
# ── THE NEW ADDRESS IS PROVEN, WHICH THE ADMIN PATH DOES NOT DO ─────────────────────────────────
#   A second six-digit code goes to the NEW address and has to come back before anything is written.
#   admin_set_user_email deliberately skips that step because the user it serves may not be able to
#   read mail ANYWHERE, and a bounce would re-strand them. That reasoning does not carry across: a
#   person typing a new address here is claiming they can read it, and if they cannot then the whole
#   change is pointless. So the proof is free, and it buys the thing this feature most needs to avoid
#   - a typo that moves the account onto an inbox that does not exist, locking it out permanently
#   with no way back. This is the same machinery, the same model and the same 10-minute code the
#   signed-in flow uses (views.request_email_change / confirm_email_change + EmailChangeRequest);
#   only the way the caller proved themselves is different.
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# The refusal for an account with two-step sign-in on. It names the reason, because by this point
# the caller has already proved the number and the account is not a secret from them, and it points
# at the two things they CAN still do rather than leaving them at a dead end.
_TWO_FACTOR_REFUSAL = (
    "This account uses two-step sign-in, so its email address cannot be changed this way. "
    "Two-step sign-in is the protection standing between your account and anyone who gets hold of "
    "your phone number, and moving the address here would step around it. You can still reset your "
    "password on this page, and support can change the address for you once they have checked who "
    "you are."
)

# The ONE error a failed confirm returns, for the same reason _GENERIC_CODE_ERROR exists: a caller
# who could tell "no pending change" from "wrong code" from "expired" learns something about the
# state of an account they may not own.
_GENERIC_EMAIL_CODE_ERROR = (
    "That code is not valid. Request a new one and try again."
)


def _refuse_if_two_factor(user):
    """The §4 rule, in one place so the request and confirm calls cannot drift apart.

    Returns a Response to send, or None to proceed. Checked on BOTH calls rather than only the
    first: the two are minutes apart, another session could switch 2FA on in between, and the check
    costs one indexed query.
    """
    if two_factor.is_enabled_for(user):
        return Response(
            {"message": _TWO_FACTOR_REFUSAL, "two_factor_enabled": True},
            status=status.HTTP_409_CONFLICT,
        )
    return None


def _email_attempt_key(grant):
    """Cache key counting wrong codes against ONE grant. Keyed on the grant's primary key, never on
    its token: the token is a bearer value and does not belong in a key that other systems can
    enumerate, and the pk identifies the same single in-flight attempt just as well."""
    return f"recov_email_attempts:{grant.pk}"


def _burn_email_attempt(grant) -> int:
    """Record one wrong code against `grant` and return how many have been used, this one included.

    The add()-then-incr() idiom used everywhere else on this codebase (broadcast_ratelimit.py,
    _ip_throttled above): add() only writes when the key is absent, which is what starts the TTL on
    the first wrong guess, and incr() is atomic thereafter. The TTL is the grant's own lifetime, so
    the counter cannot outlive the thing it is counting against.

    FAILS CLOSED, which is the opposite of _ip_throttled and deliberate. A cache that is down here
    would otherwise mean UNLIMITED guesses at a six-digit code with an account takeover on the other
    side of it, so an error is reported as "the cap is spent" and the caller in §5 burns the grant.
    The cost of being wrong is one person redoing a WhatsApp code; the cost of failing open is the
    account.
    """
    key = _email_attempt_key(grant)
    ttl = int(AccountRecoveryGrant.LIFETIME.total_seconds())
    try:
        if cache.add(key, 1, timeout=ttl):
            return 1
        return int(cache.incr(key))
    except Exception:
        return TwoFactorChallenge.MAX_ATTEMPTS


@api_view(["POST"])
def recovery_request_email_change(request):
    """POST auth/recovery/whatsapp/request-email-change/  PUBLIC (the grant is the credential).
    Body: { grant_token, new_email }.

    PURPOSE
      Start moving the account onto an address its owner can actually read, having already proved
      the WhatsApp number on it. Sends a six-digit code to the NEW address; nothing is written until
      recovery_confirm_email_change receives that code back.

    REQUEST   grant_token  from recovery_verify. NOT consumed here - the confirm call needs it, and
                           nothing has changed yet, so there is nothing to burn.
              new_email    the address to move to.
    RESPONSE  200 { message, new_email }
              400 dead grant, malformed address, unchanged address, an address already registered to
                  another account, or one that is another player's in-game name.
              409 { message, two_factor_enabled: true } - the account has two-step sign-in on. See
                  §4: this is a flat refusal with no override, and it is stricter than the
                  admin-assisted path on purpose.
              429 another code was requested less than a minute ago.

    THE GUARDS ARE THE ADMIN TOOL'S GUARDS
      Case-insensitive duplicate refusal and the cross-field login collision check are the same two
      admin_set_user_email runs, reached through the same shared helper
      (identifiers.cross_field_conflict), because sign-in resolves ONE typed string against email,
      username and uid together. 106 accounts have a username that IS a well-formed email address,
      so an address can collide with somebody's in-game name even when no account holds it as an
      email - and this endpoint exists to END a lockout, so it must not create one.

    AUTH      the grant. CONSUMED BY frontend lib/recovery.ts requestEmailChangeWithWhatsApp().
    """
    grant = _live_grant((request.data.get("grant_token") or "").strip())
    if grant is None:
        return Response({"message": _GENERIC_GRANT_ERROR}, status=status.HTTP_400_BAD_REQUEST)

    user = grant.user

    refused = _refuse_if_two_factor(user)
    if refused:
        return refused

    new_email = (request.data.get("new_email") or "").strip()
    if not new_email:
        return Response({"message": "Enter the new email address."},
                        status=status.HTTP_400_BAD_REQUEST)

    ok, msg = is_valid_email(new_email)
    if not ok:
        return Response({"message": msg}, status=status.HTTP_400_BAD_REQUEST)

    if new_email.lower() == (user.email or "").lower():
        return Response({"message": "That is already the address on this account."},
                        status=status.HTTP_400_BAD_REQUEST)

    # Case-insensitive, matching admin_set_user_email: MySQL's default collation already compares
    # this way, but __iexact states the rule in the code so it cannot drift with a collation change.
    if User.objects.exclude(pk=user.pk).filter(email__iexact=new_email).exists():
        return Response({"message": "That email is already registered to another account."},
                        status=status.HTTP_400_BAD_REQUEST)

    # The cross-column trap uniqueness cannot see. See the docstring.
    name_clash, held_as = cross_field_conflict(new_email, "email", exclude_pk=user.pk)
    if name_clash:
        return Response(
            {"message": f"That address is already another player's {IDENTIFIER_LABELS[held_as]}, and players sign in with their name, their email or their UID. Using it here would lock both accounts out."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # 60 second cooldown between codes, the same one views.request_email_change and resend_token
    # apply, read off the pending row's own timestamp so no extra state is needed.
    existing = EmailChangeRequest.objects.filter(user=user).first()
    if existing and (timezone.now() - existing.created_at).total_seconds() < 60:
        return Response({"message": "Wait at least a minute before asking for another code."},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)

    # update_or_create because EmailChangeRequest is OneToOne: asking again for a DIFFERENT address
    # replaces the pending one rather than leaving two in flight, and refreshes both the 10 minute
    # code window and the cooldown above.
    code = str(random.randint(100000, 999999))
    EmailChangeRequest.objects.update_or_create(
        user=user,
        defaults={"new_email": new_email, "token": code, "created_at": timezone.now()},
    )
    # A fresh code deserves a fresh allowance: the attempt counter is scoped to the grant, and
    # leaving a spent one behind would refuse the first guess at a code that was only just sent.
    cache.delete(_email_attempt_key(grant))

    # ── The code goes to the NEW address and nowhere else. That IS the proof of ownership. ──────
    # THE RETURN VALUE IS CHECKED, not an exception. views.send_email catches everything internally
    # and answers False; it has never raised. Wrapping it in try/except would therefore be a check
    # that can never fire, and the user would be parked on a code screen waiting for something that
    # was never sent, with no way to tell. This is the ONE mail in the module that is load-bearing:
    # the two notice emails elsewhere are best-effort because the account has already been changed
    # by the time they go out, whereas nothing can continue without this one.
    lang = _recipient_language(user)
    sent = send_email(new_email, subject_for("confirm_new_email_recovery", lang),
                      email_change_code(code, lang), language=lang, prelocalized=True)
    if not sent:
        # The pending row is DROPPED rather than left behind, and that is the point of doing this
        # here: the 60 second cooldown above reads that row's timestamp, so keeping it would make
        # AFC's own failed send lock the user out of retrying for a minute. They can try again
        # immediately instead.
        EmailChangeRequest.objects.filter(user=user).delete()
        # The address is never logged beside the failure.
        print(f"Recovery email-change code could not be sent for {user.username}")
        return Response(
            {"message": "We could not send a code to that address. Check it is right and try again."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(
        {
            "message": "We sent a 6 digit code to that address. Enter it to finish the change.",
            "new_email": new_email,
        },
        status=status.HTTP_200_OK,
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5  Finish the move - prove the new address, then write it
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def recovery_confirm_email_change(request):
    """POST auth/recovery/whatsapp/confirm-email-change/  PUBLIC (the grant is the credential).
    Body: { grant_token, code }.

    PURPOSE
      Spend the code sent to the new address and move the account onto it, clearing away everything
      that could let the account's previous state be used afterwards.

    REQUEST   grant_token  from recovery_verify. CONSUMED here, on success and on a spent attempt
                           cap. One grant buys one ending.
              code         the six digits sent to the new address by the request call.
    RESPONSE  200 { message, email, previous_email, sessions_ended, devices_forgotten, reactivated }
              400 dead grant, or a wrong / expired / missing code (one generic message for all of
                  them, with attempts_left).
              409 the account has two-step sign-in on. See §4.
              429 the attempt cap is spent; the grant is burned and the whole proof starts again.

    ── EVERY SESSION ENDS AND EVERY REMEMBERED DEVICE IS FORGOTTEN ─────────────────────────────
      Same clearing-out as §3, for a sharper reason. This endpoint's premise is that the person
      asking may not be the person currently holding a session on the account, and an address that
      has been moved cannot be moved back by whoever lost it: from here on every "forgot password"
      goes to the new inbox. A live cookie or a remembered browser surviving that would be how a
      takeover keeps its foothold. Any pending PasswordResetToken goes too, so a token mailed to the
      OLD address cannot be spent after the move.

    ── A NEVER-VERIFIED SIGNUP IS ACTIVATED ────────────────────────────────────────────────────
      On this model User.is_active IS the email-verified flag (views.py ~1604): signup creates the
      row False and the emailed code flips it True, so False means "abandoned signup, never
      verified" and NOT "banned" - bans live on BannedPlayer.is_active, a different model entirely.
      An account stuck False is precisely the mistyped-address case this ending exists for, and its
      owner has just proved the WhatsApp number AND a code at the new address, which is strictly
      more evidence than the emailed signup code they missed. So the flag is flipped, exactly as
      admin_set_user_email flips it and for the same reason.

    ── WHY THE CODE IS CAPPED HERE ─────────────────────────────────────────────────────────────
      EmailChangeRequest has no attempt counter, and the signed-in flow that owns it does not need
      one: reaching it at all requires a session, the current password AND the old address. Here the
      only thing in front of six digits is a grant, so an uncapped check is a 1-in-1,000,000 guess
      repeated as fast as the network allows, inside a 15 minute window. The cap reuses
      TwoFactorChallenge.MAX_ATTEMPTS so this flow states ONE number of allowed guesses. It is
      counted in the shared cache, but ENFORCED by burning the grant in the database, so a cache
      that is down or flushed cannot hand an attacker an unlimited retry.

    CONSUMED BY  frontend lib/recovery.ts confirmEmailChangeWithWhatsApp().
    """
    grant = _live_grant((request.data.get("grant_token") or "").strip())
    if grant is None:
        return Response({"message": _GENERIC_GRANT_ERROR}, status=status.HTTP_400_BAD_REQUEST)

    user = grant.user

    refused = _refuse_if_two_factor(user)
    if refused:
        return refused

    code = (request.data.get("code") or "").strip()
    pending = EmailChangeRequest.objects.filter(user=user).first()

    # Wrong code, no pending change and an expired code are one outcome with one message. Each costs
    # an attempt, so a caller cannot separate them by counting either.
    if not pending or not code or pending.token != code or not pending.is_valid():
        used = _burn_email_attempt(grant)
        if used >= TwoFactorChallenge.MAX_ATTEMPTS:
            # Cap spent. The grant dies in the DATABASE, so the whole WhatsApp proof has to be done
            # again; a cleared cache cannot resurrect the allowance.
            grant.consume()
            EmailChangeRequest.objects.filter(user=user).delete()
            return Response(
                {"message": "Too many wrong codes. Start the recovery again.", "attempts_left": 0},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        return Response(
            {"message": _GENERIC_EMAIL_CODE_ERROR,
             "attempts_left": max(TwoFactorChallenge.MAX_ATTEMPTS - used, 0)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    new_email = pending.new_email
    previous_email = user.email or ""

    # Re-checked at COMMIT time, not just at request time: minutes have passed and somebody else may
    # have registered the address in between. views.confirm_email_change does the same.
    if User.objects.exclude(pk=user.pk).filter(email__iexact=new_email).exists():
        pending.delete()
        return Response(
            {"message": "That address was just registered to another account. Start again with a different one."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reactivated = not user.is_active

    # Everything above is validation. Only now do we write: a `return Response(...)` from inside an
    # atomic block silently discards the writes that came before it on this codebase.
    with transaction.atomic():
        user.email = new_email
        user.is_active = True  # see the docstring: this flag is "email verified", not "not banned"
        user.save(update_fields=["email", "is_active"])

        pending.delete()
        sessions_ended = SessionToken.objects.filter(user=user).delete()[0]
        PasswordResetToken.objects.filter(user=user).delete()
        # revoke_all, INSIDE the transaction and NOT the quiet variant, for the same reason §3 does
        # it: "the address moved but the old browser is still trusted" is exactly the state this
        # must never produce, and refusing the whole change is the safe way to fail.
        devices_forgotten = trusted_devices.revoke_all(user)

        # THE record of the one irreversible thing this feature can do. Written in the same
        # transaction as the address change, so a moved account can never exist without a row
        # naming the address it moved OFF - which, if a recycled number was used to steal the
        # account, is the only place that address still survives. See the model's field comments.
        grant.consume(
            outcome=AccountRecoveryGrant.OUTCOME_EMAIL,
            detail=f"{previous_email or 'none'} -> {new_email}",
        )

    cache.delete(_email_attempt_key(grant))

    # ── tell BOTH addresses. The OLD one first: for its reader this is the tripwire, and the last
    #    message AFC can ever send them about this account. Best effort on each, and a dead old
    #    address is the expected case here - that is why the account needed moving.
    lang = _recipient_language(user)
    when = timezone.now().strftime("%d %b %Y, %H:%M UTC")
    subject = subject_for("email_changed_recovery", lang)
    body = email_recovery_email_changed(user.username, new_email, when, lang=lang)
    for address in (previous_email, new_email):
        if not address:
            continue
        try:
            send_email(address, subject, body, language=lang, prelocalized=True)
        except Exception as exc:
            # Never fail the change on a mail error, and never log the address beside it.
            print(f"Recovery email-change notice failed for {user.username}: {exc}")

    return Response(
        {
            "message": "Done. Your account now uses that email address. Sign in to carry on.",
            "email": new_email,
            "previous_email": previous_email,
            "sessions_ended": sessions_ended,
            "devices_forgotten": devices_forgotten,
            "reactivated": reactivated,
        },
        status=status.HTTP_200_OK,
    )
