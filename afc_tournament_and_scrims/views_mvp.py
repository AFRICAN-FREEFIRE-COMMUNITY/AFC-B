# ── EVENT MVP (owner 2026-07-02, per-map semantics) ─────────────────────────────
# "MVP picked from" has two options: the OVERALL EVENT PER MAP, or the WINNING TEAM PER MAP (the
# team that won that map). An MVP is decided for EVERY MAP (match) by the arranged criteria — the
# ordered criteria act like tie-breakers (kills first, ties fall to damage, ...). The EVENT MVP is
# then the player with the HIGHEST NUMBER of per-map MVPs; equal counts fall back to the same
# criteria on event totals. The per-player MVP COUNT is also intended as a leaderboard TIE-BREAKER
# criterion (owner: "mvp should then be a criteria to be used for tie breaker" — consumed when the
# leaderboard tie-breaker feature lands; the count is computed here).
#
# ENDPOINT (gate = _broadcast_gate = AFC event admin OR org can_edit_events):
#   GET  events/<event_id>/mvp/  -> compute with the event's SAVED config (Event.mvp_config)
#   POST events/<event_id>/mvp/  -> {criteria: [...], scope} — save, then return the recomputed
#                                   ranking (save + preview in one round trip).
#
# AVAILABLE vs PENDING criteria: kills / damage / assists are stored today. deaths, survival_time,
# headshots, kdr arrive with the 3D-room debugger ingest (tasks/overlay-scene-panel-plan.md) — they
# are declared available=False so the FE tags them "needs live 3D-room data"; a saved config that
# includes them simply skips them at compute time until the data exists.
#
# CONSUMED BY: the "MVPs" tab on app/(a)/a/leaderboards/[id]/edit (MvpTab.tsx).

from collections import defaultdict

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Event, TournamentPlayerMatchStats
# _broadcast_gate = the AFC-event-admin OR org-can_edit_events gate every broadcast surface uses.
# _expand_overlay_combine = the leaderboard overlay's COMBINE validator (views.py ~L18941): it turns a
# {group_ids, stage_ids} selection into the concrete list of THIS-EVENT group ids (whole stages expand
# to their groups; cross-event / malformed ids dropped). REUSED here so an MVP / top-killers board's
# combine scope resolves EXACTLY like a leaderboard board's (owner-locked unit = whole stages + groups).
from .views import _broadcast_gate, _expand_overlay_combine

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# CONTRACT (for the FE agent building the MVP (G) + Top-killers (H) overlays/designs) ─ owner 2026-07-05
# ──────────────────────────────────────────────────────────────────────────────────────────────────
# TWO new player-driven board KINDS, both rendered THROUGH any design and both COMBINE-aware (whole
# STAGES + individual GROUPS). They share ONE aggregation shape, ONE design-row shape, ONE render path,
# so the FE builds ONE renderer for both.
#
# 1) RANKED-PLAYER dict (compute_event_mvp / compute_top_killers -> "players": [...]):
#      { user_id, username, in_game_name, team_name, team_country, esports_image (URL|None),
#        kills, damage, assists, matches, mvp_count, deaths, survival_time, headshots, kdr }
#    MVP is ranked by per-map-MVP COUNT then the arranged criteria; Top-killers by SUM(kills) then
#    damage then assists. Both cap at 50.
#
# 2) DESIGN ROW (build_player_design_rows -> what a design/graphic renders; keyed by the PLAYER
#    FIELD_CHOICES field types added to afc_organizers.OrgLeaderboardDesignField):
#      { "pos": <player rank int>,        # reuses the existing `pos` field type
#        "player_name": <IGN str>,        # NEW field type (player's in-game name)
#        "team_name": <team str>,         # reuses `team_name`
#        "team_country": <iso2|name str>, # optional `team_flag` column (image)
#        "esports_image": <URL|path|None>,# player PHOTO -> renders as an IMAGE (reuses `esports_image`)
#        "kills": int, "damage": int,     # `damage` + `assists` are NEW field types
#        "assists": int, "mvp_count": int,# `mvp_count` NEW (map MVPs won; the MVP kind's headline stat)
#        "matches": int }                 # reuses `matches`
#    esports_image is a URL in the overlay JSON (FE <img>) and is resolved to a local media file by
#    graphic.py at PNG-render time, so the SAME rows render both in the browser and in the download.
#
# 3) OVERLAY PAYLOAD (views_overlays._mvp_payload / _top_killers_payload, bundled into overlay_config
#    exactly like _h2h_payload): { kind, players:[<design row>...], top:<design row|None>,
#      combine:{group_ids:[...]|None, combined:bool}, design:<_design_look|None> }.
#
# 4) COMBINE params (endpoints + overlay config): group_ids[] and/or stage_ids[] (whole stages expand
#    to their groups); absent => whole event. Same {scope, group_ids, stage_ids} shape complaint C used
#    for leaderboards. Singular group_id/stage_id are folded in for convenience.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

