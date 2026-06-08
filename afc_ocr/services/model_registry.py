"""
afc_ocr/services/model_registry.py
================================================================================
OCR learning loop, Phase 4 (part 1 of 2): the BLUE-GREEN MODEL SWAP for the
self-hosted "student" OCR model.

WHY THIS EXISTS
    P1-P3 capture admin-confirmed training data and train a student recognizer
    OFF-BOX (on a GPU we do not run in production). The trained bundle (a fine-tuned
    rec.onnx + rec_keys.txt + VERSION) is then dropped onto the box under
    media/models/student_v<N>/. This module is the only thing that decides WHICH of
    those bundles is "live", and it flips that choice ATOMICALLY and INSTANTLY:
      - blue  = the bundle currently serving traffic,
      - green = the freshly deployed candidate bundle.
    promote() swaps blue->green by re-pointing a single 'current' pointer; rollback()
    swaps it straight back. No Django restart, no request dropped, no half-written
    pointer ever observed (write-temp-then-rename).

HOW IT CONNECTS TO THE REST OF THE OCR PATH
    - Producer of bundles: the off-box trainer (P2/P3) + ops, who place
      media/models/student_v<N>/ on disk. This module never trains; it only points.
    - Consumer of the active bundle: afc_ocr.services.local_ocr.LocalOCREngine, whose
      constructor already accepts a model_dir and loads <model_dir>/rec.onnx +
      rec_keys.txt + VERSION (see local_ocr.LocalOCREngine.__init__). The wiring that
      makes get_engine() pass active_model_dir() into that constructor is owned by the
      local_ocr maintainer (we do NOT edit local_ocr.py here). What we own is:
        (a) resolving the pointer -> active_model_dir(), the single source of truth
            for "which bundle is live" that get_engine() should consult, and
        (b) the reload SIGNAL: after a swap we reset local_ocr._ENGINE to None so the
            very next get_engine() rebuilds the singleton against the new pointer,
            picking up the new model WITHOUT a process restart.
    - Caller of promote()/rollback()/status: the manage.py command
      afc_ocr/management/commands/ocr_model.py (admin-driven), and potentially the
      retrain pipeline once an off-box run is validated. record_shadow() is called from
      the OCR router/commit path (local_ocr maintainer) to log how a shadow student read
      compared to what the admin actually committed, feeding promotion decisions.

COLD START (no model deployed)
    active_model_dir() returns None when nothing has been promoted yet. That is the
    DESIGNED cold-start state: with model_dir=None the router stays Gemini-only (the
    student handles 0% until the first bundle is promoted). None is a first-class,
    expected return value, not an error.

WHY A POINTER FILE (and not always a symlink)
    On POSIX we use an atomic symlink swap. On Windows, symlink creation needs elevated
    privileges or developer mode, so we fall back to a plain pointer FILE whose contents
    are the target bundle's directory name. Both are flipped with write-temp-then-rename
    so a reader never sees a partially written pointer (os.replace is atomic on the same
    filesystem on both platforms). The reader (active_model_dir) understands both forms.

EVERYTHING HERE IS LOCAL / GITIGNORED
    media/models/ (the bundles, the 'current' pointer, 'current.prev') and media/ocr_shadow/
    (shadow logs) are all gitignored (see backend/.gitignore). None of it is ever pushed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Layout under MEDIA_ROOT. Kept as helpers (not module constants) so tests and
# alternate deployments resolve against the CURRENT settings.MEDIA_ROOT rather than
# a value frozen at import time.
# ──────────────────────────────────────────────────────────────────────────────
#   media/models/                      <- registry root
#   media/models/current               <- the active-model pointer (symlink OR file)
#   media/models/current.prev          <- previous pointer target, for instant rollback()
#   media/models/student_v<N>/         <- a deployed student bundle
#       rec.onnx, rec_keys.txt, VERSION
#   media/ocr_shadow/shadow.log.jsonl  <- append-only shadow comparison log
_MODELS_SUBDIR = "models"
_SHADOW_SUBDIR = "ocr_shadow"
_POINTER_NAME = "current"
_PREV_POINTER_NAME = "current.prev"
_BUNDLE_PREFIX = "student_v"


def _models_root() -> str:
    """Absolute path to media/models/, created on demand. This is the registry root
    where bundles live and where the 'current' pointer is flipped."""
    root = os.path.join(settings.MEDIA_ROOT, _MODELS_SUBDIR)
    os.makedirs(root, exist_ok=True)
    return root


def _pointer_path() -> str:
    """Absolute path to the 'current' pointer (media/models/current). On POSIX this is
    usually a symlink to a bundle dir; on Windows it is a small text file holding the
    bundle's directory name. active_model_dir() resolves either form."""
    return os.path.join(_models_root(), _POINTER_NAME)


