import uuid
from django.db import models


class OCRSession(models.Model):
    """
    Server-side draft for one map's OCR result.
    Persists until the admin commits or discards it.
    """

    STATUS_CHOICES = [
        ("pending_review", "Pending Review"),
        ("committed",      "Committed"),
        ("discarded",      "Discarded"),
    ]

    EVENT_TYPE_CHOICES = [
        ("solo", "Solo"),
        ("team", "Team"),
    ]

    session_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    match = models.ForeignKey(
        "afc_tournament_and_scrims.Match",
        on_delete=models.CASCADE,
        related_name="ocr_sessions",
    )
    map_index = models.PositiveSmallIntegerField(
        help_text="Which map in the match this screenshot covers (1-indexed)."
    )
    # ── learning-loop link to the committed screenshot ───────────────────────
    # The uploaded screenshot bytes used to be read in upload_ocr_session, fed to
    # Gemini, and then DISCARDED (only raw_output + draft_rows survived). For the
    # self-hosted OCR learning loop (Phase 1) we must keep the exact pixels so that
    # at commit-time training_capture.capture_training_pair can re-read them and
    # content-address them into the training set. We reuse the existing
    # MatchResultImage model (afc_tournament_and_scrims.MatchResultImage, an
    # ImageField the app already uses for manual result uploads) so we do not add a
    # second image store. upload_ocr_session / ocr_from_stored_image set this FK;
    # commit_ocr_session -> capture_training_pair reads it back. SET_NULL so deleting
    # an image (or its match) never blocks an in-flight session.
    image = models.ForeignKey(
        "afc_tournament_and_scrims.MatchResultImage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ocr_sessions",
    )
    created_by = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.CASCADE,
        related_name="ocr_sessions",
    )
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending_review")
    event_type = models.CharField(max_length=10, choices=EVENT_TYPE_CHOICES)

    # Raw Gemini output — stored for debugging / re-processing
    raw_output = models.JSONField()

    # Matched & annotated rows ready for the review table
    draft_rows = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"OCRSession {self.session_id} | Match {self.match_id} | Map {self.map_index}"


class OCRNameAlias(models.Model):
    """
    Maps a raw OCR name (as Gemini reads it) to the correct registered user.
    Built passively from admin corrections.
    """

    raw_name    = models.CharField(max_length=100, db_index=True, unique=True)
    user        = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="ocr_aliases",
    )
    match_count = models.PositiveIntegerField(default=1)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "OCR Name Alias"
        verbose_name_plural = "OCR Name Aliases"

    def __str__(self):
        return f'"{self.raw_name}" → {self.user}'


class OCRTeamNote(models.Model):
    """
    Records when a player was confirmed to have played for a team
    other than their registered team (sub / stand-in).
    """

    user = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.CASCADE,
        related_name="team_notes",
    )
    registered_team = models.ForeignKey(
        "afc_team.Team",
        on_delete=models.CASCADE,
        related_name="sub_out_notes",
        null=True, blank=True,
    )
    played_for_team = models.ForeignKey(
        "afc_team.Team",
        on_delete=models.CASCADE,
        related_name="sub_in_notes",
    )
    match = models.ForeignKey(
        "afc_tournament_and_scrims.Match",
        on_delete=models.CASCADE,
    )
    confirmed_by = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="confirmed_team_notes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} subbed for {self.played_for_team} in match {self.match_id}"


