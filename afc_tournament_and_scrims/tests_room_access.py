"""Who sees a match's room details: the event page and the Help assistant, one rule (inbox #280).

Owner 2026-10-16, with support ticket AFC-8C3CAF (a registered squad asked the Help assistant for its
room ID and password; it could not see them): "The afc bot should be able to see lobby details and
info also, it should of course alway confirm and make sure only registered teams or players asking
for it are who it gives the information to."

The rule (room_access.py, unchanged from the event page's own since 2026-06-17): a group's released
room goes to that group's competitors only; fcfs_room events also give released rooms to waitlisted
competitors; event staff see every room ON THE PAGE, never through the assistant.

These hold both doors:
  - released_rooms_for (what afc_helpbot.facts hands the assistant) for every kind of person;
  - get_event_details (the page), which now calls the same functions, still gating the same way.

Run: python manage.py test afc_tournament_and_scrims.tests_room_access
"""
import datetime
import json
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_helpbot.facts import account_facts
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event, Match, RegisteredCompetitors, StageGroupCompetitor, StageGroups, Stages,
    TournamentTeam, TournamentTeamMember,
)
from afc_tournament_and_scrims.room_access import released_rooms_for


def _user(name, role="player"):
    u = User.objects.create(username=name, email=f"{name}@x.com", full_name=name, role=role,
                            password="x")
    SessionToken.objects.create(user=u, token=f"tok-{name}",
                                expires_at=timezone.now() + datetime.timedelta(hours=1))
    return u


class RoomAccessTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = datetime.date.today()
        self.creator = _user("ra_creator")
        self.event = Event.objects.create(
            competition_type="scrims", participant_type="squad", event_type="internal",
            max_teams_or_players=24, event_name="Room Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="r", event_status="ongoing",
            registration_link="", number_of_stages=1, creator=self.creator,
            is_public=True, is_draft=False, timezone="Africa/Lagos")
        stage = Stages.objects.create(
            event=self.event, stage_name="Day 1", start_date=today, end_date=today,
            number_of_groups=2, stage_format="br", teams_qualifying_from_stage=1,
            stage_status="ongoing")
        self.group_a = StageGroups.objects.create(
            stage=stage, group_name="Group 1", playing_date=today, playing_time="18:00",
            teams_qualifying=1, match_count=2, match_maps=["bermuda", "purgatory"])
        self.group_b = StageGroups.objects.create(
            stage=stage, group_name="Group 2", playing_date=today, playing_time="20:00",
            teams_qualifying=1, match_count=1, match_maps=["bermuda"])
        now = timezone.now()
        # Group 1: map 1 released, map 2 entered but not released, an old map already played.
        self.a1 = Match.objects.create(group=self.group_a, match_number=1, match_map="bermuda",
                                       room_id="111", room_name="AFC G1", room_password="pw-a1",
                                       room_details_released_at=now)
        self.a2 = Match.objects.create(group=self.group_a, match_number=2, match_map="purgatory",
                                       room_id="112", room_password="pw-a2")
        Match.objects.create(group=self.group_a, match_number=3, match_map="kalahari", room_id="113",
                             room_password="pw-played", room_details_released_at=now,
                             result_inputted=True)
        self.b1 = Match.objects.create(group=self.group_b, match_number=1, match_map="bermuda",
                                       room_id="221", room_password="pw-b1",
                                       room_details_released_at=now)

        self.alpha_player = _user("ra_alpha")
        self.bravo_player = _user("ra_bravo")
        self.idle_player = _user("ra_idle")       # team registered, not drawn into a group
        self.stranger = _user("ra_stranger")
        self.admin = _user("ra_admin", role="admin")
        self._team("ALPHA", self.alpha_player, self.group_a)
        self._team("BRAVO", self.bravo_player, self.group_b)
        self._team("IDLE", self.idle_player, None)

    def _team(self, name, member, group, waitlisted=False):
        t = Team.objects.create(team_name=name, team_tag=name[:3], join_settings="open",
                                team_creator=member, team_owner=member, country="NG")
        tt = TournamentTeam.objects.create(event=self.event, team=t, status="active",
                                           is_waitlisted=waitlisted)
        TournamentTeamMember.objects.create(tournament_team=tt, user=member, event=self.event)
        if group is not None:
            StageGroupCompetitor.objects.create(stage_group=group, tournament_team=tt)
        return tt

    # ── what the assistant is given ─────────────────────────────────────────────────────────
    def test_a_member_gets_only_their_groups_released_unplayed_room(self):
        rooms = released_rooms_for(self.alpha_player)
        self.assertEqual([(r["group"], r["match_number"], r["room_id"], r["room_password"])
                          for r in rooms["released"]], [("Group 1", 1, "111", "pw-a1")])
        self.assertEqual([(r["group"], r["match_number"]) for r in rooms["not_posted_yet"]],
                         [("Group 1", 2)])
        self.assertNotIn("room_password", rooms["not_posted_yet"][0])

    def test_the_other_group_gets_its_own_room_only(self):
        rooms = released_rooms_for(self.bravo_player)
        self.assertEqual([r["room_password"] for r in rooms["released"]], ["pw-b1"])

    def test_nobody_else_gets_a_room(self):
        for person in (self.idle_player, self.stranger, None):
            self.assertEqual(released_rooms_for(person), {"released": [], "not_posted_yet": []},
                             getattr(person, "username", None))

    def test_an_admin_gets_no_extra_rooms_through_the_assistant(self):
        self.assertEqual(released_rooms_for(self.admin), {"released": [], "not_posted_yet": []})

    def test_a_finished_event_gives_nothing(self):
        Event.objects.filter(pk=self.event.pk).update(event_status="completed")
        self.assertEqual(released_rooms_for(self.alpha_player)["released"], [])

    def test_fcfs_room_waitlisted_team_gets_released_rooms(self):
        Event.objects.filter(pk=self.event.pk).update(waitlist_mode="fcfs_room")
        late = _user("ra_late")
        self._team("LATE", late, None, waitlisted=True)
        got = sorted(r["room_password"] for r in released_rooms_for(late)["released"])
        self.assertEqual(got, ["pw-a1", "pw-b1"])

    def test_a_solo_player_in_a_group_gets_their_room(self):
        Event.objects.filter(pk=self.event.pk).update(participant_type="solo")
        solo = _user("ra_solo")
        reg = RegisteredCompetitors.objects.create(event=self.event, user=solo, status="registered")
        StageGroupCompetitor.objects.create(stage_group=self.group_b, player=reg)
        self.assertEqual([r["room_password"] for r in released_rooms_for(solo)["released"]], ["pw-b1"])

    def test_the_assistant_facts_carry_the_rooms_for_the_member_only(self):
        facts = account_facts(self.alpha_player)
        self.assertEqual([r["room_password"] for r in facts["my_lobbies"]], ["pw-a1"])
        self.assertEqual(account_facts(self.stranger)["my_lobbies"], [])

    def test_a_busy_day_still_fits_the_bot_limit_and_keeps_the_soonest_room(self):
        """The bot refuses facts over 8000 characters and the whole answer fails, so rooms give way."""
        big = {"event": "E" * 60, "page": "/tournaments/" + "e" * 60, "stage": "S" * 30,
               "group": "G" * 30, "plays_on": "2026-10-16", "group_start_time": "18:00",
               "event_timezone": "Africa/Johannesburg", "match_number": 1, "map": "purgatory",
               "room_id": "1" * 13, "room_name": "R" * 40, "room_password": "P" * 20,
               "room_is_3d": False, "my_group": True}
        many = {"released": [dict(big, match_number=i) for i in range(1, 9)],
                "not_posted_yet": [{"event": "E" * 60, "page": "/x" * 30, "group": "G" * 30,
                                    "match_number": i, "plays_on": "2026-10-16"} for i in range(1, 9)]}
        with patch("afc_tournament_and_scrims.room_access.released_rooms_for", return_value=many):
            facts = account_facts(self.alpha_player)
        self.assertLessEqual(len(json.dumps(facts, default=str)), 8000)
        self.assertEqual(facts["my_lobbies"][0]["match_number"], 1)

    # ── the event page, through the same rule ───────────────────────────────────────────────
    def _page_rooms(self, user):
        extra = {"HTTP_AUTHORIZATION": f"Bearer tok-{user.username}"} if user else {}
        url = "/events/get-event-details/" if user else "/events/get-event-details-not-logged-in/"
        resp = self.client.post(url, data={"slug": self.event.slug}, content_type="application/json",
                                **extra)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        groups = resp.json()["event_details"]["stages"][0]["groups"]
        return {(g["group_name"], m["match_number"]): m["room_password"]
                for g in groups for m in g.get("matches", [])}

    def test_the_page_still_shows_a_member_only_their_released_room(self):
        rooms = self._page_rooms(self.alpha_player)
        self.assertEqual(rooms[("Group 1", 1)], "pw-a1")
        self.assertIsNone(rooms[("Group 1", 2)])
        self.assertIsNone(rooms[("Group 2", 1)])

    def test_the_page_shows_a_stranger_no_room(self):
        self.assertTrue(all(v is None for v in self._page_rooms(self.stranger).values()))

    def test_the_page_still_shows_staff_every_room(self):
        rooms = self._page_rooms(self.admin)
        self.assertEqual(rooms[("Group 2", 1)], "pw-b1")
        self.assertEqual(rooms[("Group 1", 2)], "pw-a2")
