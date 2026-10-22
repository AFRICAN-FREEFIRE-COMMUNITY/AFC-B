"""
afc_ocr/services/local_ocr.py
================================================================================
Self-hosted, CPU-only OCR engine for AFC Free Fire result screenshots. This is the
"student" half of the design: a local model that reads the screenshot WITHOUT calling
Gemini, so the platform keeps working at zero API cost. Gemini is only a teacher
(bootstrap labels, services/training_capture + tasks) and a confidence-gated fallback
(services/ocr_confidence + the router in services/extract.py).

PIPELINE (two stages):
  Stage 1  recognition : PaddleOCR's PP-OCRv6 detector + recognizer as ONNX, run by the
                         `rapidocr` package on onnxruntime (no paddle, no torch on the box).
                         It returns every text box on the screen: (quad, text, score). A
                         portrait file is a sideways screenshot (Free Fire only runs in
                         landscape), so it is turned before it is read.
  Stage 2  structuring : rebuild the result board from those boxes into the SAME JSON shape
                         Gemini returns ({"placements":[{"placement", "players":[{"name",
                         "kills"}]}]}). See _structure for how the board is laid out.

WHY STAGE 2 WAS REWRITTEN (22 Oct 2026, inbox #328): measured on the 12 admin-confirmed
screenshots in production, the first version put 4% of players in the right team. It took one
text line for one team, but every team on the board is a 2x2 block over TWO lines, and it took
every word for a player ("SEN" and "TOKYO" as two people). The confidence gate still trusted
two of those screens. Evaluation, data and numbers: WEBSITE/tasks/ocr-eval-2026-10-22/ and
HANDOVER-2026-10-16.md section 45.

The recognizer can be swapped for a FINE-TUNED one (off-box training, afc_ocr/training) by
promoting a bundle through model_registry: a custom rec.onnx + rec_keys.txt; the detector and
the structuring are unchanged.

CONTRACT (drop-in for services/gemini.call_gemini, plus an event_type arg + a confidence
return): LocalOCREngine.run(image_bytes, mime_type, aliases, team_notes, event_type)
  -> (result_json: dict, confidence: dict)
`result_json` matches call_gemini's shape EXACTLY so the draft-row build, match_name,
detect_team_mismatches and the commit path stay byte-for-byte unchanged.
`confidence` carries the structural signals services/ocr_confidence.gate reads to decide
whether to escalate to Gemini.

CONSUMED BY: services/extract.extract_rows (the one local-first router every OCR upload goes
through) via get_engine().run(...), and management/commands/bootstrap_ocr via
_read_oriented / _recognize. The engine is a lazy module-level singleton so the ONNX sessions
load ONCE per worker and serve in-request.
"""

from __future__ import annotations

import logging
import re

import numpy as np

logger = logging.getLogger(__name__)

# "3 Eliminations" / "3Eliminations" / "3 elims" -> a kill count. Kept for older callers;
# the structuring below uses the more tolerant _KILL_CELL_RE.
_KILLS_RE = re.compile(r"(\d+)\s*elimin\w*", re.IGNORECASE)

# One kill cell as Free Fire draws it: the count, then the word "Eliminations". Tolerant of
# what the reader does to it on real screens: O for 0, l / I / | for 1, the word cut short at
# the right edge of the screen ("Eliminatic"), and the NEXT player's name glued straight on
# because the game draws them overlapping ("1EliminationsLORD JA¥"). The tail is lowercase
# only, so a glued-on name that starts with a capital stays part of the name.
_KILL_CELL_RE = re.compile(r"([0-9OoIl|]{0,2})\s*[Ee][Ll1I][Ii1l][Mm][Ii1l][Nn][a-z]{0,6}")
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})

# A bare team number ("4", "10") drawn at the left of a team's block. The top three are
# drawn as badges, which the reader sometimes gives as a digit and sometimes not at all.
_RANK_RE = re.compile(r"^#?\s*(\d{1,2})$")

# Max players in one placement for each event format - a structural sanity check.
_MAX_PLAYERS_PER_PLACEMENT = {"team": 4, "solo": 1}

# Screen chrome that is never a name: the logo above the board and the button below it.
_NOISE_RE = re.compile(r"^(free\s*f[i1l]?re|freefre|back)$", re.IGNORECASE)

