# afc_tournament_and_scrims/test_registration_window_tz.py
# ──────────────────────────────────────────────────────────────────────────────
# Regression tests for two owner-reported bugs (2026-08-03 backlog):
#
# ITEM 38 - "Registration open time is being evaluated per viewer timezone: an organizer opened
#   CTL scrims registration and users in Ethiopia still saw registration closed. When it opens it
#   must open for everyone at once."
#
#   A registration window is ONE instant in time. Two things were wrong:
#     (a) the backend gate in register_for_event compared date.today() (the SERVER's local date)
#         against the two DATE fields and ignored registration_start_time / registration_end_time
#         entirely, so an "opens 18:00" event accepted entries from 00:00; and
#     (b) the frontend rebuilt the window from the naive date+time strings, which JavaScript parses
#         in the VIEWER's browser timezone, so the boundary moved by each viewer's UTC offset.
#   registration_window_instants() / registration_is_open() now resolve the window against the
#   EVENT's own timezone, and the resolved instants are published to the frontend.
#
#   The tests below assert the decision is IDENTICAL no matter what timezone the viewer (or the
#   server process) is in - that is the whole point of the fix.
#
# ITEM 27 - "Duplicate an event, edit it, set a future start date, publish, and it still shows
#   Event completed."
#
#   clone_event copies the source's start_date/end_date, so a clone carries PAST dates. The date
#   sweep stamps any published past-end event with a stored event_status="completed", and its
#   sibling rule (reset future-dated events to "upcoming") explicitly EXCLUDES "completed" - so
#   nothing ever cleared it, and effective_event_status() short-circuited on the stale raw value
#   forever. It now refuses to report "completed" for an event whose START instant is still in the
#   future, and register_for_event gates on the EFFECTIVE status so such an event accepts entries.
# ──────────────────────────────────────────────────────────────────────────────
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from afc_auth.models import User

from .models import Event
from .views import (
    effective_event_status,
    registration_is_open,
    registration_window_instants,
    validate_placements,
)


