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
    # ── IP-derived country, drives the per-PLAYER flag (owner ask 2026-06-29) ──────────────
    # `country` (above) is the player's PROFILE country: set once at signup or hand-edited, and it
    # is what the TEAM flag is derived from (afc_team.views._derive_team_country). The owner wants
    # the flag shown next to a PLAYER's name to reflect where that player actually IS, not the
    # team's country, so we denormalize the latest IP-resolved country here. It is refreshed on
    # every login (login / google_auth / discord) by set_ip_country() in views.py, skipped when the
    # connection looks like a VPN/datacenter so an exit node can't mislabel the flag. Readers do
    # `ip_country or country` (profile country is the fallback): afc_team.views.get_team_details
    # (roster flag), afc_player.aggregation (public player profile), afc_player.views (admin detail).
    # Denormalized onto User (not read from LoginHistory) so list/roster serializers avoid an N+1.
    ip_country = models.CharField(max_length=40, blank=True, default='')
    # ── Preferred display currency (multi-currency, owner 2026-06-30) ──────────────────────
    # The platform stores money in USD and shows each user their own currency. This is the user's
    # chosen display currency (ISO-4217, e.g. "NGN", "USD", "GHS"). BLANK = not chosen -> the
    # resolver (afc_auth.fx.user_currency) derives it from the user's country (ip_country/country),
    # falling back to USD. An explicit pick in profile settings stores it here and is never
    # auto-overridden. Display-only: it never changes how money is STORED (always USD), only how the
    # FE renders it (lib/money formatMoney via the /auth/fx-rates/ rates).
    preferred_currency = models.CharField(max_length=3, blank=True, default='')
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

    # One-time NEW-USER ONBOARDING (owner 2026-06-20): the skippable first-login flow that walks a
    # brand-new account through the usual site requirements (upload esports image, set Free Fire UID,
    # add a profile picture). Flipped True when the user finishes OR skips the flow.
    #
    # DEFAULT TRUE on purpose: this field was added to an existing 6k-user base, and the AddField
    # default backfills EVERY existing row - defaulting True means the whole existing userbase is
    # treated as already-onboarded and is NOT swept into the flow. Genuinely NEW accounts get False
    # set EXPLICITLY at creation (signup() + google_auth()), so only they see onboarding. Read by
    # get_user_profile (frontend AuthContext -> OnboardingGate); written by complete_onboarding.
    has_completed_onboarding = models.BooleanField(default=True)

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

    # Per-user STATS PRIVACY opt-in (owner 2026-06-27). The 2026-06-24 lockdown (v7.0.64) already
    # hides every player's individual stats from organizers/sponsors/non-members; this flag is the
    # USER's own switch on TOP of that, deciding whether ordinary viewers (other players, the public)
    # may see their individual player-profile stats. DEFAULT FALSE = hidden: stats stay private until
    # the user explicitly opts in. Self + AFC admins (is_stats_admin) ALWAYS see the stats regardless,
    # so a user never loses sight of their own numbers.
    #   - Read by  : afc_player.views._can_view_player_stats (the gate that decides if a viewer sees a
    #                player's stats) and afc_auth.views.get_user_profile (returned so the FE settings
    #                toggle shows the current value).
    #   - Written by: afc_auth.views.edit_profile (the "Show my stats to others" switch in profile edit).
    # Companion: Team.stats_visible (afc_team) is the team-level equivalent, set by the owner/manager.
    stats_visible = models.BooleanField(default=False)

    # LETTER AVATARS (owner 2026-06-29). Free Fire ships a fixed "letter avatar" per letter (A-Z); a
    # player declares which ones they OWN here. Stored canonical: UPPERCASE single A-Z chars, de-duped,
    # sorted (e.g. ["A", "C", "Z"]) - the same normalization the shared FE picker applies (see
    # frontend/components/ui/letter-avatar-picker.tsx and afc_auth.views.normalize_letter_avatars).
    # Default [] = owns none. Lives on User (not UserProfile) to match uid / stats_visible, which
    # edit_profile + get_user_profile already touch (precedent: seen_dashboard_intros).
    #   - Read by  : afc_auth.views.get_user_profile (echo so the FE picker seeds itself); the team
    #                "available letters" union (afc_team.Team derives union(member.letter_avatars) +
    #                Team.manual_letter_avatars, LIVE - never stored); the event register-gate that
    #                counts a roster's available letters (afc_tournament_and_scrims.register_for_event,
    #                Event.min_letter_avatars).
    #   - Written by: afc_auth.views.edit_profile (the "Letter avatars" picker in profile edit).
    letter_avatars = models.JSONField(default=list, blank=True)

    USERNAME_FIELD = "username"  # Set in_game_name as username
    REQUIRED_FIELDS = ["email", "full_name"]

    def save(self, *args, **kwargs):
        # Trim-on-save for name fields (owner 2026-06-20). Seed data carried stray
        # leading/trailing whitespace in usernames/names (~21% of users), which breaks
        # name-based lookups: SQL `=` ignores only TRAILING spaces (MySQL PADSPACE) and
        # `__iexact`/LIKE ignores neither. Stripping here is the single chokepoint that
        # keeps every new + edited row clean regardless of which view wrote it; the
        # clean_name_whitespace management command backfills the existing rows.
        for f in ("username", "full_name", "uid"):
            v = getattr(self, f, None)
            if isinstance(v, str):
                setattr(self, f, v.strip())
        super().save(*args, **kwargs)

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

    # Idle-timeout window: users are auto-logged-out after 3 HOURS OF INACTIVITY (owner 2026-06-14,
    # kept at 3h 2026-07-01). SLIDING, not absolute-from-login: every authed request calls touch() to
    # push expires_at forward, so an active user never gets logged out, but 3h with no request expires
    # the session. MUST stay in sync with the frontend auth_token cookie window (AuthContext
    # COOKIE_OPTIONS.expires, also 3h): if the FE cookie outlives this, the user still LOOKS logged in
    # after the backend token expires and every action 401s "Invalid or expired session token". Both at
    # 3h => on a >3h idle gap the cookie ALSO expires, so the FE cleanly shows logged-out (re-login).
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

    # ── Scheduled publish / auto-release (owner: News "schedule publish" feature) ──────────────
    # Two fields drive a timer-based release:
    #   • is_published        - the public visibility gate. Default True so every PRE-EXISTING news
    #                           row stays visible after this migration (nothing was hidden before).
    #                           A news item is only shown on the PUBLIC list/detail when this is True.
    #   • scheduled_publish_at - the future moment the item should go live. When an admin schedules a
    #                           post for later, create_news sets this to that (timezone-aware, UTC)
    #                           datetime and is_published=False, so the post is created HIDDEN.
    #
    # How it connects end to end:
    #   - Written by : afc_auth.views.create_news / edit_news (admin create/edit endpoints) when the
    #                  optional `scheduled_publish_at` field is sent from the admin News form
    #                  (frontend app/(a)/a/news/create + [slug]/edit).
    #   - Flipped by : afc_auth.tasks.publish_scheduled_news - a Celery beat task (every minute, see
    #                  afc/celery_config.py 'publish_scheduled_news_every_minute') that sets
    #                  is_published=True on any due item (scheduled_publish_at <= now). It KEEPS
    #                  scheduled_publish_at so the admin can still see "this went live at <time>".
    #   - Filtered by: afc_auth.views.get_all_news (public list) + get_news_detail (public detail)
    #                  exclude is_published=False for non-admin viewers; the ADMIN list still shows
    #                  scheduled items with their time + a "Scheduled" state (is_published is exposed
    #                  to admin callers so the frontend can render the badge).
    scheduled_publish_at = models.DateTimeField(null=True, blank=True)
    is_published = models.BooleanField(default=True)

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

    # ── MULTI deep-link targets (owner 2026-06-17) ─────────────────────────────────────────────────
    # A broadcast can now be tied to MORE THAN ONE entity (the link picker lets an admin search and
    # select several events). Each entry mirrors the single target_type/target_id pair above:
    #   [{"target_type": "event", "target_id": "dynasty-cup"}, {"target_type": "event", "target_id": "rush"}]
    # target_type/target_id (above) keep holding the FIRST target for backward compatibility (old
    # readers + the single-link "Take me there"); `targets` holds the full list. get_notifications
    # turns it into a `links` array (one "View" per linked entity). Written by deliver_broadcast.
    targets = models.JSONField(default=list, blank=True)

    def mark_as_read(self):
        self.is_read = True
        self.save()


