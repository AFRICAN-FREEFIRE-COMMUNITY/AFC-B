"""
afc_leaderboard.graphic - render a standalone leaderboard's standings onto a branded design.

OWNER 2026-06-13: organizers upload branded background designs (a per-org library,
afc_organizers.OrgLeaderboardDesign) and, when exporting a leaderboard, pick which design +
which size to download. This module composites the LIVE standings (rank / name / points /
kills) plus the tournament title, an optional stage-or-group subtitle, and the org logo onto
the chosen background, at Instagram (1080x1350) or YouTube (1920x1080) size, with Pillow.

Pure rendering: standings in (from standings.standalone_standings), PNG bytes out. No ORM
writes. Called by afc_leaderboard.views.leaderboard_graphic (the download endpoint).
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont


# ── Country flag resolver for the "team_flag" design column (owner 2026-07-04) ──────────────────
# A design can place a TEAM FLAG column; the renderer turns each row's team_country (ISO-2 or a full
# country name) into that country's flag PNG. Flags are downloaded ONCE from flagcdn.com and cached
# on disk (MEDIA_ROOT/flag_cache/<iso2>.png), so an export/overlay render never blocks on the
# network after the first time a country is seen. Returns None (no flag drawn) on any failure -
# unknown country, offline first-fetch, etc. - so a missing flag never breaks the graphic.
_FLAG_CACHE_MEM: dict = {}  # iso2 -> path | None, per-process memo


def _country_iso2(country):
    """Normalise a country string (ISO-2 like 'NG' or a full name like 'Nigeria') to lowercase
    ISO-2, or None. Uses pycountry (already a dependency) for the name lookup."""
    if not country:
        return None
    c = str(country).strip()
    if len(c) == 2 and c.isalpha():
        return c.lower()
    try:
        import pycountry
        hit = pycountry.countries.get(name=c) or (pycountry.countries.search_fuzzy(c) or [None])[0]
        return hit.alpha_2.lower() if hit else None
    except Exception:
        return None


def _country_flag_path(country):
    """Return a filesystem path to the country's flag PNG (cached), or None. Downloads from
    flagcdn.com on first use per country; memoised in-process + on disk."""
    iso2 = _country_iso2(country)
    if not iso2:
        return None
    if iso2 in _FLAG_CACHE_MEM:
        return _FLAG_CACHE_MEM[iso2]
    try:
        from django.conf import settings
        cache_dir = os.path.join(settings.MEDIA_ROOT, "flag_cache")
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{iso2}.png")
        if not os.path.exists(path):
            import requests
            # w320 = a crisp, small flag (flags are wide; the renderer fits it into the cell).
            resp = requests.get(f"https://flagcdn.com/w320/{iso2}.png", timeout=8)
            if resp.status_code != 200 or not resp.content:
                _FLAG_CACHE_MEM[iso2] = None
                return None
            with open(path, "wb") as fh:
                fh.write(resp.content)
        _FLAG_CACHE_MEM[iso2] = path
        return path
    except Exception:
        _FLAG_CACHE_MEM[iso2] = None
        return None

# Output canvases. IG = portrait feed post; YT = 16:9 thumbnail / stream card.
CANVAS = {
    "instagram": (1080, 1350),
    "youtube": (1920, 1080),
}
DEFAULT_BG = (10, 14, 12)        # dark AFC base when no background is uploaded for a size
DEFAULT_ACCENT = "#34d27b"
DEFAULT_TEXT = "#FFFFFF"
# A positioned logo's longest edge, as a fraction of canvas HEIGHT, per size band. Lets a big org
# logo and small sponsor logos coexist on one design. These three fractions are MIRRORED, verbatim,
# by the FE so a downloaded PNG matches the design editor + live overlay exactly:
#   • editor logo markers - LeaderboardDesignsManager.tsx LOGO_SIZE_FRAC (~L105)
#   • live overlay board  - DesignBoard.tsx LOGO_SIZE_FRAC (~L70)
# The FE draws each positioned logo in a square (edge x edge) box with CSS `object-fit: contain`
# (longest edge = edge); _paste_logos below reproduces that exact box via _contain_resize.
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

# An IN-ROW image cell (team logo / flag / player photo) is drawn 1.35x the field's TEXT size, so a
# logo sits slightly larger than the row's numbers. Mirrors the FE image cell, which sizes every
# image field at `fSizePx * 1.35` (fSizePx = the field's font_size_pct% of canvas height):
#   • design editor preview - DesignFieldsEditor.tsx ~L2135 (`const boxPx = fSizePx * 1.35`)
#   • live overlay board    - DesignBoard.tsx ~L213 (`height/width: sizePx * 1.35`)
# Backend previously sized in-row logos at 0.06*H (team_logo) / 0.05*H (flag) with NO 1.35 factor - 
# ~2x the editor box - which is why a downloaded logo looked far bigger than the editor sample
# (owner audit complaint I, 2026-07-05). The editor/overlay are the source of truth.
ROW_LOGO_SCALE = 1.35

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


def _contain_resize(img, box_px):
    """Scale `img` (up OR down) to FIT inside a box_px x box_px square while preserving aspect: the
    longest edge becomes box_px, the shorter edge scales proportionally. This is the pixel-exact
    equivalent of the FE's CSS `object-fit: contain` in a square box - the editor logo markers
    (LeaderboardDesignsManager.tsx `object-contain`), the editor in-row sample (DesignFieldsEditor.tsx
    `objectFit: "contain"`) and the overlay CellValue img (DesignBoard.tsx `objectFit: "contain"`).
    Unlike PIL.Image.thumbnail it UPSCALES small art too (the browser does), so the rendered box
    equals the editor's box regardless of the source resolution. Used by BOTH logo paths below."""
    box_px = max(1, int(box_px))
    iw, ih = img.size
    if iw <= 0 or ih <= 0:
        return img
    scale = box_px / float(max(iw, ih))
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    return img.resize((nw, nh), Image.LANCZOS)


