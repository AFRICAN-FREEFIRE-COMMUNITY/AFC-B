from django.shortcuts import render
from django.shortcuts import get_object_or_404
from django.db.models import Sum
from rest_framework.response import Response
from rest_framework.decorators import api_view

from afc_auth.models import User
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

        data.append({
            "user_id": user.user_id,
            "name": user.username,
            "team_name": team_name,
            "total_kills": total_kills,
            "total_wins": total_wins,
            "total_mvps": total_mvps,
            "status": user.status,
            "role": user.role  # optional but useful
        })

    return Response({"users": data})


@api_view(["POST"])
def get_player_details(request):

    player_id = request.data.get("player_id")

    if not player_id:
        return Response({"message": "player_id is required"}, status=400)

    player = get_object_or_404(User, user_id=player_id)

    stats = TournamentPlayerMatchStats.objects.filter(
        player=player
    ).select_related(
        "team_stats__match__leaderboard__event"
    )

    total_kills = 0
    total_damage = 0
    total_matches = stats.count()

    scrim_kills = 0
    tournament_kills = 0

    for s in stats:
        total_kills += s.kills
        total_damage += s.damage

        event_type = s.team_stats.match.leaderboard.event.competition_type

        if event_type == "scrims":
            scrim_kills += s.kills
        else:
            tournament_kills += s.kills

    # MVPs
    total_mvps = Match.objects.filter(mvp=player).count()

    # Wins / Booyahs
    team_ids = TournamentTeamMember.objects.filter(user=player).values_list("tournament_team", flat=True)

    team_stats = TournamentTeamMatchStats.objects.filter(
        tournament_team_id__in=team_ids
    ).select_related("match__leaderboard__event")

    total_wins = 0
    scrim_wins = 0
    tournament_wins = 0

    scrim_booyah = 0
    tournament_booyah = 0

    for t in team_stats:
        event_type = t.match.leaderboard.event.competition_type

        if t.placement == 1:
            total_wins += 1

            if event_type == "scrims":
                scrim_wins += 1
                scrim_booyah += 1
            else:
                tournament_wins += 1
                tournament_booyah += 1

    # Calculations
    kdr = total_kills / total_matches if total_matches > 0 else 0
    avg_damage = total_damage / total_matches if total_matches > 0 else 0
    win_rate = (total_wins / total_matches * 100) if total_matches > 0 else 0

    # Team
    team_member = TournamentTeamMember.objects.filter(user=player).last()
    team_name = team_member.tournament_team.team.team_name if team_member else None

    return Response({
        "player_id": player.user_id,
        "name": player.username,
        "team": team_name,
        "email": player.email,
        "uid": player.uid,
        "discord_username": player.discord_username,
        "country": player.country,

        "kdr": round(kdr, 2),
        "avg_damage": round(avg_damage, 2),
        "win_rate": round(win_rate, 2),

        "total_kills": total_kills,
        "total_wins": total_wins,
        "total_mvps": total_mvps,

        "scrims_kills": scrim_kills,
        "tournaments_kills": tournament_kills,

        "scrims_wins": scrim_wins,
        "tournaments_wins": tournament_wins,

        "scrim_booyah": scrim_booyah,
        "tournament_booyah": tournament_booyah,
    })