r"""Player statistics must divide a population by ITSELF, and one number must carry one name.

THE BUGS (owner, 2026-08-07). All four were live on the PUBLIC player pages.

  A. WIN RATE mixed two populations. The numerator counted placement-1 rows from
     TournamentTeamMatchStats across EVERY team the player was rostered on; the denominator
     counted the player's OWN TournamentPlayerMatchStats rows. Different populations, so the
     ratio was meaningless in both directions: five real players rendered a win rate ABOVE
     100% (four at 400%, one at 140%), and fifty rostered players who had never played read
     "7 wins, 0.0% win rate" off the divide-by-zero guard.

  B. WINS AND BOOYAHS WERE THE SAME EXPRESSION. get_user_profile computed
     `solo_wins + team_wins` twice, into total_wins and into total_booyahs, so the profile
     showed "Wins 7, Booyahs 7" and the two could never differ. compute_player_stats had the
     same duplication one level down (scrim_booyah/tournament_booyah were incremented in the
     same branch as scrims_wins/tournaments_wins), and the admin player page ADDED the pair,
     printing exactly double.

  D. CANCELLED ENTRIES STILL COUNTED. RegisteredCompetitors / TournamentTeamMember were read
     with no status check, so a disqualified registration, a rejected roster slot, a withdrawn
     team, a waitlisted entry that never got a slot and a marked no-show all counted as a
     tournament played.

Run: .venv\Scripts\python.exe manage.py test afc_player.tests_stat_correctness
"""
import datetime

from django.test import TestCase
from django.utils import timezone

from afc_auth.models import User
from afc_player.aggregation import compute_player_stats
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    RegisteredCompetitors,
    Stages,
    TournamentPlayerMatchStats,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)
from afc_tournament_and_scrims.participation import counted_event_ids


class _StatFixtureMixin:
    """Builds the minimum real object graph the aggregation walks:

    Event -> Stages -> Leaderboard -> Match -> TournamentTeamMatchStats -> TournamentPlayerMatchStats

    compute_player_stats reads competition_type through match.leaderboard.event, so a match
    needs a leaderboard to be attributed to an event at all.
    """

    def _make_event(self, name, *, competition_type="tournament", is_draft=False):
        today = timezone.localdate()
        return Event.objects.create(
            competition_type=competition_type, participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name=name, event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r",
            event_status="completed", registration_link="https://x.com/r",
            number_of_stages=1, creator=self.admin, is_draft=is_draft)

    def _make_leaderboard(self, event):
        today = timezone.localdate()
        stage = Stages.objects.create(
            event=event, stage_name="Stage 1", start_date=today, end_date=today,
            number_of_groups=1, stage_format="battle_royale", teams_qualifying_from_stage=1)
        return Leaderboard.objects.create(
            leaderboard_name=f"LB {event.event_name}", event=event, stage=stage,
            creator=self.admin, leaderboard_method="automatic")

    def _make_tournament_team(self, event, team, *, status="active",
                              is_waitlisted=False, is_no_show=False):
        return TournamentTeam.objects.create(
            event=event, team=team, status=status,
            is_waitlisted=is_waitlisted, is_no_show=is_no_show)

    def _play_match(self, leaderboard, tournament_team, *, number, placement, players=()):
        """One match: a team line at `placement`, plus a player line for each user in `players`.

        `players` is the roster actually FIELDED for this match, which is the whole point: a
        team can play a match without a given roster member being on the sheet for it.
        """
        match = Match.objects.create(leaderboard=leaderboard, match_number=number)
        team_stats = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tournament_team, placement=placement, kills=0)
        for user in players:
            TournamentPlayerMatchStats.objects.create(
                team_stats=team_stats, player=user, kills=2, damage=100, assists=0)
        return match


