# afc_organizers/views_design.py
# ──────────────────────────────────────────────────────────────────────────────
# Leaderboard-design request API for the Organizer feature (Phase 3).
#
# The flow is "organizer asks → AFC builds it": an org member submits a custom-look
# request for their leaderboards/results (a reference image + notes), and an AFC
# designer picks it up, builds it, and marks it applied/rejected. Human-in-the-loop —
# there is no self-serve renderer.
#
# Two audiences, two auth gates, all in this one file:
#   • ORGANIZER (member-scoped) — submit a request / list their own org's requests.
#       Gated through afc_organizers.permissions so the owner/admin bypass stays central:
#         - submit  → org_can(user, "can_submit_designs", org)
#         - list    → active member of the org (read-only)
#   • AFC ADMIN (platform oversight) — triage every org's requests, set status + notes.
#       Gated by permissions.is_platform_org_admin (the SAME bypass the rest of the
#       oversight surface in views_admin.py uses).
#
# Convention note (why the code looks like this): this module deliberately mirrors the
# original hand in afc_organizers/views_admin.py + views_organizer.py (which themselves
# mirror afc_team/views.py) — NOT the newer rankings app. So:
#   * function-based @api_view views, one job each;
#   * the auth handshake is done INLINE at the top of every view: read the
#     "Authorization" header, require the "Bearer " prefix, split the token, and hand it
#     to afc_auth's validate_token() — byte-for-byte the rest of the codebase's pattern;
#   * dict serialization written out inline via a shared _serialize_request helper (no
#     serializers.py module);
#   * Response({...}, status=status.HTTP_*) for every return.
# The coordinator owns route mounting — urls.py is edited separately to expose these.
# Full spec: WEBSITE/tasks/organizers-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token is imported the SAME way afc_team/views.py imports it (from the auth
# *views* module, not the models module) so the auth handshake here is identical to the
# rest of the codebase.
from afc_auth.views import validate_token

# Org models + the design-request row this whole module is about.
from afc_organizers.models import Organization, OrganizationMember, LeaderboardDesignRequest

# Reuse the gate helpers — never re-implement the owner/admin bypass here.
from afc_organizers.permissions import org_can, is_platform_org_admin


# ──────────────────────────────────────────────────────────────────────────────
# §0  Shared helpers (DRY — used by every view below)
# ──────────────────────────────────────────────────────────────────────────────
def _authenticate(request):
    """Run the standard Bearer + validate_token handshake.

    Returns a tuple (user, error_response):
      * (user, None)  → authenticated, proceed;
      * (None, resp)  → stop and return `resp` (400 missing header / 400 bad format /
                        401 bad token).
    Mirrors the inline block at the top of every view in views_organizer.py — extracted
    once here because this module has four endpoints that all open the same way."""
    session_token = request.headers.get("Authorization")
    # 400 when the header is missing entirely (malformed request, not an auth failure yet).
    if not session_token:
        return None, Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # 400 when the scheme is wrong — token format is the caller's mistake.
    if not session_token.startswith("Bearer "):
        return None, Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Strip the "Bearer " prefix and resolve the session → user.
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    # 401 when the token does not resolve to a live session/user.
    if not user:
        return None, Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None


def _org_or_404(slug):
    """Load an org by its public slug, or hand back a 404 Response.

    Returns (organization, error_response): exactly one is non-None."""
    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return None, Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return org, None


def _member_or_403(user, org):
    """Return the caller's ACTIVE OrganizationMember row for `org`, or None if they are
    not an active member. Views translate the None into a 403 — membership is the floor
    for the member-scoped read endpoint, so we resolve it in exactly one place."""
    return OrganizationMember.objects.filter(
        organization=org, user=user, status="active"
    ).first()


