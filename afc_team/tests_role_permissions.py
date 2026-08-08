r"""Team role permissions: the owner decides what each role may do with the team.

Owner 2026-08-08: "a way for team owners to decide what controls the other roles in the team have
over the team."

THE PROPERTY THAT MATTERS MOST IS THE ONE EASIEST TO BREAK: no team alive today has a settings row,
and none of them may behave differently on the day this ships. So the first class below walks EVERY
role through EVERY real endpoint with no settings saved, and asserts the exact answer the
hard-coded gates gave before this feature existed. It also asserts that doing all of that created
zero TeamRolePermission rows, because a fallback that quietly writes rows is a fallback that will
drift.

The rest proves the feature actually does something: a granted capability changes what a role can DO
through the real HTTP endpoint (not just what the UI draws), a revoked one is refused server-side
when the request is sent straight to the API, the owner cannot be locked out, and an AFC admin is
untouched by any of it.

Run: .venv\Scripts\python.exe manage.py test afc_team.tests_role_permissions
"""
import datetime
import json
from datetime import date, timedelta

from django.test import Client, TestCase

from afc_auth.models import Roles, SessionToken, User, UserRoles
from afc_team.models import JoinRequest, Team, TeamMembers, TeamRolePermission
from afc_team.permissions import (
    TEAM_CAPABILITIES,
    default_permission_map,
    resolve_team_permissions,
    team_role_can,
)
from afc_tournament_and_scrims.models import Event
from afc_tournament_and_scrims.views import _user_can_register_team

INVITE = "/team/invite-member/"
GENERATE_LINK = "/team/generate-invite-link/"
REVIEW_JOIN = "/team/review-join-request/"
VIEW_JOIN = "/team/view-join-requests/"
EDIT_TEAM = "/team/edit-team/"
MANAGE_ROSTER = "/team/manage-team-roster/"
KICK = "/team/kick-team-member/"
READ_PERMS = "/team/role-permissions/"
SET_PERMS = "/team/set-role-permissions/"
REGISTER = "/events/register-for-event/"

# The six management roles, and the answer each one holds TODAY for each capability. This is a
# transcription of the pre-2026-08-08 gates, written out longhand on purpose: if it were derived
# from DEFAULT_ROLE_CAPABILITIES it would prove only that the dict equals itself.
#
#   invite / join-requests / edit-profile -> owner-only  (invite_member, review_join_request,
#                                                         view_join_requests, edit_team)
#   edit roster / remove members          -> owner + coach (the old _can_manage_roster)
#   register for events                   -> owner + captain + vice + manager + coach
#                                            (TEAM_EVENT_REGISTER_ROLES)
TODAYS_BEHAVIOUR = {
    "team_captain": {"can_register_for_events"},
    "vice_captain": {"can_register_for_events"},
    "member": set(),
    "coach": {"can_edit_roster", "can_remove_members", "can_register_for_events"},
    "manager": {"can_register_for_events"},
    "analyst": set(),
}


