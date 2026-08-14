# ── EVENT OVERLAYS - saved, named broadcast overlays (owner 2026-07-02, studio v2) ──
# An overlay is a persistent per-event entity: created from a design (kind="leaderboard") or as a
# scene (kind="timer"), named/renamed, duplicated, deleted. Its public link NEVER changes:
# /overlay/view/<Event.overlay_token>/<overlay_id> polls overlay_config below, so edits from the
# studio (design, stage/group, animations, timer trigger) update what the SAME link renders live.
#
# ENDPOINTS (Bearer via _broadcast_gate - AFC event admin OR org that can_edit_events):
#   GET  events/<event_id>/overlays/                    -> list
#   POST events/<event_id>/overlays/create/             -> {name, kind, config} -> row
#   POST events/<event_id>/overlays/<overlay_id>/update/    -> {name?, config?, active?} -> row
#   POST events/<event_id>/overlays/<overlay_id>/duplicate/ -> copy (name + " copy")
#   POST events/<event_id>/overlays/<overlay_id>/delete/    -> gone
# PUBLIC (the overlay token is the read capability, mirrors overlay_feed):
#   GET  events/overlay/config/?token=&overlay=<id>     -> {kind, name, config, active, server_time}
#
# CONSUMED BY: FE lib/overlay.ts overlaysApi/overlayConfigApi -> studio app/(a)/a/overlays/[eventId]
# (cards) + the stable renderer app/overlay/view/[token]/[overlayId]/page.tsx.

from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Event, EventOverlay
# One answer to "is this stage Clash Squad?", across every generation of stage_format values.
# The bracket scene below picks the stage to render with it. See stage_formats.py.
from .stage_formats import is_clash_squad
from .views import _broadcast_gate, _org_hidden

VALID_KINDS = {k for k, _ in EventOverlay.KINDS}


def _serialize(row):
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "config": row.config or {},
        "active": row.active,
        "updated_at": row.updated_at,
    }


@api_view(["GET"])
def list_overlays(request, event_id):
    """GET events/<event_id>/overlays/ - every saved overlay, in creation order (the studio's cards)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    return Response(
        {"overlays": [_serialize(r) for r in EventOverlay.objects.filter(event=event)]},
        status=200,
    )


@api_view(["POST"])
def create_overlay(request, event_id):
    """POST events/<event_id>/overlays/create/ {name, kind, config} - new overlay (e.g. picked a
    design -> a leaderboard overlay preconfigured with it; or a fresh timer scene)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    kind = (request.data.get("kind") or "leaderboard").strip().lower()
    if kind not in VALID_KINDS:
        return Response({"message": f"Unknown overlay kind '{kind}'."}, status=400)
    name = (request.data.get("name") or "").strip()[:80] or kind.title()
    config = request.data.get("config") if isinstance(request.data.get("config"), dict) else {}
    row = EventOverlay.objects.create(
        event=event, name=name, kind=kind, config=config,
        # Timers start hidden until triggered; leaderboards render immediately; BOOYAH banners start
        # ACTIVE (owner 2026-07-02: "showing automatically without having to click trigger") - in
        # live mode they render the latest booyah as soon as the source loads. MVP + TOP-KILLERS boards
        # (owner 2026-07-05) render immediately too, like the leaderboard.
        active=(kind in ("leaderboard", "booyah", "mvp", "top_killers")),
    )
    return Response(_serialize(row), status=201)


def _get_row(event, overlay_id):
    return EventOverlay.objects.filter(event=event, id=overlay_id).first()


@api_view(["POST"])
def update_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/update/ {name?, config?, active?} - partial edit.
    config REPLACES wholesale when given (the FE always sends the full config object); rename via
    name; scenes trigger/hide via active. The public link keeps rendering the new state."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Overlay not found."}, status=404)
    if "name" in request.data:
        name = (request.data.get("name") or "").strip()[:80]
        if name:
            row.name = name
    if isinstance(request.data.get("config"), dict):
        row.config = request.data["config"]
    if "active" in request.data:
        row.active = bool(request.data.get("active"))
    row.save()
    return Response(_serialize(row), status=200)


