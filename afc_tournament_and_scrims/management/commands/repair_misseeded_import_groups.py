"""
Remove competitors a BUGGY results import seeded into the wrong group.

WHY THIS EXISTS (owner 2026-08-23, found importing FFWS Africa 2026 Fall): before the fix in
afc_results_import.services._target_group, a sheet named "<stage> - <group>" could be matched to a
DIFFERENT stage's group of the same letter, because the matcher accepted a loose
`sheet_name.endswith(group_name)` fallback in the same pass as the exact match and returned whichever
row the queryset happened to yield first. On a multi-phase event, which is the ordinary shape, every
"Phase 2 - Group A" sheet could therefore land on "Phase 1 - Group A".

Two separate kinds of damage came out of that, and re-importing only repairs ONE of them:

  1. THE STATS. commit_import deletes and rewrites the rows it owns (upload_method="xlsx_import")
     for each group it writes, so simply re-importing with the fixed matcher puts the right numbers
     back in the right group. Nothing to do here.

  2. THE GROUP MEMBERSHIP. Appearing in a sheet is what puts a team in a stage and a group, and that
     is written with StageGroupCompetitor.objects.get_or_create(...) - which only ever ADDS. Nothing
     removes a membership a previous import created, so after re-importing, the wrongly-seeded teams
     are STILL listed in the group they never played in, now showing 0 points. On FFWS that left
     Phase 1 Group A holding 23 competitors: its own 12 plus 11 strays from Phase 2.

This command removes exactly those strays. THE SIGNATURE IT KEYS ON is "a competitor in a group
whose results came from an import, that has no stats row in that group": a correctly imported team
always has one, because the same loop that seeds it also writes it. It is deliberately NOT a general
"remove competitors with no results" tool, which would delete legitimately seeded teams in an
ordinary AFC event that simply has not been played yet.

SCOPED, AND SAFE BY DEFAULT:
  * --event <slug> is REQUIRED. This never sweeps the whole database.
  * Only groups that actually hold imported results are considered, so a hand-run group in the same
    event is not touched even if it is seeded and unplayed.
  * Dry-run unless --apply, printing every removal it would make.
  * A StageCompetitor (stage-level membership) is dropped only when the team is left in NO group of
    that stage, so a team that genuinely plays another group of the same stage keeps its place.

    python manage.py repair_misseeded_import_groups --event ffws-fall-ssa
    python manage.py repair_misseeded_import_groups --event ffws-fall-ssa --apply

CONNECTS TO: afc_results_import.services.commit_import (what created the rows), the fixed
_target_group (what stops it recurring), and the admin Results Import tab that runs both.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from afc_tournament_and_scrims.models import (
    Event, StageGroups, StageGroupCompetitor, StageCompetitor,
    TournamentTeamMatchStats, Match,
)

# Matches afc_results_import.services.UPLOAD_METHOD. Imported here as a literal rather than by
# import so this command still runs if the results-import app is ever removed.
UPLOAD_METHOD = "xlsx_import"


class Command(BaseCommand):
    help = ("Remove competitors that a buggy results import seeded into the wrong group. "
            "Dry-run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--event", required=True,
                            help="Event slug to repair. Required; this never runs event-wide.")
        parser.add_argument("--apply", action="store_true",
                            help="Actually delete. Without this the command only reports.")

    def handle(self, *args, **opts):
        slug = opts["event"]
        apply_changes = opts["apply"]
        try:
            event = Event.objects.get(slug=slug)
        except Event.DoesNotExist:
            raise CommandError(f"No event with slug {slug!r}.")

        groups = (StageGroups.objects.filter(stage__event=event)
                  .select_related("stage")
                  .order_by("stage__stage_order", "stage_id", "group_order", "group_id"))

        total_removed = 0
        for group in groups:
            # Only groups whose results came from an import. A group that was played and scored on
            # AFC has nothing to do with this bug and must not be touched.
            if not Match.objects.filter(group=group, upload_method=UPLOAD_METHOD).exists():
                continue

            scored_ids = set(
                TournamentTeamMatchStats.objects
                .filter(match__group=group)
                .values_list("tournament_team_id", flat=True))

            strays = (StageGroupCompetitor.objects
                      .filter(stage_group=group)
                      .exclude(tournament_team_id__in=scored_ids)
                      .select_related("tournament_team__team", "tournament_team__ghost_team"))

            if not strays.exists():
                continue

            label = f"{group.stage.stage_name} / {group.group_name}"
            self.stdout.write(f"{label}: {strays.count()} competitor(s) with no result here")
            for sgc in strays:
                self.stdout.write(f"    - {sgc.tournament_team.display_name}")

            if not apply_changes:
                total_removed += strays.count()
                continue

            with transaction.atomic():
                stray_tt_ids = list(strays.values_list("tournament_team_id", flat=True))
                removed, _ = StageGroupCompetitor.objects.filter(
                    stage_group=group, tournament_team_id__in=stray_tt_ids).delete()
                total_removed += len(stray_tt_ids)

                # Drop the stage-level seat only for a team now in NO group of this stage. A team
                # that legitimately plays another group of the same stage keeps its StageCompetitor.
                for tt_id in stray_tt_ids:
                    still_in_stage = StageGroupCompetitor.objects.filter(
                        stage_group__stage=group.stage, tournament_team_id=tt_id).exists()
                    if not still_in_stage:
                        StageCompetitor.objects.filter(
                            stage=group.stage, tournament_team_id=tt_id).delete()

        if total_removed == 0:
            self.stdout.write(self.style.SUCCESS(
                f"{slug}: nothing to repair; every imported group holds only teams with results."))
            return

        if apply_changes:
            self.stdout.write(self.style.SUCCESS(
                f"{slug}: removed {total_removed} mis-seeded competitor(s)."))
        else:
            self.stdout.write(self.style.WARNING(
                f"{slug}: {total_removed} mis-seeded competitor(s) would be removed. "
                f"Re-run with --apply to do it."))
