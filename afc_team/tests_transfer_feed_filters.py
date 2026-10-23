"""
afc_team/tests_transfer_feed_filters.py
================================================================================
Search and filters on the public transfer feed, GET /team/transfers/ (inbox #146 / #161, owner
2026-10-05: "the transfers page doesnt loook good, no pagination and also no search ... the
filters should be better, like by countries, or by tiers or by teams/players").

Covers: q over player and team names (live and as recorded at the move), country, tier and
direction filters, the country / tier options taken from the whole feed, and coded refusals for a
bad tier, an unknown direction and an over-long search. Reuses the shared fixture of
tests_transfer_feed.py (a team that has competed, builders for teams, players and matches).

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_team.tests_transfer_feed_filters --keepdb
"""
import datetime

from afc_rankings.models import Season, TeamQuarterlyScore
from afc_team.models import Team, TeamMembers
from afc_team.tests_transfer_feed import TransferFeedTestCase


class TransferFeedFilterTests(TransferFeedTestCase):
    def setUp(self):
        super().setUp()
        # A second team that has competed, in another country and tier.
        self.other = self._team("Lagos Lions", self.owner)
        self._record_a_played_match(self.other)
        self.ama = self._player("ama_sniper")
        self.tunde = self._player("tunde_rush")
        TeamMembers.objects.create(team=self.competed, member=self.ama)
        TeamMembers.objects.create(team=self.other, member=self.tunde)
        # Tunde leaves: one "left" row to filter on.
        TeamMembers.objects.filter(team=self.other, member=self.tunde).delete()
        # Set AFTER the roster moves: every move recomputes the team's country from its members
        # (afc_team/signals.py), and a queryset update skips that.
        Team.objects.filter(pk=self.competed.pk).update(country="Ghana")
        Team.objects.filter(pk=self.other.pk).update(country="Nigeria")
        # Tiers are the RANKING tiers of the latest season with tiers published (not Team.team_tier,
        # which is "3" for every team on production). An older published season says otherwise,
        # and an unpublished newer one too: neither may be read.
        old = self._tier_season("Old", datetime.date(2025, 1, 1), published=True)
        current = self._tier_season("Current", datetime.date(2026, 1, 1), published=True)
        draft = self._tier_season("Draft", datetime.date(2026, 4, 1), published=False)
        TeamQuarterlyScore.objects.create(team=self.competed, season=old, tier_assigned=2)
        TeamQuarterlyScore.objects.create(team=self.competed, season=current, tier_assigned=1)
        TeamQuarterlyScore.objects.create(team=self.other, season=current, tier_assigned=2)
        TeamQuarterlyScore.objects.create(team=self.other, season=draft, tier_assigned=1)

    def _tier_season(self, name, start, published):
        return Season.objects.create(
            name=name, year=start.year, quarter=(start.month - 1) // 3 + 1, start_date=start,
            end_date=start + datetime.timedelta(days=80), transfer_window_open=start,
            transfer_window_close=start + datetime.timedelta(days=13), tiers_published=published,
        )

    def _names(self, **params):
        return sorted((r["player_username"], r["direction"]) for r in self._feed(**params)["results"])

    def test_search_finds_a_player_by_part_of_the_name(self):
        self.assertEqual(self._names(q="SNIP"), [("ama_sniper", "joined")])

    def test_search_finds_a_teams_moves_by_team_name(self):
        names = self._names(q="lagos")
        self.assertEqual(names, [("tunde_rush", "joined"), ("tunde_rush", "left")])

    def test_search_finds_a_renamed_player_under_the_old_name(self):
        self.ama.username = "ama_new_name"
        self.ama.save(update_fields=["username"])
        self.assertEqual(self._names(q="ama_sniper"), [("ama_new_name", "joined")])

    def test_country_tier_and_direction_filters(self):
        self.assertEqual(self._names(country="Ghana"), [("ama_sniper", "joined")])
        self.assertEqual(self._names(tier="2"), [("tunde_rush", "joined"), ("tunde_rush", "left")])
        self.assertEqual(self._names(tier="1"), [("ama_sniper", "joined")])
        self.assertEqual(self._names(tier="3"), [])
        self.assertEqual(self._names(direction="left"), [("tunde_rush", "left")])
        self.assertEqual(self._names(country="Nigeria", direction="joined"), [("tunde_rush", "joined")])

    def test_options_come_from_the_whole_feed_not_the_filtered_page(self):
        data = self._feed(country="Ghana")
        self.assertEqual(data["countries"], ["Ghana", "Nigeria"])
        self.assertEqual(data["tiers"], ["1", "2"])
        self.assertEqual(data["total_count"], 1)

    def test_bad_values_are_refused_with_codes(self):
        for params, code in (({"tier": "one"}, "tier_number"),
                             ({"direction": "sideways"}, "direction_unknown"),
                             ({"q": "x" * 51}, "query_too_long")):
            with self.subTest(params=params):
                response = self.client.get("/team/transfers/", params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], code)
