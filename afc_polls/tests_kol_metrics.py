"""
Tests for afc_polls.kol_metrics (inbox #653, owner 27 Nov 2026: "the metrics page should show very very detailed
info, who was most searched (how many times), average time to nominate, stuff like that").

What they hold: the ballot records opens (one per visit), changes and the first count; the search log refuses what it
cannot read and needs the creator's cookie; the admin metrics read back the funnel, the time to nominate, the most
searched creators, searches that found nothing, the per-award numbers, the self-only list with its reminder, the codes
never opened and the email counts; only a poll admin or a KOL Admin may read them.
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .kol_nominations import new_code
from .models import KolBallotEvent, KolCreator, KolEmail
from .tests_kol_nominations import (
    add_creators, admin_client, ballot, client_for, door, kol_admin_user, kol_edition, open_round, save,
)
from .tests import make_user


def search(client, edition, question_id, query, result_count, picked_id=None):
    return client.post(f"/polls/kol-nominations/{edition.slug}/search/",
                       {"question_id": question_id, "query": query, "result_count": result_count,
                        "picked_id": picked_id}, format="json")


class KolMetricsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.edition = kol_edition()
        self.creators = add_creators(self.edition, names=("Jokkie", "Lord Rex", "Ama Plays", "Kwame FF"))
        self.jokkie, self.rex, self.ama, self.kwame = self.creators
        self.round = open_round(self.edition)
        self.awards = list(self.round.poll.questions.order_by("order"))
        self.client = APIClient()
        self.assertEqual(door(self.client, self.edition, new_code(self.jokkie)).status_code, 200)
        self.admin = admin_client("kolmetricsadmin")
        self.url = f"/polls/admin/kol-nominations/{self.edition.slug}/metrics/"

    def full(self, who):
        return {str(a.question_id): [who.pk] for a in self.awards}

    def test_open_is_recorded_once_per_visit(self):
        ballot(self.client, self.edition)
        ballot(self.client, self.edition)
        self.assertEqual(KolBallotEvent.objects.filter(kind=KolBallotEvent.OPENED, creator=self.jokkie).count(), 1)
        KolBallotEvent.objects.update(at=timezone.now() - timedelta(hours=1))      # the next visit
        ballot(self.client, self.edition)
        self.assertEqual(KolBallotEvent.objects.filter(kind=KolBallotEvent.OPENED, creator=self.jokkie).count(), 2)

    def test_save_records_changes_and_the_first_count_only(self):
        q0 = str(self.awards[0].question_id)
        self.assertEqual(save(self.client, self.edition, {q0: [self.rex.pk]}).status_code, 200)
        self.assertFalse(KolBallotEvent.objects.filter(kind=KolBallotEvent.COMPLETE).exists())
        self.assertEqual(save(self.client, self.edition, self.full(self.rex)).status_code, 200)
        self.assertEqual(KolBallotEvent.objects.filter(kind=KolBallotEvent.COMPLETE).count(), 1)
        picks = self.full(self.rex)
        picks[q0] = [self.ama.pk]                                                   # one swap
        self.assertEqual(save(self.client, self.edition, picks).status_code, 200)
        self.assertEqual(KolBallotEvent.objects.filter(kind=KolBallotEvent.COMPLETE).count(), 1)
        changed = KolBallotEvent.objects.get(kind=KolBallotEvent.CHANGED)
        self.assertEqual(changed.result_count, 1)

    def test_search_log_reads_only_what_it_can(self):
        qid = self.awards[0].question_id
        self.assertEqual(search(self.client, self.edition, qid, "  Lord  REX ", 1, self.rex.pk).status_code, 200)
        row = KolBallotEvent.objects.get(kind=KolBallotEvent.SEARCH)
        self.assertEqual((row.query, row.result_count, row.picked_id, row.creator_id),
                         ("lord rex", 1, self.rex.pk, self.jokkie.pk))
        for bad in ((qid, "", 1, None), (qid, 5, 1, None), (True, "x", 1, None), (qid, "x", True, None),
                    (qid, "x", -1, None), (qid, "x", 1, "7"), (999999, "x", 1, None), (qid, "x", 1, 999999)):
            answer = search(self.client, self.edition, *bad)
            self.assertEqual((answer.status_code, answer.json()["code"]), (400, "kol_bad_search"), bad)
        self.assertEqual(search(APIClient(), self.edition, qid, "x", 0).status_code, 401)
        self.assertEqual(KolBallotEvent.objects.filter(kind=KolBallotEvent.SEARCH).count(), 1)

    def test_metrics_read_back(self):
        qid = self.awards[0].question_id
        search(self.client, self.edition, qid, "rex", 1, self.rex.pk)
        search(self.client, self.edition, qid, "lord", 1, self.rex.pk)
        search(self.client, self.edition, qid, "mr fun", 0)
        self.assertEqual(save(self.client, self.edition, self.full(self.rex)).status_code, 200)
        # Rex starts a ballot with only himself in award 1: not counting, on the self-only list
        rex_client = APIClient()
        door(rex_client, self.edition, new_code(self.rex))
        save(rex_client, self.edition, {str(qid): [self.rex.pk]})
        KolEmail.objects.create(creator=self.ama, to_address="ama_plays@example.com", status=KolEmail.FAILED,
                                error="kol_email_send_failed", finished_at=timezone.now())
        KolEmail.objects.create(creator=self.jokkie, to_address="jokkie@example.com", status=KolEmail.SENT,
                                finished_at=timezone.now() - timedelta(days=1))
        KolCreator.objects.filter(pk=self.kwame.pk).update(email="")

        data = self.admin.get(self.url).json()
        head = data["headline"]
        self.assertEqual((head["voters"], head["counting"], head["started_not_counting"]), (4, 1, 1))
        self.assertEqual((head["codes_opened"], head["never_opened"]), (2, 2))
        self.assertEqual((head["searches"], head["searchers"], head["searches_nothing"]), (3, 1, 1))
        self.assertEqual((head["code_emails_sent"], head["code_emails_failed"], head["no_address"]), (1, 1, 1))
        self.assertEqual([step["count"] for step in data["funnel"]], [4, 1, 2, 4, 2, 1])
        self.assertEqual(data["time"]["ballots"], 1)
        self.assertEqual(data["time"]["from_first_save"], 0)                       # measured from the recorded open
        self.assertIsNotNone(data["time"]["median_seconds"])
        top = data["searches"]["most_searched"][0]
        self.assertEqual((top["name"], top["searches"], top["by"]), ("Lord Rex", 2, 1))
        self.assertEqual(data["searches"]["found_nothing"][0]["query"], "mr fun")
        award = data["awards"][0]
        self.assertEqual((award["with_pick"], award["empty"], award["only_self"], award["picked_self"]), (2, 0, 1, 1))
        self.assertEqual(award["top"]["name"], "Lord Rex")
        self.assertEqual(data["awards"][1]["empty"], 1)
        only = data["self"]["only_self"]
        self.assertEqual([(r["name"], r["awards_only_self"]) for r in only], [("Lord Rex", 1)])
        self.assertIn("only yourself", only[0]["reminder"])
        self.assertNotIn("african", only[0]["reminder"].lower())                   # a copied message never names AFC
        self.assertEqual({r["name"] for r in data["codes"]["never_opened"]}, {"Ama Plays", "Kwame FF"})
        self.assertEqual(data["emails"]["failed"][0]["address"][:2], "a*")          # masked
        self.assertEqual(len(data["creators"]), 4)
        self.assertTrue(next(r for r in data["creators"] if r["name"] == "Jokkie")["counts"])

    def test_finish_time_rebuilt_for_a_ballot_saved_before_recording(self):
        self.assertEqual(save(self.client, self.edition, self.full(self.rex)).status_code, 200)
        KolBallotEvent.objects.all().delete()                                      # as on a ballot from 27 Nov
        data = self.admin.get(self.url).json()
        self.assertEqual((data["time"]["ballots"], data["time"]["from_first_save"]), (1, 1))
        self.assertEqual(sum(day["counted"] for day in data["activity"]["days"]), 1)

    def test_who_may_read(self):
        self.assertEqual(APIClient().get(self.url).status_code, 401)
        self.assertEqual(client_for(make_user("kolmetricsplayer")).get(self.url).status_code, 403)
        self.assertEqual(client_for(kol_admin_user("kolmetricsgarena")).get(self.url).status_code, 200)
        other = kol_edition(slug="other-2026")
        self.assertEqual(client_for(kol_admin_user("kolmetricsgarena2")).get(
            f"/polls/admin/kol-nominations/{other.slug}/metrics/").status_code, 404)
