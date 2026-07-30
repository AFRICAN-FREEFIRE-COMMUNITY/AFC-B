"""
afc_auth/test_news_overhaul.py
================================================================================
Tests for the NEWS OVERHAUL backend slice (owner design decision 2026-07-14).

WHAT THIS COVERS (the backend contract the frontend depends on):
  1. upload_news_image  - POST /auth/upload-news-image/  (Tiptap editor image / gallery "Upload" tab).
       Accepts a small real JPEG -> 200 {"status":"ok","url": <absolute media url>}; rejects >10MB.
  2. upload_news_video  - POST /auth/upload-news-video/  (editor video node "Upload" tab).
       Rejects a non-video content_type and a >50MB clip; accepts a small video/mp4 -> 200 + url.
  3. related_events M2M  - create_news sets it from repeated `related_events` form ids and mirrors the
       FIRST id into the legacy related_event FK; edit_news can CHANGE and CLEAR it; get_news_detail
       returns the related_events list with the exact keys the public "Related events" block reads
       (event_id, event_name, slug, tournament_tier, end_date).

HOW IT DRIVES THE CODE:
  Like afc_auth/test_act_as.py + test_audit_log.py, these hit the REAL HTTP endpoints with a Bearer
  SessionToken (exactly as the admin News form does) via Django's test Client. No network is touched:
  translate-on-read is a no-op at the default "en" locale, and MEDIA writes go to a throwaway temp
  MEDIA_ROOT (see _MEDIA / override_settings) so nothing lands in the repo's media/ folder.

Run: ./.venv/Scripts/python.exe manage.py test afc_auth.test_news_overhaul --keepdb -v1
"""
import datetime
import shutil
import tempfile
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings

from PIL import Image

from afc_auth.models import User, Roles, UserRoles, SessionToken, News
from afc_tournament_and_scrims.models import Event


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────
def _jpeg_bytes(color=(200, 30, 30), size=(64, 64)):
    """A tiny, genuinely-decodable JPEG so normalize_image_upload (PIL open) takes the real path."""
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _mk_event(name, tier="tier_1"):
    """A minimal but complete Event (the model has many required fields). Mirrors the fixture shape in
    test_stage_over_lock._mk_event. slug + tournament_tier are what _serialize_related_news_events emits."""
    today = datetime.date.today()
    return Event.objects.create(
        competition_type="tournament", participant_type="squad", event_type="internal",
        max_teams_or_players=16, event_name=name, event_mode="virtual",
        start_date=today - datetime.timedelta(days=1),
        end_date=today + datetime.timedelta(days=5),
        registration_open_date=today - datetime.timedelta(days=3),
        registration_end_date=today - datetime.timedelta(days=1),
        prizepool="0", event_rules="r", event_status="ongoing",
        registration_link="https://example.com/r", number_of_stages=1,
        tournament_tier=tier, is_draft=False,
    )


class _NewsBase(TestCase):
    """Builds a news_admin caller (role admin + granular news_admin) with a live Bearer token, plus a
    plain player used to prove the permission gate. This is the SAME gate create_news / the upload
    endpoints enforce (_is_news_admin)."""

    def setUp(self):
        self.client = Client()

        # Granular role used by the news permission gate.
        self.r_news, _ = Roles.objects.get_or_create(role_name="news_admin")

        # The privileged caller: base role "admin" + the news_admin granular role.
        self.admin = User.objects.create(
            username="newsadmin", email="newsadmin@x.com", full_name="News Admin",
            role="admin", password="x",
        )
        UserRoles.objects.create(user=self.admin, role=self.r_news)
        self.admin_tok = SessionToken.objects.create(
            user=self.admin, token="tok_newsadmin"
        ).token

        # A plain player - NOT a news admin. The header is valid but the gate must 403.
        self.player = User.objects.create(
            username="plainplayer", email="plainplayer@x.com", full_name="Plain Player",
            role="player", password="x",
        )
        self.player_tok = SessionToken.objects.create(
            user=self.player, token="tok_plainplayer"
        ).token

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────────────────────────
# 1 + 2. Media upload endpoints (write to a throwaway MEDIA_ROOT so nothing litters the repo)
# ──────────────────────────────────────────────────────────────────────────────
_MEDIA = tempfile.mkdtemp(prefix="afc_news_media_")


