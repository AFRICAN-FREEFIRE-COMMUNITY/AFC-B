# afc_auth/two_factor.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# TWO-FACTOR AUTHENTICATION - the logic layer (owner 2026-08-06)
#
# WHAT THIS MODULE IS
#   Everything 2FA that is NOT HTTP: the method registry, code generation, hashing, the rate limits,
#   and verification. The views (afc_auth/views_two_factor.py) and the login gate
#   (afc_auth/views.py::login) do nothing but call into here, so the security rules live in exactly
#   ONE place and cannot drift between the login step, the enable step and the disable step.
#
# WHY A METHOD REGISTRY
#   The first shipped method was EMAIL and only email, because email is the one factor ~6,790 AFC
#   accounts can all actually use (WhatsApp reaches roughly 90 of them). But the flow was written
#   against a small TwoFactorMethod interface instead of hardcoding "send an email", and on
#   2026-08-07 that paid off: TOTP (authenticator apps) was added as a subclass plus one entry in
#   METHODS. No view signature, no response shape and no login-gate line had to change. WhatsApp,
#   the remaining known method, drops in the same way.
#
# HOW IT CONNECTS
#   models   : TwoFactorSettings / TwoFactorChallenge / TwoFactorBackupCode (afc_auth/models.py)
#   email    : afc_auth.views.send_email (the single localized SMTP chokepoint) with the
#              hand-authored copy from afc_auth.email_i18n ("two_factor_code" + subject "two_factor").
#   totp     : pyotp (RFC 6238) for code generation, cryptography.Fernet for secret storage. §6.
#   callers  : afc_auth.views.login_or_challenge (the ONE gate all three sign-in paths use),
#              afc_auth.views_two_factor.* (every endpoint).
#   frontend : the challenge token this module mints is what lib/twoFactor.ts carries between the
#              login form and the code screen.
#
# SECURITY RULES ENFORCED HERE (all of them, in one place)
#   • The plaintext code exists only inside issue_challenge() and inside the email. It is hashed
#     before it touches the database and is NEVER printed, logged or returned in an API response.
#   • Codes expire (TwoFactorChallenge.CODE_LIFETIME), are single-use, and issuing a new one burns
#     every older live challenge for that purpose.
#   • MAX_ATTEMPTS wrong guesses burn the challenge, so knowing the password does not let anyone
#     walk the six-digit space.
#   • Sends are rate limited per user (RESEND_COOLDOWN between sends, MAX_SENDS_PER_HOUR ceiling),
#     counted in the DATABASE rather than the Redis cache so the limit survives a cache flush and
#     so the test suite does not need a live Redis.
#   • Hitting the send ceiling never locks the real user out: we hand back the challenge they
#     already have instead of refusing, so an attacker spamming login cannot deny them their code.
#   • A method that SENDS nothing has no send ceiling to throttle it, so TOTP gets its own
#     cross-challenge attempt budget (MAX_CODELESS_ATTEMPTS_PER_HOUR). Without it, an attacker who
#     already has the password could mint unlimited fresh challenges and walk the code space.
#   • The TOTP secret is stored ENCRYPTED, never in plaintext, and a spent time step can never be
#     replayed. Both are in §6.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
import base64
import hmac
import secrets
import string
import time

import pyotp
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import TwoFactorBackupCode, TwoFactorChallenge, TwoFactorSettings

# How many digits a one-time code has. Six matches every other code AFC already emails (signup
# verification, password reset, email change), so the copy, the inputs and the muscle memory all
# line up. The attempt cap - not the length - is what makes six digits safe.
CODE_LENGTH = 6