def _serialize_request(r):
    """Inline dict shape for one LeaderboardDesignRequest. Used by EVERY endpoint that
    returns a request (organizer submit + list, admin list + patch) so the record shape
    is identical everywhere. select_related on the caller's queryset keeps the FK reads
    here from firing extra queries.

    reference_image is rendered as a relative media URL (matching views_admin.py, which
    serializes org logo/banner with `.url`) or null when no image was uploaded."""
    return {
        "id": r.pk,
        "organization_id": r.organization_id,
        # organization_name / *_username read the related rows; callers select_related
        # them so this stays cheap even across a paginated list.
        "organization_name": r.organization.name if r.organization_id else None,
        "title": r.title,
        "notes": r.notes,
        "reference_image": r.reference_image.url if r.reference_image else None,
        "status": r.status,
        "resolution_notes": r.resolution_notes,
        # submitted_by / handled_by are nullable FKs (SET_NULL) — guard every access.
        "submitted_by": r.submitted_by_id,
        "submitted_by_username": r.submitted_by.username if r.submitted_by_id else None,
        "handled_by_username": r.handled_by.username if r.handled_by_id else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _paginate(request, queryset, default_limit=25, max_limit=100):
    """Shared paginator for the admin list. Accepts ?limit (default 25, max 100) and
    ?offset, returning (page, total_count, has_more) so the response can page without
    ever loading the full table into memory. Junk values fall back to defaults rather
    than 500-ing the request — same contract as views_admin.py / views_organizer.py."""
    try:
        limit = int(request.GET.get("limit", default_limit))
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    # Clamp into the allowed window: limit in [1, max_limit], offset non-negative.
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)

    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


