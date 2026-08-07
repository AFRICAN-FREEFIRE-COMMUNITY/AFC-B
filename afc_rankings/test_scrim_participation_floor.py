"""
test_scrim_participation_floor.py
─────────────────────────────────
Covers the participation-floor gates in recalc.py letting SCRIM activity through (owner
2026-08-03 / backlog item 14: "scrims must count toward tiers and rankings").

WHAT WAS WRONG. There are four gates that decide whether an entity keeps a score row at all:

    recalc_team_monthly      team_monthly_floor      amended 2026-08-03 (scrim escape added)
    recalc_team_quarterly    tournaments_played == 0   NOT amended
    recalc_player_monthly    player_monthly_floor      NOT amended
    recalc_player_quarterly  tournaments_played == 0   NOT amended

Only the first was fixed when the scrim cap was given a flat allowance, so a scrim-only team
scored on the MONTHLY ladder and was then deleted off the SEASON ladder - and the season ladder is
the table tiers are read from (recalc.team_quarter_ladder -> top_n_team_tiers / assign_tier). That
is precisely "scrims do not count toward tiers": the points existed, the row that would have
carried a tier did not. Players had it worse still, being dropped from both of their ladders.

The scrim points themselves (the flat cap that makes a scrim-only team score at all) are covered by
test_scrim_flat_cap.py, and the per-event toggles reaching scrims by test_scrim_counting.py. This
file is only about whether the ROW SURVIVES.

Fixture shape mirrors test_scrim_counting.py.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats, TournamentPlayerMatchStats,
)
from afc_rankings import recalc
from afc_rankings.models import (
    EventCountingControl, PlayerMonthlyScore, PlayerQuarterlyScore, Season,
    TeamMonthlyScore, TeamQuarterlyScore,
)

User = get_user_model()

# Inside the season built below, so the same fixture serves the monthly and quarterly gates.
PLAY_DAY = datetime.date(2099, 5, 10)
MONTH = datetime.date(2099, 5, 1)


class _ScrimFixtureMixin:
    """Team + player + season, and helpers to hang a scrim (or tournament) with results on them.

    Shared by the three classes below so the object graph is built one way only. Same shape as
    test_scrim_counting.py: Event -> Stages -> StageGroups -> Match -> TournamentTeam ->
    TournamentTeamMatchStats -> TournamentPlayerMatchStats.
    """

    def _make_entities(self, *, username, team_name, season_name):
        self.user = User.objects.create(username=username, email=f"{username}@example.com")
        self.team = Team.objects.create(
            team_name=team_name, join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        self.season = Season.objects.create(
            name=season_name, quarter=2, year=2099,
            start_date=datetime.date(2099, 4, 1), end_date=datetime.date(2099, 6, 30),
            transfer_window_open=datetime.date(2099, 4, 1),
            transfer_window_close=datetime.date(2099, 4, 14),
            is_active=True,
        )

    def _event(self, name, competition_type):
        return Event.objects.create(
            event_name=name, competition_type=competition_type, participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=PLAY_DAY, end_date=PLAY_DAY,
            registration_open_date=PLAY_DAY - datetime.timedelta(days=5),
            registration_end_date=PLAY_DAY - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
            number_of_stages=1, creator=self.user, is_draft=False,
        )

    def _result(self, event, *, placement, kills):
        stage = Stages.objects.create(
            event=event, stage_name="Main", start_date=PLAY_DAY, end_date=PLAY_DAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=PLAY_DAY,
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        match = Match.objects.create(
            group=group, match_map="bermuda", match_number=1, played_on=PLAY_DAY,
        )
        tt = TournamentTeam.objects.create(
            event=event, team=self.team, registered_by=self.user, status="active",
        )
        ts = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=placement, kills=kills,
        )
        TournamentPlayerMatchStats.objects.create(
            team_stats=ts, player=self.user, kills=kills, played=True,
        )
        return ts


class ScrimOnlyKeepsItsRowTests(_ScrimFixtureMixin, TestCase):
    """A team/player whose only results are scrims stays on every ladder."""

    def setUp(self):
        self._make_entities(username="scrim_floor", team_name="Scrims Only FC",
                            season_name="Scrim Season 2099 Q2")
        self.scrim = self._event("Tuesday Scrims", "scrims")
        self._result(self.scrim, placement=1, kills=20)

    # ── the gate that already worked (guard against a regression) ──
    def test_team_keeps_its_monthly_row(self):
        recalc.recalc_team_monthly(self.team.team_id, MONTH)
        row = TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).first()
        self.assertIsNotNone(row)
        self.assertGreater(row.scrim_pts, 0)

    # ── the three gates this change fixes ──
    def test_team_keeps_its_SEASON_row_and_is_therefore_tiered(self):
        """The season row is what carries tier_assigned, so its absence WAS the missing tier."""
        recalc.recalc_team_quarterly(self.team.team_id, self.season.season_id)
        row = TeamQuarterlyScore.objects.filter(team=self.team, season=self.season).first()
        self.assertIsNotNone(row)
        self.assertGreater(row.scrim_pts, 0)
        self.assertIsNotNone(row.tier_assigned)

    def test_player_keeps_their_monthly_row(self):
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        row = PlayerMonthlyScore.objects.filter(player=self.user, month=MONTH).first()
        self.assertIsNotNone(row)
        self.assertGreater(row.scrim_kill_pts + row.scrim_win_pts, 0)

    def test_player_keeps_their_season_row(self):
        recalc.recalc_player_quarterly(self.user.pk, self.season.season_id)
        row = PlayerQuarterlyScore.objects.filter(player=self.user, season=self.season).first()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row.tier_assigned)

    # ── the floor still does its actual job ──
    def test_a_scrim_only_team_is_still_below_the_TOURNAMENT_floor(self):
        """Keeping the row is not the same as meeting the floor.

        ``meets_participation_floor`` counts TOURNAMENTS and is left alone on purpose: a team that
        played no tournament has not met a tournament floor, and saying otherwise would quietly
        promote scrim-only teams past teams that competed. What changed is only that the team is
        now visible on the ladder with the points it earned, instead of being deleted from it.
        """
        recalc.recalc_team_quarterly(self.team.team_id, self.season.season_id)
        row = TeamQuarterlyScore.objects.get(team=self.team, season=self.season)
        self.assertEqual(row.participated_in_tournaments, 0)
        self.assertFalse(row.meets_participation_floor)
        self.assertIn("Insufficient activity", row.insufficient_activity_note)


class NoActivityStillDropsTheRowTests(_ScrimFixtureMixin, TestCase):
    """The floor must still remove an entity with NOTHING - that is the case it exists for."""

    def setUp(self):
        self._make_entities(username="no_activity", team_name="Idle FC",
                            season_name="Idle Season 2099 Q2")

    def test_team_with_no_results_has_no_monthly_row(self):
        recalc.recalc_team_monthly(self.team.team_id, MONTH)
        self.assertFalse(TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).exists())

    def test_team_with_no_results_has_no_season_row(self):
        recalc.recalc_team_quarterly(self.team.team_id, self.season.season_id)
        self.assertFalse(
            TeamQuarterlyScore.objects.filter(team=self.team, season=self.season).exists())

    def test_player_with_no_results_has_no_monthly_row(self):
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        self.assertFalse(PlayerMonthlyScore.objects.filter(player=self.user, month=MONTH).exists())

    def test_player_with_no_results_has_no_season_row(self):
        recalc.recalc_player_quarterly(self.user.pk, self.season.season_id)
        self.assertFalse(
            PlayerQuarterlyScore.objects.filter(player=self.user, season=self.season).exists())


class SwitchedOffScrimStillDropsTheRowTests(_ScrimFixtureMixin, TestCase):
    """The two changes must not fight each other.

    A scrim-only entity keeps its row BECAUSE it has scrim points. Switch that scrim off with the
    master toggle and the points go, so the row must go with them - otherwise the new escape hatch
    would quietly resurrect exactly the events an admin just switched off.
    """

    def setUp(self):
        self._make_entities(username="off_scrim", team_name="Switched Off FC",
                            season_name="Off Season 2099 Q2")
        self.scrim = self._event("Tuesday Scrims", "scrims")
        self._result(self.scrim, placement=1, kills=20)
        EventCountingControl.objects.create(event=self.scrim, counts_toward_rankings=False)

    def test_monthly_row_is_removed(self):
        recalc.recalc_team_monthly(self.team.team_id, MONTH)
        self.assertFalse(TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).exists())

    def test_season_row_is_removed(self):
        recalc.recalc_team_quarterly(self.team.team_id, self.season.season_id)
        self.assertFalse(
            TeamQuarterlyScore.objects.filter(team=self.team, season=self.season).exists())

    def test_player_rows_are_removed(self):
        recalc.recalc_player_monthly(self.user.pk, MONTH)
        recalc.recalc_player_quarterly(self.user.pk, self.season.season_id)
        self.assertFalse(PlayerMonthlyScore.objects.filter(player=self.user, month=MONTH).exists())
        self.assertFalse(
            PlayerQuarterlyScore.objects.filter(player=self.user, season=self.season).exists())
