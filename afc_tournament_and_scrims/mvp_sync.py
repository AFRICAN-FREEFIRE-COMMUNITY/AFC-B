# ── MATCH MVP SYNC (owner 14 Nov 2026, AFC inbox #579: "yes fix it also for future sessions") ─────────
"""Keeps Match.mvp equal to each map's MVP by the event's own saved MVP rule.

WHY. Match.mvp (one user per map) was written by no live code, so every reader of it read 0 or null:
  * afc_auth/views.py            the player profile's total MVPs (Match.objects.filter(mvp=user))
  * afc_player/aggregation.py    the player page: is_mvp per map, total and list of MVP maps
  * afc_player/views.py          the players list's MVP count per player
  * afc_rankings/aggregation.py  ranking points (players score on MVPs; afc_rankings.tasks.sweep_rankings, 04:00)
  * afc_partner_api/serialize.py the partner API's per-map `mvp` (Game Evo reads it)
The MVP of a map is DECIDED in one place only: views_mvp (the leaderboard's MVPs tab, admin and organizer pages,
Event.mvp_config = ordered criteria + scope "overall" / "winning_team"). This module runs that same decision
(_mvp_criteria + _walk_player_stats + _award_map_mvps) and stores the winner, so the stored value and the MVPs tab
can never disagree. It never decides an MVP by a rule of its own.

WHO CALLS IT.
  * views_mvp.event_mvp          on a rule save (POST events/<id>/mvp/): the whole event, at once.
  * tasks.sync_match_mvps_recent every 10 minutes: ongoing events and events that ended in the last RECENT_DAYS,
                                 so a result entered by ANY path (AFC Capture, manual entry, OCR, an edit, a
                                 backfill) is stored within 10 minutes. Result writes use queryset updates and
                                 bulk creates in about 40 places, which skip model signals, hence a sweep.
  * tasks.sync_match_mvps_all    03:45 every day, every event with results, before the 04:00 rankings sweep.
  * management command sync_match_mvps   a dry run by default; --apply writes (the backfill of past maps).

A map with no player stat lines keeps what it has: there is nothing to judge it by. A full tie on every criterion
goes to the earliest stat line (views_mvp orders the lines by pk), the same player on the MVPs tab and here.
"""
import datetime

from django.db.models import Q
from django.utils import timezone

from .models import Event, Match, TournamentPlayerMatchStats
from .views_mvp import _award_map_mvps, _mvp_criteria, _walk_player_stats

# The 10-minute sweep looks at events still running or finished this recently (a late upload, a corrected result).
RECENT_DAYS = 3


def event_map_mvps(event):
    """{match_id: user_id} for every map of `event` with player stat lines, by the event's saved MVP rule."""
    _criteria, rankable, scope, _avail = _mvp_criteria(event)
    by_match, players = _walk_player_stats(event, None, None)
    return {m["match_id"]: m["mvp_user_id"] for m in _award_map_mvps(by_match, players, rankable, scope)}


def sync_event_match_mvps(event, apply=True):
    """Store each map's MVP on Match.mvp for one event. Returns (changes, maps_judged), where changes is a list of
    (match_id, old_user_id, new_user_id) for the maps whose stored MVP differs. apply=False only reports."""
    want = event_map_mvps(event)
    if not want:
        return [], 0
    have = dict(Match.objects.filter(match_id__in=list(want)).values_list("match_id", "mvp_id"))
    changes = [(mid, have.get(mid), uid) for mid, uid in sorted(want.items()) if have.get(mid) != uid]
    if apply:
        for mid, _old, uid in changes:
            # A queryset update: Match.save() is not needed and must not touch match_date (auto_now_add).
            Match.objects.filter(match_id=mid).update(mvp_id=uid)
    return changes, len(want)


def events_with_results():
    """Every event that has at least one player stat line (the only events a map MVP can be decided for)."""
    ids = (TournamentPlayerMatchStats.objects
           .values_list("team_stats__match__group__stage__event_id", flat=True).distinct())
    return Event.objects.filter(event_id__in=list(ids))


def recent_events():
    """The 10-minute sweep's events: with results, and ongoing or ended in the last RECENT_DAYS days."""
    since = timezone.localdate() - datetime.timedelta(days=RECENT_DAYS)
    return events_with_results().filter(Q(event_status="ongoing") | Q(end_date__gte=since))


def sync_many(events, apply=True):
    """Sync each event in turn. Returns {"events": n, "maps": judged, "changed": n}. One event failing (bad data)
    never stops the others; it is counted under "failed" and logged by the caller."""
    import logging

    log = logging.getLogger(__name__)
    total = {"events": 0, "maps": 0, "changed": 0, "failed": 0}
    for event in events:
        try:
            changes, judged = sync_event_match_mvps(event, apply=apply)
        except Exception:  # noqa: BLE001 - a sweep keeps going; the event id goes to the log with the trace
            log.exception("match MVP sync failed for event %s", event.event_id)
            total["failed"] += 1
            continue
        total["events"] += 1
        total["maps"] += judged
        total["changed"] += len(changes)
    return total
