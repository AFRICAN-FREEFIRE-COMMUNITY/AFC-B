r"""Saving a map by hand must not destroy that map's flagged / attributed kills.

THE BUG (found 2026-08-06 while verifying the manual-entry zero fix, reported by the team lead):
re-saving a map from the results grid DESTROYED unattributed kills. On the local copy of match 3723
a PLAIN, UNCHANGED "Save this map" rewrote NOOBZ Esports 3 -> 2 and CLIQ ESPORT 8 -> 7 and answered
HTTP 200. Nothing looked broken: the MatchKillFlag rows survived, so the kills were still on record,
the team totals just quietly got smaller every time somebody corrected a placement.

WHY. A team's kills are not the sum of its player rows. The canonical definition, and the only one,
is views._recompute_team_kills_for_event:

    team kills = rostered player rows + counting MatchKillFlags + attributed UnmatchedTeamBlocks

because a ringer, a returning player whose Free Fire UID changed, and a player line the FF client
dropped from the log all produce real kills with no roster row to hang them on. Both manual writers
(enter_team_match_result_manual via result_writes.write_team_result_row, and edit_match_result
inline) rebuilt the team row from the POSTED player rows alone, so every such kill was dropped.

THE SECOND BUG pinned here: edit_match_result created TournamentTeamMatchStats WITHOUT `played`,
falling back to the model default True, so a team the admin explicitly unticked was stored as having
played. write_team_result_row has always set it. The two writers disagreed for identical input.

Run: .venv\Scripts\python.exe manage.py test afc_tournament_and_scrims.tests_manual_save_keeps_flagged_kills
"""
import datetime
import json

from django.test import TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    MatchKillFlag,
    Stages,
    StageGroups,
    TournamentPlayerMatchStats,
    TournamentTeam,
    TournamentTeamMatchStats,
)

EDIT_URL = "/events/edit-match-result/"
MANUAL_URL = "/events/enter-team-match-result-manual/"
TOKEN = "manual-save-flagged-kills-token"