# ── Render caches (owner 2026-07-13, "it took time to download today") ───────────────────────────
# Every graphic download re-renders server-side from scratch: the FE deliberately cache-busts each
# request (params._ts) so the PNG always reflects the LATEST scores, which means the same heavy pixels
# were recomputed on every click - the uploaded background decoded + LANCZOS cover-resized to the full
# 1920x1080 / 1080x1350 canvas, and every team/sponsor logo re-decoded + resized, per render. These
# two per-process memos skip that repeat work. Keyed on (file path + mtime + target box) so replacing
# a background or a team logo transparently invalidates only its own entry; a plain miss just does the
# original work, so correctness never depends on the cache. Bounded - cleared wholesale when large
# (backgrounds/logos per org are few, so this stays tiny in practice).
_BG_COVER_CACHE: dict = {}     # (path, mtime, w, h) -> cover-resized RGB Image (a COPY is returned)
_LOGO_RESIZE_CACHE: dict = {}  # (path, mtime, box_px) -> contained RGBA Image (only ever pasted)


def _file_mtime(path):
    """File mtime for cache keying, or 0 when it can't be read (a missing/None path just re-renders)."""
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0


def _cover_cached(path, size):
    """`_cover` (decode + LANCZOS fill-crop to `size`) memoised by (path, mtime, size). Returns a
    fresh COPY every call because the caller draws its fields/rows straight ONTO this base image, so
    the cached original must stay pristine for the next render. Raises on a bad path (the caller's
    existing try/except falls back to the plain dark background), matching the old inline behaviour."""
    key = (path, _file_mtime(path), size[0], size[1])
    img = _BG_COVER_CACHE.get(key)
    if img is None:
        img = _cover(Image.open(path).convert("RGB"), size)
        if len(_BG_COVER_CACHE) > 48:
            _BG_COVER_CACHE.clear()
        _BG_COVER_CACHE[key] = img
    return img.copy()


def _load_contained_rgba(path, box_px):
    """Decode `path` to RGBA + `_contain_resize` into a box_px square, memoised by (path, mtime,
    box_px). The result is only ever PASTED (paste reads the image, never mutates it), so callers may
    share the cached instance directly - no copy needed, even when the same logo repeats across rows.
    Returns None on any decode failure (so a bad/None path is a silent no-op, as before)."""
    box_px = max(1, int(box_px))
    key = (path, _file_mtime(path), box_px)
    img = _LOGO_RESIZE_CACHE.get(key)
    if img is None:
        try:
            img = _contain_resize(Image.open(path).convert("RGBA"), box_px)
        except Exception:
            return None
        if len(_LOGO_RESIZE_CACHE) > 256:
            _LOGO_RESIZE_CACHE.clear()
        _LOGO_RESIZE_CACHE[key] = img
    return img


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


def _fit_font(draw, text, base_size, max_w, font_path=None):
    """A font for `text` that fits within max_w: shrink from base_size down to a floor (45% of base).
    The caller still clips with _clip_text if even the floor overflows, so a very long title both
    shrinks AND ellipsis-truncates instead of overrunning the canvas.

    `font_path` (owner 2026-08-05) picks the design's UPLOADED font instead of the built-in one, so a
    column header / board header shrinks in the SAME typeface its column or design uses. None keeps
    the original built-in-font behaviour, which is what every pre-existing caller passes."""
    floor = max(14, int(base_size * 0.45))
    size = base_size
    while size > floor:
        f = _load_font(font_path, size)
        if _text_w(draw, text, f) <= max_w:
            return f
        size -= 2
    return _load_font(font_path, floor)


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


