# afc_auth/views_dashboard.py
# ──────────────────────────────────────────────────────────────────────────────
# THE ADMIN DASHBOARD'S NUMBERS, IN ONE PLACE.
#
# Owner 2026-09-02: "everything actually works fine and as they should, check and ensure it is so."
# The check found that /a/dashboard was not merely thin, it was PRINTING NUMBERS NOBODY HAD
# CALCULATED. Measured on production the same day:
#
#   Diamond Bundles  "0"  and  "Top: 0"      hardcoded in the JSX. 18 paid orders exist.
#   Total Revenue    "N0" and "N0 from ..."  hardcoded in the JSX.
#   Scrims           "0 active"              hardcoded in the JSX; no endpoint ever existed for it.
#   Player Match Stats Records  "0"          the page called an ADMIN endpoint with no Authorization
#                                            header, got HTTP 400, and swallowed it in .catch().
#                                            2,982 rows exist.
#
# A number that is typed into the markup cannot go stale, cannot be wrong, and cannot be right. It
# is decoration wearing the costume of a metric, and it is worse than showing nothing, because an
# admin reads "Total Revenue N0" as a fact about the business.
#
# WHY ONE ENDPOINT RATHER THAN SIX MORE
# The dashboard already made THIRTEEN requests to fill one screen, three of which downloaded an
# entire table to call .length on it: get-all-teams (362 KB), get-all-news (232 KB) and
# get-admin-history (305 KB), about 900 KB to render three integers and ten rows. Adding a
# per-number endpoint for each missing figure would have made that worse. This returns every count
# the dashboard shows, so the page can ask once.
#
# HOW IT CONNECTS
#   Route     GET auth/admin/dashboard-stats/   (afc_auth/urls.py)
#   Consumed  frontend app/(a)/a/dashboard/page.tsx  (the only caller)
#   Reads     afc_auth.User / News / AdminHistory, afc_team.Team,
#             afc_tournament_and_scrims.Event / TournamentPlayerMatchStats / SoloPlayerMatchStats,
#             afc_shop.Order / OrderItem / Product / ProductVariant
#   Writes    nothing. Read-only aggregate.
#   Auth      Bearer, admin only. The existing per-number endpoints this replaces are PUBLIC, and
#             deliberately stay that way so nothing else that calls them breaks; but the shop
#             revenue figures are not public information, so the aggregate is gated.
# ──────────────────────────────────────────────────────────────────────────────
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc.api_utils import authenticate as _authenticate

from afc_team.models import Team
from afc_shop.models import Order, OrderItem, Product, ProductVariant
from afc_tournament_and_scrims.models import (
    Event,
    SoloPlayerMatchStats,
    TournamentPlayerMatchStats,
)

from .models import AdminHistory, News, User


def _is_admin(user):
    """Any admin may READ the dashboard. Deliberately broader than the send-a-broadcast gate: this
    is counts, and a shop_admin has a legitimate reason to see how many orders there are."""
    if not user:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "role", None) == "admin":
        return True
    return user.userroles.exists()


def _money(value):
    """Decimals over the wire as strings, never floats. A revenue figure that has been through
    binary floating point is a revenue figure somebody will eventually have to explain."""
    return str(value if value is not None else Decimal("0.00"))


