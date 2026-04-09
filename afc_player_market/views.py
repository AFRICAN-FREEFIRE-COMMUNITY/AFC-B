from django.shortcuts import render

# Create your views here.


from rest_framework.decorators import api_view
from rest_framework.response import Response
from datetime import datetime

from django.db.models import Q, Sum

from afc_auth.models import BannedPlayer, Notifications
from afc_team.models import Team, TeamMembers
from .models import Country, DirectTrialInvite, PlayerReport, RecruitmentApplication, RecruitmentPost, TrialChat, TrialChatMessage, TrialInvite
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

            # Set multiple countries (list of country codes, e.g. ["NG", "GH", "KE"])
            country_names = data.get("country_names", [])
            if country_names:
                selected_countries = Country.objects.filter(name__in=country_names)
                post.countries.set(selected_countries)

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
            post.save()

            # Set multiple countries (list of country codes, e.g. ["NG", "GH", "KE"])
            country_names = data.get("country_names", [])
            if country_names:
                selected_countries = Country.objects.filter(name__in=country_names)
                post.countries.set(selected_countries)

            return Response({
                "message": "Recruitment post created successfully",
                "post_id": post.id
            }, status=201)

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
            "countries": list(post.countries.values("name", "code")),
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

    # Retention email at every 5th application milestone (5, 10, 15, ...)
    Notifications.objects.create(
        user=post.team.team_owner,
        message=f"Your Player Market post is getting attention!"
    )
    total_number_of_applications = RecruitmentApplication.objects.filter(team=post.team).count()

    if total_number_of_applications % 5 == 0:
        team_owner_email = post.team.team_owner.email
        team_captain_email = post.team.team_captain.email if post.team.team_captain else None
        manager_emails = list(post.team.memberships.filter(management_role="manager").values_list("member__email", flat=True))
        coach_emails = list(post.team.memberships.filter(management_role="coach").values_list("member__email", flat=True))
        recipient_emails = set([team_owner_email, team_captain_email] + manager_emails + coach_emails)

        email_subject = "Your Player Market post is getting attention!"
        email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Your post is getting attention</title>
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
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Your Post Is Getting Attention!</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hi <strong style="color:#ffffff;">{post.team.team_name} Management</strong>, your recruitment post for
                <strong style="color:#ff7a00;">{post.team.team_name}</strong> is attracting players!
              </p>

              <!-- Milestone Counter -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
                <tr>
                  <td align="center" style="background-color:#242424;border-radius:10px;border:1px solid #333;padding:28px;">
                    <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#666;">Total Applications</p>
                    <p style="margin:0;font-size:56px;font-weight:800;color:#ff7a00;line-height:1;">{total_number_of_applications}</p>
                    <p style="margin:8px 0 0 0;font-size:13px;color:#888;">players have applied to join your team</p>
                  </td>
                </tr>
              </table>

              <!-- Message -->
              <div style="background-color:#1e1e1e;border-left:3px solid #ff7a00;border-radius:0 8px 8px 0;padding:16px 20px;margin-bottom:28px;">
                <p style="margin:0;font-size:14px;color:#bbbbbb;line-height:1.7;">
                  Don&rsquo;t let talent slip away &mdash; log in to review your applications, shortlist the best candidates, and invite players to trial.
                </p>
              </div>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/team/applications"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Review Applications
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">You received this because you are a staff member of <strong style="color:#777;">{post.team.team_name}</strong>.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
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
    if application.team.team_owner != user:
        return Response({"message": "Unauthorized"}, status=403)

    action = request.data.get("action")

    if action == "REJECT":
        application.reason = request.data.get("reason")
        application.status = "REJECTED"

        Notifications.objects.create(
            user=application.player,
            message=f"Your application to {application.team.team_name} has been rejected."
        )

        # SEND EMAIL TO PLAYER
        email_subject = f"Application Update from {application.team.team_name}"
        email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Application Update</title>
