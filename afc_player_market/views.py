from django.shortcuts import render

# Create your views here.


from rest_framework.decorators import api_view
from rest_framework.response import Response
from datetime import datetime

from django.db.models import Sum

from afc_auth.models import BannedPlayer, Notifications
from afc_team.models import Team, TeamMembers
from .models import Country, PlayerReport, RecruitmentApplication, RecruitmentPost, TrialChat, TrialChatMessage, TrialInvite
from afc_auth.views import send_email, validate_token
from afc_tournament_and_scrims.models import TournamentPlayerMatchStats, TournamentTeamMatchStats


TRANSFER_WINDOW_STATUS = "OPEN"  # This can be dynamically set based on date or admin input


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
            try:
                team = Team.objects.get(team_owner=user)
            except Team.DoesNotExist:
                team = None   
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
            "country": post.country.name if post.country else None,
            "expiry": post.post_expiry_date,
            "created_at": post.created_at,

            # Player fields
            "player": post.player.username if post.player else None,
            "primary_role": post.primary_role,
            "secondary_role": post.secondary_role,
            "availability_type": post.availability_type,

            # Team fields
            "team": post.team.team_name if post.team else None,
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
            "team": post.team.team_name if post.team else None,
            "country": post.country.name if post.country else None,
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
    # ensure the applier is currently not in a team
    if TeamMembers.objects.filter(member=user).exists():
        return Response({"message": "You must leave your current team before applying"}, status=400)
    
    if post.post_type != "TEAM_RECRUITMENT":
        return Response({"message": "Invalid post"}, status=400)

    application_message = request.data.get("application_message", "")

    # ensure the user has not already applied to this post
    if RecruitmentApplication.objects.filter(player=user, recruitment_post=post).exists():
        return Response({"message": "Already applied"}, status=400)

    application, created = RecruitmentApplication.objects.get_or_create(
        player=user,
        recruitment_post=post,
        team=post.team,
        application_message=application_message
    )

    
    Notifications.objects.create(
        user=post.team.team_owner,
        message=f"{user.username} has applied to join your team {post.team.team_name}."
    )

    # Send Mail To Team Owner, Manager, Coach, Team Captain
    team_owner_email = post.team.team_owner.email
    team_captain_email = post.team.team_captain.email if post.team.team_captain else None
    manager_emails = post.team.memberships.filter(management_role="manager").values_list("member__email", flat=True)
    coach_emails = post.team.memberships.filter(management_role="coach").values_list("member__email", flat=True)
    recipient_emails = set([team_owner_email, team_captain_email] + list(manager_emails) + list(coach_emails))


    email_subject = f"New Application for {post.team.team_name}"
    email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>New Team Application</title>
