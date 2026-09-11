"""
afc_tournament_and_scrims/test_open_roster.py - open-roster events (owner 2026-09-11).

Owner, in one message: "organizers select if they want teams to be able to use any players for
that particular event. Roster lock won't apply to this event, but the event also will not count
towards any rankings or tiers. For such events, admins will be able to input the results of teams
without having to input the result of players." Design answers (19:05): player profile stats
still count, only rankings and tiers are skipped; an outsider may appear in ONE team per open event.

COVERS
    - registration: a roster with a non-member is refused on a normal event and accepted on an
      open-roster one (every other gate still runs: the outsider is a real user, not banned)
    - one team per player per event: the same outsider cannot be fielded by a second team of the
      same open event (409 from register_for_event and from edit_roster, which never checked before)
    - admin add-player: an outsider is refused on a normal event and accepted on an open one, and
      refused with 409 when already fielded elsewhere in the event
    - club locks: being fielded in an open-roster event never locks the player's club roster
      (_member_in_active_event_roster) or their identity (_has_active_event_registration)
    - rankings: the counting control is forced off on save, the PATCH refuses to switch it back
      on, and the aggregation helpers exclude the event whatever the control row says
    - team-only results: write_team_result_row scores a row with team-level kills and no players
    - the contract: open_roster is declared once and reads back on the public event payload

Run: python manage.py test afc_tournament_and_scrims.test_open_roster
"""
import datetime
import json
from datetime import date, timedelta

from django.test import Client, TestCase

from afc_auth.models import SessionToken, User, UserProfile
from afc_auth.views import _has_active_event_registration
from afc_rankings import aggregation
from afc_rankings.models import EventCountingControl
from afc_team.models import Team, TeamMembers
from afc_team.views import _member_in_active_event_roster
from afc_tournament_and_scrims import open_roster, result_writes
from afc_tournament_and_scrims.event_contract import EVENT_FIELDS
from afc_tournament_and_scrims.models import (
    Event, Leaderboard, Match, StageGroups, Stages, TournamentPlayerMatchStats, TournamentTeam,
    TournamentTeamMatchStats, TournamentTeamMember,
)


def _user(username, role="player"):
    u = User.objects.create(
        username=username, email=f"{username}@x.com", full_name=username.title(),
        role=role, password="x", country="Nigeria",
    )
    UserProfile.objects.create(user=u)
    tok = SessionToken.objects.create(
        user=u, token=f"tok_{username}"[:32],
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    )
    return u, tok.token


def _event(creator, **overrides):
    fields = dict(
        event_name="Open Cup", competition_type="scrims", participant_type="squad",
        event_type="online", max_teams_or_players=10, event_mode="single",
        start_date=date.today() + timedelta(days=7), end_date=date.today() + timedelta(days=8),
        registration_open_date=date.today() - timedelta(days=1),
        registration_end_date=date.today() + timedelta(days=5),
        number_of_stages=1, creator=creator, is_public=True, is_draft=False,
    )
    fields.update(overrides)
    return Event.objects.create(**fields)


def _club(name, owner, mates):
    team = Team.objects.create(team_name=name, team_owner=owner, team_creator=owner, join_settings="open")
    TeamMembers.objects.create(team=team, member=owner)
    for m in mates:
        TeamMembers.objects.create(team=team, member=m)
    return team


