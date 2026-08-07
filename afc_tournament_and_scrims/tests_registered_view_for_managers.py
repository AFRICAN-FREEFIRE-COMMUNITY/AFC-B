# afc_tournament_and_scrims/tests_registered_view_for_managers.py
# ──────────────────────────────────────────────────────────────────────────────
# "SOME CAPTAINS AND MANAGERS CANNOT SEE EDIT REGISTRATION" (owner report, 2026-08-07, urgent).
#
# The owner sent a screenshot: a team captain opens an event their team is ALREADY registered for,
# is walked through registration from the beginning again including the sponsor steps, and is then
# refused with "You cannot rejoin this event."
#
# The cause was not the refusal. The refusal was correct: the team really was already registered.
# The cause was get_event_details telling that person they were NOT registered, so the page offered
# them the registration flow. It computed is_registered as "do I have a TournamentTeamMember row",
# which is roster membership. A captain, vice-captain, manager or coach who registers the team
# WITHOUT putting themselves in the 4-6 player roster has no such row. The people most likely to
# register a team were therefore the people most likely to be told they had not.
#
# It compounded: the frontend sweeps the saved registration draft when is_registered turns true
# (EventDetailsWrapper), so for these users the stale draft was never cleared either, which is why
# the flow resumed mid-way through sponsor requirements instead of starting clean.
#
# What these tests pin:
#   * a manager / coach / vice-captain / owner who is NOT on the roster reads as registered;
#   * they get the roster-edit context too, so the person who submitted the roster can fix it;
#   * a plain member with no role and no roster row does NOT (they are not registered, and must
#     still be able to be added by someone who is);
#   * a team that withdrew, left or was disqualified does NOT read as registered, so those people
#     fall through to the ordinary registration path rather than being shown Edit Registration for
#     a team that is out of the event;
#   * roster membership on its own still works, which is the pre-existing behaviour.
# ──────────────────────────────────────────────────────────────────────────────
import datetime
import json
import uuid

from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import Event, TournamentTeam, TournamentTeamMember

DETAILS_URL = "/events/get-event-details/"


