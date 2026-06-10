# afc_organizers/views_blacklist.py
# ──────────────────────────────────────────────────────────────────────────────
# ORGANIZER BLACKLIST endpoints - the organizer-side surface for blacklisting teams and the
# affected-party surface for requesting a lift.
#
# Two audiences live here, gated differently:
#   • ORGANIZER staff (org_can(can_manage_registrations) on the relevant org, plus the
#     AFC-admin/owner bypass baked into permissions.org_can): create a blacklist, list the
#     org's blacklists, lift one early, list incoming lift requests, decide a lift request.
#   • The AFFECTED PARTY (no org membership): a team manager (captain/owner/coach/manager of
#     the blacklisted team) or an affected snapshot player requests a lift of a blacklist that
#     targets them.
#
# Convention note (why this file looks the way it does): it deliberately mirrors the original
# hand in afc_team/views.py and the sibling afc_organizers/views_reports.py - function-based
# @api_view views (one job each), an inline Bearer auth handshake via afc_auth.views.validate_token,
# inline dict serialization (no serializers.py), and Response({...}, status=...) for every return.
#
# How this connects to the rest of the system:
#   - Models: OrganizerBlacklist / OrganizerBlacklistPlayer / BlacklistLiftRequest (models.py).
#   - Permission: afc_organizers.permissions.org_can(..., "can_manage_registrations", org) for
#     every organizer action; the team-role helper afc_team.views._can_manage_roster plus a
#     captain/manager check for the lift-request side.
#   - Enforcement: creating a blacklist snapshots TeamMembers; the snapshot is what
#     afc_organizers.blacklist.organizer_blacklist_block reads at registration time inside
#     afc_tournament_and_scrims.views.register_for_event (the follows-the-player rule).
#   - Routes: mounted in afc_organizers/urls.py (owned by the URL map, not this file).
#   - Frontend consumers: the organizer dashboard "Blacklists" page (create/list/lift +
#     lift-requests approve/deny) and the team surface "Request lift" actions (see the design doc).
# Full spec: WEBSITE/tasks/organizer-blacklist-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from django.utils import timezone
from datetime import datetime, time, timedelta

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token lives in afc_auth.views - imported the SAME way afc_team/views.py and the
# sibling organizer views import it. Returns the User for a valid, non-expired session token.
from afc_auth.views import validate_token

from afc_team.models import Team, TeamMembers

from afc_organizers.models import (
    Organization,
    OrganizerBlacklist,
    OrganizerBlacklistPlayer,
    BlacklistLiftRequest,
)
from afc_organizers.permissions import org_can


# ──────────────────────────────────────────────────────────────────────────────
# §0  Shared helpers (DRY - identical handshake / paging / role checks across views)
# ──────────────────────────────────────────────────────────────────────────────
def _authenticate(request):
    """Resolve the caller from the Authorization header exactly like the original hand: read the
    raw header, require the "Bearer " scheme, strip it, hand the token to validate_token. Returns
    (user, None) on success or (None, error_response) so each view can bail on the Response:
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
    (page, total_count, has_more) so the frontend can page without ever loading the whole table.
    Junk values fall back to the default rather than 500-ing the request."""
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


def _is_team_manager(user, team):
    """True if `user` may act for `team` on the lift-request surface: the team OWNER or a member
    whose management_role is team_captain / coach / manager. This is the team-side floor for a
    team-scope lift request (a member or analyst cannot request a lift). Bound to THIS team so it
    can never authorize acting for a different team. Mirrors afc_team's roster-permission shape
    while widening it (the spec wants captain/owner/coach/manager) for the lift surface."""
    if team.team_owner_id == user.user_id:
        return True
    return TeamMembers.objects.filter(
        team=team,
        member=user,
        management_role__in=("team_captain", "coach", "manager"),
    ).exists()


