# afc_organizers/views_blacklist_lookup.py
# ──────────────────────────────────────────────────────────────────────────────
# BLACKLIST VISIBILITY endpoints (owner ask 2026-06-12): "other organizers can see if a team
# or player was blacklisted by other orgs and how many times a team has been blacklisted in
# any time frame they decide to check; AFC admins can also see this on a dashboard:
# blacklists, how many times, by whom and why."
#
# Two audiences, two views, gated differently:
#   • ORGANIZER LOOKUP (blacklist_lookup)  - any ACTIVE organization member (an organizer of
#     ANY org) or a platform admin looks up one team OR one player and gets: whether they were
#     blacklisted, how many times, by which organizations, and when - over an optional date
#     window. PRIVACY RULE (owner): organizers looking at OTHER orgs' blacklists see counts,
#     org names, and dates, but NEVER the reasons. Only platform admins get `reason`.
#   • ADMIN DASHBOARD FEED (admin_list_blacklists) - platform admins (is_platform_org_admin:
#     head_admin / organizer_admin) get the full cross-org list of every OrganizerBlacklist
#     row, reasons included, with search / status / date-window filters, pagination, and
#     top-level aggregates (total, active, top orgs, most-blacklisted teams) for stat cards.
#
# Convention note: deliberately mirrors the sibling afc_organizers/views_blacklist.py and
# views_reports.py - function-based @api_view views, the inline Bearer handshake via
# afc_auth.views.validate_token (own private _authenticate copy, per-file like the siblings),
# inline dict serialization (no serializers.py), Response({...}, status=...) everywhere.
#
# How this connects to the rest of the system:
#   - Models read: OrganizerBlacklist / OrganizerBlacklistPlayer / BlacklistLiftRequest
#     (models.py) and OrganizationMember (the "is the caller an organizer at all?" gate).
#   - Permission: afc_organizers.permissions.is_platform_org_admin for the admin gate and
#     the reason-visibility switch on the lookup.
#   - Search fold: utils.search_utils (normalized_column / separator_stripped), the same
#     punctuation-insensitive matching the sitewide typeaheads use.
#   - Routes: organizers/blacklist-lookup/ and organizers/admin/blacklists/ in urls.py.
#   - Frontend consumers: the organizer dashboard "Blacklists" page Lookup section
#     (app/(organizer)/organizer/blacklists/page.tsx, organizersApi.lookupBlacklists) and the
#     AFC admin dashboard page (app/(a)/a/blacklists/page.tsx, organizersApi.adminListBlacklists).
# Related spec: WEBSITE/tasks/organizer-blacklist-design.md (the underlying blacklist feature).
# ──────────────────────────────────────────────────────────────────────────────
from datetime import datetime, time

from django.db.models import Count, Q
from django.utils import timezone

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token lives in afc_auth.views - imported the SAME way the sibling organizer view
# modules import it. Returns the User for a valid, non-expired session token.
from afc_auth.views import validate_token
from afc_auth.models import User

from afc_team.models import Team

from afc_organizers.models import (
    OrganizationMember,
    OrganizerBlacklist,
    OrganizerBlacklistPlayer,
    BlacklistLiftRequest,
)
from afc_organizers.permissions import is_platform_org_admin


# ──────────────────────────────────────────────────────────────────────────────
# §0  Shared helpers (per-file copies, matching the sibling modules' convention)
# ──────────────────────────────────────────────────────────────────────────────
def _authenticate(request):
    """Resolve the caller from the Authorization header exactly like the sibling views: read
    the raw header, require the "Bearer " scheme, strip it, hand the token to validate_token.
    Returns (user, None) on success or (None, error_response) so each view can bail early:
    400 missing header / 400 bad format / 401 invalid-or-expired token."""
    session_token = request.headers.get("Authorization")
    if not session_token:
        return None, Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return None, Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None


