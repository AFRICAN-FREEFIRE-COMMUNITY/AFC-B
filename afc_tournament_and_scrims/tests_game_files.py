"""Game files upload (owner 2026-10-24, inbox #386 #391 #394 #396): many debugger + result files read as games.

The rules, one test each, all measured on real logs first (game_files.py header): the knock is the "Knock
Down" line and never the gun trace; a bleed-out after the knocker left for good is nobody's kill, while a
knocker who is only dead (this mode brings players back) still gets it; a client restart is joined into
one game; a lobby that never played is dropped; a debugger game and its result file pair by match id and
agree; a game with no result file needs a placement for every team. The log lines are the real formats.
"""
import datetime
import gzip

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import game_files as gf
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, MatchKillEvent, StageGroupCompetitor, StageGroups, Stages, TournamentPlayerMatchStats,
    TournamentTeam, TournamentTeamMember,
)

A1, A2, B1, B2 = "16777217", "16777218", "33554433", "33554434"   # two teams of two (eid >> 24 = 1, 2)
UIDS = {A1: "6643413913", A2: "6152193352", B1: "1111111111", B2: "2222222222"}


def line(sec, text, day="2026-10-20", hour=19):
    return f"[{day} {hour}:{sec // 60:02d}:{sec % 60:02d}.000][1][1] {text}"


def joins(sec=1):
    return [line(sec, f"Player Join, {UIDS[e]}, {e}, P{e[-2:]},False{int(e) >> 24}") for e in (A1, A2, B1, B2)]


GAME = "\n".join([
    line(0, "EventTypeEnterGame"), *joins(),
    # A1 knocks B1 (headshot trace first), then finishes him
    line(10, f"@zwj PlayKnockDownGunTrace killer={A1} victim={B1} headshot=True"),
    line(10, f"Player '{B1}' Knock Down, by '{A1}'"),
    line(12, f"Player {B1} Dead, killed by {A1}"),
    line(12, f"@qhz [PC] OnGameAchievementNotify killerId:{A1} beKilled:{B1} weapon:113"),
    # B2 kills A2 outright: a trace, no knock line. Not a knock.
    line(20, f"@zwj PlayKnockDownGunTrace killer={B2} victim={A2} headshot=False"),
    line(20, f"Player {A2} Dead, killed by {B2}"),
    line(21, f"NotifyPlayerEnterPendingRevive -> {A2}"),
    # B1 comes back; A2 is only dead, then knocks... no: A1 knocks B2, A1 dies but comes back, B2 bleeds out
    line(25, f"Revive Player {B1}, revivePosition=(1.00, 2.00, 3.00)"),
    line(30, f"Player '{B2}' Knock Down, by '{A1}'"),
    line(31, f"Player {A1} Dead, killed by {B1}"),
    line(33, f"Player {B2} Dead, killed by {A1}"),     # A1 dead, NOT out for good: the game credits A1
    line(40, f"Revive Player {A1}, revivePosition=(1.00, 2.00, 3.00)"),
    # B1 knocks A1, B1 leaves for good, A1 bleeds out: nobody's kill
    line(50, f"Player '{A1}' Knock Down, by '{B1}'"),
    line(51, f"Player {B1} Dead, killed by {A2}"),
    line(51, f"Player quit revive, {B1}"),
    line(55, f"Player {A1} Dead, killed by {B1}"),
    line(60, "SendLogEndGame  matchend matchid = 900001 / user id = 1"),
])

# the official result of the same game: A1 2 kills (B1 at 12 s, B2 at 33 s), A2 1, B2 1, B1 1 (A1 at 31 s)
RESULT = """TeamName: CREED   Rank: 1   KillScore: 3   RankScore: 12   TotalScore: 15
NAME: P17   ID: 6643413913   KILL: 2
NAME: P18   ID: 6152193352   KILL: 1
TeamName: SAINT   Rank: 2   KillScore: 2   RankScore: 9   TotalScore: 11
NAME: P33   ID: 1111111111   KILL: 1
NAME: P34   ID: 2222222222   KILL: 1
"""
RESULT_NAME = "MatchResult_900001_2026-10-20-19-01-00.log"


def debugger_file(text, name="debugger-2026-10-20T18-59-00.log"):
    return {"id": gf.new_file_id(), "name": name, "kind": "debugger", "pieces": gf.read_debugger(text)}


