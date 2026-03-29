from django.shortcuts import render

# Create your views here.


from rest_framework.decorators import api_view
from rest_framework.response import Response
from datetime import datetime

from afc_auth.models import Notifications
from afc_team.models import Team
from .models import Country, RecruitmentApplication, RecruitmentPost
from afc_auth.views import send_email, validate_token


@api_view(["POST"])
def create_recruitment_post(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    data = request.data

    try:
        post_type = data.get("post_type")
        country_code = data.get("country_code")
        expiry = data.get("post_expiry_date")

        if not post_type or not expiry:
            return Response({"message": "post_type and post_expiry_date are required"}, status=400)
        
        # 🌍 Get country
        country = None
        if country_code:
            country = Country.objects.filter(code=country_code).first()
            if not country:
                return Response({"message": "Invalid country code"}, status=400)

        post = RecruitmentPost.objects.create(
            post_type=post_type,
            country=country,
            post_expiry_date=datetime.strptime(expiry, "%Y-%m-%d").date(),
            created_by=user
        )


        # ---------------- PLAYER POST ----------------
        if post_type == "PLAYER_AVAILABLE":
            post.player = user
            post.primary_role = data.get("primary_role")
            post.secondary_role = data.get("secondary_role")
            post.availability_type = data.get("availability_type")
            post.additional_info = data.get("additional_info")

        # ---------------- TEAM POST ----------------
        elif post_type == "TEAM_RECRUITMENT":
            team = Team.objects.get(team_owner=user)
            if not team:
                return Response({"message": "You must own a team to create a recruitment post"}, status=400)
            post.team = team
            post.roles_needed = data.get("roles_needed")  # JSON
            post.minimum_tier_required = data.get("minimum_tier_required")
            post.commitment_type = data.get("commitment_type")
            post.recruitment_criteria = data.get("recruitment_criteria")

        else:
            return Response({"message": "Invalid post_type"}, status=400)

        post.save()

        return Response({
            "message": "Recruitment post created successfully",
            "post_id": post.id
        }, status=201)

    except Exception as e:
        return Response({"message": str(e)}, status=500)
    

@api_view(["GET"])
def get_recruitment_posts(request):

    posts = RecruitmentPost.objects.all().order_by("-created_at")

    data = []

    for post in posts:
        data.append({
            "id": post.id,
            "post_type": post.post_type,
            "country": post.country,
            "expiry": post.post_expiry_date,
            "created_at": post.created_at,

            # Player fields
            "player": post.player.username if post.player else None,
            "primary_role": post.primary_role,
            "secondary_role": post.secondary_role,
            "availability_type": post.availability_type,

            # Team fields
            "team": post.team.name if post.team else None,
            "roles_needed": post.roles_needed,
            "minimum_tier_required": post.minimum_tier_required,
            "commitment_type": post.commitment_type,
        })

    return Response(data, status=200)


@api_view(["GET"])
def view_all_team_recruitment_post(request):

    posts = RecruitmentPost.objects.filter(
        post_type="TEAM_RECRUITMENT"
    ).order_by("-created_at")

    data = []

    for post in posts:
        data.append({
            "id": post.id,
            "team": post.team.name if post.team else None,
            "country": post.country,
            "roles_needed": post.roles_needed,
            "minimum_tier_required": post.minimum_tier_required,
            "commitment_type": post.commitment_type,
            "expiry": post.post_expiry_date,
        })

    return Response(data, status=200)


@api_view(["GET"])
def view_all_player_availability_post(request):

    posts = RecruitmentPost.objects.filter(
        post_type="PLAYER_AVAILABLE"
    ).order_by("-created_at")

    data = []

    for post in posts:
        data.append({
            "id": post.id,
            "player": post.player.username if post.player else None,
            "country": post.country,
            "primary_role": post.primary_role,
            "secondary_role": post.secondary_role,
            "availability_type": post.availability_type,
            "additional_info": post.additional_info,
            "expiry": post.post_expiry_date,
        })

    return Response(data, status=200)


@api_view(["POST"])
def apply_to_team(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)
    

    post_id = request.data.get("post_id")
    post = RecruitmentPost.objects.get(id=post_id)

    if post.post_type != "TEAM_RECRUITMENT":
        return Response({"message": "Invalid post"}, status=400)

    application, created = RecruitmentApplication.objects.get_or_create(
        player=user,
        recruitment_post=post,
        team=post.team
    )

    if not created:
        return Response({"message": "Already applied"}, status=400)

    return Response({"message": "Application submitted"}, status=201)


from datetime import timedelta
from django.utils import timezone


@api_view(["POST"])
def update_application_status(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)
    application_id = request.data.get("application_id")

    application = RecruitmentApplication.objects.get(id=application_id)

    # Ensure user owns the team
    if application.team.owner != user:
        return Response({"message": "Unauthorized"}, status=403)

    action = request.data.get("action")

    if action == "REJECT":
        application.status = "REJECTED"

    elif action == "SHORTLIST":
        application.status = "SHORTLISTED"

    elif action == "INVITE":
        application.status = "INVITED"
        application.contact_unlocked = True
        application.invite_expires_at = timezone.now() + timedelta(hours=72)

        # 🔥 Trigger notification here (important)
        send_trial_invite_notification(application)

    else:
        return Response({"message": "Invalid action"}, status=400)

    application.save()

    return Response({"message": "Application updated"}, status=200)


@api_view(["POST"])
def get_player_contact(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    application_id = request.data.get("application_id")

    application = RecruitmentApplication.objects.get(id=application_id)

    if application.team.team_owner != user:
        return Response({"message": "Unauthorized"}, status=403)

    if not application.contact_unlocked:
        return Response({"message": "Contact locked"}, status=403)

    if application.invite_expires_at < timezone.now():
        return Response({"message": "Invite expired"}, status=403)

    player = application.player

    return Response({
        "discord": player.discord_username,
        "uid": player.uid
    })


def send_trial_invite_notification(application):

    player = application.player
    team = application.team

    message = f"""
    {team.team_name} has invited you to a trial.

    Join their Discord within 72 hours to proceed.
    """

    # Save notification in DB (you should have Notification model)
    Notifications.objects.create(
        user=player,
        message=message
    )

    # Optional:
    send_email(player.email, message)
    # send_discord_dm(player.discord_id, message)


@api_view(["POST"])
def finalize_trial(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)
    application_id = request.data.get("application_id")

    application = RecruitmentApplication.objects.get(id=application_id)

    if application.team.team_owner != user:
        return Response({"message": "Unauthorized"}, status=403)

    action = request.data.get("action")

    if action == "ACCEPT":
        application.status = "ACCEPTED"

        # 🔥 Add player to team logic here

    elif action == "REJECT":
        application.status = "REJECTED"

    elif action == "EXTEND":
        application.status = "TRIAL_EXTENDED"

    else:
        return Response({"message": "Invalid action"}, status=400)

    application.save()

    return Response({"message": "Trial updated"})


@api_view(["GET"])
def view_applications(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    applications = RecruitmentApplication.objects.filter(team__owner=user).order_by("-created_at")

    data = []

    for app in applications:
        data.append({
            "id": app.id,
            "player": app.player.username,
            "team": app.team.name,
            "post_id": app.recruitment_post.id,
            "status": app.status,
            "contact_unlocked": app.contact_unlocked,
            "invite_expires_at": app.invite_expires_at,
            "applied_at": app.created_at,
        })

    return Response(data, status=200)