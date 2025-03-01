from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_date
from .models import Event, Leaderboard
from afc_auth.models import User
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_auth.models import User
from django.core.exceptions import ObjectDoesNotExist
# Create your views here.


@api_view(["POST"])
def create_event(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Check if the user has permission to create an event
    if user.role not in ["admin", "moderator", "support"]:
        return Response({"message": "You do not have permission to create an event."}, status=status.HTTP_403_FORBIDDEN)

    # Extract event details
    event_name = request.data.get("event_name")
    event_type = request.data.get("event_type")
    event_format = request.data.get("format")  # Matching model field name
    location = request.data.get("location")
    start_date = parse_date(request.data.get("start_date"))
    end_date = parse_date(request.data.get("end_date"))
    registration_open_date = parse_date(request.data.get("registration_open_date"))
    registration_end_date = parse_date(request.data.get("registration_end_date"))
    prizepool = request.data.get("prizepool")
    prize_distribution = request.data.get("prize_distribution", {})
    event_rules = request.data.get("event_rules")
    event_status = request.data.get("event_status", "upcoming")
    registration_link = request.data.get("registration_link")
    tournament_tier = request.data.get("tournament_tier")
    event_banner = request.FILES.get("event_banner")
    stream_channel = request.data.get("stream_channel")

    # Validate required fields
    required_fields = [event_name, event_type, event_format, location, start_date, end_date, registration_open_date, 
                       registration_end_date, prizepool, event_rules, registration_link, tournament_tier]
    
    if any(field is None for field in required_fields):
        return Response({"message": "All required fields must be provided."}, status=status.HTTP_400_BAD_REQUEST)

    # Ensure registration dates are valid
    if registration_open_date > registration_end_date:
        return Response({"message": "Registration open date cannot be after registration end date."}, status=status.HTTP_400_BAD_REQUEST)

    # Ensure event dates are valid
    if start_date > end_date:
        return Response({"message": "Event start date cannot be after end date."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate prizepool is a number
    try:
        prizepool = float(prizepool)
    except ValueError:
        return Response({"message": "Invalid prizepool amount."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate prize_distribution format (should be a dictionary)
    if not isinstance(prize_distribution, dict):
        return Response({"message": "Invalid prize distribution format. Must be a JSON object."}, status=status.HTTP_400_BAD_REQUEST)

    # Create the event
    event = Event.objects.create(
        event_name=event_name,
        event_type=event_type,
        format=event_format,  # Ensure correct field name
        location=location,
        start_date=start_date,
        end_date=end_date,
        registration_open_date=registration_open_date,
        registration_end_date=registration_end_date,
        prizepool=prizepool,
        prize_distribution=prize_distribution,
        event_rules=event_rules,
        event_status=event_status,
        registration_link=registration_link,
        tournament_tier=tournament_tier,
        event_banner=event_banner,
        stream_channel=stream_channel,
    )

    return Response({
        "message": "Event created successfully.",
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_status": event.event_status,
    }, status=status.HTTP_201_CREATED)




@api_view(["POST"])
def edit_event(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Check if the user has permission to edit an event
    if user.role not in ["admin", "moderator", "support"]:
        return Response({"message": "You do not have permission to edit an event."}, status=status.HTTP_403_FORBIDDEN)

    # Extract event ID
    event_id = request.data.get("event_id")
    try:
        event = Event.objects.get(event_id=event_id)
    except Event.DoesNotExist:
        return Response({"message": "Event not found."}, status=status.HTTP_404_NOT_FOUND)

    # Update event details
    event.event_name = request.data.get("event_name", event.event_name)
    event.event_type = request.data.get("event_type", event.event_type)
    event.format = request.data.get("format", event.format)
    event.location = request.data.get("location", event.location)
    event.start_date = parse_date(request.data.get("start_date")) or event.start_date
    event.end_date = parse_date(request.data.get("end_date")) or event.end_date
    event.registration_open_date = parse_date(request.data.get("registration_open_date")) or event.registration_open_date
    event.registration_end_date = parse_date(request.data.get("registration_end_date")) or event.registration_end_date
    event.prizepool = request.data.get("prizepool", event.prizepool)
    event.prize_distribution = request.data.get("prize_distribution", event.prize_distribution)
    event.event_rules = request.data.get("event_rules", event.event_rules)
    event.event_status = request.data.get("event_status", event.event_status)
    event.registration_link = request.data.get("registration_link", event.registration_link)
    event.tournament_tier = request.data.get("tournament_tier", event.tournament_tier)
    event.event_banner = request.FILES.get("event_banner", event.event_banner)
    event.stream_channel = request.data.get("stream_channel", event.stream_channel)

    # Validate registration dates
    if event.registration_open_date > event.registration_end_date:
        return Response({"message": "Registration open date cannot be after registration end date."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate event dates
    if event.start_date > event.end_date:
        return Response({"message": "Event start date cannot be after end date."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate prizepool is a number
    try:
        event.prizepool = float(event.prizepool)
    except ValueError:
        return Response({"message": "Invalid prizepool amount."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate prize_distribution format
    if not isinstance(event.prize_distribution, dict):
        return Response({"message": "Invalid prize distribution format. Must be a JSON object."}, status=status.HTTP_400_BAD_REQUEST)

    # Save updates
    event.save()

    return Response({
        "message": "Event updated successfully.",
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_status": event.event_status,
    }, status=status.HTTP_200_OK)


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

