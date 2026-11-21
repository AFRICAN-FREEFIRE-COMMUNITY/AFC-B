"""Organizers cannot charge an entry fee on AFC for now; only AFC's own events take payment (owner 16 Nov 2026,
AFC inbox #594; open organizations plan 1 Task 5).

create_event refuses a paid organization event; edit_event refuses a free organization event becoming paid. An
organization event that was already paid before this rule stays editable (its players may have paid), and AFC's own
paid events are unchanged.
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_organizers.models import Organization
from afc_rankings.tests_badge_gate import create_payload, make_event
from afc_tournament_and_scrims.models import Event


class OrgNoPaidEntryTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create(username="fee_admin", email="fa@x.com", full_name="Fee Admin",
                                         role="admin", password="x")
        SessionToken.objects.create(user=self.admin, token="fee-token",
                                    expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        self.auth = {"HTTP_AUTHORIZATION": "Bearer fee-token"}
        self.org = Organization.objects.create(slug="kopa", name="Kopa Esports")

    def paid(self):
        return {"registration_type": "paid", "registration_fee": "5", "registration_fee_currency": "USD",
                "paid_terms_accepted": "true"}

    def edit(self, event, **data):
        return self.client.post("/events/edit-event/", {"event_id": event.event_id, **data}, **self.auth)

    def test_org_create_paid_refused(self):
        r = self.client.post("/events/create-event/", create_payload("KOPA PAID", self.org, **self.paid()),
                             **self.auth)
        self.assertEqual((r.status_code, r.json().get("code")), (400, "org_paid_entry_off"))
        self.assertFalse(Event.objects.filter(event_name="KOPA PAID").exists())

    def test_afc_paid_event_still_allowed(self):
        r = self.client.post("/events/create-event/", create_payload("AFC PAID", None, **self.paid()), **self.auth)
        self.assertIn(r.status_code, (200, 201), r.content[:300])
        self.assertEqual(Event.objects.get(event_name="AFC PAID").registration_type, "paid")

    def test_org_edit_free_to_paid_refused(self):
        ev = make_event("KOPA FREE", org=self.org, event_status="upcoming")
        r = self.edit(ev, **self.paid())
        self.assertEqual((r.status_code, r.json().get("code")), (400, "org_paid_entry_off"))
        ev.refresh_from_db()
        self.assertEqual(ev.registration_type, "free")

    def test_org_existing_paid_stays_editable(self):
        ev = make_event("KOPA OLD PAID", org=self.org, event_status="upcoming", registration_type="paid",
                        registration_fee=5)
        r = self.edit(ev, event_name="KOPA OLD PAID RENAMED")
        self.assertEqual(r.status_code, 200, r.content[:300])
        ev.refresh_from_db()
        self.assertEqual((ev.event_name, ev.registration_type), ("KOPA OLD PAID RENAMED", "paid"))
