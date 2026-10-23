"""
afc_auth/test_news_multi_category.py
================================================================================
A news post in SEVERAL categories at once (inbox #160, owner 2026-10-05: "an article or post can
be under several categories simultaneously").

WHAT THIS COVERS
  1. create_news takes `categories` as a repeated multipart field (the admin News form) and as a
     JSON list; stores them in the admin's order with duplicates dropped; mirrors the FIRST into
     the legacy `category`.
  2. Validation: one unknown key refuses the whole post (nothing created); an empty pick is refused
     with its own code.
  3. edit_news replaces the set when `categories` is sent and leaves it alone when it is not.
  4. Readers: get_all_news, get_news_detail and get_pinned_news answer `categories`; a post
     written before the field existed (empty list) answers [category], so no backfill is needed.
  The single-category behaviour (legacy `category` field, the choice list itself) stays covered by
  test_news_categories.py.

Same fixture and HTTP shape as test_news_categories.py (_NewsBase from test_news_overhaul.py).

Run: ./.venv/Scripts/python.exe manage.py test afc_auth.test_news_multi_category --keepdb -v1
"""
from datetime import timedelta

from django.utils import timezone

from afc_auth.models import News
from afc_auth.test_news_overhaul import _NewsBase

DOC = '{"type":"doc","content":[]}'


class NewsMultiCategoryTests(_NewsBase):
    CREATE_URL = "/auth/create-news/"
    EDIT_URL = "/auth/edit-news/"
    DETAIL_URL = "/auth/get-news-detail/"
    LIST_URL = "/auth/get-all-news/"
    NOTICES_URL = "/auth/get-pinned-news/"

    def _create(self, categories, title="Multi Post", **extra):
        """POST create-news as multipart with `categories` repeated, the way the admin form sends it."""
        return self.client.post(
            self.CREATE_URL,
            data={"news_title": title, "content": DOC, "categories": categories, **extra},
            **self._auth(self.admin_tok),
        )

    # ── 1. create ───────────────────────────────────────────────────────────────
    def test_create_in_two_categories_keeps_both_and_the_first_is_the_category(self):
        resp = self._create(["team_updates", "bans"])
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["categories"], ["team_updates", "bans"])
        news = News.objects.get(news_id=resp.json()["news_id"])
        self.assertEqual(news.categories, ["team_updates", "bans"])
        self.assertEqual(news.category, "team_updates")

    def test_duplicates_are_dropped_and_the_admins_order_kept(self):
        resp = self._create(["education", "general", "education"])
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["categories"], ["education", "general"])

    def test_a_json_list_works_too(self):
        resp = self.client.post(
            self.CREATE_URL,
            data={"news_title": "JSON post", "content": DOC, "categories": ["tournament", "education"]},
            content_type="application/json",
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["categories"], ["tournament", "education"])

    # ── 2. validation ──────────────────────────────────────────────────────────
    def test_one_unknown_key_refuses_the_whole_post(self):
        before = News.objects.count()
        resp = self._create(["general", "rumours"])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "invalid_category")
        self.assertEqual(News.objects.count(), before)

    def test_an_empty_pick_is_refused_with_its_own_code(self):
        resp = self._create([""])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "categories_required")

    def test_no_category_at_all_is_still_a_required_field(self):
        resp = self.client.post(
            self.CREATE_URL, data={"news_title": "No category", "content": DOC},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "title_content_category_required")

    # ── 3. edit ─────────────────────────────────────────────────────────────────
    def test_edit_replaces_the_set(self):
        news_id = self._create(["general"]).json()["news_id"]
        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": str(news_id), "categories": ["banned_teams", "team_updates"]},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["categories"], ["banned_teams", "team_updates"])
        news = News.objects.get(news_id=news_id)
        self.assertEqual(news.category, "banned_teams")

    def test_an_edit_that_does_not_send_categories_keeps_them(self):
        news_id = self._create(["tournament", "education"]).json()["news_id"]
        resp = self.client.post(
            self.EDIT_URL, data={"news_id": str(news_id), "news_title": "Retitled"},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(News.objects.get(news_id=news_id).categories, ["tournament", "education"])

    def test_a_refused_edit_changes_nothing(self):
        news_id = self._create(["tournament", "education"]).json()["news_id"]
        resp = self.client.post(
            self.EDIT_URL, data={"news_id": str(news_id), "categories": ["tournament", "nope"]},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(News.objects.get(news_id=news_id).categories, ["tournament", "education"])

    # ── 4. readers ──────────────────────────────────────────────────────────────
    def test_list_and_detail_answer_every_category(self):
        news_id = self._create(["general", "education"]).json()["news_id"]
        by_id = {n["news_id"]: n for n in self.client.get(self.LIST_URL).json()["news"]}
        self.assertEqual(by_id[news_id]["categories"], ["general", "education"])
        self.assertEqual(by_id[news_id]["category"], "general")
        slug = News.objects.get(news_id=news_id).slug
        detail = self.client.post(self.DETAIL_URL, data={"slug": slug}, content_type="application/json")
        self.assertEqual(detail.json()["news"]["categories"], ["general", "education"])

    def test_a_post_from_before_the_field_answers_its_one_category(self):
        old = News.objects.create(news_title="Old post", content=DOC, category="bans", is_published=True,
                                 author=self.admin)
        self.assertEqual(old.categories, [])
        by_id = {n["news_id"]: n for n in self.client.get(self.LIST_URL).json()["news"]}
        self.assertEqual(by_id[old.news_id]["categories"], ["bans"])

    def test_homepage_notices_answer_every_category(self):
        until = (timezone.now() + timedelta(days=2)).isoformat()
        news_id = self._create(["tournament", "team_updates"], pinned_until=until).json()["news_id"]
        by_id = {n["news_id"]: n for n in self.client.get(self.NOTICES_URL).json()["notices"]}
        self.assertEqual(by_id[news_id]["categories"], ["tournament", "team_updates"])
