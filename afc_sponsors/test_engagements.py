"""
afc_sponsors/test_engagements.py — endpoint tests for sponsor P2/P3/P4 (engagements +
submissions + approval gate).

Covers: the wizard configure endpoint (schema validation + permissions), the public for-event
read, registration-time submission writes (solo + squad, via the REAL register-for-event
endpoint with Discord membership mocked), the approval gate (pending registration), the
decide surface (approve / reject-with-reason / reject_final frees the slot / undo), the
player rejection loop (notification + resubmit returns to pending), and the portal's
per-engagement submissions read (privacy: no emails).

Run: python manage.py test afc_sponsors.test_engagements
"""
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, Client

from afc_auth.models import Notifications, SessionToken, User
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import (
    Event, RegisteredCompetitors, Stages, TournamentTeam, TournamentTeamMember,
)

from .models import Sponsor, SponsorMember, EventSponsorship, SponsorEngagementSubmission


def _user(username, role="player", discord=True):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x",
        discord_connected=discord, discord_id=f"d_{username}" if discord else None,
    )
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _event(creator, name="Sponsored Cup", participant_type="solo"):
    return Event.objects.create(
        event_name=name,
        competition_type="tournament",
        participant_type=participant_type,
        event_type="online",
        max_teams_or_players=10,
        event_mode="single",
        start_date=date.today() + timedelta(days=7),
        end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1,
        creator=creator,
        is_public=True,
    )


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


ENGAGEMENTS = [
    {"type": "collect_id", "label": "ydpay UID"},
    {"type": "join_group", "platform": "whatsapp", "invite_url": "https://wa.me/x"},
]


class EngagementBase(TestCase):
    """Shared fixture: an admin, a sponsor with one member, an event with the sponsorship
    attached and a two-entry engagement config (collect_id + whatsapp join_group)."""

    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = _user("spadmin", role="admin")
        self.member, self.member_tok = _user("ydpay_staff")
        self.player, self.player_tok = _user("player_one")

        self.sponsor = Sponsor.objects.create(name="ydpay", slug="ydpay", created_by=self.admin)
        SponsorMember.objects.create(sponsor=self.sponsor, user=self.member, role="owner")
        self.event = _event(self.admin)
        self.sp = EventSponsorship.objects.create(
            event=self.event, sponsor=self.sponsor,
            requires_approval=True, engagements=ENGAGEMENTS,
        )

    # The register endpoint checks live Discord membership; mock it truthy for tests.
    def _register_solo(self, tok, submissions):
        with patch("afc_tournament_and_scrims.views.check_discord_membership", return_value=True):
            return self.client.post(
                "/events/register-for-event/",
                data=json.dumps({
                    "event_id": self.event.event_id,
                    "sponsorships": [{"sponsorship_id": self.sp.id, "submissions": submissions}],
                }),
                content_type="application/json", **bearer(tok),
            )

    def _full_submissions(self, uid_value="yd-001"):
        return [
            {"engagement_index": 0, "payload": {"value": uid_value}},
            {"engagement_index": 1, "payload": {"phone": "8011111111", "country_code": "+234"}},
        ]


