"""
Letters per room (inbox #602, owner 2026-11-16): a team's in-game letter only has to be distinct
inside one ROOM, never across the whole event.

Owner: "i need to be able to do this only teams in certains groups or per stage ... The admin or
organizer decides that, also decides what stages it applies to." So:
  - a letter may repeat between teams that never share a lobby (letter_rooms.stage_rooms),
  - an assignment applies to ALL stages (TournamentTeam.assigned_letter) or to chosen stages
    (StageTeamLetter rows, which override the all-stages letter in that stage),
  - the clash check reads the FINAL state of a whole apply, so a bulk swap (X: A->B, Y: B->A) passes.

Run:
    manage.py test afc_tournament_and_scrims.tests_letter_rooms
"""
import datetime
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from afc_auth.models import SessionToken, User
from afc_team.models import Team

from .letter_rooms import LetterClash, apply_letters, effective_letters, stage_rooms
from .models import (
    Event, RoundRobinGroup, StageCompetitor, StageGroupCompetitor, StageGroups, Stages,
    StageTeamLetter, TournamentTeam,
)

TODAY = datetime.date.today()


def _u(p="x"):
    return f"{p}-{uuid.uuid4().hex[:10]}"


class LetterRoomsFixture(TestCase):
    """Event with two stages:
      S1 (br - normal): lobby G1 = t0, t1 ; lobby G2 = t2, t3.
      S2 (br - round robin): base groups A = t0, t2 ; B = t1, t3 ; one day lobby "Day 1: A+B" that
         merges A and B through source_groups and has NO StageGroupCompetitor rows (the review-focus
         case: the room must still be the union of both base groups).
    """

    def setUp(self):
        self.admin = User.objects.create_user(username=_u("adm"), email=f"{_u('e')}@t.local",
                                              password="pw-strong-9273", role="admin")
        self.event = Event.objects.create(
            slug=_u("ev"), competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_name="Letter Rooms Cup",
            event_mode="virtual", start_date=TODAY, end_date=TODAY, registration_open_date=TODAY,
            registration_end_date=TODAY, prizepool="0", prize_distribution={}, event_rules="r",
            event_status="ongoing", registration_link="https://x.co/r", number_of_stages=2,
        )
        self.tts = []
        for i in range(4):
            team = Team.objects.create(team_name=_u(f"T{i}"), join_settings="open",
                                       team_creator=self.admin, team_owner=self.admin)
            self.tts.append(TournamentTeam.objects.create(event=self.event, team=team, status="active"))

        self.s1 = Stages.objects.create(event=self.event, stage_name="Groups", start_date=TODAY,
                                        end_date=TODAY, number_of_groups=2,
                                        stage_format="br - normal", teams_qualifying_from_stage=4)
        self.g1 = self._lobby(self.s1, "Group 1", [0, 1])
        self.g2 = self._lobby(self.s1, "Group 2", [2, 3])

        self.s2 = Stages.objects.create(event=self.event, stage_name="Round Robin", start_date=TODAY,
                                        end_date=TODAY, number_of_groups=2,
                                        stage_format="br - round robin", teams_qualifying_from_stage=2)
        rr_a = RoundRobinGroup.objects.create(stage=self.s2, label="A", order=0)
        rr_b = RoundRobinGroup.objects.create(stage=self.s2, label="B", order=1)
        rr_a.teams.add(self.tts[0], self.tts[2])
        rr_b.teams.add(self.tts[1], self.tts[3])
        self.day1 = self._lobby(self.s2, "Day 1: A+B", [])
        self.day1.source_groups.add(rr_a, rr_b)

    def _lobby(self, stage, name, idxs):
        g = StageGroups.objects.create(stage=stage, group_name=name, playing_date=TODAY,
                                       playing_time=datetime.time(12, 0), teams_qualifying=1,
                                       match_count=1, match_maps=[])
        for i in idxs:
            StageCompetitor.objects.get_or_create(stage=stage, tournament_team=self.tts[i])
            StageGroupCompetitor.objects.create(stage_group=g, tournament_team=self.tts[i])
        return g

    def pk(self, i):
        return self.tts[i].pk


