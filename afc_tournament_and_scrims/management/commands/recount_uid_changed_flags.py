"""
recount_uid_changed_flags - repair existing standings after the 2026-07-06 flagged-kills fix.

WHY: before the fix, a rostered player whose Free Fire UID changed between signup and a match was
flagged `name_matched_uid_changed` with count_kills=False (forced PENDING), so their kills were
silently withheld from the team total EVEN WITH "count flagged kills" ON. The fix makes new uploads
create those flags with count_kills=None (follow the event's count_flagged_kills toggle). This command
repairs flags ALREADY stored as False by flipping them back to None, then re-runs the canonical
team-total recompute so the saved standings reflect the returning players' kills.

Scope: only `name_matched_uid_changed` flags that are currently count_kills=False are touched.
`name_matched_other_team` / `belongs_to_other_team` (cross-team ringers) are left PENDING on purpose.
A flag an admin explicitly APPROVED (count_kills=True) is already counting and is left alone.

Consumes: MatchKillFlag (afc_tournament_and_scrims.models), views._recompute_team_kills_for_event
(the same recompute the "count flagged players' kills" toggle calls). Reported case: DYNASTY CUP
event 172 - PARADOX GAMING 295 -> 307, FROZEN EMPIRE +4.

Usage:
    python manage.py recount_uid_changed_flags                 # dry-run, ALL events
    python manage.py recount_uid_changed_flags --event-id 172  # dry-run, one event
    python manage.py recount_uid_changed_flags --event-id 172 --apply
    python manage.py recount_uid_changed_flags --apply         # apply across every event
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from afc_tournament_and_scrims.models import MatchKillFlag, Event
from afc_tournament_and_scrims.views import _recompute_team_kills_for_event


class Command(BaseCommand):
    help = "Flip stale name_matched_uid_changed flags (count_kills=False -> None) and recompute totals."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", type=int, default=None,
                            help="Limit to a single event (default: all events).")
        parser.add_argument("--apply", action="store_true",
                            help="Persist the changes. Without it this is a dry-run.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        event_id = opts["event_id"]

        # Stale rows: same-team UID-change flags still forced to the old PENDING value.
        flags = MatchKillFlag.objects.filter(
            reason="name_matched_uid_changed", count_kills=False)
        if event_id:
            flags = flags.filter(tournament_team__event_id=event_id)

        # Which events need a recompute (distinct owning events of the affected flags).
        event_ids = sorted(set(
            flags.values_list("tournament_team__event_id", flat=True)))
        self.stdout.write(
            f"{flags.count()} stale name_matched_uid_changed flag(s) across {len(event_ids)} event(s).")
        for f in flags.select_related("tournament_team__team", "match"):
            nm = f.name.encode("ascii", "replace").decode()
            self.stdout.write(
                f"  event {f.tournament_team.event_id} match {f.match_id} "
                f"{f.tournament_team.team.team_name.strip()[:18]:18} {nm[:16]:16} +{f.kills} kills")

        if not apply:
            self.stdout.write(self.style.WARNING("DRY-RUN. Re-run with --apply to persist + recompute."))
            return

        with transaction.atomic():
            updated = flags.update(count_kills=None)   # follow the event toggle from now on
            for ev in Event.objects.filter(event_id__in=event_ids):
                _recompute_team_kills_for_event(ev)     # canonical re-score of the affected teams
        self.stdout.write(self.style.SUCCESS(
            f"APPLIED: {updated} flag(s) set to follow the toggle; recomputed {len(event_ids)} event(s)."))
