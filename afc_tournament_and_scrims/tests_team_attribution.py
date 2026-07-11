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

    # ── 3. PERSISTENT block + attribute/unattribute from the panel endpoint ───────────────────────
    def test_persistent_block_attribute_and_unattribute(self):
        from afc_tournament_and_scrims.models import UnmatchedTeamBlock
        target = self._register("Phoenix Squad", ["8001", "8002", "8003", "8004"])
        log = (
            "TeamName: ZZZ UNKNOWN CLAN  Rank: 1  KillScore: 6  RankScore: 12  TotalScore: 18\n"
            "NAME: zz.one  ID: 5001  KILL: 4\n"
            "NAME: zz.two  ID: 5002  KILL: 2\n"
        )
        # Real upload (not dry-run) -> the unmatched block PERSISTS, unresolved (not scored yet).
        self.assertEqual(self._upload(log).status_code, 200)
        blk = UnmatchedTeamBlock.objects.get(match=self.match, team_name="ZZZ UNKNOWN CLAN")
        self.assertIsNone(blk.attributed_team_id)
        self.assertEqual((blk.kills, blk.placement), (6, 1))
        self.assertIsNone(self._team_stat(target))

        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token.token}"}
        # GET flagged-kills lists the block + the registered teams as options.
        g = self.client.get(f"/events/flagged-kills/?event_id={self.event.event_id}", **auth).json()
        self.assertTrue(any(u["block_id"] == blk.id for u in g["unmatched_teams"]))
        self.assertTrue(any(t["tournament_team_id"] == target.tournament_team_id for t in g["event_teams"]))

        # Attribute -> the team is scored (placement + kills).
        a = self.client.patch(
            "/events/flagged-kills/unmatched-team/",
            data={"block_id": blk.id, "tournament_team_id": target.tournament_team_id},
            format="json", **auth,
        )
        self.assertEqual(a.status_code, 200, a.content)
        st = self._team_stat(target)
        self.assertIsNotNone(st)
        self.assertEqual((st.placement, st.kills), (1, 6))

        # Unattribute (null) -> the attribution-only row is cleaned up.
        a2 = self.client.patch(
            "/events/flagged-kills/unmatched-team/",
            data={"block_id": blk.id, "tournament_team_id": None},
            format="json", **auth,
        )
        self.assertEqual(a2.status_code, 200, a2.content)
        self.assertIsNone(self._team_stat(target))

    # ── 4. MULTI-MAP: each map's flagged (off-roster) kills count on a fresh upload, NO toggle ─────
    # Reproduces the owner report "after deploy it still under-counted until I toggled count-flagged-
    # kills off/on". Three maps uploaded one after another (the multi-map flow) for a name-adopted team
    # whose players are all off-roster -> with count_flagged_kills True (default) every map must count
    # immediately, no toggle.
    def test_multimap_offroster_kills_count_without_toggle(self):
        from afc_tournament_and_scrims.models import Match, TournamentTeamMatchStats
        self.assertTrue(self.event.count_flagged_kills)  # default ON
        saints = self._register("The saints", ["7001", "7002", "7003", "7004"])
        lb = self.match.leaderboard
        m2 = Match.objects.create(leaderboard=lb, group=self.group, match_number=2, match_map="kalahari",
                                  scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})
        m3 = Match.objects.create(leaderboard=lb, group=self.group, match_number=3, match_map="purgatory",
                                  scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})

        def _log(kills):
            return (
                f"TeamName: the saint  Rank: 1  KillScore: {kills}  RankScore: 12  TotalScore: {kills + 12}\n"
                f"NAME: ts.alpha  ID: 9001  KILL: {kills}\n"
                f"NAME: ts.bravo  ID: 9002  KILL: 0\n"
                f"NAME: ts.charlie  ID: 9003  KILL: 0\n"
                f"NAME: ts.delta  ID: 9004  KILL: 0\n"
            )

        for m, k in [(self.match, 9), (m2, 23), (m3, 25)]:
            f = SimpleUploadedFile("m.log", _log(k).encode("utf-8"), content_type="text/plain")
            r = self.client.post(
                "/events/upload-team-match-result/",
                data={"match_id": m.match_id, "file": f},
                HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
            )
            self.assertEqual(r.status_code, 200, r.content)

        by_match = {s.match_id: s.kills for s in
                    TournamentTeamMatchStats.objects.filter(tournament_team=saints)}
        self.assertEqual(by_match.get(self.match.match_id), 9)
        self.assertEqual(by_match.get(m2.match_id), 23)
        self.assertEqual(by_match.get(m3.match_id), 25)

    # ── 5. ID: 0 SENTINEL — zero-UID players must NOT be collapsed (owner 2026-07-11) ───────────────
    # Free Fire can export EVERY player with ID: 0 (a "UID unknown" sentinel, not a real identity). All
    # four below are different players. Before the fix they all shared the key "0", so the per-block
    # de-dupe kept only the first and dropped the other three BEFORE they became counted flags -> the
    # team lost their kills even with count_flagged_kills ON (the DYNASTY CUP "RUSH POINT" map-6 report:
    # FORSE ESP 6->2, ALPHA WOLVES 5->1, etc.). Their names match this team's roster, so each is a
    # name_matched_uid_changed flag that counts under the default-ON toggle -> the team total must be 6.
    def test_zero_uid_players_are_not_collapsed(self):
        from afc_tournament_and_scrims.models import MatchKillFlag
        self.assertTrue(self.event.count_flagged_kills)  # default ON
        tt = self._register("Zero Squad", ["1111111111", "2222222222", "3333333333", "4444444444"])
        log = (
            "TeamName: Zero Squad  Rank: 1  KillScore: 6  RankScore: 12  TotalScore: 18\n"
            "NAME: Zero Squad_0  ID: 0  KILL: 2\n"
            "NAME: Zero Squad_1  ID: 0  KILL: 1\n"
            "NAME: Zero Squad_2  ID: 0  KILL: 2\n"
            "NAME: Zero Squad_3  ID: 0  KILL: 1\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        st = self._team_stat(tt)
        self.assertIsNotNone(st)
        self.assertEqual(st.placement, 1)
        # BUG regression guard: pre-fix this was 2 (only the first ID:0 player survived the de-dupe).
        self.assertEqual(st.kills, 6, "all four ID:0 players must count, not collapse to one")
        # Each zero-UID player becomes its OWN flag (distinct synthetic uid), not a single "0" row.
        self.assertEqual(MatchKillFlag.objects.filter(match=self.match, tournament_team=tt).count(), 4)

    # Mixed block: one real UID (rostered) + three ID: 0 (the FROZEN EMPIRE shape, 9 -> was 5).
    def test_zero_uid_mixed_with_real_uid(self):
        tt = self._register("Mix Squad", ["1111111111", "2222222222", "3333333333", "4444444444"])
        log = (
            "TeamName: Mix Squad  Rank: 1  KillScore: 9  RankScore: 12  TotalScore: 21\n"
            "NAME: Mix Squad_0  ID: 1111111111  KILL: 4\n"   # real UID -> rostered
            "NAME: Mix Squad_1  ID: 0  KILL: 1\n"
            "NAME: Mix Squad_2  ID: 0  KILL: 3\n"
            "NAME: Mix Squad_3  ID: 0  KILL: 1\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        st = self._team_stat(tt)
        self.assertIsNotNone(st)
        self.assertEqual(st.kills, 9, "rostered 4 + three ID:0 players (1+3+1) must all count")

    # ── 6. PLAYER-NAME PLURALITY team resolution (owner 2026-07-11) ─────────────────────────────────
    def _register_named(self, team_name, members):
        """Register a team with EXPLICIT (username, uid) members so the file names can be made to
        match (or not) the roster — used by the player-name resolution tests."""
        team = Team.objects.create(
            team_name=team_name, team_tag=team_name[:3], join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
        for uname, uid in members:
            u = User.objects.create(username=uname, email=f"{uid}@x.com", full_name=uname,
                                    role="player", password="x", uid=uid)
            TournamentTeamMember.objects.create(tournament_team=tt, user=u)
        return tt

    def test_team_resolved_by_player_name_plurality(self):
        # Abbreviated in-game team name + ALL ID:0 UIDs -> the block resolves by NEITHER UID nor team
        # name ("berserk gen" != registered "berserk generation"). It must be resolved from the PLAYERS'
        # NAMES (the owner's BERSERK GEN report). All four names sit on the roster -> the lineup names it.
        tt = self._register_named("Berserk Generation", [
            ("GEN.liamHIGH", "6111111111"), ("GEN.CRACKED", "6222222222"),
            ("GEN.SMITH", "6333333333"), ("GEN.roroHIGH", "6444444444"),
        ])
        log = (
            "TeamName: BERSERK GEN  Rank: 1  KillScore: 6  RankScore: 12  TotalScore: 18\n"
            "NAME: GEN.LiamHIGH  ID: 0  KILL: 3\n"   # case differs, still normalises to match
            "NAME: GEN.CRACKED   ID: 0  KILL: 1\n"
            "NAME: GEN.SMITH.    ID: 0  KILL: 1\n"   # trailing dot, still matches
            "NAME: GEN.roroHIGH  ID: 0  KILL: 1\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        st = self._team_stat(tt)
        self.assertIsNotNone(st, "BERSERK GEN must resolve to BERSERK GENERATION from its players' names")
        self.assertEqual(st.placement, 1)
        self.assertEqual(st.kills, 6, "all 4 name-matched players' kills count under the default toggle")
        self.assertNotIn("BERSERK GEN", resp.json().get("missing_teams", []))

    def test_player_name_plurality_needs_majority(self):
        # Only ONE file name coincidentally matches the roster (rest are strangers) -> below the
        # >=2 / half-the-lineup threshold -> the block is NOT grabbed; it stays unmatched (missing_teams).
        self._register_named("Berserk Generation", [
            ("GEN.liamHIGH", "6111111111"), ("GEN.CRACKED", "6222222222"),
            ("GEN.SMITH", "6333333333"), ("GEN.roroHIGH", "6444444444"),
        ])
        log = (
            "TeamName: RANDOM STRANGERS  Rank: 1  KillScore: 6  RankScore: 12  TotalScore: 18\n"
            "NAME: GEN.liamHIGH  ID: 0           KILL: 3\n"   # 1 coincidental name match
            "NAME: totally.new1  ID: 7111111111  KILL: 1\n"
            "NAME: totally.new2  ID: 7222222222  KILL: 1\n"
            "NAME: totally.new3  ID: 7333333333  KILL: 1\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("RANDOM STRANGERS", resp.json().get("missing_teams", []))

    def test_player_name_plurality_tiebreak_by_unique_teammates(self):
        # Two teams SHARE a pair of names (Alpha, Beta). A block whose lineup also carries Team A's
        # UNIQUE members (Gamma, Delta) resolves to A: A gets 4 votes, B only 2 -> the teammates break
        # the tie (owner: "compare with teammate names").
        a = self._register_named("Squad Alpha", [
            ("Alpha", "8101"), ("Beta", "8102"), ("Gamma", "8103"), ("Delta", "8104")])
        b = self._register_named("Squad Bravo", [
            ("Alpha.", "9101"), ("Beta.", "9102"), ("Epsilon", "9103"), ("Zeta", "9104")])
        log = (
            "TeamName: AAA UNKNOWN  Rank: 1  KillScore: 4  RankScore: 12  TotalScore: 16\n"
            "NAME: Alpha  ID: 0  KILL: 1\n"
            "NAME: Beta   ID: 0  KILL: 1\n"
            "NAME: Gamma  ID: 0  KILL: 1\n"
            "NAME: Delta  ID: 0  KILL: 1\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNotNone(self._team_stat(a), "unique teammates Gamma/Delta must break the tie to Squad Alpha")
        self.assertIsNone(self._team_stat(b), "Squad Bravo (only the 2 shared names) must NOT be grabbed")
        self.assertEqual(self._team_stat(a).kills, 4)

    def test_player_name_plurality_fully_ambiguous_abstains(self):
        # A block whose names are ENTIRELY shared between two teams (Alpha, Beta only) ties 2-2 -> no
        # strict winner -> the resolver abstains (NO auto-grab), leaving it for manual attribution.
        a = self._register_named("Squad Alpha", [
            ("Alpha", "8101"), ("Beta", "8102"), ("Gamma", "8103"), ("Delta", "8104")])
        b = self._register_named("Squad Bravo", [
            ("Alpha.", "9101"), ("Beta.", "9102"), ("Epsilon", "9103"), ("Zeta", "9104")])
        log = (
            "TeamName: SHARED CLAN  Rank: 1  KillScore: 2  RankScore: 12  TotalScore: 14\n"
            "NAME: Alpha  ID: 0  KILL: 1\n"
            "NAME: Beta   ID: 0  KILL: 1\n"
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(self._team_stat(a))
        self.assertIsNone(self._team_stat(b))
        self.assertIn("SHARED CLAN", resp.json().get("missing_teams", []),
                      "a fully-ambiguous lineup must stay unmatched, not be guessed")
