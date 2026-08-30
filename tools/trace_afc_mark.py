#!/usr/bin/env python
# ──────────────────────────────────────────────────────────────────────────────
# Traces the AFC mark from its 500x500 PNG into a real vector, and PROVES the
# result against the source before writing it.
#
# WHY THIS EXISTS (owner, 2026-08-30: "create the vector of afc mark")
#     afc_sso/brand.py published a brand kit whose logo section had to say, in
#     writing, that the only AFC mark in either repository is a 500x500 PNG and
#     that a partner must not draw it larger. That is a real ceiling: the house
#     rule is that art is never drawn above its own resolution, and a better
#     filter does not invent detail that was never in the file.
#
#     The rule allows exactly three honest moves when art is too small. AFC could
#     not take the first (nobody holds the original vector) and would not take the
#     third (drawing it smaller is what the ceiling already forced). So this takes
#     the second: TRACE what we have.
#
# THE ONE RULE THAT SHAPES THIS WHOLE FILE
#     "Feed the tracer the raw coverage gradient, never a mask hardened at source
#     resolution, because the antialiasing is sub-pixel edge information and
#     hardening throws it away."
#
#     A 500x500 PNG whose alpha runs the full 0..255 range carries the true edge
#     to roughly a fifth of a pixel inside its antialiasing. Thresholding at 500
#     would discard that and hand the tracer a staircase. So this script:
#
#       1. splits the mark into two COVERAGE fields (float 0..1), not two masks
#       2. bilinearly resamples each coverage field 4x, which interpolates the
#          gradient rather than inventing edges
#       3. only then thresholds, at 2000x2000, where a half-covered source pixel
#          lands its boundary within about a quarter of a source pixel
#
# THE MARK IS TWO FLAT COLOURS STACKED, WHICH IS WHY A TRACE IS HONEST HERE
#     Measured on the source: 76.1% fully transparent, and the opaque body is two
#     clusters, near-black (3,3,3) and AFC green (43,160,53), everything between
#     them being antialiasing or the resave noise of a logo that has been through
#     a lossy step at some point in its life. A photograph would not survive this
#     treatment. A two-colour logotype does.
#
#     The two layers are stacked, not adjacent: the black is the outline and drop
#     shadow, and the green sits on top of it. So the dark layer is traced from
#     the TOTAL silhouette and the green is painted over it. Tracing the dark
#     layer as "the pixels that are dark" instead would leave a hairline seam
#     everywhere the two meet, because the antialiased boundary pixels belong
#     partly to both and fully to neither.
#
# WHAT IT WRITES  (nothing is written unless verification passes)
#     afc_sso/brand_assets/afc-mark.svg           the mark, green on black
#     afc_sso/brand_assets/afc-mark-on-dark.svg   the same paths, wordmark light
#     afc_sso/brand_assets/afc-mark.provenance.json
#
# WHY A PROVENANCE FILE
#     House rule: record where every piece of art came from, with the score that
#     justified it, because "where did that logo come from" gets asked and the
#     answer must not be a memory. The JSON carries the source path, its sha256,
#     the tracer and its settings, and the measured agreement per layer.
#
# THE CHECK IS PART OF THE BUILD, NOT PART OF THIS SCRIPT
#     afc_sso/tests/test_brand_vector.py re-measures the committed SVG against the
#     committed PNG on every CI run. This class of fault is invisible to a build
#     that only checks the file exists, and it has shipped soft elsewhere twice.
#
# CONNECTS TO
#     afc_sso/brand.py                     serves the svg key this produces
#     afc_sso/brand_assets/                where the output lands, committed
#     afc_sso/tests/test_brand_vector.py   re-runs the agreement measurement
#     frontend app/(root)/brand/page.tsx   offers the svg as the preferred download
#
# RUN
#     backend/.venv/Scripts/python.exe tools/trace_afc_mark.py
#     backend/.venv/Scripts/python.exe tools/trace_afc_mark.py --check   (measure only)
#
# DEPENDENCIES: pillow and numpy, both already in the venv, plus `vtracer` which
# is a BUILD-TIME tool only. It is deliberately NOT in requirements.txt: the
# server never traces anything, it serves a committed file. Install it only when
# re-tracing:  pip install vtracer
# ──────────────────────────────────────────────────────────────────────────────
import argparse
import hashlib
import json
import os
import re
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)

