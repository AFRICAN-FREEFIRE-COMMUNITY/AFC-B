"""
AFC Ranking & Tiering — data models (afc_rankings).
Spec: AFC_RANKING_TIERING_SPEC.md §19. Plan: tasks/ranking-tiering-plan.md.

Conventions (locked in master plan):
- Season PK = season_id (AutoField); GhostTeam PK = ghost_team_id (UUID); score models use default `id`.
- Tiebreaker fields denormalized: tournament_wins / total_kills / tournaments_played (team),
  total_kills / mvp_count / finals_appearances (player) — recalc reorder sorts on these exact names.
- `finalized` on monthly scores (archive + skip-closed-month).
- team XOR ghost_team enforced via CheckConstraint (MySQL 8.0.16+; afc_db = mysql:8.0 ✓).
"""
import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


# ───────────────────────────── §19.1 Season ─────────────────────────────
class Season(models.Model):
    QUARTER_CHOICES = [(1, "Q1"), (2, "Q2"), (3, "Q3"), (4, "Q4")]

    season_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100)                      # "Season 1 2026"
    quarter = models.PositiveSmallIntegerField(choices=QUARTER_CHOICES)
    year = models.PositiveIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    transfer_window_open = models.DateField()
    transfer_window_close = models.DateField()
    is_active = models.BooleanField(default=False)

    tier_eval_run = models.BooleanField(default=False)
    tier_eval_run_at = models.DateTimeField(null=True, blank=True)
    tier_eval_run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="seasons_evaluated",
    )
    scores_frozen_at = models.DateTimeField(null=True, blank=True)

    # Publishing — the ranked scores and the tier assignments are published to the public
    # INDEPENDENTLY ("rankings done separately from tiering"). The public read API hides each
    # until its flag is set; admins always see the computed draft so they can preview first,
    # and can unpublish either at any time.
    rankings_published = models.BooleanField(default=False)
    rankings_published_at = models.DateTimeField(null=True, blank=True)
    rankings_published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="seasons_rankings_published",
    )
    tiers_published = models.BooleanField(default=False)
    tiers_published_at = models.DateTimeField(null=True, blank=True)
    tiers_published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="seasons_tiers_published",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def is_transfer_window_open(self, on=None):
        """True if the transfer window is open on the given date (default: today)."""
        day = on or timezone.now().date()
        return self.transfer_window_open <= day <= self.transfer_window_close

    class Meta:
        ordering = ["-year", "-quarter"]
        constraints = [
            models.UniqueConstraint(fields=["year", "quarter"], name="uniq_season_year_quarter"),
        ]
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["year", "quarter"]),
        ]

    def __str__(self):
        return self.name


# ───────────────────────────── §19.4 GhostTeam ─────────────────────────────
class GhostTeam(models.Model):
    CLAIM_STATUS = [
        ("unclaimed", "Unclaimed"),
        ("pending", "Claim Pending"),
        ("claimed", "Claimed"),
        ("revoked", "Claim Revoked"),
    ]
    ghost_team_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_name = models.CharField(max_length=200)
    country = models.CharField(max_length=100)
    external_id = models.CharField(max_length=200, null=True, blank=True)
    is_provisional = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    # claim lifecycle (inline for Phase 1; a dedicated GhostTeamClaim model may split this out in Phase 3)
    claim_status = models.CharField(max_length=20, choices=CLAIM_STATUS, default="unclaimed")
    claimed_by = models.ForeignKey(
        "afc_team.Team", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="claimed_ghost_teams",
    )
    claim_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="ghost_claims_requested",
    )
    claim_requested_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="ghost_claims_approved",
    )
    claim_revoked_at = models.DateTimeField(null=True, blank=True)
    claim_note = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="ghost_teams_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["is_active"]),
            models.Index(fields=["claim_status"]),
            models.Index(fields=["claimed_by"]),
        ]

    def __str__(self):
        return f"[GHOST] {self.team_name}"