</head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#1a1a1a,#2a2a2a);padding:32px 40px;text-align:center;border-bottom:3px solid #333;">
              <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:#666;text-transform:uppercase;">African Free Fire Community</p>
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Application Update</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hi <strong style="color:#ffffff;">{application.player.username}</strong>,
              </p>

              <p style="margin:0 0 24px 0;font-size:15px;color:#aaaaaa;line-height:1.7;">
                Thank you for your interest in joining <strong style="color:#ffffff;">{application.team.team_name}</strong>.
                After careful consideration, we regret to inform you that your application was not successful at this time.
                We encourage you to keep honing your skills and consider applying again in the future.
              </p>

              <!-- Reason Box (only shown if reason provided) -->
              {"" if not application.reason else f"""
              <p style="margin:0 0 8px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#555;">Reason</p>
              <div style="background-color:#1e1e1e;border-left:3px solid #555;border-radius:0 8px 8px 0;padding:16px 20px;margin-bottom:28px;">
                <p style="margin:0;font-size:14px;color:#999999;line-height:1.7;font-style:italic;">{application.reason}</p>
              </div>
              """}

              <!-- Encouragement Card -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
                <tr>
                  <td style="background-color:#1e1a0e;border:1px solid #ff9500;border-radius:8px;padding:20px 24px;">
                    <p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#ff9500;text-transform:uppercase;letter-spacing:1px;">Keep Going</p>
                    <p style="margin:0;font-size:13px;color:#cc9933;line-height:1.6;">
                      Every great player started somewhere. Keep practicing, stay active in the community, and your next opportunity could be just around the corner.
                    </p>
                  </td>
                </tr>
              </table>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/player-market"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Browse Other Teams
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">We wish you the best of luck in your esports journey.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
        send_email(application.player.email, email_subject, email_body)

    elif action == "SHORTLIST":
        application.status = "SHORTLISTED"

        Notifications.objects.create(
            user=application.player,
            message=f"Your application to {application.team.team_name} has been shortlisted."
        )

    elif action == "INVITE":
        player = application.player

        # Check if player is already in 2 active trials
        active_trials = RecruitmentApplication.objects.filter(
            player=player,
            status="TRIAL_ONGOING"
        ).count()

        if active_trials >= 2:
            return Response({
                "message": f"{player.username} is currently in {active_trials} active trial(s) and cannot be in more than 2 at a time."
            }, status=400)

        application.status = "TRIAL_ONGOING"
        application.contact_unlocked = True
        application.invite_expires_at = timezone.now() + timedelta(hours=72)

        TrialInvite.objects.create(
            team=application.team,
            player=player,
            application=application,
            expires_at=application.invite_expires_at,
            status="ACCEPTED"
        )

        chat = TrialChat.objects.create(application=application)

        # Notify player
        Notifications.objects.create(
            user=player,
            message=f"You have been added to a trial with {application.team.team_name}. A trial chat has been created."
        )

        # Email to player
        email_subject = f"You've Been Added to a Trial with {application.team.team_name}!"
        player_email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Trial Started</title>
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
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Your Trial Has Begun!</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hey <strong style="color:#ffffff;">{player.username}</strong> &mdash;
                <strong style="color:#ff7a00;">{application.team.team_name}</strong> has selected you for a trial!
                A dedicated trial chat has been created where you can communicate directly with the team&rsquo;s management.
              </p>

              <!-- Team Card -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:24px;">
                <tr>
                  <td style="padding:20px 24px;">
                    <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Team</p>
                    <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{application.team.team_name}</p>
                  </td>
                </tr>
              </table>

              <!-- Info Box -->
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
                <tr>
                  <td style="background-color:#1e1a0e;border:1px solid #ff6b0044;border-radius:8px;padding:16px 20px;">
                    <p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#ff9500;text-transform:uppercase;letter-spacing:1px;">What happens next?</p>
                    <p style="margin:0;font-size:13px;color:#cc9933;line-height:1.6;">
                      Use the trial chat in the AFC app to coordinate with the team. This is your chance to impress &mdash; give it your all!
                    </p>
                  </td>
                </tr>
              </table>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/applications"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Open Trial Chat
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">This trial was started because you applied to <strong style="color:#777;">{application.team.team_name}</strong> on the AFC Player Market.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
        send_email(player.email, email_subject, player_email_body)

        # Notify team staff
        Notifications.objects.create(
            user=application.team.team_owner,
            message=f"{player.username} has been added to a trial. A trial chat has been created."
        )

        # Email to team owner, manager, coach, captain
        team_owner_email = application.team.team_owner.email
        team_captain_email = application.team.team_captain.email if application.team.team_captain else None
        manager_emails = list(application.team.memberships.filter(management_role="manager").values_list("member__email", flat=True))
        coach_emails = list(application.team.memberships.filter(management_role="coach").values_list("member__email", flat=True))
        recipient_emails = set(filter(None, [team_owner_email, team_captain_email] + manager_emails + coach_emails))

        team_email_subject = f"Trial Started — {player.username} has been added!"
        team_email_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Trial Started</title>
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
              <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;letter-spacing:1px;">Trial Started</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
                Hi <strong style="color:#ffffff;">{application.team.team_name}</strong> Management,
              </p>

              <p style="margin:0 0 24px 0;font-size:15px;color:#aaaaaa;line-height:1.7;">
                <strong style="color:#ff7a00;">{player.username}</strong> has been added to a trial with your team.
                A dedicated trial chat is now available to coordinate and evaluate their performance.
              </p>

              <!-- Player Card -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:28px;">
                <tr>
                  <td style="padding:20px 24px;">
                    <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Player on Trial</p>
                    <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{player.username}</p>
                  </td>
                </tr>
              </table>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="https://africanfreefirecommunity.com/team/trials"
                       style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
                      Open Trial Chat
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
              <p style="margin:0;font-size:12px;color:#555555;">You received this because you are a staff member of <strong style="color:#777;">{application.team.team_name}</strong>.</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
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
            send_email(email, team_email_subject, team_email_body)

        application.save()
        return Response({"message": "Trial started.", "chat_id": chat.id}, status=200)

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




