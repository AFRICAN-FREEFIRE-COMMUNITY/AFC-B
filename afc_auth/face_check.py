# ─────────────────────────────────────────────────────────────────────────────
# face_check.py  (owner 2026-06-20)
#
# PURPOSE
#   Lightweight, FREE, fully-local "is this an actual human picture?" gate for the
#   esport-image upload. The owner wants to stop players from uploading random
#   gallery junk (logos, screenshots, memes) as their esport image. This does NOT
#   use any paid AI API: it runs OpenCV Haar cascades that already ship with
#   opencv-python (see requirements.txt: opencv-python / opencv-python-headless),
#   so there is zero per-call cost and nothing to authenticate.
#
# WHO CALLS IT
#   afc_auth.views.upload_esport_image -> image_has_human_face(file) before saving
#   UserProfile.esports_pic. ADVISORY since 2026-07-06: a no-face verdict is only
#   logged and the upload saves anyway (Haar recall false-rejected real bust shots -
#   tilted heads, caps/headsets, low light). The hard 400 gate survives ONLY in the
#   admin media-audit upload (views_media_audit.py), which has a force override.
#
# DESIGN NOTES
#   - FAIL-OPEN: any detector/import problem returns (True, "skipped") so a broken
#     OpenCV install or an exotic image format can NEVER block a legitimate upload.
#     This is a quality nudge, not a security boundary.
#   - Multiple cascades (frontal default + frontal alt2 + profile, plus a
#     horizontally-flipped profile pass) so a face turned either way still passes.
#   - Image is downscaled to <=1000px on the long edge before detection purely for
#     speed; histogram equalisation improves recall on dark/backlit shots.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Long-edge cap fed to the detector. Esport busts are large faces, so downscaling
# this far costs no real recall and keeps detection well under ~100ms.
_MAX_DIM = 1000


def image_has_human_face(image_file) -> tuple[bool, str]:
    """
    Return (has_face, reason).

    image_file : a Django UploadedFile / file-like with .read(), OR raw bytes.
    has_face   : True if at least one human face is detected (or the check was
                 skipped because the detector is unavailable - fail-open).
    reason     : short machine tag ("face" / "no_face" / "skipped:<why>").

    Never raises: every failure path returns (True, "skipped:...") so the caller
    can treat a True result as "allowed to upload".
    """
    # ── read raw bytes (accept both a file object and bytes) ──────────────────
    try:
        if hasattr(image_file, "read"):
            try:
                image_file.seek(0)
            except Exception:
                pass
            data = image_file.read()
            try:
                image_file.seek(0)  # rewind so the caller can still save the file
            except Exception:
                pass
        else:
            data = image_file
        if not data:
            return True, "skipped:empty"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("face_check: could not read upload (%s); allowing", exc)
        return True, "skipped:read_error"

    # ── lazy import so a missing OpenCV/numpy never breaks the upload path ─────
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # opencv/numpy not installed in this env
        logger.warning("face_check: opencv/numpy unavailable (%s); allowing", exc)
        return True, "skipped:no_cv2"

    try:
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            # Not a decodable raster (e.g. SVG/HEIC) - don't punish the user.
            return True, "skipped:undecodable"

        # Downscale long edge to _MAX_DIM for speed.
        h, w = img.shape[:2]
        scale = _MAX_DIM / float(max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        img = cv2.equalizeHist(img)

        # minSize keyed off the (downscaled) frame so we ignore tiny logo specks
        # but still accept a normal bust shot.
        sh, sw = img.shape[:2]
        min_side = max(40, int(min(sh, sw) * 0.08))
        min_size = (min_side, min_side)

        cascades = [
            "haarcascade_frontalface_default.xml",
            "haarcascade_frontalface_alt2.xml",
            "haarcascade_profileface.xml",
        ]
        flipped = cv2.flip(img, 1)  # profile cascade only finds one orientation

        for name in cascades:
            clf = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if clf.empty():
                continue
            faces = clf.detectMultiScale(img, scaleFactor=1.1, minNeighbors=5, minSize=min_size)
            if len(faces) > 0:
                return True, "face"
            if "profileface" in name:
                faces = clf.detectMultiScale(flipped, scaleFactor=1.1, minNeighbors=5, minSize=min_size)
                if len(faces) > 0:
                    return True, "face"

        return False, "no_face"
    except Exception as exc:  # pragma: no cover - any detector hiccup => allow
        logger.warning("face_check: detection error (%s); allowing", exc)
        return True, "skipped:error"
