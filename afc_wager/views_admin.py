"""
afc_wager.views_admin - the CMS. Mounted at `wagers/admin/`. Bearer SessionToken; three gates:

    wager_admin    markets: create, edit, publish, lock, reopen, void, suggest, settle, queue,
                   templates, the market side of the overview
    finance_admin  money: users and their Winnings, withdrawals, adjustments, KYC, ledger
    head_admin     everything above, plus settings and every co-sign (a second key must be a
                   DIFFERENT admin: services enforces submitter != approver)
    super_admin / is_superuser count as head_admin.

Every write calls afc_auth.audit.set_audit with a sentence, so the site's audit log (the same
one every admin surface writes to) carries the actor and the target. Refusals are
`{"message", "code"}` (R35). Consumed by frontend app/(a)/a/wagers and app/(a)/a/winnings.
"""
import logging
from datetime import datetime

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from afc_auth.audit import set_audit
from afc_auth.models import User
from afc_auth.slugs import resolve_or_redirect
from afc_auth.views import validate_token
from afc_tournament_and_scrims.models import Event, Match, Stages, TournamentTeam

from . import serializers as ser, services
from .models import (
    Adjustment, KycStatus, LedgerEntry, Market, MarketOption, MarketTemplate, Wager, WagerSettings,
    Withdrawal, WinningsAccount,
)
from .services import WagerError

logger = logging.getLogger(__name__)

MARKET_ROLES = ("wager_admin", "head_admin", "super_admin")
FINANCE_ROLES = ("finance_admin", "head_admin", "super_admin")
HEAD_ROLES = ("head_admin", "super_admin")


def _actor(request):
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return validate_token(auth.split(" ", 1)[1].strip())


def _has(user, roles):
    if not user:
        return False
    if getattr(user, "is_superuser", False):
        return True
    try:
        return user.userroles.filter(role__role_name__in=roles).exists()
    except Exception:  # noqa: BLE001
        return False


def _require(request, roles, code):
    user = _actor(request)
    if not user:
        return None, Response({"message": "Authorization header is required.", "code": "auth_required"},
                              status=status.HTTP_401_UNAUTHORIZED)
    if not _has(user, roles):
        return None, Response({"message": "You do not have access to this.", "code": code},
                              status=status.HTTP_403_FORBIDDEN)
    return user, None


def _require_market_admin(request):
    return _require(request, MARKET_ROLES, "wager_admin_required")


def _require_finance_admin(request):
    return _require(request, FINANCE_ROLES, "finance_admin_required")


def _require_head_admin(request):
    return _require(request, HEAD_ROLES, "head_admin_required")


def _refused(exc):
    return Response(exc.as_dict(), status=exc.status)


