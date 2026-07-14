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
# ORM primitives for the SOLO combined aggregator (_solo_combined_standings below). Mirrors the
# imports advancement_routing._solo_standings / round_robin._aggregate_team_standings use.
from django.db.models import Case, Count, F, IntegerField, Sum, Value, When
from django.db.models.functions import Coalesce

from afc.api_utils import authenticate as _authenticate
from afc_organizers.permissions import org_can_event
from afc_organizers.models import OrgLeaderboardDesign
from afc_organizers.views_leaderboard_design import (
    build_field_layout, build_pages_for_export, build_ephemeral_afc_default)
from afc_leaderboard.graphic import render_leaderboard_graphic, render_design_all_pages

from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, StageGroupCompetitor, TournamentTeam,
    TournamentTeamMatchStats, SoloPlayerMatchStats)
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


def _design_max_rank(design, size):
    """How many ranked rows a design actually displays = the HIGHEST rank any of its column groups
    references (start_rank + row_count - 1), scanned across the design-level groups AND every page's
    groups for this size. A single-column board returns ~8; a 2-column board returns 16 (8 per column);
    a multi-page board returns the deepest rank across pages. Used to size how many standings rows to
    fetch so NO column/page is left with empty rows (owner 2026-07-01: the 2-column export showed an
    empty right column because standings were capped at the single design.max_rows = 8)."""
    yt = size == "youtube"

    def _rank(groups):
        m = 0
        for g in (groups or []):
            try:
                m = max(m, int(g.get("start_rank", 1) or 1) + int(g.get("row_count", 0) or 0) - 1)
            except Exception:
                pass
        return m

    ranks = [_rank((design.column_groups_youtube or design.column_groups) if yt
                   else design.column_groups)]
    for p in design.pages.all():
        ranks.append(_rank((p.column_groups_youtube or p.column_groups) if yt
                           else p.column_groups))
    return max(ranks) if ranks else 0


# ── COMBINE selection (owner 2026-07-05, complaint B) ─────────────────────────────────────────────
# The base export renders ONE group or ONE stage. Complaint B lets the user COMBINE the leaderboards
# of SELECTED units — any mix of whole STAGES and individual GROUPS — into ONE merged, re-ranked board
# and download it through the chosen design (paginated across ALL pages via ?page=all). The combine
# UNIT rule is owner-locked: whole stages AND individual groups are both selectable, and a stage
# expands to its groups.
#
# We deliberately REUSE the broadcast overlay's combine machinery instead of re-implementing it:
#   • _expand_overlay_combine(event, group_ids, stage_ids)  (afc_tournament_and_scrims.views, ~L18765)
#     resolves the selection to a validated, THIS-EVENT list of group ids (each stage expands to its
#     groups; cross-event / malformed ids are dropped so a forged id can never pull another event's
#     data). Imported LAZILY inside _parse_combine_selection to keep this module free of the 21k-line
#     views.py at import time (same pattern views.py uses to lazy-import round_robin / this module).
#   • round_robin._aggregate_team_standings(qs, event=event)  does the actual TEAM merge+re-rank — the
#     SAME aggregator every team-ranking surface (cumulative_standings / group_standings / the overlay
#     _overlay_cumulative_rows) funnels through, so a combined export agrees with the site's tables.
def _parse_combine_selection(request, event):
    """Read the combine selection off the export query (?group_ids=<csv|repeated>&stage_ids=<csv|
    repeated>) and resolve it to a validated, THIS-EVENT list of group ids to aggregate.

    Returns:
      • None  -> NO combine params were sent (the caller keeps the legacy single-group/stage path,
                 fully untouched).
      • []    -> combine params WERE sent but none resolved to a valid group of this event, so the
                 caller can return a clear 400 instead of silently rendering an empty board.
      • [g1, g2, ...] -> the deduped, sorted group ids to sum (each selected stage already expanded
                 to its groups by _expand_overlay_combine).

    Accepts both ?group_ids=1&group_ids=2 (repeated) and ?group_ids=1,2 (csv), matching the overlay's
    _parse_overlay_combine ergonomics. Shared by event_stage_graphic only; the overlay has its own
    parser (views._parse_overlay_combine) reading ?groups=/?stages= for the iframe link."""
    def _multi(name):
        out = []
        for v in request.query_params.getlist(name):   # repeated ?group_ids=1&group_ids=2
            out.extend(str(v).split(","))               # and the ?group_ids=1,2 csv form
        return [x.strip() for x in out if str(x).strip()]

    groups = _multi("group_ids")
    stages = _multi("stage_ids")
    if not groups and not stages:
        return None
    # Lazy import: views.py is the 21k-line module and is NOT loaded at this module's top level.
    from .views import _expand_overlay_combine
    return _expand_overlay_combine(event, groups, stages)


