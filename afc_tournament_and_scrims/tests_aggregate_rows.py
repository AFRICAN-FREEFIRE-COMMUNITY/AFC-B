"""
afc_tournament_and_scrims.tests_aggregate_rows - one stats row standing for SEVERAL matches.

Covers spec WEBSITE/tasks/external-results-import-design.md sections 4.1, 4.2 and 5, built as
WEBSITE/tasks/plan-2-aggregate-result-rows.md.

WHY AGGREGATE ROWS EXIST
    AFC carries tournaments it did not run, and their organizers publish a standings GRAPHIC, not a
    match log: "6 matches, 3 Booyahs, 47 placement, 82 elims, 129 total" for a whole group. Splitting
    that into six per-match rows would invent data (47/6 is not what any match scored) that is then
    indistinguishable from real results forever. So one flagged row carries the summed total.

THE TEST THAT MATTERS MOST here is test_matches_played_counts_the_aggregate_span: a team with an
aggregate row worth 6 matches plus 8 real ones must report 14 matches played, not 9. Counting ROWS
is the failure this whole plan exists to prevent, and it is a visibly wrong number on a public
profile rather than a crash.

HOW IT CONNECTS
    - Rows are written by the xlsx importer (Plan 3), which does not exist yet; these tests build
      them directly so the storage and the readers can be locked in first.
    - The readers exercised here are the same expressions used by afc_team/views.py's per-event team
      performance block and by afc_partner_api.serialize.

Run: python manage.py test afc_tournament_and_scrims.tests_aggregate_rows
"""
import datetime

from django.db.models import Sum, Min, Count
from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, TournamentTeam, TournamentTeamMatchStats,
)

TODAY = datetime.date.today()


