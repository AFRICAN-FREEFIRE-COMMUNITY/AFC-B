"""The broadcast pack keys on capture/context (owner 2026-11-04, inbox #484).

AFC LIVE's ten Tournament overlays fetch GET /events/capture/context/ with the event's capture key (the same
answer AFC Capture reads for its roster check) and draw each team's tag, logo and flag and each player's photo
and in-game role from it (LAYO/FREEFIRE/BOUNTY-LIVE tournament.fetch_pack). These tests pin the new keys, the
media audit's opt-outs, the ghost team (no tag, no logo) and that the keys Capture reads are unchanged.

Fixture: one squad event with one group, two AFC teams (tag, country, logo; club memberships with in-game
roles), one ghost team, and an upload token. Picture fields hold plain storage names, like the overlay tests,
so no file is written.
"""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import User, UserProfile
from afc_rankings.models import GhostTeam
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import (
    Event, EventMediaOptOut, EventUploadToken, StageGroups, Stages, TournamentTeam, TournamentTeamMember,
)

URL = "/events/capture/context/"


class CaptureContextPackTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(username="packadmin", email="packadmin@x.com", full_name="Pack Admin",
                                         role="admin", password="x")
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Pack Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Groups", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2, stage_order=1)
        StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today, playing_time=datetime.time(18, 0),
            teams_qualifying=2, match_count=1)
        self.teams = {}
        for tag, base, country, roles in (("SKG", 1000, "Nigeria", ["rusher", "sniper", None, "support"]),
                                          ("KZN", 2000, "GH", ["grenader", None, None, None])):
            team = Team.objects.create(team_name=f"{tag} ESPORTS", team_tag=tag, join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin, country=country,
                                       team_logo=f"teams_logos/{tag.lower()}.png")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            for i, role in enumerate(roles, 1):
                u = User.objects.create(username=f"{tag}{i}", email=f"{tag}{i}@x.com", full_name=f"{tag}{i}",
                                        role="player", password="x", uid=str(base + i))
                TeamMembers.objects.create(team=team, member=u, in_game_role=role)
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)
                if i == 1:
                    UserProfile.objects.create(user=u, esports_pic=f"esports_pictures/{tag.lower()}1.png")
            # The TeamMembers signal recomputes Team.country from its players (none set here), so the
            # stored value is written after the roster, without the signal.
            Team.objects.filter(pk=team.pk).update(country=country)
            self.teams[tag] = team
        ghost = GhostTeam.objects.create(team_name="GHOST FIVE", country="Ivory Coast", created_by=self.admin)
        TournamentTeam.objects.create(event=self.event, ghost_team=ghost, registered_by=self.admin)
        self.token = EventUploadToken.objects.create(event=self.event, created_by=self.admin, label="AFC LIVE")

    def _context(self):
        resp = self.client.get(URL, HTTP_X_UPLOAD_TOKEN=self.token.token)
        self.assertEqual(resp.status_code, 200, resp.content)
        return {t["team_name"]: t for t in resp.json()["teams"]}

    def test_team_pack_keys(self):
        teams = self._context()
        skg, kzn = teams["SKG ESPORTS"], teams["KZN ESPORTS"]
        self.assertEqual((skg["team_tag"], skg["country"], skg["country_code"]), ("SKG", "Nigeria", "NG"))
        self.assertEqual((kzn["team_tag"], kzn["country_code"]), ("KZN", "GH"))
        # Absolute, so a program on another PC can download it.
        self.assertTrue(skg["team_logo"].startswith("http://testserver/"), skg["team_logo"])
        self.assertIn("skg.png", skg["team_logo"])

    def test_player_pack_keys(self):
        players = {p["username"]: p for p in self._context()["SKG ESPORTS"]["players"]}
        self.assertEqual([players[f"SKG{i}"]["in_game_role"] for i in range(1, 5)], ["rusher", "sniper", "", "support"])
        self.assertTrue(players["SKG1"]["photo"].startswith("http://testserver/"))
        self.assertIn("skg1.png", players["SKG1"]["photo"])
        self.assertIsNone(players["SKG2"]["photo"])   # no esport image: null, never a placeholder

    def test_player_country_follows_the_site_flag_rule(self):
        """#500: each player's own country for AFC LIVE's player countries overlay: the login country first,
        else the profile country, else blank; the code is the ISO alpha-2 (None when unknown)."""
        User.objects.filter(username="SKG1").update(ip_country="Ghana", country="Nigeria")
        User.objects.filter(username="SKG2").update(ip_country="", country="Kenya")
        User.objects.filter(username="SKG3").update(ip_country="", country="")
        players = {p["username"]: p for p in self._context()["SKG ESPORTS"]["players"]}
        self.assertEqual((players["SKG1"]["country"], players["SKG1"]["country_code"]), ("Ghana", "GH"))
        self.assertEqual((players["SKG2"]["country"], players["SKG2"]["country_code"]), ("Kenya", "KE"))
        self.assertEqual(players["SKG3"]["country"], "")

    def test_media_audit_opt_outs_are_honoured(self):
        EventMediaOptOut.objects.create(event=self.event, kind="team_logo", team=self.teams["SKG"])
        skg1 = User.objects.get(username="SKG1")
        EventMediaOptOut.objects.create(event=self.event, kind="esports_image", user=skg1)
        teams = self._context()
        self.assertIsNone(teams["SKG ESPORTS"]["team_logo"])
        self.assertIsNotNone(teams["KZN ESPORTS"]["team_logo"])
        players = {p["username"]: p for p in teams["SKG ESPORTS"]["players"]}
        self.assertIsNone(players["SKG1"]["photo"])
        self.assertIsNotNone({p["username"]: p for p in teams["KZN ESPORTS"]["players"]}["KZN1"]["photo"])

    def test_ghost_team_has_name_and_country_only(self):
        ghost = self._context()["GHOST FIVE"]
        self.assertEqual((ghost["team_tag"], ghost["team_logo"]), ("", None))
        self.assertEqual((ghost["country"], ghost["country_code"]), ("Ivory Coast", "CI"))
        self.assertEqual(ghost["players"], [])

    def test_keys_capture_reads_are_unchanged(self):
        """AFC Capture's roster check reads team_name, players[].uid and players[].username: still there."""
        skg = self._context()["SKG ESPORTS"]
        self.assertEqual(sorted(p["uid"] for p in skg["players"]), ["1001", "1002", "1003", "1004"])
        for key in ("tournament_team_id", "team_id", "team_name", "status", "players"):
            self.assertIn(key, skg)
        for key in ("user_id", "username", "uid"):
            self.assertIn(key, skg["players"][0])

    def test_unknown_country_is_null_not_an_error(self):
        team = self.teams["KZN"]
        team.country = "Atlantis"
        team.save(update_fields=["country"])
        self.assertIsNone(self._context()["KZN ESPORTS"]["country_code"])
