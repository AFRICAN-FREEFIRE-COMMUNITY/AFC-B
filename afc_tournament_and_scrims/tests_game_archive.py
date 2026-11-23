"""Game file archive, the store (owner 2026-11-18, inbox #603 #611 "save everything").

game_archive.py keeps every file the game writes for a map, gzipped, once per content (sha256), filed under the
event and linked to the site map(s) it belongs to. These tests pin the name allowlist (the observer's password
file must never be accepted), the dedupe, the debugger log that grows during the evening, the size caps, the
space guard and the two ways a file finds its map. Plan: tasks/game-archive-plan-2026-11-18.md, Task 1.
"""
import datetime
import gzip
import hashlib
import shutil
import tempfile
from collections import namedtuple
from unittest import mock

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from afc_auth.models import User
from afc_tournament_and_scrims import game_archive as ga
from afc_tournament_and_scrims.models import (
    Event, GameFile, Leaderboard, Match, MatchResultLog, StageGroups, Stages,
)
from afc_tournament_and_scrims.tests_game_files import GAME

DiskUsage = namedtuple("DiskUsage", "total used free")
GB = 1024 ** 3


def make_event(admin, name="Archive Cup"):
    """One event, one stage, one group, two maps: the smallest tree a file can be filed in."""
    day = datetime.date(2026, 11, 18)
    event = Event.objects.create(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=12, event_name=name, event_mode="virtual", start_date=day, end_date=day,
        registration_open_date=day, registration_end_date=day, prizepool="0", event_rules="r",
        event_status="ongoing", registration_link="https://x.com/r", number_of_stages=1, creator=admin)
    stage = Stages.objects.create(event=event, stage_name="Groups", start_date=day, end_date=day,
                                  number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
                                  stage_order=1)
    group = StageGroups.objects.create(stage=stage, group_name="A", playing_date=day,
                                       playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=2)
    lb = Leaderboard.objects.create(leaderboard_name="A", event=event, stage=stage, group=group, creator=admin,
                                    placement_points={"1": 12}, kill_point=1.0, leaderboard_method="manual")
    maps = [Match.objects.create(leaderboard=lb, group=group, match_number=n, match_map="bermuda",
                                 scoring_settings={"placement_points": {"1": 12}, "kill_point": 1})
            for n in (1, 2)]
    return event, stage, group, maps


