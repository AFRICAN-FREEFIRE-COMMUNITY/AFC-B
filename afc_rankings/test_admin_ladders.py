"""
afc_rankings.test_admin_ladders - the ADMIN monthly ladder preview endpoints.

Covers the two routes added for the admin Ladders view (owner 2026-08-03: "there is no page or
place for rankings on the admin ranking and tiering page"):

    GET /rankings/admin/teams/monthly/     admin_publish.admin_teams_monthly
    GET /rankings/admin/players/monthly/   admin_publish.admin_players_monthly

What matters here is the DIFFERENCE from the public twins in ``views.py``:
  - the public endpoints hide every row until the month's season is published; these must return
    the rows anyway and merely REPORT the publish state on ``published``,
  - the public default month prefers the newest PUBLISHED month (so the public keeps seeing last
    quarter while the live one is pending); the admin default must land on the newest POPULATED
    month, which is the data an admin has to look at before deciding to publish,
  - the default month is read per TABLE, so the players ladder never lands on a month that only
    the teams table has populated.
Plus the auth gate: these are Bearer + head_admin/metrics_admin like every other admin route.

HOW IT CONNECTS
    - Drives afc_rankings.admin_publish through the real URL map (django test Client + reverse),
      the same Bearer idiom as test_ghost_claims.
    - The response is consumed by frontend app/(a)/a/rankings/ladders/page.tsx through
      lib/rankingsAdmin.ts (adminTeamsMonthly / adminPlayersMonthly).
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from afc_auth.models import SessionToken, Roles, UserRoles
from afc_team.models import Team
from afc_rankings.models import Season, TeamMonthlyScore, PlayerMonthlyScore

User = get_user_model()


# ───────────────────────── local helpers (mirror test_ghost_claims) ─────────────────────────
def _token(user, label):
    """A live SessionToken string for `user` (the house Bearer idiom)."""
    return SessionToken.objects.create(user=user, token=f"tok_{label}").token


def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _make_admin(username="ladderadmin"):
    """A ranking admin (granular head_admin role -> passes admin_views._auth)."""
    u = User.objects.create(username=username, email=f"{username}@x.com")
    r, _ = Roles.objects.get_or_create(role_name="head_admin")
    UserRoles.objects.create(user=u, role=r)
    return u, _token(u, username)


class AdminMonthlyLadderTests(TestCase):
    """Q2/2099 is over and PUBLISHED, Q3/2099 is live and NOT published - the exact production
    shape the Ladders view has to render (see test_public_gating for the public half)."""

    def setUp(self):
        self.admin, self.admin_tok = _make_admin()
        self.player = User.objects.create(username="ladder_player", email="lp@example.com")
        self.team = Team.objects.create(
            team_name="Ladder FC", join_settings="open",
            team_creator=self.player, team_owner=self.player, country="NG",
        )
        self.published = Season.objects.create(
            name="Published Q", quarter=2, year=2099,
            start_date=datetime.date(2099, 4, 1), end_date=datetime.date(2099, 7, 1),
            transfer_window_open=datetime.date(2099, 4, 1),
            transfer_window_close=datetime.date(2099, 4, 14),
            is_active=False, rankings_published=True,
        )
        self.live = Season.objects.create(
            name="Live Q", quarter=3, year=2099,
            start_date=datetime.date(2099, 7, 1), end_date=datetime.date(2099, 10, 1),
            transfer_window_open=datetime.date(2099, 7, 1),
            transfer_window_close=datetime.date(2099, 7, 14),
            is_active=True, rankings_published=False,
        )
        # One row in the PUBLISHED quarter and one in the LIVE (unpublished) one, both tables.
        TeamMonthlyScore.objects.create(team=self.team, month=datetime.date(2099, 6, 1),
                                        total_score=10, rank=1)
        TeamMonthlyScore.objects.create(team=self.team, month=datetime.date(2099, 8, 1),
                                        total_score=22, rank=1)
        PlayerMonthlyScore.objects.create(player=self.player, month=datetime.date(2099, 6, 1),
                                          total_score=8, rank=1)
        PlayerMonthlyScore.objects.create(player=self.player, month=datetime.date(2099, 8, 1),
                                          total_score=15, rank=1)

    def _get(self, name, token=None, **params):
        return self.client.get(reverse(name), data=params, **(_bearer(token) if token else {}))

    # ── the point of the whole view: unpublished rows ARE returned, just flagged ──
    def test_teams_monthly_returns_unpublished_rows_flagged_as_preview(self):
        body = self._get("rankings_admin_teams_monthly", self.admin_tok, month="2099-08").json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["total_score"], 22)
        self.assertFalse(body["published"])
        self.assertEqual(body["season"]["name"], "Live Q")

    def test_players_monthly_returns_unpublished_rows_flagged_as_preview(self):
        body = self._get("rankings_admin_players_monthly", self.admin_tok, month="2099-08").json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["total_score"], 15)
        self.assertFalse(body["published"])

    # ── a published month reports published=True, so the UI can drop the preview badge ──
    def test_published_month_reports_published_true(self):
        for name in ("rankings_admin_teams_monthly", "rankings_admin_players_monthly"):
            body = self._get(name, self.admin_tok, month="2099-06").json()
            self.assertTrue(body["published"], name)
            self.assertEqual(body["season"]["name"], "Published Q", name)

    # ── the admin default lands on the newest POPULATED month, NOT the newest published one ──
    def test_default_month_is_the_newest_populated_not_the_newest_published(self):
        for name in ("rankings_admin_teams_monthly", "rankings_admin_players_monthly"):
            body = self._get(name, self.admin_tok).json()
            self.assertEqual(body["month"], "2099-08-01", name)
            self.assertFalse(body["published"], name)

    # ── an empty month is an empty ladder, not an error, and still names its season ──
    def test_month_with_no_rows_returns_an_empty_ladder(self):
        body = self._get("rankings_admin_teams_monthly", self.admin_tok, month="2099-05").json()
        self.assertEqual(body["results"], [])
        self.assertEqual(body["season"]["name"], "Published Q")

    # ── a garbage ?month falls back to the default instead of 500ing ──
    def test_unparseable_month_falls_back_to_the_default(self):
        body = self._get("rankings_admin_teams_monthly", self.admin_tok, month="not-a-month").json()
        self.assertEqual(body["month"], "2099-08-01")

    # ── auth gate: same as every other admin ranking route ──
    def test_requires_a_bearer_token(self):
        for name in ("rankings_admin_teams_monthly", "rankings_admin_players_monthly"):
            self.assertEqual(self._get(name).status_code, 400, name)

    def test_non_admin_is_forbidden(self):
        plain = User.objects.create(username="plain", email="plain@example.com")
        tok = _token(plain, "plain")
        for name in ("rankings_admin_teams_monthly", "rankings_admin_players_monthly"):
            self.assertEqual(self._get(name, tok).status_code, 403, name)


class AdminMonthlyPerTableDefaultTests(TestCase):
    """Each ladder resolves its default month from its OWN score table."""

    def setUp(self):
        self.admin, self.admin_tok = _make_admin("pertableadmin")
        self.player = User.objects.create(username="pt_player", email="pt@example.com")
        self.team = Team.objects.create(
            team_name="PerTable FC", join_settings="open",
            team_creator=self.player, team_owner=self.player, country="NG",
        )
        Season.objects.create(
            name="Only Q", quarter=1, year=2099,
            start_date=datetime.date(2099, 1, 1), end_date=datetime.date(2099, 3, 31),
            transfer_window_open=datetime.date(2099, 1, 1),
            transfer_window_close=datetime.date(2099, 1, 14),
            is_active=True, rankings_published=False,
        )
        # Teams have a MARCH row, players only a FEBRUARY one.
        TeamMonthlyScore.objects.create(team=self.team, month=datetime.date(2099, 3, 1),
                                        total_score=10, rank=1)
        PlayerMonthlyScore.objects.create(player=self.player, month=datetime.date(2099, 2, 1),
                                          total_score=8, rank=1)

    def test_each_ladder_defaults_to_its_own_latest_month(self):
        teams = self.client.get(reverse("rankings_admin_teams_monthly"),
                                **_bearer(self.admin_tok)).json()
        players = self.client.get(reverse("rankings_admin_players_monthly"),
                                  **_bearer(self.admin_tok)).json()
        self.assertEqual(teams["month"], "2099-03-01")
        self.assertEqual(len(teams["results"]), 1)
        self.assertEqual(players["month"], "2099-02-01")
        self.assertEqual(len(players["results"]), 1)
