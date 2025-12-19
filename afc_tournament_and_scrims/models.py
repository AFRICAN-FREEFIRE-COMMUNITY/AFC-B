from django.db import models
from afc_leaderboard.models import TournamentTeam
from afc_team.models import Team, TeamMembers
from afc_auth.models import User

# ---------------- Event ----------------
class Event(models.Model):
    COMPETITION_TYPE_CHOICES = [
        ("tournament", "Tournament"),
        ("scrims", "Scrims")
    ]

    PARTICIPANT_TYPE_CHOICES = [
        ("solo", "Solo"),
        ("duo", "Duo"),
        ("squad", "Squad")
    ]

    EVENT_TYPE_CHOICES = [
        ("internal", "Internal"),
        ("external", "External")
    ]

    EVENT_MODE_CHOICES = [
        ("virtual", "Online"),
        ("physical(lan)", "Physical(LAN)"),
        ("hybrid", "Hybrid")
    ]

    EVENT_STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("ongoing", "Ongoing"),
        ("completed", "Completed")
    ]

    TOURNAMENT_TIER_CHOICES = [
        ("tier_1", "Tier 1"),
        ("tier_2", "Tier 2"), 
        ("tier_3", "Tier 3")
    ]

    event_id = models.AutoField(primary_key=True)
    competition_type = models.CharField(max_length=10, choices=COMPETITION_TYPE_CHOICES)
    participant_type = models.CharField(max_length=10, choices=PARTICIPANT_TYPE_CHOICES)
    event_type = models.CharField(max_length=10, choices=EVENT_TYPE_CHOICES)
    max_teams_or_players = models.PositiveIntegerField()
    event_name = models.CharField(max_length=40)
    event_mode = models.CharField(max_length=20, choices=EVENT_MODE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    registration_open_date = models.DateField()
    registration_end_date = models.DateField()
    prizepool = models.CharField(max_length=20)
    prize_distribution = models.JSONField(default=dict)
    event_rules = models.CharField(max_length=200)
    event_status = models.CharField(max_length=20, choices=EVENT_STATUS_CHOICES)
    registration_link = models.URLField()
    tournament_tier = models.CharField(max_length=20, choices=TOURNAMENT_TIER_CHOICES, default="tier_3")
    event_banner = models.ImageField(upload_to='event_banner/', null=True)
    number_of_stages = models.PositiveIntegerField()
    uploaded_rules = models.FileField(upload_to='event_rules/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_draft = models.BooleanField(default=True)

# ---------------- Stream Channels ----------------
class StreamChannel(models.Model):
    channel_id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="stream_channels")
    channel_url = models.URLField()

# ---------------- Stages ----------------
class Stages(models.Model):
    STAGE_FORMAT_CHOICES = [
        ("br - normal", "Battle Royale - Normal"),
        ("br - roundrobin", "Battle Royale - Knockout"),
        ("br - point rush", "Battle Royale - Point Rush"),
        ("br - champion rush", "Battle Royale - Champion Rush"),
        ("cs - normal", "Clash Squad - Normal"),
        ("cs - league", "Clash Squad - League"),
        ("cs - knockout", "Clash Squad - Knockout"),
        ("cs - double elimination", "Clash Squad - Double Elimination"),
        ("cs - round robin", "Clash Squad - Round Robin")
    ]
    stage_id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="stages")
    stage_name = models.CharField(max_length=50)
    start_date = models.DateField()
    end_date = models.DateField()
    number_of_groups = models.PositiveIntegerField()
    stage_format = models.CharField(max_length=100, choices=STAGE_FORMAT_CHOICES)
    teams_qualifying_from_stage = models.PositiveIntegerField()
    stage_discord_role_id = models.CharField(max_length=100, null=True, blank=True)

class StageGroups(models.Model):
    group_id = models.AutoField(primary_key=True)
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="groups")
    group_name = models.CharField(max_length=50)
    playing_date = models.DateField()
    playing_time = models.TimeField()
    teams_qualifying = models.PositiveIntegerField()
    group_discord_role_id = models.CharField(max_length=100, null=True, blank=True)


# ---------------- Registered Competitors ----------------
class RegisteredCompetitors(models.Model):
    STATUS_CHOICES = [
        ("registered", "Registered"),
        ("disqualified", "Disqualified"),
        ("withdrawn", "Withdrawn")
    ]
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="registrations")
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="registered")
    registration_date = models.DateTimeField(auto_now_add=True)


class EventPageView(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="pageviews")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)  # if available
    ip_address = models.CharField(max_length=45, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class SocialShare(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="social_shares")
    platform = models.CharField(max_length=50, null=True, blank=True) # facebook/twitter/whatsapp...
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class StageCompetitor(models.Model):
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="competitors")
    tournament_team = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.CASCADE)
    player = models.ForeignKey(RegisteredCompetitors, null=True, blank=True, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=20,
        choices=[("active", "Active"), ("disqualified", "Disqualified"), ("withdrawn", "Withdrawn")],
        default="active"
    )

    class Meta:
        unique_together = ("stage", "tournament_team", "player")


class StageGroupCompetitor(models.Model):
    stage_group = models.ForeignKey(StageGroups, on_delete=models.CASCADE, related_name="competitors")
    tournament_team = models.ForeignKey(TournamentTeam, null=True, blank=True, on_delete=models.CASCADE)
    player = models.ForeignKey(RegisteredCompetitors, null=True, blank=True, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=20,
        choices=[("active", "Active"), ("disqualified", "Disqualified"), ("withdrawn", "Withdrawn")],
        default="active"
    )

    class Meta:
        unique_together = ("stage_group", "tournament_team", "player")