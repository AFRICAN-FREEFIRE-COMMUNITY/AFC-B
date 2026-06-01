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


def team_monthly(s):
    return {
        "rank": s.rank,
        "team_id": s.team_id,
        "team_name": _team_name(s),
        "is_ghost": bool(getattr(s, "ghost_team_id", None)),
        "total_score": round(s.total_score, 2),
        "tournament_pts": round(s.tournament_pts, 2),
        "scrim_pts": round(s.scrim_pts, 2),
        "wins": s.tournament_wins,
        "kills": s.total_kills,
        "tournaments_played": s.tournaments_played,
        "month": s.month.isoformat(),
    }


def team_quarterly(s):
    return {
        "rank": s.rank,
        "team_id": s.team_id,
        "team_name": _team_name(s),
        "is_ghost": bool(getattr(s, "ghost_team_id", None)),
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
        "tier_overridden": s.tier_overridden,
        "is_zeroed": s.is_zeroed,
        "points_deducted": round(s.points_deducted, 2),
        "effective_score": round(max(0.0, s.total_score - s.points_deducted), 2),
        "season_id": s.season_id,
    }


def player_monthly(s):
    return {
        "rank": s.rank,
        "player_id": s.player_id,
        "username": s.player.username,
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
    return {
        "rank": s.rank,
        "player_id": s.player_id,
        "username": s.player.username,
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
        "transfer_window_is_open": s.is_transfer_window_open(),
        "is_active": s.is_active, "tier_eval_run": s.tier_eval_run,
        # independent publish gates (rankings vs tiers).
        "rankings_published": s.rankings_published,
        "tiers_published": s.tiers_published,
    }