class TeamPermissionTestBase(TestCase):
    """A team with one member seated in every role, plus a token for each of them."""

    def setUp(self):
        self.client = Client()
        self.owner = self._user("rp_owner")
        self.team = Team.objects.create(
            team_name="Role Perms FC", team_tag="RPF", join_settings="by_request",
            team_creator=self.owner, team_owner=self.owner, country="NG")
        # The owner is ALSO seated as team_captain, which is what create_team really does. Every
        # owner-lockout test below depends on this being the realistic shape.
        TeamMembers.objects.create(
            team=self.team, member=self.owner, management_role="team_captain")

        # One person per non-owner role. 'team_captain' gets a second holder so the owner's own
        # captaincy is never the thing under test.
        self.people = {}
        for role in ("team_captain", "vice_captain", "member", "coach", "manager", "analyst"):
            person = self._user(f"rp_{role}")
            TeamMembers.objects.create(team=self.team, member=person, management_role=role)
            self.people[role] = person

    def _user(self, name):
        u = User.objects.create(
            username=name, email=f"{name}@x.com", full_name=name, role="player", password="x")
        SessionToken.objects.create(
            user=u, token=f"tok-{name}",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        return u

    def _auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer tok-{user.username}"}

    def _outsider(self, name="rp_outsider"):
        """Somebody with no team, so they can be invited / kicked without disturbing the roster.

        Suffixed with a per-test counter because several tests call the same _try_* helper twice
        (once before a grant, once after) and User.username is unique.
        """
        self._outsider_seq = getattr(self, "_outsider_seq", 0) + 1
        return self._user(f"{name}{self._outsider_seq}")

    # ── the six capabilities, each exercised through its REAL endpoint ──
    # Every helper returns True when the endpoint let the actor THROUGH its permission gate. A
    # later refusal (capacity, validation) still counts as "allowed", because this is testing the
    # permission gate and nothing else - so each helper asserts on the permission status/message
    # specifically rather than on a bare 200.

    def _try_invite(self, actor):
        invitee = self._outsider(f"inv_target_{actor.username}")
        resp = self.client.post(
            INVITE, data={"invitee_email_or_ign": invitee.username,
                          "team_id": self.team.team_id, "role": "member"},
            **self._auth(actor))
        return resp.status_code != 403, resp

    def _try_generate_link(self, actor):
        resp = self.client.post(
            GENERATE_LINK, data={"role": "member"}, **self._auth(actor))
        return resp.status_code != 403, resp

    def _try_review_join_request(self, actor):
        requester = self._outsider(f"jr_{actor.username}")
        jr = JoinRequest.objects.create(requester=requester, team=self.team)
        resp = self.client.post(
            REVIEW_JOIN, data={"request_id": jr.request_id, "decision": "denied"},
            **self._auth(actor))
        return resp.status_code != 403, resp

    def _try_view_join_requests(self, actor):
        resp = self.client.get(VIEW_JOIN, **self._auth(actor))
        return resp.status_code != 403, resp

    def _try_edit_profile(self, actor):
        resp = self.client.post(
            EDIT_TEAM, data={"team_id": self.team.team_id,
                             "team_description": "edited by " + actor.username},
            **self._auth(actor))
        return resp.status_code != 403, resp

    def _try_edit_roster(self, actor):
        """Promote the plain player to vice-captain (a player->player move, so the transfer-window
        and 6-player rules stay out of the way)."""
        resp = self.client.post(
            MANAGE_ROSTER,
            data=json.dumps({"team_id": self.team.team_id,
                             "updates": [{"member_id": self.people["member"].user_id,
                                          "management_role": "vice_captain"}]}),
            content_type="application/json", **self._auth(actor))
        return resp.status_code != 403, resp

    def _try_kick(self, actor, victim_role="analyst"):
        resp = self.client.post(
            KICK, data={"team_id": self.team.team_id,
                        "member_id": self.people[victim_role].user_id},
            **self._auth(actor))
        return resp.status_code != 403, resp

    def _can(self, actor, capability):
        """Run `capability` through its real endpoint and report whether the gate let `actor` in."""
        runner = {
            "can_invite_members": self._try_invite,
            "can_manage_join_requests": self._try_review_join_request,
            "can_edit_roster": self._try_edit_roster,
            "can_remove_members": self._try_kick,
            "can_edit_team_profile": self._try_edit_profile,
        }[capability]
        allowed, _resp = runner(actor)
        return allowed


class NoSettingsRowMeansNoChange(TeamPermissionTestBase):
    """THE REGRESSION THAT MATTERS. Every existing team has no settings row; none of them may
    behave differently."""

    def test_every_role_gets_exactly_todays_answer_through_the_real_endpoints(self):
        """The whole matrix, walked through live HTTP, with nothing configured."""
        for role, expected in TODAYS_BEHAVIOUR.items():
            actor = self.people[role]
            for capability in TEAM_CAPABILITIES:
                if capability == "can_register_for_events":
                    continue  # covered by its own test below (needs an Event)
                self.assertEqual(
                    self._can(actor, capability), capability in expected,
                    f"{role} / {capability}: behaviour changed for a team with no settings row")

    def test_registration_permission_is_unchanged_for_every_role(self):
        """_user_can_register_team IS the server-side gate for both register_for_event and the
        event-invitation accept/decline path."""
        self.assertTrue(_user_can_register_team(self.owner, self.team))
        for role, expected in TODAYS_BEHAVIOUR.items():
            self.assertEqual(
                _user_can_register_team(self.people[role], self.team),
                "can_register_for_events" in expected, f"{role} registration permission changed")

    def test_a_stranger_holds_nothing(self):
        stranger = self._outsider("rp_stranger")
        for capability in TEAM_CAPABILITIES:
            self.assertFalse(team_role_can(stranger, self.team, capability))
        self.assertFalse(_user_can_register_team(stranger, self.team))

    def test_none_of_that_created_a_settings_row(self):
        """A fallback that writes rows is a fallback that drifts."""
        for role in TODAYS_BEHAVIOUR:
            for capability in TEAM_CAPABILITIES:
                team_role_can(self.people[role], self.team, capability)
        self._try_invite(self.people["coach"])
        self._try_edit_roster(self.people["coach"])

        self.assertEqual(TeamRolePermission.objects.count(), 0)

    def test_the_resolved_matrix_equals_the_stock_matrix(self):
        self.assertEqual(resolve_team_permissions(self.team), default_permission_map())

    def test_the_read_endpoint_reports_the_team_as_uncustomised(self):
        resp = self.client.get(
            READ_PERMS, {"team_id": self.team.team_id}, **self._auth(self.owner))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["is_customised"])
        self.assertEqual(resp.json()["permissions"], resp.json()["defaults"])


