"""
afc_tournament_and_scrims/tests_live_push_scoring.py
================================================================================
The capture client observes; the event and leaderboard models calculate (owner 2026-09-22).

What is proven here:
  1. The placement rule lives on the SERVER: a wiped team is locked at ``N - elimination_order + 1``
     and the living take the top slots ordered by this match's kills, then players still standing,
     then name. The client sends no placement at all.
  2. A client cannot influence the board with numbers of its own: rows pushed in reverse order, with
     invented point columns, come back ordered and scored from the event's config.
  3. The points come from THIS event's scoring (match.scoring_settings first, then the Leaderboard
     row), through scoring.compute_team_points - the same function the official upload path uses.
  4. A pre-1.4.0 client (rows with no elimination facts) still renders: its pushed order is kept and
     its points are recomputed anyway.
  5. Every observation the client sent (the rich stats and players[]) survives the pass untouched.

Run: python manage.py test afc_tournament_and_scrims.tests_live_push_scoring
"""
import datetime

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from afc_auth.models import User
from afc_tournament_and_scrims.live_ranking import normalize_live_standings, rank_rows
from afc_tournament_and_scrims.models import (
    Event, EventUploadToken, Leaderboard, Match, StageGroups, Stages,
)

# The canonical AFC table, as scoring.DEFAULT_PLACEMENT holds it.
TABLE = {1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5, 7: 4, 8: 3, 9: 2, 10: 1}


def _row(name, **kw):
    """An observation row shaped like afc-capture 1.4.0's snapshot: counts and facts, no points."""
    row = {
        "team_name": name,
        "kills": 0,
        "eliminated": False,
        "elimination_order": None,
        "alive_count": 4,
        "roster_size": 4,
    }
    row.update(kw)
    return row


class PlacementRuleTests(TestCase):
    """The rule that used to live in the client's _placements, now server-side."""

    def test_wiped_teams_are_locked_first_out_finishes_last(self):
        rows = [
            _row("ALIVE ONE", kills=2),
            _row("OUT FIRST", eliminated=True, elimination_order=1, alive_count=0, kills=9),
            _row("OUT SECOND", eliminated=True, elimination_order=2, alive_count=0, kills=8),
        ]
        ranked = rank_rows(rows)
        self.assertEqual([(r["team_name"], p) for r, p in ranked],
                         [("ALIVE ONE", 1), ("OUT SECOND", 2), ("OUT FIRST", 3)])

    def test_living_teams_rank_by_this_matchs_kills_then_players_left(self):
        rows = [
            _row("TWO KILLS", kills=2, alive_count=1),
            _row("FIVE KILLS", kills=5, alive_count=1),
            _row("TWO KILLS MORE ALIVE", kills=2, alive_count=3),
        ]
        self.assertEqual([r["team_name"] for r, _ in rank_rows(rows)],
                         ["FIVE KILLS", "TWO KILLS MORE ALIVE", "TWO KILLS"])

    def test_equal_rows_order_by_name_so_two_pushes_do_not_flicker(self):
        rows = [_row("ZULU"), _row("ALPHA"), _row("MIKE")]
        self.assertEqual([r["team_name"] for r, _ in rank_rows(rows)], ["ALPHA", "MIKE", "ZULU"])

    def test_placements_are_a_gap_free_permutation(self):
        rows = [_row("A", kills=1), _row("B", eliminated=True, elimination_order=1, alive_count=0),
                _row("C", kills=3), _row("D", eliminated=True, elimination_order=2, alive_count=0)]
        self.assertEqual(sorted(p for _, p in rank_rows(rows)), [1, 2, 3, 4])


