"""
Bulk accept/reject flagged players (owner 2026-07-13): PATCH events/flagged-kills/bulk/ applies many
MatchKillFlag decisions in ONE call + ONE _recompute_team_kills_for_event, because clicking one by one
re-scored the whole event on every click and felt slow.

Covers: many flags flipped to count / to drop in a single call; the team total reflects the batch;
flags from a DIFFERENT event are refused (skipped, not applied); auth is required.
Run: venv\\Scripts\\python.exe manage.py test afc_tournament_and_scrims.tests_flagged_bulk
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import User, SessionToken
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, Leaderboard,
    TournamentTeam, TournamentTeamMember, TournamentTeamMatchStats,
    TournamentPlayerMatchStats, MatchKillFlag,
)


class FlaggedKillsBulkTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="bulk_admin", email="bulk_admin@x.com", full_name="Bulk Admin", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token="bulk-admin-token",
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Bulk Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.lb = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})

        # Team A with two rostered players (9 rostered kills) + two ringer flags (3 + 2 kills).
        self.team_a = TournamentTeam.objects.create(
            event=self.event,
            team=Team.objects.create(team_name="Alpha", team_tag="ALP", join_settings="open",
                                     team_creator=self.admin, team_owner=self.admin, country="NG"),
            registered_by=self.admin)
        self.a_stat = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.team_a, placement=1, kills=9,
            damage=0, assists=0, placement_points=12, kill_points=9, total_points=21, played=True)
        for i, k in enumerate((5, 4)):
            u = User.objects.create(username=f"a{i}", email=f"a{i}@x.com", full_name=f"a{i}",
                                    role="player")
            TournamentPlayerMatchStats.objects.create(
                team_stats=self.a_stat, player=u, kills=k, played=True)
        self.f1 = MatchKillFlag.objects.create(
            match=self.match, tournament_team=self.team_a, uid="R1", name="Ringer1", kills=3,
            reason="name_matched_other_team", count_kills=None)
        self.f2 = MatchKillFlag.objects.create(
            match=self.match, tournament_team=self.team_a, uid="R2", name="Ringer2", kills=2,
            reason="name_matched_other_team", count_kills=None)

    def _bulk(self, body, token=None):
        return self.client.patch(
            "/events/flagged-kills/bulk/", data={"event_id": self.event.event_id, **body},
            format="json", HTTP_AUTHORIZATION=f"Bearer {token or self.token.token}")

    def test_bulk_reject_all_drops_every_ringer_in_one_call(self):
        resp = self._bulk({"flags": [
            {"flag_id": self.f1.id, "count_kills": False},
            {"flag_id": self.f2.id, "count_kills": False},
        ]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["flags_updated"], 2)
        self.f1.refresh_from_db(); self.f2.refresh_from_db()
        self.assertIs(self.f1.count_kills, False)
        self.assertIs(self.f2.count_kills, False)
        # Team total = rostered 9 + 0 counted ringer kills.
        self.a_stat.refresh_from_db()
        self.assertEqual(self.a_stat.kills, 9)

    def test_bulk_accept_all_counts_every_ringer(self):
        resp = self._bulk({"flags": [
            {"flag_id": self.f1.id, "count_kills": True},
            {"flag_id": self.f2.id, "count_kills": True},
        ]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.a_stat.refresh_from_db()
        self.assertEqual(self.a_stat.kills, 14)   # 9 rostered + 3 + 2 ringer

    def test_flag_from_another_event_is_skipped_not_applied(self):
        other = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Other Cup", event_mode="virtual",
            start_date=self.event.start_date, end_date=self.event.end_date,
            registration_open_date=self.event.start_date, registration_end_date=self.event.start_date,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/o", number_of_stages=1, creator=self.admin)
        o_stage = Stages.objects.create(
            event=other, stage_name="S", start_date=other.start_date, end_date=other.end_date,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1, stage_order=1)
        o_group = StageGroups.objects.create(
            stage=o_stage, group_name="G", playing_date=other.start_date,
            playing_time=datetime.time(0, 0), teams_qualifying=1, match_count=1)
        o_match = Match.objects.create(group=o_group, match_number=1, match_map="bermuda")
        o_team = TournamentTeam.objects.create(
            event=other,
            team=Team.objects.create(team_name="Zeta", team_tag="ZET", join_settings="open",
                                     team_creator=self.admin, team_owner=self.admin, country="NG"),
            registered_by=self.admin)
        foreign = MatchKillFlag.objects.create(
            match=o_match, tournament_team=o_team, uid="Z1", name="Z", kills=4,
            reason="not_on_roster", count_kills=None)

        # Bulk call scoped to self.event must NOT touch the foreign flag.
        resp = self._bulk({"flags": [{"flag_id": foreign.id, "count_kills": False}]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["flags_updated"], 0)
        self.assertEqual(len(resp.json()["skipped"]), 1)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.count_kills)   # untouched

    def test_requires_auth(self):
        resp = self.client.patch(
            "/events/flagged-kills/bulk/",
            data={"event_id": self.event.event_id, "flags": []}, format="json")
        self.assertIn(resp.status_code, (400, 401, 403))
