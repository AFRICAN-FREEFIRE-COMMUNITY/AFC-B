"""afc_referrals.views - referral program endpoints (inbox #47), mounted at referrals/ in afc/urls.py.

Every answer is the AFC envelope; every refusal carries a `code` the frontend translates (R35). The
decisions themselves live in engine.py; this file parses, gates and shapes.

PUBLIC (no account)
  GET  referrals/r/<code>/        what /r/<code> shows: the program, who invited you, the welcome prize.
                                  404 bad_code. A program that is not running answers 200 with
                                  running=false, so the page can say so.
                                  Caller: frontend app/r/[code]/page.tsx.
  POST referrals/click/ {code}    records a visit; 200 {click_token}. 30 per hour per address (R59).
                                  Caller: the same page, once, then kept in the afc_ref cookie.

SIGNED IN (Bearer)
  POST referrals/claim/ {code, click_token?}
                                  "this new account came through that code", sent once after the first
                                  sign-in by lib/referrals.ts claimPendingReferral(). 200 {status,
                                  program}. Refusals: bad_code, program_not_active, not_eligible,
                                  self_referral, account_not_new, already_referred (400), rate_limited.
  GET  referrals/mine/            the profile Referrals card: each running program the user may refer in,
                                  with their code, link and numbers; their rewards; who referred them.
                                  Caller: frontend components/referrals/ReferralsCard.tsx.

HEAD ADMIN (Bearer, super_admin / head_admin; afc_auth.views_account_deletion._require_head_admin)
  GET|POST  referrals/admin/programs/               list (paginated) / create
  GET|PATCH referrals/admin/programs/<slug>/        detail with funnel and leaderboard / edit
  GET       referrals/admin/programs/<slug>/referrals/?status=&limit=&offset=
  GET       referrals/admin/programs/<slug>/rewards/?status=&limit=&offset=
  GET       referrals/admin/programs/<slug>/export/  CSV of every referral
  POST      referrals/admin/programs/<slug>/award-ranks/
  POST      referrals/admin/referrals/<token>/decide/ {action: count|reject|release}
  POST      referrals/admin/rewards/<token>/deliver/ {note?}
  Caller: frontend app/(a)/a/referrals/.
"""
import csv
import hashlib
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views_account_deletion import _require_head_admin, _require_user

from . import engine
from .models import ProgramPrize, Referral, ReferralClick, ReferralCode, ReferralProgram, Reward
from .notify import prize_label

CLICKS_PER_HOUR = 30
CLAIMS_PER_HOUR = 10
MAX_PAGE = 100


def _refuse(message, code, http=status.HTTP_400_BAD_REQUEST):
    return Response({"message": message, "code": code}, status=http)


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.META.get("REMOTE_ADDR") or "")


def _ip_hash(request):
    # Salted, so no raw address is stored (only compared)
    return hashlib.sha256(f"{settings.SECRET_KEY}:{_client_ip(request)}".encode()).hexdigest()[:32]


def _under_limit(bucket, identity, per_hour):
    key = f"ref_{bucket}:{identity}:{timezone.now().strftime('%Y%m%d%H')}"
    cache.add(key, 0, 3600)
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3600)
        n = 1
    return n <= per_hour


def _page(request):
    try:
        limit = max(1, min(MAX_PAGE, int(request.query_params.get("limit", 25))))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        limit, offset = 25, 0
    return limit, offset


def _iso(value):
    return value.isoformat() if value else None


# ── shapes (one per kind of thing, R24) ───────────────────────────────────────────────────────────

