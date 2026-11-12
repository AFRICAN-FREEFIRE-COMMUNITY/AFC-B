"""The Game files box reads a debugger log with AFC Capture's own reader (owner 2026-11-11, inbox #541).

"when you upload a debugger file, you should be able to get all stats that capture wold have given ypou and if not,
the site should request for all files it needs for upload."

Pinned here: the copied reader is byte for byte Capture's (sha256 of each file, LF); a log that reaches the map's end
is applied through capture_apply (Capture's payload: knocked, grenades, gloo walls, medkits, the gun, the kill feed),
a flagged (off-roster) player keeps his numbers on MatchKillFlag.stats; each game lists the files still missing by
their real names; a debugger log dropped on the per-map result upload is told where it goes, with a code.
"""
import hashlib
import os

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from afc_tournament_and_scrims import capture_reader
from afc_tournament_and_scrims.models import MatchKillEvent, MatchKillFlag, TournamentPlayerMatchStats
from afc_tournament_and_scrims import tests_game_files as _tgf
from afc_tournament_and_scrims.tests_game_files import A1, B1, GAME, RESULT, RESULT_NAME, UIDS, line

# The same game with the two lines AFC Capture takes as the map's end (the game prints both when a map is over:
# MatchEnd_Res, then CompleteGame carrying the match id; real format, as in afc-capture/dev/fixture_lines.py).
GAME_ENDED = GAME.replace(
    line(60, "SendLogEndGame  matchend matchid = 900001 / user id = 1"),
    line(59, "[MatchResult] receive S2C_RUDP_MatchEnd_Res from GS") + "\n"
    + line(60, "SendLogEndGame  matchend matchid = 900001 / user id = 1") + "\n"
    + line(61, 'SendEventLog: EventTypeCompleteGame {"match_id":900001,"region":"ME"}'),
)


class CaptureReaderCopyTests(TestCase):
    def test_the_copy_is_capture_154(self):
        folder = os.path.dirname(capture_reader.__file__)
        for name, want in capture_reader.SOURCE_SHA256.items():
            with open(os.path.join(folder, name), "rb") as fh:
                got = hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()
            self.assertEqual(got, want, f"{name} differs from AFC Capture {capture_reader.SOURCE_VERSION}")
        self.assertEqual(len(capture_reader.SOURCE_SHA256), 5)


class CaptureReaderFlowTests(TestCase):
    # The Game files endpoint fixture, borrowed (not inherited, so its own tests do not run twice here).
    setUp = _tgf.EndpointTests.setUp
    url = _tgf.EndpointTests.url
    read = _tgf.EndpointTests.read

    def _upload_result(self, ids, key="900001"):
        text = self.client.post(self.url("result-text"), {"file_ids": ids, "key": key}, format="json").json()
        up = self.client.post("/events/upload-team-match-result/", {
            "match_id": self.matches[0].match_id,
            "file": SimpleUploadedFile(text["filename"], text["text"].encode("utf-8"))}, format="multipart")
        self.assertEqual(up.status_code, 200, up.content)

    def test_a_finished_map_gets_what_capture_sends(self):
        dbg = self.read("debugger-2026-10-20T18-59-00.log", GAME_ENDED.encode("utf-8"))
        res = self.read(RESULT_NAME, RESULT.encode("utf-8"))
        ids = [dbg["file_id"], res["file_id"]]
        g = self.client.post(self.url("games"), {"file_ids": ids}, format="json").json()["games"][0]
        self.assertTrue(g["capture_ready"])
        self.assertEqual(g["files"], {"debugger": True, "result": True, "replay": False})
        self.assertEqual(g["missing"], [{"kind": "replay", "hint": "Replays/ReplayInfo_900001_<date>.bin"}])
        self._upload_result(ids)
        done = self.client.post(self.url("debugger"), {"file_ids": ids, "key": "900001",
                                                       "match_id": self.matches[0].match_id}, format="json").json()
        self.assertEqual(done["reader"], "capture")
        self.assertEqual(done["updated_rows"], 4)
        a1 = TournamentPlayerMatchStats.objects.get(team_stats__match=self.matches[0], player__uid=UIDS[A1])
        self.assertEqual((a1.knockdowns, a1.rich_stats_filled, a1.rich_stats_source), (2, True, "capture"))
        self.assertTrue(MatchKillEvent.objects.filter(match=self.matches[0], kind="knock").exists())

    def test_a_flagged_player_keeps_his_numbers(self):
        # B1's account loses its UID on the site: the result upload flags him; Capture's numbers land on the flag.
        from afc_auth.models import User
        User.objects.filter(uid=UIDS[B1]).update(uid="9999999999")
        dbg = self.read("debugger-2026-10-20T18-59-00.log", GAME_ENDED.encode("utf-8"))
        res = self.read(RESULT_NAME, RESULT.encode("utf-8"))
        ids = [dbg["file_id"], res["file_id"]]
        self._upload_result(ids)
        self.client.post(self.url("debugger"), {"file_ids": ids, "key": "900001",
                                                "match_id": self.matches[0].match_id}, format="json")
        flag = MatchKillFlag.objects.get(match=self.matches[0], uid=UIDS[B1])
        self.assertIn("knockdowns", flag.stats)
        self.assertEqual(flag.stats["knockdowns"], 1)

    def test_no_result_file_asks_for_it_and_offers_the_logs_placements(self):
        dbg = self.read("debugger-2026-10-20T18-59-00.log", GAME_ENDED.encode("utf-8"))
        g = self.client.post(self.url("games"), {"file_ids": [dbg["file_id"]]}, format="json").json()["games"][0]
        self.assertEqual([m["kind"] for m in g["missing"]], ["result", "replay"])
        self.assertEqual(g["missing"][0]["hint"], "MatchResult_900001_<date>.log")

    def test_a_log_that_never_ends_falls_back_to_the_site_reader(self):
        dbg = self.read("debugger-2026-10-20T18-59-00.log", GAME.encode("utf-8"))
        res = self.read(RESULT_NAME, RESULT.encode("utf-8"))
        ids = [dbg["file_id"], res["file_id"]]
        self._upload_result(ids)
        done = self.client.post(self.url("debugger"), {"file_ids": ids, "key": "900001",
                                                       "match_id": self.matches[0].match_id}, format="json").json()
        self.assertEqual((done["reader"], done["updated_rows"]), ("site", 4))

    def test_a_damaged_replay_is_refused_with_a_code(self):
        resp = self.client.post(self.url("read"), {"file": SimpleUploadedFile(
            "ReplayInfo_900001_2026-10-20-19-00-00.bin", b"not a replay at all")}, format="multipart")
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "replay_file_unreadable"))

    def test_a_debugger_log_on_the_map_upload_is_told_where_it_goes(self):
        up = self.client.post("/events/upload-team-match-result/", {
            "match_id": self.matches[0].match_id,
            "file": SimpleUploadedFile("debugger-2026-10-20T18-59-00.log", GAME.encode("utf-8"))}, format="multipart")
        self.assertEqual((up.status_code, up.json()["code"]), (400, "game_log_not_result"))
