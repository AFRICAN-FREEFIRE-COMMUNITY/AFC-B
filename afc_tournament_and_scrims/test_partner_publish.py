"""
afc_tournament_and_scrims/test_partner_publish.py - an event is published to partners when it
finishes (owner 2026-10-09, inbox #201).

Pins the two doors to "completed" (complete_event_core, and edit_event moving the status) and the
three things that must NOT publish: a draft, an event already completed (so a withdrawal is not
overruled), and an edit that leaves the status alone.
"""
import json
from datetime import date, time, timedelta

from django.test import Client, TestCase, override_settings

from afc_auth.models import SessionToken, User, UserProfile
from afc_tournament_and_scrims.models import Event
from afc_tournament_and_scrims.views import complete_event_core


def _event(**kw):
    fields = dict(
        event_name="Publish Me Cup", competition_type="tournament", participant_type="squad",
        event_type="online", event_mode="single", max_teams_or_players=12, number_of_stages=1,
        start_date=date.today() - timedelta(days=3), end_date=date.today() - timedelta(days=2),
        registration_open_date=date.today() - timedelta(days=10),
        registration_end_date=date.today() - timedelta(days=5),
        event_start_time=time(18, 0), event_end_time=time(22, 0),
        prizepool="0", event_rules="Rules.", event_description="A cup.", event_status="ongoing",
        # is_draft defaults to TRUE on the model; a real, live event is not a draft.
        is_draft=False,
    )
    fields.update(kw)
    return Event.objects.create(**fields)


class PublishOnCompletionTests(TestCase):
    def test_completing_publishes(self):
        ev = _event()
        self.assertTrue(complete_event_core(ev, None, source="auto-date"))
        ev.refresh_from_db()
        self.assertEqual(ev.event_status, "completed")
        self.assertTrue(ev.partner_published)

    def test_a_draft_is_never_published(self):
        ev = _event(is_draft=True)
        complete_event_core(ev, None, source="auto-date")
        ev.refresh_from_db()
        self.assertFalse(ev.partner_published)

    def test_a_withdrawal_is_not_overruled(self):
        """An admin withdrew a finished event. The sweep runs again: it stays withdrawn,
        because complete_event_core does nothing for an event already completed."""
        ev = _event(event_status="completed", partner_published=False)
        self.assertFalse(complete_event_core(ev, None, source="auto-date"))
        ev.refresh_from_db()
        self.assertFalse(ev.partner_published)


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class PublishFromEditFormTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(
            username="pubeditor", email="pubeditor@x.com", full_name="Pub Editor", role="admin",
            password="x", country="Nigeria")
        UserProfile.objects.create(user=self.admin)
        self.token = SessionToken.objects.create(user=self.admin, token="tok_pubeditor").token

    def _edit(self, ev, **payload):
        return Client().post(
            "/events/edit-event/", data=json.dumps({"event_id": ev.event_id, **payload}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_moving_the_status_to_completed_publishes(self):
        ev = _event(creator=self.admin)
        resp = self._edit(ev, event_status="completed")
        self.assertEqual(resp.status_code, 200, resp.content)
        ev.refresh_from_db()
        self.assertEqual(ev.event_status, "completed")
        self.assertTrue(ev.partner_published)

    def test_an_edit_that_leaves_the_status_alone_does_not_publish(self):
        ev = _event(creator=self.admin, event_status="completed", partner_published=False)
        resp = self._edit(ev, event_description="New words.")
        self.assertEqual(resp.status_code, 200, resp.content)
        ev.refresh_from_db()
        self.assertFalse(ev.partner_published)
