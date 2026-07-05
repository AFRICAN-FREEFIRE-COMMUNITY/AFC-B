"""
Dedupe duplicate (event, team) TournamentTeam registrations.

WHY THIS EXISTS (owner 2026-07-05, Bug C): a race in register_for_event /
add_teams_to_event let the SAME team register twice for one event (twin rows, e.g. a
team showing as both #15 and #16), which pushed a 15-team event to 16 registered
("-1 slot left") and the duplicate could not be removed from the UI. The fix adds a
UniqueConstraint(event, team) on TournamentTeam so it can never happen again.

But a bare AddConstraint FAILS if the table already holds those duplicates. Migrations
are gitignored in this repo (prod runs `makemigrations` + `migrate` itself), so the
dedupe logic in the local 0050 migration does NOT reach prod. Therefore prod must run
THIS command to clean the existing dupes BEFORE generating + applying the constraint
migration:

    python manage.py dedupe_event_team_registrations            # dry-run, report only
    python manage.py dedupe_event_team_registrations --apply     # collapse the dupes
    python manage.py makemigrations afc_tournament_and_scrims     # generates the AddConstraint
    python manage.py migrate                                      # now succeeds (no dupes)

Survivor rule: within each (event, team) group keep the row that HAS match stats
(TournamentTeamMatchStats) so a scored row is never deleted; otherwise keep the LOWEST
tournament_team_id. Every other row is deleted (its members + stage/group seeds cascade
via their FKs). RegisteredCompetitors is not linked to a specific TournamentTeam, so the
(event, team) RegisteredCompetitors rows are also collapsed to the earliest one.

SAFE BY DEFAULT: dry-run unless --apply. Mirrors the dedupe in migration 0050 exactly,
so running either (command first on prod, or the migration locally) reaches the same state.

Read by: ops. Touches afc_tournament_and_scrims.TournamentTeam + RegisteredCompetitors.
Pairs with the UniqueConstraint on TournamentTeam.Meta (models.py) + the register/remove
fixes in views.py.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from afc_tournament_and_scrims.models import (
    RegisteredCompetitors,
    TournamentTeam,
    TournamentTeamMatchStats,
)


class Command(BaseCommand):
    help = "Collapse duplicate (event, team) TournamentTeam rows to one, before adding the unique constraint."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the deletes. Without this flag the command only reports (dry-run).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        # Bucket every registration by its (event, team) pair, lowest id first.
        groups = defaultdict(list)
        for tt in TournamentTeam.objects.all().order_by("tournament_team_id"):
            groups[(tt.event_id, tt.team_id)].append(tt)

        dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}

        if not dupe_groups:
            self.stdout.write(self.style.SUCCESS("No duplicate (event, team) registrations. Safe to add the constraint."))
            return

        total_extra = sum(len(v) - 1 for v in dupe_groups.values())
        self.stdout.write(
            f"Found {len(dupe_groups)} duplicated (event, team) group(s), {total_extra} extra row(s) to remove:"
        )

        # Report (and, when --apply, delete) each group.
        to_delete_ids = []
        for (event_id, team_id), rows in sorted(dupe_groups.items()):
            # Prefer a row that carries match stats (deleting it would orphan its stats);
            # otherwise the lowest tournament_team_id.
            survivor = next(
                (tt for tt in rows if TournamentTeamMatchStats.objects.filter(tournament_team=tt).exists()),
                rows[0],
            )
            losers = [tt for tt in rows if tt.tournament_team_id != survivor.tournament_team_id]
            to_delete_ids.extend(tt.tournament_team_id for tt in losers)
            self.stdout.write(
                f"  - event {event_id}, team {team_id}: keep #{survivor.tournament_team_id}"
                f"{' (has stats)' if survivor is not rows[0] else ''}, "
                f"delete {[tt.tournament_team_id for tt in losers]}"
            )

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY-RUN. Re-run with --apply to collapse the dupes."))
            return

        with transaction.atomic():
            deleted = 0
            for (event_id, team_id), rows in dupe_groups.items():
                survivor = next(
                    (tt for tt in rows if TournamentTeamMatchStats.objects.filter(tournament_team=tt).exists()),
                    rows[0],
                )
                for tt in rows:
                    if tt.tournament_team_id != survivor.tournament_team_id:
                        tt.delete()  # cascades members + stage/group seeds via their FKs
                        deleted += 1
                # Collapse duplicate (event, team) registration rows to the earliest.
                rc_rows = list(
                    RegisteredCompetitors.objects.filter(event_id=event_id, team_id=team_id).order_by("id")
                )
                for rc in rc_rows[1:]:
                    rc.delete()

        self.stdout.write(self.style.SUCCESS(f"\nDone. Removed {deleted} duplicate registration row(s)."))
