from django.shortcuts import redirect, render
from django.contrib.auth import authenticate
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
import random
import string
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.core.mail import send_mail
from django.urls import reverse
from django.contrib.auth.models import User
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from sympy import Q

from .models import AdminHistory, LoginHistory, LoginHistory, Roles, User, UserProfile, BannedPlayer, News, PasswordResetToken, UserRoles
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.urls import reverse
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .models import User
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_team.models import Team, TeamBan
from django.utils import timezone
from .models import TeamBan
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_datetime
from afc_tournament_and_scrims.models import Event
from django.contrib.auth.hashers import make_password, check_password
from django.core.cache import cache
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
import requests
from django.conf import settings


from utils.ipinfo_lookup import lookup_ip

def get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        # X-Forwarded-For may contain multiple IPs
        ip = x_forwarded_for.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


def generate_session_token(length=16):
    """Generate a random 16-character token."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))


def send_email(to_address, subject, html_body):
    # Gmail SMTP server credentials
    smtp_server = 'smtp.gmail.com'
    smtp_port = 465  # or 587 for TLS
    from_address = 'africanfreefirecommunity3@gmail.com' #vermillioninformation@gmail.com, Info@v-ent.co
    password = 'wobd dlxw riuh tsnm'
    #'yyzm prff sjfo bcmg2'
    # 'rvgn rzha ihli dfdp1'  # Or your actual Gmail password (if less secure apps are enabled)

# wobd dlxw riuh tsnm africanfreefirecommunity3@gmail.com

    try:
        # Create a MIMEMultipart email object
        msg = MIMEMultipart()
        msg['From'] = from_address
        msg['To'] = to_address
        msg['Subject'] = subject

        # Attach the HTML body to the MIME message
        msg.attach(MIMEText(html_body, 'html'))

        # Set up the SMTP connection using SSL
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        server.login(from_address, password)
        
        # Send the email
        server.sendmail(from_address, to_address, msg.as_string())
        server.quit()

        return True
    except Exception as e:
        return False


# @api_view(['POST'])
# def admin_login(request):
#     username = request.data.get('username')
#     password = request.data.get('password')

#     if not username or not password:
#         return Response(
#             {'status': 'error', 'message': 'Username and password are required.'},
#             status=status.HTTP_400_BAD_REQUEST
#         )

#     # Authenticate user with username or email
#     admin = authenticate(username=username, password=password)

#     if admin is not None:
#         # Generate a session token
#         session_token = generate_session_token()

#         # Save session token to the user model (ensure this field exists)
#         admin.session_token = session_token
#         admin.save()

#         # Return success response with the session token
#         return Response(
#             {
#                 'status': 'success',
#                 'message': 'Login successful',
#                 'data': {
#                     'session_token': session_token,
#                 }
#             },
#             status=status.HTTP_200_OK
#         )
#     else:
#         # Authentication failed, return error response
#         return Response(
#             {'status': 'error', 'message': 'Invalid username or password'},
#             status=status.HTTP_401_UNAUTHORIZED
#         )


@api_view(['POST'])
def login(request):
    ign_or_uid = request.data.get('ign_or_uid')
    password = request.data.get('password')

    # Authenticate user with either username or email
    user = authenticate(request, username=ign_or_uid, password=password)
    print("stage 1 complete", user)
    if user is not None:
        print("stage 2 complete")
        # Check if the user's account is active
        if not user.is_active:
            return Response({
                'message': 'Your account is not confirmed. Please verify your email address.'
            }, status=status.HTTP_403_FORBIDDEN)

        # Generate a session token
        session_token = generate_session_token()
        
        # Save session token to the user model
        user.session_token = session_token
        user.last_login = timezone.now()
        user.save()

        ip = get_client_ip(request)
        geo = lookup_ip("8.8.8.8")  # Replace with 'ip' for real IP lookup

        if geo:
            print(geo["country_code"], geo["country"])

        LoginHistory.objects.create(
            user=user,
            ip_address=ip,
            continent=geo["continent"] if geo else None,
            country_code=geo["country_code"] if geo else None,
            country=geo["country"] if geo else None,
            user_agent=request.META.get("HTTP_USER_AGENT")
        )

        # Return success response with the session token
        return Response({
            'message': 'Login successful', 
            'session_token': session_token,
            'user': {
                'id': user.user_id,
                'username': user.username,
            },
            "geo": geo
        }, status=status.HTTP_200_OK)
    else:
        # Authentication failed, return error response
        return Response({
            'message': 'Invalid username/email or password',
            'geo': geo
        }, status=status.HTTP_401_UNAUTHORIZED)


@api_view(["POST"])
def signup(request):
    in_game_name = request.data.get("in_game_name")
    uid = request.data.get("uid")
    email = request.data.get("email")
    password = request.data.get("password")
    confirm_password = request.data.get("confirm_password")
    full_name = request.data.get("full_name")
    country = request.data.get("country")

    try:
        # Validation
        if not all([in_game_name, uid, email, password, confirm_password]):
            return Response({"error": "All fields are required."}, status=status.HTTP_400_BAD_REQUEST)

        if password != confirm_password:
            return Response({"error": "Passwords do not match."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if in-game name or UID are already in use by **active users**
        if User.objects.filter(username=in_game_name, is_active=True).exists():
            return Response({"error": "In-game name is already in use."}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(uid=uid, is_active=True).exists():
            return Response({"error": "UID is already in use."}, status=status.HTTP_400_BAD_REQUEST)

        # Check for email
        existing_user = User.objects.filter(email=email).first()
        if existing_user:
            if existing_user.is_active:
                return Response({"error": "Email is already in use."}, status=status.HTTP_400_BAD_REQUEST)
            else:
                # User exists but not verified → resend verification code
                verification_code = random.randint(100000, 999999)
                cache.set(f"verification_code_{existing_user.user_id}", verification_code, timeout=600)

                subject = 'Your Verification Code'
                message = f'''Hi {existing_user.username},

Your verification code is: {verification_code}

Please enter this code in the app to verify your account.

If you did not create an account, please ignore this email.
'''
                send_email(email, subject, message)

                return Response({
                    "message": "You already signed up but didn't verify your email. A new verification code has been sent."
                }, status=status.HTTP_200_OK)

        # Create new user
        user = User.objects.create(
            username=in_game_name,
            uid=uid,
            email=email,
            is_active=False,
            full_name=full_name,
            country=country
        )
        user.set_password(password)
        user.save()
        UserProfile.objects.create(user=user)

        # Generate verification code
        verification_code = random.randint(100000, 999999)
        cache.set(f"verification_code_{user.user_id}", verification_code, timeout=600)

        # Send verification email
        subject = 'Your Verification Code'
        message = f'''Hi {in_game_name},

Your verification code is: {verification_code}

Please enter this code in the app to verify your account.

If you did not create an account, please ignore this email.
'''
        send_email(email, subject, message)

        return Response({"message": "Signup successful. Please check your email for the verification code."}, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({"error": f"An unexpected error occurred: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def verify_code(request):
    email = request.data.get("email")
    code = request.data.get("code")

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({"error": "Invalid email."}, status=status.HTTP_400_BAD_REQUEST)

    stored_code = cache.get(f"verification_code_{user.user_id}")

    if stored_code is None:
        return Response({"error": "Verification code expired or invalid."}, status=status.HTTP_400_BAD_REQUEST)

    if str(stored_code) != str(code):
        return Response({"error": "Invalid verification code."}, status=status.HTTP_400_BAD_REQUEST)

    # Activate user account
    user.is_active = True
    user.save()

    # Remove the verification code after successful verification
    cache.delete(f"verification_code_{user.user_id}")

    return Response({"message": "Account verified successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def resend_verification_code(request):
    email = request.data.get("email")

    if not email:
        return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

    user = User.objects.filter(email=email).first()

    if not user:
        return Response({"error": "No account found with this email."}, status=status.HTTP_404_NOT_FOUND)

    if user.is_active:
        return Response({"error": "This account is already verified."}, status=status.HTTP_400_BAD_REQUEST)

    # Generate new verification code
    verification_code = random.randint(100000, 999999)

    # Store in cache with a 10-minute expiry
    cache.set(f"verification_code_{user.user_id}", verification_code, timeout=600)

    # Send the new verification email
    subject = "Your New Verification Code"
    message = f'''Hi {user.username},

You requested a new verification code.

Your new verification code is: {verification_code}

Please enter this code in the app to verify your account.

If you did not request this, please ignore this email.
'''
    send_email(user.email, subject, message)

    return Response({"message": "A new verification code has been sent to your email."}, status=status.HTTP_200_OK)


@api_view(["GET"])
def verify_token(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)

        # Validate token
        if default_token_generator.check_token(user, token):
            user.is_active = True  # Activate user after verification
            user.save()
            return Response({"message": "Email verified successfully!"}, status=status.HTTP_200_OK)
        else:
            return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

    except User.DoesNotExist:
        return Response({"error": "Invalid user."}, status=status.HTTP_400_BAD_REQUEST)
    

@api_view(["POST"])
def ban_team(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify admin/moderator
    try:
        user = User.objects.get(session_token=session_token)
        if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)            
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    if user.userroles.filter(role__role_name='head_admin').exists() or user.userroles.filter(role__role_name='teams_admin').exists():
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to ban a team."}, status=status.HTTP_403_FORBIDDEN)
                    

    team_id = request.data.get("team_id")
    ban_duration = request.data.get("ban_duration")  # Duration in hours
    reason = request.data.get("reason", "Violation of rules")

    if not team_id or not ban_duration:
        return Response({"message": "Team ID and ban duration are required."}, status=status.HTTP_400_BAD_REQUEST)

    # Get team
    try:
        team = Team.objects.get(team_id=team_id)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if already banned
    if team.is_banned:
        return Response({"message": "Team is already banned."}, status=status.HTTP_400_BAD_REQUEST)

    # Calculate ban end time
    ban_end_date = timezone.now() + timezone.timedelta(hours=int(ban_duration))

    # Ban team
    team.is_banned = True
    team.save()

    TeamBan.objects.create(
        team=team,
        ban_end_date=ban_end_date,
        reason=reason,
        banned_by=user
    )

    AdminHistory.objects.create(
        admin_user=user,
        action="banned_team",
        description=f"Team {team.team_name} (ID: {team.team_id}) banned until {ban_end_date} for reason: {reason}"
    )

    return Response({
        "message": "Team banned successfully.",
        "team_id": team.team_id,
        "ban_end_date": ban_end_date,
        "reason": reason
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def unban_team(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify admin/moderator
    try:
        user = User.objects.get(session_token=session_token)
        if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    if user.userroles.filter(role__role_name='head_admin').exists() or user.userroles.filter(role__role_name='teams_admin').exists():
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to ban a team."}, status=status.HTTP_403_FORBIDDEN)


    team_id = request.data.get("team_id")

    if not team_id:
        return Response({"message": "Team ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Get the team
    try:
        team = Team.objects.get(team_id=team_id)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if the team is actually banned
    try:
        team_ban = TeamBan.objects.get(team=team)
        team_ban.delete()  # Remove ban record
        team.is_banned = False
        team.save()
    except TeamBan.DoesNotExist:
        return Response({"message": "Team is not banned."}, status=status.HTTP_400_BAD_REQUEST)
    
    AdminHistory.objects.create(
        admin_user=user,
        action="unbanned_team",
        description=f"Team {team.team_name} (ID: {team.team_id}) unbanned"
    )

    return Response({"message": "Team unbanned successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def ban_player(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Check if the user has permission to ban a player
    if user.role not in ["admin", "moderator", "support"]:
        return Response({"message": "You do not have permission to ban a player."}, status=status.HTTP_403_FORBIDDEN)
    
    if user.userroles.filter(role__role_name='head_admin').exists() or user.userroles.filter(role__role_name='teams_admin').exists():
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to ban a player."}, status=status.HTTP_403_FORBIDDEN)

    # Extract player IGN and ban details
    player_ign = request.data.get("player_ign")
    duration = request.data.get("duration")  # Duration in days
    reason = request.data.get("reason", "No reason provided")

    if not player_ign or not duration:
        return Response({"message": "Player IGN and duration are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        player = User.objects.get(in_game_name=player_ign)
    except User.DoesNotExist:
        return Response({"message": "Player not found."}, status=status.HTTP_404_NOT_FOUND)

    # Create a ban entry
    ban_entry = BannedPlayer.objects.create(
        banned_player=player,
        ban_duration=int(duration),
        reason=reason
    )

    AdminHistory.objects.create(
        admin_user=user,
        action="banned_player",
        description=f"Player {player_ign} (ID: {player.user_id}) banned for {duration} days for reason: {reason}"
    )

    return Response({
        "message": f"Player {player_ign} has been banned for {duration} days.",
        "ban_id": ban_entry.ban_id
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def unban_player(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Check if the user has permission to unban a player
    if user.role not in ["admin", "moderator", "support"]:
        return Response({"message": "You do not have permission to unban a player."}, status=status.HTTP_403_FORBIDDEN)
    
    if user.userroles.filter(role__role_name='head_admin').exists() or user.userroles.filter(role__role_name='teams_admin').exists():
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to unban a player."}, status=status.HTTP_403_FORBIDDEN)

    # Extract player IGN
    player_ign = request.data.get("player_ign")

    if not player_ign:
        return Response({"message": "Player IGN is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        player = User.objects.get(username=player_ign)
    except User.DoesNotExist:
        return Response({"message": "Player not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if the player is banned
    try:
        ban_entry = BannedPlayer.objects.get(banned_player=player, is_active=True)
    except BannedPlayer.DoesNotExist:
        return Response({"message": "Player is not currently banned."}, status=status.HTTP_400_BAD_REQUEST)

    # Unban the player
    ban_entry.is_active = False
    ban_entry.save()

    AdminHistory.objects.create(
        admin_user=user,
        action="unbanned_player",
        description=f"Player {player_ign} (ID: {player.user_id}) unbanned"
    )

    return Response({
        "message": f"Player {player_ign} has been unbanned."
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def create_news(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Check if the user has permission to create news
    if user.role not in ["admin", "moderator", "support"]:
        return Response({"message": "You do not have permission to create news."}, status=status.HTTP_403_FORBIDDEN)
    
    if user.userroles.filter(role__role_name='head_admin').exists() or user.userroles.filter(role__role_name='news_admin').exists():
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to create news."}, status=status.HTTP_403_FORBIDDEN)

    # Extract news details
    news_title = request.data.get("news_title")
    content = request.data.get("content")
    category = request.data.get("category")
    related_event_id = request.data.get("related_event")
    images = request.FILES.get("images")

    # Validate required fields
    if not news_title or not content or not category:
        return Response({"message": "Title, content, and category are required."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate category choice
    valid_categories = ["general", "tournament", "bans"]
    if category not in valid_categories:
        return Response({"message": "Invalid category."}, status=status.HTTP_400_BAD_REQUEST)

    # Fetch related event if provided
    related_event = None
    if related_event_id:
        try:
            related_event = Event.objects.get(event_id=related_event_id)
        except Event.DoesNotExist:
            return Response({"message": "Related event not found."}, status=status.HTTP_404_NOT_FOUND)

    # Create the news
    news = News.objects.create(
        news_title=news_title,
        content=content,
        category=category,
        related_event=related_event,
        images=images,
        author=user
    )

    AdminHistory.objects.create(
        admin_user=user,
        action="created_news",
        description=f"News '{news_title}' created in category '{category}'"
    )

    return Response({
        "message": "News created successfully.",
        "news_id": news.news_id,
        "news_title": news.news_title,
        "category": news.category,
        "author_name": user.username,
        "author_pic": request.build_absolute_uri(user.userprofile.profile_pic.url) if hasattr(user, 'userprofile') and user.userprofile.profile_pic else None


    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def edit_news(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Extract news ID
    news_id = request.data.get("news_id")
    if not news_id:
        return Response({"message": "News ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Fetch news
    try:
        news = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is the author or an admin
    if news.author != user and user.role != "admin":
        return Response({"message": "You do not have permission to edit this news."}, status=status.HTTP_403_FORBIDDEN)
    
    if user.userroles.filter(role__role_name='head_admin').exists() or user.userroles.filter(role__role_name='news_admin').exists():
        pass  # User has permission
    else:
        return Response({"message": "You do not have permission to edit this news."}, status=status.HTTP_403_FORBIDDEN)

    # Extract new values (if provided)
    news_title = request.data.get("news_title", news.news_title)
    content = request.data.get("content", news.content)
    category = request.data.get("category", news.category)
    related_event_id = request.data.get("related_event", None)
    images = request.FILES.get("images", news.images)

    # Validate category if changed
    valid_categories = ["general", "tournament", "bans"]
    if category and category not in valid_categories:
        return Response({"message": "Invalid category."}, status=status.HTTP_400_BAD_REQUEST)

    # Update related event if changed
    if related_event_id:
        try:
            news.related_event = Event.objects.get(event_id=related_event_id)
        except Event.DoesNotExist:
            return Response({"message": "Related event not found."}, status=status.HTTP_404_NOT_FOUND)

    # Apply updates
    news.news_title = news_title
    news.content = content
    news.category = category
    news.images = images
    news.save()

    AdminHistory.objects.create(
        admin_user=user,
        action="edited_news",
        description=f"News '{news_title}' (ID: {news.news_id}) edited"
    )

    return Response({
        "message": "News updated successfully.",
        "news_id": news.news_id,
        "news_title": news.news_title,
        "category": news.category
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_news(request):
    news_list = News.objects.all().order_by('-created_at')
    news_data = []

    for news in news_list:
        news_data.append({
            "news_id": news.news_id,
            "news_title": news.news_title,
            "content": news.content,
            "category": news.category,
            "related_event": news.related_event.event_name if news.related_event else None,
            "images_url": request.build_absolute_uri(news.images.url) if news.images else None,
            "author": news.author.username,
            "created_at": news.created_at,
            # "updated_at": news.updated_at
        })

    return Response({"news": news_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def get_news_detail(request):
    news_id = request.data.get("news_id")
    if not news_id:
        return Response({"message": "News ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        news = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News not found."}, status=status.HTTP_404_NOT_FOUND)

    news_data = {
        "news_id": news.news_id,
        "news_title": news.news_title,
        "content": news.content,
        "category": news.category,
        "related_event": news.related_event.event_name if news.related_event else None,
        "images_url": request.build_absolute_uri(news.images.url) if news.images else None,
        "author": news.author.username,
        "created_at": news.created_at,
        # "updated_at": news.updated_at
    }

    return Response({"news": news_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def delete_news(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    

    # Extract news ID
    news_id = request.data.get("news_id")
    if not news_id:
        return Response({"message": "News ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Fetch news
    try:
        news = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is the author or an admin
    if news.author != user and user.role != "admin":
        if user.userroles.filter(role__role_name='head_admin').exists() or user.userroles.filter(role__role_name='news_admin').exists():
            pass  # User has news_editor role, allow deletion
        return Response({"message": "You do not have permission to delete this news."}, status=status.HTTP_403_FORBIDDEN)

    news.delete()

    return Response({"message": "News deleted successfully."}, status=status.HTTP_200_OK)

@api_view(["POST"])
def edit_profile(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Extract new profile details
    full_name = request.data.get("full_name")
    country = request.data.get("country")
    in_game_name = request.data.get("in_game_name")
    email = request.data.get("email")
    uid = request.data.get("uid")
    profile_pic = request.FILES.get("profile_pic")

    # Validate required fields
    if not all([full_name, country, in_game_name, email, uid]):
        return Response({"message": "All fields are required."}, status=status.HTTP_400_BAD_REQUEST)

    # Check for uniqueness conflicts
    if User.objects.exclude(pk=user.pk).filter(uid=uid).exists():
        return Response({"message": "UID is already in use by another user."}, status=status.HTTP_400_BAD_REQUEST)

    if User.objects.exclude(pk=user.pk).filter(email=email).exists():
        return Response({"message": "Email is already registered to another user."}, status=status.HTTP_400_BAD_REQUEST)

    if User.objects.exclude(pk=user.pk).filter(username=in_game_name).exists():
        return Response({"message": "In-game name is already taken."}, status=status.HTTP_400_BAD_REQUEST)

    # Update User fields
    user.full_name = full_name
    user.country = country
    user.username = in_game_name
    user.email = email
    user.uid = uid
    user.save()

    # Update or create UserProfile
    user_profile, created = UserProfile.objects.get_or_create(user=user)

    if profile_pic:
        user_profile.profile_pic = profile_pic
        user_profile.save()

    return Response({
        "message": "Profile updated successfully.",
        "user_id": user.user_id,
        "full_name": user.full_name,
        "country": user.country,
        "in_game_name": user.username,
        "email": user.email,
        "uid": user.uid,
        "profile_pic_url": request.build_absolute_uri(user_profile.profile_pic.url) if user_profile.profile_pic else None
    }, status=status.HTTP_200_OK)



@api_view(["GET"])
def get_user_profile(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Try to get UserProfile
    try:
        profile = UserProfile.objects.get(user=user)
        profile_pic_url = request.build_absolute_uri(profile.profile_pic.url) if profile.profile_pic else None
    except UserProfile.DoesNotExist:
        profile_pic_url = None

    # Return user info
    return Response({
        "user_id": user.user_id,
        "full_name": user.full_name,
        "country": user.country,
        "in_game_name": user.username,
        "email": user.email,
        "uid": user.uid,
        "team": user.team.team_name if hasattr(user, 'team') else None,
        "role": user.role,
        "profile_pic": profile_pic_url,
        "roles": list(UserRoles.objects.filter(user=user).values_list('role__role_name', flat=True)),
        "is_banned": BannedPlayer.objects.filter(banned_player=user, is_active=True).exists()
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def send_verification_token(request):
    email = request.data.get("email")

    if not email:
        return Response({"message": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({"message": "User with this email does not exist."}, status=status.HTTP_404_NOT_FOUND)

    # Generate a 6-digit token
    token = str(random.randint(100000, 999999))

    # Store or update token
    PasswordResetToken.objects.update_or_create(
        user=user,
        defaults={
            'token': token,
            'created_at': timezone.now()
        }
    )

    # Send email
    subject = "Your Password Reset Token"
    message = f"Your password reset token is: {token}"
    send_email(email, subject, message)

    return Response({"message": "Password reset token has been sent to your email."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def verify_token(request):
    email = request.data.get("email")
    token = request.data.get("token")

    if not email or not token:
        return Response({"message": "Email and token are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email)
        reset_token = PasswordResetToken.objects.get(user=user, token=token)
    except (User.DoesNotExist, PasswordResetToken.DoesNotExist):
        return Response({"message": "Invalid email or token."}, status=status.HTTP_400_BAD_REQUEST)

    if not reset_token.is_valid():
        return Response({"message": "Token has expired."}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"message": "Token is valid."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def reset_password(request):
    email = request.data.get("email")
    token = request.data.get("token")
    new_password = request.data.get("new_password")

    if not all([email, token, new_password]):
        return Response({"message": "Email, token, and new password are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email)
        reset_token = PasswordResetToken.objects.get(user=user, token=token)
    except (User.DoesNotExist, PasswordResetToken.DoesNotExist):
        return Response({"message": "Invalid email or token."}, status=status.HTTP_400_BAD_REQUEST)

    if not reset_token.is_valid():
        return Response({"message": "Token has expired."}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    user.save()

    reset_token.delete()  # remove token after successful password reset

    return Response({"message": "Password has been reset successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def resend_token(request):
    email = request.data.get("email")

    if not email:
        return Response({"message": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({"message": "User with this email does not exist."}, status=status.HTTP_404_NOT_FOUND)
    

    existing = PasswordResetToken.objects.filter(user=user).first()
    if existing and (timezone.now() - existing.created_at).seconds < 60:
        return Response({"message": "You must wait at least 1 minute before requesting a new token."},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)


    # Generate a new token
    token = str(random.randint(100000, 999999))

    # Update or create a reset token
    PasswordResetToken.objects.update_or_create(
        user=user,
        defaults={
            'token': token,
            'created_at': timezone.now()
        }
    )

    # Resend email
    subject = "Your New Password Reset Token"
    message = f"Your new password reset token is: {token}\nIt will expire in 10 minutes."
    send_email(email, subject, message)

    return Response({"message": "A new password reset token has been sent to your email."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def contact_us(request):
    name = request.data.get("name")
    email = request.data.get("email")
    message = request.data.get("message")

    if not all([email, name, message]):
        return Response({"message": "Email, name, and message are required."}, status=status.HTTP_400_BAD_REQUEST)

    # Send email to support
    support_email = 'africanfreefirecommunity1@gmail.com'
    email_subject = f"Contact Us Form Submission from {name}"
    email_body = f"Name: {name}\nEmail: {email}\nMessage: {message}"
    send_email(support_email, email_subject, email_body)

    return Response({"message": "Your message has been sent successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def get_admin_info(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    if not user.is_admin:
        return Response({"message": "User is not an admin."}, status=status.HTTP_403_FORBIDDEN)

    # Return admin information
    admin_info = {
        "id": user.id,
        "email": user.email,
        "name": user.username,
        "is_active": user.is_active,
        "status": user.status,
        "admin_roles": [role.name for role in user.roles.all()]
    }

    return Response({"message": "Admin information retrieved successfully.", "data": admin_info}, status=status.HTTP_200_OK)



@api_view(["GET"])
def get_all_roles(request):
    roles = Roles.objects.all()
    roles_data = [{"role_id": role.role_id, "role_name": role.role_name, "description": role.description} for role in roles]
    return Response({"roles": roles_data}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_user_and_user_roles(request):
    users = User.objects.all()
    users_data = []

    for user in users:
        user_roles = UserRoles.objects.filter(user=user)
        roles = [ur.role.role_name for ur in user_roles]
        users_data.append({
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "status": user.status,
            "last_login": user.last_login,
            "roles": roles,
            "created_at": user.created_at
        })

    return Response({"users": users_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def suspend_user(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    if user.role != "admin":
        return Response({"message": "You do not have permission to suspend a user."}, status=status.HTTP_403_FORBIDDEN)

    user_id = request.data.get("user_id")
    if not user_id:
        return Response({"message": "User ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(user_id=user_id)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    user.status = "suspended"
    user.save()

    AdminHistory.objects.create(
        admin_user=user,
        action="suspended_user",
        description=f"User {user.username} (ID: {user.user_id}) suspended"
    )

    return Response({"message": f"User {user.username} has been suspended."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def activate_user(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    if user.role != "admin":
        return Response({"message": "You do not have permission to activate a user."}, status=status.HTTP_403_FORBIDDEN)

    user_id = request.data.get("user_id")
    if not user_id:
        return Response({"message": "User ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(user_id=user_id)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    user.status = "active"
    user.save()

    AdminHistory.objects.create(
        admin_user=user,
        action="activated_user",
        description=f"User {user.username} (ID: {user.user_id}) activated"
    )

    return Response({"message": f"User {user.username} has been activated."}, status=status.HTTP_200_OK)


# @api_view(["POST"])
# def assign_roles_to_user(request):
#     # Retrieve session token
#     session_token = request.headers.get("Authorization")

#     if not session_token:
#         return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

#     if not session_token.startswith("Bearer "):
#         return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

#     session_token = session_token.split(" ")[1]

#     # Identify the logged-in user using the session token
#     try:
#         admin_user = User.objects.get(session_token=session_token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
#     if admin_user.role != "admin":
#         return Response({"message": "You do not have permission to assign roles."}, status=status.HTTP_403_FORBIDDEN)

#     username = request.data.get("username")
#     email = request.data.get("email")
#     role_ids = request.data.get("role_ids", [])

#     if not email or not username or not role_ids:
#         return Response({"message": "Email, username, and role IDs are required."}, status=status.HTTP_400_BAD_REQUEST)

#     try:
#         user = User.objects.get(email=email, username=username)
#     except User.DoesNotExist:
#         return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

#     for role_id in role_ids:
#         try:
#             role = Roles.objects.get(role_id=role_id)
#             UserRoles.objects.get_or_create(user=user, role=role)
#         except Roles.DoesNotExist:
#             return Response({"message": f"Role with ID {role_id} not found."}, status=status.HTTP_404_NOT_FOUND)

#     return Response({"message": f"Roles assigned to user {user.username} successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def assign_roles_to_user(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token or not session_token.startswith("Bearer "):
        return Response(
            {"status": "error", "message": "Authorization token is missing or invalid."},
            status=status.HTTP_400_BAD_REQUEST
        )

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        admin_user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response(
            {"status": "error", "message": "Invalid session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if admin_user.role != "admin":
        return Response(
            {"status": "error", "message": "You do not have permission to assign roles."},
            status=status.HTTP_403_FORBIDDEN
        )

    username = request.data.get("username")
    email = request.data.get("email")
    role_ids = request.data.get("role_ids", [])

    if not email or not username or not role_ids:
        return Response(
            {"status": "error", "message": "Email, username, and role IDs are required."},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        user = User.objects.get(email=email, username=username)
    except User.DoesNotExist:
        return Response(
            {"status": "error", "message": "User not found."},
            status=status.HTTP_404_NOT_FOUND
        )

    user.role = "admin"
    user.save()

    # Ensure role_ids is a list of integers
    if not isinstance(role_ids, list) or not all(isinstance(r, int) for r in role_ids):
        return Response(
            {"status": "error", "message": "role_ids must be a list of integers."},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate all roles first
    roles = Roles.objects.filter(role_id__in=role_ids)
    if len(roles) != len(role_ids):
        return Response(
            {"status": "error", "message": "One or more role IDs are invalid."},
            status=status.HTTP_404_NOT_FOUND
        )

    # 🔑 Reset: remove all existing roles first
    UserRoles.objects.filter(user=user).delete()

    # Assign new roles
    for role in roles:
        UserRoles.objects.create(user=user, role=role)



    AdminHistory.objects.create(
        admin_user=admin_user,
        action="assigned_roles",
        description=f"Assigned roles {', '.join([role.role_name for role in roles])} to user {user.username} (ID: {user.user_id})"
    )

    return Response(
        {"status": "success", "message": f"Roles reset and assigned to user {user.username} successfully."},
        status=status.HTTP_200_OK
    )



@api_view(["POST"])
def edit_user_roles(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        admin_user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    if admin_user.role != "admin":
        return Response({"message": "You do not have permission to edit user roles."}, status=status.HTTP_403_FORBIDDEN)

    username = request.data.get("username")
    email = request.data.get("email")
    new_role_ids = request.data.get("new_role_ids", [])

    if not email or not username or not new_role_ids:
        return Response({"message": "Email, username, and new role IDs are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email, username=username)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    # Clear existing roles
    UserRoles.objects.filter(user=user).delete()

    # Assign new roles
    for role_id in new_role_ids:
        try:
            role = Roles.objects.get(role_id=role_id)
            UserRoles.objects.create(user=user, role=role)
        except Roles.DoesNotExist:
            return Response({"message": f"Role with ID {role_id} not found."}, status=status.HTTP_404_NOT_FOUND)
    
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="edited_user_roles",
        description=f"Edited roles for user {user.username} (ID: {user.user_id})"
    )

    return Response({"message": f"User {user.username}'s roles updated successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def add_role(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        admin_user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    if admin_user.role != "admin":
        return Response({"message": "You do not have permission to add roles."}, status=status.HTTP_403_FORBIDDEN)
    
    if not admin_user.userroles.filter(role__role_name='head_admin').exists():
        return Response({"message": "You do not have permission to add roles."}, status=status.HTTP_403_FORBIDDEN)

    role_name = request.data.get("role_name")
    description = request.data.get("description", "")

    if not role_name:
        return Response({"message": "Role name is required."}, status=status.HTTP_400_BAD_REQUEST)

    if Roles.objects.filter(role_name=role_name).exists():
        return Response({"message": "Role name already exists."}, status=status.HTTP_400_BAD_REQUEST)

    role = Roles.objects.create(role_name=role_name, description=description)

    AdminHistory.objects.create(
        admin_user=admin_user,
        action="added_role",
        description=f"Added new role '{role_name}' (ID: {role.role_id})"
    )

    return Response({
        "message": "Role added successfully.",
        "role_id": role.role_id,
        "role_name": role.role_name,
        "description": role.description
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def delete_role(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        admin_user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    if admin_user.role != "admin":
        return Response({"message": "You do not have permission to delete roles."}, status=status.HTTP_403_FORBIDDEN)
    
    if not admin_user.userroles.filter(role__role_name='head_admin').exists():
        return Response({"message": "You do not have permission to delete roles."}, status=status.HTTP_403_FORBIDDEN)

    role_id = request.data.get("role_id")
    if not role_id:
        return Response({"message": "Role ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        role = Roles.objects.get(role_id=role_id)
    except Roles.DoesNotExist:
        return Response({"message": "Role not found."}, status=status.HTTP_404_NOT_FOUND)
    
    users_with_role = UserRoles.objects.filter(role=role)
    if users_with_role.exists():
        return Response({"message": "Cannot delete role assigned to users. Remove role from users first."}, status=status.HTTP_400_BAD_REQUEST)

    role_name = role.role_name
    role.delete()

    AdminHistory.objects.create(
        admin_user=admin_user,
        action="deleted_role",
        description=f"Deleted role '{role_name}' (ID: {role_id})"
    )

    return Response({"message": f"Role '{role_name}' has been deleted."}, status=status.HTTP_200_OK)

@api_view(["GET"])
def get_all_roles(request):
    roles = Roles.objects.all()
    roles_data = [{"role_id": role.role_id, "role_name": role.role_name, "description": role.description} for role in roles]
    return Response({"roles": roles_data}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_admin_history(request):
    histories = AdminHistory.objects.all().order_by('-timestamp')
    history_data = []

    for history in histories:
        history_data.append({
            "admin_user": history.admin_user.username,
            "action": history.action,
            "description": history.description,
            "timestamp": history.timestamp
        })

    return Response({"admin_history": history_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def search_admin_users(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    try:
        admin_user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    if admin_user.role != "admin":
        return Response({"message": "You do not have permission to search users."}, status=status.HTTP_403_FORBIDDEN)

    query = request.data.get("query", "")
    if not query:
        return Response({"message": "Search query is required."}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
def get_total_number_of_users(request):
    total_users = User.objects.count()
    verified_users = User.objects.filter(status="active").count()
    return Response({"total_users": total_users, "verified_users": verified_users}, status=status.HTTP_200_OK)


# @api_view(["GET"])
# def connect_discord(request):
#     session_token = request.GET.get("session_token")  # frontend must pass this
#     tournament_id = request.GET.get("tournament_id")

#     if not session_token:
#         return Response({"message": "session_token is required"}, status=400)

#     client_id = settings.DISCORD_CLIENT_ID
#     redirect_uri = settings.DISCORD_REDIRECT_URI

#     scope = "identify guilds.join"

#     discord_oauth_url = (
#         f"https://discord.com/api/oauth2/authorize?client_id={client_id}"
#         f"&redirect_uri={redirect_uri}"
#         f"&response_type=code&scope={scope}"
#         f"&state={session_token}|https://africanfreefirecommunity.com/tournaments/{tournament_id}" 
#     )

#     return redirect(discord_oauth_url)



@api_view(["GET"])
def connect_discord(request):
    session_token = request.GET.get("session_token")
    tournament_id = request.GET.get("tournament_id")

    if not session_token or not tournament_id:
        return Response({"message": "session_token and tournament_id required"}, status=400)

    client_id = settings.DISCORD_CLIENT_ID
    redirect_uri = settings.DISCORD_REDIRECT_URI

    scope = "identify guilds.join"

    # Encode the custom redirect URL
    from urllib.parse import quote
    return_url = quote(f"{settings.FRONTEND_URL}/tournaments/{tournament_id}")

    # state = session_token + return_url
    state = f"{session_token}|{return_url}"

    # Build OAuth URL
    discord_oauth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&state={state}"
    )

    return redirect(discord_oauth_url)


DISCORD_GUILD_ID = settings.DISCORD_GUILD_ID
DISCORD_BOT_TOKEN = settings.DISCORD_BOT_TOKEN

def check_discord_membership(discord_id):
    url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    return r.status_code == 200  # 200 means they are in the server


def assign_discord_role(discord_id, role_id):
    url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.put(url, headers=headers)
    return r.status_code == 204  # 204 = success


# @api_view(["GET"])
# def discord_callback(request):
#     code = request.GET.get("code")
#     session_token = request.GET.get("state")  # state contains session_token
#     tournament_id = request.GET.get("tournament_id")

#     if not code or not session_token:
#         return Response({"message": "Missing code or session_token"}, status=400)

#     # Get user
#     try:
#         user = User.objects.get(session_token=session_token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session"}, status=401)

#     # Exchange code → access token
#     data = {
#         "client_id": settings.DISCORD_CLIENT_ID,
#         "client_secret": settings.DISCORD_CLIENT_SECRET,
#         "grant_type": "authorization_code",
#         "code": code,
#         "redirect_uri": settings.DISCORD_REDIRECT_URI
#     }

#     token_res = requests.post(
#         "https://discord.com/api/oauth2/token",
#         data=data,
#         headers={"Content-Type": "application/x-www-form-urlencoded"}
#     )

#     if token_res.status_code != 200:
#         return Response({"message": "Failed to get Discord token"}, status=400)

#     token_data = token_res.json()
#     access_token = token_data["access_token"]

#     # Fetch Discord user
#     me = requests.get(
#         "https://discord.com/api/users/@me",
#         headers={"Authorization": f"Bearer {access_token}"}
#     ).json()

#     discord_id = me["id"]

#     # Auto-join them to discord server
#     join_payload = {
#         "access_token": access_token
#     }

#     join_res = requests.put(
#         f"https://discord.com/api/guilds/{settings.DISCORD_GUILD_ID}/members/{discord_id}",
#         json=join_payload,
#         headers={"Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}"}
#     )

#     # (200, 201, 204) = success
#     if join_res.status_code not in [200, 201, 204]:
#         return Response({"message": "Failed to join Discord server"}, status=400)

#     # Save Discord info
#     user.discord_id = discord_id
#     user.discord_username = me["username"]
#     user.discord_connected = True
#     user.save()

#     final_redirect = f"{settings.FRONTEND_URL}/tournaments/{tournament_id}?discord=connected"

#     return redirect(final_redirect)

#     # return Response({
#     #     "message": "Discord connected successfully",
#     #     "discord_username": me["username"]
#     # })



# @api_view(["GET"])
# def discord_callback(request):
#     code = request.GET.get("code")
#     state = request.GET.get("state")

#     if not code or not state:
#         # Redirect back with success flag
#         error_redirect = f"{return_url}?discord=failed"
#         return redirect(error_redirect)
#         # return Response({"message": "Missing code or state"}, status=400)

#     # Extract session_token and encoded return_url
#     try:
#         session_token, encoded_return_url = state.split("|")
#     except ValueError:
#         return Response({"message": "Invalid state format"}, status=400)

#     from urllib.parse import unquote
#     return_url = unquote(encoded_return_url)

#     # Get user
#     try:
#         user = User.objects.get(session_token=session_token)
#     except User.DoesNotExist:
#         return Response({"message": "Invalid session"}, status=401)

#     # Exchange code → token
#     data = {
#         "client_id": settings.DISCORD_CLIENT_ID,
#         "client_secret": settings.DISCORD_CLIENT_SECRET,
#         "grant_type": "authorization_code",
#         "code": code,
#         "redirect_uri": settings.DISCORD_REDIRECT_URI
#     }

#     token_res = requests.post(
#         "https://discord.com/api/oauth2/token",
#         data=data,
#         headers={"Content-Type": "application/x-www-form-urlencoded"}
#     )

#     if token_res.status_code != 200:
#         return Response({"message": "Failed to get Discord token"}, status=400)

#     access_token = token_res.json()["access_token"]

#     # Fetch Discord user
#     me = requests.get(
#         "https://discord.com/api/users/@me",
#         headers={"Authorization": f"Bearer {access_token}"}
#     ).json()

#     discord_id = me["id"]

#     # Auto-join Discord Guild
#     join_payload = {"access_token": access_token}

#     join_res = requests.put(
#         f"https://discord.com/api/guilds/{settings.DISCORD_GUILD_ID}/members/{discord_id}",
#         json=join_payload,
#         headers={"Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}"}
#     )

#     if join_res.status_code not in [200, 201, 204]:
#         return Response({"message": "Failed to join Discord server"}, status=400)

#     # Save Discord info
#     user.discord_id = discord_id
#     user.discord_username = me["username"]
#     user.discord_connected = True
#     user.save()

#     # Redirect back with success flag
#     final_redirect = f"{return_url}?discord=connected"

#     return redirect(final_redirect)



@api_view(["GET"])
def discord_callback(request):
    code = request.GET.get("code")
    state = request.GET.get("state")
    error = request.GET.get("error")   

    # If the user clicked "Cancel", Discord sends ?error=access_denied
    if error:
        # state may still be valid, so try parsing return URL
        try:
            session_token, encoded_return_url = state.split("|")
            from urllib.parse import unquote
            return_url = unquote(encoded_return_url)
        except:
            return redirect(f"{settings.FRONTEND_URL}?discord=failed")

        return redirect(f"{return_url}?discord=failed")

    # If state/code missing → fail safe
    if not code or not state:
        return redirect(f"{settings.FRONTEND_URL}?discord=failed")

    # Extract session_token and encoded return URL
    try:
        session_token, encoded_return_url = state.split("|")
        from urllib.parse import unquote
        return_url = unquote(encoded_return_url)
    except:
        return redirect(f"{settings.FRONTEND_URL}?discord=failed")

    fail_redirect = f"{return_url}?discord=failed"

    # Validate user session
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return redirect(fail_redirect)

    # ---- Exchange code → access token ----
    try:
        token_res = requests.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": settings.DISCORD_CLIENT_ID,
                "client_secret": settings.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.DISCORD_REDIRECT_URI
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        if token_res.status_code != 200:
            return redirect(fail_redirect)

        access_token = token_res.json().get("access_token")
        if not access_token:
            return redirect(fail_redirect)
    except:
        return redirect(fail_redirect)

    # ---- Fetch Discord user ----
    try:
        me = requests.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        if me.status_code != 200:
            return redirect(fail_redirect)

        me = me.json()
        discord_id = me.get("id")
        if not discord_id:
            return redirect(fail_redirect)
    except:
        return redirect(fail_redirect)

    # ---- Add to Discord server ----
    try:
        join_res = requests.put(
            f"https://discord.com/api/guilds/{settings.DISCORD_GUILD_ID}/members/{discord_id}",
            json={"access_token": access_token},
            headers={"Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}"},
            timeout=10
        )
        if join_res.status_code not in [200, 201, 204]:
            return redirect(fail_redirect)
    except:
        return redirect(fail_redirect)

    # ---- Save user ----
    try:
        user.discord_id = discord_id
        user.discord_username = me.get("username", "")
        user.discord_connected = True
        user.save()
    except:
        return redirect(fail_redirect)

    # ---- SUCCESS ----
    return redirect(f"{return_url}?discord=connected")


@api_view(["GET"])
def get_all_login_history(request):
    histories = LoginHistory.objects.all().order_by('-created_at')
    history_data = []

    for history in histories:
        history_data.append({
            "user": history.user.username,
            "ip_address": history.ip_address,
            "user_agent": history.user_agent,
            "country": history.country,
            "country_code": history.country_code,
            "continent": history.continent,
            "timestamp": history.created_at
        })

    return Response({"login_history": history_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def get_user_login_history(request):
    username = request.data.get("username")
    email = request.data.get("email")

    if not username or not email:
        return Response({"message": "Username and email are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(username=username, email=email)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    histories = LoginHistory.objects.filter(user=user).order_by('-created_at')
    history_data = []

    for history in histories:
        history_data.append({
            "ip_address": history.ip_address,
            "user_agent": history.user_agent,
            "timestamp": history.created_at
        })

    return Response({"login_history": history_data}, status=status.HTTP_200_OK)