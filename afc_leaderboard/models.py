"""
afc_leaderboard.models — Standalone Leaderboards (Phase 1).

PURPOSE
    A "standalone" leaderboard is an event-less competition table. An AFC admin (org = null,
    AFC-native) or an organizer (org = their Organization, gated by `can_upload_results`) can
    create one, add real-or-ghost teams/players as participants, enter per-map results, and view
    the computed standings. This is deliberately DECOUPLED from the event-tied stats tables
    (afc_tournament_and_scrims) so editing a standalone leaderboard can never touch a live event.
    See WEBSITE/tasks/standalone-leaderboard-design.md §2 (this file = "Approach A — new tables").

HOW IT CONNECTS
    - Point math is NOT redefined here. Results are scored on save through
      `afc_tournament_and_scrims.scoring.compute_team_points / compute_solo_points` (the single
      source of truth shared with event entry + OCR), called from afc_leaderboard.views.
    - Ghosts reuse the real platform entities `afc_rankings.GhostTeam / GhostPlayer` (claimable
      later, already scored via the team-XOR-ghost path), created inline during participant add.
    - Ownership FK is `afc_organizers.Organization` (null = AFC-native). Permission decisions live
      in afc_leaderboard.permissions (`can_manage_standalone_lb`, `can_set_rankings_flag`).
    - Standings are computed on read by afc_leaderboard.standings.standalone_standings(lb).
    - Consumed by the FE wizard (/a/leaderboards/standalone/create) + view page
      (/leaderboards/standalone/<id>) via the endpoints in afc_leaderboard.views / urls.

MODELS
    StandaloneLeaderboard  — the leaderboard header: name, format, scoring config, owner, status.
    LeaderboardParticipant — one competitor row: exactly one of {team, ghost_team, user, ghost_player}.
    LeaderboardMatch       — one "map" played in the leaderboard.
    ParticipantMatchResult — one participant's result in one map, with computed point columns.
"""
import uuid

from django.db import models
from django.conf import settings


class StandaloneLeaderboard(models.Model):
    """
    The header for an event-less leaderboard.

    `organization` null  => AFC-native (created by an AFC admin).
    `organization` set   => owned by that org (created by an organizer with can_upload_results).
    `counts_toward_rankings` is AFC-admin-only (enforced in the view, not the model); Phase 1 only
    stores the flag — wiring it into the rankings engine is Phase 3.
    `status` starts 'draft' (editable, hidden) and the creator publishes it to make standings viewable.
    Scoring config (placement_points / kill_point / points_per_assist / points_per_1000_damage) mirrors
    the args of scoring.compute_team_points so the same point math used by events applies here.
    """
    FORMAT = (("team", "Team"), ("solo", "Solo"))
    STATUS = (("draft", "Draft"), ("published", "Published"))

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=120)
    format = models.CharField(max_length=10, choices=FORMAT)
    # null organization = AFC-native owner. SET_NULL so deleting an org re-homes its leaderboards to AFC.
    organization = models.ForeignKey(
        "afc_organizers.Organization", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="standalone_leaderboards",
    )
    placement_points = models.JSONField(default=dict)            # {"1":12,"2":9,...,"10":1}
    kill_point = models.FloatField(default=1.0)
    points_per_assist = models.FloatField(default=0.0)           # optional, team format
    points_per_1000_damage = models.FloatField(default=0.0)      # optional, team format
    counts_toward_rankings = models.BooleanField(default=False)  # AFC-admin only (enforced in view)
    # ── P3 rankings-feed config (AFC-admin-only, only meaningful when counts_toward_rankings) ──
    # These two columns configure HOW this leaderboard's results enter the AFC rankings engine when
    # the flag above is on. They are consumed by afc_rankings.standalone (the standalone->rankings
    # input builders) via effective_date below; an organizer can never set them (they cannot set the
    # flag — see afc_leaderboard.views.create_leaderboard / edit_leaderboard, which gate both fields
    # behind permissions.can_set_rankings_flag exactly like counts_toward_rankings).
    # played_on buckets this leaderboard's results into a rankings month + season (null => the
    # created_at date is used by effective_date). One date per leaderboard (a standalone LB models
    # one event/day), mirroring how Match.played_on buckets an event match in aggregation.py.
    played_on = models.DateField(null=True, blank=True)
    # ranking_tier is the tournament tier the rankings engine weights this LB's points by (mirrors
    # Event.tournament_tier; tier_1=2.0x .. tier_3=1.0x via scoring.engine.tier_multiplier). Default
    # tier_3 = lowest weight (safest). Read by afc_rankings.standalone when building TournamentInput /
    # PlayerTournamentInput for this LB's participants.
    RANKING_TIER_CHOICES = [("tier_1", "Tier 1"), ("tier_2", "Tier 2"), ("tier_3", "Tier 3")]
    ranking_tier = models.CharField(max_length=10, choices=RANKING_TIER_CHOICES, default="tier_3")
    status = models.CharField(max_length=10, choices=STATUS, default="draft")
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="standalone_leaderboards",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def effective_date(self):
        """The date this leaderboard's results are bucketed under for rankings (month + season).

        Prefers the admin-set played_on; falls back to the created_at date. Returns None only on an
        unsaved row with neither. Read by afc_rankings.standalone._lb_in_window (window membership)
        and recompute_for_leaderboard (which month/season to recompute), so it is the single date
        this leaderboard contributes its TournamentInput/PlayerTournamentInput to.
        """
        return self.played_on or (self.created_at.date() if self.created_at else None)

    def __str__(self):
        return f"{self.name} ({self.format})"


