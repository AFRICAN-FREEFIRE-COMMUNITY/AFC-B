"""
afc_ocr/services/extract.py
================================================================================
The ONE shared OCR extraction service: local-first (the self-hosted student) with a
Gemini fallback (the teacher). This is the single place screenshot bytes become the
canonical {"placements": [...]} draft, so the local-vs-Gemini routing lives in one
auditable spot.

WHY THIS MODULE EXISTS (P2)
    The routing logic used to live ONLY inside afc_ocr.views._extract_with_router, where it
    was reachable solely by the event OCR upload paths. The P2 standalone-leaderboard OCR
    assist (afc_leaderboard.views.ocr_extract) needs the SAME extraction without the event
    commit machinery, so the body was lifted here and BOTH callers delegate to extract_rows:
      - afc_ocr.views._extract_with_router  -> thin wrapper (event flow, behavior-preserving).
      - afc_leaderboard.views.ocr_extract   -> calls extract_rows directly (standalone flow),
        passing prompt_kind="team_standings" for team leaderboards so Gemini also reads a
        team_name per placement.

HOW IT CONNECTS
    - Calls services.gemini.call_gemini (the teacher) and services.local_ocr.get_engine()
      (the student), gated by services.ocr_confidence.gate.
    - prompt_kind is threaded down into call_gemini -> build_prompt so a caller can pick the
      solo/team_standings prompt variant. The local student ignores prompt_kind (it structures
      from pixels, not a prompt) and is only used for the event flow's default behavior.
    - Returns (raw_output: dict, engine: str) exactly as the old router did, so the draft-row
      build / match_name / commit path are untouched.
"""
import logging

from .gemini import call_gemini, effective_model

logger = logging.getLogger(__name__)


def extract_rows(image_bytes, mime_type, event_type, aliases=None, team_notes=None, prompt_kind=None):
    """LOCAL-FIRST OCR extraction with Gemini fallback (the self-hosted OCR student, P3).

    Returns (raw_output: dict, engine: str). This is the ONE place the upload paths get their
    extraction, so the local-vs-Gemini routing lives in a single auditable spot. Flow:
      1. If local-first is enabled and the local engine is available, run the student
         (services/local_ocr) and ask the confidence gate (services/ocr_confidence) whether
         to trust it.
      2. gate == "local"  -> use the student's output, ZERO Gemini calls (the cost win).
      3. otherwise         -> escalate to Gemini (the teacher), exactly as before.
    Graceful degradation (mirrors the old 503 handling): if the local engine errors or is
    absent we go to Gemini; if Gemini is disabled/unavailable we serve the student's
    best-effort draft so the admin can still review (never a hard fail when SOME engine ran).
    `engine` is returned + persisted (raw_output["_engine"]) so the FE "which engine" badge
    and the training corpus (teacher_model) know the source. raw_output keeps Gemini's exact
    shape {"placements": [...]} so the draft build / match_name / commit path are untouched.

    prompt_kind selects the Gemini prompt variant (None/"solo" = the existing player prompt,
    "team_standings" = additionally read a team_name per placement). It is threaded into
    call_gemini; the local student ignores it (it structures from layout, not a prompt). The
    default (prompt_kind=None) reproduces the event flow's pre-P2 behavior exactly.
    """
    from django.conf import settings
    from . import local_ocr, ocr_confidence

    gemini_enabled = getattr(settings, "OCR_GEMINI_FALLBACK", True) and bool(getattr(settings, "GEMINI_API_KEY", None))

    student_json, conf, decision = None, None, "gemini"
    if getattr(settings, "OCR_LOCAL_FIRST", True) and local_ocr.is_available():
        try:
            student_json, conf = local_ocr.get_engine().run(image_bytes, mime_type, aliases, team_notes, event_type)
            decision = ocr_confidence.gate(student_json, conf)["decision"]
        except Exception:
            logger.exception("local OCR student failed; escalating to Gemini")
            student_json, decision = None, "gemini"

    if decision == "local" and student_json is not None:
        return student_json, f"local_student_{(conf or {}).get('model_version', 'v0')}"

    if gemini_enabled:
        # Label the engine with the ACTUAL model used (settings.GEMINI_MODEL, default flash), not a
        # hardcoded "pro" — so the FE badge + the training corpus record the real teacher model.
        return call_gemini(image_bytes, mime_type, aliases, team_notes, prompt_kind=prompt_kind), effective_model()

    if student_json is not None:  # Gemini off/unavailable: best-effort local draft for review
        return student_json, f"local_best_effort_{(conf or {}).get('model_version', 'v0')}"

    raise RuntimeError("No OCR engine available (local unavailable and Gemini disabled).")
