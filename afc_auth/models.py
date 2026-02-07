from datetime import timedelta
import uuid
from django.conf import settings
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from django.utils.timezone import now
from django.utils.text import slugify

from afc_tournament_and_scrims.models import Stages, StageGroups

class User(AbstractUser):
    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("moderator", "Moderator"),
        ("support", "Support"),
        ("player", "Player")
    ]

    STATUS_CHOICES = [
        ("active", "Active"),
        ("suspended", "Suspended")
    ]

    user_id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=40, unique=True)
    # in_game_name = models.CharField(max_length=12, unique=True)
    uid = models.CharField(max_length=15, unique=True)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=120, blank=False, null=False)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, null=False, default="player")
    # session_token = models.CharField(max_length=16)
    full_name = models.CharField(max_length=40)
    country = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, null=False, default="active")
    last_login = models.DateTimeField(null=True)
    discord_id = models.CharField(max_length=50, null=True, blank=True)
    discord_username = models.CharField(max_length=100, null=True, blank=True)
    discord_avatar = models.URLField(null=True, blank=True)
    discord_connected = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = "username"  # Set in_game_name as username
    REQUIRED_FIELDS = ["email", "full_name"]

    def __str__(self):
        return self.username


# class User(AbstractUser):
#     ROLE_CHOICES = [
#         ("admin", "Admin"),
#         ("moderator", "Moderator"),
#         ("support", "Support"),
#         ("player", "Player")
#     ]

#     STATUS_CHOICES = [
#         ("active", "Active"),
#         ("suspended", "Suspended")
#     ]

#     user_id = models.AutoField(primary_key=True)
#     username = models.CharField(max_length=40, unique=True)
#     # in_game_name = models.CharField(max_length=12, unique=True)
#     uid = models.CharField(max_length=15, unique=True)
#     email = models.EmailField(unique=True)
#     password = models.CharField(max_length=120, blank=False, null=False)
#     role = models.CharField(max_length=20, choices=ROLE_CHOICES, null=False, default="player")
#     session_token = models.CharField(max_length=16)
#     full_name = models.CharField(max_length=40)
#     country = models.CharField(max_length=40)
#     status = models.CharField(max_length=20, choices=STATUS_CHOICES, null=False, default="active")
#     last_login = models.DateTimeField(null=True)
#     discord_id = models.CharField(max_length=50, null=True, blank=True)
#     discord_username = models.CharField(max_length=100, null=True, blank=True)
#     discord_avatar = models.URLField(null=True, blank=True)
#     discord_connected = models.BooleanField(default=False)
#     created_at = models.DateTimeField(auto_now_add=True)

#     USERNAME_FIELD = "username"  # Set in_game_name as username
#     REQUIRED_FIELDS = ["email", "full_name"]

#     def __str__(self):
#         return self.username


class SessionToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="session_tokens")
    token = models.CharField(max_length=64, unique=True)  # can be random string
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        # Automatically set expiry to 20 mins after creation if not provided
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=60)
        super().save(*args, **kwargs)

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"{self.user.username} - {self.token}"


class UserProfile(models.Model):
    profile_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    date_of_birth = models.DateField(null=True)
    state = models.CharField(max_length=40, null=True)
    profile_pic = models.ImageField(upload_to='profile_pictures/', null=True)
    esports_pic = models.ImageField(upload_to='esports_pictures/', null=True)


class LoginHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    ip_address = models.CharField(max_length=45)
    user_agent = models.TextField(null=True, blank=True)
    continent = models.CharField(max_length=50, null=True, blank=True)
    country = models.CharField(max_length=50, null=True, blank=True)
    city = models.CharField(max_length=50, null=True, blank=True)
    region = models.CharField(max_length=50, null=True, blank=True)
    timezone = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Roles(models.Model):
    ROLES = [
        ("head_admin", "Head Admin"),
        ("shop_admin", "Shop Admin"),
        ("news_admin", "News Admin"),
        ("event_admin", "Event Admin"),
        ("teams_admin", "Teams Admin"),
        ("partner_admin", "Partner Admin"),

    ]

    role_id = models.AutoField(primary_key=True)
    role_name = models.CharField(max_length=20, choices=ROLES, unique=True)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.role_name
    

class UserRoles(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="userroles")
    role = models.ForeignKey(Roles, on_delete=models.CASCADE)
    date_assigned = models.DateTimeField(auto_now=True)


class PasswordResetToken(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    token = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        return (timezone.now() - self.created_at) <= timedelta(minutes=10)  # token valid for 10 mins


class TeamBan(models.Model):
    team = models.OneToOneField("afc_team.Team", on_delete=models.CASCADE)
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
    slug = models.SlugField(max_length=220, unique=True, blank=True, db_index=True, null=True)
    news_title = models.CharField(max_length=255)
    content = models.TextField()
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    related_event = models.ForeignKey("afc_tournament_and_scrims.Event", on_delete=models.SET_NULL, null=True, blank=True)
    images = models.ImageField(upload_to="news_images/", null=True, blank=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE)  # Admin, Mod, or Support
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.news_title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.news_title)[:200] or "news"
            slug = base
            i = 2
            while News.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)
    

class AdminHistory(models.Model):
    action_id = models.AutoField(primary_key=True)
    admin_user = models.ForeignKey(User, on_delete=models.CASCADE)
    action = models.CharField(max_length=50)  # e.g., "banned_player", "edited_news"
    description = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)


class Notifications(models.Model):
    notification_id = models.AutoField(primary_key=True)
    notification_type = models.CharField(max_length=50, null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    related_event = models.ForeignKey("afc_tournament_and_scrims.Event", on_delete=models.CASCADE, null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    related_invite = models.ForeignKey("afc_team.Invite", on_delete=models.CASCADE, null=True, blank=True)

    def mark_as_read(self):
        self.is_read = True
        self.save()

    
class DiscordRoleAssignment(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("success", "Success"),
        ("failed", "Failed"),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    discord_id = models.CharField(max_length=50)
    role_id = models.CharField(max_length=50)

    stage = models.ForeignKey(Stages, null=True, blank=True, on_delete=models.CASCADE)
    group = models.ForeignKey(StageGroups, null=True, blank=True, on_delete=models.CASCADE)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    error_message = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)



class DiscordStageRoleAssignmentProgress(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage = models.ForeignKey(Stages, on_delete=models.CASCADE)
    total = models.PositiveIntegerField(default=0)
    completed = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=[("pending", "pending"), ("running", "running"), ("done", "done")],
        default="pending"
    )
    created_at = models.DateTimeField(auto_now_add=True)