class SentBroadcast(models.Model):
    """Audit/history record of ONE broadcast send (owner 2026-06-17).

    The per-recipient `Notifications` rows can't answer "what was sent, when, by whom, to how many,
    and to which group/stage/event" — there's no sender, no grouping key, no recipient snapshot. This
    model is that history: deliver_broadcast writes exactly ONE row per send (it is the single delivery
    chokepoint), so admins + organizers can review every announcement / room-details push.

    Consumed by:
      • afc_tournament_and_scrims.get_broadcast_history -> GET /events/broadcast-history/?event_id=
        (admin event "Communication" view + organizer event leaderboard/communication view): event-scoped.
      • afc_auth.get_general_broadcast_history -> GET /auth/broadcast-history/ (admin Settings >
        Notifications tab): the general (scope general/direct) sends not tied to an event.
    Written by: afc_auth.deliver_broadcast (every push/email broadcast funnels through it).
    """
    SCOPE_CHOICES = [
        ("general", "General"),            # admin Settings broadcast to selected users
        ("event", "Whole event"),          # all registered competitors of an event
        ("stage", "Stage"),                # every group/lobby in a stage
        ("group", "Group"),                # one group/lobby
        ("room_details", "Room details"),  # room id/name/password push to a group
        ("direct", "Direct message"),      # a single player or team
    ]
    DELIVERY_CHOICES = [("push", "App only"), ("email", "Email only"), ("both", "App + Email")]

    id = models.AutoField(primary_key=True)
    # Who sent it. SET_NULL + a username snapshot so the history survives a deleted admin account.
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_broadcasts")
    sender_username = models.CharField(max_length=150, blank=True, default="")
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default="general")
    title = models.CharField(max_length=255, null=True, blank=True)
    message = models.TextField(blank=True, default="")
    delivery = models.CharField(max_length=10, choices=DELIVERY_CHOICES, default="both")
    recipient_count = models.PositiveIntegerField(default=0)  # how many users this reached

    # Where it went (nullable; only the relevant ones are set per scope). event is a real FK so the
    # event-scoped history query is a simple filter; stage/group are id + name snapshots (no extra
    # cross-app FKs) since they're only for display/filtering.
    event = models.ForeignKey("afc_tournament_and_scrims.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_broadcasts")
    stage_id = models.PositiveIntegerField(null=True, blank=True)
    stage_name = models.CharField(max_length=120, blank=True, default="")
    group_id = models.PositiveIntegerField(null=True, blank=True)
    group_name = models.CharField(max_length=120, blank=True, default="")

    # Snapshot of the deep-link targets the message carried (same shape as Notifications.targets).
    targets = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def to_history_dict(self):
        """Plain dict for the broadcast-history API (shared by both history endpoints).

        Pure data (no link building): the history surfaces show WHAT was sent, WHEN, BY WHOM, the
        scope (human label), recipient count, the event/stage/group it targeted, and the deep-link
        targets snapshot. `created_at` is the UTC instant; the FE renders it via <LocalTime>."""
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_label": self.get_scope_display(),
            "title": self.title or "",
            "message": self.message or "",
            "delivery": self.delivery,
            "recipient_count": self.recipient_count,
            "sender_username": self.sender_username or "",
            "event_id": self.event_id,
            "event_name": getattr(self.event, "event_name", "") if self.event_id else "",
            "stage_id": self.stage_id,
            "stage_name": self.stage_name or "",
            "group_id": self.group_id,
            "group_name": self.group_name or "",
            "targets": self.targets or [],
            "created_at": self.created_at,
        }

    
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


