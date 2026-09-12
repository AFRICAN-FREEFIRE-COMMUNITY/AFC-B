"""
afc_ocr/services/providers/gemini.py - Google Gemini behind the shared provider contract.

The heavy lifting stays in services.gemini.call_gemini (the retries, the key-stripped errors, the
no-candidates handling), which now takes the organization's key and model. This wrapper only
turns its ValueError / RuntimeError into a ProviderError so the caller sees one exception type
whatever the provider.
"""
from ..gemini import call_gemini
from .base import ProviderError


def read(image_bytes: bytes, mime_type: str, prompt: str, *, api_key: str, model: str, base_url: str = "",
         aliases=None, team_notes=None, prompt_kind=None) -> dict:
    # call_gemini builds its own prompt from aliases / team_notes / prompt_kind (the same prompt
    # the other adapters receive as `prompt`); it is passed through so the Gemini path is unchanged.
    try:
        return call_gemini(image_bytes, mime_type, aliases or [], team_notes or [], prompt_kind=prompt_kind,
                           api_key=api_key, model=model)
    except (ValueError, RuntimeError) as exc:
        raise ProviderError(str(exc)[:280]) from None