def _make_event(**overrides):
    """Minimal published Event. Defaults put registration OPEN around 'now' in Africa/Lagos."""
    creator = overrides.pop("creator")
    today = date.today()
    fields = dict(
        competition_type="tournament",
        participant_type="squad",
        event_type="internal",
        max_teams_or_players=16,
        event_name="TZ Window Cup",
        event_mode="virtual",
        start_date=today + timedelta(days=7),
        end_date=today + timedelta(days=8),
        registration_open_date=today,
        registration_end_date=today,
        registration_start_time=time(18, 0),
        registration_end_time=time(20, 0),
        timezone="Africa/Lagos",  # UTC+1, no DST
        prizepool="100 NGN",
        prizepool_cash_value=100,
        prize_currency="NGN",
        prize_distribution={"1": "100"},
        event_rules="No cheating",
        event_status="upcoming",
        registration_link="https://example.com/reg",
        tournament_tier="tier_1",
        number_of_stages=1,
        creator=creator,
        is_draft=False,
        is_public=True,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


class RegistrationWindowInstantTests(TestCase):
    """Item 38: the window resolves to one global instant, independent of server/viewer tz."""

    def setUp(self):
        self.creator = User.objects.create_user(
            username="tzcreator", email="tzcreator@example.com", password="x", role="admin"
        )

    def test_window_resolves_in_event_timezone_not_server_timezone(self):
        """18:00 Lagos is 17:00 UTC, whatever timezone the Django process is running in."""
        event = _make_event(creator=self.creator)
        open_dt, close_dt = registration_window_instants(event)

        self.assertEqual(open_dt.astimezone(ZoneInfo("UTC")).hour, 17)   # 18:00 WAT
        self.assertEqual(close_dt.astimezone(ZoneInfo("UTC")).hour, 19)  # 20:00 WAT

    def test_same_verdict_for_every_viewer_timezone(self):
        """THE item-38 assertion.

        One instant, 19:00 Lagos (inside an 18:00-20:00 window). Rendered as a wall clock that is
        21:00 in Addis Ababa - PAST the '20:00' the Ethiopian browser used to compare against, which
        is exactly why those users saw 'Registration Closed'. The verdict must be open for all.
        """
        event = _make_event(creator=self.creator)
        instant = datetime.combine(
            event.registration_open_date, time(19, 0), tzinfo=ZoneInfo("Africa/Lagos")
        )

        viewer_zones = [
            "Africa/Lagos",       # event tz            UTC+1
            "Africa/Addis_Ababa",  # the reported bug   UTC+3
            "UTC",
            "America/New_York",   # far west            UTC-4/-5
            "Asia/Tokyo",         # far east            UTC+9
            "Pacific/Kiritimati",  # extreme east       UTC+14
        ]
        for zone in viewer_zones:
            with self.subTest(viewer=zone):
                # Same absolute instant, expressed in the viewer's zone. The verdict must not move.
                self.assertTrue(
                    registration_is_open(event, now=instant.astimezone(ZoneInfo(zone))),
                    f"registration must read OPEN for a viewer in {zone}",
                )

    def test_closed_for_every_viewer_timezone_after_close_instant(self):
        """The mirror case: one instant past the close boundary is closed for everyone."""
        event = _make_event(creator=self.creator)
        instant = datetime.combine(
            event.registration_open_date, time(20, 1), tzinfo=ZoneInfo("Africa/Lagos")
        )
        for zone in ["Africa/Lagos", "Africa/Addis_Ababa", "UTC", "America/New_York", "Asia/Tokyo"]:
            with self.subTest(viewer=zone):
                self.assertFalse(registration_is_open(event, now=instant.astimezone(ZoneInfo(zone))))

    def test_time_of_day_is_honoured_not_just_the_date(self):
        """Before the fix the gate was date-only, so 09:00 on the open date wrongly counted as open."""
        event = _make_event(creator=self.creator)
        lagos = ZoneInfo("Africa/Lagos")
        before = datetime.combine(event.registration_open_date, time(9, 0), tzinfo=lagos)
        during = datetime.combine(event.registration_open_date, time(18, 30), tzinfo=lagos)

        self.assertFalse(registration_is_open(event, now=before))
        self.assertTrue(registration_is_open(event, now=during))

    def test_missing_times_span_the_whole_day_in_event_tz(self):
        """Legacy events with no times keep the old date-only behaviour, but tz-correct.

        The close boundary in particular must be END of the closing day: the frontend previously
        fell back to UTC midnight, which shut registration at the very START of the end date.
        """
        event = _make_event(
            creator=self.creator, registration_start_time=None, registration_end_time=None
        )
        lagos = ZoneInfo("Africa/Lagos")
        open_dt, close_dt = registration_window_instants(event)

        self.assertEqual(open_dt.astimezone(lagos).hour, 0)
        self.assertEqual(close_dt.astimezone(lagos).hour, 23)
        self.assertTrue(
            registration_is_open(
                event,
                now=datetime.combine(event.registration_end_date, time(23, 30), tzinfo=lagos),
            ),
            "a time-less window must stay open until the end of its closing day",
        )

    @override_settings(TIME_ZONE="America/New_York")
    def test_server_timezone_does_not_change_the_verdict(self):
        """The old gate used date.today() (server-local). Moving the server must change nothing."""
        event = _make_event(creator=self.creator)
        instant = datetime.combine(
            event.registration_open_date, time(19, 0), tzinfo=ZoneInfo("Africa/Lagos")
        )
        self.assertTrue(registration_is_open(event, now=instant))

    def test_legacy_event_without_timezone_falls_back_consistently(self):
        """timezone is NULL on older events; every viewer must still resolve the same instant."""
        event = _make_event(creator=self.creator, timezone=None)
        open_a, close_a = registration_window_instants(event)
        open_b, close_b = registration_window_instants(Event.objects.get(pk=event.pk))
        self.assertEqual(open_a, open_b)
        self.assertEqual(close_a, close_b)


class DuplicatedEventStatusTests(TestCase):
    """Item 27: a stale stored 'completed' must not survive a move to future dates."""

    def setUp(self):
        self.creator = User.objects.create_user(
            username="clonecreator", email="clonecreator@example.com", password="x", role="admin"
        )

    def test_future_dated_event_stamped_completed_reads_upcoming(self):
        """The reported bug: duplicate, set a future start date, publish, still shows completed."""
        today = date.today()
        event = _make_event(
            creator=self.creator,
            event_status="completed",              # stale stamp inherited via the date sweep
            start_date=today + timedelta(days=10),  # owner moved it into the future
            end_date=today + timedelta(days=11),
        )
        self.assertEqual(effective_event_status(event), "upcoming")

    def test_genuinely_finished_event_stays_completed(self):
        """Control: a real past event must NOT be re-opened by the guard above."""
        today = date.today()
        event = _make_event(
            creator=self.creator,
            event_status="completed",
            start_date=today - timedelta(days=10),
            end_date=today - timedelta(days=9),
        )
        self.assertEqual(effective_event_status(event), "completed")

    def test_ongoing_event_stamped_completed_stays_completed(self):
        """An event that started in the past keeps its completed stamp (only FUTURE ones are freed)."""
        today = date.today()
        event = _make_event(
            creator=self.creator,
            event_status="completed",
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=1),
        )
        self.assertEqual(effective_event_status(event), "completed")

    def test_cancelled_is_untouched(self):
        """A cancelled future event must stay cancelled, not be rescued into 'upcoming'."""
        today = date.today()
        event = _make_event(
            creator=self.creator,
            event_status="cancelled",
            start_date=today + timedelta(days=10),
            end_date=today + timedelta(days=11),
        )
        self.assertEqual(effective_event_status(event), "cancelled")


