"""
test_result_write_queues_players.py
───────────────────────────────────
Open list item 5 (18 Nov 2026): one AFC Capture upload took the server about 20 s because
views._recompute_team_kills_for_event re-saved EVERY team row of the event, and each save made
afc_rankings.signals.on_team_stats_save queue a ranking recalc for that team and each of its players.
On a local copy of event 373 that was 1,112 queued recalcs and 5,556 SQL queries for one map.

The recompute now saves only the rows whose kills / kill_points / total_points changed. That exposed
a dependency: the one result writer (result_writes.write_team_result_row) creates the team row first
(its post_save sees no players) and then bulk_creates the players (no post_save at all), so the
blanket re-save had been the ONLY thing queuing the new map's players. The writer now queues them via
afc_rankings.signals.queue_player_recalcs.

  1. WriterQueuesPlayersTests      - a written team row queues each of its players exactly once.
  2. RecomputeSkipsUnchangedTests  - re-running the recompute over unchanged rows saves nothing and
                                     queues nothing, and a changed row is still saved and queued.

Fixture: test_event_change_triggers._EventFixtureMixin (one event, stage, group, match, team).
The enqueue helpers are patched, so these test the WIRING, not the recalculation itself.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from afc_rankings.test_event_change_triggers import LOCMEM, _EventFixtureMixin
from afc_tournament_and_scrims.models import TournamentTeamMatchStats
from afc_tournament_and_scrims.result_writes import scoring_context, write_team_result_row
from afc_tournament_and_scrims.views import _recompute_team_kills_for_event


def _row(user_ids, kills=4, placement=1):
    return {"placement": placement, "played": True, "bonus_points": 0, "penalty_points": 0,
            "players": [{"user_id": uid, "kills": kills, "played": True} for uid in user_ids]}


@override_settings(CACHES=LOCMEM, RANKINGS_RECALC_SYNC=False)
class WriterQueuesPlayersTests(_EventFixtureMixin, TestCase):
    def setUp(self):
        self._build(username="writerq", team_name="Writer FC", season_name="Writer 2099 Q2")
        self.match.scoring_settings = {"placement_points": {"1": 12}, "kill_point": 1}
        self.match.save(update_fields=["scoring_settings"])
        cache.clear()

    def test_each_player_of_a_written_row_is_queued_once(self):
        with mock.patch("afc_rankings.signals._enqueue_player") as enq_player, \
                mock.patch("afc_rankings.signals._enqueue_team"):
            with self.captureOnCommitCallbacks(execute=True):
                write_team_result_row(match=self.match, tournament_team_id=self.tt.tournament_team_id,
                                      row=_row([self.user.pk]), ctx=scoring_context(self.match),
                                      frozen_roles={})
        queued = [c.args[0] for c in enq_player.call_args_list]
        self.assertEqual(queued, [self.user.pk])

    def test_nothing_is_queued_before_the_commit(self):
        with mock.patch("afc_rankings.signals._enqueue_player") as enq_player, \
                mock.patch("afc_rankings.signals._enqueue_team"):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                write_team_result_row(match=self.match, tournament_team_id=self.tt.tournament_team_id,
                                      row=_row([self.user.pk]), ctx=scoring_context(self.match),
                                      frozen_roles={})
            self.assertEqual(enq_player.call_count, 0)
            self.assertTrue(callbacks)


@override_settings(CACHES=LOCMEM, RANKINGS_RECALC_SYNC=False)
class RecomputeSkipsUnchangedTests(_EventFixtureMixin, TestCase):
    def setUp(self):
        self._build(username="skipq", team_name="Skip FC", season_name="Skip 2099 Q2")
        self.match.scoring_settings = {"placement_points": {"1": 12}, "kill_point": 1}
        self.match.save(update_fields=["scoring_settings"])
        with self.captureOnCommitCallbacks(execute=False):
            self.ts = write_team_result_row(match=self.match, tournament_team_id=self.tt.tournament_team_id,
                                            row=_row([self.user.pk], kills=5), ctx=scoring_context(self.match),
                                            frozen_roles={})
        cache.clear()

    def test_an_unchanged_event_saves_and_queues_nothing(self):
        with mock.patch("afc_rankings.signals._enqueue_player") as enq_player, \
                mock.patch("afc_rankings.signals._enqueue_team") as enq_team:
            with self.captureOnCommitCallbacks(execute=True):
                recomputed = _recompute_team_kills_for_event(self.event)
        self.assertEqual(recomputed, 1)          # still counted as recomputed for the callers
        self.assertEqual(enq_team.call_count, 0)
        self.assertEqual(enq_player.call_count, 0)

    def test_a_changed_row_is_saved_and_queued(self):
        # The stored total drifts from the player rows (what a flag toggle or an old bug leaves behind).
        TournamentTeamMatchStats.objects.filter(pk=self.ts.pk).update(kills=1, kill_points=1, total_points=13)
        with mock.patch("afc_rankings.signals._enqueue_player") as enq_player, \
                mock.patch("afc_rankings.signals._enqueue_team") as enq_team:
            with self.captureOnCommitCallbacks(execute=True):
                _recompute_team_kills_for_event(self.event)
        self.ts.refresh_from_db()
        self.assertEqual((self.ts.kills, self.ts.kill_points, self.ts.total_points), (5, 5, 17))
        self.assertEqual(enq_team.call_count, 1)
        self.assertEqual([c.args[0] for c in enq_player.call_args_list], [self.user.pk])
