"""
afc_organizers.views_leaderboard_design — CRUD for an org's leaderboard DESIGN LIBRARY.

OWNER 2026-06-13: organizers upload branded leaderboard backgrounds (a library of designs),
and when exporting a leaderboard pick which design + size to download. This module is the
library management surface (the export/render itself lives in
afc_leaderboard.views.leaderboard_graphic + afc_leaderboard.graphic).

Model: afc_organizers.OrgLeaderboardDesign (per-org). Write access = org_can(can_submit_designs)
(owner / sub-organizer granted designs, + the platform-admin bypass org_can already applies),
matching the existing LeaderboardDesignRequest gate. Read = any active org member.

A design library is keyed by an optional `organization_id`:
  * organization_id present  -> that organizer's library (gate: org_can(can_submit_designs)).
  * organization_id absent    -> the AFC-NATIVE library (organization = null), managed by AFC
                                 admins (user.role == "admin"), used for AFC's own leaderboards.

ENDPOINTS (mounted under organizers/ via afc_organizers/urls.py)
    GET    organizers/leaderboard-designs/?organization_id=<id?>   designs_collection (read)
    POST   organizers/leaderboard-designs/  body organization_id?  designs_collection (create; multipart)
    PATCH  organizers/leaderboard-designs/by-id/<id>/              design_item        (multipart/json)
    DELETE organizers/leaderboard-designs/by-id/<id>/              design_item

Consumed by: the organizer + admin "Leaderboard designs" page + the leaderboard export picker.
"""
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from afc_organizers.models import Organization, OrgLeaderboardDesign
from afc_organizers.permissions import org_can

from .views_design import _authenticate, _member_or_403


def _resolve_library(request, raw_org_id):
    """Resolve the (organization|None) a design library request targets, and whether the caller
    may WRITE to it. Returns (organization_or_None, can_write, error_response).

    org id given  -> that org; write = org_can(can_submit_designs) (owner/admin bypass inside).
    no org id     -> AFC-native (None); write = AFC staff admin (role == "admin"). AFC admins
                      may also write to ANY org library (is_platform_org_admin bypass)."""
    user = request._afc_user
    if raw_org_id in (None, "", "null"):
        return None, (user.role == "admin"), None
    # Organization's PK field is `organization_id` (not `id`), so resolve by `pk` to stay
    # correct regardless of the field name (filter(id=...) raises FieldError on this model).
    org = Organization.objects.filter(pk=raw_org_id).first()
    if not org:
        return None, False, Response({"message": "Organization not found."}, status=404)
    return org, org_can(user, "can_submit_designs", org), None


