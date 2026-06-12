"""
afc_sponsors.tests — endpoint tests for the sponsor-system P1.

Covers: sponsor-admin gating, sponsor CRUD, member add (+ notification + reactivation) /
remove, event attach/detach, the MEMBER-SCOPED portal reads (mine, events, submissions with
the ydpay-only rule), the privacy guarantee (no emails anywhere), and the CSV export.

Drives the real HTTP endpoints with Bearer SessionTokens, mirroring the afc_leaderboard test
idiom. Run: python manage.py test afc_sponsors
"""
import json
from datetime import date, timedelta

from django.test import TestCase, Client

from afc_auth.models import User, SessionToken, Roles, UserRoles, Notifications
from afc_tournament_and_scrims.models import Event, RegisteredCompetitors

from .models import Sponsor, SponsorMember, EventSponsorship


def make_user(username, role="player", granular=None):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x",
    )
    for rn in (granular or []):
        r, _ = Roles.objects.get_or_create(role_name=rn)
        UserRoles.objects.create(user=u, role=r)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def make_event(name, creator, participant_type="solo"):
    """Minimal Event for attachment tests (mirrors test_esport_media's factory: `creator` FK +
    the required date/count fields; solo keeps the registrant path simple)."""
    return Event.objects.create(
        event_name=name,
        competition_type="tournament",
        participant_type=participant_type,
        event_type="online",
        max_teams_or_players=10,
        event_mode="single",
        start_date=date.today() + timedelta(days=7),
        end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1,
        creator=creator,
    )


class SponsorAdminCrudTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_user("sadmin", role="admin")
        # A granular-only sponsor admin (base role player) must ALSO pass the gate.
        self.granular, self.granular_tok = make_user("granular", granular=["sponsor_admin"])
        self.player, self.player_tok = make_user("regular")

    def _create(self, tok, name="ydpay"):
        return self.client.post(
            "/sponsors/create/", data=json.dumps({"name": name, "website": "https://ydpay.app"}),
            content_type="application/json", **bearer(tok),
        )

    def test_admin_creates_sponsor_with_slug(self):
        resp = self._create(self.admin_tok)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["sponsor"]["slug"], "ydpay")

    def test_granular_sponsor_admin_allowed(self):
        self.assertEqual(self._create(self.granular_tok).status_code, 201)

    def test_regular_user_forbidden(self):
        self.assertEqual(self._create(self.player_tok).status_code, 403)
        self.assertEqual(self.client.get("/sponsors/", **bearer(self.player_tok)).status_code, 403)

    def test_duplicate_name_rejected(self):
        self._create(self.admin_tok)
        self.assertEqual(self._create(self.admin_tok, name="YDPAY").status_code, 400)

    def test_list_and_edit(self):
        sid = self._create(self.admin_tok).json()["sponsor"]["id"]
        listing = self.client.get("/sponsors/", {"q": "ydp"}, **bearer(self.admin_tok)).json()
        self.assertEqual(listing["total_count"], 1)
        resp = self.client.patch(
            f"/sponsors/{sid}/edit/", data=json.dumps({"status": "suspended"}),
            content_type="application/json", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.json()["sponsor"]["status"], "suspended")


class SponsorMembershipTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_user("sadmin2", role="admin")
        self.member, self.member_tok = make_user("timuwa")
        self.sponsor = Sponsor.objects.create(name="ydpay", slug="ydpay", created_by=self.admin)

    def _add(self, user_id, role="member"):
        return self.client.post(
            f"/sponsors/{self.sponsor.id}/members/add/",
            data=json.dumps({"user_id": user_id, "role": role}),
            content_type="application/json", **bearer(self.admin_tok),
        )

    def test_add_member_notifies_and_dupes_rejected(self):
        resp = self._add(self.member.user_id)
        self.assertEqual(resp.status_code, 201)
        # The access notification (also the FE coachmark trigger) landed.
        self.assertTrue(Notifications.objects.filter(
            user=self.member, notification_type="sponsor_access",
        ).exists())
        self.assertEqual(self._add(self.member.user_id).status_code, 400)

    def test_remove_then_readd_reactivates(self):
        mid = self._add(self.member.user_id).json()["member"]["member_id"]
        self.client.delete(f"/sponsors/{self.sponsor.id}/members/{mid}/", **bearer(self.admin_tok))
        self.assertEqual(SponsorMember.objects.get(id=mid).status, "removed")
        resp = self._add(self.member.user_id, role="owner")
        self.assertEqual(resp.status_code, 201)
        m = SponsorMember.objects.get(id=mid)
        self.assertEqual((m.status, m.role), ("active", "owner"))


