"""
test_group_capacity.py - "Teams per group" on a stage (owner 2026-09-12).

What must stay true, and the test that holds it:
    - no size set: the split is the round robin it always was                (PlanTests)
    - a size caps every group; the pool is spread least-loaded-first over
      rows already in the groups; a pool that does not fit is refused with
      the numbers, or truncated only for the best-effort callers             (PlanTests)
    - the team seeder refuses over capacity (and a clear_existing run leaves
      the groups untouched), fills evenly under it                            (SeederTests)
    - autoseed places what fits and stops; the explicit reseed refuses        (SeederTests)
    - the field round-trips through create_event / edit_event / the readers   (RoundTripTests)

Run: python manage.py test afc_tournament_and_scrims.test_group_capacity
"""
import datetime
import json
from datetime import date, timedelta

from unittest.mock import patch

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User, UserProfile
from afc_team.models import Team, TeamMembers

from .group_capacity import GroupCapacityError, check_fits, plan_placements, stage_capacity
from .models import Event, StageCompetitor, StageGroupCompetitor, StageGroups, Stages, TournamentTeam
from .seeding_management import _distribute_into_groups


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(
        user=u, token=f"tok_{username}"[:32],
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    )
    return u, tok.token


def _event(creator):
    return Event.objects.create(
        event_name="Size Cup", competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=16, event_mode="single",
        start_date=date.today() + timedelta(days=3), end_date=date.today() + timedelta(days=4),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=2),
        number_of_stages=1, creator=creator, is_public=True, is_draft=False,
    )


def _stage(event, groups=2, size=None):
    today = date.today()
    st = Stages.objects.create(
        event=event, stage_name="Group Stage", start_date=today, end_date=today,
        number_of_groups=groups, competitors_per_group=size, stage_format="br - normal",
        teams_qualifying_from_stage=1,
    )
    for i in range(groups):
        StageGroups.objects.create(
            stage=st, group_name=f"Group {chr(65 + i)}", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=1, match_count=1,
        )
    return st


