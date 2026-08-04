"""Teams submitting their own per-map results, and organizers approving them (item 6).

The four things that have to hold, and why each has tests below:

  1. AN APPROVED SUBMISSION PRODUCES THE ORGANIZER'S OWN ROWS. Both paths go through
     result_writes.write_team_result_row, and test_approved_result_matches_manual_entry proves
     it by scoring the same map twice, once each way, and comparing every stored column. If
     that ever fails, the standings have started disagreeing with themselves.
  2. ONLY THE RIGHT PEOPLE CAN SUBMIT. Not on the team, team not in this match, event not
     accepting submissions, already approved.
  3. ONLY THE RIGHT PEOPLE CAN REVIEW. A team member cannot approve their own submission.
  4. CONFLICTS ARE VISIBLE. Two teams claiming the same placement are reported to the
     organizer rather than blocked at submission time.
"""
import datetime
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from afc_auth.models import Roles, SessionToken, UserRoles
from afc_team.models import Team
from afc_tournament_and_scrims.models import (
    Event,
    Leaderboard,
    Match,
    StageGroups,
    Stages,
    TeamMapResultSubmission,
    TournamentPlayerMatchStats,
    TournamentTeam,
    TournamentTeamMatchStats,
    TournamentTeamMember,
)

User = get_user_model()

SUBMIT_URL = "/events/team-map-results/submit/"
MINE_URL = "/events/team-map-results/mine/"
QUEUE_URL = "/events/team-map-results/queue/"

# Two placement points and one point per kill, so a hand-checked expectation is easy to read:
# first place with six kills scores 10 + 6 = 16.
SCORING = {
    "placement_points": {"1": 10, "2": 6, "3": 3},
    "kill_point": 1,
    "points_per_assist": 0,
    "points_per_1000_damage": 0,
}


class TeamMapSubmissionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # ── the people ──
        self.captain = self._user("captain")
        self.team_mate = self._user("teammate")
        self.outsider = self._user("outsider")          # on no team in this event
        self.other_team_player = self._user("rival")
        self.organizer = self._user("organizer")
        head_admin, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=self.organizer, role=head_admin)

        # ── the event, opted IN to team submissions ──
        # Same event -> stage -> group -> leaderboard -> match fixture the scoring regression
        # tests build (tests_scoring.py), so this exercises a realistic match rather than a
        # bare row. participant_type "squad" because the endpoint refuses solo events.
        today = datetime.date.today()
        self.event = Event.objects.create(
            competition_type="tournament",
            participant_type="squad",
            event_type="internal",
            max_teams_or_players=16,
            event_name="Map Submit Cup",
            event_mode="virtual",
            start_date=today,
            end_date=today,
            registration_open_date=today,
            registration_end_date=today,
            prizepool="0",
            event_rules="rules",
            event_status="ongoing",
            registration_link="https://example.com/reg",
            number_of_stages=1,
            creator=self.organizer,
            allow_team_result_submissions=True,
        )
        self.stage = Stages.objects.create(
            event=self.event, stage_name="Group Stage", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1,
        )
        self.group = StageGroups.objects.create(
            stage=self.stage, group_name="Group A", playing_date=today,
            playing_time=datetime.time(18, 0), teams_qualifying=1, match_count=1,
        )
        self.leaderboard = Leaderboard.objects.create(
            leaderboard_name="Group A LB", event=self.event, stage=self.stage,
            group=self.group, creator=self.organizer,
            placement_points=SCORING["placement_points"], kill_point=1.0,
            leaderboard_method="manual",
        )
        self.match = Match.objects.create(
            leaderboard=self.leaderboard, group=self.group, match_number=1,
            match_map="bermuda", scoring_settings=SCORING,
        )

        self.team = self._tournament_team("Alpha Wolves", [self.captain, self.team_mate])
        self.rival = self._tournament_team("Bravo Squad", [self.other_team_player])

    # ── fixtures ──
    def _user(self, name):
        user = User.objects.create_user(
            username=name, email=f"{name}@afc.test", password="x")
        SessionToken.objects.create(user=user, token=f"tok-{name}")
        return user

    def _tournament_team(self, name, members):
        team = Team.objects.create(
            team_name=name, team_tag=name[:3].upper(), join_settings="open",
            team_creator=members[0], team_owner=members[0], country="NG")
        tt = TournamentTeam.objects.create(
            event=self.event, team=team, registered_by=members[0])
        for member in members:
            TournamentTeamMember.objects.create(
                tournament_team=tt, user=member, event=self.event, status="active")
        return tt

    def _auth(self, name):
        return {"HTTP_AUTHORIZATION": f"Bearer tok-{name}"}

    def _payload(self, placement=1, kills=(3, 3)):
        return {
            "placement": placement,
            "played": True,
            "players": [
                {"user_id": self.captain.pk, "kills": kills[0], "damage": 0, "assists": 0},
                {"user_id": self.team_mate.pk, "kills": kills[1], "damage": 0, "assists": 0},
            ],
        }

    def _submit(self, who="captain", **overrides):
        body = {"match_id": self.match.match_id, "results": self._payload(**overrides)}
        return self.client.post(SUBMIT_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth(who))

    def _approve(self, submission_id, who="organizer", body=None):
        return self.client.post(
            f"/events/team-map-results/{submission_id}/approve/",
            data=json.dumps(body or {}), content_type="application/json", **self._auth(who))

    def _reject(self, submission_id, note="Wrong placement", who="organizer"):
        return self.client.post(
            f"/events/team-map-results/{submission_id}/reject/",
            data=json.dumps({"note": note}), content_type="application/json", **self._auth(who))

    # ──────────────────────────────────────────────────────────────────────────
    # 1) The main path
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_team_member_can_submit_their_own_row(self):
        resp = self._submit()
        self.assertEqual(resp.status_code, 201, resp.content)

        submission = TeamMapResultSubmission.objects.get()
        self.assertEqual(submission.status, "pending")
        self.assertEqual(submission.tournament_team_id, self.team.pk)
        self.assertEqual(submission.submitted_by_id, self.captain.pk)
        # Nothing has reached the standings yet: a submission is a proposal.
        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.match).exists())

    def test_approving_writes_the_result(self):
        submission_id = self._submit().json()["submission"]["submission_id"]

        resp = self._approve(submission_id)
        self.assertEqual(resp.status_code, 200, resp.content)

        stats = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=self.team)
        self.assertEqual(stats.placement, 1)
        self.assertEqual(stats.kills, 6)
        self.assertEqual(stats.placement_points, 10)
        self.assertEqual(stats.kill_points, 6)
        self.assertEqual(stats.total_points, 16)
        self.assertEqual(
            TournamentPlayerMatchStats.objects.filter(team_stats=stats).count(), 2)

        submission = TeamMapResultSubmission.objects.get(pk=submission_id)
        self.assertEqual(submission.status, "approved")
        self.assertEqual(submission.reviewed_by_id, self.organizer.pk)
        self.assertIsNotNone(submission.reviewed_at)
        self.assertEqual(submission.approved_payload["placement"], 1)

        self.match.refresh_from_db()
        self.assertTrue(self.match.result_inputted)

    def test_the_organizer_can_correct_before_approving(self):
        """A transposed digit should cost the organizer one edit, not a rejection and a wait.
        What was submitted and what was approved both survive, so the correction is visible."""
        submission_id = self._submit(placement=1, kills=(3, 3)).json()["submission"]["submission_id"]

        resp = self._approve(submission_id, body={
            "results": {
                "placement": 2,
                "played": True,
                "players": [
                    {"user_id": self.captain.pk, "kills": 2},
                    {"user_id": self.team_mate.pk, "kills": 1},
                ],
            },
            "penalty_points": 4,
        })
        self.assertEqual(resp.status_code, 200, resp.content)

        stats = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.team)
        self.assertEqual(stats.placement, 2)
        self.assertEqual(stats.kills, 3)
        self.assertEqual(stats.penalty_points, 4)
        self.assertEqual(stats.total_points, 6 + 3 - 4)

        submission = TeamMapResultSubmission.objects.get(pk=submission_id)
        self.assertEqual(submission.submitted_payload["placement"], 1)   # what the team said
        self.assertEqual(submission.approved_payload["placement"], 2)    # what the organizer ruled

    def test_rejection_requires_and_records_a_reason(self):
        submission_id = self._submit().json()["submission"]["submission_id"]

        self.assertEqual(self._reject(submission_id, note="").status_code, 400)

        resp = self._reject(submission_id, note="You were third, not first.")
        self.assertEqual(resp.status_code, 200, resp.content)

        submission = TeamMapResultSubmission.objects.get(pk=submission_id)
        self.assertEqual(submission.status, "rejected")
        self.assertEqual(submission.review_note, "You were third, not first.")
        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.match).exists())

        # And the team can read the reason.
        mine = self.client.get(f"{MINE_URL}?match_id={self.match.match_id}",
                               **self._auth("captain")).json()["submissions"]
        self.assertEqual(mine[0]["review_note"], "You were third, not first.")

    # ──────────────────────────────────────────────────────────────────────────
    # 2) THE PROPERTY THAT MATTERS: identical to what the organizer would type
    # ──────────────────────────────────────────────────────────────────────────
    def test_approved_result_matches_manual_entry(self):
        """Score the same map twice, once by approving a submission and once by the organizer's
        own manual entry, and compare every stored column. Both go through
        result_writes.write_team_result_row; if somebody re-inlines either one, this fails."""
        # Placement 1 deliberately. The manual endpoint refuses a lobby with no team at
        # position 1 ("this map has no winner recorded"), because it receives the WHOLE lobby
        # and can check it. A single-team approval cannot apply that rule and does not: the
        # organizer assembles a map one team at a time, so the winner may not have been
        # approved yet. Comparing on placement 1 keeps both paths legal and compares like
        # with like.
        submission_id = self._submit(placement=1, kills=(4, 1)).json()["submission"]["submission_id"]
        self._approve(submission_id)

        approved = TournamentTeamMatchStats.objects.get(
            match=self.match, tournament_team=self.team)
        approved_columns = {
            f: getattr(approved, f) for f in
            ("placement", "kills", "damage", "assists", "placement_points",
             "kill_points", "bonus_points", "penalty_points", "total_points", "played")
        }
        approved_players = sorted(
            (p.player_id, p.kills, p.damage, p.assists, p.played, p.role_at_match)
            for p in TournamentPlayerMatchStats.objects.filter(team_stats=approved))

        # Now the same numbers through the organizer's manual entry endpoint.
        resp = self.client.post(
            "/events/enter-team-match-result-manual/",
            data=json.dumps({
                "match_id": self.match.match_id,
                "results": [{
                    "tournament_team_id": self.team.pk,
                    "placement": 1,
                    "played": True,
                    "players": [
                        {"user_id": self.captain.pk, "kills": 4, "damage": 0, "assists": 0},
                        {"user_id": self.team_mate.pk, "kills": 1, "damage": 0, "assists": 0},
                    ],
                }],
            }),
            content_type="application/json", **self._auth("organizer"))
        self.assertEqual(resp.status_code, 200, resp.content)

        manual = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.team)
        manual_columns = {f: getattr(manual, f) for f in approved_columns}
        manual_players = sorted(
            (p.player_id, p.kills, p.damage, p.assists, p.played, p.role_at_match)
            for p in TournamentPlayerMatchStats.objects.filter(team_stats=manual))

        self.assertEqual(approved_columns, manual_columns)
        self.assertEqual(approved_players, manual_players)

    # ──────────────────────────────────────────────────────────────────────────
    # 3) Who may submit
    # ──────────────────────────────────────────────────────────────────────────
    def test_someone_not_on_a_team_cannot_submit(self):
        resp = self._submit(who="outsider")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(TeamMapResultSubmission.objects.exists())

    def test_a_team_not_in_this_match_cannot_submit(self):
        """A player whose team is registered for the event but is not in this match's group."""
        today = datetime.date.today()
        other_event = Event.objects.create(
            competition_type="tournament", participant_type="squad", event_type="internal",
            max_teams_or_players=16, event_name="Another Cup", event_mode="virtual",
            start_date=today, end_date=today, registration_open_date=today,
            registration_end_date=today, prizepool="0", event_rules="rules",
            event_status="ongoing", registration_link="https://example.com/reg",
            number_of_stages=1, creator=self.organizer, allow_team_result_submissions=True)
        other_stage = Stages.objects.create(
            event=other_event, stage_name="Group Stage", start_date=today, end_date=today,
            number_of_groups=1, stage_format="br - normal", teams_qualifying_from_stage=1)
        other_lb = Leaderboard.objects.create(
            leaderboard_name="Other LB", event=other_event, stage=other_stage,
            creator=self.organizer, placement_points=SCORING["placement_points"],
            kill_point=1.0, leaderboard_method="manual")
        other_match = Match.objects.create(
            leaderboard=other_lb, match_number=1, scoring_settings=SCORING)

        resp = self.client.post(
            SUBMIT_URL,
            data=json.dumps({"match_id": other_match.match_id, "results": self._payload()}),
            content_type="application/json", **self._auth("captain"))
        self.assertEqual(resp.status_code, 403)

    def test_an_event_that_has_not_opted_in_refuses_submissions(self):
        """Most organizers will not want this on, so the flag defaults off and switching it off
        stops new submissions immediately."""
        self.event.allow_team_result_submissions = False
        self.event.save(update_fields=["allow_team_result_submissions"])

        resp = self._submit()
        self.assertEqual(resp.status_code, 403)
        self.assertIn("not accepting", resp.json()["message"])

    def test_a_team_cannot_resubmit_over_an_approved_result(self):
        submission_id = self._submit().json()["submission"]["submission_id"]
        self._approve(submission_id)

        resp = self._submit(placement=1, kills=(9, 9))
        self.assertEqual(resp.status_code, 409)
        stats = TournamentTeamMatchStats.objects.get(match=self.match, tournament_team=self.team)
        self.assertEqual(stats.kills, 6)  # unchanged

    def test_a_second_submission_replaces_the_pending_one(self):
        """One current answer per team per map, so the organizer never has to work out which of
        five queued rows is the real one."""
        first = self._submit(placement=3).json()["submission"]["submission_id"]
        second = self._submit(placement=2).json()["submission"]["submission_id"]

        self.assertNotEqual(first, second)
        rows = TeamMapResultSubmission.objects.filter(
            match=self.match, tournament_team=self.team)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().submitted_payload["placement"], 2)

    def test_more_than_four_played_players_is_refused(self):
        extra = [self._user(f"sub{i}") for i in range(3)]
        for user in extra:
            TournamentTeamMember.objects.create(
                tournament_team=self.team, user=user, event=self.event, status="active")

        body = {
            "match_id": self.match.match_id,
            "results": {
                "placement": 1, "played": True,
                "players": [{"user_id": u.pk, "kills": 1} for u in
                            [self.captain, self.team_mate] + extra],
            },
        }
        resp = self.client.post(SUBMIT_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth("captain"))
        self.assertEqual(resp.status_code, 400)
        # Assert the NUMBER, not the English. The cap used to be the hardcoded word "four";
        # it now comes from the event's format, so pinning the sentence would break every time
        # the copy is reworded while saying nothing about the rule.
        self.assertIn("4 players", resp.json()["message"])

    def test_the_played_cap_follows_the_EVENT_FORMAT_not_a_constant(self):
        """AFC runs duo events as well as squad ones, and the cap was hardcoded to four.

        On a duo event the form demanded exactly four ticks while the backend refused more than
        two, so every submission failed and there was no combination the team could send. The
        cap is now read from the event, and the message says which number applies.
        """
        self.event.participant_type = "duo"
        self.event.save(update_fields=["participant_type"])

        body = {
            "match_id": self.match.match_id,
            "results": {
                "placement": 1, "played": True,
                "players": [{"user_id": u.pk, "kills": 1}
                            for u in (self.captain, self.team_mate)],
            },
        }
        resp = self.client.post(SUBMIT_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth("captain"))
        self.assertEqual(resp.status_code, 201, resp.content)

        # A third player on a DUO map is refused, where on a squad map it would be fine.
        third = self._user("duo_extra")
        TournamentTeamMember.objects.create(
            tournament_team=self.team, user=third, event=self.event, status="active")
        body["results"]["players"].append({"user_id": third.pk, "kills": 1})
        resp = self.client.post(SUBMIT_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth("captain"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("2 players", resp.json()["message"])

    def test_a_player_who_is_not_on_the_roster_is_refused(self):
        """The endpoint validated the SUBMITTER's team membership and then trusted the whole
        player list, so a captain could put any user_id in the payload and, once approved, those
        kills landed in a stranger's ranking. The organizer could not catch it either: the queue
        showed the placement and the TOTAL kills and never named a player."""
        stranger = self._user("not_on_this_team")
        body = {
            "match_id": self.match.match_id,
            "results": {
                "placement": 1, "played": True,
                "players": [{"user_id": self.captain.pk, "kills": 1},
                            {"user_id": stranger.pk, "kills": 9}],
            },
        }
        resp = self.client.post(SUBMIT_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth("captain"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("on your roster", resp.json()["message"])
        self.assertIn(str(stranger.pk), resp.json()["message"])

    def test_a_nonexistent_user_id_is_refused_at_submit_not_at_approve(self):
        """Same guard, second benefit: an unknown id used to pass submit (the payload is only
        stored) and blow up as an FK IntegrityError inside the approve transaction, so the
        ORGANIZER got a 500 for a payload somebody else had sent."""
        body = {
            "match_id": self.match.match_id,
            "results": {
                "placement": 1, "played": True,
                "players": [{"user_id": 99999999, "kills": 1}],
            },
        }
        resp = self.client.post(SUBMIT_URL, data=json.dumps(body),
                                content_type="application/json", **self._auth("captain"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("on your roster", resp.json()["message"])

    def test_the_queue_names_the_players_whose_kills_are_being_approved(self):
        """Approval is the only safeguard between a submission and the standings, so the queue
        has to show WHOSE ranking the kills land in. A total is not reviewable."""
        self._submit()
        resp = self.client.get(f"{QUEUE_URL}?match_id={self.match.match_id}",
                               **self._auth("organizer"))
        self.assertEqual(resp.status_code, 200, resp.content)
        names = resp.json()["submissions"][0]["player_names"]
        self.assertEqual(names[str(self.captain.pk)], self.captain.username)

    # ──────────────────────────────────────────────────────────────────────────
    # 4) Who may review
    # ──────────────────────────────────────────────────────────────────────────
    def test_a_team_member_cannot_approve_their_own_submission(self):
        submission_id = self._submit().json()["submission"]["submission_id"]

        resp = self._approve(submission_id, who="captain")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(TournamentTeamMatchStats.objects.filter(match=self.match).exists())

    def test_a_team_member_cannot_read_the_review_queue(self):
        self._submit()
        resp = self.client.get(f"{QUEUE_URL}?match_id={self.match.match_id}",
                               **self._auth("captain"))
        self.assertEqual(resp.status_code, 403)

    def test_a_team_only_sees_its_own_submissions(self):
        self._submit()
        mine = self.client.get(f"{MINE_URL}?match_id={self.match.match_id}",
                               **self._auth("rival")).json()["submissions"]
        self.assertEqual(mine, [])

    def test_approving_twice_is_refused(self):
        submission_id = self._submit().json()["submission"]["submission_id"]
        self.assertEqual(self._approve(submission_id).status_code, 200)
        self.assertEqual(self._approve(submission_id).status_code, 409)

    # ──────────────────────────────────────────────────────────────────────────
    # 5) Conflicts
    # ──────────────────────────────────────────────────────────────────────────
    def test_two_teams_claiming_one_placement_are_reported_not_blocked(self):
        """Both teams say they came first. Neither submission is refused, because blocking would
        let one team's mistake stop another filing anything; the organizer is told instead."""
        self._submit(placement=1)
        self.client.post(
            SUBMIT_URL,
            data=json.dumps({
                "match_id": self.match.match_id,
                "results": {"placement": 1, "played": True,
                            "players": [{"user_id": self.other_team_player.pk, "kills": 5}]},
            }),
            content_type="application/json", **self._auth("rival"))

        self.assertEqual(TeamMapResultSubmission.objects.count(), 2)

        queue = self.client.get(f"{QUEUE_URL}?match_id={self.match.match_id}",
                                **self._auth("organizer")).json()["submissions"]
        self.assertEqual(len(queue), 2)
        for row in queue:
            self.assertEqual(len(row["conflicts"]), 1, row)
            self.assertEqual(row["conflicts"][0]["placement"], 1)

    def test_approving_a_correction_supersedes_the_earlier_approval(self):
        """An organizer correcting an already-approved map replaces the stats row and leaves the
        earlier decision in the audit trail rather than deleting it."""
        first = self._submit(placement=1, kills=(3, 3)).json()["submission"]["submission_id"]
        self._approve(first)

        # A second submission cannot come from the team now (409, tested above), so the organizer
        # records the correction on a fresh row the way a re-review would.
        second = TeamMapResultSubmission.objects.create(
            match=self.match, tournament_team=self.team, submitted_by=self.captain,
            submitted_payload=self._payload(placement=3, kills=(1, 1)), status="pending")

        resp = self._approve(second.pk)
        self.assertEqual(resp.status_code, 200, resp.content)

        self.assertEqual(TeamMapResultSubmission.objects.get(pk=first).status, "superseded")
        self.assertEqual(TeamMapResultSubmission.objects.get(pk=second.pk).status, "approved")

        # One stats row for the team, carrying the corrected numbers.
        rows = TournamentTeamMatchStats.objects.filter(match=self.match, tournament_team=self.team)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().placement, 3)
        self.assertEqual(rows.first().kills, 2)

    def test_approving_one_team_leaves_another_teams_result_alone(self):
        """The writer clears only the team it is writing. If it cleared the match, approving the
        second team's submission would silently delete the first team's result."""
        first = self._submit(placement=1).json()["submission"]["submission_id"]
        self._approve(first)

        rival_submission = TeamMapResultSubmission.objects.create(
            match=self.match, tournament_team=self.rival, submitted_by=self.other_team_player,
            submitted_payload={"placement": 2, "played": True,
                               "players": [{"user_id": self.other_team_player.pk, "kills": 5}]},
            status="pending")
        self._approve(rival_submission.pk)

        self.assertEqual(
            TournamentTeamMatchStats.objects.filter(match=self.match).count(), 2)
        self.assertEqual(
            TournamentTeamMatchStats.objects.get(
                match=self.match, tournament_team=self.team).placement, 1)
