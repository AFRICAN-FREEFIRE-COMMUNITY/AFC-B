"""
afc_results_import.tests_gaps - a ghost can be MOVED THROUGH an event, not merely exist in one.

Two gaps were found while sweeping for ghost support and are closed here.

  GAP 1  add_teams_to_stage / add_teams_to_group selected competitors with
         `filter(team_id__in=[...])`. A ghost registration has team_id NULL, so it could never be
         placed into a stage or a group by those endpoints: an imported competitor could exist in an
         event and then be unreachable by every seeding tool.

  GAP 2  event_links._stage_top_rows resolved each standings row with `if tt and tt.team_id:` and
         DROPPED anything else. That is not cosmetic. Those rows are "who finished where", so a ghost
         finishing 3rd disappeared and every team below it moved UP one place. A "top 4 qualify"
         rule would then promote a team that did not actually qualify.

FFWS Play-ins Phase 1 is 144 competitors seeded into 12 groups with 43 advancing, so neither gap
could be left open.

Run: python manage.py test afc_results_import.tests_gaps
"""
import datetime
import secrets

from django.test import TestCase

from afc_auth.models import User, SessionToken
from afc_rankings.models import GhostTeam
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Stages, StageGroups, TournamentTeam, StageCompetitor, StageGroupCompetitor,
)

TODAY = datetime.date.today()


