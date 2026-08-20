"""
afc_tournament_and_scrims.test_dedupe_event_team_registrations - the OLDER (event, team) collapse
command (owner 2026-07-05, Bug C). No test module existed for this command before Task 6 of the
ghost-competitors-in-events plan (owner 2026-08-20) - it was the more dangerous of the two dedupe
commands precisely because nothing was catching a regression in it.

Covers the same (event, team, ghost_team) grouping fix as test_dedupe_tournament_teams.py, PLUS the
command-2-specific hazard: RegisteredCompetitors.team is nullable for a totally different reason than
TournamentTeam.team is (team=None on RegisteredCompetitors means a SOLO player, not a ghost - that
model has no ghost_team column at all). A ghost duplicate group has team_id=None, so collapsing
"extra" RegisteredCompetitors rows with filter(team_id=None) for that group would match and delete
every solo player's registration in the event instead of the ghost's own rows.
See dedupe_event_team_registrations.py's module docstring for the full writeup.

Uses TransactionTestCase + a live DDL drop of the relevant unique index(es) so a genuine legacy
duplicate can be inserted at all (the constraints these commands exist to enable would otherwise
reject the second row) - same technique test_dedupe_tournament_teams.py already uses.

Run:
    ./.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_dedupe_event_team_registrations
"""
import datetime

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from afc_rankings.models import GhostTeam
from afc_team.models import Team

from .models import (
    Event, Match, RegisteredCompetitors, Stages, StageGroups,
    TournamentTeam, TournamentTeamMatchStats,
)

User = get_user_model()
TODAY = datetime.date.today()


def _u(p="x"):
    import uuid
    return f"{p}-{uuid.uuid4().hex[:10]}"


def _drop_constraint(name):
    """Remove a TournamentTeam unique/check constraint by name so a test can create the legacy
    duplicate row the constraint would otherwise reject. Idempotent (the try/except no-ops once the
    constraint is already gone).

    CALLERS MUST RESTORE IT with _restore_constraint, via self.addCleanup. These are
    TransactionTestCase classes, so DDL is NOT rolled back between tests: a dropped constraint stays
    dropped for the rest of the process. Leaving it off made
    tests_ghost_competitor.test_one_registration_per_ghost_per_event fail with "IntegrityError not
    raised" on a full-suite run, because by the time it ran the constraint it asserts on no longer
    existed. The failure surfaced in a DIFFERENT module from its cause, which is what made it
    expensive to find, and it is invisible when either module is run on its own.
    """
    cons = [c for c in TournamentTeam._meta.constraints if c.name == name]
    if cons:
        with connection.schema_editor(atomic=False) as se:
            try:
                se.remove_constraint(TournamentTeam, cons[0])
            except Exception:
                pass  # already absent on this backend


def _restore_constraint(name):
    """Put back a constraint dropped by _drop_constraint, so the schema this process shares with
    every later test matches what the models declare. Idempotent and never raises: a backend that
    never applied it, or a double cleanup, must not turn teardown into an error."""
    cons = [c for c in TournamentTeam._meta.constraints if c.name == name]
    if cons:
        with connection.schema_editor(atomic=False) as se:
            try:
                se.add_constraint(TournamentTeam, cons[0])
            except Exception:
                pass  # already present, or unsupported on this backend


def _make_event(owner):
    return Event.objects.create(
        slug=_u("event"), competition_type="tournament", participant_type="squad",
        event_type="internal", max_teams_or_players=16, event_name="E", event_mode="virtual",
        start_date=TODAY, end_date=TODAY, registration_open_date=TODAY, registration_end_date=TODAY,
        prizepool="$1", prize_distribution={}, event_rules="r", event_status="ongoing",
        registration_link="https://x.co/r", number_of_stages=1,
    )


