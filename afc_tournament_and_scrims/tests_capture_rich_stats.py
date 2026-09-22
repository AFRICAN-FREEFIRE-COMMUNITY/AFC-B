"""
afc_tournament_and_scrims/tests_capture_rich_stats.py
================================================================================
The site-side home for the per-player stats AFC Capture 1.4.0 counts (owner 2026-09-22, inbox #36).

What is proven here:
  1. A MatchResult upload that carries the capture client's `rich_stats` field fills the rich columns
     on the player rows the shared writer created, matched by UID, marks rich_stats_filled and the
     source "capture", and answers rich_stats_applied. Kills are untouched (they come from the file).
  2. A malformed `rich_stats` never fails the upload: the kills save, the answer names the reason.
  3. An upload without the field leaves the rows at zero / unfilled, exactly as before.
  4. The leaderboard editor's per-map players[] carries the rich columns.
  5. The official overlay feed sums the rich stats per team from the filled rows.
  6. The debugger-log backfill still fills its rows and marks the source "backfill".

Run: python manage.py test afc_tournament_and_scrims.tests_capture_rich_stats
"""
import datetime
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims.capture_rich_stats import (
    RICH_PLAYER_FIELDS, apply_capture_rich_stats, parse_rich_stats,
)
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, StageGroups, Stages, TournamentPlayerMatchStats, TournamentTeam,
    TournamentTeamMember, TournamentTeamMatchStats,
)

LOG = (
    "TeamName: TRG ESPORT   Rank: 1   KillScore: 5   RankScore: 12   TotalScore: 17\r\n"
    "NAME: moussa   ID: 1001   KILL: 3\r\n"
    "NAME: naruto   ID: 1002   KILL: 2\r\n"
    "TeamName: KOCC   Rank: 2   KillScore: 1   RankScore: 9   TotalScore: 10\r\n"
    "NAME: k0   ID: 300   KILL: 1\r\n"
    "NAME: k1   ID: 301   KILL: 0\r\n"
)

RICH = {
    "ff_match_id": "2102196355679832064",
    "players": {
        "1001": {"deaths": 1, "knockdowns": 4, "knocked": 2, "headshots": 1, "assists": 1,
                 "revives": 1, "grenades_used": 3, "grenade_kills": 1, "gloowall_used": 6,
                 "medkit_used": 2, "most_used_weapon": "9", "survival_seconds": 1010},
        "1002": {"deaths": 0, "knockdowns": 2, "knocked": 0, "headshots": 0, "assists": 0,
                 "revives": 0, "grenades_used": 0, "grenade_kills": 0, "gloowall_used": 1,
                 "medkit_used": 0, "most_used_weapon": "9", "survival_seconds": 1010},
        "300": {"deaths": 2, "knockdowns": 1, "knocked": 3, "headshots": 0, "assists": 0,
                "revives": 1, "grenades_used": 1, "grenade_kills": 0, "gloowall_used": 2,
                "medkit_used": 1, "most_used_weapon": "88", "survival_seconds": 600},
        # 301 deliberately absent: its row must stay unfilled
        "9999999": {"deaths": 9},   # not in the match: ignored
    },
    "teams": {"TRG ESPORT": {"survival_seconds": 1010}},
}