def _paginate(request, qs, default=25, cap=200):
    try:
        limit = min(max(int(request.GET.get("limit", default)), 1), cap)
    except (TypeError, ValueError):
        limit = default
    try:
        offset = max(int(request.GET.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    total = qs.count()
    rows = list(qs[offset:offset + limit])
    nxt = offset + limit if offset + limit < total else None
    return rows, {"has_more": nxt is not None, "next_offset": nxt, "total_count": total}


def _int(value, name, *, minimum=0):
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise WagerError(f"{name}_invalid", f"{name.replace('_', ' ')} must be a whole number.")
    if out < minimum:
        raise WagerError(f"{name}_invalid", f"{name.replace('_', ' ')} must be at least {minimum}.")
    return out


def _dt(value, name, *, required=False):
    if value in (None, ""):
        if required:
            raise WagerError(f"{name}_required", f"{name.replace('_', ' ')} is required.")
        return None
    dt = parse_datetime(str(value)) if not isinstance(value, datetime) else value
    if dt is None:
        raise WagerError(f"{name}_invalid", f"{name.replace('_', ' ')} is not a date and time.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return dt


def _market_by_slug(slug):
    """A market by its slug, or by a retired slug (the title was edited and the slug followed it,
    R22): the old address still answers, as `{"status": "moved", "slug": <current>}`, the same
    answer the public view gives, so an admin link in a chat or a bookmark keeps working."""
    m, moved_to = resolve_or_redirect(Market, slug, "slug")
    if m is None:
        return None, Response({"message": "We could not find that market.", "code": "market_not_found"},
                              status=status.HTTP_404_NOT_FOUND)
    if moved_to and moved_to != slug:
        return None, Response({"status": "moved", "slug": moved_to}, status=status.HTTP_200_OK)
    m = Market.objects.select_related("event", "template", "match", "stage", "settled_option", "suggested_option").get(pk=m.pk)
    return m, None


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# settings and templates
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET", "PATCH"])
@authentication_classes([])
def admin_settings(request):
    if request.method == "GET":
        user, err = _require_market_admin(request)
    else:
        user, err = _require_head_admin(request)
    if err:
        return err
    cfg = WagerSettings.get()
    if request.method == "PATCH":
        changed = []
        try:
            for f in WagerSettings.EDITABLE_FIELDS:
                if f not in request.data:
                    continue
                value = request.data.get(f)
                if f == "wagering_enabled":
                    value = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
                elif f == "maintenance_message":
                    value = str(value or "")[:240]
                else:
                    value = _int(value, f)
                setattr(cfg, f, value)
                changed.append(f)
        except WagerError as exc:
            return _refused(exc)
        unknown = set(request.data.keys()) - set(WagerSettings.EDITABLE_FIELDS)
        if unknown:
            return Response({"message": f"Unknown settings: {', '.join(sorted(unknown))}.", "code": "unknown_fields"},
                            status=status.HTTP_400_BAD_REQUEST)
        cfg.updated_by = user
        cfg.save()
        set_audit(request, f"Changed wager settings: {', '.join(changed)}", fields=changed)
    return Response({"message": "Settings saved." if request.method == "PATCH" else "", "settings": ser.settings_dict(cfg)})


@api_view(["GET", "POST"])
@authentication_classes([])
def templates(request):
    user, err = _require_market_admin(request)
    if err:
        return err
    if request.method == "GET":
        return Response({"results": [ser.template_dict(t) for t in MarketTemplate.objects.all()]})
    return _save_template(request, user, MarketTemplate())


@api_view(["PATCH"])
@authentication_classes([])
def template_detail(request, code):
    user, err = _require_market_admin(request)
    if err:
        return err
    t = MarketTemplate.objects.filter(code=code).first()
    if t is None:
        return Response({"message": "We could not find that template.", "code": "template_not_found"},
                        status=status.HTTP_404_NOT_FOUND)
    return _save_template(request, user, t)


def _save_template(request, user, t):
    data = request.data
    if t.pk is None or "code" in data:
        code = str(data.get("code") or "").strip().lower()
        if not code:
            return Response({"message": "A template needs a code.", "code": "code_required"}, status=400)
        if MarketTemplate.objects.filter(code=code).exclude(pk=t.pk).exists():
            return Response({"message": "That code is taken.", "code": "code_taken"}, status=409)
        t.code = code[:40]
    if "name" in data or t.pk is None:
        t.name = str(data.get("name") or "").strip()[:80]
        if not t.name:
            return Response({"message": "A template needs a name.", "code": "name_required"}, status=400)
    if "description" in data:
        t.description = str(data.get("description") or "")[:240]
    if "option_source" in data:
        if data["option_source"] not in dict(MarketTemplate.OPTION_SOURCES):
            return Response({"message": "Unknown option source.", "code": "option_source_invalid"}, status=400)
        t.option_source = data["option_source"]
    if "settle_rule" in data:
        if data["settle_rule"] not in dict(MarketTemplate.SETTLE_RULES):
            return Response({"message": "Unknown settle rule.", "code": "settle_rule_invalid"}, status=400)
        t.settle_rule = data["settle_rule"]
    if "needs_match" in data:
        t.needs_match = bool(data["needs_match"])
    if "is_active" in data:
        t.is_active = bool(data["is_active"])
    if "sort_order" in data:
        try:
            t.sort_order = int(data["sort_order"])
        except (TypeError, ValueError):
            return Response({"message": "Sort order must be a number.", "code": "sort_order_invalid"}, status=400)
    t.save()
    set_audit(request, f"Saved wager template {t.code}")
    return Response({"message": "Template saved.", "template": ser.template_dict(t)},
                    status=status.HTTP_201_CREATED if request.method == "POST" else 200)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# pickers: events, stages, matches, teams, players of an event (for the create form)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def picker_events(request):
    user, err = _require_market_admin(request)
    if err:
        return err
    q = (request.GET.get("q") or "").strip()
    qs = Event.objects.all().order_by("-event_id")
    if q:
        qs = qs.filter(event_name__icontains=q)
    rows = list(qs[:30])
    return Response({"results": [{"id": e.pk, "slug": e.slug, "name": e.event_name, "status": e.event_status} for e in rows]})


@api_view(["GET"])
@authentication_classes([])
def picker_event(request, event_id):
    """Stages, matches, teams and players of one event, for the market form."""
    user, err = _require_market_admin(request)
    if err:
        return err
    event = Event.objects.filter(pk=event_id).first()
    if event is None:
        return Response({"message": "We could not find that event.", "code": "event_not_found"}, status=404)
    stages = list(Stages.objects.filter(event=event).order_by("stage_order" if hasattr(Stages, "stage_order") else "pk"))
    matches = list(Match.objects.filter(group__stage__event=event).select_related("group", "group__stage").order_by("group__stage_id", "match_number"))
    teams = list(TournamentTeam.objects.filter(event=event).select_related("team", "ghost_team"))
    players = []
    for tt in teams:
        for member in tt.members.select_related("user").all():
            if getattr(member, "user_id", None):
                players.append({"id": member.user_id, "username": member.user.username, "team": tt.display_name})
    return Response({
        "event": {"id": event.pk, "slug": event.slug, "name": event.event_name},
        "stages": [{"id": s.pk, "name": s.stage_name} for s in stages],
        "matches": [{"id": m.pk, "number": m.match_number, "stage_id": m.group.stage_id if m.group_id else None,
                     "group": m.group.group_name if m.group_id else None, "result_in": bool(m.result_inputted),
                     "map": m.match_map} for m in matches],
        "teams": [{"id": t.pk, "name": t.display_name} for t in teams],
        "players": players,
    })


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# markets
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def markets(request):
    user, err = _require_market_admin(request)
    if err:
        return err
    qs = Market.objects.select_related("event", "template", "settled_option", "suggested_option", "created_by")
    st = (request.GET.get("status") or "").upper()
    if st and st in dict(Market.STATUS_CHOICES):
        qs = qs.filter(status=st)
    if request.GET.get("event"):
        qs = qs.filter(event__slug=request.GET["event"])
    if request.GET.get("template"):
        qs = qs.filter(template__code=request.GET["template"])
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(slug__icontains=q) | Q(event__event_name__icontains=q))
    rows, page = _paginate(request, qs.order_by("-created_at"))
    return Response({"results": [ser.admin_market_row(m) for m in rows], **page})


def _apply_market_fields(m, data, *, creating):
    """The named field list a market takes from the body (R65). Raises WagerError."""
    cfg = WagerSettings.get()
    if creating or "title" in data:
        m.title = str(data.get("title") or "").strip()[:120]
        if not m.title:
            raise WagerError("title_required", "A market needs a title.")
    if "description" in data:
        m.description = str(data.get("description") or "")
    if "rules_text" in data:
        m.rules_text = str(data.get("rules_text") or "")
    if creating or "lock_at" in data:
        m.lock_at = _dt(data.get("lock_at"), "lock_at", required=True)
    if "open_at" in data:
        m.open_at = _dt(data.get("open_at"), "open_at")
    if "visibility" in data:
        if data["visibility"] not in dict(Market.VISIBILITY_CHOICES):
            raise WagerError("visibility_invalid", "Unknown visibility.")
        m.visibility = data["visibility"]
    if "featured" in data:
        m.featured = bool(data["featured"])
    if "over_under_line" in data and data["over_under_line"] not in (None, ""):
        m.over_under_line = _int(data["over_under_line"], "over_under_line", minimum=1)
    for f, default in (("rake_bps", cfg.rake_bps), ("cancel_fee_bps", cfg.cancel_fee_bps),
                       ("min_stake_kobo", cfg.min_stake_kobo), ("max_stake_per_user_kobo", cfg.max_stake_per_user_kobo),
                       ("max_pool_kobo", cfg.max_pool_kobo)):
        if creating and f not in data:
            setattr(m, f, default)
        elif f in data:
            setattr(m, f, _int(data[f], f))
    if m.rake_bps > 2000:
        raise WagerError("rake_bps_invalid", "The rake cannot be above 20%.")
    if m.cancel_fee_bps > 1000:
        raise WagerError("cancel_fee_bps_invalid", "The cancel fee cannot be above 10%.")
    if m.open_at and m.lock_at and m.open_at >= m.lock_at:
        raise WagerError("open_at_invalid", "The market must open before it locks.")


@api_view(["POST"])
@authentication_classes([])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def create_market(request):
    user, err = _require_market_admin(request)
    if err:
        return err
    data = request.data
    try:
        template = MarketTemplate.objects.filter(code=data.get("template"), is_active=True).first()
        if template is None:
            raise WagerError("template_required", "Pick a template.")
        event = Event.objects.filter(pk=data.get("event_id")).first()
        if event is None:
            raise WagerError("event_required", "Pick an event.")
        match = None
        if data.get("match_id") not in (None, ""):
            match = Match.objects.filter(pk=data.get("match_id"), group__stage__event=event).first()
            if match is None:
                raise WagerError("match_invalid", "That match is not part of this event.")
        if template.needs_match and match is None:
            raise WagerError("match_required", "This kind of market settles from a match; pick one.")
        stage = None
        if data.get("stage_id") not in (None, ""):
            stage = Stages.objects.filter(pk=data.get("stage_id"), event=event).first()
        elif match is not None and match.group_id:
            stage = match.group.stage
        m = Market(event=event, stage=stage, match=match, template=template, created_by=user)
        _apply_market_fields(m, data, creating=True)
        options = services.build_options(m, template, data.get("options"))
        m.save()
        for o in options:
            o.market = m
        MarketOption.objects.bulk_create(options)
        if data.get("publish"):
            m.status = Market.OPEN
            m.save(update_fields=["status", "updated_at"])
        if request.FILES.get("image"):
            from afc_auth.image_utils import require_image_upload
            m.image = require_image_upload(request.FILES["image"])
            m.save(update_fields=["image", "updated_at"])
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"Created wager market {m.title} ({m.status})", slug=m.slug)
    return Response({"message": "Market published." if m.status == Market.OPEN else "Draft saved.",
                     "market": ser.admin_market_detail(m)}, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
@authentication_classes([])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def market_admin_detail(request, slug):
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    if request.method == "GET":
        return Response(ser.admin_market_detail(m))
    if m.status not in (Market.DRAFT, Market.OPEN):
        return Response({"message": "Only a draft or open market can be edited.", "code": "market_not_editable"},
                        status=status.HTTP_409_CONFLICT)
    try:
        _apply_market_fields(m, request.data, creating=False)
        if "options" in request.data:
            if m.status != Market.DRAFT or m.cached_wager_count:
                raise WagerError("options_locked", "Options can only change on a draft with no stakes.")
            m.options.all().delete()
            MarketOption.objects.bulk_create(services.build_options(m, m.template, request.data.get("options")))
        m.save()
        if request.FILES.get("image"):
            from afc_auth.image_utils import require_image_upload
            m.image = require_image_upload(request.FILES["image"])
            m.save(update_fields=["image", "updated_at"])
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"Edited wager market {m.title}", slug=m.slug)
    return Response({"message": "Market saved.", "market": ser.admin_market_detail(m)})


@api_view(["POST"])
@authentication_classes([])
def publish_market(request, slug):
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    if m.status != Market.DRAFT:
        return Response({"message": "Only a draft can be published.", "code": "market_not_draft"}, status=409)
    if m.options.count() < 2:
        return Response({"message": "A market needs at least two options.", "code": "options_required"}, status=400)
    if m.lock_at <= timezone.now():
        return Response({"message": "The lock time has already passed.", "code": "lock_at_past"}, status=400)
    m.status = Market.OPEN
    m.save(update_fields=["status", "updated_at"])
    set_audit(request, f"Published wager market {m.title}", slug=m.slug)
    return Response({"message": "Market published.", "market": ser.admin_market_detail(m)})


@api_view(["POST"])
@authentication_classes([])
def lock_market(request, slug):
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    if m.status != Market.OPEN:
        return Response({"message": "Only an open market can be locked.", "code": "market_not_open"}, status=409)
    m = services.lock_market(m, by=user)
    set_audit(request, f"Locked wager market {m.title}", slug=m.slug)
    return Response({"message": "Market locked.", "market": ser.admin_market_detail(m)})


@api_view(["POST"])
@authentication_classes([])
def reopen_market(request, slug):
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    try:
        new_lock = _dt(request.data.get("lock_at"), "lock_at", required=True)
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            raise WagerError("reason_required", "Say why the market is reopening.")
        m = services.reopen_market(m, new_lock_at=new_lock, by=user, reason=reason)
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"Reopened wager market {m.title} until {new_lock.isoformat()}: {reason}", slug=m.slug)
    return Response({"message": "Market reopened.", "market": ser.admin_market_detail(m)})


