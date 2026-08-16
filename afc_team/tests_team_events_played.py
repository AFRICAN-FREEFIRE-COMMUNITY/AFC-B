r"""A TEAM's "Tournaments Played" card must mean the same thing as a PLAYER's.

THE BUG (owner ruling 2026-08-08): "It should count events played. Matches they participated
in where a score was assigned to them."

The player-side counter was moved onto that rule (afc_tournament_and_scrims/participation.py).
The TEAM-side counter on the team page's Overview tab was left behind: get_team_details builds
`tournament_performance` from every TournamentTeam row, i.e. every REGISTRATION, and then counted
those rows. So an event a team signed up for and never played still appeared as a tournament
played, and the team card and the player card disagreed about the very same event.

The team's evidence of play is its own scored match lines, so the gate is `matches_played > 0`
(a COUNT of TournamentTeamMatchStats rows, never a truthiness test on a score - a team that
played and scored nothing has matches_played >= 1 and still counts).

The per-event breakdown list is deliberately NOT filtered: an entered-but-not-played event is
still part of the team's history and is shown there honestly, with zeros.

Run: .venv\Scripts\python.exe manage.py test afc_team.tests_team_events_played
"""
from django.test import TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    Stages,
    TournamentTeam,
    TournamentTeamMatchStats,
)


class TeamEventsPlayedCountsScoredMatchesTests(TestCase):
    """get_team_details.stats.tournaments_played / scrims_played."""

    def setUp(self):
        # A member viewer, because the detailed stats block is gated to current members
        # and admins (see _can_view_team_stats in afc_team/views.py).
        self.admin = User.objects.create(
            username="tepadmin", email="tepadmin@x.com", full_name="TEP Admin",
            role="admin", password="x")
        self.member = User.objects.create(
            username="tepmember", email="tepmember@x.com", full_name="TEP Member",
            role="player", password="x")
        # SessionToken does NOT auto-generate its token; it must be passed.
        self.token = SessionToken.objects.create(user=self.member, token="tok_tep_member")
        self.team = Team.objects.create(
            team_name="TEP Team", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)
        TeamMembers.objects.create(team=self.team, member=self.member)

    # ── fixture helpers, mirroring afc_player/tests_stat_correctness.py ──────────────────
    def _make_event(self, name, *, competition_type="tournament"):
        today = timezone.localdate()
        return Event.objects.create(
            competition_type=competition_type, participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_name=name,
            event_mode="virtual", start_date=today, end_date=today,
            registration_open_date=today, registration_end_date=today, prizepool="0",
            event_rules="r", event_status="completed",
            registration_link="https://x.com/r", number_of_stages=1, creator=self.admin)

    def _register(self, event):
        """The team ENTERS the event. This alone is not play."""
        return TournamentTeam.objects.create(event=event, team=self.team, status="active")

    def _play(self, event, tournament_team, *, placement=3, kills=4, total_points=10):
        """One scored match line for the team, which is the evidence of play."""
        today = timezone.localdate()
        stage = Stages.objects.create(
            event=event, stage_name="Stage 1", start_date=today, end_date=today,
            number_of_groups=1, stage_format="battle_royale", teams_qualifying_from_stage=1)
        leaderboard = Leaderboard.objects.create(
            leaderboard_name=f"LB {event.event_name}", event=event, stage=stage,
            creator=self.admin, leaderboard_method="automatic")
        match = Match.objects.create(leaderboard=leaderboard, match_number=1)
        return TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tournament_team, placement=placement,
            kills=kills, total_points=total_points)

    def _stats(self):
        res = self.client.post(
            "/team/get-team-details/", {"team_name": self.team.team_name},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()["team"]
        self.assertTrue(body["stats_visible"], "member viewer should see the stats block")
        return body

    # ── the reported meaning ────────────────────────────────────────────────────────────
    def test_an_event_the_team_entered_but_never_played_does_not_count(self):
        """THE BUG. Registration is not play, for a team exactly as for a player."""
        event = self._make_event("Entered Never Played Cup")
        self._register(event)

        stats = self._stats()["stats"]

        self.assertEqual(stats["tournaments_played"], 0)
        self.assertEqual(stats["scrims_played"], 0)

    def test_an_event_with_a_scored_match_line_counts(self):
        event = self._make_event("Really Played Cup")
        tt = self._register(event)
        self._play(event, tt)

        self.assertEqual(self._stats()["stats"]["tournaments_played"], 1)

    def test_a_zero_score_match_still_counts_as_played(self):
        """The falsy-zero guard: a team that turned up and scored nothing still played.
        This repo has shipped three separate bugs from treating a real 0 as no data."""
        event = self._make_event("Zero Score Cup")
        tt = self._register(event)
        self._play(event, tt, placement=12, kills=0, total_points=0)

        self.assertEqual(self._stats()["stats"]["tournaments_played"], 1)

    def test_the_split_follows_competition_type(self):
        cup = self._make_event("Split Team Cup", competition_type="tournament")
        self._play(cup, self._register(cup))
        scrim = self._make_event("Split Team Scrim", competition_type="scrims")
        self._play(scrim, self._register(scrim))
        # ... plus one of each that was only entered.
        self._register(self._make_event("Unplayed Team Cup", competition_type="tournament"))
        self._register(self._make_event("Unplayed Team Scrim", competition_type="scrims"))

        stats = self._stats()["stats"]

        self.assertEqual(stats["tournaments_played"], 1)
        self.assertEqual(stats["scrims_played"], 1)

    def test_the_event_history_still_lists_the_unplayed_entry(self):
        """Only the COUNTER changed. The per-event breakdown keeps the entered-but-not-played
        event, honestly showing zero matches, so nothing disappears from the team's history."""
        played = self._make_event("Listed Played Cup")
        self._play(played, self._register(played))
        entered_only = self._make_event("Listed Entered Cup")
        self._register(entered_only)

        body = self._stats()
        # tournament_performance rows carry the event name under "name" (not "event_name").
        names = {row["name"]: row for row in body["tournament_performance"]}

        self.assertIn("Listed Entered Cup", names)
        self.assertEqual(names["Listed Entered Cup"]["matches_played"], 0)
        self.assertEqual(names["Listed Played Cup"]["matches_played"], 1)
        self.assertEqual(body["stats"]["tournaments_played"], 1)
