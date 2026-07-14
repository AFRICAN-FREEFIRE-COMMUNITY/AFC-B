# ─────────────────────────────────────────────────────────────────────────────
# backfill_player_winnings — one-off fix (owner 2026-07-14).
#
# WHY: prize_sync.sync_event_prize_payouts creates the TEAM payout (EventPrizePayout) but, until the
# 2026-07-14 fix, never wrote the per-player PlayerWinning shares that player profiles read. So every
# player profile showed 0 tournament winnings for prize-pool events even though the team page showed the
# prize. The sync distributes going forward, but ALREADY-SYNCED events (e.g. DECA CUP SEASON 5) keep
# their existing team payouts and are SKIPPED by the on-read sweep, so their player shares never appear.
#
# WHAT: for every TEAM EventPrizePayout that has no PlayerWinning rows yet, split it across the team's
# active roster via the same admin_prize._distribute_payout used by manual entry + the sync. Idempotent
# (keyed on payout, delete-then-recreate) and it does NOT re-create the payout, so awarded dates
# (created_at) are preserved. Payouts that already distributed are left untouched.
#
# RUN (prod, once, after deploy):  python manage.py backfill_player_winnings --apply
# CONNECTS TO: afc_player.views.get_public_player_stats -> tournament_winnings (player-profile card),
# and the team page's total_earnings (unaffected — that already worked off EventPrizePayout).
# ─────────────────────────────────────────────────────────────────────────────
from django.core.management.base import BaseCommand

from afc_tournament_and_scrims.models import EventPrizePayout, PlayerWinning


class Command(BaseCommand):
    help = "Write per-player PlayerWinning shares for team prize payouts that have none (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the shares. Without this the command only reports (dry run).")

    def handle(self, *args, **opts):
        # Lazy import: admin_prize imports prize_sync, so keep the dependency chain lazy (same as the sync).
        from afc_rankings.admin_prize import _distribute_payout

        # Team payouts (a TournamentTeam is set) that have no PlayerWinning shares yet. The reverse-FK
        # __isnull=True is the "left-join, no child row" filter; distinct() guards against join fan-out.
        missing = (EventPrizePayout.objects
                   .filter(tournament_team__isnull=False, player_winnings__isnull=True)
                   .select_related("tournament_team", "event")
                   .distinct())
        n = missing.count()
        self.stdout.write(f"Team payouts missing per-player shares: {n}")
        for p in missing.order_by("event_id", "id"):
            name = p.event.event_name if p.event_id else "?"
            self.stdout.write(f"  ev{p.event_id} {name!r} payout#{p.id} "
                              f"tt={p.tournament_team_id} amount={p.amount}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing written. Re-run with --apply."))
            return

        written = 0
        for p in missing:
            # Splits the payout across the team's active roster and writes one PlayerWinning per member.
            # Idempotent (keyed on payout) and failure-safe; does not modify the payout row itself.
            _distribute_payout(p)
            written += PlayerWinning.objects.filter(payout=p).count()
        self.stdout.write(self.style.SUCCESS(
            f"Distributed shares for {n} payout(s); {written} PlayerWinning row(s) written."))
