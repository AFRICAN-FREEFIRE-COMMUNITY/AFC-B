"""
afc_auth/test_identity_lock_naming.py - the identity lock says WHICH event holds it, and a waitlist
holds nothing (owner 2026-09-13, inbox #13 + #14).

"it should also tell people what event they are locked into" and "even being on waitlist and even if
they did not get to play still holds teams". Both were true of the in-game name / Free Fire UID lock
as well as the team roster lock, and on production the same 29 people were caught by both: sitting
on the WAITLIST of a scrim that had ended and been reopened.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_auth.test_identity_lock_naming
"""
import datetime

from django.test import Client, TestCase

from afc_auth.models import Roles, SessionToken, User, UserRoles
from afc_auth.views import _has_active_event_registration, _identity_locking_events
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    RegisteredCompetitors,
    TournamentTeam,
    TournamentTeamMember,
)


def _event(name, **over):
    """A started, not-finished, registration-closed event: the shape where the identity lock holds,
    so the only thing under test is what the new rules change."""
    today = datetime.date.today()
    fields = dict(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=today - datetime.timedelta(days=1),
        end_date=today + datetime.timedelta(days=5),
        registration_open_date=today - datetime.timedelta(days=3),
        registration_end_date=today - datetime.timedelta(days=1),
        prizepool="0", event_rules="r", event_status="ongoing",
        registration_link="https://example.com/r", number_of_stages=1, is_draft=False,
    )
    fields.update(over)
    return Event.objects.create(**fields)


class IdentityLockNamesTheEventTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="lockedplayer", email="lp@x.com",
                                        full_name="Locked Player", password="x")
        self.token = SessionToken.objects.create(user=self.user, token="tok_lp").token
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        self.team = Team.objects.create(team_name="Holders", team_tag="HLD", country="NG",
                                        join_settings="open", team_owner=self.user,
                                        team_creator=self.user)
        self.client = Client()

    def _try_rename(self, new_name):
        """edit_profile is a full-form save: full_name + in_game_name + email are all required, so a
        rename posts the whole form with only the in-game name changed."""
        return self.client.post(
            "/auth/edit-profile/",
            {"full_name": self.user.full_name, "email": self.user.email, "in_game_name": new_name},
            content_type="application/json", **self.auth,
        )

    def _roster(self, event, waitlisted=False):
        tt = TournamentTeam.objects.create(event=event, team=self.team, status="active",
                                           is_waitlisted=waitlisted)
        TournamentTeamMember.objects.create(tournament_team=tt, user=self.user, event=event)
        return tt

    def test_the_locking_event_is_named(self):
        event = _event("FFWS AFRICA FINALS")
        self._roster(event)
        locking = _identity_locking_events(self.user)
        self.assertEqual([e.event_id for e in locking], [event.event_id])
        self.assertTrue(_has_active_event_registration(self.user))

    def test_the_profile_reports_the_events_beside_the_boolean(self):
        event = _event("FFWS AFRICA FINALS")
        self._roster(event)
        r = self.client.get("/auth/get-user-profile/", **self.auth)
        self.assertEqual(r.status_code, 200, r.content[:200])
        body = r.json()
        self.assertTrue(body["identity_locked"])
        self.assertEqual([e["event_name"] for e in body["identity_lock_events"]],
                         ["FFWS AFRICA FINALS"])
        self.assertEqual(body["identity_lock_events"][0]["event_id"], event.event_id)

    def test_the_refusal_names_the_event(self):
        event = _event("FFWS AFRICA FINALS")
        self._roster(event)
        r = self._try_rename("NewName")
        self.assertEqual(r.status_code, 400, r.content[:300])
        body = r.json()
        self.assertIn("FFWS AFRICA FINALS", body["message"])
        self.assertEqual([e["event_id"] for e in body["events"]], [event.event_id])
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "lockedplayer")  # unchanged

    def test_a_waitlisted_team_locks_nothing(self):
        # Queued, not playing. They may never be promoted, and the owner saw exactly this hold
        # people: on production the same 29 players were waitlisted on a reopened, finished scrim.
        event = _event("OVERSUBSCRIBED CUP")
        self._roster(event, waitlisted=True)
        self.assertEqual(_identity_locking_events(self.user), [])
        self.assertFalse(_has_active_event_registration(self.user))
        r = self.client.get("/auth/get-user-profile/", **self.auth)
        self.assertFalse(r.json()["identity_locked"])
        self.assertEqual(r.json()["identity_lock_events"], [])

    def test_a_promoted_team_locks_again_by_itself(self):
        event = _event("OVERSUBSCRIBED CUP")
        tt = self._roster(event, waitlisted=True)
        self.assertEqual(_identity_locking_events(self.user), [])
        tt.is_waitlisted = False  # the organizer promotes them off the waitlist
        tt.save(update_fields=["is_waitlisted"])
        self.assertEqual([e.event_id for e in _identity_locking_events(self.user)], [event.event_id])

    def test_a_waitlisted_solo_entry_locks_nothing(self):
        event = _event("SOLO CUP", participant_type="solo")
        RegisteredCompetitors.objects.create(event=event, user=self.user, status="registered",
                                             is_waitlisted=True)
        self.assertEqual(_identity_locking_events(self.user), [])

    def test_two_events_are_both_reported(self):
        a = _event("CUP A")
        b = _event("CUP B")
        self._roster(a)
        self._roster(b)
        names = {e.event_name for e in _identity_locking_events(self.user)}
        self.assertEqual(names, {"CUP A", "CUP B"})
        r = self._try_rename("NewName")
        self.assertEqual(r.status_code, 400)
        self.assertIn("CUP A", r.json()["message"])
        self.assertIn("CUP B", r.json()["message"])