</head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
              <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">New Player Application</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <!-- Intro -->
              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                A player has applied to join <strong style="color:#ff7a00;">{post.team.team_name}</strong>. Review the details below.
              </p>

              <!-- Player Info Card -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:8px;border:1px solid #333;margin-bottom:24px;">
                <tr>
                  <td style="padding:20px 24px;">
                    <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Player</p>
                    <p style="margin:0;font-size:20px;font-weight:700;color:#ffffff;">{user.username}</p>
                  </td>
                </tr>
                <tr>
                  <td style="padding:0 24px 20px 24px;">
                    <table width="100%" cellpadding="0" cellspacing="0">
                      <tr>
                        <td width="50%" style="padding-right:8px;">
                          <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Primary Role</p>
                          <p style="margin:0;font-size:14px;color:#ff7a00;font-weight:600;">{post.primary_role or "N/A"}</p>
                        </td>
                        <td width="50%" style="padding-left:8px;">
                          <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Secondary Role</p>
                          <p style="margin:0;font-size:14px;color:#ff7a00;font-weight:600;">{post.secondary_role or "N/A"}</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>

              <!-- Application Message -->
              <p style="margin:0 0 8px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Application Message</p>
              <div style="background-color:#242424;border-left:3px solid #ff7a00;border-radius:0 8px 8px 0;padding:16px 20px;margin-bottom:28px;">
                <p style="margin:0;font-size:14px;color:#cccccc;line-height:1.7;white-space:pre-wrap;">{application_message or "No message provided."}</p>
              </div>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/team/applications"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Review Application
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">You received this email because you are a staff member of <strong style="color:#777;">{post.team.team_name}</strong>.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2025 African Free Fire Community. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    for email in recipient_emails:
        if email:
            send_email(email, email_subject, email_body)


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

        Notifications.objects.create(
            user=application.player,
            message=f"Your application to {application.team.team_name} has been rejected."
        )

    elif action == "SHORTLIST":
        application.status = "SHORTLISTED"

        Notifications.objects.create(
            user=application.player,
            message=f"Your application to {application.team.team_name} has been shortlisted."
        )

    elif action == "INVITE":
        application.status = "INVITED"
        application.contact_unlocked = True
        application.invite_expires_at = timezone.now() + timedelta(hours=72)

        # # 🔥 Trigger notification here (important)
        # send_trial_invite_notification(application)

        # Send Trial Invite
        TrialInvite.objects.create(
            team=application.team,
            player=application.player,
            application=application,
            expires_at=application.invite_expires_at
        )

        # Send Notification
        Notifications.objects.create(
            user=application.player,
            message=f"{application.team.team_name} has invited you to a trial."
        )


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

    try:
        team = Team.objects.get(team_owner=user)
    except Team.DoesNotExist:
        return Response({"message": "Team not found"}, status=404)


    applications = RecruitmentApplication.objects.filter(team=team).order_by("-created_at")

    data = []

    for app in applications:
        player = app.player

        if player:
            tournament_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="tournament",
                placement=1,
            ).count()

            total_tournament_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
            ).aggregate(total=Sum("kills"))["total"] or 0

            # Finals appearances = distinct tournament events where player played in a stage named "final"
            tournament_finals_appearances = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
                team_stats__match__leaderboard__stage__stage_name__icontains="final",
            ).values("team_stats__tournament_team__event").distinct().count()

            scrims_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="scrims",
            ).aggregate(total=Sum("kills"))["total"] or 0

            scrims_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="scrims",
                placement=1,
            ).count()
        else:
            tournament_wins = 0
            total_tournament_kills = 0
            tournament_finals_appearances = 0
            scrims_kills = 0
            scrims_wins = 0

        data.append({
            "id": app.id,
            "player": player.username if player else None,
            "team": app.team.team_name if app.team else None,
            "post_id": app.recruitment_post.id,
            "status": app.status,
            "contact_unlocked": app.contact_unlocked,
            "invite_expires_at": app.invite_expires_at,
            "applied_at": app.created_at,
            "uid": player.uid if player else None,
            "discord_username": player.discord_username if player else None,
            "primary_role": app.recruitment_post.primary_role,
            "secondary_role": app.recruitment_post.secondary_role,
            "country": app.recruitment_post.country.name if app.recruitment_post.country else None,
            "is_banned": True if player and BannedPlayer.objects.filter(banned_player=player, is_active=True).exists() else False,
            "application_message": app.application_message,
            "tournament_wins": tournament_wins,
            "total_tournament_kills": total_tournament_kills,
            "tournament_finals_appearances": tournament_finals_appearances,
            "scrims_kills": scrims_kills,
            "scrims_wins": scrims_wins,
        })

    return Response(data, status=200)


@api_view(["POST"])
def report_team(request, application_id):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)
    
    application = RecruitmentApplication.objects.get(id=application_id)

    PlayerReport.objects.create(
        player=user,
        team=application.team,
        application=application,
        reason=request.data.get("reason")
    )

    return Response({"message": "Report submitted"}, status=201)


def _is_trial_chat_participant(user, chat):
    """Returns True if user is allowed to access the trial chat."""
    application = chat.application
    if user == application.player:
        return True
    if user == application.team.team_owner:
        return True
    return TeamMembers.objects.filter(
        team=application.team,
        member=user,
        management_role__in=['coach', 'manager']
    ).exists()


