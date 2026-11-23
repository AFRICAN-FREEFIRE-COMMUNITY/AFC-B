"""Game file archive, zips (owner 2026-11-18, inbox #603: "download any at anytime or all together").

A zip of an event, stage, group, map or the "not matched" pile is built in the background (202, then poll), holds
the files that existed when it was asked for, unpacked to their original names under Stage/Group/Map N/, and
is gone after 24 hours. Plan: tasks/game-archive-plan-2026-11-18.md, Task 3.
"""
import io
import os
import time
import zipfile
from unittest import mock

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.conf import settings
from rest_framework.test import APIClient

from afc_tournament_and_scrims import game_archive as ga
from afc_tournament_and_scrims import tasks
from afc_tournament_and_scrims import views_game_archive as vga
from afc_tournament_and_scrims.models import EventUploadToken, MatchResultLog
from afc_tournament_and_scrims.tests_game_archive import ArchiveTestBase, make_event
from afc_tournament_and_scrims.tests_game_archive_views import bearer


class ArchiveZipTests(ArchiveTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=bearer(self.admin, "gz-admin-token"))
        self.base = f"/events/{self.event.event_id}/archive/zips/"
        MatchResultLog.objects.create(match=self.maps[0], file=ContentFile(b"x", name="MatchResult_777_a.log"),
                                      file_name="MatchResult_777_a.log")
        ga.store_bytes(self.event, "SafeZone_777_x.log", b"zone one", source="capture")
        ga.store_bytes(self.event, "SafeZone_888_x.log", b"zone two", source="capture")

    def ask(self, **body):
        return self.client.post(self.base, data=body, format="json")

    def read_zip(self, zip_id):
        r = self.client.get(f"{self.base}{zip_id}/download/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertIn("attachment;", r["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(b"".join(r.streaming_content))) as z:
            return {n: z.read(n) for n in z.namelist()}

    def test_zip_request_202_and_ready(self):
        with mock.patch.object(vga, "_enqueue_zip", side_effect=tasks.build_archive_zip):
            r = self.ask(scope="event")
        self.assertEqual(r.status_code, 202, r.content)
        status = self.client.get(f"{self.base}{r.data['zip_id']}/")
        self.assertEqual(status.data["status"], "ready")
        self.assertNotIn("file_ids", status.data)
        self.assertEqual(self.read_zip(r.data["zip_id"]), {
            "Groups/A/Map 1/SafeZone_777_x.log": b"zone one",
            "not-matched/SafeZone_888_x.log": b"zone two",
        })

    def test_zip_of_one_group_and_unmatched(self):
        with mock.patch.object(vga, "_enqueue_zip", side_effect=tasks.build_archive_zip):
            g = self.ask(scope="group", id=self.group.group_id)
            u = self.ask(scope="unmatched")
        self.assertEqual(list(self.read_zip(g.data["zip_id"])), ["Groups/A/Map 1/SafeZone_777_x.log"])
        self.assertEqual(list(self.read_zip(u.data["zip_id"])), ["not-matched/SafeZone_888_x.log"])

    def test_zip_frozen_at_request(self):
        with mock.patch.object(vga, "_enqueue_zip"):
            r = self.ask(scope="event")
        ga.store_bytes(self.event, "SafeZone_999_x.log", b"late", source="capture")
        tasks.build_archive_zip(r.data["zip_id"])
        self.assertEqual(len(self.read_zip(r.data["zip_id"])), 2)

    def test_zip_scope_checks(self):
        _, other_stage, _, _ = make_event(self.admin, name="Other Cup")
        bad = self.ask(scope="stage", id=other_stage.stage_id)
        self.assertEqual((bad.status_code, bad.data["code"]), (404, "archive_scope_not_found"))
        nonsense = self.ask(scope="everything")
        self.assertEqual((nonsense.status_code, nonsense.data["code"]), (400, "archive_bad_zip"))
        empty = self.ask(scope="match", id=self.maps[1].match_id)
        self.assertEqual((empty.status_code, empty.data["code"]), (400, "archive_zip_empty"))
        token = EventUploadToken.objects.create(event=self.event, created_by=self.admin)
        r = APIClient().post(self.base, data={"scope": "event"}, format="json", HTTP_X_UPLOAD_TOKEN=token.token)
        self.assertEqual((r.status_code, r.data["code"]), (403, "archive_token_send_only"))

    def test_zip_other_event_answers_like_missing(self):
        other_event, _, _, _ = make_event(self.admin, name="Other Cup")
        with mock.patch.object(vga, "_enqueue_zip", side_effect=tasks.build_archive_zip):
            r = self.ask(scope="event")
        zid = r.data["zip_id"]
        for path in (f"/events/{other_event.event_id}/archive/zips/{zid}/",
                     f"/events/{other_event.event_id}/archive/zips/{zid}/download/",
                     f"{self.base}{'0' * 32}/", f"{self.base}{'0' * 32}/download/"):
            resp = self.client.get(path)
            self.assertEqual((resp.status_code, resp.data["code"]), (410, "archive_zip_expired"), path)

    def test_purge_removes_old_only(self):
        folder = os.path.join(settings.MEDIA_ROOT, "game_archive", "_zips")
        os.makedirs(folder, exist_ok=True)
        old, new = os.path.join(folder, "a" * 32 + ".zip"), os.path.join(folder, "b" * 32 + ".zip")
        for p in (old, new):
            with open(p, "wb") as fh:
                fh.write(b"PK")
        day_ago = time.time() - 25 * 3600
        os.utime(old, (day_ago, day_ago))
        self.assertEqual(tasks.purge_archive_zips(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