# criterion -> (label, available_now, higher_is_better). Order here = the default arrangement.
CRITERIA_META = {
    "kills":         ("Kills", True, True),
    "damage":        ("Damage", True, True),
    "assists":       ("Assists", True, True),
    "deaths":        ("Deaths", False, False),          # 3D-room ingest pending (fewer = better)
    "survival_time": ("Survival time", False, True),    # 3D-room ingest pending
    "headshots":     ("Headshots", False, True),        # 3D-room ingest pending
    "kdr":           ("K/D ratio", False, True),        # needs deaths -> pending with it
}
DEFAULT_CRITERIA = ["kills", "damage", "assists"]
DEFAULT_SCOPE = "overall"


def _crit_key(line_stats, rankable):
    """The tie-breaker sort tuple for one stat dict over the ordered rankable criteria
    (higher-is-better values as-is; lower-is-better ones negated so one reverse sort works)."""
    return tuple(
        (line_stats.get(c, 0) if CRITERIA_META[c][2] else -(line_stats.get(c, 0)))
        for c in rankable
    )


# ── COMBINE SCOPE for player boards (owner 2026-07-05, complaints G+H) ─────────────────────────────
# The MVP (G) + Top-killers (H) boards compute over the WHOLE EVENT (default) OR over a COMBINED
# selection of whole STAGES + individual GROUPS. We REUSE the leaderboard overlay's validator so a
# player board's scope resolves to concrete this-event group ids exactly like a team board's does.

def _read_scope_params(request):
    """Read a {group_ids, stage_ids} selection off an event_mvp / top-killers request. Accepts the
    repeated (?group_ids=1&group_ids=2), csv (?group_ids=1,2) and JSON-array body forms, plus a
    singular group_id/stage_id folded in. Returns (group_ids, stage_ids) as raw str lists (validation
    happens in _resolve_player_scope). Mirrors the leaderboard export's _parse_combine_selection."""
    def _collect(plural, singular):
        vals = []
        body = request.data.get(plural) if hasattr(request, "data") else None
        if isinstance(body, (list, tuple)):
            vals.extend(body)                          # JSON body: a real list
        elif body not in (None, ""):
            vals.extend(str(body).split(","))          # JSON body: a csv string
        for v in request.query_params.getlist(plural):  # query: repeated and/or csv
            vals.extend(str(v).split(","))
        one = (request.data.get(singular) if hasattr(request, "data") else None) \
            or request.query_params.get(singular)      # singular group_id / stage_id fold-in
        if one not in (None, ""):
            vals.append(one)
        return [str(x).strip() for x in vals if str(x).strip()]
    return _collect("group_ids", "group_id"), _collect("stage_ids", "stage_id")


def _resolve_player_scope(event, group_ids, stage_ids):
    """Expand a {group_ids, stage_ids} selection to the concrete list of THIS-EVENT group ids to
    aggregate over, or None (= whole event) when nothing was selected. An all-invalid selection also
    yields None (graceful fall-through to whole event, never an empty board) — mirrors the overlay's
    _parse_overlay_combine. Delegates to _expand_overlay_combine so G/H agree with the leaderboard."""
    if not group_ids and not stage_ids:
        return None
    return _expand_overlay_combine(event, group_ids, stage_ids) or None


def _player_match_qs(event, group_ids):
    """Every player stat line of the event, optionally restricted to `group_ids` (None = whole event).
    group_ids are already validated to this event by _resolve_player_scope, so filtering by group id is
    safe. select_related pulls identity + match/group/stage + team in one query (no N+1)."""
    qs = TournamentPlayerMatchStats.objects.select_related(
        "player", "team_stats", "team_stats__match", "team_stats__match__group",
        "team_stats__match__group__stage", "team_stats__tournament_team__team",
    )
    if group_ids is None:
        return qs.filter(team_stats__match__group__stage__event=event)
    return qs.filter(team_stats__match__group_id__in=group_ids)


