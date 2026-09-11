"""
afc_sponsors/test_bulk_decisions.py - deciding many sponsor submissions in one request
(owner 2026-09-11: "there should be a way to bulk approve or reject teams").

WHY THESE TESTS EXIST
    The approval queue had one Confirm and one Reject per row and refetched the whole page after
    each click, so clearing thirty pending rows was thirty clicks and thirty reloads. The bulk
    endpoint decides a list in one request. What must stay true: every row is judged by the same
    rules and the same permission gate as the single endpoint, a refused row does not stop the
    others, a reject still needs a reason, and undo is not bulk.

COVERS
    - approve a batch: every row approved, decided_by recorded, applied count right
    - reject a batch with one reason: every row rejected with that reason
    - reject without a reason: refused before anything is written
    - a batch mixing a row the caller may decide with one they may not: the allowed row is
      applied, the other refused with 403, nothing raised
    - an unknown id: reported as 404 in its own result, the rest applied
    - the cap: more than BULK_DECISION_MAX ids is refused up front
    - undo is not an accepted bulk action
    - both queues list pending rows FIRST even after decisions (the old order_by on the raw
      status string put "approved" above "pending", alphabetically)

Run: python manage.py test afc_sponsors.test_bulk_decisions
"""
import json
from datetime import date, timedelta

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_organizers.models import Organization, OrganizationMember
from afc_tournament_and_scrims.models import Event

from .engagements import BULK_DECISION_MAX
from .models import EventSponsorship, Sponsor, SponsorEngagementSubmission, SponsorMember


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x",
    )
    return u, SessionToken.objects.create(user=u, token=f"tok_{username}").token


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


ENGAGEMENTS = [{"type": "collect_id", "label": "Sponsor UID"}]