def _row_image_box_px(f, H):
    """Pixel box for an IN-ROW image cell (team logo / flag / player photo). It is the field's TEXT
    size (font_size_pct% of canvas H, default 2.1% = FIELD_SIZE_FRAC, the SAME default the editor +
    overlay use via `field.font_size_pct ?? 2.1`) times ROW_LOGO_SCALE (1.35). This reproduces the FE
    image-cell box exactly - DesignFieldsEditor.tsx ~L2135 `boxPx = fSizePx * 1.35` and DesignBoard.tsx
    CellValue `sizePx * 1.35` - so a downloaded logo lands at the size the operator sees in the editor.
    Computed in ONE expression (no intermediate floor) to stay within a pixel of the FE's float box."""
    pct = f.get("font_size_pct")
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = FIELD_SIZE_FRAC * 100.0   # 2.1 - mirrors the editor default `?? 2.1`
    return max(1, int(pct / 100.0 * H * ROW_LOGO_SCALE))


def _local_media_path(src):
    """Resolve an in-row image SOURCE that may be a filesystem PATH or a /media/... URL (absolute or
    relative) to a local file under MEDIA_ROOT, or return it unchanged when it already points at a
    readable file. None when it cannot be resolved. Lets the SAME rows the overlay serves (where
    esports_image / team_logo are absolute URLs) also render in the downloadable PNG: team logos + flags
    already pass filesystem paths (they fall straight through the os.path.exists check), while a player
    PHOTO URL from the MVP/top-killers payload is mapped back onto the local media file. Never raises."""
    if not src:
        return None
    try:
        s = str(src)
        if os.path.exists(s):                       # already a real filesystem path (logo/flag/export)
            return s
        from django.conf import settings
        # Try the configured MEDIA_URL first, then the conventional "/media/" marker, so a URL like
        # https://host/media/esports_pictures/x.png -> MEDIA_ROOT/esports_pictures/x.png.
        media_url = (getattr(settings, "MEDIA_URL", "") or "").rstrip("/")
        for marker in [m for m in (media_url, "/media") if m]:
            idx = s.find(marker + "/")
            if idx != -1:
                rel = s[idx + len(marker) + 1:].split("?", 1)[0].split("#", 1)[0]
                cand = os.path.join(settings.MEDIA_ROOT, rel.replace("/", os.sep))
                if os.path.exists(cand):
                    return cand
    except Exception:
        return None
    return None


