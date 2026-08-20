"""
afc_results_import.models - importing a tournament AFC did not run, from a spreadsheet.

WHY THIS APP EXISTS
    AFC carries external tournaments (Free Fire World Series Africa is the driving case). Their
    organizers publish a standings GRAPHIC, not a match log, so the data available is usually a
    summed row per team per group: "6 matches, 3 Booyahs, 47 placement, 82 elims, 129 total". Some
    stages do publish per-match results. This app ingests both shapes from one workbook.

WHY A SEPARATE APP rather than more of afc_tournament_and_scrims/views.py: that module is already
    around 27,000 lines. A self-contained feature with its own parsing, resolution and commit rules
    is easier to read, test and delete as one unit.

HOW IT CONNECTS
    - Competitors resolve to afc_team.Team where AFC knows them, and otherwise to
      afc_rankings.GhostTeam, the unclaimed-competitor identity that already carries a claim
      lifecycle. TournamentTeam gained a ghost_team FK for exactly this.
    - Results are written as afc_tournament_and_scrims.TournamentTeamMatchStats. A summed stage uses
      the aggregate fields (is_aggregate / matches_counted / booyah_count / final_position); a
      per-match stage writes ordinary rows and touches none of them.
    - Stage and group membership is written as StageCompetitor / StageGroupCompetitor, because a
      team APPEARING in a stage's sheet is the statement that it advanced there. FFWS Play-ins
      Phase 1 advances "top 3 per group plus the 7 best 4th places", a cross-group rule with its own
      tie-break chain that no per-stage qualifying number can express, so membership is taken from
      the file rather than derived.

Spec: WEBSITE/tasks/external-results-import-design.md
Plan: WEBSITE/tasks/plan-3-xlsx-results-import.md
"""
from django.conf import settings
from django.db import models


class ResultsImport(models.Model):
    """One upload of one workbook against one event.

    A row is created at UPLOAD time holding the parsed PREVIEW and nothing else. Nothing touches the
    tournament tables until an admin confirms, which is the main safety property of this feature:
    a bad file produces a report, not a half-imported event.
    """

    STATUS = [
        ("previewed", "Previewed"),   # parsed, validated, nothing written
        ("committed", "Committed"),   # written to the tournament tables
        ("failed", "Failed"),         # parse or commit refused; see preview["problems"]
    ]

    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event", on_delete=models.CASCADE, related_name="results_imports",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="results_imports",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    source_filename = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="previewed")

    # What the parse found: sheets, rows, resolved competitors, warnings, rejections. This is what
    # the admin reads BEFORE deciding to commit, and what explains a refusal afterwards.
    preview = models.JSONField(default=dict, blank=True)
    # What the commit actually did: counts of rows written, teams created, groups replaced.
    summary = models.JSONField(default=dict, blank=True)

    # Spec section 9. TournamentPlayerMatchStats.player is a FK to a real User, and an external
    # tournament has no AFC accounts for its players (FFWS Play-ins Phase 1 alone is ~720 of them).
    # With this on, the import writes ZERO per-player rows, which is the only workable answer.
    # Consequences that are correct rather than gaps: no MVP for such a stage, and no contribution
    # to the per-player ladders.
    team_scores_only = models.BooleanField(default=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"Import of {self.source_filename or 'workbook'} into {self.event_id} ({self.status})"


class ExternalResultTeamAlias(models.Model):
    """What a name in a spreadsheet MEANS, for one event.

    THIS IS NOT A TEAM MERGE, deliberately. It records AFC's reading of one file and nothing more,
    so correcting a bad match or a renamed club is a single row update followed by a re-import. No
    rosters move, no history is rewritten, and there is nothing to undo if the correction is wrong.

    Scoped per EVENT on purpose: two tournaments can legitimately have different teams under similar
    names, and a global alias table would let one import's correction silently change another's.

    A claim is the OTHER tool and they are not interchangeable. An alias fixes a spelling; a claim is
    a real team asserting ownership of a ghost's history, goes through afc_rankings.admin_ghost
    approval, and moves ranked points. Using a claim to fix a typo would be wrong, and using an alias
    to hand a team its history would bypass an approval that exists on purpose.
    """

    RESOLUTION = [
        ("auto_matched", "Matched an existing AFC team"),
        ("matched_ghost", "Matched an existing ghost"),
        ("auto_created_ghost", "Created a new ghost"),
        ("manually_paired", "Paired by an admin"),
        ("skipped", "Skipped"),
    ]

    event = models.ForeignKey(
        "afc_tournament_and_scrims.Event", on_delete=models.CASCADE, related_name="result_aliases",
    )
    # Exactly as it appears in the file, kept verbatim so an admin can recognise it.
    source_name = models.CharField(max_length=255)
    # _norm_tname(source_name): what matching actually keys on. Clan tags and unicode glyphs (the
    # real FFWS standings contain a "TGR[]LEGENDS") normalise away here.
    normalized_name = models.CharField(max_length=255)

    tournament_team = models.ForeignKey(
        "afc_tournament_and_scrims.TournamentTeam", null=True, blank=True,
        on_delete=models.CASCADE, related_name="result_aliases",
    )
    resolution = models.CharField(max_length=32, choices=RESOLUTION)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="result_aliases_resolved",
    )

    class Meta:
        constraints = [
            # One meaning per spelling per event. A second import of the same file must reuse the
            # decision rather than resolve the name again and risk a different answer.
            models.UniqueConstraint(
                fields=["event", "normalized_name"],
                name="uniq_result_alias_per_event",
            ),
        ]

    def __str__(self):
        return f"{self.source_name!r} -> {self.tournament_team_id} ({self.resolution})"
