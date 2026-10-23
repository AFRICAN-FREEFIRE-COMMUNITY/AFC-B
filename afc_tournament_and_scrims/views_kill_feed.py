"""GET events/kill-stats/?event=<slug>[&stage=<stage_id>][&export=csv]: the event's kill stats (inbox #326,
#350, #376).

PURPOSE: the NFL Season 2 sniper award ("sniper kills recorded": bolt-action kills whose weapon the game
reported) and team head to head (times met, wins each, draws, by players left after each fight). The rules
live in kill_feed.py; this view only gathers the event's stored MatchKillEvent rows and asks it.

REQUEST: ?event=<event slug>; optional ?stage=<stage_id> (one stage, e.g. the group stage for the first
sniper graphic); optional ?export=csv for the GFX team (not ?format=: DRF reserves it and
answers 404 for a format it has no renderer for).
RESPONSE 200 (json): {event, stage, switches: {tab, overlay, export}, coverage: {maps, kills,
kills_with_weapon}, sniper: [{uid, username, team_name, sniper_kills}], head_to_head: [{team_a: {id, name},
team_b: {id, name}, met, wins_a, wins_b, draws}], sniper_weapons: [{weapon_id, name}]}.
RESPONSE 200 (csv): two blocks, "sniper kills recorded" then "head to head".
AUTH: open to anybody once the event switched the matching surface on (kill_feed_tab / kill_feed_overlay
for json, kill_feed_export for csv); before that, only the event's admins and organizer (to preview).
Refusals carry a code: event_not_found (404), kill_stats_off (403).
CONSUMED BY: nothing yet. Next: the event page tab (mockups/kill-feed-tab/, waiting for approval), the
AFC LIVE overlays, and the GFX export link.
"""
import csv
import io

from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from . import kill_feed
from .models import Event, GameWeapon, MatchKillEvent, TournamentPlayerMatchStats, TournamentTeam

PLAYERS_PER_MAP = {"solo": 1, "duo": 2, "squad": 4}


def _viewer(request):
    auth = request.headers.get("Authorization") or ""
    return validate_token(auth.split(" ", 1)[1]) if auth.startswith("Bearer ") and " " in auth else None


def _can_preview(user, event):
    from .views import _is_event_admin

    return bool(user) and (_is_event_admin(user) or org_can_event(user, "can_edit_events", event))


@api_view(["GET"])
def kill_stats(request):
    slug = (request.GET.get("event") or "").strip()
    event = Event.objects.filter(slug=slug).first() if slug else None
    if event is None:
        return Response({"message": "Event not found.", "code": "event_not_found"}, status=404)

    as_csv = (request.GET.get("export") or "").lower() == "csv"
    shown = event.kill_feed_export if as_csv else (event.kill_feed_tab or event.kill_feed_overlay)
    if not shown and not _can_preview(_viewer(request), event):
        return Response({"message": "This event does not show kill stats.", "code": "kill_stats_off"},
                        status=403)

    stage_id = request.GET.get("stage")
    rows = MatchKillEvent.objects.filter(match__group__stage__event=event)
    if stage_id and str(stage_id).isdigit():
        rows = rows.filter(match__group__stage_id=int(stage_id))
    rows = list(rows.order_by("match_id", "seq").values(
        "match_id", "t", "kind", "killer_uid", "victim_uid", "killer_team_id", "victim_team_id", "weapon_id"))

    by_match = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append({
            "t": r["t"], "kind": r["kind"], "killer_uid": r["killer_uid"], "victim_uid": r["victim_uid"],
            "killer_team": r["killer_team_id"], "victim_team": r["victim_team_id"], "weapon": r["weapon_id"]})

    # players who started each map, per team: the map's played rows, else the event's format
    default_size = PLAYERS_PER_MAP.get((event.participant_type or "").lower(), 4)
    sizes = {}
    for match_id, team_id in (TournamentPlayerMatchStats.objects
                              .filter(team_stats__match_id__in=list(by_match), played=True)
                              .values_list("team_stats__match_id", "team_stats__tournament_team_id")):
        sizes.setdefault(match_id, {}).setdefault(team_id, 0)
        sizes[match_id][team_id] += 1
    fights = []
    for match_id, events in by_match.items():
        teams = {e["killer_team"] for e in events} | {e["victim_team"] for e in events}
        size = {t: sizes.get(match_id, {}).get(t, default_size) for t in teams if t is not None}
        fights.append(kill_feed.fights_of_match(events, size))
    h2h = kill_feed.head_to_head(fights)

    sniper_ids = set(GameWeapon.objects.filter(is_sniper=True).values_list("weapon_id", flat=True))
    counts = kill_feed.sniper_counts([e for evs in by_match.values() for e in evs], sniper_ids)
    from afc_auth.models import User

    users = {str(u.uid).lstrip("0"): u for u in User.objects.filter(uid__in=list(counts))}
    team_of = {e["killer_uid"]: e["killer_team"] for evs in by_match.values() for e in evs}
    team_ids = set(team_of.values()) | {t for pair in h2h for t in pair}
    names = {tt.tournament_team_id: getattr(tt.team, "team_name", "") for tt in
             TournamentTeam.objects.filter(tournament_team_id__in=[t for t in team_ids if t]).select_related("team")}

    sniper = sorted(({"uid": uid, "username": getattr(users.get(uid.lstrip("0")), "username", ""),
                      "team_name": names.get(team_of.get(uid), ""), "sniper_kills": n}
                     for uid, n in counts.items()), key=lambda r: (-r["sniper_kills"], r["username"]))
    pairs = sorted(({"team_a": {"id": a, "name": names.get(a, "")}, "team_b": {"id": b, "name": names.get(b, "")},
                     "met": row["met"], "wins_a": row["wins"][a], "wins_b": row["wins"][b], "draws": row["draws"]}
                    for (a, b), row in h2h.items()), key=lambda r: (-r["met"], r["team_a"]["name"]))

    if as_csv:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["sniper kills recorded"])
        w.writerow(["player", "team", "sniper_kills"])
        for r in sniper:
            w.writerow([r["username"] or r["uid"], r["team_name"], r["sniper_kills"]])
        w.writerow([])
        w.writerow(["head to head"])
        w.writerow(["team_a", "team_b", "met", "wins_a", "wins_b", "draws"])
        for r in pairs:
            w.writerow([r["team_a"]["name"], r["team_b"]["name"], r["met"], r["wins_a"], r["wins_b"], r["draws"]])
        resp = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{event.slug}-kill-stats.csv"'
        return resp

    kills = [e for evs in by_match.values() for e in evs if e["kind"] == "kill"]
    return Response({
        "event": event.slug,
        "stage": int(stage_id) if stage_id and str(stage_id).isdigit() else None,
        "switches": {"tab": event.kill_feed_tab, "overlay": event.kill_feed_overlay,
                     "export": event.kill_feed_export},
        "coverage": {"maps": len(by_match), "kills": len(kills),
                     "kills_with_weapon": sum(1 for e in kills if e["weapon"] is not None)},
        "sniper": sniper,
        "head_to_head": pairs,
        "sniper_weapons": list(GameWeapon.objects.filter(is_sniper=True).values("weapon_id", "name")),
    })
