"""
add_teams_to_event: bans still refuse, plus the waiver API.

THE RULE CHANGED ON 2026-08-27 (owner): "if an organizer or admin is adding them, they
automatically bypass those requirements. Invitations is different." A direct add is a human
decision by the person the requirements exist to serve, so requirements no longer refuse it. An
invited team still registers ITSELF through register_for_event, which enforces everything.

WHAT THIS FILE STILL COVERS: the things that did NOT change. Bans still refuse. An existing waiver
is still honoured. The waiver API is unchanged.

THE NEW RULE IS COVERED IN test_bulk_add_reasons.py, deliberately in one place rather than split
across two files. Two tests that used to live here (a requirement refusing an add, and a waive
needing a reason) were REMOVED rather than adjusted, because they pinned behaviour the owner has
since reversed. Keeping them "passing" by weakening the assertion would have left a test that
looked like it guarded something it no longer guards.

WHAT IT USED TO DO, worth keeping written down: it wrote RegisteredCompetitors directly, with no
reference anywhere in the function to _missing_registration_assets, is_banned, BannedPlayer or
max_teams_or_players. So bans and capacity were skipped too, silently. Bans are now checked and
a bypassed requirement is now RECORDED, which is the part of that original concern that survives.

Run: AFC_TEST_DB_NAME=test_afc_conn python manage.py test afc_tournament_and_scrims.test_bulk_add_gate
"""
from datetime import date, timedelta

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User, UserProfile
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import (
    Event,
    EventRequirementWaiver,
    RegisteredCompetitors,
)


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
        event_name="Bulk Cup", competition_type="tournament", participant_type="squad",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


class BulkAddGateTests(TestCase):
    def setUp(self):
        self.admin, self.admin_token = _user("bulkadmin", role="admin")
        self.captain, _ = _user("bulkcaptain")
        self.team = Team.objects.create(
            team_name="Bulk FC", team_owner=self.captain, team_creator=self.captain,
        )
        TeamMembers.objects.create(team=self.team, member=self.captain)

    def _add(self, event, **extra):
        return Client().post(
            "/events/add-teams-to-event/",
            {"event_id": event.event_id, "team_ids": [self.team.team_id], **extra},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )

    def test_a_compliant_team_is_still_added_exactly_as_before(self):
        event = _event(self.admin)
        resp = self._add(event)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            RegisteredCompetitors.objects.filter(event=event, team=self.team).exists()
        )

    def test_the_success_response_says_which_checks_it_ran(self):
        """A pass here is NOT the same as passing register_for_event, and the endpoint says so
        rather than letting an admin assume parity."""
        event = _event(self.admin, event_name="Bulk Cup Checks")
        self.assertIn("checks_run", self._add(event).json())

    def test_a_direct_add_over_a_requirement_writes_a_real_waiver_row(self):
        """The record survives the rule change.

        The add now succeeds because a direct add bypasses requirements, not because `waive` was
        passed; the flag is accepted and ignored. What matters, and what this still pins, is that
        stepping over a requirement leaves an EventRequirementWaiver naming the code and the admin.
        """
        event = _event(self.admin, require_team_logo=True, event_name="Bulk Waive Cup")
        resp = self._add(event, waive=True, reason="Invited by AFC, logo coming later")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            RegisteredCompetitors.objects.filter(event=event, team=self.team).exists()
        )
        waiver = EventRequirementWaiver.objects.get(event=event, team=self.team, active=True)
        self.assertEqual(waiver.created_by_id, self.admin.user_id)
        self.assertIn("team_logo_required", waiver.waived_codes)
        self.assertTrue(waiver.reason)

    def test_a_banned_team_cannot_be_waived_in(self):
        event = _event(self.admin, event_name="Bulk Ban Cup")
        self.team.is_banned = True
        self.team.save()
        resp = self._add(event, waive=True, reason="trying it on")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("team_banned", resp.json()["blocked"][0]["codes"])
        self.assertFalse(
            RegisteredCompetitors.objects.filter(event=event, team=self.team).exists()
        )

    def test_an_existing_waiver_lets_the_team_in_with_no_waive_flag(self):
        """A waiver granted earlier through the admin API is honoured here too, without the admin
        having to tick anything again."""
        from afc_tournament_and_scrims import waivers

        event = _event(self.admin, require_team_logo=True, event_name="Bulk Prewaived Cup")
        waivers.grant(
            event, actor=self.admin, reason="Agreed last week",
            codes=["team_logo_required"], team=self.team,
        )
        resp = self._add(event)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            RegisteredCompetitors.objects.filter(event=event, team=self.team).exists()
        )


