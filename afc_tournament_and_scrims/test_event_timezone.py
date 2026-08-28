"""An event's timezone belongs to the EVENT, not to whoever is editing it.

WHY THIS FILE EXISTS (owner report 2026-08-28)
    An organizer: "i hired two people to assist me with hosting And they from Nigeria But the time
    for them is showing in SA time."

    Event times are stored as NAIVE wall-clock (`event_start_time`, a TimeField) paired with
    `Event.timezone`. All four event forms, both creates AND both edits, used to stamp that timezone
    from `Intl.DateTimeFormat().resolvedOptions().timeZone`, the browser of whoever had the form
    open. Two defects fell out of that:

      DISPLAY, which is what was reported: the form showed a bare "22:00" with no timezone anywhere,
      so an assistant in Lagos read a Johannesburg wall-clock as their own.

      SILENT CORRUPTION, which nobody had noticed: that same assistant saving ANY unrelated edit
      re-stamped the event Africa/Lagos while the numbers stayed 22:00, moving the event an hour for
      every viewer and every registered player.

    The frontend fix is to stop sending the key on edit. THIS FILE IS WHY THAT WORKS AND KEEPS
    WORKING: `apply_event_writes` skips absent keys, so an edit that does not mention the timezone
    cannot change it. That behaviour is currently a property of the contract loop rather than an
    explicit decision about timezones, so without a test naming it, a future refactor could make
    absent mean "clear" and silently reopen the bug from the other side.

Run: AFC_TEST_DB_NAME=test_afc_tz python manage.py test afc_tournament_and_scrims.test_event_timezone
"""
from datetime import date, time, timedelta

from django.test import TestCase

from afc_auth.models import User, UserProfile
from afc_tournament_and_scrims.event_contract import apply_event_writes
from afc_tournament_and_scrims.models import Event

JOBURG = "Africa/Johannesburg"
LAGOS = "Africa/Lagos"


class EventTimezoneSurvivesAnEditTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create(
            username="tzorganizer", email="tz@x.com", full_name="TZ", role="admin",
            password="x", country="South Africa", uid=None,
        )
        UserProfile.objects.create(user=self.organizer)
        self.event = Event.objects.create(
            event_name="Timezone Cup", slug="timezone-cup",
            competition_type="tournament", participant_type="squad",
            event_type="online", event_mode="single",
            max_teams_or_players=16, number_of_stages=1,
            start_date=date.today() + timedelta(days=3),
            end_date=date.today() + timedelta(days=4),
            registration_open_date=date.today(),
            registration_end_date=date.today() + timedelta(days=2),
            event_start_time=time(22, 0),
            event_end_time=time(23, 30),
            timezone=JOBURG,
            creator=self.organizer,
        )

    def _apply(self, payload):
        apply_event_writes(self.event, payload, actor=self.organizer)
        self.event.save()
        self.event.refresh_from_db()

    # ── the regression test ───────────────────────────────────────────────────────────────────
    def test_an_edit_that_does_not_mention_the_timezone_LEAVES_IT(self):
        """THE ONE THAT MATTERS. This is what the frontend fix depends on: the edit forms no longer
        send the key at all, so absent must mean "unchanged" and never "clear"."""
        self._apply({"event_name": "Timezone Cup, renamed"})
        self.assertEqual(self.event.timezone, JOBURG)
        self.assertEqual(self.event.event_name, "Timezone Cup, renamed")

    def test_the_WALL_CLOCK_is_untouched_too(self):
        """The pairing is what carries the meaning. A timezone that survived while the numbers moved
        would be the same bug wearing different clothes."""
        self._apply({"event_name": "Renamed again"})
        self.assertEqual(self.event.event_start_time, time(22, 0))
        self.assertEqual(self.event.event_end_time, time(23, 30))

    def test_an_edit_from_another_country_cannot_re_stamp_it(self):
        """The reported scenario, end to end. An assistant in Lagos edits an unrelated field on a
        Johannesburg event. Before the fix the form appended Africa/Lagos here and the event moved
        an hour for everybody."""
        assistant = User.objects.create(
            username="lagosassistant", email="lagos@x.com", full_name="Lagos", role="admin",
            password="x", country="Nigeria", uid=None,
        )
        UserProfile.objects.create(user=assistant)
        apply_event_writes(
            self.event, {"event_name": "Edited from Lagos"}, actor=assistant
        )
        self.event.save()
        self.event.refresh_from_db()
        self.assertEqual(self.event.timezone, JOBURG)

    # ── the other direction: it is still settable ON PURPOSE ──────────────────────────────────
    def test_sending_a_timezone_DELIBERATELY_still_changes_it(self):
        """Not sending it must mean "leave alone", NOT "read only". An organizer who genuinely moves
        an event has to be able to say so."""
        self._apply({"timezone": LAGOS})
        self.assertEqual(self.event.timezone, LAGOS)

    def test_a_LEGACY_event_with_no_timezone_does_not_gain_one_from_an_edit(self):
        """13 of 35 events on the prod clone have no timezone at all. An unrelated edit must not
        invent one for them, because a guessed timezone reads as fact on the public page."""
        self.event.timezone = None
        self.event.save()
        self._apply({"event_name": "Legacy edit"})
        self.assertIsNone(self.event.timezone)

    def test_clearing_it_on_purpose_stores_NULL_not_an_empty_string(self):
        """The column is nullable and the contract's cleaner exists for exactly this: an empty
        string would be a third state that every reader would have to know about."""
        self._apply({"timezone": ""})
        self.assertIsNone(self.event.timezone)
