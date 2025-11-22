import uuid
from django.utils.timezone import now
from django.db import models
from afc_auth.models import *
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from imports import User

# Create your models here.

class Team(models.Model):
    JOIN_SETTINGS_CHOICES = [
        ('open', 'Open'),
        ('by_request', 'By Request')
    ]
    team_id = models.AutoField(primary_key=True)
    team_name = models.CharField(unique=True, max_length=60)
    team_logo = models.ImageField(upload_to='teams_logos/', null=True, blank=True)
    team_tag = models.CharField(max_length=5, null=True)
    join_settings = models.CharField(max_length=20, choices=JOIN_SETTINGS_CHOICES)
    creation_date = models.DateTimeField(default=now)
    team_creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_teams')
    team_owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_teams')
    is_banned = models.BooleanField(default=False)
    team_tier = models.CharField(max_length=1, default="3")
    team_description = models.CharField(max_length=200, default="We Love Playing Free Fire")
    country = models.CharField(max_length=20)

    def __str__(self):
        return self.team_name
    

class TeamSocialMediaLinks(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    platform = models.CharField(max_length=20)
    link = models.URLField(max_length=200)
    


class TeamMembers(models.Model):
    MANAGEMENT_ROLE_CHOICES = [
        ('team_owner', 'Team Owner'),
        ('team_captain', 'Team Captain'),
        ('vice_captain', 'Vice Captain'),
        ('member', 'Member'),
        ('coach', 'Coach'),
        ('manager', 'Manager'),
        ('analyst', 'Analyst'),
    ]

    IN_GAME_ROLE_CHOICES = [
        ('rusher', 'Rusher'),
        ('support', 'Support'),
        ('grenader', 'Grenader'),
        ('sniper', 'Sniper')
    ]

    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    member = models.ForeignKey(User, on_delete=models.CASCADE)
    management_role = models.CharField(max_length=20, choices=MANAGEMENT_ROLE_CHOICES, default='member')
    in_game_role = models.CharField(max_length=20, choices=IN_GAME_ROLE_CHOICES, null=True, blank=True)
    join_date = models.DateTimeField(default=now)


    def __str__(self):
        return f"{self.member.username} - {self.team.team_name} ({self.role})"


# class JoinRequests(models.Model):
#     DECISION_CHOICES = [
#         ('approved', 'Appproved'),
#         ('denied', 'Denied'),
#     ]

#     STATUS_CHOICES = [
#         ('unattended_to', 'Unattended To'),
#         ('attended_to', 'Attended To')
#     ]

#     user = models.ForeignKey(User, on_delete=models.CASCADE)
#     team = models.ForeignKey(Team, on_delete=models.CASCADE)
#     status_of_request = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unattended_to')
#     decision = models.CharField(max_length=20, choices=DECISION_CHOICES, null=True, blank=True)
#     created_at = models.DateTimeField(auto_now_add=True)


class Invite(models.Model):
    STATUS_CHOICES = [
        ('unattended_to', 'Unattended To'),
        ('attended_to', 'Attended To'),
    ]
    DECISION_CHOICES = [
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
    ]

    invite_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inviter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_invites')
    invitee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_invites', null=True, blank=True)
    team = models.ForeignKey('Team', on_delete=models.CASCADE)
    status_of_invite = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unattended_to')
    decision = models.CharField(max_length=20, choices=DECISION_CHOICES, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()  # When the invite expires

    def save(self, *args, **kwargs):
        # Default expiration: 7 days from creation
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*args, **kwargs)

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"Invite: {self.inviter.username} -> {self.invitee.username if self.invitee else 'Pending'} ({self.team.team_name})"


class Report(models.Model):
    ACTION_CHOICES = [
        ("team_created", "Team Created"),
        ("player_left", "Player Left Team"),
        ("player_joined", "Player Joined Team"),
        ("player_removed", "Player Removed From Team"),
        ("role_assigned", "Role Assigned to Player"),
        ("role_changed", "Player Role Changed"),
        ("team_disbanded", "Team Disbanded"),
        ("team_name_changed", "Team Name Changed"),
        ("team_banned", "Team Banned"),
        ("player_banned", "Player Banned")
    ]

    report_id = models.AutoField(primary_key=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.team.name} - {self.get_action_display()} on {self.created_at}"


class JoinRequest(models.Model):
    request_id = models.AutoField(primary_key=True)
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_request')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='received_request')
    status_of_request = models.CharField(max_length=20, choices=[
        ('unattended_to', 'Unattended To'),
        ('attended_to', 'Attended To')
    ], default='unattended_to')
    decision = models.CharField(max_length=20, choices=[
        ('approved', 'Approved'),
        ('denied', 'Denied')
    ], null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    message = models.CharField(max_length=150, null=True, blank=True)

    def __str__(self):
        return f"Join Request: {self.requester.username} -> ({self.team.team_name})"