# How many backup codes a user gets, and how long each one is. Ten is enough to keep a set usable
# for years without printing a page; 10 characters from a 32-symbol alphabet is ~50 bits, far beyond
# anything guessable, which is why these do not need an attempt cap of their own.
BACKUP_CODE_COUNT = 10
BACKUP_CODE_LENGTH = 10
# Crockford-style alphabet: no 0/O/1/I/L, because these get read off a screen and typed by hand.
BACKUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# ── Authenticator app (TOTP) tunables. Full reasoning in §6. ─────────────────────────────────────
# RFC 6238 defaults, and they are defaults for a reason: Google Authenticator IGNORES the algorithm
# and digits parameters in an otpauth:// URI, so anything other than SHA1/6/30 silently produces an
# app that shows codes AFC would reject. Interoperability wins over a theoretical SHA256 upgrade.
TOTP_DIGITS = 6
TOTP_PERIOD = 30            # seconds per code
TOTP_ALGORITHM = "SHA1"
# How many 30-second steps either side of "now" we accept, to absorb ordinary phone clock drift.
# 1 means a code is accepted for the previous, current and next step: a 90-second window. That is
# what Google, GitHub and AWS use. Larger windows multiply an attacker's guessing surface for no
# real-world gain; smaller ones start rejecting phones that are 40 seconds fast.
TOTP_DRIFT_STEPS = 1
# 32 base32 characters = 160 bits, the minimum RFC 4226 §4 R6 recommends for the shared secret.
TOTP_SECRET_LENGTH = 32
# What the authenticator app lists the entry under. Short on purpose: Google Authenticator truncates
# long issuers in the list, and the account label beside it already carries the username.
TOTP_ISSUER = "AFC"
# How long a started-but-unconfirmed enrolment stays confirmable. A QR left on a shared screen must
# not still be claimable an hour later.
TOTP_ENROLMENT_LIFETIME = timezone.timedelta(minutes=30)
# The throttle that replaces the send ceiling for methods that send nothing (see §4). Total wrong
# guesses allowed per user per rolling hour, ACROSS challenges. 20 is far more than a person
# fat-fingering their own app will ever need, and it turns walking the million-code space into
# roughly 50,000 hours of work.
MAX_CODELESS_ATTEMPTS_PER_HOUR = 20


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  The method interface
#
# A method answers four questions: can this user use me, where would the code go (a string safe to
# show on screen), please deliver this code, and is this submitted code correct.
#
# `requires_delivery = False` is the hook the TOTP method uses - an authenticator app generates the
# code itself, so there is nothing to send, no resend button to render, and no send rate limit that
# means anything.
#
# NOTE WHAT IS *NOT* ON THIS INTERFACE: attempt counting, the attempt cap, consuming a challenge on
# success. Those stay in verify_code() below so every method is throttled and burned identically and
# a new method cannot accidentally ship without them. A method only answers "is this code right".
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class TwoFactorMethod:
    """Base class. Subclass it, set `code`, and register the instance in METHODS below."""

    code = ""                 # the value stored in TwoFactorSettings.method
    requires_delivery = True  # False for a method that generates its own code (TOTP)

    def is_available(self, user) -> bool:
        """Can this user actually receive/produce a code by this method right now?"""
        raise NotImplementedError

    def destination_hint(self, user) -> str:
        """A MASKED description of where the code goes, safe to render on a pre-login screen.

        Pre-login is the important part: the code screen is shown before the session exists, so this
        must never reveal the full address to whoever is holding the browser."""
        raise NotImplementedError

    def deliver(self, user, code) -> bool:
        """Send `code` to the user. Returns True when it went out. Must never raise: a delivery
        failure is reported to the caller, it does not blow up the login request."""
        raise NotImplementedError

    def check_code(self, user, challenge, submitted) -> bool:
        """Is `submitted` the right code for `challenge`? True/False only.

        The DEFAULT is right for every method that mints and delivers its own code: compare against
        the hash stored on the challenge row. A method that derives codes from a shared secret
        (TOTP) overrides this and ignores `challenge.code_hash` entirely."""
        return check_password(submitted, challenge.code_hash)


class EmailCodeMethod(TwoFactorMethod):
    """The one method AFC ships today: a six-digit code to the account's verified email address.

    Delivery goes through afc_auth.views.send_email, the single SMTP chokepoint, with
    prelocalized=True and the hand-authored fr/pt copy from afc_auth.email_i18n - so a French user
    gets a French code email without this module knowing anything about translation."""

    code = "email"

    def is_available(self, user) -> bool:
        return bool(getattr(user, "email", ""))

    def destination_hint(self, user) -> str:
        return mask_email(getattr(user, "email", "") or "")

    def deliver(self, user, code) -> bool:
        # Imported inside the method, not at module import time: afc_auth.views imports this module
        # for the login gate, so a top-level import here would be circular.
        from afc_auth.email_i18n import subject_for
        from afc_auth.views import email_two_factor_code, send_email

        try:
            lang = (getattr(user, "language", "") or "en")
            return bool(send_email(
                user.email,
                subject_for("two_factor", lang),
                email_two_factor_code(code, lang),
                language=lang,
                prelocalized=True,
            ))
        except Exception as exc:
            # Deliberately logs the EXCEPTION and the username, never the code.
            print(f"2FA email delivery failed for {getattr(user, 'username', '?')}: {exc}")
            return False