def _walk_player_stats(event, group_ids, request):
    """Accumulate this event's (or the scoped groups') player stat lines into:
      by_match : {match_id: [(match, line_stats), ...]}   — per-map pools for MVP selection
      players  : {user_id: {identity + summed kills/damage/assists/deaths/... + matches + mvp_count}}
    esports_image is the player's esport photo URL (User.esports_pic lives on UserProfile — bug fix
    2026-07-02). SHARED by compute_event_mvp (per-map MVP ranking) and compute_top_killers (sum-of-kills
    ranking) so the MVP (G) and Top-killers (H) boards aggregate identically."""
    from afc_auth.models import esports_pic_url
    by_match = defaultdict(list)   # match_id -> [(match, player line stats)]
    players = {}                   # user_id -> accumulated scope totals + identity
    for s in _player_match_qs(event, group_ids):
        p = s.player
        if p is None:
            continue
        m = s.team_stats.match
        line = {
            "user_id": p.user_id,
            "kills": s.kills or 0,
            "damage": s.damage or 0,
            "assists": s.assists or 0,
            # 3D-room rich stats (0 until the debugger backfill fills them).
            "deaths": s.deaths or 0,
            "survival_time": s.survival_seconds or 0,
            "headshots": s.headshots or 0,
            "kdr": round((s.kills or 0) / max(1, s.deaths or 0), 2),
            # The team line's placement decides the map's WINNING team (placement 1 = booyah).
            "team_placement": s.team_stats.placement,
        }
        by_match[m.match_id].append((m, line))

        row = players.get(p.user_id)
        if row is None:
            team = s.team_stats.tournament_team.team if s.team_stats.tournament_team else None
            row = players[p.user_id] = {
                "user_id": p.user_id,
                "username": p.username,
                "in_game_name": getattr(p, "in_game_name", "") or p.username,
                "team_name": team.team_name if team else None,
                # Team FLAG source (owner 2026-07-03): the country beside the player's team, so a design
                # can place a team_flag column on a player board too. Team.country is auto-derived; team
                # is select_related above so this adds no query.
                "team_country": (team.country or None) if team else None,
                # esports_pic lives on UserProfile, not User (bug fix 2026-07-02).
                "esports_image": esports_pic_url(p, request),
                "kills": 0, "damage": 0, "assists": 0, "matches": 0, "mvp_count": 0,
                "deaths": 0, "survival_time": 0, "headshots": 0,
            }
        row["kills"] += line["kills"]
        row["damage"] += line["damage"]
        row["assists"] += line["assists"]
        row["deaths"] += line["deaths"]
        row["survival_time"] += line["survival_time"]
        row["headshots"] += line["headshots"]
        row["matches"] += 1
    return by_match, players