@api_view(["POST"])
def respond_to_trial_invite(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    trial_invite_id = request.data.get("trial_invite_id")
    action = request.data.get("action")  # ACCEPT or DECLINE

    try:
        trial_invite = TrialInvite.objects.get(id=trial_invite_id)
    except TrialInvite.DoesNotExist:
        return Response({"message": "Trial invite not found."}, status=404)

    application = trial_invite.application

    if trial_invite.player != user:
        return Response({"message": "Unauthorized."}, status=403)

    if application.status != "INVITED":
        return Response({"message": "No pending trial invite for this application."}, status=400)

    if application.invite_expires_at and application.invite_expires_at < timezone.now():
        return Response({"message": "Trial invite has expired."}, status=400)

    if action == "ACCEPT":
        application.status = "TRIAL_ONGOING"
        application.save()

        trial_invite.status = "ACCEPTED"
        trial_invite.save()

        chat = TrialChat.objects.create(application=application)

        Notifications.objects.create(
            user=application.team.team_owner,
            message=f"{user.username} has accepted your trial invite. A trial chat has been created."
        )

        return Response({"message": "Trial invite accepted.", "chat_id": chat.id}, status=200)

    elif action == "DECLINE":
        application.status = "REJECTED"
        application.save()

        trial_invite.status = "REJECTED"
        trial_invite.save()

        Notifications.objects.create(
            user=application.team.team_owner,
            message=f"{user.username} has declined your trial invite."
        )

        return Response({"message": "Trial invite declined."}, status=200)

    else:
        return Response({"message": "Invalid action. Use ACCEPT or DECLINE."}, status=400)


@api_view(["GET"])
def get_trial_chat_messages(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    chat_id = request.query_params.get("chat_id")

    try:
        chat = TrialChat.objects.get(id=chat_id)
    except TrialChat.DoesNotExist:
        return Response({"message": "Chat not found."}, status=404)

    if not _is_trial_chat_participant(user, chat):
        return Response({"message": "Unauthorized."}, status=403)

    messages = chat.messages.select_related("sender").all()

    data = [
        {
            "id": msg.id,
            "sender": msg.sender.username,
            "sender_id": msg.sender.id,
            "message": msg.message,
            "sent_at": msg.sent_at,
        }
        for msg in messages
    ]

    return Response({
        "chat_id": chat.id,
        "application_id": chat.application.id,
        "team": chat.application.team.team_name,
        "player": chat.application.player.username,
        "messages": data,
    }, status=200)


@api_view(["POST"])
def send_trial_chat_message(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    chat_id = request.data.get("chat_id")
    message_text = request.data.get("message", "").strip()

    if not message_text:
        return Response({"message": "Message cannot be empty."}, status=400)

    try:
        chat = TrialChat.objects.get(id=chat_id)
    except TrialChat.DoesNotExist:
        return Response({"message": "Chat not found."}, status=404)

    if not _is_trial_chat_participant(user, chat):
        return Response({"message": "Unauthorized."}, status=403)

    msg = TrialChatMessage.objects.create(chat=chat, sender=user, message=message_text)

    return Response({
        "id": msg.id,
        "sender": user.username,
        "message": msg.message,
        "sent_at": msg.sent_at,
    }, status=201)


@api_view(["GET"])
def view_my_applications(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)


    applications = RecruitmentApplication.objects.filter(player=user).order_by("-created_at")

    data = []

    for app in applications:
        player = app.player

        if player:
            tournament_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="tournament",
                placement=1,
            ).count()

            total_tournament_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
            ).aggregate(total=Sum("kills"))["total"] or 0

            # Finals appearances = distinct tournament events where player played in a stage named "final"
            tournament_finals_appearances = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="tournament",
                team_stats__match__leaderboard__stage__stage_name__icontains="final",
            ).values("team_stats__tournament_team__event").distinct().count()

            scrims_kills = TournamentPlayerMatchStats.objects.filter(
                player=player,
                team_stats__tournament_team__event__competition_type="scrims",
            ).aggregate(total=Sum("kills"))["total"] or 0

            scrims_wins = TournamentTeamMatchStats.objects.filter(
                tournament_team__members__user=player,
                tournament_team__event__competition_type="scrims",
                placement=1,
            ).count()
        else:
            tournament_wins = 0
            total_tournament_kills = 0
            tournament_finals_appearances = 0
            scrims_kills = 0
            scrims_wins = 0

        data.append({
            "id": app.id,
            "player": player.username if player else None,
            "team": app.team.team_name if app.team else None,
            "post_id": app.recruitment_post.id,
            "status": app.status,
            "contact_unlocked": app.contact_unlocked,
            "invite_expires_at": app.invite_expires_at,
            "applied_at": app.created_at,
            "uid": player.uid if player else None,
            "discord_username": player.discord_username if player else None,
            "primary_role": app.recruitment_post.primary_role,
            "secondary_role": app.recruitment_post.secondary_role,
            "country": app.recruitment_post.country.name if app.recruitment_post.country else None,
            "is_banned": True if player and BannedPlayer.objects.filter(banned_player=player, is_active=True).exists() else False,
            "application_message": app.application_message,
            "tournament_wins": tournament_wins,
            "total_tournament_kills": total_tournament_kills,
            "tournament_finals_appearances": tournament_finals_appearances,
            "scrims_kills": scrims_kills,
            "scrims_wins": scrims_wins,
        })

    return Response(data, status=200)