class TotpMethod(TwoFactorMethod):
    """Authenticator app codes (RFC 6238 TOTP): Google Authenticator, Authy, 1Password, Aegis.

    THE THING THAT MAKES THIS DIFFERENT FROM EVERY OTHER METHOD: nothing is sent. The user's phone
    and this server share a secret and independently derive the same six digits from the clock. So:
      • requires_delivery is False - there is no email, no cooldown, no "resend" that means
        anything, and the login screen must not offer one.
      • destination_hint is empty - there is no address to mask, and telling a pre-login visitor
        "your authenticator app" is already all the method value says.
      • check_code ignores challenge.code_hash completely (issue_challenge stores an UNUSABLE hash
        for this method) and instead walks the accepted time steps. See §6 for drift and replay.

    Setting it up is afc_auth.views_two_factor.totp_setup / totp_confirm: enrol, scan, prove, and
    only then does TwoFactorSettings.method flip to "totp"."""

    code = "totp"
    requires_delivery = False

    def is_available(self, user) -> bool:
        # A CONFIRMED secret we can still decrypt. An enrolment that was started and abandoned is
        # deliberately not enough - active_totp_secret only returns the promoted one.
        return bool(active_totp_secret(user))

    def destination_hint(self, user) -> str:
        return ""

    def deliver(self, user, code) -> bool:
        # Never called (requires_delivery is False). Returning True rather than raising keeps the
        # interface honest if a future caller forgets to check the flag.
        return True

    def check_code(self, user, challenge, submitted) -> bool:
        return consume_totp(user, submitted)


# The registry. Adding WhatsApp later = write a WhatsAppCodeMethod calling
# afc_whatsapp.client.send_template(number, "login_code", lang, body_params=[code]), add it here,
# and add "whatsapp" to ENABLED_METHODS. Nothing else in the stack changes - which is exactly how
# TotpMethod above went in.
METHODS = {
    EmailCodeMethod.code: EmailCodeMethod(),
    TotpMethod.code: TotpMethod(),
}

# What a user is allowed to CHOOSE right now. Kept separate from METHODS so a method can be
# implemented and tested before it is offered to users. Order matters: the security page renders
# the methods in this order, and email stays first because it is the one every account can use.
ENABLED_METHODS = ("email", "totp")

DEFAULT_METHOD = "email"


def get_method(method_code):
    """The TwoFactorMethod for `method_code`, falling back to the default. Never returns None, so
    callers never have to null-check a method they just read out of the database."""
    return METHODS.get(method_code) or METHODS[DEFAULT_METHOD]


def mask_email(email: str) -> str:
    """"jonathan@gmail.com" -> "jo*****@gmail.com". Enough for the owner to recognise their own
    inbox, not enough for a stranger at the keyboard to learn the address."""
    if "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        visible = local[:1]
    else:
        visible = local[:2]
    return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §2  Settings helpers
#
# `is_enabled_for` is the ONE question afc_auth.views.login asks, and it is the reason 2FA cannot
# break login: it swallows a database error (the table not existing yet, because migrations are
# generated on the server in this repo) and answers "no 2FA". A late migration therefore degrades
# to "2FA is not live yet" instead of "nobody can sign in". It does NOT swallow anything else, so a
# user who really has 2FA on is never waved through.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def settings_for(user):
    """This user's TwoFactorSettings row, or None if they have never touched 2FA."""
    try:
        return TwoFactorSettings.objects.filter(user=user).first()
    except Exception:
        return None


def is_enabled_for(user) -> bool:
    """True only when this user has explicitly switched 2FA on AND the method still works for them.

    The method re-check matters, and it is deliberately applied to EVERY method rather than only to
    the ones that deliver something: if an account ends up with no email address, or with a TOTP
    secret this server can no longer decrypt (the Django secret key was rotated - see §6), we must
    not strand it behind a factor that can never be satisfied. The user falls back to a one-step
    sign-in and can set the factor up again, which is a smaller failure than a permanent lockout
    that only a database edit can undo."""
    row = settings_for(user)
    if not row or not row.is_enabled:
        return False
    if not get_method(row.method).is_available(user):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §3  Issuing a challenge
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _generate_code() -> str:
    """A cryptographically random CODE_LENGTH-digit string, leading zeros preserved."""
    return "".join(secrets.choice(string.digits) for _ in range(CODE_LENGTH))


def _generate_challenge_token() -> str:
    """The opaque handle the browser carries between login step one and step two. 43 URL-safe
    characters of CSPRNG output - it is a bearer value for the code screen, so it is generated with
    `secrets`, not the `random` module that generate_session_token() uses."""
    return secrets.token_urlsafe(32)


def _live_challenges(user, purpose):
    """This user's still-usable challenges for `purpose`, newest first."""
    return (TwoFactorChallenge.objects
            .filter(user=user, purpose=purpose, consumed_at__isnull=True,
                    expires_at__gt=timezone.now(), attempts__lt=TwoFactorChallenge.MAX_ATTEMPTS)
            .order_by("-created_at"))


def _sends_last_hour(user, purpose) -> int:
    """How many codes we have already sent this user for `purpose` in the last rolling hour."""
    since = timezone.now() - timezone.timedelta(hours=1)
    return TwoFactorChallenge.objects.filter(
        user=user, purpose=purpose, created_at__gte=since,
    ).count()