def _serialize_blacklist(blacklist, include_players=True):
    """Inline dict for one OrganizerBlacklist. When include_players, nests the snapshot rows
    (id/user_id/username/is_active) so the organizer dashboard sees exactly who is blocked.
    Image/FK fields follow the codebase contract (value or None)."""
    data = {
        "id": blacklist.id,
        "organization_id": blacklist.organization_id,
        "team_id": blacklist.team_id,
        "team_name": blacklist.team.team_name if blacklist.team else None,
        "reason": blacklist.reason,
        "status": blacklist.status,
        "is_currently_active": blacklist.is_currently_active(),
        "start_date": blacklist.start_date.isoformat() if blacklist.start_date else None,
        "end_date": blacklist.end_date.isoformat() if blacklist.end_date else None,
        "created_by_username": blacklist.created_by.username if blacklist.created_by else None,
        "created_at": blacklist.created_at.isoformat() if blacklist.created_at else None,
    }
    if include_players:
        data["players"] = [
            {
                "id": p.id,
                "user_id": p.user_id,
                "username": p.user.username if p.user else None,
                "is_active": p.is_active,
            }
            # prefetched via .players in the list view (no N+1 per blacklist).
            for p in blacklist.players.all()
        ]
    return data


def _serialize_lift_request(req):
    """Inline dict for one BlacklistLiftRequest, including a little blacklist context (team) so
    the organizer can triage without a second fetch."""
    return {
        "id": req.id,
        "blacklist_id": req.blacklist_id,
        "organization_id": req.blacklist.organization_id if req.blacklist else None,
        "team_id": req.blacklist.team_id if req.blacklist else None,
        "team_name": req.blacklist.team.team_name if req.blacklist and req.blacklist.team else None,
        "scope": req.scope,
        "target_user_id": req.target_user_id,
        "target_username": req.target_user.username if req.target_user else None,
        "requested_by_username": req.requested_by.username if req.requested_by else None,
        "reason": req.reason,
        "status": req.status,
        "decided_by_username": req.decided_by.username if req.decided_by else None,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "created_at": req.created_at.isoformat() if req.created_at else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# §0b  GET blacklists/mine/  - the AFFECTED-PARTY discovery view (NO org gate)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def my_blacklists(request):
    """List the ACTIVE blacklists that affect the CALLING user, so a team or player can discover
    what is blocking them and then request a lift.

    Why this exists: every other list surface (list_blacklists) is org-gated on
    can_manage_registrations, which is exactly backwards for the affected party. A blacklisted
    team or a snapshotted player has no organizer permissions, so without this endpoint they could
    never see their own blacklist to act on it. This view is therefore NOT org-gated; it is scoped
    to "blacklists that affect ME", resolved two independent ways and deduped:
      • TEAM-manager view  - blacklists whose `team` is a team the caller manages (owner /
        captain / coach / manager, via the SAME _is_team_manager helper request_lift uses).
      • PLAYER view        - blacklists where the caller has an active OrganizerBlacklistPlayer
        (is_active=True). This covers the follows-the-player case: a snapshotted player keeps
        seeing the blacklist even after leaving the team (they are no longer a manager, but the
        per-player row still points at them).
    "Active" means status="active" AND end_date in the future (mirrors is_currently_active), so
    expired or lifted blacklists never appear.

    Auth: Bearer (any logged-in user); deliberately NO org-permission gate (this is the affected
          party's own view, not an organizer surface).
    Query: optional ?team_id= to narrow to a single team (plus the standard ?limit/?offset paging).
    Response: 200 {results, total_count, has_more}. Each result row:
          {id, team_id, team_name, organization_id, organization_name, reason, start_date,
           end_date, status, can_request_team_lift, can_request_self_lift, my_pending_request}.
        - can_request_team_lift: true if the caller manages this blacklist's team (eligible to
          raise a scope="team" lift request).
        - can_request_self_lift: true if the caller is an ACTIVE snapshot player on it (eligible
          to raise a scope="player" lift request for themselves).
        - my_pending_request: the caller's existing PENDING BlacklistLiftRequest for this
          blacklist serialized (or null), so the UI can disable a duplicate re-request.
    FE consumer: the team page RequestBlacklistLift component (shows "Request lift" for managers
                 and "Request lift for myself" for affected players, disabled while pending).
    """
    user, err = _authenticate(request)
    if err:
        return err

    now = timezone.now()

    # Base queryset: only blacklists that are currently active. order + select_related/prefetch
    # so the serializer below never triggers an N+1 (team, organization, players, my requests).
    active_qs = (
        OrganizerBlacklist.objects.filter(status="active", end_date__gt=now)
        .select_related("team", "organization", "created_by")
        .prefetch_related("players")
    )

    # Optional ?team_id= filter, applied before we split into the two affected-party views.
    team_id = request.GET.get("team_id")
    if team_id:
        active_qs = active_qs.filter(team_id=team_id)

    # ── (1) TEAM-manager view: blacklists on a team the caller manages ──
    # We resolve the caller's managed team ids the same way _is_team_manager judges membership:
    # teams they OWN, plus teams where their management_role is captain/coach/manager.
    managed_team_ids = set(
        Team.objects.filter(team_owner=user).values_list("team_id", flat=True)
    )
    managed_team_ids |= set(
        TeamMembers.objects.filter(
            member=user, management_role__in=("team_captain", "coach", "manager")
        ).values_list("team_id", flat=True)
    )
    manager_blacklists = active_qs.filter(team_id__in=managed_team_ids) if managed_team_ids \
        else OrganizerBlacklist.objects.none()

    # ── (2) PLAYER view: blacklists where the caller is an ACTIVE snapshot player ──
    # Queried via the per-player rows so it follows the person even after they leave the team.
    player_blacklist_ids = set(
        OrganizerBlacklistPlayer.objects.filter(
            user=user, is_active=True,
            blacklist__status="active", blacklist__end_date__gt=now,
        ).values_list("blacklist_id", flat=True)
    )
    player_blacklists = active_qs.filter(id__in=player_blacklist_ids) if player_blacklist_ids \
        else OrganizerBlacklist.objects.none()

    # ── dedupe: a manager who is also a snapshot player must yield ONE row ──
    # Collect distinct blacklist objects keyed by id, preserving newest-first ordering.
    seen = {}
    for blacklist in list(manager_blacklists) + list(player_blacklists):
        seen.setdefault(blacklist.id, blacklist)
    combined = sorted(seen.values(), key=lambda b: b.created_at or now, reverse=True)

    # Capability inputs computed once for the whole page (no per-row query):
    #   • which of these blacklists name a team the caller manages (team-lift eligibility);
    #   • the caller's PENDING lift requests for these blacklists (UI disable + payload echo).
    combined_ids = [b.id for b in combined]
    pending_by_blacklist = {
        req.blacklist_id: req
        for req in BlacklistLiftRequest.objects.filter(
            blacklist_id__in=combined_ids, requested_by=user, status="pending"
        ).select_related("blacklist__team", "target_user", "requested_by", "decided_by")
    }

    # Build each affected-party row inline (this surface needs different keys than the organizer
    # _serialize_blacklist - capability flags + the caller's pending request - so it is its own
    # serializer rather than a flag on the shared one).
    def _serialize_mine(blacklist):
        can_request_team_lift = blacklist.team_id in managed_team_ids
        can_request_self_lift = blacklist.id in player_blacklist_ids
        pending = pending_by_blacklist.get(blacklist.id)
        return {
            "id": blacklist.id,
            "team_id": blacklist.team_id,
            "team_name": blacklist.team.team_name if blacklist.team else None,
            "organization_id": blacklist.organization_id,
            "organization_name": blacklist.organization.name if blacklist.organization else None,
            "reason": blacklist.reason,
            "start_date": blacklist.start_date.isoformat() if blacklist.start_date else None,
            "end_date": blacklist.end_date.isoformat() if blacklist.end_date else None,
            "status": blacklist.status,
            "can_request_team_lift": can_request_team_lift,
            "can_request_self_lift": can_request_self_lift,
            "my_pending_request": _serialize_lift_request(pending) if pending else None,
        }

    # Page the already-materialized, deduped list (small per user) with the SAME limit/offset
    # contract as the other list endpoints. _paginate expects a queryset (.count()), but `combined`
    # is a Python list (deduped across two querysets), so we apply the identical clamp/slice inline
    # rather than changing the shared helper.
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
    total_count = len(combined)
    page = combined[offset:offset + limit]
    has_more = (offset + limit) < total_count
    results = [_serialize_mine(b) for b in page]
    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §1  blacklists/  - ONE route, two verbs (mirrors views_design.design_requests).
# ──────────────────────────────────────────────────────────────────────────────
# Per the spec the create + list surfaces share the path "blacklists/":
#   POST blacklists/                      -> create a blacklist (+ snapshot the team)
#   GET  blacklists/?organization_id=...  -> list the org's blacklists (+ nested players)
# We follow the codebase idiom (views_design): a single @api_view branches on request.method
# (DRF 405s any other verb), delegating to the focused _create_blacklist / _list_blacklists
# helpers below so each concern stays readable top-to-bottom.
@api_view(["POST", "GET"])
def blacklists(request):
    """Verb dispatcher for blacklists/. POST -> create, GET -> list. See the helpers below for
    the per-verb request/response shape, auth, and FE consumer docs."""
    if request.method == "POST":
        return _create_blacklist(request)
    return _list_blacklists(request)


def _create_blacklist(request):
    """Create a blacklist of a team by an organization over a calendar date RANGE, snapshotting the
    team's CURRENT members into OrganizerBlacklistPlayer rows (this snapshot is what enforcement
    reads, so the block follows the players even after they leave the team).

    Request (JSON):
      {organization_id, team_id,
       start_date,        # ISO YYYY-MM-DD, the chosen start day. Optional: defaults to now.
                          #   Parsed to that day's START (00:00).
       end_date,          # ISO YYYY-MM-DD, the chosen end day. REQUIRED (primary path).
                          #   Parsed to that day's END (23:59:59) so the whole selected day counts.
       reason?}
      Backward-compatible fallback: if BOTH start_date and end_date are omitted, an old-style
      {duration_days} (positive int) is accepted and end_date = now + duration_days.
    Validation (all 400 with a clear message):
      - end_date is required (unless the duration_days fallback is used);
      - start_date / end_date must be valid ISO YYYY-MM-DD strings;
      - end_date must be strictly AFTER start_date;
      - end_date must be in the FUTURE.
    Auth: Bearer; gated by org_can(user, "can_manage_registrations", org) (owner/AFC-admin bypass
          included). 403 for anyone else.
    Response: 201 {message, blacklist: <serialized + nested players>}.
    FE consumer: organizer dashboard "Blacklists" page (create form, calendar date-range picker).
    """
    user, err = _authenticate(request)
    if err:
        return err

    # ── resolve + validate inputs ──
    organization_id = request.data.get("organization_id")
    team_id = request.data.get("team_id")
    if not organization_id or not team_id:
        return Response(
            {"message": "organization_id and team_id are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    org = Organization.objects.filter(pk=organization_id).first()
    if not org:
        return Response({"message": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    team = Team.objects.filter(pk=team_id).first()
    if not team:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # ── permission gate: only someone who can manage registrations for THIS org ──
    if not org_can(user, "can_manage_registrations", org):
        return Response(
            {"message": "You do not have permission to manage blacklists for this organization."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── window: a calendar date RANGE (primary), or a duration_days fallback (legacy) ──
    # The organizer picks start_date + end_date on a calendar. We parse the ISO YYYY-MM-DD strings
    # and store start_date at day-start, end_date at day-END (so the whole selected end day is
    # covered). For old callers that send neither date, we fall back to duration_days.
    raw_start = request.data.get("start_date")
    raw_end = request.data.get("end_date")
    now = timezone.now()

    # _parse_day turns an ISO YYYY-MM-DD string into a tz-aware datetime at the given time-of-day,
    # or returns None when the string is missing/malformed (the caller maps None to a 400). We
    # combine the parsed date with day-start / day-end and make it timezone-aware so comparisons
    # against timezone.now() are valid (USE_TZ project).
    def _parse_day(value, end_of_day):
        if not value:
            return None
        try:
            parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except (TypeError, ValueError, AttributeError):
            return None
        tod = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        return timezone.make_aware(datetime.combine(parsed, tod))

    if raw_start is not None or raw_end is not None:
        # ── PRIMARY PATH: calendar date range ──
        # end_date is required on this path.
        if not raw_end:
            return Response(
                {"message": "end_date is required (ISO YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # start_date is optional; default to "now" at day-start when omitted.
        start_date = _parse_day(raw_start, end_of_day=False) if raw_start else now
        end_date = _parse_day(raw_end, end_of_day=True)
        if start_date is None or end_date is None:
            return Response(
                {"message": "start_date and end_date must be valid ISO dates (YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # end must be strictly after start (a zero/negative-length window is meaningless).
        if end_date <= start_date:
            return Response(
                {"message": "end_date must be after start_date."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # end must be in the future (cannot create an already-expired blacklist).
        if end_date <= now:
            return Response(
                {"message": "end_date must be in the future."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        # ── FALLBACK PATH: legacy duration_days (positive int), kept so old callers still work ──
        try:
            duration_days = int(request.data.get("duration_days"))
        except (TypeError, ValueError):
            return Response(
                {"message": "duration_days must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if duration_days <= 0:
            return Response(
                {"message": "duration_days must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start_date = now
        end_date = now + timedelta(days=duration_days)

    reason = (request.data.get("reason") or "").strip()

    # ── create the blacklist over [start_date, end_date] + snapshot the roster ──
    blacklist = OrganizerBlacklist.objects.create(
        organization=org,
        team=team,
        reason=reason,
        start_date=start_date,
        end_date=end_date,
        created_by=user,
        status="active",
    )

    # Snapshot the team's CURRENT members. This is the heart of the feature: enforcement keys off
    # these rows per (organization, player), so the block tracks each person wherever they go.
    member_user_ids = TeamMembers.objects.filter(team=team).values_list("member_id", flat=True)
    OrganizerBlacklistPlayer.objects.bulk_create([
        OrganizerBlacklistPlayer(blacklist=blacklist, user_id=uid, is_active=True)
        for uid in member_user_ids
    ])

    return Response(
        {"message": "Team blacklisted.", "blacklist": _serialize_blacklist(blacklist)},
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  GET blacklists/  - organizer lists their org's blacklists (+ snapshot players)
# ──────────────────────────────────────────────────────────────────────────────
def _list_blacklists(request):
    """List an organization's blacklists (newest first), each with its nested snapshot players.

    Query: ?organization_id= (required) & ?status= (optional: active/lifted/expired) & paging.
    Auth: Bearer; gated by org_can(user, "can_manage_registrations", org).
    Response: 200 {results, total_count, has_more}.
    FE consumer: organizer dashboard "Blacklists" page (list).
    """
    user, err = _authenticate(request)
    if err:
        return err

    organization_id = request.GET.get("organization_id")
    if not organization_id:
        return Response(
            {"message": "organization_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    org = Organization.objects.filter(pk=organization_id).first()
    if not org:
        return Response({"message": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not org_can(user, "can_manage_registrations", org):
        return Response(
            {"message": "You do not have permission to view blacklists for this organization."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # select_related the FK rows the serializer touches + prefetch players (and their user) so a
    # page of blacklists serializes in a couple of queries, never N+1 per row.
    qs = (
        OrganizerBlacklist.objects.filter(organization=org)
        .select_related("team", "created_by")
        .prefetch_related("players__user")
        .order_by("-created_at")
    )
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    page, total_count, has_more = _paginate(request, qs)
    results = [_serialize_blacklist(b) for b in page]
    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  POST blacklists/<id>/lift/  - organizer lifts a blacklist early
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def lift_blacklist(request, blacklist_id):
    """Lift a blacklist early: set status="lifted" and deactivate ALL its player snapshot rows
    (is_active=False), so the team and everyone snapshotted can register again immediately.

    Auth: Bearer; gated by org_can(user, "can_manage_registrations", blacklist.organization).
    Response: 200 {message, blacklist}.
    FE consumer: organizer dashboard "Blacklists" page ("Lift" button).
    """
    user, err = _authenticate(request)
    if err:
        return err

    blacklist = (
        OrganizerBlacklist.objects.select_related("organization", "team")
        .filter(pk=blacklist_id)
        .first()
    )
    if not blacklist:
        return Response({"message": "Blacklist not found."}, status=status.HTTP_404_NOT_FOUND)

    if not org_can(user, "can_manage_registrations", blacklist.organization):
        return Response(
            {"message": "You do not have permission to lift this blacklist."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Lift the whole thing: status + every player row in one update each (no per-row loop).
    blacklist.status = "lifted"
    blacklist.save(update_fields=["status"])
    blacklist.players.update(is_active=False)

    return Response(
        {"message": "Blacklist lifted.", "blacklist": _serialize_blacklist(blacklist)},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §4  POST blacklists/<id>/request-lift/  - affected party requests a lift
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def request_lift(request, blacklist_id):
    """Raise a pending lift request against a blacklist. Two scopes:
      • scope="team"   - asks for the whole blacklist to be lifted. Requester MUST be a manager
                         (captain/owner/coach/manager) of blacklist.team.
      • scope="player" - asks for one person to be unblocked. Requester must be that target user
                         themselves OR a team manager acting on their behalf; target_user_id is
                         required and must be a snapshot player on this blacklist.
    Rejects (400) a duplicate when a pending request already exists for the same
    (blacklist, scope, target_user).

    Request (JSON): {scope:"team"|"player", target_user_id?, reason?}.
    Auth: Bearer (the affected party - NOT org-gated).
    Response: 201 {message, lift_request}.
    FE consumer: team surface "Request lift" / player "Request lift for myself" actions.
    """
    user, err = _authenticate(request)
    if err:
        return err

    blacklist = (
        OrganizerBlacklist.objects.select_related("team")
        .filter(pk=blacklist_id)
        .first()
    )
    if not blacklist:
        return Response({"message": "Blacklist not found."}, status=status.HTTP_404_NOT_FOUND)

    scope = request.data.get("scope")
    if scope not in ("team", "player"):
        return Response(
            {"message": "scope must be 'team' or 'player'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reason = (request.data.get("reason") or "").strip()
    target_user = None

    if scope == "team":
        # Team-scope: only a manager of the blacklisted team may ask for a full lift.
        if not _is_team_manager(user, blacklist.team):
            return Response(
                {"message": "Only the team owner, captain, coach, or manager can request a team lift."},
                status=status.HTTP_403_FORBIDDEN,
            )
    else:  # scope == "player"
        target_user_id = request.data.get("target_user_id")
        if not target_user_id:
            return Response(
                {"message": "target_user_id is required for a player-scope lift request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # The target must actually be a snapshot player on this blacklist - you cannot request a
        # lift for someone who was never blocked here.
        player_row = OrganizerBlacklistPlayer.objects.filter(
            blacklist=blacklist, user_id=target_user_id
        ).select_related("user").first()
        if not player_row:
            return Response(
                {"message": "That player is not on this blacklist."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target_user = player_row.user
        # Permission: the player themselves, OR a manager of the team acting on their behalf.
        is_self = (user.user_id == target_user.user_id)
        if not (is_self or _is_team_manager(user, blacklist.team)):
            return Response(
                {"message": "You can only request a lift for yourself, or a team manager can request it for you."},
                status=status.HTTP_403_FORBIDDEN,
            )

    # ── duplicate guard: at most one PENDING request per (blacklist, scope, target_user) ──
    duplicate = BlacklistLiftRequest.objects.filter(
        blacklist=blacklist, scope=scope, target_user=target_user, status="pending"
    ).exists()
    if duplicate:
        return Response(
            {"message": "A pending lift request already exists for this scope."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    lift_request = BlacklistLiftRequest.objects.create(
        blacklist=blacklist,
        requested_by=user,
        scope=scope,
        target_user=target_user,
        reason=reason,
        status="pending",
    )

    return Response(
        {"message": "Lift request submitted.", "lift_request": _serialize_lift_request(lift_request)},
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §5  GET blacklists/lift-requests/  - organizer lists incoming lift requests
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def list_lift_requests(request):
    """List lift requests for an organization (across all its blacklists), newest first.

    Query: ?organization_id= (required) & ?status= (optional, e.g. pending) & paging.
    Auth: Bearer; gated by org_can(user, "can_manage_registrations", org).
    Response: 200 {results, total_count, has_more}.
    FE consumer: organizer dashboard "Blacklists" -> "Lift requests" section.
    """
    user, err = _authenticate(request)
    if err:
        return err

    organization_id = request.GET.get("organization_id")
    if not organization_id:
        return Response(
            {"message": "organization_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    org = Organization.objects.filter(pk=organization_id).first()
    if not org:
        return Response({"message": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

    if not org_can(user, "can_manage_registrations", org):
        return Response(
            {"message": "You do not have permission to view lift requests for this organization."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Filter by the requests whose blacklist belongs to THIS org. select_related walks the chain
    # the serializer reads (blacklist -> team, target_user, requested_by, decided_by).
    qs = (
        BlacklistLiftRequest.objects.filter(blacklist__organization=org)
        .select_related("blacklist__team", "target_user", "requested_by", "decided_by")
        .order_by("-created_at")
    )
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    page, total_count, has_more = _paginate(request, qs)
    results = [_serialize_lift_request(r) for r in page]
    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §6  POST blacklists/lift-requests/<id>/decide/  - organizer approves / denies
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def decide_lift_request(request, request_id):
    """Approve or deny a pending lift request, stamping decided_by/decided_at.

    Decision rules:
      • approve + team scope   -> lift the WHOLE blacklist (status="lifted", all players
                                  is_active=False). Team + everyone can register again.
      • approve + player scope -> retire just that player's OrganizerBlacklistPlayer
                                  (is_active=False); the rest stay blocked. If NO active players
                                  remain afterwards, the blacklist itself is lifted.
      • deny                   -> request status="denied"; nothing else changes (still blocked).

    Request (JSON): {decision:"approve"|"deny", reason?}.
    Auth: Bearer; gated by org_can(user, "can_manage_registrations", blacklist.organization).
    Response: 200 {message, lift_request}.
    FE consumer: organizer dashboard "Lift requests" approve/deny buttons.
    """
    user, err = _authenticate(request)
    if err:
        return err

    lift_request = (
        BlacklistLiftRequest.objects.select_related(
            "blacklist__organization", "blacklist__team", "target_user", "requested_by"
        )
        .filter(pk=request_id)
        .first()
    )
    if not lift_request:
        return Response({"message": "Lift request not found."}, status=status.HTTP_404_NOT_FOUND)

    blacklist = lift_request.blacklist
    if not org_can(user, "can_manage_registrations", blacklist.organization):
        return Response(
            {"message": "You do not have permission to decide this lift request."},
            status=status.HTTP_403_FORBIDDEN,
        )

    decision = request.data.get("decision")
    if decision not in ("approve", "deny"):
        return Response(
            {"message": "decision must be 'approve' or 'deny'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Only a still-pending request can be decided (idempotency guard: re-deciding does nothing).
    if lift_request.status != "pending":
        return Response(
            {"message": "This lift request has already been decided."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if decision == "deny":
        lift_request.status = "denied"
    else:  # approve
        lift_request.status = "approved"
        if lift_request.scope == "team":
            # Full lift: end the blacklist and clear every player block.
            blacklist.status = "lifted"
            blacklist.save(update_fields=["status"])
            blacklist.players.update(is_active=False)
        else:  # player scope
            # Unblock ONLY this player; the others remain blocked.
            OrganizerBlacklistPlayer.objects.filter(
                blacklist=blacklist, user=lift_request.target_user
            ).update(is_active=False)
            # If that was the last active player, the blacklist no longer blocks anyone, so lift
            # it too (keeps status honest for the dashboard + future enforcement).
            if not blacklist.players.filter(is_active=True).exists():
                blacklist.status = "lifted"
                blacklist.save(update_fields=["status"])

    # Stamp who decided + when, with an optional decision note appended to the request reason.
    lift_request.decided_by = user
    lift_request.decided_at = timezone.now()
    decision_note = (request.data.get("reason") or "").strip()
    if decision_note:
        lift_request.reason = (
            f"{lift_request.reason}\n[Organizer] {decision_note}" if lift_request.reason
            else f"[Organizer] {decision_note}"
        )
    lift_request.save()

    return Response(
        {"message": f"Lift request {lift_request.status}.",
         "lift_request": _serialize_lift_request(lift_request)},
        status=status.HTTP_200_OK,
    )