@api_view(["GET"])
def get_my_trial_chats(request):
    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    trial_chats = TrialChat.objects.filter(
        Q(application__player=user) |
        Q(application__team__team_owner=user) |
        Q(application__team__memberships__member=user, application__team__memberships__management_role__in=['coach', 'manager'])
    ).distinct().select_related("application", "application__team", "application__player")

    data = [
        {
            "chat_id": chat.id,
            "application_id": chat.application.id,
            "team": chat.application.team.team_name,
            "player": chat.application.player.username,
        }
        for chat in trial_chats
    ]

    return Response(data, status=200)


@api_view(["POST"])
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

# ─────────────────────────────────────────────────────────────────────────────
# DIRECT TRIAL INVITES  (Team → Player from a PLAYER_AVAILABLE post)
# ─────────────────────────────────────────────────────────────────────────────

@api_view(["POST"])
def invite_player_to_trial(request):
    """
    Team invites a player who posted a PLAYER_AVAILABLE post.
    - Caller must be team owner, manager, or coach
    - Team must have < 4 active (TRIAL_ONGOING) trials
    - No duplicate pending invite allowed
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    post_id = request.data.get("post_id")
    invite_message = request.data.get("message", "")

    try:
        post = RecruitmentPost.objects.get(id=post_id)
    except RecruitmentPost.DoesNotExist:
        return Response({"message": "Post not found."}, status=404)

    if post.post_type != "PLAYER_AVAILABLE":
        return Response({"message": "This post is not a player availability post."}, status=400)

    # Resolve which team this user represents
    team = None
    if Team.objects.filter(team_owner=user).exists():
        team = Team.objects.get(team_owner=user)
    else:
        membership = TeamMembers.objects.filter(
            member=user, management_role__in=['manager', 'coach']
        ).select_related('team').first()
        if membership:
            team = membership.team

    if not team:
        return Response({"message": "You must be a team owner, manager, or coach to send a trial invite."}, status=403)

    if TeamMembers.objects.filter(team=team, member=post.player).exists():
        return Response({"message": "This player is already in your team."}, status=400)

    if DirectTrialInvite.objects.filter(team=team, player_post=post, status="PENDING").exists():
        return Response({"message": "You have already sent a pending trial invite to this player."}, status=400)

    active_team_trials = RecruitmentApplication.objects.filter(team=team, status="TRIAL_ONGOING").count()
    if active_team_trials >= 4:
        return Response({"message": "Your team already has 4 active trials. Finalize an existing trial before starting more."}, status=400)

    invite = DirectTrialInvite.objects.create(
        team=team,
        player=post.player,
        player_post=post,
        message=invite_message,
        expires_at=timezone.now() + timedelta(hours=72),
    )

    Notifications.objects.create(
        user=post.player,
        message=f"{team.team_name} has sent you a trial invite!"
    )

    player = post.player
    email_subject = f"Trial Invite from {team.team_name}"
    message_row = (
        f'<tr><td style="padding:0 24px 20px 24px;border-top:1px solid #333;">'
        f'<p style="margin:12px 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Message</p>'
        f'<p style="margin:0;font-size:14px;color:#bbbbbb;line-height:1.6;font-style:italic;">{invite_message}</p>'
        f'</td></tr>'
    ) if invite_message else ""

    email_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">
        <tr><td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
          <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
          <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;">A Team Wants You!</h1>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
            Hey <strong style="color:#ffffff;">{player.username}</strong> &mdash;
            <strong style="color:#ff7a00;">{team.team_name}</strong> saw your availability post and wants you on their roster for a trial!
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:24px;">
            <tr><td style="padding:20px 24px;">
              <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Team Inviting You</p>
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{team.team_name}</p>
            </td></tr>
            {message_row}
          </table>
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
            <tr><td style="background-color:#2a1a00;border:1px solid #ff6b0044;border-radius:8px;padding:16px 20px;">
              <table cellpadding="0" cellspacing="0"><tr>
                <td style="padding-right:12px;font-size:22px;">&#9201;</td>
                <td>
                  <p style="margin:0;font-size:13px;font-weight:700;color:#ff9500;text-transform:uppercase;letter-spacing:1px;">72-Hour Window</p>
                  <p style="margin:4px 0 0 0;font-size:13px;color:#cc8800;line-height:1.5;">You must accept or decline within <strong>72 hours</strong>. After that, the invite expires.</p>
                </td>
              </tr></table>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
            <a href="https://africanfreefirecommunity.com/my-invites"
               style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
              View &amp; Respond to Invite
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
          <p style="margin:0;font-size:12px;color:#555555;">This invite was sent because you have an active availability post on the AFC Player Market.</p>
          <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    send_email(player.email, email_subject, email_body)
    return Response({"message": "Trial invite sent.", "invite_id": invite.id}, status=201)


@api_view(["GET"])
def view_my_trial_invites(request):
    """Player views all direct trial invites received from teams."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    invites = DirectTrialInvite.objects.filter(player=user).select_related(
        "team", "player_post"
    ).order_by("-created_at")

    data = []
    for invite in invites:
        if invite.status == "PENDING" and invite.expires_at < timezone.now():
            invite.status = "EXPIRED"
            invite.save(update_fields=["status"])

        data.append({
            "invite_id": invite.id,
            "team": invite.team.team_name,
            "team_id": invite.team.team_id,
            "team_logo": invite.team.team_logo.url if invite.team.team_logo else None,
            "message": invite.message,
            "status": invite.status,
            "post_id": invite.player_post.id,
            "expires_at": invite.expires_at,
            "created_at": invite.created_at,
        })

    return Response(data, status=200)