# ═══════════════════════════════════════════════════════════════════════════
# OCR LEARNING LOOP — Phase 1: training-data capture
# ═══════════════════════════════════════════════════════════════════════════
#
# Goal: every time an admin reviews a Gemini OCR result and commits it to the
# leaderboard, we already have a perfectly labeled example, the admin-confirmed
# truth, of what that screenshot says. Phase 1 captures that truth as durable
# training data so that later phases can train a self-hosted "student" OCR model
# (and stop paying per-screenshot for the Gemini "teacher").
#
# Two models, two granularities:
#   - OCRTrainingPair  = one screenshot  -> one admin-confirmed JSON result.
#   - OCRCropLabel     = one cell of that screenshot -> its exact text label.
#
# How it connects to the rest of the system:
#   - Producer: afc_ocr.services.training_capture.capture_training_pair, called
#     from afc_ocr.views.commit_ocr_session AFTER the leaderboard write succeeds
#     (so capturing data can never break a real commit).
#   - Inputs it consumes: OCRSession.image (the screenshot, via MatchResultImage),
#     OCRSession.raw_output (the Gemini engine output), and final_rows (the
#     admin-confirmed review rows, same shape matching.match_name produces).
#   - Consumers (future phases): an offline exporter that turns these rows into a
#     dataset (the `split` / `dataset_version` fields drive train/eval/holdout),
#     and a layout cropper that fills OCRCropLabel.crop_path from image+final_json.
#
# IMPORTANT distinction these models encode on purpose:
#   recognition-truth vs identity-truth.
#     - recognition-truth = what the PIXELS literally say. For a name cell that is
#       the exact on-screen string (OCRCropLabel.text where field='name'); for a
#       kill cell it is the digit string. This is the CTC/transcription target the
#       student OCR model learns to reproduce.
#     - identity-truth = WHO that string resolves to (the registered AFC user). That
#       is OCRCropLabel.matched_user_id, kept in a SEPARATE field. We never collapse
#       the two: the same pixels "Pro_Sniper" might map to different users across
#       events, and an OCR model must learn to read the pixels, not to guess the
#       roster. Mixing them would teach the model to hallucinate names it has seen
#       before instead of transcribing what is on screen.
# ═══════════════════════════════════════════════════════════════════════════


def _assign_split(image_sha256: str) -> str:
    """
    Deterministically bucket a screenshot into train / eval / holdout from its
    content hash, roughly 80 / 10 / 10.

    Why hash-based (not random): the split must be STABLE and reproducible. The
    same screenshot (same sha256) always lands in the same bucket, even if it is
    re-uploaded or re-captured later, so a screenshot can never leak from the
    training set into the eval/holdout set (which would inflate eval scores). We
    bucket on the FULL image hash, not per-crop, so every crop from one screenshot
    shares that screenshot's split (no leakage between train and eval within a
    single image). Uses the first 8 hex chars of the sha256 as a stable integer.
    """
    # int(...,16) over 8 hex chars gives a uniform value in [0, 16^8); mod 10
    # then carves 0-7 -> train (80%), 8 -> eval (10%), 9 -> holdout (10%).
    try:
        bucket = int(image_sha256[:8], 16) % 10
    except (TypeError, ValueError):
        # Defensive: a missing/garbage hash must not crash capture. Default to the
        # largest bucket so a stray bad row still trains rather than silently
        # polluting eval/holdout.
        return "train"
    if bucket < 8:
        return "train"
    if bucket == 8:
        return "eval"
    return "holdout"


