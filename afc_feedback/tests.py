"""
afc_feedback.tests - coverage for the always-on, reusable feedback form (backlog item 29).

WHAT IS PROVEN HERE, and why each case earns its place:
  - a signed-in submission is attributed, an anonymous one is stored and NOT rejected. The anonymous
    path is the whole point of the feature, so it is tested first.
  - the rate limit actually BITES on the open endpoint, on both of its limits, and a rejected
    submission does not consume the sender's allowance.
  - an INACTIVE form refuses submissions server-side, not merely by being hidden from the schema.
  - the admin queue is gated: anonymous, non-admin and admin callers get 401, 403 and 200.
  - the form is genuinely REUSABLE: a second form with a different field set validates against its
    OWN fields, which is what separates this from a hardcoded feedback table.

The rate limit lives in the shared Redis cache, so every test that touches it clears the cache first;
otherwise a previous test's counter leaks into the next one and the failure looks like a logic bug.
"""
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from afc_auth.models import User, SessionToken, Roles, UserRoles

from .models import FeedbackForm, FeedbackField, FeedbackSubmission
from .views import FEEDBACK_RATE_LIMIT_PER_HOUR


def _make_session(user):
    """Mint a real SessionToken for `user` and return the Bearer value.

    Tokens are minted directly rather than by POSTing the login endpoint: that keeps these tests
    about feedback rather than about authentication, and it avoids handling a password."""
    session = SessionToken.objects.create(
        user=user,
        token=f"test-token-{user.user_id}",
        expires_at=timezone.now() + timezone.timedelta(hours=3),
    )
    return f"Bearer {session.token}"


class FeedbackTestBase(APITestCase):
    """Shared fixture: the default site_feedback form plus three kinds of caller."""

    def setUp(self):
        # The rate limit is cache-backed and the cache outlives a test, so start every test clean.
        cache.clear()

        self.form = FeedbackForm.objects.create(
            key="site_feedback",
            title="Send us feedback",
            description="Found a bug, or have an idea?",
            thank_you_message="Thanks.",
            is_active=True,
        )
        FeedbackField.objects.create(
            form=self.form, key="rating", label="How is your experience?",
            field_type=FeedbackField.RATING, required=False, order=1, max_rating=5,
        )
        FeedbackField.objects.create(
            form=self.form, key="comment", label="What would you like to tell us?",
            field_type=FeedbackField.TEXTAREA, required=True, order=2, max_length=2000,
        )
        FeedbackField.objects.create(
            form=self.form, key="contact", label="Email, if you want a reply",
            field_type=FeedbackField.TEXT, required=False, order=3, max_length=200,
        )

        self.player = User.objects.create_user(
            username="player1", email="player1@example.com", password="x", role="player",
        )
        self.non_admin_token = _make_session(self.player)

        self.admin = User.objects.create_user(
            username="afcadmin", email="admin@example.com", password="x", role="admin",
        )
        self.admin_token = _make_session(self.admin)

        self.submit_url = reverse("feedback_submit", kwargs={"key": "site_feedback"})
        self.schema_url = reverse("feedback_form_schema", kwargs={"key": "site_feedback"})
        self.admin_list_url = reverse("feedback_admin_submissions")