class ManualSaveKeepsFlaggedKillsTests(TestCase):
    """Drives the two real endpoints and reads the stored rows back.

    CONNECTS TO: the admin results grid (app/(a)/a/leaderboards/[id]/edit/page.tsx), the organizer
    grid (app/(organizer)/organizer/events/[slug]/leaderboard/page.tsx) and GroupResultsEditor.tsx,
    all of which POST the payloads built below whenever someone clicks "Save this map".
    """

    def setUp(self):
        today = datetime.date.today()

        self.admin = User.objects.create(
            username="flagkeep_admin", email="flagkeep_admin@x.com",
            full_name="Flag Keep Admin", role="admin", password="x")
        # SessionToken does NOT generate a token by itself; it has to be passed.
        SessionToken.objects.create(
            user=self.admin, token=TOKEN,
            expires_at=datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc))

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Flag Keep Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin,
            count_flagged_kills=True)          # the event-wide default a None flag follows
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.lb = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})

        self.team_a = self._team("Alpha", "ALP")
        self.team_b = self._team("Bravo", "BRV")

        # Team A: two rostered players (5 + 4 = 9 kills) plus a ringer worth 3 that COUNTS and a
        # cross-team ringer worth 7 the admin already REJECTED. Canonical total = 9 + 3 = 12.
        self.a_players = [self._player("a0", 5), self._player("a1", 4)]
        self.counting_flag = MatchKillFlag.objects.create(
            match=self.match, tournament_team=self.team_a, uid="R1", name="Ringer1", kills=3,
            reason="not_on_roster", count_kills=None)          # follows the event default -> counts
        self.rejected_flag = MatchKillFlag.objects.create(
            match=self.match, tournament_team=self.team_a, uid="R2", name="Ringer2", kills=7,
            reason="name_matched_other_team", count_kills=False)   # explicitly refused -> excluded

        # Team B: one rostered player, no flags. Its total must never move.
        self.b_players = [self._player("b0", 2)]

        self._seed_stats()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _team(self, name, tag):
        return TournamentTeam.objects.create(
            event=self.event,
            team=Team.objects.create(team_name=name, team_tag=tag, join_settings="open",
                                     team_creator=self.admin, team_owner=self.admin, country="NG"),
            registered_by=self.admin)

    def _player(self, handle, kills):
        user = User.objects.create(username=handle, email=f"{handle}@x.com",
                                   full_name=handle, role="player", password="x")
        return {"user": user, "kills": kills}

    def _seed_stats(self):
        """The state an UPLOAD leaves behind: team kills already include the counting flag."""
        a = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.team_a, placement=1, kills=12,
            damage=0, assists=0, placement_points=12, kill_points=12, total_points=24, played=True)
        for p in self.a_players:
            TournamentPlayerMatchStats.objects.create(
                team_stats=a, player=p["user"], kills=p["kills"], damage=0, assists=0, played=True)
        b = TournamentTeamMatchStats.objects.create(
            match=self.match, tournament_team=self.team_b, placement=2, kills=2,
            damage=0, assists=0, placement_points=9, kill_points=2, total_points=11, played=True)
        for p in self.b_players:
            TournamentPlayerMatchStats.objects.create(
                team_stats=b, player=p["user"], kills=p["kills"], damage=0, assists=0, played=True)

    def _team_row(self, tt, placement, played=True):
        """One team's slice of the payload the grid posts, with its CURRENT player rows."""
        players = self.a_players if tt is self.team_a else self.b_players
        return {
            "tournament_team_id": tt.tournament_team_id,
            "placement": placement,
            "played": played,
            "bonus_points": 0,
            "penalty_points": 0,
            "players": [{"user_id": p["user"].user_id, "kills": p["kills"],
                         "damage": 0, "assists": 0, "played": True} for p in players],
        }

    def _post(self, url, results):
        return self.client.post(
            url, data=json.dumps({"match_id": self.match.match_id, "results": results}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def _stats(self, tt):
        return TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=tt)

    def _unchanged_resave(self):
        """Exactly what "Save this map" sends when the admin edits nothing."""
        return [self._team_row(self.team_a, 1), self._team_row(self.team_b, 2)]

    # ── the data loss ────────────────────────────────────────────────────────

    def test_an_unchanged_resave_keeps_the_flagged_kills(self):
        # Arrange: team A stores 12 (9 rostered + 3 flagged); the posted player rows sum to only 9.
        self.assertEqual(self._stats(self.team_a).kills, 12)

        # Act: re-save the map without changing anything.
        resp = self._post(EDIT_URL, self._unchanged_resave())

        # Assert: the flagged 3 is still in the total. Pre-fix this stored 9.
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._stats(self.team_a).kills, 12)

    def test_the_points_follow_the_restored_kills(self):
        # A total rebuilt without the flagged kills would also under-score the team.
        self._post(EDIT_URL, self._unchanged_resave())

        a = self._stats(self.team_a)
        self.assertEqual(a.kill_points, 12)
        self.assertEqual(a.total_points, 24)     # 12 placement + 12 kills

    def test_a_rejected_flag_stays_excluded(self):
        # The 7-kill cross-team ringer was refused, so restoring must not hand it back.
        self._post(EDIT_URL, self._unchanged_resave())

        self.assertEqual(self._stats(self.team_a).kills, 12)   # not 19

    def test_a_real_edit_still_applies_on_top_of_the_flagged_kills(self):
        # Arrange: the admin corrects one player 5 -> 1, so rostered drops 9 -> 5.
        self.a_players[0]["kills"] = 1

        # Act
        self._post(EDIT_URL, self._unchanged_resave())

        # Assert: the edit lands AND the flagged 3 survives it (5 + 3).
        self.assertEqual(self._stats(self.team_a).kills, 8)

    def test_a_team_with_no_flags_is_untouched(self):
        self._post(EDIT_URL, self._unchanged_resave())

        b = self._stats(self.team_b)
        self.assertEqual(b.kills, 2)
        self.assertEqual(b.total_points, 11)

    def test_the_flag_rows_themselves_are_never_consumed(self):
        # The recount must be re-runnable: saving twice must not double-count or drop the flag.
        self._post(EDIT_URL, self._unchanged_resave())
        self._post(EDIT_URL, self._unchanged_resave())

        self.assertEqual(self._stats(self.team_a).kills, 12)
        self.assertEqual(MatchKillFlag.objects.filter(match=self.match).count(), 2)

    def test_manual_entry_keeps_the_flagged_kills_too(self):
        # The other manual writer has the same hole: re-entering an uploaded map by hand.
        resp = self._post(MANUAL_URL, self._unchanged_resave())

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._stats(self.team_a).kills, 12)

    def test_an_unnamed_slot_keeps_its_kills_alongside_a_flag(self):
        """The trap: fixing this by REBUILDING the total from player rows erases unnamed slots.

        Manual entry accepts a slot with kills but no user_id (nobody to attribute them to), and
        result_writes.write_team_result_row deliberately creates no player row for it. A rebuild
        would score those kills as 0. The fold-in must ADD the flag to the stored total instead.
        """
        # Arrange: team B is entered as one unnamed slot worth 8, and carries a 2-kill ringer.
        MatchKillFlag.objects.create(
            match=self.match, tournament_team=self.team_b, uid="R3", name="Ringer3", kills=2,
            reason="not_on_roster", count_kills=None)
        results = [
            self._team_row(self.team_a, 1),
            {
                "tournament_team_id": self.team_b.tournament_team_id,
                "placement": 2, "played": True, "bonus_points": 0, "penalty_points": 0,
                "players": [{"kills": 8, "damage": 0, "assists": 0, "played": True}],  # no user_id
            },
        ]

        # Act
        resp = self._post(MANUAL_URL, results)

        # Assert: the unnamed 8 survives AND the flagged 2 is added on top.
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._stats(self.team_b).kills, 10)

    # ── the two writers must agree on `played` ───────────────────────────────

    def test_edit_stores_played_false_for_an_unticked_team(self):
        # Arrange: team B sat this map out. Pre-fix this stored the model default, True.
        results = [self._team_row(self.team_a, 1),
                   self._team_row(self.team_b, 2, played=False)]

        # Act
        resp = self._post(EDIT_URL, results)

        # Assert
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(self._stats(self.team_b).played)

    def test_the_two_write_paths_agree_on_played(self):
        """Pins the parity itself, so re-inlining either writer cannot silently drift again."""
        results = [self._team_row(self.team_a, 1),
                   self._team_row(self.team_b, 2, played=False)]

        self._post(EDIT_URL, results)
        via_edit = {tt: self._stats(tt).played for tt in (self.team_a, self.team_b)}

        # enter_team_match_result_manual goes through result_writes.write_team_result_row.
        self._post(MANUAL_URL, results)
        via_manual = {tt: self._stats(tt).played for tt in (self.team_a, self.team_b)}

        self.assertEqual(via_edit, via_manual)
        self.assertEqual(list(via_manual.values()), [True, False])
