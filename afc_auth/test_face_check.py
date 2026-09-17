"""
afc_auth/test_face_check.py - the esport-image picture check records what it saw and never blocks.

Owner 2026-09-13. The check went advisory on 2026-07-06 (Haar was false-rejecting real bust shots)
and its verdict was thrown away, so junk sat on the site unseen. Now the verdict is RECORDED and a
human works a queue. These tests cover the contract, not the detector's eyesight: how well it sees
was measured over 121 real roster images and is written up in GATES-esport-image-check-2026-09-13.md
(0 of 116 real busts flagged, 5 of 5 known junk flagged). A unit test cannot assert that without
shipping photographs of real players into the repo.

Run: ../backend/.venv/Scripts/python.exe manage.py test afc_auth.test_face_check
"""
import shutil
import tempfile
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from PIL import Image

from afc_auth import face_check
from afc_auth.models import SessionToken, User, UserProfile, canonical_profile

_MEDIA = tempfile.mkdtemp(prefix="afc_face_check_")


def _jpeg(color=(200, 30, 30), size=(400, 400)):
    """A genuinely decodable JPEG, so the detector takes its real path rather than an error path."""
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _upload(name="shot.jpg", body=None):
    return SimpleUploadedFile(name, body if body is not None else _jpeg(), content_type="image/jpeg")


class VerdictShapeTests(TestCase):
    """What check_esport_image answers, for inputs whose verdict is not a matter of eyesight."""

    def test_a_flat_colour_is_certainly_not_a_person(self):
        # Nothing face-like at any threshold: the one verdict a caller may refuse over.
        out = face_check.check_esport_image(BytesIO(_jpeg()))
        self.assertEqual(out["verdict"], face_check.NOT_A_PERSON)
        self.assertTrue(face_check.is_certainly_not_a_person(out["verdict"]))
        self.assertEqual(out["face_share"], 0.0)
        self.assertIn(out["detector"], ("yunet", "haar"))

    def test_something_face_like_but_unsure_is_flagged_not_refused(self):
        # The gap between "sure there is nobody" and "cannot see a face here" is the whole point:
        # the worst real photographs on the site score 0.198 and up, logos score under 0.15.
        self.assertLess(face_check.NOT_A_PERSON_SCORE, 0.198)
        with patch.object(face_check, "_best_candidate", return_value=0.30):
            out = face_check.check_esport_image(BytesIO(_jpeg()))
        self.assertEqual(out["verdict"], face_check.NO_FACE)
        self.assertFalse(face_check.is_certainly_not_a_person(out["verdict"]))

    def test_a_second_look_that_fails_never_refuses(self):
        # The second look is the only thing standing between a player and a refusal, so when IT
        # breaks it must answer "unsure" (1.0), never "nobody there".
        import cv2
        self.assertEqual(face_check._best_candidate(cv2, None), 1.0)
        with patch.object(face_check, "_best_candidate", return_value=1.0):
            out = face_check.check_esport_image(BytesIO(_jpeg()))
        self.assertEqual(out["verdict"], face_check.NO_FACE)

    def test_the_bundled_yunet_model_is_there_and_is_used(self):
        # The model is committed on purpose: the check must work on a box with no internet egress.
        self.assertTrue(face_check.MODEL_PATH.exists(), face_check.MODEL_PATH)
        self.assertEqual(face_check.check_esport_image(BytesIO(_jpeg()))["detector"], "yunet")

    def test_every_unreadable_input_is_skipped_not_blamed(self):
        for value in (b"", b"not an image at all", BytesIO(b"")):
            out = face_check.check_esport_image(value)
            self.assertEqual(out["verdict"], face_check.SKIPPED, value)
        # ...and "skipped" reads as allowed through the old two-value wrapper.
        self.assertTrue(face_check.image_has_human_face(b"")[0])

    def test_the_file_is_rewound_so_the_caller_can_still_save_it(self):
        f = BytesIO(_jpeg())
        face_check.check_esport_image(f)
        self.assertEqual(f.tell(), 0)
        self.assertTrue(f.read(2))

    def test_the_old_two_value_wrapper_maps_the_verdicts(self):
        for verdict, expected in ((face_check.OK, True), (face_check.SKIPPED, True),
                                  (face_check.NO_FACE, False), (face_check.FACE_TOO_SMALL, False)):
            with patch.object(face_check, "check_esport_image",
                              return_value={"verdict": verdict, "reason": verdict}):
                self.assertEqual(face_check.image_has_human_face(b"x")[0], expected, verdict)

    def test_a_face_that_fills_too_little_of_the_frame_is_not_a_bust_shot(self):
        # A full-body shot on a photo set measured 0.036 of frame height; the smallest real bust
        # measured 0.123. The floor sits between them.
        self.assertLess(face_check.MIN_FACE_SHARE, 0.123)
        self.assertGreater(face_check.MIN_FACE_SHARE, 0.036)
        with patch.object(face_check, "_detect_yunet", return_value=(True, 0.9, 0.04, "yunet")):
            self.assertEqual(face_check.check_esport_image(BytesIO(_jpeg()))["verdict"],
                             face_check.FACE_TOO_SMALL)
        with patch.object(face_check, "_detect_yunet", return_value=(True, 0.9, 0.20, "yunet")):
            self.assertEqual(face_check.check_esport_image(BytesIO(_jpeg()))["verdict"], face_check.OK)

    def test_haar_is_the_fallback_when_yunet_cannot_run(self):
        with patch.object(face_check, "_detect_yunet", return_value=(False, 0.0, 0.0, "none")):
            self.assertEqual(face_check.check_esport_image(BytesIO(_jpeg()))["detector"], "haar")


