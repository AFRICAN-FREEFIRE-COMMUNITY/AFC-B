"""
Tests for the last image doors that relied on the FAIL-SAFE normaliser alone (owner, 11 Nov 2026, inbox #531:
"Upload checks on 36 image sites - do it").

normalize_image_upload never refuses: a file Pillow cannot open came back UNCHANGED, under the client's own name, and
was stored. So an HTML page named "me.jpg" became somebody's profile picture on our media host. Each door below now goes
through require_image_upload (afc_auth/image_utils.py): Pillow decodes the bytes, the format list is short, the size is
capped, the copy is re-encoded and named opaquely, and a non-image is refused with the code not_an_image.

  edit-profile          profile_pic, any signed-in player; no file still means no change
  upload-esport-image   esports_pic, any signed-in player (refused BEFORE the face check sees it)
  upload-news-image     the news editor's article images (news admins)

The two news cover doors (create-news, edit-news) carry the same three lines and the same code.

Run: python manage.py test afc_auth.tests_image_doors
"""
import io
import json
import re

from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from afc_auth.models import Roles, SessionToken, User, UserProfile, UserRoles

import tempfile

MEDIA = tempfile.mkdtemp(prefix="afc-image-doors-")
HTML = b"<html><body><script>alert(document.cookie)</script></body></html>"


def png_bytes():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (60, 60), (30, 120, 60)).save(out, "PNG")
    return out.getvalue()


@override_settings(MEDIA_ROOT=MEDIA)
class ImageDoorTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.player = self._user("door_player", "doorplayer@gmail.com", uid="1234567890")

    def _user(self, username, email, role="player", granular=(), uid=None):
        user = User.objects.create(
            username=username, email=email, full_name=username.title(), role=role,
            password=make_password("CorrectHorse!9"), is_active=True, uid=uid, language="en",
        )
        for name in granular:
            role_row, _ = Roles.objects.get_or_create(role_name=name, defaults={"description": name})
            UserRoles.objects.create(user=user, role=role_row)
        return user

    def _auth(self, user):
        token = SessionToken.objects.create(
            user=user, token=f"tok-{user.username}-{timezone.now().timestamp()}"[:64],
            expires_at=timezone.now() + SessionToken.SESSION_LIFETIME,
        ).token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _edit_profile(self, picture=None):
        data = {"full_name": self.player.full_name, "in_game_name": self.player.username, "email": self.player.email}
        if picture is not None:
            data["profile_pic"] = picture
        return self.client.post("/auth/edit-profile/", data=data, **self._auth(self.player))

    # ── edit profile ─────────────────────────────────────────────────────────────────────────────
    def test_profile_picture_that_is_a_web_page_is_refused_and_not_stored(self):
        resp = self._edit_profile(SimpleUploadedFile("me.jpg", HTML, content_type="image/jpeg"))
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "not_an_image"))
        profile = UserProfile.objects.filter(user=self.player).first()
        self.assertFalse(profile and profile.profile_pic)

    def test_a_real_picture_is_stored_under_an_opaque_name(self):
        resp = self._edit_profile(SimpleUploadedFile("My Face.php.png", png_bytes(), content_type="image/png"))
        self.assertEqual(resp.status_code, 200, resp.content)
        profile = UserProfile.objects.filter(user=self.player).order_by("-pk").first()
        name = profile.profile_pic.name.rsplit("/", 1)[-1]
        self.assertRegex(name, r"^[0-9a-f]{32}(_[A-Za-z0-9]+)?\.jpg$")
        self.assertNotIn("php", name)

    def test_no_picture_still_saves_the_profile(self):
        self.assertEqual(self._edit_profile().status_code, 200)

    # ── esports image ────────────────────────────────────────────────────────────────────────────
    def test_esport_image_that_is_not_a_picture_is_refused_before_the_face_check(self):
        resp = self.client.post("/auth/upload-esport-image/",
                                data={"esport_image": SimpleUploadedFile("me.jpg", HTML, content_type="image/jpeg")},
                                **self._auth(self.player))
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "not_an_image"))

    # ── news editor images ───────────────────────────────────────────────────────────────────────
    def test_news_image_that_is_not_a_picture_is_refused(self):
        editor = self._user("door_editor", "dooreditor@gmail.com", role="admin", granular=("head_admin",))
        resp = self.client.post("/auth/upload-news-image/",
                                data={"image": SimpleUploadedFile("chart.png", HTML, content_type="image/png")},
                                **self._auth(editor))
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "not_an_image"))

    def test_news_image_real_picture_is_accepted(self):
        editor = self._user("door_editor2", "dooreditor2@gmail.com", role="admin", granular=("head_admin",))
        resp = self.client.post("/auth/upload-news-image/",
                                data={"image": SimpleUploadedFile("chart.png", png_bytes(), content_type="image/png")},
                                **self._auth(editor))
        self.assertEqual(resp.status_code, 200, resp.content)
        url = json.dumps(resp.json())
        self.assertTrue(re.search(r"[0-9a-f]{32}", url), url)
