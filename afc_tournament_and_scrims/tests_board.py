"""THE BOARD (owner 2026-11-11, inbox #538 #539 #540): one table every reader shows, the map in progress added
to it (never shown instead of it), per-player numbers that keep off-roster players, any choice of maps.

Fixture: one squad event, one group, three teams. ALPHA and BRAVO played maps 1 and 2; CHARLIE is seeded into the
group and has not played. Map 3 is the map in progress (AFC Capture pushes it through events/live/push/).
  Map 1: BRAVO 1st 3 kills (12+3 = 15), ALPHA 2nd 5 kills (9+5 = 14)
  Map 2: ALPHA 1st 6 kills (12+6 = 18), BRAVO 2nd 2 kills (9+2 = 11)   -> ALPHA 32, BRAVO 26, CHARLIE 0
"""
import datetime

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims import board
from afc_tournament_and_scrims.models import (
    Event, EventUploadToken, Leaderboard, Match, MatchKillFlag, StageGroupCompetitor, StageGroups, Stages,
    TournamentPlayerMatchStats, TournamentTeam, TournamentTeamMatchStats, TournamentTeamMember,
)
from afc_tournament_and_scrims.views_mvp import compute_top_killers

SCORING = {"placement_points": {"1": 12, "2": 9, "3": 8}, "kill_point": 1}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "board-tests"}}