class BulkDecisionTests(TestCase):
    """Two sponsors, two events. The sponsor member of Brand A may decide Brand A's rows only."""

    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = _user("bd_admin", role="admin")
        self.brand_a_staff, self.brand_a_tok = _user("brand_a_staff")
        self.organizer, _ = _user("bd_org_owner")
        self.players = [_user(f"bd_player{i}")[0] for i in range(4)]

        self.org = Organization.objects.create(
            name="Bulk Org", slug="bulk-org", created_by=self.admin, status="active",
        )
        OrganizationMember.objects.create(
            organization=self.org, user=self.organizer, role="owner", status="active",
        )
        self.event = self._event("Bulk Cup")
        self.brand_a = Sponsor.objects.create(name="Brand A", slug="brand-a", created_by=self.admin)
        self.brand_b = Sponsor.objects.create(name="Brand B", slug="brand-b", created_by=self.admin)
        SponsorMember.objects.create(
            sponsor=self.brand_a, user=self.brand_a_staff, role="owner", status="active",
        )
        self.sp_a = EventSponsorship.objects.create(
            event=self.event, sponsor=self.brand_a, requires_approval=True, engagements=ENGAGEMENTS,
        )
        self.sp_b = EventSponsorship.objects.create(
            event=self.event, sponsor=self.brand_b, requires_approval=True, engagements=ENGAGEMENTS,
        )
        # three rows for Brand A, one for Brand B, all pending
        self.a_rows = [self._submission(self.sp_a, p) for p in self.players[:3]]
        self.b_row = self._submission(self.sp_b, self.players[3])

    def _event(self, name):
        return Event.objects.create(
            event_name=name, competition_type="tournament", participant_type="solo",
            event_type="online", max_teams_or_players=10, event_mode="single",
            start_date=date.today() + timedelta(days=7),
            end_date=date.today() + timedelta(days=8),
            registration_open_date=date.today() - timedelta(days=1),
            registration_end_date=date.today() + timedelta(days=5),
            number_of_stages=1, creator=self.organizer, is_public=True,
            organization=self.org,
        )

    def _submission(self, sponsorship, player):
        return SponsorEngagementSubmission.objects.create(
            sponsorship=sponsorship, event=self.event, user=player,
            engagement_index=0, payload={"value": "abc"}, approval_status="pending",
        )

    def _bulk(self, tok, ids, action="approve", reason=""):
        return self.client.post(
            "/sponsors/submissions/decide/",
            data=json.dumps({"ids": ids, "action": action, "reason": reason}),
            content_type="application/json", **bearer(tok),
        )

    # ---- happy paths ----------------------------------------------------------------------

    def test_approve_a_batch(self):
        resp = self._bulk(self.admin_tok, [r.id for r in self.a_rows])
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual((body["applied"], body["refused"]), (3, 0))
        for r in self.a_rows:
            r.refresh_from_db()
            self.assertEqual(r.approval_status, "approved")
            self.assertEqual(r.decided_by_id, self.admin.user_id)
        # every result carries the same shape the single endpoint returns, for in-place patching
        self.assertTrue(all(x["ok"] and x["submission"]["approval_status"] == "approved" for x in body["results"]))

    def test_reject_a_batch_with_one_reason(self):
        resp = self._bulk(self.admin_tok, [r.id for r in self.a_rows], action="reject", reason="Blurry screenshot")
        self.assertEqual(resp.status_code, 200, resp.content)
        for r in self.a_rows:
            r.refresh_from_db()
            self.assertEqual((r.approval_status, r.reason), ("rejected", "Blurry screenshot"))

    # ---- rules that must hold per row ------------------------------------------------------

    def test_reject_needs_a_reason_before_anything_is_written(self):
        resp = self._bulk(self.admin_tok, [r.id for r in self.a_rows], action="reject", reason="  ")
        self.assertEqual(resp.status_code, 400)
        for r in self.a_rows:
            r.refresh_from_db()
            self.assertEqual(r.approval_status, "pending")

    def test_mixed_permissions_refuse_only_the_foreign_row(self):
        """Brand A's staff may decide Brand A's rows; Brand B's row in the same batch is refused
        with 403 in its own result, and the Brand A rows are still applied."""
        ids = [self.a_rows[0].id, self.b_row.id]
        resp = self._bulk(self.brand_a_tok, ids)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual((body["applied"], body["refused"]), (1, 1))
        by_id = {x["id"]: x for x in body["results"]}
        self.assertTrue(by_id[self.a_rows[0].id]["ok"])
        self.assertEqual(by_id[self.b_row.id]["status"], 403)
        self.a_rows[0].refresh_from_db()
        self.b_row.refresh_from_db()
        self.assertEqual(self.a_rows[0].approval_status, "approved")
        self.assertEqual(self.b_row.approval_status, "pending")

    def test_unknown_id_is_reported_not_raised(self):
        resp = self._bulk(self.admin_tok, [self.a_rows[0].id, 999999])
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual((body["applied"], body["refused"]), (1, 1))
        self.assertEqual([x["status"] for x in body["results"] if x["id"] == 999999], [404])

    def test_cap(self):
        resp = self._bulk(self.admin_tok, list(range(1, BULK_DECISION_MAX + 2)))
        self.assertEqual(resp.status_code, 400)

    def test_undo_is_not_bulk(self):
        resp = self._bulk(self.admin_tok, [self.a_rows[0].id], action="undo")
        self.assertEqual(resp.status_code, 400)

    def test_single_endpoint_still_behaves(self):
        """The refactor moved the rules into _apply_decision; the single route must be unchanged."""
        resp = self.client.post(
            f"/sponsors/submissions/{self.a_rows[0].id}/decide/",
            data=json.dumps({"action": "approve"}), content_type="application/json",
            **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["submission"]["approval_status"], "approved")

    # ---- ordering: pending sits at the top, in both queues --------------------------------

    def test_decided_rows_do_not_float_above_pending(self):
        """Approve two of Brand A's three rows, then read both queues: the pending row must come
        first, then the rejected ones, then the approved ones. Before 2026-09-11 the raw status
        string was the sort key and approved rows led the list."""
        self._bulk(self.admin_tok, [self.a_rows[0].id, self.a_rows[1].id])
        self._bulk(self.admin_tok, [self.b_row.id], action="reject", reason="No")

        admin_q = self.client.get("/sponsors/queue/engagement-submissions/", **bearer(self.admin_tok))
        self.assertEqual(admin_q.status_code, 200, admin_q.content)
        statuses = [r["approval_status"] for r in admin_q.json()["results"]]
        self.assertEqual(statuses, ["pending", "rejected", "approved", "approved"])

        portal_q = self.client.get(
            f"/sponsors/{self.brand_a.id}/events/{self.event.event_id}/engagement-submissions/",
            **bearer(self.brand_a_tok),
        )
        self.assertEqual(portal_q.status_code, 200, portal_q.content)
        statuses = [r["approval_status"] for r in portal_q.json()["results"]]
        self.assertEqual(statuses, ["pending", "approved", "approved"])

