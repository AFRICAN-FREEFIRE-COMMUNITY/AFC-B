# afc_organizers/views_organizer.py
# ──────────────────────────────────────────────────────────────────────────────
# Member-scoped Organizer endpoints — the surface that org OWNERS and SUB_ORGANIZERS
# use to manage their OWN organization. Each endpoint is scoped to a single org
# (resolved by <slug>) and the caller must be an active member of that org; nobody
# reaches across into another org here. (The AFC-staff oversight surface — provisioning,
# suspending, cross-org listing — lives elsewhere and is gated by the platform-admin
# bypass inside permissions.org_can.)
#
# Why this file mirrors afc_team / afc_tournament_and_scrims and NOT the rankings app:
#   • Function-based @api_view views, one concern per function.
#   • The auth handshake is done inline at the top of every view: read the
#     "Authorization" header, require the "Bearer " prefix, split the token, and hand
#     it to afc_auth's validate_token() — the exact pattern afc_team/views.py uses.
#   • Responses are serialized INLINE as plain dicts (no serializers.py module), so the
#     original developer can read a view top-to-bottom and see the whole request/response
#     shape in one place.
#
# Permission rule of thumb (all enforced via permissions.org_can so the owner/admin
# bypass stays in ONE place — never re-implement it here):
#   • owner               → every can_* is effectively True.
#   • sub_organizer       → only the granular toggles the owner granted.
#   • non-member          → 403 (cannot see or touch the org at all).
#
# Full spec: WEBSITE/tasks/organizers-design.md.
# ──────────────────────────────────────────────────────────────────────────────
import json

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token is imported the SAME way afc_team/views.py imports it (from the auth
# *views* module, not the models module) so the auth handshake here is byte-for-byte
# identical to the rest of the codebase.
from afc_auth.views import validate_token

from afc_organizers.models import Organization, OrganizationMember, PERMISSION_FIELDS
from afc_organizers.permissions import org_can
from afc_auth.models import User, Roles, UserRoles


# ──────────────────────────────────────────────────────────────────────────────
# §0  Shared helpers (DRY — used by several views below)
# ──────────────────────────────────────────────────────────────────────────────
def _member_or_403(user, org):
    """Return the caller's ACTIVE OrganizationMember row for `org`, or None if they
    are not an active member. Views translate the None into a 403 — membership is the
    floor for every member-scoped endpoint, so we resolve it in exactly one place."""
    return OrganizationMember.objects.filter(
        organization=org, user=user, status="active"
    ).first()


def _effective_permissions(member):
    """Collapse a member row into the {can_*: bool} map the frontend actually needs.
    The owner implicitly has EVERYTHING, so we short-circuit to all-True for them; a
    sub_organizer reports the literal column values. This mirrors permissions.org_can's
    owner rule — we surface the same truth, just as a serialized map instead of a gate."""
    if member.role == "owner":
        return {field: True for field in PERMISSION_FIELDS}
    return {field: bool(getattr(member, field, False)) for field in PERMISSION_FIELDS}


def _serialize_member(member):
    """Inline dict shape for one OrganizationMember, including its effective permission
    map. Kept tiny and dependency-free so the member-list and member-mutation endpoints
    all hand back an identical record shape."""
    member_user = member.user
    return {
        "user_id": member_user.user_id,
        "username": member_user.username,
        "full_name": member_user.full_name,
        "role": member.role,
        "status": member.status,
        "permissions": _effective_permissions(member),
    }


def _paginate(request, default_limit=25, max_limit=100):
    """Parse ?limit/?offset off the query string with safe bounds. Every list endpoint
    in this file uses the same contract — limit defaults to 25, hard-capped at 100, and
    offset floors at 0 — so the response envelope ({results,total_count,has_more}) is
    consistent and can never be asked to load an unbounded result set."""
    try:
        limit = int(request.GET.get("limit", default_limit))
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    # Clamp into the allowed window.
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)
    return limit, offset


