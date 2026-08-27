"""add_teams_to_event must say WHICH team failed WHICH requirement, and name the players.

WHY THIS FILE EXISTS
    Owner, 2026-08-27, blocked while adding two teams to a live event: "Some teams do not meet this
    event's requirements", and nothing else. The requirement gate itself was working as designed
    (it exists so an admin cannot step around bans and requirements without a record), but it was
    a dead end: the admin could not tell which team, which requirement, or what to do next.

    The detail was already being computed and thrown away. _missing_registration_assets returns a
    per-player map of exactly what each player is missing, and the endpoint reduced it to a single
    code before answering.

WHAT IS PINNED HERE
    That the 409 carries enough to write a sentence a human can act on, and that the never-waivable
    codes stay never-waivable however the payload is dressed up.

Run: AFC_TEST_DB_NAME=test_afc_bulkadd python manage.py test afc_tournament_and_scrims.test_bulk_add_reasons
"""
import json
from datetime import date, timedelta

from django.test import Client, TestCase, override_settings

from afc_auth.models import SessionToken, User, UserProfile
from afc_team.models import Team, TeamMembers
from afc_tournament_and_scrims.models import Event


def _user(username, role="player", uid=None):
    # uid=None, NOT "": User.uid carries a UNIQUE index, so two users with an empty-string uid
    # collide. MySQL allows many NULLs in a unique index, and the requirement check treats NULL and
    # blank alike ("not (uid or '').strip()"), so NULL is the correct way to say "has no uid".
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria", uid=uid,
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _team(owner, name, members):
    t = Team.objects.create(
        team_name=name, team_owner=owner, team_creator=owner,
        country="Nigeria", join_settings="open",
    )
    for m in members:
        TeamMembers.objects.create(team=t, member=m, management_role="member")
    return t


@override_settings(GOOGLE_OAUTH_CLIENT_ID="gid", VENT_CLIENT_ID="", VENT_CLIENT_SECRET="")
class BulkAddReasonTests(TestCase):
    def setUp(self):
        self.admin, self.token = _user("bulkadmin", role="admin")
        # Two players with NO uid, so require_player_uid blocks them and names them.
        self.p1, _ = _user("bulkp1")
        self.p2, _ = _user("bulkp2")
        self.team = _team(self.admin, "CATALYST TEST", [self.p1, self.p2])
        self.event = Event.objects.create(
            event_name="Bulk Add Cup", slug="bulk-add-cup",
            competition_type="tournament", participant_type="squad",
            event_type="online", event_mode="single",
            max_teams_or_players=16, number_of_stages=1,
            start_date=date.today() + timedelta(days=10),
            end_date=date.today() + timedelta(days=11),
            registration_open_date=date.today(),
            registration_end_date=date.today() + timedelta(days=5),
            creator=self.admin,
        )

    def _add(self, **extra):
        return Client().post(
            "/events/add-teams-to-event/",
            data=json.dumps({
                "event_id": self.event.event_id,
                "team_ids": [self.team.team_id],
                **extra,
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_with_no_requirements_the_team_is_added(self):
        """The case the owner described: every requirement off, so nothing should block.

        Pinned because that is precisely what was reported as still failing, and a gate that
        refuses when it has nothing to check would be the worst version of this bug.
        """
        resp = self._add()
        self.assertIn(resp.status_code, (200, 201), resp.content)

    def test_a_blocked_team_is_named_with_its_failing_code(self):
        self.event.require_player_uid = True
        self.event.save()
        resp = self._add()
        self.assertEqual(resp.status_code, 409, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], "requirements_unmet")
        row = body["blocked"][0]
        self.assertEqual(row["team_name"], "CATALYST TEST")
        self.assertIn("registration_requirements_unmet", row["codes"])

    def test_the_response_names_the_PLAYERS_and_what_each_is_missing(self):
        """THE POINT OF THE CHANGE. Without this the admin gets a code and no next step."""
        self.event.require_player_uid = True
        self.event.save()
        body = self._add().json()
        missing = {m["username"]: m["fields"] for m in body["blocked"][0]["missing"]}
        self.assertEqual(set(missing), {"bulkp1", "bulkp2"})
        self.assertEqual(missing["bulkp1"], ["uid"])

    def test_a_required_CONNECTION_is_reported_per_player(self):
        """The connection codes carry a "connection:<slug>" prefix so the UI can offer the right
        link, and that prefix has to survive into this payload."""
        self.event.required_connections = ["google"]
        self.event.save()
        body = self._add().json()
        fields = body["blocked"][0]["missing"][0]["fields"]
        self.assertIn("connection:google", fields)

    def test_checks_run_is_reported_so_the_ui_never_implies_full_parity(self):
        self.event.require_player_uid = True
        self.event.save()
        self.assertIn("capacity_full", self._add().json()["checks_run"])

    def test_waiving_admits_the_team_and_needs_a_reason(self):
        self.event.require_player_uid = True
        self.event.save()

        no_reason = self._add(waive=True)
        self.assertEqual(no_reason.status_code, 400, "a waiver with no reason must be refused")

        ok = self._add(waive=True, reason="Checked their UIDs manually before the match.")
        self.assertIn(ok.status_code, (200, 201), ok.content)

    def test_a_BANNED_team_cannot_be_waived_at_any_price(self):
        """The gate's whole point. A waivable code can be ticked past; a ban cannot."""
        self.team.is_banned = True
        self.team.save()
        resp = self._add(waive=True, reason="please let them in")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("team_banned", resp.json()["blocked"][0]["codes"])
