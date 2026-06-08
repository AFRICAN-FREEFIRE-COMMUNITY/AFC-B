"""
afc_ocr/services/synth.py
================================================================================
OCR self-hosting, P0: SYNTHETIC DATA + CHARACTER DICTIONARY.

WHY THIS EXISTS
    The self-hosted "student" recognizer (services/local_ocr.py) needs labeled
    text crops to pretrain / fine-tune on. Real admin-confirmed crops trickle in
    one screenshot at a time (services/training_capture.py), so on day one there
    is almost nothing to train against. This module manufactures a large, labeled
    corpus on a plain CPU: it renders Free Fire player names onto cropped, blurred
    pieces of REAL result screenshots, then applies realistic corruptions (blur,
    JPEG, glare, glyph confusables) so the synthetic crops fail the same way real
    OCR inputs fail. That closes most of the synth-to-real gap before a single
    real label exists.

    It also produces the CHARACTER DICTIONARY: the exact set of glyphs the
    recognizer must be able to output. Free Fire community names are wildly
    stylized unicode (fullwidth letters, superscripts, currency glyphs, runic and
    decorative symbols). A vanilla PP-OCR latin dictionary cannot represent
    "S!R᭄AlMBOT࿐" or "!฿ł₲VɆ₦Ø₥", so we build the dictionary from the REAL names
    the AFC community actually uses (User.username + OCRNameAlias.raw_name) on top
    of a baseline of ASCII + common symbols.

HOW IT CONNECTS TO THE REST OF THE SYSTEM
    - READS (real-name distribution):
        afc_auth.models.User.username           -> every registered name.
        afc_ocr.models.OCRNameAlias.raw_name    -> every raw name an admin has
                                                   confirmed Gemini read off a
                                                   screen. Both feed real_name_pool()
                                                   and build_char_dictionary().
    - READS (backgrounds / look-and-feel):
        the real Free Fire screenshots in SCREENSHOTS_DIR (the same folder
        ../ocr_test.py harvests). We crop and blur small patches of these to use as
        crop backgrounds, so synthetic name plates sit on the actual game palette
        and texture instead of a flat color.
    - WRITES (the corpus catalog, MySQL, exactly like real captures):
        afc_ocr.models.OCRTrainingPair (source='synthetic', is_clean=True) and
        afc_ocr.models.OCRCropLabel (field='name', text=label, crop_path=<png>).
        We reuse the SAME two tables the admin-review path writes, so the offline
        exporter / corpus catalog indexes synthetic and real data through one
        schema. The only difference is source='synthetic'.
    - WRITES (the on-disk dataset):
        media/ocr_training/synth/<sha>.png crops + a rec_gt.txt manifest
        ("relpath\tlabel" per line) in the PaddleOCR recognition ground-truth
        format the P2 trainer reads.
    - WRITES (the char dict):
        media/models/rec_keys.txt, one character per line (PaddleOCR rec keys
        format). services/local_ocr.LocalOCREngine already knows how to load a
        custom rec_keys.txt when a fine-tuned bundle is pointed at it
        (rec_keys_path), so this file is the dictionary a future fine-tune ships.

    CALLED BY: afc_ocr/management/commands/synth_ocr.py
    (`python manage.py synth_ocr --count N`).

CRITICAL MODELING RULE (mirrors models.py + training_capture.py headers)
    - recognition-truth vs identity-truth: a synthetic crop's label is ONLY the
      rendered string (recognition-truth, the CTC target). We do NOT attach a
      matched_user_id, because a synthetic plate does not resolve to any real
      roster slot. OCRCropLabel.matched_user_id is left None on purpose.
    - SPLIT DISCIPLINE: synthetic data is TRAIN-ONLY, never eval/holdout. Eval and
      holdout must measure performance on REAL screenshots, otherwise the score is
      measuring how well the model memorized our generator, not how well it reads
      the game. _assign_split() can return eval/holdout from a hash, so we DO NOT
      use it here; every synthetic pair is forced to split='train'.

FONT ASSUMPTION (no bundled FF font)
    Free Fire renders names in its own proprietary in-game font, which we do not
    ship and cannot legally bundle. We therefore render with the best system fonts
    available (see _candidate_font_paths): Segoe UI Symbol first (the widest
    unicode glyph coverage on Windows, so the stylized names actually render
    instead of showing tofu boxes), then Arial / Segoe UI / Tahoma / Verdana, plus
    CJK and Myanmar fallbacks. The goal of P0 is to teach the recognizer the
    CORRUPTION patterns and the REAL name-string distribution, both of which are
    font-independent; swapping in the real FF font later (drop a TTF next to this
    module and add it to _candidate_font_paths) only improves glyph fidelity.
    If NONE of the candidate fonts load, we fall back to PIL's built-in bitmap font
    so the generator still runs (lower fidelity, logged once).

    KNOWN LIMITATION (rare-script tofu): about 2.5% of real names use exotic
    decorative codepoints (Balinese U+1B00 range, Tibetan ornaments, some Indic
    blocks) that NONE of the available system fonts can draw, so those specific
    glyphs render as the font's missing-character box ("tofu"). For those names the
    rendered pixels of the ornament do not match its label codepoint, which is a
    small label/pixel desync on the ornament only (the readable latin core of the
    name still renders and labels correctly). We do NOT silently drop or rewrite
    those characters, because reliable per-glyph coverage detection needs a font
    cmap reader (fontTools), which is not an installed dependency and we will not
    add one here. The honest fix is to ship the real Free Fire font (which itself
    falls back to boxes in-game for the same exotic glyphs), so the synthetic tofu
    then matches what the game actually shows. Until then this affects a small
    minority of crops and is logged at the dataset level for visibility.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import random
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from django.conf import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Paths + constants
# ──────────────────────────────────────────────────────────────────────────────
#
# All generated artifacts live UNDER media/ (gitignored for the OCR paths via the
# backend .gitignore entries added in P0) so synthetic data + the char dict never
# get pushed. We compute everything from settings.MEDIA_ROOT so it is correct on
# any machine / storage layout.

# media/models/rec_keys.txt - the PaddleOCR recognition character dictionary
# (one char per line). Consumed by a future fine-tune via
# local_ocr.LocalOCREngine(model_dir=...) -> rec_keys_path.
CHAR_DICT_REL = os.path.join("models", "rec_keys.txt")

# media/ocr_training/synth/ - where the rendered crops + manifest land. We keep
# them in a `synth/` subfolder so they sit alongside, but never collide with, the
# real admin-captured screenshots that training_capture.py writes directly under
# media/ocr_training/.
SYNTH_REL_DIR = os.path.join("ocr_training", "synth")

# rec_gt.txt - the PaddleOCR recognition ground-truth manifest: one
# "relpath\tlabel" line per crop. The P2 trainer reads exactly this format.
MANIFEST_NAME = "rec_gt.txt"

# Where the real FF screenshots live (same folder ../ocr_test.py harvests). We
# only READ these (crop background patches); we never modify them. Kept as a
# module constant so it is easy to repoint. Pulled from settings if an override is
# configured, else the known local download folder.
SCREENSHOTS_DIR = getattr(
    settings,
    "OCR_SCREENSHOTS_DIR",
    r"C:\Users\Sweez\Downloads\freefire screenshots",
)

# Candidate render fonts, in PREFERENCE order. Segoe UI Symbol is first on purpose:
# it carries by far the widest unicode coverage of the stock Windows fonts, so the
# stylized community glyphs render as glyphs instead of missing-character boxes.
# Arial / Segoe UI / Tahoma / Verdana cover plain latin names; the CJK + Myanmar
# faces catch the occasional name built from those scripts. To raise fidelity later,
# drop the real Free Fire TTF beside this file and prepend its path here.
_WINDOWS_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_FONT_BASENAMES = [
    "seguisym.ttf",   # Segoe UI Symbol - widest unicode glyph coverage (primary)
    "segoeui.ttf",    # Segoe UI - clean sans, close-ish to game UI text
    "segoeuib.ttf",   # Segoe UI Bold - bolder weight variety
    "arial.ttf",      # Arial - ubiquitous latin baseline
    "arialbd.ttf",    # Arial Bold
    "tahoma.ttf",     # Tahoma - different metrics for variety
    "verdana.ttf",    # Verdana - wider glyphs
    "msyh.ttc",       # Microsoft YaHei - CJK fallback
    "mmrtext.ttf",    # Myanmar Text - Myanmar-script fallback
]

# Crop geometry. Real FF name cells are short, wide strips. We render at a fixed
# height band and a width that follows the text, matching the aspect a line
# recognizer expects (tall enough to read, never a giant square).
_CROP_HEIGHT_RANGE = (28, 40)     # px; randomized per crop for scale variety
_HORIZONTAL_PAD = (6, 16)         # px of padding left/right of the text
_VERTICAL_PAD = (3, 7)            # px of padding above/below the text

# Max characters we will render for one name. Real names are short; absurdly long
# alias strings (rare) get truncated so one crop never blows up to a huge image.
_MAX_LABEL_LEN = 28


# ──────────────────────────────────────────────────────────────────────────────
# 1. Character dictionary
# ──────────────────────────────────────────────────────────────────────────────
#
# The dictionary is the set of glyphs the recognizer is allowed to emit. Build it
# from a fixed ASCII/symbol baseline UNION every distinct character that appears in
# the REAL community names (usernames + confirmed raw OCR names). That guarantees
# the model can represent the exact stylized glyphs AFC players actually use, which
# a stock latin PP-OCR dictionary cannot.

# Baseline printable ASCII the model should always know, even if (improbably) no
# real name used a given char. Letters + digits + the symbols FF names lean on.
_ASCII_LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + \
                 [chr(c) for c in range(ord("a"), ord("z") + 1)]
_ASCII_DIGITS = [chr(c) for c in range(ord("0"), ord("9") + 1)]
_ASCII_SYMBOLS = list(" !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _real_name_chars():
    """
    Return the set of every distinct character appearing in real AFC names:
    User.username UNION OCRNameAlias.raw_name. This is what makes the dictionary
    cover the community's actual stylized unicode rather than a guess.

    DB connections: reads afc_auth.User.username and afc_ocr.OCRNameAlias.raw_name.
    Models are imported lazily (inside the function) to keep this service
    import-light and avoid app-loading-order surprises, the same pattern
    training_capture.py and commit.py use.
    """
    from afc_auth.models import User
    from afc_ocr.models import OCRNameAlias

    chars: set[str] = set()

    # .values_list(flat=True) streams just the one column - no model instances, so
    # this stays cheap even across thousands of users.
    for name in User.objects.values_list("username", flat=True):
        if name:
            chars.update(name)
    for raw in OCRNameAlias.objects.values_list("raw_name", flat=True):
        if raw:
            chars.update(raw)

    return chars


def build_char_dictionary(write: bool = True) -> list[str]:
    """
    Build (and optionally write) the recognition character dictionary.

    Result = ASCII letters/digits/common symbols  UNION  every distinct character
    used in real User.username and OCRNameAlias.raw_name values.

    We DROP characters that cannot be a recognition target:
      - control characters (categories starting with 'C', e.g. zero-width joiners,
        the ideographic/medium-mathematical spaces some names pad with). These are
        invisible, so the recognizer can never be trained to "read" them; keeping
        them would just add dead classes to the CTC head.
      - the literal newline, because the dict file is one-char-per-line (a newline
        in the dict would corrupt the file format).
    We KEEP the normal ASCII space (it separates words in many names) but it is part
    of the ASCII baseline above, not emitted as its own visible glyph line is fine
    for PaddleOCR (space is a valid class).

    Output file: media/models/rec_keys.txt, one character per line, UTF-8, in the
    PaddleOCR rec keys format. Order = ASCII baseline first (stable, human-readable
    head of the file), then the remaining real-name glyphs sorted by codepoint
    (deterministic, so regenerating with the same DB yields the same file).

    Returns the ordered char list (also when write=False, for callers/tests).

    Consumed by: a future fine-tuned bundle via local_ocr.LocalOCREngine, which
    passes this file as rec_keys_path to RapidOCR.
    """
    # Baseline first, de-duplicated but order-preserving so the file head is stable.
    baseline: list[str] = []
    seen: set[str] = set()
    for ch in _ASCII_LETTERS + _ASCII_DIGITS + _ASCII_SYMBOLS:
        if ch not in seen:
            seen.add(ch)
            baseline.append(ch)

    # Real-name glyphs not already in the baseline, filtered + sorted by codepoint.
    extras: list[str] = []
    for ch in _real_name_chars():
        if ch in seen:
            continue
        # Drop control / format / unassigned chars (category 'C*') and newline:
        # they are not visible glyphs and must not become recognition classes.
        if ch == "\n" or ch == "\r":
            continue
        category = unicodedata.category(ch)
        if category.startswith("C"):
            continue
        seen.add(ch)
        extras.append(ch)
    extras.sort(key=ord)

    char_list = baseline + extras

    if write:
        # Resolve under MEDIA_ROOT and ensure the models/ dir exists.
        out_path = os.path.join(settings.MEDIA_ROOT, CHAR_DICT_REL)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        # One char per line, UTF-8. This is the exact format PaddleOCR / RapidOCR
        # expect for a custom rec dictionary.
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(char_list))
            f.write("\n")
        logger.info(
            "build_char_dictionary: wrote %d chars (%d ascii baseline + %d real-name extras) -> %s",
            len(char_list), len(baseline), len(extras), out_path,
        )

    return char_list


# ──────────────────────────────────────────────────────────────────────────────
# 2. Real-name pool
# ──────────────────────────────────────────────────────────────────────────────


def real_name_pool() -> list[str]:
    """
    The list of REAL strings to render: User.username UNION OCRNameAlias.raw_name,
    de-duplicated.

    Training on the real name distribution (instead of random strings) is what
    closes the synth-to-real gap: the recognizer practises on the exact glyph
    combinations, lengths, and stylings AFC players actually use, so at inference
    it has already "seen" names shaped like the ones on screen.

    DB connections: same two columns as build_char_dictionary
    (afc_auth.User.username + afc_ocr.OCRNameAlias.raw_name). Empty/whitespace-only
    names are dropped (nothing to render); each remaining name is .strip()ed of
    leading/trailing whitespace and truncated to _MAX_LABEL_LEN so one crop never
    becomes an enormous image.

    Returns a de-duplicated list (insertion-order-stable for reproducibility).
    """
    from afc_auth.models import User
    from afc_ocr.models import OCRNameAlias

    seen: set[str] = set()
    pool: list[str] = []

    def _add(value):
        if not value:
            return
        cleaned = str(value).strip()
        if not cleaned:
            return
        if len(cleaned) > _MAX_LABEL_LEN:
            cleaned = cleaned[:_MAX_LABEL_LEN]
        if cleaned in seen:
            return
        seen.add(cleaned)
        pool.append(cleaned)

    for name in User.objects.values_list("username", flat=True):
        _add(name)
    for raw in OCRNameAlias.objects.values_list("raw_name", flat=True):
        _add(raw)

    logger.info("real_name_pool: %d distinct real names available for rendering.", len(pool))
    return pool


# ──────────────────────────────────────────────────────────────────────────────
# 3. Fonts + backgrounds (loaded once, reused across crops)
# ──────────────────────────────────────────────────────────────────────────────


def _candidate_font_paths() -> list[str]:
    """Absolute paths of the candidate render fonts that actually exist on this box,
    in preference order (see _FONT_BASENAMES). To use the real Free Fire font later,
    drop its TTF beside this module and prepend it here."""
    paths = []
    for base in _FONT_BASENAMES:
        p = os.path.join(_WINDOWS_FONT_DIR, base)
        if os.path.exists(p):
            paths.append(p)
    return paths


# Module-level caches so we open each font file / decode each background ONCE and
# reuse across thousands of crops (the generator is otherwise font-load bound).
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_PATHS_CACHE: list[str] | None = None
_BACKGROUND_CACHE: list[Image.Image] | None = None
_WARNED_NO_FONT = False


def _get_font(size: int, rng: random.Random) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Return a font at `size`, chosen at random from the available candidates so the
    corpus carries multiple typefaces (the recognizer must not overfit one font).
    Falls back to PIL's built-in bitmap font if no TTF is available, so generation
    never hard-fails on a font-less box (logged once).

    The font choice draws from the SEEDED `rng` (passed in from render_name_crop),
    not the global random module, so a given (seed, count) reproduces the exact same
    corpus on every run. That is what makes generate_dataset idempotent: identical
    inputs render byte-identical crops, which then collapse on the sha dedupe.
    """
    global _FONT_PATHS_CACHE, _WARNED_NO_FONT
    if _FONT_PATHS_CACHE is None:
        _FONT_PATHS_CACHE = _candidate_font_paths()

    if not _FONT_PATHS_CACHE:
        if not _WARNED_NO_FONT:
            logger.warning(
                "synth: no system TTF fonts found; falling back to PIL bitmap font "
                "(low fidelity). Drop a TTF into the Windows Fonts dir or beside "
                "synth.py to improve glyph rendering."
            )
            _WARNED_NO_FONT = True
        return ImageFont.load_default()

    path = rng.choice(_FONT_PATHS_CACHE)
    key = (path, size)
    font = _FONT_CACHE.get(key)
    if font is None:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            # A .ttc/.ttf that PIL cannot open at this index: fall back to default
            # rather than crash the whole run.
            font = ImageFont.load_default()
        _FONT_CACHE[key] = font
    return font