def _prev_pointer_path() -> str:
    """Absolute path to the saved previous pointer (media/models/current.prev). promote()
    snapshots the old target here so rollback() can flip back in one step."""
    return os.path.join(_models_root(), _PREV_POINTER_NAME)


def bundle_dir_for(version) -> str:
    """Absolute path to the bundle dir for a given version, e.g. student_v9.

    `version` may be an int (9), a bare string ("9"), or already-prefixed
    ("student_v9"); all normalize to media/models/student_v9. Centralised so the
    naming convention lives in exactly one place."""
    name = str(version)
    if not name.startswith(_BUNDLE_PREFIX):
        name = f"{_BUNDLE_PREFIX}{name}"
    return os.path.join(_models_root(), name)


# ──────────────────────────────────────────────────────────────────────────────
# READ SIDE: resolve the pointer to the active bundle dir (or None on cold start).
# ──────────────────────────────────────────────────────────────────────────────
def active_model_dir() -> str | None:
    """Resolve media/models/current to the absolute path of the live student bundle,
    or None when no model is deployed yet (cold start -> router stays Gemini-only).

    Handles BOTH pointer forms transparently:
      - symlink (POSIX): follow it to the real bundle dir.
      - pointer file (Windows / fallback): read the bundle dir NAME it contains and
        resolve it under media/models/.

    Returns None (never raises) when the pointer is missing, dangling, or points at a
    directory that no longer exists. A None here is normal and expected, so callers
    (get_engine) can treat "no model" as "use Gemini" without special-casing errors.
    """
    pointer = _pointer_path()

    try:
        # ── symlink form (POSIX) ────────────────────────────────────────────
        # islink() is true only for an actual symlink. os.path.realpath resolves it;
        # we then confirm the target really exists (a dangling symlink -> cold start).
        if os.path.islink(pointer):
            target = os.path.realpath(pointer)
            return target if os.path.isdir(target) else None

        # ── pointer-file form (Windows / fallback) ──────────────────────────
        # A regular file whose single line is the bundle dir name (e.g. "student_v9").
        if os.path.isfile(pointer):
            with open(pointer, encoding="utf-8") as f:
                name = f.read().strip()
            if not name:
                return None
            # Resolve relative to media/models/. We store only the NAME (not an absolute
            # path) so the registry is relocatable: moving MEDIA_ROOT does not break it.
            target = os.path.join(_models_root(), os.path.basename(name))
            return target if os.path.isdir(target) else None

        # No pointer at all -> nothing deployed yet (cold start).
        return None
    except OSError as exc:
        # Defensive: a filesystem hiccup must not crash the OCR path. Degrade to None
        # (= "no model, use Gemini") and log, rather than propagate.
        logger.warning("model_registry.active_model_dir: could not resolve pointer: %s", exc)
        return None


def active_version() -> str | None:
    """Read the VERSION file of the live bundle (its human-readable version string), or
    None if no model is active or the bundle has no VERSION file. Used by --status and
    shadow logging to tag which student produced a read."""
    d = active_model_dir()
    if not d:
        return None
    ver_file = os.path.join(d, "VERSION")
    try:
        if os.path.isfile(ver_file):
            return open(ver_file, encoding="utf-8").read().strip() or None
    except OSError:
        pass
    # Fall back to the directory name (student_v9 -> "9") when VERSION is absent.
    base = os.path.basename(d.rstrip(os.sep))
    return base[len(_BUNDLE_PREFIX):] if base.startswith(_BUNDLE_PREFIX) else base


# ──────────────────────────────────────────────────────────────────────────────
# WRITE SIDE: the atomic pointer flip + the in-process engine reload signal.
# ──────────────────────────────────────────────────────────────────────────────
def _current_target_name() -> str | None:
    """The bundle dir NAME the 'current' pointer points at right now (e.g. "student_v8"),
    or None if no pointer exists. Used to snapshot the previous target before a swap so
    rollback() knows where to return to."""
    pointer = _pointer_path()
    try:
        if os.path.islink(pointer):
            return os.path.basename(os.path.realpath(pointer).rstrip(os.sep))
        if os.path.isfile(pointer):
            name = open(pointer, encoding="utf-8").read().strip()
            return os.path.basename(name) if name else None
    except OSError:
        return None
    return None


