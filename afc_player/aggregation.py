"""
afc_player/aggregation.py
─────────────────────────
Shared player-stats aggregation used by BOTH the admin player-profile endpoint
(afc_player.views.get_player_details, authenticated, keyed by player_id) and the
new PUBLIC player-profile endpoint (afc_player.views.get_public_player_stats,
no auth, keyed by username/IGN).

WHY a shared helper:
The admin endpoint already computes the canonical aggregate numbers (total_kills,
total_wins, total_mvps, kdr, avg_damage, win_rate, scrim/tournament splits,
booyahs). The public Team Stats + Player Profile pages need the SAME numbers plus
a per-event and per-match breakdown. Rather than duplicate (and risk drift), the
heavy lifting lives here once and both views call it.

DATA SOURCES (all real tables — nothing is fabricated here):
  • TournamentPlayerMatchStats  → per-player per-match kills / damage  (the player line)
  • TournamentTeamMatchStats     → per-team   per-match placement / points (the team line
                                    the player's booyah / win is read from)
  • Match.mvp                    → MVP awards
  • Event (via match.leaderboard.event) → competition_type (tournament vs scrims),
                                    name, date, tier

If a player has no recorded stats every number is simply 0 / every list empty —
that is the truthful empty state, not a stub.

NOTE on competition_type: an Event row's competition_type is "tournament" or
"scrims" (see Event.COMPETITION_TYPE_CHOICES). We split kills/wins on that value,
mirroring the admin endpoint's existing `if event_type == "scrims"` branch.
"""
from collections import OrderedDict

from afc_auth.models import User
from afc_team.models import TeamMembers
from afc_tournament_and_scrims.models import (
    Match,
    TournamentPlayerMatchStats,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)


# Map the Event's stored tournament_tier code to a human label for display.
# (Event.TOURNAMENT_TIER_CHOICES = tier_1/tier_2/tier_3.)
_EVENT_TIER_LABELS = {
    "tier_1": "Tier 1",
    "tier_2": "Tier 2",
    "tier_3": "Tier 3",
}


def _event_of(team_stats_row):
    """
    Safely walk TournamentTeamMatchStats → Match → Leaderboard → Event.

    Match.leaderboard is nullable, so the admin endpoint's direct
    `row.match.leaderboard.event` access can raise on matches that were entered
    without a leaderboard. This helper returns None instead of crashing, so the
    public surface degrades gracefully on partial data.
    """
    match = getattr(team_stats_row, "match", None)
    if match is None:
        return None
    leaderboard = getattr(match, "leaderboard", None)
    if leaderboard is None:
        return None
    return getattr(leaderboard, "event", None)


