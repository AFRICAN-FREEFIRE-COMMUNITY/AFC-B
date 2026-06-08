"""
afc_ocr/services/eval_gate.py
================================================================================
OCR learning loop, Phase 2 (P2): the EVAL GATE. This is the SAFETY SPINE of the
whole student-model program. Its single job is to answer one question honestly:

    "Is this newly fine-tuned student model GENUINELY better than the one currently
     in production, with NO regression anywhere that matters? Ship it, or not?"

WHY A HARD GATE (and not just 'higher number wins')
    A leaderboard OCR result feeds real tournament standings. If a new model is even
    slightly worse at a thing an admin relies on (reading a name, counting kills,
    aligning rows), it can silently corrupt results. So 'better on average' is not
    enough. We ship ONLY when the primary metric improves AND no secondary metric
    regresses past a small tolerance AND a curated must-pass slice has zero NEW
    failures. Anything that is not a clear, regression-free win stays unshipped.
    Nothing ever ships worse. That is the spine: it is intentionally conservative.

HOW IT CONNECTS TO THE REST OF THE SYSTEM
    Upstream:
      - afc_ocr.services.dataset.assemble_rec_dataset(splits=('eval',)) produces the
        FROZEN gold eval slice (source='admin_review' only — human-confirmed truth).
        The off-box trainer (afc_ocr/training/finetune.py) runs the new ONNX over that
        slice to produce `predictions`, and reads the SAME slice's labels as `gold`.
      - The current production model's metrics (computed once, the same way) are passed
        in as `current_metrics` for the regression comparison.
    Downstream:
      - afc_ocr/training/finetune.py calls compute_metrics() then regression_gate(); it
        writes the metrics into the bundle's eval_report.json and only emits/flips the
        student bundle (media/models/student_v<N>/) to current when regression_gate
        returns ship=True.
      - afc_ocr.services.local_ocr.LocalOCREngine then loads whichever bundle the
        pointer references. The gate is what decides that pointer ever moves.

PURE PYTHON + NUMPY ONLY. No torch, no paddle, no Django. Importing this module must be
cheap and dependency-light so it can run on the CPU box, in CI, and inside the off-box
trainer alike. numpy is used only for trivial means; everything else is stdlib.

§5 METRICS (the names referenced by the design doc)
    1. name exact-match accuracy   — fraction of name cells transcribed character-perfect
    2. name CER (char error rate)  — mean Levenshtein(pred,gold)/len(gold) over names
    3. kill exact-match accuracy   — fraction of kill cells whose integer count is exact
    4. per-image exact-JSON acc.   — fraction of IMAGES whose ENTIRE result matches gold
                                      (this is the PRIMARY ship metric: it is what the
                                      admin actually experiences — the whole screen right)
    5. row-alignment rate          — fraction of images whose predicted row STRUCTURE
                                      (placement count + players-per-placement) matches
                                      gold, i.e. did we even line the rows up correctly
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Gate tolerances — the knobs of the safety spine. Kept as module constants so the
# policy is visible, auditable, and tunable in ONE place (never buried in a branch).
# ──────────────────────────────────────────────────────────────────────────────

# Minimum improvement in the PRIMARY metric (per-image exact-JSON accuracy) required to
# ship. A new model must beat the current one by at least this much. 0.005 = half a
# percentage point. This guards against shipping on noise: a 0.001 'win' on a small eval
# set is almost certainly not real, so we demand a margin.
EPS = 0.005

# How far a SECONDARY metric is allowed to slip before we call it a regression and block
# the ship. Small and symmetric. A new model may trade a hair of one secondary metric for
# a real primary gain, but only within this tolerance; anything worse blocks.
#   - For accuracies (higher is better): regression = drop > SECONDARY_TOL.
#   - For CER (LOWER is better): regression = rise  > SECONDARY_TOL.
SECONDARY_TOL = 0.005


# ──────────────────────────────────────────────────────────────────────────────
# Low-level text metrics (stdlib only)
# ──────────────────────────────────────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    """
    Classic edit distance (insertions + deletions + substitutions) between two strings.

    Used as the numerator of CER. Iterative two-row DP -> O(len(a)*len(b)) time, O(min)
    space. Pure stdlib so the gate never needs an external Levenshtein package.
    """
    a = a or ""
    b = b or ""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    # Ensure b is the shorter string so the row we keep is the smaller one.
    if len(b) > len(a):
        a, b = b, a

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            insert_cost = current[j - 1] + 1
            delete_cost = previous[j] + 1
            replace_cost = previous[j - 1] + (0 if ca == cb else 1)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]


def _cer(pred: str, gold: str) -> float:
    """
    Character error rate for ONE string pair = Levenshtein(pred, gold) / len(gold).

    Edge case: gold is empty. If pred is also empty -> perfect, CER 0.0. If pred is
    non-empty against an empty gold -> every predicted char is an insertion error, which
    is unbounded as a ratio; we report 1.0 (fully wrong) rather than infinity so the
    aggregate mean stays finite and interpretable.
    """
    pred = pred or ""
    gold = gold or ""
    if len(gold) == 0:
        return 0.0 if len(pred) == 0 else 1.0
    return _levenshtein(pred, gold) / len(gold)


# ──────────────────────────────────────────────────────────────────────────────
# Result-shape helpers — flatten the canonical OCR JSON into comparable cells.
# ──────────────────────────────────────────────────────────────────────────────

def _placements(result: dict) -> list:
    """Return the placements list of one result dict, defensively ([] if absent)."""
    if not isinstance(result, dict):
        return []
    return result.get("placements", []) or []


def _row_shape(result: dict) -> list[int]:
    """
    The STRUCTURE of a result, ignoring text: a list giving the number of players in
    each placement, in placement order. Two results have the same row structure iff they
    have the same number of placements and the same player-count per placement. This is
    what 'row-alignment' measures — did we segment the screen into the right rows, even
    before judging whether the text in them is right.
    """
    return [len(p.get("players", []) or []) for p in _placements(result)]


def _name_kill_cells(result: dict):
    """
    Yield (placement_index, player_index, name_str, kills_int) for every player cell in a
    result, in a stable order. Used to line up predicted cells against gold cells
    positionally. Position-based alignment is deliberate: §5 name/kill accuracy assumes
    the row structure matched (which row-alignment rate separately measures), so we
    compare cell i of pred against cell i of gold.
    """
    out = []
    for pi, placement in enumerate(_placements(result)):
        for ji, player in enumerate(placement.get("players", []) or []):
            name = str(player.get("name", "") or "")
            # Kills may arrive as int, str, or None. Normalise to int; non-numeric -> -1
            # so a garbage kill value can never accidentally equal a real one.
            raw_kills = player.get("kills", 0)
            try:
                kills = int(raw_kills)
            except (TypeError, ValueError):
                kills = -1
            out.append((pi, ji, name, kills))
    return out


def _canonical(result: dict) -> tuple:
    """
    A hashable, comparison-safe canonical form of an entire result, used for per-image
    exact-JSON equality. Two results are 'exactly equal' iff they have the same
    placements, in order, each with the same players in order, each player with the same
    (name, kills). We compare this tuple rather than dict==dict so ordering and the
    name/kills normalisation (int kills) are applied consistently.
    """
    canon = []
    for placement in _placements(result):
        players = tuple(
            (str(pl.get("name", "") or ""),
             # same int-normalisation as _name_kill_cells for a consistent comparison
             (int(pl["kills"]) if str(pl.get("kills", "")).lstrip("-").isdigit() else -1))
            for pl in (placement.get("players", []) or [])
        )
        canon.append((placement.get("placement"), players))
    return tuple(canon)


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC: compute the §5 metrics
# ──────────────────────────────────────────────────────────────────────────────

def compute_metrics(predictions: list, gold: list) -> dict:
    """
    Compute the §5 evaluation metrics over a set of images.

    Args:
        predictions: list of result dicts (the model's output per image), canonical OCR
                     shape: {"placements":[{"placement":int,
                                             "players":[{"name":str,"kills":int}]}]}.
        gold:        list of the corresponding GROUND-TRUTH result dicts, SAME length and
                     SAME order as predictions (predictions[i] is the model's read of the
                     image whose truth is gold[i]).

    Returns:
        dict of metrics:
          {
            "n_images":              int,    # how many images were scored
            "n_name_cells":          int,    # how many name cells were comparable
            "n_kill_cells":          int,    # how many kill cells were comparable
            "name_exact_acc":        float,  # §5.1  higher better, in [0,1]
            "name_cer":              float,  # §5.2  LOWER better, >= 0
            "kill_exact_acc":        float,  # §5.3  higher better, in [0,1]
            "image_exact_json_acc":  float,  # §5.4  higher better, in [0,1]  (PRIMARY)
            "row_alignment_rate":    float,  # §5.5  higher better, in [0,1]
          }

    Cell-level name/kill metrics only count cells that exist in BOTH pred and gold at the
    same position (positional alignment); a mismatch in row count is captured by
    row_alignment_rate and by image_exact_json_acc, not double-counted into name/kill
    accuracy. An empty input set returns all-zero metrics (and name_cer 0.0) rather than
    raising, so a 0-image eval degrades cleanly.
    """
    if len(predictions) != len(gold):
        # Misaligned inputs would silently produce meaningless metrics. Fail loud.
        raise ValueError(
            f"compute_metrics: predictions ({len(predictions)}) and gold ({len(gold)}) "
            "must be the same length and aligned by index."
        )

    n_images = len(gold)

    name_correct = 0
    name_total = 0
    cer_values: list[float] = []

    kill_correct = 0
    kill_total = 0

    image_exact = 0
    row_aligned = 0

    for pred_result, gold_result in zip(predictions, gold):
        # ── §5.4 per-image exact-JSON: the whole screen, character-perfect ──────
        if _canonical(pred_result) == _canonical(gold_result):
            image_exact += 1

        # ── §5.5 row alignment: did the row STRUCTURE match (text aside)? ───────
        if _row_shape(pred_result) == _row_shape(gold_result):
            row_aligned += 1

        # ── §5.1-5.3 cell metrics: compare cells that exist in both, by position ─
        pred_cells = {(pi, ji): (name, kills)
                      for (pi, ji, name, kills) in _name_kill_cells(pred_result)}
        gold_cells = {(pi, ji): (name, kills)
                      for (pi, ji, name, kills) in _name_kill_cells(gold_result)}

        for key, (g_name, g_kills) in gold_cells.items():
            # Names. A gold cell with no predicted counterpart counts as a name miss
            # (pred = "" -> exact fails, CER vs gold counts the whole gold as errors).
            p_name, p_kills = pred_cells.get(key, ("", -1))

            name_total += 1
            if p_name == g_name:
                name_correct += 1
            cer_values.append(_cer(p_name, g_name))

            # Kills exact-match (integer equality).
            kill_total += 1
            if p_kills == g_kills:
                kill_correct += 1

    # Aggregate. Guard every division so a 0-cell / 0-image set yields 0.0, not a crash.
    name_exact_acc = (name_correct / name_total) if name_total else 0.0
    name_cer = float(np.mean(cer_values)) if cer_values else 0.0
    kill_exact_acc = (kill_correct / kill_total) if kill_total else 0.0
    image_exact_json_acc = (image_exact / n_images) if n_images else 0.0
    row_alignment_rate = (row_aligned / n_images) if n_images else 0.0

    return {
        "n_images": n_images,
        "n_name_cells": name_total,
        "n_kill_cells": kill_total,
        "name_exact_acc": round(name_exact_acc, 6),
        "name_cer": round(name_cer, 6),
        "kill_exact_acc": round(kill_exact_acc, 6),
        "image_exact_json_acc": round(image_exact_json_acc, 6),
        "row_alignment_rate": round(row_alignment_rate, 6),
    }


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC: the regression gate — the actual ship / no-ship decision
# ──────────────────────────────────────────────────────────────────────────────

def regression_gate(new_metrics: dict, current_metrics: dict, must_pass_results) -> tuple:
    """
    THE SAFETY SPINE. Decide whether the new student model may ship, given its metrics,
    the current production model's metrics, and a curated must-pass slice. Conservative
    by design: ship ONLY on a clear, regression-free improvement. Nothing ever ships
    worse.

    Ship is granted IFF ALL of the following hold:

      (A) PRIMARY GAIN — per-image exact-JSON accuracy rises by at least EPS:
            new.image_exact_json_acc - current.image_exact_json_acc >= EPS
          This is the metric the admin actually feels (the whole screen correct), and the
          margin (EPS) guards against shipping on eval-set noise.

      (B) NO SECONDARY REGRESSION — none of the secondary metrics slips past
          SECONDARY_TOL:
            - name_exact_acc   may not DROP more than SECONDARY_TOL   (higher is better)
            - kill_exact_acc   may not DROP more than SECONDARY_TOL   (higher is better)
            - row_alignment_rate may not DROP more than SECONDARY_TOL (higher is better)
            - name_cer         may not RISE more than SECONDARY_TOL   (LOWER is better)
          A model that buys a primary gain by wrecking a secondary metric is NOT shipped.

      (C) MUST-PASS CLEAN — the curated must-pass slice has ZERO NEW failures. This slice
          is a hand-picked set of cases the system must never get wrong (e.g. known-hard
          but business-critical screenshots). `must_pass_results` is a list of per-case
          dicts; a case is a NEW failure if the new model fails a case the current model
          passed. Even one new must-pass failure blocks the ship, regardless of the
          aggregate numbers.

    Args:
        new_metrics:     dict from compute_metrics() for the candidate model.
        current_metrics: dict from compute_metrics() for the live production model.
        must_pass_results: list of dicts, one per must-pass case, each shaped:
                             {"id": <case id>,
                              "new_pass": bool,        # did the candidate pass this case
                              "current_pass": bool}    # did the live model pass it
                           A 'new failure' = current_pass is True and new_pass is False.
                           Pass [] when there is no must-pass slice (condition C is then
                           vacuously satisfied).

    Returns:
        (ship: bool, reasons: list[str])
          ship   — True only when A AND B AND C all hold.
          reasons— human-readable lines explaining EACH check's outcome (both the passing
                   and the failing ones), so the trainer can log exactly why a model was
                   or was not shipped. The decision is always auditable from `reasons`.
    """
    reasons: list[str] = []

    # Defensive reads: a missing metric is treated as the worst case for the candidate
    # (so a malformed metrics dict can never accidentally wave a bad model through).
    def _new(key, worst):
        return float(new_metrics.get(key, worst))

    def _cur(key, default):
        return float(current_metrics.get(key, default))

    # ── (A) PRIMARY GAIN: per-image exact-JSON must rise by >= EPS ─────────────────
    new_primary = _new("image_exact_json_acc", worst=-1.0)
    cur_primary = _cur("image_exact_json_acc", default=0.0)
    primary_delta = new_primary - cur_primary
    primary_ok = primary_delta >= EPS
    reasons.append(
        f"[{'PASS' if primary_ok else 'FAIL'}] primary image_exact_json_acc "
        f"{cur_primary:.4f} -> {new_primary:.4f} (delta {primary_delta:+.4f}, "
        f"need >= +{EPS})"
    )

    # ── (B) NO SECONDARY REGRESSION ────────────────────────────────────────────────
    secondary_ok = True

    # Accuracies: higher is better -> a regression is a DROP beyond tolerance.
    for key in ("name_exact_acc", "kill_exact_acc", "row_alignment_rate"):
        # Worst case for the candidate on a higher-is-better metric is 0.0.
        new_v = _new(key, worst=0.0)
        cur_v = _cur(key, default=0.0)
        delta = new_v - cur_v
        regressed = delta < -SECONDARY_TOL
        if regressed:
            secondary_ok = False
        reasons.append(
            f"[{'FAIL' if regressed else 'PASS'}] secondary {key} "
            f"{cur_v:.4f} -> {new_v:.4f} (delta {delta:+.4f}, "
            f"allowed drop <= {SECONDARY_TOL})"
        )

    # CER: LOWER is better -> a regression is a RISE beyond tolerance.
    # Worst case for the candidate on a lower-is-better metric is a large value (1e9).
    new_cer = _new("name_cer", worst=1e9)
    cur_cer = _cur("name_cer", default=0.0)
    cer_delta = new_cer - cur_cer
    cer_regressed = cer_delta > SECONDARY_TOL
    if cer_regressed:
        secondary_ok = False
    reasons.append(
        f"[{'FAIL' if cer_regressed else 'PASS'}] secondary name_cer "
        f"{cur_cer:.4f} -> {new_cer:.4f} (delta {cer_delta:+.4f}, "
        f"allowed rise <= {SECONDARY_TOL})"
    )

    # ── (C) MUST-PASS CLEAN: zero NEW failures on the curated slice ────────────────
    new_failures = []
    for case in (must_pass_results or []):
        # A new failure = the live model passed this case but the candidate fails it.
        # (A case both already failed and still fails is NOT a NEW failure; the gate
        # blocks regressions, not pre-existing known gaps.)
        if case.get("current_pass", False) and not case.get("new_pass", False):
            new_failures.append(case.get("id", "?"))
    must_pass_ok = len(new_failures) == 0
    if must_pass_ok:
        reasons.append(
            f"[PASS] must-pass slice: 0 new failures across "
            f"{len(must_pass_results or [])} case(s)"
        )
    else:
        reasons.append(
            f"[FAIL] must-pass slice: {len(new_failures)} new failure(s): {new_failures}"
        )

    # ── FINAL: ship only when A AND B AND C all hold. Conservative by design. ──────
    ship = bool(primary_ok and secondary_ok and must_pass_ok)
    reasons.append(f"[{'SHIP' if ship else 'HOLD'}] final decision: ship={ship}")

    logger.info("eval_gate.regression_gate decision: ship=%s; %s", ship, " | ".join(reasons))
    return ship, reasons