def prize_dict(prize):
    variant = prize.product_variant
    return {
        "prize_id": prize.pk,
        "kind": prize.kind,
        "threshold": prize.threshold,
        "rank": prize.rank,
        "prize_type": prize.prize_type,
        "product_variant": variant.sku if variant else None,
        "product_variant_title": (variant.title or variant.product.name) if variant else None,
        "diamonds_amount": variant.diamonds_amount if variant else None,
        "coupon_discount_type": prize.coupon_discount_type or None,
        "coupon_discount_value": str(prize.coupon_discount_value) if prize.coupon_discount_value is not None else None,
        "cash_amount": str(prize.cash_amount) if prize.cash_amount is not None else None,
        "custom_text": prize.custom_text or None,
        "label": prize_label(prize),
    }


def program_public_dict(program):
    return {
        "slug": program.slug,
        "name": program.name,
        "description": program.description,
        "starts_at": _iso(program.starts_at),
        "ends_at": _iso(program.ends_at),
        "count_rule": program.count_rule,
        "count_event": ({"slug": program.count_event.slug, "name": program.count_event.event_name}
                        if program.count_event_id else None),
        "running": engine.is_running(program),
    }


def program_admin_dict(program, detail=False):
    counts = program.referrals.aggregate(
        total=Count("pk"),
        counted=Count("pk", filter=Q(status=Referral.COUNTED)),
        pending=Count("pk", filter=Q(status=Referral.PENDING)),
        flagged=Count("pk", filter=Q(status=Referral.FLAGGED)),
        rejected=Count("pk", filter=Q(status=Referral.REJECTED)),
    )
    now = timezone.now()
    state = ("draft" if not program.is_published else "scheduled" if now < program.starts_at
             else "ended" if now > program.ends_at else "running")
    data = {
        **program_public_dict(program),
        "is_published": program.is_published,
        "state": state,
        "scope": program.scope,
        "countries": program.countries or [],
        "teams": list(program.teams.values_list("team_name", flat=True)),
        "users": list(program.users.values_list("username", flat=True)),
        "program_code": program.program_code or None,
        "ranks_awarded_at": _iso(program.ranks_awarded_at),
        "funnel": {
            "clicks": ReferralClick.objects.filter(code__program=program).count(),
            "signups": counts["total"],
            "counted": counts["counted"],
            "pending": counts["pending"],
            "flagged": counts["flagged"],
            "rejected": counts["rejected"],
        },
        "rewards_pending": program.rewards.filter(status=Reward.PENDING).count(),
        "created_at": _iso(program.created_at),
    }
    if detail:
        data["prizes"] = [prize_dict(p) for p in program.prizes.select_related("product_variant__product")]
        data["leaderboard"] = [
            {"rank": i + 1, "username": row["referrer__username"], "counted": row["counted"],
             "last_counted_at": _iso(row["last"])}
            for i, row in enumerate(engine.leaderboard(program, limit=20))
        ]
    return data


def referral_dict(referral):
    return {
        "token": referral.public_token,
        "referrer": referral.referrer.username if referral.referrer_id else None,
        "referred": referral.referred.username,
        "code": referral.code.code,
        "status": referral.status,
        "reason": referral.reason or None,
        "created_at": _iso(referral.created_at),
        "counted_at": _iso(referral.counted_at),
    }


def reward_dict(reward, for_owner=False):
    data = {
        "token": reward.public_token,
        "program": reward.program.name,
        "kind": reward.prize.kind,
        "prize": prize_dict(reward.prize),
        "status": reward.status,
        "created_at": _iso(reward.created_at),
        "delivered_at": _iso(reward.delivered_at),
    }
    if for_owner:
        # The coupon code IS the prize: only its owner (and admins) see it
        data["coupon_code"] = reward.coupon.code if reward.coupon_id else None
    else:
        data["username"] = reward.user.username
        data["note"] = reward.note or None
        data["coupon_code"] = reward.coupon.code if reward.coupon_id else None
    return data


# ── public ────────────────────────────────────────────────────────────────────────────────────────

