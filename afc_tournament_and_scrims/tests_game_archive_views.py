"""Game file archive, the doors (owner 2026-11-18, inbox #603 #606 #611).

views_game_archive.py: AFC Capture sends (check, send) with the event's upload token; AFC admins and the event's
organizers list, page and download. The result upload and the Game files box keep what they already receive.
Every refusal carries a code. The archive never fails a result. Plan: tasks/game-archive-plan-2026-11-18.md,
Task 2.
"""
import datetime
import gzip
import hashlib
import io
from unittest import mock

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import game_archive as ga
from afc_tournament_and_scrims import views_game_archive as vga
from afc_tournament_and_scrims.models import (
    EventUploadToken, GameFile, MatchResultLog, TournamentTeam, TournamentTeamMember,
)
from afc_tournament_and_scrims.tests_game_archive import GB, ArchiveTestBase, DiskUsage, make_event
from afc_tournament_and_scrims.tests_game_files import GAME

RESULT = ("TeamName: ALPHA  Rank: 1  KillScore: 3  RankScore: 12  TotalScore: 15\n"
          "NAME: A-ONE  ID: 5001  KILL: 3\n")


def bearer(user, value):
    SessionToken.objects.create(user=user, token=value,
                                expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
    return f"Bearer {value}"


class ArchiveViewTests(ArchiveTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.admin_auth = bearer(self.admin, "ga-admin-token")
        self.upload = EventUploadToken.objects.create(event=self.event, created_by=self.admin, label="Observer PC")
        self.base = f"/events/{self.event.event_id}/archive/"

    # ── helpers ──
    def send(self, name, data, *, token=None, sha=None, gz=True, **extra):
        body = gzip.compress(data) if gz else data
        f = SimpleUploadedFile("blob.gz", body, content_type="application/gzip")
        payload = {"file": f, "name": name, "sha256": sha or hashlib.sha256(data).hexdigest(), **extra}
        return self.client.post(self.base + "files/", data=payload,
                                HTTP_X_UPLOAD_TOKEN=token if token is not None else self.upload.token)

    def get(self, path, auth=None, **headers):
        return self.client.get(self.base + path, HTTP_AUTHORIZATION=auth or self.admin_auth, **headers)

    def team_with_player(self):
        team = Team.objects.create(team_name="ALPHA", team_tag="ALP", join_settings="open", team_creator=self.admin,
                                   team_owner=self.admin, country="NG")
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
        u = User.objects.create(username="a_one", email="a_one@x.com", full_name="A One", role="player",
                                password="x", uid="5001")
        TournamentTeamMember.objects.create(tournament_team=tt, user=u)

    def upload_result(self, name):
        f = SimpleUploadedFile(name, RESULT.encode("utf-8"), content_type="text/plain")
        return self.client.post("/events/upload-team-match-result/",
                                data={"match_id": self.maps[0].match_id, "file": f},
                                HTTP_AUTHORIZATION=self.admin_auth)

    # ── sending (AFC Capture) ──
    def test_token_sends_and_dedupes(self):
        r1 = self.send("SafeZone_777_2026-11-06-21-03-00.log", b"65,136")
        self.assertEqual(r1.status_code, 200, r1.content)
        self.assertEqual((r1.data["result"], r1.data["kind"]), ("stored", "safezone"))
        r2 = self.send("SafeZone_777_2026-11-06-21-03-00.log", b"65,136")
        self.assertEqual(r2.data["result"], "duplicate")
        self.assertEqual(r2.data["file_id"], r1.data["file_id"])
        row = GameFile.objects.get()
        self.assertEqual((row.source, row.uploaded_by_id), ("capture", self.admin.pk))

    def test_check_returns_stored(self):
        self.send("SafeZone_777_x.log", b"65,136")
        known, unknown = hashlib.sha256(b"65,136").hexdigest(), "f" * 64
        r = self.client.post(self.base + "files/check/", data={"sha256": [known, unknown]}, format="json",
                             HTTP_X_UPLOAD_TOKEN=self.upload.token)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["stored"], [known])
        bad = self.client.post(self.base + "files/check/", data={"sha256": ["nothex"]}, format="json",
                               HTTP_X_UPLOAD_TOKEN=self.upload.token)
        self.assertEqual((bad.status_code, bad.data["code"]), (400, "archive_bad_check"))

    def test_token_of_other_event_refused(self):
        other_event, _, _, _ = make_event(self.admin, name="Other Cup")
        other = EventUploadToken.objects.create(event=other_event, created_by=self.admin)
        r = self.send("SafeZone_777_x.log", b"zone", token=other.token)
        self.assertEqual((r.status_code, r.data["code"]), (403, "invalid_revoked_upload_token"))
        self.upload.revoked = True
        self.upload.save()
        r = self.send("SafeZone_777_x.log", b"zone")
        self.assertEqual((r.status_code, r.data["code"]), (403, "invalid_revoked_upload_token"))
        self.assertFalse(GameFile.objects.exists())

    def test_token_cannot_list_or_download(self):
        sent = self.send("SafeZone_777_x.log", b"zone")
        for path in ("", "files/", f"files/{sent.data['file_id']}/download/"):
            r = self.client.get(self.base + path, HTTP_X_UPLOAD_TOKEN=self.upload.token)
            self.assertEqual((r.status_code, r.data["code"]), (403, "archive_token_send_only"), path)

    def test_name_not_allowed_and_sha_mismatch(self):
        r = self.send("GuestCacheProduction.json", b'{"uid":1,"password":"x"}')
        self.assertEqual((r.status_code, r.data["code"]), (400, "archive_name_not_allowed"))
        r = self.send("SafeZone_777_x.log", b"zone", sha="0" * 64)
        self.assertEqual((r.status_code, r.data["code"]), (400, "archive_sha_mismatch"))
        self.assertFalse(GameFile.objects.exists())

    def test_plain_bytes_accepted(self):
        r = self.send("SafeZone_777_x.log", b"zone", gz=False)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(GameFile.objects.get().size_bytes, 4)

    def test_stage_group_must_belong_to_event(self):
        _, other_stage, other_group, _ = make_event(self.admin, name="Other Cup")
        r = self.send("SafeZone_777_x.log", b"zone", stage=other_stage.stage_id, group=other_group.group_id)
        self.assertEqual((r.status_code, r.data["code"]), (400, "archive_bad_stage_group"))
        ok = self.send("SafeZone_777_x.log", b"zone", stage=self.stage.stage_id, group=self.group.group_id)
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(GameFile.objects.get().group_id, self.group.group_id)

    def test_space_low_refuses_archive_not_result(self):
        self.team_with_player()
        with mock.patch.object(ga.shutil, "disk_usage", return_value=DiskUsage(300 * GB, 291 * GB, 9 * GB)):
            r = self.send("SafeZone_777_x.log", b"zone")
            self.assertEqual((r.status_code, r.data["code"]), (503, "archive_space_low"))
            res = self.upload_result("MatchResult_777_2026-11-18-20-00-00.log")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(GameFile.objects.exists())

    def test_rate_limited(self):
        with mock.patch.object(vga, "SENDS_PER_MINUTE", 2):
            codes = [self.send(f"SafeZone_77{i}_x.log", b"zone%d" % i).status_code for i in range(3)]
        self.assertEqual(codes[:2], [200, 200])
        self.assertEqual(codes[2], 429)

    # ── the two existing doors ──
    def test_result_upload_archives_and_links_waiting(self):
        self.team_with_player()
        replay = self.send("Replays/ReplayInfo_777_2026-11-18-19-50-00.bin", b"\x00replay")
        self.assertEqual(replay.status_code, 200)
        res = self.upload_result("MatchResult_777_2026-11-18-20-00-00.log")
        self.assertEqual(res.status_code, 200, res.content)
        result_row = GameFile.objects.get(kind="result")
        self.assertEqual(result_row.source, "result_upload")
        self.assertEqual(list(result_row.matches.values_list("pk", flat=True)), [self.maps[0].pk])
        replay_row = GameFile.objects.get(kind="replay_bin")
        self.assertEqual(list(replay_row.matches.values_list("pk", flat=True)), [self.maps[0].pk])

    def test_result_upload_survives_archive_failure(self):
        self.team_with_player()
        with mock.patch.object(ga, "store_bytes", side_effect=RuntimeError("disk on fire")):
            with self.assertLogs("afc_tournament_and_scrims", level="ERROR"):
                res = self.upload_result("MatchResult_777_2026-11-18-20-00-00.log")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(MatchResultLog.objects.count(), 1)
        self.assertFalse(GameFile.objects.exists())

    def test_box_read_archives(self):
        f = SimpleUploadedFile("evening.log", GAME.encode("utf-8"), content_type="text/plain")
        r = self.client.post(f"/events/{self.event.event_id}/game-files/read/", data={"file": f, "name": "evening.log"},
                             HTTP_AUTHORIZATION=self.admin_auth)
        self.assertEqual(r.status_code, 200, r.content)
        row = GameFile.objects.get()
        self.assertEqual((row.kind, row.source, row.original_name), ("debugger", "upload_box", "evening.log"))
        self.assertEqual(row.game_match_ids, ["900001"])
        self.assertEqual(row.uploaded_by_id, self.admin.pk)

    # ── reading (the Archive tab) ──
    def test_tree_counts(self):
        MatchResultLog.objects.create(match=self.maps[0], file=ContentFile(b"x", name="MatchResult_777_a.log"),
                                      file_name="MatchResult_777_a.log")
        self.send("SafeZone_777_x.log", b"zone")             # map 1
        self.send("SafeZone_888_x.log", b"zone2")            # nobody's yet
        r = self.get("")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["totals"], {"files": 2, "bytes": 9})
        self.assertEqual(r.data["unmatched"], {"files": 1, "bytes": 5})
        group = r.data["stages"][0]["groups"][0]
        self.assertEqual((group["files"], group["bytes"]), (1, 4))
        self.assertEqual([(m["map_number"], m["files"]) for m in group["maps"]], [(1, 1), (2, 0)])
        self.assertIn("refusing", r.data["space"])

    def test_files_paginated(self):
        for i in range(60):
            self.send(f"SafeZone_{900 + i}_x.log", b"zone%d" % i)
        r = self.get("files/?unmatched=1&limit=50")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((len(r.data["results"]), r.data["total_count"], r.data["has_more"], r.data["next_offset"]),
                         (50, 60, True, 50))
        r2 = self.get("files/?unmatched=1&limit=50&offset=50")
        self.assertEqual((len(r2.data["results"]), r2.data["has_more"]), (10, False))
        first = r.data["results"][0]
        self.assertEqual(set(first), {"file_id", "kind", "name", "size_bytes", "created_at", "source",
                                      "game_match_id"})

    def test_files_filter_by_map(self):
        MatchResultLog.objects.create(match=self.maps[0], file=ContentFile(b"x", name="MatchResult_777_a.log"),
                                      file_name="MatchResult_777_a.log")
        self.send("SafeZone_777_x.log", b"zone")
        self.send("SafeZone_888_x.log", b"zone2")
        r = self.get(f"files/?match={self.maps[0].match_id}")
        self.assertEqual([x["name"] for x in r.data["results"]], ["SafeZone_777_x.log"])
        _, _, _, other_maps = make_event(self.admin, name="Other Cup")
        bad = self.get(f"files/?match={other_maps[0].match_id}")
        self.assertEqual((bad.status_code, bad.data["code"]), (404, "archive_scope_not_found"))

    def test_download_original_bytes_and_headers(self):
        data = b"\x00\x01replay" * 100
        sent = self.send("Replays/ReplayInfo_777_2026-11-18-19-50-00.bin", data)
        r = self.get(f"files/{sent.data['file_id']}/download/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), data)
        self.assertEqual(r["Content-Type"], "application/octet-stream")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertIn('attachment; filename="ReplayInfo_777_2026-11-18-19-50-00.bin"', r["Content-Disposition"])

    def test_download_other_event_file_same_404(self):
        other_event, _, _, _ = make_event(self.admin, name="Other Cup")
        theirs, _ = ga.store_bytes(other_event, "SafeZone_777_x.log", b"zone", source="capture")
        r1 = self.get(f"files/{theirs.token}/download/")
        r2 = self.get(f"files/{'0' * 32}/download/")
        self.assertEqual((r1.status_code, r1.data["code"]), (404, "archive_file_not_found"))
        self.assertEqual((r2.status_code, r2.data["code"]), (404, "archive_file_not_found"))

    def test_signed_in_non_member_refused_everywhere(self):
        player = User.objects.create(username="gaplayer", email="gaplayer@x.com", full_name="P", role="player",
                                     password="x")
        auth = bearer(player, "ga-player-token")
        sent = self.send("SafeZone_777_x.log", b"zone")
        for path in ("", "files/", f"files/{sent.data['file_id']}/download/"):
            r = self.get(path, auth=auth)
            self.assertEqual((r.status_code, r.data["code"]), (403, "not_permission_event"), path)
        r = self.client.post(self.base + "files/check/", data={"sha256": []}, format="json",
                             HTTP_AUTHORIZATION=auth)
        self.assertEqual(r.status_code, 403)

    def test_anonymous_refused(self):
        for path in ("", "files/", f"files/{'0' * 32}/download/"):
            r = self.client.get(self.base + path)
            self.assertEqual(r.status_code, 401, path)
        r = self.client.post(self.base + "files/check/", data={"sha256": []}, format="json")
        self.assertEqual(r.status_code, 401)

    # ── backfill ──
    def test_backfill_command_idempotent(self):
        MatchResultLog.objects.create(match=self.maps[1], file=ContentFile(RESULT.encode(), name="MatchResult_555_b.log"),
                                      file_name="MatchResult_555_b.log", uploaded_by=self.admin)
        out = io.StringIO()
        call_command("archive_result_logs", stdout=out)
        call_command("archive_result_logs", stdout=out)
        row = GameFile.objects.get()
        self.assertEqual((row.kind, row.source, row.game_match_id), ("result", "backfill", "555"))
        self.assertEqual(list(row.matches.values_list("pk", flat=True)), [self.maps[1].pk])
