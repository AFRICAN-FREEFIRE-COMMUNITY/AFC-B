"""The wrong-data guard on AUTOMATIC capture uploads (owner 2026-10-23, inbox #353).

What it guards against, measured on 19 Oct 2026: AFC Capture re-sent 38 old MatchResult files after a
read of the game folder, and a 17 Sept game was scored as event 383 Group B map 5 although 29 of its 44
players were not on that event's roster. The second sighting of "wrong data scored as a map" (the first
was complaint D, 5 July), so this file is the catcher (owner rule R28): every test below fails on the
code before the guard (the off-roster and old files were scored) and passes after it.

Fixture: one squad event starting TODAY, one group with one empty map slot, two registered teams with
four UIDs each (1001-1004, 2001-2004). Uploads go through the capture path (a group, no match_id).
"""
import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, PendingCaptureUpload, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats, TournamentTeamMember,
)

URL = "/events/upload-team-match-result/"


def _log(blocks):
    """A MatchResult text: blocks = [(team name, rank, [(name, uid, kills), ...]), ...]."""
    out = []
    for team, rank, players in blocks:
        kills = sum(k for _, _, k in players)
        out.append(f"TeamName: {team}  Rank: {rank}  KillScore: {kills}  RankScore: 0  TotalScore: {kills}\n")
        out += [f"NAME: {n}  ID: {u}  KILL: {k}\n" for n, u, k in players]
    return "".join(out)


class CaptureGuardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(username="capadmin", email="capadmin@x.com", full_name="Cap Admin",
                                         role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="cap-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        self.today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Guard Cup", event_mode="virtual",
            start_date=self.today, end_date=self.today, registration_open_date=self.today,
            registration_end_date=self.today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        stage = Stages.objects.create(
            event=self.event, stage_name="Groups", start_date=self.today, end_date=self.today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1)
        self.group = StageGroups.objects.create(
            stage=stage, group_name="Group B", playing_date=self.today, playing_time=datetime.time(18, 0),
            teams_qualifying=2, match_count=1)
        lb = Leaderboard.objects.create(
            leaderboard_name="GB", event=self.event, stage=stage, group=self.group, creator=self.admin,
            placement_points={"1": 12, "2": 9}, kill_point=1.0, leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})
        for tag, base in (("SKG", 1000), ("KZN", 2000)):
            team = Team.objects.create(team_name=f"{tag} ESPORTS", team_tag=tag, join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin, country="NG")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            for i in range(1, 5):
                u = User.objects.create(username=f"{tag}{i}", email=f"{tag}{i}@x.com", full_name=f"{tag}{i}",
                                        role="player", password="x", uid=str(base + i))
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)

    def _capture(self, text, name="MatchResult_2112293672160249856_{d}-22-39-48.log", **extra):
        name = name.format(d=self.today.strftime("%Y-%m-%d"))
        f = SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/plain")
        data = {"group": self.group.group_id, "file": f, **extra}
        return self.client.post(URL, data=data, HTTP_AUTHORIZATION="Bearer cap-admin-token")

    ROSTER_GAME = [("SKG ESPORTS", 1, [("a", "1001", 3), ("b", "1002", 1), ("c", "1003", 0), ("d", "1004", 2)]),
                   ("KZN ESPORTS", 2, [("e", "2001", 1), ("f", "2002", 0), ("g", "2003", 0), ("h", "2004", 1)])]
    FOREIGN_GAME = [("VENT ESPORTS", 1, [("p", "9001", 3), ("q", "9002", 1), ("r", "9003", 0), ("s", "9004", 2)]),
                    ("GROW ESPORTS", 2, [("t", "9101", 1), ("u", "9102", 0), ("v", "9103", 0), ("w", "1001", 1)])]

    # ── the guard ──
    def test_a_game_of_mostly_strangers_is_parked_not_scored(self):
        resp = self._capture(_log(self.FOREIGN_GAME))
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.json()["code"], "capture_parked_off_roster")
        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.match).exists())
        self.match.refresh_from_db()
        self.assertFalse(self.match.result_inputted)
        parked = PendingCaptureUpload.objects.get()
        self.assertEqual(parked.parsed_summary["parked_reason"], "off_roster")
        self.assertIn("VENT ESPORTS", parked.raw_payload["file_text"])

    def test_a_file_dated_before_the_event_is_parked(self):
        old = (self.today - datetime.timedelta(days=36)).strftime("%Y-%m-%d")
        resp = self._capture(_log(self.ROSTER_GAME), name=f"MatchResult_2100644077688768512_{old}-19-10-09.log")
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.json()["code"], "capture_parked_older_than_event")
        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.match).exists())

    def test_an_extra_map_marked_new_is_guarded_too(self):
        resp = self._capture(_log(self.FOREIGN_GAME), attribution="new")
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(Match.objects.filter(group=self.group).count(), 1, "no slot may be made for it")

    # ── what still scores ──
    def test_this_events_game_still_scores_into_the_next_slot(self):
        resp = self._capture(_log(self.ROSTER_GAME))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(TournamentTeamMatchStats.objects.filter(match=self.match).count(), 2)
        self.assertFalse(PendingCaptureUpload.objects.exists())

    def test_unknown_uids_are_not_counted_as_strangers(self):
        """ID 0 is the game's 'UID unknown' sentinel (observer exports): a lobby of zeros is not judged."""
        zeros = [("SKG ESPORTS", 1, [(n, "0", 1) for n in "abcd"]), ("KZN ESPORTS", 2, [(n, "0", 0) for n in "efgh"])]
        resp = self._capture(_log(zeros))
        self.assertNotEqual(resp.status_code, 202, resp.content)
        self.assertFalse(PendingCaptureUpload.objects.exists())

    def test_a_person_choosing_the_slot_is_not_second_guessed(self):
        """An explicit match_id (the website upload) is a decision, so the guard does not apply."""
        f = SimpleUploadedFile("match.log", _log(self.FOREIGN_GAME).encode("utf-8"), content_type="text/plain")
        resp = self.client.post(URL, data={"match_id": self.match.match_id, "file": f},
                                HTTP_AUTHORIZATION="Bearer cap-admin-token")
        self.assertNotEqual(resp.status_code, 202, resp.content)
        self.assertFalse(PendingCaptureUpload.objects.exists())
