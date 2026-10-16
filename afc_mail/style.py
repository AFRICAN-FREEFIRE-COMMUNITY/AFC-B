"""
afc_mail/style.py - one look for a whole email: colours, fonts, text size (inbox #287, 2026-10-16:
"even fonts, font uploads and color designs should be made aviailable").

The composer's "Colours and fonts" panel sends a style object; parse_style keeps only what it knows,
in the shapes it expects, so a stored style can only ever produce a colour, a font name AFC chose or a
font the admin uploaded (a MailAsset), and a size in range. Everything the email renderer reads about
the look comes from here.

WHAT EMAIL APPS DO WITH IT (the composer says the same on screen): colours and sizes show everywhere;
a font from FONTS shows where the reader's device has it, otherwise the next one in its stack; an
uploaded font shows in Apple Mail and iPhone Mail, and other apps show the fallback picked for it.

CONNECTS TO: afc_mail.document (renders with it), afc_mail.content (the shell), afc_mail.views
(campaign and template saves), AFC_Frontend components/mail/ (the same font list and themes).
"""
import re

# The fonts an admin can pick, with the stack an email uses for each. The frontend offers the same
# names (components/mail/emailFonts.ts); a name not in this list is dropped.
FONTS = {
    "DM Sans": "'DM Sans', Arial, Helvetica, sans-serif",
    "Arial": "Arial, Helvetica, sans-serif",
    "Verdana": "Verdana, Geneva, sans-serif",
    "Tahoma": "Tahoma, Verdana, sans-serif",
    "Trebuchet MS": "'Trebuchet MS', Arial, sans-serif",
    "Georgia": "Georgia, 'Times New Roman', serif",
    "Times New Roman": "'Times New Roman', Times, serif",
    "Courier New": "'Courier New', Courier, monospace",
}

COLOUR_KEYS = ("page", "mail", "text", "head", "link", "btn", "btnText")

THEMES = {
    "AFC dark": {"page": "#0d1410", "mail": "#13201a", "text": "#cdd6cf", "head": "#ffffff", "link": "#4ade80", "btn": "#16a34a", "btnText": "#ffffff"},
    "AFC light": {"page": "#e9eeea", "mail": "#fbfcfb", "text": "#26332c", "head": "#14532d", "link": "#15803d", "btn": "#16a34a", "btnText": "#ffffff"},
    "Gold night": {"page": "#14110a", "mail": "#1e1a10", "text": "#e8dfc8", "head": "#eab308", "link": "#eab308", "btn": "#eab308", "btnText": "#14110a"},
}

DEFAULT_STYLE = {"theme": "AFC dark", **THEMES["AFC dark"], "bodyFont": "DM Sans", "headFont": "DM Sans", "size": 15, "fonts": []}

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
SHORT_HEX = re.compile(r"^#[0-9a-fA-F]{3}$")
RGB = re.compile(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+\s*)?\)$")
FONT_TOKEN = re.compile(r"^a_[0-9a-f]{32}$")
MAX_UPLOADED_FONTS = 5


def clean_colour(value):
    """'#rrggbb' from '#rrggbb', '#rgb' or 'rgb(r, g, b)'; None for anything else."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if HEX.match(v):
        return v.lower()
    if SHORT_HEX.match(v):
        return "#" + "".join(c * 2 for c in v[1:]).lower()
    m = RGB.match(v)
    if m and all(0 <= int(x) <= 255 for x in m.groups()):
        return "#" + "".join(f"{int(x):02x}" for x in m.groups())
    return None


def mix(colour, other, share):
    """`colour` moved `share` (0..1) of the way to `other`. Email apps do not all understand rgba, so
    a soft colour (the quote background, the footer text) is mixed here into a solid one."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * share):02x}" for x, y in zip(a, b))


def font_family_name(css_value):
    """The first family named in a CSS font-family value: '"Squad Display", Arial' -> 'Squad Display'."""
    if not isinstance(css_value, str):
        return ""
    first = css_value.split(",")[0].strip().strip("'\"").strip()
    return first[:60]


def parse_style(raw, *, font_assets=None):
    """A clean style from whatever the composer sent. `font_assets` maps an uploaded font's token to
    its MailAsset (kind "font"); an uploaded font the server does not hold is dropped."""
    font_assets = font_assets or {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_STYLE)
    theme = raw.get("theme")
    if isinstance(theme, str) and (theme in THEMES or theme == "Custom"):
        out["theme"] = theme
        out.update(THEMES.get(theme, {}))  # a colour that does not parse falls back to its theme's
    for key in COLOUR_KEYS:
        colour = clean_colour(raw.get(key))
        if colour:
            out[key] = colour
    uploads = []
    for item in (raw.get("fonts") if isinstance(raw.get("fonts"), list) else [])[:MAX_UPLOADED_FONTS]:
        if not isinstance(item, dict):
            continue
        token = item.get("asset")
        if not (isinstance(token, str) and FONT_TOKEN.match(token)) or token not in font_assets:
            continue
        fallback = item.get("fallback") if item.get("fallback") in FONTS else "Arial"
        uploads.append({"asset": token, "family": font_assets[token].family, "fallback": fallback})
    out["fonts"] = uploads
    names = set(FONTS) | {u["family"] for u in uploads}
    for key in ("bodyFont", "headFont"):
        if raw.get(key) in names:
            out[key] = raw[key]
    size = raw.get("size")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and 13 <= size <= 20:
        out["size"] = int(size)
    return out


def font_tokens(raw):
    """The uploaded-font tokens a raw style mentions (so the caller can load just those assets)."""
    fonts = raw.get("fonts") if isinstance(raw, dict) else None
    return [f.get("asset") for f in (fonts if isinstance(fonts, list) else []) if isinstance(f, dict) and isinstance(f.get("asset"), str)]


def font_stack(name, style):
    """The CSS stack for a font name in this style: a listed font, an uploaded one with its fallback,
    or DM Sans's stack for anything else."""
    if name in FONTS:
        return FONTS[name]
    for u in style.get("fonts", []):
        if u["family"] == name:
            return f"'{name}', {FONTS.get(u['fallback'], FONTS['Arial'])}"
    return FONTS["DM Sans"]
