"""GET events/kill-stats/?event=<slug>[&stage=<stage_id>][&export=csv]: the event's kill stats (inbox #326,
#350, #376).

PURPOSE: the NFL Season 2 sniper award ("sniper kills recorded": bolt-action kills whose weapon the game
reported) and team head to head (times met, wins each, draws, by players left after each fight). The rules
live in kill_feed.py; this view only gathers the event's stored MatchKillEvent rows and asks it.

REQUEST: ?event=<event slug>; optional ?stage=<stage_id> (one stage, e.g. the group stage for the first
sniper graphic); optional ?export=csv for the GFX team (not ?format=: DRF reserves it and
answers 404 for a format it has no renderer for); optional ?pair=<team id>-<team id> for one team
pair's fights (the tab asks when a pair is opened, so the first answer stays small on a phone).
RESPONSE 200 (json): {event, stage, stages: [{id, name}], switches: {tab, overlay, export},
coverage: {maps, kills, kills_with_weapon}, sniper: [{uid, username, team_name, sniper_kills}],
head_to_head: [{team_a: {id, name}, team_b: {id, name}, met, wins_a, wins_b, draws, third_party}],
teams: {id: name}, players: {uid: {name, team_id}}, player_pairs: [[uid_a, uid_b, kills_a_on_b,
kills_b_on_a, knocks_a_on_b, knocks_b_on_a]], sniper_weapons: [{weapon_id, name}]}.
  third_party: in how many of the pair's fights a third team came in (inbox #383).
  player_pairs: who killed and knocked who, every two players who met in a fight (#384, #385:
  knocks count, a knock is not a kill and players left still drops only on a kill).
RESPONSE 200 (json, ?pair=): {pair: [a, b], fights: [{match_id, match_number, stage, group, start, end,
started_by: [a, b], third_parties: [team ids, in the order they came in], left: {team id: players left},
winner: team id | null, events: [[t, kind, killer_uid, victim_uid, killer_team, victim_team]]}],
duels: [[uid_a, uid_b, kills_a_on_b, kills_b_on_a, knocks_a_on_b, knocks_b_on_a]]} (duels: only
the two teams' players against each other). 400 {code: bad_pair} for a pair that is not two ids.
RESPONSE 200 (csv): three blocks, "sniper kills recorded", "head to head", "who killed and knocked who".
AUTH: open to anybody once the event switched the matching surface on (kill_feed_tab / kill_feed_overlay
for json, kill_feed_export for csv); before that, only the event's admins and organizer (to preview).
Refusals carry a code: event_not_found (404), kill_stats_off (403).
CONSUMED BY: the event page's Kill stats tab (AFC_Frontend components/events/KillStatsTab.tsx through
lib/killStats.ts; approved mockup mockups/kill-feed-tab/, inbox #392) and its CSV link. Next: the AFC LIVE
overlays (the player duel overlay gets its own design, #385).
"""
import csv
import io

from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response

from afc_auth.views import validate_token
from afc_organizers.permissions import org_can_event