@override_settings(MEDIA_ROOT=_MEDIA)
class NewsMediaUploadTests(_NewsBase):
    IMG_URL = "/auth/upload-news-image/"
    VID_URL = "/auth/upload-news-video/"

    @classmethod
    def tearDownClass(cls):
        # Remove every file the valid-upload tests saved under the temp MEDIA_ROOT.
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    # ── image ────────────────────────────────────────────────────────────────
    def test_upload_image_ok(self):
        """A small real JPEG -> 200 with an absolute news_images/ url the editor embeds as a node src."""
        f = SimpleUploadedFile("photo.jpg", _jpeg_bytes(), content_type="image/jpeg")
        resp = self.client.post(self.IMG_URL, data={"image": f}, **self._auth(self.admin_tok))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["url"].startswith("http"))     # absolute (build_absolute_uri)
        self.assertIn("news_images/", body["url"])

    def test_upload_image_too_large_rejected(self):
        """>10MB (contract cap) -> 400 before any normalization. Size check is first, so the bytes
        need not be a valid image to exercise the guard."""
        big = SimpleUploadedFile("big.jpg", b"x" * (10 * 1024 * 1024 + 1), content_type="image/jpeg")
        resp = self.client.post(self.IMG_URL, data={"image": big}, **self._auth(self.admin_tok))
        self.assertEqual(resp.status_code, 400)

    def test_upload_image_missing_file(self):
        resp = self.client.post(self.IMG_URL, data={}, **self._auth(self.admin_tok))
        self.assertEqual(resp.status_code, 400)

    def test_upload_image_forbidden_for_non_news_admin(self):
        """Same gate as create_news: a plain player with a valid token is 403 (not a news manager)."""
        f = SimpleUploadedFile("photo.jpg", _jpeg_bytes(), content_type="image/jpeg")
        resp = self.client.post(self.IMG_URL, data={"image": f}, **self._auth(self.player_tok))
        self.assertEqual(resp.status_code, 403)

    # ── video ────────────────────────────────────────────────────────────────
    def test_upload_video_ok(self):
        """A small video/mp4 -> 200 with an absolute news_videos/ url (no normalization for video)."""
        f = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4")
        resp = self.client.post(self.VID_URL, data={"video": f}, **self._auth(self.admin_tok))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["url"].startswith("http"))
        self.assertIn("news_videos/", body["url"])

    def test_upload_video_non_video_content_type_rejected(self):
        """A mislabeled non-video file -> 400 (guards against an <img>/doc rendering blank as a video)."""
        f = SimpleUploadedFile("note.txt", b"not a video at all", content_type="text/plain")
        resp = self.client.post(self.VID_URL, data={"video": f}, **self._auth(self.admin_tok))
        self.assertEqual(resp.status_code, 400)

    def test_upload_video_too_large_rejected(self):
        """>50MB (contract cap) with a real video/ content_type -> 400. content_type is checked before
        size, so the payload must be BOTH a video type AND oversized to reach the size guard."""
        big = SimpleUploadedFile("big.mp4", b"\x00" * (50 * 1024 * 1024 + 1), content_type="video/mp4")
        resp = self.client.post(self.VID_URL, data={"video": big}, **self._auth(self.admin_tok))
        self.assertEqual(resp.status_code, 400)

    def test_upload_video_forbidden_for_non_news_admin(self):
        f = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4")
        resp = self.client.post(self.VID_URL, data={"video": f}, **self._auth(self.player_tok))
        self.assertEqual(resp.status_code, 403)


