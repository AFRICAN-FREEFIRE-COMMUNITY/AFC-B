# afc_organizers/views_admin.py
# ──────────────────────────────────────────────────────────────────────────────
# AFC-staff provisioning + oversight endpoints for the Organizer feature.
#
# These are the views the platform team (head_admin / organizer_admin) uses to
# stand up an organization for an external organizer, watch over every org, and
# fix things when something goes wrong. They are the "oversight layer" referenced
# in afc_organizers/permissions.py — AFC has full reach into every org by design.
#
# Convention note (why the code looks like this): this module deliberately mirrors
# the original hand in afc_team/views.py and afc_tournament_and_scrims — NOT the
# newer rankings app. So:
#   * function-based @api_view views, one job each;
#   * auth done inline by reading the Authorization header and calling
#     validate_token (imported the same way afc_team/views.py imports it);
#   * dict serialization written out inline in each view (no serializers.py);
#   * Response({...}, status=status.HTTP_*) for every return.
# The coordinator owns route mounting — this file ONLY defines view functions; it
# does not touch urls.py. Full spec: WEBSITE/tasks/organizers-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from django.utils.text import slugify
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# validate_token lives in afc_auth.views — import it the SAME way afc_team/views.py
# does (confirmed: `from afc_auth.views import validate_token`).
from afc_auth.views import validate_token
from afc_auth.models import User, Roles, UserRoles
from afc_organizers.models import Organization, OrganizationMember, PERMISSION_FIELDS
from afc_organizers.permissions import is_platform_org_admin


# ──────────────────────────────────────────────────────────────────────────────
# Auth gate helper
# ──────────────────────────────────────────────────────────────────────────────
# Every endpoint in this module is AFC-staff only, so the header parse +
# token validation + platform-admin check is identical across all of them. We
# resolve it once here and let each view bail early on the returned Response.
#
# Returns a tuple (user, error_response):
#   * (user, None)  → authenticated AFC staff, proceed;
#   * (None, resp)  → stop and return `resp` (400 missing header / 401 bad token /
#                     403 not platform admin).
def _require_platform_admin(request):
    # Read the raw Authorization header exactly like the original hand does.
    session_token = request.headers.get("Authorization")

    # 400 when the header is missing entirely (it is a malformed request, not an
    # auth failure yet) — matches afc_team/views.py wording/shape.
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

    # 403 GATE: every view here is AFC-staff only. A valid login that is not a
    # platform org admin must be refused with the exact message the spec dictates.
    if not is_platform_org_admin(user):
        return None, Response(
            {"message": "You do not have permission to manage organizations."},
            status=status.HTTP_403_FORBIDDEN,
        )

    return user, None


