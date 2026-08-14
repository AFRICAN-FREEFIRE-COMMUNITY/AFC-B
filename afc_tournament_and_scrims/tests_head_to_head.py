"""Tests for the Clash-Squad head-to-head bracket engine (bracket sub-projects C + D).

Covers, mirroring tests_round_robin.py's fixture idiom (User/SessionToken bearer handshake,
full-kwargs Event factory, Stages with a real CS stage_format):
  - single-elim generation for 4 teams (full tree, links) and 6 teams (byes for the top
    seeds, auto-advanced into the semis),
  - result reporting + winner advancement, tie refusal, the re-report window (allowed
    until a downstream match completes),
  - double-elim loser drops (winners bracket losers land in the losers bracket, grand
    final wiring) and full-playthrough placements,
  - league standings ordering (match wins -> round-win diff -> round wins),
  - the SUB-PROJECT D bridge: write_placement_stats writes synthetic
    TournamentTeamMatchStats rows (match_number=0 Match) that the EXISTING leaderboard
    aggregation (round_robin.cumulative_standings reads the same rows the leaderboard
    view sums) sees without any changes,
  - permissions (stranger 403, organizer of the owning org allowed, public GET), and
  - the regeneration guard (byes do not block, a real result does).

Run: venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_head_to_head
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_organizers.models import Organization, OrganizationMember
from afc_team.models import Team

from afc_tournament_and_scrims import head_to_head, round_robin
from afc_tournament_and_scrims.models import (
    Event,
    H2HPlayerStat,
    HeadToHeadMatch,
    Leaderboard,
    Match,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentTeamMember,
    TournamentPlayerMatchStats,
)


class H2HBase(TestCase):
    """Shared fixture: an admin with a live token, one event, one CS knockout stage, and
    six TournamentTeam rows named T1..T6 (T1 = strongest seed)."""

    STAGE_FORMAT = "cs - knockout"

    def setUp(self):
        self.client = Client()
        D = datetime.date(2026, 6, 1)

        # Admin + live session token so the admin gate (_is_event_admin) passes.
        self.admin = User.objects.create(
            username="h2h_admin", email="h2h_admin@afc.test",
            full_name="H2H Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="h2h-admin-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))

        self.event = Event.objects.create(
            event_name="CS Bracket Cup", competition_type="tournament",
            participant_type="squad", event_type="internal", max_teams_or_players=16,
            event_mode="virtual", start_date=D, end_date=D, registration_open_date=D,
            registration_end_date=D, prizepool="$1000", event_rules="rules",
            event_status="ongoing", registration_link="https://afc.test/reg",
            number_of_stages=1, creator=self.admin, is_draft=False)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Playoffs", start_date=D, end_date=D,
            number_of_groups=1, stage_format=self.STAGE_FORMAT,
            teams_qualifying_from_stage=4)

        # Six tournament teams in seed order: tts[0] = seed 1 ... tts[5] = seed 6.
        self.tts = [self._make_tt(f"T{i}") for i in range(1, 7)]

    # ── tiny fixture builders (mirror tests_round_robin.py) ──
    def _make_tt(self, name):
        team = Team.objects.create(
            team_name=name, join_settings="open", team_creator=self.admin,
            team_owner=self.admin, team_captain=self.admin, country="Nigeria")
        return TournamentTeam.objects.create(event=self.event, team=team)

    def _ids(self, count):
        return [tt.tournament_team_id for tt in self.tts[:count]]

    def _generate(self, team_ids, fmt=None, token=None, stage=None):
        payload = {"team_ids": team_ids}
        if fmt:
            payload["fmt"] = fmt
        stage = stage or self.stage
        return self.client.post(
            f"/events/stages/{stage.stage_id}/bracket/generate/",
            data=payload, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.token.token}")

    def _report(self, match, score_a, score_b, token=None):
        return self.client.post(
            f"/events/h2h-matches/{match.h2h_match_id}/result/",
            data={"score_a": score_a, "score_b": score_b},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.token.token}")

    def _get_bracket(self, stage=None):
        stage = stage or self.stage
        return self.client.get(f"/events/stages/{stage.stage_id}/bracket/")

    def _m(self, bracket, round_number, position, stage=None):
        return HeadToHeadMatch.objects.get(
            stage=stage or self.stage, bracket=bracket,
            round_number=round_number, position=position)


class SingleElimGenerationTests(H2HBase):
    """Bracket-tree shape for the knockout format (fmt derived from 'cs - knockout')."""

    def test_four_teams_full_tree(self):
        resp = self._generate(self._ids(4))
        self.assertEqual(resp.status_code, 201, resp.content)

        # 4 teams -> 2 round-1 matches + 1 final, all in the winners bracket, no byes.
        self.assertEqual(HeadToHeadMatch.objects.filter(stage=self.stage).count(), 3)
        m0, m1 = self._m("winners", 1, 0), self._m("winners", 1, 1)
        final = self._m("winners", 2, 0)

        # Standard seeding: slot order [1,4,2,3] -> match 0 = seed1 vs seed4,
        # match 1 = seed2 vs seed3 (1 and 2 can only meet in the final).
        self.assertEqual(m0.team_a_id, self.tts[0].tournament_team_id)
        self.assertEqual(m0.team_b_id, self.tts[3].tournament_team_id)
        self.assertEqual(m1.team_a_id, self.tts[1].tournament_team_id)
        self.assertEqual(m1.team_b_id, self.tts[2].tournament_team_id)

        # Advancement wiring: match p feeds final slot a/b by parity; final has no next.
        self.assertEqual((m0.next_match_id, m0.next_match_slot), (final.pk, "a"))
        self.assertEqual((m1.next_match_id, m1.next_match_slot), (final.pk, "b"))
        self.assertIsNone(final.next_match_id)
        # Everything pending: no byes in a power-of-2 field.
        self.assertEqual(
            HeadToHeadMatch.objects.filter(stage=self.stage, status="pending").count(), 3)

    def test_six_teams_get_byes_for_top_seeds(self):
        resp = self._generate(self._ids(6))
        self.assertEqual(resp.status_code, 201, resp.content)

        # 6 teams -> bracket size 8 -> 4 + 2 + 1 = 7 matches.
        self.assertEqual(HeadToHeadMatch.objects.filter(stage=self.stage).count(), 7)

        # Slot order for 8: [1,8,4,5,2,7,3,6]; seeds 7+8 don't exist, so the matches of
        # seeds 1 and 2 are byes (higher seeds get the byes) and auto-complete.
        bye0, bye2 = self._m("winners", 1, 0), self._m("winners", 1, 2)
        for bye, seed_tt in ((bye0, self.tts[0]), (bye2, self.tts[1])):
            self.assertEqual(bye.status, "completed")
            self.assertIsNone(bye.team_b_id)
            self.assertEqual(bye.winner_id, seed_tt.tournament_team_id)
            self.assertEqual((bye.score_a, bye.score_b), (0, 0))

        # The bye winners were auto-advanced into their semifinal slots.
        sf0, sf1 = self._m("winners", 2, 0), self._m("winners", 2, 1)
        self.assertEqual(sf0.team_a_id, self.tts[0].tournament_team_id)
        self.assertEqual(sf1.team_a_id, self.tts[1].tournament_team_id)
        # Their other slots wait on the real round-1 matches: 4v5 and 3v6.
        m_45, m_36 = self._m("winners", 1, 1), self._m("winners", 1, 3)
        self.assertEqual({m_45.team_a_id, m_45.team_b_id},
                         {self.tts[3].tournament_team_id, self.tts[4].tournament_team_id})
        self.assertEqual({m_36.team_a_id, m_36.team_b_id},
                         {self.tts[2].tournament_team_id, self.tts[5].tournament_team_id})
        self.assertEqual(m_45.status, "pending")
        self.assertEqual(m_36.status, "pending")

    def test_get_bracket_is_public_and_flags_byes(self):
        self._generate(self._ids(6))
        resp = self._get_bracket()  # NO Authorization header: public spectator read
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()

        self.assertEqual(body["fmt"], "single_elim")
        self.assertTrue(body["generated"])
        round1 = body["rounds"]["winners"][0]
        self.assertEqual(round1["round"], 1)
        self.assertEqual(len(round1["matches"]), 4)
        # The seed-1 bye is flagged for the FE renderer.
        first = round1["matches"][0]
        self.assertTrue(first["is_bye"])
        self.assertEqual(first["team_a"]["team_name"], "T1")
        self.assertIsNone(first["team_b"])
        # No losers/league rounds in a single-elim tree.
        self.assertEqual(body["rounds"]["losers"], [])
        self.assertEqual(body["rounds"]["league"], [])


class SingleElimReportingTests(H2HBase):
    """Result entry, advancement, tie refusal, and the re-report window (4-team tree)."""

    def setUp(self):
        super().setUp()
        self._generate(self._ids(4))
        self.m0 = self._m("winners", 1, 0)   # T1 vs T4
        self.m1 = self._m("winners", 1, 1)   # T2 vs T3
        self.final = self._m("winners", 2, 0)

    def test_report_advances_winner_into_final(self):
        resp = self._report(self.m0, 4, 2)  # T1 wins the set 4-2
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["match"]["winner_id"], self.tts[0].tournament_team_id)
        self.assertFalse(body["bracket_complete"])

        self.final.refresh_from_db()
        self.assertEqual(self.final.team_a_id, self.tts[0].tournament_team_id)
        self.assertIsNone(self.final.team_b_id)  # other semifinal not played yet

    def test_tie_is_refused_in_elimination(self):
        resp = self._report(self.m0, 3, 3)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Ties are not allowed", resp.json()["message"])
        self.m0.refresh_from_db()
        self.assertEqual(self.m0.status, "pending")

    def test_negative_score_refused(self):
        resp = self._report(self.m0, -1, 3)
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_absurdly_large_score_refused(self):
        # P2 sanity cap (owner 2026-07-13): a fat-finger "400-2" is rejected, not silently stored.
        resp = self._report(self.m0, 400, 2)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("too large", resp.json()["message"])
        self.m0.refresh_from_db()
        self.assertEqual(self.m0.status, "pending")

    def test_score_at_cap_is_accepted(self):
        # The boundary value (99) is still a legal set score.
        resp = self._report(self.m0, 99, 2)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_cannot_report_match_missing_a_team(self):
        # The final has no teams yet: reporting it must be refused.
        resp = self._report(self.final, 4, 0)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("does not have both teams", resp.json()["message"])

    def test_rereport_allowed_until_downstream_completes(self):
        # First report: T1 beats T4; re-report flips it to T4 - allowed, final not played.
        self.assertEqual(self._report(self.m0, 4, 2).status_code, 200)
        resp = self._report(self.m0, 1, 4)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.final.refresh_from_db()
        # The corrected winner OVERWRITES the slot the old winner occupied.
        self.assertEqual(self.final.team_a_id, self.tts[3].tournament_team_id)

        # Finish the bracket: other semi + final.
        self.assertEqual(self._report(self.m1, 4, 1).status_code, 200)  # T2 wins
        self.final.refresh_from_db()
        self.assertEqual(self._report(self.final, 4, 3).status_code, 200)  # T4 champion

        # Now the downstream (final) is completed: the semifinal is frozen.
        resp = self._report(self.m0, 4, 0)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("already completed", resp.json()["message"])

    def test_full_playthrough_standings(self):
        # T1 > T4, T3 > T2, final T1 > T3.
        self.assertEqual(self._report(self.m0, 4, 1).status_code, 200)
        self.assertEqual(self._report(self.m1, 2, 4).status_code, 200)
        self.final.refresh_from_db()
        resp = self._report(self.final, 4, 2)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["bracket_complete"])

        rows = {r["team_name"]: r for r in self._get_bracket().json()["standings"]}
        self.assertEqual(rows["T1"]["placement"], 1)   # champion
        self.assertEqual(rows["T3"]["placement"], 2)   # runner-up
        # Semifinal (round 1 here) losers share 3rd.
        self.assertEqual(rows["T4"]["placement"], 3)
        self.assertEqual(rows["T2"]["placement"], 3)
        self.assertEqual(rows["T1"]["wins"], 2)
        self.assertEqual(rows["T1"]["rounds_won"], 8)   # 4 + 4
        self.assertEqual(rows["T1"]["rounds_lost"], 3)  # 1 + 2


class RegenerationGuardTests(H2HBase):
    """Regenerate freely until a REAL result lands; auto-byes never block."""

    def test_byes_do_not_block_but_real_result_does(self):
        # 6-team field -> two auto-completed byes exist immediately...
        self.assertEqual(self._generate(self._ids(6)).status_code, 201)
        # ...and regeneration is still allowed (byes are not entered results).
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        self.assertEqual(HeadToHeadMatch.objects.filter(stage=self.stage).count(), 3)

        # Enter one real result -> the bracket is locked against regeneration.
        self.assertEqual(self._report(self._m("winners", 1, 0), 4, 0).status_code, 200)
        resp = self._generate(self._ids(4))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("no longer be regenerated", resp.json()["message"])

    def test_generate_validation(self):
        # Fewer than two teams.
        self.assertEqual(self._generate(self._ids(1)).status_code, 400)
        # Duplicate seeds.
        dup = self._ids(3) + [self.tts[0].tournament_team_id]
        self.assertEqual(self._generate(dup).status_code, 400)
        # A team id from another event.
        other_event = Event.objects.create(
            event_name="Other Cup", competition_type="tournament",
            participant_type="squad", event_type="internal", max_teams_or_players=16,
            event_mode="virtual", start_date=self.event.start_date,
            end_date=self.event.end_date, registration_open_date=self.event.start_date,
            registration_end_date=self.event.start_date, prizepool="$1",
            event_rules="rules", event_status="ongoing",
            registration_link="https://afc.test/reg", number_of_stages=1,
            creator=self.admin, is_draft=False)
        foreign_tt = TournamentTeam.objects.create(
            event=other_event, team=self.tts[0].team)
        resp = self._generate(self._ids(2) + [foreign_tt.tournament_team_id])
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("do not belong to this event", resp.json()["message"])
        # Double elim needs at least 3 teams.
        self.assertEqual(self._generate(self._ids(2), fmt="double_elim").status_code, 400)
        # Unknown fmt string.
        self.assertEqual(self._generate(self._ids(4), fmt="triple_elim").status_code, 400)
        # Non-numeric team id (P2, owner 2026-07-13): a clean 400, not an uncaught 500.
        resp = self._generate([self.tts[0].tournament_team_id, "not-an-int"])
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("integer", resp.json()["message"])

    def test_non_cs_stage_requires_explicit_fmt(self):
        br_stage = Stages.objects.create(
            event=self.event, stage_name="BR Stage", start_date=self.event.start_date,
            end_date=self.event.end_date, number_of_groups=1,
            stage_format="br - normal", teams_qualifying_from_stage=4)
        # No fmt and not a CS format -> 400 telling the caller to pass fmt.
        self.assertEqual(self._generate(self._ids(4), stage=br_stage).status_code, 400)
        # With an explicit fmt the same stage generates fine (tiebreaker-bracket escape hatch).
        self.assertEqual(
            self._generate(self._ids(4), fmt="single_elim", stage=br_stage).status_code, 201)


class DoubleElimTests(H2HBase):
    """Loser drops, grand-final wiring, and full-playthrough placements (4-team field)."""

    STAGE_FORMAT = "cs - double elimination"

    def setUp(self):
        super().setUp()
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        # 4 teams: WB R1 x2, WB final, grand final (winners R3), LB R1, LB R2 -> 6 matches.
        self.wb0 = self._m("winners", 1, 0)      # T1 vs T4
        self.wb1 = self._m("winners", 1, 1)      # T2 vs T3
        self.wb_final = self._m("winners", 2, 0)
        self.grand_final = self._m("winners", 3, 0)
        self.lb1 = self._m("losers", 1, 0)
        self.lb2 = self._m("losers", 2, 0)

    def test_structure_and_wiring(self):
        self.assertEqual(HeadToHeadMatch.objects.filter(stage=self.stage).count(), 6)
        # WB round-1 losers pair up in LB round 1 (slots by position parity).
        self.assertEqual((self.wb0.loser_next_match_id, self.wb0.loser_next_match_slot),
                         (self.lb1.pk, "a"))
        self.assertEqual((self.wb1.loser_next_match_id, self.wb1.loser_next_match_slot),
                         (self.lb1.pk, "b"))
        # WB final: winner to GF slot a, loser to LB final slot a.
        self.assertEqual((self.wb_final.next_match_id, self.wb_final.next_match_slot),
                         (self.grand_final.pk, "a"))
        self.assertEqual((self.wb_final.loser_next_match_id, self.wb_final.loser_next_match_slot),
                         (self.lb2.pk, "a"))
        # LB chain: LB1 winner to LB2 slot b; LB2 winner to GF slot b.
        self.assertEqual((self.lb1.next_match_id, self.lb1.next_match_slot), (self.lb2.pk, "b"))
        self.assertEqual((self.lb2.next_match_id, self.lb2.next_match_slot),
                         (self.grand_final.pk, "b"))
        # The grand final is the tree root.
        self.assertIsNone(self.grand_final.next_match_id)

    def test_loser_drops_into_losers_bracket(self):
        self.assertEqual(self._report(self.wb0, 4, 2).status_code, 200)  # T1 > T4
        self.lb1.refresh_from_db()
        self.assertEqual(self.lb1.team_a_id, self.tts[3].tournament_team_id)  # T4 dropped
        self.assertEqual(self._report(self.wb1, 1, 4).status_code, 200)  # T3 > T2
        self.lb1.refresh_from_db()
        self.assertEqual(self.lb1.team_b_id, self.tts[1].tournament_team_id)  # T2 dropped

    def test_full_playthrough_placements(self):
        # WB: T1 > T4, T2 > T3; WB final T1 > T2 (T2 drops to LB final).
        self.assertEqual(self._report(self.wb0, 4, 0).status_code, 200)
        self.assertEqual(self._report(self.wb1, 4, 2).status_code, 200)
        self.wb_final.refresh_from_db()
        self.assertEqual(self._report(self.wb_final, 4, 3).status_code, 200)
        # LB: T4 vs T3 -> T3 wins; LB final T2 vs T3 -> T2 wins; GF T1 vs T2 -> T1 champion.
        self.lb1.refresh_from_db()
        self.assertEqual(self._report(self.lb1, 2, 4).status_code, 200)
        self.lb2.refresh_from_db()
        self.assertEqual({self.lb2.team_a_id, self.lb2.team_b_id},
                         {self.tts[1].tournament_team_id, self.tts[2].tournament_team_id})
        self.assertEqual(self._report(self.lb2, 4, 1).status_code, 200)  # T2 > T3
        self.grand_final.refresh_from_db()
        self.assertEqual(self.grand_final.team_b_id, self.tts[1].tournament_team_id)
        resp = self._report(self.grand_final, 4, 2)  # T1 wins it all
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["bracket_complete"])

        rows = {r["team_name"]: r for r in self._get_bracket().json()["standings"]}
        self.assertEqual(rows["T1"]["placement"], 1)  # GF winner
        self.assertEqual(rows["T2"]["placement"], 2)  # GF loser
        self.assertEqual(rows["T3"]["placement"], 3)  # eliminated in the LB final
        self.assertEqual(rows["T4"]["placement"], 4)  # eliminated in LB round 1


class LeagueTests(H2HBase):
    """Every-pair-once league: shape, tie handling, and standings tiebreakers."""

    STAGE_FORMAT = "cs - league"

    def test_four_teams_play_every_pair_once(self):
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        matches = HeadToHeadMatch.objects.filter(stage=self.stage)
        self.assertEqual(matches.count(), 6)  # C(4,2)
        self.assertTrue(all(m.bracket == "league" for m in matches))
        self.assertTrue(all(m.next_match_id is None for m in matches))  # no advancement
        # Circle method: 3 rounds of 2 matches each (every team plays once per round).
        self.assertEqual(
            sorted(set(matches.values_list("round_number", flat=True))), [1, 2, 3])
        # Every unordered pair appears exactly once.
        pairs = {frozenset((m.team_a_id, m.team_b_id)) for m in matches}
        self.assertEqual(len(pairs), 6)

    def test_odd_team_count_sits_one_out_per_round(self):
        self.assertEqual(self._generate(self._ids(5)).status_code, 201)
        matches = HeadToHeadMatch.objects.filter(stage=self.stage)
        self.assertEqual(matches.count(), 10)  # C(5,2)
        pairs = {frozenset((m.team_a_id, m.team_b_id)) for m in matches}
        self.assertEqual(len(pairs), 10)

    def test_tie_allowed_and_standings_tiebreakers(self):
        self.assertEqual(self._generate(self._ids(3)).status_code, 201)
        t1, t2, t3 = self._ids(3)

        def match_of(a, b):
            return HeadToHeadMatch.objects.get(
                stage=self.stage, team_a__in=[a, b], team_b__in=[a, b])

        def report_oriented(a, b, score_for_a, score_for_b):
            """Report a result expressed from team a's perspective, regardless of which
            slot the circle method put each team in."""
            m = match_of(a, b)
            if m.team_a_id == a:
                return self._report(m, score_for_a, score_for_b)
            return self._report(m, score_for_b, score_for_a)

        # T1 beats T2 4-0; T3 beats T2 4-3; T1 vs T3 is a TIE 2-2 (allowed in league).
        self.assertEqual(report_oriented(t1, t2, 4, 0).status_code, 200)
        self.assertEqual(report_oriented(t3, t2, 4, 3).status_code, 200)
        resp = report_oriented(t1, t3, 2, 2)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(resp.json()["match"]["winner_id"])

        # Both T1 and T3 have 1 win; round diff breaks it: T1 +6 (6-2), T3 +1 (6-5).
        rows = self._get_bracket().json()["standings"]
        self.assertEqual([r["team_name"] for r in rows], ["T1", "T3", "T2"])
        self.assertEqual([r["placement"] for r in rows], [1, 2, 3])
        t1_row = rows[0]
        self.assertEqual((t1_row["wins"], t1_row["losses"]), (1, 0))  # the tie counts as neither
        self.assertEqual((t1_row["rounds_won"], t1_row["rounds_lost"]), (6, 2))


class PlacementStatsBridgeTests(H2HBase):
    """SUB-PROJECT D: a completed bracket mirrors placements into the existing pipeline."""

    def _play_four_team_knockout(self):
        """T1 > T4, T3 > T2, final T1 > T3 -> placements T1=1, T3=2, T4=T2=3."""
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        self.assertEqual(self._report(self._m("winners", 1, 0), 4, 1).status_code, 200)
        self.assertEqual(self._report(self._m("winners", 1, 1), 2, 4).status_code, 200)
        return self._report(self._m("winners", 2, 0), 4, 2)

    def test_completion_writes_synthetic_stat_rows(self):
        resp = self._play_four_team_knockout()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["bracket_complete"])

        # The synthetic match exists in a stage group, flagged by match_number=0.
        synthetic = Match.objects.get(group__stage=self.stage, match_number=0)
        self.assertTrue(synthetic.result_inputted)

        # One stat row per placed team: placement + DEFAULT_PLACEMENT points, zero kills.
        stats = {s.tournament_team.team.team_name: s
                 for s in TournamentTeamMatchStats.objects.filter(match=synthetic)}
        self.assertEqual(set(stats), {"T1", "T2", "T3", "T4"})
        self.assertEqual(stats["T1"].placement, 1)
        self.assertEqual(stats["T1"].placement_points, 12)  # DEFAULT_PLACEMENT[1]
        self.assertEqual(stats["T3"].placement, 2)
        self.assertEqual(stats["T3"].placement_points, 9)
        self.assertEqual(stats["T4"].placement, 3)          # semifinal losers share 3rd
        self.assertEqual(stats["T2"].placement, 3)
        self.assertEqual(stats["T2"].placement_points, 8)   # DEFAULT_PLACEMENT[3]
        self.assertEqual(stats["T1"].kills, 0)
        self.assertEqual(stats["T1"].total_points, 12)

    def test_existing_leaderboard_read_sees_the_bridge_rows(self):
        # round_robin.cumulative_standings sums the SAME TournamentTeamMatchStats rows the
        # leaderboard view aggregates (match__group__stage walk), so it proves the bridge
        # is visible to the existing pipeline with no changes on its side.
        self._play_four_team_knockout()
        table = round_robin.cumulative_standings(self.stage)
        self.assertEqual([r["team_name"] for r in table][:2], ["T1", "T3"])
        self.assertEqual(table[0]["effective_total"], 12)
        self.assertEqual(table[0]["total_booyah"], 1)  # placement 1 counts as a booyah

    def test_corrected_final_refreshes_the_same_rows(self):
        self._play_four_team_knockout()
        # The final has no downstream, so it may be re-reported: T3 now beats T1.
        final = self._m("winners", 2, 0)
        self.assertEqual(self._report(final, 1, 4).status_code, 200)

        synthetic = Match.objects.get(group__stage=self.stage, match_number=0)
        stats = {s.tournament_team.team.team_name: s.placement
                 for s in TournamentTeamMatchStats.objects.filter(match=synthetic)}
        # Same four rows, champion and runner-up swapped - refreshed, not duplicated.
        self.assertEqual(stats, {"T3": 1, "T1": 2, "T4": 3, "T2": 3})


class PlayerRankingBridgeTests(H2HBase):
    """CS results feed PLAYER rankings too (owner 2026-07-13: "cs should be both team and player
    ranking"). write_placement_stats writes a PLAYED TournamentPlayerMatchStats (kills 0) per rostered
    member of each placed team, so every CS player counts as having played the event (participation)
    and the champion's roster gets the team-win bonus - afc_rankings._collect_player scores on
    participation + kills + team_won + finals, never raw placement."""

    def setUp(self):
        super().setUp()
        # Give the four bracket teams (T1..T4) a 2-player roster each; T5/T6 sit out the 4-team field.
        self.rosters = {}
        for tt in self.tts[:4]:
            self.rosters[tt.tournament_team_id] = self._add_roster(tt, 2)

    def _add_roster(self, tt, n):
        users = []
        base = tt.team.team_name
        for i in range(n):
            u = User.objects.create(
                username=f"{base}_m{i}", email=f"{base}_m{i}@afc.test",
                full_name=f"{base} M{i}", role="player")
            TournamentTeamMember.objects.create(
                tournament_team=tt, user=u, event=self.event, status="active")
            users.append(u)
        return users

    def _play_four_team_knockout(self):
        """T1 > T4, T3 > T2, final T1 > T3 -> all four placed."""
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        self.assertEqual(self._report(self._m("winners", 1, 0), 4, 1).status_code, 200)
        self.assertEqual(self._report(self._m("winners", 1, 1), 2, 4).status_code, 200)
        return self._report(self._m("winners", 2, 0), 4, 2)

    def test_completion_writes_player_stats_for_rosters(self):
        resp = self._play_four_team_knockout()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["bracket_complete"])

        synthetic = Match.objects.get(group__stage=self.stage, match_number=0)
        pstats = TournamentPlayerMatchStats.objects.filter(team_stats__match=synthetic)
        # 4 placed teams x 2 roster members = 8 played, kills-0 player rows (no per-player CS kills).
        self.assertEqual(pstats.count(), 8)
        self.assertTrue(all(p.played and p.kills == 0 for p in pstats))

        # Champion T1's two members each get a row hung off T1's synthetic team stat.
        t1 = self.tts[0]
        t1_users = {u.pk for u in self.rosters[t1.tournament_team_id]}
        t1_stat = TournamentTeamMatchStats.objects.get(match=synthetic, tournament_team=t1)
        got = set(TournamentPlayerMatchStats.objects.filter(team_stats=t1_stat)
                  .values_list("player_id", flat=True))
        self.assertEqual(got, t1_users)

    def test_roster_change_resyncs_player_stats(self):
        self._play_four_team_knockout()
        synthetic = Match.objects.get(group__stage=self.stage, match_number=0)
        t1 = self.tts[0]
        # Drop one member from T1's roster, then re-run the bridge (a corrected final would do this).
        dropped = self.rosters[t1.tournament_team_id][0]
        TournamentTeamMember.objects.filter(tournament_team=t1, user=dropped).delete()
        head_to_head.write_placement_stats(self.stage)

        t1_stat = TournamentTeamMatchStats.objects.get(match=synthetic, tournament_team=t1)
        remaining = set(TournamentPlayerMatchStats.objects.filter(team_stats=t1_stat)
                        .values_list("player_id", flat=True))
        self.assertNotIn(dropped.pk, remaining)
        self.assertEqual(len(remaining), 1)


class PermissionTests(H2HBase):
    """Stranger 403s; an organizer of the OWNING org passes both write gates."""

    def setUp(self):
        super().setUp()
        # A plain player with a live token (the stranger).
        self.player = User.objects.create(
            username="h2h_player", email="h2h_player@afc.test",
            full_name="H2H Player", role="player")
        self.player_token = SessionToken.objects.create(
            user=self.player, token="h2h-player-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))

        # An organizer OWNER of the org that owns a second, org-scoped event.
        self.organizer = User.objects.create(
            username="h2h_org_owner", email="h2h_org_owner@afc.test",
            full_name="H2H Org Owner", role="player")
        self.organizer_token = SessionToken.objects.create(
            user=self.organizer, token="h2h-organizer-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.org = Organization.objects.create(
            slug="h2h-esports", name="H2H Esports", created_by=self.admin)
        OrganizationMember.objects.create(
            organization=self.org, user=self.organizer, role="owner", status="active")
        self.event.organization = self.org
        self.event.save(update_fields=["organization"])

    def test_stranger_cannot_generate_or_report(self):
        resp = self._generate(self._ids(4), token=self.player_token.token)
        self.assertEqual(resp.status_code, 403, resp.content)

        # Build a bracket as admin, then try to report as the stranger.
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        resp = self._report(self._m("winners", 1, 0), 4, 0, token=self.player_token.token)
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_org_owner_can_generate_and_report_on_their_event(self):
        # Owner role implies every org permission (can_edit_events + can_upload_results).
        resp = self._generate(self._ids(4), token=self.organizer_token.token)
        self.assertEqual(resp.status_code, 201, resp.content)
        resp = self._report(self._m("winners", 1, 0), 4, 2,
                            token=self.organizer_token.token)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_organizer_of_another_org_is_rejected(self):
        # Same user, but the event belongs to a DIFFERENT org -> org_can_event fails.
        other_org = Organization.objects.create(
            slug="other-esports", name="Other Esports", created_by=self.admin)
        self.event.organization = other_org
        self.event.save(update_fields=["organization"])
        resp = self._generate(self._ids(4), token=self.organizer_token.token)
        self.assertEqual(resp.status_code, 403, resp.content)


class CreateEventCSGroupGuardTests(TestCase):
    """CS remediation P1#1 (owner 2026-07-13): create_event / edit_event must NOT materialise the
    BR-style group + Match + Leaderboard rows the stage-config wizard still sends for a `cs - *`
    stage. Those phantom "Pending" matches sit next to the real HeadToHeadMatch bracket and entering
    a result into one DOUBLE-WRITES scoring. A BR stage in the SAME create keeps materialising its
    group + matches, proving the guard is CS-specific (mirrors the round-robin phantom-group guard).

    Consumes: POST /events/create-event/ (create_event) and /events/<id>/edit-event/ style edit.
    Related engine tests above prove the bracket itself needs no group to seed (generate_bracket
    takes explicit team_ids)."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create(
            username="cs_guard_admin", email="cs_guard_admin@afc.test",
            full_name="CS Guard Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="cs-guard-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))

    def _payload(self):
        D = "2026-06-01"

        def group(name, count, maps):
            # The BR-shaped group the stage-config wizard forces onto EVERY stage, CS included.
            return {"group_name": name, "playing_date": D, "playing_time": "10:00",
                    "teams_qualifying": 1, "match_count": count, "match_maps": maps}

        return {
            "competition_type": "tournament", "participant_type": "squad",
            "event_type": "internal", "max_teams_or_players": 16,
            "event_name": "CS Guard Cup", "event_mode": "virtual",
            "start_date": D, "end_date": D,
            "registration_open_date": D, "registration_end_date": D,
            "event_start_time": "10:00", "event_end_time": "12:00",
            "registration_start_time": "09:00", "registration_end_time": "09:30",
            "prizepool": "$1000", "number_of_stages": 2, "is_draft": False,
            "stages": [
                {"stage_name": "Bracket", "start_date": D, "end_date": D,
                 "number_of_groups": 1, "stage_format": "cs - knockout",
                 "teams_qualifying_from_stage": 1,
                 "groups": [group("CS Group A", 3, ["bermuda", "purgatory", "kalahari"])]},
                {"stage_name": "Group Stage", "start_date": D, "end_date": D,
                 "number_of_groups": 1, "stage_format": "br - normal",
                 "teams_qualifying_from_stage": 1,
                 "groups": [group("BR Group 1", 2, ["bermuda", "purgatory"])]},
            ],
        }

    def test_cs_stage_creates_no_phantom_group_but_br_stage_does(self):
        resp = self.client.post(
            "/events/create-event/", data=self._payload(),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertIn(resp.status_code, (200, 201), resp.content)

        event = Event.objects.get(event_name="CS Guard Cup")
        cs_stage = Stages.objects.get(event=event, stage_format="cs - knockout")
        br_stage = Stages.objects.get(event=event, stage_format="br - normal")

        # CS stage: the wizard's forced BR group - its maps, its matches, its leaderboard - is
        # still IGNORED. That is the guard this test exists for, and it has not changed.
        #
        # What HAS changed (owner backlog item 21, 2026-08-13): the stage now carries exactly one
        # BRACKET group, because the mode lives on the group rather than in stage_format. It is a
        # bracket, not a lobby: no Match rows, no Leaderboard, no maps. Splitting a stage into
        # several groups is the organizer's opt-in; one is what an unsplit stage looks like.
        cs_groups = list(StageGroups.objects.filter(stage=cs_stage))
        self.assertEqual(len(cs_groups), 1)
        self.assertEqual(cs_groups[0].bracket_format, "single_elim")
        self.assertEqual(cs_groups[0].match_maps, [])
        self.assertEqual(Match.objects.filter(group__stage=cs_stage).count(), 0)
        self.assertEqual(Leaderboard.objects.filter(stage=cs_stage).count(), 0)

        # BR control stage: its group + 2 matches + leaderboard DID materialise (guard is CS-only).
        self.assertEqual(StageGroups.objects.filter(stage=br_stage).count(), 1)
        self.assertEqual(Match.objects.filter(group__stage=br_stage).count(), 2)
        self.assertEqual(Leaderboard.objects.filter(stage=br_stage).count(), 1)


class BracketOverlayPayloadTests(H2HBase):
    """CS remediation P1#6 (owner 2026-07-13): the h2h broadcast overlay renders the BRACKET for a
    Clash Squad event instead of a versus stat card (a pure CS event has no BR stats to compare).
    views_overlays._h2h_payload with config {mode:"bracket", stage_id} returns the resolved bracket
    tree (same shape as the public bracket GET) so the overlay renderer can draw it read-only.

    Consumes: _h2h_payload (bundled into the public overlay_config poll). design_id is omitted, so
    _design_look returns None without touching the request and a RequestFactory stub is enough."""

    def _payload(self, config):
        from django.test import RequestFactory
        from afc_tournament_and_scrims.views_overlays import _h2h_payload
        return _h2h_payload(self.event, config, RequestFactory().get("/"))

    def test_bracket_mode_returns_the_resolved_tree(self):
        # Build + partly play a 4-team knockout so the tree has a real round-1 result.
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        self.assertEqual(self._report(self._m("winners", 1, 0), 4, 1).status_code, 200)

        out = self._payload({"mode": "bracket", "stage_id": self.stage.stage_id})
        self.assertEqual(out["mode"], "bracket")
        self.assertEqual(out["competitors"], [])          # no stat cards in bracket mode
        self.assertIsNotNone(out["bracket"])
        self.assertTrue(out["bracket"]["generated"])
        self.assertEqual(out["bracket"]["stage_id"], self.stage.stage_id)
        # 4 teams -> 2 winners rounds (round 1 + final), no losers bracket for single-elim.
        self.assertEqual(len(out["bracket"]["rounds"]["winners"]), 2)
        self.assertEqual(out["bracket"]["rounds"]["losers"], [])

    def test_bracket_mode_falls_back_to_first_cs_stage_when_stage_id_missing(self):
        self.assertEqual(self._generate(self._ids(4)).status_code, 201)
        # No stage_id: resolve the event's only CS stage automatically.
        out = self._payload({"mode": "bracket"})
        self.assertIsNotNone(out["bracket"])
        self.assertEqual(out["bracket"]["stage_id"], self.stage.stage_id)

    def test_bracket_mode_before_generation_returns_none_bracket(self):
        # Stage exists but no bracket generated yet -> bracket is a not-generated payload.
        out = self._payload({"mode": "bracket", "stage_id": self.stage.stage_id})
        self.assertIsNotNone(out["bracket"])
        self.assertFalse(out["bracket"]["generated"])
        self.assertEqual(out["bracket"]["rounds"]["winners"], [])


class ThirdPlaceMatchTests(H2HBase):
    """The optional bronze match in a single-elimination bracket (owner 2026-08-12).

    Without it, the two semifinal losers SHARE placement 3 and an event that pays 3rd and 4th
    differently cannot use the bracket's own result. With it, they play each other: the winner
    is 3rd, the loser 4th, and everyone knocked out earlier shifts down accordingly.
    """

    def _generate_third(self, team_ids, third_place=True):
        return self.client.post(
            f"/events/stages/{self.stage.stage_id}/bracket/generate/",
            data={"team_ids": team_ids, "third_place": third_place},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def test_not_created_unless_asked(self):
        # The default is unchanged: no bronze match, semifinal losers still share 3rd.
        self._generate(self._ids(4))
        self.assertFalse(HeadToHeadMatch.objects.filter(stage=self.stage, bracket="third").exists())

    def test_created_and_fed_by_both_semifinals(self):
        resp = self._generate_third(self._ids(4))
        self.assertEqual(resp.status_code, 201)
        third = HeadToHeadMatch.objects.get(stage=self.stage, bracket="third")
        # 4 teams -> R = 2, so the semifinals are round 1 and both drop their loser into it.
        semi_a = self._m("winners", 1, 0)
        semi_b = self._m("winners", 1, 1)
        self.assertEqual(semi_a.loser_next_match_id, third.h2h_match_id)
        self.assertEqual(semi_a.loser_next_match_slot, "a")
        self.assertEqual(semi_b.loser_next_match_id, third.h2h_match_id)
        self.assertEqual(semi_b.loser_next_match_slot, "b")
        # It is NOT in the winners bracket, so "the final is the winners match with no
        # next_match" still resolves to the real final.
        self.assertEqual(third.bracket, "third")
        self.assertIsNone(third.next_match_id)

    def test_skipped_when_there_are_no_semifinals(self):
        # 2 teams is a final only; there is no 3rd place to play for.
        self._generate_third(self._ids(2))
        self.assertFalse(HeadToHeadMatch.objects.filter(stage=self.stage, bracket="third").exists())

    def test_losers_are_routed_into_it_and_placements_split_3_and_4(self):
        self._generate_third(self._ids(4))
        third = HeadToHeadMatch.objects.get(stage=self.stage, bracket="third")

        # Semifinals: T1 beats T4, T2 beats T3 (seeding puts 1v4 and 2v3).
        semi_a, semi_b = self._m("winners", 1, 0), self._m("winners", 1, 1)
        self._report(semi_a, 4, 1)
        self._report(semi_b, 4, 2)

        # Both losers have been dropped into the bronze match.
        semi_a.refresh_from_db()
        semi_b.refresh_from_db()
        third.refresh_from_db()

        def loser_of(m):
            return m.team_b_id if m.winner_id == m.team_a_id else m.team_a_id

        self.assertEqual(third.team_a_id, loser_of(semi_a))
        self.assertEqual(third.team_b_id, loser_of(semi_b))

        # Final first: the bracket must NOT be called complete while the bronze match is pending.
        final = self._m("winners", 2, 0)
        resp = self._report(final, 4, 3)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["bracket_complete"])

        # Now the bronze match decides 3rd and 4th.
        third.refresh_from_db()
        resp = self._report(third, 4, 0)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["bracket_complete"])

        rows = {r["team_name"]: r["placement"] for r in head_to_head.standings(self.stage)}
        final.refresh_from_db()
        third.refresh_from_db()
        bronze_winner = third.winner_id
        bronze_loser = (third.team_b_id if bronze_winner == third.team_a_id else third.team_a_id)
        names = {tt.tournament_team_id: tt.team.team_name for tt in self.tts}
        self.assertEqual(rows[names[final.winner_id]], 1)
        self.assertEqual(rows[names[bronze_winner]], 3)
        self.assertEqual(rows[names[bronze_loser]], 4)
        # Nobody shares a placement any more: 1, 2, 3, 4 all distinct.
        self.assertEqual(sorted(rows.values()), [1, 2, 3, 4])

    def test_eight_teams_quarterfinal_losers_sit_below_fourth(self):
        self._generate_third(self._ids(6))  # 6 teams -> bracket of 8, two byes
        # Play everything; the helper walks whatever is playable until nothing is left.
        for _ in range(30):
            m = (HeadToHeadMatch.objects
                 .filter(stage=self.stage, status="pending",
                         team_a__isnull=False, team_b__isnull=False)
                 .order_by("bracket", "round_number", "position").first())
            if not m:
                break
            head_to_head.report_result(m, 4, 1)

        rows = head_to_head.standings(self.stage)
        placements = sorted(r["placement"] for r in rows if r["placement"])
        # 1, 2, 3, 4 distinct, then the two quarterfinal losers share 5th.
        self.assertEqual(placements[:4], [1, 2, 3, 4])
        self.assertEqual(placements[4:], [5, 5])

    def test_payload_carries_the_bronze_match_in_its_own_bucket(self):
        self._generate_third(self._ids(4))
        body = self._get_bracket().json()
        self.assertEqual(len(body["rounds"]["third"]), 1)
        self.assertEqual(len(body["rounds"]["third"][0]["matches"]), 1)
        # And it is not mixed into the winners columns: R=2 means exactly 2 winners rounds.
        self.assertEqual(len(body["rounds"]["winners"]), 2)

    def test_beaten_finalist_is_second_while_the_bronze_match_is_still_pending(self):
        # Regression (owner saw it live 2026-08-12): between the final and the bronze match, the
        # two teams waiting to play for 3rd used to occupy slots 2 and 3, which pushed the beaten
        # finalist down to #4. They can only finish 3rd or 4th, so they belong below it.
        self._generate_third(self._ids(4))
        self._report(self._m("winners", 1, 0), 4, 1)
        self._report(self._m("winners", 1, 1), 4, 2)
        self._report(self._m("winners", 2, 0), 4, 3)  # final played, bronze still pending

        rows = head_to_head.standings(self.stage)
        placed = [(r["team_name"], r["placement"]) for r in rows if r["placement"]]
        self.assertEqual([p for _, p in placed], [1, 2])   # champion and runner-up, nothing else
        # The two waiting on the bronze match come last, with no placement yet.
        self.assertIsNone(rows[-1]["placement"])
        self.assertIsNone(rows[-2]["placement"])


