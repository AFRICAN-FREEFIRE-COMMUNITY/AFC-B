from django.shortcuts import render
from django.shortcuts import get_object_or_404
from django.db.models import Sum
from rest_framework.response import Response
from rest_framework.decorators import api_view

from afc_auth.models import User, BannedPlayer
from afc_team.models import TeamMembers
from afc_tournament_and_scrims.models import Match, TournamentPlayerMatchStats, TournamentTeamMatchStats, TournamentTeamMember
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db.models import Sum
from afc_auth.models import User
from afc_tournament_and_scrims.models import (
    TournamentPlayerMatchStats,
    TournamentTeamMatchStats,
    TournamentTeamMember,
    Match
)

# Shared player-stats aggregation (reused by the admin + public player endpoints).
from afc_player.aggregation import (
    compute_player_stats,
    basic_player_profile,
    player_tier_history,
)



# Create your views here.



@api_view(["GET"])
def get_all_users(request):

    users = User.objects.all()

    data = []

    for user in users:

        # --- PLAYER STATS ---
        player_stats = TournamentPlayerMatchStats.objects.filter(player=user)

        total_kills = player_stats.aggregate(total=Sum("kills"))["total"] or 0

        # --- MVPs ---
        total_mvps = Match.objects.filter(mvp=user).count()

        # --- TEAM RELATION ---
        team_member = TournamentTeamMember.objects.filter(user=user).last()

        team_name = None
        team_ids = []

        if team_member:
            team_name = team_member.tournament_team.team.team_name
            team_ids = TournamentTeamMember.objects.filter(user=user).values_list(
                "tournament_team", flat=True
            )

        # --- WINS (placement = 1) ---
        total_wins = 0
        if team_ids:
            total_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team_id__in=team_ids,
                placement=1
            ).count()

        # --- BAN STATE ---
        is_banned = BannedPlayer.objects.filter(banned_player=user, is_active=True).exists()

        data.append({
            "user_id": user.user_id,
            "name": user.username,
            "team_name": team_name,
            "total_kills": total_kills,
            "total_wins": total_wins,
            "total_mvps": total_mvps,
            "status": "banned" if is_banned else user.status,
            "role": user.role  # optional but useful
        })

    return Response({"users": data})


@api_view(["POST"])
def get_player_details(request):
    # ADMIN player profile (keyed by player_id). The heavy stat aggregation now lives in
    # afc_player.aggregation.compute_player_stats so the public player page can reuse the
    # EXACT same numbers (single source of truth, no drift). This response keeps every key
    # it returned before — the shared helper produces the same scalar names — and additionally
    # gains per_event[] / recent_matches[] breakdown lists (additive; old callers ignore them).

    player_id = request.data.get("player_id")

    if not player_id:
        return Response({"message": "player_id is required"}, status=400)

    player = get_object_or_404(User, user_id=player_id)

    # Shared aggregation (kills/wins/mvps/kdr/avg_damage/win_rate + scrim/tournament splits
    # + booyahs + per_event[] + recent_matches[]). Defensive against null leaderboards.
    agg = compute_player_stats(player, include_breakdown=True)

    # Team + roles (unchanged behaviour: last tournament team for display name, current
    # TeamMembers row for in-game / management role).
    team_member = TournamentTeamMember.objects.filter(user=player).last()
    team_name = team_member.tournament_team.team.team_name if team_member else None
    member = TeamMembers.objects.filter(member=player).first()
    in_game_role = member.in_game_role if member else None
    management_role = member.management_role if member else None

    return Response({
        "player_id": player.user_id,
        "name": player.username,
        "team": team_name,
        "email": player.email,            # admin surface — PII allowed here (auth-gated)
        "uid": player.uid,
        "discord_username": player.discord_username,
        "country": player.country,
        "in_game_role": in_game_role,
        "management_role": management_role,

        # ── scalar aggregates (unchanged keys, now from the shared helper) ──
        "kdr": agg["kdr"],
        "avg_damage": agg["avg_damage"],
        "win_rate": agg["win_rate"],

        "total_kills": agg["total_kills"],
        "total_wins": agg["total_wins"],
        "total_mvps": agg["total_mvps"],

        "scrims_kills": agg["scrims_kills"],
        "tournaments_kills": agg["tournaments_kills"],

        "scrims_wins": agg["scrims_wins"],
        "tournaments_wins": agg["tournaments_wins"],

        "scrim_booyah": agg["scrim_booyah"],
        "tournament_booyah": agg["tournament_booyah"],

        # ── NEW additive breakdown (admin page can render the same tables the public page does) ──
        "total_matches": agg["total_matches"],
        "per_event": agg["per_event"],
        "recent_matches": agg["recent_matches"],
    })


@api_view(["POST"])
def get_public_player_stats(request):
    """
    PUBLIC player profile + stats (NO auth), keyed by USERNAME / IGN.

    This is the public counterpart to the admin get_player_details above. It powers the
    public Player Profile page (feature D). It returns:
      • a NON-sensitive identity block (NO email / no PII)        — basic_player_profile()
      • the SAME aggregated stats as the admin endpoint           — compute_player_stats()
      • a per-event list and per-match breakdown                  — included in the agg
      • the player's published tier / rank history per season     — player_tier_history()

    Body: {"player_ign": "<username>"}.
    A player with no recorded matches simply returns zeroes and empty lists (truthful
    empty state — nothing is fabricated). A player on no team returns team: null.
    """
    player_ign = request.data.get("player_ign")
    if not player_ign:
        return Response({"message": "player_ign is required."}, status=400)

    try:
        player = User.objects.get(username=player_ign)
    except User.DoesNotExist:
        return Response({"message": "Player not found."}, status=404)

    # Identity (public, no PII) + full stat block (scalars + per_event + recent_matches)
    profile = basic_player_profile(player, request=request)
    stats = compute_player_stats(player, include_breakdown=True)
    tier_history = player_tier_history(player)

    return Response({
        "player": {
            **profile,
            # scalar aggregates
            "total_matches": stats["total_matches"],
            "total_kills": stats["total_kills"],
            "total_wins": stats["total_wins"],
            "total_mvps": stats["total_mvps"],
            "kdr": stats["kdr"],
            "avg_damage": stats["avg_damage"],
            "win_rate": stats["win_rate"],
            "scrims_kills": stats["scrims_kills"],
            "tournaments_kills": stats["tournaments_kills"],
            "scrims_wins": stats["scrims_wins"],
            "tournaments_wins": stats["tournaments_wins"],
            "scrim_booyah": stats["scrim_booyah"],
            "tournament_booyah": stats["tournament_booyah"],
            # breakdown lists
            "per_event": stats["per_event"],
            "recent_matches": stats["recent_matches"],
            # published tier / rank history (gated by afc_rankings publish flags)
            "tier_history": tier_history,
        }
    })