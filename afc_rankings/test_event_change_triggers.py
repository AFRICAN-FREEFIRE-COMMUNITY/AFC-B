"""
test_event_change_triggers.py
─────────────────────────────
Covers the two "an admin changed something about the event, so the ladders must move" paths that
were not firing correctly (owner backlog items 12 + 14):

  1. ``EventPeriodsTests``      - ``recalc.event_periods``: the months a lever must repair are the
                                  ones the event was PLAYED in, not the month the admin is in.
  2. ``ToggleRepairsPlayedMonthTests``
                                - switching an event off with the Result Markers master switch
                                  actually clears the played month's ladder row.
  3. ``TierChangeTriggersRecalcTests``
                                - changing ``Event.tournament_tier`` recomputes by itself.

WHY 1 AND 2 ARE THE SAME BUG. Every enqueue helper on the Result Markers surface passed
``recalc.current_month()``. An event played in May, switched off in August, therefore recomputed
each team's AUGUST monthly row - which the toggle does not affect - and left the MAY ladder, the
one the results are actually on, serving the old numbers indefinitely. The season row happened to
be right (the season window contains May), which is what made it look like the toggle worked.

WHY 3 EXISTS. The tier is the largest multiplier on a result (tier_1 = 2.0x + a 30-point win bonus
against tier_3's 1.0x and 12), and it is the field an admin corrects when an event is mis-tiered -
which is the whole of backlog item 12. Nothing recalculated when it changed, through any of its
three doors (a head admin's manual pick, the automatic classifier on create/edit, and the
``reclassify_event_tiers`` bulk pass). So the correction landed and the ladders did not move.

Fixture shape mirrors test_scrim_counting.py / test_event_counts_toward_rankings.py.
``RANKINGS_RECALC_SYNC=True`` plus ``captureOnCommitCallbacks(execute=True)`` run the enqueued
tasks in-process, so these test the WIRING rather than the Celery transport (see
test_recalc_debounce.AutomaticRecalcTests for the same arrangement and why).
"""
import datetime

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats, TournamentPlayerMatchStats,
)
from afc_rankings import recalc
from afc_rankings.admin_results import _enqueue_event_team_recalc, _enqueue_event_player_recalc
from afc_rankings.models import (
    EventCountingControl, PlayerMonthlyScore, Season, TeamMonthlyScore,
)

User = get_user_model()

# Deliberately NOT the current month: the whole point is that the lever must reach a ladder the
# admin is not standing on. (2099 is what the sibling suites use, so the fixtures read alike.)
PLAY_DAY = datetime.date(2099, 5, 10)
MONTH = datetime.date(2099, 5, 1)

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "event-change-trigger-tests",
    }
}