def _post(token, path, body):
    return Client().post(path, json.dumps(body), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


class OpenRosterRegistrationTests(TestCase):
    """Two clubs, one outsider. Alpha fields the outsider; Beta then tries to field them too."""

    def setUp(self):
        self.admin, self.admin_tok = _user("or_admin", role="admin")
        self.cap_a, self.tok_a = _user("or_cap_a")
        self.cap_b, self.tok_b = _user("or_cap_b")
        self.mates_a = [_user(f"or_mate_a{i}")[0] for i in range(3)]
        self.mates_b = [_user(f"or_mate_b{i}")[0] for i in range(3)]
        self.outsider, _ = _user("or_outsider")
        self.alpha = _club("Alpha", self.cap_a, self.mates_a)
        self.beta = _club("Beta", self.cap_b, self.mates_b)
        self.open_event = _event(self.admin, open_roster=True)
        self.closed_event = _event(self.admin, event_name="Closed Cup", open_roster=False)

    def _register(self, token, event, team, roster):
        return _post(token, "/events/register-for-event/", {
            "event_id": event.event_id, "team_id": team.team_id,
            "roster_member_ids": [u.user_id for u in roster],
        })

    def test_outsider_refused_on_a_normal_event(self):
        resp = self._register(self.tok_a, self.closed_event, self.alpha,
                              [self.cap_a] + self.mates_a[:2] + [self.outsider])
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("not members of this team", resp.json()["message"])

    def test_outsider_accepted_on_an_open_roster_event(self):
        resp = self._register(self.tok_a, self.open_event, self.alpha,
                              [self.cap_a] + self.mates_a[:2] + [self.outsider])
        self.assertEqual(resp.status_code, 201, resp.content)
        tt = TournamentTeam.objects.get(event=self.open_event, team=self.alpha)
        rows = TournamentTeamMember.objects.filter(tournament_team=tt)
        self.assertEqual(rows.count(), 4)
        outsider_row = rows.get(user=self.outsider)
        # No club role to freeze for an outsider; left None rather than guessed.
        self.assertIsNone(outsider_row.in_game_role)

    def test_one_team_per_player_per_open_event(self):
        self._register(self.tok_a, self.open_event, self.alpha,
                       [self.cap_a] + self.mates_a[:2] + [self.outsider])
        resp = self._register(self.tok_b, self.open_event, self.beta,
                              [self.cap_b] + self.mates_b[:2] + [self.outsider])
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["user_ids"], [self.outsider.user_id])

    def test_edit_roster_also_refuses_a_player_fielded_elsewhere(self):
        self._register(self.tok_a, self.open_event, self.alpha, [self.cap_a] + self.mates_a)
        self._register(self.tok_b, self.open_event, self.beta, [self.cap_b] + self.mates_b)
        # Beta swaps a mate for Alpha's captain: the captain is already fielded by Alpha.
        resp = _post(self.tok_b, "/events/edit-roster/", {
            "event_id": self.open_event.event_id, "team_id": self.beta.team_id,
            "roster_member_ids": [self.cap_b.user_id, self.cap_a.user_id]
            + [m.user_id for m in self.mates_b[:2]],
        })
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["user_ids"], [self.cap_a.user_id])

    def test_edit_roster_accepts_an_unfielded_outsider_on_an_open_event(self):
        self._register(self.tok_a, self.open_event, self.alpha, [self.cap_a] + self.mates_a)
        resp = _post(self.tok_a, "/events/edit-roster/", {
            "event_id": self.open_event.event_id, "team_id": self.alpha.team_id,
            "roster_member_ids": [self.cap_a.user_id, self.outsider.user_id]
            + [m.user_id for m in self.mates_a[:2]],
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        tt = TournamentTeam.objects.get(event=self.open_event, team=self.alpha)
        self.assertTrue(TournamentTeamMember.objects.filter(tournament_team=tt, user=self.outsider).exists())

    def test_edit_roster_still_refuses_an_outsider_on_a_normal_event(self):
        self._register(self.tok_a, self.closed_event, self.alpha, [self.cap_a] + self.mates_a)
        resp = _post(self.tok_a, "/events/edit-roster/", {
            "event_id": self.closed_event.event_id, "team_id": self.alpha.team_id,
            "roster_member_ids": [self.cap_a.user_id, self.outsider.user_id]
            + [m.user_id for m in self.mates_a[:2]],
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["message"], "Roster players must belong to team.")


class AdminAddPlayerTests(TestCase):
    def setUp(self):
        self.admin, self.admin_tok = _user("oa_admin", role="admin")
        self.cap_a, self.tok_a = _user("oa_cap_a")
        self.cap_b, self.tok_b = _user("oa_cap_b")
        self.mates_a = [_user(f"oa_mate_a{i}")[0] for i in range(3)]
        self.mates_b = [_user(f"oa_mate_b{i}")[0] for i in range(3)]
        self.outsider, _ = _user("oa_outsider")
        self.alpha = _club("Alpha", self.cap_a, self.mates_a)
        self.beta = _club("Beta", self.cap_b, self.mates_b)
        self.open_event = _event(self.admin, open_roster=True)
        self.closed_event = _event(self.admin, event_name="Closed Cup")
        for ev in (self.open_event, self.closed_event):
            _post(self.tok_a, "/events/register-for-event/", {
                "event_id": ev.event_id, "team_id": self.alpha.team_id,
                "roster_member_ids": [self.cap_a.user_id] + [m.user_id for m in self.mates_a],
            })

    def _add(self, event, team, user):
        return _post(self.admin_tok, "/events/add-player-to-event-roster/", {
            "event_id": event.event_id, "team_id": team.team_id, "user_id": user.user_id,
        })

    def test_outsider_refused_on_a_normal_event(self):
        resp = self._add(self.closed_event, self.alpha, self.outsider)
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_outsider_added_on_an_open_event_with_no_frozen_role(self):
        resp = self._add(self.open_event, self.alpha, self.outsider)
        self.assertEqual(resp.status_code, 200, resp.content)
        tt = TournamentTeam.objects.get(event=self.open_event, team=self.alpha)
        self.assertIsNone(TournamentTeamMember.objects.get(tournament_team=tt, user=self.outsider).in_game_role)

    def test_outsider_already_fielded_elsewhere_is_refused(self):
        _post(self.tok_b, "/events/register-for-event/", {
            "event_id": self.open_event.event_id, "team_id": self.beta.team_id,
            "roster_member_ids": [self.cap_b.user_id, self.outsider.user_id]
            + [m.user_id for m in self.mates_b[:2]],
        })
        resp = self._add(self.open_event, self.alpha, self.outsider)
        self.assertEqual(resp.status_code, 409, resp.content)


class OpenRosterLockTests(TestCase):
    """Being fielded in an open-roster event locks nothing about the player's club or identity."""

    def setUp(self):
        self.admin, _ = _user("ol_admin", role="admin")
        self.cap, self.tok = _user("ol_cap")
        self.mates = [_user(f"ol_mate{i}")[0] for i in range(3)]
        self.club = _club("Locked FC", self.cap, self.mates)

    def _live_event(self, **overrides):
        # Started, not over, registration closed: the state in which both locks hold for a normal event.
        ev = _event(self.admin, start_date=date.today(), end_date=date.today() + timedelta(days=1),
                    registration_end_date=date.today() - timedelta(days=1),
                    event_status="ongoing", **overrides)
        tt = TournamentTeam.objects.create(event=ev, team=self.club, registered_by=self.cap)
        for u in [self.cap] + self.mates:
            TournamentTeamMember.objects.create(tournament_team=tt, user=u, event=ev)
        return ev

    def test_a_normal_live_event_locks_the_club_roster_and_identity(self):
        self._live_event()
        self.assertTrue(_member_in_active_event_roster(self.club, self.mates[0].user_id))
        self.assertTrue(_has_active_event_registration(self.mates[0]))

    def test_an_open_roster_live_event_locks_nothing(self):
        self._live_event(open_roster=True)
        self.assertFalse(_member_in_active_event_roster(self.club, self.mates[0].user_id))
        self.assertFalse(_has_active_event_registration(self.mates[0]))


class OpenRosterRankingsTests(TestCase):
    def setUp(self):
        self.admin, self.admin_tok = _user("ork_admin", role="admin")
        from afc_auth.models import Roles, UserRoles
        role, _ = Roles.objects.get_or_create(role_name="head_admin", defaults={"description": "head"})
        UserRoles.objects.create(user=self.admin, role=role)
        self.event = _event(self.admin, open_roster=True)

    def test_sync_after_save_forces_the_control_off(self):
        open_roster.sync_after_save(self.event, self.admin)
        control = EventCountingControl.objects.get(event=self.event)
        self.assertFalse(control.counts_toward_rankings)
        self.assertEqual(control.updated_by_id, self.admin.user_id)
        # A normal event gets no row at all: "no row means everything counts" stays true.
        normal = _event(self.admin, event_name="Normal Cup")
        open_roster.sync_after_save(normal, self.admin)
        self.assertFalse(EventCountingControl.objects.filter(event=normal).exists())

    def test_the_admin_cannot_switch_an_open_event_back_on(self):
        open_roster.sync_after_save(self.event, self.admin)
        resp = Client().patch(
            f"/rankings/event-counting/{self.event.event_id}/",
            json.dumps({"counts_toward_rankings": True, "reason": "trying to count it anyway"}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.admin_tok}",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("open-roster", resp.json()["message"])
        self.assertFalse(EventCountingControl.objects.get(event=self.event).counts_toward_rankings)

    def test_the_detail_reports_the_lock(self):
        resp = Client().get(f"/rankings/event-counting/{self.event.event_id}/",
                            HTTP_AUTHORIZATION=f"Bearer {self.admin_tok}")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["locked_open_roster"])

    def test_aggregation_excludes_the_event_whatever_the_control_row_says(self):
        # Even a hand-flipped row cannot let the event score: the column is the source of truth.
        EventCountingControl.objects.create(event=self.event, counts_toward_rankings=True)
        normal = _event(self.admin, event_name="Normal Cup")
        ids = [self.event.event_id, normal.event_id]
        self.assertEqual(aggregation._open_roster_event_ids(ids), {self.event.event_id})
        self.assertEqual(aggregation._open_roster_event_ids([]), set())


class TeamOnlyResultTests(TestCase):
    """The shared writer scores a team from placement + team kills when no player list is posted."""

    def setUp(self):
        self.admin, self.admin_tok = _user("otr_admin", role="admin")
        today = date.today()
        self.event = _event(self.admin, open_roster=True, competition_type="tournament",
                            start_date=today, end_date=today, event_status="ongoing")
        stage = Stages.objects.create(
            event=self.event, stage_name="Group Stage", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        group = StageGroups.objects.create(
            stage=stage, group_name="Group A", playing_date=today, playing_time=datetime.time(18, 0),
            teams_qualifying=1, match_count=1,
        )
        lb = Leaderboard.objects.create(
            leaderboard_name="Group A LB", event=self.event, stage=stage, group=group,
            creator=self.admin, placement_points={"1": 12, "2": 9, "3": 8}, kill_point=1.0,
            leaderboard_method="manual",
        )
        self.match = Match.objects.create(
            leaderboard=lb, group=group, match_number=1, match_map="bermuda",
            scoring_settings={"placement_points": {"1": 12, "2": 9, "3": 8}, "kill_point": 1},
        )
        club = Team.objects.create(team_name="Alpha", team_owner=self.admin, team_creator=self.admin)
        self.tt = TournamentTeam.objects.create(event=self.event, team=club, registered_by=self.admin)

    def test_team_level_kills_score_without_player_rows(self):
        ctx = result_writes.scoring_context(self.match)
        row = result_writes.write_team_result_row(
            match=self.match, tournament_team_id=self.tt.tournament_team_id,
            row={"placement": 1, "kills": 8, "played": True}, ctx=ctx, frozen_roles={},
        )
        self.assertEqual((row.placement, row.kills, row.total_points), (1, 8, 20))
        self.assertEqual(TournamentPlayerMatchStats.objects.filter(team_stats=row).count(), 0)

    def test_player_rows_win_over_the_team_value_when_both_are_posted(self):
        ctx = result_writes.scoring_context(self.match)
        row = result_writes.write_team_result_row(
            match=self.match, tournament_team_id=self.tt.tournament_team_id,
            row={"placement": 2, "kills": 99, "players": [{"kills": 3}, {"kills": 2}]},
            ctx=ctx, frozen_roles={},
        )
        self.assertEqual((row.kills, row.total_points), (5, 14))

    def test_a_team_that_did_not_play_scores_nothing(self):
        ctx = result_writes.scoring_context(self.match)
        row = result_writes.write_team_result_row(
            match=self.match, tournament_team_id=self.tt.tournament_team_id,
            row={"placement": 1, "kills": 8, "played": False}, ctx=ctx, frozen_roles={},
        )
        self.assertEqual((row.kills, row.total_points), (0, 0))

    def test_manual_endpoint_accepts_team_only_rows(self):
        resp = _post(self.admin_tok, "/events/enter-team-match-result-manual/", {
            "match_id": self.match.match_id,
            "results": [{"tournament_team_id": self.tt.tournament_team_id, "placement": 1, "kills": 6}],
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        row = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.tt)
        self.assertEqual((row.kills, row.total_points), (6, 18))


class OpenRosterContractTests(TestCase):
    def test_declared_once_and_public(self):
        fields = [f for f in EVENT_FIELDS if f.name == "open_roster"]
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].read, "public")
        self.assertEqual(fields[0].write, "organizer")

    def test_reads_back_on_the_public_payload(self):
        admin, _ = _user("oc_admin", role="admin")
        ev = _event(admin, open_roster=True)
        resp = Client().post("/events/get-event-details-not-logged-in/",
                             json.dumps({"slug": ev.slug}), content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["event_details"]["open_roster"])