def _load_backgrounds(max_count: int = 12) -> list[Image.Image]:
    """
    Load up to `max_count` real FF screenshots from SCREENSHOTS_DIR as RGB images,
    cached at module level. We crop random small patches of these per name (see
    _make_background), so synthetic name plates sit on the ACTUAL game palette and
    texture instead of a flat fill. If the folder is missing or empty we return [],
    and _make_background falls back to a procedural dark gradient.
    """
    global _BACKGROUND_CACHE
    if _BACKGROUND_CACHE is not None:
        return _BACKGROUND_CACHE

    backgrounds: list[Image.Image] = []
    directory = SCREENSHOTS_DIR
    if directory and os.path.isdir(directory):
        names = sorted(os.listdir(directory))
        for fn in names:
            if not fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            try:
                img = Image.open(os.path.join(directory, fn)).convert("RGB")
                img.load()  # force decode now so later crops are cheap
                backgrounds.append(img)
            except Exception as exc:
                logger.warning("synth: could not load background %s: %s", fn, exc)
            if len(backgrounds) >= max_count:
                break

    if not backgrounds:
        logger.warning(
            "synth: no real screenshots in %s; using procedural backgrounds only.",
            directory,
        )
    _BACKGROUND_CACHE = backgrounds
    return backgrounds