def compute_player_stats(player, *, include_breakdown=True):
    """
    Compute the full aggregate stat block for a single player (a User instance).

    Returns a dict with:
      • the same scalar aggregates the admin endpoint returns
        (total_kills, total_wins, total_mvps, kdr, avg_damage, win_rate,
         scrims_kills, tournaments_kills, scrims_wins, tournaments_wins,
         scrim_booyah, tournament_booyah, total_matches)
      • when include_breakdown=True, two extra real lists:
         - per_event[]  → one row per Event the player competed in
         - recent_matches[] → the player's last 25 individual match lines

    `include_breakdown=False` is available for callers (e.g. a future list view)
    that only need the scalars and want to skip the per-row work.
    """
    # ── 1. Per-player match lines (the individual kill/damage record) ──
    # select_related the full Event path so each row read is one query, not N.
    player_stat_rows = (
        TournamentPlayerMatchStats.objects.filter(player=player)
        .select_related(
            "team_stats",
            "team_stats__match",
            "team_stats__match__leaderboard",
            "team_stats__match__leaderboard__event",
        )
    )

    total_kills = 0
    total_damage = 0
    total_matches = player_stat_rows.count()

    scrim_kills = 0
    tournament_kills = 0

    # per-event accumulator (kills / mvps / placement context), keyed by event_id.
    # OrderedDict keeps insertion order stable for a deterministic response.
    events_acc = OrderedDict()

    # per-match breakdown rows (individual player lines)
    match_breakdown = []

    for s in player_stat_rows:
        total_kills += s.kills
        total_damage += s.damage

        team_stats = s.team_stats
        event = _event_of(team_stats)
        event_type = event.competition_type if event else None

        if event_type == "scrims":
            scrim_kills += s.kills
        elif event_type is not None:
            # any non-scrims competition_type counts as tournament (mirrors admin's else-branch)
            tournament_kills += s.kills

        if include_breakdown and event is not None:
            # ── per-event roll-up ──
            acc = events_acc.get(event.event_id)
            if acc is None:
                acc = {
                    "event_id": event.event_id,
                    "event_name": event.event_name,
                    "competition_type": event.competition_type,
                    "event_date": event.start_date.isoformat() if event.start_date else None,
                    "tournament_tier": event.tournament_tier,
                    "tournament_tier_label": _EVENT_TIER_LABELS.get(event.tournament_tier),
                    "kills": 0,
                    "damage": 0,
                    "matches_played": 0,
                    "mvps": 0,
                    "best_placement": None,   # filled from the team line below
                    "total_points": 0,        # filled from the team line below
                }
                events_acc[event.event_id] = acc
            acc["kills"] += s.kills
            acc["damage"] += s.damage
            acc["matches_played"] += 1

            # ── per-match line ──
            match = getattr(team_stats, "match", None)
            match_breakdown.append({
                "event_id": event.event_id,
                "event_name": event.event_name,
                "competition_type": event.competition_type,
                "match_number": getattr(match, "match_number", None),
                "match_map": getattr(match, "match_map", None),
                "match_date": match.match_date.isoformat() if match and match.match_date else None,
                # team line context for this match (placement / team points)
                "placement": team_stats.placement,
                "team_points": team_stats.total_points,
                # the player's own line
                "kills": s.kills,
                "damage": s.damage,
                "assists": s.assists,
                # is this player the MVP of this match?
                "is_mvp": bool(match and match.mvp_id == player.user_id),
            })

    # ── 2. MVPs (Match.mvp points at the User) ──
    total_mvps = Match.objects.filter(mvp=player).count()

    # ── 3. Wins / booyahs (read from the TEAM line of every team the player rostered) ──
    # A "win" is a team placement == 1 in a match the player's tournament-team played.
    team_ids = TournamentTeamMember.objects.filter(user=player).values_list(
        "tournament_team", flat=True
    )
    team_stat_rows = (
        TournamentTeamMatchStats.objects.filter(tournament_team_id__in=team_ids)
        .select_related(
            "match",
            "match__leaderboard",
            "match__leaderboard__event",
            "tournament_team__event",
        )
    )

    total_wins = 0
    scrim_wins = 0
    tournament_wins = 0
    scrim_booyah = 0
    tournament_booyah = 0

    for t in team_stat_rows:
        event = _event_of(t)
        event_type = event.competition_type if event else None

        if t.placement == 1:
            total_wins += 1
            if event_type == "scrims":
                scrim_wins += 1
                scrim_booyah += 1
            elif event_type is not None:
                tournament_wins += 1
                tournament_booyah += 1

        # fold the team line into the per-event roll-up (best placement + team points)
        if include_breakdown and event is not None and event.event_id in events_acc:
            acc = events_acc[event.event_id]
            acc["total_points"] += t.total_points
            if acc["best_placement"] is None or t.placement < acc["best_placement"]:
                acc["best_placement"] = t.placement

    # ── 4. Fold MVP counts into the per-event roll-up ──
    if include_breakdown:
        mvp_event_rows = (
            Match.objects.filter(mvp=player)
            .select_related("leaderboard", "leaderboard__event")
        )
        for m in mvp_event_rows:
            lb = getattr(m, "leaderboard", None)
            ev = getattr(lb, "event", None) if lb else None
            if ev is not None and ev.event_id in events_acc:
                events_acc[ev.event_id]["mvps"] += 1

    # ── 5. Derived ratios (guard divide-by-zero exactly like the admin endpoint) ──
    kdr = total_kills / total_matches if total_matches > 0 else 0
    avg_damage = total_damage / total_matches if total_matches > 0 else 0
    win_rate = (total_wins / total_matches * 100) if total_matches > 0 else 0

    result = {
        "total_matches": total_matches,
        "total_kills": total_kills,
        "total_wins": total_wins,
        "total_mvps": total_mvps,
        "kdr": round(kdr, 2),
        "avg_damage": round(avg_damage, 2),
        "win_rate": round(win_rate, 2),
        "scrims_kills": scrim_kills,
        "tournaments_kills": tournament_kills,
        "scrims_wins": scrim_wins,
        "tournaments_wins": tournament_wins,
        "scrim_booyah": scrim_booyah,
        "tournament_booyah": tournament_booyah,
    }

    if include_breakdown:
        # newest events first by date (None dates sort last)
        per_event = list(events_acc.values())
        per_event.sort(key=lambda e: (e["event_date"] is not None, e["event_date"]), reverse=True)
        result["per_event"] = per_event

        # newest 25 match lines first
        match_breakdown.sort(
            key=lambda m: (m["match_date"] is not None, m["match_date"]), reverse=True
        )
        result["recent_matches"] = match_breakdown[:25]

    return result


