"""
Task 2.6 — tests for the ASYNC multi-image OCR batch (afc_leaderboard.ocr + the ocr_job_* endpoints).

The batch lets an admin upload several maps, each with one or more screenshots, and read them in the
BACKGROUND (Celery) so the synchronous request can never time out. These tests:
  - merge_placements: several screenshots of ONE map are unioned + de-duped into one ordered list.
  - process_job: the worker body reads each image via the SHARED extractor (mocked at the
    afc_ocr.services.extract boundary — never hits Gemini), merges, matches, stores rows + status.
  - endpoints: create (multipart, many images) → run / run-all (eager Celery) → list (poll) → apply
    (reuses _apply_ocr_rows, so a map + participants + scored results appear) → delete. Plus the
    non-manager 403 gate on every mutation.
"""
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings

from afc_leaderboard.models import (
    StandaloneLeaderboard, LeaderboardParticipant, LeaderboardMatch,
    LeaderboardOcrJob, LeaderboardOcrImage,
)
from afc_leaderboard.ocr import merge_placements, process_job

from ._helpers import make_afc_admin, make_user, make_team, bearer


# A fake team-standings read the mocked extractor returns (placements 1-3, with a team_name + kills).
_FAKE_TEAM_READ = {
    "match_type": "team",
    "placements": [
        {"placement": 1, "team_name": "Alpha", "kills": 5, "players": [{"name": "a1", "kills": 5}]},
        {"placement": 2, "team_name": "Bravo", "kills": 2, "players": [{"name": "b1", "kills": 2}]},
    ],
}


def _img(name="shot.jpg"):
    """A throwaway uploaded file. The bytes are never decoded in these tests (extract is mocked), so
    any content works; the model ImageField stores it without opening it."""
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xe0fakejpegbytes", content_type="image/jpeg")


class MergePlacementsTests(TestCase):
    """merge_placements unions a map's several screenshots, drops exact duplicates, sorts by placement."""

    def test_union_of_two_halves_team(self):
        top = [{"placement": 1, "team_name": "Alpha", "kills": 5}]
        bottom = [{"placement": 2, "team_name": "Bravo", "kills": 1}]
        merged = merge_placements([top, bottom], is_team=True)
        self.assertEqual([m["placement"] for m in merged], [1, 2])

    def test_dedupes_overlap_team(self):
        a = [{"placement": 1, "team_name": "Alpha", "kills": 5}, {"placement": 2, "team_name": "Bravo", "kills": 1}]
        b = [{"placement": 2, "team_name": "Bravo", "kills": 1}, {"placement": 3, "team_name": "Cobra", "kills": 0}]
        merged = merge_placements([a, b], is_team=True)
        # Bravo@2 seen twice -> kept once; union is 1,2,3.
        self.assertEqual([m["placement"] for m in merged], [1, 2, 3])

    def test_solo_dedupe_by_player(self):
        a = [{"placement": 1, "players": [{"name": "Solo", "kills": 4}]}]
        b = [{"placement": 1, "players": [{"name": "Solo", "kills": 4}]}]  # exact dup
        merged = merge_placements([a, b], is_team=False)
        self.assertEqual(len(merged), 1)


