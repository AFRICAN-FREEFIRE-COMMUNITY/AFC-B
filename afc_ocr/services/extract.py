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
import threading
import time

from django.db import transaction
from django.utils import timezone

from .gemini import build_prompt, effective_model
from .providers import Credentials, ProviderError, read_with

logger = logging.getLogger(__name__)


class OcrKeyRequired(Exception):
    """The screenshot needs the AI engine, the organization has no working key and no free read
    left. The message is the sentence the organizer reads; views answer it as HTTP 402 with
    {"message", "code": "ocr_key_required", "organization_slug"} so the page can link to the
    connect page. Raised by resolve_credentials, never past a view."""

    def __init__(self, organization):
        self.organization = organization
        super().__init__(
            "That was your free read, or it has been used. Connect your own AI key to keep using OCR."
        )


FAILURES_BEFORE_NOTICE = 3


def _is_staff(user) -> bool:
    """AFC staff read on AFC's key whatever the event's organization (owner 2026-09-12)."""
    if not user:
        return False
    if getattr(user, "role", None) in ("admin", "moderator", "support"):
        return True
    try:
        from afc_tournament_and_scrims.views import _is_event_admin
        return bool(_is_event_admin(user))
    except Exception:  # noqa: BLE001 - a permission helper must never break a read
        return False


def resolve_credentials(org, actor):
    """Whose key pays for this escalated read, in the owner's order (2026-09-12):
      1. AFC staff, or no organization (an AFC-run event): AFC's key.
      2. The organization has a connected key that opens: the organization's key.
      3. The organization still has a free read: AFC's key, and the read is spent right here,
         under a row lock so two uploads at once cannot both take the last one.
      4. Otherwise OcrKeyRequired: nothing is read, nothing is billed.
    Returns a Credentials with paid_by set, or raises OcrKeyRequired."""
    from django.conf import settings

    afc = Credentials(provider="gemini", model=effective_model(),
                      api_key=getattr(settings, "GEMINI_API_KEY", "") or "", paid_by="afc")
    if org is None or _is_staff(actor):
        return afc
    if getattr(org, "ocr_disabled", False):
        raise OcrKeyRequired(org)
    try:
        key = org.ai_key
    except Exception:  # RelatedObjectDoesNotExist
        key = None
    if key is not None:
        plain = key.get_key()
        if plain:
            return Credentials(provider=key.provider, model=key.model, api_key=plain,
                               base_url=key.base_url, paid_by="org")
    # the free read, spent atomically
    from afc_organizers.models import Organization
    with transaction.atomic():
        row = Organization.objects.select_for_update().get(pk=org.pk)
        if row.ocr_free_reads_left > 0:
            row.ocr_free_reads_left -= 1
            row.save(update_fields=["ocr_free_reads_left"])
            org.ocr_free_reads_left = row.ocr_free_reads_left
            return Credentials(provider="gemini", model=afc.model, api_key=afc.api_key, paid_by="afc_free")
    raise OcrKeyRequired(org)


def _record_usage(creds, org, actor, ok, latency_ms, error="", event=None, leaderboard=None):
    """One OcrUsage row per AI read, and the key's own health stamps. Never raises."""
    try:
        from afc_ocr.models import OcrUsage
        from afc_ocr.services.providers import registry
        price = 0
        try:
            price = registry.get(creds.provider).get("cost_per_image_usd", 0)
        except KeyError:
            pass
        OcrUsage.objects.create(
            organization=org, event=event, leaderboard=leaderboard, actor=actor if getattr(actor, "user_id", None) else None,
            paid_by=creds.paid_by, provider=creds.provider, model=creds.model, ok=ok,
            latency_ms=latency_ms, error=(error or "")[:300], cost_estimate_usd=price if ok else 0,
        )
        if creds.paid_by == "org" and org is not None:
            _stamp_org_key(org, ok, error)
    except Exception:  # noqa: BLE001
        logger.exception("could not record OCR usage")


def _stamp_org_key(org, ok, error):
    """Refresh the key's last-tested stamps from a real read; tell the owner at the third failure
    in a row (a revoked key, an empty balance) with the provider's message and a link to the page."""
    from afc_organizers.models import OrganizationAiKey
    key = OrganizationAiKey.objects.filter(organization=org).first()
    if key is None:
        return
    key.last_tested_at = timezone.now()
    key.last_test_ok = ok
    key.last_error = "" if ok else (error or "")[:300]
    key.consecutive_failures = 0 if ok else key.consecutive_failures + 1
    key.save(update_fields=["last_tested_at", "last_test_ok", "last_error", "consecutive_failures", "updated_at"])
    if not ok and key.consecutive_failures == FAILURES_BEFORE_NOTICE:
        try:
            from afc_auth.models import Notifications
            from afc_organizers.models import OrganizationMember
            owners = OrganizationMember.objects.filter(organization=org, role="owner", status="active").select_related("user")
            Notifications.objects.bulk_create([
                Notifications(
                    user=m.user,
                    title=f"Your AI key is failing: {org.name}",
                    message=(f"Three screenshot reads in a row failed on your {key.provider} key. "
                             f"The provider said: {key.last_error or 'no message'}. Open Organization settings, AI key, and test or replace it."),
                    notification_type="ocr_key_failing", target_type="organization", target_id=str(org.slug),
                ) for m in owners
            ])
        except Exception:  # noqa: BLE001
            logger.exception("could not notify the org owner about a failing AI key")


