"""
afc_ocr/services/training_capture.py
─────────────────────────────────────

OCR learning loop, Phase 1: turn every admin-committed OCR review into durable
training data.

WHY THIS EXISTS
    AFC reads Free Fire match-result screenshots with Gemini (the "teacher"), then
    an admin reviews and corrects the result before it hits the leaderboard. At the
    moment of commit we therefore hold a perfectly labeled example: the screenshot
    plus the admin-confirmed truth of what it says. This module captures that pair so
    later phases can train a self-hosted "student" OCR model and stop paying Gemini
    per screenshot. Phase 1 only CAPTURES the labels (and the screenshot bytes); it
    does not train anything.

WHO CALLS IT
    afc_ocr.views.commit_ocr_session, AFTER the leaderboard write
    (commit_team_result / commit_solo_result) and the alias/team-note writes have
    already succeeded. Capture runs last and is fully isolated in a try/except: a
    failure here MUST NEVER roll back or break a real leaderboard commit. The worst
    a bug here can do is skip one training example.

WHAT IT CONSUMES (how it connects to the existing OCR path)
    - session:    the OCRSession being committed. We read session.raw_output (the
                  Gemini engine output), session.image (the persisted screenshot,
                  an afc_tournament_and_scrims.MatchResultImage set on upload),
                  session.draft_rows (the ORIGINAL Gemini draft, to diff against),
                  session.event_type, session.match, session.created_by.
    - final_rows: the admin-confirmed review rows (same shape matching.match_name
                  produces: row_id, raw_name, matched_user_id, kills, placement,
                  and the optional corrected_text added in this phase).
    - image_bytes (optional): the raw screenshot bytes if the caller still has them
                  in scope (upload path). If omitted we re-read them from
                  session.image's file.

WHAT IT PRODUCES
    - One OCRTrainingPair (source='admin_review') with final_json built from
      final_rows, content-addressed image stored under media/ocr_training/<sha>.<ext>,
      plus difficulty signals (num_corrections / is_clean) from the draft-vs-final diff.
    - Per player row: one OCRCropLabel(field='name') whose `text` is the
      recognition-truth (corrected_text if the admin edited the read name, else the
      original raw_name) and whose matched_user_id is the identity-truth, and one
      OCRCropLabel(field='kills') whose `text` is the digit string of the kills.
      crop_path is left '' (the offline layout cropper fills it in a later phase).

RECOGNITION-TRUTH vs IDENTITY-TRUTH (the core modeling rule, see models.py header)
    `text`            = what the PIXELS say  -> the OCR/CTC target.
    `matched_user_id` = WHO it resolves to   -> metadata, kept separate.
    We deliberately store the read string, not the matched username, so the student
    learns to transcribe pixels rather than to guess the roster.
"""

import hashlib
import logging
import os

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

# Where deduped training screenshots live, relative to MEDIA_ROOT / the configured
# storage backend. We go through Django's default_storage (not raw open()) so this
# works identically on local disk and on S3 in production.
TRAINING_IMAGE_DIR = "ocr_training"


def _resolve_image_bytes(session, image_bytes):
    """
    Return the raw screenshot bytes for this session, or None if unavailable.

    Preference order:
      1. image_bytes passed by the caller (the upload path still has them in scope,
         so we avoid a redundant re-read).
      2. Re-read from session.image (the persisted MatchResultImage) file.

    Returns None (never raises) when nothing is readable, so capture degrades to a
    no-op instead of breaking the commit.
    """
    if image_bytes:
        return image_bytes

    stored = getattr(session, "image", None)
    if stored is None or not getattr(stored, "image", None):
        # No screenshot was persisted for this session (e.g. an older session created
        # before the image FK existed). Nothing to capture; caller logs and moves on.
        return None

    try:
        with stored.image.open("rb") as f:
            return f.read()
    except Exception as exc:
        logger.warning("training_capture: could not re-read session image: %s", exc)
        return None


