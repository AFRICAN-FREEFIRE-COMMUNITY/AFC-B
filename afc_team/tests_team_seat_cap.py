r"""A team never gets a 7th player, by any door (owner 2026-10-18, inbox #314).

THE QUESTION: "HOW ARE THERE TEAM ON AFC SITE THAT HAVE 7 MEMBERS WHO ARE PLAYERS?" Production that
day: 14 of 937 teams held 7 or 8 players. Thirteen went over between December 2025 and June 2026,
when three of the four ways in skipped the 6-player check (the open join link checked nothing); the
fourteenth on 5 August, the day that fix was written. The fix of 4 and 5 August closed those doors
and on purpose removed nobody.

Two doors were still open, and these tests pin them shut:
  1. admin_add_member checked the 9-person headcount only, so an admin could seat a 7th player. It
     also took a force-moved player OFF their old team before checking the new one, so a refused
     add left them on no team at all.
  2. Every join path read the roster and then wrote to it with nothing in between, so two people
     accepted at the same moment could both take the last playing seat. Every seat is now taken
     under _team_seat_lock (a row lock on the team).

The structural test reads the module's syntax tree (rule R47: the owning module, not a regex over
source): a TeamMembers row created outside the lock fails it, proven both ways with a fixture.

Run: .venv\Scripts\python.exe manage.py test afc_team.tests_team_seat_cap
"""
import ast
import datetime
import inspect
import json

from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from afc_auth.models import Notifications, SessionToken, User
from afc_team import views as team_views
from afc_team.models import Report, Team, TeamMembers
from afc_team.views import MAX_MEMBERS, MAX_PLAYERS, PLAYER_ROLES

ADD = "/team/admin-add-member/"
JOIN = "/team/join-team/"

# The one place a TeamMembers row may be created without the seat lock: the founding captain of a
# team that does not exist until that same request.
UNLOCKED_ALLOWED = {"create_team"}


def unlocked_creates(source):
    """(function, line) for every TeamMembers.objects.create(...) that is not inside a
    `with _team_seat_lock(...)` block, outside the allowed functions."""
    tree = ast.parse(source)
    found = []

    def is_create(node):
        f = node.func if isinstance(node, ast.Call) else None
        return (isinstance(f, ast.Attribute) and f.attr == "create"
                and isinstance(f.value, ast.Attribute) and f.value.attr == "objects"
                and isinstance(f.value.value, ast.Name) and f.value.value.id == "TeamMembers")

    def is_lock(item):
        call = item.context_expr
        return isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_team_seat_lock"

    def walk(node, func, locked):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name, False)
                continue
            inner = locked or (isinstance(child, ast.With) and any(is_lock(i) for i in child.items))
            if isinstance(child, ast.Call) and is_create(child) and not locked and func not in UNLOCKED_ALLOWED:
                found.append((func, child.lineno))
            walk(child, func, inner)

    walk(tree, None, False)
    return found


class EverySeatIsTakenUnderTheLock(TestCase):
    def test_no_member_is_created_outside_the_seat_lock(self):
        self.assertEqual(unlocked_creates(inspect.getsource(team_views)), [])

    def test_the_check_finds_an_unlocked_create(self):
        fixture = (
            "def join(team, user):\n"
            "    TeamMembers.objects.create(team=team, member=user)\n"
            "def join_locked(team, user):\n"
            "    with _team_seat_lock(team):\n"
            "        TeamMembers.objects.create(team=team, member=user)\n"
            "def create_team(user):\n"
            "    TeamMembers.objects.create(member=user)\n"
        )
        self.assertEqual(unlocked_creates(fixture), [("join", 2)])


