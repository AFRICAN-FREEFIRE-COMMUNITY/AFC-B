"""
test_event_counts_toward_rankings.py
────────────────────────────────────
Covers the per-event MASTER switch, ``EventCountingControl.counts_toward_rankings`` (owner
2026-08-03, backlog item 14: "admin toggle so all events count by default, with admins able to
switch individual ones off").

WHAT WAS MISSING: the Result-Markers surface only had the three COMPONENT toggles
(count_winner / count_placement / count_kills). Turning all three off still left an event feeding
the rankings through the finals bonus, the MVP points, the participation points and - at the
quarterly level - the PRIZE MONEY, which is summed straight off EventPrizePayout and never went
near the counting controls. So there was no way to say "this competition does not count at all".

WHAT THIS FILE PINS DOWN, in the order the classes appear:
  1. DefaultsUnchangedTests   - the field changes nothing for anything that exists today.
  2. MasterSwitchTests        - switching it off removes the event from team AND player scores,
                                tournaments and scrims alike.
  3. PrizeMoneyTests          - and removes its prize money, for both a switched-off event and a
                                per-entity ResultExclusion (the "everywhere, not just the obvious
                                place" half of the ask).

Fixture shape mirrors test_scrim_counting.py so the two read the same way:
Event(competition_type=...) -> Stages -> StageGroups -> Match -> TournamentTeam ->
TournamentTeamMatchStats -> TournamentPlayerMatchStats.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, EventPrizePayout, Match, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats, TournamentPlayerMatchStats,
)
from afc_rankings import aggregation
from afc_rankings.models import EventCountingControl, ResultExclusion, Season

User = get_user_model()

MONTH = datetime.date(2099, 5, 1)
PLAY_DAY = datetime.date(2099, 5, 10)


class _EventFixtureMixin:
    """Builds one team + one player and lets a test attach events with results to them."""

    def _make_entities(self):
        self.user = User.objects.create(username="master_sw_player", email="msw@example.com")
        self.team = Team.objects.create(
            team_name="Master Switch FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )

    def _event(self, name, competition_type="tournament"):
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

    def _result(self, event, *, placement, kills, finals=False):
        """One played match in ``event`` where self.team finishes ``placement`` with ``kills``.

        ``finals`` marks the stage as the finals stage, which is how a player earns finals points -
        the component toggles cannot switch those off, so it is what proves the master switch does.
        """
        stage = Stages.objects.create(
            event=event, stage_name="Main", start_date=PLAY_DAY, end_date=PLAY_DAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
            is_finals_stage=finals,
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
        return tt


class DefaultsUnchangedTests(_EventFixtureMixin, TestCase):
    """The new field must not move a single existing number.

    This is the regression guard the lead asked for: every event on the platform today either has
    NO control row at all, or has one created for a component toggle. Both must score exactly as
    they did before the field existed.
    """

    def setUp(self):
        self._make_entities()
        self.tournament = self._event("Ranked Cup")
        self._result(self.tournament, placement=1, kills=10)

    def test_event_with_no_control_row_still_counts(self):
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertGreater(agg.result.total, 0)
        self.assertEqual(agg.tournaments_played, 1)

    def test_control_row_created_for_a_component_toggle_defaults_to_counting(self):
        """A row written the old way (component toggle only) gets counts_toward_rankings True."""
        control = EventCountingControl.objects.create(event=self.tournament, count_kills=False)
        self.assertTrue(control.counts_toward_rankings)
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        # Kills are off, so kill points are gone, but the event still counts: placement and the
        # winner bonus survive, exactly as they did before the master switch existed.
        self.assertGreater(agg.result.total, 0)
        self.assertEqual(agg.tournaments_played, 1)


class MasterSwitchTests(_EventFixtureMixin, TestCase):
    """Switching an event off removes it from every score it fed."""

    def setUp(self):
        self._make_entities()
        # A finals-stage tournament: its finals + MVP + participation points are the ones NO
        # component toggle can reach, so they are what the master switch has to prove it removes.
        self.tournament = self._event("Ranked Cup")
        self._result(self.tournament, placement=1, kills=10, finals=True)
        self.scrim = self._event("Tuesday Scrims", "scrims")
        self._result(self.scrim, placement=1, kills=8)

    def _switch_off(self, event):
        EventCountingControl.objects.create(event=event, counts_toward_rankings=False)

    def test_switched_off_tournament_leaves_the_team_with_nothing(self):
        self._switch_off(self.tournament)
        self._switch_off(self.scrim)
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertEqual(agg.tournaments_played, 0)
        self.assertEqual(agg.result.total, 0)

    def test_switched_off_tournament_leaves_the_player_with_nothing(self):
        self._switch_off(self.tournament)
        self._switch_off(self.scrim)
        agg = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertEqual(agg.tournaments_played, 0)
        self.assertEqual(agg.result.total, 0)

    def test_the_component_toggles_could_not_do_this(self):
        """All three components off still scores; the master switch is what zeroes the event.

        This is the test that justifies the new field existing at all: it shows the old surface
        could not express "this event does not count", because the finals and participation
        points have no component flag.
        """
        EventCountingControl.objects.create(
            event=self.tournament,
            count_winner=False, count_placement=False, count_kills=False,
        )
        with_components_off = aggregation.compute_player_monthly(self.user, MONTH).result.total
        self.assertGreater(with_components_off, 0)

        EventCountingControl.objects.filter(event=self.tournament).update(
            counts_toward_rankings=False)
        with_master_off = aggregation.compute_player_monthly(self.user, MONTH).result.total
        self.assertLess(with_master_off, with_components_off)

    def test_switching_a_scrim_off_drops_only_the_scrim(self):
        """Per-event, not global: the tournament beside it is untouched."""
        self._switch_off(self.scrim)
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertEqual(agg.result.scrim_pts, 0)
        self.assertGreater(agg.result.tournament_pts, 0)
        self.assertEqual(agg.tournaments_played, 1)

    def test_switching_the_tournament_off_leaves_the_scrim_counting(self):
        self._switch_off(self.tournament)
        agg = aggregation.compute_team_monthly(self.team, MONTH)
        self.assertEqual(agg.result.tournament_pts, 0)
        self.assertEqual(agg.tournaments_played, 0)
        self.assertGreater(agg.result.scrim_pts, 0)

    def test_switched_off_event_contributes_no_role_history(self):
        """The role breakdown is derived beside the score, so it has to agree with it."""
        stats = TournamentPlayerMatchStats.objects.filter(player=self.user)
        stats.update(role_at_match="rusher")
        before = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertIn("rusher", before.role_breakdown)

        self._switch_off(self.tournament)
        self._switch_off(self.scrim)
        after = aggregation.compute_player_monthly(self.user, MONTH)
        self.assertEqual(after.role_breakdown, {})


class PrizeMoneyTests(_EventFixtureMixin, TestCase):
    """Prize money is summed off EventPrizePayout, not off matches, so it needed its own filter.

    Without it an admin could switch an event off (or disqualify a team from it) and still watch
    its prize money scoring in the quarterly total, which would make both switches a half-truth.
    """

    def setUp(self):
        self._make_entities()
        self.season = Season.objects.create(
            name="Prize Season 2099 Q2", quarter=2, year=2099,
            start_date=datetime.date(2099, 4, 1), end_date=datetime.date(2099, 6, 30),
            transfer_window_open=datetime.date(2099, 4, 1),
            transfer_window_close=datetime.date(2099, 4, 14),
            is_active=True,
        )
        self.tournament = self._event("Prize Cup")
        self.tt = self._result(self.tournament, placement=1, kills=10)
        # The score WITHOUT any payout. The §7.2 bracket table has no zero floor - its lowest band
        # is "up to 100,000 naira", so a team with no prize at all still scores that band. Comparing
        # against this baseline rather than against 0 is what makes "the prize stopped counting" a
        # real assertion instead of an accident of the bracket table.
        self.no_prize_pts = aggregation.compute_team_quarterly(
            self.team, self.season).result.prize_money_pts

    def _pay(self, amount=500000):
        """Record a payout to this team INSIDE the season window.

        ``EventPrizePayout.created_at`` is auto_now_add, and the aggregation window filters on it,
        so a freshly created row lands on today's date and falls outside a 2099 season entirely.
        The follow-up update puts it in the window; without it the whole class would be asserting
        against a payout the query never sees.
        """
        payout = EventPrizePayout.objects.create(
            event=self.tournament, tournament_team=self.tt, amount=amount,
        )
        EventPrizePayout.objects.filter(pk=payout.pk).update(
            created_at=datetime.datetime(2099, 5, 1, 12, 0, tzinfo=datetime.timezone.utc))
        return payout

    def _prize_pts(self):
        return aggregation.compute_team_quarterly(self.team, self.season).result.prize_money_pts

    def test_prize_money_counts_by_default(self):
        self._pay()
        self.assertGreater(self._prize_pts(), self.no_prize_pts)

    def test_switched_off_event_contributes_no_prize_money(self):
        self._pay()
        EventCountingControl.objects.create(
            event=self.tournament, counts_toward_rankings=False)
        self.assertEqual(self._prize_pts(), self.no_prize_pts)

    def test_excluded_team_contributes_no_prize_money(self):
        """A ResultExclusion says this team's results in this event don't count. Its prize is one."""
        self._pay()
        ResultExclusion.objects.create(
            event=self.tournament, entity_type="team", team=self.team, reason="Disqualified",
        )
        self.assertEqual(self._prize_pts(), self.no_prize_pts)

    def test_component_toggles_do_not_touch_prize_money(self):
        """Deliberate: the component flags trim placement / kills / win, never the prize."""
        self._pay()
        EventCountingControl.objects.create(
            event=self.tournament,
            count_winner=False, count_placement=False, count_kills=False,
        )
        self.assertGreater(self._prize_pts(), self.no_prize_pts)


class PrizeExclusionIsPerEntityTests(_EventFixtureMixin, TestCase):
    """One team's disqualification must not delete a DIFFERENT team's prize from the same event.

    WHY THIS IS ITS OWN CLASS. ``_non_counting_prize_q`` is used with ``.exclude()``, and
    ``ResultExclusion`` is a MULTI-VALUED relation from EventPrizePayout's point of view (one event
    can carry an exclusion for every team in it). Django compiles an ``exclude()`` across a
    multi-valued relation into a subquery, and the failure mode if that subquery is written a shade
    too loosely is silent and severe: disqualifying one team would quietly strip the prize-money
    points off every other team in that event, and nothing in the product would say so. The class
    above only ever looks at the excluded team, so it cannot catch that. This one watches the
    bystander.
    """

    def setUp(self):
        self._make_entities()
        self.season = Season.objects.create(
            name="Bystander Season 2099 Q2", quarter=2, year=2099,
            start_date=datetime.date(2099, 4, 1), end_date=datetime.date(2099, 6, 30),
            transfer_window_open=datetime.date(2099, 4, 1),
            transfer_window_close=datetime.date(2099, 4, 14),
            is_active=True,
        )
        self.tournament = self._event("Shared Prize Cup")
        self.tt = self._result(self.tournament, placement=1, kills=10)
        self._pay(self.tt)

        # The bystander: a second team in the SAME event, with its own payout and no exclusion.
        self.other = Team.objects.create(
            team_name="Bystander FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        self.other_tt = TournamentTeam.objects.create(
            event=self.tournament, team=self.other, registered_by=self.user, status="active",
        )
        self._pay(self.other_tt)
        self.other_prize_pts = self._prize_pts(self.other)

    def _pay(self, tournament_team, amount=500000):
        """Payout to ``tournament_team``, dated into the 2099 season window (see PrizeMoneyTests)."""
        payout = EventPrizePayout.objects.create(
            event=self.tournament, tournament_team=tournament_team, amount=amount,
        )
        EventPrizePayout.objects.filter(pk=payout.pk).update(
            created_at=datetime.datetime(2099, 5, 1, 12, 0, tzinfo=datetime.timezone.utc))
        return payout

    def _prize_pts(self, team):
        return aggregation.compute_team_quarterly(team, self.season).result.prize_money_pts

    def test_excluding_one_team_leaves_the_other_team_prize_alone(self):
        ResultExclusion.objects.create(
            event=self.tournament, entity_type="team", team=self.team, reason="Disqualified",
        )
        self.assertEqual(self._prize_pts(self.other), self.other_prize_pts)

    def test_the_excluded_team_still_loses_its_own_prize(self):
        """The other half of the same assertion: precision cuts both ways."""
        with_prize = self._prize_pts(self.team)
        ResultExclusion.objects.create(
            event=self.tournament, entity_type="team", team=self.team, reason="Disqualified",
        )
        self.assertLess(self._prize_pts(self.team), with_prize)

    def test_switching_the_event_off_removes_prize_money_for_everyone(self):
        """The master switch is per EVENT, so unlike an exclusion it is meant to hit both teams."""
        EventCountingControl.objects.create(
            event=self.tournament, counts_toward_rankings=False)
        self.assertLess(self._prize_pts(self.other), self.other_prize_pts)