def basic_player_profile(player, request=None):
    """
    The public, NON-sensitive identity block for a player.

    Deliberately omits email and any other PII. Mirrors the field names the
    existing public /team/get-player-details/ endpoint already exposes
    (username, country, profile_picture, uid, team, roles, join_date) so the
    frontend can reuse its existing types.

    `request` (optional) is used only to build absolute media URLs.
    """
    from afc_auth.models import UserProfile

    profile = UserProfile.objects.filter(user=player).first()

    def _abs(media_field):
        if not media_field:
            return None
        url = media_field.url
        return request.build_absolute_uri(url) if request is not None else url

    # current team (if any) — a player may be on no team; handle gracefully
    membership = (
        TeamMembers.objects.select_related("team").filter(member=player).first()
    )
    team_block = None
    in_game_role = None
    management_role = None
    join_date = None
    if membership is not None:
        team = membership.team
        in_game_role = membership.in_game_role
        management_role = membership.management_role
        join_date = membership.join_date
        team_block = {
            "team_id": team.team_id,
            "team_name": team.team_name,
            "team_tag": team.team_tag,
            "team_logo": _abs(getattr(team, "team_logo", None)),
        }

    return {
        "username": player.username,
        # Player flag = IP-derived country (owner 2026-06-29), profile country as fallback. See
        # afc_auth.views.set_ip_country / User.ip_country. Consumed by the public player profile.
        "country": (player.ip_country or player.country),
        "uid": player.uid,
        "discord_username": player.discord_username,
        "profile_picture": _abs(getattr(profile, "profile_pic", None)) if profile else None,
        "esports_picture": _abs(getattr(profile, "esports_pic", None)) if profile else None,
        "in_game_role": in_game_role,
        "management_role": management_role,
        "join_date": join_date.isoformat() if join_date else None,
        "team": team_block,
    }


def player_tier_history(player):
    """
    Per-season tier + rank history for a player, sourced from the real
    afc_rankings.PlayerQuarterlyScore table — ONLY for seasons whose tiers have
    been published (Season.tiers_published). Rank is shown only when the season's
    rankings are published (Season.rankings_published). This mirrors the public
    rankings read API's two independent publish gates exactly.

    Returns a list (possibly empty) of:
      {season_id, season_name, year, quarter, tier, tier_label, rank}

    If afc_rankings is unavailable for any reason, returns [] (the frontend then
    shows the truthful "no tier history" empty state).
    """
    try:
        from afc_rankings.models import PlayerQuarterlyScore
        from afc_rankings.serializers import TIER_LABELS
    except Exception:
        return []

    rows = (
        PlayerQuarterlyScore.objects.filter(player=player)
        .select_related("season")
        .order_by("season__year", "season__quarter")
    )

    history = []
    for r in rows:
        season = r.season
        # tier is gated behind tiers_published; rank behind rankings_published
        tier = r.tier_assigned if season.tiers_published else None
        rank = r.rank if season.rankings_published else None
        # skip seasons that expose nothing publicly yet
        if tier is None and rank is None:
            continue
        history.append({
            "season_id": season.season_id,
            "season_name": season.name,
            "year": season.year,
            "quarter": season.quarter,
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier) if tier is not None else None,
            "rank": rank,
        })
    return history
