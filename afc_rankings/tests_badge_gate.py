"""Only Verified and AFC Partner organizations' results count for the rankings (owner 16 Nov 2026, AFC inbox #588;
open organizations plan 1 Task 3).

The gate is the organization's badge AND the per-event flag Event.rankings_verified (the review lever the reports
flow switches off). A Community organization's event never counts, flagged or not; a new event of a Verified or
Partner organization is created with the flag on; AFC's own events always count. Demoting an organization stops its
results counting from the next recalculation and promoting it brings them back: nothing is deleted.
"""
import datetime
import json

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_organizers.models import Organization
from afc_rankings.aggregation import _unverified_org_event_ids
from afc_tournament_and_scrims.models import Event


def make_event(name, org=None, **over):
    kwargs = dict(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=datetime.date(2026, 7, 1), end_date=datetime.date(2026, 9, 6),
        registration_open_date=datetime.date(2026, 6, 1), registration_end_date=datetime.date(2026, 6, 30),
        prizepool="0", event_rules="rules", event_status="completed",
        registration_link="https://example.com/reg", number_of_stages=1, organization=org,
    )
    kwargs.update(over)
    return Event.objects.create(**kwargs)


def create_payload(name, org=None, **over):
    """The smallest create-event body the endpoint takes (shape of tests_create_event_perf._payload)."""
    today = datetime.date.today().isoformat()
    stages = [{"stage_name": "Finals", "start_date": today, "end_date": today, "number_of_groups": 1,
               "stage_format": "br - normal", "teams_qualifying_from_stage": 0, "stage_order": 1,
               "groups": [{"group_name": "Finals", "playing_date": today, "playing_time": "18:00",
                           "teams_qualifying": 0, "match_count": 1, "match_maps": ["bermuda"]}]}]
    body = {"competition_type": "tournament", "participant_type": "squad", "event_type": "internal",
            "max_teams_or_players": 12, "event_name": name, "event_mode": "virtual", "start_date": today,
            "end_date": today, "registration_open_date": today, "registration_end_date": today, "prizepool": "0",
            "event_rules": "rules", "registration_link": "https://x.com/r", "number_of_stages": 1, "is_draft": "false",
            "event_start_time": "18:00", "event_end_time": "21:00", "registration_start_time": "09:00",
            "registration_end_time": "17:00", "stages": json.dumps(stages)}
    if org is not None:
        body["organization_id"] = org.pk
    body.update(over)
    return body


class AdminClient:
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create(username="gate_admin", email="ga@x.com", full_name="Gate Admin",
                                         role="admin", password="x")
        SessionToken.objects.create(user=self.admin, token="gate-token",
                                    expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        self.auth = {"HTTP_AUTHORIZATION": "Bearer gate-token"}


class BadgeGateTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(slug="kopa", name="Kopa Esports")      # verified by default

    def excluded(self, *events):
        return _unverified_org_event_ids([e.event_id for e in events])

    def test_community_event_never_counts_even_if_flagged(self):
        Organization.objects.filter(pk=self.org.pk).update(badge="community")
        ev = make_event("COMMUNITY CUP", org=self.org, rankings_verified=True)
        self.assertEqual(self.excluded(ev), {ev.event_id})

    def test_verified_event_counts_when_flagged(self):
        flagged = make_event("FLAGGED", org=self.org, rankings_verified=True)
        unflagged = make_event("UNFLAGGED", org=self.org, rankings_verified=False)
        self.assertEqual(self.excluded(flagged, unflagged), {unflagged.event_id})

    def test_partner_event_counts_when_flagged(self):
        Organization.objects.filter(pk=self.org.pk).update(badge="partner")
        ev = make_event("PARTNER CUP", org=self.org, rankings_verified=True)
        self.assertEqual(self.excluded(ev), set())

    def test_demote_then_promote(self):
        ev = make_event("SEASON", org=self.org, rankings_verified=True)
        Organization.objects.filter(pk=self.org.pk).update(badge="community")
        self.assertEqual(self.excluded(ev), {ev.event_id})
        Organization.objects.filter(pk=self.org.pk).update(badge="verified")
        self.assertEqual(self.excluded(ev), set())
        self.assertTrue(Event.objects.filter(pk=ev.pk, rankings_verified=True).exists())

    def test_afc_event_always_counts(self):
        ev = make_event("AFC CUP", rankings_verified=False)
        self.assertEqual(self.excluded(ev), set())


class NewEventFlagTests(AdminClient, TestCase):
    def setUp(self):
        AdminClient.setUp(self)
        self.org = Organization.objects.create(slug="kopa", name="Kopa Esports")

    def create(self, name, org):
        r = self.client.post("/events/create-event/", create_payload(name, org), **self.auth)
        self.assertIn(r.status_code, (200, 201), r.content[:400])
        return Event.objects.get(event_name=name)

    def test_new_event_of_verified_org_is_flagged(self):
        self.assertTrue(self.create("VERIFIED NEW", self.org).rankings_verified)

    def test_new_event_of_community_org_is_not_flagged(self):
        Organization.objects.filter(pk=self.org.pk).update(badge="community")
        self.assertFalse(self.create("COMMUNITY NEW", self.org).rankings_verified)