def issue_challenge(user, purpose="login", method_code=None):
    """Create (or reuse) a 2FA challenge for `user` and deliver the code.

    Returns a dict the views hand almost straight back to the client:
        {
          "challenge": TwoFactorChallenge | None,
          "sent": bool,          # did a NEW code actually go out on this call
          "reason": None | "cooldown" | "hourly" | "delivery_failed" | "unavailable",
          "retry_after": int,    # seconds until another send is allowed (0 when sent)
          "method": str,
          "destination": str,    # masked, safe to display
        }

    THE RATE-LIMIT SHAPE, AND WHY:
      • A method that DELIVERS NOTHING (TOTP) skips all of it and always gets a fresh challenge:
        every limit below is a limit on sending, and nothing is sent. Its guessing is capped in
        verify_code instead. `sent` is False for it with reason None, which is not a failure - the
        code was already on the user's phone before they asked.
      • A live challenge younger than RESEND_COOLDOWN is REUSED rather than replaced. Two rapid
        submissions therefore mean one email and one valid code, not a race between two codes.
      • Past MAX_SENDS_PER_HOUR we stop SENDING but still hand back the live challenge if there is
        one. Refusing outright would let anyone who knows a password lock the real owner out of
        their own inbox-based factor by burning the hourly budget.
      • Only when there is nothing live AND the hour is spent do we return challenge=None, which the
        views turn into a 429.
    """
    row = settings_for(user)
    method_code = method_code or (row.method if row else DEFAULT_METHOD)
    method = get_method(method_code)
    destination = method.destination_hint(user) if method.is_available(user) else ""

    # Applied to every method, not just the ones that deliver: "no email address" and "no usable
    # authenticator secret" are the same failure, and both must refuse rather than mint a challenge
    # nobody can answer.
    if not method.is_available(user):
        return {"challenge": None, "sent": False, "reason": "unavailable", "retry_after": 0,
                "method": method.code, "destination": ""}

    now = timezone.now()

    # ── (0) Methods that SEND NOTHING (TOTP) take the short path. ─────────────────────────────
    # Every rate limit below this point counts SENDS, and there is no send here: the code is
    # already on the user's phone. Applying the 60-second cooldown or the hourly ceiling to an
    # authenticator user would refuse them a sixth sign-in in an hour for no reason at all. Their
    # guessing is throttled instead by MAX_CODELESS_ATTEMPTS_PER_HOUR, enforced in verify_code.
    #
    # Older live challenges are still burned, so exactly one challenge per user per purpose is
    # answerable at a time - the same single-in-flight rule the email path has.
    if not method.requires_delivery:
        with transaction.atomic():
            for stale in _live_challenges(user, purpose):
                stale.consume()
            challenge = TwoFactorChallenge.objects.create(
                user=user,
                purpose=purpose,
                method=method.code,
                token=_generate_challenge_token(),
                # An UNUSABLE hash (Django's "!" sentinel). Nothing can ever match it, which is
                # exactly right: TotpMethod.check_code never looks at this column, and if a future
                # refactor made it fall back to the default check, the fallback would refuse rather
                # than accept.
                code_hash=make_password(None),
                created_at=now,
                expires_at=now + TwoFactorChallenge.CODE_LIFETIME,
            )
        return {"challenge": challenge, "sent": False, "reason": None, "retry_after": 0,
                "method": method.code, "destination": destination}

    newest = _live_challenges(user, purpose).first()

    # (a) Still inside the cooldown: reuse the code already in their inbox.
    if newest and (now - newest.created_at) < TwoFactorChallenge.RESEND_COOLDOWN:
        wait = TwoFactorChallenge.RESEND_COOLDOWN - (now - newest.created_at)
        return {"challenge": newest, "sent": False, "reason": "cooldown",
                "retry_after": max(1, int(wait.total_seconds())),
                "method": method.code, "destination": destination}

    # (b) Hourly ceiling reached: send nothing more, but do not strand the user.
    if _sends_last_hour(user, purpose) >= TwoFactorChallenge.MAX_SENDS_PER_HOUR:
        return {"challenge": newest, "sent": False, "reason": "hourly",
                "retry_after": 3600, "method": method.code, "destination": destination}

    # (c) Normal path: burn any older live challenge, mint a new code, deliver it.
    code = _generate_code()
    with transaction.atomic():
        for stale in _live_challenges(user, purpose):
            stale.consume()
        challenge = TwoFactorChallenge.objects.create(
            user=user,
            purpose=purpose,
            method=method.code,
            token=_generate_challenge_token(),
            code_hash=make_password(code),
            created_at=now,
            expires_at=now + TwoFactorChallenge.CODE_LIFETIME,
        )

    # Only delivering methods reach this line; the codeless path returned at (0) above.
    delivered = method.deliver(user, code)
    # `code` goes out of scope here and is never written anywhere but the recipient's inbox.

    return {
        "challenge": challenge,
        "sent": bool(delivered),
        "reason": None if delivered else "delivery_failed",
        "retry_after": 0 if delivered else int(TwoFactorChallenge.RESEND_COOLDOWN.total_seconds()),
        "method": method.code,
        "destination": destination,
    }


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §4  Verifying
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def get_challenge(token, purpose=None):
    """Look up a live challenge by its token. Returns None for unknown, consumed, expired or
    attempt-burned tokens - the caller cannot tell those apart, and deliberately so."""
    if not token:
        return None
    qs = TwoFactorChallenge.objects.filter(token=token)
    if purpose:
        qs = qs.filter(purpose=purpose)
    challenge = qs.first()
    if not challenge or not challenge.is_live():
        return None
    return challenge


