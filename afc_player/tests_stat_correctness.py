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

  E. AND THEN THE WHOLE NUMBER MEANT THE WRONG THING (owner ruling, 2026-08-08):
     "It should count events played. Matches they participated in where a score was assigned
     to them." Fixing D still left the count measuring a real SLOT, which is permission to
     play, not play. TournamentsPlayedCountsScoredMatchLinesTests below replaces D's class:
     the tests that used to assert "an accepted registration counts" now assert the opposite,
     because signing up is not playing. Platform-wide this moved 1013 of 1366 players.

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
    SoloPlayerMatchStats,
    Stages,
    TournamentPlayerMatchStats,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)
from afc_tournament_and_scrims.participation import counted_event_ids, played_event_counts


class _StatFixtureMixin:
    """Builds the minimum real object graph the aggregation walks:

    Event -> Stages -> Leaderboard -> Match -> TournamentTeamMatchStats -> TournamentPlayerMatchStats

    compute_player_stats reads competition_type through match.leaderboard.event, so a match
    needs a leaderboard to be attributed to an event at all.
    """

    def _make_event(self, name, *, competition_type="tournament", is_draft=False,
                    participant_type="squad"):
        today = timezone.localdate()
        return Event.objects.create(
            competition_type=competition_type, participant_type=participant_type,
            event_type="internal",
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

    def _play_squad_line(self, leaderboard, tournament_team, user, *, number,
                         placement=1, kills=2, played=True, total_points=10):
        """One SQUAD match line for one user, with the score fields set explicitly.

        Separate from _play_match because the events-played rule turns on exactly two
        things this helper exposes: the `played` flag, and the fact that a ZERO score is
        still a score. Callers set kills=0 / total_points=0 to pin the falsy-zero guard.
        """
        match = Match.objects.create(leaderboard=leaderboard, match_number=number)
        team_stats = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tournament_team, placement=placement,
            kills=kills, total_points=total_points)
        return TournamentPlayerMatchStats.objects.create(
            team_stats=team_stats, player=user, kills=kills, damage=0, assists=0,
            played=played)

    def _play_solo_line(self, event, competitor, *, number, placement=1, kills=3,
                        played=True, total_points=10):
        """One SOLO match line. A solo competitor has no team, so the line hangs off their
        RegisteredCompetitors row, which is what carries the event."""
        leaderboard = self._make_leaderboard(event)
        match = Match.objects.create(leaderboard=leaderboard, match_number=number)
        return SoloPlayerMatchStats.objects.create(
            match=match, competitor=competitor, placement=placement, kills=kills,
            total_points=total_points, played=played)


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


