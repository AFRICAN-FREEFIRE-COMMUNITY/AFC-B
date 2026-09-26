"""afc_referrals.engine - every referral decision, in one place (R24).

    eligible_referrer(program, user)   may this user refer in this program (its scope)
    code_for(program, user)            the user's code in a program, made on first ask
    claim(user, code, click_token, ip_hash)
                                       a new account says "I came through this code"
    on_action(user, rule, event=None)  something the new person did; counts the referral if it is the
                                       program's rule (called by signals.py from the real write paths)
    count(referral)                    pending/flagged -> counted, then award what that unlocks
    leaderboard(program)               counted referrals per referrer
    award_ranks(program)               after the end: the top-N prizes

Nothing here trusts a user id from a request body: `claim` is given the signed-in user, and the referrer
is the owner of the code (R64). Prize values come from the ProgramPrize row and the shop (R73).
Callers: afc_referrals.views, afc_referrals.signals, the award_referral_ranks command.
"""
import logging
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Max
from django.utils import timezone

from .models import ProgramPrize, Referral, ReferralClick, ReferralCode, ReferralProgram, Reward

logger = logging.getLogger(__name__)

# No 0/O, 1/I/L: a code is read aloud and typed from a screenshot
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 7
NEW_ACCOUNT_WINDOW = timedelta(days=7)   # a typed code (no click) only credits accounts this young
BURST_WINDOW = timedelta(hours=24)
BURST_LIMIT = 5                          # claims on one code from one address before they are held


class ClaimRefused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ── programs and codes ────────────────────────────────────────────────────────────────────────────

def is_running(program, at=None):
    at = at or timezone.now()
    return program.is_published and program.starts_at <= at <= program.ends_at


def user_country(user):
    return (getattr(user, "ip_country", "") or getattr(user, "country", "") or "").strip()


def eligible_referrer(program, user):
    if user is None or getattr(user, "status", "active") == "deleted":
        return False
    scope = program.scope
    if scope == ReferralProgram.SCOPE_EVERYONE:
        return True
    if scope == ReferralProgram.SCOPE_COUNTRIES:
        country = user_country(user).lower()
        return bool(country) and country in {c.strip().lower() for c in program.countries or []}
    if scope == ReferralProgram.SCOPE_TEAMS:
        from afc_team.models import TeamMembers
        return TeamMembers.objects.filter(member=user, team__in=program.teams.all()).exists()
    if scope == ReferralProgram.SCOPE_USERS:
        return program.users.filter(pk=user.pk).exists()
    return False


def new_code():
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not ReferralCode.objects.filter(code=code).exists():
            return code
    raise RuntimeError("could not mint a referral code")  # pragma: no cover - 31^7 space


def code_for(program, user):
    existing = ReferralCode.objects.filter(program=program, user=user).first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            return ReferralCode.objects.create(program=program, user=user, code=new_code())
    except IntegrityError:
        return ReferralCode.objects.get(program=program, user=user)


def sync_program_code(program):
    """The program's shared code (e.g. BOUNTY26) lives as a ReferralCode with no user, so a click or a
    claim looks it up exactly like a personal code. Called after an admin saves the program."""
    wanted = (program.program_code or "").strip().upper()
    shared = ReferralCode.objects.filter(program=program, user__isnull=True).first()
    if not wanted:
        if shared and not shared.referrals.exists():
            shared.delete()
        return None
    if shared:
        if shared.code != wanted:
            shared.code = wanted
            shared.save(update_fields=["code"])
        return shared
    return ReferralCode.objects.create(program=program, user=None, code=wanted)


def find_code(raw):
    code = (raw or "").strip().upper()
    if not code or len(code) > 20:
        return None
    return ReferralCode.objects.select_related("program", "user").filter(code=code).first()


# ── claiming ──────────────────────────────────────────────────────────────────────────────────────

def _whatsapp(user):
    from afc_auth.models import canonical_profile
    profile = canonical_profile(user)
    return (getattr(profile, "whatsapp_number", "") or "").strip() if profile else ""