def _attempts_last_hour(user) -> int:
    """Every wrong guess this user has made in the last rolling hour, summed ACROSS challenges.

    Why across challenges: a per-challenge cap alone is not a throttle when challenges are free to
    mint. That is fine for email (each new challenge costs a send, and sends are capped), but a
    method that sends nothing has no such cost - so this is the number that stops someone with a
    stolen password from walking the code space one fresh challenge at a time."""
    since = timezone.now() - timezone.timedelta(hours=1)
    return TwoFactorChallenge.objects.filter(
        user=user, created_at__gte=since,
    ).aggregate(total=Sum("attempts"))["total"] or 0


def verify_code(challenge, code) -> tuple[bool, str]:
    """Check `code` against `challenge`. Returns (ok, reason).

    WHAT IS RIGHT vs WHAT IS ALLOWED are split on purpose. The METHOD answers "is this the right
    code" (TwoFactorMethod.check_code); everything that makes a wrong answer expensive - the attempt
    counter, the per-challenge cap, the hourly cap, consuming on success - lives here, in one place,
    so no method can ship without it.

    On success the challenge is CONSUMED, so the same code can never be replayed. On failure the
    attempt counter goes up and, at MAX_ATTEMPTS, the challenge is burned outright - the user has to
    start over rather than keep guessing. reason is one of "" (ok), "invalid", "expired", "locked".
    """
    if challenge is None:
        return False, "expired"
    if not challenge.is_live():
        return False, "locked" if challenge.attempts >= TwoFactorChallenge.MAX_ATTEMPTS else "expired"

    method = get_method(challenge.method)

    # The hourly guessing budget for methods with no send ceiling to throttle them (see §4 (0)).
    # Deliberately scoped to those methods: the email path is already throttled by
    # MAX_SENDS_PER_HOUR, and widening this to email would be a behaviour change to a flow that
    # 6,790 accounts are already using.
    #
    # The challenge is NOT burned here. Someone genuinely locked out by this still has to be able
    # to reach for a recovery code, and views_two_factor checks the recovery code BEFORE it calls
    # this function, on the same live challenge.
    if not method.requires_delivery and _attempts_last_hour(challenge.user) >= MAX_CODELESS_ATTEMPTS_PER_HOUR:
        return False, "locked"

    code = (code or "").strip()
    if code and method.check_code(challenge.user, challenge, code):
        challenge.consume()
        return True, ""

    challenge.attempts += 1
    challenge.save(update_fields=["attempts"])
    if challenge.attempts >= TwoFactorChallenge.MAX_ATTEMPTS:
        challenge.consume()
        return False, "locked"
    return False, "invalid"


def attempts_left(challenge) -> int:
    """How many more guesses this challenge allows. Surfaced to the user so a wrong code says how
    close they are to having to start again, instead of silently dying on the fifth try."""
    if challenge is None:
        return 0
    return max(0, TwoFactorChallenge.MAX_ATTEMPTS - challenge.attempts)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §5  Backup codes
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _generate_backup_code() -> str:
    """One recovery code, formatted "XXXXX-XXXXX" so it is readable when written down."""
    raw = "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
    return f"{raw[:5]}-{raw[5:]}"


def generate_backup_codes(user):
    """Replace this user's backup codes with a fresh set and return the PLAINTEXT list.

    This is the only moment the plaintext exists. The caller returns it to the user exactly once
    (the security page makes them confirm they have saved it); only hashes are stored, so nobody -
    including AFC staff - can show them again. Regenerating invalidates every previous code, which
    is also the answer to "I think someone saw my codes"."""
    with transaction.atomic():
        TwoFactorBackupCode.objects.filter(user=user).delete()
        codes = [_generate_backup_code() for _ in range(BACKUP_CODE_COUNT)]
        TwoFactorBackupCode.objects.bulk_create([
            TwoFactorBackupCode(user=user, code_hash=make_password(c)) for c in codes
        ])
    return codes


