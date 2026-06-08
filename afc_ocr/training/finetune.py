"""
afc_ocr/training/finetune.py
================================================================================
OFF-BOX (GPU) fine-tune driver for the AFC self-hosted OCR student model.

WHAT THIS IS
    A runnable-off-box SCRIPT SKELETON that orchestrates one student-model fine-tune,
    end to end, on a GPU box (Colab / Kaggle / a spot g4dn). It does NOT train on the
    machine that runs Django. It glues together the pieces the rest of the loop already
    built:

        1. PULL the assembled dataset (rec_gt.txt + crops/ + rec_keys.txt) that
           afc_ocr.services.dataset.assemble_rec_dataset produced, either from a local
           directory, an exported tarball, or a read-only prod-DB clone.
        2. FINE-TUNE PaddleOCR PP-OCRv5_mobile_rec (the SVTR_LCNet recognizer) on that
           data, using the CUSTOM character dictionary (rec_keys.txt) so the student
           speaks exactly AFC's alphabet (Free Fire player-name glyphs + digits).
        3. EXPORT the trained recognizer to ONNX via paddle2onnx (rec.onnx).
        4. EVALUATE the ONNX over the FROZEN gold eval slice with
           afc_ocr.services.eval_gate.compute_metrics, then ask
           afc_ocr.services.eval_gate.regression_gate whether it beats the current
           production model with no regression.
        5. EMIT a versioned bundle  student_v<N>/ = {rec.onnx, rec_keys.txt, VERSION,
           model_card.json, eval_report.json}  that
           afc_ocr.services.local_ocr.LocalOCREngine can load as-is.

HOW IT CONNECTS TO THE REST OF THE SYSTEM
    - INPUT comes from afc_ocr.services.dataset (the assembler) — same rec_gt.txt +
      rec_keys.txt + crops/ layout, byte-for-byte.
    - The EVAL step calls afc_ocr.services.eval_gate (compute_metrics + regression_gate),
      the same safety spine the CPU box uses.
    - The OUTPUT bundle is consumed by afc_ocr.services.local_ocr.LocalOCREngine, which
      looks for <model_dir>/rec.onnx + rec_keys.txt + VERSION (see that file's __init__).
      Dropping the bundle at backend/media/models/student_v<N>/ and flipping the current
      pointer is what promotes a new student into production. The human steps for that
      promotion are in finetune_ppocrv5.md.

WHY PADDLE IS IMPORTED LAZILY
    paddle / paddleocr / paddle2onnx are heavy GPU dependencies that are NOT installed on
    the CPU box (or in CI). This module must stay importable there so the eval-gate path
    and tooling can `import afc_ocr.training.finetune` without paddle present. Therefore
    the ONLY place paddle is imported is INSIDE run_finetune() (and its helpers), never at
    module top level. Importing this file requires nothing beyond the stdlib.

USAGE (on the GPU box, after `pip install paddlepaddle-gpu paddleocr paddle2onnx`):
    python -m afc_ocr.training.finetune \
        --dataset-dir ./ocr_dataset_train \
        --eval-dir    ./ocr_dataset_eval \
        --pretrained  ./PP-OCRv5_mobile_rec_pretrained \
        --out-dir     ./media/models \
        --version     3

    See afc_ocr/training/finetune_ppocrv5.md for the full, concrete walkthrough
    (exact paddle config knobs, the paddle2onnx command, and where to drop the bundle).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Bundle filenames — MUST match what afc_ocr.services.local_ocr.LocalOCREngine loads:
# it reads <model_dir>/rec.onnx, <model_dir>/rec_keys.txt, <model_dir>/VERSION.
# We add model_card.json + eval_report.json for provenance (local_ocr ignores extras).
# ──────────────────────────────────────────────────────────────────────────────
BUNDLE_ONNX = "rec.onnx"
BUNDLE_KEYS = "rec_keys.txt"
BUNDLE_VERSION = "VERSION"
BUNDLE_MODEL_CARD = "model_card.json"
BUNDLE_EVAL_REPORT = "eval_report.json"

# Standard dataset filenames produced by afc_ocr.services.dataset.assemble_rec_dataset.
REC_GT_FILENAME = "rec_gt.txt"
CHAR_DICT_FILENAME = "rec_keys.txt"


# ──────────────────────────────────────────────────────────────────────────────
# Eval-slice loading (pure stdlib — runs anywhere, no paddle needed)
# ──────────────────────────────────────────────────────────────────────────────

def load_eval_pairs(eval_dir: str):
    """
    Load the FROZEN gold eval slice as (predictions_placeholder, gold) inputs for the
    eval gate.

    The eval slice on disk is a PaddleOCR rec dataset (rec_gt.txt + crops/) exactly like
    the train slice, but assembled with splits=('eval',) so it is gold-only
    (source='admin_review'). This helper reads rec_gt.txt and returns the gold cell
    labels keyed by crop path, so the caller can run the ONNX over the same crops and
    align predictions to gold.

    Returns:
        list of (crop_relpath, gold_text) tuples, in file order.

    NOTE: This returns CELL-level labels (one per crop), which is what the recognizer
    predicts. Turning cell predictions back into per-IMAGE result JSON (for §5.4 per-image
    exact-JSON) is done by _regroup_cells_to_images() below using the eval manifest. Kept
    separate so the I/O here stays trivial and paddle-free.
    """
    gt_path = os.path.join(eval_dir, REC_GT_FILENAME)
    pairs = []
    if not os.path.exists(gt_path):
        logger.warning("load_eval_pairs: no %s in %s", REC_GT_FILENAME, eval_dir)
        return pairs
    with open(gt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            # rec_gt format = "<crop_relpath>\t<text>"
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            crop_relpath, text = parts
            pairs.append((crop_relpath, text))
    return pairs


# ──────────────────────────────────────────────────────────────────────────────
# Bundle writer (pure stdlib — the emit step does not need paddle)
# ──────────────────────────────────────────────────────────────────────────────

def write_bundle(out_dir: str, version: int, onnx_path: str, keys_path: str,
                 metrics: dict, gate_reasons: list, ship: bool,
                 dataset_version: str = "", extra_card: dict | None = None) -> str:
    """
    Assemble the student bundle directory  <out_dir>/student_v<version>/  with everything
    local_ocr + provenance need:

        rec.onnx          — copied from onnx_path (the paddle2onnx export)
        rec_keys.txt      — copied from keys_path (the CUSTOM char dict used to train)
        VERSION           — the string "student_v<version>" (local_ocr reads this as
                            self.model_version)
        model_card.json   — provenance: when, which pretrained base, dataset_version,
                            char count, ship decision + gate reasons
        eval_report.json  — the full compute_metrics() dict + the gate reasons

    This step is intentionally paddle-free: by the time we get here the ONNX already
    exists, so emitting the bundle is just file copies + JSON writes. That keeps the bundle
    layout testable on the CPU box.

    Returns:
        Absolute path to the created student_v<version>/ directory.
    """
    bundle_dir = os.path.join(out_dir, f"student_v{version}")
    os.makedirs(bundle_dir, exist_ok=True)

    # Copy the two files local_ocr actually loads at inference.
    shutil.copyfile(onnx_path, os.path.join(bundle_dir, BUNDLE_ONNX))
    shutil.copyfile(keys_path, os.path.join(bundle_dir, BUNDLE_KEYS))

    # VERSION: local_ocr reads this verbatim into self.model_version.
    version_str = f"student_v{version}"
    with open(os.path.join(bundle_dir, BUNDLE_VERSION), "w", encoding="utf-8") as f:
        f.write(version_str + "\n")

    # Count the dictionary so the card records the vocab size the ONNX was built with.
    try:
        with open(keys_path, "r", encoding="utf-8") as f:
            num_chars = sum(1 for _ in f)
    except OSError:
        num_chars = None

    # model_card.json — human + machine readable provenance.
    card = {
        "version": version_str,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_model": "PP-OCRv5_mobile_rec (SVTR_LCNet)",
        "dataset_version": dataset_version,
        "num_chars": num_chars,
        "shipped": ship,
        "gate_reasons": gate_reasons,
    }
    if extra_card:
        card.update(extra_card)
    with open(os.path.join(bundle_dir, BUNDLE_MODEL_CARD), "w", encoding="utf-8") as f:
        json.dump(card, f, indent=2)

    # eval_report.json — the metrics that drove the decision.
    report = {
        "version": version_str,
        "metrics": metrics,
        "ship": ship,
        "gate_reasons": gate_reasons,
    }
    with open(os.path.join(bundle_dir, BUNDLE_EVAL_REPORT), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    logger.info("write_bundle: wrote %s (ship=%s).", bundle_dir, ship)
    return bundle_dir


# ──────────────────────────────────────────────────────────────────────────────
# The fine-tune itself — THE ONLY place paddle is imported (guarded).
# ──────────────────────────────────────────────────────────────────────────────

def run_finetune(
    dataset_dir: str,
    eval_dir: str,
    pretrained: str,
    out_dir: str,
    version: int,
    current_metrics: dict | None = None,
    must_pass_results: list | None = None,
    epochs: int = 20,
    config_path: str | None = None,
) -> dict:
    """
    Run one full off-box fine-tune + export + eval-gate cycle and emit the bundle.

    THIS IS THE ONLY FUNCTION THAT NEEDS PADDLE. paddle / paddleocr / paddle2onnx are
    imported INSIDE this function so `import afc_ocr.training.finetune` never requires them
    on the CPU box. Calling this function on a box without paddle raises a clear, actionable
    error telling the user to run it on the GPU box per finetune_ppocrv5.md.

    High-level flow (see the .md for the concrete commands behind each step):
        1. Sanity-check the dataset (rec_gt.txt + rec_keys.txt + crops/ present).
        2. Build/patch the PP-OCRv5_mobile_rec training config to point at:
             - Train.dataset.label_file_list = <dataset_dir>/rec_gt.txt
             - Global.character_dict_path    = <dataset_dir>/rec_keys.txt   (CUSTOM dict)
             - Global.use_space_char         = True                         (names contain spaces)
             - Global.pretrained_model       = <pretrained>                 (PP-OCRv5_mobile_rec)
           then run PaddleOCR's tools/train.py.
        3. Export best -> inference model -> ONNX (paddle2onnx).
        4. Run the ONNX over the frozen gold eval slice, compute_metrics, regression_gate.
        5. write_bundle(...) -> student_v<version>/.

    Returns:
        dict { "bundle_dir": str, "ship": bool, "metrics": dict, "reasons": list }.

    The actual paddle calls are intentionally left as a documented skeleton: training is
    interactive and box-specific (which is exactly why the procedure lives in the .md). The
    skeleton hard-fails with guidance if paddle is missing so nobody accidentally tries to
    train on the Django box.
    """
    # ── GUARDED HEAVY IMPORT — keep this module importable without paddle ──────────
    try:
        import paddle  # noqa: F401  (GPU dep; only present on the training box)
    except Exception as exc:  # ImportError, or a paddle init failure on a CPU box
        raise RuntimeError(
            "run_finetune requires PaddlePaddle, which is not installed here. "
            "This function is meant to run OFF-BOX on a GPU (Colab / Kaggle / spot g4dn). "
            "On that box run: pip install paddlepaddle-gpu paddleocr paddle2onnx, then "
            "follow afc_ocr/training/finetune_ppocrv5.md. "
            f"(underlying import error: {exc})"
        ) from exc

    # NOTE: the steps below are the documented skeleton. On the GPU box, fill the marked
    # spots with the exact PaddleOCR tools/train.py + paddle2onnx invocations from the .md.
    # They are left explicit (not silently stubbed) so the operator knows what runs where.

    # 1. Dataset sanity check (pure stdlib; safe to keep here).
    gt = os.path.join(dataset_dir, REC_GT_FILENAME)
    keys = os.path.join(dataset_dir, CHAR_DICT_FILENAME)
    if not (os.path.exists(gt) and os.path.exists(keys)):
        raise FileNotFoundError(
            f"dataset_dir {dataset_dir} must contain {REC_GT_FILENAME} and {CHAR_DICT_FILENAME} "
            "(produced by afc_ocr.services.dataset.assemble_rec_dataset)."
        )

    # 2. Train. <<< on the GPU box: invoke PaddleOCR tools/train.py with the patched
    #    PP-OCRv5_mobile_rec config (see .md §3). Pseudocode:
    #        subprocess.run(["python", "tools/train.py", "-c", config_path, "-o",
    #                        f"Global.character_dict_path={keys}",
    #                        "Global.use_space_char=True",
    #                        f"Global.pretrained_model={pretrained}",
    #                        f"Train.dataset.label_file_list=[{gt}]",
    #                        f"Global.epoch_num={epochs}"], check=True)
    raise NotImplementedError(
        "run_finetune skeleton: the paddle train/export steps are run on the GPU box "
        "per afc_ocr/training/finetune_ppocrv5.md. Fill steps 2-3 with the tools/train.py "
        "and paddle2onnx commands from the .md, then steps 4-5 below assemble + gate the "
        "bundle using afc_ocr.services.eval_gate and write_bundle()."
    )

    # 3. Export to ONNX via paddle2onnx. <<< on the GPU box (see .md §5).
    # 4. Eval + gate (uses the SAME eval_gate the CPU box uses):
    #        from afc_ocr.services import eval_gate
    #        predictions, gold = _predict_eval(onnx_path, eval_dir)   # run ONNX over crops
    #        metrics = eval_gate.compute_metrics(predictions, gold)
    #        ship, reasons = eval_gate.regression_gate(metrics, current_metrics or {},
    #                                                  must_pass_results or [])
    # 5. write_bundle(out_dir, version, onnx_path, keys, metrics, reasons, ship, ...)
    #    -> student_v<version>/, then (manually, per .md) flip the current pointer.


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point — argparse only, no paddle at import time.
# ──────────────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI args for `python -m afc_ocr.training.finetune`. Defined separately so it can be
    unit-imported without running anything."""
    p = argparse.ArgumentParser(
        description="Off-box fine-tune driver for the AFC OCR student model. "
                    "Run on a GPU box; see afc_ocr/training/finetune_ppocrv5.md."
    )
    p.add_argument("--dataset-dir", required=True,
                   help="Train dataset dir (rec_gt.txt + rec_keys.txt + crops/) from assemble_rec_dataset.")
    p.add_argument("--eval-dir", required=True,
                   help="Frozen gold eval dataset dir (splits=('eval',)) from assemble_rec_dataset.")
    p.add_argument("--pretrained", required=True,
                   help="Path to the PP-OCRv5_mobile_rec pretrained weights to fine-tune from.")
    p.add_argument("--out-dir", default="./media/models",
                   help="Where to write student_v<N>/. Prod reads backend/media/models/.")
    p.add_argument("--version", type=int, required=True,
                   help="Integer student version N -> bundle student_v<N>/.")
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--config", default=None,
                   help="Optional path to a PaddleOCR rec config to patch instead of the default.")
    return p


def main(argv=None):
    """`python -m afc_ocr.training.finetune ...`. Parses args and calls run_finetune. The
    paddle requirement only bites inside run_finetune (guarded), so --help and arg parsing
    work on any box."""
    args = _build_arg_parser().parse_args(argv)
    return run_finetune(
        dataset_dir=args.dataset_dir,
        eval_dir=args.eval_dir,
        pretrained=args.pretrained,
        out_dir=args.out_dir,
        version=args.version,
        epochs=args.epochs,
        config_path=args.config,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
