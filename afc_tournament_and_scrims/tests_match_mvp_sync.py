"""Match.mvp is kept equal to each map's MVP by the event's saved MVP rule (owner 14 Nov 2026, AFC inbox #579:
"yes fix it also for future sessions"). See afc_tournament_and_scrims/mvp_sync.py.

Fixtures are the board tests' (tests_board.BoardTests.setUp): map 1 ALPHA 2nd (ALPHA1 4 kills 300 damage, ALPHA2 1 kill
700 damage 1 assist), BRAVO booyah (BRAVO1 2 kills 300, BRAVO2 1 kill 700 1 assist); map 2 ALPHA booyah (ALPHA1 5
kills), BRAVO 2nd (1 and 1); map 3 not played yet (no player lines).

Run: python manage.py test afc_tournament_and_scrims.tests_match_mvp_sync
"""
import datetime
import io
from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase, override_settings

from afc_partner_api.serialize import serialize_match
from afc_tournament_and_scrims import mvp_sync, tests_board
from afc_tournament_and_scrims.models import Event, Match


def _mvp(match):
    return Match.objects.get(pk=match.pk).mvp_id


@override_settings(CACHES=tests_board.LOCMEM)
class MatchMvpSyncTests(TestCase):
    # The board fixtures, without importing BoardTests itself (the runner would run its tests twice).
    setUp = tests_board.BoardTests.setUp

    def test_default_rule_fills_each_played_map(self):
        changes, judged = mvp_sync.sync_event_match_mvps(self.event)
        self.assertEqual((len(changes), judged), (2, 2))
        self.assertEqual(_mvp(self.maps[0]), self.users["ALPHA1"].pk)    # kills first: 4
        self.assertEqual(_mvp(self.maps[1]), self.users["ALPHA1"].pk)    # 5
        self.assertIsNone(_mvp(self.maps[2]))                            # no player lines: nothing to judge

    def test_a_second_run_changes_nothing(self):
        mvp_sync.sync_event_match_mvps(self.event)
        changes, _judged = mvp_sync.sync_event_match_mvps(self.event)
        self.assertEqual(changes, [])

    def test_saving_the_rule_on_the_mvps_tab_stores_the_new_winners(self):
        mvp_sync.sync_event_match_mvps(self.event)
        resp = self.client.post(f"/events/{self.event.event_id}/mvp/", {"criteria": ["kills"], "scope": "winning_team"},
                                format="json", HTTP_AUTHORIZATION="Bearer bd-admin-token")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(_mvp(self.maps[0]), self.users["BRAVO1"].pk)    # map 1's booyah team: BRAVO
        self.assertEqual(_mvp(self.maps[1]), self.users["ALPHA1"].pk)

    def test_a_full_tie_goes_to_the_same_player_every_time(self):
        # Damage then kills: ALPHA2 and BRAVO2 both 700 damage, 1 kill on map 1. The earliest stat line wins.
        Event.objects.filter(pk=self.event.pk).update(mvp_config={"criteria": ["damage", "kills"], "scope": "overall"})
        self.event.refresh_from_db()
        mvp_sync.sync_event_match_mvps(self.event)
        self.assertEqual(_mvp(self.maps[0]), self.users["ALPHA2"].pk)
        for _ in range(3):
            self.assertEqual(mvp_sync.sync_event_match_mvps(self.event)[0], [])

    def test_dry_run_writes_nothing(self):
        changes, _judged = mvp_sync.sync_event_match_mvps(self.event, apply=False)
        self.assertEqual(len(changes), 2)
        self.assertIsNone(_mvp(self.maps[0]))

    def test_the_readers_see_it(self):
        mvp_sync.sync_event_match_mvps(self.event)
        # Player profile / player pages / rankings count Match.mvp rows; the partner API names the player.
        self.assertEqual(Match.objects.filter(mvp=self.users["ALPHA1"]).count(), 2)
        partner = SimpleNamespace(include_maps=True, include_mvp=True)
        out = serialize_match(Match.objects.get(pk=self.maps[0].pk), partner)
        self.assertEqual(out["mvp"], "ALPHA1")

    def test_the_command_is_a_dry_run_unless_apply(self):
        out = io.StringIO()
        call_command("sync_match_mvps", "--event-id", str(self.event.event_id), stdout=out)
        self.assertIsNone(_mvp(self.maps[0]))
        self.assertIn("DRY RUN: events 1, maps judged 2, would change 2", out.getvalue())
        call_command("sync_match_mvps", "--event-id", str(self.event.event_id), "--apply", stdout=io.StringIO())
        self.assertEqual(_mvp(self.maps[0]), self.users["ALPHA1"].pk)

    def test_the_ten_minute_sweep_picks_running_and_recent_events(self):
        self.assertIn(self.event, list(mvp_sync.recent_events()))            # ongoing
        old = datetime.date.today() - datetime.timedelta(days=30)
        Event.objects.filter(pk=self.event.pk).update(event_status="completed", start_date=old, end_date=old)
        self.assertNotIn(self.event.pk, [e.pk for e in mvp_sync.recent_events()])
        self.assertIn(self.event.pk, [e.pk for e in mvp_sync.events_with_results()])   # the nightly sweep has it

    def test_the_tasks_run_the_sync(self):
        from afc_tournament_and_scrims.tasks import sync_match_mvps_all, sync_match_mvps_recent
        self.assertEqual(sync_match_mvps_recent(), {"events": 1, "maps": 2, "changed": 2, "failed": 0})
        self.assertEqual(sync_match_mvps_all(), {"events": 1, "maps": 2, "changed": 0, "failed": 0})