@api_view(["GET"])
def admin_dashboard_stats(request):
    """GET auth/admin/dashboard-stats/ - every number on the admin dashboard, in one response.

    REQUEST   no parameters.
    RESPONSE  200 {
                members:  {total, verified, this_month},
                teams:    {total, this_month},
                events:   {tournaments, tournaments_active, scrims, scrims_active,
                           popular_format},
                news:     {total, published},
                combat:   {solo_kills, team_kills, total_kills,
                           player_match_records, solo_match_records},
                shop:     {products, variants, diamond_variants, orders_total, orders_paid,
                           revenue_paid, diamond_bundles_sold, diamond_revenue, top_bundle},
                activity: {admin_actions_total},
              }
    AUTH      Bearer; any admin. 403 otherwise.

    EVERY FIGURE IS COMPUTED, none is a constant. Where a figure is genuinely zero it is zero
    because the query said so, which is a different statement from the one the markup used to make.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_admin(user):
        return Response({"message": "Admins only."}, status=status.HTTP_403_FORBIDDEN)

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # ── events ────────────────────────────────────────────────────────────────
    # "Active" is the same window total_active_tournaments has always used (started and not yet
    # ended), applied to scrims too. The dashboard printed "0 active" for scrims as a LITERAL
    # because no endpoint computed it; this is that missing query.
    def _active(kind):
        return Event.objects.filter(
            competition_type=kind, start_date__lte=now, end_date__gte=now,
        ).count()

    popular = (
        Event.objects.exclude(event_type__isnull=True).exclude(event_type="")
        .values("event_type").annotate(n=Count("event_id")).order_by("-n").first()
    )

    # ── shop ──────────────────────────────────────────────────────────────────
    # Revenue counts PAID orders only. Summing every row would include the 13 pending ones, and a
    # dashboard that reports money must not count a basket somebody abandoned as income.
    paid_orders = Order.objects.filter(status="paid")
    diamond_items = (
        OrderItem.objects.filter(order__status="paid")
        .exclude(variant__diamonds_amount=None)
        .exclude(variant__diamonds_amount=0)
    )
    top = (
        diamond_items.values("product_name_snapshot", "variant_title_snapshot")
        .annotate(q=Sum("quantity")).order_by("-q").first()
    )
    top_label = ""
    if top:
        # The snapshot pair can be blank on older rows, so fall back rather than render an empty
        # string as if it were the name of a product.
        top_label = " ".join(
            p for p in (top.get("product_name_snapshot"), top.get("variant_title_snapshot")) if p
        ).strip()

    # ── news ──────────────────────────────────────────────────────────────────
    # total_published_news (afc_tournament_and_scrims.views) counts EVERY News row and calls the
    # result "published", so a scheduled or unpublished item is reported as live. Both numbers are
    # returned separately here so the card can stop labelling one as the other.
    news_total = News.objects.count()

    return Response({
        "members": {
            "total": User.objects.count(),
            "verified": User.objects.filter(is_active=True).count(),
            "this_month": User.objects.filter(date_joined__gte=month_start).count(),
        },
        "teams": {
            "total": Team.objects.count(),
            "this_month": Team.objects.filter(creation_date__gte=month_start).count(),
        },
        "events": {
            "tournaments": Event.objects.filter(competition_type="tournament").count(),
            "tournaments_active": _active("tournament"),
            "scrims": Event.objects.filter(competition_type="scrims").count(),
            "scrims_active": _active("scrims"),
            "popular_format": (popular or {}).get("event_type") or None,
        },
        "news": {
            "total": news_total,
            "published": News.objects.filter(is_published=True).count(),
        },
        "combat": {
            "solo_kills": SoloPlayerMatchStats.objects.aggregate(s=Sum("kills"))["s"] or 0,
            "team_kills": TournamentPlayerMatchStats.objects.aggregate(s=Sum("kills"))["s"] or 0,
            "total_kills": (
                (SoloPlayerMatchStats.objects.aggregate(s=Sum("kills"))["s"] or 0)
                + (TournamentPlayerMatchStats.objects.aggregate(s=Sum("kills"))["s"] or 0)
            ),
            "player_match_records": TournamentPlayerMatchStats.objects.count(),
            "solo_match_records": SoloPlayerMatchStats.objects.count(),
        },
        "shop": {
            "products": Product.objects.count(),
            "variants": ProductVariant.objects.count(),
            "diamond_variants": ProductVariant.objects.exclude(
                diamonds_amount=None).exclude(diamonds_amount=0).count(),
            "orders_total": Order.objects.count(),
            "orders_paid": paid_orders.count(),
            "revenue_paid": _money(paid_orders.aggregate(s=Sum("total"))["s"]),
            "diamond_bundles_sold": diamond_items.aggregate(q=Sum("quantity"))["q"] or 0,
            "diamond_revenue": _money(diamond_items.aggregate(s=Sum("line_total"))["s"]),
            "top_bundle": top_label or None,
        },
        "activity": {
            "admin_actions_total": AdminHistory.objects.count(),
        },
    })