class ConfigureTests(EngagementBase):
    def test_admin_configures_and_bad_schema_rejected(self):
        resp = self.client.patch(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/configure/",
            data=json.dumps({"requires_approval": False,
                             "engagements": [{"type": "collect_id", "label": "New UID"}]}),
            content_type="application/json", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.sp.refresh_from_db()
        self.assertFalse(self.sp.requires_approval)
        self.assertEqual(self.sp.engagements[0]["label"], "New UID")

        bad = self.client.patch(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/configure/",
            data=json.dumps({"engagements": [{"type": "nope"}]}),
            content_type="application/json", **bearer(self.admin_tok),
        )
        self.assertEqual(bad.status_code, 400)

    def test_stranger_cannot_configure(self):
        resp = self.client.patch(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/configure/",
            data=json.dumps({"requires_approval": False}),
            content_type="application/json", **bearer(self.player_tok),
        )
        self.assertEqual(resp.status_code, 403)

    def test_for_event_public_read(self):
        resp = self.client.get(f"/sponsors/for-event/{self.event.event_id}/")
        self.assertEqual(resp.status_code, 200)
        row = resp.json()["results"][0]
        self.assertEqual(row["sponsor"]["name"], "ydpay")
        self.assertEqual(len(row["engagements"]), 2)
        self.assertTrue(row["requires_approval"])


class SoloRegistrationTests(EngagementBase):
    def test_register_creates_submissions_and_parks_pending(self):
        resp = self._register_solo(self.player_tok, self._full_submissions())
        self.assertEqual(resp.status_code, 201, resp.json())
        self.assertTrue(resp.json()["pending_sponsor_approval"])
        rc = RegisteredCompetitors.objects.get(event=self.event, user=self.player)
        self.assertEqual(rc.status, "pending")
        subs = SponsorEngagementSubmission.objects.filter(event=self.event, user=self.player)
        self.assertEqual(subs.count(), 2)
        self.assertTrue(all(s.approval_status == "pending" for s in subs))

    def test_missing_answer_rolls_back_registration(self):
        resp = self._register_solo(self.player_tok, [
            {"engagement_index": 0, "payload": {"value": "yd-001"}},
        ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Missing sponsor requirement", resp.json()["message"])
        self.assertFalse(RegisteredCompetitors.objects.filter(
            event=self.event, user=self.player).exists())

    def test_collect_id_duplicate_guard(self):
        self._register_solo(self.player_tok, self._full_submissions("yd-DUP"))
        other, other_tok = _user("player_two")
        resp = self._register_solo(other_tok, self._full_submissions("yd-DUP"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already registered", resp.json()["message"])

    def test_no_approval_required_registers_directly(self):
        self.sp.requires_approval = False
        self.sp.save(update_fields=["requires_approval"])
        resp = self._register_solo(self.player_tok, self._full_submissions())
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()["pending_sponsor_approval"])
        rc = RegisteredCompetitors.objects.get(event=self.event, user=self.player)
        self.assertEqual(rc.status, "registered")
        self.assertTrue(all(
            s.approval_status == "not_required"
            for s in SponsorEngagementSubmission.objects.filter(event=self.event, user=self.player)
        ))


class DecideTests(EngagementBase):
    def setUp(self):
        super().setUp()
        self._register_solo(self.player_tok, self._full_submissions())
        self.subs = list(SponsorEngagementSubmission.objects.filter(
            event=self.event, user=self.player).order_by("engagement_index"))

    def _decide(self, sub_id, action, tok=None, **extra):
        return self.client.post(
            f"/sponsors/submissions/{sub_id}/decide/",
            data=json.dumps({"action": action, **extra}),
            content_type="application/json", **bearer(tok or self.member_tok),
        )

    def test_approving_all_activates_registration(self):
        self._decide(self.subs[0].id, "approve")
        rc = RegisteredCompetitors.objects.get(event=self.event, user=self.player)
        self.assertEqual(rc.status, "pending")  # one still pending
        self._decide(self.subs[1].id, "approve")
        rc.refresh_from_db()
        self.assertEqual(rc.status, "registered")

    def test_reject_requires_reason_and_notifies(self):
        self.assertEqual(self._decide(self.subs[0].id, "reject").status_code, 400)
        resp = self._decide(self.subs[0].id, "reject", reason="UID does not exist")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["submission"]["approval_status"], "rejected")
        note = Notifications.objects.filter(
            user=self.player, notification_type="sponsor_rejection").first()
        self.assertIsNotNone(note)
        self.assertIn("UID does not exist", note.message)

    def test_resubmit_returns_to_pending(self):
        self._decide(self.subs[0].id, "reject", reason="bad uid")
        # A stranger cannot resubmit someone else's row.
        stranger, stranger_tok = _user("stranger1")
        denied = self.client.post(
            f"/sponsors/submissions/{self.subs[0].id}/resubmit/",
            data=json.dumps({"payload": {"value": "yd-FIXED"}}),
            content_type="application/json", **bearer(stranger_tok),
        )
        self.assertEqual(denied.status_code, 403)
        resp = self.client.post(
            f"/sponsors/submissions/{self.subs[0].id}/resubmit/",
            data=json.dumps({"payload": {"value": "yd-FIXED"}}),
            content_type="application/json", **bearer(self.player_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.subs[0].refresh_from_db()
        self.assertEqual(self.subs[0].approval_status, "pending")
        self.assertEqual(self.subs[0].payload["value"], "yd-FIXED")

    def test_reject_final_frees_the_slot(self):
        resp = self._decide(self.subs[0].id, "reject_final", reason="fraudulent entry")
        self.assertEqual(resp.status_code, 200)
        rc = RegisteredCompetitors.objects.get(event=self.event, user=self.player)
        self.assertEqual(rc.status, "rejected")
        final_note = Notifications.objects.filter(
            user=self.player, notification_type="sponsor_rejection",
            message__icontains="slot has been released").exists()
        self.assertTrue(final_note)

    def test_undo_restores_prior_state(self):
        self._decide(self.subs[0].id, "reject", reason="oops wrong row")
        out = self._decide(self.subs[0].id, "undo").json()["submission"]
        self.assertEqual(out["approval_status"], "pending")
        self.assertEqual(out["reason"], "")

    def test_player_cannot_decide(self):
        self.assertEqual(
            self._decide(self.subs[0].id, "approve", tok=self.player_tok).status_code, 403)

    def test_portal_listing_and_privacy(self):
        resp = self.client.get(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/engagement-submissions/",
            **bearer(self.member_tok),
        )
        self.assertEqual(resp.status_code, 200)
        body = json.dumps(resp.json())
        self.assertIn("player_one", body)
        self.assertNotIn("@x.com", body)  # privacy: no account emails anywhere
        # filter by engagement index
        only0 = self.client.get(
            f"/sponsors/{self.sponsor.id}/events/{self.event.event_id}/engagement-submissions/?engagement=0",
            **bearer(self.member_tok),
        ).json()["results"]
        self.assertTrue(all(r["engagement_index"] == 0 for r in only0))

    def test_my_submissions_read(self):
        resp = self.client.get(
            f"/sponsors/my-submissions/{self.event.event_id}/", **bearer(self.player_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total_count"], 2)


class TeamRegistrationTests(EngagementBase):
    """Squad path: per-player payloads, the team parks pending, approving every member's
    submissions activates the team via the existing check_and_activate_team machinery."""

    def setUp(self):
        super().setUp()
        self.team_event = _event(self.admin, "Squad Sponsored Cup", participant_type="squad")
        # The team registration path reads the event's first stage for the Discord role id.
        Stages.objects.create(
            event=self.team_event, stage_name="Group Stage", stage_format="br - normal",
            start_date=date.today(), end_date=date.today(), number_of_groups=1,
            teams_qualifying_from_stage=2,
        )
        self.team_sp = EventSponsorship.objects.create(
            event=self.team_event, sponsor=self.sponsor,
            requires_approval=True,
            engagements=[{"type": "collect_id", "label": "ydpay UID"}],
        )
        self.captain, self.captain_tok = _user("cap_alpha")
        self.mates = [self.captain]
        for i in range(3):
            mate, _ = _user(f"mate_{i}")
            self.mates.append(mate)
        self.team = Team.objects.create(
            team_name="Alpha Squad", team_owner=self.captain, team_creator=self.captain,
            join_settings="open", country="NG",
        )
        for i, m in enumerate(self.mates):
            TeamMembers.objects.create(
                team=self.team, member=m,
                management_role="team_captain" if i == 0 else "member",
            )

    def _register_team(self, by_user_payloads):
        with patch("afc_tournament_and_scrims.views.check_discord_membership", return_value=True):
            return self.client.post(
                "/events/register-for-event/",
                data=json.dumps({
                    "event_id": self.team_event.event_id,
                    "team_id": self.team.team_id,
                    "roster_member_ids": [m.user_id for m in self.mates],
                    "sponsorships": [{
                        "sponsorship_id": self.team_sp.id,
                        "submissions_by_user": by_user_payloads,
                    }],
                }),
                content_type="application/json", **bearer(self.captain_tok),
            )

    def test_team_parks_pending_then_full_approval_activates(self):
        payloads = {
            str(m.user_id): [{"engagement_index": 0, "payload": {"value": f"yd-{m.user_id}"}}]
            for m in self.mates
        }
        resp = self._register_team(payloads)
        self.assertEqual(resp.status_code, 201, resp.json())
        tt = TournamentTeam.objects.get(event=self.team_event, team=self.team)
        self.assertEqual(tt.status, "pending")
        self.assertEqual(
            TournamentTeamMember.objects.filter(tournament_team=tt, status="pending").count(), 4)

        # Approve every member's submission: the team activates + RC flips registered.
        for s in SponsorEngagementSubmission.objects.filter(event=self.team_event):
            self.client.post(
                f"/sponsors/submissions/{s.id}/decide/",
                data=json.dumps({"action": "approve"}),
                content_type="application/json", **bearer(self.member_tok),
            )
        tt.refresh_from_db()
        self.assertEqual(tt.status, "active")
        rc = RegisteredCompetitors.objects.get(event=self.team_event, team=self.team)
        self.assertEqual(rc.status, "registered")

    def test_omitted_player_rolls_back(self):
        payloads = {
            str(self.captain.user_id): [{"engagement_index": 0, "payload": {"value": "yd-1"}}],
        }
        resp = self._register_team(payloads)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(TournamentTeam.objects.filter(
            event=self.team_event, team=self.team).exists())
