"""
Dedupe duplicate (match, team) TournamentTeamMatchStats rows.

WHY THIS EXISTS (owner 2026-07-06, "leaderboards show wrong scores"): TournamentTeamMatchStats
had no unique constraint on (match, tournament_team), so a team could hold TWO stats rows for
the SAME match (pre-2026-06-29 foreign-log residue: a ringer block was credited to a site team
that already had a row). The standings builder sums Sum(total_points) and Count(match_id) across
every row, so the duplicate DOUBLE-COUNTS: the team's total and matches-played inflate and it
renders twice in the match grid (event 134 "Alpha Wolves" showed 84 / 7 played instead of the
correct 70 / 5). The fix adds a UniqueConstraint(match, tournament_team) on the model so it can
never recur.

But a bare AddConstraint FAILS while those duplicates still exist. Migrations are gitignored in
this repo (prod runs `makemigrations` + `migrate` itself), so any dedupe baked into a local
migration does NOT reach prod. Therefore prod must run THIS command to collapse the existing
dupes BEFORE generating + applying the constraint migration:

    python manage.py dedupe_team_match_stats             # dry-run, report only
    python manage.py dedupe_team_match_stats --apply      # collapse the dupes
    python manage.py makemigrations afc_tournament_and_scrims   # generates the AddConstraint
    python manage.py migrate                              # now succeeds (no dupes)

Survivor rule: within each (match, team) group keep the LOWEST team_stats_id and delete the
rest; each deleted row's TournamentPlayerMatchStats cascade via their FK. Because the surviving
row's stored total may itself be a wrong single value, the authoritative fix is to RE-UPLOAD the
affected matches afterwards (upload clears the match's rows and recomputes from the file); this
command only removes the double-count so the constraint can apply and the inflation stops.

Read by: ops. Touches afc_tournament_and_scrims.TournamentTeamMatchStats. Pairs with the
UniqueConstraint on TournamentTeamMatchStats.Meta (models.py) and the recompute-on-config-edit
fix in views._recompute_team_match_points.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from afc_tournament_and_scrims.models import TournamentTeamMatchStats


class Command(BaseCommand):
    help = "Collapse duplicate (match, team) TournamentTeamMatchStats rows to one, before adding the unique constraint."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the deletes. Without this flag the command only reports (dry-run).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        # Bucket every stats row by its (match, team) pair, lowest id first.
        groups = defaultdict(list)
        for row in TournamentTeamMatchStats.objects.all().order_by("team_stats_id"):
            groups[(row.match_id, row.tournament_team_id)].append(row)

        dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}

        if not dupe_groups:
            self.stdout.write(self.style.SUCCESS("No duplicate (match, team) stats rows. Safe to add the constraint."))
            return

        total_extra = sum(len(v) - 1 for v in dupe_groups.values())
        self.stdout.write(
            f"Found {len(dupe_groups)} duplicated (match, team) group(s), {total_extra} extra row(s) to remove:"
        )

        for (match_id, team_id), rows in sorted(dupe_groups.items()):
            survivor = rows[0]  # lowest team_stats_id
            losers = rows[1:]
            self.stdout.write(
                f"  - match {match_id}, team {team_id}: keep #{survivor.team_stats_id} "
                f"(total {survivor.total_points}), delete "
                f"{[(r.team_stats_id, r.total_points) for r in losers]}"
            )

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDRY-RUN. Re-run with --apply to collapse the dupes, then RE-UPLOAD the affected "
                "matches for authoritative values."
            ))
            return

        with transaction.atomic():
            deleted = 0
            for rows in dupe_groups.values():
                for row in rows[1:]:  # keep rows[0] (lowest id), delete the rest
                    row.delete()  # cascades TournamentPlayerMatchStats via their FK
                    deleted += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Removed {deleted} duplicate stats row(s). Now RE-UPLOAD the affected matches so "
            f"the surviving totals are recomputed from the source files."
        ))
