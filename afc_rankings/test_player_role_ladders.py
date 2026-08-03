"""
test_player_role_ladders.py
───────────────────────────
Covers ``afc_rankings.player_roles.players_by_role`` - the per-role player ladders the owner
asked for ("sniper rankings, rusher rankings, etc").

WHAT THESE TESTS PIN DOWN
    1. A role table is a FILTER, not a second scoring system: the scores are byte-for-byte the
       ones the main ladder shows, only the population differs.
    2. Ranks inside a role table are ranks WITHIN the role (1, 2, 3...), not the global ones,
       and the global number survives as ``overall_rank``. This is the whole point of the
       feature - a table numbered 3, 17, 24 would read as broken.
    3. The publish gates are the SAME as the main ladder's. A role table must never be a hole
       in the gate that shows an unpublished period, and the quarterly tier badge stays hidden
       until tiers are published independently.
    4. Players with no in-game role, and ghost players (no roster at all), are absent from
       every role table but still present in the unfiltered one.
    5. The tab counts describe the SCORED population for the period, so a role with rostered
       players but nobody scored reads 0 rather than opening onto an empty table.

Exercised through APIRequestFactory, the function-view idiom the rest of afc_rankings uses
(see test_public_gating.py).
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from afc_team.models import Team, TeamMembers
from afc_rankings import player_roles, views
from afc_rankings.models import (
    GhostPlayer, GhostTeam, PlayerMonthlyScore, PlayerQuarterlyScore, Season,
)

User = get_user_model()

MONTH = datetime.date(2098, 5, 1)


def _season(**overrides):
    """A season whose window contains MONTH. Published unless a test says otherwise."""
    defaults = dict(
        name="Role Q", quarter=2, year=2098,
        start_date=datetime.date(2098, 4, 1), end_date=datetime.date(2098, 7, 1),
        transfer_window_open=datetime.date(2098, 4, 1),
        transfer_window_close=datetime.date(2098, 4, 14),
        is_active=True, rankings_published=True, tiers_published=True,
    )
    defaults.update(overrides)
    return Season.objects.create(**defaults)


class RoleLadderTests(TestCase):
    """The monthly role ladder: filtering, within-role ranks, counts, and who is excluded."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.season = _season()

        # One team so every player has somewhere to hold a role. A user belongs to at most one
        # team (unique_member_one_team), so one team is enough for four distinct roles.
        owner = User.objects.create(username="role_owner", email="ro@example.com")
        self.team = Team.objects.create(
            team_name="Role FC", join_settings="open",
            team_creator=owner, team_owner=owner, country="NG",
        )

        # Scores descend so the global ranks are 1..6 in the order created. Two snipers sit at
        # global 2 and 5, which is what makes the within-role renumbering visible: they must
        # come back as 1 and 2, not 2 and 5.
        self.players = {}
        roster = [
            ("rush_a", 100.0, "rusher"),
            ("snipe_a", 90.0, "sniper"),
            ("supp_a", 80.0, "support"),
            ("nade_a", 70.0, "grenader"),
            ("snipe_b", 60.0, "sniper"),
            ("staff_a", 50.0, None),        # a coach/manager: on the roster, no in-game role
        ]
        for rank, (username, score, role) in enumerate(roster, start=1):
            user = User.objects.create(username=username, email=f"{username}@example.com")
            self.players[username] = user
            TeamMembers.objects.create(
                team=self.team, member=user,
                management_role="coach" if role is None else "member",
                in_game_role=role,
            )
            PlayerMonthlyScore.objects.create(
                player=user, month=MONTH, total_score=score, rank=rank,
            )

        # A ghost player: interleaved into the ladder by score, but with no roster row and
        # therefore no role. Global rank 7, below everyone above.
        ghost_team = GhostTeam.objects.create(
            team_name="Ghost FC", country="NG", created_by=owner,
        )
        self.ghost = GhostPlayer.objects.create(ghost_team=ghost_team, ign="GhostSniper", slot=1)
        PlayerMonthlyScore.objects.create(
            ghost_player=self.ghost, month=MONTH, total_score=40.0, rank=7,
        )

    def _get(self, **params):
        params.setdefault("month", "2098-05")
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return player_roles.players_by_role(
            self.factory.get(f"/rankings/players/by-role/?{qs}")
        ).data

    # ── 1. the unfiltered ladder is the ladder ──
    def test_no_role_returns_the_whole_ladder_with_global_ranks(self):
        body = self._get()
        self.assertIsNone(body["role"])
        self.assertEqual(body["period"], "monthly")
        self.assertEqual(len(body["results"]), 7)          # 6 real + 1 ghost
        self.assertEqual([r["rank"] for r in body["results"]], [1, 2, 3, 4, 5, 6, 7])
        # overall_rank is present on every shape so the client renders one row component.
        self.assertEqual([r["overall_rank"] for r in body["results"]], [1, 2, 3, 4, 5, 6, 7])

    def test_all_sentinel_is_the_same_as_no_role(self):
        self.assertEqual(self._get(role="all")["results"],
                         self._get()["results"])

    def test_unknown_role_degrades_to_the_full_ladder(self):
        """A stale bookmark shows the ladder rather than an error page."""
        body = self._get(role="igl")
        self.assertIsNone(body["role"])
        self.assertEqual(len(body["results"]), 7)

    # ── 2. within-role ranks, the headline behaviour ──
    def test_role_table_ranks_are_within_the_role(self):
        body = self._get(role="sniper")
        self.assertEqual(body["role"], "sniper")
        self.assertEqual([r["username"] for r in body["results"]], ["snipe_a", "snipe_b"])
        self.assertEqual([r["rank"] for r in body["results"]], [1, 2])
        # ...and the global position is still available for a "2nd overall" label.
        self.assertEqual([r["overall_rank"] for r in body["results"]], [2, 5])

    def test_scores_are_identical_to_the_main_ladder(self):
        """A role table re-orders, it never re-scores."""
        main = {r["username"]: r["total_score"]
                for r in views.players_monthly(
                    self.factory.get("/rankings/players/monthly/?month=2098-05")).data["results"]}
        for row in self._get(role="sniper")["results"]:
            self.assertEqual(row["total_score"], main[row["username"]])

    def test_within_role_ranks_continue_across_pages(self):
        """Page 2 continues 2, 3... - it does not restart at 1."""
        body = self._get(role="sniper", limit=1, offset=1)
        self.assertEqual([r["rank"] for r in body["results"]], [2])
        self.assertEqual(body["pagination"]["total_count"], 2)

    # ── 3. who is excluded ──
    def test_roleless_player_is_in_no_role_table(self):
        for role in player_roles.ROLE_KEYS:
            names = [r["username"] for r in self._get(role=role)["results"]]
            self.assertNotIn("staff_a", names, f"staff_a leaked into the {role} table")
        # ...but is still on the full ladder.
        self.assertIn("staff_a", [r["username"] for r in self._get()["results"]])

    def test_ghost_player_is_in_no_role_table(self):
        """A ghost has no roster row, so it has no role. It stays on the unfiltered ladder."""
        for role in player_roles.ROLE_KEYS:
            self.assertFalse(
                any(r["is_ghost"] for r in self._get(role=role)["results"]),
                f"a ghost row leaked into the {role} table",
            )
        self.assertTrue(any(r["is_ghost"] for r in self._get()["results"]))

    # ── 4. the tab bar ──
    def test_catalog_lists_every_role_with_scored_counts(self):
        counts = {r["role"]: r["player_count"] for r in self._get()["roles"]}
        self.assertEqual(counts, {"rusher": 1, "support": 1, "grenader": 1, "sniper": 2})
        # every role is listed even at zero, so the tab bar keeps its shape
        self.assertEqual(len(self._get()["roles"]), len(player_roles.ROLE_KEYS))

    def test_counts_ignore_rostered_players_with_no_score_this_month(self):
        """A role can be full of players and still read 0 for a month nobody played."""
        extra = User.objects.create(username="snipe_c", email="sc@example.com")
        TeamMembers.objects.create(team=self.team, member=extra, in_game_role="sniper")
        counts = {r["role"]: r["player_count"] for r in self._get()["roles"]}
        self.assertEqual(counts["sniper"], 2)          # snipe_c has no score row this month

    def test_counts_are_the_same_whichever_role_is_selected(self):
        """The tabs describe the period, so they must not move as the user clicks between them."""
        self.assertEqual(self._get(role="sniper")["roles"], self._get(role="rusher")["roles"])


