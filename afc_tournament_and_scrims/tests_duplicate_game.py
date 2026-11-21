"""One game is scored once per event (inbox #623, owner 2026-11-18). The catcher for the duplicate map of 18 Nov.

What happened (nginx api log, 18 Nov 2026 UTC): AFC Capture's upload of the Day 3 first game waited 30 s and gave
up (499 at 18:28:51) while the server was still scoring MATCH 1; the retry found MATCH 1 scored and filled MATCH 2
with the same game, so the standings counted it twice. Every test below fails on the code before
duplicate_game.py (the second upload is scored into the next map) and passes after it.

Fixture: one squad event, one group with three empty maps, two registered teams with four UIDs each. Automatic
uploads go through the capture path (a group, no match_id), website uploads name the map (match_id).
"""
import datetime
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import board, duplicate_game
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, MatchResultLog, PendingCaptureUpload, StageGroups, Stages, TournamentTeam,
    TournamentTeamMatchStats, TournamentTeamMember,
)

URL = "/events/upload-team-match-result/"
GAME_A = "2123115915098374144"
GAME_B = "2123121882187874304"


def _log(blocks):
    """A MatchResult text: blocks = [(team name, rank, [(name, uid, kills), ...]), ...]."""
    out = []
    for team, rank, players in blocks:
        kills = sum(k for _, _, k in players)
        out.append(f"TeamName: {team}  Rank: {rank}  KillScore: {kills}  RankScore: 0  TotalScore: {kills}\n")
        out += [f"NAME: {n}  ID: {u}  KILL: {k}\n" for n, u, k in players]
    return "".join(out)


GAME_A_TEXT = _log([("SKG ESPORTS", 1, [("a", "1001", 3), ("b", "1002", 1), ("c", "1003", 0), ("d", "1004", 2)]),
                    ("KZN ESPORTS", 2, [("e", "2001", 1), ("f", "2002", 0), ("g", "2003", 0), ("h", "2004", 1)])])
GAME_B_TEXT = _log([("KZN ESPORTS", 1, [("e", "2001", 4), ("f", "2002", 2), ("g", "2003", 0), ("h", "2004", 1)]),
                    ("SKG ESPORTS", 2, [("a", "1001", 0), ("b", "1002", 1), ("c", "1003", 0), ("d", "1004", 0)])])


