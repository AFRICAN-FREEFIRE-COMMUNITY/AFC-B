"""
test_recalc_debounce.py
───────────────────────
Covers the two halves of §18 real-time recalculation (owner backlog item 14, "rankings for players
and teams must update automatically in real time"):

  * ``WithLockDebounceTests``  - ``tasks._with_lock``, the debounce that collapses a burst of
                                 result edits into one recalculation without losing any of them;
  * ``AutomaticRecalcTests``   - the whole chain actually FIRING: saving a match result writes the
                                 score row by itself, with nobody pressing recalculate;
  * ``NightlySweepTests``      - ``tasks.sweep_rankings``, the backstop that repairs the ladders
                                 when nothing has been draining the rankings_recalc queue.

THE DEBOUNCE, AND WHAT WAS WRONG WITH IT. The lock used to RETURN when it was already held, which
silently threw the second recalculation away. That is a lost update, not a debounce: the run in
progress had already read the database before the later edit committed, so the entity kept a score
derived from stale data until something unrelated happened to touch it again. On a stats upload -
many rows, one after another - that is the common case, not an edge case.

WHAT WAS WRONG. The lock used to RETURN when it was already held, which silently threw the second
recalculation away. That is a lost update, not a debounce: the run in progress had already read the
database before the later edit committed, so the entity kept a score derived from stale data until
something unrelated happened to touch it again. On a stats upload - many rows, one after another -
that is the common case, not an edge case.

WHAT IT DOES NOW. A collapsed call leaves a ``recalc_dirty`` marker, and the holder re-runs once
after finishing if the marker is set. Any number of collapsed calls set the same marker, so the
burst still collapses to (at most) one extra pass rather than one pass per edit.

The tests drive ``_with_lock`` directly with a counting function, and force contention by having
that function re-enter ``_with_lock`` on the same key - which is exactly the state a second worker
would find. A local-memory cache is used so the assertions do not depend on a running Redis.
"""
import datetime

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats, TournamentPlayerMatchStats,
)
from afc_rankings import tasks
from afc_rankings.models import PlayerMonthlyScore, Season, TeamMonthlyScore, TeamQuarterlyScore

User = get_user_model()

PLAY_DAY = datetime.date(2099, 5, 10)
MONTH = datetime.date(2099, 5, 1)

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "recalc-debounce-tests",
    }
}


@override_settings(CACHES=LOCMEM)
class WithLockDebounceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.runs = []

    def test_a_single_call_runs_once(self):
        tasks._with_lock("k", lambda: self.runs.append("run"))
        self.assertEqual(len(self.runs), 1)

    def test_an_edit_arriving_mid_run_is_not_lost(self):
        """The regression this fixes: work that arrives while the lock is held must still happen."""
        def fn():
            self.runs.append("run")
            if len(self.runs) == 1:
                # Stands in for a second worker picking up an edit that landed mid-run: it finds
                # the lock held, so it marks the state dirty and returns instead of running.
                tasks._with_lock("k", lambda: self.runs.append("SHOULD-NOT-RUN-INLINE"))

        tasks._with_lock("k", fn)
        # Two passes of the real work, and the contending call never ran its own function inline.
        self.assertEqual(self.runs, ["run", "run"])

    def test_a_burst_collapses_to_one_extra_pass(self):
        """Five collapsed calls set the SAME marker, so they cost one re-run between them."""
        def fn():
            self.runs.append("run")
            if len(self.runs) == 1:
                for _ in range(5):
                    tasks._with_lock("k", lambda: self.runs.append("SHOULD-NOT-RUN-INLINE"))

        tasks._with_lock("k", fn)
        self.assertEqual(self.runs, ["run", "run"])

    def test_the_trailing_runs_are_capped(self):
        """A never-ending stream of edits must not pin a worker on one entity forever."""
        def fn():
            self.runs.append("run")
            tasks._with_lock("k", lambda: None)   # always dirty again

        tasks._with_lock("k", fn)
        self.assertEqual(len(self.runs), 1 + tasks._MAX_TRAILING_RUNS)

    def test_different_keys_do_not_block_each_other(self):
        """The lock is per (entity, period); two teams recalculating at once is normal."""
        tasks._with_lock("team-a", lambda: self.runs.append("a"))
        tasks._with_lock("team-b", lambda: self.runs.append("b"))
        self.assertEqual(self.runs, ["a", "b"])

    def test_the_lock_is_released_when_the_work_raises(self):
        """A failed recalculation must not leave the key locked for the whole TTL."""
        with self.assertRaises(ValueError):
            tasks._with_lock("k", self._boom)
        tasks._with_lock("k", lambda: self.runs.append("run"))
        self.assertEqual(self.runs, ["run"])

    @staticmethod
    def _boom():
        raise ValueError("recalc blew up")