def _paste_row_logo(base, path, cx, cy, edge_px):
    """Paste an in-row team logo / flag / player photo centred at (cx, cy), contained into a fixed
    edge_px x edge_px box (aspect preserved, longest side = edge_px), matching the design editor.

    NO alpha-trim (changed 2026-07-05, owner audit complaint I "downloaded logos don't match the
    editor"): the FE editor sample + live overlay draw the image with plain CSS `object-fit: contain`
    and NO trimming (DesignFieldsEditor.tsx ~L2148, DesignBoard.tsx CellValue ~L215). Trimming here
    made a padded logo fill the box MORE than the editor showed, so the download looked bigger than the
    sample. Dropping the trim + using _contain_resize (same box math as the editor, incl. upscale)
    makes the rendered footprint equal the editor's box. Silent no-op on a bad path.

    `path` may be a filesystem path OR a /media/... URL - _local_media_path resolves either (owner
    2026-07-05, complaints G+H: the MVP/top-killers overlay rows carry esports_image as a URL, so the
    export renders the SAME rows by mapping the URL onto the local media file)."""
    path = _local_media_path(path)
    if not path:
        return
    # Contain into the fixed box exactly like the editor's `object-fit: contain` (no trim, aspect
    # kept, small art upscaled), so the on-canvas footprint matches the editor sample pixel-for-pixel.
    # _load_contained_rgba memoises the decode+resize (same logo across rows/renders resolves once).
    limg = _load_contained_rgba(path, edge_px)
    if limg is None:
        return
    base.paste(limg, (cx - limg.width // 2, cy - limg.height // 2), limg)


# ══ BOARD CHROME: column headers, grid lines, event/stage header (owner 2026-08-05, backlog #2) ═══
#
# WHAT THIS IS: three OPT-IN layers drawn around the placed columns of the field-layout path. Until
# now the exported PNG showed bare numbers in bare rows - no label saying which column was kill points
# and which was placement points, no rules to follow a row across, and no event/stage name unless the
# designer happened to type one as a freeform text. These three layers fill that in WITHOUT moving a
# single placed column, so an existing graphic keeps its shape and only gains the missing information.
#
# HOW THEY ARE TURNED ON: purely from `field_layout` (built by
# afc_organizers.views_leaderboard_design.build_field_layout / build_pages_for_export /
# build_ephemeral_afc_default from the design's show_column_headers / show_grid / show_board_header
# booleans). A layout without those keys renders EXACTLY as before, so every caller that has not been
# updated is unaffected.
#
# WHY THE GEOMETRY IS DERIVED, NEVER STORED: headers and grid lines are computed from the fields'
# OWN x positions and the column group's OWN row tiling. Drag a column in the design editor and its
# header + its grid line move with it - they can never drift out of alignment with the numbers.
#
# MIRRORED BY (so the download is WYSIWYG with what the operator sees):
#   • the design editor canvas - frontend DesignFieldsEditor.tsx (header row + grid + board header)
#   • the live OBS overlay      - frontend app/overlay/leaderboard/_components/DesignBoard.tsx

# field_type -> the header label printed above that column. Uppercase because the AFC boards are
# uppercase throughout. An IMAGE column (team logo / flag / player photo) maps to "" so no label is
# stamped over the artwork; the TEAM/PLAYER label rides on the adjacent name column instead.
# `matches` is labelled MP: the owner asks for maps played to be shown as MP (backlog #17), and it is
# the same number the "Matches played" column has always carried (games_played from the standings).
COLUMN_HEADER_LABELS = {
    "pos": "POS",
    "team_name": "TEAM",
    "player_name": "PLAYER",
    "team_logo": "", "team_flag": "", "esports_image": "",
    "matches": "MP",
    "booyah": "BOOYAH",
    "kill_points": "KILL POINTS",
    "placement_points": "PLACEMENT POINTS",
    "total_points": "TOTAL POINTS",
    "kills": "KILLS",
    "rush_points": "RUSH POINTS",
    "base_total": "BASE TOTAL",
    "bonus": "BONUS",
    "penalty": "PENALTY",
    "damage": "DAMAGE",
    "assists": "ASSISTS",
    "mvp_count": "MVPS",
    # The map a booyah was won on (owner 2026-08-06); only booyah rows carry it.
    "match_map": "MAP",
    # LIVE-only stats (a design may place them; they get a label too rather than an unlabelled column).
    "deaths": "DEATHS", "knockdowns": "KNOCKS", "headshots": "HEADSHOTS",
    "most_used_weapon": "WEAPON", "survival_time": "SURVIVAL",
    "revives_received": "REVIVES", "gloowall_used": "GLOO", "medkit_used": "MEDKITS",
}

# A header is drawn ONE row-height above the group's first row, at 85% of the row font size, so it
# reads as a label rather than as another data row.
HEADER_ROW_GAP = 1.15       # in row-heights, above row 1 of the group (clears the grid's top rule)
HEADER_SIZE_SCALE = 0.85    # of the column's own font size
# Grid hairlines: alpha over whatever is behind them (background art or transparency) and a width
# that scales with the canvas, so the rules stay hairlines at 1080 AND at 1920.
GRID_ALPHA = 70
GRID_WIDTH_FRAC = 0.0015    # of canvas HEIGHT
# Default board-header placement when the design supplies no title_style / subtitle_style: centred
# near the top, clear of the AFC/organizer logos the default design parks at 8% / 90% x.
#
# max_w_pct IS THE PART THAT MATTERS, added 2026-08-05 after looking at a real export. "Clear of
# the logos" was only true for a SHORT title: the text was fitted to the full canvas width, so
# "DYNASTY CUP GRAND FINALS SSA" grew wide enough to run underneath the AFC logo and the board
# read "...NASTY CUP GRAND FINALS SSA". Capping the width makes a long title SHRINK instead of
# sliding under the artwork, which is the behaviour anybody would expect from a centred heading.
#
# 66% leaves the outer sixth of the canvas on each side to the logos. The subtitle gets 80%
# because it sits BELOW them and only needs to stay inside the padding.
BOARD_TITLE_DEFAULTS = {"x_pct": 50.0, "y_pct": 6.5, "font_size_pct": 4.5, "align": "center",
                        "max_w_pct": 66.0}
BOARD_SUBTITLE_DEFAULTS = {"x_pct": 50.0, "y_pct": 12.0, "font_size_pct": 2.6, "align": "center",
                           "max_w_pct": 80.0}


def _layout_groups(field_layout, rows_len):
    """The column groups of a field_layout, with the same default the field renderer uses. Shared by
    _render_fields, _render_column_headers and _render_grid so all three tile on identical geometry."""
    return field_layout.get("column_groups") or [
        {"row_start_pct": 33.0, "row_height_pct": 7.0, "row_count": rows_len, "start_rank": 1}
    ]


def _group_fields(field_layout, gi):
    """Every placed field belonging to column group `gi` (the same filter _render_fields applies)."""
    return [f for f in (field_layout.get("fields") or [])
            if int(f.get("column_group", 0) or 0) == gi]


LEFT_ALIGN_EDGE_PAD = 1.0   # percent of width kept in front of a LEFT-aligned column's anchor


def _column_edges(fields):
    """Turn a column group's placed fields into TABLE EDGES (percent of width): the boundary in front
    of each column, plus the closing edge after the last one. Returns [] for an empty group. Used for
    BOTH the vertical grid rules and to bound how wide a header label may grow.

    WHY NOT JUST THE MIDPOINT between neighbouring columns: a leaderboard's TEAM name is left-aligned
    and its text runs a long way to the right, into a wide gap before the first numeric column. A
    midpoint rule lands in the middle of that gap - straight through the team names. So a column's
    boundary is derived from its OWN alignment and its own cell width instead:
      • a LEFT-aligned column anchors its text at x and runs rightwards, so its boundary sits just in
        FRONT of x and its cell extends to wherever the next column's boundary falls (the name column
        gets the whole gap, which is exactly what it needs).
      • a CENTRE-aligned column's text spreads both ways, so its boundary sits half a cell in front,
        where the cell is the SMALLER of its two neighbouring gaps (never the huge name gap).
      • a RIGHT-aligned column's text runs leftwards, so it takes a full cell in front.
    Edges are then clamped to stay in order, so an odd hand-built layout can never produce a
    backwards rectangle."""
    if not fields:
        return []
    cols = sorted(
        ((float(f.get("x_pct", 10.0)), (f.get("align") or "center")) for f in fields),
        key=lambda c: c[0],
    )
    xs = [c[0] for c in cols]
    if len(cols) == 1:
        return [xs[0] - 6.0, xs[0] + 6.0]
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]

    def _cell(i):
        """The width this column may claim in front of its anchor (percent of canvas width)."""
        before = gaps[i - 1] if i > 0 else gaps[0]
        after = gaps[i] if i < len(gaps) else gaps[-1]
        return min(before, after)

    edges = []
    for i, (x, align) in enumerate(cols):
        if align == "left":
            edges.append(x - LEFT_ALIGN_EDGE_PAD)
        elif align == "right":
            edges.append(x - _cell(i))
        else:
            edges.append(x - _cell(i) / 2.0)
    # Closing edge after the last column, mirroring how much room it claimed in front of itself.
    last_x, last_align = cols[-1]
    tail = LEFT_ALIGN_EDGE_PAD if last_align == "right" else (
        _cell(len(cols) - 1) if last_align == "left" else _cell(len(cols) - 1) / 2.0)
    edges.append(last_x + tail)
    # Monotonic guard: keep every edge at or after the one before it.
    for i in range(1, len(edges)):
        edges[i] = max(edges[i], edges[i - 1])
    return edges


def _render_grid(base, field_layout, W, H, rgb):
    """Draw the row + column hairlines of every column group and return the (composited) image.

    Rows: one rule between each pair of rows plus one above the first and below the last, so each
    standings row sits in its own band. Columns: one rule on every edge from _column_edges, so the
    numbers line up in visible columns. Both are drawn on an RGBA layer at GRID_ALPHA and composited,
    which keeps them subtle over busy background art and keeps a TRANSPARENT overlay design
    transparent (a solid line would punch an opaque box into the OBS overlay).

    RETURNS the image to keep drawing on: alpha_composite produces a NEW image, so the caller must
    reassign (`base = _render_grid(base, ...)`). Called only from render_leaderboard_graphic, BEFORE
    the fields, so the data always sits on top of its own rules."""
    groups = _layout_groups(field_layout, 0)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    line_rgba = tuple(rgb) + (GRID_ALPHA,)
    width = max(1, int(H * GRID_WIDTH_FRAC))
    drew = False

    for gi, cg in enumerate(groups):
        edges = _column_edges(_group_fields(field_layout, gi))
        if not edges:
            continue
        rs = float(cg.get("row_start_pct", 33.0))
        rh = float(cg.get("row_height_pct", 7.0))
        rc = int(cg.get("row_count", 0) or 0)
        if rc <= 0:
            continue
        # Row band edges: half a row above row 1, then one per row boundary, to half a row below
        # the last row. A row's text is vertically centred on its y, so the half-row offset puts the
        # rule exactly between two rows.
        y_top = rs - rh / 2.0
        y_bottom = rs + (rc - 0.5) * rh
        x_left = edges[0] / 100.0 * W
        x_right = edges[-1] / 100.0 * W
        for i in range(rc + 1):
            y = int((y_top + i * rh) / 100.0 * H)
            ld.line([(x_left, y), (x_right, y)], fill=line_rgba, width=width)
        for e in edges:
            x = int(e / 100.0 * W)
            ld.line([(x, int(y_top / 100.0 * H)), (x, int(y_bottom / 100.0 * H))],
                    fill=line_rgba, width=width)
        drew = True

    if not drew:
        return base
    out = Image.alpha_composite(base.convert("RGBA"), layer)
    # Give an OPAQUE board back its RGB mode: alpha_composite always returns RGBA, and saving an
    # otherwise-opaque export with a redundant alpha channel just makes the PNG bigger. A transparent
    # overlay design stays RGBA, which is the whole point of it.
    return out if base.mode == "RGBA" else out.convert("RGB")


def _wrap_header(draw, label, base_size, max_w, font_path):
    """Fit a column-header label into max_w, returning (font, [line, ...]) with AT MOST two lines.

    A two-word label over a narrow numeric column ("PLACEMENT POINTS" above a ~10%-wide column) cannot
    shrink onto one line without becoming unreadable, so it stacks instead - the same thing a designer
    would do by hand, and the reason the owner's wording is kept verbatim rather than abbreviated.
    Single-word labels (POS, MP, BOOYAH) always come back as one line. The caller still ellipsis-clips
    each line, so even a pathological label can never overrun its column.

    WRAP BEFORE SHRINK: we try the full size on one line, then the full size on TWO lines, and only
    shrink after that. Shrinking first made "KILL POINTS" render at half the size of "BOOYAH" beside
    it, which reads as a mistake; wrapping first keeps every header in the row at the same size."""
    font = _load_font(font_path, base_size)
    if _text_w(draw, label, font) <= max_w:
        return font, [label]
    if " " not in label:
        return _fit_font(draw, label, base_size, max_w, font_path=font_path), [label]
    # Pick the space that gives the most balanced two-line split (smallest widest line).
    words = label.split(" ")
    best = min(
        (
            (max(_text_w(draw, " ".join(words[:i]), font),
                 _text_w(draw, " ".join(words[i:]), font)), i)
            for i in range(1, len(words))
        ),
        key=lambda pair: pair[0],
    )[1]
    top, bottom = " ".join(words[:best]), " ".join(words[best:])
    # Re-fit on the WIDER of the two lines so both share one size (a mixed-size header reads broken).
    wider = top if _text_w(draw, top, font) >= _text_w(draw, bottom, font) else bottom
    return _fit_font(draw, wider, base_size, max_w, font_path=font_path), [top, bottom]


def _render_column_headers(base, field_layout, W, H, default_rgb):
    """Draw one label above each placed column of each column group.

    The label comes from COLUMN_HEADER_LABELS; a column with no label (an image cell) is skipped. The
    label is drawn at the column's OWN x with the column's OWN alignment + font, so it stays welded to
    its numbers however the operator drags the column. Size = the column's own size x
    HEADER_SIZE_SCALE, shrunk further (and finally ellipsis-clipped) to fit the gap to the neighbouring
    column, so "PLACEMENT POINTS" over a narrow column shrinks instead of overrunning "TOTAL POINTS".
    Colour = the design's accent (AFC green) unless the column sets its own, matching the site rule
    that a heading is primary-coloured. Called from render_leaderboard_graphic after the rows."""
    draw = ImageDraw.Draw(base)
    for gi, cg in enumerate(_layout_groups(field_layout, 0)):
        fields = _group_fields(field_layout, gi)
        if not fields:
            continue
        rs = float(cg.get("row_start_pct", 33.0))
        rh = float(cg.get("row_height_pct", 7.0))
        # One row-height above row 1, clamped so a group tiled very high on the canvas still shows
        # its header instead of drawing it off the top edge.
        y = int(max(rh * 0.6, rs - rh * HEADER_ROW_GAP) / 100.0 * H)
        edges = _column_edges(fields)
        for f in fields:
            label = COLUMN_HEADER_LABELS.get(f.get("field_type"), "")
            if not label:
                continue
            x_pct = float(f.get("x_pct", 10.0))
            # Widest this label may be: the distance to the column edges on either side of it (the
            # cell it owns), minus a small breathing gap.
            lo = max([e for e in edges if e <= x_pct], default=x_pct - 6.0)
            hi = min([e for e in edges if e >= x_pct], default=x_pct + 6.0)
            max_w = max(24, int((hi - lo) / 100.0 * W) - int(W * 0.008))
            base_size = int(_elem_size_px(f, H, FIELD_SIZE_FRAC) * HEADER_SIZE_SCALE)
            font, lines = _wrap_header(draw, label, base_size, max_w, f.get("font_path"))
            # Stack the (at most two) lines centred on the header baseline so a one-line and a
            # two-line header in the same row still read as one header row.
            line_h = int(getattr(font, "size", base_size) * 1.05)
            y0 = y - (line_h * (len(lines) - 1)) // 2
            for li, line in enumerate(lines):
                draw.text((int(x_pct / 100.0 * W), y0 + li * line_h),
                          _clip_text(draw, line, font, max_w),
                          font=font, fill=_elem_color(f, default_rgb),
                          anchor=_anchor_x(f.get("align", "center")) + "m")


def _render_board_header(base, field_layout, title, subtitle, W, H, text_rgb, accent_rgb,
                         show_title=True, show_subtitle=True):
    """Draw the board's TITLE (the event name) and SUB-TITLE (the stage name) on a field-layout board.

    The two strings are the `title` / `subtitle` the export endpoints already pass - event_stage_graphic
    sends event.event_name + stage.stage_name, leaderboard_graphic sends the leaderboard name + the
    typed subtitle - so nothing new has to be plumbed; the field-layout path simply never drew them
    before (only the legacy auto-table did). Position/size/colour/alignment come from the design's
    title_style / subtitle_style (OrgLeaderboardDesign, owner 2026-07-02, previously unused by any
    renderer), falling back to BOARD_TITLE_DEFAULTS / BOARD_SUBTITLE_DEFAULTS: centred at the top,
    title in the accent (AFC green, per the site's primary-coloured page titles) and sub-header in the
    text colour. Both shrink-to-fit then ellipsis-clip so a long event name cannot overrun the canvas."""
    draw = ImageDraw.Draw(base)
    pad = int(W * 0.06)
    max_w = W - 2 * pad

    def _draw(style, defaults, content, colour):
        style = style or {}
        x = int(float(style.get("x_pct", defaults["x_pct"])) / 100.0 * W)
        y = int(float(style.get("y_pct", defaults["y_pct"])) / 100.0 * H)
        base_size = max(8, int(float(style.get("font_size_pct", defaults["font_size_pct"]))
                               / 100.0 * H))
        path = style.get("font_path")
        # The width this line may occupy. A DESIGN that positions its own title keeps the old
        # full-width behaviour, because the person who placed it decided where it goes; only the
        # defaults constrain themselves, and only so a long event name shrinks rather than sliding
        # under a logo.
        limit = max_w
        if "max_w_pct" in defaults and not style.get("x_pct"):
            limit = min(limit, int(float(defaults["max_w_pct"]) / 100.0 * W))
        font = _fit_font(draw, content, base_size, limit, font_path=path)
        raw = (style.get("color") or "").strip()
        draw.text((x, y), _clip_text(draw, content, font, limit), font=font,
                  fill=(_hex(raw, "#FFFFFF") if raw else colour),
                  anchor=_anchor_x(style.get("align", defaults["align"])) + "m")

    if show_title and title:
        _draw(field_layout.get("title_style"), BOARD_TITLE_DEFAULTS, str(title), accent_rgb)
    if show_subtitle and subtitle:
        _draw(field_layout.get("subtitle_style"), BOARD_SUBTITLE_DEFAULTS, str(subtitle), text_rgb)


def _render_fields(base, field_layout, rows, W, H, default_rgb):
    """FIELD-LAYOUT path: tile the standings `rows` down per column group and draw each placed
    field at its x_pct. `rows` is a list of dicts keyed by field_type. For a TEAM leaderboard board:
    pos/team_name/team_logo/team_flag/booyah/placement_points/kill_points/total_points/rush_points/
    kills/matches/base_total/bonus/penalty. For a PLAYER board (MVP / top-killers, owner 2026-07-05):
    pos (player rank)/player_name/esports_image (photo)/kills/damage/assists/mvp_count/team_name/
    team_country. IMAGE cells (team_logo, team_flag, esports_image) are pasted via _paste_row_logo;
    esports_image accepts a URL or a filesystem path. Every other key is drawn as TEXT. A key a given
    board doesn't carry is simply skipped (blank cell), so team + player boards share this one path.
    Y for row i of group g comes from the group's row_start_pct + i*row_height_pct."""
    draw = ImageDraw.Draw(base)
    groups = _layout_groups(field_layout, len(rows))
    fields = field_layout.get("fields") or []
    # Fallback artwork per IMAGE field_type (owner 2026-08-05, backlog #3: the MVP graphic must still
    # show a portrait for a player who has never uploaded one). {} => a missing image just leaves the
    # cell blank, exactly as before. Supplied by the ephemeral player default
    # (afc_organizers.views_leaderboard_design.build_ephemeral_afc_player_default).
    placeholders = field_layout.get("image_placeholders") or {}
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
                    # Box = the field's font px x 1.35 (see _row_image_box_px), matching the editor's
                    # in-row logo sample so a downloaded logo is the same size the operator placed.
                    _paste_row_logo(base, r.get("team_logo"), x, y, _row_image_box_px(f, H))
                    continue
                if ft == "team_flag":
                    # Country flag column (owner 2026-07-04): resolve the row's team_country to a
                    # cached flag PNG and paste it in the cell. Same box math as team_logo (the FE
                    # renders team_flag through the identical image cell), so the flag matches too.
                    _paste_row_logo(base, _country_flag_path(r.get("team_country")), x, y,
                                    _row_image_box_px(f, H))
                    continue
                if ft == "esports_image":
                    # Player PHOTO cell (owner 2026-07-05, complaints G+H): the MVP / top-killers boards
                    # place the player's esport image. Render it as an IMAGE (object-contain box) exactly
                    # like team_logo / team_flag - previously it fell through to the TEXT path and drew
                    # the raw URL. Same box math (_row_image_box_px) so a player photo is sized WYSIWYG
                    # with the editor. The value may be a URL (overlay payload rows) OR a local path
                    # (export rows); _paste_row_logo -> _local_media_path resolves either. A player with
                    # NO photo falls back to the layout's placeholder art when one is supplied (owner
                    # 2026-08-05: the MVP graphic must never show an empty portrait slot), else the cell
                    # stays blank - which is also what a TEAM leaderboard row does (it has no such key).
                    # RESOLVE FIRST, then fall back. `row_value or placeholder` was not enough: a
                    # player row usually carries a photo URL, and a URL that does not map onto a
                    # file under MEDIA_ROOT is still TRUTHY, so the placeholder was skipped and
                    # _paste_row_logo bailed out silently. Every slot on the MVP board came out
                    # empty even though the placeholder art was generated, wired and correct.
                    # That is the common case, not an edge one: production media is not on the
                    # machine rendering a local export.
                    photo = _local_media_path(r.get("esports_image"))
                    _paste_row_logo(base, photo or placeholders.get("esports_image"),
                                    x, y, _row_image_box_px(f, H))
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
        frac = LOGO_SIZE_FRAC.get((spec.get("size") or "medium"), LOGO_SIZE_FRAC["medium"])
        edge = max(1, int(H * frac))
        # Contain into an edge x edge box (longest side = edge), matching the FE editor/overlay's
        # square `object-fit: contain` logo box (LeaderboardDesignsManager.tsx marker + DesignBoard.tsx
        # positioned logo). _contain_resize also upscales small art, exactly like the browser.
        # _load_contained_rgba memoises the decode+resize (sponsor logos repeat across every re-render).
        limg = _load_contained_rgba(spec.get("path"), edge)
        if limg is None:
            continue
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
                      placed fields/logos/texts are drawn - the PNG can overlay an OBS scene. Wired
                      from event_stage_graphic + leaderboard_graphic (design.transparent_background).
    logos           : the design's positioned logos, a list of
                      {"path": <fs path>, "x_pct": 0..100, "y_pct": 0..100, "size": s|m|l}.
                      Each is drawn CENTRED at (x_pct% of W, y_pct% of H) and scaled per size band.
                      Drawn on TOP so the user's placement is honoured (WYSIWYG with the editor).
    logo_path       : org logo path, drawn top-left as a FALLBACK only when `logos` is empty (so an
                      unconfigured design still carries branding); or None.
    title           : the tournament / leaderboard name (drawn when show_title).
    subtitle        : stage / group played, typed at export (drawn when show_subtitle).
    field_layout    : the placed-column layout (build_field_layout / build_pages_for_export /
                      build_ephemeral_afc_default). Beyond column_groups / fields / texts it may
                      carry the OPT-IN board chrome added 2026-08-05 (backlog #2), each ignored when
                      absent so an older layout renders unchanged:
                        show_column_headers  -> a label row above each column group
                        show_grid            -> hairline rules between rows AND columns
                        show_board_header    -> draw `title` as the header + `subtitle` as the
                                                sub-header (styled by title_style/subtitle_style)
                        image_placeholders   -> {field_type: path} fallback art for an empty image
                                                cell (the MVP board's missing player photo)
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
            # _cover_cached memoises the decode + LANCZOS cover-resize across re-renders (returns a
            # private copy to draw on); falls back to the plain dark fill on a bad path as before.
            base = _cover_cached(background_path, canvas_size)
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
        # ── Board CHROME (owner 2026-08-05, backlog #2) ── all three layers are OPT-IN per design, so a
        # layout without these keys renders byte-for-byte as it did before. Order matters: the GRID goes
        # down first (rules under the data), then the rows, then the column headers + the event/stage
        # header on top of them. _render_grid composites onto a NEW image, hence the reassignment.
        if field_layout.get("show_grid"):
            base = _render_grid(base, field_layout, W, H, text_rgb)
        _render_fields(base, field_layout, rows or [], W, H, text_rgb)
        if field_layout.get("show_column_headers"):
            _render_column_headers(base, field_layout, W, H, accent_rgb)
        if field_layout.get("show_board_header"):
            _render_board_header(base, field_layout, title, subtitle, W, H, text_rgb, accent_rgb,
                                 show_title=show_title, show_subtitle=show_subtitle)
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
    # Uses the SAME helper as the field-layout path so both positioned-logo code paths size
    # identically (they previously duplicated this loop; unified 2026-07-05 so they can never drift).
    _paste_logos(base, logos, W, H)

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
