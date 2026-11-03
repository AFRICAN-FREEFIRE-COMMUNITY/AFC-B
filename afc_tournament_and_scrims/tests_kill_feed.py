"""Kill feed: sniper kills recorded and team head to head (owner 2026-10-21 / 23, inbox #326, #350, #376).

The owner's rules, one test each: bolt-action snipers only, and only kills whose weapon the game reported;
a third team that fights either side joins the SAME fight; players left counts finished kills only (a knock
does not lower it); a fight ends after FIGHT_GAP_SECONDS of game time with nothing among its teams; each
surface is switched per event. The log lines are the real formats (the same script afc-capture's tailer
tests use: tests/test_tailer.py SCRIPT).
"""
import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import kill_feed
from afc_tournament_and_scrims.models import (
    Event, GameWeapon, Leaderboard, Match, MatchKillEvent, StageGroups, Stages, TournamentTeam,
    TournamentTeamMember,
)
from afc_tournament_and_scrims.weapon_names import WEAPON_NAMES
from afc_tournament_and_scrims.weapon_names_retired import RETIRED_NAMES
from afc_tournament_and_scrims.weapons import UNKNOWN, admin_names, weapon_label

LOG = "\n".join([
    "[2026-07-01 18:29:59.000][0][0] menu noise before the first round",
    "[2026-07-01 18:30:00.000][1][1] EventTypeEnterGame",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 6643413913, 16777217, CRDㅤHANZO,False1",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 6152193352, 16777218, CRDㅤWARD7,False1",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 1111111111, 33554433, SNTㅤRAGE,False2",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 2222222222, 33554434, SNTㅤVOID,False2",
    "[2026-07-01 18:30:04.500][2][9] @zwj PlayKnockDownGunTrace killer=33554433 victim=16777217 headshot=True",
    # the knock itself (inbox #396: the trace also fires on direct kills, so it is never the knock)
    "[2026-07-01 18:30:04.500][2][9] Player '16777217' Knock Down, by '33554433'",
    "[2026-07-01 18:30:05.000][2][11] Player 16777217 Dead, killed by 33554433",
    "[2026-07-01 18:30:05.000][2][11] @qhz [PC] OnGameAchievementNotify killerId:33554433 beKilled:16777217 weapon:601",
    "[2026-07-01 18:30:06.500][2][12] @zwj PlayKnockDownGunTrace killer=16777218 victim=33554433 headshot=False",
    "[2026-07-01 18:30:06.500][2][12] Player '33554433' Knock Down, by '16777218'",
    "[2026-07-01 18:30:07.000][3][13] Player 33554433 Dead, killed by 16777218",
    "[2026-07-01 18:30:07.000][3][13] @qhz [PC] OnGameAchievementNotify killerId:16777218 beKilled:33554433 weapon:-5",
    "[2026-07-01 18:30:12.000][4][15] @zwj PlayKnockDownGunTrace killer=33554434 victim=16777218 headshot=True",
    "[2026-07-01 18:30:12.000][4][15] Player '16777218' Knock Down, by '33554434'",
    "[2026-07-01 18:30:12.000][4][15] Player 16777218 Dead, killed by 33554434",
    "[2026-07-01 18:30:12.000][4][15] @qhz [PC] OnGameAchievementNotify killerId:33554434 beKilled:16777218 weapon:113",
    "[2026-07-01 18:31:00.000][5][20] EventTypeEnterGame",
    "[2026-07-01 18:31:05.000][5][21] lobby: nobody joined this one",
])


def ev(t, kind, a, b, weapon=None):
    return {"t": t, "kind": kind, "killer_uid": f"u{a}", "victim_uid": f"v{b}",
            "killer_team": a, "victim_team": b, "weapon": weapon}