from . import kill_feed
from .models import (
    Event, GameWeapon, Match, MatchKillEvent, Stages, TournamentPlayerMatchStats, TournamentTeam,
)

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

    # ?pair=<a>-<b>: one team pair's fights (read before any work, so a bad value costs nothing).
    pair_param = (request.GET.get("pair") or "").strip()
    want_pair = None
    if pair_param:
        bits = pair_param.split("-")
        if len(bits) != 2 or not all(b.isdigit() for b in bits) or bits[0] == bits[1]:
            return Response({"message": "pair must be two team ids, like 12-34.", "code": "bad_pair"}, status=400)
        want_pair = tuple(sorted(int(b) for b in bits))

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
    # Every fight of the event (or stage), each tagged with its map, in one flat list.
    all_fights = []
    for match_id, events in by_match.items():
        teams = {e["killer_team"] for e in events} | {e["victim_team"] for e in events}
        size = {t: sizes.get(match_id, {}).get(t, default_size) for t in teams if t is not None}
        for fight in kill_feed.fights_of_match(events, size):
            fight["match_id"] = match_id
            all_fights.append(fight)
    h2h = kill_feed.head_to_head([all_fights])

    if want_pair is not None:
        return Response(_pair_answer(want_pair, all_fights))

    # The admin's table when it has rows, else the proven defaults (kill_feed.KNOWN_WEAPONS).
    sniper_ids = (set(GameWeapon.objects.filter(is_sniper=True).values_list("weapon_id", flat=True))
                  if GameWeapon.objects.exists() else kill_feed.default_sniper_ids())
    counts = kill_feed.sniper_counts([e for evs in by_match.values() for e in evs], sniper_ids)
    from afc_auth.models import User

    # Everybody who killed or knocked, or was killed or knocked, with the roster team they played for.
    team_of = {}
    for evs in by_match.values():
        for e in evs:
            if e["killer_team"] is not None:
                team_of.setdefault(e["killer_uid"], e["killer_team"])
            if e["victim_team"] is not None:
                team_of.setdefault(e["victim_uid"], e["victim_team"])
    users = {str(u.uid).lstrip("0"): u for u in User.objects.filter(uid__in=list(team_of))}
    team_ids = set(team_of.values()) | {t for pair in h2h for t in pair}
    names = {tt.tournament_team_id: getattr(tt.team, "team_name", "") for tt in
             TournamentTeam.objects.filter(tournament_team_id__in=[t for t in team_ids if t]).select_related("team")}

    sniper = sorted(({"uid": uid, "username": getattr(users.get(uid.lstrip("0")), "username", ""),
                      "team_name": names.get(team_of.get(uid), ""), "sniper_kills": n}
                     for uid, n in counts.items()), key=lambda r: (-r["sniper_kills"], r["username"]))
    third = {}
    for fight in all_fights:
        if len(fight["teams"]) > 2:
            for pair in fight["pairs"]:
                third[pair] = third.get(pair, 0) + 1
    pairs = sorted(({"team_a": {"id": a, "name": names.get(a, "")}, "team_b": {"id": b, "name": names.get(b, "")},
                     "met": row["met"], "wins_a": row["wins"][a], "wins_b": row["wins"][b], "draws": row["draws"],
                     "third_party": third.get((a, b), 0)}
                    for (a, b), row in h2h.items()), key=lambda r: (-r["met"], r["team_a"]["name"]))
    player_pairs = _duels(e for fight in all_fights for e in fight["events"])
    player_names = {uid: getattr(users.get(uid.lstrip("0")), "username", "") or uid for uid in team_of}

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
        w.writerow([])
        w.writerow(["who killed and knocked who"])
        w.writerow(["player_a", "player_b", "a_killed_b", "b_killed_a", "a_knocked_b", "b_knocked_a"])
        for a, b, kab, kba, nab, nba in player_pairs:
            w.writerow([player_names.get(a, a), player_names.get(b, b), kab, kba, nab, nba])
        resp = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{event.slug}-kill-stats.csv"'
        return resp

    kills = [e for evs in by_match.values() for e in evs if e["kind"] == "kill"]
    return Response({
        "event": event.slug,
        "stage": int(stage_id) if stage_id and str(stage_id).isdigit() else None,
        "stages": [{"id": s.stage_id, "name": s.stage_name}
                   for s in Stages.objects.filter(event=event).order_by("stage_order", "stage_id")],
        "switches": {"tab": event.kill_feed_tab, "overlay": event.kill_feed_overlay,
                     "export": event.kill_feed_export},
        "coverage": {"maps": len(by_match), "kills": len(kills),
                     "kills_with_weapon": sum(1 for e in kills if e["weapon"] is not None)},
        "sniper": sniper,
        "head_to_head": pairs,
        "teams": {str(t): n for t, n in names.items()},
        "players": {uid: {"name": player_names[uid], "team_id": team_of[uid]} for uid in team_of},
        "player_pairs": player_pairs,
        "sniper_weapons": ([{"weapon_id": w, "name": kill_feed.KNOWN_WEAPONS.get(w, (str(w), True))[0]}
                            for w in sorted(sniper_ids)]),
    })


# ── who killed and knocked who ───────────────────────────────────────────────────────────────────
def _duels(events):
    """[[uid_a, uid_b, kills a on b, kills b on a, knocks a on b, knocks b on a]] for every two players
    of different teams who knocked or killed each other, the pair in uid order, most meetings first."""
    rows = {}
    for e in events:
        a, b = e["killer_uid"], e["victim_uid"]
        if not a or not b or a == b or e["killer_team"] == e["victim_team"]:
            continue
        lo, hi = sorted((a, b))
        row = rows.setdefault((lo, hi), [lo, hi, 0, 0, 0, 0])
        forward = a == lo
        if e["kind"] == "kill":
            row[2 if forward else 3] += 1
        else:
            row[4 if forward else 5] += 1
    return sorted(rows.values(), key=lambda r: (-(r[2] + r[3] + r[4] + r[5]), r[0], r[1]))


def _pair_answer(pair, all_fights):
    """One team pair's fights, every kill and knock in order, and its players against each other."""
    a, b = pair
    mine = [f for f in all_fights if pair in f["pairs"]]
    meta = {m.match_id: m for m in Match.objects.filter(match_id__in={f["match_id"] for f in mine})
            .select_related("group__stage")}
    fights = []
    for f in sorted(mine, key=lambda f: (getattr(meta.get(f["match_id"]), "match_number", 0) or 0, f["match_id"],
                                         f["start"])):
        m = meta.get(f["match_id"])
        fights.append({
            "match_id": f["match_id"],
            "match_number": getattr(m, "match_number", None),
            "stage": getattr(getattr(getattr(m, "group", None), "stage", None), "stage_name", ""),
            "group": getattr(getattr(m, "group", None), "group_name", ""),
            "start": f["start"], "end": f["end"],
            "started_by": f["started_by"],
            "third_parties": f["teams"][2:],
            "left": {str(t): n for t, n in f["left"].items()},
            "winner": f["pairs"][pair]["winner"],
            "events": [[e["t"], e["kind"], e["killer_uid"], e["victim_uid"], e["killer_team"], e["victim_team"]]
                       for e in f["events"]],
        })
    duels = _duels(e for f in mine for e in f["events"] if {e["killer_team"], e["victim_team"]} == {a, b})
    return {"pair": [a, b], "fights": fights, "duels": duels}

