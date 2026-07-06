"""
afc_tournament_and_scrims.test_dedupe_tournament_teams — the TournamentTeam merge command
(owner 2026-07-06, prod migrate failure on uniq_event_team_registration).

Verifies dedupe_tournament_teams safely MERGES a duplicate (event, team) registration into the
survivor: every child is repointed (never orphaned), a child that would collide with one the survivor
already owns is dropped (not duplicated), the RR-group M2M is remapped, and the empty loser is deleted.

Uses TransactionTestCase + a live DDL drop of the unique index so a duplicate row can be inserted at
all (the constraint that the command exists to enable would otherwise reject the second row).

Run:
    ./.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_dedupe_tournament_teams
"""
import datetime

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from afc_team.models import Team
from django.contrib.auth import get_user_model

from .models import (
    Event, Stages, StageGroups, StageCompetitor,
    TournamentTeam, TournamentTeamMember, Match, TournamentTeamMatchStats,
    RoundRobinGroup,
)

User = get_user_model()
TODAY = datetime.date.today()


def _u(p="x"):
    import uuid
    return f"{p}-{uuid.uuid4().hex[:10]}"


class DedupeTournamentTeamsTests(TransactionTestCase):
    reset_sequences = False

    def setUp(self):
        # Drop the unique index so we can craft a real duplicate (event, team) pair to merge.
        cons = [c for c in TournamentTeam._meta.constraints if c.name == "uniq_event_team_registration"]
        if cons:
            with connection.schema_editor(atomic=False) as se:
                try:
                    se.remove_constraint(TournamentTeam, cons[0])
                except Exception:
                    pass  # already absent on this backend

        self.owner = User.objects.create_user(username=_u("u"), email=f"{_u('e')}@t.local",
                                               password="pw-strong-9273", role="player")
        self.event = Event.objects.create(
            slug=_u("event"), competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_name="E", event_mode="virtual",
            start_date=TODAY, end_date=TODAY, registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="$1", prize_distribution={}, event_rules="r", event_status="ongoing",
            registration_link="https://x.co/r", number_of_stages=1,
        )
        self.stage = Stages.objects.create(event=self.event, stage_name="S1", start_date=TODAY,
                                            end_date=TODAY, number_of_groups=1,
                                            stage_format="br - normal", teams_qualifying_from_stage=8)
        self.group = StageGroups.objects.create(stage=self.stage, group_name="A", playing_date=TODAY,
                                                playing_time=datetime.time(12, 0), teams_qualifying=8,
                                                match_count=1, match_maps=[])
        self.team = Team.objects.create(team_name=_u("T"), join_settings="open",
                                        team_creator=self.owner, team_owner=self.owner)

    def test_merge_repoints_children_and_drops_collisions(self):
        # SURVIVOR: 2 match_stats (most data) + a roster member + a stage-pool row.
        survivor = TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        m1 = Match.objects.create(group=self.group, match_number=1, match_map="bermuda")
        m2 = Match.objects.create(group=self.group, match_number=2, match_map="kalahari")
        TournamentTeamMatchStats.objects.create(match=m1, tournament_team=survivor, placement=1)
        TournamentTeamMatchStats.objects.create(match=m2, tournament_team=survivor, placement=3)
        TournamentTeamMember.objects.create(tournament_team=survivor, user=self.owner, event=self.event)
        StageCompetitor.objects.create(stage=self.stage, tournament_team=survivor)

        # LOSER (same event+team): 1 stat in a NEW match m3 (repointable) + 1 stat in m1 that COLLIDES
        # with the survivor's m1 stat (must be dropped, not duplicated) + its own stage-pool row that
        # collides with the survivor's (dropped).
        loser = TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        m3 = Match.objects.create(group=self.group, match_number=3, match_map="purgatory")
        TournamentTeamMatchStats.objects.create(match=m3, tournament_team=loser, placement=2)
        TournamentTeamMatchStats.objects.create(match=m1, tournament_team=loser, placement=5)  # collides
        StageCompetitor.objects.create(stage=self.stage, tournament_team=loser)  # collides if uniq

        # A RR group the LOSER sits in (M2M remap target).
        rr = RoundRobinGroup.objects.create(stage=self.stage, label="RR-A")
        rr.teams.add(loser)

        survivor_id = survivor.tournament_team_id
        loser_id = loser.tournament_team_id

        call_command("dedupe_tournament_teams", "--apply")

        # Loser gone, survivor stays.
        self.assertFalse(TournamentTeam.objects.filter(pk=loser_id).exists())
        self.assertTrue(TournamentTeam.objects.filter(pk=survivor_id).exists())

        # m3 stat repointed to the survivor; m1 collision dropped (survivor keeps ITS m1 row, not two).
        self.assertTrue(TournamentTeamMatchStats.objects.filter(
            match=m3, tournament_team_id=survivor_id).exists())
        self.assertEqual(TournamentTeamMatchStats.objects.filter(match=m1).count(), 1)
        self.assertEqual(TournamentTeamMatchStats.objects.filter(match=m1,
                         tournament_team_id=survivor_id).first().placement, 1)  # survivor's original kept
        # No stats left dangling on the deleted loser id.
        self.assertEqual(TournamentTeamMatchStats.objects.filter(tournament_team_id=loser_id).count(), 0)

        # RR group now holds the survivor, not the loser.
        self.assertTrue(rr.teams.filter(tournament_team_id=survivor_id).exists())
        self.assertFalse(rr.teams.filter(tournament_team_id=loser_id).exists())

    def test_no_dupes_is_a_noop(self):
        TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        call_command("dedupe_tournament_teams", "--apply")  # must not raise
        self.assertEqual(TournamentTeam.objects.filter(event=self.event, team=self.team).count(), 1)