# ══════════════════════════════════════════════════════════════════════════════
#  PLAYER-TO-PLAYER REPORTS  (owner 2026-06-20)
# ──────────────────────────────────────────────────────────────────────────────
#  Lets ANY player report ANOTHER player with proof + notes; admins review every
#  report and ANSWER it (the answer is shown back to the reporter). When a single
#  player collects 3+ reports inside a rolling 2-week window, a "repeat offender"
#  flag is surfaced to admins (computed on read - see views_player_reports.
#  _recent_report_count - so it needs no extra column).
#
#  Modelled field-for-field on afc_player_market.MarketReport so the original dev
#  reads it the same way; the difference is the SUBJECT is always a User (a player),
#  there is no post/team, and `admin_response` is a reporter-FACING answer (vs
#  MarketReport.resolution_notes which is an internal triage note).
#
#  Consumed by (afc_auth/views_player_reports.py + afc_auth/urls.py):
#    • POST  /auth/report-player/                 (file_player_report)   — any user
#    • GET   /auth/my-player-reports/             (my_player_reports)    — reporter
#    • GET   /auth/admin/player-reports/          (admin_list_player_reports)
#    • PATCH /auth/admin/player-reports/<id>/     (admin_respond_player_report)
#  Frontend: ReportPlayerDialog on app/(user)/players/[username] (file) + the
#  "Player Reports" tab on the admin dashboard (triage + answer), and a "My reports"
#  view where the reporter reads the admin's answer.
# ══════════════════════════════════════════════════════════════════════════════
class UserReport(models.Model):
    """An abuse report a user files against another PLAYER or a whole TEAM.

    subject_type says which: "player" -> reported_user is set; "team" -> reported_team
    is set (owner 2026-06-20, "player and team reports"). reporter / reported_user /
    reported_team are all SET_NULL so deleting an account or team does NOT erase the
    abuse record (only the link goes null) - the history must outlive the subject,
    exactly like afc_player_market.MarketReport.
    """

    # Whether the report targets a single player or a whole team.
    SUBJECT_TYPE_CHOICES = [
        ("player", "Player"),
        ("team", "Team"),
    ]

    # Report reasons. Kept deliberately close to MarketReport.CATEGORY_CHOICES plus
    # the player-vs-player specifics (cheating, toxicity, impersonation). Shared by
    # player + team reports (all categories apply to a team too).
    CATEGORY_CHOICES = [
        ("cheating", "Cheating / hacking"),
        ("toxicity", "Toxic / abusive behaviour"),
        ("harassment", "Harassment"),
        ("impersonation", "Impersonation / fake identity"),
        ("scam", "Scam / fraud"),
        ("other", "Other"),
    ]

    # Triage lifecycle (mirrors MarketReport minus the "banned" terminal state -
    # banning a player is the existing site-wide BannedPlayer flow, not this queue).
    STATUS_CHOICES = [
        ("open", "Open"),
        ("reviewing", "Reviewing"),
        ("resolved", "Resolved"),
        ("dismissed", "Dismissed"),
    ]

    # ── who reported whom ──
    reporter = models.ForeignKey(
        "afc_auth.User", null=True, on_delete=models.SET_NULL,
        related_name="user_reports_filed",
    )
    subject_type = models.CharField(
        max_length=10, choices=SUBJECT_TYPE_CHOICES, default="player",
    )
    # Exactly one of these is set, per subject_type. Both SET_NULL so a deleted
    # player/team only nulls the link, never erases the report.
    reported_user = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="user_reports_against",
    )
    reported_team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="user_reports_against",
    )

    # ── the report body (category + required notes + optional proof image) ──
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="other")
    details = models.TextField()                       # required free text ("notes")
    evidence = models.ImageField(
        upload_to="player_report_evidence/", null=True, blank=True
    )                                                  # optional proof screenshot

    # ── triage + the admin's reporter-facing ANSWER ──
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="open")
    reviewed_by = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="user_reports_reviewed",
    )
    admin_response = models.TextField(blank=True, default="")  # shown to the reporter

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The repeat-offender check filters by subject + created_at window, so
            # index both subject FKs paired with created_at for a cheap COUNT.
            models.Index(fields=["reported_user", "created_at"]),
            models.Index(fields=["reported_team", "created_at"]),
        ]

    def __str__(self):
        if self.subject_type == "team":
            who = self.reported_team.team_name if self.reported_team else "deleted team"
        else:
            who = self.reported_user.username if self.reported_user else "deleted"
        by = self.reporter.username if self.reporter else "deleted"
        return f"Report of {who} by {by} ({self.status})"


