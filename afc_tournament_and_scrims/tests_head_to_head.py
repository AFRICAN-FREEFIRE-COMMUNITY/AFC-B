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
    and the champion's roster gets the team-win bonus — afc_rankings._collect_player scores on
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

        # CS stage: the forced group + its matches + leaderboard were IGNORED (no phantom rows) —
        # the bracket owns the structure, generated later from team_ids with zero groups.
        self.assertEqual(StageGroups.objects.filter(stage=cs_stage).count(), 0)
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