@api_view(["POST"])
@authentication_classes([])
def void_market(request, slug):
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    try:
        settlement = services.void_market(m, by=user, reason=str(request.data.get("reason") or ""))
    except WagerError as exc:
        return _refused(exc)
    m.refresh_from_db()
    set_audit(request, f"Voided wager market {m.title}: {m.void_reason}", slug=m.slug,
              refunded_kobo=settlement.refund_total_kobo)
    return Response({"message": "Market voided; every stake refunded.", "market": ser.admin_market_detail(m)})


@api_view(["POST"])
@authentication_classes([])
def suggest_market(request, slug):
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    try:
        m = services.suggest_settlement(m)
    except WagerError as exc:
        return _refused(exc)
    m = Market.objects.select_related("event", "template", "match", "stage", "settled_option", "suggested_option").get(pk=m.pk)
    return Response({"message": "Suggestion ready." if m.status == Market.PENDING_SETTLEMENT else
                     (m.suggestion_evidence or {}).get("note", "No suggestion yet."),
                     "market": ser.admin_market_detail(m)})


@api_view(["POST"])
@authentication_classes([])
def settle_market(request, slug):
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    option = MarketOption.objects.filter(pk=request.data.get("option_id"), market=m).first()
    if option is None:
        return Response({"message": "Pick the winning option.", "code": "option_required"}, status=400)
    try:
        settlement = services.settle_market(m, final_option=option, by=user,
                                            override_reason=str(request.data.get("override_reason") or ""))
    except WagerError as exc:
        return _refused(exc)
    m = Market.objects.select_related("event", "template", "match", "stage", "settled_option", "suggested_option").get(pk=m.pk)
    set_audit(request, f"Settled wager market {m.title}: {option.label} ({settlement.resolution})", slug=m.slug,
              paid_kobo=settlement.paid_total_kobo, refunded_kobo=settlement.refund_total_kobo)
    return Response({"message": "Market settled and paid.", "market": ser.admin_market_detail(m),
                     "settlement": ser.settlement_dict(settlement)})