class ParseTests(TestCase):
    def test_the_log_becomes_one_round_of_kills_and_knocks_with_weapons(self):
        rounds = kill_feed.parse_kill_events(LOG)
        self.assertEqual(len(rounds), 1, "the empty second round and the menu noise are not rounds")
        kinds = [(e["kind"], e["killer_uid"], e["victim_uid"], e["weapon"]) for e in rounds[0]]
        self.assertEqual(kinds, [
            ("knock", "1111111111", "6643413913", None),
            ("kill", "1111111111", "6643413913", 601),
            ("knock", "6152193352", "1111111111", None),
            ("kill", "6152193352", "1111111111", None),     # weapon -5 (bleed out) is not a gun
            ("knock", "2222222222", "6152193352", None),
            ("kill", "2222222222", "6152193352", 113),
        ])
        self.assertEqual(rounds[0][0]["t"], 4.5)
        self.assertTrue(rounds[0][0]["headshot"])

    def test_a_bad_capture_payload_is_refused_and_teams_are_never_taken_from_it(self):
        self.assertIsNone(kill_feed.clean_events({"not": "a list"}))
        self.assertIsNone(kill_feed.clean_events([{"kind": "explode"}]))
        out = kill_feed.clean_events([{"t": 3, "kind": "kill", "killer_uid": "11", "victim_uid": "22",
                                       "weapon": 7, "killer_team": 999}])
        self.assertEqual(out, [{"t": 3.0, "kind": "kill", "killer_uid": "11", "victim_uid": "22",
                                "headshot": None, "weapon": 7}])


class FightTests(TestCase):
    def test_a_third_team_that_comes_in_joins_the_same_fight(self):
        events = [ev(0, "knock", 1, 2), ev(5, "kill", 1, 2), ev(12, "kill", 3, 1), ev(20, "kill", 3, 1)]
        fights = kill_feed.fights_of_match(events, {1: 4, 2: 4, 3: 4})
        self.assertEqual(len(fights), 1)
        self.assertEqual(fights[0]["teams"], [1, 2, 3])
        self.assertEqual(fights[0]["started_by"], [1, 2])
        self.assertEqual(fights[0]["left"], {1: 2, 2: 3, 3: 4})
        self.assertEqual(fights[0]["pairs"][(1, 2)]["winner"], 2)    # 2 has 3 left against 1's 2
        self.assertEqual(fights[0]["pairs"][(1, 3)]["winner"], 3)
        self.assertNotIn((2, 3), fights[0]["pairs"], "2 and 3 never fought each other")
        self.assertEqual([(e["t"], e["kind"]) for e in fights[0]["events"]],
                         [(0, "knock"), (5, "kill"), (12, "kill"), (20, "kill")], "the fight keeps its own events")

    def test_a_knock_does_not_lower_players_left(self):
        events = [ev(0, "knock", 1, 2), ev(2, "knock", 1, 2), ev(4, "knock", 1, 2), ev(6, "kill", 2, 1)]
        fight = kill_feed.fights_of_match(events, {1: 4, 2: 4})[0]
        self.assertEqual(fight["left"], {1: 3, 2: 4})
        self.assertEqual(fight["pairs"][(1, 2)]["winner"], 2)

    def test_a_quiet_gap_ends_the_fight_and_the_next_one_is_new(self):
        gap = kill_feed.FIGHT_GAP_SECONDS
        events = [ev(0, "kill", 1, 2), ev(gap + 1, "kill", 2, 1)]
        fights = kill_feed.fights_of_match(events, {1: 4, 2: 4})
        self.assertEqual(len(fights), 2)
        self.assertEqual(fights[0]["pairs"][(1, 2)]["winner"], 1)
        self.assertIsNone(fights[1]["pairs"][(1, 2)]["winner"], "3 against 3: a draw")

    def test_head_to_head_adds_up_across_maps(self):
        m1 = kill_feed.fights_of_match([ev(0, "kill", 1, 2)], {1: 4, 2: 4})
        m2 = kill_feed.fights_of_match([ev(0, "kill", 2, 1), ev(1, "kill", 2, 1)], {1: 4, 2: 4})
        m3 = kill_feed.fights_of_match([ev(0, "knock", 1, 2)], {1: 4, 2: 4})
        h2h = kill_feed.head_to_head([m1, m2, m3])
        self.assertEqual(h2h[(1, 2)], {"met": 3, "wins": {1: 1, 2: 1}, "draws": 1})

    def test_sniper_kills_count_only_bolt_action_kills_with_a_weapon(self):
        events = [ev(0, "kill", 1, 2, weapon=50), ev(1, "kill", 1, 2, weapon=None), ev(2, "knock", 1, 2, weapon=50),
                  ev(3, "kill", 1, 2, weapon=9)]
        self.assertEqual(kill_feed.sniper_counts(events, {50}), {"u1": 1})


