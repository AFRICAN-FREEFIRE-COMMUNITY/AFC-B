"""
afc_rankings.test_home_rankings_feed (owner backlog #9 / #20, 2026-08-03):
"homepage ranking and tiers must show current results and update automatically".

WHY THIS MODULE EXISTS
    The home page's "Rankings and Tiers" card used to render two HARDCODED arrays
    (`teamRankings` / `quarterlyTiers` in frontend/constants/index.ts), so it showed a frozen
    snapshot of a past quarter no matter what the database said. Those arrays are deleted; the
    card is now frontend/app/(user)/_components/HomeRankingsTiers.tsx, which reads the SAME two
    public endpoints the /rankings page reads:

        GET /rankings/teams/monthly/    -> afc_rankings.views.teams_monthly
        GET /rankings/teams/quarterly/  -> afc_rankings.views.teams_quarterly

    The home page's correctness now rests entirely on the response contract of those two views,
    so this module locks that contract down. These are the guarantees the card depends on and
    that nothing else asserts directly today:

      1. PUBLISH GATE BLOCKS. With the active season unpublished, the monthly endpoint returns
         published=False and NO rows. The card must be able to tell "not published yet" apart
         from "no data", because rendering stale numbers here is the exact bug being fixed.
         (test_ghost_rankings.py only ever sets rankings_published=True to get past this gate;
         nothing asserted that the gate actually closes.)
      2. PUBLISHED READS ARE LIVE. Once published, the endpoint serves the current stored rows,
         in rank order, with the scores that are in the database right now.
      3. A SCORE EDIT SHOWS UP ON THE NEXT READ. There is no caching layer in front of these
         views, so an updated score is visible immediately. This is the "updates automatically"
         half of the owner's request: the client re-polls (useLiveTick) and gets fresh numbers.
      4. MONTH RESOLUTION FOLLOWS THE DATA. views._resolve_month defaults to the newest month
         whose season is PUBLISHED, so the card shows the most recent readable month rather than
         an empty current month. The card labels the month from the envelope's `month` field, so
         this value is user-visible. The last-published FALLBACK behaviour that this rule implies
         (keep showing the previous period while the live one is pending) is covered separately by
         LastPublishedPeriodFallbackTests below.

    Tier presentation is gated separately (Season.tiers_published); test 5 covers that the
    quarterly endpoint nulls `tier` while rankings are public, which is what makes the card show
    its "tiers coming soon" note instead of a column of blank badges.
"""
import datetime

from django.test import TestCase
from django.contrib.auth import get_user_model

from afc_team.models import Team
from afc_rankings.models import (
    Season, TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore,
)

User = get_user_model()

# A month inside the season below. Kept in the future so it can never collide with real cloned
# rows if this module is ever run against a populated database.
MONTH = datetime.date(2099, 2, 1)


