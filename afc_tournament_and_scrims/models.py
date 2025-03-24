from django.db import models
from afc_team.models import Team
from afc_auth.models import User

# Create your models here.


class Event(models.Model):
    EVENT_TYPE_CHOICES = [
        ("tournament", "Tournament"),
        ("scrims", "Scrims")
    ]

    FORMAT_CHOICES = [
        ("battle_royale", "Battle Royale"),
        ("clash_squad", "Clash Squad"),
        ("hybrid", "Hybrid")
    ]

    LOCATION_CHOICES = [
        ("online", "Online"),
        ("physical", "Physical"),
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
    event_type = models.CharField(max_length=10, choices=EVENT_TYPE_CHOICES)
    event_name = models.CharField(max_length=40)
    format = models.CharField(max_length=20, choices=FORMAT_CHOICES)
    location = models.CharField(max_length=20, choices=LOCATION_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    registration_open_date = models.DateField()
    registration_end_date = models.DateField()
    prizepool = models.CharField(max_length=20)
    prize_distribution = models.JSONField(default=dict)
    event_rules = models.CharField(max_length=200)
    event_status = models.CharField(max_length=20, choices=EVENT_STATUS_CHOICES, null=False)
    registration_link = models.URLField()
    tournament_tier = models.CharField(max_length=20, choices=TOURNAMENT_TIER_CHOICES, null=False)
    event_banner = models.ImageField(upload_to='event_banner/', null=True)
    stream_channel = models.URLField()


class Leaderboard(models.Model):
    STAGE_CHOICES = [("group_stage", "Group Stage"), ("finals", "Finals")]
    GROUP_CHOICES = [("A", "Group A"), ("B", "Group B"), ("C", "Group C")]

    leaderboard_id = models.AutoField(primary_key=True)
    leaderboard_name = models.CharField(max_length=120)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    stage = models.CharField(max_length=50, choices=STAGE_CHOICES)
    group = models.CharField(max_length=10, choices=GROUP_CHOICES, null=True, blank=True)
    creation_date = models.DateField(auto_now=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE)


class Match(models.Model):
    MATCH_TYPE_CHOICES = [("battle_royale", "Battle Royale"), ("clash_squad", "Clash Squad")]

    match_id = models.AutoField(primary_key=True)
    leaderboard = models.ForeignKey(Leaderboard, on_delete=models.CASCADE)
    match_type = models.CharField(max_length=20, choices=MATCH_TYPE_CHOICES)
    map = models.CharField(max_length=50)
    mvp = models.ForeignKey("afc_auth.User", on_delete=models.CASCADE, related_name="tournament_mvp")



class MatchTeamStats(models.Model):
    team_stats_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE)
    team = models.ForeignKey("afc_team.Team", on_delete=models.CASCADE, related_name="tournament_match_stats")
    placement = models.PositiveIntegerField()  # Integer instead of CharField


class MatchPlayerStats(models.Model):
    player_stats_id = models.AutoField(primary_key=True)
    team_stats = models.ForeignKey(MatchTeamStats, on_delete=models.CASCADE)
    player = models.ForeignKey(User, on_delete=models.CASCADE)
    kills = models.PositiveIntegerField()
    damage = models.PositiveIntegerField(null=True, blank=True)