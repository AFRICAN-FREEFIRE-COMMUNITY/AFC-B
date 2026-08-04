"""The OCR commit stores exactly what the organizer typing the same numbers would store.

WHY THIS TEST EXISTS (owner 2026-08-04)
    Until this change there was no single place a map result became standings: manual entry, the
    match-log upload and this OCR commit each built TournamentTeamMatchStats and
    TournamentPlayerMatchStats by hand and agreed only by habit. They all now write through
    afc_tournament_and_scrims.result_writes.write_team_result_row, and the point of consolidating
    is precisely that the same map cannot score differently depending on which door it came
    through. So the test scores ONE map twice, once by committing OCR rows and once by posting the
    organizer's manual entry, and compares every stored column of every team row plus every player
    row. If somebody re-inlines either write, this fails.

    It is the OCR twin of afc_tournament_and_scrims.tests_team_submissions
    .test_approved_result_matches_manual_entry (team submission versus manual entry) and of
    tests_log_attribution.test_log_upload_matches_manual_entry (match-log file versus manual entry).

WHAT IT DELIBERATELY COVERS BEYOND THE HAPPY PATH
    * assists and damage, with per-assist and per-1000-damage scoring switched on, so those two
      columns and their point contributions are compared and not just kills;
    * a penalty on one team, because the writer must fold bonus and penalty into total_points;
    * an OCR row the reviewer could NOT tie to an account. It has to keep counting toward the team
      total while producing no player row - the same "unnamed slot" rule the manual form relies on,
      and the rule whose absence once scored whole teams at zero.

WHAT IT DELIBERATELY DOES NOT COVER
    Ringers. A ringer is a thing OCR can express and a manual entry cannot: it becomes a
    MatchKillFlag rather than a player row, so the two doors are not describing the same map any
    more. test_ringer_flag.py owns that behaviour.

Run: venv\\Scripts\\python.exe manage.py test afc_ocr.tests.test_commit_matches_manual
"""
import datetime
import json

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_ocr.services.commit import commit_team_result
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageGroups,
    Stages,
    TournamentPlayerMatchStats,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)

# Assists and damage both score, so a difference in either column changes total_points and the
# comparison below would catch it. Kept small enough to check by hand: first place with 9 kills,
# 2 assists and 3000 damage scores 12 + 9 + 2 + 6 = 29.
SCORING = {
    "placement_points": {"1": 12, "2": 9},
    "kill_point": 1,
    "points_per_assist": 1,
    "points_per_1000_damage": 2,
}

MANUAL_URL = "/events/enter-team-match-result-manual/"


class OcrCommitMatchesManualEntryTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = datetime.date.today()

        self.admin = User.objects.create(
            username="ocr_parity_admin", email="ocr_parity_admin@x.com",
            full_name="OCR Parity Admin", role="admin", password="x")
        self.token = SessionToken.objects.create(
            user=self.admin, token="ocr-parity-token",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="OCR Parity Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Quals", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2,
            stage_order=1)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points=SCORING["placement_points"], kill_point=1.0,
            leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings=SCORING)

        # Two registered teams, two players each, every roster row carrying a FROZEN in-game role
        # so role_at_match is compared as a real value rather than None against None.
        self.alpha, self.alpha_players = self._register("Alpha", ["rusher", "sniper"])
        self.bravo, self.bravo_players = self._register("Bravo", ["support", "grenader"])

    # ── fixtures ──
    def _register(self, team_name, roles):
        team = Team.objects.create(
            team_name=team_name, team_tag=team_name[:3], join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG")
        tt = TournamentTeam.objects.create(
            event=self.event, team=team, registered_by=self.admin)
        players = []
        for index, role in enumerate(roles):
            user = User.objects.create(
                username=f"{team_name}_{index}", email=f"{team_name}_{index}@x.com",
                full_name=f"{team_name} p{index}", role="player", password="x",
                uid=f"{team_name}{index}")
            TournamentTeamMember.objects.create(
                tournament_team=tt, user=user, event=self.event, in_game_role=role)
            players.append(user)
        return tt, players

    def _snapshot(self):
        """Every stored column of the match's team rows plus every player row, keyed by team.
        Whatever either write path stores differently shows up as a difference here."""
        snapshot = {}
        for stats in TournamentTeamMatchStats.objects.filter(match=self.match):
            snapshot[stats.tournament_team_id] = {
                "columns": {
                    field: getattr(stats, field) for field in (
                        "placement", "kills", "damage", "assists", "placement_points",
                        "kill_points", "bonus_points", "penalty_points", "total_points", "played")
                },
                "players": sorted(
                    (p.player_id, p.kills, p.damage, p.assists, p.played, p.role_at_match)
                    for p in TournamentPlayerMatchStats.objects.filter(team_stats=stats)),
            }
        return snapshot

    # ──────────────────────────────────────────────────────────────────────────
    # The property that matters
    # ──────────────────────────────────────────────────────────────────────────
    def test_ocr_commit_matches_manual_entry(self):
        alpha_0, alpha_1 = self.alpha_players
        bravo_0, bravo_1 = self.bravo_players

        # The already-resolved OCR rows the review step produces. Alpha wins with two named
        # players; Bravo is second with two named players and one line the reviewer could not tie
        # to an account (matched_user_id None), which must still count toward Bravo's total.
        commit_team_result(self.match, [
            {"placement": 1, "matched_team_id": self.alpha.tournament_team_id,
             "matched_user_id": alpha_0.pk, "kills": 5, "damage": 2000, "assists": 1,
             "raw_name": alpha_0.full_name},
            {"placement": 1, "matched_team_id": self.alpha.tournament_team_id,
             "matched_user_id": alpha_1.pk, "kills": 4, "damage": 1000, "assists": 1,
             "raw_name": alpha_1.full_name, "penalty_points": 0},
            {"placement": 2, "matched_team_id": self.bravo.tournament_team_id,
             "matched_user_id": bravo_0.pk, "kills": 3, "damage": 1500, "assists": 2,
             "raw_name": bravo_0.full_name, "penalty_points": 5},
            {"placement": 2, "matched_team_id": self.bravo.tournament_team_id,
             "matched_user_id": bravo_1.pk, "kills": 1, "damage": 500, "assists": 0,
             "raw_name": bravo_1.full_name},
            {"placement": 2, "matched_team_id": self.bravo.tournament_team_id,
             "matched_user_id": None, "kills": 2, "damage": 0, "assists": 0,
             "raw_name": "unreadable"},
        ])

        from_ocr = self._snapshot()

        # Sanity before comparing, so two empty or two all-None snapshots cannot pass by accident.
        # Alpha: 12 placement + 9 kills + 2 assists + 3000 damage at 2 per 1000 = 29.
        self.assertEqual(from_ocr[self.alpha.pk]["columns"]["total_points"], 29)
        # Bravo counts the unreadable line's 2 kills but writes no player row for it.
        self.assertEqual(from_ocr[self.bravo.pk]["columns"]["kills"], 6)
        self.assertEqual(len(from_ocr[self.bravo.pk]["players"]), 2)
        self.assertTrue(all(player[5] for player in from_ocr[self.alpha.pk]["players"]))
        # No ringers in this map, so nothing is hidden behind a flag.
        self.assertEqual(from_ocr[self.bravo.pk]["columns"]["penalty_points"], 5)

        # The same map, typed by the organizer. This endpoint clears the whole match first, so it
        # replaces the committed rows rather than adding to them.
        resp = self.client.post(
            MANUAL_URL,
            data=json.dumps({
                "match_id": self.match.match_id,
                "results": [
                    {
                        "tournament_team_id": self.alpha.pk,
                        "placement": 1,
                        "played": True,
                        "players": [
                            {"user_id": alpha_0.pk, "kills": 5, "damage": 2000, "assists": 1},
                            {"user_id": alpha_1.pk, "kills": 4, "damage": 1000, "assists": 1},
                        ],
                    },
                    {
                        "tournament_team_id": self.bravo.pk,
                        "placement": 2,
                        "played": True,
                        "penalty_points": 5,
                        "players": [
                            {"user_id": bravo_0.pk, "kills": 3, "damage": 1500, "assists": 2},
                            {"user_id": bravo_1.pk, "kills": 1, "damage": 500, "assists": 0},
                            # The organizer's equivalent of the unreadable OCR line: kills that
                            # belong to the team with nobody to attribute them to.
                            {"kills": 2, "damage": 0, "assists": 0},
                        ],
                    },
                ],
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(resp.status_code, 200, resp.content)

        self.assertEqual(from_ocr, self._snapshot())

    def test_the_leaderboard_fallbacks_survive_the_shared_writer(self):
        """THE REGRESSION GUARD for the one thing this refactor deliberately did NOT hand over.

        commit.py builds its own scoring context instead of calling result_writes.scoring_context,
        because this path has always had two fallbacks the other doors never had: a match with no
        scoring_settings of its own falls back to the LEADERBOARD's placement table and kill point,
        and an empty table falls back to the Free Fire default. scoring_context reads the match and
        nothing else, so routing this through it would score those maps at ZERO placement points.

        Every other test in this file hands the match its own scoring_settings, so all of them
        would keep passing if somebody "simplified" those lines away, and real historical events
        would quietly re-score on their next commit. This test is the only thing standing between
        that edit and the standings, which is why it clears scoring_settings on purpose.
        """
        # An EMPTY dict, not None: the column is NOT NULL, and empty is what a match that was
        # never given its own scoring actually holds.
        self.match.scoring_settings = {}
        self.match.save(update_fields=["scoring_settings"])

        alpha_0, alpha_1 = self.alpha_players
        commit_team_result(self.match, [
            {"placement": 1, "matched_team_id": self.alpha.tournament_team_id,
             "matched_user_id": alpha_0.pk, "kills": 5, "raw_name": alpha_0.full_name},
            {"placement": 1, "matched_team_id": self.alpha.tournament_team_id,
             "matched_user_id": alpha_1.pk, "kills": 4, "raw_name": alpha_1.full_name},
        ])

        stats = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team_id=self.alpha.pk)
        # The LEADERBOARD's table (first place = 12) and its kill_point of 1.0, neither of which
        # the match itself carries any more. A zero here is the failure this exists to catch.
        self.assertEqual(stats.placement_points, 12, "the leaderboard's placement table was lost")
        self.assertEqual(stats.kill_points, 9)
        self.assertEqual(stats.total_points, 21)

    def test_two_placement_groups_on_one_team_are_refused(self):
        """A reviewer who credits two blocks to the same team must not be able to make one of them
        disappear. The unique (match, team) constraint used to catch this on the second insert;
        the shared writer clears a team's row before writing it, so the commit has to say no
        itself. Nothing may be stored, because the whole commit is one transaction."""
        alpha_0 = self.alpha_players[0]
        bravo_0 = self.bravo_players[0]

        with self.assertRaises(ValueError):
            commit_team_result(self.match, [
                {"placement": 1, "matched_team_id": self.alpha.tournament_team_id,
                 "matched_user_id": alpha_0.pk, "kills": 5, "raw_name": alpha_0.full_name},
                # Second place credited to Alpha as well: the mistake this refuses.
                {"placement": 2, "matched_team_id": self.alpha.tournament_team_id,
                 "matched_user_id": bravo_0.pk, "kills": 3, "raw_name": bravo_0.full_name},
            ])

        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.match).exists())
        self.match.refresh_from_db()
        self.assertFalse(self.match.result_inputted)