def consume_backup_code(user, submitted) -> bool:
    """Spend one unused backup code. True when it matched (and is now spent), False otherwise.

    Normalizes case and the display hyphen so "abcde-fghij", "ABCDEFGHIJ" and "ABCDE-FGHIJ" are the
    same code to a user typing it on a phone. Walks only UNUSED rows, so a code really is one-shot."""
    submitted = (submitted or "").strip().upper().replace(" ", "")
    if not submitted:
        return False
    if "-" not in submitted and len(submitted) == BACKUP_CODE_LENGTH:
        submitted = f"{submitted[:5]}-{submitted[5:]}"

    for row in TwoFactorBackupCode.objects.filter(user=user, used_at__isnull=True):
        if check_password(submitted, row.code_hash):
            row.used_at = timezone.now()
            row.save(update_fields=["used_at"])
            return True
    return False


def backup_codes_remaining(user) -> int:
    """Unused recovery codes left. Shown on the security page so a user notices before the last one
    is gone."""
    try:
        return TwoFactorBackupCode.objects.filter(user=user, used_at__isnull=True).count()
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6  Authenticator app (TOTP, RFC 6238) - added 2026-08-07
#
# WHAT IS DIFFERENT ABOUT THIS METHOD, in one sentence: instead of AFC minting a code and sending
# it, the user's phone and this server hold the same secret and both derive the same six digits
# from the clock. Nothing travels over email or the network, so it survives a compromised mailbox,
# a hijacked SIM and an SMTP outage - which is exactly why admins and organizers want it.
#
# ── THE DEPENDENCY: pyotp ────────────────────────────────────────────────────────────────────────
# pyotp (2.10.0, pure Python, ZERO runtime dependencies) does the RFC 6238 arithmetic and builds the
# otpauth:// URI. It is in requirements.txt, so the server needs `pip install -r requirements.txt`
# on deploy. This was a deliberate choice over hand-writing ~20 lines of hmac: the three places
# TOTP implementations are silently wrong (base32 padding, big-endian counter packing, the dynamic
# truncation offset) all fail as "some codes work and some do not", which is the worst possible
# failure mode for a lockout-adjacent feature.
#
# NO QR LIBRARY IS INSTALLED HERE ON PURPOSE. The backend returns the otpauth:// URI as a string and
# the FRONTEND draws the QR from it (components/TotpQrCode.tsx, react-qr-code). That keeps an image
# encoder off the server entirely, and it means the secret is rendered client-side rather than
# travelling back as a PNG we would then have to think about caching.
#
# ── HOW THE SECRET IS STORED: ENCRYPTED, NOT PLAINTEXT, AND NOT HASHED ──────────────────────────
# A one-time code can be hashed because we only ever need to compare it. A TOTP secret cannot: the
# server has to REPRODUCE codes from it, so it must be recoverable, so hashing is off the table.
# That leaves plaintext or encryption, and plaintext would mean one leaked database dump = every
# second factor on the site, immediately usable and completely silent.
#
# So the base32 secret is sealed with Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`, which
# was ALREADY a dependency - this added nothing) under a key derived from the Django secret key with
# HKDF-SHA256. The threat this actually stops is the realistic one: a stolen backup, a read replica,
# an injection that can SELECT. It does NOT stop an attacker who has the application secret key as
# well, and no symmetric scheme in the app could - that is a limit worth stating plainly rather
# than pretending otherwise.
#
# The KDF info string is versioned ("v1") so a future re-key can decrypt old rows with the old
# derivation while writing new ones. The key is read from settings.TOTP_ENCRYPTION_KEY when it
# exists and falls back to SECRET_KEY, so ops can pin it independently if DJANGO_SECRET_KEY is ever
# rotated - because a rotation without that pin makes every stored secret undecryptable.
# UNDECRYPTABLE FAILS SOFT: decrypt returns "", is_available goes False, is_enabled_for goes False,
# and the user signs in with their password and re-enrols. See is_enabled_for for why that beats a
# permanent lockout only a DBA can undo.
#
# ── DRIFT AND REPLAY ────────────────────────────────────────────────────────────────────────────
# Drift: TOTP_DRIFT_STEPS = 1, so the previous, current and next 30-second step are all accepted -
# a 90-second window, absorbing the phone clock being up to half a minute out either way.
# Replay: a 90-second window would otherwise mean six digits keep working for 90 seconds after
# somebody watched them being typed. consume_totp records the step it accepted in
# TwoFactorSettings.totp_last_step and refuses anything not strictly newer, so each code is spent
# once and every code from an already-spent step dies with it.
#
# ── HOW IT CONNECTS ─────────────────────────────────────────────────────────────────────────────
#   models    : TwoFactorSettings.totp_* (afc_auth/models.py)
#   HTTP      : afc_auth/views_two_factor.py -> totp_setup, totp_confirm; and the SHARED endpoints
#               (login verify, resend, send-code, disable) which needed no TOTP-specific code.
#   login gate: afc_auth.views.login_or_challenge, unchanged - it asks is_enabled_for and hands back
#               a challenge whose `method` happens to be "totp".
#   frontend  : lib/twoFactor.ts setupTotp/confirmTotp -> TotpEnrolDialog.tsx (enrolment) and
#               TwoFactorStep.tsx (which hides resend when method === "totp").
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# Built once per process on first use. HKDF is cheap but not free, and this runs on every login of
# every authenticator user.
_TOTP_BOX = None


