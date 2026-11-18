# ── Broadcast Kit art: the pictures the kit draws for the game client (AFC inbox #618, 18 Nov 2026) ────
# Pure image work, no database. broadcast_kit.build_broadcast_kit calls these and writes the results into
# the zip; tests_broadcast_kit.py measures them.
#
#   compose_flag       FlagLogos/<letter id>.png: the team's logo on the organizer's EMPTY banner, at the
#                      size and height they set on BroadcastKitCard (the in-map flag on a pole).
#   compose_billboard  TeamBillboard/<slot>.png: the team's logo and name on the organizer's billboard art,
#                      left, centre or right (the map billboards show the killing team).
#   fit_sky            EnvWeather/SkyboxPicture.png: the sky title fitted to the effect's own banner shape.
#   plain_png          HUD_PIC and PCTexture uploads: the organizer's own pixels, as PNG, never resized
#                      (the game stretches a panel picture into its panel, so the shape is theirs to draw).
#
# ART RULE (owner, 21 Aug 2026): nothing is drawn above about 1.2x its own size. A small logo stays small
# on a big banner rather than going soft; a small billboard art keeps its own size instead of being blown
# up to 1920 x 1080.
#
# The team name on a billboard is set in DM Sans ExtraBold (AFC's typeface, SIL Open Font License, from
# Google Fonts), shipped beside this file in fonts/ so the server needs nothing installed.

import io
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

UPSCALE_MAX = 1.2
FLAG_MAX_SIDE = 2560                 # a banner bigger than this is shrunk first (the semis used 512 x 1280)
BILLBOARD_SIZE = (1920, 1080)        # what the game's billboards show (client folder guide, section A5)
BILLBOARD_PLAIN = (22, 20, 27, 255)  # background when the organizer adds no art
PLACES = ("left", "centre", "right")

_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DMSans-ExtraBold.ttf")


def open_image(raw):
    """Uploaded bytes -> an RGBA image, or None when the bytes are not a picture."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGBA")
    except Exception:
        return None


def read_field_image(image_field):
    """An ImageField's file (a team logo) -> RGBA image, or None when missing or broken."""
    try:
        image_field.open("rb")
        raw = image_field.read()
        image_field.close()
    except Exception:
        return None
    return open_image(raw)


