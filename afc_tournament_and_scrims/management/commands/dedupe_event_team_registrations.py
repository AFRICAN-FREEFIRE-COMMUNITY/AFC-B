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

GHOSTS (owner 2026-08-20, external results import): a TournamentTeam may now point at an
afc_rankings.GhostTeam instead of a real Team, and a ghost row has team_id = None (see
TournamentTeam.team's docstring in models.py). Two problems that plain (event, team) grouping
creates for ghosts:
  1. EVERY ghost in an event shares the bucket (event, None), so the command would read all of
     them as one giant duplicate group and delete every ghost but one. Fixed by adding ghost_team
     to the grouping key (below), the same fix applied to dedupe_tournament_teams.py.
  2. RegisteredCompetitors.team is ALSO nullable, but for a completely different reason: team=None
     there means a SOLO player registration, not a ghost (RegisteredCompetitors has no ghost_team
     column at all). Collapsing "extra" RegisteredCompetitors rows with filter(team_id=None) for a
     ghost group would therefore match and delete EVERY solo player's registration in the event,
     not the ghost's own rows. The apply step below only runs that collapse for real-team groups.

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

    @staticmethod
    def _describe(tt):
        # Mirrors dedupe_tournament_teams.Command._describe: a ghost's team_id is always None, so
        # printing "team None" in the report would read as a bug instead of a real ghost registration.
        if tt.is_ghost:
            return f"ghost {tt.ghost_team_id} ({tt.display_name})"
        return f"team {tt.team_id}"

    def handle(self, *args, **options):
        apply = options["apply"]

        # Bucket every registration by (event, team, ghost_team), lowest id first. team_id and
        # ghost_team_id are never both set (tt_team_xor_ghost), so a real row's bucket is
        # (event, <id>, None) and a ghost's is (event, None, <uuid>) - the two kinds never collide
        # with each other, only with a genuine duplicate of their own kind. See the GHOSTS note in
        # the module docstring for what a plain (event, team) key used to do to every ghost.
        groups = defaultdict(list)
        for tt in TournamentTeam.objects.all().order_by("tournament_team_id"):
            groups[(tt.event_id, tt.team_id, tt.ghost_team_id)].append(tt)

        dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}

        if not dupe_groups:
            self.stdout.write(self.style.SUCCESS("No duplicate (event, team) registrations. Safe to add the constraint."))
            return

        total_extra = sum(len(v) - 1 for v in dupe_groups.values())
        self.stdout.write(
            f"Found {len(dupe_groups)} duplicated (event, team) group(s), {total_extra} extra row(s) to remove:"
        )

        # Report (and, when --apply, delete) each group.
        # Sort key: team_id/ghost_team_id compared as strings, not raw values - within the SAME event
        # one dupe group can be a real-team group (team_id an int, ghost_team_id None) and another a
        # ghost group (team_id None, ghost_team_id a uuid). Sorting the raw tuples would try to compare
        # an int (or uuid) to None once event_id ties, which raises TypeError.
        to_delete_ids = []
        for (event_id, team_id, ghost_team_id), rows in sorted(
            dupe_groups.items(), key=lambda item: (item[0][0], str(item[0][1]), str(item[0][2]))
        ):
            # Prefer a row that carries match stats (deleting it would orphan its stats);
            # otherwise the lowest tournament_team_id.
            survivor = next(
                (tt for tt in rows if TournamentTeamMatchStats.objects.filter(tournament_team=tt).exists()),
                rows[0],
            )
            losers = [tt for tt in rows if tt.tournament_team_id != survivor.tournament_team_id]
            to_delete_ids.extend(tt.tournament_team_id for tt in losers)
            self.stdout.write(
                f"  - event {event_id}, {self._describe(survivor)}: keep #{survivor.tournament_team_id}"
                f"{' (has stats)' if survivor is not rows[0] else ''}, "
                f"delete {[tt.tournament_team_id for tt in losers]}"
            )

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY-RUN. Re-run with --apply to collapse the dupes."))
            return

        with transaction.atomic():
            deleted = 0
            for (event_id, team_id, ghost_team_id), rows in dupe_groups.items():
                survivor = next(
                    (tt for tt in rows if TournamentTeamMatchStats.objects.filter(tournament_team=tt).exists()),
                    rows[0],
                )
                for tt in rows:
                    if tt.tournament_team_id != survivor.tournament_team_id:
                        tt.delete()  # cascades members + stage/group seeds via their FKs
                        deleted += 1
                # Collapse duplicate (event, team) registration rows to the earliest - REAL TEAMS ONLY.
                # RegisteredCompetitors has no ghost_team column; team=None on that model means a SOLO
                # player registration, a completely different population from a ghost. For a ghost
                # group team_id is None here, and filter(team_id=None) would match every solo player's
                # registration in the event rather than the ghost's own rows, deleting almost all of
                # them. Ghosts have no RegisteredCompetitors row to collapse in the first place, so this
                # step is skipped entirely for a ghost group.
                if team_id is not None:
                    rc_rows = list(
                        RegisteredCompetitors.objects.filter(event_id=event_id, team_id=team_id).order_by("id")
                    )
                    for rc in rc_rows[1:]:
                        rc.delete()

        self.stdout.write(self.style.SUCCESS(f"\nDone. Removed {deleted} duplicate registration row(s)."))
