r"""The automatic draw, once the organizer can say WHERE and WHEN.

WHY (owner 2026-08-05). Auto-seed did one thing: at the event's start instant, draw into the entry
stage. The owner asked for the choice to be theirs, "if it should apply to specific stages or
groups and what should trigger it", while keeping execution scoped to the first stage for now.

THE PROPERTY THAT MATTERS MOST HERE IS THE ONE THAT IS EASY TO BREAK: every event created before
any of this existed must keep behaving exactly as it did. Nobody's tournament should draw at a
different moment, or into a different stage, because a field was added underneath it. Several of
the tests below exist only to hold that line.

Run: .venv\Scripts\python.exe manage.py test afc_tournament_and_scrims.tests_autoseed_config
"""
import datetime

from django.test import TestCase
from django.utils import timezone

from afc_auth.models import User
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    StageCompetitor,
    StageGroupCompetitor,
    StageGroups,
    Stages,
    TournamentTeam,
)
from afc_tournament_and_scrims.views_autoseed import (
    auto_seed_due_at,
    run_auto_seed,
    stages_to_seed,
)


class AutoSeedConfigTests(TestCase):
    def setUp(self):
        self.today = datetime.date.today()
        self.admin = User.objects.create(
            username="seed_admin", email="seed_admin@x.com", full_name="Seed Admin",
            role="admin", password="x")

        self.event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=24, event_name="Seed Cup", event_mode="virtual",
            start_date=self.today, end_date=self.today, registration_open_date=self.today,
            registration_end_date=self.today, prizepool="0", event_rules="r",
            event_status="ongoing", registration_link="https://x.com/r", number_of_stages=2,
            creator=self.admin, auto_seed_on_start=True,
            event_start_time=datetime.time(18, 0),
            registration_end_time=datetime.time(9, 0))

        self.stage_one = self._stage("Qualifiers", order=1, groups=2)
        self.stage_two = self._stage("Finals", order=2, groups=1)

        for i in range(6):
            team = Team.objects.create(
                team_name=f"Seed Team {i}", team_tag=f"S{i}", join_settings="open",
                team_creator=self.admin, team_owner=self.admin, country="NG")
            TournamentTeam.objects.create(
                event=self.event, team=team, registered_by=self.admin,
                status="active", is_waitlisted=False)

    def _stage(self, name, *, order, groups):
        stage = Stages.objects.create(
            event=self.event, stage_name=name, start_date=self.today, end_date=self.today,
            number_of_groups=groups, stage_format="br - normal",
            teams_qualifying_from_stage=2, stage_order=order)
        for g in range(groups):
            StageGroups.objects.create(
                stage=stage, group_name=f"{name} Group {g + 1}", playing_date=self.today,
                playing_time=datetime.time(18, 0), teams_qualifying=2, match_count=1)
        return stage

    # ── which stages ──
    def test_an_event_with_no_stage_ticked_still_seeds_the_entry_stage(self):
        """THE BACKWARD-COMPATIBILITY LINE. Every event that existed before Stages.auto_seed was
        added has it False on every stage, and auto-seed used to mean "the entry stage". An empty
        selection therefore has to keep meaning that, not "nowhere"."""
        self.assertEqual(stages_to_seed(self.event), [self.stage_one])

        run_auto_seed(self.event)

        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_one).count(), 6)
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_two).count(), 0)

    def test_ticking_a_later_stage_seeds_that_one_instead(self):
        self.stage_two.auto_seed = True
        self.stage_two.save(update_fields=["auto_seed"])

        self.assertEqual(stages_to_seed(self.event), [self.stage_two])

        run_auto_seed(self.event)

        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_one).count(), 0)
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_two).count(), 6)

    def test_two_ticked_stages_are_both_seeded_in_play_order(self):
        for stage in (self.stage_one, self.stage_two):
            stage.auto_seed = True
            stage.save(update_fields=["auto_seed"])

        self.assertEqual(stages_to_seed(self.event), [self.stage_one, self.stage_two])

        result = run_auto_seed(self.event)

        self.assertEqual(len(result["stages"]), 2)
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_one).count(), 6)
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_two).count(), 6)

    def test_the_event_level_numbers_still_describe_the_first_stage(self):
        """Every existing caller and test reads seeded / groups / stage_id off the top level, and
        one seeded stage is still the ordinary case."""
        result = run_auto_seed(self.event)

        self.assertEqual(result["stage_id"], self.stage_one.stage_id)
        self.assertEqual(result["seeded"], 6)

    # ── which groups ──
    def test_a_group_held_back_receives_nobody(self):
        """Unticking a group is how an organizer reserves one to fill by hand, for example an
        invitational group."""
        held = self.stage_one.groups.order_by("group_id").last()
        held.auto_seed_include = False
        held.save(update_fields=["auto_seed_include"])

        run_auto_seed(self.event)

        self.assertEqual(StageGroupCompetitor.objects.filter(stage_group=held).count(), 0)
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_one).count(), 6)

    def test_excluding_every_group_seeds_nobody_rather_than_erroring(self):
        """The same no-op a stage with no groups at all gets. An organizer who unticked everything
        has said "not automatically", and a 500 would be a worse answer than nothing happening."""
        self.stage_one.groups.update(auto_seed_include=False)

        result = run_auto_seed(self.event)

        self.assertEqual(result["skipped"], "no_groups")
        self.assertEqual(StageGroupCompetitor.objects.count(), 0)

    # ── when ──
    def test_the_default_trigger_is_the_event_start(self):
        due = auto_seed_due_at(self.event)

        self.assertIsNotNone(due)
        self.assertEqual(timezone.localtime(due).time().hour, 18)

    def test_registration_close_uses_the_registration_end(self):
        self.event.auto_seed_trigger = "registration_close"
        self.event.save(update_fields=["auto_seed_trigger"])

        self.assertEqual(timezone.localtime(auto_seed_due_at(self.event)).time().hour, 9)

    def test_checkin_close_uses_the_checkin_window(self):
        closes = timezone.now() + datetime.timedelta(hours=3)
        self.event.auto_seed_trigger = "checkin_close"
        self.event.checkin_enabled = True
        self.event.checkin_end = closes
        self.event.save(update_fields=["auto_seed_trigger", "checkin_enabled", "checkin_end"])

        self.assertEqual(auto_seed_due_at(self.event), closes)

    def test_checkin_close_falls_back_to_the_start_when_checkin_is_off(self):
        """A trigger that can never fire would quietly mean "never seed", and the organizer did
        ask for an automatic draw."""
        self.event.auto_seed_trigger = "checkin_close"
        self.event.checkin_enabled = False
        self.event.save(update_fields=["auto_seed_trigger", "checkin_enabled"])

        self.assertEqual(timezone.localtime(auto_seed_due_at(self.event)).time().hour, 18)

    def test_a_missing_date_gives_no_due_time_rather_than_a_guess(self):
        """Falling back to the event start here would draw at a moment the organizer did not pick.

        Set in memory and not saved: registration_end_date is NOT NULL, so this state cannot be
        stored. It is still reachable, because auto_seed_due_at is handed an Event instance and any
        caller that builds or annotates one can present a blank date.
        """
        self.event.auto_seed_trigger = "registration_close"
        self.event.save(update_fields=["auto_seed_trigger"])
        self.event.registration_end_date = None

        self.assertIsNone(auto_seed_due_at(self.event))

    # ── safety ──
    def test_a_stage_already_seeded_by_hand_is_never_clobbered(self):
        group = self.stage_one.groups.first()
        team = TournamentTeam.objects.filter(event=self.event).first()
        StageCompetitor.objects.create(
            stage=self.stage_one, tournament_team=team, status="active")
        StageGroupCompetitor.objects.create(stage_group=group, tournament_team=team)

        result = run_auto_seed(self.event)

        self.assertEqual(result["skipped"], "already_seeded")
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_one).count(), 1)

    def test_running_twice_does_not_double_seed(self):
        run_auto_seed(self.event)
        run_auto_seed(self.event)

        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group__stage=self.stage_one).count(), 6)