@api_view(["GET"])
@authentication_classes([])
def market_wagers(request, slug):
    """Who staked what on this market (the lines the May branch never showed)."""
    user, err = _require_market_admin(request)
    if err:
        return err
    m, err = _market_by_slug(slug)
    if err:
        return err
    qs = Wager.objects.filter(market=m).exclude(status=Wager.EXPIRED).select_related("user").order_by("-created_at")
    rows, page = _paginate(request, qs, default=50)
    return Response({"results": [ser.admin_wager_row(w) for w in rows], **page})


@api_view(["GET"])
@authentication_classes([])
def settlement_queue(request):
    """LOCKED and PENDING_SETTLEMENT markets, oldest lock first, each with its suggestion and
    the evidence, so the admin sees why."""
    user, err = _require_market_admin(request)
    if err:
        return err
    qs = (Market.objects.filter(status__in=(Market.LOCKED, Market.PENDING_SETTLEMENT))
          .select_related("event", "template", "match", "stage", "suggested_option", "settled_option")
          .order_by("lock_at"))
    rows = [ser.admin_market_detail(m) for m in qs]
    return Response({"results": rows, "pending": sum(1 for r in rows if r["status"] == Market.PENDING_SETTLEMENT),
                     "locked": sum(1 for r in rows if r["status"] == Market.LOCKED)})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# overview
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def overview(request):
    user, err = _require(request, MARKET_ROLES + FINANCE_ROLES, "wager_admin_required")
    if err:
        return err
    now = timezone.now()
    markets_by_status = {r["status"]: r["n"] for r in Market.objects.values("status").annotate(n=Count("id"))}
    liabilities = WinningsAccount.objects.aggregate(balance=Sum("balance_kobo"), held=Sum("held_kobo"))
    open_pool = Market.objects.filter(status__in=(Market.OPEN, Market.LOCKED, Market.PENDING_SETTLEMENT)).aggregate(s=Sum("cached_pool_kobo"))["s"] or 0
    day = now - timezone.timedelta(days=1)
    week = now - timezone.timedelta(days=7)
    return Response({
        "house": services.house_totals(),
        "liabilities_kobo": liabilities["balance"] or 0,
        "held_kobo": liabilities["held"] or 0,
        "open_pool_kobo": open_pool,
        "markets": markets_by_status,
        "queue": {"locked": markets_by_status.get(Market.LOCKED, 0),
                  "pending_settlement": markets_by_status.get(Market.PENDING_SETTLEMENT, 0)},
        "withdrawals": {r["status"]: r["n"] for r in Withdrawal.objects.values("status").annotate(n=Count("id"))},
        "adjustments_pending": Adjustment.objects.filter(status=Adjustment.PENDING_COSIGN).count(),
        "stakes_24h_kobo": Wager.objects.filter(paid_at__gte=day, status__in=(Wager.ACTIVE, Wager.WON, Wager.LOST, Wager.REFUNDED)).aggregate(s=Sum("total_stake_kobo"))["s"] or 0,
        "stakes_7d_kobo": Wager.objects.filter(paid_at__gte=week, status__in=(Wager.ACTIVE, Wager.WON, Wager.LOST, Wager.REFUNDED)).aggregate(s=Sum("total_stake_kobo"))["s"] or 0,
        "paid_out_7d_kobo": LedgerEntry.objects.filter(kind=LedgerEntry.WITHDRAWAL_PAID, created_at__gte=week).aggregate(s=Sum("amount_kobo"))["s"] or 0,
        "players_with_balance": WinningsAccount.objects.filter(balance_kobo__gt=0).count(),
        "frozen_accounts": WinningsAccount.objects.filter(frozen=True).count(),
        "settings": ser.settings_dict(WagerSettings.get()),
    })


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# users and their winnings
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def users(request):
    user, err = _require_finance_admin(request)
    if err:
        return err
    qs = WinningsAccount.objects.select_related("user")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(user__username__icontains=q) | Q(user__email__icontains=q))
    if request.GET.get("frozen") == "1":
        qs = qs.filter(frozen=True)
    if request.GET.get("with_balance") == "1":
        qs = qs.filter(balance_kobo__gt=0)
    rows, page = _paginate(request, qs.order_by("-balance_kobo", "-updated_at"))
    return Response({"results": [ser.admin_account_row(a) for a in rows], **page})


