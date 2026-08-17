"""
fantasy_recompute - rebuild fantasy scores from the current match results.

WHY IT EXISTS
    AFC corrects results after the fact: a kill count is fixed, a team is disqualified, a match is
    re-uploaded. Fantasy scores are derived from those results and must follow them, so this is the
    command that makes every table agree with the results pages again.

    Safe to run at any time and any number of times. Scoring REPLACES rather than accumulates
    (afc_fantasy.scoring.recompute_league), so a correction that LOWERS a score works exactly as
    well as one that raises it.

RUN
    python manage.py fantasy_recompute                    # every league that is scoring
    python manage.py fantasy_recompute --league <slug>    # just one
    python manage.py fantasy_recompute --all              # including drafts and settled ones

    By default it skips drafts (nobody has entered) and settled leagues (the result is published,
    and quietly moving a published table is worse than leaving it). --all overrides that for the
    case where a settled league was settled on a result that has since been corrected, which is a
    decision a human should make deliberately.

HOW IT CONNECTS
    Calls afc_fantasy.scoring.recompute_league, the same function the admin "rebuild scores" button
    uses (afc_fantasy.admin_views.admin_recompute). One implementation, two doors.
"""
from django.core.management.base import BaseCommand

from afc_fantasy.models import FantasyLeague
from afc_fantasy.scoring import recompute_league

# Leagues whose scores are live. A draft has no entrants; a settled league has a published result.
SCORING_STATUSES = ("open", "locked")


class Command(BaseCommand):
    help = "Rebuild fantasy squad scores from the current match results."

    def add_arguments(self, parser):
        parser.add_argument("--league", default=None,
                            help="Slug of a single league. Default: every scoring league.")
        parser.add_argument("--all", action="store_true",
                            help="Include drafts and settled leagues. Moving a published table is "
                                 "a deliberate act, so it is not the default.")

    def handle(self, *args, **opts):
        qs = FantasyLeague.objects.select_related("event")
        if opts["league"]:
            qs = qs.filter(slug=opts["league"])
        elif not opts["all"]:
            qs = qs.filter(status__in=SCORING_STATUSES)

        leagues = list(qs)
        if not leagues:
            self.stdout.write("No leagues to recompute.")
            return

        total = 0
        for league in leagues:
            rows = recompute_league(league)
            total += rows
            self.stdout.write(f"  {league.slug}: {rows} score row(s) from "
                              f"{league.squads.count()} squad(s)")
        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt {total} score row(s) across {len(leagues)} league(s)."))