def compute_event_mvp(event, request, group_ids=None):
    """The MVP computation (per-map MVP -> event ranking) over a scope (`group_ids=None` = whole event,
    else only those groups' matches). Returns the SAME dict event_mvp responds with. EXTRACTED so the
    endpoint AND the overlay _mvp_payload share ONE implementation. Criteria + winning-team scope come
    from the saved Event.mvp_config; the COMBINE scope (group_ids, owner unit = stages + groups) is
    passed in already-resolved. See the CONTRACT block at the top of this module."""
    cfg = event.mvp_config or {}
    criteria = [c for c in (cfg.get("criteria") or DEFAULT_CRITERIA) if c in CRITERIA_META]
    scope = cfg.get("scope") if cfg.get("scope") in ("overall", "winning_team") else DEFAULT_SCOPE
    # The 3D-room criteria become RANKABLE once this event has debugger-backfilled rows
    # (rich_stats_filled — see debugger_ingest.py). Until then they stay tagged/pending.
    has_rich = TournamentPlayerMatchStats.objects.filter(
        team_stats__match__group__stage__event=event, rich_stats_filled=True
    ).exists()
    def _avail(c):
        return CRITERIA_META[c][1] or has_rich
    rankable = [c for c in criteria if _avail(c)] or DEFAULT_CRITERIA

    by_match, players = _walk_player_stats(event, group_ids, request)

    # ── One MVP per MAP: rank that map's pool by the criteria; winning_team scope restricts the
    #    pool to the booyah (placement-1) team's players of THAT map. ──
    map_mvps = []
    for match_id, lines in by_match.items():
        match = lines[0][0]
        pool = [ln for (_m, ln) in lines]
        if scope == "winning_team":
            winners = [ln for ln in pool if ln.get("team_placement") == 1]
            pool = winners or pool  # a map with no recorded placement-1 falls back to everyone
        if not pool:
            continue
        best = max(pool, key=lambda ln: _crit_key(ln, rankable))
        players[best["user_id"]]["mvp_count"] += 1
        map_mvps.append({
            "match_id": match_id,
            "match_number": match.match_number,
            "match_map": match.match_map,
            "stage_name": match.group.stage.stage_name if (match.group and match.group.stage) else None,
            "group_name": match.group.group_name if match.group else None,
            "mvp_user_id": best["user_id"],
            "mvp_name": players[best["user_id"]]["in_game_name"],
            "kills": best["kills"], "damage": best["damage"], "assists": best["assists"],
        })

    # ── Event ranking: most per-map MVPs first; count ties fall to the criteria on event totals. ──
    for r in players.values():
        # Event-level KDR for the count tie-break (per-map lines carry their own).
        r["kdr"] = round(r["kills"] / max(1, r["deaths"]), 2)
    ranked = sorted(
        players.values(),
        key=lambda r: (r["mvp_count"],) + _crit_key(r, rankable),
        reverse=True,
    )

    return {
        "criteria": criteria,
        "rankable_criteria": rankable,
        "scope": scope,
        "criteria_meta": [
            {"key": k, "label": v[0], "available": _avail(k)} for k, v in CRITERIA_META.items()
        ],
        # Per-map winners (ordered by stage/group/match number for a stable display).
        "map_mvps": sorted(
            map_mvps,
            key=lambda r: (r["stage_name"] or "", r["group_name"] or "", r["match_number"] or 0),
        ),
        "players": ranked[:50],
        "mvp": ranked[0] if ranked else None,
    }


def compute_top_killers(event, request, group_ids=None):
    """TOP-KILLERS (complaint H): rank players by SUM(kills) over the scope (`group_ids=None` = whole
    event, else only those groups' matches). Returns the SAME player row shape as compute_event_mvp
    (identity + kills/damage/assists + esports_image + team + matches + mvp_count), so the MVP (G) and
    Top-killers (H) boards share ONE render path + ONE FE renderer. Ties fall to damage then assists —
    a stable, meaningful order for a kills board. Capped at 50 like the MVP list."""
    _by_match, players = _walk_player_stats(event, group_ids, request)
    for r in players.values():
        r["kdr"] = round(r["kills"] / max(1, r["deaths"]), 2)
    ranked = sorted(
        players.values(),
        key=lambda r: (r["kills"], r["damage"], r["assists"]),
        reverse=True,
    )
    return {"players": ranked[:50], "top": ranked[0] if ranked else None}


# ── PLAYER-ROW -> DESIGN FIELD_CHOICES mapping (THE RENDER CONTRACT) ───────────────────────────────
# A player board renders through a design just like a team board: graphic.py._render_fields + the FE
# DesignBoard bind each placed field by its `field_type` key. So a ranked player (from compute_event_mvp
# / compute_top_killers) is reshaped into a dict keyed by the PLAYER field types added to
# afc_organizers.OrgLeaderboardDesignField.FIELD_CHOICES: pos (player rank) / player_name / esports_image
# (photo, drawn as an IMAGE) / kills / damage / assists / team_name (+ team_country for an optional flag
# + mvp_count for the MVP kind). esports_image is a URL here (FE <img>) and is resolved to a local media
# file by graphic.py at PNG-render time, so the SAME rows render in the browser AND in the download.
def _player_design_row(rank, p):
    """One ranked player -> a design row keyed by FIELD_CHOICES field types (see the block comment)."""
    return {
        "pos": rank,                                                # player rank (reuses `pos`)
        "player_name": p.get("in_game_name") or p.get("username") or "",
        "team_name": p.get("team_name") or "",
        "team_country": p.get("team_country") or "",               # optional team_flag column
        "esports_image": p.get("esports_image"),                   # player PHOTO (renders as image)
        "kills": p.get("kills", 0),
        "damage": p.get("damage", 0),
        "assists": p.get("assists", 0),
        "mvp_count": p.get("mvp_count", 0),                        # map MVPs won (MVP kind headline)
        "matches": p.get("matches", 0),
    }


