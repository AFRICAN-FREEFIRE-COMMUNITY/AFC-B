"""A team marked as NOT PLAYED must survive the round trip to the browser.

WHY THIS FILE EXISTS (owner report 2026-08-27)
    `TournamentTeamMatchStats.played` was written but never READ. Every per-match stats payload
    built it with a `.values()` list that left the column out, so "this team did not play" never
    reached the frontend at all.

    The visible symptom, reproduced on a real 14-team map before the fix: mark two teams as not
    playing, save, reopen the map. Both come back TICKED as played with no finishing position, and
    the save is then refused ("No finishing position entered for: ...") until the organizer unticks
    them again. Every single time the map is opened.

    Nothing in the suite noticed, because nothing asserted on the SHAPE of the stats payload; the
    tests that existed all went through code that never looked at `played`.

WHAT IS COVERED
    1. the round trip itself, through the real endpoint, with a real not-played row;
    2. that a PLAYED team still reports played=True, so the fix is not "always False";
    3. that every copy of the payload exposes the key, via tools/check_played_in_stats.py, so a
       fourth copy cannot be added without it.

    Test 1 is the one that would have failed before the fix.

Run: AFC_TEST_DB_NAME=test_afc_played python manage.py test afc_tournament_and_scrims.test_played_round_trip
"""
import json
import subprocess
import sys
from datetime import date, time, timedelta
from pathlib import Path

from django.test import Client, TestCase, override_settings

from afc_auth.models import SessionToken, User, UserProfile
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Match,
    StageGroups,
    Stages,
    TournamentTeam,
    TournamentTeamMatchStats,
)


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria", uid=None,
    )
    UserProfile.objects.create(user=u)
    return u, SessionToken.objects.create(user=u, token=f"tok_{username}").token


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class PlayedSurvivesTheRoundTripTests(TestCase):
    def setUp(self):
        self.admin, self.token = _user("playedadmin", role="admin")
        self.event = Event.objects.create(
            event_name="Played Round Trip Cup", slug="played-round-trip-cup",
            competition_type="tournament", participant_type="squad",
            event_type="online", event_mode="single",
            max_teams_or_players=16, number_of_stages=1,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
            registration_open_date=date.today() - timedelta(days=1),
            registration_end_date=date.today(),
            creator=self.admin,
        )
        stage = Stages.objects.create(
            event=self.event, stage_name="Stage 1",
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
            number_of_groups=1, stage_format="battle_royale",
            teams_qualifying_from_stage=4,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="Group 1",
            playing_date=date.today() + timedelta(days=1),
            playing_time=time(18, 0), teams_qualifying=4, match_count=1,
        )
        self.match = Match.objects.create(group=group, match_number=1, match_map="Bermuda")

        # Two competitors on the same map: one turned up, one did not.
        self.played_tt = self._competitor("TURNED UP", placement=1, played=True)
        self.absent_tt = self._competitor("DID NOT TURN UP", placement=0, played=False)

    def _competitor(self, team_name, placement, played):
        owner, _ = _user(f"owner{team_name.replace(' ', '').lower()}")
        team = Team.objects.create(
            team_name=team_name, team_owner=owner, team_creator=owner,
            country="Nigeria", join_settings="open",
        )
        tt = TournamentTeam.objects.create(event=self.event, team=team)
        TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=tt, placement=placement, played=played,
        )
        return tt

    def _stats_rows(self, path, **body):
        resp = Client().post(
            path, data=json.dumps({"slug": self.event.slug, **body}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        payload = resp.json()
        details = payload.get("event_details", payload)
        rows = []
        for stage in details.get("stages", []):
            for group in stage.get("groups", []):
                for match in group.get("matches", []):
                    rows.extend(match.get("stats", []))
        return {r["tournament_team_id"]: r for r in rows}

    # ── the regression test for the reported bug ──────────────────────────────────────────────
    def test_a_not_played_team_is_serialised_as_not_played(self):
        """THE ONE THAT WOULD HAVE FAILED. Before the fix `played` was absent from every row, so the
        frontend defaulted it to True and the organizer had to untick the team again on every open."""
        rows = self._stats_rows("/events/get-event-details/")
        absent = rows[self.absent_tt.tournament_team_id]
        self.assertIn("played", absent, "the stats payload does not carry `played` at all")
        self.assertFalse(absent["played"])

    def test_a_played_team_is_still_serialised_as_played(self):
        """The guard against a fix that simply always answers False."""
        rows = self._stats_rows("/events/get-event-details/")
        self.assertTrue(rows[self.played_tt.tournament_team_id]["played"])

    def test_the_PUBLIC_payload_agrees_with_the_logged_in_one(self):
        """A map must not read differently depending on who is looking at it. The public reader is a
        hand-copied duplicate of the logged-in one, which is exactly why it drifted."""
        rows = self._stats_rows("/events/get-event-details-not-logged-in/")
        self.assertFalse(rows[self.absent_tt.tournament_team_id]["played"])
        self.assertTrue(rows[self.played_tt.tournament_team_id]["played"])


class PlayedIsExposedEverywhereTests(TestCase):
    """Run the standalone checker in CI, so a FOURTH copy of the payload cannot be added without
    the key. Prose did not stop this happening three times; a failing check will."""

    def test_every_per_match_stats_payload_exposes_played(self):
        script = Path(__file__).resolve().parent.parent / "tools" / "check_played_in_stats.py"
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK.", result.stdout)
