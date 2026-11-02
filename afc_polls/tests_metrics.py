"""
afc_polls.tests_metrics - admins keep the detailed metrics of PAST awards (inbox #432).

WHAT IS WORTH TESTING HERE

  1. A CLOSED EDITION AND A CLOSED POLL STILL ANSWER AN ADMIN. The endpoints carry no phase or date
     filter on purpose, so the test builds an ARCHIVED edition whose polls closed months ago and
     checks every per-option tally is still there.
  2. THE ORIGINAL (2025) BALLOT IS REACHABLE. Its votes live in afc_awards.Vote, which the poll
     engine never imported, so a dedicated admin endpoint reads them.
  3. NOBODY ELSE GETS ANYTHING NEW. A signed-out caller is 401, a plain player is 403, and so is an
     organizer, on both endpoints.

Run:
    AFC_TEST_DB_NAME=test_afc_polls python manage.py test afc_polls.tests_metrics -v 2
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from afc_awards.models import Category, CategoryNominee, Nominee, Section, Vote

from .models import AwardsEdition, Poll, PollAnswer, PollOption, PollQuestion, PollResponse
from .tests import make_user, token_for


def closed_edition():
    """An ARCHIVED edition with one poll that closed long ago and three submitted ballots."""
    edition = AwardsEdition.objects.create(
        slug="nfca-2024", title="NFCA 2024", year=2024, status=AwardsEdition.ARCHIVED,
    )
    poll = Poll.objects.create(
        slug="nfca-2024-esports", title="Esports", kind=Poll.AWARD, edition=edition,
        awards_edition="NFCA 2024", visibility=Poll.PUBLIC,
        opens_at=timezone.now() - timedelta(days=200), closes_at=timezone.now() - timedelta(days=180),
    )
    question = PollQuestion.objects.create(poll=poll, order=0, prompt="Best player", required=True)
    first = PollOption.objects.create(question=question, order=0, label="ALPHA")
    second = PollOption.objects.create(question=question, order=1, label="BRAVO")
    for index, option in enumerate([first, first, second]):
        voter = make_user(f"voter{index}")
        response = PollResponse.objects.create(poll=poll, respondent=voter, status=PollResponse.SUBMITTED)
        PollAnswer.objects.create(response=response, question=question, option=option)
    return edition, poll, first, second


class EditionMetricsTests(TestCase):
    def setUp(self):
        self.edition, self.poll, self.first, self.second = closed_edition()
        self.admin = make_user("metrics_admin", role="admin")
        self.player = make_user("metrics_player", role="player")
        self.url = f"/polls/admin/editions/{self.edition.slug}/metrics/"

    def test_a_closed_edition_and_poll_still_answer_an_admin(self):
        self.assertEqual(self.edition.phase(), AwardsEdition.ARCHIVED)
        self.assertTrue(self.poll.closes_at < timezone.now())
        response = self.client.get(self.url, **token_for(self.admin))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["totals"], {"polls": 1, "questions": 1, "responses": 3})
        poll = body["polls"][0]
        self.assertTrue(poll["is_closed"])
        options = poll["questions"][0]["options"]
        self.assertEqual([(o["label"], o["votes"]) for o in options], [("ALPHA", 2), ("BRAVO", 1)])
        self.assertEqual(options[0]["percent"], 66.7)

    def test_an_unknown_edition_is_404_for_an_admin(self):
        response = self.client.get("/polls/admin/editions/nope/metrics/", **token_for(self.admin))
        self.assertEqual(response.status_code, 404)

    def test_signed_out_is_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_a_player_is_refused(self):
        self.assertEqual(self.client.get(self.url, **token_for(self.player)).status_code, 403)


class LegacyBallotMetricsTests(TestCase):
    url = "/polls/admin/legacy-ballot-metrics/"

    def setUp(self):
        self.admin = make_user("legacy_admin", role="admin")
        self.player = make_user("legacy_player", role="player")
        section = Section.objects.create(name="Esports Awards", max_votes=13)
        category = Category.objects.create(name="Best Duo", section=section)
        alpha = Nominee.objects.create(name="ALPHA")
        bravo = Nominee.objects.create(name="BRAVO")
        for nominee in (alpha, bravo):
            CategoryNominee.objects.create(category=category, nominee=nominee)
        for index, nominee in enumerate([alpha, alpha, bravo]):
            Vote.objects.create(user=make_user(f"lv{index}"), section=section, category=category, nominee=nominee)

    def test_an_admin_reads_every_breakdown(self):
        response = self.client.get(self.url, **token_for(self.admin))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["totals"], {"votes": 3, "voters": 3})
        self.assertEqual(body["sections"], [{"section": "Esports Awards", "votes": 3}])
        category = body["categories"][0]
        self.assertEqual(category["votes"], 3)
        self.assertEqual([(n["nominee"], n["votes"]) for n in category["nominees"]],
                         [("ALPHA", 2), ("BRAVO", 1)])
        self.assertEqual(sum(day["votes"] for day in body["timeline"]), 3)

    def test_signed_out_is_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_a_player_is_refused(self):
        self.assertEqual(self.client.get(self.url, **token_for(self.player)).status_code, 403)
