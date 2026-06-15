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

    # i18n Phase 0 (owner 2026-06-15): the user's preferred UI/email/content language.
    # Locales: English (default), French, Portuguese. Stored as the 2-letter code so the
    # frontend i18n layer and the backend localization (Phase 1+) can key off it directly.
    #   - Written by: afc_auth.views.login (auto-detected from the geo country on FIRST login only,
    #                 via afc_auth.language_utils.language_for_country - never overrides a value the
    #                 user already chose) and afc_auth.views.edit_profile (the manual override the
    #                 user picks in the profile settings language selector).
    #   - Read by   : afc_auth.views.login response + get_user_profile (returned in the auth payload
    #                 the frontend AuthContext maps onto User.language) so the FE can set the active
    #                 locale (NEXT_LOCALE cookie) and send Accept-Language on API calls.
    # blank=True + default "en" so existing rows and any code path that skips it resolve to English.
    LANGUAGE_CHOICES = [
        ("en", "English"),
        ("fr", "Français"),
        ("pt", "Português"),
    ]

    user_id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=40, unique=True)
    # in_game_name = models.CharField(max_length=12, unique=True)
    uid = models.CharField(max_length=15, unique=True, null=True, blank=True)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=120, blank=False, null=False)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, null=False, default="player")
    # session_token = models.CharField(max_length=16)
    full_name = models.CharField(max_length=40)
    country = models.CharField(max_length=40, blank=True, default='')
    # Preferred language code ("en"/"fr"/"pt"). See LANGUAGE_CHOICES above for the why + the readers/writers.
    # Default is BLANK on purpose (not "en"): blank means "not yet chosen/detected", which is what the
    # login() country auto-detect guard (`if not user.language`) keys off, so a first login from a
    # Francophone/Lusophone country sets fr/pt. All readers coalesce blank -> "en" for display
    # (user.language or "en"). An explicit pick in settings stores en/fr/pt and is never auto-overridden.
    language = models.CharField(max_length=2, choices=LANGUAGE_CHOICES, default="", blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, null=False, default="active")
    last_login = models.DateTimeField(null=True)
    discord_id = models.CharField(max_length=50, null=True, blank=True, unique=True, db_index=True)
    discord_username = models.CharField(max_length=100, null=True, blank=True)
    discord_avatar = models.URLField(null=True, blank=True)
    discord_connected = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    # First-time WELCOME tour flag. False until the user finishes/skips/closes the animated
    # newcomer welcome tour, then flipped True so it never auto-opens again.
    #   - Read by  : afc_auth.views.get_user_profile (returned in the logged-in payload the
    #                frontend AuthContext fetches), so the client knows whether to auto-show.
    #   - Written by: afc_auth.views.mark_welcome_seen (POST /auth/mark-welcome-seen/), called
    #                 best-effort by frontend app/(user)/_components/WelcomeTour.tsx on finish.
    has_seen_welcome = models.BooleanField(default=False)

    # One-time DASHBOARD intro coach marks (owner 2026-06-12): when a user is GRANTED access to a
    # role dashboard (admin / sponsor / organizer / vendor), their next login shows a one-time
    # callout pointing at the nav menu where that dashboard lives - NOT a navigate-now popup.
    # Keys are dashboard ids ("admin"|"sponsor"|"organizer"|"vendor") -> True once dismissed.
    # A dict (not a single boolean) so granting a SECOND dashboard later re-triggers the callout
    # for just that new dashboard.
    #   - Read by  : afc_auth.views.get_user_profile (frontend AuthContext maps it onto the user;
    #                app/(user)/_components/DashboardIntroCoachmark.tsx decides what to show).
    #   - Written by: afc_auth.views.mark_dashboard_intro_seen (POST /auth/mark-dashboard-intro-seen/).
    seen_dashboard_intros = models.JSONField(default=dict, blank=True)

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

    # Idle-timeout window: users are auto-logged-out after 3 HOURS OF INACTIVITY (owner
    # 2026-06-14). It is a SLIDING window, not absolute-from-login: every authed request
    # calls touch() to push expires_at forward, so an active user never gets logged out,
    # but 3h with no request expires the session. The frontend slides the auth_token cookie
    # the same way (AuthContext, on each successful API call). Keep these in sync.
    SESSION_LIFETIME = timedelta(hours=3)
    # Only persist a slide when it moves the expiry by more than this, so we do at most one
    # DB write per ~5 min of activity instead of one per request.
    TOUCH_THROTTLE = timedelta(minutes=5)

    def save(self, *args, **kwargs):
        # Default the expiry to the full session window (3h) when not explicitly set.
        if not self.expires_at:
            self.expires_at = timezone.now() + self.SESSION_LIFETIME
        super().save(*args, **kwargs)

    def is_expired(self):
        return timezone.now() > self.expires_at

    def touch(self):
        """Slide the idle window forward on activity. Called by validate_token after a token
        validates. Throttled (TOUCH_THROTTLE) so a burst of requests writes the DB at most once
        every ~5 minutes while still keeping an active session alive."""
        new_exp = timezone.now() + self.SESSION_LIFETIME
        if new_exp - self.expires_at > self.TOUCH_THROTTLE:
            self.expires_at = new_exp
            self.save(update_fields=["expires_at"])

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
    # VPN/datacenter signal (owner ask 2026-06-14). `org` is the ipinfo "AS#### Provider" string;
    # `is_vpn` is a HEURISTIC flag set when that org/ASN looks like a datacenter/hosting/VPN provider
    # (consumer traffic comes from ISP ASNs, not datacenters). It is a SIGNAL for admin review (paired
    # with the account-overlap view), never an auto-block. See afc_auth.views.looks_like_vpn.
    org = models.CharField(max_length=120, null=True, blank=True)
    is_vpn = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class Roles(models.Model):
    ROLES = [
        # super_admin sits ABOVE head_admin: full access, and can only be granted/removed by another
        # super_admin (a head_admin cannot touch it). Seeded + assigned on prod via the
        # `ensure_super_admin` management command. See afc_auth/views.py role-edit guards.
        ("super_admin", "Super Admin"),
        ("head_admin", "Head Admin"),
        ("metrics_admin", "Metrics Admin"),
        ("shop_admin", "Shop Admin"),
        ("news_admin", "News Admin"),
        ("event_admin", "Event Admin"),
        ("teams_admin", "Teams Admin"),
        ("partner_admin", "Partner Admin"),
        ("sponsor_admin", "Sponsor Admin"),
        ("organizer", "Organizer"),              # granted to any active OrganizationMember
        ("organizer_admin", "Organizer Admin"),  # AFC staff who oversee organizations
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
    updated_at = models.DateTimeField(auto_now=True)
    


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


class NewsLike(models.Model):
    news = models.ForeignKey(News, on_delete=models.CASCADE, related_name="likes")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("news", "user")  # Ensure a user can like a news item only once


class NewsDislike(models.Model):
    news = models.ForeignKey(News, on_delete=models.CASCADE, related_name="dislikes")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("news", "user")  # Ensure a user can dislike a news item only once


class NewsViews(models.Model):
    news = models.ForeignKey(News, on_delete=models.CASCADE, related_name="views")
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)  # Allow null for anonymous views
    created_at = models.DateTimeField(auto_now_add=True)
    viewer_ip = models.CharField(max_length=45, null=True, blank=True)
    viewer_user_agent = models.TextField(null=True, blank=True)

    class Meta:
        unique_together = ("news", "user")  # Ensure a user can view a news item only once
    