def build_player_design_rows(players):
    """Map a ranked players list -> design rows (1-based rank). Consumed by graphic.py (the export +
    the overlay payloads' FE renderer). SHARED by G + H so both bind the same field-type keys."""
    return [_player_design_row(i + 1, p) for i, p in enumerate(players)]


@api_view(["GET", "POST"])
def event_mvp(request, event_id):
    """GET/POST events/<event_id>/mvp/ — save (POST) the criteria arrangement + scope, then return:
    the per-map MVP list, the per-player MVP counts, and the event MVP (most per-map MVPs; count ties
    broken by the same criteria on event totals). See compute_event_mvp + the module docstring.

    OPTIONAL COMBINE scope (owner 2026-07-05, complaint G): pass group_ids[] and/or stage_ids[] (query
    or body; whole stages expand to their groups) to compute the MVP over ONLY those groups/stages;
    absent => whole event (unchanged default). The saved config still only stores criteria + winning-
    team scope (the combine selection is a per-request/per-overlay thing, not a saved event setting)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err

    if request.method == "POST":
        raw = request.data.get("criteria")
        criteria = [c for c in raw if c in CRITERIA_META] if isinstance(raw, list) else DEFAULT_CRITERIA
        scope = request.data.get("scope")
        scope = scope if scope in ("overall", "winning_team") else DEFAULT_SCOPE
        event.mvp_config = {"criteria": criteria or DEFAULT_CRITERIA, "scope": scope}
        event.save(update_fields=["mvp_config"])

    group_ids, stage_ids = _read_scope_params(request)
    scope_group_ids = _resolve_player_scope(event, group_ids, stage_ids)
    result = compute_event_mvp(event, request, group_ids=scope_group_ids)
    # Echo the resolved combine scope so the FE can label "combined across N groups".
    result["combine"] = {"group_ids": scope_group_ids, "combined": scope_group_ids is not None}
    return Response(result, status=200)


@api_view(["GET"])
def event_top_killers(request, event_id):
    """GET events/<event_id>/top-killers/ — players ranked by SUM(kills) (complaint H), with the SAME
    optional COMBINE scope as event_mvp (group_ids[]/stage_ids[], whole stages expand to groups; absent
    => whole event). A preview source for the Top-killers design tab (mirrors how MvpTab consumes
    event_mvp). See compute_top_killers. Gate = _broadcast_gate (AFC event admin OR org can_edit_events)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    group_ids, stage_ids = _read_scope_params(request)
    scope_group_ids = _resolve_player_scope(event, group_ids, stage_ids)
    result = compute_top_killers(event, request, group_ids=scope_group_ids)
    result["combine"] = {"group_ids": scope_group_ids, "combined": scope_group_ids is not None}
    return Response(result, status=200)