def claim(user, raw_code, click_token="", ip_hash=""):
    """Record that `user` (signed in, new) arrived through `raw_code`. Returns the Referral, or raises
    ClaimRefused with a code the frontend translates."""
    code = find_code(raw_code)
    if code is None:
        raise ClaimRefused("bad_code", "That referral code does not exist.")
    program = code.program
    now = timezone.now()
    if not is_running(program, now):
        raise ClaimRefused("program_not_active", "That referral program is not running.")
    if code.user_id and code.user_id == user.pk:
        raise ClaimRefused("self_referral", "You cannot use your own referral code.")
    if code.user_id and not eligible_referrer(program, code.user):
        raise ClaimRefused("not_eligible", "That referral code is no longer valid.")
    if Referral.objects.filter(referred=user).exists():
        raise ClaimRefused("already_referred", "Your account already has a referral.")

    click = None
    if click_token:
        click = ReferralClick.objects.filter(public_token=click_token, code=code).first()
    # The account must be NEW: created after the click, or (a typed code) within the last week. An
    # account that existed before anybody referred it is nobody's referral.
    joined = user.date_joined
    if (click and joined < click.created_at) or (not click and joined < now - NEW_ACCOUNT_WINDOW):
        raise ClaimRefused("account_not_new", "Referral codes are for new accounts only.")

    status, reason = Referral.PENDING, ""
    recent = Referral.objects.filter(code=code, created_at__gte=now - BURST_WINDOW)
    if ip_hash and recent.filter(ip_hash=ip_hash).count() >= BURST_LIMIT:
        # Held for an admin, never silently thrown away (spec: bursts are flagged, not rejected)
        status, reason = Referral.FLAGGED, "burst_same_network"
    if code.user_id:
        mine, theirs = _whatsapp(user), _whatsapp(code.user)
        if mine and mine == theirs:
            # The one-number-one-account rule: a second account on the referrer's number never counts
            status, reason = Referral.REJECTED, "shared_phone"

    try:
        with transaction.atomic():
            referral = Referral.objects.create(
                program=program, code=code, referrer=code.user, referred=user, click=click,
                status=status, reason=reason, ip_hash=ip_hash,
            )
    except IntegrityError:
        raise ClaimRefused("already_referred", "Your account already has a referral.")

    if status == Referral.PENDING and program.count_rule == ReferralProgram.RULE_SIGNUP and user.is_active:
        referral = count(referral)
    return referral


def hash_ip(ip):
    """Salted hash of an address, the same shape the referral views store (never the raw address)."""
    import hashlib
    from django.conf import settings
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip or ''}".encode()).hexdigest()[:32]


def claim_at_signup(user, raw_code, click_token="", ip=""):
    """The email sign-up's referral (inbox #53, owner 2026-09-26). Called by afc_auth.views.signup right
    after the account is created, so the referral lives on the SERVER from that moment: confirming the
    email on another phone or computer still counts it (the signals count a RULE_SIGNUP referral when
    the account turns active). Before this, the code only waited in the first browser's cookie and was
    lost when the person confirmed and signed in somewhere else.

    Never raises and never blocks the sign-up: returns {"status": ...} or {"refused": code}, or None when
    no code was sent. Google and Discord sign-ups still claim through the browser (they are signed in on
    the same device at once)."""
    if not (raw_code or "").strip():
        return None
    try:
        referral = claim(user, raw_code, click_token=click_token or "", ip_hash=hash_ip(ip))
        return {"status": referral.status}
    except ClaimRefused as refused:
        return {"refused": refused.code}
    except Exception:  # noqa: BLE001 - a referral must never fail an account creation
        logger.exception("referrals: claim at signup failed for user %s", getattr(user, "pk", None))
        return {"refused": "error"}


# ── counting and awards ───────────────────────────────────────────────────────────────────────────

