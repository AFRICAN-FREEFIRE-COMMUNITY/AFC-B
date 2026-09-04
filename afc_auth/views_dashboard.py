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
import datetime
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

from .history_text import describe_history, event_names, humanize_action
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
    # Group by the VARIANT, not by the snapshot pair. The snapshots are blank on older rows, and
    # grouping on them collapsed every such row into ONE nameless bucket which then won, so the
    # card said "no bundle sold yet" over 28 real sales. The variant is the thing that was bought;
    # a snapshot is only how it happened to be named at the time.
    top = (diamond_items.values("variant_id")
           .annotate(q=Sum("quantity")).order_by("-q").first())
    top_label = ""
    if top:
        item = diamond_items.filter(variant_id=top["variant_id"]).first()
        variant = getattr(item, "variant", None)
        # Prefer the snapshot, which is what the buyer actually saw; fall back to the live variant
        # only when it is blank. An empty string is never rendered as if it were a product name.
        parts = [item.product_name_snapshot, item.variant_title_snapshot] if item else []
        if not any(parts) and variant is not None:
            product = getattr(variant, "product", None)
            parts = [getattr(product, "name", "") or "", variant.title or ""]
        top_label = " ".join(p for p in parts if p).strip()
        if not top_label and variant is not None and variant.diamonds_amount:
            top_label = f"{variant.diamonds_amount} diamonds"

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
            # The ten rows the dashboard table renders, sent WITH the counts. The page used
            # to pull get-admin-history in full (305 KB, 1,545 rows) and slice(0, 10) in the
            # browser, which is 1,535 rows of network for nothing.
            "recent": _recent_rows(AdminHistory.objects.order_by("-timestamp")[:10]),
        },
    })


# ══════════════════════════════════════════════════════════════════════════════════════════════
# THE DRILL-DOWN (owner 2026-09-02: "when you click on each text or mini tab it takes you that
# stats and it opens up to much more detail")
#
# ONE SHAPE FOR EVERY METRIC, which is the whole design. Each builder returns the same envelope:
#
#     {metric, title, subtitle, headline: [{label, value, hint}], sections: [
#         {key, title, note, columns: [...], rows: [[...], ...]}
#     ]}
#
# so the frontend renders ELEVEN drill-downs with ONE component and no per-metric branching. Adding
# a twelfth metric is a builder in the registry below and nothing else; a metric whose detail view
# needs bespoke frontend code has been designed wrong.
#
# Rows are pre-formatted primitives (string or number), never model instances, because the renderer
# must not need to know what a Team is in order to put one in a table.
# ══════════════════════════════════════════════════════════════════════════════════════════════
from .country_grouping import group_country_counts


def _section(key, title, columns, rows, note=""):
    """One table in a detail view. `rows` is a list of lists, already ordered and formatted."""
    return {"key": key, "title": title, "note": note, "columns": columns, "rows": rows}


def _recent_rows(rows):
    """Serialise admin-history rows for a human reader.

    THE COMPLAINT THIS ANSWERS (owner 2026-09-03): the table printed the raw stored text, and
    edit_event stores its row as a JSON document, so the dashboard showed a line beginning
    { "event_id": 333, "changes": [ "event_name: ... at a person, escape sequences and all.

    `summary` is now one sentence and `details` carries every individual change for the expander,
    so nothing is hidden, it is just no longer shouted in JSON. See afc_auth/history_text.py. The
    event NAME costs ONE extra query for the whole page (event_names), because the stored blob
    records only an id.

    admin_user is a ForeignKey and it is NULLABLE, so the username is read defensively rather than
    serialising a User object or crashing on None.
    """
    rows = list(rows)
    names = event_names([r.description for r in rows])
    out = []
    for h in rows:
        told = describe_history(h.action, h.description, names)
        out.append({
            "id": h.action_id,
            "admin_user": getattr(h.admin_user, "username", None) or "Unknown",
            "action": h.action,
            "action_label": humanize_action(h.action),
            "summary": told["summary"],
            "details": told["details"],
            "timestamp": h.timestamp.isoformat() if h.timestamp else None,
        })
    return out


def _stat(label, value, hint=""):
    return {"label": label, "value": value, "hint": hint}


def _monthly(queryset, field, months=12):
    """[[YYYY-MM, count], ...] for the last `months` months, oldest first, INCLUDING the months
    that have no rows. A series that silently omits its empty months draws a shape that lies about
    the trend."""
    now = timezone.now()
    buckets = []
    year, month = now.year, now.month
    for _ in range(months):
        buckets.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    buckets.reverse()

    first_year, first_month = buckets[0]
    start = datetime.datetime(first_year, first_month, 1, tzinfo=datetime.timezone.utc)
    counts = {}
    for value in queryset.filter(**{f"{field}__gte": start}).values_list(field, flat=True):
        if value:
            key = (value.year, value.month)
            counts[key] = counts.get(key, 0) + 1
    return [[f"{y:04d}-{m:02d}", counts.get((y, m), 0)] for y, m in buckets]


