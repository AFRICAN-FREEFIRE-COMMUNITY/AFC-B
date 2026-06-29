"""
test_evaluation_recompute.py
────────────────────────────
Covers the evaluation-recompute fix (owner bug 2026-06-29: "I ran evaluation and got nothing
even though results were inputted"). run_evaluation now rebuilds the season's quarterly SCORES
from match results (recalc_season) before tiering, on REAL runs only, so the button is
self-sufficient even when the async recalc pipeline never built the score rows.

Verified here (no tournament match-stats fixture needed — these assert the new CONTRACT):
  - real run sets recomputed=True; dry run sets recomputed=False (preview writes nothing).
  - an empty season returns a helpful `note` instead of a silent "0 tiered" success, and the
    note differs for a recomputed real run vs a non-recomputed dry run.
  - pre-seeded quarterly rows whose teams have NO match stats survive the recompute (recalc_season
    only touches teams/players that actually played) and still get tiered — i.e. the recompute is
    additive, never destructive to legitimately-present rows.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from afc_rankings import recalc
from afc_rankings.recalc import run_evaluation
from afc_rankings.models import TeamQuarterlyScore
# Reuse the shared season/team builders (same ones the ghost + standalone suites use).
from afc_rankings.test_standalone_feed import _make_season, _make_team

User = get_user_model()


class EvaluationRecomputeContractTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="evalowner", email="evalo@x.com")
        self.season = _make_season()

    def test_empty_season_real_run_recomputes_and_explains(self):
        # No scores, no match stats anywhere -> recompute runs (real), finds nothing, and says WHY.
        res = run_evaluation(self.season, user=self.owner)
        self.assertTrue(res["ok"])
        self.assertTrue(res["recomputed"], "a real run must recompute scores from results first")
        self.assertEqual(res["teams_evaluated"], 0)
        self.assertEqual(res["players_evaluated"], 0)
        self.assertIn("No tournament results", res["note"])  # not a silent empty success

    def test_dry_run_does_not_recompute(self):
        # A dry run is a pure preview: it must NOT recompute (writes nothing) and says so.
        res = run_evaluation(self.season, user=self.owner, dry_run=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["recomputed"], "dry run must not write/recompute")
        self.assertIn("Run a real evaluation", res["note"])

    def test_recompute_false_opt_out(self):
        # Callers/tests can tier existing rows as-is without a recompute.
        res = run_evaluation(self.season, user=self.owner, recompute=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["recomputed"])

    def test_seeded_rows_without_match_stats_survive_recompute_and_tier(self):
        # A quarterly row whose team has NO tournament match stats must NOT be wiped by the
        # recompute (recalc_season only recomputes teams that actually played) — it still tiers.
        team = _make_team("Seeded Alpha", self.owner)
        TeamQuarterlyScore.objects.create(
            team=team, season=self.season, total_score=180,
            participated_in_tournaments=3, meets_participation_floor=True,
        )
        res = run_evaluation(self.season, user=self.owner)
        self.assertTrue(res["ok"])
        self.assertTrue(res["recomputed"])
        self.assertEqual(res["teams_evaluated"], 1, "the seeded row survived recompute and was tiered")
        self.assertEqual(res["note"], "")  # something was tiered -> no empty-note
        row = TeamQuarterlyScore.objects.get(team=team, season=self.season)
        self.assertEqual(row.tier_assigned, 0)  # 180 -> Elite(0)