def _user_or_404(username):
    u = User.objects.filter(username__iexact=username).first()
    if u is None:
        return None, Response({"message": "We could not find that player.", "code": "user_not_found"}, status=404)
    return u, None


@api_view(["GET"])
@authentication_classes([])
def user_detail(request, username):
    user, err = _require_finance_admin(request)
    if err:
        return err
    target, err = _user_or_404(username)
    if err:
        return err
    account = services.account_for(target)
    limits, caps = services.effective_limits(target)
    return Response({
        "account": ser.admin_account_row(account, kyc=services.kyc_state(target), limits=ser.limits_dict(limits, caps)),
        "wagers": [ser.wager_dict(w) for w in Wager.objects.filter(user=target).exclude(status=Wager.EXPIRED).select_related("market")[:50]],
        "ledger": ser.ledger_rows(LedgerEntry.objects.filter(account=account)[:100]),
        "withdrawals": [ser.withdrawal_dict(w, for_staff=True) for w in Withdrawal.objects.filter(user=target).select_related("bank_account", "user", "reviewed_by", "cosigned_by")[:50]],
        "adjustments": [ser.adjustment_dict(a) for a in Adjustment.objects.filter(user=target).select_related("user", "submitted_by", "cosigned_by")[:50]],
        "bank_accounts": [ser.bank_account_dict(b) for b in target.payout_bank_accounts.all()],
    })


