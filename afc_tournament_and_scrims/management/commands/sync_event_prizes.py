# ─────────────────────────────────────────────────────────────────────────────
# sync_event_prizes — attribute prize pools for finished events (owner 2026-07-14).
#
# WHY: the event -> prize auto-sync (prize_sync.sync_completed_events) only runs when an admin opens
# the rankings "Prize Money" page. So a finished event's prize pool (e.g. DYNASTY CUP GRAND FINALS SSA)
# shows "-" on the team/player earnings pages until someone happens to open that admin page. This
# command runs the sync on demand so the whole rollout is one predictable step (deploy -> run this),
# not "go open the right page and hope".
#
# WHAT (with --apply):
#   1. sync_completed_events(): for every EFFECTIVELY-completed event with a prize_distribution and no
#      auto payouts yet, create the team EventPrizePayout rows AND their per-player PlayerWinning shares
#      (the sync distributes now). This is what attributes GRAND FINALS to its top finishers.
#   2. Distribute shares for any pre-existing team payouts that still have none (older events synced
#      before the distribution fix, e.g. DECA). Same job as backfill_player_winnings, folded in so one
#      command fully attributes everything.
# Idempotent + safe: only creates what is missing, never edits manual rows, preserves awarded dates.
#
# RUN (prod, once, after the backend deploy):  python manage.py sync_event_prizes --apply
# CONNECTS TO: team page total_earnings + tournament_performance.prize_earned (get-team-details) and
# player profile tournament_winnings (get_public_player_stats).
# ─────────────────────────────────────────────────────────────────────────────
import datetime

from django.core.management.base import BaseCommand
from django.db.models import Q

from afc_tournament_and_scrims.models import Event, EventPrizePayout, PlayerWinning


class Command(BaseCommand):
    help = "Attribute finished-event prize pools: create missing team payouts + per-player shares."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write payouts + shares. Without this the command only reports (dry run).")
        parser.add_argument("--force", action="store_true",
                            help="Re-sync EVERY finished prized event, even ones whose auto payouts already "
                                 "match. Use after a scoring/data change if you want to rebuild all rows.")

    def handle(self, *args, **opts):
        from afc_tournament_and_scrims.views import effective_event_status
        from afc_tournament_and_scrims.prize_sync import event_prize_is_stale, sync_event_prize_payouts

        force = opts["force"]

        # ── Preview: finished events whose auto payouts are MISSING or STALE ──
        # event_prize_is_stale is True when the stored auto payouts differ from what the CURRENT rule
        # (last-stage-played, summed pools) produces — so this catches both never-attributed events AND
        # ones left with an out-of-date amount/team from the earlier cumulative rule (owner 2026-07-14).
        today = datetime.date.today()
        candidates = (Event.objects
                      .exclude(prize_distribution={})
                      .filter(Q(event_status="completed") | Q(end_date__lte=today)))
        to_sync = []
        for ev in candidates:
            if effective_event_status(ev) != "completed":
                continue
            if force or event_prize_is_stale(ev):
                to_sync.append(ev)
        self.stdout.write(f"Finished events to (re)attribute prizes for: {len(to_sync)}"
                          f"{' [FORCE: all]' if force else ' [missing or stale]'}")
        for ev in to_sync:
            self.stdout.write(f"  ev{ev.event_id} {ev.event_name!r} raw={ev.event_status} "
                              f"end={ev.end_date} dist={ev.prize_distribution}")

        # Pre-existing team payouts (any event) with no per-player shares yet.
        missing_shares = (EventPrizePayout.objects
                          .filter(tournament_team__isnull=False, player_winnings__isnull=True)
                          .distinct())
        self.stdout.write(f"Existing team payouts missing per-player shares: {missing_shares.count()}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing written. Re-run with --apply."))
            return

        # ── 1. (Re)sync each missing/stale event: delete + recreate its auto payouts + shares. ──
        created = 0
        for ev in to_sync:
            created += sync_event_prize_payouts(ev)

        # ── 2. Backfill shares for any pre-existing payouts that still lack them. ──
        from afc_rankings.admin_prize import _distribute_payout
        shares = 0
        for p in EventPrizePayout.objects.filter(
                tournament_team__isnull=False, player_winnings__isnull=True).distinct():
            _distribute_payout(p)
            shares += PlayerWinning.objects.filter(payout=p).count()

        self.stdout.write(self.style.SUCCESS(
            f"Done. (Re)synced {len(to_sync)} event(s), wrote {created} team payout row(s); "
            f"wrote {shares} PlayerWinning share(s) for previously-undistributed payouts."))
