"""
afc_auth/test_news_pinned_notices.py
================================================================================
Homepage notices (backlog item 22, owner 2026-08-08: "Homepage section for public notices and
important announcements").

A notice is a NEWS POST that has been pinned to the homepage until a date - there is no second
notices model and no second admin screen. See the header of News.pinned_until (afc_auth/models.py)
for why, and afc_auth.views.HOME_PINNED_NOTICES_LIMIT for the "how many, and what when several are
pinned" rules these tests lock down.

WHAT THESE COVER, and why each one exists:
  • A pinned post appears in the homepage block, and an unpinned one does not. The obvious pair.
  • AN EXPIRY IN THE PAST IS NOT PINNED. This is the whole promise of the feature - a notice takes
    itself down instead of needing somebody to remember - and it is the one behaviour that fails
    SILENTLY if it regresses (the notice just sits there forever, which is how it looked before).
  • UNPINNING DELETES NOTHING. The post must stay published and readable at /news afterwards. An
    editor clearing a pin should never be able to lose an article by doing it.
  • The CAP and its tie-break: three at a time, newest first, and the extras are not lost.
  • A SCHEDULED (not-yet-published) post that was pinned ahead of time must not leak onto the
    homepage before its release moment.
  • An edit that does not mention pinning must not silently unpin - otherwise any older caller,
    or the FE's own "just change the title" path, would knock notices off the homepage.

HOW IT DRIVES THE CODE: the REAL HTTP endpoints via Django's test Client with a Bearer
SessionToken, exactly as the admin News form calls them. It reuses the news-admin fixture from
afc_auth/test_news_overhaul.py rather than declaring a second, slightly different one.

Run: .venv\\Scripts\\python.exe manage.py test afc_auth.test_news_pinned_notices
"""
from datetime import timedelta

from django.utils import timezone

from afc_auth.models import News
from afc_auth.views import HOME_PINNED_NOTICES_LIMIT
from afc_auth.test_news_overhaul import _NewsBase