def _guess_extension(session) -> str:
    """
    Best-effort file extension for the deduped copy, derived from the persisted
    image's name. The student trainer reads bytes, so the extension is cosmetic; we
    just keep something sensible. Defaults to .jpg.
    """
    stored = getattr(session, "image", None)
    if stored is not None and getattr(stored, "image", None):
        name = (stored.image.name or "").lower()
        for ext in (".png", ".webp", ".jpeg", ".jpg"):
            if name.endswith(ext):
                # Normalize .jpeg -> .jpg for a single canonical extension.
                return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _recognition_text(row) -> str:
    """
    The recognition-truth (what the pixels say) for a row's NAME cell.

    = corrected_text when the admin edited the read on-screen name during review,
      else the original raw_name. Backward compatible: rows captured before the
      corrected_text field existed simply fall back to raw_name.
    """
    corrected = row.get("corrected_text")
    if corrected is not None and str(corrected).strip() != "":
        return str(corrected)
    return row.get("raw_name", "") or ""


def _build_final_json(final_rows: list) -> dict:
    """
    Reshape the flat admin-confirmed review rows into the canonical recognition
    truth the rest of the loop speaks:

        {"placements": [
            {"placement": 1, "players": [{"name": "<read text>", "kills": 3}, ...]},
            ...
        ]}

    `name` here is recognition-truth (corrected_text or raw_name), NOT the matched
    username, on purpose (read the pixels, do not encode the roster).
    """
    from collections import defaultdict

    groups = defaultdict(list)
    for row in final_rows:
        placement = int(row.get("placement", 0) or 0)
        groups[placement].append({
            "name":  _recognition_text(row),
            "kills": int(row.get("kills", 0) or 0),
        })

    return {
        "placements": [
            {"placement": placement, "players": players}
            for placement, players in sorted(groups.items())
        ]
    }


def _count_corrections(final_rows: list, original_rows: list) -> int:
    """
    How many review rows the admin changed versus the original Gemini draft.

    A row counts as corrected if the admin changed any of:
      - matched_user_id (identity correction),
      - kills           (stat correction),
      - the read text    (corrected_text differs from the original raw_name).
    Matched to draft rows by row_id (the stable id matching.match_name assigns).
    Used for num_corrections / is_clean (is_clean == zero corrections == Gemini
    nailed the whole screen).
    """
    original_map = {r.get("row_id"): r for r in (original_rows or [])}
    count = 0

    for row in final_rows:
        orig = original_map.get(row.get("row_id"))
        if not orig:
            # No matching draft row (e.g. admin-added row): treat as a correction.
            count += 1
            continue

        changed = False

        # Identity change.
        if row.get("matched_user_id") != orig.get("matched_user_id"):
            changed = True

        # Kills change (compare as ints, tolerating string/None inputs).
        if int(row.get("kills", 0) or 0) != int(orig.get("kills", 0) or 0):
            changed = True

        # Read-text change: the admin edited the on-screen name. The original draft
        # has no corrected_text, so the baseline read is its raw_name.
        final_text = _recognition_text(row)
        orig_text = orig.get("raw_name", "") or ""
        if final_text != orig_text:
            changed = True

        if changed:
            count += 1

    return count


