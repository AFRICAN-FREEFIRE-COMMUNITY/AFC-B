"""
Endpoint tests for the CRUD surface (create / list / detail / edit / delete) of standalone leaderboards.

Covers: AFC-native create, organizer org-scoped create with the rankings toggle FORCED false,
organizer create with no org (403), list scoping (admin sees all, organizer sees own org),
detail + standings, draft visibility gate, publish via PATCH, delete + 403 for a non-manager.
"""
import json
from django.test import TestCase, Client

from afc_leaderboard.models import StandaloneLeaderboard

from ._helpers import make_afc_admin, make_user, make_org, add_member, bearer


class CrudTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.org = make_org()
        self.uploader, self.up_tok = make_user("uploader")
        add_member(self.org, self.uploader, role="owner", can_upload_results=True)
        self.stranger, self.stranger_tok = make_user("stranger")

    def _post(self, path, body, tok):
        return self.client.post(path, data=json.dumps(body), content_type="application/json", **bearer(tok))

    def _patch(self, path, body, tok):
        return self.client.patch(path, data=json.dumps(body), content_type="application/json", **bearer(tok))

    # ── create ──────────────────────────────────────────────────────────────────────────────
    def test_admin_creates_native_leaderboard_with_toggle(self):
        resp = self._post(
            "/leaderboards/standalone/create/",
            {"name": "AFC Cup", "format": "team", "placement_points": {"1": 12},
             "counts_toward_rankings": True},
            self.admin_tok,
        )
        self.assertEqual(resp.status_code, 201)
        lb = resp.json()["leaderboard"]
        self.assertIsNone(lb["organization_id"])
        self.assertTrue(lb["counts_toward_rankings"])  # admin may set the flag
        self.assertEqual(lb["status"], "draft")

    def test_organizer_create_forces_org_and_disables_toggle(self):
        resp = self._post(
            "/leaderboards/standalone/create/",
            {"name": "Org Cup", "format": "solo", "organization_id": self.org.organization_id,
             "counts_toward_rankings": True},
            self.up_tok,
        )
        self.assertEqual(resp.status_code, 201)
        lb = resp.json()["leaderboard"]
        self.assertEqual(lb["organization_id"], self.org.organization_id)
        # The rankings toggle is AFC-admin-only -> forced False for the organizer even though they sent True.
        self.assertFalse(lb["counts_toward_rankings"])

    def test_organizer_without_org_is_rejected(self):
        resp = self._post(
            "/leaderboards/standalone/create/",
            {"name": "No Org", "format": "team"},
            self.up_tok,
        )
        self.assertEqual(resp.status_code, 403)

    def test_create_requires_name_and_valid_format(self):
        self.assertEqual(self._post("/leaderboards/standalone/create/", {"format": "team"}, self.admin_tok).status_code, 400)
        self.assertEqual(self._post("/leaderboards/standalone/create/", {"name": "X", "format": "bad"}, self.admin_tok).status_code, 400)

    def test_create_requires_auth(self):
        resp = self.client.post("/leaderboards/standalone/create/", data=json.dumps({"name": "X", "format": "team"}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)  # missing Authorization header

    # ── list ────────────────────────────────────────────────────────────────────────────────
    def test_list_scoping(self):
        # One native, one org-owned.
        self._post("/leaderboards/standalone/create/", {"name": "Native", "format": "team"}, self.admin_tok)
        self._post("/leaderboards/standalone/create/", {"name": "OrgLB", "format": "team", "organization_id": self.org.organization_id}, self.up_tok)

        admin_list = self.client.get("/leaderboards/standalone/", **bearer(self.admin_tok)).json()
        self.assertEqual(admin_list["total_count"], 2)  # admin sees both
        self.assertIn("has_more", admin_list)
        self.assertIn("next_offset", admin_list)

        org_list = self.client.get("/leaderboards/standalone/", **bearer(self.up_tok)).json()
        names = {r["name"] for r in org_list["results"]}
        self.assertEqual(names, {"OrgLB"})  # organizer sees only their org's

        stranger_list = self.client.get("/leaderboards/standalone/", **bearer(self.stranger_tok)).json()
        self.assertEqual(stranger_list["total_count"], 0)  # unrelated user sees none

    # ── detail + standings ──────────────────────────────────────────────────────────────────
    def test_detail_returns_standings_block(self):
        lb_id = self._post("/leaderboards/standalone/create/", {"name": "D", "format": "team"}, self.admin_tok).json()["leaderboard"]["id"]
        resp = self.client.get(f"/leaderboards/standalone/{lb_id}/", **bearer(self.admin_tok))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("leaderboard", "participants", "matches", "standings", "can_manage"):
            self.assertIn(key, body)
        self.assertTrue(body["can_manage"])

    def test_draft_hidden_from_non_manager(self):
        lb_id = self._post("/leaderboards/standalone/create/", {"name": "Draft", "format": "team"}, self.admin_tok).json()["leaderboard"]["id"]
        # Stranger cannot view a draft.
        self.assertEqual(self.client.get(f"/leaderboards/standalone/{lb_id}/", **bearer(self.stranger_tok)).status_code, 403)
        # Publish it, then the stranger can view.
        self._patch(f"/leaderboards/standalone/{lb_id}/edit/", {"status": "published"}, self.admin_tok)
        self.assertEqual(self.client.get(f"/leaderboards/standalone/{lb_id}/", **bearer(self.stranger_tok)).status_code, 200)

    # ── edit / publish ──────────────────────────────────────────────────────────────────────
    def test_publish_via_patch(self):
        lb_id = self._post("/leaderboards/standalone/create/", {"name": "P", "format": "team"}, self.admin_tok).json()["leaderboard"]["id"]
        resp = self._patch(f"/leaderboards/standalone/{lb_id}/edit/", {"status": "published"}, self.admin_tok)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["leaderboard"]["status"], "published")

    def test_organizer_cannot_set_rankings_flag_on_edit(self):
        lb_id = self._post("/leaderboards/standalone/create/", {"name": "E", "format": "team", "organization_id": self.org.organization_id}, self.up_tok).json()["leaderboard"]["id"]
        resp = self._patch(f"/leaderboards/standalone/{lb_id}/edit/", {"counts_toward_rankings": True}, self.up_tok)
        self.assertEqual(resp.status_code, 200)
        # Field silently ignored for the organizer.
        self.assertFalse(resp.json()["leaderboard"]["counts_toward_rankings"])
        self.assertFalse(StandaloneLeaderboard.objects.get(id=lb_id).counts_toward_rankings)

    # ── delete ──────────────────────────────────────────────────────────────────────────────
    def test_delete_by_manager_and_403_for_non_manager(self):
        lb_id = self._post("/leaderboards/standalone/create/", {"name": "Del", "format": "team"}, self.admin_tok).json()["leaderboard"]["id"]
        # Stranger cannot delete.
        self.assertEqual(self.client.delete(f"/leaderboards/standalone/{lb_id}/delete/", **bearer(self.stranger_tok)).status_code, 403)
        # Admin can.
        self.assertEqual(self.client.delete(f"/leaderboards/standalone/{lb_id}/delete/", **bearer(self.admin_tok)).status_code, 200)
        self.assertFalse(StandaloneLeaderboard.objects.filter(id=lb_id).exists())
