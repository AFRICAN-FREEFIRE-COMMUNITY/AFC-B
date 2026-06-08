"""
afc_ocr/services/local_ocr.py
================================================================================
Self-hosted, CPU-only OCR engine for AFC Free Fire result screenshots. This is the
"student" half of the design: a local model that reads the screenshot WITHOUT calling
Gemini, so the platform keeps working at zero API cost. Gemini is only a teacher
(bootstrap labels, services/training_capture + tasks) and a confidence-gated fallback
(services/ocr_confidence + the router in views.py).

PIPELINE (two stages, per the approved design):
  Stage 1  recognition : a PP-OCR ONNX recognizer (rapidocr-onnxruntime, runs on
                         onnxruntime + opencv, no torch/paddle on the box) reads every
                         text box on the screen: (quad, text, score).
  Stage 2  structuring : group those boxes into the SAME JSON shape Gemini returns
                         ({"placements":[{"placement", "players":[{"name","kills"}]}]})
                         using spatial layout (columns + rows) and content heuristics
                         ("N Eliminations" -> kills, the rest -> names). Placement is
                         inferred from row order within each column (FF shows ranks 1..K
                         top-to-bottom, often split into a left and a right column).

The recognizer can be swapped for a FINE-TUNED model later (P2, off-box GPU) by pointing
settings.OCR_LOCAL_MODEL_PATH at a custom rec ONNX + char dict; the structuring stage is
unchanged. Until a fine-tune is dropped in, the bundled PP-OCR rec is the baseline student.

CONTRACT (drop-in for services/gemini.call_gemini, plus an event_type arg + a confidence
return): run_local_student(image_bytes, mime_type, aliases, team_notes, event_type)
  -> (result_json: dict, confidence: dict)
`result_json` matches call_gemini's shape EXACTLY so the existing draft-row build,
match_name, detect_team_mismatches and the commit path stay byte-for-byte unchanged.
`confidence` carries the per-field + structural signals services/ocr_confidence.gate reads
to decide whether to escalate to Gemini.

CONSUMED BY: afc_ocr/views.py (the upload_ocr_session / ocr_from_stored_image router) via
get_engine().run(...). The engine is a lazy module-level singleton so the ONNX session
loads ONCE per worker (sub-second, ~10s of MB) and serves in-request.
"""

from __future__ import annotations

import logging
import re

import numpy as np

logger = logging.getLogger(__name__)