class GrantingAndRevokingChangesWhatARoleCanDo(TeamPermissionTestBase):
    """A switch that does not change a real endpoint's answer is decoration."""

    def _set(self, actor, permissions, expect=200):
        resp = self.client.post(
            SET_PERMS,
            data=json.dumps({"team_id": self.team.team_id, "permissions": permissions}),
            content_type="application/json", **self._auth(actor))
        self.assertEqual(resp.status_code, expect, resp.content)
        return resp

    # ── granting ──
    def test_granting_invite_lets_a_captain_actually_invite(self):
        captain = self.people["team_captain"]
        blocked, resp = self._try_invite(captain)
        self.assertFalse(blocked, "captain should be refused before the grant")

        self._set(self.owner, {"team_captain": {"can_invite_members": True}})

        allowed, resp = self._try_invite(captain)
        self.assertTrue(allowed, resp.content)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_granting_invite_also_lets_them_mint_a_link(self):
        captain = self.people["team_captain"]
        self.assertFalse(self._try_generate_link(captain)[0])

        self._set(self.owner, {"team_captain": {"can_invite_members": True}})

        allowed, resp = self._try_generate_link(captain)
        self.assertTrue(allowed, resp.content)
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_granting_join_request_review_works_through_both_endpoints(self):
        manager = self.people["manager"]
        self.assertFalse(self._try_review_join_request(manager)[0])
        self.assertFalse(self._try_view_join_requests(manager)[0])

        self._set(self.owner, {"manager": {"can_manage_join_requests": True}})

        self.assertTrue(self._try_review_join_request(manager)[0])
        resp = self.client.get(VIEW_JOIN, **self._auth(manager))
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_granting_remove_lets_a_manager_actually_kick_somebody(self):
        manager = self.people["manager"]
        self.assertFalse(self._try_kick(manager)[0])

        self._set(self.owner, {"manager": {"can_remove_members": True}})
        allowed, resp = self._try_kick(manager)

        self.assertTrue(allowed, resp.content)
        self.assertFalse(
            TeamMembers.objects.filter(team=self.team, member=self.people["analyst"]).exists(),
            "the kick was permitted but the member is still on the roster")

    def test_granting_profile_edit_actually_writes_the_profile(self):
        vice = self.people["vice_captain"]
        self.assertFalse(self._try_edit_profile(vice)[0])

        self._set(self.owner, {"vice_captain": {"can_edit_team_profile": True}})
        allowed, resp = self._try_edit_profile(vice)

        self.assertTrue(allowed, resp.content)
        self.team.refresh_from_db()
        self.assertEqual(self.team.team_description, f"edited by {vice.username}")

    def test_granting_registration_changes_the_real_registration_gate(self):
        player = self.people["member"]
        self.assertFalse(_user_can_register_team(player, self.team))

        self._set(self.owner, {"member": {"can_register_for_events": True}})

        self.assertTrue(_user_can_register_team(player, self.team))

    # ── revoking ──
    def test_revoking_roster_editing_takes_it_off_the_coach(self):
        """The coach is the ONLY non-owner who can edit a roster today. Taking it away must work."""
        coach = self.people["coach"]
        self.assertTrue(self._try_edit_roster(coach)[0], "coach should start with roster editing")

        self._set(self.owner, {"coach": {"can_edit_roster": False}})
        allowed, resp = self._try_edit_roster(coach)

        self.assertFalse(allowed)
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_revoking_removal_stops_the_coach_kicking_anybody(self):
        coach = self.people["coach"]
        self.assertTrue(self._try_kick(coach, "analyst")[0])
        TeamMembers.objects.create(
            team=self.team, member=self.people["analyst"], management_role="analyst")

        self._set(self.owner, {"coach": {"can_remove_members": False}})
        allowed, resp = self._try_kick(coach, "analyst")

        self.assertFalse(allowed)
        self.assertTrue(
            TeamMembers.objects.filter(team=self.team, member=self.people["analyst"]).exists(),
            "the kick was refused but the member was removed anyway")

    def test_revoking_registration_is_enforced_by_the_real_event_endpoint(self):
        """Sent straight to the API, with no UI involved."""
        coach = self.people["coach"]
        self._set(self.owner, {"coach": {"can_register_for_events": False}})
        today = date.today()
        event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Perm Cup", event_mode="virtual",
            start_date=today + timedelta(days=3), end_date=today + timedelta(days=4),
            registration_open_date=today, registration_end_date=today + timedelta(days=2),
            prizepool="$100", prizepool_cash_value=100, prize_distribution={"1": "100%"},
            event_rules="none", event_status="upcoming", tournament_tier="tier_3",
            number_of_stages=1, creator=self.owner, is_draft=False, is_public=True)

        resp = self.client.post(
            REGISTER,
            data=json.dumps({"event_id": event.event_id, "team_id": self.team.team_id,
                             "roster_member_ids": []}),
            content_type="application/json", **self._auth(coach))

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn("can register the team", resp.json().get("message", ""))

    def test_the_two_roster_capabilities_are_independent(self):
        """An owner may want a coach to shuffle positions without being able to throw players out."""
        coach = self.people["coach"]
        self._set(self.owner, {"coach": {"can_remove_members": False}})

        self.assertTrue(self._try_edit_roster(coach)[0], "editing should survive")
        self.assertFalse(self._try_kick(coach)[0], "removing should be gone")

    # ── saving behaviour ──
    def test_a_partial_save_leaves_every_other_role_alone(self):
        self._set(self.owner, {"member": {"can_invite_members": True}})

        matrix = resolve_team_permissions(self.team)
        stock = default_permission_map()
        for role in ("team_captain", "vice_captain", "coach", "manager", "analyst"):
            self.assertEqual(matrix[role], stock[role], f"{role} changed without being asked")

    def test_a_partial_save_keeps_the_roles_other_capabilities(self):
        """Toggling one switch must not silently strip the five the role already had."""
        self._set(self.owner, {"coach": {"can_invite_members": True}})

        coach_row = resolve_team_permissions(self.team)["coach"]
        self.assertTrue(coach_row["can_invite_members"])
        self.assertTrue(coach_row["can_edit_roster"], "coach lost roster editing it already had")
        self.assertTrue(coach_row["can_remove_members"])
        self.assertTrue(coach_row["can_register_for_events"])

    def test_saving_twice_is_idempotent(self):
        self._set(self.owner, {"manager": {"can_remove_members": True}})
        self._set(self.owner, {"manager": {"can_remove_members": True}})

        self.assertEqual(
            TeamRolePermission.objects.filter(team=self.team, management_role="manager").count(), 1)

    def test_an_unknown_role_is_refused_rather_than_ignored(self):
        self._set(self.owner, {"supreme_leader": {"can_invite_members": True}}, expect=400)
        self.assertEqual(TeamRolePermission.objects.count(), 0)

    def test_an_unknown_capability_is_refused_rather_than_ignored(self):
        self._set(self.owner, {"coach": {"can_delete_the_internet": True}}, expect=400)
        self.assertEqual(TeamRolePermission.objects.count(), 0)

    def test_a_settings_change_shows_up_in_get_team_details(self):
        """The frontend reads my_capabilities from here, so it must track the matrix."""
        self._set(self.owner, {"analyst": {"can_edit_roster": True}})

        resp = self.client.post(
            "/team/get-team-details/", data={"team_name": self.team.team_name},
            **self._auth(self.people["analyst"]))

        self.assertEqual(resp.status_code, 200, resp.content)
        caps = resp.json()["team"]["my_capabilities"]
        self.assertTrue(caps["can_edit_roster"])
        self.assertFalse(caps["can_invite_members"])


