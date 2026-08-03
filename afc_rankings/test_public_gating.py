"""
test_public_gating.py
─────────────────────
Covers the PUBLIC monthly-ladder publish gate in ``afc_rankings.views`` (owner 2026-08-03:
"public ranking page shows no PLAYER rankings").

The bug: ``_gated_monthly`` resolved the gate season with ``_resolve_season`` - the season that is
ACTIVE today - instead of the season the requested MONTH belongs to. The public rankings page
(frontend app/(user)/rankings) only sends ``month`` on the monthly endpoints, so as soon as a new
quarter rolled over unpublished, EVERY month was judged against that unpublished season and both
ladders read as empty, including months whose own season had been published.

Second, smaller bug covered here: ``_resolve_month`` always defaulted to the latest populated
TeamMonthlyScore month, even for the players ladder, so a month with team rows but no player rows
sent the players endpoint to an empty month.

Both endpoints are exercised through APIRequestFactory (no URL routing needed) - the same
function-view idiom the rest of afc_rankings tests use.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from afc_team.models import Team
from afc_rankings import views
from afc_rankings.models import Season, TeamMonthlyScore, PlayerMonthlyScore

User = get_user_model()


class MonthlyPublishGateTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create(username="gate_player", email="gp@example.com")
        self.team = Team.objects.create(
            team_name="Gate FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        # Q2 is over and PUBLISHED; Q3 is the live season and NOT published yet. Their windows touch
        # on 1 July, exactly like the real SEASON 2 / SEASON 3 rows in production.
        self.published = Season.objects.create(
            name="Published Q", quarter=2, year=2099,
            start_date=datetime.date(2099, 4, 1), end_date=datetime.date(2099, 7, 1),
            transfer_window_open=datetime.date(2099, 4, 1),
            transfer_window_close=datetime.date(2099, 4, 14),
            is_active=False, rankings_published=True,
        )
        self.unpublished = Season.objects.create(
            name="Live Q", quarter=3, year=2099,
            start_date=datetime.date(2099, 7, 1), end_date=datetime.date(2099, 10, 1),
            transfer_window_open=datetime.date(2099, 7, 1),
            transfer_window_close=datetime.date(2099, 7, 14),
            is_active=True, rankings_published=False,
        )
        # One team row + one player row inside the PUBLISHED season's window.
        self.month = datetime.date(2099, 6, 1)
        TeamMonthlyScore.objects.create(team=self.team, month=self.month, total_score=10, rank=1)
        PlayerMonthlyScore.objects.create(player=self.user, month=self.month, total_score=8, rank=1)

    def _get(self, view, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return view(self.factory.get(f"/rankings/x/?{qs}")).data

    # ── the fix: a month inside a published season is visible even when the LIVE season is not ──
    def test_players_monthly_visible_for_a_published_months_season(self):
        body = self._get(views.players_monthly, month="2099-06")
        self.assertTrue(body["published"])
        self.assertEqual(len(body["results"]), 1)

    def test_teams_monthly_visible_for_a_published_months_season(self):
        body = self._get(views.teams_monthly, month="2099-06")
        self.assertTrue(body["published"])
        self.assertEqual(len(body["results"]), 1)

    # ── the gate still holds for a month belonging to an UNPUBLISHED season ──
    def test_month_in_unpublished_season_stays_hidden(self):
        for view in (views.teams_monthly, views.players_monthly):
            body = self._get(view, month="2099-08")
            self.assertFalse(body["published"])
            self.assertEqual(body["results"], [])

    # ── boundary: the seasons share 1 July, and July belongs to the LATER (live) season ──
    def test_boundary_month_belongs_to_the_later_season(self):
        body = self._get(views.players_monthly, month="2099-07")
        self.assertFalse(body["published"])

    # ── an explicit ?season_id still wins (admin/deep-link behaviour is unchanged) ──
    def test_explicit_season_id_overrides_the_month_lookup(self):
        body = self._get(views.players_monthly, month="2099-08",
                         season_id=self.published.season_id)
        self.assertTrue(body["published"])


class ResolveMonthPerTableTests(TestCase):
    """The players ladder must default to the latest populated PLAYER month, not the team one."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create(username="rm_player", email="rm@example.com")
        self.team = Team.objects.create(
            team_name="Resolve FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        Season.objects.create(
            name="Open Q", quarter=1, year=2099,
            start_date=datetime.date(2099, 1, 1), end_date=datetime.date(2099, 3, 31),
            transfer_window_open=datetime.date(2099, 1, 1),
            transfer_window_close=datetime.date(2099, 1, 14),
            is_active=True, rankings_published=True,
        )
        # Teams have a MARCH row, players only have a FEBRUARY one. Before the fix the players
        # endpoint defaulted to March (the latest TEAM month) and returned nothing.
        TeamMonthlyScore.objects.create(team=self.team, month=datetime.date(2099, 3, 1),
                                        total_score=10, rank=1)
        PlayerMonthlyScore.objects.create(player=self.user, month=datetime.date(2099, 2, 1),
                                          total_score=8, rank=1)

    def test_players_monthly_defaults_to_the_latest_player_month(self):
        body = views.players_monthly(self.factory.get("/rankings/players/monthly/")).data
        self.assertEqual(body["month"], "2099-02-01")
        self.assertEqual(len(body["results"]), 1)

    def test_teams_monthly_still_defaults_to_the_latest_team_month(self):
        body = views.teams_monthly(self.factory.get("/rankings/teams/monthly/")).data
        self.assertEqual(body["month"], "2099-03-01")
        self.assertEqual(len(body["results"]), 1)
