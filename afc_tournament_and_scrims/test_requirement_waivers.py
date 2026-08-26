"""
Tests for EVENT REQUIREMENT WAIVERS (owner 2026-08-26).

WHY the feature exists: AFC invites a team it wants in an event, and that team is judged by the same
requirements as anyone else (event_invites replays the accept through register_for_event on
purpose). Sometimes AFC wants them in anyway. A waiver is how that happens ON THE RECORD instead of
by an admin quietly force-adding the team through a bypass.

WHY THE CONSTRAINT LOOKS ODD, and do not "simplify" it back:
The natural way to say "one ACTIVE waiver per (event, team)" is a partial index,
UniqueConstraint(condition=Q(revoked_at__isnull=True)). MySQL has no partial indexes, Django
SILENTLY SKIPS such a constraint, and the guarantee becomes a comment pretending to be a constraint.
This project has already been bitten twice: see the EventTeamInvitation.Meta comment in models.py,
and tests_letter_constraint.py, which exists because TournamentTeam.assigned_letter SHIPPED with a
condition= that gave zero enforcement in production. The workaround here is that file's: a PLAIN
unique constraint over a NULLABLE marker column. Revoking sets active=None, and because MySQL allows
any number of NULLs inside a unique index, revoked rows never collide while at most one live row can
exist. These tests run against the real (MySQL) test database, so they exercise the engine behaviour
the design depends on.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_tournament_and_scrims.test_requirement_waivers
"""
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User, UserProfile
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims import waivers
from afc_tournament_and_scrims.models import Event, EventRequirementWaiver


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _event(creator, **overrides):
    fields = dict(
        event_name="Waiver Cup", competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


def _team(name, owner):
    return Team.objects.create(team_name=name, team_owner=owner, team_creator=owner)


class WaiverConstraintTests(TestCase):
    def setUp(self):
        self.admin, _ = _user("wvadmin", role="admin")
        self.captain, _ = _user("wvcaptain")
        self.event = _event(self.admin)
        self.team = _team("Waived FC", self.captain)

    def _waiver(self, **overrides):
        fields = dict(
            event=self.event, team=self.team, waived_codes=["team_logo_required"],
            reason="Invited by AFC", created_by=self.admin,
        )
        fields.update(overrides)
        return EventRequirementWaiver.objects.create(**fields)

    def test_two_active_waivers_for_one_team_are_refused_by_the_database(self):
        self._waiver()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._waiver()

    def test_a_revoked_waiver_does_not_block_a_new_one(self):
        first = self._waiver()
        first.active = None
        first.revoked_at = timezone.now()
        first.revoked_by = self.admin
        first.save()
        self._waiver()  # must not raise
        self.assertEqual(EventRequirementWaiver.objects.filter(event=self.event).count(), 2)

    def test_many_revoked_waivers_coexist(self):
        """The whole point of the nullable marker: MySQL treats NULLs as distinct inside a unique
        index, so a team can be waived and revoked any number of times."""
        for _ in range(3):
            row = self._waiver()
            row.active = None
            row.revoked_at = timezone.now()
            row.save()
        self.assertEqual(EventRequirementWaiver.objects.filter(active=None).count(), 3)

    def test_the_same_team_in_a_different_event_is_fine(self):
        self._waiver()
        other = _event(self.admin, event_name="Other Waiver Cup")
        self._waiver(event=other)
        self.assertEqual(EventRequirementWaiver.objects.count(), 2)

    def test_a_solo_waiver_uses_the_user_column(self):
        player, _ = _user("wvsolo")
        solo_event = _event(self.admin, participant_type="solo", event_name="Solo Waiver Cup")
        EventRequirementWaiver.objects.create(
            event=solo_event, user=player, waived_codes=["uid"],
            reason="Legacy account", created_by=self.admin,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                EventRequirementWaiver.objects.create(
                    event=solo_event, user=player, waived_codes=["uid"],
                    reason="again", created_by=self.admin,
                )


class WaivableCodeTests(TestCase):
    def setUp(self):
        self.admin, _ = _user("wvcadmin", role="admin")
        self.captain, _ = _user("wvccaptain")
        self.event = _event(self.admin, event_name="Codes Cup")
        self.team = _team("Codes FC", self.captain)

    def test_bans_and_payment_can_never_be_waived(self):
        for code in ["team_banned", "player_banned", "payment_required", "paid_terms_required"]:
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    waivers.clean_codes([code])

    def test_a_waivable_code_is_accepted_and_deduplicated(self):
        self.assertEqual(
            waivers.clean_codes(["team_logo_required", "team_logo_required"]),
            ["team_logo_required"],
        )

    def test_an_unknown_code_is_refused(self):
        with self.assertRaises(ValueError):
            waivers.clean_codes(["make_us_win"])

    def test_no_waiver_means_an_empty_set_rather_than_none(self):
        self.assertEqual(waivers.waived_codes(self.event, team=self.team), set())

    def test_an_active_waiver_is_returned(self):
        waivers.grant(
            self.event, actor=self.admin, reason="Invited by AFC",
            codes=["team_logo_required"], team=self.team,
        )
        self.assertEqual(
            waivers.waived_codes(self.event, team=self.team), {"team_logo_required"}
        )

    def test_a_revoked_waiver_stops_applying(self):
        waiver = waivers.grant(
            self.event, actor=self.admin, reason="Invited by AFC",
            codes=["team_logo_required"], team=self.team,
        )
        waivers.revoke(waiver, actor=self.admin)
        self.assertEqual(waivers.waived_codes(self.event, team=self.team), set())

    def test_granting_twice_edits_the_existing_row_rather_than_stacking(self):
        waivers.grant(self.event, actor=self.admin, reason="First",
                      codes=["team_logo_required"], team=self.team)
        waivers.grant(self.event, actor=self.admin, reason="Second",
                      codes=["capacity_full"], team=self.team)
        rows = EventRequirementWaiver.objects.filter(
            event=self.event, team=self.team, active=True
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().waived_codes, ["capacity_full"])

    def test_a_reason_is_required(self):
        with self.assertRaises(ValueError):
            waivers.grant(self.event, actor=self.admin, reason="   ",
                          codes=["team_logo_required"], team=self.team)

    def test_naming_neither_a_team_nor_a_user_is_refused(self):
        """The rule a CheckConstraint cannot be trusted to enforce on MySQL."""
        with self.assertRaises(ValueError):
            waivers.grant(self.event, actor=self.admin, reason="nobody",
                          codes=["team_logo_required"])


class WaiverAtRegistrationTests(TestCase):
    """The waiver has to work through the ORDINARY registration endpoint, because that is the one an
    invited team's accept replays into (event_invites._register_through_the_normal_path). If it only
    worked in a special admin path, invited teams would be judged by different rules again, which is
    the exact thing this codebase went out of its way to avoid."""

    def setUp(self):
        self.admin, _ = _user("wvradmin", role="admin")
        self.captain, self.captain_token = _user("wvrcaptain")
        self.mates = [_user(f"wvrmate{i}")[0] for i in range(3)]
        self.team = _team("No Logo FC", self.captain)
        TeamMembers.objects.create(team=self.team, member=self.captain)
        for mate in self.mates:
            TeamMembers.objects.create(team=self.team, member=mate)
        self.event = _event(self.admin, require_team_logo=True, event_name="Logo Gate Cup")

    def _register(self):
        return Client().post(
            "/events/register-for-event/",
            {
                "event_id": self.event.event_id,
                "team_id": self.team.team_id,
                "roster_member_ids": [self.captain.user_id] + [m.user_id for m in self.mates],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.captain_token}",
        )

    def test_without_a_waiver_the_team_is_refused(self):
        resp = self._register()
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(resp.json()["code"], "team_logo_required")

    def test_with_a_waiver_the_logo_gate_lets_the_team_past(self):
        waivers.grant(self.event, actor=self.admin, reason="Invited by AFC",
                      codes=["team_logo_required"], team=self.team)
        resp = self._register()
        self.assertNotEqual(
            resp.json().get("code"), "team_logo_required",
            "the waived gate must not be the one refusing",
        )

    def test_a_waiver_for_a_different_code_does_not_help(self):
        waivers.grant(self.event, actor=self.admin, reason="Wrong one",
                      codes=["capacity_full"], team=self.team)
        self.assertEqual(self._register().json()["code"], "team_logo_required")

    def test_a_waiver_for_a_different_team_does_not_help(self):
        other = _team("Other FC", self.admin)
        waivers.grant(self.event, actor=self.admin, reason="Other team",
                      codes=["team_logo_required"], team=other)
        self.assertEqual(self._register().json()["code"], "team_logo_required")

    def test_a_banned_team_is_still_refused_even_with_every_code_waived(self):
        """Bans are outside the vocabulary entirely, so this proves the refusal is structural and
        not merely a code an admin forgot to tick."""
        waivers.grant(self.event, actor=self.admin, reason="Everything",
                      codes=sorted(waivers.WAIVABLE_CODES), team=self.team)
        self.team.is_banned = True
        self.team.save()
        resp = self._register()
        self.assertEqual(resp.status_code, 403)
        self.assertIn("banned", resp.json()["message"].lower())