def on_action(user, rule, event=None, at=None):
    """The new person did something. Counts their pending referral when it is the program's rule and the
    program is running. Never raises: it runs inside other features' saves (signals.py)."""
    try:
        referral = (Referral.objects.select_related("program")
                    .filter(referred_id=getattr(user, "pk", user), status=Referral.PENDING).first())
        if referral is None:
            return None
        program = referral.program
        if program.count_rule != rule or not is_running(program, at):
            return None
        if rule == ReferralProgram.RULE_EVENT and program.count_event_id and \
                getattr(event, "pk", None) != program.count_event_id:
            return None
        return count(referral)
    except Exception:  # noqa: BLE001 - a referral must never break a team join or an order
        logger.exception("referrals: on_action failed for user %s rule %s", getattr(user, "pk", user), rule)
        return None


def count(referral):
    with transaction.atomic():
        referral = Referral.objects.select_for_update().get(pk=referral.pk)
        if referral.status not in (Referral.PENDING, Referral.FLAGGED):
            return referral
        referral.status = Referral.COUNTED
        referral.counted_at = timezone.now()
        referral.save(update_fields=["status", "counted_at"])
    _award_after_count(referral)
    return referral


def _award_after_count(referral):
    program = referral.program
    if referral.referrer_id:
        done = Referral.objects.filter(program=program, referrer_id=referral.referrer_id,
                                       status=Referral.COUNTED).count()
        for prize in program.prizes.filter(kind=ProgramPrize.KIND_MILESTONE, threshold__lte=done):
            give(prize, referral.referrer)
    for prize in program.prizes.filter(kind=ProgramPrize.KIND_WELCOME):
        give(prize, referral.referred, referral=referral)


def _coupon_for(prize, user):
    from afc_shop.models import Coupon
    code = f"REF-{new_code()}"
    return Coupon.objects.create(
        code=code, slug=code.lower(), discount_type=prize.coupon_discount_type or "percent",
        discount_value=prize.coupon_discount_value or 0, max_uses=1, is_active=True,
        description=f"Referral prize for {user.username} ({prize.program.name})",
    )


def give(prize, user, referral=None):
    """One reward per prize per person (per referral for a welcome prize); a repeat call is a no-op."""
    key = f"{prize.pk}:r{referral.pk}" if referral else f"{prize.pk}:u{user.pk}"
    if Reward.objects.filter(award_key=key).exists():
        return None
    try:
        with transaction.atomic():
            reward = Reward.objects.create(program=prize.program, prize=prize, user=user, referral=referral,
                                           award_key=key)
            if prize.prize_type == ProgramPrize.TYPE_COUPON:
                # A coupon is delivered the moment it exists: the code is the prize
                reward.coupon = _coupon_for(prize, user)
                reward.status = Reward.DELIVERED
                reward.delivered_at = timezone.now()
                reward.save(update_fields=["coupon", "status", "delivered_at"])
    except IntegrityError:
        return None
    from .notify import notify_reward
    notify_reward(reward)
    return reward


def leaderboard(program, limit=None):
    rows = (Referral.objects.filter(program=program, status=Referral.COUNTED, referrer__isnull=False)
            .values("referrer_id", "referrer__username")
            .annotate(counted=Count("pk"), last=Max("counted_at"))
            # Ties: whoever got there first ranks higher
            .order_by("-counted", "last", "referrer_id"))
    return list(rows[:limit] if limit else rows)


def award_ranks(program, now=None):
    now = now or timezone.now()
    if now < program.ends_at:
        raise ClaimRefused("program_not_ended", "Leaderboard prizes are awarded after the program ends.")
    from afc_auth.models import User
    board = leaderboard(program)
    given = []
    for prize in program.prizes.filter(kind=ProgramPrize.KIND_RANK):
        if prize.rank and prize.rank <= len(board):
            user = User.objects.get(pk=board[prize.rank - 1]["referrer_id"])
            reward = give(prize, user)
            if reward:
                given.append(reward)
    if program.ranks_awarded_at is None:
        program.ranks_awarded_at = now
        program.save(update_fields=["ranks_awarded_at"])
    return given
