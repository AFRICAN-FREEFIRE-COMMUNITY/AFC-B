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

    # ── Paid-event terms (feature "paid-events", 2026-06-08) ──
    # An organizer must read + accept the paid-event terms (escrow held by the processor, AFC
    # releases to the organizer only after the event runs, first 10 paid tournaments per org are
    # 0% fee then AFC takes 2%, refund handling) BEFORE creating their first PAID event. We record
    # WHEN they accepted + WHO + which terms version, so a later terms change can re-prompt.
    # Set by create_event when an org first submits a paid event with terms accepted; read by the
    # FE to decide whether to show the terms modal. Null = not yet accepted.
    paid_terms_accepted_at = models.DateTimeField(null=True, blank=True)
    paid_terms_accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="accepted_org_paid_terms",
    )
    paid_terms_version = models.CharField(max_length=20, blank=True, default="")

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


# ════════ Phase 3 — leaderboard-design request (organizer submits → AFC builds) ════════


class LeaderboardDesignRequest(models.Model):
    """An organizer's request for a custom look for their leaderboards/results. The organizer
    submits a reference image + notes; an AFC designer builds it and marks it applied (the
    "design request → AFC builds it" decision). Human-in-the-loop — no self-serve renderer."""

    STATUS_CHOICES = [
        ("open", "Open"),               # submitted, awaiting AFC
        ("in_progress", "In progress"),  # an AFC designer is building it
        ("applied", "Applied"),          # built + live for the org's results
        ("rejected", "Rejected"),        # AFC declined (see resolution_notes)
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE,
                                     related_name="design_requests")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                     related_name="leaderboard_design_requests")
    title = models.CharField(max_length=140)
    notes = models.TextField(blank=True, default="")          # what the organizer wants
    reference_image = models.ImageField(upload_to="leaderboard_design_refs/", null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="open")
    resolution_notes = models.TextField(blank=True, default="")  # AFC's reply / build notes
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="handled_design_requests")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DesignRequest({self.organization_id}: {self.title} [{self.status}])"


# ════════ Phase 4 — reports, ratings & comments ════════


class OrganizationReport(models.Model):
    """A user-submitted report against an organization (e.g. suspected results manipulation
    to game the rankings). Carries a category, written details, and optional evidence. AFC
    reviews + resolves; resolution can suspend the org and/or exclude the event from rankings
    via the existing afc_rankings tools."""

    CATEGORY_CHOICES = [
        ("rankings_manipulation", "Rankings manipulation"),
        ("fake_results", "Fake / falsified results"),
        ("unfair_conduct", "Unfair conduct"),
        ("other", "Other"),
    ]
    STATUS_CHOICES = [
        ("open", "Open"), ("reviewing", "Reviewing"),
        ("resolved", "Resolved"), ("dismissed", "Dismissed"),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="reports")
    # optional specific event the report is about (helps AFC target a ResultExclusion).
    event = models.ForeignKey("afc_tournament_and_scrims.Event", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="organization_reports")
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                 related_name="organization_reports")
    category = models.CharField(max_length=24, choices=CATEGORY_CHOICES, default="other")
    details = models.TextField()                                  # what happened
    evidence = models.ImageField(upload_to="organization_report_evidence/", null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="open")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="reviewed_org_reports")
    resolution_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Report({self.organization_id} {self.category} [{self.status}])"


class EventRating(models.Model):
    """A user's 1–5 rating of an event. Editable by the user (unique per event+user), and
    ANONYMOUS to the organizer — only the aggregate is shown publicly + to the organizer."""

    event = models.ForeignKey("afc_tournament_and_scrims.Event", on_delete=models.CASCADE,
                              related_name="ratings")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="event_ratings")
    score = models.PositiveSmallIntegerField()                    # 1..5 (validated in the view)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("event", "user")

    def __str__(self):
        return f"Rating(event={self.event_id} {self.score}/5)"


class EventComment(models.Model):
    """A user's free-text comment on an event. ONLY the event's organizer (+ AFC) can read it
    — never shown publicly or to other users."""

    event = models.ForeignKey("afc_tournament_and_scrims.Event", on_delete=models.CASCADE,
                              related_name="comments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                             related_name="event_comments")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Comment(event={self.event_id} by {self.user_id})"
