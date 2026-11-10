"""Live standings on capture/context ?standings=1 (owner 2026-11-10, inbox #518).

AFC LIVE's broadcast overlays read the group's points from the site instead of ranking by the game room's own
running score (owner rule 22 Sep 2026: the event and leaderboard models calculate, overlays read the result), and
the MVP of each map by the event's own MVP rule ("Mvp rule should be taken from the one on the website the
leaderboard"). These tests pin: the keys are opt-in (AFC Capture's roster fetch is unchanged), the standings are
exactly final_standings.official_group_standings, the points table and each map's stored points, and that the MVP
follows Event.mvp_config (a damage-first rule names a different player than the default kills-first one).

Fixture: one squad event, one group with two scored maps, two teams of two players with game UIDs, an upload token.
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.final_standings import official_group_standings
from afc_tournament_and_scrims.models import (
    Event, EventUploadToken, Leaderboard, Match, StageGroups, Stages, TournamentPlayerMatchStats, TournamentTeam,
    TournamentTeamMatchStats, TournamentTeamMember,
)

URL = "/events/capture/context/"
SCORING = {"placement_points": {"1": 12, "2": 9}, "kill_point": 1}


class CaptureLiveStandingsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(username="lsadmin", email="lsadmin@x.com", full_name="LS Admin",
                                         role="admin", password="x")
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Standings Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quarter Finals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today, playing_time=datetime.time(18, 0),
            teams_qualifying=2, match_count=2)
        lb = Leaderboard.objects.create(
            leaderboard_name="QF A", event=self.event, stage=self.stage, group=self.group, creator=self.admin,
            placement_points={"1": 12, "2": 9}, kill_point=1.0, leaderboard_method="manual")
        self.maps = [Match.objects.create(leaderboard=lb, group=self.group, match_number=n, match_map=name,
                                          scoring_settings=SCORING)
                     for n, name in ((1, "bermuda"), (2, "purgatory"))]
        self.tts, self.users = {}, {}
        for tag, base in (("ALPHA", 1000), ("BRAVO", 2000)):
            team = Team.objects.create(team_name=f"{tag} ESPORTS", team_tag=tag[:3], join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin)
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            self.tts[tag] = tt
            for i in (1, 2):
                u = User.objects.create(username=f"{tag}{i}", email=f"{tag}{i}@x.com", full_name=f"{tag}{i}",
                                        role="player", password="x", uid=str(base + i))
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)
                self.users[f"{tag}{i}"] = u
        # Map 1: BRAVO booyah (12 + 3 = 15), ALPHA 2nd (9 + 5 = 14).
        # Map 2: ALPHA booyah (12 + 6 = 18), BRAVO 2nd (9 + 2 = 11).  Totals: ALPHA 32, BRAVO 26.
        for m, rows in ((self.maps[0], {"ALPHA": (2, 5, 9), "BRAVO": (1, 3, 12)}),
                        (self.maps[1], {"ALPHA": (1, 6, 12), "BRAVO": (2, 2, 9)})):
            for tag, (place, kills, pp) in rows.items():
                ts = TournamentTeamMatchStats.objects.create(
                    match=m, tournament_team=self.tts[tag], placement=place, kills=kills,
                    placement_points=pp, kill_points=kills, total_points=pp + kills, played=True)
                # Player 1 of each team: more kills, less damage; player 2: fewer kills, most damage.
                k1 = kills - 1
                TournamentPlayerMatchStats.objects.create(team_stats=ts, player=self.users[f"{tag}1"], kills=k1,
                                                          damage=300, assists=0, played=True)
                TournamentPlayerMatchStats.objects.create(team_stats=ts, player=self.users[f"{tag}2"], kills=1,
                                                          damage=2500 if tag == "ALPHA" else 900, assists=1,
                                                          played=True)
        self.token = EventUploadToken.objects.create(event=self.event, created_by=self.admin, label="AFC LIVE")

    def _get(self, **params):
        resp = self.client.get(URL, params, HTTP_X_UPLOAD_TOKEN=self.token.token)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_opt_in_only(self):
        body = self._get(group=self.group.group_id)
        for key in ("standings", "scoring", "map_points", "mvp"):
            self.assertNotIn(key, body)
        self.assertIn("teams", body)                       # what AFC Capture reads is still there

    def test_standings_are_the_site_table(self):
        body = self._get(group=self.group.group_id, standings="1")
        site = official_group_standings(self.group)
        self.assertEqual([r["team_name"] for r in body["standings"]], [r["team_name"] for r in site])
        self.assertEqual([(r["rank"], r["team_name"], r["total"], r["booyahs"], r["kills"], r["games_played"])
                          for r in body["standings"]],
                         [(1, "ALPHA ESPORTS", 32, 1, 11, 2), (2, "BRAVO ESPORTS", 26, 1, 5, 2)])
        self.assertEqual(body["standings"][0]["placement_points"], 21)
        self.assertEqual(body["standings"][0]["kill_points"], 11)

    def test_points_table_and_each_map(self):
        body = self._get(group=self.group.group_id, standings="1")
        self.assertEqual(body["scoring"], {"placement": {"1": 12, "2": 9}, "kill": 1, "assist": 0,
                                           "per_1000_damage": 0})
        self.assertEqual([(m["match_number"], m["map"]) for m in body["map_points"]], [(1, "bermuda"), (2, "purgatory")])
        first = body["map_points"][0]["teams"]
        self.assertEqual([(t["team_name"], t["placement"], t["kills"], t["points"]) for t in first],
                         [("BRAVO ESPORTS", 1, 3, 15), ("ALPHA ESPORTS", 2, 5, 14)])

    def test_mvp_default_rule_is_kills_first(self):
        mvp = self._get(group=self.group.group_id, standings="1")["mvp"]
        self.assertEqual(mvp["criteria"][:2], ["kills", "damage"])
        # Map 1: ALPHA1 has 4 kills (most); map 2: ALPHA1 has 5.
        self.assertEqual([(m["match_number"], m["name"] or m["uid"], m["uid"], m["kills"]) for m in mvp["maps"]],
                         [(1, mvp["maps"][0]["name"], "1001", 4), (2, mvp["maps"][1]["name"], "1001", 5)])
        self.assertEqual(mvp["maps"][0]["team_name"], "ALPHA ESPORTS")
        self.assertEqual((mvp["top"]["uid"], mvp["top"]["mvp_count"]), ("1001", 2))

    def test_mvp_follows_the_events_saved_rule(self):
        # The organizer puts damage first on the leaderboard's MVPs tab: ALPHA2 (2,500 damage) wins both maps.
        Event.objects.filter(pk=self.event.pk).update(mvp_config={"criteria": ["damage", "kills"], "scope": "overall"})
        mvp = self._get(group=self.group.group_id, standings="1")["mvp"]
        self.assertEqual(mvp["criteria"], ["damage", "kills"])
        self.assertEqual([(m["uid"], m["damage"]) for m in mvp["maps"]], [("1002", 2500), ("1002", 2500)])

    def test_winning_team_scope(self):
        # Scope "winning_team": map 1's MVP comes from BRAVO (the booyah team) even though ALPHA1 had more kills.
        Event.objects.filter(pk=self.event.pk).update(mvp_config={"criteria": ["kills", "damage"],
                                                                  "scope": "winning_team"})
        mvp = self._get(group=self.group.group_id, standings="1")["mvp"]
        self.assertEqual(mvp["scope"], "winning_team")
        self.assertEqual(mvp["maps"][0]["team_name"], "BRAVO ESPORTS")

    def test_group_with_no_scored_map(self):
        TournamentTeamMatchStats.objects.all().delete()
        body = self._get(group=self.group.group_id, standings="1")
        self.assertIsNone(body["scoring"])
        self.assertEqual((body["standings"], body["map_points"], body["mvp"]["maps"], body["mvp"]["top"]),
                         ([], [], [], None))
