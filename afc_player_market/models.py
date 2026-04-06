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
    country = models.ForeignKey(Country, on_delete=models.SET_NULL, null=True, blank=True)
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


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