class DedupeEventTeamRegistrationsTests(TransactionTestCase):
    """The command's original job (real teams) - untouched by the ghost fix, verified so a
    regression there is caught alongside the ghost tests below."""
    reset_sequences = False

    def setUp(self):
        _drop_constraint("uniq_event_team_registration")
        # TransactionTestCase does not roll back DDL, so put it back or every LATER test in this
        # process runs without it (see _drop_constraint).
        self.addCleanup(_restore_constraint, "uniq_event_team_registration")

        self.owner = User.objects.create_user(username=_u("u"), email=f"{_u('e')}@t.local",
                                               password="pw-strong-9273", role="player")
        self.event = _make_event(self.owner)
        self.stage = Stages.objects.create(event=self.event, stage_name="S1", start_date=TODAY,
                                            end_date=TODAY, number_of_groups=1,
                                            stage_format="br - normal", teams_qualifying_from_stage=8)
        self.group = StageGroups.objects.create(stage=self.stage, group_name="A", playing_date=TODAY,
                                                playing_time=datetime.time(12, 0), teams_qualifying=8,
                                                match_count=1, match_maps=[])
        self.team = Team.objects.create(team_name=_u("T"), join_settings="open",
                                        team_creator=self.owner, team_owner=self.owner)

    def test_duplicate_real_team_collapses_to_the_row_with_stats(self):
        """Survivor rule: the row that HAS match stats is kept even if it is not the lowest id."""
        no_stats = TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        has_stats = TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        match = Match.objects.create(group=self.group, match_number=1, match_map="bermuda")
        TournamentTeamMatchStats.objects.create(match=match, tournament_team=has_stats, placement=1)

        # Two RegisteredCompetitors rows for the same (event, team) - proves the RC collapse this
        # command also does is still exercised (and still works) for a REAL team group.
        RegisteredCompetitors.objects.create(event=self.event, team=self.team, status="registered")
        RegisteredCompetitors.objects.create(event=self.event, team=self.team, status="registered")

        call_command("dedupe_event_team_registrations", "--apply")

        self.assertFalse(TournamentTeam.objects.filter(pk=no_stats.pk).exists())
        self.assertTrue(TournamentTeam.objects.filter(pk=has_stats.pk).exists())
        self.assertEqual(
            RegisteredCompetitors.objects.filter(event=self.event, team=self.team).count(), 1,
        )

    def test_no_dupes_is_a_noop(self):
        TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        call_command("dedupe_event_team_registrations", "--apply")  # must not raise
        self.assertEqual(TournamentTeam.objects.filter(event=self.event, team=self.team).count(), 1)


