"""
DB-level enforcement test for the per-event assigned-letter uniqueness (Gap 3, feature #7).

TournamentTeam.assigned_letter is the single A-Z letter an admin/organizer assigns to a registered
team for in-game use in one event (see assign_team_letter). The Meta UniqueConstraint
`uniq_assigned_letter_per_event` must guarantee, AT THE DATABASE LEVEL, that no two teams in the
same event hold the same non-null letter — while leaving any number of UNASSIGNED teams
(assigned_letter = NULL) free of collisions.

The constraint used to carry `condition=Q(assigned_letter__isnull=False)` (a partial index). MySQL —
the production database — silently IGNORES that condition, so it gave ZERO enforcement there. It is
now a PLAIN UniqueConstraint, which MySQL DOES enforce, and because MySQL/Postgres both allow
MULTIPLE NULLs in a unique index, unassigned teams still don't collide. These tests pin both halves:
duplicate non-null letter -> IntegrityError; many NULLs -> fine; same letter in a DIFFERENT event ->
fine. They run against the project's real (MySQL) test database, so they exercise the actual engine
behaviour the production swap depends on.
"""
import datetime

from django.db import IntegrityError, transaction
from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import Event, TournamentTeam


class AssignedLetterUniqueConstraintTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create(
            username="letter_admin", email="letter_admin@example.com",
            full_name="Letter Admin", role="admin", password="x",
        )
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Letter Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="rules", event_status="ongoing",
            registration_link="https://example.com/reg", number_of_stages=1, creator=self.admin,
        )
        # A second event to prove the constraint is scoped PER EVENT (same letter elsewhere is fine).
        self.other_event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Other Letter Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today, registration_end_date=today,
            prizepool="0", event_rules="rules", event_status="ongoing",
            registration_link="https://example.com/reg", number_of_stages=1, creator=self.admin,
        )

    def _team(self, name, tag):
        return Team.objects.create(
            team_name=name, team_tag=tag, join_settings="open",
            team_creator=self.admin, team_owner=self.admin, country="NG",
        )

    def test_duplicate_non_null_letter_in_same_event_is_rejected(self):
        # First team takes "A" -> ok. Second team in the SAME event taking "A" -> DB IntegrityError.
        TournamentTeam.objects.create(
            event=self.event, team=self._team("Alpha", "ALP"),
            registered_by=self.admin, assigned_letter="A",
        )
        with self.assertRaises(IntegrityError):
            # atomic so the failed INSERT doesn't poison the outer test transaction.
            with transaction.atomic():
                TournamentTeam.objects.create(
                    event=self.event, team=self._team("Bravo", "BRV"),
                    registered_by=self.admin, assigned_letter="A",
                )

    def test_multiple_null_letters_in_same_event_coexist(self):
        # Several UNASSIGNED teams (assigned_letter=NULL) in one event must NOT collide — the whole
        # reason we keep NULLs out of the uniqueness. Three null-letter rows save cleanly.
        for name, tag in (("N1", "N1"), ("N2", "N2"), ("N3", "N3")):
            TournamentTeam.objects.create(
                event=self.event, team=self._team(name, tag), registered_by=self.admin,
            )
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, assigned_letter__isnull=True).count(), 3,
        )

    def test_same_letter_in_different_events_is_allowed(self):
        # The uniqueness is (event, assigned_letter): the SAME letter in a DIFFERENT event is fine.
        TournamentTeam.objects.create(
            event=self.event, team=self._team("Alpha", "ALP"),
            registered_by=self.admin, assigned_letter="A",
        )
        TournamentTeam.objects.create(
            event=self.other_event, team=self._team("Alpha2", "AL2"),
            registered_by=self.admin, assigned_letter="A",
        )
        self.assertEqual(TournamentTeam.objects.filter(assigned_letter="A").count(), 2)
