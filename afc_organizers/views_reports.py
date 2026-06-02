# afc_organizers/views_reports.py
# ──────────────────────────────────────────────────────────────────────────────
# Organization ABUSE-REPORT endpoints — the Phase 4 "report → AFC reviews → integrity
# action" surface for the Organizer feature.
#
# Two audiences live in this file:
#   • Any logged-in USER can file a report against an organization (suspected rankings
#     manipulation, fake results, unfair conduct, …) with optional evidence.
#   • AFC PLATFORM STAFF (head_admin / organizer_admin) list, triage, and resolve those
#     reports — and, when a report is upheld, perform the integrity action: flip the
#     offending event's `rankings_verified` to False so it stops counting toward the
#     official rankings.
#
# Convention note (why the code looks like this): this module deliberately mirrors the
# original hand in afc_team/views.py and the sibling afc_organizers views — NOT the
# newer rankings app. So:
#   * function-based @api_view views, one job each;
#   * auth done inline by reading the Authorization header and calling validate_token
#     (imported the SAME way afc_team/views.py imports it — from afc_auth.views);
#     missing header → 400, wrong scheme → 400, bad/expired token → 401;
#   * dict serialization written out inline in each view (no serializers.py);
#   * Response({...}, status=status.HTTP_*) for every return.
#
# Permission gates reuse afc_organizers/permissions.py so the owner/admin-bypass rules
# stay in ONE place: is_platform_org_admin for the AFC-staff triage views. (org_can /
# org_can_event are imported alongside it to keep this file's permission surface
# identical to the rest of the app, even though the report-filing view intentionally
# only requires a logged-in user.)
#
# Route mounting lives in afc_organizers/urls.py and is owned by the coordinator —
# this file ONLY defines view functions; it does not touch urls.py.
# Full spec: WEBSITE/tasks/organizers-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token lives in afc_auth.views — import it the SAME way afc_team/views.py
# does (confirmed: `from afc_auth.views import validate_token`). It returns the User
# for a valid, non-expired session token, or None otherwise.
from afc_auth.views import validate_token

from afc_organizers.models import Organization, OrganizationReport
from afc_organizers.permissions import org_can, org_can_event, is_platform_org_admin
from afc_tournament_and_scrims.models import Event


# ──────────────────────────────────────────────────────────────────────────────
# §0  Shared auth helper (DRY — the Bearer handshake is identical across views)
# ──────────────────────────────────────────────────────────────────────────────
# Resolve the caller from the Authorization header exactly like the original hand:
# read the raw header, require the "Bearer " scheme, strip it, and hand the token to
# afc_auth's validate_token(). We return a tuple (user, error_response) so each view
# can bail early on the returned Response:
#   * (user, None)  → authenticated, proceed;
#   * (None, resp)  → stop and return `resp` (400 missing header / 400 bad format /
#                     401 invalid-or-expired token).
def _authenticate(request):
    session_token = request.headers.get("Authorization")

    # 400 when the header is missing entirely — a malformed request, not yet an
    # auth failure (matches afc_team/views.py wording/shape).
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