def _atomic_write_pointer(target_name: str) -> None:
    """Point 'current' at the bundle dir named `target_name`, ATOMICALLY.

    Atomicity is the whole point of blue-green: a concurrent request resolving the
    pointer must see EITHER the old bundle OR the new one, never a half-written value.
    We achieve that by writing a temp pointer next to the real one and then os.replace()
    onto it, which is an atomic rename on the same filesystem on both POSIX and Windows.

    Per the design we use a SYMLINK on POSIX (os.name == 'posix') and a pointer FILE on
    Windows (os.name == 'nt'). Choosing by platform up front (rather than try-symlink-then-
    fall-back) matters on Windows: even when this box happens to permit symlink CREATION,
    os.replace() onto an EXISTING symlink raises WinError 5, so re-promoting would fail. The
    pointer-file form sidesteps that entirely. On POSIX, os.replace onto an existing symlink
    is fine, so the symlink swap stays truly atomic there.

    Both forms: write the new pointer to a temp name beside 'current', then os.replace() onto
    'current' (atomic rename on the same filesystem). If even that replace is refused because
    the destination is a leftover symlink/reparse point (rare Windows edge), we remove the
    destination and rename (a tiny non-atomic window, strictly better than failing the swap).
    """
    pointer = _pointer_path()
    tmp = pointer + ".tmp"

    # Clean any stale temp from a previously interrupted swap.
    if os.path.lexists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass

    use_symlink = (os.name == "posix")

    # Build the temp pointer in the chosen form.
    if use_symlink:
        try:
            os.symlink(target_name, tmp)        # relative symlink: 'current' -> student_v9
        except (OSError, NotImplementedError, AttributeError):
            # POSIX symlink unexpectedly refused -> degrade to the portable file form.
            use_symlink = False
    if not use_symlink:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(target_name + "\n")

    # Atomic flip, with a Windows-safe fallback if replacing an existing pointer is refused.
    try:
        os.replace(tmp, pointer)
    except OSError:
        # Destination exists as something os.replace won't overwrite atomically (Windows
        # symlink/reparse edge). Remove it then rename; the gap is microscopic and the
        # alternative is a failed promotion.
        if os.path.lexists(pointer):
            try:
                os.remove(pointer)
            except OSError:
                pass
        os.replace(tmp, pointer)

    logger.info(
        "model_registry: pointer %s -> %s",
        "symlinked" if use_symlink else "file written", target_name,
    )


def _reset_engine() -> None:
    """Signal the in-process LocalOCREngine to rebuild on next use.

    The engine is a lazy module-level singleton (local_ocr._ENGINE): the first
    get_engine() builds it once per worker, then every request reuses it. After a swap
    that cached singleton still holds the OLD model in memory, so we reset the singleton
    to None. The NEXT get_engine() then rebuilds it (and, once the local_ocr maintainer
    wires get_engine() to read active_model_dir(), rebuilds it against the NEW bundle) -
    all WITHOUT a Django/worker restart.

    Import is local + best-effort: in a process that never loaded the engine (or where
    the optional onnx deps are absent) there is simply nothing cached to reset, and a
    failure here must never abort the pointer flip (the flip already succeeded on disk;
    worst case a stale worker serves the old model until its next natural reload)."""
    try:
        from afc_ocr.services import local_ocr
        local_ocr._ENGINE = None
        logger.info("model_registry: local_ocr._ENGINE reset; engine will rebuild on next get_engine().")
    except Exception as exc:  # noqa: BLE001 - never let a reload hiccup undo a successful flip
        logger.warning("model_registry: could not reset local_ocr engine (will reload naturally): %s", exc)