@api_view(["POST"])
def respond_to_direct_trial_invite(request):
    """
    Player accepts or declines a DirectTrialInvite.
    ACCEPT:  player < 2 active trials, team < 4 active trials → creates RecruitmentApplication + TrialChat
    DECLINE: marks invite rejected, notifies team
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    invite_id = request.data.get("invite_id")
    action = request.data.get("action")  # ACCEPT or DECLINE

    try:
        invite = DirectTrialInvite.objects.select_related("team", "player", "player_post").get(id=invite_id)
    except DirectTrialInvite.DoesNotExist:
        return Response({"message": "Invite not found."}, status=404)

    if invite.player != user:
        return Response({"message": "Unauthorized."}, status=403)

    if invite.status != "PENDING":
        return Response({"message": f"This invite has already been {invite.status.lower()}."}, status=400)

    if invite.expires_at < timezone.now():
        invite.status = "EXPIRED"
        invite.save(update_fields=["status"])
        return Response({"message": "This invite has expired."}, status=400)

    if action == "DECLINE":
        invite.status = "REJECTED"
        invite.save()
        Notifications.objects.create(
            user=invite.team.team_owner,
            message=f"{user.username} has declined your trial invite."
        )
        return Response({"message": "Invite declined."}, status=200)

    elif action == "ACCEPT":
        player_active_trials = RecruitmentApplication.objects.filter(
            player=user, status="TRIAL_ONGOING"
        ).count()
        if player_active_trials >= 2:
            return Response({
                "message": f"You are already in {player_active_trials} active trial(s). You cannot be in more than 2 at a time."
            }, status=400)

        team_active_trials = RecruitmentApplication.objects.filter(
            team=invite.team, status="TRIAL_ONGOING"
        ).count()
        if team_active_trials >= 4:
            return Response({
                "message": f"{invite.team.team_name} already has 4 active trials and cannot start more right now."
            }, status=400)

        invite.status = "ACCEPTED"
        invite.save()

        # Unified: RecruitmentApplication + TrialChat so all existing chat logic works
        application = RecruitmentApplication.objects.create(
            player=user,
            recruitment_post=invite.player_post,
            team=invite.team,
            status="TRIAL_ONGOING",
            contact_unlocked=True,
        )
        chat = TrialChat.objects.create(application=application)

        Notifications.objects.create(
            user=invite.team.team_owner,
            message=f"{user.username} accepted your trial invite. A trial chat has been created."
        )

        team = invite.team
        team_owner_email = team.team_owner.email
        team_captain_email = team.team_captain.email if team.team_captain else None
        manager_emails = list(team.memberships.filter(management_role="manager").values_list("member__email", flat=True))
        coach_emails = list(team.memberships.filter(management_role="coach").values_list("member__email", flat=True))
        recipient_emails = set(filter(None, [team_owner_email, team_captain_email] + manager_emails + coach_emails))

        team_email_subject = f"{user.username} accepted your trial invite!"
        team_email_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">
        <tr><td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
          <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
          <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;">Trial Accepted!</h1>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">Hi <strong style="color:#ffffff;">{team.team_name}</strong> Management,</p>
          <p style="margin:0 0 24px 0;font-size:15px;color:#aaaaaa;line-height:1.7;">
            <strong style="color:#ff7a00;">{user.username}</strong> has accepted your trial invite. A dedicated trial chat is now open.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Player on Trial</p>
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{user.username}</p>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
            <a href="https://africanfreefirecommunity.com/team/trials"
               style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
              Open Trial Chat
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
          <p style="margin:0;font-size:12px;color:#555555;">You received this because you are a staff member of <strong style="color:#777;">{team.team_name}</strong>.</p>
          <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
        for email in recipient_emails:
            send_email(email, team_email_subject, team_email_body)

        return Response({"message": "Trial accepted.", "chat_id": chat.id}, status=200)

    else:
        return Response({"message": "Invalid action. Use ACCEPT or DECLINE."}, status=400)


@api_view(["GET"])
def view_application_details(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    application_id = request.query_params.get("application_id")

    try:
        app = RecruitmentApplication.objects.select_related(
            "player", "team", "recruitment_post", "recruitment_post__country"
        ).get(id=application_id)
    except RecruitmentApplication.DoesNotExist:
        return Response({"message": "Application not found."}, status=404)

    if app.player != user:
        return Response({"message": "Unauthorized."}, status=403)

    player = app.player

    tournament_wins = TournamentTeamMatchStats.objects.filter(
        tournament_team__members__user=player,
        tournament_team__event__competition_type="tournament",
        placement=1,
    ).count()

    total_tournament_kills = TournamentPlayerMatchStats.objects.filter(
        player=player,
        team_stats__tournament_team__event__competition_type="tournament",
    ).aggregate(total=Sum("kills"))["total"] or 0

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

    # Trial chat if one exists for this application
    chat_id = None
    try:
        chat_id = app.trial_chat.id
    except Exception:
        pass

    return Response({
        "id": app.id,
        "status": app.status,
        "applied_at": app.created_at,
        "updated_at": app.updated_at,
        "application_message": app.application_message,
        "reason": app.reason,
        "invite_expires_at": app.invite_expires_at,
        "contact_unlocked": app.contact_unlocked,

        "team": {
            "id": app.team.team_id,
            "name": app.team.team_name,
            "tag": app.team.team_tag,
            "logo": app.team.team_logo.url if app.team.team_logo else None,
            "tier": app.team.team_tier,
            "country": app.team.country,
        },

        "post": {
            "id": app.recruitment_post.id,
            "roles_needed": app.recruitment_post.roles_needed,
            "commitment_type": app.recruitment_post.commitment_type,
            "minimum_tier_required": app.recruitment_post.minimum_tier_required,
            "country": app.recruitment_post.country.name if app.recruitment_post.country else None,
            "expiry": app.recruitment_post.post_expiry_date,
        },

        "stats": {
            "tournament_wins": tournament_wins,
            "total_tournament_kills": total_tournament_kills,
            "tournament_finals_appearances": tournament_finals_appearances,
            "scrims_kills": scrims_kills,
            "scrims_wins": scrims_wins,
        },

        "chat_id": chat_id,
    }, status=200)


@api_view(["GET"])
def view_all_trials_and_applications(request):
    """Admin view to see all trials and applications in the system."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    if user.role not in ["admin", "moderator"]:
        return Response({"message": "Unauthorized."}, status=403)

    # Optional filters via query params
    status_filter = request.query_params.get("status")    # e.g. ?status=TRIAL_ONGOING
    team_filter   = request.query_params.get("team_id")   # e.g. ?team_id=5
    player_filter = request.query_params.get("player_id") # e.g. ?player_id=12

    applications = RecruitmentApplication.objects.select_related(
        "player", "team", "recruitment_post"
    ).order_by("-created_at")

    if status_filter:
        applications = applications.filter(status=status_filter)
    if team_filter:
        applications = applications.filter(team__team_id=team_filter)
    if player_filter:
        applications = applications.filter(player__id=player_filter)

    from django.db.models import Count
    status_summary = list(
        RecruitmentApplication.objects
        .values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )

    data = []
    for app in applications:
        chat_id = None
        try:
            chat_id = app.trial_chat.id
        except Exception:
            pass

        data.append({
            "id": app.id,
            "status": app.status,
            "applied_at": app.created_at,
            "updated_at": app.updated_at,
            "reason": app.reason,
            "invite_expires_at": app.invite_expires_at,
            "contact_unlocked": app.contact_unlocked,
            "chat_id": chat_id,

            "player": {
                "id": app.player.id,
                "username": app.player.username,
                "uid": app.player.uid,
                "discord": app.player.discord_username,
                "is_banned": BannedPlayer.objects.filter(banned_player=app.player, is_active=True).exists(),
            },

            "team": {
                "id": app.team.team_id,
                "name": app.team.team_name,
                "tag": app.team.team_tag,
                "tier": app.team.team_tier,
            },

            "post": {
                "id": app.recruitment_post.id,
                "post_type": app.recruitment_post.post_type,
                "roles_needed": app.recruitment_post.roles_needed,
                "commitment_type": app.recruitment_post.commitment_type,
            },
        })

    return Response({
        "summary": status_summary,
        "total": len(data),
        "applications": data,
    }, status=200)