def ai_read(creds, image_bytes, mime_type, aliases, team_notes, prompt_kind, org=None, actor=None,
            event=None, leaderboard=None):
    """One AI read on the given credentials, recorded either way. Raises ProviderError."""
    prompt = build_prompt(aliases or [], team_notes or [], prompt_kind=prompt_kind)
    started = time.monotonic()
    try:
        out = read_with(creds, image_bytes, mime_type, prompt, aliases=aliases or [], team_notes=team_notes or [],
                        prompt_kind=prompt_kind)
    except ProviderError as exc:
        _record_usage(creds, org, actor, False, int((time.monotonic() - started) * 1000), exc.message,
                      event=event, leaderboard=leaderboard)
        raise
    _record_usage(creds, org, actor, True, int((time.monotonic() - started) * 1000), event=event, leaderboard=leaderboard)
    return out

# Serializes local-student inference across threads. The batch OCR worker
# (afc_leaderboard.ocr.process_job) reads a map's several screenshots CONCURRENTLY - 
# the win is overlapping the Gemini HTTP calls - but the student is one shared
# process-wide engine (local_ocr._ENGINE, lazily built) doing CPU-bound ONNX work,
# so concurrent .run() would race the lazy build and thrash the CPU for no speedup.
# One lock here covers every caller; single-image callers never contend on it.
_STUDENT_LOCK = threading.Lock()


def extract_rows(image_bytes, mime_type, event_type, aliases=None, team_notes=None, prompt_kind=None,
                 org=None, actor=None, event=None, leaderboard=None):
    """LOCAL-FIRST OCR extraction with an AI fallback (the self-hosted OCR student, P3).

    Own-key OCR (owner 2026-09-12): `org` is the organization that owns the event / leaderboard
    being read (None for AFC-run), `actor` the user reading. When the screenshot is escalated
    past the local engine, resolve_credentials decides whose key pays (AFC staff -> AFC; the
    org's key; the org's single free read; else OcrKeyRequired, which the views answer as 402).
    Every AI read writes an OcrUsage row. `engine` names the provider and model that read it.

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

    gemini_enabled = getattr(settings, "OCR_GEMINI_FALLBACK", True)

    student_json, conf, decision = None, None, "gemini"
    if getattr(settings, "OCR_LOCAL_FIRST", True) and local_ocr.is_available():
        try:
            # Lock: see _STUDENT_LOCK above - the student is a shared CPU-bound engine, so
            # concurrent batch threads take turns here while their Gemini calls overlap freely.
            with _STUDENT_LOCK:
                student_json, conf = local_ocr.get_engine().run(image_bytes, mime_type, aliases, team_notes, event_type)
            decision = ocr_confidence.gate(student_json, conf)["decision"]
        except Exception:
            logger.exception("local OCR student failed; escalating to Gemini")
            student_json, decision = None, "gemini"

    if decision == "local" and student_json is not None:
        return student_json, f"local_student_{(conf or {}).get('model_version', 'v0')}"

    if gemini_enabled:
        # Whose key: the organization's own, AFC's free read, or AFC's (staff / AFC-run). A missing
        # key raises OcrKeyRequired for the views to turn into the connect-your-key answer.
        creds = resolve_credentials(org, actor)
        if not creds.api_key:
            if student_json is not None:
                return student_json, f"local_best_effort_{(conf or {}).get('model_version', 'v0')}"
            raise RuntimeError("No OCR engine available (local unavailable and no AI key configured).")
        out = ai_read(creds, image_bytes, mime_type, aliases, team_notes, prompt_kind,
                      org=org, actor=actor, event=event, leaderboard=leaderboard)
        # Label the engine with the ACTUAL provider + model used, so the FE badge + the training
        # corpus record the real teacher.
        engine = creds.model if creds.provider == "gemini" else f"{creds.provider}:{creds.model}"
        out["_paid_by"] = creds.paid_by
        return out, engine

    if student_json is not None:  # Gemini off/unavailable: best-effort local draft for review
        return student_json, f"local_best_effort_{(conf or {}).get('model_version', 'v0')}"

    raise RuntimeError("No OCR engine available (local unavailable and Gemini disabled).")
