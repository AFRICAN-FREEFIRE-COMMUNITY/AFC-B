"""
afc_polls.tests_phases - the later phases: branching, team voting, awards editions, hydration.

`tests.py` covers the Phase 1 spine (eligibility, the submit gate, anonymity, permissions, results
visibility, the Phase 0 import). This file covers what was built on top of it, and it is a separate
module for the same reason afc_team has tests_transfer_feed.py: one file per feature area beats one
file that nobody can find anything in.

WHAT IS WORTH TESTING HERE

  1. BRANCHING DISCARDS OFF-PATH ANSWERS. The dangerous bug is not "the wrong question showed", it
     is a stored answer to a question the person was never supposed to be asked. Every individual
     response looks reasonable and the totals are quietly wrong, so there is a test that answers
     Q2, changes its mind on Q1, and asserts the Q2 answer is GONE from the database.

  2. QUORUM COUNTS PLAYING ROLES ONLY. The roster in these tests is deliberately six players plus
     two staff, which is the shape where the rule is visible: a whole-roster count would need five
     answers, playing roles need four.

  3. NO_CONSENSUS IS ITS OWN BUCKET. A split team, a silent team and a team whose captain never
     opened the poll are three different events needing three different follow-ups.

  4. THE OVERRIDE KEEPS THE MEMBERS' TALLY. Decision 6 is about visibility, so the test asserts
     the four members' votes are still on the row after the captain overrules them.

  5. CLOSED AND ANNOUNCED ARE DIFFERENT STATES. after_close publishes the instant voting stops;
     an awards night needs the days in between.

  6. A LABEL IS NEVER TRANSLATED. "SCARLETT" through machine translation into French is a bug that
     ships quietly and is very visible when it does.

Run:
    AFC_TEST_DB_NAME=test_afc_polls python manage.py test afc_polls -v 2
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from afc_team.models import Team, TeamMembers

from .branching import canonical_path
from .models import (
    AwardsEdition,
    Poll,
    PollAnswer,
    PollBranchRule,
    PollOption,
    PollQuestion,
    PollResponse,
    PollTeamResult,
    PollWatch,
)
from .team_voting import freeze_poll, quorum_target, roster_counts
from .tests import make_user, open_poll, token_for


# ── branching: an answer decides the next question (Phase 2) ──────────────────────────────────


def branching_poll(slug="branch-poll"):
    """Q1 decides whether Q2 is asked. Every branching poll reduces to this shape."""
    poll = Poll.objects.create(
        slug=slug, title="Branching poll", visibility=Poll.PUBLIC,
        opens_at=timezone.now() - timedelta(hours=1),
        closes_at=timezone.now() + timedelta(days=1),
    )
    q1 = PollQuestion.objects.create(poll=poll, order=0, prompt="Do you compete?")
    yes = PollOption.objects.create(question=q1, order=0, label="Yes")
    no = PollOption.objects.create(question=q1, order=1, label="No")
    q2 = PollQuestion.objects.create(poll=poll, order=1, prompt="Which mode?")
    br = PollOption.objects.create(question=q2, order=0, label="Battle Royale")
    PollBranchRule.objects.create(
        poll=poll, order=0, when_question=q1, operator=PollBranchRule.IS,
        value={"option_ids": [yes.option_id]}, action=PollBranchRule.SHOW, target_question=q2,
    )
    return poll, q1, yes, no, q2, br


class BranchingTests(TestCase):
    def setUp(self):
        self.poll, self.q1, self.yes, self.no, self.q2, self.br = branching_poll()
        self.voter = make_user("brancher")

    def test_a_poll_with_no_rules_is_simply_linear(self):
        """The common case has to cost nothing: every question stays on the path."""
        poll, question, _first, _second = open_poll("linear-poll")
        self.assertEqual(canonical_path(poll, {}), [question.question_id])

    def test_the_target_is_hidden_until_the_rule_is_satisfied(self):
        self.assertEqual(
            canonical_path(self.poll, {self.q1.question_id: [self.no.option_id]}),
            [self.q1.question_id],
        )
        self.assertEqual(
            canonical_path(self.poll, {self.q1.question_id: [self.yes.option_id]}),
            [self.q1.question_id, self.q2.question_id],
        )

    def test_an_unanswered_watched_question_never_satisfies_a_rule(self):
        """Including `is_not`. "You did not answer Q1" is not the same claim as "your answer was
        not X", and treating a blank as satisfying is_not would show the follow-up to everybody
        who simply has not got there yet, which on a long ballot is everybody."""
        rule = self.poll.branch_rules.first()
        rule.operator = PollBranchRule.IS_NOT
        rule.save(update_fields=["operator"])
        self.assertEqual(canonical_path(self.poll, {}), [self.q1.question_id])

    def test_the_server_discards_an_answer_that_is_off_the_path(self):
        """THE branching test. Somebody answers Q2, changes their mind on Q1, and submits."""
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [
                {"question_id": self.q1.question_id, "option_ids": [self.no.option_id]},
                {"question_id": self.q2.question_id, "option_ids": [self.br.option_id]},
            ]},
            content_type="application/json", **token_for(self.voter),
        )
        self.assertEqual(response.status_code, 201)

        sheet = PollResponse.objects.get(poll=self.poll)
        self.assertEqual(set(sheet.answers.values_list("question_id", flat=True)),
                         {self.q1.question_id})
        self.assertEqual(sheet.path_snapshot, [self.q1.question_id])

    def test_a_required_question_off_the_path_does_not_block_a_submit(self):
        """A required question the branching removed is not one they failed to answer."""
        self.q2.required = True
        self.q2.save(update_fields=["required"])
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.q1.question_id, "option_ids": [self.no.option_id]}]},
            content_type="application/json", **token_for(self.voter),
        )
        self.assertEqual(response.status_code, 201)

    def test_a_required_question_on_the_path_still_blocks(self):
        self.q2.required = True
        self.q2.save(update_fields=["required"])
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.q1.question_id,
                          "option_ids": [self.yes.option_id]}]},
            content_type="application/json", **token_for(self.voter),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Which mode?", response.json()["message"])

    def test_hide_beats_show_on_the_same_question(self):
        """"Do not ask this person" is a stronger statement than "you may"."""
        PollBranchRule.objects.create(
            poll=self.poll, order=1, when_question=self.q1, operator=PollBranchRule.IS,
            value={"option_ids": [self.yes.option_id]}, action=PollBranchRule.HIDE,
            target_question=self.q2,
        )
        self.assertEqual(
            canonical_path(self.poll, {self.q1.question_id: [self.yes.option_id]}),
            [self.q1.question_id],
        )

    def test_the_rules_are_sent_to_the_client_so_the_form_can_react(self):
        payload = self.client.get(f"/polls/{self.poll.slug}/").json()
        self.assertEqual(len(payload["branch_rules"]), 1)
        self.assertEqual(payload["branch_rules"][0]["target_question_id"], self.q2.question_id)

    def test_a_rule_cannot_target_the_question_it_watches(self):
        """It can never be satisfied twice the same way, and reads to an admin as a page that
        flickers."""
        admin = make_user("branch_admin", role="admin")
        response = self.client.put(
            f"/polls/admin/polls/{self.poll.slug}/questions/",
            {
                "questions": [
                    {"question_id": self.q1.question_id, "prompt": "Do you compete?",
                     "options": [{"option_id": self.yes.option_id, "label": "Yes"}]},
                ],
                "branch_rules": [{
                    "when_question_id": self.q1.question_id,
                    "target_question_id": self.q1.question_id,
                    "operator": "is", "action": "show", "value": {"option_ids": []},
                }],
            },
            content_type="application/json", **token_for(admin),
        )
        self.assertEqual(response.status_code, 400)


# ── team voting (Phase 4) ─────────────────────────────────────────────────────────────────────


class TeamVotingTests(TestCase):
    """Six players plus two staff on purpose: that is the roster shape where the playing-roles
    quorum rule is visible. A whole-roster count would need five answers; playing roles need
    four."""

    def setUp(self):
        self.owner = make_user("t_owner")
        self.team = Team.objects.create(
            team_name="Quorum FC", join_settings="open",
            team_creator=self.owner, team_owner=self.owner,
        )
        self.players = [make_user(f"t_player{index}") for index in range(6)]
        self.captain = self.players[0]
        self.team.team_captain = self.captain
        self.team.save(update_fields=["team_captain"])
        TeamMembers.objects.create(team=self.team, member=self.captain,
                                   management_role="team_captain")
        for player in self.players[1:]:
            TeamMembers.objects.create(team=self.team, member=player, management_role="member")
        for role in ("coach", "manager"):
            TeamMembers.objects.create(team=self.team, member=make_user(f"t_{role}"),
                                       management_role=role)

        self.poll, self.question, self.first, self.second = open_poll(
            "team-poll", subject=Poll.TEAM,
        )

    def _vote(self, user, option):
        return self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [option.option_id]}]},
            content_type="application/json", **token_for(user),
        )

    def _result(self):
        return PollTeamResult.objects.get(poll=self.poll, question=self.question, team=self.team)

    def test_quorum_counts_playing_roles_only(self):
        """A team should not fail quorum because its analyst is on holiday."""
        self.assertEqual(roster_counts(self.team), (6, 8))
        self.assertEqual(quorum_target(self.poll, 6), 4)

    def test_below_quorum_the_team_casts_no_vote(self):
        for player in self.players[:3]:
            self._vote(player, self.first)
        result = self._result()
        self.assertFalse(result.quorum_met)
        self.assertEqual(result.resolution, PollTeamResult.BELOW_QUORUM)
        self.assertIsNone(result.winning_option_id)
        # The tally is still recorded, so the roll-up shows "3 of 6" rather than an empty card
        # that reads as an error.
        self.assertEqual(result.answered_count, 3)
        self.assertEqual(result.playing_roster_size, 6)
        self.assertEqual(result.full_roster_size, 8)

    def test_a_clear_majority_wins_once_quorum_is_met(self):
        for player in self.players[:4]:
            self._vote(player, self.first)
        result = self._result()
        self.assertTrue(result.quorum_met)
        self.assertEqual(result.resolution, PollTeamResult.PLURALITY)
        self.assertEqual(result.winning_option_id, self.first.option_id)

    def test_a_tie_falls_to_the_captain(self):
        self._vote(self.captain, self.first)
        self._vote(self.players[1], self.first)
        self._vote(self.players[2], self.second)
        self._vote(self.players[3], self.second)
        result = self._result()
        self.assertEqual(result.resolution, PollTeamResult.TIE_BROKEN_BY_CAPTAIN)
        self.assertEqual(result.winning_option_id, self.first.option_id)

    def test_a_tie_with_no_captain_answer_is_no_consensus_not_a_missing_row(self):
        """A SPLIT team is not a SILENT team. An admin reading the results has to tell them
        apart, because the follow-up is different."""
        for player in self.players[1:3]:
            self._vote(player, self.first)
        for player in self.players[3:5]:
            self._vote(player, self.second)
        result = self._result()
        self.assertEqual(result.resolution, PollTeamResult.NO_CONSENSUS)
        self.assertIsNone(result.winning_option_id)
        self.assertTrue(result.quorum_met)

    def test_somebody_with_no_team_cannot_answer_a_team_poll(self):
        response = self._vote(make_user("t_loner"), self.first)
        self.assertEqual(response.status_code, 403)
        self.assertIn("not on a roster", response.json()["message"])

    def test_the_captain_override_is_refused_when_the_switch_is_off(self):
        """OFF by default. An override the roster cannot see is a trust problem, not a feature."""
        self.assertFalse(self.poll.captain_override_allowed)
        response = self.client.post(
            f"/polls/{self.poll.slug}/team-answer/",
            {"question_id": self.question.question_id, "option_id": self.second.option_id},
            content_type="application/json", **token_for(self.captain),
        )
        self.assertEqual(response.status_code, 403)

    def test_the_override_keeps_the_members_tally_visible(self):
        self.poll.captain_override_allowed = True
        self.poll.save(update_fields=["captain_override_allowed"])
        for player in self.players[:4]:
            self._vote(player, self.first)

        response = self.client.post(
            f"/polls/{self.poll.slug}/team-answer/",
            {"question_id": self.question.question_id, "option_id": self.second.option_id},
            content_type="application/json", **token_for(self.captain),
        )
        self.assertEqual(response.status_code, 200)

        result = self._result()
        self.assertEqual(result.resolution, PollTeamResult.CAPTAIN_OVERRIDE)
        self.assertEqual(result.winning_option_id, self.second.option_id)
        self.assertEqual(result.set_by_id, self.captain.pk)
        self.assertEqual(result.tally.get(str(self.first.option_id)), 4)

    def test_a_later_member_submit_does_not_undo_the_captains_override(self):
        self.poll.captain_override_allowed = True
        self.poll.save(update_fields=["captain_override_allowed"])
        for player in self.players[:4]:
            self._vote(player, self.first)
        self.client.post(
            f"/polls/{self.poll.slug}/team-answer/",
            {"question_id": self.question.question_id, "option_id": self.second.option_id},
            content_type="application/json", **token_for(self.captain),
        )
        self._vote(self.players[4], self.first)

        result = self._result()
        self.assertEqual(result.resolution, PollTeamResult.CAPTAIN_OVERRIDE)
        self.assertEqual(result.winning_option_id, self.second.option_id)
        # Refreshed, not frozen: the roster keeps seeing what they actually voted for.
        self.assertEqual(result.tally.get(str(self.first.option_id)), 5)

    def test_a_non_captain_cannot_use_the_override(self):
        self.poll.captain_override_allowed = True
        self.poll.save(update_fields=["captain_override_allowed"])
        response = self.client.post(
            f"/polls/{self.poll.slug}/team-answer/",
            {"question_id": self.question.question_id, "option_id": self.second.option_id},
            content_type="application/json", **token_for(self.players[2]),
        )
        self.assertEqual(response.status_code, 403)

    def test_the_rollup_reaches_the_member_who_is_answering(self):
        self._vote(self.players[1], self.first)
        payload = self.client.get(
            f"/polls/{self.poll.slug}/", **token_for(self.players[1])
        ).json()
        self.assertEqual(payload["team"]["team_name"], "Quorum FC")
        self.assertFalse(payload["team"]["is_captain"])
        self.assertEqual(payload["team"]["rollup"][0]["quorum_target"], 4)
        self.assertEqual(payload["team"]["rollup"][0]["playing_roster_size"], 6)

    def test_freezing_is_idempotent(self):
        for player in self.players[:4]:
            self._vote(player, self.first)
        self.assertEqual(freeze_poll(self.poll), 1)
        # A frozen row is history: re-running must not recompute it against a roster that has
        # changed since the poll closed.
        self.assertEqual(freeze_poll(self.poll), 0)


# ── awards editions and the published winner ──────────────────────────────────────────────────


class AwardsEditionTests(TestCase):
    def setUp(self):
        self.admin = make_user("edition_admin", role="admin")
        self.edition = AwardsEdition.objects.create(
            slug="nfca-2026", title="NFCA 2026", year=2026,
        )
        self.poll, self.question, self.first, self.second = open_poll(
            "nfca-2026-creators", kind=Poll.AWARD, awards_edition="NFCA 2026",
        )
        self.poll.edition = self.edition
        self.poll.save(update_fields=["edition"])

    def test_the_phase_is_derived_from_the_dates_and_never_stored(self):
        now = timezone.now()
        self.assertEqual(self.edition.phase(), AwardsEdition.ANNOUNCED)
        self.edition.voting_opens_at = now - timedelta(days=1)
        self.assertEqual(self.edition.phase(), AwardsEdition.VOTING)
        self.edition.voting_closes_at = now - timedelta(hours=1)
        self.assertEqual(self.edition.phase(), AwardsEdition.COUNTING)
        self.edition.winners_announced_at = now - timedelta(minutes=1)
        self.assertEqual(self.edition.phase(), AwardsEdition.WINNERS)

    def test_closed_and_announced_are_different_states(self):
        """after_close publishes the instant voting stops. An awards night needs the days in
        between, where the page says: closed, counting, come back on the night."""
        self.poll.results_visibility = Poll.AFTER_ANNOUNCEMENT
        self.poll.closes_at = timezone.now() - timedelta(hours=1)
        self.poll.save(update_fields=["results_visibility", "closes_at"])
        self.assertFalse(self.client.get(f"/polls/{self.poll.slug}/").json()["results_visible"])

        self.edition.winners_announced_at = timezone.now() - timedelta(minutes=1)
        self.edition.save(update_fields=["winners_announced_at"])
        self.assertTrue(self.client.get(f"/polls/{self.poll.slug}/").json()["results_visible"])

    def test_the_edition_endpoint_counts_progress_across_its_polls(self):
        """"You have voted in 12 of 28" crosses poll boundaries, which is why this endpoint
        exists at all."""
        voter = make_user("edition_voter")
        self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.first.option_id]}]},
            content_type="application/json", **token_for(voter),
        )
        payload = self.client.get(
            f"/polls/editions/{self.edition.slug}/", **token_for(voter)
        ).json()
        self.assertEqual(payload["edition"]["slug"], "nfca-2026")
        self.assertEqual(payload["totals"]["questions"], 1)
        self.assertEqual(payload["totals"]["answered"], 1)
        self.assertEqual(payload["polls"][0]["answered_question_ids"],
                         [self.question.question_id])

    def test_a_signed_out_visitor_gets_the_edition_with_no_progress(self):
        payload = self.client.get(f"/polls/editions/{self.edition.slug}/").json()
        self.assertIsNone(payload["totals"]["answered"])
        self.assertFalse(payload["polls"][0]["eligibility"]["eligible"])

    def test_publishing_a_winner_is_an_editorial_act_with_a_date(self):
        response = self.client.post(
            f"/polls/admin/polls/{self.poll.slug}/publish-winner/",
            {"question_id": self.question.question_id, "option_id": self.second.option_id,
             "votes": 313},
            content_type="application/json", **token_for(self.admin),
        )
        self.assertEqual(response.status_code, 200)

        self.question.refresh_from_db()
        self.assertEqual(self.question.published_winner_option_id, self.second.option_id)
        self.assertEqual(self.question.published_winner_votes, 313)
        self.assertIsNotNone(self.question.published_at)
        self.assertEqual(self.question.published_result_source, "admin:edition_admin")

    def test_a_published_winner_beats_the_tally_and_the_tally_is_still_carried(self):
        """The 2025 winners are published values that MAY disagree with the stored votes, because
        the vote-count validation in afc_awards was commented out before those votes were cast.
        Where they disagree the published claim wins, and the difference stays visible for a human
        rather than being silently reconciled."""
        for index in range(3):
            self.client.post(
                f"/polls/{self.poll.slug}/responses/",
                {"answers": [{"question_id": self.question.question_id,
                              "option_ids": [self.first.option_id]}]},
                content_type="application/json", **token_for(make_user(f"disagree{index}")),
            )
        self.question.published_winner_option = self.second
        self.question.published_winner_votes = 313
        self.question.save()
        self.poll.results_visibility = Poll.ALWAYS
        self.poll.save(update_fields=["results_visibility"])

        payload = self.client.get(f"/polls/{self.poll.slug}/").json()["questions"][0]
        self.assertEqual(payload["published_winner_option_id"], self.second.option_id)
        self.assertEqual(payload["published_winner_votes"], 313)
        votes = {option["option_id"]: option["votes"] for option in payload["options"]}
        self.assertEqual(votes[self.first.option_id], 3)

    def test_an_edition_with_polls_cannot_be_deleted(self):
        response = self.client.delete(
            f"/polls/admin/editions/{self.edition.slug}/", **token_for(self.admin)
        )
        self.assertEqual(response.status_code, 409)

    def test_only_afc_staff_may_manage_an_edition(self):
        """An edition spans several polls and is site-wide, so it sits on the same side of the
        line as a site-wide poll: an organizer running a poll on their own event has no claim."""
        response = self.client.post(
            "/polls/admin/editions/", {"title": "Rogue edition"},
            content_type="application/json", **token_for(make_user("rogue")),
        )
        self.assertEqual(response.status_code, 403)


# ── option hydration, question anchors and watches ────────────────────────────────────────────


class HydrationAndAnchorTests(TestCase):
    def setUp(self):
        self.poll, self.question, self.first, self.second = open_poll("hydration-poll")
        self.player = make_user("nominee_one")
        self.team = Team.objects.create(
            team_name="V-ENT ESPORTS", join_settings="open",
            team_creator=self.player, team_owner=self.player,
        )

    def _options(self, **extra):
        return self.client.get(f"/polls/{self.poll.slug}/", **extra).json()["questions"][0][
            "options"
        ]

    def test_a_linked_option_carries_the_player_behind_it(self):
        self.first.linked_type = PollOption.LINK_USER
        self.first.linked_id = self.player.pk
        self.first.save()

        linked = self._options()[0]["linked"]
        self.assertEqual(linked["display_name"], "nominee_one")
        self.assertEqual(linked["profile_url"], "/players/nominee_one")
        # NULL, not a placeholder: the frontend draws a designed monogram from the name, and a
        # placeholder URL here would take that decision away from the surface that can make it
        # look right.
        self.assertIsNone(linked["avatar_url"])

    def test_a_linked_team_points_at_the_team_route_by_name(self):
        """The /teams route takes the NAME. Getting this wrong ships a 404 deep link that looks
        fine in a test asserting the same mistake, which has happened on this project before."""
        self.second.linked_type = PollOption.LINK_TEAM
        self.second.linked_id = self.team.pk
        self.second.save()
        self.assertEqual(self._options()[1]["linked"]["profile_url"], "/teams/V-ENT ESPORTS")

    def test_an_option_whose_entity_is_gone_still_renders_its_label(self):
        """A published award winner cannot vanish from the record because somebody closed their
        account. That is why the link is soft and `label` is the durable copy."""
        self.first.linked_type = PollOption.LINK_USER
        self.first.linked_id = 99999999
        self.first.save()

        option = self._options()[0]
        self.assertIsNone(option["linked"])
        self.assertEqual(option["label"], "First")

    def test_an_option_label_is_never_machine_translated(self):
        """Labels are NAMES. "SCARLETT" through machine translation into French is a bug that
        ships quietly and is very visible when it does. Only the description is a sentence."""
        self.first.label = "SCARLETT"
        self.first.save(update_fields=["label"])
        self.assertEqual(self._options(HTTP_ACCEPT_LANGUAGE="fr")[0]["label"], "SCARLETT")

    def test_every_question_gets_a_stable_anchor_slug(self):
        self.assertEqual(self.question.slug, "pick-one")

    def test_two_prompts_differing_only_in_case_get_two_slugs(self):
        """The live 2025 data holds "Favorite DUO (Male)" and "Favorite DUO (MALE)"."""
        first = PollQuestion.objects.create(poll=self.poll, order=1, prompt="Favorite DUO (Male)")
        second = PollQuestion.objects.create(poll=self.poll, order=2, prompt="Favorite DUO (MALE)")
        self.assertEqual(first.slug, "favorite-duo-male")
        self.assertEqual(second.slug, "favorite-duo-male-2")

    def test_a_watch_is_idempotent_and_can_be_cancelled(self):
        watcher = make_user("watcher")
        for _ in range(2):
            response = self.client.post(
                "/polls/watch/", {"poll_slug": self.poll.slug, "reason": "eligibility"},
                content_type="application/json", **token_for(watcher),
            )
            self.assertIn(response.status_code, (200, 201))
        self.assertEqual(PollWatch.objects.filter(user=watcher).count(), 1)

        self.client.delete(
            "/polls/watch/", {"poll_slug": self.poll.slug, "reason": "eligibility"},
            content_type="application/json", **token_for(watcher),
        )
        self.assertEqual(PollWatch.objects.filter(user=watcher).count(), 0)

    def test_a_watch_needs_an_account(self):
        """A watch is a promise to notify a specific person, so there is nobody to promise it to
        without an account."""
        response = self.client.post(
            "/polls/watch/", {"poll_slug": self.poll.slug}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


# ── the four remaining answer types (Phase 2) ─────────────────────────────────────────────────


class AnswerTypeTests(TestCase):
    def setUp(self):
        self.poll, self.question, self.first, self.second = open_poll("types-poll")
        self.question.required = False
        self.question.save(update_fields=["required"])
        self.voter = make_user("typer")

    def _add(self, answer_type, **config):
        return PollQuestion.objects.create(
            poll=self.poll, order=9, prompt=f"A {answer_type} question",
            answer_type=answer_type, config=config,
        )

    def _post(self, entry):
        return self.client.post(
            f"/polls/{self.poll.slug}/responses/", {"answers": [entry]},
            content_type="application/json", **token_for(self.voter),
        )

    def test_a_rating_is_stored_as_a_value_with_no_option(self):
        question = self._add(PollQuestion.RATING, scale_points=5)
        self.assertEqual(
            self._post({"question_id": question.question_id, "rating": 4}).status_code, 201
        )
        answer = PollAnswer.objects.get(question=question)
        self.assertIsNone(answer.option_id)
        self.assertEqual(answer.value, {"rating": 4})

    def test_a_rating_outside_the_scale_is_refused(self):
        """Clamped rather than silently stored out of range: a 9 on a five-point scale would break
        every average that reads it."""
        question = self._add(PollQuestion.RATING, scale_points=5)
        response = self._post({"question_id": question.question_id, "rating": 9})
        self.assertEqual(response.status_code, 400)
        self.assertIn("between 1 and 5", response.json()["message"])

    def test_long_text_respects_its_own_limit(self):
        question = self._add(PollQuestion.LONG_TEXT, max_length=20)
        self.assertEqual(
            self._post({"question_id": question.question_id, "text": "x" * 21}).status_code, 400
        )
        self.assertEqual(
            self._post({"question_id": question.question_id,
                        "text": "short enough"}).status_code, 201
        )

    def test_a_ranking_stores_one_row_per_option_carrying_its_position(self):
        question = self._add(PollQuestion.RANKING)
        options = [
            PollOption.objects.create(question=question, order=index, label=f"Option {index}")
            for index in range(3)
        ]
        ordered = [options[2].option_id, options[0].option_id, options[1].option_id]
        self.assertEqual(
            self._post({"question_id": question.question_id, "option_ids": ordered}).status_code,
            201,
        )
        positions = {
            answer.option_id: answer.value["position"]
            for answer in PollAnswer.objects.filter(question=question)
        }
        self.assertEqual(positions[options[2].option_id], 1)
        self.assertEqual(positions[options[1].option_id], 3)

    def test_a_ranking_is_capped_at_five(self):
        """It is the hardest type to use on a phone, and most AFC users are on phones."""
        question = self._add(PollQuestion.RANKING)
        options = [
            PollOption.objects.create(question=question, order=index, label=f"Option {index}")
            for index in range(6)
        ]
        response = self._post({
            "question_id": question.question_id,
            "option_ids": [option.option_id for option in options],
        })
        self.assertEqual(response.status_code, 400)