class ParserTests(TestCase):
    def setUp(self):
        self.piece = gf.read_debugger("menu noise\n" + GAME)[0]
        self.stats, self.feed = gf.game_stats(self.piece["players"], self.piece["events"])
        self.by_uid = {s["uid"]: s for s in self.stats.values()}

    def test_the_match_id_comes_from_the_end_of_the_game(self):
        self.assertEqual(self.piece["match_id"], "900001")
        self.assertEqual(len(self.piece["players"]), 4)

    def test_a_knock_is_the_knock_line_and_a_trace_on_a_direct_kill_is_not(self):
        self.assertEqual(self.by_uid[UIDS[A1]]["knockdowns"], 2)
        self.assertEqual(self.by_uid[UIDS[B1]]["knockdowns"], 1)
        self.assertEqual(self.by_uid[UIDS[B2]]["knockdowns"], 0, "the trace on B2's direct kill is no knock")
        self.assertEqual(self.by_uid[UIDS[A1]]["headshots"], 1)
        knock = next(e for e in self.feed if e["kind"] == "knock")
        self.assertTrue(knock["headshot"], "the trace before the knock says headshot")

    def test_a_bleed_out_after_the_knocker_left_for_good_is_nobodys_kill(self):
        self.assertEqual(self.by_uid[UIDS[B1]]["kills"], 1, "A1 at 31 s only; A1's bleed-out at 55 s is not B1's")
        self.assertEqual(self.by_uid[UIDS[A1]]["deaths"], 2)

    def test_a_knocker_who_is_only_dead_still_gets_the_bleed_out(self):
        self.assertEqual(self.by_uid[UIDS[A1]]["kills"], 2)

    def test_the_debugger_agrees_with_the_result_file(self):
        item = {"debugger": dict(self.piece, gaps=[]), "result": gf.read_result(RESULT, RESULT_NAME)}
        diffs, kills = gf.compare(item)
        self.assertEqual(diffs, [])
        self.assertEqual(kills, {"result_kills": 5, "debugger_kills": 5})

    def test_the_result_file_is_read_with_its_match_id_and_time(self):
        r = gf.read_result(RESULT, RESULT_NAME)
        self.assertEqual((r["match_id"], r["played_at"]), ("900001", "2026-10-20T19:01:00"))
        self.assertEqual([t["rank"] for t in r["teams"]], [1, 2])

    def test_files_are_told_apart_by_what_they_hold(self):
        self.assertEqual(gf.looks_like("x.txt", RESULT), "result")
        self.assertEqual(gf.looks_like("x.txt", GAME), "debugger")
        self.assertIsNone(gf.looks_like("photo.png", "nothing here"))

    def test_a_gzip_upload_is_unpacked(self):
        self.assertEqual(gf.unpack_upload(gzip.compress(GAME.encode("utf-8"))), GAME)


