from django.db import models

# Create your models here.

class Country(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=2, unique=True)

    def __str__(self):
        return self.name


class RecruitmentPost(models.Model):
    POST_TYPE_CHOICES = [
        ('TEAM_RECRUITMENT', 'Team Recruitment'),
        ('PLAYER_AVAILABLE', 'Player Available'),
    ]
    TIER_CHOICES = [
        ('TIER_1', 'Tier 1'),
        ('TIER_2', 'Tier 2'),
        ('TIER_3', 'Tier 3'),
    ]
    COMMITMENT_CHOICES = [
        ('FULL_TIME', 'Full Time'),
        ('PART_TIME', 'Part Time'),
    ]
    REGION_CHOICES = [
        ('WA', 'West Africa'),
        ('EA', 'East Africa'),
        ('NA', 'North Africa'),
        ('SA', 'South Africa'),
        ('CA', 'Central Africa'),
    ]
    ROLE_CHOICES = [
        ('IGL', 'In-Game Leader'),
        ('RUSHER', 'Rusher'),
        ('SUPPORT', 'Support'),
        ('SNIPER', 'Sniper'),
        ('GRENADE', 'Grenade'),
    ]
    AVAILABILITY_TYPE_CHOICES = [
        ('TRIAL', 'Trial'),
        ('PERMANENT', 'Permanent'),
        ('SCRIMS_ONLY', 'Scrims Only'),
    ]

    # Applies to both team and player posts
    post_type = models.CharField(max_length=20, choices=POST_TYPE_CHOICES)
    post_expiry_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE, related_name='recruitment_posts')
    country = models.ForeignKey(Country, on_delete=models.SET_NULL, null=True, blank=True)  # Used for player posts (single)
    countries = models.ManyToManyField(Country, blank=True, related_name='team_recruitment_posts')  # Used for team posts (multiple)
    is_visible = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    # Applies to player posts
    player = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE, null=True, blank=True)
    primary_role = models.CharField(max_length=50, blank=True, choices=ROLE_CHOICES)
    secondary_role = models.CharField(max_length=50, blank=True, choices=ROLE_CHOICES)
    availability_type = models.CharField(max_length=20, choices=AVAILABILITY_TYPE_CHOICES, blank=True)
    additional_info = models.TextField(blank=True)

    # Applies to team posts
    team = models.ForeignKey('afc_team.Team', on_delete=models.CASCADE, null=True, blank=True)
    roles_needed = models.JSONField(blank=True, null=True)
    minimum_tier_required = models.CharField(max_length=50, choices=TIER_CHOICES, blank=True)
    commitment_type = models.CharField(max_length=20, choices=COMMITMENT_CHOICES, blank=True)
    recruitment_criteria = models.TextField(blank=True)


    @property
    def is_active(self):
        from datetime import date
        return self.post_expiry_date >= date.today()
    
    class Meta:
        indexes = [
            models.Index(fields=['post_type']),
            models.Index(fields=['country']),
            models.Index(fields=['created_at']),
        ]


class RecruitmentApplication(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('REJECTED', 'Rejected'),
        ('SHORTLISTED', 'Shortlisted'),
        ('INVITED', 'Invited to Trial'),
        ('TRIAL_ONGOING', 'Trial Ongoing'),
        ('ACCEPTED', 'Accepted'),
        ('TRIAL_EXTENDED', 'Trial Extended'),
    ]

    player = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE)
    recruitment_post = models.ForeignKey('RecruitmentPost', on_delete=models.CASCADE)

    team = models.ForeignKey('afc_team.Team', on_delete=models.CASCADE)
    application_message = models.TextField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')

    contact_unlocked = models.BooleanField(default=False)
    invite_expires_at = models.DateTimeField(null=True, blank=True)

    reason = models.TextField(null=True, blank=True)  # Reason for rejection or other status updates

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class TrialInvite(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
    ]
    team = models.ForeignKey('afc_team.Team', on_delete=models.CASCADE)
    player = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE)
    application = models.ForeignKey('RecruitmentApplication', on_delete=models.CASCADE)
    reason = models.TextField(null=True, blank=True)  # Reason for trial invite

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    status = models.CharField(max_length=20, default='PENDING') # PENDING / ACCEPTED / REJECTED


class TrialInviteLog(models.Model):
    team = models.ForeignKey('afc_team.Team', on_delete=models.CASCADE)
    player = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE)
    application = models.ForeignKey('RecruitmentApplication', on_delete=models.CASCADE)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    status = models.CharField(max_length=20, default='ACTIVE')  # ACTIVE / EXPIRED