@api_view(["POST"])
def duplicate_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/duplicate/ - copy config+kind under "<name> copy"
    (a fresh id = a fresh stable link, so the copy can diverge without touching the original)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Overlay not found."}, status=404)
    copy = EventOverlay.objects.create(
        event=event, name=f"{row.name} copy"[:80], kind=row.kind,
        config=dict(row.config or {}),
        # Always-render kinds (leaderboard / mvp / top_killers) keep the original's active flag; scenes
        # (timer / booyah / h2h) start hidden so a duplicate never auto-fires.
        active=row.active if row.kind in ("leaderboard", "mvp", "top_killers") else False,
    )
    return Response(_serialize(copy), status=201)


@api_view(["POST"])
def delete_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/delete/ - remove it (its link then 404s)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Overlay not found."}, status=404)
    row.delete()
    return Response({"message": "Overlay deleted."}, status=200)


@api_view(["GET"])
def overlay_config(request):
    """GET events/overlay/config/?token=&overlay=<id> - PUBLIC config the stable renderer polls.
    Token = Event.overlay_token (same capability as overlay_feed); a hidden org's event 404s.
    server_time lets the timer correct client-clock drift. A deleted overlay 404s (OBS shows blank)."""
    token = (request.query_params.get("token") or "").strip()
    try:
        overlay_id = int(request.query_params.get("overlay") or 0)
    except (TypeError, ValueError):
        overlay_id = 0
    if not token or not overlay_id:
        return Response({"message": "token and overlay are required."}, status=400)
    event = Event.objects.select_related("organization").filter(overlay_token=token).first()
    if not event or _org_hidden(event):
        return Response({"message": "Not found."}, status=404)
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Not found."}, status=404)
    payload = {
        "kind": row.kind,
        "name": row.name,
        "config": row.config or {},
        "active": row.active,
        "event_id": event.event_id,
        "server_time": timezone.now(),
    }
    # LEADERBOARD overlays bundle their RESOLVED standings with the config poll (owner 2026-07-05,
    # complaint C), mirroring how h2h/booyah bundle their data below. This is where the per-overlay
    # COMBINE spec (config {scope:"combine", group_ids, stage_ids}) is honoured: a combine config
    # returns the merged cumulative rows spanning every chosen group/stage, a single-scope config just
    # that group/stage, and a follow config the event's live broadcast selection - the SAME numbers the
    # stable link's inner /overlay/leaderboard iframe pulls from overlay_feed, so the two never drift.
    # The STABLE link is unchanged by any of this: editing the card re-saves config and this poll
    # re-resolves, so the one link always renders the overlay's current combination.
    if row.kind == "leaderboard":
        from .views import _overlay_config_leaderboard_standings
        payload["standings"] = _overlay_config_leaderboard_standings(event, row.config or {}, request)
    # H2H overlays ship their RESOLVED competitor stats + design look with the config poll, so the
    # public page needs exactly one request per poll (mirrors overlay_feed bundling design+standings).
    if row.kind == "h2h":
        payload["h2h"] = _h2h_payload(event, row.config or {}, request)
    # MVP (G) + TOP-KILLERS (H) overlays (owner 2026-07-05) bundle their RESOLVED ranked PLAYER rows +
    # design look with the config poll, mirroring _h2h_payload. Each honours the overlay config's COMBINE
    # scope (config {scope, group_ids, stage_ids}; whole stages expand to their groups, absent => whole
    # event), so the SAME stable link renders whatever combination the studio card saved. Both return the
    # identical row shape (keyed by the design player FIELD_CHOICES) so the FE renders G + H with ONE
    # renderer through the bound design. See views_mvp.py (the CONTRACT block).
    if row.kind == "mvp":
        payload["mvp"] = _mvp_payload(event, row.config or {}, request)
    if row.kind == "top_killers":
        payload["top_killers"] = _top_killers_payload(event, row.config or {}, request)
    # BOOYAH LIVE mode (owner 2026-07-02): config.live=true makes the banner FOLLOW THE LEADERBOARD -
    # each poll resolves the event's LATEST booyah (most recent match with a placement-1 team) and
    # overrides team/logo/map in the RESPONSE (nothing persisted), so as new results land the banner
    # updates itself. shown_at = that match's id-stamped marker so a NEW winner re-keys the pop-in.
    if row.kind == "booyah":
        cfg = dict(row.config or {})
        if cfg.get("live"):
            cfg = _booyah_live_config(event, cfg, request)
            payload["config"] = cfg
        # Design template + the booyah team's roster ride along with every poll.
        payload["booyah"] = _booyah_payload(event, cfg, request)
    return Response(payload, status=200)


