"""
Owner override (2026-06-29): Point-Rush carry-over must influence QUALIFICATION, not just display.

"The point adds to the team's total points" — so a team's source-stage placement bonus, carried into
the connected/TARGET stage, is part of that team's TOTAL POINTS there and must count when ranking the
target stage to decide WHO ADVANCES OUT of it. views._fold_carry_over applies the SAME on-read bonus
(views._carry_over_for_stage) the leaderboard shows to the three advancement ranking paths:
  • advance_group_competitors_to_next_stage  (per-group, Sum(total_points))
  • advance_round_robin                       (cumulative_standings, effective_total)
  • advancement_routing._ranking_for_rule     (branching engine, both shapes)

These tests build a real event where, in the TARGET stage, team A leads on RAW points but team B's
source carry-over lifts B's TOTAL above A — and prove qualification ORDER flips to B. TestCase =>
every row is rolled back, no MySQL state leaks. Nothing is persisted by the feature (mirrors the
leaderboard overlay; avoids the seed-time double-count flagged earlier).
"""
import datetime
import json

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import advancement_routing, event_links, views
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageAdvancementRule,
    StageCompetitor,
    StageGroups,
    Stages,
    TournamentTeam,
)


class CarryOverQualificationDBTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Admin + forged token (advance_group requires role == "admin"; advance/routing use it too).
        self.admin = User.objects.create(
            username="adv_admin", email="adv_admin@example.com",
            full_name="Adv Admin", role="admin", password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin, token="adv-admin-token-1234567890",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )

        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Carry Qualify Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="rules", event_status="ongoing",
            registration_link="https://example.com/reg", number_of_stages=3, creator=self.admin,
        )

        # Stage chain: SOURCE (Point-Rush) -> TARGET (advanced out of) -> NEXT (seed destination).
        self.target = Stages.objects.create(
            event=self.event, stage_name="Target", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1, stage_order=2,
        )
        self.next_stage = Stages.objects.create(
            event=self.event, stage_name="Next", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1, stage_order=3,
        )
        # SOURCE rewards ONLY 1st place with a big +50 (so the source winner clearly leapfrogs in target).
        self.source = Stages.objects.create(
            event=self.event, stage_name="Source", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1,
            point_rush_enabled=True, point_rush_reward={"1": 50}, point_rush_target_stage=self.target,
        )

        # Two teams.
        self.team_a = Team.objects.create(
            team_name="Alpha", team_tag="ALP", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.team_b = Team.objects.create(
            team_name="Bravo", team_tag="BRV", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.tt_a = TournamentTeam.objects.create(event=self.event, team=self.team_a, registered_by=self.admin)
        self.tt_b = TournamentTeam.objects.create(event=self.event, team=self.team_b, registered_by=self.admin)

        # SOURCE group + lb + match. Enter B=1st, A=2nd -> B ranks 1st in source -> carry {B: 50}.
        self.source_group, self.source_match = self._group_with_match(self.source, "Source A", qualify=2)
        self._enter_team_results(self.source_match, [(self.tt_b, 1), (self.tt_a, 2)])

        # TARGET group + lb + match. Enter A=1st(12), B=2nd(9): RAW leader is A, but B's +50 carry wins.
        self.target_group, self.target_match = self._group_with_match(self.target, "Target A", qualify=1)
        self._enter_team_results(self.target_match, [(self.tt_a, 1), (self.tt_b, 2)])

    # ── builders ──────────────────────────────────────────────────────────────────────────────
    def _group_with_match(self, stage, group_name, *, qualify):
        group = StageGroups.objects.create(
            stage=stage, group_name=group_name, playing_date=datetime.date.today(),
            playing_time=datetime.time(18, 0), teams_qualifying=qualify, match_count=1,
        )
        lb = Leaderboard.objects.create(
            leaderboard_name=f"{group_name} LB", event=self.event, stage=stage, group=group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual",
        )
        match = Match.objects.create(
            leaderboard=lb, group=group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1},
        )
        return group, match

    def _enter_team_results(self, match, placements):
        # placements: list of (tournament_team, placement). 0 kills -> points are pure placement.
        results = [
            {"tournament_team_id": tt.tournament_team_id, "placement": p, "played": True,
             "players": [{"kills": 0, "damage": 0, "assists": 0, "played": True}]}
            for (tt, p) in placements
        ]
        resp = self.client.post(
            "/events/enter-team-match-result-manual/",
            data={"match_id": match.match_id, "results": json.dumps(results)},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # The advance gate refuses any group with an un-inputted match; guarantee the flag is set.
        Match.objects.filter(match_id=match.match_id).update(result_inputted=True)

    def _advance_target_group(self):
        return self.client.post(
            "/events/advance-group-competitors-to-next-stage/",
            data={"event_id": self.event.event_id, "group_id": self.target_group.group_id},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    # ── tests ─────────────────────────────────────────────────────────────────────────────────
    def test_advance_group_carry_over_flips_qualifier(self):
        # Raw target leader is A (12 > 9). B's +50 source carry-over makes B's TOTAL 59 -> B qualifies.
        resp = self._advance_target_group()
        self.assertEqual(resp.status_code, 200, resp.content)
        seeded = list(
            StageCompetitor.objects.filter(stage=self.next_stage)
            .values_list("tournament_team_id", flat=True)
        )
        self.assertEqual(seeded, [self.tt_b.tournament_team_id])  # B advanced, NOT A
        self.assertNotIn(self.tt_a.tournament_team_id, seeded)

    def test_advance_group_no_point_rush_is_raw_order(self):
        # Disable the source's Point-Rush: carry is now empty -> the fold is a no-op -> the RAW leader
        # A advances (proves a normal event qualifies byte-identically).
        self.source.point_rush_enabled = False
        self.source.save(update_fields=["point_rush_enabled"])
        resp = self._advance_target_group()
        self.assertEqual(resp.status_code, 200, resp.content)
        seeded = list(
            StageCompetitor.objects.filter(stage=self.next_stage)
            .values_list("tournament_team_id", flat=True)
        )
        self.assertEqual(seeded, [self.tt_a.tournament_team_id])  # raw leader A advances

    def test_fold_carry_over_effective_total_shape_reorders(self):
        # The effective_total path (shared by advance_round_robin + advancement_routing team scope):
        # A leads raw (effective_total 12 > 9), B's +50 carry -> B leads after the fold.
        rows = [
            {"tournament_team_id": self.tt_a.tournament_team_id, "team_name": "Alpha",
             "effective_total": 12, "total_booyah": 1, "total_kills": 0},
            {"tournament_team_id": self.tt_b.tournament_team_id, "team_name": "Bravo",
             "effective_total": 9, "total_booyah": 0, "total_kills": 0},
        ]
        views._fold_carry_over(
            rows, self.target, "squad",
            id_key="tournament_team_id", metric_key="effective_total",
            sort_key=lambda r: (-int(r.get("effective_total") or 0),
                                -int(r.get("total_booyah") or 0),
                                -int(r.get("total_kills") or 0),
                                r.get("team_name") or ""),
        )
        self.assertEqual(rows[0]["tournament_team_id"], self.tt_b.tournament_team_id)
        self.assertEqual(rows[0]["effective_total"], 59)  # 9 + 50 carry
        self.assertEqual(rows[0]["carry_over_points"], 50)
        self.assertEqual(rows[1]["effective_total"], 12)  # A untouched (no source reward at 2nd)

    def test_fold_carry_over_no_source_is_noop(self):
        # Applied to the SOURCE stage (nothing targets it), the fold leaves rows + order untouched.
        rows = [
            {"tournament_team_id": self.tt_a.tournament_team_id, "team_name": "Alpha",
             "effective_total": 12, "total_booyah": 1, "total_kills": 0},
            {"tournament_team_id": self.tt_b.tournament_team_id, "team_name": "Bravo",
             "effective_total": 9, "total_booyah": 0, "total_kills": 0},
        ]
        before = [r["tournament_team_id"] for r in rows]
        views._fold_carry_over(
            rows, self.source, "squad",
            id_key="tournament_team_id", metric_key="effective_total",
            sort_key=lambda r: -int(r.get("effective_total") or 0),
        )
        self.assertEqual([r["tournament_team_id"] for r in rows], before)
        self.assertEqual(rows[0]["effective_total"], 12)  # unchanged

    def test_branching_rule_ranking_respects_carry_over(self):
        # The branching engine ranks rule.source_stage (= Target) before slicing positions. With the
        # carry-over fold, B (9 + 50) outranks A (12), so a top-1 routing rule would route B.
        rule = StageAdvancementRule.objects.create(
            source_stage=self.target, source_group=None, target_stage=self.next_stage,
            position_from=1, position_to=1, order=0,
        )
        rows, kind = advancement_routing._ranking_for_rule(rule, self.event)
        self.assertEqual(kind, "team")
        self.assertEqual(rows[0]["tournament_team_id"], self.tt_b.tournament_team_id)
        self.assertEqual(rows[1]["tournament_team_id"], self.tt_a.tournament_team_id)

    def test_linked_event_qualification_respects_carry_over(self):
        # CROSS-EVENT linked qualification (event_links): the top-N of a stage qualify INTO another
        # event. fire_link takes _stage_top_rows(source_stage)[:qualify_count], so the ORDER decides
        # who qualifies. With the carry-over fold, B (raw 9 + 50 carry = 59) outranks A (raw 12), so a
        # top-1 link off the Target stage would qualify B, not A — consistent with in-event advance.
        rows = event_links._stage_top_rows(self.target, self.event.participant_type)
        self.assertEqual(rows[0]["team_id"], self.team_b.team_id)  # B leads on the carry-inclusive total
        self.assertEqual(rows[1]["team_id"], self.team_a.team_id)

    def test_linked_event_qualification_no_point_rush_is_raw_order(self):
        # No Point-Rush source -> the fold is a no-op -> the linked top-N is the RAW order (A first),
        # proving a normal cross-event link ranks byte-identically.
        self.source.point_rush_enabled = False
        self.source.save(update_fields=["point_rush_enabled"])
        rows = event_links._stage_top_rows(self.target, self.event.participant_type)
        self.assertEqual(rows[0]["team_id"], self.team_a.team_id)  # raw leader A
        self.assertEqual(rows[1]["team_id"], self.team_b.team_id)