class SponsorPortalTests(TestCase):
    """The scoping rules: members see ONLY their sponsor; reads are privacy-stripped."""

    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_user("sadmin3", role="admin")
        self.member, self.member_tok = make_user("timuwa2")
        self.outsider, self.outsider_tok = make_user("outsider")
        self.sponsor = Sponsor.objects.create(name="ydpay", slug="ydpay", created_by=self.admin)
        self.other = Sponsor.objects.create(name="FreeMobile", slug="freemobile", created_by=self.admin)
        SponsorMember.objects.create(sponsor=self.sponsor, user=self.member)
        self.event = make_event("Dynasty Cup Nigeria", self.admin)
        EventSponsorship.objects.create(event=self.event, sponsor=self.sponsor)
        # One solo registrant with a submitted ydpay value.
        self.p1, _ = make_user("layott")
        RegisteredCompetitors.objects.create(
            event=self.event, user=self.p1, status="registered", user_id_from_sponsor="ydp-88291",
        )

    def test_mine_lists_only_my_sponsors(self):
        mine = self.client.get("/sponsors/mine/", **bearer(self.member_tok)).json()
        self.assertEqual([s["name"] for s in mine["results"]], ["ydpay"])
        self.assertEqual(
            self.client.get("/sponsors/mine/", **bearer(self.outsider_tok)).json()["total_count"], 0,
        )

    def test_member_reads_events_and_submissions_without_emails(self):
        events = self.client.get(f"/sponsors/{self.sponsor.id}/events/", **bearer(self.member_tok)).json()
        self.assertEqual(events["results"][0]["event_name"], "Dynasty Cup Nigeria")
        subs = self.client.get(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/submissions/",
            **bearer(self.member_tok),
        )
        self.assertEqual(subs.status_code, 200)
        body = subs.json()
        self.assertEqual(body["results"][0]["username"], "layott")
        self.assertEqual(body["results"][0]["value"], "ydp-88291")
        # PRIVACY: no email key anywhere in the payload (the legacy endpoint leaked it).
        self.assertNotIn("email", json.dumps(body))

    def test_member_cannot_read_another_sponsor(self):
        self.assertEqual(
            self.client.get(f"/sponsors/{self.other.id}/events/", **bearer(self.member_tok)).status_code,
            403,
        )

    def test_unattached_event_404s(self):
        other_event = make_event("Unrelated Cup", self.admin)
        resp = self.client.get(
            f"/sponsors/{self.sponsor.id}/events/{other_event.event_id}/submissions/",
            **bearer(self.member_tok),
        )
        self.assertEqual(resp.status_code, 404)

    def test_csv_export(self):
        resp = self.client.get(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/submissions/",
            {"csv": "1"}, **bearer(self.member_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        text = resp.content.decode()
        self.assertIn("layott", text)
        self.assertIn("ydp-88291", text)
        self.assertNotIn("@x.com", text)  # privacy holds in the CSV too

    def test_attach_detach(self):
        e2 = make_event("Dynasty Cup Ghana", self.admin)
        resp = self.client.post(
            f"/sponsors/{self.sponsor.id}/events/attach/",
            data=json.dumps({"event_id": e2.event_id}),
            content_type="application/json", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            self.client.delete(
                f"/sponsors/{self.sponsor.id}/events/{e2.event_id}/", **bearer(self.admin_tok),
            ).status_code,
            200,
        )
