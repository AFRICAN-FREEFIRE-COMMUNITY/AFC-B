"""
afc_rankings.test_tier_top_n - tiering by POSITION instead of by score.

Owner request, 2026-08-03: "can there be a new criteria we can set where tier 1 teams are the
top N teams on the tier at the end of the season?"

Today a team reaches Tier 1 by clearing an absolute score (>= 150). This adds the alternative:
the tiers are fixed SIZES and the scores only decide the order. It is a per-config CHOICE
(``tier_thresholds.mode``), season-scoped like every other setting, and the score mode stays
the default, so nothing changes for anyone who does not opt in.

The owner's one sentence does not settle four cases, and a team WILL ask about each of them.
Every one is answered here, deliberately, and pinned so the answer cannot drift:

  1. A TIE ON THE LAST PLACE IN A TIER -> every tied team goes UP.
     If Tier 1 is the top 10 and the 10th and 11th teams scored the same, both are Tier 1.
     Two teams on an identical score cannot be given different tiers for a whole season on
     the strength of an alphabetical tiebreak. The count is a minimum size, not a maximum.

  2. THE PARTICIPATION FLOOR IS APPLIED FIRST, BEFORE THE COUNT IS TAKEN.
     A team that has not met the floor is not eligible and does not occupy a place, so "the
     top 10" is the top 10 teams that qualify to be ranked at all. Counting first would let a
     floor-failing team burn a Tier 1 place and hand it to nobody.

  3. MID-SEASON IT IS PROVISIONAL, OFF THE LIVE LADDER.
     "At the end of the season" is when it is final, but the ladder is tiered continuously in
     every mode, so top-N is applied continuously too and moves as results land. The locked,
     end-of-season answer is still whatever ``run_evaluation`` stamps - the existing contract.
     Refusing to tier until evaluation would leave the site showing score-based tiers all
     season and different ones at the end, which is the more surprising outcome, not the less.

  4. COUNTS THAT DO NOT COVER THE LADDER -> the rest fall to the default tier.
     The same fall-through score mode already uses for a team below every cutoff.

HOW IT CONNECTS
    The rules live in ``afc_rankings/scoring/engine.assign_tiers_top_n`` (pure, no Django) and
    are applied to a real ladder by ``afc_rankings/recalc.rerank_team_quarter`` /
    ``rerank_player_quarter`` and locked by ``recalc.run_evaluation``. The mode and the sizes
    are authored through the same admin editor as every other scoring number
    (``afc_rankings/admin_scoring_config``), so the fixtures here reuse the helpers in
    ``test_scoring_config_editable``.
"""
import copy

from django.test import TestCase
from django.urls import reverse

from afc_team.models import Team
from afc_rankings import aggregation, recalc
from afc_rankings.models import (
    ScoringConfig, SeasonScoringConfig, TeamQuarterlyScore,
)
from afc_rankings.scoring import engine
from afc_rankings.scoring.tables import defaults_config, tables_from_config
from afc_rankings.scoring.validation import validate_config

# The season/user/token fixtures are the ones the editable-config suite already uses, so both
# suites drive the endpoints the same way. _ScoredFixture defines no test methods of its own,
# so importing it here does not re-run anything.
from afc_rankings.test_scoring_config_editable import (
    REASON, _ScoredFixture, _bearer, _current_season_dates, _season, _user_with_role,
)


def top_n_config(counts, *, mode="top_n"):
    """The shipped defaults, switched to top-N with a size for each tier.

    ``counts`` is (tier_0_size, tier_1_size, tier_2_size), in the order the tiers are listed.
    The score cutoffs are left exactly as they ship, which is the point: both columns live on
    the SAME tier rows, so switching back to score mode finds the cutoffs still there.
    """
    config = copy.deepcopy(defaults_config())
    config["tier_thresholds"]["mode"] = mode
    for row, count in zip(config["tier_thresholds"]["brackets"], counts):
        row["count"] = count
    return config


