"""
afc_tournament_and_scrims/test_event_links.py — endpoint tests for EVENT LINKING P1.

Covers: link create validation (stage ownership, participant-type match, self-link, cycle,
duplicate), fire (standings -> qualifications -> auto-promote registers the team in the
target via the same rows register_for_event writes), window-closed pending + allow bypass,
decline -> replace_next, the UNDO path (withdraws the created registration), the
standings-edited diff + creator notification, and permissions (organizers only their own).

Standings are seeded the real way: a stage/group/match with TournamentTeamMatchStats rows.
Run: python manage.py test afc_tournament_and_scrims.test_event_links
"""
import json
from datetime import date, timedelta

from django.test import TestCase, Client

from afc_auth.models import Notifications, SessionToken, User
from afc_team.models import Team, TeamMembers

from .models import (
    Event, EventLink, EventQualification, Match, RegisteredCompetitors, Stages, StageGroups,
    TournamentTeam, TournamentTeamMember, TournamentTeamMatchStats,
)


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x",
    )
    tok = SessionToken.objects.create(user=u, token=f"tok_{username}")
    return u, tok.token


def _event(creator, name="Source Cup", participant_type="squad", reg_end_in_days=5):
    return Event.objects.create(
        event_name=name,
        competition_type="tournament",
        participant_type=participant_type,
        event_type="online",
        max_teams_or_players=12,
        event_mode="single",
        start_date=date.today() + timedelta(days=7),
        end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=reg_end_in_days),
        number_of_stages=1,
        creator=creator,
    )


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class EventLinkBase(TestCase):
    """Shared fixture: a source event with one stage whose standings rank Alpha > Bravo >
    Charlie, plus an empty target event."""

    def setUp(self):
        self.client = Client()
        self.admin, self.admin_tok = _user("linkadmin", role="admin")
        self.stranger, self.stranger_tok = _user("linkstranger")

        self.source = _event(self.admin, "Dynasty Cup Nigeria")
        self.target = _event(self.admin, "Dynasty Grand Finals")
        self.stage = Stages.objects.create(
            event=self.source, stage_name="Grand Finals", stage_format="br - normal",
            start_date=date.today(), end_date=date.today(), number_of_groups=1,
            teams_qualifying_from_stage=2,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Lobby 1",
            playing_date=date.today(), playing_time="18:00", teams_qualifying=2,
            match_count=1,
        )
        self.match = Match.objects.create(group=self.group, match_number=1)

        # Three teams with owners + captains; Alpha/Bravo/Charlie finish 1st/2nd/3rd.
        self.teams = {}
        for i, name in enumerate(["Alpha", "Bravo", "Charlie"]):
            owner, _ = _user(f"owner_{name.lower()}")
            team = Team.objects.create(
                team_name=name, team_owner=owner, team_creator=owner,
                join_settings="open", country="NG",
            )
            TeamMembers.objects.create(team=team, member=owner, management_role="team_captain")
            tt = TournamentTeam.objects.create(event=self.source, team=team, status="active",
                                               registered_by=owner, country="NG")
            TournamentTeamMember.objects.create(tournament_team=tt, user=owner, event=self.source,
                                                status="active")
            TournamentTeamMatchStats.objects.create(
                match=self.match, tournament_team=tt, placement=i + 1, kills=5 - i,
                placement_points=12 - i * 3, kill_points=5 - i, total_points=17 - i * 4,
            )
            self.teams[name] = team

    def _create_link(self, tok=None, **overrides):
        body = {
            "source_stage_id": self.stage.stage_id,
            "target_event_id": self.target.event_id,
            "qualify_count": 2,
            "auto_promote": True,
            "roster_mode": "copy",
        }
        body.update(overrides)
        return self.client.post(
            f"/events/{self.source.event_id}/links/create/",
            data=json.dumps(body), content_type="application/json",
            **bearer(tok or self.admin_tok),
        )

    def _fire(self, link_id, tok=None):
        return self.client.post(f"/events/links/{link_id}/fire/", **bearer(tok or self.admin_tok))

    def _decide(self, link_id, qual_id, action, tok=None, **extra):
        return self.client.post(
            f"/events/links/{link_id}/decide/",
            data=json.dumps({"qualification_id": qual_id, "action": action, **extra}),
            content_type="application/json", **bearer(tok or self.admin_tok),
        )