class ArchiveTestBase(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp(prefix="afc-archive-test-")
        self._media = override_settings(MEDIA_ROOT=self.media)
        self._media.enable()
        self.admin = User.objects.create(username="gaadmin", email="gaadmin@x.com", full_name="GA Admin",
                                         role="admin", password="x")
        self.event, self.stage, self.group, self.maps = make_event(self.admin)

    def tearDown(self):
        self._media.disable()
        shutil.rmtree(self.media, ignore_errors=True)


class ClassifyTests(TestCase):
    def test_classify_allowlist(self):
        cases = {
            "MatchResult_2118795595432509440_2026-11-06-21-16-02.log": ("result", "2118795595432509440"),
            "MatchResult_777.log": ("result", "777"),
            "Debugger/debugger-2026-11-18-19-00-01.log": ("debugger", ""),
            "Debugger\\debugger-2026-11-18-19-00-01.log": ("debugger", ""),
            "Replays/ReplayInfo_777_2026-11-06-21-00-10.json": ("replay_info", "777"),
            "Replays/ReplayInfo_777_2026-11-06-21-00-10.bin": ("replay_bin", "777"),
            "ReplayInfo_777_2026-11-06-21-00-10.bin": ("replay_bin", "777"),
            "SafeZone_777_2026-11-06-21-03-00.log": ("safezone", "777"),
            "HeatMap/123456789/hud_click_2026-11-06-21-16-02_p0.bin": ("heatmap", ""),
            "MatchId_777_2026-11-06-21-00-00.log": ("marker", "777"),
            "RoomID_5551234_2026-11-06-20-58-00.log": ("marker", ""),
            # Custom rooms also write this one (UIModelCustomRoom.LogBatchMatchId, read from the client 18 Nov).
            "BatchMatchID_777_2026-11-18-20-00-00.log": ("marker", "777"),
        }
        for name, want in cases.items():
            self.assertEqual(ga.classify(name), want, name)

    def test_classify_refuses_everything_else(self):
        for name in ("GuestCacheProduction.json", "PlayerNameOverwrite.json", "StreamingAssets/x.json",
                     "StreamingAssets/MatchResult_777.log", "MatchResult_.log", "notes.txt",
                     "Debugger/notes.txt", "Replays/ReplayInfo_abc_x.bin", "../MatchResult_777.log"):
            self.assertIsNone(ga.classify(name), name)


class StoreTests(ArchiveTestBase):
    def test_store_dedupes_by_sha(self):
        data = b"zone 65,136\n"
        gf1, r1 = ga.store_bytes(self.event, "SafeZone_777_2026-11-06-21-03-00.log", data, source="capture")
        gf2, r2 = ga.store_bytes(self.event, "SafeZone_777_2026-11-06-21-03-00.log", data, source="capture")
        self.assertEqual((r1, r2), ("stored", "duplicate"))
        self.assertEqual(gf1.pk, gf2.pk)
        self.assertEqual(GameFile.objects.filter(event=self.event).count(), 1)

    def test_store_gzips_and_keeps_name(self):
        data = b"\x00\x01replay bytes" * 1000
        gf, result = ga.store_bytes(self.event, "Replays/ReplayInfo_777_2026-11-06-21-00-10.bin", data,
                                    source="capture", user=self.admin)
        self.assertEqual(result, "stored")
        with gf.file.open("rb") as fh:
            raw = fh.read()
        self.assertEqual(raw[:2], b"\x1f\x8b")
        self.assertEqual(gzip.decompress(raw), data)
        self.assertEqual(gf.original_name, "ReplayInfo_777_2026-11-06-21-00-10.bin")
        self.assertEqual(gf.size_bytes, len(data))
        self.assertEqual(gf.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual((gf.kind, gf.game_match_id, gf.source), ("replay_bin", "777", "capture"))
        self.assertEqual(len(gf.token), 32)

    def test_debugger_growth_replaces_prefix(self):
        name = "Debugger/debugger-2026-11-18-19-00-01.log"
        first = b"a" * 1000
        old, r1 = ga.store_bytes(self.event, name, first, source="capture")
        old_path = old.file.path
        new, r2 = ga.store_bytes(self.event, name, first + b"b" * 500, source="capture")
        self.assertEqual((r1, r2), ("stored", "replaced"))
        self.assertEqual(GameFile.objects.filter(event=self.event, kind="debugger").count(), 1)
        self.assertEqual(new.size_bytes, 1500)
        self.assertFalse(GameFile.objects.filter(pk=old.pk).exists())
        import os
        self.assertFalse(os.path.exists(old_path))
        kept, r3 = ga.store_bytes(self.event, name, first, source="capture")
        self.assertEqual(r3, "kept_longer")
        self.assertEqual(kept.pk, new.pk)
        self.assertEqual(GameFile.objects.filter(event=self.event, kind="debugger").count(), 1)

    def test_debugger_same_name_different_content_kept(self):
        name = "Debugger/debugger-2026-11-18-19-00-01.log"
        ga.store_bytes(self.event, name, b"a" * 1000, source="capture")
        _, result = ga.store_bytes(self.event, name, b"c" * 1500, source="capture")
        self.assertEqual(result, "stored")
        self.assertEqual(GameFile.objects.filter(event=self.event, kind="debugger").count(), 2)

    def test_debugger_game_ids_from_parser(self):
        second = GAME.replace("900001", "900002").replace(" 19:", " 20:")
        text = GAME + "\n" + second + "\n"
        gf, _ = ga.store_bytes(self.event, "Debugger/debugger-2026-10-20-18-59-00.log", text.encode("utf-8"),
                               source="upload_box")
        self.assertEqual(sorted(gf.game_match_ids), ["900001", "900002"])
        self.assertEqual(gf.game_match_id, "")

    def test_caps_and_space(self):
        with self.assertRaises(ga.ArchiveRefused) as too_big:
            ga.store_bytes(self.event, "SafeZone_777_x.log", b"z" * (1024 * 1024 + 1), source="capture")
        self.assertEqual((too_big.exception.code, too_big.exception.status), ("archive_file_too_large", 413))
        with self.assertRaises(ga.ArchiveRefused) as bad_name:
            ga.store_bytes(self.event, "GuestCacheProduction.json", b"{}", source="capture")
        self.assertEqual(bad_name.exception.code, "archive_name_not_allowed")
        with mock.patch.object(ga.shutil, "disk_usage", return_value=DiskUsage(300 * GB, 290 * GB, 10 * GB)):
            with self.assertRaises(ga.ArchiveRefused) as full:
                ga.store_bytes(self.event, "SafeZone_777_x.log", b"zone", source="capture")
            self.assertEqual((full.exception.code, full.exception.status), ("archive_space_low", 503))
            self.assertTrue(ga.space_state()["refusing"])
        self.assertFalse(GameFile.objects.exists())

    def test_space_warning_between_thresholds(self):
        with mock.patch.object(ga.shutil, "disk_usage", return_value=DiskUsage(300 * GB, 270 * GB, 30 * GB)):
            state = ga.space_state()
        self.assertEqual((state["refusing"], state["warning"]), (False, True))
        self.assertAlmostEqual(state["free_gb"], 30.0, places=1)


class LinkTests(ArchiveTestBase):
    def _result_log(self, match, name):
        MatchResultLog.objects.create(match=match, file=ContentFile(b"TeamName: A", name=name), file_name=name)

    def test_link_by_result_log_name(self):
        self._result_log(self.maps[0], "MatchResult_777_2026-11-06-21-16-02.log")
        gf, _ = ga.store_bytes(self.event, "Replays/ReplayInfo_777_2026-11-06-21-00-10.bin", b"bin",
                               source="capture")
        self.assertEqual(list(gf.matches.values_list("pk", flat=True)), [self.maps[0].pk])
        other, _ = ga.store_bytes(self.event, "Replays/ReplayInfo_7777_2026-11-06-21-00-10.bin", b"bin2",
                                  source="capture")
        self.assertEqual(other.matches.count(), 0)

    def test_link_ignores_other_events_maps(self):
        _, _, _, other_maps = make_event(self.admin, name="Other Cup")
        self._result_log(other_maps[0], "MatchResult_777_2026-11-06-21-16-02.log")
        gf, _ = ga.store_bytes(self.event, "SafeZone_777_x.log", b"zone", source="capture")
        self.assertEqual(gf.matches.count(), 0)

    def test_link_waiting(self):
        replay, _ = ga.store_bytes(self.event, "Replays/ReplayInfo_777_2026-11-06-21-00-10.bin", b"bin",
                                   source="capture")
        text = GAME.replace("900001", "777")
        log, _ = ga.store_bytes(self.event, "Debugger/debugger-x.log", text.encode("utf-8"), source="capture")
        self.assertEqual(replay.matches.count(), 0)
        self.assertEqual(ga.link_waiting(self.maps[1], "777"), 2)
        self.assertEqual(list(replay.matches.values_list("pk", flat=True)), [self.maps[1].pk])
        self.assertEqual(list(log.matches.values_list("pk", flat=True)), [self.maps[1].pk])

    def test_store_with_explicit_match(self):
        gf, _ = ga.store_bytes(self.event, "MatchResult_888_2026.log", b"TeamName: A", source="result_upload",
                               match=self.maps[0])
        self.assertEqual(list(gf.matches.values_list("pk", flat=True)), [self.maps[0].pk])
