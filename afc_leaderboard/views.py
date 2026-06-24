"""
afc_leaderboard.views — REST endpoints for Standalone Leaderboards (Phase 1).

PURPOSE
    All create/list/detail/edit/delete + participant + map + results endpoints for the event-less
    "standalone" leaderboard feature. An AFC admin (org = null) or an organizer with
    `can_upload_results` (org = their org) builds a leaderboard, adds real-or-ghost participants,
    adds maps, enters per-map results, and publishes it; standings are computed on read.

HOUSE IDIOMS (mirrors afc_auth.views / afc_tournament_and_scrims.views / afc_partner_api)
    - Function-based @api_view. Auth via Bearer SessionToken using afc_auth.views.validate_token
      (wrapped in _auth_user below → returns (user, error_Response)).
    - Errors: Response({"message": ...}, status=4xx).
    - Pagination: the afc_partner_api envelope {results, has_more, next_offset, total_count}
      (limit<=100 default 25, offset>=0).
    - Permissions: afc_leaderboard.permissions.can_manage_standalone_lb / can_set_rankings_flag,
      which themselves reuse _is_event_admin + org_can.

HOW IT CONNECTS
    - Point math: afc_tournament_and_scrims.scoring.compute_team_points / compute_solo_points
      (single source of truth) — called in save_match_results.
    - Standings: afc_leaderboard.standings.standalone_standings — called in leaderboard_detail.
    - Ghosts: afc_rankings.GhostTeam / GhostPlayer, created inline in add_participant.
    - Orgs: afc_organizers.models.Organization / OrganizationMember + org_can for org scoping.
    - Consumed by the FE wizard (/a/leaderboards/standalone/create) + view page
      (/leaderboards/standalone/<id>) + list sections on /a/leaderboards and /organizer/leaderboards,
      via frontend/lib/standaloneLeaderboards.ts.

ENDPOINTS (mounted at leaderboards/standalone/… via afc/urls.py → afc_leaderboard/urls.py)
    POST   leaderboards/standalone/                       create_leaderboard
    GET    leaderboards/standalone/                       list_leaderboards         (paginated)
    GET    leaderboards/standalone/<id>/                  leaderboard_detail        (+ standings)
    PATCH  leaderboards/standalone/<id>/                  edit_leaderboard          (incl. publish)
    DELETE leaderboards/standalone/<id>/                  delete_leaderboard
    POST   leaderboards/standalone/<id>/participants/     add_participant           (real|ghost_new|ghost_existing)
    DELETE leaderboards/standalone/<id>/participants/<pid>/  remove_participant
    POST   leaderboards/standalone/<id>/matches/          add_match
    DELETE leaderboards/standalone/matches/<mid>/         delete_match
    POST   leaderboards/standalone/matches/<mid>/results/ save_match_results        (bulk compute+store)
"""
import datetime
import io
import uuid
import zipfile

from django.core.exceptions import ValidationError
from django.db import transaction

from rest_framework.decorators import api_view
from rest_framework.response import Response

from django.db.models import Q

from afc_auth.views import validate_token
from afc_auth.models import User
from afc_team.models import Team, TeamMembers
from afc_organizers.models import Organization, OrganizationMember
from afc_organizers.permissions import org_can
from afc_rankings.models import GhostTeam, GhostPlayer
from afc_tournament_and_scrims.views import _is_event_admin
from afc_tournament_and_scrims.scoring import (
    normalize_placement_points,
    compute_team_points,
    compute_solo_points,
)
# P2 OCR assist — the shared extraction service + platform-wide matchers. extract.extract_rows is
# the SAME local-first-then-Gemini router the event OCR flow uses; the matching helpers un-gate the
# candidate pool to the whole platform (a standalone leaderboard has no event roster).
from afc_ocr.services import extract
from afc_ocr.services.matching import (
    all_platform_players, all_platform_teams_with_ghosts, match_team_name, match_name,
)

from .models import (
    StandaloneLeaderboard,
    LeaderboardParticipant,
    LeaderboardMatch,
    ParticipantMatchResult,
    LeaderboardOcrJob,
    LeaderboardOcrImage,
)
from .permissions import can_manage_standalone_lb, can_set_rankings_flag
from .standings import standalone_standings
# Row builders live in afc_leaderboard.ocr (the testable OCR-engine layer) so the batch worker and the
# legacy single-shot endpoint share one row shape. Aliased to the original private names so ocr_extract
# below is unchanged. process_leaderboard_ocr_job is the Celery task the batch endpoints enqueue.
from .ocr import (
    build_team_ocr_rows as _build_team_ocr_rows,
    build_solo_ocr_rows as _build_solo_ocr_rows,
    build_rows_from_match_log as _build_rows_from_match_log,
)
from .tasks import process_leaderboard_ocr_job
# Match-log file parser (the "upload result file" option) — shared format knowledge in utils.
from utils.match_log import parse_team_match_log
# Punctuation/leet-insensitive search (same util search-teams / search-users use), for the
# ghost-team / ghost-player typeahead endpoints below.
from utils.search_utils import normalized_column, separator_stripped


# ── pagination (afc_partner_api envelope) ────────────────────────────────────────────────────
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# Valid ranking_tier values, taken straight from the model choices so the view and the model can
# never drift. Used by _apply_rankings_feed_fields below.
_VALID_RANKING_TIERS = {c[0] for c in StandaloneLeaderboard.RANKING_TIER_CHOICES}