def _solo_combined_standings(group_ids):
    """Combined SOLO standings across a set of groups (owner 2026-07-05, complaint B) — the solo twin
    of the TEAM combine path (round_robin._aggregate_team_standings over the matched TTMS).

    Folds every SoloPlayerMatchStats row whose match is in `group_ids` into ONE per-competitor points
    table, re-ranked. Mirrors advancement_routing._solo_standings (the canonical solo ranking: summed
    total_points, then kills) but ADDITIONALLY surfaces the placement/kill/bonus/penalty point splits +
    games_played + booyah, so a design that places those columns renders real numbers rather than a
    blank cell (a bare _solo_standings only carries total_points/kills/booyah).

    KEY-SHAPE NOTE: rows use the SAME stat keys the export's row-builder reads for teams
    (effective_total / placement_sum / kill_sum / bonus_sum / penalty_sum / total_kills / total_booyah
    / games_played) but carry competitor identity (competitor_id + username) instead of
    tournament_team_id + team_name. event_stage_graphic's solo branch maps username -> the design's
    team_name column so the placed name column shows the player.

    TIE-BREAK CAVEAT (Task 3): solo uses the fixed -effective_total -> -total_kills -> username chain
    (identical to _solo_standings / the legacy solo advance). The event's CONFIG tie-breakers
    (round_robin.apply_tie_breakers) are TEAM-scoped — they read tournament_team_id + team fields — so
    they are intentionally NOT applied to solo here, exactly as the single-stage solo standings path
    does not apply them either. Team combines DO honour the event-level config tie-breakers (via
    _aggregate_team_standings(event=event))."""
    qs = SoloPlayerMatchStats.objects.filter(match__group_id__in=group_ids)
    rows = (
        qs
        .values("competitor_id", username=F("competitor__user__username"))
        .annotate(
            games_played=Count("match_id"),
            total_kills=Coalesce(Sum("kills"), 0),
            total_booyah=Coalesce(Sum(
                Case(When(placement=1, then=Value(1)), default=Value(0),
                     output_field=IntegerField())), 0),
            placement_sum=Coalesce(Sum("placement_points"), 0),
            kill_sum=Coalesce(Sum("kill_points"), 0),
            bonus_sum=Coalesce(Sum("bonus_points"), 0),
            penalty_sum=Coalesce(Sum("penalty_points"), 0),
            # effective_total = the authoritative solo score = the stored per-match total_points summed
            # (solo total_points is already placement+kill+bonus-penalty; see SoloPlayerMatchStats).
            effective_total=Coalesce(Sum("total_points"), 0),
        )
        .order_by("-effective_total", "-total_kills", "username")
    )
    return list(rows)


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

    # ── COMBINE selection (owner 2026-07-05, complaint B) ──────────────────────────────────────────
    # ?group_ids= / ?stage_ids= (csv or repeated) turn this into a COMBINED export: sum the standings
    # of ALL selected groups (each stage expands to its groups) into one merged, re-ranked board. See
    # _parse_combine_selection: None => no combine params (legacy single-group/stage path untouched);
    # [] => params sent but nothing valid resolved (400 below, never an empty board); [ids] => combine.
    combine_group_ids = _parse_combine_selection(request, event)
    is_combined = combine_group_ids is not None

    # SOLO: the single-stage/group graphic export is still unsupported (400 as before) — but a COMBINE
    # export IS supported through _solo_combined_standings, so only 400 for solo when this is NOT a
    # combine request (owner 2026-07-05: combined solo boards download too).
    if event.participant_type == "solo" and not is_combined:
        return Response({"message": "Graphic export for solo stages is not available yet."},
                        status=status.HTTP_400_BAD_REQUEST)
    # Combine params sent but nothing valid resolved (all cross-event / malformed) => clear 400.
    if is_combined and not combine_group_ids:
        return Response(
            {"message": "No valid groups or stages were selected for the combined export."},
            status=status.HTTP_400_BAD_REQUEST)

    size = (request.query_params.get("size") or "youtube").lower()
    if size not in ("instagram", "youtube"):
        size = "youtube"

    design = _resolve_event_design(event, request.query_params.get("design_id"))
    # Fetch enough standings for EVERY column/page the design shows (a 2-column board needs 16, not the
    # single design.max_rows=8), so no column is left with empty rows. Falls back to design.max_rows.
    max_rows = (_design_max_rank(design, size) if design else 0) or (design.max_rows if design else 16)

    # Standings source. Three shapes, one downstream render path:
    #   • COMBINED (owner 2026-07-05, complaint B): sum every competitor across the selected groups.
    #     Teams reuse round_robin._aggregate_team_standings over the union of matched TTMS (the SAME
    #     aggregator + event-level tie-breakers the broadcast overlay's _overlay_cumulative_rows uses,
    #     so a combined export equals the site's cumulative tables); solo uses _solo_combined_standings.
    #   • single GROUP (owner 2026-06-16 fix): the event leaderboard PAGE shows a single group's
    #     "Overall Leaderboard" (TTMS filtered by match__group=group), so ?group_id renders THAT group.
    #   • whole STAGE (default): the stage-wide cumulative table (all groups of the stage).
    is_solo_combined = is_combined and event.participant_type == "solo"
    group = None  # hoisted so the seeded-team zero-fill below can scope by group in every branch
    if is_combined:
        if is_solo_combined:
            standings = _solo_combined_standings(combine_group_ids)
        else:
            qs = TournamentTeamMatchStats.objects.filter(match__group_id__in=combine_group_ids)
            standings = round_robin._aggregate_team_standings(qs, event=event)
    else:
        group_id = request.query_params.get("group_id")
        if group_id and str(group_id).isdigit():
            group = StageGroups.objects.filter(group_id=int(group_id), stage=stage).first()
        standings = (
            round_robin.group_standings(group) if group
            else round_robin.cumulative_standings(stage)
        )

    # ── Include SEEDED teams that have NO results entered yet, so a team with no scores still shows
    #    on the downloaded graphic (owner 2026-07-13: "downloaded leaderboard did not add the last
    #    team, but they should be there regardless"). ─────────────────────────────────────────────
    # WHY: the standings above come from round_robin (an aggregate over TournamentTeamMatchStats), so
    # a team SEEDED into the stage/group but not yet scored produces zero match-stats rows and is
    # silently absent from the export. The on-site leaderboard PAGE already fixes this by zero-filling
    # seeded competitors (afc_tournament_and_scrims/views.py get_all_leaderboard_details_for_event,
    # the "Include SEEDED competitors with no results yet" block, owner 2026-06-21) — the download was
    # the only surface still dropping them, so the page showed the team while the PNG did not. This
    # brings the export to PARITY with the page: append a 0-point row (keyed exactly like the
    # round_robin rows: tournament_team_id / team_name / team_country / games_played / *_sum /
    # effective_total) for every ACTIVE seeded team missing from `standings`, then STABLE-sort by
    # effective_total so scored teams keep the aggregator's order (incl. config tie-breakers) and the
    # 0-point seeded teams sink to the bottom (alphabetical among themselves). Runs BEFORE the design
    # row cap below, so scored teams still win the limited slots on a small design. TEAM events only
    # (solo-combined rows carry username, not tournament_team, so there is no seeded-team roster to
    # zero-fill here — left as-is). Opt out with ?include_unscored=0 for the legacy scored-only board.
    include_unscored = str(
        request.query_params.get("include_unscored") or "1"
    ).strip().lower() not in ("0", "false", "no", "off")
    if include_unscored and not is_solo_combined:
        # Scope the seeded roster the same way the standings were scoped:
        #   combined -> the selected groups' competitors; single group -> that group's; whole stage
        #   -> every group of the stage. StageGroupCompetitor is the per-lobby seeding the page reads.
        if is_combined:
            seeded_qs = StageGroupCompetitor.objects.filter(stage_group_id__in=combine_group_ids)
        elif group is not None:
            seeded_qs = StageGroupCompetitor.objects.filter(stage_group=group)
        else:
            seeded_qs = StageGroupCompetitor.objects.filter(stage_group__stage=stage)
        seeded_qs = (
            seeded_qs.filter(status="active", tournament_team__isnull=False)
            .select_related("tournament_team__team")
        )
        present_ids = {r.get("tournament_team_id") for r in standings}
        extra_rows, seen_seed = [], set()
        for sc in seeded_qs:
            tt_id = sc.tournament_team_id
            if tt_id in present_ids or tt_id in seen_seed:
                continue  # already scored, or the same team seeded into two of the combined groups
            seen_seed.add(tt_id)
            team = sc.tournament_team.team if sc.tournament_team else None
            extra_rows.append({
                "tournament_team_id": tt_id,
                "team_name": (team.team_name if team else "") or "",
                "team_country": (team.country if team else "") or "",
                "games_played": 0, "total_kills": 0, "total_booyah": 0,
                "placement_sum": 0, "kill_sum": 0, "bonus_sum": 0, "penalty_sum": 0,
                "effective_total": 0, "last_match_placement": 999,
            })
        if extra_rows:
            extra_rows.sort(key=lambda r: (r["team_name"] or "").lower())
            # Stable sort keeps the aggregator's ordering for the real (scored) rows untouched and
            # only lowers the 0-point seeded rows to the bottom (Python sort is stable).
            standings = sorted(
                list(standings) + extra_rows,
                key=lambda r: -int(r.get("effective_total", 0) or 0),
            )
    # A SAVED design shows a fixed number of rows, so we cap the fetched standings to what it displays.
    # The branded-default FALLBACK (design is None, ?plain not set) instead paginates ALL rows by its
    # row->page rule, so it must NOT be capped here (owner 2026-07-05, complaint J: auto row detection
    # off the real standings length, not the hardcoded 16). ?plain=1 keeps the legacy capped table.
    plain = str(request.query_params.get("plain") or "").strip().lower() in ("1", "true", "yes", "on")
    if not (design is None and not plain):
        standings = standings[: max(1, max_rows)]

    # Team logos in bulk (tournament_team_id -> team_logo filesystem path). SOLO rows carry a
    # competitor_id + username (no tournament_team), so there is no team logo/flag to fetch — the PNG
    # field-layout renderer just skips a team_logo/team_flag cell whose value is None (see graphic.py
    # _render_fields), so a combined SOLO board renders its name + stat columns cleanly.
    logo_by_tt = {}
    country_by_tt = {}  # country flag column (owner 2026-07-04)
    if not is_solo_combined:
        tt_ids = [r["tournament_team_id"] for r in standings]
        for tt in TournamentTeam.objects.filter(
                tournament_team_id__in=tt_ids).select_related("team"):
            try:
                if tt.team and tt.team.team_logo:
                    logo_by_tt[tt.tournament_team_id] = tt.team.team_logo.path
            except Exception:
                pass
            if tt.team:
                country_by_tt[tt.tournament_team_id] = tt.team.country or ""

    # Per-row dicts keyed by field_type (the field-layout path reads these); also a legacy-shaped
    # list so a design with NO placed fields still renders via the built-in auto-table.
    rows, legacy = [], []
    for i, r in enumerate(standings):
        # SOLO combined: identity is competitor_id + username; map the username onto the design's
        # team_name column (mirrors the overlay solo feed), no team logo/flag. TEAM: tournament_team.
        if is_solo_combined:
            tt_id = None
            name = r.get("username") or "-"
        else:
            tt_id = r["tournament_team_id"]
            name = r.get("team_name") or "-"
        rows.append({
            "pos": i + 1,
            "team_name": name,
            "team_logo": logo_by_tt.get(tt_id),
            "team_country": country_by_tt.get(tt_id, ""),
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
        # A combined board spans multiple stages/groups, so a single stage name would mislead — leave
        # the subtitle blank for combine (the FE / design's own freeform header supplies any label).
        subtitle = "" if is_combined else (stage.stage_name or "")
    # Filename scope tag (owner 2026-07-05, complaint B): "combined" for a combine export, else the
    # stage name. Used by every download filename below so a combined graphic saves as "<event>-combined".
    scope_label = "combined" if is_combined else (stage.stage_name or "stage")

    # ── Multi-page export (owner 2026-06-14) ── ?page=all returns a ZIP of one PNG per design page.
    # Backward compatible: any other (or no) ?page value falls through to the single-PNG path below.
    # Consumed by the FE EventStageExportGraphicDialog, which requests page=all when the design has
    # >1 page. build_pages_for_export returns 1 entry for a single-page (legacy) design, N otherwise.
    page_param = (request.query_params.get("page") or "").strip().lower()
    want_all_pages = (page_param == "all") and design is not None

    # ══ Branded AFC default FALLBACK (owner 2026-07-05, audit complaint J) ═══════════════════════
    # When the event's design library is EMPTY (design is None), do NOT drop to the legacy bare dark
    # auto-table below. Build an EPHEMERAL AFC-branded default (identical look to create_default_design)
    # sized to the ACTUAL standings length and render THROUGH it. Nothing is persisted to the library.
    #   row->page rule (see build_ephemeral_afc_default): n<=12 -> one 12-row page; n<=15 -> one 15-row
    #   page; n>15 -> pages of 24 (two 12-row columns), ceil(n/24) pages. AUTO row detection = len(rows).
    # ?page=all zips every page; ?page=<N> renders that page; no ?page renders page 1. ?plain=1 keeps
    # the legacy bare table (falls through to the single-PNG path below with field_layout=None).
    if design is None and not plain and rows:
        eph = build_ephemeral_afc_default(
            len(rows), org=(event.organization if event.organization_id else None))
        safe_name = f"{event.event_name}-{scope_label}".replace(" ", "_")
        # page=all -> ZIP of every page (only when >1 page; a 1-page default falls to the single return).
        if page_param == "all" and eph.page_count > 1:
            pngs = render_design_all_pages(
                rows, eph.pages_spec, size=size, logos=eph.logos, title=title, subtitle=subtitle,
                text_color=eph.text_color, accent_color=eph.accent_color, max_rows=eph.max_rows,
                show_title=eph.show_title, show_subtitle=eph.show_subtitle,
                transparent_background=eph.transparent_background)
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, png in enumerate(pngs, start=1):
                    zf.writestr(f"{safe_name}-{size}-page{i}.png", png)
            zip_buf.seek(0)
            resp = HttpResponse(zip_buf.read(), content_type="application/zip")
            resp["Content-Disposition"] = f'attachment; filename="{safe_name}-{size}-all-pages.zip"'
            return resp
        # page=<N> -> that page only (1-based, clamped to range); anything else -> page 1.
        idx = int(page_param) - 1 if (page_param.isdigit()
                                      and 1 <= int(page_param) <= eph.page_count) else 0
        pngs = render_design_all_pages(
            rows, [eph.pages_spec[idx]], size=size, logos=eph.logos, title=title, subtitle=subtitle,
            text_color=eph.text_color, accent_color=eph.accent_color, max_rows=eph.max_rows,
            show_title=eph.show_title, show_subtitle=eph.show_subtitle,
            transparent_background=eph.transparent_background)
        resp = HttpResponse(pngs[0], content_type="image/png")
        suffix = f"-page{idx + 1}" if eph.page_count > 1 else ""
        fname = f"{safe_name}-{size}{suffix}.png"
        resp["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp

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
            fname = f"{event.event_name}-{scope_label}-{size}-page{n}.png".replace(" ", "_")
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
            safe_name = f"{event.event_name}-{scope_label}".replace(" ", "_")
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
    fname = f"{event.event_name}-{scope_label}-{size}.png".replace(" ", "_")
    resp["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp
