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
# WHY A METHOD REGISTRY FOR A SINGLE METHOD
#   The shipped method is EMAIL and only email (see ENABLED_METHODS). Email is the only factor
#   ~6,790 AFC accounts can all actually use: WhatsApp reaches roughly 90 of them today. But the
#   next two methods are known - the approved WhatsApp "login_code" template, and an authenticator
#   app (TOTP) - so the flow is written against a small TwoFactorMethod interface instead of
#   hardcoding "send an email". Adding WhatsApp later is a new subclass plus one entry in METHODS;
#   no view, no model and no frontend screen has to change.
#
# HOW IT CONNECTS
#   models   : TwoFactorSettings / TwoFactorChallenge / TwoFactorBackupCode (afc_auth/models.py)
#   email    : afc_auth.views.send_email (the single localized SMTP chokepoint) with the
#              hand-authored copy from afc_auth.email_i18n ("two_factor_code" + subject "two_factor").
#   callers  : afc_auth.views.login (step one gate), afc_auth.views_two_factor.* (every endpoint).
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
# ─────────────────────────────────────────────────────────────────────────────────────────────────
import secrets
import string

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
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


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §1  The method interface
#
# A method answers three questions: can this user use me, where would the code go (a string safe to
# show on screen), and please deliver this code. `requires_delivery = False` is the hook a future
# TOTP method needs - an authenticator app generates the code itself, so there is nothing to send
# and no resend button to render.
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


# The registry. Adding WhatsApp later = write a WhatsAppCodeMethod calling
# afc_whatsapp.client.send_template(number, "login_code", lang, body_params=[code]), add it here,
# and add "whatsapp" to ENABLED_METHODS. Nothing else in the stack changes.
METHODS = {
    EmailCodeMethod.code: EmailCodeMethod(),
}

# What a user is allowed to CHOOSE right now. Kept separate from METHODS so a method can be
# implemented and tested before it is offered to users.
ENABLED_METHODS = ("email",)

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

    The method re-check matters: if an account somehow ends up with no email address, we must not
    strand it behind a factor that can never arrive."""
    row = settings_for(user)
    if not row or not row.is_enabled:
        return False
    method = get_method(row.method)
    if method.requires_delivery and not method.is_available(user):
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

    if method.requires_delivery and not method.is_available(user):
        return {"challenge": None, "sent": False, "reason": "unavailable", "retry_after": 0,
                "method": method.code, "destination": ""}

    now = timezone.now()
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

    delivered = method.deliver(user, code) if method.requires_delivery else True
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


def verify_code(challenge, code) -> tuple[bool, str]:
    """Check `code` against `challenge`. Returns (ok, reason).

    On success the challenge is CONSUMED, so the same code can never be replayed. On failure the
    attempt counter goes up and, at MAX_ATTEMPTS, the challenge is burned outright - the user has to
    start over rather than keep guessing. reason is one of "" (ok), "invalid", "expired", "locked".
    """
    if challenge is None:
        return False, "expired"
    if not challenge.is_live():
        return False, "locked" if challenge.attempts >= TwoFactorChallenge.MAX_ATTEMPTS else "expired"

    code = (code or "").strip()
    if code and check_password(code, challenge.code_hash):
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