class PinnedNoticesTests(_NewsBase):
    CREATE_URL = "/auth/create-news/"
    EDIT_URL = "/auth/edit-news/"
    LIST_URL = "/auth/get-all-news/"
    PINNED_URL = "/auth/get-pinned-news/"

    # ── helpers ─────────────────────────────────────────────────────────────────────────────
    def _create(self, title, *, pinned_until=None, scheduled_publish_at=None):
        """POST create-news the way the admin News form does: multipart, ISO UTC datetimes."""
        payload = {
            "news_title": title,
            "content": '{"type":"doc","content":[]}',
            "category": "general",
        }
        if pinned_until is not None:
            payload["pinned_until"] = pinned_until.isoformat()
        if scheduled_publish_at is not None:
            payload["scheduled_publish_at"] = scheduled_publish_at.isoformat()
        resp = self.client.post(self.CREATE_URL, data=payload, **self._auth(self.admin_tok))
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()["news_id"]

    def _notice_titles(self):
        resp = self.client.get(self.PINNED_URL)
        self.assertEqual(resp.status_code, 200)
        return [n["news_title"] for n in resp.json()["notices"]]

    @staticmethod
    def _in(days):
        return timezone.now() + timedelta(days=days)

    @staticmethod
    def _ago(days):
        return timezone.now() - timedelta(days=days)

    # ── 1. the obvious pair ─────────────────────────────────────────────────────────────────
    def test_a_pinned_post_shows_on_the_homepage(self):
        # Arrange / Act
        self._create("Server maintenance Sunday", pinned_until=self._in(3))

        # Assert
        self.assertEqual(self._notice_titles(), ["Server maintenance Sunday"])

    def test_an_ordinary_post_does_not_show_on_the_homepage(self):
        # Arrange / Act
        self._create("Just a normal article")

        # Assert
        self.assertEqual(self._notice_titles(), [])

    # ── 2. self-expiry, the point of the feature ────────────────────────────────────────────
    def test_an_expiry_in_the_past_means_not_pinned(self):
        """Sent a date that has already gone by, the post is simply not pinned. Not an error to
        argue with: it is the same answer the reader would get a moment later anyway."""
        # Arrange / Act
        news_id = self._create("Yesterday's notice", pinned_until=self._ago(1))

        # Assert - and the column is cleared rather than storing a stale date that reads as pinned.
        self.assertIsNone(News.objects.get(news_id=news_id).pinned_until)
        self.assertEqual(self._notice_titles(), [])

    def test_a_pin_that_lapses_takes_the_notice_down_by_itself(self):
        """Nobody edits anything here. The clock does the work, which is the whole promise."""
        # Arrange
        news_id = self._create("Registration closes soon", pinned_until=self._in(2))
        self.assertEqual(self._notice_titles(), ["Registration closes soon"])

        # Act - the pin lapses (simulated by moving the stored expiry into the past).
        News.objects.filter(news_id=news_id).update(pinned_until=self._ago(1))

        # Assert
        self.assertEqual(self._notice_titles(), [])

    def test_is_pinned_now_is_false_once_the_date_has_passed(self):
        news = News.objects.create(
            news_title="Lapsed", content="{}", category="general",
            author=self.admin, pinned_until=self._ago(1))
        self.assertFalse(news.is_pinned_now())

    # ── 3. unpinning is not deleting ────────────────────────────────────────────────────────
    def test_unpinning_leaves_the_article_published_and_readable(self):
        """The failure this guards against is an editor clearing a pin and losing the article."""
        # Arrange
        news_id = self._create("Prize payout update", pinned_until=self._in(5))
        news = News.objects.get(news_id=news_id)

        # Act - the admin form sends an empty pinned_until when the switch is turned off.
        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": news_id, "pinned_until": ""},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # Assert - off the homepage...
        self.assertEqual(self._notice_titles(), [])
        # ...but the row is intact, still published, and still in the public news list.
        news.refresh_from_db()
        self.assertIsNone(news.pinned_until)
        self.assertTrue(news.is_published)
        self.assertEqual(news.news_title, "Prize payout update")
        listed = self.client.get(self.LIST_URL).json()["news"]
        self.assertIn(news_id, [item["news_id"] for item in listed])

    def test_an_edit_that_does_not_mention_pinning_leaves_the_pin_alone(self):
        """Absent field means "leave it", present-but-empty means "unpin". Without that split, any
        caller that predates this feature would knock every notice off the homepage."""
        # Arrange
        news_id = self._create("Keep me pinned", pinned_until=self._in(4))

        # Act - a title-only edit, no pinned_until key at all.
        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": news_id, "news_title": "Keep me pinned (v2)"},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # Assert
        self.assertEqual(self._notice_titles(), ["Keep me pinned (v2)"])

    def test_pinning_an_existing_article_later_puts_it_on_the_homepage(self):
        # Arrange
        news_id = self._create("Promoted after the fact")
        self.assertEqual(self._notice_titles(), [])

        # Act
        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": news_id, "pinned_until": self._in(6).isoformat()},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # Assert
        self.assertTrue(resp.json()["is_pinned"])
        self.assertEqual(self._notice_titles(), ["Promoted after the fact"])

    # ── 4. the cap, and what happens on a conflict ──────────────────────────────────────────
    def test_at_most_three_notices_show_and_the_newest_win(self):
        # Arrange - five pinned posts, created oldest first.
        for i in range(1, 6):
            self._create(f"Notice {i}", pinned_until=self._in(10))

        # Act
        titles = self._notice_titles()

        # Assert - newest first, capped at the named limit.
        self.assertEqual(HOME_PINNED_NOTICES_LIMIT, 3)
        self.assertEqual(titles, ["Notice 5", "Notice 4", "Notice 3"])

    def test_the_ones_beyond_the_cap_are_still_pinned_and_still_readable(self):
        """They are not silently unpinned to make room: they stay on file and come back as the
        newer ones expire, so an editor never loses a notice by pinning a fourth."""
        # Arrange
        ids = [self._create(f"Notice {i}", pinned_until=self._in(10)) for i in range(1, 5)]
        oldest = ids[0]
        self.assertNotIn("Notice 1", self._notice_titles())

        # Assert - still pinned in the data, and still in the public news list.
        self.assertIsNotNone(News.objects.get(news_id=oldest).pinned_until)
        self.assertTrue(News.objects.get(news_id=oldest).is_pinned_now())
        listed = self.client.get(self.LIST_URL).json()["news"]
        self.assertIn(oldest, [item["news_id"] for item in listed])

        # Act - the three newer notices lapse.
        News.objects.filter(news_id__in=ids[1:]).update(pinned_until=self._ago(1))

        # Assert - the fourth surfaces on its own.
        self.assertEqual(self._notice_titles(), ["Notice 1"])

    # ── 5. a scheduled post must not leak early ─────────────────────────────────────────────
    def test_a_scheduled_post_pinned_in_advance_stays_off_the_homepage(self):
        # Arrange / Act
        news_id = self._create(
            "Embargoed announcement",
            pinned_until=self._in(9),
            scheduled_publish_at=self._in(2),
        )
        self.assertFalse(News.objects.get(news_id=news_id).is_published)

        # Assert
        self.assertEqual(self._notice_titles(), [])

        # Act - the beat task releases it (that is all publish_scheduled_news does).
        News.objects.filter(news_id=news_id).update(is_published=True)

        # Assert - now it is a notice.
        self.assertEqual(self._notice_titles(), ["Embargoed announcement"])

    # ── 6. the pin state the admin surfaces read ────────────────────────────────────────────
    def test_the_news_list_reports_the_pin_state_for_the_admin_badge(self):
        # Arrange
        pinned_id = self._create("Pinned one", pinned_until=self._in(3))
        plain_id = self._create("Plain one")

        # Act
        by_id = {i["news_id"]: i for i in self.client.get(self.LIST_URL).json()["news"]}

        # Assert
        self.assertTrue(by_id[pinned_id]["is_pinned"])
        self.assertIsNotNone(by_id[pinned_id]["pinned_until"])
        self.assertFalse(by_id[plain_id]["is_pinned"])
        self.assertIsNone(by_id[plain_id]["pinned_until"])

    def test_an_unparseable_pin_date_is_rejected(self):
        resp = self.client.post(
            self.CREATE_URL,
            data={
                "news_title": "Bad date",
                "content": '{"type":"doc","content":[]}',
                "category": "general",
                "pinned_until": "not-a-date",
            },
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pinned_until", resp.json()["message"])
