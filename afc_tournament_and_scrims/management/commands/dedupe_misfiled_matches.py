"""
Delete misfiled DUPLICATE matches - a match whose result rows are an EXACT copy of another match
in the same stage but sitting in the WRONG group.

WHY THIS EXISTS (owner 2026-07-06, event 172 SEMI FINALS): the round-robin "combined day" groups
(e.g. "Day 1: A+B", "Day 2: A+C", "Day 3: B+C") each hold that day's matches. A Day-2 (A+C) result
(match 3610, map Bermuda) got a second copy saved into the Day-1 (A+B) group as an extra 6th match
with a blank map (match 3644). Because every leaderboard aggregator scopes by `match__group=<group>`
(see round_robin._aggregate_team_standings and get_all_leaderboard_details_for_event), that stray
match dragged all of group C's teams (PARADOX, COSA NOSTRA, ...) and their points into the Day-1
board - teams that never played Day 1. Symptom the owner saw: the "Day 1: A+B" leaderboard listed 18
teams instead of the 12 (A+B) that actually played.

WHAT COUNTS AS A DUPLICATE (safe, conservative): within ONE stage, two matches whose set of
(tournament_team_id, placement, kills) rows is byte-for-byte identical AND that live in DIFFERENT
groups. Identical results in two different day-groups cannot happen legitimately - each real map is
uploaded once, to one group. We keep the SURVIVOR (the match with a real, non-empty match_map; if
both have maps we DO NOT touch either - reported for manual review) and delete the STRAY (blank map,
else the higher match_id = created later). Deleting the stray removes its TournamentTeamMatchStats
via cascade; the survivor still holds the real result, so no data is lost.

RUN (prod, same pattern as dedupe_tournament_teams / dedupe_team_match_stats):
    python manage.py dedupe_misfiled_matches            # dry-run, report only
    python manage.py dedupe_misfiled_matches --apply     # delete the verified strays

After --apply, re-open the affected leaderboard: the polluted day now shows only the teams that
played that day. No migration needed (pure data). Read by: ops.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from afc_tournament_and_scrims.models import Match, TournamentTeamMatchStats, Stages


class Command(BaseCommand):
    help = ("Delete misfiled duplicate matches (exact result-row copy of another match in the same "
            "stage but a different group) that pollute a round-robin day's leaderboard.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the deletions. Without this flag the command only reports (dry-run).",
        )

    @staticmethod
    def _rowset(match_id):
        # A match's identity = the multiset of its result rows. frozenset is safe here because a match
        # never has two rows for the same team (uniq_team_stats_per_match), so rows are distinct.
        return frozenset(
            (s.tournament_team_id, s.placement, s.kills)
            for s in TournamentTeamMatchStats.objects.filter(match_id=match_id)
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        strays = []          # matches we will delete
        needs_review = []    # duplicates where both copies have a real map (ambiguous - never auto-delete)

        for st in Stages.objects.all():
            matches = list(Match.objects.filter(group__stage=st))
            if len(matches) < 2:
                continue
            by_rows = defaultdict(list)
            for m in matches:
                rs = self._rowset(m.match_id)
                if len(rs) >= 4:          # ignore empty / trivially-small matches
                    by_rows[rs].append(m)
            for rs, dups in by_rows.items():
                if len(dups) < 2:
                    continue
                if len({d.group_id for d in dups}) < 2:
                    continue              # same group => a real re-upload issue, not a misfile; skip
                # Survivor = the copy with a real map. Blank-map copies are the strays.
                with_map = [d for d in dups if (d.match_map or "").strip()]
                blank = [d for d in dups if not (d.match_map or "").strip()]
                if with_map and blank:
                    survivor = max(with_map, key=lambda d: d.match_id)  # a real map wins; any works
                    for s in blank:
                        strays.append((st, survivor, s))
                else:
                    # Both blank OR both have maps -> ambiguous. Report, do not delete.
                    needs_review.append((st, dups))

        if not strays and not needs_review:
            self.stdout.write(self.style.SUCCESS("No misfiled duplicate matches found. Nothing to do."))
            return

        if strays:
            self.stdout.write(f"Found {len(strays)} misfiled duplicate match(es) to delete:")
            for st, survivor, stray in strays:
                nrows = len(self._rowset(stray.match_id))
                self.stdout.write(
                    f"  - stage {st.stage_id} (event {st.event_id}) {st.stage_name!r}: "
                    f"DELETE match {stray.match_id} (group {stray.group_id}, map "
                    f"{stray.match_map!r}, {nrows} rows) - exact copy of match {survivor.match_id} "
                    f"(group {survivor.group_id}, map {survivor.match_map!r})"
                )
        if needs_review:
            self.stdout.write(self.style.WARNING(
                f"\n{len(needs_review)} ambiguous duplicate set(s) (both copies have a map, or both "
                f"blank) - NOT auto-deleted, review manually:"))
            for st, dups in needs_review:
                self.stdout.write(
                    f"  - stage {st.stage_id} (event {st.event_id}): matches "
                    f"{[(d.match_id, d.group_id, d.match_map) for d in dups]}")

        if not apply:
            self.stdout.write(self.style.WARNING("\nDRY-RUN. Re-run with --apply to delete the strays above."))
            return

        deleted = 0
        with transaction.atomic():
            for _st, _survivor, stray in strays:
                stray.delete()   # cascades to its TournamentTeamMatchStats / player stats / kill flags
                deleted += 1
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Deleted {deleted} misfiled duplicate match(es). Re-open the affected "
            f"leaderboard - each day now shows only the teams that played it."))
