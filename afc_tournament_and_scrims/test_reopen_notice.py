"""
afc_tournament_and_scrims/test_reopen_notice.py - reopening a FINISHED event does not lock rosters
again, and the answer says so (owner 2026-09-14, inbox #16).

"if an organizer reopens an event past the close date, it should notify them that players will
still be able to change their roster". That is the truthful answer since the roster lock started
asking the clock: an event past its end instant holds nobody, reopened or not. So reopen_event
flags it, and the event-edit Actions tab warns before and after the click.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_reopen_notice
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_tournament_and_scrims.models import Event


def _event(name, start, end, status="completed"):
    return Event.objects.create(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=start, end_date=end,
        registration_open_date=start - datetime.timedelta(days=3),
        registration_end_date=start - datetime.timedelta(days=1),
        prizepool="0", event_rules="r", event_status=status,
        registration_link="https://example.com/r", number_of_stages=1, is_draft=False,
    )


class ReopenSaysRostersStayOpenTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(username="eventboss", email="eb@x.com",
                                         full_name="Event Boss", password="x", role="admin")
        self.token = SessionToken.objects.create(user=self.admin, token="tok_eb").token
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        self.client = Client()

    def _reopen(self, event):
        return self.client.post("/events/reopen-event/", {"event_id": event.event_id},
                                content_type="application/json", **self.auth)

    def test_an_event_past_its_end_warns_that_rosters_stay_open(self):
        today = datetime.date.today()
        ended = _event("LEGACY SCRIMS DAY 30", today - datetime.timedelta(days=12),
                       today - datetime.timedelta(days=12))
        r = self._reopen(ended)
        self.assertEqual(r.status_code, 200, r.content[:300])
        body = r.json()
        self.assertTrue(body["roster_unlocked"])
        self.assertEqual(body["code"], "reopened_roster_unlocked")
        ended.refresh_from_db()
        self.assertEqual(ended.event_status, "ongoing")
        self.assertTrue(ended.auto_complete_suppressed)

    def test_an_event_completed_early_is_not_flagged(self):
        # Finished by hand while its dates still run: reopening this one DOES hold rosters again,
        # so there is nothing to warn about.
        today = datetime.date.today()
        early = _event("FFWS AFRICA FINALS", today - datetime.timedelta(days=1),
                       today + datetime.timedelta(days=5))
        r = self._reopen(early)
        self.assertEqual(r.status_code, 200, r.content[:300])
        body = r.json()
        self.assertFalse(body["roster_unlocked"])
        self.assertEqual(body["code"], "reopened")