# ──────────────────────── §19.2 TeamSeasonEnrollment ────────────────────────
class TeamSeasonEnrollment(models.Model):
    team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True,
        on_delete=models.CASCADE, related_name="season_enrollments",
    )
    ghost_team = models.ForeignKey(
        GhostTeam, null=True, blank=True,
        on_delete=models.CASCADE, related_name="season_enrollments",
    )
    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="enrollments")
    enrolled_at = models.DateTimeField(auto_now_add=True)
    roster_locked_at = models.DateTimeField(null=True, blank=True)
    is_ghost = models.BooleanField(default=False)
    late_entry = models.BooleanField(default=False)
    late_entry_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="late_enrollments_approved",
    )

    class Meta:
        constraints = [
            # plain unique (no condition): MySQL treats NULLs as distinct, so the
            # null side (ghost rows for the team index, and vice versa) is ignored.
            models.UniqueConstraint(fields=["team", "season"], name="uniq_team_season_enrollment"),
            models.UniqueConstraint(fields=["ghost_team", "season"], name="uniq_ghost_season_enrollment"),
            models.CheckConstraint(
                name="enrollment_team_xor_ghost",
                check=(
                    models.Q(team__isnull=False, ghost_team__isnull=True) |
                    models.Q(team__isnull=True, ghost_team__isnull=False)
                ),
            ),
        ]
        indexes = [models.Index(fields=["season", "is_ghost"])]

    def __str__(self):
        return f"Enrollment({self.team_id or self.ghost_team_id} → {self.season_id})"


# ──────────────────────── §19.3 TeamSeasonRoster ────────────────────────
class TeamSeasonRoster(models.Model):
    team = models.ForeignKey("afc_team.Team", on_delete=models.CASCADE, related_name="season_rosters")
    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="rosters")
    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="season_rosters")
    joined_at = models.DateField(default=timezone.now)
    left_at = models.DateField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="roster_approvals",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        # §14.2 one-team-per-player is PARTIAL uniqueness (active rows only). MySQL can't do
        # partial unique indexes, so it is enforced in the roster write-path (Phase 4), not the DB.
        indexes = [
            models.Index(fields=["season", "team"]),
            models.Index(fields=["season", "player"]),
        ]

    def __str__(self):
        return f"Roster(player={self.player_id}, team={self.team_id}, season={self.season_id})"


# ──────────────────────── §19.5 TeamMonthlyScore ────────────────────────
class TeamMonthlyScore(models.Model):
    team = models.ForeignKey("afc_team.Team", null=True, blank=True,
                             on_delete=models.CASCADE, related_name="monthly_scores")
    ghost_team = models.ForeignKey(GhostTeam, null=True, blank=True,
                                   on_delete=models.CASCADE, related_name="monthly_scores")
    month = models.DateField()                       # first day of month (UTC)

    tournament_pts = models.FloatField(default=0)
    scrim_pts = models.FloatField(default=0)
    total_score = models.FloatField(default=0)
    rank = models.IntegerField(null=True, blank=True)

    # tiebreaker (§5.4)
    tournament_wins = models.IntegerField(default=0)
    total_kills = models.IntegerField(default=0)
    tournaments_played = models.IntegerField(default=0)

    finalized = models.BooleanField(default=False)   # archive / skip-closed-month
    is_zeroed = models.BooleanField(default=False)
    zeroed_reason = models.CharField(max_length=255, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "month"], name="uniq_team_month_score"),
            models.UniqueConstraint(fields=["ghost_team", "month"], name="uniq_ghost_month_score"),
            models.CheckConstraint(
                name="team_month_team_xor_ghost",
                check=(models.Q(team__isnull=False, ghost_team__isnull=True) |
                       models.Q(team__isnull=True, ghost_team__isnull=False)),
            ),
        ]
        indexes = [
            models.Index(fields=["month", "-total_score"]),
            models.Index(fields=["team", "month"]),
        ]

    def __str__(self):
        return f"TeamMonthly({self.team_id or self.ghost_team_id} @ {self.month}: {self.total_score})"


