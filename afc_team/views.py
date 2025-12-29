from datetime import timedelta, timedelta
from django.utils import timezone
import uuid
from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from afc_auth.views import validate_token
from afc_leaderboard_calc import models
from afc_leaderboard_calc.models import Match, MatchLeaderboard, Tournament
from .models import Team, TeamMembers, Invite, Report, JoinRequest, TeamSocialMediaLinks
from afc_auth.models import Notifications, User, UserProfile
from django.utils.timezone import now
from django.db.models import Q
from .models import Team, TeamMembers, Invite, TeamSocialMediaLinks
import json

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
    country = request.data.get("country")
    join_settings = request.data.get("join_settings", "by_request")
    list_of_players_to_invite = request.data.getlist("list_of_players_to_invite", [])
    team_social_media_links = request.data.get("team_social_media_links", [])
    if team_social_media_links:
        team_social_media_links = json.loads(team_social_media_links)

    # Validate required fields
    if not team_name or not country:
        return Response({"message": "Team name and country are required."}, status=status.HTTP_400_BAD_REQUEST)

    if join_settings not in ["open", "by_request"]:
        return Response({"message": "Invalid join settings."}, status=status.HTTP_400_BAD_REQUEST)

    # Check for existing team name
    if Team.objects.filter(team_name=team_name).exists():
        return Response({"message": "Team name already exists."}, status=status.HTTP_400_BAD_REQUEST)

    # Create the team
    team = Team.objects.create(
        team_name=team_name,
        team_logo=team_logo,
        team_creator=user,
        team_owner=user,
        team_description=team_description,
        country=country,
        join_settings=join_settings
    )

    # Add team creator as owner
    TeamMembers.objects.create(team=team, member=user, management_role='team_owner', in_game_role='rusher')

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
            notification_type="team_invitation"
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
def rank_teams_into_tiers(request):
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

    # Fetch all teams and calculate points
    team_points = {}
    all_tournaments = Tournament.objects.all()

    for tournament in all_tournaments:
        for match in Match.objects.filter(tournament=tournament):
            for leaderboard_entry in MatchLeaderboard.objects.filter(match=match):
                team = leaderboard_entry.team
                points = team_points.get(team, 0)

                # Tournament win points
                if tournament.tournament_type == "tournament" and leaderboard_entry.position_in_match == 1:
                    points += 10

                # Calculate total kills in the tournament
                total_kills = MatchLeaderboard.objects.filter(match__tournament=tournament, team=team).aggregate(total_kills=models.Sum('kills'))['total_kills'] or 0

                # Kill points for tournaments
                if tournament.tournament_type == "tournament":
                    if 10 <= total_kills < 20:
                        points += 1
                    elif 20 <= total_kills < 40:
                        points += 2
                    elif 40 <= total_kills < 60:
                        points += 3
                    elif 60 <= total_kills < 80:
                        points += 4
                    elif 80 <= total_kills < 100:
                        points += 5
                    elif 100 <= total_kills < 120:
                        points += 6
                    elif 120 <= total_kills < 140:
                        points += 7
                    elif 140 <= total_kills < 170:
                        points += 8
                    elif 170 <= total_kills < 190:
                        points += 9

                # Kill points for scrims
                elif tournament.tournament_type == "scrims":
                    if 10 <= leaderboard_entry.kills < 20:
                        points += 0.5
                    elif 20 <= leaderboard_entry.kills < 40:
                        points += 1
                    elif 41 <= leaderboard_entry.kills < 60:
                        points += 1.5
                    elif 61 <= leaderboard_entry.kills < 80:
                        points += 2
                    elif 81 <= leaderboard_entry.kills <= 100:
                        points += 3

                # Placement points
                if leaderboard_entry.position_in_match == 1:
                    points += 3
                elif leaderboard_entry.position_in_match == 2:
                    points += 2
                elif leaderboard_entry.position_in_match == 3:
                    points += 1

                team_points[team] = points

    # Rank teams into tiers
    team_tiers = []
    for team, points in team_points.items():
        if points >= 70:
            tier = "Tier 1"
        elif 50 <= points < 70:
            tier = "Tier 2"
        else:
            tier = "Tier 3"

        team_tiers.append({
            "team_name": team.name,
            "points": points,
            "tier": tier
        })

    return Response({"team_tiers": team_tiers}, status=200)


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
        new_owner_member.management_role = "team_owner"
        new_owner_member.save()

        # Transfer ownership
        team.team_owner = new_owner
        team.save()

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
        reviewer = User.objects.get(session_token=session_token)

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
        # Find the team where the user is the owner
        team = Team.objects.get(team_owner=user)

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
        return Response({"message": "You do not own any team."}, status=status.HTTP_403_FORBIDDEN)
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

    return Response({"message": "Team details updated successfully."}, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_all_teams(request):
    teams = Team.objects.all()
    teams_data = []

    for team in teams:
        teams_data.append({
            "team_id": team.team_id,
            "team_name": team.team_name,
            "team_logo": team.team_logo.url if team.team_logo else None,
            "team_tag": team.team_tag,
            "join_settings": team.join_settings,
            "creation_date": team.creation_date,
            "team_creator": team.team_creator.username,
            "team_owner": team.team_owner.username,
            "is_banned": team.is_banned,
            "team_tier": team.team_tier,
            "team_description": team.team_description,
            "country": team.country,
            "member_count": TeamMembers.objects.filter(team=team).count()
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


@api_view(["POST"])
def get_team_details(request):
    team_name = request.data.get("team_name")

    if not team_name:
        return Response({"message": "Team name is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        team = Team.objects.get(team_name=team_name)
    except Team.DoesNotExist:
        return Response({"message": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

    # Team members
    members_qs = TeamMembers.objects.filter(team=team).select_related("member")
    members_data = [
        {
            "id": member.member.user_id,
            "uid": member.member.uid,
            "username": member.member.username,
            "management_role": member.management_role,
            "in_game_role": member.in_game_role,
            "join_date": member.join_date,
        }
        for member in members_qs
    ]

    # Social media links
    social_links_qs = TeamSocialMediaLinks.objects.filter(team=team)
    social_links = [
        {
            "platform": link.platform,
            "link": link.link,
        }
        for link in social_links_qs
    ]

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
        "total_members": members_qs.count(),
        "members": members_data,
        "social_media_links": social_links,
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

        # Prevent the team owner from exiting the team
        if team.team_owner == user:
            return Response({"message": "Team owners cannot exit their own team. Please transfer ownership or disband the team."}, status=status.HTTP_403_FORBIDDEN)

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

    try:
        team = Team.objects.get(team_owner=user)
        invite = Invite.objects.create(
            inviter=user,
            team=team
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
    except Invite.DoesNotExist:
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
        # Only add if not already in the team
        if not TeamMembers.objects.filter(team=invite.team, member=user).exists():
            TeamMembers.objects.create(
                team=invite.team,
                member=user,
                management_role='member'
            )
        else:
            return Response({"message": "You are already a member of this team."}, status=400)
        return Response({"message": f"You have joined {invite.team.team_name} successfully."})

    else:
        return Response({"message": "You declined the invite."})


@api_view(["GET"])
def get_team_details_based_on_invite(request, invite_id):
    try:
        invite = Invite.objects.get(invite_id=invite_id)
    except Invite.DoesNotExist:
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
    }

    return Response({"team": team_data}, status=status.HTTP_200_OK)


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

        # Only team owner allowed
        if team.team_owner != user:
            return Response({"error": "Only the team owner can manage the roster"}, status=403)

        # Valid role sets
        valid_m_roles = [choice[0] for choice in TeamMembers.MANAGEMENT_ROLE_CHOICES]
        valid_i_roles = [choice[0] for choice in TeamMembers.IN_GAME_ROLE_CHOICES]

        results = []

        for data in updates:
            member_id = data.get("member_id")
            new_m_role = data.get("management_role")
            new_i_role = data.get("in_game_role")

            try:
                tm = TeamMembers.objects.get(team=team, member_id=member_id)
            except TeamMembers.DoesNotExist:
                results.append({
                    "member_id": member_id,
                    "status": "failed",
                    "reason": "Member not in team"
                })
                continue

            # Prevent owner from demoting themselves unless allowed
            if tm.member == user and new_m_role and new_m_role != "team_owner":
                results.append({
                    "member_id": member_id,
                    "status": "failed",
                    "reason": "Owner cannot change their own management role"
                })
                continue

            # Validate management role
            if new_m_role:
                if new_m_role not in valid_m_roles:
                    results.append({
                        "member_id": member_id,
                        "status": "failed",
                        "reason": "Invalid management_role"
                    })
                    continue
                tm.management_role = new_m_role

            # Validate in-game role
            if new_i_role:
                if new_i_role not in valid_i_roles:
                    results.append({
                        "member_id": member_id,
                        "status": "failed",
                        "reason": "Invalid in_game_role"
                    })
                    continue
                tm.in_game_role = new_i_role

            tm.save()

            results.append({
                "member_id": member_id,
                "status": "success",
                "management_role": tm.management_role,
                "in_game_role": tm.in_game_role
            })


            # Log the action in the Report table
            Report.objects.create(
                team=team,
                user=user,
                action="roster_updated",
                description=f"{tm.member.username}'s roles updated to management_role: {tm.management_role}, in_game_role: {tm.in_game_role} by {user.username}."
            )

        return Response({
            "message": "Bulk roster update completed",
            "results": results
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

        # Only team owner allowed
        if team.team_owner != user:
            return Response({"error": "Only the team owner can kick members"}, status=403)

        # Get team member to kick
        try:
            tm = TeamMembers.objects.get(team=team, member=member_id)
        except TeamMembers.DoesNotExist:
            return Response({"error": "Member not in team"}, status=404)

        # Prevent owner from kicking themselves
        if tm.member == user:
            return Response({"error": "Owner cannot kick themselves"}, status=400)

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
    try:
        team_with_most_wins = Team.objects.order_by('-total_wins').first()
        if not team_with_most_wins:
            return Response({"message": "No teams found."}, status=status.HTTP_404_NOT_FOUND)

        team_data = {
            "team_id": team_with_most_wins.team_id,
            "team_name": team_with_most_wins.team_name,
            "total_wins": team_with_most_wins.total_wins
        }

        return Response({"team_with_highest_wins": team_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(["GET"])
def get_top_earning_teams(request):
    try:
        top_earning_teams = Team.objects.order_by('-total_earnings')[:5]
        teams_data = []

        for team in top_earning_teams:
            teams_data.append({
                "team_id": team.team_id,
                "team_name": team.team_name,
                "total_earnings": team.total_earnings
            })

        return Response({"top_earning_teams": teams_data}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"message": "An error occurred.", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)