class PublicSubmissionTests(FeedbackTestBase):
    """The open, anonymous-capable write path."""

    def test_anonymous_visitor_can_submit(self):
        # Arrange: no Authorization header at all.
        payload = {
            "answers": {"rating": 4, "comment": "The shop page is slow on my phone."},
            "page_path": "/shop",
            "locale": "fr",
        }

        # Act
        res = self.client.post(self.submit_url, payload, format="json")

        # Assert: accepted, stored, and explicitly NOT attributed to anyone.
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        submission = FeedbackSubmission.objects.get(id=res.data["submission_id"])
        self.assertIsNone(submission.user)
        self.assertEqual(submission.answers["comment"], "The shop page is slow on my phone.")
        self.assertEqual(submission.answers["rating"], 4)
        self.assertEqual(submission.page_path, "/shop")
        self.assertEqual(submission.locale, "fr")

    def test_logged_in_submission_is_attributed(self):
        # Arrange / Act
        res = self.client.post(
            self.submit_url,
            {"answers": {"comment": "Great tournament."}, "page_path": "/tournaments"},
            format="json",
            HTTP_AUTHORIZATION=self.non_admin_token,
        )

        # Assert
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        submission = FeedbackSubmission.objects.get(id=res.data["submission_id"])
        self.assertEqual(submission.user_id, self.player.user_id)

    def test_expired_or_garbage_token_still_submits_anonymously(self):
        # A stale token must not cost a visitor their feedback: the endpoint degrades to anonymous
        # rather than 401ing, which is the behaviour _optional_user exists to provide.
        res = self.client.post(
            self.submit_url,
            {"answers": {"comment": "Sent with a dead token."}},
            format="json",
            HTTP_AUTHORIZATION="Bearer not-a-real-token",
        )

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(FeedbackSubmission.objects.get(id=res.data["submission_id"]).user)

    def test_page_path_query_string_is_stripped(self):
        # A query string can carry an invite or reset token. It must never land in the row.
        res = self.client.post(
            self.submit_url,
            {"answers": {"comment": "x"}, "page_path": "/invite?token=SECRET123"},
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        submission = FeedbackSubmission.objects.get(id=res.data["submission_id"])
        self.assertEqual(submission.page_path, "/invite")
        self.assertNotIn("SECRET123", submission.page_path)

    def test_absolute_url_page_path_is_discarded(self):
        res = self.client.post(
            self.submit_url,
            {"answers": {"comment": "x"}, "page_path": "https://evil.example.com/phish"},
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(FeedbackSubmission.objects.get(id=res.data["submission_id"]).page_path, "")

    def test_missing_required_field_is_rejected(self):
        res = self.client.post(
            self.submit_url, {"answers": {"rating": 5}}, format="json"
        )

        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FeedbackSubmission.objects.count(), 0)

    def test_rating_outside_the_scale_is_rejected(self):
        res = self.client.post(
            self.submit_url, {"answers": {"comment": "ok", "rating": 99}}, format="json"
        )

        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FeedbackSubmission.objects.count(), 0)

    def test_unknown_answer_keys_are_dropped(self):
        # The row stores only fields the form declares, so a scripted client cannot stuff the blob.
        res = self.client.post(
            self.submit_url,
            {"answers": {"comment": "hi", "injected": "should not be stored"}},
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        answers = FeedbackSubmission.objects.get(id=res.data["submission_id"]).answers
        self.assertIn("comment", answers)
        self.assertNotIn("injected", answers)

    def test_long_text_is_truncated_to_the_field_cap(self):
        res = self.client.post(
            self.submit_url, {"answers": {"comment": "a" * 5000}}, format="json"
        )

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        answers = FeedbackSubmission.objects.get(id=res.data["submission_id"]).answers
        self.assertEqual(len(answers["comment"]), 2000)

    def test_submission_snapshots_the_questions(self):
        # Editing a label later must not rewrite what a past submitter appears to have answered.
        res = self.client.post(self.submit_url, {"answers": {"comment": "hi"}}, format="json")
        submission = FeedbackSubmission.objects.get(id=res.data["submission_id"])

        self.form.fields.filter(key="comment").update(label="COMPLETELY REWORDED")
        submission.refresh_from_db()

        labels = {f["key"]: f["label"] for f in submission.fields_snapshot}
        self.assertEqual(labels["comment"], "What would you like to tell us?")

    def test_raw_ip_is_never_stored(self):
        res = self.client.post(
            self.submit_url, {"answers": {"comment": "hi"}}, format="json",
            REMOTE_ADDR="41.58.100.7",
        )

        submission = FeedbackSubmission.objects.get(id=res.data["submission_id"])
        self.assertTrue(submission.ip_hash)
        self.assertNotIn("41.58.100.7", submission.ip_hash)
        self.assertEqual(len(submission.ip_hash), 64)  # sha256 hex


class InactiveFormTests(FeedbackTestBase):
    """A retired form must be inert, not merely hidden."""

    def test_inactive_form_refuses_submissions(self):
        # Arrange: retire the form AFTER a client could have loaded its schema.
        self.form.is_active = False
        self.form.save()

        # Act
        res = self.client.post(self.submit_url, {"answers": {"comment": "hi"}}, format="json")

        # Assert: the write is refused server-side, so a stale open tab cannot post to it.
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(FeedbackSubmission.objects.count(), 0)

    def test_inactive_form_is_absent_from_the_schema_endpoint(self):
        self.form.is_active = False
        self.form.save()

        res = self.client.get(self.schema_url)

        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_active_form_schema_is_public_and_ordered(self):
        res = self.client.get(self.schema_url)

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [f["key"] for f in res.data["form"]["fields"]], ["rating", "comment", "contact"]
        )
        self.assertEqual(res.data["form"]["title"], "Send us feedback")


