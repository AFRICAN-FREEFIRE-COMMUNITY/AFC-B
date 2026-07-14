"""
OCR ringer flagging (owner 2026-07-13): extend the .log "kill flag" feature to the OCR/screenshot
commit path. When an OCR'd player's own registered team differs from the team their placement block
is credited to, commit_team_result must record a MatchKillFlag (reason name_matched_other_team,
synthetic uid "ocr:<user_id>", count_kills=None so it follows the event toggle) INSTEAD of a
TournamentPlayerMatchStats, and add the ringer's kills to the team total only while that flag counts.

This mirrors afc_tournament_and_scrims.tests_team_attribution's fixture idiom (real Event + teams +
roster + Match), then calls commit_team_result directly with already-resolved OCR rows (the shape the
OCR review step produces: each row carries matched_user_id + matched_team_id + kills).

Run: venv\\Scripts\\python.exe manage.py test afc_ocr.tests.test_ringer_flag
"""
import datetime

from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, Leaderboard,
    TournamentTeam, TournamentTeamMember,
    TournamentTeamMatchStats, TournamentPlayerMatchStats, MatchKillFlag,
)
from afc_tournament_and_scrims.views import _recompute_team_kills_for_event
from afc_ocr.services.commit import commit_team_result


class OcrRingerFlagTests(TestCase):
    def setUp(self):
        today = datetime.date.today()
        self.admin = User.objects.create(
            username="ocr_ringer_admin", email="ocr_ringer_admin@x.com",
            full_name="OCR Ringer Admin", role="admin", password="x")
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="OCR Ringer Cup", event_mode="virtual",
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
        lb = Leaderboard.objects.create(
            leaderboard_name="GA LB", event=self.event, stage=self.stage, group=self.group,
            creator=self.admin, placement_points={"1": 12, "2": 9}, kill_point=1.0,
            leaderboard_method="manual")
        self.match = Match.objects.create(
            leaderboard=lb, group=self.group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9}, "kill_point": 1})

        # Two registered teams with two players each. Alpha wins the map; one Bravo player is a
        # RINGER credited under Alpha (played for a team he is not rostered on).
        self.alpha, self.a_players = self._register("Alpha", 2)
        self.bravo, self.b_players = self._register("Bravo", 2)

    def _register(self, team_name, n):
        team = Team.objects.create(
            team_name=team_name, team_tag=team_name[:3], join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG")
        tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=self.admin)
        players = []
        for i in range(n):
            u = User.objects.create(
                username=f"{team_name}_{i}", email=f"{team_name}_{i}@x.com",
                full_name=f"{team_name} p{i}", role="player", password="x", uid=f"{team_name}{i}")
            TournamentTeamMember.objects.create(tournament_team=tt, user=u)
            players.append(u)
        return tt, players

    def _row(self, placement, team_id, user, kills):
        # The already-resolved OCR row shape the review step feeds to commit_team_result.
        # matched_user_id is the User PK (custom User model → use .pk, not .id).
        return {
            "placement": placement, "matched_team_id": team_id, "matched_user_id": user.pk,
            "kills": kills, "damage": 0, "assists": 0, "raw_name": user.full_name,
        }

    def _rows_with_ringer(self):
        """Placement-1 block credited to Alpha: two Alpha players (rostered) + one Bravo player
        (ringer). Placement-2 block: the remaining Bravo player, clean."""
        a0, a1 = self.a_players
        b0, b1 = self.b_players
        return [
            # Alpha block — first row is a rostered Alpha player so commit credits the block to Alpha.
            self._row(1, self.alpha.tournament_team_id, a0, 5),
            self._row(1, self.alpha.tournament_team_id, a1, 4),
            self._row(1, self.bravo.tournament_team_id, b0, 3),   # RINGER (Bravo player under Alpha)
            # Bravo block — clean.
            self._row(2, self.bravo.tournament_team_id, b1, 2),
        ]

    def test_ringer_becomes_flag_not_playerstat(self):
        commit_team_result(self.match, self._rows_with_ringer())
        b0 = self.b_players[0]

        # One flag on the match: the ringer, credited to Alpha, name-based synthetic uid, follows default.
        flags = MatchKillFlag.objects.filter(match=self.match)
        self.assertEqual(flags.count(), 1)
        flag = flags.first()
        self.assertEqual(flag.tournament_team_id, self.alpha.tournament_team_id)
        self.assertEqual(flag.uid, f"ocr:{b0.pk}")
        self.assertEqual(flag.registered_user_id, b0.pk)
        self.assertEqual(flag.reason, "name_matched_other_team")
        self.assertIsNone(flag.count_kills)          # follows the event count_flagged_kills toggle
        self.assertEqual(flag.kills, 3)

        # The ringer gets NO player-stat (would double-count against the flag in the recompute).
        alpha_stat = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=self.alpha)
        player_ids = set(
            TournamentPlayerMatchStats.objects.filter(team_stats=alpha_stat)
            .values_list("player_id", flat=True))
        self.assertEqual(player_ids, {self.a_players[0].pk, self.a_players[1].pk})
        self.assertNotIn(b0.pk, player_ids)

        # Event default is count -> Alpha kills = rostered (5+4) + counted ringer (3) = 12.
        self.assertEqual(alpha_stat.kills, 12)

    def test_toggle_off_drops_ringer_kills_from_team_total(self):
        commit_team_result(self.match, self._rows_with_ringer())
        flag = MatchKillFlag.objects.get(match=self.match)

        # Admin sets the flag to "do not count" and recomputes (same path the panel PATCH uses).
        flag.count_kills = False
        flag.save(update_fields=["count_kills"])
        _recompute_team_kills_for_event(self.event)

        alpha_stat = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=self.alpha)
        self.assertEqual(alpha_stat.kills, 9)        # 5 + 4, ringer's 3 dropped

    def test_recommit_restores_prior_decision(self):
        commit_team_result(self.match, self._rows_with_ringer())
        flag = MatchKillFlag.objects.get(match=self.match)
        flag.count_kills = False                     # admin dropped it
        flag.save(update_fields=["count_kills"])

        # Re-commit the same OCR result: the flag is re-derived but the admin's decision survives.
        commit_team_result(self.match, self._rows_with_ringer())
        flag2 = MatchKillFlag.objects.get(match=self.match)
        self.assertIs(flag2.count_kills, False)
        # And with the ringer still dropped, Alpha's stored total is 9 (no re-inflation on re-commit).
        alpha_stat = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=self.alpha)
        self.assertEqual(alpha_stat.kills, 9)

    def test_no_ringer_no_flag(self):
        # A clean block (every player on the credited team) produces no flags and normal player stats.
        a0, a1 = self.a_players
        rows = [
            self._row(1, self.alpha.tournament_team_id, a0, 5),
            self._row(1, self.alpha.tournament_team_id, a1, 4),
        ]
        commit_team_result(self.match, rows)
        self.assertEqual(MatchKillFlag.objects.filter(match=self.match).count(), 0)
        alpha_stat = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=self.alpha)
        self.assertEqual(alpha_stat.kills, 9)
        self.assertEqual(
            TournamentPlayerMatchStats.objects.filter(team_stats=alpha_stat).count(), 2)
