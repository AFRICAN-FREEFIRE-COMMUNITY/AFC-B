"""
afc_polls.tests - the poll engine's test suite.

WHAT IS ACTUALLY WORTH TESTING HERE, and why these and not others:

  1. ELIGIBILITY REFUSES, AND SAYS WHY. One test per rule type (country, hand-set team tier,
     season tier, rank window, team role, event, profile field), each asserting BOTH that the
     person is refused AND that the requirement line names their own value. A refusal the UI
     cannot explain is the failure this whole feature exists to prevent, so "eligible is False"
     on its own is not a passing test.

  2. THE SERVER RE-CHECKS AT SUBMIT. The most dangerous possible bug in this app is a gate that
     only exists in the client, so there is a test that posts straight to the submit endpoint with
     a valid session, skipping every page that would have stopped it, and asserts a 403 carrying
     the full verdict.

  3. THE WRITE PATH'S RULES. One response per person, editing allowed or refused per poll, an
     option from another question rejected, a closed poll refused.

  4. ANONYMITY IS A STORAGE SHAPE. A test that submits to an anonymous poll and then asserts that
     `respondent_id` really is NULL in the row, that the person can still find and edit their own
     sheet, and that the stored submit time is rounded to the hour.

  5. THE PHASE 0 IMPORT. That the parser reads the ACTIVE array and not the commented-out one,
     which is the difference between publishing the current winners and publishing an old draft.

Run:
    AFC_TEST_DB_NAME=test_afc_polls python manage.py test afc_polls -v 2
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from afc_auth.models import SessionToken, User
from afc_team.models import Team, TeamMembers

from .eligibility import check_eligibility
from .models import (
    Poll,
    PollAnswer,
    PollEligibilityRule,
    PollOption,
    PollParticipation,
    PollQuestion,
    PollResponse,
)


def make_user(username, **extra):
    return User.objects.create(
        username=username,
        email=f"{username}@example.com",
        password="x",
        full_name=username.title(),
        **extra,
    )


def token_for(user):
    """A real SessionToken, because that is what validate_token reads. Never a fake header.

    get_or_create rather than create: a test that acts as the same person twice (vote, then check
    the roll-up) would otherwise collide on the unique token column, which fails with a database
    error that says nothing about what the test was checking."""
    session, _ = SessionToken.objects.get_or_create(
        token=f"tok-{user.pk}",
        defaults={"user": user, "expires_at": timezone.now() + timedelta(hours=3)},
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {session.token}"}


def open_poll(slug="test-poll", **extra):
    """An OPEN poll with one single-choice question and two options."""
    poll = Poll.objects.create(
        slug=slug,
        title="Test poll",
        visibility=Poll.PUBLIC,
        opens_at=timezone.now() - timedelta(hours=1),
        closes_at=timezone.now() + timedelta(days=1),
        **extra,
    )
    question = PollQuestion.objects.create(poll=poll, order=0, prompt="Pick one", required=True)
    first = PollOption.objects.create(question=question, order=0, label="First")
    second = PollOption.objects.create(question=question, order=1, label="Second")
    return poll, question, first, second


def make_event(name, creator):
    """An Event with every non-null column filled. Event has fifteen required fields, so building
    one inline in each test would bury what the test is actually about."""
    from afc_tournament_and_scrims.models import Event

    today = timezone.localdate()
    return Event.objects.create(
        event_name=name,
        competition_type="tournament",
        participant_type="squad",
        event_type="public",
        event_mode="br",
        max_teams_or_players=12,
        start_date=today,
        end_date=today + timedelta(days=1),
        registration_open_date=today - timedelta(days=2),
        registration_end_date=today,
        prizepool="0",
        event_rules="Play fair",
        event_status="upcoming",
        registration_link="https://example.com/register",
        number_of_stages=1,
        creator=creator,
    )


def set_audience(poll, spec):
    PollEligibilityRule.objects.update_or_create(poll=poll, defaults={"spec": spec})


def requirement(verdict, key):
    return next((r for r in verdict["requirements"] if r["key"] == key), None)


# ── 1. eligibility: refused, with a reason ────────────────────────────────────────────────────


class EligibilityRefusalTests(TestCase):
    """A person refused by EACH rule type, and the per-requirement breakdown that explains it."""

    def setUp(self):
        self.poll, self.question, self.option, _ = open_poll()
        self.nigerian = make_user("ada", country="Nigeria")
        self.ghanaian = make_user("kwame", country="Ghana")

    def test_country_rule_refuses_and_names_your_country(self):
        set_audience(self.poll, {"countries": ["nigeria"]})

        allowed = check_eligibility(self.poll, self.nigerian)
        refused = check_eligibility(self.poll, self.ghanaian)

        self.assertTrue(allowed["eligible"])
        self.assertFalse(refused["eligible"])
        line = requirement(refused, "countries")
        self.assertFalse(line["passed"])
        self.assertEqual(line["your_value"], "Ghana")
        self.assertIn("nigeria", line["requirement_text"].lower())
        # The panel is shown to EVERYONE: the eligible voter gets the same line, ticked.
        self.assertTrue(requirement(allowed, "countries")["passed"])

    def test_team_tier_rule_refuses_and_names_your_tier(self):
        tier_one = Team.objects.create(
            team_name="Tier one", join_settings="open", team_tier="1",
            team_creator=self.nigerian, team_owner=self.nigerian,
        )
        tier_three = Team.objects.create(
            team_name="Tier three", join_settings="open", team_tier="3",
            team_creator=self.ghanaian, team_owner=self.ghanaian,
        )
        TeamMembers.objects.create(team=tier_one, member=self.nigerian)
        TeamMembers.objects.create(team=tier_three, member=self.ghanaian)
        set_audience(self.poll, {"tiers": ["1"]})

        refused = check_eligibility(self.poll, self.ghanaian)
        self.assertFalse(refused["eligible"])
        line = requirement(refused, "tiers")
        self.assertFalse(line["passed"])
        self.assertEqual(line["your_value"], "Tier 3")
        self.assertIn("Tier 1", line["requirement_text"])
        # No raw integer anywhere in the line the user reads (spec 2.4).
        self.assertTrue(check_eligibility(self.poll, self.nigerian)["eligible"])

    def test_team_role_rule_counts_both_captain_representations(self):
        """Team.team_captain and TeamMembers.management_role can disagree. A rule that honoured
        only one would quietly exclude a real captain, which is indistinguishable from a bug."""
        by_row = make_user("captain_by_row")
        by_fk = make_user("captain_by_fk")
        plain = make_user("plain_member")
        team = Team.objects.create(
            team_name="Roster", join_settings="open",
            team_creator=by_row, team_owner=by_row, team_captain=by_fk,
        )
        TeamMembers.objects.create(team=team, member=by_row, management_role="team_captain")
        TeamMembers.objects.create(team=team, member=by_fk, management_role="member")
        TeamMembers.objects.create(team=team, member=plain, management_role="member")
        set_audience(self.poll, {"team_roles": ["team_captain"]})

        self.assertTrue(check_eligibility(self.poll, by_row)["eligible"])
        self.assertTrue(check_eligibility(self.poll, by_fk)["eligible"])

        refused = check_eligibility(self.poll, plain)
        self.assertFalse(refused["eligible"])
        line = requirement(refused, "team_roles")
        self.assertEqual(line["your_value"], "Player")
        self.assertIn("however your team records it", line["requirement_text"])

    def test_event_rule_refuses_somebody_not_registered(self):
        from afc_tournament_and_scrims.models import RegisteredCompetitors

        event = make_event("Dynasty Cup", self.nigerian)
        RegisteredCompetitors.objects.create(
            event=event, user=self.nigerian, status="approved", is_waitlisted=False
        )
        set_audience(self.poll, {"event_ids": [event.event_id]})

        self.assertTrue(check_eligibility(self.poll, self.nigerian)["eligible"])
        refused = check_eligibility(self.poll, self.ghanaian)
        self.assertFalse(refused["eligible"])
        line = requirement(refused, "event_ids")
        self.assertEqual(line["your_value"], "Not registered")
        self.assertIn("Dynasty Cup", line["requirement_text"])

    def test_waitlisted_registration_is_not_in_the_event(self):
        from afc_tournament_and_scrims.models import RegisteredCompetitors

        event = make_event("Waitlist Cup", self.nigerian)
        RegisteredCompetitors.objects.create(
            event=event, user=self.ghanaian, status="approved", is_waitlisted=True
        )
        set_audience(self.poll, {"event_ids": [event.event_id]})
        self.assertFalse(check_eligibility(self.poll, self.ghanaian)["eligible"])

    def test_profile_field_refusal_carries_a_link_that_fixes_it(self):
        """Decision 10: the one requirement whose refusal is expected to be temporary."""
        set_audience(self.poll, {"everyone": True, "require_profile_fields": ["uid"]})

        refused = check_eligibility(self.poll, self.ghanaian)
        self.assertFalse(refused["eligible"])
        line = requirement(refused, "profile_uid")
        self.assertFalse(line["passed"])
        self.assertEqual(line["your_value"], "Not set yet")
        self.assertEqual(line["fix_url"], "/profile/edit")
        self.assertTrue(line["fix_hint"])

        # Fill it in and the SAME check passes, with no cache to bust.
        self.ghanaian.uid = "123456789"
        self.ghanaian.save(update_fields=["uid"])
        self.assertTrue(check_eligibility(self.poll, self.ghanaian)["eligible"])

    def test_profile_requirement_does_not_shrink_the_audience(self):
        """It narrows nobody: a person with an empty UID IS the audience, they simply cannot vote
        yet. If it went into the queryset the admin's count would silently drop and they would
        never learn that people were one field away from voting (spec 2.1)."""
        from afc_auth.audience import parse_audience_spec, resolve_audience

        set_audience(self.poll, {"countries": ["ghana"], "require_profile_fields": ["uid"]})
        spec = parse_audience_spec({"countries": ["ghana"], "require_profile_fields": ["uid"]})

        # The audience count does NOT drop: kwame is in it, UID or no UID.
        self.assertEqual(resolve_audience(spec).count(), 1)
        # But he still cannot vote yet, and the reason is the requirement, not the audience.
        verdict = check_eligibility(self.poll, self.ghanaian)
        self.assertFalse(verdict["eligible"])
        self.assertTrue(requirement(verdict, "countries")["passed"])
        self.assertFalse(requirement(verdict, "profile_uid")["passed"])

    def test_signed_out_visitor_is_told_to_sign_in_and_nothing_is_guessed(self):
        set_audience(self.poll, {"countries": ["nigeria"]})
        verdict = check_eligibility(self.poll, None)

        self.assertFalse(verdict["eligible"])
        self.assertFalse(requirement(verdict, "signed_in")["passed"])
        self.assertEqual(requirement(verdict, "signed_in")["fix_url"], "/login")
        # Undecided, not failed: refusing somebody we have not identified is a guess dressed as a
        # decision, and the panel must not render a red cross for it.
        self.assertIsNone(requirement(verdict, "countries")["passed"])

    def test_picked_users_union_with_the_category_filters(self):
        """An explicitly picked person passes even when they fail every category rule, so the
        panel has to say "any of these" rather than "all of these"."""
        set_audience(self.poll, {"countries": ["nigeria"], "user_ids": [self.ghanaian.pk]})
        verdict = check_eligibility(self.poll, self.ghanaian)

        self.assertTrue(verdict["eligible"])
        self.assertEqual(verdict["match_rule"], "any")
        self.assertFalse(requirement(verdict, "countries")["passed"])
        self.assertTrue(requirement(verdict, "invited")["passed"])


class RankingEligibilityTests(TestCase):
    """The two afc_rankings-derived filters, and the freezing that decision 2 demands."""

    def setUp(self):
        self.poll, *_ = open_poll("ranked-poll")
        self.owner = make_user("owner")
        self.top = make_user("top_player")
        self.bottom = make_user("bottom_player")
        self.top_team = Team.objects.create(
            team_name="Top team", join_settings="open",
            team_creator=self.owner, team_owner=self.owner,
        )
        self.bottom_team = Team.objects.create(
            team_name="Bottom team", join_settings="open",
            team_creator=self.owner, team_owner=self.owner,
        )
        TeamMembers.objects.create(team=self.top_team, member=self.top)
        TeamMembers.objects.create(team=self.bottom_team, member=self.bottom)

        from afc_rankings.models import Season, TeamQuarterlyScore

        today = timezone.localdate()
        self.season = Season.objects.create(
            name="Season Q3 2026", quarter=3, year=2026,
            start_date=today - timedelta(days=10), end_date=today + timedelta(days=10),
            transfer_window_open=today, transfer_window_close=today, is_active=True,
        )
        TeamQuarterlyScore.objects.create(
            team=self.top_team, season=self.season, rank=3, tier_assigned=0
        )
        TeamQuarterlyScore.objects.create(
            team=self.bottom_team, season=self.season, rank=88, tier_assigned=2
        )

    def test_rank_window_refuses_and_names_your_rank(self):
        set_audience(self.poll, {"rank_range": {"scope": "team", "from": 1, "to": 50}})

        self.assertTrue(check_eligibility(self.poll, self.top)["eligible"])
        refused = check_eligibility(self.poll, self.bottom)
        self.assertFalse(refused["eligible"])
        line = requirement(refused, "rank_range")
        self.assertEqual(line["your_value"], "#88 in Season Q3 2026")
        self.assertIn("#1 to #50", line["requirement_text"])

    def test_season_tier_refuses_and_never_shows_the_raw_number(self):
        set_audience(self.poll, {"season_tiers": {"scope": "team", "values": [0]}})

        self.assertTrue(check_eligibility(self.poll, self.top)["eligible"])
        refused = check_eligibility(self.poll, self.bottom)
        line = requirement(refused, "season_tiers")
        self.assertFalse(refused["eligible"])
        self.assertIn("Rising", line["your_value"])
        self.assertIn("Elite", line["requirement_text"])
        # The numbers run opposite ways (season 0 is best, hand-set tier 1 is best), so a raw
        # integer in this copy would be read backwards by half the admins who see it.
        self.assertNotIn("0", line["requirement_text"])

    def test_freezing_pins_the_audience_against_a_later_recalculation(self):
        """A team promoted after the poll opened does not join an open poll, and a team that drops
        does not lose the ballot it was shown."""
        from afc_auth.audience import freeze_ranking_filters, parse_audience_spec
        from afc_rankings.models import TeamQuarterlyScore

        spec = parse_audience_spec({"rank_range": {"scope": "team", "from": 1, "to": 50}})
        frozen = freeze_ranking_filters(spec)
        self.assertEqual(frozen["rank_range"]["frozen_team_ids"], [self.top_team.team_id])
        self.assertTrue(frozen["rank_range"]["frozen_at"])

        # The rankings move underneath the poll: the two teams swap places.
        TeamQuarterlyScore.objects.filter(team=self.top_team).update(rank=88)
        TeamQuarterlyScore.objects.filter(team=self.bottom_team).update(rank=3)

        set_audience(self.poll, frozen)
        self.assertTrue(check_eligibility(self.poll, self.top)["eligible"])
        self.assertFalse(check_eligibility(self.poll, self.bottom)["eligible"])

        # And re-freezing is a no-op, so re-saving an open poll cannot silently re-pin it.
        self.assertEqual(freeze_ranking_filters(frozen), frozen)


# ── 2 and 3. the write path ───────────────────────────────────────────────────────────────────


class SubmitTests(TestCase):
    def setUp(self):
        self.poll, self.question, self.option, self.other = open_poll()
        self.voter = make_user("voter", country="Nigeria")
        self.outsider = make_user("outsider", country="Ghana")
        self.auth = token_for(self.voter)
        self.outsider_auth = token_for(self.outsider)

    def _body(self, option=None):
        return {
            "answers": [
                {"question_id": self.question.question_id,
                 "option_ids": [(option or self.option).option_id]}
            ]
        }

    def test_the_server_rechecks_eligibility_at_submit(self):
        """THE test. A client that skips the page and posts straight to the endpoint, with a
        perfectly valid session, is refused by the server's own check and gets the same
        per-requirement verdict the page would have rendered."""
        set_audience(self.poll, {"countries": ["nigeria"]})

        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/", self._body(),
            content_type="application/json", **self.outsider_auth,
        )

        self.assertEqual(response.status_code, 403)
        verdict = response.json()["eligibility"]
        self.assertFalse(verdict["eligible"])
        line = next(r for r in verdict["requirements"] if r["key"] == "countries")
        self.assertFalse(line["passed"])
        self.assertEqual(line["your_value"], "Ghana")
        # And nothing was written, which is the half that actually matters.
        self.assertEqual(PollResponse.objects.count(), 0)
        self.assertEqual(PollParticipation.objects.count(), 0)

    def test_a_signed_out_post_is_refused(self):
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/", self._body(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(PollResponse.objects.count(), 0)

    def test_submitting_writes_a_response_a_participation_and_an_answer(self):
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/", self._body(),
            content_type="application/json", **self.auth,
        )
        self.assertEqual(response.status_code, 201)

        sheet = PollResponse.objects.get(poll=self.poll)
        self.assertEqual(sheet.respondent_id, self.voter.pk)
        self.assertEqual(sheet.status, PollResponse.SUBMITTED)
        self.assertEqual(
            list(sheet.answers.values_list("option_id", flat=True)), [self.option.option_id]
        )
        self.assertTrue(PollParticipation.objects.filter(poll=self.poll, user=self.voter).exists())

    def test_editing_replaces_the_sheet_when_the_poll_allows_it(self):
        self.client.post(f"/polls/{self.poll.slug}/responses/", self._body(),
                         content_type="application/json", **self.auth)
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/", self._body(self.other),
            content_type="application/json", **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PollResponse.objects.count(), 1)
        self.assertEqual(PollAnswer.objects.count(), 1)
        self.assertEqual(PollAnswer.objects.get().option_id, self.other.option_id)

    def test_editing_is_refused_when_the_poll_says_first_answer_is_final(self):
        self.poll.allow_edit_until_close = False
        self.poll.save(update_fields=["allow_edit_until_close"])
        self.client.post(f"/polls/{self.poll.slug}/responses/", self._body(),
                         content_type="application/json", **self.auth)

        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/", self._body(self.other),
            content_type="application/json", **self.auth,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PollAnswer.objects.get().option_id, self.option.option_id)

    def test_an_option_from_another_poll_is_rejected(self):
        _, other_question, foreign_option, _ = open_poll("another-poll")
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [foreign_option.option_id]}]},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PollResponse.objects.count(), 0)

    def test_single_choice_refuses_two_picks(self):
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.option.option_id, self.other.option_id]}]},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_multiple_choice_respects_max_choices(self):
        self.question.answer_type = PollQuestion.MULTIPLE_CHOICE
        self.question.config = {"max_choices": 1}
        self.question.save()
        response = self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.option.option_id, self.other.option_id]}]},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_a_closed_poll_refuses_answers_but_stays_readable(self):
        self.poll.closes_at = timezone.now() - timedelta(minutes=1)
        self.poll.save(update_fields=["closes_at"])

        submit = self.client.post(f"/polls/{self.poll.slug}/responses/", self._body(),
                                  content_type="application/json", **self.auth)
        self.assertEqual(submit.status_code, 403)

        read = self.client.get(f"/polls/{self.poll.slug}/")
        self.assertEqual(read.status_code, 200)
        self.assertFalse(read.json()["poll"]["accepting_answers"])
        self.assertTrue(read.json()["poll"]["is_closed"])

    def test_a_preview_only_poll_is_visible_and_unanswerable(self):
        self.poll.visibility = Poll.PREVIEW_ONLY
        self.poll.save(update_fields=["visibility"])

        self.assertEqual(self.client.get(f"/polls/{self.poll.slug}/").status_code, 200)
        self.assertEqual(
            self.client.post(f"/polls/{self.poll.slug}/responses/", self._body(),
                             content_type="application/json", **self.auth).status_code,
            403,
        )

    def test_a_draft_is_not_visible_to_the_public(self):
        self.poll.visibility = Poll.DRAFT
        self.poll.save(update_fields=["visibility"])
        self.assertEqual(self.client.get(f"/polls/{self.poll.slug}/").status_code, 404)
        self.assertEqual(self.client.get("/polls/").json()["total_count"], 0)


# ── 4. anonymity is a storage shape, not a display rule ───────────────────────────────────────


class AnonymityTests(TestCase):
    def setUp(self):
        self.poll, self.question, self.option, self.other = open_poll(
            "anon-poll", anonymous=True
        )
        self.voter = make_user("anon_voter")
        self.auth = token_for(self.voter)

    def test_the_respondent_is_never_written(self):
        self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.option.option_id]}]},
            content_type="application/json", **self.auth,
        )
        sheet = PollResponse.objects.get(poll=self.poll)

        # The link is ABSENT from the data, not merely hidden behind a permission check.
        self.assertIsNone(sheet.respondent_id)
        self.assertTrue(sheet.respondent_key)
        # The roll still knows they took part. That is the whole point of the two tables.
        self.assertTrue(PollParticipation.objects.filter(poll=self.poll, user=self.voter).exists())

    def test_the_submit_time_is_rounded_to_the_hour(self):
        """A time to the second beside a participation row to the second is a join with extra
        steps, and it would undo everything above."""
        self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.option.option_id]}]},
            content_type="application/json", **self.auth,
        )
        submitted_at = PollResponse.objects.get(poll=self.poll).submitted_at
        self.assertEqual((submitted_at.minute, submitted_at.second), (0, 0))

    def test_you_can_still_find_and_edit_your_own_anonymous_sheet(self):
        for option in (self.option, self.other):
            self.client.post(
                f"/polls/{self.poll.slug}/responses/",
                {"answers": [{"question_id": self.question.question_id,
                              "option_ids": [option.option_id]}]},
                content_type="application/json", **self.auth,
            )
        self.assertEqual(PollResponse.objects.count(), 1)
        self.assertEqual(PollAnswer.objects.get().option_id, self.other.option_id)

        detail = self.client.get(f"/polls/{self.poll.slug}/", **self.auth).json()
        self.assertEqual(
            detail["your_response"]["answers"][str(self.question.question_id)],
            [self.other.option_id],
        )

    def test_two_people_get_two_different_keys(self):
        second = make_user("anon_voter_two")
        for auth in (self.auth, token_for(second)):
            self.client.post(
                f"/polls/{self.poll.slug}/responses/",
                {"answers": [{"question_id": self.question.question_id,
                              "option_ids": [self.option.option_id]}]},
                content_type="application/json", **auth,
            )
        keys = set(PollResponse.objects.values_list("respondent_key", flat=True))
        self.assertEqual(len(keys), 2)

    def test_anonymous_cannot_be_switched_off_once_a_response_exists(self):
        from .views import _apply_poll_fields

        self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.option.option_id]}]},
            content_type="application/json", **self.auth,
        )
        _apply_poll_fields(self.poll, {"anonymous": False})
        self.assertTrue(self.poll.anonymous)

    def test_the_voter_list_cannot_be_on_at_the_same_time(self):
        from .views import _apply_poll_fields

        _apply_poll_fields(self.poll, {"show_voter_list": True})
        self.assertFalse(self.poll.show_voter_list)


# ── the permission gate ───────────────────────────────────────────────────────────────────────


class PermissionTests(TestCase):
    def setUp(self):
        self.poll, self.question, *_ = open_poll("perm-poll")
        self.nobody = make_user("nobody")
        self.admin = make_user("event_admin_user", role="admin")

    def test_an_ordinary_player_cannot_manage_a_poll(self):
        from .permissions import can_manage_poll

        self.assertFalse(can_manage_poll(self.nobody, self.poll))
        response = self.client.patch(
            f"/polls/admin/polls/{self.poll.slug}/", {"title": "Hijacked"},
            content_type="application/json", **token_for(self.nobody),
        )
        self.assertEqual(response.status_code, 403)
        self.poll.refresh_from_db()
        self.assertEqual(self.poll.title, "Test poll")

    def test_an_afc_admin_can(self):
        from .permissions import can_manage_poll

        self.assertTrue(can_manage_poll(self.admin, self.poll))
        response = self.client.patch(
            f"/polls/admin/polls/{self.poll.slug}/", {"title": "Renamed"},
            content_type="application/json", **token_for(self.admin),
        )
        self.assertEqual(response.status_code, 200)
        self.poll.refresh_from_db()
        self.assertEqual(self.poll.title, "Renamed")

    def test_questions_cannot_change_once_people_have_answered(self):
        voter = make_user("already_voted")
        self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.question.options.first().option_id]}]},
            content_type="application/json", **token_for(voter),
        )
        response = self.client.put(
            f"/polls/admin/polls/{self.poll.slug}/questions/",
            {"questions": [{"prompt": "A different question", "options": [{"label": "X"}]}]},
            content_type="application/json", **token_for(self.admin),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.poll.questions.get().prompt, "Pick one")

    def test_a_poll_with_answers_cannot_be_deleted(self):
        voter = make_user("voted_then_delete")
        self.client.post(
            f"/polls/{self.poll.slug}/responses/",
            {"answers": [{"question_id": self.question.question_id,
                          "option_ids": [self.question.options.first().option_id]}]},
            content_type="application/json", **token_for(voter),
        )
        response = self.client.delete(
            f"/polls/admin/polls/{self.poll.slug}/", **token_for(self.admin)
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Poll.objects.filter(slug=self.poll.slug).exists())


# ── results visibility ────────────────────────────────────────────────────────────────────────


class ResultsVisibilityTests(TestCase):
    def setUp(self):
        self.poll, self.question, self.option, _ = open_poll("results-poll")
        self.admin = make_user("results_admin", role="admin")

    def _cast(self, count):
        for index in range(count):
            voter = make_user(f"caster{index}")
            self.client.post(
                f"/polls/{self.poll.slug}/responses/",
                {"answers": [{"question_id": self.question.question_id,
                              "option_ids": [self.option.option_id]}]},
                content_type="application/json", **token_for(voter),
            )

    def test_admins_only_hides_the_numbers_from_the_public(self):
        self._cast(6)
        self.poll.results_visibility = Poll.ADMINS_ONLY
        self.poll.save(update_fields=["results_visibility"])

        public = self.client.get(f"/polls/{self.poll.slug}/").json()
        self.assertFalse(public["results_visible"])
        self.assertNotIn("votes", public["questions"][0]["options"][0])

        as_admin = self.client.get(f"/polls/{self.poll.slug}/", **token_for(self.admin)).json()
        self.assertTrue(as_admin["results_visible"])
        self.assertEqual(as_admin["questions"][0]["options"][0]["votes"], 6)

    def test_after_close_publishes_only_once_the_poll_has_closed(self):
        self._cast(6)
        self.poll.results_visibility = Poll.AFTER_CLOSE
        self.poll.save(update_fields=["results_visibility"])
        self.assertFalse(self.client.get(f"/polls/{self.poll.slug}/").json()["results_visible"])

        self.poll.closes_at = timezone.now() - timedelta(minutes=1)
        self.poll.save(update_fields=["closes_at"])
        self.assertTrue(self.client.get(f"/polls/{self.poll.slug}/").json()["results_visible"])

    def test_an_option_tally_is_not_suppressed_on_a_public_poll(self):
        """The small-cell floor is about DEMOGRAPHIC BUCKETS, not about an option's own count.

        Spec 5.2 wrote the floor of five to stop "Tier 1 voted X" naming people in a population of
        18 teams. Applied literally to an option tally it also swallowed the result, so a nominee
        who finished with three votes read "fewer than 5" on a public awards page: odd, and
        pointless, because the vote is public and the count names nobody. Narrowed 2026-08-08 per
        awards-grand-design.md item 8."""
        self._cast(3)
        self.poll.results_visibility = Poll.ALWAYS
        self.poll.save(update_fields=["results_visibility"])

        public = self.client.get(f"/polls/{self.poll.slug}/").json()
        self.assertFalse(public["results_suppressed_small_cell"])
        self.assertEqual(public["questions"][0]["options"][0]["votes"], 3)
        self.assertEqual(public["response_count"], 3)

    def test_the_floor_applies_to_admins_too_on_an_anonymous_poll(self):
        self.poll.anonymous = True
        self.poll.results_visibility = Poll.ALWAYS
        self.poll.save()
        self._cast(3)

        results = self.client.get(
            f"/polls/admin/polls/{self.poll.slug}/results/", **token_for(self.admin)
        ).json()
        self.assertTrue(results["results_suppressed_small_cell"])
        self.assertFalse(results["breakdowns_available"])
        self.assertEqual(results["headline"]["responses"], 3)


# ── 5. the Phase 0 import ─────────────────────────────────────────────────────────────────────


class AwardsImportTests(TestCase):
    """The parser reads the ACTIVE MANUAL_WINNERS array and never the commented-out one. Getting
    this wrong publishes an old draft of the winners over the real ones."""

    def _write_page(self, body):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "page.tsx"
        path.write_text(body, encoding="utf-8")
        return path

    def test_commented_out_entries_are_skipped(self):
        from .management.commands.import_awards_winners import parse_manual_winners

        page = self._write_page(
            'const MANUAL_WINNERS: SectionWinners[] = [\n'
            '  {\n'
            '    id: "content-creators",\n'
            '    name: "Content Creators",\n'
            '    categories: [\n'
            '      {\n'
            '        id: "1",\n'
            '        name: "Live award",\n'
            '        winner: { id: "w1", name: "REAL", votes: 10 },\n'
            '      },\n'
            '      // {\n'
            '      //   id: "2",\n'
            '      //   name: "Withheld award",\n'
            '      //   winner: { id: "w2", name: "NEVER", votes: 99 },\n'
            '      // },\n'
            '    ],\n'
            '  },\n'
            '];\n'
        )
        sections, skipped = parse_manual_winners(page)

        self.assertEqual(len(sections), 1)
        self.assertEqual([c["winner"] for c in sections[0]["categories"]], ["REAL"])
        self.assertEqual(skipped, 5)

    def test_a_commented_out_array_is_not_read_at_all(self):
        from .management.commands.import_awards_winners import parse_manual_winners

        page = self._write_page(
            '// const MANUAL_WINNERS: SectionWinners[] = [\n'
            '//   { id: "old", name: "Old", categories: [] },\n'
            '// ];\n'
            'const MANUAL_WINNERS: SectionWinners[] = [\n'
            '  {\n'
            '    id: "esports-awards",\n'
            '    name: "Esports Awards",\n'
            '    categories: [\n'
            '      {\n'
            '        id: "19",\n'
            '        name: "Best Esports Team",\n'
            '        winner: { id: "w19", name: "V-ENT ESPORTS", votes: 143 },\n'
            '      },\n'
            '    ],\n'
            '  },\n'
            '];\n'
        )
        sections, _ = parse_manual_winners(page)
        self.assertEqual([s["id"] for s in sections], ["esports-awards"])

    def test_two_categories_may_share_a_prompt_that_differs_only_in_case(self):
        """The live 2025 data holds "Favorite DUO (Male)" and "Favorite DUO (MALE)" with different
        winners. PollQuestion.prompt must NOT be unique, unlike the Category.name it replaces, or
        the import rejects real published history."""
        poll = Poll.objects.create(slug="dupe", title="Dupe")
        PollQuestion.objects.create(poll=poll, order=16, prompt="Favorite DUO (Male)")
        PollQuestion.objects.create(poll=poll, order=17, prompt="Favorite DUO (MALE)")
        self.assertEqual(poll.questions.count(), 2)