@api_view(["GET"])
def landing(request, code):
    found = engine.find_code(code)
    if found is None:
        return _refuse("That referral link does not exist.", "bad_code", status.HTTP_404_NOT_FOUND)
    program = found.program
    if not program.is_published:
        return _refuse("That referral link does not exist.", "bad_code", status.HTTP_404_NOT_FOUND)
    return Response({
        "code": found.code,
        "referrer": found.user.username if found.user_id else None,
        "program": program_public_dict(program),
        "welcome_prizes": [prize_dict(p) for p in program.prizes.filter(kind=ProgramPrize.KIND_WELCOME)],
    })


@api_view(["POST"])
def click(request):
    if not _under_limit("click", _ip_hash(request), CLICKS_PER_HOUR):
        return _refuse("Too many requests. Try again later.", "rate_limited", status.HTTP_429_TOO_MANY_REQUESTS)
    found = engine.find_code(str(request.data.get("code") or ""))
    if found is None or not found.program.is_published:
        return _refuse("That referral link does not exist.", "bad_code", status.HTTP_404_NOT_FOUND)
    if not engine.is_running(found.program):
        return _refuse("That referral program is not running.", "program_not_active")
    visit = ReferralClick.objects.create(code=found, ip_hash=_ip_hash(request))
    return Response({"click_token": visit.public_token, "code": found.code})


# ── signed in ─────────────────────────────────────────────────────────────────────────────────────

@api_view(["POST"])
def claim(request):
    user, err = _require_user(request)
    if err:
        return err
    if not _under_limit("claim", f"u{user.pk}", CLAIMS_PER_HOUR):
        return _refuse("Too many requests. Try again later.", "rate_limited", status.HTTP_429_TOO_MANY_REQUESTS)
    code = str(request.data.get("code") or "")[:20]
    token = str(request.data.get("click_token") or "")[:16]
    try:
        referral = engine.claim(user, code, click_token=token, ip_hash=_ip_hash(request))
    except engine.ClaimRefused as refused:
        return _refuse(refused.message, refused.code)
    return Response({"status": referral.status, "program": referral.program.name})


@api_view(["GET"])
def mine(request):
    user, err = _require_user(request)
    if err:
        return err
    now = timezone.now()
    programs = []
    running = (ReferralProgram.objects.filter(is_published=True, starts_at__lte=now, ends_at__gte=now)
               .prefetch_related("prizes"))
    for program in running:
        if not engine.eligible_referrer(program, user):
            continue
        code = engine.code_for(program, user)
        mine_q = Referral.objects.filter(program=program, referrer=user)
        counted = mine_q.filter(status=Referral.COUNTED).count()
        milestones = sorted(p.threshold for p in program.prizes.all()
                            if p.kind == ProgramPrize.KIND_MILESTONE and p.threshold)
        upcoming = [t for t in milestones if t > counted]
        board = engine.leaderboard(program)
        position = next((i + 1 for i, row in enumerate(board) if row["referrer_id"] == user.pk), None)
        programs.append({
            **program_public_dict(program),
            "code": code.code,
            "link_path": f"/r/{code.code}",
            "clicks": code.clicks.count(),
            "signups": mine_q.count(),
            "counted": counted,
            "pending": mine_q.filter(status__in=[Referral.PENDING, Referral.FLAGGED]).count(),
            "next_milestone": ({"threshold": upcoming[0], "remaining": upcoming[0] - counted} if upcoming else None),
            "rank": position,
            "prizes": [prize_dict(p) for p in program.prizes.all()],
        })
    rewards = (Reward.objects.filter(user=user).exclude(status=Reward.CANCELLED)
               .select_related("program", "prize__product_variant__product", "coupon").order_by("-created_at")[:50])
    try:
        received = user.referral_received
    except Referral.DoesNotExist:
        received = None
    return Response({
        "programs": programs,
        "rewards": [reward_dict(r, for_owner=True) for r in rewards],
        "referred_by": ({"program": received.program.name,
                         "referrer": received.referrer.username if received.referrer_id else None,
                         "status": received.status,
                         "count_rule": received.program.count_rule} if received else None),
    })