# ══════════════════════════════════════════════════════════════════════════════
#  WATCHLIST  (owner 2026-06-21)
# ──────────────────────────────────────────────────────────────────────────────
#  A SHARED, AFC-WIDE ADVISORY watchlist of suspicious players + teams. Distinct from
#  BannedPlayer / TeamBan / afc_organizers.OrganizerBlacklist (which all BLOCK): this one
#  ONLY WARNS. Every AFC admin AND every organizer sees the same entries; both can add and
#  clear them (added_by + reason are recorded for accountability). Wherever an admin/organizer
#  sees a flagged name (registered teams, rosters, leaderboard standings + results entry, the
#  upload review, team/player admin pages) a "Watch" tag is shown, and there is a dedicated
#  Watchlist tab in the admin + organizer dashboards. It NEVER blocks registration/play/results;
#  it only raises a soft heads-up Notifications row to the event's organizer(s)+admins when a
#  watched team/player registers (afc_tournament_and_scrims.register_for_event / add_teams_*).
#
#  One ACTIVE row per subject (a player OR a team) — re-flagging a cleared subject reactivates
#  the same logical entry. player/team are SET_NULL so a deleted account/team only nulls the link.
#
#  Consumed by (afc_auth/views_watchlist.py + afc_auth/urls.py, prefix auth/):
#    • GET   auth/watchlist/        list (filter subject_type/status, search, paginate)
#    • POST  auth/watchlist/        add  {subject_type, player_id|team_id, reason, source?, context?}
#    • PATCH auth/watchlist/<id>/   clear / reactivate
#    • GET   auth/watchlist/tags/   bulk "which of these ids are watched" (renders <WatchTag> N+1-free)
#  Gate: afc_auth.watchlist_permissions.can_use_watchlist (AFC admin OR any active organizer).
#  Frontend: /a/watchlist (admin, i18n-exempt) + /organizer/watchlist (i18n en/fr/pt),
#  components <WatchTag>, lib/watchlist.ts. Spec: WEBSITE/tasks/watchlist-spec.md.
# ══════════════════════════════════════════════════════════════════════════════
class WatchlistEntry(models.Model):
    SUBJECT_TYPE_CHOICES = [
        ("player", "Player"),
        ("team", "Team"),
    ]
    STATUS_CHOICES = [
        ("active", "Active"),    # currently watched (tag shows, warnings fire)
        ("cleared", "Cleared"),  # soft-cleared; kept for audit, no tag/warning
    ]
    SOURCE_CHOICES = [
        ("manual", "Added manually"),
        ("upload", "Added from a leaderboard upload mismatch"),
    ]

    watch_id = models.AutoField(primary_key=True)
    subject_type = models.CharField(max_length=10, choices=SUBJECT_TYPE_CHOICES, default="player")
    # Exactly one of these is set, per subject_type. SET_NULL: a deleted player/team only nulls the
    # link, never erases the watch history (same rule as UserReport).
    player = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="watchlist_entries_against",
    )
    team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="watchlist_entries_against",
    )

    reason = models.TextField()  # required: why they are being watched
    added_by = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="watchlist_entries_added",
    )
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="manual")
    # Free-text provenance for an upload-sourced flag, e.g. "event 130, uid 1270915668 (TC• M KING)".
    context = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")
    cleared_by = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="watchlist_entries_cleared",
    )
    cleared_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The bulk tags lookup filters active entries by subject FK, so index each FK.
            models.Index(fields=["player", "status"]),
            models.Index(fields=["team", "status"]),
        ]

    @property
    def subject_name(self):
        """Display name of the watched subject (for tags, the tab, notifications)."""
        if self.subject_type == "team":
            return self.team.team_name if self.team else "deleted team"
        return self.player.username if self.player else "deleted player"

    def __str__(self):
        by = self.added_by.username if self.added_by else "deleted"
        return f"Watch {self.subject_type} {self.subject_name} by {by} ({self.status})"


