# afc_auth/test_dashboard_stats.py
# ──────────────────────────────────────────────────────────────────────────────
# The admin dashboard's numbers, and the eleven drill-downs behind them.
#
# WHAT THIS FILE IS DEFENDING AGAINST, in the exact form it was found on 2026-09-02:
#
#   1. NUMBERS NOBODY CALCULATED. The page had "0", "Top: 0", "N0" and "0 active" typed into the
#      markup. A constant cannot go stale, cannot be wrong, and cannot be right. So every assertion
#      here compares the endpoint against a queryset, never against a number I typed.
#   2. A SWALLOWED FAILURE READING AS A ZERO. "Player Match Stats Records" showed 0 because the
#      page called an admin endpoint with no Authorization header and hid the 400 in a .catch.
#      So the auth cases are asserted explicitly: no header is a 400, a non-admin is a 403.
#   3. A METRIC THAT RENDERS AN EMPTY PAGE. Every registered metric is walked, and each must come
#      back with a title, a headline and at least one section. A drill-down that 200s with nothing
#      in it is the same lie as a hardcoded zero, wearing a different hat.
#
# CONNECTS TO: afc_auth/views_dashboard.py (both endpoints and DETAIL_BUILDERS),
# frontend app/(a)/a/dashboard/page.tsx and app/(a)/a/dashboard/[metric]/page.tsx.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import uuid

from django.test import Client, TestCase
from django.utils import timezone

from afc_tournament_and_scrims.models import Event
from afc_team.models import Team

from .models import AdminHistory, News, SessionToken, User
from .views_dashboard import DETAIL_BUILDERS

SUMMARY_URL = "/auth/admin/dashboard-stats/"


def _detail_url(metric):
    return f"/auth/admin/dashboard-stats/{metric}/"


class DashboardStatsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="dash_admin", email="dash_admin@afc.test", password="x",
            role="admin", status="active", is_active=True, country="Nigeria",
        )
        self.player = User.objects.create_user(
            username="dash_player", email="dash_player@afc.test", password="x",
            role="player", status="active", is_active=True, country="Ghana",
        )
        today = timezone.localdate()
        # One of each competition type, so the tournament/scrims split is exercised rather than
        # assumed. The scrims row is the whole reason this audit happened.
        for name, kind in (("Dash Cup", "tournament"), ("Dash Scrims", "scrims")):
            Event.objects.create(
                event_name=name, competition_type=kind, participant_type="squad",
                event_type="virtual", event_mode="br", max_teams_or_players=12,
                number_of_stages=1, is_public=True, is_draft=False,
                start_date=today, end_date=today + datetime.timedelta(days=1),
                registration_open_date=today - datetime.timedelta(days=1),
                registration_end_date=today + datetime.timedelta(days=1),
            )
        Team.objects.create(team_name="Dash Team", join_settings="open",
                            team_creator=self.player, team_owner=self.player, country="Nigeria")
        News.objects.create(news_title="Dash published", content="x", category="tournament",
                            author=self.admin, is_published=True)
        News.objects.create(news_title="Dash draft", content="x", category="tournament",
                            author=self.admin, is_published=False)
        # THREE admin actions, and this is not padding. The first version of this file created
        # NO AdminHistory rows, so the loop that serialises them never executed and the suite went
        # green while the endpoint 500d against the real database with "Object of type User is not
        # JSON serializable" (admin_user is a ForeignKey, not a string). A test whose data is empty
        # proves the code compiles, not that it works.
        for i in range(3):
            AdminHistory.objects.create(admin_user=self.admin, action="edit_event",
                                        description=f"did thing {i}")
        # One with NO admin_user, because the column is nullable and production holds such rows.
        AdminHistory.objects.create(admin_user=None, action="system", description="orphan row")

    def _auth(self, user):
        token = SessionToken.objects.create(
            user=user, token=f"d-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    # ── auth ──────────────────────────────────────────────────────────────────
    def test_no_authorization_header_is_a_400_not_a_zero(self):
        # The exact failure that made the dashboard print 0 records with 2,982 in the table. The
        # endpoint must refuse loudly; it is the CALLER's job not to hide it.
        res = self.client.get(SUMMARY_URL)
        self.assertEqual(res.status_code, 400, res.content)

    def test_a_non_admin_is_refused(self):
        res = self.client.get(SUMMARY_URL, **self._auth(self.player))
        self.assertEqual(res.status_code, 403, res.content)

    # ── the summary ───────────────────────────────────────────────────────────
    def test_every_summary_figure_matches_its_queryset(self):
        res = self.client.get(SUMMARY_URL, **self._auth(self.admin))
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()

        # Compared against live querysets, never against a literal, so the test cannot drift into
        # asserting the same fiction the markup used to.
        self.assertEqual(body["members"]["total"], User.objects.count())
        self.assertEqual(body["teams"]["total"], Team.objects.count())
        self.assertEqual(body["news"]["total"], News.objects.count())
        self.assertEqual(body["news"]["published"],
                         News.objects.filter(is_published=True).count())
        self.assertEqual(body["events"]["tournaments"],
                         Event.objects.filter(competition_type="tournament").count())
        self.assertEqual(body["events"]["scrims"],
                         Event.objects.filter(competition_type="scrims").count())

    def test_the_scrim_created_in_setup_is_actually_counted(self):
        # The regression this whole branch exists for. Against a "scrim" filter this is 0.
        body = self.client.get(SUMMARY_URL, **self._auth(self.admin)).json()
        self.assertEqual(body["events"]["scrims"], 1)

    def test_published_and_total_news_are_reported_separately(self):
        # setUp writes one published and one draft. A single number labelled "published" cannot be
        # right for both, which is exactly what total_published_news used to return.
        body = self.client.get(SUMMARY_URL, **self._auth(self.admin)).json()
        self.assertEqual(body["news"]["total"], 2)
        self.assertEqual(body["news"]["published"], 1)

    def test_the_recent_activity_rows_ride_along_with_the_counts(self):
        # At most ten, newest first, so the page never downloads 1,545 rows to show ten.
        body = self.client.get(SUMMARY_URL, **self._auth(self.admin)).json()
        recent = body["activity"]["recent"]
        self.assertEqual(len(recent), 4, "setUp writes four; an empty list would prove nothing")
        for row in recent:
            # A STRING, never a User. Serialising the ForeignKey itself is what 500d.
            self.assertIsInstance(row["admin_user"], str)
            self.assertIn("timestamp", row)
        self.assertIn("Unknown", [r["admin_user"] for r in recent],
                      "the row with a NULL admin_user must degrade, not crash")

    def test_revenue_is_a_string_not_a_float(self):
        # Money through binary floating point is money somebody eventually has to explain.
        body = self.client.get(SUMMARY_URL, **self._auth(self.admin)).json()
        self.assertIsInstance(body["shop"]["revenue_paid"], str)
        self.assertIsInstance(body["shop"]["diamond_revenue"], str)

    # ── the drill-downs ───────────────────────────────────────────────────────
    def test_every_registered_metric_returns_a_usable_view(self):
        headers = self._auth(self.admin)
        checked = []
        for metric in sorted(DETAIL_BUILDERS):
            res = self.client.get(_detail_url(metric), **headers)
            self.assertEqual(res.status_code, 200, f"{metric}: {res.content}")
            body = res.json()
            self.assertEqual(body["metric"], metric)
            self.assertTrue(body.get("title"), f"{metric} has no title")
            self.assertTrue(body.get("headline"), f"{metric} has no headline figure")
            self.assertTrue(body.get("sections"), f"{metric} has no sections")
            for section in body["sections"]:
                self.assertTrue(section.get("columns"), f"{metric}/{section['key']} has no columns")
                for row in section["rows"]:
                    self.assertEqual(
                        len(row), len(section["columns"]),
                        f"{metric}/{section['key']}: a row does not match its column count",
                    )
            checked.append(metric)
        # State the count out loud: a loop that silently iterated nothing would otherwise pass.
        self.assertEqual(len(checked), len(DETAIL_BUILDERS))
        self.assertEqual(len(checked), 11, f"expected 11 metrics, walked {checked}")

    def test_the_activity_breakdown_names_admins_rather_than_user_objects(self):
        # Grouping on the FK buckets by id and labels each row with a User repr.
        body = self.client.get(_detail_url("activity"), **self._auth(self.admin)).json()
        by_admin = next(s for s in body["sections"] if s["key"] == "by_admin")
        self.assertTrue(by_admin["rows"], "no admin rows; setUp writes four actions")
        for label, _count in by_admin["rows"]:
            self.assertNotIn("User object", str(label))

    def test_an_unknown_metric_404s_and_names_the_valid_ones(self):
        res = self.client.get(_detail_url("not-a-metric"), **self._auth(self.admin))
        self.assertEqual(res.status_code, 404)
        self.assertIn("members", res.json()["available"])

    def test_a_detail_view_is_admin_only_too(self):
        self.assertEqual(self.client.get(_detail_url("members")).status_code, 400)
        self.assertEqual(
            self.client.get(_detail_url("members"), **self._auth(self.player)).status_code, 403)

    def test_the_monthly_series_keeps_its_empty_months(self):
        # Twelve buckets, always. A series that drops its empty months draws a shape that lies.
        body = self.client.get(_detail_url("members"), **self._auth(self.admin)).json()
        by_month = next(s for s in body["sections"] if s["key"] == "by_month")
        self.assertEqual(len(by_month["rows"]), 12)
        self.assertEqual(sum(row[1] for row in by_month["rows"]), User.objects.count())