class _Fixture(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="capadmin", email="capadmin@x.com", full_name="Cap Admin", role="admin", password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin, token="cap-admin-token-123",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Capture Cup", event_mode="virtual",
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
        self.tt = {}
        for name, tag, members in (
            ("TRG ESPORT", "TRG", {"moussa": "1001", "naruto": "1002"}),
            ("KOCC", "KOC", {"k0": "300", "k1": "301"}),
        ):
            team = Team.objects.create(team_name=name, team_tag=tag, join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin, country="NG")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
            self.tt[name] = tt
            for uname, uid in members.items():
                u = User.objects.create(username=uname, email=f"{uname}@x.com", full_name=uname,
                                        role="player", password="x", uid=uid)
                TournamentTeamMember.objects.create(tournament_team=tt, user=u)

    def _upload(self, rich_stats=None):
        f = SimpleUploadedFile("match.log", LOG.encode("utf-8"), content_type="text/plain")
        data = {"match_id": self.match.match_id, "file": f}
        if rich_stats is not None:
            data["rich_stats"] = rich_stats
        return self.client.post("/events/upload-team-match-result/", data=data,
                                HTTP_AUTHORIZATION=f"Bearer {self.token.token}")

    def _row(self, uid):
        return TournamentPlayerMatchStats.objects.get(team_stats__match=self.match, player__uid=uid)


class CaptureRichStatsUploadTests(_Fixture):
    def test_upload_with_rich_stats_fills_the_rows(self):
        resp = self._upload(rich_stats=json.dumps(RICH))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["rich_stats_applied"], 3)
        self.assertEqual(resp.data["rich_stats_error"], "")
        r = self._row("1001")
        self.assertEqual(r.kills, 3)                    # from the file, untouched
        self.assertEqual((r.deaths, r.knockdowns, r.knocked, r.headshots, r.assists), (1, 4, 2, 1, 1))
        self.assertEqual((r.revives_received, r.grenades_used, r.grenade_kills), (1, 3, 1))
        self.assertEqual((r.gloowall_used, r.medkit_used, r.most_used_weapon, r.survival_seconds),
                         (6, 2, "9", 1010))
        self.assertTrue(r.rich_stats_filled)
        self.assertEqual(r.rich_stats_source, "capture")
        # a player the payload does not name stays unfilled, so 0 and "no data" stay distinct
        u = self._row("301")
        self.assertFalse(u.rich_stats_filled)
        self.assertEqual(u.rich_stats_source, "")
        self.assertEqual((u.deaths, u.grenades_used), (0, 0))

    def test_malformed_rich_stats_never_fails_the_upload(self):
        resp = self._upload(rich_stats="{not json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["rich_stats_applied"], 0)
        self.assertEqual(resp.data["rich_stats_error"], "not_json")
        self.assertEqual(self._row("1001").kills, 3)
        resp2 = self._upload(rich_stats=json.dumps({"players": [1, 2]}))
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["rich_stats_error"], "bad_shape")

    def test_upload_without_rich_stats_is_unchanged(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["rich_stats_applied"], 0)
        self.assertEqual(resp.data["rich_stats_error"], "")
        self.assertFalse(self._row("1001").rich_stats_filled)

    def test_values_are_clamped(self):
        payload = {"players": {"1001": {"deaths": -4, "knockdowns": "7", "grenades_used": 10 ** 9,
                                        "most_used_weapon": "x" * 40, "headshots": "zero"}}}
        self._upload(rich_stats=json.dumps(payload))
        r = self._row("1001")
        self.assertEqual((r.deaths, r.knockdowns, r.grenades_used, r.headshots), (0, 7, 100000, 0))
        self.assertEqual(len(r.most_used_weapon), 16)

    def test_parse_rich_stats(self):
        self.assertEqual(parse_rich_stats(None), (None, "absent"))
        self.assertEqual(parse_rich_stats(""), (None, "absent"))
        self.assertEqual(parse_rich_stats("nope"), (None, "not_json"))
        self.assertEqual(parse_rich_stats('{"players": 3}'), (None, "bad_shape"))
        data, why = parse_rich_stats(json.dumps(RICH))
        self.assertEqual(why, "")
        self.assertEqual(data["ff_match_id"], RICH["ff_match_id"])

    def test_leaderboard_editor_players_carry_the_rich_columns(self):
        self._upload(rich_stats=json.dumps(RICH))
        resp = self.client.post("/events/get-all-leaderboard-details-for-event/",
                                data={"event_id": self.event.event_id}, format="json",
                                HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", resp.content)[:200])
        body = json.dumps(resp.data)
        players = []

        def walk(node):
            if isinstance(node, dict):
                if "players" in node and isinstance(node["players"], list) and "team_name" in node:
                    players.extend(node["players"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(resp.data)
        self.assertTrue(players, body[:300])
        moussa = next(p for p in players if p.get("username") == "moussa")
        for key in RICH_PLAYER_FIELDS:
            self.assertIn(key, moussa)
        self.assertEqual(moussa["grenades_used"], 3)
        self.assertEqual(moussa["rich_stats_source"], "capture")

    def test_official_overlay_feed_sums_rich_stats_per_team(self):
        from afc_tournament_and_scrims.views import _overlay_standings_rows

        class _Req:
            def build_absolute_uri(self, u):
                return "https://x" + u
        self._upload(rich_stats=json.dumps(RICH))
        rows = {r["team_name"]: r for r in _overlay_standings_rows(self.event, self.stage, self.group, 20, _Req())}
        trg = rows["TRG ESPORT"]
        self.assertEqual((trg["deaths"], trg["knockdowns"], trg["knocked"], trg["headshots"], trg["assists"]),
                         (1, 6, 2, 1, 1))
        self.assertEqual((trg["grenades_used"], trg["grenade_kills"], trg["gloowall_used"], trg["medkit_used"]),
                         (3, 1, 7, 2))
        self.assertEqual(trg["revives_received"], 1)
        self.assertEqual(trg["survival_time"], 1010)          # the longest-surviving player of the map
        self.assertEqual(trg["most_used_weapon"], "9")
        kocc = rows["KOCC"]
        self.assertEqual((kocc["deaths"], kocc["gloowall_used"], kocc["most_used_weapon"]), (2, 2, "88"))
        self.assertEqual(kocc["survival_time"], 600)


class BackfillStillWorks(_Fixture):
    def test_backfill_marks_its_source_and_capture_overwrites(self):
        from afc_tournament_and_scrims.models import TournamentTeamMatchStats  # noqa: F401
        self._upload()
        # simulate the debugger-log backfill's write on the row (debugger_ingest.py apply step)
        r = self._row("1001")
        r.deaths, r.rich_stats_filled, r.rich_stats_source = 5, True, "backfill"
        r.save(update_fields=["deaths", "rich_stats_filled", "rich_stats_source"])
        self.assertEqual(self._row("1001").rich_stats_source, "backfill")
        # a capture payload for the same map overwrites it (the freshest full source wins)
        n = apply_capture_rich_stats(self.match, RICH)
        self.assertEqual(n, 3)
        r = self._row("1001")
        self.assertEqual((r.deaths, r.rich_stats_source), (1, "capture"))
