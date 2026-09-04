# afc_tournament_and_scrims/test_registration_settles_invitation.py
# ──────────────────────────────────────────────────────────────────────────────────────────────
# REGISTERING IS ACCEPTING.
#
# THE BUG THESE EXIST FOR, and it was mine. On 2026-09-02 the Accept dialog on the team page was
# deleted and Accept became a link to the EVENT PAGE, because only the event page can resolve a
# refusal. The event page registers through register_for_event, which knew nothing about
# invitations. So the answer was never recorded, and the owner opened a live event the next day to
# find "Accepted 0" over eight pending rows while three of those eight teams sat in Registered
# Teams on the same screen.
#
# The contract was already written down, in accept_team_invitation's own docstring: "Accepting IS
# registering." Only the inverse had never been wired.
#
# WHAT IS PINNED HERE. That a direct registration settles the row, that it settles the RIGHT row,
# that the accept endpoint does not settle it twice (one answer, one notification, one fcfs place),
# and that a refused registration leaves the invitation pending so it can still be answered.
# ──────────────────────────────────────────────────────────────────────────────────────────────
import datetime
import json
import uuid
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import Notifications, SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import Event, EventTeamInvitation, RegisteredCompetitors

REGISTER_URL = "/events/register-for-event/"


class RegistrationSettlesInvitationTests(TestCase):
    def setUp(self):
        self.client = Client()
        today = timezone.localdate()
        self.event = Event.objects.create(
            event_name="Settle Cup", slug="settle-cup",
            competition_type="tournament", participant_type="squad", event_type="virtual",
            event_mode="br", max_teams_or_players=16, number_of_stages=1,
            is_public=True, is_draft=False,
            start_date=today + datetime.timedelta(days=7),
            end_date=today + datetime.timedelta(days=8),
            registration_open_date=today - datetime.timedelta(days=1),
            registration_end_date=today + datetime.timedelta(days=5),
        )
        self.organizer = self._user("settle_organizer")
        self.owner = self._user("settle_owner")
        self.team = Team.objects.create(
            team_name="Settle Team", join_settings="open",
            team_creator=self.owner, team_owner=self.owner, country="Nigeria",
        )
        TeamMembers.objects.create(team=self.team, member=self.owner,
                                   management_role="team_captain")
        self.roster = [self.owner]
        for i in range(3):
            player = self._user(f"settle_p{i}")
            TeamMembers.objects.create(team=self.team, member=player, management_role="member")
            self.roster.append(player)

    # ── fixture helpers ───────────────────────────────────────────────────────────────────────
    def _user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria",
        )

    def _auth(self, user):
        token = SessionToken.objects.create(
            user=user, token=f"st-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _register(self, user, **extra):
        body = {
            "event_id": self.event.event_id,
            "team_id": self.team.team_id,
            "roster_member_ids": [u.user_id for u in self.roster],
            **extra,
        }
        return self.client.post(REGISTER_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth(user))

    def _invite(self, team=None, user=None):
        return EventTeamInvitation.objects.create(
            event=self.event, team=team if user is None else None, user=user,
            status="pending", invited_by=self.organizer,
            message="We saved you a slot.",
        )

    def _registered(self):
        return RegisteredCompetitors.objects.filter(
            event=self.event, team=self.team).exists()

    # ── the reported bug ──────────────────────────────────────────────────────────────────────
    def test_registering_from_the_event_page_settles_the_invitation(self):
        invitation = self._invite(team=self.team)
        res = self._register(self.owner)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(self._registered())

        invitation.refresh_from_db()
        # THE ASSERTION THE OWNER WOULD MAKE: the panel must not say "pending" about a team that
        # is visibly in the event.
        self.assertEqual(invitation.status, "accepted")
        self.assertEqual(invitation.responded_by_id, self.owner.user_id)
        self.assertIsNotNone(invitation.responded_at)

    def test_the_inviter_is_told(self):
        self._invite(team=self.team)
        self._register(self.owner)
        notes = Notifications.objects.filter(
            user=self.organizer, notification_type="event_team_invitation_response")
        self.assertEqual(notes.count(), 1, "the organizer should be told exactly once")
        self.assertIn("accepted", notes.first().message)

    def test_it_settles_only_the_invitation_addressed_to_this_team(self):
        other_owner = self._user("settle_other_owner")
        other = Team.objects.create(
            team_name="Other Settle Team", join_settings="open",
            team_creator=other_owner, team_owner=other_owner, country="Nigeria")
        theirs = self._invite(team=other)
        mine = self._invite(team=self.team)

        self._register(self.owner)

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.status, "accepted")
        # A team that has not registered has not answered. If this ever flips, the panel starts
        # lying in the opposite direction, which is no better than the bug it replaced.
        self.assertEqual(theirs.status, "pending")

    def test_a_refused_registration_leaves_it_pending(self):
        # Registration closed yesterday: the team cannot get in, so it has not answered and must
        # still be able to answer later.
        self.event.registration_end_date = timezone.localdate() - datetime.timedelta(days=1)
        self.event.save(update_fields=["registration_end_date"])
        invitation = self._invite(team=self.team)

        res = self._register(self.owner)
        self.assertGreaterEqual(res.status_code, 400, res.content)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "pending")

    def test_accepting_through_the_accept_endpoint_settles_it_exactly_once(self):
        # The accept endpoint registers by calling register_for_event, so without the guard flag
        # the row would be settled twice: two notifications for one answer, and on an fcfs
        # campaign two places burned.
        invitation = self._invite(team=self.team)
        res = self.client.post(
            f"/events/team-invitations/{invitation.id}/accept/",
            data=json.dumps({"roster_member_ids": [u.user_id for u in self.roster]}),
            content_type="application/json", **self._auth(self.owner))
        self.assertEqual(res.status_code, 201, res.content)

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "accepted")
        self.assertEqual(
            Notifications.objects.filter(
                user=self.organizer,
                notification_type="event_team_invitation_response").count(),
            1,
            "one answer must produce one notification, not one per code path",
        )