class ProcessJobTests(TestCase):
    """process_job (the worker body) reads each image via the mocked extractor, merges, builds rows."""

    def setUp(self):
        self.admin, _ = make_afc_admin()
        self.lb = StandaloneLeaderboard.objects.create(
            name="TeamLB", format="team", placement_points={"1": 12, "2": 9}, kill_point=1.0,
            creator=self.admin,
        )
        make_team("Alpha", self.admin)  # so "Alpha" matches a real team in the platform pool

    @patch("afc_ocr.services.extract.extract_rows", return_value=(_FAKE_TEAM_READ, "gemini-2.5-flash"))
    def test_process_two_images_merges_and_matches(self, mock_extract):
        job = LeaderboardOcrJob.objects.create(leaderboard=self.lb, created_by=self.admin)
        LeaderboardOcrImage.objects.create(job=job, image=_img("a.jpg"), order=0)
        LeaderboardOcrImage.objects.create(job=job, image=_img("b.jpg"), order=1)

        process_job(job)
        job.refresh_from_db()

        self.assertEqual(job.status, "done")
        self.assertEqual(job.engine, "gemini-2.5-flash")
        self.assertEqual(mock_extract.call_count, 2)             # one call per image
        # Two distinct placements survive the merge (both images returned the same 2 -> deduped).
        self.assertEqual(len(job.rows), 2)
        # "Alpha" row matched the real team (is_unmatched False), so the FE pre-resolves it.
        alpha_row = next(r for r in job.rows if r["raw_name"] == "Alpha")
        self.assertFalse(alpha_row["is_unmatched"])
        self.assertIsNotNone(alpha_row["matched_team_id"])
        # The player names the OCR read are surfaced per row (owner: "display the full name it sees").
        self.assertEqual(alpha_row["players_read"], ["a1"])

    @patch("afc_ocr.services.extract.extract_rows", side_effect=RuntimeError("gemini down"))
    def test_failure_marks_job_failed_not_raises(self, _mock):
        job = LeaderboardOcrJob.objects.create(leaderboard=self.lb, created_by=self.admin)
        LeaderboardOcrImage.objects.create(job=job, image=_img(), order=0)
        process_job(job)                       # must NOT raise
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("gemini down", job.error)

    def test_images_read_concurrently_and_in_order(self):
        """The per-image extractions overlap in threads (the 5-10 min sequential-batch fix) and the
        stored raw_output / merge order still follows image order, not thread completion order.

        Each mocked extract sleeps 0.25s; 3 images sequentially would be >=0.75s, so a wall time
        under 0.6s proves the reads overlapped. The first image's read finishes LAST (longest
        sleep) to catch any completion-order regression in how outputs are zipped back to images.
        """
        import time as _time

        sleeps = {b"img0": 0.5, b"img1": 0.25, b"img2": 0.25}  # img0 slowest -> completes last

        def _slow_extract(image_bytes, *_args, **_kwargs):
            _time.sleep(sleeps[image_bytes])
            label = image_bytes.decode()  # "img0" / "img1" / "img2"
            return (
                {"placements": [{"placement": int(label[-1]) + 1, "team_name": label, "kills": 1}]},
                "gemini-2.5-flash",
            )

        job = LeaderboardOcrJob.objects.create(leaderboard=self.lb, created_by=self.admin)
        for i in range(3):
            img = SimpleUploadedFile(f"s{i}.jpg", f"img{i}".encode(), content_type="image/jpeg")
            LeaderboardOcrImage.objects.create(job=job, image=img, order=i)

        with patch("afc_ocr.services.extract.extract_rows", side_effect=_slow_extract):
            started = _time.monotonic()
            process_job(job)
            elapsed = _time.monotonic() - started

        job.refresh_from_db()
        self.assertEqual(job.status, "done")
        # Sequential would be >= 1.0s (0.5 + 0.25 + 0.25); concurrent ~= the slowest single read.
        self.assertLess(elapsed, 0.9, f"reads did not overlap (took {elapsed:.2f}s)")
        # Order preserved: image N stored image N's read (incl. the per-image timing stamp).
        for i, img in enumerate(job.images.order_by("order")):
            self.assertEqual(img.raw_output["placements"][0]["team_name"], f"img{i}")
            self.assertIn("_elapsed_ms", img.raw_output)
        self.assertEqual([r["raw_name"] for r in job.rows], ["img0", "img1", "img2"])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OcrJobEndpointTests(TestCase):
    """The create -> run -> list -> apply -> delete REST surface. Eager Celery + a mocked extractor make
    `run` actually process inline, so we can assert the job goes done with rows."""

    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.stranger, self.stranger_tok = make_user("stranger")
        self.lb = StandaloneLeaderboard.objects.create(
            name="TeamLB", format="team", placement_points={"1": 12, "2": 9}, kill_point=1.0,
            creator=self.admin,
        )
        self.alpha = make_team("Alpha", self.admin)

    def _create_job(self, tok=None, n_images=2, label="Bermuda"):
        files = [_img(f"s{i}.jpg") for i in range(n_images)]
        return self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/jobs/create/",
            data={"images": files, "map_label": label},
            **bearer(tok or self.admin_tok),
        )

    def test_create_persists_job_and_images(self):
        resp = self._create_job(n_images=2)
        self.assertEqual(resp.status_code, 201)
        body = resp.json()["job"]
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["image_count"], 2)
        self.assertEqual(body["map_label"], "Bermuda")
        job = LeaderboardOcrJob.objects.get(id=body["id"])
        self.assertEqual(job.images.count(), 2)

    def test_create_requires_images(self):
        resp = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/jobs/create/",
            data={"map_label": "x"}, **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_non_manager_403(self):
        self.assertEqual(self._create_job(tok=self.stranger_tok).status_code, 403)

    @patch("afc_ocr.services.extract.extract_rows", return_value=(_FAKE_TEAM_READ, "gemini-2.5-flash"))
    def test_run_processes_to_done(self, _mock):
        job_id = self._create_job().json()["job"]["id"]
        resp = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/jobs/{job_id}/run/",
            **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        # Eager Celery ran process_job inline -> the job is done with merged rows.
        job = LeaderboardOcrJob.objects.get(id=job_id)
        self.assertEqual(job.status, "done")
        self.assertEqual(len(job.rows), 2)

    @patch("afc_ocr.services.extract.extract_rows", return_value=(_FAKE_TEAM_READ, "gemini-2.5-flash"))
    def test_run_all_processes_every_pending(self, _mock):
        j1 = self._create_job(label="M1").json()["job"]["id"]
        j2 = self._create_job(label="M2").json()["job"]["id"]
        resp = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/run-all/", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["queued"], 2)
        for jid in (j1, j2):
            self.assertEqual(LeaderboardOcrJob.objects.get(id=jid).status, "done")

    def test_list_polls_jobs(self):
        self._create_job(label="M1")
        resp = self.client.get(
            f"/leaderboards/standalone/{self.lb.id}/ocr/jobs/", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["jobs"]), 1)

    def test_apply_creates_map_and_marks_applied(self):
        job_id = self._create_job().json()["job"]["id"]
        rows = [{"placement": 1, "kills": 5, "resolution": {"kind": "real", "id": self.alpha.team_id}}]
        resp = self.client.post(
            f"/leaderboards/standalone/{self.lb.id}/ocr/jobs/{job_id}/apply/",
            data={"rows": rows}, content_type="application/json", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LeaderboardMatch.objects.filter(leaderboard=self.lb).count(), 1)
        self.assertEqual(LeaderboardParticipant.objects.filter(leaderboard=self.lb, team=self.alpha).count(), 1)
        job = LeaderboardOcrJob.objects.get(id=job_id)
        self.assertEqual(job.status, "applied")
        self.assertIsNotNone(job.applied_match_id)
        # placement 1 (12) + 5 kills = 17 (scored via the shared _apply_ocr_rows helper).
        self.assertEqual(resp.json()["standings"][0]["total_points"], 17)

    def test_delete_removes_job(self):
        job_id = self._create_job().json()["job"]["id"]
        resp = self.client.delete(
            f"/leaderboards/standalone/{self.lb.id}/ocr/jobs/{job_id}/", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(LeaderboardOcrJob.objects.filter(id=job_id).exists())
