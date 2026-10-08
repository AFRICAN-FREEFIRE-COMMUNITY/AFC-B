"""
afc_rankings/test_public_tiers.py
================================================================================
ONE tier on the site (inbox #162 / #165, owner 2026-10-05 "yes" and 2026-10-08 "broadcasts and
polls should use the tiering everything else uses"): every reader asks afc_rankings/public_tiers.py
for the team's published RANKING tier, never the hand-set afc_team.Team.team_tier.

Covers:
  - the season rule: the latest season with tiers PUBLISHED; a newer unpublished season (a draft)
    is never read; no published season at all means everybody is unranked
  - tier_label: code 0 is "Tier 1", and codes above 3 are legitimate (tiers are extensible)
  - the team endpoints carry `ranking_tier` (the code, or None) and no longer carry team_tier:
    get_all_teams, get_team_details
  - the admin dashboard's teams-by-tier split
  - the manual tier endpoint is gone

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_rankings.test_public_tiers --keepdb
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from afc_auth.models import User
from afc_rankings.models import Season, TeamQuarterlyScore
from afc_rankings.public_tiers import (
    published_team_tier,
    published_team_tier_codes,
    published_team_tiers,
    published_tier_season,
    team_ids_in_tiers,
    tier_label,
)
from afc_team.models import Team


def _season(name, quarter, start_offset_days, published):
    today = timezone.localdate()
    start = today + timedelta(days=start_offset_days)
    return Season.objects.create(
        name=name, quarter=quarter, year=2026,
        start_date=start, end_date=start + timedelta(days=89),
        transfer_window_open=start, transfer_window_close=start + timedelta(days=13),
        rankings_published=published, tiers_published=published,
    )


class PublicTiersTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="tier_owner", email="o@example.com", password="x")
        # Team.team_tier is left at its default "3" on every team, like production: it must not
        # decide anything anywhere.
        self.vent = Team.objects.create(team_name="V-ENT ESPORTS", join_settings="open",
                                        team_creator=self.owner, team_owner=self.owner)
        self.entry = Team.objects.create(team_name="Entry Squad", join_settings="open",
                                         team_creator=self.owner, team_owner=self.owner)
        self.unranked = Team.objects.create(team_name="Brand New", join_settings="open",
                                            team_creator=self.owner, team_owner=self.owner)
        self.older = _season("SEASON 1 2026", 1, -200, published=True)
        self.published = _season("SEASON 2 2026", 2, -120, published=True)
        self.draft = _season("SEASON 4 2026", 4, -20, published=False)
        TeamQuarterlyScore.objects.create(team=self.vent, season=self.older, tier_assigned=0)
        TeamQuarterlyScore.objects.create(team=self.vent, season=self.published, tier_assigned=2)
        TeamQuarterlyScore.objects.create(team=self.entry, season=self.published, tier_assigned=3)
        TeamQuarterlyScore.objects.create(team=self.vent, season=self.draft, tier_assigned=0)
        TeamQuarterlyScore.objects.create(team=self.unranked, season=self.draft, tier_assigned=1)

    # ── the season rule ──────────────────────────────────────────────────────────────────────
    def test_the_latest_published_season_is_read_and_a_draft_never_is(self):
        self.assertEqual(published_tier_season(), self.published)
        self.assertEqual(published_team_tiers([self.vent.team_id, self.entry.team_id,
                                               self.unranked.team_id]),
                         {self.vent.team_id: 2, self.entry.team_id: 3})
        self.assertEqual(published_team_tier(self.vent), 2)
        self.assertIsNone(published_team_tier(self.unranked))

    def test_no_published_season_means_everybody_is_unranked(self):
        Season.objects.update(tiers_published=False)
        self.assertIsNone(published_tier_season())
        self.assertEqual(published_team_tiers([self.vent.team_id]), {})
        self.assertEqual(published_team_tier_codes(), [])
        self.assertFalse(team_ids_in_tiers([2]).exists())

    def test_one_query_for_many_teams(self):
        ids = [self.vent.team_id, self.entry.team_id, self.unranked.team_id]
        season = published_tier_season()
        with self.assertNumQueries(1):
            published_team_tiers(ids, season=season)

    def test_codes_and_subqueries(self):
        self.assertEqual(published_team_tier_codes(), [2, 3])
        self.assertEqual(set(team_ids_in_tiers([2, 3]).values_list("team_id", flat=True)),
                         {self.vent.team_id, self.entry.team_id})

    # ── the label ────────────────────────────────────────────────────────────────────────────
    def test_the_code_is_not_the_label(self):
        self.assertEqual(tier_label(0), "Tier 1")
        self.assertEqual(tier_label(2), "Tier 3")
        self.assertEqual(tier_label(3), "Tier 4")
        self.assertEqual(tier_label(5), "Tier 6")       # extensible, never capped
        self.assertEqual(tier_label(None), "Unranked")

    # ── the team endpoints ───────────────────────────────────────────────────────────────────
    def test_get_all_teams_carries_the_ranking_tier(self):
        resp = self.client.get(reverse("get_all_teams"))
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = {t["team_name"]: t for t in resp.json()["teams"]}
        self.assertEqual(rows["V-ENT ESPORTS"]["ranking_tier"], 2)
        self.assertEqual(rows["Entry Squad"]["ranking_tier"], 3)
        self.assertIsNone(rows["Brand New"]["ranking_tier"])
        self.assertNotIn("team_tier", rows["V-ENT ESPORTS"])

    def test_get_team_details_carries_the_ranking_tier(self):
        resp = self.client.post(reverse("get_team_details"), {"team_name": "V-ENT ESPORTS"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        team = resp.json()["team"]
        self.assertEqual(team["ranking_tier"], 2)
        self.assertNotIn("team_tier", team)

        resp = self.client.post(reverse("get_team_details"), {"team_name": "Brand New"},
                                content_type="application/json")
        self.assertIsNone(resp.json()["team"]["ranking_tier"])

    # ── admin dashboard ──────────────────────────────────────────────────────────────────────
    def test_dashboard_splits_teams_by_published_tier(self):
        from afc_auth.views_dashboard import _detail_teams

        detail = _detail_teams(None)
        by_tier = next(s for s in detail["sections"] if s["key"] == "by_tier")
        self.assertEqual(by_tier["rows"], [["Tier 3", 1], ["Tier 4", 1], ["Unranked", 1]])
        self.assertIn("SEASON 2 2026", by_tier["note"])

    # ── the old hand-set control is gone; the admin page pins the ranking tier (inbox #173) ────
    def test_the_old_manual_tier_endpoint_is_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("admin_change_team_tier")

    def _admin_headers(self):
        from afc_auth.models import SessionToken

        admin = User.objects.create(username="tier_admin", email="ta@example.com", password="x", role="admin")
        token = SessionToken.objects.create(user=admin, token="tok-tier-admin").token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_an_admin_pins_an_unranked_team_and_a_recalc_keeps_it(self):
        from afc_rankings.recalc import recalc_team_quarterly

        h = self._admin_headers()
        r = self.client.post(reverse("admin_team_tier"), {"team_id": self.unranked.team_id, "tier": 1},
                             content_type="application/json", **h)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["ranking_tier"], 1)
        self.assertTrue(r.json()["pinned"])
        self.assertEqual(published_team_tier(self.unranked), 1)
        # The team has no activity, which used to delete its season row on the next recalc.
        recalc_team_quarterly(self.unranked.team_id, self.published.season_id)
        self.assertEqual(published_team_tier(self.unranked), 1)
        # Every reader sees it.
        rows = {t["team_name"]: t for t in self.client.get(reverse("get_all_teams")).json()["teams"]}
        self.assertEqual(rows["Brand New"]["ranking_tier"], 1)
        # The owner is told, by label.
        from afc_auth.models import Notifications
        self.assertTrue(Notifications.objects.filter(user=self.owner, message__contains="Tier 2").exists())

    def test_automatic_removes_a_pin_the_pin_created(self):
        h = self._admin_headers()
        url = reverse("admin_team_tier")
        self.client.post(url, {"team_id": self.unranked.team_id, "tier": 0}, content_type="application/json", **h)
        r = self.client.post(url, {"team_id": self.unranked.team_id, "tier": None}, content_type="application/json", **h)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.json()["ranking_tier"])
        self.assertFalse(r.json()["pinned"])
        self.assertIsNone(published_team_tier(self.unranked))

    def test_a_ranked_team_moves_and_the_state_reads_back(self):
        h = self._admin_headers()
        url = reverse("admin_team_tier")
        r = self.client.post(url, {"team_id": self.vent.team_id, "tier": 0, "reason": "Won the national final"},
                             content_type="application/json", **h)
        self.assertEqual(r.json()["ranking_tier"], 0)
        g = self.client.get(url, {"team_id": self.vent.team_id}, **h).json()
        self.assertEqual(g["ranking_tier"], 0)
        self.assertTrue(g["pinned"])
        self.assertEqual(g["reason"], "Won the national final")
        self.assertEqual(g["season"], "SEASON 2 2026")
        self.assertIn(3, g["options"])

    def test_refusals_carry_codes(self):
        h = self._admin_headers()
        url = reverse("admin_team_tier")
        self.assertEqual(self.client.post(url, {"team_id": self.vent.team_id, "tier": 42},
                                          content_type="application/json", **h).json()["code"], "tier_unknown")
        self.assertEqual(self.client.post(url, {"team_id": self.vent.team_id, "tier": "Tier 1"},
                                          content_type="application/json", **h).json()["code"], "tier_number")
        self.assertEqual(self.client.get(url, {"team_id": 999999}, **h).status_code, 404)
        self.assertEqual(self.client.get(url, {"team_id": self.vent.team_id}).status_code, 401)
        Season.objects.update(tiers_published=False)
        self.assertEqual(self.client.post(url, {"team_id": self.vent.team_id, "tier": 1},
                                          content_type="application/json", **h).json()["code"],
                         "no_published_tier_season")