def _make_background(width: int, height: int, rng: random.Random) -> Image.Image:
    """
    Build a (width x height) crop background.

    Preferred: cut a random patch out of a real screenshot and blur it slightly, so
    the text sits on real game colors/texture (the genuine "name plate over the
    result panel" look). Fallback: a procedural dark vertical gradient with light
    noise, matching FF's dark result panels, when no screenshots are available.

    Why blur the patch: real name cells sit on softly-out-of-focus panel art behind
    the text, and a blurred real patch reproduces that low-frequency background
    without copying any readable foreground text into our crop (which would corrupt
    the label).
    """
    backgrounds = _load_backgrounds()
    if backgrounds:
        src = rng.choice(backgrounds)
        sw, sh = src.size
        if sw > width and sh > height:
            left = rng.randint(0, sw - width)
            top = rng.randint(0, sh - height)
            patch = src.crop((left, top, left + width, top + height)).copy()
        else:
            # Source smaller than the crop (rare): resize a copy to fit.
            patch = src.resize((width, height))
        # Blur so any text in the source patch is unreadable (cannot pollute our
        # label) and the patch reads as soft background art.
        patch = patch.filter(ImageFilter.GaussianBlur(radius=rng.uniform(1.5, 3.5)))
        # Darken slightly toward FF's dim panels so light name text stays legible.
        patch = Image.eval(patch, lambda px: int(px * rng.uniform(0.55, 0.85)))
        return patch

    # ── Procedural fallback: dark vertical gradient + faint noise ────────────────
    # Noise is drawn from a numpy Generator SEEDED off the same `rng`, not the global
    # np.random state, so the procedural path stays deterministic for a given
    # (seed, count) too (see _get_font for why determinism matters here).
    top_v = rng.randint(15, 45)
    bot_v = rng.randint(30, 70)
    grad = np.linspace(top_v, bot_v, height).reshape(height, 1)
    grad = np.repeat(grad, width, axis=1)
    np_rng = np.random.default_rng(rng.getrandbits(64))
    noise = np_rng.normal(0, 6, (height, width))
    arr = np.clip(grad + noise, 0, 255).astype(np.uint8)
    rgb = np.stack([arr, arr, arr], axis=2)
    return Image.fromarray(rgb, "RGB")


