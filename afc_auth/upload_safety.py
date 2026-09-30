# afc_auth/upload_safety.py
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# WHAT A FILE REALLY IS, AND THE NAME IT IS STORED UNDER (owner rule R87, 2026-09-30, inbox #98).
#
# R87: an uploaded file never runs and never opens as a page. The attack it is written against:
# upload a script with ".jpg" on the end, the server trusts the name or the type the browser
# declared, stores it "as a working file", and the attacker opens its address. On this site that
# address is https://api.africanfreefirecommunity.com/media/..., served by nginx by EXTENSION, so a
# stored "x.html" would open as a page on the API's own origin.
#
# Two rules, both enforced here so every upload path answers the same way:
#   1. The BYTES decide what a file is (sniff_upload), never its name or its declared content type.
#   2. The name on disk is opaque and its extension comes from the sniff (opaque_name), so neither
#      "x.php.jpg" nor a guessable "screenshot.png" ever reaches storage.
#
# Images additionally go through require_image_upload (afc_auth/image_utils.py), which decodes
# with Pillow and RE-ENCODES, dropping anything hidden in a valid image and the GPS EXIF.
#
# CALLERS: image_utils.require_image_upload (the image gate), afc_support._save_attachments
# (documents, pictures, video), afc_player_market report evidence (pictures, video),
# afc_organizers font library (fonts). Tests: afc_auth/tests_upload_hardening.py.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
import uuid

# kind -> {extension: mime}. The first extension of a kind is the one used when the bytes alone
# cannot tell two members apart and the client's own extension is not one of them.
_ZIP_DOCS = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             ".odt": "application/vnd.oasis.opendocument.text",
             ".zip": "application/zip"}
_OLE_DOCS = {".doc": "application/msword", ".xls": "application/vnd.ms-excel"}
_TEXT = {".txt": "text/plain", ".csv": "text/csv"}

HEAD_BYTES = 64


def _head(uploaded, n=HEAD_BYTES):
    try:
        uploaded.seek(0)
        data = uploaded.read(n)
        uploaded.seek(0)
        return data or b""
    except Exception:
        return b""


def _client_ext(uploaded):
    name = (getattr(uploaded, "name", "") or "").lower()
    return "." + name.rsplit(".", 1)[1] if "." in name else ""


def _looks_like_text(uploaded, limit=64 * 1024):
    """Plain UTF-8 with no NUL bytes and no markup opener. Text is still only ever served as a
    DOWNLOAD (support_attachment), this just refuses a binary or an HTML file wearing .txt."""
    try:
        uploaded.seek(0)
        chunk = uploaded.read(limit)
        uploaded.seek(0)
        if b"\x00" in chunk:
            return False
        text = chunk.decode("utf-8")
    except Exception:
        return False
    lowered = text.lstrip().lower()
    return not (lowered.startswith("<") or "<script" in lowered or "<html" in lowered)


def sniff_upload(uploaded, allowed_kinds):
    """(kind, ext, mime) read from the file's BYTES, or (None, None, None) when it is none of
    `allowed_kinds`. Kinds: "image", "video", "pdf", "office", "text", "font".

    Images are recognised here only by signature; the caller still runs them through
    require_image_upload, which decodes and re-encodes them.
    """
    head = _head(uploaded)
    client_ext = _client_ext(uploaded)

    if "image" in allowed_kinds:
        if head.startswith(b"\xff\xd8\xff"):
            return "image", ".jpg", "image/jpeg"
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image", ".png", "image/png"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image", ".webp", "image/webp"
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return "image", ".gif", "image/gif"
        if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif"):
            return "image", ".heic", "image/heic"

    if "video" in allowed_kinds:
        if head[4:8] == b"ftyp":
            brand = head[8:12]
            if brand == b"qt  ":
                return "video", ".mov", "video/quicktime"
            if brand.startswith(b"M4V"):
                return "video", ".m4v", "video/x-m4v"
            return "video", ".mp4", "video/mp4"
        if head.startswith(b"\x1a\x45\xdf\xa3"):
            return ("video", ".mkv", "video/x-matroska") if client_ext == ".mkv" else ("video", ".webm", "video/webm")
        if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
            return "video", ".avi", "video/x-msvideo"

    if "pdf" in allowed_kinds and head.startswith(b"%PDF-"):
        return "pdf", ".pdf", "application/pdf"

    if "office" in allowed_kinds:
        if head.startswith(b"PK\x03\x04"):
            ext = client_ext if client_ext in _ZIP_DOCS else ".zip"
            return "office", ext, _ZIP_DOCS[ext]
        if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            ext = client_ext if client_ext in _OLE_DOCS else ".doc"
            return "office", ext, _OLE_DOCS[ext]
        if head.startswith(b"{\\rtf"):
            return "office", ".rtf", "application/rtf"

    if "font" in allowed_kinds:
        if head[:4] in (b"\x00\x01\x00\x00", b"true"):
            return "font", ".ttf", "font/ttf"
        if head[:4] == b"OTTO":
            return "font", ".otf", "font/otf"

    if "text" in allowed_kinds and _looks_like_text(uploaded):
        ext = client_ext if client_ext in _TEXT else ".txt"
        return "text", ext, _TEXT[ext]

    return None, None, None


def opaque_name(ext):
    """A storage name nobody can guess and whose extension came from the sniff: '<32 hex><ext>'."""
    return f"{uuid.uuid4().hex}{ext or ''}"
