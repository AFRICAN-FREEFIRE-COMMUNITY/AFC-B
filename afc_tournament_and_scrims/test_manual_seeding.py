"""
afc_tournament_and_scrims.test_manual_seeding — manual seed / unseed endpoints (owner 2026-07-06).

Covers seeding_management's targeted single-competitor removal + the solo-add siblings:
  • remove_competitor_from_group / remove_competitor_from_stage (team + solo),
  • the DATA-SAFETY results guard (400, nothing deleted, real stats preserved),
  • idempotency (removing something not present -> 200 removed=0),
  • add_solo_players_to_group / add_solo_players_to_stage,
  • the organizer auth gate (owning organizer 200, non-owner 403).

Run:
    ./.venv/Scripts/python.exe manage.py test afc_tournament_and_scrims.test_manual_seeding
"""
import datetime
import uuid

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from afc_auth.models import SessionToken
from afc_team.models import Team
from afc_organizers.models import Organization, OrganizationMember

from .models import (
    Event, Stages, StageGroups, StageCompetitor, StageGroupCompetitor,
    RegisteredCompetitors, TournamentTeam, Match,
    TournamentTeamMatchStats, SoloPlayerMatchStats,
)

User = get_user_model()
TODAY = datetime.date.today()


# ── fixture builders (each value unique so tests never collide on the DB unique constraints) ────────
def _uniq(prefix="x"):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def make_user(role="player"):
    return User.objects.create_user(
        username=_uniq("user"),
        email=f"{_uniq('e')}@test.local",
        password="pw-strong-9273",
        role=role,
    )


def make_token(user):
    tok = _uniq("tok")
    SessionToken.objects.create(
        user=user, token=tok, expires_at=timezone.now() + datetime.timedelta(hours=1),
    )
    return tok


def make_event(participant_type="squad", organization=None, creator=None):
    return Event.objects.create(
        slug=_uniq("event"),
        competition_type="tournament",
        participant_type=participant_type,
        event_type="internal",
        max_teams_or_players=16,
        event_name="Test Event",
        event_mode="virtual",
        start_date=TODAY, end_date=TODAY,
        registration_open_date=TODAY, registration_end_date=TODAY,
        prizepool="$100",
        prize_distribution={},
        event_rules="Rules",
        event_status="ongoing",
        registration_link="https://example.com/reg",
        number_of_stages=1,
        organization=organization,
        creator=creator,
    )


def make_stage(event, stage_format="br - normal"):
    return Stages.objects.create(
        event=event, stage_name="Stage 1", start_date=TODAY, end_date=TODAY,
        number_of_groups=1, stage_format=stage_format, teams_qualifying_from_stage=8,
    )


def make_group(stage, name="Group A"):
    return StageGroups.objects.create(
        stage=stage, group_name=name, playing_date=TODAY, playing_time=datetime.time(12, 0),
        teams_qualifying=8, match_count=1, match_maps=[],
    )


def make_team_seeded(event, stage, group):
    """A TournamentTeam seeded into BOTH the stage pool (StageCompetitor) and the group."""
    owner = make_user()
    team = Team.objects.create(
        team_name=_uniq("Team"), join_settings="open", team_creator=owner, team_owner=owner,
    )
    tt = TournamentTeam.objects.create(event=event, team=team, status="active")
    StageCompetitor.objects.create(stage=stage, tournament_team=tt)
    StageGroupCompetitor.objects.create(stage_group=group, tournament_team=tt)
    return tt


def make_solo_seeded(event, stage, group):
    """A RegisteredCompetitors (solo player) seeded into the stage pool + the group."""
    player = make_user()
    reg = RegisteredCompetitors.objects.create(event=event, user=player, status="registered")
    StageCompetitor.objects.create(stage=stage, player=reg)
    StageGroupCompetitor.objects.create(stage_group=group, player=reg)
    return reg


