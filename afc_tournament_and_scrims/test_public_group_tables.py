"""One group table for every visitor (owner, inbox #226, 2026-10-10).

The public tournament page reads a group's standings from get_event_details when the visitor is
signed in and from get_event_details_not_logged_in when not. Each endpoint used to carry its own
copy of the table builder, and the signed-out copy had lost the step that lists teams drawn into a
group with no result yet: on DYNASTY CUP GRAND FINALS SSA a signed-in visitor saw Xrootz at 0 in the
Grand Finals and a signed-out visitor did not.

Both now call views._public_group_overall. These tests read the same group through both doors and
require the same table, so the two cannot drift apart again without a red test.
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Match, StageGroupCompetitor, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats,
)


class PublicGroupTablesTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = datetime.date.today()
        self.viewer = User.objects.create(
            username="pgt_viewer", email="pgt_viewer@x.com", full_name="Viewer", role="player",
            password="x")
        SessionToken.objects.create(
            user=self.viewer, token="pgt-viewer-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=24, event_name="Group Table Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="completed",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.viewer,
            is_public=True, is_draft=False, results_published=True)
        stage = Stages.objects.create(
            event=self.event, stage_name="Grand Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br", teams_qualifying_from_stage=1,
            stage_status="completed")
        self.group = StageGroups.objects.create(
            stage=stage, group_name="Final Lobby", playing_date=today, playing_time="18:00",
            teams_qualifying=1, match_count=1, match_maps=["bermuda"])
        match = Match.objects.create(group=self.group, match_number=1, match_map="bermuda",
                                     result_inputted=True)

        def team(name):
            t = Team.objects.create(team_name=name, team_tag=name[:3], join_settings="open",
                                    team_creator=self.viewer, team_owner=self.viewer, country="NG")
            tt = TournamentTeam.objects.create(event=self.event, team=t, status="active")
            StageGroupCompetitor.objects.create(stage_group=self.group, tournament_team=tt)
            return tt

        for name, placement, points in (("ALPHA", 1, 20), ("BRAVO", 2, 15)):
            TournamentTeamMatchStats.objects.create(
                match=match, tournament_team=team(name), placement=placement,
                placement_points=points, total_points=points)
        team("NO SHOW")  # drawn into the group, never played

    def _table(self, signed_in):
        url = "/events/get-event-details/" if signed_in else "/events/get-event-details-not-logged-in/"
        extra = {"HTTP_AUTHORIZATION": "Bearer pgt-viewer-token"} if signed_in else {}
        resp = self.client.post(url, data={"slug": self.event.slug},
                                content_type="application/json", **extra)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        event = body.get("event_details", body)
        group = event["stages"][0]["groups"][0]
        self.assertEqual(group["group_name"], "Final Lobby")
        return [((r.get("competitor_name") or r.get("tournament_team__team__team_name")),
                 r.get("total_points"), r.get("matches_played")) for r in group["overall_leaderboard"]]

    def test_signed_out_lists_a_drawn_team_with_no_result_at_zero(self):
        self.assertEqual(self._table(signed_in=False),
                         [("ALPHA", 20, 1), ("BRAVO", 15, 1), ("NO SHOW", 0, 0)])

    def test_signed_in_and_signed_out_see_the_same_table(self):
        self.assertEqual(self._table(signed_in=True), self._table(signed_in=False))