# ──────────────────────────────────────────────────────────────────────────────
# §1  GET /  — the caller's own active memberships (the "switcher" feed)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def get_my_organizations(request):
    """List every organization the current user is an ACTIVE member of, with their role
    and effective permissions per org. This powers the organizer org-switcher: a user can
    belong to several orgs, and each row carries the permission map for THAT org."""
    # ── auth handshake (Bearer + validate_token) ──
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Active memberships only — a removed member should not see the org at all.
    # select_related("organization") avoids an N+1 across the org rows we serialize.
    memberships = (
        OrganizationMember.objects.filter(user=user, status="active")
        .select_related("organization")
        .order_by("organization__name")
    )

    results = []
    for member in memberships:
        org = member.organization
        results.append(
            {
                "organization": {
                    "organization_id": org.organization_id,
                    "slug": org.slug,
                    "name": org.name,
                    "logo": request.build_absolute_uri(org.logo.url) if org.logo else None,
                    "status": org.status,
                },
                "role": member.role,
                "permissions": _effective_permissions(member),
            }
        )

    return Response({"results": results}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# §2  GET /<slug>  — full profile of one org the caller belongs to
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def get_organization(request, slug):
    """Return the full profile of a single organization plus the caller's effective
    permissions in it. Any active member may read; a non-member is 403 (they should not
    learn anything about an org they have no stake in)."""
    # ── auth handshake (Bearer + validate_token) ──
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Resolve the org by its public slug.
    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Membership is the gate — must be an active member of THIS org.
    member = _member_or_403(user, org)
    if not member:
        return Response(
            {"message": "You are not a member of this organization."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Full inline serialization — includes branding, contact, description, socials.
    organization = {
        "organization_id": org.organization_id,
        "slug": org.slug,
        "name": org.name,
        "logo": request.build_absolute_uri(org.logo.url) if org.logo else None,
        "default_banner": request.build_absolute_uri(org.default_banner.url) if org.default_banner else None,
        "email": org.email,
        "description": org.description,
        "socials": org.socials,
        "status": org.status,
        # headline counts for the dashboard Overview (active members + events homed here).
        "member_count": org.members.filter(status="active").count(),
        "event_count": org.events.count(),
    }

    return Response(
        {"organization": organization, "my_permissions": _effective_permissions(member)},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §3  PATCH /<slug>  — edit org branding/contact (OWNER ONLY)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def edit_organization_profile(request, slug):
    """Update an organization's public profile: email, description, socials, and the
    uploaded logo / default_banner files. Restricted to the OWNER — not even a
    sub_organizer with can_manage_members may rebrand the org, since branding is an
    identity-level action reserved for the account holder."""
    # ── auth handshake (Bearer + validate_token) ──
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Owner-only gate: must be an active member AND hold the owner role.
    member = _member_or_403(user, org)
    if not member or member.role != "owner":
        return Response(
            {"message": "Only the organization owner can edit the profile."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── apply only the fields actually present in the request (PATCH semantics) ──
    # Text/JSON fields: update when the key was sent so callers can clear a field
    # explicitly without us guessing intent on absent keys.
    if "email" in request.data:
        org.email = request.data.get("email") or None
    if "description" in request.data:
        org.description = request.data.get("description") or ""
    if "socials" in request.data:
        socials = request.data.get("socials")
        # Tolerate a JSON-encoded string (multipart form posts) or an already-parsed dict.
        if isinstance(socials, str):
            try:
                socials = json.loads(socials)
            except (ValueError, TypeError):
                return Response(
                    {"message": "socials must be a valid JSON object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if not isinstance(socials, dict):
            return Response(
                {"message": "socials must be a JSON object."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        org.socials = socials

    # File fields: only replace when a new upload is present in request.FILES.
    if "logo" in request.FILES:
        org.logo = request.FILES["logo"]
    if "default_banner" in request.FILES:
        org.default_banner = request.FILES["default_banner"]

    org.save()

    organization = {
        "organization_id": org.organization_id,
        "slug": org.slug,
        "name": org.name,
        "logo": request.build_absolute_uri(org.logo.url) if org.logo else None,
        "default_banner": request.build_absolute_uri(org.default_banner.url) if org.default_banner else None,
        "email": org.email,
        "description": org.description,
        "socials": org.socials,
        "status": org.status,
    }

    return Response(
        {"message": "Organization profile updated.", "organization": organization},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §4  GET /<slug>/members  — roster of one org (active members only)
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["GET"])
def get_organization_members(request, slug):
    """List the active members of an organization (owner + sub_organizers) with each
    member's role and effective permission map. Any active member may view the roster —
    it is read-only and the org's own people legitimately need to see who else is on it."""
    # ── auth handshake (Bearer + validate_token) ──
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Membership gate — only the org's own people may see the roster.
    if not _member_or_403(user, org):
        return Response(
            {"message": "You are not a member of this organization."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Active members only; select_related("user") to avoid an N+1 across the roster.
    members = (
        OrganizationMember.objects.filter(organization=org, status="active")
        .select_related("user")
        .order_by("-role", "user__username")  # owner first (sorts after sub_* desc), then A→Z
    )

    results = [_serialize_member(m) for m in members]
    return Response({"results": results}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# §5  POST /<slug>/members  — invite/add a sub_organizer
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["POST"])
def add_organization_member(request, slug):
    """Add an existing user to the org as a sub_organizer (creating, or reactivating a
    previously-removed, OrganizationMember row) and grant them the platform-level
    'organizer' role. Gated by can_manage_members so an owner or a trusted sub_organizer
    can grow the team. Optional `permissions` toggles seed the new member's grants."""
    # ── auth handshake (Bearer + validate_token) ──
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Permission gate — routed through org_can so the owner/admin bypass stays central.
    if not org_can(user, "can_manage_members", org):
        return Response(
            {"message": "You do not have permission to manage members."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── resolve the target user by username ──
    username = request.data.get("username")
    if not username:
        return Response(
            {"message": "username is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    target_user = User.objects.filter(username=username).first()
    if not target_user:
        return Response(
            {"message": "No user found with that username."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Whitelist the incoming permission toggles to known PERMISSION_FIELDS only — we
    # never let a request set arbitrary attributes on the member row.
    requested_perms = request.data.get("permissions") or {}
    if not isinstance(requested_perms, dict):
        requested_perms = {}

    # ── create or reactivate the membership row ──
    # unique_together(organization, user) means a user can only have ONE row per org, so
    # a "removed" person is reactivated rather than duplicated.
    member, created = OrganizationMember.objects.get_or_create(
        organization=org,
        user=target_user,
        defaults={"role": "sub_organizer", "invited_by": user, "status": "active"},
    )
    if not created:
        # Refuse to clobber the owner via the add endpoint.
        if member.role == "owner":
            return Response(
                {"message": "This user is the organization owner."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Re-activate a previously removed sub_organizer and reset its inviter.
        member.role = "sub_organizer"
        member.status = "active"
        member.invited_by = user

    # Apply only the permission keys that exist in PERMISSION_FIELDS, coercing to bool.
    for field in PERMISSION_FIELDS:
        if field in requested_perms:
            setattr(member, field, bool(requested_perms[field]))
    member.save()

    # ── grant the platform-level 'organizer' role (idempotent) ──
    # Roles.role is an FK on UserRoles, so we resolve the Roles row first; get_or_create
    # on the Roles side keeps this safe even on a fresh DB where the row is missing.
    organizer_role, _ = Roles.objects.get_or_create(
        role_name="organizer",
        defaults={"description": "Granted to any active OrganizationMember."},
    )
    UserRoles.objects.get_or_create(user=target_user, role=organizer_role)

    return Response(
        {"message": "Member added to the organization.", "member": _serialize_member(member)},
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §6  PATCH /<slug>/members/<user_id>  — retune a sub_organizer's permissions
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["PATCH"])
def edit_organization_member(request, slug, user_id):
    """Toggle the granular permission booleans on one sub_organizer's membership row.
    Gated by can_manage_members. The owner's own row is off-limits (400) — the owner
    already has everything implicitly and must never be down-scoped through this path."""
    # ── auth handshake (Bearer + validate_token) ──
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Permission gate — central owner/admin bypass via org_can.
    if not org_can(user, "can_manage_members", org):
        return Response(
            {"message": "You do not have permission to manage members."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Target the membership row for <user_id> within THIS org (active members only).
    member = OrganizationMember.objects.filter(
        organization=org, user__user_id=user_id, status="active"
    ).select_related("user").first()
    if not member:
        return Response(
            {"message": "Member not found in this organization."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # The owner's row carries every permission implicitly — never editable here.
    if member.role == "owner":
        return Response(
            {"message": "The organization owner's permissions cannot be edited."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Apply only the recognised permission keys present in the body, coercing to bool.
    requested_perms = request.data.get("permissions")
    if requested_perms is None or not isinstance(requested_perms, dict):
        # Fall back to reading permission keys straight off the top-level body, so callers
        # may send {"can_create_events": true} without a nesting wrapper.
        requested_perms = request.data
    for field in PERMISSION_FIELDS:
        if field in requested_perms:
            setattr(member, field, bool(requested_perms[field]))
    member.save()

    return Response(
        {"message": "Member permissions updated.", "member": _serialize_member(member)},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# §7  DELETE /<slug>/members/<user_id>  — soft-remove a sub_organizer
# ──────────────────────────────────────────────────────────────────────────────
@api_view(["DELETE"])
def remove_organization_member(request, slug, user_id):
    """Soft-remove a sub_organizer from the org by flipping their membership status to
    'removed' (preserving history and allowing later reactivation). Gated by
    can_manage_members. The owner cannot be removed through this path (400)."""
    # ── auth handshake (Bearer + validate_token) ──
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response(
            {"message": "Authorization header is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session_token.startswith("Bearer "):
        return Response(
            {"message": "Invalid token format"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Permission gate — central owner/admin bypass via org_can.
    if not org_can(user, "can_manage_members", org):
        return Response(
            {"message": "You do not have permission to manage members."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Target the active membership row for <user_id> within THIS org.
    member = OrganizationMember.objects.filter(
        organization=org, user__user_id=user_id, status="active"
    ).first()
    if not member:
        return Response(
            {"message": "Member not found in this organization."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # The owner is structural — refuse to remove them through the member endpoint.
    if member.role == "owner":
        return Response(
            {"message": "The organization owner cannot be removed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Soft-delete: flip status rather than deleting the row, preserving audit history.
    member.status = "removed"
    member.save()

    return Response(
        {"message": "Member removed from the organization."},
        status=status.HTTP_200_OK,
    )