def _booyah_live_config(event, config, request):
    """Resolve the event's latest booyah for a LIVE booyah banner (see overlay_config above)."""
    from .models import TournamentTeamMatchStats
    win = (TournamentTeamMatchStats.objects
           .filter(match__group__stage__event=event, placement=1)
           .select_related("match", "tournament_team__team")
           .order_by("-match__match_date", "-match__match_id")
           .first())
    if win:
        team = win.tournament_team.team if win.tournament_team else None
        config.update({
            "team_name": team.team_name if team else "",
            "team_logo": (request.build_absolute_uri(team.team_logo.url)
                          if (team and team.team_logo) else None),
            "match_map": win.match.match_map,
            # Stable per-winner marker: the animation re-keys ONLY when a newer booyah lands.
            "shown_at": f"live-{win.match.match_id}",
        })
    return config


def _h2h_payload(event, config, request):
    """Resolve an H2H overlay's competitor slots to THIS-EVENT stats (owner 2026-07-02).

    config: {mode: "team"|"player"|"bracket", competitor_ids: [2-3 ids], stage_id?, design_id?}.
    Teams compare their aggregated TournamentTeamMatchStats (kills/booyahs/points/matches); players
    their TournamentPlayerMatchStats (kills/damage/assists + the 3D-room rich stats when the debugger
    backfill has filled them). The picked DESIGN drives the look (bg + colors) - "overlays are
    created based off available designs"; the full versus design-editor type is the next phase.

    mode == "bracket" (Clash Squad, P1#6 owner 2026-07-13): a pure CS event has no BR stats to put in
    a versus card, so the overlay renders the STAGE BRACKET instead. config.stage_id picks which CS
    stage (falls back to the event's first cs- stage); the resolved tree is the SAME shape the public
    bracket GET returns (head_to_head_views._bracket_payload), so the FE draws it read-only over the
    design look. Returns {mode:"bracket", bracket:{...}|None, competitors:[], design:{...}}.

    Returns {mode, competitors: [...], design: {...}} for the public overlay_config feed."""
    from django.db.models import Sum, Count, Case, When, Value, IntegerField
    from .models import TournamentTeamMatchStats, TournamentPlayerMatchStats

    mode = (config.get("mode") or "team").strip()
    ids = [int(i) for i in (config.get("competitor_ids") or []) if str(i).strip()][:3]
    competitors = []

    # ── Clash Squad bracket mode: render the stage bracket, not a stat comparison. ──
    if mode == "bracket":
        from .models import StageGroups, Stages
        from .head_to_head_views import _bracket_payload
        from . import head_to_head
        stage = None
        sid = config.get("stage_id")
        if sid not in (None, ""):
            stage = Stages.objects.filter(event=event, stage_id=sid).first()
        if stage is None:
            # No explicit / stale stage_id: fall back to the event's first Clash Squad stage.
            # Asked through is_clash_squad so BOTH spellings match: a stage created since
            # 2026-08-13 carries plain "cs" (owner item 21), everything older carries
            # "cs - <mode>". The old query tested `startswith("cs -")` and skipped the new one.
            stage = next(
                (s for s in Stages.objects.filter(event=event)
                                          .order_by("stage_order", "stage_id")
                 if is_clash_squad(s.stage_format)),
                None,
            )
        return {
            "mode": "bracket",
            "competitors": [],
            # A Clash Squad stage can be SPLIT into groups, each running its own bracket (owner
            # item 21, 2026-08-13), and a bracket is now the matches of a group. An overlay shows
            # ONE bracket, so it takes the stage's single one: resolve_bracket_group_id says which
            # that is, keeps a pre-item-21 stage (matches with no group) on its old behaviour, and
            # returns None for a stage split into several, where "the bracket" is genuinely
            # ambiguous. Without this the payload asks for the matches that belong to NO group and
            # every Clash Squad bracket overlay renders empty.
            "bracket": _bracket_payload(
                stage,
                StageGroups.objects.filter(
                    group_id=head_to_head.resolve_bracket_group_id(stage)).first(),
            ) if stage is not None else None,
            "design": _design_look(config.get("design_id"), request),
        }

    if mode == "player":
        from afc_auth.models import User
        for uid in ids:
            u = User.objects.filter(user_id=uid).first()
            if not u:
                continue
            agg = (TournamentPlayerMatchStats.objects
                   .filter(team_stats__match__group__stage__event=event, player=u)
                   .aggregate(kills=Sum("kills"), damage=Sum("damage"), assists=Sum("assists"),
                              deaths=Sum("deaths"), headshots=Sum("headshots"),
                              survival=Sum("survival_seconds"), matches=Count("player_stats_id")))
            from afc_auth.models import esports_pic_url
            competitors.append({
                "name": getattr(u, "in_game_name", "") or u.username,
                # esports_pic lives on UserProfile, not User (bug fix 2026-07-02).
                "image": esports_pic_url(u, request),
                "stats": {
                    "kills": agg["kills"] or 0, "damage": agg["damage"] or 0,
                    "assists": agg["assists"] or 0, "deaths": agg["deaths"] or 0,
                    "headshots": agg["headshots"] or 0, "survival_seconds": agg["survival"] or 0,
                    "matches": agg["matches"] or 0,
                },
            })
    else:
        from afc_team.models import Team
        for tid in ids:
            team = Team.objects.filter(team_id=tid).first()
            if not team:
                continue
            agg = (TournamentTeamMatchStats.objects
                   .filter(match__group__stage__event=event, tournament_team__team=team)
                   .aggregate(kills=Sum("kills"), points=Sum("total_points"),
                              matches=Count("team_stats_id"),
                              booyahs=Sum(Case(When(placement=1, then=Value(1)),
                                               default=Value(0), output_field=IntegerField()))))
            competitors.append({
                "name": team.team_name,
                "image": (request.build_absolute_uri(team.team_logo.url)
                          if getattr(team, "team_logo", None) else None),
                "stats": {
                    "kills": agg["kills"] or 0, "points": agg["points"] or 0,
                    "booyahs": agg["booyahs"] or 0, "matches": agg["matches"] or 0,
                },
            })

    return {"mode": mode, "competitors": competitors,
            "design": _design_look(config.get("design_id"), request)}


