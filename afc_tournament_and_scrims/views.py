from datetime import date
import json
from afc import settings
from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_date

from afc_auth.views import assign_discord_role, check_discord_membership
from afc_team.models import Team, TeamMembers
from .models import Event, Leaderboard, RegisteredCompetitors, StageGroups, Stages, StreamChannel
from afc_auth.models import User
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_auth.models import User
from django.core.exceptions import ObjectDoesNotExist
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

    user = User.objects.filter(login_session_token=session_token).first()
    if not user:
        return Response({"error": "Invalid session"}, status=status.HTTP_401_UNAUTHORIZED)

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
    events = Event.objects.all()
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

    events = Event.objects.all().order_by("-start_date")
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
    events = Event.objects.all()
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

    events = Event.objects.all().order_by("-start_date")
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
    tournaments = Event.objects.filter(competition_type="tournament")
    scrims = Event.objects.filter(competition_type="scrim")

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

    tournaments = Event.objects.filter(competition_type="tournament").order_by("-start_date")
    scrims = Event.objects.filter(competition_type="scrim").order_by("-start_date")

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
    try:
        user = User.objects.get(session_token=token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=401)

    # Permissions
    if user.role not in ["admin", "moderator", "support"] and not user.userroles.filter(role_name__in=["event_admin", "head_admin"]).exists():
        return Response({"message": "You do not have permission to create an event."}, status=403)

    # Extract event data
    required_fields = [
        "competition_type", "participant_type", "event_type",
        "max_teams_or_players", "event_name",
        "event_mode", "start_date", "end_date",
        "registration_open_date", "registration_end_date",
        "prizepool", "number_of_stages"
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
        uploaded_rules=request.FILES.get("uploaded_rules")
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
            teams_qualifying_from_stage=stage_data["teams_qualifying_from_stage"]
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
            )

    return Response({
        "message": "Event created successfully.",
        "event_id": event.event_id
    }, status=201)


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
    try:
        user = User.objects.get(session_token=token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=401)

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

        # Delete all existing stages/groups
        Stages.objects.filter(event=event).delete()

        # Recreate stages + groups
        for stage_data in stages_data:
            stage = Stages.objects.create(
                event=event,
                stage_name=stage_data["stage_name"],
                start_date=parse_date(stage_data["start_date"]),
                end_date=parse_date(stage_data["end_date"]),
                number_of_groups=stage_data["number_of_groups"],
                stage_format=stage_data["stage_format"],
                teams_qualifying_from_stage=stage_data["teams_qualifying_from_stage"]
            )

            # Add groups
            for group in stage_data.get("groups", []):
                StageGroups.objects.create(
                    stage=stage,
                    group_name=group["group_name"],
                    playing_date=parse_date(group["playing_date"]),
                    playing_time=group["playing_time"],
                    teams_qualifying=group["teams_qualifying"]
                )

    return Response({
        "message": "Event updated successfully.",
        "event_id": event.event_id
    }, status=200)


@api_view(["GET"])
def get_total_events_count(request):
    total_events = Event.objects.count()
    return Response({"total_events": total_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_total_tournaments_count(request):
    total_tournaments = Event.objects.filter(competition_type="tournament").count()
    return Response({"total_tournaments": total_tournaments}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_total_scrims_count(request):
    total_scrims = Event.objects.filter(competition_type="scrim").count()
    return Response({"total_scrims": total_scrims}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_upcoming_events_count(request):
    upcoming_events = Event.objects.filter(event_status="upcoming").count()
    return Response({"upcoming_events": upcoming_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_ongoing_events_count(request):
    ongoing_events = Event.objects.filter(event_status="ongoing").count()
    return Response({"ongoing_events": ongoing_events}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_completed_events_count(request):
    completed_events = Event.objects.filter(event_status="completed").count()
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
                    "status": "registered"
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
                "matches": matches_data
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
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({'status': 'error', 'message': 'Invalid or expired session token'}, status=401)

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
