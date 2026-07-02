# ── EVENT OVERLAYS — saved, named broadcast overlays (owner 2026-07-02, studio v2) ──
# An overlay is a persistent per-event entity: created from a design (kind="leaderboard") or as a
# scene (kind="timer"), named/renamed, duplicated, deleted. Its public link NEVER changes:
# /overlay/view/<Event.overlay_token>/<overlay_id> polls overlay_config below, so edits from the
# studio (design, stage/group, animations, timer trigger) update what the SAME link renders live.
#
# ENDPOINTS (Bearer via _broadcast_gate — AFC event admin OR org that can_edit_events):
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
    """GET events/<event_id>/overlays/ — every saved overlay, in creation order (the studio's cards)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    return Response(
        {"overlays": [_serialize(r) for r in EventOverlay.objects.filter(event=event)]},
        status=200,
    )


@api_view(["POST"])
def create_overlay(request, event_id):
    """POST events/<event_id>/overlays/create/ {name, kind, config} — new overlay (e.g. picked a
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
        # live mode they render the latest booyah as soon as the source loads.
        active=(kind in ("leaderboard", "booyah")),
    )
    return Response(_serialize(row), status=201)


def _get_row(event, overlay_id):
    return EventOverlay.objects.filter(event=event, id=overlay_id).first()


@api_view(["POST"])
def update_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/update/ {name?, config?, active?} — partial edit.
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
    """POST events/<event_id>/overlays/<overlay_id>/duplicate/ — copy config+kind under "<name> copy"
    (a fresh id = a fresh stable link, so the copy can diverge without touching the original)."""
    event, err = _broadcast_gate(request, event_id)
    if err:
        return err
    row = _get_row(event, overlay_id)
    if not row:
        return Response({"message": "Overlay not found."}, status=404)
    copy = EventOverlay.objects.create(
        event=event, name=f"{row.name} copy"[:80], kind=row.kind,
        config=dict(row.config or {}), active=row.active if row.kind == "leaderboard" else False,
    )
    return Response(_serialize(copy), status=201)


@api_view(["POST"])
def delete_overlay(request, event_id, overlay_id):
    """POST events/<event_id>/overlays/<overlay_id>/delete/ — remove it (its link then 404s)."""
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
    """GET events/overlay/config/?token=&overlay=<id> — PUBLIC config the stable renderer polls.
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
    # H2H overlays ship their RESOLVED competitor stats + design look with the config poll, so the
    # public page needs exactly one request per poll (mirrors overlay_feed bundling design+standings).
    if row.kind == "h2h":
        payload["h2h"] = _h2h_payload(event, row.config or {}, request)
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

    config: {mode: "team"|"player", competitor_ids: [2-3 ids], design_id?}. Teams compare their
    aggregated TournamentTeamMatchStats (kills/booyahs/points/matches); players their
    TournamentPlayerMatchStats (kills/damage/assists + the 3D-room rich stats when the debugger
    backfill has filled them). The picked DESIGN drives the look (bg + colors) - "overlays are
    created based off available designs"; the full versus design-editor type is the next phase.
    Returns {mode, competitors: [...], design: {...}} for the public overlay_config feed."""
    from django.db.models import Sum, Count, Case, When, Value, IntegerField
    from .models import TournamentTeamMatchStats, TournamentPlayerMatchStats

    mode = (config.get("mode") or "team").strip()
    ids = [int(i) for i in (config.get("competitor_ids") or []) if str(i).strip()][:3]
    competitors = []

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
            competitors.append({
                "name": getattr(u, "in_game_name", "") or u.username,
                "image": (request.build_absolute_uri(u.esports_pic.url)
                          if getattr(u, "esports_pic", None) else None),
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
    """The design-templated booyah banner's extras (owner 2026-07-02): the picked design's look +
    the WINNING TEAM'S ROSTER (player names + esport images) so the banner can show the players of
    the booyah team, not just the team name/logo. Roster resolves from config.team_name against the
    event's registered teams (works for manual, auto-fired and live-resolved configs alike)."""
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
                roster.append({
                    "name": getattr(u, "in_game_name", "") or u.username,
                    "image": (request.build_absolute_uri(u.esports_pic.url)
                              if getattr(u, "esports_pic", None) else None),
                })
    return {"design": _design_look(config.get("design_id"), request), "roster": roster}


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
    """GET events/capture/version/ — the latest capture-payload release descriptor (or 404 when no
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
    """GET events/capture/config/ — centralised runtime settings for the capture app, so ops can
    tune cadences/endpoints without any code or exe change."""
    return Response({
        "live_push_interval_seconds": 2,
        "upload_endpoint": "/events/upload-team-match-result/",
        "live_push_endpoint": "/events/live/push/",
        "resolve_endpoint": "/events/capture/resolve/",
    }, status=200)