def png_bytes(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def plain_png(raw):
    """Uploaded picture -> PNG bytes at its own size (HUD panels, map skins). None if not a picture."""
    img = open_image(raw)
    return png_bytes(img) if img is not None else None


def _scaled(img, box_w, box_h):
    """`img` fitted inside box_w x box_h keeping its shape, never more than UPSCALE_MAX its own size."""
    scale = min(box_w / img.width, box_h / img.height, UPSCALE_MAX)
    w, h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    return img if (w, h) == img.size else img.resize((w, h), Image.LANCZOS)


# ── FlagLogos ──────────────────────────────────────────────────────────────────────────────────
def compose_flag(banner, logo, size_pct=62, height_pct=34):
    """PNG bytes: `logo` centred across `banner`, its box `size_pct` percent of the banner's width, its
    middle `height_pct` percent down the banner. The banner keeps its own size (shrunk only past
    FLAG_MAX_SIDE). Both are RGBA images."""
    canvas = banner.copy()
    if max(canvas.size) > FLAG_MAX_SIDE:
        canvas.thumbnail((FLAG_MAX_SIDE, FLAG_MAX_SIDE), Image.LANCZOS)
    w, h = canvas.size
    box = max(1, round(w * size_pct / 100))
    mark = _scaled(logo, box, box)
    x = (w - mark.width) // 2
    y = round(h * height_pct / 100 - mark.height / 2)
    y = min(max(0, y), max(0, h - mark.height))
    canvas.alpha_composite(mark, (x, y))
    return png_bytes(canvas)


# ── TeamBillboard ──────────────────────────────────────────────────────────────────────────────
def _billboard_canvas(art):
    """The billboard background: the art cut to 16:9 (centre), at its own size up to 1920 x 1080; or a
    plain dark 1920 x 1080 when there is no art."""
    if art is None:
        return Image.new("RGBA", BILLBOARD_SIZE, BILLBOARD_PLAIN)
    target = 16 / 9
    if art.width / art.height > target:
        cw, ch = round(art.height * target), art.height
    else:
        cw, ch = art.width, round(art.width / target)
    left, top = (art.width - cw) // 2, (art.height - ch) // 2
    crop = art.crop((left, top, left + cw, top + ch))
    if cw > BILLBOARD_SIZE[0]:
        crop = crop.resize(BILLBOARD_SIZE, Image.LANCZOS)
    return crop.copy()


def _font(size):
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except Exception:
        return ImageFont.load_default(size=size)


def _wrap(draw, text, font, max_w):
    """Words of `text` on lines no wider than max_w (a single word wider than max_w stays whole)."""
    lines, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if not line or draw.textlength(trial, font=font) <= max_w:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _fit_name(draw, name, max_w, start_size, max_lines=3):
    """(font, lines): the largest size from start_size down at which `name` fits in max_lines lines."""
    size = start_size
    while True:
        font = _font(size)
        lines = _wrap(draw, name, font, max_w)
        widest = max((draw.textlength(ln, font=font) for ln in lines), default=0)
        if (len(lines) <= max_lines and widest <= max_w) or size <= 12:
            return font, lines
        size = int(size * 0.9)


def compose_billboard(art, logo, name, place="right"):
    """PNG bytes: the team's logo and NAME (upper case) on the billboard art. `place` is left, centre or
    right: left and right put logo and name side by side in that 58 percent of the picture (the rest is
    the art's own), centre stacks them in the middle. Proportions match the BroadcastKitCard preview."""
    place = place if place in PLACES else "right"
    canvas = _billboard_canvas(art)
    w, h = canvas.size
    name = " ".join((name or "").upper().split())
    measure = ImageDraw.Draw(canvas)

    logo_side = round(w * 0.22)
    mark = _scaled(logo, logo_side, logo_side) if logo is not None else None
    if place == "centre":
        text_w = round(w * 0.70)
        font, lines = _fit_name(measure, name, text_w, round(w * 0.062))
    else:
        text_w = round(w * 0.58) - (logo_side + round(w * 0.04) if mark else 0)
        font, lines = _fit_name(measure, name, text_w, round(w * 0.062))
    ascent, descent = font.getmetrics()
    line_h = round((ascent + descent) * 0.95)
    text_h = line_h * len(lines)

    # Where each piece goes.
    pieces = []  # (kind, x, y, payload)
    if place == "centre":
        gap = round(w * 0.025) if mark else 0
        block_h = (mark.height if mark else 0) + gap + text_h
        y = (h - block_h) // 2
        if mark:
            pieces.append(("img", (w - mark.width) // 2, y, mark))
            y += mark.height + gap
        for ln in lines:
            lw = measure.textlength(ln, font=font)
            pieces.append(("text", round((w - lw) / 2), y, ln))
            y += line_h
    else:
        lock_w = round(w * 0.58)
        left = round(w * 0.04) if place == "left" else w - round(w * 0.04) - lock_w
        x = left
        if mark:
            pieces.append(("img", x + (logo_side - mark.width) // 2, (h - mark.height) // 2, mark))
            x += logo_side + round(w * 0.04)
        y = (h - text_h) // 2
        for ln in lines:
            pieces.append(("text", x, y, ln))
            y += line_h

    # A soft dark shadow under the name keeps it readable on any art (this is a picture, not page UI).
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for kind, x, y, payload in pieces:
        if kind == "text":
            sd.text((x, y + round(w * 0.004)), payload, font=font, fill=(0, 0, 0, 160))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(1, round(w * 0.008)))))
    draw = ImageDraw.Draw(canvas)
    for kind, x, y, payload in pieces:
        if kind == "img":
            canvas.alpha_composite(payload, (x, y))
        else:
            draw.text((x, y), payload, font=font, fill=(255, 255, 255, 255))
    return png_bytes(canvas)


# ── EnvWeather sky title ───────────────────────────────────────────────────────────────────────
def fit_sky(img, size):
    """PNG bytes at exactly `size` (the effect's banner shape): `img` fitted inside it, centred, on a
    transparent background, never more than UPSCALE_MAX its own size."""
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    mark = _scaled(img, size[0], size[1])
    canvas.alpha_composite(mark, ((size[0] - mark.width) // 2, (size[1] - mark.height) // 2))
    return png_bytes(canvas)
