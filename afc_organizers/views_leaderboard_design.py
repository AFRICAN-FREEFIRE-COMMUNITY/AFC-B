"""
afc_organizers.views_leaderboard_design — CRUD for an org's leaderboard DESIGN LIBRARY.

OWNER 2026-06-13: organizers upload branded leaderboard backgrounds (a library of designs),
and when exporting a leaderboard pick which design + size to download. This module is the
library management surface (the export/render itself lives in
afc_leaderboard.views.leaderboard_graphic + afc_leaderboard.graphic).

Model: afc_organizers.OrgLeaderboardDesign (per-org). Write access = org_can(can_submit_designs)
(owner / sub-organizer granted designs, + the platform-admin bypass org_can already applies).
Read = any active org member (or any AFC admin). can_submit_designs is the same permission that
gated the now-removed "request a design" flow.

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
import json

from django.http import FileResponse, Http404
from django.urls import reverse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

from afc_organizers.models import (
    Organization, OrganizationMember, OrgLeaderboardDesign, OrgLeaderboardDesignLogo,
    OrgLeaderboardDesignField, OrgLeaderboardDesignText, OrgLeaderboardDesignFont,
    OrgLeaderboardDesignPage,   # multi-page support (owner 2026-06-14)
)
from afc_organizers.permissions import org_can
from afc.api_utils import authenticate as _authenticate
from afc_organizers.permissions import member_or_403 as _member_or_403

# Allowed values for placed fields (mirror the model choices so the API rejects junk early).
FIELD_TYPES = {c[0] for c in OrgLeaderboardDesignField.FIELD_CHOICES}
ALIGN_VALUES = {"left", "center", "right"}


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


def _abs_url(request, field):
    """Absolute media URL for an ImageField (or None). The frontend lives on a DIFFERENT origin than
    the API (FE :3001 / api :8000 in dev; separate domains in prod), so a relative /media/ URL would
    not resolve in an <img>. We return an absolute URL via build_absolute_uri, matching the org
    logo/banner serialization in views_admin.py. request may be None (renderer uses .path, not URLs)."""
    if not field:
        return None
    if request is not None:
        return request.build_absolute_uri(field.url)
    return field.url


def _serialize_logo(logo, request=None):
    """One positioned logo: its (absolute) media URL + centre position (percent) + size band."""
    return {
        "id": logo.id,
        "image": _abs_url(request, logo.image),
        "x_pct": logo.x_pct,
        "y_pct": logo.y_pct,
        "size": logo.size,
    }


def _serialize_font(f, request=None):
    """An uploaded font library item: its name + a CORS-enabled file URL.

    The `file` URL points at the Django-served font_file view (organizers/leaderboard-fonts/by-id/
    <id>/file/), NOT the raw /media/ URL. The frontend loads this via the browser FontFace API to
    PREVIEW the typeface (canvas cells/text, the font pickers, the §E library). In production /media/
    is served by nginx with NO Access-Control-Allow-Origin, so a cross-origin FontFace load of the
    raw media URL fails and every font fell back to DM Sans (owner 2026-06-21). Serving the bytes
    through Django routes the response through corsheaders, which DOES add the CORS header, so the
    cross-origin font load succeeds. Server-side export is unaffected (it reads f.file.path directly).
    """
    file_url = None
    if f.file:
        path = reverse("organizers_leaderboard_font_file", args=[f.id])
        file_url = request.build_absolute_uri(path) if request is not None else path
    return {
        "id": f.id,
        "name": f.name,
        "file": file_url,
    }


def _serialize_field(f, request=None):
    """One placed/connected column: which stat it binds to + its position/style overrides.
    page_id is null for legacy fields (page 1) or the id of the page they belong to."""
    return {
        "id": f.id,
        "field_type": f.field_type,
        "column_group": f.column_group,
        "x_pct": f.x_pct,
        # Independent YouTube X (owner 2026-06-15); null => the editor falls back to x_pct for YT.
        "x_pct_youtube": f.x_pct_youtube,
        "align": f.align,
        "font_id": f.font_id,
        "font_size_pct": f.font_size_pct,
        "color": f.color,
        "order": f.order,
        "page_id": f.page_id,  # null = legacy/page-1
    }


def _serialize_text(t, request=None):
    """One freeform text element: content + position + style overrides.
    page_id is null for legacy texts (page 1) or the id of the page they belong to."""
    return {
        "id": t.id,
        "text": t.text,
        "x_pct": t.x_pct,
        "y_pct": t.y_pct,
        # Independent YouTube position (owner 2026-06-15); null => editor falls back to x_pct/y_pct.
        "x_pct_youtube": t.x_pct_youtube,
        "y_pct_youtube": t.y_pct_youtube,
        "align": t.align,
        "font_id": t.font_id,
        "font_size_pct": t.font_size_pct,
        "color": t.color,
        "order": t.order,
        "page_id": t.page_id,  # null = legacy/page-1
    }


def _serialize_page(p, request=None):
    """One design page: its page number, backgrounds (absolute URLs), and column groups.
    Fields/texts on this page are NOT embedded here; they are returned in the top-level
    fields/texts arrays on the design with a page_id discriminator.
    Consumed by DesignFieldsEditor.tsx page tabs (frontend) via _serialize_design's pages array."""
    return {
        "id": p.id,
        "page_number": p.page_number,
        "background_instagram": _abs_url(request, p.background_instagram),
        "background_youtube": _abs_url(request, p.background_youtube),
        "column_groups": p.column_groups or [],
        # Independent YouTube geometry (owner 2026-06-15); empty => editor falls back to column_groups.
        "column_groups_youtube": p.column_groups_youtube or [],
    }