class TheOwnerCannotBeLockedOut(TeamPermissionTestBase):
    """A team that can permanently disable its own owner is a support ticket forever."""

    def _revoke_everything_from_every_role(self):
        self.client.post(
            SET_PERMS,
            data=json.dumps({
                "team_id": self.team.team_id,
                "permissions": {role: {cap: False for cap in TEAM_CAPABILITIES}
                                for role in TODAYS_BEHAVIOUR},
            }),
            content_type="application/json", **self._auth(self.owner))

    def test_the_owner_keeps_every_control_after_revoking_everything(self):
        """The owner is seated as team_captain here, and team_captain has just been stripped
        bare - the owner short-circuit is what has to hold."""
        self._revoke_everything_from_every_role()

        for capability in TEAM_CAPABILITIES:
            self.assertTrue(team_role_can(self.owner, self.team, capability), capability)
        self.assertTrue(_user_can_register_team(self.owner, self.team))

    def test_the_owner_can_still_use_the_real_endpoints_after_revoking_everything(self):
        self._revoke_everything_from_every_role()

        self.assertTrue(self._try_invite(self.owner)[0])
        self.assertTrue(self._try_edit_profile(self.owner)[0])
        self.assertTrue(self._try_edit_roster(self.owner)[0])
        self.assertTrue(self._try_kick(self.owner)[0])
        self.assertTrue(self._try_review_join_request(self.owner)[0])

    def test_the_owner_can_still_reach_the_settings_screen_after_revoking_everything(self):
        """The way out. If this 403s, the team really is stuck."""
        self._revoke_everything_from_every_role()

        resp = self.client.get(
            READ_PERMS, {"team_id": self.team.team_id}, **self._auth(self.owner))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["can_edit"])

    def test_there_is_no_owner_row_to_write(self):
        """'owner' is not a management_role, so the matrix cannot address the owner at all."""
        resp = self.client.post(
            SET_PERMS,
            data=json.dumps({"team_id": self.team.team_id,
                             "permissions": {"owner": {"can_invite_members": False}}}),
            content_type="application/json", **self._auth(self.owner))

        self.assertEqual(resp.status_code, 400, resp.content)

    def test_nobody_but_the_owner_can_change_the_settings(self):
        for role in TODAYS_BEHAVIOUR:
            resp = self.client.post(
                SET_PERMS,
                data=json.dumps({"team_id": self.team.team_id,
                                 "permissions": {"member": {"can_invite_members": True}}}),
                content_type="application/json", **self._auth(self.people[role]))
            self.assertEqual(resp.status_code, 403, f"{role} was allowed to rewrite the matrix")
        self.assertEqual(TeamRolePermission.objects.count(), 0)

    def test_a_role_granted_everything_still_cannot_rewrite_the_matrix(self):
        """The escalation hole: can_edit_roster must not become can_grant_myself_anything."""
        self.client.post(
            SET_PERMS,
            data=json.dumps({"team_id": self.team.team_id,
                             "permissions": {"coach": {cap: True for cap in TEAM_CAPABILITIES}}}),
            content_type="application/json", **self._auth(self.owner))

        resp = self.client.post(
            SET_PERMS,
            data=json.dumps({"team_id": self.team.team_id,
                             "permissions": {"coach": {"can_invite_members": True}}}),
            content_type="application/json", **self._auth(self.people["coach"]))

        self.assertEqual(resp.status_code, 403, resp.content)

    def test_another_teams_owner_cannot_touch_this_team(self):
        other_owner = self._user("rp_other_owner")
        Team.objects.create(
            team_name="Other FC", team_tag="OTH", join_settings="open",
            team_creator=other_owner, team_owner=other_owner, country="NG")

        resp = self.client.post(
            SET_PERMS,
            data=json.dumps({"team_id": self.team.team_id,
                             "permissions": {"member": {"can_invite_members": True}}}),
            content_type="application/json", **self._auth(other_owner))

        self.assertEqual(resp.status_code, 403, resp.content)