def _page_params(request):
    """Parse + sanitize ?limit / ?offset. Caps limit at MAX_LIMIT, floors offset at 0, falls back
    to safe defaults on malformed input (never 500s on a bad query string)."""
    try:
        limit = min(int(request.GET.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
        offset = int(request.GET.get("offset", 0))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT, 0
    return (limit if limit >= 1 else DEFAULT_LIMIT, offset if offset >= 0 else 0)


def _envelope(rows, total, offset, limit):
    """The shared {results, has_more, next_offset, total_count} pagination envelope."""
    nxt = offset + limit
    has_more = nxt < total
    return Response({
        "results": rows,
        "has_more": has_more,
        "next_offset": nxt if has_more else None,
        "total_count": total,
    })


# ── auth helper ──────────────────────────────────────────────────────────────────────────────
def _auth_user(request):
    """Resolve the Bearer SessionToken to a User (house idiom). Returns (user, None) on success or
    (None, Response) carrying the 400 (missing/bad header) / 401 (invalid/expired token) error."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ", 1)[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    return user, None


# ── serialization helpers ────────────────────────────────────────────────────────────────────
def _serialize_lb(lb):
    """Compact header dict for list rows + the top of the detail payload."""
    return {
        "id": lb.id,
        "name": lb.name,
        "format": lb.format,
        "organization_id": lb.organization_id,
        "organization_name": lb.organization.name if lb.organization_id else None,
        "placement_points": lb.placement_points,
        "kill_point": lb.kill_point,
        "points_per_assist": lb.points_per_assist,
        "points_per_1000_damage": lb.points_per_1000_damage,
        "counts_toward_rankings": lb.counts_toward_rankings,
        # P3 rankings-feed config (only meaningful when counts_toward_rankings; AFC-admin-set). The FE
        # reveals these inside the rankings block. Consumed by afc_rankings.standalone via the model's
        # effective_date / ranking_tier when this LB feeds the engine.
        "played_on": lb.played_on.isoformat() if lb.played_on else None,
        "ranking_tier": lb.ranking_tier,
        "status": lb.status,
        "creator_id": lb.creator_id,
        "created_at": lb.created_at.isoformat() if lb.created_at else None,
        "updated_at": lb.updated_at.isoformat() if lb.updated_at else None,
    }


def _serialize_participant(p):
    """One participant row (real or ghost), with the entity id so the FE can re-select it."""
    return {
        "id": p.id,
        "name": p.display_name,
        "is_ghost": p.is_ghost,
        "kind": p.kind,
        "team_id": p.team_id,
        "ghost_team_id": str(p.ghost_team_id) if p.ghost_team_id else None,
        "user_id": p.user_id,
        "ghost_player_id": p.ghost_player_id,
    }


def _serialize_match(m):
    """One map row."""
    return {
        "id": m.id,
        "match_number": m.match_number,
        "match_map": m.match_map,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _get_lb_or_404(lb_id):
    """Fetch a leaderboard or return (None, Response 404)."""
    try:
        return StandaloneLeaderboard.objects.get(id=lb_id), None
    except StandaloneLeaderboard.DoesNotExist:
        return None, Response({"message": "Leaderboard not found."}, status=404)


def _resolve_organization_for_create(user, organization_id):
    """
    Decide the owning org for a NEW leaderboard, enforcing the ownership rules (spec §5):
      - AFC admin: organization_id may be null (AFC-native) or any existing org.
      - Organizer (not AFC admin): organization is FORCED to an org they belong to with
        `can_upload_results`. If they pass an org they cannot upload to (or none), reject.
    Returns (organization_or_None, error_Response).
    """
    if _is_event_admin(user):
        # AFC admin: honor whatever they sent (null = AFC-native).
        if organization_id in (None, "", 0):
            return None, None
        try:
            return Organization.objects.get(organization_id=organization_id), None
        except Organization.DoesNotExist:
            return None, Response({"message": "Organization not found."}, status=400)

    # Organizer path: they MUST own/upload-to the org they pick.
    if organization_id in (None, "", 0):
        return None, Response(
            {"message": "An organizer must create the leaderboard under their organization."},
            status=403,
        )
    try:
        org = Organization.objects.get(organization_id=organization_id)
    except Organization.DoesNotExist:
        return None, Response({"message": "Organization not found."}, status=400)

    # Reuse org_can so the "owner implicitly, or sub_organizer with can_upload_results" rule stays
    # in one place.
    if not org_can(user, "can_upload_results", org):
        return None, Response(
            {"message": "You do not have permission to create a leaderboard for this organization."},
            status=403,
        )
    return org, None


def _apply_rankings_feed_fields(lb, data, user):
    """Apply the P3 rankings-feed fields (played_on + ranking_tier) onto `lb` from request `data`,
    ONLY when `user` may set the rankings flag (AFC admin). Mirrors the counts_toward_rankings gating
    exactly: an organizer's values are silently ignored (the keys are skipped), never an error.

    Validates each present field:
      - ranking_tier must be one of the model choices (tier_1/tier_2/tier_3) else returns a 400.
      - played_on must be a YYYY-MM-DD string, or null/empty (clears it) else returns a 400.
    Mutates `lb` in place (the caller saves). Returns an error Response on bad input, else None.

    Used by create_leaderboard + edit_leaderboard. The two columns are read downstream by
    afc_rankings.standalone (effective_date / ranking_tier) when the LB feeds the rankings engine.
    """
    # Non-admins never set these (same rule as counts_toward_rankings) — skip silently.
    if not can_set_rankings_flag(user):
        return None

    if "ranking_tier" in data:
        tier = (data.get("ranking_tier") or "").strip()
        if tier not in _VALID_RANKING_TIERS:
            return Response(
                {"message": "ranking_tier must be one of 'tier_1', 'tier_2', or 'tier_3'."},
                status=400,
            )
        lb.ranking_tier = tier

    if "played_on" in data:
        raw = data.get("played_on")
        if raw in (None, ""):
            lb.played_on = None  # explicit clear -> effective_date falls back to created_at
        else:
            try:
                lb.played_on = datetime.date.fromisoformat(str(raw).strip())
            except (TypeError, ValueError):
                return Response(
                    {"message": "played_on must be a date in YYYY-MM-DD format, or null."},
                    status=400,
                )
    return None


# ════════════════════════════════════════════════════════════════════════════════════════════
# Task 4 — CRUD
#
# NOTE on routing: the spec §4 lists RESTful paths (POST/GET on `standalone/`, GET/PATCH/DELETE on
# `standalone/<id>/`). The existing AFC codebase, however, gives each handler its OWN verb-suffixed
# path (e.g. create-team/, edit-team/, disband-team/). We follow the repo's actual idiom here — one
# function view per URL — so urls.py mounts list at `standalone/`, create at `standalone/create/`,
# detail at `standalone/<id>/`, edit at `standalone/<id>/edit/`, delete at `standalone/<id>/delete/`.
# Each view still guards its own HTTP method via @api_view.
# ════════════════════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def create_leaderboard(request):
    """
    POST leaderboards/standalone/  — create a draft standalone leaderboard.

    Auth: Bearer SessionToken. Must be an AFC admin OR an organizer with can_upload_results on the
    target org (enforced by _resolve_organization_for_create).
    Request body:
        {
          "name": str (required),
          "format": "team" | "solo" (required),
          "placement_points": {"1":12,...} (optional, defaults to scoring.DEFAULT_PLACEMENT shape),
          "kill_point": float (optional, default 1.0),
          "points_per_assist": float (optional, default 0.0),
          "points_per_1000_damage": float (optional, default 0.0),
          "organization_id": int | null (AFC admin only may send null; organizer forced to own org),
          "counts_toward_rankings": bool (AFC admin only; forced False otherwise),
          "played_on": "YYYY-MM-DD" | null (P3, AFC admin only; rankings bucket date),
          "ranking_tier": "tier_1"|"tier_2"|"tier_3" (P3, AFC admin only; default tier_3)
        }
    Response 201: { "leaderboard": <header dict> }
    Errors: 400 missing/invalid name or format / bad ranking_tier / bad played_on;
            403 organizer without org / without can_upload_results.
    Consumed by: the wizard BasicsStep via standaloneLeaderboards.create().
    """
    user, err = _auth_user(request)
    if err:
        return err

    data = request.data or {}
    name = (data.get("name") or "").strip()
    fmt = (data.get("format") or "").strip()
    if not name:
        return Response({"message": "name is required."}, status=400)
    if fmt not in ("team", "solo"):
        return Response({"message": "format must be 'team' or 'solo'."}, status=400)

    # Ownership + org scoping (spec §5): AFC admin may own AFC-native (null) or any org; organizer
    # is forced to an org they can upload results to.
    org, org_err = _resolve_organization_for_create(user, data.get("organization_id"))
    if org_err:
        return org_err

    # counts_toward_rankings is AFC-admin-only. Force False for anyone who cannot set the flag,
    # regardless of what they sent (organizers never get the rankings feed in Phase 1).
    wants_rankings = bool(data.get("counts_toward_rankings"))
    counts_toward_rankings = wants_rankings if can_set_rankings_flag(user) else False

    # Build the row unsaved so the P3 rankings-feed fields (played_on + ranking_tier) can be applied
    # + validated BEFORE the INSERT — a bad tier/date returns 400 without creating a leaderboard.
    lb = StandaloneLeaderboard(
        name=name,
        format=fmt,
        organization=org,
        placement_points=data.get("placement_points") or {},
        kill_point=float(data.get("kill_point", 1.0)),
        points_per_assist=float(data.get("points_per_assist", 0.0)),
        points_per_1000_damage=float(data.get("points_per_1000_damage", 0.0)),
        counts_toward_rankings=counts_toward_rankings,
        status="draft",
        creator=user,
    )
    # played_on + ranking_tier are AFC-admin-gated exactly like counts_toward_rankings (organizers'
    # values are silently ignored). ranking_tier defaults to tier_3 on the model when not set.
    feed_err = _apply_rankings_feed_fields(lb, data, user)
    if feed_err:
        return feed_err
    lb.save()
    return Response({"leaderboard": _serialize_lb(lb)}, status=201)


@api_view(["GET"])
def list_leaderboards(request):
    """
    GET leaderboards/standalone/  — list standalone leaderboards visible to the caller (paginated).

    Auth: Bearer SessionToken.
    Visibility:
        - AFC admin: all leaderboards. Optional ?organization_id=<id> filter (use 'null'/'none'/0
          for AFC-native only). Optional ?status=draft|published, ?format=team|solo, ?q=<name>.
        - Organizer: only leaderboards owned by an org they are an active member of.
    Query: ?limit (<=100, default 25) &offset (>=0) plus the filters above.
    Response 200: { results: [<header dict>], has_more, next_offset, total_count }.
    Consumed by: the list sections on /a/leaderboards and /organizer/leaderboards via list().
    """
    user, err = _auth_user(request)
    if err:
        return err

    qs = StandaloneLeaderboard.objects.select_related("organization", "creator")

    if _is_event_admin(user):
        # AFC admin sees everything; optional org filter.
        org_filter = request.GET.get("organization_id")
        if org_filter is not None:
            if org_filter in ("null", "none", "0", ""):
                qs = qs.filter(organization__isnull=True)
            else:
                try:
                    qs = qs.filter(organization_id=int(org_filter))
                except (TypeError, ValueError):
                    pass
    else:
        # Organizer: only their orgs' leaderboards (active membership). Platform-org-admins are
        # already AFC admins above, so this branch is genuine organizers.
        member_org_ids = OrganizationMember.objects.filter(
            user=user, status="active",
        ).values_list("organization_id", flat=True)
        qs = qs.filter(organization_id__in=list(member_org_ids))

    # Shared optional filters.
    status_f = request.GET.get("status")
    if status_f in ("draft", "published"):
        qs = qs.filter(status=status_f)
    fmt_f = request.GET.get("format")
    if fmt_f in ("team", "solo"):
        qs = qs.filter(format=fmt_f)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(name__icontains=q)

    qs = qs.order_by("-created_at")
    total = qs.count()
    limit, offset = _page_params(request)
    rows = [_serialize_lb(lb) for lb in qs[offset:offset + limit]]
    return _envelope(rows, total, offset, limit)


@api_view(["GET"])
def leaderboard_detail(request, lb_id):
    """
    GET leaderboards/standalone/<id>/  — full detail: header + participants + matches + computed standings.

    Auth: Bearer SessionToken. A draft is visible only to a manager of it; a published leaderboard
    is visible to any logged-in user.
    Response 200:
        {
          "leaderboard": <header dict>,
          "participants": [ <participant dict> ],
          "matches": [ <match dict> ],
          "standings": [ {rank, participant:{id,name,is_ghost,kind}, played_count, total_points,
                          kills, booyahs, per_match:[{match_number,placement,kills,total_points}]} ],
          "can_manage": bool
        }
    Errors: 404 not found; 403 draft viewed by a non-manager.
    Consumed by: the view page (/leaderboards/standalone/<id>) + the wizard Review step.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf

    manager = can_manage_standalone_lb(user, lb)
    if lb.status == "draft" and not manager:
        # A draft leaderboard is hidden from non-managers (spec §1.6 — published makes it viewable).
        return Response({"message": "This leaderboard is not published."}, status=403)

    participants = (
        LeaderboardParticipant.objects
        .filter(leaderboard=lb)
        .select_related("team", "ghost_team", "user", "ghost_player")
    )
    matches = LeaderboardMatch.objects.filter(leaderboard=lb)
    return Response({
        "leaderboard": _serialize_lb(lb),
        "participants": [_serialize_participant(p) for p in participants],
        "matches": [_serialize_match(m) for m in matches],
        "standings": standalone_standings(lb),
        "can_manage": manager,
    })


@api_view(["PATCH"])
def edit_leaderboard(request, lb_id):
    """
    PATCH leaderboards/standalone/<id>/  — edit basics / scoring / publish.

    Auth: Bearer SessionToken + can_manage_standalone_lb(user, lb).
    Request body (all optional): name, format (only allowed while it has no matches/participants),
        placement_points, kill_point, points_per_assist, points_per_1000_damage,
        status ("draft"|"published"), counts_toward_rankings (AFC admin only, ignored otherwise),
        played_on ("YYYY-MM-DD"|null, AFC admin only) and ranking_tier ("tier_1"|"tier_2"|"tier_3",
        AFC admin only) — the P3 rankings-feed config, gated like counts_toward_rankings.
    Response 200: { "leaderboard": <header dict> }.
    Errors: 404 not found; 403 non-manager; 400 invalid format/status / format-change after results /
            bad ranking_tier / bad played_on.
    Consumed by: the wizard (publish on Review) + the view-page Edit link.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    data = request.data or {}

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return Response({"message": "name cannot be empty."}, status=400)
        lb.name = name

    if "format" in data:
        fmt = (data.get("format") or "").strip()
        if fmt not in ("team", "solo"):
            return Response({"message": "format must be 'team' or 'solo'."}, status=400)
        # Changing format after participants/matches exist would orphan their entity columns, so
        # block it once the leaderboard has content (keep the team-XOR-solo invariant honest).
        if fmt != lb.format and (lb.participants.exists() or lb.matches.exists()):
            return Response(
                {"message": "Cannot change format after participants or matches have been added."},
                status=400,
            )
        lb.format = fmt

    if "placement_points" in data:
        lb.placement_points = data.get("placement_points") or {}
    if "kill_point" in data:
        lb.kill_point = float(data.get("kill_point") or 0.0)
    if "points_per_assist" in data:
        lb.points_per_assist = float(data.get("points_per_assist") or 0.0)
    if "points_per_1000_damage" in data:
        lb.points_per_1000_damage = float(data.get("points_per_1000_damage") or 0.0)

    if "status" in data:
        new_status = (data.get("status") or "").strip()
        if new_status not in ("draft", "published"):
            return Response({"message": "status must be 'draft' or 'published'."}, status=400)
        lb.status = new_status

    # Only AFC admins may flip the rankings flag; silently ignore the field for everyone else.
    if "counts_toward_rankings" in data and can_set_rankings_flag(user):
        lb.counts_toward_rankings = bool(data.get("counts_toward_rankings"))

    # P3 rankings-feed fields (played_on + ranking_tier) — same AFC-admin gate as the flag above.
    # Validated before save so a bad tier/date returns 400 without persisting the edit.
    feed_err = _apply_rankings_feed_fields(lb, data, user)
    if feed_err:
        return feed_err

    lb.save()
    return Response({"leaderboard": _serialize_lb(lb)})


@api_view(["DELETE"])
def delete_leaderboard(request, lb_id):
    """
    DELETE leaderboards/standalone/<id>/  — delete a leaderboard (cascades participants/matches/results).

    Auth: Bearer SessionToken + can_manage_standalone_lb. Ghosts are NOT deleted (they are
    platform-wide reusable entities; only the participant link is removed via cascade).
    Response 200: { "message": "Leaderboard deleted." }.
    Errors: 404 not found; 403 non-manager.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to delete this leaderboard."}, status=403)
    lb.delete()
    return Response({"message": "Leaderboard deleted."})


@api_view(["GET"])
def leaderboard_graphic(request, lb_id):
    """GET leaderboards/standalone/<id>/graphic/?design_id=&size=&title=&subtitle=

    Render the live standings onto a branded design as a downloadable PNG (owner 2026-06-13).
    size = instagram (1080x1350) | youtube (1920x1080). design_id picks a design from the
    leaderboard's LIBRARY: its org's designs, or the AFC-native library (organization=null)
    when the leaderboard is AFC-owned. Omitted design_id falls back to the library default,
    then to a plain dark AFC background. title defaults to the leaderboard name; subtitle is
    free text the user types (the tournament stage/group played). Manager-gated, so both AFC
    admins and the owning organizer can export.
    Consumed by: the export picker on the standalone leaderboard view page."""
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response(
            {"message": "You do not have permission to export this leaderboard."}, status=403)

    size = (request.GET.get("size") or "instagram").lower()
    if size not in ("instagram", "youtube"):
        size = "instagram"

    # Resolve the design from the leaderboard's library (org-scoped, or AFC-native org=null).
    from afc_organizers.models import OrgLeaderboardDesign
    _pf = ("logos", "fields", "fields__font", "texts", "texts__font")
    if lb.organization_id is None:
        lib = OrgLeaderboardDesign.objects.filter(organization__isnull=True).prefetch_related(*_pf)
    else:
        lib = OrgLeaderboardDesign.objects.filter(
            organization_id=lb.organization_id).prefetch_related(*_pf)
    design = None
    design_id = request.GET.get("design_id")
    # Validate before filtering: id is an integer PK, so a non-numeric ?design_id=abc would raise a
    # ValueError (500) instead of falling back. Coerce defensively and let a bad/missing id drop to
    # the library default below.
    if design_id and str(design_id).isdigit():
        design = lib.filter(id=int(design_id)).first()
    if design is None:
        design = lib.filter(is_default=True).first() or lib.first()

    # Background (for the requested size) + org logo filesystem paths; None -> renderer defaults.
    bg_path = None
    if design:
        field = design.background_instagram if size == "instagram" else design.background_youtube
        if field:
            try:
                bg_path = field.path
            except Exception:
                bg_path = None
    logo_path = None
    if lb.organization_id and getattr(lb.organization, "logo", None):
        try:
            logo_path = lb.organization.logo.path
        except Exception:
            logo_path = None

    # The design's positioned logos (centre x_pct/y_pct + size). Each resolves to a filesystem
    # path the renderer composites on top; logos with an unreadable file are skipped. When the
    # design has no logos this list is empty and the renderer falls back to the org logo top-left.
    logo_specs = []
    if design:
        for logo in design.logos.all():
            if not logo.image:
                continue
            try:
                path = logo.image.path
            except Exception:
                continue
            logo_specs.append({
                "path": path, "x_pct": logo.x_pct, "y_pct": logo.y_pct, "size": logo.size,
            })

    title = (request.GET.get("title") or lb.name or "").strip()
    subtitle = (request.GET.get("subtitle") or "").strip()

    # Standings for both render paths: the legacy auto-table reads the standalone_standings list;
    # the field-layout path (when the design places its own columns) reads per-row dicts keyed by
    # field_type. A standalone leaderboard only tracks rank/name/total/kills, so booyah/PP/KP/rush
    # columns (if placed) render empty here — those stats only exist on EVENT stage standings.
    std = standalone_standings(lb)
    rows = [{
        "pos": i + 1,
        "team_name": (r.get("participant", {}) or {}).get("name") or "-",
        "total_points": r.get("total_points", 0),
        "kills": r.get("kills", 0),
    } for i, r in enumerate(std)]
    from afc_organizers.views_leaderboard_design import build_field_layout, build_pages_for_export
    from .graphic import render_leaderboard_graphic, render_design_all_pages
    from django.http import HttpResponse

    # ── Multi-page export (owner 2026-06-14) ── ?page=all returns a ZIP of one PNG per design page.
    # Backward compatible: any other (or no) ?page value falls through to the single-PNG path below.
    # Consumed by the FE ExportGraphicDialog, which requests page=all when the design has >1 page.
    page_param = (request.GET.get("page") or "").strip().lower()
    want_all_pages = (page_param == "all") and design is not None

    # ?page=<N> (owner 2026-06-16): render ONLY page N as a single PNG so the FE downloads each page as
    # a SEPARATE image instead of one ZIP (owner prefers multiple images). ?page=all still ZIPs.
    if design is not None and page_param.isdigit():
        pages_spec = build_pages_for_export(design, size=size)
        n = int(page_param)
        if 1 <= n <= len(pages_spec):
            pngs = render_design_all_pages(
                rows, [pages_spec[n - 1]], size=size,
                logos=logo_specs, title=title, subtitle=subtitle,
                text_color=(design.text_color if design else "#FFFFFF"),
                accent_color=(design.accent_color if design else "#34d27b"),
                max_rows=(design.max_rows if design else 16),
                show_title=(design.show_title if design else True),
                show_subtitle=(design.show_subtitle if design else True),
                logo_path=logo_path,
            )
            safe_name = (lb.name or "leaderboard").replace('"', "").replace("\n", " ")
            resp = HttpResponse(pngs[0], content_type="image/png")
            resp["Content-Disposition"] = f'attachment; filename="{safe_name}-{size}-page{n}.png"'
            return resp
        # invalid page index -> fall through to the single-PNG path below.

    if want_all_pages:
        # build_pages_for_export returns 1 entry for a single-page (legacy) design, N for multi-page.
        pages_spec = build_pages_for_export(design, size=size)
        if len(pages_spec) <= 1:
            # Single-page design even though page=all was asked: fall through to the single-PNG path.
            want_all_pages = False
        else:
            pngs = render_design_all_pages(
                rows, pages_spec, size=size,
                logos=logo_specs, title=title, subtitle=subtitle,
                text_color=(design.text_color if design else "#FFFFFF"),
                accent_color=(design.accent_color if design else "#34d27b"),
                max_rows=(design.max_rows if design else 16),
                show_title=(design.show_title if design else True),
                show_subtitle=(design.show_subtitle if design else True),
                logo_path=logo_path,
            )
            zip_buf = io.BytesIO()
            safe_name = (lb.name or "leaderboard").replace('"', "").replace("\n", " ")
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, png in enumerate(pngs, start=1):
                    zf.writestr(f"{safe_name}-{size}-page{i}.png", png)
            zip_buf.seek(0)
            resp = HttpResponse(zip_buf.read(), content_type="application/zip")
            resp["Content-Disposition"] = f'attachment; filename="{safe_name}-{size}-all-pages.zip"'
            return resp

    # ── Single-page PNG (default path, unchanged behaviour) ──
    field_layout = build_field_layout(design, size=size) if design else None
    png = render_leaderboard_graphic(
        std,
        size=size,
        background_path=bg_path,
        logo_path=logo_path,
        logos=logo_specs,
        title=title,
        subtitle=subtitle,
        text_color=(design.text_color if design else "#FFFFFF"),
        accent_color=(design.accent_color if design else "#34d27b"),
        max_rows=(design.max_rows if design else 16),
        show_title=(design.show_title if design else True),
        show_subtitle=(design.show_subtitle if design else True),
        field_layout=field_layout,
        rows=rows,
    )

    resp = HttpResponse(png, content_type="image/png")
    safe = (lb.name or "leaderboard").replace('"', "").replace("\n", " ")
    resp["Content-Disposition"] = f'attachment; filename="{safe}-{size}.png"'
    return resp


# ════════════════════════════════════════════════════════════════════════════════════════════
# Task 5 — Participants
# ════════════════════════════════════════════════════════════════════════════════════════════
class _ParticipantResolutionError(Exception):
    """Raised by _resolve_or_create_participant when a resolution cannot be turned into a
    participant (missing field, entity not found, duplicate). Carries a `message` + HTTP `status`
    so the calling view (add_participant / ocr_apply) can return a clean 400 instead of a 500.
    Kept as an exception (not a returned Response) so the helper works INSIDE a transaction.atomic()
    block and a raise rolls the transaction back automatically."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _resolve_or_create_participant(lb, resolution, actor):
    """Resolve a unified `resolution` dict into a LeaderboardParticipant for leaderboard `lb`,
    creating ghosts inline as needed. The single source of truth for participant resolution, shared
    by add_participant (one participant per request) and ocr_apply (many participants per apply).

    `resolution` (format-agnostic; lb.format decides team-vs-solo):
        {"kind": "real",          "id": <team_id|user_id>}
        {"kind": "ghost_new",     "name": str, "country"?: str, "players"?: [ign,...]}  # team
        {"kind": "ghost_new",     "ign": str (or "name": str)}                          # solo
        {"kind": "ghost_existing","id": <ghost_team_id uuid | ghost_player_id int>,
                                  "players"?: [ign,...]}  # team: APPEND missing roster slots

    Behavior per kind (mirrors the original add_participant logic exactly):
        real           -> get-or-create: if the real entity is already a participant of THIS lb,
                          REUSE that participant row (idempotent for OCR re-apply); else create one.
        ghost_new      -> create the ghost (GhostTeam/GhostPlayer, provenance stamped to `actor` for
                          teams) + the participant.
        ghost_existing -> reuse the platform ghost; if already a participant of this lb, REUSE the
                          existing participant row. For TEAM ghosts an optional players list APPENDS
                          GhostPlayer slots the ghost does not have yet (owner 2026-06-12: "add those
                          players to a newly created team or old ghost team") - existing slots are
                          never altered, dedupe is case-insensitive on ign.
    Returns the LeaderboardParticipant. Raises _ParticipantResolutionError (rolls back) on any
    invalid input. Reads: afc_team.Team, afc_auth.User, afc_rankings.GhostTeam/GhostPlayer.
    """
    is_team = lb.format == "team"
    kind = (resolution.get("kind") or "").strip()
    if kind not in ("real", "ghost_new", "ghost_existing"):
        raise _ParticipantResolutionError("kind must be 'real', 'ghost_new', or 'ghost_existing'.")

    # ── REAL ── get-or-create so a re-apply of the same team/user reuses its existing participant ──
    if kind == "real":
        if is_team:
            team_id = resolution.get("id")
            if not team_id:
                raise _ParticipantResolutionError("team_id is required for a real team participant.")
            try:
                team = Team.objects.get(team_id=team_id)
            except Team.DoesNotExist:
                raise _ParticipantResolutionError("Team not found.")
            existing = LeaderboardParticipant.objects.filter(leaderboard=lb, team=team).first()
            return existing or LeaderboardParticipant.objects.create(leaderboard=lb, team=team)
        uid = resolution.get("id")
        if not uid:
            raise _ParticipantResolutionError("user_id is required for a real solo participant.")
        try:
            u = User.objects.get(user_id=uid)
        except User.DoesNotExist:
            raise _ParticipantResolutionError("User not found.")
        existing = LeaderboardParticipant.objects.filter(leaderboard=lb, user=u).first()
        return existing or LeaderboardParticipant.objects.create(leaderboard=lb, user=u)

    # ── GHOST_NEW ── create the ghost inline (gated by can_manage at the view) + the participant ──
    if kind == "ghost_new":
        if is_team:
            gname = (resolution.get("name") or "").strip()
            if not gname:
                raise _ParticipantResolutionError("name is required to create a ghost team.")
            # GhostTeam.created_by stamps the actor (provenance). country has no model default, so we
            # always pass it (empty string when the caller omitted it).
            ghost = GhostTeam.objects.create(
                team_name=gname,
                country=(resolution.get("country") or "").strip(),
                created_by=actor,
            )
            # Optional roster IGNs -> GhostPlayer slots attached to the new ghost team.
            # Dedupe case-insensitively so the same OCR-read name twice never doubles a slot.
            seen_igns = set()
            slot = 0
            for ign in resolution.get("players") or []:
                ign = (ign or "").strip()
                if not ign or ign.lower() in seen_igns:
                    continue
                seen_igns.add(ign.lower())
                slot += 1
                GhostPlayer.objects.create(ghost_team=ghost, ign=ign, slot=slot)
            return LeaderboardParticipant.objects.create(leaderboard=lb, ghost_team=ghost)
        ign = (resolution.get("ign") or resolution.get("name") or "").strip()
        if not ign:
            raise _ParticipantResolutionError("ign is required to create a ghost player.")
        # Standalone (team-less) ghost player. NOTE: GhostPlayer has no created_by field.
        ghost = GhostPlayer.objects.create(ign=ign)
        return LeaderboardParticipant.objects.create(leaderboard=lb, ghost_player=ghost)

    # ── GHOST_EXISTING ── reuse a platform-wide ghost; reuse its participant row if present ──
    if is_team:
        gid = resolution.get("id")
        if not gid:
            raise _ParticipantResolutionError("ghost_team_id is required.")
        # ghost_team_id is a UUID PK; a malformed value raises ValidationError, not DoesNotExist,
        # so catch broadly and 400 rather than 500.
        try:
            ghost = GhostTeam.objects.get(ghost_team_id=gid)
        except GhostTeam.DoesNotExist:
            raise _ParticipantResolutionError("Ghost team not found.")
        except Exception:
            raise _ParticipantResolutionError("Invalid ghost_team_id.")
        # Optional players: APPEND roster slots this ghost team does not have yet (the OCR review
        # lets the admin attach the read/approved players to an EXISTING ghost team, not only a new
        # one). Existing slots stay untouched; dedupe is case-insensitive on ign.
        incoming = [(p or "").strip() for p in (resolution.get("players") or [])]
        incoming = [p for p in incoming if p]
        if incoming:
            existing_igns = {
                (g.ign or "").lower() for g in GhostPlayer.objects.filter(ghost_team=ghost)
            }
            next_slot = GhostPlayer.objects.filter(ghost_team=ghost).count()
            for ign in incoming:
                if ign.lower() in existing_igns:
                    continue
                existing_igns.add(ign.lower())
                next_slot += 1
                GhostPlayer.objects.create(ghost_team=ghost, ign=ign, slot=next_slot)
        existing = LeaderboardParticipant.objects.filter(leaderboard=lb, ghost_team=ghost).first()
        return existing or LeaderboardParticipant.objects.create(leaderboard=lb, ghost_team=ghost)
    gpid = resolution.get("id")
    if not gpid:
        raise _ParticipantResolutionError("ghost_player_id is required.")
    try:
        ghost = GhostPlayer.objects.get(id=gpid)
    except (GhostPlayer.DoesNotExist, ValueError):
        raise _ParticipantResolutionError("Ghost player not found.")
    existing = LeaderboardParticipant.objects.filter(leaderboard=lb, ghost_player=ghost).first()
    return existing or LeaderboardParticipant.objects.create(leaderboard=lb, ghost_player=ghost)


@api_view(["POST"])
def add_participant(request, lb_id):
    """
    POST leaderboards/standalone/<id>/participants/  — add a participant (real, ghost_new, or ghost_existing).

    Auth: Bearer SessionToken + can_manage_standalone_lb (this is also the gate for inline ghost
    creation — NOT the stricter rankings-admin gate; spec §5).
    Request body — `kind` selects the path, and MUST match the leaderboard's format:
        kind="real":
            team format  → {"team_id": int}
            solo format  → {"user_id": int}
        kind="ghost_new"   (creates the ghost + the participant in one call):
            team format  → {"name": str, "country"?: str, "players"?: [ign, ...]}
            solo format  → {"ign": str}    (or {"name": str})
        kind="ghost_existing":
            team format  → {"ghost_team_id": uuid}
            solo format  → {"ghost_player_id": int}
    Response 201: { "participant": <participant dict> }.
    Errors: 404 leaderboard not found; 403 non-manager; 400 wrong kind for format / missing field /
            entity not found / duplicate participant.
    Consumed by: the wizard ParticipantsStep (TeamSearchSelect / UserSearchSelect + GhostCreateInline).
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    data = request.data or {}
    kind = (data.get("kind") or "").strip()
    if kind not in ("real", "ghost_new", "ghost_existing"):
        return Response({"message": "kind must be 'real', 'ghost_new', or 'ghost_existing'."}, status=400)

    is_team = lb.format == "team"

    # Normalize this endpoint's format-specific body (team_id/user_id, name/ign, ghost_team_id/
    # ghost_player_id) into the unified `resolution` shape _resolve_or_create_participant consumes.
    # This keeps the per-kind creation logic in ONE place (shared with ocr_apply) while preserving
    # add_participant's behavior, including its EXPLICIT duplicate-rejection below (the helper reuses
    # an existing participant for OCR re-apply, but this endpoint must still 400 on a duplicate).
    if kind == "real":
        resolution = {"kind": "real", "id": data.get("team_id") if is_team else data.get("user_id")}
    elif kind == "ghost_new":
        resolution = {"kind": "ghost_new", "name": data.get("name"), "country": data.get("country"),
                      "players": data.get("players"), "ign": data.get("ign")}
    else:  # ghost_existing
        resolution = {"kind": "ghost_existing",
                      "id": data.get("ghost_team_id") if is_team else data.get("ghost_player_id")}

    # Explicit duplicate guard (unchanged behavior): reject a second add of the same real/ghost
    # entity with a 400, BEFORE delegating creation. The OCR apply path deliberately does NOT do
    # this (it reuses the participant) — that divergence lives here, not in the shared helper.
    dup_msg = _duplicate_participant_message(lb, kind, resolution, is_team)
    if dup_msg:
        return Response({"message": dup_msg}, status=400)

    with transaction.atomic():
        try:
            participant = _resolve_or_create_participant(lb, resolution, user)
        except _ParticipantResolutionError as e:
            return Response({"message": e.message}, status=e.status)

    return Response({"participant": _serialize_participant(participant)}, status=201)


def _duplicate_participant_message(lb, kind, resolution, is_team):
    """Return the add_participant 400 message if `resolution` names a real/ghost entity that is
    ALREADY a participant of `lb`, else None. Used only by add_participant (NOT ocr_apply, which is
    idempotent by design). Pure read; never creates anything. ghost_new is never a duplicate (it
    mints a brand-new ghost), so it is skipped here."""
    if kind == "ghost_new":
        return None
    entity_id = resolution.get("id")
    if not entity_id:
        return None  # missing-id errors are surfaced by the helper, not here
    # A malformed id (e.g. a non-UUID ghost_team_id) raises here; swallow it and let the helper
    # produce the proper "Invalid ghost_team_id." 400 — never 500 from this pre-check.
    try:
        if kind == "real":
            if is_team and LeaderboardParticipant.objects.filter(leaderboard=lb, team_id=entity_id).exists():
                return "This team is already a participant."
            if not is_team and LeaderboardParticipant.objects.filter(leaderboard=lb, user_id=entity_id).exists():
                return "This user is already a participant."
            return None
        # kind == "ghost_existing"
        if is_team and LeaderboardParticipant.objects.filter(leaderboard=lb, ghost_team_id=entity_id).exists():
            return "This ghost team is already a participant."
        if not is_team and LeaderboardParticipant.objects.filter(leaderboard=lb, ghost_player_id=entity_id).exists():
            return "This ghost player is already a participant."
    except Exception:
        return None
    return None


@api_view(["DELETE"])
def remove_participant(request, lb_id, pid):
    """
    DELETE leaderboards/standalone/<id>/participants/<pid>/  — remove a participant from a leaderboard.

    Auth: Bearer SessionToken + can_manage_standalone_lb. Cascades the participant's results. The
    underlying real entity / ghost is NOT deleted (ghosts are platform-wide reusable).
    Response 200: { "message": "Participant removed." }.
    Errors: 404 leaderboard or participant not found; 403 non-manager.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)
    try:
        p = LeaderboardParticipant.objects.get(id=pid, leaderboard=lb)
    except LeaderboardParticipant.DoesNotExist:
        return Response({"message": "Participant not found."}, status=404)
    p.delete()
    return Response({"message": "Participant removed."})


# ════════════════════════════════════════════════════════════════════════════════════════════
# Task 6 — Matches + results
# ════════════════════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def add_match(request, lb_id):
    """
    POST leaderboards/standalone/<id>/matches/  — add a map to the leaderboard.

    Auth: Bearer SessionToken + can_manage_standalone_lb.
    Request body (optional): {"match_number": int, "match_map": str}. If match_number is omitted it
    auto-increments to (max existing + 1).
    Response 201: { "match": <match dict> }.
    Errors: 404 not found; 403 non-manager; 400 non-integer match_number.
    Consumed by: the wizard ResultsStep ("add map").
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    data = request.data or {}
    match_number = data.get("match_number")
    if match_number in (None, ""):
        # Auto-increment: next number after the current max (1 if none yet).
        last = lb.matches.order_by("-match_number").first()
        match_number = (last.match_number + 1) if last else 1
    else:
        try:
            match_number = int(match_number)
        except (TypeError, ValueError):
            return Response({"message": "match_number must be an integer."}, status=400)

    match = LeaderboardMatch.objects.create(
        leaderboard=lb,
        match_number=match_number,
        match_map=(data.get("match_map") or None),
    )
    return Response({"match": _serialize_match(match)}, status=201)


@api_view(["DELETE"])
def delete_match(request, mid):
    """
    DELETE leaderboards/standalone/matches/<mid>/  — delete a map (cascades its results).

    Auth: Bearer SessionToken + can_manage_standalone_lb (of the match's leaderboard).
    Response 200: { "message": "Match deleted." }.
    Errors: 404 not found; 403 non-manager.
    """
    user, err = _auth_user(request)
    if err:
        return err
    try:
        match = LeaderboardMatch.objects.select_related("leaderboard").get(id=mid)
    except LeaderboardMatch.DoesNotExist:
        return Response({"message": "Match not found."}, status=404)
    if not can_manage_standalone_lb(user, match.leaderboard):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)
    match.delete()
    return Response({"message": "Match deleted."})


def _save_one_result(match, participant, row, lb):
    """Score ONE result row and upsert its ParticipantMatchResult on `match` for `participant`.

    The single per-row compute+store step, shared by save_match_results (manual entry) and ocr_apply
    (screenshot apply) so the point math lives in ONE place. Reads the raw inputs from `row`
    (placement, kills, damage, assists, bonus, penalty, played), computes the point columns via the
    single-source-of-truth scoring helpers (compute_team_points for a team lb, compute_solo_points
    for solo, using `lb`'s scoring config + its normalized placement table), then upserts (unique per
    match+participant — re-saving overwrites). Returns the {participant_id, placement, kills,
    placement_points, kill_points, total_points} summary the callers echo back. Must be called inside
    a transaction by the caller (both callers wrap it in transaction.atomic()).
    """
    placement = int(row.get("placement", 0) or 0)
    kills = int(row.get("kills", 0) or 0)
    damage = int(row.get("damage", 0) or 0)
    assists = int(row.get("assists", 0) or 0)
    bonus = int(row.get("bonus", 0) or 0)
    penalty = int(row.get("penalty", 0) or 0)
    played = bool(row.get("played", True))

    # ── Per-player kill breakdown (team format, owner 2026-06-12) ──
    # Optional row["players"] = [{"name": str, "user_id"?: int, "kills": int}, ...]. When given,
    # the TEAM kills are the SUM of the player kills (server is the authority, exactly like the
    # event flow's enter_team_match_result_manual sums played players), and the normalized
    # breakdown is stored on the result row so reloading a map can re-show per-player inputs.
    players_in = row.get("players")
    player_kills = None
    if isinstance(players_in, list) and players_in:
        player_kills = []
        for p in players_in:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            uid_val = p.get("user_id")
            player_kills.append({
                "name": name,
                "user_id": int(uid_val) if uid_val is not None else None,
                "kills": int(p.get("kills", 0) or 0),
            })
        if player_kills:
            kills = sum(p["kills"] for p in player_kills)
        else:
            player_kills = None

    # Normalize the placement table (int->int) the SAME way the events surface does, per call. (Cheap;
    # keeps the helper self-contained so either caller can invoke it without pre-normalizing.)
    placement_points = normalize_placement_points(lb.placement_points)

    # Compute the point columns via the single-source-of-truth scoring helpers.
    if lb.format == "team":
        pts = compute_team_points(
            placement_points=placement_points,
            kill_point=lb.kill_point,
            points_per_assist=lb.points_per_assist,
            points_per_1000_damage=lb.points_per_1000_damage,
            placement=placement,
            kills=kills,
            damage=damage,
            assists=assists,
            bonus=bonus,
            penalty=penalty,
            played=played,
        )
    else:
        pts = compute_solo_points(
            placement_points=placement_points,
            kill_point=lb.kill_point,
            placement=placement,
            kills=kills,
            played=played,
        )

    # Upsert: unique per (match, participant) — re-saving overwrites the prior row.
    ParticipantMatchResult.objects.update_or_create(
        match=match,
        participant=participant,
        defaults={
            "placement": placement,
            "kills": kills,
            "damage": damage,
            "assists": assists,
            # Stored breakdown (or None when the row was entered as a plain team total). Re-saving
            # without a breakdown clears the stale one - the row is replaced wholesale on upsert.
            "player_kills": player_kills,
            "bonus_points": bonus,
            "penalty_points": penalty,
            "placement_points": pts["placement_points"],
            "kill_points": pts["kill_points"],
            "total_points": pts["total_points"],
            "played": played,
        },
    )
    return {
        "participant_id": participant.id,
        "placement": placement,
        "kills": kills,
        "placement_points": pts["placement_points"],
        "kill_points": pts["kill_points"],
        "total_points": pts["total_points"],
    }


@api_view(["POST"])
def save_match_results(request, mid):
    """
    POST leaderboards/standalone/matches/<mid>/results/  — bulk save + score one map's results.

    Auth: Bearer SessionToken + can_manage_standalone_lb (of the match's leaderboard).
    Request body:
        {
          "results": [
            {"participant_id": int, "placement": int, "kills": int,
             "damage"?: int, "assists"?: int, "bonus"?: int, "penalty"?: int, "played"?: bool},
            ...
          ]
        }
    For each row we compute points via afc_tournament_and_scrims.scoring.compute_team_points (team
    format) or compute_solo_points (solo format) using the leaderboard's scoring config, then upsert
    a ParticipantMatchResult (unique per match+participant — re-saving overwrites). Standings re-derive
    on the next detail read.
    Response 200: { "saved": <count>, "results": [ {participant_id, placement, kills,
                    placement_points, kill_points, total_points} ] }.
    Errors: 404 match not found; 403 non-manager; 400 missing results / participant not in this
            leaderboard.
    Consumed by: the wizard ResultsStep (per-map editable table → saveResults).
    """
    user, err = _auth_user(request)
    if err:
        return err
    try:
        match = LeaderboardMatch.objects.select_related("leaderboard").get(id=mid)
    except LeaderboardMatch.DoesNotExist:
        return Response({"message": "Match not found."}, status=404)
    lb = match.leaderboard
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    rows = (request.data or {}).get("results")
    if not isinstance(rows, list) or not rows:
        return Response({"message": "results must be a non-empty list."}, status=400)

    # Map participant_id → participant, restricted to THIS leaderboard so a caller cannot write a
    # result for a participant in someone else's leaderboard.
    valid_participants = {
        p.id: p for p in LeaderboardParticipant.objects.filter(leaderboard=lb)
    }

    saved = []
    with transaction.atomic():
        for row in rows:
            pid = row.get("participant_id")
            if pid not in valid_participants:
                return Response(
                    {"message": f"participant_id {pid} is not a participant of this leaderboard."},
                    status=400,
                )
            # Per-row compute + upsert via the shared helper (same math save + OCR apply use).
            saved.append(_save_one_result(match, valid_participants[pid], row, lb))

    return Response({"saved": len(saved), "results": saved})


@api_view(["GET"])
def participant_roster(request, lb_id, pid):
    """
    GET leaderboards/standalone/<lb_id>/participants/<pid>/roster/  — the participant's player list.

    Auth: Bearer SessionToken + can_manage_standalone_lb.
    Purpose (owner 2026-06-12): the manual ResultsStep shows each selected TEAM's players with a
    kills input per player ("just like the manual input for the main leaderboard"). REAL teams read
    afc_team.TeamMembers; GHOST teams read their afc_rankings.GhostPlayer slots (ordered). Solo
    participants have no roster (empty list, the FE hides the panel).
    Response 200: { "players": [ {"name": str, "user_id": int|null}, ... ] }
        user_id is set for real-team members (so saved breakdowns can link back to platform users)
        and null for ghost roster names.
    Errors: 404 leaderboard/participant not found; 403 non-manager.
    Consumed by: ResultsStep.tsx (fetched lazily per team participant, cached in state).
    """
    user, err = _auth_user(request)
    if err:
        return err
    try:
        lb = StandaloneLeaderboard.objects.get(id=lb_id)
    except StandaloneLeaderboard.DoesNotExist:
        return Response({"message": "Leaderboard not found."}, status=404)
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to view this leaderboard's rosters."}, status=403)
    try:
        participant = LeaderboardParticipant.objects.get(id=pid, leaderboard=lb)
    except LeaderboardParticipant.DoesNotExist:
        return Response({"message": "Participant not found."}, status=404)

    players = []
    if participant.team_id:
        # Real team roster: every TeamMembers row, username as the display name.
        players = [
            {"name": m["member__username"], "user_id": m["member_id"]}
            for m in TeamMembers.objects.filter(team_id=participant.team_id)
            .order_by("join_date")
            .values("member_id", "member__username")
        ]
    elif participant.ghost_team_id:
        # Ghost team roster: the GhostPlayer slots, in slot order. No platform user behind them.
        players = [
            {"name": gp.ign, "user_id": None}
            for gp in GhostPlayer.objects.filter(ghost_team_id=participant.ghost_team_id).order_by("slot")
        ]
    # solo kinds (user / ghost_player): no roster - the FE only asks for team participants.

    return Response({"players": players})


# ── Ghost typeahead search (owner 2026-06-12: "let ghost teams and players be searchable") ──────
# The /rankings/ghost-teams/ list is head_admin-gated, so organizers building a standalone
# leaderboard could not DISCOVER existing ghosts - they could only create duplicates or wait for an
# OCR suggestion. These two endpoints mirror the search-teams / search-users idiom (Bearer auth,
# q>=2, limit<=25, punctuation/leet-insensitive via utils.search_utils) over the ghost models.
# Consumed by: components/ui/team-search-select.tsx + user-search-select.tsx when their opt-in
# `includeGhosts` prop is set (the standalone wizard pickers). A picked ghost is added to the
# leaderboard via the existing add_participant kind="ghost_existing" contract.

@api_view(["GET"])
def search_ghost_teams(request):
    """
    GET leaderboards/standalone/search-ghost-teams/?q=<text>&limit=10  — ghost-team typeahead.

    Auth: Bearer SessionToken (any authenticated user - ghost names/countries are not sensitive,
    same openness as /team/search-teams/).
    Query: q (min 2 chars), limit (1..25, default 10). Matches team_name (icontains OR the
    normalized punctuation/leet-stripped form, widening only).
    Response 200: { "results": [ {"ghost_team_id": str(uuid), "team_name": str, "country": str,
                                  "players_count": int} ], "total_count": int }
    Consumed by: TeamSearchSelect (includeGhosts) on the standalone wizard.
    """
    user, err = _auth_user(request)
    if err:
        return err
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return Response({"results": [], "total_count": 0})
    try:
        limit = max(1, min(int(request.GET.get("limit", 10)), 25))
    except (TypeError, ValueError):
        limit = 10

    # Same widen-only condition shape as /team/search-teams/: plain icontains OR the
    # punctuation/leet-normalized comparison (only when the stripped query is non-empty).
    cond = Q(team_name__icontains=q)
    stripped = separator_stripped(q)
    qs = GhostTeam.objects.filter(is_active=True).annotate(_norm_name=normalized_column("team_name"))
    if stripped:
        cond |= Q(_norm_name__icontains=stripped)
    qs = qs.filter(cond).order_by("team_name")
    total = qs.count()
    results = []
    for gt in qs[:limit]:
        results.append({
            "ghost_team_id": str(gt.ghost_team_id),
            "team_name": gt.team_name,
            "country": gt.country,
            "players_count": gt.players.count(),
        })
    return Response({"results": results, "total_count": total})


@api_view(["GET"])
def search_ghost_players(request):
    """
    GET leaderboards/standalone/search-ghost-players/?q=<text>&limit=10  — ghost-player typeahead.

    Auth: Bearer SessionToken (any authenticated user).
    Query: q (min 2 chars), limit (1..25, default 10). Matches ign (icontains OR the normalized
    punctuation/leet-stripped form).
    Response 200: { "results": [ {"ghost_player_id": int, "ign": str,
                                  "ghost_team_id": str(uuid)|null, "ghost_team_name": str|null} ],
                    "total_count": int }
    Consumed by: UserSearchSelect (includeGhosts) on the standalone wizard (solo format), so an
    existing ghost player is reused (kind="ghost_existing") instead of duplicated.
    """
    user, err = _auth_user(request)
    if err:
        return err
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return Response({"results": [], "total_count": 0})
    try:
        limit = max(1, min(int(request.GET.get("limit", 10)), 25))
    except (TypeError, ValueError):
        limit = 10

    cond = Q(ign__icontains=q)
    stripped = separator_stripped(q)
    qs = GhostPlayer.objects.select_related("ghost_team").annotate(_norm_ign=normalized_column("ign"))
    if stripped:
        cond |= Q(_norm_ign__icontains=stripped)
    qs = qs.filter(cond).order_by("ign")
    total = qs.count()
    results = []
    for gp in qs[:limit]:
        results.append({
            "ghost_player_id": gp.id,
            "ign": gp.ign,
            "ghost_team_id": str(gp.ghost_team_id) if gp.ghost_team_id else None,
            "ghost_team_name": gp.ghost_team.team_name if gp.ghost_team_id else None,
        })
    return Response({"results": results, "total_count": total})


# ════════════════════════════════════════════════════════════════════════════════════════════
# Task 2.4 / 2.5 — OCR assist (Phase 2)
#
# These two endpoints let a manager upload a result screenshot and turn it into participants +
# results WITHOUT the event-OCR commit machinery (which is Match/OCRSession-bound). The flow is:
#   POST .../ocr/        -> ocr_extract: run the shared OCR engine, match read names against the
#                           WHOLE platform, return a STATELESS draft for the FE review table.
#   POST .../ocr/apply/  -> ocr_apply (Task 2.5): the reviewed/corrected rows are turned into
#                           participants (real or ghost) + one match + scored results, reusing the
#                           same resolution + scoring helpers add_participant / save_match_results use.
# extract.extract_rows is the SAME local-first-then-Gemini router the event flow uses (lifted into
# afc_ocr.services.extract so both surfaces share it). The event OCR flow is untouched.
# ════════════════════════════════════════════════════════════════════════════════════════════
@api_view(["POST"])
def ocr_extract(request, lb_id):
    """
    POST leaderboards/standalone/<id>/ocr/  — read a result screenshot into a draft of review rows.

    PURPOSE
        Run the shared OCR extraction engine on an uploaded screenshot, then match the read names
        against the WHOLE platform (every Team for a team leaderboard, every User for a solo one),
        and return a STATELESS draft the FE review table renders for correction. Nothing is persisted
        here (no OCRSession — that model is event-Match-bound); the draft is returned to the client
        and applied later via ocr_apply.
    AUTH
        Bearer SessionToken + can_manage_standalone_lb(user, lb) (the same gate as every other
        mutation on this leaderboard, and the gate for inline ghost creation at apply-time). A
        non-manager gets 403.
    REQUEST (multipart/form-data)
        screenshot (file: PNG / JPG / WEBP)   — required.
    RESPONSE 200
        {
          "draft_id": "<uuid>",          # opaque id for the FE to track this draft (not stored)
          "format": "team" | "solo",
          "rows": [
            # team format:
            {"row_id", "raw_name", "placement", "kills", "matched_team_id", "matched_name",
             "confidence", "top_candidates":[{team_id, team_name, confidence}], "is_unmatched"}
            # solo format:
            {"row_id", "raw_name", "placement", "kills", "matched_user_id", "matched_name",
             "confidence", "top_candidates":[{user_id, username, confidence}], "is_unmatched"}
          ]
        }
    ERRORS
        404 leaderboard not found; 403 non-manager; 400 missing screenshot; 503 OCR engine failure.
    HOW IT CONNECTS
        - extract.extract_rows (afc_ocr.services.extract) does the local-first-then-Gemini read;
          team format passes prompt_kind="team_standings" so Gemini also reads a team_name.
        - all_platform_teams_with_ghosts + match_team_name (team) / all_platform_players + match_name (solo)
          build the candidate matches.
        - Consumed by the FE OcrUploadDialog (frontend) which posts the file and renders `rows`;
          the corrected rows are then sent to ocr_apply.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    screenshot = request.FILES.get("screenshot")
    if not screenshot:
        return Response({"message": "screenshot is required."}, status=400)

    is_team = lb.format == "team"
    event_type = "team" if is_team else "solo"
    # Team leaderboards use the team_standings prompt so Gemini reads a team_name per placement;
    # solo uses the default player prompt (prompt_kind None).
    prompt_kind = "team_standings" if is_team else None

    image_bytes = screenshot.read()
    mime_type = screenshot.content_type or "image/jpeg"

    try:
        raw_output, _engine = extract.extract_rows(
            image_bytes, mime_type, event_type, prompt_kind=prompt_kind,
        )
    except Exception as exc:
        return Response({"message": f"OCR extraction failed: {exc}"}, status=503)

    if is_team:
        # Same pools as the batch worker: teams INCLUDING ghosts (resolve to an existing ghost
        # instead of duplicating it) + per-player platform matches for the review table.
        rows = _build_team_ocr_rows(
            raw_output, all_platform_teams_with_ghosts(), players_pool=all_platform_players(),
        )
    else:
        rows = _build_solo_ocr_rows(raw_output, all_platform_players())

    # draft_id is a stateless correlation id for the FE only (we never persist an OCRSession here —
    # the standalone flow has no Match to bind one to). The FE echoes it back on apply for tracing.
    return Response({"draft_id": str(uuid.uuid4()), "format": lb.format, "rows": rows})


@api_view(["POST"])
def results_file_extract(request, lb_id):
    """
    POST leaderboards/standalone/<id>/results-file/  — parse a match-log RESULT FILE into review rows.

    PURPOSE
        The "upload result file" option (owner 2026-06-12: every upload option the event flow has
        must exist on standalone leaderboards too). Parses the in-game TEAM match-log text export
        (the same file events accept on events/upload-team-match-result/) and returns the SAME
        stateless review-row draft ocr_extract returns, so the FE reuses one review table and the
        one ocr_apply pipeline for screenshots AND files. Nothing is persisted here.
        Unlike OCR, the file carries each player's UID, so players resolve EXACTLY by User.uid
        (confidence 1.0); unknown UIDs fall back to the fuzzy name match.
    AUTH
        Bearer SessionToken + can_manage_standalone_lb(user, lb) (same gate as ocr_extract).
    REQUEST (multipart/form-data)
        file (text export from the game)   — required. TEAM leaderboards only (the file format is
        team-shaped; solo leaderboards keep manual + OCR).
    RESPONSE 200
        { "draft_id": "<uuid>", "format": "team", "rows": [<same team row shape as ocr_extract,
          including players_detail with per-player kills + matches>] }
    ERRORS
        404 leaderboard not found; 403 non-manager; 400 missing file / solo leaderboard / nothing
        parsed from the file.
    HOW IT CONNECTS
        - utils.match_log.parse_team_match_log owns the file format (regexes mirror the event flow).
        - afc_leaderboard.ocr.build_rows_from_match_log builds the rows (UID-exact players, team
          matching over all_platform_teams_with_ghosts like the OCR path).
        - Consumed by the FE ResultFileDialog (frontend) inside the standalone wizard; the reviewed
          rows are applied via the existing POST .../ocr/apply/.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)
    if lb.format != "team":
        return Response(
            {"message": "Result-file upload is for TEAM leaderboards. Use manual entry or OCR for solo."},
            status=400,
        )

    uploaded = request.FILES.get("file")
    if not uploaded:
        return Response({"message": "file is required."}, status=400)

    text = uploaded.read().decode("utf-8", errors="ignore")
    parsed = parse_team_match_log(text)
    if not parsed:
        return Response(
            {"message": "No team data could be parsed from this file. Is it the game's match-log export?"},
            status=400,
        )

    rows = _build_rows_from_match_log(
        parsed, all_platform_teams_with_ghosts(), all_platform_players(),
    )
    return Response({"draft_id": str(uuid.uuid4()), "format": lb.format, "rows": rows})


@api_view(["POST"])
def ocr_apply(request, lb_id):
    """
    POST leaderboards/standalone/<id>/ocr/apply/  — turn reviewed OCR rows into participants + a scored map.

    PURPOSE
        Take the reviewed/corrected rows from ocr_extract and, in ONE transaction, (1) resolve each
        row's participant (real, ghost_new, or ghost_existing) via the SAME _resolve_or_create_participant
        helper add_participant uses, (2) create ONE LeaderboardMatch (next match_number), and (3) write a
        scored ParticipantMatchResult per row via the SAME _save_one_result helper save_match_results uses.
        No duplicate point math, no duplicate ghost-creation logic. One apply == one map.
    AUTH
        Bearer SessionToken + can_manage_standalone_lb(user, lb) (also the gate for the inline ghost
        creation a ghost_new row triggers). Non-manager -> 403.
    REQUEST (application/json)
        {
          "match_map"?: str,                     # optional free-text map name for the created match
          "rows": [
            {"placement": int, "kills": int,
             "damage"?, "assists"?, "bonus"?, "penalty"?, "played"?,   # optional scoring inputs
             "resolution": {"kind": "real", "id": <team_id|user_id>}
                         | {"kind": "ghost_new", "name": str, "country"?: str}   # team
                         | {"kind": "ghost_new", "ign": str}                     # solo
                         | {"kind": "ghost_existing", "id": <ghost_team_id|ghost_player_id>}},
            ...
          ]
        }
    RESPONSE 200
        { "match": <match dict>, "participants": [<participant dict>], "standings": [<standings rows>] }
        (standings reuse afc_leaderboard.standings.standalone_standings — same shape as leaderboard_detail.)
    ERRORS
        404 leaderboard not found; 403 non-manager; 400 empty rows / a resolution that cannot resolve
        (missing field, entity not found, bad kind). The whole apply is atomic: any bad row rolls back
        the match + every result, so a partial map is never left behind.
    HOW IT CONNECTS
        - _resolve_or_create_participant (shared with add_participant) does ghost creation + real
          get-or-create (a real/ghost_existing entity already present is reused, so re-applying the same
          screenshot does not duplicate participants).
        - _save_one_result (shared with save_match_results) does the point math via scoring.compute_*.
        - Consumed by the FE OcrUploadDialog "Apply" action; on success the wizard ingests the returned
          participants + match and jumps to the Results step pre-filled.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    data = request.data or {}
    try:
        match, unique_participants = _apply_ocr_rows(lb, data.get("rows"), data.get("match_map"), user)
    except _ParticipantResolutionError as e:
        # A bad resolution rolls the whole apply back (no orphan match/results) and returns a clean 4xx.
        return Response({"message": e.message}, status=e.status)

    return Response({
        "match": _serialize_match(match),
        "participants": [_serialize_participant(p) for p in unique_participants],
        "standings": standalone_standings(lb),
    })


def _apply_ocr_rows(lb, rows, match_map, user):
    """Shared apply: turn reviewed OCR rows into ONE new map + its participants + scored results, atomically.

    The single source of truth for "apply reviewed OCR rows", used by BOTH the legacy single-shot ocr_apply
    and the batch ocr_job_apply (Phase 2.6) so the resolve+score logic lives in one place. Each row is
    {placement, kills, [damage/assists/bonus/penalty/played], resolution:{kind,...}}. Resolves every row's
    participant via _resolve_or_create_participant (real get-or-create / ghost creation, shared with
    add_participant), creates one LeaderboardMatch (auto-numbered), and scores each result via
    _save_one_result (shared with save_match_results). Returns (match, [unique participants]). Raises
    _ParticipantResolutionError (rolls the whole apply back) on empty rows or any row that cannot resolve,
    so the caller returns a clean 4xx and never leaves a partial map behind.
    """
    if not isinstance(rows, list) or not rows:
        raise _ParticipantResolutionError("rows must be a non-empty list.")
    # Validate every row carries a resolution dict BEFORE the transaction (cheap fail-fast).
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("resolution"), dict):
            raise _ParticipantResolutionError("each row must include a resolution object.")

    created_participants = []
    with transaction.atomic():
        # One apply = one new map. Auto-number it after the current max (mirrors add_match).
        last = lb.matches.order_by("-match_number").first()
        next_number = (last.match_number + 1) if last else 1
        match = LeaderboardMatch.objects.create(
            leaderboard=lb,
            match_number=next_number,
            match_map=(match_map or None),
        )
        # Resolve each row's participant (reusing existing ones / creating ghosts) and score it.
        for row in rows:
            participant = _resolve_or_create_participant(lb, row["resolution"], user)
            created_participants.append(participant)
            _save_one_result(match, participant, row, lb)

    # De-dupe participants for the response (a re-applied real entity could appear once per row).
    seen = set()
    unique_participants = []
    for p in created_participants:
        if p.id not in seen:
            seen.add(p.id)
            unique_participants.append(p)
    return match, unique_participants


# ════════════════════════════════════════════════════════════════════════════════════════════
# Task 2.6 — OCR BATCH (async, multi-image)
#
# Lets an admin upload SEVERAL maps, each with ONE OR MORE screenshots, and read them in the
# background so the synchronous request can never time out (the old ocr_extract died on prod's ~30s
# request cap because a Gemini read takes 12-26s). Flow:
#   POST jobs/            -> ocr_job_create: persist one map's screenshots as a pending LeaderboardOcrJob.
#   POST jobs/<id>/run/   -> ocr_job_run: enqueue that map's Celery read.
#   POST run-all/         -> ocr_run_all: enqueue EVERY pending/failed map (parallel "run as a group").
#   GET  jobs/            -> ocr_job_list: poll status + the merged review rows once done.
#   POST jobs/<id>/apply/ -> ocr_job_apply: turn the reviewed rows into a map (reuses _apply_ocr_rows).
#   DELETE jobs/<id>/     -> ocr_job_delete: discard a map's job + its screenshots.
# The heavy read/merge/match work is in afc_leaderboard.ocr.process_job (run by the Celery task); these
# views only persist, enqueue, serialize, and apply. can_manage_standalone_lb gates every endpoint.
# ════════════════════════════════════════════════════════════════════════════════════════════
def _serialize_job(job, image_count=None):
    """One OCR-job row for the FE poll. `rows` is the merged review table (null until the job is done);
    image_count is passed in by the list endpoint (prefetched) or counted here for a single job."""
    return {
        "id": str(job.id),
        "map_label": job.map_label,
        "status": job.status,
        "engine": job.engine,
        "error": job.error,
        "image_count": image_count if image_count is not None else job.images.count(),
        "applied_match_id": job.applied_match_id,
        # Only meaningful once status == "done"; the FE renders these in the editable review table.
        "rows": job.rows,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def _get_job_or_404(lb, job_id):
    """Fetch a LeaderboardOcrJob scoped to `lb` (so a caller can't poke another leaderboard's job), or
    return (None, Response 404). A malformed UUID raises on the query, so catch broadly and 404."""
    try:
        return LeaderboardOcrJob.objects.get(id=job_id, leaderboard=lb), None
    except (LeaderboardOcrJob.DoesNotExist, ValueError, ValidationError):
        return None, Response({"message": "OCR job not found."}, status=404)


@api_view(["POST"])
def ocr_job_create(request, lb_id):
    """
    POST leaderboards/standalone/<id>/ocr/jobs/  — create ONE map's OCR job from 1+ uploaded screenshots.

    Auth: Bearer SessionToken + can_manage_standalone_lb. Does NOT start the read (call run/ or run-all/).
    Request (multipart/form-data): images (1+ files, field name "images"), map_label (optional str).
    Response 201: { "job": <job dict, status="pending"> }.
    Errors: 404 leaderboard not found; 403 non-manager; 400 no images attached.
    Consumed by: the FE OcrBatchDialog (one job per map card).
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    files = request.FILES.getlist("images")
    if not files:
        return Response({"message": "Attach at least one screenshot for this map."}, status=400)

    map_label = (request.data.get("map_label") or "").strip()[:120]
    job = LeaderboardOcrJob.objects.create(leaderboard=lb, map_label=map_label, created_by=user)
    for i, f in enumerate(files):
        LeaderboardOcrImage.objects.create(job=job, image=f, order=i)
    return Response({"job": _serialize_job(job, image_count=len(files))}, status=201)


@api_view(["POST"])
def ocr_job_run(request, lb_id, job_id):
    """
    POST leaderboards/standalone/<id>/ocr/jobs/<job_id>/run/  — enqueue ONE map's background read.

    Auth: Bearer SessionToken + can_manage_standalone_lb. Resets the job to pending + clears any prior
    error, then dispatches the Celery task (process_leaderboard_ocr_job). Idempotent-ish: a job already
    processing is left alone. Response 200: { "job": <job dict> }.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)
    job, jnf = _get_job_or_404(lb, job_id)
    if jnf:
        return jnf
    if job.status == "processing":
        return Response({"job": _serialize_job(job)})  # already running; let the FE keep polling
    job.status = "pending"
    job.error = ""
    job.save(update_fields=["status", "error", "updated_at"])
    process_leaderboard_ocr_job.delay(str(job.id))
    return Response({"job": _serialize_job(job)})


@api_view(["POST"])
def ocr_run_all(request, lb_id):
    """
    POST leaderboards/standalone/<id>/ocr/run-all/  — enqueue EVERY not-yet-read map at once.

    Auth: Bearer SessionToken + can_manage_standalone_lb. Enqueues all jobs in pending/failed state so a
    whole batch processes in parallel across Celery workers (the owner's "run them as a group
    simultaneously"). Already-done/applied/processing jobs are skipped. Response 200:
    { "jobs": [<every job dict>], "queued": <count enqueued> }.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)

    to_run = list(lb.ocr_jobs.filter(status__in=["pending", "failed"]))
    for j in to_run:
        j.status = "pending"
        j.error = ""
        j.save(update_fields=["status", "error", "updated_at"])
        process_leaderboard_ocr_job.delay(str(j.id))
    jobs = lb.ocr_jobs.prefetch_related("images").all()
    return Response({"jobs": [_serialize_job(j, image_count=j.images.count()) for j in jobs], "queued": len(to_run)})


@api_view(["GET"])
def ocr_job_list(request, lb_id):
    """
    GET leaderboards/standalone/<id>/ocr/jobs/  — list this leaderboard's OCR jobs (the poll endpoint).

    Auth: Bearer SessionToken + can_manage_standalone_lb (OCR drafts are manager-only). Returns every job
    with its status + merged review rows (rows are null until done). The FE polls this until each job is
    done/failed. Response 200: { "jobs": [<job dict>] }.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to view this leaderboard."}, status=403)
    jobs = lb.ocr_jobs.prefetch_related("images").all()
    return Response({"jobs": [_serialize_job(j, image_count=j.images.count()) for j in jobs]})


@api_view(["POST"])
def ocr_job_apply(request, lb_id, job_id):
    """
    POST leaderboards/standalone/<id>/ocr/jobs/<job_id>/apply/  — apply ONE map's reviewed rows.

    Auth: Bearer SessionToken + can_manage_standalone_lb. Takes the admin-corrected rows (apply-shape:
    each row carries a `resolution`) and creates one map + participants + scored results via the shared
    _apply_ocr_rows helper, then marks the job applied + links the created map. Request body:
    { "rows": [<apply rows>], "match_map"?: str }. Response 200:
    { "match": <match dict>, "participants": [<participant dict>], "standings": [...] }.
    Errors: 404 leaderboard/job not found; 403 non-manager; 400 empty/invalid rows or an unresolvable row.
    Consumed by: the FE OcrBatchDialog "Apply" (per map) / "Apply all" (loops this per done map).
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)
    job, jnf = _get_job_or_404(lb, job_id)
    if jnf:
        return jnf

    # Idempotency guard (owner 2026-06-24 fix): re-applying an already-applied OCR job created a SECOND
    # map (the prior applied_match was orphaned) whose rows DOUBLE-COUNTED in the standings. Block a
    # second apply; the FE should edit/delete the existing map instead of re-applying the job.
    if job.status == "applied" and job.applied_match_id:
        return Response(
            {"message": "This OCR job has already been applied.",
             "code": "ocr_already_applied", "applied_match_id": job.applied_match_id},
            status=409,
        )

    data = request.data or {}
    # match_map: explicit override, else the map label the admin typed on the job.
    match_map = data.get("match_map") or job.map_label or None
    try:
        match, unique_participants = _apply_ocr_rows(lb, data.get("rows"), match_map, user)
    except _ParticipantResolutionError as e:
        return Response({"message": e.message}, status=e.status)

    job.status = "applied"
    job.applied_match = match
    job.save(update_fields=["status", "applied_match", "updated_at"])
    return Response({
        "match": _serialize_match(match),
        "participants": [_serialize_participant(p) for p in unique_participants],
        "standings": standalone_standings(lb),
    })


@api_view(["DELETE"])
def ocr_job_delete(request, lb_id, job_id):
    """
    DELETE leaderboards/standalone/<id>/ocr/jobs/<job_id>/  — discard a map's OCR job + its screenshots.

    Auth: Bearer SessionToken + can_manage_standalone_lb. Cascades the job's LeaderboardOcrImage rows. The
    created map (if already applied) is NOT deleted — only the OCR job. Response 200: { "message": ... }.
    """
    user, err = _auth_user(request)
    if err:
        return err
    lb, nf = _get_lb_or_404(lb_id)
    if nf:
        return nf
    if not can_manage_standalone_lb(user, lb):
        return Response({"message": "You do not have permission to edit this leaderboard."}, status=403)
    job, jnf = _get_job_or_404(lb, job_id)
    if jnf:
        return jnf
    job.delete()
    return Response({"message": "OCR job removed."})