class ClientNumbersAreIgnoredTests(TestCase):
    def test_reverse_order_with_invented_points_is_reordered_and_rescored(self):
        rows = [
            # The client claims the wiped team leads with 999 points, and pushes worst first.
            _row("OUT FIRST", eliminated=True, elimination_order=1, alive_count=0, kills=1,
                 pos=1, placement=1, total_points=999, kill_points=999, placement_points=999),
            _row("TOP", kills=7, pos=3, placement=3, total_points=0, kill_points=0,
                 placement_points=0),
        ]
        out = normalize_live_standings(rows, placement_points=TABLE, kill_point=1.0)
        self.assertEqual([r["team_name"] for r in out], ["TOP", "OUT FIRST"])
        self.assertEqual([r["pos"] for r in out], [1, 2])
        # TOP: 7 kills * 1 + 12 for placement 1
        self.assertEqual((out[0]["kills"], out[0]["kill_points"], out[0]["placement_points"],
                          out[0]["total_points"]), (7, 7, 12, 19))
        # OUT FIRST: locked last of two -> 9 points for 2nd, 1 kill
        self.assertEqual((out[1]["kill_points"], out[1]["placement_points"], out[1]["total_points"]),
                         (1, 9, 10))

    def test_observations_pass_through_untouched(self):
        rows = [_row("KEEPER", kills=3, knockdowns=5, knocked=2, headshots=1, grenades_used=4,
                     grenade_kills=1, gloowall_used=6, medkit_used=2, most_used_weapon="9",
                     survival_time=1010.5, team_name_source="roster",
                     players=[{"uid": "1001", "name": "moussa", "kills": 3}])]
        out = normalize_live_standings(rows, placement_points=TABLE, kill_point=1.0)
        r = out[0]
        self.assertEqual((r["knockdowns"], r["knocked"], r["headshots"]), (5, 2, 1))
        self.assertEqual((r["grenades_used"], r["grenade_kills"], r["gloowall_used"]), (4, 1, 6))
        self.assertEqual((r["most_used_weapon"], r["survival_time"]), ("9", 1010.5))
        self.assertEqual(r["team_name_source"], "roster")
        self.assertEqual(r["players"][0]["uid"], "1001")

    def test_a_pre_1_4_0_client_keeps_its_order_and_is_still_scored(self):
        rows = [
            {"team_name": "OLD A", "kills": 2, "pos": 1, "total_points": 41},
            {"team_name": "OLD B", "kills": 5, "pos": 2, "total_points": 40},
        ]
        out = normalize_live_standings(rows, placement_points=TABLE, kill_point=1.0)
        self.assertEqual([r["team_name"] for r in out], ["OLD A", "OLD B"])
        self.assertEqual(out[0]["total_points"], 2 + 12)
        self.assertEqual(out[1]["total_points"], 5 + 9)

    def test_a_malformed_row_never_drops_the_snapshot(self):
        rows = [_row("GOOD", kills=1), {"team_name": "BAD", "kills": "lots"}]
        out = normalize_live_standings(rows, placement_points=TABLE, kill_point=1.0)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["team_name"], "GOOD")

    def test_assists_score_only_when_the_event_pays_for_them(self):
        rows = [_row("A", kills=1, assists=4)]
        plain = normalize_live_standings(rows, placement_points=TABLE, kill_point=1.0)
        paid = normalize_live_standings(rows, placement_points=TABLE, kill_point=1.0,
                                        points_per_assist=0.5)
        self.assertEqual(plain[0]["total_points"], 1 + 12)
        self.assertEqual(paid[0]["total_points"], int(1 + 12 + 4 * 0.5))


@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "live-push-tests"}})
class LivePushEndpointTests(TestCase):
    """The view end: a real push, scored from the event's own config, read back off the cache key.

    The live snapshot lives in the cache, which is Redis in production. These tests use the in-memory
    backend so they prove the VIEW (auth, scope, ranking, scoring, the shared key) on a machine with
    no Redis; the key itself is built by the same _overlay_live_key both sides share."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(username="livepush", email="livepush@x.com",
                                         full_name="Live Push", role="admin", password="x")
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Live Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin,
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1,
        )
        self.lb = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual",
        )
        # A CUSTOM per-match config: 20 for first, 3 points a kill. The live board must use THIS.
        Match.objects.create(
            leaderboard=self.lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 20, "2": 10}, "kill_point": 3},
        )
        self.token = EventUploadToken.objects.create(event=self.event, created_by=self.admin,
                                                     label="Observer PC 1")

    def _push(self, standings):
        return self.client.post(
            "/events/live/push/?token=%s" % self.token.token,
            data={"event_id": self.event.event_id, "stage_id": self.stage.stage_id,
                  "group_id": self.group.group_id, "standings": standings},
            format="json",
        )

    def _cached(self):
        from afc_tournament_and_scrims.views import _overlay_live_key
        return cache.get(_overlay_live_key(self.event.event_id, self.stage.stage_id,
                                           self.group.group_id))

    def test_the_event_scoring_decides_the_points_not_the_client(self):
        resp = self._push([
            _row("SECOND", eliminated=True, elimination_order=1, alive_count=0, kills=1,
                 total_points=500),
            _row("FIRST", kills=2, total_points=0),
        ])
        self.assertEqual(resp.status_code, 200, resp.data)
        rows = self._cached()
        self.assertEqual([r["team_name"] for r in rows], ["FIRST", "SECOND"])
        # custom config: 3 a kill, 20 for first, 10 for second
        self.assertEqual((rows[0]["kill_points"], rows[0]["placement_points"],
                          rows[0]["total_points"]), (6, 20, 26))
        self.assertEqual((rows[1]["kill_points"], rows[1]["placement_points"],
                          rows[1]["total_points"]), (3, 10, 13))
        self.assertEqual([r["pos"] for r in rows], [1, 2])

    def test_a_snapshot_with_no_points_at_all_is_still_a_full_board(self):
        rows_in = [_row("A", kills=1), _row("B", kills=4)]
        for r in rows_in:
            self.assertNotIn("total_points", r)     # the 1.4.0 client sends none
        self.assertEqual(self._push(rows_in).status_code, 200)
        rows = self._cached()
        self.assertEqual([r["team_name"] for r in rows], ["B", "A"])
        self.assertEqual(rows[0]["total_points"], 4 * 3 + 20)
        self.assertEqual(rows[1]["total_points"], 1 * 3 + 10)
