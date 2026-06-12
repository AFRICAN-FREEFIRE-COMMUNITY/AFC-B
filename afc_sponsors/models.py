"""
afc_sponsors.models — SPONSOR ENTITIES (P1 of the sponsor-system redesign).

WHY THIS APP EXISTS (owner 2026-06-12, spec: WEBSITE/tasks/sponsors-redesign-design.md v2)
    Today a "sponsor" is a USER carrying the sponsor_admin granular role, linked to events via
    afc_tournament_and_scrims.SponsorEvent — every sponsor-access user sees the same dashboard
    data. The redesign gives sponsors their own ENTITY (like Organization / Vendor): admins
    create a Sponsor profile, ASSIGN members to it (a ydpay member sees ONLY ydpay), and attach
    events to it. P1 ships the entities + member scoping + the per-event legacy-data reads;
    engagements/approval (P3/P4 of the spec) hang off EventSponsorship.engagements later.

HOW IT CONNECTS
    - Members: afc_auth.User via SponsorMember (admin-assigned; the FE coachmark's "new access"
      trigger). Granular admin role `sponsor_admin` (afc_auth.Roles) manages these.
    - Events: afc_tournament_and_scrims.Event via EventSponsorship (replaces the legacy
      user-keyed SponsorEvent going forward; the legacy table keeps serving the old dashboard
      until the P2 cutover).
    - Read by afc_sponsors.views (admin CRUD + the member-scoped /sponsors portal endpoints)
      and consumed by frontend lib/sponsors.ts -> /a/sponsors + the sponsor dashboard.
"""
from django.conf import settings
from django.db import models


class Sponsor(models.Model):
    """One sponsor BRAND (ydpay, FreeMobile NG, ...) — the entity events attach to and members
    belong to. The analogue of afc_organizers.Organization for sponsors."""
    STATUS_CHOICES = [("active", "Active"), ("suspended", "Suspended")]

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    logo = models.ImageField(upload_to="sponsor_logos/", null=True, blank=True)
    description = models.TextField(blank=True)
    website = models.URLField(blank=True)
    # The sponsor's OFFICIAL pages, used later by the follow/like/share engagement (P3):
    # [{"platform": "instagram", "url": "..."}]. Stored now so profiles are complete from P1.
    socials = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="active")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="sponsors_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SponsorMember(models.Model):
    """Admin-assigned membership: WHO can open this sponsor's dashboard. The ydpay-user-sees-
    only-ydpay rule lives here — the portal endpoints resolve the caller's ACTIVE memberships
    and scope every read to those sponsors. A user may belong to several sponsors (the portal
    shows a switcher, mirroring the organizer org switcher)."""
    ROLE_CHOICES = [("owner", "Owner"), ("member", "Member")]
    STATUS_CHOICES = [("active", "Active"), ("removed", "Removed")]

    id = models.AutoField(primary_key=True)
    sponsor = models.ForeignKey(Sponsor, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sponsor_memberships",
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="member")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["sponsor", "user"], name="uniq_sponsor_member"),
        ]

    def __str__(self):
        return f"{self.user_id} @ {self.sponsor_id} ({self.status})"


class EventSponsorship(models.Model):
    """The event <-> sponsor attachment (an event can carry MULTIPLE sponsors from launch,
    owner decision). P1 uses it purely for SCOPING (which events show in which sponsor's
    dashboard); the per-sponsorship engagement config + approval gate (spec sections 2/4)
    activate in P3/P4 — the fields exist now so those phases are pure logic, no schema churn."""
    id = models.AutoField(primary_key=True)
    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event", on_delete=models.CASCADE, related_name="sponsorships",
    )
    sponsor = models.ForeignKey(Sponsor, on_delete=models.CASCADE, related_name="sponsorships")
    # P4: registration only completes after the sponsor approves it (rejection notifies the
    # player with the reason + a re-input prompt; final rejection auto-frees the slot).
    requires_approval = models.BooleanField(default=False)
    # P3: ordered engagement entries ({type: collect_id|follow_social|create_account|join_group,
    # ...} — full schema in the design doc). Unused by P1 logic.
    engagements = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "sponsor"], name="uniq_event_sponsorship"),
        ]

    def __str__(self):
        return f"{self.sponsor_id} sponsors event {self.event_id}"
