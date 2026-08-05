r"""A duplicated event must not vanish from the player who registered for it.

THE BUG (owner backlog item 27). Duplicate an event, give it a future start date, publish it, and
it still said "Event completed". Two separate causes, and fixing one without the other leaves the
report standing:

  1. `duplicate_event` copied the source's DATES unchanged, so a clone of a finished event sat in
     the past. The status sweep that runs every five minutes then re-stamped it "completed" on its
     own, undoing the reset the clone had just made.
  2. `compute_registered_events` filtered `event_status` in SQL, which reads the RAW stored word.
     An event carrying a stale "completed" was dropped by the database before any code could ask
     what its status effectively was, so a player who had registered appeared to be registered for
     nothing, while the event's own page said "upcoming" at the same moment.

Run: .venv\Scripts\python.exe manage.py test afc_player.tests_registered_events
"""
import datetime

from django.test import TestCase
from django.utils import timezone

from afc_auth.models import User
from afc_player.aggregation import compute_registered_events
from afc_tournament_and_scrims.models import Event, RegisteredCompetitors
from afc_tournament_and_scrims.views import _cloned_dates


class RegisteredEventsEffectiveStatusTests(TestCase):
    def setUp(self):
        self.player = User.objects.create(
            username="regplayer", email="regplayer@x.com", full_name="Reg Player",
            role="player", password="x")
        self.creator = User.objects.create(
            username="regadmin", email="regadmin@x.com", full_name="Reg Admin",
            role="admin", password="x")

    def _event(self, *, status, start):
        return Event.objects.create(
            competition_type="tournament", participant_type="solo", event_type="internal",
            max_teams_or_players=16, event_name=f"Event {status} {start}", event_mode="virtual",
            start_date=start, end_date=start, registration_open_date=start,
            registration_end_date=start, prizepool="0", event_rules="r", event_status=status,
            registration_link="https://x.com/r", number_of_stages=1, creator=self.creator,
            is_draft=False)

    def _register(self, event):
        return RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="approved")

    def test_a_stale_completed_stamp_does_not_hide_an_upcoming_event(self):
        """THE REPORTED SYMPTOM. The stored word is "completed" and the start date is next week,
        which is the exact state a freshly duplicated event is in."""
        future = timezone.localdate() + datetime.timedelta(days=7)
        event = self._event(status="completed", start=future)
        self._register(event)

        rows = compute_registered_events(self.player)

        self.assertEqual([r["event_id"] for r in rows], [event.event_id])

    def test_the_status_reported_is_the_effective_one_not_the_stored_word(self):
        """A row that appeared but still read "completed" would just move the confusion rather
        than fix it, so the badge here has to agree with the event's own page."""
        future = timezone.localdate() + datetime.timedelta(days=7)
        event = self._event(status="completed", start=future)
        self._register(event)

        rows = compute_registered_events(self.player)

        self.assertEqual(rows[0]["event_status"], "upcoming")

    def test_a_genuinely_finished_event_is_still_left_out(self):
        """The widening must not turn this list into every event a player ever entered. An event
        that finished LAST week is finished, and its stored status is honest."""
        past = timezone.localdate() - datetime.timedelta(days=7)
        event = self._event(status="completed", start=past)
        self._register(event)

        self.assertEqual(compute_registered_events(self.player), [])

    def test_a_cancelled_event_is_left_out(self):
        past_or_future = timezone.localdate() + datetime.timedelta(days=3)
        event = self._event(status="cancelled", start=past_or_future)
        self._register(event)

        self.assertEqual(compute_registered_events(self.player), [])

    def test_a_withdrawn_registration_is_still_left_out(self):
        """The registration-side filters are untouched by the widening."""
        future = timezone.localdate() + datetime.timedelta(days=7)
        event = self._event(status="upcoming", start=future)
        row = self._register(event)
        row.status = "withdrawn"
        row.save(update_fields=["status"])

        self.assertEqual(compute_registered_events(self.player), [])


class ClonedDatesTests(TestCase):
    """The other half: a clone must not land in the past for the sweep to find."""

    class _Source:
        def __init__(self, start, end, reg_open, reg_end):
            self.start_date = start
            self.end_date = end
            self.registration_open_date = reg_open
            self.registration_end_date = reg_end

    def test_a_clone_of_a_finished_event_starts_tomorrow(self):
        today = timezone.localdate()
        source = self._Source(
            today - datetime.timedelta(days=30), today - datetime.timedelta(days=28),
            today - datetime.timedelta(days=40), today - datetime.timedelta(days=31))

        shifted = _cloned_dates(source)

        self.assertEqual(shifted["start_date"], today + datetime.timedelta(days=1))

    def test_the_shape_of_the_schedule_survives_the_shift(self):
        """A three day event stays three days, and registration still opens the same number of
        days before it starts. Clearing the dates would have been simpler and would have thrown
        away the schedule the organizer duplicated the event in order to reuse."""
        today = timezone.localdate()
        start = today - datetime.timedelta(days=30)
        source = self._Source(
            start, start + datetime.timedelta(days=2),
            start - datetime.timedelta(days=10), start - datetime.timedelta(days=1))

        shifted = _cloned_dates(source)

        self.assertEqual(shifted["end_date"] - shifted["start_date"], datetime.timedelta(days=2))
        self.assertEqual(
            shifted["start_date"] - shifted["registration_open_date"], datetime.timedelta(days=10))

    def test_dates_already_in_the_future_are_left_exactly_as_they_are(self):
        """Nothing to protect against, and the organizer's own dates beat anything computed."""
        today = timezone.localdate()
        start = today + datetime.timedelta(days=14)
        source = self._Source(start, start, start, start)

        self.assertEqual(_cloned_dates(source)["start_date"], start)

    def test_a_source_with_no_start_date_is_carried_across_untouched(self):
        """There is nothing to anchor a shift to, and an event with no start date is not one the
        sweep can mis-stamp anyway."""
        source = self._Source(None, None, None, None)

        self.assertEqual(
            _cloned_dates(source),
            {"start_date": None, "end_date": None,
             "registration_open_date": None, "registration_end_date": None})
