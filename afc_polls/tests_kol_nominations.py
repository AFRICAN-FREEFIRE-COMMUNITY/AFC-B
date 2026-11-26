"""
afc_polls.tests_kol_nominations - the KOL creators' nomination round (AFC inbox #633 / #634).

Design: WEBSITE/tasks/kol-nominations-design-2026-11-25.md. Plan: WEBSITE/tasks/plan-kol-nominations-2026-11-26.md.

WHAT IS WORTH TESTING HERE
  1. THE ROUND'S POLL IS INVISIBLE TO EVERY PUBLIC DOOR. It is a DRAFT with no edition, so /polls, /awards and the
     normal submit door never list it, show it or take a vote on it. Only the code door writes to it.
  2. ONE CODE, ONE CREATOR, ONE BALLOT. Codes are stored hashed, typed sloppily they still work, a wrong one is
     counted and limited, a replaced one stops working, and the ballot belongs to the CREATOR (a new code keeps the
     picks).
  3. NEVER YOURSELF, AT MOST SIX, NO COUNTS. The server refuses a self-pick and a seventh pick whatever the page sent.
  4. THE ADMIN SIDE never stores an email and never writes AFC into the message a creator receives.

Run (see memory reference_backend_tests_in_worktree):
    AFC_TEST_DB_NAME=test_afc_kolnom python manage.py test afc_polls.tests_kol_nominations -v 2
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .kol_nominations import AWARDS, ensure_round, respondent_key_for
from .models import AwardsEdition, KolCreator, KolCreatorOption, KolNominationRound, Poll, PollOption, PollQuestion
from .tests import make_user, token_for


def kol_edition(slug="kol-2026", hidden=True):
    """The themed KOL edition. Hidden by default: that is how it sits while the creators nominate."""
    return AwardsEdition.objects.create(
        slug=slug, title="KOL Awards 2026", year=2026, theme=AwardsEdition.THEME_DETTY_ROYALE, hidden=hidden,
    )


def add_creators(edition, names=("Jokkie", "Lord Rex", "Ama Plays", "Kwame FF")):
    """One KolCreator per name; the handle is the name, lower case, without spaces."""
    return [
        KolCreator.objects.create(
            edition=edition, name=name, tiktok_handle=name.lower().replace(" ", "_"), country="Nigeria",
        )
        for name in names
    ]


def open_round(edition, **dates):
    """ensure_round with the round open now, unless dates are given."""
    now = timezone.now()
    return ensure_round(
        edition,
        opens_at=dates.get("opens_at", now - timedelta(hours=1)),
        closes_at=dates.get("closes_at", now + timedelta(days=3)),
    )


class KolRoundBuildTests(TestCase):
    def test_round_poll_is_draft_and_editionless(self):
        edition = kol_edition()
        add_creators(edition)
        poll = open_round(edition).poll
        self.assertEqual(poll.visibility, Poll.DRAFT)
        self.assertIsNone(poll.edition_id)
        questions = list(poll.questions.order_by("order"))
        self.assertEqual([q.prompt for q in questions], list(AWARDS))
        self.assertEqual(len(questions), 6)
        for question in questions:
            self.assertEqual(question.answer_type, PollQuestion.MULTIPLE_CHOICE)
            self.assertEqual(question.config.get("max_choices"), 6)

    def test_ensure_round_is_idempotent(self):
        edition = kol_edition()
        add_creators(edition)
        first = open_round(edition)
        second = ensure_round(edition)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Poll.objects.filter(kol_round__edition=edition).count(), 1)
        self.assertEqual(first.poll.questions.count(), 6)
        self.assertEqual(PollOption.objects.filter(question__poll=first.poll).count(), 6 * 4)
        self.assertEqual(KolCreatorOption.objects.filter(creator__edition=edition).count(), 6 * 4)

    def test_new_creator_gets_options_on_rerun(self):
        edition = kol_edition()
        add_creators(edition)
        round_ = open_round(edition)
        add_creators(edition, names=("Zara Clips",))
        ensure_round(edition)
        self.assertEqual(PollOption.objects.filter(question__poll=round_.poll).count(), 6 * 5)
        self.assertEqual(KolCreator.objects.get(name="Zara Clips").options.count(), 6)

    def test_round_not_in_edition_detail(self):
        edition = kol_edition()
        add_creators(edition)
        round_ = open_round(edition)
        admin = make_user("kolnomadmin", role="admin")
        answer = APIClient().get(f"/polls/editions/{edition.slug}/", **token_for(admin))
        self.assertEqual(answer.status_code, 200)
        slugs = [poll["slug"] for poll in answer.json()["polls"]]
        self.assertNotIn(round_.poll.slug, slugs)

    def test_state_by_dates(self):
        now = timezone.now()
        edition = kol_edition(hidden=True)
        add_creators(edition)
        round_ = open_round(edition)
        self.assertEqual(round_.state(), KolNominationRound.OPEN)       # a hidden edition is still open
        round_.opens_at, round_.closes_at = now + timedelta(hours=1), now + timedelta(days=1)
        self.assertEqual(round_.state(), KolNominationRound.NOT_OPEN)
        round_.opens_at, round_.closes_at = now - timedelta(days=2), now - timedelta(minutes=1)
        self.assertEqual(round_.state(), KolNominationRound.CLOSED)
        round_.opens_at, round_.closes_at = None, now + timedelta(days=1)
        self.assertEqual(round_.state(), KolNominationRound.NOT_OPEN)       # no opening date: not open

    def test_respondent_key_is_per_creator(self):
        edition = kol_edition()
        jokkie, rex, *_ = add_creators(edition)
        round_ = open_round(edition)
        self.assertEqual(respondent_key_for(round_, jokkie), respondent_key_for(round_, jokkie))
        self.assertNotEqual(respondent_key_for(round_, jokkie), respondent_key_for(round_, rex))
        self.assertEqual(len(respondent_key_for(round_, jokkie)), 64)
