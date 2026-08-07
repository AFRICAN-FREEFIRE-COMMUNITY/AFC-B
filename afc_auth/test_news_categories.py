"""
afc_auth/test_news_categories.py
================================================================================
Tests for the NEWS CATEGORY set (owner request 2026-08-06: "for the news section add EDUCATION
UPDATES as a category that can be picked by admins and that shows on the user facing side").

WHAT THIS COVERS (the backend contract the News surfaces depend on):
  1. News.CATEGORY_CHOICES is the single source of truth - it carries the new "education" key while
     keeping every pre-existing key, in the order the pickers and filters render.
  2. create_news  - POST /auth/create-news/  accepts category="education" and stores it verbatim.
  3. get_news_detail / get_all_news return that raw key, which is what the public pages map to a
     localized label (frontend messages/<locale>/news.json categories.*).
  4. Existing categories are unaffected: "general" / "tournament" / "bans" still create and read
     back, and edit_news can move a post between an old category and the new one both ways.
  5. Validation still bites: an unknown category is rejected 400 on BOTH create and edit, so the
     switch from a hardcoded list to News.CATEGORY_CHOICES did not open the field up.

HOW IT DRIVES THE CODE:
  Same shape as afc_auth/test_news_overhaul.py - the REAL HTTP endpoints via Django's test Client
  with a Bearer SessionToken, exactly as the admin News form calls them. It reuses that module's
  _NewsBase fixture (news_admin caller + plain player) rather than re-declaring a second, slightly
  different news-admin setUp. No network is touched: translate-on-read is a no-op at "en".

Run: ./.venv/Scripts/python.exe manage.py test afc_auth.test_news_categories --keepdb -v1
"""
from afc_auth.models import News

# Shared news-admin fixture (admin + granular news_admin role + live Bearer token). Imported so the
# category tests and the news-overhaul tests exercise the SAME permission setup.
from afc_auth.test_news_overhaul import _NewsBase


class NewsCategoryTests(_NewsBase):
    CREATE_URL = "/auth/create-news/"
    EDIT_URL = "/auth/edit-news/"
    DETAIL_URL = "/auth/get-news-detail/"
    LIST_URL = "/auth/get-all-news/"

    def _create(self, category, title="Category Post"):
        """POST create-news the way the admin News form does: multipart, stringified Tiptap doc."""
        return self.client.post(
            self.CREATE_URL,
            data={
                "news_title": title,
                "content": '{"type":"doc","content":[]}',
                "category": category,
            },
            **self._auth(self.admin_tok),
        )

    # ── 1. the choice set itself ───────────────────────────────────────────────
    def test_education_is_a_model_choice_and_old_ones_survive(self):
        """The new key exists, no pre-existing key was dropped, and the order is the one every
        picker/filter renders (general, tournament, education, bans - "bans" stays last)."""
        keys = [key for key, _label in News.CATEGORY_CHOICES]
        self.assertEqual(keys, ["general", "tournament", "education", "bans"])
        labels = dict(News.CATEGORY_CHOICES)
        self.assertEqual(labels["education"], "Education Updates")

    # ── 2 + 3. create with the new category, then read it back ─────────────────
    def test_create_with_education_category(self):
        resp = self._create("education", title="How to read a bracket")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["category"], "education")

        # Stored verbatim on the row (no coercion, no fallback to "general").
        news = News.objects.get(news_id=resp.json()["news_id"])
        self.assertEqual(news.category, "education")

    def test_detail_returns_education_category(self):
        """get_news_detail hands the RAW key to the frontend, which localizes it."""
        news_id = self._create("education").json()["news_id"]
        news = News.objects.get(news_id=news_id)
        resp = self.client.post(
            self.DETAIL_URL, data={"slug": news.slug}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["news"]["category"], "education")

    def test_public_list_includes_the_education_post(self):
        """The anonymous /news list (and the home teaser) must see it, not just the admin."""
        news_id = self._create("education", title="Ranking rules explained").json()["news_id"]
        resp = self.client.get(self.LIST_URL)
        self.assertEqual(resp.status_code, 200)
        by_id = {item["news_id"]: item for item in resp.json()["news"]}
        self.assertIn(news_id, by_id)
        self.assertEqual(by_id[news_id]["category"], "education")

    # ── 4. existing categories are untouched ───────────────────────────────────
    def test_existing_categories_still_create_and_read_back(self):
        for category in ("general", "tournament", "bans"):
            with self.subTest(category=category):
                resp = self._create(category, title=f"Post about {category}")
                self.assertEqual(resp.status_code, 201)
                self.assertEqual(
                    News.objects.get(news_id=resp.json()["news_id"]).category, category
                )

    def test_edit_moves_a_post_between_old_and_new_category(self):
        """Both directions: an existing tournament post can become education, and back again."""
        news_id = self._create("tournament").json()["news_id"]

        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": str(news_id), "category": "education"},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(News.objects.get(news_id=news_id).category, "education")

        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": str(news_id), "category": "tournament"},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(News.objects.get(news_id=news_id).category, "tournament")

    def test_edit_without_a_category_leaves_it_alone(self):
        """An edit that omits `category` (e.g. a title-only fix) must not wipe or change it."""
        news_id = self._create("education").json()["news_id"]
        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": str(news_id), "news_title": "Retitled"},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        news = News.objects.get(news_id=news_id)
        self.assertEqual(news.category, "education")
        self.assertEqual(news.news_title, "Retitled")

    # ── 5. validation still rejects anything not in CATEGORY_CHOICES ───────────
    def test_create_rejects_unknown_category(self):
        resp = self._create("educational")   # near-miss of the real key
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["message"], "Invalid category.")

    def test_edit_rejects_unknown_category(self):
        news_id = self._create("education").json()["news_id"]
        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": str(news_id), "category": "not_a_category"},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 400)
        # The rejected edit left the stored category untouched.
        self.assertEqual(News.objects.get(news_id=news_id).category, "education")