def _country_rows(queryset, field="country", limit=20):
    """Country counts, FOLDED. User.country and Team.country hold both ISO codes and full names for
    the same country ("NG" and "Nigeria"), so a raw group-by splits one country across two rows and
    ranks both too low. group_country_counts is the existing fix for exactly that."""
    raw = {}
    rows = (queryset.exclude(**{f"{field}__isnull": True}).exclude(**{field: ""})
            .values(field).annotate(n=Count("pk")))
    for row in rows:
        raw[row[field]] = raw.get(row[field], 0) + row["n"]
    return [[g["label"], g["count"]] for g in group_country_counts(raw)[:limit]]


def _counts_raw(queryset, field, limit=None):
    """[[stored_value, count], ...], biggest first. The sibling of _counts for callers that must
    translate the value themselves: an action slug becomes a sentence, not a Title Cased slug."""
    rows = queryset.values(field).annotate(n=Count("pk")).order_by("-n")
    if limit:
        rows = rows[:limit]
    return [[row[field], row["n"]] for row in rows]


def _counts(queryset, field, limit=None):
    """[[label, count], ...] for a plain group-by, biggest first."""
    rows = queryset.values(field).annotate(n=Count("pk")).order_by("-n")
    if limit:
        rows = rows[:limit]
    out = []
    for row in rows:
        value = row[field]
        label = str(value).replace("_", " ").title() if value else "Not set"
        out.append([label, row["n"]])
    return out


def _detail_members(_request):
    qs = User.objects.all()
    return {
        "title": "Members",
        "subtitle": "Every account on the platform, and where they came from.",
        "headline": [
            _stat("Total members", qs.count()),
            _stat("Active", qs.filter(is_active=True).count()),
            _stat("Suspended", qs.filter(is_active=False).count(),
                  "Accounts blocked from signing in."),
        ],
        "sections": [
            _section("by_month", "Joined per month", ["Month", "New members"],
                     _monthly(qs, "date_joined"), "Last 12 months, empty months included."),
            _section("by_country", "By country", ["Country", "Members"], _country_rows(qs),
                     "Spellings folded: NG and Nigeria are one row."),
            _section("by_role", "By role", ["Role", "Members"], _counts(qs, "role")),
            _section("by_language", "By language", ["Language", "Members"], _counts(qs, "language")),
        ],
    }


def _detail_teams(_request):
    qs = Team.objects.all()
    return {
        "title": "Teams",
        "subtitle": "Registered teams, their tiers and where they are based.",
        "headline": [_stat("Total teams", qs.count())],
        "sections": [
            _section("by_month", "Created per month", ["Month", "New teams"],
                     _monthly(qs, "creation_date")),
            _section("by_tier", "By tier", ["Tier", "Teams"], _counts(qs, "team_tier")),
            _section("by_country", "By country", ["Country", "Teams"], _country_rows(qs)),
        ],
    }


def _events_detail(kind, title, subtitle):
    """Tournaments and scrims are the same shape, so they are the same builder. The ONLY difference
    is competition_type, and getting that value wrong platform-wide is precisely the bug this audit
    started from, so it is passed in from the registry and never spelled twice."""
    def build(_request):
        qs = Event.objects.filter(competition_type=kind)
        now = timezone.now()
        return {
            "title": title,
            "subtitle": subtitle,
            "headline": [
                _stat(f"Total {title.lower()}", qs.count()),
                _stat("Running now", qs.filter(start_date__lte=now, end_date__gte=now).count(),
                      "Started and not yet finished."),
                _stat("Drafts", qs.filter(is_draft=True).count(), "Not visible to players."),
            ],
            "sections": [
                _section("by_status", "By status", ["Status", "Events"], _counts(qs, "event_status")),
                _section("by_month", "Starting per month", ["Month", "Events"],
                         _monthly(qs, "start_date")),
                _section("by_format", "By format", ["Format", "Events"], _counts(qs, "event_type")),
                _section("by_mode", "By mode", ["Mode", "Events"], _counts(qs, "event_mode")),
                _section("by_tier", "By tier", ["Tier", "Events"], _counts(qs, "tournament_tier")),
                # Same reasoning as the revenue table: name the three columns rather than
                # selecting all 87 that Event carries.
                _section("recent", "Most recent", ["Event", "Status", "Starts"],
                         [[e["event_name"], (e["event_status"] or "-").title(),
                           str(e["start_date"])]
                          for e in qs.order_by("-start_date")
                          .values("event_name", "event_status", "start_date")[:15]]),
            ],
        }
    return build


