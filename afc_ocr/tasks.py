"""
afc_ocr/tasks.py
================================================================================
OCR learning loop, Phase 4 (part 2 of 2): the RETRAIN LOOP background tasks.

Two Celery tasks, both on the dedicated `ocr_ml` queue (mirrors how afc_rankings
puts its work on the `rankings_recalc` queue; run a worker with
`celery -A afc worker -Q ocr_ml` in prod). Beat fires them on a schedule
(see afc/celery_config.py): autolabel nightly, retrain-trigger weekly.

  1. autolabel_backlog()
       NIGHTLY. Mines the screenshots we already stored for matches but never turned
       into training data, and asks Gemini (the "teacher") to label them - but only
       trusts a label when TWO independent Gemini reads AGREE (consensus). Agreement
       -> a SILVER training pair (source='gemini_autolabel', is_clean=True). Disagreement
       -> a flagged pair (is_clean=False) routed to admin review instead of silently
       trusting one noisy read. This grows the corpus between the (rarer) GOLD pairs an
       admin confirms by hand, at the cost of a couple of Gemini calls per image.

  2. check_retrain_trigger()
       WEEKLY. A HYSTERESIS trigger that decides when enough NEW gold data has
       accumulated to justify an (off-box) retrain. It does NOT train on the box -
       training is GPU work we run elsewhere. It only SIGNALS that a retrain is due and
       drops a 'retrain_requested' marker (a dataset-snapshot request) for the off-box
       pipeline / ops to pick up.

HOW THIS CONNECTS TO THE REST OF THE SYSTEM
  - Reads screenshots from afc_tournament_and_scrims.MatchResultImage (the same image
    store OCRSession.image points at).
  - Calls afc_ocr.services.gemini.call_gemini (the teacher) for autolabelling.
  - Writes afc_ocr.models.OCRTrainingPair rows (source='gemini_autolabel'), reusing the
    SAME split logic (_assign_split) and the SAME content-addressed image store
    (media/ocr_training/<sha>.<ext>) that services.training_capture uses, so silver and
    gold data live side by side and an exporter treats them uniformly.
  - check_retrain_trigger counts GOLD pairs (source='admin_review') captured by
    services.training_capture, and writes its marker under media/ocr_retrain/ (gitignored).
  - The trained bundle that eventually comes back is deployed + flipped live by
    afc_ocr.services.model_registry.promote (the blue-green swap). This file requests the
    retrain; model_registry serves the result.

DEFENSIVENESS (hard rule, mirrors training_capture)
  A background task must NEVER crash the worker. Every Gemini call and every row write is
  wrapped so one bad image is skipped, not fatal. The tasks degrade to no-ops on missing
  config (no API key, no images) and always return a small summary dict for the logs.

LOCAL DEV
  Mirrors the rankings RANKINGS_RECALC_SYNC pattern: OCR_ML_SYNC (defaults to DEBUG) lets
  a developer run these inline (e.g. from the shell or a management command) without a
  Celery worker. The beat schedule still only fires in a real beat+worker deployment.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Tunables (env-overridable, with sane defaults). Kept at module top so an operator
# can see every knob in one place.
# ──────────────────────────────────────────────────────────────────────────────
# autolabel: how many un-labelled screenshots to process per nightly run. Capped so a
# huge backlog cannot blow the Gemini budget (or the run time) in one go; the backlog
# drains over several nights.
_AUTOLABEL_CAP = int(os.getenv("OCR_AUTOLABEL_CAP", "50"))
# autolabel consensus: each image is read TWICE at this temperature; the two reads must
# agree for the label to be trusted. 0.1 matches call_gemini's own generationConfig.
_AUTOLABEL_TEMP = 0.1

# retrain trigger (hysteresis):
#   N_NEW  = volume threshold; fire as soon as this many NEW gold pairs exist.
#   N_MIN  = floor for the weekly cadence path; fire on the weekly beat if at least this
#            many new gold pairs exist, even below N_NEW (so a slow-but-steady stream of
#            corrections still triggers a refresh roughly weekly instead of never).
#   MIN_DAYS_BETWEEN = cadence floor; never fire more than once per this many days, so a
#            burst of corrections cannot request a retrain every night.
_RETRAIN_N_NEW = int(os.getenv("OCR_RETRAIN_N_NEW", "200"))
_RETRAIN_N_MIN = int(os.getenv("OCR_RETRAIN_N_MIN", "50"))
_RETRAIN_MIN_DAYS_BETWEEN = int(os.getenv("OCR_RETRAIN_MIN_DAYS", "7"))

# Where retrain-request markers are dropped (gitignored; off-box pipeline + ops read these).
_RETRAIN_SUBDIR = "ocr_retrain"


def _sync() -> bool:
    """Run inline (no Celery worker) in local dev, exactly like afc_rankings does with
    RANKINGS_RECALC_SYNC. Defaults to DEBUG. This only affects callers that route through
    a _dispatch-style helper; beat-scheduled invocations always go through the worker."""
    return getattr(settings, "OCR_ML_SYNC", getattr(settings, "DEBUG", False))


# ══════════════════════════════════════════════════════════════════════════════
# TASK 1 - autolabel_backlog (nightly): teacher-label un-labelled screenshots via
# two-read Gemini CONSENSUS.
# ══════════════════════════════════════════════════════════════════════════════
@shared_task(queue="ocr_ml")
def autolabel_backlog(cap: int | None = None) -> dict:
    """Nightly: take MatchResultImage rows that have NO OCRTrainingPair yet, and label
    them with Gemini using two-read consensus.

    Pipeline per image (all of it inside a per-image try/except so one failure never
    aborts the run):
      1. Read the screenshot bytes from the MatchResultImage file.
      2. Call call_gemini TWICE at temp 0.1 (two independent reads).
      3. If the two reads AGREE (same normalised placements) -> write a SILVER pair:
           OCRTrainingPair(source='gemini_autolabel', is_clean=True, split per hash).
         If they DISAGREE -> write a FLAGGED pair (is_clean=False) for an admin to review
           later, rather than trusting one noisy read.
      4. Skip on any Gemini error (logged, counted) - never crash the worker.

    `cap` bounds how many images one run processes (defaults to OCR_AUTOLABEL_CAP). Returns
    a summary dict {processed, silver, flagged, skipped} for the logs.
    """
    # Lazy imports (keep the module import-light and avoid app-loading order surprises,
    # mirroring training_capture / commit.py).
    from afc_tournament_and_scrims.models import MatchResultImage
    from afc_ocr.models import OCRTrainingPair

    cap = cap if cap is not None else _AUTOLABEL_CAP

    # Gemini must be configured; without a key there is nothing to do (degrade to no-op).
    if not getattr(settings, "GEMINI_API_KEY", ""):
        logger.info("autolabel_backlog: GEMINI_API_KEY not set; skipping run.")
        return {"processed": 0, "silver": 0, "flagged": 0, "skipped": 0, "reason": "no_api_key"}

    # ── pick the backlog: images that have produced NO training pair yet ────────
    # A training pair links back to its source via OCRTrainingPair.session.image (the
    # MatchResultImage). We approximate "already labelled" as "has any OCRSession whose
    # training_pairs exist", but the robust, image-centric check is: no training pair
    # references this image at all. We resolve that through the session FK because
    # OCRTrainingPair has no direct image FK - it carries image_sha256 instead. To keep
    # this simple and correct WITHOUT re-hashing every image here, we treat an image as
    # "unlabelled" when it has no OCRSession that produced a committed training pair AND
    # no autolabel pair already exists for its content. We dedupe by content hash at
    # write time (below), so re-processing is harmless; this query just avoids the obvious
    # already-done rows cheaply.
    labelled_session_image_ids = set(
        OCRTrainingPair.objects
        .exclude(session__isnull=True)
        .exclude(session__image__isnull=True)
        .values_list("session__image_id", flat=True)
    )
    backlog = (
        MatchResultImage.objects
        .exclude(image_id__in=labelled_session_image_ids)
        .order_by("-uploaded_at")[: cap * 2]   # over-fetch; some may dedupe out by hash
    )

    processed = silver = flagged = skipped = 0

    for mri in backlog:
        if processed >= cap:
            break
        try:
            raw, mime = _read_image(mri)
            if not raw:
                skipped += 1
                continue

            # Content-address up front so we can dedupe against ANY existing pair
            # (gold or silver) for the same pixels before spending two Gemini calls.
            import hashlib
            sha = hashlib.sha256(raw).hexdigest()
            if OCRTrainingPair.objects.filter(image_sha256=sha).exists():
                # Already in the corpus (committed by an admin or autolabelled before).
                # Skip without calling Gemini.
                continue

            # ── two-read consensus ──────────────────────────────────────────
            read_a = _gemini_read(raw, mime)
            read_b = _gemini_read(raw, mime)
            if read_a is None or read_b is None:
                # A Gemini error on either read -> skip this image (do not guess).
                skipped += 1
                continue

            agree = _reads_agree(read_a, read_b)
            _write_autolabel_pair(
                mri=mri,
                image_bytes=raw,
                image_sha256=sha,
                raw_output=read_a,        # keep the first read as the engine raw_output
                final_json=read_a,        # consensus read becomes the (silver) truth
                is_clean=agree,           # agree -> trusted silver; disagree -> flagged
            )
            processed += 1
            if agree:
                silver += 1
            else:
                flagged += 1

        except Exception as exc:  # noqa: BLE001 - one bad image must never kill the run
            logger.exception("autolabel_backlog: failed on image %s: %s",
                             getattr(mri, "image_id", "?"), exc)
            skipped += 1

    summary = {"processed": processed, "silver": silver, "flagged": flagged, "skipped": skipped}
    logger.info("autolabel_backlog: %s", summary)
    return summary


def _read_image(mri) -> tuple[bytes | None, str]:
    """Read a MatchResultImage's bytes + guess its mime type. Returns (None, "") if the
    file is missing/unreadable so the caller skips it. mime guess mirrors the extensions
    training_capture recognises."""
    try:
        with mri.image.open("rb") as f:
            raw = f.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("autolabel_backlog: could not read image %s: %s",
                      getattr(mri, "image_id", "?"), exc)
        return None, ""
    name = (getattr(mri.image, "name", "") or "").lower()
    mime = "image/png" if name.endswith(".png") else ("image/webp" if name.endswith(".webp") else "image/jpeg")
    return raw, mime


def _gemini_read(image_bytes: bytes, mime_type: str) -> dict | None:
    """One Gemini teacher read, returning its result JSON or None on ANY error.

    We pass empty aliases/team_notes: autolabelling is recognition-truth capture, not
    roster matching, so we deliberately do not bias the read with known names (consistent
    with the recognition-vs-identity rule in models.py). Never raises; logs and returns
    None so the caller can skip cleanly."""
    from afc_ocr.services.gemini import call_gemini
    try:
        return call_gemini(image_bytes, mime_type, aliases=[], team_notes=[])
    except Exception as exc:  # noqa: BLE001 - skip on teacher error, never crash
        logger.warning("autolabel_backlog: Gemini read failed: %s", exc)
        return None


def _normalise_for_compare(result_json: dict) -> str:
    """Canonical, order-insensitive string form of a result JSON, for comparing two reads.

    Reduces {"placements":[{placement, players:[{name,kills}]}]} to a sorted tuple of
    (placement, sorted players) so that two reads which list the same rows in a different
    order still count as AGREEING. Whitespace-trimmed names, int-coerced kills. Defensive
    against missing keys."""
    placements = []
    for pl in (result_json or {}).get("placements", []) or []:
        players = sorted(
            ((p.get("name") or "").strip(), int(p.get("kills") or 0))
            for p in (pl.get("players") or [])
        )
        placements.append((pl.get("placement"), tuple(players)))
    placements.sort(key=lambda x: (x[0] is None, x[0]))
    return json.dumps(placements, sort_keys=True)


def _reads_agree(a: dict, b: dict) -> bool:
    """True when two Gemini reads describe the SAME screen (consensus). Compared on the
    normalised, order-insensitive form so cosmetic ordering differences do not count as
    disagreement."""
    return _normalise_for_compare(a) == _normalise_for_compare(b)


def _write_autolabel_pair(mri, image_bytes, image_sha256, raw_output, final_json, is_clean):
    """Persist one silver/flagged training pair from an autolabel result.

    Reuses the EXACT storage + split conventions of services.training_capture so silver
    and gold data are interchangeable downstream:
      - content-addressed image at media/ocr_training/<sha>.<ext> (deduped),
      - split assigned deterministically from the hash (_assign_split),
      - source='gemini_autolabel', teacher_model from the gemini service.
    is_clean carries the consensus result: True = both reads agreed (trusted silver),
    False = reads disagreed (flagged for admin review). We do NOT emit OCRCropLabel rows
    here (those are the high-precision per-cell labels the cropper derives from gold/admin
    data); autolabel contributes screen-level silver pairs only.
    """
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    from afc_ocr.models import OCRTrainingPair, _assign_split

    # Same content-addressed store training_capture uses (media/ocr_training/<sha>.<ext>).
    name = (getattr(mri.image, "name", "") or "").lower()
    ext = ".png" if name.endswith(".png") else (".webp" if name.endswith(".webp") else ".jpg")
    rel_path = f"ocr_training/{image_sha256}{ext}"
    if not default_storage.exists(rel_path):
        default_storage.save(rel_path, ContentFile(image_bytes))

    teacher_model = None
    try:
        from afc_ocr.services.gemini import GEMINI_MODEL
        teacher_model = GEMINI_MODEL
    except Exception:  # noqa: BLE001
        teacher_model = None

    # event_type: derive from the match's event when available, else default to 'team'
    # (the dominant format). Best-effort - a wrong default here only mislabels the format
    # tag, never the recognition truth.
    event_type = _event_type_for_match(getattr(mri, "match", None))

    OCRTrainingPair.objects.create(
        session=None,                      # autolabel has no review session
        match=getattr(mri, "match", None),
        image_sha256=image_sha256,
        image_path=rel_path,
        event_type=event_type,
        raw_output=raw_output or {},
        final_json=final_json or {},
        source="gemini_autolabel",
        teacher_model=teacher_model,
        num_corrections=0,
        edit_distance=0,
        is_clean=bool(is_clean),           # consensus agree -> trusted; disagree -> flagged
        split=_assign_split(image_sha256),
        dataset_version=None,
        created_by=None,                   # machine-generated, no human author
    )


def _event_type_for_match(match) -> str:
    """Best-effort 'solo'/'team' for a match, mirroring gemini.get_prompt_context's logic.
    Defaults to 'team' (the common case) on any uncertainty - this only tags the format,
    never the labels."""
    if match is None:
        return "team"
    try:
        from afc_ocr.services.gemini import _get_event
        event = _get_event(match)
        if event and getattr(event, "participant_type", None) == "solo":
            return "solo"
    except Exception:  # noqa: BLE001
        pass
    return "team"


# ══════════════════════════════════════════════════════════════════════════════
# TASK 2 - check_retrain_trigger (weekly): hysteresis "is a retrain due?" signal.
# ══════════════════════════════════════════════════════════════════════════════
@shared_task(queue="ocr_ml")
def check_retrain_trigger() -> dict:
    """Weekly: decide whether enough NEW gold data justifies an (off-box) retrain, and if
    so, emit a 'retrain_requested' marker. Does NOT train on the box.

    HYSTERESIS (two ways to fire, one way it stays quiet):
      Fire when EITHER
        (a) VOLUME: new gold pairs since the last request >= N_NEW (default 200), OR
        (b) CADENCE: it has been >= MIN_DAYS_BETWEEN days since the last request AND there
            are >= N_MIN (default 50) new gold pairs.
      Never fire when fewer than MIN_DAYS_BETWEEN days have passed since the last request
      (the cadence floor) UNLESS the volume threshold (a) is hit - a big enough batch
      always wins. This hysteresis stops the trigger from firing every night on a trickle
      of corrections while still guaranteeing a refresh roughly weekly once real data flows.

    "New gold pairs" = OCRTrainingPair(source='admin_review') created AFTER the timestamp of
    the last retrain request (read from the most recent marker). On a cold corpus (no admin
    pairs, no prior marker) it simply does not fire and logs that it ran. Returns a summary
    dict either way. NEVER trains; only SIGNALS (writes a marker + logs).
    """
    from afc_ocr.models import OCRTrainingPair

    last_request_at = _last_retrain_request_at()
    days_since = (
        (now() - last_request_at).days if last_request_at else None
    )

    # Count NEW gold (admin-confirmed) pairs since the last request. These are the
    # high-precision examples a retrain actually wants; silver autolabel pairs are NOT
    # counted toward the trigger (they did not move the needle on label quality).
    gold_qs = OCRTrainingPair.objects.filter(source="admin_review")
    if last_request_at:
        gold_qs = gold_qs.filter(created_at__gt=last_request_at)
    n_new_gold = gold_qs.count()

    # ── decide, with hysteresis ─────────────────────────────────────────────
    fire = False
    reason = None

    if n_new_gold >= _RETRAIN_N_NEW:
        # (a) VOLUME path: a big enough batch always fires, even before the cadence floor.
        fire, reason = True, "volume"
    elif (
        n_new_gold >= _RETRAIN_N_MIN
        and (last_request_at is None or (days_since is not None and days_since >= _RETRAIN_MIN_DAYS_BETWEEN))
    ):
        # (b) CADENCE path: weekly floor met AND a meaningful (but sub-N_NEW) batch exists.
        fire, reason = True, "cadence"

    summary = {
        "fired": fire,
        "reason": reason,
        "n_new_gold": n_new_gold,
        "days_since_last": days_since,
        "n_new_threshold": _RETRAIN_N_NEW,
        "n_min_threshold": _RETRAIN_N_MIN,
        "min_days_between": _RETRAIN_MIN_DAYS_BETWEEN,
    }

    if fire:
        marker = _write_retrain_marker(reason=reason, n_new_gold=n_new_gold)
        summary["marker"] = marker
        logger.info("check_retrain_trigger: RETRAIN REQUESTED (%s); marker=%s; %s",
                   reason, marker, summary)
    else:
        # Not firing is the common, healthy case - log at info so a beat run is auditable.
        logger.info("check_retrain_trigger: no retrain due; %s", summary)

    return summary


def _retrain_dir() -> str:
    """media/ocr_retrain/, created on demand. Holds the retrain-request markers (gitignored).
    Each marker is a JSON file the off-box pipeline / ops poll to know a snapshot is due."""
    d = os.path.join(settings.MEDIA_ROOT, _RETRAIN_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _last_retrain_request_at():
    """Timestamp of the most recent retrain request, read from the newest marker file, or
    None if none exists yet. Drives both the 'new since last request' count and the cadence
    floor. Returns an aware datetime (UTC) so it compares cleanly with django.utils.now()."""
    d = _retrain_dir()
    try:
        markers = [f for f in os.listdir(d) if f.startswith("retrain_requested_") and f.endswith(".json")]
    except OSError:
        return None
    if not markers:
        return None
    # Marker filenames are timestamp-prefixed (sortable); the newest is the last request.
    latest = sorted(markers)[-1]
    try:
        with open(os.path.join(d, latest), encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("requested_at")
        if ts:
            # Stored as ISO-8601 UTC.
            return datetime.fromisoformat(ts)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def _write_retrain_marker(reason: str, n_new_gold: int) -> str:
    """Write a 'retrain_requested' marker = the dataset-SNAPSHOT request for the off-box
    trainer. Returns the marker's absolute path.

    The marker is intentionally just a signal + metadata, NOT a dataset: it records WHY the
    retrain fired, HOW MANY new gold pairs triggered it, and a suggested next student
    version (current active version + 1) so the off-box run knows what to call its output.
    The trainer reads gold+silver OCRTrainingPair rows from the DB directly when it runs;
    this file only says "a snapshot is due". Timestamped filename so markers are append-only
    and sortable (newest = last request)."""
    from afc_ocr.services import model_registry

    ts = datetime.now(timezone.utc)
    # Suggest the next version label = active version + 1 when numeric, else "next".
    active = model_registry.active_version()
    try:
        suggested = str(int(active) + 1) if active is not None else "1"
    except (TypeError, ValueError):
        suggested = "next"

    payload = {
        "requested_at": ts.isoformat(),
        "reason": reason,                      # 'volume' or 'cadence'
        "n_new_gold": n_new_gold,
        "active_version": active,
        "suggested_next_version": suggested,
        "note": (
            "Off-box retrain request. Train a student recognizer (rec.onnx + rec_keys.txt "
            "+ VERSION) from OCRTrainingPair gold+silver data, deploy to "
            "media/models/student_v<version>/, then promote via "
            "`manage.py ocr_model --promote <version>`."
        ),
    }
    fname = f"retrain_requested_{ts.strftime('%Y%m%dT%H%M%SZ')}.json"
    path = os.path.join(_retrain_dir(), fname)
    # Atomic write (temp + replace) so a poller never reads a half-written marker.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    return path