class RateLimitTests(FeedbackTestBase):
    """The open write endpoint must be bounded, or it is an abuse vector."""

    def test_cooldown_blocks_an_immediate_second_submission(self):
        first = self.client.post(self.submit_url, {"answers": {"comment": "one"}}, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second = self.client.post(self.submit_url, {"answers": {"comment": "two"}}, format="json")

        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(second.data["reason"], "cooldown")
        self.assertIn("resets_at", second.data)
        self.assertEqual(FeedbackSubmission.objects.count(), 1)

    def test_hourly_cap_blocks_once_the_allowance_is_spent(self):
        # Spend the whole hourly allowance. Between sends we delete ONLY the cooldown key, so the
        # cooldown cannot mask the result and the HOURLY counter is what is actually under test.
        for i in range(FEEDBACK_RATE_LIMIT_PER_HOUR):
            res = self.client.post(
                self.submit_url, {"answers": {"comment": f"msg {i}"}}, format="json"
            )
            self.assertEqual(res.status_code, status.HTTP_201_CREATED)
            cache.delete(self._anonymous_cooldown_key())

        blocked = self.client.post(
            self.submit_url, {"answers": {"comment": "one too many"}}, format="json"
        )

        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(blocked.data["reason"], "hourly")
        self.assertEqual(FeedbackSubmission.objects.count(), FEEDBACK_RATE_LIMIT_PER_HOUR)

    @staticmethod
    def _anonymous_cooldown_key():
        """The cooldown cache key the Django test client's requests land on (REMOTE_ADDR 127.0.0.1),
        so a test can step past the cooldown without sleeping while leaving the hourly counter
        intact. Built from the real helpers rather than a hardcoded string, so it cannot drift."""
        from .views import _client_ip_hash, _cooldown_key, _rate_limit_identity

        class _FakeRequest:
            META = {"REMOTE_ADDR": "127.0.0.1"}

        return _cooldown_key(_rate_limit_identity(None, _client_ip_hash(_FakeRequest())))

    def test_a_rejected_submission_does_not_consume_the_allowance(self):
        # A validation failure must not cost the visitor a slot, or a typo would lock them out.
        bad = self.client.post(self.submit_url, {"answers": {}}, format="json")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

        good = self.client.post(self.submit_url, {"answers": {"comment": "now valid"}}, format="json")

        self.assertEqual(good.status_code, status.HTTP_201_CREATED)

    def test_signed_in_senders_are_limited_separately_from_anonymous_ones(self):
        # Identity is the user id when known, so one visitor's cooldown must not block another user.
        anon = self.client.post(self.submit_url, {"answers": {"comment": "anon"}}, format="json")
        self.assertEqual(anon.status_code, status.HTTP_201_CREATED)

        signed_in = self.client.post(
            self.submit_url, {"answers": {"comment": "signed in"}}, format="json",
            HTTP_AUTHORIZATION=self.non_admin_token,
        )

        self.assertEqual(signed_in.status_code, status.HTTP_201_CREATED)


class AdminQueueTests(FeedbackTestBase):
    """Reading and triaging feedback is admin-only."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.submission = FeedbackSubmission.objects.create(
            form=self.form,
            user=self.player,
            answers={"comment": "The register button does nothing.", "rating": 2},
            page_path="/tournaments/dynasty-cup",
        )

    def test_anonymous_cannot_list_submissions(self):
        res = self.client.get(self.admin_list_url)

        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_admin_cannot_list_submissions(self):
        res = self.client.get(self.admin_list_url, HTTP_AUTHORIZATION=self.non_admin_token)

        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_list_submissions(self):
        res = self.client.get(self.admin_list_url, HTTP_AUTHORIZATION=self.admin_token)

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["total_count"], 1)
        self.assertEqual(res.data["open_count"], 1)
        row = res.data["results"][0]
        self.assertEqual(row["username"], "player1")
        self.assertEqual(row["page_path"], "/tournaments/dynasty-cup")
        self.assertEqual(row["status"], "open")

    def test_granular_head_admin_role_can_list(self):
        # A user whose coarse `role` is still "player" but who carries the granular head_admin role
        # must get in: that is how AFC actually grants platform-wide admin.
        helper = User.objects.create_user(
            username="helper", email="helper@example.com", password="x", role="player",
        )
        role, _ = Roles.objects.get_or_create(role_name="head_admin")
        UserRoles.objects.create(user=helper, role=role)

        res = self.client.get(self.admin_list_url, HTTP_AUTHORIZATION=_make_session(helper))

        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_area_admin_without_a_platform_role_is_refused(self):
        # Site feedback is not scoped to one area, so an area admin (shop, news, teams) must NOT
        # inherit access to it just by being an admin of something.
        shopkeeper = User.objects.create_user(
            username="shopkeeper", email="shop@example.com", password="x", role="player",
        )
        role, _ = Roles.objects.get_or_create(role_name="shop_admin")
        UserRoles.objects.create(user=shopkeeper, role=role)

        res = self.client.get(self.admin_list_url, HTTP_AUTHORIZATION=_make_session(shopkeeper))

        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_filter_by_form_and_status(self):
        other = FeedbackForm.objects.create(key="post_event_survey", title="Survey")
        FeedbackField.objects.create(
            form=other, key="q1", label="Q1", field_type=FeedbackField.TEXTAREA, required=True,
        )
        FeedbackSubmission.objects.create(form=other, answers={"q1": "other form"})

        only_default = self.client.get(
            self.admin_list_url, {"form": "site_feedback"}, HTTP_AUTHORIZATION=self.admin_token
        )
        self.assertEqual(only_default.data["total_count"], 1)

        only_handled = self.client.get(
            self.admin_list_url, {"status": "handled"}, HTTP_AUTHORIZATION=self.admin_token
        )
        self.assertEqual(only_handled.data["total_count"], 0)

    def test_admin_can_mark_handled_and_reopen(self):
        url = reverse(
            "feedback_admin_update_submission", kwargs={"submission_id": self.submission.id}
        )

        handled = self.client.patch(
            url, {"status": "handled", "admin_note": "Fixed in v7.1.33."}, format="json",
            HTTP_AUTHORIZATION=self.admin_token,
        )

        self.assertEqual(handled.status_code, status.HTTP_200_OK)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, "handled")
        self.assertEqual(self.submission.handled_by_id, self.admin.user_id)
        self.assertIsNotNone(self.submission.handled_at)
        self.assertEqual(self.submission.admin_note, "Fixed in v7.1.33.")

        # Reopening must clear the audit stamps, so an open row never claims someone handled it.
        reopened = self.client.patch(
            url, {"status": "open"}, format="json", HTTP_AUTHORIZATION=self.admin_token
        )

        self.assertEqual(reopened.status_code, status.HTTP_200_OK)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, "open")
        self.assertIsNone(self.submission.handled_by)
        self.assertIsNone(self.submission.handled_at)

    def test_marking_handled_twice_is_idempotent(self):
        url = reverse(
            "feedback_admin_update_submission", kwargs={"submission_id": self.submission.id}
        )
        self.client.patch(url, {"status": "handled"}, format="json",
                          HTTP_AUTHORIZATION=self.admin_token)
        self.client.patch(url, {"status": "handled"}, format="json",
                          HTTP_AUTHORIZATION=self.admin_token)

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, "handled")

    def test_unknown_status_is_rejected(self):
        url = reverse(
            "feedback_admin_update_submission", kwargs={"submission_id": self.submission.id}
        )

        res = self.client.patch(url, {"status": "banana"}, format="json",
                                HTTP_AUTHORIZATION=self.admin_token)

        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_admin_cannot_mark_handled(self):
        url = reverse(
            "feedback_admin_update_submission", kwargs={"submission_id": self.submission.id}
        )

        res = self.client.patch(url, {"status": "handled"}, format="json",
                                HTTP_AUTHORIZATION=self.non_admin_token)

        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, "open")

    def test_admin_forms_listing_carries_open_counts(self):
        res = self.client.get(reverse("feedback_admin_forms"), HTTP_AUTHORIZATION=self.admin_token)

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        by_key = {f["key"]: f for f in res.data["forms"]}
        self.assertEqual(by_key["site_feedback"]["open_count"], 1)
        self.assertEqual(by_key["site_feedback"]["total_count"], 1)


class ReusabilityTests(FeedbackTestBase):
    """The requirement that separates this from a hardcoded feedback table: MORE THAN ONE form."""

    def setUp(self):
        super().setUp()
        cache.clear()
        # A second form with a completely different field set, including a CHOICE field the default
        # form does not have.
        self.survey = FeedbackForm.objects.create(
            key="post_event_survey", title="How was the tournament?", is_active=True,
        )
        FeedbackField.objects.create(
            form=self.survey, key="would_return", label="Would you play again?",
            field_type=FeedbackField.CHOICE, required=True, order=1,
            options=["Yes", "No", "Not sure"],
        )
        FeedbackField.objects.create(
            form=self.survey, key="notes", label="Anything else?",
            field_type=FeedbackField.TEXTAREA, required=False, order=2,
        )
        self.survey_url = reverse("feedback_submit", kwargs={"key": "post_event_survey"})

    def test_second_form_serves_its_own_schema(self):
        res = self.client.get(
            reverse("feedback_form_schema", kwargs={"key": "post_event_survey"})
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        fields = res.data["form"]["fields"]
        self.assertEqual([f["key"] for f in fields], ["would_return", "notes"])
        self.assertEqual(fields[0]["options"], ["Yes", "No", "Not sure"])

    def test_second_form_validates_against_its_own_fields(self):
        # "comment" is required on site_feedback but does not exist here, so it must be dropped
        # rather than demanded, and would_return must be enforced.
        missing_required = self.client.post(
            self.survey_url, {"answers": {"notes": "fun"}}, format="json"
        )
        self.assertEqual(missing_required.status_code, status.HTTP_400_BAD_REQUEST)

        cache.clear()
        valid = self.client.post(
            self.survey_url,
            {"answers": {"would_return": "Yes", "comment": "not a field here"}},
            format="json",
        )

        self.assertEqual(valid.status_code, status.HTTP_201_CREATED)
        answers = FeedbackSubmission.objects.get(id=valid.data["submission_id"]).answers
        self.assertEqual(answers, {"would_return": "Yes"})

    def test_choice_outside_the_declared_options_is_rejected(self):
        res = self.client.post(
            self.survey_url, {"answers": {"would_return": "Maybe someday"}}, format="json"
        )

        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_form_key_is_404(self):
        res = self.client.post(
            reverse("feedback_submit", kwargs={"key": "does-not-exist"}),
            {"answers": {"comment": "hi"}}, format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)


class SeedCommandTests(APITestCase):
    """The seed command must make the widget work out of the box, and be safe to re-run."""

    def test_seed_is_idempotent(self):
        from django.core.management import call_command

        cache.clear()
        call_command("seed_feedback_forms", verbosity=0)
        call_command("seed_feedback_forms", verbosity=0)

        form = FeedbackForm.objects.get(key="site_feedback")
        self.assertTrue(form.is_active)
        self.assertEqual(form.fields.count(), 3)
        self.assertEqual(
            [f.key for f in form.fields.all()], ["rating", "comment", "contact"]
        )
        # The comment is the only required question: a rating with no words is nearly useless, and a
        # contact address must stay optional for an anonymous sender.
        self.assertEqual([f.key for f in form.fields.filter(required=True)], ["comment"])