def capture_training_pair(session, final_rows: list, image_bytes: bytes = None):
    """
    Capture one admin-confirmed OCR review as training data.

    Called from commit_ocr_session AFTER the leaderboard commit succeeds. Wrapped
    in its own try/except so capturing training data can NEVER break a leaderboard
    commit (worst case: one example is skipped and logged).

    Steps:
      1. Resolve the screenshot bytes (arg, else re-read session.image).
      2. Content-address: sha256(bytes); copy to media/ocr_training/<sha>.<ext>,
         skipping the copy if that content already exists (dedupe).
      3. Build final_json (placements grouped, recognition-truth names + kills).
      4. Diff draft vs final for num_corrections / is_clean.
      5. Write the OCRTrainingPair, then one OCRCropLabel per name cell and one per
         kill cell (crop_path left '' for the offline cropper).

    Returns the created OCRTrainingPair, or None if capture was skipped/failed.
    """
    # Imported here (not at module top) to keep this service import-light and avoid
    # any app-loading order surprises, mirroring how commit.py imports models lazily.
    from afc_ocr.models import OCRTrainingPair, OCRCropLabel, _assign_split

    try:
        # ── 1. Resolve the exact screenshot bytes ────────────────────────────
        raw = _resolve_image_bytes(session, image_bytes)
        if not raw:
            # No bytes available -> nothing to learn from. Not an error: older
            # sessions (pre image-FK) simply skip capture.
            logger.info(
                "training_capture: no image bytes for session %s; skipping capture.",
                getattr(session, "session_id", "?"),
            )
            return None

        # ── 2. Content-address + dedupe into media/ocr_training/ ─────────────
        image_sha256 = hashlib.sha256(raw).hexdigest()
        ext = _guess_extension(session)
        # Path is fully determined by the content hash, so identical screenshots
        # collapse to one file and one stable training-set location.
        rel_path = f"{TRAINING_IMAGE_DIR}/{image_sha256}{ext}"

        if not default_storage.exists(rel_path):
            # Skip the write when the content is already present (dedupe). Going
            # through default_storage keeps this correct on both local disk and S3.
            default_storage.save(rel_path, ContentFile(raw))

        # ── 3. Build the admin-confirmed recognition truth for the whole screen
        final_json = _build_final_json(final_rows)

        # ── 4. Difficulty signals from the draft-vs-final diff ───────────────
        original_rows = getattr(session, "draft_rows", None) or []
        num_corrections = _count_corrections(final_rows, original_rows)
        is_clean = num_corrections == 0

        # Which engine produced raw_output. Today it is always Gemini; we read the
        # model name from the gemini service so this stays correct if it changes,
        # and fall back to None (the field is nullable) rather than guessing.
        teacher_model = None
        try:
            from afc_ocr.services.gemini import GEMINI_MODEL
            teacher_model = GEMINI_MODEL
        except Exception:
            teacher_model = None

        # ── 5a. The screen-level pair ────────────────────────────────────────
        pair = OCRTrainingPair.objects.create(
            session=session,
            match=getattr(session, "match", None),
            image_sha256=image_sha256,
            image_path=rel_path,
            event_type=getattr(session, "event_type", "team"),
            raw_output=getattr(session, "raw_output", None) or {},
            final_json=final_json,
            source="admin_review",
            teacher_model=teacher_model,
            num_corrections=num_corrections,
            edit_distance=0,  # reserved for a later phase (per-char distance scoring)
            is_clean=is_clean,
            split=_assign_split(image_sha256),
            created_by=getattr(session, "created_by", None),
        )

        # ── 5b. Per-cell text labels (what the student model trains on) ──────
        crop_labels = []
        for row in final_rows:
            placement = int(row.get("placement", 0) or 0)

            # Name cell: recognition-truth text + identity-truth user id (separate).
            crop_labels.append(OCRCropLabel(
                pair=pair,
                crop_path="",  # filled later by the offline layout cropper
                field="name",
                text=_recognition_text(row),
                placement=placement,
                matched_user_id=row.get("matched_user_id"),
            ))
            # Kills cell: recognition-truth = the digit string. No identity.
            crop_labels.append(OCRCropLabel(
                pair=pair,
                crop_path="",
                field="kills",
                text=str(int(row.get("kills", 0) or 0)),
                placement=placement,
                matched_user_id=None,
            ))

        if crop_labels:
            OCRCropLabel.objects.bulk_create(crop_labels)

        logger.info(
            "training_capture: captured pair %s (split=%s, corrections=%d, crops=%d).",
            pair.pair_id, pair.split, num_corrections, len(crop_labels),
        )
        return pair

    except Exception as exc:
        # HARD RULE: never propagate. Capturing training data must not break a
        # leaderboard commit. Log loudly and return None.
        logger.exception(
            "training_capture: failed to capture training pair for session %s: %s",
            getattr(session, "session_id", "?"), exc,
        )
        return None
