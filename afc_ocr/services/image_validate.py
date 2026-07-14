# afc_ocr/services/image_validate.py
#
# PURPOSE (spec A8)
# ----------------
# One reusable, server-side guard for the images that get uploaded to any OCR read, run
# BEFORE a single Gemini call is made. It caps the file COUNT and per-file SIZE, restricts
# the content type to the formats Gemini reads reliably inline (PNG / JPG / WEBP), and gives
# an iPhone HEIC/HEIF upload a clear, friendly "share it as a PNG or JPG" message instead of
# a cryptic downstream failure.
#
# WHY IT LIVES HERE / WHY NOT A GLOBAL SETTING
# --------------------------------------------
# settings.py sets NEITHER DATA_UPLOAD_MAX_MEMORY_SIZE NOR FILE_UPLOAD_MAX_MEMORY_SIZE (by
# design, other endpoints such as the shop / design-asset uploads have their own, larger
# caps and a restrictive global would break them). So the cap has to be applied per OCR
# endpoint. This module is the single place that logic lives, so every OCR upload path shares
# the exact same limits and copy.
#
# WHO CALLS THIS (the OCR upload views wire this in)
# --------------------------------------------------
#   * afc_ocr.views.upload_ocr_session       -> POST /events/ocr-match-result/  (event flow)
#   * afc_leaderboard.views.ocr_extract      -> standalone single-shot extract  (wrap its one
#                                               "screenshot" file in a list before calling)
#   * afc_leaderboard.views.ocr_job_create   -> standalone batch job create     (the "images"
#                                               file list)
# Each caller resolves its uploaded files and does:
#     if (image_err := validate_ocr_images(files)):
#         return Response({"message": image_err}, status=400)
# i.e. a non-None return is the client-safe error string; None means "every file passed".
# The check runs BEFORE touching the extraction engine (afc_ocr.services.extract.extract_rows
# / .gemini.call_gemini).
#
# CONTRACT
# --------
# validate_ocr_images(files) -> str | None
#   None            -> every file passed.
#   "<client copy>" -> the first failing check; a client-safe string (no internals).
# It NEVER raises into the caller: any unexpected internal error fails OPEN (returns None) and
# is logged, because the extraction engine already degrades to a clean 503 on a bad image, so
# a validator hiccup must not block a legitimate upload.
#
# The numeric limits default here but are read from settings (afc/settings.py: OCR_MAX_IMAGE_
# BYTES, OCR_MAX_IMAGES) so ops can tune them per environment; the getattr fallbacks keep this
# module working even if a deployment omits those settings.

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Content types Gemini inline_data reads reliably. "image/jpg" is a non-standard label some
# clients send for a real JPEG, so it is accepted as an alias of "image/jpeg", rejecting a
# genuine JPG on a mislabel would be a bug, not a feature.
ALLOWED_OCR_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp"}

# HEIC/HEIF (the default iPhone photo format) is NOT a safe inline mime for Gemini, so it is
# rejected explicitly with a tailored message. Detected by content type OR file extension,
# because phones sometimes send an empty/odd content type for a HEIC file.
HEIC_MIME = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}
HEIC_EXTS = (".heic", ".heif")

# Fallback limits (mirrors afc_player_market.MAX_POST_IMAGE_BYTES = 10 MB). The authoritative
# values come from settings; these keep the module standalone-safe.
_DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024   # 10 MB / file
_DEFAULT_MAX_IMAGES = 8                        # files / request


def _max_image_bytes() -> int:
    """Per-file byte cap, from settings.OCR_MAX_IMAGE_BYTES (fallback 10 MB)."""
    return getattr(settings, "OCR_MAX_IMAGE_BYTES", _DEFAULT_MAX_IMAGE_BYTES)


def _max_images() -> int:
    """Max files per upload, from settings.OCR_MAX_IMAGES (fallback 8)."""
    return getattr(settings, "OCR_MAX_IMAGES", _DEFAULT_MAX_IMAGES)


def _is_heic(content_type: str, name: str) -> bool:
    """True if the file looks like an iPhone HEIC/HEIF photo, by mime OR extension."""
    if content_type in HEIC_MIME:
        return True
    return bool(name) and name.lower().endswith(HEIC_EXTS)


def validate_ocr_images(files):
    """Validate a list of uploaded OCR screenshots.

    Args:
        files: an iterable of uploaded file objects (Django UploadedFile-like: each exposes
               .content_type, .name and .size). A single-file caller (ocr_extract) should wrap
               its one file in a list before calling.

    Returns:
        str | None:
            None               every file passed.
            "<client copy>"    the first failing check; a string safe to show a user.

    Checks, in order: at least one file; count <= OCR_MAX_IMAGES; each file is PNG/JPG/WEBP
    (HEIC/HEIF gets its own message); each file <= OCR_MAX_IMAGE_BYTES.
    Never raises: unexpected internal errors fail open (return None) and are logged.
    """
    try:
        # Normalise to a concrete list so we can count and iterate safely (a QueryDict
        # getlist() already returns a list; this also tolerates a generator).
        file_list = [f for f in (files or []) if f is not None]

        # ---- count checks ----
        if not file_list:
            return "Attach at least one screenshot."

        max_count = _max_images()
        if len(file_list) > max_count:
            return f"Upload up to {max_count} screenshots at a time."

        # ---- per-file type + size checks ----
        max_bytes = _max_image_bytes()
        max_mb = max(1, max_bytes // (1024 * 1024))  # human-readable cap for the message

        for f in file_list:
            content_type = (getattr(f, "content_type", "") or "").strip().lower()
            name = (getattr(f, "name", "") or "").strip()

            # HEIC/HEIF: reject first, with the tailored "share as PNG or JPG" guidance so the
            # (very common) iPhone upload gets an actionable message, not the generic one.
            if _is_heic(content_type, name):
                return (
                    "HEIC/HEIF photos are not supported. On your phone, please share this "
                    "screenshot as a PNG or JPG and upload that."
                )

            if content_type not in ALLOWED_OCR_MIME:
                return (
                    "Only PNG, JPG or WEBP screenshots are accepted. Please share the image "
                    "as a PNG or JPG."
                )

            # .size can be absent on exotic file-likes; only enforce when we actually know it.
            size = getattr(f, "size", 0) or 0
            if size > max_bytes:
                return f"Each screenshot must be {max_mb} MB or smaller."

        return None

    except Exception:  # noqa: BLE001 - guard must never raise into the view; fail open + log.
        # The extraction engine already degrades to a clean 503 on a bad image, so a validator
        # bug must not block a legitimate upload. Log with detail for server-side triage.
        logger.exception("validate_ocr_images failed unexpectedly; allowing the upload through")
        return None