class DedupeEventTeamRegistrationsGhostTests(TransactionTestCase):
    """The (event, team, ghost_team) grouping fix, plus the RegisteredCompetitors solo-safety guard
    (Task 6, owner 2026-08-20, external results import)."""
    reset_sequences = False

    def setUp(self):
        self.owner = User.objects.create_user(username=_u("u"), email=f"{_u('e')}@t.local",
                                               password="pw-strong-9273", role="player")
        self.event = _make_event(self.owner)
        self.team = Team.objects.create(team_name=_u("T"), join_settings="open",
                                        team_creator=self.owner, team_owner=self.owner)
        self.ghost_a = GhostTeam.objects.create(team_name="Ghost A", country="Nigeria", created_by=self.owner)
        self.ghost_b = GhostTeam.objects.create(team_name="Ghost B", country="Kenya", created_by=self.owner)

    def test_two_different_ghosts_are_not_merged(self):
        """Two DIFFERENT ghosts registered to the same event must never be reported (or merged) as
        duplicates of each other - before the (event, team, ghost_team) key, both landed in the same
        (event, None) bucket the plain (event, team) grouping used."""
        tt_a = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_a, status="active")
        tt_b = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_b, status="active")

        call_command("dedupe_event_team_registrations", "--apply")

        self.assertTrue(TournamentTeam.objects.filter(pk=tt_a.pk).exists())
        self.assertTrue(TournamentTeam.objects.filter(pk=tt_b.pk).exists())
        self.assertEqual(TournamentTeam.objects.filter(event=self.event).count(), 2)

    def test_mixed_real_dupe_and_ghosts_in_same_event_does_not_crash(self):
        """A real (event, team) duplicate group AND two distinct, non-duplicate ghosts in the SAME
        event - exercises the report-loop sort key, which must compare team_id/ghost_team_id as
        strings once event_id ties (an int/None pair against a None/uuid pair is not orderable in
        Python 3 as raw values). Also re-confirms a genuine same-team duplicate is still caught
        alongside ghosts (the richer per-child check lives in
        DedupeEventTeamRegistrationsTests.test_duplicate_real_team_collapses_to_the_row_with_stats)."""
        _drop_constraint("uniq_event_team_registration")
        # TransactionTestCase does not roll back DDL, so put it back or every LATER test in this
        # process runs without it (see _drop_constraint).
        self.addCleanup(_restore_constraint, "uniq_event_team_registration")
        TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        ghost_tt_a = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_a, status="active")
        ghost_tt_b = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_b, status="active")

        call_command("dedupe_event_team_registrations", "--apply")  # must not raise TypeError

        self.assertEqual(TournamentTeam.objects.filter(event=self.event, team=self.team).count(), 1)
        self.assertTrue(TournamentTeam.objects.filter(pk=ghost_tt_a.pk).exists())
        self.assertTrue(TournamentTeam.objects.filter(pk=ghost_tt_b.pk).exists())

    def test_ghost_duplicate_does_not_delete_solo_registered_competitors(self):
        """THE critical regression this task exists to prevent. A genuine ghost duplicate group has
        team_id=None, same as every SOLO player's RegisteredCompetitors row (team is null there for a
        solo registration - RegisteredCompetitors has no ghost_team column at all). Before the guard,
        the RC-collapse step ran unconditionally with filter(event_id=event_id, team_id=None), which
        would match every solo competitor's row in the whole event and delete all but one of them.

        Forces a genuine ghost dupe by dropping uniq_event_ghost_registration (the constraint that
        would otherwise make this row impossible to create through the ORM) - mirroring exactly how
        this command is meant to be run: to clean up legacy rows that predate the constraint."""
        _drop_constraint("uniq_event_ghost_registration")
        self.addCleanup(_restore_constraint, "uniq_event_ghost_registration")
        ghost_dupe_1 = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_a, status="active")
        ghost_dupe_2 = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_a, status="active")

        # Three unrelated SOLO players registered for the SAME event (team is null - the population
        # the bug would have wiped out). Distinct users so they are genuinely separate registrations.
        solo_users = [
            User.objects.create_user(username=_u("solo"), email=f"{_u('e')}@t.local",
                                      password="pw-strong-9273", role="player")
            for _ in range(3)
        ]
        solo_regs = [
            RegisteredCompetitors.objects.create(event=self.event, user=u, team=None, status="registered")
            for u in solo_users
        ]

        call_command("dedupe_event_team_registrations", "--apply")

        # The ghost dupe itself was still collapsed to one row (the command's actual job)...
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, ghost_team=self.ghost_a).count(), 1,
        )
        self.assertTrue(
            TournamentTeam.objects.filter(pk=ghost_dupe_1.pk).exists()
            or TournamentTeam.objects.filter(pk=ghost_dupe_2.pk).exists(),
        )
        # ...but every solo player's registration in the event is completely untouched.
        for reg in solo_regs:
            self.assertTrue(
                RegisteredCompetitors.objects.filter(pk=reg.pk).exists(),
                f"solo registration {reg.pk} was deleted by a ghost group's RegisteredCompetitors collapse",
            )
        self.assertEqual(
            RegisteredCompetitors.objects.filter(event=self.event, team__isnull=True).count(), 3,
        )