class AdminPanelNamesTheEventTests(TestCase):
    """The head admin is the escape hatch for a mid-event rename (views_admin_identity), so the
    panel they open has to say WHICH event the override would cut across, not merely that one
    exists. Same source as the player's own note: _identity_locking_events + event_refs."""

    def setUp(self):
        self.admin = User.objects.create(username="headboss", email="hb@x.com",
                                         full_name="Head Boss", password="x", role="admin")
        role, _ = Roles.objects.get_or_create(role_name="head_admin",
                                              defaults={"description": "head_admin"})
        UserRoles.objects.create(user=self.admin, role=role)
        self.token = SessionToken.objects.create(user=self.admin, token="tok_hb").token
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        self.player = User.objects.create(username="lockedplayer2", email="lp2@x.com",
                                          full_name="Locked Player Two", password="x")
        self.team = Team.objects.create(team_name="Holders Two", team_tag="HL2", country="NG",
                                        join_settings="open", team_owner=self.player,
                                        team_creator=self.player)
        self.client = Client()

    def _roster(self, event, waitlisted=False):
        tt = TournamentTeam.objects.create(event=event, team=self.team, status="active",
                                           is_waitlisted=waitlisted)
        TournamentTeamMember.objects.create(tournament_team=tt, user=self.player, event=event)
        return tt

    def _panel(self):
        return self.client.get(f"/auth/admin/user-identity/{self.player.user_id}/", **self.auth)

    def test_the_panel_names_the_locking_event(self):
        event = _event("FFWS AFRICA FINALS")
        self._roster(event)
        r = self._panel()
        self.assertEqual(r.status_code, 200, r.content[:300])
        body = r.json()
        self.assertTrue(body["identity_locked"])
        self.assertEqual([e["event_name"] for e in body["identity_lock_events"]],
                         ["FFWS AFRICA FINALS"])
        self.assertEqual(body["identity_lock_events"][0]["event_id"], event.event_id)

    def test_a_waitlisted_player_is_not_locked_in_the_panel(self):
        self._roster(_event("OVERSUBSCRIBED CUP"), waitlisted=True)
        body = self._panel().json()
        self.assertFalse(body["identity_locked"])
        self.assertEqual(body["identity_lock_events"], [])