class KillStatsEndpointTests(TestCase):
    URL = "/events/kill-stats/"

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(username="kfadmin", email="kfadmin@x.com", full_name="KF Admin",
                                         role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="kf-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="duo", event_type="internal",
            max_teams_or_players=16, event_name="NFL Kill Cup", event_mode="virtual", start_date=today,
            end_date=today, registration_open_date=today, registration_end_date=today, prizepool="0",
            event_rules="r", event_status="ongoing", registration_link="https://x.com/r", number_of_stages=1,
            creator=self.admin)
        stage = Stages.objects.create(event=self.event, stage_name="Groups", start_date=today, end_date=today,
                                      number_of_groups=1, stage_format="br - normal",
                                      teams_qualifying_from_stage=2, stage_order=1)
        group = StageGroups.objects.create(stage=stage, group_name="A", playing_date=today,
                                           playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        lb = Leaderboard.objects.create(leaderboard_name="A", event=self.event, stage=stage, group=group,
                                        creator=self.admin, placement_points={"1": 12}, kill_point=1.0,
                                        leaderboard_method="manual")
        self.match = Match.objects.create(leaderboard=lb, group=group, match_number=1, match_map="bermuda",
                                          scoring_settings={"placement_points": {"1": 12}, "kill_point": 1})
        for name, uids in (("CREED", ("6643413913", "6152193352")), ("SAINT", ("1111111111", "2222222222"))):
            team = Team.objects.create(team_name=name, team_tag=name[:3], join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin, country="NG")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            for uid in uids:
                u = User.objects.create(username=f"p{uid}", email=f"p{uid}@x.com", full_name=uid,
                                        role="player", password="x", uid=uid)
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)
        GameWeapon.objects.create(weapon_id=113, name="AWM (example id)", is_sniper=True)
        rounds = kill_feed.parse_kill_events(LOG)
        self.stored = kill_feed.store_events(self.match, self.event, rounds[0], "backfill")

    def test_events_are_stored_with_the_roster_teams(self):
        self.assertEqual(self.stored, 6)
        row = MatchKillEvent.objects.get(match=self.match, kind="kill", weapon_id=601)
        self.assertEqual(row.killer_team.team.team_name, "SAINT")
        self.assertEqual(row.victim_team.team.team_name, "CREED")

    def test_switched_off_it_answers_only_the_organizer(self):
        resp = self.client.get(self.URL, {"event": self.event.slug})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "kill_stats_off")
        resp = self.client.get(self.URL, {"event": self.event.slug}, HTTP_AUTHORIZATION="Bearer kf-admin-token")
        self.assertEqual(resp.status_code, 200)

    def test_switched_on_anybody_reads_the_sniper_and_head_to_head(self):
        self.event.kill_feed_tab = True
        self.event.save(update_fields=["kill_feed_tab"])
        body = self.client.get(self.URL, {"event": self.event.slug}).json()
        self.assertEqual(body["coverage"], {"maps": 1, "kills": 3, "kills_with_weapon": 2})
        self.assertEqual([(r["username"], r["sniper_kills"]) for r in body["sniper"]], [("p2222222222", 1)])
        pair = body["head_to_head"][0]
        self.assertEqual(pair["met"], 1)
        # duo: 2 each; CREED lost HANZO and WARD7 (0 left), SAINT lost RAGE (1 left): SAINT won
        winner = pair["team_a"]["name"] if pair["wins_a"] else pair["team_b"]["name"]
        self.assertEqual(winner, "SAINT")
        self.assertEqual(self.client.get(self.URL, {"event": self.event.slug, "export": "csv"}).status_code, 403,
                         "the export has its own switch")

    def test_the_export_is_a_csv_for_the_gfx_team(self):
        self.event.kill_feed_export = True
        self.event.save(update_fields=["kill_feed_export"])
        resp = self.client.get(self.URL, {"event": self.event.slug, "export": "csv"})
        self.assertEqual(resp.status_code, 200)
        text = resp.content.decode("utf-8")
        self.assertIn("sniper kills recorded", text)
        self.assertIn("p2222222222", text)

    # ── the event tab's answer (inbox #392) ──
    def _ids(self):
        tt = {t.team.team_name: t.tournament_team_id for t in TournamentTeam.objects.filter(event=self.event)}
        return tt["CREED"], tt["SAINT"]

    def test_the_answer_carries_stages_names_and_who_killed_and_knocked_who(self):
        self.event.kill_feed_tab = True
        self.event.save(update_fields=["kill_feed_tab"])
        body = self.client.get(self.URL, {"event": self.event.slug}).json()
        self.assertEqual([s["name"] for s in body["stages"]], ["Groups"])
        self.assertEqual(body["head_to_head"][0]["third_party"], 0)
        self.assertEqual(body["players"]["1111111111"]["name"], "p1111111111")
        creed, saint = self._ids()
        self.assertEqual(body["players"]["1111111111"]["team_id"], saint)
        self.assertEqual(body["teams"][str(creed)], "CREED")
        # RAGE knocked and killed HANZO; WARD7 knocked and killed RAGE; VOID knocked and killed WARD7.
        pairs = {(r[0], r[1]): r[2:] for r in body["player_pairs"]}
        self.assertEqual(pairs[("1111111111", "6643413913")], [1, 0, 1, 0])
        self.assertEqual(pairs[("1111111111", "6152193352")], [0, 1, 0, 1])
        self.assertEqual(pairs[("2222222222", "6152193352")], [1, 0, 1, 0])
        self.assertEqual(len(pairs), 3)

    def test_a_pair_answers_its_fights_in_order_and_its_players_duels(self):
        self.event.kill_feed_tab = True
        self.event.save(update_fields=["kill_feed_tab"])
        creed, saint = self._ids()
        body = self.client.get(self.URL, {"event": self.event.slug, "pair": f"{saint}-{creed}"}).json()
        self.assertEqual(body["pair"], sorted([creed, saint]))
        self.assertEqual(len(body["fights"]), 1)
        fight = body["fights"][0]
        self.assertEqual((fight["match_number"], fight["stage"], fight["group"]), (1, "Groups", "A"))
        self.assertEqual(fight["third_parties"], [])
        self.assertEqual(fight["left"], {str(creed): 0, str(saint): 1})
        self.assertEqual(fight["winner"], saint)
        self.assertEqual([e[1] for e in fight["events"]], ["knock", "kill", "knock", "kill", "knock", "kill"])
        self.assertEqual(len(body["duels"]), 3)

    def test_a_bad_pair_is_a_coded_400_and_a_switched_off_pair_is_refused(self):
        self.event.kill_feed_tab = True
        self.event.save(update_fields=["kill_feed_tab"])
        resp = self.client.get(self.URL, {"event": self.event.slug, "pair": "12"})
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "bad_pair"))
        self.event.kill_feed_tab = False
        self.event.save(update_fields=["kill_feed_tab"])
        creed, saint = self._ids()
        resp = self.client.get(self.URL, {"event": self.event.slug, "pair": f"{creed}-{saint}"})
        self.assertEqual((resp.status_code, resp.json()["code"]), (403, "kill_stats_off"))

    def test_the_csv_lists_who_killed_and_knocked_who(self):
        self.event.kill_feed_export = True
        self.event.save(update_fields=["kill_feed_export"])
        text = self.client.get(self.URL, {"event": self.event.slug, "export": "csv"}).content.decode("utf-8")
        self.assertIn("who killed and knocked who", text)
        self.assertIn("p1111111111,p6643413913,1,0,1,0", text)

    def test_an_unknown_event_is_a_coded_404(self):
        resp = self.client.get(self.URL, {"event": "no-such-event"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "event_not_found")

    def test_the_sniper_weapons_are_named_never_a_bare_number(self):
        # inbox #468: an admin ticked a gun nobody has named yet; it reads "unknown gun (id N)", not "80006"
        GameWeapon.objects.create(weapon_id=80006, name="", is_sniper=True)
        body = self.client.get(self.URL, {"event": self.event.slug},
                               HTTP_AUTHORIZATION="Bearer kf-admin-token").json()
        named = {w["weapon_id"]: w["name"] for w in body["sniper_weapons"]}
        self.assertEqual(named, {113: "AWM (example id)", 80006: "unknown gun (id 80006)"})

    def test_weapons_used_lists_kills_per_gun_by_name_with_the_top_player(self):
        # inbox #467, the leaderboard's Kill stats tab: the log has a grenade kill (601) by RAGE and an
        # admin-named 113 kill by VOID; the zone kill (-5) has no gun and is left out
        body = self.client.get(self.URL, {"event": self.event.slug},
                               HTTP_AUTHORIZATION="Bearer kf-admin-token").json()
        got = {w["name"]: (w["kills"], w["top"]["name"], w["top"]["team_name"], w["top"]["kills"])
               for w in body["weapons"]}
        self.assertEqual(got, {"Grenade": (1, "p1111111111", "SAINT", 1),
                               "AWM (example id)": (1, "p2222222222", "SAINT", 1)})
        self.assertEqual(sum(w["kills"] for w in body["weapons"]), body["coverage"]["kills_with_weapon"])


class CaptureKillEventsUploadTests(TestCase):
    """AFC Capture sends `kill_events` beside the MatchResult file (inbox #376): stored for the map with the
    roster teams; a bad payload never fails the upload. Same fixture as tests_capture_rich_stats."""

    def setUp(self):
        from afc_tournament_and_scrims.tests_capture_rich_stats import _Fixture

        self.fx = _Fixture(methodName="setUp")
        self.fx.setUp()

    def _upload(self, kill_events):
        import json as _json

        from django.core.files.uploadedfile import SimpleUploadedFile

        from afc_tournament_and_scrims.tests_capture_rich_stats import LOG as RICH_LOG

        f = SimpleUploadedFile("match.log", RICH_LOG.encode("utf-8"), content_type="text/plain")
        data = {"match_id": self.fx.match.match_id, "file": f,
                "kill_events": kill_events if isinstance(kill_events, str) else _json.dumps(kill_events)}
        return self.fx.client.post("/events/upload-team-match-result/", data=data,
                                   HTTP_AUTHORIZATION=f"Bearer {self.fx.token.token}")

    def test_the_capture_upload_stores_the_maps_kill_events(self):
        resp = self._upload([
            {"t": 12.5, "kind": "knock", "killer_uid": "1001", "victim_uid": "300", "headshot": True, "weapon": None},
            {"t": 13.0, "kind": "kill", "killer_uid": "1001", "victim_uid": "300", "headshot": None, "weapon": 113},
        ])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["kill_events_stored"], 2)
        rows = list(MatchKillEvent.objects.filter(match=self.fx.match).order_by("seq"))
        self.assertEqual([r.kind for r in rows], ["knock", "kill"])
        self.assertEqual(rows[1].killer_team_id, self.fx.tt["TRG ESPORT"].pk)
        self.assertEqual(rows[1].victim_team_id, self.fx.tt["KOCC"].pk)
        self.assertEqual(rows[1].source, "capture")

    def test_a_bad_kill_events_payload_never_fails_the_upload(self):
        resp = self._upload("{not json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["kill_events_error"], "bad_shape")
        self.assertFalse(MatchKillEvent.objects.exists())


class WeaponTableTests(TestCase):
    """The weapon table (kill_feed.KNOWN_WEAPONS, from the game's own names) and the seed command (inbox #350,
    #474: "EVO3 and SeaGull are not the actual names of the guns please")."""

    def test_the_defaults_count_the_owners_four_guns_and_every_version_of_them(self):
        ids = kill_feed.default_sniper_ids()
        # AWM, its gold / Droid / awakened / Blood Bat versions; Kar98k and its levels; M24 and its levels; M82B
        for sniper in (4, 172, 65, 635, 30924, 21, 64, 128, 129, 175, 75, 805, 806, 45, 173, 812, 30805, 30909):
            self.assertIn(sniper, ids, sniper)
        # marksman rifles, the grenade, the XM8 159 once guessed to be an M82B, the M249 / M249-X (not the M24)
        for other in (26, 89, 197, 18, 11, 13, 601, 159, 19, 71):
            self.assertNotIn(other, ids, other)

    def test_a_version_is_named_after_its_gun_and_the_m249_is_not_the_m24(self):
        self.assertEqual(kill_feed.gun_of("AWM-Y"), "AWM")
        self.assertEqual(kill_feed.gun_of("Kar98k-III (Droid Apocalypse)"), "Kar98k")
        self.assertEqual(kill_feed.gun_of("M24-I"), "M24")
        self.assertEqual(kill_feed.gun_of("M249-X"), "M249")

    def test_the_names_are_the_games_and_never_an_internal_label(self):
        self.assertEqual((WEAPON_NAMES[814], WEAPON_NAMES[812], WEAPON_NAMES[809], WEAPON_NAMES[815]),
                         ("Skorp", "Hawk", "M7", "RPK"))
        self.assertEqual((WEAPON_NAMES[45], WEAPON_NAMES[48], WEAPON_NAMES[57], WEAPON_NAMES[21002]),
                         ("M82B", "Woodpecker", "Kingfisher", "M590"))
        internal = {"EVO3", "SeaGull", "BLT", "M21A5", "AR15", "XM82", "Changuo", "RG550"}
        for wid, name in WEAPON_NAMES.items():
            self.assertNotIn(name, internal, wid)
            self.assertFalse(any(bit in name for bit in ("BloodBat", "Blood Bat", "FFWS", "Storm", "风暴")), wid)

    def test_every_seed_name_fits_the_column(self):
        # 159's label was 61 characters for a 60 character column: the seed crashed on any table without 159
        limit = GameWeapon._meta.get_field("name").max_length
        for wid, (name, _) in kill_feed.KNOWN_WEAPONS.items():
            self.assertLessEqual(len(name), limit, wid)
        for wid in RETIRED_NAMES:
            self.assertLessEqual(len(UNKNOWN.format(wid)), limit, wid)

    def test_the_seed_fills_an_empty_table(self):
        call_command("seed_game_weapons", stdout=StringIO())
        self.assertEqual(GameWeapon.objects.count(), len(WEAPON_NAMES))
        m82b = GameWeapon.objects.get(weapon_id=45)
        self.assertEqual((m82b.name, m82b.is_sniper), ("M82B", True))
        hawk = GameWeapon.objects.get(weapon_id=812)
        self.assertEqual((hawk.name, hawk.is_sniper), ("Hawk", True), "the owner added the Hawk, 3 Nov 2026")
        self.assertFalse(GameWeapon.objects.filter(weapon_id=80006).exists(), "no name is ever invented")

    def test_the_seed_renames_what_a_machine_wrote_and_never_what_an_admin_did(self):
        # production on 3 Nov 2026: 259 rows as the old seed wrote them, none edited
        for wid, name, sniper in ((814, "EVO3", False), (45, "BLT", False), (21, "Kar98k", True),
                                  (159, "XM82, likely the M82B: confirm on stream first", False),
                                  (26, "SVD (marksman, not counted)", False), (65, "Gold AWM", True),
                                  (102, "Fak", False)):
            GameWeapon.objects.create(weapon_id=wid, name=name, is_sniper=sniper)
        GameWeapon.objects.create(weapon_id=50, name="Big Shotgun")                   # an admin's name
        GameWeapon.objects.create(weapon_id=75, name="M24", is_sniper=False)           # an admin's unticked sniper
        call_command("seed_game_weapons", stdout=StringIO())
        got = {w.weapon_id: (w.name, w.is_sniper) for w in GameWeapon.objects.all()}
        self.assertEqual(got[814], ("Skorp", False))
        self.assertEqual(got[45], ("M82B", True), "the M82B is the owner's sniper")
        self.assertEqual(got[159], ("XM8 (Droid Apocalypse)", False))
        self.assertEqual(got[26], ("SVD", False))
        self.assertEqual(got[65], ("AWM-Y", True))
        self.assertEqual(got[21], ("Kar98k", True))
        self.assertEqual(got[102], ("unknown gun (id 102)", False), "an id the game's text does not name")
        self.assertEqual(got[50], ("Big Shotgun", False), "an admin's name stays")
        self.assertEqual(got[75], ("M24", False), "an admin's tick stays")
        self.assertEqual(len(got), len(set(WEAPON_NAMES) | {102}))
        out = StringIO()
        call_command("seed_game_weapons", stdout=out)
        self.assertIn("0 new, 0 updated", out.getvalue())


class StoredWeaponNameTests(TestCase):
    """seed_game_weapons names the most-used weapons stored as a code (inbox #468) or under a name an older table
    wrote (inbox #474: "EVO3" on 75 production rows on 3 Nov 2026)."""
    setUp = KillStatsEndpointTests.setUp       # the same event, match and roster teams, not its tests

    def test_the_seed_names_codes_and_old_machine_names_and_leaves_real_names(self):
        from afc_tournament_and_scrims.models import TournamentPlayerMatchStats as Stats
        from afc_tournament_and_scrims.models import TournamentTeamMatchStats

        team_stats = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=TournamentTeam.objects.filter(event=self.event).first())
        made = [Stats.objects.create(team_stats=team_stats, most_used_weapon=v).pk
                for v in ("9", "9", "21002", "unknown gun (id 815)", "EVO3", "BLT", "unknown gun (id 80006)",
                          "MP40", "", "Gold MP5", "M249", "Gold Mystery")]
        out = StringIO()
        call_command("seed_game_weapons", "--dry-run", stdout=out)
        self.assertIn("7 stored most-used weapons would be named", out.getvalue())
        self.assertEqual(Stats.objects.filter(most_used_weapon="9").count(), 2, "a dry run changes nothing")
        call_command("seed_game_weapons", stdout=StringIO())
        values = sorted(Stats.objects.filter(pk__in=made).values_list("most_used_weapon", flat=True))
        # "Gold MP5" was three gold MP5 levels (60, 120, 121): it names the MP5; "M249" is a gun's current name and
        # "Gold Mystery" no table ever wrote: both stay as they are
        self.assertEqual(values, sorted(["Desert Eagle", "Desert Eagle", "M590", "RPK", "Skorp", "M82B",
                                         "unknown gun (id 80006)", "MP40", "", "MP5", "M249", "Gold Mystery"]))
        # an admin names 80006 later: the next run reaches the rows stored as unknown
        GameWeapon.objects.create(weapon_id=80006, name="Mystery SMG")
        out = StringIO()
        call_command("seed_game_weapons", stdout=out)
        self.assertIn("1 stored most-used weapons named", out.getvalue())
        self.assertTrue(Stats.objects.filter(pk__in=made, most_used_weapon="Mystery SMG").exists())


