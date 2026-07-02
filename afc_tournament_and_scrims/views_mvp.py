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
from .views import _broadcast_gate

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


@api_view(["GET", "POST"])
def event_mvp(request, event_id):
    """GET/POST events/<event_id>/mvp/ — save (POST) the criteria arrangement + scope, then return:
    the per-map MVP list, the per-player MVP counts, and the event MVP (most per-map MVPs; count
    ties broken by the same criteria on event totals). See the module docstring."""
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

    # ── Every player line of the event, grouped per MATCH (map). ──
    qs = (
        TournamentPlayerMatchStats.objects
        .filter(team_stats__match__group__stage__event=event)
        .select_related(
            "player", "team_stats", "team_stats__match", "team_stats__match__group",
            "team_stats__match__group__stage", "team_stats__tournament_team__team",
        )
    )

    by_match = defaultdict(list)   # match_id -> [player line stats]
    players = {}                   # user_id -> accumulated event totals + identity
    for s in qs:
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
                "esports_image": (
                    request.build_absolute_uri(p.esports_pic.url)
                    if getattr(p, "esports_pic", None) else None
                ),
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

    return Response({
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
    }, status=200)


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