def _totp_box() -> Fernet:
    """The Fernet cipher that seals TOTP secrets, derived from the app's secret key.

    HKDF (not the raw key) so the encryption key is a full 32 bytes of uniform material regardless
    of how the deployment's SECRET_KEY looks, and so this key is domain-separated: it is derived for
    exactly this purpose and cannot collide with anything else that ever derives from SECRET_KEY."""
    global _TOTP_BOX
    if _TOTP_BOX is None:
        base = getattr(settings, "TOTP_ENCRYPTION_KEY", None) or settings.SECRET_KEY
        if not base:
            # Only reachable with an unconfigured deployment. Raising here is right: silently
            # falling back to a fixed key would store every secret under a key that is in the
            # source tree.
            raise RuntimeError(
                "TOTP secrets cannot be encrypted: neither TOTP_ENCRYPTION_KEY nor SECRET_KEY is "
                "set. Set DJANGO_SECRET_KEY in the environment.")
        material = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"afc-2fa-totp-secret-v1",
        ).derive(str(base).encode("utf-8"))
        _TOTP_BOX = Fernet(base64.urlsafe_b64encode(material))
    return _TOTP_BOX


def encrypt_totp_secret(secret: str) -> str:
    """Seal a base32 secret for storage. ~140 chars of URL-safe base64."""
    return _totp_box().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_totp_secret(sealed: str) -> str:
    """Open a stored secret, or "" when it cannot be opened.

    Returns "" rather than raising for the key-rotation case described in the section header: the
    caller treats an empty secret as "this method is not available to this user", which degrades to
    a one-step sign-in instead of a 500 on every login attempt."""
    if not sealed:
        return ""
    try:
        return _totp_box().decrypt(sealed.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def active_totp_secret(user) -> str:
    """The CONFIRMED authenticator secret for `user`, or "" if there is not one we can use.

    Confirmed is the load-bearing word: an enrolment that was started (QR shown) but never proved
    lives in totp_pending_secret and must never satisfy a login."""
    row = settings_for(user)
    if not row or not row.totp_secret or not row.totp_confirmed_at:
        return ""
    return decrypt_totp_secret(row.totp_secret)


def pending_totp_secret(user) -> str:
    """The IN-FLIGHT enrolment secret, or "" when there is none or it has gone stale."""
    row = settings_for(user)
    if not row or not row.totp_pending_secret or not row.totp_pending_at:
        return ""
    if timezone.now() - row.totp_pending_at > TOTP_ENROLMENT_LIFETIME:
        return ""
    return decrypt_totp_secret(row.totp_pending_secret)


def start_totp_enrolment(user) -> str:
    """Mint a fresh enrolment secret for `user`, store it as PENDING, and return the plaintext.

    The plaintext is returned exactly once, to be shown as a QR and as typeable text, and is never
    returned again by any endpoint. Calling this twice simply replaces the pending secret; the
    user's ACTIVE authenticator, if they have one, is untouched until totp_confirm promotes it.
    That is the whole reason for the two columns: a half-finished re-enrolment cannot break a
    working phone."""
    secret = pyotp.random_base32(length=TOTP_SECRET_LENGTH)
    row, _created = TwoFactorSettings.objects.get_or_create(user=user)
    row.totp_pending_secret = encrypt_totp_secret(secret)
    row.totp_pending_at = timezone.now()
    row.save(update_fields=["totp_pending_secret", "totp_pending_at", "updated_at"])
    return secret


def totp_provisioning_uri(user, secret: str) -> str:
    """The otpauth:// URI an authenticator app imports, e.g.

        otpauth://totp/AFC:player1?secret=BASE32&issuer=AFC

    The frontend renders this as a QR (and the raw secret underneath it, for anyone who cannot
    scan). Built by pyotp so the label/issuer escaping is the library's problem, not ours."""
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD).provisioning_uri(
        name=getattr(user, "username", "") or getattr(user, "email", "") or "AFC account",
        issuer_name=TOTP_ISSUER,
    )


def current_totp_step(at=None) -> int:
    """Which 30-second step the clock is in. Unix epoch, so Django's TIME_ZONE is irrelevant here:
    the phone and the server agree because they both count from the same absolute instant."""
    return int(at if at is not None else time.time()) // TOTP_PERIOD