class GamesTests(TestCase):
    def test_a_restarted_client_is_joined_into_one_game(self):
        lines = GAME.split("\n")
        cut = lines.index(line(31, f"Player {A1} Dead, killed by {B1}"))
        first = "\n".join(lines[:cut])                                   # the client stops at 30 s
        second = "\n".join([line(45, "EventTypeEnterGame"), *joins(46)] + lines[cut + 4:])  # back at 45 s
        items = gf.build_games([debugger_file(first, "debugger-a.log"), debugger_file(second, "debugger-b.log")])
        self.assertEqual(len(items), 1)
        d = items[0]["debugger"]
        self.assertEqual(d["match_id"], "900001")
        self.assertEqual([s["file"] for s in d["sources"]], ["debugger-a.log", "debugger-b.log"])
        self.assertEqual(d["gaps"], [15])
        self.assertEqual(items[0]["key"], "900001")

    def test_a_lobby_that_never_played_is_dropped_and_other_players_are_not_joined(self):
        lobby = "\n".join([line(0, "EventTypeEnterGame", hour=18),
                           line(1, "Player Join, 5555555555, 16777217, LOBBY1,False1", hour=18)])
        items = gf.build_games([debugger_file(lobby, "debugger-a.log"), debugger_file(GAME)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["debugger"]["sources"][0]["file"], "debugger-2026-10-20T18-59-00.log")

    def test_a_game_and_its_result_file_pair_by_match_id(self):
        res = {"id": "r1", "name": RESULT_NAME, "kind": "result", "result": gf.read_result(RESULT, RESULT_NAME)}
        old = {"id": "r2", "name": "MatchResult_800001_2026-09-17-19-10-09.log", "kind": "result",
               "result": gf.read_result(RESULT, "MatchResult_800001_2026-09-17-19-10-09.log")}
        items = gf.build_games([res, debugger_file(GAME), old])
        self.assertEqual([i["key"] for i in items], ["800001", "900001"], "in time order")
        self.assertIsNone(items[0]["debugger"])
        self.assertEqual(items[1]["result"]["file_name"], RESULT_NAME)

    def test_choosing_the_debugger_moves_the_player_and_the_team_kills(self):
        changed = RESULT.replace("ID: 6643413913   KILL: 2", "ID: 6643413913   KILL: 3").replace(
            "KillScore: 3", "KillScore: 4")
        item = {"debugger": dict(gf.read_debugger(GAME)[0], gaps=[]), "result": gf.read_result(changed, RESULT_NAME)}
        diffs, _ = gf.compare(item)
        self.assertEqual([(d["scope"], d["result"], d["debugger"]) for d in diffs], [("team", 4, 3), ("player", 3, 2)])
        text, err = gf.game_result_text(item, keep_debugger=["6643413913"])
        self.assertIsNone(err)
        self.assertIn("KillScore: 3", text)
        self.assertIn("ID: 6643413913   KILL: 2", text)
        self.assertEqual(gf.game_result_text(item)[0], changed, "no choice: the result file as it is")

    def test_a_game_with_no_result_file_needs_one_placement_per_team(self):
        item = {"debugger": dict(gf.read_debugger(GAME)[0], gaps=[]), "result": None}
        self.assertEqual(gf.game_result_text(item, placements={"1": 1})[1], "placement_missing")
        self.assertEqual(gf.game_result_text(item, placements={"1": 1, "2": 1})[1], "placements_not_one_each")
        text, err = gf.game_result_text(item, placements={"1": 2, "2": 1}, team_names={"1": "CREED"})
        self.assertIsNone(err)
        self.assertTrue(text.startswith("TeamName: Team 2   Rank: 1   KillScore: 2"))
        self.assertIn("TeamName: CREED   Rank: 2   KillScore: 3", text)


class EndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(username="gfadmin", email="gfadmin@x.com", full_name="GF Admin",
                                         role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="gf-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer gf-admin-token")
        day = datetime.date(2026, 10, 20)
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="duo", event_type="internal",
            max_teams_or_players=16, event_name="Game Files Cup", event_mode="virtual", start_date=day,
            end_date=day, registration_open_date=day, registration_end_date=day, prizepool="0",
            event_rules="r", event_status="ongoing", registration_link="https://x.com/r", number_of_stages=1,
            creator=self.admin)
        stage = Stages.objects.create(event=self.event, stage_name="Groups", start_date=day, end_date=day,
                                      number_of_groups=1, stage_format="br - normal",
                                      teams_qualifying_from_stage=2, stage_order=1)
        group = StageGroups.objects.create(stage=stage, group_name="A", playing_date=day,
                                           playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=2)
        lb = Leaderboard.objects.create(leaderboard_name="A", event=self.event, stage=stage, group=group,
                                        creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
                                        leaderboard_method="manual")
        self.matches = [Match.objects.create(leaderboard=lb, group=group, match_number=n, match_map="bermuda",
                                             scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})
                        for n in (1, 2)]
        for name, eids in (("CREED", (A1, A2)), ("SAINT", (B1, B2))):
            team = Team.objects.create(team_name=name, team_tag=name[:3], join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin, country="NG")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            StageGroupCompetitor.objects.create(stage_group=group, tournament_team=tt)
            for e in eids:
                u = User.objects.create(username=f"p{e}", email=f"p{e}@x.com", full_name=e, role="player",
                                        password="x", uid=UIDS[e])
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)

    def url(self, step):
        return f"/events/{self.event.event_id}/game-files/{step}/"

    def read(self, name, content):
        resp = self.client.post(self.url("read"), {"file": SimpleUploadedFile(name, content)}, format="multipart")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_strangers_are_refused(self):
        anon = APIClient()
        resp = anon.post(self.url("read"), {"file": SimpleUploadedFile("a.log", GAME.encode())}, format="multipart")
        self.assertEqual(resp.status_code, 401)

    def test_the_whole_flow_saves_the_result_and_what_only_the_debugger_knows(self):
        dbg = self.read("debugger-2026-10-20T18-59-00.log", gzip.compress(GAME.encode("utf-8")))
        res = self.read(RESULT_NAME, RESULT.encode("utf-8"))
        self.assertEqual((dbg["kind"], dbg["games"], res["kind"], res["match_id"]), ("debugger", 1, "result", "900001"))
        ids = [dbg["file_id"], res["file_id"]]
        games = self.client.post(self.url("games"), {"file_ids": ids}, format="json").json()
        g = games["games"][0]
        self.assertEqual((g["key"], g["state"], g["match_id"], g["on_roster"], g["diffs"]),
                         ("900001", "ok", self.matches[0].match_id, 4, []))
        self.assertEqual(len(games["slots"]), 2)
        text = self.client.post(self.url("result-text"), {"file_ids": ids, "key": "900001"}, format="json").json()
        self.assertEqual(text["filename"], RESULT_NAME)
        up = self.client.post("/events/upload-team-match-result/", {
            "match_id": self.matches[0].match_id,
            "file": SimpleUploadedFile(text["filename"], text["text"].encode("utf-8"))}, format="multipart")
        self.assertEqual(up.status_code, 200, up.content)
        done = self.client.post(self.url("debugger"), {"file_ids": ids, "key": "900001",
                                                       "match_id": self.matches[0].match_id}, format="json").json()
        self.assertEqual(done, {"updated_rows": 4, "kill_events": 8}, "3 knocks + 5 kills; the 55 s bleed-out is nobody's")
        a1 = TournamentPlayerMatchStats.objects.get(team_stats__match=self.matches[0], player__uid=UIDS[A1])
        self.assertEqual((a1.knockdowns, a1.deaths, a1.revives_received, a1.rich_stats_filled), (2, 2, 1, True))
        self.assertEqual(MatchKillEvent.objects.filter(match=self.matches[0], kind="knock").count(), 3)

    def test_the_same_game_uploaded_again_goes_back_to_its_match(self):
        from afc_tournament_and_scrims.models import MatchResultLog
        from django.core.files.base import ContentFile

        log = MatchResultLog(match=self.matches[1], file_name=RESULT_NAME)
        log.file.save(RESULT_NAME, ContentFile(RESULT.encode("utf-8")), save=True)
        Match.objects.filter(pk=self.matches[1].pk).update(result_inputted=True)
        dbg = self.read("debugger-2026-10-20T18-59-00.log", GAME.encode("utf-8"))
        g = self.client.post(self.url("games"), {"file_ids": [dbg["file_id"]]}, format="json").json()["games"][0]
        self.assertEqual((g["match_id"], g["saved_before"]), (self.matches[1].match_id, True))

    def test_a_game_before_the_event_is_set_to_dont_use(self):
        early = GAME.replace("2026-10-20", "2026-09-17")
        dbg = self.read("debugger-2026-09-17T18-00-00.log", early.encode("utf-8"))
        g = self.client.post(self.url("games"), {"file_ids": [dbg["file_id"]]}, format="json").json()["games"][0]
        self.assertEqual((g["state"], g["match_id"], g["checks"]), ("skip", None, ["before_event"]))

    def test_a_game_with_no_result_file_lists_its_teams_for_placements(self):
        dbg = self.read("debugger-2026-10-20T18-59-00.log", GAME.encode("utf-8"))
        g = self.client.post(self.url("games"), {"file_ids": [dbg["file_id"]]}, format="json").json()["games"][0]
        self.assertEqual(g["state"], "look")
        self.assertEqual([(t["team"], t["name"], t["kills"]) for t in g["teams"]], [("1", "CREED", 3), ("2", "SAINT", 2)])
        resp = self.client.post(self.url("result-text"), {"file_ids": [dbg["file_id"]], "key": "900001",
                                                          "placements": {"1": 1}}, format="json")
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "placement_missing"))

    def test_an_expired_file_and_a_non_game_file_are_coded(self):
        resp = self.client.post(self.url("games"), {"file_ids": ["abc123"]}, format="json")
        self.assertEqual((resp.status_code, resp.json()["code"]), (410, "game_files_expired"))
        resp = self.client.post(self.url("read"), {"file": SimpleUploadedFile("notes.txt", b"hello")}, format="multipart")
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "not_a_game_file"))