@override_settings(CACHES=LOCMEM, RANKINGS_RECALC_SYNC=True)
class AutomaticRecalcTests(TestCase):
    """The chain FIRES: a result lands, the score row appears, nobody pressed recalculate.

    signals.on_team_stats_save -> transaction.on_commit -> tasks.enqueue_team ->
    tasks.recalculate_team_monthly / _quarterly -> recalc.recalc_team_* -> the score row.

    ``captureOnCommitCallbacks(execute=True)`` is what makes this testable: a TestCase wraps each
    test in a transaction that never commits, so the on_commit callbacks the signals register would
    otherwise never run. ``RANKINGS_RECALC_SYNC=True`` is the dev/inline dispatch path (settings
    defaults it to DEBUG), so the tasks execute in-process instead of needing a Celery worker -
    which is the point of the test: the wiring, not the transport.
    """

    def setUp(self):
        self.user = User.objects.create(username="realtime", email="rt@example.com")
        self.team = Team.objects.create(
            team_name="Realtime FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        self.season = Season.objects.create(
            name="Realtime Season 2099 Q2", quarter=2, year=2099,
            start_date=datetime.date(2099, 4, 1), end_date=datetime.date(2099, 6, 30),
            transfer_window_open=datetime.date(2099, 4, 1),
            transfer_window_close=datetime.date(2099, 4, 14),
            is_active=True,
        )
        self.event = Event.objects.create(
            event_name="Live Scrims", competition_type="scrims", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=PLAY_DAY, end_date=PLAY_DAY,
            registration_open_date=PLAY_DAY - datetime.timedelta(days=5),
            registration_end_date=PLAY_DAY - datetime.timedelta(days=1),
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
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
        cache.clear()

    def test_saving_a_scrim_result_writes_the_score_rows_by_itself(self):
        self.assertFalse(TeamMonthlyScore.objects.filter(team=self.team).exists())

        with self.captureOnCommitCallbacks(execute=True):
            stats = TournamentTeamMatchStats.objects.create(
                match=self.match, tournament_team=self.tt, placement=1, kills=20,
            )
            TournamentPlayerMatchStats.objects.create(
                team_stats=stats, player=self.user, kills=20, played=True,
            )

        month_row = TeamMonthlyScore.objects.filter(team=self.team, month=MONTH).first()
        self.assertIsNotNone(month_row)
        self.assertGreater(month_row.scrim_pts, 0)
        # And the SEASON row too - which is the one that carries the tier, and the one a
        # scrim-only team used to be deleted from.
        season_row = TeamQuarterlyScore.objects.filter(team=self.team, season=self.season).first()
        self.assertIsNotNone(season_row)
        self.assertGreater(season_row.scrim_pts, 0)

    def test_the_player_row_is_written_too(self):
        with self.captureOnCommitCallbacks(execute=True):
            stats = TournamentTeamMatchStats.objects.create(
                match=self.match, tournament_team=self.tt, placement=1, kills=20,
            )
            TournamentPlayerMatchStats.objects.create(
                team_stats=stats, player=self.user, kills=20, played=True,
            )

        row = PlayerMonthlyScore.objects.filter(player=self.user, month=MONTH).first()
        self.assertIsNotNone(row)
        self.assertGreater(row.scrim_kill_pts + row.scrim_win_pts, 0)


@override_settings(CACHES=LOCMEM, RANKINGS_RECALC_SYNC=True)
class NightlySweepTests(TestCase):
    """``tasks.sweep_rankings`` repairs the ladders without any per-entity task being enqueued.

    This is the worker-down case: it must rebuild the current month and the active season by
    calling recalc directly, so a night with nothing draining rankings_recalc still ends correct.
    The test proves it by deleting the score rows behind the sweep's back (as a stalled queue
    effectively does - the rows just never get written) and running only the sweep.
    """

    def setUp(self):
        self.user = User.objects.create(username="sweeper", email="sw@example.com")
        self.team = Team.objects.create(
            team_name="Sweeper FC", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        today = datetime.date.today()
        month_start = today.replace(day=1)
        # The sweep works on the CURRENT month + ACTIVE season by design, so the fixture has to
        # sit in real time rather than in the year 2099 the other suites use.
        self.season = Season.objects.create(
            name="Sweep Season", quarter=((today.month - 1) // 3) + 1, year=today.year,
            start_date=month_start, end_date=month_start + datetime.timedelta(days=80),
            transfer_window_open=month_start,
            transfer_window_close=month_start + datetime.timedelta(days=14),
            is_active=True,
        )
        event = Event.objects.create(
            event_name="Sweep Scrims", competition_type="scrims", participant_type="squad",
            event_type="internal", max_teams_or_players=12, event_mode="virtual",
            start_date=today, end_date=today,
            registration_open_date=month_start, registration_end_date=today,
            prizepool="0", event_rules="none", event_status="completed",
            registration_link="https://example.com/r", tournament_tier="tier_3",
            number_of_stages=1, creator=self.user, is_draft=False,
        )
        stage = Stages.objects.create(
            event=event, stage_name="Main", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=today,
            playing_time=datetime.time(19, 0), teams_qualifying=1, match_count=1,
            match_maps=["bermuda"],
        )
        match = Match.objects.create(
            group=group, match_map="bermuda", match_number=1, played_on=today,
        )
        tt = TournamentTeam.objects.create(
            event=event, team=self.team, registered_by=self.user, status="active",
        )
        stats = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=1, kills=20,
        )
        TournamentPlayerMatchStats.objects.create(
            team_stats=stats, player=self.user, kills=20, played=True,
        )
        # Whatever the signals wrote is irrelevant here - wipe it so only the sweep can put it back.
        TeamMonthlyScore.objects.all().delete()
        TeamQuarterlyScore.objects.all().delete()
        PlayerMonthlyScore.objects.all().delete()
        cache.clear()
        self.month = month_start

    def test_the_sweep_rebuilds_the_month_and_the_season(self):
        tasks.sweep_rankings()
        self.assertTrue(
            TeamMonthlyScore.objects.filter(team=self.team, month=self.month).exists())
        self.assertTrue(
            TeamQuarterlyScore.objects.filter(team=self.team, season=self.season).exists())
        self.assertTrue(
            PlayerMonthlyScore.objects.filter(player=self.user, month=self.month).exists())

    def test_the_sweep_is_idempotent(self):
        tasks.sweep_rankings()
        first = TeamMonthlyScore.objects.get(team=self.team, month=self.month).total_score
        tasks.sweep_rankings()
        second = TeamMonthlyScore.objects.get(team=self.team, month=self.month).total_score
        self.assertEqual(first, second)
