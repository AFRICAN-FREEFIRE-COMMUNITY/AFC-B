from django.db import models

from afc_tournament_and_scrims.models import Stages, StageGroups, Event
from afc_auth.models import User
from afc_team.models import Team

# Create your models here.


# ---------------- Leaderboard ----------------
class Leaderboard(models.Model):
    leaderboard_id = models.AutoField(primary_key=True)
    leaderboard_name = models.CharField(max_length=120)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="leaderboards")
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE, related_name="leaderboards")
    group = models.ForeignKey(StageGroups, on_delete=models.CASCADE, null=True, blank=True, related_name="leaderboards")
    creation_date = models.DateField(auto_now=True)
    creator = models.ForeignKey(User, on_delete=models.CASCADE)


# ---------------- Matches & Stats ----------------
class Match(models.Model):
    match_id = models.AutoField(primary_key=True)
    leaderboard = models.ForeignKey(Leaderboard, on_delete=models.CASCADE, related_name="matches")
    map_name = models.CharField(max_length=50)
    mvp = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="mvp_matches")
    match_date = models.DateTimeField(auto_now_add=True)

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
    user = models.ForeignKey(User, on_delete=models.CASCADE)

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

class TournamentPlayerMatchStats(models.Model):
    """
    Stores stats per player in a match (solo/duo/squad)
    """
    player_stats_id = models.AutoField(primary_key=True)
    team_stats = models.ForeignKey(TournamentTeamMatchStats, on_delete=models.CASCADE, related_name="player_stats")
    player = models.ForeignKey(User, on_delete=models.CASCADE)
    kills = models.PositiveIntegerField(default=0)
    damage = models.PositiveIntegerField(default=0)
    assists = models.PositiveIntegerField(default=0)
