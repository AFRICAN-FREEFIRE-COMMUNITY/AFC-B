from datetime import date
import json

from celery import shared_task
from afc import settings
from django.shortcuts import get_object_or_404, render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_date

from afc_auth.views import assign_discord_role, check_discord_membership, discord_member_has_role, remove_discord_role, validate_token
# from afc_leaderboard_calc.models import Match, MatchLeaderboard
from afc_team.models import Team, TeamMembers
from .models import Event, RegisteredCompetitors, SoloPlayerMatchStats, StageCompetitor, StageGroupCompetitor, StageGroups, Stages, StreamChannel, TournamentTeam, Leaderboard, TournamentTeamMatchStats, Match, TournamentTeamMember
from afc_auth.models import DiscordRoleAssignment, DiscordStageRoleAssignmentProgress, Notifications, User
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from datetime import datetime, date, timedelta
from django.utils import timezone
from django.db.models import Count, Q, F, Sum, Max
from django.db.models.functions import TruncDate
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
# Create your views here.

from rest_framework.pagination import PageNumberPagination

def paginate_queryset(request, queryset, serializer_func):
    paginator = PageNumberPagination()
    paginator.page_size = int(request.GET.get("page_size", 10))  # default 10
    result_page = paginator.paginate_queryset(queryset, request)
    serialized = [serializer_func(obj) for obj in result_page]
    return paginator.get_paginated_response(serialized)



# @api_view(["POST"])
# def create_leaderboard(request):
#     session_token = request.headers.get("Authorization")
    
#     if not session_token:
#         return Response({"error": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

#     user = validate_token(session_token)
#     if not user:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     # Ensure only admins and moderators can create leaderboards
#     if user.role not in ["admin", "moderator"]:
#         return Response({"error": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)

#     leaderboard_name = request.data.get("leaderboard_name")
#     event_id = request.data.get("event_id")
#     stage = request.data.get("stage")
#     group = request.data.get("group", None)  # Optional field

#     if not all([leaderboard_name, event_id, stage]):
#         return Response({"error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except ObjectDoesNotExist:
#         return Response({"error": "Event not found"}, status=status.HTTP_404_NOT_FOUND)

#     leaderboard = Leaderboard.objects.create(
#         leaderboard_name=leaderboard_name,
#         event=event,
#         stage=stage,
#         group=group,
#         creator=user
#     )

#     return Response({"message": "Leaderboard created successfully", "leaderboard_id": leaderboard.leaderboard_id}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def get_all_events(request):
    events = Event.objects.filter(is_draft=False)
    event_list = []
    for event in events:
        event_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.start_date,
            "event_status": event.event_status,
            "competition_type": event.competition_type,
            "number_of_participants": event.max_teams_or_players,
            "prizepool": event.prizepool,
            "total_registered_competitors": RegisteredCompetitors.objects.filter(event=event).count(),
        })
    return Response({"events": event_list}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_events_paginated(request):
    limit = int(request.GET.get("limit", 10))
    offset = int(request.GET.get("offset", 0))

    events = Event.objects.filter(is_draft=False).order_by("-start_date")
    total = events.count()

    # slice manually (faster than Paginator for large tables)
    paginated = events[offset: offset + limit]

    event_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_date": event.start_date,
        "event_status": event.event_status,
        "competition_type": event.competition_type,
        "number_of_participants": event.max_teams_or_players,
        "prizepool": event.prizepool,
        "total_registered_competitors": RegisteredCompetitors.objects.filter(event=event).count(), 
    } for event in paginated]

    return Response({
        "count": total,
        "limit": limit,
        "offset": offset,
        "next": offset + limit if offset + limit < total else None,
        "previous": offset - limit if offset - limit >= 0 else None,
        "events": event_list,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_tournaments_and_scrims(request):
    events = Event.objects.filter(is_draft=False)
    event_list = []
    for event in events:
        event_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.start_date,
            "event_status": event.event_status,
        })
    return Response({"events": event_list}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_tournaments_and_scrims_paginated(request):
    limit = int(request.GET.get("limit", 10))
    offset = int(request.GET.get("offset", 0))

    events = Event.objects.filter(is_draft=False).order_by("-start_date")
    total = events.count()

    paginated = events[offset: offset + limit]

    event_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_date": event.start_date,
        "event_status": event.event_status,
    } for event in paginated]

    return Response({
        "count": total,
        "limit": limit,
        "offset": offset,
        "next": offset + limit if offset + limit < total else None,
        "previous": offset - limit if offset - limit >= 0 else None,
        "events": event_list,
    }, status=status.HTTP_200_OK)