class TournamentsPlayedCountsScoredMatchLinesTests(_StatFixtureMixin, TestCase):
    """Bug E, the meaning change (owner 2026-08-08): an event counts when the player has a
    match line in it that a score was written to. Registering does not count. Being named
    on a roster and never fielded does not count.

    This class REPLACES the old TournamentsPlayedExcludesCancelledEntriesTests, whose first
    two assertions ("an accepted solo registration counts", "an active roster slot counts")
    are now exactly backwards. They are kept here, inverted, as the two tests that fail
    before this change and pass after it.
    """

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

    # ── registration / roster alone is NOT participation ──────────────────────────────
    def test_an_accepted_solo_registration_alone_does_not_count(self):
        """THE REPORTED SYMPTOM. Signing up is not playing, so a clean accepted
        registration with no match line behind it counts for nothing."""
        event = self._make_event("Signed Up Solo", participant_type="solo")
        RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_an_active_roster_slot_alone_does_not_count(self):
        """The squad half of the same symptom: rostered, never fielded, never scored."""
        event = self._make_event("Benched Squad")
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")

        self.assertEqual(counted_event_ids(self.player), set())

    def test_a_rostered_player_left_off_every_sheet_does_not_count(self):
        """Their team plays the whole event; this player is never on a sheet for any of it."""
        event = self._make_event("Played Without Me")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        teammate = User.objects.create(
            username="cntmate", email="cntmate@x.com", full_name="Cnt Mate",
            role="player", password="x")
        for n in (1, 2, 3):
            self._play_squad_line(lb, tt, teammate, number=n)

        self.assertEqual(counted_event_ids(self.player), set())

    def test_a_played_false_placeholder_line_does_not_count(self):
        """create_leaderboard pre-seeds a played=False row for EVERY rostered member so the
        manual score grid has something to type into. A placeholder is not play."""
        event = self._make_event("Placeholder Squad")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        TournamentTeamMember.objects.create(
            tournament_team=tt, user=self.player, event=event, status="active")
        self._play_squad_line(lb, tt, self.player, number=1, placement=0, kills=0,
                              total_points=0, played=False)

        self.assertEqual(counted_event_ids(self.player), set())

    # ── a scored line IS participation ────────────────────────────────────────────────
    def test_a_scored_squad_line_counts(self):
        event = self._make_event("Really Played")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        self._play_squad_line(lb, tt, self.player, number=1)

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    def test_a_zero_score_squad_line_still_counts(self):
        """ZERO IS A REAL SCORE. 1003 of 2982 live squad lines carry zero kills; a
        truthiness test on the score would delete a third of the platform's real play."""
        event = self._make_event("Quiet Match")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        self._play_squad_line(lb, tt, self.player, number=1, kills=0, total_points=0)

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    def test_a_scored_solo_line_counts(self):
        """Solo events have a different shape: no team, no roster, the line hangs off the
        RegisteredCompetitors row."""
        event = self._make_event("Solo Cup", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1)

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    def test_a_zero_score_solo_line_still_counts(self):
        """360 of 1019 live solo lines carry zero kills."""
        event = self._make_event("Quiet Solo", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1, kills=0, total_points=0)

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    def test_a_played_false_solo_line_does_not_count(self):
        event = self._make_event("Solo Placeholder", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1, kills=0, total_points=0,
                             played=False)

        self.assertEqual(counted_event_ids(self.player), set())

    # ── sanctions do not un-play a match ──────────────────────────────────────────────
    def test_a_fielded_player_on_a_disqualified_team_still_counts(self):
        """Deliberate, and the one place this rule is LOOSER than the slot rule it replaced.
        A disqualification decides where a team FINISHES; the matches still happened and the
        score was still assigned to this player. Their standings and prize money are governed
        elsewhere and are untouched by this number."""
        event = self._make_event("DQ Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team, status="disqualified")
        self._play_squad_line(lb, tt, self.player, number=1)

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    def test_a_fielded_player_on_a_team_marked_no_show_still_counts(self):
        """Three real players are in exactly this state on event 133 (team QX4): flagged
        absent by the organizer, yet carrying scored match lines for that event."""
        event = self._make_event("No Show Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team, is_no_show=True)
        self._play_squad_line(lb, tt, self.player, number=1)

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    # ── the event still has to be real ────────────────────────────────────────────────
    def test_a_draft_event_does_not_count(self):
        """A draft is an organizer's unpublished sketch, not a competition."""
        event = self._make_event("Draft Cup", is_draft=True)
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        self._play_squad_line(lb, tt, self.player, number=1)

        self.assertEqual(counted_event_ids(self.player), set())

    def test_one_event_entered_both_ways_is_counted_once(self):
        """counted_event_ids returns a SET, so the solo and squad paths cannot double-count."""
        event = self._make_event("Both Ways")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        self._play_squad_line(lb, tt, self.player, number=1)
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=2)

        self.assertEqual(counted_event_ids(self.player), {event.event_id})

    # ── the split, and the two surfaces agreeing ──────────────────────────────────────
    def test_the_split_follows_the_events_competition_type(self):
        played_tournament = self._make_event("Split Cup", competition_type="tournament")
        lb_t = self._make_leaderboard(played_tournament)
        tt_t = self._make_tournament_team(played_tournament, self.team)
        self._play_squad_line(lb_t, tt_t, self.player, number=1)

        played_scrim = self._make_event("Split Scrim", competition_type="scrims")
        lb_s = self._make_leaderboard(played_scrim)
        tt_s = self._make_tournament_team(played_scrim, self.team)
        self._play_squad_line(lb_s, tt_s, self.player, number=1)

        # ... plus one of each the player only SIGNED UP for, which must not show up.
        for name, kind in (("Signed Cup", "tournament"), ("Signed Scrim", "scrims")):
            unplayed = self._make_event(name, competition_type=kind)
            unplayed_tt = self._make_tournament_team(unplayed, self.team)
            TournamentTeamMember.objects.create(
                tournament_team=unplayed_tt, user=self.player, event=unplayed, status="active")

        self.assertEqual(played_event_counts(self.player), (1, 1))

    def test_the_profile_and_the_public_player_page_report_the_same_pair(self):
        """The whole reason the rule lives in one module: get_user_profile and
        compute_player_stats must not describe two different careers for one person."""
        event = self._make_event("Agreement Cup")
        lb = self._make_leaderboard(event)
        tt = self._make_tournament_team(event, self.team)
        self._play_squad_line(lb, tt, self.player, number=1)
        scrim = self._make_event("Agreement Scrim", competition_type="scrims")
        lb_s = self._make_leaderboard(scrim)
        tt_s = self._make_tournament_team(scrim, self.team)
        self._play_squad_line(lb_s, tt_s, self.player, number=1)

        # Act: the expression get_user_profile uses, and the keys the player endpoints serve.
        own_profile_pair = played_event_counts(self.player)
        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(own_profile_pair, (1, 1))
        self.assertEqual(
            (stats["tournaments_played"], stats["scrims_played"]), own_profile_pair)


class SoloPlayIsPartOfTheSameCareerTests(_StatFixtureMixin, TestCase):
    """Bug F (found 2026-08-08): the two profile endpoints counted KILLS from different tables.

    afc_auth.views.get_user_profile has always summed SoloPlayerMatchStats + Tournament-
    PlayerMatchStats, while afc_player.aggregation.compute_player_stats walked only the squad
    table. A player who has only ever entered solo events therefore read real numbers on their
    OWN profile and 0 kills / 0 matches on the PUBLIC player page and the admin player detail.
    In the live database 109 players disagreed with themselves, 78 of them solo-only.

    Same family as the win-rate bug (one statistic, two populations), and the fix is the same
    shape: the aggregation reads both entry paths, so there is one population and one answer.
    """

    def setUp(self):
        self.admin = User.objects.create(
            username="soloadmin", email="soloadmin@x.com", full_name="Solo Admin",
            role="admin", password="x")
        self.player = User.objects.create(
            username="soloplayer", email="soloplayer@x.com", full_name="Solo Player",
            role="player", password="x")
        self.team = Team.objects.create(
            team_name="Solo Team", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)

    def _profile_kills(self, user):
        """The expression afc_auth.views.get_user_profile uses for total_kills: solo + squad."""
        solo = sum(SoloPlayerMatchStats.objects.filter(
            competitor__user=user).values_list("kills", flat=True))
        squad = sum(TournamentPlayerMatchStats.objects.filter(
            player=user).values_list("kills", flat=True))
        return solo + squad

    def test_a_solo_only_players_kills_are_not_zero_on_the_public_page(self):
        """THE REPORTED SYMPTOM. Two solo matches, 7 kills, and the public page said 0."""
        event = self._make_event("Solo Only Cup", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1, placement=4, kills=3)
        self._play_solo_line(event, competitor, number=2, placement=1, kills=4)

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_kills"], 7)
        self.assertEqual(stats["total_matches"], 2)
        self.assertEqual(self._profile_kills(self.player), stats["total_kills"])

    def test_the_two_endpoints_agree_when_a_player_has_played_BOTH_ways(self):
        """The mixed case: the squad half must not be dropped while adding the solo half."""
        squad_event = self._make_event("Mixed Squad Cup")
        lb = self._make_leaderboard(squad_event)
        tt = self._make_tournament_team(squad_event, self.team)
        self._play_squad_line(lb, tt, self.player, number=1, placement=3, kills=5)

        solo_event = self._make_event("Mixed Solo Cup", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=solo_event, user=self.player, status="registered")
        self._play_solo_line(solo_event, competitor, number=1, placement=2, kills=6)

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_kills"], 11)
        self.assertEqual(stats["total_matches"], 2)
        self.assertEqual(self._profile_kills(self.player), stats["total_kills"])

    def test_a_solo_win_counts_and_the_rate_stays_bounded(self):
        """A solo line placing 1st is that player's own win, and win_rate divides the two
        halves by the population BOTH were counted from."""
        event = self._make_event("Solo Win Cup", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1, placement=1, kills=2)
        self._play_solo_line(event, competitor, number=2, placement=9, kills=1)

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_wins"], 1)
        self.assertEqual(stats["total_matches"], 2)
        self.assertEqual(stats["win_rate"], 50.0)
        self.assertLessEqual(stats["win_rate"], 100.0)

    def test_a_zero_kill_solo_line_is_a_played_match_not_missing_data(self):
        """The falsy-zero guard, on the aggregation side this time: 0 kills is a real score,
        so the match still counts toward total_matches and the kill rate."""
        event = self._make_event("Solo Zero Cup", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1, placement=11, kills=0,
                             total_points=0)

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_matches"], 1)
        self.assertEqual(stats["total_kills"], 0)
        self.assertEqual(stats["kdr"], 0)

    def test_avg_damage_keeps_a_squad_only_denominator(self):
        """SoloPlayerMatchStats has no damage column, so solo matches must not dilute the
        average. One squad match at 300 damage plus three solo matches is still 300, not 75."""
        squad_event = self._make_event("Damage Cup")
        lb = self._make_leaderboard(squad_event)
        tt = self._make_tournament_team(squad_event, self.team)
        match = Match.objects.create(leaderboard=lb, match_number=1)
        team_stats = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt, placement=2, kills=4)
        TournamentPlayerMatchStats.objects.create(
            team_stats=team_stats, player=self.player, kills=4, damage=300, assists=0)

        solo_event = self._make_event("Damage Solo Cup", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=solo_event, user=self.player, status="registered")
        for n in (1, 2, 3):
            self._play_solo_line(solo_event, competitor, number=n, placement=5, kills=1)

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_matches"], 4)
        self.assertEqual(stats["avg_damage"], 300.0)

    def test_a_solo_only_player_gets_a_per_event_history_instead_of_a_blank_tab(self):
        """The Overview tab said "1 tournament played" while the Stats tab listed nothing,
        because per_event was built from squad rows only."""
        event = self._make_event("Solo History Cup", participant_type="solo")
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1, placement=3, kills=2,
                             total_points=8)
        self._play_solo_line(event, competitor, number=2, placement=6, kills=1,
                             total_points=4)

        stats = compute_player_stats(self.player)

        self.assertEqual(len(stats["per_event"]), 1)
        row = stats["per_event"][0]
        self.assertEqual(row["event_id"], event.event_id)
        self.assertEqual(row["kills"], 3)
        self.assertEqual(row["matches_played"], 2)
        self.assertEqual(row["best_placement"], 3)
        self.assertEqual(row["total_points"], 12)
        self.assertEqual(len(stats["recent_matches"]), 2)
        # And the headline count agrees with the history it is standing next to.
        self.assertEqual(stats["tournaments_played"], 1)

    def test_a_draft_solo_event_is_still_excluded(self):
        """The squad path gets the draft filter for free (no leaderboard -> no event); the
        solo path resolves its event directly, so it has to say so."""
        event = self._make_event("Draft Solo Cup", participant_type="solo", is_draft=True)
        competitor = RegisteredCompetitors.objects.create(
            event=event, user=self.player, status="registered")
        self._play_solo_line(event, competitor, number=1, placement=1, kills=9)

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["total_matches"], 0)
        self.assertEqual(stats["total_kills"], 0)
        self.assertEqual(stats["tournaments_played"], 0)

    def test_solo_kills_split_by_competition_type(self):
        """The scrims/tournaments kill split must see solo play too."""
        cup = self._make_event("Solo Split Cup", participant_type="solo")
        cup_competitor = RegisteredCompetitors.objects.create(
            event=cup, user=self.player, status="registered")
        self._play_solo_line(cup, cup_competitor, number=1, placement=2, kills=5)

        scrim = self._make_event("Solo Split Scrim", participant_type="solo",
                                 competition_type="scrims")
        scrim_competitor = RegisteredCompetitors.objects.create(
            event=scrim, user=self.player, status="registered")
        self._play_solo_line(scrim, scrim_competitor, number=1, placement=4, kills=2)

        stats = compute_player_stats(self.player, include_breakdown=False)

        self.assertEqual(stats["tournaments_kills"], 5)
        self.assertEqual(stats["scrims_kills"], 2)
        self.assertEqual(stats["total_kills"], 7)