# Recognizer choice (measured 22 Oct 2026 on the real screenshots, see the docstring). The
# "small" PP-OCRv6 models ship INSIDE the rapidocr wheel, so the server downloads nothing at
# run time. Two threads: inference is serialized by extract._STUDENT_LOCK, and the API box has
# four cores that gunicorn also needs.
_OCR_VERSION = "PP-OCRv6"
_MODEL_TYPE = "small"
_ORT_THREADS = 2

# Board geometry, in multiples of the median kill-cell height h, measured on the board:
# two lines of one team sit about 1.7h apart, two teams about 2.2h to 2.5h apart (the
# team cut is measured per screen, _NEW_TEAM_GAP is only the fallback); the kill-cell
# slots of one column are 9h or more apart; a name sits level with its kill cell.
_NEW_LINE_GAP = 0.6
_NEW_TEAM_GAP = 2.1
_NEW_SLOT_GAP = 2.5
_ATTACH_GAP = 0.7
_BADGE_HEIGHT = 1.8


# ──────────────────────────────────────────────────────────────────────────────
# Engine singleton - load the ONNX models once per process.
# ──────────────────────────────────────────────────────────────────────────────
_ENGINE = None


def get_engine():
    """Return the process-wide LocalOCREngine, building it on first use (lazy so a worker
    that never does OCR pays nothing, and so import of this module never hard-fails if the
    optional rapidocr/onnxruntime deps are absent - get_engine() raises only when actually
    called).

    Loads the FINE-TUNED student bundle that model_registry currently points at
    (media/models/current), if any; on cold start (no bundle promoted) that resolves to None
    and we use the bundled PP-OCR models. model_registry.promote()/rollback() reset _ENGINE
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
    whether a local-first attempt is even possible; when False it falls straight to Gemini.
    On production this was False until 22 Oct 2026: the packages were only ever in
    requirements-local.txt, never in requirements-prod.txt, which the deploy installs."""
    try:
        import onnxruntime  # noqa: F401
        from rapidocr import RapidOCR  # noqa: F401
        return True
    except Exception:
        return False


