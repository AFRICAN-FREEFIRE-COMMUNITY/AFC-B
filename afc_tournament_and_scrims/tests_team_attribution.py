"""
Team-resolution in upload_team_match_result (owner 2026-06-30).

Two owner reports about a team in the .log that did NOT resolve to a registered team, so its whole
result (placement + kills) was dropped and the registered team showed 0:

  1.  SINGULAR/PLURAL name fold: in-game "the saint" must still resolve to the registered "The saints"
      (the exact normalized name missed only on a trailing 's'). The adopted team gets its
      TournamentTeamMatchStats row + its players' kills (count_flagged_kills is True by default and a
      fresh upload now recomputes, so the not-on-roster kills count immediately).
  2.  MANUAL ATTRIBUTION: a block that matches NO registered team is reported in `missing_teams` (so the
      admin is told), and re-uploading with team_attributions = { "<in-game name>": tournament_team_id }
      scores that block for the chosen team; left unmapped it stays dropped ("don't count").

These build a real event + upload real .log text through the endpoint; TestCase rolls every row back.
Run: python manage.py test afc_tournament_and_scrims.tests_team_attribution
"""
import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from django.test import TestCase

from afc_auth.models import User, SessionToken
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, TournamentTeam, TournamentTeamMember,
    TournamentTeamMatchStats, Leaderboard,
)


class TeamAttributionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="attr_admin", email="attr_admin@x.com", full_name="Attr Admin",
            role="admin", password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin, token="attr-admin-token-123",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Attr Cup", event_mode="virtual",
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

    def _register(self, team_name, uids):
        team = Team.objects.create(
            team_name=team_name, team_tag=team_name[:3], join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
        for i, uid in enumerate(uids):
            u = User.objects.create(
                username=f"{team_name}_{i}", email=f"{team_name}_{i}@x.com", full_name=f"p{i}",
                role="player", password="x", uid=uid,
            )
            TournamentTeamMember.objects.create(tournament_team=tt, user=u)
        return tt

    def _upload(self, log_text, **extra):
        f = SimpleUploadedFile("match.log", log_text.encode("utf-8"), content_type="text/plain")
        return self.client.post(
            "/events/upload-team-match-result/",
            data={"match_id": self.match.match_id, "file": f, **extra},
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    def _team_stat(self, tt):
        return TournamentTeamMatchStats.objects.filter(match=self.match, tournament_team=tt).first()

    # ── 1. SINGULAR / PLURAL name fold ────────────────────────────────────────────────────────────
    def test_singular_plural_name_is_adopted(self):
        # Registered "The saints" with roster UIDs that DON'T appear in the file (so it can't UID-resolve).
        saints = self._register("The saints", ["7001", "7002", "7003", "7004"])
        # In-game block "the saint" (singular + lowercase) with off-roster players who DID get kills.
        log = (
            "TeamName: the saint  Rank: 1  KillScore: 9  RankScore: 12  TotalScore: 21\n"
            "NAME: ts.alpha  ID: 9001  KILL: 5\n"
            "NAME: ts.bravo  ID: 9002  KILL: 4\n"
            "NAME: ts.charlie  ID: 9003  KILL: 0\n"
            "NAME: ts.delta  ID: 9004  KILL: 0\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        st = self._team_stat(saints)
        self.assertIsNotNone(st, "The saints should be adopted from the in-game name 'the saint'")
        self.assertEqual(st.placement, 1)
        # count_flagged_kills defaults True + the upload recomputes -> the off-roster kills count now.
        self.assertEqual(st.kills, 9)
        self.assertNotIn("the saint", resp.json().get("missing_teams", []))

    # ── 2. MANUAL ATTRIBUTION of a genuinely unmatched team ───────────────────────────────────────
    def test_unmatched_team_reported_then_attributed(self):
        target = self._register("Phoenix Squad", ["8001", "8002", "8003", "8004"])
        # A block that matches NO registered team by UID or name.
        log = (
            "TeamName: ZZZ UNKNOWN CLAN  Rank: 1  KillScore: 6  RankScore: 12  TotalScore: 18\n"
            "NAME: zz.one  ID: 5001  KILL: 4\n"
            "NAME: zz.two  ID: 5002  KILL: 2\n"
            "NAME: zz.three  ID: 5003  KILL: 0\n"
            "NAME: zz.four  ID: 5004  KILL: 0\n"
        )
        # First a dry-run preview: the admin is TOLD via missing_teams, and event_teams lists the options.
        preview = self._upload(log, dry_run="true")
        self.assertEqual(preview.status_code, 200, preview.content)
        body = preview.json()
        self.assertIn("ZZZ UNKNOWN CLAN", body.get("missing_teams", []))
        ids = {t["tournament_team_id"] for t in body.get("event_teams", [])}
        self.assertIn(target.tournament_team_id, ids)
        # No stat written on dry-run.
        self.assertIsNone(self._team_stat(target))

        # Now APPLY with the admin's attribution -> the block is scored for Phoenix Squad.
        import json
        applied = self._upload(
            log,
            team_attributions=json.dumps({"ZZZ UNKNOWN CLAN": target.tournament_team_id}),
        )
        self.assertEqual(applied.status_code, 200, applied.content)
        st = self._team_stat(target)
        self.assertIsNotNone(st, "the attributed team should be scored")
        self.assertEqual(st.placement, 1)
        self.assertEqual(st.kills, 6)
        self.assertNotIn("ZZZ UNKNOWN CLAN", applied.json().get("missing_teams", []))

    def test_unmatched_team_without_attribution_is_dropped(self):
        target = self._register("Phoenix Squad", ["8001", "8002", "8003", "8004"])
        log = (
            "TeamName: ZZZ UNKNOWN CLAN  Rank: 1  KillScore: 6  RankScore: 12  TotalScore: 18\n"
            "NAME: zz.one  ID: 5001  KILL: 4\n"
            "NAME: zz.two  ID: 5002  KILL: 2\n"
        )
        resp = self._upload(log)  # no team_attributions
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("ZZZ UNKNOWN CLAN", resp.json().get("missing_teams", []))
        self.assertIsNone(self._team_stat(target), "nothing should be scored without an attribution")