def _detail_news(_request):
    qs = News.objects.all()
    return {
        "title": "News and announcements",
        "subtitle": "What has been posted, and what is still waiting to go out.",
        "headline": [
            _stat("Total posts", qs.count()),
            _stat("Published", qs.filter(is_published=True).count()),
            _stat("Unpublished", qs.filter(is_published=False).count(),
                  "Drafts and scheduled posts. The dashboard used to report these as published."),
        ],
        "sections": [
            _section("by_category", "By category", ["Category", "Posts"], _counts(qs, "category")),
            _section("by_month", "Posted per month", ["Month", "Posts"], _monthly(qs, "created_at")),
            _section("recent", "Most recent", ["Title", "Category", "Published"],
                     [[n["news_title"], (n["category"] or "-").title(),
                       "Yes" if n["is_published"] else "No"]
                      for n in qs.order_by("-created_at")
                      .values("news_title", "category", "is_published")[:15]]),
        ],
    }


def _detail_shop(_request):
    orders = Order.objects.all()
    paid = orders.filter(status="paid")
    items = OrderItem.objects.filter(order__status="paid")
    top = (items.values("product_name_snapshot", "variant_title_snapshot")
           .annotate(q=Sum("quantity")).order_by("-q")[:15])
    return {
        "title": "Shop",
        "subtitle": "Products, orders and what people actually bought.",
        "headline": [
            _stat("Products", Product.objects.count()),
            _stat("Variants", ProductVariant.objects.count()),
            _stat("Orders", orders.count()),
            _stat("Paid orders", paid.count(), "Only these count toward revenue."),
        ],
        "sections": [
            _section("by_status", "Orders by status", ["Status", "Orders"], _counts(orders, "status")),
            _section("by_month", "Orders per month", ["Month", "Orders"],
                     _monthly(orders, "created_at")),
            _section("top", "Best sellers", ["Product", "Units"],
                     [[" ".join(p for p in (r["product_name_snapshot"],
                                            r["variant_title_snapshot"]) if p).strip() or "Unnamed",
                       r["q"]] for r in top],
                     "Paid orders only. Older rows can carry a blank product snapshot."),
        ],
    }


def _detail_revenue(_request):
    paid = Order.objects.filter(status="paid")
    items = (OrderItem.objects.filter(order__status="paid")
             .exclude(variant__diamonds_amount=None).exclude(variant__diamonds_amount=0))
    by_month = {}
    for created, total in paid.values_list("created_at", "total"):
        if created:
            key = f"{created.year:04d}-{created.month:02d}"
            by_month[key] = by_month.get(key, Decimal("0")) + (total or Decimal("0"))
    return {
        "title": "Revenue",
        "subtitle": "Money actually taken. Pending and abandoned baskets are excluded.",
        "headline": [
            _stat("Paid revenue", _money(paid.aggregate(s=Sum("total"))["s"])),
            _stat("From diamonds", _money(items.aggregate(s=Sum("line_total"))["s"])),
            _stat("Paid orders", paid.count()),
        ],
        "sections": [
            _section("by_month", "Revenue per month", ["Month", "Revenue"],
                     [[k, str(v)] for k, v in sorted(by_month.items())]),
            _section("by_provider", "By payment provider", ["Provider", "Orders"],
                     _counts(paid, "provider")),
            # .values(), not model instances. Fetching whole Orders here selects all 34 columns
            # to render three, and it couples a STATS endpoint to every future column added to the
            # table: a dev database one migration behind made this 500 with "Unknown column
            # afc_shop_order.buyer_confirmed_at". Naming the columns is both cheaper and immune.
            _section("largest", "Largest paid orders", ["Order", "Total", "Placed"],
                     [[f"#{o['id']}", str(o["total"]),
                       str(o["created_at"].date()) if o["created_at"] else "-"]
                      for o in paid.order_by("-total").values("id", "total", "created_at")[:15]]),
        ],
    }


def _detail_kills(_request):
    team_rows = TournamentPlayerMatchStats.objects
    solo_rows = SoloPlayerMatchStats.objects
    return {
        "title": "Platform kills",
        "subtitle": "Every kill recorded, across solo and team play.",
        "headline": [
            _stat("Total kills",
                  (solo_rows.aggregate(s=Sum("kills"))["s"] or 0)
                  + (team_rows.aggregate(s=Sum("kills"))["s"] or 0)),
            _stat("Team kills", team_rows.aggregate(s=Sum("kills"))["s"] or 0),
            _stat("Solo kills", solo_rows.aggregate(s=Sum("kills"))["s"] or 0),
        ],
        "sections": [
            _section("records", "Where the numbers come from", ["Source", "Rows"],
                     [["Team match stats", team_rows.count()],
                      ["Solo match stats", solo_rows.count()]]),
        ],
    }