class StageRoomsTests(LetterRoomsFixture):
    def test_lobbies_are_rooms(self):
        rooms = {r["group_name"]: r["team_ids"] for r in stage_rooms(self.s1)}
        self.assertEqual(rooms, {"Group 1": {self.pk(0), self.pk(1)}, "Group 2": {self.pk(2), self.pk(3)}})

    def test_round_robin_day_lobby_is_the_union_of_its_base_groups(self):
        rooms = stage_rooms(self.s2)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["team_ids"], {self.pk(i) for i in range(4)})

    def test_stage_without_lobbies_is_one_room(self):
        s3 = Stages.objects.create(event=self.event, stage_name="Final", start_date=TODAY,
                                   end_date=TODAY, number_of_groups=1,
                                   stage_format="br - normal", teams_qualifying_from_stage=1)
        StageCompetitor.objects.create(stage=s3, tournament_team=self.tts[0])
        StageCompetitor.objects.create(stage=s3, tournament_team=self.tts[3])
        self.assertEqual([r["team_ids"] for r in stage_rooms(s3)], [{self.pk(0), self.pk(3)}])


class ApplyLettersTests(LetterRoomsFixture):
    def test_same_letter_in_two_different_lobbies_is_allowed(self):
        apply_letters(self.event, {self.pk(0): "A", self.pk(2): "A"}, [self.s1.stage_id])
        self.assertEqual(effective_letters(self.event, self.s1)[self.pk(2)], "A")

    def test_same_letter_inside_one_lobby_is_refused(self):
        apply_letters(self.event, {self.pk(0): "A"}, [self.s1.stage_id])
        with self.assertRaises(LetterClash) as cm:
            apply_letters(self.event, {self.pk(1): "A"}, [self.s1.stage_id])
        self.assertEqual(cm.exception.info["other_tournament_team_id"], self.pk(0))
        self.assertEqual(cm.exception.info["stage_id"], self.s1.stage_id)
        # Nothing was written by the refused apply.
        self.assertFalse(StageTeamLetter.objects.filter(tournament_team=self.tts[1]).exists())

    def test_round_robin_lobby_without_competitor_rows_still_clashes(self):
        # t0 (base group A) and t1 (base group B) meet in "Day 1: A+B".
        apply_letters(self.event, {self.pk(0): "K"}, [self.s2.stage_id])
        with self.assertRaises(LetterClash):
            apply_letters(self.event, {self.pk(1): "K"}, [self.s2.stage_id])

    def test_all_stages_letter_is_checked_in_every_stage(self):
        # t0 and t2 never share a lobby in S1, but they meet in the round robin day lobby.
        apply_letters(self.event, {self.pk(0): "Z"}, None)
        with self.assertRaises(LetterClash) as cm:
            apply_letters(self.event, {self.pk(2): "Z"}, None)
        self.assertEqual(cm.exception.info["stage_id"], self.s2.stage_id)

    def test_a_swap_in_one_apply_passes(self):
        apply_letters(self.event, {self.pk(0): "A", self.pk(1): "B"}, [self.s1.stage_id])
        apply_letters(self.event, {self.pk(0): "B", self.pk(1): "A"}, [self.s1.stage_id])
        eff = effective_letters(self.event, self.s1)
        self.assertEqual((eff[self.pk(0)], eff[self.pk(1)]), ("B", "A"))

    def test_stage_letter_overrides_and_all_stages_clears_overrides(self):
        apply_letters(self.event, {self.pk(0): "M"}, None)
        apply_letters(self.event, {self.pk(0): "Q"}, [self.s1.stage_id])
        self.assertEqual(effective_letters(self.event, self.s1)[self.pk(0)], "Q")
        self.assertEqual(effective_letters(self.event, self.s2)[self.pk(0)], "M")
        apply_letters(self.event, {self.pk(0): "R"}, None)
        self.assertFalse(StageTeamLetter.objects.filter(tournament_team=self.tts[0]).exists())
        self.assertEqual(effective_letters(self.event, self.s1)[self.pk(0)], "R")

    def test_clearing_a_stage_letter_keeps_the_all_stages_letter(self):
        apply_letters(self.event, {self.pk(0): "M"}, None)
        apply_letters(self.event, {self.pk(0): "Q"}, [self.s1.stage_id])
        apply_letters(self.event, {self.pk(0): None}, [self.s1.stage_id])
        self.assertEqual(effective_letters(self.event, self.s1)[self.pk(0)], "M")
        self.tts[0].refresh_from_db()
        self.assertEqual(self.tts[0].assigned_letter, "M")

    def test_an_old_clash_elsewhere_does_not_block_an_unrelated_team(self):
        # A pre-existing clash between t2 and t3 (written straight to the table) must not stop t0.
        StageTeamLetter.objects.create(stage=self.s1, tournament_team=self.tts[2], letter="C")
        StageTeamLetter.objects.create(stage=self.s1, tournament_team=self.tts[3], letter="C")
        apply_letters(self.event, {self.pk(0): "D"}, [self.s1.stage_id])

    def test_a_stage_from_another_event_is_refused(self):
        other = Event.objects.create(
            slug=_u("ev2"), competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_name="Other", event_mode="virtual",
            start_date=TODAY, end_date=TODAY, registration_open_date=TODAY, registration_end_date=TODAY,
            prizepool="0", prize_distribution={}, event_rules="r", event_status="ongoing",
            registration_link="https://x.co/r", number_of_stages=1,
        )
        foreign = Stages.objects.create(event=other, stage_name="X", start_date=TODAY, end_date=TODAY,
                                        number_of_groups=1, stage_format="br - normal",
                                        teams_qualifying_from_stage=1)
        with self.assertRaises(ValueError):
            apply_letters(self.event, {self.pk(0): "A"}, [foreign.stage_id])


