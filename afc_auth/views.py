import os

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
from .models import AdminHistory, AuditLog, DiscordRoleAssignment, LoginHistory, LoginHistory, NewsDislike, NewsLike, NewsViews, Notifications, Roles, SessionToken, User, UserProfile, BannedPlayer, News, PasswordResetToken, UserRoles
# set_audit lets these admin views supply a SPECIFIC human audit summary (entity name + before/after)
# that the AuditLogMiddleware records instead of its generic "Edited ... #id" fallback.
from afc_auth.audit import set_audit
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
from afc_team.models import Team, TeamMembers
from django.utils import timezone
from .models import TeamBan
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils.dateparse import parse_datetime
from afc_tournament_and_scrims.models import Event, Match, RegisteredCompetitors, SoloPlayerMatchStats, TournamentPlayerMatchStats, TournamentTeam, TournamentTeamMatchStats, TournamentTeamMember
from django.contrib.auth.hashers import make_password, check_password
from django.core.cache import cache
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
import requests
from django.conf import settings
from .models import Notifications
from django.db.models import Count
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Avg
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.db import IntegrityError  # backstop for the username/email unique-constraint race in signup()
from django.db.models import Q  # OR-query for the unverified-takeover lookup in signup()
from utils.ipinfo_lookup import lookup_ip

import re

# Popular email domains whitelist
ALLOWED_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
    "aol.com",
    "protonmail.com",
    "zoho.com",
    "mail.com",
    "gmx.com",
    "yandex.com",
    "live.com",
    "msn.com",
    "me.com",
    "inbox.com"
}


def is_valid_email(email: str) -> tuple[bool, str]:
    if not email:
        return False, "Email is required."

    # Basic format check
    email_regex = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    if not re.match(email_regex, email):
        return False, "Invalid email format."

    # Extract domain
    domain = email.split("@")[-1].lower()

    # Check if domain is allowed
    if domain not in ALLOWED_EMAIL_DOMAINS:
        return False, "Please use a valid email provider (e.g., Gmail, Yahoo)."

    return True, "Valid email."


def get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        # X-Forwarded-For may contain multiple IPs
        ip = x_forwarded_for.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip

def validate_token(token):
    try:
        session = SessionToken.objects.get(token=token)
        if session.is_expired():
            return None
        return session.user
    except SessionToken.DoesNotExist:
        return None


def require_admin(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)

    admin = validate_token(auth.split(" ")[1])
    if not admin:
        return None, Response({"message": "Invalid or expired session token."}, status=401)

    if admin.role != "admin":
        return None, Response({"message": "You do not have permission."}, status=403)

    return admin, None


def _user_role_names(user):
    """Granular role names a user holds (the UserRoles -> Roles.role_name set). Cheap, one query."""
    if not user:
        return set()
    return set(user.userroles.values_list("role__role_name", flat=True))


def _is_super_admin(user):
    """super_admin = the top role, above head_admin. Only a super_admin can manage the super_admin
    role or modify another super_admin (see assign_roles_to_user / edit_user_roles)."""
    return bool(user) and ("super_admin" in _user_role_names(user) or user.is_superuser)


def require_head_admin(request):
    """Gate for the most sensitive admin surfaces (e.g. the audit log). Allows ONLY head_admin or
    super_admin (NOT plain role=='admin'). Returns (user, None) on success, (None, Response) on
    failure - same shape as require_admin."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    roles = _user_role_names(user)
    if "head_admin" not in roles and "super_admin" not in roles and not user.is_superuser:
        return None, Response({"message": "Head admin access required."}, status=403)
    return user, None



def generate_session_token(length=16):
    """Generate a random 16-character token."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

import smtplib
import os
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_email(to_address, subject, html_body):
    try: 
        is_valid, message = is_valid_email(to_address)
        if not is_valid:
            print(f"Invalid email address: {to_address}. Error: {message}")
            return False
    except Exception as e:
        print(f"Error validating email address: {to_address}. Exception: {e}")
        return False


    smtp_server = 'smtp.office365.com'
    smtp_port = 587

    from_address = 'info@africanfreefirecommunity.com'
    password = os.getenv("EMAIL_PASSWORD")

    try:
        msg = MIMEMultipart()
        msg['From'] = from_address
        msg['To'] = to_address
        msg['Subject'] = subject

        msg.attach(MIMEText(html_body, 'html'))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.ehlo()
        server.starttls()
        server.ehlo()

        server.login(from_address, password)

        server.sendmail(from_address, to_address, msg.as_string())
        server.quit()

        print("EMAIL SENT SUCCESSFULLY")
        return True

    except Exception as e:
        traceback.print_exc()
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Branded transactional email templates (2026-06-08)
# ──────────────────────────────────────────────────────────────────────────────
# Build the HTML bodies passed to send_email(). One shared shell (_email_shell) gives every
# AFC email the same dark branded look (wordmark header, dark card, footer + disclaimer); each
# builder fills the middle. EMAIL-SAFE HTML ONLY: tables + inline styles + web-safe fonts (no
# flexbox / external CSS), so it renders consistently across mail clients. accent='green' for
# normal mail, 'gold' for password/security mail. Used by: signup + resend (verification code),
# verify_code (welcome), request/reset password token (reset token), reset_password +
# change_password (password-changed confirmation). Replaces the old plain-text bodies.
SITE_URL = "https://africanfreefirecommunity.com"


def _email_shell(body_inner_html, accent="green"):
    """Wrap an email's inner table rows in the shared AFC branded shell. `body_inner_html` is the
    <tr>...</tr> content between the header and footer; `accent` is 'green' (default) or 'gold'."""
    a = "#34d27b" if accent == "green" else "#f5c518"
    grad = "linear-gradient(135deg,#0c1f15 0%,#0f1411 60%)" if accent == "green" \
        else "linear-gradient(135deg,#1f1608 0%,#0f1411 60%)"
    hb = "#1d2a22" if accent == "green" else "#2a2113"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:24px 0;background:#070a08;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#0f1411;border:1px solid #1d2a22;border-radius:16px;overflow:hidden;">
  <tr><td style="background:{grad};padding:28px 0 22px;text-align:center;border-bottom:1px solid {hb};">
    <!-- Actual AFC logo (owner request 2026-06-09) on a white badge. The logo art has dark
         outlines + black tagline text, so a white badge keeps it readable on the dark header
         regardless of the PNG's own background. Hosted at the site root: Next public/logo.png
         -> https://africanfreefirecommunity.com/logo.png. An absolute hosted URL is required
         because mail clients cannot load local/inline files. This shell wraps EVERY AFC email
         (verification, welcome, reset, password-changed, and the shop/vendor order emails), so
         the logo now appears in all of them. -->
    <table role="presentation" cellpadding="0" cellspacing="0" align="center"><tr><td style="background:#ffffff;border-radius:14px;padding:9px 14px;">
      <img src="{SITE_URL}/logo.png" alt="African Free Fire Community" width="92" height="92" style="display:block;border:0;outline:none;width:92px;height:auto;">
    </td></tr></table>
  </td></tr>
  {body_inner_html}
  <tr><td style="padding:18px 44px 30px;border-top:1px solid #1d2a22;">
    <div style="font-size:12px;color:#55635a;">African Free Fire Community &nbsp;&bull;&nbsp; <a href="{SITE_URL}" style="color:{a};text-decoration:none;">africanfreefirecommunity.com</a></div>
  </td></tr>
</table></td></tr></table></body></html>"""


def email_verification_code(username, code):
    """Signup / resend verification-code email (green). Consumed by signup + resend_code."""
    inner = f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">Verify your account</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">Hi <span style="color:#e8efe9;font-weight:600;">{username}</span>, welcome to the arena. Enter this code on <a href="{SITE_URL}/verify" style="color:#34d27b;text-decoration:none;font-weight:600;">africanfreefirecommunity.com</a> to finish creating your account.</div>
  </td></tr>
  <tr><td style="padding:24px 44px 8px;" align="center">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:#0a120d;border:1px solid #2c7a4d;border-radius:12px;padding:18px 34px;">
      <span style="font-size:38px;font-weight:800;letter-spacing:12px;color:#34d27b;font-family:Consolas,Menlo,monospace;">{code}</span>
    </td></tr></table>
  </td></tr>
  <tr><td style="padding:14px 44px 26px;text-align:center;"><div style="font-size:13px;color:#7c8c83;">This code expires in 10 minutes.</div></td></tr>
  <tr><td style="padding:0 44px 8px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">If you did not create an AFC account, you can safely ignore this email. Never share this code with anyone, AFC staff will never ask for it.</div>
  </td></tr>"""
    return _email_shell(inner, "green")


def email_welcome(username):
    """Welcome / account-created email, sent after verification succeeds (green). verify_code."""
    inner = f"""
  <tr><td style="padding:40px 44px 6px;text-align:center;">
    <div style="font-size:46px;">&#127881;</div>
    <div style="font-size:23px;font-weight:800;color:#ffffff;margin-top:10px;">You're in, {username}</div>
    <div style="font-size:15px;line-height:1.65;color:#aab5ae;margin-top:12px;">Your account is verified and ready. Join tournaments, climb the rankings, build your team, and rep your country across Africa.</div>
  </td></tr>
  <tr><td style="padding:26px 44px 10px;" align="center">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:#34d27b;border-radius:10px;">
      <a href="{SITE_URL}" style="display:inline-block;padding:14px 40px;font-size:15px;font-weight:700;color:#062012;text-decoration:none;">Enter the Community</a>
    </td></tr></table>
  </td></tr>
  <tr><td style="padding:22px 44px 30px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="33%" style="text-align:center;padding:8px;"><div style="font-size:22px;">&#127942;</div><div style="font-size:12px;color:#8b988f;margin-top:6px;">Compete in tournaments</div></td>
      <td width="33%" style="text-align:center;padding:8px;"><div style="font-size:22px;">&#128200;</div><div style="font-size:12px;color:#8b988f;margin-top:6px;">Climb the rankings</div></td>
      <td width="33%" style="text-align:center;padding:8px;"><div style="font-size:22px;">&#129309;</div><div style="font-size:12px;color:#8b988f;margin-top:6px;">Find your team</div></td>
    </tr></table>
  </td></tr>"""
    return _email_shell(inner, "green")


def email_reset_token(token):
    """Forgot-password reset-token email (gold security accent). reset-password request views."""
    inner = f"""
  <tr><td style="padding:38px 44px 8px;">
    <div style="font-size:21px;font-weight:700;color:#ffffff;">Reset your password</div>
    <div style="font-size:15px;line-height:1.6;color:#aab5ae;margin-top:12px;">We received a request to reset your password. Use the token below to set a new one.</div>
  </td></tr>
  <tr><td style="padding:24px 44px 8px;" align="center">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:#16120a;border:1px solid #7a611f;border-radius:12px;padding:18px 34px;">
      <span style="font-size:34px;font-weight:800;letter-spacing:10px;color:#f5c518;font-family:Consolas,Menlo,monospace;">{token}</span>
    </td></tr></table>
  </td></tr>
  <tr><td style="padding:14px 44px 26px;text-align:center;"><div style="font-size:13px;color:#7c8c83;">This token expires in 10 minutes.</div></td></tr>
  <tr><td style="padding:0 44px 8px;">
    <div style="font-size:12px;line-height:1.6;color:#6b7a71;">If you did not request a password reset, ignore this email, your password stays unchanged. Never share this token.</div>
  </td></tr>"""
    return _email_shell(inner, "gold")