class WeaponNameTests(TestCase):
    """weapons.weapon_label, the site's one rule for a gun's name (inbox #468, #474): never a bare number, never an
    internal label."""

    def test_an_id_reads_as_the_gun_the_game_shows(self):
        for value in (9, "9", " 9 "):
            self.assertEqual(weapon_label(value), "Desert Eagle")
        self.assertEqual(weapon_label("601"), "Grenade")
        self.assertEqual(weapon_label(21), "Kar98k")
        self.assertEqual(weapon_label("814"), "Skorp")
        self.assertEqual(weapon_label("unknown gun (id 812)"), "Hawk", "a row stored as unknown is named")

    def test_an_old_machine_name_reads_as_the_gun(self):
        self.assertEqual(weapon_label("EVO3"), "Skorp")
        self.assertEqual(weapon_label("SeaGull"), "Hawk")
        self.assertEqual(weapon_label("BLT"), "M82B")
        self.assertEqual(weapon_label("Gold MP5"), "MP5", "three gold MP5 levels had it: it names the gun")

    def test_an_id_nobody_named_says_so(self):
        self.assertEqual(weapon_label("80006"), "unknown gun (id 80006)")
        self.assertEqual(weapon_label("unknown gun (id 80006)"), "unknown gun (id 80006)", "naming twice is a no-op")

    def test_no_gun_is_empty(self):
        for value in (None, "", "-5", -5, "  ", True, False):
            self.assertEqual(weapon_label(value), "", value)

    def test_a_name_stays_a_name(self):
        self.assertEqual(weapon_label("Desert Eagle"), "Desert Eagle")

    def test_an_admin_name_wins_but_a_machine_name_never_reaches_a_screen(self):
        GameWeapon.objects.create(weapon_id=814, name="MP40 Cobra")
        GameWeapon.objects.create(weapon_id=159, name="M82B")
        GameWeapon.objects.create(weapon_id=26, name="SVD (marksman, not counted)")   # the old seed's award note
        GameWeapon.objects.create(weapon_id=812, name="SeaGull")                       # the old seed's label
        self.assertEqual(weapon_label("814"), "MP40 Cobra")
        self.assertEqual(weapon_label("unknown gun (id 814)"), "MP40 Cobra")
        self.assertEqual(weapon_label("159"), "M82B")
        self.assertEqual(weapon_label("26"), "SVD", "not 'SVD (marksman, not counted)'")
        self.assertEqual(weapon_label("812"), "Hawk", "not 'SeaGull'")

    def test_one_query_names_a_whole_snapshot(self):
        names = admin_names()
        with self.assertNumQueries(0):
            self.assertEqual([weapon_label(v, names) for v in ("9", "88", "814", "21002", "80006")],
                             ["Desert Eagle", "MAC10", "Skorp", "M590", "unknown gun (id 80006)"])