# ──────────────────────── §19.6 TeamQuarterlyScore ────────────────────────
class TeamQuarterlyScore(models.Model):
    TIER_CHOICES = [(0, "Elite"), (1, "Competitive"), (2, "Rising"), (3, "Entry")]

    team = models.ForeignKey("afc_team.Team", null=True, blank=True,
                             on_delete=models.CASCADE, related_name="quarterly_scores")
    ghost_team = models.ForeignKey(GhostTeam, null=True, blank=True,
                                   on_delete=models.CASCADE, related_name="quarterly_scores")
    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="team_quarterly_scores")

    tournament_pts = models.FloatField(default=0)
    scrim_pts = models.FloatField(default=0)
    prize_money_pts = models.FloatField(default=0)     # §7.2
    social_media_pts = models.FloatField(default=0)    # §7.3 (max 10)
    total_score = models.FloatField(default=0)

    tier_assigned = models.IntegerField(choices=TIER_CHOICES, null=True, blank=True)  # §11
    tier_assigned_at = models.DateTimeField(null=True, blank=True)
    tier_overridden = models.BooleanField(default=False)
    tier_override_reason = models.CharField(max_length=255, blank=True)

    participated_in_tournaments = models.IntegerField(default=0)   # §7.4
    meets_participation_floor = models.BooleanField(default=False)
    insufficient_activity_note = models.CharField(max_length=255, blank=True)

    tournament_wins = models.IntegerField(default=0)
    total_kills = models.IntegerField(default=0)
    rank = models.IntegerField(null=True, blank=True)

    is_zeroed = models.BooleanField(default=False)
    zeroed_reason = models.CharField(max_length=255, blank=True)
    # §16 manual partial penalty — an admin deducts points without a full ban-zero.
    # Sticky across recalc (recalc's update_or_create only writes the raw component
    # fields, never this), so a deduction persists until an admin clears it. The
    # effective ranking score is max(0, total_score - points_deducted).
    points_deducted = models.FloatField(default=0)
    points_deducted_reason = models.CharField(max_length=255, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "season"], name="uniq_team_season_score"),
            models.UniqueConstraint(fields=["ghost_team", "season"], name="uniq_ghost_season_score"),
            models.CheckConstraint(
                name="team_qscore_team_xor_ghost",
                check=(models.Q(team__isnull=False, ghost_team__isnull=True) |
                       models.Q(team__isnull=True, ghost_team__isnull=False)),
            ),
        ]
        indexes = [
            models.Index(fields=["season", "-total_score"]),
            models.Index(fields=["season", "tier_assigned"]),
        ]

    def __str__(self):
        return f"TeamQuarterly({self.team_id or self.ghost_team_id} @ season {self.season_id}: {self.total_score})"


# ──────────────────────── §19.7 PlayerMonthlyScore ────────────────────────
class PlayerMonthlyScore(models.Model):
    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                               related_name="monthly_scores")
    month = models.DateField()

    kill_pts = models.FloatField(default=0)
    placement_pts = models.FloatField(default=0)
    mvp_pts = models.FloatField(default=0)
    finals_pts = models.FloatField(default=0)
    team_win_pts = models.FloatField(default=0)
    participation_pts = models.FloatField(default=0)
    scrim_kill_pts = models.FloatField(default=0)
    scrim_win_pts = models.FloatField(default=0)
    total_score = models.FloatField(default=0)
    rank = models.IntegerField(null=True, blank=True)

    # tiebreaker (§6.4)
    total_kills = models.IntegerField(default=0)
    mvp_count = models.IntegerField(default=0)
    finals_appearances = models.IntegerField(default=0)

    finalized = models.BooleanField(default=False)
    is_zeroed = models.BooleanField(default=False)
    zeroed_reason = models.CharField(max_length=255, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["player", "month"], name="uniq_player_month_score"),
        ]
        indexes = [models.Index(fields=["month", "-total_score"])]

    def __str__(self):
        return f"PlayerMonthly(player={self.player_id} @ {self.month}: {self.total_score})"