# ──────────────────────────────────────────────────────────────────────────────
# 3. related_events M2M: create sets it, edit changes/clears it, detail returns it
# ──────────────────────────────────────────────────────────────────────────────
class NewsRelatedEventsTests(_NewsBase):
    CREATE_URL = "/auth/create-news/"
    EDIT_URL = "/auth/edit-news/"
    DETAIL_URL = "/auth/get-news-detail/"

    def setUp(self):
        super().setUp()
        self.ev1 = _mk_event("Alpha Cup", tier="tier_1")
        self.ev2 = _mk_event("Bravo Cup", tier="tier_2")
        self.ev3 = _mk_event("Charlie Cup", tier="tier_3")

    def _create(self, related_ids):
        """POST create-news as the admin form does: multipart, related_events repeated per selected id."""
        return self.client.post(
            self.CREATE_URL,
            data={
                "news_title": "Weekend Recap",
                "content": '{"type":"doc","content":[]}',   # a stringified (empty) Tiptap doc
                "category": "tournament",
                "related_events": [str(i) for i in related_ids],
            },
            **self._auth(self.admin_tok),
        )

    # ── CREATE sets the M2M and mirrors the first id into the legacy FK ─────────
    def test_create_sets_related_events_and_mirrors_legacy_fk(self):
        resp = self._create([self.ev1.event_id, self.ev2.event_id])
        self.assertEqual(resp.status_code, 201)
        news = News.objects.get(news_id=resp.json()["news_id"])
        self.assertEqual(
            set(news.related_events.values_list("event_id", flat=True)),
            {self.ev1.event_id, self.ev2.event_id},
        )
        # The FIRST matched event is mirrored into the legacy single related_event FK (back-compat).
        self.assertEqual(news.related_event_id, self.ev1.event_id)

    def test_create_ignores_unknown_event_ids(self):
        """Non-existent / non-numeric ids are dropped; only real events are linked."""
        resp = self._create([self.ev1.event_id, 999999, "abc"])
        self.assertEqual(resp.status_code, 201)
        news = News.objects.get(news_id=resp.json()["news_id"])
        self.assertEqual(
            list(news.related_events.values_list("event_id", flat=True)),
            [self.ev1.event_id],
        )

    # ── EDIT can change the whole set ──────────────────────────────────────────
    def test_edit_changes_related_events(self):
        news = self._create([self.ev1.event_id]).json()
        news_id = news["news_id"]
        resp = self.client.post(
            self.EDIT_URL,
            data={
                "news_id": str(news_id),
                "related_events": [str(self.ev2.event_id), str(self.ev3.event_id)],
            },
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        obj = News.objects.get(news_id=news_id)
        self.assertEqual(
            set(obj.related_events.values_list("event_id", flat=True)),
            {self.ev2.event_id, self.ev3.event_id},
        )
        # legacy FK follows the new first id.
        self.assertEqual(obj.related_event_id, self.ev2.event_id)

    # ── EDIT can CLEAR the set (empty value present -> full deselect) ───────────
    def test_edit_clears_related_events(self):
        news_id = self._create([self.ev1.event_id, self.ev2.event_id]).json()["news_id"]
        # The admin form sends a single empty `related_events` value when the selection is cleared.
        resp = self.client.post(
            self.EDIT_URL,
            data={"news_id": str(news_id), "related_events": ""},
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        obj = News.objects.get(news_id=news_id)
        self.assertEqual(obj.related_events.count(), 0)
        self.assertIsNone(obj.related_event_id)   # legacy FK cleared too

    # ── get_news_detail returns the related_events list with the exact keys ─────
    def test_detail_returns_related_events_with_expected_keys(self):
        news_id = self._create([self.ev1.event_id, self.ev2.event_id]).json()["news_id"]
        news = News.objects.get(news_id=news_id)
        resp = self.client.post(
            self.DETAIL_URL,
            data={"slug": news.slug},
            content_type="application/json",
            **self._auth(self.admin_tok),
        )
        self.assertEqual(resp.status_code, 200)
        related = resp.json()["news"]["related_events"]
        self.assertEqual(len(related), 2)
        # Every entry carries exactly the keys the public "Related events" block consumes.
        for entry in related:
            self.assertEqual(
                set(entry.keys()),
                {"event_id", "event_name", "slug", "tournament_tier", "end_date"},
            )
        by_id = {e["event_id"]: e for e in related}
        self.assertEqual(by_id[self.ev1.event_id]["event_name"], "Alpha Cup")
        self.assertEqual(by_id[self.ev1.event_id]["slug"], self.ev1.slug)
        self.assertEqual(by_id[self.ev1.event_id]["tournament_tier"], "tier_1")
        # end_date serialized as an ISO "YYYY-MM-DD" string.
        self.assertEqual(by_id[self.ev1.event_id]["end_date"], self.ev1.end_date.isoformat())
        # Legacy single-event name key stays for back-compat.
        self.assertEqual(resp.json()["news"]["related_event"], "Alpha Cup")