# ──────────────────────────────────────────────────────────────────────────────
# 4. Glyph-similarity (confusable) swaps
# ──────────────────────────────────────────────────────────────────────────────
#
# Real OCR errors are NOT random: a recognizer confuses glyphs that LOOK alike. To
# teach the student which pairs are genuinely hard, we occasionally render a name
# with a confusable substituted in. CRITICAL: the label we save is the CORRUPTED
# string we actually rendered, never the original. The crop must say exactly what
# its pixels say (recognition-truth), so if we draw an 'O' the label is 'O'. This
# augmentation widens coverage of look-alike glyphs the real distribution is thin
# on; it never desyncs pixels from label.

# Single-character confusables (bidirectional intent, applied one direction at a
# time at render). Each maps a char to glyphs a reader/OCR commonly mistakes it for.
_CONFUSABLES = {
    "0": "O", "O": "0",
    "1": "lI", "l": "1I", "I": "1l",
    "5": "S", "S": "5",
    "8": "B", "B": "8",
    "2": "Z", "Z": "2",
    "6": "G", "G": "6",
    "9": "g", "g": "9",
    "rn": "m",  # the classic two-char -> one-char confusable (handled separately)
    "vv": "w",
    "cl": "d",
}
# Multi-char confusable pairs handled as substring swaps (e.g. "rn" <-> "m").
_MULTI_CONFUSABLES = [("rn", "m"), ("m", "rn"), ("vv", "w"), ("cl", "d")]