# ── MVP (G) + TOP-KILLERS (H) player-board payloads (owner 2026-07-05) ─────────────────────────────
# Both mirror _h2h_payload: resolve the overlay's ranked PLAYER rows (honouring the config COMBINE
# scope) + attach the bound design's look, so the FE renders each through its design with ONE renderer.
# The heavy lifting (aggregation, ranking, the design-row shape) lives in views_mvp.py so the endpoints
# and these payloads share ONE implementation; see the CONTRACT block at the top of views_mvp.py.

def _combine_ids_from_config(config, plural_key, singular_key):
    """Read a combine id list off an overlay config: a real JSON list under plural_key (or a csv string)
    plus an optional singular id folded in. Returns raw str ids; validation (this-event / stage-expand)
    happens in views_mvp._resolve_player_scope. Mirrors the endpoint's _read_scope_params ergonomics."""
    vals = []
    raw = (config or {}).get(plural_key)
    if isinstance(raw, (list, tuple)):
        vals.extend(raw)
    elif raw not in (None, ""):
        vals.extend(str(raw).split(","))
    one = (config or {}).get(singular_key)
    if one not in (None, ""):
        vals.append(one)
    return [str(x).strip() for x in vals if str(x).strip()]


def _mvp_payload(event, config, request):
    """Resolve an MVP overlay (owner 2026-07-05, complaint G) to its RANKED PLAYER ROWS + design look,
    mirroring _h2h_payload. config: {design_id?, scope?, group_ids?: [...], stage_ids?: [...], group_id?,
    stage_id?}. The combine scope (whole STAGES + individual GROUPS) resolves via the SAME validator the
    leaderboard combine uses, so an MVP board agrees with the site. Each row is keyed by the design
    player FIELD_CHOICES (pos/player_name/esports_image/kills/damage/assists/team_name/mvp_count) so G
    renders through any bound design. Returns {kind:"mvp", players:[...], top:<row|None>, combine:{...},
    design:{...}}."""
    from .views_mvp import (compute_event_mvp, build_player_design_rows, _resolve_player_scope)
    group_ids = _combine_ids_from_config(config, "group_ids", "group_id")
    stage_ids = _combine_ids_from_config(config, "stage_ids", "stage_id")
    scope_group_ids = _resolve_player_scope(event, group_ids, stage_ids)
    computed = compute_event_mvp(event, request, group_ids=scope_group_ids)
    rows = build_player_design_rows(computed["players"])
    return {
        "kind": "mvp",
        "players": rows,
        "top": rows[0] if rows else None,
        "combine": {"group_ids": scope_group_ids, "combined": scope_group_ids is not None},
        "design": _design_look(config.get("design_id"), request),
    }