def _paginate(request, queryset):
    """List endpoints accept ?limit (default 25, max 100) and ?offset, returning
    (page, total_count, has_more). Junk values fall back to the default rather than 500-ing."""
    try:
        limit = int(request.GET.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


def _parse_day(value, end_of_day):
    """Turn an ISO YYYY-MM-DD string into a tz-aware datetime at day-start (or day-END when
    end_of_day, so the whole selected day is covered). Returns None for missing/malformed input
    (callers map a malformed-but-present value to a 400). Same parser the blacklist create view
    uses, so the lookup window and the stored start/end dates round-trip identically."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError, AttributeError):
        return None
    tod = time(23, 59, 59) if end_of_day else time(0, 0, 0)
    return timezone.make_aware(datetime.combine(parsed, tod))


def _effective_status(blacklist, now, player_row_active=None):
    """Collapse the stored status + live expiry (+ the per-player retire flag, when this entry
    is being read FOR a specific player) into the three statuses the visibility surfaces show:

      lifted  - the whole blacklist was lifted, OR (player lookups only) this player's own
                snapshot row was individually retired (is_active=False) - their block is over
                even though team-mates may still be blocked.
      expired - past end_date. Includes "active"-status rows whose end_date has lapsed but no
                sweep has relabelled them yet (mirrors is_currently_active(): enforcement
                already treats those as not-blocking, so visibility must agree).
      active  - currently blocking.
    """
    if blacklist.status == "lifted":
        return "lifted"
    if player_row_active is False:
        return "lifted"
    if blacklist.status == "expired" or (blacklist.end_date and blacklist.end_date <= now):
        return "expired"
    return "active"


# ──────────────────────────────────────────────────────────────────────────────
# §1  GET blacklist-lookup/  - cross-org visibility for organizers (counts, no reasons)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def blacklist_lookup(request):
    """Look up one team OR one player across EVERY organization's blacklists: whether they were
    blacklisted, how many times, by which orgs, and when - over an optional date window.

    PLAYER-LOOKUP SEMANTICS (documented per the build brief): a player "was blacklisted" by
    every OrganizerBlacklist under which they have an OrganizerBlacklistPlayer snapshot row.
    The snapshot rows ARE the follows-the-player rule (a player is snapshotted onto the
    team-level row at blacklist time and stays bound to it even after leaving that team), and
    every snapshot row points at exactly one team-level OrganizerBlacklist - so "snapshot rows
    plus the team-level rows of teams they were snapshotted on" is ONE set of distinct
    OrganizerBlacklist rows, which is what we count. A blacklist of a team the player was
    NEVER snapshotted on (they joined after the blacklist was created) does not count against
    the player - it never blocked them.

    Auth:  Bearer. Caller must be an ACTIVE OrganizationMember of ANY org (an organizer) or a
           platform admin (is_platform_org_admin). Everyone else (plain players) gets 403 -
           this is an organizer-to-organizer trust surface, not public data.
    Query: ?team_id=<id> OR ?user_id=<id> (exactly one, 400 otherwise)
           &start=YYYY-MM-DD &end=YYYY-MM-DD (both optional = all time; the window filters on
           each blacklist's start_date, i.e. "blacklists CREATED/STARTED in this window")
           &limit/&offset (standard paging over the entries).
    Response: 200 {
        target: {type:"team"|"player", team_id|user_id, team_name|username},
        window: {start, end},          # the applied window echoed back (ISO or null)
        total_count,                   # blacklist rows whose start_date falls in the window
        active_count,                  # of those, the ones blocking RIGHT NOW (for a player:
                                       #   their own snapshot row must also still be active)
        entries: [{id, organization_name, organization_slug, team_id, team_name,
                   start_date, end_date, status: active|expired|lifted
                   (+ reason - PLATFORM ADMINS ONLY)}],   # newest first
        has_more }
    PRIVACY: `reason` is included on each entry ONLY when the caller is a platform admin.
    Organizers see whether / how many times / by whom / when - never why (owner rule).
    FE consumer: the Lookup section on the organizer "Blacklists" page
    (app/(organizer)/organizer/blacklists/page.tsx via organizersApi.lookupBlacklists).
    """
    user, err = _authenticate(request)
    if err:
        return err

    # ── gate: any ACTIVE org member (organizer) or platform admin; plain players 403 ──
    is_admin = is_platform_org_admin(user)
    if not is_admin and not OrganizationMember.objects.filter(
        user=user, status="active"
    ).exists():
        return Response(
            {"message": "Only organizers and AFC admins can look up blacklists."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── target: exactly one of team_id / user_id ──
    team_id = request.GET.get("team_id")
    user_id = request.GET.get("user_id")
    if bool(team_id) == bool(user_id):  # both set or both missing
        return Response(
            {"message": "Provide exactly one of team_id or user_id."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── optional date window (each bound independently optional = open-ended) ──
    raw_start = request.GET.get("start")
    raw_end = request.GET.get("end")
    window_start = _parse_day(raw_start, end_of_day=False)
    window_end = _parse_day(raw_end, end_of_day=True)
    if (raw_start and window_start is None) or (raw_end and window_end is None):
        return Response(
            {"message": "start and end must be valid ISO dates (YYYY-MM-DD)."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    now = timezone.now()
    target = None            # echoed back so the FE can label the result card
    row_active_by_bl = {}    # player lookups: blacklist_id -> their snapshot row's is_active

    if team_id:
        # ── TEAM lookup: every blacklist row that names this team, across all orgs ──
        team = Team.objects.filter(pk=team_id).first()
        if not team:
            return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
        target = {"type": "team", "team_id": team.team_id, "team_name": team.team_name}
        qs = OrganizerBlacklist.objects.filter(team=team)
    else:
        # ── PLAYER lookup: every blacklist the player has a snapshot row under (the
        # follows-the-player rule - see the docstring for the full counting semantics). We
        # also remember each row's is_active so active_count + per-entry status can honour an
        # individually-lifted player. ──
        target_user = User.objects.filter(pk=user_id).first()
        if not target_user:
            return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        target = {"type": "player", "user_id": target_user.user_id,
                  "username": target_user.username}
        row_active_by_bl = dict(
            OrganizerBlacklistPlayer.objects.filter(user=target_user)
            .values_list("blacklist_id", "is_active")
        )
        qs = OrganizerBlacklist.objects.filter(id__in=row_active_by_bl.keys())

    # ── apply the window on start_date ("blacklists that started in this time frame") ──
    if window_start:
        qs = qs.filter(start_date__gte=window_start)
    if window_end:
        qs = qs.filter(start_date__lte=window_end)

    # select_related the FKs the serializer reads; newest first per the brief.
    qs = qs.select_related("organization", "team").order_by("-start_date")

    # ── active_count: rows in the window that block RIGHT NOW ──
    # Team: a currently-active blacklist row. Player: additionally their OWN snapshot row must
    # still be active (an individually-lifted player is not blocked even while team-mates are).
    active_qs = qs.filter(status="active", end_date__gt=now)
    if target["type"] == "player":
        active_count = sum(
            1 for bl_id in active_qs.values_list("id", flat=True)
            if row_active_by_bl.get(bl_id, True)
        )
    else:
        active_count = active_qs.count()

    # ── page + serialize (reason ONLY for platform admins - the owner privacy rule) ──
    page, total_count, has_more = _paginate(request, qs)
    entries = []
    for bl in page:
        entry = {
            "id": bl.id,
            "organization_name": bl.organization.name if bl.organization else None,
            "organization_slug": bl.organization.slug if bl.organization else None,
            # team context rides along (essential on player lookups: WHICH team they were
            # snapshotted on; harmless redundancy on team lookups).
            "team_id": bl.team_id,
            "team_name": bl.team.team_name if bl.team else None,
            "start_date": bl.start_date.isoformat() if bl.start_date else None,
            "end_date": bl.end_date.isoformat() if bl.end_date else None,
            "status": _effective_status(
                bl, now,
                player_row_active=(
                    row_active_by_bl.get(bl.id) if target["type"] == "player" else None
                ),
            ),
        }
        if is_admin:
            entry["reason"] = bl.reason
        entries.append(entry)

    return Response(
        {
            "target": target,
            "window": {
                "start": window_start.isoformat() if window_start else None,
                "end": window_end.isoformat() if window_end else None,
            },
            "total_count": total_count,
            "active_count": active_count,
            "entries": entries,
            "has_more": has_more,
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  GET admin/blacklists/  - the AFC dashboard feed (everything, reasons included)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_blacklists(request):
    """The AFC admin dashboard feed: every OrganizerBlacklist row across all organizations,
    reasons included, plus the aggregates the dashboard stat cards need.

    Auth:  Bearer; platform admins only (is_platform_org_admin - head_admin/organizer_admin).
    Query: ?search=   - matches team name + org name, punctuation/leet-insensitively via the
                        shared utils.search_utils fold (the same widening the typeaheads use)
           &status=   - active | expired | lifted, judged by EFFECTIVE status: "active" means
                        blocking right now; "expired" includes lapsed rows a sweep has not
                        relabelled yet (mirrors enforcement, see _effective_status)
           &start= &end= - YYYY-MM-DD window on each row's start_date (both optional)
           &limit/&offset - standard paging (default 25, max 100).
    Response: 200 {
        results: [{id, organization_name, organization_slug, team_name, team_id, reason,
                   status: active|expired|lifted, start_date, end_date, created_at,
                   lifted_by_username, lifted_at,    # see note below
                   player_snapshot_count}],          # newest first (created_at)
        total_count, has_more,
        aggregates: {     # computed over the FILTERED set, so the stat cards answer
                          # "in this window / for this search" - not just all-time
            total, active,
            by_organization:        top 10 [{organization_name, organization_slug, count}],
            most_blacklisted_teams: top 10 [{team_name, team_id, count}] } }

    LIFTED-INFO NOTE: the model does not stamp who lifted a blacklist. When a lift came from
    an APPROVED team-scope BlacklistLiftRequest we surface its decided_by/decided_at as
    lifted_by_username/lifted_at (latest approval wins). A direct organizer lift leaves no
    such record, so those rows carry nulls - the status pill still reads "lifted".
    FE consumer: the AFC admin Blacklists dashboard (app/(a)/a/blacklists/page.tsx via
    organizersApi.adminListBlacklists).
    """
    user, err = _authenticate(request)
    if err:
        return err

    # ── platform-admin gate: this is the oversight surface, reasons included ──
    if not is_platform_org_admin(user):
        return Response(
            {"message": "You do not have permission to view the blacklist dashboard."},
            status=status.HTTP_403_FORBIDDEN,
        )

    now = timezone.now()

    # ── base queryset + filters (all narrowing happens before aggregates so the stat
    # cards reflect exactly what the table shows) ──
    qs = OrganizerBlacklist.objects.all()

    # search: team name + org name, plain icontains OR'd with the punctuation/leet-insensitive
    # fold from utils.search_utils (normalized_column on the columns, separator_stripped on the
    # query) - the same only-ever-widens pattern afc_team.views.search_teams uses.
    search = (request.GET.get("search") or "").strip()
    if search:
        from utils.search_utils import normalized_column, separator_stripped

        cond = Q(team__team_name__icontains=search) | Q(organization__name__icontains=search)
        norm_q = separator_stripped(search)
        qs = qs.annotate(
            _norm_team=normalized_column("team__team_name"),
            _norm_org=normalized_column("organization__name"),
        )
        if norm_q:
            cond |= Q(_norm_team__icontains=norm_q) | Q(_norm_org__icontains=norm_q)
        qs = qs.filter(cond)

    # status filter by EFFECTIVE status (must mirror _effective_status so the pill the admin
    # filters by and the pill the row shows always agree):
    #   active  -> stored "active" AND end_date still in the future (blocking right now)
    #   expired -> stored "expired" OR a lapsed "active" row no sweep has relabelled yet
    #   lifted  -> stored "lifted"
    status_filter = request.GET.get("status")
    if status_filter == "active":
        qs = qs.filter(status="active", end_date__gt=now)
    elif status_filter == "expired":
        qs = qs.filter(Q(status="expired") | Q(status="active", end_date__lte=now))
    elif status_filter == "lifted":
        qs = qs.filter(status="lifted")

    # optional date window on start_date (same semantics as the organizer lookup).
    raw_start = request.GET.get("start")
    raw_end = request.GET.get("end")
    window_start = _parse_day(raw_start, end_of_day=False)
    window_end = _parse_day(raw_end, end_of_day=True)
    if (raw_start and window_start is None) or (raw_end and window_end is None):
        return Response(
            {"message": "start and end must be valid ISO dates (YYYY-MM-DD)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if window_start:
        qs = qs.filter(start_date__gte=window_start)
    if window_end:
        qs = qs.filter(start_date__lte=window_end)

    # ── aggregates over the filtered set ──
    # Re-anchor through pk__in so the GROUP BY aggregates are never entangled with the search
    # annotations above (values().annotate() on an already-annotated qs is fragile).
    agg_base = OrganizerBlacklist.objects.filter(pk__in=qs.values("pk"))
    aggregates = {
        "total": agg_base.count(),
        # "active" here = blocking right now (status active + end_date in the future),
        # matching both _effective_status and registration-time enforcement.
        "active": agg_base.filter(status="active", end_date__gt=now).count(),
        "by_organization": [
            {
                "organization_name": row["organization__name"],
                "organization_slug": row["organization__slug"],
                "count": row["count"],
            }
            for row in agg_base.values("organization__name", "organization__slug")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        ],
        "most_blacklisted_teams": [
            {
                "team_name": row["team__team_name"],
                "team_id": row["team_id"],
                "count": row["count"],
            }
            for row in agg_base.values("team__team_name", "team_id")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        ],
    }

    # ── page the table rows (newest first), pulling FKs + the snapshot size in one go ──
    # Count("players", distinct=True) guards against join-multiplication from the search joins.
    page_qs = (
        qs.select_related("organization", "team")
        .annotate(player_snapshot_count=Count("players", distinct=True))
        .order_by("-created_at")
    )
    page, total_count, has_more = _paginate(request, page_qs)
    page = list(page)

    # ── lifted info: map lifted rows to their approving team-scope lift request (if any) ──
    # One query for the whole page (no N+1). Ordered by decided_at ascending so the LATEST
    # approval wins the dict insert. Direct organizer lifts have no request -> nulls.
    lifted_ids = [bl.id for bl in page if bl.status == "lifted"]
    lift_info = {}
    if lifted_ids:
        for req in (
            BlacklistLiftRequest.objects.filter(
                blacklist_id__in=lifted_ids, scope="team", status="approved"
            )
            .select_related("decided_by")
            .order_by("decided_at")
        ):
            lift_info[req.blacklist_id] = req

    results = []
    for bl in page:
        lift_req = lift_info.get(bl.id)
        results.append({
            "id": bl.id,
            "organization_name": bl.organization.name if bl.organization else None,
            "organization_slug": bl.organization.slug if bl.organization else None,
            "team_name": bl.team.team_name if bl.team else None,
            "team_id": bl.team_id,
            "reason": bl.reason,  # admins see everything, including why (owner rule)
            "status": _effective_status(bl, now),
            "start_date": bl.start_date.isoformat() if bl.start_date else None,
            "end_date": bl.end_date.isoformat() if bl.end_date else None,
            "created_at": bl.created_at.isoformat() if bl.created_at else None,
            "lifted_by_username": (
                lift_req.decided_by.username if lift_req and lift_req.decided_by else None
            ),
            "lifted_at": (
                lift_req.decided_at.isoformat() if lift_req and lift_req.decided_at else None
            ),
            "player_snapshot_count": bl.player_snapshot_count,
        })

    return Response(
        {
            "results": results,
            "total_count": total_count,
            "has_more": has_more,
            "aggregates": aggregates,
        },
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  GET admin/blacklist-counts/  - bulk per-row counts for the admin tables
# ──────────────────────────────────────────────────────────────────────────────
# Owner ask 2026-06-13: "add a blacklists tab and column under both teams & players
# page." The COLUMN needs a count per visible table row; doing one blacklist_lookup
# call per row would be N requests per page, so this endpoint answers a whole page
# of ids in ONE call.
@api_view(["GET"])
def admin_blacklist_counts(request):
    """Bulk blacklist counts for a set of teams OR a set of players - one call returns the
    per-row numbers the admin Teams & Players tables render in their "Blacklists" column
    ("N (M active)"), instead of one blacklist-lookup request per row.

    Counting semantics deliberately mirror blacklist_lookup (§1) so the column and the
    lookup can never disagree:
      - TEAM   total  = OrganizerBlacklist rows naming the team (any status, all orgs);
               active = of those, the ones blocking RIGHT NOW (status "active" AND
                        end_date still in the future - live expiry, no sweep needed,
                        same rule as is_currently_active()).
      - PLAYER total  = the player's OrganizerBlacklistPlayer snapshot rows (the
                        follows-the-player rule; unique_together (blacklist, user)
                        guarantees one row per blacklist, so this equals the number of
                        distinct blacklists that ever bound them - identical to the
                        lookup's total_count);
               active = snapshot rows that still block: their OWN row is_active (an
                        individually-lifted player is not blocked) AND the parent
                        blacklist is blocking right now (same live-expiry rule).

    Auth:  Bearer; platform admins only (is_platform_org_admin - the same gate as the
           dashboard feed above: this decorates admin tables, it is not organizer data).
    Query: ?team_ids=1,2,3 OR ?user_ids=4,5,6 (exactly one, 400 otherwise; comma-separated
           integers, max 200 per call - callers only ever send one table page).
    Response: 200 { counts: { "<id>": { total, active } } } keyed by the REQUESTED id
           (stringified - JSON object keys are strings). Ids with no blacklist history
           come back as { total: 0, active: 0 } so the client needs no missing-key logic.
    FE consumers: the "Blacklists" column on the admin Teams & Players tables
    (app/(a)/a/_components/TeamsAdminContent.tsx + PlayersAdminContent.tsx, both through
    the shared useBlacklistCounts hook, via organizersApi.adminBlacklistCounts).
    """
    user, err = _authenticate(request)
    if err:
        return err

    # ── platform-admin gate (same rule as admin_list_blacklists above) ──
    if not is_platform_org_admin(user):
        return Response(
            {"message": "You do not have permission to view blacklist counts."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── target list: exactly one of team_ids / user_ids ──
    raw_team_ids = request.GET.get("team_ids")
    raw_user_ids = request.GET.get("user_ids")
    if bool(raw_team_ids) == bool(raw_user_ids):  # both set or both missing
        return Response(
            {"message": "Provide exactly one of team_ids or user_ids."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Parse "1,2,3" -> [1, 2, 3]. A malformed token is a 400 (like the malformed window
    # dates in §1), not a silent skip - the caller built the list from its own rows, so
    # junk means a caller bug we want surfaced.
    try:
        ids = [int(tok) for tok in str(raw_team_ids or raw_user_ids).split(",") if tok.strip()]
    except (TypeError, ValueError):
        return Response(
            {"message": "team_ids / user_ids must be a comma-separated list of integers."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not ids:
        return Response(
            {"message": "team_ids / user_ids must contain at least one id."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(ids) > 200:  # callers send one table page; anything bigger is a misuse
        return Response(
            {"message": "At most 200 ids per call."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    now = timezone.now()
    # Seed every requested id with zeros so the response always covers the full request
    # (ids never blacklisted simply stay at zero - no missing keys client-side).
    counts = {str(i): {"total": 0, "active": 0} for i in ids}

    if raw_team_ids:
        # ── TEAM counts: one GROUP BY over the blacklist rows for the whole page ──
        rows = (
            OrganizerBlacklist.objects.filter(team_id__in=ids)
            .values("team_id")
            .annotate(
                total=Count("id"),
                active=Count("id", filter=Q(status="active", end_date__gt=now)),
            )
        )
        for row in rows:
            counts[str(row["team_id"])] = {"total": row["total"], "active": row["active"]}
    else:
        # ── PLAYER counts: one GROUP BY over the snapshot rows (follows-the-player) ──
        rows = (
            OrganizerBlacklistPlayer.objects.filter(user_id__in=ids)
            .values("user_id")
            .annotate(
                total=Count("id"),
                active=Count(
                    "id",
                    filter=Q(
                        is_active=True,
                        blacklist__status="active",
                        blacklist__end_date__gt=now,
                    ),
                ),
            )
        )
        for row in rows:
            counts[str(row["user_id"])] = {"total": row["total"], "active": row["active"]}

    return Response({"counts": counts}, status=status.HTTP_200_OK)