class _EventFixtureMixin:
    """One team + one player with a played result in ``self.event``, inside ``self.season``."""

    def _build(self, *, username, team_name, season_name, tier="tier_3"):
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
        self.event = Event.objects.create(
            event_name="Period Cup", competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=PLAY_DAY, end_date=PLAY_DAY,
            registration_open_date=PLAY_DAY - datetime.timedelta(days=5),
            registration_end_date=PLAY_DAY - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier=tier,
            number_of_stages=1, creator=self.user, is_draft=False,
        )
        stage = Stages.objects.create(
            event=self.event, stage_name="Main", start_date=PLAY_DAY, end_date=PLAY_DAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=PLAY_DAY,
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        self.match = Match.objects.create(
            group=group, match_map="bermuda", match_number=1, played_on=PLAY_DAY,
        )
        self.tt = TournamentTeam.objects.create(
            event=self.event, team=self.team, registered_by=self.user, status="active",
        )

    def _play(self):
        """Record the result, letting the §18 signals write the score rows as they would live."""
        with self.captureOnCommitCallbacks(execute=True):
            stats = TournamentTeamMatchStats.objects.create(
                match=self.match, tournament_team=self.tt, placement=1, kills=20,
            )
            TournamentPlayerMatchStats.objects.create(
                team_stats=stats, player=self.user, kills=20, played=True,
            )


@override_settings(CACHES=LOCMEM, RANKINGS_RECALC_SYNC=True)
class EventPeriodsTests(_EventFixtureMixin, TestCase):
    """``recalc.event_periods`` reads the event's own matches."""

    def setUp(self):
        self._build(username="periods", team_name="Periods FC", season_name="Periods 2099 Q2")
        cache.clear()

    def test_it_returns_the_month_the_event_was_played_in(self):
        months, season_id = recalc.event_periods(self.event.event_id)
        self.assertEqual(months, [MONTH])
        self.assertEqual(season_id, self.season.season_id)
        # The regression, stated plainly: this is NOT the month the admin is clicking in.
        self.assertNotEqual(months, [recalc.current_month()])

    def test_an_event_spanning_two_months_returns_both(self):
        """Neither ladder may be missed, so both month-starts come back, sorted."""
        stage = Stages.objects.create(
            event=self.event, stage_name="Finals", start_date=datetime.date(2099, 6, 2),
            end_date=datetime.date(2099, 6, 2), number_of_groups=1,
            stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=datetime.date(2099, 6, 2),
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        Match.objects.create(group=group, match_map="bermuda", match_number=1,
                             played_on=datetime.date(2099, 6, 2))
        months, _ = recalc.event_periods(self.event.event_id)
        self.assertEqual(months, [MONTH, datetime.date(2099, 6, 1)])

    def test_an_unplayed_event_falls_back_to_the_current_period(self):
        """No matches means no past ladder to repair, so the caller still gets a usable period."""
        Match.objects.filter(group__stage__event=self.event).delete()
        months, season_id = recalc.event_periods(self.event.event_id)
        self.assertEqual(months, [recalc.current_month()])
        self.assertEqual(season_id, self.season.season_id)


@override_settings(CACHES=LOCMEM, RANKINGS_RECALC_SYNC=True)
class ToggleRepairsPlayedMonthTests(_EventFixtureMixin, TestCase):
    """Switching an event off clears the ladder for the month it was played in."""

    def setUp(self):
        self._build(username="toggle", team_name="Toggle FC", season_name="Toggle 2099 Q2")
        self._play()
        cache.clear()

    def test_the_team_row_for_the_played_month_is_cleared(self):
        self.assertTrue(TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).exists())

        EventCountingControl.objects.create(event=self.event, counts_toward_rankings=False)
        with self.captureOnCommitCallbacks(execute=True):
            _enqueue_event_team_recalc(self.event.event_id)

        # Before the fix this asserted nothing: the recalc ran against the CURRENT month, so the
        # May row survived untouched and the ladder kept showing a switched-off event.
        self.assertFalse(TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).exists())

    def test_the_player_row_for_the_played_month_is_cleared(self):
        self.assertTrue(PlayerMonthlyScore.objects.filter(player=self.user, month=MONTH).exists())

        EventCountingControl.objects.create(event=self.event, counts_toward_rankings=False)
        with self.captureOnCommitCallbacks(execute=True):
            _enqueue_event_player_recalc(self.event.event_id)

        self.assertFalse(PlayerMonthlyScore.objects.filter(player=self.user, month=MONTH).exists())

    def test_switching_it_back_on_restores_the_row(self):
        """The lever is reversible, which is what makes it safe for an admin to use."""
        control = EventCountingControl.objects.create(
            event=self.event, counts_toward_rankings=False)
        with self.captureOnCommitCallbacks(execute=True):
            _enqueue_event_team_recalc(self.event.event_id)
        self.assertFalse(TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).exists())

        control.counts_toward_rankings = True
        control.save(update_fields=["counts_toward_rankings"])
        with self.captureOnCommitCallbacks(execute=True):
            _enqueue_event_team_recalc(self.event.event_id)
        self.assertTrue(TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).exists())