def _post(token, path, body=None):
    return Client().post(path, json.dumps(body or {}), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


class Fixture(TestCase):
    """An event, a 2-group stage, 8 registered clubs in the stage pool."""

    n_teams = 8
    size = None

    def setUp(self):
        # The Discord role sync behind every seed is a Celery task; patched here (a patcher in
        # setUp reaches every subclass) so a test never publishes to the broker, which on the
        # VPS rig is the PRODUCTION worker's Redis.
        patcher = patch("afc_tournament_and_scrims.views.assign_group_roles_from_db_task.delay")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin, self.admin_tok = _user("gc_admin", role="admin")
        self.event = _event(self.admin)
        self.stage = _stage(self.event, groups=2, size=self.size)
        self.groups = list(StageGroups.objects.filter(stage=self.stage).order_by("group_id"))
        self.tts = []
        for i in range(self.n_teams):
            cap, _ = _user(f"gc_cap{i}")
            team = Team.objects.create(team_name=f"Club {i}", team_owner=cap, team_creator=cap)
            TeamMembers.objects.create(team=team, member=cap, management_role="team_captain")
            tt = TournamentTeam.objects.create(event=self.event, team=team, registered_by=cap)
            StageCompetitor.objects.create(stage=self.stage, tournament_team=tt)
            self.tts.append(tt)

    def counts(self):
        return [
            StageGroupCompetitor.objects.filter(stage_group=g).count() for g in self.groups
        ]


class PlanTests(Fixture):
    def test_no_size_is_round_robin(self):
        plan = plan_placements(self.stage, self.groups, 5)
        self.assertEqual([g.group_name for g in plan], ["Group A", "Group B", "Group A", "Group B", "Group A"])
        self.assertIsNone(stage_capacity(self.stage, self.groups))

    def test_size_caps_and_fills_least_loaded_first(self):
        self.stage.competitors_per_group = 4
        # Group B already holds two rows: the next four land A, A (now level), then A, B
        StageGroupCompetitor.objects.create(stage_group=self.groups[1], tournament_team=self.tts[0])
        StageGroupCompetitor.objects.create(stage_group=self.groups[1], tournament_team=self.tts[1])
        plan = plan_placements(self.stage, self.groups, 4)
        self.assertEqual([g.group_name for g in plan], ["Group A", "Group A", "Group A", "Group B"])
        self.assertEqual(stage_capacity(self.stage, self.groups), 8)

    def test_over_capacity_is_refused_with_the_numbers(self):
        self.stage.competitors_per_group = 3
        with self.assertRaises(GroupCapacityError) as ctx:
            plan_placements(self.stage, self.groups, 8)
        self.assertIn("8 teams for 2 groups of 3: room for 6", str(ctx.exception))
        # the lenient form places what fits and stops
        plan = plan_placements(self.stage, self.groups, 8, strict=False)
        self.assertEqual(len(plan), 6)
        self.assertEqual(sorted(g.group_name for g in plan), ["Group A"] * 3 + ["Group B"] * 3)

    def test_check_fits_counts_existing_rows(self):
        self.stage.competitors_per_group = 2
        StageGroupCompetitor.objects.create(stage_group=self.groups[0], tournament_team=self.tts[0])
        check_fits(self.stage, self.groups, 3)  # room: A 1 + B 2
        with self.assertRaises(GroupCapacityError):
            check_fits(self.stage, self.groups, 4)


class SeederTests(Fixture):
    size = 3

    def test_team_seeder_refuses_over_capacity_and_touches_nothing(self):
        # a stale row, which clear_existing would wipe: it must survive the refusal (rollback)
        StageGroupCompetitor.objects.create(stage_group=self.groups[0], tournament_team=self.tts[0])
        resp = _post(self.admin_tok, "/events/seed-stage-competitors-to-groups-team/",
                     {"stage_id": self.stage.stage_id, "clear_existing": True})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("8 teams for 2 groups of 3: room for 6", resp.json()["message"])
        self.assertEqual(self.counts(), [1, 0])

    def test_team_seeder_fills_evenly_under_capacity(self):
        Stages.objects.filter(pk=self.stage.pk).update(competitors_per_group=4)
        resp = _post(self.admin_tok, "/events/seed-stage-competitors-to-groups-team/",
                     {"stage_id": self.stage.stage_id, "clear_existing": True})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.counts(), [4, 4])

    def test_autoseed_places_what_fits(self):
        placed = _distribute_into_groups(self.stage, shuffle=False, only_ungrouped=True)
        self.assertEqual(placed, 6)
        self.assertEqual(self.counts(), [3, 3])
        # the two left over are still in the stage pool, ungrouped
        grouped = StageGroupCompetitor.objects.filter(stage_group__stage=self.stage).count()
        self.assertEqual(StageCompetitor.objects.filter(stage=self.stage).count() - grouped, 2)

    def test_explicit_reseed_refuses(self):
        resp = _post(self.admin_tok, "/events/seeding/reseed/",
                     {"event_id": self.event.event_id, "stage_id": self.stage.stage_id, "clear_existing": True})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("room for 6", resp.json()["message"])
        self.assertEqual(self.counts(), [0, 0])


class RoundTripTests(Fixture):
    def _edit(self, stage_payload):
        return _post(self.admin_tok, "/events/edit-event/",
                     {"event_id": self.event.event_id, "stages": [stage_payload]})

    def test_edit_event_writes_and_the_readers_echo(self):
        d = str(date.today())
        stage_payload = {
            "stage_id": self.stage.stage_id, "stage_name": "Group Stage",
            "start_date": d, "end_date": d,
            "number_of_groups": 2, "competitors_per_group": 4, "stage_format": "br - normal",
            "teams_qualifying_from_stage": 1, "groups": [],
        }
        resp = self._edit(stage_payload)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.competitors_per_group, 4)

        admin_view = _post(self.admin_tok, "/events/get-event-details-for-admin/", {"slug": self.event.slug})
        self.assertEqual(admin_view.status_code, 200, admin_view.content)
        self.assertEqual(admin_view.json()["stages"][0]["competitors_per_group"], 4)
        public = Client().post("/events/get-event-details-not-logged-in/", json.dumps({"slug": self.event.slug}),
                               content_type="application/json")
        self.assertEqual(public.status_code, 200, public.content)
        self.assertEqual(public.json()["event_details"]["stages"][0]["competitors_per_group"], 4)

        # empty clears it
        stage_payload["competitors_per_group"] = ""
        resp = self._edit(stage_payload)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.stage.refresh_from_db()
        self.assertIsNone(self.stage.competitors_per_group)
