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
from django.utils import timezone


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


class OrgLeaderboardDesign(models.Model):
    """A self-serve leaderboard GRAPHIC template an organizer uploads (owner 2026-06-13).

    Unlike LeaderboardDesignRequest (human-in-the-loop, AFC builds it), this is automatic: the
    organizer uploads a branded background per output size, and the renderer
    (afc_leaderboard.graphic.render_leaderboard_graphic) composites the live standings + the
    title/subtitle + the org logo onto it. An org keeps a LIBRARY of these designs; when
    exporting a leaderboard the user picks WHICH design and WHICH size to download.

    background_instagram = the portrait/square IG canvas (rendered at 1080x1350).
    background_youtube    = the landscape YT canvas (rendered at 1920x1080).
    Either may be blank - that size then renders on a plain dark AFC default background.
    show_title/show_subtitle gate the tournament-name + stage/group lines the user types at
    export time. max_rows caps how many standings rows are drawn. is_default marks the design
    pre-selected in the export picker."""

    # null organization = an AFC-NATIVE design (platform-wide library managed by AFC admins),
    # used for AFC's own (org-less) standalone leaderboards. A set organization scopes the
    # design to that organizer (owner 2026-06-13: admins get this too, not only organizers).
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="leaderboard_designs",
        null=True, blank=True,
    )
    name = models.CharField(max_length=80)
    background_instagram = models.ImageField(
        upload_to="org_leaderboard_designs/", null=True, blank=True)
    background_youtube = models.ImageField(
        upload_to="org_leaderboard_designs/", null=True, blank=True)
    # Hex colours the renderer draws the standings text + accents in.
    text_color = models.CharField(max_length=9, default="#FFFFFF")
    accent_color = models.CharField(max_length=9, default="#34d27b")
    show_title = models.BooleanField(default=True)
    show_subtitle = models.BooleanField(default=True)
    max_rows = models.PositiveSmallIntegerField(default=16)
    is_default = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="leaderboard_designs_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self):
        return f"OrgLeaderboardDesign({self.organization_id}: {self.name})"


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


# ════════ Organizer blacklist (feature "organizer-blacklist", 2026-06-10) ════════
#
# An organizer can blacklist a team for a fixed duration. While the blacklist is active the
# team AND the people who were on that team at blacklist time cannot register for ANY of that
# organizer's events. The defining behaviour is the FOLLOWS-THE-PLAYER rule: blacklisting a
# team snapshots its CURRENT members into OrganizerBlacklistPlayer rows, and enforcement keys
# off (organization, player), NOT (organization, team). So a snapshotted player stays blocked
# from that organizer's events even after they leave the blacklisted team and join another one.
#   - The team-level OrganizerBlacklist row blocks the team ENTITY (re-registering that team).
#   - The per-player OrganizerBlacklistPlayer rows block the PEOPLE wherever they go.
#
# How this connects to the rest of the system:
#   - Created / listed / lifted / decided by the organizer endpoints in
#     afc_organizers/views_blacklist.py, gated by permissions.org_can(can_manage_registrations).
#   - Lift requests are raised by the affected party: a team manager (captain/owner/coach/
#     manager via afc_team helpers) or an affected player (themselves).
#   - ENFORCED at registration time by afc_organizers/blacklist.py::organizer_blacklist_block,
#     which afc_tournament_and_scrims.views.register_for_event calls on the TEAM path (after the
#     existing ban checks) for any event that has an owning Organization.
# Full spec: WEBSITE/tasks/organizer-blacklist-design.md.


