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

# Default sizes for the FIELD-LAYOUT path (owner 2026-06-14), as a fraction of canvas HEIGHT,
# used when a field/text has no explicit font_size_pct. A field row (~3.6% of H) reads cleanly in
# a standings box; freeform text defaults larger (~5%). Both are overridable per element.
# 0.021 = 2.1% of canvas height, matching the DesignBoard/overlay default
# (`field.font_size_pct ?? 2.1` in DesignBoard.tsx). Was 0.036, which rendered every unset field
# ~1.7x bigger in the PNG export than the editor preview + live overlay showed (owner 2026-07-03:
# "download didn't follow the sizes set in design"). The editor/overlay are the source of truth.
FIELD_SIZE_FRAC = 0.021
TEXT_SIZE_FRAC = 0.05

# Cache loaded truetype fonts by (path, size) so a 16-row x 6-field render does not re-open the
# same .ttf 96 times.
_FONT_CACHE = {}


def _load_font(path, size):
    """A truetype font from an uploaded font file at `size` px, cached. Falls back to the built-in
    scalable font (_font) when no path is given or the file cannot be read (so a missing/broken
    custom font never breaks a render)."""
    size = max(8, int(size))
    if not path:
        return _font(size)
    key = (path, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        f = _font(size)
    _FONT_CACHE[key] = f
    return f


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


def _anchor_x(align):
    """Map an alignment to a Pillow text anchor X char: left=l, center=m, right=r (paired with
    'm' for vertical-middle => 'lm'/'mm'/'rm'). Lets a placed field/text be anchored at its x_pct."""
    return {"left": "l", "right": "r"}.get(align, "m")


def _elem_color(elem, default_rgb):
    """A field/text's colour: its own hex when set, else the design default (already an rgb tuple)."""
    raw = (elem.get("color") or "").strip()
    return _hex(raw, "#FFFFFF") if raw else default_rgb


def _elem_size_px(elem, H, frac):
    """A field/text's pixel size: font_size_pct (% of canvas H) when set, else `frac` of H."""
    pct = elem.get("font_size_pct")
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = frac * 100.0
    return max(8, int(pct / 100.0 * H))


def _paste_row_logo(base, path, cx, cy, H, edge_px):
    """Paste a team logo centred at (cx, cy), normalised so EVERY logo occupies the same visual
    footprint regardless of its source aspect/padding (owner 2026-07-03: "uniformity in size for all
    logos"). Two steps: (1) auto-trim the logo's transparent border so baked-in padding doesn't make
    one mark look tiny next to a full-bleed square; (2) scale the trimmed mark to FILL a fixed
    edge_px x edge_px box (contain: the longest side hits edge_px), centred. Silent no-op on a bad
    path."""
    if not path:
        return
    try:
        limg = Image.open(path).convert("RGBA")
    except Exception:
        return
    # (1) Trim fully-transparent margins so logos with lots of alpha padding scale up to match
    # full-bleed ones. getbbox() on the alpha channel is the tight content box; skip if the logo is
    # opaque (no alpha to trim) or the bbox read fails.
    try:
        bbox = limg.split()[3].getbbox()
        if bbox and bbox != (0, 0, limg.width, limg.height):
            limg = limg.crop(bbox)
    except Exception:
        pass
    # (2) Contain into the fixed box. thumbnail preserves aspect; after the trim every mark now
    # fills the box to its longest side, so footprints are uniform.
    limg.thumbnail((edge_px, edge_px), Image.LANCZOS)
    base.paste(limg, (cx - limg.width // 2, cy - limg.height // 2), limg)


def _render_fields(base, field_layout, rows, W, H, default_rgb):
    """FIELD-LAYOUT path: tile the standings `rows` down per column group and draw each placed
    field at its x_pct. `rows` is a list of dicts keyed by field_type (pos/team_name/team_logo/
    booyah/placement_points/kill_points/total_points/rush_points/kills/matches/base_total/bonus/
    penalty); team_logo carries a filesystem path. Y for row i of group g comes from the group's
    row_start_pct + i*row_height_pct."""
    draw = ImageDraw.Draw(base)
    groups = field_layout.get("column_groups") or [
        {"row_start_pct": 33.0, "row_height_pct": 7.0, "row_count": len(rows), "start_rank": 1}
    ]
    fields = field_layout.get("fields") or []
    for gi, cg in enumerate(groups):
        rs = float(cg.get("row_start_pct", 33.0))
        rh = float(cg.get("row_height_pct", 7.0))
        rc = int(cg.get("row_count", len(rows)) or 0)
        start = int(cg.get("start_rank", 1) or 1)
        gfields = [f for f in fields if int(f.get("column_group", 0) or 0) == gi]
        for i in range(rc):
            ridx = start - 1 + i
            if ridx < 0 or ridx >= len(rows):
                continue
            r = rows[ridx]
            y = int((rs + i * rh) / 100.0 * H)
            for f in gfields:
                x = int(float(f.get("x_pct", 10.0)) / 100.0 * W)
                ft = f.get("field_type")
                if ft == "team_logo":
                    _paste_row_logo(base, r.get("team_logo"), x, y, H, _elem_size_px(f, H, 0.06))
                    continue
                val = r.get(ft)
                if val is None or val == "":
                    continue
                font = _load_font(f.get("font_path"), _elem_size_px(f, H, FIELD_SIZE_FRAC))
                draw.text((x, y), str(val), font=font, fill=_elem_color(f, default_rgb),
                          anchor=_anchor_x(f.get("align", "center")) + "m")


def _render_texts(base, texts, W, H, default_rgb):
    """Draw each FREEFORM text element once at (x_pct, y_pct) with its own font/size/colour/align."""
    draw = ImageDraw.Draw(base)
    for t in (texts or []):
        content = (t.get("text") or "").strip()
        if not content:
            continue
        x = int(float(t.get("x_pct", 50.0)) / 100.0 * W)
        y = int(float(t.get("y_pct", 15.0)) / 100.0 * H)
        font = _load_font(t.get("font_path"), _elem_size_px(t, H, TEXT_SIZE_FRAC))
        draw.text((x, y), content, font=font, fill=_elem_color(t, default_rgb),
                  anchor=_anchor_x(t.get("align", "center")) + "m")


def _paste_logos(base, logos, W, H):
    """Paste positioned logos centred at (x_pct% W, y_pct% H), longest edge = LOGO_SIZE_FRAC[size]
    of canvas height. Shared by the field-layout path (the legacy path keeps its own inline loop)."""
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
        px = max(0, min(W - limg.width, cx - limg.width // 2))
        py = max(0, min(H - limg.height, cy - limg.height // 2))
        base.paste(limg, (px, py), limg)


def render_leaderboard_graphic(standings, *, size="instagram", background_path=None,
                               logo_path=None, logos=None, title="", subtitle="",
                               text_color=DEFAULT_TEXT, accent_color=DEFAULT_ACCENT,
                               max_rows=16, show_title=True, show_subtitle=True,
                               field_layout=None, rows=None, transparent_background=False):
    """Composite `standings` (the standalone_standings list) onto a branded canvas and return
    PNG bytes.

    size            : "instagram" (1080x1350) or "youtube" (1920x1080).
    background_path : a filesystem path to the org design's background for this size, or None
                      -> a plain dark AFC background.
    transparent_background : when True (owner 2026-07-01, live-overlay designs) the canvas is a
                      fully-transparent RGBA image and the dark default fill is SKIPPED, so only the
                      placed fields/logos/texts are drawn — the PNG can overlay an OBS scene. Wired
                      from event_stage_graphic + leaderboard_graphic (design.transparent_background).
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
    # Transparent overlay designs (owner 2026-07-01) skip the background entirely: a fully-transparent
    # RGBA canvas so the placed columns float over whatever the streamer composites behind them in OBS.
    # Everything below (field-layout draw, positioned logos, texts) works on RGBA, and PNG preserves
    # the alpha; the legacy auto-table path is guarded to NOT flatten it (see the scrim block below).
    if transparent_background:
        base = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    elif background_path:
        try:
            bg = Image.open(background_path).convert("RGB")
            base = _cover(bg, canvas_size)
        except Exception:
            base = Image.new("RGB", canvas_size, DEFAULT_BG)
    else:
        base = Image.new("RGB", canvas_size, DEFAULT_BG)
    # FIELD-LAYOUT path (owner 2026-06-14): when the design places its own data fields, the design
    # IS the full graphic (e.g. the Dynasty board with its own headers/boxes). We do NOT apply the
    # scrim or draw the built-in title/table; we just fill the placed fields + freeform texts +
    # positioned logos, then return. The legacy auto-table path runs only when no fields are placed.
    use_field_layout = bool(field_layout and field_layout.get("fields"))
    if use_field_layout:
        _render_fields(base, field_layout, rows or [], W, H, text_rgb)
        _paste_logos(base, logos, W, H)            # positioned logos on top of the data
        _render_texts(base, field_layout.get("texts") or [], W, H, text_rgb)  # freeform on very top
        buf = io.BytesIO()
        base.save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()

    # A subtle dark scrim over the lower 2/3 keeps standings text legible on any background.
    # SKIP it for a transparent overlay (owner 2026-07-01): the dark scrim + convert("RGB") would
    # re-introduce an opaque fill and defeat the transparency. We instead keep the RGBA canvas so the
    # auto-table rows draw straight onto transparency and the PNG stays overlay-ready.
    if transparent_background:
        base = base.convert("RGBA")
    else:
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


def render_design_all_pages(rows, pages_spec, size="instagram", *,
                            logos=None, title="", subtitle="",
                            text_color=DEFAULT_TEXT, accent_color=DEFAULT_ACCENT,
                            max_rows=16, show_title=True, show_subtitle=True,
                            logo_path=None, transparent_background=False):
    """Render ALL pages of a multi-page design and return a list of PNG byte strings.

    pages_spec : list of per-page dicts as returned by
                 afc_organizers.views_leaderboard_design.build_pages_for_export:
        [{"page_number": int, "background_instagram": ImageField|None,
          "background_youtube": ImageField|None, "field_layout": dict|None}, ...]
    rows       : standings list (same per-row-dict format as render_leaderboard_graphic's `rows`
                 keyword). ALL rows are passed to every page; each page's field_layout column_groups
                 control which slice of the rankings that page shows (via start_rank + row_count).
    size       : "instagram" or "youtube" (all pages use the same size).
    logos      : the design-level positioned logos (drawn on every page). Page-specific logos are
                 not modelled yet; the design-level logos apply to all pages.
    Returns    : list[bytes] ordered by the pages_spec order (page_number). Called by
                 leaderboard_graphic + event_stage_graphic when ?page=all is requested, to build
                 the ZIP of one PNG per page."""
    result_pngs = []
    for page_spec in pages_spec:
        # Resolve the background filesystem PATH for the requested size from this page's ImageField.
        bg_field = (
            page_spec["background_youtube"] if size == "youtube"
            else page_spec["background_instagram"]
        )
        bg_path = None
        if bg_field:
            try:
                bg_path = bg_field.path
            except Exception:
                bg_path = None

        png = render_leaderboard_graphic(
            rows,               # full standings; column groups determine the per-page slice
            size=size,
            background_path=bg_path,
            logo_path=logo_path,
            logos=logos,
            title=title,
            subtitle=subtitle,
            text_color=text_color,
            accent_color=accent_color,
            max_rows=max_rows,
            show_title=show_title,
            show_subtitle=show_subtitle,
            field_layout=page_spec.get("field_layout"),
            rows=rows,
            transparent_background=transparent_background,
        )
        result_pngs.append(png)
    return result_pngs
