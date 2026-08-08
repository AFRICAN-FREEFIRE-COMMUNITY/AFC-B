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
    # rankings §7.3 - verified follower count snapshot inputs.
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
    # A member's role on the team. The choices MIX two families with very different rules:
    # PLAYING roles (team_captain / vice_captain / member) can be fielded and count toward the
    # 6-player cap; STAFF roles (coach / manager / analyst) are support-only, never play, and are
    # limited to one of each. The canonical split lives in afc_team/views.py as PLAYER_ROLES /
    # STAFF_ROLES, and the two caps live there as MAX_PLAYERS (6) / MAX_MEMBERS (9 = 6 + 1 + 1 + 1).
    #
    # DISPLAY RENAME, owner 2026-08-04 (backlog item 33: "Rename the management role 'member' to
    # 'player'"). Only the human LABEL changed, 'Member' -> 'Player'. The STORED value is still
    # 'member' on purpose:
    #   - This repo gitignores migrations and generates them on the server, so a data migration
    #     that rewrites every live TeamMembers row (and every pending Invite's
    #     role_to_be_given_upon_acceptance) cannot ship safely.
    #   - The literal 'member' is the join/invite DEFAULT and is hard-coded in the backend role
    #     sets (PLAYER_ROLES, ALLOWED_IG_ROLES, _INVITABLE_ROLES) and mirrored in the frontend
    #     (teams/[id]/roster, tournaments/[slug] EventDetailsWrapper's registration picker). A
    #     value rename would have to land in all of them at the same instant as the data change.
    # Changing only the label is a schema no-op for a CharField, and every user-facing surface now
    # says "Player" (frontend messages/*/teamsplayers.json -> roster.member / teamDetail.role*).
    MANAGEMENT_ROLE_CHOICES = [
        ('team_captain', 'Team Captain'),
        ('vice_captain', 'Vice Captain'),
        ('member', 'Player'),          # stored value stays 'member'; displayed as "Player"
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

    # ── One link, several people (owner 2026-08-05) ──────────────────────────────────────────
    # "we need a way for teams to be able to generate links that multiple team members can use to
    # join the team and not just separate links option."
    #
    # An invite was strictly single-use: the first person to accept flipped status_of_invite to
    # 'attended_to' and everybody after them got "Invite already used", so a captain filling four
    # seats had to mint and distribute four links.
    #
    # max_uses is NULL on every invite that already exists and on every DIRECT invite (one named
    # invitee), and NULL keeps meaning exactly what it did: one use. Only a link explicitly minted
    # as reusable carries a number, so nothing about existing behaviour shifts underneath anyone.
    #
    # Uses are counted rather than the row being duplicated, so ONE link is one auditable object:
    # who created it, for which seat, how many of its uses are gone. accepted_by records which
    # accounts walked through it, which is what a captain wants when a link leaks.
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    # Who walked through this link, as a plain list of user ids.
    #
    # A ManyToManyField would be the obvious modelling choice and it CANNOT be used here: Invite's
    # primary key is a UUIDField, and MySQL refuses the join table's foreign key outright -
    # "Referencing column 'invite_id' and referenced column 'invite_id' ... are incompatible"
    # (error 3780, a collation mismatch on the char(32) the UUID is stored as). A JSON list needs
    # no join table, so it sidesteps the problem entirely, and this list is only ever read for one
    # invite at a time - it is never queried across rows, which is the case a real relation would
    # have been worth the fight for.
    accepted_user_ids = models.JSONField(default=list, blank=True)

    def is_multi_use(self):
        """True when this link was minted to be shared. NULL max_uses = the original single-use
        invite, which is every row created before this field existed."""
        return self.max_uses is not None and self.max_uses > 1

    def uses_left(self):
        """Remaining uses. A single-use invite has one left until it is attended_to."""
        if self.max_uses is None:
            return 0 if self.status_of_invite == "attended_to" else 1
        return max(0, self.max_uses - self.use_count)

    def is_exhausted(self):
        return self.uses_left() <= 0

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


# ──────────────────── TeamRolePermission: the owner's control matrix ────────────────────
# Owner 2026-08-08: "a way for team owners to decide what controls the other roles in the team
# have over the team." One row = one team's answer for one management_role.
#
# WHY A ROW PER (TEAM, ROLE) AND NOT A JSON BLOB ON Team:
#   Six roles times six capabilities is thirty-six answers. As a JSON column they would be
#   unqueryable and unvalidated, and any typo'd key would read as False forever with nothing to
#   catch it. As explicit BooleanFields each capability is a real column the DB checks, which is
#   also how the closest prior art in this repo models the same problem (afc_organizers, the
#   per-member organizer permission switches).
#
# WHY A ROW PER ROLE AND NOT PER MEMBER:
#   The owner asked to configure ROLES ("what controls the other roles have"), not individuals. Per
#   role means promoting somebody to coach hands them the coach's controls immediately, with
#   nothing extra to remember, and a team with nine members still has at most six rows.
#
# WHY EVERY FIELD DEFAULTS TO FALSE, when the stock behaviour is NOT all-false:
#   A row is only ever created by an owner's explicit save, and that save writes all six values, so
#   the field default is never the thing that decides a live answer - afc_team.permissions
#   .DEFAULT_ROLE_CAPABILITIES is. The default matters only for a capability ADDED LATER: a new
#   column lands as False on already-saved rows, so an existing team never silently gains a control
#   its owner never granted. Deny is the safe side of that trade.
#
# NO BACKFILL, EVER. Every team alive today has zero rows and keeps zero rows. Absent = the stock
# behaviour, resolved per role in afc_team.permissions.team_role_can. See that module for the full
# reasoning and for the list of gates that read this.
class TeamRolePermission(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="role_permissions")

    # One of TeamMembers.MANAGEMENT_ROLE_CHOICES. The team OWNER is deliberately NOT a valid value:
    # "owner" is not a management_role, so no row here can ever restrict the owner, and a team
    # cannot lock itself out. The write endpoint rejects any key outside these six.
    management_role = models.CharField(
        max_length=20, choices=TeamMembers.MANAGEMENT_ROLE_CHOICES)

    # ── the capabilities (see afc_team.permissions.TEAM_CAPABILITIES for what each one gates) ──
    can_invite_members = models.BooleanField(default=False)
    can_manage_join_requests = models.BooleanField(default=False)
    can_edit_roster = models.BooleanField(default=False)
    can_remove_members = models.BooleanField(default=False)
    can_register_for_events = models.BooleanField(default=False)
    can_edit_team_profile = models.BooleanField(default=False)

    # Audit trail. Only the owner can write a row, but ownership transfers (transfer_ownership /
    # admin_transfer_team_ownership), so "which owner set this" is worth keeping. SET_NULL because
    # a deleted account must not take the team's settings with it.
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="team_role_permission_updates")

    class Meta:
        # One answer per role per team. The write endpoint upserts on this pair, which is what
        # makes saving the screen twice idempotent.
        unique_together = ("team", "management_role")

    def __str__(self):
        return f"{self.team.team_name} / {self.management_role}"


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