# The source of truth for the mark. Same file afc-mark-500.png is a copy of, and
# the same file the event proposals and the organizer cards draw.
SOURCE_PNG = os.path.join(BACKEND, "afc_organizers", "assets", "afc-logo.png")
OUT_DIR = os.path.join(BACKEND, "afc_sso", "brand_assets")
OUT_SVG = os.path.join(OUT_DIR, "afc-mark.svg")
OUT_SVG_DARK = os.path.join(OUT_DIR, "afc-mark-on-dark.svg")
OUT_PROV = os.path.join(OUT_DIR, "afc-mark.provenance.json")

# The two colours the mark is actually drawn in, measured off the source rather
# than taken from the brand tokens: this is what the artwork contains, and the
# point of the split is to reproduce the artwork, not to restyle it.
#
# The published brand green is #15a249 (oklch 0.624 0.170 149.09). The mark's own
# green is #2ba035, a little duller. They are NOT reconciled here on purpose:
# silently recolouring a logo to match a token is a substitution nobody agreed to,
# and the whole reason this file exists is that a partner drew our mark wrong.
DARK_HEX = "#030303"
GREEN_HEX = "#2ba035"
DARK_RGB = np.array([3, 3, 3], dtype=np.float64)
GREEN_RGB = np.array([43, 160, 53], dtype=np.float64)

# On a dark surface the black wordmark disappears entirely (see the module note in
# brand.py). The vector makes a light variant free, so it is emitted: same paths,
# the dark layer swapped for the site's own off-white foreground token.
LIGHT_HEX = "#fafafa"

# 8x. This number was MEASURED, not guessed. vtracer walks the outer edge of the
# set pixels, so the boundary it returns sits about half a supersampled pixel
# outside the true half-coverage contour, and the mark comes back very slightly
# fat: at 4x that was 851 pixels of spread edge against 46 genuinely missing, an
# 0.983 agreement that failed the floor for a reason that is a bias and not a
# wrong shape. Doubling the supersample halves the bias. Going further stops
# paying: the tracer's corner detector starts reading a sharp point as a short
# curve once the quantisation is finer than its own tolerances, and these
# letterforms are all points.
SUPERSAMPLE = 8

# vtracer settings, all stated rather than defaulted so a re-trace years from now
# reproduces this file.
#
# The two thresholds measured in PIXELS are expressed against SUPERSAMPLE, because
# they mean something about the SOURCE and would silently change meaning if the
# supersample were retuned and they were left as bare numbers.
#   filter_speckle  discards a patch under this AREA. 0.4 of a source pixel: small
#                   enough to keep the wordmark's thinnest strokes, which at 500px
#                   are barely two pixels wide, and large enough to drop the resave
#                   noise this file carries.
#   length_threshold  the shortest segment worth keeping. THREE source pixels, and that
#                   number was swept rather than picked: at one pixel the pair of svgs came
#                   to 361 KB, at three it is 252 KB, and the agreement moves by 0.0003 on
#                   the silhouette while the green actually improves. Past three it starts
#                   costing fidelity for very little size, so this is where it stops.
# corner_threshold and splice_threshold are ANGLES and so are scale free.
# corner_threshold is raised from vtracer's default 60 because the AFC letterforms
# are angular throughout and a lower value rounds their points off.
TRACE_OPTS = {
    "colormode": "binary",
    "mode": "spline",
    "filter_speckle": int(0.4 * SUPERSAMPLE * SUPERSAMPLE),
    "corner_threshold": 75,
    "length_threshold": 3.0 * SUPERSAMPLE,
    "splice_threshold": 45,
    "max_iterations": 10,
    "path_precision": 2,
}