# ── TEAM remove ─────────────────────────────────────────────────────────────────────────────────────
class ManualSeedingTeamRemoveTests(APITestCase):
    def setUp(self):
        self.admin = make_user(role="admin")   # AFC event admin passes _seeding_gate on a native event
        self.token = make_token(self.admin)
        self.event = make_event("squad")        # organization=None -> admin-only
        self.stage = make_stage(self.event)
        self.group = make_group(self.stage)
        self.tt = make_team_seeded(self.event, self.stage, self.group)

    def _post(self, name, body):
        return self.client.post(
            reverse(name), body, format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_remove_team_from_group_leaves_stage_pool(self):
        res = self._post("seeding_remove_from_group", {
            "group_id": self.group.group_id, "tournament_team_id": self.tt.tournament_team_id,
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["removed"], 1)
        # Gone from the group...
        self.assertFalse(StageGroupCompetitor.objects.filter(
            stage_group=self.group, tournament_team=self.tt).exists())
        # ...but still in the stage pool (group-only removal).
        self.assertTrue(StageCompetitor.objects.filter(
            stage=self.stage, tournament_team=self.tt).exists())

    def test_remove_team_from_group_accepts_underlying_team_id(self):
        # add_teams_to_group / the FE AddTeamsModal key on the underlying Team FK — accept it too.
        res = self._post("seeding_remove_from_group", {
            "group_id": self.group.group_id, "team_id": self.tt.team_id,
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["removed"], 1)

    def test_remove_team_from_stage_strips_stage_and_groups(self):
        res = self._post("seeding_remove_from_stage", {
            "stage_id": self.stage.stage_id, "tournament_team_id": self.tt.tournament_team_id,
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(StageGroupCompetitor.objects.filter(
            stage_group__stage=self.stage, tournament_team=self.tt).exists())
        self.assertFalse(StageCompetitor.objects.filter(
            stage=self.stage, tournament_team=self.tt).exists())
        # Event registration is left intact (only the stage seeding is undone).
        self.assertTrue(TournamentTeam.objects.filter(pk=self.tt.pk).exists())

    def test_results_guard_blocks_group_remove_and_preserves_everything(self):
        match = Match.objects.create(group=self.group, match_number=1, match_map="bermuda")
        TournamentTeamMatchStats.objects.create(match=match, tournament_team=self.tt, placement=1)
        res = self._post("seeding_remove_from_group", {
            "group_id": self.group.group_id, "tournament_team_id": self.tt.tournament_team_id,
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("results", res.data["message"].lower())
        # Nothing deleted: seeding rows AND the real match stats survive.
        self.assertTrue(StageGroupCompetitor.objects.filter(
            stage_group=self.group, tournament_team=self.tt).exists())
        self.assertTrue(StageCompetitor.objects.filter(
            stage=self.stage, tournament_team=self.tt).exists())
        self.assertTrue(TournamentTeamMatchStats.objects.filter(
            match=match, tournament_team=self.tt).exists())

    def test_results_guard_blocks_stage_remove(self):
        match = Match.objects.create(group=self.group, match_number=1, match_map="bermuda")
        TournamentTeamMatchStats.objects.create(match=match, tournament_team=self.tt, placement=1)
        res = self._post("seeding_remove_from_stage", {
            "stage_id": self.stage.stage_id, "tournament_team_id": self.tt.tournament_team_id,
        })
        self.assertEqual(res.status_code, 400)
        self.assertTrue(StageCompetitor.objects.filter(
            stage=self.stage, tournament_team=self.tt).exists())

    def test_remove_is_idempotent(self):
        body = {"group_id": self.group.group_id, "tournament_team_id": self.tt.tournament_team_id}
        first = self._post("seeding_remove_from_group", body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["removed"], 1)
        again = self._post("seeding_remove_from_group", body)   # nothing left to remove
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["removed"], 0)


# ── SOLO remove + add ────────────────────────────────────────────────────────────────────────────────
class ManualSeedingSoloTests(APITestCase):
    def setUp(self):
        self.admin = make_user(role="admin")
        self.token = make_token(self.admin)
        self.event = make_event("solo")
        self.stage = make_stage(self.event)
        self.group = make_group(self.stage)
        self.reg = make_solo_seeded(self.event, self.stage, self.group)

    def _post(self, name, body):
        return self.client.post(
            reverse(name), body, format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_remove_solo_from_group_by_competitor_id(self):
        res = self._post("seeding_remove_from_group", {
            "group_id": self.group.group_id, "competitor_id": self.reg.id,
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["removed"], 1)
        self.assertFalse(StageGroupCompetitor.objects.filter(
            stage_group=self.group, player=self.reg).exists())
        self.assertTrue(StageCompetitor.objects.filter(stage=self.stage, player=self.reg).exists())

    def test_remove_solo_from_stage_by_user_id(self):
        # The group-roster payload only exposes user_id per solo row -> the endpoint resolves it.
        res = self._post("seeding_remove_from_stage", {
            "stage_id": self.stage.stage_id, "user_id": self.reg.user_id,
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(StageCompetitor.objects.filter(stage=self.stage, player=self.reg).exists())
        self.assertFalse(StageGroupCompetitor.objects.filter(
            stage_group__stage=self.stage, player=self.reg).exists())
        # Registration intact.
        self.assertTrue(RegisteredCompetitors.objects.filter(pk=self.reg.pk).exists())

    def test_solo_results_guard(self):
        match = Match.objects.create(group=self.group, match_number=1, match_map="bermuda")
        SoloPlayerMatchStats.objects.create(match=match, competitor=self.reg, placement=1)
        res = self._post("seeding_remove_from_group", {
            "group_id": self.group.group_id, "competitor_id": self.reg.id,
        })
        self.assertEqual(res.status_code, 400)
        self.assertTrue(StageGroupCompetitor.objects.filter(
            stage_group=self.group, player=self.reg).exists())
        self.assertTrue(SoloPlayerMatchStats.objects.filter(
            match=match, competitor=self.reg).exists())

    def test_add_solo_players_to_group(self):
        player = make_user()
        reg2 = RegisteredCompetitors.objects.create(
            event=self.event, user=player, status="registered")
        res = self._post("seeding_add_solo_to_group", {
            "group_id": self.group.group_id, "competitor_ids": [reg2.id],
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn(reg2.id, res.data["added_competitor_ids"])
        self.assertTrue(StageGroupCompetitor.objects.filter(
            stage_group=self.group, player=reg2).exists())
        # Pool kept consistent (undo/reseed work off StageCompetitor).
        self.assertTrue(StageCompetitor.objects.filter(stage=self.stage, player=reg2).exists())

    def test_add_solo_players_to_stage(self):
        player = make_user()
        reg2 = RegisteredCompetitors.objects.create(
            event=self.event, user=player, status="registered")
        res = self._post("seeding_add_solo_to_stage", {
            "stage_id": self.stage.stage_id, "competitor_ids": [reg2.id],
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(StageCompetitor.objects.filter(stage=self.stage, player=reg2).exists())

    def test_add_solo_endpoint_rejects_team_event(self):
        team_event = make_event("squad")
        stage = make_stage(team_event)
        group = make_group(stage)
        res = self._post("seeding_add_solo_to_group", {
            "group_id": group.group_id, "competitor_ids": [1],
        })
        self.assertEqual(res.status_code, 400)


# ── AUTH gate (organizer parity) ──────────────────────────────────────────────────────────────────
class ManualSeedingAuthTests(APITestCase):
    def setUp(self):
        self.org = Organization.objects.create(slug=_uniq("org"), name="Org")
        self.owner = make_user()  # plain player role, but the org OWNER (implicitly can do everything)
        OrganizationMember.objects.create(
            organization=self.org, user=self.owner, role="owner", status="active")
        self.event = make_event("squad", organization=self.org, creator=self.owner)
        self.stage = make_stage(self.event)
        self.group = make_group(self.stage)
        self.tt = make_team_seeded(self.event, self.stage, self.group)

    def test_owning_organizer_can_remove(self):
        token = make_token(self.owner)
        res = self.client.post(
            reverse("seeding_remove_from_group"),
            {"group_id": self.group.group_id, "tournament_team_id": self.tt.tournament_team_id},
            format="json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["removed"], 1)

    def test_non_owner_forbidden_and_nothing_removed(self):
        outsider = make_user()  # not a member of the org, not an AFC admin, not the creator
        token = make_token(outsider)
        res = self.client.post(
            reverse("seeding_remove_from_group"),
            {"group_id": self.group.group_id, "tournament_team_id": self.tt.tournament_team_id},
            format="json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertTrue(StageGroupCompetitor.objects.filter(
            stage_group=self.group, tournament_team=self.tt).exists())