class AdminHistory(models.Model):
    action_id = models.AutoField(primary_key=True)
    # admin_user is nullable to match the production schema (the prod column has long been
    # nullable). Aligning the model avoids makemigrations trying to alter the column to
    # non-nullable on prod (which prompts for a one-off default). Making a field nullable
    # never needs a default, so prod migrations stay prompt-free.
    admin_user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    action = models.CharField(max_length=50)  # e.g., "banned_player", "edited_news"
    description = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)


class AuditLog(models.Model):
    """
    Sitewide AUTOMATIC admin audit log.

    AdminHistory (above) is written by hand in ~40 scattered places and only covers a handful of
    apps. AuditLog instead records EVERY mutating request (POST/PUT/PATCH/DELETE) made by a user who
    holds an admin/staff role, with zero per-view code, so a new admin endpoint is covered the moment
    it ships. This is the "sitewide automatic" audit log the project asked for.

    How it connects end to end:
      - Written by : afc_auth/middleware.py -> AuditLogMiddleware (best-effort, never raises). It
                     resolves the acting User from the Bearer SessionToken (same mechanism as
                     afc_auth.views.validate_token) and logs only admin/staff actors.
      - Read by    : afc_auth/views.py -> get_audit_log  (GET /auth/get-audit-log/, admin-gated,
                     paginated + filtered with the afc_partner_api {results, has_more, ...} envelope).
      - Surfaced on: frontend app/(a)/a/history/page.tsx (the admin "History" page).

    Snapshot fields (actor_username / actor_role) are copied at write time so a row stays meaningful
    even if the User is later renamed or deleted (actor FK is SET_NULL). `metadata` holds a small,
    REDACTED JSON blob (URL kwargs + query params with secret-looking keys stripped) - never raw
    request bodies, passwords or tokens (project security rule: never log secrets/PII).
    """
    id = models.AutoField(primary_key=True)

    # ── who acted (FK + snapshots) ──────────────────────────────────────────────────────────────
    # actor may become null if the User is later deleted; the username/role snapshots below preserve
    # who it was at the moment of the action.
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    actor_username = models.CharField(max_length=40, db_index=True)        # snapshot of actor.username
    actor_role = models.CharField(max_length=160, blank=True, default="")  # snapshot e.g. "admin" or "player+shop_admin"

    # ── what happened ───────────────────────────────────────────────────────────────────────────
    # `summary` is the HUMAN-READABLE short form shown in the table (e.g. "Edited an event #163"),
    # computed in the middleware from the action slug + target. `action` is the underlying slug
    # (e.g. "edit_event") kept for filtering; `view_name` is the fully-qualified view for debugging.
    # `path`/`method` are the raw request line (shown in the expandable details, not the table).
    summary = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=120, db_index=True)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=512)
    view_name = models.CharField(max_length=255, blank=True, default="")

    # Best-effort target identity, pulled from the resolved URL kwargs (e.g. {"product_id": 3} ->
    # target_type="product_id", target_id="3").
    target_type = models.CharField(max_length=120, blank=True, default="")
    target_id = models.CharField(max_length=120, blank=True, default="")

    # ── result + request context ────────────────────────────────────────────────────────────────
    status_code = models.PositiveIntegerField(null=True, blank=True)
    ip_address = models.CharField(max_length=45, blank=True, default="")   # IPv6-safe, matches LoginHistory
    user_agent = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)                  # redacted kwargs + query params

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]  # newest first; the read endpoint relies on this default order
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.actor_username} {self.method} {self.action} ({self.status_code}) @ {self.created_at:%Y-%m-%d %H:%M}"


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

    # ── notification DEEP-LINKING (owner 2026-06-15) ───────────────────────────────────────────────
    # An admin picks WHAT a notification points at when composing it; the user then gets a "Take me
    # there" button that opens that exact frontend page. `target_type` says which kind of page,
    # `target_id` carries the lookup value for that page:
    #   event     -> Event slug (or id)      -> /tournaments/<slug-or-id>
    #   news      -> News slug (or id)        -> /news/<slug-or-id>
    #   team      -> team_id                  -> /teams/<id>
    #   player    -> username                 -> /players/<username>
    #   shop      -> (ignored)               -> /shop
    #   organizer -> Organization slug (or id)-> /organizations/<slug-or-id>
    #   custom    -> a full RELATIVE path     -> target_id used as-is (must start with "/")
    #   none / "" -> no link (the "Take me there" button is hidden)
    # The actual URL is NOT stored: it is built on read by afc_auth.notification_links.build_notification_link
    # and returned as `link` by get_notifications, so a slug change reflects automatically.
    # WRITTEN BY: afc_auth.send_notification / send_notification_to_multiple_users / admin_send_message
    # and the tournament broadcast path (afc_auth.deliver_broadcast, called from
    # afc_tournament_and_scrims.broadcast_announcement / broadcast_to_group).
    # READ BY: afc_auth.get_notifications -> frontend notifications dropdown / page "Take me there" button.
    TARGET_TYPE_CHOICES = [
        ("event", "Tournament/Event"),
        ("news", "News"),
        ("team", "Team"),
        ("player", "Player"),
        ("shop", "Shop"),
        ("organizer", "Organizer"),
        ("custom", "Custom URL"),
        ("none", "No link"),
    ]
    target_type = models.CharField(max_length=20, blank=True, default="", choices=TARGET_TYPE_CHOICES)
    target_id = models.CharField(max_length=120, blank=True, default="")  # slug / id / username, or a relative URL for "custom"

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