class PlayerReport(models.Model):
    player = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE)
    team = models.ForeignKey('afc_team.Team', on_delete=models.CASCADE)
    application = models.ForeignKey('RecruitmentApplication', on_delete=models.CASCADE)

    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class DirectTrialInvite(models.Model):
    """
    Sent by a team to a player who has posted a PLAYER_AVAILABLE post.
    This is the reverse of RecruitmentApplication (team reaches out to player).
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('ACCEPTED', 'Accepted'),
        ('REJECTED', 'Rejected'),
        ('EXPIRED', 'Expired'),
    ]

    team = models.ForeignKey('afc_team.Team', on_delete=models.CASCADE, related_name='sent_direct_invites')
    player = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE, related_name='received_direct_invites')
    player_post = models.ForeignKey('RecruitmentPost', on_delete=models.CASCADE, related_name='direct_invites')
    message = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('team', 'player_post')  # one invite per team per post

    def __str__(self):
        return f"{self.team.team_name} → {self.player.username} ({self.status})"


class TrialChat(models.Model):
    application = models.OneToOneField('RecruitmentApplication', on_delete=models.CASCADE, related_name='trial_chat')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Trial Chat - {self.application.team.team_name} & {self.application.player.username}"


class TrialChatMessage(models.Model):
    chat = models.ForeignKey('TrialChat', on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey('afc_auth.User', on_delete=models.CASCADE)
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sent_at']

    def __str__(self):
        return f"{self.sender.username}: {self.message[:50]}"


# ══════════════════════════════════════════════════════════════════════════════
#  MODERATION — player-market reporting + bans  (feature "J-market-reporting")
# ──────────────────────────────────────────────────────────────────────────────
#  Two models power the moderation surface built from
#  WEBSITE/tasks/market-reporting-mockup.html:
#
#    • MarketReport — a user-filed abuse report against a market post (a team's
#      recruitment post or a player's availability post). Copied IN SPIRIT from
#      afc_organizers.OrganizationReport: a category + free-text details + optional
#      evidence image + a triage status (open / reviewing / resolved / dismissed /
#      banned), with reviewed_by + resolution_notes for the moderator's record.
#
#    • MarketBan — a moderator-applied ban that blocks a PLAYER or a whole TEAM from
#      acting on the market (posting, applying, inviting). Copied from afc_auth's
#      TeamBan / BannedPlayer shape: a start date, a duration in days (null = a
#      permanent ban), a computed ban_end_date, a reason (shown to the banned user),
#      banned_by, and an is_active flag. Enforcement lives in afc_player_market/views.py
#      (see _active_market_ban) — every post/apply/invite entry point checks it first.
#
#  Why a NEW MarketBan rather than reusing afc_auth.TeamBan / BannedPlayer: those
#  models are the SITE-WIDE ban (they flip Team.is_banned / gate auth) and TeamBan is
#  a OneToOne, so one team can hold only one. A market ban is scoped to the player
#  market alone, must coexist with the site-wide ban, and a subject can be re-banned
#  over time — so it needs its own table. The field shape is intentionally identical
#  so the original dev reads it the same way.
# ══════════════════════════════════════════════════════════════════════════════

from datetime import timedelta                       # ban_end_date = start + duration
from django.utils import timezone                     # tz-aware "now" for ban dates


class MarketReport(models.Model):
    """A user-submitted report against a player-market post.

    Mirrors afc_organizers.OrganizationReport. The reported SUBJECT is either a team
    (a recruitment post) or a player (an availability post); we store BOTH a
    subject_type discriminator AND the concrete FK so the admin queue can render and
    filter without re-deriving it from the post. The originating `post` FK is kept
    (SET_NULL) so a moderator can open the exact post, but a deleted post does not
    delete the report (the abuse record must outlive the post).

    Consumed by:
      • POST /player-market/report-post/          (file_market_report)   — any user
      • GET  /player-market/admin/reports/        (admin_list_market_reports)
      • PATCH/player-market/admin/reports/<id>/   (admin_update_market_report)
    Frontend: the Report dialog on app/(user)/player-markets/page.tsx (file) and the
    "Reports & Flags" tab on app/(a)/a/player-markets/page.tsx (triage).
    """

    # Whether the report targets a whole team or a single player. Drives the default
    # ban scope in the report → ban flow (a team report bans the team, etc.).
    SUBJECT_TYPE_CHOICES = [
        ("team", "Team"),
        ("player", "Player"),
    ]

    # Report reasons — match the radio options in the mockup's Report dialog 1:1.
    CATEGORY_CHOICES = [
        ("bad_tryout", "Negative tryout experience"),
        ("scam", "Scam / fraud"),
        ("abusive", "Abusive conduct"),
        ("fake_post", "Fake / misleading post"),
        ("other", "Other"),
    ]

    # Triage lifecycle. "banned" is the terminal state stamped when a moderator bans
    # the subject straight from the report (mirrors the mockup's banned row badge).
    STATUS_CHOICES = [
        ("open", "Open"),
        ("reviewing", "Reviewing"),
        ("resolved", "Resolved"),
        ("dismissed", "Dismissed"),
        ("banned", "Banned"),
    ]

    # ── who/what is being reported ──
    subject_type = models.CharField(max_length=10, choices=SUBJECT_TYPE_CHOICES)
    # Exactly one of these is set depending on subject_type. Both SET_NULL so a deleted
    # team/player does not erase the abuse record (only the link goes null).
    reported_team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="market_reports",
    )
    reported_player = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="market_reports_against",
    )
    # The post that prompted the report. SET_NULL — the report survives the post.
    post = models.ForeignKey(
        "RecruitmentPost", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reports",
    )

    # ── the report body (mirrors OrganizationReport.category/details/evidence) ──
    reporter = models.ForeignKey(
        "afc_auth.User", null=True, on_delete=models.SET_NULL,
        related_name="market_reports_filed",
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="other")
    details = models.TextField()                       # required free text (what happened)
    evidence = models.ImageField(upload_to="market_report_evidence/", null=True, blank=True)

    # ── triage (mirrors OrganizationReport.status/reviewed_by/resolution_notes) ──
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="open")
    reviewed_by = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="market_reports_reviewed",
    )
    resolution_notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Newest-first is the default queue order; index created_at + status so the
        # admin list (filter by status, order by -created_at) stays index-served.
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        who = self.reported_team_id or self.reported_player_id
        return f"MarketReport({self.subject_type} {who} {self.category} [{self.status}])"


class MarketBan(models.Model):
    """A moderator-applied ban blocking a player or a team from the player market.

    Field shape copied from afc_auth.BannedPlayer / TeamBan: ban_start_date,
    ban_duration (days; NULL = permanent), ban_end_date (computed on save when a
    duration is given), reason (shown to the banned user), banned_by, is_active.

    Scope semantics:
      • scope="player" → only `banned_player` is blocked; their teammates are not.
      • scope="team"   → every member of `banned_team` is blocked (enforcement resolves
        the acting user's team membership, see views._active_market_ban).

    A ban is "active" when is_active is True AND (it is permanent OR ban_end_date is in
    the future). Enforcement never trusts is_active alone — it also checks expiry, the
    same belt-and-braces pattern BannedPlayer uses, so an un-swept expired row can't
    keep blocking a user.

    Consumed by:
      • POST /player-market/admin/ban/         (admin_market_ban)        — moderators
      • the enforcement guard in create_recruitment_post / apply_to_team /
        invite_player_to_trial (block a banned poster before the row is created).
    Frontend: the Ban dialog on the admin "Reports & Flags" tab.
    """

    SCOPE_CHOICES = [
        ("player", "Player"),
        ("team", "Team"),
    ]

    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    # Exactly one is set per `scope`. CASCADE: if the team/player is hard-deleted the
    # ban is moot, so it goes with them (matches BannedPlayer/TeamBan CASCADE).
    banned_team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True, on_delete=models.CASCADE,
        related_name="market_bans",
    )
    banned_player = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.CASCADE,
        related_name="market_bans",
    )

    ban_start_date = models.DateTimeField(default=timezone.now)
    # Duration in DAYS. NULL = permanent (mirrors the mockup's "Permanent" preset, which
    # posts days:null). A positive integer otherwise.
    ban_duration = models.IntegerField(null=True, blank=True)
    # Computed end. NULL for a permanent ban; otherwise start + duration (set in save()).
    ban_end_date = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=255, default="No reason provided")

    banned_by = models.ForeignKey(
        "afc_auth.User", null=True, on_delete=models.SET_NULL,
        related_name="market_bans_issued",
    )
    # The report this ban was actioned from, if any (lets the queue mark that report
    # "banned"). SET_NULL so deleting a report does not wipe the ban record.
    source_report = models.ForeignKey(
        "MarketReport", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="resulting_bans",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["is_active"]),
        ]

    def save(self, *args, **kwargs):
        # Compute ban_end_date from the duration on first save (only when a finite
        # duration is given). A permanent ban (ban_duration is None) keeps a null end.
        # Mirrors BannedPlayer.save() exactly, guarded for the permanent case.
        if self.ban_end_date is None and self.ban_duration:
            self.ban_end_date = self.ban_start_date + timedelta(days=self.ban_duration)
        super().save(*args, **kwargs)

    @property
    def is_permanent(self) -> bool:
        """True when no duration was set — the ban has no end date."""
        return self.ban_duration is None

    def is_currently_active(self) -> bool:
        """The truth used by enforcement: active flag AND (permanent OR not expired).

        Checked alongside is_active rather than instead of it so a stale row whose
        end date has passed stops blocking even if a sweeper hasn't flipped is_active.
        """
        if not self.is_active:
            return False
        if self.is_permanent:
            return True
        return self.ban_end_date is not None and timezone.now() < self.ban_end_date

    def lift_ban(self):
        """Lift the ban manually (mirrors BannedPlayer.lift_ban)."""
        self.is_active = False
        self.save()

    def __str__(self):
        target = self.banned_team_id if self.scope == "team" else self.banned_player_id
        end = "permanent" if self.is_permanent else f"until {self.ban_end_date}"
        return f"MarketBan({self.scope} {target} {end})"