def _serialize_design(d):
    return {
        "id": d.id,
        "name": d.name,
        "background_instagram": d.background_instagram.url if d.background_instagram else None,
        "background_youtube": d.background_youtube.url if d.background_youtube else None,
        "text_color": d.text_color,
        "accent_color": d.accent_color,
        "show_title": d.show_title,
        "show_subtitle": d.show_subtitle,
        "max_rows": d.max_rows,
        "is_default": d.is_default,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _apply_fields(d, data):
    """Copy the editable scalar fields from request data onto a design (used by create + edit).
    Image files are handled separately by the caller (request.FILES)."""
    if "name" in data:
        d.name = (data.get("name") or "").strip() or d.name
    if "text_color" in data:
        d.text_color = (data.get("text_color") or "#FFFFFF").strip()
    if "accent_color" in data:
        d.accent_color = (data.get("accent_color") or "#34d27b").strip()
    # Booleans arrive as "true"/"false" strings over multipart.
    for flag in ("show_title", "show_subtitle"):
        if flag in data:
            v = data.get(flag)
            d.__setattr__(flag, str(v).lower() in ("true", "1", "yes", "on", "true"))
    if "max_rows" in data:
        try:
            d.max_rows = max(1, min(50, int(data.get("max_rows"))))
        except (TypeError, ValueError):
            pass


def _unset_other_defaults(org, keep_id):
    """Keep at most one default design per library: clear is_default on every OTHER design in
    the same library (org-scoped, or the AFC-native org=null set)."""
    qs = (OrgLeaderboardDesign.objects.filter(organization__isnull=True)
          if org is None else OrgLeaderboardDesign.objects.filter(organization=org))
    qs.filter(is_default=True).exclude(id=keep_id).update(is_default=False)


def _library_qs(org):
    """The design queryset for a library: an org's designs, or the AFC-native (org=null) set."""
    if org is None:
        return OrgLeaderboardDesign.objects.filter(organization__isnull=True)
    return OrgLeaderboardDesign.objects.filter(organization=org)


@api_view(["GET", "POST"])
def designs_collection(request):
    """GET  organizers/leaderboard-designs/?organization_id=<id?>  — list a library.
    POST organizers/leaderboard-designs/  (body organization_id?) — create one (multipart with
    name + optional background_instagram / background_youtube + style fields).

    organization_id absent = the AFC-native library (AFC admins). Present = that org's library."""
    user, err = _authenticate(request)
    if err:
        return err
    request._afc_user = user

    if request.method == "GET":
        org, can_write, err = _resolve_library(request, request.query_params.get("organization_id"))
        if err:
            return err
        # Read floor: an org member OR any AFC admin for org libraries; AFC admin for the native one.
        # We use user.role == "admin" (not the stricter is_platform_org_admin) so the gate MATCHES
        # can_manage_standalone_lb (afc_leaderboard.permissions), which is what decides whether the
        # leaderboard's "Export graphic" button even shows. Otherwise an AFC admin who can manage an
        # org-owned leaderboard could open the export picker but fail to load that org's designs (403).
        if org is None:
            if user.role != "admin":
                return Response({"message": "Admins only."}, status=status.HTTP_403_FORBIDDEN)
        elif user.role != "admin" and not _member_or_403(user, org):
            return Response({"message": "You do not have access to this organization."},
                            status=status.HTTP_403_FORBIDDEN)
        rows = [_serialize_design(d) for d in _library_qs(org)]
        return Response({"results": rows, "total_count": len(rows)})

    # POST = create
    org, can_write, err = _resolve_library(request, request.data.get("organization_id"))
    if err:
        return err
    if not can_write:
        return Response({"message": "You do not have permission to manage these designs."},
                        status=status.HTTP_403_FORBIDDEN)
    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"message": "A design name is required."}, status=status.HTTP_400_BAD_REQUEST)
    d = OrgLeaderboardDesign(organization=org, name=name, created_by=user)
    _apply_fields(d, request.data)
    if request.FILES.get("background_instagram"):
        d.background_instagram = request.FILES["background_instagram"]
    if request.FILES.get("background_youtube"):
        d.background_youtube = request.FILES["background_youtube"]
    # First design in the library becomes the default automatically.
    if not _library_qs(org).exists():
        d.is_default = True
    else:
        d.is_default = str(request.data.get("is_default")).lower() in ("true", "1", "yes", "on")
    d.save()
    if d.is_default:
        _unset_other_defaults(org, d.id)
    return Response({"design": _serialize_design(d)}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def design_item(request, design_id):
    """PATCH  organizers/leaderboard-designs/by-id/<id>/  — edit (style, name, replace images,
    set default). DELETE — remove a design. Both gated by can_submit_designs on the owning org."""
    user, err = _authenticate(request)
    if err:
        return err
    try:
        d = OrgLeaderboardDesign.objects.select_related("organization").get(id=design_id)
    except OrgLeaderboardDesign.DoesNotExist:
        return Response({"message": "Design not found."}, status=status.HTTP_404_NOT_FOUND)
    # AFC-native design (no org) -> AFC staff admin; org design -> org_can(can_submit_designs).
    can_write = (user.role == "admin") if d.organization_id is None else org_can(
        user, "can_submit_designs", d.organization)
    if not can_write:
        return Response({"message": "You do not have permission to manage this design."},
                        status=status.HTTP_403_FORBIDDEN)

    if request.method == "DELETE":
        d.delete()
        return Response({"message": "Design deleted."})

    # PATCH
    _apply_fields(d, request.data)
    if request.FILES.get("background_instagram"):
        d.background_instagram = request.FILES["background_instagram"]
    if request.FILES.get("background_youtube"):
        d.background_youtube = request.FILES["background_youtube"]
    if "is_default" in request.data:
        d.is_default = str(request.data.get("is_default")).lower() in ("true", "1", "yes", "on")
    d.save()
    if d.is_default:
        _unset_other_defaults(d.organization, d.id)
    return Response({"design": _serialize_design(d)})