# ──────────────────────── §19.8 PlayerQuarterlyScore ────────────────────────
class PlayerQuarterlyScore(models.Model):
    TIER_CHOICES = [(0, "Elite"), (1, "Competitive"), (2, "Rising"), (3, "Entry")]
    TIER_SOURCE = [("team", "From Team"), ("individual", "Individual")]

    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                               related_name="quarterly_scores")
    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="player_quarterly_scores")

    total_score = models.FloatField(default=0)
    prize_money_pts = models.FloatField(default=0)        # §6.2 inherited

    tier_assigned = models.IntegerField(choices=TIER_CHOICES, null=True, blank=True)
    tier_source = models.CharField(max_length=12, choices=TIER_SOURCE, null=True, blank=True)
    team_at_evaluation = models.ForeignKey(
        "afc_team.Team", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="player_quarterly_evals",
    )
    tier_assigned_at = models.DateTimeField(null=True, blank=True)

    participated_in_tournaments = models.IntegerField(default=0)
    meets_participation_floor = models.BooleanField(default=False)
    rank = models.IntegerField(null=True, blank=True)

    is_zeroed = models.BooleanField(default=False)
    zeroed_reason = models.CharField(max_length=255, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["player", "season"], name="uniq_player_season_score"),
        ]
        indexes = [
            models.Index(fields=["season", "-total_score"]),
            models.Index(fields=["season", "tier_assigned"]),
        ]

    def __str__(self):
        return f"PlayerQuarterly(player={self.player_id} @ season {self.season_id}: {self.total_score})"


# ──────────────────────── §19.9 AnnualLeaderboardEntry ────────────────────────
class AnnualLeaderboardEntry(models.Model):
    ENTITY_TYPE = [("team", "Team"), ("player", "Player")]

    year = models.PositiveIntegerField()
    entity_type = models.CharField(max_length=6, choices=ENTITY_TYPE)
    team = models.ForeignKey("afc_team.Team", null=True, blank=True,
                             on_delete=models.CASCADE, related_name="annual_entries")
    player = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                               on_delete=models.CASCADE, related_name="annual_entries")
    q1_score = models.FloatField(default=0)
    q2_score = models.FloatField(default=0)
    q3_score = models.FloatField(default=0)
    q4_score = models.FloatField(default=0)
    total_score = models.FloatField(default=0)
    rank = models.IntegerField(null=True, blank=True)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["year", "team"], name="uniq_year_team_annual"),
            models.UniqueConstraint(fields=["year", "player"], name="uniq_year_player_annual"),
            models.CheckConstraint(
                name="annual_team_xor_player",
                check=(models.Q(team__isnull=False, player__isnull=True) |
                       models.Q(team__isnull=True, player__isnull=False)),
            ),
        ]
        indexes = [models.Index(fields=["year", "entity_type", "-total_score"])]

    def __str__(self):
        return f"Annual({self.year} {self.entity_type} {self.team_id or self.player_id}: {self.total_score})"


# ──────────────────────── §19.10 TransferWindowLog ────────────────────────
class TransferWindowLog(models.Model):
    ACTION_CHOICES = [("opened", "Opened"), ("closed", "Closed"), ("extended", "Extended")]

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="transfer_logs")
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    previous_open_date = models.DateField(null=True, blank=True)
    previous_close_date = models.DateField(null=True, blank=True)
    new_open_date = models.DateField(null=True, blank=True)
    new_close_date = models.DateField(null=True, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="transfer_window_changes")
    changed_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-changed_at"]
        indexes = [models.Index(fields=["season", "-changed_at"])]

    def __str__(self):
        return f"TransferWindowLog(season={self.season_id} {self.action} by {self.changed_by_id})"


