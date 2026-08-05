r"""One invite link, several people.

WHY (owner 2026-08-05): "we need a way for teams to be able to generate links that multiple team
members can use to join the team and not just separate links option."

An invite was strictly single-use. The first person to accept flipped status_of_invite to
'attended_to' and everybody after them got "Invite already used", so a captain filling four seats
had to mint and hand out four separate links.

THE PROPERTY THAT MATTERS MOST HERE IS THE ONE EASIEST TO BREAK: every invite that already exists,
and every direct invite to a named person, must keep behaving EXACTLY as it does today. max_uses
is NULL on all of them and NULL still means one use. Several tests below exist only to hold that
line, because a shared-link bug that leaks into direct invites would let one person's invitation be
spent by somebody else.

Run: .venv\Scripts\python.exe manage.py test afc_team.tests_multi_use_invite
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User
from afc_team.models import Invite, Team, TeamMembers
from afc_team.views import MAX_MEMBERS

GENERATE = "/team/generate-invite-link/"


class MultiUseInviteTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = self._user("mu_owner")
        self.team = Team.objects.create(
            team_name="Multi Use", team_tag="MUL", join_settings="invite_only",
            team_creator=self.owner, team_owner=self.owner, country="NG")
        TeamMembers.objects.create(
            team=self.team, member=self.owner, management_role="team_captain")

    def _user(self, name):
        u = User.objects.create(
            username=name, email=f"{name}@x.com", full_name=name, role="player", password="x")
        SessionToken.objects.create(
            user=u, token=f"tok-{name}",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        return u

    def _generate(self, **data):
        return self.client.post(GENERATE, data=data, HTTP_AUTHORIZATION="Bearer tok-mu_owner")

    def _accept(self, invite_id, username):
        return self.client.post(
            f"/team/respond-invite/{invite_id}/", data={"action": "accept"},
            HTTP_AUTHORIZATION=f"Bearer tok-{username}")

    # ── minting ──
    def test_a_link_with_no_max_uses_is_still_single_use(self):
        """THE BACKWARD-COMPATIBILITY LINE. Every existing caller sends no max_uses."""
        resp = self._generate(role="member")

        self.assertEqual(resp.status_code, 200, resp.content)
        invite = Invite.objects.get()
        self.assertIsNone(invite.max_uses)
        self.assertFalse(invite.is_multi_use())

    def test_a_link_can_be_minted_for_several_people(self):
        resp = self._generate(role="member", max_uses=4)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["max_uses"], 4)
        self.assertEqual(resp.json()["uses_left"], 4)

    def test_max_uses_of_one_is_treated_as_an_ordinary_single_use_link(self):
        self._generate(role="member", max_uses=1)

        self.assertIsNone(Invite.objects.get().max_uses)

    def test_a_link_cannot_be_minted_for_more_people_than_a_team_can_hold(self):
        """A link for 50 is either a mistake or one somebody is about to regret sharing, and a
        team can never seat that many anyway."""
        resp = self._generate(role="member", max_uses=MAX_MEMBERS + 1)

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Invite.objects.count(), 0)

    def test_a_nonsense_max_uses_is_refused_rather_than_crashing(self):
        resp = self._generate(role="member", max_uses="lots")

        self.assertEqual(resp.status_code, 400, resp.content)

    # ── using ──
    def test_three_different_people_can_use_one_link(self):
        """THE ASK, stated plainly."""
        self._generate(role="member", max_uses=3)
        invite = Invite.objects.get()
        for name in ("mu_a", "mu_b", "mu_c"):
            self._user(name)
            resp = self._accept(invite.invite_id, name)
            self.assertEqual(resp.status_code, 200, f"{name}: {resp.content}")

        self.assertEqual(TeamMembers.objects.filter(team=self.team).count(), 4)  # + the owner

    def test_the_link_reports_how_many_uses_are_left(self):
        self._generate(role="member", max_uses=3)
        invite = Invite.objects.get()
        self._user("mu_first")

        resp = self._accept(invite.invite_id, "mu_first")

        self.assertEqual(resp.json()["uses_left"], 2)

    def test_the_link_stops_working_once_its_uses_are_spent(self):
        self._generate(role="member", max_uses=2)
        invite = Invite.objects.get()
        for name in ("mu_x", "mu_y"):
            self._user(name)
            self._accept(invite.invite_id, name)

        self._user("mu_z")
        resp = self._accept(invite.invite_id, "mu_z")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("no uses left", resp.json()["message"])
        self.assertFalse(TeamMembers.objects.filter(member__username="mu_z").exists())

    def test_a_spent_link_is_marked_closed(self):
        self._generate(role="member", max_uses=1 + 1)
        invite = Invite.objects.get()
        for name in ("mu_p", "mu_q"):
            self._user(name)
            self._accept(invite.invite_id, name)

        invite.refresh_from_db()
        self.assertEqual(invite.status_of_invite, "attended_to")
        self.assertTrue(invite.is_exhausted())

    def test_it_records_who_used_it(self):
        """What a captain needs the moment a shared link leaks."""
        self._generate(role="member", max_uses=3)
        invite = Invite.objects.get()
        user = self._user("mu_tracked")

        self._accept(invite.invite_id, "mu_tracked")

        invite.refresh_from_db()
        self.assertIn(user.pk, invite.accepted_user_ids)

    def test_one_person_cannot_spend_two_uses(self):
        """They would have to leave the team first, which is exactly the loop this closes."""
        self._generate(role="member", max_uses=3)
        invite = Invite.objects.get()
        user = self._user("mu_greedy")
        self._accept(invite.invite_id, "mu_greedy")
        TeamMembers.objects.filter(member=user).delete()   # they left the team

        resp = self._accept(invite.invite_id, "mu_greedy")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Invite.objects.get().use_count, 1)

    def test_one_person_declining_does_not_close_a_shared_link(self):
        """One person saying no is not a reason to shut a door the captain opened for several."""
        self._generate(role="member", max_uses=3)
        invite = Invite.objects.get()
        self._user("mu_decliner")
        self.client.post(
            f"/team/respond-invite/{invite.invite_id}/", data={"action": "decline"},
            HTTP_AUTHORIZATION="Bearer tok-mu_decliner")

        invite.refresh_from_db()
        self.assertEqual(invite.status_of_invite, "unattended_to")
        self.assertEqual(invite.uses_left(), 3)

        self._user("mu_after")
        self.assertEqual(self._accept(invite.invite_id, "mu_after").status_code, 200)

    # ── the old behaviour, untouched ──
    def test_a_single_use_link_still_closes_on_first_accept(self):
        self._generate(role="member")
        invite = Invite.objects.get()
        self._user("mu_solo")
        self._accept(invite.invite_id, "mu_solo")

        invite.refresh_from_db()
        self.assertEqual(invite.status_of_invite, "attended_to")

        self._user("mu_late")
        resp = self._accept(invite.invite_id, "mu_late")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already used", resp.json()["message"])

    def test_a_single_use_link_still_records_its_invitee(self):
        """Direct invites are addressed to a person and the UI reads invitee. A shared link
        deliberately does NOT set it, so this pins that the two paths stayed apart."""
        self._generate(role="member")
        invite = Invite.objects.get()
        user = self._user("mu_named")
        self._accept(invite.invite_id, "mu_named")

        invite.refresh_from_db()
        self.assertEqual(invite.invitee_id, user.pk)

    def test_a_shared_link_does_not_pretend_it_was_addressed_to_the_last_joiner(self):
        self._generate(role="member", max_uses=3)
        invite = Invite.objects.get()
        self._user("mu_shared")
        self._accept(invite.invite_id, "mu_shared")

        invite.refresh_from_db()
        self.assertIsNone(invite.invitee_id)
