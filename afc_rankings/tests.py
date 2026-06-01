import datetime

from django.test import TestCase
from django.contrib.auth import get_user_model

from afc_team.models import Team
from afc_rankings.models import Season, TeamQuarterlyScore, PlayerQuarterlyScore
from afc_rankings import recalc as R

User = get_user_model()


class StickyBanGuardTests(TestCase):
    """§2.15 sticky-ban guard.

    A zeroed (banned) team/player must survive an unrelated recalc. Before the
    guard, ``recalc_team_quarterly`` / ``recalc_player_quarterly`` overwrote
    ``total_score`` + ``tier_assigned`` from raw aggregation on every run, so any
    later edit to the entity's data (a marker, a prize, a ghost transfer — each
    enqueues a recalc on commit) silently un-banned a zeroed entity: the score
    and tier returned while only the ``is_zeroed`` flag lingered.
    """

    def setUp(self):
        self.user = User.objects.create(username="owner1", email="o1@example.com")
        self.season = Season.objects.create(
            name="Test Season", quarter=1, year=2099,
            start_date=datetime.date(2099, 1, 1), end_date=datetime.date(2099, 3, 31),
            transfer_window_open=datetime.date(2099, 1, 1),
            transfer_window_close=datetime.date(2099, 1, 14),
            is_active=True,
        )
        self.team = Team.objects.create(
            team_name="Banned FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )

    def test_zeroed_team_survives_recalc(self):
        TeamQuarterlyScore.objects.create(
            team=self.team, season=self.season,
            total_score=0, tier_assigned=3, is_zeroed=True, zeroed_reason="Confirmed cheating",
        )
        # an unrelated recalc fires (e.g. a later prize/marker edit enqueues it)
        R.recalc_team_quarterly(self.team.team_id, self.season.season_id)
        row = TeamQuarterlyScore.objects.get(team=self.team, season=self.season)
        self.assertTrue(row.is_zeroed, "ban flag was cleared by recalc")
        self.assertEqual(row.total_score, 0, "zeroed score was overwritten by recalc")
        self.assertEqual(row.tier_assigned, 3, "zeroed tier was overwritten by recalc")

    def test_zeroed_player_survives_recalc(self):
        player = User.objects.create(username="cheater1", email="c1@example.com")
        PlayerQuarterlyScore.objects.create(
            player=player, season=self.season,
            total_score=0, tier_assigned=3, is_zeroed=True, zeroed_reason="Confirmed cheating",
        )
        R.recalc_player_quarterly(player.pk, self.season.season_id)
        row = PlayerQuarterlyScore.objects.get(player=player, season=self.season)
        self.assertTrue(row.is_zeroed, "ban flag was cleared by recalc")
        self.assertEqual(row.total_score, 0, "zeroed score was overwritten by recalc")
        self.assertEqual(row.tier_assigned, 3, "zeroed tier was overwritten by recalc")

    def test_unzeroed_team_recomputes_normally(self):
        # control: a NON-zeroed team with no activity is removed by the §7.4 floor,
        # proving the guard only protects zeroed rows (not a blanket early-return).
        TeamQuarterlyScore.objects.create(
            team=self.team, season=self.season,
            total_score=120, tier_assigned=1, is_zeroed=False,
        )
        R.recalc_team_quarterly(self.team.team_id, self.season.season_id)
        self.assertFalseExists = TeamQuarterlyScore.objects.filter(
            team=self.team, season=self.season
        ).exists()
        self.assertFalse(self.assertFalseExists, "non-zeroed inactive team should be dropped by the floor")
