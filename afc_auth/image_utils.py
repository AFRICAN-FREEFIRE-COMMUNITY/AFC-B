# ─────────────────────────────────────────────────────────────────────────────
# image_utils.py  (owner 2026-06-21)
#
# normalize_image_upload(): make an uploaded image web-safe AND storage-friendly
# before it is saved. It does two things:
#   1. Converts HEIC/HEIF (the iPhone default) to a normal format - browsers can't
#      render HEIC in <img>, and OpenCV can't decode it for the esports face check.
#   2. Downscales oversized images + recompresses them, so a 12-megapixel phone photo
#      (8-15 MB) is stored as a ~150-300 KB image. This keeps media storage from
#      filling up fast (owner ask 2026-06-21).
#
# Used by every image-upload view (upload_esport_image, edit_profile profile_pic,
# team create/edit team_logo). FAIL-SAFE: anything it can't open or that errors is
# returned UNCHANGED rather than blocking the upload - it never raises.
#
# Format choice: photos (esports image, profile pic) -> JPEG (force_jpeg=True), which
# is much smaller. Logos may need transparency, so they keep PNG when they have an
# alpha channel (force_jpeg=False, the default).
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# Register the HEIF/HEIC opener with Pillow on import (best-effort). If pillow-heif
# is not installed, HEIC uploads pass through unchanged (and won't display until the
# lib is installed) - never crashes.
try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIF_OK = True
except Exception:  # pragma: no cover - lib absent on this host
    _HEIF_OK = False

_HEIC_EXTS = (".heic", ".heif")

# Defaults: long edge cap + JPEG quality. 1280px is plenty for an avatar / esports
# bust / logo while cutting file size by ~10-30x vs a raw phone photo.
DEFAULT_MAX_DIM = 1280
DEFAULT_JPEG_QUALITY = 85


def normalize_image_upload(uploaded, *, max_dim=DEFAULT_MAX_DIM,
                           jpeg_quality=DEFAULT_JPEG_QUALITY, force_jpeg=False):
    """Return a web-displayable, size-bounded image upload (Django file).

    - HEIC/HEIF is decoded; oversized images are scaled down to `max_dim` on the long
      edge; the result is recompressed.
    - `force_jpeg=True` always outputs JPEG (best for photos). Otherwise an image with
      transparency is kept as PNG; everything else becomes JPEG.
    - Non-raster files (e.g. SVG) or any failure -> the ORIGINAL upload, untouched.
    """
    if uploaded is None:
        return uploaded

    name = (getattr(uploaded, "name", "") or "")
    lname = name.lower()
    ctype = (getattr(uploaded, "content_type", "") or "").lower()
    is_heic = lname.endswith(_HEIC_EXTS) or "heic" in ctype or "heif" in ctype

    if is_heic and not _HEIF_OK:
        logger.warning("HEIC upload but pillow-heif unavailable; storing as-is.")
        return uploaded

    try:
        from PIL import Image, ImageOps
        from django.core.files.base import ContentFile

        try:
            uploaded.seek(0)
        except Exception:
            pass

        img = Image.open(uploaded)
        # Respect the phone's EXIF rotation before any resize.
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # Keep transparency only when we're not forcing JPEG.
        has_alpha = img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        )
        use_png = has_alpha and not force_jpeg

        # Downscale the long edge to max_dim (never upscale).
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        buf = io.BytesIO()
        base = (name or "image").rsplit(".", 1)[0] or "image"
        if use_png:
            img.save(buf, format="PNG", optimize=True)
            out_name = f"{base}.png"
        else:
            img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            out_name = f"{base}.jpg"
        buf.seek(0)
        return ContentFile(buf.read(), name=out_name)
    except Exception as exc:
        logger.warning("Image normalize/resize failed (%s); storing original upload.", exc)
        try:
            uploaded.seek(0)
        except Exception:
            pass
        return uploaded
