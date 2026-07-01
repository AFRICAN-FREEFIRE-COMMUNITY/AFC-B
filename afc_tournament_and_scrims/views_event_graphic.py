"""
afc_tournament_and_scrims.views_event_graphic — render an EVENT stage's standings onto a
leaderboard DESIGN (owner 2026-06-14).

The standalone-leaderboard design system (afc_organizers.OrgLeaderboardDesign + the positionable
connected-column FIELDS / freeform TEXT / uploaded FONTS added 2026-06-14, rendered by
afc_leaderboard.graphic) is reused here for EVENTS: the Dynasty Cup qualifiers/finals are events
with stages, and their cumulative stage standings carry every stat a design places (POS, team,
booyah, placement points, kill points, total). This endpoint pulls a stage's standings, maps them
to the renderer's per-row dicts, resolves the chosen design + size, and returns the PNG.

ENDPOINT (mounted via afc_tournament_and_scrims/urls.py)
    GET events/<event_id>/stages/<stage_id>/graphic/?design_id=&size=&title=&subtitle=

AUTH: an AFC event admin, or an organizer who can_edit_events on the event's org.
CONSUMED BY: the frontend EventGroupExportGraphicDialog on the event leaderboard page.

NOTE (rush): cumulative_standings is the raw whole-stage table; the per-lobby Point-Rush carry-over
is NOT folded in here (matches round_robin's spec), so a placed RUSH column renders empty for a
stage exported this way. Booyah/PP/KP/TP/kills/matches are all present.
"""
import io
import zipfile

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.http import HttpResponse

from afc.api_utils import authenticate as _authenticate
from afc_organizers.permissions import org_can_event
from afc_organizers.models import OrgLeaderboardDesign
from afc_organizers.views_leaderboard_design import build_field_layout, build_pages_for_export
from afc_leaderboard.graphic import render_leaderboard_graphic, render_design_all_pages

from afc_tournament_and_scrims.models import Event, Stages, StageGroups, TournamentTeam
from afc_tournament_and_scrims import round_robin
from afc_tournament_and_scrims.event_links import _is_event_admin


def _resolve_event_design(event, design_id):
    """Pick the design to render: the event org's library (or the AFC-native library when the event
    has no org). Honour an explicit design_id, else the library default, else any. Prefetches the
    placed logos/fields/texts (+ their fonts) so build_field_layout does not N+1."""
    org_id = event.organization_id
    lib = (
        OrgLeaderboardDesign.objects.filter(organization__isnull=True)
        if org_id is None
        else OrgLeaderboardDesign.objects.filter(organization_id=org_id)
    ).prefetch_related("logos", "fields", "fields__font", "texts", "texts__font")
    design = None
    if design_id and str(design_id).isdigit():
        design = lib.filter(id=int(design_id)).first()
    if design is None:
        design = lib.filter(is_default=True).first() or lib.first()
    return design


