"""
afc_tournament_and_scrims/tests_kill_stats_fields.py
================================================================================
The three kill-event design columns (knocks_recorded, sniper_kills, h2h_record; owner 2026-11-01,
inbox #399 / #421a) over the SAME fixture as tests_kill_feed.KillStatsEndpointTests: one duo map,
CREED against SAINT, six events (three knocks, three kills), SAINT wins the fight.

What is held here:
  * the numbers equal what the Kill stats tab shows for the same map (one rule, two surfaces);
  * a team with no maps in scope gets "-", never a 0 that pretends a map was read;
  * a sniper cell is "-" when no kill in the maps names a weapon;
  * a group id of ANOTHER event cannot pull that event's numbers (the scope is always the event's);
  * the overlay feed helper stamps the keys and the graphic export labels them.

Run: python manage.py test afc_tournament_and_scrims.tests_kill_stats_fields
"""
import datetime

from django.test import TestCase

from afc_leaderboard.graphic import COLUMN_HEADER_LABELS
from afc_organizers.views_leaderboard_design import FIELD_TYPES
from afc_tournament_and_scrims import kill_stats_fields
from afc_tournament_and_scrims.models import MatchKillEvent, StageGroups, TournamentTeam
from afc_tournament_and_scrims import tests_kill_feed
from afc_tournament_and_scrims.views import _overlay_kill_fields


class KillStatsFieldsTests(TestCase):
    """Borrows the endpoint tests' fixture (setUp) without inheriting their tests."""

    setUp = tests_kill_feed.KillStatsEndpointTests.setUp

    def _team_ids(self):
        return {t.team.team_name: t.tournament_team_id for t in TournamentTeam.objects.filter(event=self.event)}

    def test_the_numbers_match_the_kill_stats_tab(self):
        ids = self._team_ids()
        out = kill_stats_fields.team_kill_fields(self.event, {"group": self.match.group})
        # SAINT knocked twice (HANZO, WARD7) and killed with a sniper weapon once (113); CREED knocked once.
        self.assertEqual(out[ids["SAINT"]], {"knocks_recorded": 2, "sniper_kills": 1, "h2h_record": "1-0-0"})
        self.assertEqual(out[ids["CREED"]], {"knocks_recorded": 1, "sniper_kills": 0, "h2h_record": "0-0-1"})

    def test_a_team_with_nothing_recorded_shows_a_dash_not_a_zero(self):
        other = StageGroups.objects.create(
            stage=self.match.group.stage, group_name="B", playing_date=datetime.date.today(),
            playing_time=datetime.time(19, 0), teams_qualifying=2, match_count=1)
        per_team = kill_stats_fields.team_kill_fields(self.event, {"group": other})
        self.assertEqual(per_team, {})
        row = kill_stats_fields.fill_row({}, per_team, self._team_ids()["SAINT"])
        self.assertEqual(row, {"knocks_recorded": "-", "sniper_kills": "-", "h2h_record": "-"})

    def test_sniper_is_a_dash_when_the_game_named_no_weapon(self):
        MatchKillEvent.objects.filter(match=self.match, kind="kill").update(weapon_id=None)
        ids = self._team_ids()
        out = kill_stats_fields.team_kill_fields(self.event, {"group": self.match.group})
        self.assertEqual(out[ids["SAINT"]]["sniper_kills"], "-")
        self.assertEqual(out[ids["SAINT"]]["knocks_recorded"], 2, "knocks do not need a weapon")

    def test_another_events_group_cannot_pull_this_events_numbers(self):
        # The same group id, asked under a DIFFERENT event: scoped to the event, so nothing comes back.
        from afc_tournament_and_scrims.models import Event

        today = datetime.date.today()
        other_event = Event.objects.create(
            competition_type="tournament", participant_type="duo", event_type="internal",
            max_teams_or_players=16, event_name="Other Cup", event_mode="virtual", start_date=today,
            end_date=today, registration_open_date=today, registration_end_date=today, prizepool="0",
            event_rules="r", event_status="ongoing", registration_link="https://x.com/r", number_of_stages=1,
            creator=self.admin)
        out = kill_stats_fields.team_kill_fields(other_event, {"group_id__in": [self.match.group_id]})
        self.assertEqual(out, {})

    def test_the_overlay_helper_reads_the_maps_the_standings_filter_names(self):
        ids = self._team_ids()
        standings = [{"tournament_team_id": ids["SAINT"]}, {"tournament_team_id": ids["CREED"]}]
        out = _overlay_kill_fields(standings, {"team_stats__match__group": self.match.group})
        self.assertEqual(out[ids["SAINT"]]["knocks_recorded"], 2)
        # the cumulative path adds an event hop; it is dropped, the scope stays the event's
        out = _overlay_kill_fields(standings, {"team_stats__match__group__stage__event": self.event})
        self.assertEqual(out[ids["CREED"]]["h2h_record"], "0-0-1")
        self.assertEqual(_overlay_kill_fields(standings, None), {})

    def test_the_three_keys_are_placeable_and_labelled_on_the_export(self):
        for key in kill_stats_fields.DESIGN_KEYS:
            self.assertIn(key, FIELD_TYPES)
            self.assertTrue(COLUMN_HEADER_LABELS.get(key))