def _top_killers_payload(event, config, request):
    """Resolve a TOP-KILLERS overlay (owner 2026-07-05, complaint H) to ranked player rows + design look
    - identical shape/keys to _mvp_payload (so G and H share ONE FE renderer), but ranked by SUM(kills)
    over the same combine scope. Returns {kind:"top_killers", players, top, combine, design}."""
    from .views_mvp import (compute_top_killers, build_player_design_rows, _resolve_player_scope)
    group_ids = _combine_ids_from_config(config, "group_ids", "group_id")
    stage_ids = _combine_ids_from_config(config, "stage_ids", "stage_id")
    scope_group_ids = _resolve_player_scope(event, group_ids, stage_ids)
    computed = compute_top_killers(event, request, group_ids=scope_group_ids)
    rows = build_player_design_rows(computed["players"])
    return {
        "kind": "top_killers",
        "players": rows,
        "top": rows[0] if rows else None,
        "combine": {"group_ids": scope_group_ids, "combined": scope_group_ids is not None},
        "design": _design_look(config.get("design_id"), request),
    }


def _design_look(design_id, request):
    """A design's broadcast LOOK (bg + colors + versus stat picks) for the scene renderers
    (H2H + the design-templated booyah banner). None when no design picked/found."""
    if not design_id:
        return None
    from afc_organizers.models import OrgLeaderboardDesign
    d = OrgLeaderboardDesign.objects.filter(id=design_id).first()
    if not d:
        return None
    return {
        "background": (request.build_absolute_uri(d.background_youtube.url)
                       if d.background_youtube else
                       (request.build_absolute_uri(d.background_instagram.url)
                        if d.background_instagram else None)),
        "text_color": d.text_color, "accent_color": d.accent_color,
        "transparent": d.transparent_background,
        # Versus designs pick WHICH stat rows the H2H shows (order = display order).
        "stat_keys": (getattr(d, "versus_config", {}) or {}).get("stat_keys") or [],
    }


def _booyah_payload(event, config, request):
    """The booyah banner's extras. TWO shapes ride along, and which one the FE uses depends entirely
    on the bound design's TYPE:

      • `board` (owner 2026-08-06) - present ONLY when the picked design is design_type="booyah".
        It carries the FULL serialized design (the same _serialize_design payload the leaderboard
        overlay feed ships) plus the resolved booyah ROWS, so the FE renders the moment through
        DesignBoard - the very same renderer the live leaderboard uses. That is the owner's ask:
        "the overlays should be based off what's on the design and what was set to come up there".
      • `design` (the 4-key look) + `roster` - the LEGACY hard-coded banner's inputs, kept because
        every booyah overlay configured before this change is bound to a leaderboard design (or to
        none), and must keep rendering exactly as it did. See the FE BooyahView: `board` wins when
        present, otherwise the legacy banner draws. Nothing was migrated, nothing changed shape.

    Roster (legacy path) resolves from config.team_name against the event's registered teams (works
    for manual, auto-fired and live-resolved configs alike)."""
    from .models import TournamentTeam, TournamentTeamMember
    roster = []
    team_name = (config.get("team_name") or "").strip()
    if team_name:
        tt = (TournamentTeam.objects
              .filter(event=event, team__team_name=team_name)
              .select_related("team").first())
        if tt:
            for m in TournamentTeamMember.objects.filter(
                    tournament_team=tt).select_related("user")[:6]:
                u = m.user
                if not u:
                    continue
                from afc_auth.models import esports_pic_url
                roster.append({
                    "name": getattr(u, "in_game_name", "") or u.username,
                    # esports_pic lives on UserProfile, not User (bug fix 2026-07-02).
                    "image": esports_pic_url(u, request),
                })
    return {
        "design": _design_look(config.get("design_id"), request),
        "roster": roster,
        "board": _booyah_board(event, config, request),
    }