class AggregateResultRowTests(TestCase):
    """Storage: an aggregate row holds a summed total and says so."""

    def setUp(self):
        self.actor = User.objects.create(username="aggadmin", email="agg@example.com")
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="FFWS Africa 2026 Fall", event_mode="virtual",
            start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="rules", event_status="ongoing",
            registration_link="https://example.com/reg", number_of_stages=4,
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Play-ins Phase 1", start_date=TODAY, end_date=TODAY,
            number_of_groups=12, stage_format="br - normal", teams_qualifying_from_stage=43,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="A", playing_date=TODAY,
            playing_time=datetime.time(12, 0), teams_qualifying=3, match_count=6, match_maps=[],
        )
        self.team = Team.objects.create(
            team_name="Elite Hunters", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        self.tt = TournamentTeam.objects.create(event=self.event, team=self.team)

    def _aggregate_match(self):
        """The synthetic match an aggregate row hangs off. A BO6 group spans several maps, so the
        map is "multiple" rather than an arbitrary pick."""
        return Match.objects.create(
            group=self.group, match_number=1, match_map="multiple",
            upload_method="xlsx_import", result_inputted=True, played_on=TODAY,
        )

    def test_ordinary_row_is_unchanged_by_the_new_fields(self):
        """Every row that already exists in production must read as a single real match, with no
        backfill. That is what the defaults are for."""
        match = Match.objects.create(group=self.group, match_number=1, match_map="bermuda")
        row = TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=self.tt, placement=3, kills=11, total_points=25,
        )
        self.assertFalse(row.is_aggregate)
        self.assertEqual(row.matches_counted, 1)
        self.assertEqual(row.booyah_count, 0)
        self.assertIsNone(row.final_position)
        self.assertEqual(row.placement, 3)

    def test_aggregate_row_stores_the_published_totals(self):
        """The real FFWS Group A line for Elite Hunters: 6 matches, 3 Booyahs, 47 placement points,
        82 eliminations, 129 total, finishing 1st."""
        row = TournamentTeamMatchStats.objects.create(
            match=self._aggregate_match(), tournament_team=self.tt,
            placement=None, is_aggregate=True, matches_counted=6, booyah_count=3,
            final_position=1, placement_points=47, kills=82, total_points=129,
        )
        row.refresh_from_db()
        self.assertTrue(row.is_aggregate)
        self.assertIsNone(row.placement)
        self.assertEqual(
            (row.matches_counted, row.booyah_count, row.final_position, row.total_points),
            (6, 3, 1, 129),
        )

    def test_placement_null_does_not_poison_best_placement(self):
        """`Min("placement")` is read as "best single map result". An aggregate row must contribute
        NOTHING to it. This is why the field is NULL rather than a 0 sentinel: 0 would win every
        Min() and report a nonexistent "0th place" as the team's best ever result."""
        real = Match.objects.create(group=self.group, match_number=2, match_map="bermuda")
        TournamentTeamMatchStats.objects.create(
            match=real, tournament_team=self.tt, placement=4, total_points=10)
        TournamentTeamMatchStats.objects.create(
            match=self._aggregate_match(), tournament_team=self.tt,
            placement=None, is_aggregate=True, matches_counted=6, final_position=1, total_points=129)

        best = TournamentTeamMatchStats.objects.filter(
            tournament_team=self.tt).aggregate(b=Min("placement"))["b"]

        self.assertEqual(best, 4)

    def test_matches_played_counts_the_aggregate_span(self):
        """THE regression this plan exists to prevent.

        A team with one aggregate row worth 6 matches plus 8 real Grand Final rows has played 14
        matches. COUNTING ROWS says 9, and 9 is a visibly wrong number on a public profile."""
        TournamentTeamMatchStats.objects.create(
            match=self._aggregate_match(), tournament_team=self.tt,
            placement=None, is_aggregate=True, matches_counted=6, final_position=1, total_points=129)
        for n in range(8):
            m = Match.objects.create(group=self.group, match_number=10 + n, match_map="alpine")
            TournamentTeamMatchStats.objects.create(
                match=m, tournament_team=self.tt, placement=2, total_points=12)

        agg = TournamentTeamMatchStats.objects.filter(tournament_team=self.tt).aggregate(
            counted_rows=Count("team_stats_id"),
            matches_played=Sum("matches_counted"),
        )

        self.assertEqual(agg["counted_rows"], 9)      # the WRONG answer, kept to show the difference
        self.assertEqual(agg["matches_played"], 14)   # the right one

    def test_total_points_sums_correctly_across_both_kinds(self):
        """The premise of the whole design: `Sum("total_points")` needed NO change anywhere, because
        an aggregate row simply carries a bigger number in the same column."""
        TournamentTeamMatchStats.objects.create(
            match=self._aggregate_match(), tournament_team=self.tt,
            placement=None, is_aggregate=True, matches_counted=6, total_points=129)
        m = Match.objects.create(group=self.group, match_number=20, match_map="alpine")
        TournamentTeamMatchStats.objects.create(
            match=m, tournament_team=self.tt, placement=1, total_points=21)

        total = TournamentTeamMatchStats.objects.filter(
            tournament_team=self.tt).aggregate(t=Sum("total_points"))["t"]

        self.assertEqual(total, 150)

    def test_booyahs_cannot_be_derived_from_an_aggregate_row(self):
        """Readers that count `placement=1` rows to get Booyahs (afc_partner_api.serialize._BOOYAH
        does exactly this) see ZERO for an aggregate row no matter how many times the team really
        won. The stored booyah_count is the only truthful source, which is why the field exists."""
        TournamentTeamMatchStats.objects.create(
            match=self._aggregate_match(), tournament_team=self.tt,
            placement=None, is_aggregate=True, matches_counted=6, booyah_count=3, total_points=129)

        derived = TournamentTeamMatchStats.objects.filter(
            tournament_team=self.tt, placement=1).count()
        stored = TournamentTeamMatchStats.objects.filter(
            tournament_team=self.tt).aggregate(b=Sum("booyah_count"))["b"]

        self.assertEqual(derived, 0)   # what counting rows would say
        self.assertEqual(stored, 3)    # what actually happened