class _Fixture(TestCase):
    """The event, group, maps, teams and upload helpers; no tests of its own."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(username="dupadmin", email="dupadmin@x.com", full_name="Dup Admin",
                                         role="admin", password="x")
        SessionToken.objects.create(
            user=self.admin, token="dup-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        self.today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Dupe Cup", event_mode="virtual",
            start_date=self.today, end_date=self.today, registration_open_date=self.today,
            registration_end_date=self.today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Semis", start_date=self.today, end_date=self.today,
            number_of_groups=2, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1)
        self.group = self._group("Day 3: B+C")
        self.m1, self.m2, self.m3 = (self._map(self.group, n, name)
                                     for n, name in ((1, "bermuda"), (2, "nexterra"), (3, "purgatory")))
        for tag, base in (("SKG", 1000), ("KZN", 2000)):
            team = Team.objects.create(team_name=f"{tag} ESPORTS", team_tag=tag, join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin, country="NG")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            for i in range(1, 5):
                u = User.objects.create(username=f"{tag}{i}", email=f"{tag}{i}@x.com", full_name=f"{tag}{i}",
                                        role="player", password="x", uid=str(base + i))
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)

    def _group(self, name):
        group = StageGroups.objects.create(
            stage=self.stage, group_name=name, playing_date=self.today, playing_time=datetime.time(18, 0),
            teams_qualifying=2, match_count=3)
        self.lb = Leaderboard.objects.create(
            leaderboard_name=name, event=self.event, stage=self.stage, group=group, creator=self.admin,
            placement_points={"1": 12, "2": 9}, kill_point=1.0, leaderboard_method="manual")
        return group

    def _map(self, group, number, name):
        return Match.objects.create(leaderboard=self.lb, group=group, match_number=number, match_map=name,
                                    scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})

    def _name(self, game_id):
        return f"MatchResult_{game_id}_{self.today:%Y-%m-%d}-19-25-17.log"

    def _capture(self, text, name, group=None, **extra):
        f = SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/plain")
        data = {"group": (group or self.group).group_id, "file": f, **extra}
        return self.client.post(URL, data=data, HTTP_AUTHORIZATION="Bearer dup-admin-token")

    def _website(self, text, name, match):
        f = SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/plain")
        return self.client.post(URL, data={"match_id": match.match_id, "file": f},
                                HTTP_AUTHORIZATION="Bearer dup-admin-token")

    def _scored(self):
        return list(Match.objects.filter(group__stage__event=self.event, result_inputted=True)
                    .order_by("match_number", "match_id").values_list("match_id", flat=True))


class DuplicateGameTests(_Fixture):

    # ── the 18 Nov case ──
    def test_the_same_game_sent_again_is_refused_and_names_the_map(self):
        first = self._capture(GAME_A_TEXT, self._name(GAME_A))
        self.assertEqual(first.status_code, 200, first.content)
        again = self._capture(GAME_A_TEXT, self._name(GAME_A))
        self.assertEqual(again.status_code, 409, again.content)
        body = again.json()
        self.assertEqual(body["code"], "capture_duplicate_game")
        self.assertTrue(body["duplicate_game"])
        self.assertEqual(body["existing_match_id"], self.m1.match_id)
        self.assertEqual(body["existing_match_number"], 1)
        self.assertEqual(body["params"], {"match_number": 1, "map": "Bermuda", "group": "Day 3: B+C"})
        self.assertIn("MATCH 1 (Bermuda)", body["message"])
        self.assertEqual(self._scored(), [self.m1.match_id], "MATCH 2 must stay empty")
        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.m2).exists())
        self.assertEqual(MatchResultLog.objects.filter(match=self.m2).count(), 0)

    def test_the_next_game_still_fills_the_next_map(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        resp = self._capture(GAME_B_TEXT, self._name(GAME_B))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._scored(), [self.m1.match_id, self.m2.match_id])

    def test_the_same_game_is_refused_in_another_group_of_the_event(self):
        """18 Nov: the same game was parked under Day 2 and scored in Day 3. One game is one map per EVENT."""
        other = self._group("Day 2: A+B")
        other_map = self._map(other, 1, "bermuda")
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        resp = self._capture(GAME_A_TEXT, self._name(GAME_A), group=other)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertFalse(Match.objects.get(pk=other_map.pk).result_inputted)

    def test_a_renamed_file_or_a_game_id_of_0_is_still_recognised(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        renamed = self._capture(GAME_A_TEXT, "day 3 map 1.log")
        self.assertEqual(renamed.status_code, 409, renamed.content)
        zero = self._capture(GAME_A_TEXT, f"MatchResult_0_{self.today:%Y-%m-%d}-19-25-17.log")
        self.assertEqual(zero.status_code, 409, zero.content)
        self.assertEqual(self._scored(), [self.m1.match_id])

    def test_two_games_with_id_0_and_different_results_are_two_games(self):
        zero = f"MatchResult_0_{self.today:%Y-%m-%d}-19-25-17.log"
        self.assertEqual(self._capture(GAME_A_TEXT, zero).status_code, 200)
        self.assertEqual(self._capture(GAME_B_TEXT, zero).status_code, 200)
        self.assertEqual(self._scored(), [self.m1.match_id, self.m2.match_id])

    # ── a person's chosen map ──
    def test_the_website_may_re_upload_the_map_that_holds_the_game(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        resp = self._website(GAME_A_TEXT, self._name(GAME_A), self.m1)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._scored(), [self.m1.match_id])

    def test_the_website_may_not_put_the_game_into_a_second_map(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        resp = self._website(GAME_A_TEXT, self._name(GAME_A), self.m2)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["existing_match_id"], self.m1.match_id)
        self.assertFalse(Match.objects.get(pk=self.m2.pk).result_inputted)

    def test_a_map_re_uploaded_with_another_game_frees_the_first_game(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        self.assertEqual(self._website(GAME_B_TEXT, self._name(GAME_B), self.m1).status_code, 200)
        resp = self._capture(GAME_A_TEXT, self._name(GAME_A))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._scored(), [self.m1.match_id, self.m2.match_id])

    def test_a_cleared_map_frees_its_game(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        cleared = self.client.post("/events/clear-match-result/", {"match_id": self.m1.match_id},
                                   HTTP_AUTHORIZATION="Bearer dup-admin-token")
        self.assertEqual(cleared.status_code, 200, cleared.content)
        resp = self._capture(GAME_A_TEXT, self._name(GAME_A))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._scored(), [self.m1.match_id])

    def test_a_dry_run_preview_says_it_too(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        f = SimpleUploadedFile(self._name(GAME_A), GAME_A_TEXT.encode("utf-8"), content_type="text/plain")
        resp = self.client.post(URL, data={"match_id": self.m2.match_id, "file": f, "dry_run": "true"},
                                HTTP_AUTHORIZATION="Bearer dup-admin-token")
        self.assertEqual(resp.status_code, 409, resp.content)

    # ── the race: a retry arriving while the first request is still scoring ──
    def test_the_check_under_the_lock_refuses_what_the_early_check_missed(self):
        """The early check reads committed data, so on 18 Nov the retry saw nothing yet. Here the early check is
        made blind (it returns None once) to stand in for that; the check under the group lock must still refuse."""
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        real = duplicate_game.scored_holder
        calls = {"n": 0}

        def blind_once(*args, **kwargs):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(*args, **kwargs)

        with mock.patch.object(duplicate_game, "scored_holder", side_effect=blind_once):
            resp = self._capture(GAME_A_TEXT, self._name(GAME_A))
        self.assertEqual(calls["n"], 2, "checked once early and once under the lock")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(self._scored(), [self.m1.match_id])
        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.m2).exists())

    def test_an_extra_map_marked_new_leaves_no_empty_map_behind(self):
        for m in (self.m1, self.m2, self.m3):
            m.delete()
        only = self._map(self.group, 1, "bermuda")
        self.assertEqual(self._capture(GAME_A_TEXT, self._name(GAME_A)).status_code, 200)
        early = self._capture(GAME_A_TEXT, self._name(GAME_A), attribution="new")
        self.assertEqual(early.status_code, 409, early.content)
        real = duplicate_game.scored_holder
        calls = {"n": 0}

        def blind_once(*args, **kwargs):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(*args, **kwargs)

        with mock.patch.object(duplicate_game, "scored_holder", side_effect=blind_once):
            late = self._capture(GAME_A_TEXT, self._name(GAME_A), attribution="new")
        self.assertEqual(late.status_code, 409, late.content)
        self.assertEqual(list(Match.objects.filter(group=self.group).values_list("match_id", flat=True)),
                         [only.match_id], "the map made for the duplicate is removed again")

    # ── the pending bucket ──
    def test_decide_later_does_not_park_a_game_already_in(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        resp = self._capture(GAME_A_TEXT, self._name(GAME_A), attribution="pending")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertFalse(PendingCaptureUpload.objects.exists())

    def test_scoring_a_game_retires_its_parked_copy(self):
        parked = self._capture(GAME_A_TEXT, self._name(GAME_A), attribution="pending")
        self.assertEqual(parked.status_code, 202, parked.content)
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        p = PendingCaptureUpload.objects.get()
        self.assertEqual(p.status, "discarded")
        self.assertEqual(p.resolution, f"duplicate of match {self.m1.match_id}")
        self.assertEqual(p.resolved_match_id, self.m1.match_id)

    def test_resolving_a_parked_copy_of_a_scored_game_is_refused_with_its_code(self):
        parked = PendingCaptureUpload.objects.create(
            event=self.event, stage=self.stage, group=self.group, file_name=self._name(GAME_A),
            raw_payload={"file_text": GAME_A_TEXT, "file_name": self._name(GAME_A), "file_type": "",
                         "group_id": self.group.group_id}, status="pending")
        # scored through the website so the parked row is not retired by the capture path first
        self.assertEqual(self._website(GAME_A_TEXT, "map1.log", self.m1).status_code, 200)
        resp = self.client.post(f"/events/{self.event.event_id}/pending-captures/{parked.id}/resolve/",
                                {"attribution": "new", "group_id": self.group.group_id},
                                HTTP_AUTHORIZATION="Bearer dup-admin-token")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["code"], "capture_duplicate_game")
        parked.refresh_from_db()
        self.assertEqual(parked.status, "pending", "a refused resolve leaves it for the operator")
        self.assertEqual(Match.objects.filter(group=self.group).count(), 3, "no extra map made")

    # ── the helpers ──
    def test_game_id_from_name(self):
        self.assertEqual(duplicate_game.game_id_from_name(r"C:\x\MatchResult_212_2026-11-18-19-25-17.log"), "212")
        self.assertEqual(duplicate_game.game_id_from_name("MatchResult_0_2026-11-11-21-15-55.log"), "")
        self.assertEqual(duplicate_game.game_id_from_name("map1.log"), "")
        self.assertEqual(duplicate_game.game_id_from_name(None), "")


class LiveStateAfterClearTests(TestCase):
    """inbox #622: clearing a scored map during a live game hid the live board (scored_at_start only rose)."""

    def setUp(self):
        cache.clear()

    def test_note_cleared_lowers_never_raises(self):
        with mock.patch.object(board, "scored_map_count", return_value=2):
            cache.set(board.live_state_key(1, 9), {"scored_at_start": 3, "kills": 40, "wiped": 5})
            self.assertEqual(board.note_cleared(1, 9)["scored_at_start"], 2)
            cache.set(board.live_state_key(1, 9), {"scored_at_start": 1, "kills": 40, "wiped": 5})
            self.assertEqual(board.note_cleared(1, 9)["scored_at_start"], 1)
        self.assertIsNone(board.note_cleared(1, 10), "no state, nothing to do")


class LiveStateClearViewTests(_Fixture):
    """The views call note_cleared: the live board of the map being played survives a clear and a delete."""

    def _live(self):
        key = board.live_state_key(self.event.event_id, self.group.group_id)
        cache.set(key, {"scored_at_start": board.scored_map_count(self.group.group_id), "kills": 30, "wiped": 4})
        return key

    def test_clear_match_result_keeps_the_live_board(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        self._capture(GAME_B_TEXT, self._name(GAME_B))
        key = self._live()
        self.assertEqual(cache.get(key)["scored_at_start"], 2)
        self.client.post("/events/clear-match-result/", {"match_id": self.m2.match_id},
                         HTTP_AUTHORIZATION="Bearer dup-admin-token")
        self.assertEqual(cache.get(key)["scored_at_start"], 1)

    def test_delete_match_keeps_the_live_board(self):
        self._capture(GAME_A_TEXT, self._name(GAME_A))
        self._capture(GAME_B_TEXT, self._name(GAME_B))
        key = self._live()
        resp = self.client.post("/events/delete-match/", {"match_id": self.m2.match_id, "force": "true"},
                                HTTP_AUTHORIZATION="Bearer dup-admin-token")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(cache.get(key)["scored_at_start"], 1)