class CreateLinkTests(EventLinkBase):
    def test_create_and_duplicate_guard(self):
        resp = self._create_link()
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["link"]["source_stage_name"], "Grand Finals")
        self.assertEqual(self._create_link().status_code, 400)  # same stage+target again

    def test_self_link_rejected(self):
        resp = self._create_link(target_event_id=self.source.event_id)
        self.assertEqual(resp.status_code, 400)

    def test_participant_type_mismatch_rejected(self):
        solo = _event(self.admin, "Solo Cup", participant_type="solo")
        resp = self._create_link(target_event_id=solo.event_id)
        self.assertEqual(resp.status_code, 400)

    def test_cycle_rejected(self):
        # target -> source link exists; creating source -> target must flag the cycle...
        # build the reverse: a stage on the TARGET linking back into the SOURCE.
        back_stage = Stages.objects.create(
            event=self.target, stage_name="Finals", stage_format="br - normal",
            start_date=date.today(), end_date=date.today(), number_of_groups=1,
            teams_qualifying_from_stage=2,
        )
        EventLink.objects.create(
            source_event=self.target, source_stage=back_stage, target_event=self.source,
            created_by=self.admin,
        )
        self.assertEqual(self._create_link().status_code, 400)

    def test_stranger_forbidden(self):
        self.assertEqual(self._create_link(tok=self.stranger_tok).status_code, 403)


