from datetime import date
import json

from celery import shared_task
from afc import settings
from django.shortcuts import get_object_or_404, render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_date

from afc_auth.views import assign_discord_role, check_discord_membership, remove_discord_role, validate_token
# from afc_leaderboard_calc.models import Match, MatchLeaderboard
from afc_team.models import Team, TeamMembers
from .models import Event, RegisteredCompetitors, StageCompetitor, StageGroupCompetitor, StageGroups, Stages, StreamChannel, TournamentTeam, Leaderboard, TournamentTeamMatchStats, Match
from afc_auth.models import Notifications, User
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



@api_view(["POST"])
def create_leaderboard(request):
    session_token = request.headers.get("Authorization")
    
    if not session_token:
        return Response({"error": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # Ensure only admins and moderators can create leaderboards
    if user.role not in ["admin", "moderator"]:
        return Response({"error": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)

    leaderboard_name = request.data.get("leaderboard_name")
    event_id = request.data.get("event_id")
    stage = request.data.get("stage")
    group = request.data.get("group", None)  # Optional field

    if not all([leaderboard_name, event_id, stage]):
        return Response({"error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        event = Event.objects.get(event_id=event_id)
    except ObjectDoesNotExist:
        return Response({"error": "Event not found"}, status=status.HTTP_404_NOT_FOUND)

    leaderboard = Leaderboard.objects.create(
        leaderboard_name=leaderboard_name,
        event=event,
        stage=stage,
        group=group,
        creator=user
    )

    return Response({"message": "Leaderboard created successfully", "leaderboard_id": leaderboard.leaderboard_id}, status=status.HTTP_201_CREATED)


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
                        "map_name": match.map_name,
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


@shared_task(bind=True, rate_limit="1/s")
def assign_stage_role_task(self, discord_id, role_id):
    assign_discord_role(discord_id, role_id)

@shared_task(bind=True, rate_limit="1/s")
def assign_group_role_task(self, discord_id, role_id):
    assign_discord_role(discord_id, role_id)

@shared_task(bind=True, rate_limit="1/s")
def remove_group_role_task(self, discord_id, role_id):  
    remove_discord_role(discord_id, role_id)

@api_view(["POST"])
def seed_solo_players_to_stage(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = auth.split(" ")[1]
    admin = validate_token(token)

    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)

    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    event_id = request.data.get("event_id")
    stage_id = request.data.get("stage_id")

    if not event_id or not stage_id:
        return Response({"message": "event_id and stage_id are required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    if event.participant_type != "solo":
        return Response({"message": "This event is not a solo event."}, status=400)

    stage = get_object_or_404(Stages, stage_id=stage_id, event=event)

    solo_players = RegisteredCompetitors.objects.filter(
        event=event,
        user__isnull=False,
        team__isnull=True,
        status="registered"
    )

    seeded_count = 0

    error_disc = []

    for reg in solo_players:
        # ✅ Avoid duplicate StageCompetitor
        obj, created = StageCompetitor.objects.get_or_create(
            stage=stage,
            player=reg,
            defaults={"status": "active"}
        )

        if created:
            seeded_count += 1

            # ✅ Assign Discord role in background
            if reg.user.discord_id and stage.stage_discord_role_id:
                try:
                    assign_stage_role_task.delay(
                        reg.user.discord_id,
                        stage.stage_discord_role_id
                    )
                except Exception as e:
                    # Log error, but don't fail the whole seeding
                    error_disc.append(f"Failed to queue Discord role for {reg.user.username}: {e}")
                    print(f"Failed to queue Discord role for {reg.user.username}: {e}")

    stage.stage_status = "ongoing"
    stage.save()

    return Response({
        "message": f"Seeded {seeded_count} solo players into stage '{stage.stage_name}'.",
        "errors": error_disc
    }, status=200)



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


@api_view(["POST"])
def seed_stage_competitors_to_groups(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    token = auth.split(" ")[1]
    admin = validate_token(token)

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

    # Get all active StageCompetitors (players only)
    competitors = list(stage.competitors.filter(status="active", player__isnull=False))
    if not competitors:
        return Response({"message": "No competitors found to seed."}, status=400)

    shuffle(competitors)  # randomize

    group_count = len(groups)
    seeded_count = 0

    for idx, competitor in enumerate(competitors):
        group = groups[idx % group_count]

        # Create or skip if already in group
        obj, created = StageGroupCompetitor.objects.get_or_create(
            stage_group=group,
            player=competitor.player,
            defaults={"status": "active"}
        )

        if created:
            seeded_count += 1

            # Assign discord role safely
            if competitor.player.user and competitor.player.user.discord_id and group.group_discord_role_id:
                assign_group_role_task.delay(competitor.player.user.discord_id, group.group_discord_role_id)

    return Response({
        "message": f"Seeded {seeded_count} competitors into {group_count} groups for stage '{stage.stage_name}'."
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


@api_view(["POST"])
def send_match_room_details_notification_to_competitor(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)
    
    token = session_token.split(" ")[1]
    admin = validate_token(token)
    if not admin:
        return Response({"message": "Invalid or expired session token."}, status=401)
    
    if admin.role != "admin":
        return Response({"message": "You do not have permission to perform this action."}, status=403)

    # ---------------- EVENT ----------------
    event_id = request.data.get("event_id")
    group_id = request.data.get("group_id")
    if not event_id:
        return Response({"message": "event_id is required."}, status=400)

    event = get_object_or_404(Event, event_id=event_id)

    # ---------------- GET MATCHES ----------------
    matches = Match.objects.filter(group=group_id)
    if not matches.exists():
        return Response({"message": "No matches found for this event."}, status=400)

    total_notifications = 0

    for match in matches:
        # Ensure match has room details
        if not match.room_id or not match.room_name or not match.room_password:
            continue

        # Solo event
        if event.participant_type == "solo":
            competitors = StageGroupCompetitor.objects.filter(stage_group=match.group, player__isnull=False)
            for sc in competitors:
                user = sc.player.user
                if user:
                    message = (
                        f"Hello {user.username}, your match details for '{event.event_name}' (Stage: {match.group.stage.stage_name}, "
                        f"Group: {match.group.group_name}, Match: {match.match_number}) are:\n"
                        f"Room ID: {match.room_id}\n"
                        f"Room Name: {match.room_name}\n"
                        f"Password: {match.room_password}"
                    )
                    Notifications.objects.create(user=user, message=message)
                    total_notifications += 1

        # Team event
        else:
            teams = StageGroupCompetitor.objects.filter(stage_group=match.group, tournament_team__isnull=False)
            for sgc in teams:
                team = sgc.tournament_team
                for player in team.players.all():
                    if player.user:
                        message = (
                            f"Hello {player.user.username}, your match details for '{event.event_name}' (Stage: {match.group.stage.stage_name}, "
                            f"Group: {match.group.group_name}, Match: {match.match_number}) are:\n"
                            f"Room ID: {match.room_id}\n"
                            f"Room Name: {match.room_name}\n"
                            f"Password: {match.room_password}"
                        )
                        Notifications.objects.create(user=player.user, message=message)
                        total_notifications += 1

    return Response({
        "message": f"Sent match room notifications to {total_notifications} users for event '{event.event_name}'."
    }, status=200)



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