# Small shared paginator. List endpoints accept ?limit (default 25, max 100) and
# ?offset, returning {"results", "total_count", "has_more"} so the frontend can page
# without ever loading the whole table into memory. Junk values fall back to the
# default rather than 500-ing the request.
def _paginate(request, queryset):
    try:
        limit = int(request.GET.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0

    # Clamp to sane bounds: limit in [1, 100], offset non-negative.
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    total_count = queryset.count()
    page = queryset[offset:offset + limit]
    has_more = (offset + limit) < total_count
    return page, total_count, has_more


# Canonical inline dict for one OrganizationReport. Reused by the admin list view and
# returned (singly) from admin_update_report so both surfaces hand back an identical
# record shape. Image fields follow the codebase contract: (img.url if img else None).
# select_related on organization / event / reporter / reviewed_by upstream keeps this
# dependency-free of extra queries when serializing a page of rows.
def _serialize_report(report):
    return {
        "id": report.id,
        "organization_id": report.organization_id,
        # organization is non-nullable on the model, but guard defensively anyway.
        "organization_name": report.organization.name if report.organization else None,
        # event is nullable (SET_NULL) — a report may be org-wide, not event-specific.
        "event_id": report.event_id,
        "event_name": report.event.event_name if report.event else None,
        "category": report.category,
        "details": report.details,
        "evidence": report.evidence.url if report.evidence else None,
        "status": report.status,
        "resolution_notes": report.resolution_notes,
        # reporter / reviewed_by are SET_NULL FKs — surface the username or None.
        "reporter_username": report.reporter.username if report.reporter else None,
        "reviewed_by_username": report.reviewed_by.username if report.reviewed_by else None,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# §1  POST /<slug>/reports  — file a report against an organization
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def report_organization(request, slug):
    """File an abuse report against an organization. Open to ANY logged-in user — the
    floor here is just a valid session, not org membership (the whole point is that an
    outsider who suspects manipulation can flag it). Accepts a multipart body: a
    category (validated against the model choices, defaulting to 'other'), required
    free-text details, an optional event_id (must belong to THIS org), and optional
    evidence image upload. The row is created 'open' with the caller as reporter."""
    # ── auth handshake (Bearer + validate_token) — any logged-in user passes ──
    user, err = _authenticate(request)
    if err:
        return err

    # Resolve the org by its public slug. 404 if it does not exist.
    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── category: validate against the model's choices, default to "other" ──
    valid_categories = {choice[0] for choice in OrganizationReport.CATEGORY_CHOICES}
    category = request.data.get("category") or "other"
    if category not in valid_categories:
        return Response(
            {"message": "Invalid report category."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── details: required free text (400 if empty / whitespace-only) ──
    details = (request.data.get("details") or "").strip()
    if not details:
        return Response(
            {"message": "Report details are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── optional event_id: if supplied it MUST be an event homed under THIS org ──
    # A mismatched event would let a report point at someone else's event, so we reject
    # rather than silently dropping it (the caller named a specific event for a reason).
    event = None
    event_id = request.data.get("event_id")
    if event_id:
        event = Event.objects.filter(pk=event_id, organization=org).first()
        if not event:
            return Response(
                {"message": "Event does not belong to this organization."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # ── optional evidence image from the multipart upload ──
    evidence = request.FILES.get("evidence")

    # ── create the report (always starts 'open'; reporter is the caller) ──
    OrganizationReport.objects.create(
        organization=org,
        event=event,
        reporter=user,
        category=category,
        details=details,
        evidence=evidence,
        status="open",
    )

    return Response(
        {"message": "Report submitted. Thank you — AFC will review it."},
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §2  GET /reports  — AFC-staff triage list of every report
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def admin_list_reports(request):
    """Oversight list of every organization report for AFC staff. Gated by
    is_platform_org_admin (403 for anyone else). Supports an optional ?status= filter
    (open / reviewing / resolved / dismissed) and ?organization_id= filter, and is
    paginated (?limit default 25 / max 100, ?offset). Newest first so fresh reports
    surface at the top of the triage queue."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _authenticate(request)
    if err:
        return err

    # AFC-staff gate — this is an oversight surface, not for organizers or users.
    if not is_platform_org_admin(user):
        return Response(
            {"message": "You do not have permission to view organization reports."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Base queryset, newest first. select_related pulls the FK rows the serializer
    # touches (org / event / reporter / reviewed_by) in one query — no N+1 per page.
    qs = (
        OrganizationReport.objects.select_related(
            "organization", "event", "reporter", "reviewed_by"
        )
        .all()
        .order_by("-created_at")
    )

    # ── optional exact status filter ──
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    # ── optional organization filter (by numeric organization_id) ──
    organization_id = request.GET.get("organization_id")
    if organization_id:
        qs = qs.filter(organization_id=organization_id)

    # Paginate + serialize through the shared helpers.
    page, total_count, has_more = _paginate(request, qs)
    results = [_serialize_report(report) for report in page]

    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  PATCH /reports/<report_id>  — AFC-staff resolve / triage a report
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def admin_update_report(request, report_id):
    """Triage one report: update its status and/or resolution_notes, recording the
    acting admin as reviewed_by. Gated by is_platform_org_admin. The INTEGRITY ACTION
    lives here too: when `exclude_event` is true AND the report names an event, that
    event's `rankings_verified` is flipped to False so its results stop counting toward
    the official rankings — the concrete "uphold the report" lever AFC pulls."""
    # ── auth handshake (Bearer + validate_token) ──
    user, err = _authenticate(request)
    if err:
        return err

    # AFC-staff gate.
    if not is_platform_org_admin(user):
        return Response(
            {"message": "You do not have permission to manage organization reports."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Load the report (select_related so the serialized response and the exclude
    # action both see the event/org rows without an extra round-trip).
    report = (
        OrganizationReport.objects.select_related(
            "organization", "event", "reporter", "reviewed_by"
        )
        .filter(pk=report_id)
        .first()
    )
    if not report:
        return Response(
            {"message": "Report not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ── status: only apply when present AND valid (true PATCH semantics) ──
    if "status" in request.data:
        new_status = request.data.get("status")
        valid_statuses = {choice[0] for choice in OrganizationReport.STATUS_CHOICES}
        if new_status not in valid_statuses:
            return Response(
                {"message": "Invalid report status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        report.status = new_status

    # ── resolution_notes: apply when the key was sent (allows clearing to "") ──
    if "resolution_notes" in request.data:
        report.resolution_notes = request.data.get("resolution_notes") or ""

    # ── reviewed_by: always stamp the acting admin — they handled this report ──
    report.reviewed_by = user
    report.save()

    # ── integrity action: exclude the reported event from official rankings ──
    # Only fires when exclude_event is truthy AND the report actually names an event.
    # This is the lever that "upholds" a manipulation report: the event's results stop
    # counting toward the rankings until/unless AFC re-verifies it.
    exclude_event = request.data.get("exclude_event")
    if exclude_event and report.event:
        report.event.rankings_verified = False
        report.event.save()

    return Response(
        {"message": "Report updated.", "report": _serialize_report(report)},
        status=status.HTTP_200_OK,
    )
