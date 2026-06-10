"""
Endpoint tests for the P3 rankings-feed fields on the create/edit surface: played_on + ranking_tier.

Mirrors the gating of counts_toward_rankings exactly (afc_leaderboard.views._apply_rankings_feed_fields):
  - an AFC admin may set both fields (and they echo back in the serializer),
  - an organizer's values are silently ignored (defaults kept, no error),
  - a bad ranking_tier or a malformed played_on returns 400.
Driven through the same Bearer-token Django test Client as the rest of the leaderboard tests.
"""
import json
from django.test import TestCase, Client

from afc_leaderboard.models import StandaloneLeaderboard

from ._helpers import make_afc_admin, make_user, make_org, add_member, bearer


class RankingsFeedFieldTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = make_afc_admin()
        self.org = make_org()
        self.uploader, self.up_tok = make_user("feeduploader")
        add_member(self.org, self.uploader, role="owner", can_upload_results=True)

    def _post(self, body, tok):
        return self.client.post(
            "/leaderboards/standalone/create/",
            data=json.dumps(body), content_type="application/json", **bearer(tok),
        )

    def _patch(self, lb_id, body, tok):
        return self.client.patch(
            f"/leaderboards/standalone/{lb_id}/edit/",
            data=json.dumps(body), content_type="application/json", **bearer(tok),
        )

    # ── admin sets both fields ────────────────────────────────────────────────────────────────
    def test_admin_create_sets_played_on_and_tier(self):
        resp = self._post(
            {"name": "Feed Cup", "format": "team",
             "counts_toward_rankings": True, "played_on": "2099-02-10", "ranking_tier": "tier_1"},
            self.admin_tok,
        )
        self.assertEqual(resp.status_code, 201)
        lb = resp.json()["leaderboard"]
        self.assertEqual(lb["played_on"], "2099-02-10")
        self.assertEqual(lb["ranking_tier"], "tier_1")
        row = StandaloneLeaderboard.objects.get(id=lb["id"])
        self.assertEqual(str(row.played_on), "2099-02-10")
        self.assertEqual(row.ranking_tier, "tier_1")

    def test_admin_edit_updates_both_fields(self):
        created = self._post({"name": "Feed Cup", "format": "team"}, self.admin_tok).json()["leaderboard"]
        # default tier is tier_3, played_on null
        self.assertEqual(created["ranking_tier"], "tier_3")
        self.assertIsNone(created["played_on"])

        resp = self._patch(created["id"], {"ranking_tier": "tier_2", "played_on": "2099-03-01"}, self.admin_tok)
        self.assertEqual(resp.status_code, 200)
        lb = resp.json()["leaderboard"]
        self.assertEqual(lb["ranking_tier"], "tier_2")
        self.assertEqual(lb["played_on"], "2099-03-01")

    def test_admin_can_clear_played_on_with_null(self):
        created = self._post(
            {"name": "Feed Cup", "format": "team", "played_on": "2099-02-10"}, self.admin_tok
        ).json()["leaderboard"]
        self.assertEqual(created["played_on"], "2099-02-10")
        resp = self._patch(created["id"], {"played_on": None}, self.admin_tok)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["leaderboard"]["played_on"])

    # ── organizer is silently ignored (defaults kept, no error) ───────────────────────────────
    def test_organizer_values_ignored(self):
        resp = self._post(
            {"name": "Org Cup", "format": "team", "organization_id": self.org.organization_id,
             "played_on": "2099-02-10", "ranking_tier": "tier_1"},
            self.up_tok,
        )
        self.assertEqual(resp.status_code, 201)
        lb = resp.json()["leaderboard"]
        # The organizer cannot set the rankings feed -> defaults kept, no 400.
        self.assertIsNone(lb["played_on"])
        self.assertEqual(lb["ranking_tier"], "tier_3")

    # ── validation ────────────────────────────────────────────────────────────────────────────
    def test_bad_tier_400(self):
        resp = self._post(
            {"name": "Feed Cup", "format": "team", "ranking_tier": "tier_9"}, self.admin_tok
        )
        self.assertEqual(resp.status_code, 400)
        # No leaderboard should have been created on the bad-input path.
        self.assertFalse(StandaloneLeaderboard.objects.filter(name="Feed Cup").exists())

    def test_bad_date_400(self):
        resp = self._post(
            {"name": "Feed Cup", "format": "team", "played_on": "10/02/2099"}, self.admin_tok
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(StandaloneLeaderboard.objects.filter(name="Feed Cup").exists())

    def test_bad_tier_on_edit_400(self):
        created = self._post({"name": "Feed Cup", "format": "team"}, self.admin_tok).json()["leaderboard"]
        resp = self._patch(created["id"], {"ranking_tier": "platinum"}, self.admin_tok)
        self.assertEqual(resp.status_code, 400)
        # The stored tier is unchanged (still the default).
        row = StandaloneLeaderboard.objects.get(id=created["id"])
        self.assertEqual(row.ranking_tier, "tier_3")
