# afc_organizers/models.py
# ──────────────────────────────────────────────────────────────────────────────
# Organization + membership models for the Organizer feature.
#
# An Organization is an AFC-provisioned tenant under which external organizers run
# tournaments (the events themselves reuse afc_tournament_and_scrims — an Event simply
# gains a nullable `organization` FK). OrganizationMember connects EXISTING user accounts
# to an org with a role (owner / sub_organizer) and granular per-member permissions.
#
# Permission checks never read these rows directly — they go through
# afc_organizers/permissions.py::org_can so the owner/admin-bypass rules live in one place.
# Full spec: WEBSITE/tasks/organizers-design.md.
# ──────────────────────────────────────────────────────────────────────────────
from django.conf import settings
from django.db import models


class Organization(models.Model):
    """An AFC-provisioned organizer tenant. Owns events, branding, and members."""

    STATUS_CHOICES = [
        ("active", "Active"),
        ("suspended", "Suspended"),   # reversible freeze (org hidden, no actions)
        ("deleted", "Deleted"),       # soft-delete; events are re-homed to AFC (FK SET_NULL)
    ]

    organization_id = models.AutoField(primary_key=True)
    slug = models.SlugField(max_length=140, unique=True)          # public handle: /organizations/<slug>
    name = models.CharField(max_length=120)
    logo = models.ImageField(upload_to="organization_logos/", null=True, blank=True)
    default_banner = models.ImageField(upload_to="organization_banners/", null=True, blank=True)
    email = models.EmailField(null=True, blank=True)              # public contact only — not auth
    description = models.TextField(blank=True, default="")
    socials = models.JSONField(default=dict, blank=True)          # {"x","instagram","youtube","discord"}
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="active")
    created_by = models.ForeignKey(                              # the AFC admin who provisioned it
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="provisioned_organizations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.slug})"


# Canonical list of the granular permission columns on OrganizationMember. Used by the
# member-management endpoints (to whitelist which toggles a request may set) and by tests.
PERMISSION_FIELDS = (
    "can_create_events",
    "can_edit_events",
    "can_upload_results",
    "can_manage_registrations",
    "can_submit_designs",
    "can_view_metrics",
    "can_view_reviews",
    "can_manage_members",
)


class OrganizationMember(models.Model):
    """Connects a user to an organization. Owner has every permission implicitly; a
    sub_organizer only has the toggles the owner granted (see permissions.org_can)."""

    ROLE_CHOICES = [("owner", "Owner"), ("sub_organizer", "Sub-organizer")]
    STATUS_CHOICES = [("active", "Active"), ("removed", "Removed")]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organization_memberships"
    )
    role = models.CharField(max_length=14, choices=ROLE_CHOICES, default="sub_organizer")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="organization_invites_sent",
    )

    # ── granular permissions (owner has all implicitly; see permissions.org_can) ──
    can_create_events = models.BooleanField(default=False)
    can_edit_events = models.BooleanField(default=False)
    can_upload_results = models.BooleanField(default=False)        # results + leaderboards
    can_manage_registrations = models.BooleanField(default=False)  # approve/reject teams & players
    can_submit_designs = models.BooleanField(default=False)        # leaderboard-design requests
    can_view_metrics = models.BooleanField(default=False)
    can_view_reviews = models.BooleanField(default=False)          # ratings + organizer-only comments
    can_manage_members = models.BooleanField(default=False)        # add/remove subs, toggle perms

    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("organization", "user")

    def __str__(self):
        return f"{self.user_id} @ {self.organization_id} ({self.role})"