# ══════════════════════════════════════════════════════════════════════════════
#  FAN / HATER SENTIMENT  (owner 2026-06-20)
# ──────────────────────────────────────────────────────────────────────────────
#  A fun, public reaction on a player or team profile: any logged-in user can tap
#  "I'm a fan" or "I'm a hater" ONCE per subject. The two are mutually exclusive
#  (one stance per voter per subject); tapping the active stance again clears it,
#  tapping the other switches. The fan + hater COUNTS are shown publicly on the
#  profile to everyone.
#
#  One row per (voter, subject). Both target FKs are SET_NULL so a deleted account
#  /team only nulls the link. Enforced-unique per subject in the view via
#  get_or_create(voter, target_user|target_team).
#
#  Consumed by (afc_auth/views_sentiment.py + afc_auth/urls.py):
#    • POST /auth/sentiment/set/   (set_sentiment)  — toggle/switch, returns counts
#    • GET  /auth/sentiment/       (get_sentiment)  — public counts + my stance
#  Frontend: components/profile/FanHater.tsx on the player profile + team page.
# ══════════════════════════════════════════════════════════════════════════════
class ProfileSentiment(models.Model):
    SUBJECT_TYPE_CHOICES = [
        ("player", "Player"),
        ("team", "Team"),
    ]
    STANCE_CHOICES = [
        ("fan", "Fan"),
        ("hater", "Hater"),
    ]

    voter = models.ForeignKey(
        "afc_auth.User", on_delete=models.CASCADE, related_name="sentiments_given",
    )
    subject_type = models.CharField(max_length=10, choices=SUBJECT_TYPE_CHOICES)
    target_user = models.ForeignKey(
        "afc_auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sentiments_received",
    )
    target_team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sentiments_received",
    )
    stance = models.CharField(max_length=6, choices=STANCE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One stance row per voter per player, and per voter per team. NO condition:
            # MySQL does not support partial/conditional unique constraints (warning W036)
            # and would silently skip them. A plain unique on (voter, target_user) is safe
            # because MySQL treats NULLs as DISTINCT - team-sentiment rows (target_user=NULL)
            # never collide on it, and player rows are deduped; the (voter, target_team) pair
            # mirrors it for teams. So this enforces the intended one-stance-per-subject on MySQL.
            models.UniqueConstraint(
                fields=["voter", "target_user"],
                name="unique_voter_player_sentiment",
            ),
            models.UniqueConstraint(
                fields=["voter", "target_team"],
                name="unique_voter_team_sentiment",
            ),
        ]
        indexes = [
            # Counting fans/haters per subject = group by target + stance.
            models.Index(fields=["target_user", "stance"]),
            models.Index(fields=["target_team", "stance"]),
        ]

    def __str__(self):
        tgt = self.target_team.team_name if self.subject_type == "team" and self.target_team else (
            self.target_user.username if self.target_user else "deleted")
        return f"{self.voter_id} is {self.stance} of {tgt}"






