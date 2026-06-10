# afc_player_market/tests.py
# ──────────────────────────────────────────────────────────────────────────────
# Tests for the one-month recruitment-post expiry cap (feature "L-market-expiry-cap").
#
# WHAT IS UNDER TEST:
#   A recruitment post (TEAM_RECRUITMENT or PLAYER_AVAILABLE) may live AT MOST one
#   calendar month before it auto-closes. No DB field, no celery job; the cap is a
#   validation guard in afc_player_market.views on the two write paths, plus a one-off
#   backfill management command for already-existing far-future rows:
#
#     • create_recruitment_post: expiry must be in [today, add_one_month(today)].
#     • edit_recruitment_post: new expiry must be in
#                              [today, add_one_month(post.created_at.date())].
#     • clamp_post_expiries cmd: pulls existing rows past the cap back to their cap.
#
# We exercise create/edit through the real HTTP endpoints (via the player-market URL
# prefix mounted in afc.urls) using PLAYER_AVAILABLE posts, which need no team. Auth is
# a Bearer SessionToken, the same scheme the views read. The command is exercised
# directly via call_command on seeded rows.
# ──────────────────────────────────────────────────────────────────────────────
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from afc_auth.models import SessionToken, User
from .models import RecruitmentPost
from .views import add_one_month


def _ymd(d):
    """Format a date the way the endpoints + the FE inputs send it (YYYY-MM-DD)."""
    return d.strftime("%Y-%m-%d")


class ExpiryCapHelperTests(TestCase):
    """add_one_month is the single source of the cap; pin its day-clamp behaviour."""

    def test_normal_month(self):
        from datetime import date
        self.assertEqual(add_one_month(date(2026, 1, 15)), date(2026, 2, 15))

    def test_day_clamped_to_short_month(self):
        # Jan 31 + 1 month → Feb 28 (no Feb 31st; clamp to the month length).
        from datetime import date
        self.assertEqual(add_one_month(date(2026, 1, 31)), date(2026, 2, 28))

    def test_december_rolls_year(self):
        from datetime import date
        self.assertEqual(add_one_month(date(2026, 12, 10)), date(2027, 1, 10))


class CreatePostExpiryCapTests(TestCase):
    """create_recruitment_post must reject an expiry beyond one month from today, or in
    the past, and accept one exactly at the one-month cap."""

    def setUp(self):
        self.user = User.objects.create(
            username="capplayer", email="capplayer@example.com", full_name="Cap Player",
        )
        self.token = SessionToken.objects.create(user=self.user, token="cap-create-token")
        self.url = reverse("create_recruitment_post")
        self.today = timezone.now().date()

    def _post(self, expiry_date):
        return self.client.post(
            self.url,
            data={"post_type": "PLAYER_AVAILABLE", "post_expiry_date": _ymd(expiry_date)},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    def test_expiry_two_months_out_is_rejected(self):
        # today + ~2 months (one month past the cap) → 400.
        two_months = add_one_month(add_one_month(self.today))
        resp = self._post(two_months)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("within 1 month", resp.json()["message"])
        self.assertEqual(RecruitmentPost.objects.count(), 0)

    def test_expiry_exactly_one_month_is_accepted(self):
        # The boundary (today + 1 month) is allowed (a post may last the full month).
        resp = self._post(add_one_month(self.today))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(RecruitmentPost.objects.count(), 1)

    def test_expiry_in_the_past_is_rejected(self):
        resp = self._post(self.today - timedelta(days=1))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("past", resp.json()["message"])
        self.assertEqual(RecruitmentPost.objects.count(), 0)


class EditPostExpiryCapTests(TestCase):
    """edit_recruitment_post must cap the new expiry relative to the post's OWN start,
    so an edit cannot stretch a post past one month of total life."""

    def setUp(self):
        self.user = User.objects.create(
            username="capeditor", email="capeditor@example.com", full_name="Cap Editor",
        )
        self.token = SessionToken.objects.create(user=self.user, token="cap-edit-token")
        self.url = reverse("edit_recruitment_post")
        self.today = timezone.now().date()
        # A live, in-cap post created "today" (created_at is auto_now_add).
        self.post = RecruitmentPost.objects.create(
            post_type="PLAYER_AVAILABLE",
            post_expiry_date=self.today + timedelta(days=7),
            created_by=self.user,
            player=self.user,
        )

    def _patch(self, expiry_date):
        return self.client.patch(
            self.url,
            data={"post_id": self.post.id, "post_expiry_date": _ymd(expiry_date)},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token.token}",
        )

    def test_edit_two_months_out_is_rejected(self):
        start = self.post.created_at.date()
        two_months = add_one_month(add_one_month(start))
        resp = self._patch(two_months)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("1 month", resp.json()["message"])
        self.post.refresh_from_db()
        self.assertEqual(self.post.post_expiry_date, self.today + timedelta(days=7))  # unchanged

    def test_edit_within_range_is_accepted(self):
        new_expiry = self.today + timedelta(days=14)
        resp = self._patch(new_expiry)
        self.assertEqual(resp.status_code, 200)
        self.post.refresh_from_db()
        self.assertEqual(self.post.post_expiry_date, new_expiry)


class ClampPostExpiriesCommandTests(TestCase):
    """clamp_post_expiries pulls far-future rows back to created_at + 1 month, leaves
    in-range rows untouched, and is idempotent on re-run."""

    def setUp(self):
        self.user = User.objects.create(
            username="capclamp", email="capclamp@example.com", full_name="Cap Clamp",
        )
        self.today = timezone.now().date()

    def _seed_post(self, expiry_date, created_at):
        """Create a post then force its created_at (auto_now_add can't be set on create)
        with .update(), which bypasses auto_now_add, so we can seed a known start."""
        post = RecruitmentPost.objects.create(
            post_type="PLAYER_AVAILABLE",
            post_expiry_date=expiry_date,
            created_by=self.user,
            player=self.user,
        )
        RecruitmentPost.objects.filter(id=post.id).update(created_at=created_at)
        post.refresh_from_db()
        return post

    def test_clamps_far_future_and_leaves_in_range(self):
        created = timezone.now()
        start = created.date()
        # A far-future post (a year out) must be clamped to start + 1 month.
        far = self._seed_post(start + timedelta(days=365), created)
        # An in-range post (2 weeks out) must be left exactly as-is.
        ok = self._seed_post(start + timedelta(days=14), created)

        out = StringIO()
        call_command("clamp_post_expiries", stdout=out)
        self.assertIn("Clamped 1", out.getvalue())

        far.refresh_from_db()
        ok.refresh_from_db()
        self.assertEqual(far.post_expiry_date, add_one_month(start))      # pulled to the cap
        self.assertEqual(ok.post_expiry_date, start + timedelta(days=14))  # untouched

        # Idempotent: a second run finds nothing left over the cap.
        out2 = StringIO()
        call_command("clamp_post_expiries", stdout=out2)
        self.assertIn("Clamped 0", out2.getvalue())
        far.refresh_from_db()
        self.assertEqual(far.post_expiry_date, add_one_month(start))  # still at the cap