# ═════════════════════════ the rules themselves ═════════════════════════
class TopNEngineTests(TestCase):
    """Driven straight against the pure engine - no database, no scoring, no HTTP.

    These are decisions about ORDER and COUNTING. Asserting them on a ladder built out of real
    match results would bury the rule under the arithmetic that produced the scores, and an
    exact boundary tie is very hard to construct out of placements and kills.
    """

    def _tables(self, counts):
        return tables_from_config(top_n_config(counts))

    def _entries(self, *pairs):
        """(score, meets_floor) pairs, given in ladder order. Keys are their index."""
        return [engine.LadderEntry(key=i, score=score, meets_floor=floor)
                for i, (score, floor) in enumerate(pairs)]

    def test_top_n_puts_exactly_n_teams_in_tier_1(self):
        tables = self._tables((3, 2, 2))
        entries = self._entries(*[(100 - i, True) for i in range(10)])
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual(sorted(k for k, t in tiers.items() if t == 0), [0, 1, 2])

    def test_every_team_on_the_ladder_gets_an_answer(self):
        tables = self._tables((2, 2, 2))
        entries = self._entries(*[(50 - i, True) for i in range(9)])
        self.assertEqual(set(engine.assign_tiers_top_n(entries, tables)),
                         {e.key for e in entries})

    # ── decision 1: the boundary tie ──
    def test_a_tie_on_the_last_place_promotes_every_tied_team(self):
        tables = self._tables((2, 2, 2))
        entries = self._entries((90, True), (50, True), (50, True), (40, True), (10, True))
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual(tiers[1], 0)
        self.assertEqual(tiers[2], 0, "the team tied for the last Tier 1 place is promoted")
        self.assertEqual(sum(1 for t in tiers.values() if t == 0), 3, "so the tier runs long")

    def test_a_tie_never_straddles_two_tiers(self):
        """The corollary of promoting ties: once the tied group is absorbed upward the next
        tier starts on a strictly lower score, so no team can point at an equal team above it."""
        tables = self._tables((1, 2, 2))
        entries = self._entries((50, True), (50, True), (50, True), (20, True))
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual([tiers[i] for i in range(3)], [0, 0, 0])
        self.assertEqual(tiers[3], 1)

    def test_two_scores_a_rounding_error_apart_count_as_tied(self):
        """Quarterly scores are sums of floats. Two teams that genuinely earned the same
        points can differ in the last bit, and a bare == would split a real tie."""
        tables = self._tables((1, 1, 1))
        entries = self._entries((50.0, True), (50.0 + 1e-12, True), (10, True))
        self.assertEqual(engine.assign_tiers_top_n(entries, tables)[1], 0)

    def test_a_score_genuinely_below_the_boundary_is_not_promoted(self):
        tables = self._tables((1, 1, 1))
        entries = self._entries((50, True), (49.9, True), (10, True))
        self.assertEqual(engine.assign_tiers_top_n(entries, tables)[1], 1)

    # ── decision 2: the participation floor ──
    def test_a_team_below_the_floor_takes_no_place_and_gets_the_default_tier(self):
        tables = self._tables((2, 2, 2))
        entries = self._entries((90, True), (80, False), (70, True), (60, True))
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual(tiers[1], tables.tier_default, "no tier for a team below the floor")
        self.assertEqual([tiers[0], tiers[2]], [0, 0], "the place it did not take goes on")
        self.assertEqual(sum(1 for t in tiers.values() if t == 0), 2, "still exactly 2")

    def test_the_floor_outranks_any_score(self):
        tables = self._tables((1, 1, 1))
        entries = self._entries((10, True), (999, False))
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual(tiers[0], 0)
        self.assertEqual(tiers[1], tables.tier_default)

    # ── decision 4: counts that do not cover the ladder ──
    def test_teams_past_the_last_count_fall_to_the_default_tier(self):
        tables = self._tables((1, 1, 1))
        entries = self._entries(*[(100 - i, True) for i in range(6)])
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual([tiers[i] for i in range(6)], [0, 1, 2, 3, 3, 3])

    def test_counts_bigger_than_the_ladder_simply_run_out(self):
        """Not an error: a small season leaves the lower tiers empty rather than failing."""
        tables = self._tables((5, 5, 5))
        tiers = engine.assign_tiers_top_n(self._entries((90, True), (80, True)), tables)
        self.assertEqual(sorted(tiers.values()), [0, 0])

    def test_an_empty_ladder_is_not_an_error(self):
        self.assertEqual(engine.assign_tiers_top_n([], self._tables((3, 3, 3))), {})

    def test_a_tier_sized_zero_is_skipped_without_shifting_anyone_else(self):
        tables = self._tables((2, 0, 2))
        entries = self._entries(*[(100 - i, True) for i in range(5)])
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual([tiers[i] for i in range(5)], [0, 0, 2, 2, 3])

    def test_an_unset_size_leaves_that_tier_empty_rather_than_crashing(self):
        """Validation refuses to SAVE an unset size in top-N mode; this is the fail-soft path
        for a config that reached the engine anyway, e.g. a row edited by hand in the DB."""
        tables = tables_from_config(top_n_config((2, None, 2)))
        entries = self._entries(*[(100 - i, True) for i in range(5)])
        tiers = engine.assign_tiers_top_n(entries, tables)
        self.assertEqual([tiers[i] for i in range(5)], [0, 0, 2, 2, 3])

    # ── the mode is a choice, and the other choice is untouched ──
    def test_score_mode_is_the_default_and_still_tiers_by_score(self):
        tables = tables_from_config(defaults_config())
        self.assertEqual(tables.tier_mode, engine.TIER_MODE_THRESHOLD)
        self.assertEqual(engine.assign_tier(150, True, tables), 0)
        self.assertEqual(engine.assign_tier(149.99, True, tables), 1)
        self.assertEqual(engine.assign_tier(90, True, tables), 1)
        self.assertEqual(engine.assign_tier(40, True, tables), 2)
        self.assertEqual(engine.assign_tier(0, True, tables), 3)
        self.assertEqual(engine.assign_tier(999, False, tables), 3)

    def test_switching_the_mode_keeps_the_score_cutoffs(self):
        """Both columns live on the same rows, so a mode is never a one-way door."""
        self.assertEqual(tables_from_config(top_n_config((3, 3, 3))).tier_thresholds,
                         tables_from_config(defaults_config()).tier_thresholds)

    def test_a_config_saved_before_this_feature_reads_back_as_score_mode(self):
        """Every blob already in production carries neither key."""
        config = copy.deepcopy(defaults_config())
        config["tier_thresholds"].pop("mode")
        for row in config["tier_thresholds"]["brackets"]:
            row.pop("count")
        tables = tables_from_config(config)
        self.assertEqual(tables.tier_mode, engine.TIER_MODE_THRESHOLD)
        self.assertEqual(tables.tier_thresholds,
                         tables_from_config(defaults_config()).tier_thresholds)