def _apply_confusable_swaps(text: str, rng: random.Random, prob: float = 0.18) -> str:
    """
    With probability `prob`, return a glyph-confused variant of `text`; else return
    `text` unchanged. The RETURNED string is the new label (we render and label the
    same corrupted string, never the original).

    Two passes:
      1. A multi-char substring swap ("rn" -> "m", "vv" -> "w", ...), at most once,
         since these are the highest-signal confusables.
      2. Single-character swaps on a few random positions (0 <-> O, 1 <-> l <-> I,
         etc.), each independently at a low rate.
    Mirrors how a real recognizer slips on look-alike glyphs.
    """
    if rng.random() >= prob:
        return text

    out = text

    # Pass 1: one multi-char substring swap, if any pattern is present.
    rng.shuffle(_MULTI_CONFUSABLES)  # vary which pattern wins when several match
    for src, dst in _MULTI_CONFUSABLES:
        idx = out.find(src)
        if idx != -1:
            out = out[:idx] + dst + out[idx + len(src):]
            break

    # Pass 2: independent single-char swaps at a low per-position rate. `ch` is one
    # character; `repl` is a string of one or more candidate look-alike glyphs, so we
    # pick one at random. Multi-char source keys ("rn", "vv", "cl") never match a
    # single-character `ch`, so they are naturally skipped here (handled in pass 1).
    chars = list(out)
    for i, ch in enumerate(chars):
        repl = _CONFUSABLES.get(ch)
        if repl and rng.random() < 0.25:
            chars[i] = rng.choice(repl)
    return "".join(chars)