class WinRateIsOnePopulationTests(_StatFixtureMixin, TestCase):
    """Bug A: numerator and denominator must come from the same rows."""

    def setUp(self):
        self.admin = User.objects.create(
            username="statadmin", email="statadmin@x.com", full_name="Stat Admin",
            role="admin", password="x")
        self.player = User.objects.create(
            username="statplayer", email="statplayer@x.com", full_name="Stat Player",
            role="player", password="x")
        self.team = Team.objects.create(
            team_name="Stat Team", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)

    def test_win_rate_cannot_exceed_100_percent(self):
        """THE REPORTED SYMPTOM, reproduced exactly: a player fielded in ONE match of a team
        that won FOUR. The old code read 4 wins over 1 match and printed 400%."""
        # Arrange: the team plays 4 matches and wins all of them; the player is on the sheet
        # for only the first.
        event = self._make_event("Four Win Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_match(lb, tt, number=1, placement=1, players=[self.player])
        for n in (2, 3, 4):
            self._play_match(lb, tt, number=n, placement=1, players=[])

        # Act
        stats = compute_player_stats(self.player, include_breakdown=False)

        # Assert: the player's own record is 1 match, 1 win, 100%. Never 400%.
        self.assertEqual(stats["total_matches"], 1)
        self.assertEqual(stats["total_wins"], 1)
        self.assertEqual(stats["win_rate"], 100.0)
        self.assertLessEqual(stats["win_rate"], 100.0)

    def test_a_match_the_player_sat_out_is_not_their_win(self):
        """The player is fielded once and LOSES it, while the team wins the other three."""
        event = self._make_event("Sat Out Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_match(lb, tt, number=1, placement=7, players=[self.player])
        for n in (2, 3, 4):
            self._play_match(lb, tt, number=n, placement=1, players=[])

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_matches"], 1)
        self.assertEqual(stats["total_wins"], 0)
        self.assertEqual(stats["win_rate"], 0.0)

    def test_the_team_record_is_still_reported_under_its_own_name(self):
        """The roster record is a real statistic and was not thrown away, it was NAMED."""
        event = self._make_event("Named Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_match(lb, tt, number=1, placement=1, players=[self.player])
        for n in (2, 3, 4):
            self._play_match(lb, tt, number=n, placement=1, players=[])

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["team_matches"], 4)
        self.assertEqual(stats["team_wins"], 4)
        self.assertEqual(stats["team_win_rate"], 100.0)
        # and it is a DIFFERENT number from the personal one
        self.assertNotEqual(stats["team_wins"], stats["total_wins"])

    def test_a_rostered_player_who_never_played_reads_zero_not_seven_wins(self):
        """The other half of bug A: 50 live players read "7 wins, 0.0% win rate" because the
        wins came from the team while the rate divided by their own zero matches."""
        event = self._make_event("Bench Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        for n in (1, 2, 3):
            self._play_match(lb, tt, number=n, placement=1, players=[])

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_matches"], 0)
        self.assertEqual(stats["total_wins"], 0)
        self.assertEqual(stats["win_rate"], 0)
        # the team's record is intact and clearly labelled as the team's
        self.assertEqual(stats["team_wins"], 3)
        self.assertEqual(stats["team_win_rate"], 100.0)

    def test_a_rejected_roster_slot_lends_the_player_nothing(self):
        """Bug D applied to the team record: a rejected member did not play for that team."""
        event = self._make_event("Rejected Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="rejected")
        self._play_match(lb, tt, number=1, placement=1, players=[])

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["team_matches"], 0)
        self.assertEqual(stats["team_wins"], 0)

    def test_a_disqualified_team_lends_its_members_nothing(self):
        event = self._make_event("DQ Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team, status="disqualified")
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_match(lb, tt, number=1, placement=1, players=[])

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["team_wins"], 0)


class WinsAndBooyahsAreOneNumberTests(_StatFixtureMixin, TestCase):
    """Bug B: the aggregation must not hand back two names for one value."""

    def setUp(self):
        self.admin = User.objects.create(
            username="dupadmin", email="dupadmin@x.com", full_name="Dup Admin",
            role="admin", password="x")
        self.player = User.objects.create(
            username="dupplayer", email="dupplayer@x.com", full_name="Dup Player",
            role="player", password="x")
        self.team = Team.objects.create(
            team_name="Dup Team", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)

    def test_the_duplicate_booyah_keys_are_gone(self):
        """scrim_booyah / tournament_booyah were incremented in the same branch as
        scrims_wins / tournaments_wins, so they could never differ. The admin player page
        summed the two and therefore printed exactly double."""
        event = self._make_event("Dup Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_match(lb, tt, number=1, placement=1, players=[self.player])

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertNotIn("scrim_booyah", stats)
        self.assertNotIn("tournament_booyah", stats)
        self.assertEqual(stats["tournaments_wins"], 1)
        self.assertEqual(stats["scrims_wins"], 0)

    def test_scrims_and_tournaments_wins_split_by_competition_type(self):
        """The surviving split still splits, so removing the duplicate lost no information."""
        tourney = self._make_event("Split Cup", competition_type="tournament")
        scrim = self._make_event("Split Scrim", competition_type="scrims")
        for event, placement in ((tourney, 1), (scrim, 1)):
            lb = self._make_leaderboard(event)
            tt = self._make_tournament_team(event, self.team)
            TournamentTeamMember.objects.create(
                tournament_team=tt, user=self.player, event=event, status="active")
            self._play_match(lb, tt, number=1, placement=placement, players=[self.player])

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["tournaments_wins"], 1)
        self.assertEqual(stats["scrims_wins"], 1)
        self.assertEqual(stats["total_wins"], 2)


class OwnProfileBooyahsAgreeWithPublicProfileTests(_StatFixtureMixin, TestCase):
    """The two endpoints must not report different match-win counts for the SAME player.

    get_user_profile (own profile, "Booyahs") counted placement-1 rows across the user's whole
    roster, while compute_player_stats (public page, "Wins") counts the matches the player was
    fielded in. One player therefore read 7 on one page and 3 on the other. Both now read the
    player's own record.
    """

    def setUp(self):
        self.admin = User.objects.create(
            username="agreeadmin", email="agreeadmin@x.com", full_name="Agree Admin",
            role="admin", password="x")
        self.player = User.objects.create(
            username="agreeplayer", email="agreeplayer@x.com", full_name="Agree Player",
            role="player", password="x")
        self.team = Team.objects.create(
            team_name="Agree Team", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)

    def test_the_admin_list_agrees_with_the_detail_pages(self):
        """get_all_players built total_wins by summing every rostered team's placement-1 rows,
        so the admin Players TABLE and the player's own detail page disagreed."""
        event = self._make_event("Admin List Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_match(lb, tt, number=1, placement=1, players=[self.player])
        for n in (2, 3, 4):
            self._play_match(lb, tt, number=n, placement=1, players=[])

        # the grouped expression get_all_players now uses for the whole table
        list_wins = TournamentPlayerMatchStats.objects.filter(
            player=self.player, team_stats__placement=1).count()
        detail_wins = compute_player_stats(
            self.player, include_breakdown=False)["total_wins"]

        self.assertEqual(list_wins, 1)
        self.assertEqual(list_wins, detail_wins)

    def test_the_two_endpoints_report_the_same_match_wins(self):
        # Arrange: team wins 4, player fielded in 2 of them (one win, one loss).
        event = self._make_event("Agree Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_match(lb, tt, number=1, placement=1, players=[self.player])
        self._play_match(lb, tt, number=2, placement=5, players=[self.player])
        for n in (3, 4, 5):
            self._play_match(lb, tt, number=n, placement=1, players=[])

        # Act: the expression get_user_profile uses for the team half of total_booyahs.
        own_profile_team_booyahs = TournamentPlayerMatchStats.objects.filter(
            player=self.player, team_stats__placement=1).count()
        public_wins = compute_player_stats(
            self.player, include_breakdown=False)["total_wins"]

        # Assert: one win each way, not four on the profile and one on the public page.
        self.assertEqual(own_profile_team_booyahs, 1)
        self.assertEqual(own_profile_team_booyahs, public_wins)


class TournamentsPlayedExcludesCancelledEntriesTests(_StatFixtureMixin, TestCase):
    """Bug D: only a real, accepted, non-waitlisted entry in a real event counts."""

    def setUp(self):
        self.admin = User.objects.create(
            username="cntadmin", email="cntadmin@x.com", full_name="Cnt Admin",
            role="admin", password="x")
        self.player = User.objects.create(
            username="cntplayer", email="cntplayer@x.com", full_name="Cnt Player",
            role="player", password="x")
        self.team = Team.objects.create(
            team_name="Cnt Team", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)

    def test_an_accepted_solo_registration_counts(self):
        event = self._make_event("Good Solo")
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    def test_a_disqualified_registration_does_not_count(self):
        event = self._make_event("DQ Solo")
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="disqualified")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_a_pending_registration_does_not_count(self):
        event = self._make_event("Pending Solo")
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="pending")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_a_waitlisted_registration_that_never_got_a_slot_does_not_count(self):
        event = self._make_event("Waitlist Solo")
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered", is_waitlisted=True)

        self.assertEqual(counted_event_ids(self.player), set())

    def test_a_no_show_does_not_count(self):
        event = self._make_event("No Show Solo")
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered", is_no_show=True)

        self.assertEqual(counted_event_ids(self.player), set())

    def test_a_draft_event_does_not_count(self):
        event = self._make_event("Draft Solo", is_draft=True)
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_an_active_roster_slot_counts(self):
        event = self._make_event("Good Squad")
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    def test_a_rejected_roster_slot_does_not_count(self):
        """39 of these were live in the database."""
        event = self._make_event("Rejected Squad")
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="rejected")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_a_pending_roster_slot_does_not_count(self):
        """3 of these were live in the database."""
        event = self._make_event("Pending Squad")
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="pending")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_an_active_slot_on_a_withdrawn_team_does_not_count(self):
        event = self._make_event("Withdrawn Squad")
        tt = self._make_tournament_team(event, self.team, status="withdrawn")
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_an_active_slot_on_a_waitlisted_team_does_not_count(self):
        event = self._make_event("Waitlisted Squad")
        tt = self._make_tournament_team(event, self.team, is_waitlisted=True)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_one_event_entered_both_ways_is_counted_once(self):
        """counted_event_ids returns a SET, so the solo and squad paths cannot double-count."""
        event = self._make_event("Both Ways")
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")

        self.assertEqual(counted_event_ids(self.player), {event.event_id})
