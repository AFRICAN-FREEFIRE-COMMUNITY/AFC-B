"""
test_scrim_counting.py
──────────────────────
Covers scrim results feeding the rankings, and the admin counting controls reaching them
(owner 2026-08-03: "scrims must count toward tiers and rankings ... admin toggle so all events
count by default, with admins able to switch individual ones off").

WHAT WAS WRONG: ``aggregation._collect_team`` / ``_collect_player`` routed a scrim match into the
scrim bucket WITHOUT its event_id, so the three ``EventCountingControl`` toggles, the per-entity
``ResultExclusion`` rows and the unverified-organizer gate all applied to tournaments only. A scrim
event's placement/kill/win points counted unconditionally and there was no way for an admin to turn
one off. The scrim rows now carry their event_id and go through the same three gates.

SUPERSEDED (owner 2026-08-03): the 30%-of-tournament-points cap used to mean a scrim-only team
scored 0 and was then dropped by the §5.2 participation floor. The owner has since ruled that
scrims must count toward rankings, so the cap is now the HIGHER of a flat allowance and that 30%,
and scrim activity satisfies the participation floor. ``test_scrim_only_team_now_scores`` locks in
the new contract; the cap itself is covered by afc_rankings/test_scrim_flat_cap.py.

The fixture builds the minimal object graph aggregation walks:
Event(competition_type=...) -> Stages -> StageGroups -> Match -> TournamentTeam ->
TournamentTeamMatchStats -> TournamentPlayerMatchStats.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats, TournamentPlayerMatchStats,
)
from afc_rankings import aggregation
from afc_rankings.models import EventCountingControl, ResultExclusion

User = get_user_model()

MONTH = datetime.date(2099, 5, 1)
PLAY_DAY = datetime.date(2099, 5, 10)


class ScrimCountingControlTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="scrim_player", email="sp@example.com")
        self.team = Team.objects.create(
            team_name="Scrim FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        # One TOURNAMENT (so the team clears the §5.2 participation floor and the 30% scrim cap is
        # non-zero) plus one SCRIM whose points we then toggle on and off.
        self.tournament = self._event("Ranked Cup", "tournament")
        self.scrim = self._event("Tuesday Scrims", "scrims")
        self._result(self.tournament, placement=1, kills=10)
        self._result(self.scrim, placement=1, kills=8)

    # ── fixture helpers ──
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
        """One played match in ``event`` where self.team finishes ``placement`` with ``kills``."""
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

    # ── baseline: scrims DO contribute once the team has tournament points ──
    def test_scrim_points_count_by_default(self):
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertGreater(agg.result.scrim_pts, 0)

    def test_player_scrim_kills_count_by_default(self):
        agg = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertGreater(agg.result.scrim_kill_pts, 0)
        self.assertGreater(agg.result.scrim_win_pts, 0)

    # ── the fix: the admin toggles now reach a scrim event ──
    def test_disabling_kills_on_a_scrim_drops_its_kill_points(self):
        EventCountingControl.objects.create(event=self.scrim, count_kills=False)
        agg = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertEqual(agg.result.scrim_kill_pts, 0)
        # The scrim WIN is a separate toggle and is untouched.
        self.assertGreater(agg.result.scrim_win_pts, 0)

    def test_disabling_the_winner_flag_on_a_scrim_drops_its_win_bonus(self):
        EventCountingControl.objects.create(event=self.scrim, count_winner=False)
        agg = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertEqual(agg.result.scrim_win_pts, 0)

    def test_disabling_every_component_zeroes_the_teams_scrim_points(self):
        EventCountingControl.objects.create(
            event=self.scrim, count_winner=False, count_placement=False, count_kills=False,
        )
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertEqual(agg.result.scrim_pts, 0)
        # The tournament half is untouched - the toggle is per event, not global.
        self.assertGreater(agg.result.tournament_pts, 0)

    def test_result_exclusion_removes_a_teams_scrim_results(self):
        ResultExclusion.objects.create(
            event=self.scrim, entity_type="team", team=self.team, reason="DQ",
        )
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertEqual(agg.result.scrim_pts, 0)

    def test_result_exclusion_removes_a_players_scrim_results(self):
        ResultExclusion.objects.create(
            event=self.scrim, entity_type="player", player=self.user, reason="DQ",
        )
        agg = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertEqual(agg.result.scrim_kill_pts, 0)
        self.assertEqual(agg.result.scrim_win_pts, 0)

    # ── control: a toggle on the TOURNAMENT must not silently kill the scrim side ──
    def test_disabling_the_tournament_leaves_the_scrim_toggles_alone(self):
        EventCountingControl.objects.create(event=self.tournament, count_kills=False)
        agg = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertGreater(agg.result.scrim_kill_pts, 0)


class ScrimOnlyEntityTests(TestCase):
    """A team whose ONLY results are scrims scores nothing and does not appear.

    This is spec §5.1 Step 3 (scrim points are capped at 30% of the tournament total, so zero
    tournament points caps scrims to zero) plus the §5.2 participation floor in
    ``recalc.recalc_team_monthly``. It is the reason the owner sees scrims "not counting". The test
    documents the CURRENT contract so a future spec change is a deliberate, visible edit.
    """

    def setUp(self):
        self.user = User.objects.create(username="scrim_only", email="so@example.com")
        self.team = Team.objects.create(
            team_name="Scrim Only FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        event = Event.objects.create(
            event_name="Scrims Only", competition_type="scrims", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=PLAY_DAY, end_date=PLAY_DAY,
            registration_open_date=PLAY_DAY - datetime.timedelta(days=5),
            registration_end_date=PLAY_DAY - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
            number_of_stages=1, creator=self.user, is_draft=False,
        )
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
        TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=20,
        )

    def test_scrim_only_team_now_scores(self):
        """Renamed from test_scrim_only_team_scores_nothing, owner 2026-08-03.

        The old name described the bug: a team that played nothing but scrims earned zero,
        because the cap was a percentage of its zero tournament points. Scrims are meant to
        count toward rankings, so such a team now scores up to the flat allowance.
        """
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertEqual(agg.tournaments_played, 0)
        self.assertGreater(agg.result.scrim_pts, 0)
        self.assertEqual(agg.result.total, agg.result.scrim_pts)