class LocalOCREngine:
    """Wraps the PP-OCR ONNX models + the Free Fire result-board structuring. One instance
    per worker (see get_engine)."""

    def __init__(self, model_dir: str | None = None):
        # model_dir lets a fine-tuned bundle override the bundled recognizer + its char dict.
        # When None we use rapidocr's bundled PP-OCR models.
        from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

        self.model_version = f"ppocr_{_OCR_VERSION.lower().replace('-', '')}_{_MODEL_TYPE}_s1"
        params = {
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": _ORT_THREADS,
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion(_OCR_VERSION),
            "Rec.ocr_version": OCRVersion(_OCR_VERSION),
            "Det.model_type": ModelType(_MODEL_TYPE),
            "Rec.model_type": ModelType(_MODEL_TYPE),
        }
        if model_dir:
            # A fine-tuned bundle ships its own rec model + rec dict; the detector stays bundled.
            import os
            rec = os.path.join(model_dir, "rec.onnx")
            keys = os.path.join(model_dir, "rec_keys.txt")
            if os.path.exists(rec):
                params["Rec.model_path"] = rec
            if os.path.exists(keys):
                params["Rec.rec_keys_path"] = keys
            ver = os.path.join(model_dir, "VERSION")
            if os.path.exists(ver):
                try:
                    self.model_version = open(ver, encoding="utf-8").read().strip() or self.model_version
                except OSError:
                    pass
        self._rapid = RapidOCR(params=params)

    def _ocr(self, img):
        """Run the models on one decoded image -> ([(quad, text, score)], None). The same
        return shape the older rapidocr_onnxruntime package had, so the rest of the module
        (and the evaluation, which replays saved boxes through this method) does not care
        which package read it."""
        out = self._rapid(img)
        if out.boxes is None:
            return [], None
        return [(quad.tolist(), text, float(score))
                for quad, text, score in zip(out.boxes, out.txts, out.scores)], None

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
            result, signals = self._structure(boxes, event_type)
            conf = self._confidence(boxes, result, signals, event_type)
            conf["model_version"] = self.model_version
            return result, conf
        except Exception as e:  # defensive: a student failure must never break the upload
            logger.exception("local_ocr.run failed: %s", e)
            return ({"match_type": event_type, "placements": []},
                    {"ok": False, "reason": f"error:{type(e).__name__}", "model_version": self.model_version})

    # ── stage 1: recognition ──────────────────────────────────────────────────
    def _recognize(self, image_bytes: bytes) -> list[dict]:
        """Decode bytes -> turn a sideways screenshot -> run det+rec -> list of
        {text, score, cx, cy, h, x0, x1}. cx/cy = box centre, h = box height, x0/x1 = left and
        right edges (all in the coordinates of the image that was read)."""
        return self._read_oriented(image_bytes)[1]

    def _read_oriented(self, image_bytes: bytes):
        """Return (image_read, boxes), with the screenshot turned upright first.

        Free Fire only runs in landscape, so a portrait file is a screenshot saved sideways (it
        happens with images forwarded on WhatsApp): it is turned a quarter first. That leaves it
        either upright or upside down, and so does a landscape file uploaded upside down (owner,
        22 Oct 2026: "if a screenshot is uploaded sideways or upside down, can the site know and
        then rotate it itself?").

        The reader cannot tell us: it turns every upside-down WORD the right way up on its own,
        so a flipped board reads as clean text in the wrong places. The board layout can: on an
        upright board each player's name sits just LEFT of their kill cell, on a flipped one
        just RIGHT (_upright_margin). Measured on all 29 real screenshots, upright boards score
        clearly positive and flipped ones clearly negative. So: read once; if the board does not
        look clearly upright, read it turned half way too and keep whichever looks more upright.
        An upright upload (nearly all of them) is read once. bootstrap_ocr crops from the
        returned image so its crops line up with the boxes."""
        import cv2
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None, []
        if img.shape[0] > img.shape[1]:
            img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        boxes = self._boxes(img)
        margin, n_kills = self._upright_margin(boxes)
        if margin < max(3, 0.25 * n_kills):
            turned = cv2.rotate(img, cv2.ROTATE_180)
            turned_boxes = self._boxes(turned)
            if self._upright_margin(turned_boxes)[0] > margin:
                return turned, turned_boxes
        return img, boxes

    @staticmethod
    def _upright_margin(boxes: list[dict]) -> tuple[int, int]:
        """(kill cells with a name just to their LEFT) minus (those with a name just to their
        RIGHT), and the number of kill cells. Positive means upright. "Just" is within three
        text heights on the same line. Both happen on an upright board (a name follows the kill
        cell of the player before), but left wins about 3 to 1; flipped, right wins as clearly."""
        kills = [b for b in boxes if _KILL_CELL_RE.search(b["text"])]
        if not kills:
            return 0, 0
        names = [b for b in boxes if not _KILL_CELL_RE.search(b["text"]) and len(b["text"].replace(" ", "")) >= 2]
        h = float(np.median([k["h"] for k in kills]))
        left = right = 0
        for k in kills:
            line = [n for n in names if abs(n["cy"] - k["cy"]) <= 0.6 * h]
            left += any(0 <= k["x0"] - n["x1"] <= 3 * h for n in line)
            right += any(0 <= n["x0"] - k["x1"] <= 3 * h for n in line)
        return left - right, len(kills)

    def _boxes(self, img) -> list[dict]:
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
                "x1": max(xs),
            })
        return out

    # ── stage 2: structuring ──────────────────────────────────────────────────
    def _structure(self, boxes: list[dict], event_type: str) -> tuple[dict, dict]:
        """Rebuild the result board from the text boxes. Returns (result_json, signals).

        How Free Fire draws the board (every screenshot in production has this shape):
          - up to two COLUMNS of teams: 1 to 5 down the left, 6 onwards down the right
            (the right column scrolls, so a second screenshot shows the teams below);
          - each TEAM is a block of two lines, two players per line, each player drawn as
            "NAME   N Eliminations"; a team of one or two players has one line;
          - the team number sits at the left of its block (1 to 3 as badges, which the
            reader may or may not give as a digit).

        So the kill cells are the anchors:
          1. Cut every box into pieces: kill cells, name text, team numbers. One box can hold
             a kill cell AND the next player's name glued on, because the game draws them
             overlapping in the right column.
          2. Kill cells stand in vertical SLOTS (two per column): cluster their x positions,
             pair the slots into columns, and give every other piece to its column.
          3. In each column, cluster the pieces into LINES by height, and the lines into
             TEAMS by the gap (two lines of one team are closer than two teams).
          4. In each line, the name pieces before a kill cell are that player's name (a name
             the reader split into words is joined back).
          5. Number the teams: from the team numbers the reader saw, else 1 for the top of
             the left column and on from the end of the column before.
        """
        signals = {"unknown_kills": 0, "unnamed": 0, "rank_conflicts": 0, "n_kill_cells": 0,
                   "unanchored_columns": 0}
        pieces = [p for b in boxes if not _NOISE_RE.match(b["text"].replace(" ", ""))
                  for p in self._pieces(b)]
        kills = [p for p in pieces if p["kind"] == "kill"]
        signals["n_kill_cells"] = len(kills)
        if not kills:
            return {"match_type": event_type, "placements": []}, signals

        h = float(np.median([k["h"] for k in kills])) or 20.0
        # The board spans the kill cells; the logo above it and the id string / button below it
        # are dropped by position as well as by text.
        top = min(k["cy"] for k in kills) - 1.5 * h
        bottom = max(k["cy"] for k in kills) + 1.5 * h
        pieces = [p for p in pieces if top <= p["cy"] <= bottom]

        columns = self._columns(pieces, kills, h)
        placements, number = [], 0
        for i, col in enumerate(columns):
            teams = self._teams(col, h)
            numbers, conflicts, anchored = self._number(teams, col, h, number)
            signals["rank_conflicts"] += conflicts
            # The right column scrolls: players scroll it to reach team 12, which cuts team 6
            # off the top (owner, 22 Oct 2026), and sometimes only that screenshot is sent. So a
            # right column is only numbered safely from a team number the reader actually saw;
            # without one, counting on from the left column could be off by one.
            if i > 0 and teams and not anchored:
                signals["unanchored_columns"] += 1
            for team, n in zip(teams, numbers):
                players = []
                for line in team:
                    players.extend(self._line_players(line))
                if not players:
                    continue
                for pl in players:
                    if pl["kills"] is None:
                        signals["unknown_kills"] += 1
                    if len(pl["name"].replace(" ", "")) < 2:
                        signals["unnamed"] += 1
                placements.append({"placement": n, "players": [
                    {"name": pl["name"], "kills": pl["kills"] if pl["kills"] is not None else 0}
                    for pl in players]})
            if numbers:
                number = numbers[-1]
        placements.sort(key=lambda p: p["placement"])
        return {"match_type": event_type, "placements": placements}, signals

    @staticmethod
    def _pieces(box: dict) -> list[dict]:
        """Cut one box into kill / name / rank pieces. A piece's x range is the box's range cut
        in proportion to its characters (good enough to order pieces left to right)."""
        text = box["text"]
        n = len(text) or 1
        span = box.get("x1", box["x0"] + box["h"] * n / 2) - box["x0"]

        def piece(kind, s, a, b, **extra):
            return dict(kind=kind, text=s.strip(), x0=box["x0"] + span * a / n,
                        x1=box["x0"] + span * b / n, cy=box["cy"], h=box["h"],
                        score=box["score"], **extra)

        out, pos = [], 0
        for m in _KILL_CELL_RE.finditer(text):
            if text[pos:m.start()].strip():
                out.append(piece("name", text[pos:m.start()], pos, m.start()))
            digits = m.group(1).translate(_DIGIT_FIX)
            out.append(piece("kill", m.group(0), m.start(), m.end(),
                             kills=int(digits) if digits.isdigit() else None))
            pos = m.end()
        if text[pos:].strip():
            out.append(piece("name", text[pos:], pos, n))
        for p in out:
            if p["kind"] == "name" and _RANK_RE.match(p["text"]):
                p["kind"], p["rank"] = "rank", int(_RANK_RE.match(p["text"]).group(1))
        return out

    @staticmethod
    def _columns(pieces: list[dict], kills: list[dict], h: float) -> list[list[dict]]:
        """Split the board into its columns. Kill cells stand in vertical slots, two per
        column; slots are clusters of kill-cell centres more than _NEW_SLOT_GAP * h apart.
        A column ends just after its second slot, so the next column's names (which start
        close to it) are not pulled back."""
        xs = sorted((k["x0"] + k["x1"]) / 2 for k in kills)
        slots = [[xs[0]]]
        for x in xs[1:]:
            if x - slots[-1][-1] > _NEW_SLOT_GAP * h:
                slots.append([x])
            else:
                slots[-1].append(x)
        bounds = []
        for i in range(1, len(slots), 2):
            right = max(k["x1"] for k in kills if slots[i][0] <= (k["x0"] + k["x1"]) / 2 <= slots[i][-1])
            bounds.append(right + 0.5 * h)
        if len(slots) % 2:  # an odd last slot (the column's second slot is off screen)
            bounds.append(float("inf"))
        bounds[-1] = float("inf")
        cols = [[] for _ in bounds]
        for p in pieces:
            cx = (p["x0"] + p["x1"]) / 2
            for i, edge in enumerate(bounds):
                if cx < edge:
                    cols[i].append(p)
                    break
        return cols

    @staticmethod
    def _teams(col: list[dict], h: float) -> list[list[list[dict]]]:
        """Group a column into lines, and lines into teams (at most two lines per team).

        Lines come from the KILL CELLS only: every player has one, level with the name, so they
        mark the lines exactly. Name pieces then join the nearest line; a piece too far from
        every line (a top-3 badge read as text, a name cut at the board edge) joins none, and a
        piece much taller than the text is a badge drawn as a picture and never a name. Building
        lines from every piece instead let one badge read as "E" make a line of its own and
        shift every team below it (measured 22 Oct 2026).

        Teams: the gap between two lines of one team is smaller than the gap between two teams.
        The ratio is the same on every screen but the absolute size is not (it depends on how
        tall the reader draws its boxes), so the split is measured on each screen: the line gaps
        fall into two groups, and the cut sits between them (see _team_gap)."""
        kills = sorted((p for p in col if p["kind"] == "kill"), key=lambda p: p["cy"])
        if not kills:
            return []
        lines = [[kills[0]]]
        for k in kills[1:]:
            if k["cy"] - np.mean([q["cy"] for q in lines[-1]]) > _NEW_LINE_GAP * h:
                lines.append([k])
            else:
                lines[-1].append(k)
        centres = [float(np.mean([q["cy"] for q in ln])) for ln in lines]
        for p in col:
            if p["kind"] != "name" or p["h"] > _BADGE_HEIGHT * h:
                continue
            i = int(np.argmin([abs(c - p["cy"]) for c in centres]))
            if abs(centres[i] - p["cy"]) <= _ATTACH_GAP * h:
                lines[i].append(p)
        cut = LocalOCREngine._team_gap([b - a for a, b in zip(centres, centres[1:])], h)
        teams = [[lines[0]]]
        for ln, prev, cy in zip(lines[1:], centres, centres[1:]):
            if cy - prev > cut or len(teams[-1]) == 2:
                teams.append([ln])
            else:
                teams[-1].append(ln)
        return [[sorted(ln, key=lambda p: p["x0"]) for ln in team] for team in teams]

    @staticmethod
    def _team_gap(gaps: list[float], h: float) -> float:
        """The line gap above which a new team starts, measured on this column. When the gaps
        clearly fall into a small group (lines of one team) and a large one (between teams), cut
        between the two groups at the split that separates them best. When they are all alike
        (every team one line, or one team of two lines), fall back to _NEW_TEAM_GAP * h."""
        if len(gaps) >= 2 and max(gaps) >= 1.25 * min(gaps):
            ordered = sorted(gaps)
            best_i, best_spread = 1, -1.0
            for i in range(1, len(ordered)):
                low, high = ordered[:i], ordered[i:]
                spread = len(low) * len(high) * (np.mean(high) - np.mean(low)) ** 2
                if spread > best_spread:
                    best_i, best_spread = i, spread
            low, high = ordered[:best_i], ordered[best_i:]
            if np.mean(high) >= 1.25 * np.mean(low):
                return (max(low) + min(high)) / 2.0
        return _NEW_TEAM_GAP * h

    @staticmethod
    def _number(teams, col, h, number_before):
        """Placement numbers for one column's teams, top to bottom. A team number the reader
        saw (a rank piece left of the names, level with the team) fixes the column's offset;
        the majority offset wins and every disagreeing number is counted as a conflict. With
        none seen, the column continues from the previous one (the left column starts at 1).
        Returns (numbers, conflicts, anchored)."""
        if not teams:
            return [], 0, False
        names_left = min((p["x0"] for t in teams for ln in t for p in ln if p["kind"] == "name"),
                         default=float("inf"))
        offsets = []
        for r in (p for p in col if p["kind"] == "rank" and p["x1"] <= names_left + 0.5 * h):
            for i, team in enumerate(teams):
                ys = [p["cy"] for ln in team for p in ln]
                if min(ys) - h <= r["cy"] <= max(ys) + h:
                    offsets.append(r["rank"] - i)
                    break
        if offsets:
            offset = max(set(offsets), key=offsets.count)
            conflicts = sum(1 for o in offsets if o != offset)
        else:
            offset, conflicts = number_before + 1, 0
        return [i + offset for i in range(len(teams))], conflicts, bool(offsets)

    @staticmethod
    def _line_players(line: list[dict]) -> list[dict]:
        """One line, left to right: the name pieces before each kill cell are that player's
        name (joined back with a space when the reader split it into words). A name with no
        kill cell after it (cut off at the screen edge) keeps kills=None so the confidence
        sees it; a kill cell with no name before it gets an empty name for the same reason."""
        players, words = [], []
        for p in line:
            if p["kind"] == "kill":
                players.append({"name": " ".join(words).strip(), "kills": p["kills"]})
                words = []
            elif p["kind"] == "name":
                words.append(p["text"])
        if words:
            players.append({"name": " ".join(words).strip(), "kills": None})
        return [pl for pl in players if pl["name"] or pl["kills"] is not None]

    # ── confidence ────────────────────────────────────────────────────────────
    def _confidence(self, boxes: list[dict], result: dict, signals: dict, event_type: str) -> dict:
        """Structural confidence the gate reads. Honest + conservative: anything the board
        model could not account for sends the screen to Gemini. `ok` needs ALL of:
          - a team-format read (the solo board has not been measured, so solo always escalates);
          - every player has a name of two or more characters AND a kill count that parsed;
          - no team holds more than four players;
          - the team numbers the reader saw agree with each other, and the right column
            (which scrolls) was numbered from a team number it saw, not by counting on;
          - placement numbers are unique;
          - a high mean recognition score."""
        scores = [b["score"] for b in boxes]
        mean_score = float(np.mean(scores)) if scores else 0.0
        min_score = float(np.min(scores)) if scores else 0.0
        placements = result.get("placements", [])
        per_counts = [len(p["players"]) for p in placements]
        n_players = sum(per_counts)
        named = n_players - signals.get("unnamed", 0)
        named_frac = (named / n_players) if n_players else 0.0
        cap = _MAX_PLAYERS_PER_PLACEMENT.get(event_type, 4)
        oversized = sum(1 for c in per_counts if c > cap)
        oversized_frac = (oversized / len(per_counts)) if per_counts else 1.0
        numbers = [p["placement"] for p in placements]
        structural_ok = (
            event_type == "team"
            and bool(placements)
            and n_players > 0
            and signals.get("unnamed", 0) == 0
            and signals.get("unknown_kills", 0) == 0
            and oversized == 0
            and signals.get("rank_conflicts", 0) == 0
            and signals.get("unanchored_columns", 0) == 0
            and len(set(numbers)) == len(numbers)
            and mean_score >= 0.80
        )
        reason = None
        if not structural_ok:
            reason = ("solo board not measured" if event_type != "team"
                      else f"unknown_kills={signals.get('unknown_kills', 0)} unnamed={signals.get('unnamed', 0)} "
                           f"oversized={oversized} rank_conflicts={signals.get('rank_conflicts', 0)} "
                           f"unanchored_columns={signals.get('unanchored_columns', 0)}")
        return {
            # `ok` drives the gate: only a read the board model fully accounts for is trusted
            # locally; everything else escalates.
            "ok": structural_ok,
            "reason": reason,
            "mean_score": round(mean_score, 4),
            "min_score": round(min_score, 4),
            "n_boxes": len(boxes),
            "n_placements": len(placements),
            "n_players": n_players,
            "named_frac": round(named_frac, 4),
            "oversized_frac": round(oversized_frac, 4),
            "max_players_in_placement": max(per_counts) if per_counts else 0,
            "unknown_kills": signals.get("unknown_kills", 0),
            "rank_conflicts": signals.get("rank_conflicts", 0),
            "unanchored_columns": signals.get("unanchored_columns", 0),
        }