@override_settings(MEDIA_ROOT=_MEDIA)
class UploadRecordsTheVerdictTests(TestCase):
    """The player upload never refuses, and the verdict lands on the profile."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create(username="player", email="p@x.com", full_name="Player", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {SessionToken.objects.create(user=self.user, token='tok_p').token}"}
        self.client = Client()

    def _post(self, body=None):
        return self.client.post("/auth/upload-esport-image/", {"esport_image": _upload(body=body)}, **self.auth)

    def _flagged(self, verdict=face_check.NO_FACE):
        """Patch the check to the given verdict: the detector's eyesight is measured elsewhere, this
        class is about what the endpoint DOES with each answer."""
        return patch("afc_auth.face_check.check_esport_image",
                     return_value={"verdict": verdict, "reason": verdict, "confidence": 0.3,
                                   "face_share": 0.0, "detector": "yunet"})

    def test_a_picture_with_nobody_in_it_is_REFUSED_with_a_clear_error(self):
        r = self._post()  # a flat colour: nothing face-like at any threshold
        self.assertEqual(r.status_code, 400, r.content[:200])
        body = r.json()
        self.assertEqual(body["code"], "not_a_person")
        self.assertIn("photo of a person", body["message"])
        self.assertIn("photo of yourself", body["message"])
        # nothing was saved, and no verdict was recorded against them
        profile = canonical_profile(self.user)
        self.assertFalse(profile and profile.esports_pic)

    def test_a_flagged_image_still_uploads_and_is_recorded(self):
        with self._flagged():
            r = self._post()
        self.assertEqual(r.status_code, 200, r.content[:200])
        profile = canonical_profile(self.user)
        self.assertEqual(profile.esports_pic_check, face_check.NO_FACE)
        self.assertIsNotNone(profile.esports_pic_checked_at)
        self.assertTrue(profile.esports_pic)

    def test_a_flagged_upload_warns_about_the_ban_rule(self):
        # Owner 2026-09-13: "for the ones it flags it should warn them that they can get banned for
        # uploading images that are not their faces."
        for verdict in (face_check.NO_FACE, face_check.FACE_TOO_SMALL):
            with self._flagged(verdict):
                r = self._post()
            body = r.json()
            self.assertEqual(r.status_code, 200, body)
            self.assertEqual(body["code"], verdict)
            self.assertIn("banned", body["warning"])
            self.assertIn("review", body["warning"])

    def test_a_clean_upload_carries_no_warning(self):
        with self._flagged("ok"):
            r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("warning", r.json())
        self.assertNotIn("code", r.json())

    def test_a_good_image_is_recorded_ok(self):
        with patch("afc_auth.face_check.check_esport_image",
                   return_value={"verdict": "ok", "reason": "face", "confidence": 0.9,
                                 "face_share": 0.3, "detector": "yunet"}):
            r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(canonical_profile(self.user).esports_pic_check, "ok")

    def test_replacing_a_cleared_image_re_checks_the_new_one(self):
        # A clear is about the IMAGE. A new picture gets the new picture's verdict.
        profile = canonical_profile(self.user, create=True)
        profile.esports_pic_check = "cleared"
        profile.save(update_fields=["esports_pic_check"])
        with self._flagged():
            self.assertEqual(self._post().status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.esports_pic_check, face_check.NO_FACE)

    def test_the_upload_is_never_refused_even_when_the_check_explodes(self):
        with patch("afc_auth.face_check._read", side_effect=RuntimeError("boom")):
            r = self._post()
        self.assertEqual(r.status_code, 200, r.content[:200])


@override_settings(MEDIA_ROOT=_MEDIA)
class CheckEsportImagesCommandTests(TestCase):
    """The backfill fills in the verdicts the site never stored, and settles nothing itself."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def _profile(self, name, check=""):
        user = User.objects.create(username=name, email=f"{name}@x.com", full_name=name, password="x")
        profile = UserProfile.objects.create(user=user, esports_pic_check=check)
        profile.esports_pic.save(f"{name}.jpg", SimpleUploadedFile(f"{name}.jpg", _jpeg()), save=True)
        return profile

    def test_dry_run_counts_and_writes_nothing(self):
        p = self._profile("a")
        call_command("check_esport_images", "--dry-run")
        p.refresh_from_db()
        self.assertEqual(p.esports_pic_check, "")

    def test_it_records_a_verdict_and_leaves_a_cleared_row_alone(self):
        unchecked = self._profile("b")
        cleared = self._profile("c", check="cleared")
        call_command("check_esport_images")
        unchecked.refresh_from_db()
        cleared.refresh_from_db()
        self.assertEqual(unchecked.esports_pic_check, face_check.NOT_A_PERSON)
        self.assertIsNotNone(unchecked.esports_pic_checked_at)
        self.assertEqual(cleared.esports_pic_check, "cleared")  # a human settled it; nothing overrules that

    def test_a_second_run_skips_what_it_already_recorded_unless_asked(self):
        p = self._profile("d")
        call_command("check_esport_images")
        p.refresh_from_db()
        first = p.esports_pic_checked_at
        call_command("check_esport_images")          # no --recheck: this row is already done
        p.refresh_from_db()
        self.assertEqual(p.esports_pic_checked_at, first)
        call_command("check_esport_images", "--recheck")
        p.refresh_from_db()
        self.assertNotEqual(p.esports_pic_checked_at, first)

    def test_a_row_whose_file_is_gone_is_skipped_not_recorded(self):
        p = self._profile("e")
        p.esports_pic.storage.delete(p.esports_pic.name)
        call_command("check_esport_images")
        p.refresh_from_db()
        self.assertEqual(p.esports_pic_check, "")