class HomeRankingsFeedTests(TestCase):
    """The /home Rankings and Tiers card's backend contract (see module docstring)."""

    def setUp(self):
        self.owner = User.objects.create(username="home_owner", email="home_owner@example.com")
        self.season = Season.objects.create(
            name="Home Season 2099 Q1", quarter=1, year=2099,
            start_date=datetime.date(2099, 1, 1), end_date=datetime.date(2099, 3, 31),
            transfer_window_open=datetime.date(2099, 1, 1),
            transfer_window_close=datetime.date(2099, 1, 14),
            is_active=True,
            # Deliberately UNPUBLISHED: this is the state the production database is in today
            # (2026 Q3 active, rankings_published=False), and test 1 depends on it.
            rankings_published=False, tiers_published=False,
        )
        # Two teams with a clear score ordering, so rank order is unambiguous.
        self.top = self._team("Home Top FC")
        self.second = self._team("Home Second FC")
        self.top_month = TeamMonthlyScore.objects.create(
            team=self.top, month=MONTH, total_score=50.0, rank=1,
            tournament_wins=2, total_kills=120, tournaments_played=3,
        )
        TeamMonthlyScore.objects.create(
            team=self.second, month=MONTH, total_score=20.0, rank=2,
            tournament_wins=0, total_kills=40, tournaments_played=1,
        )

    def _team(self, name):
        return Team.objects.create(
            team_name=name, join_settings="open",
            team_creator=self.owner, team_owner=self.owner, country="NG",
        )

    def _publish_rankings(self):
        self.season.rankings_published = True
        self.season.save(update_fields=["rankings_published"])

    def _monthly(self):
        """GET the monthly ladder the way the home card does (no ?month, no auth)."""
        return self.client.get("/rankings/teams/monthly/").json()

    # ── 1. the gate actually closes ──────────────────────────────────────────
    def test_monthly_is_empty_and_flagged_while_season_unpublished(self):
        body = self._monthly()
        self.assertEqual(body["results"], [], "unpublished season leaked ranking rows to the public")
        self.assertIs(body["published"], False,
                      "published flag must be False so the card can say 'not published yet'")
        self.assertEqual(body["pagination"]["total_count"], 0)

    # ── 2. published reads serve the live stored rows ────────────────────────
    def test_monthly_returns_current_rows_in_rank_order_once_published(self):
        self._publish_rankings()
        body = self._monthly()
        self.assertIs(body["published"], True)
        names = [r["team_name"] for r in body["results"]]
        self.assertEqual(names, ["Home Top FC", "Home Second FC"], "rows not served in rank order")
        top = body["results"][0]
        self.assertEqual(top["total_score"], 50.0)
        self.assertEqual(top["wins"], 2)
        self.assertEqual(top["kills"], 120)

    # ── 3. an edit is visible on the very next read (no cache in front) ──────
    def test_score_change_is_reflected_on_the_next_read(self):
        self._publish_rankings()
        before = self._monthly()
        self.assertEqual(before["results"][0]["total_score"], 50.0)

        # Simulate what a recalc does after new match stats land: the stored score changes and
        # the order flips. recalc/rerank normally writes both fields; we write them directly so
        # this test pins the READ path, not the scoring maths (covered in tests.py / recalc tests).
        self.top_month.total_score = 5.0
        self.top_month.rank = 2
        self.top_month.save(update_fields=["total_score", "rank"])
        TeamMonthlyScore.objects.filter(team=self.second, month=MONTH).update(rank=1)

        after = self._monthly()
        self.assertEqual([r["team_name"] for r in after["results"]],
                         ["Home Second FC", "Home Top FC"],
                         "reordered standings were not picked up by the next read")
        self.assertEqual(after["results"][1]["total_score"], 5.0,
                         "updated score was not served, a cache is masking live data")

    # ── 4. month resolution follows the data ─────────────────────────────────
    def test_month_defaults_to_the_newest_readable_month(self):
        self._publish_rankings()
        # Both months below sit inside this (now published) season, so the newest wins. The case
        # where the newest month's season is NOT published is LastPublishedPeriodFallbackTests.
        # A newer readable month must win, so the card never shows an older month by default.
        newer = datetime.date(2099, 3, 1)
        TeamMonthlyScore.objects.create(
            team=self.top, month=newer, total_score=99.0, rank=1,
            tournament_wins=5, total_kills=300, tournaments_played=6,
        )
        body = self._monthly()
        self.assertEqual(body["month"], newer.isoformat(),
                         "envelope month must be the newest readable month (the card labels it)")
        self.assertEqual([r["total_score"] for r in body["results"]], [99.0],
                         "rows must come from the resolved month only")

    # ── 5. tiers are gated independently of scores ───────────────────────────
    def test_quarterly_hides_tier_until_tiers_published(self):
        self._publish_rankings()          # scores public, tiers still not
        TeamQuarterlyScore.objects.create(
            team=self.top, season=self.season, total_score=50.0, rank=1, tier_assigned=1,
        )
        body = self.client.get("/rankings/teams/quarterly/").json()
        self.assertEqual(len(body["results"]), 1, "published rankings should still return the row")
        self.assertIsNone(body["results"][0]["tier"],
                          "tier must stay hidden until tiers_published")

        self.season.tiers_published = True
        self.season.save(update_fields=["tiers_published"])
        body = self.client.get("/rankings/teams/quarterly/").json()
        self.assertEqual(body["results"][0]["tier"], 1, "tier not revealed after publishing tiers")