# ═════════════════════════ what the editor may save ═════════════════════════
class TopNValidationTests(TestCase):
    def setUp(self):
        self.admin, self.token = _user_with_role("topn_head", "head_admin")

    def _codes(self, config):
        return {e["code"] for e in validate_config(config)["errors"]}

    def _kinds(self, config):
        return {c["kind"] for c in validate_config(config)["contradictions"]}

    def test_a_tier_with_no_size_is_refused(self):
        """Left unset the tier would silently hold nobody, and the admin who just switched
        modes would see Tier 1 empty with nothing telling them why."""
        self.assertIn("missing_tier_count", self._codes(top_n_config((10, None, None))))

    def test_a_fractional_size_is_refused(self):
        self.assertIn("not_an_integer", self._codes(top_n_config((10, 2.5, 5))))

    def test_a_negative_size_is_refused(self):
        self.assertIn("out_of_range", self._codes(top_n_config((10, -1, 5))))

    def test_an_unknown_mode_is_refused(self):
        self.assertIn("unknown_tier_mode",
                      self._codes(top_n_config((1, 1, 1), mode="whatever_the_ui_sent")))

    def test_a_size_of_zero_is_allowed_but_reported(self):
        checked = validate_config(top_n_config((10, 0, 5)))
        self.assertEqual(checked["errors"], [])
        self.assertIn("empty_tier", {c["kind"] for c in checked["contradictions"]})

    def test_the_shipped_defaults_are_still_valid(self):
        self.assertEqual(validate_config(defaults_config()),
                         {"errors": [], "contradictions": []})

    def test_unreachable_cutoffs_do_not_block_a_top_n_config(self):
        """The 'no team could ever leave the default tier' refusal asks whether any team can
        clear the lowest cutoff. In top-N nobody has to: the tiers fill by position, so a
        ladder of low scores still produces a full Tier 1."""
        config = top_n_config((10, 20, 30))
        # Every cutoff put out of reach, so even the LOWEST one is unattainable - that is the
        # condition the refusal tests.
        for index, row in enumerate(config["tier_thresholds"]["brackets"]):
            row["min"] = 10_000_000 * (len(config["tier_thresholds"]["brackets"]) - index)
        self.assertEqual(validate_config(config)["errors"], [])

        as_score_mode = copy.deepcopy(config)
        as_score_mode["tier_thresholds"]["mode"] = engine.TIER_MODE_THRESHOLD
        self.assertIn("unreachable_scale", self._codes(as_score_mode))

    def test_out_of_order_cutoffs_are_not_reported_while_they_are_dormant(self):
        """Warning about a number that is not in force is noise, and noise hides the warnings
        that are. The same config in score mode is still reported."""
        config = top_n_config((10, 20, 30))
        config["tier_thresholds"]["brackets"] = [
            {"min": 150, "tier": 0, "count": 10},
            {"min": 150, "tier": 1, "count": 20},
            {"min": 40, "tier": 2, "count": 30},
        ]
        self.assertNotIn("unreachable_tier_cutoff", self._kinds(config))

        as_score_mode = copy.deepcopy(config)
        as_score_mode["tier_thresholds"]["mode"] = engine.TIER_MODE_THRESHOLD
        self.assertIn("unreachable_tier_cutoff", self._kinds(as_score_mode))

    def test_sizing_the_same_tier_twice_is_reported(self):
        config = top_n_config((10, 20, 30))
        config["tier_thresholds"]["brackets"][1]["tier"] = 0
        self.assertIn("duplicate_tier_size", self._kinds(config))

    def test_a_valid_top_n_config_saves_and_reads_back(self):
        response = self.client.post(
            reverse("rankings_scoring_config"),
            {"config": top_n_config((10, 20, 30)), "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 201, response.content)
        stored = ScoringConfig.objects.get(version=1).config["tier_thresholds"]
        self.assertEqual(stored["mode"], "top_n")
        self.assertEqual([row["count"] for row in stored["brackets"]], [10, 20, 30])

    def test_a_config_with_no_size_is_refused_at_the_endpoint_and_writes_nothing(self):
        response = self.client.post(
            reverse("rankings_scoring_config"),
            {"config": top_n_config((10, None, None)), "reason": REASON},
            content_type="application/json", **_bearer(self.token),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ScoringConfig.objects.count(), 0)

    def test_the_editor_is_told_which_modes_exist(self):
        """So the UI renders the mode picker from the API rather than hardcoding the strings,
        and can label which column of the tier rows is live."""
        body = self.client.get(reverse("rankings_scoring_config"),
                               **_bearer(self.token)).json()
        modes = body["field_meta"]["tier_thresholds"]["modes"]
        self.assertEqual({m["value"]: m["column"] for m in modes},
                         {"threshold": "min", "top_n": "count"})


# ═════════════════════════ applied to a real ladder ═════════════════════════
class _LadderFixture(TestCase):
    """A season with a hand-seeded team ladder.

    The scores are written straight onto TeamQuarterlyScore rather than earned from match
    results. Deliberate: these tests are about which ROW gets which tier for a given order,
    and the end-to-end path from match results to a score is already covered by
    test_scoring_config_editable.ConfigReachesScoringTests.
    """

    def setUp(self):
        self.admin, self.admin_token = _user_with_role("ladder_head", "head_admin")
        quarter, start, end = _current_season_dates()
        self.season = _season(f"Season {quarter} {start.year}", start.year, quarter,
                              start, end, active=True)

    def _bind(self, config):
        """Pin a scoring config to this season, as a save with it in scope would."""
        cfg = ScoringConfig.objects.create(version=1, is_active=True, config=config)
        SeasonScoringConfig.objects.update_or_create(
            season=self.season,
            defaults={"config": cfg, "origin": SeasonScoringConfig.APPLIED},
        )
        return cfg

    def _team(self, name, score, *, floor=True, **extra):
        team = Team.objects.create(
            team_name=name, join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        TeamQuarterlyScore.objects.create(
            team=team, season=self.season, total_score=score, tournament_pts=score,
            participated_in_tournaments=5 if floor else 0,
            meets_participation_floor=floor, **extra,
        )
        return team

    def _read(self):
        return {r.team.team_name: r.tier_assigned
                for r in TeamQuarterlyScore.objects.filter(season=self.season)
                                                   .select_related("team")}

    def _tiers(self):
        """{team_name: tier} after the LADDER PASS - the provisional, live answer.

        Only top-N writes tiers here; in score mode the ladder pass renumbers ranks and
        nothing else (the tier comes from each team's own score, in recalc_team_quarterly),
        which is exactly what ScoreModeUnchangedTests asserts.
        """
        recalc.rerank_team_quarter(self.season)
        return self._read()

    def _evaluated_tiers(self):
        """{team_name: tier} after the end-of-season evaluation - the locked answer."""
        recalc.run_evaluation(self.season, user=self.admin, recompute=False)
        return self._read()


class TopNLadderTests(_LadderFixture):
    """Top-N through recalc, on rows the site actually renders."""

    def setUp(self):
        super().setUp()
        # Tier 1 = top 2, Tier 2 = the next 2, Tier 3 = the next 1, the rest default (4th tier).
        self._bind(top_n_config((2, 2, 1)))

    def test_exactly_n_teams_are_placed_in_tier_1(self):
        for i in range(8):
            self._team(f"T{i}", 100 - i)
        tiers = self._tiers()
        self.assertEqual(sorted(name for name, t in tiers.items() if t == 0), ["T0", "T1"])

    def test_the_tiers_follow_the_printed_rank(self):
        """A team shown as rank 3 must never be holding a Tier 1 place. Rank and tier are
        computed from the one ladder query (recalc.team_quarter_ladder) for exactly this
        reason - if they read different orders the site could not explain either number."""
        for i in range(6):
            self._team(f"T{i}", 100 - i)
        recalc.rerank_team_quarter(self.season)
        rows = list(TeamQuarterlyScore.objects.filter(season=self.season).order_by("rank"))
        self.assertEqual([r.rank for r in rows], [1, 2, 3, 4, 5, 6])
        self.assertEqual([r.tier_assigned for r in rows], [0, 0, 1, 1, 2, 3])

    def test_a_tie_on_the_boundary_promotes_both_teams(self):
        self._team("Alpha", 100)
        self._team("Bravo", 50)
        self._team("Charlie", 50)     # tied with Bravo for the last Tier 1 place
        self._team("Delta", 10)
        tiers = self._tiers()
        self.assertEqual([tiers["Bravo"], tiers["Charlie"]], [0, 0])
        self.assertEqual(tiers["Delta"], 1, "the next tier starts below the tied group")

    def test_a_team_below_the_participation_floor_does_not_take_a_place(self):
        self._team("Alpha", 100)
        self._team("Tourist", 95, floor=False)   # 2nd on score alone
        self._team("Bravo", 90)
        self._team("Charlie", 80)
        tiers = self._tiers()
        self.assertEqual(tiers["Tourist"], 3, "the default tier, whatever it scored")
        self.assertEqual([tiers["Alpha"], tiers["Bravo"]], [0, 0])
        self.assertEqual(tiers["Charlie"], 1)

    def test_the_rest_of_the_ladder_falls_to_the_default_tier(self):
        for i in range(9):
            self._team(f"T{i}", 100 - i)
        tiers = self._tiers()
        self.assertEqual([tiers[f"T{i}"] for i in range(9)], [0, 0, 1, 1, 2, 3, 3, 3, 3])

    def test_a_banned_team_keeps_its_tier_and_takes_no_place(self):
        """§2.15: an unrelated recalc must never quietly un-ban a team. It is also not
        competing, so it must not hold a Tier 1 place open either."""
        self._team("Alpha", 100)
        self._team("Banned", 0, is_zeroed=True, tier_assigned=3, zeroed_reason="ban")
        self._team("Bravo", 90)
        self._team("Charlie", 80)
        tiers = self._tiers()
        self.assertEqual(tiers["Banned"], 3, "untouched")
        self.assertEqual([tiers["Alpha"], tiers["Bravo"]], [0, 0])

    def test_an_overridden_tier_is_not_stomped_and_takes_no_place(self):
        self._team("Alpha", 100)
        self._team("Locked", 95, tier_overridden=True, tier_assigned=1,
                   tier_override_reason="admin decision")
        self._team("Bravo", 90)
        self._team("Charlie", 80)
        tiers = self._tiers()
        self.assertEqual(tiers["Locked"], 1, "the admin's decision stands")
        self.assertEqual([tiers["Alpha"], tiers["Bravo"]], [0, 0])

    def test_a_deduction_moves_a_team_down_the_ladder_and_out_of_its_tier(self):
        """Top-N reads the same EFFECTIVE score the rank does (§16), so a penalty costs a
        place rather than only lowering a displayed number. Penalised would be 2nd on its
        raw 95; on its effective 5 it is last."""
        self._team("Alpha", 100)
        self._team("Penalised", 95, points_deducted=90)
        self._team("Bravo", 90)
        self._team("Charlie", 80)
        self._team("Delta", 70)
        self._team("Echo", 60)
        tiers = self._tiers()
        self.assertEqual([tiers["Alpha"], tiers["Bravo"]], [0, 0])
        self.assertEqual([tiers["Charlie"], tiers["Delta"]], [1, 1])
        self.assertEqual(tiers["Penalised"], 3, "last on the ladder, so past every count")

    def test_the_ladder_retiers_as_results_land(self):
        """Decision 3: mid-season the answer is provisional and follows the live ladder. A
        new result that outscores the current Tier 1 pushes a team out, immediately."""
        self._team("Alpha", 100)
        self._team("Bravo", 90)
        self.assertEqual([self._tiers()["Alpha"], self._tiers()["Bravo"]], [0, 0])

        self._team("Latecomer", 200)
        tiers = self._tiers()
        self.assertEqual([tiers["Latecomer"], tiers["Alpha"]], [0, 0])
        self.assertEqual(tiers["Bravo"], 1, "pushed out of the top 2 by a better team")

    def test_evaluation_locks_the_tiers_the_ladder_was_already_showing(self):
        """The provisional answer and the end-of-season answer are the same computation over
        the same order, so evaluation confirms the ladder rather than rearranging it."""
        for i in range(6):
            self._team(f"T{i}", 100 - i)
        before = self._tiers()

        summary = recalc.run_evaluation(self.season, user=self.admin, recompute=False)
        self.assertTrue(summary.get("ok", True), summary)

        rows = list(TeamQuarterlyScore.objects.filter(season=self.season)
                                              .select_related("team"))
        self.assertEqual({r.team.team_name: r.tier_assigned for r in rows}, before)
        for row in rows:
            self.assertIsNotNone(row.tier_assigned_at, "the tier is now locked")

    def test_evaluation_respects_the_floor_and_the_counts_too(self):
        self._team("Alpha", 100)
        self._team("Tourist", 95, floor=False)
        self._team("Bravo", 90)
        recalc.run_evaluation(self.season, user=self.admin, recompute=False)
        tiers = {r.team.team_name: r.tier_assigned
                 for r in TeamQuarterlyScore.objects.filter(season=self.season)
                                                    .select_related("team")}
        self.assertEqual(tiers, {"Alpha": 0, "Bravo": 0, "Tourist": 3})


class ScoreModeUnchangedTests(_LadderFixture):
    """The same ladder under the shipped mode. Nothing about it may have moved."""

    def setUp(self):
        super().setUp()
        self._bind(copy.deepcopy(defaults_config()))

    def test_the_ladder_pass_writes_ranks_and_leaves_tiers_alone(self):
        """The regression that matters most: under the default mode the new ladder-wide
        tiering must not run at all. A tier is still each team's own business, decided from
        its own score in recalc_team_quarterly and locked at evaluation."""
        self._team("Elite", 200)
        self._team("Entry", 5)
        self.assertEqual(set(self._tiers().values()), {None},
                         "reranking did not invent a tier")
        rows = TeamQuarterlyScore.objects.filter(season=self.season).order_by("rank")
        self.assertEqual([r.rank for r in rows], [1, 2])

    def test_tiers_come_from_the_score_not_the_position(self):
        self._team("Elite", 200)        # >= 150
        self._team("AlsoElite", 160)    # >= 150 too, which top-N with a count of 1 would cap
        self._team("Competitive", 100)  # 90-149
        self._team("Rising", 50)        # 40-89
        self._team("Entry", 5)          # < 40
        self.assertEqual(self._evaluated_tiers(),
                         {"Elite": 0, "AlsoElite": 0, "Competitive": 1,
                          "Rising": 2, "Entry": 3})

    def test_a_ladder_where_nobody_clears_the_cutoff_has_no_tier_1(self):
        """The behaviour top-N exists to offer an alternative to. Pinned so the difference
        between the two modes stays visible in the suite."""
        for i in range(5):
            self._team(f"T{i}", 30 - i)
        self.assertEqual(set(self._evaluated_tiers().values()), {3})

    def test_the_floor_still_sends_a_high_scorer_to_the_default_tier(self):
        self._team("Tourist", 500, floor=False)
        self.assertEqual(self._evaluated_tiers(), {"Tourist": 3})


class NoConfigAtAllTests(_LadderFixture):
    """No ScoringConfig row anywhere: the shipped constants govern, in score mode."""

    def test_the_shipped_defaults_still_apply(self):
        self._team("Elite", 200)
        self._team("Entry", 5)
        self.assertEqual(self._evaluated_tiers(), {"Elite": 0, "Entry": 3})
        self.assertEqual(aggregation.resolve_tables(season=self.season).tier_mode,
                         engine.TIER_MODE_THRESHOLD)


# ═════════════════════════ the mode obeys the season rules ═════════════════════════
class TopNSeasonScopeTests(_ScoredFixture):
    """Switching modes is a config change like any other, so it is not retroactive either.

    _ScoredFixture gives one team with a real tournament result in a CLOSED season and in the
    CURRENT one, both already scored, so these assertions compare real numbers.
    """

    def setUp(self):
        super().setUp()
        # A SECOND result in each season. The team quarterly participation floor is 2
        # tournaments, and a team below the floor gets the default tier in BOTH modes - so
        # with the single result _ScoredFixture provides, switching modes could not change
        # anything and these tests would pass without proving a thing.
        self._result(self.closed_play_day, "Old Cup II")
        self._result(self.current_play_day, "New Cup II")
        recalc.recalc_season(self.closed)
        recalc.recalc_season(self.current)
        for season in (self.closed, self.current):
            self.assertTrue(
                TeamQuarterlyScore.objects.get(team=self.team, season=season)
                .meets_participation_floor,
                "fixture must clear the participation floor or the mode cannot matter",
            )

    def _top_n_save(self, **body):
        return self._save(config=top_n_config((1, 1, 1)), **body)

    def test_switching_to_top_n_does_not_alter_a_closed_season(self):
        before_score = self._score(self.closed)
        before_tier = TeamQuarterlyScore.objects.get(
            team=self.team, season=self.closed).tier_assigned

        self.assertEqual(self._top_n_save().status_code, 201)
        recalc.recalc_season(self.closed)   # force it, the way an unrelated edit would

        row = TeamQuarterlyScore.objects.get(team=self.team, season=self.closed)
        self.assertEqual(row.total_score, before_score)
        self.assertEqual(row.tier_assigned, before_tier,
                         "a closed season keeps the mode it was scored under")

    def test_each_season_resolves_its_own_mode(self):
        self._top_n_save()
        self.assertIsNone(SeasonScoringConfig.objects.get(season=self.closed).config)
        self.assertEqual(aggregation.resolve_tables(season=self.closed).tier_mode,
                         engine.TIER_MODE_THRESHOLD)
        self.assertEqual(aggregation.resolve_tables(season=self.current).tier_mode,
                         engine.TIER_MODE_TOP_N)

    def test_the_current_season_moves_to_top_n_and_is_retiered(self):
        """One team, which has played enough to qualify, so it is the whole ladder - and the
        top 1 of it. Under the score cutoffs the same team is nowhere near Tier 1, which is
        what makes this assertion prove the mode reached the scoring."""
        row = TeamQuarterlyScore.objects.get(team=self.team, season=self.current)
        self.assertNotEqual(row.tier_assigned, 0, "not Tier 1 on score")

        self.assertEqual(self._top_n_save().status_code, 201)
        row.refresh_from_db()
        self.assertEqual(row.tier_assigned, 0, "top of the ladder, whatever it scored")

    def test_switching_back_to_score_mode_restores_the_score_based_tier(self):
        self._top_n_save()
        self.assertEqual(TeamQuarterlyScore.objects.get(
            team=self.team, season=self.current).tier_assigned, 0)

        self.assertEqual(self._save(config=copy.deepcopy(defaults_config())).status_code, 201)
        row = TeamQuarterlyScore.objects.get(team=self.team, season=self.current)
        self.assertEqual(row.tier_assigned, engine.score_to_tier(row.total_score))
        self.assertNotEqual(row.tier_assigned, 0, "two small results are nowhere near 150")

    def test_choosing_the_closed_season_explicitly_does_switch_its_mode(self):
        before = TeamQuarterlyScore.objects.get(
            team=self.team, season=self.closed).tier_assigned
        self.assertNotEqual(before, 0)

        response = self._top_n_save(apply_to_seasons=[self.closed.season_id],
                                    acknowledge_published=True)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(TeamQuarterlyScore.objects.get(
            team=self.team, season=self.closed).tier_assigned, 0)

    def test_the_audit_entry_leads_back_to_the_mode_that_was_saved(self):
        """'Why did my tier change when my score did not' has to have an answer. The audit
        row names the version; the version carries the mode."""
        from afc_rankings.models import RankingAuditLog
        self._top_n_save()
        entry = RankingAuditLog.objects.get(object_type="scoring_config", action="save")
        saved = ScoringConfig.objects.get(version=entry.after_snapshot["version"])
        self.assertEqual(saved.config["tier_thresholds"]["mode"], "top_n")
        self.assertIsNone(entry.before_snapshot["version"], "nothing was saved before")
        self.assertIn(self.current.season_id,
                      [s["season_id"] for s in entry.after_snapshot["applied_seasons"]])