@override_settings(CACHES=LOCMEM)
class BoardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.admin = User.objects.create(username="bdadmin", email="bdadmin@x.com", full_name="BD Admin",
                                         role="admin", password="x")
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Board Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin,
            overlay_token="ovtok-board")
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quarter Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today, playing_time=datetime.time(18, 0),
            teams_qualifying=2, match_count=3)
        lb = Leaderboard.objects.create(
            leaderboard_name="QF A", event=self.event, stage=self.stage, group=self.group, creator=self.admin,
            placement_points={"1": 12, "2": 9, "3": 8}, kill_point=1.0, leaderboard_method="manual")
        self.maps = [Match.objects.create(leaderboard=lb, group=self.group, match_number=n, match_map="bermuda",
                                          scoring_settings=SCORING) for n in (1, 2, 3)]
        self.tts, self.users = {}, {}
        for tag, base in (("ALPHA", 1000), ("BRAVO", 2000), ("CHARLIE", 3000)):
            team = Team.objects.create(team_name=f"{tag} ESPORTS", team_tag=tag[:3], join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin)
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            self.tts[tag] = tt
            StageGroupCompetitor.objects.create(stage_group=self.group, tournament_team=tt)
            for i in (1, 2):
                u = User.objects.create(username=f"{tag}{i}", email=f"{tag}{i}@x.com", full_name=f"{tag}{i}",
                                        role="player", password="x", uid=str(base + i))
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)
                self.users[f"{tag}{i}"] = u
        for m, rows in ((self.maps[0], {"ALPHA": (2, 5, 9), "BRAVO": (1, 3, 12)}),
                        (self.maps[1], {"ALPHA": (1, 6, 12), "BRAVO": (2, 2, 9)})):
            for tag, (place, kills, pp) in rows.items():
                ts = TournamentTeamMatchStats.objects.create(
                    match=m, tournament_team=self.tts[tag], placement=place, kills=kills, damage=1000,
                    placement_points=pp, kill_points=kills, total_points=pp + kills, played=True)
                TournamentPlayerMatchStats.objects.create(team_stats=ts, player=self.users[f"{tag}1"],
                                                          kills=kills - 1, damage=300, played=True)
                TournamentPlayerMatchStats.objects.create(team_stats=ts, player=self.users[f"{tag}2"], kills=1,
                                                          damage=700, assists=1, played=True)
        self.upload = EventUploadToken.objects.create(event=self.event, created_by=self.admin, label="Capture")
        SessionToken.objects.create(
            user=self.admin, token="bd-admin-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

    # ── helpers ──
    def push(self, rows, stage=True):
        body = {"event_id": self.event.event_id, "group_id": self.group.group_id, "standings": rows,
                "updated_at": "2026-11-11T19:00:00Z"}
        if stage:
            body["stage_id"] = self.stage.stage_id
        resp = self.client.post("/events/live/push/", body, format="json", HTTP_X_UPLOAD_TOKEN=self.upload.token)
        self.assertEqual(resp.status_code, 200, resp.content)

    def live_map(self, charlie_kills=4, wiped_bravo=False):
        """Map 3 in progress: CHARLIE leads on kills with 2 alive, ALPHA 1 kill, BRAVO 0 (wiped or not)."""
        return [
            {"team_name": "CHARLIE ESPORTS", "kills": charlie_kills, "eliminated": False, "elimination_order": None,
             "alive_count": 2, "roster_size": 2,
             "players": [{"name": "C1", "uid": "3001", "kills": charlie_kills, "alive": True}]},
            {"team_name": "alpha esports", "kills": 1, "eliminated": False, "elimination_order": None,
             "alive_count": 1, "roster_size": 2, "players": [{"name": "A1", "uid": "1001", "kills": 1}]},
            {"team_name": "BRAVO ESPORTS", "kills": 0, "eliminated": wiped_bravo,
             "elimination_order": 1 if wiped_bravo else None, "alive_count": 0 if wiped_bravo else 2,
             "roster_size": 2, "players": []},
        ]

    def final_map(self):
        """Map 3 over: BRAVO out first, ALPHA second, CHARLIE the last team standing (booyah)."""
        rows = self.live_map(wiped_bravo=True)
        rows[1].update({"eliminated": True, "elimination_order": 2, "alive_count": 0})
        return rows

    def score_map3(self):
        """The official result of map 3 lands (what AFC Capture's MatchResult upload writes)."""
        for tag, (place, kills, pp) in {"CHARLIE": (1, 4, 12), "ALPHA": (2, 1, 9), "BRAVO": (3, 0, 8)}.items():
            TournamentTeamMatchStats.objects.create(
                match=self.maps[2], tournament_team=self.tts[tag], placement=place, kills=kills,
                placement_points=pp, kill_points=kills, total_points=pp + kills, played=True)

    # ── official table ──
    def test_group_table_has_seeded_team_at_zero(self):
        rows = board.group_table(self.group)
        self.assertEqual([(r["team_name"], r["effective_total"]) for r in rows],
                         [("ALPHA ESPORTS", 32), ("BRAVO ESPORTS", 26), ("CHARLIE ESPORTS", 0)])

    def test_group_table_matches_the_admin_leaderboard(self):
        resp = self.client.post("/events/get-all-leaderboard-details-for-event/", {"event_id": self.event.event_id},
                                format="json", HTTP_AUTHORIZATION="Bearer bd-admin-token")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        text = resp.content.decode()
        # The admin page lists the same three teams in the same order with the same totals.
        order = [text.find(n) for n in ("ALPHA ESPORTS", "BRAVO ESPORTS", "CHARLIE ESPORTS")]
        self.assertTrue(all(i >= 0 for i in order), order)
        rows = board.group_table(self.group)
        self.assertEqual([r["team_name"] for r in rows], ["ALPHA ESPORTS", "BRAVO ESPORTS", "CHARLIE ESPORTS"])

    # ── live map ──
    def test_no_push_no_live(self):
        rows, meta = board.live_group_table(self.group)
        self.assertIsNone(meta)
        self.assertTrue(all(r["live"] is None and r["total_with_live"] == r["total"] for r in rows))

    def test_live_map_is_added_to_the_official_table(self):
        self.push(self.live_map())
        rows, meta = board.live_group_table(self.group)
        self.assertEqual(meta["map_number"], 3)
        got = {r["team_name"]: (r["total"], r["live"]["points"], r["total_with_live"]) for r in rows}
        # live placements: CHARLIE 1st (12 + 4), ALPHA 2nd (9 + 1), BRAVO 3rd (8 + 0)
        self.assertEqual(got, {"ALPHA ESPORTS": (32, 10, 42), "BRAVO ESPORTS": (26, 8, 34),
                               "CHARLIE ESPORTS": (0, 16, 16)})
        for r in rows:
            self.assertEqual(r["total_with_live"], r["total"] + r["live"]["points"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])

    def test_finished_map_is_not_counted_twice_after_its_upload(self):
        self.push(self.final_map())
        self.score_map3()
        self.push(self.final_map())                        # Capture keeps re-sending its last board
        rows, meta = board.live_group_table(self.group)
        self.assertIsNone(meta)
        self.assertEqual({r["team_name"]: r["total"] for r in rows},
                         {"ALPHA ESPORTS": 42, "BRAVO ESPORTS": 34, "CHARLIE ESPORTS": 16})

    def test_next_map_starts_live_again(self):
        self.push(self.final_map())
        self.score_map3()
        Match.objects.create(leaderboard=self.maps[0].leaderboard, group=self.group, match_number=4,
                             match_map="kalahari", scoring_settings=SCORING)
        self.push(self.live_map(charlie_kills=0))          # map 4 starts: kills went down
        _rows, meta = board.live_group_table(self.group)
        self.assertEqual(meta["map_number"], 4)

    def test_late_upload_of_the_previous_map_keeps_the_new_map_live(self):
        self.push(self.live_map(charlie_kills=2))          # map 3 in progress, several teams alive
        self.score_map3()                                  # a result lands while it is still played
        self.push(self.live_map(charlie_kills=3))
        _rows, meta = board.live_group_table(self.group)
        self.assertIsNotNone(meta)

    def test_overlay_follows_a_broadcast_group_with_the_map_in_progress(self):
        Event.objects.filter(pk=self.event.pk).update(broadcast_scope="group", broadcast_group_id=self.group.group_id)
        self.push(self.live_map())
        body = self.client.get("/events/overlay/feed/", {"token": "ovtok-board", "live": "1"}).json()
        self.assertTrue(body["live"])
        got = [(r["team_name"], r["total_points"], r["live_points"]) for r in body["standings"]]
        self.assertEqual(got, [("ALPHA ESPORTS", 42, 10), ("BRAVO ESPORTS", 34, 8), ("CHARLIE ESPORTS", 16, 16)])

    def test_overlay_official_when_nothing_is_live(self):
        body = self.client.get("/events/overlay/feed/", {"token": "ovtok-board", "live": "1",
                                                         "group": self.group.group_id}).json()
        self.assertFalse(body["live"])
        self.assertEqual([r["total_points"] for r in body["standings"]], [32, 26, 0])

    def test_capture_context_live_block(self):
        self.push(self.live_map())
        body = self.client.get("/events/capture/context/", {"group": self.group.group_id, "standings": "1"},
                               HTTP_X_UPLOAD_TOKEN=self.upload.token).json()
        self.assertEqual([r["total"] for r in body["standings"]], [32, 26, 0])      # official stays official
        live = body["live"]
        self.assertEqual(live["map_number"], 3)
        self.assertEqual([(r["team_name"], r["total_with_live"]) for r in live["standings"]],
                         [("ALPHA ESPORTS", 42), ("BRAVO ESPORTS", 34), ("CHARLIE ESPORTS", 16)])
        tk = {p["uid"]: (p["kills"], p["live_kills"], p["kills_with_live"]) for p in live["top_killers"]}
        self.assertEqual(tk["1001"], (9, 1, 10))          # ALPHA1: 4 + 5 official + 1 live
        self.assertEqual(tk["3001"], (0, 4, 4))           # CHARLIE's player, live only

    # ── players and maps ──
    def test_flagged_player_is_on_the_player_board_with_his_stats(self):
        ts = TournamentTeamMatchStats.objects.get(match=self.maps[0], tournament_team=self.tts["BRAVO"])
        MatchKillFlag.objects.create(match=self.maps[0], tournament_team=self.tts["BRAVO"], uid="777",
                                     name="SUB RINGER", kills=7, reason="not_on_roster",
                                     stats={"damage": 1900, "knockdowns": 5, "most_used_weapon": "M1014"})
        self.assertIsNotNone(ts)
        players = board.player_board(self.event, [self.maps[0].match_id])
        ringer = next(p for p in players if p["uid"] == "777")
        self.assertEqual((ringer["rank"], ringer["kills"], ringer["damage"], ringer["knockdowns"],
                          ringer["registered"], ringer["most_used_weapon"]), (1, 7, 1900, 5, False, "M1014"))
        killers = compute_top_killers(self.event, None, group_ids=[self.group.group_id])["players"]
        ringer = next(p for p in killers if p.get("uid") == "777")
        self.assertEqual((ringer["kills"], ringer["damage"], ringer["user_id"], ringer["registered"]),
                         (7, 1900, None, False))
        self.assertEqual([p["kills"] for p in killers[:2]], [9, 7])   # ALPHA1 (4 + 5), then the ringer

    def test_flag_that_does_not_count_stays_off(self):
        MatchKillFlag.objects.create(match=self.maps[0], tournament_team=self.tts["BRAVO"], uid="778",
                                     name="NOT COUNTED", kills=9, reason="not_on_roster", count_kills=False)
        self.assertFalse(any(p["uid"] == "778" for p in board.player_board(self.event, [self.maps[0].match_id])))

    def test_one_map_and_chosen_maps(self):
        one = board.team_board(self.event, [self.maps[1].match_id])
        self.assertEqual([(r["team_name"], r["total"], r["damage"]) for r in one],
                         [("ALPHA ESPORTS", 18, 1000), ("BRAVO ESPORTS", 11, 1000)])
        players = board.player_board(self.event, [self.maps[0].match_id])
        a2 = next(p for p in players if p["user_id"] == self.users["ALPHA2"].pk)
        self.assertEqual((a2["damage"], a2["matches"]), (700, 1))
        both = board.player_board(self.event, [m.match_id for m in self.maps[:2]])
        a2 = next(p for p in both if p["user_id"] == self.users["ALPHA2"].pk)
        self.assertEqual((a2["damage"], a2["matches"]), (1400, 2))

    # ── endpoint + export ──
    def test_board_endpoint_and_scope_checks(self):
        auth = {"HTTP_AUTHORIZATION": "Bearer bd-admin-token"}
        url = f"/events/{self.event.event_id}/board/"
        self.assertEqual(self.client.get(url).status_code, 401)
        first = self.client.get(url, **auth).json()          # no group: the groups to choose from
        self.assertEqual((first["scope"], first["teams"], [g["group_id"] for g in first["groups"]]),
                         (None, [], [self.group.group_id]))
        self.assertEqual([m["scored"] for m in first["groups"][0]["maps"]], [True, True, False])
        self.assertEqual(self.client.get(url, {"group_id": "x"}, **auth).json()["code"], "board_group_required")
        self.assertEqual(self.client.get(url, {"group_id": 999999}, **auth).status_code, 404)
        self.assertEqual(self.client.get(url, {"group_id": self.group.group_id, "match_ids": "999999"},
                                         **auth).json()["code"], "board_maps_invalid")
        body = self.client.get(url, {"group_id": self.group.group_id}, **auth).json()
        self.assertEqual(body["scope"]["match_ids"], [self.maps[0].match_id, self.maps[1].match_id])
        self.assertEqual([t["total"] for t in body["teams"]], [32, 26])
        self.assertIsNone(body["live"])
        self.push(self.live_map())
        body = self.client.get(url, {"group_id": self.group.group_id}, **auth).json()
        self.assertEqual(body["live"]["map_number"], 3)
        one = self.client.get(url, {"group_id": self.group.group_id, "match_ids": str(self.maps[1].match_id)},
                              **auth).json()
        self.assertIsNone(one["live"])                    # live only on the whole-group view
        self.assertFalse(one["scope"]["all_maps"])

    def test_export_csv(self):
        auth = {"HTTP_AUTHORIZATION": "Bearer bd-admin-token"}
        Team.objects.filter(pk=self.tts["BRAVO"].team_id).update(team_name="=HYPERLINK(1)")
        resp = self.client.get(f"/events/{self.event.event_id}/board/export/",
                               {"group_id": self.group.group_id, "match_ids": str(self.maps[1].match_id),
                                "kind": "teams"}, **auth)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("maps-2-teams.csv", resp["Content-Disposition"])
        text = resp.content.decode("utf-8-sig")
        self.assertTrue(text.startswith("Rank,Team,Country,Maps"))
        self.assertIn("'=HYPERLINK(1)", text)
        players = self.client.get(f"/events/{self.event.event_id}/board/export/",
                                  {"group_id": self.group.group_id, "kind": "players"}, **auth)
        self.assertIn("Damage", players.content.decode("utf-8-sig").splitlines()[0])
        self.assertEqual(self.client.get(f"/events/{self.event.event_id}/board/export/",
                                         {"group_id": self.group.group_id, "kind": "x"}, **auth).status_code, 400)