@override_settings(CACHES=LOCMEM, RANKINGS_RECALC_SYNC=True)
class TierChangeTriggersRecalcTests(_EventFixtureMixin, TestCase):
    """Re-tiering an event recomputes its results, with nobody pressing recalculate."""

    def setUp(self):
        self._build(username="tiermove", team_name="Tier Move FC",
                    season_name="Tier Move 2099 Q2", tier="tier_3")
        self._play()
        cache.clear()

    def test_promoting_the_event_raises_the_scores_by_itself(self):
        before = TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score

        with self.captureOnCommitCallbacks(execute=True):
            self.event.tournament_tier = "tier_1"
            self.event.save(update_fields=["tournament_tier"])

        after = TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score
        # tier_1 doubles placement and kill points against tier_3, so the direction is unambiguous.
        self.assertGreater(after, before)

    def test_the_player_score_is_tier_independent_by_design(self):
        """Written down because it is surprising, and it is why the receiver is teams-only.

        A player's score has no tier factor anywhere in it: scoring/engine._player_components
        computes compressed personal kills and placement plus the flat MVP / finals / team-win /
        participation weights, and never reads PlayerTournamentInput.tier. Players reach a tier by
        INHERITING their team's at quarterly evaluation (spec §8.1), not by scoring more per point.
        So re-tiering an event moves the team ladder and leaves the player ladder exactly where it
        was - and enqueueing every player in the event would be a guaranteed no-op pass.
        """
        player_before = PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).total_score
        team_before = TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score

        with self.captureOnCommitCallbacks(execute=True):
            self.event.tournament_tier = "tier_1"
            self.event.save(update_fields=["tournament_tier"])

        self.assertEqual(
            PlayerMonthlyScore.objects.get(player=self.user, month=MONTH).total_score,
            player_before)
        # ... while the TEAM score on the same event did move. Asserting both sides in one test is
        # what makes this a statement about the scoring rules rather than a recalculation that
        # quietly failed to run.
        self.assertGreater(
            TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score, team_before)

    def test_demoting_the_event_lowers_the_scores(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.event.tournament_tier = "tier_1"
            self.event.save(update_fields=["tournament_tier"])
        promoted = TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score

        with self.captureOnCommitCallbacks(execute=True):
            self.event.tournament_tier = "tier_3"
            self.event.save(update_fields=["tournament_tier"])

        self.assertLess(
            TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score, promoted)

    def test_saving_the_event_without_moving_the_tier_recalculates_nothing(self):
        """The precision that keeps this from being a stampede.

        Event.save() runs constantly - the status convergence sweep completes and reopens events
        every few minutes - so the receiver has to fire on a real tier change and nothing else.
        A sentinel written straight into the score row proves no recalculation overwrote it.
        """
        TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).update(total_score=999)

        with self.captureOnCommitCallbacks(execute=True):
            self.event.event_name = "Period Cup (renamed)"
            self.event.save(update_fields=["event_name"])

        self.assertEqual(
            TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score, 999)

    def test_creating_an_event_recalculates_nothing(self):
        """A new event has no results to reweight, so it must not trigger a pass."""
        TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).update(total_score=999)

        with self.captureOnCommitCallbacks(execute=True):
            Event.objects.create(
                event_name="Brand New Cup", competition_type="tournament",
                participant_type="squad", event_type="internal", max_teams_or_players=12,
                event_mode="virtual", start_date=PLAY_DAY, end_date=PLAY_DAY,
                registration_open_date=PLAY_DAY - datetime.timedelta(days=5),
                registration_end_date=PLAY_DAY - datetime.timedelta(days=1),
                prizepool="0", event_rules="none", event_status="upcoming",
                registration_link="https://example.com/r", tournament_tier="tier_1",
                number_of_stages=1, creator=self.user, is_draft=False,
            )

        self.assertEqual(
            TeamMonthlyScore.objects.get(team=self.team, month=MONTH).total_score, 999)