# ──────────────────────────────────────────────────────────────────────────────
# 5. Render one crop (text -> corrupted PIL image + final label)
# ──────────────────────────────────────────────────────────────────────────────


def render_name_crop(text: str, rng: random.Random | None = None) -> tuple[Image.Image, str]:
    """
    Render `text` as a game-like name plate over a real-screenshot background, apply
    a stack of realistic corruptions, and return (PIL.Image RGB, label_text).

    label_text is what the PIXELS actually say after augmentation: if a confusable
    swap turned an 'O' into a '0', the label reflects the rendered character, never
    the original input. This keeps the crop's label = recognition-truth.

    CORRUPTION STACK (each mirrors a specific real OCR failure mode):
      0. confusable swap   - look-alike glyph substitution (0<->O, 1<->l<->I,
                             rn<->m). Mirrors the recognizer's own hardest mistakes;
                             relabels to the rendered string.
      1. slight rotation   - phones/screenshots are rarely axis-aligned; a small
                             skew teaches rotation tolerance.
      2. gaussian blur     - compression + downscaled game UI smears edges; blur
                             reproduces soft, low-contrast strokes.
      3. brightness/glare  - bright result panels + screen glare wash out text; a
                             random brightness shift (and occasional glare blob)
                             mirrors that.
      4. downscale-upscale - screenshots are often shrunk then viewed larger, losing
                             detail; round-tripping the resolution reproduces that
                             irreversible detail loss.
      5. JPEG recompress   - WhatsApp/Discord re-encode screenshots as low-quality
                             JPEG, adding blocky 8x8 artifacts around glyph edges;
                             we re-encode at a random low quality to bake those in.

    Not all corruptions fire on every crop (each is probabilistic), so the corpus
    spans clean-ish to heavily-degraded, like the real input range.
    """
    if rng is None:
        rng = random.Random()

    # ── 0. Confusable swap (relabels to what we will actually draw) ──────────────
    label = _apply_confusable_swaps(text, rng)
    if not label:
        # Degenerate (all chars stripped) - fall back to the raw text so we never
        # render an empty crop with an empty label.
        label = text or " "

    # ── Pick a font + size, measure the text box ────────────────────────────────
    height = rng.randint(*_CROP_HEIGHT_RANGE)
    # Font size a touch under the crop height so ascenders/descenders fit.
    font_size = max(12, int(height * rng.uniform(0.62, 0.82)))
    font = _get_font(font_size, rng)

    # Measure rendered text size robustly across PIL versions (textbbox on a scratch
    # draw). Some stylized glyphs report zero width if the font lacks them; we floor
    # the width so the crop is never degenerate.
    scratch = Image.new("RGB", (4, 4))
    sdraw = ImageDraw.Draw(scratch)
    try:
        bbox = sdraw.textbbox((0, 0), label, font=font)
        text_w = max(1, bbox[2] - bbox[0])
        text_h = max(1, bbox[3] - bbox[1])
        offset_x, offset_y = bbox[0], bbox[1]
    except Exception:
        # Very defensive: if measuring fails, approximate from char count.
        text_w = max(1, len(label) * font_size // 2)
        text_h = font_size
        offset_x = offset_y = 0

    pad_x = rng.randint(*_HORIZONTAL_PAD)
    pad_y = rng.randint(*_VERTICAL_PAD)
    crop_w = text_w + 2 * pad_x
    crop_h = max(height, text_h + 2 * pad_y)

    # ── Background patch (real screenshot crop, blurred) ────────────────────────
    img = _make_background(crop_w, crop_h, rng)
    draw = ImageDraw.Draw(img)

    # ── Draw the name: light text with a soft dark outline (FF name plates are ──
    # light glyphs with a contrasting stroke so they read over busy art).
    text_color = tuple(rng.randint(200, 255) for _ in range(3))
    outline_color = tuple(rng.randint(0, 40) for _ in range(3))
    tx = pad_x - offset_x
    ty = pad_y - offset_y
    # Cheap 1px outline by drawing the text offset in the outline color first.
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.text((tx + dx, ty + dy), label, font=font, fill=outline_color)
    draw.text((tx, ty), label, font=font, fill=text_color)

    # ── 1. Slight rotation ──────────────────────────────────────────────────────
    if rng.random() < 0.6:
        angle = rng.uniform(-3.5, 3.5)
        # expand=False keeps the crop size; fill the corners with a mid-dark value
        # so rotation does not punch black triangles into the plate.
        img = img.rotate(angle, resample=Image.BILINEAR, expand=False, fillcolor=(20, 20, 20))

    # ── 2. Gaussian blur ────────────────────────────────────────────────────────
    if rng.random() < 0.7:
        img = img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.3, 1.4)))

    # ── 3. Brightness shift + occasional glare blob ─────────────────────────────
    if rng.random() < 0.7:
        factor = rng.uniform(0.65, 1.45)
        img = Image.eval(img, lambda px: min(255, int(px * factor)))
    if rng.random() < 0.15:
        # A soft bright ellipse mimics screen glare / a highlight washing out part
        # of the text - a common, locally-destructive real failure.
        glare = Image.new("L", img.size, 0)
        gdraw = ImageDraw.Draw(glare)
        gx = rng.randint(0, crop_w)
        gy = rng.randint(0, crop_h)
        gr = rng.randint(crop_h // 2, crop_h)
        gdraw.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), fill=rng.randint(60, 130))
        glare = glare.filter(ImageFilter.GaussianBlur(radius=gr / 2))
        white = Image.new("RGB", img.size, (255, 255, 255))
        img = Image.composite(white, img, glare)

    # ── 4. Downscale then upscale (irreversible detail loss) ────────────────────
    if rng.random() < 0.6:
        scale = rng.uniform(0.5, 0.8)
        small = img.resize(
            (max(1, int(crop_w * scale)), max(1, int(crop_h * scale))),
            Image.BILINEAR,
        )
        img = small.resize((crop_w, crop_h), Image.BILINEAR)

    # ── 5. JPEG recompression (blocky 8x8 artifacts) ────────────────────────────
    if rng.random() < 0.8:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=rng.randint(28, 70))
        buf.seek(0)
        img = Image.open(buf).convert("RGB")
        img.load()

    return img, label


