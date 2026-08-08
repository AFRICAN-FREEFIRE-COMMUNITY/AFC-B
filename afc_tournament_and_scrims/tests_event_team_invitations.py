# afc_tournament_and_scrims/tests_event_team_invitations.py
# ──────────────────────────────────────────────────────────────────────────────
# INVITE A TEAM TO AN EVENT, end to end (owner backlog item 34, 2026-08-06).
#
# The item in the owner's words: "Invite teams to an event as a distinct invitation type they must
# accept or decline."
#
# The thing these tests exist to prove is not that a row can be written. It is that ACCEPTING AN
# INVITATION IS AN ORDINARY REGISTRATION. event_invites.accept_team_invitation does not re-check
# anything itself: it replays the captain's answer through views.register_for_event and hands that
# endpoint's answer back untouched. If that wiring ever breaks - if the forwarded body stops being
# parsed, or somebody "helpfully" writes the registration rows directly here - an invited team would
# quietly stop being subject to the rules every other team obeys. So most of what follows drives the
# accept endpoint into register_for_event's OWN refusals and asserts the wording comes back verbatim:
#
#   * a full event                 -> "Registration limit reached."
#   * a closed registration window -> "Registration is closed."
#   * a roster that is too small   -> "Roster must contain 4 to 6 players."
#   * a team already registered    -> 409 "This team is already registered for this event."
#
# and, in the happy path, that the rows register_for_event writes (RegisteredCompetitors +
# TournamentTeam + TournamentTeamMember) actually exist afterwards - read back from the database,
# not inferred from the 201.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import json
import uuid

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Notifications, SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import (
    Event, EventTeamInvitation, RegisteredCompetitors, TournamentTeam, TournamentTeamMember,
)

CREATE_URL = "/events/team-invitations/create/"
LIST_URL = "/events/team-invitations/"
MINE_URL = "/events/team-invitations/mine/"


def _accept_url(invitation_id):
    return f"/events/team-invitations/{invitation_id}/accept/"


def _decline_url(invitation_id):
    return f"/events/team-invitations/{invitation_id}/decline/"


def _cancel_url(invitation_id):
    return f"/events/team-invitations/{invitation_id}/cancel/"