class TranslationCache(models.Model):
    """
    i18n Phase 1 (owner 2026-06-15): persistent cache of machine translations.

    WHY: the translation engine (afc_auth/translation.py) calls Gemini to translate UI strings and
    content from English into French / Portuguese. Gemini calls cost money and are rate-limited, and
    the same strings (button labels, news bodies, product names) get requested over and over. We
    therefore cache every (source text, target language) result here so a repeat translation is a
    single indexed DB read instead of a fresh API call. This is what makes the engine cheap enough
    to run on every request.

    How it connects end to end:
      - Written/read by: afc_auth/translation.py -> translate() / translate_batch(). On a miss the
                         engine calls Gemini, stores the result here, and returns it; on a hit it
                         returns translated_text directly and NEVER touches the API.
      - Keyed by      : source_hash = sha256(source_text + target_lang). Combining the target lang
                        into the hash input (and ALSO into the unique_together) means the same English
                        string cached for "fr" and for "pt" are two distinct rows that never collide.
      - Consumed by   : any caller that localizes output - request handlers using
                        afc_auth.locale_middleware.get_locale(request), the build-time catalog script,
                        and bulk content localization (news, products) via translate_richtext().

    Note: this stores ONLY public UI text and content (no PII, tokens, or secrets), in line with the
    project rule against logging sensitive data.
    """
    # sha256 hex digest of (source_text + target_lang). 64 chars, indexed for fast lookups. We hash
    # rather than store the raw source as the key because source bodies (e.g. a full news article)
    # can be arbitrarily long and would not make a usable index key.
    source_hash = models.CharField(max_length=64, db_index=True)

    # 2-letter language codes ("en"/"fr"/"pt"), matching afc_auth.models.User.LANGUAGE_CHOICES.
    source_lang = models.CharField(max_length=2)   # almost always "en" (our source of truth)
    target_lang = models.CharField(max_length=2)   # the language we translated INTO

    # The cached translation in target_lang. TextField because content bodies can be long.
    translated_text = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One cached translation per (source string, target language). The hash already folds the
        # target lang in, but we still scope uniqueness by target_lang explicitly so the constraint
        # reads correctly and stays correct even if the hashing scheme ever changes.
        unique_together = ("source_hash", "target_lang")

    def __str__(self):
        return f"{self.source_lang}->{self.target_lang} {self.source_hash[:12]}"




