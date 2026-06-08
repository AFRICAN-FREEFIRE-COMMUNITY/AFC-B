# afc_partner_api/models.py
# ──────────────────────────────────────────────────────────────────────────────
# Partner identity + per-partner scope/toggles for the read-only partner API;
# mirrors afc_organizers PERMISSION_FIELDS pattern.
#
# A Partner is an AFC-provisioned external consumer of completed/published
# tournament data. All of its access is described HERE (never in code branches):
#   • scope  — which events it may read (explicit events, whole organizations,
#              and/or every native AFC event), and
#   • toggles — which resources (endpoints) respond and which fields appear,
#              every one defaulting OFF for least privilege.
# PartnerApiKey rows are the rotatable credentials that inherit a partner's config;
# only the sha256 hash of a key is stored, the plaintext is shown to the AFC admin
# exactly once at issue time. Full spec: WEBSITE/tasks/partner-api-design.md (§5).
# ──────────────────────────────────────────────────────────────────────────────
from django.conf import settings
from django.db import models


class Partner(models.Model):
    partner_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    contact_email = models.EmailField(blank=True)          # INTERNAL ONLY — never serialized to partners
    status = models.CharField(max_length=20, default="active",
                              choices=[("active", "Active"), ("suspended", "Suspended")])
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    # ── Scope ──
    allowed_events = models.ManyToManyField("afc_tournament_and_scrims.Event", blank=True,
                                            related_name="partner_grants")
    allowed_organizations = models.ManyToManyField("afc_organizers.Organization", blank=True,
                                                    related_name="partner_grants")
    allow_all_native_afc = models.BooleanField(default=False)   # all organization-less AFC events

    # ── Resource toggles (which endpoints respond) ──
    can_read_events = models.BooleanField(default=False)
    can_read_stages = models.BooleanField(default=False)
    can_read_matches = models.BooleanField(default=False)
    can_read_standings = models.BooleanField(default=False)
    can_read_teams = models.BooleanField(default=False)
    can_read_players = models.BooleanField(default=False)

    # ── Field toggles (which fields appear) ──
    include_placements = models.BooleanField(default=False)
    include_kills = models.BooleanField(default=False)
    include_damage = models.BooleanField(default=False)
    include_assists = models.BooleanField(default=False)
    include_rosters = models.BooleanField(default=False)
    include_maps = models.BooleanField(default=False)
    include_prize = models.BooleanField(default=False)
    include_mvp = models.BooleanField(default=False)


# Whitelist that drives admin-edit + serialization (mirrors PERMISSION_FIELDS).
RESOURCE_TOGGLES = ("can_read_events", "can_read_stages", "can_read_matches",
                    "can_read_standings", "can_read_teams", "can_read_players")
FIELD_TOGGLES = ("include_placements", "include_kills", "include_damage", "include_assists",
                 "include_rosters", "include_maps", "include_prize", "include_mvp")
PARTNER_TOGGLE_FIELDS = RESOURCE_TOGGLES + FIELD_TOGGLES


class PartnerApiKey(models.Model):
    key_id = models.AutoField(primary_key=True)
    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name="api_keys")
    key_prefix = models.CharField(max_length=16, db_index=True)   # lookup handle, e.g. "afcp_3f9a"
    key_hash = models.CharField(max_length=64)                    # sha256 of the full key; plaintext NEVER stored
    label = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=20, default="active",
                              choices=[("active", "Active"), ("revoked", "Revoked")])
    rate_limit_per_min = models.PositiveIntegerField(default=60)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
