import uuid
from django.db import models
from afc_team.models import Team, TeamMembers
from django.conf import settings

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

    STAGE_STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("ongoing", "Ongoing"),
        ("completed", "Completed")
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
    stage_status = models.CharField(max_length=20, choices=STAGE_STATUS_CHOICES, default="upcoming")

class StageGroups(models.Model):
    group_id = models.AutoField(primary_key=True)
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="groups")
    group_name = models.CharField(max_length=50)
    playing_date = models.DateField()
    playing_time = models.TimeField()
    teams_qualifying = models.PositiveIntegerField()
    group_discord_role_id = models.CharField(max_length=100, null=True, blank=True)
    match_count = models.PositiveIntegerField()
    match_maps = models.JSONField(default=list)  # List of maps for the matches



# ---------------- Registered Competitors ----------------
class RegisteredCompetitors(models.Model):
    STATUS_CHOICES = [
        ("registered", "Registered"),
        ("disqualified", "Disqualified"),
        ("withdrawn", "Withdrawn")
    ]
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="registrations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="registered")
    registration_date = models.DateTimeField(auto_now_add=True)

# ---------------- Leaderboard ----------------
class Leaderboard(models.Model):
    LEADERBOARD_METHOD_CHOICES = [
        ("manual", "Manual"),
        ("room_file_upload", "Room File Upload"),
        ("image_upload", "Image Upload")
    ]

    FILE_TYPE_CHOICES = [
        ("math_result_file", "Match Result File"),
        ("debugger_file", "Debugger File")
    ]

    leaderboard_id = models.AutoField(primary_key=True)
    leaderboard_name = models.CharField(max_length=120)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="leaderboards")
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="leaderboards")
    group = models.ForeignKey(StageGroups, on_delete=models.CASCADE, null=True, blank=True, related_name="leaderboards")
    creation_date = models.DateField(auto_now=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    placement_points = models.JSONField(default=dict, blank=True)  
    # example: {"1": 12, "2": 9, "3": 8, ..., "10": 1}
    kill_point = models.FloatField(default=1.0)
    leaderboard_method = models.CharField(max_length=30, choices=LEADERBOARD_METHOD_CHOICES)
    file_type = models.CharField(max_length=30, choices=FILE_TYPE_CHOICES, null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("event", "stage", "group")

# ---------------- Matches & Stats ----------------
class Match(models.Model):
    match_id = models.AutoField(primary_key=True)
    leaderboard = models.ForeignKey(Leaderboard, on_delete=models.CASCADE, related_name="matches", null=True, blank=True)
    group = models.ForeignKey(StageGroups, on_delete=models.CASCADE, related_name="matches", null=True, blank=True)
    mvp = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="mvp_matches")
    match_date = models.DateTimeField(auto_now_add=True)
    match_number = models.PositiveIntegerField()
    room_id = models.CharField(max_length=50, null=True, blank=True)
    room_password = models.CharField(max_length=50, null=True, blank=True)
    room_name = models.CharField(max_length=100, null=True, blank=True)
    result_inputted = models.BooleanField(default=False)
    match_map = models.CharField(
        max_length=50,
        choices=[
            ('bermuda', 'Bermuda'),
            ('purgatory', 'Purgatory'),
            ('kalahari', 'Kalahari'),
            ('alpine', 'Alpine'),
            ('nexterra', 'Nexterra'),
            ('solara', 'Solara'),
        ]
    )

class TournamentTeam(models.Model):
    """
    Links a Team to a Tournament Event.
    """
    TEAM_STATUS = [
        ("active", "Active"),
        ("disqualified", "Disqualified"),
        ("withdrawn", "Withdrawn"),
    ]
    tournament_team_id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tournament_teams")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="tournament_entries")
    status = models.CharField(max_length=20, choices=TEAM_STATUS, default="active")

    def __str__(self):
        return f"{self.team.team_name} in {self.event.event_name}"
    

class TournamentTeamMember(models.Model):
    """
    Members of the team for this tournament.
    """
    tournament_team = models.ForeignKey(TournamentTeam, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("tournament_team", "user")

    def __str__(self):
        return f"{self.user.username} in {self.tournament_team.team.team_name}"

class TournamentTeamMatchStats(models.Model):
    """
    Stores stats per team in a match
    """
    team_stats_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="team_stats")
    tournament_team = models.ForeignKey(TournamentTeam, on_delete=models.CASCADE, related_name="match_stats")
    placement = models.PositiveIntegerField()
    kills = models.PositiveIntegerField(default=0)
    damage = models.PositiveIntegerField(default=0)
    assists = models.PositiveIntegerField(default=0)
    placement_points = models.PositiveIntegerField(default=0)
    kill_points = models.PositiveIntegerField(default=0)
    total_points = models.PositiveIntegerField(default=0)

class TournamentPlayerMatchStats(models.Model):
    """
    Stores stats per player in a match (solo/duo/squad)
    """
    player_stats_id = models.AutoField(primary_key=True)
    team_stats = models.ForeignKey(TournamentTeamMatchStats, on_delete=models.CASCADE, related_name="player_stats")
    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kills = models.PositiveIntegerField(default=0)
    damage = models.PositiveIntegerField(default=0)
    assists = models.PositiveIntegerField(default=0)



class EventPageView(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="pageviews")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)  # if available
    ip_address = models.CharField(max_length=45, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class SocialShare(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="social_shares")
    platform = models.CharField(max_length=50, null=True, blank=True) # facebook/twitter/whatsapp...
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
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


# class PlacementPointSystem(models.Model):
#     leaderboard = models.ForeignKey(Leaderboard, on_delete=models.CASCADE, related_name="point_system")
#     placement = models.PositiveIntegerField()  # 1,2,3...
#     points = models.PositiveIntegerField()

#     class Meta:
#         unique_together = ("leaderboard", "placement")


class SoloPlayerMatchStats(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="solo_stats")
    competitor = models.ForeignKey(RegisteredCompetitors, on_delete=models.CASCADE)
    placement = models.PositiveIntegerField()
    kills = models.PositiveIntegerField(default=0)

    placement_points = models.PositiveIntegerField(default=0)
    kill_points = models.PositiveIntegerField(default=0)

    bonus_points = models.IntegerField(default=0)   # ✅ +
    penalty_points = models.IntegerField(default=0) # ✅ -
    total_points = models.IntegerField(default=0)

    class Meta:
        unique_together = ("match", "competitor")
