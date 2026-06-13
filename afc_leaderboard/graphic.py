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
# A positioned logo's longest edge, as a fraction of canvas HEIGHT, per size band. Lets a big org
# logo and small sponsor logos coexist on one design.
LOGO_SIZE_FRAC = {"small": 0.07, "medium": 0.11, "large": 0.16}


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


def _clip_text(draw, text, font, max_w):
    """Truncate `text` with an ellipsis so it fits within max_w at `font` (mirrors the standings
    name-column clip). Returns the text unchanged when it already fits."""
    if _text_w(draw, text, font) <= max_w:
        return text
    s = text
    while s and _text_w(draw, s + "…", font) > max_w:
        s = s[:-1]
    return (s + "…") if s else text


def _fit_font(draw, text, base_size, max_w):
    """A font for `text` that fits within max_w: shrink from base_size down to a floor (45% of base).
    The caller still clips with _clip_text if even the floor overflows, so a very long title both
    shrinks AND ellipsis-truncates instead of overrunning the canvas."""
    floor = max(14, int(base_size * 0.45))
    size = base_size
    while size > floor:
        f = _font(size)
        if _text_w(draw, text, f) <= max_w:
            return f
        size -= 2
    return _font(floor)


def render_leaderboard_graphic(standings, *, size="instagram", background_path=None,
                               logo_path=None, logos=None, title="", subtitle="",
                               text_color=DEFAULT_TEXT, accent_color=DEFAULT_ACCENT,
                               max_rows=16, show_title=True, show_subtitle=True):
    """Composite `standings` (the standalone_standings list) onto a branded canvas and return
    PNG bytes.

    size            : "instagram" (1080x1350) or "youtube" (1920x1080).
    background_path : a filesystem path to the org design's background for this size, or None
                      -> a plain dark AFC background.
    logos           : the design's positioned logos, a list of
                      {"path": <fs path>, "x_pct": 0..100, "y_pct": 0..100, "size": s|m|l}.
                      Each is drawn CENTRED at (x_pct% of W, y_pct% of H) and scaled per size band.
                      Drawn on TOP so the user's placement is honoured (WYSIWYG with the editor).
    logo_path       : org logo path, drawn top-left as a FALLBACK only when `logos` is empty (so an
                      unconfigured design still carries branding); or None.
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

    # ── org logo (top-left) ── FALLBACK only: when the design configures its own positioned
    # logos (drawn on top, at the end) we do NOT also draw the org logo here. An unconfigured
    # design still shows the org logo top-left so it carries branding by default.
    has_custom_logos = bool(logos)
    y_header = pad
    if logo_path and not has_custom_logos:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            lsize = int(H * 0.10)
            logo.thumbnail((lsize, lsize), Image.LANCZOS)
            base.paste(logo, (pad, pad), logo)
        except Exception:
            pass

    # ── title + subtitle (top) ── offset right of the FALLBACK org logo only; with custom logos
    # the title sits at the left pad (the user places logos freely and owns any overlap).
    title_x = pad + (int(H * 0.10) + pad // 2 if (logo_path and not has_custom_logos) else 0)
    # The text must not overrun the canvas: shrink the font to fit the available width, then clip
    # with an ellipsis as a last resort (the standings names already do this; titles must too, since
    # the title defaults to the user-controlled leaderboard name).
    text_max_w = W - title_x - pad
    if show_title and title:
        tf = _fit_font(draw, title, int(H * 0.05), text_max_w)
        draw.text((title_x, pad), _clip_text(draw, title, tf, text_max_w), font=tf, fill=text_rgb)
        y_header = pad + int(H * 0.05) + 8
    if show_subtitle and subtitle:
        sf = _fit_font(draw, subtitle, int(H * 0.028), text_max_w)
        draw.text((title_x, y_header), _clip_text(draw, subtitle, sf, text_max_w),
                  font=sf, fill=accent_rgb)
        y_header += int(H * 0.028) + 8

    # ── standings zone ──
    zone_top = max(int(H * 0.24), y_header + int(H * 0.02))
    zone_bottom = int(H * 0.95)
    zone_h = zone_bottom - zone_top
    shown = standings[: max(1, max_rows)]
    display_n = max(1, len(shown))
    # Row height fills the zone, but is CAPPED so a handful of rows don't balloon into giant
    # text (a 3-row board must not stretch each row to a third of the canvas, which would blow
    # the font up past the column widths and collide name/pts/kills). The cap keeps the font
    # readable and the columns clear regardless of row count; with few rows the board simply
    # top-aligns and leaves clean space below.
    max_row_h = int(H * 0.075)
    row_h = min(max_row_h, zone_h / display_n)
    row_font = _font(max(16, int(row_h * 0.42)))

    # Column geometry (rank | name | pts | kills). pts + kills are RIGHT-aligned inside reserved
    # right-hand columns, so a wide number can never overrun the name or spill past the canvas.
    rank_x = pad
    name_x = pad + int(W * 0.09)
    kills_right = W - pad                     # kills right edge
    pts_right = kills_right - int(W * 0.15)   # pts right edge (reserves the kills column)
    name_right = pts_right - int(W * 0.20)    # name clip edge (reserves the pts column + a gap,
                                              # wide enough for a 4-digit "1999 pts" total)
    max_name_w = name_right - name_x

    for i, row in enumerate(shown):
        y = zone_top + int(i * row_h)
        # Vertically center the text within the (capped) row band.
        cy = y + int(row_h * 0.28)
        rank = row.get("rank", i + 1)
        name = (row.get("participant", {}) or {}).get("name") or "-"
        pts = row.get("total_points", 0)
        kills = row.get("kills", 0)
        # subtle alternating row band
        if i % 2 == 0:
            band = Image.new("RGBA", (W, max(1, int(row_h))), (255, 255, 255, 16))
            base.paste(band, (0, y), band)
            draw = ImageDraw.Draw(base)
        # rank (accent), left
        draw.text((rank_x, cy), f"#{rank}", font=row_font, fill=accent_rgb)
        # name, clipped to its column so it never reaches the numbers
        nm = name
        while nm and _text_w(draw, nm, row_font) > max_name_w:
            nm = nm[:-1]
        if nm != name and nm:
            nm = nm[:-1] + "…"
        draw.text((name_x, cy), nm, font=row_font, fill=text_rgb)
        # pts, right-aligned at pts_right
        ptxt = f"{pts} pts"
        draw.text((pts_right - _text_w(draw, ptxt, row_font), cy), ptxt, font=row_font, fill=text_rgb)
        # kills, right-aligned at kills_right (muted)
        ktxt = f"{kills} K"
        draw.text((kills_right - _text_w(draw, ktxt, row_font), cy), ktxt, font=row_font, fill=muted_rgb)

    # ── positioned logos (drawn ON TOP, after standings) ── each centred at (x_pct% of W,
    # y_pct% of H) and scaled so its longest edge is LOGO_SIZE_FRAC[size] of the canvas height.
    for spec in (logos or []):
        try:
            limg = Image.open(spec["path"]).convert("RGBA")
        except Exception:
            continue
        frac = LOGO_SIZE_FRAC.get((spec.get("size") or "medium"), LOGO_SIZE_FRAC["medium"])
        edge = max(1, int(H * frac))
        limg.thumbnail((edge, edge), Image.LANCZOS)
        cx = int((spec.get("x_pct", 10.0) / 100.0) * W)
        cy = int((spec.get("y_pct", 10.0) / 100.0) * H)
        # paste centred on (cx, cy); clamp the top-left so the logo stays on-canvas.
        px = max(0, min(W - limg.width, cx - limg.width // 2))
        py = max(0, min(H - limg.height, cy - limg.height // 2))
        base.paste(limg, (px, py), limg)

    buf = io.BytesIO()
    base.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()
