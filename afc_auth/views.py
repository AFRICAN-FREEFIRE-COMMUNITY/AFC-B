from django.shortcuts import render
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
from .models import User, UserProfile, BannedPlayer, News
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

def generate_session_token(length=16):
    """Generate a random 16-character token."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))


def send_email(to_address, subject, html_body):
    # Gmail SMTP server credentials
    smtp_server = 'smtp.gmail.com'
    smtp_port = 465  # or 587 for TLS
    from_address = 'africanfreefirecommunity1@gmail.com' #vermillioninformation@gmail.com, Info@v-ent.co
    password = 'rvgn rzha ihli dfdp'  # Or your actual Gmail password (if less secure apps are enabled)

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
        user.save()

        # Return success response with the session token
        return Response({
            'message': 'Login successful', 
            'session_token': session_token,
            'user': {
                'id': user.user_id,
                'username': user.username,
            }
        }, status=status.HTTP_200_OK)
    else:
        # Authentication failed, return error response
        return Response({
            'message': 'Invalid username/email or password'
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

        # Check if email is already in use
        if User.objects.filter(email=email).exists():
            return Response({"error": "Email is already in use."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if in_game_name is already in use
        if User.objects.filter(username=in_game_name).exists():
            return Response({"error": "In-game name is already in use."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if uid is already in use
        if User.objects.filter(uid=uid).exists():
            return Response({"error": "UID is already in use."}, status=status.HTTP_400_BAD_REQUEST)

        # Create new user if they don't exist
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

        # Generate new verification code
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
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify admin/moderator
    try:
        user = User.objects.get(session_token=session_token)
        if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

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

    return Response({
        "message": "Team banned successfully.",
        "team_id": team.team_id,
        "ban_end_date": ban_end_date,
        "reason": reason
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def unban_team(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify admin/moderator
    try:
        user = User.objects.get(session_token=session_token)
        if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

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

    return Response({"message": "Team unbanned successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def ban_player(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Check if the user has permission to ban a player
    if user.role not in ["admin", "moderator", "support"]:
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

    return Response({
        "message": f"Player {player_ign} has been banned for {duration} days.",
        "ban_id": ban_entry.ban_id
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def create_news(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Check if the user has permission to create news
    if user.role not in ["admin", "moderator", "support"]:
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

    return Response({
        "message": "News created successfully.",
        "news_id": news.news_id,
        "news_title": news.news_title,
        "category": news.category
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def edit_news(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

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

    return Response({
        "message": "News updated successfully.",
        "news_id": news.news_id,
        "news_title": news.news_title,
        "category": news.category
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def edit_profile(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

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

    # Validate required fields
    if not all([full_name, country, in_game_name, email, uid]):
        return Response({"message": "All fields are required."}, status=status.HTTP_400_BAD_REQUEST)

    # Update user profile
    user.full_name = full_name
    user.country = country
    user.username = in_game_name
    user.email = email
    user.uid = uid
    user.save()

    return Response({
        "message": "Profile updated successfully.",
        "user_id": user.user_id,
        "full_name": user.full_name,
        "country": user.country,
        "in_game_name": user.username,
        "email": user.email,
        "uid": user.uid,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_user_profile(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({"message": "Session token is required."}, status=status.HTTP_401_UNAUTHORIZED)

    # Identify the logged-in user using the session token
    try:
        user = User.objects.get(session_token=session_token)
    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)

    # Return user info
    return Response({
        "user_id": user.user_id,
        "full_name": user.full_name,
        "country": user.country,
        "in_game_name": user.username,
        "email": user.email,
        "uid": user.uid,
    }, status=status.HTTP_200_OK)