class PlacementMessageTests(TestCase):
    """Item 19: the manual-entry rejection must explain the problem AND the remedy.

    The owner reported the form "sometimes says placement not even" and asked whether it is a bug.
    The rule itself is correct (a map has one 1st, one 2nd, and so on), but the old copy only
    restated the condition, so organizers read it as the form misbehaving. Two genuine defects sat
    underneath it, both covered below: blank cells arrive as 0 and collide, and the team path
    compared raw payload values so "2" and 2 counted as different placements.
    """

    def test_duplicate_placements_are_named(self):
        msg = validate_placements([1, 2, 2, 4], noun="team")
        self.assertIsNotNone(msg)
        self.assertIn("same finishing position", msg)
        self.assertIn("2", msg)  # names the offending position

    def test_blank_cells_collide_as_zero_and_are_explained(self):
        """MatchResultsGrid coerces an empty box with `parseInt(v) || 0`, so two blanks both send 0."""
        msg = validate_placements([1, 0, 0], noun="team")
        self.assertIsNotNone(msg)
        self.assertIn("left empty", msg)

    def test_string_and_int_duplicates_are_caught(self):
        """REAL BUG: set() on raw values let "2" and 2 through as distinct placements."""
        self.assertIsNotNone(validate_placements([1, "2", 2], noun="team"))

    def test_string_placements_still_validate_cleanly(self):
        """A grid that posts every placement as a string must still be accepted."""
        self.assertIsNone(validate_placements(["1", "2", "3"], noun="team"))

    def test_missing_winner_explains_the_fix(self):
        msg = validate_placements([2, 3, 4], noun="team")
        self.assertIsNotNone(msg)
        self.assertIn("no winner recorded", msg)

    def test_blank_placement_is_reported_as_missing(self):
        msg = validate_placements([1, None, 3], noun="player")
        self.assertIsNotNone(msg)
        self.assertIn("finishing position", msg)
        self.assertIn("player", msg)  # copy follows the participant type

    def test_valid_placements_pass(self):
        self.assertIsNone(validate_placements([1, 2, 3, 4], noun="team"))

    def test_gaps_are_allowed(self):
        """Placements need not be contiguous: teams that did not play are simply absent."""
        self.assertIsNone(validate_placements([1, 3, 7, 99], noun="team"))
