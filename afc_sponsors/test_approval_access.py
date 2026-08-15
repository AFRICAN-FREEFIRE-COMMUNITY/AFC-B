"""
afc_sponsors/test_approval_access.py - who can clear a sponsor's approval queue, the cross-event
queue itself, and inviting a sponsor's contact by email (owner 2026-08-14).

WHY THESE TESTS EXIST
    "Sponsor must approve registrations" could be switched on for an event whose sponsor had no
    member account. Nothing on the platform could then decide those submissions: approving was
    sponsor-only, and AFC staff had no screen that even listed them. The owner's answer was to let
    AFC staff AND the organizer running the event decide, to give both a cross-event queue, and to
    let an admin invite the sponsor's own contact by email so the sponsor can take it over.

COVERS
    - the widened gate: sponsor member, sponsor-admin and the event's organizer may decide; a
      stranger and an organizer of a DIFFERENT org may not;
    - the queue: scoping (you only see what you may decide), the pending-first order, the status
      and event filters, pagination metadata, and the CSV;
    - invites: an address that already has an account becomes a member immediately, an unknown
      address gets a pending invite, a repeat invite is re-sent rather than duplicated, verifying
      the account claims the invite, and a revoked invite claims nothing.

Run: python manage.py test afc_sponsors.test_approval_access
"""
import json
from datetime import date, timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_organizers.models import Organization, OrganizationMember
from afc_tournament_and_scrims.models import Event

from .models import (
    EventSponsorship,
    Sponsor,
    SponsorEngagementSubmission,
    SponsorMember,
    SponsorMemberInvite,
)


def _user(username, role="player", email=None):
    u = User.objects.create(
        username=username, email=email or f"{username}@x.com",
        full_name=username.title(), role=role, password="x",
    )
    return u, SessionToken.objects.create(user=u, token=f"tok_{username}").token


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


ENGAGEMENTS = [{"type": "collect_id", "label": "Sponsor UID"}]


class ApprovalAccessBase(TestCase):
    """An org-run event with a sponsorship, one pending submission, and the cast of callers:
    AFC admin, sponsor member, the event's organizer, an unrelated organizer, a stranger."""

    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = _user("sp_admin", role="admin")
        self.sponsor_staff, self.sponsor_tok = _user("brand_staff")
        self.organizer, self.organizer_tok = _user("org_owner")
        self.other_organizer, self.other_organizer_tok = _user("other_org_owner")
        self.stranger, self.stranger_tok = _user("stranger")
        self.player, _ = _user("registrant")

        self.org = Organization.objects.create(
            name="Runner Org", slug="runner-org", created_by=self.admin, status="active",
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.organizer, role="owner", status="active",
        )
        self.other_org = Organization.objects.create(
            name="Other Org", slug="other-org", created_by=self.admin, status="active",
        )
        OrganizationMember.objects.create(
            organization=self.other_org, user=self.other_organizer, role="owner", status="active",
        )

        self.event = Event.objects.create(
            event_name="Org Cup", competition_type="tournament", participant_type="solo",
            event_type="online", max_teams_or_players=10, event_mode="single",
            start_date=date.today() + timedelta(days=7),
            end_date=date.today() + timedelta(days=8),
            registration_open_date=date.today() - timedelta(days=1),
            registration_end_date=date.today() + timedelta(days=5),
            number_of_stages=1, creator=self.organizer, is_public=True,
            organization=self.org,
        )
        self.sponsor = Sponsor.objects.create(
            name="Brand", slug="brand", created_by=self.admin,
        )
        SponsorMember.objects.create(
            sponsor=self.sponsor, user=self.sponsor_staff, role="owner", status="active",
        )
        self.sp = EventSponsorship.objects.create(
            event=self.event, sponsor=self.sponsor,
            requires_approval=True, engagements=ENGAGEMENTS,
        )
        self.submission = SponsorEngagementSubmission.objects.create(
            sponsorship=self.sp, event=self.event, user=self.player,
            engagement_index=0, payload={"value": "abc-1"}, approval_status="pending",
        )

    def _decide(self, tok, action="approve", reason=""):
        return self.client.post(
            f"/sponsors/submissions/{self.submission.id}/decide/",
            data=json.dumps({"action": action, "reason": reason}),
            content_type="application/json", **bearer(tok),
        )