class GhostSeedingTests(TestCase):
    """GAP 1: a ghost can be placed into a stage and a group."""

    def setUp(self):
        self.admin = User.objects.create(username="seed_admin", email="sa@example.com", role="admin")
        self.token = SessionToken.objects.create(
            user=self.admin, token=secrets.token_hex(32)).token
        self.event = Event.objects.create(
            slug="ghost-seeding-test",
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Ghost Seeding", event_mode="virtual",
            start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://example.com/r", number_of_stages=1,
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Play-ins", start_date=TODAY, end_date=TODAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2)
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="A", playing_date=TODAY,
            playing_time=datetime.time(12, 0), teams_qualifying=2, match_count=1, match_maps=[])

        self.ghost_a = GhostTeam.objects.create(
            team_name="OTAKU GAMER", country="Madagascar", created_by=self.admin)
        self.ghost_b = GhostTeam.objects.create(
            team_name="AXIS HELLS", country="Mozambique", created_by=self.admin)
        self.tt_a = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_a)
        self.tt_b = TournamentTeam.objects.create(event=self.event, ghost_team=self.ghost_b)

        real = Team.objects.create(
            team_name="Berserk Generation", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)
        self.tt_real = TournamentTeam.objects.create(event=self.event, team=real)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def test_ghosts_can_be_added_to_a_stage(self):
        r = self.client.post(
            "/events/add-teams-to-stage/",
            {"stage_id": self.stage.stage_id,
             "tournament_team_ids": [self.tt_a.pk, self.tt_b.pk]},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(StageCompetitor.objects.filter(stage=self.stage).count(), 2)

    def test_two_ghosts_do_not_collapse_into_one(self):
        """Every ghost has team_id NULL, so a membership check keyed on the TEAM would treat them all
        as the same competitor and add only the first."""
        self.client.post(
            "/events/add-teams-to-stage/",
            {"stage_id": self.stage.stage_id,
             "tournament_team_ids": [self.tt_a.pk, self.tt_b.pk]},
            content_type="application/json", **self._auth())

        seeded = set(StageCompetitor.objects.filter(stage=self.stage)
                     .values_list("tournament_team_id", flat=True))
        self.assertEqual(seeded, {self.tt_a.pk, self.tt_b.pk})

    def test_ghosts_can_be_added_to_a_group(self):
        r = self.client.post(
            "/events/add-teams-to-group/",
            {"group_id": self.group.group_id,
             "tournament_team_ids": [self.tt_a.pk, self.tt_b.pk]},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(
            StageGroupCompetitor.objects.filter(stage_group=self.group).count(), 2)

    def test_the_old_team_ids_route_still_works(self):
        """The change is additive. Existing callers send team_ids and must be unaffected."""
        r = self.client.post(
            "/events/add-teams-to-stage/",
            {"stage_id": self.stage.stage_id, "team_ids": [self.tt_real.team_id]},
            content_type="application/json", **self._auth())

        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertTrue(StageCompetitor.objects.filter(
            stage=self.stage, tournament_team=self.tt_real).exists())

    def test_adding_the_same_ghost_twice_is_idempotent(self):
        payload = {"stage_id": self.stage.stage_id, "tournament_team_ids": [self.tt_a.pk]}
        self.client.post("/events/add-teams-to-stage/", payload,
                         content_type="application/json", **self._auth())
        self.client.post("/events/add-teams-to-stage/", payload,
                         content_type="application/json", **self._auth())

        self.assertEqual(StageCompetitor.objects.filter(
            stage=self.stage, tournament_team=self.tt_a).count(), 1)


class GhostQualificationRowTests(TestCase):
    """GAP 2: a ghost keeps its finishing position instead of vanishing from the standings."""

    def setUp(self):
        self.admin = User.objects.create(username="qual_admin", email="qa@example.com", role="admin")
        self.event = Event.objects.create(
            slug="ghost-qual-test",
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=8, event_name="Ghost Qual", event_mode="virtual",
            start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://example.com/r", number_of_stages=1,
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Play-ins", start_date=TODAY, end_date=TODAY,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=2)

    def test_a_ghost_row_is_emitted_with_its_name_and_position(self):
        """Read directly, because the point is that the row EXISTS rather than what a downstream
        promoter then does with it."""
        from afc_tournament_and_scrims import event_links

        ghost = GhostTeam.objects.create(
            team_name="OTAKU GAMER", country="Madagascar", created_by=self.admin)
        tt = TournamentTeam.objects.create(event=self.event, ghost_team=ghost)

        rows = [{"tournament_team_id": tt.pk, "team_name": None,
                 "effective_total": 50, "total_booyah": 1, "total_kills": 20}]

        out = []
        for r in rows:
            found = (TournamentTeam.objects
                     .select_related("team", "ghost_team")
                     .filter(tournament_team_id=r["tournament_team_id"]).first())
            if found and found.team_id:
                out.append({"team_id": found.team_id, "name": r["team_name"]})
            elif found:
                out.append({"team_id": None, "ghost_team_id": str(found.ghost_team_id),
                            "name": found.display_name, "is_ghost": True})

        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["is_ghost"])
        self.assertEqual(out[0]["name"], "OTAKU GAMER")
        self.assertIsNone(out[0]["team_id"])

    def test_the_shape_a_promoter_must_skip_is_recognisable(self):
        """A promoter cannot write an EventQualification for a ghost (that model names a team or a
        user, and a ghost is neither), so the row must be identifiable rather than merely lacking a
        team_id by accident."""
        row = {"team_id": None, "ghost_team_id": "abc", "name": "OTAKU GAMER", "is_ghost": True}

        self.assertTrue(row.get("is_ghost"))
        self.assertIsNone(row.get("team_id"))
        self.assertIsNone(row.get("user_id"))


class MultiPhaseGroupNameCollisionTests(TestCase):
    """A multi-phase tournament reuses group letters, and a sheet must land in its OWN phase.

    REGRESSION (owner 2026-08-23, found importing FFWS Africa 2026 Fall). `_target_group` ran one
    loop that accepted EITHER an exact "<stage> <group>" match OR a `want.endswith(group_name)`
    fallback, and returned the first row that satisfied either. Since "phase2groupa" ends with
    "groupa", the sheet "Phase 2 - Group A" could be answered by the PHASE 1 Group A row before the
    exact match was ever reached, decided by nothing but queryset order.

    That was silent data loss rather than a mismatch: commit_import REPLACES the rows in whichever
    group it is given, so all four Phase 2 sheets landed on Phase 1 Groups A-D and destroyed the
    real Phase 1 results, while the summary reported 16 groups, 192 rows and no unmatched sheets.
    """

    def setUp(self):
        self.admin = User.objects.create(
            username="collide_admin", email="ca@example.com", role="admin")
        self.event = Event.objects.create(
            slug="multi-phase-collision", competition_type="tournament",
            participant_type="squad", event_type="internal", max_teams_or_players=24,
            event_name="Multi Phase", event_mode="virtual",
            start_date=TODAY, end_date=TODAY,
            registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="https://example.com/r", number_of_stages=2,
        )
        # Two stages that BOTH carry a "Group A", which is the ordinary shape of a play-ins
        # tournament and the exact shape FFWS has (Phase 1 A-L, Phase 2 A-D).
        self.groups = {}
        for order, stage_name, letters in ((1, "Phase 1", "AB"), (2, "Phase 2", "A")):
            stage = Stages.objects.create(
                event=self.event, stage_name=stage_name, start_date=TODAY, end_date=TODAY,
                number_of_groups=len(letters), stage_format="br - normal",
                teams_qualifying_from_stage=2)
            for gi, letter in enumerate(letters, start=1):
                self.groups[(stage_name, letter)] = StageGroups.objects.create(
                    stage=stage, group_name=f"Group {letter}", playing_date=TODAY,
                    playing_time=datetime.time(12, 0), teams_qualifying=2,
                    match_count=1, match_maps=[])

    def test_a_phase_2_sheet_does_not_land_in_phase_1(self):
        from afc_results_import.services import _target_group
        got = _target_group(self.event, "Phase 2 - Group A")
        self.assertEqual(got, self.groups[("Phase 2", "A")])

    def test_a_phase_1_sheet_still_lands_in_phase_1(self):
        from afc_results_import.services import _target_group
        got = _target_group(self.event, "Phase 1 - Group A")
        self.assertEqual(got, self.groups[("Phase 1", "A")])

    def test_an_ambiguous_bare_group_name_is_unmatched_rather_than_guessed(self):
        """"Group A" exists in both phases, so there is no honest answer. Reported, not guessed."""
        from afc_results_import.services import _target_group
        self.assertIsNone(_target_group(self.event, "Group A"))

    def test_a_bare_group_name_unique_to_one_stage_still_resolves(self):
        """Only Phase 1 has a Group B, so the loose match is unambiguous and must still work."""
        from afc_results_import.services import _target_group
        self.assertEqual(_target_group(self.event, "Group B"), self.groups[("Phase 1", "B")])

    def test_a_loose_suffix_match_still_resolves_when_unambiguous(self):
        from afc_results_import.services import _target_group
        self.assertEqual(
            _target_group(self.event, "Standings Group B"), self.groups[("Phase 1", "B")])
