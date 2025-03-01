from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .models import Tournament, Match, MatchLeaderboard, OverallLeaderboard, ResultImage, RegisteredTeams, MatchTeamStats  # Ensure correct model imports
from afc_auth.models import User
from celery import shared_task
import openai
import json
import base64
import os
import json
from django.conf import settings
from django.db import models
from afc_auth.models import User
from afc_team.models import Team
from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from celery import shared_task
import os
import json
import base64
import openai
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .models import Match
from datetime import datetime
from django.db import transaction

# Ensure your OpenAI API key is set in your environment
# openai.api_key = "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"


# def extract_data_from_image(image_path, json_schema):
#     try:
#         with open(image_path, 'rb') as image_file:
#             image_base64 = base64.b64encode(image_file.read()).decode('utf-8')

#         response = openai.ChatCompletion.create(
#             model='gpt-4-vision-preview',
#             messages=[
#                 {"role": "user", "content": f"Extract data from this image based on the following JSON Schema:\n{json.dumps(json_schema)}"},
#                 {"role": "user", "content": f"Image data (base64): {image_base64}"}
#             ]
#         )

#         extracted_data = json.loads(response['choices'][0]['message']['content'])
#         return extracted_data

#     except Exception as e:
#         print(f"Error during image data extraction: {e}")
#         return None


client = openai.OpenAI(api_key="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")

def extract_data_from_image(image_path, json_schema):
    try:
        with open(image_path, 'rb') as image_file:
            image_base64 = base64.b64encode(image_file.read()).decode('utf-8')

        # Updated call with client.completions.create
        response = client.chat.completions.create(
            model="gpt-4o",  # Ensure you have access to this model
            messages=[
                {"role": "user", "content": f"Extract data from this image based on the following JSON Schema:\n{json.dumps(json_schema)}"},
                {"role": "user", "content": f"Image data (base64): {image_base64}"}
            ],
            max_tokens=1000
        )

        # Adjusted extraction for the new format
        extracted_data = json.loads(response.choices[0].message.content)
        return extracted_data

    except Exception as e:
        print(f"Error during image data extraction: {e}")
        return None


