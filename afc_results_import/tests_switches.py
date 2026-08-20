"""
afc_results_import.tests_switches - what an imported tournament is allowed to affect.

Spec section 11, built as WEBSITE/tasks/plan-4-switches-and-presentation.md.

THE POINT OF THESE SWITCHES
    An imported event carries results AFC did not observe. Letting them silently change a real
    team's public numbers, or the rankings ladder, would mean one bad spreadsheet quietly rewriting
    the scene's record. So both profile switches default OFF and an admin turns them on after
    checking the numbers.

FOUR QUESTIONS, TWO NEW FIELDS. The other two were already answered by existing machinery and are
deliberately not duplicated:
    * feeds the rankings ladder -> afc_rankings.EventCountingControl.counts_toward_rankings
    * what tier is it          -> Event.tournament_tier + tier_overridden

Run: python manage.py test afc_results_import.tests_switches
"""
import datetime

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, Match, TournamentTeam, TournamentTeamMatchStats,
)

TODAY = datetime.date.today()


class ImportedEventSwitchTests(TestCase):
    """The two new Event switches, and the organizer-activity exclusion."""

    def setUp(self):
        self.actor = User.objects.create(username="switcher", email="sw@example.com")
        self.team = Team.objects.create(
            team_name="Berserk Generation", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        self.event = self._event("FFWS Africa 2026 Fall", imported=True)
        self.normal = self._event("AFC Cup", imported=False)

    def _event(self, name, *, imported):
        ev = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name=name, event_mode="virtual",
            start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="r", event_status="completed",
            registration_link="https://example.com/r", number_of_stages=1,
            results_imported_at=(timezone.now() if imported else None),
        )
        stage = Stages.objects.create(
            event=ev, stage_name="S1", start_date=TODAY, end_date=TODAY, number_of_groups=1,
            stage_format="br - normal", teams_qualifying_from_stage=4)
        group = StageGroups.objects.create(
            stage=stage, group_name="A", playing_date=TODAY,
            playing_time=datetime.time(12, 0), teams_qualifying=2, match_count=1, match_maps=[])
        tt = TournamentTeam.objects.create(event=ev, team=self.team)
        match = Match.objects.create(
            group=group, match_number=1,
            match_map=("multiple" if imported else "bermuda"),
            upload_method=("xlsx_import" if imported else "image_upload"),
        )
        TournamentTeamMatchStats.objects.create(
            match=match, tournament_team=tt,
            placement=(None if imported else 1),
            is_aggregate=imported,
            matches_counted=(6 if imported else 1),
            booyah_count=(3 if imported else 0),
            kills=82, total_points=129,
        )
        return ev

    def _career_totals(self):
        """The same query the team profile runs for a team's lifetime numbers."""
        return (TournamentTeamMatchStats.objects
                .filter(tournament_team__team=self.team)
                .exclude(
                    tournament_team__event__results_imported_at__isnull=False,
                    tournament_team__event__imported_results_count_in_profile_stats=False,
                )
                .aggregate(kills=Sum("kills"), matches=Sum("matches_counted")))

    def test_both_switches_default_off(self):
        """An import must not change anything on a public page until somebody says so."""
        self.assertFalse(self.event.imported_results_visible_on_profiles)
        self.assertFalse(self.event.imported_results_count_in_profile_stats)

    def test_an_unapproved_import_does_not_move_career_totals(self):
        """The imported event has 82 kills over 6 matches. With the statistics switch off, the
        team's lifetime numbers must show only the ordinary event."""
        totals = self._career_totals()
        self.assertEqual(totals["kills"], 82)      # the normal event only
        self.assertEqual(totals["matches"], 1)

    def test_turning_the_statistics_switch_on_folds_the_import_in(self):
        self.event.imported_results_count_in_profile_stats = True
        self.event.save(update_fields=["imported_results_count_in_profile_stats"])

        totals = self._career_totals()

        self.assertEqual(totals["kills"], 164)     # 82 normal + 82 imported
        # 1 real match + the 6 the aggregate row stands for. Counting ROWS would say 2.
        self.assertEqual(totals["matches"], 7)

    def test_an_ordinary_event_is_never_gated(self):
        """results_imported_at is NULL for everything AFC ran, so the exclusion cannot touch it."""
        self.assertIsNone(self.normal.results_imported_at)
        self.assertIn(
            self.normal.event_id,
            TournamentTeamMatchStats.objects
            .filter(tournament_team__team=self.team)
            .exclude(
                tournament_team__event__results_imported_at__isnull=False,
                tournament_team__event__imported_results_count_in_profile_stats=False,
            ).values_list("tournament_team__event_id", flat=True),
        )

    def test_imported_matches_are_not_organizer_activity(self):
        """Whoever uploads an external tournament's standings did not organize it, so counting those
        matches would overstate their activity."""
        counted = Match.objects.exclude(upload_method="xlsx_import")

        self.assertEqual(counted.filter(group__stage__event=self.event).count(), 0)
        self.assertEqual(counted.filter(group__stage__event=self.normal).count(), 1)