# Decimal places kept on a 500-unit coordinate. This is a FILE SIZE knob, and the number
# was chosen by measuring both ends: at 3 the pair of svgs came to 410 KB, which is more
# than the 500px PNG they are meant to replace and undercuts the advice to prefer them. Two
# places is a hundredth of a source pixel, still far finer than anything an 8x trace of a
# 500px raster can resolve, and the agreement score below is what proves it costs nothing.
COORD_DECIMALS = 2

# The agreement each layer must reach against the source coverage before anything
# is written. Intersection over union on the half-covered mask, measured at source
# resolution. 0.985 is not arbitrary: below about 0.98 the wordmark's counters
# start filling in, which is visible at 32px.
MIN_IOU = 0.985


# ── §1  the source, and its coverage fields ───────────────────────────────────
def load_source():
    """The mark as (rgb float array, alpha coverage 0..1, sha256 of the file)."""
    with open(SOURCE_PNG, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    im = Image.open(SOURCE_PNG).convert("RGBA")
    arr = np.asarray(im).astype(np.float64)
    return arr[..., :3], arr[..., 3] / 255.0, digest, im.size


def coverage_fields(rgb, alpha):
    """Split the mark into the two stacked coverage fields it is drawn from.

    Returns (dark_coverage, green_coverage), both float 0..1 at source size.

    The green weight is the pixel's position on the black-to-green axis, which is
    the right question for a two-colour image: a boundary pixel that reads as a
    50/50 blend is half green, and saying so preserves the sub-pixel edge that a
    nearest-colour classification would round away.

    The dark layer is the FULL silhouette, not "the pixels that are dark". The
    black is behind the green in the artwork, so painting the whole silhouette
    dark and the green over it reproduces the stack and leaves no seam. Tracing
    the two as adjacent regions leaves a hairline of background between them at
    every shared edge, which at 32px reads as a crack through the letterforms.
    """
    axis = GREEN_RGB - DARK_RGB
    denom = float(np.dot(axis, axis))
    # Projection of (pixel - black) onto the black-to-green axis, clamped to the
    # segment. Fully transparent pixels carry meaningless colour, so their weight
    # is multiplied out by alpha below regardless of what it lands on.
    weight = np.clip(((rgb - DARK_RGB) @ axis) / denom, 0.0, 1.0)
    return alpha.copy(), alpha * weight


# ── §2  resample the gradient, then and only then threshold ───────────────────
def upsample_coverage(cov, factor):
    """Bilinearly resample a coverage field, sampling at pixel CENTRES.

    This is the step the house rule is about. Interpolating coverage does not
    invent detail; it reads the edge position the antialiasing already encoded.
    Sampling at centres (the +0.5 offsets) is what keeps the resampled field
    aligned with the source grid instead of drifting half a pixel toward 0,0.
    """
    h, w = cov.shape
    # Source coordinates of each destination pixel centre.
    ys = (np.arange(h * factor) + 0.5) / factor - 0.5
    xs = (np.arange(w * factor) + 0.5) / factor - 0.5
    ys = np.clip(ys, 0, h - 1)
    xs = np.clip(xs, 0, w - 1)

    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]

    top = cov[np.ix_(y0, x0)] * (1 - wx) + cov[np.ix_(y0, x1)] * wx
    bot = cov[np.ix_(y1, x0)] * (1 - wx) + cov[np.ix_(y1, x1)] * wx
    return top * (1 - wy) + bot * wy


def binary_rgba(mask):
    """A traceable RGBA buffer: opaque black shape on opaque white.

    Opaque white rather than transparent because vtracer in binary mode reads
    luminance, and a transparent background would leave the shape and the hole in
    it indistinguishable.
    """
    h, w = mask.shape
    buf = np.full((h, w, 4), 255, dtype=np.uint8)
    buf[..., :3][mask] = 0
    return buf


