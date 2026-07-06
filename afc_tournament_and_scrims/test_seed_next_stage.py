"""
afc_tournament_and_scrims.test_seed_next_stage — seed_next_stage_by_standings (owner 2026-07-06).

Verifies the "seed the NEXT stage's groups by this round-robin stage's combined standings, snake"
option: teams are ordered by cumulative_standings(rr_stage) and snaked across the next stage's groups,
plus the guard paths (last stage, missing auth).

Run:
    ./.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_seed_next_stage
"""
import datetime
import uuid

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from afc_auth.models import SessionToken
from afc_team.models import Team

from .models import (
    Event, Stages, StageGroups, StageCompetitor, StageGroupCompetitor,
    TournamentTeam, Match, TournamentTeamMatchStats,
)

User = get_user_model()
TODAY = datetime.date.today()


def _uniq(p="x"):
    return f"{p}-{uuid.uuid4().hex[:10]}"


class SeedNextStageByStandingsTests(APITestCase):
    def setUp(self):
        # AFC admin passes _seeding_gate on a native (org=None) event.
        self.admin = User.objects.create_user(
            username=_uniq("u"), email=f"{_uniq('e')}@t.local", password="pw-strong-9273", role="admin")
        self.token = _uniq("tok")
        SessionToken.objects.create(
            user=self.admin, token=self.token, expires_at=timezone.now() + datetime.timedelta(hours=1))

        self.event = Event.objects.create(
            slug=_uniq("event"), competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_name="E", event_mode="virtual",
            start_date=TODAY, end_date=TODAY, registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="$1", prize_distribution={}, event_rules="r", event_status="ongoing",
            registration_link="https://x.co/r", number_of_stages=2)

        # RR stage: one lobby + one played match to carry results.
        self.rr = Stages.objects.create(
            event=self.event, stage_name="SEMI", start_date=TODAY, end_date=TODAY, number_of_groups=1,
            stage_format="br - round robin", teams_qualifying_from_stage=8)
        self.lobby = StageGroups.objects.create(
            stage=self.rr, group_name="Day 1", playing_date=TODAY, playing_time=datetime.time(12, 0),
            teams_qualifying=8, match_count=1, match_maps=[])
        self.match = Match.objects.create(
            group=self.lobby, match_number=1, match_map="bermuda", result_inputted=True)

        # Next stage: normal, TWO groups so the snake actually alternates.
        self.nxt = Stages.objects.create(
            event=self.event, stage_name="FINALS", start_date=TODAY, end_date=TODAY, number_of_groups=2,
            stage_format="br - normal", teams_qualifying_from_stage=8)
        self.g1 = StageGroups.objects.create(
            stage=self.nxt, group_name="Group A", playing_date=TODAY, playing_time=datetime.time(12, 0),
            teams_qualifying=8, match_count=1, match_maps=[], group_order=1)
        self.g2 = StageGroups.objects.create(
            stage=self.nxt, group_name="Group B", playing_date=TODAY, playing_time=datetime.time(12, 0),
            teams_qualifying=8, match_count=1, match_maps=[], group_order=2)

        # 6 teams with strictly-descending RR points (60..10) = ranks 1..6, all advanced into the next
        # stage's pool (StageCompetitor). self.teams[0] = rank 1 ... self.teams[5] = rank 6.
        self.teams = []
        for k, pts in enumerate([60, 50, 40, 30, 20, 10]):
            o = User.objects.create_user(
                username=_uniq("o"), email=f"{_uniq('e')}@t.local", password="pw-strong-9273", role="player")
            team = Team.objects.create(team_name=f"T{k+1}", join_settings="open", team_creator=o, team_owner=o)
            tt = TournamentTeam.objects.create(event=self.event, team=team, status="active")
            TournamentTeamMatchStats.objects.create(
                match=self.match, tournament_team=tt, placement=k + 1, kills=pts, total_points=pts)
            StageCompetitor.objects.create(stage=self.nxt, tournament_team=tt, status="active")
            self.teams.append(tt)

    def _post(self, body, auth=True):
        kw = {"format": "json"}
        if auth:
            kw["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
        return self.client.post(reverse("seed_next_stage_by_standings"), body, **kw)

    def test_snake_seeds_next_stage_by_rr_standings(self):
        res = self._post({"stage_id": self.rr.stage_id})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["seeded"], 6)
        g1 = set(StageGroupCompetitor.objects.filter(stage_group=self.g1).values_list("tournament_team_id", flat=True))
        g2 = set(StageGroupCompetitor.objects.filter(stage_group=self.g2).values_list("tournament_team_id", flat=True))
        T = [t.tournament_team_id for t in self.teams]  # T[0]=rank1 ... T[5]=rank6
        # Snake over 2 groups: r1->A, r2->B, (reverse) r3->B, r4->A, (forward) r5->A, r6->B.
        self.assertEqual(g1, {T[0], T[3], T[4]}, "Group A should hold ranks 1,4,5")
        self.assertEqual(g2, {T[1], T[2], T[5]}, "Group B should hold ranks 2,3,6")

    def test_auto_advances_qualifiers_when_next_stage_empty(self):
        # Empty the next stage's pool + set the RR cut to 4: the endpoint must ADVANCE the top 4 by
        # standings (write their StageCompetitor rows) AND seed them - no separate advance step.
        StageCompetitor.objects.filter(stage=self.nxt).delete()
        self.rr.teams_qualifying_from_stage = 4
        self.rr.save(update_fields=["teams_qualifying_from_stage"])

        res = self._post({"stage_id": self.rr.stage_id})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["advanced"], 4)
        self.assertEqual(res.data["seeded"], 4)

        T = [t.tournament_team_id for t in self.teams]  # T[0]=rank1 ... T[5]=rank6
        advanced = set(StageCompetitor.objects.filter(
            stage=self.nxt, tournament_team__isnull=False).values_list("tournament_team_id", flat=True))
        self.assertEqual(advanced, {T[0], T[1], T[2], T[3]}, "top 4 by standings advanced")
        g1 = set(StageGroupCompetitor.objects.filter(stage_group=self.g1).values_list("tournament_team_id", flat=True))
        g2 = set(StageGroupCompetitor.objects.filter(stage_group=self.g2).values_list("tournament_team_id", flat=True))
        self.assertEqual(g1, {T[0], T[3]}, "snake: Group A = ranks 1,4")
        self.assertEqual(g2, {T[1], T[2]}, "snake: Group B = ranks 2,3")

    def test_reseed_replaces_previous_seeding(self):
        # Running twice must not duplicate (clear-then-write), staying at exactly 6 group rows.
        self._post({"stage_id": self.rr.stage_id})
        self._post({"stage_id": self.rr.stage_id})
        self.assertEqual(StageGroupCompetitor.objects.filter(stage_group__stage=self.nxt).count(), 6)

    def test_last_stage_has_no_next(self):
        res = self._post({"stage_id": self.nxt.stage_id})
        self.assertEqual(res.status_code, 400)
        self.assertIn("last stage", res.data["message"].lower())

    def test_missing_auth_rejected(self):
        res = self._post({"stage_id": self.rr.stage_id}, auth=False)
        self.assertEqual(res.status_code, 400)