class RoleLadderGatingTests(TestCase):
    """The role ladder must obey exactly the gates the main ladder obeys."""

    def setUp(self):
        self.factory = APIRequestFactory()
        owner = User.objects.create(username="gate_owner", email="go@example.com")
        self.team = Team.objects.create(
            team_name="Gate Role FC", join_settings="open",
            team_creator=owner, team_owner=owner, country="NG",
        )
        self.user = User.objects.create(username="gate_sniper", email="gs@example.com")
        TeamMembers.objects.create(team=self.team, member=self.user, in_game_role="sniper")

    def _get(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return player_roles.players_by_role(
            self.factory.get(f"/rankings/players/by-role/?{qs}")
        ).data

    def test_unpublished_season_serves_nothing_but_keeps_the_tab_bar(self):
        _season(rankings_published=False, tiers_published=False)
        PlayerMonthlyScore.objects.create(player=self.user, month=MONTH, total_score=10, rank=1)
        body = self._get(role="sniper", month="2098-05")
        self.assertFalse(body["published"])
        self.assertEqual(body["results"], [])
        # the UI keeps its shape rather than collapsing to nothing
        self.assertEqual(len(body["roles"]), len(player_roles.ROLE_KEYS))
        self.assertTrue(all(r["player_count"] == 0 for r in body["roles"]))

    def test_quarterly_role_table_renumbers_and_carries_the_tier(self):
        season = _season()
        other = User.objects.create(username="q_sniper_b", email="qsb@example.com")
        TeamMembers.objects.create(team=self.team, member=other, in_game_role="sniper")
        middle = User.objects.create(username="q_rusher", email="qr@example.com")
        TeamMembers.objects.create(team=self.team, member=middle, in_game_role="rusher")
        for rank, (user, score) in enumerate(
            [(self.user, 90.0), (middle, 80.0), (other, 70.0)], start=1
        ):
            PlayerQuarterlyScore.objects.create(
                player=user, season=season, total_score=score, rank=rank, tier_assigned=1,
            )
        body = self._get(role="sniper", period="quarterly")
        self.assertEqual(body["period"], "quarterly")
        self.assertEqual([r["rank"] for r in body["results"]], [1, 2])
        self.assertEqual([r["overall_rank"] for r in body["results"]], [1, 3])
        self.assertEqual([r["tier"] for r in body["results"]], [1, 1])

    def test_quarterly_tier_stays_hidden_until_tiers_are_published(self):
        """tiers_published is a SECOND, independent gate (views._gated_quarterly)."""
        season = _season(tiers_published=False)
        PlayerQuarterlyScore.objects.create(
            player=self.user, season=season, total_score=90, rank=1, tier_assigned=0,
        )
        body = self._get(role="sniper", period="quarterly")
        self.assertTrue(body["published"])
        self.assertEqual(len(body["results"]), 1)
        self.assertIsNone(body["results"][0]["tier"])
        self.assertIsNone(body["results"][0]["tier_label"])

    def test_period_defaults_to_monthly_for_an_unrecognised_value(self):
        _season()
        PlayerMonthlyScore.objects.create(player=self.user, month=MONTH, total_score=10, rank=1)
        self.assertEqual(self._get(period="annual", month="2098-05")["period"], "monthly")