def promote(version) -> str:
    """Make student_v<version> the LIVE model (blue-green green-cut).

    Steps, in order, so the system is always in a consistent state:
      1. Validate the bundle exists at media/models/student_v<version>/. We do not flip
         to a bundle that is not on disk (that would dark-out OCR). Raises FileNotFoundError
         with a clear message if missing (the manage.py command surfaces it to the admin).
      2. Snapshot the CURRENT pointer target into current.prev BEFORE flipping, so
         rollback() can return to it in one step. (If nothing was live, prev is cleared.)
      3. Atomically flip 'current' -> student_v<version> (write-temp-then-rename).
      4. Reset the in-process engine so the new model is picked up without a restart.

    Returns the absolute path of the now-active bundle. Idempotent: promoting the
    already-live version simply re-points to the same place (safe to retry).
    """
    target = bundle_dir_for(version)
    if not os.path.isdir(target):
        raise FileNotFoundError(
            f"Cannot promote: bundle directory does not exist: {target}. "
            f"Deploy the off-box student bundle (rec.onnx + rec_keys.txt + VERSION) there first."
        )
    target_name = os.path.basename(target.rstrip(os.sep))

    # ── 2. snapshot the previous target for instant rollback ────────────────
    prev_name = _current_target_name()
    _write_prev_pointer(prev_name)  # clears prev when prev_name is None (cold start)

    # ── 3. atomic flip ──────────────────────────────────────────────────────
    _atomic_write_pointer(target_name)

    # ── 4. in-process reload signal ─────────────────────────────────────────
    _reset_engine()

    logger.info("model_registry.promote: now serving %s (previous=%s).", target_name, prev_name)
    return target


def rollback() -> str | None:
    """Instantly revert to the previously live model (blue-green blue-cut).

    Reads current.prev (saved by the last promote) and flips 'current' back to it. This
    is the safety valve: if a freshly promoted student regresses, one rollback() returns
    to the known-good bundle in O(1), again with no restart.

    Behaviour:
      - prev points at a bundle that still exists -> flip to it, reset engine, return its path.
      - prev is empty/missing OR its bundle is gone -> there is nothing safe to fall back to,
        so we CLEAR 'current' entirely (cold start: router reverts to Gemini-only) and
        return None. Clearing is the conservative choice: better Gemini-only than serving a
        bundle we just decided was bad.
    """
    prev_name = _read_prev_pointer()

    # Nothing to roll back to, or the previous bundle no longer exists on disk:
    # clear the pointer -> cold start (Gemini-only), which is always safe.
    if not prev_name or not os.path.isdir(os.path.join(_models_root(), prev_name)):
        _clear_pointer()
        _reset_engine()
        logger.info("model_registry.rollback: no valid previous model; cleared pointer (cold start / Gemini-only).")
        return None

    # Flip back to the previous bundle. We do NOT re-snapshot prev here (rollback is a
    # one-step undo of the last promote, not a new generation), so a second rollback is a
    # no-op rather than a confusing ping-pong.
    _atomic_write_pointer(prev_name)
    _reset_engine()
    target = os.path.join(_models_root(), prev_name)
    logger.info("model_registry.rollback: reverted to %s.", prev_name)
    return target


# ── prev-pointer + clear helpers (small, single-purpose, write-temp-then-rename) ──
def _write_prev_pointer(name: str | None) -> None:
    """Persist the previous target name into current.prev (or clear it when name is None).
    Written atomically so a crash mid-promote never leaves a torn prev pointer."""
    prev = _prev_pointer_path()
    if not name:
        # Nothing was live before this promote (cold start). Clear prev so a later
        # rollback correctly resolves to "no previous model".
        if os.path.exists(prev):
            try:
                os.remove(prev)
            except OSError:
                pass
        return
    tmp = prev + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(name + "\n")
    os.replace(tmp, prev)


def _read_prev_pointer() -> str | None:
    """Read the saved previous-target name from current.prev, or None if absent/empty."""
    prev = _prev_pointer_path()
    try:
        if os.path.isfile(prev):
            return open(prev, encoding="utf-8").read().strip() or None
    except OSError:
        pass
    return None