# "3 Eliminations" / "3Eliminations" / "3 elims" -> the kill count for a player cell.
# Match the FULL elimination word (elimin\w*) so removing it leaves a clean name and never
# a stray "inations" fragment.
_KILLS_RE = re.compile(r"(\d+)\s*elimin\w*", re.IGNORECASE)
# Max players in one placement group for each event format — used by the confidence gate to
# flag a structurally implausible read (rows merged across columns) and escalate to Gemini.
_MAX_PLAYERS_PER_PLACEMENT = {"team": 4, "solo": 1}
# A bare placement rank like "#1" or "1" sitting at the start of a row (best-effort only;
# we mostly infer placement from row order because FF often draws the rank as an icon).
_RANK_RE = re.compile(r"^#?\s*(\d{1,2})$")
# Header / chrome text we drop before structuring (never a name or a kill count).
_NOISE_RE = re.compile(
    r"^(free\s*fire|booyah|result|rank|kills?|elimination|player|team|"
    r"total|score|points?|match|round|map)\b",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────────
# Engine singleton — load the ONNX recognizer once per process.
# ──────────────────────────────────────────────────────────────────────────────
_ENGINE = None


def get_engine():
    """Return the process-wide LocalOCREngine, building it on first use (lazy so a worker
    that never does OCR pays nothing, and so import of this module never hard-fails if the
    optional rapidocr/onnxruntime deps are absent — get_engine() raises only when actually
    called).

    Loads the FINE-TUNED student bundle that model_registry currently points at
    (media/models/current), if any; on cold start (no bundle promoted) that resolves to None
    and we use the bundled PP-OCR baseline. model_registry.promote()/rollback() reset _ENGINE
    to None, so the very next call here rebuilds with the newly-active model, no restart."""
    global _ENGINE
    if _ENGINE is None:
        model_dir = None
        try:  # registry is optional at import time; never let it block OCR
            from afc_ocr.services.model_registry import active_model_dir
            model_dir = active_model_dir()
        except Exception:
            model_dir = None
        _ENGINE = LocalOCREngine(model_dir=model_dir)
    return _ENGINE


def is_available() -> bool:
    """True when the local engine can run (deps importable). The router uses this to decide
    whether a local-first attempt is even possible; when False it falls straight to Gemini,
    which is the designed cold-start behaviour (student handles 0%, Gemini 100%)."""
    try:
        import onnxruntime  # noqa: F401
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        return True
    except Exception:
        return False


class LocalOCREngine:
    """Wraps the PP-OCR ONNX recognizer + the FF-result structuring. One instance per
    worker (see get_engine)."""

    def __init__(self, model_dir: str | None = None):
        # model_dir lets a fine-tuned bundle (P2) override the bundled PP-OCR rec + char
        # dict. When None we use rapidocr's bundled PP-OCR ONNX models (the baseline student).
        from rapidocr_onnxruntime import RapidOCR

        self.model_version = "ppocr_baseline_v0"
        kwargs = {}
        if model_dir:
            # A fine-tuned bundle ships its own rec_model + rec dict; det/cls stay bundled.
            import os
            rec = os.path.join(model_dir, "rec.onnx")
            keys = os.path.join(model_dir, "rec_keys.txt")
            if os.path.exists(rec):
                kwargs["rec_model_path"] = rec
            if os.path.exists(keys):
                kwargs["rec_keys_path"] = keys
            ver = os.path.join(model_dir, "VERSION")
            if os.path.exists(ver):
                try:
                    self.model_version = open(ver, encoding="utf-8").read().strip() or self.model_version
                except OSError:
                    pass
        self._ocr = RapidOCR(**kwargs)

    # ── public API (drop-in for call_gemini) ──────────────────────────────────
    def run(self, image_bytes: bytes, mime_type: str, aliases=None, team_notes=None,
            event_type: str = "team") -> tuple[dict, dict]:
        """Read `image_bytes` and return (result_json, confidence). Never raises into the
        caller: on any failure it returns an empty result with confidence ok=False so the
        router escalates to Gemini."""
        try:
            boxes = self._recognize(image_bytes)
            if not boxes:
                return ({"match_type": event_type, "placements": []},
                        {"ok": False, "reason": "no_text", "model_version": self.model_version})
            result = self._structure(boxes, event_type)
            conf = self._confidence(boxes, result, event_type)
            conf["model_version"] = self.model_version
            return result, conf
        except Exception as e:  # defensive: a student failure must never break the upload
            logger.exception("local_ocr.run failed: %s", e)
            return ({"match_type": event_type, "placements": []},
                    {"ok": False, "reason": f"error:{type(e).__name__}", "model_version": self.model_version})

    # ── stage 1: recognition ──────────────────────────────────────────────────
    def _recognize(self, image_bytes: bytes) -> list[dict]:
        """Decode bytes -> run the ONNX det+rec -> list of {text, score, cx, cy, h, x0}.
        cx/cy = box centre, h = box height (for row clustering), x0 = left edge (column
        split + name/kill ordering)."""
        import cv2
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return []
        res, _ = self._ocr(img)
        out = []
        for quad, text, score in (res or []):
            text = (text or "").strip()
            if not text:
                continue
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            out.append({
                "text": text,
                "score": float(score),
                "cx": sum(xs) / 4.0,
                "cy": sum(ys) / 4.0,
                "h": max(ys) - min(ys),
                "x0": min(xs),
            })
        return out

    # ── stage 2: structuring ──────────────────────────────────────────────────
    def _structure(self, boxes: list[dict], event_type: str) -> dict:
        """Group recognized boxes into {"placements":[{placement, players:[{name,kills}]}]}.

        Heuristics (robust to FF's two-column result layout, tolerant of misreads):
          1. Drop header/chrome noise.
          2. Split into a LEFT and RIGHT column by a gap in x-centres (if the screen is
             single-column this is a no-op).
          3. Within each column, cluster boxes into ROWS by y-proximity (one row ~ one
             placement line).
          4. In each row, a box matching "N Eliminations" is a kill count; everything else
             is a player name; pair each kill to the nearest preceding name by x.
          5. Placement = global row rank (left column rows first, then right column rows),
             which is how FF numbers placements 1..K top-to-bottom across the two columns.
        """
        boxes = [b for b in boxes if not _NOISE_RE.match(b["text"])]
        if not boxes:
            return {"match_type": event_type, "placements": []}

        median_h = float(np.median([b["h"] for b in boxes])) or 20.0
        columns = self._split_columns(boxes)

        placements = []
        placement_no = 0
        for col in columns:
            for row in self._cluster_rows(col, median_h):
                players = self._row_to_players(row)
                if not players:
                    continue
                placement_no += 1
                placements.append({"placement": placement_no, "players": players})
        return {"match_type": event_type, "placements": placements}

    @staticmethod
    def _split_columns(boxes: list[dict]) -> list[list[dict]]:
        """Return [left_boxes, right_boxes] when the layout is clearly two-column, else
        [all_boxes]. Detected by a wide empty band in the x-centre distribution."""
        xs = sorted(b["cx"] for b in boxes)
        if len(xs) < 6:
            return [boxes]
        span = xs[-1] - xs[0]
        if span <= 0:
            return [boxes]
        # Largest gap between consecutive x-centres; a column break if it is a big fraction
        # of the total width and sits near the middle.
        best_gap, split_x = 0.0, None
        for a, b in zip(xs, xs[1:]):
            gap = b - a
            mid = (a + b) / 2.0
            if gap > best_gap and 0.30 * span < (mid - xs[0]) < 0.70 * span:
                best_gap, split_x = gap, mid
        if split_x is not None and best_gap > 0.18 * span:
            left = [b for b in boxes if b["cx"] < split_x]
            right = [b for b in boxes if b["cx"] >= split_x]
            if len(left) >= 2 and len(right) >= 2:
                return [left, right]
        return [boxes]

    @staticmethod
    def _cluster_rows(boxes: list[dict], median_h: float) -> list[list[dict]]:
        """Cluster a column's boxes into rows by vertical proximity (gap > ~0.8 line height
        starts a new row). Each row is x-sorted so names precede their kill counts."""
        if not boxes:
            return []
        ordered = sorted(boxes, key=lambda b: b["cy"])
        rows, cur = [], [ordered[0]]
        thresh = max(median_h * 0.8, 8.0)
        for b in ordered[1:]:
            if b["cy"] - cur[-1]["cy"] > thresh:
                rows.append(cur)
                cur = [b]
            else:
                cur.append(b)
        rows.append(cur)
        return [sorted(r, key=lambda b: b["x0"]) for r in rows]

    @staticmethod
    def _row_to_players(row: list[dict]) -> list[dict]:
        """Turn one row's boxes into [{name, kills}]. A box may carry BOTH a name and its
        elim count merged ("AE.MI6 4Eliminations"); we split on the kills pattern. A bare
        "N Eliminations" box attaches to the most recent name."""
        players: list[dict] = []
        for b in row:
            text = b["text"]
            m = _KILLS_RE.search(text)
            kills = int(m.group(1)) if m else None
            name_part = _KILLS_RE.sub("", text).strip(" .:-")
            # A name needs at least 2 non-space chars; shorter leftovers (e.g. a stray digit
            # or punctuation from a split box) are not real names.
            is_name = len(name_part.replace(" ", "")) >= 2 and not _RANK_RE.match(name_part)
            if is_name:
                players.append({"name": name_part, "kills": kills if kills is not None else 0})
            elif kills is not None and players and not players[-1].get("_has_kills"):
                # a standalone "N Eliminations" box -> the kills for the last name on the row
                players[-1]["kills"] = kills
                players[-1]["_has_kills"] = True
        for p in players:
            p.pop("_has_kills", None)
        return players

    # ── confidence ────────────────────────────────────────────────────────────
    def _confidence(self, boxes: list[dict], result: dict, event_type: str) -> dict:
        """Per-field + structural confidence the gate reads. Honest + conservative: a sparse
        or structurally odd read returns low confidence so the router defers to Gemini."""
        scores = [b["score"] for b in boxes]
        mean_score = float(np.mean(scores)) if scores else 0.0
        min_score = float(np.min(scores)) if scores else 0.0
        placements = result.get("placements", [])
        per_counts = [len(p["players"]) for p in placements]
        n_players = sum(per_counts)
        named = sum(1 for p in placements for pl in p["players"] if pl.get("name"))
        named_frac = (named / n_players) if n_players else 0.0

        # STRUCTURAL HONESTY: the recognizer score can be high while the STRUCTURING is wrong
        # (two-column rows merged -> placements with 7-14 players). Flag that so the router
        # defers to Gemini instead of committing a mangled draft. A team placement should hold
        # at most 4 players; a structurally-sound team read has a tight, low players-per-row.
        cap = _MAX_PLAYERS_PER_PLACEMENT.get(event_type, 4)
        oversized = sum(1 for c in per_counts if c > cap)
        oversized_frac = (oversized / len(per_counts)) if per_counts else 1.0
        structural_ok = (
            bool(placements)
            and n_players > 0
            and named_frac >= 0.7
            and oversized_frac <= 0.15          # almost no rows exceed the per-placement cap
            and mean_score >= 0.80
        )
        return {
            # `ok` drives the gate: only a structurally-sound, high-score read is trusted
            # locally; everything else escalates (the designed Gemini-heavy cold start).
            "ok": structural_ok,
            "mean_score": round(mean_score, 4),
            "min_score": round(min_score, 4),
            "n_boxes": len(boxes),
            "n_placements": len(placements),
            "n_players": n_players,
            "named_frac": round(named_frac, 4),
            "oversized_frac": round(oversized_frac, 4),
            "max_players_in_placement": max(per_counts) if per_counts else 0,
        }