@api_view(["POST"])
def create_tournament(request):
    name = request.data.get("name")
    session_token = request.data.get("session_token")
    teams_names = request.data.getlist("teams_names")

    if not name or not session_token:
        return Response({"status": "error", "message": "Name and session token are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        creator = User.objects.get(session_token=session_token)
        if not creator.is_admin:
            return Response({"status": "error", "message": "User is not an admin."}, status=status.HTTP_403_FORBIDDEN)
    except User.DoesNotExist:
        return Response({"status": "error", "message": "Admin not found."}, status=status.HTTP_404_NOT_FOUND)

    with transaction.atomic():
        tournament = Tournament.objects.create(name=name, creator=creator)
        registered_teams = []
        
        for team_name in teams_names:
            try:
                team = Team.objects.get(team_name=team_name)
                if not RegisteredTeams.objects.filter(tournament=tournament, team=team).exists():
                    registered_teams.append(RegisteredTeams(tournament=tournament, team=team))
            except Team.DoesNotExist:
                return Response({"status": "error", "message": f"Team '{team_name}' not found."}, status=status.HTTP_404_NOT_FOUND)
        
        RegisteredTeams.objects.bulk_create(registered_teams)

    return Response({
        "status": "success",
        "message": "Tournament created successfully.",
        "data": {
            "tournament_id": tournament.tournament_id,
            "name": tournament.name,
            "creator": creator.username,
        }
    }, status=status.HTTP_201_CREATED)


def load_json_schema(schema_file: str) -> dict:
    try:
        with open(os.path.join(settings.BASE_DIR, schema_file), 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        print(f"Error: {schema_file} not found.")
        return {}


def merge_results(existing_results, new_results):
    for player, score in new_results.items():
        if player not in existing_results:
            existing_results[player] = score
    return existing_results


def calculate_rank(results):
    sorted_results = sorted(results.items(), key=lambda x: x[1], reverse=True)
    ranked = {player: rank + 1 for rank, (player, score) in enumerate(sorted_results)}
    return ranked


@shared_task
def process_match_result_images(match_id):
    try:
        match = Match.objects.get(pk=match_id)
        images = match.result_images.all()
        combined_results = {}

        score_schema = load_json_schema("test_schema.json")


        for result_image in images:
            extracted_data = extract_data_from_image(result_image.image.path, score_schema)
            print(extracted_data)
            if extracted_data:
                combined_results = merge_results(combined_results, extracted_data)

        rankings = calculate_rank(combined_results)
        for player, score in combined_results.items():
            user = User.objects.get(username=player)
            MatchLeaderboard.objects.create(
                match=match,
                team=user.team,  # Ensure the user belongs to a team
                score=score,
                rank=rankings[player]
            )

        match.processed = True
        match.save()

    except Match.DoesNotExist:
        print("Match not found")


@api_view(["POST"])
def upload_match_results(request, tournament_id, match_number):
    try:
        tournament = Tournament.objects.get(pk=tournament_id)
    except Tournament.DoesNotExist:
        return Response({"status": "error", "message": "Tournament not found."}, status=status.HTTP_404_NOT_FOUND)

    images = request.FILES.getlist("result_images")
    if not images or len(images) > 4:
        return Response({"status": "error", "message": "You must upload between 1 and 4 result images."}, status=status.HTTP_400_BAD_REQUEST)

    match, created = Match.objects.get_or_create(tournament=tournament, match_number=match_number)

    for image in images:
        ResultImage.objects.create(match=match, image=image)

    process_match_result_images(match.match_id)

    return Response({
        "status": "success",
        "message": "Match results uploaded successfully. Processing in progress.",
        "data": {
            "match_id": match.match_id,
            "match_number": match.match_number,
            "tournament": tournament.name,
            "processed": match.processed,
        },
    }, status=status.HTTP_201_CREATED)



@api_view(["GET"])
def get_all_mvps(request):
    session_token = request.headers.get('Authorization')  # Assuming session token is in Authorization header
    from_date = request.query_params.get('from_date')
    to_date = request.query_params.get('to_date')
    team = request.query_params.get('team')
    tournament_id = request.query_params.get('tournament_id')

    # Convert string dates to datetime objects if provided
    if from_date:
        from_date = datetime.strptime(from_date, "%Y-%m-%d")
    if to_date:
        to_date = datetime.strptime(to_date, "%Y-%m-%d")

    # Build the query filters based on provided parameters
    filters = {}
    if tournament_id:
        filters['tournament_id'] = tournament_id
    if from_date and to_date:
        filters['created_at__range'] = [from_date, to_date]
    elif from_date:
        filters['created_at__gte'] = from_date
    elif to_date:
        filters['created_at__lte'] = to_date
    if team:
        filters['matchleaderboard__team_id'] = team

    # Get all matches based on the filters
    matches = Match.objects.filter(**filters)

    # Retrieve the MVPs for the filtered matches
    mvps = []
    for match in matches:
        if match.mvp:
            mvps.append({
                "match_id": match.match_id,
                "match_number": match.match_number,
                "tournament_name": match.tournament.name,
                "mvp_name": match.mvp.full_name,
                "mvp_username": match.mvp.username
            })

    if not mvps:
        return Response({"message": "No MVPs found for the given criteria."}, status=status.HTTP_404_NOT_FOUND)

    return Response({"mvps": mvps})

@api_view(["GET"])
def get_tournaments_played(request):
    session_token = request.headers.get('Authorization')  # Assuming session token is in Authorization header
    player_id = request.query_params.get('player_id')  # Player ID for filtering by player
    team_id = request.query_params.get('team_id')  # Team ID for filtering by team
    from_date = request.query_params.get('from_date')
    to_date = request.query_params.get('to_date')

    # Convert string dates to datetime objects if provided
    if from_date:
        from_date = datetime.strptime(from_date, "%Y-%m-%d")
    if to_date:
        to_date = datetime.strptime(to_date, "%Y-%m-%d")

    # Validate that either player_id or team_id is provided
    if not player_id and not team_id:
        return Response({"error": "You must provide either a player_id or team_id."}, status=status.HTTP_400_BAD_REQUEST)

    # Build filters for tournaments
    filters = {}
    if from_date and to_date:
        filters['created_at__range'] = [from_date, to_date]
    elif from_date:
        filters['created_at__gte'] = from_date
    elif to_date:
        filters['created_at__lte'] = to_date

    # Get all tournaments based on the date filters
    tournaments = Tournament.objects.filter(**filters)

    # Now we need to check if the player or team participated in these tournaments
    tournaments_played = 0
    for tournament in tournaments:
        # Check if player participated
        if player_id:
            # Find matches in the tournament where the player participated
            match_leaderboards = MatchLeaderboard.objects.filter(match__tournament=tournament, team__players__id=player_id)
            if match_leaderboards.exists():
                tournaments_played += 1

        # Check if team participated
        if team_id:
            # Find matches in the tournament where the team participated
            match_leaderboards = MatchLeaderboard.objects.filter(match__tournament=tournament, team_id=team_id)
            if match_leaderboards.exists():
                tournaments_played += 1

    return Response({"tournaments_played": tournaments_played})


@api_view(["GET"])
def get_tournament_wins(request):
    session_token = request.headers.get('Authorization')  # Assuming session token is in Authorization header
    player_id = request.query_params.get('player_id')  # Player ID for filtering by player
    team_id = request.query_params.get('team_id')  # Team ID for filtering by team
    from_date = request.query_params.get('from_date')
    to_date = request.query_params.get('to_date')

    # Convert string dates to datetime objects if provided
    if from_date:
        from_date = datetime.strptime(from_date, "%Y-%m-%d")
    if to_date:
        to_date = datetime.strptime(to_date, "%Y-%m-%d")

    # Validate that either player_id or team_id is provided
    if not player_id and not team_id:
        return Response({"error": "You must provide either a player_id or team_id."}, status=status.HTTP_400_BAD_REQUEST)

    # Build filters for tournaments
    filters = {}
    if from_date and to_date:
        filters['created_at__range'] = [from_date, to_date]
    elif from_date:
        filters['created_at__gte'] = from_date
    elif to_date:
        filters['created_at__lte'] = to_date

    # Get all tournaments based on the date filters
    tournaments = Tournament.objects.filter(**filters)

    # Now we need to check if the player or team won these tournaments
    wins = 0
    for tournament in tournaments:
        # Get all matches in the tournament
        matches = Match.objects.filter(tournament=tournament)
        
        for match in matches:
            # Check if player participated and if they won
            if player_id:
                match_leaderboard = MatchLeaderboard.objects.filter(match=match, team__players__id=player_id).first()
                if match_leaderboard and match_leaderboard.rank == 1:
                    wins += 1

            # Check if team participated and if they won
            if team_id:
                match_leaderboard = MatchLeaderboard.objects.filter(match=match, team_id=team_id).first()
                if match_leaderboard and match_leaderboard.rank == 1:
                    wins += 1

    return Response({"tournament_wins": wins})


@api_view(["POST"])
def add_match(request):
    session_token = request.data.get("session_token")
    tournament_id = request.data.get("tournament_id")

    # Validate session token
    try:
        user = User.objects.get(login_session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token."}, status=401)

    # Validate tournament
    try:
        tournament = Tournament.objects.get(tournament_id=tournament_id)
    except Tournament.DoesNotExist:
        return Response({"error": "Tournament not found."}, status=404)

    # Check if the user is the creator of the tournament
    if tournament.creator != user:
        return Response({"error": "You are not authorized to add matches to this tournament."}, status=403)

    # Calculate the next match number
    existing_matches = Match.objects.filter(tournament=tournament).count()
    next_match_number = existing_matches + 1

    # Create the new match
    match = Match.objects.create(tournament=tournament, match_number=next_match_number)

    return Response({
        "message": "Match added successfully.",
        "match_id": match.match_id,
        "match_number": match.match_number
    }, status=201)


@api_view(["POST"])
def upload_team_result_based_on_match(request):
    session_token = request.data.get("session_token")
    match_number = request.data.get("match_number")
    team_name = request.data.get("team_name")
    team_pos = request.data.get("team_pos")
    players_names = request.data.getlist("players_names")
    players_kills = request.data.getlist("players_kills")

    # Validate session token
    try:
        user = User.objects.get(login_session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token."}, status=401)

    # Find the match based on match number
    try:
        match = Match.objects.get(match_number=match_number)
    except Match.DoesNotExist:
        return Response({"error": "Match not found."}, status=404)

    # Find the team
    try:
        team = Team.objects.get(name=team_name)
    except Team.DoesNotExist:
        return Response({"error": "Team not found."}, status=404)

    # Validate data length
    if len(players_names) != len(players_kills):
        return Response({"error": "Players and kills count mismatch."}, status=400)

    # Register team stats and kills
    total_kills = 0
    for name, kills in zip(players_names, players_kills):
        try:
            player = User.objects.get(username=name)
        except User.DoesNotExist:
            return Response({"error": f"Player {name} not found."}, status=404)

        MatchTeamStats.objects.create(match=match, team=team, player=player, kills=int(kills))
        total_kills += int(kills)

    # Update leaderboard
    score = (100 - int(team_pos)) + total_kills * 10  # Example scoring system
    MatchLeaderboard.objects.create(
        match=match,
        team=team,
        kills=total_kills,
        position_in_match=int(team_pos),
        score=score
    )

    match_leaderboard = MatchLeaderboard.objects.filter(match=match)
    for entry in match_leaderboard:
        entry.update_rankings()

    return Response({"message": "Team results uploaded successfully."}, status=201)


# @api_view(["GET"])
def get_match_leaderboard(request):
    session_token = request.data.get("session_token")
    match_id = request.data.get("match_id")

    # Validate session token
    try:
        user = User.objects.get(login_session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token."}, status=401)

    # Fetch match leaderboard
    try:
        match = Match.objects.get(match_id=match_id)
    except Match.DoesNotExist:
        return Response({"error": "Match not found."}, status=404)

    leaderboard = MatchLeaderboard.objects.filter(match=match).order_by("rank")
    data = [{
        "team": entry.team.name,
        "kills": entry.kills,
        "position": entry.position_in_match,
        "score": entry.score,
        "rank": entry.rank
    } for entry in leaderboard]

    return Response({"leaderboard": data}, status=200)


@api_view(["GET"])
def get_overall_leaderboard(request):
    session_token = request.data.get("session_token")
    tournament_id = request.data.get("tournament_id")

    # Validate session token
    try:
        user = User.objects.get(login_session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token."}, status=401)

    # Fetch overall leaderboard
    try:
        tournament = Tournament.objects.get(tournament_id=tournament_id)
    except Tournament.DoesNotExist:
        return Response({"error": "Tournament not found."}, status=404)

    overall_leaderboard = OverallLeaderboard.objects.filter(tournament=tournament).order_by("rank")
    data = [{
        "team_name": entry.team_name,
        "total_score": entry.total_score,
        "rank": entry.rank
    } for entry in overall_leaderboard]

    return Response({"overall_leaderboard": data}, status=200)


@api_view(["GET"])
def get_all_tournaments(request):
    session_token = request.data.get("session_token")

    # Validate session token
    try:
        user = User.objects.get(login_session_token=session_token)
    except User.DoesNotExist:
        return Response({"error": "Invalid session token."}, status=401)

    # Fetch all tournaments
    tournaments = Tournament.objects.all()
    data = [{
        "tournament_id": tournament.tournament_id,
        "name": tournament.name,
        "creator": tournament.creator.username,
        "created_at": tournament.created_at,
        "updated_at": tournament.updated_at
    } for tournament in tournaments]

    return Response({"tournaments": data}, status=200)


@api_view("POST")
def rank_teams_into_tiers(request):
    session_token = request.data.get("session_token")