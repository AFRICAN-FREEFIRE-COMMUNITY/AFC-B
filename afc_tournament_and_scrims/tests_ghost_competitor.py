"""
afc_tournament_and_scrims.tests_ghost_competitor - a TournamentTeam may represent a GHOST.

Covers spec WEBSITE/tasks/external-results-import-design.md section 4.4b: an external competitor
with no AFC account is an afc_rankings.GhostTeam, and a TournamentTeam may point at one instead of
a real afc_team.Team. Exactly one of the two is set, enforced in the database, mirroring the
team XOR ghost_team pattern afc_leaderboard.LeaderboardParticipant already uses.

HOW IT CONNECTS
    - GhostTeam is created and claimed through afc_rankings.admin_ghost; the claim moves history via
      afc_rankings.claims (extended for events in Task 8 of this plan).
    - The accessors asserted here (display_name / competitor / is_ghost) are what the ~172 call
      sites swept in Tasks 3 to 6 use instead of reaching through .team directly.
"""
import datetime

from django.db import IntegrityError, transaction
from django.test import TestCase

from afc_auth.models import User
from afc_team.models import Team
from afc_rankings.models import GhostTeam
from afc_tournament_and_scrims.models import Event, TournamentTeam


class GhostCompetitorSchemaTests(TestCase):
    """The XOR: a TournamentTeam is a real team or a ghost, never both, never neither."""

    def setUp(self):
        self.actor = User.objects.create(username="admin1", email="a@example.com")
        # Required fields beyond event_name/dates/number_of_stages were filled in from the model's
        # field list (Ruling 4: fixtures are the implementer's to adapt to build; the assertions
        # below are the unmodified contract). Shape matches the existing TournamentTeam-in-Event
        # fixture in tests_letter_constraint.py so both read the same way.
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="FFWS Africa 2026 Fall", event_mode="virtual",
            start_date=datetime.date(2026, 7, 1), end_date=datetime.date(2026, 9, 6),
            registration_open_date=datetime.date(2026, 6, 1), registration_end_date=datetime.date(2026, 6, 30),
            prizepool="0", event_rules="rules", event_status="upcoming",
            registration_link="https://example.com/reg", number_of_stages=4,
        )
        # join_settings/team_creator/team_owner have no default on Team either; same Ruling 4 fill-in,
        # matching the Team fixture shape in tests_letter_constraint.py.
        self.real_team = Team.objects.create(
            team_name="Berserk Generation", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        self.ghost = GhostTeam.objects.create(
            team_name="Otaku Gamer", country="Madagascar", created_by=self.actor,
        )

    def test_real_team_registration_still_works(self):
        """The existing shape is untouched: team set, ghost_team null."""
        tt = TournamentTeam.objects.create(event=self.event, team=self.real_team)
        self.assertIsNotNone(tt.pk)
        self.assertIsNone(tt.ghost_team)

    def test_ghost_registration_is_accepted(self):
        tt = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)
        self.assertIsNotNone(tt.pk)
        self.assertIsNone(tt.team)

    def test_neither_set_is_rejected_by_model_validation(self):
        """"Competes as nobody" is refused by clean(), NOT by the database, and the difference is
        forced on us rather than chosen.

        MySQL cannot defer constraint checks, so when Django cascade-deletes a Team it NULLS the
        nullable `team` column first and deletes the row immediately after. A strict database XOR
        rejects that transient instant and makes deleting ANY team fail (it did, in
        afc_team.tests_transfer_feed). So the DB constraint only forbids BOTH set, and "at least one
        set" moved to clean(), which is the path a real application write takes.
        """
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            TournamentTeam(event=self.event).clean()

    def test_deleting_a_team_still_works(self):
        """The regression this constraint change exists for. Deleting a Team cascades to its
        registrations, and on MySQL Django nulls the column before deleting the row. Under the
        original strict XOR this raised
        "Check constraint 'tt_team_xor_ghost' is violated" and no team could ever be deleted."""
        team = Team.objects.create(
            team_name="Doomed FC", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        TournamentTeam.objects.create(event=self.event, team=team)

        team.delete()

        self.assertFalse(
            TournamentTeam.objects.filter(event=self.event, team_id=team.pk).exists())

    def test_both_set_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TournamentTeam.objects.create(
                    event=self.event, team=self.real_team, ghost_team=self.ghost,
                )

    def test_one_registration_per_ghost_per_event(self):
        """The ghost twin of uniq_event_team_registration. Without it an import that runs twice
        registers the same ghost again and double-counts it in the standings."""
        TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)

    def test_two_different_ghosts_coexist_in_one_event(self):
        """Guards against a unique constraint that accidentally collapses all ghosts. MySQL and
        Postgres both allow multiple NULLs in a unique index, which is what makes this work."""
        other = GhostTeam.objects.create(
            team_name="Axis Hells", country="Mozambique", created_by=self.actor,
        )
        TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)
        TournamentTeam.objects.create(event=self.event, ghost_team=other)
        self.assertEqual(TournamentTeam.objects.filter(event=self.event).count(), 2)


class GhostCompetitorAccessorTests(TestCase):
    """The three questions callers actually ask, each with exactly one answer."""

    def setUp(self):
        self.actor = User.objects.create(username="admin2", email="b@example.com")
        # Same fixture shape as GhostCompetitorSchemaTests.setUp above (Ruling 4: fixtures are the
        # implementer's to adapt to build; the brief's setUp was written from the model's field list,
        # not a running database, and is missing required fields on Event and Team).
        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="FFWS Africa 2026 Fall", event_mode="virtual",
            start_date=datetime.date(2026, 7, 1), end_date=datetime.date(2026, 9, 6),
            registration_open_date=datetime.date(2026, 6, 1), registration_end_date=datetime.date(2026, 6, 30),
            prizepool="0", event_rules="rules", event_status="upcoming",
            registration_link="https://example.com/reg", number_of_stages=4,
        )
        self.real_team = Team.objects.create(
            team_name="Berserk Generation", join_settings="open",
            team_creator=self.actor, team_owner=self.actor,
        )
        self.ghost = GhostTeam.objects.create(
            team_name="Otaku Gamer", country="Madagascar", created_by=self.actor,
        )
        self.tt_real = TournamentTeam.objects.create(event=self.event, team=self.real_team)
        self.tt_ghost = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost)

    def test_display_name_reads_the_real_team(self):
        self.assertEqual(self.tt_real.display_name, "Berserk Generation")

    def test_display_name_reads_the_ghost(self):
        self.assertEqual(self.tt_ghost.display_name, "Otaku Gamer")

    def test_competitor_returns_the_underlying_object(self):
        self.assertEqual(self.tt_real.competitor, self.real_team)
        self.assertEqual(self.tt_ghost.competitor, self.ghost)

    def test_is_ghost(self):
        self.assertFalse(self.tt_real.is_ghost)
        self.assertTrue(self.tt_ghost.is_ghost)

    def test_str_does_not_crash_on_a_ghost(self):
        """__str__ currently reads self.team.team_name and is called by the Django admin, by
        repr() in tracebacks, and by several log lines. A ghost row must not blow those up."""
        self.assertIn("Otaku Gamer", str(self.tt_ghost))