@api_view(["POST"])
@authentication_classes([])
def user_freeze(request, username):
    user, err = _require_finance_admin(request)
    if err:
        return err
    target, err = _user_or_404(username)
    if err:
        return err
    frozen = bool(request.data.get("frozen", True))
    reason = str(request.data.get("reason") or "").strip()
    if frozen and not reason:
        return Response({"message": "Say why the Winnings are being frozen.", "code": "reason_required"}, status=400)
    account = services.set_frozen(target, frozen=frozen, reason=reason, by=user)
    set_audit(request, f"{'Froze' if frozen else 'Unfroze'} Winnings of {target.username}" + (f": {reason}" if reason else ""))
    return Response({"message": "Winnings frozen." if frozen else "Winnings unfrozen.", "account": ser.admin_account_row(account)})


@api_view(["POST"])
@authentication_classes([])
def user_adjust(request, username):
    user, err = _require_finance_admin(request)
    if err:
        return err
    target, err = _user_or_404(username)
    if err:
        return err
    try:
        adj = services.adjust_winnings(target, direction=str(request.data.get("direction") or "").upper(),
                                       amount_kobo=request.data.get("amount_kobo"),
                                       reason=str(request.data.get("reason") or ""), by=user)
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"{adj.direction.title()} adjustment of {adj.amount_kobo} kobo for {target.username} ({adj.status}): {adj.reason}")
    return Response({"message": "Adjustment applied." if adj.status == Adjustment.EXECUTED else
                     "Adjustment submitted; a second admin has to co-sign it.",
                     "adjustment": ser.adjustment_dict(adj)}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@authentication_classes([])
def user_limits(request, username):
    """A finance admin tightens a player's caps or sets a cool-off on their behalf (support
    cases). Loosening from here follows the same 24 h rule."""
    user, err = _require_finance_admin(request)
    if err:
        return err
    target, err = _user_or_404(username)
    if err:
        return err
    try:
        caps = {f: request.data.get(f) for f in ("daily_stake_cap_kobo", "weekly_stake_cap_kobo", "daily_loss_cap_kobo") if f in request.data}
        if caps:
            services.set_limits(target, caps=caps)
        if request.data.get("cooloff_days"):
            services.set_cooloff(target, days=request.data.get("cooloff_days"))
    except WagerError as exc:
        return _refused(exc)
    limits, eff = services.effective_limits(target)
    set_audit(request, f"Changed wager limits of {target.username}")
    return Response({"message": "Limits saved.", "limits": ser.limits_dict(limits, eff)})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ledger, withdrawals, adjustments
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def admin_ledger(request):
    user, err = _require_finance_admin(request)
    if err:
        return err
    qs = LedgerEntry.objects.select_related("account", "account__user", "created_by")
    if request.GET.get("kind"):
        qs = qs.filter(kind=request.GET["kind"].upper())
    if request.GET.get("user"):
        qs = qs.filter(account__user__username__iexact=request.GET["user"])
    if request.GET.get("house") == "1":
        qs = qs.filter(account__isnull=True)
    if request.GET.get("ref"):
        qs = qs.filter(ref=request.GET["ref"])
    rows, page = _paginate(request, qs, default=50)
    return Response({"results": [ser.admin_ledger_row(e) for e in rows], **page})


@api_view(["GET"])
@authentication_classes([])
def withdrawals(request):
    user, err = _require_finance_admin(request)
    if err:
        return err
    qs = Withdrawal.objects.select_related("bank_account", "user", "reviewed_by", "cosigned_by")
    st = (request.GET.get("status") or "").upper()
    if st == "OPEN":
        qs = qs.filter(status__in=Withdrawal.OPEN_STATUSES + (Withdrawal.FAILED,))
    elif st and st in dict(Withdrawal.STATUS_CHOICES):
        qs = qs.filter(status=st)
    rows, page = _paginate(request, qs)
    return Response({"results": [ser.withdrawal_dict(w, for_staff=True) for w in rows], **page})


def _withdrawal_or_404(token):
    w = Withdrawal.objects.select_related("bank_account", "user", "account", "reviewed_by", "cosigned_by").filter(public_token=token).first()
    if w is None:
        return None, Response({"message": "We could not find that withdrawal.", "code": "withdrawal_not_found"}, status=404)
    return w, None


@api_view(["POST"])
@authentication_classes([])
def withdrawal_approve(request, token):
    user, err = _require_finance_admin(request)
    if err:
        return err
    w, err = _withdrawal_or_404(token)
    if err:
        return err
    try:
        w = services.approve_withdrawal(w, by=user)
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"Approved withdrawal {w.public_token} of {w.amount_kobo} kobo for {w.user.username} ({w.status})")
    msg = {Withdrawal.PENDING_COSIGN: "Signed. A second admin has to co-sign this one.",
           Withdrawal.APPROVED: "Approved; the transfer is with Paystack.",
           Withdrawal.PAID: "Approved and paid."}.get(w.status, "Saved.")
    return Response({"message": msg, "withdrawal": ser.withdrawal_dict(w, for_staff=True)})


