r"""SECURITY: view-join-requests-for-a-team must not hand out login identifiers.

THE BUG (found 2026-08-08, fixed the same day). `view_join_requests_for_a_team` was
`@api_view(["POST"])` with NO AUTHENTICATION OF ANY KIND. It took a team_id from an anonymous POST
and returned, for every pending join request on that team, the requester's username AND their Free
Fire uid.

Why that is worse than a privacy leak: sign-in on this platform resolves a typed identifier against
username OR uid OR email (afc_auth.EmailOrUsernameModelBackend). So the endpoint handed an
unauthenticated caller two of the three ways to name a real account, in bulk, for anybody who had
ever asked to join a team. That is ready-made input for credential stuffing. Walking team_id 1..N
also enumerated which teams exist.

The tests below are the regression fence. The one most worth keeping honest is
test_a_missing_team_and_a_forbidden_team_are_indistinguishable: if those two answers ever diverge
again, team ids become enumerable, and that is the kind of thing a later "helpful" 404 quietly
reintroduces.

Run: .venv\Scripts\python.exe manage.py test afc_team.tests_join_request_leak
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import JoinRequest, Team, TeamMembers, TeamRolePermission

URL = "/team/view-join-requests-for-a-team/"


class JoinRequestLeakTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = self._user("jr_owner", uid="111222333")
        self.team = Team.objects.create(
            team_name="Leak FC", team_tag="LEK", join_settings="by_request",
            team_creator=self.owner, team_owner=self.owner, country="NG")
        TeamMembers.objects.create(
            team=self.team, member=self.owner, management_role="team_captain")

        # A plain player and a manager on the team, to prove membership alone is not entitlement.
        self.player = self._user("jr_player")
        TeamMembers.objects.create(
            team=self.team, member=self.player, management_role="member")
        self.manager = self._user("jr_manager")
        TeamMembers.objects.create(
            team=self.team, member=self.manager, management_role="manager")

        # Somebody with no connection to the team at all.
        self.stranger = self._user("jr_stranger")

        # The pending request whose personal data was leaking.
        self.applicant = self._user("jr_applicant", uid="999888777")
        self.join_request = JoinRequest.objects.create(
            requester=self.applicant, team=self.team, message="let me in")

    def _user(self, name, uid=None):
        # User.uid is UNIQUE (it is an account identifier, which is the whole point of this file),
        # so every test user needs a distinct one rather than sharing the empty string.
        self._uid_seq = getattr(self, "_uid_seq", 0) + 1
        u = User.objects.create(
            username=name, email=f"{name}@x.com", full_name=name, role="player",
            password="x", uid=uid or f"7000000{self._uid_seq:02d}")
        SessionToken.objects.create(
            user=u, token=f"tok-{name}",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        return u

    def _get(self, actor=None, team_id=None):
        kwargs = {}
        if actor is not None:
            kwargs["HTTP_AUTHORIZATION"] = f"Bearer tok-{actor.username}"
        return self.client.post(
            URL, data={"team_id": team_id if team_id is not None else self.team.team_id}, **kwargs)

    # ── the leak itself ──
    def test_an_anonymous_caller_is_refused(self):
        """THE BUG. This used to return 200 with usernames and Free Fire UIDs."""
        resp = self._get(actor=None)

        self.assertIn(resp.status_code, (400, 401), resp.content)
        self.assertNotIn(b"999888777", resp.content, "the applicant's UID leaked to an anonymous caller")
        self.assertNotIn(b"jr_applicant", resp.content)

    def test_an_invalid_token_is_refused(self):
        resp = self.client.post(
            URL, data={"team_id": self.team.team_id}, HTTP_AUTHORIZATION="Bearer not-a-real-token")

        self.assertEqual(resp.status_code, 401, resp.content)
        self.assertNotIn(b"999888777", resp.content)

    def test_a_stranger_with_a_valid_session_is_refused(self):
        """Being logged in is not entitlement."""
        resp = self._get(actor=self.stranger)

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertNotIn(b"999888777", resp.content)

    def test_a_plain_member_of_the_team_is_refused(self):
        """Membership is not entitlement either. can_manage_join_requests defaults to owner-only,
        which matches the Requests tab being rendered only for the owner."""
        resp = self._get(actor=self.player)

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertNotIn(b"999888777", resp.content)

    # ── who IS allowed ──
    def test_the_team_owner_is_allowed(self):
        resp = self._get(actor=self.owner)

        self.assertEqual(resp.status_code, 200, resp.content)
        rows = resp.json()["join_requests"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requester"], "jr_applicant")

    def test_the_uid_is_still_delivered_to_an_entitled_caller(self):
        """The Requests table renders a translated UID column so the owner can confirm the
        applicant is the Free Fire player they expect. The fix gates the field, it does not
        remove it."""
        resp = self._get(actor=self.owner)

        self.assertEqual(resp.json()["join_requests"][0]["uid"], "999888777")

    def test_a_role_the_owner_grants_becomes_allowed(self):
        """The entitlement reads the team's own role-permission matrix, so it tracks whatever the
        owner has decided rather than being a second, separate rule."""
        before = self._get(actor=self.manager)
        self.assertEqual(before.status_code, 403, before.content)

        self.client.post(
            "/team/set-role-permissions/",
            data='{"team_id": %d, "permissions": {"manager": {"can_manage_join_requests": true}}}'
                 % self.team.team_id,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer tok-{self.owner.username}")

        after = self._get(actor=self.manager)
        self.assertEqual(after.status_code, 200, after.content)
        self.assertEqual(after.json()["join_requests"][0]["uid"], "999888777")

    def test_revoking_it_again_closes_the_door(self):
        TeamRolePermission.objects.create(
            team=self.team, management_role="manager", can_manage_join_requests=False)

        resp = self._get(actor=self.manager)

        self.assertEqual(resp.status_code, 403, resp.content)

    # ── enumeration ──
    def test_a_missing_team_and_a_forbidden_team_are_indistinguishable(self):
        """If these two ever diverge, team ids become enumerable by an ordinary logged-in user."""
        forbidden = self._get(actor=self.stranger, team_id=self.team.team_id)
        missing = self._get(actor=self.stranger, team_id=99999999)

        self.assertEqual(forbidden.status_code, missing.status_code)
        self.assertEqual(forbidden.json(), missing.json())

    def test_a_nonsense_team_id_does_not_500(self):
        resp = self._get(actor=self.stranger, team_id="not-a-number")

        self.assertEqual(resp.status_code, 403, resp.content)

    def test_a_missing_team_id_is_still_a_400(self):
        resp = self.client.post(
            URL, data={}, HTTP_AUTHORIZATION=f"Bearer tok-{self.owner.username}")

        self.assertEqual(resp.status_code, 400, resp.content)

    def test_another_teams_owner_cannot_read_this_teams_requests(self):
        other_owner = self._user("jr_other_owner")
        Team.objects.create(
            team_name="Other Leak FC", team_tag="OLK", join_settings="open",
            team_creator=other_owner, team_owner=other_owner, country="NG")

        resp = self._get(actor=other_owner)

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertNotIn(b"999888777", resp.content)