# ──────────────────────── §16 + §20 RankingAuditLog ────────────────────────
class RankingAuditLog(models.Model):
    OBJECT_TYPES = [
        ("tournament_result", "Tournament Result"),
        ("scrim_result", "Scrim Result"),
        ("prize_money", "Prize Money"),
        ("social_media", "Social Media Count"),
        ("roster", "Roster Change"),
        ("ghost_claim", "Ghost Claim"),
        ("tier_override", "Tier Override"),
        ("ban_zeroing", "Ban Zeroing"),
        ("transfer_window", "Transfer Window"),
        # Phase 2 additions — one bucket per admin write surface so the audit log filters cleanly.
        ("season", "Season"),                  # create / edit season, transfer window
        ("evaluation", "Quarterly Evaluation"),  # run-evaluation (tier lock)
        ("scoring_config", "Scoring Config"),    # edits to the scoring rule set
        ("event_tier", "Tournament Tier Rule"),  # tournament-tier classification rules
        ("point_deduction", "Point Deduction"),  # manual partial-penalty point deduction
    ]
    audit_id = models.AutoField(primary_key=True)
    object_type = models.CharField(max_length=30, choices=OBJECT_TYPES)
    object_ref = models.CharField(max_length=100, blank=True)
    action = models.CharField(max_length=50)
    reason = models.TextField()
    before_snapshot = models.JSONField(default=dict, blank=True)
    after_snapshot = models.JSONField(default=dict, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="ranking_audit_entries")
    changed_at = models.DateTimeField(auto_now_add=True)
    season = models.ForeignKey(Season, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-changed_at"]
        indexes = [
            models.Index(fields=["object_type", "-changed_at"]),
            models.Index(fields=["changed_by", "-changed_at"]),
        ]

    def __str__(self):
        return f"Audit({self.object_type} {self.action} by {self.changed_by_id})"


# ──────────────────────── §7.3 TeamSocialSnapshot ────────────────────────
class TeamSocialSnapshot(models.Model):
    team = models.ForeignKey("afc_team.Team", on_delete=models.CASCADE, related_name="social_snapshots")
    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="social_snapshots")
    # self-connect (§7.3 rework): a team links its own handles, an admin then verifies.
    instagram_handle = models.CharField(max_length=100, blank=True)
    tiktok_handle = models.CharField(max_length=100, blank=True)
    instagram_followers = models.PositiveIntegerField(default=0)
    tiktok_followers = models.PositiveIntegerField(default=0)
    combined_followers = models.PositiveIntegerField(default=0)
    social_media_pts = models.FloatField(default=0)               # computed 0-10
    # True once a team self-submits its handles (vs an admin entering them directly).
    connected_by_team = models.BooleanField(default=False)
    # Mutable verification gate: ONLY a verified snapshot contributes social_media_pts to
    # the score (aggregation reads combined_followers only when is_verified). Admin
    # verify → True, unverify → False (the score then drops to 0 on the next recalc).
    is_verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="social_snapshots_verified")
    verified_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["team", "season"], name="uniq_team_season_social")]

    def __str__(self):
        return f"Social(team={self.team_id} @ season {self.season_id}: {self.combined_followers})"


# ──────────────────────── §19.4b GhostPlayer ────────────────────────
class GhostPlayer(models.Model):
    """A provisional roster slot on a ghost team.

    Off-platform tournament results attribute to these in-game names until the ghost
    team is claimed by a real team, at which point the slots map onto that team's
    players. Created/edited from the ghost-team admin surfaces (Rankings + Teams page).
    """
    ghost_team = models.ForeignKey(GhostTeam, on_delete=models.CASCADE, related_name="players")
    ign = models.CharField(max_length=100)                 # in-game name
    slot = models.PositiveSmallIntegerField(default=1)     # display order in the roster (1-based)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ghost_team", "slot"]
        indexes = [models.Index(fields=["ghost_team"])]

    def __str__(self):
        return f"{self.ign} [{self.ghost_team.team_name}]"


# ═════════════════════ Admin-editable config and result-marker models (not the spec score tables) ═════════════════════
# ──────────────────────── ScoringConfig (admin-editable scoring rules) ────────────────────────
class ScoringConfig(models.Model):
    """Versioned, admin-editable snapshot of the scoring rule set.

    Mirrors the tables hardcoded in ``scoring/constants.py`` (tier multipliers, placement
    points, compression scales, win/finals bonuses, prize + social brackets, tier
    thresholds, scrim rules, player weights) as a single JSON blob. The engine reads the
    *active* config and falls back to ``constants.py`` when none is active. Saving from the
    admin Scoring Config surface drafts a NEW version (immutable history) and activates it.
    """
    version = models.PositiveIntegerField(unique=True)
    is_active = models.BooleanField(default=False)
    config = models.JSONField(default=dict)                # full snapshot keyed by rule group
    note = models.CharField(max_length=255, blank=True)    # the admin's save reason / changelog line
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="scoring_configs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        indexes = [models.Index(fields=["is_active"])]

    def __str__(self):
        return f"ScoringConfig v{self.version}{' (active)' if self.is_active else ''}"


