"""manage.py sync_match_mvps: store each map's MVP (Match.mvp) by the event's saved MVP rule.

Owner 14 Nov 2026, AFC inbox #579. The decision is views_mvp's (the leaderboard's MVPs tab); the write is
mvp_sync.sync_event_match_mvps; the schedule is tasks.sync_match_mvps_recent / _all (afc/celery_config.py). This
command is the backfill and the by-hand run. A DRY RUN unless --apply is given.

    python manage.py sync_match_mvps                  # every event with results, report only
    python manage.py sync_match_mvps --event-id 373   # one event, report only
    python manage.py sync_match_mvps --apply          # write
"""
from django.core.management.base import BaseCommand

from afc_tournament_and_scrims.models import Event
from afc_tournament_and_scrims.mvp_sync import events_with_results, sync_event_match_mvps


class Command(BaseCommand):
    help = "Store each map's MVP (Match.mvp) by the event's saved MVP rule. Dry run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", type=int, default=None, help="Limit to one event.")
        parser.add_argument("--apply", action="store_true", help="Write the MVPs (default: report only).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        events = (Event.objects.filter(event_id=opts["event_id"]) if opts["event_id"]
                  else events_with_results().order_by("event_id"))
        n_events = n_maps = n_changed = 0
        for event in events:
            changes, judged = sync_event_match_mvps(event, apply=apply)
            n_events += 1
            n_maps += judged
            n_changed += len(changes)
            if changes:
                self.stdout.write(f"event {event.event_id} {event.event_name[:40]}: {judged} maps, {len(changes)} "
                                  f"{'set' if apply else 'would change'}")
        self.stdout.write(f"{'APPLIED' if apply else 'DRY RUN'}: events {n_events}, maps judged {n_maps}, "
                          f"{'changed' if apply else 'would change'} {n_changed}")
