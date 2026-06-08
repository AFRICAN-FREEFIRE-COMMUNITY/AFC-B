"""
afc_ocr/services/dataset.py
================================================================================
OCR learning loop, Phase 2 (P2): the DATASET ASSEMBLER.

WHY THIS EXISTS
    Phase 1 (services/training_capture.py) durably captures one OCRTrainingPair +
    its OCRCropLabel rows every time an admin commits a reviewed OCR session. Those
    rows are the raw, content-addressed truth. They are NOT yet a trainable dataset.

    This module turns those DB rows into the on-disk format PaddleOCR's recognition
    trainer eats: a `rec_gt.txt` label file (one line per crop = "crop_relpath\\ttext"),
    the cropped cell images sitting next to it, and a character dictionary listing
    every glyph the model must be able to emit. The actual fine-tune runs OFF-BOX on
    a GPU (see afc_ocr/training/finetune_ppocrv5.md); this assembler is the thing that
    produces the tarball that gets shipped to that GPU box.

HOW IT CONNECTS TO THE REST OF THE SYSTEM
    Upstream (what it reads):
      - afc_ocr.models.OCRCropLabel  -> the per-cell recognition-truth (`text`) the
        recognizer learns to reproduce. `crop_path` points at the cropped cell image
        (filled by the offline layout cropper; rows with an empty crop_path are
        skipped here because there is no image to learn from yet).
      - afc_ocr.models.OCRTrainingPair -> the parent screenshot. We only include crops
        whose parent pair is `is_clean` (zero admin corrections == high-precision
        truth) and whose `split` is in the requested `splits`.
      - afc_ocr.services.synth.build_char_dictionary (OPTIONAL) -> if the synthetic
        data generator ships a canonical char-dictionary builder we reuse it so the
        synthetic crops and the real crops share ONE dictionary. If synth.py is not
        present yet (another agent owns it) we derive the dictionary from the labels.

    Downstream (who reads the output):
      - afc_ocr/training/finetune.py (run off-box) loads rec_gt.txt + crops + dict and
        fine-tunes PP-OCRv5_mobile_rec, then exports the student bundle.
      - afc_ocr.services.eval_gate consumes the FROZEN gold eval/holdout slice this
        assembler emits to decide whether the new student ships.
      - afc_ocr.services.local_ocr.LocalOCREngine loads the resulting <model_dir>/
        rec.onnx + rec_keys.txt + VERSION at inference time. The dictionary written
        here (rec_keys.txt) MUST match the one baked into that ONNX, which is exactly
        why the assembler and the trainer share this single derivation.

DATA-HYGIENE RULES (baked in on purpose, see assemble_rec_dataset):
    - SILVER (source='gemini_autolabel') and SYNTHETIC (source='synthetic') labels are
      teacher/generated truth, not human-confirmed. They may only ever land in the
      TRAIN split. They must NEVER contaminate eval/holdout, or the eval score is
      measuring the teacher's mistakes instead of ground truth.
    - GOLD (source='admin_review') is human-confirmed. It is the ONLY source allowed to
      provide eval/holdout. (Gold also trains, in the train split, like any pair.)
    - The split itself (train/eval/holdout) is decided ONCE, deterministically, from the
      screenshot's content hash at capture time (models._assign_split). We never
      re-bucket here; we only FILTER by it. That guarantees a screenshot can never leak
      from train into eval across dataset versions.

EVERYTHING THIS MODULE WRITES IS LOCAL + GITIGNORED. The output lives under
MEDIA_ROOT/ocr_training/ (media/ is not committed) and the off-box training artifacts
never get pushed. Nothing here touches prod directly: the off-box flow reads from a
read-only prod-DB clone (see the project prod-DB-clone memory note).

PUBLIC API
    assemble_rec_dataset(out_dir, splits=('train',), include_synthetic=True) -> manifest
    freeze_manifest(dataset_version, pairs) -> path to the appended manifest_v<N>.jsonl
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil

from django.conf import settings
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants / conventions (mirror training_capture.py so the two services agree on
# where training data lives and which source strings mean what).
# ──────────────────────────────────────────────────────────────────────────────

# Source strings as defined on OCRTrainingPair.SOURCE_CHOICES. Centralised here so
# the data-hygiene rules below read as English, not magic strings.
SOURCE_GOLD = "admin_review"        # human-confirmed -> may provide eval/holdout
SOURCE_SILVER = "gemini_autolabel"  # teacher-labeled  -> TRAIN only
SOURCE_SYNTHETIC = "synthetic"      # generated         -> TRAIN only

# Sources that are allowed ONLY in the train split, never in eval/holdout. Used as the
# guard in _crop_allowed_in_split.
TRAIN_ONLY_SOURCES = frozenset({SOURCE_SILVER, SOURCE_SYNTHETIC})

# Where frozen, append-only manifests live (relative to MEDIA_ROOT). Each manifest is
# an immutable record of exactly which pairs went into one dataset_version, so a model
# can always be traced back to its training data.
MANIFEST_DIR = os.path.join(settings.MEDIA_ROOT, "ocr_training", "manifests")

# Standard PaddleOCR recognition label filename. The trainer's config points its
# `label_file_list` at this.
REC_GT_FILENAME = "rec_gt.txt"
# Standard char-dictionary filename. The trainer's config `character_dict_path` points
# here, and local_ocr expects the SAME content as rec_keys.txt in the shipped bundle.
CHAR_DICT_FILENAME = "rec_keys.txt"
# Subdirectory (inside out_dir) that holds the copied crop images. rec_gt.txt lines are
# relative to out_dir, e.g. "crops/<sha>.jpg\\ttext".
CROPS_SUBDIR = "crops"


# ──────────────────────────────────────────────────────────────────────────────
# Char dictionary
# ──────────────────────────────────────────────────────────────────────────────

def _build_char_dictionary(texts: list[str]) -> list[str]:
    """
    Produce the ordered list of characters the recognizer must be able to emit.

    Preference order, to keep ONE canonical dictionary across real + synthetic data:
      1. afc_ocr.services.synth.build_char_dictionary if that module/function exists
         (the synthetic generator owns the canonical alphabet). Imported defensively
         because synth.py is built by another agent and may be absent.
      2. Derive from the labels: the sorted set of every character that appears in any
         included `text`. Deterministic (sorted) so the same labels always yield the
         same dictionary, which keeps the ONNX vocab stable across dataset versions.

    NOTE: we return the bare character list (one glyph per entry). PaddleOCR adds the
    CTC blank itself and, when use_space_char=True, appends the space token; so we do
    NOT inject a blank here. We DO strip the literal space from the derived set because
    PaddleOCR represents space via use_space_char, not as a dictionary line (a blank
    line in rec_keys.txt would be ambiguous).
    """
    # 1. Reuse the synthetic generator's canonical builder if present.
    try:
        from afc_ocr.services.synth import build_char_dictionary  # type: ignore
        chars = list(build_char_dictionary(texts))
        if chars:
            logger.info("dataset: using synth.build_char_dictionary (%d chars).", len(chars))
            return chars
    except Exception:
        # synth.py not present yet, or it raised. Fall through to label-derived dict.
        # This is expected during P2 bring-up and is NOT an error.
        pass

    # 2. Derive from the labels. Sorted set of all non-space characters seen.
    seen: set[str] = set()
    for t in texts:
        for ch in (t or ""):
            if ch != " ":  # space is handled by use_space_char, not a dict line
                seen.add(ch)
    chars = sorted(seen)
    logger.info("dataset: derived char dictionary from labels (%d chars).", len(chars))
    return chars


# ──────────────────────────────────────────────────────────────────────────────
# Split / source gating
# ──────────────────────────────────────────────────────────────────────────────

def _crop_allowed_in_split(source: str, split: str) -> bool:
    """
    Enforce the data-hygiene rule: silver + synthetic may ONLY be used in the train
    split; gold (admin_review) may be used in any split.

    Returns True if a crop from a pair with this `source` is allowed to be emitted into
    this `split`, False if including it would contaminate an eval/holdout set with
    non-human-confirmed truth.
    """
    if source in TRAIN_ONLY_SOURCES and split != "train":
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Crop image resolution
# ──────────────────────────────────────────────────────────────────────────────

def _copy_crop(crop_path: str, dest_dir: str) -> str | None:
    """
    Copy one crop image (referenced by OCRCropLabel.crop_path, a path relative to the
    Django storage backend) into dest_dir, and return its NEW filename (relative to the
    dataset out_dir's crops/ subdir), or None if the source is missing/unreadable.

    We content-name the destination by hashing the crop_path string so two labels that
    point at the same crop file collapse to one copy and the rec_gt line is stable. We
    go through default_storage to read (works on local disk AND on S3 in prod), then
    write a plain local file (the trainer reads local files).
    """
    if not crop_path:
        # crop_path is '' until the offline layout cropper fills it. No image -> nothing
        # to train on for this label yet; the caller skips it.
        return None

    if not default_storage.exists(crop_path):
        logger.warning("dataset: crop image missing in storage: %s", crop_path)
        return None

    # Preserve the original extension (cosmetic; trainer reads bytes), default .jpg.
    _, ext = os.path.splitext(crop_path)
    ext = (ext or ".jpg").lower()
    # Stable destination name = hash of the source path -> dedupes repeats.
    name = hashlib.sha256(crop_path.encode("utf-8")).hexdigest()[:24] + ext
    dest_rel = f"{CROPS_SUBDIR}/{name}"
    dest_abs = os.path.join(dest_dir, name)

    if not os.path.exists(dest_abs):
        try:
            with default_storage.open(crop_path, "rb") as src:
                data = src.read()
            with open(dest_abs, "wb") as dst:
                dst.write(data)
        except Exception as exc:
            logger.warning("dataset: failed to copy crop %s: %s", crop_path, exc)
            return None
    return dest_rel


# ──────────────────────────────────────────────────────────────────────────────
# Dataset version tag
# ──────────────────────────────────────────────────────────────────────────────

def _dataset_version_tag(pair_ids: list[str]) -> str:
    """
    Content hash of the included pair_ids -> a short, stable dataset_version tag.

    Two assembles that pull the EXACT same set of pairs get the same tag (reproducible);
    adding/removing a single pair changes it. We sort first so ordering never affects the
    tag. Returns 'v_empty' for an empty set so callers always get a usable string.
    """
    if not pair_ids:
        return "v_empty"
    joined = "\n".join(sorted(str(p) for p in pair_ids))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
    return f"v_{digest}"


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC: assemble the recognition dataset
# ──────────────────────────────────────────────────────────────────────────────

def assemble_rec_dataset(
    out_dir: str,
    splits: tuple[str, ...] = ("train",),
    include_synthetic: bool = True,
) -> dict:
    """
    Assemble a PaddleOCR-recognition dataset on disk from the captured crop labels.

    What it does, step by step:
      1. Select OCRCropLabel rows whose parent OCRTrainingPair.is_clean is True and
         whose pair.split is one of `splits`. (is_clean == the admin made zero
         corrections == the cleanest possible recognition truth.)
      2. Apply the data-hygiene gate: silver/synthetic crops are kept only when the
         requested split is 'train'; gold (admin_review) is kept in any split. When
         include_synthetic is False, synthetic-sourced crops are dropped entirely.
      3. For each surviving crop with a real crop_path image, copy the image into
         out_dir/crops/ and append a "crops/<file>\\t<text>" line to rec_gt.txt.
      4. Build the char dictionary (reuse synth's if present, else derive from the
         labels) and write it to out_dir/rec_keys.txt.
      5. Return a manifest dict: per-split crop counts, the set of included pair_ids,
         the char count, and a dataset_version tag = a content hash of those pair_ids.

    Args:
        out_dir: directory to write rec_gt.txt + rec_keys.txt + crops/ into. Created if
                 missing. Intended to live under MEDIA_ROOT (gitignored) or a scratch
                 dir; nothing here is ever committed.
        splits:  which OCRTrainingPair.split buckets to pull, e.g. ('train',) for the
                 training shard or ('eval',) / ('holdout',) for the frozen gold sets.
        include_synthetic: when False, synthetic-sourced crops are excluded entirely
                 (useful to assemble a pure real-data slice).

    Returns:
        manifest dict, shape:
          {
            "out_dir": str,
            "splits": [str, ...],
            "counts": { "<split>": int, ... , "total": int },
            "by_source": { "<source>": int, ... },
            "num_pairs": int,
            "pair_ids": [str, ...],            # the included pair_ids (sorted)
            "num_chars": int,
            "char_dict_path": str,
            "rec_gt_path": str,
            "dataset_version": str,            # content hash of pair_ids
            "skipped_no_crop": int,            # labels with no crop image yet
          }

    Safe to call when there are zero matching crops (e.g. before the layout cropper has
    filled any crop_path): it writes an empty rec_gt.txt + an (empty) dict and returns a
    manifest with all-zero counts. That is exactly what the local CPU dry-run exercises.
    """
    # Lazy model import (mirrors training_capture.py: keep this service import-light and
    # avoid app-loading order surprises when the module is imported outside Django).
    from afc_ocr.models import OCRCropLabel

    os.makedirs(out_dir, exist_ok=True)
    crops_dir = os.path.join(out_dir, CROPS_SUBDIR)
    os.makedirs(crops_dir, exist_ok=True)

    rec_gt_path = os.path.join(out_dir, REC_GT_FILENAME)
    char_dict_path = os.path.join(out_dir, CHAR_DICT_FILENAME)

    # ── 1. Pull candidate crop labels: parent must be clean + in a requested split ──
    # select_related('pair') so reading label.pair.* does not fire one query per row
    # (N+1 guard — the captured set can be large).
    qs = (
        OCRCropLabel.objects
        .select_related("pair")
        .filter(pair__is_clean=True, pair__split__in=list(splits))
        .order_by("pair__pair_id", "placement", "field")
    )

    # Accumulators for the manifest + the on-disk files.
    counts_by_split: dict[str, int] = {s: 0 for s in splits}
    counts_by_source: dict[str, int] = {}
    included_pair_ids: set[str] = set()
    all_texts: list[str] = []
    gt_lines: list[str] = []
    skipped_no_crop = 0

    for label in qs.iterator():
        pair = label.pair
        source = getattr(pair, "source", SOURCE_GOLD)
        split = getattr(pair, "split", "train")

        # ── 2. Data-hygiene gate ──────────────────────────────────────────────
        # Drop synthetic entirely when the caller opted out.
        if source == SOURCE_SYNTHETIC and not include_synthetic:
            continue
        # Silver/synthetic may only ever land in train; gold may land anywhere.
        if not _crop_allowed_in_split(source, split):
            continue

        # ── 3. Resolve + copy the crop image. No image yet -> skip (count it). ──
        dest_rel = _copy_crop(label.crop_path, crops_dir)
        if dest_rel is None:
            skipped_no_crop += 1
            continue

        # PaddleOCR rec_gt format: "<relative image path>\t<label text>". The path is
        # relative to out_dir (rec_gt.txt sits in out_dir), the text is the exact
        # recognition-truth string. Tabs/newlines in a label would corrupt the file, so
        # we defensively flatten any embedded whitespace control chars to a space.
        text = (label.text or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")
        gt_lines.append(f"{dest_rel}\t{text}")
        all_texts.append(text)

        counts_by_split[split] = counts_by_split.get(split, 0) + 1
        counts_by_source[source] = counts_by_source.get(source, 0) + 1
        included_pair_ids.add(str(pair.pair_id))

    # Write rec_gt.txt (UTF-8, newline-joined). Always written, even when empty, so the
    # output dir is always a well-formed (if empty) dataset.
    with open(rec_gt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(gt_lines))
        if gt_lines:
            f.write("\n")

    # ── 4. Char dictionary (one glyph per line, no trailing blank). ───────────────
    chars = _build_char_dictionary(all_texts)
    with open(char_dict_path, "w", encoding="utf-8") as f:
        for ch in chars:
            f.write(ch + "\n")

    # ── 5. Build + return the manifest. ───────────────────────────────────────────
    pair_ids_sorted = sorted(included_pair_ids)
    counts_by_split["total"] = sum(
        v for k, v in counts_by_split.items() if k != "total"
    )
    manifest = {
        "out_dir": out_dir,
        "splits": list(splits),
        "counts": counts_by_split,
        "by_source": counts_by_source,
        "num_pairs": len(pair_ids_sorted),
        "pair_ids": pair_ids_sorted,
        "num_chars": len(chars),
        "char_dict_path": char_dict_path,
        "rec_gt_path": rec_gt_path,
        "dataset_version": _dataset_version_tag(pair_ids_sorted),
        "skipped_no_crop": skipped_no_crop,
    }
    logger.info(
        "dataset: assembled %d crops from %d pairs into %s (version=%s, splits=%s, skipped_no_crop=%d).",
        manifest["counts"]["total"], manifest["num_pairs"], out_dir,
        manifest["dataset_version"], list(splits), skipped_no_crop,
    )
    return manifest


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC: freeze an immutable, append-only manifest
# ──────────────────────────────────────────────────────────────────────────────

def freeze_manifest(dataset_version: str, pairs) -> str:
    """
    Write an immutable record of exactly which pairs constituted one dataset_version.

    WHY: a shipped student model must be traceable back to the precise data it learned
    from. The frozen manifest is that audit trail. It is APPEND-ONLY: we never rewrite
    or delete an existing manifest file, and within a file each pair is one JSON line
    (jsonl) so the file can be appended to safely and read line by line.

    Each line records:
      - pair_id:         the OCRTrainingPair primary key (stringified UUID).
      - image_sha256:    content address of the screenshot (proves which pixels).
      - final_json_hash: sha256 of the canonicalised final_json (proves which label).
                         Canonicalised with sorted keys so the same logical label always
                         hashes the same regardless of key order.

    Args:
        dataset_version: the version tag (typically the one returned by
                         assemble_rec_dataset) -> filename manifest_<version>.jsonl.
        pairs:           an iterable of OCRTrainingPair instances (or any objects with
                         .pair_id, .image_sha256, .final_json).

    Returns:
        Absolute path to the manifest file written/appended.

    The file lives under MEDIA_ROOT/ocr_training/manifests/ (gitignored). Append-only:
    re-running with the same version appends again rather than truncating, preserving
    history; callers that want exactly-once semantics should dedupe upstream.
    """
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    # Sanitise the version into a safe filename fragment (defensive: a tag should already
    # be hash-safe, but never let a stray path separator escape the manifests dir).
    safe_version = "".join(c for c in str(dataset_version) if c.isalnum() or c in ("_", "-", "."))
    manifest_path = os.path.join(MANIFEST_DIR, f"manifest_{safe_version}.jsonl")

    # Append-only: open in 'a'. Each pair -> one jsonl line.
    with open(manifest_path, "a", encoding="utf-8") as f:
        for pair in pairs:
            final_json = getattr(pair, "final_json", None) or {}
            # Canonical JSON (sorted keys, no spaces) so the label hash is stable.
            canonical = json.dumps(final_json, sort_keys=True, separators=(",", ":"))
            final_json_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            line = {
                "pair_id": str(getattr(pair, "pair_id", "")),
                "image_sha256": getattr(pair, "image_sha256", ""),
                "final_json_hash": final_json_hash,
            }
            f.write(json.dumps(line, separators=(",", ":")) + "\n")

    logger.info("dataset: froze manifest %s.", manifest_path)
    return manifest_path