class WaiverApiTests(TestCase):
    def setUp(self):
        self.admin, self.admin_token = _user("wvapiadmin", role="admin")
        self.outsider, self.outsider_token = _user("wvoutsider")
        self.captain, _ = _user("wvapicaptain")
        self.event = _event(self.admin, event_name="Waiver API Cup")
        self.team = Team.objects.create(
            team_name="API FC", team_owner=self.captain, team_creator=self.captain,
        )

    def _post(self, body, token=None):
        return Client().post(
            "/events/waivers/", body, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.admin_token}",
        )

    def test_a_player_cannot_grant_a_waiver(self):
        resp = self._post(
            {"event_id": self.event.event_id, "team_id": self.team.team_id,
             "codes": ["team_logo_required"], "reason": "let me in"},
            token=self.outsider_token,
        )
        self.assertEqual(resp.status_code, 403)

    def test_an_admin_can_grant_one(self):
        resp = self._post({
            "event_id": self.event.event_id, "team_id": self.team.team_id,
            "codes": ["team_logo_required"], "reason": "Invited by AFC",
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["waiver"]["created_by"], "wvapiadmin")

    def test_a_reason_is_required(self):
        resp = self._post({
            "event_id": self.event.event_id, "team_id": self.team.team_id,
            "codes": ["team_logo_required"], "reason": "",
        })
        self.assertEqual(resp.status_code, 400)

    def test_a_never_waivable_code_is_refused_by_the_api(self):
        resp = self._post({
            "event_id": self.event.event_id, "team_id": self.team.team_id,
            "codes": ["player_banned"], "reason": "trying it on",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("never be waived", resp.json()["message"])

    def test_naming_neither_a_team_nor_a_user_is_refused(self):
        """The rule a CheckConstraint cannot be trusted to enforce on MySQL."""
        resp = self._post({
            "event_id": self.event.event_id,
            "codes": ["team_logo_required"], "reason": "nobody",
        })
        self.assertEqual(resp.status_code, 400)

    def test_listing_returns_active_waivers_only(self):
        self._post({
            "event_id": self.event.event_id, "team_id": self.team.team_id,
            "codes": ["team_logo_required"], "reason": "Invited by AFC",
        })
        listed = Client().get(
            f"/events/{self.event.event_id}/waivers/",
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        ).json()
        self.assertEqual(len(listed["waivers"]), 1)

        waiver_id = listed["waivers"][0]["waiver_id"]
        revoke = Client().delete(
            f"/events/waivers/{waiver_id}/", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        )
        self.assertEqual(revoke.status_code, 200, revoke.content)

        after = Client().get(
            f"/events/{self.event.event_id}/waivers/",
            HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
        ).json()
        self.assertEqual(after["waivers"], [])

    def test_revoking_twice_is_idempotent(self):
        created = self._post({
            "event_id": self.event.event_id, "team_id": self.team.team_id,
            "codes": ["team_logo_required"], "reason": "Invited by AFC",
        }).json()["waiver"]["waiver_id"]
        for _ in range(2):
            resp = Client().delete(
                f"/events/waivers/{created}/", HTTP_AUTHORIZATION=f"Bearer {self.admin_token}",
            )
            self.assertEqual(resp.status_code, 200)
