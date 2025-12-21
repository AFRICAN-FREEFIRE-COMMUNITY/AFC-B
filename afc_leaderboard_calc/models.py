from django.db import models
from django.conf import settings
from afc_team.models import Team

class Tournament(models.Model):
    tournament_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tournaments')
    tournament_type = models.CharField(max_length=10) # tournament or scrims

    def __str__(self):
        return self.name
    

class RegisteredTeams(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE)
    team = models.ForeignKey(Team, on_delete=models.CASCADE)


class Match(models.Model):
    match_id = models.AutoField(primary_key=True)
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="matches")
    match_number = models.PositiveIntegerField()
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    mvp = models.ForeignKey("afc_auth.User", on_delete=models.CASCADE, related_name="leaderboard_mvp")


    def __str__(self):
        return f"Match {self.match_number} - {self.tournament.name}"


class ResultImage(models.Model):
    result_image_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="result_images")
    image = models.ImageField(upload_to="match_results/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Result Image for Match {self.match.match_number} - {self.match.tournament.name}"


class MatchLeaderboard(models.Model):
    leaderboard_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name='leaderboard')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True)
    kills = models.IntegerField()
    position_in_match = models.IntegerField()
    score = models.IntegerField()
    rank = models.PositiveIntegerField()

    def __str__(self):
        return f"Leaderboard for {self.match}"

    def update_rankings(self):
        leaderboard_entries = MatchLeaderboard.objects.filter(match=self.match).order_by('-score')
        for index, entry in enumerate(leaderboard_entries, start=1):
            entry.rank = index
            entry.save()


class MatchTeamStats(models.Model):
    teamstats_id = models.AutoField(primary_key=True)
    match = models.ForeignKey(Match, on_delete=models.CASCADE)  # Corrected FK
    team = models.ForeignKey("afc_team.Team", on_delete=models.CASCADE, related_name="leaderboard_match_stats")
    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kills = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.player.username} - {self.match_leaderboard.match}"


class OverallLeaderboard(models.Model):
    leaderboard_id = models.AutoField(primary_key=True)
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name='overall_leaderboard')
    team_name = models.CharField(max_length=255)
    total_score = models.IntegerField()
    rank = models.PositiveIntegerField()

    def __str__(self):
        return f"Overall Leaderboard for {self.tournament.name}"

    def update_overall_rankings(self):
        overall_entries = OverallLeaderboard.objects.filter(tournament=self.tournament).order_by('-total_score')
        for index, entry in enumerate(overall_entries, start=1):
            entry.rank = index
            entry.save()