class NoStagesTests(TestCase):
    """An event with no stages yet keeps the old rule: the whole event is one room."""

    def test_all_stages_letter_is_unique_per_event_when_there_are_no_stages(self):
        admin = User.objects.create_user(username=_u("adm"), email=f"{_u('e')}@t.local",
                                         password="pw-strong-9273", role="admin")
        event = Event.objects.create(
            slug=_u("ev"), competition_type="tournament", participant_type="squad",
            event_type="internal", max_teams_or_players=16, event_name="No Stages Cup",
            event_mode="virtual", start_date=TODAY, end_date=TODAY, registration_open_date=TODAY,
            registration_end_date=TODAY, prizepool="0", prize_distribution={}, event_rules="r",
            event_status="ongoing", registration_link="https://x.co/r", number_of_stages=1,
        )
        tts = [
            TournamentTeam.objects.create(event=event, status="active", team=Team.objects.create(
                team_name=_u("T"), join_settings="open", team_creator=admin, team_owner=admin))
            for _ in range(2)
        ]
        apply_letters(event, {tts[0].pk: "A"}, None)
        with self.assertRaises(LetterClash):
            apply_letters(event, {tts[1].pk: "A"}, None)


class LetterEndpointTests(LetterRoomsFixture):
    def setUp(self):
        super().setUp()
        SessionToken.objects.create(user=self.admin, token="letters-admin-token",
                                    expires_at=timezone.now() + datetime.timedelta(hours=1))
        self.api = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": "Bearer letters-admin-token"}

    def _assign(self, team_idx, letter, stage_ids=None):
        body = {"event_id": self.event.event_id, "team_id": self.tts[team_idx].team_id, "letter": letter}
        if stage_ids is not None:
            body["stage_ids"] = stage_ids
        return self.api.post("/events/assign-team-letter/", body, format="json", **self.auth)

    def test_assign_reuses_a_letter_across_lobbies_and_refuses_inside_one(self):
        self.assertEqual(self._assign(0, "A", [self.s1.stage_id]).status_code, 200)
        self.assertEqual(self._assign(2, "A", [self.s1.stage_id]).status_code, 200)
        r = self._assign(1, "A", [self.s1.stage_id])
        self.assertEqual((r.status_code, r.json()["code"]), (409, "letter_taken"))
        self.assertIn("Group 1", r.json()["message"])

    def test_a_guest_registration_takes_a_letter_by_its_registration_id(self):
        """A ghost (guest) registration has no AFC team, so the row sends tournament_team_id (21 Nov 2026, from the
        18 Nov open list: guest registrations could not be assigned from the row)."""
        from afc_rankings.models import GhostTeam
        ghost = GhostTeam.objects.create(team_name="Kano Kings", country="NG", created_by=self.admin)
        tt = TournamentTeam.objects.create(event=self.event, ghost_team=ghost, status="active")
        r = self.api.post("/events/assign-team-letter/", {"event_id": self.event.event_id,
                          "tournament_team_id": tt.pk, "letter": "K"}, format="json", **self.auth)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["team_id"], r.json()["team_name"], r.json()["notified"]), (None, "Kano Kings", 0))
        tt.refresh_from_db()
        self.assertEqual(tt.assigned_letter, "K")

    def test_a_registration_of_another_event_is_not_found(self):
        r = self.api.post("/events/assign-team-letter/", {"event_id": self.event.event_id,
                          "tournament_team_id": 999999, "letter": "K"}, format="json", **self.auth)
        self.assertEqual((r.status_code, r.json()["code"]), (404, "team_not_registered_event"))
        r = self.api.post("/events/assign-team-letter/", {"event_id": self.event.event_id, "letter": "K"},
                          format="json", **self.auth)
        self.assertEqual((r.status_code, r.json()["code"]), (400, "event_team_required"))

    def test_assign_without_stage_ids_is_all_stages(self):
        self.assertEqual(self._assign(0, "U").status_code, 200)
        self.tts[0].refresh_from_db()
        self.assertEqual(self.tts[0].assigned_letter, "U")

    def test_assign_with_a_foreign_stage_is_400_invalid_stage(self):
        r = self._assign(0, "A", [999999])
        self.assertEqual((r.status_code, r.json()["code"]), (400, "invalid_stage"))

    def test_bulk_swap_passes_and_a_bulk_clash_writes_nothing(self):
        s = [self.s1.stage_id]
        url = "/events/assign-team-letters/"
        ok = self.api.post(url, {"event_id": self.event.event_id, "stage_ids": s, "assignments": [
            {"team_id": self.tts[0].team_id, "letter": "A"}, {"team_id": self.tts[1].team_id, "letter": "B"},
        ]}, format="json", **self.auth)
        self.assertEqual(ok.status_code, 200)
        swap = self.api.post(url, {"event_id": self.event.event_id, "stage_ids": s, "assignments": [
            {"team_id": self.tts[0].team_id, "letter": "B"}, {"team_id": self.tts[1].team_id, "letter": "A"},
        ]}, format="json", **self.auth)
        self.assertEqual(swap.status_code, 200)
        bad = self.api.post(url, {"event_id": self.event.event_id, "stage_ids": s, "assignments": [
            {"team_id": self.tts[2].team_id, "letter": "C"}, {"team_id": self.tts[3].team_id, "letter": "C"},
        ]}, format="json", **self.auth)
        self.assertEqual((bad.status_code, bad.json()["code"]), (409, "letter_taken"))
        self.assertFalse(StageTeamLetter.objects.filter(tournament_team__in=self.tts[2:]).exists())

    def test_bulk_is_gated(self):
        stranger = User.objects.create_user(username=_u("p"), email=f"{_u('e')}@t.local",
                                            password="pw-strong-9273", role="player")
        SessionToken.objects.create(user=stranger, token="letters-player-token",
                                    expires_at=timezone.now() + datetime.timedelta(hours=1))
        r = self.api.post("/events/assign-team-letters/", {"event_id": self.event.event_id, "assignments": []},
                          format="json", HTTP_AUTHORIZATION="Bearer letters-player-token")
        self.assertEqual(r.status_code, 403)

    def test_list_returns_rooms_and_stage_letters(self):
        self._assign(0, "Q", [self.s1.stage_id])
        r = self.api.get(f"/events/event-team-letters/?event_id={self.event.event_id}", **self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        s1 = next(s for s in body["stages"] if s["stage_id"] == self.s1.stage_id)
        self.assertEqual(sorted(len(room["tournament_team_ids"]) for room in s1["rooms"]), [2, 2])
        row = next(t for t in body["teams"] if t["tournament_team_id"] == self.pk(0))
        self.assertEqual(row["stage_letters"], {str(self.s1.stage_id): "Q"})


class BroadcastKitGroupTests(LetterRoomsFixture):
    def test_kit_summary_endpoint_lists_stages_and_scopes_to_a_lobby(self):
        SessionToken.objects.create(user=self.admin, token="kit-admin-token",
                                    expires_at=timezone.now() + datetime.timedelta(hours=1))
        r = APIClient().get(
            f"/events/{self.event.event_id}/broadcast-kit/?stage_id={self.s1.stage_id}&group_id={self.g1.group_id}",
            HTTP_AUTHORIZATION="Bearer kit-admin-token",
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        s1 = next(s for s in body["stages"] if s["stage_id"] == self.s1.stage_id)
        self.assertEqual([g["group_name"] for g in s1["groups"]], ["Group 1", "Group 2"])
        self.assertEqual({t["tournament_team_id"] for t in body["teams"]}, {self.pk(0), self.pk(1)})

    def test_kit_summary_scoped_to_a_group_uses_that_stage_letters(self):
        from .broadcast_kit import build_kit_summary
        apply_letters(self.event, {self.pk(0): "A", self.pk(2): "A"}, [self.s1.stage_id])
        rows = build_kit_summary(self.event, self.s1, group=self.g2)
        by_id = {r["tournament_team_id"]: r for r in rows}
        self.assertEqual(set(by_id), {self.pk(2), self.pk(3)})
        self.assertEqual(by_id[self.pk(2)]["assigned_letter"], "A")