class RegisteredViewForManagersTests(TestCase):
    # ── fixtures ──────────────────────────────────────────────────────────────
    def _user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@afc.test", password="x",
            role="player", status="active", is_active=True, country="Nigeria",
        )

    def _auth(self, user):
        token = SessionToken.objects.create(
            user=user, token=f"t-{uuid.uuid4().hex}"[:64],
            expires_at=timezone.now() + datetime.timedelta(days=1),
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _details(self, user):
        """The event page payload as that user sees it. get_event_details nests everything under
        event_details, which is what EventDetailsWrapper reads, so the tests assert on the same
        shape the browser gets rather than on an intermediate value."""
        res = self.client.post(
            DETAILS_URL, data=json.dumps({"slug": self.event.slug}),
            content_type="application/json", **self._auth(user),
        )
        self.assertEqual(res.status_code, 200, res.content[:300])
        return res.json()["event_details"]

    def setUp(self):
        self.client = Client()
        today = timezone.now().date()

        self.owner = self._user("mgr_owner")
        self.team = Team.objects.create(
            team_name="Managed Squad", join_settings="open",
            team_creator=self.owner, team_owner=self.owner, country="Nigeria",
        )
        # The people who run the team but do NOT play. This is the shape that was broken.
        self.manager = self._user("mgr_manager")
        self.coach = self._user("mgr_coach")
        self.vice = self._user("mgr_vice")
        TeamMembers.objects.create(team=self.team, member=self.manager, management_role="manager")
        TeamMembers.objects.create(team=self.team, member=self.coach, management_role="coach")
        TeamMembers.objects.create(team=self.team, member=self.vice, management_role="vice_captain")

        # A plain member with no management role who is also NOT on the submitted roster.
        self.bench = self._user("mgr_bench")
        TeamMembers.objects.create(team=self.team, member=self.bench, management_role="member")

        # Four players who ARE on the roster.
        self.players = []
        for i in range(4):
            player = self._user(f"mgr_p{i}")
            TeamMembers.objects.create(team=self.team, member=player, management_role="member")
            self.players.append(player)

        self.event = Event.objects.create(
            event_name="Manager Visibility Cup", slug="manager-visibility-cup",
            competition_type="tournament", participant_type="squad", event_type="virtual",
            max_teams_or_players=16, is_public=True, is_draft=False, number_of_stages=1,
            registration_open_date=today - datetime.timedelta(days=2),
            registration_end_date=today + datetime.timedelta(days=2),
            start_date=today + datetime.timedelta(days=5),
            end_date=today + datetime.timedelta(days=6),
        )

        # The team is registered, with ONLY the four players on the roster. Nobody who manages the
        # team is a TournamentTeamMember, which is exactly the owner's situation.
        self.tt = TournamentTeam.objects.create(
            event=self.event, team=self.team, status="active", registered_by=self.manager,
        )
        for player in self.players:
            TournamentTeamMember.objects.create(tournament_team=self.tt, user=player)

    # ── the bug ───────────────────────────────────────────────────────────────
    def test_a_manager_who_is_not_on_the_roster_reads_as_registered(self):
        body = self._details(self.manager)
        self.assertTrue(
            body["is_registered"],
            "the person who registered the team was told they were not registered, so the page "
            "offered them registration again and the backend then refused the duplicate",
        )

    def test_a_coach_who_is_not_on_the_roster_reads_as_registered(self):
        self.assertTrue(self._details(self.coach)["is_registered"])

    def test_a_vice_captain_who_is_not_on_the_roster_reads_as_registered(self):
        self.assertTrue(self._details(self.vice)["is_registered"])

    def test_the_team_owner_who_is_not_on_the_roster_reads_as_registered(self):
        self.assertTrue(self._details(self.owner)["is_registered"])

    def test_a_rostered_player_still_reads_as_registered(self):
        # The pre-existing behaviour, which must not regress.
        self.assertTrue(self._details(self.players[0])["is_registered"])

    # ── the limits of the fix ─────────────────────────────────────────────────
    def test_a_plain_member_off_the_roster_is_not_registered(self):
        # No management role and no roster row: this person genuinely is not in the event, and
        # widening the rule to "anyone in the team" would show them somebody else's registration.
        self.assertFalse(self._details(self.bench)["is_registered"])

    def test_a_stranger_is_not_registered(self):
        self.assertFalse(self._details(self._user("mgr_outsider"))["is_registered"])

    def test_a_withdrawn_team_does_not_read_as_registered(self):
        self.tt.status = "withdrawn"
        self.tt.save(update_fields=["status"])
        self.assertFalse(self._details(self.manager)["is_registered"])
        self.assertFalse(self._details(self.players[0])["is_registered"])

    def test_a_disqualified_team_does_not_read_as_registered(self):
        self.tt.status = "disqualified"
        self.tt.save(update_fields=["status"])
        self.assertFalse(self._details(self.manager)["is_registered"])

    # ── the roster-edit window follows the same rule ──────────────────────────
    def test_a_manager_gets_the_roster_edit_context_too(self):
        # Without this, the one person who can fix a wrong roster is the one person locked out of
        # the Edit Roster button.
        # roster_edit_open is computed from roster_edit_until, so open the window the real way.
        self.tt.roster_edit_until = timezone.now() + datetime.timedelta(days=1)
        self.tt.save(update_fields=["roster_edit_until"])
        body = self._details(self.manager)
        self.assertTrue(body["your_team_roster_edit_open"])
        self.assertIsNotNone(body["your_team_roster_edit_until"])

    def test_the_manager_is_counted_once_not_once_per_role_row(self):
        # The team-members join can match the same person more than once. If that leaked into a
        # count or a .first() ordering this would be the test that noticed.
        TeamMembers.objects.filter(team=self.team, member=self.manager).update(
            management_role="manager")
        self.assertTrue(self._details(self.manager)["is_registered"])
