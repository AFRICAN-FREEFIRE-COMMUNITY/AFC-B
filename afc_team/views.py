from datetime import timedelta, timedelta
from django.utils import timezone
import uuid
from collections import Counter
from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_auth.views import validate_token
from afc_tournament_and_scrims.models import TournamentTeam, TournamentTeamMatchStats, EventPrizePayout
from .models import Team, TeamMembers, Invite, Report, JoinRequest, TeamSocialMediaLinks
from afc_auth.models import AdminHistory, BannedPlayer, Notifications, TeamBan, User, UserProfile, UserRoles
from django.utils.timezone import now
# Invite.invite_id is a UUID. Looking it up with a non-UUID value (e.g. a tampered URL)
# raises ValidationError, NOT DoesNotExist, so we catch it to return 404 instead of 500.
from django.core.exceptions import ValidationError
from django.db.models import Avg, Count, F, Min, Q, Sum
from .models import Team, TeamMembers, Invite, TeamSocialMediaLinks
import json
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


# ──────────────────────────────────────────────────────────────────────────
# Player-ban helper (shared across the team views below)
#
# Ban rule (afc_auth.BannedPlayer): a player is CURRENTLY banned when an is_active=True
# row exists AND its ban_end_date is still in the future. Both conditions matter: an
# is_active row whose ban_end_date has already passed is a stale/expired ban and must NOT
# block the player. (TeamBan, the team-level sibling, is read separately via Team.is_banned,
# which TeamBan.lift_ban_if_expired() keeps in sync.)
#
# Used by: exit_team (POST /team/exit-team/) and edit_team (POST /team/edit-team/, which is
# also the team-profile / name / logo / social-links update surface). The tournament app's
# register_for_event replicates this same is_active+ban_end_date__gt=now check inline
# (afc_tournament_and_scrims/views.py) rather than importing across apps in its hot path.
# ──────────────────────────────────────────────────────────────────────────
def _is_player_banned(user):
    """Return the active, non-expired BannedPlayer row for `user`, or None if the player
    is not currently banned. Callers treat a truthy return as "blocked"."""
    return BannedPlayer.objects.filter(
        banned_player=user, is_active=True, ban_end_date__gt=timezone.now()
    ).first()


# ──────────────────────────────────────────────────────────────────────────
# Roster role families + the 6-player cap (single source of truth)
#
# A member's role is the single `management_role` CharField on TeamMembers
# (afc_team/models.py). Its choices MIX two families with very different rules:
#   - PLAYING roles  (team_captain / vice_captain / member): these are the people who
#     can be fielded; they count toward the 6-player cap and are the only members
#     eligible to be put on a tournament roster.
#   - STAFF roles    (coach / manager / analyst): support-only. They never play, never
#     count toward the cap, and must never be picked for an event roster.
# There is NO boolean on the model that distinguishes the two, so this split lives here
# as the canonical constant pair. It was previously duplicated as locals inside
# manage_team_roster; it is promoted to module scope so EVERY cap / eligibility check
# (manage_team_roster, the invite-accept path) shares one definition.
#
# Consumed by:
#   - manage_team_roster (POST /team/manage-roster/) — enforces the 6 PLAYING cap when a
#     member is moved INTO a playing role.
#   - respond_to_invite (POST /team/respond-to-invite/) — the invite-accept path that adds
#     a new member; rejects a 7th playing member.
#   - afc_tournament_and_scrims.register_for_event / add_player_to_event_roster import
#     STAFF_ROLES from here to keep coach/manager/analyst off event rosters (single rule).
# ──────────────────────────────────────────────────────────────────────────
PLAYER_ROLES = {"team_captain", "vice_captain", "member"}   # PLAYING — count toward the 6 cap
STAFF_ROLES = {"coach", "manager", "analyst"}               # MANAGEMENT — never play / never rostered
MAX_PLAYERS = 6


def _playing_member_count(team):
    """Count of PLAYING-role members on a team (the basis for the 6-player cap).

    Counts TeamMembers whose management_role is in PLAYER_ROLES (captain / vice / member).
    Staff (coach / manager / analyst) are excluded because they never occupy a player slot.
    This is the canonical cap basis (NOT in_game_role) per the roster-rules spec.
    """
    return TeamMembers.objects.filter(
        team=team, management_role__in=PLAYER_ROLES
    ).count()


# ──────────────────────────────────────────────────────────────────────────
# Team country auto-derivation (owner 2026-06-20)
#
# Team.country is AUTO-set from the LOCATION of the team's PLAYING members (PLAYER_ROLES; staff are
# excluded). Rule: the single most-common player country wins; if the top spot is a TIE between two or
# more countries (no clear majority, e.g. 2 NG / 2 ZA / 1 TG) it falls back to the TEAM OWNER's country.
# Counting uses a NORMALIZED key (afc_tournament_and_scrims.views.normalize_country, pycountry-backed) so
# "Nigeria" and "NG" are the SAME country; the stored value is a human-readable name. Recomputed on every
# roster change by the TeamMembers signal (afc_team/signals.py) and explicitly after an ownership transfer
# and when a member edits their own country (afc_auth.edit_profile). There is NO season/transfer-window
# lock (owner dropped that): the team country is always live.
# Consumed by: get_team_details (FE team page country + per-member country + the rule explanation),
# the recompute_team_countries management command (one-off backfill), and the signal/explicit triggers.
# ──────────────────────────────────────────────────────────────────────────
def _canonical_country(raw):
    """(counting_key, display_name) for a free-text/ISO country string, or ("","") when blank/unknown.
    Uses the shared pycountry-backed normalizer so 'Nigeria' and 'NG' collapse to one key; the display
    name is pycountry's canonical Name (properly cased) when resolvable, else the title-cased key."""
    if not raw or not str(raw).strip():
        return "", ""
    # Lazy cross-app import: afc_tournament_and_scrims.views is heavy and would be circular at load time.
    from afc_tournament_and_scrims.views import normalize_country
    key = normalize_country(raw)              # lowercased canonical name, or lowercased raw if unknown
    if not key:
        return "", ""
    try:
        import pycountry
        return key, pycountry.countries.lookup(key).name
    except Exception:
        return key, key.title()


def _derive_team_country(team):
    """Derive a team's country from its PLAYING members' locations (see the section header above).
    Returns the DISPLAY country name to store on Team.country, or "" when nothing is resolvable.
    Counts only PLAYER_ROLES members; unique most-common country wins; a tie at the top -> owner country."""
    playing = (
        TeamMembers.objects
        .filter(team=team, management_role__in=PLAYER_ROLES)
        .select_related("member")
    )
    pairs = [_canonical_country(getattr(tm.member, "country", "")) for tm in playing]
    counts = Counter(k for k, _ in pairs if k)
    display_by_key = {k: d for k, d in pairs if k}

    _, owner_display = _canonical_country(getattr(team.team_owner, "country", "") or "")

    if not counts:
        return owner_display                 # no playing-member countries -> owner's country

    ranked = counts.most_common()
    # A unique top (clear plurality) wins; a tie for first place falls back to the owner's country.
    if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
        win_key = ranked[0][0]
        return display_by_key.get(win_key, win_key.title())
    return owner_display


def recompute_team_country(team):
    """Re-derive + persist Team.country (best-effort; writes only when it actually changes). Called by the
    TeamMembers signal (afc_team/signals.py) and by the owner-transfer / profile-country-edit paths. Safe
    to call any time; never raises into the caller so a recompute can't break a roster mutation."""
    try:
        new_country = (_derive_team_country(team) or "")[:64]
        if new_country != (team.country or ""):
            team.country = new_country
            team.save(update_fields=["country"])
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Team-tag helper (shared by create_team and edit_team)
#
# team_tag (Team.team_tag, CharField(max_length=5, null=True)) is the short team handle
# (e.g. "AFC"). It is OPTIONAL. The same tag also powers two other surfaces:
#   1. team search  -> search_teams (GET /team/search-teams/) matches q against team_tag.
#   2. OCR matching  -> the leaderboard OCR pipeline matches the short tag printed on a
#      player's name against this field, so keeping it clean (letters/digits, upper-cased)
#      matters beyond the profile page.
# Normalisation: strip whitespace, upper-case, cap at 5 chars. Empty string -> None so the
# owner can clear the tag. Validation: letters and digits only (no spaces / symbols), which
# is what both search and OCR expect.
#
# Returns a 2-tuple: (normalised_value_or_None, error_message_or_None). Callers check the
# error first and return a 400 with it; otherwise they assign the value onto the team.
# ──────────────────────────────────────────────────────────────────────────
# Sentinel used by edit_team to distinguish "team_tag key omitted" (leave the existing tag
# alone) from "team_tag sent as empty string" (owner wants to clear the tag). A plain None
# can't carry that distinction because the form may legitimately send an empty value.
_TAG_UNSET = object()


def _normalize_team_tag(raw):
    # Treat a missing key (None) as "no change requested" by the caller; only the caller
    # knows whether absence means "leave as-is" (edit) or "stays null" (create), so we just
    # report None back unchanged here.
    if raw is None:
        return None, None

    # Strip surrounding whitespace and upper-case so "afc " becomes "AFC".
    tag = str(raw).strip().upper()

    # Empty after stripping -> caller wants the tag cleared (stored as NULL).
    if tag == "":
        return "", None

    # Length cap mirrors the model's max_length=5.
    if len(tag) > 5:
        return None, "Team tag must be at most 5 characters."

    # Letters and digits only: spaces or symbols break search + OCR matching.
    if not tag.isalnum():
        return None, "Team tag can only contain letters and digits (no spaces or symbols)."

    return tag, None