# ── §3  trace, and bring the coordinates back to the source's own scale ───────
# vtracer 0.6 emits ONE <path> per traced region, and every one of them starts at
# M0 0 with a `transform="translate(x,y)"` carrying its real position. Reading the
# `d` alone gives 27 shapes all stacked on the origin, most of them off-canvas at
# negative coordinates. That is exactly what the first run of this script produced
# and what the agreement floor caught: 0.0 against the source, nothing written.
_PATH_TAG = re.compile(r"<path\b[^>]*?/>", re.S)
_ATTR_D = re.compile(r'\bd="([^"]*)"')
_ATTR_FILL = re.compile(r'\bfill="([^"]*)"')
_ATTR_TRANSLATE = re.compile(r'\btransform="translate\(\s*([-\d.eE]+)[ ,]+([-\d.eE]+)\s*\)"')

# vtracer emits only these commands, all absolute, and every one of them takes
# whole coordinate PAIRS. Both facts are relied on below (the pair alternation is
# what lets a translate be folded into the numbers), so both are asserted.
_ALLOWED_CMDS = set("MLCZ")
_TOKEN = re.compile(r"[A-Za-z]|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

# In binary mode the traced shape comes back black. Anything else is the paper.
_SHAPE_FILL = "#000000"


def trace_layer(mask):
    """Trace one boolean mask into path `d` strings, at the mask's own scale.

    The translate on each path is folded into its coordinates here, so what comes
    back is a set of paths that already sit where they belong.
    """
    import tempfile

    import vtracer  # imported here so --check never needs the build-time tool

    # Through a temporary PNG rather than vtracer's pixel-list entry point: at this
    # supersample the list form means building tens of millions of Python tuples,
    # which costs minutes and gigabytes for no difference in the result.
    tmp = tempfile.mkdtemp(prefix="afc-trace-")
    src = os.path.join(tmp, "layer.png")
    out = os.path.join(tmp, "layer.svg")
    Image.fromarray(binary_rgba(mask), "RGBA").save(src)
    vtracer.convert_image_to_svg_py(src, out, **TRACE_OPTS)
    with open(out, "r", encoding="utf-8") as fh:
        svg = fh.read()

    paths = []
    for tag in _PATH_TAG.findall(svg):
        fill = _ATTR_FILL.search(tag)
        if fill and fill.group(1).lower() != _SHAPE_FILL:
            continue  # the background, not the mark
        d = _ATTR_D.search(tag)
        if not d:
            continue
        move = _ATTR_TRANSLATE.search(tag)
        if not move and "transform=" in tag:
            raise SystemExit(
                f"Tracer put a transform on a path that is not a plain translate: "
                f"{tag[-120:]!r}. Folding it into the coordinates needs a look."
            )
        tx = float(move.group(1)) if move else 0.0
        ty = float(move.group(2)) if move else 0.0
        paths.append((d.group(1), tx, ty))
    return paths


def place_path(d, tx, ty, factor):
    """Fold a translate into a path's numbers and divide them by `factor`.

    Done by rewriting the numbers rather than leaving a transform on the output,
    so the committed SVG reads as a plain 500-unit drawing that anyone can open
    and edit without first undoing two levels of transform.

    Every allowed command consumes coordinate PAIRS, so the numbers alternate x,
    y, x, y across the whole path however the commands are grouped. That is what
    makes a single pass over the tokens correct.
    """
    out = []
    axis = 0  # 0 = the next number is an x, 1 = a y
    for token in _TOKEN.findall(d):
        if token.isalpha():
            if token.upper() not in _ALLOWED_CMDS:
                raise SystemExit(
                    f"Tracer emitted an unexpected path command {token!r}. The rewrite "
                    f"in place_path only understands M, L, C and Z, all absolute and "
                    f"all taking coordinate pairs, so this needs a look rather than a "
                    f"silent rescale."
                )
            if token.islower():
                raise SystemExit(
                    f"Tracer emitted a RELATIVE command {token!r}. Folding a translate "
                    f"into relative coordinates would move every point after the first, "
                    f"so this is refused rather than guessed at."
                )
            out.append(token)
            axis = 0
            continue
        value = (float(token) + (tx if axis == 0 else ty)) / factor
        axis ^= 1
        out.append(f"{value:.{COORD_DECIMALS}f}".rstrip("0").rstrip(".") or "0")

    # Numbers are joined with a space; a command needs none before it.
    text = ""
    for token in out:
        if token.isalpha():
            text += token
        else:
            text += (" " if text and not text[-1].isalpha() else "") + token
    return text


# ── §4  measure the trace against the source before trusting it ───────────────
def rasterise(svg_path, size):
    """Render an SVG to an RGBA array with headless Chrome.

    Chrome rather than a Python renderer because Chrome is what a browser will
    use to draw this on a partner's login page: measuring with the same engine
    that will paint it is the point. It is already a dependency of the docs and
    proposal builds.
    """
    import base64
    import subprocess
    import tempfile

    chrome = os.environ.get(
        "CHROME_BIN", r"C:/Program Files/Google/Chrome/Application/chrome.exe"
    )
    if not os.path.exists(chrome):
        return None

    with open(svg_path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")

    tmp = tempfile.mkdtemp(prefix="afc-mark-")
    html = os.path.join(tmp, "r.html")
    shot = os.path.join(tmp, "r.png")
    # Transparent page, the mark at exactly `size`, nothing else on it.
    with open(html, "w", encoding="utf-8") as fh:
        fh.write(
            "<style>html,body{margin:0;background:transparent}"
            f"img{{display:block;width:{size}px;height:{size}px}}</style>"
            f'<img src="data:image/svg+xml;base64,{data}">'
        )
    subprocess.run(
        [
            chrome,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--window-size={size},{size}",
            f"--screenshot={shot}",
            html,
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return np.asarray(Image.open(shot).convert("RGBA")).astype(np.float64)


def iou(a, b):
    """Intersection over union of two boolean masks."""
    union = np.logical_or(a, b).sum()
    return 1.0 if union == 0 else float(np.logical_and(a, b).sum()) / float(union)


def measure(svg_path, rgb, alpha):
    """Agreement between a rendered SVG and the source, per layer.

    Compared as masks at half coverage rather than as pixels, because a one-level
    difference in an antialiased edge pixel is not a defect and would drown the
    number that matters: whether the SHAPE is the same shape.
    """
    rendered = rasterise(svg_path, alpha.shape[0])
    if rendered is None:
        return None

    r_alpha = rendered[..., 3] / 255.0
    _, src_green = coverage_fields(rgb, alpha)
    _, out_green = coverage_fields(rendered[..., :3], r_alpha)

    return {
        "silhouette_iou": round(iou(alpha >= 0.5, r_alpha >= 0.5), 5),
        "green_iou": round(iou(src_green >= 0.5, out_green >= 0.5), 5),
        "coverage_mean_abs_error": round(float(np.abs(alpha - r_alpha).mean()), 5),
    }


# ── §5  the files ─────────────────────────────────────────────────────────────
def build_svg(dark_paths, green_paths, size, dark_fill):
    """One SVG: the dark silhouette, then the green over it.

    No width or height attributes, only a viewBox, so the mark scales to whatever
    box a partner puts it in. `shape-rendering` is left alone: the default is what
    a browser tunes per zoom level.
    """
    w, h = size
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="AFC, African Free Fire Community">',
        "<title>AFC, African Free Fire Community</title>",
        # fill-rule evenodd: the letterforms have counters (the hole in the A),
        # and the tracer emits them as separate subpaths of one path.
        f'<g fill="{dark_fill}" fill-rule="evenodd">',
    ]
    parts += [f'<path d="{d}"/>' for d in dark_paths]
    parts.append("</g>")
    parts.append(f'<g fill="{GREEN_HEX}" fill-rule="evenodd">')
    parts += [f'<path d="{d}"/>' for d in green_paths]
    parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="measure the committed SVG against the PNG and write nothing",
    )
    args = ap.parse_args()

    rgb, alpha, digest, size = load_source()

    if args.check:
        if not os.path.exists(OUT_SVG):
            raise SystemExit(f"No traced mark at {OUT_SVG}. Run without --check.")
        scores = measure(OUT_SVG, rgb, alpha)
        if scores is None:
            raise SystemExit("Chrome not found, so the trace could not be measured.")
        print(json.dumps(scores, indent=2))
        worst = min(scores["silhouette_iou"], scores["green_iou"])
        if worst < MIN_IOU:
            raise SystemExit(f"Agreement {worst} is below the {MIN_IOU} floor.")
        return

    print(f"source   {SOURCE_PNG}")
    print(f"sha256   {digest}")
    print(f"size     {size[0]}x{size[1]}")

    dark_cov, green_cov = coverage_fields(rgb, alpha)
    print(
        f"coverage dark {dark_cov.sum():.0f}px  green {green_cov.sum():.0f}px  "
        f"(as area, not pixel counts)"
    )

    layers = {}
    for name, cov in (("dark", dark_cov), ("green", green_cov)):
        big = upsample_coverage(cov, SUPERSAMPLE)
        mask = big >= 0.5
        print(f"trace    {name}: {mask.shape[0]}x{mask.shape[1]}, {mask.sum()} px set")
        traced = trace_layer(mask)
        layers[name] = [place_path(d, tx, ty, SUPERSAMPLE) for d, tx, ty in traced]
        print(f"         {name}: {len(traced)} paths")

    os.makedirs(OUT_DIR, exist_ok=True)
    # Written to a temporary name first: verification decides whether it lands.
    staged = OUT_SVG + ".staged"
    with open(staged, "w", encoding="utf-8") as fh:
        fh.write(build_svg(layers["dark"], layers["green"], size, DARK_HEX))

    scores = measure(staged, rgb, alpha)
    if scores is None:
        os.remove(staged)
        raise SystemExit(
            "Chrome not found, so the trace could not be verified. Nothing written. "
            "Set CHROME_BIN to the Chrome executable and run again."
        )
    print("agreement", json.dumps(scores))

    worst = min(scores["silhouette_iou"], scores["green_iou"])
    if worst < MIN_IOU:
        # The staged file is deliberately LEFT ON DISK. A trace that misses is
        # something to open and look at, and deleting the evidence is how the
        # next run repeats the same mistake.
        raise SystemExit(
            f"Agreement {worst} is below the {MIN_IOU} floor, so the mark was NOT "
            f"replaced. The attempt is at {staged} to look at. A trace that does not "
            f"match is worse than the PNG it replaces, because it looks sharp while "
            f"being the wrong shape."
        )

    os.replace(staged, OUT_SVG)
    with open(OUT_SVG_DARK, "w", encoding="utf-8") as fh:
        fh.write(build_svg(layers["dark"], layers["green"], size, LIGHT_HEX))

    with open(OUT_PROV, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "what": "The AFC mark, traced to vector from the only raster AFC holds.",
                "source": {
                    "path": "afc_organizers/assets/afc-logo.png",
                    "sha256": digest,
                    "resolution": list(size),
                    "note": (
                        "No original vector of the AFC mark exists in either "
                        "repository or was supplied by anyone. This is a trace of "
                        "AFC's own file, not a lookalike found elsewhere."
                    ),
                },
                "method": {
                    "tool": "vtracer",
                    "options": TRACE_OPTS,
                    "supersample": SUPERSAMPLE,
                    "input": (
                        "float coverage fields resampled bilinearly, thresholded "
                        "only after resampling, so the source antialiasing carries "
                        "the sub-pixel edge into the trace"
                    ),
                    "script": "tools/trace_afc_mark.py",
                },
                "colors": {"dark": DARK_HEX, "green": GREEN_HEX, "on_dark": LIGHT_HEX},
                "agreement": scores,
                "agreement_floor": MIN_IOU,
                "verified_with": "headless Chrome, rendered at the source resolution",
                "traced_on": "2026-08-30",
            },
            fh,
            indent=2,
        )
        fh.write("\n")

    for path in (OUT_SVG, OUT_SVG_DARK, OUT_PROV):
        print(f"wrote    {os.path.relpath(path, BACKEND)}  {os.path.getsize(path)} bytes")


if __name__ == "__main__":
    sys.exit(main())
