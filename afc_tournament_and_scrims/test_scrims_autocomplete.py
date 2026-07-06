"""
Scrims auto-completion (owner 2026-07-06 organizer bug: "I input results for 5 maps but the
scrims stayed ongoing").

Previously scrims were explicitly EXCLUDED from both auto-complete paths:
  - maybe_autocomplete_event (results-based, views.py) early-returned on competition_type=="scrims",
  - close_finished_events (date sweep, tasks.py) `continue`d on scrims.
So a scrims could only be completed manually and stayed "ongoing" forever once its day passed. These
tests pin the new behaviour: a scrims auto-completes once its final stage's maps are all entered, and
does NOT complete while any map is still missing a result.
"""
import datetime

from django.test import TestCase
from django.utils import timezone

from afc_auth.models import User
from afc_tournament_and_scrims.models import Event, Stages, StageGroups, Match
from afc_tournament_and_scrims.views import (
    maybe_autocomplete_event,
    effective_event_status,
    update_event_and_stage_statuses,
)


def _scrims_with_maps(creator, n_maps, all_inputted=True):
    today = datetime.date.today()
    ev = Event.objects.create(
        competition_type="scrims", participant_type="squad", event_type="internal",
        max_teams_or_players=12, event_name="Scrim Day 8", event_mode="virtual",
        start_date=today, end_date=today, registration_open_date=today, registration_end_date=today,
        prizepool="0", event_rules="r", event_status="ongoing", registration_link="https://x",
        number_of_stages=1, creator=creator, is_draft=False,
    )
    st = Stages.objects.create(
        event=ev, stage_name="S", start_date=today, end_date=today, number_of_groups=1,
        stage_format="br - normal", teams_qualifying_from_stage=1,
    )
    g = StageGroups.objects.create(
        stage=st, group_name="G", playing_date=today, playing_time=datetime.time(18, 0),
        teams_qualifying=1, match_count=n_maps,
    )
    for i in range(n_maps):
        Match.objects.create(group=g, match_number=i + 1, match_map="bermuda",
                             result_inputted=all_inputted)
    return ev, g


class ScrimsAutoCompleteTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(
            username="scrim_admin", email="scrim_admin@example.com", full_name="Scrim Admin",
            role="admin", password="x",
        )

    def test_scrims_completes_when_all_maps_entered(self):
        ev, _ = _scrims_with_maps(self.admin, 5, all_inputted=True)
        self.assertTrue(maybe_autocomplete_event(ev, None))
        ev.refresh_from_db()
        self.assertEqual(ev.event_status, "completed")

    def test_scrims_stays_ongoing_with_a_missing_map(self):
        ev, g = _scrims_with_maps(self.admin, 5, all_inputted=True)
        Match.objects.filter(group=g, match_number=1).update(result_inputted=False)
        self.assertFalse(maybe_autocomplete_event(ev, None))
        ev.refresh_from_db()
        self.assertEqual(ev.event_status, "ongoing")

    def test_draft_scrims_never_completes(self):
        ev, _ = _scrims_with_maps(self.admin, 5, all_inputted=True)
        Event.objects.filter(pk=ev.pk).update(is_draft=True)
        ev.refresh_from_db()
        self.assertFalse(maybe_autocomplete_event(ev, None))
        ev.refresh_from_db()
        self.assertEqual(ev.event_status, "ongoing")


class EventStatusDisplayTests(TestCase):
    """effective_event_status: the read-time badge. Regression for the cancelled + timezone fixes
    (owner 2026-07-06)."""

    def setUp(self):
        self.admin = User.objects.create(
            username="disp_admin", email="disp_admin@example.com", full_name="Disp Admin",
            role="admin", password="x",
        )

    def _event(self, **over):
        today = datetime.date.today()
        base = dict(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=12, event_name="Disp Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="r", event_status="ongoing", registration_link="https://x",
            number_of_stages=1, creator=self.admin, is_draft=False,
        )
        base.update(over)
        return Event.objects.create(**base)

    def test_cancelled_event_reads_cancelled(self):
        ev = self._event(event_status="cancelled")
        self.assertEqual(effective_event_status(ev), "cancelled")

    def test_completed_event_reads_completed(self):
        ev = self._event(event_status="completed")
        self.assertEqual(effective_event_status(ev), "completed")

    def test_past_end_reads_completed_in_event_timezone(self):
        # Event ended yesterday: regardless of tz it is over, so the badge must read completed
        # (not "ongoing"). Exercises the end-instant completion branch with a real IANA timezone.
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        ev = self._event(start_date=yesterday, end_date=yesterday, event_status="ongoing",
                         timezone="Africa/Lagos")
        self.assertEqual(effective_event_status(ev), "completed")


class UpdateSweepTests(TestCase):
    """update_event_and_stage_statuses: the scheduled lifecycle sweep. Its completions must route
    through complete_event_core and must NOT re-close a reopened (auto_complete_suppressed) event."""

    def setUp(self):
        self.admin = User.objects.create(
            username="sweep_admin", email="sweep_admin@example.com", full_name="Sweep Admin",
            role="admin", password="x",
        )

    def _past_event(self, suppressed=False):
        d = datetime.date.today() - datetime.timedelta(days=2)
        return Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=12, event_name="Past Cup", event_mode="virtual",
            start_date=d, end_date=d, registration_open_date=d, registration_end_date=d,
            prizepool="0", event_rules="r", event_status="ongoing", registration_link="https://x",
            number_of_stages=1, creator=self.admin, is_draft=False,
            auto_complete_suppressed=suppressed,
        )

    def test_sweep_completes_past_event(self):
        ev = self._past_event(suppressed=False)
        update_event_and_stage_statuses()
        ev.refresh_from_db()
        self.assertEqual(ev.event_status, "completed")

    def test_sweep_spares_reopened_event(self):
        ev = self._past_event(suppressed=True)
        update_event_and_stage_statuses()
        ev.refresh_from_db()
        self.assertNotEqual(ev.event_status, "completed")
