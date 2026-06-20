"""Plain serialization helpers + pagination (matches the codebase's manual-dict style)."""
import datetime

TIER_LABELS = {0: "Elite", 1: "Competitive", 2: "Rising", 3: "Entry"}


def paginate(request, qs):
    """Canonical envelope: returns (page_list, pagination_meta)."""
    try:
        limit = max(1, min(100, int(request.GET.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    total = qs.count()
    items = list(qs[offset:offset + limit])
    has_more = offset + limit < total
    meta = {
        "limit": limit, "offset": offset, "total_count": total,
        "has_more": has_more, "next_offset": (offset + limit) if has_more else None,
    }
    return items, meta


def _team_name(s):
    if s.team_id:
        return s.team.team_name
    if getattr(s, "ghost_team_id", None):
        return f"[Ghost] {s.ghost_team.team_name}"
    return "Unknown"


def _player_name(s):
    """Display label for a player score row, real OR ghost. Mirrors _team_name above so the player
    ladders render ghost rows without dereferencing a null player. Read by player_monthly /
    player_quarterly below; the FE shows the "[Ghost]" prefix as a badge (a ghost has no profile)."""
    if s.player_id:
        return s.player.username
    if getattr(s, "ghost_player_id", None):
        return f"[Ghost] {s.ghost_player.ign}"
    return "Unknown"


# ───────────────────────── ghost-claim hints for the PUBLIC ladders ─────────────────────────
# A ghost row in the public ladder has no team_id / player_id (those are NULL for a ghost), so the
# public client needs the ghost's OWN id to target the user-facing claim-request endpoints:
#   POST /rankings/ghost-teams/<uuid>/request-claim/     (team ladder)
#   POST /rankings/ghost-players/<int>/request-claim/    (player ladder)
# These two helpers expose that id PLUS the ghost's claim_status so the FE can hide the "Claim"
# button once a ghost is already pending/claimed. They read off the (select_related) ghost relation,
# so no extra query: the views already select_related("ghost_team") / ("ghost_player"). Real rows
# return (None, None) and the FE simply ignores the keys (it only acts on is_ghost rows).
# Consumed by: app/(user)/rankings/page.tsx → ClaimGhostDialog (lib/rankings.ts TeamRow/PlayerRow).
def _ghost_team_claim(s):
    """(ghost_team_id:str|None, claim_status:str|None) for a team score row, both None if real."""
    if getattr(s, "ghost_team_id", None):
        return str(s.ghost_team_id), s.ghost_team.claim_status
    return None, None


def _ghost_player_claim(s):
    """(ghost_player_id:int|None, claim_status:str|None) for a player score row, both None if real."""
    if getattr(s, "ghost_player_id", None):
        return s.ghost_player_id, s.ghost_player.claim_status
    return None, None


def team_monthly(s):
    gid, gstatus = _ghost_team_claim(s)
    return {
        "rank": s.rank,
        "team_id": s.team_id,
        "team_name": _team_name(s),
        # Team country (Team.country) so the rankings UI can show a flag beside the name; None for
        # ghost rows (no real Team). No extra query - the view select_relateds team. (owner 2026-06-20)
        "country": (s.team.country if s.team_id else None),
        "is_ghost": bool(getattr(s, "ghost_team_id", None)),
        # ghost-claim hints (NULL for real rows): let the FE target request-claim + hide the
        # button once not unclaimed. See _ghost_team_claim above.
        "ghost_team_id": gid,
        "claim_status": gstatus,
        "total_score": round(s.total_score, 2),
        "tournament_pts": round(s.tournament_pts, 2),
        "scrim_pts": round(s.scrim_pts, 2),
        "wins": s.tournament_wins,
        "kills": s.total_kills,
        "tournaments_played": s.tournaments_played,
        "month": s.month.isoformat(),
    }


def team_quarterly(s):
    gid, gstatus = _ghost_team_claim(s)
    return {
        "rank": s.rank,
        "team_id": s.team_id,
        "team_name": _team_name(s),
        # Team country (Team.country) so the rankings UI can show a flag beside the name; None for
        # ghost rows (no real Team). No extra query - the view select_relateds team. (owner 2026-06-20)
        "country": (s.team.country if s.team_id else None),
        "is_ghost": bool(getattr(s, "ghost_team_id", None)),
        # ghost-claim hints (NULL for real rows): see _ghost_team_claim above.
        "ghost_team_id": gid,
        "claim_status": gstatus,
        "total_score": round(s.total_score, 2),
        "tournament_pts": round(s.tournament_pts, 2),
        "scrim_pts": round(s.scrim_pts, 2),
        "prize_money_pts": round(s.prize_money_pts, 2),
        "social_media_pts": round(s.social_media_pts, 2),
        "tier": s.tier_assigned,
        "tier_label": TIER_LABELS.get(s.tier_assigned),
        "wins": s.tournament_wins,
        "kills": s.total_kills,
        "tournaments_played": s.participated_in_tournaments,
        "meets_participation_floor": s.meets_participation_floor,
        "insufficient_activity_note": s.insufficient_activity_note,
        # Admin-override state (used by the /a/rankings/overrides surface; the public
        # /rankings page simply ignores these extra keys). effective_score is the score
        # after any manual point deduction, floored at 0.
        # tier_overridden / is_zeroed / points_deducted are written by admin_overrides.py;
        # effective_score = max(0, score - deducted) must stay in lockstep with that module
        # so the public number matches the admin number.
        "tier_overridden": s.tier_overridden,
        "is_zeroed": s.is_zeroed,
        "points_deducted": round(s.points_deducted, 2),
        "effective_score": round(max(0.0, s.total_score - s.points_deducted), 2),
        "season_id": s.season_id,
    }


def player_monthly(s):
    gpid, gstatus = _ghost_player_claim(s)
    return {
        "rank": s.rank,
        "player_id": s.player_id,
        "username": _player_name(s),
        "is_ghost": bool(getattr(s, "ghost_player_id", None)),
        # ghost-claim hints (NULL for real rows): let the FE target request-claim + hide the
        # button once not unclaimed. See _ghost_player_claim above.
        "ghost_player_id": gpid,
        "claim_status": gstatus,
        "total_score": round(s.total_score, 2),
        "kill_pts": round(s.kill_pts, 2),
        "placement_pts": round(s.placement_pts, 2),
        "mvp_pts": round(s.mvp_pts, 2),
        "finals_pts": round(s.finals_pts, 2),
        "team_win_pts": round(s.team_win_pts, 2),
        "participation_pts": round(s.participation_pts, 2),
        "scrim_pts": round(s.scrim_kill_pts + s.scrim_win_pts, 2),
        "kills": s.total_kills,
        "mvps": s.mvp_count,
        "month": s.month.isoformat(),
    }


def player_quarterly(s):
    gpid, gstatus = _ghost_player_claim(s)
    return {
        "rank": s.rank,
        "player_id": s.player_id,
        "username": _player_name(s),
        "is_ghost": bool(getattr(s, "ghost_player_id", None)),
        # ghost-claim hints (NULL for real rows): see _ghost_player_claim above.
        "ghost_player_id": gpid,
        "claim_status": gstatus,
        "total_score": round(s.total_score, 2),
        "prize_money_pts": round(s.prize_money_pts, 2),
        "tier": s.tier_assigned,
        "tier_label": TIER_LABELS.get(s.tier_assigned),
        "tier_source": s.tier_source,
        "season_id": s.season_id,
    }


def annual(s):
    return {
        "rank": s.rank,
        "year": s.year,
        "entity_type": s.entity_type,
        "team_id": s.team_id,
        "player_id": s.player_id,
        "name": (s.team.team_name if s.team_id else (s.player.username if s.player_id else "Unknown")),
        "total_score": round(s.total_score, 2),
        "q1": round(s.q1_score, 2), "q2": round(s.q2_score, 2),
        "q3": round(s.q3_score, 2), "q4": round(s.q4_score, 2),
    }


def season(s):
    return {
        "season_id": s.season_id, "name": s.name, "quarter": s.quarter, "year": s.year,
        "start_date": s.start_date.isoformat(), "end_date": s.end_date.isoformat(),
        "transfer_window_open": s.transfer_window_open.isoformat(),
        "transfer_window_close": s.transfer_window_close.isoformat(),
        # computed live so the public page can show a prominent OPEN/CLOSED indicator.
        # is_transfer_window_open() is the same Season method the afc_team roster guards
        # call (exit_team / kick_team_member / disband_team) — single source of truth for
        # the OPEN/CLOSED state shown publicly and enforced on roster moves.
        "transfer_window_is_open": s.is_transfer_window_open(),
        "is_active": s.is_active, "tier_eval_run": s.tier_eval_run,
        # independent publish gates (rankings vs tiers).
        "rankings_published": s.rankings_published,
        "tiers_published": s.tiers_published,
    }