class SeatCapBase(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = self._user("seat_owner")
        self.team = self._team("Seat Cap", "SCP", self.owner)
        self.admin = self._user("seat_admin")
        self.admin.role = "admin"
        self.admin.save()

    def _user(self, name):
        u = User.objects.create(
            username=name, email=f"{name}@x.com", full_name=name, role="player", password="x")
        SessionToken.objects.create(
            user=u, token=f"tok-{name}",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        return u

    def _team(self, name, tag, owner):
        team = Team.objects.create(team_name=name, team_tag=tag, join_settings="open",
                                   team_creator=owner, team_owner=owner, country="NG")
        TeamMembers.objects.create(team=team, member=owner, management_role="team_captain")
        return team

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer tok-{user.username}"}

    def _fill_players(self, team=None):
        team = team or self.team
        for i in range(MAX_PLAYERS - 1):
            TeamMembers.objects.create(team=team, member=self._user(f"{team.team_tag}_p{i}"),
                                       management_role="member")

    def _players(self, team=None):
        return TeamMembers.objects.filter(team=team or self.team, management_role__in=PLAYER_ROLES).count()

    def _admin_add(self, player, role="member", **extra):
        body = {"team_id": self.team.team_id, "player_id": player.user_id, "management_role": role, **extra}
        return self.client.post(ADD, data=json.dumps(body), content_type="application/json",
                                **self._auth(self.admin))


class AdminCannotSeatASeventhPlayer(SeatCapBase):
    def test_a_seventh_player_is_refused_and_named(self):
        self._fill_players()
        res = self._admin_add(self._user("seventh"))
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()["code"], "team_full")
        self.assertIn(f"maximum of {MAX_PLAYERS} players", res.json()["message"])
        self.assertEqual(self._players(), MAX_PLAYERS)

    def test_override_lifts_the_headcount_but_never_the_player_cap(self):
        self._fill_players()
        res = self._admin_add(self._user("seventh_override"), override_limit=True)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(self._players(), MAX_PLAYERS)

    def test_a_full_playing_side_still_takes_staff_from_an_admin(self):
        self._fill_players()
        res = self._admin_add(self._user("new_coach"), role="coach")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self._players(), MAX_PLAYERS)

    def test_a_second_coach_is_refused(self):
        TeamMembers.objects.create(team=self.team, member=self._user("coach_one"), management_role="coach")
        res = self._admin_add(self._user("coach_two"), role="coach")
        self.assertEqual(res.status_code, 400, res.content)

    def test_headcount_override_still_seats_a_tenth_person_as_staff_free_seat(self):
        # 6 players + coach + manager: one seat left, the analyst. A full 9 is reached, then the
        # override lets an admin past the headcount only when a seat of that kind is free.
        self._fill_players()
        TeamMembers.objects.create(team=self.team, member=self._user("c"), management_role="coach")
        TeamMembers.objects.create(team=self.team, member=self._user("m"), management_role="manager")
        res = self._admin_add(self._user("analyst"), role="analyst")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(TeamMembers.objects.filter(team=self.team).count(), MAX_MEMBERS)


class ARefusedForceMoveLeavesThePlayerWhereTheyWere(SeatCapBase):
    def test_refused_move_keeps_the_old_team(self):
        self._fill_players()
        other_owner = self._user("other_owner")
        other = self._team("Other Side", "OTH", other_owner)
        mover = self._user("mover")
        TeamMembers.objects.create(team=other, member=mover, management_role="member")

        res = self._admin_add(mover, force_move=True)

        self.assertEqual(res.status_code, 400, res.content)
        self.assertTrue(TeamMembers.objects.filter(team=other, member=mover).exists(),
                        "a refused add must not take the player off their old team")
        self.assertFalse(Report.objects.filter(team=other, action="player_removed").exists())
        self.assertFalse(Notifications.objects.filter(user=other_owner, notification_type="team_kick").exists())

    def test_allowed_move_moves(self):
        other = self._team("Other Side", "OTH", self._user("other_owner2"))
        mover = self._user("mover2")
        TeamMembers.objects.create(team=other, member=mover, management_role="member")

        res = self._admin_add(mover, force_move=True)

        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(TeamMembers.objects.filter(team=other, member=mover).exists())
        self.assertTrue(TeamMembers.objects.filter(team=self.team, member=mover).exists())


class JoinTakesTheTeamLock(SeatCapBase):
    def test_a_join_locks_the_team_row_before_it_counts(self):
        joiner = self._user("locked_joiner")
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.post(JOIN, data=json.dumps({"team_id": self.team.team_id}),
                                   content_type="application/json", **self._auth(joiner))
        self.assertEqual(res.status_code, 200, res.content)
        sql = [q["sql"] for q in ctx.captured_queries]
        lock_at = next((i for i, s in enumerate(sql) if "FOR UPDATE" in s.upper() and "afc_team_team" in s), None)
        self.assertIsNotNone(lock_at, "the team row is never locked")
        insert_at = next(i for i, s in enumerate(sql) if s.upper().startswith("INSERT INTO") and "afc_team_teammembers" in s)
        self.assertLess(lock_at, insert_at)