def email_password_changed(username, when_text):
    """Password-changed confirmation (green). reset_password + change_password."""
    inner = f"""
  <tr><td style="padding:40px 44px 6px;text-align:center;">
    <div style="width:64px;height:64px;line-height:64px;border-radius:50%;background:#0a120d;border:1px solid #2c7a4d;margin:0 auto;font-size:30px;color:#34d27b;">&#10003;</div>
    <div style="font-size:21px;font-weight:700;color:#ffffff;margin-top:18px;">Your password was changed</div>
    <div style="font-size:15px;line-height:1.65;color:#aab5ae;margin-top:12px;">This confirms the password for <span style="color:#e8efe9;font-weight:600;">{username}</span> was updated on {when_text}.</div>
  </td></tr>
  <tr><td style="padding:24px 44px 30px;">
    <div style="background:#16120a;border:1px solid #4a3a14;border-radius:10px;padding:14px 18px;font-size:13px;line-height:1.6;color:#d8c98f;">
      Did not do this? Your account may be at risk. Reset your password immediately and contact <a href="{SITE_URL}/contact" style="color:#f5c518;text-decoration:none;">support</a>.
    </div>
  </td></tr>"""
    return _email_shell(inner, "green")


# def send_email(to_address, subject, html_body):
#     # Gmail SMTP server credentials
#     # smtp_server = 'smtp.gmail.com'
#     # smtp_port = 465  # or 587 for TLS
#     smtp_server = 'smtp.office365.com'
#     smtp_port = 587
#     from_address = 'info@africanfreefirecommunity.com' #vermillioninformation@gmail.com, Info@v-ent.co, africanfreefirecommunity3@gmail.com
#     password = os.getenv("EMAIL_PASSWORD")  

#     try:
#         msg = MIMEMultipart()
#         msg['From'] = from_address
#         msg['To'] = to_address
#         msg['Subject'] = subject

#         msg.attach(MIMEText(html_body, 'html'))

#         server = smtplib.SMTP(smtp_server, smtp_port)
#         server.starttls()  # IMPORTANT for Microsoft
#         server.login(from_address, password)

#         server.sendmail(from_address, to_address, msg.as_string())
#         server.quit()

#         return True

#     except Exception as e:
#         print(e)
#         return False


    # password = 'wobd dlxw riuh tsnm'
    #'yyzm prff sjfo bcmg2'
    # 'rvgn rzha ihli dfdp1'  # Or your actual Gmail password (if less secure apps are enabled)