# ──────────────────────── EventTierRule (tournament tier classification) ────────────────────────
class EventTierRule(models.Model):
    """One rule in the ordered, first-match-wins list that classifies a tournament into a
    tier (Tier 1/2/3 → scoring multiplier 2.0×/1.5×/1.0×).

    Evaluated top-down by ``priority`` (lowest first); the first ENABLED rule whose
    conditions match sets the event's tier, else the fall-through ``EventTierConfig.default_tier``.
    Conditions are stored as JSON so the admin Tournament Tiers surface can build them freely:
        [{"field": "prize"|"teams"|"players"|"format",
          "op":    "gte"|"lte"|"is_lan"|"is_virtual",
          "value": <int, ignored for format ops>}]
    combined with ``match`` ("all" = AND, "any" = OR).
    """
    MATCH_CHOICES = [("all", "Match all"), ("any", "Match any")]
    TIER_CHOICES = [(1, "Tier 1"), (2, "Tier 2"), (3, "Tier 3")]

    priority = models.PositiveIntegerField(default=0)      # lower = evaluated first
    match = models.CharField(max_length=3, choices=MATCH_CHOICES, default="all")
    conditions = models.JSONField(default=list)
    tier = models.PositiveSmallIntegerField(choices=TIER_CHOICES, default=2)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "created_at"]

    def __str__(self):
        return f"Rule #{self.priority} → Tier {self.tier}"


class EventTierConfig(models.Model):
    """Singleton settings row for tournament-tier classification — the fall-through tier
    used when an event matches no ``EventTierRule``. (Kept as its own row so it is editable
    from the admin surface alongside the rules.)"""
    default_tier = models.PositiveSmallIntegerField(default=3)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"EventTierConfig (default Tier {self.default_tier})"


# ──────────────────────── Result Markers — counting controls ────────────────────────
# Cross-file: both models below are read in aggregation._counting_controls /
# aggregation._excluded_event_ids BEFORE the engine inputs are built — a disabled
# component is zeroed and an excluded event row is skipped, so the scoring engine stays pure.
class EventCountingControl(models.Model):
    """Per-tournament admin toggles for whether each scoring component COUNTS toward
    rankings (the Result Markers surface). Checked in ``aggregation.py`` BEFORE the engine
    input is built — a disabled component is zeroed out for every team/player in that event,
    so the scoring engine itself stays pure. No row for an event ⇒ everything counts.
    """
    event = models.OneToOneField(
        "afc_tournament_and_scrims.Event", on_delete=models.CASCADE, related_name="counting_control",
    )
    count_winner = models.BooleanField(default=True)      # winner bonus counts
    count_placement = models.BooleanField(default=True)   # placement points count
    count_kills = models.BooleanField(default=True)       # kill points count
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="event_counting_controls",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"CountingControl(event={self.event_id})"


class ResultExclusion(models.Model):
    """Per-event opt-out for ONE team or player — their results in this event don't count
    toward rankings (e.g. a disqualification or a protest). Checked in ``aggregation.py``:
    the entity's tournament row for that event is skipped entirely. team XOR player.
    """
    ENTITY = [("team", "Team"), ("player", "Player")]
    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event", on_delete=models.CASCADE, related_name="result_exclusions",
    )
    entity_type = models.CharField(max_length=6, choices=ENTITY)
    team = models.ForeignKey(
        "afc_team.Team", null=True, blank=True,
        on_delete=models.CASCADE, related_name="result_exclusions",
    )
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="result_exclusions",
    )
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="result_exclusions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "team"], name="uniq_event_team_exclusion"),
            models.UniqueConstraint(fields=["event", "player"], name="uniq_event_player_exclusion"),
            models.CheckConstraint(
                name="result_exclusion_team_xor_player",
                check=(models.Q(team__isnull=False, player__isnull=True) |
                       models.Q(team__isnull=True, player__isnull=False)),
            ),
        ]
        indexes = [models.Index(fields=["event"])]

    def __str__(self):
        return f"Exclusion(event={self.event_id} {self.entity_type} {self.team_id or self.player_id})"
