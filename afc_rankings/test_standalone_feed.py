"""
afc_rankings.test_standalone_feed — Stream P3 (standalone leaderboard -> rankings feed) tests.

Sibling flat test module to afc_rankings/tests.py (afc_rankings uses a flat tests.py, not a tests
package; this stays a flat module so both are discovered). Covers afc_rankings/standalone.py +
the aggregation wiring + the signals/tasks recompute path, exercising the locked P3 decisions in
WEBSITE/tasks/standalone-leaderboard-p2p3-design.md.

Builds a real published+counting standalone TEAM leaderboard with two real teams + a ghost team,
enters per-map results, and asserts the rankings engine sees the right contribution while leaving
the existing event-only output untouched (the regression that gates the whole stream).
"""
import datetime

from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model

from afc_team.models import Team
from afc_rankings.models import (
    Season, GhostTeam, TeamMonthlyScore, TeamQuarterlyScore,
    PlayerMonthlyScore,
)
from afc_rankings import standalone, aggregation, recalc
from afc_rankings.scoring import engine
from afc_rankings.scoring.engine import ScrimInput
from afc_leaderboard.models import (
    StandaloneLeaderboard, LeaderboardParticipant, LeaderboardMatch, ParticipantMatchResult,
)
from afc_tournament_and_scrims.scoring import normalize_placement_points, compute_team_points, compute_solo_points

User = get_user_model()


# ───────────────────────── shared builders ─────────────────────────
# A standalone LB whose effective date lands inside PLAYED_MONTH so its results bucket into the
# month/season the tests recompute. We deliberately give the LB a NON-canonical placement_points
# config ({"1": 100, ...}) so the assertions can prove the rankings feed ignores it and uses the
# canonical engine.placement_points table instead.
PLAYED_ON = datetime.date(2099, 2, 10)
PLAYED_MONTH = datetime.date(2099, 2, 1)
LB_PLACEMENT_POINTS = {"1": 100, "2": 50, "3": 25}  # intentionally != canonical (12/9/8)


def _make_season():
    return Season.objects.create(
        name="P3 Season", quarter=1, year=2099,
        start_date=datetime.date(2099, 1, 1), end_date=datetime.date(2099, 3, 31),
        transfer_window_open=datetime.date(2099, 1, 1),
        transfer_window_close=datetime.date(2099, 1, 14),
        is_active=True,
    )


def _make_team(name, owner):
    return Team.objects.create(
        team_name=name, join_settings="open",
        team_creator=owner, team_owner=owner, country="NG",
    )


def _make_lb(creator, *, fmt="team", status="published", counts=True,
             tier="tier_3", played_on=PLAYED_ON):
    return StandaloneLeaderboard.objects.create(
        name="P3 Cup", format=fmt, placement_points=LB_PLACEMENT_POINTS,
        kill_point=1.0, counts_toward_rankings=counts, ranking_tier=tier,
        status=status, played_on=played_on, creator=creator,
    )


def _add_team_participant(lb, team=None, ghost_team=None):
    return LeaderboardParticipant.objects.create(leaderboard=lb, team=team, ghost_team=ghost_team)


def _add_solo_participant(lb, user):
    return LeaderboardParticipant.objects.create(leaderboard=lb, user=user)


def _save_result(lb, match, participant, placement, kills):
    """Write a ParticipantMatchResult scored via the SAME helper the view uses (so the LB's own
    standings are honest), so `won` derived from standings matches what the FE would show."""
    pp = normalize_placement_points(lb.placement_points)
    if lb.format == "team":
        pts = compute_team_points(
            placement_points=pp, kill_point=lb.kill_point, points_per_assist=0.0,
            points_per_1000_damage=0.0, placement=placement, kills=kills,
            damage=0, assists=0, bonus=0, penalty=0, played=True,
        )
    else:
        pts = compute_solo_points(
            placement_points=pp, kill_point=lb.kill_point,
            placement=placement, kills=kills, played=True,
        )
    # update_or_create mirrors the view's upsert (unique per match+participant), so re-saving the
    # same row in the edit test overwrites rather than violating the constraint.
    obj, _ = ParticipantMatchResult.objects.update_or_create(
        match=match, participant=participant,
        defaults=dict(
            placement=placement, kills=kills,
            placement_points=pts["placement_points"], kill_points=pts["kill_points"],
            total_points=pts["total_points"], played=True,
        ),
    )
    return obj


# ═════════════════════════ Task 3.2 — input builders ═════════════════════════
class StandaloneInputBuilderTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="p3owner", email="p3o@x.com")
        self.season = _make_season()
        self.teamA = _make_team("Alpha", self.owner)
        self.teamB = _make_team("Bravo", self.owner)
        self.start, self.end = aggregation.month_bounds(PLAYED_MONTH)

        # A published, counting, team-format LB. teamA wins (placement 1, more kills), teamB second.
        self.lb = _make_lb(self.owner, tier="tier_3")
        self.pA = _add_team_participant(self.lb, team=self.teamA)
        self.pB = _add_team_participant(self.lb, team=self.teamB)
        self.m1 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=1)
        _save_result(self.lb, self.m1, self.pA, placement=1, kills=10)
        _save_result(self.lb, self.m1, self.pB, placement=2, kills=4)

    def test_team_inputs_use_canonical_placement_and_summed_kills(self):
        inputs = standalone.standalone_team_inputs(self.teamA, self.start, self.end)
        self.assertEqual(len(inputs), 1)
        inp = inputs[0]
        # Canonical placement_points: 1 -> 12 (NOT the LB's own 100).
        self.assertEqual(inp.raw_placement_pts, engine.placement_points(1))
        self.assertEqual(inp.raw_placement_pts, 12)
        self.assertNotEqual(inp.raw_placement_pts, 100)  # the LB's own config is ignored
        self.assertEqual(inp.raw_kills, 10)
        self.assertEqual(inp.tier, "tier_3")
        self.assertTrue(inp.won, "teamA leads the standings so won must be True")
        self.assertEqual(inp.finals_appearances, 0)

    def test_non_leader_won_is_false(self):
        inputs = standalone.standalone_team_inputs(self.teamB, self.start, self.end)
        self.assertEqual(len(inputs), 1)
        self.assertFalse(inputs[0].won, "teamB is not the standings leader")

    def test_tier_string_flows_through(self):
        self.lb.ranking_tier = "tier_1"
        self.lb.save()
        inputs = standalone.standalone_team_inputs(self.teamA, self.start, self.end)
        self.assertEqual(inputs[0].tier, "tier_1")

    def test_draft_lb_yields_nothing(self):
        self.lb.status = "draft"
        self.lb.save()
        self.assertEqual(standalone.standalone_team_inputs(self.teamA, self.start, self.end), [])

    def test_flag_off_lb_yields_nothing(self):
        self.lb.counts_toward_rankings = False
        self.lb.save()
        self.assertEqual(standalone.standalone_team_inputs(self.teamA, self.start, self.end), [])

    def test_lb_outside_window_yields_nothing(self):
        # An LB played in a different month must not contribute to this month's window.
        self.lb.played_on = datetime.date(2099, 5, 10)
        self.lb.save()
        self.assertEqual(standalone.standalone_team_inputs(self.teamA, self.start, self.end), [])

    def test_solo_player_input(self):
        solo_lb = _make_lb(self.owner, fmt="solo", tier="tier_2")
        u = User.objects.create(username="solo1", email="solo1@x.com")
        p = _add_solo_participant(solo_lb, u)
        sm = LeaderboardMatch.objects.create(leaderboard=solo_lb, match_number=1)
        _save_result(solo_lb, sm, p, placement=1, kills=7)
        inputs = standalone.standalone_player_inputs(u, self.start, self.end)
        self.assertEqual(len(inputs), 1)
        inp = inputs[0]
        self.assertEqual(inp.personal_kills, 7)
        self.assertEqual(inp.personal_placement_pts, 0)  # players never score raw placement
        self.assertEqual(inp.tier, "tier_2")
        self.assertTrue(inp.participated)
        self.assertFalse(inp.team_won)


# ═════════════════════════ Task 3.3 — ghost-team compute + recalc ═════════════════════════
class GhostTeamRecalcTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="ghostowner", email="go@x.com")
        self.season = _make_season()
        # A ghost team + a real team both in the same published+counting LB.
        self.ghost = GhostTeam.objects.create(team_name="Ghost FC", country="NG", created_by=self.owner)
        self.real = _make_team("Real FC", self.owner)
        self.lb = _make_lb(self.owner, tier="tier_3")
        self.pGhost = _add_team_participant(self.lb, ghost_team=self.ghost)
        self.pReal = _add_team_participant(self.lb, team=self.real)
        m1 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=1)
        _save_result(self.lb, m1, self.pGhost, placement=1, kills=12)
        _save_result(self.lb, m1, self.pReal, placement=2, kills=5)

    def test_ghost_monthly_row_created_and_ranked(self):
        # Owner directive 2026-06-10: ghost teams are now ranked (no longer rank 0). With only the
        # ghost present in the month, it ranks #1.
        standalone.recalc_ghost_team_monthly(self.ghost.pk, PLAYED_MONTH)
        row = TeamMonthlyScore.objects.get(ghost_team=self.ghost, month=PLAYED_MONTH)
        self.assertEqual(row.rank, 1, "the only ghost row in the month ranks #1")
        self.assertEqual(row.tournaments_played, 1)
        self.assertEqual(row.total_kills, 12)
        # totals match the engine computing from the standalone input directly
        expected = standalone.compute_ghost_team_monthly(self.ghost, PLAYED_MONTH)
        self.assertEqual(row.total_score, expected.result.total)
        self.assertEqual(row.tournament_wins, 1, "ghost is the standings leader")

    def test_ghost_quarterly_row_created_and_ranked(self):
        standalone.recalc_ghost_team_quarterly(self.ghost.pk, self.season.season_id)
        row = TeamQuarterlyScore.objects.get(ghost_team=self.ghost, season=self.season)
        self.assertEqual(row.rank, 1, "the only ghost row in the season ranks #1")
        self.assertEqual(row.participated_in_tournaments, 1)
        expected = standalone.compute_ghost_team_quarterly(self.ghost, self.season)
        self.assertEqual(row.total_score, expected.result.total)

    def test_ghost_floor_deletes_row_at_zero_tournaments(self):
        # First create a row, then unpublish the LB so the ghost has 0 counting tournaments.
        standalone.recalc_ghost_team_monthly(self.ghost.pk, PLAYED_MONTH)
        self.assertTrue(TeamMonthlyScore.objects.filter(ghost_team=self.ghost, month=PLAYED_MONTH).exists())
        self.lb.status = "draft"
        self.lb.save()
        standalone.recalc_ghost_team_monthly(self.ghost.pk, PLAYED_MONTH)
        self.assertFalse(
            TeamMonthlyScore.objects.filter(ghost_team=self.ghost, month=PLAYED_MONTH).exists(),
            "ghost row should be removed by the participation floor",
        )

    def test_ghost_write_interleaves_without_reordering_real_teams(self):
        # Owner directive 2026-06-10: a ghost write now DOES rerank the table (ghost + real together).
        # A real team that OUTSCORES the ghost keeps rank #1; the ghost slots in at #2. The real
        # team's relative position is preserved (only the rank number is shared by score order).
        TeamMonthlyScore.objects.create(
            team=self.real, month=PLAYED_MONTH, total_score=9999, rank=1, tournaments_played=1,
        )
        standalone.recalc_ghost_team_monthly(self.ghost.pk, PLAYED_MONTH)
        real_row = TeamMonthlyScore.objects.get(team=self.real, month=PLAYED_MONTH)
        ghost_row = TeamMonthlyScore.objects.get(ghost_team=self.ghost, month=PLAYED_MONTH)
        self.assertEqual(real_row.rank, 1, "the higher-scoring real team stays ahead of the ghost")
        self.assertEqual(ghost_row.rank, 2, "the ghost interleaves below the higher real team")


# ═════════════════════════ Task 3.4 — wire standalone into event aggregation ═════════════════════════
class AggregationWiringTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="aggowner", email="aw@x.com")
        self.season = _make_season()
        self.team = _make_team("Aggro", self.owner)

    def test_real_team_with_only_standalone_lb_appears(self):
        # A real team that played ONLY a published+counting standalone team LB (no events) must now
        # appear in compute_team_monthly with the standalone contribution folded in.
        lb = _make_lb(self.owner, tier="tier_3")
        p = _add_team_participant(lb, team=self.team)
        m1 = LeaderboardMatch.objects.create(leaderboard=lb, match_number=1)
        _save_result(lb, m1, p, placement=1, kills=10)

        agg = aggregation.compute_team_monthly(self.team, PLAYED_MONTH)
        self.assertEqual(agg.tournaments_played, 1, "the standalone LB counts as one tournament")
        self.assertEqual(agg.total_kills, 10)
        # The total equals the engine scoring the single standalone TournamentInput directly.
        inputs = standalone.standalone_team_inputs(self.team, *aggregation.month_bounds(PLAYED_MONTH))
        expected = engine.monthly_team_score(inputs, ScrimInput(0, 0, 0))
        self.assertEqual(agg.result.total, expected.total)
        self.assertGreater(agg.result.total, 0)

    def test_draft_and_flag_off_add_nothing(self):
        draft = _make_lb(self.owner, status="draft", tier="tier_3")
        p1 = _add_team_participant(draft, team=self.team)
        m1 = LeaderboardMatch.objects.create(leaderboard=draft, match_number=1)
        _save_result(draft, m1, p1, placement=1, kills=10)

        flag_off = _make_lb(self.owner, counts=False, tier="tier_3")
        p2 = _add_team_participant(flag_off, team=self.team)
        m2 = LeaderboardMatch.objects.create(leaderboard=flag_off, match_number=1)
        _save_result(flag_off, m2, p2, placement=1, kills=10)

        agg = aggregation.compute_team_monthly(self.team, PLAYED_MONTH)
        # Neither a draft nor a flag-off LB contributes, so the team has no counting activity and is
        # an empty (floor) aggregate.
        self.assertEqual(agg.tournaments_played, 0)
        self.assertEqual(agg.total_kills, 0)
        self.assertEqual(agg.result.total, 0)

    # ── REGRESSION (mandatory): with NO standalone LBs, the event-only output is unchanged. ──
    def test_regression_no_standalone_lb_team_output_identical(self):
        # No standalone leaderboards exist in this test DB at all, so the standalone hook must add
        # exactly zero — the standalone builder returns [] and compute_team_monthly is a pure
        # event-only computation. We assert the hook is a true no-op by comparing the full result to
        # the engine scoring ONLY the (empty) event input list.
        from afc_leaderboard.models import StandaloneLeaderboard
        self.assertEqual(StandaloneLeaderboard.objects.count(), 0)

        start, end = aggregation.month_bounds(PLAYED_MONTH)
        # The standalone builder is empty -> the appended delta is [].
        self.assertEqual(standalone.standalone_team_inputs(self.team, start, end), [])

        # End-to-end: a team with no event AND no standalone data is a 0-tournament floor aggregate,
        # byte-identical to what the pre-P3 code produced (no rows, total 0).
        agg = aggregation.compute_team_monthly(self.team, PLAYED_MONTH)
        self.assertEqual(agg.tournaments_played, 0)
        self.assertEqual(agg.total_kills, 0)
        self.assertEqual(agg.result.total, 0)
        self.assertEqual(agg.result.tournament_pts, 0)
        self.assertEqual(agg.result.scrim_pts, 0)

    def test_regression_no_standalone_lb_player_output_identical(self):
        from afc_leaderboard.models import StandaloneLeaderboard
        self.assertEqual(StandaloneLeaderboard.objects.count(), 0)
        player = User.objects.create(username="aggplayer", email="ap@x.com")
        start, end = aggregation.month_bounds(PLAYED_MONTH)
        self.assertEqual(standalone.standalone_player_inputs(player, start, end), [])

        agg = aggregation.compute_player_monthly(player, PLAYED_MONTH)
        self.assertEqual(agg.tournaments_played, 0)
        self.assertEqual(agg.total_kills, 0)
        self.assertEqual(agg.result.total, 0)

    def test_real_user_with_only_solo_standalone_lb_appears(self):
        solo_lb = _make_lb(self.owner, fmt="solo", tier="tier_1")
        u = User.objects.create(username="soloagg", email="sa@x.com")
        p = _add_solo_participant(solo_lb, u)
        sm = LeaderboardMatch.objects.create(leaderboard=solo_lb, match_number=1)
        _save_result(solo_lb, sm, p, placement=1, kills=9)

        agg = aggregation.compute_player_monthly(u, PLAYED_MONTH)
        self.assertEqual(agg.tournaments_played, 1)
        self.assertEqual(agg.total_kills, 9)
        self.assertGreater(agg.result.total, 0)


# ═════════════════════════ Task 3.5 — signals + recompute (sync) ═════════════════════════
# RANKINGS_RECALC_SYNC=True makes tasks._dispatch run inline; captureOnCommitCallbacks(execute=True)
# fires the transaction.on_commit hooks the receivers register (a TestCase transaction never commits
# on its own). Together they exercise the full signal -> recompute_for_leaderboard -> enqueue ->
# recalc path end-to-end without a Celery worker.
@override_settings(RANKINGS_RECALC_SYNC=True)
class SignalRecomputeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="sigowner", email="sg@x.com")
        self.season = _make_season()
        self.team = _make_team("Signal FC", self.owner)
        self.ghost = GhostTeam.objects.create(team_name="Sig Ghost", country="NG", created_by=self.owner)

    def _build_published_lb_with_results(self):
        """A published+counting team LB with a real team + a ghost team and one map of results."""
        lb = _make_lb(self.owner, tier="tier_3")
        pReal = _add_team_participant(lb, team=self.team)
        pGhost = _add_team_participant(lb, ghost_team=self.ghost)
        m1 = LeaderboardMatch.objects.create(leaderboard=lb, match_number=1)
        return lb, pReal, pGhost, m1

    def test_saving_a_result_creates_team_monthly_row(self):
        lb, pReal, pGhost, m1 = self._build_published_lb_with_results()
        with self.captureOnCommitCallbacks(execute=True):
            _save_result(lb, m1, pReal, placement=1, kills=8)
        self.assertTrue(
            TeamMonthlyScore.objects.filter(team=self.team, month=PLAYED_MONTH).exists(),
            "saving a standalone result must create the real team's monthly score row",
        )

    def test_publish_creates_rows_for_all_participants(self):
        # Build the LB as a DRAFT first (so the initial result saves don't yet count), then publish.
        lb = _make_lb(self.owner, status="draft", tier="tier_3")
        pReal = _add_team_participant(lb, team=self.team)
        pGhost = _add_team_participant(lb, ghost_team=self.ghost)
        m1 = LeaderboardMatch.objects.create(leaderboard=lb, match_number=1)
        with self.captureOnCommitCallbacks(execute=True):
            _save_result(lb, m1, pReal, placement=1, kills=8)
            _save_result(lb, m1, pGhost, placement=2, kills=3)
        # Draft => nothing counts yet.
        self.assertFalse(TeamMonthlyScore.objects.filter(team=self.team, month=PLAYED_MONTH).exists())
        self.assertFalse(TeamMonthlyScore.objects.filter(ghost_team=self.ghost, month=PLAYED_MONTH).exists())

        with self.captureOnCommitCallbacks(execute=True):
            lb.status = "published"
            lb.save()
        # Publishing recomputes ALL participants -> both rows now exist, each ranked (owner directive
        # 2026-06-10: ghost rows are no longer rank 0). The real team won (placement 1, more kills) so
        # it leads; the ghost interleaves below it.
        self.assertTrue(TeamMonthlyScore.objects.filter(team=self.team, month=PLAYED_MONTH).exists())
        ghost_row = TeamMonthlyScore.objects.get(ghost_team=self.ghost, month=PLAYED_MONTH)
        real_row = TeamMonthlyScore.objects.get(team=self.team, month=PLAYED_MONTH)
        self.assertEqual(real_row.rank, 1)
        self.assertEqual(ghost_row.rank, 2)

    def test_unpublish_removes_the_contribution(self):
        lb, pReal, pGhost, m1 = self._build_published_lb_with_results()
        with self.captureOnCommitCallbacks(execute=True):
            _save_result(lb, m1, pReal, placement=1, kills=8)
        self.assertTrue(TeamMonthlyScore.objects.filter(team=self.team, month=PLAYED_MONTH).exists())

        with self.captureOnCommitCallbacks(execute=True):
            lb.status = "draft"
            lb.save()
        # The team had no other activity, so the floor drops its row once the LB stops counting.
        self.assertFalse(
            TeamMonthlyScore.objects.filter(team=self.team, month=PLAYED_MONTH).exists(),
            "un-publishing must drop the standalone contribution",
        )

    def test_toggle_off_removes_the_contribution(self):
        lb, pReal, pGhost, m1 = self._build_published_lb_with_results()
        with self.captureOnCommitCallbacks(execute=True):
            _save_result(lb, m1, pReal, placement=1, kills=8)
        self.assertTrue(TeamMonthlyScore.objects.filter(team=self.team, month=PLAYED_MONTH).exists())

        with self.captureOnCommitCallbacks(execute=True):
            lb.counts_toward_rankings = False
            lb.save()
        self.assertFalse(
            TeamMonthlyScore.objects.filter(team=self.team, month=PLAYED_MONTH).exists(),
            "toggling counts_toward_rankings off must drop the standalone contribution",
        )

    def test_editing_a_kill_rescores(self):
        lb, pReal, pGhost, m1 = self._build_published_lb_with_results()
        with self.captureOnCommitCallbacks(execute=True):
            _save_result(lb, m1, pReal, placement=1, kills=2)
        row1 = TeamMonthlyScore.objects.get(team=self.team, month=PLAYED_MONTH)
        first_kills = row1.total_kills

        with self.captureOnCommitCallbacks(execute=True):
            # Overwrite the same result with more kills (unique per match+participant).
            _save_result(lb, m1, pReal, placement=1, kills=40)
        row2 = TeamMonthlyScore.objects.get(team=self.team, month=PLAYED_MONTH)
        self.assertEqual(first_kills, 2)
        self.assertEqual(row2.total_kills, 40, "editing the kills must re-score the monthly row")
