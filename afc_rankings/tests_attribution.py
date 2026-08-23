"""
Admin-initiated ghost attribution (owner 2026-08-24).

WHY THESE EXIST: before `ghost_attribute`, the only route from a ghost to a real team was
`ghost_team_request_claim` -> `ghost_approve_claim`, and the request half is explicitly NOT an admin
action. An admin could only ever approve a claim somebody else filed. Importing FFWS Africa 2026
Fall created ~150 ghosts in one afternoon, so waiting on 150 captains was not a workable process.

What is covered: that the admin route works at all, that the per-team history choice actually
changes what happens (the whole point of asking it), that the bulk route applies one choice to many
and reports partial failure rather than aborting, and that the role gate still holds.
"""
import secrets

from django.test import TestCase

from afc_auth.models import User, SessionToken, Roles, UserRoles
from afc_rankings.models import GhostTeam
from afc_team.models import Team

REASON = "Attributing the imported FFWS ghost to its real profile."


def _admin(username, role_name="head_admin"):
    """A user holding a granular ranking-admin role, which is what admin_views._auth checks."""
    user = User.objects.create(username=username, email=f"{username}@example.com", role="admin")
    role, _ = Roles.objects.get_or_create(role_name=role_name)
    UserRoles.objects.create(user=user, role=role)
    return user


def _token(user):
    return SessionToken.objects.create(user=user, token=secrets.token_hex(32)).token


class GhostAttributeTests(TestCase):
    def setUp(self):
        self.admin = _admin("attr_admin")
        self.token = _token(self.admin)
        self.ghost = GhostTeam.objects.create(
            team_name="CRIMSON MYTH", country="South Africa", created_by=self.admin)
        self.team = Team.objects.create(
            team_name="Crimson Myth", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def _url(self, ghost=None):
        return f"/rankings/ghost-teams/{(ghost or self.ghost).ghost_team_id}/attribute/"

    # ── A1: the route exists and an admin can use it WITHOUT the team asking first ──────────────
    def test_an_admin_can_attribute_without_a_prior_request(self):
        self.assertEqual(self.ghost.claim_status, "unclaimed")
        r = self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": REASON, "move_history": True},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 200, r.content)
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "claimed")
        self.assertEqual(self.ghost.claimed_by_id, self.team.pk)
        self.assertEqual(self.ghost.claim_approved_by_id, self.admin.pk)

    # ── A2 / A3: the history choice must actually change what happens ───────────────────────────
    def test_move_history_true_reports_a_reattribution(self):
        r = self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": REASON, "move_history": True},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["history_moved"])
        # A ghost with no participations still returns a summary dict rather than None: the move ran.
        self.assertIsNotNone(r.json()["reattribution"])

    def test_move_history_false_links_without_moving_anything(self):
        r = self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": REASON, "move_history": False},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertFalse(body["history_moved"])
        # reattribute_ghost_team was never called, so there is no summary to report.
        self.assertIsNone(body["reattribution"])
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claimed_by_id, self.team.pk)

    def test_history_moves_by_default_when_the_caller_says_nothing(self):
        """A silent default of False would quietly strand every point an import created."""
        r = self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": REASON},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["history_moved"])

    # ── guards ──────────────────────────────────────────────────────────────────────────────────
    def test_an_already_claimed_ghost_is_refused(self):
        """Re-pointing a claimed ghost would move history twice. Undo the claim first."""
        self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": REASON, "move_history": False},
            content_type="application/json", **self._auth())
        other = Team.objects.create(
            team_name="Someone Else", join_settings="open",
            team_creator=self.admin, team_owner=self.admin)
        r = self.client.post(
            self._url(), {"team_id": other.pk, "reason": REASON},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 400)
        self.assertIn("already claimed", r.json()["message"])

    def test_a_missing_team_id_is_refused(self):
        r = self.client.post(
            self._url(), {"reason": REASON}, content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_team_id_is_refused(self):
        r = self.client.post(
            self._url(), {"team_id": 99999999, "reason": REASON},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 400)

    def test_a_short_reason_is_refused(self):
        """The >=10 char audit reason gate applies to this write like every other admin write."""
        r = self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": "no"},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 400)

    # ── A5: the role gate ───────────────────────────────────────────────────────────────────────
    def test_a_normal_user_cannot_attribute(self):
        nobody = User.objects.create(username="nobody", email="nb@example.com", role="player")
        r = self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": REASON},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_token(nobody)}")
        self.assertEqual(r.status_code, 403)
        self.ghost.refresh_from_db()
        self.assertEqual(self.ghost.claim_status, "unclaimed")

    def test_an_anonymous_caller_cannot_attribute(self):
        r = self.client.post(
            self._url(), {"team_id": self.team.pk, "reason": REASON},
            content_type="application/json")
        self.assertIn(r.status_code, (400, 401))


class GhostAttributeBulkTests(TestCase):
    """A4: one history choice applied to many, and partial failure reported rather than aborting."""

    def setUp(self):
        self.admin = _admin("bulk_admin")
        self.token = _token(self.admin)
        self.ghosts = [
            GhostTeam.objects.create(team_name=n, country="Nigeria", created_by=self.admin)
            for n in ("ALPHA WOLVES", "BURN KNUCKLES", "CATALYST")
        ]
        self.teams = [
            Team.objects.create(team_name=n, join_settings="open",
                                team_creator=self.admin, team_owner=self.admin)
            for n in ("Alpha Wolves", "Burn Knuckless", "Catalyst FC")
        ]

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    URL = "/rankings/ghost-teams/attribute-bulk/"

    def test_one_choice_is_applied_to_every_item(self):
        items = [{"ghost_team_id": str(g.ghost_team_id), "team_id": t.pk}
                 for g, t in zip(self.ghosts, self.teams)]
        r = self.client.post(
            self.URL, {"items": items, "reason": REASON, "move_history": False},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(len(body["attributed"]), 3)
        self.assertEqual(body["failed"], [])
        self.assertFalse(body["moved_history"])
        for g in self.ghosts:
            g.refresh_from_db()
            self.assertEqual(g.claim_status, "claimed")

    def test_a_bad_item_does_not_stop_the_good_ones(self):
        """Partial success is the point: one bad row must not cost the other hundred."""
        items = [
            {"ghost_team_id": str(self.ghosts[0].ghost_team_id), "team_id": self.teams[0].pk},
            {"ghost_team_id": str(self.ghosts[1].ghost_team_id), "team_id": 99999999},
            {"ghost_team_id": str(self.ghosts[2].ghost_team_id), "team_id": self.teams[2].pk},
        ]
        r = self.client.post(
            self.URL, {"items": items, "reason": REASON, "move_history": False},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(len(body["attributed"]), 2)
        self.assertEqual(len(body["failed"]), 1)
        self.ghosts[0].refresh_from_db()
        self.ghosts[1].refresh_from_db()
        self.assertEqual(self.ghosts[0].claim_status, "claimed")
        self.assertEqual(self.ghosts[1].claim_status, "unclaimed")

    def test_an_empty_item_list_is_refused(self):
        r = self.client.post(
            self.URL, {"items": [], "reason": REASON},
            content_type="application/json", **self._auth())
        self.assertEqual(r.status_code, 400)

    def test_a_normal_user_cannot_bulk_attribute(self):
        nobody = User.objects.create(username="bulk_nobody", email="bn@example.com", role="player")
        items = [{"ghost_team_id": str(self.ghosts[0].ghost_team_id), "team_id": self.teams[0].pk}]
        r = self.client.post(
            self.URL, {"items": items, "reason": REASON},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_token(nobody)}")
        self.assertEqual(r.status_code, 403)