@api_view(["POST"])
def create_team(request):
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

    # Ensure user is not in another team
    if TeamMembers.objects.filter(member=user).exists():
        return Response({"message": "You are already in another team and cannot create a new one."}, status=status.HTTP_400_BAD_REQUEST)

    # Extract data
    team_name = request.data.get("team_name")
    team_logo = request.FILES.get("team_logo")
    team_description = request.data.get("team_description", "We Love Playing Free Fire")
    country = user.country
    join_settings = request.data.get("join_settings", "by_request")
    # OPTIONAL short team handle (e.g. "AFC"). Normalised + validated by _normalize_team_tag.
    # Also feeds team search (search_teams) and the OCR name-matching pipeline.
    team_tag = request.data.get("team_tag")
    list_of_players_to_invite = request.data.getlist("list_of_players_to_invite", [])
    team_social_media_links = request.data.get("team_social_media_links", [])
    if team_social_media_links:
        team_social_media_links = json.loads(team_social_media_links)

    # Validate required fields
    if not team_name:
        return Response({"message": "Team name is required."}, status=status.HTTP_400_BAD_REQUEST)

    if join_settings not in ["open", "by_request"]:
        return Response({"message": "Invalid join settings."}, status=status.HTTP_400_BAD_REQUEST)

    # Check for existing team name
    if Team.objects.filter(team_name=team_name).exists():
        return Response({"message": "Team name already exists."}, status=status.HTTP_400_BAD_REQUEST)
    
    if len(team_description) > 200:
        return Response({"message": "Team Description should not be more than 200 Characters."}, status=status.HTTP_400_BAD_REQUEST)

    # Normalise + validate the optional team tag. Empty string -> stored as NULL.
    normalized_tag, tag_error = _normalize_team_tag(team_tag)
    if tag_error:
        return Response({"message": tag_error}, status=status.HTTP_400_BAD_REQUEST)
    # "" means "no tag"; anything else is the cleaned tag. None (key absent) also -> NULL on create.
    team_tag_value = normalized_tag if normalized_tag else None

    # Create the team
    team = Team.objects.create(
        team_name=team_name,
        team_logo=team_logo,
        team_creator=user,
        team_owner=user,
        team_description=team_description,
        team_tag=team_tag_value,
        country=country,
        join_settings=join_settings
    )

    # Add team creator as owner
    TeamMembers.objects.create(team=team, member=user, management_role='team_captain')

    # Process invites
    invited_players = []
    for identifier in list_of_players_to_invite:
        invitee = User.objects.filter(Q(username=identifier) | Q(email=identifier)).first()
        if invitee and not TeamMembers.objects.filter(member=invitee).exists():
            Invite.objects.create(inviter=user, invitee=invitee, team=team)
            invited_players.append(invitee.username)

    # # Save social media links
    # for sm_link in team_social_media_links:
    #     platform = sm_link.get("platform")
    #     link = sm_link.get("link")
    #     if platform and link:
    #         TeamSocialMediaLinks.objects.create(team=team, platform=platform, link=link)

    for link_data in team_social_media_links:
        platform = link_data.get("platform")
        link = link_data.get("link")
        if platform and link:
            TeamSocialMediaLinks.objects.create(team=team, platform=platform, link=link)

    Report.objects.create(
        team=team,
        user=user,
        action="team_created",
        description=f"Team '{team.team_name}' was created by {user.username} on {now()}."
    )

    return Response({
        "message": "Team created successfully.",
        "team_id": team.team_id,
        "team_name": team.team_name,
        "team_tag": team.team_tag,
        "team_owner": team.team_owner.username,
        "invited_players": invited_players
    }, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def invite_member(request):
   # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]

    # # Identify the logged-in user using the session token
    # try:
    #     user = User.objects.get(session_token=session_token)
    # except User.DoesNotExist:
    #     return Response({"message": "Invalid session token."}, status=status.HTTP_401_UNAUTHORIZED)
    
    invitee_email_or_ign = request.data.get("invitee_email_or_ign")
    team_id = request.data.get("team_id")


    if not invitee_email_or_ign or not team_id:
        return Response({'message': 'Invitee and team ID are required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Validate inviter
        # inviter = User.objects.get(session_token=session_token)
        inviter = validate_token(session_token)
        if not inviter:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Validate the team and check if the inviter is the owner or captain
        team = Team.objects.get(team_id=team_id, team_owner=inviter)
        
        # Validate invitee by checking both email and in-game name
        invitee = User.objects.filter(email=invitee_email_or_ign).first() or \
                  User.objects.filter(in_game_name=invitee_email_or_ign).first()

        if not invitee:
            return Response({'message': 'Invitee not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Ensure the invitee is not already in a team
        if TeamMembers.objects.filter(member=invitee).exists():
            return Response({'message': 'The invitee is already a member of another team.'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if an invitation already exists
        if Invite.objects.filter(invitee=invitee, team=team, status_of_invite='unattended_to').exists():
            return Response({'message': 'An invitation to this user is already pending.'}, status=status.HTTP_400_BAD_REQUEST)

        # Create invitation
        Invite.objects.create(inviter=inviter, invitee=invitee, team=team, status_of_invite='unattended_to')

        # Notify the invitee (optional)
        notification_message = f"You have been invited to join the team: {team.team_name}."
        Notifications.objects.create(
            user=invitee,
            message=notification_message,
            notification_type="team_invitation",
            invite=Invite.objects.latest('created_at', invitee=invitee, team=team)
        )

        return Response({'message': 'Invitation sent successfully.'}, status=status.HTTP_201_CREATED)

    except User.DoesNotExist:
        return Response({'message': 'Invalid session token.'}, status=status.HTTP_401_UNAUTHORIZED)
    except Team.DoesNotExist:
        return Response({'message': 'Team not found or you do not own this team.'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'message': 'An error occurred.', 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def review_invitation(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]
    
    invite_id = request.data.get("invite_id")
    decision = request.data.get("decision")  # 'accepted' or 'declined'


    if decision not in ['accepted', 'declined']:
        return Response({'message': 'Invalid decision. Must be "accepted" or "declined".'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Validate the user
        user = validate_token(session_token)
        if not user:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Validate the invite
        invite = Invite.objects.get(invite_id=invite_id, invitee=user)

        if invite.status_of_invite == 'attended_to':
            return Response({'message': 'This invitation has already been reviewed.'}, status=status.HTTP_400_BAD_REQUEST)

        # Process the decision
        if decision == 'accepted':
            # Ensure the invitee is not part of another team
            if TeamMembers.objects.filter(member=user).exists():
                return Response({'message': 'You are already a member of a team.'}, status=status.HTTP_400_BAD_REQUEST)

            
            # Ensure the team is not up to 8 players yet
            if TeamMembers.objects.filter(team=invite.team).count() >= 8:
                return Response({'message': 'The team has reached the maximum number of members.'}, status=status.HTTP_400_BAD_REQUEST)


            # Ensure there are not more than 6 players with member management role
            

            # Add the user to the team
            TeamMembers.objects.create(team=invite.team, member=user, management_role='member', in_game_role='rusher')
            invite.decision = 'accepted'
        else:
            invite.decision = 'declined'

        # Mark the invite as attended
        invite.status_of_invite = 'attended_to'
        invite.save()

        Report.objects.create(
            team=invite.team,
            user=user,
            action="invitation_reviewed",
            description=f"Invitation {invite.invite_id} was {decision} by {user.username}."
        )

        return Response({'message': f'Invitation {decision} successfully.'}, status=status.HTTP_200_OK)

    except User.DoesNotExist:
        return Response({'message': 'Invalid session token.'}, status=status.HTTP_401_UNAUTHORIZED)
    except Invite.DoesNotExist:
        return Response({'message': 'Invalid invitation ID or you are not authorized to review this invite.'}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({'message': 'An error occurred.', 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["POST"])
def disband_team(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]
    
    team_id = request.data.get("team_id")

    if not team_id:
        return Response({'message': 'Team ID is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Validate user
        user = validate_token(session_token)
        if not user:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Validate team ownership
        team = Team.objects.get(team_id=team_id, team_owner=user)

        # Roster moves are locked outside the transfer window — a team cannot be disbanded
        # while the window is CLOSED (matches the player-leave lock in exit_team). The window
        # is the active ranking season's range (afc_rankings.Season), toggled by admins.
        from afc_rankings.models import Season
        active_season = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
        if active_season and not active_season.is_transfer_window_open():
            return Response(
                {"message": "The transfer window is currently closed. Teams cannot be disbanded until it reopens."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Create a report before deleting the team
        Report.objects.create(
            team=team,
            user=user,
            action="team_disbanded",
            description=f"Team '{team.team_name}' was disbanded by {user.username} on {now()}."
        )

        # Delete invites related to the team
        Invite.objects.filter(team=team).delete()
        # Delete join requests related to the team
        JoinRequest.objects.filter(team=team).delete()

        # Notify team members (optional)
        team_members = TeamMembers.objects.filter(team=team)
        for member in team_members:
            notification_message = f"The team '{team.team_name}' has been disbanded by the owner."
            Notifications.objects.create(
                user=member.member,
                message=notification_message,
                notification_type="team_disbanded"
            )

        # Remove all team members
        TeamMembers.objects.filter(team=team).delete()

        # Delete the team
        team.delete()

        return Response({'message': 'Team disbanded successfully, and a report has been recorded.'}, status=status.HTTP_200_OK)

    except User.DoesNotExist:
        return Response({'message': 'Invalid session token.'}, status=status.HTTP_401_UNAUTHORIZED)
    except Team.DoesNotExist:
        return Response({'message': 'Team not found or you do not own this team.'}, status=status.HTTP_403_FORBIDDEN)
    except Exception as e:
        return Response({'message': 'An error occurred.', 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def transfer_ownership(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]
    
    new_owner_ign = request.data.get("new_owner_ign")  # Username of the new owner

    if not new_owner_ign:
        return Response({"message": "New owner username is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Identify the logged-in user (current owner)
        # current_owner = User.objects.get(session_token=session_token)
        current_owner = validate_token(session_token)
        if not current_owner:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Find the team where the user is the owner
        team = Team.objects.get(team_owner=current_owner)

        # Ensure the new owner exists and is part of the team
        new_owner = User.objects.get(username=new_owner_ign)
        try:
            new_owner_member = TeamMembers.objects.get(team=team, member=new_owner)
        except TeamMembers.DoesNotExist:
            return Response({"message": "New owner must be a member of the team."}, status=status.HTTP_400_BAD_REQUEST)

        # Update roles in TeamMembers
        # 1. Old owner -> member
        TeamMembers.objects.filter(team=team, member=current_owner).update(management_role="member")

        # 2. New owner -> team_owner
        # new_owner_member.management_role = "tea"
        new_owner_member.save()

        # Transfer ownership
        team.team_owner = new_owner
        team.save()

        # Re-derive the team country now that the owner changed: on a country TIE the tiebreak follows
        # the (new) owner. Neither change above triggers the roster signal (the old-owner role change is a
        # bulk .update(), and the owner change is a Team save, not a TeamMembers save), so recompute here.
        recompute_team_country(team)

        # Log the action
        Report.objects.create(
            team=team,
            user=current_owner,
            action="role_changed",
            description=f"Ownership transferred from {current_owner.username} to {new_owner.username}."
        )

        # Notify the new owner
        Notifications.objects.create(
            user=new_owner,
            message=f"You are now the owner of the team: {team.team_name}.",
            notification_type="team_owner_transfer"
        )

        # Notify the old owner
        Notifications.objects.create(
            user=current_owner,
            message=f"You have transferred ownership of the team: {team.team_name} to {new_owner.username}.",
            notification_type="team_owner_transfer"
        )

        # Notify other team members
        other_members = TeamMembers.objects.filter(team=team).exclude(member__in=[current_owner, new_owner])
        for member in other_members:
            Notifications.objects.create(
                user=member.member,
                message=f"{new_owner.username} is now the owner of the team: {team.team_name}.",
                notification_type="team_owner_transfer"
            )

        return Response({
            "message": "Team ownership transferred successfully.",
            "new_owner": new_owner.username
        }, status=status.HTTP_200_OK)

    except User.DoesNotExist:
        return Response({"message": "Invalid session token or new owner username."}, status=status.HTTP_404_NOT_FOUND)
    except Team.DoesNotExist:
        return Response({"message": "You do not own any team."}, status=status.HTTP_403_FORBIDDEN)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def send_join_request(request):
    # Retrieve session token
    session_token = request.headers.get("Authorization")

    if not session_token:
        return Response({'status': 'error', 'message': 'Authorization header is required'}, status=status.HTTP_400_BAD_REQUEST)

    if not session_token.startswith("Bearer "):
        return Response({'status': 'error', 'message': 'Invalid token format'}, status=status.HTTP_400_BAD_REQUEST)

    session_token = session_token.split(" ")[1]
    
    team_id = request.data.get("team_id")
    message = request.data.get("message")

    try:
        # Identify the requester
        # requester = User.objects.get(session_token=session_token)
        requester = validate_token(session_token)
        if not requester:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # Ensure the team exists
        team = Team.objects.get(team_id=team_id)

        # Ensure the user is not already in a team
        if TeamMembers.objects.filter(member=requester).exists():
            return Response({"message": "You are already a member of a team."}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure a request isn't already pending
        if JoinRequest.objects.filter(requester=requester, team=team, status_of_request="unattended_to").exists():
            return Response({"message": "You already have a pending join request for this team."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Ensure the team is not up to 8 players yet
        if TeamMembers.objects.filter(team=team).count() >= 8:
            return Response({'message': 'The team has reached the maximum number of members.'}, status=status.HTTP_400_BAD_REQUEST)

        # Create a join request
        JoinRequest.objects.create(requester=requester, team=team, message=message)
        
        return Response({"message": "Join request sent successfully."}, status=status.HTTP_201_CREATED)

    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_404_NOT_FOUND)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def review_join_request(request):
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
    
    request_id = request.data.get("request_id")
    decision = request.data.get("decision")  # 'approved' or 'denied'

    if not request_id:
        return Response({"message": "Request ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    if not decision:
        return Response({"message": "Decision is required."}, status=status.HTTP_400_BAD_REQUEST)

    if decision not in ["approved", "denied"]:
        return Response({"message": "Invalid decision. Must be 'approved' or 'denied'."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Identify the team owner or captain reviewing the request
        reviewer = user

        # Ensure the join request exists and belongs to a team the reviewer owns
        join_request = JoinRequest.objects.get(request_id=request_id)
        team = join_request.team

        # Check if the reviewer has permission to approve/deny join requests
        if team.team_owner != reviewer:
            return Response({"message": "You do not have permission to review join requests for this team."}, status=status.HTTP_403_FORBIDDEN)

        # Ensure the request has not already been reviewed
        if join_request.status_of_request == "attended_to":
            return Response({"message": "This request has already been reviewed."}, status=status.HTTP_400_BAD_REQUEST)

        if decision == "approved":
            # Ensure the requester is not already in a team
            if TeamMembers.objects.filter(member=join_request.requester).exists():
                return Response({"message": "User is already a member of a team."}, status=status.HTTP_400_BAD_REQUEST)
            
            # Ensure the team is not up to 8 players yet
            if TeamMembers.objects.filter(team=team).count() >= 8:
                return Response({'message': 'The team has reached the maximum number of members.'}, status=status.HTTP_400_BAD_REQUEST)

            # Add the user to the team
            TeamMembers.objects.create(team=team, member=join_request.requester, management_role='member')


        # Update join request status
        join_request.status_of_request = "attended_to"
        join_request.decision = decision
        join_request.save()

        # Log the action in the Report table
        Report.objects.create(
            team=team,
            user=join_request.requester,
            action="player_joined" if decision == "approved" else "player_removed",
            description=f"Join request {decision} by {join_request.requester.username}."
        )

        # Notify the requester
        notification_message = f"Your request to join the team '{team.team_name}' has been {decision}."
        Notifications.objects.create(
            user=join_request.requester,
            message=notification_message,
            notification_type="join_request_review"
        )

        return Response({"message": f"Join request {decision} successfully."}, status=status.HTTP_200_OK)

    except User.DoesNotExist:
        return Response({"message": "Invalid session token."}, status=status.HTTP_404_NOT_FOUND)
    except JoinRequest.DoesNotExist:
        return Response({"message": "Join request not found."}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["GET"])
def view_join_requests(request):
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

    try:
        # Find the team where the user is the owner.
        # team_owner is a ForeignKey (related_name='owned_teams', models.py:25), so a user
        # can own multiple teams -- .get() would raise MultipleObjectsReturned -> 500. Use
        # .filter().first() (the repo convention, see lines 89/411/1277) + a guard instead.
        team = Team.objects.filter(team_owner=user).first()
        if not team:
            return Response({"message": "You do not own any team."}, status=status.HTTP_403_FORBIDDEN)

        # Fetch all pending join requests for the team
        join_requests = JoinRequest.objects.filter(team=team, status_of_request="unattended_to")

        requests_data = []
        for req in join_requests:
            requests_data.append({
                "request_id": req.request_id,
                "requester": req.requester.username,
                "uid": req.requester.uid,
                "message": req.message,
                "request_date": req.created_at
            })

        return Response({"join_requests": requests_data}, status=status.HTTP_200_OK)

    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["POST"])
def view_join_requests_for_a_team(request):
    team_id = request.data.get("team_id")

    if not team_id:
        return Response({"message": "Team ID is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Ensure the team exists
        team = Team.objects.get(team_id=team_id)

        # Fetch all pending join requests for the team
        join_requests = JoinRequest.objects.filter(team=team, status_of_request="unattended_to")

        requests_data = []
        for req in join_requests:
            requests_data.append({
                "request_id": req.request_id,
                "requester": req.requester.username,
                "uid": req.requester.uid,
                "message": req.message,
                "request_date": req.created_at
            })

        return Response({"join_requests": requests_data}, status=status.HTTP_200_OK)

    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def edit_team(request):
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

    # Extract data from request
    team_id = request.data.get("team_id")
    team_name = request.data.get("team_name")
    team_logo = request.FILES.get("team_logo")
    join_settings = request.data.get("join_settings")
    # OPTIONAL short team handle. Default to a sentinel (object()) so we can tell "key omitted"
    # (leave the tag untouched) apart from "key sent empty" (owner is clearing the tag).
    team_tag = request.data.get("team_tag", _TAG_UNSET)
    social_media_links = request.data.get("social_media_links", [])  # Expecting a list of dicts [{'platform': 'Twitter', 'link': 'https://twitter.com/...'}]
    if social_media_links:
        social_media_links = json.loads(social_media_links)

    # Validate team ID
    try:
        team = Team.objects.get(team_id=team_id)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # Ensure the user is the team owner
    if team.team_owner != user:
        return Response({"message": "You do not have permission to edit this team."}, status=status.HTTP_403_FORBIDDEN)

    # ── ban guard (afc_auth.BannedPlayer + Team.is_banned) ──
    # A banned team (TeamBan -> Team.is_banned) or a banned acting user may NOT edit the team
    # or its profile (name / logo / join settings / social links). edit_team is the team-profile
    # update endpoint, so this single guard covers profile edits too. Placed right after the
    # owner-permission check so the owner identity is already resolved.
    if team.is_banned:
        return Response({"message": "This team is banned and cannot be edited."}, status=status.HTTP_403_FORBIDDEN)
    if _is_player_banned(user):
        return Response({"message": "You are banned and cannot edit a team."}, status=status.HTTP_403_FORBIDDEN)

    # Track if team name changes
    old_team_name = team.team_name
    team_name_changed = False

    # Update fields if provided
    if team_name and team_name != old_team_name:
        if Team.objects.filter(team_name=team_name).exclude(team_id=team.team_id).exists():
            return Response({"message": "Team name already exists."}, status=status.HTTP_400_BAD_REQUEST)
        team.team_name = team_name
        team_name_changed = True

    if team_logo:
        team.team_logo = team_logo

    if join_settings in ["open", "by_request"]:
        team.join_settings = join_settings

    # Update the optional team tag only when the client actually sent the key. An empty value
    # clears it (stored as NULL); a non-empty value is normalised + validated. The tag also
    # feeds team search (search_teams) and the OCR name-matching pipeline, so it is kept clean.
    if team_tag is not _TAG_UNSET:
        normalized_tag, tag_error = _normalize_team_tag(team_tag)
        if tag_error:
            return Response({"message": tag_error}, status=status.HTTP_400_BAD_REQUEST)
        # "" -> clear the tag (NULL); otherwise store the cleaned, upper-cased handle.
        team.team_tag = normalized_tag if normalized_tag else None

    team.save()

    # Log report if team name was changed
    if team_name_changed:
        Report.objects.create(
            team=team,
            user=user,
            action="team_name_changed",
            description=f"Team name changed from '{old_team_name}' to '{team_name}' by {user.username}."
        )

    # Update social media links
    if isinstance(social_media_links, list):
        # Clear existing links
        TeamSocialMediaLinks.objects.filter(team=team).delete()

        # Add new links
        for link_data in social_media_links:
            platform = link_data.get("platform")
            link = link_data.get("link")
            if platform and link:
                TeamSocialMediaLinks.objects.create(team=team, platform=platform, link=link)

    # Return team_tag in the response so the frontend edit form can re-sync the saved value
    # (the create response already includes it; this keeps the two endpoints consistent).
    return Response({"message": "Team details updated successfully.", "team_tag": team.team_tag}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_teams(request):
    # Admin team list (frontend app/(a)/a/teams/page.tsx -> GET /team/get-all-teams/).
    # PERFORMANCE: previously this lazy-loaded team_creator + team_owner usernames AND ran a
    # TeamMembers count() PER team inside the loop -> ~3 queries x 567 teams (~1700 queries,
    # ~7.5s). Now: select_related the two owner/creator FKs (one join) + a single grouped
    # member-count query assembled into a dict. Same response shape, ~3 queries total.
    teams = Team.objects.all().select_related("team_creator", "team_owner")

    member_counts = {
        row["team"]: row["c"]
        for row in TeamMembers.objects.values("team").annotate(c=Count("pk"))
    }

    teams_data = []
    for team in teams:
        teams_data.append({
            "team_id": team.team_id,
            "team_name": team.team_name,
            # Absolute URL (API host) so the logo loads from api.africanfreefirecommunity.com.
            # `.url` alone is relative (/media/...), which the browser resolves against the FRONTEND
            # origin where no media lives -> 404 / blank logos on /teams. Matches the build_absolute_uri
            # pattern used by the other team endpoints (get_team_details, etc.).
            "team_logo": request.build_absolute_uri(team.team_logo.url) if team.team_logo else None,
            "team_tag": team.team_tag,
            "join_settings": team.join_settings,
            "creation_date": team.creation_date,
            "team_creator": team.team_creator.username,
            "team_owner": team.team_owner.username,
            "is_banned": team.is_banned,
            "team_tier": team.team_tier,
            "team_description": team.team_description,
            "country": team.country,
            "member_count": member_counts.get(team.team_id, 0),
        })

    return Response({"teams": teams_data}, status=status.HTTP_200_OK)


# @api_view(["POST"])
# def get_team_details(request):
#     team_name = request.data.get("team_name")

#     if not team_name:
#         return Response({"message": "Team name is required."}, status=status.HTTP_400_BAD_REQUEST)

#     try:
#         team = Team.objects.get(team_name=team_name)
#     except Team.DoesNotExist:
#         return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

#     team_data = {
#         "team_id": team.team_id,
#         "team_name": team.team_name,
#         "team_logo": request.build_absolute_uri(team.team_logo.url) if team.team_logo else None,
#         "team_tag": team.team_tag,
#         "join_settings": team.join_settings,
#         "creation_date": team.creation_date,
#         "team_creator": team.team_creator.username,
#         "team_owner": team.team_owner.username,
#         "is_banned": team.is_banned,
#         "team_tier": team.team_tier,
#         "team_description": team.team_description,
#         "country": team.country
#     }

#     return Response({"team": team_data}, status=status.HTTP_200_OK)


def _team_tier_history(team):
    """
    Per-season tier + rank history for a team, sourced from the real afc_rankings
    TeamQuarterlyScore table — ONLY for seasons whose tiers/rankings have been published.

    tier is exposed only when Season.tiers_published; rank only when
    Season.rankings_published (the two are independent publish gates, matching the public
    rankings read API exactly). Seasons that expose nothing publicly yet are skipped.

    Returns a list (possibly empty) of:
      {season_id, season_name, year, quarter, tier, tier_label, rank}

    If afc_rankings is unavailable, returns [] so get_team_details degrades gracefully.
    """
    try:
        from afc_rankings.models import TeamQuarterlyScore
        from afc_rankings.serializers import TIER_LABELS
    except Exception:
        return []

    rows = (
        TeamQuarterlyScore.objects.filter(team=team)
        .select_related("season")
        .order_by("season__year", "season__quarter")
    )

    history = []
    for r in rows:
        season = r.season
        tier = r.tier_assigned if season.tiers_published else None
        rank = r.rank if season.rankings_published else None
        if tier is None and rank is None:
            continue  # nothing published for this season yet
        history.append({
            "season_id": season.season_id,
            "season_name": season.name,
            "year": season.year,
            "quarter": season.quarter,
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier) if tier is not None else None,
            "rank": rank,
        })
    return history


# ──────────────────────────────────────────────────────────────────────────────
# PRIVACY HELPERS (team stats visibility)
# ──────────────────────────────────────────────────────────────────────────────
# Detailed team statistics (win/loss totals, win rate, average kills/placement,
# per-event tournament performance, recent matches) are PRIVATE: only CURRENT
# members of that team — plus AFC admins — may see them. Anonymous or non-member
# viewers get the team's public identity (name, tier, member list) but NOT the
# detailed numbers.
#
# "Member" is REAL roster membership in afc_team.TeamMembers (one row per
# (team, member)). These helpers are consumed by get_team_details below. The
# frontend caller is TeamStatisticsTab.tsx, which now sends the viewer's Bearer
# token when logged in and reads the `stats_visible` flag in the response.


def _viewer_from_request(request):
    """
    Resolve the OPTIONAL viewer from an Authorization: Bearer <token> header.

    get_team_details stays public (no token required), so a missing / malformed /
    expired token simply yields None (anonymous viewer) instead of an error.
    Returns a User instance or None. Uses the shared validate_token helper so this
    behaves identically to the authenticated team endpoints.
    """
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    return validate_token(token)


def _can_view_team_stats(viewer, team):
    """
    Decide whether `viewer` (a User or None) may see `team`'s detailed stats.

    True when the viewer is an AFC admin (User.role == "admin") OR a current
    member of THIS team (a TeamMembers row for this (team, viewer)). Anonymous
    (viewer is None) or non-member viewers => False.

    Query cost: at most one tiny indexed existence check on TeamMembers. No N+1.
    """
    if viewer is None:
        return False

    # AFC admins always see full stats (consistent with require_admin elsewhere).
    if getattr(viewer, "role", None) == "admin":
        return True

    # Member of this exact team?
    return TeamMembers.objects.filter(team=team, member=viewer).exists()


@api_view(["POST"])
def get_team_details(request):
    """
    PUBLIC team detail + PRIVACY-GATED team statistics, keyed by team_name.

    Powers the public team page (frontend teams/[id]/page.tsx) and, in particular,
    the detailed "Statistics" tab (TeamStatisticsTab.tsx).

    AUTH (optional): stays public, but reads an OPTIONAL Authorization: Bearer
    <session-token> header to identify the viewer (via the shared validate_token).
    A missing/expired token simply means "anonymous viewer".

    PRIVACY (stats_visible): the DETAILED performance numbers (win/loss totals,
    win rate, average kills/placement, per-event tournament_performance[],
    recent_matches[]) are visible ONLY to CURRENT MEMBERS of this team
    (afc_team.TeamMembers rows for this team) and to AFC admins. For everyone else
    `stats_visible` is False and those sensitive numbers are ZEROED / EMPTIED.
    The team's public IDENTITY (name, tag, logo, tier, description, country,
    member count, member list, social links, ban info, tier_history) is ALWAYS
    returned. Back-compatible: no keys renamed — we only add `stats_visible` and
    gate the values behind it. TeamStatisticsTab.tsx reads `stats_visible` and
    shows a "members only" message when it is False.
    """
    team_name = request.data.get("team_name")

    if not team_name:
        return Response({"message": "Team name is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        team = Team.objects.get(team_name=team_name)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # ── Viewer + team-stats visibility (self-membership / admin gate) ──────────
    # Resolve the OPTIONAL viewer from the Bearer token (None when anonymous), then
    # decide whether they may see the DETAILED team stats: a current member of THIS
    # team, or an AFC admin. Non-members get the public identity only.
    viewer = _viewer_from_request(request)
    stats_visible = _can_view_team_stats(viewer, team)

    # Members
    members_qs = TeamMembers.objects.filter(team=team).select_related("member")
    members_data = [
        {
            "id": m.member.user_id,
            "uid": m.member.uid,
            "username": m.member.username,
            "management_role": m.management_role,
            "in_game_role": m.in_game_role,
            # Per-member country (raw User.country) so the team page can show each member's location and
            # explain how the auto-derived team country (above) was decided. (owner 2026-06-20)
            "country": m.member.country,
            "join_date": m.join_date,
            "discord_id": m.member.discord_id,
        }
        for m in members_qs
    ]

    # Social links
    social_links = [
        {"platform": lnk.platform, "link": lnk.link}
        for lnk in TeamSocialMediaLinks.objects.filter(team=team)
    ]

    # ── DETAILED STATS (members + admins only) ────────────────────────────────
    # Everything from here to recent_matches is the sensitive performance block.
    # We compute it ONLY when stats_visible is True; otherwise we short-circuit to
    # zeroed scalars + empty lists so no private number is computed or leaked. The
    # default values below are exactly what a brand-new team with no matches would
    # return, so the shape is identical either way (back-compatible).
    total_matches = 0
    total_wins = 0
    win_rate = 0
    avg_kills = 0
    avg_placement = 0
    tournament_performance = []
    recent_matches = []

    if stats_visible:
        # Aggregate match stats across all tournament entries
        agg = TournamentTeamMatchStats.objects.filter(
            tournament_team__team=team
        ).aggregate(
            total_matches=Count("team_stats_id"),
            total_wins=Count("team_stats_id", filter=Q(placement=1)),
            avg_kills=Avg("kills"),
            avg_placement=Avg("placement"),
        )
        total_matches = agg["total_matches"] or 0
        total_wins = agg["total_wins"] or 0
        win_rate = round((total_wins / total_matches) * 100, 1) if total_matches else 0
        avg_kills = round(float(agg["avg_kills"] or 0), 1)
        avg_placement = round(float(agg["avg_placement"] or 0), 1)

        # Per-event tournament performance
        tournament_teams = TournamentTeam.objects.filter(team=team).select_related("event")
        for tt in tournament_teams:
            tt_agg = TournamentTeamMatchStats.objects.filter(tournament_team=tt).aggregate(
                total_points=Sum("total_points"),
                total_kills=Sum("kills"),
                matches_played=Count("team_stats_id"),
                best_placement=Min("placement"),
            )

            # ── ADDITIVE: per-event date + per-event prize earned ──
            # event_date: prefer the event's scheduled start_date; fall back to the team's
            # registration date for this event when start_date is missing. Both are real fields.
            event_date = None
            if tt.event.start_date:
                event_date = tt.event.start_date.isoformat()
            elif tt.registration_date:
                event_date = tt.registration_date.isoformat()

            # prize_earned: the sum of EventPrizePayout rows recorded for THIS team in THIS
            # event (payout rows carry tournament_team + amount). Real, cleanly derivable.
            # Returns "0.00" when no payout row exists for this team/event (truthful zero,
            # not a fabricated figure).
            prize_earned = EventPrizePayout.objects.filter(
                event=tt.event, tournament_team=tt
            ).aggregate(total=Sum("amount"))["total"]

            tournament_performance.append({
                "event_id": tt.event.event_id,
                "name": tt.event.event_name,
                "competition_type": tt.event.competition_type,
                "event_status": tt.event.event_status,
                "team_status": tt.status,
                "best_placement": tt_agg["best_placement"],
                "total_points": tt_agg["total_points"] or 0,
                "total_kills": tt_agg["total_kills"] or 0,
                "matches_played": tt_agg["matches_played"] or 0,
                # additive keys (existing keys above are untouched):
                "event_date": event_date,
                "prize_earned": str(prize_earned) if prize_earned is not None else "0.00",
            })

        # Last 10 match stats
        recent_stat_qs = (
            TournamentTeamMatchStats.objects.filter(tournament_team__team=team)
            .select_related("match", "tournament_team__event")
            .order_by("-match__match_date")[:10]
        )
        recent_matches = [
            {
                "event_name": s.tournament_team.event.event_name,
                "match_number": s.match.match_number,
                "match_map": s.match.match_map,
                "placement": s.placement,
                "kills": s.kills,
                "total_points": s.total_points,
                "match_date": s.match.match_date,
            }
            for s in recent_stat_qs
        ]

    # Ban info
    ban_info = None
    try:
        tb = TeamBan.objects.get(team=team)
        ban_info = {
            "reason": tb.reason,
            "ban_start_date": tb.ban_start_date,
            "ban_end_date": tb.ban_end_date,
            "banned_by": tb.banned_by.username,
        }
    except TeamBan.DoesNotExist:
        pass

    # ── ADDITIVE: per-season tier / rank history (from afc_rankings, publish-gated) ──
    # Sourced from the real TeamQuarterlyScore table. tier is shown only for seasons whose
    # tiers are published; rank only when rankings are published — mirroring the public
    # rankings read API's two independent publish gates. Empty list when nothing published.
    tier_history = _team_tier_history(team)

    team_data = {
        "team_id": team.team_id,
        "team_name": team.team_name,
        "team_logo": request.build_absolute_uri(team.team_logo.url) if team.team_logo else None,
        "team_tag": team.team_tag,
        "join_settings": team.join_settings,
        "creation_date": team.creation_date,
        "team_creator": team.team_creator.username,
        "team_owner": team.team_owner.username,
        "team_captain": team.team_captain.username if team.team_captain else None,
        "is_banned": team.is_banned,
        "ban_info": ban_info,
        "team_tier": team.team_tier,
        "team_description": team.team_description,
        "country": team.country,
        "total_earnings": str(team.total_earnings or 0),
        "total_members": members_qs.count(),
        "members": members_data,
        "social_media_links": social_links,
        # ── Privacy flag: True only for current team members + AFC admins ──
        # When False, every detailed-stat field below is the zeroed/empty default
        # (no private number leaks). TeamStatisticsTab.tsx reads this flag and shows
        # a "Team stats are visible to team members only." message instead of the
        # numbers, keeping the public identity above visible.
        "stats_visible": stats_visible,
        # Stats (zeroed when stats_visible is False)
        "total_wins": total_wins,
        "total_losses": total_matches - total_wins,
        "win_rate": win_rate,
        "average_kills": avg_kills,
        "average_placement": avg_placement,
        # Performance (empty lists when stats_visible is False)
        "tournament_performance": tournament_performance,
        "recent_matches": recent_matches,
        # additive: per-season tier / rank history (publish-gated; [] when nothing published)
        # Always returned — tier/rank are public ranking data, not private team stats.
        "tier_history": tier_history,
    }

    return Response({"team": team_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def get_user_current_team(request):
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

    try:
        team_member = TeamMembers.objects.select_related("team").get(member=user)
        team = team_member.team

        team_data = {
            "team_id": team.team_id,
            "team_name": team.team_name,
            "team_logo": request.build_absolute_uri(team.team_logo.url) if team.team_logo else None,
            "team_tag": team.team_tag,
            "join_settings": team.join_settings,
            "creation_date": team.creation_date,
            "team_creator": team.team_creator.username,
            "team_owner": team.team_owner.username,
            "is_banned": team.is_banned,
            "team_tier": team.team_tier,
            "team_description": team.team_description,
            "country": team.country,
            "user_role_in_team": team_member.management_role,
            "in_game_role": team_member.in_game_role,
            "join_date": team_member.join_date,
            "member_count": TeamMembers.objects.filter(team=team).count()
        }

        return Response({"team": team_data}, status=status.HTTP_200_OK)

    except TeamMembers.DoesNotExist:
        return Response({"message": "You are not currently a member of any team."}, status=status.HTTP_404_NOT_FOUND)
    

@api_view(["POST"])
def get_player_details(request):
    # AUTH (2026-06-08): returns PII (user.email) + profile pics. Previously UNGATED — any
    # caller could POST a player_ign and read that player's email. No current frontend caller
    # (effectively orphaned), so we lock it to AFC staff (coarse role OR any granular UserRoles
    # row). Mirrors the gate added to afc_player.get_player_details.
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=status.HTTP_400_BAD_REQUEST)
    caller = validate_token(auth.split(" ")[1])
    if not caller:
        return Response({"message": "Invalid session."}, status=status.HTTP_401_UNAUTHORIZED)
    if caller.role not in ("admin", "moderator", "support") and not caller.userroles.exists():
        return Response({"message": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)

    player_ign = request.data.get("player_ign")

    if not player_ign:
        return Response({"message": "Player IGN is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(username=player_ign)
    except User.DoesNotExist:
        return Response({"message": "Player not found."}, status=status.HTTP_404_NOT_FOUND)
    
    try:
        profile = UserProfile.objects.get(user=user)
    except UserProfile.DoesNotExist:
        return Response({"message": "User profile not found."}, status=status.HTTP_404_NOT_FOUND)
    
    
    team_member = TeamMembers.objects.select_related("team").get(member=user)
    team = team_member.team

    player_data = {
        "username": user.username,
        "email": user.email,
        "country": user.country,
        "profile_picture": request.build_absolute_uri(profile.profile_pic.url) if profile.profile_pic else None,
        "esports_picture": request.build_absolute_uri(profile.esports_pic.url) if profile.esports_pic else None,
        "uid": user.uid,
        "team_id": team.team_id,
        "team_name": team.team_name,
        "team_logo": request.build_absolute_uri(team.team_logo.url) if team.team_logo else None,
        "management_role": team_member.management_role,
        "in_game_role": team_member.in_game_role,
        "join_date": team_member.join_date,
    }

    return Response({"player": player_data}, status=status.HTTP_200_OK)


@api_view(["POST"])
def exit_team(request):
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

    try:
        team_member = TeamMembers.objects.select_related("team").get(member=user)
        team = team_member.team

        # ── ban guard (afc_auth.BannedPlayer + Team.is_banned) ──
        # A banned player, or a member of a banned team (TeamBan -> Team.is_banned), is frozen in
        # place and cannot leave. This is an ADDITIONAL guard layered before the existing
        # transfer-window + active-tournament locks below (those stay intact). Membership must
        # NOT be deleted when this fires, so it returns before team_member.delete().
        if _is_player_banned(user) or team.is_banned:
            return Response(
                {"message": "You cannot leave your team while you or your team is banned."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Prevent the team owner from exiting the team
        if team.team_owner == user:
            return Response({"message": "Team owners cannot exit their own team. Please transfer ownership or disband the team."}, status=status.HTTP_403_FORBIDDEN)

        # Roster moves are locked outside the transfer window — a player can only leave a
        # team while the window is OPEN. The window is defined on the active ranking season
        # (afc_rankings.Season) and toggled by admins. Admin kicks/removals are unaffected.
        from afc_rankings.models import Season
        active_season = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
        if active_season and not active_season.is_transfer_window_open():
            return Response(
                {"message": "The transfer window is currently closed. You cannot leave your team until it reopens."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Active-tournament lock (separate from, and additional to, the transfer-window guard
        # above): a team's roster is FROZEN while it is registered for a tournament that has not
        # finished. "Active registration" = a TournamentTeam row for this team whose status is not
        # a removed one (disqualified / withdrawn / left) and whose event is still upcoming or
        # ongoing (not completed). TournamentTeam lives in afc_tournament_and_scrims and is
        # imported lazily here (same as Season) to avoid an app-level import cycle.
        from afc_tournament_and_scrims.models import TournamentTeam
        has_active_registration = (
            TournamentTeam.objects.filter(team=team)
            .exclude(status__in=["disqualified", "withdrawn", "left"])
            .filter(event__event_status__in=["upcoming", "ongoing"])
            .exists()
        )
        if has_active_registration:
            return Response(
                {"message": "You cannot leave your team while it is registered for an active tournament. Withdraw from the tournament first or wait until it is completed."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Remove the user from the team
        team_member.delete()

        # Log the action in the Report table
        Report.objects.create(
            team=team,
            user=user,
            action="player_removed",
            description=f"{user.username} exited the team {team.team_name}."
        )

        # Notify the team owner
        Notifications.objects.create(
            user=team.team_owner,
            message=f"{user.username} has exited the team {team.team_name}.",
            notification_type="team_exit"
        )

        return Response({"message": "You have successfully exited the team."}, status=status.HTTP_200_OK)

    except TeamMembers.DoesNotExist:
        return Response({"message": "You are not currently a member of any team."}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def generate_invite_link(request):
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)
    
    session_token = session_token.split(" ")[1]

    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    
    role_to_be_given_upon_acceptance = request.data.get("role", "member")  # Default role is "member"
    # ensure role is valid
    if role_to_be_given_upon_acceptance not in ["member", "coach", "analyst", "manager"]:
        return Response({"message": "Invalid role specified. Must be 'member' or 'captain'."}, status=400)

    try:
        team = Team.objects.get(team_owner=user)
        invite = Invite.objects.create(
            inviter=user,
            team=team,
            role_to_be_given_upon_acceptance=role_to_be_given_upon_acceptance
        )
        invite_link = f"https://africanfreefirecommunity.com/invite/{invite.invite_id}"
        return Response({"invite_link": invite_link, "invite_id": str(invite.invite_id)}, status=200)
    except Team.DoesNotExist:
        return Response({"message": "You do not own any team."}, status=403)


@api_view(["POST"])
def respond_invite(request, invite_id):
    """
    Accept or decline an invite based on user's choice.
    Expects JSON: {"action": "accept"} or {"action": "decline"}
    """
    session_token = request.headers.get("Authorization")
    if not session_token or not session_token.startswith("Bearer "):
        return Response({"message": "Invalid token"}, status=400)
    
    session_token = session_token.split(" ")[1]
    
    user = validate_token(session_token)
    if not user:
        return Response(
            {"message": "Invalid or expired session token."},
            status=status.HTTP_401_UNAUTHORIZED
        )

    try:
        invite = Invite.objects.get(invite_id=invite_id)
    except (Invite.DoesNotExist, ValidationError, ValueError):
        # DoesNotExist = no such invite; ValidationError/ValueError = the id wasn't even a
        # valid UUID (the route is <str:invite_id>). Either way it's a 404, never a 500.
        return Response({"message": "Invite not found."}, status=404)

    if invite.is_expired():
        return Response({"message": "Invite has expired."}, status=400)

    if invite.status_of_invite == 'attended_to':
        return Response({"message": "Invite already used."}, status=400)

    action = request.data.get("action")
    if action not in ["accept", "decline"]:
        return Response({"message": "Invalid action."}, status=400)

    invite.invitee = user
    invite.status_of_invite = 'attended_to'
    invite.decision = 'accepted' if action == "accept" else 'declined'
    invite.save()

    if action == "accept":
        # Check if user is already in any team
        existing_membership = TeamMembers.objects.filter(member=user).select_related("team").first()
        if existing_membership:
            return Response(
                {"message": f"You are already a member of '{existing_membership.team.team_name}'. Leave that team before joining another."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Ensure the team is not up to 8 players yet
        if TeamMembers.objects.filter(team=invite.team).count() >= 8:
            return Response({'message': 'The team has reached the maximum number of members.'}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure the team is not already at the 6 PLAYING-member cap before adding another
        # player. Two bugs were fixed here (roster-rules, 2026-06-15):
        #   1. Off-by-one: the old guard used `> 6`, which let a 7th player slip in (the
        #      check should reject when the team is ALREADY at the cap, i.e. `>= MAX_PLAYERS`,
        #      because this request is about to add one more).
        #   2. Wrong basis: the old count was `management_role='member'` only, so captains and
        #      vice-captains did not count toward the 6. The cap is over ALL playing roles, so
        #      we now use the shared _playing_member_count (management_role__in=PLAYER_ROLES).
        # The cap applies ONLY when the incoming member is joining as a PLAYING role; a staff
        # join (coach / manager / analyst) never takes a player slot, so it is exempt. The
        # message explains the fix so the inviter/joiner knows to use a staff role instead.
        incoming_role = invite.role_to_be_given_upon_acceptance
        if incoming_role in PLAYER_ROLES and _playing_member_count(invite.team) >= MAX_PLAYERS:
            return Response({
                'message': (
                    f'This team already has the maximum of {MAX_PLAYERS} players. Anyone else must '
                    'join as staff (coach, manager, or analyst), which does not take a '
                    'player slot.'
                )
            }, status=status.HTTP_400_BAD_REQUEST)

        TeamMembers.objects.create(
            team=invite.team,
            member=user,
            management_role=invite.role_to_be_given_upon_acceptance
        )
        return Response({"message": f"You have joined {invite.team.team_name} successfully."})

    else:
        return Response({"message": "You declined the invite."})


@api_view(["GET"])
def get_team_details_based_on_invite(request, invite_id):
    try:
        invite = Invite.objects.get(invite_id=invite_id)
    except (Invite.DoesNotExist, ValidationError, ValueError):
        # DoesNotExist = no such invite; ValidationError/ValueError = the id wasn't even a
        # valid UUID (the route is <str:invite_id>). Either way it's a 404, never a 500.
        return Response({"message": "Invite not found."}, status=404)

    if invite.is_expired():
        return Response({"message": "Invite has expired."}, status=400)

    team = invite.team
    team_data = {
        "team_id": team.team_id,
        "team_name": team.team_name,
        "team_logo": request.build_absolute_uri(team.team_logo.url) if team.team_logo else None,
        "team_tag": team.team_tag,
        "join_settings": team.join_settings,
        "creation_date": team.creation_date,
        "team_creator": team.team_creator.username,
        "team_owner": team.team_owner.username,
        "is_banned": team.is_banned,
        "team_tier": team.team_tier,
        "team_description": team.team_description,
        "country": team.country,
        "inviter": invite.inviter.username,
        "role": invite.role_to_be_given_upon_acceptance
    }

    return Response({"team": team_data}, status=status.HTTP_200_OK)


def _can_manage_roster(user, team):
    """Who may manage a team's roster (change roles + kick members).

    Allowed: the team OWNER, and any member whose management_role is 'coach'.
    NOT allowed: team captains, vice captains, managers, analysts, members.
    The check is bound to THIS team (the team looked up by team_id), so it can
    never authorize managing a different team's roster.
    """
    if team.team_owner_id == user.user_id:
        return True
    return TeamMembers.objects.filter(
        team=team, member=user, management_role="coach"
    ).exists()


def _transfer_window_open():
    """True when player<->staff roster moves are allowed.

    Tied to the active ranking season's transfer window (admins toggle it) — the SAME lock
    that already gates kicks. If there is no active season, there is no lock (treated as open).
    """
    from afc_rankings.models import Season
    active = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
    if not active:
        return True
    return active.is_transfer_window_open()


@api_view(["POST"])
def manage_team_roster(request):
    try:
        # Authorization
        session_token = request.headers.get("Authorization")
        if not session_token or not session_token.startswith("Bearer "):
            return Response({"error": "Authorization token missing or invalid"}, status=400)

        session_token = session_token.split(" ")[1]

        user = validate_token(session_token)
        if not user:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        team_id = request.data.get("team_id")
        updates = request.data.get("updates", [])

        if not team_id or not isinstance(updates, list):
            return Response({"error": "team_id and updates[] are required"}, status=400)

        # Get team
        try:
            team = Team.objects.get(team_id=team_id)
        except Team.DoesNotExist:
            return Response({"error": "Team not found"}, status=404)

        # Roster management is allowed for the team owner OR a coach (see _can_manage_roster).
        if not _can_manage_roster(user, team):
            return Response({"error": "Only the team owner or a coach can manage the roster"}, status=403)

        # ── Transfer-window lock on POSITIONS (in_game_role) ──────────────────────
        # In-game positions (rusher / support / grenader / sniper) can be edited ONLY while the
        # active ranking season's transfer window is OPEN. This is the SAME pattern exit_team uses
        # (load the active Season; if one is active and its window is closed, block), and it
        # mirrors _transfer_window_open() used for the player<->staff move below. The check gates
        # ONLY the in_game_role path: a request that supplies "in_game_role" for any member is a
        # position change, so the whole call is rejected with 403 when the window is closed.
        # Management-role (captain / manager / etc.) changes are NOT newly restricted here — they
        # keep only the crossing rule already enforced per-member below. With no active season the
        # guard does not fire (positions stay editable), matching the existing pattern.
        #
        # IMPORTANT (owner 2026-06-20): only treat it as a position change when the in_game_role VALUE
        # actually DIFFERS from the member's current one. The frontend re-sends every member's existing
        # in_game_role on every save, so the old "key is present" test fired on management-only
        # changes too - which wrongly blocked e.g. an owner changing their own management role while the
        # window was closed. Comparing against the stored value keeps positions locked (their intent)
        # without blocking management-role edits.
        def _is_real_position_change():
            for data in updates:
                if not isinstance(data, dict) or "in_game_role" not in data:
                    continue
                try:
                    _cur = TeamMembers.objects.get(
                        team=team, member_id=data.get("member_id")
                    ).in_game_role or ""
                except TeamMembers.DoesNotExist:
                    continue
                if str(data.get("in_game_role") or "") != str(_cur):
                    return True
            return False

        wants_position_change = _is_real_position_change()
        if wants_position_change:
            from afc_rankings.models import Season
            active_season = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
            if active_season and not active_season.is_transfer_window_open():
                return Response(
                    {"error": "Positions are locked until the transfer window reopens. You can change positions while the transfer window is open."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Valid role sets
        valid_m_roles = [choice[0] for choice in TeamMembers.MANAGEMENT_ROLE_CHOICES]
        valid_i_roles = [choice[0] for choice in TeamMembers.IN_GAME_ROLE_CHOICES]

        results = []
        existing_in_game_count = TeamMembers.objects.filter(
            team=team, in_game_role__isnull=False
        ).exclude(in_game_role="").count()

        MAX_IN_GAME = 6
        ALLOWED_IG_ROLES = {"team_captain", "vice_captain", "member"}

        for data in updates:
            member_id = data.get("member_id")
            new_m_role = data.get("management_role")
            # None means "don’t touch", "" means "clear the role"
            new_i_role = data.get("in_game_role", None)

            try:
                tm = TeamMembers.objects.get(team=team, member_id=member_id)
            except TeamMembers.DoesNotExist:
                results.append({"member_id": member_id, "status": "failed", "reasons": ["Member not in team"]})
                continue

            failures = []

            # ── Management-role change rules ──────────────────────────────────────
            # Role families come from the module-scope PLAYER_ROLES / STAFF_ROLES constants
            # defined at the top of this file (the canonical split): PLAYER roles can hold an
            # in-game slot + count toward the 6-player cap; STAFF roles (coach/manager/analyst)
            # are support-only and cannot be fielded.
            if new_m_role and new_m_role != tm.management_role:
                # Crossing the player<->staff boundary is the abuse-prone move (parking an extra
                # player as "staff" to dodge the 6-player cap, then swapping them back in), so it
                # is gated by the transfer window.
                crossing = (
                    (tm.management_role in PLAYER_ROLES and new_m_role in STAFF_ROLES)
                    or (tm.management_role in STAFF_ROLES and new_m_role in PLAYER_ROLES)
                )
                # 6-PLAYING cap (roster-rules, 2026-06-15): moving a member INTO a playing role
                # is the action that can push a team past 6 players. Reject when the team is
                # already at MAX_PLAYERS playing members and this member is not already one of
                # them (a staff -> player promotion adds a player; a player -> player rename
                # does not, so it is exempt). Basis = _playing_member_count (PLAYER_ROLES), the
                # same cap basis used by the invite-accept path.
                joining_play = new_m_role in PLAYER_ROLES and tm.management_role not in PLAYER_ROLES
                # The team OWNER may change their OWN management role to ANY role (owner request
                # 2026-06-20) - still subject to the crossing/window, 6-player cap, and one-staff-each
                # checks below. Everyone else is still hard-blocked from changing their own role, and a
                # non-owner (e.g. a coach who can manage the roster) still cannot change the owner's role.
                is_owner_self = (tm.member == user) and (tm.member_id == team.team_owner_id)
                if tm.member == user and not is_owner_self:
                    failures.append("You cannot change your own management role")
                elif tm.member_id == team.team_owner_id and not is_owner_self:
                    failures.append("The team owner's management role cannot be changed")
                elif new_m_role not in valid_m_roles:
                    failures.append("Invalid management_role")
                elif crossing and not _transfer_window_open():
                    failures.append("Player/staff role changes are only allowed during the transfer window")
                elif joining_play and _playing_member_count(team) >= MAX_PLAYERS:
                    failures.append(
                        f"A team can have at most {MAX_PLAYERS} players. To add this person as a "
                        "player, free a slot first or keep them as staff (coach, manager, or "
                        "analyst). Staff do not count toward the player limit."
                    )
                elif new_m_role in STAFF_ROLES and TeamMembers.objects.filter(
                    team=team, management_role=new_m_role
                ).exclude(member_id=tm.member_id).exists():
                    # Cap: at most one coach, one manager, one analyst per team.
                    failures.append(f"This team already has a {new_m_role.replace('_', ' ')}")
                else:
                    # Player -> staff: staff are non-players, so drop any in-game role they held
                    # (closes the "park a player as staff, then swap them back" loophole).
                    if new_m_role in STAFF_ROLES and tm.in_game_role:
                        tm.in_game_role = None
                        existing_in_game_count = max(0, existing_in_game_count - 1)
                    tm.management_role = new_m_role

            # In-game role — None means skip, "" means clear
            if new_i_role is not None:
                if new_i_role == "":
                    tm.in_game_role = None
                elif new_i_role not in valid_i_roles:
                    failures.append("Invalid in_game_role")
                elif tm.management_role not in ALLOWED_IG_ROLES:
                    failures.append("Only players (captain, vice captain, member) can have in-game roles")
                elif not tm.in_game_role and existing_in_game_count >= MAX_IN_GAME:
                    failures.append(
                        "A team can field at most 6 players. To add more people, give them a "
                        "staff role (coach, manager, or analyst) instead. Staff do not count "
                        "toward the 6-player limit."
                    )
                else:
                    if not tm.in_game_role:
                        existing_in_game_count += 1
                    tm.in_game_role = new_i_role

            tm.save()

            if failures:
                # Hard blocks (self-role-change / owner-protected) mean nothing was applied for
                # this member -> "failed"; any other mix means some changes landed -> "partial".
                _HARD_BLOCKS = ("You cannot change your own", "The team owner's management role")
                results.append({
                    "member_id": member_id,
                    "username": tm.member.username,
                    "status": "failed" if all(f.startswith(_HARD_BLOCKS) for f in failures) else "partial",
                    "reasons": failures,
                    "management_role": tm.management_role,
                    "in_game_role": tm.in_game_role,
                })
            else:
                results.append({
                    "member_id": member_id,
                    "username": tm.member.username,
                    "status": "success",
                    "management_role": tm.management_role,
                    "in_game_role": tm.in_game_role,
                })
                Report.objects.create(
                    team=team,
                    user=user,
                    action="role_changed",
                    description=f"{tm.member.username}’s roles updated: management_role={tm.management_role}, in_game_role={tm.in_game_role} by {user.username}."
                )

        any_failed = any(r["status"] in ("failed", "partial") for r in results)
        return Response({
            "message": "Roster update completed" if not any_failed else "Some updates could not be applied — see results for details.",
            "results": results,
            "has_errors": any_failed,
        }, status=200)

    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(["POST"])
def kick_team_member(request):
    try:
        # Authorization
        session_token = request.headers.get("Authorization")
        if not session_token or not session_token.startswith("Bearer "):
            return Response({"error": "Authorization token missing or invalid"}, status=400)

        session_token = session_token.split(" ")[1]

        user = validate_token(session_token)
        if not user:
            return Response(
                {"message": "Invalid or expired session token."},
                status=status.HTTP_401_UNAUTHORIZED
            )

        team_id = request.data.get("team_id")
        member_id = request.data.get("member_id")

        if not team_id or not member_id:
            return Response({"error": "team_id and member_id are required"}, status=400)

        # Get team
        try:
            team = Team.objects.get(team_id=team_id)
        except Team.DoesNotExist:
            return Response({"error": "Team not found"}, status=404)

        # Kicking is allowed for the team owner OR a coach (see _can_manage_roster).
        if not _can_manage_roster(user, team):
            return Response({"error": "Only the team owner or a coach can kick members"}, status=403)

        # Roster moves are locked outside the transfer window — members cannot be kicked
        # while the window is CLOSED (matches the player-leave + disband locks). The window
        # is the active ranking season's range (afc_rankings.Season), toggled by admins.
        from afc_rankings.models import Season
        active_season = Season.objects.filter(is_active=True).order_by("-year", "-quarter").first()
        if active_season and not active_season.is_transfer_window_open():
            return Response(
                {"error": "The transfer window is currently closed. Members cannot be kicked until it reopens."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Active-tournament lock (separate from, and additional to, the transfer-window guard
        # above): a captain cannot remove a player while the team is registered for a tournament
        # that has not finished. "Active registration" = a TournamentTeam row for this team whose
        # status is not a removed one (disqualified / withdrawn / left) and whose event is still
        # upcoming or ongoing (not completed). TournamentTeam is imported lazily (same as Season)
        # to avoid an app-level import cycle.
        from afc_tournament_and_scrims.models import TournamentTeam
        has_active_registration = (
            TournamentTeam.objects.filter(team=team)
            .exclude(status__in=["disqualified", "withdrawn", "left"])
            .filter(event__event_status__in=["upcoming", "ongoing"])
            .exists()
        )
        if has_active_registration:
            return Response(
                {"error": "You cannot remove a player while the team is registered for an active tournament."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Get team member to kick
        try:
            tm = TeamMembers.objects.get(team=team, member=member_id)
        except TeamMembers.DoesNotExist:
            return Response({"error": "Member not in team"}, status=404)

        # You cannot kick yourself.
        if tm.member == user:
            return Response({"error": "You cannot kick yourself"}, status=400)

        # The team owner cannot be kicked (a coach must not be able to remove the owner).
        if tm.member_id == team.team_owner_id:
            return Response({"error": "The team owner cannot be kicked."}, status=400)

        kicked_member_username = tm.member.username
        tm.delete()

        # Log the action in the Report table
        Report.objects.create(
            team=team,
            user=user,
            action="player_removed",
            description=f"{kicked_member_username} was kicked from the team {team.team_name} by {user.username}."
        )

        # Notify the kicked member
        Notifications.objects.create(
            user=tm.member,
            message=f"You have been kicked from the team {team.team_name} by {user.username}.",
            notification_type="team_kick"
        )

        return Response({"message": f"Member {kicked_member_username} has been kicked from the team."}, status=200)

    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(["GET"])
def get_number_of_teams(request):
    try:
        total_teams = Team.objects.count()
        return Response({"total_teams": total_teams}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
def join_team(request):
    team_id = request.data.get("team_id")
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

    try:
        team = Team.objects.get(team_id=team_id)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # Check if user is already in a team
    if TeamMembers.objects.filter(member=user).exists():
        return Response({"message": "You are already a member of a team."}, status=status.HTTP_400_BAD_REQUEST)

    # Check join settings
    if team.join_settings == "by_request":
        return Response({"message": "This team requires a join request. Please send a join request instead."}, status=status.HTTP_403_FORBIDDEN)

    # Add user to team
    TeamMembers.objects.create(team=team, member=user, management_role='member')

    # Log the action in the Report table
    Report.objects.create(
        team=team,
        user=user,
        action="player_joined",
        description=f"{user.username} joined the team {team.team_name}."
    )

    # Notify the team owner
    Notifications.objects.create(
        user=team.team_owner,
        message=f"{user.username} has joined your team {team.team_name}.",
        notification_type="team_join"
    )

    return Response({"message": f"You have successfully joined the team {team.team_name}."}, status=status.HTTP_200_OK)



@api_view(["GET"])
def get_total_teams_count(request):
    try:
        total_teams = Team.objects.count()
        return Response({"total_teams": total_teams}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["GET"])
def get_current_active_teams_count(request):
    try:
        active_teams = Team.objects.filter(is_banned=False).count()
        return Response({"active_teams": active_teams}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["GET"])
def get_banned_teams_count(request):
    try:
        banned_teams = Team.objects.filter(is_banned=True).count()
        return Response({"banned_teams": banned_teams}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["GET"])
def get_new_teams_count(request):
    try:
        seven_days_ago = timezone.now() - timedelta(days=7)
        new_teams = Team.objects.filter(creation_date__gte=seven_days_ago).count()
        return Response({"new_teams_last_7_days": new_teams}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["GET"])
def get_average_members_per_team(request):
    try:
        total_teams = Team.objects.count()
        if total_teams == 0:
            average_members = 0
        else:
            total_members = TeamMembers.objects.count()
            average_members = total_members / total_teams

        return Response({"average_members_per_team": average_members}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["GET"])
def get_team_with_highest_wins(request):
    top = (
        TournamentTeamMatchStats.objects
        .filter(placement=1)
        .values(team_id=F("tournament_team__team__team_id"),
                team_name=F("tournament_team__team__team_name"))
        # Count by the model's real PK "team_stats_id" — TournamentTeamMatchStats has no auto "id" field (explicit AutoField PK), matching Count("team_stats_id") used elsewhere in this file
        .annotate(total_wins=Count("team_stats_id"))
        .order_by("-total_wins", "team_name")
        .first()
    )

    if not top:
        return Response({"message": "No team win records found."}, status=status.HTTP_404_NOT_FOUND)

    return Response({"team_with_highest_wins": top}, status=status.HTTP_200_OK)

    

@api_view(["GET"])
def get_top_earning_teams(request):
    qs = Team.objects.order_by("-total_earnings")[:5]
    data = [{"team_id": t.team_id, "team_name": t.team_name, "total_earnings": t.total_earnings} for t in qs]
    return Response({"top_earning_teams": data}, status=status.HTTP_200_OK)


def _is_admin(user):
    """Return True if the user is a full admin or has head_admin/teams_admin role."""
    if user.role == "admin":
        return True
    return UserRoles.objects.filter(
        user=user,
        role__role_name__in=["head_admin", "teams_admin"]
    ).exists()


def _get_authed_user(request):
    """Validate Bearer token and return (user, error_response)."""
    session_token = request.headers.get("Authorization", "")
    if not session_token or not session_token.startswith("Bearer "):
        return None, Response({"message": "Authorization header is required."}, status=status.HTTP_400_BAD_REQUEST)
    user = validate_token(session_token.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)
    return user, None


@api_view(["GET"])
def admin_search_players(request):
    admin, err = _get_authed_user(request)
    if err:
        return err
    if not _is_admin(admin):
        return Response({"message": "Admin access required."}, status=status.HTTP_403_FORBIDDEN)

    query = request.query_params.get("q", "").strip()
    if len(query) < 2:
        return Response({"players": []}, status=status.HTTP_200_OK)

    matched = User.objects.filter(
        Q(username__icontains=query) | Q(email__icontains=query)
    )[:10]

    results = []
    for u in matched:
        membership = TeamMembers.objects.filter(member=u).select_related("team").first()
        results.append({
            "user_id": u.user_id,
            "username": u.username,
            "email": u.email,
            "current_team": {
                "team_id": membership.team.team_id,
                "team_name": membership.team.team_name,
            } if membership else None,
        })

    return Response({"players": results}, status=status.HTTP_200_OK)


@api_view(["POST"])
def admin_remove_member(request):
    admin, err = _get_authed_user(request)
    if err:
        return err
    if not _is_admin(admin):
        return Response({"message": "Admin access required."}, status=status.HTTP_403_FORBIDDEN)

    team_id = request.data.get("team_id")
    member_id = request.data.get("member_id")

    if not team_id or not member_id:
        return Response({"message": "team_id and member_id are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        team = Team.objects.get(team_id=team_id)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        tm = TeamMembers.objects.get(team=team, member_id=member_id)
    except TeamMembers.DoesNotExist:
        return Response({"message": "Member not found in this team."}, status=status.HTTP_404_NOT_FOUND)

    if team.team_owner_id == tm.member_id:
        return Response({"message": "Cannot remove the team owner."}, status=status.HTTP_400_BAD_REQUEST)

    removed_member = tm.member
    removed_username = removed_member.username
    tm.delete()

    Report.objects.create(
        team=team,
        user=admin,
        action="player_removed",
        description=f"{removed_username} was removed from {team.team_name} by admin {admin.username}."
    )
    Notifications.objects.create(
        user=removed_member,
        message=f"You have been removed from the team '{team.team_name}' by an admin.",
        notification_type="team_kick"
    )
    Notifications.objects.create(
        user=team.team_owner,
        message=f"{removed_username} was removed from your team '{team.team_name}' by an admin.",
        notification_type="team_kick"
    )

    return Response({"message": f"{removed_username} has been removed from {team.team_name}."}, status=status.HTTP_200_OK)


@api_view(["POST"])
def admin_add_member(request):
    admin, err = _get_authed_user(request)
    if err:
        return err
    if not _is_admin(admin):
        return Response({"message": "Admin access required."}, status=status.HTTP_403_FORBIDDEN)

    team_id = request.data.get("team_id")
    player_id = request.data.get("player_id")
    management_role = request.data.get("management_role", "member")
    force_move = request.data.get("force_move", False)
    override_limit = request.data.get("override_limit", False)

    if not team_id or not player_id:
        return Response({"message": "team_id and player_id are required."}, status=status.HTTP_400_BAD_REQUEST)

    valid_roles = [choice[0] for choice in TeamMembers.MANAGEMENT_ROLE_CHOICES]
    if management_role not in valid_roles:
        return Response({"message": f"Invalid role. Must be one of: {', '.join(valid_roles)}."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        team = Team.objects.get(team_id=team_id)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        player = User.objects.get(user_id=player_id)
    except User.DoesNotExist:
        return Response({"message": "Player not found."}, status=status.HTTP_404_NOT_FOUND)

    if TeamMembers.objects.filter(team=team, member=player).exists():
        return Response({"message": "Player is already a member of this team."}, status=status.HTTP_400_BAD_REQUEST)

    existing = TeamMembers.objects.filter(member=player).select_related("team").first()
    if existing:
        if not force_move:
            return Response({
                "message": f"Player is currently on team '{existing.team.team_name}'.",
                "error_code": "player_on_team",
                "current_team": existing.team.team_name,
            }, status=status.HTTP_400_BAD_REQUEST)
        Report.objects.create(
            team=existing.team,
            user=admin,
            action="player_removed",
            description=f"{player.username} was force-moved from '{existing.team.team_name}' to '{team.team_name}' by admin {admin.username}."
        )
        Notifications.objects.create(
            user=existing.team.team_owner,
            message=f"{player.username} was removed from your team '{existing.team.team_name}' by an admin.",
            notification_type="team_kick"
        )
        existing.delete()

    current_count = TeamMembers.objects.filter(team=team).count()
    if current_count >= 8 and not override_limit:
        return Response({
            "message": f"Team already has {current_count} members (above the 8-member limit).",
            "error_code": "team_full",
            "current_count": current_count,
        }, status=status.HTTP_400_BAD_REQUEST)

    TeamMembers.objects.create(team=team, member=player, management_role=management_role)

    Report.objects.create(
        team=team,
        user=admin,
        action="player_joined",
        description=f"{player.username} was added to '{team.team_name}' as {management_role} by admin {admin.username}."
    )
    Notifications.objects.create(
        user=player,
        message=f"You have been added to the team '{team.team_name}' by an admin.",
        notification_type="team_join"
    )
    Notifications.objects.create(
        user=team.team_owner,
        message=f"{player.username} has been added to your team '{team.team_name}' by an admin.",
        notification_type="team_join"
    )


# ── Admin Team Management ──────────────────────────────────────────────────────

def _require_team_admin(request):
    """Validate Bearer token and require admin/moderator/support role. Returns (user, err)."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None, Response({"message": "Invalid or missing Authorization token."}, status=400)
    user = validate_token(auth.split(" ")[1])
    if not user:
        return None, Response({"message": "Invalid or expired session token."}, status=401)
    if user.role not in ["admin", "moderator", "support"]:
        return None, Response({"message": "You do not have permission to perform this action."}, status=403)
    return user, None


@api_view(["GET"])
def admin_get_team_event_history(request):
    user, err = _require_team_admin(request)
    if err:
        return err

    team_id = request.query_params.get("team_id")
    if not team_id:
        return Response({"message": "team_id is required."}, status=400)

    try:
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(50, max(1, int(request.query_params.get("page_size", 10))))
    except ValueError:
        return Response({"message": "page and page_size must be integers."}, status=400)

    team = get_object_or_404(Team, team_id=team_id)

    qs = TournamentTeam.objects.filter(team=team).select_related("event").order_by("-registration_date")
    total = qs.count()
    offset = (page - 1) * page_size
    entries = qs[offset : offset + page_size]

    results = [
        {
            "event_id": tt.event.event_id,
            "event_name": tt.event.event_name,
            "competition_type": tt.event.competition_type,
            "event_status": tt.event.event_status,
            "team_status": tt.status,
            "registration_date": tt.registration_date,
            "is_waitlisted": tt.is_waitlisted,
        }
        for tt in entries
    ]

    return Response(
        {
            "results": results,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        },
        status=200,
    )


@api_view(["POST"])
def admin_change_team_tier(request):
    user, err = _require_team_admin(request)
    if err:
        return err

    team_id = request.data.get("team_id")
    tier = str(request.data.get("tier", "")).strip()

    if not team_id:
        return Response({"message": "team_id is required."}, status=400)
    if tier not in ["1", "2", "3"]:
        return Response({"message": "tier must be 1, 2, or 3."}, status=400)

    team = get_object_or_404(Team, team_id=team_id)
    old_tier = team.team_tier
    team.team_tier = tier
    team.save(update_fields=["team_tier"])

    AdminHistory.objects.create(
        admin_user=user,
        action="change_team_tier",
        description=f"Changed tier of '{team.team_name}' (ID: {team.team_id}) from {old_tier} to {tier}.",
    )
    Notifications.objects.create(
        user=team.team_owner,
        message=f"Your team '{team.team_name}' has been moved to Tier {tier} by an admin.",
        notification_type="team_update",
    )

    return Response(
        {"message": f"Team tier updated from {old_tier} to {tier}.", "new_tier": tier},
        status=200,
    )


@api_view(["POST"])
def admin_transfer_team_ownership(request):
    user, err = _require_team_admin(request)
    if err:
        return err

    team_id = request.data.get("team_id")
    new_owner_id = request.data.get("new_owner_id")

    if not team_id or not new_owner_id:
        return Response({"message": "team_id and new_owner_id are required."}, status=400)

    team = get_object_or_404(Team, team_id=team_id)
    new_owner = get_object_or_404(User, user_id=new_owner_id)
    old_owner = team.team_owner

    if new_owner == old_owner:
        return Response({"message": "New owner is already the team owner."}, status=400)

    if not TeamMembers.objects.filter(team=team, member=new_owner).exists():
        return Response({"message": "New owner must be a current team member."}, status=400)

    # Update management roles
    TeamMembers.objects.filter(team=team, member=old_owner).update(management_role="member")
    TeamMembers.objects.filter(team=team, member=new_owner).update(management_role="team_captain")

    team.team_owner = new_owner
    team.save(update_fields=["team_owner"])

    Report.objects.create(
        team=team,
        user=new_owner,
        action="role_assigned",
        description=f"Admin {user.username} transferred ownership from {old_owner.username} to {new_owner.username}.",
    )
    AdminHistory.objects.create(
        admin_user=user,
        action="transfer_team_ownership",
        description=f"Transferred ownership of '{team.team_name}' from {old_owner.username} to {new_owner.username}.",
    )
    Notifications.objects.create(
        user=new_owner,
        message=f"You are now the owner of '{team.team_name}'. Ownership was transferred by an admin.",
        notification_type="team_update",
    )
    Notifications.objects.create(
        user=old_owner,
        message=f"Ownership of '{team.team_name}' has been transferred to {new_owner.username} by an admin.",
        notification_type="team_update",
    )

    return Response(
        {"message": f"Ownership transferred to {new_owner.username} successfully."},
        status=200,
    )

    return Response({"message": f"{player.username} has been added to {team.team_name} as {management_role}."}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def search_teams(request):
    """
    GET /team/search-teams/?q=<text>&limit=10 - typeahead lookup of EXISTING teams.

    Powers the reusable <TeamSearchSelect/> typeahead (frontend components/ui/team-search-select.tsx),
    the team-format counterpart of <UserSearchSelect/>. It is consumed by the Standalone Leaderboards
    wizard (Participants step) wherever a human picks an existing team to add as a participant.
    Mirrors afc_auth.views.search_users: Bearer SessionToken auth, q >= 2 chars (so it can never dump
    the whole table), icontains match, limit capped at 25 (default 10).

    Auth: any logged-in user (Bearer SessionToken).
    Match: team_name icontains q (and team_tag icontains q, since the tag is the short handle).
    Response 200: { results: [ {team_id, team_name, team_tag, country} ], total_count }.
    q < 2 chars -> { results: [], total_count: 0 }.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return Response({"message": "Invalid or missing Authorization token."}, status=status.HTTP_400_BAD_REQUEST)
    requester = validate_token(auth.split(" ", 1)[1])
    if not requester:
        return Response({"message": "Invalid or expired session token."}, status=status.HTTP_401_UNAUTHORIZED)

    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        # Require at least 2 characters so the endpoint never returns the whole team table.
        return Response({"results": [], "total_count": 0}, status=status.HTTP_200_OK)

    try:
        limit = min(max(int(request.GET.get("limit", 10)), 1), 25)
    except (TypeError, ValueError):
        limit = 10

    # Match by team_name (the full name) OR team_tag (the short handle), case-insensitive.
    # ALSO match punctuation-insensitively so "ve" finds the team literally named "V-E": we strip the
    # common separators (-, _, ., space, ...) from both the column (normalized_column) and the query
    # (separator_stripped) and compare. OR-ing keeps every existing icontains match intact (this only
    # widens results). Shared with the in-browser filters via frontend/lib/search.ts.
    from utils.search_utils import normalized_column, separator_stripped

    cond = Q(team_name__icontains=q) | Q(team_tag__icontains=q)
    norm_q = separator_stripped(q)
    qs = Team.objects.annotate(
        _norm_team_name=normalized_column("team_name"),
        _norm_team_tag=normalized_column("team_tag"),
    )
    if norm_q:
        cond |= Q(_norm_team_name__icontains=norm_q) | Q(_norm_team_tag__icontains=norm_q)
    qs = qs.filter(cond).order_by("team_name")
    total = qs.count()

    results = [
        {
            "team_id": t.team_id,
            "team_name": t.team_name,
            "team_tag": t.team_tag,
            "country": t.country,
        }
        for t in qs[:limit]
    ]
    return Response({"results": results, "total_count": total}, status=status.HTTP_200_OK)
