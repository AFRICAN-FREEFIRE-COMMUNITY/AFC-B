"""
afc_ocr/services/providers/base.py - the one contract every AI provider adapter meets.

WHY (owner 2026-09-12, "virtually any api key from any ai they can manage")
    OCR used to know exactly one teacher, Gemini, on AFC's key. Now an organization brings its own
    key from whichever provider it manages. Three adapters cover the list (Gemini; anything that
    speaks the OpenAI request shape, which is nearly everyone; Anthropic), and every one of them
    turns screenshot bytes into the SAME draft the rest of the pipeline already understands:

        {"placements": [{"placement": 1, "team_name": "...", "kills": 7, "players": [...]}, ...]}

    so the row builders, the name matching and the commit path never learn which provider read
    the image. The prompt is the one written for Gemini (services.gemini.build_prompt); the owner
    chose to ship the other adapters on it rather than tune each on its own key.

WHAT THIS FILE HOLDS
    ProviderError      what an adapter raises, carrying the provider's OWN message (key-free) so the
                       organizer reads "Incorrect API key provided" rather than a stack trace
    Credentials        the resolved key for one read: provider, model, base_url, api_key, paid_by
    parse_placements   the shared JSON extraction: strips code fences, validates the shape, and
                       lets the adapter retry once with a "JSON only" nudge on malformed output
"""
import json
import re
from dataclasses import dataclass


class ProviderError(RuntimeError):
    """The provider refused or failed. `message` is safe to show to the organizer and never
    contains the key (adapters strip request URLs and headers before raising)."""

    def __init__(self, message: str, status: int = None):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Credentials:
    provider: str          # registry id
    model: str
    api_key: str
    base_url: str = ""     # custom OpenAI-compatible only
    paid_by: str = "afc"   # "org" | "afc_free" | "afc"  (afc_ocr.models.OcrUsage.PAID_BY)


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_placements(text: str) -> dict:
    """The model's reply as the canonical draft, or raise ValueError when it is not one.

    Models wrap JSON in ``` fences, prepend "Here is the JSON:", or answer with a bare list; all
    of that is accepted. What is NOT accepted: no `placements` list, or placements that are not
    objects. The adapter catches ValueError once and re-asks with a stricter instruction; the
    second failure becomes a ProviderError the organizer can read.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty reply")
    body = text.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1)
    # Some models add a sentence before the JSON: take the outermost object or list.
    start = min((i for i in (body.find("{"), body.find("[")) if i >= 0), default=-1)
    if start > 0:
        body = body[start:]
    data = json.loads(body)
    if isinstance(data, list):
        data = {"placements": data}
    if not isinstance(data, dict) or not isinstance(data.get("placements"), list):
        raise ValueError("no placements list")
    for row in data["placements"]:
        if not isinstance(row, dict):
            raise ValueError("a placement is not an object")
    return data


JSON_ONLY_NUDGE = (
    "\n\nAnswer with the JSON object only. No prose, no markdown fences, no explanation: the first "
    "character of your reply must be { and the last must be }."
)
