"""
backfill_capture_stats - give maps that were already uploaded the stats AFC Capture 1.5.4 reads, without touching their
results (owner, 9 Nov 2026, inbox #512: "resend everything all past maps").

WHY: until Capture 1.5.4 nobody sent a player's damage (the site stored 0 for every map), knocks carried no gun and about
4 kills in 10 carried none. 1.5.4 reads the game's replay file: the game's own damage figure for every player and the gun
of every kill and knock. A map's replay and debugger log are still on the observer PC, so the tool that runs 1.5.4 over
them offline (afc-capture/dev/backfill_past_maps.py) writes one JSON file, and this command applies it.

WHAT IT CHANGES, per map, and nothing else:
  - the player rows' rich stats, damage included (capture_rich_stats.apply_capture_rich_stats, matched by UID);
  - the map's kill feed, replaced by the payload's (kill_feed.store_events: idempotent, teams from the roster);
  - each team row's damage = its players' damage + the damage of the flagged players whose kills count
    (MatchKillFlag.effective_count, the same rule as the upload), and its total_points recomputed from the stored
    placement, kills, assists, bonus and penalty with the match's own scoring (result_writes.scoring_context ->
    scoring.compute_team_points, the formula every writer uses). Kills, placement and flags are never written.
  It does NOT call views._recompute_team_kills_for_event: that rebuilds KILLS from player rows, which is wrong for a map
  entered by hand (its docstring says so).

WHICH MATCH: an entry names `site_match_id`, or the game's `ff_match_id`, found through the result file the map was
uploaded with (MatchResultLog.file_name "MatchResult_<ff id>_...log"). A game id that maps to no match, or to more than
one, is reported and skipped, never guessed.

PAYLOAD: [{"ff_match_id": "2118...", "site_match_id": 5024 (optional), "rich_stats": {"players": {uid: {...}}},
           "kill_events": [...] (optional)}, ...]   (rich_stats and kill_events in the shapes the upload takes)

Usage:
    python manage.py backfill_capture_stats --file payload.json            # dry run: what would change
    python manage.py backfill_capture_stats --file payload.json --apply    # write, one transaction per map
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from afc_tournament_and_scrims import capture_rich_stats, kill_feed
from afc_tournament_and_scrims.models import Match, MatchResultLog


def find_match(entry):
    """(match, "") or (None, reason): by site_match_id, else by the game's match id in an uploaded result file's name."""
    if entry.get("site_match_id"):
        match = Match.objects.filter(match_id=entry["site_match_id"]).first()
        return (match, "") if match else (None, "no_such_site_match")
    ff = str(entry.get("ff_match_id") or "").strip()
    if not ff.isdigit():
        return None, "no_match_id"
    ids = set(MatchResultLog.objects.filter(file_name__startswith="MatchResult_%s_" % ff)
              .values_list("match_id", flat=True))
    if not ids:
        return None, "never_uploaded"
    if len(ids) > 1:
        return None, "uploaded_to_%d_matches" % len(ids)
    return Match.objects.get(match_id=ids.pop()), ""


# team_damage_and_points lives in capture_apply now (11 Nov 2026), shared with the Game files box, which applies
# an uploaded map exactly the way this command backfills one.
from afc_tournament_and_scrims.capture_apply import team_damage_and_points  # noqa: E402


class Command(BaseCommand):
    help = "Give already uploaded maps AFC Capture 1.5.4's stats (damage, every gun) without touching their results."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="The JSON list afc-capture/dev/backfill_past_maps.py wrote.")
        parser.add_argument("--apply", action="store_true", help="Write. Without it nothing is saved.")

    def handle(self, *args, **opts):
        try:
            with open(opts["file"], encoding="utf-8") as fh:
                entries = json.load(fh)
        except (OSError, ValueError) as exc:
            raise CommandError("Cannot read %s: %s" % (opts["file"], exc))
        if not isinstance(entries, list):
            raise CommandError("The file must hold a JSON list.")
        applied = skipped = 0
        for entry in entries:
            label = entry.get("site_match_id") or entry.get("ff_match_id")
            match, why = find_match(entry)
            rich, reason = capture_rich_stats.parse_rich_stats(entry.get("rich_stats"))
            if match is None or rich is None:
                skipped += 1
                self.stdout.write("SKIP %s: %s" % (label, why or "rich_stats_" + reason))
                continue
            kills = entry.get("kill_events")
            kills = kill_feed.clean_events(kills) if kills is not None else None
            damage = capture_rich_stats.damage_by_uid(rich)
            with transaction.atomic():
                filled = capture_rich_stats.apply_capture_rich_stats(match, rich)
                stored = kill_feed.store_events(match, match.group.stage.event, kills, "capture") if kills else 0
                rows = team_damage_and_points(match, damage)
                summary = ", ".join("%s %d dmg %d pts" % (ts.tournament_team_id, ts.damage, ts.total_points)
                                    for ts in rows)
                if not opts["apply"]:
                    transaction.set_rollback(True)
            applied += 1
            self.stdout.write("%s %s -> site match %s: %d player rows, %d kill events; teams: %s" % (
                "APPLIED" if opts["apply"] else "WOULD APPLY", label, match.match_id, filled, stored, summary))
        self.stdout.write("%s %d, skipped %d" % ("applied" if opts["apply"] else "dry run: would apply", applied, skipped))