@api_view(["GET"])
def event_player_board_graphic(request, event_id):
    """GET events/<event_id>/player-board-graphic/?kind=mvp|top_killers&design_id=&size=instagram|
    youtube&group_ids=&stage_ids= — download the MVP (G) or Top-killers (H) board as a PNG rendered
    THROUGH a design, the same way the team leaderboard exports (owner 2026-07-05, complaints G+H #7).

    Reuses the org design library (_resolve_event_design), build_field_layout (design -> field_layout)
    and render_leaderboard_graphic; the player rows carry the FIELD_CHOICES player keys and esports_image
    renders as an IMAGE (graphic.py resolves the URL to a local media file). A design with no fields
    placed falls back to render_leaderboard_graphic's built-in path with the title only. Gate =
    _broadcast_gate. Consumed by the FE MVP/Top-killers design tab's "Download" action."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    from django.http import HttpResponse
    from afc_organizers.views_leaderboard_design import build_field_layout
    from .views_event_graphic import _resolve_event_design
    from afc_leaderboard.graphic import render_leaderboard_graphic

    kind = (request.query_params.get("kind") or "mvp").strip().lower()
    size = "youtube" if (request.query_params.get("size") or "").strip().lower() == "youtube" \
        else "instagram"
    group_ids, stage_ids = _read_scope_params(request)
    scope_group_ids = _resolve_player_scope(event, group_ids, stage_ids)

    if kind == "top_killers":
        players = compute_top_killers(event, request, group_ids=scope_group_ids)["players"]
        title = "Top killers"
    else:
        players = compute_event_mvp(event, request, group_ids=scope_group_ids)["players"]
        title = "MVP"
    rows = build_player_design_rows(players)

    # Design lookup + look, mirroring event_stage_graphic's bg/logo resolution exactly (filesystem
    # PATHS for the local Pillow render).
    design = _resolve_event_design(event, request.query_params.get("design_id"))
    field_layout = build_field_layout(design, size=size) if design else None
    bg, logos = None, []
    text_color, accent_color, transparent_bg = "#FFFFFF", "#34d27b", False
    if design:
        f = design.background_youtube if size == "youtube" else design.background_instagram
        try:
            bg = f.path if f else None
        except Exception:
            bg = None
        for lg in design.logos.all():
            try:
                logos.append({"path": lg.image.path, "x_pct": lg.x_pct,
                              "y_pct": lg.y_pct, "size": lg.size})
            except Exception:
                pass
        text_color, accent_color = design.text_color, design.accent_color
        transparent_bg = design.transparent_background

    png = render_leaderboard_graphic(
        [], size=size, background_path=bg, logos=logos, title=title, subtitle="",
        text_color=text_color, accent_color=accent_color,
        # A field-layout design IS the whole board (its own header); only draw the built-in title when
        # the design places no fields (the legacy fallback path).
        show_title=not bool(field_layout), show_subtitle=False,
        field_layout=field_layout, rows=rows, transparent_background=transparent_bg,
    )
    resp = HttpResponse(png, content_type="image/png")
    fname = f"{event.event_name}-{kind}-{size}.png".replace(" ", "_")
    resp["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


# ── LEADERBOARD TIE-BREAKERS config (owner 2026-07-02) ──────────────────────────
# Kept in this module because the two features are siblings (both are "arranged criteria"; the MVP
# count is itself a tie-breaker criterion). Engine + resolution: round_robin.apply_tie_breakers
# (group > stage > event default > legacy booyahs->kills chain).
#
#   GET  events/<event_id>/tie-breakers/  -> the saved config + the criteria catalog
#   POST events/<event_id>/tie-breakers/  -> {criteria: [...], scope: "all"|"stage"|"group",
#                                             stage_id?, group_id?}  ("all" clears per-scope
#                                             overrides ONLY when body has replace_all=true)
# Consumed by the TieBreakersPanel on the leaderboard edit Scoring Config tab.

TIE_BREAKER_LABELS = {
    "booyahs": "Booyahs",
    "kills": "Kills",
    "placement_points": "Placement points",
    "kill_points": "Kill points",
    "bonus": "Bonus points",
    "fewest_penalties": "Fewest penalties",
    "matches_played": "Matches played",
    "mvp_count": "Map MVPs won",
}


@api_view(["GET", "POST"])
def event_tie_breakers(request, event_id):
    """GET/POST events/<event_id>/tie-breakers/ — read/save the arranged tie-breaker criteria for
    the whole event, one stage, or one group (like maps: apply to all, or per stage/group)."""
    from .round_robin import TIE_BREAKER_KEYS
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err

    if request.method == "POST":
        raw = request.data.get("criteria")
        criteria = [c for c in raw if c in TIE_BREAKER_KEYS] if isinstance(raw, list) else []
        scope = (request.data.get("scope") or "all").strip()
        cfg = dict(event.tie_breakers or {})
        cfg.setdefault("stages", {})
        cfg.setdefault("groups", {})
        if scope == "stage" and request.data.get("stage_id"):
            cfg["stages"][str(request.data["stage_id"])] = criteria
        elif scope == "group" and request.data.get("group_id"):
            cfg["groups"][str(request.data["group_id"])] = criteria
        else:
            cfg["default"] = criteria
            # "Apply to all" wipes per-stage/group overrides when explicitly asked, so one save can
            # reset the whole event to a single chain (mirrors the maps "apply to all" behaviour).
            if request.data.get("replace_all"):
                cfg["stages"], cfg["groups"] = {}, {}
        event.tie_breakers = cfg
        event.save(update_fields=["tie_breakers"])

    return Response({
        "tie_breakers": event.tie_breakers or {},
        "catalog": [{"key": k, "label": v} for k, v in TIE_BREAKER_LABELS.items()],
    }, status=200)