# ──────────────────────────────────────────────────────────────────────────────
# DRY serialization helpers
# ──────────────────────────────────────────────────────────────────────────────
# _org_or_404 centralises the "load this org or 404" lookup so list/detail/edit
# views all behave identically (and 404 messages stay in one place).
#
# Returns (organization, error_response): exactly one is non-None.
def _org_or_404(slug):
    org = Organization.objects.filter(slug=slug).first()
    if not org:
        return None, Response(
            {"message": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return org, None


# _serialize_org produces the canonical org dict reused by the list view and as
# the core of the detail view. Kept lean: counts use the reverse relations
# (members / events) declared on the models so we never hand-join.
def _serialize_org(org):
    return {
        "organization_id": org.organization_id,
        "slug": org.slug,
        "name": org.name,
        "status": org.status,
        "email": org.email,
        # active members only — removed rows must not inflate the headcount.
        "member_count": org.members.filter(status="active").count(),
        # every event currently homed under this org (drafts included).
        "event_count": org.events.count(),
        "created_at": org.created_at.isoformat() if org.created_at else None,
    }


# Small shared paginator. List endpoints accept ?limit (default 25, max 100) and
# ?offset, returning {"results", "total_count", "has_more"} so the frontend can
# page without ever loading the full table into memory.
def _paginate(request, queryset):
    # Parse limit defensively — a junk value falls back to the default rather
    # than 500-ing the request.
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


# ──────────────────────────────────────────────────────────────────────────────
# 1) admin_create_organization  (POST)
# ──────────────────────────────────────────────────────────────────────────────
# Provision a brand-new organization for an external organizer. The organizer
# already has a normal user account — we attach it as the org owner and grant the
# 'organizer' role so they can reach the organizer dashboard. We do NOT create a
# user here; owner_username must resolve to an existing account.
@api_view(["POST"])
def admin_create_organization(request):
    # Auth + AFC-staff gate.
    user, err = _require_platform_admin(request)
    if err:
        return err

    # ── read + validate body ──
    name = (request.data.get("name") or "").strip()
    owner_username = (request.data.get("owner_username") or "").strip()
    email = request.data.get("email")
    description = request.data.get("description", "")

    if not name:
        return Response({"message": "Organization name is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not owner_username:
        return Response({"message": "owner_username is required."}, status=status.HTTP_400_BAD_REQUEST)

    # ── resolve the owner account (must already exist) ──
    owner = User.objects.filter(username=owner_username).first()
    if not owner:
        return Response({"message": "Owner user not found."}, status=status.HTTP_404_NOT_FOUND)

    # ── derive a unique slug ──
    # slugify the name, then suffix "-2", "-3", … until we hit a free handle.
    base_slug = slugify(name) or "organization"
    slug = base_slug
    suffix = 2
    while Organization.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    # ── create the org (created_by records WHICH AFC admin provisioned it) ──
    org = Organization.objects.create(
        name=name,
        slug=slug,
        email=email,
        description=description or "",
        created_by=user,
    )

    # ── attach the owner as an active 'owner' member ──
    # invited_by is the provisioning admin so the audit trail is clear.
    OrganizationMember.objects.create(
        organization=org,
        user=owner,
        role="owner",
        status="active",
        invited_by=user,
    )

    # ── grant the owner the platform 'organizer' role (idempotent) ──
    # get_or_create so re-provisioning for the same person never duplicates rows.
    organizer_role = Roles.objects.get(role_name="organizer")
    UserRoles.objects.get_or_create(user=owner, role=organizer_role)

    return Response(
        {"message": "Organization created successfully.", "organization": _serialize_org(org)},
        status=status.HTTP_201_CREATED,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2) admin_list_organizations  (GET)
# ──────────────────────────────────────────────────────────────────────────────
# Oversight list of every org. Supports ?status= (exact) and ?search= (name OR
# slug, case-insensitive contains). Paginated so the admin table never pulls the
# whole set at once.
@api_view(["GET"])
def admin_list_organizations(request):
    # Auth + AFC-staff gate.
    user, err = _require_platform_admin(request)
    if err:
        return err

    # Start from all orgs, newest first (most recently provisioned on top).
    qs = Organization.objects.all().order_by("-created_at")

    # Optional exact status filter (active / suspended / deleted).
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    # Optional fuzzy search across name OR slug.
    search = request.GET.get("search")
    if search:
        from django.db.models import Q
        qs = qs.filter(Q(name__icontains=search) | Q(slug__icontains=search))

    # Paginate and serialize.
    page, total_count, has_more = _paginate(request, qs)
    results = [_serialize_org(org) for org in page]

    return Response(
        {"results": results, "total_count": total_count, "has_more": has_more},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3) admin_get_organization  (GET, <slug>)
# ──────────────────────────────────────────────────────────────────────────────
# Full oversight detail for one org: the base org dict plus its members (with the
# granular per-member permission flags) and its events. "reports" is reserved for
# Phase 4 and intentionally returned empty for now so the frontend contract is
# stable.
@api_view(["GET"])
def admin_get_organization(request, slug):
    # Auth + AFC-staff gate.
    user, err = _require_platform_admin(request)
    if err:
        return err

    org, err = _org_or_404(slug)
    if err:
        return err

    # ── members: include every member (active + removed) so the admin sees the
    # full roster; surface each granular can_* flag inline from the row. ──
    members = []
    for m in org.members.select_related("user").all():
        members.append({
            "user_id": m.user_id,
            "username": m.user.username,
            "role": m.role,
            "status": m.status,
            # Pull each permission field straight off the member row.
            "permissions": {field: getattr(m, field) for field in PERMISSION_FIELDS},
        })

    # ── events currently homed under this org ──
    events = []
    for event in org.events.all():
        events.append({
            "event_id": event.pk,
            "event_name": event.event_name,
            "status": event.event_status,
            "is_draft": event.is_draft,
        })

    # Base org dict + the nested collections.
    detail = _serialize_org(org)
    detail["members"] = members
    detail["events"] = events
    detail["reports"] = []  # Phase 4 — kept present so the response shape is stable.

    return Response({"organization": detail}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 4) admin_edit_organization  (PATCH, <slug>)
# ──────────────────────────────────────────────────────────────────────────────
# Partial update of an org's editable fields. Only the keys actually present in
# the body are touched (true PATCH semantics) so an admin can change one field
# without clobbering the rest. Slug changes are uniqueness-checked.
@api_view(["PATCH"])
def admin_edit_organization(request, slug):
    # Auth + AFC-staff gate.
    user, err = _require_platform_admin(request)
    if err:
        return err

    org, err = _org_or_404(slug)
    if err:
        return err

    # ── name ──
    if "name" in request.data:
        new_name = (request.data.get("name") or "").strip()
        if not new_name:
            return Response({"message": "Organization name cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
        org.name = new_name

    # ── slug (must stay unique across all OTHER orgs) ──
    if "slug" in request.data:
        new_slug = slugify(request.data.get("slug") or "")
        if not new_slug:
            return Response({"message": "Slug cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
        if Organization.objects.filter(slug=new_slug).exclude(pk=org.pk).exists():
            return Response({"message": "That slug is already taken."}, status=status.HTTP_400_BAD_REQUEST)
        org.slug = new_slug

    # ── email ──
    if "email" in request.data:
        org.email = request.data.get("email")

    # ── description ──
    if "description" in request.data:
        org.description = request.data.get("description") or ""

    # ── socials (free-form JSON blob: {"x","instagram","youtube","discord"}) ──
    if "socials" in request.data:
        socials = request.data.get("socials")
        # Guard the JSONField against a non-object payload.
        if socials is not None and not isinstance(socials, dict):
            return Response({"message": "socials must be an object."}, status=status.HTTP_400_BAD_REQUEST)
        org.socials = socials or {}

    # ── status (active / suspended / deleted) ──
    if "status" in request.data:
        new_status = request.data.get("status")
        valid_statuses = {choice[0] for choice in Organization.STATUS_CHOICES}
        if new_status not in valid_statuses:
            return Response({"message": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
        org.status = new_status

    org.save()

    return Response(
        {"message": "Organization updated successfully.", "organization": _serialize_org(org)},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5) admin_suspend_organization  (POST, <slug>)
# ──────────────────────────────────────────────────────────────────────────────
# Reversible freeze / unfreeze. body {suspend: bool}. We deliberately never touch
# a "deleted" org here — un-suspending a soft-deleted org would silently resurrect
# it, so suspension only moves between active <-> suspended.
@api_view(["POST"])
def admin_suspend_organization(request, slug):
    # Auth + AFC-staff gate.
    user, err = _require_platform_admin(request)
    if err:
        return err

    org, err = _org_or_404(slug)
    if err:
        return err

    suspend = request.data.get("suspend")
    # Refuse on a soft-deleted org — see header comment.
    if org.status == "deleted":
        return Response({"message": "Cannot change the status of a deleted organization."}, status=status.HTTP_400_BAD_REQUEST)

    # Truthy `suspend` → freeze; falsy → reactivate.
    org.status = "suspended" if suspend else "active"
    org.save()

    return Response(
        {"message": "Organization status updated.", "status": org.status},
        status=status.HTTP_200_OK,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 6) admin_delete_organization  (DELETE, <slug>)
# ──────────────────────────────────────────────────────────────────────────────
# SOFT delete. We never drop the row (events, audit trail, history would vanish).
# Instead: flag the org "deleted", re-home its events back to AFC (organization →
# NULL, which the Event FK is built to allow via SET_NULL), and mark every member
# "removed" so the org disappears from their dashboards.
@api_view(["DELETE"])
def admin_delete_organization(request, slug):
    # Auth + AFC-staff gate.
    user, err = _require_platform_admin(request)
    if err:
        return err

    org, err = _org_or_404(slug)
    if err:
        return err

    # 1) Soft-delete the org itself.
    org.status = "deleted"
    org.save()

    # 2) Re-home every event under this org back to AFC (no orphaned events).
    #    Imported locally to avoid a module-level cross-app import cycle.
    from afc_tournament_and_scrims.models import Event
    Event.objects.filter(organization=org).update(organization=None)

    # 3) Detach all members so the org leaves their dashboards.
    org.members.update(status="removed")

    return Response({"message": "Organization deleted."}, status=status.HTTP_200_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 7) admin_manage_organization_member  (POST, <slug>)
# ──────────────────────────────────────────────────────────────────────────────
# AFC-staff override for an org's roster. Three actions:
#   * "add"       → attach (or reactivate) a member, set role + granted perms,
#                   and grant the platform 'organizer' role;
#   * "remove"    → mark a member "removed" (refused for the owner — an org must
#                   always keep its owner; transfer first via set_owner);
#   * "set_owner" → transfer ownership: demote the current owner to sub_organizer
#                   and promote the target to owner.
@api_view(["POST"])
def admin_manage_organization_member(request, slug):
    # Auth + AFC-staff gate.
    user, err = _require_platform_admin(request)
    if err:
        return err

    org, err = _org_or_404(slug)
    if err:
        return err

    # ── read body ──
    action = request.data.get("action")
    username = (request.data.get("username") or "").strip()

    if action not in ("add", "remove", "set_owner"):
        return Response({"message": "Invalid action."}, status=status.HTTP_400_BAD_REQUEST)
    if not username:
        return Response({"message": "username is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Resolve the target user — every action operates on an existing account.
    target = User.objects.filter(username=username).first()
    if not target:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    # ───────────────────────────── add ─────────────────────────────
    if action == "add":
        # Default new members to sub_organizer unless a valid role is supplied.
        role = request.data.get("role") or "sub_organizer"
        valid_roles = {choice[0] for choice in OrganizationMember.ROLE_CHOICES}
        if role not in valid_roles:
            return Response({"message": "Invalid role."}, status=status.HTTP_400_BAD_REQUEST)

        # Create OR reactivate (the model has unique_together on org+user, so a
        # previously-removed person is updated in place, never duplicated).
        member, _created = OrganizationMember.objects.get_or_create(
            organization=org, user=target,
            defaults={"role": role, "status": "active", "invited_by": user},
        )
        member.role = role
        member.status = "active"

        # Apply only the permission keys that (a) were sent AND (b) are real
        # columns in PERMISSION_FIELDS — never trust arbitrary keys from the body.
        permissions = request.data.get("permissions") or {}
        if isinstance(permissions, dict):
            for field in PERMISSION_FIELDS:
                if field in permissions:
                    setattr(member, field, bool(permissions[field]))
        member.save()

        # Grant the platform 'organizer' role so they can reach the dashboard.
        organizer_role = Roles.objects.get(role_name="organizer")
        UserRoles.objects.get_or_create(user=target, role=organizer_role)

        return Response({"message": "Member added."}, status=status.HTTP_200_OK)

    # ─────────────────────────── remove ────────────────────────────
    if action == "remove":
        member = OrganizationMember.objects.filter(organization=org, user=target).first()
        if not member:
            return Response({"message": "Member not found."}, status=status.HTTP_404_NOT_FOUND)
        # The owner cannot be removed — transfer ownership first (set_owner).
        if member.role == "owner":
            return Response({"message": "Cannot remove the organization owner. Transfer ownership first."}, status=status.HTTP_400_BAD_REQUEST)
        member.status = "removed"
        member.save()
        return Response({"message": "Member removed."}, status=status.HTTP_200_OK)

    # ────────────────────────── set_owner ──────────────────────────
    # action == "set_owner": demote whoever currently owns the org, then promote
    # the target to owner. Target must already be a member of this org.
    new_owner = OrganizationMember.objects.filter(organization=org, user=target).first()
    if not new_owner:
        return Response({"message": "Member not found."}, status=status.HTTP_404_NOT_FOUND)

    # Demote the existing owner(s) to sub_organizer (normally exactly one).
    OrganizationMember.objects.filter(organization=org, role="owner").update(role="sub_organizer")

    # Promote the target to owner and make sure they are active.
    new_owner.role = "owner"
    new_owner.status = "active"
    new_owner.save()

    return Response({"message": "Ownership transferred."}, status=status.HTTP_200_OK)
