from datetime import timedelta, timezone
import uuid
from django.utils.timezone import now
from django.db import models
from afc_auth.models import *
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
# from imports import User
from django.conf import settings

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
    team_creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='created_teams')
    team_owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='owned_teams')
    is_banned = models.BooleanField(default=False)
    team_tier = models.CharField(max_length=1, default="3")
    team_description = models.CharField(max_length=200, default="We Love Playing Free Fire")
    # Auto-derived from the LOCATION of the team's PLAYING members (owner 2026-06-20): the most-common
    # player country wins; a tie for first falls back to the team owner's country. Recomputed on every
    # roster change via the TeamMembers signal (afc_team/signals.py) + recompute_team_country() in views.
    # Widened from 20 -> 64 because some country names exceed 20 chars (e.g. "Democratic Republic of the
    # Congo"). Stored as a human-readable name. blank=True so a team with no resolvable country is valid.
    country = models.CharField(max_length=64, blank=True)
    total_earnings = models.DecimalField(max_digits=15, decimal_places=2, default=0.0, null=True, blank=True)
    team_captain = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='captained_teams')

    # Team-level STATS PRIVACY opt-in (owner 2026-06-27). Companion to User.stats_visible (afc_auth):
    # that flag governs an individual player's stats; THIS flag governs the TEAM's aggregate stats
    # (the team-profile Statistics tab) for outside viewers. DEFAULT FALSE = hidden. Only the team
    # OWNER or a MANAGER may flip it (the user controls who can open it up), so a lone roster member
    # can't expose the whole team. AFC admins (is_stats_admin) and the team's own current members
    # always see team stats regardless of this flag; it only opens the stats to OUTSIDERS.
    #   - Read by  : afc_team.views._can_view_team_stats (gate for team-stats visibility) and
    #                get_team_details (returned as team.stats_visible so the FE settings switch +
    #                the public TeamStatisticsTab gate reflect it).
    #   - Written by: afc_team.views.edit_team (the "Show team stats publicly" switch, owner/manager only).
    stats_visible = models.BooleanField(default=False)

    # MANUAL letter-avatar extras for the team (Letter Avatars feature, owner 2026-06-29).
    # Free Fire ships a fixed set of 26 "letter avatars" (one per A-Z). A team's USABLE letters
    # are LIVE-DERIVED, never stored: union(every current member's afc_auth.User.letter_avatars)
    # ∪ THIS field. This field holds ONLY the manual EXTRAS a team manager declares by hand (letters
    # the team can field that no current member's own letter avatars already cover). Mirrors how
    # Team.total_earnings / the team's available letters are computed live rather than persisted, so
    # the team's available set self-corrects whenever a member joins, leaves, or edits their letters.
    # Stored canonical form: sorted, de-duplicated, UPPERCASE single chars, e.g. ["B","Q","Z"].
    #   - Read by  : afc_team.views.get_team_details (folded into the live `available_letters` union +
    #                returned raw as `manual_letters`) and afc_team.views._team_available_letters.
    #   - Written by: afc_team.views.set_team_letters (POST /team/set-team-letters/), gated by
    #                _can_manage_team_letters (owner + captain/vice-captain/manager/coach).
    # Default empty list (no backfill needed); blank=True so an empty list is a valid value.
    manual_letter_avatars = models.JSONField(default=list, blank=True)

    def save(self, *args, **kwargs):
        # Trim-on-save for the name fields (owner 2026-06-20). Seed data had stray
        # leading/trailing whitespace in team names (~41% of teams), e.g. 'FROZEN EMPIRE ',
        # which breaks name-based lookups (SQL `=` ignores only trailing spaces; LIKE/
        # __iexact ignores neither). Stripping here keeps new + edited teams clean; the
        # clean_name_whitespace management command backfills existing rows.
        if isinstance(self.team_name, str):
            self.team_name = self.team_name.strip()
        if isinstance(self.team_tag, str):
            self.team_tag = self.team_tag.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.team_name
    

class TeamSocialMediaLinks(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="social_links")
    platform = models.CharField(max_length=20)
    link = models.URLField(max_length=200)
    # rankings §7.3 — verified follower count snapshot inputs.
    # Verified by admin via afc_rankings.admin_social; snapshotted per-season into
    # TeamSocialSnapshot by aggregation for the social_media_pts component of the team
    # quarterly score.
    follower_count = models.PositiveIntegerField(null=True, blank=True)
    followers_verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="verified_social_counts",
    )
    


class TeamMembers(models.Model):
    MANAGEMENT_ROLE_CHOICES = [
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

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    management_role = models.CharField(max_length=20, choices=MANAGEMENT_ROLE_CHOICES, default='member')
    in_game_role = models.CharField(max_length=20, choices=IN_GAME_ROLE_CHOICES, null=True, blank=True)
    join_date = models.DateTimeField(default=now)


    class Meta:
        unique_together = ('team', 'member')
        constraints = [
            models.UniqueConstraint(fields=['member'], name='unique_member_one_team'),
        ]

    def __str__(self):
        return f"{self.member.username} - {self.team.team_name} ({self.management_role})"


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
    inviter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_invites')
    invitee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='received_invites', null=True, blank=True)
    team = models.ForeignKey('Team', on_delete=models.CASCADE)
    status_of_invite = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unattended_to')
    role_to_be_given_upon_acceptance = models.CharField(max_length=20, choices=TeamMembers.MANAGEMENT_ROLE_CHOICES, default='member')
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
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.team.name} - {self.get_action_display()} on {self.created_at}"


class JoinRequest(models.Model):
    request_id = models.AutoField(primary_key=True)
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_request')
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