# ── DESIGN-DRIVEN BOOYAH BOARD (owner 2026-08-06) ─────────────────────────────────────────────────
# "Let it work like the way leaderboard works, that whatever will populate on the design will come
# from the leaderboard, so the overlays should be based off what's on the design."
#
# The leaderboard overlay is design + a flat list of ROWS keyed by design field_type; the FE
# DesignBoard draws row[field.field_type] at each placed field's x_pct, tiling rows down the design's
# column groups. Everything below exists to express a BOOYAH MOMENT in that same row shape, so the
# booyah overlay can go through the SAME renderer instead of its own hard-coded markup:
#
#   slot 1        -> the WINNING TEAM, its row lifted straight out of the live leaderboard
#                    (_overlay_standings_rows - the identical helper the leaderboard overlay uses,
#                    so the points on the banner ARE the points on the site), plus `match_map`.
#   slots 2..N+1  -> that team's PLAYERS in the won match (name, photo, kills, damage, assists,
#                    and the rich 3D-room stats when a debugger log filled them).
#
# `slot` is what DesignBoard positions by; `pos` stays a DISPLAYABLE rank (the team's rank on the
# leaderboard, each player's rank within the squad), so placing the POS column shows something
# meaningful on either block. A design lays this out with two column groups: one row starting at
# rank 1 (the team), then N rows starting at rank 2 (the roster) - which is exactly what the editor's
# "add column group" button produces by default, since it starts the next group at
# start_rank + row_count.
#
# CONNECTS TO: overlay_config (the 1s public poll) -> FE app/overlay/view/[token]/[overlayId]
# BooyahView -> DesignBoard. Data sources: TournamentTeamMatchStats (which team won which map),
# _overlay_standings_rows (the leaderboard numbers), TournamentPlayerMatchStats (the squad's map
# stats), TournamentTeamMember (the roster fallback when a map has no per-player rows yet).

# Slot numbers the rows above occupy. Named because BOTH the resolver and the tests assert on them,
# and because a reader of a design's column groups needs to know what "start_rank 2" means here.
BOOYAH_TEAM_SLOT = 1
BOOYAH_FIRST_PLAYER_SLOT = 2
# How many players of the winning squad the board can carry (a Free Fire squad is 4, +2 headroom for
# substitutes on the event roster). Mirrors the legacy banner's [:6] roster cap.
BOOYAH_MAX_PLAYERS = 6
# How deep into the group's standings to look for the winning team's row. A booyah winner is usually
# near the top, but not always on the first map of a stage, so this is generous rather than tight -
# it only bounds one already-computed list.
BOOYAH_STANDINGS_CAP = 200


def _booyah_design(design_id):
    """The bound design IF it was authored for the booyah moment (design_type="booyah"), else None.

    This ONE check is the whole migration story: a booyah overlay only leaves the legacy banner when
    somebody deliberately binds a booyah-TYPE design to it. Every overlay configured before this
    change points at a leaderboard design (or at nothing), so it keeps the banner it has."""
    if not design_id:
        return None
    from afc_organizers.models import OrgLeaderboardDesign
    d = (OrgLeaderboardDesign.objects
         .filter(id=design_id, design_type="booyah")
         .prefetch_related("logos", "fields", "texts", "pages").first())
    return d


def _booyah_winning_stat(event, config):
    """The TournamentTeamMatchStats row (placement 1 = the booyah) the banner is currently showing.

    Three ways a booyah overlay names its winner, all handled here:
      • config.live -> the event's LATEST booyah (the same row _booyah_live_config resolves, so the
        board and the banner never disagree about who won);
      • a manual/auto trigger -> config.team_name (+ config.match_map when the operator typed one,
        which disambiguates a team that has won more than one map);
      • nothing set yet -> None, and the caller renders no board.
    Returns None when the named team has no booyah on record (e.g. triggered before the result was
    uploaded); the caller then falls back to the roster-only row set."""
    from .models import TournamentTeamMatchStats
    qs = (TournamentTeamMatchStats.objects
          .filter(match__group__stage__event=event, placement=1)
          .select_related("match", "match__group", "tournament_team__team")
          .order_by("-match__match_date", "-match__match_id"))
    team_name = (config.get("team_name") or "").strip()
    if not team_name:
        # LIVE mode (or an untouched card): the most recent booyah in the event.
        return qs.first() if config.get("live") else None
    scoped = qs.filter(tournament_team__team__team_name=team_name)
    match_map = (config.get("match_map") or "").strip()
    if match_map:
        on_map = scoped.filter(match__match_map__iexact=match_map).first()
        if on_map is not None:
            return on_map
    return scoped.first()