@api_view(["POST"])
@authentication_classes([])
def withdrawal_reject(request, token):
    user, err = _require_finance_admin(request)
    if err:
        return err
    w, err = _withdrawal_or_404(token)
    if err:
        return err
    try:
        w = services.reject_withdrawal(w, by=user, reason=str(request.data.get("reason") or ""))
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"Rejected withdrawal {w.public_token} for {w.user.username}: {w.reject_reason}")
    return Response({"message": "Withdrawal rejected; the hold is released.", "withdrawal": ser.withdrawal_dict(w, for_staff=True)})


@api_view(["POST"])
@authentication_classes([])
def withdrawal_mark_paid(request, token):
    """When the transfer.success webhook did not come (or was missed): the admin confirms from
    the Paystack dashboard and marks it here."""
    user, err = _require_head_admin(request)
    if err:
        return err
    w, err = _withdrawal_or_404(token)
    if err:
        return err
    try:
        w = services.mark_withdrawal_paid(w)
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"Marked withdrawal {w.public_token} paid by hand")
    return Response({"message": "Marked paid.", "withdrawal": ser.withdrawal_dict(w, for_staff=True)})


@api_view(["GET"])
@authentication_classes([])
def adjustments(request):
    user, err = _require_finance_admin(request)
    if err:
        return err
    qs = Adjustment.objects.select_related("user", "submitted_by", "cosigned_by")
    st = (request.GET.get("status") or "").upper()
    if st and st in dict(Adjustment.STATUS_CHOICES):
        qs = qs.filter(status=st)
    rows, page = _paginate(request, qs)
    return Response({"results": [ser.adjustment_dict(a) for a in rows], **page})


