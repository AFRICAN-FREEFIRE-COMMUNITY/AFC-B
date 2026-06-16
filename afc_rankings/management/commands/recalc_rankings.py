"""
Recompute the rankings score tables (then re-rank) for a month and/or season.

WHY THIS EXISTS (owner 2026-06-16): the public ranking ladders read straight off
TeamMonthlyScore / PlayerMonthlyScore / *QuarterlyScore, which are written by the
recalc layer when a result is saved. Earlier result-save bugs (squad-save drop,
MySQL bulk_create PK loss, atomic-rollback-on-reject) wrote PARTIAL stats, so the
stored scores went stale (e.g. flat scores, kills that don't match the event).
After fixing the save path (and after `audit_orphan_match_stats --apply` clears any
leftover duplicate match-stat rows), run this to rebuild the ladders from the
now-correct match data. It calls the SAME recalc functions the save path uses, so
there is one source of truth.

USAGE (prod, inside the backend venv):
    python manage.py recalc_rankings --month 2026-06          # one month
    python manage.py recalc_rankings --season 4               # one season (by id)
    python manage.py recalc_rankings --month 2026-06 --season 4
    python manage.py recalc_rankings --all-months             # every month that has score rows
    python manage.py recalc_rankings                          # current month + active season

It NEVER publishes anything — publish stays a separate, explicit admin action
(afc_rankings.admin_publish.publish_state). Recompute first, eyeball the admin
draft preview, THEN publish. Read by: ops (manual prod rebuild). Calls:
recalc.recalc_month / recalc_season + rerank_* (afc_rankings/recalc.py).
"""
import datetime

from django.core.management.base import BaseCommand, CommandError

from afc_rankings import recalc
from afc_rankings.models import Season, TeamMonthlyScore


def _parse_month(raw: str) -> datetime.date:
    try:
        y, m = raw.split("-")
        return datetime.date(int(y), int(m), 1)
    except (ValueError, AttributeError):
        raise CommandError(f"--month must be YYYY-MM, got {raw!r}")


class Command(BaseCommand):
    help = "Recompute + re-rank the ranking score tables for a month and/or season."

    def add_arguments(self, parser):
        parser.add_argument("--month", help="YYYY-MM to recompute the monthly ladders for.")
        parser.add_argument("--season", type=int, help="Season id to recompute the quarterly ladders for.")
        parser.add_argument("--all-months", action="store_true",
                            help="Recompute every month that currently has TeamMonthlyScore rows.")

    def handle(self, *args, **opts):
        did_anything = False

        # ── Monthly ──────────────────────────────────────────────────────────
        months: list[datetime.date] = []
        if opts.get("all_months"):
            months = sorted(set(TeamMonthlyScore.objects.values_list("month", flat=True)))
            if not months:
                self.stdout.write("No TeamMonthlyScore rows exist yet; nothing to recompute monthly.")
        elif opts.get("month"):
            months = [_parse_month(opts["month"])]
        elif not opts.get("season"):
            # No flags at all → default to the current month (+ active season below).
            months = [recalc.current_month()]

        for month in months:
            self.stdout.write(f"Recomputing monthly ladders for {month.isoformat()} ...")
            recalc.recalc_month(month)        # rewrites every active team + player score for the month
            recalc.rerank_team_month(month)   # then assign ranks across the rebuilt rows
            recalc.rerank_player_month(month)
            did_anything = True
            self.stdout.write(self.style.SUCCESS(f"  done {month.isoformat()}"))

        # ── Quarterly (season) ───────────────────────────────────────────────
        season = None
        if opts.get("season"):
            season = Season.objects.filter(pk=opts["season"]).first()
            if not season:
                raise CommandError(f"Season id {opts['season']} not found.")
        elif not opts.get("month") and not opts.get("all_months"):
            # Default run (no flags): also refresh the active season.
            season = recalc.current_season()

        if season:
            self.stdout.write(f"Recomputing quarterly ladders for season {season.season_id} "
                              f"({season.name}) ...")
            recalc.recalc_season(season)
            recalc.rerank_team_quarter(season)
            recalc.rerank_player_quarter(season)
            did_anything = True
            self.stdout.write(self.style.SUCCESS(f"  done season {season.season_id}"))

        if not did_anything:
            self.stdout.write(self.style.WARNING("Nothing recomputed (no month/season resolved)."))
        else:
            self.stdout.write(self.style.SUCCESS(
                "Recompute complete. Review the admin draft preview, then publish "
                "(rankings/seasons/<id>/publish/) when ready. Recompute does NOT publish."))