def _booyah_suppressed(event, kind, team_id=None, user_id=None):
    """True when this event's media audit suppressed a team logo / a player's esport image
    (EventMediaOptOut, set from the studio's media-audit card). The leaderboard row builder already
    honours the team-logo opt-out; the booyah board honours BOTH so a player who asked for their
    photo to be off a broadcast does not reappear on the winner banner."""
    from .models import EventMediaOptOut
    qs = EventMediaOptOut.objects.filter(event=event, kind=kind)
    return qs.filter(team_id=team_id).exists() if team_id else qs.filter(user_id=user_id).exists()


def _booyah_team_row(event, win, request):
    """Slot 1: the winning team, taken FROM THE LEADERBOARD.

    Runs the winning match's group through _overlay_standings_rows - the exact helper the live
    leaderboard overlay uses - and picks out the winner's row, so every number on the booyah banner
    (total points, kills, kill/placement points, booyahs, matches) is the number the site shows. Only
    when that lookup cannot resolve (no group on the match, or the team is not in that group's
    standings) does it fall back to the match's own stats, so a board still draws."""
    from .views import _overlay_standings_rows
    team = win.tournament_team.team if win.tournament_team else None
    team_name = team.team_name if team else ""
    group = win.match.group

    row = None
    if group is not None and team_name:
        for r in _overlay_standings_rows(event, None, group, BOOYAH_STANDINGS_CAP, request):
            if r.get("team_name") == team_name:
                row = dict(r)
                break
    if row is None:
        # Fallback: the won MATCH's own stats. Same keys, so a design placed against the leaderboard
        # row renders identically - just scoped to the single map instead of the group total.
        logo = None
        if team and team.team_logo and not _booyah_suppressed(
                event, "team_logo", team_id=team.team_id):
            logo = request.build_absolute_uri(team.team_logo.url)
        row = {
            "pos": win.placement,
            "team_name": team_name or "-",
            "team_logo": logo,
            "team_country": (team.country or "") if team else "",
            "esports_image": None,
            "booyah": 1,
            "placement_points": win.placement_points,
            "kill_points": win.kill_points,
            "total_points": win.total_points,
            "kills": win.kills,
            "matches": 1,
            "base_total": win.total_points,
            "bonus": win.bonus_points,
            "penalty": win.penalty_points,
        }

    row.update({
        "slot": BOOYAH_TEAM_SLOT,
        # Stable identity across polls so DesignBoard keeps the same DOM element (and its count-up
        # animation) while the same team is on screen.
        "row_key": f"team-{win.tournament_team_id}",
        # The one value a booyah has that a standings row does not: which map was won.
        "match_map": win.match.match_map or "",
    })
    return row


def _booyah_player_rows(event, win, team_row, request):
    """Slots 2+: the winning squad, with their stats FROM THE MAP THEY JUST WON.

    Ordered by kills (then the stats-row id) so the standout player leads the block and the order is
    deterministic between polls. When the match has no per-player rows yet (a result entered without
    them, or a manual trigger fired before the upload), falls back to the event ROSTER
    (TournamentTeamMember) with zeroed stats - the same source the legacy banner's roster cards use -
    so the block is never empty just because the numbers have not landed."""
    from afc_auth.models import esports_pic_url
    from .models import TournamentPlayerMatchStats, TournamentTeamMember

    def _shell(user, index, stats=None):
        """One player row keyed by the design's PLAYER field types, inheriting the team-level values
        so a design can repeat the team name/logo/flag/map beside each player if it wants to."""
        photo = None
        if not _booyah_suppressed(event, "esports_image", user_id=user.user_id):
            photo = esports_pic_url(user, request)
        return {
            "slot": BOOYAH_FIRST_PLAYER_SLOT + index,
            "row_key": f"player-{user.user_id}",
            # Rank WITHIN the winning squad, so a placed POS column reads 1, 2, 3, 4 down the block.
            "pos": index + 1,
            "player_name": getattr(user, "in_game_name", "") or user.username,
            "esports_image": photo,
            # Inherited team context (a booyah design usually shows the team once, but nothing stops
            # it repeating the crest per player).
            "team_name": team_row.get("team_name", ""),
            "team_logo": team_row.get("team_logo"),
            "team_country": team_row.get("team_country", ""),
            "match_map": team_row.get("match_map", ""),
            "matches": 1,
            "kills": getattr(stats, "kills", 0),
            "damage": getattr(stats, "damage", 0),
            "assists": getattr(stats, "assists", 0),
            # Rich 3D-room stats: real values only when a debugger log was ingested for this match
            # (rich_stats_filled), 0 otherwise - the same contract the leaderboard columns carry.
            "deaths": getattr(stats, "deaths", 0),
            "knockdowns": getattr(stats, "knockdowns", 0),
            "headshots": getattr(stats, "headshots", 0),
            "revives_received": getattr(stats, "revives_received", 0),
            "survival_time": getattr(stats, "survival_seconds", 0),
        }

    played = list(
        TournamentPlayerMatchStats.objects
        .filter(team_stats=win).select_related("player")
        .order_by("-kills", "player_stats_id")[:BOOYAH_MAX_PLAYERS]
    )
    if played:
        return [_shell(ps.player, i, ps) for i, ps in enumerate(played) if ps.player]

    roster = TournamentTeamMember.objects.filter(
        tournament_team=win.tournament_team).select_related("user")[:BOOYAH_MAX_PLAYERS]
    return [_shell(m.user, i) for i, m in enumerate(roster) if m.user]