def _serialize_design(d, request=None):
    return {
        "id": d.id,
        "name": d.name,
        "background_instagram": _abs_url(request, d.background_instagram),
        "background_youtube": _abs_url(request, d.background_youtube),
        "text_color": d.text_color,
        "accent_color": d.accent_color,
        "show_title": d.show_title,
        "show_subtitle": d.show_subtitle,
        "max_rows": d.max_rows,
        "is_default": d.is_default,
        # Row tiling for each column group (the field-layout path). [] => legacy auto-table.
        "column_groups": d.column_groups or [],
        # Independent YouTube geometry (owner 2026-06-15); empty => editor falls back to column_groups.
        "column_groups_youtube": d.column_groups_youtube or [],
        # Multi-page support (owner 2026-06-14): ordered list of explicit page rows. An EMPTY list
        # means a single-page (legacy) design (backward compatible). The editor only shows page tabs
        # when this is non-empty; export returns a ZIP when len > 1.
        "pages": [_serialize_page(p, request) for p in d.pages.all()],
        # The positioned logos drawn on this design (drag-canvas editor reads x_pct/y_pct/size).
        "logos": [_serialize_logo(l, request) for l in d.logos.all()],
        # The placed/connected data columns + freeform text elements (owner 2026-06-14).
        # page_id discriminates which page each element belongs to (null = legacy/page-1).
        "fields": [_serialize_field(f, request) for f in d.fields.all()],
        "texts": [_serialize_text(t, request) for t in d.texts.all()],
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def build_field_layout(design, size="instagram"):
    """Convert a design's placed fields + freeform texts + column groups into the `field_layout`
    dict the renderer (afc_leaderboard.graphic.render_leaderboard_graphic) consumes, resolving each
    element's font FK to a filesystem PATH. Returns None when the design has no fields placed, so
    the renderer falls back to its legacy auto-table. Imported by the standalone + event graphic
    endpoints; pass the result (plus the matching `rows`) to render_leaderboard_graphic.

    `size` ("instagram"|"youtube") selects which independent layout to render (owner 2026-06-15: IG
    and YT positions/geometry are stored separately). For YouTube we use the *_youtube columns,
    FALLING BACK to the Instagram values whenever a YT value is unset (NULL field x / empty column
    groups), so a design that only has an IG layout renders identically on both sizes."""
    fields = list(design.fields.all())
    if not fields:
        return None

    yt = size == "youtube"

    def _font_path(elem):
        try:
            return elem.font.file.path if (elem.font_id and elem.font and elem.font.file) else None
        except Exception:
            return None

    def _fx(f):
        return f.x_pct_youtube if (yt and f.x_pct_youtube is not None) else f.x_pct

    def _tx(t):
        return t.x_pct_youtube if (yt and t.x_pct_youtube is not None) else t.x_pct

    def _ty(t):
        return t.y_pct_youtube if (yt and t.y_pct_youtube is not None) else t.y_pct

    groups = ((design.column_groups_youtube or design.column_groups) if yt
              else design.column_groups) or []

    return {
        "column_groups": groups,
        "fields": [{
            "field_type": f.field_type,
            "column_group": f.column_group,
            "x_pct": _fx(f),
            "align": f.align,
            "font_path": _font_path(f),
            "font_size_pct": f.font_size_pct,
            "color": f.color,
        } for f in fields],
        "texts": [{
            "text": t.text,
            "x_pct": _tx(t),
            "y_pct": _ty(t),
            "align": t.align,
            "font_path": _font_path(t),
            "font_size_pct": t.font_size_pct,
            "color": t.color,
        } for t in design.texts.all()],
    }


def build_pages_for_export(design, size="instagram"):
    """Return an ordered list of per-page render specs for the multi-page export path.

    `size` ("instagram"|"youtube") selects which independent layout to bake into each page's
    field_layout (owner 2026-06-15). YouTube uses the *_youtube positions/column-groups, falling
    back to the Instagram values when a YT value is unset.

    Each entry is a dict with:
        page_number          : int (1-based)
        background_instagram : ImageField or None (the IG background for this page)
        background_youtube   : ImageField or None (the YT background for this page)
        field_layout         : dict or None (the build_field_layout-shaped result for this page's
                               fields, with font FKs already resolved to filesystem paths)

    When the design has no OrgLeaderboardDesignPage rows (legacy single-page), this returns
    a list with ONE entry built from the design-level backgrounds + the null-page fields/texts,
    which is exactly what the existing single-PNG export does. The export endpoints call this to
    decide single-PNG vs ZIP: len(pages_for_export) == 1 -> single PNG; else ZIP.

    The `size` is NOT chosen here; callers pass the user-requested size separately to
    render_design_all_pages, which picks background_instagram or background_youtube per entry.

    Consumed by leaderboard_graphic (afc_leaderboard.views) and event_stage_graphic
    (afc_tournament_and_scrims.views_event_graphic) when ?page=all is requested."""
    pages_qs = list(design.pages.order_by("page_number"))
    yt = size == "youtube"

    def _font_path(elem):
        try:
            return elem.font.file.path if (elem.font_id and elem.font and elem.font.file) else None
        except Exception:
            return None

    # Per-size pickers: YouTube uses *_youtube, falling back to the Instagram value when unset.
    def _fx(f):
        return f.x_pct_youtube if (yt and f.x_pct_youtube is not None) else f.x_pct

    def _tx(t):
        return t.x_pct_youtube if (yt and t.x_pct_youtube is not None) else t.x_pct

    def _ty(t):
        return t.y_pct_youtube if (yt and t.y_pct_youtube is not None) else t.y_pct

    def _groups(obj):
        return ((obj.column_groups_youtube or obj.column_groups) if yt
                else obj.column_groups) or []

    def _layout(source_obj, fields, texts):
        if not fields:
            return None
        return {
            "column_groups": _groups(source_obj),
            "fields": [{"field_type": f.field_type, "column_group": f.column_group,
                         "x_pct": _fx(f), "align": f.align, "font_path": _font_path(f),
                         "font_size_pct": f.font_size_pct, "color": f.color} for f in fields],
            "texts": [{"text": t.text, "x_pct": _tx(t), "y_pct": _ty(t), "align": t.align,
                        "font_path": _font_path(t), "font_size_pct": t.font_size_pct,
                        "color": t.color} for t in texts],
        }

    if not pages_qs:
        # Single-page (legacy) design: one entry using design-level data + null-page fields/texts.
        return [{
            "page_number": 1,
            "background_instagram": design.background_instagram,
            "background_youtube": design.background_youtube,
            "field_layout": _layout(
                design,
                list(design.fields.filter(page__isnull=True)),
                list(design.texts.filter(page__isnull=True)),
            ),
        }]

    # Multi-page design: one entry per page, with that page's fields + texts.
    return [{
        "page_number": page.page_number,
        "background_instagram": page.background_instagram,
        "background_youtube": page.background_youtube,
        "field_layout": _layout(page, list(page.fields.all()), list(page.texts.all())),
    } for page in pages_qs]


def _can_write_design(user, design):
    """Whether `user` may mutate `design` (and its logos): AFC admin for the AFC-native library
    (no org), else org_can(can_submit_designs) on the owning org. Shared by design_item + the
    logo sub-endpoints so the gate is identical everywhere."""
    if design.organization_id is None:
        return user.role == "admin"
    return org_can(user, "can_submit_designs", design.organization)


def _clamp_pct(value, fallback):
    """Parse a 0..100 percent (logo centre position); fall back on anything malformed."""
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return fallback


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
    # Column-group row tiling (field-layout path). Arrives as a JSON string over multipart or a
    # list over JSON. A malformed value is ignored (keeps the existing groups).
    if "column_groups" in data:
        raw = data.get("column_groups")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        if isinstance(raw, list):
            # Per-size row geometry (owner 2026-06-15): editing the YouTube layout writes
            # column_groups_youtube; otherwise the Instagram column_groups. Empty YT falls back to IG.
            if (data.get("size") or "instagram").lower() == "youtube":
                d.column_groups_youtube = raw
            else:
                d.column_groups = raw


def _unset_other_defaults(org, keep_id):
    """Keep at most one default design per library: clear is_default on every OTHER design in
    the same library (org-scoped, or the AFC-native org=null set)."""
    qs = (OrgLeaderboardDesign.objects.filter(organization__isnull=True)
          if org is None else OrgLeaderboardDesign.objects.filter(organization=org))
    qs.filter(is_default=True).exclude(id=keep_id).update(is_default=False)


def _library_qs(org):
    """The design queryset for a library: an org's designs, or the AFC-native (org=null) set.
    Prefetches logos, fields, texts, AND pages so _serialize_design does not N+1 over each
    design's related rows (the pages array is read by the multi-page editor tabs)."""
    base = (OrgLeaderboardDesign.objects.filter(organization__isnull=True)
            if org is None else OrgLeaderboardDesign.objects.filter(organization=org))
    return base.prefetch_related("logos", "fields", "texts", "pages")


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
        rows = [_serialize_design(d, request) for d in _library_qs(org)]
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
    return Response({"design": _serialize_design(d, request)}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def design_item(request, design_id):
    """PATCH  organizers/leaderboard-designs/by-id/<id>/  — edit (style, name, replace images,
    set default). DELETE — remove a design. Both gated by can_submit_designs on the owning org."""
    user, err = _authenticate(request)
    if err:
        return err
    try:
        d = (OrgLeaderboardDesign.objects.select_related("organization")
             .prefetch_related("logos", "fields", "texts", "pages").get(id=design_id))
    except OrgLeaderboardDesign.DoesNotExist:
        return Response({"message": "Design not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _can_write_design(user, d):
        return Response({"message": "You do not have permission to manage this design."},
                        status=status.HTTP_403_FORBIDDEN)

    if request.method == "DELETE":
        was_default = d.is_default
        org = d.organization
        d.delete()
        # Keep the "one default per library" invariant the create path establishes: if we removed
        # the default, promote the first remaining design (ordering is ["-is_default", "name"]) so
        # the export picker still has a deliberate default to pre-select.
        if was_default:
            qs = (OrgLeaderboardDesign.objects.filter(organization__isnull=True)
                  if org is None else OrgLeaderboardDesign.objects.filter(organization=org))
            if not qs.filter(is_default=True).exists():
                nxt = qs.first()
                if nxt:
                    nxt.is_default = True
                    nxt.save(update_fields=["is_default"])
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
    return Response({"design": _serialize_design(d, request)})


# ───────────────────────── Logo sub-endpoints (positioned logos on a design) ─────────────────────────
# A design carries 0..N logos, each at a centre position (x_pct/y_pct, 0..100) + a size band.
# The drag-canvas editor (LeaderboardDesignsManager) adds/moves/removes them through these.
SIZE_VALUES = {"small", "medium", "large"}


def _get_design_for_write(user, design_id):
    """Resolve a design + gate the caller for writes. Returns (design, error_response).
    Used by the logo, field, text, AND page sub-endpoints so the 404/403 handling is identical.
    Prefetches `pages` so the page endpoints can read d.pages without an extra query."""
    try:
        d = (OrgLeaderboardDesign.objects.select_related("organization")
             .prefetch_related("pages").get(id=design_id))
    except OrgLeaderboardDesign.DoesNotExist:
        return None, Response({"message": "Design not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _can_write_design(user, d):
        return None, Response({"message": "You do not have permission to manage this design."},
                              status=status.HTTP_403_FORBIDDEN)
    return d, None


@api_view(["POST"])
def design_logos(request, design_id):
    """POST organizers/leaderboard-designs/by-id/<design_id>/logos/ — add a logo to a design.

    Multipart body: image (required file) + x_pct + y_pct (0..100 centre position) + size
    (small|medium|large). Returns {logo}. Gated like the design itself (can_submit_designs / AFC
    admin). Consumed by the LeaderboardDesignsManager editor when a staged logo is saved."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err
    image = request.FILES.get("image")
    if not image:
        return Response({"message": "A logo image is required."}, status=status.HTTP_400_BAD_REQUEST)
    size = (request.data.get("size") or "medium").lower()
    logo = OrgLeaderboardDesignLogo.objects.create(
        design=d,
        image=image,
        x_pct=_clamp_pct(request.data.get("x_pct"), 10.0),
        y_pct=_clamp_pct(request.data.get("y_pct"), 10.0),
        size=size if size in SIZE_VALUES else "medium",
    )
    return Response({"logo": _serialize_logo(logo, request)}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def design_logo_item(request, design_id, logo_id):
    """PATCH organizers/leaderboard-designs/by-id/<design_id>/logos/<logo_id>/ — reposition/resize
    (x_pct, y_pct, size). DELETE — remove the logo. Gated like the parent design."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err
    logo = OrgLeaderboardDesignLogo.objects.filter(design=d, id=logo_id).first()
    if not logo:
        return Response({"message": "Logo not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        logo.delete()
        return Response({"message": "Logo removed."})

    # PATCH — only the fields present are changed (drag updates x/y; the dropdown updates size).
    if "x_pct" in request.data:
        logo.x_pct = _clamp_pct(request.data.get("x_pct"), logo.x_pct)
    if "y_pct" in request.data:
        logo.y_pct = _clamp_pct(request.data.get("y_pct"), logo.y_pct)
    if "size" in request.data:
        size = (request.data.get("size") or "").lower()
        if size in SIZE_VALUES:
            logo.size = size
    logo.save()
    return Response({"logo": _serialize_logo(logo, request)})


# ───────────── MULTI-PAGE sub-endpoints (owner 2026-06-14) ─────────────
# A design with >=1 OrgLeaderboardDesignPage rows is treated as multi-page; 0 rows = single-page
# (backward compatible). A page has its own backgrounds + column_groups. Fields/texts created
# after a page exists carry a page_id FK so they are scoped to that page on the canvas.
# Mounted in afc_organizers/urls.py (.../by-id/<design_id>/pages/ + .../pages/<page_id>/);
# consumed by the DesignFieldsEditor.tsx "Add page" tab action + the per-page background/groups save.


def _next_page_number(design):
    """The next page_number for a new page (max existing + 1, at least 2 since page 1 is implicit)."""
    existing = list(design.pages.values_list("page_number", flat=True))
    return max(existing, default=1) + 1


@api_view(["POST"])
def design_pages(request, design_id):
    """POST organizers/leaderboard-designs/by-id/<design_id>/pages/ — create a new page.

    Creates a new OrgLeaderboardDesignPage for the design. page_number auto-increments
    (max existing + 1). Accepts multipart: background_instagram (file), background_youtube (file),
    column_groups (JSON string). If this is the FIRST explicit page POST, page 1 is created
    implicitly first (copying the design-level backgrounds and column_groups) so the
    caller's newly created page becomes page 2, and the design-level data acts as page 1.

    Response 201: {"page": <page dict>, "design": <updated design dict>}.
    Consumed by: DesignFieldsEditor "Add page" tab action."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err

    # If this is the very first page POST on this design, auto-create page 1 first, carrying
    # over the design-level backgrounds + BOTH column-group layouts (IG + YT) so no data is lost
    # for the current layout.
    if not d.pages.exists():
        page_one = OrgLeaderboardDesignPage.objects.create(
            design=d,
            page_number=1,
            background_instagram=d.background_instagram or None,
            background_youtube=d.background_youtube or None,
            column_groups=list(d.column_groups or []),
            column_groups_youtube=list(d.column_groups_youtube or []),
        )
        # Re-home the design's existing legacy (page_id=NULL) fields/texts onto the freshly
        # materialised page 1. Without this they keep page_id=NULL while explicit pages now exist,
        # so the editor (which filters fields/texts by page_id === currentPageId, and defaults the
        # first tab to page 1's id) would render page 1 EMPTY and the org/admin would lose every
        # column + text element the moment they add a second page. Consumed by DesignFieldsEditor's
        # initial-load + page-switch effects; the frontend comment there anticipates this reassignment.
        d.fields.filter(page__isnull=True).update(page=page_one)
        d.texts.filter(page__isnull=True).update(page=page_one)

    # Now create the new page (page 2, 3, ...).
    new_page_num = _next_page_number(d)
    # Parse column_groups from a JSON string if provided (multipart arrives as string).
    raw_cg = request.data.get("column_groups")
    cg = []
    if raw_cg:
        try:
            parsed = json.loads(raw_cg) if isinstance(raw_cg, str) else raw_cg
            if isinstance(parsed, list):
                cg = parsed
        except (TypeError, ValueError):
            pass

    page = OrgLeaderboardDesignPage(design=d, page_number=new_page_num, column_groups=cg)
    if request.FILES.get("background_instagram"):
        page.background_instagram = request.FILES["background_instagram"]
    if request.FILES.get("background_youtube"):
        page.background_youtube = request.FILES["background_youtube"]
    page.save()

    # Re-fetch design with all related to include updated pages in the response.
    d_fresh = (OrgLeaderboardDesign.objects.select_related("organization")
               .prefetch_related("logos", "fields", "texts", "pages").get(id=design_id))
    return Response(
        {"page": _serialize_page(page, request), "design": _serialize_design(d_fresh, request)},
        status=status.HTTP_201_CREATED,
    )


@api_view(["PATCH", "DELETE"])
def design_page_item(request, design_id, page_id):
    """PATCH/DELETE organizers/leaderboard-designs/by-id/<design_id>/pages/<page_id>/.

    PATCH: update backgrounds (multipart) and/or column_groups (JSON string). Only keys present
    are changed. Returns {"page": <updated page dict>}.
    DELETE: remove the page (cascades its fields + texts). If the design has only 1 page row
    left after deletion, that row is also removed (returning to single-page / design-level mode).
    Returns {"message": "Page removed."}.
    Response 403 if caller cannot write; 404 if page not found on this design."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err

    page = OrgLeaderboardDesignPage.objects.filter(design=d, id=page_id).first()
    if not page:
        return Response({"message": "Page not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        page.delete()
        # Collapse back to single-page mode if only one page remains (no multi-page value).
        remaining = d.pages.all()
        if remaining.count() == 1:
            remaining.first().delete()
        return Response({"message": "Page removed."})

    # PATCH: update only the keys present.
    if request.FILES.get("background_instagram"):
        page.background_instagram = request.FILES["background_instagram"]
    if request.FILES.get("background_youtube"):
        page.background_youtube = request.FILES["background_youtube"]
    if "column_groups" in request.data:
        raw_cg = request.data.get("column_groups")
        try:
            parsed = json.loads(raw_cg) if isinstance(raw_cg, str) else raw_cg
            if isinstance(parsed, list):
                # Per-size geometry (owner 2026-06-15): editing the YouTube layout writes
                # column_groups_youtube; otherwise the Instagram column_groups.
                if (request.data.get("size") or "instagram").lower() == "youtube":
                    page.column_groups_youtube = parsed
                else:
                    page.column_groups = parsed
        except (TypeError, ValueError):
            pass
    page.save()
    return Response({"page": _serialize_page(page, request)})


@api_view(["POST"])
def apply_background_to_all(request, design_id):
    """POST organizers/leaderboard-designs/by-id/<design_id>/apply-background-to-all/ (owner 2026-06-27)

    Apply ONE uploaded background image to EVERY page of a multi-page design at once (or to the
    design-level background when the design is still single-page / has no explicit page rows). Saves the
    owner/admin from re-uploading the same backdrop on each page tab.

    Multipart body (at least one file required):
      • background_instagram  — the IG/portrait backdrop to apply to all pages
      • background_youtube     — the YT/landscape backdrop to apply to all pages
    Whichever file(s) are present are applied; the other size is left untouched on every page.

    Response 200: {"design": <updated design dict>} (full design so the editor refreshes every page).
    400 when no file is supplied. Gate: _get_design_for_write (same as design_item PATCH / page edits).
    Consumed by: DesignFieldsEditor "Apply to all pages" background control."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err

    ig_file = request.FILES.get("background_instagram")
    yt_file = request.FILES.get("background_youtube")
    if not ig_file and not yt_file:
        return Response(
            {"message": "Upload at least one background image to apply."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Read each upload's bytes ONCE. A single uploaded file object can't be re-saved onto many model
    # instances (its stream is consumed on the first save), so we wrap the bytes in a fresh ContentFile
    # per target — each page/design gets its own stored copy. Mirrors how other multipart saves here
    # take request.FILES[...] directly, just fanned out to N targets.
    from django.core.files.base import ContentFile
    ig_bytes = ig_file.read() if ig_file else None
    yt_bytes = yt_file.read() if yt_file else None

    def _copy(src_file, data):
        return ContentFile(data, name=getattr(src_file, "name", "background"))

    pages = list(d.pages.all())
    if pages:
        # Multi-page: stamp the backdrop on every page.
        for p in pages:
            if ig_bytes is not None:
                p.background_instagram = _copy(ig_file, ig_bytes)
            if yt_bytes is not None:
                p.background_youtube = _copy(yt_file, yt_bytes)
            p.save()
    else:
        # Single-page design: apply to the design-level background fields (page 1 is implicit).
        if ig_bytes is not None:
            d.background_instagram = _copy(ig_file, ig_bytes)
        if yt_bytes is not None:
            d.background_youtube = _copy(yt_file, yt_bytes)
        d.save()

    d_fresh = (OrgLeaderboardDesign.objects.select_related("organization")
               .prefetch_related("logos", "fields", "texts", "pages").get(id=design_id))
    return Response({"design": _serialize_design(d_fresh, request)}, status=status.HTTP_200_OK)


# ───────────── Connected-column FIELDS (placed data columns on a design) ─────────────
# Each field binds to a real standings stat and is drawn at x_pct for every row of its column
# group. The connected-columns palette + drag canvas in LeaderboardDesignsManager manage these.

def _resolve_font_for(design, font_id):
    """Resolve a font id to a font that belongs to the SAME library as `design` (same org, or both
    AFC-native). Returns the font, or None (clear / not found / wrong library). Keeps a design from
    referencing another org's font."""
    if font_id in (None, "", "null", 0, "0"):
        return None
    f = OrgLeaderboardDesignFont.objects.filter(id=font_id).first()
    if f and f.organization_id == design.organization_id:
        return f
    return None


def _apply_field_attrs(f, data, design):
    """Copy editable field attributes from request data (used by create + patch). field_type is set
    by the caller on create only (it is the binding and never changes after)."""
    if "column_group" in data:
        try:
            f.column_group = max(0, int(data.get("column_group")))
        except (TypeError, ValueError):
            pass
    # Per-size position (owner 2026-06-15): a drag while editing the YouTube layout sends
    # size="youtube" and writes x_pct_youtube; otherwise it writes the Instagram x_pct. Create
    # (no size) writes the IG x_pct; the YT value stays NULL and falls back to IG until edited.
    if "x_pct" in data:
        if (data.get("size") or "instagram").lower() == "youtube":
            f.x_pct_youtube = _clamp_pct(
                data.get("x_pct"),
                f.x_pct_youtube if f.x_pct_youtube is not None else f.x_pct,
            )
        else:
            f.x_pct = _clamp_pct(data.get("x_pct"), f.x_pct)
    if "align" in data:
        a = (data.get("align") or "").lower()
        if a in ALIGN_VALUES:
            f.align = a
    if "font_id" in data:
        f.font = _resolve_font_for(design, data.get("font_id"))
    if "font_size_pct" in data:
        raw = data.get("font_size_pct")
        try:
            f.font_size_pct = float(raw) if raw not in (None, "", "null") else None
        except (TypeError, ValueError):
            f.font_size_pct = None
    if "color" in data:
        f.color = (data.get("color") or "").strip()
    if "order" in data:
        try:
            f.order = max(0, int(data.get("order")))
        except (TypeError, ValueError):
            pass


@api_view(["POST"])
def design_fields(request, design_id):
    """POST organizers/leaderboard-designs/by-id/<design_id>/fields/ — add a connected column.
    Body: field_type (required, must be a known stat) + column_group + x_pct + align + font_id? +
    font_size_pct? + color?. Returns {field}."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err
    ft = (request.data.get("field_type") or "").strip()
    if ft not in FIELD_TYPES:
        return Response({"message": "Unknown field type."}, status=status.HTTP_400_BAD_REQUEST)
    f = OrgLeaderboardDesignField(design=d, field_type=ft, x_pct=_clamp_pct(request.data.get("x_pct"), 10.0))
    _apply_field_attrs(f, request.data, d)
    # Optional page scoping (multi-page, owner 2026-06-14). page_id null = legacy / page-1 layout.
    # When the editor's currentPageId is a real page, this binds the field to that page so the
    # export ZIP slices it onto the right page (build_pages_for_export reads page.fields).
    page_id = request.data.get("page_id")
    if page_id not in (None, "", "null", "0", 0):
        pg = OrgLeaderboardDesignPage.objects.filter(design=d, id=page_id).first()
        if pg:
            f.page = pg
    f.save()
    return Response({"field": _serialize_field(f, request)}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def design_field_item(request, design_id, field_id):
    """PATCH .../fields/<field_id>/ — reposition/restyle a column. DELETE — remove it."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err
    f = OrgLeaderboardDesignField.objects.filter(design=d, id=field_id).first()
    if not f:
        return Response({"message": "Field not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        f.delete()
        return Response({"message": "Field removed."})
    _apply_field_attrs(f, request.data, d)
    f.save()
    return Response({"field": _serialize_field(f, request)})


# ───────────── Freeform TEXT elements ─────────────

def _apply_text_attrs(t, data, design):
    if "text" in data:
        t.text = (data.get("text") or "")[:200]
    # Per-size position (owner 2026-06-15): editing the YouTube layout (size="youtube") writes the
    # *_youtube position; otherwise the Instagram position. YT NULL falls back to IG until edited.
    yt = (data.get("size") or "instagram").lower() == "youtube"
    if "x_pct" in data:
        if yt:
            t.x_pct_youtube = _clamp_pct(
                data.get("x_pct"), t.x_pct_youtube if t.x_pct_youtube is not None else t.x_pct,
            )
        else:
            t.x_pct = _clamp_pct(data.get("x_pct"), t.x_pct)
    if "y_pct" in data:
        if yt:
            t.y_pct_youtube = _clamp_pct(
                data.get("y_pct"), t.y_pct_youtube if t.y_pct_youtube is not None else t.y_pct,
            )
        else:
            t.y_pct = _clamp_pct(data.get("y_pct"), t.y_pct)
    if "align" in data:
        a = (data.get("align") or "").lower()
        if a in ALIGN_VALUES:
            t.align = a
    if "font_id" in data:
        t.font = _resolve_font_for(design, data.get("font_id"))
    if "font_size_pct" in data:
        raw = data.get("font_size_pct")
        try:
            t.font_size_pct = float(raw) if raw not in (None, "", "null") else None
        except (TypeError, ValueError):
            t.font_size_pct = None
    if "color" in data:
        t.color = (data.get("color") or "").strip() or "#FFFFFF"
    if "order" in data:
        try:
            t.order = max(0, int(data.get("order")))
        except (TypeError, ValueError):
            pass


@api_view(["POST"])
def design_texts(request, design_id):
    """POST .../by-id/<design_id>/texts/ — add a freeform text element. Body: text + x_pct + y_pct
    + align + font_id? + font_size_pct? + color?."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err
    t = OrgLeaderboardDesignText(design=d, text=(request.data.get("text") or "")[:200] or "Text")
    _apply_text_attrs(t, request.data, d)
    # Optional page scoping (multi-page, owner 2026-06-14), mirroring design_fields. null page_id =
    # legacy / page-1. Binds the freeform text to a page so build_pages_for_export draws it on the
    # right page of the export ZIP (it reads page.texts) and DesignFieldsEditor.tsx filters by page.
    page_id = request.data.get("page_id")
    if page_id not in (None, "", "null", "0", 0):
        pg = OrgLeaderboardDesignPage.objects.filter(design=d, id=page_id).first()
        if pg:
            t.page = pg
    t.save()
    return Response({"text": _serialize_text(t, request)}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def design_text_item(request, design_id, text_id):
    """PATCH .../texts/<text_id>/ — edit a freeform text. DELETE — remove it."""
    user, err = _authenticate(request)
    if err:
        return err
    d, err = _get_design_for_write(user, design_id)
    if err:
        return err
    t = OrgLeaderboardDesignText.objects.filter(design=d, id=text_id).first()
    if not t:
        return Response({"message": "Text not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        t.delete()
        return Response({"message": "Text removed."})
    _apply_text_attrs(t, request.data, d)
    t.save()
    return Response({"text": _serialize_text(t, request)})


# ───────────── FONT library (uploaded TTF/OTF, org-scoped or AFC-native) ─────────────
FONT_EXTS = (".ttf", ".otf")


@api_view(["GET", "POST"])
def fonts_collection(request):
    """GET organizers/leaderboard-fonts/?organization_id=<id?> — list a font library.
    POST — upload a font (multipart: file + name?). org id absent = AFC-native library. Read floor
    = org member or AFC admin (mirrors designs); write = can_submit_designs / AFC admin."""
    user, err = _authenticate(request)
    if err:
        return err
    request._afc_user = user
    if request.method == "GET":
        org, can_write, err = _resolve_library(request, request.query_params.get("organization_id"))
        if err:
            return err
        if org is None:
            if user.role != "admin":
                return Response({"message": "Admins only."}, status=status.HTTP_403_FORBIDDEN)
        elif user.role != "admin" and not _member_or_403(user, org):
            return Response({"message": "You do not have access to this organization."},
                            status=status.HTTP_403_FORBIDDEN)
        qs = (OrgLeaderboardDesignFont.objects.filter(organization__isnull=True)
              if org is None else OrgLeaderboardDesignFont.objects.filter(organization=org))
        rows = [_serialize_font(f, request) for f in qs]
        return Response({"results": rows, "total_count": len(rows)})

    # POST = upload
    org, can_write, err = _resolve_library(request, request.data.get("organization_id"))
    if err:
        return err
    if not can_write:
        return Response({"message": "You do not have permission to manage these fonts."},
                        status=status.HTTP_403_FORBIDDEN)
    upload = request.FILES.get("file")
    if not upload:
        return Response({"message": "A font file is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not upload.name.lower().endswith(FONT_EXTS):
        return Response({"message": "Only .ttf or .otf font files are allowed."},
                        status=status.HTTP_400_BAD_REQUEST)
    name = (request.data.get("name") or "").strip() or upload.name.rsplit(".", 1)[0][:80]
    f = OrgLeaderboardDesignFont.objects.create(
        organization=org, name=name, file=upload, created_by=user)
    return Response({"font": _serialize_font(f, request)}, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
def font_item(request, font_id):
    """DELETE organizers/leaderboard-fonts/by-id/<font_id>/ — remove an uploaded font. Fields/texts
    referencing it fall back to the default font (FK is SET_NULL). Gated like its library."""
    user, err = _authenticate(request)
    if err:
        return err
    f = OrgLeaderboardDesignFont.objects.select_related("organization").filter(id=font_id).first()
    if not f:
        return Response({"message": "Font not found."}, status=status.HTTP_404_NOT_FOUND)
    can_write = (user.role == "admin") if f.organization_id is None else org_can(
        user, "can_submit_designs", f.organization)
    if not can_write:
        return Response({"message": "You do not have permission to manage this font."},
                        status=status.HTTP_403_FORBIDDEN)
    f.delete()
    return Response({"message": "Font removed."})


@api_view(["GET"])
@permission_classes([AllowAny])
def font_file(request, font_id):
    """GET organizers/leaderboard-fonts/by-id/<font_id>/file/ — stream a font's raw bytes.

    WHY THIS EXISTS (owner 2026-06-21 "see how the fonts will look"): uploaded fonts live under
    /media/, which in PRODUCTION is served by nginx directly and therefore carries NO
    Access-Control-Allow-Origin header. The browser FontFace API (used by DesignFieldsEditor to
    preview a typeface in the canvas, the three font pickers, and the §E font library) requires CORS
    for ANY cross-origin font, so loading a /media/ font from the frontend origin silently failed in
    prod and every font fell back to DM Sans. Serving the SAME bytes through this Django view routes
    the response through corsheaders (CORS_ORIGIN_ALLOW_ALL=True), which adds the CORS header the
    cross-origin FontFace load needs, so the preview works. (Local dev already worked because there
    Django serves /media/ itself, through corsheaders.)

    PUBLIC (AllowAny): a cross-origin FontFace fetch cannot attach the auth cookie/header, and a font
    file is not sensitive. Consumed by: frontend/lib/leaderboardDesigns.ts LeaderboardDesignFont.file
    -> DesignFieldsEditor.tsx FontFace loader. The serialized `file` URL (see _serialize_font) points
    here. Server-side PNG export is unaffected (it reads f.file.path on disk, never this URL).
    """
    f = OrgLeaderboardDesignFont.objects.filter(id=font_id).first()
    if not f or not f.file:
        raise Http404("Font not found.")
    # Content type per extension (default to TTF). The FE only needs the bytes; the @font-face format
    # is inferred by the browser regardless, but a correct type keeps caches/proxies happy.
    name = f.file.name.lower()
    ctype = "font/otf" if name.endswith(".otf") else "font/ttf"
    resp = FileResponse(f.file.open("rb"), content_type=ctype)
    # Font bytes are immutable for a given id, and the FE loads each once per session, so cache hard.
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp
