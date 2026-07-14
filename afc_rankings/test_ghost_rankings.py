"""
afc_rankings.test_ghost_rankings (owner directive 2026-06-10): ghost teams AND ghost players are
first-class in the monthly/quarterly rankings ladders + get tiers, interleaved with real entities by
score, badged as ghost.

Sibling flat test module to afc_rankings/tests.py + test_standalone_feed.py (afc_rankings uses flat
tests modules, not a tests package, so both are auto-discovered). Covers:
  - STREAM G1: a published+counting TEAM standalone LB makes a ghost team appear in teams_monthly +
    teams_quarterly with rank > 0 and a tier; the serializer emits is_ghost + a "[Ghost]" name.
  - STREAM G2: a published+counting SOLO standalone LB with a ghost_player participant makes that
    ghost player appear in players_monthly + players_quarterly ranked + tiered; the XOR + unique
    constraints are enforced; the serializer renders a ghost player without crashing.
  - run_evaluation tiers a ghost team + a ghost player without crashing and WITHOUT altering any real
    entity's tier inheritance.
  - REGRESSION (mandatory): with NO ghost rows present, teams_monthly/quarterly +
    players_monthly/quarterly + the rerank output + run_evaluation results are IDENTICAL to before
    (real entities only), real ranks/tiers unchanged.

The spec the ghost feed implements lives in WEBSITE/tasks/standalone-leaderboard-ghost-rankings-design.md
(STREAM G1 + G2). The standalone->rankings plumbing it rides on is in afc_rankings/standalone.py.

HOW IT CONNECTS
    - Exercises afc_rankings.standalone.recalc_ghost_team_* / recalc_ghost_player_* (the recalc that
      writes ghost score rows + reranks), the rerank_* tiebreaks in afc_rankings.recalc (now
      Coalesce-based so ghost rows sort), the read views in afc_rankings.views (filters dropped +
      select_related ghost sides), and the serializers (_team_name / _player_name + is_ghost).
    - Reuses the shared LB builders from test_standalone_feed (a published+counting LB with results
      scored through the same compute_* helper the view uses) so the standings-derived `won` and the
      kill sums are honest.
"""
import datetime

from django.test import TestCase
from django.urls import reverse
from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model

from afc_team.models import Team
from afc_rankings.models import (
    Season, GhostTeam, GhostPlayer,
    TeamMonthlyScore, TeamQuarterlyScore, PlayerMonthlyScore, PlayerQuarterlyScore,
)
from afc_rankings import standalone, recalc, serializers as S
from afc_rankings.recalc import run_evaluation
from afc_leaderboard.models import LeaderboardMatch

# Reuse the standalone-feed builders verbatim (same conventions, no duplication).
from afc_rankings.test_standalone_feed import (
    PLAYED_MONTH, _make_season, _make_team, _make_lb,
    _add_team_participant, _add_solo_participant, _save_result,
)

User = get_user_model()

# The monthly read endpoints parse ?month as YYYY-MM (views._resolve_month), NOT a full ISO date, so
# the test passes the month in that exact shape.
PLAYED_MONTH_PARAM = PLAYED_MONTH.strftime("%Y-%m")


def _add_ghost_player_participant(lb, ghost_player):
    """A ghost-player participant row on a SOLO leaderboard (the missing builder vs the shared ones).
    Mirrors _add_solo_participant but points at ghost_player instead of user."""
    from afc_leaderboard.models import LeaderboardParticipant
    return LeaderboardParticipant.objects.create(leaderboard=lb, ghost_player=ghost_player)


# ═════════════════════════ STREAM G1: ghost TEAMS ranked + tiered ═════════════════════════
class GhostTeamRankedAndTieredTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="g1owner", email="g1o@x.com")
        self.season = _make_season()
        # A published+counting TEAM LB: a real team beats a ghost team (real placement 1 + more kills).
        self.real = _make_team("Real Alpha", self.owner)
        self.ghost = GhostTeam.objects.create(team_name="Ghost Bravo", country="NG", created_by=self.owner)
        self.lb = _make_lb(self.owner, tier="tier_3")
        self.pReal = _add_team_participant(self.lb, team=self.real)
        self.pGhost = _add_team_participant(self.lb, ghost_team=self.ghost)
        m1 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=1)
        _save_result(self.lb, m1, self.pReal, placement=1, kills=20)
        _save_result(self.lb, m1, self.pGhost, placement=2, kills=8)
        # Recompute both participants (real rides recalc, ghost rides the standalone ghost recalc).
        recalc.recalc_team_monthly(self.real.team_id, PLAYED_MONTH)
        standalone.recalc_ghost_team_monthly(self.ghost.pk, PLAYED_MONTH)
        recalc.recalc_team_quarterly(self.real.team_id, self.season.season_id)
        standalone.recalc_ghost_team_quarterly(self.ghost.pk, self.season.season_id)

    def test_ghost_team_in_monthly_endpoint_ranked(self):
        # Monthly standings are gated on the season's rankings_published (owner 2026-06-16), the same
        # gate the quarterly tests below set. Publish so the public endpoint returns the rows.
        self.season.rankings_published = True
        self.season.save()
        resp = self.client.get(reverse("rankings_teams_monthly"), {"month": PLAYED_MONTH_PARAM})
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        names = [r["team_name"] for r in rows]
        # Both the real and the ghost row are returned, the real one leading.
        self.assertIn("Real Alpha", names)
        self.assertIn("[Ghost] Ghost Bravo", names)
        ghost_row = next(r for r in rows if r["is_ghost"])
        self.assertGreater(ghost_row["rank"], 0, "ghost team must have a real (non-zero) rank now")
        self.assertEqual(ghost_row["team_id"], None, "a ghost row has no real team_id")
        real_row = next(r for r in rows if not r["is_ghost"])
        self.assertLess(real_row["rank"], ghost_row["rank"], "the higher-scoring real team ranks above")

    def test_ghost_team_in_quarterly_endpoint_ranked_and_tiered(self):
        # Publish rankings + tiers so the public quarterly endpoint returns the rows with tiers.
        self.season.rankings_published = True
        self.season.tiers_published = True
        self.season.save()
        resp = self.client.get(reverse("rankings_teams_quarterly"), {"season_id": self.season.season_id})
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        ghost_row = next(r for r in rows if r["is_ghost"])
        self.assertEqual(ghost_row["team_name"], "[Ghost] Ghost Bravo")
        self.assertGreater(ghost_row["rank"], 0)
        # A ghost team gets a tier from its own score (here below the >=2-tournament floor => Entry/3).
        self.assertIsNotNone(ghost_row["tier"], "ghost team must carry a tier")
        self.assertEqual(ghost_row["tier"], 3, "one tournament is below the §7.4 floor -> Entry tier")

    def test_serializer_emits_is_ghost_and_bracketed_name(self):
        row = TeamMonthlyScore.objects.get(ghost_team=self.ghost, month=PLAYED_MONTH)
        out = S.team_monthly(row)
        self.assertTrue(out["is_ghost"])
        self.assertEqual(out["team_name"], "[Ghost] Ghost Bravo")


# ═════════════════════════ STREAM G2: ghost PLAYERS ranked + tiered ═════════════════════════
class GhostPlayerRankedAndTieredTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="g2owner", email="g2o@x.com")
        self.season = _make_season()
        # A published+counting SOLO LB: a real user beats a ghost player (more kills).
        self.realuser = User.objects.create(username="RealSolo", email="rs@x.com")
        # A ghost player needs a parent ghost team (or none). Use a standalone (team-less) ghost player.
        self.ghost = GhostPlayer.objects.create(ign="GhostSolo", slot=1)
        self.lb = _make_lb(self.owner, fmt="solo", tier="tier_2")
        self.pReal = _add_solo_participant(self.lb, self.realuser)
        self.pGhost = _add_ghost_player_participant(self.lb, self.ghost)
        m1 = LeaderboardMatch.objects.create(leaderboard=self.lb, match_number=1)
        _save_result(self.lb, m1, self.pReal, placement=1, kills=15)
        _save_result(self.lb, m1, self.pGhost, placement=2, kills=6)
        recalc.recalc_player_monthly(self.realuser.pk, PLAYED_MONTH)
        standalone.recalc_ghost_player_monthly(self.ghost.pk, PLAYED_MONTH)
        recalc.recalc_player_quarterly(self.realuser.pk, self.season.season_id)
        standalone.recalc_ghost_player_quarterly(self.ghost.pk, self.season.season_id)

    def test_ghost_player_in_monthly_endpoint_ranked(self):
        # Monthly standings are gated on rankings_published (owner 2026-06-16); publish like the
        # quarterly test below so the public endpoint returns rows.
        self.season.rankings_published = True
        self.season.save()
        resp = self.client.get(reverse("rankings_players_monthly"), {"month": PLAYED_MONTH_PARAM})
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        names = [r["username"] for r in rows]
        self.assertIn("RealSolo", names)
        self.assertIn("[Ghost] GhostSolo", names)
        ghost_row = next(r for r in rows if r["is_ghost"])
        self.assertGreater(ghost_row["rank"], 0, "ghost player must have a real rank")
        self.assertIsNone(ghost_row["player_id"], "a ghost row has no real player_id")
        real_row = next(r for r in rows if not r["is_ghost"])
        self.assertLess(real_row["rank"], ghost_row["rank"], "the higher-scoring real player ranks above")

    def test_ghost_player_in_quarterly_endpoint_ranked_and_tiered(self):
        self.season.rankings_published = True
        self.season.tiers_published = True
        self.season.save()
        resp = self.client.get(reverse("rankings_players_quarterly"), {"season_id": self.season.season_id})
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        ghost_row = next(r for r in rows if r["is_ghost"])
        self.assertEqual(ghost_row["username"], "[Ghost] GhostSolo")
        self.assertGreater(ghost_row["rank"], 0)
        self.assertIsNotNone(ghost_row["tier"], "ghost player must carry a tier")
        # A ghost player is never attached -> always the INDIVIDUAL tier source.
        row = PlayerQuarterlyScore.objects.get(ghost_player=self.ghost, season=self.season)
        self.assertEqual(row.tier_source, "individual")

    def test_serializer_renders_ghost_player_without_crash(self):
        monthly = PlayerMonthlyScore.objects.get(ghost_player=self.ghost, month=PLAYED_MONTH)
        quarterly = PlayerQuarterlyScore.objects.get(ghost_player=self.ghost, season=self.season)
        # _player_name must not dereference the null player FK.
        m_out = S.player_monthly(monthly)
        q_out = S.player_quarterly(quarterly)
        self.assertTrue(m_out["is_ghost"])
        self.assertEqual(m_out["username"], "[Ghost] GhostSolo")
        self.assertTrue(q_out["is_ghost"])
        self.assertEqual(q_out["username"], "[Ghost] GhostSolo")

    def test_player_xor_ghost_constraint_enforced(self):
        # A row with BOTH player and ghost_player set violates the XOR CheckConstraint.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlayerMonthlyScore.objects.create(
                    player=self.realuser, ghost_player=self.ghost, month=PLAYED_MONTH,
                )

    def test_player_xor_ghost_requires_one(self):
        # A row with NEITHER player nor ghost_player set also violates the XOR CheckConstraint.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlayerQuarterlyScore.objects.create(season=self.season)

    def test_ghost_player_month_unique_constraint(self):
        # A second monthly row for the same ghost_player + month violates the unique constraint.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PlayerMonthlyScore.objects.create(ghost_player=self.ghost, month=PLAYED_MONTH)


# ═════════════════════════ run_evaluation tiers ghosts without touching real inheritance ═════════════════════════
class RunEvaluationWithGhostsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="evowner", email="ev@x.com")
        self.season = _make_season()
        # A real team + a rostered real player (so the player INHERITS the team's tier at eval), plus
        # a ghost team and a ghost player (both must be tiered without disturbing the inheritance).
        self.real = _make_team("Eval Real", self.owner)
        self.player = User.objects.create(username="EvalPlayer", email="evp@x.com")
        self.ghostteam = GhostTeam.objects.create(team_name="Eval Ghost FC", country="NG", created_by=self.owner)
        self.ghostplayer = GhostPlayer.objects.create(ign="EvalGhostSolo", slot=1)

        # Real team quarterly row with a high score -> Elite (tier 0), meets the floor.
        TeamQuarterlyScore.objects.create(
            team=self.real, season=self.season, total_score=200,
            participated_in_tournaments=3, meets_participation_floor=True,
        )
        # Real player rostered on the real team for this season (attached at eval -> inherits team 0).
        from afc_rankings.models import TeamSeasonRoster
        TeamSeasonRoster.objects.create(team=self.real, season=self.season, player=self.player)
        PlayerQuarterlyScore.objects.create(
            player=self.player, season=self.season, total_score=10,
            participated_in_tournaments=1, meets_participation_floor=True,
        )
        # Ghost team quarterly row (high score, meets floor) + ghost player row (low score).
        TeamQuarterlyScore.objects.create(
            ghost_team=self.ghostteam, season=self.season, total_score=160,
            participated_in_tournaments=2, meets_participation_floor=True,
        )
        PlayerQuarterlyScore.objects.create(
            ghost_player=self.ghostplayer, season=self.season, total_score=5,
            participated_in_tournaments=1, meets_participation_floor=True, tier_source="individual",
        )

    def test_evaluation_tiers_ghosts_and_preserves_real_inheritance(self):
        result = run_evaluation(self.season, user=self.owner)
        self.assertTrue(result["ok"])

        # Real team -> Elite (0) from its 200 score.
        real_team = TeamQuarterlyScore.objects.get(team=self.real, season=self.season)
        self.assertEqual(real_team.tier_assigned, 0)
        # Real player INHERITS the real team's tier (attached) -> tier 0, source "team".
        real_player = PlayerQuarterlyScore.objects.get(player=self.player, season=self.season)
        self.assertEqual(real_player.tier_assigned, 0, "attached real player inherits its team's tier")
        self.assertEqual(real_player.tier_source, "team")
        self.assertEqual(real_player.team_at_evaluation_id, self.real.team_id)

        # Ghost team -> Elite (0) from its own 160 score; it has NO inheriting players so it must not
        # change the real player's inheritance above.
        ghost_team = TeamQuarterlyScore.objects.get(ghost_team=self.ghostteam, season=self.season)
        self.assertEqual(ghost_team.tier_assigned, 0)
        # Ghost player -> always individual tier (never attached); 5 pts -> Entry (3).
        ghost_player = PlayerQuarterlyScore.objects.get(ghost_player=self.ghostplayer, season=self.season)
        self.assertEqual(ghost_player.tier_source, "individual", "a ghost player is never attached")
        self.assertEqual(ghost_player.tier_assigned, 3)
        self.assertIsNone(ghost_player.team_at_evaluation, "a ghost player has no team_at_evaluation")


# ═════════════════════════ REGRESSION (mandatory): no-ghost output identical ═════════════════════════
class NoGhostRegressionTests(TestCase):
    """With NO ghost rows present, every read endpoint + rerank + run_evaluation must produce the
    EXACT real-entity-only result it produced before the ghost feed existed. These guard against the
    Coalesce tiebreak / dropped filters / nullable player accidentally changing real output."""

    def setUp(self):
        self.owner = User.objects.create(username="regowner", email="reg@x.com")
        self.season = _make_season()
        self.teamA = _make_team("Reg Alpha", self.owner)   # higher score -> rank 1
        self.teamB = _make_team("Reg Bravo", self.owner)   # lower score -> rank 2
        self.playerA = User.objects.create(username="RegPA", email="rpa@x.com")
        self.playerB = User.objects.create(username="RegPB", email="rpb@x.com")

        # Real monthly + quarterly rows, no ghosts anywhere.
        TeamMonthlyScore.objects.create(team=self.teamA, month=PLAYED_MONTH, total_score=100, tournaments_played=2)
        TeamMonthlyScore.objects.create(team=self.teamB, month=PLAYED_MONTH, total_score=60, tournaments_played=1)
        TeamQuarterlyScore.objects.create(
            team=self.teamA, season=self.season, total_score=180,
            participated_in_tournaments=3, meets_participation_floor=True,
        )
        TeamQuarterlyScore.objects.create(
            team=self.teamB, season=self.season, total_score=50,
            participated_in_tournaments=2, meets_participation_floor=True,
        )
        PlayerMonthlyScore.objects.create(player=self.playerA, month=PLAYED_MONTH, total_score=40, total_kills=10)
        PlayerMonthlyScore.objects.create(player=self.playerB, month=PLAYED_MONTH, total_score=20, total_kills=4)
        PlayerQuarterlyScore.objects.create(
            player=self.playerA, season=self.season, total_score=95,
            participated_in_tournaments=2, meets_participation_floor=True,
        )
        PlayerQuarterlyScore.objects.create(
            player=self.playerB, season=self.season, total_score=30,
            participated_in_tournaments=1, meets_participation_floor=True,
        )

    def test_rerank_team_month_real_only_unchanged(self):
        recalc.rerank_team_month(PLAYED_MONTH)
        a = TeamMonthlyScore.objects.get(team=self.teamA, month=PLAYED_MONTH)
        b = TeamMonthlyScore.objects.get(team=self.teamB, month=PLAYED_MONTH)
        self.assertEqual(a.rank, 1, "higher-scoring real team ranks 1 (unchanged by the Coalesce tiebreak)")
        self.assertEqual(b.rank, 2)

    def test_rerank_team_quarter_real_only_unchanged(self):
        recalc.rerank_team_quarter(self.season)
        a = TeamQuarterlyScore.objects.get(team=self.teamA, season=self.season)
        b = TeamQuarterlyScore.objects.get(team=self.teamB, season=self.season)
        self.assertEqual(a.rank, 1)
        self.assertEqual(b.rank, 2)

    def test_rerank_player_month_real_only_unchanged(self):
        recalc.rerank_player_month(PLAYED_MONTH)
        a = PlayerMonthlyScore.objects.get(player=self.playerA, month=PLAYED_MONTH)
        b = PlayerMonthlyScore.objects.get(player=self.playerB, month=PLAYED_MONTH)
        self.assertEqual(a.rank, 1)
        self.assertEqual(b.rank, 2)

    def test_rerank_player_quarter_real_only_unchanged(self):
        recalc.rerank_player_quarter(self.season)
        a = PlayerQuarterlyScore.objects.get(player=self.playerA, season=self.season)
        b = PlayerQuarterlyScore.objects.get(player=self.playerB, season=self.season)
        self.assertEqual(a.rank, 1)
        self.assertEqual(b.rank, 2)

    def test_teams_monthly_endpoint_real_only_unchanged(self):
        # Monthly standings are gated on rankings_published (owner 2026-06-16); publish so the
        # public endpoint returns the two real rows.
        self.season.rankings_published = True
        self.season.save()
        recalc.rerank_team_month(PLAYED_MONTH)
        resp = self.client.get(reverse("rankings_teams_monthly"), {"month": PLAYED_MONTH_PARAM})
        rows = resp.json()["results"]
        self.assertEqual(len(rows), 2, "exactly the two real teams, no ghost row injected")
        self.assertFalse(any(r["is_ghost"] for r in rows))
        self.assertEqual([r["team_name"] for r in rows], ["Reg Alpha", "Reg Bravo"])
        self.assertEqual([r["rank"] for r in rows], [1, 2])

    def test_players_monthly_endpoint_real_only_unchanged(self):
        # Monthly standings are gated on rankings_published (owner 2026-06-16); publish so the
        # public endpoint returns the two real rows.
        self.season.rankings_published = True
        self.season.save()
        recalc.rerank_player_month(PLAYED_MONTH)
        resp = self.client.get(reverse("rankings_players_monthly"), {"month": PLAYED_MONTH_PARAM})
        rows = resp.json()["results"]
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(r["is_ghost"] for r in rows))
        self.assertEqual([r["username"] for r in rows], ["RegPA", "RegPB"])
        self.assertEqual([r["rank"] for r in rows], [1, 2])

    def test_run_evaluation_real_only_unchanged(self):
        result = run_evaluation(self.season, user=self.owner)
        self.assertTrue(result["ok"])
        # Two real teams + two real players evaluated, zero ghosts present.
        self.assertEqual(result["teams_evaluated"], 2)
        self.assertEqual(result["players_evaluated"], 2)
        a = TeamQuarterlyScore.objects.get(team=self.teamA, season=self.season)
        b = TeamQuarterlyScore.objects.get(team=self.teamB, season=self.season)
        # 180 -> Elite(0); 50 -> Rising(2). Identical to the pre-ghost engine output.
        self.assertEqual(a.tier_assigned, 0)
        self.assertEqual(b.tier_assigned, 2)