class LastPublishedPeriodFallbackTests(TestCase):
    """Owner 2026-08-03: "it should show the past one pending when a new one is published".

    While the LIVE season's rankings are unpublished, the public ladders must keep serving the most
    recent PUBLISHED period instead of going blank, flagged so the UI can label it. The genuine
    empty state survives only for the case where nothing has ever been published.

    Covers afc_rankings.views._resolve_month (monthly) and _resolve_quarterly_season (quarterly),
    plus the _period_meta envelope keys the /home card reads
    (frontend app/(user)/_components/HomeRankingsTiers.tsx).

    Mirrors the real production shape: SEASON 2 published and over, SEASON 3 live and pending, with
    their windows touching on 1 July exactly like the real rows.
    """

    def setUp(self):
        self.user = User.objects.create(username="fb_user", email="fb@example.com")
        self.team = Team.objects.create(
            team_name="Fallback FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        self.old = Season.objects.create(
            name="Old Published Q", quarter=2, year=2099,
            start_date=datetime.date(2099, 4, 1), end_date=datetime.date(2099, 7, 1),
            transfer_window_open=datetime.date(2099, 4, 1),
            transfer_window_close=datetime.date(2099, 4, 14),
            is_active=False, rankings_published=True, tiers_published=True,
        )
        self.live = Season.objects.create(
            name="Live Pending Q", quarter=3, year=2099,
            start_date=datetime.date(2099, 7, 1), end_date=datetime.date(2099, 10, 1),
            transfer_window_open=datetime.date(2099, 7, 1),
            transfer_window_close=datetime.date(2099, 7, 14),
            is_active=True, rankings_published=False, tiers_published=False,
        )
        # June belongs to the PUBLISHED season, August to the live PENDING one. August is newer, so
        # the pre-fix "newest populated month" rule would have picked it and returned nothing.
        self.published_month = datetime.date(2099, 6, 1)
        self.pending_month = datetime.date(2099, 8, 1)
        for month, score in ((self.published_month, 10.0), (self.pending_month, 99.0)):
            TeamMonthlyScore.objects.create(team=self.team, month=month, total_score=score, rank=1)
            PlayerMonthlyScore.objects.create(player=self.user, month=month, total_score=score, rank=1)
        TeamQuarterlyScore.objects.create(
            team=self.team, season=self.old, total_score=10.0, rank=1, tier_assigned=1)
        TeamQuarterlyScore.objects.create(
            team=self.team, season=self.live, total_score=99.0, rank=1, tier_assigned=0)

    # ── 1. active season unpublished + an older published season -> show the older, flagged ──
    def test_monthly_falls_back_to_the_last_published_month(self):
        for url, label in (("/rankings/teams/monthly/", "teams"),
                           ("/rankings/players/monthly/", "players")):
            body = self.client.get(url).json()
            self.assertEqual(body["month"], self.published_month.isoformat(),
                             f"{label}: should fall back to the last PUBLISHED month")
            self.assertIs(body["published"], True, f"{label}: fallback period must be readable")
            self.assertEqual(len(body["results"]), 1, f"{label}: fallback rows missing")
            self.assertEqual(body["results"][0]["total_score"], 10.0,
                             f"{label}: served the pending month's numbers")
            self.assertIs(body["is_current_period"], False,
                          f"{label}: stale period must be flagged so the UI can label it")
            self.assertEqual(body["current_season"]["name"], "Live Pending Q",
                             f"{label}: envelope must name the season still pending")

    def test_quarterly_falls_back_to_the_last_published_season(self):
        for url in ("/rankings/teams/quarterly/", "/rankings/players/quarterly/"):
            body = self.client.get(url).json()
            self.assertEqual(body["season"]["name"], "Old Published Q", f"{url}: wrong season served")
            self.assertIs(body["is_current_period"], False, f"{url}: stale season not flagged")
            self.assertEqual(body["current_season"]["name"], "Live Pending Q")
        # The rows served are the published season's, not the pending season's.
        body = self.client.get("/rankings/teams/quarterly/").json()
        self.assertEqual([r["total_score"] for r in body["results"]], [10.0])

    # ── 2. nothing ever published -> the genuine empty state survives ────────
    def test_empty_state_survives_when_nothing_was_ever_published(self):
        self.old.rankings_published = False
        self.old.tiers_published = False
        self.old.save(update_fields=["rankings_published", "tiers_published"])
        monthly = self.client.get("/rankings/teams/monthly/").json()
        self.assertIs(monthly["published"], False, "unpublished data must stay hidden")
        self.assertEqual(monthly["results"], [])
        # With nothing published we report the newest populated month, so the empty state can name it.
        self.assertEqual(monthly["month"], self.pending_month.isoformat())
        quarterly = self.client.get("/rankings/teams/quarterly/").json()
        self.assertEqual(quarterly["results"], [], "unpublished season leaked quarterly rows")

    # ── 3. current season published -> show current, NOT flagged ─────────────
    def test_current_period_is_served_and_unflagged_once_published(self):
        self.live.rankings_published = True
        self.live.save(update_fields=["rankings_published"])
        monthly = self.client.get("/rankings/teams/monthly/").json()
        self.assertEqual(monthly["month"], self.pending_month.isoformat(),
                         "once published, the live month must win again")
        self.assertEqual(monthly["results"][0]["total_score"], 99.0)
        self.assertIs(monthly["is_current_period"], True, "current period must not be flagged stale")
        quarterly = self.client.get("/rankings/teams/quarterly/").json()
        self.assertEqual(quarterly["season"]["name"], "Live Pending Q")
        self.assertIs(quarterly["is_current_period"], True)

    # ── an explicit ?month= still wins over the fallback (deep links / the /rankings picker) ──
    def test_explicit_month_is_not_overridden_by_the_fallback(self):
        body = self.client.get("/rankings/teams/monthly/?month=2099-08").json()
        self.assertEqual(body["month"], self.pending_month.isoformat())
        self.assertIs(body["published"], False, "explicit month must still obey its own season gate")
