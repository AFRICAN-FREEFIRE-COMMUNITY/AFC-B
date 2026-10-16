"""
afc_mail/designs.py - "use your own design": a finished email made elsewhere, uploaded per language
(inbox #281, owner 2026-10-16: "add that people can upload email templates or design like in html
formats or other formats").

ACCEPTED: an HTML file, a ZIP holding the HTML and its pictures, or MJML (compiled with mrml, the Rust
MJML compiler, with includes switched OFF so a design can never make the server read a file or fetch a
web address). 2 MB uploaded at most; a ZIP may unpack to 12 MB in 200 files at most.

WHAT IS DONE TO IT before it is stored (clean_design; the composer lists the same things back):
  - taken out: script, iframe, frame, object, embed, form and its fields, base, link, meta refresh,
    svg, video and audio, every on... attribute, every javascript:/vbscript:/data: address (except a
    picture packed inside the file), srcset, and in CSS @import, expression(), behavior and
    -moz-binding. Comments go too, except Outlook's own [if mso] sections, which only Outlook reads.
  - pictures packed inside the HTML (data:image) or the ZIP become AFC assets (afc_mail.assets: checked,
    re-encoded, served from AFC), because most email apps do not show pictures packed into an email.
    Pictures on web addresses are left as they are: the reader's mail app loads them, the server never
    fetches an address a person typed.
  - links are counted (each becomes a tracked link at send time, afc_mail.content), the size is
    measured against the 102 KB at which Gmail cuts an email, and AFC's unsubscribe footer is added
    when it is sent, whatever the design says.

CONNECTS TO: afc_mail.views.campaign_design (the upload endpoint), afc_mail.content (sending it),
afc_mail.assets (packed pictures).
"""
import base64
import binascii
import io
import posixpath
import re
import zipfile

from bs4 import BeautifulSoup, Comment

from . import assets

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ZIP_TOTAL = 12 * 1024 * 1024
MAX_ZIP_FILES = 200
MAX_ZIP_RATIO = 100
GMAIL_CLIP_KB = 102

DROP_TAGS = ("script", "iframe", "frame", "frameset", "object", "embed", "applet", "form", "input", "button",
             "select", "textarea", "option", "base", "link", "noscript", "template", "svg", "math", "audio",
             "video", "source", "track", "portal", "dialog", "canvas", "param", "slot")
BAD_SCHEME = re.compile(r"^\s*(javascript|vbscript|data|file|blob)\s*:", re.I)
CSS_BAD = re.compile(r"@import[^;]*;?|expression\s*\(|behavior\s*:|-moz-binding\s*:|javascript\s*:", re.I)
DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,(.+)$", re.I | re.S)


class DesignError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _clean_css(text):
    return CSS_BAD.sub("", text or "")


def _kind(name, data):
    low = (name or "").lower()
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if low.endswith(".mjml") or b"<mjml" in data[:2000].lower():
        return "mjml"
    if low.endswith((".html", ".htm")) or b"<html" in data[:4000].lower() or b"<!doctype" in data[:200].lower():
        return "html"
    return None


def _from_zip(data):
    """(html bytes, {member path: bytes} of pictures, base dir of the html)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DesignError("mail_design_unreadable", "That ZIP file could not be opened.")
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_FILES:
        raise DesignError("mail_design_too_many_files", "That ZIP holds too many files.")
    total = sum(i.file_size for i in infos)
    if total > MAX_ZIP_TOTAL or any(i.compress_size and i.file_size / i.compress_size > MAX_ZIP_RATIO for i in infos):
        raise DesignError("mail_design_too_big", "That ZIP unpacks to more than AFC accepts.")
    pages = sorted((i for i in infos if i.filename.lower().endswith((".html", ".htm"))),
                   key=lambda i: (i.filename.count("/"), not posixpath.basename(i.filename).lower().startswith("index"), i.filename))
    if not pages:
        raise DesignError("mail_design_no_html", "There is no HTML file in that ZIP.")
    page = pages[0]
    pictures = {i.filename: zf.read(i) for i in infos if i.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))}
    return zf.read(page), pictures, posixpath.dirname(page.filename)


def _from_mjml(data):
    try:
        import mrml
    except ImportError:
        raise DesignError("mail_design_mjml_unavailable", "MJML cannot be turned into HTML on this server yet.")
    # mj-include would read other files; the loader is a no-op, so say why rather than a vague failure.
    if b"<mj-include" in data.lower():
        raise DesignError("mail_design_mjml_include", "MJML that includes other files cannot be used. Put it all in one file.")
    try:
        options = mrml.ParserOptions(include_loader=mrml.noop_loader())
        # fonts={}: no Google Fonts <link> of its own (mrml adds Ubuntu by default); the cleaner would
        # strip it and report it as if the admin's file had carried it (walk of 17 Oct 2026).
        out = mrml.to_html(data.decode("utf-8", errors="replace"), parser_options=options,
                           render_options=mrml.RenderOptions(fonts={}))
    except Exception:
        raise DesignError("mail_design_mjml_invalid", "That MJML could not be turned into HTML.")
    return out.content.encode("utf-8")


def clean_design(name, data, user):
    """{"name", "html", "size", "report"} from an uploaded file, or DesignError."""
    if not data:
        raise DesignError("mail_design_empty", "That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DesignError("mail_design_too_big", "That file is bigger than 2 MB.")
    kind = _kind(name, data)
    pictures, base = {}, ""
    if kind == "zip":
        data, pictures, base = _from_zip(data)
    elif kind == "mjml":
        data = _from_mjml(data)
    elif kind != "html":
        raise DesignError("mail_design_wrong_type", "That file is not HTML, ZIP or MJML.")

    soup = BeautifulSoup(data, "lxml")
    report = {"removed": 0, "handlers": 0, "packed": 0, "web_pictures": 0, "dropped_pictures": 0, "links": 0}

    for tag in soup.find_all(list(DROP_TAGS)):
        report["removed"] += 1
        tag.decompose()
    for meta in soup.find_all("meta"):
        if meta.get("http-equiv"):
            meta.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        if not (c.strip().startswith("[if") or c.strip().endswith("endif]")):
            c.extract()
    for style in soup.find_all("style"):
        style.string = _clean_css(style.get_text())

    for el in soup.find_all(True):
        for attr in list(el.attrs):
            value = el.attrs[attr]
            text = " ".join(value) if isinstance(value, list) else str(value)
            low = attr.lower()
            if low.startswith("on"):
                del el.attrs[attr]
                report["handlers"] += 1
            elif low in ("srcset", "formaction", "xlink:href", "ping"):
                del el.attrs[attr]
            elif low == "style":
                el.attrs[attr] = _clean_css(text)
            elif low in ("href", "src", "background", "action", "poster", "data") and BAD_SCHEME.match(text):
                if el.name == "img" and low == "src" and DATA_IMAGE.match(text.strip()):
                    continue  # moved to an asset below
                del el.attrs[attr]
                report["handlers"] += 1

    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        m = DATA_IMAGE.match(src)
        blob = None
        if m:
            try:
                blob = base64.b64decode(m.group(2), validate=False)
            except (binascii.Error, ValueError):
                blob = None
        elif src and not re.match(r"^https?://", src, re.I):
            blob = pictures.get(posixpath.normpath(posixpath.join(base, src.split("?")[0].split("#")[0])).lstrip("/"))
        elif src:
            report["web_pictures"] += 1
            continue
        if blob:
            asset, _code = assets.store_image_bytes(blob, user, name="design-picture")
            if asset:
                img["src"] = assets.public_url(asset)
                report["packed"] += 1
                continue
        # A picture that could not be read: its description stays, the broken picture goes.
        alt = img.get("alt") or ""
        report["dropped_pictures"] += 1
        if alt:
            img.replace_with(alt)
        else:
            img.decompose()

    for a in soup.find_all("a", href=True):
        if re.match(r"^https?://", a["href"].strip(), re.I):
            report["links"] += 1

    html_out = str(soup)
    if not html_out.lstrip().lower().startswith("<!doctype"):
        html_out = "<!DOCTYPE html>\n" + html_out
    size = len(html_out.encode("utf-8"))
    report["size_kb"] = max(1, round(size / 1024))
    report["gmail_clip_kb"] = GMAIL_CLIP_KB
    return {"name": (name or "design.html")[:120], "html": html_out, "size": size, "report": report}