# ──────────────────────────────────────────────────────────────────────────────
# §1  ORGANIZER — POST + GET /design-requests/<slug>/  (submit / list the org's requests)
# ──────────────────────────────────────────────────────────────────────────────
# POST and GET share the EXACT same URL per the spec, so — matching the codebase rule
# "one function-based @api_view per route, DRF returns 405 for the wrong method" — both
# verbs live on ONE view that branches on request.method. The two concerns are kept as
# private helpers below so each reads top-to-bottom like its own view.
@api_view(["POST", "GET"])
def design_requests(request, slug):
    """Member-scoped leaderboard-design requests for one org (resolved by <slug>):
      • POST → submit a new request   (gated by can_submit_designs via org_can)
      • GET  → list the org's requests (any ACTIVE member of the org may read)
    The auth handshake + org lookup are shared; the per-verb gate + body handling live in
    the _submit_design_request / _list_design_requests helpers."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _authenticate(request)
    if err:
        return err

    # Resolve the org by its public slug (404 if missing) — shared by both verbs.
    org, err = _org_or_404(slug)
    if err:
        return err

    # Route on verb. (@api_view already 405s anything outside POST/GET.)
    if request.method == "POST":
        return _submit_design_request(request, user, org)
    return _list_design_requests(request, user, org)


def _submit_design_request(request, user, org):
    """POST handler: create a new design request for `org`. Gated by can_submit_designs
    (routed through org_can so the owner/admin bypass stays central). Accepts multipart so
    an optional reference_image can ride along in request.FILES. The new row starts life
    `open` with submitted_by = the caller."""
    # Permission gate — can_submit_designs, via org_can (owner/admin bypass lives there).
    if not org_can(user, "can_submit_designs", org):
        return Response(
            {"message": "You do not have permission to submit design requests."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── read + validate body ── title is required; notes optional.
    title = (request.data.get("title") or "").strip()
    notes = request.data.get("notes") or ""
    if not title:
        return Response(
            {"message": "title is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── create the request (status defaults to "open"; submitted_by = caller) ──
    # reference_image is optional and arrives via multipart (request.FILES); pass it
    # straight through to the ImageField when present.
    design_request = LeaderboardDesignRequest.objects.create(
        organization=org,
        submitted_by=user,
        title=title,
        notes=notes,
        reference_image=request.FILES.get("reference_image"),
    )

    return Response(
        {"message": "Design request submitted.", "request": _serialize_request(design_request)},
        status=status.HTTP_201_CREATED,
    )


def _list_design_requests(request, user, org):
    """GET handler: list every design request belonging to `org`, newest first. Any ACTIVE
    member of the org may read it (read-only roster of the org's own requests) — a
    non-member is 403 so they learn nothing about an org they have no stake in."""
    # Membership gate — only the org's own active people may see its requests.
    if not _member_or_403(user, org):
        return Response(
            {"message": "You are not a member of this organization."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # This org's requests, newest first. select_related the FK rows the serializer reads.
    requests = (
        LeaderboardDesignRequest.objects.filter(organization=org)
        .select_related("organization", "submitted_by", "handled_by")
        .order_by("-created_at")
    )

    results = [_serialize_request(r) for r in requests]
    return Response({"results": results}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# §3  AFC ADMIN — GET /admin/design-requests/  (triage queue across every org)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_design_requests(request):
    """Oversight list of design requests across EVERY org. AFC-staff only
    (is_platform_org_admin). Supports ?status= (exact) and ?organization_id= (exact)
    filters, paginated so the admin queue never pulls the whole table at once. Each row
    carries organization_name + submitted_by username (from _serialize_request)."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _authenticate(request)
    if err:
        return err

    # AFC-staff gate — same platform-admin bypass the rest of the oversight surface uses.
    if not is_platform_org_admin(user):
        return Response(
            {"message": "You do not have permission to manage design requests."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Start from all requests, newest first; select_related the FK rows the serializer reads.
    qs = (
        LeaderboardDesignRequest.objects.all()
        .select_related("organization", "submitted_by", "handled_by")
        .order_by("-created_at")
    )

    # Optional exact status filter (open / in_progress / applied / rejected).
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    # Optional exact organization filter — guard the int parse so junk is a no-op.
    organization_id = request.GET.get("organization_id")
    if organization_id:
        try:
            qs = qs.filter(organization_id=int(organization_id))
        except (TypeError, ValueError):
            pass

    # Paginate (default 25 / max 100) and serialize.
    page, total_count, has_more = _paginate(request, qs)
    results = [_serialize_request(r) for r in page]

    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §4  AFC ADMIN — PATCH /admin/design-requests/<id>/  (resolve a request)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def admin_update_design_request(request, request_id):
    """Triage one design request: move its status (open → in_progress → applied/rejected)
    and/or attach resolution_notes. AFC-staff only (is_platform_org_admin). handled_by is
    stamped to the acting admin so the audit trail records who worked it. True PATCH —
    only the keys present in the body are touched."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _authenticate(request)
    if err:
        return err

    # AFC-staff gate — same platform-admin bypass the rest of the oversight surface uses.
    if not is_platform_org_admin(user):
        return Response(
            {"message": "You do not have permission to manage design requests."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Resolve the target request by id (404 if missing); select_related for the serializer.
    design_request = (
        LeaderboardDesignRequest.objects
        .select_related("organization", "submitted_by", "handled_by")
        .filter(pk=request_id)
        .first()
    )
    if not design_request:
        return Response(
            {"message": "Design request not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── status (open / in_progress / applied / rejected) ──
    if "status" in request.data:
        new_status = request.data.get("status")
        valid_statuses = {choice[0] for choice in LeaderboardDesignRequest.STATUS_CHOICES}
        if new_status not in valid_statuses:
            return Response(
                {"message": "Invalid status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        design_request.status = new_status

    # ── resolution_notes (AFC's reply / build notes) ──
    if "resolution_notes" in request.data:
        design_request.resolution_notes = request.data.get("resolution_notes") or ""

    # Stamp the acting admin as the handler so the audit trail is clear.
    design_request.handled_by = user
    design_request.save()

    return Response(
        {"message": "Design request updated.", "request": _serialize_request(design_request)},
        status=status.HTTP_200_OK,
    )
