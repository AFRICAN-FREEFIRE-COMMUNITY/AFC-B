"""
afc_mail/assets.py - pictures and fonts an admin uploads for an email (POST mail/assets/).

PICTURES go through AFC's one image door, afc_auth.image_utils.require_image_upload: the bytes are
decoded by Pillow (JPEG, PNG, WEBP, GIF, HEIF only), re-encoded (a picture with transparency stays PNG),
shrunk to 1200 px on the long side, and stored under an opaque name (owner rule R87). A designed
picture (drawn in the browser from layers, #284) is uploaded the same way, as a PNG.

FONTS are recognised by their signature, never by the file name: TrueType and OpenType through
afc_auth.upload_safety.sniff_upload, WOFF and WOFF2 by their own four-byte marks. 5 MB at most, stored
under an opaque name with the extension of what the bytes are. The family name the email declares is
made from the file name with everything but letters, digits and spaces removed.

TRANSPARENCY (#284: "change transpaceny of image uploaded"): email apps do not all honour CSS opacity
(Outlook ignores it), so a picture set below full opacity gets its own PNG with the alpha multiplied in,
made once per (picture, opacity) and kept under mail/baked/.

CONNECTS TO: afc_mail.views.assets (the endpoint), afc_mail.document (picture addresses),
afc_mail.content (fonts' @font-face), afc_mail.designs (pictures packed inside an uploaded design).
"""
import io
import os
import re

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from afc_auth.image_utils import IMAGE_TOO_LARGE, NOT_AN_IMAGE, require_image_upload
from afc_auth.upload_safety import opaque_name, sniff_upload

from .addresses import media_url
from .models import MailAsset

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FONT_BYTES = 5 * 1024 * 1024
MAX_SIDE = 1200
FONT_TOO_LARGE = "mail_font_too_large"
NOT_A_FONT = "mail_not_a_font"


def _family_from(name):
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    family = re.sub(r"[^A-Za-z0-9 ]+", " ", stem)
    family = re.sub(r"\s+", " ", family).strip()[:40]
    return family or "Uploaded font"


def store_image(uploaded, user):
    """(MailAsset, None) or (None, code). Code is afc_auth's not_an_image / image_too_large."""
    cleaned, code = require_image_upload(uploaded, max_bytes=MAX_IMAGE_BYTES, max_dim=MAX_SIDE)
    if code:
        return None, code
    width = height = None
    try:
        from PIL import Image
        cleaned.seek(0)
        with Image.open(cleaned) as img:
            width, height = img.size
            fmt = (img.format or "").upper()
        cleaned.seek(0)
    except Exception:
        return None, NOT_AN_IMAGE
    content_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt, "image/jpeg")
    asset = MailAsset(kind="image", content_type=content_type, width=width, height=height,
                      original_name=os.path.basename(getattr(uploaded, "name", "") or "")[:120], created_by=user)
    asset.file.save(cleaned.name, cleaned, save=False)
    asset.save()
    return asset, None


def store_image_bytes(data, user, name="picture"):
    """store_image for bytes in memory (a picture packed inside an uploaded design)."""
    upload = ContentFile(data, name=name)
    upload.size = len(data)
    return store_image(upload, user)


def _font_kind(uploaded):
    uploaded.seek(0)
    head = uploaded.read(4)
    uploaded.seek(0)
    if head == b"wOF2":
        return ".woff2", "font/woff2"
    if head == b"wOFF":
        return ".woff", "font/woff"
    kind, ext, mime = sniff_upload(uploaded, ["font"])
    uploaded.seek(0)
    if kind == "font":
        return ext, mime
    return None, None


def store_font(uploaded, user):
    """(MailAsset, None) or (None, code)."""
    if uploaded is None:
        return None, NOT_A_FONT
    if (getattr(uploaded, "size", 0) or 0) > MAX_FONT_BYTES:
        return None, FONT_TOO_LARGE
    ext, mime = _font_kind(uploaded)
    if not ext:
        return None, NOT_A_FONT
    family = _family_from(getattr(uploaded, "name", ""))
    taken = set(MailAsset.objects.filter(kind="font", family__startswith=family).values_list("family", flat=True))
    if family in taken:
        n = 2
        while f"{family} {n}" in taken:
            n += 1
        family = f"{family} {n}"
    asset = MailAsset(kind="font", content_type=mime, family=family,
                      original_name=os.path.basename(getattr(uploaded, "name", "") or "")[:120], created_by=user)
    uploaded.seek(0)
    asset.file.save(opaque_name(ext), ContentFile(uploaded.read()), save=False)
    asset.save()
    return asset, None


def public_url(asset):
    return media_url(asset.file.name)


def baked_url(asset, opacity):
    """The picture's address at this opacity: the picture itself when fully opaque, otherwise a PNG
    with the transparency baked in (made the first time it is asked for)."""
    if asset is None or asset.kind != "image":
        return None
    if opacity is None or opacity >= 0.995:
        return public_url(asset)
    pct = max(10, min(99, int(round(opacity * 100))))
    name = f"mail/baked/{asset.token}-o{pct}.png"
    if not default_storage.exists(name):
        from PIL import Image
        with asset.file.open("rb") as fh, Image.open(fh) as img:
            rgba = img.convert("RGBA")
            alpha = rgba.getchannel("A").point(lambda a: int(a * pct / 100))
            rgba.putalpha(alpha)
            buf = io.BytesIO()
            rgba.save(buf, format="PNG", optimize=True)
        name = default_storage.save(name, ContentFile(buf.getvalue()))
    return media_url(name)


def load(tokens, *, kind=None):
    """{token: MailAsset} for the tokens given (one query)."""
    qs = MailAsset.objects.filter(token__in=list(tokens or []))
    if kind:
        qs = qs.filter(kind=kind)
    return {a.token: a for a in qs}


def serialize(asset):
    out = {"token": asset.token, "kind": asset.kind, "url": public_url(asset), "content_type": asset.content_type}
    if asset.kind == "image":
        out.update({"width": asset.width, "height": asset.height})
    else:
        out["family"] = asset.family
    return out


__all__ = ["store_image", "store_image_bytes", "store_font", "public_url", "baked_url", "load", "serialize",
           "NOT_AN_IMAGE", "IMAGE_TOO_LARGE", "NOT_A_FONT", "FONT_TOO_LARGE"]
