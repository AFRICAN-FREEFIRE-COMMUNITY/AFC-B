from datetime import timedelta
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from django.utils.timezone import now
from afc_tournament_and_scrims.models import Event

class User(AbstractUser):
    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("moderator", "Moderator"),
        ("support", "Support"),
        ("player", "Player")
    ]
    user_id = models.AutoField(primary_key=True)
    in_game_name = models.CharField(max_length=12, unique=True)
    uid = models.CharField(max_length=15, unique=True)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=40)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, null=False)
    session_token = models.CharField(max_length=16)


class UserProfile(models.Model):
    profile_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    date_of_birth = models.DateField(null=True)
    country = models.CharField(max_length=40)
    state = models.CharField(max_length=40)
    esports_pic = models.ImageField(upload_to='esports_pictures/', null=True)


class TeamBan(models.Model):
    team = models.OneToOneField("Team", on_delete=models.CASCADE)
    ban_start_date = models.DateTimeField(default=timezone.now)
    ban_end_date = models.DateTimeField()
    reason = models.CharField(max_length=255)
    banned_by = models.ForeignKey(User, null=False, on_delete=models.CASCADE)

    def lift_ban_if_expired(self):
        """Automatically lift the ban if expired"""
        if timezone.now() >= self.ban_end_date:
            self.team.is_banned = False
            self.team.save()
            self.delete()

class BannedPlayer(models.Model):
    ban_id = models.AutoField(primary_key=True)
    banned_player = models.ForeignKey(User, on_delete=models.CASCADE, related_name="bans")
    ban_start_date = models.DateTimeField(default=now)
    ban_duration = models.IntegerField()  # Duration in days
    ban_end_date = models.DateTimeField()
    reason = models.CharField(max_length=255, default="No reason provided")
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if not self.ban_end_date:
            self.ban_end_date = self.ban_start_date + timedelta(days=self.ban_duration)
        super().save(*args, **kwargs)

    def lift_ban(self):
        """Lift the ban manually."""
        self.is_active = False
        self.save()

    def __str__(self):
        return f"{self.banned_player.username} banned until {self.ban_end_date}"


class News(models.Model):
    CATEGORY_CHOICES = [
        ("general", "General News"),
        ("tournament", "Tournament Updates"),
        ("bans", "Banned Players/Teams"),
    ]

    news_id = models.AutoField(primary_key=True)
    news_title = models.CharField(max_length=255)
    content = models.TextField()
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    related_event = models.ForeignKey(Event, on_delete=models.SET_NULL, null=True, blank=True)
    images = models.ImageField(upload_to="news_images/", null=True, blank=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE)  # Admin, Mod, or Support
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.news_title