class PlayerStatEntryTests(H2HBase):
    """Per-player lines on a Clash Squad set (owner 2026-08-12: "you should be able to enter for
    each player also ... then there will be stats for players too like the BR section").

    The per-set rows live on H2HPlayerStat; write_placement_stats then sums them into the one
    synthetic TournamentPlayerMatchStats row per player, which is what player profiles, the kill
    tables and afc_rankings actually read.
    """

    def setUp(self):
        super().setUp()
        # Two players on each of the first two teams, rostered for this event.
        self.players = {}
        for tt in self.tts[:2]:
            for n in (1, 2):
                u = User.objects.create(
                    username=f"p{tt.tournament_team_id}_{n}",
                    email=f"p{tt.tournament_team_id}_{n}@afc.test",
                    full_name=f"Player {n}", role="player")
                TournamentTeamMember.objects.create(
                    tournament_team=tt, user=u, event=self.event,
                    status="active", in_game_role="rusher")
                self.players.setdefault(tt.tournament_team_id, []).append(u)

    def _report_with_players(self, match, score_a, score_b, rows):
        return self.client.post(
            f"/events/h2h-matches/{match.h2h_match_id}/result/",
            data={"score_a": score_a, "score_b": score_b, "player_stats": rows},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def _rows_for(self, match, kills_by_player):
        out = []
        for tid in (match.team_a_id, match.team_b_id):
            for u in self.players[tid]:
                out.append({
                    "player_id": u.user_id, "tournament_team_id": tid,
                    "kills": kills_by_player.get(u.user_id, 0), "damage": 100, "assists": 1,
                    "played": True,
                })
        return out

    def test_lines_are_stored_and_echoed_back(self):
        self._generate(self._ids(2))       # T1 vs T2, a single final
        final = self._m("winners", 1, 0)
        a1 = self.players[final.team_a_id][0]
        resp = self._report_with_players(final, 4, 2, self._rows_for(final, {a1.user_id: 7}))
        self.assertEqual(resp.status_code, 200, resp.content)

        self.assertEqual(H2HPlayerStat.objects.filter(h2h_match=final).count(), 4)
        stored = H2HPlayerStat.objects.get(h2h_match=final, player=a1)
        self.assertEqual((stored.kills, stored.damage, stored.assists), (7, 100, 1))

        # The bracket payload carries them, so a correction can pre-fill from what was entered.
        body = self._get_bracket().json()
        match_body = body["rounds"]["winners"][0]["matches"][0]
        self.assertEqual(len(match_body["player_stats"]), 4)

    def test_a_correction_replaces_rather_than_appends(self):
        self._generate(self._ids(2))
        final = self._m("winners", 1, 0)
        a1 = self.players[final.team_a_id][0]
        self._report_with_players(final, 4, 2, self._rows_for(final, {a1.user_id: 7}))
        self._report_with_players(final, 4, 1, self._rows_for(final, {a1.user_id: 9}))

        self.assertEqual(H2HPlayerStat.objects.filter(h2h_match=final).count(), 4)
        self.assertEqual(H2HPlayerStat.objects.get(h2h_match=final, player=a1).kills, 9)

    def test_a_player_from_another_team_is_refused_and_the_score_rolls_back(self):
        self._generate(self._ids(2))
        final = self._m("winners", 1, 0)
        stranger = User.objects.create(
            username="stranger", email="stranger@afc.test", full_name="S", role="player")
        rows = [{"player_id": stranger.user_id, "tournament_team_id": final.team_a_id,
                 "kills": 3, "damage": 0, "assists": 0}]
        resp = self._report_with_players(final, 4, 2, rows)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("roster", resp.json()["message"].lower())

        # The whole thing was refused: no score written either.
        final.refresh_from_db()
        self.assertEqual(final.status, "pending")
        self.assertIsNone(final.winner_id)

    def test_kills_roll_up_into_the_player_and_team_rows_the_rest_of_afc_reads(self):
        self._generate(self._ids(2))
        final = self._m("winners", 1, 0)
        a1, a2 = self.players[final.team_a_id]
        self._report_with_players(
            final, 4, 2, self._rows_for(final, {a1.user_id: 7, a2.user_id: 3}))

        # Bracket complete -> the sub-project D bridge ran.
        team_stat = TournamentTeamMatchStats.objects.get(
            match__group__stage=self.stage, tournament_team_id=final.team_a_id)
        self.assertEqual(team_stat.kills, 10)          # 7 + 3, summed from the set
        self.assertEqual(team_stat.kill_points, 0)     # CS is scored on placement, not kills
        self.assertEqual(team_stat.total_points, team_stat.placement_points)

        line = TournamentPlayerMatchStats.objects.get(team_stats=team_stat, player=a1)
        self.assertEqual(line.kills, 7)
        self.assertEqual(line.role_at_match, "rusher")

    def test_set_scores_alone_still_work_and_leave_players_on_zero(self):
        # Unchanged behaviour for an organizer who does not enter player lines.
        self._generate(self._ids(2))
        final = self._m("winners", 1, 0)
        self._report(final, 4, 0)
        team_stat = TournamentTeamMatchStats.objects.get(
            match__group__stage=self.stage, tournament_team_id=final.team_a_id)
        self.assertEqual(team_stat.kills, 0)
        # Participation credit is still written for every rostered player.
        self.assertEqual(
            TournamentPlayerMatchStats.objects.filter(team_stats=team_stat).count(), 2)

    def test_rosters_endpoint_lists_both_sides(self):
        self._generate(self._ids(2))
        final = self._m("winners", 1, 0)
        resp = self.client.get(
            f"/events/h2h-matches/{final.h2h_match_id}/rosters/",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.content)
        teams = resp.json()["teams"]
        self.assertEqual(len(teams), 2)
        self.assertEqual(len(teams[0]["players"]), 2)

    def test_rosters_endpoint_is_not_public(self):
        self._generate(self._ids(2))
        final = self._m("winners", 1, 0)
        self.assertEqual(
            self.client.get(f"/events/h2h-matches/{final.h2h_match_id}/rosters/").status_code, 400)