@api_view(["GET"])
def event_stage_graphic(request, event_id, stage_id):
    """Render `stage`'s cumulative standings onto a chosen design and return a PNG download."""
    user, err = _authenticate(request)
    if err:
        return err

    event = Event.objects.select_related("organization").filter(event_id=event_id).first()
    if not event:
        return Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)
    stage = Stages.objects.filter(stage_id=stage_id, event=event).first()
    if not stage:
        return Response({"message": "Stage not found."}, status=status.HTTP_404_NOT_FOUND)
    # Gate: AFC event admin, or an organizer who can edit this event's org.
    if not (_is_event_admin(user) or org_can_event(user, "can_edit_events", event)):
        return Response({"message": "You do not have permission to export this event."},
                        status=status.HTTP_403_FORBIDDEN)
    # v1 supports TEAM stages (cumulative_standings is team-based); the Dynasty Cup is team.
    if event.participant_type == "solo":
        return Response({"message": "Graphic export for solo stages is not available yet."},
                        status=status.HTTP_400_BAD_REQUEST)

    size = (request.query_params.get("size") or "youtube").lower()
    if size not in ("instagram", "youtube"):
        size = "youtube"

    design = _resolve_event_design(event, request.query_params.get("design_id"))
    max_rows = design.max_rows if design else 16

    # Standings source (owner 2026-06-16 fix): the event leaderboard PAGE shows a single GROUP's
    # "Overall Leaderboard" (TTMS filtered by match__group=group). The export must mirror EXACTLY
    # what the user sees, so when the FE passes the selected group_id we render THAT group's
    # standings; otherwise we fall back to the whole-stage cumulative table (all groups). Without
    # this, a multi-group stage (or a group whose stage differs from the requested stage_id) exported
    # the design background with NO rows because the stage-wide query missed the per-group data.
    group_id = request.query_params.get("group_id")
    group = None
    if group_id and str(group_id).isdigit():
        group = StageGroups.objects.filter(group_id=int(group_id), stage=stage).first()
    standings = (
        round_robin.group_standings(group) if group
        else round_robin.cumulative_standings(stage)
    )[: max(1, max_rows)]

    # Team logos in bulk (tournament_team_id -> team_logo filesystem path).
    tt_ids = [r["tournament_team_id"] for r in standings]
    logo_by_tt = {}
    for tt in TournamentTeam.objects.filter(tournament_team_id__in=tt_ids).select_related("team"):
        try:
            if tt.team and tt.team.team_logo:
                logo_by_tt[tt.tournament_team_id] = tt.team.team_logo.path
        except Exception:
            pass

    # Per-row dicts keyed by field_type (the field-layout path reads these); also a legacy-shaped
    # list so a design with NO placed fields still renders via the built-in auto-table.
    rows, legacy = [], []
    for i, r in enumerate(standings):
        tt_id = r["tournament_team_id"]
        name = r.get("team_name") or "-"
        rows.append({
            "pos": i + 1,
            "team_name": name,
            "team_logo": logo_by_tt.get(tt_id),
            "booyah": r.get("total_booyah", 0),
            "placement_points": r.get("placement_sum", 0),
            "kill_points": r.get("kill_sum", 0),
            "total_points": r.get("effective_total", 0),
            "kills": r.get("total_kills", 0),
            "matches": r.get("games_played", 0),
            "base_total": r.get("effective_total", 0),
            "bonus": r.get("bonus_sum", 0),
            "penalty": r.get("penalty_sum", 0),
        })
        legacy.append({
            "rank": i + 1,
            "participant": {"name": name},
            "total_points": r.get("effective_total", 0),
            "kills": r.get("total_kills", 0),
        })

    field_layout = build_field_layout(design, size=size) if design else None

    # Background for this size + positioned logos from the design.
    bg = None
    logos = []
    text_color, accent_color, show_title, show_subtitle = "#FFFFFF", "#34d27b", True, True
    # Transparent-overlay flag (owner 2026-07-01): a design flagged transparent_background renders on a
    # transparent RGBA canvas (no dark fill) so the exported PNG matches the live overlay. Default False.
    transparent_bg = False
    if design:
        f = design.background_youtube if size == "youtube" else design.background_instagram
        try:
            bg = f.path if f else None
        except Exception:
            bg = None
        for lg in design.logos.all():
            try:
                logos.append({"path": lg.image.path, "x_pct": lg.x_pct, "y_pct": lg.y_pct, "size": lg.size})
            except Exception:
                pass
        text_color, accent_color = design.text_color, design.accent_color
        show_title, show_subtitle = design.show_title, design.show_subtitle
        transparent_bg = design.transparent_background

    title = request.query_params.get("title") or event.event_name
    subtitle = request.query_params.get("subtitle")
    if subtitle is None:
        subtitle = stage.stage_name or ""

    # ── Multi-page export (owner 2026-06-14) ── ?page=all returns a ZIP of one PNG per design page.
    # Backward compatible: any other (or no) ?page value falls through to the single-PNG path below.
    # Consumed by the FE EventStageExportGraphicDialog, which requests page=all when the design has
    # >1 page. build_pages_for_export returns 1 entry for a single-page (legacy) design, N otherwise.
    page_param = (request.query_params.get("page") or "").strip().lower()
    want_all_pages = (page_param == "all") and design is not None

    # ?page=<N> (owner 2026-06-16): render ONLY page N of a multi-page design as a single PNG, so the
    # FE can download each page as a SEPARATE image instead of one ZIP (owner prefers multiple images).
    # ?page=all still returns the ZIP (kept for compatibility / fallback).
    if design is not None and page_param.isdigit():
        pages_spec = build_pages_for_export(design, size=size)
        n = int(page_param)
        if 1 <= n <= len(pages_spec):
            pngs = render_design_all_pages(
                rows, [pages_spec[n - 1]], size=size,
                logos=logos, title=title, subtitle=subtitle,
                text_color=text_color, accent_color=accent_color,
                max_rows=max_rows, show_title=show_title, show_subtitle=show_subtitle,
                transparent_background=transparent_bg,
            )
            resp = HttpResponse(pngs[0], content_type="image/png")
            fname = f"{event.event_name}-{stage.stage_name or 'stage'}-{size}-page{n}.png".replace(" ", "_")
            resp["Content-Disposition"] = f'attachment; filename="{fname}"'
            return resp
        # invalid page index -> fall through to the single-PNG path below.

    if want_all_pages:
        pages_spec = build_pages_for_export(design, size=size)
        if len(pages_spec) > 1:
            pngs = render_design_all_pages(
                rows, pages_spec, size=size,
                logos=logos, title=title, subtitle=subtitle,
                text_color=text_color, accent_color=accent_color,
                max_rows=max_rows, show_title=show_title, show_subtitle=show_subtitle,
                transparent_background=transparent_bg,
            )
            zip_buf = io.BytesIO()
            safe_name = f"{event.event_name}-{stage.stage_name or 'stage'}".replace(" ", "_")
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, png in enumerate(pngs, start=1):
                    zf.writestr(f"{safe_name}-{size}-page{i}.png", png)
            zip_buf.seek(0)
            resp = HttpResponse(zip_buf.read(), content_type="application/zip")
            resp["Content-Disposition"] = f'attachment; filename="{safe_name}-{size}-all-pages.zip"'
            return resp
        # else: design is actually single-page; fall through to the single-PNG path below.

    png = render_leaderboard_graphic(
        legacy, size=size, background_path=bg, logos=logos, title=title, subtitle=subtitle,
        text_color=text_color, accent_color=accent_color, max_rows=max_rows,
        show_title=show_title, show_subtitle=show_subtitle,
        field_layout=field_layout, rows=rows, transparent_background=transparent_bg,
    )
    resp = HttpResponse(png, content_type="image/png")
    fname = f"{event.event_name}-{stage.stage_name or 'stage'}-{size}.png".replace(" ", "_")
    resp["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp
