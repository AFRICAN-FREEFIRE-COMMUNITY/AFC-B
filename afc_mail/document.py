"""
afc_mail/document.py - the email an admin writes, from the editor's document to email HTML.

WHY A DOCUMENT AND AN ALLOWLIST (owner approved the Gmail-style composer 2026-10-16, inbox #288 #290):
the composer is a rich text editor (Tiptap, the one AFC already uses for news). It hands the server its
own document, a tree of typed nodes, never HTML. parse_doc() keeps only the node types, marks and
attributes listed here, each checked for type and range, and render() builds the email from that clean
tree. So a person's text is always escaped, a link is always http(s) or mailto, a picture is always an
AFC asset found by its token, and an unknown node simply disappears. Nothing typed is ever sent as
markup, which is the rule the first, text-only version of this app was built on.

THE NODES (frontend components/mail/emailExtensions.ts registers the same names):
  doc, paragraph {textAlign, indent}, heading {level 1-3, textAlign, indent}, blockquote,
  bulletList {listStyle}, orderedList {start, listStyle}, listItem, hardBreak, text {marks},
  emailImage {asset, alt, width %, opacity, align, href}, emailButton {label, href, align},
  designedPicture {asset, alt, width %, opacity, align, design}  (a picture drawn in the browser from
  layers; `design` keeps the layers so it can be edited again and is never rendered here)
  marks: bold, italic, underline, strike, link {href}, textStyle {color, fontFamily, fontSize}

EMAIL HTML: inline styles only (Gmail drops <style> blocks in many places), tables for the button,
explicit pixel widths beside percentages for Outlook, solid colours (no rgba).

CONNECTS TO: afc_mail.content (wraps this in the shell, adds footer and pixel, tracks links),
afc_mail.style (colours, fonts), afc_mail.assets (picture addresses, baked transparency),
afc_mail.views (parse_doc on every save).
"""
import html
import json
import re
from urllib.parse import urlsplit

from .style import FONTS, clean_colour, font_family_name, font_stack, mix

MAX_NODES = 4000
MAX_DEPTH = 24
MAX_TEXT = 20000
MAX_DESIGN_BYTES = 60000
CONTENT_WIDTH = 552  # 600 px email less 24 px a side: the pixel width Outlook needs beside the %

ALIGNS = {"left", "center", "right"}
OL_STYLES = {"decimal", "lower-alpha", "upper-alpha", "lower-roman", "upper-roman"}
UL_STYLES = {"disc", "circle", "square"}
HEAD_SIZES = {1: 28, 2: 22, 3: 18}
ASSET_TOKEN = re.compile(r"^a_[0-9a-f]{32}$")
BLOCKS = {"paragraph", "heading", "blockquote", "bulletList", "orderedList", "emailImage", "emailButton", "designedPicture"}
SIMPLE_MARKS = ("bold", "italic", "underline", "strike")


def attr(value):
    """Escape for a double-quoted attribute. Single quotes are left alone: the font stacks use them,
    and every value placed in a style attribute was checked (colours, numbers, AFC's own font names)."""
    return html.escape(value, quote=False).replace('"', "&quot;")


class DocError(ValueError):
    """A document that cannot be stored (too big, too deep, not a document). Carries a code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ── checks for single values ─────────────────────────────────────────────────────────────────────

def clean_href(value):
    """An http(s) or mailto address, or None. No spaces, no control characters, a host for web links."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > 2000 or any(c.isspace() or ord(c) < 32 for c in v):
        return None
    low = v.lower()
    if low.startswith(("https://", "http://")):
        try:
            if not urlsplit(v).netloc:
                return None
        except ValueError:
            return None
        return v
    if low.startswith("mailto:") and "@" in v and len(v) > 8:
        return v
    return None


def _int(value, lo, hi, default):
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        m = re.match(r"^\s*(\d{1,4})(?:\.\d+)?\s*(px|%)?\s*$", value)
        value = int(m.group(1)) if m else None
    if isinstance(value, float):
        value = round(value)
    if isinstance(value, int) and lo <= value <= hi:
        return value
    return default


def _float(value, lo, hi, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(lo, min(hi, float(value)))


def _text(value, cap):
    return value[:cap] if isinstance(value, str) else ""


# ── parse: keep only what is listed ──────────────────────────────────────────────────────────────

class _Counter:
    def __init__(self):
        self.nodes = 0
        self.assets = []

    def tick(self):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise DocError("mail_doc_too_big", "The email is too long.")


def _marks(raw):
    out = []
    seen = set()
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict) or m.get("type") in seen:
            continue
        kind = m.get("type")
        attrs = m.get("attrs") if isinstance(m.get("attrs"), dict) else {}
        if kind in SIMPLE_MARKS:
            out.append({"type": kind})
        elif kind == "link":
            href = clean_href(attrs.get("href"))
            if not href:
                continue
            out.append({"type": "link", "attrs": {"href": href}})
        elif kind == "textStyle":
            clean = {}
            colour = clean_colour(attrs.get("color"))
            if colour:
                clean["color"] = colour
            family = font_family_name(attrs.get("fontFamily"))
            if family:
                clean["fontFamily"] = family  # checked against the style's fonts at render time
            size = _int(attrs.get("fontSize"), 8, 72, None)
            if size:
                clean["fontSize"] = size
            if not clean:
                continue
            out.append({"type": "textStyle", "attrs": clean})
        else:
            continue
        seen.add(kind)
    return out


def _inline_content(raw, counter):
    out = []
    for n in raw if isinstance(raw, list) else []:
        if not isinstance(n, dict):
            continue
        counter.tick()
        if n.get("type") == "text":
            text = _text(n.get("text"), MAX_TEXT)
            if text:
                node = {"type": "text", "text": text}
                marks = _marks(n.get("marks"))
                if marks:
                    node["marks"] = marks
                out.append(node)
        elif n.get("type") == "hardBreak":
            out.append({"type": "hardBreak"})
    return out


def _block_attrs(attrs, *, heading=False):
    clean = {}
    align = attrs.get("textAlign")
    if align in ALIGNS:
        clean["textAlign"] = align
    indent = _int(attrs.get("indent"), 0, 6, 0)
    if indent:
        clean["indent"] = indent
    if heading:
        clean["level"] = _int(attrs.get("level"), 1, 3, 2)
    return clean


def _picture_attrs(attrs, counter):
    token = attrs.get("asset")
    if not (isinstance(token, str) and ASSET_TOKEN.match(token)):
        return None
    counter.assets.append(token)
    clean = {
        "asset": token,
        "alt": _text(attrs.get("alt"), 200),
        "width": _int(attrs.get("width"), 10, 100, 100),
        "opacity": round(_float(attrs.get("opacity"), 0.1, 1.0, 1.0), 2),
        "align": attrs.get("align") if attrs.get("align") in ALIGNS else "center",
    }
    href = clean_href(attrs.get("href"))
    if href:
        clean["href"] = href
    return clean


def _design(raw, counter):
    """The layers of a designed picture, kept for editing it again. Never rendered by the server, but
    still reduced to known keys and ranges, because the composer draws it back from this."""
    if not isinstance(raw, dict):
        return None
    out = {"h": _int(raw.get("h"), 120, 600, 260), "bg": clean_colour(raw.get("bg")) or "#13201a", "layers": []}
    for layer in (raw.get("layers") if isinstance(raw.get("layers"), list) else [])[:30]:
        if not isinstance(layer, dict) or layer.get("kind") not in ("pic", "text"):
            continue
        clean = {
            "id": _text(layer.get("id"), 20) or "l",
            "kind": layer["kind"],
            "x": _int(layer.get("x"), -600, 1200, 0),
            "y": _int(layer.get("y"), -600, 1200, 0),
            "opacity": round(_float(layer.get("opacity"), 0.1, 1.0, 1.0), 2),
            "z": _int(layer.get("z"), -999, 999, 1),
        }
        if layer["kind"] == "pic":
            token = layer.get("asset")
            if not (isinstance(token, str) and ASSET_TOKEN.match(token)):
                continue
            counter.assets.append(token)
            clean.update({"asset": token, "w": _int(layer.get("w"), 20, 1200, 240)})
        else:
            clean.update({
                "text": _text(layer.get("text"), 200),
                "size": _int(layer.get("size"), 8, 200, 36),
                "color": clean_colour(layer.get("color")) or "#ffffff",
                "bold": bool(layer.get("bold")),
                "font": font_family_name(layer.get("font")) or "DM Sans",
            })
        out["layers"].append(clean)
    if len(json.dumps(out)) > MAX_DESIGN_BYTES:
        raise DocError("mail_doc_too_big", "The designed picture has too many layers.")
    return out


def _blocks(raw, counter, depth, *, in_list=False):
    if depth > MAX_DEPTH:
        raise DocError("mail_doc_too_deep", "The email is nested too deeply.")
    out = []
    for n in raw if isinstance(raw, list) else []:
        if not isinstance(n, dict):
            continue
        kind = n.get("type")
        attrs = n.get("attrs") if isinstance(n.get("attrs"), dict) else {}
        counter.tick()
        if kind == "paragraph":
            out.append({"type": "paragraph", "attrs": _block_attrs(attrs), "content": _inline_content(n.get("content"), counter)})
        elif kind == "heading":
            out.append({"type": "heading", "attrs": _block_attrs(attrs, heading=True), "content": _inline_content(n.get("content"), counter)})
        elif kind == "blockquote":
            out.append({"type": "blockquote", "content": _blocks(n.get("content"), counter, depth + 1)})
        elif kind in ("bulletList", "orderedList"):
            items = []
            for item in n.get("content") if isinstance(n.get("content"), list) else []:
                if isinstance(item, dict) and item.get("type") == "listItem":
                    counter.tick()
                    items.append({"type": "listItem", "content": _blocks(item.get("content"), counter, depth + 1, in_list=True)})
            clean = {}
            if kind == "orderedList":
                clean["start"] = _int(attrs.get("start"), 1, 999, 1)
                clean["listStyle"] = attrs.get("listStyle") if attrs.get("listStyle") in OL_STYLES else "decimal"
            else:
                clean["listStyle"] = attrs.get("listStyle") if attrs.get("listStyle") in UL_STYLES else "disc"
            if items:
                out.append({"type": kind, "attrs": clean, "content": items})
        elif kind == "emailImage" and not in_list:
            clean = _picture_attrs(attrs, counter)
            if clean:
                out.append({"type": "emailImage", "attrs": clean})
        elif kind == "designedPicture" and not in_list:
            clean = _picture_attrs(attrs, counter)
            if clean:
                clean.pop("href", None)
                design = _design(attrs.get("design"), counter)
                if design:
                    clean["design"] = design
                out.append({"type": "designedPicture", "attrs": clean})
        elif kind == "emailButton" and not in_list:
            href = clean_href(attrs.get("href"))
            label = _text(attrs.get("label"), 80).strip()
            if href and label:
                out.append({"type": "emailButton", "attrs": {"label": label, "href": href, "align": attrs.get("align") if attrs.get("align") in ALIGNS else "center"}})
    return out


def parse_doc(raw):
    """(clean doc, asset tokens it names) from what the editor sent; None stays None. Raises DocError
    for a document too big or too deep to keep."""
    if raw is None:
        return None, []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise DocError("mail_doc_invalid", "The email could not be read.")
    if not isinstance(raw, dict) or raw.get("type") != "doc":
        raise DocError("mail_doc_invalid", "The email could not be read.")
    counter = _Counter()
    doc = {"type": "doc", "content": _blocks(raw.get("content"), counter, 0)}
    return doc, list(dict.fromkeys(counter.assets))


def doc_tokens(doc):
    """Every asset token a stored (already clean) document names."""
    _clean, tokens = parse_doc(doc) if doc else (None, [])
    return tokens


