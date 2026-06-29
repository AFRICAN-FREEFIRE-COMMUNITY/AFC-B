"""
afc_tournament_and_scrims/tests_log_attribution.py
================================================================================
Regression test for the duplicate-team-attribution bug in upload_team_match_result
(owner report 2026-06-29: "TRG ESPORT is counted twice + given way more scores").

ROOT CAUSE: a log-team block was resolved to a site team via the FIRST of its players
found on any roster, with NO guard against two blocks resolving to the SAME site team.
So when one of a team's rostered players was fielded inside ANOTHER (often unregistered)
team's in-game lineup, that foreign block ALSO got attributed to the first team -> a
second TournamentTeamMatchStats row in the match -> the team showed TWICE with an
inflated total.

FIX: each block resolves to the site team MOST of its players are rostered to (plurality),
and a site team is claimed by AT MOST ONE block per match (the strongest claim wins; the
weaker block is left unresolved -> its off-roster players are flagged, not counted).

This test reproduces the exact reported shape (a real team's player UID appears in a
foreign block) and asserts the team is scored EXACTLY ONCE with its own kills.

Run: python manage.py test afc_tournament_and_scrims.tests_log_attribution
"""
import datetime
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from django.test import TestCase

from afc_auth.models import User, SessionToken
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, TournamentTeam, TournamentTeamMember,
    TournamentTeamMatchStats, Leaderboard,
)


class LogDuplicateAttributionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="logadmin", email="logadmin@x.com", full_name="Log Admin",
            role="admin", password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin, token="log-admin-token-123",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )

        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Log Attr Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin,
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1,
        )
        lb = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual",
        )
        self.match = Match.objects.create(
            leaderboard=lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1},
        )

        # TRG team: 4 rostered members with UIDs. COBRA is the player who gets fielded elsewhere.
        self.trg_team = Team.objects.create(
            team_name="TRG ESPORT", team_tag="TRG", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.tt_trg = TournamentTeam.objects.create(
            event=self.event, team=self.trg_team, registered_by=self.admin,
        )
        self.trg_uids = {"moussa": "1001", "naruto": "1002", "papa": "1003", "cobra": "1099"}
        for name, uid in self.trg_uids.items():
            u = User.objects.create(
                username=f"trg_{name}", email=f"trg_{name}@x.com", full_name=name,
                role="player", password="x", uid=uid,
            )
            TournamentTeamMember.objects.create(tournament_team=self.tt_trg, user=u)

        # A second registered team so the event is realistic (not the source of the bug).
        self.other_team = Team.objects.create(
            team_name="KOCC", team_tag="KOC", join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        self.tt_other = TournamentTeam.objects.create(
            event=self.event, team=self.other_team, registered_by=self.admin,
        )
        for i in range(2):
            u = User.objects.create(
                username=f"kocc_{i}", email=f"kocc_{i}@x.com", full_name=f"k{i}",
                role="player", password="x", uid=f"30{i}",
            )
            TournamentTeamMember.objects.create(tournament_team=self.tt_other, user=u)

    def _upload(self, log_text):
        f = SimpleUploadedFile("match.log", log_text.encode("utf-8"), content_type="text/plain")
        return self.client.post(
            "/events/upload-team-match-result/",
            data={"match_id": self.match.match_id, "file": f},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    def test_team_scored_once_when_its_player_is_in_a_foreign_block(self):
        # The exact reported shape: TRG's real block (3 rostered players, majority) AND a foreign
        # "VOLTA E-SPORT" block whose lineup happens to include TRG's COBRA (uid 1099).
        log = (
            "TeamName: TRG ESPORT  Rank: 1  KillScore: 7  RankScore: 12  TotalScore: 19\n"
            "NAME: TRG-MOUSSA  ID: 1001  KILL: 3\n"
            "NAME: TRG-NARUTO  ID: 1002  KILL: 2\n"
            "NAME: TRG-PAPA  ID: 1003  KILL: 2\n"
            "NAME: ghost  ID: 8888  KILL: 0\n"
            "TeamName: VOLTA E-SPORT  Rank: 2  KillScore: 9  RankScore: 9  TotalScore: 18\n"
            "NAME: VT-KABUTO  ID: 2001  KILL: 4\n"
            "NAME: VT-METZO  ID: 2002  KILL: 2\n"
            "NAME: TRG-COBRA  ID: 1099  KILL: 3\n"
            "NAME: VT-DIOP  ID: 2003  KILL: 0\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)

        # TRG must be scored EXACTLY ONCE in this match (the bug created two rows).
        trg_rows = TournamentTeamMatchStats.objects.filter(
            match=self.match, tournament_team=self.tt_trg,
        )
        self.assertEqual(trg_rows.count(), 1, "TRG ESPORT was attributed more than once")

        # And with its OWN result (placement 1, its 3 rostered players' 7 kills), NOT inflated by
        # the foreign VOLTA block's kills.
        row = trg_rows.first()
        self.assertEqual(row.placement, 1)
        self.assertEqual(row.kills, 7)  # 3 + 2 + 2 (the foreign COBRA's 3 are NOT added here)

        # The foreign VOLTA block did NOT silently become a second team row for anyone registered.
        self.assertLessEqual(
            TournamentTeamMatchStats.objects.filter(match=self.match).count(), 2,
        )

    def test_normal_two_real_teams_still_score_independently(self):
        # Sanity: when two registered teams each field their OWN roster, both score once (no regression).
        log = (
            "TeamName: TRG ESPORT  Rank: 1  KillScore: 5  RankScore: 12  TotalScore: 17\n"
            "NAME: TRG-MOUSSA  ID: 1001  KILL: 3\n"
            "NAME: TRG-NARUTO  ID: 1002  KILL: 2\n"
            "TeamName: KOCC  Rank: 2  KillScore: 1  RankScore: 9  TotalScore: 10\n"
            "NAME: KOCC-0  ID: 300  KILL: 1\n"
            "NAME: KOCC-1  ID: 301  KILL: 0\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            TournamentTeamMatchStats.objects.filter(match=self.match, tournament_team=self.tt_trg).count(), 1,
        )
        self.assertEqual(
            TournamentTeamMatchStats.objects.filter(match=self.match, tournament_team=self.tt_other).count(), 1,
        )