def _booyah_board(event, config, request):
    """The design-driven booyah board, or None when this overlay should keep the legacy banner.

    Returns {design: <full _serialize_design>, rows: [...], size: "youtube"} - everything the FE
    DesignBoard needs, in one poll. None whenever there is no booyah-type design bound, or no booyah
    resolved yet, which is precisely when the FE must fall back to the old banner."""
    design = _booyah_design(config.get("design_id"))
    if design is None:
        return None
    win = _booyah_winning_stat(event, config)
    if win is None:
        return None
    from afc_organizers.views_leaderboard_design import _serialize_design
    team_row = _booyah_team_row(event, win, request)
    rows = [team_row] + _booyah_player_rows(event, win, team_row, request)
    return {
        "design": _serialize_design(design, request),
        "rows": rows,
        # OBS browser sources are 1920x1080, matching the leaderboard overlay's stable link, which
        # also hardcodes size=youtube (see the FE leaderboardUrl builder).
        "size": "youtube",
    }


# ── AFC CAPTURE remote update + config (owner 2026-07-02) ───────────────────────
# "Update the capture software remotely without re-uploading the full exe": the installed exe is a
# THIN LAUNCHER (afc-capture/launcher.py scaffold) that, on start, GETs capture/version/ and
# downloads the small payload zip (the Python logic) only when `version` is newer than its local
# copy, verifies sha256, then runs it with the bundled runtime. Ops updates = drop a new payload
# zip + bump capture_release.json; the exe re-ships only when the runtime changes.
# capture/config/ centralises tweakables (endpoints, poll cadences) so most changes need no code
# at all. Both PUBLIC (no secrets here; the capture WRITE key stays per-event).
# The release descriptor lives in MEDIA_ROOT/capture/capture_release.json:
#   {"version": "1.1.0", "payload_url": "<abs or /media/... url>", "sha256": "<hex>"}

import json as _json
import os as _os

from django.conf import settings as _settings


@api_view(["GET"])
def capture_version(request):
    """DEPRECATED (owner 2026-07-05): superseded by views_capture_update.capture_version, which serves the
    DB-model CaptureRelease and drives the FULL installer auto-update. urls.py no longer routes
    capture/version/ here; this legacy file-based descriptor (the "thin launcher + payload zip" experiment)
    is kept only for reference. Left intact to avoid churn; do not wire it back without a reason.

    GET events/capture/version/ - the latest capture-payload release descriptor (or 404 when no
    release has been published yet). The launcher compares `version` to its local payload."""
    path = _os.path.join(_settings.MEDIA_ROOT, "capture", "capture_release.json")
    if not _os.path.exists(path):
        return Response({"message": "No capture release published."}, status=404)
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
    except Exception:
        return Response({"message": "Release descriptor unreadable."}, status=500)
    # Relative payload paths resolve against this host so one descriptor works on any environment.
    if data.get("payload_url", "").startswith("/"):
        data["payload_url"] = request.build_absolute_uri(data["payload_url"])
    return Response(data, status=200)


@api_view(["GET"])
def capture_config(request):
    """GET events/capture/config/ - centralised runtime settings for the capture app, so ops can
    tune cadences/endpoints without any code or exe change."""
    return Response({
        "live_push_interval_seconds": 2,
        "upload_endpoint": "/events/upload-team-match-result/",
        "live_push_endpoint": "/events/live/push/",
        "resolve_endpoint": "/events/capture/resolve/",
    }, status=200)