class EventTeamInvitationTests(TestCase):
    # ── fixtures ──────────────────────────────────────────────────────────────
    def _user(self, name, role="player"):
        return User.objects.create_user(
            username=name, email=f"{name}@afc.test", password="x",
            role=role, status="active", is_active=True, country="Nigeria",
        )

    def _auth(self, user):
        """A real SessionToken, because validate_token is what every endpoint here calls."""
        token = SessionToken.objects.create(
            user=user, token=f"t-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _team(self, label, players=4):
        """A team with its OWN owner + captain and `players` playing members, so no player is
        shared between two teams (a shared player would make the roster conflicts in
        register_for_event fire for reasons unrelated to what is being tested)."""
        owner = self._user(f"{label}_owner")
        team = Team.objects.create(
            team_name=f"Team {label}", join_settings="open",
            team_creator=owner, team_owner=owner, country="Nigeria",
        )
        TeamMembers.objects.create(team=team, member=owner, management_role="team_captain")
        members = [owner]
        for i in range(players - 1):
            player = self._user(f"{label}_p{i}")
            TeamMembers.objects.create(team=team, member=player, management_role="member")
            members.append(player)
        return team, owner, members

    def _post(self, url, body, user):
        return self.client.post(
            url, data=json.dumps(body), content_type="application/json", **self._auth(user),
        )

    def setUp(self):
        self.client = Client()
        self.admin = self._user("inv_admin", role="admin")
        self.outsider = self._user("inv_outsider")

        today = timezone.localdate()
        self.event = Event.objects.create(
            event_name="Invitational Cup", slug="invitational-cup",
            participant_type="squad", competition_type="tournament", event_type="virtual",
            max_teams_or_players=2, is_public=True, is_draft=False, number_of_stages=1,
            start_date=today + datetime.timedelta(days=7),
            end_date=today + datetime.timedelta(days=7),
            registration_open_date=today - datetime.timedelta(days=1),
            registration_end_date=today + datetime.timedelta(days=3),
        )
        self.team, self.captain, self.roster = self._team("Alpha")

    def _invite(self, team=None, inviter=None, message="Come play"):
        """Create one invitation through the endpoint and return its id."""
        res = self._post(
            CREATE_URL,
            {"event_id": self.event.event_id, "team_ids": [(team or self.team).team_id],
             "message": message},
            inviter or self.admin,
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["invited"][0]["id"]

    def _roster_ids(self, members=None):
        return [u.user_id for u in (members or self.roster)]

    # ══════════════════════════════════════════════════════════════════════════
    # 1. Happy path: invite -> accept -> the team is genuinely registered
    # ══════════════════════════════════════════════════════════════════════════
    def test_accept_registers_the_team_through_the_normal_path(self):
        invitation_id = self._invite()

        res = self._post(
            _accept_url(invitation_id), {"roster_member_ids": self._roster_ids()}, self.captain,
        )
        self.assertEqual(res.status_code, 201, res.content)

        # The rows are READ BACK from the database. A 201 only proves the endpoint answered; these
        # three tables are what "registered" actually means everywhere else in the product.
        self.assertTrue(
            TournamentTeam.objects.filter(event=self.event, team=self.team).exists())
        self.assertTrue(
            RegisteredCompetitors.objects.filter(
                event=self.event, team=self.team, status="registered").exists())
        self.assertEqual(
            TournamentTeamMember.objects.filter(
                tournament_team__event=self.event, tournament_team__team=self.team).count(),
            len(self.roster),
        )
        self.assertEqual(
            EventTeamInvitation.objects.get(id=invitation_id).status, "accepted")

    def test_inviting_notifies_the_team_and_accepting_notifies_the_inviter(self):
        invitation_id = self._invite()
        # Everybody who MAY answer is told, so the ping never lands only on somebody powerless.
        self.assertTrue(
            Notifications.objects.filter(
                user=self.captain, notification_type="event_team_invitation").exists())
        # The deep link must point at the team page, which is where the Accept card lives.
        # By NAME, not by team_id (corrected 2026-08-08): this assertion used to require
        # str(team_id), which is what the code did and what made the link a 404 in the browser, since
        # /teams/[id] resolves that segment as the team NAME. The old expectation was the bug written
        # down, so it is the expectation that changes here, not the requirement.
        note = Notifications.objects.filter(user=self.captain).first()
        self.assertEqual(note.target_type, "team")
        self.assertEqual(note.target_id, self.team.team_name)

        self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                   self.captain)
        reply = Notifications.objects.filter(
            user=self.admin, notification_type="event_team_invitation_response").first()
        self.assertIsNotNone(reply)
        self.assertIn("accepted", reply.message)
        self.assertEqual(reply.target_type, "event")

    # ══════════════════════════════════════════════════════════════════════════
    # 2. Decline
    # ══════════════════════════════════════════════════════════════════════════
    def test_decline_records_the_reason_and_tells_the_inviter_why(self):
        invitation_id = self._invite()

        res = self._post(_decline_url(invitation_id), {"reason": "Clashes with our league night"},
                         self.captain)
        self.assertEqual(res.status_code, 200, res.content)

        invitation = EventTeamInvitation.objects.get(id=invitation_id)
        self.assertEqual(invitation.status, "declined")
        self.assertEqual(invitation.decline_reason, "Clashes with our league night")
        self.assertEqual(invitation.responded_by_id, self.captain.pk)
        # Declining must NOT register anybody.
        self.assertFalse(TournamentTeam.objects.filter(event=self.event, team=self.team).exists())

        reply = Notifications.objects.filter(
            user=self.admin, notification_type="event_team_invitation_response").first()
        self.assertIn("declined", reply.message)
        self.assertIn("Clashes with our league night", reply.message)

    def test_decline_reason_is_optional(self):
        invitation_id = self._invite()
        res = self._post(_decline_url(invitation_id), {}, self.captain)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation_id).decline_reason, "")

    # ══════════════════════════════════════════════════════════════════════════
    # 3. An invitation is answered ONCE
    # ══════════════════════════════════════════════════════════════════════════
    def test_double_accept_is_refused(self):
        invitation_id = self._invite()
        first = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                           self.captain)
        self.assertEqual(first.status_code, 201, first.content)

        second = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                            self.captain)
        self.assertEqual(second.status_code, 400)
        self.assertIn("already accepted", second.json()["message"])
        # And no second registration was written behind it.
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, team=self.team).count(), 1)

    def test_declined_invitation_cannot_then_be_accepted(self):
        invitation_id = self._invite()
        self._post(_decline_url(invitation_id), {"reason": "no"}, self.captain)
        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         self.captain)
        self.assertEqual(res.status_code, 400)
        self.assertIn("already declined", res.json()["message"])

    # ══════════════════════════════════════════════════════════════════════════
    # 4. register_for_event's refusals come back VERBATIM, and leave the invite open
    # ══════════════════════════════════════════════════════════════════════════
    def test_accept_when_the_event_is_full_returns_the_registration_error(self):
        # Fill the two slots with other teams, then try to accept.
        for label in ("Bravo", "Charlie"):
            other, _, _ = self._team(label)
            TournamentTeam.objects.create(event=self.event, team=other, status="active")

        invitation_id = self._invite()
        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         self.captain)

        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["message"], "Registration limit reached.")
        # The invitation stays PENDING: nothing was accepted, so the team can still accept once a
        # slot frees up instead of having to be invited all over again.
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation_id).status, "pending")
        self.assertFalse(TournamentTeam.objects.filter(event=self.event, team=self.team).exists())

    def test_accept_when_registration_is_closed_returns_the_registration_error(self):
        invitation_id = self._invite()
        # Close the window AFTER the invitation was sent, which is exactly the real-world case.
        self.event.registration_end_date = timezone.localdate() - datetime.timedelta(days=1)
        self.event.save(update_fields=["registration_end_date"])

        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         self.captain)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["message"], "Registration is closed.")
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation_id).status, "pending")

    def test_accept_with_an_incomplete_roster_returns_the_registration_error(self):
        invitation_id = self._invite()
        res = self._post(_accept_url(invitation_id),
                         {"roster_member_ids": self._roster_ids()[:2]}, self.captain)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["message"], "Roster must contain 4 to 6 players.")
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation_id).status, "pending")

    def test_accept_when_the_team_is_already_registered_returns_the_registration_error(self):
        invitation_id = self._invite()
        # Registered through some other door (self-registration, an admin add) after being invited.
        TournamentTeam.objects.create(event=self.event, team=self.team, status="active")

        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         self.captain)

        # THE EQUIVALENCE, now PROVEN rather than restated. This assertion used to be a second
        # hardcoded copy of the sentence, which meant the test could only ever confirm that the
        # string had not changed - it could not notice the two doors drifting apart, which is the
        # thing that actually matters. So the same captain also self-registers the same team for
        # the same event, straight through register_for_event, and the two answers are compared.
        # If anybody ever gives invited teams their own refusal path, this fails, whatever wording
        # either side happens to use.
        direct = self.client.post(
            "/events/register-for-event/",
            data=json.dumps({
                "event_id": self.event.event_id,
                "team_id": self.team.team_id,
                "roster_member_ids": self._roster_ids(),
            }),
            content_type="application/json",
            **self._auth(self.captain),
        )
        self.assertEqual(
            (res.status_code, res.json()["message"]),
            (direct.status_code, direct.json()["message"]),
            "An invited team must be refused in exactly the words a self-registering team is.",
        )

        # And the wording itself, pinned, because the module docstring above quotes it verbatim.
        # It used to be 400 "You cannot rejoin this event." - the quirk this file documented when
        # the feature was built, where register_for_event compared a TournamentTeam's status against
        # "registered" (a RegisteredCompetitors value no TournamentTeam ever holds) and so told
        # every already-registered team it had tried to rejoin. Fixed on its own terms
        # (views._existing_team_registration_refusal, 2026-08-06); the answer is now the 409 this
        # file always said it should be.
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["message"], "This team is already registered for this event.")
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation_id).status, "pending")
        self.assertEqual(
            TournamentTeam.objects.filter(event=self.event, team=self.team).count(), 1)

    # ══════════════════════════════════════════════════════════════════════════
    # 5. Permissions
    # ══════════════════════════════════════════════════════════════════════════
    def test_a_plain_player_cannot_accept(self):
        invitation_id = self._invite()
        plain_player = self.roster[1]  # management_role "member": may play, may not register
        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         plain_player)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation_id).status, "pending")

    def test_somebody_outside_the_team_cannot_accept_or_decline(self):
        invitation_id = self._invite()
        self.assertEqual(
            self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                       self.outsider).status_code, 403)
        self.assertEqual(
            self._post(_decline_url(invitation_id), {"reason": "lol"}, self.outsider).status_code,
            403)

    def test_a_non_organizer_cannot_invite(self):
        res = self._post(
            CREATE_URL, {"event_id": self.event.event_id, "team_ids": [self.team.team_id]},
            self.outsider,
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(EventTeamInvitation.objects.count(), 0)

    def test_a_team_cannot_read_another_teams_invitations(self):
        self._invite()
        other, other_owner, _ = self._team("Delta")
        res = self.client.get(
            f"{MINE_URL}?team_id={self.team.team_id}", **self._auth(other_owner))
        self.assertEqual(res.status_code, 403)

    # ══════════════════════════════════════════════════════════════════════════
    # 6. Creating invitations: batching, skips, cancellation
    # ══════════════════════════════════════════════════════════════════════════
    def test_several_teams_are_invited_in_one_call(self):
        bravo, _, _ = self._team("Bravo")
        charlie, _, _ = self._team("Charlie")
        res = self._post(
            CREATE_URL,
            {"event_id": self.event.event_id,
             "team_ids": [self.team.team_id, bravo.team_id, charlie.team_id]},
            self.admin,
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(len(res.json()["invited"]), 3)
        self.assertEqual(EventTeamInvitation.objects.filter(status="pending").count(), 3)

    def test_an_already_registered_team_is_skipped_with_a_reason(self):
        TournamentTeam.objects.create(event=self.event, team=self.team, status="active")
        bravo, _, _ = self._team("Bravo")

        res = self._post(
            CREATE_URL,
            {"event_id": self.event.event_id, "team_ids": [self.team.team_id, bravo.team_id]},
            self.admin,
        )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        # The batch is NOT failed by one bad entry: the other team is still invited.
        self.assertEqual([t["team_id"] for t in body["invited"]], [bravo.team_id])
        self.assertEqual(body["skipped"][0]["reason"], "already_registered")

    def test_a_second_pending_invitation_to_the_same_team_is_skipped(self):
        self._invite()
        res = self._post(
            CREATE_URL, {"event_id": self.event.event_id, "team_ids": [self.team.team_id]},
            self.admin,
        )
        self.assertEqual(res.json()["skipped"][0]["reason"], "already_invited")
        self.assertEqual(EventTeamInvitation.objects.filter(status="pending").count(), 1)

    def test_declining_frees_the_team_to_be_invited_again(self):
        # The uniqueness rule is "one PENDING invitation", not "one ever": an organizer must be able
        # to ask again next season (or after fixing whatever the team objected to).
        first = self._invite()
        self._post(_decline_url(first), {"reason": "not this time"}, self.captain)
        second = self._invite()
        self.assertNotEqual(first, second)

    def test_cancel_takes_back_a_pending_invitation(self):
        invitation_id = self._invite()
        res = self._post(_cancel_url(invitation_id), {}, self.admin)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(EventTeamInvitation.objects.get(id=invitation_id).status, "cancelled")

        # A cancelled invitation is dead: the team can no longer accept it.
        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         self.captain)
        self.assertEqual(res.status_code, 400)
        self.assertIn("already cancelled", res.json()["message"])

    def test_cancel_after_acceptance_is_refused(self):
        invitation_id = self._invite()
        self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                   self.captain)
        res = self._post(_cancel_url(invitation_id), {}, self.admin)
        self.assertEqual(res.status_code, 400)

    def test_teams_cannot_be_invited_to_a_solo_event(self):
        solo = Event.objects.create(
            event_name="Solo Cup", slug="solo-cup", participant_type="solo",
            competition_type="tournament", event_type="virtual", max_teams_or_players=50,
            is_public=True, is_draft=False, number_of_stages=1,
            start_date=timezone.localdate() + datetime.timedelta(days=7),
            end_date=timezone.localdate() + datetime.timedelta(days=7),
            registration_open_date=timezone.localdate() - datetime.timedelta(days=1),
            registration_end_date=timezone.localdate() + datetime.timedelta(days=3),
        )
        res = self._post(
            CREATE_URL, {"event_id": solo.event_id, "team_ids": [self.team.team_id]}, self.admin)
        self.assertEqual(res.status_code, 400)

    # ══════════════════════════════════════════════════════════════════════════
    # 7. The two list surfaces
    # ══════════════════════════════════════════════════════════════════════════
    def test_organizer_list_shows_status_and_the_decline_reason(self):
        invitation_id = self._invite()
        self._post(_decline_url(invitation_id), {"reason": "roster is thin"}, self.captain)

        res = self.client.get(f"{LIST_URL}?event_id={self.event.event_id}", **self._auth(self.admin))
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["counts"]["declined"], 1)
        self.assertEqual(body["invitations"][0]["decline_reason"], "roster is thin")
        self.assertEqual(body["invitations"][0]["team_name"], self.team.team_name)
        self.assertEqual(body["total_count"], 1)

    def test_team_list_carries_what_a_captain_needs_to_decide(self):
        self._invite()
        res = self.client.get(f"{MINE_URL}?team_id={self.team.team_id}", **self._auth(self.captain))
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body["can_respond"])
        self.assertEqual(body["pending_count"], 1)
        row = body["invitations"][0]
        self.assertEqual(row["event_name"], "Invitational Cup")
        self.assertEqual(row["event_slug"], "invitational-cup")
        self.assertTrue(row["registration_open"])
        self.assertFalse(row["team_registered"])
        self.assertEqual(row["message"], "Come play")

    def test_a_plain_player_sees_the_invitation_but_cannot_respond(self):
        self._invite()
        res = self.client.get(f"{MINE_URL}?team_id={self.team.team_id}",
                              **self._auth(self.roster[1]))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()["can_respond"])
        self.assertEqual(len(res.json()["invitations"]), 1)

    def test_an_expired_invitation_is_swept_on_read_and_cannot_be_accepted(self):
        invitation_id = self._invite()
        EventTeamInvitation.objects.filter(id=invitation_id).update(
            expires_at=timezone.now() - datetime.timedelta(hours=1))

        res = self.client.get(f"{MINE_URL}?team_id={self.team.team_id}", **self._auth(self.captain))
        self.assertEqual(res.json()["invitations"][0]["status"], "expired")

        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         self.captain)
        self.assertEqual(res.status_code, 400)
        self.assertIn("already expired", res.json()["message"])

    # ══════════════════════════════════════════════════════════════════════════
    # 8. Private events: the invitation carries its own token
    # ══════════════════════════════════════════════════════════════════════════
    def test_a_private_event_invitation_can_actually_be_accepted(self):
        # register_for_event demands an EventInviteToken when the event is private. Creating the
        # invitation mints one, so the team never has to hunt for a link the organizer sent
        # somewhere else - and the existing gate is satisfied rather than skipped.
        self.event.is_public = False
        self.event.save(update_fields=["is_public"])

        invitation_id = self._invite()
        self.assertIsNotNone(EventTeamInvitation.objects.get(id=invitation_id).invite_token_id)

        res = self._post(_accept_url(invitation_id), {"roster_member_ids": self._roster_ids()},
                         self.captain)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(TournamentTeam.objects.filter(event=self.event, team=self.team).exists())

    def test_cancelling_a_private_invitation_destroys_its_token(self):
        self.event.is_public = False
        self.event.save(update_fields=["is_public"])
        invitation_id = self._invite()
        token_id = EventTeamInvitation.objects.get(id=invitation_id).invite_token_id

        self._post(_cancel_url(invitation_id), {}, self.admin)

        from .models import EventInviteToken
        self.assertFalse(EventInviteToken.objects.filter(id=token_id).exists())
