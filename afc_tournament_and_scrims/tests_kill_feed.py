"""Kill feed: sniper kills recorded and team head to head (owner 2026-10-21 / 23, inbox #326, #350, #376).

The owner's rules, one test each: bolt-action snipers only, and only kills whose weapon the game reported;
a third team that fights either side joins the SAME fight; players left counts finished kills only (a knock
does not lower it); a fight ends after FIGHT_GAP_SECONDS of game time with nothing among its teams; each
surface is switched per event. The log lines are the real formats (the same script afc-capture's tailer
tests use: tests/test_tailer.py SCRIPT).
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import kill_feed
from afc_tournament_and_scrims.models import (
    Event, GameWeapon, Leaderboard, Match, MatchKillEvent, StageGroups, Stages, TournamentTeam,
    TournamentTeamMember,
)

LOG = "\n".join([
    "[2026-07-01 18:29:59.000][0][0] menu noise before the first round",
    "[2026-07-01 18:30:00.000][1][1] EventTypeEnterGame",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 6643413913, 16777217, CRDㅤHANZO,False1",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 6152193352, 16777218, CRDㅤWARD7,False1",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 1111111111, 33554433, SNTㅤRAGE,False2",
    "[2026-07-01 18:30:00.050][1][2] Player Join, 2222222222, 33554434, SNTㅤVOID,False2",
    "[2026-07-01 18:30:04.500][2][9] @zwj PlayKnockDownGunTrace killer=33554433 victim=16777217 headshot=True",
    "[2026-07-01 18:30:05.000][2][11] Player 16777217 Dead, killed by 33554433",
    "[2026-07-01 18:30:05.000][2][11] @qhz [PC] OnGameAchievementNotify killerId:33554433 beKilled:16777217 weapon:601",
    "[2026-07-01 18:30:06.500][2][12] @zwj PlayKnockDownGunTrace killer=16777218 victim=33554433 headshot=False",
    "[2026-07-01 18:30:07.000][3][13] Player 33554433 Dead, killed by 16777218",
    "[2026-07-01 18:30:07.000][3][13] @qhz [PC] OnGameAchievementNotify killerId:16777218 beKilled:33554433 weapon:-5",
    "[2026-07-01 18:30:12.000][4][15] @zwj PlayKnockDownGunTrace killer=33554434 victim=16777218 headshot=True",
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

    def test_an_unknown_event_is_a_coded_404(self):
        resp = self.client.get(self.URL, {"event": "no-such-event"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "event_not_found")


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
    """The proven weapon ids (kill_feed.KNOWN_WEAPONS) and the seed command (inbox #350)."""

    def test_the_defaults_count_bolt_action_only(self):
        ids = kill_feed.default_sniper_ids()
        for bolt in (4, 172, 65, 21, 64, 128, 129, 175, 75):
            self.assertIn(bolt, ids)
        for marksman_or_other in (26, 89, 197, 18, 11, 13, 601, 159):   # 159 waits for a stream check
            self.assertNotIn(marksman_or_other, ids)

    def test_the_seed_fills_once_and_never_overwrites_an_admin_edit(self):
        from io import StringIO

        from django.core.management import call_command

        GameWeapon.objects.create(weapon_id=159, name="M82B (checked on stream)", is_sniper=True)
        call_command("seed_game_weapons", stdout=StringIO())
        self.assertEqual(GameWeapon.objects.count(), len(kill_feed.KNOWN_WEAPONS))
        self.assertTrue(GameWeapon.objects.get(weapon_id=159).is_sniper, "the admin's tick stays")
        self.assertEqual(GameWeapon.objects.get(weapon_id=21).name, "Kar98k")
        out = StringIO()
        call_command("seed_game_weapons", stdout=out)
        self.assertIn("0 new", out.getvalue())
