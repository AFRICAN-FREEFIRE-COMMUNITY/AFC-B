"""
afc_ocr/services/ocr_confidence.py
================================================================================
The escalation gate: given the local student's output + its confidence signals
(produced by services/local_ocr.LocalOCREngine._confidence), decide whether to TRUST the
local read or DEFER to the Gemini teacher. This is the single decision the router in
afc_ocr/views.py consults; keeping it here keeps the policy in one auditable place and lets
the thresholds move to settings/env without touching the engine or the views.

DECISIONS:
  "local"   trust the student entirely  -> zero Gemini calls (the goal; the cost KPI is the
            fraction of images that reach this verdict).
  "gemini"  the whole read is untrustworthy -> call Gemini on the full image.
  "hybrid"  the read is mostly good but specific crops are weak -> (future) re-read only the
            weak crops with Gemini and splice. Until the engine emits per-crop confidence,
            the gate never returns "hybrid"; it is reserved here so the router contract is
            stable and the hybrid path can light up once crop-level scoring lands.

COLD START: when no local model is deployed (model_registry.active_model_dir() is None) the
engine reports ok=False, so the gate returns "gemini" for everything. That is the designed
Gemini-heavy day-one behaviour; the local share climbs as the student is trained + the
deterministic crop is calibrated (services/local_ocr structuring is v0 today).

GRACEFUL DEGRADATION: the router (not this gate) handles Gemini being unavailable. The gate
only states the IDEAL routing; if Gemini is down the router falls back to the best-effort
local draft so the admin can still review (mirrors the existing 503 handling).
"""

from __future__ import annotations

from django.conf import settings


# Defaults are conservative (trust the local read only when it is clearly sound). Overridable
# via env so the cost/accuracy trade can be tuned in prod without a code change.
def _threshold(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def gate(result_json: dict, confidence: dict) -> dict:
    """Return {"decision": "local"|"gemini"|"hybrid", "reason": str, "escalate_crops": [...]}.

    `confidence` is the dict from LocalOCREngine._confidence: keys ok, mean_score, min_score,
    named_frac, oversized_frac, n_players, n_placements, max_players_in_placement.
    """
    min_mean = _threshold("OCR_GATE_MIN_MEAN_SCORE", 0.80)
    min_named = _threshold("OCR_GATE_MIN_NAMED_FRAC", 0.70)
    max_oversized = _threshold("OCR_GATE_MAX_OVERSIZED_FRAC", 0.15)

    # The engine already folds structural sanity into `ok`; the gate re-derives the same
    # decision from the raw numbers so the policy (and its thresholds) live HERE, not split
    # between the engine and the gate. They agree today; the engine's `ok` is the fast path.
    placements = result_json.get("placements") or []
    if not placements or not confidence.get("ok"):
        return {"decision": "gemini", "reason": _why(confidence), "escalate_crops": []}

    mean_score = float(confidence.get("mean_score", 0.0))
    named_frac = float(confidence.get("named_frac", 0.0))
    oversized_frac = float(confidence.get("oversized_frac", 1.0))

    if mean_score < min_mean:
        return {"decision": "gemini", "reason": f"mean_score {mean_score:.2f} < {min_mean}", "escalate_crops": []}
    if named_frac < min_named:
        return {"decision": "gemini", "reason": f"named_frac {named_frac:.2f} < {min_named}", "escalate_crops": []}
    if oversized_frac > max_oversized:
        return {"decision": "gemini", "reason": f"oversized_frac {oversized_frac:.2f} > {max_oversized}", "escalate_crops": []}

    return {"decision": "local", "reason": "student confident + structurally sound", "escalate_crops": []}


def _why(confidence: dict) -> str:
    """Human-readable reason the local read was not trusted (for logs + the teacher_model
    audit trail)."""
    if not confidence.get("n_boxes"):
        return "no text recognized"
    if not confidence.get("n_placements"):
        return "no placements structured"
    return confidence.get("reason") or "low confidence / structurally implausible"
