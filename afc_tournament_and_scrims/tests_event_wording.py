r"""What AFC calls an event when it writes to a player.

WHY (owner backlog item 32): AFC runs tournaments AND scrims, and every message that hardcoded
"tournament" told half its competitors they were in something they were not. It hid for a month
because scrims only began auto-completing on 2026-07-06, so the completion notice was the first
message most scrims players ever received.

These tests exist so the fix cannot quietly regress the way the original bug arrived: by somebody
writing one more sentence with the word "tournament" in it.

Run: .venv\Scripts\python.exe manage.py test afc_tournament_and_scrims.tests_event_wording
"""
import datetime

from django.test import TestCase

from afc_auth.models import User
from afc_tournament_and_scrims.event_wording import event_noun, is_scrims
from afc_tournament_and_scrims.models import Event


class EventWordingTests(TestCase):
    def setUp(self):
        today = datetime.date.today()
        self.creator = User.objects.create(
            username="wording_admin", email="wording_admin@x.com", full_name="Wording Admin",
            role="admin", password="x")
        self.common = dict(
            participant_type="squad", event_type="internal", max_teams_or_players=16,
            event_mode="virtual", start_date=today, end_date=today,
            registration_open_date=today, registration_end_date=today, prizepool="0",
            event_rules="r", event_status="ongoing", registration_link="https://x.com/r",
            number_of_stages=1, creator=self.creator)

    def _event(self, competition_type):
        return Event.objects.create(
            competition_type=competition_type, event_name=f"{competition_type} event",
            **self.common)

    def test_a_scrims_event_is_called_scrims(self):
        event = self._event("scrims")

        self.assertTrue(is_scrims(event))
        self.assertEqual(event_noun(event), "Scrims")
        self.assertEqual(event_noun(event, capitalized=False), "scrims")

    def test_a_tournament_is_called_a_tournament(self):
        event = self._event("tournament")

        self.assertFalse(is_scrims(event))
        self.assertEqual(event_noun(event), "Tournament")
        self.assertEqual(event_noun(event, capitalized=False), "tournament")

    def test_an_unknown_competition_type_reads_as_a_tournament(self):
        """The safe default. competition_type is a choice field, so this should not happen, but a
        message that has to pick a word cannot raise: an event with a value nobody expected still
        needs its players told it finished."""
        event = self._event("tournament")
        event.competition_type = "something_new"

        self.assertEqual(event_noun(event), "Tournament")

    def test_every_notice_that_names_an_event_kind_goes_through_the_helper(self):
        """THE REGRESSION GUARD, and the reason this file exists rather than three assertions
        buried elsewhere. The bug was not one wrong string, it was the habit of writing the word
        directly. If somebody adds a fourth notice with a hardcoded "tournament", this fails.

        Scoped to the notification and title literals in views.py, so a comment or a variable name
        mentioning tournaments is not a false positive.
        """
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parent / "views.py"
        text = source.read_text(encoding="utf-8")

        offenders = []
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # A title= or message= literal that says "tournament" without asking the event.
            if not re.search(r'\b(title|message)\s*=\s*f?["\']', stripped):
                continue
            if re.search(r'\btournament\b', stripped, re.IGNORECASE) and "_noun" not in stripped:
                offenders.append(f"{number}: {stripped[:100]}")

        self.assertEqual(
            offenders, [],
            "These notification literals name a tournament without checking the event's kind. "
            "Use afc_tournament_and_scrims.event_wording.event_noun:\n" + "\n".join(offenders))