# ─────────────────────────────────────────────────────────────────────────────
# FxRate — cached USD->currency exchange rates (multi-currency, owner 2026-06-30).
#
# WHY: the platform stores money in USD and shows each user their local currency, so we need
# live FX rates. One row per ISO-4217 currency, `rate` = units of that currency per 1 USD
# (matching the open.er-api.com / exchangerate-api "USD base" payload). Refreshed lazily (see
# afc_auth.fx.get_rates: re-fetch when the newest row is stale) so no Celery-beat/cron is needed.
# Durable (DB) so a brief FX-API outage falls back to the last-good rates instead of breaking
# every money render.
#
# HOW IT CONNECTS:
#   - Written by afc_auth.fx.refresh_fx_rates() (hits the free no-key FX API).
#   - Read by afc_auth.fx.get_rates()/convert() and served by the /auth/fx-rates/ endpoint, which
#     the frontend lib/fx.ts fetches + caches; lib/money.ts formatMoney() converts USD->user currency.
# ─────────────────────────────────────────────────────────────────────────────
class FxRate(models.Model):
    currency = models.CharField(max_length=3, unique=True, db_index=True)  # ISO-4217, e.g. "NGN"
    rate = models.DecimalField(max_digits=20, decimal_places=8)            # units per 1 USD
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"1 USD = {self.rate} {self.currency}"