class WhoCanDecideTests(ApprovalAccessBase):
    def test_sponsor_member_can_decide(self):
        self.assertEqual(self._decide(self.sponsor_tok).status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.approval_status, "approved")

    def test_afc_admin_can_decide(self):
        self.assertEqual(self._decide(self.admin_tok).status_code, 200)

    def test_the_events_organizer_can_decide(self):
        """The queue must never be un-clearable just because the sponsor has no account yet."""
        resp = self._decide(self.organizer_tok)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.decided_by_id, self.organizer.user_id)

    def test_an_unrelated_organizer_cannot_decide(self):
        self.assertEqual(self._decide(self.other_organizer_tok).status_code, 403)

    def test_a_stranger_cannot_decide(self):
        self.assertEqual(self._decide(self.stranger_tok).status_code, 403)

    def test_the_events_organizer_can_read_the_per_event_table(self):
        resp = self.client.get(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/engagement-submissions/",
            **bearer(self.organizer_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["total_count"], 1)


class QueueTests(ApprovalAccessBase):
    def test_admin_sees_the_row_with_its_event_and_sponsor(self):
        resp = self.client.get("/sponsors/queue/engagement-submissions/", **bearer(self.admin_tok))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        row = body["results"][0]
        self.assertEqual(row["event_name"], "Org Cup")
        self.assertEqual(row["sponsor_name"], "Brand")
        self.assertEqual(row["username"], "registrant")
        self.assertEqual(row["approval_status"], "pending")
        # The dropdowns offer exactly what this caller can reach.
        self.assertEqual(len(body["filters"]["events"]), 1)
        self.assertEqual(len(body["filters"]["sponsors"]), 1)

    def test_organizer_sees_only_their_own_events(self):
        mine = self.client.get(
            "/sponsors/queue/engagement-submissions/", **bearer(self.organizer_tok),
        ).json()
        self.assertEqual(mine["total_count"], 1)

        theirs = self.client.get(
            "/sponsors/queue/engagement-submissions/", **bearer(self.other_organizer_tok),
        ).json()
        self.assertEqual(theirs["total_count"], 0)
        self.assertEqual(theirs["filters"]["events"], [])

    def test_a_stranger_sees_nothing(self):
        body = self.client.get(
            "/sponsors/queue/engagement-submissions/", **bearer(self.stranger_tok),
        ).json()
        self.assertEqual(body["total_count"], 0)

    def test_status_and_event_filters(self):
        approved = self.client.get(
            "/sponsors/queue/engagement-submissions/?status=approved", **bearer(self.admin_tok),
        ).json()
        self.assertEqual(approved["total_count"], 0)

        by_event = self.client.get(
            f"/sponsors/queue/engagement-submissions/?event={self.event.event_id}",
            **bearer(self.admin_tok),
        ).json()
        self.assertEqual(by_event["total_count"], 1)

        wrong_event = self.client.get(
            "/sponsors/queue/engagement-submissions/?event=999999", **bearer(self.admin_tok),
        ).json()
        self.assertEqual(wrong_event["total_count"], 0)

    def test_pagination_metadata(self):
        for i in range(3):
            other_player, _ = _user(f"reg_{i}")
            SponsorEngagementSubmission.objects.create(
                sponsorship=self.sp, event=self.event, user=other_player,
                engagement_index=0, payload={"value": f"v{i}"}, approval_status="pending",
            )
        body = self.client.get(
            "/sponsors/queue/engagement-submissions/?limit=2", **bearer(self.admin_tok),
        ).json()
        self.assertEqual(body["total_count"], 4)
        self.assertEqual(len(body["results"]), 2)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], 2)

    def test_csv_export(self):
        resp = self.client.get(
            "/sponsors/queue/engagement-submissions/?csv=1", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv")
        text = resp.content.decode()
        self.assertIn("Org Cup", text)
        self.assertIn("registrant", text)


class InviteTests(ApprovalAccessBase):
    def _invite(self, email, tok=None, role="member"):
        return self.client.post(
            f"/sponsors/{self.sponsor.id}/members/invite/",
            data=json.dumps({"email": email, "role": role}),
            content_type="application/json", **bearer(tok or self.admin_tok),
        )

    def test_inviting_an_existing_account_grants_access_immediately(self):
        existing, _ = _user("brand_boss", email="boss@brand.com")
        resp = self._invite("boss@brand.com")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["outcome"], "member_added")
        self.assertTrue(
            SponsorMember.objects.filter(
                sponsor=self.sponsor, user=existing, status="active",
            ).exists(),
        )

    def test_inviting_an_unknown_address_creates_a_pending_invite(self):
        resp = self._invite("newcontact@brand.com")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["outcome"], "invited")
        invite = SponsorMemberInvite.objects.get(email="newcontact@brand.com")
        self.assertEqual(invite.status, "pending")
        self.assertTrue(invite.token)
        self.assertGreater(invite.expires_at, timezone.now())

    def test_inviting_twice_resends_instead_of_duplicating(self):
        self._invite("newcontact@brand.com")
        resp = self._invite("newcontact@brand.com")
        self.assertEqual(resp.json()["outcome"], "already_invited")
        self.assertEqual(
            SponsorMemberInvite.objects.filter(email="newcontact@brand.com").count(), 1,
        )

    def test_only_a_sponsor_admin_can_invite(self):
        self.assertEqual(self._invite("x@brand.com", tok=self.organizer_tok).status_code, 403)
        self.assertEqual(self._invite("x@brand.com", tok=self.stranger_tok).status_code, 403)

    def test_a_bad_address_is_refused(self):
        self.assertEqual(self._invite("not-an-email").status_code, 400)

    def test_verifying_the_account_claims_the_invite(self):
        """The whole point: the contact signs up normally and already has the access."""
        self._invite("newcontact@brand.com")
        newcomer = User.objects.create(
            username="newcontact", email="newcontact@brand.com",
            full_name="New Contact", role="player", password="x", is_active=False,
        )
        cache.set(f"verification_code_{newcomer.user_id}", "123456", 600)
        resp = self.client.post(
            "/auth/verify-code/",
            data=json.dumps({"email": "newcontact@brand.com", "code": "123456"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            SponsorMember.objects.filter(
                sponsor=self.sponsor, user=newcomer, status="active",
            ).exists(),
        )
        invite = SponsorMemberInvite.objects.get(email="newcontact@brand.com")
        self.assertEqual(invite.status, "accepted")
        self.assertEqual(invite.accepted_user_id, newcomer.user_id)

    def test_a_revoked_invite_grants_nothing(self):
        self._invite("newcontact@brand.com")
        invite = SponsorMemberInvite.objects.get(email="newcontact@brand.com")
        resp = self.client.delete(
            f"/sponsors/{self.sponsor.id}/members/invites/{invite.id}/", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        newcomer = User.objects.create(
            username="newcontact", email="newcontact@brand.com",
            full_name="New Contact", role="player", password="x", is_active=False,
        )
        cache.set(f"verification_code_{newcomer.user_id}", "123456", 600)
        self.client.post(
            "/auth/verify-code/",
            data=json.dumps({"email": "newcontact@brand.com", "code": "123456"}),
            content_type="application/json",
        )
        self.assertFalse(
            SponsorMember.objects.filter(sponsor=self.sponsor, user=newcomer).exists(),
        )

    def test_pending_invites_are_listed(self):
        self._invite("newcontact@brand.com")
        body = self.client.get(
            f"/sponsors/{self.sponsor.id}/members/invites/", **bearer(self.admin_tok),
        ).json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["results"][0]["email"], "newcontact@brand.com")
