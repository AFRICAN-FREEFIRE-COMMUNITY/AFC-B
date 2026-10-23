"""
Tests for each organizer's own line in the standalone leaderboard screenshot batch
(afc_leaderboard/ocr_queue.py, owner 2026-10-22, inbox #334: "its own queue so one organizer's big
batch doesn't hold up others"). Celery is never reached: process_leaderboard_ocr_job.delay is mocked.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from afc_leaderboard import ocr_queue
from afc_leaderboard.models import LeaderboardOcrImage, LeaderboardOcrJob, StandaloneLeaderboard


def _lb(org, owner, name):
    return StandaloneLeaderboard.objects.create(name=name, format="team", organization=org, creator=owner)


def _jobs(lb, n, start):
    out = []
    for i in range(n):
        j = LeaderboardOcrJob.objects.create(leaderboard=lb, created_by=lb.creator, status="pending",
                                             queued_at=start + timedelta(seconds=i))
        out.append(j)
    return out


@override_settings(OCR_QUEUE_SLOTS=2)
class OcrQueueTests(TestCase):
    def setUp(self):
        from afc_organizers.test_ai_key import _org, _user
        self.a_owner, self.a_tok = _user("q_a_owner")
        self.b_owner, _ = _user("q_b_owner")
        self.org_a = _org("q-org-a", self.a_owner)
        self.org_b = _org("q-org-b", self.b_owner)
        self.lb_a = _lb(self.org_a, self.a_owner, "A")
        self.lb_b = _lb(self.org_b, self.b_owner, "B")
        patcher = mock.patch("afc_leaderboard.tasks.process_leaderboard_ocr_job.delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)
        self.t0 = timezone.now() - timedelta(minutes=1)

    def _status(self, jobs):
        return [LeaderboardOcrJob.objects.get(pk=j.pk).status for j in jobs]

    def test_organizers_take_turns_so_a_big_batch_does_not_hold_up_a_small_one(self):
        big = _jobs(self.lb_a, 5, self.t0)                          # organizer A asked first, 5 maps
        small = _jobs(self.lb_b, 1, self.t0 + timedelta(seconds=30))  # organizer B, later, 1 map
        started = ocr_queue.dispatch()
        self.assertEqual(set(started), {str(big[0].id), str(small[0].id)})
        self.assertEqual(self._status(big), ["processing", "pending", "pending", "pending", "pending"])
        self.assertEqual(self.delay.call_count, 2)

    def test_one_organizer_alone_uses_every_slot(self):
        jobs = _jobs(self.lb_a, 3, self.t0)
        ocr_queue.dispatch()
        self.assertEqual(self._status(jobs), ["processing", "processing", "pending"])

    def test_a_finished_read_hands_its_slot_to_the_next_map(self):
        jobs = _jobs(self.lb_a, 3, self.t0)
        ocr_queue.dispatch()
        LeaderboardOcrJob.objects.filter(pk=jobs[0].pk).update(status="done")
        ocr_queue.dispatch()
        self.assertEqual(self._status(jobs), ["done", "processing", "processing"])

    def test_a_read_that_hung_is_failed_with_a_sentence_and_frees_its_slot(self):
        jobs = _jobs(self.lb_a, 3, self.t0)
        ocr_queue.dispatch()
        LeaderboardOcrJob.objects.filter(pk=jobs[0].pk).update(updated_at=timezone.now() - timedelta(minutes=11))
        ocr_queue.dispatch()
        hung = LeaderboardOcrJob.objects.get(pk=jobs[0].pk)
        self.assertEqual((hung.status, hung.error), ("failed", ocr_queue.STALE_MESSAGE))
        self.assertEqual(self._status(jobs[1:]), ["processing", "processing"])

    def test_a_map_never_asked_to_be_read_does_not_join_the_line(self):
        j = LeaderboardOcrJob.objects.create(leaderboard=self.lb_a, created_by=self.a_owner, status="pending")
        self.assertEqual(ocr_queue.dispatch(), [])
        self.assertEqual(self._status([j]), ["pending"])

    def test_the_wait_shows_the_place_in_this_organizers_line(self):
        jobs = _jobs(self.lb_a, 4, self.t0)
        for j in jobs:
            LeaderboardOcrImage.objects.create(job=j, image="leaderboard_ocr/x.png")
        _jobs(self.lb_b, 2, self.t0 - timedelta(seconds=5))   # another organizer, waiting longer
        ocr_queue.dispatch()                                  # A gets one slot, B gets one
        fresh = list(LeaderboardOcrJob.objects.filter(leaderboard=self.lb_a).order_by("queued_at"))
        est = ocr_queue.estimates(fresh)
        self.assertNotIn(str(fresh[0].id), est, "a map being read has no place in line")
        self.assertEqual([est[str(j.id)][0] for j in fresh[1:]], [1, 2, 3])
        waits = [est[str(j.id)][1] for j in fresh[1:]]
        self.assertTrue(all(w > 0 for w in waits) and waits == sorted(waits), waits)

    def test_run_all_puts_maps_in_line_instead_of_straight_on_the_workers(self):
        from afc_organizers.models import OrganizationMember
        from afc_organizers.test_ai_key import _post
        OrganizationMember.objects.filter(organization=self.org_a, user=self.a_owner).update(can_upload_results=True)
        for _ in range(4):
            LeaderboardOcrJob.objects.create(leaderboard=self.lb_a, created_by=self.a_owner, status="pending")
        with mock.patch("afc_ocr.services.extract.can_read", return_value=True):
            r = _post(self.a_tok, f"/leaderboards/standalone/{self.lb_a.pk}/ocr/run-all/")
        self.assertEqual(r.status_code, 200, r.content)
        statuses = sorted(j["status"] for j in r.json()["jobs"])
        self.assertEqual(statuses, ["pending", "pending", "processing", "processing"])
        waiting = [j for j in r.json()["jobs"] if j["status"] == "pending"]
        self.assertEqual(sorted(j["queue_position"] for j in waiting), [1, 2])
        self.assertTrue(all(j["wait_seconds"] > 0 for j in waiting))
        self.assertEqual(self.delay.call_count, 2)
