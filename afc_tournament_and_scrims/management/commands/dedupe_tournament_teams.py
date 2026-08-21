"""
Dedupe duplicate (event, team) TournamentTeam rows.

WHY THIS EXISTS (owner 2026-07-06, prod migrate failure): TournamentTeam had no unique constraint
on (event, team), so a team could be registered TWICE for the same event (a double-registration
race - the app-level guard in event_links._promote / register_for_event now prevents NEW ones, but
prod already holds strays). Adding the uniq_event_team_registration constraint (models.py Meta) then
FAILS on prod:

    MySQLdb.IntegrityError: (1062, "Duplicate entry '135-244' for key '...uniq_event_team_registration'")

Migrations are gitignored in this repo (prod runs makemigrations + migrate itself), so a dedupe baked
into a local migration never reaches prod. Prod must run THIS command to MERGE the duplicate team rows
into one BEFORE the constraint migration applies:

    python manage.py dedupe_tournament_teams              # dry-run, report only
    python manage.py dedupe_tournament_teams --apply       # merge the dupes
    python manage.py dedupe_team_match_stats --apply        # then collapse any (match, team) stat dups
    python manage.py makemigrations afc_tournament_and_scrims
    python manage.py migrate                                # now succeeds (no dupes)

SURVIVOR RULE: within each (event, team) group keep the row with the MOST real data - most
match_stats, then most roster members, then the LOWEST id as a stable tiebreak. That survivor is the
canonical registration every other surface already references.

GHOSTS (owner 2026-08-20, external results import): a TournamentTeam may now point at an
afc_rankings.GhostTeam instead of a real Team, and a ghost row has team_id = None (see
TournamentTeam.team's docstring in models.py). Grouping used to be keyed on (event, team) alone, so
EVERY ghost in an event shared the single bucket (event, None) and the command read them all as one
giant duplicate group to merge, deleting every ghost but one and reattaching their match stats to an
unrelated survivor. The grouping key below adds ghost_team so real rows and ghost rows never collide
on a shared "team_id is None" bucket; a genuine duplicate of either kind still groups together and is
still merged exactly as before.

SAFE MERGE (never orphan / never lose data): we do NOT bare-delete a duplicate (that would cascade
away its children). Instead we walk EVERY reverse relation Django knows points at TournamentTeam
(TournamentTeam._meta.related_objects - TournamentTeamMember, TournamentTeamMatchStats, MatchKillFlag,
StageCompetitor, StageGroupCompetitor, the H2H team_a/team_b/winner and UnmatchedTeamBlock.attributed
SET_NULL links, plus anything added later in any app) and REPOINT each child from the loser to the
survivor. If a repoint would violate a child's own unique key (e.g. the survivor already has a stats
row for that match, or the same roster member), that child is a genuine duplicate and is dropped
instead of repointed. The one many-to-many (RoundRobinGroup.teams) is remapped explicitly. Only once a
loser has no remaining references is it deleted.

Read by: ops. Pairs with the UniqueConstraint on TournamentTeam.Meta (models.py) and the
double-registration guards in event_links.py. Run dedupe_team_match_stats AFTER this one so any
(match, team) stat collisions surfaced by the repoint are collapsed before uniq_team_stats_per_match.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction, IntegrityError

from afc_tournament_and_scrims.models import TournamentTeam, RoundRobinGroup


class Command(BaseCommand):
    help = ("Merge duplicate (event, team) TournamentTeam rows into one, repointing every child, "
            "before adding uniq_event_team_registration.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the merge. Without this flag the command only reports (dry-run).",
        )

    @staticmethod
    def _rank(tt):
        # Survivor preference: most match_stats, then most roster members. Both are reverse relations
        # keyed off tournament_team_id (the row's own PK), never off .team, so this ranking is exactly
        # as correct for a ghost row as for a real one - nothing here needs to change for ghosts.
        return (tt.match_stats.count(), tt.members.count())

    @staticmethod
    def _describe(tt):
        # A ghost's team_id is always None (see models.py), so printing "team None" in the report
        # would look like a bug rather than a real ghost registration. Name the competitor by kind
        # instead: real teams keep the numeric id ops already grep prod logs for; ghosts get their
        # GhostTeam uuid plus display_name so the report line still identifies exactly who is merging.
        if tt.is_ghost:
            return f"ghost {tt.ghost_team_id} ({tt.display_name})"
        return f"team {tt.team_id}"

    def handle(self, *args, **options):
        apply = options["apply"]

        # Bucket every TournamentTeam by (event, team, ghost_team). team_id and ghost_team_id are
        # never both set (tt_team_xor_ghost), so a real row's bucket is (event, <id>, None) and a
        # ghost's is (event, None, <uuid>) - the two kinds can never collide with each other, only
        # with a genuine duplicate of their own kind. See the GHOSTS note in the module docstring for
        # why the plain (event, team) key used to merge every ghost in an event into one group.
        groups = defaultdict(list)
        for tt in TournamentTeam.objects.all().order_by("tournament_team_id"):
            groups[(tt.event_id, tt.team_id, tt.ghost_team_id)].append(tt)
        dupes = {k: v for k, v in groups.items() if len(v) > 1}

        if not dupes:
            self.stdout.write(self.style.SUCCESS(
                "No duplicate (event, team) TournamentTeam rows. Safe to add uniq_event_team_registration."))
            return

        # Every reverse FK/O2O relation pointing at TournamentTeam (introspected, so nothing is missed
        # even if a new app adds one). The lone many-to-many (RoundRobinGroup.teams) is handled apart.
        fk_rels = [r for r in TournamentTeam._meta.related_objects if not r.many_to_many]

        # Resolve the survivor for each group up front so the dry-run shows exactly what --apply will do.
        # Sort key: team_id/ghost_team_id are compared as strings, not raw values, because within the
        # SAME event one dupe group can be a real-team group (team_id an int, ghost_team_id None) and
        # another a ghost group (team_id None, ghost_team_id a uuid) - sorting the raw tuples would try
        # to compare an int to None (or a uuid to None) once event_id ties, which raises TypeError.
        plan = []
        for (event_id, team_id, ghost_team_id), rows in sorted(
            dupes.items(), key=lambda item: (item[0][0], str(item[0][1]), str(item[0][2]))
        ):
            survivor = max(rows, key=lambda t: (self._rank(t), -t.tournament_team_id))
            losers = [r for r in rows if r.tournament_team_id != survivor.tournament_team_id]
            plan.append((survivor, losers))

        self.stdout.write(f"Found {len(dupes)} duplicated (event, team) group(s):")
        for survivor, losers in plan:
            self.stdout.write(
                f"  - event {survivor.event_id}, {self._describe(survivor)}: keep #{survivor.tournament_team_id} "
                f"(stats {survivor.match_stats.count()}, members {survivor.members.count()}); "
                f"merge {[(l.tournament_team_id, l.match_stats.count(), l.members.count()) for l in losers]}"
            )

        if not apply:
            self.stdout.write(self.style.WARNING(
                "\nDRY-RUN. Re-run with --apply to merge, then run dedupe_team_match_stats --apply."))
            return

        moved, dropped, deleted = 0, 0, 0
        with transaction.atomic():
            for survivor, losers in plan:
                sid = survivor.tournament_team_id
                for loser in losers:
                    lid = loser.tournament_team_id
                    # 1) Reverse FK / O2O children: repoint to the survivor; on a unique-key collision
                    #    the child duplicates one the survivor already has, so drop it instead.
                    for rel in fk_rels:
                        attname = rel.field.attname  # e.g. "tournament_team_id", "team_a_id", "winner_id"
                        for child in rel.related_model.objects.filter(**{attname: lid}):
                            try:
                                with transaction.atomic():  # per-row savepoint
                                    setattr(child, attname, sid)
                                    child.save(update_fields=[attname])
                                moved += 1
                            except IntegrityError:
                                child.delete()
                                dropped += 1
                    # 2) The one many-to-many: swap the loser for the survivor in every RR group it sat in.
                    for g in RoundRobinGroup.objects.filter(teams=loser):
                        g.teams.add(survivor)
                        g.teams.remove(loser)
                    # 3) The loser now has no references left -> delete the empty shell.
                    loser.delete()
                    deleted += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Repointed {moved} child row(s), dropped {dropped} colliding duplicate(s), "
            f"deleted {deleted} duplicate team registration(s). Now run: dedupe_team_match_stats --apply, "
            f"then makemigrations + migrate."))