class LeaderboardParticipant(models.Model):
    """
    One competitor in a leaderboard. EXACTLY ONE of the four FKs is non-null (DB CheckConstraint),
    mirroring the team-XOR-ghost pattern in the afc_rankings score tables:
      - team format: `team` (real) XOR `ghost_team` (ghost)
      - solo format: `user` (real) XOR `ghost_player` (ghost)
    The view additionally enforces that the chosen kind matches the leaderboard's `format`.
    Uniqueness per entity per leaderboard is enforced in the view (no duplicate participants).
    """
    id = models.AutoField(primary_key=True)
    leaderboard = models.ForeignKey(
        StandaloneLeaderboard, on_delete=models.CASCADE, related_name="participants",
    )
    # ── team format ──
    team = models.ForeignKey("afc_team.Team", null=True, blank=True, on_delete=models.CASCADE)
    ghost_team = models.ForeignKey(
        "afc_rankings.GhostTeam", null=True, blank=True, on_delete=models.CASCADE,
    )
    # ── solo format ──
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="standalone_participations",
    )
    ghost_player = models.ForeignKey(
        "afc_rankings.GhostPlayer", null=True, blank=True, on_delete=models.CASCADE,
    )

    class Meta:
        constraints = [
            # Exactly one of the four entity FKs is set. Guards against a half-built participant
            # row (none set) and an ambiguous one (two set). The view picks the right pair per format.
            models.CheckConstraint(
                name="participant_exactly_one_entity",
                check=(
                    models.Q(team__isnull=False, ghost_team__isnull=True, user__isnull=True, ghost_player__isnull=True)
                    | models.Q(team__isnull=True, ghost_team__isnull=False, user__isnull=True, ghost_player__isnull=True)
                    | models.Q(team__isnull=True, ghost_team__isnull=True, user__isnull=False, ghost_player__isnull=True)
                    | models.Q(team__isnull=True, ghost_team__isnull=True, user__isnull=True, ghost_player__isnull=False)
                ),
            ),
        ]

    @property
    def display_name(self):
        """Human label for the participant, whichever of the four entities it points at."""
        if self.team_id:
            return self.team.team_name
        if self.ghost_team_id:
            return self.ghost_team.team_name
        if self.user_id:
            return self.user.username
        if self.ghost_player_id:
            return self.ghost_player.ign
        return ""

    @property
    def is_ghost(self):
        """True when this participant is a ghost (off-platform) entity, for the FE ghost badge."""
        return bool(self.ghost_team_id or self.ghost_player_id)

    @property
    def kind(self):
        """Stable string the FE/standings use to badge the row: real_team / ghost_team / real_user / ghost_player."""
        if self.team_id:
            return "real_team"
        if self.ghost_team_id:
            return "ghost_team"
        if self.user_id:
            return "real_user"
        if self.ghost_player_id:
            return "ghost_player"
        return "unknown"

    def __str__(self):
        return f"{self.display_name} @ {self.leaderboard_id}"


class LeaderboardMatch(models.Model):
    """One "map" played in the leaderboard. Results for every participant in this map live in
    ParticipantMatchResult. `match_map` is an optional free-text map name (same idea as the event
    match_map field)."""
    id = models.AutoField(primary_key=True)
    leaderboard = models.ForeignKey(
        StandaloneLeaderboard, on_delete=models.CASCADE, related_name="matches",
    )
    match_number = models.PositiveIntegerField(default=1)
    match_map = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["match_number"]

    def __str__(self):
        return f"Match {self.match_number} @ {self.leaderboard_id}"