class OCRTrainingPair(models.Model):
    """
    One captured training example: a single screenshot paired with the
    admin-confirmed recognition truth for that whole screen.

    This is the screen-level record. The per-cell labels (which the student OCR
    model actually trains on) live in the related OCRCropLabel rows. One pair has
    many crop labels.

    Lifecycle / connections:
      - Created by capture_training_pair (called from commit_ocr_session) the
        moment an admin commits a reviewed OCR session.
      - `session` / `match` are soft links (SET_NULL) so this training row outlives
        the session or match it came from. Training data is the point; it must not
        vanish when an admin deletes a session or a match is removed.
      - `final_json` is the same {placements:[{placement, players:[{name, kills}]}]}
        shape the review table already speaks, so an exporter can reconstruct the
        full label without re-reading the per-crop rows.
    """

    SOURCE_CHOICES = [
        ("admin_review",     "Admin Review"),      # confirmed by a human during commit (Phase 1)
        ("gemini_autolabel", "Gemini Autolabel"),  # future: teacher-labeled, no human in the loop
        ("synthetic",        "Synthetic"),         # future: generated/augmented samples
    ]

    EVENT_TYPE_CHOICES = [
        ("solo", "Solo"),
        ("team", "Team"),
    ]

    SPLIT_CHOICES = [
        ("train",   "Train"),
        ("eval",    "Eval"),
        ("holdout", "Holdout"),
    ]

    pair_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Soft links back to where this example came from (both nullable on purpose:
    # the training row must survive deletion of either the session or the match).
    session = models.ForeignKey(
        "afc_ocr.OCRSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="training_pairs",
    )
    match = models.ForeignKey(
        "afc_tournament_and_scrims.Match",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ocr_training_pairs",
    )

    # Content address of the screenshot: sha256 of the exact bytes. Indexed because
    # the exporter and the dedupe path both look pairs up by hash (same screenshot
    # committed twice should not become two unrelated training examples).
    image_sha256 = models.CharField(max_length=64, db_index=True)
    # Storage path of the deduped copy under media/ocr_training/<sha256>.<ext>.
    image_path = models.CharField(max_length=255)

    event_type = models.CharField(max_length=10, choices=EVENT_TYPE_CHOICES)

    # raw_output the engine produced (Gemini today), kept verbatim so we can later
    # measure teacher-vs-truth drift and mine the hardest examples.
    raw_output = models.JSONField()
    # The admin-confirmed recognition truth for the whole screen:
    #   {"placements": [{"placement": int, "players": [{"name": str, "kills": int}]}]}
    final_json = models.JSONField()

    source        = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="admin_review")
    # Which engine produced raw_output (e.g. "gemini-2.5-pro", "local_student_vN",
    # "hybrid"). Null when unknown. Lets us track which teacher labeled what.
    teacher_model = models.CharField(max_length=64, null=True, blank=True)

    # Quality / difficulty signals, computed at capture from the diff between the
    # Gemini draft and the admin-confirmed final:
    num_corrections = models.IntegerField(
        default=0,
        help_text="How many review rows the admin changed (identity, kills, or read text) vs the Gemini draft.",
    )
    edit_distance = models.FloatField(
        default=0,
        help_text="Aggregate text edit distance between draft and confirmed text (reserved for later scoring).",
    )
    # is_clean = the admin made zero corrections (Gemini nailed it). Cheap filter
    # for building a high-precision subset later.
    is_clean = models.BooleanField(default=True)

    # Dataset bookkeeping. `split` is assigned deterministically from image_sha256
    # at insert (see _assign_split) so a screenshot's bucket is stable forever.
    split           = models.CharField(max_length=10, choices=SPLIT_CHOICES, db_index=True)
    dataset_version = models.CharField(max_length=32, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "afc_auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ocr_training_pairs",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name        = "OCR Training Pair"
        verbose_name_plural = "OCR Training Pairs"

    def __str__(self):
        return f"OCRTrainingPair {self.pair_id} | {self.source} | {self.split}"


class OCRCropLabel(models.Model):
    """
    One labeled cell of a training screenshot: the exact text a single name- or
    kill-cell shows. These rows are what the student OCR model actually trains on.

    Recognition vs identity (see the module header above):
      - `text` is recognition-truth: the exact on-screen string. For field='name'
        it is the player name as the pixels show it; for field='kills' it is the
        digit string. This is the CTC target.
      - `matched_user_id` is identity-truth: WHO the name resolves to. Stored
        separately and only on name rows. It is metadata for analysis / linking
        back to the roster, NOT a transcription target. The OCR model must learn
        to read `text`, never to predict `matched_user_id`.

    crop_path is intentionally BLANK in Phase 1. The actual pixel crop of this cell
    is derived OFFLINE later: a future "layout cropper" phase takes the full image
    plus final_json and cuts each cell out, then back-fills crop_path. Phase 1's job
    is only to capture the text-level labels (and full OCRTrainingPair), so the crops
    can be generated deterministically from image + labels whenever the cropper ships.
    """

    FIELD_CHOICES = [
        ("name",  "Name"),   # recognition target = the on-screen player name string
        ("kills", "Kills"),  # recognition target = the on-screen kill-count digits
    ]

    crop_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pair = models.ForeignKey(
        "afc_ocr.OCRTrainingPair",
        on_delete=models.CASCADE,        # crop labels are meaningless without their pair
        related_name="crop_labels",
    )

    # Path to the cropped cell image. BLANK in Phase 1 (populated later by the
    # offline layout cropper from image_path + final_json). Defaults to '' so the
    # column is never null and the cropper can simply fill empties.
    crop_path = models.CharField(max_length=255, blank=True, default="")

    field = models.CharField(max_length=10, choices=FIELD_CHOICES)
    # Recognition-truth: the EXACT string the pixels show (CTC target). For kills
    # this is the digit string, e.g. "3".
    text = models.CharField(max_length=100)
    placement = models.IntegerField()

    # Identity-truth (name rows only): the registered user this name resolves to.
    # Null for kill rows and for unresolved names. Kept SEPARATE from `text` on
    # purpose (see header) — read the pixels, do not guess the roster.
    matched_user_id = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = "OCR Crop Label"
        verbose_name_plural = "OCR Crop Labels"

    def __str__(self):
        return f'OCRCropLabel {self.field}="{self.text}" @P{self.placement}'
