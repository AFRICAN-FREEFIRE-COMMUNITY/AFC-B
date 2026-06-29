"""
afc_tournament_and_scrims/tests_name_matching.py
================================================================================
Tests for the NAME-BASED team + player matching feature in upload_team_match_result
(owner 2026-06-29, design tasks/name-matching-design.md sections 1 + 5).

What it covers (test-plan items 1-10):
  1.  A team resolvable ONLY by name (no UID hits) is ADOPTED -> gets a
      TournamentTeamMatchStats row (placement points) instead of being skipped.
  2.  A player whose UID changed but whose NAME matches THIS team's roster becomes a
      `name_matched_uid_changed` flag with count_kills=False (PENDING), EXCLUDED from
      the team total at upload; the response row carries matched_user_id/flag_id/scope.
  3.  Approving that flag (PATCH events/flagged-kills/flag/ {count_kills:true}) recomputes
      the team total = rostered + the flag's kills; rejecting (false) backs it out.
  4.  A name that matches a member on a DIFFERENT team -> `name_matched_other_team`,
      pending, carrying the other team's id/name.
  5.  An existing-UID cross-team player (`belongs_to_other_team`) is now count_kills=False
      (deliberate behavior change, requirement c).
  6.  A collision (two roster members normalize to the same name) -> not_on_roster (no
      auto-pick), uncounted.
  7.  Dedup: the same member by old UID + new UID counts ONCE (credited_user_ids guard).
  8.  A re-upload PRESERVES a prior admin approval (the §1i snapshot/re-apply).
  9.  unique_together(match, tournament_team, uid) holds when a name-adopted team coexists
      with a UID-resolved block.
  10. Clan-tag / unicode normalization (behavioral: `_norm_pname` + the two-pass fallback).

These mirror the rolled-back-transaction upload tests in tests_log_attribution.py: build a
real Event/Stage/Group/Leaderboard/Match + TournamentTeam roster, POST a crafted .log to
/events/upload-team-match-result/ with a Bearer token, and assert on the saved rows / flags.

Run: python manage.py test afc_tournament_and_scrims.tests_name_matching
"""
import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from django.test import TestCase

from afc_auth.models import User, SessionToken
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, TournamentTeam, TournamentTeamMember,
    TournamentTeamMatchStats, TournamentPlayerMatchStats, Leaderboard, MatchKillFlag,
)


class NameMatchingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create(
            username="nm_admin", email="nm_admin@x.com", full_name="NM Admin",
            role="admin", password="x",
        )
        self.token = SessionToken.objects.create(
            user=self.admin, token="nm-admin-token-123",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        )

        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Name Match Cup", event_mode="virtual",
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

        # ALPHA: 3 plain members + "AlphaStar" (the one whose UID will "change" in the file).
        self.tt_alpha = self._register_team("ALPHA", "ALP")
        self.alpha_members = {}
        for name, uid in [("alpha0", "1001"), ("alpha1", "1002"),
                          ("alpha2", "1003"), ("AlphaStar", "1004")]:
            self.alpha_members[name] = self._add_member(self.tt_alpha, name, uid)

        # BETA: a second registered team so other-team / cross-team cases are realistic.
        self.tt_beta = self._register_team("BETA", "BET")
        self.beta_members = {}
        for name, uid in [("beta0", "2001"), ("beta1", "2002"),
                         ("beta2", "2003"), ("BetaWolf", "2004")]:
            self.beta_members[name] = self._add_member(self.tt_beta, name, uid)

    # ── helpers ──────────────────────────────────────────────────────────────────────────────────
    def _register_team(self, team_name, tag):
        team = Team.objects.create(
            team_name=team_name, team_tag=tag, join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )
        return TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)

    def _add_member(self, tt, username, uid):
        u = User.objects.create(
            username=username, email=f"{username}@x.com", full_name=username,
            role="player", password="x", uid=uid,
        )
        TournamentTeamMember.objects.create(tournament_team=tt, user=u)
        return u

    @staticmethod
    def _block(team_name, rank, players):
        # players: list of (name, uid, kills). KillScore/RankScore/TotalScore are cosmetic here.
        kill_total = sum(k for _, _, k in players)
        head = (f"TeamName: {team_name}  Rank: {rank}  KillScore: {kill_total}  "
                f"RankScore: 0  TotalScore: {kill_total}\n")
        body = "".join(f"NAME: {n}  ID: {u}  KILL: {k}\n" for n, u, k in players)
        return head + body

    def _upload(self, log_text, dry_run=False):
        f = SimpleUploadedFile("match.log", log_text.encode("utf-8"), content_type="text/plain")
        data = {"match_id": self.match.match_id, "file": f}
        if dry_run:
            data["dry_run"] = "true"
        return self.client.post(
            "/events/upload-team-match-result/", data=data,
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    def _approve(self, flag_id, count):
        return self.client.patch(
            "/events/flagged-kills/flag/",
            data={"flag_id": flag_id, "count_kills": count}, format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    # ── 1. team adopted by NAME only ───────────────────────────────────────────────────────────────
    def test_1_team_resolved_by_name_only_gets_stats_row(self):
        # ALPHA fielded an entirely off-roster (UID-unknown) lineup -> resolves by NAME, not UID.
        log = self._block("ALPHA", 1, [
            ("ghost1", "9001", 0), ("ghost2", "9002", 0),
            ("ghost3", "9003", 0), ("ghost4", "9004", 0),
        ])
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)

        rows = TournamentTeamMatchStats.objects.filter(match=self.match, tournament_team=self.tt_alpha)
        self.assertEqual(rows.count(), 1, "name-adopted team must get exactly one stats row")
        row = rows.first()
        self.assertEqual(row.placement, 1)
        self.assertEqual(row.placement_points, 12)  # placement credited even with no UID hits

        body = resp.json()
        self.assertIn("ALPHA", [t["team_name"] for t in body["roster_mismatch_teams"]])
        self.assertNotIn("ALPHA", body["missing_teams"])

    # ── 2. player UID changed, name matches THIS team ──────────────────────────────────────────────
    def test_2_name_matched_uid_changed_is_pending_and_excluded(self):
        # 3 real ALPHA UIDs (resolve the block) + "AlphaStar" under a NEW uid (real 1004 absent).
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1), ("alpha2", "1003", 1),
            ("AlphaStar", "8888", 5),
        ])
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)

        flag = MatchKillFlag.objects.get(match=self.match, uid="8888")
        self.assertEqual(flag.reason, "name_matched_uid_changed")
        self.assertEqual(flag.count_kills, False)            # explicit PENDING
        self.assertEqual(flag.kills, 5)
        self.assertEqual(flag.registered_user_id, self.alpha_members["AlphaStar"].user_id)

        # The 5 pending kills are EXCLUDED from the team total at upload (only the 4 rostered count).
        row = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.tt_alpha)
        self.assertEqual(row.kills, 4)

        # Response row carries the match metadata so the panel can approve inline.
        ur = next(r for r in resp.json()["unknown_uids"] if r["uid"] == "8888")
        self.assertEqual(ur["reason"], "name_matched_uid_changed")
        self.assertEqual(ur["matched_user_id"], self.alpha_members["AlphaStar"].user_id)
        self.assertEqual(ur["scope"], "same_team")
        self.assertIsNotNone(ur["flag_id"])
        self.assertEqual(resp.json()["pending_count"], 1)

    # ── 3. approve -> counts; reject -> backs out ─────────────────────────────────────────────────
    def test_3_approve_then_reject_recomputes_team_total(self):
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1), ("alpha2", "1003", 1),
            ("AlphaStar", "8888", 5),
        ])
        self.assertEqual(self._upload(log).status_code, 200)
        flag = MatchKillFlag.objects.get(match=self.match, uid="8888")

        # APPROVE -> 4 rostered + 5 approved = 9.
        self.assertEqual(self._approve(flag.id, True).status_code, 200)
        row = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.tt_alpha)
        self.assertEqual(row.kills, 9)

        # REJECT -> back to 4.
        self.assertEqual(self._approve(flag.id, False).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.kills, 4)

    # ── 4. name matches a member on ANOTHER team ──────────────────────────────────────────────────
    def test_4_name_matched_other_team(self):
        # ALPHA block resolves by UID; "BetaWolf" (uid unknown) matches BETA's roster, not ALPHA's.
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1), ("alpha2", "1003", 1),
            ("BetaWolf", "8889", 4),
        ])
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)

        flag = MatchKillFlag.objects.get(match=self.match, uid="8889")
        self.assertEqual(flag.reason, "name_matched_other_team")
        self.assertEqual(flag.count_kills, False)
        self.assertEqual(flag.registered_user_id, self.beta_members["BetaWolf"].user_id)

        ur = next(r for r in resp.json()["unknown_uids"] if r["uid"] == "8889")
        self.assertEqual(ur["reason"], "name_matched_other_team")
        self.assertEqual(ur["scope"], "other_team")
        self.assertEqual(ur["other_team_name"], "BETA")
        self.assertEqual(ur["other_team_id"], self.tt_beta.team_id)
        self.assertIsNotNone(ur["flag_id"])

    # ── 5. cross-team UID is now PENDING (deliberate change) ──────────────────────────────────────
    def test_5_belongs_to_other_team_now_pending(self):
        # ALPHA block resolves by UID + includes a BETA member by their REAL uid (2001).
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1), ("alpha2", "1003", 1),
            ("beta0", "2001", 3),
        ])
        self.assertEqual(self._upload(log).status_code, 200)
        flag = MatchKillFlag.objects.get(match=self.match, uid="2001")
        self.assertEqual(flag.reason, "belongs_to_other_team")
        self.assertEqual(flag.count_kills, False)  # was auto-counting; now pending per req (c)

        # And EXCLUDED from the team total until approved (only the 4 rostered count).
        row = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.tt_alpha)
        self.assertEqual(row.kills, 4)

    # ── 6. collision -> not_on_roster ─────────────────────────────────────────────────────────────
    def test_6_name_collision_falls_through_to_not_on_roster(self):
        # Two ALPHA members normalize to the SAME key ("progamer"); a file "ProGamer" is ambiguous.
        self._add_member(self.tt_alpha, "ProGamer", "1010")
        self._add_member(self.tt_alpha, "Pro.Gamer", "1011")
        # Turn the event default OFF so the uncounted assertion is unambiguous.
        self.event.count_flagged_kills = False
        self.event.save(update_fields=["count_flagged_kills"])

        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1),
            ("ProGamer", "9999", 7),
        ])
        self.assertEqual(self._upload(log).status_code, 200)

        flag = MatchKillFlag.objects.get(match=self.match, uid="9999")
        self.assertEqual(flag.reason, "not_on_roster")   # ambiguous -> no auto-pick
        self.assertIsNone(flag.count_kills)              # follows event default (now off)

        row = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.tt_alpha)
        self.assertEqual(row.kills, 3)  # 2 + 1 rostered; the ambiguous 7 is NOT counted

    # ── 7. dedup: old + new UID counts once ───────────────────────────────────────────────────────
    def test_7_dedup_same_member_old_and_new_uid(self):
        # AlphaStar's REAL uid (1004) appears first (credited), then a "new" uid 8888 under the same
        # name -> the name match must be SKIPPED (credited_user_ids guard) -> not a second credit.
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2),
            ("AlphaStar", "1004", 3),   # real UID -> credited to rostered
            ("AlphaStar", "8888", 5),   # new UID, same member -> must NOT name-match again
        ])
        self.assertEqual(self._upload(log).status_code, 200)

        # The 8888 row is NOT a name match (dedup guard) -> plain not_on_roster.
        flag = MatchKillFlag.objects.get(match=self.match, uid="8888")
        self.assertEqual(flag.reason, "not_on_roster")

        # AlphaStar is credited via UID exactly once (one player-stat row, kills 3).
        star_stats = TournamentPlayerMatchStats.objects.filter(
            team_stats__match=self.match, player=self.alpha_members["AlphaStar"],
        )
        self.assertEqual(star_stats.count(), 1)
        self.assertEqual(star_stats.first().kills, 3)

    # ── 8. re-upload preserves a prior approval ───────────────────────────────────────────────────
    def test_8_reupload_preserves_approval(self):
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1), ("alpha2", "1003", 1),
            ("AlphaStar", "8888", 5),
        ])
        self.assertEqual(self._upload(log).status_code, 200)
        flag = MatchKillFlag.objects.get(match=self.match, uid="8888")
        self.assertEqual(self._approve(flag.id, True).status_code, 200)

        row = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.tt_alpha)
        self.assertEqual(row.kills, 9)  # approved

        # Re-upload the SAME log -> the approval must survive the idempotent clear (§1i).
        self.assertEqual(self._upload(log).status_code, 200)
        flag2 = MatchKillFlag.objects.get(match=self.match, uid="8888")
        self.assertEqual(flag2.count_kills, True, "prior approval was wiped by re-upload")
        row2 = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.tt_alpha)
        self.assertEqual(row2.kills, 9, "team total did not reflect the preserved approval")

    # ── 9. name-adopted team coexists with a UID block (unique_together holds) ─────────────────────
    def test_9_name_adopted_team_coexists_with_uid_block(self):
        log = (
            self._block("ALPHA", 1, [("alpha0", "1001", 2), ("alpha1", "1002", 1)]) +
            self._block("BETA", 2, [("ghostA", "9001", 0), ("ghostB", "9002", 0)])
        )
        resp = self._upload(log)
        self.assertEqual(resp.status_code, 200, resp.content)
        # Each team scored exactly once; no IntegrityError on the flag unique_together.
        self.assertEqual(
            TournamentTeamMatchStats.objects.filter(match=self.match, tournament_team=self.tt_alpha).count(), 1)
        self.assertEqual(
            TournamentTeamMatchStats.objects.filter(match=self.match, tournament_team=self.tt_beta).count(), 1)

    # ── 10. clan-tag + unicode normalization (behavioral) ─────────────────────────────────────────
    def test_10a_clan_tag_two_pass_match(self):
        # Roster "ProGamer"; file "[XTR] ProGamer" -> pass 1 ("xtrprogamer") misses, pass 2 tail
        # ("ProGamer" -> "progamer") matches.
        self._add_member(self.tt_alpha, "ProGamer", "1010")
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1),
            ("[XTR] ProGamer", "7777", 4),
        ])
        self.assertEqual(self._upload(log).status_code, 200)
        flag = MatchKillFlag.objects.get(match=self.match, uid="7777")
        self.assertEqual(flag.reason, "name_matched_uid_changed")
        self.assertEqual(flag.count_kills, False)

    def test_10b_unicode_prefix_match(self):
        # Roster "Dragon"; file "龙 | Dragon" -> NFKD ascii-fold drops the CJK + "|" -> "dragon".
        self._add_member(self.tt_alpha, "Dragon", "1012")
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1),
            ("龙 | Dragon", "7778", 6),
        ])
        self.assertEqual(self._upload(log).status_code, 200)
        flag = MatchKillFlag.objects.get(match=self.match, uid="7778")
        self.assertEqual(flag.reason, "name_matched_uid_changed")
        self.assertEqual(flag.count_kills, False)

    # ── 11. ADVERSARIAL (HIGH): dedup is order-INDEPENDENT ─────────────────────────────────────────
    def test_11_dedup_reversed_order(self):
        # Same as test 7 but the CHANGED-uid row comes FIRST. The old incremental dedup only worked
        # when the real UID was seen first; the block_credited pre-pass must skip the name match
        # regardless of order, else approving 8888 double-credits AlphaStar (review HIGH 2026-06-29).
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2),
            ("AlphaStar", "8888", 5),   # changed UID FIRST
            ("AlphaStar", "1004", 3),   # real UID after -> already credits AlphaStar
        ])
        self.assertEqual(self._upload(log).status_code, 200)

        # 8888 must NOT be a name match (would open the double-credit-on-approval path).
        flag = MatchKillFlag.objects.get(match=self.match, uid="8888")
        self.assertEqual(flag.reason, "not_on_roster")
        self.assertNotEqual(flag.reason, "name_matched_uid_changed")

        # AlphaStar (the member) is credited exactly once via their real UID.
        star_stats = TournamentPlayerMatchStats.objects.filter(
            team_stats__match=self.match, player=self.alpha_members["AlphaStar"],
        )
        self.assertEqual(star_stats.count(), 1)
        self.assertEqual(star_stats.first().kills, 3)

    # ── 12. ADVERSARIAL (MED): same-team collision is NOT rerouted to an other-team match ──────────
    def test_12_same_team_collision_not_rerouted_to_other_team(self):
        # "Shadow" is on ALPHA TWICE (a same-team collision) AND once on BETA. A file "Shadow" must
        # stay not_on_roster (ambiguous on this team), never name_matched_other_team crediting BETA's
        # lookalike's kills to ALPHA on approval (review MED 2026-06-29).
        # Distinct usernames that all NORMALIZE to "shadow" (so the index keys collide).
        self._add_member(self.tt_alpha, "Shadow", "1020")
        self._add_member(self.tt_alpha, "Sh.adow", "1021")   # -> "shadow" too: same-team collision
        self._add_member(self.tt_beta, "Sha.dow", "2020")    # -> "shadow" on a DIFFERENT team
        log = self._block("ALPHA", 1, [
            ("alpha0", "1001", 2), ("alpha1", "1002", 1),
            ("Shadow", "9998", 4),
        ])
        self.assertEqual(self._upload(log).status_code, 200)
        flag = MatchKillFlag.objects.get(match=self.match, uid="9998")
        self.assertEqual(flag.reason, "not_on_roster")
        self.assertNotEqual(flag.reason, "name_matched_other_team")
