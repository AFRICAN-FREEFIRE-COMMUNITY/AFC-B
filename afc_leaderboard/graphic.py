"""
afc_leaderboard.graphic — render a standalone leaderboard's standings onto a branded design.

OWNER 2026-06-13: organizers upload branded background designs (a per-org library,
afc_organizers.OrgLeaderboardDesign) and, when exporting a leaderboard, pick which design +
which size to download. This module composites the LIVE standings (rank / name / points /
kills) plus the tournament title, an optional stage-or-group subtitle, and the org logo onto
the chosen background, at Instagram (1080x1350) or YouTube (1920x1080) size, with Pillow.

Pure rendering: standings in (from standings.standalone_standings), PNG bytes out. No ORM
writes. Called by afc_leaderboard.views.leaderboard_graphic (the download endpoint).
"""
import io

from PIL import Image, ImageDraw, ImageFont

# Output canvases. IG = portrait feed post; YT = 16:9 thumbnail / stream card.
CANVAS = {
    "instagram": (1080, 1350),
    "youtube": (1920, 1080),
}
DEFAULT_BG = (10, 14, 12)        # dark AFC base when no background is uploaded for a size
DEFAULT_ACCENT = "#34d27b"
DEFAULT_TEXT = "#FFFFFF"


def _font(size):
    """A scalable font at `size`. Pillow >= 10.1 ships a scalable DejaVu Sans through
    load_default(size=...), so this works identically on the Windows dev box and the Ubuntu
    server with NO bundled font file. Falls back to a couple of common truetype paths, then to
    the (small, fixed) bitmap default as a last resort."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        pass  # very old Pillow without the size kwarg
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _hex(color, fallback):
    """Parse a #RRGGBB string into an (r,g,b) tuple; fall back on anything malformed."""
    try:
        c = (color or "").lstrip("#")
        if len(c) == 6:
            return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    return _hex(fallback, "#FFFFFF") if fallback != "#FFFFFF" else (255, 255, 255)


def _cover(img, size):
    """Resize `img` to COVER `size` (fill, center-crop the overflow) so an uploaded background
    of any aspect ratio fills the canvas without distortion."""
    tw, th = size
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _text_w(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def render_leaderboard_graphic(standings, *, size="instagram", background_path=None,
                               logo_path=None, title="", subtitle="",
                               text_color=DEFAULT_TEXT, accent_color=DEFAULT_ACCENT,
                               max_rows=16, show_title=True, show_subtitle=True):
    """Composite `standings` (the standalone_standings list) onto a branded canvas and return
    PNG bytes.

    size            : "instagram" (1080x1350) or "youtube" (1920x1080).
    background_path : a filesystem path to the org design's background for this size, or None
                      -> a plain dark AFC background.
    logo_path       : org logo path, drawn top-left; or None.
    title           : the tournament / leaderboard name (drawn when show_title).
    subtitle        : stage / group played, typed at export (drawn when show_subtitle).
    """
    canvas_size = CANVAS.get(size, CANVAS["instagram"])
    W, H = canvas_size
    text_rgb = _hex(text_color, DEFAULT_TEXT)
    accent_rgb = _hex(accent_color, DEFAULT_ACCENT)
    muted_rgb = (155, 179, 166)

    # ── base ──
    if background_path:
        try:
            bg = Image.open(background_path).convert("RGB")
            base = _cover(bg, canvas_size)
        except Exception:
            base = Image.new("RGB", canvas_size, DEFAULT_BG)
    else:
        base = Image.new("RGB", canvas_size, DEFAULT_BG)
    # A subtle dark scrim over the lower 2/3 keeps standings text legible on any background.
    scrim = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    sd.rectangle([0, int(H * 0.20), W, H], fill=(0, 0, 0, 110))
    base = Image.alpha_composite(base.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(base)
    pad = int(W * 0.06)

    # ── org logo (top-left) ──
    y_header = pad
    if logo_path:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            lsize = int(H * 0.10)
            logo.thumbnail((lsize, lsize), Image.LANCZOS)
            base.paste(logo, (pad, pad), logo)
        except Exception:
            pass

    # ── title + subtitle (top, offset right of the logo) ──
    title_x = pad + (int(H * 0.10) + pad // 2 if logo_path else 0)
    if show_title and title:
        tf = _font(int(H * 0.05))
        draw.text((title_x, pad), title, font=tf, fill=text_rgb)
        y_header = pad + int(H * 0.05) + 8
    if show_subtitle and subtitle:
        sf = _font(int(H * 0.028))
        draw.text((title_x, y_header), subtitle, font=sf, fill=accent_rgb)
        y_header += int(H * 0.028) + 8

    # ── standings zone ──
    zone_top = max(int(H * 0.24), y_header + int(H * 0.02))
    zone_bottom = int(H * 0.95)
    shown = standings[: max(1, max_rows)]
    n = max(1, len(shown))
    row_h = (zone_bottom - zone_top) / n
    row_font = _font(max(14, int(row_h * 0.45)))

    # Column x positions (rank | name | pts | kills), right side reserved for the numbers.
    rank_x = pad
    name_x = pad + int(W * 0.10)
    kills_x = W - pad
    pts_x = W - pad - int(W * 0.16)

    for i, row in enumerate(shown):
        y = zone_top + int(i * row_h)
        cy = y + int(row_h * 0.22)
        rank = row.get("rank", i + 1)
        name = (row.get("participant", {}) or {}).get("name") or "-"
        pts = row.get("total_points", 0)
        kills = row.get("kills", 0)
        # subtle alternating row band
        if i % 2 == 0:
            band = Image.new("RGBA", (W, int(row_h)), (255, 255, 255, 16))
            base.paste(band, (0, y), band)
            draw = ImageDraw.Draw(base)
        draw.text((rank_x, cy), f"#{rank}", font=row_font, fill=accent_rgb)
        # clip an over-long name to keep it off the numbers
        max_name_w = pts_x - name_x - 12
        nm = name
        while nm and _text_w(draw, nm, row_font) > max_name_w:
            nm = nm[:-1]
        if nm != name:
            nm = nm[:-1] + "…"
        draw.text((name_x, cy), nm, font=row_font, fill=text_rgb)
        draw.text((pts_x, cy), f"{pts} pts", font=row_font, fill=text_rgb)
        ktxt = f"{kills} K"
        draw.text((kills_x - _text_w(draw, ktxt, row_font), cy), ktxt, font=row_font, fill=muted_rgb)

    buf = io.BytesIO()
    base.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()