class AfcAdminsAreNotAffected(TeamPermissionTestBase):
    """A team's own settings must never be able to shut AFC staff out."""

    def setUp(self):
        super().setUp()
        self.admin = self._user("rp_admin")
        self.admin.role = "admin"
        self.admin.save()
        role, _ = Roles.objects.get_or_create(role_name="teams_admin")
        self.granular_admin = self._user("rp_teams_admin")
        UserRoles.objects.create(user=self.granular_admin, role=role)

        # The most hostile setting a team can choose.
        self.client.post(
            SET_PERMS,
            data=json.dumps({
                "team_id": self.team.team_id,
                "permissions": {r: {c: False for c in TEAM_CAPABILITIES} for r in TODAYS_BEHAVIOUR},
            }),
            content_type="application/json", **self._auth(self.owner))

    def test_an_admin_can_still_remove_a_member(self):
        resp = self.client.post(
            "/team/admin-remove-member/",
            data=json.dumps({"team_id": self.team.team_id,
                             "member_id": self.people["analyst"].user_id}),
            content_type="application/json", **self._auth(self.admin))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(
            TeamMembers.objects.filter(team=self.team, member=self.people["analyst"]).exists())

    def test_a_granular_teams_admin_can_still_add_a_member(self):
        newcomer = self._outsider("rp_admin_added")
        resp = self.client.post(
            "/team/admin-add-member/",
            data=json.dumps({"team_id": self.team.team_id, "player_id": newcomer.user_id,
                             "management_role": "member"}),
            content_type="application/json", **self._auth(self.granular_admin))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(TeamMembers.objects.filter(team=self.team, member=newcomer).exists())

    def test_an_admin_can_read_the_matrix_without_being_on_the_team(self):
        resp = self.client.get(
            READ_PERMS, {"team_id": self.team.team_id}, **self._auth(self.admin))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["can_edit"], "an admin must not be able to rewrite team prefs")

    def test_an_admin_who_is_not_on_the_team_holds_no_team_capabilities(self):
        """Admin power comes from the admin_* endpoints, NOT from team_role_can leaking it."""
        for capability in TEAM_CAPABILITIES:
            self.assertFalse(team_role_can(self.admin, self.team, capability), capability)


class ReadingTheMatrix(TeamPermissionTestBase):
    def test_a_plain_member_can_see_what_their_role_may_do(self):
        resp = self.client.get(
            READ_PERMS, {"team_id": self.team.team_id}, **self._auth(self.people["member"]))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.json()["can_edit"])
        self.assertEqual(len(resp.json()["capabilities"]), len(TEAM_CAPABILITIES))

    def test_somebody_from_outside_the_team_cannot_read_it(self):
        resp = self.client.get(
            READ_PERMS, {"team_id": self.team.team_id}, **self._auth(self._outsider("rp_nosy")))

        self.assertEqual(resp.status_code, 403, resp.content)

    def test_a_missing_team_id_is_a_400_not_a_crash(self):
        resp = self.client.get(READ_PERMS, **self._auth(self.owner))

        self.assertEqual(resp.status_code, 400, resp.content)

    def test_an_unknown_team_is_a_404(self):
        resp = self.client.get(READ_PERMS, {"team_id": 999999}, **self._auth(self.owner))

        self.assertEqual(resp.status_code, 404, resp.content)