@api_view(["GET"])
def get_all_tournaments_and_scrims_separated(request):
    tournaments = Event.objects.filter(competition_type="tournament", is_draft=False)
    scrims = Event.objects.filter(competition_type="scrim", is_draft=False)

    tournament_list = []
    for event in tournaments:
        tournament_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.start_date,
            "event_status": event.event_status,
        })

    scrim_list = []
    for event in scrims:
        scrim_list.append({
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.start_date,
            "event_status": event.event_status,
        })

    return Response({
        "tournaments": tournament_list,
        "scrims": scrim_list
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_tournaments_and_scrims_separated_paginated(request):
    limit = int(request.GET.get("limit", 10))
    offset = int(request.GET.get("offset", 0))

    tournaments = Event.objects.filter(competition_type="tournament", is_draft=False).order_by("-start_date")
    scrims = Event.objects.filter(competition_type="scrim", is_draft=False).order_by("-start_date")

    total_tournaments = tournaments.count()
    total_scrims = scrims.count()

    paginated_tournaments = tournaments[offset: offset + limit]
    paginated_scrims = scrims[offset: offset + limit]

    tournament_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_date": event.start_date,
        "event_status": event.event_status,
    } for event in paginated_tournaments]

    scrim_list = [{
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_date": event.start_date,
        "event_status": event.event_status,
    } for event in paginated_scrims]

    return Response({
        "tournaments": {
            "count": total_tournaments,
            "limit": limit,
            "offset": offset,
            "next": offset + limit if offset + limit < total_tournaments else None,
            "previous": offset - limit if offset - limit >= 0 else None,
            "items": tournament_list
        },
        "scrims": {
            "count": total_scrims,
            "limit": limit,
            "offset": offset,
            "next": offset + limit if offset + limit < total_scrims else None,
            "previous": offset - limit if offset - limit >= 0 else None,
            "items": scrim_list
        }
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def create_event(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]

    # Authenticate user
    user = validate_token(token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # Permissions
    if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
        return Response({"message": "You do not have permission to create an event."}, status=403)

    # Extract event data
    required_fields = [
        "competition_type", "participant_type", "event_type",
        "max_teams_or_players", "event_name",
        "event_mode", "start_date", "end_date",
        "registration_open_date", "registration_end_date",
        "prizepool", "number_of_stages", "is_draft"
    ]

    for field in required_fields:
        if field not in request.data:
            return Response({"message": f"Missing field: {field}"}, status=400)

    # Parse dates
    start_date = parse_date(request.data.get("start_date"))
    end_date = parse_date(request.data.get("end_date"))
    open_date = parse_date(request.data.get("registration_open_date"))
    close_date = parse_date(request.data.get("registration_end_date"))

    if open_date > close_date:
        return Response({"message": "Registration open date cannot be after end date."}, status=400)

    if start_date > end_date:
        return Response({"message": "Event start date cannot be after end date."}, status=400)

    # Parse prizepool
    try:
        prizepool = float(request.data.get("prizepool"))
    except:
        return Response({"message": "Prizepool must be a number."}, status=400)

    # Parse prize distribution
    prize_distribution = request.data.get("prize_distribution")
    prize_distribution = json.loads(prize_distribution) if isinstance(prize_distribution, str) else prize_distribution
    if not isinstance(prize_distribution, dict):
        return Response({"message": "Prize distribution must be a JSON object."}, status=400)

    # Create Event
    event = Event.objects.create(
        competition_type=request.data.get("competition_type"),
        participant_type=request.data.get("participant_type"),
        event_type=request.data.get("event_type"),
        max_teams_or_players=request.data.get("max_teams_or_players"),
        event_name=request.data.get("event_name"),
        # format=request.data.get("format"),
        event_mode=request.data.get("event_mode"),
        start_date=start_date,
        end_date=end_date,
        registration_open_date=open_date,
        registration_end_date=close_date,
        prizepool=prizepool,
        prize_distribution=prize_distribution,
        event_rules=request.data.get("event_rules"),
        event_status=request.data.get("event_status", "upcoming"),
        registration_link=request.data.get("registration_link") if "registration_link" in request.data else "",
        tournament_tier=request.data.get("tournament_tier", "tier_3"),
        event_banner=request.FILES.get("event_banner"),
        number_of_stages=request.data.get("number_of_stages"),
        uploaded_rules=request.FILES.get("uploaded_rules"),
        is_draft=request.data.get("is_draft", True)
    )

    # Create stream channels
    stream_channels = request.data.get("stream_channels", [])

    if isinstance(stream_channels, str):
        stream_channels = json.loads(stream_channels)
    for url in stream_channels:
        StreamChannel.objects.create(event=event, channel_url=url)

    # Create stages + groups
    stages_data = request.data.get("stages", [])

    if isinstance(stages_data, str):
        stages_data = json.loads(stages_data)


    for stage_data in stages_data:

        stage = Stages.objects.create(
            event=event,
            stage_name=stage_data["stage_name"],
            start_date=parse_date(stage_data["start_date"]),
            end_date=parse_date(stage_data["end_date"]),
            number_of_groups=stage_data["number_of_groups"],
            stage_format=stage_data["stage_format"],
            teams_qualifying_from_stage=stage_data["teams_qualifying_from_stage"],
            stage_discord_role_id = stage_data["stage_discord_role_id"],
        )

        # Create groups inside this stage
        groups = stage_data.get("groups", [])
        
        for group in groups:
            StageGroups.objects.create(
                stage=stage,
                group_name=group["group_name"],
                playing_date=parse_date(group["playing_date"]),
                playing_time=group["playing_time"],
                teams_qualifying=group["teams_qualifying"],
                group_discord_role_id =  group["group_discord_role_id"],
                match_count = group["match_count"],
                match_maps = group["match_maps"],
            )

            # create matches for the group

            total_number_of_matches_to_be_played = group.get("match_count", 0)
            match_maps = group.get("match_maps", [])

            for match_map in match_maps:
                for match_number in range(1, total_number_of_matches_to_be_played + 1):
                    Match.objects.create(
                        leaderboard=None,
                        group=StageGroups.objects.get(stage=stage, group_name=group["group_name"]),
                        match_map=match_map,
                        match_number=match_number
                    )

    return Response({
        "message": "Event created successfully.",
        "event_id": event.event_id
    }, status=201)


@api_view(["POST"])
def delete_event(request):
    session_token = request.headers.get("Authorization")

    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]

    # Authenticate user
    user = validate_token(token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # Permission check
    if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
        return Response({"message": "You do not have permission to delete an event."}, status=403)

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    try:
        event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)

    event.delete()

    return Response({"message": "Event deleted successfully."}, status=200)

# @api_view(["POST"])
# def edit_event(request):
#     session_token = request.headers.get("Authorization")

#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]

#     # Authenticate user
#     try:
#         user = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     # Permission check
#     if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
#         return Response({"message": "You do not have permission to edit an event."}, status=403)

#     # Event ID needed
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     # Fetch event
#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # Helper function to update only if provided
#     def update_field(field_name, parser=None):
#         if field_name in request.data:
#             value = request.data.get(field_name)
#             if parser:
#                 value = parser(value)
#             setattr(event, field_name, value)

#     # Update simple fields
#     update_field("competition_type")
#     update_field("participant_type")
#     update_field("event_type")
#     update_field("max_teams_or_players")
#     update_field("event_name")
#     update_field("event_mode")
#     update_field("event_status")
#     update_field("registration_link")
#     update_field("tournament_tier")
#     update_field("rules")
#     update_field("event_rules")

#     # Date fields
#     update_field("start_date", parse_date)
#     update_field("end_date", parse_date)
#     update_field("registration_open_date", parse_date)
#     update_field("registration_end_date", parse_date)

#     # Validate dates
#     if event.registration_open_date > event.registration_end_date:
#         return Response({"message": "Registration open date cannot be after registration end date."}, status=400)

#     if event.start_date > event.end_date:
#         return Response({"message": "Event start date cannot be after end date."}, status=400)

#     # Prizepool
#     if "prizepool" in request.data:
#         try:
#             event.prizepool = float(request.data.get("prizepool"))
#         except:
#             return Response({"message": "Prizepool must be a number."}, status=400)

#     # Prize distribution
#     if "prize_distribution" in request.data:
#         prize_distribution = request.data.get("prize_distribution")
#         prize_distribution = json.loads(prize_distribution) if isinstance(prize_distribution, str) else prize_distribution
#         if not isinstance(prize_distribution, dict):
#             return Response({"message": "Prize distribution must be a JSON object."}, status=400)
#         event.prize_distribution = prize_distribution

#     # Update event banner (optional)
#     if "event_banner" in request.FILES:
#         event.event_banner = request.FILES.get("event_banner")

#     # Update number_of_stages if provided
#     if "number_of_stages" in request.data:
#         event.number_of_stages = int(request.data.get("number_of_stages"))

#     event.save()

#     # ============================
#     # STREAM CHANNEL UPDATES
#     # ============================
#     if "stream_channels" in request.data:
#         StreamChannel.objects.filter(event=event).delete()
#         if isinstance(stream_channels, str):
#             stream_channels = json.loads(stream_channels)
#         for url in request.data.get("stream_channels", []):
#             StreamChannel.objects.create(event=event, channel_url=url)

#     # ============================
#     # STAGES + GROUPS UPDATE
#     # ============================
#     if "stages" in request.data:
#         stages_data = request.data["stages"]
#         if isinstance(stages_data, str):
#             stages_data = json.loads(stages_data)

#         # Remove old stages + groups
#         Stages.objects.filter(event=event).delete()

#         for stage_data in stages_data:
#             stage = Stages.objects.create(
#                 event=event,
#                 stage_name=stage_data["stage_name"],
#                 start_date=parse_date(stage_data["start_date"]),
#                 end_date=parse_date(stage_data["end_date"]),
#                 number_of_groups=stage_data["number_of_groups"],
#                 stage_format=stage_data["stage_format"],
#                 teams_qualifying_from_stage=stage_data["teams_qualifying_from_stage"]
#             )

#             # Add groups under this stage
#             for group in stage_data.get("groups", []):
#                 StageGroups.objects.create(
#                     stage=stage,
#                     group_name=group["group_name"],
#                     playing_date=parse_date(group["playing_date"]),
#                     playing_time=group["playing_time"],
#                     teams_qualifying=group["teams_qualifying"]
#                 )

#     return Response({
#         "message": "Event updated successfully.",
#         "event_id": event.event_id
#     }, status=200)





@api_view(["POST"])
def edit_event(request):
    session_token = request.headers.get("Authorization")

    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]

    # Authenticate user
    user = validate_token(token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # Permission check
    if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
        return Response({"message": "You do not have permission to edit an event."}, status=403)

    # Event ID needed
    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    # Fetch event
    try:
        event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)

    # Helper function to update only if provided
    def update_field(field_name, parser=None):
        if field_name in request.data:
            value = request.data.get(field_name)
            if parser:
                value = parser(value)
            setattr(event, field_name, value)

    # Update simple fields
    for field in [
        "competition_type", "participant_type", "event_type",
        "max_teams_or_players", "event_name", "event_mode",
        "event_status", "registration_link", "tournament_tier",
        "rules", "event_rules"
    ]:
        update_field(field)

    # Date fields
    for date_field in [
        "start_date", "end_date",
        "registration_open_date", "registration_end_date"
    ]:
        update_field(date_field, parse_date)

    # Date validation
    if event.registration_open_date and event.registration_end_date:
        if event.registration_open_date > event.registration_end_date:
            return Response({"message": "Registration open date cannot be after registration end date."}, status=400)

    if event.start_date and event.end_date:
        if event.start_date > event.end_date:
            return Response({"message": "Event start date cannot be after end date."}, status=400)

    # Prizepool
    if "prizepool" in request.data:
        try:
            event.prizepool = float(request.data.get("prizepool"))
        except:
            return Response({"message": "Prizepool must be a number."}, status=400)

    # Prize distribution
    if "prize_distribution" in request.data:
        prize_distribution = request.data.get("prize_distribution")
        if isinstance(prize_distribution, str):
            prize_distribution = json.loads(prize_distribution)
        if not isinstance(prize_distribution, dict):
            return Response({"message": "Prize distribution must be a JSON object."}, status=400)
        event.prize_distribution = prize_distribution

    # Banner
    if "event_banner" in request.FILES:
        event.event_banner = request.FILES.get("event_banner")

    # Uploaded Rules
    if "uploaded_rules" in request.FILES:
        event.uploaded_rules = request.FILES.get("uploaded_rules")

    # Number of stages
    if "number_of_stages" in request.data:
        event.number_of_stages = int(request.data.get("number_of_stages"))

    event.save()

    # ============================
    # STREAM CHANNEL UPDATES
    # ============================
    if "stream_channels" in request.data:
        StreamChannel.objects.filter(event=event).delete()

        stream_channels = request.data.get("stream_channels")

        # Parse JSON if string
        if isinstance(stream_channels, str):
            stream_channels = json.loads(stream_channels)

        if isinstance(stream_channels, list):
            for url in stream_channels:
                StreamChannel.objects.create(event=event, channel_url=url)

    # ============================
    # STAGES + GROUPS UPDATE
    # ============================
    if "stages" in request.data:
        stages_data = request.data.get("stages")

        if isinstance(stages_data, str):
            stages_data = json.loads(stages_data)


        # Recreate stages + groups
        for stage_data in stages_data:
            stage, created = Stages.objects.update_or_create(
                event=event,
                stage_id=stage_data.get("stage_id"),  # use existing ID if provided
                defaults={
                    "stage_name": stage_data["stage_name"],
                    "start_date": parse_date(stage_data["start_date"]),
                    "end_date": parse_date(stage_data["end_date"]),
                    "number_of_groups": stage_data["number_of_groups"],
                    "stage_format": stage_data["stage_format"],
                    "teams_qualifying_from_stage": stage_data["teams_qualifying_from_stage"],
                    "stage_discord_role_id": stage_data.get("stage_discord_role_id")
                }
            )

            # Groups
            for group_data in stage_data.get("groups", []):
                group, created = StageGroups.objects.update_or_create(
                    stage=stage,
                    group_id=group_data.get("group_id"),  # use existing ID if provided
                    defaults={
                        "group_name": group_data["group_name"],
                        "playing_date": parse_date(group_data["playing_date"]),
                        "playing_time": group_data["playing_time"],
                        "teams_qualifying": group_data["teams_qualifying"],
                        "group_discord_role_id": group_data.get("group_discord_role_id"),
                        "match_count": group_data.get("match_count"),
                        "match_maps": group_data.get("match_maps"),
                    }
                )

                # Matches
                total_matches = group_data.get("match_count", 0)
                match_maps = group_data.get("match_maps", [])
                for match_map in match_maps:
                    for match_number in range(1, total_matches + 1):
                        Match.objects.update_or_create(
                            group=group,
                            match_number=match_number,
                            match_map=match_map,
                            defaults={"leaderboard": None}
                        )

    return Response({
        "message": "Event updated successfully.",
        "event_id": event.event_id
    }, status=200)


@api_view(["GET"])
def get_total_events_count(request):
    total_events = Event.objects.count(is_draft=False)
    return Response({"total_events": total_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_total_tournaments_count(request):
    total_tournaments = Event.objects.filter(competition_type="tournament", is_draft=False).count()
    return Response({"total_tournaments": total_tournaments}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_total_scrims_count(request):
    total_scrims = Event.objects.filter(competition_type="scrim", is_draft=False).count()
    return Response({"total_scrims": total_scrims}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_upcoming_events_count(request):
    upcoming_events = Event.objects.filter(event_status="upcoming", is_draft=False).count()
    return Response({"upcoming_events": upcoming_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_ongoing_events_count(request):
    ongoing_events = Event.objects.filter(event_status="ongoing", is_draft=False).count()
    return Response({"ongoing_events": ongoing_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_completed_events_count(request):
    completed_events = Event.objects.filter(event_status="completed", is_draft=False).count()
    return Response({"completed_events": completed_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_average_participants_per_event(request):
    events = Event.objects.all()
    total_participants = 0
    event_count = events.count()

    if event_count == 0:
        return Response({"average_participants": 0}, status=status.HTTP_200_OK)

    for event in events:
        total_participants += event.max_teams_or_players

    average_participants = total_participants / event_count
    return Response({"average_participants": average_participants}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_most_popular_event_format(request):
    format_counts = {}
    events = Event.objects.all()

    for event in events:
        fmt = event.format
        if fmt in format_counts:
            format_counts[fmt] += 1
        else:
            format_counts[fmt] = 1

    if not format_counts:
        return Response({"most_popular_format": None}, status=status.HTTP_200_OK)

    most_popular_format = max(format_counts, key=format_counts.get)
    return Response({"most_popular_format": most_popular_format}, status=status.HTTP_200_OK)


# @api_view(["POST"])
# def get_event_details(request):
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = (
#             Event.objects
#             .prefetch_related(
#                 "streamchannel_set",
#                 "stages_set__stagegroups_set"
#             )
#             .get(event_id=event_id)
#         )
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # Base Event Data
#     event_data = {
#         "event_id": event.event_id,
#         "competition_type": event.competition_type,
#         "participant_type": event.participant_type,
#         "event_type": event.event_type,
#         "max_teams_or_players": event.max_teams_or_players,
#         "event_name": event.event_name,
#         "event_mode": event.event_mode,
#         "start_date": event.start_date,
#         "end_date": event.end_date,
#         "registration_open_date": event.registration_open_date,
#         "registration_end_date": event.registration_end_date,
#         "prizepool": event.prizepool,
#         "prize_distribution": event.prize_distribution,
#         "event_rules": event.event_rules,
#         "event_status": event.event_status,
#         "registration_link": event.registration_link,
#         "tournament_tier": event.tournament_tier,
#         "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
#         "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
#         "number_of_stages": event.number_of_stages,
#         "created_at": event.created_at,
#     }

#     # Stream Channels
#     event_data["stream_channels"] = [
#         channel.channel_url
#         for channel in event.streamchannel_set.all()
#     ]

#     # Stages + Groups
#     stages = event.stages_set.all().order_by("start_date")
#     stage_list = []

#     for stage in stages:
#         groups = stage.stagegroups_set.all().order_by("group_name")
#         group_list = [{
#             "id": group.group_id,
#             "group_name": group.group_name,
#             "playing_date": group.playing_date,
#             "playing_time": group.playing_time,
#             "teams_qualifying": group.teams_qualifying,
#         } for group in groups]

#         stage_list.append({
#             "id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "stage_format": stage.stage_format,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": group_list,
#         })

#     event_data["stages"] = stage_list

#     return Response({"event_details": event_data}, status=200)

# @api_view(["POST"])
# def get_event_details(request):
#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.prefetch_related(
#             "stream_channels",
#             "stages__groups__leaderboards__matches__team_stats__player_stats"
#         ).get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # Base Event Data
#     event_data = {
#         "event_id": event.event_id,
#         "competition_type": event.competition_type,
#         "participant_type": event.participant_type,
#         "event_type": event.event_type,
#         "max_teams_or_players": event.max_teams_or_players,
#         "event_name": event.event_name,
#         "event_mode": event.event_mode,
#         "start_date": event.start_date,
#         "end_date": event.end_date,
#         "registration_open_date": event.registration_open_date,
#         "registration_end_date": event.registration_end_date,
#         "prizepool": event.prizepool,
#         "prize_distribution": event.prize_distribution,
#         "event_rules": event.event_rules,
#         "event_status": event.event_status,
#         "registration_link": event.registration_link,
#         "tournament_tier": event.tournament_tier,
#         "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
#         "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
#         "number_of_stages": event.number_of_stages,
#         "created_at": event.created_at,
#     }

#     # Stream Channels
#     event_data["stream_channels"] = [
#         channel.channel_url for channel in event.stream_channels.all()
#     ]

#     # Stages + Groups + Match Stats
#     stage_list = []
#     for stage in event.stages.all().order_by("start_date"):
#         group_list = []
#         for group in stage.groups.all().order_by("group_name"):
#             matches_data = []
#             for lb in group.leaderboards.all():
#                 for match in lb.matches.all():
#                     team_stats_data = []
#                     for team_stat in match.team_stats.all():
#                         player_stats_data = [{
#                             "player_id": ps.player.id,
#                             "username": ps.player.username,
#                             "kills": ps.kills,
#                             "damage": ps.damage
#                         } for ps in team_stat.player_stats.all()]

#                         team_stats_data.append({
#                             "team_id": team_stat.team.team_id,
#                             "team_name": team_stat.team.team_name,
#                             "placement": team_stat.placement,
#                             "players": player_stats_data
#                         })

#                     matches_data.append({
#                         "match_id": match.match_id,
#                         "map": match.map,
#                         "mvp": match.mvp.username,
#                         "teams": team_stats_data
#                     })

#             group_list.append({
#                 "id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "matches": matches_data
#             })

#         stage_list.append({
#             "id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "stage_format": stage.stage_format,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": group_list
#         })

#     event_data["stages"] = stage_list

#     return Response({"event_details": event_data}, status=200)


@api_view(["POST"])
def get_event_details(request):
    session_token = request.headers.get("Authorization")

    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]

    # Authenticate user
    user = validate_token(token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    try:
        event = (
            Event.objects.prefetch_related(
                "stream_channels",
                "stages__groups__leaderboards__matches__team_stats__player_stats",

                # Registered competitors (solo or team)
                "registrations__user",
                "registrations__team__teammembers__member",

                # Tournament teams + members
                "tournament_teams__team__teammembers__member",
                "tournament_teams__members__user",
            )
            .get(event_id=event_id)
        )
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=404)
    
    # check if user is registered for the event
    is_registered = False
    if event.participant_type == "solo":
        is_registered = RegisteredCompetitors.objects.filter(user=user).exists()


    # Base Event Data
    event_data = {
        "event_id": event.event_id,
        "competition_type": event.competition_type,
        "participant_type": event.participant_type,
        "event_type": event.event_type,
        "max_teams_or_players": event.max_teams_or_players,
        "event_name": event.event_name,
        "event_mode": event.event_mode,
        "start_date": event.start_date,
        "end_date": event.end_date,
        "registration_open_date": event.registration_open_date,
        "registration_end_date": event.registration_end_date,
        "prizepool": event.prizepool,
        "prize_distribution": event.prize_distribution,
        "event_rules": event.event_rules,
        "event_status": event.event_status,
        "registration_link": event.registration_link,
        "tournament_tier": event.tournament_tier,
        "event_banner_url": request.build_absolute_uri(event.event_banner.url) if event.event_banner else None,
        "uploaded_rules_url": request.build_absolute_uri(event.uploaded_rules.url) if event.uploaded_rules else None,
        "number_of_stages": event.number_of_stages,
        "created_at": event.created_at,
        "is_registered": is_registered
    }

    # Stream channels
    event_data["stream_channels"] = [
        ch.channel_url for ch in event.stream_channels.all()
    ]

    # Registered Competitors (for event registration)
    registered = []
    if event.participant_type == "solo":
        for reg in event.registrations.all():
            if reg.user:
                registered.append({
                    "player_id": reg.user.user_id,
                    "username": reg.user.username,
                    "status": reg.status
                })
    else:  # duo or squad
        for reg in event.registrations.all():
            if reg.team:
                members = reg.team.teammembers.all()
                registered.append({
                    "team_id": reg.team.team_id,
                    "team_name": reg.team.team_name,
                    "status": "registered",
                    "members": [
                        {
                            "player_id": m.member.id,
                            "username": m.member.username,
                            "role": m.in_game_role
                        }
                        for m in members
                    ]
                })

    event_data["registered_competitors"] = registered

    # Tournament Teams (official accepted teams)
    tournament_teams_list = []
    for tt in event.tournament_teams.all():
        members = tt.members.all()
        tournament_teams_list.append({
            "tournament_team_id": tt.tournament_team_id,
            "team_id": tt.team.team_id,
            "team_name": tt.team.team_name,
            "members": [
                {
                    "player_id": m.user.id,
                    "username": m.user.username
                }
                for m in members
            ]
        })

    event_data["tournament_teams"] = tournament_teams_list

    # Stages, Groups, Matches
    stage_list = []
    for stage in event.stages.all().order_by("start_date"):
        group_list = []
        for group in stage.groups.all().order_by("group_name"):
            matches_data = []

            for lb in group.leaderboards.all():
                for match in lb.matches.all():
                    team_stats_data = []
                    for team_stat in match.team_stats.all():

                        player_stats_data = [
                            {
                                "player_id": ps.player.id,
                                "username": ps.player.username,
                                "kills": ps.kills,
                                "damage": ps.damage
                            }
                            for ps in team_stat.player_stats.all()
                        ]

                        team_stats_data.append({
                            "tournament_team_id": team_stat.tournament_team.tournament_team_id,
                            "team_name": team_stat.tournament_team.team.team_name,
                            "placement": team_stat.placement,
                            "players": player_stats_data
                        })

                    matches_data.append({
                        "match_id": match.match_id,
                        "map_name": match.match_map,
                        "mvp": match.mvp.username if match.mvp else None,
                        "teams": team_stats_data
                    })

            group_list.append({
                "id": group.group_id,
                "group_name": group.group_name,
                "playing_date": group.playing_date,
                "playing_time": group.playing_time,
                "teams_qualifying": group.teams_qualifying,
                "matches": matches_data,
                "match_count": group.match_count,
                "match_maps": group.match_maps,
            })

        stage_list.append({
            "id": stage.stage_id,
            "stage_name": stage.stage_name,
            "start_date": stage.start_date,
            "end_date": stage.end_date,
            "number_of_groups": stage.number_of_groups,
            "stage_format": stage.stage_format,
            "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
            "groups": group_list
        })

    event_data["stages"] = stage_list

    return Response({"event_details": event_data}, status=200)



@api_view(["POST"])
def register_for_event(request):
    # -------------------------
    # 1. GET USER FROM SESSION TOKEN
    # -------------------------
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=400)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=400)

    session_token = session_token.split(" ")[1]

    # Identify logged-in user
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # -------------------------
    # 2. GET EVENT & TEAM
    # -------------------------
    event_id = request.data.get("event_id")
    team_id = request.data.get("team_id")  # only for team events

    if not event_id:
        return Response({"message": "event_id is required"}, status=400)

    # Fetch event
    try:
        event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found"}, status=404)

    participant_type = event.participant_type  # solo, duo, squad

    # -------------------------
    # 3. CHECK REGISTRATION WINDOW
    # -------------------------
    today = date.today()
    if not (event.registration_open_date <= today <= event.registration_end_date):
        return Response({"message": "Registration is closed."}, status=403)

    # ======================================================
    #                    SOLO REGISTRATION
    # ======================================================
    if participant_type == "solo":

        # Discord must be connected
        if not user.discord_connected:
            return Response({"message": "Connect your Discord account first."}, status=403)

        # Must join the Discord server
        if not check_discord_membership(user.discord_id):
            return Response({"message": "You must join the Discord server before registering."}, status=403)

        # Prevent duplicate registration
        if RegisteredCompetitors.objects.filter(event=event, user=user).exists():
            return Response({"message": "You are already registered."}, status=409)

        # Check event capacity
        if RegisteredCompetitors.objects.filter(event=event).count() >= event.max_teams_or_players:
            return Response({"message": "Registration limit reached."}, status=403)

        # Register user
        competitor = RegisteredCompetitors.objects.create(event=event, user=user)

        # Assign Discord role
        assign_discord_role(user.discord_id, settings.DISCORD_TOURNAMENT_DETTY_SOLOS_ROLE_ID)

        return Response({
            "message": "Successfully registered.",
            "registration_id": competitor.id
        }, status=201)

    # ======================================================
    #                 TEAM (DUO / SQUAD) REGISTRATION
    # ======================================================
    if participant_type in ["duo", "squad"]:

        if not team_id:
            return Response({"message": "team_id is required."}, status=400)

        try:
            team = Team.objects.get(team_id=team_id)
        except Team.DoesNotExist:
            return Response({"message": "Team not found"}, status=404)

        # The user must be part of the team
        if not TeamMembers.objects.filter(team=team, user=user).exists():
            return Response({"message": "You are not a member of this team."}, status=403)

        # Prevent duplicate team registration
        if RegisteredCompetitors.objects.filter(event=event, team=team).exists():
            return Response({"message": "Team already registered."}, status=409)

        # Check event capacity
        if RegisteredCompetitors.objects.filter(event=event).count() >= event.max_teams_or_players:
            return Response({"message": "Registration limit reached."}, status=403)

        # Validate Discord for all team members
        members = TeamMembers.objects.filter(team=team)

        for m in members:
            if not m.user.discord_connected:
                return Response({
                    "message": f"{m.user.username} has not connected Discord.",
                    "user_id": m.user.id
                }, status=403)

            if not check_discord_membership(m.user.discord_id):
                return Response({
                    "message": f"{m.user.username} has not joined the Discord server.",
                    "user_id": m.user.id
                }, status=403)

        # Register the team
        competitor = RegisteredCompetitors.objects.create(event=event, team=team)

        # Assign roles to all members
        for m in members:
            assign_discord_role(m.user.discord_id, settings.DISCORD_TOURNAMENT_ROLE_ID)

        return Response({
            "message": "Team successfully registered.",
            "registration_id": competitor.id
        }, status=201)

    return Response({"message": "Invalid event participant type."}, status=400)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # --- auth + basic checks ---
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     # TODO: optionally check admin role here
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)
#     # if not admin.is_staff: return 403 etc.

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     # --- Overview metrics ---
#     # total registered competitors (only count active registrations)
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     # expected max competitors
#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0.0

#     # days until start
#     today = timezone.localdate()
#     days_until_start = (event.start_date - today).days if event.start_date else None
#     # event duration in days (inclusive)
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None

#     # registrations close date and days left
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     # average registered competitors per day (since registration opened)
#     try:
#         reg_open = event.registration_open_date
#         if reg_open:
#             days_since_open = max(1, (today - reg_open).days + 1)
#             avg_reg_per_day = round(total_registered / days_since_open, 2)
#         else:
#             avg_reg_per_day = 0.0
#     except Exception:
#         avg_reg_per_day = 0.0

#     # prizepool (string in model) → try numeric
#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # --- registration timeline & statistics ---
#     # registration window days and days left
#     registration_window_days = (event.registration_end_date - event.registration_open_date).days + 1 if event.registration_open_date and event.registration_end_date else None

#     # registration counts per day (for peak registration)
#     reg_by_day = (
#         reg_qs
#         .annotate(reg_date=TruncDate("registration_date"))
#         .values("reg_date")
#         .annotate(count=Count("id"))
#         .order_by("-count")
#     )
#     peak_registration = reg_by_day[0]["count"] if reg_by_day else 0
#     # prepare timeseries (optional) - last 30 days
#     timeseries = []
#     # we can build full timeseries between open and now
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         while current <= end_ts:
#             day_count = next((r["count"] for r in reg_by_day if r["reg_date"] == current), 0)
#             timeseries.append({"date": current, "count": day_count})
#             current = current + timedelta(days=1)

#     # --- team status counts (for squad tournaments using platform Team/TournamentTeam) ---
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # --- stage progress ---
#     total_stages = event.stages.count()
#     # define stage status: completed if end_date < today, ongoing if start_date <= today <= end_date, upcoming if start_date > today
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # --- registration section (restate some metrics) ---
#     registration_rate_pct = registration_percentage
#     avg_registration_per_day = avg_reg_per_day

#     # --- stages detail (groups & team counts) ---
#     stages_data = []
#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0
#         for group in groups:
#             # compute total teams in this group via leaderboards / registered teams in leaderboards or via TournamentTeam entries
#             # We'll assume teams in group are those in leaderboards -> or you can count registered tournament_teams assigned to that stage via leaderboards
#             teams_in_group = 0
#             # try using leaderboards -> teams recorded in matches/stats:
#             # count distinct tournament_team referenced in TournamentTeamMatchStats for matches in this group
#             leaderboards = group.leaderboards.all()

#             matches_qs = Match.objects.filter(
#             leaderboard__group=group
#             )


#             teams_in_group = (
#                 TournamentTeamMatchStats.objects
#                 .filter(match__in=matches_qs)
#                 .values("tournament_team")
#                 .distinct()
#                 .count()
#             )
#             # fallback: if none, use how many teams exist in tournament_teams (approx)
#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details,
#         })

#     # --- engagement metrics ---
#     pageviews = event.pageviews.count()
#     # unique visitors: based on unique user id if present, otherwise unique ip
#     unique_visitors_by_user = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_visitors_by_ip = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_visitors_by_user + unique_visitors_by_ip

#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0.0

#     social_shares = event.social_shares.count()

#     # stream links
#     streams = [ch.channel_url for ch in event.stream_channels.all()]

#     # --- Final payload ---
#     payload = {
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": [
#                 {"date": str(item["date"]), "count": item["count"]} for item in timeseries
#             ],
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "registration_stats": {
#             "registration_rate_pct": registration_rate_pct,
#             "average_registration_per_day": avg_registration_per_day,
#             "peak_registration": peak_registration
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }

#     return Response(payload, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (
#         (event.end_date - event.start_date).days + 1
#         if event.start_date and event.end_date else None
#     )

#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (
#         (registration_close_date - today).days if registration_close_date else None
#     )

#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)
#     else:
#         avg_reg_per_day = 0

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )

#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}

#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []

#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             matches_qs = Match.objects.filter(
#                 leaderboard__group=group
#             )

#             teams_in_group = (
#                 TournamentTeamMatchStats.objects
#                 .filter(match__in=matches_qs)
#                 .values("tournament_team")
#                 .distinct()
#                 .count()
#             )

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()

#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips

#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()

#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     avg_reg_per_day = 0
#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )
#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}

#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []

#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             # Get all leaderboards for this group
#             leaderboards_qs = group.leaderboards.all()

#             # Get all matches under these leaderboards
#             matches_qs = Match.objects.filter(leaderboard__in=leaderboards_qs)

#             # Count distinct teams in this group
#             teams_in_group = (
#                 TournamentTeamMatchStats.objects
#                 .filter(match__in=matches_qs)
#                 .values("tournament_team")
#                 .distinct()
#                 .count()
#             )

#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()
#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips
#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()
#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     try:
#         admin = User.objects.get(session_token=token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.prefetch_related(
#             "stages__groups__leaderboards__matches__team_stats"
#         ).get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()

#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0

#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     avg_reg_per_day = 0
#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )
#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}

#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []

#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             # Get all leaderboards for this group
#             leaderboards = group.leaderboards.all()
#             matches = Match.objects.filter(leaderboard__in=leaderboards)

#             # Count distinct teams in matches
#             teams_in_group = TournamentTeamMatchStats.objects.filter(
#                 match__in=matches
#             ).values("tournament_team").distinct().count()

#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()
#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips
#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()
#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)


# @api_view(["POST"])
# def get_event_details_for_admin(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]

#     admin = validate_token(token)
#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to access this data."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     try:
#         event = Event.objects.get(event_id=event_id)
#     except Event.DoesNotExist:
#         return Response({"message": "Event not found."}, status=404)

#     today = timezone.localdate()

#     # ---------------- OVERVIEW ----------------
#     reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
#     total_registered = reg_qs.count()
#     max_competitors = event.max_teams_or_players or 0
#     registration_percentage = round((total_registered / max_competitors) * 100, 2) if max_competitors else 0
#     days_until_start = (event.start_date - today).days if event.start_date else None
#     event_duration_days = (event.end_date - event.start_date).days + 1 if event.start_date and event.end_date else None
#     registration_close_date = event.registration_end_date
#     days_until_registration_close = (registration_close_date - today).days if registration_close_date else None

#     if event.registration_open_date:
#         days_since_open = max(1, (today - event.registration_open_date).days + 1)
#         avg_reg_per_day = round(total_registered / days_since_open, 2)
#     else:
#         avg_reg_per_day = 0

#     try:
#         prizepool_val = float(event.prizepool)
#     except Exception:
#         prizepool_val = event.prizepool

#     # ---------------- REGISTRATION TIMELINE ----------------
#     registration_window_days = (
#         (event.registration_end_date - event.registration_open_date).days + 1
#         if event.registration_open_date and event.registration_end_date else None
#     )

#     reg_by_day = (
#         reg_qs
#         .annotate(day=TruncDate("registration_date"))
#         .values("day")
#         .annotate(count=Count("id"))
#     )
#     peak_registration = max([r["count"] for r in reg_by_day], default=0)

#     timeseries = []
#     if event.registration_open_date:
#         current = event.registration_open_date
#         end_ts = min(event.registration_end_date or today, today)
#         reg_map = {r["day"]: r["count"] for r in reg_by_day}
#         while current <= end_ts:
#             timeseries.append({
#                 "date": str(current),
#                 "count": reg_map.get(current, 0)
#             })
#             current += timedelta(days=1)

    
#     # Recent Registrations (last 5)
#     recent_registrations = (
#         reg_qs
#         .order_by("-registration_date")[:5]
#         .values("competitor_name", "registration_date", "status")
#     )

#     # ---------------- TEAM STATUS ----------------
#     active_teams = event.tournament_teams.filter(status="active").count()
#     disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
#     withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

#     # ---------------- STAGE PROGRESS ----------------
#     total_stages = event.stages.count()
#     completed_stages = event.stages.filter(end_date__lt=today).count()
#     ongoing_stages = event.stages.filter(start_date__lte=today, end_date__gte=today).count()
#     upcoming_stages = event.stages.filter(start_date__gt=today).count()

#     # ---------------- STAGES DETAIL ----------------
#     stages_data = []
#     for stage in event.stages.all().order_by("start_date"):
#         groups = stage.groups.all()
#         group_details = []
#         total_teams_in_stage = 0

#         for group in groups:
#             teams_in_group = 0
#             # iterate over leaderboards manually
#             for leaderboard in group.leaderboards.all():
#                 for match in leaderboard.matches.all():
#                     teams_in_group += TournamentTeamMatchStats.objects.filter(match=match).values("tournament_team").distinct().count()

#             if teams_in_group == 0:
#                 teams_in_group = event.tournament_teams.count()

#             total_teams_in_stage += teams_in_group

#             group_details.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "total_teams_in_group": teams_in_group
#             })

#         stages_data.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "number_of_groups": stage.number_of_groups,
#             "total_groups": groups.count(),
#             "total_teams_in_stage": total_teams_in_stage,
#             "groups": group_details
#         })

#     # ---------------- ENGAGEMENT ----------------
#     pageviews = event.pageviews.count()
#     unique_users = event.pageviews.filter(user__isnull=False).values("user").distinct().count()
#     unique_ips = event.pageviews.filter(user__isnull=True).values("ip_address").distinct().count()
#     unique_visitors = unique_users + unique_ips
#     conversion_rate = round((total_registered / unique_visitors) * 100, 2) if unique_visitors else 0
#     social_shares = event.social_shares.count()
#     streams = list(event.stream_channels.values_list("channel_url", flat=True))

#     # ---------------- RESPONSE ----------------
#     return Response({
#         "overview": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "total_registered": total_registered,
#             "max_competitors": max_competitors,
#             "registration_percentage": registration_percentage,
#             "days_until_start": days_until_start,
#             "event_duration_days": event_duration_days,
#             "registration_close_date": registration_close_date,
#             "days_until_registration_close": days_until_registration_close,
#             "average_registrations_per_day": avg_reg_per_day,
#             "prizepool": prizepool_val,
#         },
#         "registration_timeline": {
#             "registration_start_date": event.registration_open_date,
#             "registration_end_date": event.registration_end_date,
#             "registration_window_days": registration_window_days,
#             "days_left_for_registration": days_until_registration_close,
#             "registration_timeseries": timeseries,
#             "peak_registration": peak_registration,
#             "recent_registrations": list(recent_registrations)
#         },
#         "team_status": {
#             "active": active_teams,
#             "disqualified": disqualified_teams,
#             "withdrawn": withdrawn_teams
#         },
#         "stage_progress": {
#             "total_stages": total_stages,
#             "completed": completed_stages,
#             "ongoing": ongoing_stages,
#             "upcoming": upcoming_stages
#         },
#         "stages": stages_data,
#         "engagement": {
#             "pageviews": pageviews,
#             "unique_visitors": unique_visitors,
#             "conversion_rate": conversion_rate,
#             "social_shares": social_shares,
#             "stream_links": streams
#         }
#     }, status=200)

from django.utils import timezone
from django.db.models import Count, Case, When, Value, CharField, F
from django.db.models.functions import TruncDate
from datetime import timedelta
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

@api_view(["POST"])
def get_event_details_for_admin(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = session_token.split(" ")[1]
    admin = validate_token(token)

    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    if admin.role != "admin":
        return Response(
            {"message": "You do not have permission to access this data."},
            status=status.HTTP_403_FORBIDDEN
        )

    # ---------------- EVENT ----------------
    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    today = timezone.localdate()

    # ---------------- OVERVIEW ----------------
    reg_qs = RegisteredCompetitors.objects.filter(event=event, status="registered")
    total_registered = reg_qs.count()

    max_competitors = event.max_teams_or_players or 0
    registration_percentage = (
        round((total_registered / max_competitors) * 100, 2)
        if max_competitors else 0
    )

    days_until_start = (
        (event.start_date - today).days
        if event.start_date else None
    )

    event_duration_days = (
        (event.end_date - event.start_date).days + 1
        if event.start_date and event.end_date else None
    )

    registration_close_date = event.registration_end_date
    days_until_registration_close = (
        (registration_close_date - today).days
        if registration_close_date else None
    )

    if event.registration_open_date:
        days_since_open = max(1, (today - event.registration_open_date).days + 1)
        avg_reg_per_day = round(total_registered / days_since_open, 2)
    else:
        avg_reg_per_day = 0

    try:
        prizepool_val = float(event.prizepool)
    except Exception:
        prizepool_val = event.prizepool

    # ---------------- REGISTRATION TIMELINE ----------------
    registration_window_days = (
        (event.registration_end_date - event.registration_open_date).days + 1
        if event.registration_open_date and event.registration_end_date else None
    )

    reg_by_day = (
        reg_qs
        .annotate(day=TruncDate("registration_date"))
        .values("day")
        .annotate(count=Count("id"))
    )

    peak_registration = max(
        [r["count"] for r in reg_by_day],
        default=0
    )

    timeseries = []
    if event.registration_open_date:
        current = event.registration_open_date
        end_ts = min(event.registration_end_date or today, today)
        reg_map = {r["day"]: r["count"] for r in reg_by_day}

        while current <= end_ts:
            timeseries.append({
                "date": str(current),
                "count": reg_map.get(current, 0)
            })
            current += timedelta(days=1)

    # ---------------- RECENT REGISTRATIONS (FIXED) ----------------
    recent_registrations = (
        reg_qs
        .annotate(
            competitor_name=Case(
                When(user__isnull=False, then=F("user__username")),
                When(team__isnull=False, then=F("team__team_name")),
                default=Value("Unknown"),
                output_field=CharField()
            )
        )
        .order_by("-registration_date")[:5]
        .values("competitor_name", "registration_date", "status")
    )

    # ---------------- All REGISTRATIONS (ADDED) ----------------
    all_registrations = (
        reg_qs
        .annotate(
            competitor_name=Case(
                When(user__isnull=False, then=F("user__username")),
                When(team__isnull=False, then=F("team__team_name")),
                default=Value("Unknown"),
                output_field=CharField()
            )
        )
        .order_by("-registration_date")
        .values("competitor_name", "registration_date", "status")
    )

    # ---------------- TEAM STATUS ----------------
    active_teams = event.tournament_teams.filter(status="active").count()
    disqualified_teams = event.tournament_teams.filter(status="disqualified").count()
    withdrawn_teams = event.tournament_teams.filter(status="withdrawn").count()

    # ---------------- STAGE PROGRESS ----------------
    total_stages = event.stages.count()
    completed_stages = event.stages.filter(end_date__lt=today).count()
    ongoing_stages = event.stages.filter(
        start_date__lte=today,
        end_date__gte=today
    ).count()
    upcoming_stages = event.stages.filter(start_date__gt=today).count()

    # ---------------- STAGES DETAIL ----------------
    stages_data = []

    for stage in event.stages.all().order_by("start_date"):
        groups = stage.groups.all()
        group_details = []
        total_teams_in_stage = 0

        for group in groups:
            teams_in_group = 0

            for leaderboard in group.leaderboards.all():
                for match in leaderboard.matches.all():
                    teams_in_group += (
                        TournamentTeamMatchStats.objects
                        .filter(match=match)
                        .values("tournament_team")
                        .distinct()
                        .count()
                    )

            if teams_in_group == 0:
                teams_in_group = event.tournament_teams.count()

            total_teams_in_stage += teams_in_group

            group_matches = list(
                group.matches.order_by("match_number").values(
                    "match_id",
                    "match_number",
                    "match_map",
                    "room_id",
                    "room_name",
                    "room_password",
                    "result_inputted",
                    "match_date",
                ))

            group_details.append({
                "group_id": group.group_id,
                "group_name": group.group_name,
                "playing_date": group.playing_date,
                "playing_time": group.playing_time,
                "teams_qualifying": group.teams_qualifying,
                "total_teams_in_group": teams_in_group,
                "group_discord_role_id": group.group_discord_role_id,
                "match_count": group.match_count,
                "match_maps": group.match_maps,
                "matches": group_matches,

                # get stage group competitor from stagegroupcompetitor model
                "competitors_in_group": list(
                    StageGroupCompetitor.objects.filter(
                        stage_group=group,
                        player__isnull=False
                    ).values_list(
                        "player__user__username",
                        flat=True
                    )
                )
            })
            
           

        stages_data.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "start_date": stage.start_date,
            "end_date": stage.end_date,
            "number_of_groups": stage.number_of_groups,
            "total_groups": groups.count(),
            "total_teams_in_stage": total_teams_in_stage,
            "stage_discord_role_id": stage.stage_discord_role_id,
            "groups": group_details,
            "stage_format": stage.stage_format,
            "stage_status": stage.stage_status,
            "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
            "competitors_in_stage": list(
                StageCompetitor.objects.filter(
                    stage=stage,
                    player__isnull=False
                ).values_list(
                    "player__user__username",
                    flat=True
                )
            )

        })

    # ---------------- ENGAGEMENT ----------------
    pageviews = event.pageviews.count()

    unique_users = (
        event.pageviews
        .filter(user__isnull=False)
        .values("user")
        .distinct()
        .count()
    )

    unique_ips = (
        event.pageviews
        .filter(user__isnull=True)
        .values("ip_address")
        .distinct()
        .count()
    )

    unique_visitors = unique_users + unique_ips

    conversion_rate = (
        round((total_registered / unique_visitors) * 100, 2)
        if unique_visitors else 0
    )

    social_shares = event.social_shares.count()
    streams = list(
        event.stream_channels.values_list("channel_url", flat=True)
    )

    # ---------------- RESPONSE ----------------
    return Response({
        "overview": {
            "event_id": event.event_id,
            "event_name": event.event_name,
            "total_registered": total_registered,
            "max_competitors": max_competitors,
            "registration_percentage": registration_percentage,
            "days_until_start": days_until_start,
            "event_duration_days": event_duration_days,
            "registration_close_date": registration_close_date,
            "days_until_registration_close": days_until_registration_close,
            "average_registrations_per_day": avg_reg_per_day,
            "prizepool": prizepool_val,
            "prize_distribution": event.prize_distribution,
        },
        "registration_timeline": {
            "registration_start_date": event.registration_open_date,
            "registration_end_date": event.registration_end_date,
            "registration_window_days": registration_window_days,
            "days_left_for_registration": days_until_registration_close,
            "registration_timeseries": timeseries,
            "peak_registration": peak_registration,
            "recent_registrations": list(recent_registrations),
            "all_registrations": list(all_registrations),
        },
        "team_status": {
            "active": active_teams,
            "disqualified": disqualified_teams,
            "withdrawn": withdrawn_teams,
        },
        "stage_progress": {
            "total_stages": total_stages,
            "completed": completed_stages,
            "ongoing": ongoing_stages,
            "upcoming": upcoming_stages,
        },
        "stages": stages_data,
        "engagement": {
            "pageviews": pageviews,
            "unique_visitors": unique_visitors,
            "conversion_rate": conversion_rate,
            "social_shares": social_shares,
            "stream_links": streams,
        }
    }, status=200)


# @shared_task(bind=True, rate_limit="1/s")
# def assign_stage_role_task(self, discord_id, role_id):
#     assign_discord_role(discord_id, role_id)

# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_kwargs={"max_retries": 5})
# def assign_stage_role_task(self, progress_id, discord_id, role_id):
#     from afc_auth.models import DiscordStageRoleAssignmentProgress

#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)

#     try:
#         assign_discord_role(discord_id, role_id)
#         progress.completed += 1
#     except Exception:
#         progress.failed += 1
#         raise
#     finally:
#         progress.save()

#     if progress.completed + progress.failed >= progress.total:
#         progress.status = "done"
#         progress.save()


# @shared_task(bind=True, rate_limit="1/s", max_retries=10)
# def assign_stage_role_task(self, progress_id, discord_id, role_id):
#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)

#     result = assign_discord_role(discord_id, role_id)

#     if result.get("rate_limited"):
#         countdown = int(result.get("retry_after", 2)) + 1
#         raise self.retry(countdown=countdown)

#     if result.get("ok"):
#         progress.completed += 1
#     else:
#         progress.failed += 1

#     # mark done when finished
#     if progress.completed + progress.failed >= progress.total:
#         progress.status = "done"

#     progress.save()



# @shared_task(bind=True, rate_limit="1/s")
# def assign_group_role_task(self, discord_id, role_id):
#     assign_discord_role(discord_id, role_id)

# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_kwargs={"max_retries": 5, "countdown": 10})
# def assign_group_role_task(self, discord_id, role_id):
#     assign_discord_role(discord_id, role_id)


# @shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=10, retry_kwargs={"max_retries": 5})
# def assign_group_role_task(self, assignment_id):
#     assignment = DiscordRoleAssignment.objects.get(id=assignment_id)

#     try:
#         success = assign_discord_role(
#             assignment.discord_id,
#             assignment.role_id
#         )

#         if success:
#             assignment.status = "success"
#         else:
#             assignment.status = "failed"
#             assignment.error_message = "Discord API returned non-204"

#     except Exception as e:
#         assignment.status = "failed"
#         assignment.error_message = str(e)
#         raise  # allows retry

#     finally:
#         assignment.save()

# @shared_task(bind=True, rate_limit="1/s", max_retries=10)
# def assign_group_role_task(self, assignment_id):
#     assignment = DiscordRoleAssignment.objects.select_related("user").get(id=assignment_id)

#     # already done?
#     if assignment.status == "success":
#         return

#     result = assign_discord_role(assignment.discord_id, assignment.role_id)

#     if result.get("rate_limited"):
#         # don’t mark failed; just retry after Discord says so
#         countdown = int(result.get("retry_after", 2)) + 1
#         raise self.retry(countdown=countdown)

#     if result.get("ok"):
#         assignment.status = "success"
#         assignment.error_message = ""
#         assignment.save(update_fields=["status", "error_message"])
#         return

#     assignment.status = "failed"
#     assignment.error_message = f'{result.get("status")} {result.get("text","")}'
#     assignment.save(update_fields=["status", "error_message"])



@shared_task(bind=True, rate_limit="1/s")
def remove_group_role_task(self, discord_id, role_id):  
    remove_discord_role(discord_id, role_id)


@api_view(["POST"])
def discord_role_progress(request):
    stage_id = request.data.get("stage_id")

    qs = DiscordRoleAssignment.objects.filter(stage_id=stage_id)

    return Response({
        "total": qs.count(),
        "pending": qs.filter(status="pending").count(),
        "success": qs.filter(status="success").count(),
        "failed": qs.filter(status="failed").count(),
    })


@api_view(["POST"])
def get_stage_role_assignment_progress(request):
    progress_id = request.data.get("progress_id")
    progress = get_object_or_404(DiscordStageRoleAssignmentProgress, id=progress_id)

    return Response({
        "total": progress.total,
        "completed": progress.completed,
        "failed": progress.failed,
        "status": progress.status,
        "percentage": round((progress.completed / progress.total) * 100, 2) if progress.total else 0
    })


@api_view(["GET"])
def get_all_role_progress(request):
    progresses = DiscordStageRoleAssignmentProgress.objects.all().order_by("-created_at")[:10]

    data = []
    for progress in progresses:
        data.append({
            "id": progress.id,
            "stage_id": progress.stage.stage_id,
            "stage_name": progress.stage.stage_name,
            "total": progress.total,
            "completed": progress.completed,
            "failed": progress.failed,
            "status": progress.status,
            "created_at": progress.created_at,
        })

    return Response(data)


@api_view(["POST"])
def retry_failed_discord_roles(request):
    stage_id = request.data.get("stage_id")

    failed = DiscordRoleAssignment.objects.filter(
        stage_id=stage_id,
        status="failed"
    )

    for assignment in failed:
        assignment.status = "pending"
        assignment.save()
        assign_group_roles_from_db_task.delay(assignment.id)

    return Response({"message": f"Retrying {failed.count()} failed assignments"})


from celery import shared_task
from django.db import transaction
from django.db.models import F

from celery import shared_task
from django.db import transaction
from django.db.models import F

from django.db import transaction
from django.db.models import F
from celery import shared_task

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 10})
def assign_stage_roles_from_db_task(self, progress_id, stage_id):
    progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
    stage = Stages.objects.get(stage_id=stage_id)

    if progress.status != "running":
        return

    batch_size = 25

    # 1) lock + claim a batch fast
    with transaction.atomic():
        qs = (DiscordRoleAssignment.objects
              .select_for_update(skip_locked=True)
              .filter(stage=stage, group__isnull=True, status="pending")
              .order_by("created_at")[:batch_size])

        assignments = list(qs)
        if not assignments:
            # no more pending
            progress.refresh_from_db()
            if progress.completed + progress.failed >= progress.total:
                progress.status = "done"
                progress.save(update_fields=["status"])
            return

        DiscordRoleAssignment.objects.filter(id__in=[a.id for a in assignments]).update(status="processing")

    # 2) do network calls OUTSIDE the transaction
    for assignment in assignments:
        r = assign_discord_role(assignment.discord_id, assignment.role_id)

        if r.status_code == 429:
            retry_after = 2
            try:
                retry_after = float(r.json().get("retry_after", 2))
            except Exception:
                pass

            # put them back to pending so another run can pick them later
            DiscordRoleAssignment.objects.filter(id=assignment.id).update(
                status="pending",
                error_message=f"429 rate limited"
            )
            raise self.retry(countdown=retry_after)

        if r.status_code in (200, 204):
            DiscordRoleAssignment.objects.filter(id=assignment.id).update(status="success", error_message=None)
            DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(completed=F("completed") + 1)
        else:
            DiscordRoleAssignment.objects.filter(id=assignment.id).update(
                status="failed",
                error_message=f"{r.status_code} {r.text[:300]}"
            )
            DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(failed=F("failed") + 1)

    # 3) schedule next batch
    assign_stage_roles_from_db_task.apply_async(args=[progress_id, stage_id], countdown=1)


# @shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 10})
# def assign_stage_roles_from_db_task(self, progress_id, stage_id):
#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
#     stage = Stages.objects.get(stage_id=stage_id)

#     if progress.status != "running":
#         return

#     batch_size = 25  # keep small to be safe with discord rate limits

#     with transaction.atomic():
#         qs = (DiscordRoleAssignment.objects
#               .select_for_update(skip_locked=True)
#               .filter(stage=stage, group__isnull=True, status="pending")
#               .order_by("created_at")[:batch_size])

#         assignments = list(qs)

#         if not assignments:
#             progress.refresh_from_db()
#             if progress.completed + progress.failed >= progress.total:
#                 progress.status = "done"
#                 progress.save(update_fields=["status"])
#             return

#         for assignment in assignments:
#             r = assign_discord_role(assignment.discord_id, assignment.role_id)

#             if r.status_code == 429:
#                 retry_after = 2
#                 try:
#                     retry_after = float(r.json().get("retry_after", 2))
#                 except Exception:
#                     pass
#                 raise self.retry(countdown=retry_after)

#             if r.status_code in (200, 204):
#                 assignment.status = "success"
#                 assignment.error_message = None
#                 assignment.save(update_fields=["status", "error_message", "updated_at"])
#                 DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(
#                     completed=F("completed") + 1
#                 )
#             else:
#                 assignment.status = "failed"
#                 assignment.error_message = f"{r.status_code} {r.text[:300]}"
#                 assignment.save(update_fields=["status", "error_message", "updated_at"])
#                 DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(
#                     failed=F("failed") + 1
#                 )

#     # keep going
#     assign_stage_roles_from_db_task.delay(progress_id, stage_id)


# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 10})
# def assign_stage_roles_from_db_task(self, progress_id, stage_id):
#     progress = DiscordStageRoleAssignmentProgress.objects.get(id=progress_id)
#     stage = Stages.objects.get(stage_id=stage_id)

#     # get a small batch of pending assignments
#     qs = DiscordRoleAssignment.objects.filter(stage=stage, status="pending").order_by("created_at")[:50]
#     if not qs.exists():
#         # maybe done
#         with transaction.atomic():
#             progress.refresh_from_db()
#             if progress.completed + progress.failed >= progress.total:
#                 progress.status = "done"
#                 progress.save(update_fields=["status"])
#         return

#     for assignment in qs:
#         r = assign_discord_role(assignment.discord_id, assignment.role_id)

#         if r.status_code == 429:
#             retry_after = 2
#             try:
#                 retry_after = float(r.json().get("retry_after", 2))
#             except Exception:
#                 pass
#             raise self.retry(countdown=retry_after)

#         if r.status_code in (200, 204):
#             assignment.status = "success"
#             assignment.error_message = None
#             assignment.save(update_fields=["status", "error_message", "updated_at"])
#             DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(completed=F("completed") + 1)
#         else:
#             assignment.status = "failed"
#             assignment.error_message = f"{r.status_code} {r.text[:300]}"
#             assignment.save(update_fields=["status", "error_message", "updated_at"])
#             DiscordStageRoleAssignmentProgress.objects.filter(id=progress_id).update(failed=F("failed") + 1)

#     # re-run until queue empty
#     assign_stage_roles_from_db_task.delay(progress_id, stage_id)



# @shared_task(bind=True, rate_limit="1/s", autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 10})
# def assign_group_roles_from_db_task(self, stage_id):
#     stage = Stages.objects.get(stage_id=stage_id)

#     qs = DiscordRoleAssignment.objects.filter(stage=stage, group__isnull=False, status="pending").order_by("created_at")[:50]
#     if not qs.exists():
#         return

#     for assignment in qs:
#         r = assign_discord_role(assignment.discord_id, assignment.role_id)

#         if r.status_code == 429:
#             retry_after = 2
#             try:
#                 retry_after = float(r.json().get("retry_after", 2))
#             except Exception:
#                 pass
#             raise self.retry(countdown=retry_after)

#         if r.status_code in (200, 204):
#             assignment.status = "success"
#             assignment.error_message = None
#         else:
#             assignment.status = "failed"
#             assignment.error_message = f"{r.status_code} {r.text[:300]}"

#         assignment.save(update_fields=["status", "error_message", "updated_at"])

#     assign_group_roles_from_db_task.delay(stage_id)


from celery import shared_task
from django.db import transaction
from django.db.models import F
from django.utils import timezone

@shared_task(bind=True, max_retries=50)
def assign_group_roles_from_db_task(self, stage_id, batch_size=10):
    from afc_tournament_and_scrims.models import Stages
    from afc_auth.models import DiscordRoleAssignment

    stage = Stages.objects.get(stage_id=stage_id)

    with transaction.atomic():
        qs = (
            DiscordRoleAssignment.objects
            .select_for_update(skip_locked=True)
            .filter(stage=stage, group__isnull=False, status="pending")
            .order_by("created_at")[:batch_size]
        )
        assignments = list(qs)

        # mark these as "processing" to avoid re-picking (optional)
        # if you want, add "processing" to STATUS_CHOICES.
        # DiscordRoleAssignment.objects.filter(id__in=[a.id for a in assignments]).update(status="processing")

    if not assignments:
        return {"message": "No pending assignments."}

    for a in assignments:
        r = assign_discord_role(a.discord_id, a.role_id)

        # RATE LIMIT
        if r.status_code == 429:
            retry_after = 2.0
            try:
                retry_after = float(r.json().get("retry_after", 2))
            except Exception:
                pass

            # put it back to pending
            a.status = "pending"
            a.error_message = f"429 rate limited: {r.text[:200]}"
            a.save(update_fields=["status", "error_message", "updated_at"])

            # IMPORTANT: stop processing, retry later
            raise self.retry(countdown=retry_after)

        # SUCCESS
        if r.status_code in (200, 204):
            a.status = "success"
            a.error_message = None
            a.save(update_fields=["status", "error_message", "updated_at"])
        else:
            a.status = "failed"
            a.error_message = f"{r.status_code} {r.text[:300]}"
            a.save(update_fields=["status", "error_message", "updated_at"])

    # If there might be more pending, requeue gently
    remaining = DiscordRoleAssignment.objects.filter(stage=stage, group__isnull=False, status="pending").count()
    if remaining > 0:
        assign_group_roles_from_db_task.apply_async(args=[stage_id], countdown=1)

    return {"processed": len(assignments), "remaining": remaining}



# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")

#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     solo_players = RegisteredCompetitors.objects.filter(
#         event=event,
#         user__isnull=False,
#         team__isnull=True,
#         status="registered"
#     )

#     seeded_count = 0

#     error_disc = []

#     for reg in solo_players:
#         # ✅ Avoid duplicate StageCompetitor
#         obj, created = StageCompetitor.objects.get_or_create(
#             stage=stage,
#             player=reg,
#             defaults={"status": "active"}
#         )

#         if created:
#             seeded_count += 1

#             # ✅ Assign Discord role in background
#             if reg.user.discord_id and stage.stage_discord_role_id:
#                 progress = DiscordStageRoleAssignmentProgress.objects.create(
#                     stage=stage,
#                     total=solo_players.count(),
#                     status="running"
#                 )

#                 try:
#                     assign_stage_role_task.delay(
#                         progress.id,
#                         reg.user.discord_id,
#                         stage.stage_discord_role_id
#                     )
#                 except Exception as e:
#                     # Log error, but don't fail the whole seeding
#                     error_disc.append(f"Failed to queue Discord role for {reg.user.username}: {e}")
#                     print(f"Failed to queue Discord role for {reg.user.username}: {e}")

#     stage.stage_status = "ongoing"
#     stage.save()

#     return Response({
#         "message": f"Seeded {seeded_count} solo players into stage '{stage.stage_name}'.",
#         "errors": error_disc
#     }, status=200)


# import math
# from django.db import transaction
# from django.shortcuts import get_object_or_404
# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from rest_framework import status

# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     # all registered solo players
#     solo_regs = list(
#         RegisteredCompetitors.objects.select_related("user").filter(
#             event=event,
#             status="registered",
#             user__isnull=False,
#             team__isnull=True
#         )
#     )

#     if not solo_regs:
#         return Response({"message": "No solo registrations found."}, status=400)

#     # Existing StageCompetitors -> avoid duplicates without per-row get_or_create
#     existing_reg_ids = set(
#         StageCompetitor.objects.filter(stage=stage, player__in=solo_regs)
#         .values_list("player_id", flat=True)
#     )

#     to_create = [
#         StageCompetitor(stage=stage, player=reg, status="active")
#         for reg in solo_regs
#         if reg.id not in existing_reg_ids
#     ]

#     # Create ONE progress row (NOT inside the loop)
#     progress = DiscordStageRoleAssignmentProgress.objects.create(
#         stage=stage,
#         total=len(solo_regs),
#         completed=0,
#         failed=0,
#         status="running"
#     )

#     # Create StageCompetitors in bulk
#     with transaction.atomic():
#         if to_create:
#             StageCompetitor.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=1000)

#     # Queue role assignments (Celery handles rate-limit safely)
#     role_id = stage.stage_discord_role_id
#     if role_id:
#         for reg in solo_regs:
#             if reg.user and reg.user.discord_id:
#                 assign_stage_role_task.delay(progress.id, reg.user.discord_id, role_id)
#             else:
#                 # still count it so progress finishes
#                 progress.failed += 1
#         progress.save()

#     stage.stage_status = "ongoing"
#     stage.save(update_fields=["stage_status"])

#     return Response({
#         "message": f"Seed complete for stage '{stage.stage_name}'. Roles queued.",
#         "stage_id": stage.stage_id,
#         "total_registrations": len(solo_regs),
#         "created_stage_competitors": len(to_create),
#         "progress_id": str(progress.id),
#     }, status=200)


from django.db import transaction
from django.db.models import F
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


from django.db import transaction
from django.db.models import F

@api_view(["POST"])
def seed_solo_players_to_stage(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized"}, status=403)

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    if not event_id or not stage_id:
        return Response({"message": "event_id and stage_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

    if event.participant_type != "solo":
        return Response({"message": "This event is not a solo event."}, status=400)

    solo_players = RegisteredCompetitors.objects.select_related("user").filter(
        event=event,
        user__isnull=False,
        team__isnull=True,
        status="registered"
    )

    total = solo_players.count()
    if total == 0:
        return Response({"message": "No registered solo players found."}, status=400)

    # ✅ create ONE progress row
    progress = DiscordStageRoleAssignmentProgress.objects.create(
        stage=stage,
        total=total,
        status="running"
    )

    assignments = []
    created_stagecompetitors = 0

    with transaction.atomic():
        for reg in solo_players:
            _, created = StageCompetitor.objects.get_or_create(
                stage=stage,
                player=reg,
                defaults={"status": "active"}
            )
            if created:
                created_stagecompetitors += 1

            # queue role assignment only if we have discord + role id
            if reg.user and reg.user.discord_id and stage.stage_discord_role_id:
                assignments.append(DiscordRoleAssignment(
                    user=reg.user,
                    discord_id=reg.user.discord_id,
                    role_id=stage.stage_discord_role_id,
                    stage=stage,
                    group=None,
                    status="pending"
                ))

        # bulk create assignments
        DiscordRoleAssignment.objects.bulk_create(assignments, ignore_conflicts=True, batch_size=1000)

    # ✅ start the batch worker loop ONCE
    assign_stage_roles_from_db_task.delay(str(progress.id), stage.stage_id)

    stage.stage_status = "ongoing"
    stage.save(update_fields=["stage_status"])

    return Response({
        "message": "Stage competitors seeded and Discord assignments queued.",
        "total_registered": total,
        "stagecompetitors_created": created_stagecompetitors,
        "assignments_queued": len(assignments),
        "progress_id": str(progress.id),
    }, status=200)


# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     solo_players_qs = RegisteredCompetitors.objects.select_related("user").filter(
#         event=event,
#         user__isnull=False,
#         team__isnull=True,
#         status="registered"
#     )

#     total = solo_players_qs.count()
#     if total == 0:
#         return Response({"message": "No registered solo players found."}, status=400)

#     # ✅ Create ONE progress row
#     progress = DiscordStageRoleAssignmentProgress.objects.create(
#         stage=stage,
#         total=total,
#         status="running"
#     )

#     seeded_count = 0
#     role_assignments = []

#     # ✅ Preload existing StageCompetitor to avoid duplicates
#     existing_ids = set(
#         StageCompetitor.objects.filter(stage=stage, player__in=solo_players_qs)
#         .values_list("player_id", flat=True)
#     )

#     # ✅ Seed + queue role assignments
#     for reg in solo_players_qs:
#         if reg.id not in existing_ids:
#             StageCompetitor.objects.create(stage=stage, player=reg, status="active")
#             seeded_count += 1

#         if reg.user and reg.user.discord_id and stage.stage_discord_role_id:
#             role_assignments.append(
#                 DiscordRoleAssignment(
#                     user=reg.user,
#                     discord_id=reg.user.discord_id,
#                     role_id=stage.stage_discord_role_id,
#                     stage=stage,
#                     status="pending",
#                 )
#             )

#     if role_assignments:
#         DiscordRoleAssignment.objects.bulk_create(role_assignments, batch_size=500)

#         # enqueue processing (chunking helps huge loads)
#         assign_stage_roles_from_db_task.delay(str(progress.id), stage.stage_id)

#     stage.stage_status = "ongoing"
#     stage.save()

#     return Response({
#         "message": f"Seeded {seeded_count} solo players into stage '{stage.stage_name}'.",
#         "total_registered": total,
#         "progress_id": str(progress.id),
#         "queued_role_assignments": len(role_assignments),
#     }, status=200)



# @api_view(["POST"])
# def seed_solo_players_to_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     if admin.role != "admin":
#         return Response(
#             {"message": "You do not have permission to perform this action."},
#             status=status.HTTP_403_FORBIDDEN
#         )


#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")

#     if not event_id or not stage_id:
#         return Response({"message": "event_id and stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     # ✅ ENSURE SOLO EVENT
#     if event.participant_type != "solo":
#         return Response({"message": "This event is not a solo event."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

#     solo_players = RegisteredCompetitors.objects.filter(
#         event=event,
#         user__isnull=False,
#         team__isnull=True,
#         status="registered"
#     )

#     seeded_count = 0

#     for reg in solo_players:

#         if reg.user.discord_id and stage.stage_discord_role_id:
#             try:
#                 assign_stage_role_task.delay(
#                     reg.user.discord_id,
#                     stage.stage_discord_role_id
#                 )
#             except Exception as e:
#                 return Response({"message": f"Failed to assign Discord role: {str(e)}"}, status=500)

#             seeded_count += 1


#     return Response({
#         "message": f"Seeded {seeded_count} solo players into stage '{stage.stage_name}'."
#     }, status=200)



from random import shuffle
from afc_tournament_and_scrims.models import StageGroups, StageCompetitor, StageGroupCompetitor

# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     if admin.role != "admin":
#         return Response(
#             {"message": "You do not have permission to perform this action."},
#             status=status.HTTP_403_FORBIDDEN
#         )
#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)

#     groups = list(stage.groups.all())
#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     competitors = list(
#         stage.competitors.filter(status="active", player__isnull=False)
#     )

#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     shuffle(competitors)

#     group_count = len(groups)
#     seeded_count = 0

#     for idx, competitor in enumerate(competitors):
#         group = groups[idx % group_count]

#         _, created = StageGroupCompetitor.objects.get_or_create(
#             stage_group=group,
#             player=competitor.player,
#             defaults={"status": "active"}
#         )

#         if created:
#             seeded_count += 1

#             # ✅ SAFE DISCORD ASSIGN
#             user = competitor.player.user
#             if user.discord_id and group.group_discord_role_id:
#                 assign_group_role_task.delay(user.discord_id, group.group_discord_role_id)

#     return Response({
#         "message": f"Seeded {seeded_count} competitors into {group_count} groups for stage '{stage.stage_name}'."
#     }, status=200)


# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)
#     groups = list(stage.groups.all())
#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     # Get all active StageCompetitors (players only)
#     competitors = list(stage.competitors.filter(status="active", player__isnull=False))
#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     shuffle(competitors)  # randomize

#     group_count = len(groups)
#     seeded_count = 0

#     for idx, competitor in enumerate(competitors):
#         group = groups[idx % group_count]

#         # Create or skip if already in group
#         obj, created = StageGroupCompetitor.objects.get_or_create(
#             stage_group=group,
#             player=competitor.player,
#             defaults={"status": "active"}
#         )

#         if created:
#             seeded_count += 1

#             # Assign discord role safely
#             if competitor.player.user and competitor.player.user.discord_id and group.group_discord_role_id:
#                 assign_group_role_task.delay(competitor.player.user.discord_id, group.group_discord_role_id)

#     return Response({
#         "message": f"Seeded {seeded_count} competitors into {group_count} groups for stage '{stage.stage_name}'."
#     }, status=200)

# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     token = auth.split(" ")[1]
#     admin = validate_token(token)

#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)
#     groups = list(stage.groups.all())

#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     competitors = list(
#         stage.competitors.filter(status="active", player__isnull=False)
#     )

#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     shuffle(competitors)

#     group_count = len(groups)
#     seeded_count = 0

#     for idx, competitor in enumerate(competitors):
#         player = competitor.player

#         # 🚫 Prevent player being in more than one group
#         if StageGroupCompetitor.objects.filter(
#             stage_group__stage=stage,
#             player=player
#         ).exists():
#             continue

#         group = groups[idx % group_count]

#         StageGroupCompetitor.objects.create(
#             stage_group=group,
#             player=player,
#             status="active"
#         )

#         seeded_count += 1

#         # ✅ Discord role (async)
#         user = player.user
#         if user and user.discord_id and group.group_discord_role_id:
#             assignment = DiscordRoleAssignment.objects.create(
#             user=competitor.player.user,
#             discord_id=competitor.player.user.discord_id,
#             role_id=group.group_discord_role_id,
#             stage=stage,
#             group=group,
#             status="pending"
#         )

#         assign_group_role_task.delay(assignment.id)

#             # assign_group_role_task.delay(
#             #     user.discord_id,
#             #     group.group_discord_role_id
#             # )

#     return Response({
#         "message": f"Seeded {seeded_count} competitors into {group_count} groups for stage '{stage.stage_name}'."
#     }, status=200)


# from random import shuffle
# from django.db import transaction

# @api_view(["POST"])
# def seed_stage_competitors_to_groups(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     stage_id = request.data.get("stage_id")
#     if not stage_id:
#         return Response({"message": "stage_id is required."}, status=400)

#     stage = get_object_or_404(Stages, stage_id=stage_id)
#     groups = list(stage.groups.all().order_by("group_id"))
#     if not groups:
#         return Response({"message": "No groups found for this stage."}, status=400)

#     # stage competitors (solo)
#     competitors = list(
#         StageCompetitor.objects.select_related("player__user").filter(
#             stage=stage, status="active", player__isnull=False
#         )
#     )
#     if not competitors:
#         return Response({"message": "No competitors found to seed."}, status=400)

#     # players already seeded into ANY group in THIS stage
#     already_seeded_ids = set(
#         StageGroupCompetitor.objects.filter(stage_group__stage=stage, player__isnull=False)
#         .values_list("player_id", flat=True)
#     )

#     unseeded = [c for c in competitors if c.player_id not in already_seeded_ids]
#     if not unseeded:
#         return Response({"message": "All competitors are already seeded into groups for this stage."}, status=200)

#     shuffle(unseeded)

#     group_count = len(groups)
#     group_role_jobs = []   # (discord_id, role_id, stage, group)
#     to_create = []

#     for idx, comp in enumerate(unseeded):
#         group = groups[idx % group_count]
#         to_create.append(StageGroupCompetitor(
#             stage_group=group,
#             player=comp.player,
#             status="active"
#         ))

#         user = comp.player.user
#         if user and user.discord_id and group.group_discord_role_id:
#             group_role_jobs.append((user, user.discord_id, group.group_discord_role_id, stage, group))

#     with transaction.atomic():
#         StageGroupCompetitor.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=1000)

#         # create assignment rows in bulk too (fast)
#         assignments = [
#             DiscordRoleAssignment(
#                 user=u,
#                 discord_id=discord_id,
#                 role_id=role_id,
#                 stage=stage,
#                 group=group,
#                 status="pending"
#             )
#             for (u, discord_id, role_id, stage, group) in group_role_jobs
#         ]
#         DiscordRoleAssignment.objects.bulk_create(assignments, batch_size=1000)

#     # queue celery jobs
#     for a in DiscordRoleAssignment.objects.filter(stage=stage, group__isnull=False, status="pending"):
#         assign_group_role_task.delay(a.id)

#     return Response({
#         "message": f"Seeded {len(unseeded)} competitors into {group_count} groups for stage '{stage.stage_name}'.",
#         "seeded": len(unseeded),
#         "groups": group_count,
#         "discord_assignments_queued": len(group_role_jobs),
#     }, status=200)


from random import shuffle

@api_view(["POST"])
def seed_stage_competitors_to_groups(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)
    groups = list(stage.groups.all())
    if not groups:
        return Response({"message": "No groups found for this stage."}, status=400)

    competitors = list(stage.competitors.select_related("player__user").filter(status="active", player__isnull=False))
    if not competitors:
        return Response({"message": "No competitors found to seed."}, status=400)

    shuffle(competitors)

    # ✅ already seeded in ANY group in this stage
    already_seeded_player_ids = set(
        StageGroupCompetitor.objects.filter(stage_group__stage=stage, player__isnull=False)
        .values_list("player_id", flat=True)
    )

    seeded_idx = 0
    to_create = []
    role_assignments = []

    for competitor in competitors:
        player = competitor.player
        if player.id in already_seeded_player_ids:
            continue

        group = groups[seeded_idx % len(groups)]
        seeded_idx += 1
        already_seeded_player_ids.add(player.id)

        to_create.append(StageGroupCompetitor(stage_group=group, player=player, status="active"))

        user = player.user
        if user and user.discord_id and group.group_discord_role_id:
            role_assignments.append(
                DiscordRoleAssignment(
                    user=user,
                    discord_id=user.discord_id,
                    role_id=group.group_discord_role_id,
                    stage=stage,
                    group=group,
                    status="pending",
                )
            )

    StageGroupCompetitor.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
    if role_assignments:
        DiscordRoleAssignment.objects.bulk_create(role_assignments, batch_size=500)

        assign_group_roles_from_db_task.delay(stage.stage_id)  # process pending for this stage

    return Response({
        "message": f"Seeded {len(to_create)} competitors into {len(groups)} groups for stage '{stage.stage_name}'.",
        "queued_role_assignments": len(role_assignments),
    }, status=200)




@api_view(["POST"])
def disqualify_registered_competitor(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    competitor_id = request.data.get("competitor_id")
    event_id = request.data.get("event_id")

    if not competitor_id or not event_id:
        return Response({"message": "competitor_id, event_id, and stage_id are required."}, status=400)
    
    user = get_object_or_404(User, user_id=competitor_id)

    event = get_object_or_404(Event, event_id=event_id)
    competitor = get_object_or_404(RegisteredCompetitors, user=user, event=event)

    
    # remove all stage and group roles related to this event
    stages = Stages.objects.filter(event=event)
    for stage in stages:
        if user.discord_id and stage.stage_discord_role_id:
            remove_discord_role(user.discord_id, stage.stage_discord_role_id)
        groups = StageGroups.objects.filter(stage=stage)
        for group in groups:
            if user.discord_id and group.group_discord_role_id:
                remove_discord_role(user.discord_id, group.group_discord_role_id)
    
    competitor.status = "disqualified"
    competitor.save()

    return Response({
        "message": f"Competitor '{user.username}' has been disqualified from event '{competitor.event.event_name}'."
    }, status=200)


@api_view(["POST"])
def reactivate_registered_competitor(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    # stage_id = request.data.get("stage_id")
    competitor_id = request.data.get("competitor_id")
    event_id = request.data.get("event_id")

    if not competitor_id or not event_id:
        return Response({"message": "competitor_id and event_id are required."}, status=400)

    # stage = get_object_or_404(Stages, stage_id=stage_id)

    user = get_object_or_404(User, user_id=competitor_id)
    event = get_object_or_404(Event, event_id=event_id)

    competitor = get_object_or_404(RegisteredCompetitors, user=user, event=event)

    competitor.status = "registered"
    competitor.save()

    return Response({
        "message": f"Competitor '{competitor.player.competitor_name}' has been reactivated for event '{competitor.event.event_name}'."
    }, status=200)


# @api_view(["POST"])
# def send_match_room_details_notification_to_competitor(request):
#     # ---------------- AUTH ----------------
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
    
#     token = session_token.split(" ")[1]
#     admin = validate_token(token)
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
    
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     # ---------------- EVENT ----------------
#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     # ---------------- GET MATCHES ----------------
#     matches = Match.objects.filter(group=group_id)
#     if not matches.exists():
#         return Response({"message": "No matches found for this event."}, status=400)

#     total_notifications = 0

#     for match in matches:
#         # Ensure match has room details
#         if not match.room_id or not match.room_name or not match.room_password:
#             continue

#         # Solo event
#         if event.participant_type == "solo":
#             competitors = StageGroupCompetitor.objects.filter(stage_group=match.group, player__isnull=False)
#             for sc in competitors:
#                 user = sc.player.user
#                 if user:
#                     message = (
#                         f"Hello {user.username}, your match details for '{event.event_name}' (Stage: {match.group.stage.stage_name}, "
#                         f"Group: {match.group.group_name}, Match: {match.match_number}) are:\n"
#                         f"Room ID: {match.room_id}\n"
#                         f"Room Name: {match.room_name}\n"
#                         f"Password: {match.room_password}"
#                     )
#                     Notifications.objects.create(user=user, message=message)
#                     total_notifications += 1

#         # Team event
#         else:
#             teams = StageGroupCompetitor.objects.filter(stage_group=match.group, tournament_team__isnull=False)
#             for sgc in teams:
#                 team = sgc.tournament_team
#                 for player in team.players.all():
#                     if player.user:
#                         message = (
#                             f"Hello {player.user.username}, your match details for '{event.event_name}' (Stage: {match.group.stage.stage_name}, "
#                             f"Group: {match.group.group_name}, Match: {match.match_number}) are:\n"
#                             f"Room ID: {match.room_id}\n"
#                             f"Room Name: {match.room_name}\n"
#                             f"Password: {match.room_password}"
#                         )
#                         Notifications.objects.create(user=player.user, message=message)
#                         total_notifications += 1

#     return Response({
#         "message": f"Sent match room notifications to {total_notifications} users for event '{event.event_name}'."
#     }, status=200)

# @api_view(["POST"])
# def send_match_room_details_notification_to_competitor(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)

#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     # ---------------- INPUT ----------------
#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")

#     if not event_id or not group_id:
#         return Response({"message": "event_id and group_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     group = get_object_or_404(StageGroups, group_id=group_id)

#     matches = Match.objects.filter(group=group)
#     if not matches.exists():
#         return Response({"message": "No matches found for this group."}, status=400)

#     total_notifications = 0

#     for match in matches:
#         if not all([match.room_id, match.room_name, match.room_password]):
#             continue

#         # SOLO EVENT
#         if event.participant_type == "solo":
#             competitors = StageGroupCompetitor.objects.select_related(
#                 "player__user"
#             ).filter(stage_group=group, player__isnull=False)

#             for sc in competitors:
#                 user = sc.player.user
#                 if not user:
#                     continue

#                 Notifications.objects.create(
#                     user=user,
#                     message=(
#                         f"Hello {user.username}, your match details for '{event.event_name}'\n"
#                         f"Stage: {group.stage.stage_name}\n"
#                         f"Group: {group.group_name}\n"
#                         f"Match: {match.match_number}\n\n"
#                         f"Room ID: {match.room_id}\n"
#                         f"Room Name: {match.room_name}\n"
#                         f"Password: {match.room_password}"
#                     )
#                 )
#                 total_notifications += 1

#         # TEAM EVENT
#         else:
#             teams = StageGroupCompetitor.objects.select_related(
#                 "tournament_team"
#             ).filter(stage_group=group, tournament_team__isnull=False)

#             for sgc in teams:
#                 for player in sgc.tournament_team.players.select_related("user").all():
#                     if not player.user:
#                         continue

#                     Notifications.objects.create(
#                         user=player.user,
#                         message=(
#                             f"Hello {player.user.username}, your match details for '{event.event_name}'\n"
#                             f"Stage: {group.stage.stage_name}\n"
#                             f"Group: {group.group_name}\n"
#                             f"Match: {match.match_number}\n\n"
#                             f"Room ID: {match.room_id}\n"
#                             f"Room Name: {match.room_name}\n"
#                             f"Password: {match.room_password}"
#                         )
#                     )
#                     total_notifications += 1

#     return Response({
#         "message": f"Sent match room notifications to {total_notifications} users."
#     }, status=200)



@api_view(["POST"])
def send_match_room_details_notification_to_competitor(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    event_id = request.data.get("event_id")
    group_id = request.data.get("group_id")
    if not event_id or not group_id:
        return Response({"message": "event_id and group_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    group = get_object_or_404(StageGroups, group_id=group_id)

    matches = Match.objects.filter(group=group).order_by("match_number")
    if not matches.exists():
        return Response({"message": "No matches found for this group."}, status=400)

    total_notifications = 0

    for match in matches:
        if not (match.room_id and match.room_name and match.room_password):
            continue

        if event.participant_type == "solo":
            competitors = (StageGroupCompetitor.objects
                           .select_related("player__user")
                           .filter(stage_group=group, player__isnull=False))

            for sc in competitors:
                user = sc.player.user
                if not user:
                    continue

                Notifications.objects.create(
                    user=user,
                    message=(
                        f"Hello {user.username}, your match details for '{event.event_name}'\n"
                        f"Stage: {group.stage.stage_name}\n"
                        f"Group: {group.group_name}\n"
                        f"Match: {match.match_number}\n\n"
                        f"Room ID: {match.room_id}\n"
                        f"Room Name: {match.room_name}\n"
                        f"Password: {match.room_password}"
                    )
                )
                total_notifications += 1
        else:
            teams = (StageGroupCompetitor.objects
                     .select_related("tournament_team")
                     .filter(stage_group=group, tournament_team__isnull=False))

            for sgc in teams:
                members = sgc.tournament_team.members.select_related("user").all()
                for m in members:
                    if not m.user:
                        continue

                    Notifications.objects.create(
                        user=m.user,
                        message=(
                            f"Hello {m.user.username}, your match details for '{event.event_name}'\n"
                            f"Stage: {group.stage.stage_name}\n"
                            f"Group: {group.group_name}\n"
                            f"Match: {match.match_number}\n\n"
                            f"Room ID: {match.room_id}\n"
                            f"Room Name: {match.room_name}\n"
                            f"Password: {match.room_password}"
                        )
                    )
                    total_notifications += 1

    return Response({"message": f"Sent match room notifications to {total_notifications} users."}, status=200)


@api_view(["POST"])
def remove_all_stage_competitors_from_groups_and_their_discord_roles(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    groups = stage.groups.all()
    total_removed = 0

    for group in groups:
        competitors = StageGroupCompetitor.objects.filter(stage_group=group)
        for competitor in competitors:
            user = competitor.player.user
            if user.discord_id and group.group_discord_role_id:
                remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)
            competitor.delete()
            total_removed += 1

    return Response({
        "message": f"Removed {total_removed} competitors from all groups in stage '{stage.stage_name}' and their Discord roles."
    }, status=200)


@api_view(["POST"])
def delete_stage(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    # Remove all group roles from competitors
    groups = stage.groups.all()
    for group in groups:
        competitors = StageGroupCompetitor.objects.filter(stage_group=group)
        for competitor in competitors:
            user = competitor.player.user
            if user.discord_id and group.group_discord_role_id:
                remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

    stage.delete()

    return Response({
        "message": f"Stage '{stage.stage_name}' has been removed along with all associated groups and competitor roles."
    }, status=200)



@api_view(["POST"])
def delete_group(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)
    
    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required."}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)

    # Remove all group roles from competitors
    competitors = StageGroupCompetitor.objects.filter(stage_group=group)
    for competitor in competitors:
        user = competitor.player.user
        if user.discord_id and group.group_discord_role_id:
            remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

    group.delete()

    return Response({
        "message": f"Group '{group.group_name}' has been removed along with all associated competitor roles."
    }, status=200)


@api_view(["POST"])
def get_all_user_id_in_stage(request):
    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    competitors = StageCompetitor.objects.filter(
        stage=stage,
        player__isnull=False
    ).select_related("player__user")

    user_ids = [
        competitor.player.user.user_id
        for competitor in competitors
        if competitor.player.user is not None
    ]

    return Response({
        "stage_id": stage.stage_id,
        "stage_name": stage.stage_name,
        "user_ids": user_ids
    }, status=200)


@api_view(["POST"])
def get_all_user_id_in_group(request):
    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required."}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)

    competitors = StageGroupCompetitor.objects.filter(
        stage_group=group,
        player__isnull=False
    ).select_related("player__user")

    user_ids = [
        competitor.player.user.user_id
        for competitor in competitors
        if competitor.player.user is not None
    ]

    return Response({
        "group_id": group.group_id,
        "group_name": group.group_name,
        "user_ids": user_ids
    }, status=200)


@api_view(["POST"])
def delete_notifications_from_users_in_a_group(request):
    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required."}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)

    competitors = StageGroupCompetitor.objects.filter(
        stage_group=group,
        player__isnull=False
    ).select_related("player__user")

    user_ids = [
        competitor.player.user.user_id
        for competitor in competitors
        if competitor.player.user is not None
    ]

    deleted_count = Notifications.objects.filter(user__user_id__in=user_ids).delete()[0]

    return Response({
        "message": f"Deleted {deleted_count} notifications for users in group '{group.group_name}'."
    }, status=200)


# @api_view(["POST"])
# def create_leaderboard(request):
#     session_token = request.headers.get("Authorization")
#     if not session_token or not session_token.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     token = session_token.split(" ")[1]
#     admin = validate_token(token)
#     if not admin:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)
    
#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     group_id = request.data.get("group_id")


@api_view(["POST"])
def check_if_user_registered_in_event(request):
    email = request.data.get("email")
    event_id = request.data.get("event_id")

    if not email or not event_id:
        return Response({"message": "email and event_id are required."}, status=400)
    event = get_object_or_404(Event, event_id=event_id)
    user = get_object_or_404(User, email=email)
    is_registered = RegisteredCompetitors.objects.filter(
        event=event,
        user=user,
        status="registered"
    ).exists()
    return Response({
        "email": email,
        "event_id": event_id,
        "is_registered": is_registered
    }, status=200)


from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db import transaction

@api_view(["POST"])
def create_leaderboard(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission."}, status=403)

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")
    group_id = request.data.get("group_id")
    leaderboard_name = request.data.get("leaderboard_name")
    leaderboard_method = request.data.get("leaderboard_method")  # optional
    file_type = request.data.get("file_type")  # optional

    if not event_id or not stage_id or not group_id:
        return Response({"message": "event_id, stage_id and group_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
    group = get_object_or_404(StageGroups, group_id=group_id, stage=stage)

    if not leaderboard_name:
        leaderboard_name = f"{event.event_name} - {stage.stage_name} - {group.group_name}"

    # Optional: placement_points passed from frontend
    placement_points = request.data.get("placement_points", {})
    if isinstance(placement_points, str):
        import json
        placement_points = json.loads(placement_points)

    kill_point = request.data.get("kill_point", 1)
    try:
        kill_point = float(kill_point)
    except:
        kill_point = 1.0

    with transaction.atomic():
        leaderboard, created = Leaderboard.objects.get_or_create(
            event=event,
            stage=stage,
            group=group,
            defaults={
                "leaderboard_name": leaderboard_name,
                "creator": admin,
                "placement_points": placement_points or {},
                "kill_point": kill_point,
            },
            leaderboard_method=leaderboard_method,
            file_type=file_type
        )

        # If it already exists, you may want to update the points config:
        if not created:
            if placement_points:
                leaderboard.placement_points = placement_points
            leaderboard.kill_point = kill_point
            leaderboard.leaderboard_name = leaderboard_name
            leaderboard.save()

        # OPTIONAL: attach leaderboard to this group's matches (only if not already set)
        Match.objects.filter(group=group, leaderboard__isnull=True).update(leaderboard=leaderboard)

    return Response({
        "message": "Leaderboard created successfully." if created else "Leaderboard already existed. Updated config.",
        "leaderboard_id": leaderboard.leaderboard_id,
        "created": created,
    }, status=200)

# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from rest_framework import status
# from django.shortcuts import get_object_or_404
# from django.db import transaction

# @api_view(["POST"])
# def create_leaderboard(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     event_id = request.data.get("event_id")
#     stage_id = request.data.get("stage_id")
#     group_id = request.data.get("group_id")
#     leaderboard_name = request.data.get("leaderboard_name")  # optional
#     leaderboard_method = request.data.get("leaderboard_method")  # optional
#     file_type = request.data.get("file_type")  # optional

#     if not event_id or not stage_id or not group_id:
#         return Response({"message": "event_id, stage_id and group_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     stage = get_object_or_404(Stages, stage_id=stage_id, event=event)
#     group = get_object_or_404(StageGroups, group_id=group_id, stage=stage)

#     if not leaderboard_name:
#         leaderboard_name = f"{event.event_name} - {stage.stage_name} - {group.group_name}"

#     with transaction.atomic():
#         leaderboard, created = Leaderboard.objects.get_or_create(
#             event=event,
#             stage=stage,
#             group=group,
#             leaderboard_method=leaderboard_method,
#             file_type=file_type,
#             defaults={
#                 "leaderboard_name": leaderboard_name,
#                 "creator": admin,
#             }
#         )

#         # attach matches for this group to this leaderboard (no N+1)
#         updated = (
#             Match.objects
#             .filter(group=group)
#             .exclude(leaderboard=leaderboard)
#             .update(leaderboard=leaderboard)
#         )

#     return Response({
#         "message": "Leaderboard created." if created else "Leaderboard already existed; updated matches.",
#         "leaderboard_id": leaderboard.leaderboard_id,
#         "matches_linked": updated,
#     }, status=200)



# import re
# import json
# from django.db import transaction
# from rest_framework.decorators import api_view, parser_classes
# from rest_framework.parsers import MultiPartParser, FormParser
# from rest_framework.response import Response
# from rest_framework import status
# from django.shortcuts import get_object_or_404

# SOLO_BLOCK_RE = re.compile(
#     r"Rank:\s*(?P<rank>\d+).*?"
#     r"KillScore:\s*(?P<kill_score>\d+).*?"
#     r"RankScore:\s*(?P<rank_score>\d+).*?"
#     r"TotalScore:\s*(?P<total_score>\d+).*?\n"
#     r"NAME:\s*(?P<name>.+?)\s+ID:\s*(?P<uid>\d+)\s+KILL:\s*(?P<kills>\d+)",
#     re.DOTALL
# )

# @api_view(["POST"])
# @parser_classes([MultiPartParser, FormParser])
# def upload_solo_match_result(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     match_id = request.data.get("match_id")
#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)

#     uploaded_file = request.FILES.get("file")
#     if not uploaded_file:
#         return Response({"message": "file is required."}, status=400)

#     # placement_points can be JSON string or omitted (use default)
#     placement_points_raw = request.data.get("placement_points")
#     if placement_points_raw:
#         if isinstance(placement_points_raw, str):
#             placement_points = json.loads(placement_points_raw)
#         else:
#             placement_points = placement_points_raw
#         # normalize keys to int
#         placement_points = {int(k): int(v) for k, v in placement_points.items()}
#     else:
#         placement_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

#     kill_point_value = int(request.data.get("kill_point_value", 1))  # 1 point per kill by default

#     match = get_object_or_404(Match, match_id=match_id)
#     event = match.group.stage.event if match.group else (match.leaderboard.event if match.leaderboard else None)
#     if not event:
#         return Response({"message": "Match is not linked to a group/leaderboard with an event."}, status=400)

#     if event.participant_type != "solo":
#         return Response({"message": "This API is for SOLO events only."}, status=400)

#     text = uploaded_file.read().decode("utf-8", errors="ignore")

#     parsed = []
#     for m in SOLO_BLOCK_RE.finditer(text):
#         parsed.append({
#             "placement": int(m.group("rank")),
#             "kills": int(m.group("kills")),
#             "uid": m.group("uid").strip(),
#             "name": m.group("name").strip(),
#             "raw_kill_score": int(m.group("kill_score")),
#             "raw_rank_score": int(m.group("rank_score")),
#             "raw_total_score": int(m.group("total_score")),
#         })

#     if not parsed:
#         return Response({
#             "message": "No results parsed from file. File format may not match expected SOLO layout."
#         }, status=400)

#     # Map uid -> RegisteredCompetitors
#     uids = [p["uid"] for p in parsed]
#     reg_map = {
#         rc.user.uid: rc
#         for rc in RegisteredCompetitors.objects.select_related("user")
#         .filter(event=event, status="registered", user__uid__in=uids)
#     }

#     missing = [p for p in parsed if p["uid"] not in reg_map]

#     stats_to_create = []
#     for p in parsed:
#         rc = reg_map.get(p["uid"])
#         if not rc:
#             continue

#         placement_pts = placement_points.get(p["placement"], 0)
#         kill_pts = p["kills"] * kill_point_value
#         total_pts = placement_pts + kill_pts

#         stats_to_create.append(SoloPlayerMatchStats(
#             match=match,
#             competitor=rc,
#             placement=p["placement"],
#             kills=p["kills"],
#             placement_points=placement_pts,
#             kill_points=kill_pts,
#             total_points=total_pts,
#             raw_kill_score=p["raw_kill_score"],
#             raw_rank_score=p["raw_rank_score"],
#             raw_total_score=p["raw_total_score"],
#         ))

#     with transaction.atomic():
#         # wipe and replace ensures no duplicates and makes re-upload safe
#         SoloPlayerMatchStats.objects.filter(match=match).delete()
#         SoloPlayerMatchStats.objects.bulk_create(stats_to_create, batch_size=500)

#     return Response({
#         "message": "Solo match results uploaded.",
#         "match_id": match.match_id,
#         "parsed_rows": len(parsed),
#         "saved_rows": len(stats_to_create),
#         "missing_registered_competitors": [
#             {"uid": p["uid"], "name": p["name"], "placement": p["placement"]}
#             for p in missing[:30]
#         ],
#         "missing_count": len(missing),
#     }, status=200)



# import json
# import re
# from django.db import transaction
# from django.shortcuts import get_object_or_404
# from rest_framework.decorators import api_view, parser_classes
# from rest_framework.parsers import MultiPartParser, FormParser
# from rest_framework.response import Response
# from rest_framework import status

# # Matches the 2-line SOLO blocks in your log file:
# # TeamName: Rank: X KillScore: Y RankScore: Z TotalScore: T
# # NAME: <name> ID: <uid> KILL: <kills>
# SOLO_BLOCK_RE = re.compile(
#     r"TeamName:\s*Rank:\s*(?P<rank>\d+)\s*"
#     r"KillScore:\s*(?P<kill_score>\d+)\s*"
#     r"RankScore:\s*(?P<rank_score>\d+)\s*"
#     r"TotalScore:\s*(?P<total_score>\d+)\s*"
#     r"\s*[\r\n]+"
#     r"NAME:\s*(?P<name>.*?)\s*"
#     r"ID:\s*(?P<uid>\d+)\s*"
#     r"KILL:\s*(?P<kills>\d+)",
#     re.MULTILINE
# )

# @api_view(["POST"])
# @parser_classes([MultiPartParser, FormParser])
# def upload_solo_match_result(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission to perform this action."}, status=403)

#     # ---------------- INPUT ----------------
#     match_id = request.data.get("match_id")
#     if not match_id:
#         return Response({"message": "match_id is required."}, status=400)

#     uploaded_file = request.FILES.get("file")
#     if not uploaded_file:
#         return Response({"message": "file is required."}, status=400)

#     match = get_object_or_404(Match, match_id=match_id)

#     # ---------------- EVENT + LEADERBOARD ----------------
#     if not match.group:
#         return Response({"message": "This match is not linked to a group."}, status=400)

#     event = match.group.stage.event
#     if event.participant_type != "solo":
#         return Response({"message": "This API is for SOLO events only."}, status=400)

#     # Prefer match.leaderboard; else use unique leaderboard (event, stage, group)
#     leaderboard = match.leaderboard
#     if not leaderboard:
#         leaderboard = Leaderboard.objects.filter(
#             event=event,
#             stage=match.group.stage,
#             group=match.group
#         ).first()

#     if not leaderboard:
#         return Response(
#             {"message": "No leaderboard found for this group. Create/link leaderboard first."},
#             status=400
#         )

#     # placement_points stored as JSONField like {"1": 12, "2": 9, ...}
#     placement_points_raw = leaderboard.placement_points or {}
#     try:
#         placement_points = {int(k): int(v) for k, v in placement_points_raw.items()}
#     except Exception:
#         return Response(
#             {"message": "Leaderboard placement_points must be a JSON object like {'1': 12, '2': 9, ...}"},
#             status=400
#         )

#     if not placement_points:
#         # fallback default (rank 1-10)
#         placement_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

#     kill_point_value = float(getattr(leaderboard, "kill_point", 1.0) or 1.0)

#     # ---------------- PARSE FILE ----------------
#     text = uploaded_file.read().decode("utf-8", errors="ignore")

#     parsed = []
#     for m in SOLO_BLOCK_RE.finditer(text):
#         parsed.append({
#             "placement": int(m.group("rank")),
#             "uid": m.group("uid").strip(),
#             "name": m.group("name").strip(),
#             "kills": int(m.group("kills")),
#             "raw_kill_score": int(m.group("kill_score")),
#             "raw_rank_score": int(m.group("rank_score")),
#             "raw_total_score": int(m.group("total_score")),
#         })

#     if not parsed:
#         return Response(
#             {"message": "No results parsed. Your log format didn't match SOLO_BLOCK_RE."},
#             status=400
#         )

#     # ---------------- MAP UID -> REGISTERED COMPETITOR ----------------
#     uids = [p["uid"] for p in parsed]

#     reg_qs = RegisteredCompetitors.objects.select_related("user").filter(
#         event=event,
#         status="registered",
#         user__uid__in=uids
#     )
#     reg_map = {str(rc.user.uid): rc for rc in reg_qs}

#     missing = [p for p in parsed if p["uid"] not in reg_map]

#     # ---------------- SAVE ----------------
#     stats_to_create = []
#     for p in parsed:
#         rc = reg_map.get(p["uid"])
#         if not rc:
#             continue

#         placement_pts = placement_points.get(p["placement"], 0)
#         kill_pts = p["kills"] * kill_point_value  # keep float support
#         total_pts = placement_pts + kill_pts

#         stats_to_create.append(SoloPlayerMatchStats(
#             match=match,
#             competitor=rc,
#             placement=p["placement"],
#             kills=p["kills"],
#             placement_points=placement_pts,
#             kill_points=kill_pts,
#             total_points=total_pts,
#             raw_kill_score=p["raw_kill_score"],
#             raw_rank_score=p["raw_rank_score"],
#             raw_total_score=p["raw_total_score"],
#         ))

#     with transaction.atomic():
#         # re-upload safe
#         SoloPlayerMatchStats.objects.filter(match=match).delete()
#         SoloPlayerMatchStats.objects.bulk_create(stats_to_create, batch_size=500)

#     return Response({
#         "message": "Solo match results uploaded using leaderboard scoring config.",
#         "match_id": match.match_id,
#         "leaderboard_id": leaderboard.leaderboard_id,
#         "kill_point": kill_point_value,
#         "parsed_rows": len(parsed),
#         "saved_rows": len(stats_to_create),
#         "missing_count": len(missing),
#         "missing_registered_competitors_preview": [
#             {"uid": p["uid"], "name": p["name"], "placement": p["placement"]}
#             for p in missing[:30]
#         ],
#     }, status=200)


import json
import re
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status

# Matches your file format like:
# Rank: 1  ... TotalScore: 120
# NAME: VT KingHydra  ID: 490350075  KILL: 6
SOLO_BLOCK_RE = re.compile(
    r"Rank:\s*(?P<placement>\d+).*?\n"
    r"NAME:\s*(?P<name>.+?)\s+ID:\s*(?P<uid>\d+)\s+KILL:\s*(?P<kills>\d+)",
    re.DOTALL
)

@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser])
def upload_solo_match_result(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    # ---------------- INPUT ----------------
    match_id = request.data.get("match_id")
    if not match_id:
        return Response({"message": "match_id is required."}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return Response({"message": "file is required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)

    # ---------------- EVENT + LEADERBOARD ----------------
    # In your DB, SOLO matches should be linked to a group
    if not match.group:
        return Response({"message": "This match is not linked to a group."}, status=400)

    event = match.group.stage.event
    if event.participant_type != "solo":
        return Response({"message": "This API is for SOLO events only."}, status=400)

    # Find leaderboard (match.leaderboard preferred)
    leaderboard = match.leaderboard
    if not leaderboard:
        leaderboard = Leaderboard.objects.filter(
            event=event,
            stage=match.group.stage,
            group=match.group
        ).first()

    if not leaderboard:
        return Response(
            {"message": "No leaderboard found for this group. Create leaderboard first."},
            status=400
        )

    # Use scoring config from leaderboard
    placement_points_raw = leaderboard.placement_points or {}
    try:
        placement_points = {int(k): int(v) for k, v in placement_points_raw.items()}
    except Exception:
        return Response(
            {"message": "Leaderboard placement_points must be a JSON object like {'1':12,'2':9,...}"},
            status=400
        )

    if not placement_points:
        placement_points = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}

    kill_point_value = float(getattr(leaderboard, "kill_point", 1.0) or 1.0)

    # ---------------- PARSE FILE ----------------
    text = uploaded_file.read().decode("utf-8", errors="ignore")

    parsed = []
    for m in SOLO_BLOCK_RE.finditer(text):
        parsed.append({
            "placement": int(m.group("placement")),
            "kills": int(m.group("kills")),
            "uid": m.group("uid").strip(),
            "name": m.group("name").strip(),
        })

    if not parsed:
        return Response(
            {"message": "No results parsed. Your log format may differ from SOLO_BLOCK_RE."},
            status=400
        )

    # ---------------- MAP UID -> RegisteredCompetitors ----------------
    uids = [p["uid"] for p in parsed]

    reg_qs = RegisteredCompetitors.objects.select_related("user").filter(
        event=event,
        status="registered",
        user__uid__in=uids
    )
    reg_map = {rc.user.uid: rc for rc in reg_qs}

    missing = [p for p in parsed if p["uid"] not in reg_map]

    # ---------------- SAVE ----------------
    stats_to_create = []
    for p in parsed:
        rc = reg_map.get(p["uid"])
        if not rc:
            continue

        placement_pts = int(placement_points.get(p["placement"], 0))
        kill_pts = int(round(p["kills"] * kill_point_value))
        total_pts = placement_pts + kill_pts

        stats_to_create.append(
            SoloPlayerMatchStats(
                match=match,
                competitor=rc,
                placement=p["placement"],
                kills=p["kills"],
                placement_points=placement_pts,
                total_points=total_pts
            )
        )

    with transaction.atomic():
        # re-upload safe: prevents duplicates and makes upload idempotent
        SoloPlayerMatchStats.objects.filter(match=match).delete()
        SoloPlayerMatchStats.objects.bulk_create(stats_to_create, batch_size=500)

    match.result_inputted = True
    match.save()

    return Response({
        "message": "Solo match results uploaded (scoring pulled from leaderboard).",
        "match_id": match.match_id,
        "leaderboard_id": leaderboard.leaderboard_id,
        "kill_point": kill_point_value,
        "placement_points_used": placement_points,
        "parsed_rows": len(parsed),
        "saved_rows": len(stats_to_create),
        "missing_count": len(missing),
        "missing_registered_competitors_preview": [
            {"uid": p["uid"], "name": p["name"], "placement": p["placement"]}
            for p in missing[:30]
        ],
    }, status=200)


@api_view(["GET"])
def get_all_leaderboards(request):
    leaderboards = Leaderboard.objects.select_related("event", "stage", "group", "creator").all()
    data = []
    for lb in leaderboards:
        data.append({
            "leaderboard_id": lb.leaderboard_id,
            "leaderboard_name": lb.leaderboard_name,
            "event": {
                "event_id": lb.event.event_id,
                "event_name": lb.event.event_name,
            },
            "stage": {
                "stage_id": lb.stage.stage_id,
                "stage_name": lb.stage.stage_name,
            },
            "group": {
                "group_id": lb.group.group_id,
                "group_name": lb.group.group_name,
            },
            "creator": {
                "user_id": lb.creator.user_id,
                "username": lb.creator.username,
            } if lb.creator else None,
            "placement_points": lb.placement_points,
            "kill_point": lb.kill_point,
            "file_type": lb.file_type,
            "leaderboard_method": lb.leaderboard_method,
            "created_at": lb.creation_date
        })
    return Response({"leaderboards": data}, status=200) 


# @api_view(["POST"])
# def reconcile_group_discord_roles(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=403)

#     group_id = request.data.get("group_id")
#     if not group_id:
#         return Response({"message": "group_id is required."}, status=400)

#     group = get_object_or_404(StageGroups, group_id=group_id)
#     role_id = group.group_discord_role_id
#     if not role_id:
#         return Response({"message": "This group has no group_discord_role_id set."}, status=400)

#     competitors = (
#         StageGroupCompetitor.objects
#         .filter(stage_group=group, player__isnull=False)
#         .select_related("player__user")
#     )

#     checked = 0
#     queued = 0
#     already_ok = 0
#     not_in_server = 0
#     failed_check = 0

#     for c in competitors:
#         user = c.player.user
#         if not user or not user.discord_id:
#             continue

#         checked += 1

#         try:
#             has_role = member_has_role(user.discord_id, role_id)
#         except Exception:
#             failed_check += 1
#             # If you want: queue anyway
#             has_role = False

#         if has_role:
#             already_ok += 1
#             continue

#         # queue assignment (idempotent-ish using DB)
#         assignment, created = DiscordRoleAssignment.objects.get_or_create(
#             user=user,
#             discord_id=user.discord_id,
#             role_id=role_id,
#             stage=group.stage,
#             group=group,
#             defaults={"status": "pending"}
#         )

#         # If it existed but failed earlier, retry it:
#         if assignment.status in ("failed", "pending"):
#             assignment.status = "pending"
#             assignment.error_message = None
#             assignment.save()
#             assign_role_from_assignment_task.delay(assignment.id)
#             queued += 1

#     return Response({
#         "message": "Reconcile finished.",
#         "group_id": group.group_id,
#         "checked": checked,
#         "already_ok": already_ok,
#         "queued": queued,
#         "failed_check": failed_check
#     }, status=200)


@api_view(["POST"])
def sync_group_discord_roles(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid Authorization"}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized"}, status=403)

    group_id = request.data.get("group_id")
    if not group_id:
        return Response({"message": "group_id is required"}, status=400)

    group = get_object_or_404(StageGroups, group_id=group_id)
    role_id = group.group_discord_role_id
    if not role_id:
        return Response({"message": "This group has no discord role id."}, status=400)

    competitors = StageGroupCompetitor.objects.select_related("player__user").filter(
        stage_group=group, player__isnull=False, status="active"
    )

    checked = 0
    queued = 0
    missing_discord = 0
    discord_errors = 0

    for sc in competitors:
        user = sc.player.user
        if not user or not user.discord_id:
            missing_discord += 1
            continue

        has_role, resp = discord_member_has_role(user.discord_id, role_id)
        checked += 1

        if has_role is None:
            discord_errors += 1
            continue

        if not has_role:
            DiscordRoleAssignment.objects.get_or_create(
                user=user,
                discord_id=user.discord_id,
                role_id=role_id,
                stage=group.stage,
                group=group,
                defaults={"status": "pending"}
            )
            queued += 1

    if queued:
        assign_group_roles_from_db_task.delay(group.stage.stage_id)

    return Response({
        "message": "Sync complete. Missing roles were queued.",
        "checked": checked,
        "queued": queued,
        "missing_discord_id": missing_discord,
        "discord_errors": discord_errors,
    }, status=200)


# @api_view(["GET"])
# def get_all_leaderboard_details_for_event(request):


@api_view(["POST"])
def reconcile_group_roles(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Forbidden."}, status=403)

    stage_id = request.data.get("stage_id")
    if not stage_id:
        return Response({"message": "stage_id is required."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id)

    created = 0
    skipped = 0

    # all players in groups
    qs = StageGroupCompetitor.objects.select_related("player__user", "stage_group").filter(
        stage_group__stage=stage,
        player__isnull=False
    )

    for sgc in qs:
        user = sgc.player.user
        group = sgc.stage_group

        if not user or not user.discord_id or not group.group_discord_role_id:
            skipped += 1
            continue

        # already success?
        exists = DiscordRoleAssignment.objects.filter(
            user=user,
            stage=stage,
            group=group,
            role_id=group.group_discord_role_id,
            status="success"
        ).exists()

        if exists:
            skipped += 1
            continue

        DiscordRoleAssignment.objects.get_or_create(
            user=user,
            discord_id=user.discord_id,
            role_id=group.group_discord_role_id,
            stage=stage,
            group=group,
            defaults={"status": "pending"}
        )
        created += 1

    # kick worker
    assign_group_roles_from_db_task.delay(stage.stage_id)

    return Response({
        "message": "Reconcile started.",
        "created_pending": created,
        "skipped": skipped
    }, status=200)


import json
from collections import defaultdict

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

# helpers
def _normalize_points_json(points_raw):
    if not points_raw:
        return {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}
    try:
        return {int(k): int(v) for k, v in (points_raw or {}).items()}
    except Exception:
        return {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}
    

# @api_view(["POST"])
# def get_all_leaderboard_details_for_event(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=401)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     # Prefetch to reduce queries
#     stages = (
#         event.stages.all()
#         .order_by("start_date")
#         .prefetch_related(
#             "groups",
#             "groups__matches",
#             "groups__leaderboards",
#         )
#     )

#     # We'll also pull stats in bulk to avoid N+1
#     # All matches in event groups:
#     all_match_ids = []
#     for st in stages:
#         for grp in st.groups.all():
#             for m in grp.matches.all():
#                 all_match_ids.append(m.match_id)

#     # SOLO stats bulk
#     solo_stats_by_match = defaultdict(list)
#     if event.participant_type == "solo" and all_match_ids:
#         for s in SoloPlayerMatchStats.objects.select_related("competitor__user", "match").filter(match_id__in=all_match_ids):
#             solo_stats_by_match[s.match_id].append(s)

#     # TEAM stats bulk
#     team_stats_by_match = defaultdict(list)
#     if event.participant_type != "solo" and all_match_ids:
#         for s in TournamentTeamMatchStats.objects.select_related("tournament_team__team", "match").filter(match_id__in=all_match_ids):
#             team_stats_by_match[s.match_id].append(s)

#     response_stages = []

#     for stage in stages:
#         stage_groups_payload = []

#         for group in stage.groups.all():
#             # Get leaderboard for this group (unique_together = event,stage,group)
#             leaderboard = group.leaderboards.filter(event=event, stage=stage, group=group).first()

#             placement_points = _normalize_points_json(getattr(leaderboard, "placement_points", {}) if leaderboard else {})
#             kill_point = float(getattr(leaderboard, "kill_point", 1.0) if leaderboard else 1.0)

#             matches_payload = []
#             overall = {}  # key -> accumulator

#             for match in group.matches.all().order_by("match_number"):
#                 match_payload = {
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "stats": []
#                 }

#                 if event.participant_type == "solo":
#                     stats = solo_stats_by_match.get(match.match_id, [])
#                     for s in stats:
#                         username = s.competitor.user.username if s.competitor and s.competitor.user else "Unknown"
#                         uid = s.competitor.user.uid if s.competitor and s.competitor.user else None

#                         # total_points already stored in SoloPlayerMatchStats (recommended)
#                         total_pts = int(getattr(s, "total_points", 0) or 0)

#                         match_payload["stats"].append({
#                             "competitor_id": s.competitor.id,
#                             "username": username,
#                             "uid": uid,
#                             "placement": s.placement,
#                             "kills": s.kills,
#                             "placement_points": s.placement_points,
#                             "kill_points": getattr(s, "kill_points", s.kills * kill_point),
#                             "total_points": total_pts,
#                         })

#                         key = s.competitor.id
#                         if key not in overall:
#                             overall[key] = {
#                                 "competitor_id": s.competitor.id,
#                                 "username": username,
#                                 "uid": uid,
#                                 "total_points": 0,
#                                 "total_kills": 0,
#                                 "matches_played": 0,
#                             }
#                         overall[key]["total_points"] += total_pts
#                         overall[key]["total_kills"] += int(s.kills or 0)
#                         overall[key]["matches_played"] += 1

#                 else:
#                     stats = team_stats_by_match.get(match.match_id, [])
#                     for s in stats:
#                         team_name = s.tournament_team.team.team_name if s.tournament_team and s.tournament_team.team else "Unknown Team"
#                         team_id = s.tournament_team.tournament_team_id if s.tournament_team else None

#                         # if you added total_points fields on model, use them.
#                         # otherwise compute on the fly from placement_points + kills * kill_point
#                         placement_pts = int(getattr(s, "placement_points", None) or placement_points.get(int(s.placement), 0))
#                         kill_pts = int(getattr(s, "kill_points", None) or (int(s.kills or 0) * kill_point))
#                         total_pts = int(getattr(s, "total_points", None) or (placement_pts + kill_pts))

#                         match_payload["stats"].append({
#                             "tournament_team_id": team_id,
#                             "team_name": team_name,
#                             "placement": s.placement,
#                             "kills": s.kills,
#                             "placement_points": placement_pts,
#                             "kill_points": kill_pts,
#                             "total_points": total_pts,
#                         })

#                         key = team_id
#                         if key not in overall:
#                             overall[key] = {
#                                 "tournament_team_id": team_id,
#                                 "team_name": team_name,
#                                 "total_points": 0,
#                                 "total_kills": 0,
#                                 "matches_played": 0,
#                             }
#                         overall[key]["total_points"] += total_pts
#                         overall[key]["total_kills"] += int(s.kills or 0)
#                         overall[key]["matches_played"] += 1

#                 matches_payload.append(match_payload)

#             # rank overall: points desc, kills desc
#             overall_list = list(overall.values())
#             overall_list.sort(
#                 key=lambda x: (x["total_points"], x["total_kills"]),
#                 reverse=True
#             )
#             for i, row in enumerate(overall_list, start=1):
#                 row["rank"] = i

#             stage_groups_payload.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "playing_date": group.playing_date,
#                 "playing_time": group.playing_time,
#                 "teams_qualifying": group.teams_qualifying,
#                 "group_discord_role_id": group.group_discord_role_id,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": {
#                     "leaderboard_id": leaderboard.leaderboard_id if leaderboard else None,
#                     "placement_points": placement_points,
#                     "kill_point": kill_point,
#                 },
#                 "matches": matches_payload,
#                 "overall_leaderboard": overall_list,
#             })

#         response_stages.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "start_date": stage.start_date,
#             "end_date": stage.end_date,
#             "stage_discord_role_id": stage.stage_discord_role_id,
#             "groups": stage_groups_payload,
#         })

#     return Response({
#         "event": {
#             "event_id": event.event_id,
#             "event_name": event.event_name,
#             "participant_type": event.participant_type,
#         },
#         "stages": response_stages
#     }, status=200)



from django.db.models import Sum, Count, F
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

# @api_view(["POST"])
# def get_all_leaderboard_details_for_event(request):
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)

#     admin = validate_token(auth.split(" ")[1])
#     if not admin:
#         return Response({"message": "Invalid or expired session token."}, status=401)
#     if admin.role != "admin":
#         return Response({"message": "You do not have permission."}, status=403)

#     event_id = request.data.get("event_id")
#     if not event_id:
#         return Response({"message": "event_id is required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)

#     stages_payload = []

#     stages = event.stages.all().order_by("stage_id")
#     for stage in stages:
#         groups_payload = []
#         groups = stage.groups.all().order_by("group_id")

#         for group in groups:
#             # leaderboard config (unique_together means at most 1)
#             leaderboard = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

#             matches = Match.objects.filter(group=group).order_by("match_number")

#             matches_payload = []
#             for match in matches:
#                 if event.participant_type == "solo":
#                     match_stats = (SoloPlayerMatchStats.objects
#                                    .filter(match=match)
#                                    .select_related("competitor__user")
#                                    .values(
#                                        "competitor_id",
#                                        "competitor__user__username",
#                                        "placement",
#                                        "kills",
#                                        "placement_points",
#                                        "kill_points",
#                                        "total_points",
#                                    )
#                                    .order_by("placement"))
#                 else:
#                     match_stats = (TournamentTeamMatchStats.objects
#                                    .filter(match=match)
#                                    .select_related("tournament_team__team")
#                                    .values(
#                                        "tournament_team_id",
#                                        "tournament_team__team__team_name",
#                                        "placement",
#                                        "kills",
#                                        "placement_points",
#                                        "kill_points",
#                                        "total_points",
#                                    )
#                                    .order_by("placement"))

#                 matches_payload.append({
#                     "match_id": match.match_id,
#                     "match_number": match.match_number,
#                     "match_map": match.match_map,
#                     "result_inputted": match.result_inputted,
#                     "room_id": match.room_id,
#                     "room_name": match.room_name,
#                     "room_password": match.room_password,
#                     "stats": list(match_stats),
#                 })

#             # overall leaderboard for this group
#             if event.participant_type == "solo":
#                 overall = SoloPlayerMatchStats.objects.filter(match__group__stage__event=event).values(
#                     "match__group_id",
#                     "competitor_id",  # keep the real field name here in values()
#                     "competitor__user__username",
#                     "matches_played",
#                     "total_kills"
#                     ).annotate(
#                         comp_id=F("competitor_id"),          # ✅ safe alias
#                         total_pts=Sum("total_points"),
#                     ).order_by("-total_points", "-kills", "competitor__user__username")
#             else:
#                 overall = (TournamentTeamMatchStats.objects
#                            .filter(match__group=group)
#                            .values(
#                                tournament_team_id=F("tournament_team_id"),
#                                team_name=F("tournament_team__team__team_name"),
#                            )
#                            .annotate(
#                                matches_played=Count("match_id"),
#                                total_points=Sum("total_points"),
#                                total_kills=Sum("kills"),
#                            )
#                            .order_by("-total_points", "-total_kills", "team_name"))

#             groups_payload.append({
#                 "group_id": group.group_id,
#                 "group_name": group.group_name,
#                 "teams_qualifying": group.teams_qualifying,
#                 "match_count": group.match_count,
#                 "match_maps": group.match_maps,
#                 "leaderboard": None if not leaderboard else {
#                     "leaderboard_id": leaderboard.leaderboard_id,
#                     "leaderboard_name": leaderboard.leaderboard_name,
#                     "kill_point": leaderboard.kill_point,
#                     "placement_points": leaderboard.placement_points,
#                     "leaderboard_method": leaderboard.leaderboard_method,
#                     "file_type": leaderboard.file_type,
#                     "last_updated": leaderboard.last_updated,
#                 },
#                 "matches": matches_payload,
#                 "overall_leaderboard": list(overall),
#             })

#         stages_payload.append({
#             "stage_id": stage.stage_id,
#             "stage_name": stage.stage_name,
#             "stage_format": stage.stage_format,
#             "stage_status": stage.stage_status,
#             "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
#             "groups": groups_payload
#         })

#     return Response({
#         "event_id": event.event_id,
#         "event_name": event.event_name,
#         "participant_type": event.participant_type,
#         "stages": stages_payload
#     }, status=200)



from django.db.models import Sum, Count, F, Value, IntegerField
from django.db.models.functions import Coalesce

@api_view(["POST"])
def get_all_leaderboard_details_for_event(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission."}, status=403)

    event_id = request.data.get("event_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    stages_payload = []
    stages = event.stages.all().order_by("stage_id")

    for stage in stages:
        groups_payload = []
        groups = stage.groups.all().order_by("group_id")

        for group in groups:
            leaderboard = Leaderboard.objects.filter(event=event, stage=stage, group=group).first()
            matches = Match.objects.filter(group=group).order_by("match_number")

            matches_payload = []
            for match in matches:
                if event.participant_type == "solo":
                    match_stats = (
                        SoloPlayerMatchStats.objects
                        .filter(match=match)
                        .select_related("competitor__user")
                        .annotate(
                            username=F("competitor__user__username"),
                            effective_total=(
                                F("placement_points") + F("kill_points") +
                                Coalesce(F("bonus_points"), Value(0), output_field=IntegerField()) -
                                Coalesce(F("penalty_points"), Value(0), output_field=IntegerField())
                            )
                        )
                        .values(
                            "competitor_id",
                            "username",
                            "placement",
                            "kills",
                            "placement_points",
                            "kill_points",
                            "bonus_points",
                            "penalty_points",
                            "total_points",
                            "effective_total",
                        )
                        .order_by("-effective_total", "-kills", "username")
                    )
                else:
                    match_stats = (
                        TournamentTeamMatchStats.objects
                        .filter(match=match)
                        .select_related("tournament_team__team")
                        .annotate(
                            team_name=F("tournament_team__team__team_name"),
                        )
                        .values(
                            "tournament_team_id",
                            "team_name",
                            "placement",
                            "kills",
                            "placement_points",
                            "kill_points",
                            "total_points",
                        )
                        .order_by("-total_points", "-kills", "team_name")
                    )

                matches_payload.append({
                    "match_id": match.match_id,
                    "match_number": match.match_number,
                    "match_map": match.match_map,
                    "result_inputted": match.result_inputted,
                    "room_id": match.room_id,
                    "room_name": match.room_name,
                    "room_password": match.room_password,
                    "stats": list(match_stats),
                })

            # ✅ overall leaderboard PER GROUP (not whole event)
            if event.participant_type == "solo":
                overall = (
                    SoloPlayerMatchStats.objects
                    .filter(match__group=group)
                    .select_related("competitor__user")
                    .values(
                        "competitor_id",
                        "competitor__user__username",
                    )
                    .annotate(
                        matches_played=Count("match_id"),
                        total_kills=Coalesce(Sum("kills"), 0),
                        bonus_sum=Coalesce(Sum("bonus_points"), 0),
                        penalty_sum=Coalesce(Sum("penalty_points"), 0),
                        total_points=Coalesce(Sum("total_points"), 0),
                        effective_total=(
                            Coalesce(Sum("total_points"), 0) +
                            Coalesce(Sum("bonus_points"), 0) -
                            Coalesce(Sum("penalty_points"), 0)
                        )
                    )
                    .order_by("-effective_total", "-total_kills", "competitor__user__username")
                )
            else:
                overall = (
                    TournamentTeamMatchStats.objects
                    .filter(match__group=group)
                    .values(
                        "tournament_team_id",
                        team_name=F("tournament_team__team__team_name"),
                    )
                    .annotate(
                        matches_played=Count("match_id"),
                        total_kills=Coalesce(Sum("kills"), 0),
                        total_points=Coalesce(Sum("total_points"), 0),
                    )
                    .order_by("-total_points", "-total_kills", "team_name")
                )

            groups_payload.append({
                "group_id": group.group_id,
                "group_name": group.group_name,
                "teams_qualifying": group.teams_qualifying,
                "match_count": group.match_count,
                "match_maps": group.match_maps,
                "leaderboard": None if not leaderboard else {
                    "leaderboard_id": leaderboard.leaderboard_id,
                    "leaderboard_name": leaderboard.leaderboard_name,
                    "kill_point": leaderboard.kill_point,
                    "placement_points": leaderboard.placement_points,
                    "leaderboard_method": leaderboard.leaderboard_method,
                    "file_type": leaderboard.file_type,
                    "last_updated": leaderboard.last_updated,
                },
                "matches": matches_payload,
                "overall_leaderboard": list(overall),
            })

        stages_payload.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "stage_format": stage.stage_format,
            "stage_status": stage.stage_status,
            "teams_qualifying_from_stage": stage.teams_qualifying_from_stage,
            "groups": groups_payload
        })

    return Response({
        "event_id": event.event_id,
        "event_name": event.event_name,
        "participant_type": event.participant_type,
        "stages": stages_payload
    }, status=200)




import uuid
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response

def _get_group_leaderboard(event, stage, group):
    return Leaderboard.objects.filter(event=event, stage=stage, group=group).first()

# @api_view(["POST"])
# def advance_group_competitors_to_next_stage(request):
#     # ---------------- AUTH ----------------
#     auth = request.headers.get("Authorization")
#     if not auth or not auth.startswith("Bearer "):
#         return Response({"message": "Invalid or missing Authorization token."}, status=400)
#     admin = validate_token(auth.split(" ")[1])
#     if not admin or admin.role != "admin":
#         return Response({"message": "Unauthorized."}, status=401)

#     event_id = request.data.get("event_id")
#     group_id = request.data.get("group_id")
#     next_stage_id = request.data.get("next_stage_id")
#     remove_old_group_role = bool(request.data.get("remove_old_group_role", False))

#     if not event_id or not group_id or not next_stage_id:
#         return Response({"message": "event_id, group_id, next_stage_id are required."}, status=400)

#     event = get_object_or_404(Event, event_id=event_id)
#     group = get_object_or_404(StageGroups, group_id=group_id)
#     current_stage = group.stage

#     if current_stage.event_id != event.event_id:
#         return Response({"message": "This group does not belong to the provided event."}, status=400)

#     next_stage = get_object_or_404(Stages, stage_id=next_stage_id, event=event)

#     matches = list(group.matches.all().order_by("match_number"))
#     if not matches:
#         return Response({"message": "No matches found for this group."}, status=400)

#     # Validate results uploaded for ALL matches
#     if event.participant_type == "solo":
#         missing_matches = []
#         for m in matches:
#             if not SoloPlayerMatchStats.objects.filter(match=m).exists():
#                 missing_matches.append(m.match_number)
#         if missing_matches:
#             return Response({
#                 "message": "Cannot advance. Some matches have no uploaded results.",
#                 "missing_match_numbers": missing_matches
#             }, status=400)
#     else:
#         missing_matches = []
#         for m in matches:
#             if not TournamentTeamMatchStats.objects.filter(match=m).exists():
#                 missing_matches.append(m.match_number)
#         if missing_matches:
#             return Response({
#                 "message": "Cannot advance. Some matches have no uploaded results.",
#                 "missing_match_numbers": missing_matches
#             }, status=400)

#     leaderboard = _get_group_leaderboard(event, current_stage, group)
#     if not leaderboard:
#         return Response({"message": "Leaderboard not found for this group."}, status=400)

#     placement_points = _normalize_points_json(leaderboard.placement_points or {})
#     kill_point = float(leaderboard.kill_point or 1.0)

#     # Compute OVERALL standings for this group
#     if event.participant_type == "solo":
#         # Use stored total_points (fast)
#         totals = {}
#         qs = (
#             SoloPlayerMatchStats.objects
#             .select_related("competitor__user", "match")
#             .filter(match__group=group)
#         )
#         for s in qs:
#             cid = s.competitor_id
#             username = s.competitor.user.username if s.competitor and s.competitor.user else "Unknown"
#             uid = s.competitor.user.uid if s.competitor and s.competitor.user else None

#             total_pts = int(getattr(s, "total_points", 0) or 0)
#             kills = int(s.kills or 0)

#             if cid not in totals:
#                 totals[cid] = {
#                     "competitor": s.competitor,
#                     "competitor_id": cid,
#                     "username": username,
#                     "uid": uid,
#                     "total_points": 0,
#                     "total_kills": 0,
#                 }
#             totals[cid]["total_points"] += total_pts
#             totals[cid]["total_kills"] += kills

#         ranked = list(totals.values())
#         ranked.sort(key=lambda x: (x["total_points"], x["total_kills"]), reverse=True)

#         qualifying_n = int(group.teams_qualifying or 0)
#         qualified = ranked[:qualifying_n]

#     else:
#         totals = {}
#         qs = (
#             TournamentTeamMatchStats.objects
#             .select_related("tournament_team__team", "match")
#             .filter(match__group=group)
#         )
#         for s in qs:
#             tid = s.tournament_team_id
#             team_name = s.tournament_team.team.team_name if s.tournament_team and s.tournament_team.team else "Unknown Team"

#             placement_pts = int(getattr(s, "placement_points", None) or placement_points.get(int(s.placement), 0))
#             kill_pts = int(getattr(s, "kill_points", None) or (int(s.kills or 0) * kill_point))
#             total_pts = int(getattr(s, "total_points", None) or (placement_pts + kill_pts))

#             if tid not in totals:
#                 totals[tid] = {
#                     "tournament_team": s.tournament_team,
#                     "tournament_team_id": tid,
#                     "team_name": team_name,
#                     "total_points": 0,
#                     "total_kills": 0,
#                 }
#             totals[tid]["total_points"] += total_pts
#             totals[tid]["total_kills"] += int(s.kills or 0)

#         ranked = list(totals.values())
#         ranked.sort(key=lambda x: (x["total_points"], x["total_kills"]), reverse=True)

#         qualifying_n = int(group.teams_qualifying or 0)
#         qualified = ranked[:qualifying_n]

#     if qualifying_n <= 0:
#         return Response({"message": "group.teams_qualifying must be > 0."}, status=400)

#     # Seed into next stage + queue Discord role assignments
#     created_stage_competitors = 0
#     queued_roles = 0

#     with transaction.atomic():
#         # progress row for next-stage role assignment
#         progress = DiscordStageRoleAssignmentProgress.objects.create(
#             stage=next_stage,
#             total=len(qualified),
#             status="running"
#         )

#         for row in qualified:
#             if event.participant_type == "solo":
#                 reg = row["competitor"]

#                 sc, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     player=reg,
#                     defaults={"status": "active"}
#                 )
#                 if created:
#                     created_stage_competitors += 1

#                 user = reg.user if reg else None
#                 if user and user.discord_id and next_stage.stage_discord_role_id:
#                     DiscordRoleAssignment.objects.get_or_create(
#                         user=user,
#                         discord_id=user.discord_id,
#                         role_id=next_stage.stage_discord_role_id,
#                         stage=next_stage,
#                         group=None,
#                         defaults={"status": "pending"}
#                     )
#                     queued_roles += 1

#                 # optionally remove old group role
#                 if remove_old_group_role and user and user.discord_id and group.group_discord_role_id:
#                     remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

#             else:
#                 tteam = row["tournament_team"]

#                 sc, created = StageCompetitor.objects.get_or_create(
#                     stage=next_stage,
#                     tournament_team=tteam,
#                     defaults={"status": "active"}
#                 )
#                 if created:
#                     created_stage_competitors += 1

#                 # assign next stage role to ALL members of the team
#                 members = TournamentTeamMember.objects.select_related("user").filter(tournament_team=tteam)
#                 for tm in members:
#                     user = tm.user
#                     if user and user.discord_id and next_stage.stage_discord_role_id:
#                         DiscordRoleAssignment.objects.get_or_create(
#                             user=user,
#                             discord_id=user.discord_id,
#                             role_id=next_stage.stage_discord_role_id,
#                             stage=next_stage,
#                             group=None,
#                             defaults={"status": "pending"}
#                         )
#                         queued_roles += 1

#                     if remove_old_group_role and user and user.discord_id and group.group_discord_role_id:
#                         remove_group_role_task.delay(user.discord_id, group.group_discord_role_id)

#     # kick DB-worker to process queued roles
#     # (this is your safer “100% chance” style because it retries + tracks status in DB)
#     assign_stage_roles_from_db_task.delay(str(progress.id), next_stage.stage_id)

#     return Response({
#         "message": "Advanced group to next stage.",
#         "event_id": event.event_id,
#         "from_group": {"group_id": group.group_id, "group_name": group.group_name},
#         "to_stage": {"stage_id": next_stage.stage_id, "stage_name": next_stage.stage_name},
#         "qualified_count": len(qualified),
#         "created_stage_competitors": created_stage_competitors,
#         "queued_next_stage_role_assignments": queued_roles,
#         "progress_id": str(progress.id),
#     }, status=200)


from django.db import transaction
from django.db.models import Sum, F
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

@api_view(["POST"])
def advance_group_competitors_to_next_stage(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    if admin.role != "admin":
        return Response({"message": "You do not have permission."}, status=403)

    event_id = request.data.get("event_id")
    group_id = request.data.get("group_id")

    if not event_id or not group_id:
        return Response({"message": "event_id and group_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)
    group = get_object_or_404(StageGroups, group_id=group_id, stage__event=event)
    stage = group.stage

    # 1) ensure all match results uploaded
    matches = Match.objects.filter(group=group).order_by("match_number")
    if not matches.exists():
        return Response({"message": "No matches in this group."}, status=400)

    not_done = matches.filter(result_inputted=False).count()
    if not_done > 0:
        return Response({
            "message": "Cannot advance yet. Some matches have no results uploaded.",
            "missing_results_matches_count": not_done
        }, status=400)

    # 2) get next stage
    next_stage = (Stages.objects
                  .filter(event=event, stage_id__gt=stage.stage_id)
                  .order_by("stage_id")
                  .first())
    if not next_stage:
        return Response({"message": "No next stage found after this stage."}, status=400)

    qualify_n = int(group.teams_qualifying or 0)
    if qualify_n <= 0:
        return Response({"message": "group.teams_qualifying must be > 0."}, status=400)

    # 3) compute winners
    if event.participant_type == "solo":
        overall = (SoloPlayerMatchStats.objects
                   .filter(match__group=group)
                   .values("competitor_id")
                   .annotate(total_points=Sum("total_points"), total_kills=Sum("kills"))
                   .order_by("-total_points", "-total_kills")[:qualify_n])

        winner_ids = [row["competitor_id"] for row in overall]  # RegisteredCompetitors IDs
    else:
        overall = (TournamentTeamMatchStats.objects
                   .filter(match__group=group)
                   .values("tournament_team_id")
                   .annotate(total_points=Sum("total_points"), total_kills=Sum("kills"))
                   .order_by("-total_points", "-total_kills")[:qualify_n])

        winner_ids = [row["tournament_team_id"] for row in overall]  # TournamentTeam IDs

    if not winner_ids:
        return Response({"message": "No winners found (no stats?)."}, status=400)

    # 4) seed into next stage + queue discord roles
    created_count = 0
    queued_roles = 0

    with transaction.atomic():
        if event.participant_type == "solo":
            winners = RegisteredCompetitors.objects.select_related("user").filter(id__in=winner_ids)
            for rc in winners:
                obj, created = StageCompetitor.objects.get_or_create(
                    stage=next_stage,
                    player=rc,
                    tournament_team=None,
                    defaults={"status": "active"}
                )
                if created:
                    created_count += 1

                if rc.user and rc.user.discord_id and next_stage.stage_discord_role_id:
                    DiscordRoleAssignment.objects.get_or_create(
                        user=rc.user,
                        discord_id=rc.user.discord_id,
                        role_id=next_stage.stage_discord_role_id,
                        stage=next_stage,
                        group=None,
                        defaults={"status": "pending"}
                    )
                    queued_roles += 1
        else:
            winners = TournamentTeam.objects.select_related("team").filter(tournament_team_id__in=winner_ids)
            for tt in winners:
                obj, created = StageCompetitor.objects.get_or_create(
                    stage=next_stage,
                    tournament_team=tt,
                    player=None,
                    defaults={"status": "active"}
                )
                if created:
                    created_count += 1

                # queue stage role for each team member
                members = tt.members.select_related("user").all()
                for m in members:
                    if m.user and m.user.discord_id and next_stage.stage_discord_role_id:
                        DiscordRoleAssignment.objects.get_or_create(
                            user=m.user,
                            discord_id=m.user.discord_id,
                            role_id=next_stage.stage_discord_role_id,
                            stage=next_stage,
                            group=None,
                            defaults={"status": "pending"}
                        )
                        queued_roles += 1

    # 5) kick off worker batch processing
    if next_stage.stage_discord_role_id:
        progress = DiscordStageRoleAssignmentProgress.objects.create(
            stage=next_stage,
            total=queued_roles,
            status="running"
        )
        assign_stage_roles_from_db_task.delay(str(progress.id), next_stage.stage_id)

    return Response({
        "message": "Advanced winners to next stage.",
        "from_group": group.group_name,
        "to_stage": next_stage.stage_name,
        "qualified": qualify_n,
        "seeded_into_next_stage": created_count,
        "discord_roles_queued": queued_roles,
    }, status=200)


@api_view(["POST"])
def edit_leaderboard(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized."}, status=403)

    leaderboard_id = request.data.get("leaderboard_id")
    if not leaderboard_id:
        return Response({"message": "leaderboard_id is required."}, status=400)

    lb = get_object_or_404(Leaderboard, leaderboard_id=leaderboard_id)

    # optional updates
    new_stage_id = request.data.get("stage_id")
    new_group_id = request.data.get("group_id")
    new_name = request.data.get("leaderboard_name")
    placement_points_raw = request.data.get("placement_points")
    kill_point_raw = request.data.get("kill_point")

    if new_name is not None:
        lb.leaderboard_name = new_name

    if kill_point_raw is not None:
        lb.kill_point = float(kill_point_raw)

    if placement_points_raw is not None:
        if isinstance(placement_points_raw, str):
            placement_points_raw = json.loads(placement_points_raw)
        if not isinstance(placement_points_raw, dict):
            return Response({"message": "placement_points must be a JSON object."}, status=400)
        lb.placement_points = placement_points_raw

    if new_stage_id is not None:
        stage = get_object_or_404(Stages, stage_id=new_stage_id, event=lb.event)
        lb.stage = stage

    if new_group_id is not None:
        group = get_object_or_404(StageGroups, group_id=new_group_id, stage=lb.stage)
        lb.group = group

    # ensure unique_together not violated
    exists = Leaderboard.objects.filter(
        event=lb.event,
        stage=lb.stage,
        group=lb.group
    ).exclude(leaderboard_id=lb.leaderboard_id).exists()
    if exists:
        return Response({"message": "A leaderboard already exists for this event/stage/group."}, status=400)

    lb.save()

    # Optional: link matches in that group to this leaderboard
    if lb.group:
        Match.objects.filter(group=lb.group).update(leaderboard=lb)

    return Response({"message": "Leaderboard updated.", "leaderboard_id": lb.leaderboard_id}, status=200)



@api_view(["POST"])
def edit_solo_match_result(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin or admin.role != "admin":
        return Response({"message": "Unauthorized."}, status=403)

    match_id = request.data.get("match_id")
    rows = request.data.get("rows")

    if not match_id or not isinstance(rows, list):
        return Response({"message": "match_id and rows(list) are required."}, status=400)

    match = get_object_or_404(Match, match_id=match_id)
    if not match.group:
        return Response({"message": "Match must be linked to a group."}, status=400)

    event = match.group.stage.event
    if event.participant_type != "solo":
        return Response({"message": "This endpoint is for solo only."}, status=400)

    # leaderboard scoring config
    lb = match.leaderboard or Leaderboard.objects.filter(
        event=event, stage=match.group.stage, group=match.group
    ).first()
    if not lb:
        return Response({"message": "Leaderboard not found for this match."}, status=400)

    placement_points = {int(k): int(v) for k, v in (lb.placement_points or {}).items()}
    if not placement_points:
        placement_points = {1:12,2:9,3:8,4:7,5:6,6:5,7:4,8:3,9:2,10:1}
    kill_point = float(lb.kill_point or 1.0)

    with transaction.atomic():
        for r in rows:
            competitor_id = r.get("competitor_id")
            if not competitor_id:
                continue

            stats = get_object_or_404(SoloPlayerMatchStats, match=match, competitor_id=competitor_id)

            if "placement" in r:
                stats.placement = int(r["placement"])
            if "kills" in r:
                stats.kills = int(r["kills"])

            if "bonus_points" in r:
                stats.bonus_points = int(r["bonus_points"] or 0)
            if "penalty_points" in r:
                stats.penalty_points = int(r["penalty_points"] or 0)

            # recalc points
            stats.placement_points = placement_points.get(stats.placement, 0)
            stats.kill_points = int(stats.kills * kill_point)
            stats.total_points = stats.placement_points + stats.kill_points + stats.bonus_points - stats.penalty_points

            stats.save()

        match.result_inputted = True
        match.save(update_fields=["result_inputted"])

    return Response({"message": "Solo match result updated.", "match_id": match.match_id}, status=200)