def match_totp_step(secret: str, submitted, after_step: int = -1):
    """Which time step `submitted` is the code for, or None. Pure: reads no database, writes none.

    THE TWO RULES live here, so both are impossible to apply half of:
      • DRIFT: steps from -TOTP_DRIFT_STEPS to +TOTP_DRIFT_STEPS around now are acceptable.
      • REPLAY: any step at or below `after_step` is skipped without even being compared. Callers
        pass the last step this secret has already spent, so a code works once and every older code
        still inside the drift window dies with it.

    Comparison is hmac.compare_digest rather than ==, so how long this takes does not depend on how
    many leading digits happened to be right."""
    if not secret:
        return None

    # Authenticator apps display codes as "123 456", and people paste what they see.
    submitted = (submitted or "").strip().replace(" ", "")
    if len(submitted) != TOTP_DIGITS or not submitted.isdigit():
        return None

    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD)
    now_step = current_totp_step()
    for step in range(now_step - TOTP_DRIFT_STEPS, now_step + TOTP_DRIFT_STEPS + 1):
        if step <= after_step:
            continue  # already spent, or older than something already spent: a replay.
        if hmac.compare_digest(totp.generate_otp(step), submitted):
            return step
    return None


def consume_totp(user, submitted) -> bool:
    """Spend a code from this user's CONFIRMED authenticator. True when it was valid and unspent.

    This is the login/disable path. The replay floor is the account's totp_last_step, and a
    successful step is written back before returning, which is what makes the code single-use."""
    row = settings_for(user)
    if row is None:
        return False

    # -1 rather than 0: step 0 is a real (if absurdly historical) step, and a row that has never
    # spent one must not accidentally refuse it.
    spent = row.totp_last_step if row.totp_last_step is not None else -1
    step = match_totp_step(active_totp_secret(user), submitted, after_step=spent)
    if step is None:
        return False

    row.totp_last_step = step
    row.save(update_fields=["totp_last_step", "updated_at"])
    return True


def check_totp_enrolment(user, submitted):
    """Is `submitted` a valid code for the PENDING enrolment? Returns the matched step, or None.

    Deliberately READ ONLY. It changes nothing, which is what lets views_two_factor.totp_confirm
    check the new app's code FIRST and the account proof second: a mistyped app code (easy, the
    digits roll every 30 seconds) then costs nothing, instead of burning the single-use email code
    the user had just gone to their inbox for. Both still have to pass before promote_totp_enrolment
    changes anything, so the order things are CHECKED in does not change what is REQUIRED.

    WHY THE REPLAY FLOOR IS NOT APPLIED TO THE PENDING SECRET: totp_last_step records steps spent by
    the ACTIVE secret, and replay protection is a property of a secret, not of a clock. A user
    swapping one authenticator for another proves the OLD app (spending step N) and the NEW app in
    the same submission; if the pending check inherited that floor, the new app's code - generated
    in the very same step N - would be refused, and the user would see "wrong code" for a code that
    is perfectly correct. The pending secret has never been used, so its floor is "none", and the
    step that proves it becomes the account's floor at promotion."""
    secret = pending_totp_secret(user)
    if not secret:
        return None
    return match_totp_step(secret, submitted)


def promote_totp_enrolment(user, step: int):
    """Make the pending secret the ACTIVE one. Call ONLY after check_totp_enrolment returned `step`
    and every other requirement has passed.

    Splitting this from the check is what makes enrolment safe: nothing about the account changes
    until a code generated by the app the user just scanned has checked out AND they have proved the
    account is theirs. Someone who scans the QR into an app on a phone with a broken clock finds
    that out at the check, while they still have their old way in, rather than at 3am locked out."""
    now = timezone.now()
    # A queryset .update() rather than saving a model instance: the caller may have just spent a
    # step through consume_totp on a different copy of this row, and re-saving a stale instance
    # would silently undo that write. F() moves the sealed ciphertext across without ever
    # decrypting and re-encrypting it.
    TwoFactorSettings.objects.filter(user=user).update(
        totp_secret=F("totp_pending_secret"),
        totp_pending_secret="",
        totp_pending_at=None,
        totp_confirmed_at=now,
        # The step that proved the enrolment is immediately spent, so the code the user just typed
        # into the setup screen cannot be turned around and used to sign in thirty seconds later.
        totp_last_step=step,
        updated_at=now,   # auto_now does not fire on .update(), so it is set by hand
    )


def clear_totp_secret(user):
    """Forget everything about this user's authenticator app.

    Called when 2FA is switched off, for the same reason the recovery codes are deleted there: an
    entry sitting in somebody's authenticator from six months ago must not still open an account
    that was later re-enabled. Re-enabling means scanning a new QR, always."""
    now = timezone.now()
    TwoFactorSettings.objects.filter(user=user).update(
        totp_secret="",
        totp_pending_secret="",
        totp_pending_at=None,
        totp_confirmed_at=None,
        totp_last_step=None,
        updated_at=now,
    )
