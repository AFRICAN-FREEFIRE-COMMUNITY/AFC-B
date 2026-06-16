"""
Audit (and optionally clean) duplicate team match-stat rows left by failed saves.

WHY THIS EXISTS (owner 2026-06-16): earlier result-save bugs (squad save dropping
players, MySQL bulk_create losing PKs, an atomic-block returning 400 AFTER a
delete) could write a SECOND TournamentTeamMatchStats row for the same
(team, match) on a retry. There is no DB unique constraint on (match,
tournament_team), so those duplicates persist and inflate a team's match count +
kills (e.g. a team showing 7 maps when only 5 were played), which then skews the
event leaderboard AND the recomputed rankings.

A duplicate = more than one TournamentTeamMatchStats for the SAME
(tournament_team_id, match_id). The keeper is the row with the MOST child
TournamentPlayerMatchStats (then the lowest id, deterministic); the extras are
deletable. Deleting a TTMS cascades to its TournamentPlayerMatchStats children
(FK on_delete=CASCADE), so player stats stay consistent.

SAFE BY DEFAULT: dry-run unless --apply is passed. Run it, READ the report, then
re-run with --apply. After cleaning, run `manage.py recalc_rankings` so the
ladders rebuild off the de-duplicated data.

USAGE (prod, inside the backend venv):
    python manage.py audit_orphan_match_stats                 # dry-run, whole DB
    python manage.py audit_orphan_match_stats --event 134     # dry-run, one event
    python manage.py audit_orphan_match_stats --event 134 --apply   # delete the extras

Read by: ops. Touches afc_tournament_and_scrims.TournamentTeamMatchStats (+ its
TournamentPlayerMatchStats children via cascade). Pairs with recalc_rankings
(afc_rankings) which rebuilds the ladders afterwards.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from afc_tournament_and_scrims.models import TournamentTeamMatchStats, Match


class Command(BaseCommand):
    help = "Find (and with --apply, delete) duplicate (team, match) match-stat rows from failed saves."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, help="Limit to one event_id.")
        parser.add_argument("--apply", action="store_true",
                            help="Actually delete the extra duplicate rows (default is dry-run).")

    def handle(self, *args, **opts):
        apply = opts.get("apply")
        event_id = opts.get("event")

        # Base queryset. A TTMS belongs to an event via its tournament_team.event (the most reliable
        # link; the match→group→stage→event path is not always populated for these rows).
        qs = TournamentTeamMatchStats.objects.all()
        if event_id:
            qs = qs.filter(tournament_team__event_id=event_id)

        # ── Find duplicate (tournament_team_id, match_id) groups ──────────────
        dup_keys = (
            qs.values("tournament_team_id", "match_id")
            .annotate(n=Count("pk"))
            .filter(n__gt=1)
        )
        dup_keys = list(dup_keys)

        if not dup_keys:
            self.stdout.write(self.style.SUCCESS(
                f"No duplicate (team, match) match-stat rows found"
                f"{f' for event {event_id}' if event_id else ''}. Nothing to clean."))
            self._report_count_mismatches(event_id)
            return

        total_extra = 0
        per_team = defaultdict(int)
        to_delete_ids = []
        for key in dup_keys:
            rows = list(
                TournamentTeamMatchStats.objects
                .filter(tournament_team_id=key["tournament_team_id"], match_id=key["match_id"])
                .annotate(nchild=Count("player_stats"))
                .order_by("-nchild", "pk")  # keeper first: most player rows, then lowest pk
                .select_related("tournament_team__team")
            )
            keeper, extras = rows[0], rows[1:]
            to_delete_ids += [r.pk for r in extras]
            total_extra += len(extras)
            tname = (keeper.tournament_team.team.team_name
                     if keeper.tournament_team and keeper.tournament_team.team_id else "ghost/none")
            per_team[tname] += len(extras)

        self.stdout.write(self.style.WARNING(
            f"Found {len(dup_keys)} duplicated (team, match) keys -> {total_extra} extra rows to remove:"))
        for tname, n in sorted(per_team.items(), key=lambda x: -x[1]):
            self.stdout.write(f"  {n:4d} extra rows  ·  {tname}")

        if not apply:
            self.stdout.write(self.style.NOTICE(
                "\nDRY-RUN. Re-run with --apply to delete the extra rows "
                "(child player stats cascade), then run `manage.py recalc_rankings`."))
            self._report_count_mismatches(event_id)
            return

        with transaction.atomic():
            deleted = TournamentTeamMatchStats.objects.filter(pk__in=to_delete_ids).delete()
        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {total_extra} duplicate team-stat rows (+ cascaded children): {deleted}"))
        self.stdout.write(self.style.SUCCESS(
            "Now run `python manage.py recalc_rankings` to rebuild the ladders."))
        self._report_count_mismatches(event_id)

    def _report_count_mismatches(self, event_id):
        """Report-only (never auto-deletes): per (event, team), distinct matches that have stats vs
        the event's actual match count. Surfaces 'team shows N maps but only M were played' cases
        that are NOT plain duplicates (e.g. stats pointing at a since-removed match) for MANUAL
        review — deleting those is judgement-dependent, so we only flag them."""
        from afc_tournament_and_scrims.models import Event
        events = Event.objects.all()
        if event_id:
            events = events.filter(event_id=event_id)
        flagged = []
        for ev in events:
            match_ids = set(Match.objects.filter(group__stage__event=ev).values_list("match_id", flat=True))
            if not match_ids:
                continue
            n_event_matches = len(match_ids)
            rows = (TournamentTeamMatchStats.objects
                    .filter(tournament_team__event=ev)
                    .values("tournament_team__team__team_name")
                    .annotate(distinct_matches=Count("match_id", distinct=True)))
            for r in rows:
                if r["distinct_matches"] > n_event_matches:
                    flagged.append((ev.event_id, ev.event_name,
                                    r["tournament_team__team__team_name"],
                                    r["distinct_matches"], n_event_matches))
        if flagged:
            self.stdout.write(self.style.WARNING(
                "\nReview (NOT auto-cleaned) — teams with stats on more matches than the event has:"))
            for eid, ename, tname, dm, em in flagged:
                self.stdout.write(f"  event {eid} {ename!r}: {tname} has {dm} matches w/ stats, "
                                  f"event has {em}. Likely stats on a removed match; check manually.")