class FireAndPromoteTests(EventLinkBase):
    def test_fire_promotes_top_n_into_target(self):
        link_id = self._create_link().json()["link"]["id"]
        resp = self._fire(link_id)
        self.assertEqual(resp.status_code, 200)
        quals = resp.json()["link"]["qualifications"]
        self.assertEqual([q["name"] for q in quals], ["Alpha", "Bravo"])
        self.assertTrue(all(q["status"] == "promoted" for q in quals))
        # The REAL registration rows exist in the target (register_for_event parity).
        self.assertTrue(RegisteredCompetitors.objects.filter(
            event=self.target, team=self.teams["Alpha"]).exists())
        tt = TournamentTeam.objects.get(event=self.target, team=self.teams["Alpha"])
        self.assertEqual(TournamentTeamMember.objects.filter(tournament_team=tt).count(), 1)
        # Captain notified.
        self.assertTrue(Notifications.objects.filter(
            notification_type="qualification", related_event=self.target).exists())

    def test_closed_window_lands_pending_then_allow_bypasses(self):
        self.target.registration_end_date = date.today() - timedelta(days=1)
        self.target.save(update_fields=["registration_end_date"])
        link_id = self._create_link().json()["link"]["id"]
        quals = self._fire(link_id).json()["link"]["qualifications"]
        self.assertTrue(all(q["status"] == "pending" for q in quals))
        self.assertIn("window closed", quals[0]["note"])
        # Owner decision: admin ALLOW bypasses the window.
        resp = self._decide(link_id, quals[0]["id"], "allow")
        self.assertEqual(resp.json()["qualification"]["status"], "promoted")
        self.assertTrue(RegisteredCompetitors.objects.filter(
            event=self.target, team=self.teams["Alpha"]).exists())

    def test_decline_then_replace_next_promotes_charlie(self):
        link_id = self._create_link().json()["link"]["id"]
        quals = self._fire(link_id).json()["link"]["qualifications"]
        bravo = next(q for q in quals if q["name"] == "Bravo")
        self.assertEqual(
            self._decide(link_id, bravo["id"], "decline").json()["qualification"]["status"],
            "declined",
        )
        # Bravo's registration was withdrawn.
        self.assertFalse(RegisteredCompetitors.objects.filter(
            event=self.target, team=self.teams["Bravo"]).exists())
        out = self._decide(link_id, bravo["id"], "replace_next").json()["qualification"]
        self.assertEqual(out["status"], "replaced")
        self.assertIn("Charlie", out["note"])
        self.assertTrue(RegisteredCompetitors.objects.filter(
            event=self.target, team=self.teams["Charlie"]).exists())

    def test_undo_of_decline_restores_status_and_registration(self):
        link_id = self._create_link().json()["link"]["id"]
        quals = self._fire(link_id).json()["link"]["qualifications"]
        alpha = next(q for q in quals if q["name"] == "Alpha")
        # Decline withdraws Alpha's registration in the target...
        self._decide(link_id, alpha["id"], "decline")
        self.assertFalse(RegisteredCompetitors.objects.filter(
            event=self.target, team=self.teams["Alpha"]).exists())
        # ...so UNDO must restore BOTH the status flag and the actual registration rows
        # (a "promoted" row with no registration would lie to the target event).
        out = self._decide(link_id, alpha["id"], "undo").json()["qualification"]
        self.assertEqual(out["status"], "promoted")
        self.assertIn("undone", out["note"])
        self.assertTrue(RegisteredCompetitors.objects.filter(
            event=self.target, team=self.teams["Alpha"]).exists())
        tt = TournamentTeam.objects.get(event=self.target, team=self.teams["Alpha"])
        self.assertEqual(TournamentTeamMember.objects.filter(tournament_team=tt).count(), 1)

    def test_captain_can_self_decline_only(self):
        link_id = self._create_link().json()["link"]["id"]
        quals = self._fire(link_id).json()["link"]["qualifications"]
        alpha = next(q for q in quals if q["name"] == "Alpha")
        bravo = next(q for q in quals if q["name"] == "Bravo")
        cap_tok = SessionToken.objects.create(
            user=self.teams["Alpha"].team_owner, token="tok_alpha_cap",
        ).token
        # Own slot: allowed.
        self.assertEqual(self._decide(link_id, alpha["id"], "decline", tok=cap_tok).status_code, 200)
        # Someone else's slot: forbidden.
        self.assertEqual(self._decide(link_id, bravo["id"], "decline", tok=cap_tok).status_code, 403)

    def test_capacity_warning_and_chain(self):
        # Tiny target: capacity 2, 0 registered, qualify 2 -> no warning (2 fits exactly...
        # warning triggers only when registered + qualify EXCEEDS cap, so drop cap to 1).
        self.target.max_teams_or_players = 1
        self.target.save(update_fields=["max_teams_or_players"])
        self._create_link()
        resp = self.client.get(f"/events/{self.source.event_id}/links/", **bearer(self.admin_tok))
        out = resp.json()["outbound"][0]
        self.assertTrue(out["capacity_warning"])
        self.assertEqual(out["target_capacity"], 1)
        # Chain map: nodes carry both events, the edge carries the stage name.
        chain = self.client.get(
            f"/events/{self.source.event_id}/links/chain/", **bearer(self.admin_tok),
        ).json()
        self.assertEqual(len(chain["nodes"]), 2)
        self.assertEqual(chain["edges"][0]["source_stage_name"], "Grand Finals")
        self.assertTrue(any(n["is_focus"] for n in chain["nodes"]))

    def test_import_competitors_merges_and_skips_duplicates(self):
        # Two source events, each with registered teams; one team overlaps. The merge enters
        # every confirmed team once and reports the duplicate as skipped.
        src2 = _event(self.admin, "Dynasty Cup Ghana")
        for name in ["Alpha", "Delta"]:
            team = self.teams.get(name)
            if team is None:
                owner, _ = _user(f"owner_{name.lower()}2")
                team = Team.objects.create(
                    team_name=name, team_owner=owner, team_creator=owner,
                    join_settings="open", country="GH",
                )
                TeamMembers.objects.create(team=team, member=owner, management_role="team_captain")
                self.teams[name] = team
            RegisteredCompetitors.objects.create(event=src2, team=team, status="registered")
        # The base fixture's source event has TournamentTeam rows but no RC rows; give it
        # RC rows for its three teams so it reads as a confirmed field.
        for name in ["Alpha", "Bravo", "Charlie"]:
            RegisteredCompetitors.objects.get_or_create(
                event=self.source, team=self.teams[name], defaults={"status": "registered"},
            )

        resp = self.client.post(
            f"/events/{self.target.event_id}/import-competitors/",
            data=json.dumps({"source_event_ids": [self.source.event_id, src2.event_id]}),
            content_type="application/json", **bearer(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        report = {r["source_event_name"]: r for r in resp.json()["report"]}
        self.assertEqual(report["Dynasty Cup Nigeria"]["imported"], 3)       # Alpha Bravo Charlie
        self.assertEqual(report["Dynasty Cup Ghana"]["imported"], 1)         # Delta
        self.assertEqual(report["Dynasty Cup Ghana"]["skipped_duplicates"], 1)  # Alpha again
        self.assertEqual(RegisteredCompetitors.objects.filter(event=self.target).count(), 4)
        # Rosters copied from the source event's finishing roster where it existed.
        tt_alpha = TournamentTeam.objects.get(event=self.target, team=self.teams["Alpha"])
        self.assertEqual(TournamentTeamMember.objects.filter(tournament_team=tt_alpha).count(), 1)
        # Stranger cannot merge.
        denied = self.client.post(
            f"/events/{self.target.event_id}/import-competitors/",
            data=json.dumps({"source_event_ids": [self.source.event_id]}),
            content_type="application/json", **bearer(self.stranger_tok),
        )
        self.assertEqual(denied.status_code, 403)

    def test_standings_edit_diff_and_creator_notification(self):
        link_id = self._create_link().json()["link"]["id"]
        self._fire(link_id)
        # EDIT the standings after the fire: Charlie's stats jump to #1.
        tt_charlie = TournamentTeam.objects.get(event=self.source, team=self.teams["Charlie"])
        TournamentTeamMatchStats.objects.filter(tournament_team=tt_charlie).update(
            placement_points=50, kill_points=10,
        )
        resp = self.client.get(f"/events/{self.source.event_id}/links/", **bearer(self.admin_tok))
        out = resp.json()["outbound"][0]
        self.assertTrue(out["standings_changed"])
        self.assertTrue(any(d["now"] == "Charlie" for d in out["diff"]))
        # Creator notified exactly once (flag persists).
        self.client.get(f"/events/{self.source.event_id}/links/", **bearer(self.admin_tok))
        self.assertEqual(Notifications.objects.filter(
            notification_type="link_standings_changed", user=self.admin).count(), 1)