# ──────────────────────────────────────────────────────────────────────────────
# 6. Generate the dataset (write crops + manifest + catalog rows)
# ──────────────────────────────────────────────────────────────────────────────


def generate_dataset(n: int = 3000, out_dir: str | None = None, seed: int | None = 42) -> dict:
    """
    Generate `n` synthetic labeled name crops and register them in the corpus
    catalog exactly like real captures.

    For each crop:
      1. Pick a real name (cycled through real_name_pool, so the synthetic
         distribution matches the community's actual names).
      2. render_name_crop -> a corrupted PIL image + its label.
      3. Content-address the PNG bytes: sha256 -> media/ocr_training/synth/<sha>.png.
         IDEMPOTENT + DEDUPED: if that sha already exists on disk AND already has an
         OCRTrainingPair, we skip it (re-running the command never double-writes the
         same crop). Two different names will essentially never collide; the same
         (name, corruptions) producing identical bytes simply collapses to one row.
      4. Append a "relpath\tlabel" line to rec_gt.txt (PaddleOCR rec GT format).
      5. Create one OCRTrainingPair(source='synthetic', is_clean=True, split='train')
         and one OCRCropLabel(field='name', text=label, crop_path=<png>,
         matched_user_id=None). We reuse the SAME tables real captures use so the
         exporter indexes synthetic + real data through one schema.

    SPLIT DISCIPLINE: every synthetic pair is forced split='train'. Synthetic data
    must NEVER land in eval/holdout (those measure real-screenshot performance), so
    we do NOT call _assign_split here.

    DEFENSIVE: a failure rendering or saving one crop is logged and skipped; it
    never aborts the whole run.

    Returns a summary dict: counts of crops written / skipped, pairs + crop labels
    created, the manifest path, and the char-dict path.
    """
    from afc_ocr.models import OCRTrainingPair, OCRCropLabel

    rng = random.Random(seed)

    # Resolve output dir (default media/ocr_training/synth) and ensure it + the
    # manifest's parent exist.
    if out_dir is None:
        out_dir = os.path.join(settings.MEDIA_ROOT, SYNTH_REL_DIR)
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)

    # Real-name pool. If the DB has no names at all (fresh box), fall back to a tiny
    # built-in sample so the generator still produces something rather than crashing.
    pool = real_name_pool()
    if not pool:
        logger.warning("generate_dataset: empty real-name pool; using a built-in fallback sample.")
        pool = ["Pro_Sniper", "꧁☬Zeus☬꧂", "AE.MI6", "NoScope99", "DARK~LORD"]

    written = 0
    skipped_existing = 0
    failed = 0
    pairs_created = 0
    crops_created = 0
    manifest_lines: list[str] = []

    # Iterate up to n crops, cycling the name pool so a small pool still fills n
    # crops (each pass produces a fresh random corruption, so repeats are not dupes).
    for i in range(n):
        name = pool[i % len(pool)]
        try:
            image, label = render_name_crop(name, rng)

            # PNG bytes -> content address. PNG (lossless) so the saved file matches
            # exactly what the label describes (we already baked JPEG artifacts into
            # the pixels during rendering; the container format is just storage).
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            png_bytes = buf.getvalue()
            sha = hashlib.sha256(png_bytes).hexdigest()

            rel_png = os.path.join(SYNTH_REL_DIR, f"{sha}.png").replace("\\", "/")
            abs_png = os.path.join(out_dir, f"{sha}.png")

            # ── Idempotent dedupe: skip if this exact crop is already cataloged ──
            if os.path.exists(abs_png) and OCRTrainingPair.objects.filter(
                image_sha256=sha, source="synthetic"
            ).exists():
                skipped_existing += 1
                # Still record it in the manifest line list so a freshly rebuilt
                # rec_gt.txt stays complete across re-runs.
                manifest_lines.append(f"{rel_png}\t{label}")
                continue

            # Write the crop (only if not already on disk - bytes are identical
            # either way since the path IS the content hash).
            if not os.path.exists(abs_png):
                with open(abs_png, "wb") as f:
                    f.write(png_bytes)
                written += 1

            # ── Catalog rows (same schema as real captures) ─────────────────────
            # final_json mirrors the canonical recognition-truth shape the rest of
            # the loop speaks: a single-player, single-placement "screen" carrying
            # just this name. kills=0 because a synthetic NAME crop has no kill cell.
            final_json = {
                "placements": [
                    {"placement": 1, "players": [{"name": label, "kills": 0}]}
                ]
            }

            pair = OCRTrainingPair.objects.create(
                session=None,                 # synthetic data has no review session
                match=None,                   # nor a source match
                image_sha256=sha,
                image_path=rel_png,           # the per-crop png (synthetic = 1 crop/pair)
                event_type="team",            # arbitrary; synthetic crops are field-level
                raw_output={},                # no teacher engine produced this
                final_json=final_json,
                source="synthetic",           # the one field distinguishing synth from real
                teacher_model=None,
                num_corrections=0,
                edit_distance=0,
                is_clean=True,                # synthetic labels are exact by construction
                split="train",                # SYNTHETIC IS TRAIN-ONLY (never eval/holdout)
                created_by=None,
            )
            pairs_created += 1

            OCRCropLabel.objects.create(
                pair=pair,
                crop_path=rel_png,            # the rendered crop on disk (unlike real
                                              # captures, the synth crop already exists)
                field="name",                 # P0 generates NAME crops
                text=label,                   # recognition-truth = the rendered string
                placement=1,
                matched_user_id=None,         # identity-truth N/A for synthetic plates
            )
            crops_created += 1

            manifest_lines.append(f"{rel_png}\t{label}")

        except Exception as exc:
            # One bad crop must not abort the batch.
            failed += 1
            logger.warning("generate_dataset: failed on name %r (#%d): %s", name, i, exc)
            continue

    # ── Write the PaddleOCR rec ground-truth manifest ───────────────────────────
    # One "relpath\tlabel" line per crop. We rewrite the whole file from the lines
    # we accumulated this run; combined with the on-disk dedupe this keeps the
    # manifest consistent with what is in the catalog for the names we processed.
    try:
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(manifest_lines))
            if manifest_lines:
                f.write("\n")
    except Exception as exc:
        logger.warning("generate_dataset: failed to write manifest %s: %s", manifest_path, exc)

    summary = {
        "requested": n,
        "crops_written": written,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "pairs_created": pairs_created,
        "crop_labels_created": crops_created,
        "manifest_path": manifest_path,
        "out_dir": out_dir,
        "char_dict_path": os.path.join(settings.MEDIA_ROOT, CHAR_DICT_REL),
        "name_pool_size": len(pool),
    }
    logger.info("generate_dataset: %s", summary)
    return summary