def doc_is_empty(doc):
    """True when there is nothing to send: no words, no picture, no button."""
    if not doc:
        return True

    def walk(nodes):
        for n in nodes or []:
            if n.get("type") in ("emailImage", "designedPicture", "emailButton"):
                return False
            if n.get("type") == "text" and n.get("text", "").strip():
                return False
            if not walk(n.get("content")):
                return False
        return True

    return walk(doc.get("content"))


# ── render: the clean tree to email HTML and to plain text ───────────────────────────────────────

class Renderer:
    """Renders one clean document with one style.

    asset_url(token, opacity) gives a picture's absolute address (afc_mail.assets.public_url, which
    bakes transparency); link_for(url) gives the address a link should point to (the tracker's, when
    sending) and the renderer records every web link it meets, in order, in `links`."""

    def __init__(self, style, asset_url, link_for=None):
        self.style = style
        self.asset_url = asset_url
        self.link_for = link_for or (lambda url: url)
        self.links = []
        self.body_stack = font_stack(style["bodyFont"], style)
        self.head_stack = font_stack(style["headFont"], style)

    # links
    def _href(self, url):
        if url.lower().startswith(("https://", "http://")):
            if url not in self.links:
                self.links.append(url)
            return self.link_for(url)
        return url

    def _font(self, name):
        known = set(FONTS) | {u["family"] for u in self.style.get("fonts", [])}
        return font_stack(name, self.style) if name in known else None

    # inline
    def _inline(self, nodes):
        out = []
        for n in nodes or []:
            if n["type"] == "hardBreak":
                out.append("<br>")
                continue
            text = html.escape(n["text"]).replace("\n", "<br>")
            marks = {m["type"]: m.get("attrs", {}) for m in n.get("marks", [])}
            css = []
            ts = marks.get("textStyle", {})
            if ts.get("color"):
                css.append(f"color:{ts['color']}")
            stack = self._font(ts.get("fontFamily")) if ts.get("fontFamily") else None
            if stack:
                css.append(f"font-family:{stack}")
            if ts.get("fontSize"):
                css.append(f"font-size:{ts['fontSize']}px")
            if css:
                text = f'<span style="{attr(";".join(css))}">{text}</span>'
            if "bold" in marks:
                text = f"<strong>{text}</strong>"
            if "italic" in marks:
                text = f"<em>{text}</em>"
            if "underline" in marks:
                text = f"<u>{text}</u>"
            if "strike" in marks:
                text = f"<s>{text}</s>"
            if "link" in marks:
                colour = ts.get("color") or self.style["link"]
                href = html.escape(self._href(marks["link"]["href"]), quote=True)
                text = f'<a href="{href}" style="color:{colour};text-decoration:underline;">{text}</a>'
            out.append(text)
        return "".join(out)

    # blocks
    def _block_css(self, attrs, margin):
        css = [f"margin:{margin}"]
        if attrs.get("textAlign"):
            css.append(f"text-align:{attrs['textAlign']}")
        if attrs.get("indent"):
            css.append(f"padding-left:{attrs['indent'] * 28}px")
        return ";".join(css)

    def _picture(self, attrs):
        url = self.asset_url(attrs["asset"], attrs.get("opacity", 1.0))
        if not url:
            return ""
        width = attrs.get("width", 100)
        img = (f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(attrs.get("alt", ""), quote=True)}" '
               f'width="{round(CONTENT_WIDTH * width / 100)}" '
               f'style="display:inline-block;width:{width}%;max-width:100%;height:auto;border:0;outline:none;border-radius:6px;">')
        if attrs.get("href"):
            img = f'<a href="{html.escape(self._href(attrs["href"]), quote=True)}" style="text-decoration:none;">{img}</a>'
        return f'<p style="margin:0 0 14px;text-align:{attrs.get("align", "center")};">{img}</p>'

    def _button(self, attrs):
        s = self.style
        href = html.escape(self._href(attrs["href"]), quote=True)
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:2px 0 14px;">'
                f'<tr><td align="{attrs["align"]}">'
                f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td bgcolor="{s["btn"]}" style="border-radius:8px;background-color:{s["btn"]};">'
                f'<a href="{href}" style="display:inline-block;padding:12px 22px;font-family:{attr(self.body_stack)};'
                f'font-size:15px;font-weight:700;line-height:1.2;color:{s["btnText"]};text-decoration:none;border-radius:8px;">'
                f'{html.escape(attrs["label"])}</a></td></tr></table></td></tr></table>')

    def _blocks(self, nodes, *, in_list=False, quoted=False):
        s = self.style
        out = []
        for n in nodes or []:
            kind = n["type"]
            attrs = n.get("attrs", {})
            if kind == "paragraph":
                inner = self._inline(n.get("content")) or "&nbsp;"
                margin = "0" if in_list else ("0 0 8px" if quoted else "0 0 12px")
                out.append(f'<p style="{self._block_css(attrs, margin)};line-height:1.6;">{inner}</p>')
            elif kind == "heading":
                level = attrs.get("level", 2)
                css = (f'{self._block_css(attrs, "0 0 12px")};font-family:{self.head_stack};font-size:{HEAD_SIZES[level]}px;'
                       f'line-height:1.25;font-weight:700;color:{s["head"]}')
                out.append(f'<h{level} style="{attr(css)}">{self._inline(n.get("content")) or "&nbsp;"}</h{level}>')
            elif kind == "blockquote":
                bg = mix(s["mail"], s["text"], 0.08)
                out.append(f'<blockquote style="margin:0 0 12px;padding:10px 14px 2px;background-color:{bg};border-radius:6px;">'
                           f'{self._blocks(n.get("content"), quoted=True)}</blockquote>')
            elif kind in ("bulletList", "orderedList"):
                tag = "ol" if kind == "orderedList" else "ul"
                start = f' start="{attrs.get("start", 1)}"' if tag == "ol" else ""
                items = "".join(f'<li style="margin:0 0 6px;">{self._blocks(item.get("content"), in_list=True)}</li>' for item in n.get("content", []))
                margin = "4px 0 6px" if in_list else "0 0 14px"
                out.append(f'<{tag}{start} style="margin:{margin};padding-left:26px;list-style-type:{attrs.get("listStyle", "disc")};">{items}</{tag}>')
            elif kind in ("emailImage", "designedPicture"):
                out.append(self._picture(attrs))
            elif kind == "emailButton":
                out.append(self._button(attrs))
        return "".join(out)

    def html(self, doc):
        return self._blocks((doc or {}).get("content"))

    # plain text, for the text/plain part every email carries
    def text(self, doc):
        lines = []

        def inline_text(nodes):
            parts = []
            for n in nodes or []:
                if n["type"] == "hardBreak":
                    parts.append("\n")
                    continue
                t = n["text"]
                link = next((m["attrs"]["href"] for m in n.get("marks", []) if m["type"] == "link"), None)
                parts.append(f"{t} ({self._href(link)})" if link else t)
            return "".join(parts)

        def walk(nodes, prefix=""):
            for n in nodes or []:
                kind = n["type"]
                attrs = n.get("attrs", {})
                if kind in ("paragraph", "heading"):
                    t = inline_text(n.get("content")).strip()
                    if t:
                        lines.append(prefix + (t.upper() if kind == "heading" and attrs.get("level") == 1 else t))
                        if not prefix:
                            lines.append("")
                elif kind == "blockquote":
                    walk(n.get("content"), prefix + "> ")
                elif kind in ("bulletList", "orderedList"):
                    for i, item in enumerate(n.get("content", [])):
                        mark = f"{attrs.get('start', 1) + i}." if kind == "orderedList" else "-"
                        before = len(lines)
                        walk(item.get("content"), prefix + "   ")
                        if len(lines) > before:
                            lines[before] = prefix + mark + " " + lines[before].strip()
                    lines.append("")
                elif kind in ("emailImage", "designedPicture"):
                    if attrs.get("alt"):
                        lines.append(f"[{attrs['alt']}]")
                        lines.append("")
                elif kind == "emailButton":
                    lines.append(f"{attrs['label']}: {self._href(attrs['href'])}")
                    lines.append("")

        walk((doc or {}).get("content"))
        text = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