def _detail_match_stats(_request):
    qs = TournamentPlayerMatchStats.objects.all()
    return {
        "title": "Player match stats",
        "subtitle": "Individual per-match records. This card read 0 until 2026-09-02, because the "
                    "page asked for them without an Authorization header and hid the 400.",
        "headline": [
            _stat("Team match records", qs.count()),
            _stat("Solo match records", SoloPlayerMatchStats.objects.count()),
        ],
        "sections": [
            # player__username through .values() rather than select_related + attribute access:
            # one query, four columns, and no chance of serialising a User by accident, which is
            # the mistake the recent-activity rows made.
            _section("top", "Most kills in a single match",
                     ["Player", "Kills", "Damage", "Assists"],
                     [[r["player__username"] or "Unknown", r["kills"] or 0,
                       r["damage"] or 0, r["assists"] or 0]
                      for r in qs.order_by("-kills")
                      .values("player__username", "kills", "damage", "assists")[:20]]),
        ],
    }


def _detail_formats(_request):
    qs = Event.objects.all()
    return {
        "title": "Event formats",
        "subtitle": "How events are run, across everything ever hosted.",
        "headline": [_stat("Events", qs.count())],
        "sections": [
            _section("by_format", "By format", ["Format", "Events"], _counts(qs, "event_type")),
            _section("by_mode", "By mode", ["Mode", "Events"], _counts(qs, "event_mode")),
            _section("by_kind", "Tournaments vs scrims", ["Kind", "Events"],
                     _counts(qs, "competition_type")),
            _section("by_participants", "By participant type", ["Participants", "Events"],
                     _counts(qs, "participant_type")),
        ],
    }


def _detail_activity(_request):
    qs = AdminHistory.objects.all()
    return {
        "title": "Admin activity",
        "subtitle": "Who has been doing what across the dashboard.",
        "headline": [_stat("Recorded actions", qs.count())],
        "sections": [
            # admin_user__username, not admin_user: grouping on the FK buckets by id and
            # labels each row with a User object rather than a name.
            _section("by_admin", "Busiest admins", ["Admin", "Actions"],
                     _counts(qs, "admin_user__username", limit=20)),
            # The action column stores a SLUG (edit_event). Grouping stays on the slug because
            # that is the identity; only the label is put into English. See history_text.
            _section("by_action", "By action", ["Action", "Count"],
                     [[humanize_action(slug), n]
                      for slug, n in _counts_raw(qs, "action", limit=25)]),
            _section("by_month", "Actions per month", ["Month", "Actions"],
                     _monthly(qs, "timestamp")),
            # The counts above say HOW MUCH happened. This says WHAT happened, which is the
            # question somebody opening this page actually has.
            _section("latest", "Latest 50 actions", ["When", "Admin", "What happened"],
                     [[r["timestamp"], r["admin_user"], r["summary"]]
                      for r in _recent_rows(qs.order_by("-timestamp")[:50])]),
        ],
    }


# THE REGISTRY. The frontend's card-to-route mapping mirrors these keys exactly, so a key that is
# not here is a 404 rather than an empty page pretending to be a metric.
DETAIL_BUILDERS = {
    "members": _detail_members,
    "teams": _detail_teams,
    "tournaments": _events_detail("tournament", "Tournaments", "Every tournament hosted on AFC."),
    "scrims": _events_detail("scrims", "Scrims", "Every scrims block hosted on AFC."),
    "news": _detail_news,
    "shop": _detail_shop,
    "revenue": _detail_revenue,
    "kills": _detail_kills,
    "match-stats": _detail_match_stats,
    "formats": _detail_formats,
    "activity": _detail_activity,
}


@api_view(["GET"])
def admin_dashboard_detail(request, metric):
    """GET auth/admin/dashboard-stats/<metric>/ - the breakdown behind one dashboard number.

    REQUEST   metric, one of DETAIL_BUILDERS above.
    RESPONSE  200 {metric, title, subtitle, headline: [{label, value, hint}],
                   sections: [{key, title, note, columns, rows}]}
              404 {message, available: [...]} for an unknown metric, NAMING the valid keys rather
                  than leaving the caller to guess.
    AUTH      Bearer; any admin, the same gate as the summary above.
    CONSUMED BY  frontend app/(a)/a/dashboard/[metric]/page.tsx - one component for all eleven.
    """
    user, err = _authenticate(request)
    if err:
        return err
    if not _is_admin(user):
        return Response({"message": "Admins only."}, status=status.HTTP_403_FORBIDDEN)

    builder = DETAIL_BUILDERS.get(metric)
    if not builder:
        return Response(
            {"message": f"Unknown dashboard metric '{metric}'.",
             "available": sorted(DETAIL_BUILDERS)},
            status=status.HTTP_404_NOT_FOUND,
        )
    payload = builder(request)
    payload["metric"] = metric
    return Response(payload)
