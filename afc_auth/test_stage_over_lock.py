"""
Qualification-aware identity / roster lock — STAGE-OVER release (owner 2026-06-30).

Owner rule, in two messages:
  1. "If a stage or group is over, even though the event is not over, it should count as completed for
      those teams or players and so they should be able to edit team roster or profile info."
  2. "If the results is now recalculated and a team that did not qualify now qualifies, then their roster
      and all should lock back again and the other reopens."

The identity lock (afc_auth._has_active_event_registration, gates editing in-game name / Free Fire UID) and
the leave-team gate (afc_team._member_in_active_event_roster) used to hold for the WHOLE life of a
started, not-completed event. Now they follow LIVE stage qualification: a competitor is locked only while
it holds an ACTIVE StageCompetitor row in a NOT-completed stage (upcoming / ongoing / paused). An advancing
team keeps such a row in the next stage (stays locked); an eliminated team has none (releases); a later
recalc that flips qualification moves those rows, so a newly-qualified team locks again and the team it
displaced reopens — no extra bookkeeping.

These TestCase rows all roll back, so nothing leaks into MySQL. The helpers are pure (no request), so we
call them directly and assert their boolean.
"""
import datetime

from django.test import TestCase

from afc_auth.views import _has_active_event_registration, _competitor_in_active_stage
from afc_team.views import _member_in_active_event_roster
from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    RegisteredCompetitors,
    Stages,
    StageCompetitor,
    TournamentTeam,
    TournamentTeamMember,
)


def _mk_event(name, **over):
    """A STARTED, not-completed, registration-CLOSED event — so the base identity lock holds and the
    ONLY thing that decides locked/unlocked is the stage-over release under test."""
    today = datetime.date.today()
    defaults = dict(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=today - datetime.timedelta(days=1),          # started
        end_date=today + datetime.timedelta(days=5),            # not past end
        registration_open_date=today - datetime.timedelta(days=3),
        registration_end_date=today - datetime.timedelta(days=1),  # registration CLOSED
        prizepool="0", event_rules="r", event_status="ongoing",
        registration_link="https://example.com/r", number_of_stages=2,
        is_draft=False,
    )
    defaults.update(over)
    return Event.objects.create(**defaults)


def _mk_stage(event, name, status, order):
    return Stages.objects.create(
        event=event, stage_name=name, start_date=event.start_date, end_date=event.end_date,
        number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        stage_order=order, stage_status=status,
    )


class StageOverIdentityLockTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(
            username="locktest_player", email="locktest_player@example.com",
            full_name="Lock Test", role="player", password="x",
        )
        self.team = Team.objects.create(
            team_name="LockTeam", team_tag="LCK", join_settings="open",
            team_creator=self.user, team_owner=self.user, country="NG",
        )
        self.event = _mk_event("Stage Over Cup")
        # The user is on the team's roster for this event.
        self.tt = TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        self.ttm = TournamentTeamMember.objects.create(
            tournament_team=self.tt, user=self.user, event=self.event, status="active",
        )

    # ── TEAM PATH ───────────────────────────────────────────────────────────────────────────────
    def test_active_stage_locks(self):
        """In an ongoing stage with an active row -> LOCKED."""
        s = _mk_stage(self.event, "Group", "ongoing", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertTrue(_has_active_event_registration(self.user))

    def test_eliminated_unlocks(self):
        """Only stage is COMPLETED (team did not advance, no later-stage row) -> UNLOCKED."""
        s = _mk_stage(self.event, "Group", "completed", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertFalse(_has_active_event_registration(self.user))

    def test_advanced_relocks(self):
        """Completed group + an active row in the upcoming next stage (advanced) -> LOCKED."""
        s1 = _mk_stage(self.event, "Group", "completed", 1)
        s2 = _mk_stage(self.event, "Finals", "upcoming", 2)
        StageCompetitor.objects.create(stage=s1, tournament_team=self.tt, status="active")
        StageCompetitor.objects.create(stage=s2, tournament_team=self.tt, status="active")
        self.assertTrue(_has_active_event_registration(self.user))

    def test_paused_stage_locks(self):
        """A PAUSED stage is in progress, not over -> LOCKED."""
        s = _mk_stage(self.event, "Group", "paused", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertTrue(_has_active_event_registration(self.user))

    def test_disqualified_stagecompetitor_unlocks(self):
        """An ongoing stage but the team's StageCompetitor is disqualified (not active) -> UNLOCKED."""
        s = _mk_stage(self.event, "Group", "ongoing", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="disqualified")
        self.assertFalse(_has_active_event_registration(self.user))

    def test_no_stage_data_safe_default_locks(self):
        """No StageCompetitor rows at all (stages unseeded / data gap) -> SAFE DEFAULT keeps LOCKED."""
        _mk_stage(self.event, "Group", "ongoing", 1)  # stage exists but no competitor row for this team
        self.assertTrue(_has_active_event_registration(self.user))

    def test_recalc_requalify_relocks(self):
        """Eliminated (only completed-stage row) -> unlocked; a recalc then writes an active row in the
        upcoming next stage (now qualifies) -> LOCKED again. The displaced team would lose its row and
        reopen, which is the mirror assertion in test_recalc_displaced_reopens."""
        s1 = _mk_stage(self.event, "Group", "completed", 1)
        s2 = _mk_stage(self.event, "Finals", "upcoming", 2)
        StageCompetitor.objects.create(stage=s1, tournament_team=self.tt, status="active")
        self.assertFalse(_has_active_event_registration(self.user))  # eliminated
        StageCompetitor.objects.create(stage=s2, tournament_team=self.tt, status="active")  # recalc requalifies
        self.assertTrue(_has_active_event_registration(self.user))   # locked back again

    def test_recalc_displaced_reopens(self):
        """A team that WAS in the finals loses its finals row on recalc (displaced) -> reopens."""
        s1 = _mk_stage(self.event, "Group", "completed", 1)
        s2 = _mk_stage(self.event, "Finals", "upcoming", 2)
        sc1 = StageCompetitor.objects.create(stage=s1, tournament_team=self.tt, status="active")
        sc2 = StageCompetitor.objects.create(stage=s2, tournament_team=self.tt, status="active")
        self.assertTrue(_has_active_event_registration(self.user))   # in the finals -> locked
        sc2.delete()  # recalc removes them from the finals
        self.assertFalse(_has_active_event_registration(self.user))  # reopened

    def test_completed_event_unlocks_regardless(self):
        """Even with an active stage row, a COMPLETED event releases (pre-existing behavior preserved)."""
        self.event.event_status = "completed"
        self.event.save(update_fields=["event_status"])
        s = _mk_stage(self.event, "Group", "ongoing", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertFalse(_has_active_event_registration(self.user))

    def test_not_started_event_never_locks(self):
        """An event that has not started never locks, even with an active stage row."""
        self.event.event_status = "upcoming"
        self.event.start_date = datetime.date.today() + datetime.timedelta(days=3)
        self.event.save(update_fields=["event_status", "start_date"])
        s = _mk_stage(self.event, "Group", "upcoming", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertFalse(_has_active_event_registration(self.user))

    # ── SOLO PATH ───────────────────────────────────────────────────────────────────────────────
    def test_solo_active_locks_and_eliminated_unlocks(self):
        solo = User.objects.create(
            username="locktest_solo", email="locktest_solo@example.com",
            full_name="Solo", role="player", password="x",
        )
        rc = RegisteredCompetitors.objects.create(event=self.event, user=solo, status="registered")
        s = _mk_stage(self.event, "Solo Group", "ongoing", 1)
        sc = StageCompetitor.objects.create(stage=s, player=rc, status="active")
        self.assertTrue(_has_active_event_registration(solo))   # active -> locked
        s.stage_status = "completed"
        s.save(update_fields=["stage_status"])
        self.assertFalse(_has_active_event_registration(solo))  # stage over -> unlocked

    # ── HELPER DIRECTLY ─────────────────────────────────────────────────────────────────────────
    def test_competitor_in_active_stage_safe_default(self):
        """No rows -> True (safe default). Active-in-ongoing -> True. Only-completed -> False."""
        self.assertTrue(_competitor_in_active_stage(self.event, self.tt, None))  # no rows -> safe default
        s = _mk_stage(self.event, "Group", "ongoing", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertTrue(_competitor_in_active_stage(self.event, self.tt, None))
        s.stage_status = "completed"
        s.save(update_fields=["stage_status"])
        self.assertFalse(_competitor_in_active_stage(self.event, self.tt, None))


class StageOverLeaveTeamGateTests(TestCase):
    """afc_team._member_in_active_event_roster — the leave-team / remove-member gate must release on
    stage-over too (an eliminated team's player can no longer be fielded -> removable)."""

    def setUp(self):
        self.owner = User.objects.create(
            username="gate_owner", email="gate_owner@example.com", full_name="Owner",
            role="player", password="x",
        )
        self.member = User.objects.create(
            username="gate_member", email="gate_member@example.com", full_name="Member",
            role="player", password="x",
        )
        self.team = Team.objects.create(
            team_name="GateTeam", team_tag="GAT", join_settings="open",
            team_creator=self.owner, team_owner=self.owner, country="NG",
        )
        self.event = _mk_event("Gate Cup")
        self.tt = TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        TournamentTeamMember.objects.create(
            tournament_team=self.tt, user=self.member, event=self.event, status="active",
        )

    def test_active_stage_blocks_removal(self):
        s = _mk_stage(self.event, "Group", "ongoing", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertTrue(_member_in_active_event_roster(self.team, self.member.user_id))

    def test_stage_over_allows_removal(self):
        s = _mk_stage(self.event, "Group", "completed", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertFalse(_member_in_active_event_roster(self.team, self.member.user_id))

    def test_no_stage_data_safe_default_blocks(self):
        _mk_stage(self.event, "Group", "ongoing", 1)  # no competitor row for this team
        self.assertTrue(_member_in_active_event_roster(self.team, self.member.user_id))

    def test_cancelled_team_not_blocking(self):
        """A withdrawn TournamentTeam is not a live roster -> not blocking (pre-existing exclusion)."""
        self.tt.status = "withdrawn"
        self.tt.save(update_fields=["status"])
        s = _mk_stage(self.event, "Group", "ongoing", 1)
        StageCompetitor.objects.create(stage=s, tournament_team=self.tt, status="active")
        self.assertFalse(_member_in_active_event_roster(self.team, self.member.user_id))
