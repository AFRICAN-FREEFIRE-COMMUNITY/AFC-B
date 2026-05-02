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