@api_view(["POST"])
@authentication_classes([])
def adjustment_cosign(request, adjustment_id):
    user, err = _require_head_admin(request)
    if err:
        return err
    adj = Adjustment.objects.filter(pk=adjustment_id).select_related("user", "submitted_by").first()
    if adj is None:
        return Response({"message": "We could not find that adjustment.", "code": "adjustment_not_found"}, status=404)
    approve = bool(request.data.get("approve", True))
    try:
        adj = services.cosign_adjustment(adj, by=user, approve=approve, reason=str(request.data.get("reason") or ""))
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"{'Co-signed' if approve else 'Rejected'} adjustment #{adj.pk} for {adj.user.username}")
    return Response({"message": "Co-signed and executed." if approve else "Adjustment rejected.",
                     "adjustment": ser.adjustment_dict(adj)})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# KYC
# ─────────────────────────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
@authentication_classes([])
def kyc_list(request):
    user, err = _require_finance_admin(request)
    if err:
        return err
    q = (request.GET.get("q") or "").strip()
    qs = KycStatus.objects.select_related("user", "forced_by")
    if q:
        qs = qs.filter(user__username__icontains=q)
    rows, page = _paginate(request, qs.order_by("-updated_at"))
    return Response({"results": [{"username": r.user.username, **services.kyc_state(r.user),
                                  "forced_by": r.forced_by.username if r.forced_by_id and r.forced_by else None,
                                  "force_reason": r.force_reason, "forced_at": r.forced_at.isoformat() if r.forced_at else None}
                                 for r in rows], **page})


@api_view(["POST"])
@authentication_classes([])
def kyc_force(request, username):
    user, err = _require_finance_admin(request)
    if err:
        return err
    target, err = _user_or_404(username)
    if err:
        return err
    verified = bool(request.data.get("verified", True))
    try:
        state = services.kyc_force(target, by=user, verified=verified, reason=str(request.data.get("reason") or ""))
    except WagerError as exc:
        return _refused(exc)
    set_audit(request, f"{'Force-verified' if verified else 'Un-verified'} WhatsApp for {target.username}: {request.data.get('reason')}")
    return Response({"message": "Saved.", "kyc": state})