class SoloRegistrationSettlesInvitationTests(TestCase):
    """The solo shape. A solo invitation addresses the PLAYER, and a solo player has no team page,
    so the event page is the only door they have. Same contract, different key."""

    def setUp(self):
        self.client = Client()
        today = timezone.localdate()
        self.event = Event.objects.create(
            event_name="Solo Settle Cup", slug="solo-settle-cup",
            competition_type="tournament", participant_type="solo", event_type="virtual",
            event_mode="br", max_teams_or_players=48, number_of_stages=1,
            is_public=True, is_draft=False,
            start_date=today + datetime.timedelta(days=7),
            end_date=today + datetime.timedelta(days=8),
            registration_open_date=today - datetime.timedelta(days=1),
            registration_end_date=today + datetime.timedelta(days=5),
        )
        self.organizer = User.objects.create_user(
            username="solo_settle_org", email="solo_settle_org@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria")
        # A solo registration requires a connected Discord account (views.register_for_event,
        # "Connect your Discord account first"), so the fixture satisfies that gate rather than
        # working around it. Everything this test asserts happens after it.
        self.player = User.objects.create_user(
            username="solo_settle_player", email="solo_settle_player@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria",
            discord_connected=True, discord_id="123456789012345678")

    def test_a_solo_player_registering_settles_their_own_invitation(self):
        invitation = EventTeamInvitation.objects.create(
            event=self.event, user=self.player, status="pending", invited_by=self.organizer)
        token = SessionToken.objects.create(
            user=self.player, token=f"st-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1)).token
        # The solo branch calls the live Discord API to confirm server membership. Patched WHERE
        # IT IS USED (afc_tournament_and_scrims.views), not where it is defined, because the name
        # is bound in that module at import: patching the definition site would leave the real
        # call running and the assertion below would pass or fail for the wrong reason.
        with patch("afc_tournament_and_scrims.views.check_discord_membership", return_value=True):
            res = self.client.post(
                REGISTER_URL, data=json.dumps({"event_id": self.event.event_id}),
                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(res.status_code, 201, res.content)

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "accepted")


class BackfillCommandTests(TestCase):
    """The repair for rows that are ALREADY wrong.

    The fix above only helps registrations made from now on. The owner is looking at an event where
    three teams registered days ago, so `settle_accepted_invitations` walks the pending rows and
    settles the ones whose invitee is demonstrably in the event.

    THE FIXTURE MATTERS HERE. Run against the July clone the command reported "0 would be settled",
    which proves nothing at all: that database has no such rows. A command whose only evidence is a
    zero on empty data is a command nobody has tested."""

    def setUp(self):
        today = timezone.localdate()
        self.event = Event.objects.create(
            event_name="Backfill Cup", slug="backfill-cup",
            competition_type="tournament", participant_type="squad", event_type="virtual",
            event_mode="br", max_teams_or_players=16, number_of_stages=1,
            is_public=True, is_draft=False,
            start_date=today + datetime.timedelta(days=3),
            end_date=today + datetime.timedelta(days=4),
            registration_open_date=today - datetime.timedelta(days=1),
            registration_end_date=today + datetime.timedelta(days=2),
        )
        self.owner = User.objects.create_user(
            username="backfill_owner", email="backfill_owner@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria")

    def _team(self, name):
        return Team.objects.create(team_name=name, join_settings="open",
                                   team_creator=self.owner, team_owner=self.owner,
                                   country="Nigeria")

    def test_it_settles_a_team_that_is_already_registered_and_leaves_the_others(self):
        from io import StringIO

        from django.core.management import call_command

        registered = self._team("Backfill In")
        absent = self._team("Backfill Out")
        withdrawn = self._team("Backfill Gone")

        in_row = EventTeamInvitation.objects.create(
            event=self.event, team=registered, status="pending")
        out_row = EventTeamInvitation.objects.create(
            event=self.event, team=absent, status="pending")
        gone_row = EventTeamInvitation.objects.create(
            event=self.event, team=withdrawn, status="pending")

        RegisteredCompetitors.objects.create(
            event=self.event, team=registered, status="registered")
        # A team that left is NOT in the event, so recording "accepted" would be a false record.
        RegisteredCompetitors.objects.create(
            event=self.event, team=withdrawn, status="withdrawn")

        out = StringIO()
        call_command("settle_accepted_invitations", "--event", str(self.event.event_id),
                     "--apply", stdout=out)

        for row in (in_row, out_row, gone_row):
            row.refresh_from_db()
        self.assertEqual(in_row.status, "accepted")
        self.assertEqual(out_row.status, "pending")
        self.assertEqual(gone_row.status, "pending")
        self.assertIn("Settled 1", out.getvalue())

    def test_a_dry_run_writes_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        team = self._team("Backfill Dry")
        row = EventTeamInvitation.objects.create(event=self.event, team=team, status="pending")
        RegisteredCompetitors.objects.create(event=self.event, team=team, status="registered")

        out = StringIO()
        call_command("settle_accepted_invitations", "--event", str(self.event.event_id),
                     stdout=out)

        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("1 invitation(s) would be settled", out.getvalue())