class ParticipantMatchResult(models.Model):
    """
    One participant's result in one map. The raw inputs (placement, kills, damage, assists,
    bonus/penalty) are entered by the manager; the three point columns
    (placement_points / kill_points / total_points) are COMPUTED on save in the view via
    scoring.compute_team_points / compute_solo_points (never typed by hand). standings.py sums
    these columns across a participant's results to build the standings table.
    Unique per (match, participant) so re-saving a result overwrites rather than duplicates.
    """
    id = models.AutoField(primary_key=True)
    match = models.ForeignKey(LeaderboardMatch, on_delete=models.CASCADE, related_name="results")
    participant = models.ForeignKey(
        LeaderboardParticipant, on_delete=models.CASCADE, related_name="results",
    )
    # ── raw inputs (entered by the manager) ──
    placement = models.PositiveIntegerField(default=0)
    kills = models.PositiveIntegerField(default=0)
    damage = models.PositiveIntegerField(default=0)        # team format
    assists = models.PositiveIntegerField(default=0)       # team format
    # Per-player kill breakdown for a TEAM participant (owner 2026-06-12: manual entry shows the
    # roster and takes kills per player, "just like the manual input for the main leaderboard").
    # Shape: [{"name": str, "user_id": int|null, "kills": int}, ...]. When present, `kills` above
    # is the SERVER-computed sum of these (see views._save_one_result, mirroring the event flow's
    # enter_team_match_result_manual). Null for solo rows / rows entered without a breakdown.
    # JSON (not a child table) because standings never aggregate per player; this exists so the
    # breakdown is editable on reload and auditable.
    player_kills = models.JSONField(null=True, blank=True)
    bonus_points = models.IntegerField(default=0)
    penalty_points = models.IntegerField(default=0)
    # ── computed columns (set on save from scoring.*) ──
    placement_points = models.IntegerField(default=0)
    kill_points = models.IntegerField(default=0)
    total_points = models.IntegerField(default=0)
    played = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["match", "participant"], name="uniq_result_per_match_participant",
            ),
        ]

    def __str__(self):
        return f"R(p={self.participant_id}, m={self.match_id}) = {self.total_points}"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# OCR BATCH (Phase 2.6) — async, multi-image screenshot reading
#
# WHY THESE TWO MODELS EXIST
#   Reading a Free Fire result screenshot via Gemini takes ~12-26s. Doing it inside the HTTP request
#   (the old synchronous ocr_extract) exceeds the production request timeout (~30s gunicorn), so the
#   read died mid-flight and the admin saw "Could not read that screenshot" even though the engine was
#   working. The fix: BACKGROUND the read. The admin uploads one or more screenshots PER MAP, the server
#   persists them as a LeaderboardOcrJob (one job == one map) with status=pending, and a Celery task
#   (afc_leaderboard.tasks.process_leaderboard_ocr_job) reads every image, MERGES their placements into
#   that map's standings, matches names against the whole platform, and stores the review rows here.
#   The FE polls the job, the admin corrects the rows, then applies (one map + participants + results).
#
#   A batch = several jobs for one leaderboard. "Run all" enqueues every pending job so the maps process
#   in PARALLEL across Celery workers (the owner's "run them one by one or as a group simultaneously").
#
# HOW IT CONNECTS
#   - Producer of rows/status: afc_leaderboard.ocr.process_job (called by the Celery task), which reuses
#     afc_ocr.services.extract.extract_rows (the same local-first + Gemini-teacher engine the event flow
#     uses) and the platform-wide matchers.
#   - REST surface the FE (OcrBatchDialog) drives: afc_leaderboard.views.ocr_job_* (create/run/run-all/
#     list/apply/delete). Apply reuses _resolve_or_create_participant + _save_one_result (shared with the
#     manual add_participant / save_match_results paths) so there is no duplicate ghost/scoring logic.
# ════════════════════════════════════════════════════════════════════════════════════════════════
class LeaderboardOcrJob(models.Model):
    """One MAP's background OCR job inside a multi-image batch. Holds 1+ screenshots (LeaderboardOcrImage),
    processed off-request by Celery into review rows the admin corrects then applies. One job == one map."""

    STATUS_CHOICES = [
        ("pending", "Pending"),        # created, awaiting / queued for processing
        ("processing", "Processing"),  # the Celery task is reading its images
        ("done", "Done"),              # rows ready for admin review
        ("failed", "Failed"),          # read failed (see `error`); can be re-run
        ("applied", "Applied"),        # rows applied -> a map (applied_match) was created
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    leaderboard = models.ForeignKey(
        StandaloneLeaderboard, on_delete=models.CASCADE, related_name="ocr_jobs",
    )
    # Optional free-text map name the admin typed for this map (mirrors LeaderboardMatch.match_map).
    map_label = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    # The merged + platform-matched review rows (same shape the old ocr_extract returned), null until
    # the job is done. The FE renders these in the editable review table.
    rows = models.JSONField(null=True, blank=True)
    # Which engine produced the read ("gemini-2.5-flash", "local_student_vN", ...) — for the FE badge
    # + the training corpus. Blank until processed.
    engine = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")
    # The map created when this job's rows were applied. SET_NULL so deleting the map never blocks the
    # job row; lets the FE show "applied as Map N" and guards against a double-apply.
    applied_match = models.ForeignKey(
        LeaderboardMatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="ocr_jobs",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="leaderboard_ocr_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"OcrJob {self.id} ({self.status}) @ lb {self.leaderboard_id}"


class LeaderboardOcrImage(models.Model):
    """One screenshot belonging to a LeaderboardOcrJob. A single map can need several screens (e.g. the
    standings split across two images, or a top/bottom half); process_job reads each and merges them."""

    id = models.AutoField(primary_key=True)
    job = models.ForeignKey(LeaderboardOcrJob, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="leaderboard_ocr/")
    order = models.PositiveSmallIntegerField(default=0)
    # The raw per-image extraction ({"placements":[...]}) kept for debugging + as training material the
    # student-model learning loop can mine (Gemini is the teacher; see afc_ocr learning loop).
    raw_output = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"OcrImage {self.id} (job {self.job_id}, #{self.order})"