# ── admin: parsing a program (named fields only, R65 / R69) ───────────────────────────────────────

class Invalid(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _date(value, code):
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        raise Invalid(code, "A date and time is required.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _money(value, code, required=True):
    if value in (None, ""):
        if required:
            raise Invalid(code, "An amount is required.")
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise Invalid(code, "The amount is not a number.")
    if amount <= 0 or amount > Decimal("100000"):
        raise Invalid(code, "The amount is out of range.")
    return amount


def _parse_program(data, existing=None):
    """Returns (fields, relations, prizes or None). Only the keys named here are ever read."""
    from afc_auth.models import User
    from afc_shop.models import ProductVariant
    from afc_team.models import Team
    from afc_tournament_and_scrims.models import Event

    partial = existing is not None
    fields, relations = {}, {}

    def present(key):
        return key in data or not partial

    if present("name"):
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 120:
            raise Invalid("name_required", "Give the program a name (up to 120 characters).")
        fields["name"] = name
    if present("description"):
        fields["description"] = str(data.get("description") or "").strip()[:4000]
    if present("starts_at"):
        fields["starts_at"] = _date(data.get("starts_at"), "starts_at_invalid")
    if present("ends_at"):
        fields["ends_at"] = _date(data.get("ends_at"), "ends_at_invalid")
    starts = fields.get("starts_at", getattr(existing, "starts_at", None))
    ends = fields.get("ends_at", getattr(existing, "ends_at", None))
    if starts and ends and ends <= starts:
        raise Invalid("ends_before_start", "The end must be after the start.")
    if "is_published" in data:
        fields["is_published"] = data.get("is_published") in (True, "true", "1", 1)

    if present("scope"):
        scope = str(data.get("scope") or ReferralProgram.SCOPE_EVERYONE)
        if scope not in ReferralProgram.SCOPES:
            raise Invalid("scope_invalid", "Unknown scope.")
        fields["scope"] = scope
    scope = fields.get("scope", getattr(existing, "scope", ReferralProgram.SCOPE_EVERYONE))
    if "countries" in data or (not partial):
        raw = data.get("countries") or []
        if not isinstance(raw, list):
            raise Invalid("countries_invalid", "Countries must be a list.")
        fields["countries"] = sorted({str(c).strip() for c in raw if str(c).strip()})[:80]
    if "teams" in data or (not partial):
        names = data.get("teams") or []
        if not isinstance(names, list):
            raise Invalid("teams_invalid", "Teams must be a list.")
        teams = list(Team.objects.filter(team_name__in=[str(n) for n in names]))
        if len(teams) != len(set(map(str, names))):
            raise Invalid("team_not_found", "One of the teams was not found.")
        relations["teams"] = teams
    if "users" in data or (not partial):
        names = data.get("users") or []
        if not isinstance(names, list):
            raise Invalid("users_invalid", "Users must be a list.")
        users = list(User.objects.filter(username__in=[str(n) for n in names]))
        if len(users) != len(set(map(str, names))):
            raise Invalid("user_not_found", "One of the players was not found.")
        relations["users"] = users
    countries = fields.get("countries", getattr(existing, "countries", []))
    if scope == ReferralProgram.SCOPE_COUNTRIES and not countries:
        raise Invalid("countries_required", "Pick at least one country or region.")
    if scope == ReferralProgram.SCOPE_TEAMS and not relations.get("teams", list(existing.teams.all()) if existing else []):
        raise Invalid("teams_required", "Pick at least one team.")
    if scope == ReferralProgram.SCOPE_USERS and not relations.get("users", list(existing.users.all()) if existing else []):
        raise Invalid("users_required", "Pick at least one player.")

    if present("count_rule"):
        rule = str(data.get("count_rule") or ReferralProgram.RULE_SIGNUP)
        if rule not in ReferralProgram.RULES:
            raise Invalid("count_rule_invalid", "Unknown counting rule.")
        fields["count_rule"] = rule
    if "count_event" in data or not partial:
        slug = str(data.get("count_event") or "").strip()
        event = Event.objects.filter(slug=slug).first() if slug else None
        if slug and event is None:
            raise Invalid("event_not_found", "That event was not found.")
        fields["count_event"] = event
    if "program_code" in data or not partial:
        code = str(data.get("program_code") or "").strip().upper()
        if code and (len(code) < 4 or len(code) > 20 or not code.isalnum()):
            raise Invalid("program_code_invalid", "A program code is 4 to 20 letters and numbers.")
        clash = ReferralCode.objects.filter(code=code)
        if existing:
            clash = clash.exclude(program=existing, user__isnull=True)
        if code and clash.exists():
            raise Invalid("program_code_taken", "That code is already used.")
        fields["program_code"] = code

    prizes = None
    if "prizes" in data or not partial:
        raw = data.get("prizes") or []
        if not isinstance(raw, list) or len(raw) > 30:
            raise Invalid("prizes_invalid", "Prizes must be a list of at most 30.")
        prizes = []
        for item in raw:
            if not isinstance(item, dict):
                raise Invalid("prizes_invalid", "Each prize must be an object.")
            kind = str(item.get("kind") or "")
            ptype = str(item.get("prize_type") or "")
            if kind not in ProgramPrize.KINDS or ptype not in ProgramPrize.TYPES:
                raise Invalid("prize_invalid", "Unknown prize kind or type.")
            prize = {"prize_id": item.get("prize_id"), "kind": kind, "prize_type": ptype, "threshold": None,
                     "rank": None, "product_variant": None, "coupon_discount_type": "",
                     "coupon_discount_value": None, "cash_amount": None, "custom_text": ""}
            try:
                if kind == ProgramPrize.KIND_MILESTONE:
                    prize["threshold"] = int(item.get("threshold"))
                    if not 1 <= prize["threshold"] <= 100000:
                        raise ValueError
                if kind == ProgramPrize.KIND_RANK:
                    prize["rank"] = int(item.get("rank"))
                    if not 1 <= prize["rank"] <= 1000:
                        raise ValueError
            except (TypeError, ValueError):
                raise Invalid("prize_number_invalid", "A milestone needs a count and a rank prize needs a position.")
            if ptype in (ProgramPrize.TYPE_SHOP_ITEM, ProgramPrize.TYPE_DIAMONDS):
                variant = ProductVariant.objects.filter(sku=str(item.get("product_variant") or "")).first()
                if variant is None:
                    raise Invalid("prize_variant_not_found", "Pick the shop item for this prize.")
                prize["product_variant"] = variant
            elif ptype == ProgramPrize.TYPE_COUPON:
                dtype = str(item.get("coupon_discount_type") or "percent")
                if dtype not in ("percent", "fixed"):
                    raise Invalid("prize_coupon_invalid", "A coupon is a percent or a fixed amount.")
                value = _money(item.get("coupon_discount_value"), "prize_coupon_invalid")
                if dtype == "percent" and value > 100:
                    raise Invalid("prize_coupon_invalid", "A percent coupon is at most 100.")
                prize["coupon_discount_type"], prize["coupon_discount_value"] = dtype, value
            elif ptype == ProgramPrize.TYPE_CASH:
                prize["cash_amount"] = _money(item.get("cash_amount"), "prize_cash_invalid")
            else:
                text = str(item.get("custom_text") or "").strip()
                if not text or len(text) > 200:
                    raise Invalid("prize_custom_invalid", "Describe the custom prize (up to 200 characters).")
                prize["custom_text"] = text
            prizes.append(prize)
        ranks = [p["rank"] for p in prizes if p["kind"] == ProgramPrize.KIND_RANK]
        if len(ranks) != len(set(ranks)):
            raise Invalid("prize_rank_duplicate", "Two prizes for the same position.")
    return fields, relations, prizes


def _save_prizes(program, prizes):
    """Replace the prize list. A prize somebody has already been awarded cannot be removed or changed
    (its rewards point at it): it must come back with its prize_id, unchanged in kind."""
    keep, new = {}, []
    for p in prizes:
        pid = p.pop("prize_id")
        if pid:
            try:
                keep[int(pid)] = p
            except (TypeError, ValueError):
                raise Invalid("prize_not_found", "A prize in the list does not belong to this program.")
        else:
            new.append(p)
    for prize in program.prizes.all():
        if prize.pk not in keep and prize.rewards.exists():
            raise Invalid("prize_has_rewards", "A prize that has been awarded cannot be removed.")
    program.prizes.exclude(pk__in=keep.keys()).delete()
    for pid, p in keep.items():
        prize = program.prizes.filter(pk=pid).first()
        if prize is None:
            raise Invalid("prize_not_found", "A prize in the list does not belong to this program.")
        if prize.rewards.exists() and (prize.kind != p["kind"] or prize.prize_type != p["prize_type"]):
            raise Invalid("prize_has_rewards", "A prize that has been awarded cannot change kind.")
        for key, value in p.items():
            setattr(prize, key, value)
        prize.save()
    for p in new:
        ProgramPrize.objects.create(program=program, **p)


def _unique_slug(name):
    base = slugify(name)[:70] or "program"
    slug, n = base, 2
    while ReferralProgram.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


@api_view(["GET", "POST"])
def admin_programs(request):
    admin, err = _require_head_admin(request)
    if err:
        return err
    if request.method == "GET":
        limit, offset = _page(request)
        qs = ReferralProgram.objects.all()
        total = qs.count()
        rows = [program_admin_dict(p) for p in qs[offset:offset + limit]]
        return Response({"results": rows, "total_count": total, "has_more": offset + limit < total,
                         "next_offset": offset + limit if offset + limit < total else None})
    try:
        with transaction.atomic():
            fields, relations, prizes = _parse_program(request.data)
            program = ReferralProgram.objects.create(slug=_unique_slug(fields["name"]), created_by=admin, **fields)
            for key, rows in relations.items():
                getattr(program, key).set(rows)
            _save_prizes(program, prizes or [])
            engine.sync_program_code(program)
    except Invalid as bad:
        return _refuse(bad.message, bad.code)
    return Response(program_admin_dict(program, detail=True), status=status.HTTP_201_CREATED)


def _program_or_404(slug):
    return ReferralProgram.objects.filter(slug=slug).first()


@api_view(["GET", "PATCH"])
def admin_program(request, slug):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    program = _program_or_404(slug)
    if program is None:
        return _refuse("That program was not found.", "program_not_found", status.HTTP_404_NOT_FOUND)
    if request.method == "PATCH":
        try:
            with transaction.atomic():
                fields, relations, prizes = _parse_program(request.data, existing=program)
                for key, value in fields.items():
                    setattr(program, key, value)
                program.save()
                for key, rows in relations.items():
                    getattr(program, key).set(rows)
                if prizes is not None:
                    _save_prizes(program, prizes)
                engine.sync_program_code(program)
        except Invalid as bad:
            return _refuse(bad.message, bad.code)
    return Response(program_admin_dict(program, detail=True))


@api_view(["GET"])
def admin_program_referrals(request, slug):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    program = _program_or_404(slug)
    if program is None:
        return _refuse("That program was not found.", "program_not_found", status.HTTP_404_NOT_FOUND)
    limit, offset = _page(request)
    qs = program.referrals.select_related("referrer", "referred", "code").order_by("-created_at")
    wanted = request.query_params.get("status")
    if wanted in Referral.STATUSES:
        qs = qs.filter(status=wanted)
    total = qs.count()
    return Response({"results": [referral_dict(r) for r in qs[offset:offset + limit]], "total_count": total,
                     "has_more": offset + limit < total,
                     "next_offset": offset + limit if offset + limit < total else None})


@api_view(["GET"])
def admin_program_rewards(request, slug):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    program = _program_or_404(slug)
    if program is None:
        return _refuse("That program was not found.", "program_not_found", status.HTTP_404_NOT_FOUND)
    limit, offset = _page(request)
    qs = program.rewards.select_related("user", "program", "prize__product_variant__product", "coupon")
    wanted = request.query_params.get("status")
    if wanted in Reward.STATUSES:
        qs = qs.filter(status=wanted)
    qs = qs.order_by("status", "-created_at")
    total = qs.count()
    return Response({"results": [reward_dict(r) for r in qs[offset:offset + limit]], "total_count": total,
                     "has_more": offset + limit < total,
                     "next_offset": offset + limit if offset + limit < total else None})


@api_view(["GET"])
def admin_program_export(request, slug):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    program = _program_or_404(slug)
    if program is None:
        return _refuse("That program was not found.", "program_not_found", status.HTTP_404_NOT_FOUND)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="referrals-{program.slug}.csv"'
    writer = csv.writer(response)
    writer.writerow(["referrer", "referred", "code", "status", "reason", "created_at", "counted_at"])
    for r in program.referrals.select_related("referrer", "referred", "code").order_by("created_at").iterator():
        writer.writerow([r.referrer.username if r.referrer_id else "(program code)", r.referred.username,
                         r.code.code, r.status, r.reason, _iso(r.created_at), _iso(r.counted_at) or ""])
    return response


@api_view(["POST"])
def admin_award_ranks(request, slug):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    program = _program_or_404(slug)
    if program is None:
        return _refuse("That program was not found.", "program_not_found", status.HTTP_404_NOT_FOUND)
    try:
        given = engine.award_ranks(program)
    except engine.ClaimRefused as refused:
        return _refuse(refused.message, refused.code)
    return Response({"awarded": len(given), "program": program_admin_dict(program, detail=True)})


@api_view(["POST"])
def admin_decide(request, token):
    _admin, err = _require_head_admin(request)
    if err:
        return err
    referral = Referral.objects.select_related("program", "referrer", "referred", "code").filter(
        public_token=token).first()
    if referral is None:
        return _refuse("That referral was not found.", "referral_not_found", status.HTTP_404_NOT_FOUND)
    action = str(request.data.get("action") or "")
    if action == "count":
        referral = engine.count(referral)
    elif action == "reject":
        if referral.status == Referral.COUNTED:
            return _refuse("A counted referral cannot be rejected.", "referral_already_counted")
        referral.status, referral.reason = Referral.REJECTED, referral.reason or "admin"
        referral.save(update_fields=["status", "reason"])
    elif action == "release":
        if referral.status != Referral.FLAGGED:
            return _refuse("Only a held referral can be released.", "referral_not_flagged")
        referral.status = Referral.PENDING
        referral.save(update_fields=["status"])
    else:
        return _refuse("Unknown action.", "action_invalid")
    return Response(referral_dict(referral))


@api_view(["POST"])
def admin_deliver(request, token):
    admin, err = _require_head_admin(request)
    if err:
        return err
    reward = Reward.objects.select_related("user", "program", "prize", "coupon").filter(public_token=token).first()
    if reward is None:
        return _refuse("That reward was not found.", "reward_not_found", status.HTTP_404_NOT_FOUND)
    if reward.status != Reward.PENDING:
        return _refuse("That reward is not waiting to be delivered.", "reward_not_pending")
    reward.status = Reward.DELIVERED
    reward.delivered_at = timezone.now()
    reward.delivered_by = admin
    reward.note = str(request.data.get("note") or "").strip()[:200]
    reward.save(update_fields=["status", "delivered_at", "delivered_by", "note"])
    return Response(reward_dict(reward))