def _clear_pointer() -> None:
    """Remove the 'current' pointer entirely (symlink or file). Resolves the registry
    back to cold start (active_model_dir() -> None -> Gemini-only)."""
    pointer = _pointer_path()
    try:
        if os.path.lexists(pointer):
            os.remove(pointer)
    except OSError as exc:
        logger.warning("model_registry._clear_pointer: could not remove pointer: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# SHADOW COMPARISON: log how a (shadow) student read compared to the admin commit.
# ──────────────────────────────────────────────────────────────────────────────
def _shadow_log_path() -> str:
    """Absolute path to the append-only shadow log (media/ocr_shadow/shadow.log.jsonl).
    JSONL so each comparison is one self-contained line, cheap to append and to tail."""
    d = os.path.join(settings.MEDIA_ROOT, _SHADOW_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "shadow.log.jsonl")


def _placements_to_pairs(result_json: dict) -> set:
    """Flatten a result JSON ({"placements":[{placement, players:[{name,kills}]}]}) into a
    set of (placement, name, kills) tuples, for an order-insensitive exact-match compare.
    Defensive against missing/odd keys so a malformed shadow read never crashes logging."""
    pairs = set()
    for pl in (result_json or {}).get("placements", []) or []:
        placement = pl.get("placement")
        for player in pl.get("players", []) or []:
            pairs.add((placement, (player.get("name") or "").strip(), int(player.get("kills") or 0)))
    return pairs


def record_shadow(session, student_json: dict, committed_json: dict) -> dict:
    """Log how the SHADOW student output compared to what the admin actually committed.

    "Shadow" = the student model runs alongside the live path WITHOUT affecting the draft
    the admin sees; we record its read so we can measure, over many real screenshots,
    whether a candidate student is good enough to PROMOTE. This is the evidence
    promotion decisions are made on (exact-match rate, near-misses), gathered from real
    admin-confirmed truth at zero risk to the live commit.

    What it consumes:
      - session:        the OCRSession being committed (for ids + which student version ran).
      - student_json:   the student engine's read, in call_gemini's result shape.
      - committed_json: the admin-confirmed truth (same shape; what training_capture also
                        stores as final_json).

    What it writes: one JSONL line to media/ocr_shadow/shadow.log.jsonl with the match
    counts and the active student version. Fully isolated in try/except: shadow logging
    must NEVER break a commit (mirrors training_capture's hard rule). Returns the computed
    stats dict (also handy for tests), or an empty dict on failure.
    """
    try:
        student_pairs = _placements_to_pairs(student_json)
        truth_pairs = _placements_to_pairs(committed_json)

        # Exact (placement, name, kills) agreement, order-insensitive.
        matched = student_pairs & truth_pairs
        n_truth = len(truth_pairs)
        n_student = len(student_pairs)
        # Recall-style score: fraction of the admin-confirmed rows the student got exactly
        # right. 1.0 only when the student reproduced every committed row verbatim.
        exact_match = (len(matched) / n_truth) if n_truth else None

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": str(getattr(session, "session_id", "") or ""),
            "match_id": getattr(session, "match_id", None),
            "event_type": getattr(session, "event_type", None),
            "student_version": active_version(),
            "n_truth_rows": n_truth,
            "n_student_rows": n_student,
            "n_matched_rows": len(matched),
            "exact_match": round(exact_match, 4) if exact_match is not None else None,
            # is_perfect = the student would have needed zero admin corrections on this
            # screen. The running rate of is_perfect across the log is the headline
            # promotion metric.
            "is_perfect": (matched == truth_pairs and n_truth > 0),
        }

        with open(_shadow_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        return record
    except Exception as exc:  # noqa: BLE001 - shadow logging must never break a commit
        logger.warning("model_registry.record_shadow: failed to log shadow comparison: %s", exc)
        return {}


def recent_shadow_stats(limit: int = 200) -> dict:
    """Summarise the last `limit` shadow comparisons for the --status command.

    Reads the tail of the JSONL log and aggregates: how many comparisons, the mean
    exact-match rate, and how many were perfect (zero corrections needed). Returns a
    small dict; empty/zeroed when there is no log yet (cold start). Never raises -
    --status must always print something sensible."""
    path = _shadow_log_path()
    records: list[dict] = []
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                # Read all lines then keep the tail; the shadow log is small (one line per
                # committed screenshot) so this is cheap and avoids a seek dance.
                lines = f.readlines()
            for line in lines[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip a torn line rather than fail the whole summary
    except OSError as exc:
        logger.warning("model_registry.recent_shadow_stats: could not read shadow log: %s", exc)

    n = len(records)
    if not n:
        return {"count": 0, "mean_exact_match": None, "perfect_count": 0, "perfect_rate": None}

    exacts = [r["exact_match"] for r in records if r.get("exact_match") is not None]
    perfect = sum(1 for r in records if r.get("is_perfect"))
    return {
        "count": n,
        "mean_exact_match": round(sum(exacts) / len(exacts), 4) if exacts else None,
        "perfect_count": perfect,
        "perfect_rate": round(perfect / n, 4) if n else None,
    }
