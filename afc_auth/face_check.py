# ─────────────────────────────────────────────────────────────────────────────
# face_check.py  (owner 2026-06-20; detector replaced 2026-09-13)
#
# PURPOSE
#   Lightweight, FREE, fully-local "is this a picture of a person?" check for the
#   esport image. The owner wants to stop players using random gallery junk
#   (team logos, in-game screenshots, anime wallpapers, tournament posters) as the
#   image organizers put in broadcast graphics. No paid AI API, no per-call cost,
#   nothing to authenticate: it runs on our own box.
#
# WHO CALLS IT
#   - afc_auth.views.upload_esport_image: records the verdict on the profile and
#     ALWAYS saves. It has never refused since 2026-07-06 and still does not.
#   - afc_tournament_and_scrims.views_media_audit.media_upload: the ADMIN replace
#     path, which DOES refuse a no-face image unless the operator passes force=true.
#   - afc_auth.management.commands.check_esport_images: re-checks stored images.
#
# WHY THE DETECTOR CHANGED (2026-09-13)
#   The original gate used OpenCV Haar cascades (2001-era). It was demoted to
#   advisory on 2026-07-06 because it false-rejected REAL bust shots (a hand on the
#   chin, a cap, a headset, low light) and locked players out of events that require
#   an esport image. Measured on 121 real AFC roster images on 2026-09-13:
#
#       detector   real busts missed (n=116)   the 5 known junk images
#       Haar       2                           4 flagged, 1 wrongly passed
#       YuNet      0                           4 flagged, the 5th caught by size
#
#   So YuNet (cv2.FaceDetectorYN, a 232KB ONNX model from the OpenCV zoo, bundled in
#   afc_auth/assets/) both misses fewer real players AND catches more junk. Haar stays
#   as the fallback for an environment where the model or the API is missing.
#
# WHEN IT REFUSES (owner 2026-09-13: "a proper error if someone uploads an image that its very
# sure is not a human")
#   Refusing is reserved for the one case the detector can be SURE about: it looked again with the
#   threshold wound right down and still found nothing face-like. The line is drawn from what the
#   1,499 esport images already on the site actually score:
#       every real bust shot on the 121-image roster        >= 0.867
#       the worst real photos anybody has uploaded (a dark
#       indoor selfie, a blurry one, a face behind a filter) 0.198 .. 0.352
#       logos, in-game screenshots, anime, black cards       0.000 .. 0.150 (52 of the 1,499)
#   NOT_A_PERSON_SCORE is 0.15: under the worst real photograph measured, over the whole logo and
#   screenshot band. It refuses about 3.5% of what is on the site and, on this evidence, no real
#   person except one screenshot of a nearly black selfie - which the message tells them to retake.
#
#   It is deliberately CAUTIOUS. Raising it to 0.45 would refuse 116 instead of 52 (it would catch
#   the anime wallpapers and the stock football photo too), but it would also refuse about five real
#   photographs, and false rejections are precisely what killed the first version of this gate on
#   2026-07-06. Everything between this line and a clean pass still SAVES, is queued for a human,
#   and is answered with the ban warning.
#
# WHAT IT CANNOT DO, AND WHY EVERYTHING ELSE ONLY FLAGS
#   A face detector answers "is there a face here", never "is this YOU" and never
#   "is this a photograph". On the same 2026-09-13 measurement YuNet still called an
#   esport logo with a cartoon soldier, an anime avatar and a photo of somebody
#   else's family a face. So a FLAGGED verdict is a queue for a human (the per-event
#   media audit), never a refusal, and a clean verdict is not a guarantee.
#
# DESIGN NOTES
#   - FAIL-OPEN everywhere: any read, import or detector problem returns "skipped",
#     which callers treat as allowed. A broken OpenCV can never block an upload.
#   - MIN_FACE_SHARE: a bust shot's face fills a real part of the frame. The smallest
#     real bust measured was 0.123 of frame height; a full-body shot on a photo set
#     measured 0.036. The floor is 0.08, under every real bust and over the full-body.
#   - The image is downscaled to <=1000px on the long edge before detection, purely
#     for speed (detection stays well under ~100ms).
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Long-edge cap fed to the detector. Esport busts are large faces, so downscaling
# this far costs no real recall and keeps detection fast.
_MAX_DIM = 1000

# The bundled YuNet model (OpenCV zoo, face_detection_yunet_2023mar). Committed
# because it is 232KB and the check must work on a box with no internet egress.
MODEL_PATH = Path(__file__).resolve().parent / "assets" / "face_detection_yunet_2023mar.onnx"

# YuNet score threshold. 0.7 keeps the real busts (lowest real confidence measured
# was 0.72) without inviting the low-confidence noise a wallpaper produces.
_YUNET_SCORE = 0.7
_YUNET_NMS = 0.3

# The face must fill at least this share of the frame HEIGHT to read as a bust shot.
MIN_FACE_SHARE = 0.08

# Verdicts written to UserProfile.esports_pic_check. Anything not "ok" is a flag for
# the media-audit queue; "skipped" means the check could not run and nobody is blamed.
OK = "ok"
NO_FACE = "no_face"
FACE_TOO_SMALL = "face_too_small"
SKIPPED = "skipped"
# The only verdict a caller may refuse an upload over: a second, far more forgiving look found
# nothing face-like at all. See the measurement in the header.
NOT_A_PERSON = "not_a_person"

# The forgiving second look, and the score under which "there is no person here" is safe to assert.
_YUNET_LOW_SCORE = 0.1
NOT_A_PERSON_SCORE = 0.15


def check_esport_image(image_file) -> dict:
    """
    Look at one esport image and describe what is there.

    image_file : a Django UploadedFile / file-like with .read(), OR raw bytes.

    Returns a dict, always, never raises:
        verdict    "ok" | "not_a_person" | "no_face" | "face_too_small" | "skipped"
        reason     short machine tag, e.g. "no_face", "skipped:no_cv2"
        confidence detector score for the biggest face (0.0 when none)
        face_share biggest face height / image height (0.0 when none)
        detector   "yunet" | "haar" | "none"

    A verdict other than "ok" is a FLAG for a human, never grounds to refuse the
    upload: see the header on what a face detector cannot decide.
    """
    # "Never raises" is the promise the upload path relies on, so it is kept HERE rather than
    # assumed from the internals: a test that made the reader itself explode still has to leave the
    # upload working (2026-09-13).
    try:
        return _check(image_file)
    except Exception as exc:  # pragma: no cover - belt and braces around a promise
        logger.warning("face_check: unexpected failure (%s); skipping", exc)
        return _result(SKIPPED, "skipped:error")


def _check(image_file) -> dict:
    data = _read(image_file)
    if data is None:
        return _result(SKIPPED, "skipped:read_error")
    if not data:
        return _result(SKIPPED, "skipped:empty")

    try:
        import cv2
        import numpy as np
    except Exception as exc:  # opencv/numpy not installed in this env
        logger.warning("face_check: opencv/numpy unavailable (%s); skipping", exc)
        return _result(SKIPPED, "skipped:no_cv2")

    try:
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            # Not a decodable raster (e.g. SVG) - don't punish the user.
            return _result(SKIPPED, "skipped:undecodable")

        h, w = img.shape[:2]
        scale = _MAX_DIM / float(max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        found, confidence, share, detector = _detect_yunet(cv2, img)
        if detector == "none":
            found, confidence, share, detector = _detect_haar(cv2, img)
        if detector == "none":
            return _result(SKIPPED, "skipped:no_detector")

        if not found:
            # Sure, or merely unsure? Look again with the threshold wound down: a dark selfie or a
            # face behind a filter still scores something, a logo or a screenshot scores nothing.
            # Only the second answer is safe to refuse an upload over.
            best = _best_candidate(cv2, img) if detector == "yunet" else 1.0
            if best < NOT_A_PERSON_SCORE:
                return _result(NOT_A_PERSON, NOT_A_PERSON, best, 0.0, detector)
            return _result(NO_FACE, NO_FACE, best, 0.0, detector)
        if share < MIN_FACE_SHARE:
            # A person IS in the picture, but as scenery: a full-body shot on a photo
            # set, a figure in a poster. Not the bust an organizer can put in a graphic.
            return _result(FACE_TOO_SMALL, FACE_TOO_SMALL, confidence, share, detector)
        return _result(OK, "face", confidence, share, detector)
    except Exception as exc:  # pragma: no cover - any detector hiccup => skip, never block
        logger.warning("face_check: detection error (%s); skipping", exc)
        return _result(SKIPPED, "skipped:error")


def _best_candidate(cv2, img) -> float:
    """The best face-candidate score when the detector is being generous. 0.0 when there is nothing
    face-like in the picture at all. Only called when the normal pass found nothing, so it costs a
    second detection on the images that are already the interesting ones."""
    try:
        h, w = img.shape[:2]
        det = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (w, h), _YUNET_LOW_SCORE, _YUNET_NMS, 5000)
        _rc, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            return 0.0
        return float(max(f[14] for f in faces))
    except Exception as exc:  # pragma: no cover - never let the second look break the first
        logger.warning("face_check: second look failed (%s); treating as unsure", exc)
        return 1.0  # unsure -> flag, never refuse


def image_has_human_face(image_file) -> tuple[bool, str]:
    """
    (has_face, reason) - the original two-value shape, kept because two callers read it.

    has_face is True when the picture is fine AND when the check could not run
    (fail-open), so a caller can treat True as "allowed".
    """
    out = check_esport_image(image_file)
    return out["verdict"] in (OK, SKIPPED), out["reason"]


def is_certainly_not_a_person(verdict: str) -> bool:
    """Whether a verdict is the one a caller may REFUSE an upload over. One place, so a second
    caller cannot quietly start refusing on a softer signal."""
    return verdict == NOT_A_PERSON


# ── internals ────────────────────────────────────────────────────────────────
def _read(image_file):
    """Raw bytes from a file-like or bytes; None on a read error. Rewinds the file so
    the caller can still save it."""
    try:
        if hasattr(image_file, "read"):
            try:
                image_file.seek(0)
            except Exception:
                pass
            data = image_file.read()
            try:
                image_file.seek(0)
            except Exception:
                pass
            return data
        return image_file
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("face_check: could not read upload (%s); skipping", exc)
        return None


def _result(verdict, reason, confidence=0.0, share=0.0, detector="none") -> dict:
    return {
        "verdict": verdict,
        "reason": reason,
        "confidence": round(float(confidence), 3),
        "face_share": round(float(share), 3),
        "detector": detector,
    }


def _detect_yunet(cv2, img):
    """(found, confidence, face_share, detector). detector is "none" when YuNet is not
    usable here, which tells the caller to fall back to Haar."""
    if not MODEL_PATH.exists():
        return False, 0.0, 0.0, "none"
    try:
        creator = cv2.FaceDetectorYN.create  # cv2 >= 4.5.4
    except AttributeError:
        return False, 0.0, 0.0, "none"
    try:
        h, w = img.shape[:2]
        det = creator(str(MODEL_PATH), "", (w, h), _YUNET_SCORE, _YUNET_NMS, 5000)
        _rc, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            return False, 0.0, 0.0, "yunet"
        # Each row is [x, y, w, h, 5 landmark pairs..., score]. The BIGGEST face is the
        # subject; a background face in a crowd is not what the picture is of.
        biggest = max(faces, key=lambda f: f[3])
        return True, float(biggest[14]), float(biggest[3]) / float(h), "yunet"
    except Exception as exc:
        logger.warning("face_check: yunet failed (%s); falling back to haar", exc)
        return False, 0.0, 0.0, "none"


_HAAR_CASCADES = (
    "haarcascade_frontalface_default.xml",
    "haarcascade_frontalface_alt2.xml",
    "haarcascade_profileface.xml",
)


def _detect_haar(cv2, img):
    """The original 2026-06-20 detector, kept as the fallback. Same knobs it shipped
    with: histogram equalisation for dark shots, a minSize keyed off the frame, and a
    flipped pass because the profile cascade only finds one orientation."""
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    sh, sw = gray.shape[:2]
    min_side = max(40, int(min(sh, sw) * 0.08))
    flipped = cv2.flip(gray, 1)
    usable = False
    for name in _HAAR_CASCADES:
        clf = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        if clf.empty():
            continue
        usable = True
        for src in (gray, flipped) if "profileface" in name else (gray,):
            faces = clf.detectMultiScale(src, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(min_side, min_side))
            if len(faces) > 0:
                biggest = max(faces, key=lambda f: f[3])
                # Haar has no score; report 1.0 so a caller comparing detectors can see
                # the number came from a detector that does not rank its answers.
                return True, 1.0, float(biggest[3]) / float(sh), "haar"
    return (False, 0.0, 0.0, "haar") if usable else (False, 0.0, 0.0, "none")
