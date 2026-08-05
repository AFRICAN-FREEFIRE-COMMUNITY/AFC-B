r"""A full playing side no longer closes a team to everybody.

THE BUG (owner 2026-08-05, reported live: "people are trying to join teams and getting error
occurred ... when 6 players are in, literally no other person can join again").

Item 33 gave a team NINE seats: six playing, plus one coach, one manager and one analyst. But
every join path asked the capacity gate for the role "member", so the moment the six PLAYING seats
filled, the gate refused - and the three staff seats, the entire reason the cap was raised to 9,
could not be reached by anybody joining. The refusal even told people to "join as staff" while
offering no way to do it: a join request and an open-team link both hardcode "member".

Measured on the production clone the day it was reported: 127 of 608 teams were at the playing cap
and therefore closed to every new member.

The fix is _resolve_join_role: fall through to the next free STAFF seat instead of refusing, and
only refuse when all nine seats are taken. These tests pin both halves - that a seventh person
gets in, and that a tenth still does not.

Run: .venv\Scripts\python.exe manage.py test afc_team.tests_join_auto_staff
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import JoinRequest, Team, TeamMembers
from afc_team.views import MAX_MEMBERS, MAX_PLAYERS, PLAYER_ROLES, STAFF_ROLES, _resolve_join_role

JOIN = "/team/join-team/"
REQUEST = "/team/send-join-request/"
REVIEW = "/team/review-join-request/"


class JoinAutoStaffTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = self._user("cap_owner")
        self.team = Team.objects.create(
            team_name="Seat Test", team_tag="SEA", join_settings="open",
            team_creator=self.owner, team_owner=self.owner, country="NG")
        # The owner occupies a PLAYING seat, like every real team.
        TeamMembers.objects.create(team=self.team, member=self.owner, management_role="team_captain")

    def _user(self, name):
        u = User.objects.create(
            username=name, email=f"{name}@x.com", full_name=name, role="player", password="x")
        SessionToken.objects.create(
            user=u, token=f"tok-{name}",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        return u

    def _fill_playing_seats(self):
        """Take every PLAYING seat, owner included, leaving the three staff seats free."""
        for i in range(MAX_PLAYERS - 1):
            TeamMembers.objects.create(
                team=self.team, member=self._user(f"player{i}"), management_role="member")
        self.assertEqual(
            TeamMembers.objects.filter(team=self.team, management_role__in=PLAYER_ROLES).count(),
            MAX_PLAYERS)

    # ── the helper itself ──
    def test_a_free_playing_seat_is_given_as_a_playing_seat(self):
        role, err = _resolve_join_role(self.team, "member")

        self.assertIsNone(err)
        self.assertEqual(role, "member")

    def test_a_full_playing_side_falls_through_to_a_staff_seat(self):
        """THE REPORTED BUG. This returned an error string before the fix."""
        self._fill_playing_seats()

        role, err = _resolve_join_role(self.team, "member")

        self.assertIsNone(err, f"a team with three free staff seats refused a joiner: {err}")
        self.assertIn(role, STAFF_ROLES)

    def test_staff_seats_are_handed_out_one_each_until_they_run_out(self):
        self._fill_playing_seats()
        seats = []
        for i in range(3):
            role, err = _resolve_join_role(self.team, "member")
            self.assertIsNone(err)
            seats.append(role)
            TeamMembers.objects.create(
                team=self.team, member=self._user(f"staff{i}"), management_role=role)

        self.assertEqual(sorted(seats), ["analyst", "coach", "manager"])
        self.assertEqual(TeamMembers.objects.filter(team=self.team).count(), MAX_MEMBERS)

    def test_a_genuinely_full_team_is_still_refused(self):
        """The cap has to keep meaning something. Nine is nine."""
        self._fill_playing_seats()
        for i, role in enumerate(("coach", "manager", "analyst")):
            TeamMembers.objects.create(
                team=self.team, member=self._user(f"st{i}"), management_role=role)

        role, err = _resolve_join_role(self.team, "member")

        self.assertIsNone(role)
        self.assertTrue(err)
        self.assertEqual(TeamMembers.objects.filter(team=self.team).count(), MAX_MEMBERS)

    # ── the open-team link ──
    def test_joining_an_open_team_with_the_playing_side_full_seats_you_as_staff(self):
        self._fill_playing_seats()
        joiner = self._user("late_joiner")

        resp = self.client.post(
            JOIN, data={"team_id": self.team.team_id},
            HTTP_AUTHORIZATION="Bearer tok-late_joiner")

        self.assertEqual(resp.status_code, 200, resp.content)
        row = TeamMembers.objects.get(team=self.team, member=joiner)
        self.assertIn(row.management_role, STAFF_ROLES)

    def test_the_joiner_is_TOLD_they_were_seated_as_staff(self):
        """Landing as coach when you asked to play is a surprise worth naming. Finding out by
        reading the roster later is how this becomes a support message."""
        self._fill_playing_seats()
        self._user("told_joiner")

        resp = self.client.post(
            JOIN, data={"team_id": self.team.team_id},
            HTTP_AUTHORIZATION="Bearer tok-told_joiner")

        body = resp.json()
        self.assertIn(body.get("assigned_role"), STAFF_ROLES)
        self.assertIn("staff", body["message"].lower())

    # ── the request / approve pair ──
    def test_a_join_request_is_accepted_when_only_staff_seats_remain(self):
        """This is what a player hit: the request was refused up-front, so they never even got
        into the captain's queue."""
        self._fill_playing_seats()
        self._user("asker")

        resp = self.client.post(
            REQUEST, data={"team_id": self.team.team_id, "message": "let me in"},
            HTTP_AUTHORIZATION="Bearer tok-asker")

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(JoinRequest.objects.filter(team=self.team).count(), 1)

    def test_approving_that_request_seats_them_as_staff(self):
        self._fill_playing_seats()
        asker = self._user("asker2")
        JoinRequest.objects.create(requester=asker, team=self.team, message="x")
        req = JoinRequest.objects.get(requester=asker)

        resp = self.client.post(
            REVIEW, data={"request_id": req.request_id, "decision": "approved"},
            HTTP_AUTHORIZATION="Bearer tok-cap_owner")

        self.assertEqual(resp.status_code, 200, resp.content)
        row = TeamMembers.objects.get(team=self.team, member=asker)
        self.assertIn(row.management_role, STAFF_ROLES)

    def test_DENYING_a_request_still_works(self):
        """REGRESSION GUARD. The seat is resolved inside the `approved` branch, and the
        notification built after it reads that variable for BOTH decisions. Leaving it undefined
        raised NameError inside the blanket `except Exception`, which would have turned every
        denial into the same "An error occurred." this whole fix is about.

        The decision vocabulary is 'approved' / 'denied' - NOT 'rejected', which the endpoint
        refuses outright. Worth pinning, because the notification copy says "rejected" and it is an
        easy word to carry into a payload by mistake.
        """
        asker = self._user("asker3")
        JoinRequest.objects.create(requester=asker, team=self.team, message="x")
        req = JoinRequest.objects.get(requester=asker)

        resp = self.client.post(
            REVIEW, data={"request_id": req.request_id, "decision": "denied"},
            HTTP_AUTHORIZATION="Bearer tok-cap_owner")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(TeamMembers.objects.filter(team=self.team, member=asker).exists())

    def test_an_ordinary_join_still_lands_in_a_playing_seat(self):
        """The whole change must be invisible on a team with room, which is most of them."""
        joiner = self._user("normal_joiner")

        resp = self.client.post(
            JOIN, data={"team_id": self.team.team_id},
            HTTP_AUTHORIZATION="Bearer tok-normal_joiner")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            TeamMembers.objects.get(team=self.team, member=joiner).management_role, "member")