class OrganizerBlacklist(models.Model):
    """A time-boxed blacklist of one team by one organization. The row blocks the team entity;
    its related OrganizerBlacklistPlayer rows (related_name="players") block the snapshotted
    people. The organizer picks a calendar date RANGE on create: `start_date` and `end_date` are
    parsed from ISO YYYY-MM-DD (end_date stored end-of-day). A legacy `duration_days` fallback in
    the create view still computes end_date = now + duration_days for old callers. Enforcement uses
    `is_currently_active()` so an expired blacklist stops blocking the instant it lapses, even
    before any nightly sweep flips its status to "expired"."""

    STATUS_CHOICES = [
        ("active", "Active"),     # live: blocks the team + its active snapshot players
        ("lifted", "Lifted"),     # organizer (or an approved lift request) ended it early
        ("expired", "Expired"),   # past end_date; a sweep may set this, but enforcement
                                  # already treats a lapsed "active" row as not-blocking
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE,
                                     related_name="blacklists")
    team = models.ForeignKey("afc_team.Team", on_delete=models.CASCADE,
                             related_name="organizer_blacklists")
    reason = models.TextField(blank=True, default="")          # why the organizer blacklisted them
    # The organizer picks a calendar date RANGE on create. start_date is the chosen start (at
    # day-start) and defaults to "now" when omitted; end_date is the chosen end day stored as
    # end-of-day so the whole selected day is covered. default=timezone.now (not auto_now_add) so
    # the create view can set start_date explicitly while still defaulting sensibly.
    start_date = models.DateTimeField(default=timezone.now)     # when the blacklist begins
    end_date = models.DateTimeField()                          # selected end day, end-of-day (set by view)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name="created_organizer_blacklists")
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    def is_currently_active(self):
        """True only while the blacklist should still block. We require BOTH an "active" status
        AND an end_date still in the future, so expiry is honoured live (a lapsed row never
        blocks regardless of whether a sweep has relabelled it "expired" yet)."""
        from django.utils import timezone
        return self.status == "active" and self.end_date > timezone.now()

    def __str__(self):
        return f"Blacklist(org={self.organization_id} team={self.team_id} [{self.status}])"


class OrganizerBlacklistPlayer(models.Model):
    """One snapshotted player under an OrganizerBlacklist. Created from the team's CURRENT
    TeamMembers at blacklist time, so the block follows the person, not the team membership.
    `is_active=False` retires a single player's block (their individual lift was approved)
    without ending the whole blacklist. Enforcement reads these per (organization, player):
    see afc_organizers/blacklist.py."""

    blacklist = models.ForeignKey(OrganizerBlacklist, on_delete=models.CASCADE,
                                  related_name="players")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="organizer_blacklist_entries")
    is_active = models.BooleanField(default=True)              # False once this player's lift lands
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One snapshot row per (blacklist, player) so re-snapshotting is idempotent and a player
        # cannot be listed twice under the same blacklist.
        unique_together = ("blacklist", "user")

    def __str__(self):
        return f"BlacklistPlayer(bl={self.blacklist_id} user={self.user_id} active={self.is_active})"


class BlacklistLiftRequest(models.Model):
    """A request to lift a blacklist, raised by the affected party. `scope="team"` asks for the
    whole blacklist to be lifted (raised by a team manager); `scope="player"` asks only for one
    person to be unblocked (raised by that player, or by a team manager on their behalf). The
    organizer decides via views_blacklist.decide_lift_request: approving a team-scope request
    lifts the entire blacklist; approving a player-scope request retires just that player's
    OrganizerBlacklistPlayer (and lifts the blacklist if no active players remain)."""

    SCOPE_CHOICES = [("team", "Team"), ("player", "Player")]
    STATUS_CHOICES = [("pending", "Pending"), ("approved", "Approved"), ("denied", "Denied")]

    blacklist = models.ForeignKey(OrganizerBlacklist, on_delete=models.CASCADE,
                                  related_name="lift_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                     related_name="blacklist_lift_requests")
    scope = models.CharField(max_length=6, choices=SCOPE_CHOICES, default="team")
    # Only set for player-scope requests: WHICH player is asking to be unblocked.
    target_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.CASCADE,
                                    related_name="blacklist_lift_requests_targeting")
    reason = models.TextField(blank=True, default="")          # the requester's case for a lift
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default="pending")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="decided_lift_requests")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"LiftRequest(bl={self.blacklist_id} {self.scope} [{self.status}])"
