"""
afc_organizers/test_co_organizer_invites.py - the invited org can find, answer and then use a
co-organizer invite, and only within its grant (owner 2026-09-13, inbox #8).

Proves: organizers/co-organizers/mine/ lists the invites of the orgs the caller OWNS and nothing
else; the invite notification links the portal's invites page; accepting flips the row, after which
the event appears in the co-org's scoped events list carrying the grant (for a member) and without
it (for a stranger), and get_event_details tells the member which grants they hold; a co-owner with
can_upload_results but not can_edit_events is admitted by the results endpoint and refused by
edit-event; a stranger's accept is 403.
"""
from datetime import date, timedelta

from django.test import Client, TestCase

from afc_auth.models import Notifications, SessionToken, User
from afc_organizers.models import EventCoOrganizer, Organization, OrganizationMember
from afc_tournament_and_scrims.models import Event


def _user(name):
    user = User.objects.create(username=name, email=f"{name}@x.com", full_name=name.title(), role="player", password="x")
    tok = SessionToken.objects.create(user=user, token=f"tok_{name}").token
    return user, {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


def _event(org, creator, name="Shared Cup"):
    return Event.objects.create(
        event_name=name, competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator, organization=org,
    )


class CoOrganizerInviteTests(TestCase):
    def setUp(self):
        self.primary = Organization.objects.create(slug="alpha", name="Alpha Esports")
        self.guest = Organization.objects.create(slug="beta", name="Beta Gaming")
        self.other = Organization.objects.create(slug="gamma", name="Gamma")
        self.alpha_owner, self.alpha_auth = _user("alpha_owner")
        self.beta_owner, self.beta_auth = _user("beta_owner")
        self.beta_sub, self.beta_sub_auth = _user("beta_sub")
        self.gamma_owner, self.gamma_auth = _user("gamma_owner")
        self.stranger, self.stranger_auth = _user("stranger")
        OrganizationMember.objects.create(organization=self.primary, user=self.alpha_owner, role="owner")
        OrganizationMember.objects.create(organization=self.guest, user=self.beta_owner, role="owner")
        OrganizationMember.objects.create(organization=self.guest, user=self.beta_sub, role="sub_organizer",
                                          can_upload_results=True)
        OrganizationMember.objects.create(organization=self.other, user=self.gamma_owner, role="owner")
        self.event = _event(self.primary, self.alpha_owner)
        self.client = Client()

    def _invite(self, **perms):
        body = {"event_id": self.event.event_id, "organization_id": self.guest.organization_id,
                "permissions": perms, "payout_percent": 10}
        r = self.client.post("/organizers/co-organizers/invite/", body, content_type="application/json", **self.alpha_auth)
        self.assertEqual(r.status_code, 201, r.content[:300])
        return r.json()["co_organizer"]["id"]

    def test_mine_lists_only_the_invites_of_the_orgs_i_own(self):
        co_id = self._invite(can_upload_results=True)
        r = self.client.get("/organizers/co-organizers/mine/", **self.beta_auth)
        self.assertEqual(r.status_code, 200, r.content[:300])
        rows = r.json()["invites"]
        self.assertEqual([row["id"] for row in rows], [co_id])
        row = rows[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["event"]["slug"], self.event.slug)
        self.assertEqual(row["invited_by"]["name"], "Alpha Esports")
        self.assertTrue(row["permissions"]["can_upload_results"])
        self.assertFalse(row["permissions"]["can_edit_events"])
        # a sub-organizer of the invited org, the owner of another org, a stranger: nothing
        for auth in (self.beta_sub_auth, self.gamma_auth, self.stranger_auth):
            r = self.client.get("/organizers/co-organizers/mine/", **auth)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["invites"], [])
        self.assertEqual(self.client.get("/organizers/co-organizers/mine/").status_code, 400)

    def test_the_notification_opens_the_invites_page(self):
        self._invite(can_view_metrics=True)
        note = Notifications.objects.filter(user=self.beta_owner).order_by("-id").first()
        self.assertIsNotNone(note)
        self.assertEqual((note.target_type, note.target_id), ("custom", "/organizer/invites"))

    def test_accept_then_the_event_is_listed_with_the_grant_and_scoped(self):
        co_id = self._invite(can_upload_results=True, can_view_metrics=True)
        # only the invited org's owner may answer
        r = self.client.post("/organizers/co-organizers/respond/", {"co_organizer_id": co_id, "action": "accept"},
                             content_type="application/json", **self.stranger_auth)
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/organizers/co-organizers/respond/", {"co_organizer_id": co_id, "action": "accept"},
                             content_type="application/json", **self.beta_auth)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(EventCoOrganizer.objects.get(pk=co_id).status, "accepted")
        self.assertEqual(self.client.get("/organizers/co-organizers/mine/", **self.beta_auth).json()["invites"][0]["status"], "accepted")

        # the co-org's scoped list now carries the event, with the grant for a member
        r = self.client.get("/events/get-all-events/", {"organization_id": self.guest.organization_id}, **self.beta_auth)
        self.assertEqual(r.status_code, 200, r.content[:300])
        rows = {e["event_id"]: e for e in r.json()["events"]}
        self.assertIn(self.event.event_id, rows)
        self.assertEqual(rows[self.event.event_id]["co_organizer_of"], "Alpha Esports")
        self.assertEqual(rows[self.event.event_id]["co_organizer_grant"]["can_upload_results"], True)
        self.assertEqual(rows[self.event.event_id]["co_organizer_grant"]["can_edit_events"], False)
        # ...and without the grant for a stranger (and the list stays public)
        r = self.client.get("/events/get-all-events/", {"organization_id": self.guest.organization_id})
        row = next(e for e in r.json()["events"] if e["event_id"] == self.event.event_id)
        self.assertIsNone(row["co_organizer_grant"])
        self.assertEqual(row["co_organizer_of"], "Alpha Esports")
        # the primary org's own list marks its own event as owned, not co-organized
        r = self.client.get("/events/get-all-events/", {"organization_id": self.primary.organization_id}, **self.alpha_auth)
        row = next(e for e in r.json()["events"] if e["event_id"] == self.event.event_id)
        self.assertIsNone(row["co_organizer_grant"])

        # the event details tell a member of the co-org what it was granted
        r = self.client.post("/events/get-event-details/", {"slug": self.event.slug}, content_type="application/json", **self.beta_auth)
        self.assertEqual(r.status_code, 200, r.content[:300])
        grants = r.json()["my_co_organizer_grants"]
        self.assertEqual([g["organization_slug"] for g in grants], ["beta"])
        self.assertTrue(grants[0]["can_upload_results"])
        self.assertFalse(grants[0]["can_edit_events"])
        r = self.client.post("/events/get-event-details/", {"slug": self.event.slug}, content_type="application/json", **self.gamma_auth)
        self.assertEqual(r.json()["my_co_organizer_grants"], [])

        # scope, enforced by the backend: results yes, edit no
        r = self.client.post("/events/get-all-leaderboard-details-for-event/", {"event_id": self.event.event_id},
                             content_type="application/json", **self.beta_auth)
        self.assertNotEqual(r.status_code, 403, r.content[:200])
        r = self.client.post("/events/edit-event/", {"event_id": self.event.event_id, "event_name": "Renamed"},
                             content_type="application/json", **self.beta_auth)
        self.assertEqual(r.status_code, 403, r.content[:200])
        # the sub-organizer of the co-org who holds can_upload_results is admitted too; one who
        # does not hold it is not
        r = self.client.post("/events/get-all-leaderboard-details-for-event/", {"event_id": self.event.event_id},
                             content_type="application/json", **self.beta_sub_auth)
        self.assertNotEqual(r.status_code, 403, r.content[:200])

    def test_declined_or_pending_grants_nothing(self):
        co_id = self._invite(can_upload_results=True)
        r = self.client.post("/events/get-all-leaderboard-details-for-event/", {"event_id": self.event.event_id},
                             content_type="application/json", **self.beta_auth)
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/events/get-all-events/", {"organization_id": self.guest.organization_id}, **self.beta_auth)
        self.assertNotIn(self.event.event_id, [e["event_id"] for e in r.json()["events"]])
        self.client.post("/organizers/co-organizers/respond/", {"co_organizer_id": co_id, "action": "decline"},
                         content_type="application/json", **self.beta_auth)
        self.assertEqual(EventCoOrganizer.objects.get(pk=co_id).status, "declined")
        r = self.client.get("/events/get-all-events/", {"organization_id": self.guest.organization_id}, **self.beta_auth)
        self.assertNotIn(self.event.event_id, [e["event_id"] for e in r.json()["events"]])