# wobd dlxw riuh tsnm africanfreefirecommunity3@gmail.com

    # try:
    #     # Create a MIMEMultipart email object
    #     msg = MIMEMultipart()
    #     msg['From'] = from_address
    #     msg['To'] = to_address
    #     msg['Subject'] = subject

    #     # Attach the HTML body to the MIME message
    #     msg.attach(MIMEText(html_body, 'html'))

    #     # Set up the SMTP connection using SSL
    #     server = smtplib.SMTP_SSL(smtp_server, smtp_port)
    #     server.login(from_address, password)
        
    #     # Send the email
    #     server.sendmail(from_address, to_address, msg.as_string())
    #     server.quit()

    #     return True
    # except Exception as e:
    #     return False


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


        # 🔥 CLEAR OLD SESSIONS
        SessionToken.objects.filter(user=user).delete()

        # Generate a session token
        session_token = generate_session_token()

        SessionToken.objects.create(user=user, token=session_token)

        # Save session token to the user model
        user.last_login = timezone.now()
        user.save()

        ip = get_client_ip(request)
        response = requests.get(f"https://ipinfo.io/{ip}/json")
        response = response.json()

        

        LoginHistory.objects.create(
            user=user,
            ip_address=ip,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            country=response.get("country"),
            city=response.get("city"),
            region=response.get("region"),
            timezone=response.get("timezone")
        )

        # Return success response with the session token
        return Response({
            'message': 'Login successful', 
            'session_token': session_token,
            'user': {
                'id': user.user_id,
                'username': user.username,
            },
            "geo": response
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

    ip = get_client_ip(request)
    response = requests.get(f"https://ipinfo.io/{ip}/json")
    response = response.json()
    country=response.get("country")

    try:
        # Validation
        if not all([in_game_name, email, password, confirm_password]):
            return Response({"error": "All fields are required."}, status=status.HTTP_400_BAD_REQUEST)
        
        # is_valid, message = is_valid_email(email)

        # if not is_valid:
        #     return Response({"error": message}, status=400)

        if password != confirm_password:
            return Response({"error": "Passwords do not match."}, status=status.HTTP_400_BAD_REQUEST)

        # ── Uniqueness pre-checks (return FRIENDLY 400s, never the raw DB 1062 error) ──
        #
        # "Verified" flag note: on this User model `is_active` IS the email-verified flag.
        #   - signup() creates the user with is_active=False (see create() below);
        #   - verify_code() flips is_active=True only after the emailed code is confirmed;
        #   - login() (this file, ~line 421) refuses any user with is_active=False.
        # So is_active=False reliably means "abandoned signup: never verified, never logged in,
        # holds no real account data". is_active=True means a live, owned account.
        #
        # We check uniqueness against the DB unique constraints (username, email, uid) BEFORE
        # inserting so the user gets a clear message instead of the raw MySQL
        # `(1062, "Duplicate entry ...")` that the bare insert would otherwise surface.

        # Username (in-game name) conflict.
        username_clash = User.objects.filter(username=in_game_name).first()
        if username_clash and username_clash.is_active:
            # Owned by a real, verified account → always reject, never take over.
            return Response(
                {"message": "That in-game name is already taken. Please choose another."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Email conflict.
        email_clash = User.objects.filter(email=email).first()
        if email_clash and email_clash.is_active:
            # Owned by a real, verified account → always reject, never take over.
            return Response(
                {"message": "That email is already registered. Try logging in or resetting your password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # UID conflict (only checked when a UID was supplied). Same verified-only rule.
        if uid:
            uid_clash = User.objects.filter(uid=uid).first()
            if uid_clash and uid_clash.is_active:
                return Response(
                    {"message": "That UID is already in use. Please use a different one."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── UNVERIFIED TAKEOVER (the core unblock) ──
        #
        # If the only thing holding the in-game name / email / UID is an UNVERIFIED user
        # (is_active=False), that signup was abandoned (e.g. the owner typed the wrong email
        # the first time, never got the code, and is now retrying with the right one). Such a
        # row carries no real data, so we delete it and let the fresh registration proceed.
        # This is what stops an abandoned wrong-email attempt from permanently locking the name.
        #
        # SECURITY: we ONLY ever delete is_active=False rows. A verified/active account is
        # rejected above and never reaches here, so a takeover can never wipe a real account
        # or let an attacker hijack someone's confirmed username/email. The CASCADE on
        # UserProfile (and any other FK to this user) cleans up the stale profile too.
        stale_unverified = User.objects.filter(is_active=False).filter(
            Q(username=in_game_name) | Q(email=email)
        )
        if uid:
            stale_unverified = stale_unverified | User.objects.filter(is_active=False, uid=uid)
        stale_unverified = stale_unverified.distinct()

        # `country` comes from a best-effort ipinfo lookup that can fail or return None;
        # the column is NOT NULL, so coalesce to "" to avoid a spurious IntegrityError.
        country = country or ""

        # Wrap delete + create in one transaction so we never end up half-deleted on failure.
        # NOTE: the IntegrityError backstop is handled by the OUTER try/except (below), not
        # inside this block. Once a query raises IntegrityError inside an atomic block the
        # transaction is poisoned and no further queries may run until it exits, so the
        # "which field clashed?" lookups must happen AFTER the block has rolled back.
        try:
            with transaction.atomic():
                if stale_unverified.exists():
                    stale_unverified.delete()

                # Create new user (is_active=False → unverified until verify_code runs).
                user = User.objects.create(
                    username=in_game_name,
                    uid=uid if uid else None,
                    email=email,
                    is_active=False,
                    full_name=full_name,
                    country=country
                )
                user.set_password(password)
                user.save()
                UserProfile.objects.create(user=user)
        except IntegrityError:
            # Backstop for the race (two signups for the same name between our pre-check and
            # the insert) or any unique constraint we did not pre-check. The transaction has
            # rolled back, so it is now safe to query which field clashed. We map straight to
            # the friendly messages and NEVER leak the raw MySQL (1062, "Duplicate entry ...").
            if User.objects.filter(username=in_game_name).exists():
                return Response(
                    {"message": "That in-game name is already taken. Please choose another."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"message": "That email is already registered. Try logging in or resetting your password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Generate verification code
        verification_code = random.randint(100000, 999999)
        cache.set(f"verification_code_{user.user_id}", verification_code, timeout=600)

        # Send verification email
        subject = 'Verify your AFC account'
        message = email_verification_code(in_game_name, verification_code)
        try:
            send_email(email, subject, message)
        except Exception as e:
            print(f"Error sending email: {e}")
            return Response({"error": "Failed to send verification email. Please try again later."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"message": "Signup successful. Please check your email for the verification code."}, status=status.HTTP_201_CREATED)

    except IntegrityError:
        # Final safety net: any unique-constraint error that escapes the inner handler still
        # returns a friendly message rather than the raw MySQL (1062, "Duplicate entry ...").
        return Response(
            {"message": "That in-game name or email is already registered. Please try a different one or log in."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        # Log the real error server-side for debugging, but never echo internal/DB details
        # (which previously leaked the raw 1062 message) back to the user.
        print(f"Signup unexpected error: {e}")
        return Response(
            {"message": "Something went wrong while creating your account. Please try again."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
def verify_code(request):
    email = request.data.get("email")
    code = request.data.get("code")

    is_valid, message = is_valid_email(email)

    if not is_valid:
        return Response({"error": message}, status=400)

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

    # Welcome / account-created email. Best-effort: a mail failure must never block the
    # account from being verified, so we swallow any send error.
    try:
        send_email(user.email, "Welcome to African Free Fire Community", email_welcome(user.username))
    except Exception as e:
        print(f"Welcome email failed for {user.email}: {e}")

    return Response({"message": "Account verified successfully."}, status=status.HTTP_200_OK)


from django.core.cache import cache
import random
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import api_view

@api_view(["POST"])
def resend_verification_code(request):
    email = request.data.get("email")

    if not email:
        return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)
    
    is_valid, message = is_valid_email(email)

    if not is_valid:
        return Response({"error": message}, status=400)

    user = User.objects.filter(email=email).first()

    if not user:
        return Response({"error": "No account found with this email."}, status=status.HTTP_404_NOT_FOUND)

    if user.is_active:
        return Response({"error": "This account is already verified."}, status=status.HTTP_400_BAD_REQUEST)

    # 🔒 Check resend cooldown (4 mins)
    cooldown_key = f"resend_cooldown_{user.user_id}"
    if cache.get(cooldown_key):
        ttl = cache.ttl(cooldown_key)
        return Response(
            {"error": f"Please wait {ttl} seconds before requesting another code."},
            status=status.HTTP_429_TOO_MANY_REQUESTS
        )
    
    

    # 🔢 Generate new verification code
    verification_code = random.randint(100000, 999999)

    # 🕒 Store code (10 mins)
    cache.set(f"verification_code_{user.user_id}", verification_code, timeout=600)

    # ⏳ Set cooldown (4 mins = 240 seconds)
    cache.set(cooldown_key, True, timeout=240)

    # 📧 Send email
    subject = "Your new AFC verification code"
    message = email_verification_code(user.username, verification_code)
    send_email(user.email, subject, message)

    return Response(
        {"message": "A new verification code has been sent to your email."},
        status=status.HTTP_200_OK
    )


# @api_view(["POST"])
# def resend_verification_code(request):
#     email = request.data.get("email")

#     if not email:
#         return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

#     user = User.objects.filter(email=email).first()

#     if not user:
#         return Response({"error": "No account found with this email."}, status=status.HTTP_404_NOT_FOUND)

#     if user.is_active:
#         return Response({"error": "This account is already verified."}, status=status.HTTP_400_BAD_REQUEST)

#     # Generate new verification code
#     verification_code = random.randint(100000, 999999)

#     # Store in cache with a 10-minute expiry
#     cache.set(f"verification_code_{user.user_id}", verification_code, timeout=600)

#     # Send the new verification email
#     subject = "Your New Verification Code"
#     message = f'''Hi {user.username},

# You requested a new verification code.

# Your new verification code is: {verification_code}

# Please enter this code in the app to verify your account.

# If you did not request this, please ignore this email.
# '''
#     send_email(user.email, subject, message)

#     return Response({"message": "A new verification code has been sent to your email."}, status=status.HTTP_200_OK)


# NOTE: this view was previously also named `verify_token`, which collided with the
# password-reset `verify_token` POST view defined later in this module. Because
# urls.py does `from .views import *`, the later definition won the name, so the
# `verify/<uidb64>/<token>/` route actually bound to the POST view, which has signature
# (request) and cannot accept the uidb64/token URL kwargs -> TypeError 500 on every
# request to that route. Renamed to a unique name so the route binds to THIS view.
@api_view(["GET"])
def verify_email_token(request, uidb64, token):
    # Decode the uidb64 first, in its own guard. A malformed uidb64 (not valid
    # base64, or base64 that does not decode to utf-8) raises ValueError /
    # DjangoUnicodeDecodeError from urlsafe_base64_decode/force_str. A value that
    # decodes to a non-numeric string then raises ValueError/TypeError from
    # User.objects.get(pk=...) because the pk (user_id) is an integer AutoField.
    # None of those were caught by the old `except User.DoesNotExist` only, so a
    # malformed token like /verify/MQ/sampletoken/ 500'd. Treat all of these as a
    # bad request (400) rather than an unhandled 500.
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
    except (ValueError, TypeError, OverflowError):
        # prevents ValueError / DjangoUnicodeDecodeError on undecodable uidb64
        return Response({"error": "Invalid or malformed token."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(pk=uid)
    except (User.DoesNotExist, ValueError, TypeError):
        # prevents User.DoesNotExist (no such user) and ValueError/TypeError
        # (uid decoded to a non-integer that the integer pk lookup rejects)
        return Response({"error": "Invalid user."}, status=status.HTTP_400_BAD_REQUEST)

    # Validate token. check_token already returns False (never raises) for a
    # malformed token in this Django version, so no extra guard is needed here.
    if default_token_generator.check_token(user, token):
        user.is_active = True  # Activate user after verification
        user.save()
        return Response({"message": "Email verified successfully!"}, status=status.HTTP_200_OK)
    else:
        return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)
    

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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)           

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

    set_audit(request, f"Banned the team {team.team_name} ({reason})")
    AdminHistory.objects.create(
        admin_user=user,
        action="banned_team",
        description=f"Team {team.team_name} (ID: {team.team_id}) banned until {ban_end_date} for reason: {reason}"
    )

    team_owner = team.team_owner
    team_members = TeamMembers.objects.filter(team=team).select_related('member')

    # Notify team owner and members
    notification_message = f"Your team '{team.team_name}' has been banned until {ban_end_date.strftime('%Y-%m-%d %H:%M:%S')} for the following reason: {reason}."
    Notifications.objects.create(
        user=team_owner,
        message=notification_message,
        notification_type="team_ban"
    )
    for member in team_members:
        Notifications.objects.create(
            user=member.member,
            message=notification_message,
            notification_type="team_ban"
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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if user.role not in ["admin", "moderator"]:
            return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)           
    
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
    
    set_audit(request, f"Unbanned the team {team.team_name}")
    AdminHistory.objects.create(
        admin_user=user,
        action="unbanned_team",
        description=f"Team {team.team_name} (ID: {team.team_id}) unbanned"
    )

    # Notify team owner and members
    team_owner = team.team_owner
    team_members = TeamMembers.objects.filter(team=team).select_related('member')
    notification_message = f"Your team '{team.team_name}' has been unbanned."
    Notifications.objects.create(
        user=team_owner,
        message=notification_message,
        notification_type="team_unban"
    )

    for member in team_members:
        Notifications.objects.create(
            user=member.member,
            message=notification_message,
            notification_type="team_unban"
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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

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
        player = User.objects.get(username=player_ign)
    except User.DoesNotExist:
        return Response({"message": "Player not found."}, status=status.HTTP_404_NOT_FOUND)

    # Create a ban entry
    ban_entry = BannedPlayer.objects.create(
        banned_player=player,
        ban_duration=int(duration),
        reason=reason
    )

    set_audit(request, f"Banned the player {player_ign} for {duration} days ({reason})")
    AdminHistory.objects.create(
        admin_user=user,
        action="banned_player",
        description=f"Player {player_ign} (ID: {player.user_id}) banned for {duration} days for reason: {reason}"
    )

    # Notify the player
    notification_message = f"You have been banned for {duration} days for the following reason: {reason}."
    Notifications.objects.create(
        user=player,
        message=notification_message,
        notification_type="player_ban"
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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

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

    set_audit(request, f"Unbanned the player {player_ign}")
    AdminHistory.objects.create(
        admin_user=user,
        action="unbanned_player",
        description=f"Player {player_ign} (ID: {player.user_id}) unbanned"
    )

    # Notify the player
    notification_message = "Your ban has been lifted. You can now access your account."
    Notifications.objects.create(
        user=player,
        message=notification_message,
        notification_type="player_unban"
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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

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

    set_audit(request, f"Posted the news '{news_title}'")
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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

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

    set_audit(request, f"Edited the news '{news_title}'")
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
    # PERFORMANCE (2026-06-08): the news page was slow for two compounding reasons.
    #  1. Backend N+1: this loop lazy-loaded news.author + news.related_event per row
    #     (~2N+1 queries) — the same anti-pattern get_all_teams already fixed. Now
    #     select_related both FKs (one join).
    #  2. Frontend 1+N waterfall: app/(user)/news/page.tsx fired one POST
    #     /auth/get-news-likes-dislikes-count/ PER article and blocked the whole page until
    #     the slowest resolved. We now fold like/dislike COUNTS and the caller's
    #     liked/disliked state into THIS single response (grouped aggregates, a few queries
    #     total) so the FE renders from one request and the per-item loop is deleted.
    # Consumed by frontend/app/(user)/news/page.tsx and _components/LatestNews.tsx.
    from django.db.models import Count

    news_list = News.objects.select_related("author", "related_event").order_by("-created_at")

    # Like/dislike counts in ONE grouped query each → {news_id: count}.
    like_counts = dict(
        NewsLike.objects.values_list("news").annotate(c=Count("pk")).values_list("news", "c")
    )
    dislike_counts = dict(
        NewsDislike.objects.values_list("news").annotate(c=Count("pk")).values_list("news", "c")
    )

    # The caller's own liked/disliked sets (optional auth: Bearer header or ?token=). Two
    # set queries instead of two-per-article. Anonymous viewers just get empty sets.
    liked_ids, disliked_ids = set(), set()
    auth = request.headers.get("Authorization", "")
    token = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else request.GET.get("token")
    if token:
        viewer = validate_token(token)
        if viewer:
            liked_ids = set(NewsLike.objects.filter(user=viewer).values_list("news_id", flat=True))
            disliked_ids = set(NewsDislike.objects.filter(user=viewer).values_list("news_id", flat=True))

    news_data = []
    for news in news_list:
        news_data.append({
            "news_id": news.news_id,
            "news_title": news.news_title,
            "content": news.content,
            "category": news.category,
            "related_event": news.related_event.event_name if news.related_event else None,
            "images_url": request.build_absolute_uri(news.images.url) if news.images else None,
            "author": news.author.username if news.author else None,
            "created_at": news.created_at,
            "slug": news.slug,
            # Folded-in reaction data — removes the per-article frontend request.
            "likes": like_counts.get(news.news_id, 0),
            "dislikes": dislike_counts.get(news.news_id, 0),
            "is_liked_by_user": news.news_id in liked_ids,
            "is_disliked_by_user": news.news_id in disliked_ids,
        })

    return Response({"news": news_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def get_news_detail(request):
    # news_id = request.data.get("news_id")
    slug = request.data.get("slug")
    if not slug:
        return Response({"message": "Slug is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        news = News.objects.get(slug=slug)
    except News.DoesNotExist:
        return Response({"message": "News not found."}, status=status.HTTP_404_NOT_FOUND)

    news_views = NewsViews.objects.filter(news=news).count()

    news_data = {
        "news_id": news.news_id,
        "news_title": news.news_title,
        "content": news.content,
        "category": news.category,
        "related_event": news.related_event.event_name if news.related_event else None,
        "images_url": request.build_absolute_uri(news.images.url) if news.images else None,
        "author": news.author.username,
        "created_at": news.created_at,
        "updated_at": news.updated_at,
        "total_views": news_views
    }

    # add to news views
    NewsViews.objects.create(
        news=news,
        viewer_ip=get_client_ip(request),
        viewer_user_agent=request.META.get("HTTP_USER_AGENT", "")
    )

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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    

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

    set_audit(request, f"Deleted the news '{news.news_title}'")
    news.delete()

    AdminHistory.objects.create(
        admin_user=user,
        action="deleted_news",
        description=f"News '{news.news_title}' (ID: {news.news_id}) deleted"
    )

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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    # Extract new profile details
    full_name = request.data.get("full_name")
    in_game_name = request.data.get("in_game_name")
    email = request.data.get("email")
    uid = request.data.get("uid")
    profile_pic = request.FILES.get("profile_pic")

    # Validate required fields
    if not all([full_name, in_game_name, email]):
        return Response({"message": "All fields are required."}, status=status.HTTP_400_BAD_REQUEST)

    # Check for uniqueness conflicts
    if User.objects.exclude(pk=user.pk).filter(uid=uid).exists():
        return Response({"message": "UID is already in use by another user."}, status=status.HTTP_400_BAD_REQUEST)

    if User.objects.exclude(pk=user.pk).filter(email=email).exists():
        return Response({"message": "Email is already registered to another user."}, status=status.HTTP_400_BAD_REQUEST)
    
    is_valid, message = is_valid_email(email)

    if not is_valid:
        return Response({"error": message}, status=400)

    if User.objects.exclude(pk=user.pk).filter(username=in_game_name).exists():
        return Response({"message": "In-game name is already taken."}, status=status.HTTP_400_BAD_REQUEST)

    # Update User fields
    user.full_name = full_name
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



# @api_view(["GET"])
# def get_user_profile(request):
#     # Retrieve session token
#     session_token = request.headers.get("Authorization")

#     if not session_token:
#         return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

#     if not session_token.startswith("Bearer "):
#         return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

#     session_token = session_token.split(" ")[1]

#     # Identify the logged-in user using the session token
#     user = validate_token(session_token)
#     if not user:
#         return Response(
#             {"message": "Invalid or expired session token."},
#             status=status.HTTP_401_UNAUTHORIZED
#         )

#     # Try to get UserProfile
#     try:
#         profile = UserProfile.objects.get(user=user)
#         profile_pic_url = request.build_absolute_uri(profile.profile_pic.url) if profile.profile_pic else None
#     except UserProfile.DoesNotExist:
#         profile_pic_url = None


#     # Get Total Kills
#     total_kills = 0  

#     # Get All Wins
#     total_wins = 0

#     # Get All Mvps
#     total_mvps = 0

#     # Get All Booyahs(All Wins{1st place} Player had in matches either solo, duo or squad)
#     total_booyahs = 0

#     # Get Total Tournaments Played
#     total_tournaments_played = 0

#     # Get Total Scrims played
#     total_scrims_played = 0

#     # Return user info
#     return Response({
#         "user_id": user.user_id,
#         "full_name": user.full_name,
#         "country": user.country,
#         "in_game_name": user.username,
#         "email": user.email,
#         "uid": user.uid,
#         "team": user.team.team_name if hasattr(user, 'team') else None,
#         "role": user.role,
#         "profile_pic": profile_pic_url,
#         "roles": list(UserRoles.objects.filter(user=user).values_list('role__role_name', flat=True)),
#         "is_banned": BannedPlayer.objects.filter(banned_player=user, is_active=True).exists()
#     }, status=status.HTTP_200_OK)


from django.db.models import Sum, Count, Q, IntegerField
from django.db.models.functions import Coalesce
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

@api_view(["GET"])
def get_user_profile(request):
    # ---------------- AUTH ----------------
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'},
                        status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'},
                        status=status.HTTP_400_BAD_REQUEST)

    token = session_token.split(" ")[1]
    user = validate_token(token)
    if not user:
        return Response({"message": "Invalid or expired session token."},
                        status=status.HTTP_401_UNAUTHORIZED)

    # ---------------- PROFILE PIC + ESPORT IMAGE ----------------
    profile_pic_url = None
    esport_image_url = None
    try:
        profile = UserProfile.objects.get(user=user)
        profile_pic_url = request.build_absolute_uri(profile.profile_pic.url) if profile.profile_pic else None
        # The SEPARATE esport image (UserProfile.esports_pic): organizers use it for event
        # graphics; the profile-edit UI shows it + the replace flow (upload_esport_image).
        esport_image_url = request.build_absolute_uri(profile.esports_pic.url) if profile.esports_pic else None
    except UserProfile.DoesNotExist:
        pass

    # ---------------- SOLO STATS ----------------
    solo_agg = (SoloPlayerMatchStats.objects
        .filter(competitor__user=user)
        .aggregate(
            total_kills=Coalesce(Sum("kills"), 0, output_field=IntegerField()),
            total_wins=Coalesce(Count("id", filter=Q(placement=1)), 0, output_field=IntegerField()),
            total_points=Coalesce(Sum("total_points"), 0, output_field=IntegerField()),
            matches_played=Coalesce(Count("id"), 0, output_field=IntegerField()),
        )
    )

    # ---------------- TEAM STATS ----------------
    # Player stats (kills/damage/assists) exist in TournamentPlayerMatchStats
    team_player_agg = (TournamentPlayerMatchStats.objects
        .filter(player=user)
        .aggregate(
            team_kills=Coalesce(Sum("kills"), 0, output_field=IntegerField()),
            team_damage=Coalesce(Sum("damage"), 0, output_field=IntegerField()),
            team_assists=Coalesce(Sum("assists"), 0, output_field=IntegerField()),
            team_matches_played=Coalesce(Count("player_stats_id"), 0, output_field=IntegerField()),
        )
    )

    # Team wins/booyahs: based on TournamentTeamMatchStats placement=1,
    # but only for teams the user belongs to.
    team_ids = list(TournamentTeamMember.objects.filter(user=user).values_list("tournament_team_id", flat=True))

    team_wins = 0
    if team_ids:
        team_wins = (TournamentTeamMatchStats.objects
            .filter(tournament_team_id__in=team_ids, placement=1)
            .count()
        )

    # ---------------- MVPs ----------------
    # Match.mvp is a FK to User
    total_mvps = Match.objects.filter(mvp=user).count()

    # ---------------- BOOYAHs ----------------
    # booyah = first placement across SOLO + TEAM
    total_booyahs = int(solo_agg["total_wins"]) + int(team_wins)

    # ---------------- TOURNAMENTS / SCRIMS PLAYED ----------------
    # SOLO: events where user registered
    solo_event_ids = list(
        RegisteredCompetitors.objects.filter(user=user).values_list("event_id", flat=True).distinct()
    )

    # TEAM: events where user is in a TournamentTeam for that event
    team_event_ids = []
    if team_ids:
        team_event_ids = list(
            TournamentTeam.objects.filter(tournament_team_id__in=team_ids)
            .values_list("event_id", flat=True)
            .distinct()
        )

    all_event_ids = set(solo_event_ids) | set(team_event_ids)

    total_tournaments_played = Event.objects.filter(event_id__in=all_event_ids, competition_type="tournament").count()
    total_scrims_played = Event.objects.filter(event_id__in=all_event_ids, competition_type="scrims").count()

    # ---------------- TOTAL EARNINGS ----------------
    # I don’t see a User.total_earnings field in what you sent.
    # So we do a safe fallback:
    # - If user has a team and that Team has total_earnings, use it
    # - Else 0
    total_earnings = 0
    try:
        if getattr(user, "team", None) and hasattr(user.team, "total_earnings"):
            total_earnings = user.team.total_earnings or 0
    except Exception:
        total_earnings = 0

    # ---------------- TOTAL KILLS ----------------
    # Total kills = solo kills + team kills
    total_kills = int(solo_agg["total_kills"]) + int(team_player_agg["team_kills"])

    # Total wins (you can keep separate too)
    total_wins = int(solo_agg["total_wins"]) + int(team_wins)

    # is_vendor: True if this user is an ACTIVE marketplace vendor. The FE uses it to show
    # the "Vendor Dashboard" sidebar entry (the /vendor portal is otherwise only reachable by
    # typing the URL, since "vendor" is a DB record, not a role). Local import avoids an
    # afc_shop <-> afc_auth circular import at module load (afc_shop already imports from afc_auth).
    from afc_shop.models import Vendor as _Vendor
    is_vendor = _Vendor.objects.filter(user=user, status="active").exists()

    return Response({
        "user_id": user.user_id,
        "full_name": user.full_name,
        "country": user.country,
        "in_game_name": user.username,
        "email": user.email,
        "uid": user.uid,
        "team": user.team.team_name if getattr(user, "team", None) else None,
        "role": user.role,
        "profile_pic": profile_pic_url,
        # The separate esport image (see upload_esport_image): null until the player uploads one.
        "esport_image_url": esport_image_url,
        "roles": list(UserRoles.objects.filter(user=user).values_list("role__role_name", flat=True)),
        "is_banned": BannedPlayer.objects.filter(banned_player=user, is_active=True).exists(),
        "is_vendor": is_vendor,
        # First-time WELCOME tour flag. The frontend AuthContext maps this onto User.has_seen_welcome,
        # and WelcomeTour.tsx auto-shows the animated newcomer tour only while this is False. Flipped
        # True by POST /auth/mark-welcome-seen/ (mark_welcome_seen) when the user finishes/skips it.
        "has_seen_welcome": user.has_seen_welcome,
        # One-time dashboard intro callouts: {"sponsor": true, ...} once each is dismissed. The
        # frontend DashboardIntroCoachmark shows a "here is where your new dashboard lives" callout
        # for any accessible dashboard whose key is missing, then flips it via
        # POST /auth/mark-dashboard-intro-seen/.
        "seen_dashboard_intros": user.seen_dashboard_intros or {},
        "discord_id": user.discord_id if hasattr(user, "discord_id") else None,
        "discord_username": user.discord_username if hasattr(user, "discord_username") else None,

        "stats": {
            "total_kills": total_kills,
            "total_wins": total_wins,
            "total_mvps": total_mvps,
            "total_booyahs": total_booyahs,
            "total_tournaments_played": total_tournaments_played,
            "total_scrims_played": total_scrims_played,
            "total_earnings": total_earnings,

            # optional breakdowns (nice for frontend)
            "solo": {
                "kills": int(solo_agg["total_kills"]),
                "wins": int(solo_agg["total_wins"]),
                "matches_played": int(solo_agg["matches_played"]),
                "total_points": int(solo_agg["total_points"]),
            },
            "team": {
                "kills": int(team_player_agg["team_kills"]),
                "assists": int(team_player_agg["team_assists"]),
                "damage": int(team_player_agg["team_damage"]),
                "wins": int(team_wins),
                "matches_played": int(team_player_agg["team_matches_played"]),
            }
        }
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def mark_welcome_seen(request):
    """
    Mark the current user's first-time WELCOME tour as seen.

    PURPOSE
        Persists that the logged-in user has finished/skipped/closed the animated newcomer
        welcome tour, so it never auto-opens again on future sessions/devices.

    AUTH
        Bearer SessionToken in the Authorization header (same validate_token pattern as
        get_user_profile and the other afc_auth endpoints). No body required.

    REQUEST  : POST /auth/mark-welcome-seen/   (empty body)
    RESPONSE : 200 {"status": "ok", "has_seen_welcome": true}
               400 if the Authorization header is missing/malformed
               401 if the session token is invalid/expired

    FRONTEND CONSUMER
        app/(user)/_components/WelcomeTour.tsx calls this (best-effort) when the tour is
        finished, skipped, or dismissed by a logged-in user. The complementary read is
        get_user_profile, which returns has_seen_welcome so the client knows whether to show it.
        Idempotent: calling it again on an already-seen user is a harmless no-op 200.
    """
    # ---------------- AUTH (mirrors get_user_profile) ----------------
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'},
                        status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'},
                        status=status.HTTP_400_BAD_REQUEST)

    token = session_token.split(" ")[1]
    user = validate_token(token)
    if not user:
        return Response({"message": "Invalid or expired session token."},
                        status=status.HTTP_401_UNAUTHORIZED)

    # ---------------- FLIP THE FLAG (idempotent) ----------------
    # Only write when it actually changes, so a repeat call costs no DB write.
    if not user.has_seen_welcome:
        user.has_seen_welcome = True
        user.save(update_fields=["has_seen_welcome"])

    return Response({"status": "ok", "has_seen_welcome": True}, status=status.HTTP_200_OK)


# The dashboards a one-time intro callout exists for; mirrors the frontend DASHBOARDS list in
# app/(user)/_components/DashboardIntroCoachmark.tsx.
_DASHBOARD_INTRO_KEYS = {"admin", "sponsor", "organizer", "vendor"}


@api_view(["POST"])
def mark_dashboard_intro_seen(request):
    """
    Mark ONE dashboard's one-time intro callout as seen for the current user.

    PURPOSE
        When a user is granted access to a role dashboard (admin / sponsor / organizer / vendor),
        their next login shows a one-time callout pointing at the nav menu where that dashboard
        lives (owner 2026-06-12: not a navigate-now popup, and only the first time after access).
        Dismissing it calls this endpoint so it never shows again for that dashboard, on any device.

    AUTH
        Bearer SessionToken (same validate_token pattern as mark_welcome_seen).

    REQUEST  : POST /auth/mark-dashboard-intro-seen/   {"dashboard": "admin"|"sponsor"|"organizer"|"vendor"}
    RESPONSE : 200 {"status": "ok", "seen_dashboard_intros": {...}}
               400 missing/unknown dashboard key, or missing/malformed Authorization header
               401 invalid/expired session token

    FRONTEND CONSUMER
        app/(user)/_components/DashboardIntroCoachmark.tsx (mounted in the user Header) calls this
        when the callout is dismissed. The complementary read is get_user_profile's
        seen_dashboard_intros. Idempotent: re-marking an already-seen dashboard is a no-op 200.
    """
    # ---------------- AUTH (mirrors mark_welcome_seen) ----------------
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'},
                        status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'},
                        status=status.HTTP_400_BAD_REQUEST)

    token = session_token.split(" ")[1]
    user = validate_token(token)
    if not user:
        return Response({"message": "Invalid or expired session token."},
                        status=status.HTTP_401_UNAUTHORIZED)

    dashboard = (request.data.get("dashboard") or "").strip().lower()
    if dashboard not in _DASHBOARD_INTRO_KEYS:
        return Response(
            {"message": f"dashboard must be one of: {', '.join(sorted(_DASHBOARD_INTRO_KEYS))}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ---------------- FLIP THE KEY (idempotent) ----------------
    seen = dict(user.seen_dashboard_intros or {})
    if not seen.get(dashboard):
        seen[dashboard] = True
        user.seen_dashboard_intros = seen
        user.save(update_fields=["seen_dashboard_intros"])

    return Response({"status": "ok", "seen_dashboard_intros": seen}, status=status.HTTP_200_OK)


@api_view(["POST"])
def upload_esport_image(request):
    """
    Upload (or REPLACE) the current user's ESPORT IMAGE - UserProfile.esports_pic.

    PURPOSE
        The esport image is a SEPARATE asset from the profile picture (owner 2026-06-12):
        organizers use it as the player's image in event graphics, and event creators can
        REQUIRE it before a player may register for an event. The field has existed on
        UserProfile since 0001 but had no upload path; this is it.

    RULES (owner)
        - Replace-only: an esport image can NEVER be removed, only replaced - this endpoint
          requires a file and no delete path exists.
        - The player must upload THEIR OWN picture, looking like an esport image (bust shot,
          no branded shirts). The frontend shows the ban warning; uploading someone else's
          picture or a non-esport picture can get the player AND their team banned.

    AUTH     : Bearer SessionToken (same validate_token pattern as edit_profile).
    REQUEST  : POST /auth/upload-esport-image/  multipart, field `esport_image` (required).
    RESPONSE : 200 {"status": "ok", "esport_image_url": <absolute url>}
               400 missing file / missing or malformed Authorization header
               401 invalid/expired session token

    FRONTEND CONSUMER
        app/(user)/profile/edit/page.tsx ("Esport Image" section: preview + replace button +
        the ban warning). The complementary read is get_user_profile's esport_image_url.
    """
    # ---------------- AUTH (mirrors edit_profile) ----------------
    session_token = request.headers.get("Authorization")
    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'},
                        status=status.HTTP_400_BAD_REQUEST)
    user = validate_token(session_token.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."},
                        status=status.HTTP_401_UNAUTHORIZED)

    esport_image = request.FILES.get("esport_image")
    if not esport_image:
        return Response({"message": "esport_image file is required."},
                        status=status.HTTP_400_BAD_REQUEST)

    profile, _created = UserProfile.objects.get_or_create(user=user)
    profile.esports_pic = esport_image  # replace-only: the old file reference is overwritten
    profile.save()

    return Response({
        "status": "ok",
        "esport_image_url": request.build_absolute_uri(profile.esports_pic.url),
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
def send_verification_token(request):
    email = request.data.get("email")
    uid = request.data.get("uid")

    if not email and not uid:
        return Response({"message": "Email or UID is required."}, status=status.HTTP_400_BAD_REQUEST)
    
    
    
    else:
        if email:
            is_valid, message = is_valid_email(email)

            if not is_valid:
                return Response({"error": message}, status=400)
            pass
        elif not uid:
            return Response({"message": "UID is required."}, status=status.HTTP_400_BAD_REQUEST)
    

    try:
        if email:
            user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({"message": "User with this email does not exist."}, status=status.HTTP_404_NOT_FOUND)
    
    try:
        if uid:
            user = User.objects.get(uid=uid)
    except User.DoesNotExist:
        return Response({"message": "User with this UID does not exist."}, status=status.HTTP_404_NOT_FOUND)

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
    email = user.email

    # Send email
    subject = "Reset your AFC password"
    message = email_reset_token(token)
    send_email(email, subject, message)

    return Response({"message": "Password reset token has been sent to your email."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def verify_token(request):
    email = request.data.get("email")
    uid = request.data.get("uid")
    token = request.data.get("token")

    if (not email and not uid) and not token:
        return Response({"message": "Email or UID and token are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        if email:
            user = User.objects.get(email=email)
        elif uid:
            user = User.objects.get(uid=uid)
        reset_token = PasswordResetToken.objects.get(user=user, token=token)
    except (User.DoesNotExist, PasswordResetToken.DoesNotExist):
        return Response({"message": "Invalid email or token."}, status=status.HTTP_400_BAD_REQUEST)

    if not reset_token.is_valid():
        return Response({"message": "Token has expired."}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"message": "Token is valid."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def reset_password(request):
    email = request.data.get("email")
    uid = request.data.get("uid")
    token = request.data.get("token")
    new_password = request.data.get("new_password")

    if not email and not uid:
        return Response({"message": "Email or UID is required."}, status=status.HTTP_400_BAD_REQUEST)


    if not all([token, new_password]):
        return Response({"message": "Token, and new password are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        if email:
            user = User.objects.get(email=email)
        elif uid:
            user = User.objects.get(uid=uid)
        reset_token = PasswordResetToken.objects.get(user=user, token=token)
    except (User.DoesNotExist, PasswordResetToken.DoesNotExist):
        return Response({"message": "Invalid email or token."}, status=status.HTTP_400_BAD_REQUEST)

    if not reset_token.is_valid():
        return Response({"message": "Token has expired."}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    user.save()

    reset_token.delete()  # remove token after successful password reset

    # Password-changed confirmation (best-effort; never block the reset on a mail error).
    try:
        when = timezone.now().strftime("%d %b %Y, %H:%M UTC")
        send_email(user.email, "Your AFC password was changed", email_password_changed(user.username, when))
    except Exception as e:
        print(f"Password-changed email failed for {user.email}: {e}")

    return Response({"message": "Password has been reset successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def resend_token(request):
    email = request.data.get("email")

    if not email:
        return Response({"message": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)
    
    is_valid, message = is_valid_email(email)

    if not is_valid:
        return Response({"error": message}, status=400)

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
    subject = "Your new AFC password reset token"
    message = email_reset_token(token)
    send_email(email, subject, message)

    return Response({"message": "A new password reset token has been sent to your email."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def change_password(request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)

    user = validate_token(auth.split(" ")[1])

    old_password = request.data.get("old_password")
    new_password = request.data.get("new_password")

    if user.check_password(old_password):
        user.set_password(new_password)
        user.save()  # BUGFIX: save() was missing, so the new password never persisted.

        # Password-changed confirmation (best-effort; never block the change on a mail error).
        try:
            when = timezone.now().strftime("%d %b %Y, %H:%M UTC")
            send_email(user.email, "Your AFC password was changed", email_password_changed(user.username, when))
        except Exception as e:
            print(f"Password-changed email failed for {user.email}: {e}")

        return Response({"message": "Password Changed Successfully."}, status=status.HTTP_200_OK)

    else:
        return Response({"message": "You have Inputted the wrong password."}, status=status.HTTP_400_BAD_REQUEST)

@api_view(["POST"])
def contact_us(request):
    name = request.data.get("name")
    email = request.data.get("email")
    message = request.data.get("message")

    if not all([email, name, message]):
        return Response({"message": "Email, name, and message are required."}, status=status.HTTP_400_BAD_REQUEST)
    
    is_valid, message = is_valid_email(email)

    if not is_valid:
        return Response({"error": message}, status=400)

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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )


    # The User model has no `is_admin` field/property (only `role`), so the old
    # `user.is_admin` raised AttributeError -> 500 on every call. Use the same
    # role check the rest of this module uses (user.role == "admin").
    if user.role != "admin":
        return Response({"message": "User is not an admin."}, status=status.HTTP_403_FORBIDDEN)

    # Return admin information.
    # Fixes the AttributeError chain that previously 500'd this endpoint:
    #   - user.id            -> the pk is `user_id` (use that explicitly)
    #   - user.roles.all()   -> there is no `roles` related manager; the related_name
    #                           on UserRoles is `userroles`, each row's role is a
    #                           Roles FK whose label field is `role_name` (not `name`)
    admin_roles = list(
        UserRoles.objects.filter(user=user).values_list("role__role_name", flat=True)
    )
    admin_info = {
        "id": user.user_id,
        "email": user.email,
        "name": user.username,
        "is_active": user.is_active,
        "status": user.status,
        "admin_roles": admin_roles,
    }

    return Response({"message": "Admin information retrieved successfully.", "data": admin_info}, status=status.HTTP_200_OK)



@api_view(["GET"])
def get_all_roles(request):
    roles = Roles.objects.all()
    roles_data = [{"role_id": role.role_id, "role_name": role.role_name, "description": role.description} for role in roles]
    return Response({"roles": roles_data}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_user_and_user_roles(request):
    # Admin Settings -> users + their granular roles (frontend app/(a)/a/settings/page.tsx).
    # PERFORMANCE: previously ran a UserRoles query PLUS a lazy ur.role load PER user inside the
    # loop -> ~10k+ queries for 6,316 users (~6s). Now: one grouped UserRoles fetch with the role
    # joined (select_related), assembled into a {user_id: [role_name,...]} dict. Same response
    # shape, 2 queries total.
    roles_by_user = {}
    for ur in UserRoles.objects.select_related("role").all():
        roles_by_user.setdefault(ur.user_id, []).append(ur.role.role_name)

    users = User.objects.all()
    users_data = []

    for user in users:
        users_data.append({
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "status": user.status,
            "last_login": user.last_login,
            "roles": roles_by_user.get(user.user_id, []),
            "created_at": user.created_at
        })

    return Response({"users": users_data}, status=status.HTTP_200_OK)


@api_view(["GET"])
def search_users(request):
    """
    GET /auth/search-users/?q=<text>&limit=10 - typeahead lookup of EXISTING users.

    Powers the reusable <UserSearchSelect/> typeahead (frontend components/ui/user-search-select.tsx)
    used wherever a human picks an existing user instead of typing a raw username/email - e.g. admin
    bulk notifications (settings) and team member invites. Returns a short, ranked match list.

    Auth: any logged-in user (Bearer SessionToken).
    PRIVACY: email is matched + returned ONLY for admin callers. Ordinary users match by username
    (which IS the in-game name here), full_name and uid, and never see other users' emails - this
    stops email enumeration through the typeahead. Requires q >= 2 chars so it can't dump the table.

    Response: { results: [ {user_id, username, full_name, role, email?} ], total_count }
    (the `email` key is present only when the caller is an admin/staff member.)
    """
    from django.db.models import Q

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=status.HTTP_400_BAD_REQUEST)
    requester = validate_token(auth.split(" ", 1)[1])
    if not requester:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        # Require at least 2 characters so the endpoint never returns the whole user table.
        return Response({"results": [], "total_count": 0}, status=status.HTTP_200_OK)

    try:
        limit = min(max(int(request.GET.get("limit", 10)), 1), 25)
    except (TypeError, ValueError):
        limit = 10

    # Admin = base role "admin" OR any granular UserRoles row (mirrors the audit-log predicate).
    is_admin = requester.role == "admin" or requester.userroles.exists()

    # Everyone matches by username (= IGN), full_name and uid. Admins additionally match by email.
    cond = Q(username__icontains=q) | Q(full_name__icontains=q) | Q(uid__icontains=q)
    if is_admin:
        cond |= Q(email__icontains=q)

    # Punctuation-insensitive widening so "ve" finds the user "v-e" and "j.doe" finds "jdoe": strip the
    # common separators (-, _, ., space, ...) from both the columns (normalized_column) and the query
    # (separator_stripped) and OR the result onto the existing icontains conditions. Mirrors
    # afc_team.search_teams and frontend lib/search.ts; this only ever ADDS matches.
    from utils.search_utils import normalized_column, separator_stripped

    norm_q = separator_stripped(q)
    qs = User.objects.annotate(
        _norm_username=normalized_column("username"),
        _norm_full_name=normalized_column("full_name"),
        _norm_uid=normalized_column("uid"),
    )
    if norm_q:
        cond |= (
            Q(_norm_username__icontains=norm_q)
            | Q(_norm_full_name__icontains=norm_q)
            | Q(_norm_uid__icontains=norm_q)
        )
        if is_admin:
            # Email is matched (and returned) for admin callers only, same as the icontains clause above.
            qs = qs.annotate(_norm_email=normalized_column("email"))
            cond |= Q(_norm_email__icontains=norm_q)
    qs = qs.filter(cond).order_by("username")
    total = qs.count()

    results = []
    for u in qs[:limit]:
        item = {
            "user_id": u.user_id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
        }
        if is_admin:
            item["email"] = u.email  # email exposed to admin callers only (privacy)
        results.append(item)

    return Response({"results": results, "total_count": total}, status=status.HTTP_200_OK)


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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if user.role != "admin":
        return Response({"message": "You do not have permission to suspend a user."}, status=status.HTTP_403_FORBIDDEN)

    user_id = request.data.get("user_id")
    if not user_id:
        return Response({"message": "User ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(user_id=user_id)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    if user.status == "suspended":
        return Response({"message": "User is Currently Suspended"}, status=status.HTTP_400_BAD_REQUEST)

    user.status = "suspended"
    user.save()

    set_audit(request, f"Suspended the user {user.username}")
    AdminHistory.objects.create(
        admin_user=user,
        action="suspended_user",
        description=f"Active User {user.username} (ID: {user.user_id}) suspended"
    )

    # Notify the user
    notification_message = "Your account has been suspended. Please contact support for more information."
    Notifications.objects.create(
        user=user,
        message=notification_message,
        notification_type="account_suspension"
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
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if user.role != "admin":
        return Response({"message": "You do not have permission to activate a user."}, status=status.HTTP_403_FORBIDDEN)

    user_id = request.data.get("user_id")
    if not user_id:
        return Response({"message": "User ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(user_id=user_id)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    
    if user.status == "active":
        return Response({"message": "User is currently active."}, status=status.HTTP_400_BAD_REQUEST)

    user.status = "active"
    user.save()

    set_audit(request, f"Activated the user {user.username}")
    AdminHistory.objects.create(
        admin_user=user,
        action="activated_user",
        description=f"Suspended User {user.username} (ID: {user.user_id}) has been activated"
    )

    # Notify the user
    notification_message = "Your account has been activated. You can now access your account."
    Notifications.objects.create(
        user=user,
        message=notification_message,
        notification_type="account_activation"
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
    admin_user = validate_token(session_token)
    if not admin_user:
        return Response(
            {"message": "Invalid or expired session token."},
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

    # super_admin protection: only a super_admin may grant the super_admin role or modify a user who
    # already holds it. A head_admin cannot touch a super_admin (this runs BEFORE the reset below so
    # we never strip a super_admin's roles and then fail).
    new_role_names = {role.role_name for role in roles}
    if not _is_super_admin(admin_user) and (
        "super_admin" in new_role_names or "super_admin" in _user_role_names(user)
    ):
        return Response(
            {"status": "error", "message": "Only a super admin can manage the super admin role."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # 🔑 Reset: remove all existing roles first
    UserRoles.objects.filter(user=user).delete()

    # Assign new roles
    for role in roles:
        UserRoles.objects.create(user=user, role=role)



    set_audit(request, f"Assigned roles ({', '.join([role.role_name for role in roles])}) to {user.username}")
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="assigned_roles",
        description=f"Assigned roles {', '.join([role.role_name for role in roles])} to user {user.username} (ID: {user.user_id})"
    )

    #Notify the user
    notification_message = f"You have been assigned new roles: {', '.join([role.role_name for role in roles])}."
    Notifications.objects.create(
        user=user,
        message=notification_message,
        notification_type="role_assignment"
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
    admin_user = validate_token(session_token)
    if not admin_user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if admin_user.role != "admin":
        return Response({"message": "You do not have permission to edit user roles."}, status=status.HTTP_403_FORBIDDEN)

    username = request.data.get("username")
    email = request.data.get("email")
    new_role_ids = request.data.get("new_role_ids", [])

    if not email or not username:
        return Response({"message": "Email and username are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email=email, username=username)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    # super_admin protection: only a super_admin may grant/remove the super_admin role or modify a
    # user who already holds it. Runs before the role reset below so a head_admin can never strip a
    # super_admin. (new_role_ids=[] would remove all roles, so the target-is-super check matters too.)
    new_role_names = set(
        Roles.objects.filter(role_id__in=new_role_ids).values_list("role_name", flat=True)
    )
    if not _is_super_admin(admin_user) and (
        "super_admin" in new_role_names or "super_admin" in _user_role_names(user)
    ):
        return Response(
            {"message": "Only a super admin can manage the super admin role."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Clear existing roles
    UserRoles.objects.filter(user=user).delete()

    # if the new roles are empty, downgrade user to regular user
    if new_role_ids == []:
        user.role = "player"
        user.save()
        set_audit(request, f"Removed all admin roles from {user.username}")
        AdminHistory.objects.create(
            admin_user=admin_user,
            action="edited_user_roles",
            description=f"Removed all roles from user {user.username} (ID: {user.user_id})"
        )

        # Notify the user
        notification_message = "All your admin roles have been removed. You are now a regular user."
        Notifications.objects.create(
            user=user,
            message=notification_message,
            notification_type="role_update"
        )

        return Response({"message": f"User {user.username}'s roles updated successfully."}, status=status.HTTP_200_OK)

    # Assign new roles
    for role_id in new_role_ids:
        try:
            role = Roles.objects.get(role_id=role_id)
            UserRoles.objects.create(user=user, role=role)
        except Roles.DoesNotExist:
            return Response({"message": f"Role with ID {role_id} not found."}, status=status.HTTP_404_NOT_FOUND)

    user.role = "admin"
    user.save()
    
    set_audit(request, f"Updated {user.username}'s roles to: {', '.join(new_role_names) or 'none'}")
    AdminHistory.objects.create(
        admin_user=admin_user,
        action="edited_user_roles",
        description=f"Edited roles for user {user.username} (ID: {user.user_id})"
    )

    # Notify the user
    notification_message = f"Your roles have been updated to: {', '.join([Roles.objects.get(role_id=rid).role_name for rid in new_role_ids])}."
    Notifications.objects.create(
        user=user,
        message=notification_message,
        notification_type="role_update"
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
    admin_user = validate_token(session_token)
    if not admin_user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
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
    admin_user = validate_token(session_token)
    if not admin_user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
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


@api_view(["GET"])
def get_audit_log(request):
    """
    GET /auth/get-audit-log/ - paginated, filterable read of the sitewide admin audit log.

    Auth     : admin only. Reuses require_admin() (Bearer SessionToken -> User with role == "admin"),
               the same gate as the other admin read endpoints in this module.
    Source   : afc_auth.AuditLog rows, written AUTOMATICALLY by afc_auth.middleware.AuditLogMiddleware
               on every admin/staff mutation (POST/PUT/PATCH/DELETE) across the whole platform.
    Consumed : frontend app/(a)/a/history/page.tsx (the admin "History" page) via lib authHeaders()
               (Bearer auth_token cookie) + the {results, has_more, next_offset, total_count} envelope.

    Query params (all optional):
      q          case-insensitive search over actor username / request path / action slug
      actor      filter by actor username (icontains)
      action     filter by action slug (icontains)
      method     exact HTTP method (POST/PUT/PATCH/DELETE)
      status     exact response status code
      date_from  ISO date (YYYY-MM-DD), inclusive lower bound on created_at (by calendar date)
      date_to    ISO date (YYYY-MM-DD), inclusive upper bound on created_at (by calendar date)
      limit      page size, default 25, max 100   } house pagination idiom, mirrors afc_partner_api
      offset     row offset, default 0            } -> {results, has_more, next_offset, total_count}
    """
    # Django's Q is imported locally: this module's top-level `Q` is the (unrelated) sympy.Q, so a
    # module-level import here would shadow it and risk side effects elsewhere. Local keeps it surgical.
    from django.db.models import Q

    # Audit log is HEAD-ADMIN ONLY (head_admin or super_admin), not every role=="admin" user.
    admin, err = require_head_admin(request)
    if err:
        return err

    qs = AuditLog.objects.all()  # newest-first via AuditLog.Meta.ordering

    # ── filters (each applied only when its param is present) ───────────────────────────────────
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(actor_username__icontains=q)
            | Q(summary__icontains=q)
            | Q(path__icontains=q)
            | Q(action__icontains=q)
        )

    actor = request.GET.get("actor", "").strip()
    if actor:
        qs = qs.filter(actor_username__icontains=actor)

    action = request.GET.get("action", "").strip()
    if action:
        qs = qs.filter(action__icontains=action)

    method = request.GET.get("method", "").strip().upper()
    if method:
        qs = qs.filter(method=method)

    status_filter = request.GET.get("status", "").strip()
    if status_filter.isdigit():
        qs = qs.filter(status_code=int(status_filter))

    date_from = request.GET.get("date_from", "").strip()
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)

    date_to = request.GET.get("date_to", "").strip()
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    # ── pagination (afc_partner_api idiom: limit/offset -> envelope) ────────────────────────────
    try:
        limit = min(max(int(request.GET.get("limit", 25)), 1), 100)
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = max(int(request.GET.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    total = qs.count()
    rows = qs.select_related("actor")[offset:offset + limit]

    results = [{
        "id": r.id,
        "actor_username": r.actor_username,
        "actor_role": r.actor_role,
        "summary": r.summary or r.action,  # human short form; fall back to slug for old rows
        "action": r.action,
        "method": r.method,
        "path": r.path,
        "view_name": r.view_name,
        "target_type": r.target_type,
        "target_id": r.target_id,
        "status_code": r.status_code,
        "ip_address": r.ip_address,
        "user_agent": r.user_agent,
        "metadata": r.metadata,
        "timestamp": r.created_at,
    } for r in rows]

    nxt = offset + limit
    has_more = nxt < total
    return Response({
        "results": results,
        "has_more": has_more,
        "next_offset": nxt if has_more else None,
        "total_count": total,
    }, status=status.HTTP_200_OK)


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
    admin_user = validate_token(session_token)
    if not admin_user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
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
    invite_token = request.GET.get("invite_token")

    if not session_token or not tournament_id:
        return Response({"message": "session_token and tournament_id required"}, status=400)

    client_id = settings.DISCORD_CLIENT_ID
    redirect_uri = settings.DISCORD_REDIRECT_URI

    scope = "identify guilds.join"

    # Encode the custom redirect URL
    from urllib.parse import quote
    if invite_token:
        return_url = quote(f"{settings.FRONTEND_URL}/tournaments/{tournament_id}?invite_token={invite_token}")
    else:
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


from django.conf import settings
from django.shortcuts import redirect
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from urllib.parse import quote

@api_view(["GET"])
def connect_discord_account(request):
    # Auth (prefer header, not query param)
    # auth = request.headers.get("Authorization")
    # if not auth or not auth.startswith("Bearer "):
    #     return Response({"message": "Invalid or missing Authorization token."}, status=400)

    session_token = request.GET.get("session_token")
    if not session_token:
        return Response({"message": "session_token is required"}, status=400)

    user = validate_token(session_token)
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    client_id = settings.DISCORD_CLIENT_ID
    redirect_uri = settings.DISCORD_REDIRECT_URI  # your backend callback URL

    # Identify user + (optional) join guild automatically if you want
    # - identify: get user discord profile
    # - guilds.join: lets your bot add the user to your server after OAuth
    scope = "identify guilds.join"

    # Where to send user after successful connect (frontend page)
    # example: /profile or /settings
    return_to = request.GET.get("return_to") or f"{settings.FRONTEND_URL}/profile"
    return_to_enc = quote(return_to)

    # state carries user identity + return path
    # Use your session token (or a short-lived oauth nonce) – session token is okay but better as short-lived nonce.
    token = session_token
    state = f"{token}|{return_to_enc}"

    discord_oauth_url = (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        "&response_type=code"
        f"&scope={quote(scope)}"
        f"&state={state}"
        "&prompt=consent"
    )

    return redirect(discord_oauth_url)


@api_view(["POST"])
def disconnect_discord_account(request):

    # ---------------- AUTH ----------------
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    # ---------------- CHECK ----------------
    if not user.discord_connected:
        return Response({
            "message": "No Discord account connected."
        }, status=400)

    with transaction.atomic():

        # ---------------- REMOVE ROLE ASSIGNMENTS ----------------
        # Clean up any queued or assigned roles
        DiscordRoleAssignment.objects.filter(user=user, status="pending").delete()

        # ---------------- CLEAR DISCORD DATA ----------------
        user.discord_id = None
        user.discord_username = None
        user.discord_avatar = None
        user.discord_connected = False

        user.save(update_fields=[
            "discord_id",
            "discord_username",
            "discord_avatar",
            "discord_connected"
        ])

    return Response({
        "message": "Discord account disconnected successfully."
    }, status=200)



@api_view(["GET"])
def is_discord_account_connected(request):
    # Auth (prefer header, not query param)
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=400)

    session_token = auth.split(" ")[1]

    user = validate_token(session_token)
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    is_connected = bool(user.discord_connected)

    # discord_id = user.discord_id
    # if not discord_id:
    #     return Response({"connected": False}, status=status.HTTP_200_OK)

    # # Check if user is in guild
    # in_guild = check_discord_membership(discord_id)
    return Response({"connected": is_connected}, status=status.HTTP_200_OK)




DISCORD_GUILD_ID = settings.DISCORD_GUILD_ID
DISCORD_BOT_TOKEN = settings.DISCORD_BOT_TOKEN

def check_discord_membership(discord_id):
    url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    return r.status_code == 200  # 200 means they are in the server


@api_view(["POST"])
def check_discord_membership_v2(request):
    discord_id = request.data.get("discord_id")
    url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    return Response({"is_member": r.status_code == 200}, status=status.HTTP_200_OK)


import requests

# def check_discord_membership_v3(discord_id):
#     try:
#         url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
#         headers = {
#             "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
#         }

#         response = requests.get(url, headers=headers, timeout=5)

#         # ✅ Success
#         if response.status_code == 200:
#             return True

#         # ❌ Not in server
#         if response.status_code == 404:
#             return False

#         # ⚠️ Debug other cases
#         print("DISCORD CHECK ERROR:", response.status_code, response.text)

#         return False

#     except requests.exceptions.RequestException as e:
#         print("DISCORD REQUEST FAILED:", str(e))
#         return False


import time

def check_discord_membership_v3(discord_id):
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
    }

    discord_id = str(discord_id)

    for attempt in range(2):  # 🔥 retry once

        # -------- PRIMARY --------
        url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
        r = requests.get(url, headers=headers, timeout=5)

        if r.status_code == 200:
            return True

        # -------- SEARCH --------
        search_url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/search?query={discord_id}&limit=1"
        r2 = requests.get(search_url, headers=headers, timeout=5)

        if r2.status_code == 200:
            data = r2.json()
            if any(str(m["user"]["id"]) == discord_id for m in data):
                return True

        # -------- RETRY DELAY --------
        time.sleep(0.3)  # 🔥 tiny delay

    print("DISCORD CHECK FAIL:", discord_id)
    return False

# def check_discord_membership_v3(discord_id):
#     headers = {
#         "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
#     }

#     # -------- PRIMARY CHECK --------
#     url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
#     r = requests.get(url, headers=headers, timeout=5)

#     if r.status_code == 200:
#         return True

#     # -------- FALLBACK: SEARCH --------
#     # this fixes the "user exists but not cached" issue
#     search_url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/search?query={discord_id}&limit=1"
#     r2 = requests.get(search_url, headers=headers, timeout=5)

#     if r2.status_code == 200:
#         data = r2.json()
#         if any(str(member["user"]["id"]) == str(discord_id) for member in data):
#             return True

#     # -------- DEBUG --------
#     print("DISCORD CHECK FAIL:", discord_id, r.status_code, r.text)

#     return False

# 1447745369403297955

# @api_view(["POST"])
# def check_team_members_discord_membership(request):
#     discord_ids = request.data.get("discord_ids", [])
#     results = {}
#     for discord_id in discord_ids:
#         url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
#         headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
#         r = requests.get(url, headers=headers)
#         results[discord_id] = (r.status_code == 200)
#     return Response({"membership": results}, status=status.HTTP_200_OK)


@api_view(["POST"])
def check_team_members_discord_membership(request):
    discord_ids = request.data.get("discord_ids", [])

    if not isinstance(discord_ids, list) or not discord_ids:
        return Response({"message": "discord_ids must be a non-empty list"}, status=400)

    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}"
    }

    results = {}
    checked = {}

    for raw_id in discord_ids:
        discord_id = str(raw_id)

        # -------- CACHE --------
        if discord_id in checked:
            results[discord_id] = checked[discord_id]
            continue

        in_server = False

        try:
            # -------- PRIMARY CHECK --------
            url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
            r = requests.get(url, headers=headers, timeout=5)

            if r.status_code == 200:
                in_server = True

            # -------- FALLBACK SEARCH --------
            elif r.status_code == 404:
                search_url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/search?query={discord_id}&limit=1"
                r2 = requests.get(search_url, headers=headers, timeout=5)

                if r2.status_code == 200:
                    data = r2.json()
                    if any(str(m["user"]["id"]) == discord_id for m in data):
                        in_server = True

            # -------- RATE LIMIT HANDLING --------
            elif r.status_code == 429:
                retry_after = r.json().get("retry_after", 1)
                time.sleep(retry_after)

                # retry once
                r_retry = requests.get(url, headers=headers, timeout=5)
                in_server = (r_retry.status_code == 200)

        except Exception as e:
            print("Discord check error:", discord_id, str(e))
            in_server = False

        checked[discord_id] = in_server
        results[discord_id] = in_server

    return Response({"membership": results}, status=200)


# def assign_discord_role(discord_id, role_id):
#     url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
#     headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
#     r = requests.put(url, headers=headers)
#     return r.status_code == 204  # 204 = success

# def assign_discord_role(discord_id, role_id):
#     url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
#     headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

#     r = requests.put(url, headers=headers)

#     if r.status_code == 429:
#         print("⚠️ Discord rate limited:", r.text)

#     if r.status_code not in (204, 200):
#         print("❌ Discord error:", r.status_code, r.text)

#     return r.status_code in (204, 200)


# def assign_discord_role(discord_id, role_id):
#     url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
#     headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

#     r = requests.put(url, headers=headers)

#     if r.status_code == 429:
#         retry_after = float(r.headers.get("Retry-After", "2"))
#         return {"ok": False, "rate_limited": True, "retry_after": retry_after, "status": 429, "text": r.text}

#     if r.status_code in (204, 200):
#         return {"ok": True, "rate_limited": False, "status": r.status_code}

#     return {"ok": False, "rate_limited": False, "status": r.status_code, "text": r.text}

import requests
import time

# def assign_discord_role(discord_id, role_id):
#     url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
#     headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

#     r = requests.put(url, headers=headers)

#     # Return both success flag + response details for better retries/logging
#     return r

import requests

def assign_discord_role(discord_id, role_id):
    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    return requests.put(url, headers=headers, timeout=15)

def remove_discord_role(discord_id, role_id):
    url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}/roles/{role_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

    r = requests.delete(url, headers=headers)

    if r.status_code != 204:
        print(f"Failed to remove role {role_id} from {discord_id}: {r.status_code} - {r.text}")

    return r.status_code == 204


import requests

def discord_member_has_role(discord_id, role_id):
    url = f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{discord_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        return None, r
    roles = r.json().get("roles", [])
    return (role_id in roles), r




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
    user = validate_token(session_token)
    if not user:
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

    
    # Ensure the Discord account isn't already linked to another user
    if User.objects.filter(discord_id=discord_id).exclude(user_id=user.user_id).exists():
        return redirect(f"{return_url}?discord=already_linked")

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
            "region": history.region,
            "city": history.city,
            "continent": history.continent,
            "timezone": history.timezone,
            "timestamp": history.created_at
        })

    return Response({"login_history": history_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def get_user_login_history(request):
    username = request.data.get("username")
    email = request.data.get("email")

    if not username:
        return Response({"message": "Username is required."}, status=status.HTTP_400_BAD_REQUEST)
    else:
        if username:
            pass
        elif not email:
            return Response({"message": "Email is Required"})

    try:
        if username:
            user = User.objects.get(username=username)
        if email:
            user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({"message": "User not found."}, status=status.HTTP_404_NOT_FOUND)

    histories = LoginHistory.objects.filter(user=user).order_by('-created_at')
    history_data = []

    for history in histories:
        history_data.append({
            "ip_address": history.ip_address,
            "user_agent": history.user_agent,
            "timestamp": history.created_at,
            "country": history.country,
            "region": history.region,
            "city": history.city,
            "continent": history.continent,
        })

    return Response({"login_history": history_data}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_notifications(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # Identify the logged-in user using the session token
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    notifications = Notifications.objects.filter(user=user).order_by('-created_at')
    notifications_data = []

    for notification in notifications:
        notifications_data.append({
            "id": notification.notification_id,
            "message": notification.message,
            "is_read": notification.is_read,
            "created_at": notification.created_at
        })

    return Response({"notifications": notifications_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def view_notification(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    notification_id = request.data.get("notification_id")

    if not notification_id:
        return Response({"message": "Notification ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        notification = Notifications.objects.get(notification_id=notification_id)
    except Notifications.DoesNotExist:
        return Response({"message": "Notification not found."}, status=status.HTTP_404_NOT_FOUND)

    notification.is_read = True
    notification.save()

    return Response({"message": "Notification marked as read."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def send_notification(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if user.role != "admin":
        return Response({"message": "You do not have permission to send notifications."}, status=status.HTTP_403_FORBIDDEN)

    recipient_id = request.data.get("recipient_id")
    message = request.data.get("message")

    if not recipient_id or not message:
        return Response({"message": "Recipient ID and message are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        recipient = User.objects.get(user_id=recipient_id)
    except User.DoesNotExist:
        return Response({"message": "Recipient user not found."}, status=status.HTTP_404_NOT_FOUND)

    notification = Notifications.objects.create(
        user=recipient,
        message=message
    )

    AdminHistory.objects.create(
        admin_user=user,
        action="sent_notification",
        description=f"Sent notification to user {recipient.username} (ID: {recipient.user_id})"
    )

    return Response({"message": "Notification sent successfully.", "notification_id": notification.notification_id}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def send_notification_to_multiple_users(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if user.role != "admin":
        return Response({"message": "You do not have permission to send notifications."}, status=status.HTTP_403_FORBIDDEN)

    recipient_ids = request.data.get("recipient_ids", [])
    message = request.data.get("message")

    if not recipient_ids or not message:
        return Response({"message": "Recipient IDs and message are required."}, status=status.HTTP_400_BAD_REQUEST)

    recipients = User.objects.filter(user_id__in=recipient_ids)
    for recipient in recipients:
        Notifications.objects.create(
            user=recipient,
            message=message
        )

    
    set_audit(request, f"Sent a notification to {len(recipients)} users")
    AdminHistory.objects.create(
        admin_user=user,
        action="sent_notification_multiple",
        description=f"Sent notification to multiple users: {', '.join([str(r.user_id) for r in recipients])}"
    )

    return Response({"message": "Notifications sent successfully."}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def admin_send_message(request):
    """
    Admin -> direct message to a single PLAYER or a whole TEAM, over push (in-app
    Notifications), email, or both. Backs the "Send Message" control on the admin
    Players + Teams detail pages so an admin can reach a specific player or every
    member of a team without going through an event broadcast.

    Payload:
      target_type : "player" | "team"
      target_id   : user_id (player) or team_id (team)
      title       : optional subject / notification title
      message     : required body text
      delivery    : "push" | "email" | "both"   (default "both")
    """
    # ── auth (mirror the sibling notification endpoints) ──
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Authorization header is required."}, status=status.HTTP_400_BAD_REQUEST)
    user = validate_token(session_token.split(" ")[1])
    if not user:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)
    if user.role != "admin":
        return Response({"message": "You do not have permission to send messages."}, status=status.HTTP_403_FORBIDDEN)

    # ── input ──
    target_type = (request.data.get("target_type") or "").strip().lower()
    target_id = request.data.get("target_id")
    title = (request.data.get("title") or "").strip()
    message = (request.data.get("message") or "").strip()
    delivery = (request.data.get("delivery") or "both").strip().lower()

    if target_type not in ("player", "team"):
        return Response({"message": "target_type must be 'player' or 'team'."}, status=status.HTTP_400_BAD_REQUEST)
    if not target_id:
        return Response({"message": "target_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not message:
        return Response({"message": "message is required."}, status=status.HTTP_400_BAD_REQUEST)
    if delivery not in ("push", "email", "both"):
        return Response({"message": "delivery must be 'push', 'email', or 'both'."}, status=status.HTTP_400_BAD_REQUEST)

    want_push = delivery in ("push", "both")
    want_email = delivery in ("email", "both")

    # ── resolve recipients ──
    # Local import so afc_auth has no import-time dependency on afc_team
    # (afc_team imports from afc_auth, so a top-level import here could cycle).
    from afc_team.models import Team, TeamMembers

    if target_type == "player":
        try:
            recipients = [User.objects.get(user_id=target_id)]
        except User.DoesNotExist:
            return Response({"message": "Player not found."}, status=status.HTTP_404_NOT_FOUND)
        target_label = recipients[0].username
    else:
        try:
            team = Team.objects.get(team_id=target_id)
        except Team.DoesNotExist:
            return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
        recipients = [
            tm.member
            for tm in TeamMembers.objects.filter(team=team).select_related("member")
            if tm.member
        ]
        target_label = team.team_name
        if not recipients:
            return Response({"message": "This team has no members to message."}, status=status.HTTP_400_BAD_REQUEST)

    # ── deliver ──
    pushed = 0
    emailed = 0
    # Push: one in-app Notification row per recipient (bulk for the team case).
    if want_push:
        Notifications.objects.bulk_create([
            Notifications(
                user=r,
                title=title or None,
                message=message,
                notification_type="admin_message",
            )
            for r in recipients
        ])
        pushed = len(recipients)
    # Email: best-effort per recipient. send_email() already validates the address
    # and returns False on failure, so one bad address never blocks the rest.
    if want_email:
        subject = title or "A message from African Free Fire Community"
        html_body = (
            f"<p>{message}</p>"
            "<p style='color:#888;font-size:12px'>Sent by the AFC admin team.</p>"
        )
        for r in recipients:
            if r.email and send_email(r.email, subject, html_body):
                emailed += 1

    set_audit(request, f"Sent a {delivery} message to the {target_type} {target_label}")
    AdminHistory.objects.create(
        admin_user=user,
        action="admin_send_message",
        description=(
            f"Sent {delivery} message to {target_type} '{target_label}' "
            f"({pushed} pushed, {emailed} emailed)"
        ),
    )

    return Response({
        "message": f"Message sent to {target_label}.",
        "recipients": len(recipients),
        "pushed": pushed,
        "emailed": emailed,
    }, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def get_total_players_count(request):
    total_players = User.objects.filter(role="player").count()
    return Response({"total_players": total_players}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_active_players_count(request):
    active_players = User.objects.filter(role="player", status="active").count()
    return Response({"active_players": active_players}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_banned_players_count(request):
    banned_players = User.objects.filter(role="player", status="banned").count()
    return Response({"banned_players": banned_players}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_new_players_count(request):
    from django.utils import timezone
    from datetime import timedelta

    now = timezone.now()
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    new_players_this_month = User.objects.filter(
        role="player",
        date_joined__gte=start_of_month
    ).count()

    return Response({"new_players_this_month": new_players_this_month}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_average_total_kills_per_player(request):
    # solo players
    solo_avg = SoloPlayerMatchStats.objects.aggregate(avg=Avg("kills"))["avg"] or 0

    # team players (squad/duo)
    team_avg = TournamentPlayerMatchStats.objects.aggregate(avg=Avg("kills"))["avg"] or 0

    # If you want one combined number:
    # (simple average of the two averages is not statistically perfect, but OK as a quick metric)
    combined = (solo_avg + team_avg) / 2 if (solo_avg or team_avg) else 0

    return Response({
        "solo_avg_kills_per_player_per_match": round(float(solo_avg), 2),
        "team_avg_kills_per_player_per_match": round(float(team_avg), 2),
        "combined_avg_kills_per_player_per_match": round(float(combined), 2),
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_top_mvp_player(request):
    top = (
        User.objects
        .filter(mvp_matches__isnull=False)
        .annotate(total_mvps=Count("mvp_matches"))
        .order_by("-total_mvps", "username")
        .first()
    )

    if not top:
        return Response({"message": "No MVP records found."}, status=status.HTTP_404_NOT_FOUND)

    return Response({
        "user_id": top.user_id,
        "username": top.username,
        "total_mvps": top.total_mvps,
    }, status=status.HTTP_200_OK)


from django.db.models import Count, Q
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

@api_view(["GET"])
def get_top_winner_player(request):
    # SOLO wins per user (placement 1)
    solo_wins = (
        SoloPlayerMatchStats.objects
        .filter(placement=1, competitor__user__isnull=False)
        .values("competitor__user_id", "competitor__user__username")
        .annotate(wins=Count("id"))
    )

    # TEAM wins per user (team placement 1 -> members)
    team_wins = (
        TournamentTeamMatchStats.objects
        .filter(placement=1)
        .values("tournament_team__members__user_id", "tournament_team__members__user__username")
        # TournamentTeamMatchStats sets team_stats_id as its explicit PK, so it has no `id` field;
        # count its real PK (matches repo convention, e.g. afc_team/views.py:951,967) to avoid FieldError -> 500
        .annotate(wins=Count("team_stats_id"))
    )

    # Merge in python (simple + safe)
    wins_map = {}
    for r in solo_wins:
        uid = r["competitor__user_id"]
        wins_map[uid] = {"user_id": uid, "username": r["competitor__user__username"], "wins": r["wins"]}

    for r in team_wins:
        uid = r["tournament_team__members__user_id"]
        if not uid:
            continue
        if uid not in wins_map:
            wins_map[uid] = {"user_id": uid, "username": r["tournament_team__members__user__username"], "wins": 0}
        wins_map[uid]["wins"] += r["wins"]

    if not wins_map:
        return Response({"message": "No win records found."}, status=status.HTTP_404_NOT_FOUND)

    top = max(wins_map.values(), key=lambda x: x["wins"])

    return Response({
        "user_id": top["user_id"],
        "username": top["username"],
        "total_wins": top["wins"],
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_admin_activities(request):
    activities = AdminHistory.objects.all().order_by('-timestamp')[:100]  # limit to latest 100 activities
    activities_data = []

    for activity in activities:
        activities_data.append({
            "admin_user": activity.admin_user.username,
            "action": activity.action,
            "description": activity.description,
            "timestamp": activity.timestamp
        })

    return Response({"admin_activities": activities_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def like_news(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    news_id = request.data.get("news_id")

    if not news_id:
        return Response({"message": "News ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        news_item = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News item not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user already liked this news
    existing_like = NewsLike.objects.filter(user=user, news=news_item).first()
    if existing_like:
        return Response({"message": "You have already liked this news item."}, status=status.HTTP_400_BAD_REQUEST)
    
    existing_dislike = NewsDislike.objects.filter(user=user, news=news_item).first()
    if existing_dislike:
        existing_dislike.delete()  # Remove dislike if exists

    NewsLike.objects.create(user=user, news=news_item)

    return Response({"message": "News item liked successfully."}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def unlike_news(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    news_id = request.data.get("news_id")

    if not news_id:
        return Response({"message": "News ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        news_item = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News item not found."}, status=status.HTTP_404_NOT_FOUND)

    existing_like = NewsLike.objects.filter(user=user, news=news_item).first()
    if not existing_like:
        return Response({"message": "You have not liked this news item."}, status=status.HTTP_400_BAD_REQUEST)

    existing_like.delete()

    return Response({"message": "News item unliked successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def dislike_news(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    news_id = request.data.get("news_id")

    if not news_id:
        return Response({"message": "News ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        news_item = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News item not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user already liked this news
    existing_like = NewsLike.objects.filter(user=user, news=news_item).first()
    if existing_like:
        existing_like.delete()  # Remove like if exists

    # check if  already disliked
    existing_dislike = NewsDislike.objects.filter(user=user, news=news_item).first()
    if existing_dislike:
        return Response({"message": "You have already disliked this news item."}, status=status.HTTP_400_BAD_REQUEST)

    # Here you can implement a NewsDislike model similar to NewsLike if you want to track dislikes separately
    NewsDislike.objects.create(user=user, news=news_item)

    return Response({"message": "News item disliked successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def undislike_news(request):
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)
    session_token = session_token.split(" ")[1]
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    news_id = request.data.get("news_id")

    if not news_id:
        return Response({"message": "News ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        news_item = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News item not found."}, status=status.HTTP_404_NOT_FOUND)

    existing_dislike = NewsDislike.objects.filter(user=user, news=news_item).first()
    if not existing_dislike:
        return Response({"message": "You have not disliked this news item."}, status=status.HTTP_400_BAD_REQUEST)

    existing_dislike.delete()

    return Response({"message": "News item undisliked successfully."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def get_news_likes_dislikes_count(request):
    session_token = request.data.get("session_token")
    news_id = request.data.get("news_id")
    try:
        news_item = News.objects.get(news_id=news_id)
    except News.DoesNotExist:
        return Response({"message": "News item not found."}, status=status.HTTP_404_NOT_FOUND)

    likes_count = NewsLike.objects.filter(news=news_item).count()
    dislikes_count = NewsDislike.objects.filter(news=news_item).count()
    is_liked = False
    is_disliked = False
    if session_token:
        user = validate_token(session_token)
        if user:
            is_liked = NewsLike.objects.filter(news=news_item, user=user).exists()
            is_disliked = NewsDislike.objects.filter(news=news_item, user=user).exists()

    return Response({
        "news_id": news_id,
        "likes": likes_count,
        "dislikes": dislikes_count,
        "is_liked_by_user": is_liked,
        "is_disliked_by_user": is_disliked,
    }, status=status.HTTP_200_OK)